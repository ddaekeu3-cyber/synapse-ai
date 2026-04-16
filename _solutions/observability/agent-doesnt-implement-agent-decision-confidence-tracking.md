---
title: "Agent Doesn't Implement Agent Decision Confidence Tracking"
description: "Agents that make decisions without recording confidence signals have no way to detect when they are operating in low-confidence territory — ambiguous inputs, out-of-distribution queries, or conflicting tool results. Implement decision confidence tracking to capture per-decision confidence scores, detect confidence degradation over a session, and surface low-confidence decisions for human review or escalation."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-agent-decision-confidence-tracking
tags: [confidence-tracking, decision-quality, uncertainty, escalation, observability, llm-calibration]
symptoms:
  - "Agent produces confident-sounding outputs on queries where it is actually uncertain"
  - "No signal to distinguish high-quality decisions from low-quality guesses in logs"
  - "Confidence degrades over a long session but there is no way to detect or respond to this"
  - "Human review queue has no way to prioritize which agent decisions most need checking"
  - "Low-confidence decisions on critical operations go through without escalation"
---

## Why This Happens

LLMs express confidence implicitly through language hedging and token probabilities, but agents rarely capture these signals explicitly. Without explicit confidence tracking, all decisions look equally authoritative in logs — a well-grounded retrieval-backed answer looks the same as a hallucinated guess. Confidence tracking instruments the decision layer: each significant decision records a confidence score (from logprobs, ensemble agreement, retrieval score, or heuristic signals), enabling confidence-based routing, escalation triggers, and session-level degradation detection.

## Solution 1: Decision Confidence Record

```python
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

@dataclass
class ConfidenceSignal:
    signal_type: str    # "logprob" | "retrieval_score" | "ensemble_agreement" | "heuristic"
    value: float        # 0.0–1.0
    weight: float = 1.0
    metadata: Dict[str, Any] = field(default_factory=dict)

@dataclass
class DecisionConfidenceRecord:
    decision_id: str = field(default_factory=lambda: str(uuid.uuid4())[:10])
    session_id: str = ""
    agent_id: str = ""
    decision_type: str = ""   # "tool_selection" | "answer_generation" | "routing" | "classification"
    decision_value: str = ""  # what was decided (truncated for logging)

    # Confidence signals contributing to the composite score
    signals: List[ConfidenceSignal] = field(default_factory=list)
    composite_score: float = 0.0   # weighted average of signals

    # Context
    input_tokens: int = 0
    retrieval_score: Optional[float] = None    # top retrieval match score
    tool_agreement_rate: Optional[float] = None  # fraction of tools agreeing
    hedge_words_detected: bool = False          # "I think", "probably", "maybe"

    timestamp: float = field(default_factory=time.time)
    flagged_for_review: bool = False
    review_reason: str = ""

    def compute_composite(self) -> float:
        if not self.signals:
            return 0.5  # no signals = unknown confidence
        total_weight = sum(s.weight for s in self.signals)
        if total_weight == 0:
            return 0.5
        self.composite_score = sum(s.value * s.weight for s in self.signals) / total_weight
        return self.composite_score
```

## Solution 2: Confidence Extractor

```python
import re
from typing import Any, Dict, List, Optional

HEDGE_PATTERNS = re.compile(
    r"\b(I think|I believe|I'm not sure|probably|possibly|might be|could be|"
    r"perhaps|not certain|unclear|uncertain|approximately|roughly|around|"
    r"I'm unsure|I don't know|it seems|it appears|may be)\b",
    re.I,
)

class ConfidenceExtractor:
    """
    Extracts confidence signals from various sources:
    - LLM token log-probabilities (if available from API)
    - Retrieval match scores from vector search
    - Linguistic hedge detection in generated text
    - Ensemble agreement across multiple model calls
    """

    def from_logprobs(
        self,
        logprobs: List[float],
        method: str = "mean_top_k",
        k: int = 10,
    ) -> ConfidenceSignal:
        import math
        if not logprobs:
            return ConfidenceSignal(signal_type="logprob", value=0.5, weight=1.5)
        top_k = sorted(logprobs, reverse=True)[:k]
        # Convert log probs to probabilities and average
        probs = [min(1.0, math.exp(lp)) for lp in top_k]
        avg_prob = sum(probs) / len(probs)
        return ConfidenceSignal(
            signal_type="logprob",
            value=round(avg_prob, 4),
            weight=2.0,
            metadata={"method": method, "k": k},
        )

    def from_retrieval(
        self,
        scores: List[float],
        top_k: int = 3,
    ) -> ConfidenceSignal:
        if not scores:
            return ConfidenceSignal(signal_type="retrieval_score", value=0.3, weight=1.0)
        top = sorted(scores, reverse=True)[:top_k]
        avg = sum(top) / len(top)
        # Scores typically 0–1 (cosine similarity) or need normalization
        return ConfidenceSignal(
            signal_type="retrieval_score",
            value=round(min(1.0, max(0.0, avg)), 4),
            weight=1.5,
            metadata={"top_k_scores": top},
        )

    def from_hedge_detection(self, text: str) -> ConfidenceSignal:
        matches = HEDGE_PATTERNS.findall(text)
        hedge_density = len(matches) / max(len(text.split()), 1)
        # High hedge density = low confidence
        confidence = max(0.1, 1.0 - min(hedge_density * 10, 0.9))
        return ConfidenceSignal(
            signal_type="heuristic",
            value=round(confidence, 4),
            weight=0.5,
            metadata={"hedge_count": len(matches), "hedge_words": matches[:5]},
        )

    def from_ensemble(
        self,
        responses: List[str],
        similarity_fn=None,
    ) -> ConfidenceSignal:
        """Agreement rate across ensemble responses as a confidence proxy."""
        if len(responses) < 2:
            return ConfidenceSignal(signal_type="ensemble_agreement", value=0.5, weight=1.0)
        # Simple: check if responses share the same first sentence
        first_sentences = [r.split(".")[0].strip().lower() for r in responses]
        unique = len(set(first_sentences))
        agreement = 1.0 - (unique - 1) / len(first_sentences)
        return ConfidenceSignal(
            signal_type="ensemble_agreement",
            value=round(max(0.0, agreement), 4),
            weight=1.8,
        )
```

## Solution 3: Session Confidence Tracker

```python
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Deque, Dict, List, Optional

@dataclass
class SessionConfidenceSummary:
    session_id: str
    decision_count: int
    avg_confidence: float
    min_confidence: float
    low_confidence_count: int   # decisions below threshold
    trend: str                  # "stable" | "degrading" | "improving"
    flagged_decisions: List[str]

class SessionConfidenceTracker:
    """
    Tracks per-session decision confidence over time.
    Detects confidence degradation: a session where confidence is trending
    downward may indicate the agent is operating outside its knowledge boundary.
    Fires alerts when session-level confidence drops below thresholds.
    """

    def __init__(
        self,
        low_confidence_threshold: float = 0.4,
        degradation_window: int = 5,
        alert_handlers: List[Callable] = None,
    ):
        self._threshold = low_confidence_threshold
        self._window = degradation_window
        self._sessions: Dict[str, Deque[DecisionConfidenceRecord]] = {}
        self._alert_handlers = alert_handlers or []

    def record(self, record: DecisionConfidenceRecord) -> None:
        sid = record.session_id
        if sid not in self._sessions:
            self._sessions[sid] = deque(maxlen=200)
        self._sessions[sid].append(record)
        self._check_alerts(sid)

    def _check_alerts(self, session_id: str) -> None:
        decisions = list(self._sessions[session_id])
        if len(decisions) < self._window:
            return
        recent = decisions[-self._window:]
        avg_recent = sum(d.composite_score for d in recent) / len(recent)
        if avg_recent < self._threshold:
            for handler in self._alert_handlers:
                try:
                    handler("low_confidence_session", session_id, avg_recent)
                except Exception:
                    pass

    def session_summary(self, session_id: str) -> Optional[SessionConfidenceSummary]:
        decisions = list(self._sessions.get(session_id, []))
        if not decisions:
            return None
        scores = [d.composite_score for d in decisions]
        avg = sum(scores) / len(scores)

        # Trend: compare first half to second half
        mid = len(scores) // 2
        first_half_avg = sum(scores[:mid]) / max(mid, 1)
        second_half_avg = sum(scores[mid:]) / max(len(scores) - mid, 1)
        if second_half_avg < first_half_avg - 0.1:
            trend = "degrading"
        elif second_half_avg > first_half_avg + 0.1:
            trend = "improving"
        else:
            trend = "stable"

        return SessionConfidenceSummary(
            session_id=session_id,
            decision_count=len(decisions),
            avg_confidence=round(avg, 4),
            min_confidence=round(min(scores), 4),
            low_confidence_count=sum(1 for s in scores if s < self._threshold),
            trend=trend,
            flagged_decisions=[d.decision_id for d in decisions if d.flagged_for_review],
        )
```

## Solution 4: Confidence-Based Escalation Router

```python
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

@dataclass
class EscalationPolicy:
    name: str
    decision_types: List[str]    # which decision types this policy applies to
    confidence_threshold: float
    action: str                  # "flag_for_review" | "require_human" | "fallback_model" | "alert"
    priority: int = 1            # higher = checked first

class ConfidenceEscalationRouter:
    """
    Routes low-confidence decisions to appropriate escalation actions.
    Policies are matched by decision_type and ordered by priority.
    Enables automatic escalation to human review queues for critical low-confidence decisions.
    """

    def __init__(self):
        self._policies: List[EscalationPolicy] = []
        self._action_handlers: Dict[str, Callable] = {}
        self._escalated_count = 0

    def register_policy(self, policy: EscalationPolicy) -> None:
        self._policies.append(policy)
        self._policies.sort(key=lambda p: p.priority, reverse=True)

    def register_action(self, action_name: str, handler: Callable) -> None:
        self._action_handlers[action_name] = handler

    def evaluate(self, record: DecisionConfidenceRecord) -> Optional[str]:
        """
        Evaluates a decision against policies.
        Returns the action taken, or None if no escalation was needed.
        """
        for policy in self._policies:
            if record.decision_type not in policy.decision_types:
                continue
            if record.composite_score >= policy.confidence_threshold:
                continue

            # This policy matches
            record.flagged_for_review = True
            record.review_reason = (
                f"confidence {record.composite_score:.3f} < "
                f"threshold {policy.confidence_threshold} for policy '{policy.name}'"
            )
            self._escalated_count += 1

            handler = self._action_handlers.get(policy.action)
            if handler:
                try:
                    handler(record, policy)
                except Exception as exc:
                    print(f"[escalation] handler error: {exc}")

            return policy.action

        return None

    def stats(self) -> dict:
        return {
            "registered_policies": len(self._policies),
            "total_escalations": self._escalated_count,
        }
```

## Solution 5: Confidence Calibration Monitor

```python
import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, Dict, List, Optional, Tuple

@dataclass
class CalibrationSample:
    predicted_confidence: float
    actual_correct: bool   # whether the decision turned out to be correct
    decision_type: str
    timestamp: float

class ConfidenceCalibrationMonitor:
    """
    Measures whether the agent's confidence scores are calibrated:
    decisions with 0.8 confidence should be correct ~80% of the time.
    Detects overconfidence (actual accuracy < predicted) and underconfidence.
    Requires ground truth feedback to be wired in post-decision.
    """

    def __init__(self, n_buckets: int = 10):
        self._n = n_buckets
        self._samples: Deque[CalibrationSample] = deque(maxlen=10_000)

    def record_outcome(
        self,
        predicted_confidence: float,
        actual_correct: bool,
        decision_type: str = "",
    ) -> None:
        self._samples.append(CalibrationSample(
            predicted_confidence=predicted_confidence,
            actual_correct=actual_correct,
            decision_type=decision_type,
            timestamp=time.time(),
        ))

    def calibration_curve(self) -> List[Dict]:
        """Returns expected vs actual accuracy per confidence bucket."""
        buckets: Dict[int, List[bool]] = {i: [] for i in range(self._n)}
        for s in self._samples:
            bucket = min(int(s.predicted_confidence * self._n), self._n - 1)
            buckets[bucket].append(s.actual_correct)
        result = []
        for i, outcomes in buckets.items():
            if not outcomes:
                continue
            predicted = (i + 0.5) / self._n
            actual = sum(outcomes) / len(outcomes)
            result.append({
                "confidence_bucket": round(predicted, 2),
                "actual_accuracy": round(actual, 4),
                "sample_count": len(outcomes),
                "calibration_error": round(abs(predicted - actual), 4),
            })
        return result

    def expected_calibration_error(self) -> float:
        """ECE: weighted average calibration error across buckets."""
        curve = self.calibration_curve()
        if not curve:
            return 0.0
        total = sum(b["sample_count"] for b in curve)
        ece = sum(b["calibration_error"] * b["sample_count"] / total for b in curve)
        return round(ece, 4)
```

## Solution 6: Confidence Dashboard

```python
import time
from typing import Dict, List, Optional

class ConfidenceDashboard:
    """
    Aggregates confidence metrics across sessions for real-time monitoring.
    Surfaces low-confidence sessions, calibration drift, and escalation rates.
    """

    def __init__(
        self,
        tracker: SessionConfidenceTracker,
        router: ConfidenceEscalationRouter,
        calibration: ConfidenceCalibrationMonitor,
    ):
        self._tracker = tracker
        self._router = router
        self._calibration = calibration

    def render(self, top_n_sessions: int = 10) -> dict:
        summaries = [
            self._tracker.session_summary(sid)
            for sid in list(self._tracker._sessions.keys())[-top_n_sessions:]
        ]
        summaries = [s for s in summaries if s]

        degrading = [s for s in summaries if s.trend == "degrading"]
        low_conf = [s for s in summaries if s.avg_confidence < 0.4]

        return {
            "generated_at": time.time(),
            "sessions_tracked": len(self._tracker._sessions),
            "calibration_ece": self._calibration.expected_calibration_error(),
            "escalation_stats": self._router.stats(),
            "degrading_sessions": [
                {"session_id": s.session_id, "avg_confidence": s.avg_confidence}
                for s in degrading
            ],
            "low_confidence_sessions": [
                {"session_id": s.session_id, "avg_confidence": s.avg_confidence,
                 "flagged_decisions": len(s.flagged_decisions)}
                for s in low_conf
            ],
            "calibration_curve_summary": self._calibration.calibration_curve()[:5],
        }
```

## Comparison

| Approach | Signal Sources | Session Tracking | Escalation | Calibration |
|---|---|---|---|---|
| DecisionConfidenceRecord | Composite signals | No | No | No |
| ConfidenceExtractor | logprob, retrieval, hedge, ensemble | No | No | No |
| SessionConfidenceTracker | Via records | Yes (trend) | Alerts | No |
| ConfidenceEscalationRouter | Via records | No | Yes (policy) | No |
| ConfidenceCalibrationMonitor | Ground truth | No | No | Yes (ECE) |
| ConfidenceDashboard | All combined | Yes | Yes | Yes |

**Best for production**: Attach `ConfidenceExtractor` to every LLM call — at minimum extract hedge words from the response text and retrieval scores from the RAG layer. Compute composite scores in `DecisionConfidenceRecord` before routing. Register `EscalationPolicy` rules for high-stakes decision types (payment approval, account modification) with thresholds at 0.6–0.7. Route degrading sessions flagged by `SessionConfidenceTracker` to a human review queue automatically. Wire post-decision outcome feedback to `ConfidenceCalibrationMonitor` to detect when the model becomes over- or under-confident after a model update.
