---
title: "Agent Doesn't Implement Multi-Turn Conversation Health Scoring"
description: "Agents that track only individual turn metrics miss the degradation patterns that only become visible across multiple turns: growing latency as context fills, declining response quality as the conversation drifts from the original topic, increasing error rates as tool calls accumulate, and user frustration signals like short follow-up messages. Implement multi-turn conversation health scoring that aggregates per-turn signals into a session-level health score with trend detection and proactive intervention triggers."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-multi-turn-conversation-health-scoring
tags: [conversation-health, multi-turn, session-scoring, trend-detection, user-experience, proactive-intervention]
symptoms:
  - "No session-level metric — only per-turn error counts that don't reveal degradation trends"
  - "Cannot detect when a conversation is progressively getting worse"
  - "User frustration signals (short messages, repeated questions) are not tracked"
  - "Operator cannot tell if a conversation requires intervention without reading the full transcript"
  - "Health dashboard shows only snapshot metrics with no trend across conversation turns"
---

## Why This Happens

Per-turn metrics (latency, error rate, token count) capture what happened in each turn but not the trajectory of the conversation as a whole. Health degradation in multi-turn conversations is a trend phenomenon: latency gradually increases as the context window fills, quality gradually declines as the agent loses track of the original goal, and user signals (shorter messages, rephrasing the same question) indicate frustration that no single-turn metric captures. Session-level health scoring requires aggregating signals across turns, computing weighted scores, detecting trends, and triggering proactive actions when the health score drops below a threshold.

## Solution 1: Turn Health Signal

```python
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional


class TurnOutcome(str, Enum):
    SUCCESS = "success"
    PARTIAL = "partial"
    ERROR = "error"
    TIMEOUT = "timeout"


@dataclass
class TurnHealthSignal:
    turn_index: int
    timestamp: float = field(default_factory=time.time)
    outcome: TurnOutcome = TurnOutcome.SUCCESS
    latency_ms: float = 0.0
    tokens_used: int = 0
    tool_errors: int = 0
    tool_calls: int = 0
    response_length_chars: int = 0
    user_message_length_chars: int = 0
    context_utilization: float = 0.0   # tokens / context_limit
    quality_score: Optional[float] = None  # 0-1 from response quality pipeline

    def is_short_user_message(self, threshold: int = 20) -> bool:
        return 0 < self.user_message_length_chars < threshold

    def has_high_latency(self, threshold_ms: float = 5000.0) -> bool:
        return self.latency_ms > threshold_ms

    def has_errors(self) -> bool:
        return self.outcome in (TurnOutcome.ERROR, TurnOutcome.TIMEOUT) or self.tool_errors > 0
```

## Solution 2: Session Health Scorer

```python
from typing import List, Optional


class SessionHealthScorer:
    """
    Computes a 0-100 health score for a conversation session
    based on the most recent N turns. Lower score = worse health.
    """

    def __init__(
        self,
        lookback_turns: int = 5,
        latency_threshold_ms: float = 5000.0,
        context_warning_threshold: float = 0.75,
    ):
        self._lookback = lookback_turns
        self._latency_thresh = latency_threshold_ms
        self._context_thresh = context_warning_threshold

    def score(self, turns: List[TurnHealthSignal]) -> float:
        if not turns:
            return 100.0

        recent = turns[-self._lookback:]
        penalties = 0.0

        for turn in recent:
            # Error penalty
            if turn.outcome == TurnOutcome.ERROR:
                penalties += 20.0
            elif turn.outcome == TurnOutcome.TIMEOUT:
                penalties += 25.0
            elif turn.outcome == TurnOutcome.PARTIAL:
                penalties += 5.0

            # Tool error penalty
            penalties += min(turn.tool_errors * 5.0, 15.0)

            # Latency penalty
            if turn.latency_ms > self._latency_thresh * 2:
                penalties += 10.0
            elif turn.latency_ms > self._latency_thresh:
                penalties += 5.0

            # Context pressure penalty
            if turn.context_utilization > 0.90:
                penalties += 15.0
            elif turn.context_utilization > self._context_thresh:
                penalties += 5.0

            # User frustration signal (short follow-up messages)
            if turn.is_short_user_message():
                penalties += 3.0

            # Quality score penalty
            if turn.quality_score is not None and turn.quality_score < 0.5:
                penalties += 10.0

        # Normalize: spread penalties over lookback turns
        max_penalty = len(recent) * 50.0
        normalized_penalty = min(penalties / max(max_penalty, 1) * 100.0, 100.0)
        return round(max(0.0, 100.0 - normalized_penalty), 1)
```

## Solution 3: Conversation Health Session

```python
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


class HealthTrend(str, Enum):
    IMPROVING = "improving"
    STABLE = "stable"
    DECLINING = "declining"
    CRITICAL = "critical"


@dataclass
class ConversationHealthSession:
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    started_at: float = field(default_factory=time.time)
    turns: List[TurnHealthSignal] = field(default_factory=list)
    health_scores: List[float] = field(default_factory=list)
    intervention_triggered: bool = False
    last_score: float = 100.0

    def add_turn(self, signal: TurnHealthSignal, score: float) -> None:
        self.turns.append(signal)
        self.health_scores.append(score)
        self.last_score = score

    def trend(self, window: int = 3) -> HealthTrend:
        if len(self.health_scores) < 2:
            return HealthTrend.STABLE
        recent = self.health_scores[-window:]
        if len(recent) < 2:
            return HealthTrend.STABLE
        delta = recent[-1] - recent[0]
        if recent[-1] < 30:
            return HealthTrend.CRITICAL
        if delta < -15:
            return HealthTrend.DECLINING
        if delta > 10:
            return HealthTrend.IMPROVING
        return HealthTrend.STABLE

    def duration_seconds(self) -> float:
        return round(time.time() - self.started_at, 1)

    def summary(self) -> dict:
        return {
            "session_id": self.session_id,
            "turns": len(self.turns),
            "current_health": self.last_score,
            "trend": self.trend().value,
            "duration_seconds": self.duration_seconds(),
            "intervention_triggered": self.intervention_triggered,
        }
```

## Solution 4: Health Monitor

```python
import time
from typing import Callable, Optional


class ConversationHealthMonitor:
    """
    Per-session health monitor that scores each turn and triggers
    interventions when health drops below configured thresholds.
    """

    def __init__(
        self,
        scorer: SessionHealthScorer,
        critical_threshold: float = 30.0,
        warning_threshold: float = 60.0,
        on_warning: Optional[Callable] = None,
        on_critical: Optional[Callable] = None,
    ):
        self._scorer = scorer
        self._critical = critical_threshold
        self._warning = warning_threshold
        self._on_warning = on_warning
        self._on_critical = on_critical

    def observe(
        self,
        session: ConversationHealthSession,
        signal: TurnHealthSignal,
    ) -> float:
        score = self._scorer.score(session.turns + [signal])
        session.add_turn(signal, score)

        if score <= self._critical:
            session.intervention_triggered = True
            if self._on_critical:
                self._on_critical(session)
        elif score <= self._warning:
            if self._on_warning:
                self._on_warning(session)

        return score
```

## Solution 5: Fleet Health Aggregator

```python
import time
from collections import defaultdict
from threading import Lock
from typing import Dict, List


class FleetConversationHealthAggregator:
    """
    Aggregates health scores across all active sessions to detect
    fleet-wide degradation (e.g., an upstream outage affecting all conversations).
    """

    def __init__(self):
        self._sessions: Dict[str, ConversationHealthSession] = {}
        self._lock = Lock()

    def register(self, session: ConversationHealthSession) -> None:
        with self._lock:
            self._sessions[session.session_id] = session

    def deregister(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)

    def fleet_summary(self) -> dict:
        with self._lock:
            sessions = list(self._sessions.values())

        if not sessions:
            return {"active_sessions": 0}

        scores = [s.last_score for s in sessions]
        avg_score = sum(scores) / len(scores)
        critical = sum(1 for s in scores if s <= 30)
        declining = sum(1 for s in sessions if s.trend() == HealthTrend.DECLINING)

        return {
            "active_sessions": len(sessions),
            "avg_health_score": round(avg_score, 1),
            "critical_sessions": critical,
            "declining_sessions": declining,
            "min_score": round(min(scores), 1),
            "max_score": round(max(scores), 1),
        }
```

## Solution 6: Conversation Health Dashboard

```python
import time


class ConversationHealthDashboard:
    """
    Combines per-session health and fleet aggregation into a single view.
    """

    def __init__(
        self,
        aggregator: FleetConversationHealthAggregator,
        monitor: ConversationHealthMonitor,
    ):
        self._aggregator = aggregator
        self._monitor = monitor

    def render(self) -> dict:
        fleet = self._aggregator.fleet_summary()
        active = self._aggregator._sessions
        session_summaries = [s.summary() for s in list(active.values())[:20]]
        return {
            "generated_at": time.time(),
            "fleet": fleet,
            "sessions": sorted(
                session_summaries,
                key=lambda s: s["current_health"],
            ),
        }
```

## Comparison

| Approach | Turn Signals | Health Scoring | Trend Detection | Fleet Aggregation | Dashboard |
|---|---|---|---|---|---|
| TurnHealthSignal | Yes | No | No | No | No |
| SessionHealthScorer | Via signals | Yes (weighted) | No | No | No |
| ConversationHealthSession | Via signals | Via scorer | Yes (3-turn window) | No | No |
| ConversationHealthMonitor | Via session | Via scorer | Via session | No | No |
| FleetConversationHealthAggregator | No | No | No | Yes | No |
| ConversationHealthDashboard | No | No | No | No | Yes |

**Best for production**: Instrument every agent turn with `TurnHealthSignal` collection — include at minimum: outcome, latency_ms, tool_errors, and context_utilization. Set `critical_threshold=30` and wire `on_critical` to your incident response workflow — a session with health score below 30 almost certainly requires operator intervention. Monitor `FleetConversationHealthAggregator.fleet_summary()["critical_sessions"]` with a 5-minute rolling alert: more than 5% of sessions critical simultaneously indicates a systemic issue (model degradation, dependency outage) rather than individual conversation problems. Use the `HealthTrend.DECLINING` signal proactively — a session trending from 80 → 65 → 50 has not yet crossed the critical threshold but will, and early intervention (offering to restart the conversation or escalating to a human) is more effective than waiting for a hard failure.
