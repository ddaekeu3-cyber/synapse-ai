---
title: "Agent Doesn't Implement Health Check Propagation for Dependent Services"
description: "Agents that report themselves as healthy even when their downstream dependencies — databases, search APIs, embedding services, external LLMs — are degraded cause silent failures: requests succeed but produce wrong results, or they fail with cryptic errors instead of a clean 'service unavailable'. Implement health check propagation that aggregates dependency health into a composite agent health status, with separate liveness and readiness signals."
date: 2026-04-16
difficulty: intermediate
category: reliability
slug: agent-doesnt-implement-health-check-propagation-for-dependent-services
tags: [health-check, dependency-health, liveness, readiness, composite-health, service-mesh]
symptoms:
  - "Agent's /health returns 200 OK but all tool calls fail because the database is down"
  - "Load balancer routes traffic to an agent instance whose search dependency is degraded"
  - "No readiness distinction — agent receives traffic before its embedding model is warmed up"
  - "Dependency failure manifests as cryptic 500 errors instead of clean 503 responses"
  - "No alert fires when a critical dependency degrades — only when the agent itself crashes"
---

## Why This Happens

Agent health endpoints return a fixed 200 OK because they only check the process itself — a simple `{"status": "ok"}`. They don't probe dependent services. Kubernetes liveness and readiness probes use the same endpoint and cannot distinguish "process is running" from "process can serve requests correctly." Health check propagation adds a dependency check layer: each dependency is probed on a schedule, results are aggregated into a composite health status, and the readiness endpoint reports unhealthy when any critical dependency is degraded.

## Solution 1: Dependency Health Record

```python
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class HealthStatus(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"


class DependencyCriticality(str, Enum):
    CRITICAL = "critical"     # agent cannot function without it
    HIGH = "high"             # significant degradation without it
    LOW = "low"               # nice to have — agent can limp along


@dataclass
class DependencyHealthRecord:
    dependency_name: str
    criticality: DependencyCriticality
    status: HealthStatus = HealthStatus.UNKNOWN
    last_check_at: Optional[float] = None
    last_success_at: Optional[float] = None
    consecutive_failures: int = 0
    last_error: Optional[str] = None
    latency_ms: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def mark_success(self, latency_ms: float) -> None:
        now = time.time()
        self.status = HealthStatus.HEALTHY
        self.last_check_at = now
        self.last_success_at = now
        self.consecutive_failures = 0
        self.last_error = None
        self.latency_ms = latency_ms

    def mark_failure(self, error: str, latency_ms: float = 0.0) -> None:
        now = time.time()
        self.consecutive_failures += 1
        self.last_check_at = now
        self.last_error = error[:200]
        self.latency_ms = latency_ms
        self.status = (
            HealthStatus.DEGRADED if self.consecutive_failures < 3
            else HealthStatus.UNHEALTHY
        )

    def time_since_last_success_seconds(self) -> Optional[float]:
        if self.last_success_at is None:
            return None
        return time.time() - self.last_success_at
```

## Solution 2: Dependency Health Prober

```python
import asyncio
import time
from typing import Callable, Dict, List


class DependencyHealthProber:
    """
    Executes health check probes for all registered dependencies.
    Each probe is an async callable that raises on failure or returns latency on success.
    Probes run on a configurable schedule and results populate HealthRecords.
    """

    def __init__(
        self,
        check_interval_seconds: float = 30.0,
        probe_timeout_seconds: float = 5.0,
    ):
        self._interval = check_interval_seconds
        self._timeout = probe_timeout_seconds
        self._probes: Dict[str, tuple] = {}   # name -> (record, probe_fn)
        self._probe_task: asyncio.Task = None

    def register(
        self,
        record: DependencyHealthRecord,
        probe_fn: Callable[[], None],
    ) -> None:
        """probe_fn: async callable, returns None on success, raises on failure."""
        self._probes[record.dependency_name] = (record, probe_fn)

    async def probe_once(self, dependency_name: str) -> DependencyHealthRecord:
        record, probe_fn = self._probes[dependency_name]
        start = time.time()
        try:
            await asyncio.wait_for(probe_fn(), timeout=self._timeout)
            latency = (time.time() - start) * 1000
            record.mark_success(latency)
        except asyncio.TimeoutError:
            latency = (time.time() - start) * 1000
            record.mark_failure(f"timeout after {self._timeout}s", latency)
        except Exception as exc:
            latency = (time.time() - start) * 1000
            record.mark_failure(str(exc)[:200], latency)
        return record

    async def probe_all(self) -> List[DependencyHealthRecord]:
        tasks = [self.probe_once(name) for name in self._probes]
        return await asyncio.gather(*tasks)

    def start(self) -> None:
        self._probe_task = asyncio.create_task(self._probe_loop())

    def stop(self) -> None:
        if self._probe_task:
            self._probe_task.cancel()

    async def _probe_loop(self) -> None:
        while True:
            await self.probe_all()
            await asyncio.sleep(self._interval)

    def records(self) -> List[DependencyHealthRecord]:
        return [r for r, _ in self._probes.values()]
```

## Solution 3: Composite Health Aggregator

```python
from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass
class CompositeHealthReport:
    liveness: HealthStatus        # is the process alive?
    readiness: HealthStatus       # can the process serve requests?
    overall: HealthStatus         # combined assessment
    critical_unhealthy: List[str]
    high_degraded: List[str]
    dependency_statuses: Dict[str, str]
    summary: str


class CompositeHealthAggregator:
    """
    Aggregates individual dependency health records into a composite status.

    Liveness:  always HEALTHY if the process is running.
    Readiness: UNHEALTHY if any CRITICAL dependency is UNHEALTHY;
               DEGRADED if any HIGH dependency is DEGRADED or UNHEALTHY;
               HEALTHY otherwise.
    """

    def aggregate(
        self,
        records: List[DependencyHealthRecord],
    ) -> CompositeHealthReport:
        critical_unhealthy = [
            r.dependency_name
            for r in records
            if r.criticality == DependencyCriticality.CRITICAL
            and r.status == HealthStatus.UNHEALTHY
        ]
        high_degraded = [
            r.dependency_name
            for r in records
            if r.criticality == DependencyCriticality.HIGH
            and r.status in (HealthStatus.DEGRADED, HealthStatus.UNHEALTHY)
        ]

        if critical_unhealthy:
            readiness = HealthStatus.UNHEALTHY
            summary = f"critical dependencies unhealthy: {critical_unhealthy}"
        elif high_degraded:
            readiness = HealthStatus.DEGRADED
            summary = f"high-priority dependencies degraded: {high_degraded}"
        else:
            readiness = HealthStatus.HEALTHY
            summary = "all critical dependencies healthy"

        dep_statuses = {r.dependency_name: r.status.value for r in records}

        return CompositeHealthReport(
            liveness=HealthStatus.HEALTHY,
            readiness=readiness,
            overall=readiness,
            critical_unhealthy=critical_unhealthy,
            high_degraded=high_degraded,
            dependency_statuses=dep_statuses,
            summary=summary,
        )
```

## Solution 4: Health Endpoint Handler

```python
import time
from typing import Dict


class HealthEndpointHandler:
    """
    Produces HTTP-ready health responses for liveness and readiness probes.
    Returns the appropriate status code (200/503) and structured JSON body.
    Suitable for use with Kubernetes, AWS ALB, or any load balancer.
    """

    def __init__(
        self,
        prober: DependencyHealthProber,
        aggregator: CompositeHealthAggregator,
        agent_version: str = "unknown",
    ):
        self._prober = prober
        self._aggregator = aggregator
        self._version = agent_version
        self._started_at = time.time()

    def liveness(self) -> dict:
        """Always 200 if process is running."""
        return {
            "status": "alive",
            "version": self._version,
            "uptime_seconds": round(time.time() - self._started_at, 1),
        }, 200

    def readiness(self) -> tuple:
        """200 if ready; 503 if not ready to serve traffic."""
        records = self._prober.records()
        report = self._aggregator.aggregate(records)

        body = {
            "status": report.readiness.value,
            "summary": report.summary,
            "version": self._version,
            "dependencies": {
                name: {
                    "status": status,
                    "latency_ms": next(
                        (r.latency_ms for r in records if r.dependency_name == name), None
                    ),
                }
                for name, status in report.dependency_statuses.items()
            },
            "checked_at": time.time(),
        }

        http_status = (
            200 if report.readiness == HealthStatus.HEALTHY
            else 207 if report.readiness == HealthStatus.DEGRADED
            else 503
        )
        return body, http_status

    def deep_health(self) -> tuple:
        """Full health report including per-dependency details and errors."""
        records = self._prober.records()
        report = self._aggregator.aggregate(records)
        body = {
            "status": report.overall.value,
            "liveness": report.liveness.value,
            "readiness": report.readiness.value,
            "summary": report.summary,
            "dependencies": [
                {
                    "name": r.dependency_name,
                    "criticality": r.criticality.value,
                    "status": r.status.value,
                    "consecutive_failures": r.consecutive_failures,
                    "latency_ms": r.latency_ms,
                    "last_error": r.last_error,
                    "time_since_success_seconds": r.time_since_last_success_seconds(),
                }
                for r in records
            ],
        }
        http_status = 200 if report.readiness != HealthStatus.UNHEALTHY else 503
        return body, http_status
```

## Solution 5: Health Degradation Notifier

```python
import time
from typing import Callable, Dict, List


class HealthDegradationNotifier:
    """
    Fires callbacks when dependency health transitions between states.
    Prevents alert storms with a per-dependency cooldown window.
    """

    def __init__(
        self,
        prober: DependencyHealthProber,
        aggregator: CompositeHealthAggregator,
        cooldown_seconds: float = 300.0,
    ):
        self._prober = prober
        self._aggregator = aggregator
        self._cooldown = cooldown_seconds
        self._last_status: Dict[str, HealthStatus] = {}
        self._last_alert: Dict[str, float] = {}
        self._on_degraded: List[Callable] = []
        self._on_recovered: List[Callable] = []

    def on_degraded(self, fn: Callable) -> None:
        self._on_degraded.append(fn)

    def on_recovered(self, fn: Callable) -> None:
        self._on_recovered.append(fn)

    def check_transitions(self) -> List[dict]:
        records = self._prober.records()
        events = []

        for record in records:
            prev = self._last_status.get(record.dependency_name, HealthStatus.UNKNOWN)
            curr = record.status

            if curr == prev:
                continue

            now = time.time()
            last_alert = self._last_alert.get(record.dependency_name, 0)
            in_cooldown = (now - last_alert) < self._cooldown

            if curr in (HealthStatus.DEGRADED, HealthStatus.UNHEALTHY) and not in_cooldown:
                event = {"dependency": record.dependency_name, "event": "degraded",
                         "from": prev.value, "to": curr.value}
                events.append(event)
                self._last_alert[record.dependency_name] = now
                for fn in self._on_degraded:
                    try:
                        fn(record, event)
                    except Exception:
                        pass

            elif curr == HealthStatus.HEALTHY and prev != HealthStatus.UNKNOWN:
                event = {"dependency": record.dependency_name, "event": "recovered",
                         "from": prev.value, "to": curr.value}
                events.append(event)
                for fn in self._on_recovered:
                    try:
                        fn(record, event)
                    except Exception:
                        pass

            self._last_status[record.dependency_name] = curr

        return events
```

## Solution 6: Health Propagation Dashboard

```python
import time


class HealthPropagationDashboard:
    """Combines composite health, notifier events, and per-dependency details."""

    def __init__(
        self,
        prober: DependencyHealthProber,
        aggregator: CompositeHealthAggregator,
        notifier: HealthDegradationNotifier,
    ):
        self._prober = prober
        self._aggregator = aggregator
        self._notifier = notifier

    def render(self) -> dict:
        records = self._prober.records()
        report = self._aggregator.aggregate(records)
        events = self._notifier.check_transitions()

        return {
            "generated_at": time.time(),
            "overall_status": report.overall.value,
            "readiness": report.readiness.value,
            "summary": report.summary,
            "critical_unhealthy": report.critical_unhealthy,
            "recent_transitions": events,
            "dependencies": [
                {
                    "name": r.dependency_name,
                    "criticality": r.criticality.value,
                    "status": r.status.value,
                    "failures": r.consecutive_failures,
                    "latency_ms": r.latency_ms,
                    "last_error": r.last_error,
                }
                for r in sorted(records, key=lambda r: r.criticality.value)
            ],
        }
```

## Comparison

| Approach | Probe Execution | Composite Status | HTTP Endpoints | Transition Alerts |
|---|---|---|---|---|
| DependencyHealthProber | Yes (async, scheduled) | No | No | No |
| CompositeHealthAggregator | No | Yes (readiness/liveness) | No | No |
| HealthEndpointHandler | Via prober | Via aggregator | Yes (200/207/503) | No |
| HealthDegradationNotifier | Via prober | Via aggregator | No | Yes (with cooldown) |
| HealthPropagationDashboard | No | Via aggregator | No | Via notifier |

**Best for production**: Register all critical dependencies (LLM provider, vector DB, primary database) with `CRITICAL` criticality — their failure makes the readiness endpoint return 503, which causes the load balancer to route traffic away automatically. Register nice-to-have dependencies (cache, search enrichment) as `LOW` — their failure degrades quality but shouldn't kill traffic. Set probe interval to 30 seconds and timeout to 5 seconds. Wire `HealthDegradationNotifier.on_degraded()` to PagerDuty or Slack so humans know before the load balancer acts.
