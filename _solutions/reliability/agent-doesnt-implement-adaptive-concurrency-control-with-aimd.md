---
title: "Agent Doesn't Implement Adaptive Concurrency Control with AIMD"
description: "Agents with fixed concurrency limits waste capacity when downstream is healthy and overwhelm it during degradation. Implement Additive Increase Multiplicative Decrease (AIMD) concurrency control to automatically probe for the optimal concurrency level — increasing gradually on success, cutting sharply on error — the same algorithm TCP uses for congestion control."
date: 2026-04-16
difficulty: advanced
category: reliability
slug: agent-doesnt-implement-adaptive-concurrency-control-with-aimd
tags: [aimd, concurrency-control, adaptive-throttling, reliability, congestion-control, backpressure]
symptoms:
  - "Fixed concurrency limit of 10 leaves capacity unused when downstream handles 50 concurrent"
  - "Error spike causes agent to keep hitting the API at full concurrency — making degradation worse"
  - "Manual concurrency tuning required after every infrastructure change"
  - "No feedback loop between error rate and outbound request concurrency"
  - "Agent either throttles too aggressively (low fixed limit) or not enough (high fixed limit)"
---

## Why This Happens

Fixed concurrency limits are a static approximation of a dynamic system. The right concurrency level depends on downstream latency, error rate, and current load — all of which change continuously. AIMD (used in TCP congestion control) solves this elegantly: on success, increase concurrency by 1 (additive increase); on error or timeout, halve the concurrency limit (multiplicative decrease). This converges to the maximum stable throughput without manual tuning.

## Solution 1: AIMD Concurrency Controller

```python
import asyncio
import time
from dataclasses import dataclass, field
from typing import Optional

@dataclass
class AIMDState:
    limit: float          # current concurrency limit (float for smooth adjustment)
    in_flight: int = 0
    total_successes: int = 0
    total_errors: int = 0
    last_decrease: float = field(default_factory=time.monotonic)
    last_increase: float = field(default_factory=time.monotonic)

class AIMDConcurrencyController:
    """
    Implements TCP-style AIMD concurrency control.
    - On success: limit += additive_increase (default 0.1 per success)
    - On error: limit *= multiplicative_decrease (default 0.5)
    - Clamps limit between min_limit and max_limit
    - Cooldown between decreases to avoid oscillation
    """

    def __init__(
        self,
        initial_limit: int = 10,
        min_limit: int = 1,
        max_limit: int = 200,
        additive_increase: float = 0.1,
        multiplicative_decrease: float = 0.5,
        decrease_cooldown_seconds: float = 5.0,
    ):
        self._state = AIMDState(limit=float(initial_limit))
        self._min = float(min_limit)
        self._max = float(max_limit)
        self._ai = additive_increase
        self._md = multiplicative_decrease
        self._cooldown = decrease_cooldown_seconds
        self._sem = asyncio.Semaphore(initial_limit)
        self._lock = asyncio.Lock()

    @property
    def current_limit(self) -> int:
        return int(self._state.limit)

    @property
    def in_flight(self) -> int:
        return self._state.in_flight

    async def acquire(self) -> None:
        await self._sem.acquire()
        self._state.in_flight += 1

    def release(self) -> None:
        self._state.in_flight = max(0, self._state.in_flight - 1)
        self._sem.release()

    async def record_success(self) -> None:
        async with self._lock:
            old_limit = int(self._state.limit)
            self._state.limit = min(self._max, self._state.limit + self._ai)
            self._state.total_successes += 1
            new_limit = int(self._state.limit)

            # If integer limit increased, add a token to the semaphore
            if new_limit > old_limit:
                for _ in range(new_limit - old_limit):
                    self._sem.release()   # increase capacity

            self._state.last_increase = time.monotonic()

    async def record_error(self) -> None:
        async with self._lock:
            now = time.monotonic()
            # Respect cooldown to prevent rapid oscillation
            if now - self._state.last_decrease < self._cooldown:
                self._state.total_errors += 1
                return

            old_limit = int(self._state.limit)
            self._state.limit = max(self._min, self._state.limit * self._md)
            self._state.total_errors += 1
            new_limit = int(self._state.limit)
            self._state.last_decrease = now

            # If integer limit decreased, drain excess tokens
            if new_limit < old_limit:
                for _ in range(old_limit - new_limit):
                    try:
                        self._sem.acquire_nowait()
                    except asyncio.QueueEmpty:
                        break

    def stats(self) -> dict:
        return {
            "current_limit": self.current_limit,
            "in_flight": self._state.in_flight,
            "headroom": self.current_limit - self._state.in_flight,
            "total_successes": self._state.total_successes,
            "total_errors": self._state.total_errors,
            "error_rate": round(
                self._state.total_errors /
                max(self._state.total_successes + self._state.total_errors, 1),
                4,
            ),
        }
```

## Solution 2: AIMD-Controlled Request Executor

```python
import asyncio
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Callable, Coroutine, Optional, Tuple

class AIMDControlledExecutor:
    """
    Wraps any async function with AIMD concurrency control.
    Automatically records success/error and adjusts the limit.
    Supports timeout-as-error: treats slow responses as congestion signals.
    """

    def __init__(
        self,
        controller: AIMDConcurrencyController,
        request_timeout_seconds: float = 30.0,
        error_status_codes: Optional[set] = None,
    ):
        self._controller = controller
        self._timeout = request_timeout_seconds
        self._error_codes = error_status_codes or {429, 500, 502, 503, 504}

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        """Context manager that acquires a concurrency slot and records outcome."""
        await self._controller.acquire()
        success = False
        try:
            yield
            success = True
        except Exception:
            raise
        finally:
            self._controller.release()
            if success:
                await self._controller.record_success()
            else:
                await self._controller.record_error()

    async def execute(
        self,
        fn: Callable[..., Coroutine],
        *args,
        **kwargs,
    ) -> Any:
        """Execute fn with AIMD concurrency control and timeout."""
        await self._controller.acquire()
        t0 = time.monotonic()
        try:
            result = await asyncio.wait_for(
                fn(*args, **kwargs),
                timeout=self._timeout,
            )
            # Check if result has a status code indicating server error
            status = getattr(result, "status_code", None)
            if status and status in self._error_codes:
                await self._controller.record_error()
            else:
                await self._controller.record_success()
            return result
        except asyncio.TimeoutError:
            await self._controller.record_error()
            raise
        except Exception:
            await self._controller.record_error()
            raise
        finally:
            self._controller.release()
```

## Solution 3: Gradient-Descent Concurrency Optimizer

```python
import math
import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, Optional

@dataclass
class ThroughputSample:
    concurrency: int
    throughput: float   # requests per second
    latency_ms: float
    timestamp: float

class GradientDescentConcurrencyOptimizer:
    """
    Alternative to AIMD: uses gradient descent to find the concurrency level
    that maximizes throughput while keeping latency below a target.
    Uses Little's Law: throughput = concurrency / latency.
    """

    def __init__(
        self,
        target_latency_ms: float = 500.0,
        step_size: int = 2,
        history_size: int = 20,
    ):
        self._target_latency = target_latency_ms
        self._step = step_size
        self._history: Deque[ThroughputSample] = deque(maxlen=history_size)
        self._current_concurrency: int = 5

    def record(self, concurrency: int, latency_ms: float, completed: int, window_ms: float) -> None:
        throughput = completed / max(window_ms / 1000, 0.001)
        self._history.append(ThroughputSample(
            concurrency=concurrency,
            throughput=throughput,
            latency_ms=latency_ms,
            timestamp=time.monotonic(),
        ))

    def optimal_concurrency(self) -> int:
        if len(self._history) < 3:
            return self._current_concurrency

        recent = list(self._history)[-5:]
        avg_latency = sum(s.latency_ms for s in recent) / len(recent)
        avg_throughput = sum(s.throughput for s in recent) / len(recent)

        # Little's Law: optimal concurrency ≈ target_throughput × target_latency
        # If latency > target, reduce; if latency < target and throughput is growing, increase
        if avg_latency > self._target_latency * 1.2:
            self._current_concurrency = max(1, self._current_concurrency - self._step)
        elif avg_latency < self._target_latency * 0.8:
            # Check if previous increase improved throughput
            if len(self._history) >= 2:
                prev_throughput = list(self._history)[-2].throughput
                if avg_throughput >= prev_throughput:
                    self._current_concurrency += self._step

        return self._current_concurrency
```

## Solution 4: Per-Endpoint AIMD Pool

```python
import asyncio
from dataclasses import dataclass
from typing import Dict, Optional

@dataclass
class EndpointConfig:
    endpoint: str
    initial_limit: int = 10
    min_limit: int = 1
    max_limit: int = 100

class PerEndpointAIMDPool:
    """
    Maintains separate AIMD controllers per downstream endpoint.
    One slow API doesn't constrain concurrency to other healthy APIs.
    """

    def __init__(self, default_config: Optional[EndpointConfig] = None):
        self._controllers: Dict[str, AIMDConcurrencyController] = {}
        self._default = default_config

    def get_controller(self, endpoint: str) -> AIMDConcurrencyController:
        if endpoint not in self._controllers:
            if self._default:
                self._controllers[endpoint] = AIMDConcurrencyController(
                    initial_limit=self._default.initial_limit,
                    min_limit=self._default.min_limit,
                    max_limit=self._default.max_limit,
                )
            else:
                self._controllers[endpoint] = AIMDConcurrencyController()
        return self._controllers[endpoint]

    def global_stats(self) -> dict:
        return {
            endpoint: ctrl.stats()
            for endpoint, ctrl in self._controllers.items()
        }

    def total_in_flight(self) -> int:
        return sum(ctrl.in_flight for ctrl in self._controllers.values())
```

## Solution 5: AIMD Limit History Tracker

```python
import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, List

@dataclass
class LimitEvent:
    event_type: str   # "increase" | "decrease" | "clamp_min" | "clamp_max"
    old_limit: int
    new_limit: int
    trigger: str      # "success" | "error" | "timeout"
    timestamp: float

class AIMDLimitHistoryTracker:
    """
    Records the history of AIMD limit changes for analysis.
    Useful for understanding how the system responds to load patterns
    and tuning the AIMD parameters.
    """

    def __init__(self, history_size: int = 500):
        self._events: Deque[LimitEvent] = deque(maxlen=history_size)

    def record(
        self, event_type: str, old_limit: int, new_limit: int, trigger: str
    ) -> None:
        self._events.append(LimitEvent(
            event_type=event_type,
            old_limit=old_limit,
            new_limit=new_limit,
            trigger=trigger,
            timestamp=time.time(),
        ))

    def oscillation_rate(self, window_seconds: float = 60.0) -> float:
        """High oscillation = limit bounces between increase/decrease frequently."""
        cutoff = time.time() - window_seconds
        recent = [e for e in self._events if e.timestamp >= cutoff]
        if len(recent) < 2:
            return 0.0
        changes = sum(
            1 for i in range(1, len(recent))
            if recent[i].event_type != recent[i-1].event_type
        )
        return changes / len(recent)

    def summary(self, window_seconds: float = 300.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [e for e in self._events if e.timestamp >= cutoff]
        return {
            "total_events": len(recent),
            "increases": sum(1 for e in recent if e.event_type == "increase"),
            "decreases": sum(1 for e in recent if e.event_type == "decrease"),
            "oscillation_rate": round(self.oscillation_rate(window_seconds), 3),
            "min_limit_seen": min((e.new_limit for e in recent), default=0),
            "max_limit_seen": max((e.new_limit for e in recent), default=0),
        }
```

## Solution 6: AIMD Health Dashboard

```python
import time
from typing import Dict

class AIMDHealthDashboard:
    def __init__(self, pool: PerEndpointAIMDPool, history: AIMDLimitHistoryTracker):
        self._pool = pool
        self._history = history

    def report(self) -> dict:
        endpoint_stats = self._pool.global_stats()
        congested = {
            ep: s for ep, s in endpoint_stats.items()
            if s["current_limit"] <= 2 or s["error_rate"] > 0.1
        }
        return {
            "endpoints": len(endpoint_stats),
            "total_in_flight": self._pool.total_in_flight(),
            "congested_endpoints": list(congested.keys()),
            "limit_history": self._history.summary(),
            "endpoint_details": endpoint_stats,
            "generated_at": time.time(),
        }
```

## Comparison

| Approach | Auto-Tunes | Convergence | Per-Endpoint | Oscillation Control |
|---|---|---|---|---|
| AIMDConcurrencyController | Yes (AI+MD) | Fast | No | Cooldown |
| AIMDControlledExecutor | Via controller | N/A | No | Via controller |
| GradientDescentOptimizer | Yes (gradient) | Slow | No | Via step size |
| PerEndpointAIMDPool | Yes | Fast | Yes | Via controller |
| AIMDLimitHistoryTracker | N/A (metrics) | N/A | N/A | Detects it |
| AIMDHealthDashboard | N/A | N/A | N/A | N/A |

**Best for production**: Use `PerEndpointAIMDPool` with one AIMD controller per downstream API. Set `additive_increase=0.05` (slower probe) and `multiplicative_decrease=0.5` (fast retreat) for external APIs where overload causes cascading failures. Monitor `oscillation_rate` in `AIMDLimitHistoryTracker` — rate above 0.3 means parameters need tuning (try longer cooldown or smaller AI step). Never use a single global limit across multiple downstream endpoints — one slow API will starve all others.
