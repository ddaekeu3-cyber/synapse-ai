---
title: "Agent Doesn't Implement Multi-Turn Conversation Health Metrics"
description: "Agents that track individual response quality miss conversation-level health signals: a conversation where every turn scores 7/10 in isolation can still be a failing session if the user is progressively rephrasing the same question (indicating the agent is not answering it) or if topic coherence decays over turns. Implement conversation-level health metrics that track inter-turn coherence, question resolution rates, user frustration signals, and conversation arc health."
date: 2026-04-16
difficulty: advanced
category: observability
slug: agent-doesnt-implement-multi-turn-conversation-health-metrics
tags: [conversation-health, multi-turn-metrics, coherence-tracking, question-resolution, frustration-detection, session-quality]
symptoms:
  - "Per-turn quality scores look healthy but users abandon conversations at turn 5"
  - "No signal for whether the agent answered the user's original question across multiple turns"
  - "Rephrasing loops are invisible — user asks the same thing 4 ways and all turns score well individually"
  - "Conversation coherence is not measured — topic drift is only noticed in post-session review"
  - "Cannot distinguish a healthy 10-turn conversation from a failing 10-turn loop"
---

## Why This Happens

Per-turn quality metrics treat each response independently. A conversation is a sequential structure where turn N depends on turns 1 through N-1, and health signals emerge from the pattern across turns — not from any single turn. A user who rephrases the same question is signaling that prior answers were insufficient, even if each individual answer was well-formed. Detecting this pattern requires tracking question similarity across turns, measuring whether each turn advances toward resolution or loops back, and aggregating these signals into a conversation-arc health score.

## Solution 1: Conversation Turn Metrics

```python
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class TurnSignal(str, Enum):
    RESOLVED = "resolved"           # user's question appeared to be answered
    REPHRASED = "rephrased"         # user asked the same thing differently
    ESCALATED = "escalated"         # user expressed frustration or urgency
    CLARIFIED = "clarified"         # user provided more context
    TOPIC_CHANGED = "topic_changed" # new unrelated topic introduced
    ABANDONED = "abandoned"         # user did not continue after this turn


@dataclass
class ConversationTurnMetrics:
    turn_index: int
    session_id: str
    user_message: str
    assistant_message: str
    turn_signal: Optional[TurnSignal] = None
    similarity_to_previous_user_msg: Optional[float] = None   # 0.0–1.0
    response_length_chars: int = 0
    user_message_length_chars: int = 0
    contains_question: bool = False     # user message ends with a question
    recorded_at: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        self.response_length_chars = len(self.assistant_message)
        self.user_message_length_chars = len(self.user_message)
        self.contains_question = "?" in self.user_message
```

## Solution 2: Rephrasing Loop Detector

```python
import re
from typing import List, Optional, Tuple


class RephrasingLoopDetector:
    """
    Detects when a user is repeatedly asking semantically similar questions —
    a sign that previous answers were unsatisfactory.
    Uses character n-gram Jaccard similarity as a lightweight proxy for semantic similarity.
    """

    def __init__(
        self,
        similarity_threshold: float = 0.55,
        ngram_size: int = 4,
        min_question_length: int = 15,
    ):
        self._threshold = similarity_threshold
        self._ngram_size = ngram_size
        self._min_len = min_question_length

    def _ngrams(self, text: str) -> set:
        text = re.sub(r"\s+", " ", text.lower().strip())
        return {text[i:i + self._ngram_size] for i in range(len(text) - self._ngram_size + 1)}

    def similarity(self, a: str, b: str) -> float:
        if len(a) < self._min_len or len(b) < self._min_len:
            return 0.0
        sa, sb = self._ngrams(a), self._ngrams(b)
        if not sa or not sb:
            return 0.0
        return round(len(sa & sb) / len(sa | sb), 4)

    def detect_rephrase(
        self,
        current_msg: str,
        previous_msgs: List[str],
        window: int = 3,
    ) -> Tuple[bool, Optional[float]]:
        """Returns (is_rephrase, max_similarity)."""
        max_sim = 0.0
        for prev in previous_msgs[-window:]:
            sim = self.similarity(current_msg, prev)
            if sim > max_sim:
                max_sim = sim
        is_rephrase = max_sim >= self._threshold
        return is_rephrase, max_sim if is_rephrase else None
```

## Solution 3: Conversation Arc Analyzer

```python
from typing import List, Optional


class ConversationArcAnalyzer:
    """
    Analyzes the arc of a conversation across all turns.
    Computes: resolution rate, rephrase rate, frustration signal count,
    topic coherence, and an overall arc health score (0.0–1.0).
    """

    FRUSTRATION_PATTERNS = [
        r"(?:you (?:already|just) said|that(?:'s| is) not what I|still not|doesn't answer|wrong|incorrect)",
        r"(?:I (?:already|just) told you|as I (?:said|mentioned)|again,)",
        r"(?:this (?:is|isn't) (?:helpful|what I need)|not helpful|please re-?read)",
    ]

    def __init__(self, rephrase_detector: RephrasingLoopDetector):
        self._detector = rephrase_detector
        import re
        self._frustration_re = [re.compile(p, re.I) for p in self.FRUSTRATION_PATTERNS]

    def _has_frustration(self, text: str) -> bool:
        return any(p.search(text) for p in self._frustration_re)

    def analyze(self, turns: List[ConversationTurnMetrics]) -> dict:
        if not turns:
            return {"turn_count": 0, "arc_health": 1.0}

        user_msgs = [t.user_message for t in turns]
        rephrase_count = 0
        frustration_count = 0
        resolution_count = 0

        for i, turn in enumerate(turns):
            if i > 0:
                is_rephrase, sim = self._detector.detect_rephrase(
                    turn.user_message, user_msgs[:i]
                )
                if is_rephrase:
                    rephrase_count += 1
                    turn.similarity_to_previous_user_msg = sim
                    turn.turn_signal = TurnSignal.REPHRASED

            if self._has_frustration(turn.user_message):
                frustration_count += 1
                turn.turn_signal = TurnSignal.ESCALATED

            if turn.turn_signal == TurnSignal.RESOLVED:
                resolution_count += 1

        n = len(turns)
        rephrase_rate = rephrase_count / max(n - 1, 1)
        frustration_rate = frustration_count / max(n, 1)

        # Health score: penalize rephrase and frustration signals
        health = max(0.0, 1.0 - rephrase_rate * 0.5 - frustration_rate * 0.7)

        return {
            "turn_count": n,
            "rephrase_count": rephrase_count,
            "rephrase_rate": round(rephrase_rate, 4),
            "frustration_count": frustration_count,
            "frustration_rate": round(frustration_rate, 4),
            "resolution_count": resolution_count,
            "arc_health": round(health, 4),
            "arc_label": (
                "healthy" if health >= 0.7
                else "degraded" if health >= 0.4
                else "failing"
            ),
        }
```

## Solution 4: Conversation Health Aggregator

```python
import threading
import time
from collections import deque
from typing import Deque, Dict, List, Optional


class ConversationHealthAggregator:
    """
    Accumulates conversation arc analyses across sessions.
    Tracks fleet-wide conversation health metrics:
    percentage of failing conversations, average arc health,
    and most common failure patterns.
    """

    def __init__(self, window_seconds: float = 86400.0):
        self._window = window_seconds
        self._events: Deque[dict] = deque()
        self._lock = threading.Lock()

    def record(self, session_id: str, arc_analysis: dict) -> None:
        with self._lock:
            self._events.append({
                "ts": time.time(),
                "session_id": session_id,
                **arc_analysis,
            })
            self._trim()

    def _trim(self) -> None:
        cutoff = time.time() - self._window
        while self._events and self._events[0]["ts"] < cutoff:
            self._events.popleft()

    def fleet_stats(self) -> dict:
        with self._lock:
            events = list(self._events)

        if not events:
            return {"conversations": 0}

        total = len(events)
        failing = sum(1 for e in events if e.get("arc_label") == "failing")
        degraded = sum(1 for e in events if e.get("arc_label") == "degraded")
        avg_health = sum(e.get("arc_health", 1.0) for e in events) / total
        avg_rephrase_rate = sum(e.get("rephrase_rate", 0) for e in events) / total
        avg_frustration = sum(e.get("frustration_count", 0) for e in events) / total

        return {
            "conversations": total,
            "failing_pct": round(failing / total * 100, 1),
            "degraded_pct": round(degraded / total * 100, 1),
            "avg_arc_health": round(avg_health, 4),
            "avg_rephrase_rate": round(avg_rephrase_rate, 4),
            "avg_frustration_turns": round(avg_frustration, 2),
        }
```

## Solution 5: Conversation Health Alert Manager

```python
import time
from typing import Callable, List


class ConversationHealthAlertManager:
    """
    Fires alerts when fleet-wide conversation health degrades:
    - Failing conversation rate exceeds threshold
    - Average arc health drops below threshold
    - Rephrase rate spikes (suggesting systematic answer quality issues)
    """

    def __init__(
        self,
        aggregator: ConversationHealthAggregator,
        max_failing_pct: float = 15.0,
        min_avg_health: float = 0.65,
        max_rephrase_rate: float = 0.20,
        cooldown_seconds: float = 1800.0,
    ):
        self._aggregator = aggregator
        self._max_failing = max_failing_pct
        self._min_health = min_avg_health
        self._max_rephrase = max_rephrase_rate
        self._cooldown = cooldown_seconds
        self._last_fired: dict = {}
        self._handlers: List[Callable[[dict], None]] = []

    def add_handler(self, fn: Callable[[dict], None]) -> None:
        self._handlers.append(fn)

    def _can_fire(self, key: str) -> bool:
        last = self._last_fired.get(key, 0)
        if time.time() - last >= self._cooldown:
            self._last_fired[key] = time.time()
            return True
        return False

    def _fire(self, alert: dict) -> None:
        for h in self._handlers:
            try:
                h(alert)
            except Exception:
                pass

    def check(self) -> List[dict]:
        stats = self._aggregator.fleet_stats()
        if stats.get("conversations", 0) < 20:
            return []

        alerts = []
        if stats["failing_pct"] > self._max_failing and self._can_fire("failing_rate"):
            alert = {
                "type": "high_failing_conversation_rate",
                "severity": "critical",
                "failing_pct": stats["failing_pct"],
                "threshold": self._max_failing,
                "message": f"{stats['failing_pct']:.1f}% of conversations are failing (threshold {self._max_failing}%)",
            }
            alerts.append(alert)
            self._fire(alert)

        if stats["avg_arc_health"] < self._min_health and self._can_fire("low_health"):
            alert = {
                "type": "low_average_arc_health",
                "severity": "warning",
                "avg_health": stats["avg_arc_health"],
                "threshold": self._min_health,
                "message": f"Average conversation arc health {stats['avg_arc_health']:.2f} below threshold {self._min_health}",
            }
            alerts.append(alert)
            self._fire(alert)

        if stats["avg_rephrase_rate"] > self._max_rephrase and self._can_fire("rephrase_spike"):
            alert = {
                "type": "rephrase_rate_spike",
                "severity": "warning",
                "rephrase_rate": stats["avg_rephrase_rate"],
                "threshold": self._max_rephrase,
                "message": f"Users are rephrasing questions at {stats['avg_rephrase_rate']:.1%} rate — agent may be failing to answer core questions",
            }
            alerts.append(alert)
            self._fire(alert)

        return alerts
```

## Solution 6: Conversation Health Dashboard

```python
import time


class ConversationHealthDashboard:
    """Combines arc analyzer, fleet stats, and alert signals."""

    def __init__(
        self,
        aggregator: ConversationHealthAggregator,
        alert_manager: ConversationHealthAlertManager,
    ):
        self._aggregator = aggregator
        self._alerts = alert_manager

    def render(self) -> dict:
        fleet_stats = self._aggregator.fleet_stats()
        alerts = self._alerts.check()
        return {
            "generated_at": time.time(),
            "fleet_stats": fleet_stats,
            "alerts": alerts,
            "healthy": len(alerts) == 0,
        }
```

## Comparison

| Approach | Rephrase Detection | Frustration Detection | Arc Health Score | Fleet Aggregation | Alerts |
|---|---|---|---|---|---|
| RephrasingLoopDetector | Yes (Jaccard n-gram) | No | No | No | No |
| ConversationArcAnalyzer | Via detector | Yes (regex) | Yes | No | No |
| ConversationHealthAggregator | No | No | No | Yes | No |
| ConversationHealthAlertManager | No | No | No | Via aggregator | Yes |
| ConversationHealthDashboard | No | No | No | No | Yes |

**Best for production**: Run `ConversationArcAnalyzer.analyze()` at session end (when the user closes the conversation or after a 30-minute inactivity timeout). Record the arc analysis in `ConversationHealthAggregator` to build fleet-wide baselines. Set `max_failing_pct=15` as the alert threshold — a failure rate above 15% indicates a systematic issue with answer quality, not individual session variance. The rephrase rate is the single most actionable metric: a fleet-wide rephrase rate above 20% almost always points to a specific category of questions the agent consistently fails to answer, which can be diagnosed by clustering the rephrased queries.
