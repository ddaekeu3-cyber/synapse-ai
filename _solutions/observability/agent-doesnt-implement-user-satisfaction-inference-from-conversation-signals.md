---
title: "Agent Doesn't Implement User Satisfaction Inference from Conversation Signals"
description: "Agents that collect no implicit satisfaction signals cannot distinguish conversations that went well from conversations that went poorly without explicit ratings. Implicit signals — follow-up clarification requests, conversation abandonment, repeated rephrasing, positive acknowledgments — are observable without asking the user to rate anything. Implement satisfaction inference that aggregates these signals into a per-session score and surfaces fleet-level satisfaction trends."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-user-satisfaction-inference-from-conversation-signals
tags: [user-satisfaction, implicit-signals, conversation-quality, sentiment-inference, abandonment-detection, ux-observability]
symptoms:
  - "No visibility into whether users are satisfied without explicit star ratings"
  - "Cannot distinguish sessions that resolved the user's need from sessions that failed silently"
  - "Repeated rephrasing from users goes undetected and uncounted"
  - "Conversation abandonment is not tracked as a negative signal"
  - "No per-feature or per-tool satisfaction breakdown"
---

## Why This Happens

Explicit rating prompts have low completion rates and introduce friction. Implicit signals are present in every conversation: a user who says "thanks, that's exactly what I needed" is satisfied; a user who sends five rephrased versions of the same question is not; a user who stops responding mid-task has abandoned the session. Without instrumenting these signals, the agent has no feedback loop. Satisfaction inference combines lexical sentiment detection, behavioral signals (rephrase count, turn depth, abandonment), and acknowledgment patterns into a score that proxies explicit ratings without requiring them.

## Solution 1: Conversation Signal Classifier

```python
import re
from dataclasses import dataclass
from enum import Enum
from typing import List, Optional


class SignalType(str, Enum):
    POSITIVE_ACK = "positive_ack"           # "thanks", "perfect", "exactly"
    NEGATIVE_SENTIMENT = "negative_sentiment"  # "wrong", "not what I meant", "useless"
    REPHRASE = "rephrase"                   # user restates the same question
    CLARIFICATION_REQUEST = "clarification_request"  # "what do you mean by..."
    ABANDONMENT = "abandonment"             # session ends without resolution
    TASK_COMPLETION = "task_completion"     # "done", "that worked", "solved"
    ESCALATION = "escalation"              # "let me talk to a human"


@dataclass
class ConversationSignal:
    signal_type: SignalType
    turn_index: int
    confidence: float           # 0.0–1.0
    source_text: str = ""


_POSITIVE_PATTERNS = re.compile(
    r"\b(thanks|thank you|perfect|exactly|great|awesome|helpful|that works|solved|got it|yes that'?s? it)\b",
    re.IGNORECASE,
)
_NEGATIVE_PATTERNS = re.compile(
    r"\b(wrong|incorrect|not what i (wanted|meant|asked)|useless|that'?s? not|try again|no that'?s? wrong)\b",
    re.IGNORECASE,
)
_CLARIFICATION_PATTERNS = re.compile(
    r"\b(what do you mean|can you clarify|i don'?t understand|confused|unclear|elaborate)\b",
    re.IGNORECASE,
)
_COMPLETION_PATTERNS = re.compile(
    r"\b(that worked|it works|done|fixed|resolved|all good|problem solved)\b",
    re.IGNORECASE,
)
_ESCALATION_PATTERNS = re.compile(
    r"\b(human|agent|supervisor|real person|escalate|speak to someone)\b",
    re.IGNORECASE,
)


class ConversationSignalClassifier:
    """
    Classifies user messages into satisfaction-relevant signal types
    using regex heuristics. No ML required — fast and interpretable.
    """

    def classify(self, text: str, turn_index: int) -> List[ConversationSignal]:
        signals = []
        if _POSITIVE_PATTERNS.search(text):
            signals.append(ConversationSignal(
                signal_type=SignalType.POSITIVE_ACK,
                turn_index=turn_index,
                confidence=0.85,
                source_text=text[:120],
            ))
        if _NEGATIVE_PATTERNS.search(text):
            signals.append(ConversationSignal(
                signal_type=SignalType.NEGATIVE_SENTIMENT,
                turn_index=turn_index,
                confidence=0.80,
                source_text=text[:120],
            ))
        if _CLARIFICATION_PATTERNS.search(text):
            signals.append(ConversationSignal(
                signal_type=SignalType.CLARIFICATION_REQUEST,
                turn_index=turn_index,
                confidence=0.75,
                source_text=text[:120],
            ))
        if _COMPLETION_PATTERNS.search(text):
            signals.append(ConversationSignal(
                signal_type=SignalType.TASK_COMPLETION,
                turn_index=turn_index,
                confidence=0.90,
                source_text=text[:120],
            ))
        if _ESCALATION_PATTERNS.search(text):
            signals.append(ConversationSignal(
                signal_type=SignalType.ESCALATION,
                turn_index=turn_index,
                confidence=0.90,
                source_text=text[:120],
            ))
        return signals
```

## Solution 2: Rephrase Detector

```python
import re
from typing import List, Optional, Tuple


class RephraseDetector:
    """
    Detects when a user is rephrasing a previous question.
    Uses Jaccard similarity on word-level bigrams to compare
    successive user messages. High similarity with minor wording
    changes indicates a rephrase attempt.
    """

    def __init__(self, similarity_threshold: float = 0.55):
        self._threshold = similarity_threshold
        self._user_messages: List[str] = []

    def _normalize(self, text: str) -> str:
        return re.sub(r"[^\w\s]", "", text.lower().strip())

    def _bigrams(self, text: str) -> set:
        words = self._normalize(text).split()
        if len(words) < 2:
            return set(words)
        return {f"{words[i]} {words[i+1]}" for i in range(len(words) - 1)}

    def _jaccard(self, a: set, b: set) -> float:
        if not a and not b:
            return 1.0
        union = len(a | b)
        return len(a & b) / union if union else 0.0

    def observe(self, user_message: str, turn_index: int) -> Optional[ConversationSignal]:
        current_bigrams = self._bigrams(user_message)
        for prev_msg in self._user_messages[-3:]:   # look back 3 turns
            prev_bigrams = self._bigrams(prev_msg)
            sim = self._jaccard(current_bigrams, prev_bigrams)
            if sim >= self._threshold and self._normalize(user_message) != self._normalize(prev_msg):
                self._user_messages.append(user_message)
                return ConversationSignal(
                    signal_type=SignalType.REPHRASE,
                    turn_index=turn_index,
                    confidence=round(sim, 3),
                    source_text=user_message[:120],
                )
        self._user_messages.append(user_message)
        return None
```

## Solution 3: Session Satisfaction Scorer

```python
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class SessionSatisfactionState:
    session_id: str
    started_at: float = field(default_factory=time.time)
    signals: List[ConversationSignal] = field(default_factory=list)
    turn_count: int = 0
    last_user_message_at: float = field(default_factory=time.time)
    abandoned: bool = False
    score: Optional[float] = None


_SIGNAL_WEIGHTS: Dict[SignalType, float] = {
    SignalType.POSITIVE_ACK: +0.25,
    SignalType.TASK_COMPLETION: +0.40,
    SignalType.NEGATIVE_SENTIMENT: -0.30,
    SignalType.REPHRASE: -0.15,
    SignalType.CLARIFICATION_REQUEST: -0.10,
    SignalType.ESCALATION: -0.50,
    SignalType.ABANDONMENT: -0.35,
}


class SessionSatisfactionScorer:
    """
    Aggregates conversation signals into a satisfaction score in [0.0, 1.0].
    Starts at a neutral 0.6 and adjusts based on weighted signal contributions.
    Clamps to [0.0, 1.0].
    """

    BASELINE = 0.6

    def score(self, state: SessionSatisfactionState) -> float:
        adjustment = 0.0
        for signal in state.signals:
            weight = _SIGNAL_WEIGHTS.get(signal.signal_type, 0.0)
            adjustment += weight * signal.confidence

        # Long conversations without resolution are a mild negative
        if state.turn_count > 10:
            adjustment -= 0.05 * (state.turn_count - 10) * 0.01

        raw = self.BASELINE + adjustment
        return round(max(0.0, min(1.0, raw)), 4)

    def label(self, score: float) -> str:
        if score >= 0.75:
            return "satisfied"
        if score >= 0.50:
            return "neutral"
        if score >= 0.30:
            return "frustrated"
        return "dissatisfied"
```

## Solution 4: Abandonment Detector

```python
import time
from typing import Optional


class AbandonmentDetector:
    """
    Declares a session abandoned if the user has not sent a message within
    a configurable idle window AND the last agent turn did not receive a
    completion or positive acknowledgment signal.
    """

    def __init__(self, idle_threshold_seconds: float = 300.0):
        self._idle_threshold = idle_threshold_seconds

    def check(
        self,
        state: SessionSatisfactionState,
        now: Optional[float] = None,
    ) -> bool:
        now = now or time.time()
        idle_seconds = now - state.last_user_message_at

        if idle_seconds < self._idle_threshold:
            return False

        # Do not flag as abandoned if session ended with positive signal
        positive_types = {SignalType.POSITIVE_ACK, SignalType.TASK_COMPLETION}
        recent_positive = any(
            s.signal_type in positive_types
            for s in state.signals[-3:]
        )
        if recent_positive:
            return False

        return True

    def mark_abandoned(self, state: SessionSatisfactionState, turn_index: int) -> None:
        state.abandoned = True
        state.signals.append(ConversationSignal(
            signal_type=SignalType.ABANDONMENT,
            turn_index=turn_index,
            confidence=0.70,
        ))
```

## Solution 5: Satisfaction Inference Pipeline

```python
import time
from typing import Dict, Optional


class SatisfactionInferencePipeline:
    """
    Coordinates signal classification, rephrase detection, abandonment
    detection, and scoring for a single session. Call observe() on each
    user message and finalize() when the session ends.
    """

    def __init__(
        self,
        classifier: ConversationSignalClassifier,
        rephrase_detector: RephraseDetector,
        abandonment_detector: AbandonmentDetector,
        scorer: SessionSatisfactionScorer,
    ):
        self._classifier = classifier
        self._rephrase = rephrase_detector
        self._abandonment = abandonment_detector
        self._scorer = scorer

    def observe(
        self,
        session_state: SessionSatisfactionState,
        user_message: str,
    ) -> List[ConversationSignal]:
        session_state.turn_count += 1
        session_state.last_user_message_at = time.time()
        turn_idx = session_state.turn_count

        new_signals = self._classifier.classify(user_message, turn_idx)

        rephrase = self._rephrase.observe(user_message, turn_idx)
        if rephrase:
            new_signals.append(rephrase)

        session_state.signals.extend(new_signals)
        session_state.score = self._scorer.score(session_state)
        return new_signals

    def finalize(
        self,
        session_state: SessionSatisfactionState,
    ) -> dict:
        if self._abandonment.check(session_state):
            self._abandonment.mark_abandoned(session_state, session_state.turn_count)

        final_score = self._scorer.score(session_state)
        session_state.score = final_score

        signal_counts: Dict[str, int] = {}
        for s in session_state.signals:
            signal_counts[s.signal_type.value] = signal_counts.get(s.signal_type.value, 0) + 1

        return {
            "session_id": session_state.session_id,
            "score": final_score,
            "label": self._scorer.label(final_score),
            "turn_count": session_state.turn_count,
            "abandoned": session_state.abandoned,
            "signal_counts": signal_counts,
            "duration_seconds": round(time.time() - session_state.started_at, 1),
        }
```

## Solution 6: Fleet Satisfaction Aggregator

```python
import time
from collections import deque
from threading import Lock
from typing import Deque, List, Optional, Tuple


class FleetSatisfactionAggregator:
    """
    Accumulates finalized session satisfaction reports across the fleet.
    Provides rolling window percentile and label distribution queries
    for dashboard and alerting use.
    """

    def __init__(self, max_records: int = 50000):
        self._max = max_records
        self._records: Deque[Tuple[float, dict]] = deque()
        self._lock = Lock()

    def record(self, finalized_report: dict) -> None:
        with self._lock:
            self._records.append((time.time(), finalized_report))
            if len(self._records) > self._max:
                self._records.popleft()

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        with self._lock:
            recent = [r for ts, r in self._records if ts >= cutoff]

        if not recent:
            return {"window_seconds": window_seconds, "sessions": 0}

        scores = sorted(r["score"] for r in recent)
        label_counts: dict = {}
        for r in recent:
            lbl = r.get("label", "unknown")
            label_counts[lbl] = label_counts.get(lbl, 0) + 1

        abandonment_rate = sum(1 for r in recent if r.get("abandoned")) / len(recent)

        def pct(p: float) -> float:
            idx = min(int(len(scores) * p), len(scores) - 1)
            return round(scores[idx], 4)

        return {
            "window_seconds": window_seconds,
            "sessions": len(recent),
            "score_p25": pct(0.25),
            "score_p50": pct(0.50),
            "score_p75": pct(0.75),
            "score_mean": round(sum(scores) / len(scores), 4),
            "label_distribution": label_counts,
            "abandonment_rate": round(abandonment_rate, 4),
            "satisfied_rate": round(label_counts.get("satisfied", 0) / len(recent), 4),
        }

    def alert_if_degraded(
        self,
        window_seconds: float = 1800.0,
        satisfied_rate_threshold: float = 0.55,
    ) -> Optional[dict]:
        s = self.summary(window_seconds)
        if s["sessions"] < 10:
            return None
        if s["satisfied_rate"] < satisfied_rate_threshold:
            return {
                "alert": "satisfaction_degraded",
                "satisfied_rate": s["satisfied_rate"],
                "threshold": satisfied_rate_threshold,
                "sessions_in_window": s["sessions"],
            }
        return None
```

## Comparison

| Approach | Lexical Signals | Rephrase Detection | Abandonment | Session Score | Fleet Aggregation |
|---|---|---|---|---|---|
| ConversationSignalClassifier | Yes (regex) | No | No | No | No |
| RephraseDetector | No | Yes (Jaccard bigrams) | No | No | No |
| SessionSatisfactionScorer | Via signals | Via signals | Via signals | Yes | No |
| AbandonmentDetector | No | No | Yes (idle timeout) | No | No |
| SatisfactionInferencePipeline | Via classifier | Via detector | Via detector | Via scorer | No |
| FleetSatisfactionAggregator | No | No | No | No | Yes (P25/P50/P75) |

**Best for production**: Call `SatisfactionInferencePipeline.observe()` on every user message turn — it is regex-only and adds no latency to the critical path. Set abandonment idle threshold to 5 minutes for synchronous assistants; lower to 90 seconds for real-time voice or chat interfaces. Alert via `FleetSatisfactionAggregator.alert_if_degraded()` when `satisfied_rate` drops below 0.55 over a 30-minute rolling window — a degradation at this scale almost always indicates a broken tool, a prompt regression, or a provider outage rather than individual user variance. Use `signal_counts` from finalized reports to identify which signal types correlate most strongly with low scores in your specific domain: for code assistants, rephrase count is the strongest predictor; for Q&A bots, escalation rate is.
