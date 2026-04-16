---
title: "Agent Doesn't Implement User Satisfaction Signal Collection"
description: "Agents that never collect explicit or implicit user satisfaction signals cannot correlate quality scores with actual user experience, cannot identify which sessions users abandoned vs. completed successfully, and cannot close the feedback loop for prompt improvement. Implement user satisfaction signal collection that captures explicit ratings, implicit behavioral signals (abandon, rephrase, follow-up), and correlates them with session quality metrics."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-user-satisfaction-signal-collection
tags: [user-satisfaction, feedback-collection, implicit-signals, abandon-detection, satisfaction-metrics, feedback-loop]
symptoms:
  - "No thumbs-up/thumbs-down data — quality improvement is based on internal metrics only"
  - "Users rephrase the same question three times but no signal captures this as a failure"
  - "Session abandonment is not distinguished from successful task completion"
  - "Internal quality scores are high but users are unhappy — no data to explain the gap"
  - "Prompt improvements are shipped without knowing whether they improved user satisfaction"
---

## Why This Happens

Agent quality is measured internally (response coherence, grounding, latency) but not from the user's perspective. A response can be internally valid but unhelpful. Without explicit signals (ratings, thumbs) and implicit signals (rephrasing, abandonment, follow-up questions), the feedback loop for improvement is broken. Correlation between internal quality scores and user satisfaction signals reveals whether the quality metrics are actually measuring what matters to users.

## Solution 1: Satisfaction Signal Record

```python
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class SignalType(str, Enum):
    EXPLICIT_RATING = "explicit_rating"       # user clicked thumbs up/down
    EXPLICIT_TEXT = "explicit_text"           # user typed feedback
    ABANDON = "abandon"                       # user left mid-session
    REPHRASE = "rephrase"                     # user sent very similar query again
    FOLLOW_UP = "follow_up"                   # user asked a clarifying question
    COPY_RESPONSE = "copy_response"           # user copied the response (positive signal)
    REGENERATE = "regenerate"                 # user clicked "try again"
    TASK_COMPLETE = "task_complete"           # user indicated success (e.g., "thanks")


@dataclass
class SatisfactionSignal:
    signal_id: str = field(default_factory=lambda: str(uuid.uuid4())[:10])
    session_id: str = ""
    user_id: str = ""
    turn_index: Optional[int] = None
    signal_type: SignalType = SignalType.EXPLICIT_RATING
    value: Optional[float] = None        # 1.0 = positive, 0.0 = negative, None = neutral
    text_feedback: Optional[str] = None
    recorded_at: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def is_positive(self) -> Optional[bool]:
        if self.value is None:
            return None
        return self.value >= 0.5

    def sentiment_label(self) -> str:
        if self.value is None:
            return "neutral"
        return "positive" if self.value >= 0.5 else "negative"
```

## Solution 2: Implicit Signal Detector

```python
import re
from typing import List, Optional


class ImplicitSatisfactionSignalDetector:
    """
    Detects implicit satisfaction signals from user message patterns.
    Rephrase detection: user message is semantically similar to a recent query.
    Abandon: session ends without a completion marker after a low-quality response.
    Task completion: positive closing phrases.
    """

    TASK_COMPLETE_PATTERNS = re.compile(
        r"\b(thank(s| you)|perfect|great|got it|that'?s? (exactly|it|helpful|what i needed)|solved)\b",
        re.IGNORECASE,
    )
    DISSATISFACTION_PATTERNS = re.compile(
        r"\b(that'?s? (not|wrong|incorrect)|that didn'?t (help|work|answer)|try again|that'?s? not (right|what i asked))\b",
        re.IGNORECASE,
    )
    FOLLOW_UP_PATTERNS = re.compile(
        r"^(what (do you mean|about)|can you (clarify|explain|elaborate)|how (so|does that work)|why)\b",
        re.IGNORECASE,
    )

    def detect(
        self,
        user_message: str,
        previous_user_messages: List[str] = None,
        session_id: str = "",
        turn_index: int = 0,
    ) -> List[SatisfactionSignal]:
        signals = []
        msg = user_message.strip()

        if self.TASK_COMPLETE_PATTERNS.search(msg):
            signals.append(SatisfactionSignal(
                session_id=session_id,
                turn_index=turn_index,
                signal_type=SignalType.TASK_COMPLETE,
                value=1.0,
            ))

        if self.DISSATISFACTION_PATTERNS.search(msg):
            signals.append(SatisfactionSignal(
                session_id=session_id,
                turn_index=turn_index,
                signal_type=SignalType.REGENERATE,
                value=0.0,
            ))

        if self.FOLLOW_UP_PATTERNS.search(msg):
            signals.append(SatisfactionSignal(
                session_id=session_id,
                turn_index=turn_index,
                signal_type=SignalType.FOLLOW_UP,
                value=0.3,   # mild negative: user needed clarification
            ))

        if previous_user_messages:
            prev_words = set(re.findall(r"\b\w{4,}\b", previous_user_messages[-1].lower()))
            curr_words = set(re.findall(r"\b\w{4,}\b", msg.lower()))
            if prev_words and curr_words:
                overlap = len(prev_words & curr_words) / max(len(prev_words), len(curr_words))
                if overlap >= 0.6:
                    signals.append(SatisfactionSignal(
                        session_id=session_id,
                        turn_index=turn_index,
                        signal_type=SignalType.REPHRASE,
                        value=0.1,
                    ))

        return signals
```

## Solution 3: Signal Aggregator

```python
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class SessionSatisfactionSummary:
    session_id: str
    explicit_score: Optional[float] = None
    implicit_score: Optional[float] = None
    combined_score: Optional[float] = None
    signal_count: int = 0
    positive_signals: int = 0
    negative_signals: int = 0
    has_rephrase: bool = False
    has_abandon: bool = False
    has_completion: bool = False


class SatisfactionSignalAggregator:
    """
    Collects all signals for a session and computes aggregate satisfaction scores.
    Explicit signals (ratings) are weighted higher than implicit signals.
    """

    SIGNAL_WEIGHTS = {
        SignalType.EXPLICIT_RATING: 1.0,
        SignalType.EXPLICIT_TEXT: 0.8,
        SignalType.TASK_COMPLETE: 0.7,
        SignalType.COPY_RESPONSE: 0.6,
        SignalType.FOLLOW_UP: 0.3,
        SignalType.REPHRASE: 0.4,
        SignalType.REGENERATE: 0.5,
        SignalType.ABANDON: 0.6,
    }

    def __init__(self):
        self._signals: Dict[str, List[SatisfactionSignal]] = defaultdict(list)

    def record(self, signal: SatisfactionSignal) -> None:
        self._signals[signal.session_id].append(signal)

    def summarize(self, session_id: str) -> Optional[SessionSatisfactionSummary]:
        signals = self._signals.get(session_id, [])
        if not signals:
            return None

        summary = SessionSatisfactionSummary(session_id=session_id)
        summary.signal_count = len(signals)

        explicit = [s for s in signals if s.signal_type in (SignalType.EXPLICIT_RATING, SignalType.EXPLICIT_TEXT)]
        implicit = [s for s in signals if s.signal_type not in (SignalType.EXPLICIT_RATING, SignalType.EXPLICIT_TEXT)]

        if explicit:
            scored_explicit = [s for s in explicit if s.value is not None]
            if scored_explicit:
                summary.explicit_score = round(
                    sum(s.value for s in scored_explicit) / len(scored_explicit), 4
                )

        if implicit:
            scored_implicit = [s for s in implicit if s.value is not None]
            if scored_implicit:
                weights = [self.SIGNAL_WEIGHTS.get(s.signal_type, 0.3) for s in scored_implicit]
                summary.implicit_score = round(
                    sum(s.value * w for s, w in zip(scored_implicit, weights))
                    / max(sum(weights), 1e-9),
                    4,
                )

        scores = [s for s in [summary.explicit_score, summary.implicit_score] if s is not None]
        if scores:
            summary.combined_score = round(sum(scores) / len(scores), 4)

        summary.positive_signals = sum(1 for s in signals if (s.value or 0) >= 0.5)
        summary.negative_signals = sum(1 for s in signals if (s.value or 1) < 0.5 and s.value is not None)
        summary.has_rephrase = any(s.signal_type == SignalType.REPHRASE for s in signals)
        summary.has_abandon = any(s.signal_type == SignalType.ABANDON for s in signals)
        summary.has_completion = any(s.signal_type == SignalType.TASK_COMPLETE for s in signals)

        return summary
```

## Solution 4: Quality-Satisfaction Correlator

```python
from typing import Dict, List, Optional, Tuple


class QualitySatisfactionCorrelator:
    """
    Correlates internal quality scores with user satisfaction signals
    to measure whether quality metrics actually predict user happiness.
    """

    def __init__(self, aggregator: SatisfactionSignalAggregator):
        self._aggregator = aggregator
        self._pairs: List[Tuple[float, float]] = []   # (quality_score, satisfaction_score)

    def add_session(
        self,
        session_id: str,
        internal_quality_score: float,
    ) -> None:
        summary = self._aggregator.summarize(session_id)
        if summary and summary.combined_score is not None:
            self._pairs.append((internal_quality_score, summary.combined_score))

    def pearson_correlation(self) -> Optional[float]:
        if len(self._pairs) < 5:
            return None
        n = len(self._pairs)
        quality_vals = [p[0] for p in self._pairs]
        sat_vals = [p[1] for p in self._pairs]
        mean_q = sum(quality_vals) / n
        mean_s = sum(sat_vals) / n
        numerator = sum((q - mean_q) * (s - mean_s) for q, s in self._pairs)
        denom_q = sum((q - mean_q) ** 2 for q in quality_vals) ** 0.5
        denom_s = sum((s - mean_s) ** 2 for s in sat_vals) ** 0.5
        if denom_q * denom_s == 0:
            return None
        return round(numerator / (denom_q * denom_s), 4)

    def calibration_report(self) -> dict:
        r = self.pearson_correlation()
        return {
            "sample_count": len(self._pairs),
            "pearson_r": r,
            "interpretation": (
                "strong alignment" if r and r > 0.7
                else "moderate alignment" if r and r > 0.4
                else "weak alignment — quality metrics may not reflect user experience"
                if r is not None else "insufficient data"
            ),
        }
```

## Solution 5: Satisfaction Fleet Dashboard

```python
import time
from typing import List


class SatisfactionFleetDashboard:
    """
    Fleet-level satisfaction metrics: overall CSAT, rephrase rate, abandon rate,
    completion rate, and calibration with internal quality scores.
    """

    def __init__(
        self,
        aggregator: SatisfactionSignalAggregator,
        correlator: QualitySatisfactionCorrelator,
    ):
        self._aggregator = aggregator
        self._correlator = correlator

    def render(self, session_ids: List[str]) -> dict:
        summaries = [
            self._aggregator.summarize(sid)
            for sid in session_ids
            if self._aggregator.summarize(sid) is not None
        ]

        scored = [s for s in summaries if s.combined_score is not None]
        csat = round(sum(s.combined_score for s in scored) / max(len(scored), 1), 4)
        rephrase_rate = round(sum(1 for s in summaries if s.has_rephrase) / max(len(summaries), 1), 4)
        abandon_rate = round(sum(1 for s in summaries if s.has_abandon) / max(len(summaries), 1), 4)
        completion_rate = round(sum(1 for s in summaries if s.has_completion) / max(len(summaries), 1), 4)

        return {
            "generated_at": time.time(),
            "sessions_analyzed": len(summaries),
            "csat_score": csat,
            "rephrase_rate": rephrase_rate,
            "abandon_rate": abandon_rate,
            "completion_rate": completion_rate,
            "quality_calibration": self._correlator.calibration_report(),
        }
```

## Solution 6: Feedback-Driven Prompt Improvement Tracker

```python
import time
from typing import Dict, List, Optional


class FeedbackDrivenImprovementTracker:
    """
    Tracks satisfaction metrics before and after prompt changes
    to measure whether improvements actually helped users.
    """

    def __init__(self):
        self._experiments: Dict[str, dict] = {}

    def start_experiment(
        self,
        experiment_id: str,
        prompt_version: str,
        description: str,
    ) -> None:
        self._experiments[experiment_id] = {
            "prompt_version": prompt_version,
            "description": description,
            "started_at": time.time(),
            "csat_samples": [],
        }

    def record_csat(self, experiment_id: str, csat_score: float) -> None:
        exp = self._experiments.get(experiment_id)
        if exp:
            exp["csat_samples"].append(csat_score)

    def result(self, experiment_id: str) -> Optional[dict]:
        exp = self._experiments.get(experiment_id)
        if not exp or not exp["csat_samples"]:
            return None
        samples = exp["csat_samples"]
        mean = sum(samples) / len(samples)
        return {
            "experiment_id": experiment_id,
            "prompt_version": exp["prompt_version"],
            "description": exp["description"],
            "sample_count": len(samples),
            "mean_csat": round(mean, 4),
        }
```

## Comparison

| Approach | Explicit Signals | Implicit Detection | Score Aggregation | Quality Correlation | Dashboard |
|---|---|---|---|---|---|
| SatisfactionSignal | Yes (data model) | No | No | No | No |
| ImplicitSatisfactionSignalDetector | No | Yes (pattern-based) | No | No | No |
| SatisfactionSignalAggregator | Via signals | Via signals | Yes (weighted) | No | No |
| QualitySatisfactionCorrelator | Via aggregator | No | No | Yes (Pearson r) | No |
| SatisfactionFleetDashboard | Via aggregator | Via aggregator | Via aggregator | Via correlator | Yes |

**Best for production**: Collect explicit ratings on at least 5% of sessions (via a thumbs-up/down UI element) — this is the highest-quality signal and anchors the implicit signal calibration. Run `ImplicitSatisfactionSignalDetector` on every user message: rephrase detection is free and identifies the most common failure mode (user repeating themselves). Track `rephrase_rate` as a primary product health metric — target below 8%. Run `QualitySatisfactionCorrelator` weekly: a Pearson r below 0.4 between internal quality scores and CSAT means the quality metrics are not measuring what matters to users and should be revised.
