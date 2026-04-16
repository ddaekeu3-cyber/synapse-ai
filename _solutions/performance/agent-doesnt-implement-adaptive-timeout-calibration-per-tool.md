---
title: "Agent Doesn't Implement Adaptive Timeout Calibration Per Tool"
description: "Agents that apply a single global timeout to all tool calls either time out too aggressively on legitimately slow tools (causing false failures on database queries or long-running searches) or wait too long for fast tools that have hung (masking incidents with excessive tail latency). Implement adaptive timeout calibration that learns per-tool latency distributions from historical calls and sets each tool's timeout to a multiple of its observed P95, adjusting automatically as tool performance changes."
date: 2026-04-16
difficulty: advanced
category: performance
slug: agent-doesnt-implement-adaptive-timeout-calibration-per-tool
tags: [adaptive-timeout, latency-calibration, p95-timeout, tool-performance, timeout-management, tail-latency]
symptoms:
  - "Fast tools occasionally hang but are not timed out until the global timeout fires minutes later"
  - "Slow but legitimate tools (large DB queries) are incorrectly timed out under the global limit"
  - "No per-tool timeout — a single value is applied uniformly to all tool types"
  - "Timeout values were set manually and never updated after tool performance changed"
  - "Cannot distinguish a timed-out tool from a tool that returned an error"
---

## Why This Happens

Tool latency is heterogeneous. A vector search returns in 50ms; a full-text database query returns in 800ms; a code execution tool returns in up to 10 seconds. A global timeout of 5 seconds is too aggressive for the code tool and too lenient for the search tool that should never take more than 500ms. Adaptive calibration maintains a sliding window of recent latency observations for each tool, computes the P95 (to tolerate natural variance without cutting off legitimate calls), multiplies by a safety factor, and uses that as the per-tool timeout. When a tool's latency distribution shifts — after a dependency update, a schema change, or a traffic spike — the timeout follows automatically.

## Solution 1: Tool Latency Sample

```python
import time
from collections import deque
from dataclasses import dataclass
from threading import Lock
from typing import Deque, Optional, Tuple


@dataclass
class LatencySample:
    latency_ms: float
    recorded_at: float
    timed_out: bool = False
    error: bool = False


class ToolLatencyWindow:
    """
    Maintains a sliding window of latency samples for a single tool.
    Supports percentile queries for timeout calibration.
    """

    def __init__(
        self,
        window_seconds: float = 3600.0,
        max_samples: int = 1000,
    ):
        self._window = window_seconds
        self._max = max_samples
        self._samples: Deque[LatencySample] = deque()
        self._lock = Lock()

    def record(self, latency_ms: float, timed_out: bool = False, error: bool = False) -> None:
        sample = LatencySample(
            latency_ms=latency_ms,
            recorded_at=time.time(),
            timed_out=timed_out,
            error=error,
        )
        with self._lock:
            self._samples.append(sample)
            if len(self._samples) > self._max:
                self._samples.popleft()

    def _recent(self) -> list:
        cutoff = time.time() - self._window
        return [s for s in self._samples if s.recorded_at >= cutoff and not s.timed_out and not s.error]

    def percentile(self, pct: float) -> Optional[float]:
        values = sorted(s.latency_ms for s in self._recent())
        if not values:
            return None
        idx = min(int(len(values) * pct / 100.0), len(values) - 1)
        return round(values[idx], 2)

    def sample_count(self) -> int:
        return len(self._recent())
```

## Solution 2: Adaptive Timeout Calculator

```python
from typing import Optional


class AdaptiveTimeoutCalculator:
    """
    Computes a calibrated timeout for a tool from its latency window.
    Uses P95 * safety_factor, clamped to [min_timeout, max_timeout].
    Falls back to a default timeout when there are too few samples.
    """

    def __init__(
        self,
        safety_factor: float = 2.5,
        min_timeout_ms: float = 500.0,
        max_timeout_ms: float = 30000.0,
        default_timeout_ms: float = 5000.0,
        min_samples_required: int = 20,
        percentile: float = 95.0,
    ):
        self._safety = safety_factor
        self._min = min_timeout_ms
        self._max = max_timeout_ms
        self._default = default_timeout_ms
        self._min_samples = min_samples_required
        self._percentile = percentile

    def calculate(self, window: ToolLatencyWindow) -> float:
        if window.sample_count() < self._min_samples:
            return self._default

        p95 = window.percentile(self._percentile)
        if p95 is None:
            return self._default

        calibrated = p95 * self._safety
        return round(max(self._min, min(self._max, calibrated)), 2)

    def calculate_seconds(self, window: ToolLatencyWindow) -> float:
        return round(self.calculate(window) / 1000.0, 3)
```

## Solution 3: Per-Tool Timeout Registry

```python
import threading
from typing import Dict, Optional


class PerToolTimeoutRegistry:
    """
    Maintains a latency window and calibrated timeout for each registered tool.
    Provides the current timeout for a tool and records latency observations.
    """

    def __init__(
        self,
        calculator: AdaptiveTimeoutCalculator,
        window_seconds: float = 3600.0,
    ):
        self._calculator = calculator
        self._window_seconds = window_seconds
        self._windows: Dict[str, ToolLatencyWindow] = {}
        self._overrides: Dict[str, float] = {}    # manual overrides in ms
        self._lock = threading.Lock()

    def _get_window(self, tool_name: str) -> ToolLatencyWindow:
        if tool_name not in self._windows:
            self._windows[tool_name] = ToolLatencyWindow(self._window_seconds)
        return self._windows[tool_name]

    def record(
        self,
        tool_name: str,
        latency_ms: float,
        timed_out: bool = False,
        error: bool = False,
    ) -> None:
        with self._lock:
            self._get_window(tool_name).record(latency_ms, timed_out, error)

    def timeout_ms(self, tool_name: str) -> float:
        if tool_name in self._overrides:
            return self._overrides[tool_name]
        with self._lock:
            window = self._get_window(tool_name)
        return self._calculator.calculate(window)

    def timeout_seconds(self, tool_name: str) -> float:
        return round(self.timeout_ms(tool_name) / 1000.0, 3)

    def set_override(self, tool_name: str, timeout_ms: float) -> None:
        self._overrides[tool_name] = timeout_ms

    def clear_override(self, tool_name: str) -> None:
        self._overrides.pop(tool_name, None)

    def all_timeouts_ms(self) -> Dict[str, float]:
        with self._lock:
            return {tool: self.timeout_ms(tool) for tool in self._windows}
```

## Solution 4: Adaptive Timeout Executor

```python
import asyncio
import time
from typing import Any, Callable, Optional


class AdaptiveTimeoutExecutor:
    """
    Executes a tool function with a calibrated timeout from the registry.
    Records latency back into the registry after each call to close the
    feedback loop.
    """

    def __init__(self, registry: PerToolTimeoutRegistry):
        self._registry = registry

    async def execute(
        self,
        tool_name: str,
        tool_fn: Callable,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        timeout_s = self._registry.timeout_seconds(tool_name)
        start = time.time()
        timed_out = False
        error = False

        try:
            result = await asyncio.wait_for(
                tool_fn(*args, **kwargs),
                timeout=timeout_s,
            )
            return result
        except asyncio.TimeoutError:
            timed_out = True
            raise ToolTimeoutError(tool_name, timeout_s)
        except Exception:
            error = True
            raise
        finally:
            latency_ms = (time.time() - start) * 1000
            self._registry.record(
                tool_name,
                round(latency_ms, 2),
                timed_out=timed_out,
                error=error,
            )


class ToolTimeoutError(Exception):
    def __init__(self, tool_name: str, timeout_seconds: float):
        super().__init__(
            f"tool '{tool_name}' timed out after {timeout_seconds:.3f}s"
        )
        self.tool_name = tool_name
        self.timeout_seconds = timeout_seconds
```

## Solution 5: Timeout Drift Detector

```python
import time
from typing import Dict, Optional


class TimeoutDriftDetector:
    """
    Detects when a tool's calibrated timeout has drifted significantly
    from its baseline, signaling a latency regression or recovery.
    """

    def __init__(
        self,
        registry: PerToolTimeoutRegistry,
        drift_threshold_pct: float = 50.0,
    ):
        self._registry = registry
        self._threshold = drift_threshold_pct / 100.0
        self._baselines: Dict[str, float] = {}

    def snapshot_baselines(self) -> None:
        self._baselines = dict(self._registry.all_timeouts_ms())

    def check_drift(self) -> list:
        current = self._registry.all_timeouts_ms()
        alerts = []
        for tool, current_ms in current.items():
            baseline = self._baselines.get(tool)
            if baseline is None or baseline == 0:
                continue
            change = abs(current_ms - baseline) / baseline
            if change > self._threshold:
                direction = "increased" if current_ms > baseline else "decreased"
                alerts.append({
                    "tool_name": tool,
                    "baseline_ms": round(baseline, 2),
                    "current_ms": round(current_ms, 2),
                    "change_pct": round(change * 100, 1),
                    "direction": direction,
                })
        return alerts
```

## Solution 6: Adaptive Timeout Dashboard

```python
import time


class AdaptiveTimeoutDashboard:
    """
    Combines per-tool timeout values, sample counts, P95 latencies,
    and drift alerts into a single operational report.
    """

    def __init__(
        self,
        registry: PerToolTimeoutRegistry,
        drift_detector: TimeoutDriftDetector,
    ):
        self._registry = registry
        self._drift = drift_detector

    def render(self) -> dict:
        tool_details = {}
        for tool_name, window in self._registry._windows.items():
            tool_details[tool_name] = {
                "timeout_ms": self._registry.timeout_ms(tool_name),
                "timeout_seconds": self._registry.timeout_seconds(tool_name),
                "p50_ms": window.percentile(50),
                "p95_ms": window.percentile(95),
                "p99_ms": window.percentile(99),
                "sample_count": window.sample_count(),
                "override_active": tool_name in self._registry._overrides,
            }

        return {
            "generated_at": time.time(),
            "tools": tool_details,
            "drift_alerts": self._drift.check_drift(),
        }
```

## Comparison

| Approach | Latency Tracking | P95 Calculation | Auto-Calibration | Drift Detection | Manual Override |
|---|---|---|---|---|---|
| ToolLatencyWindow | Yes (sliding) | Yes | No | No | No |
| AdaptiveTimeoutCalculator | No | Via window | Yes | No | No |
| PerToolTimeoutRegistry | Via window | Via calculator | Yes | No | Yes |
| AdaptiveTimeoutExecutor | Via registry | Via registry | Via registry | No | No |
| TimeoutDriftDetector | No | No | No | Yes | No |
| AdaptiveTimeoutDashboard | No | No | No | Via detector | No |

**Best for production**: Seed the registry with a reasonable default timeout (5 seconds) and let calibration kick in after `min_samples_required=20` observations — this prevents the first 20 calls from being timed out by an uncalibrated value. Set `safety_factor=2.5` for interactive tools and `safety_factor=4.0` for batch or background tools, reflecting different user tolerance for tail latency. Run `TimeoutDriftDetector.check_drift()` after every deployment: a >50% increase in calibrated timeout for a tool means that deployment made something slower, and should trigger investigation before the issue reaches users.
