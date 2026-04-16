---
title: "Agent Doesn't Implement Tool Call Success Rate Histograms"
description: "Agents that track only aggregate tool error counts miss the latency distribution of successful calls and the per-error-type breakdown of failures. A tool with 99% success rate but P99 latency of 30 seconds is as operationally problematic as one with a 10% error rate. Implement per-tool histograms that track latency distribution (P50/P95/P99), success/failure rates, error type frequency, and trend detection across rolling windows."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-tool-call-success-rate-histograms
tags: [histogram, tool-metrics, latency-distribution, success-rate, error-classification, percentile-tracking]
symptoms:
  - "Only total error count is tracked — no latency distribution per tool"
  - "P99 latency spikes are invisible because only averages are reported"
  - "All tool errors are counted together with no breakdown by error type"
  - "No trend detection — a gradual degradation in success rate is not noticed"
  - "Metrics reset on restart, preventing cross-deployment comparison"
---

## Why This Happens

A single counter per tool tells you how many calls succeeded or failed, but not whether the failures cluster in a specific error class, whether latency is bimodal, or whether the success rate has been trending downward for the past hour. Histograms solve the latency problem by recording the full distribution of observed values and computing percentiles on demand. Pairing histograms with per-error-type counters and trend detectors gives operators the full picture: not just whether something failed, but what failed, how slowly, and whether it is getting worse.

## Solution 1: Latency Histogram

```python
import time
from threading import Lock
from typing import List, Optional


class LatencyHistogram:
    """
    Fixed-point histogram for tracking latency distributions.
    Uses configurable bucket boundaries and supports percentile queries.
    """

    DEFAULT_BUCKETS_MS = [1, 5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000, 30000]

    def __init__(self, buckets_ms: Optional[List[float]] = None):
        self._buckets = sorted(buckets_ms or self.DEFAULT_BUCKETS_MS)
        self._counts = [0] * (len(self._buckets) + 1)  # +1 for overflow bucket
        self._sum_ms = 0.0
        self._total = 0
        self._lock = Lock()

    def record(self, latency_ms: float) -> None:
        with self._lock:
            self._sum_ms += latency_ms
            self._total += 1
            for i, bound in enumerate(self._buckets):
                if latency_ms <= bound:
                    self._counts[i] += 1
                    return
            self._counts[-1] += 1  # overflow

    def percentile(self, pct: float) -> Optional[float]:
        """Returns the bucket upper bound at the given percentile."""
        with self._lock:
            if self._total == 0:
                return None
            target = self._total * pct / 100.0
            cumulative = 0
            for i, count in enumerate(self._counts):
                cumulative += count
                if cumulative >= target:
                    if i < len(self._buckets):
                        return float(self._buckets[i])
                    return float(self._buckets[-1]) * 3  # estimate overflow
            return None

    def mean(self) -> Optional[float]:
        with self._lock:
            if self._total == 0:
                return None
            return round(self._sum_ms / self._total, 2)

    def snapshot(self) -> dict:
        return {
            "count": self._total,
            "mean_ms": self.mean(),
            "p50_ms": self.percentile(50),
            "p95_ms": self.percentile(95),
            "p99_ms": self.percentile(99),
        }

    def reset(self) -> None:
        with self._lock:
            self._counts = [0] * len(self._counts)
            self._sum_ms = 0.0
            self._total = 0
```

## Solution 2: Error Type Counter

```python
import time
from collections import defaultdict
from threading import Lock
from typing import Dict, List, Tuple


class ErrorTypeCounter:
    """
    Counts errors by type string with sliding window support.
    """

    def __init__(self, window_seconds: float = 3600.0):
        self._window = window_seconds
        self._events: List[Tuple[float, str]] = []
        self._lock = Lock()

    def record(self, error_type: str) -> None:
        with self._lock:
            now = time.time()
            self._events.append((now, error_type))
            self._evict(now)

    def _evict(self, now: float) -> None:
        cutoff = now - self._window
        while self._events and self._events[0][0] < cutoff:
            self._events.pop(0)

    def counts(self, window_seconds: Optional[float] = None) -> Dict[str, int]:
        now = time.time()
        cutoff = now - (window_seconds or self._window)
        with self._lock:
            result: Dict[str, int] = defaultdict(int)
            for ts, etype in self._events:
                if ts >= cutoff:
                    result[etype] += 1
            return dict(result)

    def top_errors(self, n: int = 5, window_seconds: Optional[float] = None) -> List[dict]:
        counts = self.counts(window_seconds)
        sorted_errors = sorted(counts.items(), key=lambda x: x[1], reverse=True)
        return [{"error_type": e, "count": c} for e, c in sorted_errors[:n]]
```

## Solution 3: Tool Call Metrics

```python
import time
from threading import Lock
from typing import Optional


class ToolCallMetrics:
    """
    Combines a latency histogram, success/failure counters, and
    error type breakdown for a single named tool.
    """

    def __init__(self, tool_name: str):
        self.tool_name = tool_name
        self._histogram = LatencyHistogram()
        self._error_counter = ErrorTypeCounter()
        self._success_count = 0
        self._failure_count = 0
        self._lock = Lock()
        self._created_at = time.time()

    def record_success(self, latency_ms: float) -> None:
        with self._lock:
            self._success_count += 1
        self._histogram.record(latency_ms)

    def record_failure(self, latency_ms: float, error_type: str) -> None:
        with self._lock:
            self._failure_count += 1
        self._histogram.record(latency_ms)
        self._error_counter.record(error_type)

    def success_rate(self) -> Optional[float]:
        with self._lock:
            total = self._success_count + self._failure_count
            if total == 0:
                return None
            return round(self._success_count / total, 4)

    def snapshot(self, window_seconds: float = 3600.0) -> dict:
        with self._lock:
            total = self._success_count + self._failure_count
            sr = self._success_count / total if total > 0 else None
        return {
            "tool_name": self.tool_name,
            "total_calls": total,
            "success_rate": round(sr, 4) if sr is not None else None,
            "latency": self._histogram.snapshot(),
            "top_errors": self._error_counter.top_errors(5, window_seconds),
        }
```

## Solution 4: Multi-Tool Metrics Registry

```python
from threading import Lock
from typing import Dict, Optional


class ToolMetricsRegistry:
    """
    Manages ToolCallMetrics instances for all tools.
    Creates entries lazily on first observation.
    """

    def __init__(self):
        self._metrics: Dict[str, ToolCallMetrics] = {}
        self._lock = Lock()

    def get_or_create(self, tool_name: str) -> ToolCallMetrics:
        with self._lock:
            if tool_name not in self._metrics:
                self._metrics[tool_name] = ToolCallMetrics(tool_name)
            return self._metrics[tool_name]

    def record_call(
        self,
        tool_name: str,
        latency_ms: float,
        success: bool,
        error_type: str = "",
    ) -> None:
        m = self.get_or_create(tool_name)
        if success:
            m.record_success(latency_ms)
        else:
            m.record_failure(latency_ms, error_type or "unknown")

    def all_snapshots(self, window_seconds: float = 3600.0) -> list:
        with self._lock:
            tools = list(self._metrics.keys())
        return [
            self.get_or_create(t).snapshot(window_seconds)
            for t in tools
        ]

    def worst_tools(
        self,
        metric: str = "success_rate",
        top_n: int = 5,
    ) -> list:
        snapshots = self.all_snapshots()
        if metric == "success_rate":
            ranked = sorted(
                [s for s in snapshots if s["success_rate"] is not None],
                key=lambda s: s["success_rate"],
            )
        elif metric == "p99_ms":
            ranked = sorted(
                [s for s in snapshots if s["latency"]["p99_ms"] is not None],
                key=lambda s: s["latency"]["p99_ms"],
                reverse=True,
            )
        else:
            return []
        return ranked[:top_n]
```

## Solution 5: Success Rate Trend Detector

```python
import time
from collections import deque
from typing import Deque, List, Optional, Tuple


class SuccessRateTrendDetector:
    """
    Detects degradation trends by comparing success rates across
    two time windows: if the recent window is significantly lower
    than the baseline window, a degradation is flagged.
    """

    def __init__(
        self,
        registry: ToolMetricsRegistry,
        degradation_threshold: float = 0.05,  # 5% drop triggers alert
    ):
        self._registry = registry
        self._threshold = degradation_threshold
        self._snapshots: Deque[Tuple[float, dict]] = deque(maxlen=200)

    def record_snapshot(self) -> None:
        self._snapshots.append((time.time(), {
            s["tool_name"]: s["success_rate"]
            for s in self._registry.all_snapshots()
            if s["success_rate"] is not None
        }))

    def detect_degradations(
        self,
        baseline_window_seconds: float = 3600.0,
        recent_window_seconds: float = 300.0,
    ) -> List[dict]:
        now = time.time()
        baseline_cutoff = now - baseline_window_seconds
        recent_cutoff = now - recent_window_seconds

        baseline_snaps = {
            k: v for ts, snap in self._snapshots
            if ts >= baseline_cutoff
            for k, v in snap.items()
        }
        recent_snaps = {
            k: v for ts, snap in self._snapshots
            if ts >= recent_cutoff
            for k, v in snap.items()
        }

        degradations = []
        for tool, recent_rate in recent_snaps.items():
            baseline_rate = baseline_snaps.get(tool)
            if baseline_rate is None or recent_rate is None:
                continue
            drop = baseline_rate - recent_rate
            if drop >= self._threshold:
                degradations.append({
                    "tool_name": tool,
                    "baseline_success_rate": round(baseline_rate, 4),
                    "recent_success_rate": round(recent_rate, 4),
                    "drop": round(drop, 4),
                })
        return sorted(degradations, key=lambda d: d["drop"], reverse=True)
```

## Solution 6: Tool Histogram Dashboard

```python
import time


class ToolCallHistogramDashboard:
    """
    Renders per-tool histograms, success rates, and trend degradations.
    """

    def __init__(
        self,
        registry: ToolMetricsRegistry,
        trend_detector: SuccessRateTrendDetector,
    ):
        self._registry = registry
        self._trend = trend_detector

    def render(self, window_seconds: float = 3600.0) -> dict:
        self._trend.record_snapshot()
        return {
            "generated_at": time.time(),
            "tool_snapshots": self._registry.all_snapshots(window_seconds),
            "worst_by_success_rate": self._registry.worst_tools("success_rate", top_n=3),
            "worst_by_p99_latency": self._registry.worst_tools("p99_ms", top_n=3),
            "degradations_detected": self._trend.detect_degradations(),
        }
```

## Comparison

| Approach | Latency Distribution | Success Rate | Error Types | Trend Detection | Dashboard |
|---|---|---|---|---|---|
| LatencyHistogram | Yes (P50/P95/P99) | No | No | No | No |
| ErrorTypeCounter | No | No | Yes (sliding window) | No | No |
| ToolCallMetrics | Via histogram | Yes | Via error counter | No | No |
| ToolMetricsRegistry | Via per-tool | Via per-tool | Via per-tool | No | No |
| SuccessRateTrendDetector | No | Via registry | No | Yes (2-window) | No |
| ToolCallHistogramDashboard | No | No | No | No | Yes |

**Best for production**: Record every tool call result through `ToolMetricsRegistry.record_call()` — wrap the call dispatcher so no call site needs to be modified individually. Set P99 latency SLOs per tool category: COMPUTE tools should have P99 < 100ms, DATABASE < 500ms, EXTERNAL_API < 5000ms — alert when these are exceeded. Run `SuccessRateTrendDetector.detect_degradations()` every 5 minutes and route results with `drop >= 0.10` to PagerDuty. Use `ErrorTypeCounter.top_errors()` during incident response to determine whether a spike is a single error type (likely a config or upstream change) or spread across many types (likely a network partition or resource exhaustion).
