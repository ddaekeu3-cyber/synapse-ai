---
title: "Agent Doesn't Implement Health Check Endpoint for Readiness and Liveness"
description: "Agents deployed behind load balancers or in Kubernetes clusters without health check endpoints receive traffic before initialization completes and continue receiving traffic after entering a degraded state. Implement separate readiness and liveness probes that report initialization status, dependency health, and saturation levels so orchestrators can gate traffic and restart unhealthy instances."
date: 2026-04-16
difficulty: intermediate
category: reliability
slug: agent-doesnt-implement-health-check-endpoint-for-readiness-and-liveness
tags: [health-check, readiness-probe, liveness-probe, kubernetes, load-balancer, startup-gate]
symptoms:
  - "Agent receives requests before model client and tool registry have finished initializing"
  - "Kubernetes restarts healthy pods because no liveness probe is configured"
  - "Load balancer routes traffic to an instance that ran out of memory and is in a degraded loop"
  - "No way to distinguish 'starting up' from 'ready to serve' from 'unhealthy'"
  - "Deployment rollouts send traffic to new pods before they have warmed their caches"
---

## Why This Happens

Container orchestrators and load balancers need explicit signals to distinguish three pod states: starting (not yet ready), ready (accept traffic), and unhealthy (restart needed). Without health endpoints, orchestrators assume the process is healthy the moment it starts and route traffic immediately. A liveness probe detects stuck or deadlocked processes — it should only fail if the agent is unrecoverable. A readiness probe detects transient unavailability — it should fail during startup and during overload, causing the orchestrator to hold traffic without restarting the pod.

## Solution 1: Health Status Model

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional
import time


class HealthStatus(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    STARTING = "starting"


@dataclass
class DependencyHealthResult:
    name: str
    status: HealthStatus
    latency_ms: Optional[float] = None
    error: Optional[str] = None
    checked_at: float = field(default_factory=time.time)


@dataclass
class AgentHealthReport:
    liveness: HealthStatus
    readiness: HealthStatus
    uptime_seconds: float
    dependencies: List[DependencyHealthResult] = field(default_factory=list)
    saturation: float = 0.0          # 0.0–1.0 fraction of capacity in use
    active_requests: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def http_liveness_status(self) -> int:
        return 200 if self.liveness == HealthStatus.HEALTHY else 503

    @property
    def http_readiness_status(self) -> int:
        return 200 if self.readiness in (HealthStatus.HEALTHY, HealthStatus.DEGRADED) else 503
```

## Solution 2: Dependency Health Checker

```python
import asyncio
import time
from typing import Any, Callable, Dict, List


class DependencyHealthChecker:
    """
    Runs lightweight health checks against agent dependencies
    (LLM API, vector store, database) with per-check timeouts.
    """

    def __init__(self):
        self._checks: Dict[str, tuple] = {}  # name -> (check_fn, timeout_ms)

    def register(
        self,
        name: str,
        check_fn: Callable,
        timeout_ms: float = 2000.0,
    ) -> None:
        self._checks[name] = (check_fn, timeout_ms)

    async def run_all(self) -> List[DependencyHealthResult]:
        results = []
        for name, (fn, timeout_ms) in self._checks.items():
            start = time.time()
            try:
                await asyncio.wait_for(fn(), timeout=timeout_ms / 1000.0)
                results.append(DependencyHealthResult(
                    name=name,
                    status=HealthStatus.HEALTHY,
                    latency_ms=round((time.time() - start) * 1000, 2),
                ))
            except asyncio.TimeoutError:
                results.append(DependencyHealthResult(
                    name=name,
                    status=HealthStatus.UNHEALTHY,
                    latency_ms=timeout_ms,
                    error=f"timeout after {timeout_ms}ms",
                ))
            except Exception as exc:
                results.append(DependencyHealthResult(
                    name=name,
                    status=HealthStatus.UNHEALTHY,
                    latency_ms=round((time.time() - start) * 1000, 2),
                    error=str(exc),
                ))
        return results

    async def run_critical_only(self, critical_names: List[str]) -> List[DependencyHealthResult]:
        subset = {k: v for k, v in self._checks.items() if k in critical_names}
        original = self._checks
        self._checks = subset
        results = await self.run_all()
        self._checks = original
        return results
```

## Solution 3: Readiness Gate

```python
import time
from typing import Optional


class ReadinessGate:
    """
    Tracks whether the agent has completed initialization.
    Readiness is only granted after all required init phases pass.
    Supports manual override for maintenance windows.
    """

    def __init__(self):
        self._ready = False
        self._ready_at: Optional[float] = None
        self._not_ready_reason: str = "not yet initialized"
        self._maintenance = False
        self._start_time = time.time()

    def mark_ready(self) -> None:
        self._ready = True
        self._ready_at = time.time()
        self._not_ready_reason = ""

    def mark_not_ready(self, reason: str) -> None:
        self._ready = False
        self._not_ready_reason = reason

    def enter_maintenance(self, reason: str = "maintenance") -> None:
        self._maintenance = True
        self._not_ready_reason = reason

    def exit_maintenance(self) -> None:
        self._maintenance = False
        if self._ready:
            self._not_ready_reason = ""

    def is_ready(self) -> bool:
        return self._ready and not self._maintenance

    def status(self) -> dict:
        return {
            "ready": self.is_ready(),
            "reason": self._not_ready_reason if not self.is_ready() else "ok",
            "ready_at": self._ready_at,
            "uptime_seconds": round(time.time() - self._start_time, 1),
            "maintenance": self._maintenance,
        }
```

## Solution 4: Liveness Monitor

```python
import time
from threading import Lock
from typing import Optional


class LivenessMonitor:
    """
    Detects liveness failures: heartbeat staleness, OOM proximity,
    and deadlock signals. Reports unhealthy only for unrecoverable states.
    """

    def __init__(
        self,
        heartbeat_timeout_seconds: float = 30.0,
        max_memory_fraction: float = 0.90,
    ):
        self._timeout = heartbeat_timeout_seconds
        self._max_memory = max_memory_fraction
        self._last_heartbeat = time.time()
        self._lock = Lock()
        self._failure_reason: Optional[str] = None

    def heartbeat(self) -> None:
        with self._lock:
            self._last_heartbeat = time.time()
            self._failure_reason = None

    def report_failure(self, reason: str) -> None:
        with self._lock:
            self._failure_reason = reason

    def is_alive(self) -> tuple:
        with self._lock:
            if self._failure_reason:
                return False, self._failure_reason
            age = time.time() - self._last_heartbeat
            if age > self._timeout:
                return False, f"heartbeat stale: {age:.0f}s"
            try:
                import resource
                usage = resource.getrusage(resource.RUSAGE_SELF)
                # Basic check — real memory fraction requires platform-specific code
            except Exception:
                pass
            return True, "ok"

    def status(self) -> dict:
        alive, reason = self.is_alive()
        return {
            "alive": alive,
            "reason": reason,
            "last_heartbeat_age_s": round(time.time() - self._last_heartbeat, 1),
        }
```

## Solution 5: Health Check Handler

```python
import time
from typing import Optional


class AgentHealthCheckHandler:
    """
    Assembles liveness and readiness reports from all health subsystems.
    Designed to be called from an HTTP handler (FastAPI, aiohttp, etc.).
    """

    def __init__(
        self,
        readiness_gate: ReadinessGate,
        liveness_monitor: LivenessMonitor,
        dependency_checker: DependencyHealthChecker,
        start_time: Optional[float] = None,
    ):
        self._readiness = readiness_gate
        self._liveness = liveness_monitor
        self._deps = dependency_checker
        self._start = start_time or time.time()

    async def liveness(self) -> AgentHealthReport:
        alive, reason = self._liveness.is_alive()
        return AgentHealthReport(
            liveness=HealthStatus.HEALTHY if alive else HealthStatus.UNHEALTHY,
            readiness=HealthStatus.HEALTHY,  # not checked in liveness
            uptime_seconds=round(time.time() - self._start, 1),
            metadata={"reason": reason},
        )

    async def readiness(self, check_deps: bool = True) -> AgentHealthReport:
        dep_results = await self._deps.run_all() if check_deps else []
        failed_deps = [d for d in dep_results if d.status == HealthStatus.UNHEALTHY]

        readiness_ok = self._readiness.is_ready() and not failed_deps
        readiness_status = HealthStatus.HEALTHY if readiness_ok else (
            HealthStatus.STARTING if not self._readiness.is_ready()
            else HealthStatus.UNHEALTHY
        )

        alive, _ = self._liveness.is_alive()

        return AgentHealthReport(
            liveness=HealthStatus.HEALTHY if alive else HealthStatus.UNHEALTHY,
            readiness=readiness_status,
            uptime_seconds=round(time.time() - self._start, 1),
            dependencies=dep_results,
            metadata={
                "readiness_gate": self._readiness.status(),
                "failed_dependencies": [d.name for d in failed_deps],
            },
        )
```

## Solution 6: Health Check Dashboard

```python
import time


class HealthCheckDashboard:
    """
    Renders a full health snapshot combining liveness, readiness,
    dependency results, and saturation into a single dict.
    """

    def __init__(self, handler: AgentHealthCheckHandler):
        self._handler = handler

    async def render(self) -> dict:
        liveness_report = await self._handler.liveness()
        readiness_report = await self._handler.readiness(check_deps=True)

        dep_summary = {
            d.name: {
                "status": d.status.value,
                "latency_ms": d.latency_ms,
                "error": d.error,
            }
            for d in readiness_report.dependencies
        }

        return {
            "generated_at": time.time(),
            "liveness": {
                "status": liveness_report.liveness.value,
                "http_status": liveness_report.http_liveness_status,
            },
            "readiness": {
                "status": readiness_report.readiness.value,
                "http_status": readiness_report.http_readiness_status,
            },
            "uptime_seconds": readiness_report.uptime_seconds,
            "dependencies": dep_summary,
            "healthy_deps": sum(1 for d in readiness_report.dependencies
                                if d.status == HealthStatus.HEALTHY),
            "failed_deps": readiness_report.metadata.get("failed_dependencies", []),
        }
```

## Comparison

| Approach | Liveness | Readiness | Dependency Checks | Startup Gate | Dashboard |
|---|---|---|---|---|---|
| ReadinessGate | No | Yes (init gate) | No | Yes | No |
| LivenessMonitor | Yes (heartbeat) | No | No | No | No |
| DependencyHealthChecker | No | Via results | Yes | No | No |
| AgentHealthCheckHandler | Yes | Yes | Via checker | Via gate | No |
| HealthCheckDashboard | No | No | No | No | Yes |

**Best for production**: Separate liveness and readiness strictly — liveness failures trigger pod restarts (expensive), readiness failures remove the pod from the load balancer rotation (cheap). Call `LivenessMonitor.heartbeat()` from your main event loop at least every 10 seconds; if the loop deadlocks, the heartbeat stops and the orchestrator restarts the pod. Register only hard dependencies in `DependencyHealthChecker` for the readiness probe — a soft enrichment service being down should not mark the pod unready. Set a Kubernetes `initialDelaySeconds` equal to your observed P95 cold start time so the readiness probe doesn't fail during normal initialization.
