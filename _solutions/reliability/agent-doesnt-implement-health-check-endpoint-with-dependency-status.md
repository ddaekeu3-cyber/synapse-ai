---
title: "Agent Doesn't Implement Health Check Endpoint with Dependency Status"
description: "Agents that expose only a shallow liveness endpoint — one that returns 200 regardless of whether downstream dependencies are reachable — mislead load balancers and orchestrators into routing traffic to instances that cannot serve requests. Implement a health check endpoint that tests each critical dependency and reports its status, distinguishing between live-but-degraded and genuinely healthy."
date: 2026-04-16
difficulty: intermediate
category: reliability
slug: agent-doesnt-implement-health-check-endpoint-with-dependency-status
tags: [health-check, dependency-status, liveness, readiness, load-balancer, kubernetes-probes]
symptoms:
  - "Load balancer routes requests to agent instances that cannot reach the LLM API"
  - "Health check returns 200 even when the database connection pool is exhausted"
  - "Kubernetes readiness probe passes but agent cannot serve tool calls due to broken dependencies"
  - "No distinction between liveness (process alive) and readiness (dependencies reachable)"
  - "Incident response is delayed because operators cannot query which dependencies are failing"
---

## Why This Happens

A liveness probe answers "is the process running?" A readiness probe answers "can this instance serve traffic?" Most agents implement only liveness — a simple HTTP handler that returns 200. When a dependency fails (LLM API unreachable, database down, Redis timeout), the process is still alive but cannot serve requests. Load balancers continue routing to it. Users see failures. A readiness-aware health check actively tests each dependency on each probe and returns a degraded or failing status when any critical dependency is unhealthy, causing the load balancer to remove the instance from rotation.

## Solution 1: Dependency Health Check

```python
import asyncio
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Optional


class DependencyStatus(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"


@dataclass
class DependencyCheckResult:
    name: str
    status: DependencyStatus
    latency_ms: Optional[float] = None
    error: Optional[str] = None
    detail: str = ""
    checked_at: float = 0.0

    def __post_init__(self):
        if not self.checked_at:
            self.checked_at = time.time()
```

## Solution 2: Dependency Checker Registry

```python
import asyncio
import time
from typing import Callable, Dict, List, Optional


class DependencyChecker:
    """
    Wraps a probe function with a name, criticality flag, and timeout.
    Critical dependencies failing makes the agent unready; non-critical
    failures produce DEGRADED status.
    """

    def __init__(
        self,
        name: str,
        probe_fn: Callable[[], Any],
        critical: bool = True,
        timeout_seconds: float = 3.0,
        degraded_latency_ms: float = 1000.0,
    ):
        self.name = name
        self.critical = critical
        self._probe = probe_fn
        self._timeout = timeout_seconds
        self._degraded_latency = degraded_latency_ms

    async def check(self) -> DependencyCheckResult:
        start = time.time()
        try:
            await asyncio.wait_for(
                asyncio.coroutine(self._probe)() if not asyncio.iscoroutinefunction(self._probe)
                else self._probe(),
                timeout=self._timeout,
            )
            latency_ms = round((time.time() - start) * 1000, 2)
            status = (
                DependencyStatus.DEGRADED
                if latency_ms > self._degraded_latency
                else DependencyStatus.HEALTHY
            )
            return DependencyCheckResult(
                name=self.name,
                status=status,
                latency_ms=latency_ms,
                detail=f"probe succeeded in {latency_ms}ms",
            )
        except asyncio.TimeoutError:
            latency_ms = round((time.time() - start) * 1000, 2)
            return DependencyCheckResult(
                name=self.name,
                status=DependencyStatus.UNHEALTHY,
                latency_ms=latency_ms,
                error="timeout",
                detail=f"probe timed out after {self._timeout}s",
            )
        except Exception as exc:
            latency_ms = round((time.time() - start) * 1000, 2)
            return DependencyCheckResult(
                name=self.name,
                status=DependencyStatus.UNHEALTHY,
                latency_ms=latency_ms,
                error=type(exc).__name__,
                detail=str(exc)[:200],
            )


class DependencyCheckerRegistry:
    def __init__(self):
        self._checkers: Dict[str, DependencyChecker] = {}

    def register(self, checker: DependencyChecker) -> None:
        self._checkers[checker.name] = checker

    def checkers(self) -> List[DependencyChecker]:
        return list(self._checkers.values())
```

## Solution 3: Health Check Aggregator

```python
import asyncio
import time
from typing import Dict, List


class HealthCheckAggregator:
    """
    Runs all registered dependency checks in parallel and aggregates
    results into an overall health status: HEALTHY, DEGRADED, or UNHEALTHY.
    """

    def __init__(self, registry: DependencyCheckerRegistry):
        self._registry = registry

    async def run(self) -> dict:
        checkers = self._registry.checkers()
        if not checkers:
            return {
                "status": DependencyStatus.HEALTHY.value,
                "dependencies": {},
                "checked_at": time.time(),
            }

        results: List[DependencyCheckResult] = await asyncio.gather(
            *[c.check() for c in checkers]
        )

        dep_map = {r.name: r for r in results}
        overall = self._aggregate_status(checkers, dep_map)

        return {
            "status": overall.value,
            "dependencies": {
                name: {
                    "status": r.status.value,
                    "latency_ms": r.latency_ms,
                    "error": r.error,
                    "detail": r.detail,
                    "critical": next(
                        (c.critical for c in checkers if c.name == name), True
                    ),
                }
                for name, r in dep_map.items()
            },
            "checked_at": time.time(),
        }

    @staticmethod
    def _aggregate_status(
        checkers: List[DependencyChecker],
        results: Dict[str, DependencyCheckResult],
    ) -> DependencyStatus:
        for checker in checkers:
            result = results.get(checker.name)
            if result and result.status == DependencyStatus.UNHEALTHY and checker.critical:
                return DependencyStatus.UNHEALTHY
        if any(r.status == DependencyStatus.DEGRADED for r in results.values()):
            return DependencyStatus.DEGRADED
        if any(r.status == DependencyStatus.UNHEALTHY for r in results.values()):
            return DependencyStatus.DEGRADED   # non-critical unhealthy → degraded overall
        return DependencyStatus.HEALTHY
```

## Solution 4: Cached Health Check

```python
import asyncio
import time
from typing import Optional


class CachedHealthCheck:
    """
    Caches the last health check result for a configurable TTL to
    prevent probe storms when health endpoints are polled frequently.
    """

    def __init__(
        self,
        aggregator: HealthCheckAggregator,
        cache_ttl_seconds: float = 5.0,
    ):
        self._aggregator = aggregator
        self._ttl = cache_ttl_seconds
        self._cached: Optional[dict] = None
        self._cached_at: float = 0.0
        self._lock = asyncio.Lock()

    async def get(self) -> dict:
        async with self._lock:
            if self._cached and time.time() - self._cached_at < self._ttl:
                return {**self._cached, "cache_hit": True}
            result = await self._aggregator.run()
            self._cached = result
            self._cached_at = time.time()
            return {**result, "cache_hit": False}
```

## Solution 5: HTTP Health Handler

```python
import json
import time


class HTTPHealthHandler:
    """
    Produces HTTP response data for liveness and readiness probes.
    Liveness always returns 200 if the process is running.
    Readiness returns 200 for HEALTHY/DEGRADED and 503 for UNHEALTHY.
    """

    def __init__(self, cached_check: CachedHealthCheck):
        self._check = cached_check
        self._start_time = time.time()

    async def liveness(self) -> tuple[int, str]:
        return 200, json.dumps({"status": "alive", "uptime_seconds": round(time.time() - self._start_time, 1)})

    async def readiness(self) -> tuple[int, str]:
        result = await self._check.get()
        status = result.get("status", "unknown")
        http_code = 503 if status == DependencyStatus.UNHEALTHY.value else 200
        return http_code, json.dumps(result, indent=2)

    async def startup(self) -> tuple[int, str]:
        """Returns 200 only when all critical dependencies are healthy (for startup probes)."""
        result = await self._check.get()
        status = result.get("status", "unknown")
        http_code = 200 if status == DependencyStatus.HEALTHY.value else 503
        return http_code, json.dumps(result, indent=2)
```

## Solution 6: Health Check History Tracker

```python
import time
from collections import deque
from threading import Lock
from typing import Deque, List


class HealthCheckHistoryTracker:
    """
    Records health check results over time and surfaces
    how frequently the agent has been in each status.
    """

    def __init__(self, max_records: int = 1000):
        self._max = max_records
        self._records: Deque[dict] = deque()
        self._lock = Lock()

    def record(self, result: dict) -> None:
        with self._lock:
            self._records.append({"ts": time.time(), "status": result.get("status")})
            if len(self._records) > self._max:
                self._records.popleft()

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        with self._lock:
            recent = [r for r in self._records if r["ts"] >= cutoff]
        total = len(recent)
        counts: dict = {}
        for r in recent:
            counts[r["status"]] = counts.get(r["status"], 0) + 1
        return {
            "window_seconds": window_seconds,
            "total_checks": total,
            "status_distribution": counts,
            "availability_pct": round(
                counts.get("healthy", 0) / max(total, 1) * 100, 2
            ),
        }
```

## Comparison

| Approach | Dependency Probe | Parallel Checks | Result Caching | HTTP Response Codes | History Tracking |
|---|---|---|---|---|---|
| DependencyChecker | Yes (per-dep) | No | No | No | No |
| HealthCheckAggregator | Via registry | Yes | No | No | No |
| CachedHealthCheck | Via aggregator | No | Yes (TTL) | No | No |
| HTTPHealthHandler | No | No | Via cached | Yes (200/503) | No |
| HealthCheckHistoryTracker | No | No | No | No | Yes |

**Best for production**: Set `cache_ttl_seconds=5` for readiness probes polled every 2 seconds by Kubernetes — this prevents 30 parallel dependency checks per second under a busy cluster. Mark the LLM API and primary database as `critical=True`; mark secondary caches and analytics services as `critical=False` so their failure produces DEGRADED rather than UNHEALTHY, keeping the instance in rotation while alerting on-call. Use the startup probe (strict HEALTHY required) separately from the readiness probe (DEGRADED still passes) so a new instance does not receive traffic before all dependencies are confirmed reachable.
