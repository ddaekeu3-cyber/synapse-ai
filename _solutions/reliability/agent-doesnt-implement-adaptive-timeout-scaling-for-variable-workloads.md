---
title: "Agent Doesn't Implement Adaptive Timeout Scaling for Variable Workloads"
description: "Agents that use fixed timeouts for all tool calls fail in two directions: short timeouts cause spurious failures on legitimately slow operations; long timeouts cause agents to hang during actual outages. Implement adaptive timeout scaling that tracks per-tool latency percentiles and sets timeouts dynamically based on recent performance, expanding for slow tools and tightening when latency recovers."
date: 2026-04-16
difficulty: advanced
category: reliability
slug: agent-doesnt-implement-adaptive-timeout-scaling-for-variable-workloads
tags: [adaptive-timeout, latency-percentile, timeout-scaling, tool-reliability, dynamic-configuration, p99-timeout]
symptoms:
  - "Fixed 30-second timeout causes spurious failures on database queries during high load"
  - "Fixed 5-second timeout hangs the agent for 5 seconds during every provider outage"
  - "No relationship between observed latency and the timeout applied to each tool"
  - "Timeout configuration requires manual tuning after every infrastructure change"
  - "P99 latency spikes cause a wave of timeout failures before operators can adjust config"
---

## Why This Happens

Fixed timeouts are a guess made at development time. They do not adapt to load patterns, provider variability, or infrastructure changes. A query that normally completes in 200ms may legitimately take 4 seconds under peak load — a 3-second fixed timeout will fail it. Conversely, a tool that normally completes in 10 seconds will cause the agent to hang for the full 10 seconds during an outage if no tighter bound is applied. Adaptive timeouts use a sliding window of recent latency observations to set a timeout at a configurable percentile (e.g., P99 × 1.5), automatically tracking real performance.

## Solution 1: Latency Observation Window

```python
import time
from collections import deque
from dataclasses import dataclass, field
from threading import Lock
from typing import Deque, Optional, Tuple


@dataclass
class LatencyObservation:
    duration_ms: float
    recorded_at: float = field(default_factory=time.time)
    succeeded: bool = True


class LatencyObservationWindow:
    """
    Maintains a sliding time window of latency observations for a single tool.
    Provides percentile queries used to derive adaptive timeouts.
    """

    def __init__(self, window_seconds: float = 300.0, max_observations: int = 1000):
        self._window = window_seconds
        self._max = max_observations
        self._observations: Deque[LatencyObservation] = deque()
        self._lock = Lock()

    def record(self, duration_ms: float, succeeded: bool = True) -> None:
        with self._lock:
            self._observations.append(
                LatencyObservation(duration_ms=duration_ms, succeeded=succeeded)
            )
            if len(self._observations) > self._max:
                self._observations.popleft()

    def _recent(self) -> list:
        cutoff = time.time() - self._window
        return [o for o in self._observations if o.recorded_at >= cutoff]

    def percentile(self, pct: float) -> Optional[float]:
        recent = self._recent()
        if not recent:
            return None
        values = sorted(o.duration_ms for o in recent if o.succeeded)
        if not values:
            return None
        idx = min(int(len(values) * pct / 100.0), len(values) - 1)
        return round(values[idx], 2)

    def count(self) -> int:
        return len(self._recent())

    def success_rate(self) -> float:
        recent = self._recent()
        if not recent:
            return 1.0
        return sum(1 for o in recent if o.succeeded) / len(recent)
```

## Solution 2: Adaptive Timeout Policy

```python
from dataclasses import dataclass


@dataclass
class AdaptiveTimeoutPolicy:
    percentile: float = 99.0            # base timeout at this latency percentile
    multiplier: float = 1.5             # multiply percentile value by this
    min_timeout_ms: float = 500.0       # never go below this
    max_timeout_ms: float = 60_000.0    # never exceed this
    fallback_timeout_ms: float = 10_000.0  # used when no observations exist
    min_observations: int = 10          # require this many before adapting


class AdaptiveTimeoutCalculator:
    """
    Derives a timeout value from a latency observation window.
    Falls back to a configured default when insufficient observations exist.
    """

    def __init__(self, policy: AdaptiveTimeoutPolicy):
        self._policy = policy

    def calculate(self, window: LatencyObservationWindow) -> float:
        if window.count() < self._policy.min_observations:
            return self._policy.fallback_timeout_ms

        pct_value = window.percentile(self._policy.percentile)
        if pct_value is None:
            return self._policy.fallback_timeout_ms

        raw = pct_value * self._policy.multiplier
        clamped = max(self._policy.min_timeout_ms, min(self._policy.max_timeout_ms, raw))
        return round(clamped, 1)
```

## Solution 3: Per-Tool Adaptive Timeout Registry

```python
from typing import Dict, Optional


class PerToolAdaptiveTimeoutRegistry:
    """
    Maintains a latency window and computes adaptive timeouts for each tool.
    Records outcomes after each call to keep windows current.
    """

    def __init__(
        self,
        calculator: AdaptiveTimeoutCalculator,
        window_seconds: float = 300.0,
    ):
        self._calculator = calculator
        self._window_seconds = window_seconds
        self._windows: Dict[str, LatencyObservationWindow] = {}

    def _get_window(self, tool_name: str) -> LatencyObservationWindow:
        if tool_name not in self._windows:
            self._windows[tool_name] = LatencyObservationWindow(
                window_seconds=self._window_seconds
            )
        return self._windows[tool_name]

    def get_timeout_ms(self, tool_name: str) -> float:
        window = self._get_window(tool_name)
        return self._calculator.calculate(window)

    def record(self, tool_name: str, duration_ms: float, succeeded: bool) -> None:
        self._get_window(tool_name).record(duration_ms, succeeded)

    def all_timeouts(self) -> Dict[str, float]:
        return {
            name: self._calculator.calculate(window)
            for name, window in self._windows.items()
        }
```

## Solution 4: Adaptive Timeout Tool Executor

```python
import asyncio
import time
from typing import Any, Callable, Optional


class TimeoutExceeded(Exception):
    def __init__(self, tool_name: str, timeout_ms: float):
        super().__init__(
            f"tool '{tool_name}' exceeded adaptive timeout of {timeout_ms:.0f}ms"
        )
        self.tool_name = tool_name
        self.timeout_ms = timeout_ms


class AdaptiveTimeoutToolExecutor:
    """
    Executes tool calls with a dynamically calculated timeout.
    Records latency and success/failure back to the registry after each call.
    """

    def __init__(self, registry: PerToolAdaptiveTimeoutRegistry):
        self._registry = registry

    async def execute(
        self,
        tool_name: str,
        fn: Callable,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        timeout_ms = self._registry.get_timeout_ms(tool_name)
        timeout_s = timeout_ms / 1000.0
        start = time.time()
        try:
            result = await asyncio.wait_for(fn(*args, **kwargs), timeout=timeout_s)
            duration_ms = (time.time() - start) * 1000
            self._registry.record(tool_name, duration_ms, succeeded=True)
            return result
        except asyncio.TimeoutError:
            duration_ms = (time.time() - start) * 1000
            self._registry.record(tool_name, duration_ms, succeeded=False)
            raise TimeoutExceeded(tool_name, timeout_ms)
        except Exception:
            duration_ms = (time.time() - start) * 1000
            self._registry.record(tool_name, duration_ms, succeeded=False)
            raise
```

## Solution 5: Timeout Drift Detector

```python
from typing import Dict, Optional


class TimeoutDriftDetector:
    """
    Compares current adaptive timeouts against a set of static baseline
    values to detect when a tool's latency has drifted significantly.
    Useful for alerting when a dependency has become persistently slower.
    """

    def __init__(
        self,
        baselines_ms: Dict[str, float],
        drift_threshold_pct: float = 50.0,
    ):
        self._baselines = baselines_ms
        self._threshold = drift_threshold_pct / 100.0

    def detect(self, registry: PerToolAdaptiveTimeoutRegistry) -> list:
        alerts = []
        current = registry.all_timeouts()
        for tool_name, baseline in self._baselines.items():
            current_timeout = current.get(tool_name)
            if current_timeout is None:
                continue
            change = (current_timeout - baseline) / max(baseline, 1)
            if abs(change) >= self._threshold:
                alerts.append({
                    "tool_name": tool_name,
                    "baseline_ms": baseline,
                    "current_timeout_ms": current_timeout,
                    "change_pct": round(change * 100, 1),
                    "direction": "slower" if change > 0 else "faster",
                })
        return alerts
```

## Solution 6: Adaptive Timeout Dashboard

```python
import time
from typing import Optional


class AdaptiveTimeoutDashboard:
    """
    Renders a snapshot of adaptive timeout values, latency percentiles,
    and drift alerts for all tracked tools.
    """

    def __init__(
        self,
        registry: PerToolAdaptiveTimeoutRegistry,
        drift_detector: Optional[TimeoutDriftDetector] = None,
    ):
        self._registry = registry
        self._drift = drift_detector

    def render(self) -> dict:
        tool_stats = {}
        for tool_name, window in self._registry._windows.items():
            tool_stats[tool_name] = {
                "timeout_ms": self._registry.get_timeout_ms(tool_name),
                "p50_ms": window.percentile(50),
                "p95_ms": window.percentile(95),
                "p99_ms": window.percentile(99),
                "observation_count": window.count(),
                "success_rate": round(window.success_rate(), 4),
            }

        return {
            "generated_at": time.time(),
            "tools": tool_stats,
            "drift_alerts": self._drift.detect(self._registry) if self._drift else [],
        }
```

## Comparison

| Approach | Latency Tracking | Percentile Timeout | Min/Max Clamp | Timeout Enforcement | Drift Detection |
|---|---|---|---|---|---|
| LatencyObservationWindow | Yes (sliding) | Yes | No | No | No |
| AdaptiveTimeoutCalculator | Via window | Yes (P99 × 1.5) | Yes | No | No |
| PerToolAdaptiveTimeoutRegistry | Via windows | Via calculator | Via calculator | No | No |
| AdaptiveTimeoutToolExecutor | Via registry | Via registry | Via registry | Yes (asyncio) | No |
| TimeoutDriftDetector | No | No | No | No | Yes |
| AdaptiveTimeoutDashboard | No | No | No | No | Via detector |

**Best for production**: Use `percentile=99.0, multiplier=1.5` as the default — this sets the timeout at 1.5× the P99, which accommodates normal variance without hanging on genuine outages. Set `min_observations=10` so the system does not adapt until it has a statistically meaningful sample; use `fallback_timeout_ms` conservatively (e.g., 15 seconds) during warmup. Wire `TimeoutDriftDetector` to emit an alert when any tool's adaptive timeout has grown more than 50% above its baseline — this catches silent dependency degradations before they become outages.
