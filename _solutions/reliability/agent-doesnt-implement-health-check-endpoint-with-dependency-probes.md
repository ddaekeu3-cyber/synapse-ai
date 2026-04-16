---
title: "Agent Doesn't Implement Health Check Endpoint with Dependency Probes"
description: "Agents that expose only a shallow liveness endpoint — returning 200 OK as long as the process is running — are declared healthy by load balancers and orchestrators even when their downstream dependencies are unreachable. Implement deep health checks that probe each critical dependency and report a structured readiness status, allowing orchestration layers to route traffic away from agents that cannot serve requests."
date: 2026-04-16
difficulty: intermediate
category: reliability
slug: agent-doesnt-implement-health-check-endpoint-with-dependency-probes
tags: [health-check, dependency-probes, readiness, liveness, kubernetes, load-balancer-routing]
symptoms:
  - "Load balancer sends traffic to agent instances that cannot reach the database"
  - "Kubernetes readiness probe passes but agent returns 500s for every request"
  - "Health endpoint returns 200 even when the LLM API key is expired"
  - "No distinction between liveness (process alive) and readiness (can serve traffic)"
  - "Dependency failures are discovered by users, not by health monitoring"
---

## Why This Happens

A shallow health check (`return 200`) tests only that the HTTP server is accepting connections. It says nothing about whether the agent can actually complete a request — whether the LLM client is authenticated, whether the database is reachable, or whether the tool registry loaded successfully. Orchestration systems use health check results to route traffic: a false-healthy agent receives requests it cannot serve. Deep health checks probe each critical dependency with a lightweight operation (a ping, a HEAD request, a SELECT 1) and aggregate the results into a readiness decision that the orchestrator can act on.

## Solution 1: Dependency Probe

```python
import asyncio
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Optional


class ProbeStatus(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"


@dataclass
class ProbeResult:
    name: str
    status: ProbeStatus
    latency_ms: float
    message: str = ""
    checked_at: float = 0.0

    def __post_init__(self) -> None:
        if not self.checked_at:
            self.checked_at = time.time()


class DependencyProbe:
    """
    Wraps a probe callable with timeout, error handling, and
    latency measurement. Returns a structured ProbeResult.
    """

    def __init__(
        self,
        name: str,
        probe_fn: Callable,
        timeout_seconds: float = 3.0,
        critical: bool = True,
    ):
        self._name = name
        self._probe_fn = probe_fn
        self._timeout = timeout_seconds
        self.critical = critical

    async def run(self) -> ProbeResult:
        start = time.time()
        try:
            await asyncio.wait_for(self._probe_fn(), timeout=self._timeout)
            latency_ms = round((time.time() - start) * 1000, 2)
            return ProbeResult(
                name=self._name,
                status=ProbeStatus.HEALTHY,
                latency_ms=latency_ms,
                message="ok",
            )
        except asyncio.TimeoutError:
            latency_ms = round((time.time() - start) * 1000, 2)
            return ProbeResult(
                name=self._name,
                status=ProbeStatus.UNHEALTHY,
                latency_ms=latency_ms,
                message=f"probe timed out after {self._timeout}s",
            )
        except Exception as exc:
            latency_ms = round((time.time() - start) * 1000, 2)
            return ProbeResult(
                name=self._name,
                status=ProbeStatus.UNHEALTHY,
                latency_ms=latency_ms,
                message=str(exc)[:200],
            )
```

## Solution 2: Health Check Aggregator

```python
import asyncio
from typing import Dict, List, Optional


class HealthCheckAggregator:
    """
    Runs all registered dependency probes concurrently and aggregates
    results into an overall readiness status.
    A single failing critical probe marks the agent as NOT READY.
    """

    def __init__(self, probes: List[DependencyProbe]):
        self._probes = probes

    async def check(self) -> dict:
        results = await asyncio.gather(
            *[probe.run() for probe in self._probes],
            return_exceptions=False,
        )

        probe_map: Dict[str, ProbeResult] = {r.name: r for r in results}
        critical_failures = [
            r for r in results
            if r.status == ProbeStatus.UNHEALTHY
            and next(p for p in self._probes if p._name == r.name).critical
        ]
        degraded = [r for r in results if r.status == ProbeStatus.DEGRADED]

        if critical_failures:
            overall = ProbeStatus.UNHEALTHY
        elif degraded:
            overall = ProbeStatus.DEGRADED
        else:
            overall = ProbeStatus.HEALTHY

        return {
            "status": overall.value,
            "ready": overall != ProbeStatus.UNHEALTHY,
            "checks": {
                name: {
                    "status": r.status.value,
                    "latency_ms": r.latency_ms,
                    "message": r.message,
                }
                for name, r in probe_map.items()
            },
            "critical_failures": [r.name for r in critical_failures],
            "degraded": [r.name for r in degraded],
        }
```

## Solution 3: Probe Result Cache

```python
import asyncio
import time
from typing import Dict, Optional


class ProbeResultCache:
    """
    Caches probe results for a configurable TTL to avoid hammering
    dependencies on every health check poll from the orchestrator.
    """

    def __init__(self, ttl_seconds: float = 10.0):
        self._ttl = ttl_seconds
        self._cache: Dict[str, ProbeResult] = {}
        self._lock = asyncio.Lock()

    async def get_or_run(self, probe: DependencyProbe) -> ProbeResult:
        async with self._lock:
            cached = self._cache.get(probe._name)
            if cached and time.time() - cached.checked_at < self._ttl:
                return cached

        result = await probe.run()
        async with self._lock:
            self._cache[probe._name] = result
        return result

    def invalidate(self, probe_name: str) -> None:
        self._cache.pop(probe_name, None)

    def all_cached(self) -> Dict[str, dict]:
        return {
            name: {"status": r.status.value, "age_seconds": round(time.time() - r.checked_at, 1)}
            for name, r in self._cache.items()
        }
```

## Solution 4: Readiness State Machine

```python
import time
from typing import Optional


class ReadinessStateMachine:
    """
    Adds hysteresis to the readiness signal: requires N consecutive
    healthy checks before transitioning from NOT_READY to READY,
    and transitions to NOT_READY on the first unhealthy result.
    Prevents flapping under transient failures.
    """

    def __init__(
        self,
        healthy_threshold: int = 2,
        initial_ready: bool = False,
    ):
        self._threshold = healthy_threshold
        self._ready = initial_ready
        self._consecutive_healthy = 0
        self._last_transition_at: Optional[float] = None
        self._transitions: int = 0

    def update(self, check_result: dict) -> bool:
        is_healthy = check_result.get("ready", False)
        if is_healthy:
            self._consecutive_healthy += 1
            if not self._ready and self._consecutive_healthy >= self._threshold:
                self._ready = True
                self._last_transition_at = time.time()
                self._transitions += 1
        else:
            self._consecutive_healthy = 0
            if self._ready:
                self._ready = False
                self._last_transition_at = time.time()
                self._transitions += 1

        return self._ready

    def is_ready(self) -> bool:
        return self._ready

    def state(self) -> dict:
        return {
            "ready": self._ready,
            "consecutive_healthy": self._consecutive_healthy,
            "threshold": self._threshold,
            "transitions": self._transitions,
            "last_transition_at": self._last_transition_at,
        }
```

## Solution 5: Startup Probe Sequencer

```python
import asyncio
import time
from typing import List


class StartupProbeSequencer:
    """
    Runs probes sequentially at startup with retries until all critical
    dependencies are healthy or a deadline is exceeded.
    Prevents the agent from accepting traffic before dependencies are ready.
    """

    def __init__(
        self,
        probes: List[DependencyProbe],
        retry_interval_seconds: float = 2.0,
        startup_deadline_seconds: float = 60.0,
    ):
        self._probes = [p for p in probes if p.critical]
        self._interval = retry_interval_seconds
        self._deadline = startup_deadline_seconds

    async def wait_until_ready(self) -> dict:
        start = time.time()
        attempts = 0
        last_results = {}

        while time.time() - start < self._deadline:
            attempts += 1
            all_healthy = True
            for probe in self._probes:
                result = await probe.run()
                last_results[result.name] = result
                if result.status != ProbeStatus.HEALTHY:
                    all_healthy = False

            if all_healthy:
                return {
                    "ready": True,
                    "attempts": attempts,
                    "elapsed_ms": round((time.time() - start) * 1000, 2),
                    "probes": {n: r.status.value for n, r in last_results.items()},
                }

            await asyncio.sleep(self._interval)

        return {
            "ready": False,
            "attempts": attempts,
            "elapsed_ms": round((time.time() - start) * 1000, 2),
            "failed_probes": [
                n for n, r in last_results.items()
                if r.status != ProbeStatus.HEALTHY
            ],
        }
```

## Solution 6: Health Check Dashboard

```python
import time


class HealthCheckDashboard:
    """
    Combines aggregated probe results, readiness state machine,
    and probe cache into a single health report for ops visibility.
    """

    def __init__(
        self,
        aggregator: HealthCheckAggregator,
        state_machine: ReadinessStateMachine,
        cache: ProbeResultCache,
    ):
        self._aggregator = aggregator
        self._state_machine = state_machine
        self._cache = cache

    async def render(self) -> dict:
        check_result = await self._aggregator.check()
        self._state_machine.update(check_result)
        return {
            "generated_at": time.time(),
            "readiness": self._state_machine.state(),
            "health_check": check_result,
            "cached_probes": self._cache.all_cached(),
        }
```

## Comparison

| Approach | Per-Dependency Probing | Concurrent Probes | Caching | Hysteresis | Startup Sequencing |
|---|---|---|---|---|---|
| DependencyProbe | Yes (single) | No | No | No | No |
| HealthCheckAggregator | Yes (all) | Yes | No | No | No |
| ProbeResultCache | Via probes | No | Yes (TTL) | No | No |
| ReadinessStateMachine | No | No | No | Yes (threshold) | No |
| StartupProbeSequencer | Via probes | No | No | No | Yes |
| HealthCheckDashboard | No | No | No | No | Yes |

**Best for production**: Separate liveness from readiness — liveness (`/healthz`) returns 200 if the process is alive (no deadlock), readiness (`/readyz`) returns 200 only when all critical dependencies pass. Set `healthy_threshold=2` in `ReadinessStateMachine` to require two consecutive clean checks before re-entering rotation — this prevents a briefly-recovered dependency from immediately routing traffic before it stabilizes. Cache probe results for 10 seconds to avoid probe traffic becoming a DDoS against dependencies during orchestrator polling bursts. Mark non-critical dependencies (analytics sinks, notification services) as `critical=False` so their failure degrades but does not block traffic.
