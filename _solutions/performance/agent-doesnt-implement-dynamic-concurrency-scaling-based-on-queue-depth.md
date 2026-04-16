---
title: "Agent Doesn't Implement Dynamic Concurrency Scaling Based on Queue Depth"
description: "Agents with a fixed concurrency limit either under-utilize available capacity when queues are shallow (a limit of 5 concurrent tool calls wastes throughput when 20 tasks are waiting) or overwhelm downstream services when queues are deep (the same limit of 5 is too aggressive for a degraded dependency). Implement dynamic concurrency scaling that adjusts the concurrency limit in real time based on queue depth, error rates, and downstream latency signals."
date: 2026-04-16
difficulty: advanced
category: performance
slug: agent-doesnt-implement-dynamic-concurrency-scaling-based-on-queue-depth
tags: [dynamic-concurrency, queue-depth-scaling, adaptive-throughput, concurrency-control, backpressure, throughput-optimization]
symptoms:
  - "Fixed concurrency limit wastes throughput when the queue has many waiting tasks"
  - "Same fixed limit floods a degraded service during incidents"
  - "No feedback loop between queue depth and concurrency — limit never adjusts"
  - "High queue depth is observed but concurrency is not increased to drain it faster"
  - "Error rate spikes cause the same concurrency that was fine in steady state"
---

## Why This Happens

Concurrency limits are set at deployment time based on a steady-state estimate and never revisited. During peak load, the queue grows and the fixed limit is the bottleneck — more concurrent tasks would drain the queue faster without overwhelming the downstream. During degradation, the same fixed limit generates too many simultaneous failures, amplifying the incident. Dynamic concurrency scaling treats the concurrency limit as a control variable that responds to observable signals: when the queue is deep and error rates are low, increase concurrency; when error rates rise or latency spikes, decrease it. This is the same principle as TCP congestion control applied to task dispatch.

## Solution 1: Concurrency Signal

```python
import time
from dataclasses import dataclass
from typing import Optional


@dataclass
class ConcurrencySignal:
    queue_depth: int
    active_workers: int
    error_rate_1m: float         # errors / total in last minute
    p95_latency_ms: float        # p95 task latency in last 5 minutes
    recorded_at: float = None

    def __post_init__(self):
        if self.recorded_at is None:
            self.recorded_at = time.time()

    def utilization(self) -> float:
        return self.active_workers / max(self.active_workers + self.queue_depth, 1)
```

## Solution 2: Concurrency Policy

```python
from dataclasses import dataclass


@dataclass
class ConcurrencyPolicy:
    min_concurrency: int = 2
    max_concurrency: int = 50
    initial_concurrency: int = 10

    # Scale up signals
    queue_depth_scale_threshold: int = 5      # scale up when queue > this
    scale_up_step: int = 2                    # increase by this each interval

    # Scale down signals
    error_rate_scale_down_threshold: float = 0.05   # scale down when > 5% errors
    latency_scale_down_threshold_ms: float = 5000.0  # scale down when p95 > this
    scale_down_step: int = 1

    # Stability
    adjustment_interval_seconds: float = 10.0   # how often to re-evaluate
    cooldown_seconds: float = 30.0              # minimum time between adjustments
```

## Solution 3: Dynamic Concurrency Controller

```python
import asyncio
import threading
import time
from typing import Callable, Optional


class DynamicConcurrencyController:
    """
    Adjusts an asyncio.Semaphore capacity based on real-time signals.
    Scales up when queue is deep and healthy; scales down on errors or latency.
    """

    def __init__(
        self,
        policy: ConcurrencyPolicy,
        signal_fn: Callable[[], ConcurrencySignal],
    ):
        self._policy = policy
        self._signal_fn = signal_fn
        self._current = policy.initial_concurrency
        self._semaphore = asyncio.Semaphore(policy.initial_concurrency)
        self._last_adjustment = 0.0
        self._scale_ups = 0
        self._scale_downs = 0
        self._history: list = []
        self._lock = threading.Lock()

    def _compute_target(self, signal: ConcurrencySignal) -> int:
        target = self._current

        # Scale down conditions (take priority)
        if signal.error_rate_1m > self._policy.error_rate_scale_down_threshold:
            target -= self._policy.scale_down_step
        elif signal.p95_latency_ms > self._policy.latency_scale_down_threshold_ms:
            target -= self._policy.scale_down_step
        # Scale up conditions
        elif signal.queue_depth > self._policy.queue_depth_scale_threshold:
            target += self._policy.scale_up_step

        return max(self._policy.min_concurrency, min(self._policy.max_concurrency, target))

    async def _adjust_semaphore(self, new_target: int) -> None:
        """Adjust semaphore capacity by releasing or acquiring permits."""
        delta = new_target - self._current
        if delta > 0:
            for _ in range(delta):
                self._semaphore._value += 1   # increase capacity
                self._scale_ups += 1
        elif delta < 0:
            # Acquire permits to reduce effective capacity
            for _ in range(-delta):
                try:
                    await asyncio.wait_for(self._semaphore.acquire(), timeout=0.1)
                    self._scale_downs += 1
                except asyncio.TimeoutError:
                    break   # can't reduce further — permits in use
        self._current = new_target

    async def evaluate_and_adjust(self) -> Optional[dict]:
        now = time.time()
        with self._lock:
            if now - self._last_adjustment < self._policy.cooldown_seconds:
                return None
            self._last_adjustment = now

        signal = self._signal_fn()
        new_target = self._compute_target(signal)

        if new_target != self._current:
            old = self._current
            await self._adjust_semaphore(new_target)
            event = {
                "ts": now,
                "old_concurrency": old,
                "new_concurrency": self._current,
                "queue_depth": signal.queue_depth,
                "error_rate": signal.error_rate_1m,
                "p95_latency_ms": signal.p95_latency_ms,
            }
            with self._lock:
                self._history.append(event)
                if len(self._history) > 100:
                    self._history.pop(0)
            return event
        return None

    async def acquire(self) -> None:
        await self._semaphore.acquire()

    def release(self) -> None:
        self._semaphore.release()

    def current_concurrency(self) -> int:
        return self._current

    def stats(self) -> dict:
        return {
            "current_concurrency": self._current,
            "scale_ups": self._scale_ups,
            "scale_downs": self._scale_downs,
            "adjustment_history": self._history[-10:],
        }
```

## Solution 4: Queue Depth Monitor

```python
import asyncio
import threading
import time
from collections import deque
from typing import Deque, Tuple


class QueueDepthMonitor:
    """
    Tracks queue depth and task completion latencies for use as
    concurrency scaling signals.
    """

    def __init__(self, window_seconds: float = 300.0):
        self._window = window_seconds
        self._queue_depth = 0
        self._active = 0
        self._latencies: Deque[Tuple[float, float]] = deque()  # (ts, latency_ms)
        self._errors: Deque[float] = deque()
        self._total: Deque[float] = deque()
        self._lock = threading.Lock()

    def task_enqueued(self) -> None:
        with self._lock:
            self._queue_depth += 1

    def task_started(self) -> None:
        with self._lock:
            self._queue_depth = max(0, self._queue_depth - 1)
            self._active += 1

    def task_completed(self, latency_ms: float, is_error: bool = False) -> None:
        now = time.time()
        with self._lock:
            self._active = max(0, self._active - 1)
            self._latencies.append((now, latency_ms))
            self._total.append(now)
            if is_error:
                self._errors.append(now)
            cutoff = now - self._window
            while self._latencies and self._latencies[0][0] < cutoff:
                self._latencies.popleft()
            while self._total and self._total[0] < cutoff:
                self._total.popleft()
            while self._errors and self._errors[0] < cutoff:
                self._errors.popleft()

    def current_signal(self) -> ConcurrencySignal:
        now = time.time()
        cutoff_1m = now - 60
        with self._lock:
            lats_1m = [lat for ts, lat in self._latencies if ts >= cutoff_1m]
            total_1m = sum(1 for ts in self._total if ts >= cutoff_1m)
            errors_1m = sum(1 for ts in self._errors if ts >= cutoff_1m)
            error_rate = errors_1m / max(total_1m, 1)
            all_lats = sorted(lat for _, lat in self._latencies)
            p95 = all_lats[min(int(len(all_lats) * 0.95), len(all_lats) - 1)] if all_lats else 0.0
            return ConcurrencySignal(
                queue_depth=self._queue_depth,
                active_workers=self._active,
                error_rate_1m=error_rate,
                p95_latency_ms=p95,
            )
```

## Solution 5: Adaptive Task Dispatcher

```python
import asyncio
import time
from typing import Any, Callable


class AdaptiveTaskDispatcher:
    """
    Dispatches tasks through a dynamic concurrency controller,
    recording queue and completion metrics for scaling signals.
    """

    def __init__(
        self,
        controller: DynamicConcurrencyController,
        monitor: QueueDepthMonitor,
        adjustment_interval: float = 10.0,
    ):
        self._controller = controller
        self._monitor = monitor
        self._interval = adjustment_interval
        self._running = False

    async def dispatch(self, task_fn: Callable, *args: Any, **kwargs: Any) -> Any:
        self._monitor.task_enqueued()
        await self._controller.acquire()
        self._monitor.task_started()
        start = time.time()
        is_error = False
        try:
            result = await task_fn(*args, **kwargs)
            return result
        except Exception:
            is_error = True
            raise
        finally:
            latency_ms = round((time.time() - start) * 1000, 2)
            self._monitor.task_completed(latency_ms, is_error)
            self._controller.release()

    async def run_scaler(self) -> None:
        self._running = True
        while self._running:
            await asyncio.sleep(self._interval)
            await self._controller.evaluate_and_adjust()

    def stop_scaler(self) -> None:
        self._running = False
```

## Solution 6: Dynamic Concurrency Dashboard

```python
import time


class DynamicConcurrencyDashboard:
    """
    Combines controller state, queue metrics, and scaling history
    into a single throughput optimization view.
    """

    def __init__(
        self,
        controller: DynamicConcurrencyController,
        monitor: QueueDepthMonitor,
    ):
        self._controller = controller
        self._monitor = monitor

    def render(self) -> dict:
        signal = self._monitor.current_signal()
        return {
            "generated_at": time.time(),
            "concurrency": self._controller.stats(),
            "current_signal": {
                "queue_depth": signal.queue_depth,
                "active_workers": signal.active_workers,
                "error_rate_1m": signal.error_rate_1m,
                "p95_latency_ms": signal.p95_latency_ms,
                "utilization": round(signal.utilization(), 4),
            },
        }
```

## Comparison

| Approach | Queue-Based Scale Up | Error-Based Scale Down | Latency-Based Scale Down | Semaphore Adjustment | Dashboard |
|---|---|---|---|---|---|
| DynamicConcurrencyController | Yes | Yes | Yes | Yes (delta) | No |
| QueueDepthMonitor | Yes (depth) | Yes (rate) | Yes (p95) | No | No |
| AdaptiveTaskDispatcher | Via controller | Via monitor | Via monitor | Via controller | No |
| DynamicConcurrencyDashboard | No | No | No | No | Yes |

**Best for production**: Set `min_concurrency=2` to ensure tasks can always make progress even under extreme degradation, and `max_concurrency=50` as an upper bound that prevents runaway scaling. Use `cooldown_seconds=30` to prevent oscillation: rapid alternation between scale-up and scale-down creates the same instability it tries to prevent. Monitor `adjustment_history` in the dashboard — a pattern of constant scaling up and down without stabilization indicates the scaling signal is too sensitive and the `scale_up_step` or `queue_depth_scale_threshold` needs to be increased.
