---
title: "Agent Doesn't Implement Health Check Endpoint for Readiness Probes"
description: "Agents deployed behind load balancers or in Kubernetes that lack a readiness health check receive live traffic before their model clients are initialized, their tool registries are loaded, and their downstream dependencies are reachable — causing the first requests to fail with connection errors or uninitialized-state exceptions. Implement a health check endpoint that reports per-component readiness so orchestrators can gate traffic until the agent is genuinely ready to serve."
date: 2026-04-16
difficulty: intermediate
category: reliability
slug: agent-doesnt-implement-health-check-endpoint-for-readiness-probes
tags: [health-check, readiness-probe, liveness-probe, kubernetes, load-balancer, startup-reliability]
symptoms:
  - "First requests after deployment fail with NoneType or uninitialized client errors"
  - "Load balancer routes traffic to an instance before it finishes loading tool schemas"
  - "Kubernetes pod restarts on OOMKill and immediately receives traffic before warm-up"
  - "No way to distinguish a starting instance from a crashed one"
  - "Deployment rollouts cause brief error spikes because readiness is not gated"
---

## Why This Happens

Kubernetes and cloud load balancers mark a pod as ready as soon as the process starts listening on its port — unless a readiness probe is configured. Agents that take several seconds to initialize model clients, load tool definitions, establish database connections, and warm embedding caches are live on the port before any of that work completes. A readiness probe that returns HTTP 503 until all components report healthy prevents the orchestrator from sending traffic to an unready instance. Liveness probes serve a different purpose: they detect a stuck or deadlocked instance that is running but not processing and trigger a restart.

## Solution 1: Component Health Status

```python
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional


class ComponentStatus(str, Enum):
    UNKNOWN = "unknown"
    INITIALIZING = "initializing"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"


@dataclass
class ComponentHealth:
    name: str
    status: ComponentStatus = ComponentStatus.UNKNOWN
    message: str = ""
    last_checked_at: Optional[float] = None
    last_healthy_at: Optional[float] = None
    check_duration_ms: float = 0.0
    metadata: Dict[str, str] = field(default_factory=dict)

    def mark_healthy(self, message: str = "") -> None:
        now = time.time()
        self.status = ComponentStatus.HEALTHY
        self.message = message
        self.last_checked_at = now
        self.last_healthy_at = now

    def mark_unhealthy(self, message: str) -> None:
        self.status = ComponentStatus.UNHEALTHY
        self.message = message
        self.last_checked_at = time.time()

    def mark_degraded(self, message: str) -> None:
        self.status = ComponentStatus.DEGRADED
        self.message = message
        self.last_checked_at = time.time()

    def seconds_since_healthy(self) -> Optional[float]:
        if self.last_healthy_at is None:
            return None
        return round(time.time() - self.last_healthy_at, 1)
```

## Solution 2: Component Health Registry

```python
from threading import Lock
from typing import Callable, Dict, List, Optional


class ComponentHealthRegistry:
    """
    Maintains a live map of component health states.
    Components register themselves and update their status as initialization progresses.
    """

    def __init__(self):
        self._components: Dict[str, ComponentHealth] = {}
        self._lock = Lock()

    def register(self, name: str) -> ComponentHealth:
        with self._lock:
            ch = ComponentHealth(name=name, status=ComponentStatus.INITIALIZING)
            self._components[name] = ch
            return ch

    def get(self, name: str) -> Optional[ComponentHealth]:
        with self._lock:
            return self._components.get(name)

    def all_components(self) -> List[ComponentHealth]:
        with self._lock:
            return list(self._components.values())

    def is_ready(self) -> bool:
        """True only when all registered components are HEALTHY."""
        with self._lock:
            return all(
                c.status == ComponentStatus.HEALTHY
                for c in self._components.values()
            )

    def is_live(self) -> bool:
        """True as long as no component is UNHEALTHY (degraded is tolerated)."""
        with self._lock:
            return all(
                c.status != ComponentStatus.UNHEALTHY
                for c in self._components.values()
            )

    def summary(self) -> dict:
        components = self.all_components()
        return {
            "ready": self.is_ready(),
            "live": self.is_live(),
            "component_count": len(components),
            "components": {
                c.name: {
                    "status": c.status.value,
                    "message": c.message,
                    "last_checked_at": c.last_checked_at,
                    "seconds_since_healthy": c.seconds_since_healthy(),
                }
                for c in components
            },
        }
```

## Solution 3: Active Health Checker

```python
import asyncio
import time
from typing import Awaitable, Callable, Dict, List


CheckFn = Callable[[], Awaitable[tuple]]  # returns (ok: bool, message: str)


class ActiveHealthChecker:
    """
    Runs user-supplied async check functions for each component on demand.
    Updates the ComponentHealth entries in the registry with results.
    """

    def __init__(self, registry: ComponentHealthRegistry):
        self._registry = registry
        self._checks: Dict[str, CheckFn] = {}

    def register_check(self, component_name: str, check_fn: CheckFn) -> None:
        self._checks[component_name] = check_fn

    async def run_check(self, component_name: str) -> ComponentHealth:
        health = self._registry.get(component_name)
        if health is None:
            health = self._registry.register(component_name)
        check_fn = self._checks.get(component_name)
        if check_fn is None:
            health.mark_unhealthy("no check function registered")
            return health

        start = time.time()
        try:
            ok, message = await check_fn()
            health.check_duration_ms = round((time.time() - start) * 1000, 2)
            if ok:
                health.mark_healthy(message)
            else:
                health.mark_unhealthy(message)
        except Exception as exc:
            health.check_duration_ms = round((time.time() - start) * 1000, 2)
            health.mark_unhealthy(f"check raised: {exc}")
        return health

    async def run_all(self) -> List[ComponentHealth]:
        tasks = [self.run_check(name) for name in self._checks]
        return await asyncio.gather(*tasks)
```

## Solution 4: Readiness Gate

```python
import asyncio
import time
from typing import Optional


class ReadinessGate:
    """
    Blocks until the registry reports ready or the timeout expires.
    Use at agent startup before opening the HTTP listener port
    or before signaling readiness to the orchestrator.
    """

    def __init__(
        self,
        registry: ComponentHealthRegistry,
        checker: ActiveHealthChecker,
        poll_interval_seconds: float = 0.5,
        timeout_seconds: float = 60.0,
    ):
        self._registry = registry
        self._checker = checker
        self._poll_interval = poll_interval_seconds
        self._timeout = timeout_seconds

    async def wait_until_ready(self) -> bool:
        """
        Returns True if all components become healthy within the timeout.
        Returns False on timeout.
        """
        deadline = time.time() + self._timeout
        while time.time() < deadline:
            await self._checker.run_all()
            if self._registry.is_ready():
                return True
            await asyncio.sleep(self._poll_interval)
        return False

    async def check_once(self) -> dict:
        await self._checker.run_all()
        return self._registry.summary()
```

## Solution 5: Health Endpoint Handler

```python
import json
import time
from typing import Callable, Optional


class HealthEndpointHandler:
    """
    Produces HTTP-compatible health responses for /healthz/ready and /healthz/live.
    Returns 200 when healthy, 503 when not — suitable for Kubernetes probes.
    """

    def __init__(
        self,
        registry: ComponentHealthRegistry,
        checker: Optional[ActiveHealthChecker] = None,
        run_active_checks_on_ready: bool = False,
    ):
        self._registry = registry
        self._checker = checker
        self._active_on_ready = run_active_checks_on_ready

    async def readiness(self) -> tuple:
        """Returns (status_code, body_dict)."""
        if self._active_on_ready and self._checker:
            await self._checker.run_all()
        summary = self._registry.summary()
        code = 200 if summary["ready"] else 503
        return code, {"status": "ready" if code == 200 else "not_ready", **summary}

    async def liveness(self) -> tuple:
        """Returns (status_code, body_dict)."""
        summary = self._registry.summary()
        code = 200 if summary["live"] else 503
        return code, {"status": "live" if code == 200 else "not_live", **summary}

    async def startup(self) -> tuple:
        """Startup probe: passes once any component has been healthy."""
        components = self._registry.all_components()
        any_healthy = any(c.last_healthy_at is not None for c in components)
        code = 200 if any_healthy else 503
        return code, {"status": "started" if code == 200 else "starting"}
```

## Solution 6: Health Check Dashboard

```python
import time


class HealthCheckDashboard:
    """
    Aggregates health history and probe response times for operational visibility.
    """

    def __init__(self, registry: ComponentHealthRegistry):
        self._registry = registry
        self._probe_log: list = []

    def record_probe(self, probe_type: str, status_code: int, duration_ms: float) -> None:
        self._probe_log.append({
            "ts": time.time(),
            "probe": probe_type,
            "status_code": status_code,
            "duration_ms": duration_ms,
        })
        if len(self._probe_log) > 5000:
            self._probe_log.pop(0)

    def render(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [r for r in self._probe_log if r["ts"] >= cutoff]
        by_probe: dict = {}
        for r in recent:
            p = r["probe"]
            if p not in by_probe:
                by_probe[p] = {"total": 0, "failures": 0}
            by_probe[p]["total"] += 1
            if r["status_code"] != 200:
                by_probe[p]["failures"] += 1

        return {
            "generated_at": time.time(),
            "current_health": self._registry.summary(),
            "probe_summary": by_probe,
        }
```

## Comparison

| Approach | Per-Component Status | Active Checks | HTTP Response | Readiness Gate | Dashboard |
|---|---|---|---|---|---|
| ComponentHealthRegistry | Yes | No | No | No | No |
| ActiveHealthChecker | Via registry | Yes (async) | No | No | No |
| ReadinessGate | Via registry | Via checker | No | Yes (blocking) | No |
| HealthEndpointHandler | Via registry | Optional | Yes (200/503) | No | No |
| HealthCheckDashboard | Via registry | No | No | No | Yes |

**Best for production**: Register one `ComponentHealth` entry per dependency at startup (LLM client, vector DB, tool registry, cache) and set each to `INITIALIZING`. As each init completes, call `mark_healthy()`. Wire `/healthz/ready` to `HealthEndpointHandler.readiness()` and configure the Kubernetes readiness probe with `initialDelaySeconds=5`, `periodSeconds=3`, `failureThreshold=5` — this gives the agent 20 seconds of total grace time before the pod is considered not ready. Use `ReadinessGate.wait_until_ready()` inside the entrypoint to prevent the HTTP server from starting at all until the agent is fully initialized, eliminating the race between port-open and component-ready.
