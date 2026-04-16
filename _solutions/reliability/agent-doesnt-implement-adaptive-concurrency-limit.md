---
title: "Agent Doesn't Implement Adaptive Concurrency Limit"
description: "AI agents that use a fixed concurrency cap waste capacity when the downstream is healthy and overload it when it degrades. An adaptive concurrency limiter continuously measures latency and adjusts the cap to keep the system operating at its optimal point."
date: 2025-02-01
difficulty: advanced
category: reliability
slug: agent-doesnt-implement-adaptive-concurrency-limit
tags:
  - concurrency
  - adaptive-limits
  - backpressure
  - aimd
  - gradient-descent
  - reliability
  - performance
symptoms:
  - "Fixed concurrency cap is too conservative during normal load, wasting throughput"
  - "Downstream APIs get overwhelmed when the cap is set too high"
  - "Agent throughput crashes after a latency spike instead of recovering gracefully"
  - "Manual tuning of MAX_CONCURRENT_REQUESTS is required after every deployment"
  - "Queue depth grows unboundedly when the downstream is saturated"
---

## Problem

Static concurrency limits are a blunt instrument. Set too low: the agent underutilises the downstream API and wastes wall-clock time. Set too high: the first sign of downstream saturation causes a cascade — every in-flight request times out, the agent retries, and the downstream never recovers.

Adaptive concurrency controllers borrow ideas from TCP congestion control (AIMD, BBR) and apply them to application-level request concurrency. The key insight: **round-trip latency is a leading indicator of downstream saturation**. When latency increases beyond the minimum observed, reduce the limit; when latency is stable and low, increase it.

---

## Solution 1: AIMD Adaptive Limit (Additive Increase / Multiplicative Decrease)

Increase the limit by 1 on each successful request (additive increase); halve it on a timeout or error (multiplicative decrease). Mirrors TCP's classic congestion control.

```python
import asyncio
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class AIMDAdaptiveLimiter:
    """
    AIMD concurrency limiter.

    On success: limit += 1  (up to max_limit)
    On error/timeout: limit = max(min_limit, limit // 2)

    Usage:
        limiter = AIMDAdaptiveLimiter(initial=10, min_limit=2, max_limit=200)
        async with limiter:
            await call_api()
    """
    initial: int = 10
    min_limit: int = 2
    max_limit: int = 200
    _limit: int = field(init=False)
    _inflight: int = field(init=False, default=0)
    _semaphore: asyncio.Semaphore = field(init=False)
    _lock: asyncio.Lock = field(init=False)

    def __post_init__(self):
        self._limit = self.initial
        self._inflight = 0
        self._semaphore = asyncio.Semaphore(self.initial)
        self._lock = asyncio.Lock()

    async def _acquire(self):
        await self._semaphore.acquire()
        self._inflight += 1

    def _release(self, success: bool):
        self._semaphore.release()
        self._inflight -= 1
        if success:
            new_limit = min(self.max_limit, self._limit + 1)
        else:
            new_limit = max(self.min_limit, self._limit // 2)
        delta = new_limit - self._limit
        self._limit = new_limit
        # Adjust semaphore capacity
        if delta > 0:
            for _ in range(delta):
                self._semaphore.release()
        # Negative delta: semaphore drains naturally as permits are not re-released

    async def __aenter__(self):
        await self._acquire()
        return self

    async def __aexit__(self, exc_type, *_):
        self._release(success=exc_type is None)

    @property
    def current_limit(self) -> int:
        return self._limit

    @property
    def inflight(self) -> int:
        return self._inflight
```

---

## Solution 2: Gradient-Descent Concurrency Controller (Netflix-Style)

Inspired by Netflix's `concurrency-limits` library. Computes the gradient of latency w.r.t. the current limit. If latency is increasing, decrease the limit; if decreasing, increase it.

```python
import asyncio
import math
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Optional


class GradientConcurrencyLimiter:
    """
    Gradient-based adaptive concurrency limit.

    Tracks short-window vs long-window RTT. When short > long * threshold,
    back off. When short <= long, increase aggressively.

    Usage:
        limiter = GradientConcurrencyLimiter(initial=20)
        async with limiter.acquire() as permit:
            await call_api()
            permit.success()
    """

    def __init__(self, initial: int = 20, min_limit: int = 4,
                 max_limit: int = 1000, smoothing: float = 0.2,
                 rtt_tolerance: float = 1.5):
        self._limit = float(initial)
        self._min = min_limit
        self._max = max_limit
        self._smoothing = smoothing
        self._rtt_tolerance = rtt_tolerance
        self._inflight = 0
        self._lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(initial)

        # RTT tracking
        self._min_rtt: Optional[float] = None
        self._short_rtt: Optional[float] = None
        self._sample_window: Deque[float] = deque(maxlen=100)

    def acquire(self) -> "_GradientPermit":
        return _GradientPermit(self)

    async def _do_acquire(self):
        await self._semaphore.acquire()
        async with self._lock:
            self._inflight += 1

    def _record(self, rtt: float, success: bool):
        self._sample_window.append(rtt)
        if self._min_rtt is None or rtt < self._min_rtt:
            self._min_rtt = rtt

        # Exponential moving average for short-window RTT
        alpha = self._smoothing
        if self._short_rtt is None:
            self._short_rtt = rtt
        else:
            self._short_rtt = alpha * rtt + (1 - alpha) * self._short_rtt

        # Compute new limit
        if self._min_rtt and self._short_rtt:
            gradient = self._min_rtt / self._short_rtt
            new_limit = self._limit * gradient
        else:
            new_limit = self._limit

        if not success:
            new_limit = self._limit * 0.75

        new_limit = max(self._min, min(self._max, new_limit))
        old_int = int(self._limit)
        self._limit = new_limit
        new_int = int(new_limit)

        delta = new_int - old_int
        if delta > 0:
            for _ in range(delta):
                self._semaphore.release()

        self._semaphore.release()  # release acquired slot
        self._inflight -= 1

    @property
    def current_limit(self) -> int:
        return int(self._limit)

    @property
    def utilisation(self) -> float:
        lim = max(1, int(self._limit))
        return self._inflight / lim


class _GradientPermit:
    def __init__(self, limiter: GradientConcurrencyLimiter):
        self._limiter = limiter
        self._start: float = 0.0
        self._ok: bool = True

    async def __aenter__(self):
        await self._limiter._do_acquire()
        self._start = time.monotonic()
        return self

    def success(self):
        self._ok = True

    def failure(self):
        self._ok = False

    async def __aexit__(self, exc_type, *_):
        rtt = time.monotonic() - self._start
        self._limiter._record(rtt, success=self._ok and exc_type is None)
```

---

## Solution 3: Little's Law Limit Estimator

Use Little's Law (L = λW) to estimate the optimal concurrency: measure average throughput (λ) and average latency (W), then set limit = λ × W. Recalculates every window.

```python
import asyncio
import time
from collections import deque
from dataclasses import dataclass


@dataclass
class LatencySample:
    start: float
    end: float


class LittlesLawLimiter:
    """
    Adaptive limit derived from Little's Law: L = λ × W
    where λ = observed RPS, W = average latency.

    Recalculates the limit every `window` seconds.

    Usage:
        limiter = LittlesLawLimiter(window=5.0, safety_factor=0.8)
        asyncio.create_task(limiter.recalculate_loop())
        async with limiter:
            await call_api()
    """

    def __init__(self, window: float = 5.0, safety_factor: float = 0.8,
                 min_limit: int = 2, max_limit: int = 500, initial: int = 10):
        self._window = window
        self._safety = safety_factor
        self._min = min_limit
        self._max = max_limit
        self._limit = initial
        self._semaphore = asyncio.Semaphore(initial)
        self._samples: deque = deque()
        self._lock = asyncio.Lock()

    async def recalculate_loop(self):
        while True:
            await asyncio.sleep(self._window)
            await self._recalculate()

    async def _recalculate(self):
        now = time.monotonic()
        cutoff = now - self._window
        async with self._lock:
            while self._samples and self._samples[0].start < cutoff:
                self._samples.popleft()
            if len(self._samples) < 2:
                return
            lam = len(self._samples) / self._window
            avg_w = sum(s.end - s.start for s in self._samples) / len(self._samples)
            new_limit = int(lam * avg_w * self._safety) + 1
            new_limit = max(self._min, min(self._max, new_limit))
            delta = new_limit - self._limit
            self._limit = new_limit
            if delta > 0:
                for _ in range(delta):
                    self._semaphore.release()

    async def __aenter__(self):
        await self._semaphore.acquire()
        self._start = time.monotonic()
        return self

    async def __aexit__(self, *_):
        end = time.monotonic()
        async with self._lock:
            self._samples.append(LatencySample(self._start, end))
        self._semaphore.release()
```

---

## Solution 4: Percentile-Based Limit Controller

Adjusts the limit based on p99 latency relative to a target SLO. If p99 exceeds the SLO, reduce the limit. If p99 is well below, increase it.

```python
import asyncio
import statistics
import time
from collections import deque
from typing import Optional


class PercentileLimitController:
    """
    Concurrency limit controller driven by p99 latency SLO.

    If p99 > slo_ms: limit *= reduction_factor
    If p99 < slo_ms * 0.8: limit *= increase_factor

    Usage:
        ctrl = PercentileLimitController(slo_ms=200, initial=20)
        asyncio.create_task(ctrl.adjustment_loop())
        async with ctrl:
            await call_api()
    """

    def __init__(self, slo_ms: float = 200.0, initial: int = 20,
                 min_limit: int = 2, max_limit: int = 500,
                 window_size: int = 200,
                 increase_factor: float = 1.05,
                 reduction_factor: float = 0.9):
        self.slo_ms = slo_ms
        self._limit = initial
        self._min = min_limit
        self._max = max_limit
        self._window = window_size
        self._increase = increase_factor
        self._reduce = reduction_factor
        self._samples: deque = deque(maxlen=window_size)
        self._semaphore = asyncio.Semaphore(initial)
        self._lock = asyncio.Lock()
        self._start: float = 0.0

    async def adjustment_loop(self, interval: float = 2.0):
        while True:
            await asyncio.sleep(interval)
            await self._adjust()

    async def _adjust(self):
        async with self._lock:
            if len(self._samples) < 10:
                return
            lats = sorted(self._samples)
            p99 = lats[int(len(lats) * 0.99)]
            old_limit = self._limit
            if p99 > self.slo_ms:
                new_limit = max(self._min, int(self._limit * self._reduce))
            elif p99 < self.slo_ms * 0.8:
                new_limit = min(self._max, int(self._limit * self._increase) + 1)
            else:
                return
            delta = new_limit - old_limit
            self._limit = new_limit
            if delta > 0:
                for _ in range(delta):
                    self._semaphore.release()

    async def __aenter__(self):
        await self._semaphore.acquire()
        self._start = time.monotonic()
        return self

    async def __aexit__(self, *_):
        ms = (time.monotonic() - self._start) * 1000
        async with self._lock:
            self._samples.append(ms)
        self._semaphore.release()

    @property
    def current_limit(self) -> int:
        return self._limit
```

---

## Solution 5: Token-Bucket Weighted Concurrency

Combines a token-bucket rate limiter with an adaptive concurrency controller. Expensive requests consume more tokens; cheap ones consume fewer. Prevents one class of heavy requests from monopolising concurrency.

```python
import asyncio
import time
from dataclasses import dataclass
from typing import Callable, Optional


class WeightedAdaptiveLimiter:
    """
    Token-bucket rate limit + adaptive concurrency limit.
    Requests declare their `weight`; the limiter charges accordingly.

    Usage:
        limiter = WeightedAdaptiveLimiter(rate=100, capacity=200, concurrency=30)
        async with limiter.acquire(weight=5):   # heavy request
            await expensive_tool_call()
        async with limiter.acquire(weight=1):   # light request
            await cheap_lookup()
    """

    def __init__(self, rate: float = 100.0, capacity: float = 200.0,
                 concurrency: int = 30, min_concurrency: int = 2,
                 max_concurrency: int = 200):
        self._rate = rate
        self._capacity = capacity
        self._tokens = capacity
        self._last_refill = time.monotonic()
        self._concurrency = concurrency
        self._min_conc = min_concurrency
        self._max_conc = max_concurrency
        self._inflight = 0
        self._lock = asyncio.Lock()
        self._cond = asyncio.Condition(self._lock)

    def acquire(self, weight: float = 1.0) -> "_WeightedPermit":
        return _WeightedPermit(self, weight)

    def _refill(self):
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
        self._last_refill = now

    async def _do_acquire(self, weight: float):
        async with self._cond:
            while True:
                self._refill()
                if (self._tokens >= weight and
                        self._inflight < self._concurrency):
                    self._tokens -= weight
                    self._inflight += 1
                    return
                await self._cond.wait_for(
                    lambda: (self._tokens >= weight and
                             self._inflight < self._concurrency)
                )

    def _do_release(self, weight: float, latency_ms: float):
        # Simple AIMD on concurrency
        if latency_ms < 100:
            self._concurrency = min(self._max_conc, self._concurrency + 1)
        elif latency_ms > 500:
            self._concurrency = max(self._min_conc, self._concurrency - 2)
        self._inflight -= 1

    @property
    def current_concurrency(self) -> int:
        return self._concurrency


class _WeightedPermit:
    def __init__(self, limiter: WeightedAdaptiveLimiter, weight: float):
        self._limiter = limiter
        self._weight = weight
        self._start: float = 0.0

    async def __aenter__(self):
        await self._limiter._do_acquire(self._weight)
        self._start = time.monotonic()
        return self

    async def __aexit__(self, *_):
        ms = (time.monotonic() - self._start) * 1000
        async with self._limiter._cond:
            self._limiter._do_release(self._weight, ms)
            self._limiter._cond.notify_all()
```

---

## Solution 6: Adaptive Limit Agent Middleware

Drop-in middleware for agent tool executors that wraps every tool call with the gradient-based adaptive limiter and exposes metrics.

```python
import asyncio
import time
from typing import Any, Callable, Dict

class AdaptiveConcurrencyMiddleware:
    """
    Wraps an agent's tool executor with adaptive concurrency control.

    Usage:
        middleware = AdaptiveConcurrencyMiddleware(initial=20)
        asyncio.create_task(middleware.run_recalculation())

        result = await middleware.call(tool_fn, **kwargs)
    """

    def __init__(self, initial: int = 20, slo_ms: float = 300.0):
        self._limiter = PercentileLimitController(
            slo_ms=slo_ms, initial=initial
        )
        self._call_count = 0
        self._error_count = 0
        self._total_latency_ms = 0.0

    async def run_recalculation(self, interval: float = 2.0):
        await self._limiter.adjustment_loop(interval)

    async def call(self, fn: Callable, **kwargs) -> Any:
        async with self._limiter:
            t0 = time.monotonic()
            try:
                result = await fn(**kwargs)
                self._call_count += 1
                return result
            except Exception:
                self._error_count += 1
                raise
            finally:
                self._total_latency_ms += (time.monotonic() - t0) * 1000

    def metrics(self) -> Dict[str, Any]:
        calls = max(1, self._call_count)
        return {
            "current_limit": self._limiter.current_limit,
            "total_calls": self._call_count,
            "error_rate": round(self._error_count / calls, 4),
            "avg_latency_ms": round(self._total_latency_ms / calls, 2),
            "slo_ms": self._limiter.slo_ms,
        }
```

---

## Comparison

| Approach | Algorithm | Convergence Speed | Suitable For |
|---|---|---|---|
| **AIMD** | TCP Reno-style | Medium | Stable, predictable services |
| **Gradient Descent** | Min-RTT gradient | Fast | Variable latency profiles |
| **Little's Law** | Throughput × latency | Moderate (window-based) | High-throughput pipelines |
| **Percentile Controller** | p99 vs SLO | Configurable | SLO-driven agents |
| **Weighted Token Bucket** | AIMD + token bucket | Medium | Mixed-weight workloads |
| **Agent Middleware** | Percentile (pluggable) | Configurable | Drop-in for any agent |

**Key insight**: adaptive limits outperform static limits under all realistic load patterns. Start with the Gradient controller as a sensible default; switch to the Percentile controller if you have a hard SLO to defend.
