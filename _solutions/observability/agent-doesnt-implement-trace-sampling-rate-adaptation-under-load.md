---
title: "Agent Doesn't Implement Trace Sampling Rate Adaptation Under Load"
description: "Agents that emit traces at a fixed sampling rate flood observability backends during traffic spikes, causing ingestion throttling, dropped spans, and cost overruns — while over-sampling quiet periods wastes budget. Implement adaptive trace sampling that monitors incoming request rate and backend ingestion health, and adjusts the sampling rate dynamically to maintain trace volume within a configurable ceiling."
date: 2026-04-16
difficulty: advanced
category: observability
slug: agent-doesnt-implement-trace-sampling-rate-adaptation-under-load
tags: [trace-sampling, adaptive-sampling, observability-cost, span-volume, head-based-sampling, load-adaptive]
symptoms:
  - "Observability backend drops spans during traffic spikes because ingest rate exceeds quota"
  - "Fixed 100% sampling rate causes 10× cost overrun during load tests"
  - "Quiet-period traces are sampled at the same rate as peak-period traces — budget wasted"
  - "No feedback loop between trace backend health and sampling decisions"
  - "Error traces are downsampled at the same rate as success traces during incidents"
---

## Why This Happens

Head-based sampling with a fixed rate is stateless — it makes no decision about whether the backend can absorb the current volume. During a traffic spike, a 10% sampling rate that was calibrated for baseline traffic becomes 100% effective if the spike is 10×, flooding the backend. Adaptive sampling requires a control loop: measure current trace throughput, compare it against a ceiling, and adjust the sampling probability up or down. The adjustment must be smooth (not oscillating) and must always keep error traces at a higher sampling rate than success traces regardless of load.

## Solution 1: Trace Throughput Monitor

```python
import time
from collections import deque
from dataclasses import dataclass, field
from threading import Lock
from typing import Deque, Tuple


@dataclass
class ThroughputSample:
    count: int
    recorded_at: float = field(default_factory=time.time)


class TraceThroughputMonitor:
    """
    Counts trace decisions (sampled + dropped) in a sliding window
    and reports the current traces-per-second rate.
    """

    def __init__(self, window_seconds: float = 60.0):
        self._window = window_seconds
        self._samples: Deque[ThroughputSample] = deque()
        self._lock = Lock()

    def record(self, count: int = 1) -> None:
        with self._lock:
            self._samples.append(ThroughputSample(count=count))

    def _recent(self) -> list:
        cutoff = time.time() - self._window
        return [s for s in self._samples if s.recorded_at >= cutoff]

    def rate_per_second(self) -> float:
        with self._lock:
            recent = self._recent()
        if not recent:
            return 0.0
        total = sum(s.count for s in recent)
        return round(total / self._window, 2)

    def total_in_window(self) -> int:
        with self._lock:
            return sum(s.count for s in self._recent())
```

## Solution 2: Adaptive Sampling Rate Controller

```python
import time
from dataclasses import dataclass
from threading import Lock


@dataclass
class SamplingRateConfig:
    target_traces_per_second: float = 100.0    # ceiling for the backend
    min_rate: float = 0.01                      # never go below 1%
    max_rate: float = 1.00                      # never exceed 100%
    error_rate_floor: float = 0.50             # errors always sampled >= 50%
    adjustment_step: float = 0.05              # rate change per control cycle
    control_interval_seconds: float = 10.0     # how often to adjust


class AdaptiveSamplingRateController:
    """
    Adjusts sampling probability based on observed trace throughput.
    Increases rate when throughput is below target, decreases when above.
    Uses a smooth step adjustment to avoid oscillation.
    """

    def __init__(
        self,
        config: SamplingRateConfig,
        monitor: TraceThroughputMonitor,
    ):
        self._config = config
        self._monitor = monitor
        self._current_rate = config.max_rate
        self._last_adjustment = time.time()
        self._lock = Lock()

    def adjust(self) -> float:
        now = time.time()
        with self._lock:
            if now - self._last_adjustment < self._config.control_interval_seconds:
                return self._current_rate
            self._last_adjustment = now

            actual_rate = self._monitor.rate_per_second()
            target = self._config.target_traces_per_second

            if actual_rate > target * 1.10:
                # Above target — reduce
                self._current_rate = max(
                    self._config.min_rate,
                    self._current_rate - self._config.adjustment_step,
                )
            elif actual_rate < target * 0.80:
                # Below target — increase
                self._current_rate = min(
                    self._config.max_rate,
                    self._current_rate + self._config.adjustment_step,
                )

            return self._current_rate

    def current_rate(self) -> float:
        with self._lock:
            return self._current_rate
```

## Solution 3: Priority Sampling Decision Engine

```python
import random
from enum import Enum
from typing import Optional


class TraceOutcome(str, Enum):
    SUCCESS = "success"
    ERROR = "error"
    SLOW = "slow"        # latency exceeded threshold
    SAMPLED_BY_RULE = "sampled_by_rule"


class PrioritySamplingDecisionEngine:
    """
    Makes per-trace sampling decisions with priority overrides:
    - Errors are always sampled at max(base_rate, error_rate_floor)
    - Slow traces are sampled at max(base_rate, 0.25)
    - Success traces use the base adaptive rate
    """

    def __init__(
        self,
        controller: AdaptiveSamplingRateController,
        slow_threshold_ms: float = 5000.0,
    ):
        self._controller = controller
        self._slow_threshold = slow_threshold_ms

    def should_sample(
        self,
        outcome: TraceOutcome = TraceOutcome.SUCCESS,
        latency_ms: Optional[float] = None,
        force_sample: bool = False,
    ) -> bool:
        if force_sample:
            return True

        base_rate = self._controller.adjust()
        config = self._controller._config

        if outcome == TraceOutcome.ERROR:
            effective_rate = max(base_rate, config.error_rate_floor)
        elif outcome == TraceOutcome.SLOW or (
            latency_ms is not None and latency_ms >= self._slow_threshold
        ):
            effective_rate = max(base_rate, 0.25)
        else:
            effective_rate = base_rate

        return random.random() < effective_rate
```

## Solution 4: Adaptive Trace Sampler

```python
import time
from typing import Any, Optional


class AdaptiveTraceSampler:
    """
    Entry point for all trace sampling decisions.
    Records throughput after each decision to feed the control loop.
    """

    def __init__(
        self,
        engine: PrioritySamplingDecisionEngine,
        monitor: TraceThroughputMonitor,
    ):
        self._engine = engine
        self._monitor = monitor
        self._sampled_count = 0
        self._dropped_count = 0

    def sample(
        self,
        outcome: TraceOutcome = TraceOutcome.SUCCESS,
        latency_ms: Optional[float] = None,
        force_sample: bool = False,
    ) -> bool:
        self._monitor.record(1)
        decision = self._engine.should_sample(outcome, latency_ms, force_sample)
        if decision:
            self._sampled_count += 1
        else:
            self._dropped_count += 1
        return decision

    def stats(self) -> dict:
        total = self._sampled_count + self._dropped_count
        return {
            "sampled": self._sampled_count,
            "dropped": self._dropped_count,
            "total": total,
            "effective_rate": round(
                self._sampled_count / total if total else 0.0, 4
            ),
            "current_target_rate": self._engine._controller.current_rate(),
        }
```

## Solution 5: Backend Health Feedback Adjuster

```python
import time
from threading import Lock
from typing import Optional


class BackendHealthFeedbackAdjuster:
    """
    Receives signals from the trace backend (throttle errors, quota warnings)
    and forces the sampling rate down immediately, bypassing the gradual
    control loop. Recovers gradually once signals clear.
    """

    def __init__(
        self,
        controller: AdaptiveSamplingRateController,
        emergency_rate: float = 0.05,
        recovery_step: float = 0.02,
        recovery_interval_seconds: float = 30.0,
    ):
        self._controller = controller
        self._emergency_rate = emergency_rate
        self._recovery_step = recovery_step
        self._recovery_interval = recovery_interval_seconds
        self._throttled = False
        self._last_recovery = time.time()
        self._lock = Lock()

    def on_backend_throttle(self) -> None:
        with self._lock:
            self._throttled = True
            self._controller._current_rate = self._emergency_rate

    def on_backend_healthy(self) -> None:
        now = time.time()
        with self._lock:
            if not self._throttled:
                return
            if now - self._last_recovery < self._recovery_interval:
                return
            config = self._controller._config
            self._controller._current_rate = min(
                config.max_rate,
                self._controller._current_rate + self._recovery_step,
            )
            self._last_recovery = now
            if self._controller._current_rate >= config.max_rate * 0.9:
                self._throttled = False

    def is_throttled(self) -> bool:
        with self._lock:
            return self._throttled
```

## Solution 6: Sampling Rate Dashboard

```python
import time
from typing import Optional


class TraceSamplingRateDashboard:
    """
    Renders current sampling rates, throughput, backend health,
    and sampler statistics for operational visibility.
    """

    def __init__(
        self,
        sampler: AdaptiveTraceSampler,
        monitor: TraceThroughputMonitor,
        controller: AdaptiveSamplingRateController,
        adjuster: Optional[BackendHealthFeedbackAdjuster] = None,
    ):
        self._sampler = sampler
        self._monitor = monitor
        self._controller = controller
        self._adjuster = adjuster

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "sampling": {
                "current_rate": self._controller.current_rate(),
                "target_tps": self._controller._config.target_traces_per_second,
                "actual_tps": self._monitor.rate_per_second(),
                "backend_throttled": self._adjuster.is_throttled() if self._adjuster else False,
            },
            "counters": self._sampler.stats(),
            "throughput_window": {
                "window_seconds": self._monitor._window,
                "total_in_window": self._monitor.total_in_window(),
            },
        }
```

## Comparison

| Approach | Throughput Measurement | Rate Adjustment | Priority Sampling | Backend Feedback | Dashboard |
|---|---|---|---|---|---|
| TraceThroughputMonitor | Yes (sliding window) | No | No | No | No |
| AdaptiveSamplingRateController | Via monitor | Yes (step) | No | No | No |
| PrioritySamplingDecisionEngine | No | Via controller | Yes (error/slow) | No | No |
| AdaptiveTraceSampler | Via monitor | Via engine | Via engine | No | No |
| BackendHealthFeedbackAdjuster | No | Emergency drop | No | Yes | No |
| TraceSamplingRateDashboard | No | No | No | No | Yes |

**Best for production**: Set `target_traces_per_second` to 80% of your backend's ingestion quota to leave headroom for burst. Use `error_rate_floor=0.50` as an absolute minimum — during incidents, you need error traces even if success traces are being heavily downsampled. Wire `BackendHealthFeedbackAdjuster.on_backend_throttle()` to your HTTP client's 429 handler for the trace backend so the rate drops within one request of receiving a throttle signal. Monitor `effective_rate` from `AdaptiveTraceSampler.stats()`: if it stays persistently below 0.10, the traffic volume has grown past the backend tier and quota needs to be increased.
