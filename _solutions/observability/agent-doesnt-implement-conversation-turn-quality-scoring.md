---
title: "Agent Doesn't Implement Conversation Turn Quality Scoring"
description: "Agents that never evaluate individual conversation turns cannot detect which turn in a multi-turn session caused quality degradation, cannot identify whether the agent or the user drove the session off-track, and cannot improve prompts based on turn-level signals. Implement conversation turn quality scoring that evaluates each agent response against criteria including relevance, completeness, coherence, and grounding, and accumulates turn scores into a session quality trajectory."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-conversation-turn-quality-scoring
tags: [turn-quality, conversation-quality, response-evaluation, quality-trajectory, grounding, session-scoring]
symptoms:
  - "Session quality drops at turn 4 but the session-level score masks which turn failed"
  - "No signal for whether a response was relevant to the user's actual question"
  - "Cannot distinguish sessions that degraded gradually from those that failed at a single turn"
  - "Prompt improvement is guesswork because there is no per-turn quality breakdown"
  - "Users abandon sessions after a specific turn but there is no quality data to diagnose why"
---

## Why This Happens

Session-level quality metrics average over all turns and hide the turn where quality breaks down. A session scored 0.6 overall might have turns scored 0.9, 0.85, 0.8, 0.2, 0.3 — the quality cliff at turn 4 is the actionable signal. Per-turn scoring requires evaluating each agent response against the preceding user message and the conversation context, scoring multiple dimensions (relevance, completeness, grounding), and tracking the trajectory to detect degradation patterns.

## Solution 1: Turn Record

```python
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ConversationTurn:
    turn_id: str
    session_id: str
    turn_index: int
    user_message: str
    agent_response: str
    tool_calls_made: List[str] = field(default_factory=list)
    retrieved_chunks: List[str] = field(default_factory=list)
    model: str = ""
    latency_ms: float = 0.0
    token_count: int = 0
    timestamp: float = field(default_factory=time.time)
    quality_score: Optional[float] = None
    score_components: Dict[str, float] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def is_scored(self) -> bool:
        return self.quality_score is not None
```

## Solution 2: Turn Quality Scorer

```python
import re
from typing import List, Optional


class TurnQualityScorer:
    """
    Scores a conversation turn across multiple quality dimensions:
    - relevance: does the response address the user's question?
    - completeness: does it cover all parts of the request?
    - grounding: does it reference retrieved context where appropriate?
    - coherence: is it well-structured and internally consistent?
    - brevity: is it appropriately concise (not padded, not truncated)?

    Each dimension is scored 0.0–1.0 with configurable weights.
    """

    DIMENSION_WEIGHTS = {
        "relevance": 0.35,
        "completeness": 0.25,
        "grounding": 0.20,
        "coherence": 0.15,
        "brevity": 0.05,
    }

    # Patterns that indicate poor quality
    REFUSAL_PATTERNS = re.compile(
        r"(i (can'?t|cannot|am unable)|i don'?t know|i'm not sure|no information)",
        re.IGNORECASE,
    )
    REPETITION_PATTERN = re.compile(r"(\b\w{4,}\b)(?:\s+\S+){0,5}\s+\1", re.IGNORECASE)
    TRUNCATION_MARKERS = re.compile(r"(\.\.\.|to be continued|continued below)", re.IGNORECASE)

    def score(self, turn: ConversationTurn) -> ConversationTurn:
        response = turn.agent_response
        user_msg = turn.user_message

        components = {}

        # Relevance: key terms from user message present in response
        user_terms = set(re.findall(r"\b\w{4,}\b", user_msg.lower()))
        resp_terms = set(re.findall(r"\b\w{4,}\b", response.lower()))
        overlap = len(user_terms & resp_terms)
        components["relevance"] = min(1.0, overlap / max(len(user_terms), 1) * 2)

        # Completeness: response length relative to question complexity
        question_count = user_msg.count("?") + user_msg.count(" and ")
        expected_min_words = max(30, question_count * 40)
        word_count = len(response.split())
        components["completeness"] = min(1.0, word_count / expected_min_words)

        # Grounding: response references retrieved chunks
        if turn.retrieved_chunks:
            grounded = any(
                any(chunk_term in response.lower()
                    for chunk_term in re.findall(r"\b\w{5,}\b", chunk.lower())[:10])
                for chunk in turn.retrieved_chunks[:3]
            )
            components["grounding"] = 0.9 if grounded else 0.3
        else:
            components["grounding"] = 0.7   # no retrieval expected

        # Coherence: penalize repetition and refusals
        coherence = 1.0
        if self.REFUSAL_PATTERNS.search(response):
            coherence *= 0.5
        if self.REPETITION_PATTERN.search(response):
            coherence *= 0.7
        components["coherence"] = coherence

        # Brevity: penalize very short or excessively long responses
        if word_count < 10:
            components["brevity"] = 0.3
        elif word_count > 800:
            components["brevity"] = 0.7
        else:
            components["brevity"] = 1.0
        if self.TRUNCATION_MARKERS.search(response):
            components["brevity"] *= 0.5

        composite = sum(
            components[dim] * weight
            for dim, weight in self.DIMENSION_WEIGHTS.items()
        )

        turn.quality_score = round(composite, 4)
        turn.score_components = {k: round(v, 4) for k, v in components.items()}
        return turn
```

## Solution 3: Session Quality Trajectory

```python
import time
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class QualityTrajectory:
    session_id: str
    turn_scores: List[float] = field(default_factory=list)
    turn_indices: List[int] = field(default_factory=list)

    def add(self, turn: ConversationTurn) -> None:
        if turn.is_scored():
            self.turn_scores.append(turn.quality_score)
            self.turn_indices.append(turn.turn_index)

    def session_mean(self) -> Optional[float]:
        if not self.turn_scores:
            return None
        return round(sum(self.turn_scores) / len(self.turn_scores), 4)

    def degradation_turn(self, threshold: float = 0.3) -> Optional[int]:
        """Returns the turn index where quality first dropped below threshold."""
        for idx, score in zip(self.turn_indices, self.turn_scores):
            if score < threshold:
                return idx
        return None

    def trend(self) -> str:
        if len(self.turn_scores) < 3:
            return "insufficient_data"
        first_half = self.turn_scores[:len(self.turn_scores) // 2]
        second_half = self.turn_scores[len(self.turn_scores) // 2:]
        first_mean = sum(first_half) / len(first_half)
        second_mean = sum(second_half) / len(second_half)
        delta = second_mean - first_mean
        if delta < -0.15:
            return "degrading"
        if delta > 0.10:
            return "improving"
        return "stable"

    def lowest_turn(self) -> Optional[int]:
        if not self.turn_scores:
            return None
        min_score = min(self.turn_scores)
        idx = self.turn_scores.index(min_score)
        return self.turn_indices[idx]
```

## Solution 4: Quality Trajectory Tracker

```python
from typing import Dict, List, Optional


class QualityTrajectoryTracker:
    """
    Maintains quality trajectories for all active and recent sessions.
    Identifies sessions with degrading quality for follow-up analysis.
    """

    def __init__(self):
        self._trajectories: Dict[str, QualityTrajectory] = {}

    def record(self, turn: ConversationTurn) -> None:
        if turn.session_id not in self._trajectories:
            self._trajectories[turn.session_id] = QualityTrajectory(
                session_id=turn.session_id
            )
        self._trajectories[turn.session_id].add(turn)

    def trajectory(self, session_id: str) -> Optional[QualityTrajectory]:
        return self._trajectories.get(session_id)

    def degrading_sessions(self, threshold: float = 0.40) -> List[str]:
        return [
            sid for sid, traj in self._trajectories.items()
            if traj.trend() == "degrading"
            or traj.degradation_turn(threshold) is not None
        ]

    def fleet_summary(self) -> dict:
        all_means = [
            traj.session_mean()
            for traj in self._trajectories.values()
            if traj.session_mean() is not None
        ]
        if not all_means:
            return {"sessions": 0}
        return {
            "sessions": len(all_means),
            "fleet_mean_quality": round(sum(all_means) / len(all_means), 4),
            "degrading_session_count": len(self.degrading_sessions()),
            "low_quality_sessions": sum(1 for m in all_means if m < 0.50),
        }
```

## Solution 5: Turn Quality Alert

```python
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional


@dataclass
class TurnQualityAlert:
    session_id: str
    turn_index: int
    quality_score: float
    alert_type: str   # "low_turn_score" | "degradation_detected" | "cliff_drop"
    message: str
    fired_at: float = field(default_factory=time.time)


class TurnQualityAlertManager:
    """
    Fires alerts when individual turn scores are below threshold
    or when trajectory shows a sudden cliff drop.
    """

    def __init__(
        self,
        tracker: QualityTrajectoryTracker,
        low_score_threshold: float = 0.40,
        cliff_drop: float = 0.30,
    ):
        self._tracker = tracker
        self._low_threshold = low_score_threshold
        self._cliff_drop = cliff_drop
        self._handlers: List[Callable[[TurnQualityAlert], None]] = []
        self._fired: List[TurnQualityAlert] = []

    def add_handler(self, fn: Callable[[TurnQualityAlert], None]) -> None:
        self._handlers.append(fn)

    def evaluate(self, turn: ConversationTurn) -> List[TurnQualityAlert]:
        alerts = []
        if not turn.is_scored():
            return alerts

        if turn.quality_score < self._low_threshold:
            alert = TurnQualityAlert(
                session_id=turn.session_id,
                turn_index=turn.turn_index,
                quality_score=turn.quality_score,
                alert_type="low_turn_score",
                message=f"Turn {turn.turn_index} score {turn.quality_score:.2f} below threshold {self._low_threshold}",
            )
            alerts.append(alert)

        traj = self._tracker.trajectory(turn.session_id)
        if traj and len(traj.turn_scores) >= 2:
            prev_score = traj.turn_scores[-2]
            drop = prev_score - turn.quality_score
            if drop >= self._cliff_drop:
                alert = TurnQualityAlert(
                    session_id=turn.session_id,
                    turn_index=turn.turn_index,
                    quality_score=turn.quality_score,
                    alert_type="cliff_drop",
                    message=f"Quality cliff: turn {turn.turn_index} dropped {drop:.2f} from previous turn",
                )
                alerts.append(alert)

        for alert in alerts:
            self._fired.append(alert)
            for h in self._handlers:
                try:
                    h(alert)
                except Exception:
                    pass

        return alerts
```

## Solution 6: Turn Quality Dashboard

```python
import time


class ConversationTurnQualityDashboard:
    """Combines fleet trajectory summary, degrading sessions, and recent alerts."""

    def __init__(
        self,
        scorer: TurnQualityScorer,
        tracker: QualityTrajectoryTracker,
        alert_manager: TurnQualityAlertManager,
    ):
        self._scorer = scorer
        self._tracker = tracker
        self._alert_manager = alert_manager

    def render(self) -> dict:
        fleet = self._tracker.fleet_summary()
        degrading = self._tracker.degrading_sessions()
        recent_alerts = [
            {
                "session_id": a.session_id,
                "turn_index": a.turn_index,
                "type": a.alert_type,
                "score": a.quality_score,
                "message": a.message,
            }
            for a in self._alert_manager._fired[-20:]
        ]

        return {
            "generated_at": time.time(),
            "fleet": fleet,
            "degrading_sessions": degrading[:10],
            "recent_alerts": recent_alerts,
            "dimension_weights": TurnQualityScorer.DIMENSION_WEIGHTS,
        }
```

## Comparison

| Approach | Per-Turn Scoring | Multi-Dimension | Trajectory Tracking | Degradation Detection | Alerts |
|---|---|---|---|---|---|
| TurnQualityScorer | Yes | Yes (5 dimensions) | No | No | No |
| QualityTrajectory | No | No | Yes (trend/cliff) | Yes | No |
| QualityTrajectoryTracker | Via trajectory | No | Yes (fleet) | Yes | No |
| TurnQualityAlertManager | Via tracker | No | Via tracker | Yes | Yes |
| ConversationTurnQualityDashboard | Via scorer | Via scorer | Via tracker | Via manager | Via manager |

**Best for production**: Score every turn synchronously before returning the response — structural scoring adds under 1ms. Use the `degradation_turn()` index to annotate session replays: when support investigates a bad session, they should jump directly to the turn where quality first dropped below 0.40 rather than reading from the beginning. Export `fleet_mean_quality` as a time-series metric — a decline over 24 hours often precedes a wave of support tickets by 6–12 hours, giving time to intervene. Treat `cliff_drop` alerts (sudden quality drop of 0.30 within a single turn) as high-priority: they usually indicate a specific prompt template failure or a tool returning unexpected data.
