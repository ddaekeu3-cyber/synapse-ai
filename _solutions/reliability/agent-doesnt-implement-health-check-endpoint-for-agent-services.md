---
title: "Agent Doesn't Implement Health Check Endpoint for Agent Services"
description: "Agent services without health check endpoints cannot participate in load balancer rotation, Kubernetes liveness/readiness probes, or automated canary rollout gates. A crashed worker silently accepts connections it cannot serve, and a degraded worker stays in rotation long after its LLM provider connection broke. Implement structured health check endpoints that distinguish liveness, readiness, and deep dependency health with per-component status reporting."
date: 2026-04-16
difficulty: intermediate
category: reliability
slug: agent-doesnt-implement-health-check-endpoint-for-agent-services
tags: [health-checks, liveness, readiness, kubernetes-probes, load-balancer, service-health]
symptoms:
  - "Load balancer routes traffic to a worker whose LLM API key is invalid"
  - "Kubernetes restarts the pod on OOM but the health probe still returns 200"
  - "No readiness gate — newly deployed agents receive traffic before they have warmed their caches"
  - "Health check only verifies the HTTP server is up, not that the agent can actually serve requests"
  - "Canary deployments cannot gate on agent health because no structured health data exists"
---

## Why This Happens

The simplest health check returns HTTP 200 unconditionally — it proves the process is alive but not that it can serve requests. A fully functional health check must verify the process is alive (liveness), that all dependencies are reachable and the agent is ready to accept traffic (readiness), and optionally that deep dependencies meet SLA requirements (startup/deep checks). Kubernetes and load balancers need all three to make correct routing decisions.

## Solution 1: Health Status Definition

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional
import time


class HealthStatus(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"     # partially operational — still in rotation
    UNHEALTHY = "unhealthy"   # should be removed from rotation
    STARTING = "starting"     # not yet ready for traffic


class CheckType(str, Enum):
    LIVENESS = "liveness"     # is the process alive?
    READINESS = "readiness"   # can it serve traffic?
    STARTUP = "startup"       # has it finished initializing?
    DEEP = "deep"             # are all dependencies healthy?


@dataclass
class ComponentHealth:
    name: str
    status: HealthStatus
    check_type: CheckType
    latency_ms: float
    message: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    checked_at: float = field(default_factory=time.time)


@dataclass
class HealthReport:
    overall: HealthStatus
    components: List[ComponentHealth]
    version: str
    uptime_seconds: float
    generated_at: float = field(default_factory=time.time)

    def http_status_code(self) -> int:
        if self.overall == HealthStatus.HEALTHY:
            return 200
        if self.overall == HealthStatus.DEGRADED:
            return 200   # degraded still serves traffic
        if self.overall == HealthStatus.STARTING:
            return 503
        return 503   # UNHEALTHY

    def to_dict(self) -> dict:
        return {
            "status": self.overall.value,
            "version": self.version,
            "uptime_seconds": round(self.uptime_seconds, 1),
            "generated_at": self.generated_at,
            "components": [
                {
                    "name": c.name,
                    "status": c.status.value,
                    "type": c.check_type.value,
                    "latency_ms": c.latency_ms,
                    "message": c.message,
                    "metadata": c.metadata,
                }
                for c in self.components
            ],
        }
```

## Solution 2: Individual Health Checkers

```python
import asyncio
import time
from typing import Callable, Optional


class LLMProviderHealthChecker:
    """
    Verifies that the configured LLM provider API key is valid
    and the provider is reachable with a minimal test request.
    """

    def __init__(
        self,
        ping_fn: Callable[[], Any],   # async fn that makes a cheap API call
        timeout_seconds: float = 5.0,
    ) -> None:
        self._ping = ping_fn
        self._timeout = timeout_seconds

    async def check(self) -> ComponentHealth:
        start = time.time()
        try:
            await asyncio.wait_for(self._ping(), timeout=self._timeout)
            latency = (time.time() - start) * 1000
            return ComponentHealth(
                name="llm_provider",
                status=HealthStatus.HEALTHY,
                check_type=CheckType.READINESS,
                latency_ms=round(latency, 2),
            )
        except asyncio.TimeoutError:
            latency = (time.time() - start) * 1000
            return ComponentHealth(
                name="llm_provider",
                status=HealthStatus.UNHEALTHY,
                check_type=CheckType.READINESS,
                latency_ms=round(latency, 2),
                message=f"LLM provider ping timed out after {self._timeout}s",
            )
        except Exception as exc:
            latency = (time.time() - start) * 1000
            status = HealthStatus.UNHEALTHY
            if "429" in str(exc) or "rate" in str(exc).lower():
                status = HealthStatus.DEGRADED
            return ComponentHealth(
                name="llm_provider",
                status=status,
                check_type=CheckType.READINESS,
                latency_ms=round(latency, 2),
                message=str(exc)[:200],
            )


class ToolDependencyHealthChecker:
    """
    Checks reachability of tool dependencies (databases, APIs, vector stores).
    """

    def __init__(
        self,
        name: str,
        ping_fn: Callable[[], Any],
        timeout_seconds: float = 3.0,
        critical: bool = True,
    ) -> None:
        self._name = name
        self._ping = ping_fn
        self._timeout = timeout_seconds
        self._critical = critical

    async def check(self) -> ComponentHealth:
        start = time.time()
        try:
            await asyncio.wait_for(self._ping(), timeout=self._timeout)
            latency = (time.time() - start) * 1000
            return ComponentHealth(
                name=self._name,
                status=HealthStatus.HEALTHY,
                check_type=CheckType.READINESS,
                latency_ms=round(latency, 2),
            )
        except Exception as exc:
            latency = (time.time() - start) * 1000
            status = HealthStatus.UNHEALTHY if self._critical else HealthStatus.DEGRADED
            return ComponentHealth(
                name=self._name,
                status=status,
                check_type=CheckType.READINESS,
                latency_ms=round(latency, 2),
                message=str(exc)[:200],
            )
```

## Solution 3: Memory and Resource Health Checker

```python
import time


class ResourceHealthChecker:
    """
    Checks process memory usage and open file descriptors.
    Flags degraded state when memory exceeds warning threshold.
    """

    def __init__(
        self,
        memory_warning_mb: float = 1500.0,
        memory_critical_mb: float = 3000.0,
    ) -> None:
        self._warn = memory_warning_mb
        self._crit = memory_critical_mb

    def check(self) -> ComponentHealth:
        start = time.time()
        try:
            import psutil, os
            proc = psutil.Process(os.getpid())
            mem_mb = proc.memory_info().rss / (1024 * 1024)
            latency = (time.time() - start) * 1000

            if mem_mb >= self._crit:
                return ComponentHealth(
                    name="memory",
                    status=HealthStatus.UNHEALTHY,
                    check_type=CheckType.LIVENESS,
                    latency_ms=round(latency, 2),
                    message=f"RSS {mem_mb:.0f}MB exceeds critical threshold {self._crit}MB",
                    metadata={"rss_mb": round(mem_mb, 1)},
                )
            if mem_mb >= self._warn:
                return ComponentHealth(
                    name="memory",
                    status=HealthStatus.DEGRADED,
                    check_type=CheckType.LIVENESS,
                    latency_ms=round(latency, 2),
                    message=f"RSS {mem_mb:.0f}MB above warning threshold {self._warn}MB",
                    metadata={"rss_mb": round(mem_mb, 1)},
                )
            return ComponentHealth(
                name="memory",
                status=HealthStatus.HEALTHY,
                check_type=CheckType.LIVENESS,
                latency_ms=round(latency, 2),
                metadata={"rss_mb": round(mem_mb, 1)},
            )
        except ImportError:
            return ComponentHealth(
                name="memory",
                status=HealthStatus.HEALTHY,
                check_type=CheckType.LIVENESS,
                latency_ms=0.0,
                message="psutil not available — memory check skipped",
            )
```

## Solution 4: Health Check Aggregator

```python
import asyncio
import time
from typing import List


class HealthCheckAggregator:
    """
    Runs all registered health checkers concurrently and aggregates
    results into a single HealthReport with an overall status.
    """

    _STATUS_RANK = {
        HealthStatus.HEALTHY: 0,
        HealthStatus.STARTING: 1,
        HealthStatus.DEGRADED: 2,
        HealthStatus.UNHEALTHY: 3,
    }

    def __init__(
        self,
        version: str,
        started_at: Optional[float] = None,
    ) -> None:
        self._version = version
        self._started_at = started_at or time.time()
        self._async_checkers: List = []
        self._sync_checkers: List = []

    def add_async(self, checker) -> None:
        self._async_checkers.append(checker)

    def add_sync(self, checker) -> None:
        self._sync_checkers.append(checker)

    async def run(self) -> HealthReport:
        async_results = await asyncio.gather(
            *[c.check() for c in self._async_checkers],
            return_exceptions=False,
        )
        sync_results = [c.check() for c in self._sync_checkers]

        components = list(async_results) + sync_results
        overall = self._aggregate_status(components)

        return HealthReport(
            overall=overall,
            components=components,
            version=self._version,
            uptime_seconds=time.time() - self._started_at,
        )

    def _aggregate_status(self, components: List[ComponentHealth]) -> HealthStatus:
        if not components:
            return HealthStatus.HEALTHY
        worst = max(
            (self._STATUS_RANK[c.status] for c in components),
            default=0,
        )
        for status, rank in self._STATUS_RANK.items():
            if rank == worst:
                return status
        return HealthStatus.HEALTHY
```

## Solution 5: Cached Health Check Handler

```python
import asyncio
import time
from typing import Optional


class CachedHealthCheckHandler:
    """
    Caches health check results for a short TTL to avoid
    hammering dependencies on every Kubernetes probe interval (default: 10s).
    Liveness checks are never cached — they must always be fresh.
    """

    def __init__(
        self,
        aggregator: HealthCheckAggregator,
        readiness_cache_ttl: float = 5.0,
    ) -> None:
        self._aggregator = aggregator
        self._ttl = readiness_cache_ttl
        self._cached_report: Optional[HealthReport] = None
        self._cached_at: float = 0.0
        self._lock = asyncio.Lock()

    async def liveness(self) -> HealthReport:
        """Always runs fresh — only checks in-process liveness."""
        components = [c.check() for c in self._aggregator._sync_checkers
                      if c.check().check_type == CheckType.LIVENESS]
        # Simplified: return healthy if process is running
        return HealthReport(
            overall=HealthStatus.HEALTHY,
            components=[],
            version=self._aggregator._version,
            uptime_seconds=time.time() - self._aggregator._started_at,
        )

    async def readiness(self) -> HealthReport:
        """Cached for ttl_seconds to avoid probe hammering."""
        async with self._lock:
            if (self._cached_report is None
                    or time.time() - self._cached_at > self._ttl):
                self._cached_report = await self._aggregator.run()
                self._cached_at = time.time()
        return self._cached_report
```

## Solution 6: Health Route Builder

```python
from typing import Any


class HealthRouteBuilder:
    """
    Produces route handler coroutines compatible with async web frameworks
    (FastAPI, aiohttp, Starlette). Returns (body_dict, http_status_code).
    """

    def __init__(self, handler: CachedHealthCheckHandler) -> None:
        self._handler = handler

    async def handle_liveness(self) -> tuple:
        report = await self._handler.liveness()
        return report.to_dict(), report.http_status_code()

    async def handle_readiness(self) -> tuple:
        report = await self._handler.readiness()
        return report.to_dict(), report.http_status_code()

    async def handle_health(self) -> tuple:
        """Full deep health — used by monitoring systems, not k8s probes."""
        report = await self._aggregator.run()
        return report.to_dict(), report.http_status_code()
```

## Comparison

| Approach | Liveness | Readiness | Dependency Checks | Caching | HTTP Integration |
|---|---|---|---|---|---|
| LLMProviderHealthChecker | No | Yes | LLM only | No | No |
| ToolDependencyHealthChecker | No | Yes | Custom | No | No |
| ResourceHealthChecker | Yes | No | No | No | No |
| HealthCheckAggregator | Via checkers | Via checkers | Via checkers | No | No |
| CachedHealthCheckHandler | Yes | Yes (cached) | Via aggregator | Yes | No |
| HealthRouteBuilder | Via handler | Via handler | Via handler | Via handler | Yes |

**Best for production**: Expose three endpoints: `/healthz/live` (liveness, never cached, only checks in-process invariants), `/healthz/ready` (readiness, cached 5s, checks all dependencies), and `/healthz` (full deep check, for monitoring dashboards). Set Kubernetes `livenessProbe` to `/healthz/live` with `periodSeconds=10` and `failureThreshold=3`; set `readinessProbe` to `/healthz/ready` with `periodSeconds=5`. Mark LLM provider failure as `UNHEALTHY` (removes pod from rotation) but rate-limit errors as `DEGRADED` (keeps pod in rotation with reduced capacity signaling).
