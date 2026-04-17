---
title: "Agent Doesn't Implement Conversation Turn Latency Breakdown"
description: "Agents that report only total turn latency cannot identify where time is spent: whether the LLM call dominates, tool execution is the bottleneck, or context assembly adds unexpected overhead. Implement per-phase latency tracking for each conversation turn — context assembly, tool execution (per tool), LLM inference, and response formatting — producing a breakdown that allows engineers to target the most impactful optimization."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-conversation-turn-latency-breakdown
tags: [latency-breakdown, turn-latency, phase-timing, tool-execution-timing, llm-latency, performance-profiling]
symptoms:
  - "P95 turn latency is 8 seconds but there is no breakdown of where those 8 seconds are spent"
  - "Cannot tell whether optimizing the LLM call or tool execution would have the bigger impact"
  - "Context assembly time is never measured — assumed to be negligible but may not be"
  - "Multiple tool calls in one turn are reported as a single aggregate — individual slow tools are hidden"
  - "No per-phase latency data makes it impossible to set meaningful SLOs for each component"
---

## Why This Happens

Total turn latency is easy to measure: timestamp before the turn starts, timestamp when the response is delivered, subtract. Phase-level breakdowns require instrumentation at each phase boundary: before and after context assembly, before and after each tool call, before and after the LLM call, before and after response formatting. Most agent frameworks do not add these instrumentation points by default, and teams do not add them manually because the total latency appears acceptable until it suddenly isn't. Without phase-level data, optimization is guesswork.

## Solution 1: Turn Phase

```python
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class TurnPhase(str, Enum):
    CONTEXT_ASSEMBLY = "context_assembly"
    TOOL_EXECUTION = "tool_execution"
    LLM_INFERENCE = "llm_inference"
    RESPONSE_FORMATTING = "response_formatting"
    GUARDRAIL_CHECK = "guardrail_check"
    CACHE_LOOKUP = "cache_lookup"
    TOTAL = "total"


@dataclass
class PhaseRecord:
    phase: TurnPhase
    tool_name: Optional[str]     # populated for TOOL_EXECUTION phases
    start_time: float
    end_time: Optional[float] = None
    success: bool = True
    error: Optional[str] = None
    metadata: dict = field(default_factory=dict)

    @property
    def duration_ms(self) -> Optional[float]:
        if self.end_time is None:
            return None
        return round((self.end_time - self.start_time) * 1000, 2)

    def finish(self, success: bool = True, error: str = "") -> None:
        self.end_time = time.monotonic()
        self.success = success
        if error:
            self.error = error
```

## Solution 2: Turn Latency Tracker

```python
import time
from contextlib import asynccontextmanager
from typing import Dict, List, Optional


class TurnLatencyTracker:
    """
    Instruments a single conversation turn with phase-level timing.
    Phases are opened and closed via context managers.
    """

    def __init__(self, turn_id: str, session_id: str = ""):
        self.turn_id = turn_id
        self.session_id = session_id
        self._phases: List[PhaseRecord] = []
        self._turn_start = time.monotonic()

    @asynccontextmanager
    async def phase(
        self,
        phase_type: TurnPhase,
        tool_name: Optional[str] = None,
        metadata: dict = None,
    ):
        record = PhaseRecord(
            phase=phase_type,
            tool_name=tool_name,
            start_time=time.monotonic(),
            metadata=metadata or {},
        )
        try:
            yield record
            record.finish(success=True)
        except Exception as exc:
            record.finish(success=False, error=str(exc))
            raise
        finally:
            self._phases.append(record)

    def build_breakdown(self) -> "TurnLatencyBreakdown":
        total_ms = round((time.monotonic() - self._turn_start) * 1000, 2)

        phase_totals: Dict[str, float] = {}
        tool_times: Dict[str, float] = {}
        phase_counts: Dict[str, int] = {}

        for record in self._phases:
            dur = record.duration_ms or 0.0
            key = record.phase.value
            phase_totals[key] = phase_totals.get(key, 0.0) + dur
            phase_counts[key] = phase_counts.get(key, 0) + 1
            if record.phase == TurnPhase.TOOL_EXECUTION and record.tool_name:
                tool_times[record.tool_name] = tool_times.get(record.tool_name, 0.0) + dur

        unaccounted_ms = max(0.0, total_ms - sum(phase_totals.values()))

        return TurnLatencyBreakdown(
            turn_id=self.turn_id,
            session_id=self.session_id,
            total_ms=total_ms,
            phase_totals_ms=phase_totals,
            phase_counts=phase_counts,
            tool_breakdown_ms=tool_times,
            unaccounted_ms=round(unaccounted_ms, 2),
            phases=self._phases,
        )
```

## Solution 3: Turn Latency Breakdown

```python
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class TurnLatencyBreakdown:
    turn_id: str
    session_id: str
    total_ms: float
    phase_totals_ms: Dict[str, float]
    phase_counts: Dict[str, int]
    tool_breakdown_ms: Dict[str, float]
    unaccounted_ms: float
    phases: List[PhaseRecord] = field(default_factory=list, repr=False)

    def dominant_phase(self) -> Optional[str]:
        if not self.phase_totals_ms:
            return None
        return max(self.phase_totals_ms, key=lambda k: self.phase_totals_ms[k])

    def phase_pct(self, phase_name: str) -> float:
        if self.total_ms <= 0:
            return 0.0
        return round(self.phase_totals_ms.get(phase_name, 0.0) / self.total_ms * 100, 1)

    def to_dict(self) -> dict:
        return {
            "turn_id": self.turn_id,
            "session_id": self.session_id,
            "total_ms": self.total_ms,
            "phases": {
                name: {
                    "ms": round(ms, 2),
                    "pct": self.phase_pct(name),
                    "count": self.phase_counts.get(name, 0),
                }
                for name, ms in self.phase_totals_ms.items()
            },
            "tool_breakdown_ms": {
                t: round(ms, 2) for t, ms in self.tool_breakdown_ms.items()
            },
            "dominant_phase": self.dominant_phase(),
            "unaccounted_ms": self.unaccounted_ms,
        }
```

## Solution 4: Latency Breakdown Aggregator

```python
import time
from collections import deque
from threading import Lock
from typing import Deque, Dict, List, Optional, Tuple


class LatencyBreakdownAggregator:
    """
    Accumulates turn latency breakdowns and computes percentile distributions
    per phase for SLO tracking and optimization prioritization.
    """

    def __init__(self, window_size: int = 5000):
        self._window = window_size
        self._breakdowns: Deque[Tuple[float, TurnLatencyBreakdown]] = deque(maxlen=window_size)
        self._lock = Lock()

    def record(self, breakdown: TurnLatencyBreakdown) -> None:
        with self._lock:
            self._breakdowns.append((time.time(), breakdown))

    def percentile_by_phase(
        self,
        phase_name: str,
        pct: float,
        window_seconds: float = 3600.0,
    ) -> Optional[float]:
        cutoff = time.time() - window_seconds
        with self._lock:
            values = sorted(
                bd.phase_totals_ms.get(phase_name, 0.0)
                for ts, bd in self._breakdowns
                if ts >= cutoff
            )
        if not values:
            return None
        idx = min(int(len(values) * pct / 100.0), len(values) - 1)
        return round(values[idx], 2)

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        with self._lock:
            recent = [bd for ts, bd in self._breakdowns if ts >= cutoff]

        if not recent:
            return {"window_seconds": window_seconds, "turns": 0}

        totals = [bd.total_ms for bd in recent]
        avg_total = sum(totals) / len(totals)

        phase_avgs: dict = {}
        for phase in TurnPhase:
            if phase == TurnPhase.TOTAL:
                continue
            vals = [bd.phase_totals_ms.get(phase.value, 0.0) for bd in recent]
            nonzero = [v for v in vals if v > 0]
            if nonzero:
                phase_avgs[phase.value] = {
                    "avg_ms": round(sum(nonzero) / len(nonzero), 2),
                    "p95_ms": self.percentile_by_phase(phase.value, 95, window_seconds),
                    "pct_of_total": round(sum(nonzero) / sum(totals) * 100, 1),
                }

        dominant_counts: dict = {}
        for bd in recent:
            d = bd.dominant_phase()
            if d:
                dominant_counts[d] = dominant_counts.get(d, 0) + 1

        return {
            "window_seconds": window_seconds,
            "turns": len(recent),
            "avg_total_ms": round(avg_total, 2),
            "p95_total_ms": round(sorted(totals)[min(int(len(totals) * 0.95), len(totals) - 1)], 2),
            "phase_breakdown": phase_avgs,
            "dominant_phase_distribution": dominant_counts,
        }
```

## Solution 5: Slow Turn Detector

```python
import time
from typing import List, Optional


class SlowTurnDetector:
    """
    Identifies turns whose total or per-phase latency exceeds thresholds.
    Surfaces which phase caused the slowness for targeted investigation.
    """

    def __init__(
        self,
        total_threshold_ms: float = 5000.0,
        phase_thresholds_ms: Optional[dict] = None,
    ):
        self._total_threshold = total_threshold_ms
        self._phase_thresholds = phase_thresholds_ms or {
            TurnPhase.LLM_INFERENCE.value: 4000.0,
            TurnPhase.TOOL_EXECUTION.value: 2000.0,
            TurnPhase.CONTEXT_ASSEMBLY.value: 500.0,
        }
        self._slow_turns: List[dict] = []

    def check(self, breakdown: TurnLatencyBreakdown) -> Optional[dict]:
        violations = []

        if breakdown.total_ms >= self._total_threshold:
            violations.append({
                "dimension": "total",
                "observed_ms": breakdown.total_ms,
                "threshold_ms": self._total_threshold,
            })

        for phase_name, threshold in self._phase_thresholds.items():
            observed = breakdown.phase_totals_ms.get(phase_name, 0.0)
            if observed >= threshold:
                violations.append({
                    "dimension": phase_name,
                    "observed_ms": observed,
                    "threshold_ms": threshold,
                })

        if not violations:
            return None

        record = {
            "ts": time.time(),
            "turn_id": breakdown.turn_id,
            "session_id": breakdown.session_id,
            "total_ms": breakdown.total_ms,
            "dominant_phase": breakdown.dominant_phase(),
            "violations": violations,
            "tool_breakdown_ms": breakdown.tool_breakdown_ms,
        }
        self._slow_turns.append(record)
        return record

    def recent_slow_turns(self, limit: int = 20) -> List[dict]:
        return self._slow_turns[-limit:]
```

## Solution 6: Turn Latency Dashboard

```python
import time


class TurnLatencyDashboard:
    """
    Combines phase breakdown aggregation, slow turn detection, and
    tool-level timing into a single performance report.
    """

    def __init__(
        self,
        aggregator: LatencyBreakdownAggregator,
        slow_detector: SlowTurnDetector,
    ):
        self._aggregator = aggregator
        self._detector = slow_detector

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "summary_1h": self._aggregator.summary(3600.0),
            "recent_slow_turns": self._detector.recent_slow_turns(10),
        }
```

## Comparison

| Approach | Per-Phase Timing | Per-Tool Timing | Percentile Distribution | Slow Turn Detection | Dashboard |
|---|---|---|---|---|---|
| TurnLatencyTracker | Yes (context manager) | Yes (tool_name) | No | No | No |
| TurnLatencyBreakdown | Via tracker | Yes | No | No | No |
| LatencyBreakdownAggregator | No | No | Yes (per phase) | No | No |
| SlowTurnDetector | No | Via breakdown | No | Yes (multi-dim) | No |
| TurnLatencyDashboard | No | No | No | No | Yes |

**Best for production**: Instrument context assembly and response formatting even if you expect them to be fast — these phases often surprise teams when conversation history grows large or when response post-processing (markdown rendering, PII scanning) is added without latency budgeting. Use `SlowTurnDetector` with per-phase thresholds rather than just a total threshold: a turn that takes 6 seconds because of a single slow tool call needs a different fix than one that takes 6 seconds because context assembly is O(n²) in conversation length. Emit `TurnLatencyBreakdown.to_dict()` as a structured log field on every turn — this gives you a queryable latency breakdown in your log system without requiring a separate time-series database.
