---
title: "Agent Doesn't Implement Tool Call Waterfall Visualization"
description: "Agents that execute multiple tool calls per task produce no structured record of call sequencing, overlap, or critical path — developers must read raw logs to understand whether calls ran in parallel or serial, which call blocked progress, and where latency was spent. Implement tool call waterfall tracking that records start time, end time, and dependencies for every tool call, and produces a Gantt-style timeline report for latency analysis and parallelism optimization."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-tool-call-waterfall-visualization
tags: [waterfall, tool-calls, gantt-chart, critical-path, latency-analysis, parallelism]
symptoms:
  - "No way to see whether tool calls ran in parallel or sequentially for a given task"
  - "Cannot identify which tool call is on the critical path of a slow task"
  - "Tool call logs have timestamps but no structured start/end durations or dependency edges"
  - "Parallelism optimization requires manually reconstructing timelines from raw logs"
  - "P99 latency analysis impossible without knowing call overlap and sequencing"
---

## Why This Happens

Logging systems capture events at a point in time but do not natively represent durations, overlaps, or causal dependencies between events. A tool call that starts at T=0 and ends at T=5000ms alongside another that starts at T=0 and ends at T=3000ms appears as four separate log lines with no indication that the calls overlapped. Waterfall tracking requires recording start and end timestamps for each call within a task context, linking calls that were launched from the same agent step, and computing derived metrics — parallelism efficiency, critical path, blocking calls — from the resulting timeline.

## Solution 1: Tool Call Span

```python
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, List, Optional


class CallStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class ToolCallSpan:
    span_id: str
    task_id: str
    tool_name: str
    call_group: str            # groups calls launched in the same agent step
    started_at: float
    ended_at: Optional[float] = None
    status: CallStatus = CallStatus.RUNNING
    error: Optional[str] = None
    result_size_bytes: int = 0
    depends_on: List[str] = field(default_factory=list)  # span_ids this call waited for

    @property
    def duration_ms(self) -> Optional[float]:
        if self.ended_at is None:
            return None
        return round((self.ended_at - self.started_at) * 1000, 2)

    def complete(self, result_size_bytes: int = 0) -> None:
        self.ended_at = time.time()
        self.status = CallStatus.COMPLETED
        self.result_size_bytes = result_size_bytes

    def fail(self, error: str) -> None:
        self.ended_at = time.time()
        self.status = CallStatus.FAILED
        self.error = error
```

## Solution 2: Waterfall Recorder

```python
import time
import uuid
from threading import Lock
from typing import Dict, List, Optional


class WaterfallRecorder:
    """
    Records tool call spans for a task and computes waterfall metrics.
    Spans within the same call_group are treated as concurrent.
    """

    def __init__(self, task_id: str):
        self.task_id = task_id
        self.task_start = time.time()
        self._spans: Dict[str, ToolCallSpan] = {}
        self._lock = Lock()

    def start_call(
        self,
        tool_name: str,
        call_group: str = "",
        depends_on: Optional[List[str]] = None,
    ) -> ToolCallSpan:
        span = ToolCallSpan(
            span_id=uuid.uuid4().hex[:12],
            task_id=self.task_id,
            tool_name=tool_name,
            call_group=call_group or uuid.uuid4().hex[:8],
            started_at=time.time(),
            depends_on=depends_on or [],
        )
        with self._lock:
            self._spans[span.span_id] = span
        return span

    def end_call(
        self,
        span: ToolCallSpan,
        success: bool = True,
        error: Optional[str] = None,
        result_size_bytes: int = 0,
    ) -> None:
        if success:
            span.complete(result_size_bytes)
        else:
            span.fail(error or "unknown error")

    def spans(self) -> List[ToolCallSpan]:
        with self._lock:
            return list(self._spans.values())
```

## Solution 3: Waterfall Analyzer

```python
from typing import Dict, List, Optional, Tuple


class WaterfallAnalyzer:
    """
    Computes waterfall metrics from a set of recorded spans:
    - Total task duration
    - Critical path (longest sequential chain)
    - Parallelism efficiency (actual wall time vs sum of call durations)
    - Blocking calls (calls that delayed subsequent calls)
    """

    def analyze(self, recorder: WaterfallRecorder) -> dict:
        spans = recorder.spans()
        completed = [s for s in spans if s.ended_at is not None]

        if not completed:
            return {"task_id": recorder.task_id, "status": "no_completed_spans"}

        task_start = recorder.task_start
        task_end = max(s.ended_at for s in completed)
        task_duration_ms = round((task_end - task_start) * 1000, 2)
        sum_call_duration_ms = sum(s.duration_ms for s in completed if s.duration_ms)
        parallelism_efficiency = round(
            sum_call_duration_ms / max(task_duration_ms, 1), 3
        )

        # Group by call_group to find concurrent sets
        groups: Dict[str, List[ToolCallSpan]] = {}
        for s in completed:
            groups.setdefault(s.call_group, []).append(s)

        group_stats = []
        for group_id, group_spans in groups.items():
            group_start = min(s.started_at for s in group_spans)
            group_end = max(s.ended_at for s in group_spans)
            group_stats.append({
                "call_group": group_id,
                "concurrent_calls": len(group_spans),
                "tools": [s.tool_name for s in group_spans],
                "group_duration_ms": round((group_end - group_start) * 1000, 2),
                "slowest_call_ms": max(s.duration_ms for s in group_spans if s.duration_ms),
            })

        # Slowest call = critical path candidate
        slowest = max(completed, key=lambda s: s.duration_ms or 0)
        failed = [s for s in spans if s.status == CallStatus.FAILED]

        return {
            "task_id": recorder.task_id,
            "task_duration_ms": task_duration_ms,
            "call_count": len(spans),
            "completed_calls": len(completed),
            "failed_calls": len(failed),
            "sum_call_duration_ms": round(sum_call_duration_ms, 2),
            "parallelism_efficiency": parallelism_efficiency,
            "call_groups": len(groups),
            "slowest_call": {
                "tool_name": slowest.tool_name,
                "duration_ms": slowest.duration_ms,
                "span_id": slowest.span_id,
            },
            "group_breakdown": sorted(
                group_stats, key=lambda g: g["group_duration_ms"], reverse=True
            ),
        }
```

## Solution 4: ASCII Waterfall Renderer

```python
import math
from typing import List


class ASCIIWaterfallRenderer:
    """
    Renders a Gantt-style ASCII waterfall chart from recorded spans.
    Each row is a tool call; columns represent time slots.
    Useful for logging and CLI debugging without external visualization.
    """

    def __init__(self, width: int = 60):
        self._width = width

    def render(self, recorder: WaterfallRecorder) -> str:
        spans = [s for s in recorder.spans() if s.ended_at is not None]
        if not spans:
            return f"[task {recorder.task_id}] No completed spans."

        t_start = recorder.task_start
        t_end = max(s.ended_at for s in spans)
        total = t_end - t_start
        if total == 0:
            total = 0.001

        lines = [f"Task {recorder.task_id} — {round(total * 1000, 1)}ms total"]
        lines.append("─" * (self._width + 25))

        for span in sorted(spans, key=lambda s: s.started_at):
            rel_start = (span.started_at - t_start) / total
            rel_end = (span.ended_at - t_start) / total
            start_col = int(rel_start * self._width)
            end_col = max(start_col + 1, int(rel_end * self._width))
            bar = " " * start_col + "█" * (end_col - start_col)
            bar = bar.ljust(self._width)
            status_char = "✓" if span.status == CallStatus.COMPLETED else "✗"
            label = f"{span.tool_name[:18]:<18} {status_char} {span.duration_ms:>7.1f}ms"
            lines.append(f"{label} |{bar}|")

        lines.append("─" * (self._width + 25))
        return "\n".join(lines)
```

## Solution 5: Aggregate Waterfall Statistics

```python
import time
from typing import List


class AggregateWaterfallStatistics:
    """
    Accumulates waterfall analysis results across multiple task executions
    and surfaces patterns: which tools dominate total latency, average
    parallelism efficiency, and P95 task duration.
    """

    def __init__(self):
        self._analyses: List[dict] = []
        self._recorded_at: List[float] = []

    def record(self, analysis: dict) -> None:
        self._analyses.append(analysis)
        self._recorded_at.append(time.time())

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [
            a for a, ts in zip(self._analyses, self._recorded_at) if ts >= cutoff
        ]
        if not recent:
            return {"window_seconds": window_seconds, "tasks": 0}

        durations = sorted(a["task_duration_ms"] for a in recent)
        efficiencies = [a["parallelism_efficiency"] for a in recent]
        p95_idx = min(int(len(durations) * 0.95), len(durations) - 1)

        # Aggregate slowest calls
        slowest_counts: dict = {}
        for a in recent:
            tool = a.get("slowest_call", {}).get("tool_name", "unknown")
            slowest_counts[tool] = slowest_counts.get(tool, 0) + 1

        return {
            "window_seconds": window_seconds,
            "tasks": len(recent),
            "avg_task_duration_ms": round(sum(durations) / len(durations), 2),
            "p95_task_duration_ms": round(durations[p95_idx], 2),
            "avg_parallelism_efficiency": round(
                sum(efficiencies) / len(efficiencies), 3
            ),
            "most_frequent_bottleneck": max(
                slowest_counts, key=slowest_counts.get, default="none"
            ),
        }
```

## Solution 6: Waterfall Observability Dashboard

```python
import time


class WaterfallObservabilityDashboard:
    """
    Combines live task waterfall rendering with aggregate statistics
    for both real-time debugging and trend analysis.
    """

    def __init__(
        self,
        analyzer: WaterfallAnalyzer,
        renderer: ASCIIWaterfallRenderer,
        stats: AggregateWaterfallStatistics,
    ):
        self._analyzer = analyzer
        self._renderer = renderer
        self._stats = stats

    def render_task(self, recorder: WaterfallRecorder) -> dict:
        analysis = self._analyzer.analyze(recorder)
        ascii_chart = self._renderer.render(recorder)
        self._stats.record(analysis)
        return {
            "generated_at": time.time(),
            "analysis": analysis,
            "ascii_waterfall": ascii_chart,
        }

    def aggregate_report(self, window_seconds: float = 3600.0) -> dict:
        return {
            "generated_at": time.time(),
            "statistics": self._stats.summary(window_seconds),
        }
```

## Comparison

| Approach | Span Recording | Group Concurrency | Critical Path | ASCII Chart | Aggregate Stats |
|---|---|---|---|---|---|
| WaterfallRecorder | Yes (start/end) | Via call_group | No | No | No |
| WaterfallAnalyzer | No | Yes (group stats) | Yes (slowest) | No | No |
| ASCIIWaterfallRenderer | No | No | No | Yes | No |
| AggregateWaterfallStatistics | No | No | No | No | Yes |
| WaterfallObservabilityDashboard | No | No | No | No | Yes (combined) |

**Best for production**: Assign the same `call_group` ID to all tool calls launched from a single agent step — this is what enables the analyzer to identify true parallelism versus sequential execution. Export `WaterfallAnalyzer` results as structured log events with `task_id` and `parallelism_efficiency` fields: a dashboard filter on `parallelism_efficiency < 1.5` surfaces tasks where parallel calls exist but one slow call serializes everything. Emit the ASCII waterfall chart at DEBUG level only — it is valuable during development and incident investigation but too verbose for production steady-state logging. Track `most_frequent_bottleneck` over time: if the same tool appears as the bottleneck in more than 30% of tasks, it is a candidate for optimization (caching, parallelization, or timeout reduction) rather than just a measurement artifact.
