---
title: "Agent Doesn't Implement Long-Tail Latency Analysis"
description: "Agents that report only average or p50 latency miss the outliers that dominate user experience: a p99 of 30 seconds means 1 in 100 users waits half a minute even when the median is 2 seconds. Implement long-tail latency analysis with sliding-window percentile tracking, outlier attribution (which tool call, model, or context size caused the spike), and actionable alerts when tail latency exceeds SLO thresholds."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-long-tail-latency-analysis
tags: [latency-analysis, p99, tail-latency, percentile-tracking, slo, performance-observability]
symptoms:
  - "Average latency looks fine but users complain about occasional very slow responses"
  - "No p99 or p999 metrics — impossible to set a meaningful SLO for tail users"
  - "Latency spike investigation starts from scratch because slow requests were not annotated"
  - "Cannot tell whether tail latency is caused by a specific tool, model, or input size"
  - "Alerting fires only on average latency — SLO violations for tail users go undetected"
---

## Why This Happens

Mean latency is easy to compute but hides the distribution. A bimodal distribution (most requests at 500ms, some at 30 seconds) has a mean of 1–2 seconds that looks acceptable. Percentile tracking requires maintaining a running sample — either a reservoir or a T-Digest — that can compute p95/p99/p999 without storing every measurement. Attribution requires tagging each latency sample with the context that produced it (tool name, model, input token count) so that high-percentile samples can be filtered and inspected. Alerting on percentiles requires comparing the current window against a threshold, not just the current mean.

## Solution 1: Latency Sample

```python
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class LatencySample:
    operation: str              # e.g. "tool_call:search", "model:claude-3", "session"
    duration_ms: float
    timestamp: float = field(default_factory=time.time)
    model: str = ""
    tool_name: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    error: bool = False
    session_id: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
```

## Solution 2: Reservoir Sampler

```python
import random
from typing import List


class ReservoirSampler:
    """
    Maintains a fixed-size uniform random sample of latency measurements
    using Algorithm R. Suitable for computing percentiles over a sliding
    time window without storing all observations.
    """

    def __init__(self, capacity: int = 1024, seed: int = 42):
        self._capacity = capacity
        self._reservoir: List[float] = []
        self._count = 0
        random.seed(seed)

    def add(self, value: float) -> None:
        self._count += 1
        if len(self._reservoir) < self._capacity:
            self._reservoir.append(value)
        else:
            j = random.randint(0, self._count - 1)
            if j < self._capacity:
                self._reservoir[j] = value

    def percentile(self, p: float) -> float:
        """p in [0, 100]."""
        if not self._reservoir:
            return 0.0
        sorted_vals = sorted(self._reservoir)
        idx = max(0, int(len(sorted_vals) * p / 100) - 1)
        return sorted_vals[min(idx, len(sorted_vals) - 1)]

    def percentiles(self, ps: List[float]) -> dict:
        return {f"p{int(p)}": round(self.percentile(p), 2) for p in ps}

    def reset(self) -> None:
        self._reservoir.clear()
        self._count = 0

    @property
    def sample_count(self) -> int:
        return self._count
```

## Solution 3: Sliding Window Percentile Tracker

```python
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional, Tuple


@dataclass
class PercentileWindow:
    samples: Deque[Tuple[float, float]] = field(
        default_factory=lambda: deque(maxlen=10_000)
    )   # (timestamp, duration_ms)

    def add(self, duration_ms: float) -> None:
        self.samples.append((time.time(), duration_ms))

    def prune(self, window_seconds: float) -> None:
        cutoff = time.time() - window_seconds
        while self.samples and self.samples[0][0] < cutoff:
            self.samples.popleft()

    def percentile(self, p: float) -> float:
        if not self.samples:
            return 0.0
        values = sorted(s[1] for s in self.samples)
        idx = max(0, int(len(values) * p / 100) - 1)
        return values[min(idx, len(values) - 1)]


class SlidingWindowPercentileTracker:
    """
    Tracks latency percentiles over configurable sliding windows.
    Maintains separate windows per operation label for attribution.
    """

    def __init__(
        self,
        windows_seconds: List[float] = None,
    ):
        self._windows = windows_seconds or [60.0, 300.0, 3600.0]
        self._per_operation: Dict[str, PercentileWindow] = {}
        self._global = PercentileWindow()
        self._total_samples = 0

    def record(self, sample: LatencySample) -> None:
        self._global.add(sample.duration_ms)
        key = sample.operation
        if key not in self._per_operation:
            self._per_operation[key] = PercentileWindow()
        self._per_operation[key].add(sample.duration_ms)
        self._total_samples += 1

    def percentiles(
        self,
        operation: Optional[str] = None,
        window_seconds: float = 300.0,
        ps: List[float] = None,
    ) -> dict:
        ps = ps or [50.0, 90.0, 95.0, 99.0, 99.9]
        window = (
            self._per_operation.get(operation, PercentileWindow())
            if operation
            else self._global
        )
        window.prune(window_seconds)
        return {
            "operation": operation or "__all__",
            "window_seconds": window_seconds,
            "sample_count": len(window.samples),
            "percentiles_ms": {
                f"p{p}": round(window.percentile(p), 2) for p in ps
            },
        }

    def all_operations(self) -> List[str]:
        return list(self._per_operation.keys())
```

## Solution 4: Tail Latency Attributor

```python
from typing import Dict, List


class TailLatencyAttributor:
    """
    Identifies which attributes (tool, model, token count bucket) correlate
    with high-percentile latency by comparing their p99 against the fleet p99.
    """

    def __init__(self, tracker: SlidingWindowPercentileTracker):
        self._tracker = tracker

    def attribute(
        self,
        window_seconds: float = 300.0,
        threshold_multiplier: float = 2.0,
    ) -> List[dict]:
        """
        Returns operations whose p99 is > threshold_multiplier × fleet p99.
        """
        fleet = self._tracker.percentiles(window_seconds=window_seconds)
        fleet_p99 = fleet["percentiles_ms"].get("p99", 0.0)
        if fleet_p99 == 0.0:
            return []

        outliers = []
        for op in self._tracker.all_operations():
            op_stats = self._tracker.percentiles(
                operation=op, window_seconds=window_seconds
            )
            op_p99 = op_stats["percentiles_ms"].get("p99", 0.0)
            if op_p99 > fleet_p99 * threshold_multiplier:
                outliers.append({
                    "operation": op,
                    "p99_ms": op_p99,
                    "fleet_p99_ms": fleet_p99,
                    "multiplier": round(op_p99 / max(fleet_p99, 1), 2),
                    "sample_count": op_stats["sample_count"],
                })

        return sorted(outliers, key=lambda x: -x["multiplier"])
```

## Solution 5: Tail Latency SLO Monitor

```python
import time
from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass
class LatencySLO:
    name: str
    operation: Optional[str]     # None = fleet-wide
    percentile: float             # e.g. 99.0
    threshold_ms: float           # budget in milliseconds
    window_seconds: float = 300.0


@dataclass
class SLOViolation:
    slo_name: str
    operation: str
    percentile: float
    observed_ms: float
    threshold_ms: float
    detected_at: float


class TailLatencySLOMonitor:
    """
    Evaluates latency SLOs against current percentile windows.
    Fires violation records when observed percentile exceeds the threshold.
    """

    def __init__(self, tracker: SlidingWindowPercentileTracker):
        self._tracker = tracker
        self._slos: List[LatencySLO] = []
        self._violations: List[SLOViolation] = []

    def add_slo(self, slo: LatencySLO) -> None:
        self._slos.append(slo)

    def evaluate(self) -> List[SLOViolation]:
        new_violations = []
        for slo in self._slos:
            stats = self._tracker.percentiles(
                operation=slo.operation,
                window_seconds=slo.window_seconds,
                ps=[slo.percentile],
            )
            key = f"p{slo.percentile}"
            observed = stats["percentiles_ms"].get(key, 0.0)
            if observed > slo.threshold_ms and stats["sample_count"] >= 10:
                v = SLOViolation(
                    slo_name=slo.name,
                    operation=slo.operation or "__all__",
                    percentile=slo.percentile,
                    observed_ms=observed,
                    threshold_ms=slo.threshold_ms,
                    detected_at=time.time(),
                )
                new_violations.append(v)
                if len(self._violations) < 10_000:
                    self._violations.append(v)
        return new_violations

    def recent_violations(self, hours: float = 1.0) -> List[SLOViolation]:
        cutoff = time.time() - hours * 3600
        return [v for v in self._violations if v.detected_at >= cutoff]
```

## Solution 6: Long-Tail Latency Dashboard

```python
import time
from typing import List, Optional


class LongTailLatencyDashboard:
    """
    Unified view: fleet percentiles, per-operation breakdown, attribution,
    SLO violations, and trend comparison (current window vs previous).
    """

    def __init__(
        self,
        tracker: SlidingWindowPercentileTracker,
        attributor: TailLatencyAttributor,
        slo_monitor: TailLatencySLOMonitor,
        primary_window_seconds: float = 300.0,
    ):
        self._tracker = tracker
        self._attributor = attributor
        self._slo = slo_monitor
        self._window = primary_window_seconds

    def render(self) -> dict:
        fleet = self._tracker.percentiles(window_seconds=self._window)
        violations = self._slo.evaluate()
        outliers = self._attributor.attribute(
            window_seconds=self._window, threshold_multiplier=2.0
        )

        top_ops = []
        for op in self._tracker.all_operations()[:10]:
            stats = self._tracker.percentiles(
                operation=op, window_seconds=self._window
            )
            if stats["sample_count"] >= 5:
                top_ops.append({
                    "operation": op,
                    "p50_ms": stats["percentiles_ms"].get("p50", 0),
                    "p99_ms": stats["percentiles_ms"].get("p99", 0),
                    "samples": stats["sample_count"],
                })

        top_ops.sort(key=lambda x: -x["p99_ms"])

        return {
            "generated_at": time.time(),
            "window_seconds": self._window,
            "fleet_percentiles_ms": fleet["percentiles_ms"],
            "fleet_samples": fleet["sample_count"],
            "top_operations_by_p99": top_ops[:5],
            "tail_outliers": outliers[:5],
            "slo_violations": [
                {
                    "slo": v.slo_name,
                    "operation": v.operation,
                    "p99_observed_ms": v.observed_ms,
                    "threshold_ms": v.threshold_ms,
                }
                for v in violations
            ],
            "alerts": [
                f"SLO violation: {v.slo_name} p{v.percentile:.0f}={v.observed_ms:.0f}ms > {v.threshold_ms:.0f}ms"
                for v in violations
            ],
        }
```

## Comparison

| Approach | Percentile Tracking | Sliding Window | Attribution | SLO Alerts |
|---|---|---|---|---|
| ReservoirSampler | Yes (static) | No | No | No |
| SlidingWindowPercentileTracker | Yes | Yes (per-op) | No | No |
| TailLatencyAttributor | Via tracker | Via tracker | Yes | No |
| TailLatencySLOMonitor | Via tracker | Via tracker | No | Yes |
| LongTailLatencyDashboard | Via tracker | Via tracker | Via attributor | Via monitor |

**Best for production**: Record a `LatencySample` for every tool call, model API call, and end-to-end session — tag each with `operation="tool_call:{name}"` and `operation="model:{model_id}"`. Define SLOs: `p99 < 5000ms` for tool calls, `p99 < 15000ms` for sessions. Evaluate SLOs every 60 seconds. When `TailLatencyAttributor.attribute()` identifies a specific tool with 5× fleet p99, that tool's implementation or its upstream API is the root cause — check it before blaming the model.
