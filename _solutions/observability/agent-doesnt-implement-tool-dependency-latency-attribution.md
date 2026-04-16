---
title: "Agent Doesn't Implement Tool Dependency Latency Attribution"
description: "Agents that report only total session latency cannot answer 'which tool is making us slow?' or 'is the database slower than the search API?'. Implement tool dependency latency attribution that measures and accumulates wall-clock time per dependency, computes the fraction of total latency each dependency accounts for, and identifies which dependencies are on the critical path versus running in parallel."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-tool-dependency-latency-attribution
tags: [latency-attribution, tool-latency, dependency-profiling, critical-path, performance-analysis, tracing]
symptoms:
  - "Total session latency is high but no breakdown shows which tool is responsible"
  - "Cannot tell whether slow sessions are caused by a specific external API or the LLM itself"
  - "Parallel tool calls are not distinguished from sequential ones in latency reports"
  - "SLO violation investigation starts from scratch because per-dependency timing is not recorded"
  - "No critical path analysis — optimizing non-bottleneck tools wastes effort"
---

## Why This Happens

Most agents record total request time but not per-tool time. When multiple tools are called, their individual durations are never aggregated. Without attribution, every latency investigation requires manually reviewing logs and computing durations from timestamps. Attribution requires instrumenting each tool call with a start time, end time, and dependency label, then computing the critical path (the longest sequential chain of dependencies) to identify the bottleneck.

## Solution 1: Tool Call Timing Record

```python
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class ExecutionMode(str, Enum):
    SEQUENTIAL = "sequential"
    PARALLEL = "parallel"
    UNKNOWN = "unknown"


@dataclass
class ToolCallTimingRecord:
    call_id: str
    tool_name: str
    dependency_type: str        # "llm" | "database" | "search" | "http" | "cache" | custom
    started_at: float
    ended_at: Optional[float] = None
    duration_ms: Optional[float] = None
    parent_call_id: Optional[str] = None
    parallel_group: Optional[str] = None   # calls in same group ran concurrently
    error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def finish(self, error: Optional[str] = None) -> None:
        self.ended_at = time.time()
        self.duration_ms = (self.ended_at - self.started_at) * 1000
        self.error = error

    @property
    def is_complete(self) -> bool:
        return self.ended_at is not None

    @property
    def had_error(self) -> bool:
        return self.error is not None
```

## Solution 2: Session Latency Profiler

```python
import time
import uuid
from collections import defaultdict
from contextlib import contextmanager
from typing import Dict, Generator, List, Optional


class SessionLatencyProfiler:
    """
    Instruments tool calls within a session with microsecond-resolution timing.
    Tracks call hierarchy (parent/child) and parallel groups.
    """

    def __init__(self, session_id: str):
        self.session_id = session_id
        self.session_started_at = time.time()
        self._records: List[ToolCallTimingRecord] = []
        self._active_parallel_group: Optional[str] = None

    @contextmanager
    def measure(
        self,
        tool_name: str,
        dependency_type: str = "unknown",
        parent_call_id: Optional[str] = None,
    ) -> Generator[ToolCallTimingRecord, None, None]:
        call_id = str(uuid.uuid4())[:8]
        record = ToolCallTimingRecord(
            call_id=call_id,
            tool_name=tool_name,
            dependency_type=dependency_type,
            started_at=time.time(),
            parent_call_id=parent_call_id,
            parallel_group=self._active_parallel_group,
        )
        self._records.append(record)
        try:
            yield record
            record.finish()
        except Exception as exc:
            record.finish(error=str(exc))
            raise

    @contextmanager
    def parallel_group(self) -> Generator[str, None, None]:
        group_id = str(uuid.uuid4())[:8]
        self._active_parallel_group = group_id
        try:
            yield group_id
        finally:
            self._active_parallel_group = None

    @property
    def session_duration_ms(self) -> float:
        return (time.time() - self.session_started_at) * 1000

    def all_records(self) -> List[ToolCallTimingRecord]:
        return list(self._records)
```

## Solution 3: Latency Attribution Analyzer

```python
from dataclasses import dataclass
from typing import Dict, List


@dataclass
class DependencyLatencyAttribution:
    dependency_type: str
    tool_calls: List[str]
    total_duration_ms: float
    call_count: int
    avg_duration_ms: float
    max_duration_ms: float
    error_count: int
    pct_of_session: float
    is_parallel: bool


class LatencyAttributionAnalyzer:
    """
    Aggregates tool call timing records into per-dependency attribution.
    Distinguishes sequential time (adds to session latency) from parallel
    time (overlaps — does not add to session latency directly).
    """

    def __init__(self, profiler: SessionLatencyProfiler):
        self._profiler = profiler

    def attribute(self) -> List[DependencyLatencyAttribution]:
        records = [r for r in self._profiler.all_records() if r.is_complete]
        if not records:
            return []

        session_ms = self._profiler.session_duration_ms
        by_dep: Dict[str, List[ToolCallTimingRecord]] = {}
        for r in records:
            by_dep.setdefault(r.dependency_type, []).append(r)

        attributions = []
        for dep_type, dep_records in by_dep.items():
            total_ms = sum(r.duration_ms for r in dep_records)
            max_ms = max(r.duration_ms for r in dep_records)
            errors = sum(1 for r in dep_records if r.had_error)
            is_parallel = any(r.parallel_group is not None for r in dep_records)

            attributions.append(DependencyLatencyAttribution(
                dependency_type=dep_type,
                tool_calls=[r.tool_name for r in dep_records],
                total_duration_ms=round(total_ms, 2),
                call_count=len(dep_records),
                avg_duration_ms=round(total_ms / len(dep_records), 2),
                max_duration_ms=round(max_ms, 2),
                error_count=errors,
                pct_of_session=round(total_ms / max(session_ms, 1) * 100, 2),
                is_parallel=is_parallel,
            ))

        return sorted(attributions, key=lambda a: -a.total_duration_ms)

    def top_bottleneck(self) -> str:
        attrs = self.attribute()
        if not attrs:
            return "unknown"
        sequential = [a for a in attrs if not a.is_parallel]
        source = sequential if sequential else attrs
        return source[0].dependency_type if source else "unknown"
```

## Solution 4: Critical Path Detector

```python
from dataclasses import dataclass
from typing import List, Optional


@dataclass
class CriticalPathSegment:
    tool_name: str
    dependency_type: str
    duration_ms: float
    call_id: str


class CriticalPathDetector:
    """
    Identifies the critical path in a session — the longest chain of
    sequential tool calls that determined the overall session duration.
    Parallel tool calls contribute their max duration, not their sum.
    """

    def detect(
        self, profiler: SessionLatencyProfiler
    ) -> List[CriticalPathSegment]:
        records = [r for r in profiler.all_records() if r.is_complete]
        if not records:
            return []

        # Group parallel records: take only the longest from each group
        processed: List[ToolCallTimingRecord] = []
        seen_groups = set()
        for r in records:
            if r.parallel_group:
                if r.parallel_group in seen_groups:
                    continue
                group_recs = [x for x in records if x.parallel_group == r.parallel_group]
                longest = max(group_recs, key=lambda x: x.duration_ms)
                processed.append(longest)
                seen_groups.add(r.parallel_group)
            else:
                processed.append(r)

        # Sort by start time — critical path is the sequential chain
        sequential = sorted(
            [r for r in processed if r.parent_call_id is None],
            key=lambda r: r.started_at,
        )

        return [
            CriticalPathSegment(
                tool_name=r.tool_name,
                dependency_type=r.dependency_type,
                duration_ms=round(r.duration_ms, 2),
                call_id=r.call_id,
            )
            for r in sequential
        ]

    def critical_path_duration_ms(self, profiler: SessionLatencyProfiler) -> float:
        return sum(s.duration_ms for s in self.detect(profiler))
```

## Solution 5: Fleet-Level Attribution Aggregator

```python
import time
from collections import defaultdict
from typing import Dict, List


class FleetLatencyAttributionAggregator:
    """
    Accumulates attribution data across many sessions to compute
    fleet-level statistics: which dependency is slowest on average,
    which accounts for the largest fraction of total fleet latency.
    """

    def __init__(self, window_seconds: float = 3600.0):
        self._window = window_seconds
        self._records: List[tuple] = []   # (timestamp, attribution)

    def record(self, attributions: List[DependencyLatencyAttribution]) -> None:
        ts = time.time()
        for attr in attributions:
            self._records.append((ts, attr))

    def _trim(self) -> None:
        cutoff = time.time() - self._window
        self._records = [(ts, a) for ts, a in self._records if ts >= cutoff]

    def fleet_attribution(self) -> List[dict]:
        self._trim()
        totals: Dict[str, dict] = defaultdict(lambda: {
            "total_ms": 0.0,
            "call_count": 0,
            "error_count": 0,
            "sessions": 0,
        })

        seen_sessions: Dict[str, set] = defaultdict(set)
        for ts, attr in self._records:
            dep = attr.dependency_type
            totals[dep]["total_ms"] += attr.total_duration_ms
            totals[dep]["call_count"] += attr.call_count
            totals[dep]["error_count"] += attr.error_count
            seen_sessions[dep].add(id(attr))   # proxy for unique sessions

        grand_total = sum(v["total_ms"] for v in totals.values())
        result = []
        for dep, data in totals.items():
            result.append({
                "dependency_type": dep,
                "total_ms": round(data["total_ms"], 1),
                "call_count": data["call_count"],
                "avg_ms_per_call": round(data["total_ms"] / max(data["call_count"], 1), 1),
                "error_count": data["error_count"],
                "pct_of_fleet_latency": round(data["total_ms"] / max(grand_total, 1) * 100, 2),
            })
        return sorted(result, key=lambda x: -x["total_ms"])
```

## Solution 6: Latency Attribution Dashboard

```python
import time


class ToolDependencyLatencyDashboard:
    """
    Per-session and fleet-level latency attribution view.
    Identifies bottleneck dependencies and critical path for each session.
    """

    def __init__(
        self,
        analyzer: LatencyAttributionAnalyzer,
        critical_path_detector: CriticalPathDetector,
        fleet_aggregator: FleetLatencyAttributionAggregator,
        slo_ms: float = 10_000.0,
    ):
        self._analyzer = analyzer
        self._cp = critical_path_detector
        self._fleet = fleet_aggregator
        self._slo = slo_ms

    def render_session(self, profiler: SessionLatencyProfiler) -> dict:
        attributions = self._analyzer.attribute()
        self._fleet.record(attributions)
        cp = self._cp.detect(profiler)
        cp_ms = self._cp.critical_path_duration_ms(profiler)
        session_ms = profiler.session_duration_ms

        alerts = []
        if session_ms > self._slo:
            bottleneck = self._analyzer.top_bottleneck()
            alerts.append({
                "type": "slo_violation",
                "session_ms": round(session_ms, 1),
                "slo_ms": self._slo,
                "bottleneck_dependency": bottleneck,
            })

        return {
            "session_id": profiler.session_id,
            "session_duration_ms": round(session_ms, 1),
            "critical_path_ms": round(cp_ms, 1),
            "parallel_savings_ms": round(
                sum(a.total_duration_ms for a in attributions) - session_ms, 1
            ),
            "attribution": [
                {
                    "dependency": a.dependency_type,
                    "total_ms": a.total_duration_ms,
                    "calls": a.call_count,
                    "pct": a.pct_of_session,
                    "parallel": a.is_parallel,
                }
                for a in attributions
            ],
            "critical_path": [
                {"tool": s.tool_name, "dep": s.dependency_type, "ms": s.duration_ms}
                for s in cp
            ],
            "alerts": alerts,
        }

    def render_fleet(self) -> dict:
        return {
            "generated_at": time.time(),
            "fleet_attribution_1h": self._fleet.fleet_attribution(),
        }
```

## Comparison

| Approach | Per-Call Timing | Parallel Detection | Critical Path | Fleet Aggregation |
|---|---|---|---|---|
| SessionLatencyProfiler | Yes | Yes (groups) | No | No |
| LatencyAttributionAnalyzer | Via profiler | Yes | No (top-level only) | No |
| CriticalPathDetector | Via profiler | Yes (max of group) | Yes | No |
| FleetLatencyAttributionAggregator | No | No | No | Yes |
| ToolDependencyLatencyDashboard | Via analyzer | Via detector | Via detector | Yes |

**Best for production**: Use `SessionLatencyProfiler.measure()` as a context manager around every tool call. Use `parallel_group()` to mark all tool calls that fire concurrently — this is critical for correct critical path analysis. Emit `ToolDependencyLatencyDashboard.render_session()` at the end of every session and push it to your metrics system with `dependency_type` as a label. The fleet view shows which dependency type accumulates the most latency across all sessions — optimize that one first before touching anything else.
