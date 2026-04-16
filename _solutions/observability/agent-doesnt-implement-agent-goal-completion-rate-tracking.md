---
title: "Agent Doesn't Implement Agent Goal Completion Rate Tracking"
description: "Agents that emit no signal about whether they successfully completed the user's stated goal make it impossible to measure true effectiveness: request latency and tool call counts can be healthy while the agent is systematically failing to answer the actual question. Implement goal completion rate tracking that captures explicit success signals, infers completion from behavioral patterns, and surfaces completion rates broken down by goal category and session length."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-agent-goal-completion-rate-tracking
tags: [goal-completion, success-rate, agent-effectiveness, completion-tracking, session-analytics, outcome-measurement]
symptoms:
  - "No metric exists for whether the agent actually answered the user's question"
  - "Latency and error rate are healthy but user satisfaction is low — no completion signal exists"
  - "Agent produces output but cannot determine if the goal was fulfilled or abandoned"
  - "Goal completion data only comes from periodic user surveys, not real-time instrumentation"
  - "Cannot identify which goal categories have the highest abandonment or failure rates"
---

## Why This Happens

Agent telemetry is typically instrumented at the infrastructure level: request counts, tool call latencies, token usage. These are proxy metrics — they measure activity, not outcome. A goal completion signal requires semantic understanding of the session: did the agent produce an answer that addressed the user's intent, or did the session end in frustration, rephrasing, or abandonment? Completion tracking captures explicit signals (the agent marks a goal as achieved), inferred signals (the user said "thanks" or closed the session without escalating), and negative signals (repeated rephrasing, explicit complaints, tool error storms). Aggregating these signals by goal category reveals which use cases the agent handles well and which need improvement.

## Solution 1: Goal Record

```python
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class GoalStatus(str, Enum):
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    ABANDONED = "abandoned"
    FAILED = "failed"
    ESCALATED = "escalated"


class CompletionSignalSource(str, Enum):
    EXPLICIT_AGENT = "explicit_agent"      # agent called mark_complete()
    EXPLICIT_USER = "explicit_user"        # user confirmed satisfaction
    INFERRED_POSITIVE = "inferred_positive"  # behavioral heuristic
    INFERRED_NEGATIVE = "inferred_negative"  # rephrasing / complaint pattern
    TIMEOUT = "timeout"                    # session ended without signal


@dataclass
class GoalRecord:
    goal_id: str
    session_id: str
    goal_text: str
    goal_category: str
    started_at: float = field(default_factory=time.time)
    completed_at: Optional[float] = None
    status: GoalStatus = GoalStatus.IN_PROGRESS
    signal_source: Optional[CompletionSignalSource] = None
    confidence: float = 1.0              # 0–1; lower for inferred signals
    tool_calls_made: int = 0
    rephrasing_count: int = 0
    duration_seconds: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def close(
        self,
        status: GoalStatus,
        source: CompletionSignalSource,
        confidence: float = 1.0,
    ) -> None:
        self.status = status
        self.signal_source = source
        self.confidence = confidence
        self.completed_at = time.time()
        self.duration_seconds = round(self.completed_at - self.started_at, 2)
```

## Solution 2: Goal Tracker

```python
import threading
import time
from typing import Dict, List, Optional


class GoalTracker:
    """
    Creates and manages goal records for the current session.
    Supports explicit completion marking and rephrasing detection.
    """

    def __init__(self, session_id: str):
        self._session_id = session_id
        self._goals: Dict[str, GoalRecord] = {}
        self._lock = threading.Lock()
        self._active_goal_id: Optional[str] = None

    def start_goal(
        self,
        goal_id: str,
        goal_text: str,
        goal_category: str = "general",
    ) -> GoalRecord:
        record = GoalRecord(
            goal_id=goal_id,
            session_id=self._session_id,
            goal_text=goal_text,
            goal_category=goal_category,
        )
        with self._lock:
            self._goals[goal_id] = record
            self._active_goal_id = goal_id
        return record

    def mark_completed(
        self,
        goal_id: str,
        source: CompletionSignalSource = CompletionSignalSource.EXPLICIT_AGENT,
        confidence: float = 1.0,
    ) -> None:
        with self._lock:
            record = self._goals.get(goal_id)
            if record and record.status == GoalStatus.IN_PROGRESS:
                record.close(GoalStatus.COMPLETED, source, confidence)

    def mark_failed(
        self,
        goal_id: str,
        source: CompletionSignalSource = CompletionSignalSource.EXPLICIT_AGENT,
    ) -> None:
        with self._lock:
            record = self._goals.get(goal_id)
            if record and record.status == GoalStatus.IN_PROGRESS:
                record.close(GoalStatus.FAILED, source, confidence=1.0)

    def record_rephrase(self, goal_id: str) -> None:
        with self._lock:
            record = self._goals.get(goal_id)
            if record:
                record.rephrasing_count += 1

    def record_tool_call(self, goal_id: str) -> None:
        with self._lock:
            record = self._goals.get(goal_id)
            if record:
                record.tool_calls_made += 1

    def active_goal(self) -> Optional[GoalRecord]:
        with self._lock:
            if self._active_goal_id:
                return self._goals.get(self._active_goal_id)
            return None

    def all_goals(self) -> List[GoalRecord]:
        with self._lock:
            return list(self._goals.values())
```

## Solution 3: Completion Signal Inferrer

```python
import re
from typing import List


POSITIVE_SIGNALS = [
    r"\bthank(s| you)\b",
    r"\bperfect\b",
    r"\bthat('s| is) (exactly|what I needed|great|helpful)\b",
    r"\bgreat(,| that)?\b",
    r"\bgot it\b",
    r"\ball set\b",
]

NEGATIVE_SIGNALS = [
    r"\bthat('s| is) (not|wrong|incorrect)\b",
    r"\bthat didn't (work|help|answer)\b",
    r"\bcan you (try again|redo|fix)\b",
    r"\bI said\b.*\bnot\b",
    r"\bstill (not|wrong|broken)\b",
    r"\byou (misunderstood|missed)\b",
]


class CompletionSignalInferrer:
    """
    Infers goal completion status from user message text.
    Returns (signal_type, confidence) or (None, 0) if no signal detected.
    """

    def __init__(self):
        self._positive = [re.compile(p, re.IGNORECASE) for p in POSITIVE_SIGNALS]
        self._negative = [re.compile(p, re.IGNORECASE) for p in NEGATIVE_SIGNALS]

    def infer(self, user_message: str) -> tuple:
        positive_hits = sum(1 for p in self._positive if p.search(user_message))
        negative_hits = sum(1 for p in self._negative if p.search(user_message))

        if positive_hits > 0 and negative_hits == 0:
            confidence = min(0.9, 0.5 + positive_hits * 0.15)
            return CompletionSignalSource.INFERRED_POSITIVE, confidence

        if negative_hits > 0:
            confidence = min(0.9, 0.5 + negative_hits * 0.15)
            return CompletionSignalSource.INFERRED_NEGATIVE, confidence

        return None, 0.0
```

## Solution 4: Goal Completion Rate Recorder

```python
import time
from collections import defaultdict
from threading import Lock
from typing import Dict, List, Optional


class GoalCompletionRateRecorder:
    """
    Accumulates closed goal records and computes completion rates
    broken down by goal category and signal source.
    """

    def __init__(self, max_records: int = 50000):
        self._max = max_records
        self._records: List[GoalRecord] = []
        self._recorded_at: List[float] = []
        self._lock = Lock()

    def record(self, goal: GoalRecord) -> None:
        if goal.status == GoalStatus.IN_PROGRESS:
            return
        with self._lock:
            self._records.append(goal)
            self._recorded_at.append(time.time())
            if len(self._records) > self._max:
                self._records.pop(0)
                self._recorded_at.pop(0)

    def completion_rate(
        self,
        window_seconds: float = 3600.0,
        category: Optional[str] = None,
        min_confidence: float = 0.0,
    ) -> Optional[float]:
        cutoff = time.time() - window_seconds
        with self._lock:
            recent = [
                g for g, ts in zip(self._records, self._recorded_at)
                if ts >= cutoff
                and (category is None or g.goal_category == category)
                and (g.confidence or 1.0) >= min_confidence
            ]
        if not recent:
            return None
        completed = sum(1 for g in recent if g.status == GoalStatus.COMPLETED)
        return round(completed / len(recent), 4)

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        with self._lock:
            recent = [
                g for g, ts in zip(self._records, self._recorded_at)
                if ts >= cutoff
            ]

        if not recent:
            return {"window_seconds": window_seconds, "goals": 0}

        by_category: Dict[str, List[GoalRecord]] = defaultdict(list)
        for g in recent:
            by_category[g.goal_category].append(g)

        by_status = defaultdict(int)
        for g in recent:
            by_status[g.status.value] += 1

        durations = [g.duration_seconds for g in recent if g.duration_seconds is not None]

        return {
            "window_seconds": window_seconds,
            "goals": len(recent),
            "by_status": dict(by_status),
            "overall_completion_rate": self.completion_rate(window_seconds),
            "by_category": {
                cat: {
                    "count": len(goals),
                    "completion_rate": round(
                        sum(1 for g in goals if g.status == GoalStatus.COMPLETED) / len(goals),
                        4,
                    ),
                }
                for cat, goals in by_category.items()
            },
            "avg_duration_seconds": round(sum(durations) / len(durations), 2) if durations else None,
        }
```

## Solution 5: Session Goal Completion Auditor

```python
import time
from typing import List, Optional


class SessionGoalCompletionAuditor:
    """
    Reviews all goals in a completed session and closes any that
    were left IN_PROGRESS with a TIMEOUT signal.
    Emits a per-session completion report for downstream analytics.
    """

    def __init__(
        self,
        tracker: GoalTracker,
        recorder: GoalCompletionRateRecorder,
        inferrer: CompletionSignalInferrer,
    ):
        self._tracker = tracker
        self._recorder = recorder
        self._inferrer = inferrer

    def process_user_message(self, message: str) -> None:
        active = self._tracker.active_goal()
        if active is None or active.status != GoalStatus.IN_PROGRESS:
            return
        signal, confidence = self._inferrer.infer(message)
        if signal == CompletionSignalSource.INFERRED_POSITIVE:
            self._tracker.mark_completed(active.goal_id, signal, confidence)
            self._recorder.record(active)
        elif signal == CompletionSignalSource.INFERRED_NEGATIVE:
            self._tracker.record_rephrase(active.goal_id)

    def close_session(self) -> dict:
        goals = self._tracker.all_goals()
        for goal in goals:
            if goal.status == GoalStatus.IN_PROGRESS:
                goal.close(GoalStatus.ABANDONED, CompletionSignalSource.TIMEOUT, confidence=0.6)
            self._recorder.record(goal)

        completed = [g for g in goals if g.status == GoalStatus.COMPLETED]
        failed = [g for g in goals if g.status in (GoalStatus.FAILED, GoalStatus.ABANDONED)]

        return {
            "session_id": self._tracker._session_id,
            "total_goals": len(goals),
            "completed": len(completed),
            "failed_or_abandoned": len(failed),
            "session_completion_rate": round(len(completed) / max(len(goals), 1), 4),
        }
```

## Solution 6: Goal Completion Dashboard

```python
import time


class GoalCompletionDashboard:
    """
    Combines real-time completion rates, category breakdowns, and
    session-level audit results into a single operational view.
    """

    def __init__(
        self,
        recorder: GoalCompletionRateRecorder,
    ):
        self._recorder = recorder

    def render(self) -> dict:
        summary_1h = self._recorder.summary(window_seconds=3600.0)
        summary_24h = self._recorder.summary(window_seconds=86400.0)

        return {
            "generated_at": time.time(),
            "last_1h": {
                "goals": summary_1h.get("goals", 0),
                "completion_rate": summary_1h.get("overall_completion_rate"),
                "by_status": summary_1h.get("by_status", {}),
            },
            "last_24h": {
                "goals": summary_24h.get("goals", 0),
                "completion_rate": summary_24h.get("overall_completion_rate"),
                "by_category": summary_24h.get("by_category", {}),
            },
            "avg_duration_seconds": summary_1h.get("avg_duration_seconds"),
        }
```

## Comparison

| Approach | Explicit Signals | Inferred Signals | Category Breakdown | Session Audit | Real-Time Rate |
|---|---|---|---|---|---|
| GoalTracker | Yes (mark_complete) | No | Via goal_category | No | No |
| CompletionSignalInferrer | No | Yes (regex) | No | No | No |
| GoalCompletionRateRecorder | No | No | Yes | No | Yes |
| SessionGoalCompletionAuditor | Via tracker | Via inferrer | No | Yes | No |
| GoalCompletionDashboard | No | No | Via recorder | No | Yes |

**Best for production**: Always emit an explicit `mark_completed()` from the agent when it determines the task is done — this provides a high-confidence signal that does not depend on user behavior. Layer `CompletionSignalInferrer` on top for sessions where the agent cannot determine completion itself (open-ended conversations). Track completion rate by `goal_category` in `GoalCompletionRateRecorder`: a category with completion rate below 60% signals a systematic capability gap, not a noisy incident. Alert when the rolling 1-hour completion rate drops more than 10 percentage points below the 24-hour baseline — this indicates a regression, not natural variance.
