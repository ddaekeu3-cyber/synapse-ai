---
title: "Agent Doesn't Implement Health Check Endpoint for Liveness and Readiness"
description: "Agents deployed behind load balancers or in Kubernetes without dedicated liveness and readiness endpoints receive traffic before they are ready and continue receiving traffic after they become unhealthy. Implement separate liveness and readiness health check endpoints that reflect actual dependency reachability, model client initialization state, and circuit breaker status — not just HTTP 200 from the process."
date: 2026-04-16
difficulty: intermediate
category: reliability
slug: agent-doesnt-implement-health-check-endpoint-for-liveness-and-readiness
tags: [health-check, liveness, readiness, kubernetes, load-balancer, dependency-health]
symptoms:
  - "Agent receives requests before model client is initialized — first requests fail"
  - "Kubernetes restarts healthy pods because liveness probe hits wrong endpoint"
  - "Load balancer keeps routing to a pod whose LLM API key is expired"
  - "No way to distinguish 'process is alive' from 'agent is ready to serve traffic'"
  - "Health check always returns 200 regardless of downstream dependency state"
---

## Why This Happens

A process that responds to HTTP is not necessarily ready to serve agent requests. The model client may still be initializing, the vector database connection pool may not be established, or a circuit breaker may be open on the primary LLM API. Without distinct liveness (is the process alive?) and readiness (can it serve requests right now?) probes, orchestrators cannot make correct routing decisions. Liveness checks should be cheap and never fail unless the process itself is broken. Readiness checks should reflect real dependency state and can temporarily return unhealthy to pull the pod from rotation without triggering a restart.

## Solution 1: Health Status Types

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional
import time


class HealthStatus(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"     # serving but with reduced capability
    UNHEALTHY = "unhealthy"   # not ready to serve


@dataclass
class DependencyHealthResult:
    name: str
    status: HealthStatus
    latency_ms: float
    detail: str = ""
    checked_at: float = field(default_factory=time.time)

    def is_healthy(self) -> bool:
        return self.status == HealthStatus.HEALTHY


@dataclass
class HealthCheckReport:
    probe_type: str            # "liveness" or "readiness"
    overall_status: HealthStatus
    dependencies: List[DependencyHealthResult]
    process_uptime_s: float
    checked_at: float = field(default_factory=time.time)

    def http_status_code(self) -> int:
        if self.overall_status == HealthStatus.HEALTHY:
            return 200
        if self.overall_status == HealthStatus.DEGRADED:
            return 200    # degraded still serves; readiness will handle routing
        return 503
```

## Solution 2: Dependency Health Checker

```python
import asyncio
import time
from typing import Callable, Dict, List, Optional


class DependencyHealthChecker:
    """
    Runs async health checks against named dependencies with per-check timeouts.
    Each check is a callable returning (healthy: bool, detail: str).
    """

    def __init__(self, timeout_s: float = 3.0):
        self._timeout = timeout_s
        self._checks: Dict[str, Callable] = {}

    def register(self, name: str, check_fn: Callable) -> None:
        """check_fn: async () -> (bool, str)"""
        self._checks[name] = check_fn

    async def run_all(self) -> List[DependencyHealthResult]:
        results = []
        tasks = {
            name: asyncio.create_task(self._run_one(name, fn))
            for name, fn in self._checks.items()
        }
        for name, task in tasks.items():
            result = await task
            results.append(result)
        return results

    async def _run_one(self, name: str, fn: Callable) -> DependencyHealthResult:
        start = time.time()
        try:
            ok, detail = await asyncio.wait_for(fn(), timeout=self._timeout)
            latency_ms = round((time.time() - start) * 1000, 2)
            return DependencyHealthResult(
                name=name,
                status=HealthStatus.HEALTHY if ok else HealthStatus.UNHEALTHY,
                latency_ms=latency_ms,
                detail=detail,
            )
        except asyncio.TimeoutError:
            latency_ms = round((time.time() - start) * 1000, 2)
            return DependencyHealthResult(
                name=name,
                status=HealthStatus.UNHEALTHY,
                latency_ms=latency_ms,
                detail=f"timeout after {self._timeout}s",
            )
        except Exception as exc:
            latency_ms = round((time.time() - start) * 1000, 2)
            return DependencyHealthResult(
                name=name,
                status=HealthStatus.UNHEALTHY,
                latency_ms=latency_ms,
                detail=str(exc)[:200],
            )
```

## Solution 3: Liveness Probe Handler

```python
import time


class LivenessProbeHandler:
    """
    Liveness: is the process fundamentally alive and not deadlocked?
    Checks only internal process state — never calls external services.
    Should almost never fail; failure triggers a pod restart.
    """

    def __init__(self, process_start_time: float):
        self._start = process_start_time
        self._alive = True
        self._last_heartbeat = time.time()

    def heartbeat(self) -> None:
        """Call from the main event loop regularly to prove it is not deadlocked."""
        self._last_heartbeat = time.time()

    def check(self) -> HealthCheckReport:
        now = time.time()
        loop_lag_s = now - self._last_heartbeat
        loop_healthy = loop_lag_s < 30.0   # 30s without heartbeat = deadlocked

        status = HealthStatus.HEALTHY if (self._alive and loop_healthy) else HealthStatus.UNHEALTHY
        detail = "ok" if loop_healthy else f"event loop silent for {loop_lag_s:.1f}s"

        return HealthCheckReport(
            probe_type="liveness",
            overall_status=status,
            dependencies=[
                DependencyHealthResult(
                    name="event_loop",
                    status=status,
                    latency_ms=0.0,
                    detail=detail,
                )
            ],
            process_uptime_s=round(now - self._start, 1),
        )

    def mark_dead(self) -> None:
        self._alive = False
```

## Solution 4: Readiness Probe Handler

```python
import asyncio
import time
from typing import Set


class ReadinessProbeHandler:
    """
    Readiness: can the agent serve requests right now?
    Checks dependency health, initialization state, and circuit breaker status.
    Returning unhealthy removes the pod from LB rotation without restarting it.
    """

    def __init__(
        self,
        checker: DependencyHealthChecker,
        process_start_time: float,
        required_dependencies: Set[str],   # these must be healthy to be ready
        startup_grace_period_s: float = 10.0,
    ):
        self._checker = checker
        self._start = process_start_time
        self._required = required_dependencies
        self._grace = startup_grace_period_s
        self._initialized = False

    def mark_initialized(self) -> None:
        self._initialized = True

    async def check(self) -> HealthCheckReport:
        now = time.time()
        uptime = now - self._start

        # During grace period, return healthy to prevent premature kill
        if uptime < self._grace:
            return HealthCheckReport(
                probe_type="readiness",
                overall_status=HealthStatus.DEGRADED,
                dependencies=[],
                process_uptime_s=round(uptime, 1),
            )

        if not self._initialized:
            return HealthCheckReport(
                probe_type="readiness",
                overall_status=HealthStatus.UNHEALTHY,
                dependencies=[DependencyHealthResult(
                    name="initialization",
                    status=HealthStatus.UNHEALTHY,
                    latency_ms=0.0,
                    detail="agent not yet initialized",
                )],
                process_uptime_s=round(uptime, 1),
            )

        dep_results = await self._checker.run_all()
        failed_required = [
            r for r in dep_results
            if r.name in self._required and not r.is_healthy()
        ]

        if failed_required:
            overall = HealthStatus.UNHEALTHY
        elif any(not r.is_healthy() for r in dep_results):
            overall = HealthStatus.DEGRADED
        else:
            overall = HealthStatus.HEALTHY

        return HealthCheckReport(
            probe_type="readiness",
            overall_status=overall,
            dependencies=dep_results,
            process_uptime_s=round(uptime, 1),
        )
```

## Solution 5: Health Check Cache

```python
import asyncio
import time
from typing import Callable, Optional


class HealthCheckCache:
    """
    Caches readiness probe results to avoid hammering dependencies on every
    Kubernetes probe interval (default 10s). Cache TTL should be shorter
    than the probe interval to ensure freshness.
    """

    def __init__(self, ttl_s: float = 5.0):
        self._ttl = ttl_s
        self._cached: Optional[HealthCheckReport] = None
        self._cached_at: float = 0.0
        self._lock = asyncio.Lock()

    async def get_or_refresh(
        self, refresh_fn: Callable
    ) -> HealthCheckReport:
        async with self._lock:
            now = time.time()
            if self._cached is not None and (now - self._cached_at) < self._ttl:
                return self._cached
            result = await refresh_fn()
            self._cached = result
            self._cached_at = now
            return result

    def invalidate(self) -> None:
        self._cached = None
        self._cached_at = 0.0
```

## Solution 6: Health Check HTTP Router

```python
import json
import time
from dataclasses import asdict
from typing import Any


class HealthCheckHTTPRouter:
    """
    Produces HTTP-compatible response dicts for liveness and readiness probes.
    Integrate with any ASGI/WSGI framework by returning (status_code, body).
    """

    def __init__(
        self,
        liveness: LivenessProbeHandler,
        readiness: ReadinessProbeHandler,
        cache: HealthCheckCache,
    ):
        self._liveness = liveness
        self._readiness = readiness
        self._cache = cache

    def handle_liveness(self) -> tuple:
        report = self._liveness.check()
        body = {
            "status": report.overall_status.value,
            "uptime_s": report.process_uptime_s,
            "checks": [
                {"name": d.name, "status": d.status.value, "detail": d.detail}
                for d in report.dependencies
            ],
        }
        return report.http_status_code(), body

    async def handle_readiness(self) -> tuple:
        report = await self._cache.get_or_refresh(self._readiness.check)
        body = {
            "status": report.overall_status.value,
            "uptime_s": report.process_uptime_s,
            "checks": [
                {
                    "name": d.name,
                    "status": d.status.value,
                    "latency_ms": d.latency_ms,
                    "detail": d.detail,
                }
                for d in report.dependencies
            ],
        }
        return report.http_status_code(), body
```

## Comparison

| Approach | Process State | Dependency Checks | Caching | K8s Liveness | K8s Readiness |
|---|---|---|---|---|---|
| LivenessProbeHandler | Yes (heartbeat) | No | No | Yes | No |
| DependencyHealthChecker | No | Yes (async + timeout) | No | No | Via readiness |
| ReadinessProbeHandler | Via checker | Yes | No | No | Yes |
| HealthCheckCache | No | No | Yes (TTL) | No | Yes |
| HealthCheckHTTPRouter | Via liveness | Via readiness | Via cache | Yes | Yes |

**Best for production**: Keep liveness strictly cheap — it must never call an external API. A failed liveness check triggers a pod restart, which during an incident will cascade into a restart storm. Readiness failures are safe: they pull the pod from rotation without killing it. Set `required_dependencies` to only the model API client — vector stores and caches can be optional. Set `HealthCheckCache` TTL to half the Kubernetes `periodSeconds` so the cache is always fresh but you never hit dependencies twice per probe cycle. Use the `DependencyHealthResult.latency_ms` field to detect dependency slowdowns before they cause user-visible failures.
