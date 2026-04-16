---
title: "Agent Doesn't Implement Health Check Endpoint with Dependency Status"
description: "Agents deployed behind load balancers or orchestrators that expose only a trivial /health returning 200 OK mask dependency failures: the agent process is alive but its LLM provider, database, and vector store are all unreachable. Implement a structured health check endpoint that probes each dependency, reports individual status, and returns a degraded or unhealthy aggregate so load balancers and on-call systems can act on real readiness."
date: 2026-04-16
difficulty: intermediate
category: reliability
slug: agent-doesnt-implement-health-check-endpoint-with-dependency-status
tags: [health-check, dependency-status, readiness-probe, liveness-probe, load-balancer, kubernetes]
symptoms:
  - "Health check returns 200 OK while LLM provider is unreachable"
  - "Load balancer sends traffic to instances that cannot process requests"
  - "No way to distinguish a live-but-degraded agent from a fully healthy one"
  - "Kubernetes readiness probe passes on startup before connection pools are warmed"
  - "On-call receives user-facing errors before health check fires an alert"
---

## Why This Happens

A process-level health check — does the HTTP server respond? — is not the same as a readiness check — can this instance handle a request right now? Agents depend on external services (LLM API, database, vector store, cache) that can fail independently while the agent process continues running. Without probing these dependencies and reflecting their status in the health response, orchestrators keep sending traffic to instances that will immediately fail every request. Structured health checks probe each dependency on a schedule, cache the results, and expose a JSON response that distinguishes healthy, degraded, and unhealthy states.

## Solution 1: Dependency Health Probe

```python
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional


class HealthStatus(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"


@dataclass
class DependencyProbeResult:
    name: str
    status: HealthStatus
    latency_ms: Optional[float] = None
    detail: str = ""
    checked_at: float = field(default_factory=time.time)
    critical: bool = True       # critical=True means unhealthy → aggregate unhealthy


@dataclass
class DependencyProbe:
    name: str
    probe_fn: Callable[[], Any]   # raises on failure, returns on success
    timeout_seconds: float = 5.0
    critical: bool = True
    description: str = ""
```

## Solution 2: Dependency Health Checker

```python
import asyncio
import time
from typing import List


class DependencyHealthChecker:
    """
    Runs a list of DependencyProbe callables with timeouts.
    Returns one DependencyProbeResult per probe.
    """

    async def check_one(self, probe: DependencyProbe) -> DependencyProbeResult:
        start = time.time()
        try:
            await asyncio.wait_for(
                asyncio.coroutine(probe.probe_fn)()
                if not asyncio.iscoroutinefunction(probe.probe_fn)
                else probe.probe_fn(),
                timeout=probe.timeout_seconds,
            )
            return DependencyProbeResult(
                name=probe.name,
                status=HealthStatus.HEALTHY,
                latency_ms=round((time.time() - start) * 1000, 2),
                critical=probe.critical,
            )
        except asyncio.TimeoutError:
            return DependencyProbeResult(
                name=probe.name,
                status=HealthStatus.UNHEALTHY,
                latency_ms=round(probe.timeout_seconds * 1000, 2),
                detail=f"probe timed out after {probe.timeout_seconds}s",
                critical=probe.critical,
            )
        except Exception as exc:
            return DependencyProbeResult(
                name=probe.name,
                status=HealthStatus.UNHEALTHY,
                latency_ms=round((time.time() - start) * 1000, 2),
                detail=str(exc),
                critical=probe.critical,
            )

    async def check_all(self, probes: List[DependencyProbe]) -> List[DependencyProbeResult]:
        return list(await asyncio.gather(*[self.check_one(p) for p in probes]))
```

## Solution 3: Health Check Cache

```python
import asyncio
import time
from threading import Lock
from typing import List, Optional


class HealthCheckCache:
    """
    Caches health check results and refreshes them on a background schedule.
    Serves stale results when a refresh is in progress to keep latency low.
    """

    def __init__(
        self,
        checker: DependencyHealthChecker,
        probes: List[DependencyProbe],
        refresh_interval_seconds: float = 15.0,
        stale_threshold_seconds: float = 60.0,
    ):
        self._checker = checker
        self._probes = probes
        self._interval = refresh_interval_seconds
        self._stale_threshold = stale_threshold_seconds
        self._results: Optional[List[DependencyProbeResult]] = None
        self._last_refresh: float = 0.0
        self._lock = Lock()

    async def refresh(self) -> List[DependencyProbeResult]:
        results = await self._checker.check_all(self._probes)
        with self._lock:
            self._results = results
            self._last_refresh = time.time()
        return results

    def get(self) -> Optional[List[DependencyProbeResult]]:
        with self._lock:
            if self._results is None:
                return None
            age = time.time() - self._last_refresh
            if age > self._stale_threshold:
                return None   # too stale to serve
            return list(self._results)

    def is_stale(self) -> bool:
        with self._lock:
            return time.time() - self._last_refresh > self._interval
```

## Solution 4: Aggregate Health Evaluator

```python
from typing import List


class AggregateHealthEvaluator:
    """
    Computes an aggregate health status from a list of probe results.
    Any critical dependency unhealthy → aggregate UNHEALTHY.
    Any non-critical dependency unhealthy → aggregate DEGRADED.
    All healthy → HEALTHY.
    """

    def evaluate(self, results: List[DependencyProbeResult]) -> dict:
        if not results:
            return {"status": HealthStatus.UNKNOWN.value, "dependencies": []}

        aggregate = HealthStatus.HEALTHY
        for r in results:
            if r.status == HealthStatus.UNHEALTHY:
                if r.critical:
                    aggregate = HealthStatus.UNHEALTHY
                elif aggregate == HealthStatus.HEALTHY:
                    aggregate = HealthStatus.DEGRADED
            elif r.status == HealthStatus.DEGRADED and aggregate == HealthStatus.HEALTHY:
                aggregate = HealthStatus.DEGRADED

        return {
            "status": aggregate.value,
            "dependencies": [
                {
                    "name": r.name,
                    "status": r.status.value,
                    "latency_ms": r.latency_ms,
                    "detail": r.detail,
                    "critical": r.critical,
                    "checked_at": r.checked_at,
                }
                for r in results
            ],
        }

    def http_status_code(self, aggregate_status: str) -> int:
        return {
            HealthStatus.HEALTHY.value: 200,
            HealthStatus.DEGRADED.value: 200,   # degraded = still serving
            HealthStatus.UNHEALTHY.value: 503,
            HealthStatus.UNKNOWN.value: 503,
        }.get(aggregate_status, 503)
```

## Solution 5: Health Check Handler

```python
import time
from typing import Optional


class HealthCheckHandler:
    """
    Handles /health and /ready requests.
    /health → liveness (is the process alive?)
    /ready  → readiness (can it handle requests right now?)
    """

    def __init__(
        self,
        cache: HealthCheckCache,
        evaluator: AggregateHealthEvaluator,
        instance_id: str = "",
    ):
        self._cache = cache
        self._evaluator = evaluator
        self._instance_id = instance_id
        self._start_time = time.time()

    async def liveness(self) -> dict:
        """Always returns 200 if the process is running."""
        return {
            "status": "alive",
            "instance_id": self._instance_id,
            "uptime_seconds": round(time.time() - self._start_time, 1),
        }

    async def readiness(self) -> tuple:
        """Returns (response_dict, http_status_code)."""
        results = self._cache.get()
        if results is None:
            results = await self._cache.refresh()

        report = self._evaluator.evaluate(results)
        report["instance_id"] = self._instance_id
        report["checked_at"] = time.time()

        if self._cache.is_stale():
            import asyncio
            asyncio.create_task(self._cache.refresh())

        status_code = self._evaluator.http_status_code(report["status"])
        return report, status_code
```

## Solution 6: Health Check History Tracker

```python
import time
from threading import Lock
from typing import List


class HealthCheckHistoryTracker:
    """
    Records health check outcomes over time.
    Surfaces availability percentage and recent outage windows.
    """

    def __init__(self, max_records: int = 1000):
        self._records: List[dict] = []
        self._lock = Lock()
        self._max = max_records

    def record(self, status: str, dependency_results: List[DependencyProbeResult]) -> None:
        with self._lock:
            self._records.append({
                "ts": time.time(),
                "status": status,
                "unhealthy_deps": [
                    r.name for r in dependency_results
                    if r.status == HealthStatus.UNHEALTHY
                ],
            })
            if len(self._records) > self._max:
                self._records.pop(0)

    def availability(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        with self._lock:
            recent = [r for r in self._records if r["ts"] >= cutoff]
        if not recent:
            return {"window_seconds": window_seconds, "checks": 0}
        healthy_count = sum(1 for r in recent if r["status"] == "healthy")
        return {
            "window_seconds": window_seconds,
            "checks": len(recent),
            "healthy_pct": round(healthy_count / len(recent) * 100, 2),
            "unhealthy_checks": len(recent) - healthy_count,
        }
```

## Comparison

| Approach | Per-Dependency Probe | Timeout | Cached Results | Aggregate Status | HTTP Code |
|---|---|---|---|---|---|
| DependencyHealthChecker | Yes | Yes | No | No | No |
| HealthCheckCache | Via checker | No | Yes (TTL) | No | No |
| AggregateHealthEvaluator | No | No | No | Yes (critical/non-critical) | Yes |
| HealthCheckHandler | Via cache | No | Via cache | Via evaluator | Via evaluator |
| HealthCheckHistoryTracker | No | No | No | No | No (retrospective) |

**Best for production**: Mark LLM provider and primary database as `critical=True`; mark cache, vector store, and enrichment APIs as `critical=False`. Set `refresh_interval_seconds=15` for Kubernetes readiness probes — this matches typical probe intervals and avoids thundering herd on the dependency services. Return 503 only for `UNHEALTHY` (critical dependency down), not for `DEGRADED` — a degraded instance should still receive traffic rather than shifting load to other instances. Record every check result in `HealthCheckHistoryTracker` and alert when `healthy_pct` drops below 95% over a 1-hour window.
