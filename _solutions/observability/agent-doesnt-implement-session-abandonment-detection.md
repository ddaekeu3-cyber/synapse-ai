---
title: "Agent Doesn't Implement Session Abandonment Detection"
description: "Agents that track only completed sessions miss a critical signal: sessions where the user stopped responding mid-conversation, indicating confusion, frustration, or a failure the agent did not detect. Implement session abandonment detection that classifies sessions as abandoned based on inactivity duration, last agent message sentiment, and conversation stage — enabling product teams to identify the failure patterns that cause users to give up."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-session-abandonment-detection
tags: [session-abandonment, user-engagement, conversation-analytics, dropout-detection, ux-observability, funnel-analysis]
symptoms:
  - "No distinction between sessions that completed successfully and sessions where users stopped responding"
  - "Cannot identify which agent responses most often precede user dropout"
  - "Product team cannot measure abandonment rate as a quality metric"
  - "Sessions with tool errors show same completion rate as successful sessions"
  - "No alerting when abandonment rate spikes — only detected via weekly user surveys"
---

## Why This Happens

Most agent observability focuses on what the agent did — tool calls, latency, errors. What the user did after the agent responded is equally important but harder to capture: did they reply? Did they come back? Did they give up? Without tracking inactivity duration and classifying sessions as abandoned or completed, the agent's view of quality is purely server-side. A session where the agent returned a confident but wrong answer looks identical to a successful session in server logs. Abandonment detection requires tracking the time since the last user message, the content of the last agent turn, and session metadata to classify inactive sessions as abandoned or simply idle.

## Solution 1: Session Activity Record

```python
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


class SessionOutcome(str, Enum):
    ACTIVE = "active"
    COMPLETED = "completed"          # user explicitly ended or confirmed success
    ABANDONED = "abandoned"          # user stopped responding
    TIMED_OUT = "timed_out"          # hard session TTL exceeded
    ERROR_TERMINATED = "error_terminated"  # agent-side error ended the session


@dataclass
class SessionTurn:
    role: str         # "user" | "agent"
    content_length: int
    timestamp: float
    had_tool_error: bool = False
    had_agent_error: bool = False


@dataclass
class SessionActivityRecord:
    session_id: str
    user_id: str
    started_at: float = field(default_factory=time.time)
    last_user_message_at: Optional[float] = None
    last_agent_message_at: Optional[float] = None
    turn_count: int = 0
    user_turn_count: int = 0
    agent_turn_count: int = 0
    outcome: SessionOutcome = SessionOutcome.ACTIVE
    turns: List[SessionTurn] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    def record_turn(self, role: str, content_length: int, had_error: bool = False) -> None:
        now = time.time()
        self.turns.append(SessionTurn(
            role=role,
            content_length=content_length,
            timestamp=now,
            had_tool_error=(had_error and role == "agent"),
            had_agent_error=(had_error and role == "agent"),
        ))
        self.turn_count += 1
        if role == "user":
            self.user_turn_count += 1
            self.last_user_message_at = now
        else:
            self.agent_turn_count += 1
            self.last_agent_message_at = now

    def inactivity_seconds(self) -> float:
        """Seconds since the last user message."""
        if self.last_user_message_at is None:
            return time.time() - self.started_at
        return time.time() - self.last_user_message_at

    def agent_last_responded(self) -> bool:
        """True if the last turn was from the agent (user hasn't replied yet)."""
        if not self.turns:
            return False
        return self.turns[-1].role == "agent"
```

## Solution 2: Abandonment Classifier

```python
import time
from dataclasses import dataclass
from typing import Optional


@dataclass
class AbandonmentSignals:
    inactivity_seconds: float
    agent_last_responded: bool
    had_recent_tool_error: bool
    turn_count: int
    last_agent_content_length: int


class SessionAbandonmentClassifier:
    """
    Classifies a session as abandoned based on inactivity duration
    and conversation signals that predict abandonment.
    """

    def __init__(
        self,
        inactivity_threshold_seconds: float = 300.0,   # 5 minutes
        short_session_threshold_turns: int = 2,         # very short sessions
        error_inactivity_threshold_seconds: float = 120.0,  # faster timeout after errors
    ):
        self._inactivity = inactivity_threshold_seconds
        self._short_threshold = short_session_threshold_turns
        self._error_inactivity = error_inactivity_threshold_seconds

    def classify(self, record: SessionActivityRecord) -> tuple:
        """
        Returns (is_abandoned, reason).
        """
        if record.outcome != SessionOutcome.ACTIVE:
            return False, "session_already_classified"

        inactivity = record.inactivity_seconds()
        agent_last = record.agent_last_responded()

        # Must have at least one agent turn for abandonment to apply
        if not agent_last:
            return False, "waiting_for_agent"

        # Check for error-accelerated abandonment
        recent_error = any(
            t.had_tool_error
            for t in record.turns[-3:]
            if t.role == "agent"
        )
        threshold = (
            self._error_inactivity if recent_error else self._inactivity
        )

        if inactivity >= threshold:
            reason = "error_followed_by_inactivity" if recent_error else "inactivity_after_agent_response"
            return True, reason

        # Very short sessions that went silent may be confused users
        if (
            record.turn_count <= self._short_threshold
            and inactivity >= self._inactivity / 2
            and agent_last
        ):
            return True, "abandoned_after_short_session"

        return False, "still_active"
```

## Solution 3: Session Abandonment Store

```python
import time
from collections import defaultdict
from threading import Lock
from typing import Dict, List, Optional


class SessionAbandonmentStore:
    """
    Maintains active session records and classifies them on a polling interval.
    """

    def __init__(
        self,
        classifier: SessionAbandonmentClassifier,
        max_sessions: int = 50000,
        session_ttl_seconds: float = 3600.0,
    ):
        self._classifier = classifier
        self._max = max_sessions
        self._ttl = session_ttl_seconds
        self._sessions: Dict[str, SessionActivityRecord] = {}
        self._lock = Lock()

    def upsert(self, record: SessionActivityRecord) -> None:
        with self._lock:
            if len(self._sessions) >= self._max and record.session_id not in self._sessions:
                oldest = min(self._sessions, key=lambda s: self._sessions[s].started_at)
                del self._sessions[oldest]
            self._sessions[record.session_id] = record

    def get(self, session_id: str) -> Optional[SessionActivityRecord]:
        with self._lock:
            return self._sessions.get(session_id)

    def classify_active_sessions(self) -> List[dict]:
        """Scan all active sessions and classify abandonments."""
        now = time.time()
        classified = []
        with self._lock:
            for record in list(self._sessions.values()):
                if record.outcome != SessionOutcome.ACTIVE:
                    continue
                # Hard TTL eviction
                if now - record.started_at > self._ttl:
                    record.outcome = SessionOutcome.TIMED_OUT
                    classified.append({
                        "session_id": record.session_id,
                        "outcome": SessionOutcome.TIMED_OUT.value,
                        "reason": "ttl_exceeded",
                    })
                    continue

                abandoned, reason = self._classifier.classify(record)
                if abandoned:
                    record.outcome = SessionOutcome.ABANDONED
                    classified.append({
                        "session_id": record.session_id,
                        "user_id": record.user_id,
                        "outcome": SessionOutcome.ABANDONED.value,
                        "reason": reason,
                        "turn_count": record.turn_count,
                        "inactivity_seconds": round(record.inactivity_seconds(), 1),
                    })
        return classified

    def abandonment_rate(self, window_seconds: float = 3600.0) -> float:
        cutoff = time.time() - window_seconds
        with self._lock:
            recent = [
                r for r in self._sessions.values()
                if r.started_at >= cutoff
            ]
        if not recent:
            return 0.0
        abandoned = sum(1 for r in recent if r.outcome == SessionOutcome.ABANDONED)
        completed = sum(1 for r in recent if r.outcome in (
            SessionOutcome.COMPLETED, SessionOutcome.ABANDONED, SessionOutcome.ERROR_TERMINATED
        ))
        return round(abandoned / max(completed, 1), 4)
```

## Solution 4: Abandonment Pattern Analyzer

```python
from collections import Counter
from typing import List


class AbandonmentPatternAnalyzer:
    """
    Analyzes abandoned sessions to identify which agent behaviors
    most frequently precede abandonment.
    """

    def __init__(self, store: SessionAbandonmentStore):
        self._store = store

    def analyze(self) -> dict:
        with self._store._lock:
            abandoned = [
                r for r in self._store._sessions.values()
                if r.outcome == SessionOutcome.ABANDONED
            ]

        if not abandoned:
            return {"abandoned_count": 0}

        # Turn count distribution at abandonment
        turn_counts = Counter(r.turn_count for r in abandoned)

        # Error rate in abandoned sessions
        had_errors = sum(
            1 for r in abandoned
            if any(t.had_tool_error for t in r.turns)
        )

        # Short vs long session abandonment
        short = sum(1 for r in abandoned if r.turn_count <= 2)
        long = len(abandoned) - short

        return {
            "abandoned_count": len(abandoned),
            "with_tool_errors": had_errors,
            "error_abandonment_rate": round(had_errors / max(len(abandoned), 1), 3),
            "abandoned_after_short_session": short,
            "abandoned_after_long_session": long,
            "turn_count_distribution": dict(turn_counts.most_common(10)),
        }
```

## Solution 5: Abandonment Alert Manager

```python
import time
from typing import Callable, Optional


class AbandonmentRateAlertManager:
    """
    Fires alerts when the abandonment rate exceeds a threshold.
    """

    def __init__(
        self,
        store: SessionAbandonmentStore,
        threshold_rate: float = 0.20,
        cooldown_seconds: float = 600.0,
        alert_fn: Optional[Callable[[dict], None]] = None,
    ):
        self._store = store
        self._threshold = threshold_rate
        self._cooldown = cooldown_seconds
        self._alert = alert_fn or self._default_alert
        self._last_alerted = 0.0

    @staticmethod
    def _default_alert(event: dict) -> None:
        import json
        print(json.dumps({"event": "abandonment_rate_spike", **event}))

    def check(self) -> Optional[dict]:
        now = time.time()
        if now - self._last_alerted < self._cooldown:
            return None
        rate = self._store.abandonment_rate(window_seconds=3600.0)
        if rate >= self._threshold:
            self._last_alerted = now
            event = {
                "abandonment_rate": rate,
                "threshold": self._threshold,
                "ts": now,
            }
            self._alert(event)
            return event
        return None
```

## Solution 6: Abandonment Dashboard

```python
import time


class SessionAbandonmentDashboard:
    """
    Combines abandonment rate, pattern analysis, and recent classified
    sessions into a single operational view.
    """

    def __init__(
        self,
        store: SessionAbandonmentStore,
        analyzer: AbandonmentPatternAnalyzer,
    ):
        self._store = store
        self._analyzer = analyzer

    def render(self) -> dict:
        classified = self._store.classify_active_sessions()
        return {
            "generated_at": time.time(),
            "abandonment_rate_1h": self._store.abandonment_rate(3600.0),
            "newly_classified": classified,
            "patterns": self._analyzer.analyze(),
        }
```

## Comparison

| Approach | Inactivity Detection | Error-Signal Weighting | Pattern Analysis | Rate Alerting | Dashboard |
|---|---|---|---|---|---|
| SessionAbandonmentClassifier | Yes (threshold) | Yes (faster timeout) | No | No | No |
| SessionAbandonmentStore | Via classifier | Via classifier | No | No | No |
| AbandonmentPatternAnalyzer | No | No | Yes (turn dist + errors) | No | No |
| AbandonmentRateAlertManager | No | No | No | Yes (cooldown) | No |
| SessionAbandonmentDashboard | No | No | Via analyzer | No | Yes |

**Best for production**: Set `inactivity_threshold_seconds=300` (5 minutes) for most agent types — users who intend to continue typically reply within 5 minutes, and longer inactivity reliably indicates abandonment. Reduce to `error_inactivity_threshold_seconds=120` for error-preceded inactivity — a confused user after an error dropout much faster than a user who got a good answer and is thinking. Track `with_tool_errors` in the pattern analyzer: an abandonment rate above 50% with tool errors indicates tool reliability problems, not UX problems. Export `abandonment_rate_1h` as a product SLO metric alongside success rate — target below 15% for conversational agents; above 30% indicates a systemic problem requiring immediate investigation.
