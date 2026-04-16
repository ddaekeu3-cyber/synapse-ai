---
title: "Agent Doesn't Implement User Satisfaction Signal Collection"
description: "Agents that measure only technical metrics — latency, token count, error rate — cannot tell whether users found responses helpful. A fast, cheap response that answers the wrong question scores perfectly on technical metrics but fails the user. Implement user satisfaction signal collection that captures explicit feedback, implicit behavioral signals, and session outcome indicators to build a quality feedback loop."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-user-satisfaction-signal-collection
tags: [user-satisfaction, feedback, implicit-signals, csat, thumbs-up-down, quality-metrics]
symptoms:
  - "No thumbs-up/thumbs-down or rating mechanism on agent responses"
  - "High retry rate (user rephrases the same question) is not tracked as a dissatisfaction signal"
  - "Session abandonment after a single response is not counted as implicit negative feedback"
  - "No correlation between technical metrics and user satisfaction outcomes"
  - "Quality improvements cannot be measured because there is no satisfaction baseline"
---

## Why This Happens

Technical observability focuses on what the system did, not whether the user was helped. Satisfaction signals require capturing both explicit feedback (thumbs, ratings, free-text) and implicit behavioral signals (did the user rephrase? did they abandon? did they continue the conversation?). These signals must be attached to specific responses and turns so that model or prompt changes can be correlated with satisfaction changes. Without this instrumentation, quality improvements are unmeasurable.

## Solution 1: Satisfaction Signal Model

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional
import time


class FeedbackType(str, Enum):
    THUMBS_UP = "thumbs_up"
    THUMBS_DOWN = "thumbs_down"
    STAR_RATING = "star_rating"          # 1–5
    FREE_TEXT = "free_text"
    IMPLICIT_RETRY = "implicit_retry"    # user rephrased same query
    IMPLICIT_ABANDON = "implicit_abandon"  # session ended after this turn
    IMPLICIT_CONTINUE = "implicit_continue"  # user sent follow-up (positive signal)
    IMPLICIT_COPY = "implicit_copy"      # user copied response text


@dataclass
class SatisfactionSignal:
    signal_id: str
    session_id: str
    turn_id: str
    response_id: str
    feedback_type: FeedbackType
    value: Any = None                    # rating int, free text str, or None for binary
    collected_at: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_positive(self) -> Optional[bool]:
        if self.feedback_type in (FeedbackType.THUMBS_UP, FeedbackType.IMPLICIT_CONTINUE,
                                   FeedbackType.IMPLICIT_COPY):
            return True
        if self.feedback_type in (FeedbackType.THUMBS_DOWN, FeedbackType.IMPLICIT_ABANDON,
                                   FeedbackType.IMPLICIT_RETRY):
            return False
        if self.feedback_type == FeedbackType.STAR_RATING and isinstance(self.value, (int, float)):
            return self.value >= 4
        return None
```

## Solution 2: Satisfaction Signal Store

```python
import json
import secrets
import time
from pathlib import Path
from threading import Lock
from typing import List, Optional


class SatisfactionSignalStore:
    """
    Persists satisfaction signals to JSONL for offline analysis.
    Supports in-memory queries for real-time dashboards.
    """

    def __init__(self, path: Optional[str] = None, max_memory: int = 50_000):
        self._path = Path(path) if path else None
        self._max = max_memory
        self._signals: List[SatisfactionSignal] = []
        self._lock = Lock()

    def record(self, signal: SatisfactionSignal) -> None:
        with self._lock:
            self._signals.append(signal)
            if len(self._signals) > self._max:
                self._signals.pop(0)
            if self._path:
                with self._path.open("a") as f:
                    f.write(json.dumps({
                        "signal_id": signal.signal_id,
                        "session_id": signal.session_id,
                        "turn_id": signal.turn_id,
                        "response_id": signal.response_id,
                        "feedback_type": signal.feedback_type.value,
                        "value": signal.value,
                        "collected_at": signal.collected_at,
                        "is_positive": signal.is_positive,
                    }) + "\n")

    def recent(self, window_seconds: float = 3600.0) -> List[SatisfactionSignal]:
        cutoff = time.time() - window_seconds
        with self._lock:
            return [s for s in self._signals if s.collected_at >= cutoff]

    def for_response(self, response_id: str) -> List[SatisfactionSignal]:
        with self._lock:
            return [s for s in self._signals if s.response_id == response_id]
```

## Solution 3: Implicit Signal Detector

```python
import time
from typing import List, Optional


class ImplicitSatisfactionDetector:
    """
    Derives implicit satisfaction signals from behavioral events:
    retry detection (user submits semantically similar query shortly after),
    session abandonment (no follow-up within idle window), and
    continuation (user sends a follow-up — positive signal).
    """

    def __init__(
        self,
        retry_window_seconds: float = 120.0,
        abandon_window_seconds: float = 300.0,
        similarity_threshold: float = 0.60,
    ):
        self._retry_window = retry_window_seconds
        self._abandon_window = abandon_window_seconds
        self._sim_threshold = similarity_threshold

    def detect_retry(
        self,
        previous_query: str,
        new_query: str,
        elapsed_seconds: float,
    ) -> bool:
        if elapsed_seconds > self._retry_window:
            return False
        words_prev = set(previous_query.lower().split())
        words_new = set(new_query.lower().split())
        if not words_prev or not words_new:
            return False
        overlap = len(words_prev & words_new) / len(words_prev | words_new)
        return overlap >= self._sim_threshold

    def detect_abandonment(
        self,
        last_turn_time: float,
        current_time: Optional[float] = None,
    ) -> bool:
        now = current_time or time.time()
        return (now - last_turn_time) > self._abandon_window

    def create_signal(
        self,
        signal_type: FeedbackType,
        session_id: str,
        turn_id: str,
        response_id: str,
    ) -> SatisfactionSignal:
        return SatisfactionSignal(
            signal_id=f"implicit_{signal_type.value}_{session_id[:8]}",
            session_id=session_id,
            turn_id=turn_id,
            response_id=response_id,
            feedback_type=signal_type,
        )
```

## Solution 4: CSAT Score Calculator

```python
from typing import List, Optional


class CSATScoreCalculator:
    """
    Computes Customer Satisfaction (CSAT) and Net Promoter Score (NPS)
    proxies from collected satisfaction signals.
    """

    def compute_csat(self, signals: List[SatisfactionSignal]) -> Optional[float]:
        """
        CSAT = positive signals / (positive + negative signals).
        Excludes neutral and unclassified signals.
        """
        positive = sum(1 for s in signals if s.is_positive is True)
        negative = sum(1 for s in signals if s.is_positive is False)
        total = positive + negative
        if total == 0:
            return None
        return round(positive / total, 4)

    def compute_explicit_rating_avg(
        self, signals: List[SatisfactionSignal]
    ) -> Optional[float]:
        ratings = [
            s.value for s in signals
            if s.feedback_type == FeedbackType.STAR_RATING
            and isinstance(s.value, (int, float))
        ]
        if not ratings:
            return None
        return round(sum(ratings) / len(ratings), 2)

    def retry_rate(self, signals: List[SatisfactionSignal]) -> float:
        retries = sum(1 for s in signals if s.feedback_type == FeedbackType.IMPLICIT_RETRY)
        turns = len({s.turn_id for s in signals})
        return round(retries / max(turns, 1), 4)

    def abandon_rate(self, signals: List[SatisfactionSignal]) -> float:
        abandons = sum(1 for s in signals if s.feedback_type == FeedbackType.IMPLICIT_ABANDON)
        sessions = len({s.session_id for s in signals})
        return round(abandons / max(sessions, 1), 4)
```

## Solution 5: Feedback Collection Endpoint

```python
import secrets
import time
from typing import Any, Optional


class FeedbackCollectionEndpoint:
    """
    Accepts explicit feedback submissions from the UI layer
    and writes them to the satisfaction signal store.
    """

    def __init__(self, store: SatisfactionSignalStore):
        self._store = store

    def submit_thumbs(
        self,
        response_id: str,
        session_id: str,
        turn_id: str,
        positive: bool,
    ) -> SatisfactionSignal:
        signal = SatisfactionSignal(
            signal_id=secrets.token_hex(8),
            session_id=session_id,
            turn_id=turn_id,
            response_id=response_id,
            feedback_type=FeedbackType.THUMBS_UP if positive else FeedbackType.THUMBS_DOWN,
        )
        self._store.record(signal)
        return signal

    def submit_rating(
        self,
        response_id: str,
        session_id: str,
        turn_id: str,
        stars: int,
        free_text: Optional[str] = None,
    ) -> SatisfactionSignal:
        signal = SatisfactionSignal(
            signal_id=secrets.token_hex(8),
            session_id=session_id,
            turn_id=turn_id,
            response_id=response_id,
            feedback_type=FeedbackType.STAR_RATING,
            value=max(1, min(5, stars)),
            metadata={"free_text": free_text} if free_text else {},
        )
        self._store.record(signal)
        return signal
```

## Solution 6: Satisfaction Dashboard

```python
import time


class UserSatisfactionDashboard:
    """
    Combines CSAT, retry rate, abandon rate, and explicit ratings
    into a single quality report.
    """

    def __init__(
        self,
        store: SatisfactionSignalStore,
        calculator: CSATScoreCalculator,
    ):
        self._store = store
        self._calc = calculator

    def render(self, window_seconds: float = 86400.0) -> dict:
        signals = self._store.recent(window_seconds)
        csat = self._calc.compute_csat(signals)
        rating_avg = self._calc.compute_explicit_rating_avg(signals)
        retry_rate = self._calc.retry_rate(signals)
        abandon_rate = self._calc.abandon_rate(signals)

        by_type: dict = {}
        for s in signals:
            ft = s.feedback_type.value
            by_type[ft] = by_type.get(ft, 0) + 1

        return {
            "generated_at": time.time(),
            "window_seconds": window_seconds,
            "total_signals": len(signals),
            "csat": csat,
            "explicit_rating_avg": rating_avg,
            "retry_rate": retry_rate,
            "abandon_rate": abandon_rate,
            "signals_by_type": by_type,
            "health": (
                "healthy" if csat is not None and csat >= 0.80
                else "degraded" if csat is not None and csat >= 0.60
                else "unknown"
            ),
        }
```

## Comparison

| Approach | Explicit Feedback | Implicit Signals | CSAT Computation | Persistence | Dashboard |
|---|---|---|---|---|---|
| SatisfactionSignalStore | Yes | Yes | No | Yes (JSONL) | No |
| ImplicitSatisfactionDetector | No | Yes (retry+abandon) | No | No | No |
| CSATScoreCalculator | Via signals | Via signals | Yes | No | No |
| FeedbackCollectionEndpoint | Yes (thumbs+stars) | No | No | Via store | No |
| UserSatisfactionDashboard | No | No | Via calculator | No | Yes |

**Best for production**: Collect implicit signals passively — they are lower friction than explicit feedback and scale with every interaction. Use retry detection (`ImplicitSatisfactionDetector.detect_retry()`) as your primary quality signal during A/B tests: a new model that produces a higher retry rate is worse for users even if technical metrics improve. Set a 24-hour CSAT dashboard window and alert when CSAT drops below 0.75 — this is a leading indicator of user-visible quality regressions that won't appear in latency or error rate dashboards. Persist raw signals to JSONL so offline analysis can build labeled datasets for fine-tuning.
