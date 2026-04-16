---
title: "Agent Doesn't Implement Slow Query Detection for Tool Calls"
description: "Agents that invoke database or search tools without latency thresholds cannot distinguish a 50 ms query from a 5 000 ms one at the observability layer — both appear as successful tool calls. Without slow query detection, queries that degrade due to missing indexes, lock contention, or data growth go undetected until users complain. Implement per-tool slow query detection that classifies calls by latency tier, surfaces slow patterns, and emits structured alerts."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-slow-query-detection-for-tool-calls
tags: [slow-query, latency-detection, query-profiling, tool-monitoring, performance-regression, latency-classification]
symptoms:
  - "Tool calls occasionally take 10× longer than usual with no alert or log distinction"
  - "No latency threshold defined per tool — all durations treated as equivalent"
  - "Slow database queries caused by missing indexes are invisible in agent metrics"
  - "P99 latency degrades gradually over weeks as data grows, but no alarm fires"
  - "Cannot identify which specific query pattern is causing tail latency"
---

## Why This Happens

Tool call instrumentation typically records success/failure and total duration, but does not classify duration against a threshold. A 3-second database query and a 30-millisecond one both emit the same log event with different numbers. Without a slow query threshold and a detection layer, gradual latency regressions — caused by table growth, index fragmentation, or connection pool saturation — accumulate invisibly. Slow query detection adds a classification step: every tool call duration is compared against a per-tool threshold, classified into latency tiers, and optionally logged with the full call context so that the query pattern can be reproduced and optimized.

## Solution 1: Latency Tier Classifier

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Optional


class LatencyTier(str, Enum):
    FAST = "fast"           # below warning threshold
    WARNING = "warning"     # slow but not critical
    SLOW = "slow"           # significantly above threshold
    CRITICAL = "critical"   # unacceptable latency


@dataclass
class ToolLatencyThresholds:
    tool_name: str
    warning_ms: float = 500.0
    slow_ms: float = 2000.0
    critical_ms: float = 10000.0

    def classify(self, latency_ms: float) -> LatencyTier:
        if latency_ms >= self.critical_ms:
            return LatencyTier.CRITICAL
        if latency_ms >= self.slow_ms:
            return LatencyTier.SLOW
        if latency_ms >= self.warning_ms:
            return LatencyTier.WARNING
        return LatencyTier.FAST


@dataclass
class ToolLatencyThresholdRegistry:
    _thresholds: Dict[str, ToolLatencyThresholds] = field(default_factory=dict)
    _default: ToolLatencyThresholds = field(
        default_factory=lambda: ToolLatencyThresholds(tool_name="__default__")
    )

    def register(self, thresholds: ToolLatencyThresholds) -> None:
        self._thresholds[thresholds.tool_name] = thresholds

    def get(self, tool_name: str) -> ToolLatencyThresholds:
        return self._thresholds.get(tool_name, self._default)
```

## Solution 2: Slow Query Recorder

```python
import time
from collections import deque
from dataclasses import dataclass, field
from threading import Lock
from typing import Any, Deque, Dict, List, Optional


@dataclass
class SlowQueryRecord:
    tool_name: str
    latency_ms: float
    tier: LatencyTier
    recorded_at: float
    args_summary: str = ""    # truncated, non-sensitive representation
    error: Optional[str] = None
    session_id: str = ""


class SlowQueryRecorder:
    """
    Accumulates slow query records (WARNING tier and above) in a bounded
    ring buffer. Supports per-tool and aggregate queries.
    """

    def __init__(self, max_records: int = 2000):
        self._max = max_records
        self._records: Deque[SlowQueryRecord] = deque()
        self._lock = Lock()

    def record(self, rec: SlowQueryRecord) -> None:
        if rec.tier == LatencyTier.FAST:
            return
        with self._lock:
            self._records.append(rec)
            if len(self._records) > self._max:
                self._records.popleft()

    def recent(
        self,
        window_seconds: float = 3600.0,
        min_tier: LatencyTier = LatencyTier.WARNING,
    ) -> List[SlowQueryRecord]:
        cutoff = time.time() - window_seconds
        tier_order = [LatencyTier.FAST, LatencyTier.WARNING, LatencyTier.SLOW, LatencyTier.CRITICAL]
        min_idx = tier_order.index(min_tier)
        with self._lock:
            return [
                r for r in self._records
                if r.recorded_at >= cutoff
                and tier_order.index(r.tier) >= min_idx
            ]

    def per_tool_summary(self, window_seconds: float = 3600.0) -> Dict[str, dict]:
        records = self.recent(window_seconds, LatencyTier.WARNING)
        summary: Dict[str, dict] = {}
        for r in records:
            entry = summary.setdefault(r.tool_name, {
                "warning": 0, "slow": 0, "critical": 0, "max_ms": 0.0,
            })
            entry[r.tier.value] = entry.get(r.tier.value, 0) + 1
            entry["max_ms"] = max(entry["max_ms"], r.latency_ms)
        return summary
```

## Solution 3: Slow Query Detecting Tool Wrapper

```python
import asyncio
import time
from typing import Any, Callable, Optional


class SlowQueryDetectingToolWrapper:
    """
    Wraps any async tool call. Measures latency, classifies it,
    records slow queries, and optionally invokes an alert callback
    for CRITICAL tier calls.
    """

    def __init__(
        self,
        registry: ToolLatencyThresholdRegistry,
        recorder: SlowQueryRecorder,
        alert_fn: Optional[Callable[[SlowQueryRecord], None]] = None,
    ):
        self._registry = registry
        self._recorder = recorder
        self._alert = alert_fn

    async def call(
        self,
        tool_name: str,
        tool_fn: Callable,
        *args: Any,
        session_id: str = "",
        args_summary: str = "",
        **kwargs: Any,
    ) -> Any:
        thresholds = self._registry.get(tool_name)
        start = time.time()
        error_msg: Optional[str] = None

        try:
            result = await tool_fn(*args, **kwargs)
            return result
        except Exception as exc:
            error_msg = type(exc).__name__
            raise
        finally:
            latency_ms = round((time.time() - start) * 1000, 2)
            tier = thresholds.classify(latency_ms)
            rec = SlowQueryRecord(
                tool_name=tool_name,
                latency_ms=latency_ms,
                tier=tier,
                recorded_at=time.time(),
                args_summary=args_summary[:200],
                error=error_msg,
                session_id=session_id,
            )
            self._recorder.record(rec)
            if tier == LatencyTier.CRITICAL and self._alert:
                self._alert(rec)
```

## Solution 4: Slow Query Pattern Analyzer

```python
import re
from collections import defaultdict
from typing import Dict, List, Tuple


class SlowQueryPatternAnalyzer:
    """
    Groups slow query records by normalized args_summary pattern
    to surface which query shapes are consistently slow.
    """

    @staticmethod
    def _normalize(args_summary: str) -> str:
        # Replace numeric literals with ?
        s = re.sub(r"\b\d+\b", "?", args_summary)
        # Replace quoted strings with '?'
        s = re.sub(r"'[^']*'", "'?'", s)
        s = re.sub(r'"[^"]*"', '"?"', s)
        return s.strip()

    def analyze(self, records: List[SlowQueryRecord]) -> List[dict]:
        pattern_map: Dict[str, List[float]] = defaultdict(list)
        for rec in records:
            pattern = self._normalize(rec.args_summary)
            key = f"{rec.tool_name}::{pattern}"
            pattern_map[key].append(rec.latency_ms)

        results = []
        for key, latencies in pattern_map.items():
            tool, _, pattern = key.partition("::")
            results.append({
                "tool_name": tool,
                "pattern": pattern,
                "occurrence_count": len(latencies),
                "avg_ms": round(sum(latencies) / len(latencies), 2),
                "max_ms": round(max(latencies), 2),
                "p95_ms": round(sorted(latencies)[int(len(latencies) * 0.95)], 2),
            })

        return sorted(results, key=lambda r: r["max_ms"], reverse=True)
```

## Solution 5: Slow Query Regression Alerter

```python
import time
from typing import Dict, Optional


class SlowQueryRegressionAlerter:
    """
    Compares per-tool slow query rates between a baseline window and
    a recent window. Fires when the recent slow rate exceeds the
    baseline by more than the regression_factor.
    """

    def __init__(
        self,
        recorder: SlowQueryRecorder,
        regression_factor: float = 2.0,
        min_recent_slow_count: int = 5,
    ):
        self._recorder = recorder
        self._factor = regression_factor
        self._min_count = min_recent_slow_count
        self._last_alert: Dict[str, float] = {}
        self._cooldown_seconds = 300.0

    def check(
        self,
        baseline_window_seconds: float = 86400.0,
        recent_window_seconds: float = 600.0,
    ) -> List[dict]:
        baseline = self._recorder.per_tool_summary(baseline_window_seconds)
        recent = self._recorder.per_tool_summary(recent_window_seconds)

        alerts = []
        now = time.time()

        for tool_name, recent_data in recent.items():
            recent_slow = recent_data.get("slow", 0) + recent_data.get("critical", 0)
            if recent_slow < self._min_count:
                continue

            baseline_data = baseline.get(tool_name, {})
            baseline_slow = baseline_data.get("slow", 0) + baseline_data.get("critical", 0)
            baseline_rate = baseline_slow / max(baseline_window_seconds / recent_window_seconds, 1)

            if baseline_rate == 0 or recent_slow >= baseline_rate * self._factor:
                last = self._last_alert.get(tool_name, 0)
                if now - last >= self._cooldown_seconds:
                    self._last_alert[tool_name] = now
                    alerts.append({
                        "tool_name": tool_name,
                        "recent_slow_count": recent_slow,
                        "baseline_rate_per_window": round(baseline_rate, 2),
                        "regression_factor": self._factor,
                        "max_recent_ms": recent_data.get("max_ms", 0),
                    })

        return alerts
```

## Solution 6: Slow Query Detection Dashboard

```python
import time
from typing import Optional


class SlowQueryDetectionDashboard:
    """
    Combines slow query summaries, pattern analysis, and regression
    alerts into a single operational report.
    """

    def __init__(
        self,
        recorder: SlowQueryRecorder,
        analyzer: SlowQueryPatternAnalyzer,
        alerter: SlowQueryRegressionAlerter,
    ):
        self._recorder = recorder
        self._analyzer = analyzer
        self._alerter = alerter

    def render(self, window_seconds: float = 3600.0) -> dict:
        recent_records = self._recorder.recent(window_seconds, LatencyTier.WARNING)
        return {
            "generated_at": time.time(),
            "window_seconds": window_seconds,
            "slow_query_count": len(recent_records),
            "critical_count": sum(
                1 for r in recent_records if r.tier == LatencyTier.CRITICAL
            ),
            "per_tool_summary": self._recorder.per_tool_summary(window_seconds),
            "top_slow_patterns": self._analyzer.analyze(recent_records)[:10],
            "regression_alerts": self._alerter.check(
                baseline_window_seconds=86400.0,
                recent_window_seconds=600.0,
            ),
        }
```

## Comparison

| Approach | Latency Classification | Slow Record Storage | Pattern Analysis | Regression Detection | Alert Callback |
|---|---|---|---|---|---|
| ToolLatencyThresholdRegistry | Yes (4 tiers) | No | No | No | No |
| SlowQueryRecorder | No | Yes (ring buffer) | No | No | No |
| SlowQueryDetectingToolWrapper | Via registry | Via recorder | No | No | Yes (CRITICAL) |
| SlowQueryPatternAnalyzer | No | No | Yes (normalize) | No | No |
| SlowQueryRegressionAlerter | No | Via recorder | No | Yes | No |
| SlowQueryDetectionDashboard | No | No | No | No | No |

**Best for production**: Set per-tool thresholds based on observed P95 from the first week of operation — a `vector_search` tool with P95 of 200 ms should have `warning_ms=400` and `slow_ms=1000`, while a `key_value_lookup` tool with P95 of 5 ms should have `warning_ms=50` and `slow_ms=200`. Use `SlowQueryPatternAnalyzer` to identify query shapes that appear repeatedly in slow records — these are candidates for index creation or query rewriting. Set `regression_factor=2.0` in `SlowQueryRegressionAlerter` so a doubling of slow query rate in any 10-minute window fires an alert before users notice tail latency degradation.
