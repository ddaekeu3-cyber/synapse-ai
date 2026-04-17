---
title: "Agent Doesn't Implement Conversation Abandonment Rate Tracking"
description: "Agents that measure completion rates without tracking abandonment cannot distinguish between users who received an answer and left satisfied versus users who gave up mid-conversation because the agent was too slow, too wrong, or too confusing. Implement conversation abandonment rate tracking that classifies session endings by pattern, identifies abandonment-correlated tool failures, and surfaces the turns where users most often disengage."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-conversation-abandonment-rate-tracking
tags: [abandonment-rate, user-engagement, conversation-quality, session-analysis, ux-observability, drop-off-tracking]
symptoms:
  - "Session completion rate looks fine but users report frustration — abandonment is not measured"
  - "No distinction between clean conversation endings and mid-conversation drop-offs"
  - "Cannot identify which tool failures correlate with users leaving"
  - "No data on which turn in a conversation users most often abandon"
  - "A/B tests show completion rate differences but not quality-of-abandonment differences"
---

## Why This Happens

A conversation that ends is not necessarily a conversation that succeeded. Users abandon when the agent is too slow, gives wrong answers, loops on the same error, or fails to understand the request after multiple attempts. Without classifying session endings, every ended session looks identical. Abandonment tracking requires a timeout-based classifier that separates natural endings (user received an answer and stopped) from abrupt endings (user stopped in the middle of a tool call or retry loop), plus correlation with the agent events that immediately preceded the drop-off.

## Solution 1: Conversation Session Record

```python
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class SessionEndReason(str, Enum):
    COMPLETED = "completed"         # user received final answer
    ABANDONED_MID_TURN = "abandoned_mid_turn"     # dropped during tool execution
    ABANDONED_AFTER_ERROR = "abandoned_after_error"  # dropped after agent error
    ABANDONED_AFTER_RETRY = "abandoned_after_retry"  # dropped after retry loop
    TIMEOUT = "timeout"             # no activity for N minutes
    UNKNOWN = "unknown"


@dataclass
class TurnEvent:
    turn_number: int
    role: str                    # "user" | "agent" | "tool"
    event_type: str              # "message" | "tool_call" | "tool_error" | "retry"
    timestamp: float = field(default_factory=time.time)
    latency_ms: Optional[float] = None
    had_error: bool = False
    tool_name: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ConversationSession:
    session_id: str
    started_at: float = field(default_factory=time.time)
    ended_at: Optional[float] = None
    end_reason: SessionEndReason = SessionEndReason.UNKNOWN
    turn_events: List[TurnEvent] = field(default_factory=list)
    abandonment_turn: Optional[int] = None
    user_id: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def duration_seconds(self) -> Optional[float]:
        if self.ended_at is None:
            return None
        return round(self.ended_at - self.started_at, 2)

    @property
    def turn_count(self) -> int:
        return max((e.turn_number for e in self.turn_events), default=0)

    @property
    def error_count(self) -> int:
        return sum(1 for e in self.turn_events if e.had_error)

    def is_abandoned(self) -> bool:
        return self.end_reason not in (SessionEndReason.COMPLETED, SessionEndReason.UNKNOWN)
```

## Solution 2: Abandonment Classifier

```python
import time
from typing import List, Optional


class AbandonmentClassifier:
    """
    Classifies a conversation's end reason based on its event sequence.
    Uses heuristics: last event type, error presence, retry patterns.
    """

    def __init__(
        self,
        timeout_seconds: float = 300.0,
        retry_threshold: int = 2,
    ):
        self._timeout = timeout_seconds
        self._retry_threshold = retry_threshold

    def classify(self, session: ConversationSession) -> SessionEndReason:
        events = session.turn_events
        if not events:
            return SessionEndReason.UNKNOWN

        last_event = events[-1]
        now = time.time()

        # Timeout: last activity was too long ago and session not closed
        if session.ended_at is None:
            idle = now - last_event.timestamp
            if idle > self._timeout:
                return SessionEndReason.TIMEOUT

        # Check last event type
        if last_event.event_type == "tool_call" and not last_event.had_error:
            return SessionEndReason.ABANDONED_MID_TURN

        if last_event.had_error or last_event.event_type == "tool_error":
            return SessionEndReason.ABANDONED_AFTER_ERROR

        # Retry pattern: multiple consecutive retries before end
        retry_events = [e for e in events[-5:] if e.event_type == "retry"]
        if len(retry_events) >= self._retry_threshold:
            return SessionEndReason.ABANDONED_AFTER_RETRY

        # Last role was agent providing an answer
        if last_event.role == "agent" and last_event.event_type == "message":
            return SessionEndReason.COMPLETED

        return SessionEndReason.UNKNOWN

    def abandonment_turn(self, session: ConversationSession) -> Optional[int]:
        if not session.is_abandoned():
            return None
        for event in reversed(session.turn_events):
            if event.role == "user":
                return event.turn_number
        return session.turn_count
```

## Solution 3: Abandonment Rate Tracker

```python
import time
from collections import deque
from threading import Lock
from typing import Deque, Dict, List, Optional, Tuple


class AbandonmentRateTracker:
    """
    Accumulates classified session outcomes and computes abandonment
    rates over sliding time windows.
    """

    def __init__(self, window_seconds: int = 3600, max_sessions: int = 50000):
        self._window = window_seconds
        self._max = max_sessions
        self._sessions: Deque[Tuple[float, ConversationSession]] = deque()
        self._lock = Lock()

    def record(self, session: ConversationSession) -> None:
        ts = session.ended_at or time.time()
        with self._lock:
            self._sessions.append((ts, session))
            if len(self._sessions) > self._max:
                self._sessions.popleft()

    def _recent(self, sub_window: Optional[int] = None) -> List[ConversationSession]:
        cutoff = time.time() - (sub_window or self._window)
        with self._lock:
            return [s for ts, s in self._sessions if ts >= cutoff]

    def abandonment_rate(self, sub_window_seconds: Optional[int] = None) -> float:
        sessions = self._recent(sub_window_seconds)
        if not sessions:
            return 0.0
        abandoned = sum(1 for s in sessions if s.is_abandoned())
        return abandoned / len(sessions)

    def by_reason(self, sub_window_seconds: Optional[int] = None) -> Dict[str, int]:
        sessions = self._recent(sub_window_seconds)
        result: dict = {}
        for s in sessions:
            reason = s.end_reason.value
            result[reason] = result.get(reason, 0) + 1
        return result

    def abandonment_turn_distribution(self, sub_window_seconds: Optional[int] = None) -> Dict[int, int]:
        sessions = self._recent(sub_window_seconds)
        dist: dict = {}
        for s in sessions:
            if s.is_abandoned() and s.abandonment_turn is not None:
                turn = s.abandonment_turn
                dist[turn] = dist.get(turn, 0) + 1
        return dict(sorted(dist.items()))

    def summary(self, window_seconds: Optional[int] = None) -> dict:
        sessions = self._recent(window_seconds)
        if not sessions:
            return {"sessions": 0}
        abandoned = [s for s in sessions if s.is_abandoned()]
        completed = [s for s in sessions if s.end_reason == SessionEndReason.COMPLETED]

        return {
            "sessions": len(sessions),
            "completed": len(completed),
            "abandoned": len(abandoned),
            "abandonment_rate": round(len(abandoned) / len(sessions), 4),
            "completion_rate": round(len(completed) / len(sessions), 4),
            "by_reason": self.by_reason(window_seconds),
            "turn_distribution": self.abandonment_turn_distribution(window_seconds),
            "avg_turns_before_abandon": round(
                sum(s.turn_count for s in abandoned) / max(len(abandoned), 1), 2
            ),
        }
```

## Solution 4: Tool Failure Abandonment Correlator

```python
from typing import Dict, List


class ToolFailureAbandonmentCorrelator:
    """
    Identifies which tool failures most strongly correlate with session abandonment.
    A tool with high failure-abandonment correlation is a prime optimization target.
    """

    def __init__(self, tracker: AbandonmentRateTracker):
        self._tracker = tracker

    def correlations(self, sub_window_seconds: int = 3600) -> List[dict]:
        sessions = self._tracker._recent(sub_window_seconds)
        tool_stats: Dict[str, dict] = {}

        for session in sessions:
            tool_errors = [e for e in session.turn_events if e.had_error and e.tool_name]
            for event in tool_errors:
                name = event.tool_name
                if name not in tool_stats:
                    tool_stats[name] = {"errors": 0, "led_to_abandon": 0}
                tool_stats[name]["errors"] += 1
                if session.is_abandoned():
                    tool_stats[name]["led_to_abandon"] += 1

        result = []
        for tool_name, stats in tool_stats.items():
            abandon_rate = stats["led_to_abandon"] / max(stats["errors"], 1)
            result.append({
                "tool_name": tool_name,
                "error_count": stats["errors"],
                "abandonment_count": stats["led_to_abandon"],
                "abandonment_rate_when_error": round(abandon_rate, 4),
            })

        return sorted(result, key=lambda r: r["abandonment_rate_when_error"], reverse=True)
```

## Solution 5: Session Abandonment Monitor

```python
import asyncio
import time
from typing import Callable, Dict, List, Optional


class SessionAbandonmentMonitor:
    """
    Monitors active sessions for timeout-based abandonment.
    Classifies and records sessions that go idle beyond the timeout threshold.
    """

    def __init__(
        self,
        classifier: AbandonmentClassifier,
        tracker: AbandonmentRateTracker,
        check_interval_seconds: float = 60.0,
    ):
        self._classifier = classifier
        self._tracker = tracker
        self._interval = check_interval_seconds
        self._active_sessions: Dict[str, ConversationSession] = {}
        self._running = False

    def register_session(self, session: ConversationSession) -> None:
        self._active_sessions[session.session_id] = session

    def close_session(self, session_id: str, completed: bool = False) -> None:
        session = self._active_sessions.pop(session_id, None)
        if session:
            session.ended_at = time.time()
            if completed:
                session.end_reason = SessionEndReason.COMPLETED
            else:
                session.end_reason = self._classifier.classify(session)
                session.abandonment_turn = self._classifier.abandonment_turn(session)
            self._tracker.record(session)

    async def run_loop(self) -> None:
        self._running = True
        while self._running:
            now = time.time()
            timed_out = [
                sid for sid, session in list(self._active_sessions.items())
                if session.turn_events
                and now - session.turn_events[-1].timestamp > self._classifier._timeout
            ]
            for sid in timed_out:
                self.close_session(sid, completed=False)
            await asyncio.sleep(self._interval)

    def stop(self) -> None:
        self._running = False
```

## Solution 6: Abandonment Dashboard

```python
import time


class ConversationAbandonmentDashboard:
    """
    Combines abandonment rates, tool correlations, and turn distribution
    into a single product and engineering health view.
    """

    def __init__(
        self,
        tracker: AbandonmentRateTracker,
        correlator: ToolFailureAbandonmentCorrelator,
        monitor: SessionAbandonmentMonitor,
    ):
        self._tracker = tracker
        self._correlator = correlator
        self._monitor = monitor

    def render(self) -> dict:
        summary = self._tracker.summary(window_seconds=3600)
        correlations = self._correlator.correlations(sub_window_seconds=3600)
        top_correlated = correlations[:5]

        return {
            "generated_at": time.time(),
            "active_sessions": len(self._monitor._active_sessions),
            "summary_1h": summary,
            "top_abandonment_causing_tools": top_correlated,
            "alert": summary.get("abandonment_rate", 0) > 0.30,
        }
```

## Comparison

| Approach | Session Classification | Rate Computation | Tool Correlation | Timeout Detection | Dashboard |
|---|---|---|---|---|---|
| AbandonmentClassifier | Yes (heuristic) | No | No | Yes | No |
| AbandonmentRateTracker | Via classifier | Yes (sliding) | No | No | No |
| ToolFailureAbandonmentCorrelator | No | No | Yes | No | No |
| SessionAbandonmentMonitor | Via classifier | Via tracker | No | Yes (loop) | No |
| ConversationAbandonmentDashboard | No | No | Via correlator | No | Yes |

**Best for production**: Set the abandonment alert threshold at 30% — healthy conversational agents typically achieve 70%+ completion rates. Use `ToolFailureAbandonmentCorrelator` weekly to rank which tools to prioritize for reliability improvements — a tool with 80% abandonment rate when it errors is a higher priority than one with 20%, regardless of total error volume. Record `TurnEvent` for every agent action — the `abandonment_turn_distribution` reveals whether users abandon early (bad first response) or late (tool failure after invested effort). Track abandonment rate separately for first-time users versus returning users: high first-time abandonment indicates onboarding friction, not tool reliability.
