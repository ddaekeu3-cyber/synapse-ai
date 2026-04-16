---
title: "Agent Doesn't Implement Session Abandonment Rate Tracking"
description: "Agents that do not track session abandonment — users who start a conversation but leave before reaching a conclusion — have no visibility into a key signal of user frustration: high abandonment rates after the first agent response indicate the response was irrelevant, confusing, or too slow; high abandonment on specific question types indicates a capability gap. Implement session abandonment rate tracking that detects abandoned sessions, classifies them by abandonment stage, and surfaces abandonment trends by category and time of day."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-session-abandonment-rate-tracking
tags: [abandonment-rate, session-analytics, user-frustration, drop-off-tracking, ux-observability, session-quality]
symptoms:
  - "No metric exists for sessions that end before a satisfying conclusion"
  - "High user churn is observed but cannot be attributed to specific agent behaviors"
  - "Cannot determine at which turn users most commonly abandon sessions"
  - "No distinction between completed sessions and abandoned sessions in analytics"
  - "Abandonment after the first agent response goes undetected"
---

## Why This Happens

Session completion metrics count sessions that end in a successful outcome. Abandonment is the complement: sessions that end without one. Most logging systems record the start and end of sessions but not whether the user left satisfied or frustrated. Without an explicit abandonment signal — detected from session timeout, lack of user follow-up after an agent response, or a sudden session close — the operator sees only that sessions ended, not why. Abandonment tracking requires defining what constitutes an abandoned session (timeout after last agent response, user closed tab, explicit frustration signal), recording it, and breaking it down by stage (after turn 1, after turn 3, during tool execution) to identify where the agent most commonly loses users.

## Solution 1: Abandonment Signal

```python
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class AbandonmentStage(str, Enum):
    BEFORE_FIRST_RESPONSE = "before_first_response"  # user left before agent replied
    AFTER_FIRST_RESPONSE = "after_first_response"    # most critical: first reply didn't land
    MID_CONVERSATION = "mid_conversation"            # left partway through
    DURING_TOOL_EXECUTION = "during_tool_execution"  # left while agent was working
    AFTER_LONG_RESPONSE = "after_long_response"      # response too long or complex
    UNKNOWN = "unknown"


class AbandonmentReason(str, Enum):
    TIMEOUT = "timeout"               # no user activity within window
    EXPLICIT_EXIT = "explicit_exit"   # user explicitly closed or navigated away
    ERROR_STORM = "error_storm"       # multiple tool errors in session
    SLOW_RESPONSE = "slow_response"   # response took too long before abandonment
    INFERRED = "inferred"             # heuristic detection


@dataclass
class AbandonmentRecord:
    session_id: str
    stage: AbandonmentStage
    reason: AbandonmentReason
    turn_count: int
    session_duration_seconds: float
    last_agent_response_latency_ms: Optional[float]
    goal_category: str = "general"
    recorded_at: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)
```

## Solution 2: Abandonment Detector

```python
import time
from typing import Optional


class AbandonmentDetector:
    """
    Determines whether a session should be classified as abandoned
    based on session state at closure time.
    """

    def __init__(
        self,
        timeout_seconds: float = 300.0,
        slow_response_threshold_ms: float = 10000.0,
        error_storm_threshold: int = 3,
    ):
        self._timeout = timeout_seconds
        self._slow_threshold = slow_response_threshold_ms
        self._error_storm = error_storm_threshold

    def classify(
        self,
        session_id: str,
        turn_count: int,
        agent_turns: int,
        duration_seconds: float,
        last_activity_seconds_ago: float,
        last_latency_ms: Optional[float],
        error_count: int,
        goal_completed: bool,
        goal_category: str = "general",
    ) -> Optional[AbandonmentRecord]:
        if goal_completed:
            return None   # not an abandonment

        if last_activity_seconds_ago < 30:
            return None   # session too recent to classify

        # Determine reason
        if last_activity_seconds_ago >= self._timeout:
            reason = AbandonmentReason.TIMEOUT
        elif error_count >= self._error_storm:
            reason = AbandonmentReason.ERROR_STORM
        elif last_latency_ms and last_latency_ms >= self._slow_threshold:
            reason = AbandonmentReason.SLOW_RESPONSE
        else:
            reason = AbandonmentReason.INFERRED

        # Determine stage
        if agent_turns == 0:
            stage = AbandonmentStage.BEFORE_FIRST_RESPONSE
        elif turn_count <= 2:
            stage = AbandonmentStage.AFTER_FIRST_RESPONSE
        elif last_latency_ms and last_latency_ms >= self._slow_threshold:
            stage = AbandonmentStage.AFTER_LONG_RESPONSE
        else:
            stage = AbandonmentStage.MID_CONVERSATION

        return AbandonmentRecord(
            session_id=session_id,
            stage=stage,
            reason=reason,
            turn_count=turn_count,
            session_duration_seconds=duration_seconds,
            last_agent_response_latency_ms=last_latency_ms,
            goal_category=goal_category,
        )
```

## Solution 3: Abandonment Rate Recorder

```python
import time
import threading
from collections import defaultdict, deque
from typing import Deque, Dict, List, Optional, Tuple


class AbandonmentRateRecorder:
    """
    Accumulates abandonment records and computes rates broken down
    by stage, reason, and goal category.
    """

    def __init__(self, max_records: int = 50000):
        self._max = max_records
        self._records: Deque[Tuple[float, AbandonmentRecord]] = deque()
        self._completions: Deque[float] = deque()   # timestamps of completed sessions
        self._lock = threading.Lock()

    def record_abandonment(self, record: AbandonmentRecord) -> None:
        with self._lock:
            self._records.append((time.time(), record))
            if len(self._records) > self._max:
                self._records.popleft()

    def record_completion(self) -> None:
        with self._lock:
            self._completions.append(time.time())
            if len(self._completions) > self._max:
                self._completions.popleft()

    def abandonment_rate(self, window_seconds: float = 3600.0) -> Optional[float]:
        cutoff = time.time() - window_seconds
        with self._lock:
            abandoned = sum(1 for ts, _ in self._records if ts >= cutoff)
            completed = sum(1 for ts in self._completions if ts >= cutoff)
        total = abandoned + completed
        if total == 0:
            return None
        return round(abandoned / total, 4)

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        with self._lock:
            recent = [(ts, r) for ts, r in self._records if ts >= cutoff]
            completed = sum(1 for ts in self._completions if ts >= cutoff)

        if not recent:
            total = completed
            return {
                "window_seconds": window_seconds,
                "abandoned": 0,
                "completed": completed,
                "total": total,
                "abandonment_rate": 0.0,
            }

        records = [r for _, r in recent]
        by_stage: Dict[str, int] = defaultdict(int)
        by_reason: Dict[str, int] = defaultdict(int)
        by_category: Dict[str, int] = defaultdict(int)

        for r in records:
            by_stage[r.stage.value] += 1
            by_reason[r.reason.value] += 1
            by_category[r.goal_category] += 1

        total = len(records) + completed
        avg_turn = sum(r.turn_count for r in records) / len(records)

        return {
            "window_seconds": window_seconds,
            "abandoned": len(records),
            "completed": completed,
            "total": total,
            "abandonment_rate": round(len(records) / max(total, 1), 4),
            "by_stage": dict(by_stage),
            "by_reason": dict(by_reason),
            "by_category": dict(by_category),
            "avg_turns_at_abandonment": round(avg_turn, 2),
        }
```

## Solution 4: Session Closure Handler

```python
import time
from typing import Callable, Optional


class SessionClosureHandler:
    """
    Called when a session closes (timeout or explicit exit).
    Classifies the session as abandoned or completed and records it.
    """

    def __init__(
        self,
        detector: AbandonmentDetector,
        recorder: AbandonmentRateRecorder,
        audit_fn: Optional[Callable[[dict], None]] = None,
    ):
        self._detector = detector
        self._recorder = recorder
        self._audit = audit_fn or (lambda ev: None)

    def handle_closure(
        self,
        session_id: str,
        turn_count: int,
        agent_turns: int,
        started_at: float,
        last_activity_at: float,
        last_latency_ms: Optional[float],
        error_count: int,
        goal_completed: bool,
        goal_category: str = "general",
    ) -> dict:
        duration = time.time() - started_at
        idle = time.time() - last_activity_at

        record = self._detector.classify(
            session_id=session_id,
            turn_count=turn_count,
            agent_turns=agent_turns,
            duration_seconds=duration,
            last_activity_seconds_ago=idle,
            last_latency_ms=last_latency_ms,
            error_count=error_count,
            goal_completed=goal_completed,
            goal_category=goal_category,
        )

        if record is not None:
            self._recorder.record_abandonment(record)
            self._audit({
                "event": "session_abandoned",
                "session_id": session_id,
                "stage": record.stage.value,
                "reason": record.reason.value,
                "turn_count": turn_count,
                "timestamp": time.time(),
            })
            return {"outcome": "abandoned", "stage": record.stage.value}
        else:
            self._recorder.record_completion()
            return {"outcome": "completed"}
```

## Solution 5: Abandonment Trend Analyzer

```python
import time
from typing import Optional


class AbandonmentTrendAnalyzer:
    """
    Detects abandonment rate increases by comparing recent window
    against a baseline window, signaling regression.
    """

    def __init__(
        self,
        recorder: AbandonmentRateRecorder,
        regression_threshold_pct: float = 20.0,
    ):
        self._recorder = recorder
        self._threshold = regression_threshold_pct / 100.0

    def check_regression(
        self,
        baseline_window: float = 86400.0,
        recent_window: float = 3600.0,
    ) -> dict:
        baseline_rate = self._recorder.abandonment_rate(baseline_window)
        recent_rate = self._recorder.abandonment_rate(recent_window)

        if baseline_rate is None or recent_rate is None:
            return {"status": "insufficient_data"}

        change = (recent_rate - baseline_rate) / max(baseline_rate, 0.001)
        regressed = change > self._threshold

        return {
            "status": "regression" if regressed else "ok",
            "baseline_rate": baseline_rate,
            "recent_rate": recent_rate,
            "change_pct": round(change * 100, 1),
            "regressed": regressed,
        }
```

## Solution 6: Abandonment Dashboard

```python
import time


class SessionAbandonmentDashboard:
    """
    Combines abandonment summary, trend analysis, and stage breakdown
    into a single UX health report.
    """

    def __init__(
        self,
        recorder: AbandonmentRateRecorder,
        analyzer: AbandonmentTrendAnalyzer,
    ):
        self._recorder = recorder
        self._analyzer = analyzer

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "last_1h": self._recorder.summary(window_seconds=3600.0),
            "last_24h": self._recorder.summary(window_seconds=86400.0),
            "trend": self._analyzer.check_regression(),
        }
```

## Comparison

| Approach | Stage Classification | Reason Detection | Rate Calculation | Trend Detection | Dashboard |
|---|---|---|---|---|---|
| AbandonmentDetector | Yes (5 stages) | Yes (4 reasons) | No | No | No |
| AbandonmentRateRecorder | No | No | Yes | No | No |
| SessionClosureHandler | Via detector | Via detector | Via recorder | No | Yes (audit) |
| AbandonmentTrendAnalyzer | No | No | Via recorder | Yes | No |
| SessionAbandonmentDashboard | No | No | No | No | Yes |

**Best for production**: Alert when `AFTER_FIRST_RESPONSE` abandonment rate exceeds 30% — this is the highest-value signal of agent quality, indicating that the first reply consistently fails to engage users. Track abandonment rate separately for `SLOW_RESPONSE` reason: if this exceeds 15%, the latency problem is directly causing user loss, not just degrading experience. Compare `by_category` abandonment rates to identify which task types the agent handles poorly — categories with >50% abandonment rate are candidates for capability improvements or user expectation management.
