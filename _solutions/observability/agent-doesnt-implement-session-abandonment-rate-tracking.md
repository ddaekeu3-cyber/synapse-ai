---
title: "Agent Doesn't Implement Session Abandonment Rate Tracking"
description: "Agents with no session abandonment tracking cannot distinguish between completed sessions and sessions where the user gave up: a 40% abandonment rate masked inside an aggregate session count hides that nearly half of users are not getting useful answers. Implement session abandonment rate tracking that detects incomplete sessions, classifies abandonment reasons, and surfaces which turn numbers and tool types correlate with user drop-off."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-session-abandonment-rate-tracking
tags: [session-abandonment, drop-off-tracking, user-retention, session-completion, ux-observability, turn-analysis]
symptoms:
  - "Session count metrics show high volume but user satisfaction is low"
  - "No distinction between sessions that ended with a resolved answer vs. user frustration"
  - "Cannot identify which turn number in a conversation causes the most user drop-off"
  - "No signal that a specific tool failure pattern is driving session abandonment"
  - "Abandonment rate is unknown — only session start and end counts exist"
---

## Why This Happens

Most agent observability tracks requests and responses but not user outcomes. A session that ends after two turns with no tool call and no substantive answer is indistinguishable from a session that ended because the user got exactly what they needed. Detecting abandonment requires defining what a completed session looks like — at minimum, a session where the final turn contained a substantive response — and flagging sessions that ended without reaching that state. Turn-level attribution (which turn caused abandonment) requires correlating abandonment signals with the turn number, the tools called, and the response quality at the time of abandonment.

## Solution 1: Session Completion Criteria

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


class SessionEndReason(str, Enum):
    COMPLETED = "completed"         # user received a satisfactory answer
    ABANDONED_EARLY = "abandoned_early"   # left within first 2 turns
    ABANDONED_MID = "abandoned_mid"       # left mid-conversation
    TIMEOUT = "timeout"             # session expired without activity
    ERROR_EXIT = "error_exit"       # session ended due to an error
    UNKNOWN = "unknown"


@dataclass
class SessionCompletionCriteria:
    min_turns_for_completion: int = 2
    requires_tool_call: bool = False
    requires_substantive_response_chars: int = 100
    timeout_seconds: float = 1800.0   # 30 minutes of inactivity = timeout
    early_abandonment_turns: int = 2  # left at or before this turn = early abandon
```

## Solution 2: Session Lifecycle Recorder

```python
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class TurnRecord:
    turn_number: int
    user_message_chars: int
    response_chars: int
    tools_called: List[str]
    had_error: bool
    duration_ms: float
    recorded_at: float = field(default_factory=time.time)


@dataclass
class SessionLifecycleRecord:
    session_id: str
    user_id: str
    started_at: float
    turns: List[TurnRecord] = field(default_factory=list)
    ended_at: Optional[float] = None
    end_reason: Optional[SessionEndReason] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def duration_seconds(self) -> Optional[float]:
        if self.ended_at:
            return round(self.ended_at - self.started_at, 2)
        return None

    def last_activity(self) -> float:
        if self.turns:
            return self.turns[-1].recorded_at
        return self.started_at
```

## Solution 3: Session Abandonment Classifier

```python
import time
from typing import Optional


class SessionAbandonmentClassifier:
    """
    Classifies a session's end reason based on its turn history
    and the configured completion criteria.
    """

    def __init__(self, criteria: SessionCompletionCriteria):
        self._criteria = criteria

    def classify(self, session: SessionLifecycleRecord) -> SessionEndReason:
        turns = session.turns
        n_turns = len(turns)

        if n_turns == 0:
            return SessionEndReason.ABANDONED_EARLY

        # Check for timeout
        idle = time.time() - session.last_activity()
        if session.ended_at is None and idle > self._criteria.timeout_seconds:
            return SessionEndReason.TIMEOUT

        # Check for error exit
        if turns and turns[-1].had_error:
            return SessionEndReason.ERROR_EXIT

        # Check completion criteria
        has_enough_turns = n_turns >= self._criteria.min_turns_for_completion
        has_tool_call = (
            not self._criteria.requires_tool_call
            or any(t.tools_called for t in turns)
        )
        last_response_substantial = (
            turns[-1].response_chars >= self._criteria.requires_substantive_response_chars
            if turns else False
        )

        if has_enough_turns and has_tool_call and last_response_substantial:
            return SessionEndReason.COMPLETED

        if n_turns <= self._criteria.early_abandonment_turns:
            return SessionEndReason.ABANDONED_EARLY

        return SessionEndReason.ABANDONED_MID
```

## Solution 4: Abandonment Pattern Analyzer

```python
from collections import Counter
from typing import Dict, List, Optional, Tuple


class AbandonmentPatternAnalyzer:
    """
    Analyzes a set of classified sessions to identify which turn numbers,
    tools, and response patterns correlate with abandonment.
    """

    def analyze(
        self, sessions: List[SessionLifecycleRecord]
    ) -> dict:
        abandoned = [
            s for s in sessions
            if s.end_reason in (
                SessionEndReason.ABANDONED_EARLY,
                SessionEndReason.ABANDONED_MID,
            )
        ]
        completed = [
            s for s in sessions
            if s.end_reason == SessionEndReason.COMPLETED
        ]

        # Turn number distribution at abandonment
        abandon_turn_counts: Counter = Counter()
        for s in abandoned:
            turn_n = len(s.turns)
            abandon_turn_counts[turn_n] += 1

        # Tools called in sessions that were abandoned
        abandoned_tools: Counter = Counter()
        for s in abandoned:
            for turn in s.turns:
                for tool in turn.tools_called:
                    abandoned_tools[tool] += 1

        # Error rate comparison
        abandon_error_rate = (
            sum(1 for s in abandoned if any(t.had_error for t in s.turns))
            / max(len(abandoned), 1)
        )
        complete_error_rate = (
            sum(1 for s in completed if any(t.had_error for t in s.turns))
            / max(len(completed), 1)
        )

        total = len(sessions)
        return {
            "total_sessions": total,
            "completed": len(completed),
            "abandoned": len(abandoned),
            "abandonment_rate": round(len(abandoned) / max(total, 1), 4),
            "abandon_at_turn": dict(abandon_turn_counts.most_common(5)),
            "top_tools_at_abandon": dict(abandoned_tools.most_common(5)),
            "abandon_error_rate": round(abandon_error_rate, 4),
            "complete_error_rate": round(complete_error_rate, 4),
        }
```

## Solution 5: Session Abandonment Registry

```python
import time
from threading import Lock
from typing import Dict, List, Optional


class SessionAbandonmentRegistry:
    """
    Maintains active and completed session records, classifies
    end reasons, and sweeps timed-out sessions periodically.
    """

    def __init__(
        self,
        criteria: SessionCompletionCriteria,
        classifier: SessionAbandonmentClassifier,
        max_sessions: int = 100000,
    ):
        self._criteria = criteria
        self._classifier = classifier
        self._max = max_sessions
        self._active: Dict[str, SessionLifecycleRecord] = {}
        self._completed: List[SessionLifecycleRecord] = []
        self._lock = Lock()

    def start_session(self, session_id: str, user_id: str = "") -> None:
        with self._lock:
            self._active[session_id] = SessionLifecycleRecord(
                session_id=session_id,
                user_id=user_id,
                started_at=time.time(),
            )

    def record_turn(self, session_id: str, turn: TurnRecord) -> None:
        with self._lock:
            if session_id in self._active:
                self._active[session_id].turns.append(turn)

    def end_session(self, session_id: str) -> Optional[SessionLifecycleRecord]:
        with self._lock:
            session = self._active.pop(session_id, None)
            if session is None:
                return None
            session.ended_at = time.time()
            session.end_reason = self._classifier.classify(session)
            self._completed.append(session)
            if len(self._completed) > self._max:
                self._completed.pop(0)
            return session

    def sweep_timeouts(self) -> List[SessionLifecycleRecord]:
        timed_out = []
        with self._lock:
            now = time.time()
            expired = [
                sid for sid, s in self._active.items()
                if now - s.last_activity() > self._criteria.timeout_seconds
            ]
            for sid in expired:
                session = self._active.pop(sid)
                session.ended_at = now
                session.end_reason = SessionEndReason.TIMEOUT
                self._completed.append(session)
                timed_out.append(session)
        return timed_out

    def recent_completed(self, limit: int = 1000) -> List[SessionLifecycleRecord]:
        with self._lock:
            return list(self._completed[-limit:])
```

## Solution 6: Session Abandonment Dashboard

```python
import time


class SessionAbandonmentDashboard:
    """
    Surfaces abandonment rates, turn-level drop-off patterns, and
    tool correlations in a single operational report.
    """

    def __init__(
        self,
        registry: SessionAbandonmentRegistry,
        analyzer: AbandonmentPatternAnalyzer,
    ):
        self._registry = registry
        self._analyzer = analyzer

    def render(self) -> dict:
        sessions = self._registry.recent_completed(limit=5000)
        analysis = self._analyzer.analyze(sessions)
        return {
            "generated_at": time.time(),
            "active_sessions": len(self._registry._active),
            **analysis,
        }
```

## Comparison

| Approach | End-Reason Classification | Turn Attribution | Tool Correlation | Timeout Sweep | Dashboard |
|---|---|---|---|---|---|
| SessionAbandonmentClassifier | Yes (5 reasons) | No | No | No | No |
| AbandonmentPatternAnalyzer | No | Yes (turn counts) | Yes (tool counts) | No | No |
| SessionAbandonmentRegistry | Via classifier | Via turn records | Via records | Yes (sweep) | No |
| SessionAbandonmentDashboard | No | Via analyzer | Via analyzer | No | Yes |

**Best for production**: Instrument `record_turn()` at the agent's response dispatch layer so every turn is tracked without manual integration in individual tools. Run `sweep_timeouts()` on a 5-minute cron to capture sessions that ended due to inactivity without an explicit close event. Set `requires_substantive_response_chars=100` as the completion threshold — responses shorter than 100 characters are typically error messages or clarifying questions, not substantive answers. Alert when `abandonment_rate` exceeds 25% over a rolling 24-hour window — above that level, systematic quality or reliability issues are likely driving users away before they get useful answers.
