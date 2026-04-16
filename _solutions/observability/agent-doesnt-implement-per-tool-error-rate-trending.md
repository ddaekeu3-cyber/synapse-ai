---
title: "Agent Doesn't Implement Per-Tool Error Rate Trending"
description: "Agents that aggregate all tool errors into a single failure counter cannot detect that one specific tool's error rate is rising while others remain healthy: a database tool silently degrades from 0.1% to 8% errors over two hours with no alert. Implement per-tool error rate trending that tracks rolling error rates per tool, detects upward trends, and alerts when a tool's rate crosses its individual baseline."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-per-tool-error-rate-trending
tags: [error-rate, per-tool-metrics, trend-detection, alerting, rolling-window, tool-health]
symptoms:
  - "Global error rate looks healthy while one tool has degraded from 0% to 15%"
  - "No per-tool error counters — all failures roll up to a single metric"
  - "Tool error rate spike discovered in post-incident review, not during the incident"
  - "Cannot distinguish transient noise from a real upward trend in tool failures"
  - "On-call engineer cannot tell which tool is causing elevated latency without manual log search"
---

## Why This Happens

A global error rate hides per-tool degradation. If 10 tools each handle 10% of calls and one degrades to 50% errors, the global rate rises from ~0% to ~5% — well below most alert thresholds. Per-tool error rate tracking requires a separate sliding window counter per tool, a trend calculation that compares recent error rate to a baseline window, and an alert mechanism that fires when the trend exceeds a per-tool threshold. Without this, single-tool outages are invisible until they become severe enough to affect the aggregate.

## Solution 1: Tool Error Counter

```python
import time
from collections import deque
from dataclasses import dataclass, field
from threading import Lock
from typing import Deque, Optional, Tuple


class ToolErrorCounter:
    """
    Sliding window error rate counter for a single tool.
    Tracks (timestamp, success) events in a deque and computes
    error rate over a configurable window.
    """

    def __init__(
        self,
        tool_name: str,
        window_seconds: float = 300.0,
    ):
        self.tool_name = tool_name
        self._window = window_seconds
        self._events: Deque[Tuple[float, bool]] = deque()
        self._lock = Lock()

    def record(self, success: bool) -> None:
        now = time.time()
        with self._lock:
            self._events.append((now, success))
            cutoff = now - self._window
            while self._events and self._events[0][0] < cutoff:
                self._events.popleft()

    def error_rate(self) -> float:
        now = time.time()
        cutoff = now - self._window
        with self._lock:
            recent = [(ts, ok) for ts, ok in self._events if ts >= cutoff]
        if not recent:
            return 0.0
        errors = sum(1 for _, ok in recent if not ok)
        return round(errors / len(recent), 4)

    def call_count(self, window_seconds: Optional[float] = None) -> int:
        w = window_seconds or self._window
        cutoff = time.time() - w
        with self._lock:
            return sum(1 for ts, _ in self._events if ts >= cutoff)

    def snapshot(self) -> dict:
        now = time.time()
        cutoff = now - self._window
        with self._lock:
            recent = [(ts, ok) for ts, ok in self._events if ts >= cutoff]
        total = len(recent)
        errors = sum(1 for _, ok in recent if not ok)
        return {
            "tool_name": self.tool_name,
            "window_seconds": self._window,
            "total_calls": total,
            "errors": errors,
            "error_rate": round(errors / total, 4) if total else 0.0,
        }
```

## Solution 2: Per-Tool Error Rate Registry

```python
from typing import Dict, Optional


class PerToolErrorRateRegistry:
    """
    Maintains one ToolErrorCounter per tool.
    Creates counters lazily on first record() call.
    """

    def __init__(self, default_window_seconds: float = 300.0):
        self._window = default_window_seconds
        self._counters: Dict[str, ToolErrorCounter] = {}

    def record(self, tool_name: str, success: bool) -> None:
        if tool_name not in self._counters:
            self._counters[tool_name] = ToolErrorCounter(tool_name, self._window)
        self._counters[tool_name].record(success)

    def error_rate(self, tool_name: str) -> float:
        counter = self._counters.get(tool_name)
        return counter.error_rate() if counter else 0.0

    def all_snapshots(self) -> Dict[str, dict]:
        return {name: counter.snapshot() for name, counter in self._counters.items()}

    def tools(self) -> list:
        return sorted(self._counters.keys())
```

## Solution 3: Error Rate Trend Detector

```python
import time
from collections import defaultdict
from threading import Lock
from typing import Dict, List, Tuple


class ErrorRateTrendDetector:
    """
    Compares a tool's recent error rate (short window) against its
    baseline error rate (long window) to detect upward trends.
    """

    def __init__(
        self,
        registry: PerToolErrorRateRegistry,
        baseline_window_seconds: float = 3600.0,
        recent_window_seconds: float = 300.0,
        trend_threshold_absolute: float = 0.05,  # 5 percentage points above baseline
        trend_threshold_relative: float = 2.0,   # 2× baseline rate
        min_calls_for_baseline: int = 20,
    ):
        self._registry = registry
        self._baseline_window = baseline_window_seconds
        self._recent_window = recent_window_seconds
        self._abs_threshold = trend_threshold_absolute
        self._rel_threshold = trend_threshold_relative
        self._min_calls = min_calls_for_baseline

    def check_tool(self, tool_name: str) -> dict:
        counter = self._registry._counters.get(tool_name)
        if not counter:
            return {"tool_name": tool_name, "status": "no_data"}

        baseline_calls = counter.call_count(self._baseline_window)
        if baseline_calls < self._min_calls:
            return {
                "tool_name": tool_name,
                "status": "insufficient_baseline",
                "baseline_calls": baseline_calls,
            }

        # Compute baseline rate from long window, recent rate from short window
        baseline_rate = counter.error_rate()  # uses default window (baseline)
        # Temporarily compute recent rate using a short window snapshot
        recent_calls = counter.call_count(self._recent_window)
        recent_snap = counter.snapshot()
        recent_rate = recent_snap["error_rate"]

        absolute_increase = recent_rate - baseline_rate
        relative_increase = recent_rate / max(baseline_rate, 0.001)

        trending = (
            absolute_increase >= self._abs_threshold
            or relative_increase >= self._rel_threshold
        ) and recent_rate > 0.01

        return {
            "tool_name": tool_name,
            "status": "trending_up" if trending else "ok",
            "baseline_error_rate": baseline_rate,
            "recent_error_rate": recent_rate,
            "absolute_increase": round(absolute_increase, 4),
            "relative_increase": round(relative_increase, 2),
            "recent_calls": recent_calls,
            "trending": trending,
        }

    def check_all(self) -> List[dict]:
        return [self.check_tool(name) for name in self._registry.tools()]

    def trending_tools(self) -> List[dict]:
        return [r for r in self.check_all() if r.get("trending")]
```

## Solution 4: Tool Error Rate Alert Manager

```python
import time
from typing import Callable, Dict, List, Optional


class ToolErrorRateAlert:
    def __init__(self, tool_name: str, trend: dict, fired_at: float):
        self.tool_name = tool_name
        self.trend = trend
        self.fired_at = fired_at


class ToolErrorRateAlertManager:
    """
    Fires alerts when per-tool error rate trends are detected.
    Implements cooldown to prevent alert storms.
    """

    def __init__(
        self,
        trend_detector: ErrorRateTrendDetector,
        alert_fn: Optional[Callable[[ToolErrorRateAlert], None]] = None,
        cooldown_seconds: float = 600.0,
    ):
        self._detector = trend_detector
        self._alert_fn = alert_fn
        self._cooldown = cooldown_seconds
        self._last_fired: Dict[str, float] = {}
        self._fired_alerts: List[ToolErrorRateAlert] = []

    def evaluate(self) -> List[ToolErrorRateAlert]:
        now = time.time()
        fired = []
        for trend in self._detector.trending_tools():
            tool = trend["tool_name"]
            last = self._last_fired.get(tool, 0.0)
            if now - last < self._cooldown:
                continue
            alert = ToolErrorRateAlert(tool, trend, now)
            self._last_fired[tool] = now
            self._fired_alerts.append(alert)
            fired.append(alert)
            if self._alert_fn:
                self._alert_fn(alert)
        return fired

    def recent_alerts(self, window_seconds: float = 3600.0) -> List[dict]:
        cutoff = time.time() - window_seconds
        return [
            {"tool_name": a.tool_name, "fired_at": a.fired_at, **a.trend}
            for a in self._fired_alerts
            if a.fired_at >= cutoff
        ]
```

## Solution 5: Instrumented Tool Executor

```python
import time
from typing import Any, Callable, Dict


class ErrorRateInstrumentedToolExecutor:
    """
    Wraps tool calls and records success/failure in the error rate registry.
    """

    def __init__(
        self,
        registry: PerToolErrorRateRegistry,
        alert_manager: Optional[ToolErrorRateAlertManager] = None,
        alert_check_interval_seconds: float = 30.0,
    ):
        self._registry = registry
        self._alert_manager = alert_manager
        self._alert_interval = alert_check_interval_seconds
        self._last_alert_check = 0.0

    async def execute(
        self,
        tool_name: str,
        tool_fn: Callable,
        **kwargs: Any,
    ) -> Any:
        try:
            result = await tool_fn(**kwargs)
            self._registry.record(tool_name, success=True)
            return result
        except Exception:
            self._registry.record(tool_name, success=False)
            raise
        finally:
            now = time.time()
            if (
                self._alert_manager
                and now - self._last_alert_check >= self._alert_interval
            ):
                self._alert_manager.evaluate()
                self._last_alert_check = now
```

## Solution 6: Per-Tool Error Rate Dashboard

```python
import time


class PerToolErrorRateDashboard:
    """
    Combines all per-tool snapshots, trending analysis, and recent alerts
    into a single operational report.
    """

    def __init__(
        self,
        registry: PerToolErrorRateRegistry,
        trend_detector: ErrorRateTrendDetector,
        alert_manager: ToolErrorRateAlertManager,
    ):
        self._registry = registry
        self._trend = trend_detector
        self._alerts = alert_manager

    def render(self) -> dict:
        snapshots = self._registry.all_snapshots()
        trends = {r["tool_name"]: r for r in self._trend.check_all()}
        return {
            "generated_at": time.time(),
            "tools": {
                name: {
                    **snapshots.get(name, {}),
                    "trend": trends.get(name, {}),
                }
                for name in self._registry.tools()
            },
            "trending_up": [t["tool_name"] for t in self._trend.trending_tools()],
            "recent_alerts": self._alerts.recent_alerts(3600.0),
        }
```

## Comparison

| Approach | Per-Tool Tracking | Sliding Window | Trend Detection | Alert Cooldown | Dashboard |
|---|---|---|---|---|---|
| ToolErrorCounter | Yes (single tool) | Yes | No | No | No |
| PerToolErrorRateRegistry | Yes (all tools) | Via counter | No | No | No |
| ErrorRateTrendDetector | Via registry | Via counter | Yes (abs + rel) | No | No |
| ToolErrorRateAlertManager | Via detector | No | Via detector | Yes | No |
| PerToolErrorRateDashboard | No | No | No | No | Yes |

**Best for production**: Use `window_seconds=300` (5-minute sliding window) for real-time detection and `baseline_window_seconds=3600` (1-hour baseline) for trend comparison — this combination catches a tool that degrades over 10 minutes without false-positives from transient bursts. Set `trend_threshold_absolute=0.05` and `trend_threshold_relative=3.0`: absolute catches tools going from 0% to 5%, relative catches tools going from 1% to 3%. Set `cooldown_seconds=600` to prevent alert storms during a sustained outage. Emit `PerToolErrorRateDashboard.render()` as a structured log every minute — this gives on-call engineers a per-tool health snapshot without requiring a separate metrics system.
