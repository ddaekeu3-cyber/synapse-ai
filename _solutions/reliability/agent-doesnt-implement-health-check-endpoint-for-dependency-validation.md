---
title: "Agent Doesn't Implement Health Check Endpoint for Dependency Validation"
description: "Agents deployed behind load balancers receive traffic before their dependencies are ready — LLM clients not yet authenticated, tool registries not yet loaded, database connections not yet established. Implement a structured health check endpoint that validates each dependency independently, reports readiness vs. liveness status, and blocks traffic until all required dependencies pass."
date: 2026-04-16
difficulty: intermediate
category: reliability
slug: agent-doesnt-implement-health-check-endpoint-for-dependency-validation
tags: [health-check, readiness-probe, liveness-probe, dependency-validation, kubernetes, startup-probe]
symptoms:
  - "Load balancer routes traffic to agent before LLM client is initialized"
  - "First requests after deployment fail because tool registry hasn't finished loading"
  - "No way to distinguish a crashed agent from one still initializing"
  - "Health endpoint returns 200 OK regardless of dependency state"
  - "Kubernetes readiness probe passes immediately, causing premature traffic routing"
---

## Why This Happens

Deployment platforms route traffic to a container as soon as the HTTP server starts listening. If the agent initializes dependencies asynchronously after server startup — loading embeddings, connecting to vector stores, authenticating LLM clients — early requests arrive before the agent is capable of handling them. A proper health check distinguishes liveness (the process is running and not deadlocked) from readiness (all dependencies are initialized and the agent can serve requests). Only readiness gates traffic; liveness gates restarts.

## Solution 1: Dependency Health Status

```python
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class DependencyStatus(str, Enum):
    UNKNOWN = "unknown"
    INITIALIZING = "initializing"
    HEALTHY = "healthy"
    DEGRADED = "degraded"       # functional but impaired
    UNHEALTHY = "unhealthy"
    TIMEOUT = "timeout"


@dataclass
class DependencyHealthResult:
    name: str
    status: DependencyStatus
    latency_ms: float
    message: str = ""
    detail: Optional[Any] = None
    checked_at: float = field(default_factory=time.time)
    required: bool = True       # required dependencies block readiness

    @property
    def is_ok(self) -> bool:
        return self.status in (DependencyStatus.HEALTHY, DependencyStatus.DEGRADED)

    @property
    def blocks_readiness(self) -> bool:
        return self.required and not self.is_ok
```

## Solution 2: Dependency Health Checker

```python
import asyncio
import time
from typing import Callable, Dict, List, Optional


class DependencyHealthChecker:
    """
    Runs async health check functions per dependency with timeout protection.
    Each check function should return a dict with at least a 'status' key.
    """

    def __init__(self, timeout_seconds: float = 5.0):
        self._timeout = timeout_seconds
        self._checks: Dict[str, tuple] = {}

    def register(
        self,
        name: str,
        check_fn: Callable[[], dict],
        required: bool = True,
    ) -> None:
        self._checks[name] = (check_fn, required)

    async def _run_one(self, name: str, check_fn: Callable, required: bool) -> DependencyHealthResult:
        start = time.time()
        try:
            result = await asyncio.wait_for(
                check_fn() if asyncio.iscoroutinefunction(check_fn) else asyncio.to_thread(check_fn),
                timeout=self._timeout,
            )
            latency_ms = (time.time() - start) * 1000
            status_str = result.get("status", "healthy")
            return DependencyHealthResult(
                name=name,
                status=DependencyStatus(status_str),
                latency_ms=round(latency_ms, 2),
                message=result.get("message", ""),
                detail=result.get("detail"),
                required=required,
            )
        except asyncio.TimeoutError:
            return DependencyHealthResult(
                name=name,
                status=DependencyStatus.TIMEOUT,
                latency_ms=round(self._timeout * 1000, 2),
                message=f"Check timed out after {self._timeout}s",
                required=required,
            )
        except Exception as exc:
            latency_ms = (time.time() - start) * 1000
            return DependencyHealthResult(
                name=name,
                status=DependencyStatus.UNHEALTHY,
                latency_ms=round(latency_ms, 2),
                message=str(exc)[:200],
                required=required,
            )

    async def run_all(self) -> List[DependencyHealthResult]:
        tasks = [
            self._run_one(name, fn, required)
            for name, (fn, required) in self._checks.items()
        ]
        return list(await asyncio.gather(*tasks))
```

## Solution 3: Readiness and Liveness Gate

```python
import time
from typing import List, Optional


class AgentHealthGate:
    """
    Evaluates dependency check results to determine readiness and liveness.
    Readiness: all required dependencies healthy (gates traffic).
    Liveness: process functional, not deadlocked (gates restarts).
    """

    def __init__(self, max_unhealthy_seconds: float = 60.0):
        self._max_unhealthy = max_unhealthy_seconds
        self._first_unhealthy_at: Optional[float] = None
        self._process_start = time.time()

    def evaluate(self, results: List[DependencyHealthResult]) -> dict:
        blocking = [r for r in results if r.blocks_readiness]
        ready = len(blocking) == 0

        if not ready:
            if self._first_unhealthy_at is None:
                self._first_unhealthy_at = time.time()
            unhealthy_duration = time.time() - self._first_unhealthy_at
        else:
            self._first_unhealthy_at = None
            unhealthy_duration = 0.0

        # Liveness fails only if unhealthy for longer than max threshold
        alive = unhealthy_duration < self._max_unhealthy

        return {
            "ready": ready,
            "alive": alive,
            "uptime_seconds": round(time.time() - self._process_start, 1),
            "unhealthy_seconds": round(unhealthy_duration, 1),
            "dependencies": [
                {
                    "name": r.name,
                    "status": r.status.value,
                    "required": r.required,
                    "latency_ms": r.latency_ms,
                    "message": r.message,
                }
                for r in results
            ],
            "blocking_dependencies": [r.name for r in blocking],
        }
```

## Solution 4: Startup Readiness Barrier

```python
import asyncio
import time
from typing import Optional


class StartupReadinessBarrier:
    """
    Blocks incoming requests during agent startup until all required
    dependencies pass their health checks. Requests arriving before
    readiness receive a 503 with a Retry-After header.
    """

    def __init__(
        self,
        checker: DependencyHealthChecker,
        gate: AgentHealthGate,
        poll_interval_seconds: float = 2.0,
        max_startup_seconds: float = 120.0,
    ):
        self._checker = checker
        self._gate = gate
        self._poll_interval = poll_interval_seconds
        self._max_startup = max_startup_seconds
        self._ready = asyncio.Event()
        self._startup_complete = False

    async def wait_for_ready(self) -> bool:
        start = time.time()
        while time.time() - start < self._max_startup:
            results = await self._checker.run_all()
            evaluation = self._gate.evaluate(results)
            if evaluation["ready"]:
                self._ready.set()
                self._startup_complete = True
                return True
            await asyncio.sleep(self._poll_interval)
        return False

    def is_ready(self) -> bool:
        return self._ready.is_set()

    async def require_ready(self) -> None:
        """Call at the start of each request handler."""
        if not self._ready.is_set():
            raise RuntimeError("Agent not ready — startup in progress")
```

## Solution 5: Health Check Response Builder

```python
import json
import time
from typing import List


class HealthCheckResponseBuilder:
    """
    Formats health check evaluation results into HTTP-compatible
    response bodies with appropriate status codes.
    """

    def build_readiness(self, evaluation: dict) -> tuple:
        """Returns (status_code, body_dict)."""
        status_code = 200 if evaluation["ready"] else 503
        body = {
            "status": "ready" if evaluation["ready"] else "not_ready",
            "checked_at": time.time(),
            **evaluation,
        }
        return status_code, body

    def build_liveness(self, evaluation: dict) -> tuple:
        status_code = 200 if evaluation["alive"] else 503
        body = {
            "status": "alive" if evaluation["alive"] else "dead",
            "checked_at": time.time(),
            "uptime_seconds": evaluation["uptime_seconds"],
            "unhealthy_seconds": evaluation["unhealthy_seconds"],
        }
        return status_code, body

    def build_startup(self, startup_barrier: StartupReadinessBarrier) -> tuple:
        ready = startup_barrier.is_ready()
        status_code = 200 if ready else 503
        return status_code, {
            "status": "started" if ready else "starting",
            "checked_at": time.time(),
        }
```

## Solution 6: Health Check History Tracker

```python
import time
from collections import deque
from threading import Lock
from typing import Deque, List, Tuple


class HealthCheckHistoryTracker:
    """
    Records health check outcomes over time for trend analysis.
    Tracks availability percentage and mean time between failures.
    """

    def __init__(self, max_records: int = 1000):
        self._max = max_records
        self._records: Deque[Tuple[float, bool]] = deque()
        self._lock = Lock()

    def record(self, evaluation: dict) -> None:
        with self._lock:
            self._records.append((time.time(), evaluation["ready"]))
            if len(self._records) > self._max:
                self._records.popleft()

    def availability(self, window_seconds: float = 3600.0) -> float:
        cutoff = time.time() - window_seconds
        with self._lock:
            recent = [(ts, ready) for ts, ready in self._records if ts >= cutoff]
        if not recent:
            return 1.0
        return round(sum(1 for _, r in recent if r) / len(recent), 4)

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        with self._lock:
            recent = [(ts, ready) for ts, ready in self._records if ts >= cutoff]
        total = len(recent)
        healthy = sum(1 for _, r in recent if r)
        return {
            "window_seconds": window_seconds,
            "checks": total,
            "healthy": healthy,
            "unhealthy": total - healthy,
            "availability": round(healthy / total, 4) if total > 0 else 1.0,
        }
```

## Comparison

| Approach | Per-Dependency Checks | Readiness Gate | Liveness Gate | Startup Barrier | History Tracking |
|---|---|---|---|---|---|
| DependencyHealthChecker | Yes (async, timeout) | No | No | No | No |
| AgentHealthGate | Via checker | Yes | Yes | No | No |
| StartupReadinessBarrier | Via checker | Via gate | No | Yes | No |
| HealthCheckResponseBuilder | No | Yes (HTTP) | Yes (HTTP) | Yes (HTTP) | No |
| HealthCheckHistoryTracker | No | No | No | No | Yes |

**Best for production**: Expose three distinct endpoints — `/healthz/ready`, `/healthz/live`, `/healthz/startup` — mapped to Kubernetes readinessProbe, livenessProbe, and startupProbe respectively. The startup probe should have a generous `failureThreshold` (30 × 10s = 5 min) to allow slow initializations; the liveness probe should have a tight threshold (3 × 10s) to catch deadlocks quickly. Register optional dependencies (non-critical enrichment services) as `required=False` so they report their status without blocking readiness. Log every readiness transition (healthy→unhealthy and back) as a structured event — these are the most reliable indicators of deployment issues.
