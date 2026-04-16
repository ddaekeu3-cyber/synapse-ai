---
title: "Agent Doesn't Implement Adaptive Timeout Based on Historical Latency"
description: "Agents that use fixed timeouts — 30 seconds for every tool, regardless of its actual latency distribution — either wait too long for tools that are normally fast (wasting session time when they hang) or cut off tools that legitimately take longer under load. Implement adaptive timeouts that set each tool's deadline from its own P95 historical latency, adjusting automatically as latency profiles change."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-adaptive-timeout-based-on-historical-latency
tags: [adaptive-timeout, latency-percentile, dynamic-deadline, tool-timeout, p95-latency, timeout-tuning]
symptoms:
  - "30-second fixed timeout on a tool that normally responds in 200ms — hangs waste session time"
  - "Fast tool times out at 1 second under load spikes even though P99 is normally 800ms"
  - "No per-tool timeout differentiation — a DB query and an LLM call share the same deadline"
  - "Timeout value was set once at deploy time and never revisited"
  - "Cannot answer 'what is the right timeout for this tool right now?'"
---

## Why This Happens

A fixed timeout is a guess made at deploy time. It cannot adapt to traffic patterns, time-of-day load variation, or gradual performance degradation. Setting it too high causes user-visible hangs when a tool genuinely stalls; setting it too low causes false timeouts during legitimate load spikes. Adaptive timeouts derive the deadline from the tool's own recent latency distribution — specifically P95 or P99 plus a safety margin — so the timeout tracks actual behavior rather than a static estimate. When a tool gets faster, the timeout tightens. When a tool slows under load, the timeout loosens within a configured ceiling.

## Solution 1: Latency Sample Window

```python
import bisect
import time
from collections import deque
from dataclasses import dataclass
from threading import Lock
from typing import Deque, List, Optional, Tuple


@dataclass
class LatencySample:
    value_ms: float
    recorded_at: float


class ToolLatencySampleWindow:
    """
    Maintains a fixed-duration sliding window of latency samples per tool.
    Supports percentile queries for adaptive timeout calculation.
    """

    def __init__(self, window_seconds: float = 600.0, max_samples: int = 5000):
        self._window = window_seconds
        self._max = max_samples
        self._samples: Deque[LatencySample] = deque()
        self._lock = Lock()

    def record(self, latency_ms: float) -> None:
        now = time.time()
        with self._lock:
            self._samples.append(LatencySample(latency_ms, now))
            self._trim(now)
            if len(self._samples) > self._max:
                self._samples.popleft()

    def _trim(self, now: float) -> None:
        cutoff = now - self._window
        while self._samples and self._samples[0].recorded_at < cutoff:
            self._samples.popleft()

    def percentile(self, pct: float) -> Optional[float]:
        with self._lock:
            if not self._samples:
                return None
            values = sorted(s.value_ms for s in self._samples)
            idx = min(int(len(values) * pct / 100.0), len(values) - 1)
            return round(values[idx], 2)

    def count(self) -> int:
        with self._lock:
            return len(self._samples)

    def mean(self) -> Optional[float]:
        with self._lock:
            if not self._samples:
                return None
            return round(sum(s.value_ms for s in self._samples) / len(self._samples), 2)
```

## Solution 2: Adaptive Timeout Policy

```python
from dataclasses import dataclass
from typing import Optional


@dataclass
class AdaptiveTimeoutPolicy:
    """
    Computes a timeout from recent latency percentiles.

    The computed timeout = percentile_value * multiplier, clamped to
    [min_timeout_ms, max_timeout_ms]. Falls back to default_timeout_ms
    when fewer than min_samples are available.
    """
    percentile: float = 95.0            # use P95 as the base
    multiplier: float = 1.5             # safety margin above P95
    min_timeout_ms: float = 500.0       # floor — never shorter than this
    max_timeout_ms: float = 30_000.0    # ceiling — never longer than this
    default_timeout_ms: float = 10_000.0  # used when insufficient samples
    min_samples: int = 20               # minimum samples before adapting

    def compute(self, window: ToolLatencySampleWindow) -> float:
        if window.count() < self.min_samples:
            return self.default_timeout_ms
        p_value = window.percentile(self.percentile)
        if p_value is None:
            return self.default_timeout_ms
        computed = p_value * self.multiplier
        return round(max(self.min_timeout_ms, min(computed, self.max_timeout_ms)), 2)
```

## Solution 3: Per-Tool Timeout Registry

```python
from typing import Dict, Optional


class PerToolTimeoutRegistry:
    """
    Stores a ToolLatencySampleWindow and AdaptiveTimeoutPolicy per tool.
    Tools not registered use the default policy and a fresh window.
    """

    def __init__(self, default_policy: Optional[AdaptiveTimeoutPolicy] = None):
        self._default_policy = default_policy or AdaptiveTimeoutPolicy()
        self._windows: Dict[str, ToolLatencySampleWindow] = {}
        self._policies: Dict[str, AdaptiveTimeoutPolicy] = {}

    def register(
        self,
        tool_name: str,
        policy: Optional[AdaptiveTimeoutPolicy] = None,
        window_seconds: float = 600.0,
    ) -> None:
        self._windows[tool_name] = ToolLatencySampleWindow(window_seconds)
        self._policies[tool_name] = policy or self._default_policy

    def record(self, tool_name: str, latency_ms: float) -> None:
        if tool_name not in self._windows:
            self.register(tool_name)
        self._windows[tool_name].record(latency_ms)

    def timeout_ms(self, tool_name: str) -> float:
        if tool_name not in self._windows:
            self.register(tool_name)
        window = self._windows[tool_name]
        policy = self._policies[tool_name]
        return policy.compute(window)

    def timeout_seconds(self, tool_name: str) -> float:
        return self.timeout_ms(tool_name) / 1000.0

    def all_timeouts(self) -> dict:
        return {name: self.timeout_ms(name) for name in self._windows}
```

## Solution 4: Adaptive Timeout Tool Executor

```python
import asyncio
import time
from typing import Any, Callable


class AdaptiveTimeoutToolExecutor:
    """
    Executes tool calls using a per-tool adaptive timeout derived from
    historical latency. Records each call's latency back into the registry
    to keep timeout estimates current.
    """

    def __init__(self, registry: PerToolTimeoutRegistry):
        self._registry = registry
        self._timeout_hits = 0
        self._total_calls = 0

    async def call(
        self,
        tool_name: str,
        tool_fn: Callable,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        timeout_s = self._registry.timeout_seconds(tool_name)
        self._total_calls += 1
        start = time.time()
        try:
            result = await asyncio.wait_for(
                tool_fn(*args, **kwargs),
                timeout=timeout_s,
            )
            latency_ms = (time.time() - start) * 1000
            self._registry.record(tool_name, latency_ms)
            return result
        except asyncio.TimeoutError:
            latency_ms = (time.time() - start) * 1000
            # Record as the timeout ceiling so the window reflects the stall
            self._registry.record(tool_name, latency_ms)
            self._timeout_hits += 1
            raise

    def stats(self) -> dict:
        return {
            "total_calls": self._total_calls,
            "timeout_hits": self._timeout_hits,
            "timeout_rate": round(
                self._timeout_hits / max(self._total_calls, 1), 4
            ),
        }
```

## Solution 5: Timeout Drift Detector

```python
import time
from typing import Dict, List


class TimeoutDriftDetector:
    """
    Detects when a tool's computed adaptive timeout has drifted significantly
    from its historical baseline — indicating performance regression or improvement.
    """

    def __init__(
        self,
        registry: PerToolTimeoutRegistry,
        drift_threshold_pct: float = 50.0,
    ):
        self._registry = registry
        self._threshold = drift_threshold_pct / 100.0
        self._baseline: Dict[str, float] = {}

    def capture_baseline(self) -> None:
        """Call once after warm-up to record the reference timeouts."""
        self._baseline = dict(self._registry.all_timeouts())

    def check_drift(self) -> List[dict]:
        alerts = []
        current = self._registry.all_timeouts()
        for tool_name, current_ms in current.items():
            baseline_ms = self._baseline.get(tool_name)
            if baseline_ms is None or baseline_ms == 0:
                continue
            drift = (current_ms - baseline_ms) / baseline_ms
            if abs(drift) >= self._threshold:
                direction = "increased" if drift > 0 else "decreased"
                alerts.append({
                    "tool_name": tool_name,
                    "baseline_ms": baseline_ms,
                    "current_ms": current_ms,
                    "drift_pct": round(drift * 100, 1),
                    "direction": direction,
                })
        return alerts
```

## Solution 6: Adaptive Timeout Dashboard

```python
import time


class AdaptiveTimeoutDashboard:
    """
    Combines registry timeouts, executor stats, and drift alerts
    into a single operational snapshot.
    """

    def __init__(
        self,
        registry: PerToolTimeoutRegistry,
        executor: AdaptiveTimeoutToolExecutor,
        drift_detector: TimeoutDriftDetector,
    ):
        self._registry = registry
        self._executor = executor
        self._drift = drift_detector

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "per_tool_timeouts_ms": self._registry.all_timeouts(),
            "executor": self._executor.stats(),
            "drift_alerts": self._drift.check_drift(),
        }
```

## Comparison

| Approach | Per-Tool Latency History | Percentile Timeout | Auto-Update | Drift Detection | Dashboard |
|---|---|---|---|---|---|
| ToolLatencySampleWindow | Yes (sliding) | Yes | No | No | No |
| AdaptiveTimeoutPolicy | Via window | Yes (configurable pct) | No | No | No |
| PerToolTimeoutRegistry | Yes | Via policy | No | No | No |
| AdaptiveTimeoutToolExecutor | Via registry | Via registry | Yes (records each call) | No | No |
| TimeoutDriftDetector | No | No | No | Yes | No |
| AdaptiveTimeoutDashboard | No | No | No | No | Yes |

**Best for production**: Use `percentile=95.0` and `multiplier=1.5` as defaults — this gives a timeout at 1.5× P95, catching the vast majority of normal calls while cutting off genuine stalls quickly. Set `min_samples=20` so the system does not start adapting from a single cold-start call. Register database, LLM, and external API tools with distinct policies — a vector search tool with P95=400ms should have a tighter ceiling than an LLM call with P95=8000ms. Monitor `timeout_rate` from the executor: above 1% warrants investigation; above 5% means either the ceiling is too low or the tool has a reliability problem that timeouts are masking.
