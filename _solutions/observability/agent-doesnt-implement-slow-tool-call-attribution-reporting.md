---
title: "Agent Doesn't Implement Slow Tool Call Attribution Reporting"
description: "Agents that measure only end-to-end request latency cannot explain which tool call caused a slow response. When P99 latency spikes, engineers have no way to determine whether the bottleneck is the LLM, a database lookup, an HTTP fetch, or a computation tool. Implement slow tool call attribution that captures per-call latency, identifies outliers relative to each tool's baseline, and surfaces the top contributors to high-latency requests."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-slow-tool-call-attribution-reporting
tags: [latency-attribution, slow-tool-detection, p99-analysis, bottleneck-identification, tool-profiling, request-tracing]
symptoms:
  - "P99 request latency is 8 seconds but engineers cannot identify which tool caused it"
  - "No per-tool latency breakdown in traces — only aggregate request duration"
  - "Slow tool calls are invisible until the user complains about response time"
  - "Cannot determine if a latency regression was caused by a tool or the LLM"
  - "Tool latency outliers (5× baseline) occur regularly but are never investigated"
---

## Why This Happens

End-to-end latency is easy to measure. Per-tool attribution requires instrumenting every tool call individually, storing the result against the request that triggered it, and comparing individual call durations against each tool's historical baseline. Without per-call records attached to the request, a slow response looks like a black box — the total duration is known but the breakdown is not. Attribution also requires detecting outliers relative to tool-specific baselines rather than an absolute threshold, since a 3-second call is normal for a database-heavy tool but anomalous for a cache lookup.

## Solution 1: Tool Call Span

```python
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ToolCallSpan:
    tool_name: str
    request_id: str
    started_at: float = field(default_factory=time.time)
    ended_at: Optional[float] = None
    duration_ms: Optional[float] = None
    succeeded: bool = True
    error_type: Optional[str] = None

    def finish(self, succeeded: bool = True, error_type: Optional[str] = None) -> None:
        self.ended_at = time.time()
        self.duration_ms = round((self.ended_at - self.started_at) * 1000, 2)
        self.succeeded = succeeded
        self.error_type = error_type

    def is_complete(self) -> bool:
        return self.duration_ms is not None
```

## Solution 2: Per-Tool Baseline Tracker

```python
from collections import deque
from threading import Lock
from typing import Deque, Dict, Optional, Tuple
import time


class PerToolBaselineTracker:
    """
    Maintains a rolling window of latency samples per tool.
    Provides mean and P95 baselines used for outlier detection.
    """

    def __init__(self, window_seconds: float = 600.0, max_samples: int = 500):
        self._window = window_seconds
        self._max = max_samples
        self._samples: Dict[str, Deque[Tuple[float, float]]] = {}
        # (recorded_at, duration_ms)
        self._lock = Lock()

    def record(self, tool_name: str, duration_ms: float) -> None:
        with self._lock:
            if tool_name not in self._samples:
                self._samples[tool_name] = deque(maxlen=self._max)
            self._samples[tool_name].append((time.time(), duration_ms))

    def _recent_values(self, tool_name: str) -> list:
        cutoff = time.time() - self._window
        samples = self._samples.get(tool_name, deque())
        return [ms for ts, ms in samples if ts >= cutoff]

    def mean(self, tool_name: str) -> Optional[float]:
        values = self._recent_values(tool_name)
        if not values:
            return None
        return round(sum(values) / len(values), 2)

    def percentile(self, tool_name: str, pct: float) -> Optional[float]:
        values = sorted(self._recent_values(tool_name))
        if not values:
            return None
        idx = min(int(len(values) * pct / 100.0), len(values) - 1)
        return round(values[idx], 2)

    def sample_count(self, tool_name: str) -> int:
        return len(self._recent_values(tool_name))
```

## Solution 3: Slow Call Detector

```python
from dataclasses import dataclass
from typing import Optional


@dataclass
class SlowCallVerdict:
    tool_name: str
    duration_ms: float
    baseline_p95_ms: Optional[float]
    slowdown_factor: Optional[float]
    is_slow: bool
    reason: str


class SlowCallDetector:
    """
    Compares a tool call duration against its tool-specific P95 baseline.
    Classifies a call as slow if it exceeds baseline × multiplier.
    Falls back to an absolute threshold when baseline is unavailable.
    """

    def __init__(
        self,
        baseline_tracker: PerToolBaselineTracker,
        slowdown_multiplier: float = 3.0,
        absolute_slow_ms: float = 5000.0,
        min_baseline_samples: int = 10,
    ):
        self._tracker = baseline_tracker
        self._multiplier = slowdown_multiplier
        self._absolute = absolute_slow_ms
        self._min_samples = min_baseline_samples

    def evaluate(self, span: ToolCallSpan) -> SlowCallVerdict:
        if not span.is_complete():
            return SlowCallVerdict(
                tool_name=span.tool_name,
                duration_ms=0.0,
                baseline_p95_ms=None,
                slowdown_factor=None,
                is_slow=False,
                reason="incomplete",
            )

        duration = span.duration_ms
        sample_count = self._tracker.sample_count(span.tool_name)

        if sample_count < self._min_samples:
            is_slow = duration >= self._absolute
            return SlowCallVerdict(
                tool_name=span.tool_name,
                duration_ms=duration,
                baseline_p95_ms=None,
                slowdown_factor=None,
                is_slow=is_slow,
                reason=f"absolute_threshold ({self._absolute}ms)" if is_slow else "ok_no_baseline",
            )

        p95 = self._tracker.percentile(span.tool_name, 95)
        threshold = (p95 or self._absolute) * self._multiplier
        slowdown = round(duration / p95, 2) if p95 else None
        is_slow = duration >= threshold

        return SlowCallVerdict(
            tool_name=span.tool_name,
            duration_ms=duration,
            baseline_p95_ms=p95,
            slowdown_factor=slowdown,
            is_slow=is_slow,
            reason=f"{slowdown}× P95 baseline" if is_slow else "ok",
        )
```

## Solution 4: Request Latency Attributor

```python
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class RequestLatencyProfile:
    request_id: str
    total_duration_ms: float
    spans: List[ToolCallSpan]
    slow_calls: List[SlowCallVerdict]
    top_contributor: Optional[str]
    tool_share_pct: float    # % of total latency accounted for by tool calls


class RequestLatencyAttributor:
    """
    Accumulates all tool call spans for a request and produces
    a latency profile attributing total duration to specific tools.
    """

    def __init__(
        self,
        detector: SlowCallDetector,
        baseline_tracker: PerToolBaselineTracker,
    ):
        self._detector = detector
        self._tracker = baseline_tracker

    def attribute(
        self,
        request_id: str,
        spans: List[ToolCallSpan],
        request_duration_ms: float,
    ) -> RequestLatencyProfile:
        slow_calls = []
        for span in spans:
            if span.is_complete():
                self._tracker.record(span.tool_name, span.duration_ms)
                verdict = self._detector.evaluate(span)
                if verdict.is_slow:
                    slow_calls.append(verdict)

        total_tool_ms = sum(s.duration_ms for s in spans if s.is_complete())
        top_span = max(
            (s for s in spans if s.is_complete()),
            key=lambda s: s.duration_ms,
            default=None,
        )
        tool_share = round(
            total_tool_ms / request_duration_ms * 100 if request_duration_ms > 0 else 0.0, 1
        )

        return RequestLatencyProfile(
            request_id=request_id,
            total_duration_ms=request_duration_ms,
            spans=spans,
            slow_calls=slow_calls,
            top_contributor=top_span.tool_name if top_span else None,
            tool_share_pct=tool_share,
        )
```

## Solution 5: Slow Call Aggregator

```python
import time
from collections import deque
from threading import Lock
from typing import Deque, Dict, List


class SlowCallAggregator:
    """
    Accumulates slow call verdicts fleet-wide and provides
    per-tool slow call rates for SLO tracking and investigation triage.
    """

    def __init__(self, window_seconds: float = 3600.0, max_records: int = 20_000):
        self._window = window_seconds
        self._max = max_records
        self._records: Deque[dict] = deque()
        self._lock = Lock()

    def record_profile(self, profile: RequestLatencyProfile) -> None:
        ts = time.time()
        with self._lock:
            for verdict in profile.slow_calls:
                self._records.append({
                    "ts": ts,
                    "tool_name": verdict.tool_name,
                    "duration_ms": verdict.duration_ms,
                    "slowdown_factor": verdict.slowdown_factor,
                    "request_id": profile.request_id,
                })
                if len(self._records) > self._max:
                    self._records.popleft()

    def top_slow_tools(self, n: int = 10) -> List[dict]:
        cutoff = time.time() - self._window
        with self._lock:
            recent = [r for r in self._records if r["ts"] >= cutoff]

        counts: Dict[str, list] = {}
        for r in recent:
            counts.setdefault(r["tool_name"], []).append(r["duration_ms"])

        return sorted(
            [
                {
                    "tool_name": tool,
                    "slow_call_count": len(durations),
                    "avg_slow_duration_ms": round(sum(durations) / len(durations), 1),
                    "max_duration_ms": round(max(durations), 1),
                }
                for tool, durations in counts.items()
            ],
            key=lambda x: -x["slow_call_count"],
        )[:n]
```

## Solution 6: Slow Tool Attribution Dashboard

```python
import time
from typing import Optional


class SlowToolAttributionDashboard:
    """
    Renders per-tool baselines, slow call rates, and the top contributors
    to high-latency requests for investigation and SLO tuning.
    """

    def __init__(
        self,
        baseline_tracker: PerToolBaselineTracker,
        aggregator: SlowCallAggregator,
    ):
        self._tracker = baseline_tracker
        self._aggregator = aggregator

    def render(self, tool_names: Optional[list] = None) -> dict:
        baselines = {}
        tools = tool_names or list(self._tracker._samples.keys())
        for tool in tools:
            baselines[tool] = {
                "p50_ms": self._tracker.percentile(tool, 50),
                "p95_ms": self._tracker.percentile(tool, 95),
                "mean_ms": self._tracker.mean(tool),
                "sample_count": self._tracker.sample_count(tool),
            }

        return {
            "generated_at": time.time(),
            "baselines": baselines,
            "top_slow_tools_1h": self._aggregator.top_slow_tools(10),
        }
```

## Comparison

| Approach | Per-Call Timing | Baseline Comparison | Outlier Detection | Request Attribution | Aggregation |
|---|---|---|---|---|---|
| ToolCallSpan | Yes | No | No | No | No |
| PerToolBaselineTracker | Via record() | Yes (P95) | No | No | No |
| SlowCallDetector | Via span | Via tracker | Yes (3× P95) | No | No |
| RequestLatencyAttributor | Via spans | Via detector | Via detector | Yes | No |
| SlowCallAggregator | No | No | No | No | Yes (fleet) |
| SlowToolAttributionDashboard | No | Via tracker | No | No | Via aggregator |

**Best for production**: Record every `ToolCallSpan` to `PerToolBaselineTracker` regardless of whether the call was slow — the baseline is only useful if it reflects normal behavior, and that requires recording successes. Set `slowdown_multiplier=3.0` as the outlier threshold: calls at 3× P95 are genuinely anomalous, not just variance. Emit `slow_calls` from `RequestLatencyProfile` as a structured log field on every request so that slow call attribution is searchable in your log platform without deploying new instrumentation. Alert when any single tool accounts for more than 5 slow calls per minute — this indicates a systemic degradation in that tool's dependency, not a random spike.
