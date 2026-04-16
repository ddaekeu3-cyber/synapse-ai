---
title: "Agent Doesn't Implement User Satisfaction Signal Collection"
description: "Agents that have no mechanism for collecting user satisfaction signals — thumbs up/down, explicit ratings, implicit signals like immediate follow-up corrections — cannot measure whether their responses are actually helpful or identify which tool combinations, prompt versions, and model settings produce the best outcomes. Implement satisfaction signal collection with implicit and explicit signal types, session-level aggregation, and quality trend detection."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-user-satisfaction-signal-collection
tags: [user-satisfaction, feedback-collection, implicit-signals, thumbs-up-down, quality-measurement, outcome-tracking]
symptoms:
  - "No way to know if users found responses helpful beyond observing session length"
  - "Model changes are deployed without any pre/post satisfaction comparison"
  - "Explicit feedback UI exists but signals are never stored or analyzed"
  - "Cannot identify which query categories produce the most dissatisfaction"
  - "On-call knows about failures but not about subtly unhelpful responses that users silently abandon"
---

## Why This Happens

Satisfaction measurement requires deliberate instrumentation at the agent boundary. Explicit signals (thumbs up/down, star ratings) require UI integration and are sparse — only 5–10% of users rate responses. Implicit signals are denser: a follow-up message that says "that's wrong" or "try again" is a strong negative signal; a follow-up that builds on the answer is a positive signal; silence followed by session end after a single response is ambiguous. Collecting both signal types, tagging them with session context, and aggregating to session-level satisfaction scores enables quality measurement without requiring every user to explicitly rate every response.

## Solution 1: Satisfaction Signal

```python
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class SignalType(str, Enum):
    EXPLICIT_POSITIVE = "explicit_positive"     # thumbs up, 4-5 stars
    EXPLICIT_NEGATIVE = "explicit_negative"     # thumbs down, 1-2 stars
    EXPLICIT_NEUTRAL = "explicit_neutral"       # 3 stars
    IMPLICIT_CORRECTION = "implicit_correction" # "that's wrong", "try again"
    IMPLICIT_BUILD_ON = "implicit_build_on"     # user continues productively
    IMPLICIT_ABANDON = "implicit_abandon"       # session ends after single exchange
    IMPLICIT_RETRY = "implicit_retry"           # immediate identical rephrasing


class SignalPolarity(str, Enum):
    POSITIVE = "positive"
    NEGATIVE = "negative"
    NEUTRAL = "neutral"
    AMBIGUOUS = "ambiguous"


SIGNAL_POLARITY_MAP = {
    SignalType.EXPLICIT_POSITIVE: SignalPolarity.POSITIVE,
    SignalType.EXPLICIT_NEGATIVE: SignalPolarity.NEGATIVE,
    SignalType.EXPLICIT_NEUTRAL: SignalPolarity.NEUTRAL,
    SignalType.IMPLICIT_CORRECTION: SignalPolarity.NEGATIVE,
    SignalType.IMPLICIT_BUILD_ON: SignalPolarity.POSITIVE,
    SignalType.IMPLICIT_ABANDON: SignalPolarity.AMBIGUOUS,
    SignalType.IMPLICIT_RETRY: SignalPolarity.NEGATIVE,
}


@dataclass
class SatisfactionSignal:
    session_id: str
    response_id: str
    signal_type: SignalType
    polarity: SignalPolarity
    weight: float               # 1.0 for explicit, 0.4 for implicit ambiguous
    tool_names_used: list = field(default_factory=list)
    model: str = ""
    template_id: str = ""
    intent: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    recorded_at: float = field(default_factory=time.time)
```

## Solution 2: Implicit Signal Detector

```python
import re
from typing import Optional


CORRECTION_PATTERNS = re.compile(
    r"\b(wrong|incorrect|that'?s?\s+not|no that|try again|not right|that doesn'?t|doesn'?t work"
    r"|that'?s?\s+bad|not helpful|useless|terrible|awful|that sucks)\b",
    re.IGNORECASE,
)
BUILD_ON_PATTERNS = re.compile(
    r"^(great|perfect|thanks|thank you|exactly|that works|awesome|helpful|got it"
    r"|makes sense|understood|yes|correct|now|next|also|and then|following up)\b",
    re.IGNORECASE,
)
RETRY_PATTERNS = re.compile(
    r"^(can you|please|could you|try to|again|re-?do|rephrase|explain again)\b",
    re.IGNORECASE,
)


class ImplicitSignalDetector:
    """
    Detects implicit satisfaction signals in follow-up user messages.
    Analyzes the relationship between the previous response and the
    next user message to infer positive/negative/retry signals.
    """

    def detect(
        self,
        follow_up_message: str,
        previous_response_id: str,
        session_id: str,
        **context,
    ) -> Optional[SatisfactionSignal]:
        text = follow_up_message.strip()

        if CORRECTION_PATTERNS.search(text):
            signal_type = SignalType.IMPLICIT_CORRECTION
            weight = 0.8
        elif BUILD_ON_PATTERNS.match(text):
            signal_type = SignalType.IMPLICIT_BUILD_ON
            weight = 0.6
        elif RETRY_PATTERNS.match(text) and len(text) < 120:
            signal_type = SignalType.IMPLICIT_RETRY
            weight = 0.7
        else:
            return None

        return SatisfactionSignal(
            session_id=session_id,
            response_id=previous_response_id,
            signal_type=signal_type,
            polarity=SIGNAL_POLARITY_MAP[signal_type],
            weight=weight,
            **context,
        )
```

## Solution 3: Signal Collector

```python
import time
from collections import defaultdict
from threading import Lock
from typing import Dict, List, Optional


class SatisfactionSignalCollector:
    """
    Accumulates satisfaction signals and computes session-level and
    aggregate satisfaction scores.
    """

    def __init__(self, max_signals: int = 100000):
        self._max = max_signals
        self._signals: List[SatisfactionSignal] = []
        self._lock = Lock()

    def record(self, signal: SatisfactionSignal) -> None:
        with self._lock:
            self._signals.append(signal)
            if len(self._signals) > self._max:
                self._signals.pop(0)

    def satisfaction_rate(
        self,
        window_seconds: float = 3600.0,
        intent: Optional[str] = None,
        model: Optional[str] = None,
    ) -> Optional[float]:
        """Fraction of signals with positive polarity (excluding ambiguous)."""
        cutoff = time.time() - window_seconds
        with self._lock:
            signals = [
                s for s in self._signals
                if s.recorded_at >= cutoff
                and s.polarity != SignalPolarity.AMBIGUOUS
                and (intent is None or s.intent == intent)
                and (model is None or s.model == model)
            ]
        if not signals:
            return None
        positive = sum(s.weight for s in signals if s.polarity == SignalPolarity.POSITIVE)
        total = sum(s.weight for s in signals)
        return round(positive / max(total, 0.001), 4)

    def by_intent(self, window_seconds: float = 3600.0) -> Dict[str, dict]:
        cutoff = time.time() - window_seconds
        with self._lock:
            recent = [s for s in self._signals if s.recorded_at >= cutoff]
        groups: Dict[str, list] = defaultdict(list)
        for s in recent:
            groups[s.intent or "unknown"].append(s)
        return {
            intent: {
                "count": len(sigs),
                "satisfaction_rate": round(
                    sum(s.weight for s in sigs if s.polarity == SignalPolarity.POSITIVE)
                    / max(sum(s.weight for s in sigs if s.polarity != SignalPolarity.AMBIGUOUS), 0.001),
                    4,
                ),
            }
            for intent, sigs in groups.items()
        }
```

## Solution 4: Quality Trend Detector

```python
from typing import Optional


class QualityTrendDetector:
    """
    Compares satisfaction rate between a baseline window and a recent window.
    Detects whether a model change, prompt update, or deployment caused regression.
    """

    def __init__(
        self,
        collector: SatisfactionSignalCollector,
        regression_threshold_pct: float = 5.0,
    ):
        self._collector = collector
        self._threshold = regression_threshold_pct / 100.0

    def detect(
        self,
        baseline_seconds: float = 86400.0,
        recent_seconds: float = 3600.0,
        intent: Optional[str] = None,
        model: Optional[str] = None,
    ) -> dict:
        baseline = self._collector.satisfaction_rate(baseline_seconds, intent, model)
        recent = self._collector.satisfaction_rate(recent_seconds, intent, model)

        if baseline is None or recent is None:
            return {
                "status": "insufficient_data",
                "baseline_satisfaction": baseline,
                "recent_satisfaction": recent,
            }

        change = recent - baseline
        regressed = change < -self._threshold

        return {
            "status": "regression" if regressed else "stable",
            "baseline_satisfaction": baseline,
            "recent_satisfaction": recent,
            "change": round(change, 4),
            "threshold": self._threshold,
            "intent": intent,
            "model": model,
        }
```

## Solution 5: Session Satisfaction Summarizer

```python
import time
from typing import Dict, List, Optional


class SessionSatisfactionSummarizer:
    """
    Produces a satisfaction summary for a completed session:
    how many signals were collected, net satisfaction, and
    whether the session ended on a positive or negative note.
    """

    def __init__(self, collector: SatisfactionSignalCollector):
        self._collector = collector

    def summarize_session(self, session_id: str) -> dict:
        with self._collector._lock:
            session_signals = [
                s for s in self._collector._signals
                if s.session_id == session_id
            ]
        if not session_signals:
            return {"session_id": session_id, "signals": 0}

        positive_weight = sum(s.weight for s in session_signals if s.polarity == SignalPolarity.POSITIVE)
        negative_weight = sum(s.weight for s in session_signals if s.polarity == SignalPolarity.NEGATIVE)
        total_weight = positive_weight + negative_weight

        last_polarity = session_signals[-1].polarity.value

        return {
            "session_id": session_id,
            "signals": len(session_signals),
            "net_satisfaction": round((positive_weight - negative_weight) / max(total_weight, 0.001), 4),
            "positive_weight": round(positive_weight, 2),
            "negative_weight": round(negative_weight, 2),
            "last_signal_polarity": last_polarity,
            "signal_types": [s.signal_type.value for s in session_signals],
        }
```

## Solution 6: Satisfaction Dashboard

```python
import time


class UserSatisfactionDashboard:
    """
    Combines overall satisfaction rates, per-intent breakdown,
    quality trend detection, and signal type distribution.
    """

    def __init__(
        self,
        collector: SatisfactionSignalCollector,
        trend_detector: QualityTrendDetector,
    ):
        self._collector = collector
        self._trend = trend_detector

    def render(self) -> dict:
        with self._collector._lock:
            signal_type_dist = {}
            for s in self._collector._signals[-1000:]:
                signal_type_dist[s.signal_type.value] = signal_type_dist.get(s.signal_type.value, 0) + 1

        return {
            "generated_at": time.time(),
            "satisfaction_1h": self._collector.satisfaction_rate(3600.0),
            "satisfaction_24h": self._collector.satisfaction_rate(86400.0),
            "by_intent_1h": self._collector.by_intent(3600.0),
            "quality_trend": self._trend.detect(),
            "signal_type_distribution": signal_type_dist,
        }
```

## Comparison

| Approach | Explicit Signals | Implicit Detection | Weighted Scoring | Trend Detection | Session Summary |
|---|---|---|---|---|---|
| SatisfactionSignal | Yes (type enum) | No | Yes (weight field) | No | No |
| ImplicitSignalDetector | No | Yes (regex) | Yes (0.6–0.8) | No | No |
| SatisfactionSignalCollector | Both | Via detector | Yes | No | No |
| QualityTrendDetector | Via collector | No | Via collector | Yes | No |
| SessionSatisfactionSummarizer | Via collector | No | Via collector | No | Yes |
| UserSatisfactionDashboard | No | No | No | No | Yes |

**Best for production**: Weight explicit signals (1.0) higher than implicit signals (0.4–0.8) in satisfaction rate calculations — a user who clicked thumbs down is a stronger signal than one who wrote "try again". Track `by_intent_1h` to find the intent category with the lowest satisfaction rate — this is where prompt engineering effort has the highest ROI. Use `QualityTrendDetector` as a deployment gate: if satisfaction rate drops more than 5 percentage points in the hour after a deployment, trigger a review before promoting to full traffic. Collect implicit signals passively without disrupting the user experience — the detector runs on every follow-up message with zero UI overhead.
