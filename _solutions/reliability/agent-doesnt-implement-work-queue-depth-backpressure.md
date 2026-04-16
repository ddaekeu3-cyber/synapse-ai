---
title: "Agent Doesn't Implement Work Queue Depth Backpressure"
description: "Agents that accept every incoming request regardless of their internal queue depth allow unbounded memory growth and cascading latency: queued work accumulates, processing time per request degrades, and the entire service eventually exhausts memory or becomes too slow to be useful. Implement work queue depth backpressure that measures current queue depth, rejects new requests above a high-water mark, and signals upstream callers to slow down."
date: 2026-04-16
difficulty: intermediate
category: reliability
slug: agent-doesnt-implement-work-queue-depth-backpressure
tags: [backpressure, queue-depth, flow-control, overload-protection, high-water-mark, rate-limiting]
symptoms:
  - "Memory usage climbs steadily under load until the process is OOM-killed"
  - "Request latency grows proportionally with queue depth — 500ms at idle, 30s under load"
  - "No signal to upstream callers that the agent is overloaded"
  - "Bursts of requests during peak traffic cause multi-minute processing queues"
  - "Queue depth is never measured — only individual request latency is tracked"
---

## Why This Happens

Infinite queues defer failures instead of preventing them. When work arrives faster than it is processed, the queue grows without bound, memory fills up, and every request in the queue waits longer. Backpressure inverts the control: instead of always saying yes and hoping to catch up, the agent measures its own queue depth and says no — with a clear error — when the queue is too deep. Upstream callers receive a 429 / backpressure signal and can retry later or route to another instance, rather than waiting for a response that may take minutes.

## Solution 1: Queue Depth Config

```python
from dataclasses import dataclass


@dataclass
class BackpressureConfig:
    low_water_mark: int = 10       # below this: accept freely
    high_water_mark: int = 50      # above this: reject new work
    shedding_strategy: str = "reject"   # "reject" | "drop_oldest" | "drop_lowest_priority"
    backpressure_status_code: int = 429
    include_retry_after_seconds: float = 5.0
```

## Solution 2: Queue Depth Gauge

```python
import time
from threading import Lock
from typing import Optional


class QueueDepthGauge:
    """
    Thread-safe counter tracking the number of work items currently
    in-flight or waiting in the agent's processing queue.
    """

    def __init__(self):
        self._depth = 0
        self._peak = 0
        self._total_accepted = 0
        self._total_rejected = 0
        self._lock = Lock()
        self._last_rejection_at: Optional[float] = None

    def increment(self) -> None:
        with self._lock:
            self._depth += 1
            self._total_accepted += 1
            if self._depth > self._peak:
                self._peak = self._depth

    def decrement(self) -> None:
        with self._lock:
            self._depth = max(0, self._depth - 1)

    def record_rejection(self) -> None:
        with self._lock:
            self._total_rejected += 1
            self._last_rejection_at = time.time()

    def depth(self) -> int:
        with self._lock:
            return self._depth

    def stats(self) -> dict:
        with self._lock:
            total = max(self._total_accepted + self._total_rejected, 1)
            return {
                "current_depth": self._depth,
                "peak_depth": self._peak,
                "total_accepted": self._total_accepted,
                "total_rejected": self._total_rejected,
                "rejection_rate": round(self._total_rejected / total, 4),
                "last_rejection_at": self._last_rejection_at,
            }
```

## Solution 3: Backpressure Guard

```python
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class AdmissionDecision(str, Enum):
    ACCEPT = "accept"
    REJECT = "reject"
    SHED = "shed"


@dataclass
class AdmissionResult:
    decision: AdmissionDecision
    current_depth: int
    high_water_mark: int
    retry_after_seconds: Optional[float] = None
    reason: str = ""


class BackpressureGuard:
    """
    Makes an admission decision for each incoming request
    based on the current queue depth and configured water marks.
    """

    def __init__(self, config: BackpressureConfig, gauge: QueueDepthGauge):
        self._config = config
        self._gauge = gauge

    def check(self, priority: int = 0) -> AdmissionResult:
        depth = self._gauge.depth()

        if depth < self._config.low_water_mark:
            return AdmissionResult(
                decision=AdmissionDecision.ACCEPT,
                current_depth=depth,
                high_water_mark=self._config.high_water_mark,
            )

        if depth >= self._config.high_water_mark:
            self._gauge.record_rejection()
            if self._config.shedding_strategy == "reject":
                return AdmissionResult(
                    decision=AdmissionDecision.REJECT,
                    current_depth=depth,
                    high_water_mark=self._config.high_water_mark,
                    retry_after_seconds=self._config.include_retry_after_seconds,
                    reason=f"queue depth {depth} exceeds high-water mark {self._config.high_water_mark}",
                )
            return AdmissionResult(
                decision=AdmissionDecision.SHED,
                current_depth=depth,
                high_water_mark=self._config.high_water_mark,
                retry_after_seconds=self._config.include_retry_after_seconds,
                reason="load shedding active",
            )

        # Between low and high water mark — accept but signal pressure
        return AdmissionResult(
            decision=AdmissionDecision.ACCEPT,
            current_depth=depth,
            high_water_mark=self._config.high_water_mark,
            reason="elevated queue depth",
        )
```

## Solution 4: Backpressure-Aware Request Executor

```python
import asyncio
import contextlib
import time
from typing import Any, Callable


class BackpressureAwareExecutor:
    """
    Wraps async work items with queue depth tracking.
    Applies the backpressure guard before accepting work,
    and decrements the gauge when work completes or fails.
    """

    def __init__(self, guard: BackpressureGuard, gauge: QueueDepthGauge):
        self._guard = guard
        self._gauge = gauge

    @contextlib.asynccontextmanager
    async def _tracked(self):
        self._gauge.increment()
        try:
            yield
        finally:
            self._gauge.decrement()

    async def submit(
        self,
        fn: Callable,
        *args: Any,
        priority: int = 0,
        **kwargs: Any,
    ) -> Any:
        """
        Raises BackpressureError if the request is rejected.
        Otherwise executes fn with queue tracking.
        """
        decision = self._guard.check(priority=priority)

        if decision.decision == AdmissionDecision.REJECT:
            raise BackpressureError(
                depth=decision.current_depth,
                high_water_mark=decision.high_water_mark,
                retry_after=decision.retry_after_seconds or 5.0,
            )

        if decision.decision == AdmissionDecision.SHED:
            raise BackpressureError(
                depth=decision.current_depth,
                high_water_mark=decision.high_water_mark,
                retry_after=decision.retry_after_seconds or 5.0,
            )

        async with self._tracked():
            return await fn(*args, **kwargs)


class BackpressureError(Exception):
    def __init__(self, depth: int, high_water_mark: int, retry_after: float):
        self.depth = depth
        self.high_water_mark = high_water_mark
        self.retry_after = retry_after
        super().__init__(
            f"backpressure: queue depth {depth}/{high_water_mark}, "
            f"retry after {retry_after}s"
        )
```

## Solution 5: Adaptive Water Mark Adjuster

```python
import time
from typing import List, Tuple


class AdaptiveWaterMarkAdjuster:
    """
    Observes queue depth trends over time and adjusts the high-water mark
    dynamically: tightens when average depth is high (protecting against OOM),
    loosens when average depth is low (allowing more throughput).
    """

    def __init__(
        self,
        config: BackpressureConfig,
        gauge: QueueDepthGauge,
        adjustment_interval_seconds: float = 60.0,
        tighten_threshold_pct: float = 0.80,   # tighten if avg > 80% of HWM
        loosen_threshold_pct: float = 0.30,    # loosen if avg < 30% of HWM
        step_size: int = 5,
        absolute_min: int = 10,
        absolute_max: int = 500,
    ):
        self._config = config
        self._gauge = gauge
        self._interval = adjustment_interval_seconds
        self._tighten_pct = tighten_threshold_pct
        self._loosen_pct = loosen_threshold_pct
        self._step = step_size
        self._min = absolute_min
        self._max = absolute_max
        self._samples: List[Tuple[float, int]] = []
        self._last_adjusted = time.time()

    def sample(self) -> None:
        self._samples.append((time.time(), self._gauge.depth()))
        cutoff = time.time() - self._interval * 2
        self._samples = [(t, d) for t, d in self._samples if t >= cutoff]

    def maybe_adjust(self) -> Optional[int]:
        """Returns the new high_water_mark if adjusted, else None."""
        if time.time() - self._last_adjusted < self._interval:
            return None
        if not self._samples:
            return None

        window_cutoff = time.time() - self._interval
        recent = [d for t, d in self._samples if t >= window_cutoff]
        if not recent:
            return None

        avg_depth = sum(recent) / len(recent)
        hwm = self._config.high_water_mark

        if avg_depth > hwm * self._tighten_pct:
            new_hwm = max(self._min, hwm - self._step)
        elif avg_depth < hwm * self._loosen_pct:
            new_hwm = min(self._max, hwm + self._step)
        else:
            return None

        self._config.high_water_mark = new_hwm
        self._last_adjusted = time.time()
        return new_hwm
```

## Solution 6: Backpressure Dashboard

```python
import time


class BackpressureDashboard:
    """
    Combines gauge stats, current water marks, and adjustment
    history into a single operational snapshot.
    """

    def __init__(
        self,
        config: BackpressureConfig,
        gauge: QueueDepthGauge,
        adjuster: AdaptiveWaterMarkAdjuster,
    ):
        self._config = config
        self._gauge = gauge
        self._adjuster = adjuster

    def render(self) -> dict:
        stats = self._gauge.stats()
        depth = stats["current_depth"]
        hwm = self._config.high_water_mark
        lwm = self._config.low_water_mark

        pressure_level = "normal"
        if depth >= hwm:
            pressure_level = "saturated"
        elif depth >= lwm:
            pressure_level = "elevated"

        return {
            "generated_at": time.time(),
            "queue_depth": depth,
            "low_water_mark": lwm,
            "high_water_mark": hwm,
            "pressure_level": pressure_level,
            "utilization_pct": round(depth / max(hwm, 1) * 100, 1),
            "gauge": stats,
        }
```

## Comparison

| Approach | Depth Measurement | Admission Control | Adaptive HWM | Async Tracking | Dashboard |
|---|---|---|---|---|---|
| QueueDepthGauge | Yes (thread-safe) | No | No | No | No |
| BackpressureGuard | Via gauge | Yes (reject/shed) | No | No | No |
| BackpressureAwareExecutor | Via gauge | Via guard | No | Yes (context mgr) | No |
| AdaptiveWaterMarkAdjuster | Via gauge | No | Yes | No | No |
| BackpressureDashboard | No | No | No | No | Yes |

**Best for production**: Set `high_water_mark` to the queue depth at which observed P99 latency exceeds your SLO — measure this under load test rather than guessing. Return HTTP 429 with a `Retry-After` header so upstream callers back off gracefully rather than retrying immediately and worsening the overload. Use `AdaptiveWaterMarkAdjuster` to track steady-state depth and tighten the mark before memory pressure occurs. Monitor `rejection_rate`: a sustained rate above 5% means the agent is consistently under-provisioned and needs horizontal scaling, not a higher water mark.
