---
title: "Agent Doesn't Implement Tool Dependency Latency Attribution"
description: "Agents that measure total request latency but not per-dependency latency cannot answer why a request was slow: the total latency of 2 seconds could be 1.8 seconds of database query plus 0.2 seconds of agent processing, or 1.8 seconds of LLM inference plus 0.2 seconds of retrieval. Implement tool dependency latency attribution that records how much time each downstream dependency contributed to the total request latency, enabling targeted optimization of the slowest components."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-tool-dependency-latency-attribution
tags: [latency-attribution, dependency-profiling, waterfall-analysis, bottleneck-detection, distributed-tracing, tool-latency]
symptoms:
  - "Total request latency is measured but which dependency caused slowness is unknown"
  - "P99 latency spikes cannot be attributed to a specific tool or service"
  - "All tools are optimized equally when one tool dominates 80% of total latency"
  - "No waterfall view exists showing which tool calls overlapped vs were sequential"
  - "Dependency latency data is scattered across individual tool logs with no aggregation"
---

## Why This Happens

Total latency is easy to measure: record a timestamp before the request and another after. Attribution is harder: it requires recording timestamps for each tool call within the request, computing how much time was spent in each dependency, and aggregating this across requests to find the slow path. Without attribution, optimization is guesswork — teams spend time optimizing the wrong component. Latency attribution produces a breakdown of total request time into dependency-attributed slices, identifying which dependency is the critical path and by how much.

## Solution 1: Dependency Span

```python
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class DependencySpan:
    span_id: str
    dependency_name: str
    operation: str
    started_at: float
    ended_at: Optional[float] = None
    latency_ms: Optional[float] = None
    is_error: bool = False
    is_cached: bool = False
    parent_span_id: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def start(
        cls,
        dependency_name: str,
        operation: str,
        parent_span_id: str = "",
    ) -> "DependencySpan":
        return cls(
            span_id=str(uuid.uuid4())[:12],
            dependency_name=dependency_name,
            operation=operation,
            started_at=time.time(),
            parent_span_id=parent_span_id,
        )

    def finish(self, is_error: bool = False, is_cached: bool = False) -> None:
        self.ended_at = time.time()
        self.latency_ms = round((self.ended_at - self.started_at) * 1000, 2)
        self.is_error = is_error
        self.is_cached = is_cached
```

## Solution 2: Request Latency Profile

```python
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class RequestLatencyProfile:
    request_id: str
    session_id: str
    started_at: float
    ended_at: Optional[float] = None
    spans: List[DependencySpan] = field(default_factory=list)
    total_latency_ms: Optional[float] = None

    @classmethod
    def create(cls, session_id: str = "") -> "RequestLatencyProfile":
        return cls(
            request_id=str(uuid.uuid4())[:12],
            session_id=session_id,
            started_at=time.time(),
        )

    def add_span(self, span: DependencySpan) -> None:
        self.spans.append(span)

    def finish(self) -> None:
        self.ended_at = time.time()
        self.total_latency_ms = round((self.ended_at - self.started_at) * 1000, 2)

    def attribution(self) -> Dict[str, float]:
        """Returns {dependency_name: attributed_ms} for all spans."""
        result: Dict[str, float] = {}
        for span in self.spans:
            if span.latency_ms is not None and not span.is_cached:
                result[span.dependency_name] = result.get(span.dependency_name, 0.0) + span.latency_ms
        return result

    def critical_path_dependency(self) -> Optional[str]:
        attr = self.attribution()
        if not attr:
            return None
        return max(attr, key=attr.get)

    def unattributed_ms(self) -> float:
        if self.total_latency_ms is None:
            return 0.0
        attributed = sum(self.attribution().values())
        return max(0.0, round(self.total_latency_ms - attributed, 2))
```

## Solution 3: Latency Attribution Recorder

```python
import threading
import time
from collections import defaultdict, deque
from typing import Deque, Dict, List, Optional, Tuple


class LatencyAttributionRecorder:
    """
    Accumulates request latency profiles and computes per-dependency
    latency statistics across all recorded requests.
    """

    def __init__(self, max_profiles: int = 5000, window_seconds: float = 3600.0):
        self._max = max_profiles
        self._window = window_seconds
        self._profiles: Deque[Tuple[float, RequestLatencyProfile]] = deque()
        self._lock = threading.Lock()

    def record(self, profile: RequestLatencyProfile) -> None:
        if profile.total_latency_ms is None:
            return
        with self._lock:
            self._profiles.append((time.time(), profile))
            if len(self._profiles) > self._max:
                self._profiles.popleft()

    def dependency_stats(self, window_seconds: Optional[float] = None) -> Dict[str, dict]:
        cutoff = time.time() - (window_seconds or self._window)
        with self._lock:
            recent = [p for ts, p in self._profiles if ts >= cutoff]

        if not recent:
            return {}

        dep_latencies: Dict[str, List[float]] = defaultdict(list)
        dep_errors: Dict[str, int] = defaultdict(int)
        dep_request_counts: Dict[str, int] = defaultdict(int)

        for profile in recent:
            for span in profile.spans:
                if span.latency_ms is not None:
                    dep_latencies[span.dependency_name].append(span.latency_ms)
                    dep_request_counts[span.dependency_name] += 1
                    if span.is_error:
                        dep_errors[span.dependency_name] += 1

        result = {}
        for dep, lats in dep_latencies.items():
            sorted_lats = sorted(lats)
            n = len(sorted_lats)
            result[dep] = {
                "call_count": dep_request_counts[dep],
                "error_count": dep_errors[dep],
                "avg_ms": round(sum(lats) / n, 2),
                "p50_ms": sorted_lats[n // 2],
                "p95_ms": sorted_lats[min(int(n * 0.95), n - 1)],
                "p99_ms": sorted_lats[min(int(n * 0.99), n - 1)],
                "total_ms": round(sum(lats), 2),
            }
        return result

    def critical_path_distribution(self, window_seconds: Optional[float] = None) -> Dict[str, int]:
        cutoff = time.time() - (window_seconds or self._window)
        with self._lock:
            recent = [p for ts, p in self._profiles if ts >= cutoff]

        counts: Dict[str, int] = defaultdict(int)
        for profile in recent:
            cp = profile.critical_path_dependency()
            if cp:
                counts[cp] += 1
        return dict(counts)
```

## Solution 4: Instrumented Tool Wrapper

```python
import asyncio
import time
from typing import Any, Callable, Optional


class InstrumentedToolWrapper:
    """
    Wraps a tool function with span recording for latency attribution.
    """

    def __init__(
        self,
        dependency_name: str,
        recorder: LatencyAttributionRecorder,
    ):
        self._dep = dependency_name
        self._recorder = recorder

    async def call(
        self,
        tool_fn: Callable,
        profile: RequestLatencyProfile,
        operation: str = "call",
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        span = DependencySpan.start(
            dependency_name=self._dep,
            operation=operation,
            parent_span_id=profile.request_id,
        )
        is_error = False
        try:
            result = await tool_fn(*args, **kwargs)
            return result
        except Exception:
            is_error = True
            raise
        finally:
            span.finish(is_error=is_error)
            profile.add_span(span)
```

## Solution 5: Latency Bottleneck Detector

```python
from typing import List, Optional


class LatencyBottleneckDetector:
    """
    Identifies dependencies that consistently dominate request latency
    and are candidates for optimization.
    """

    def __init__(
        self,
        recorder: LatencyAttributionRecorder,
        bottleneck_fraction: float = 0.50,   # dep consuming >50% of total = bottleneck
    ):
        self._recorder = recorder
        self._threshold = bottleneck_fraction

    def find_bottlenecks(self, window_seconds: float = 3600.0) -> List[dict]:
        stats = self._recorder.dependency_stats(window_seconds)
        cp_dist = self._recorder.critical_path_distribution(window_seconds)
        total_cp = sum(cp_dist.values()) or 1

        bottlenecks = []
        for dep, s in stats.items():
            cp_rate = cp_dist.get(dep, 0) / total_cp
            if cp_rate >= self._threshold:
                bottlenecks.append({
                    "dependency": dep,
                    "critical_path_rate": round(cp_rate, 4),
                    "p95_ms": s["p95_ms"],
                    "p99_ms": s["p99_ms"],
                    "call_count": s["call_count"],
                    "recommendation": f"critical path {cp_rate*100:.0f}% of requests — optimize p95 ({s['p95_ms']}ms)",
                })

        return sorted(bottlenecks, key=lambda b: b["critical_path_rate"], reverse=True)
```

## Solution 6: Latency Attribution Dashboard

```python
import time


class LatencyAttributionDashboard:
    """
    Combines per-dependency stats, critical path distribution,
    and bottleneck analysis into a single performance view.
    """

    def __init__(
        self,
        recorder: LatencyAttributionRecorder,
        bottleneck_detector: LatencyBottleneckDetector,
    ):
        self._recorder = recorder
        self._detector = bottleneck_detector

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "dependency_stats": self._recorder.dependency_stats(window_seconds=3600.0),
            "critical_path_distribution": self._recorder.critical_path_distribution(3600.0),
            "bottlenecks": self._detector.find_bottlenecks(window_seconds=3600.0),
        }
```

## Comparison

| Approach | Per-Span Timing | Attribution Breakdown | Critical Path | Bottleneck Detection | Dashboard |
|---|---|---|---|---|---|
| DependencySpan | Yes (start/finish) | No | No | No | No |
| RequestLatencyProfile | Via spans | Yes (per-dep) | Yes | No | No |
| LatencyAttributionRecorder | No | Yes (aggregate) | Yes (distribution) | No | No |
| InstrumentedToolWrapper | Yes (auto span) | No | No | No | No |
| LatencyBottleneckDetector | No | No | Via recorder | Yes | No |
| LatencyAttributionDashboard | No | No | No | No | Yes |

**Best for production**: Instrument every tool call with `InstrumentedToolWrapper` — the overhead is a few microseconds of timing bookkeeping per call, negligible compared to the tool's own latency. Focus optimization effort on the dependency with the highest `critical_path_rate` rather than the highest average latency: a dependency with 500ms average that is the critical path 80% of the time contributes more to user-perceived latency than a 2000ms dependency that only runs in 5% of requests. Set a latency attribution SLO for the top dependency (e.g., database P95 < 200ms) and alert when the recorded P95 from `LatencyAttributionRecorder` exceeds it.
