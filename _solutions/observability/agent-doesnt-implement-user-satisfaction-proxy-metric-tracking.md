---
title: "Agent Doesn't Implement User Satisfaction Proxy Metric Tracking"
description: "Agents that lack user satisfaction signals have no way to distinguish between responses users found useful and responses they ignored or retried. Without satisfaction proxies, teams optimize for throughput and latency but miss degraded answer quality. Implement proxy metrics — retry rates, follow-up clarification rates, session abandonment, thumbs signals, and response edit rates — to build a leading indicator of user satisfaction before explicit feedback systems are in place."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-user-satisfaction-proxy-metric-tracking
tags: [user-satisfaction, proxy-metrics, retry-rate, session-abandonment, implicit-feedback, quality-signals]
symptoms:
  - "No way to know whether users found agent responses helpful or useless"
  - "Latency and throughput metrics are green but user complaints are rising"
  - "Retry rates (user rephrases the same question) are not tracked"
  - "Session abandonment after a specific response type is invisible"
  - "No leading indicator of answer quality degradation before support tickets arrive"
---

## Why This Happens

Explicit feedback (thumbs up/down, star ratings) requires user action and sees response rates below 5% in most chat interfaces. Teams rely on it anyway because implicit signals are not collected. Implicit satisfaction proxies are emitted by users naturally: if a response is wrong, the user rephrases immediately (retry); if it is confusing, they ask a clarifying follow-up; if it is completely unhelpful, they leave (abandonment). Collecting these signals requires tracking session continuity — correlating consecutive turns in the same session to detect the patterns that distinguish satisfied users from frustrated ones.

## Solution 1: Session Turn Event

```python
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class TurnOutcomeSignal(str, Enum):
    CONTINUED = "continued"           # user sent a normal follow-up
    RETRY = "retry"                   # user rephrased the same question
    CLARIFICATION_REQUEST = "clarification_request"  # user asked for more detail
    EXPLICIT_POSITIVE = "explicit_positive"  # thumbs up / "thanks"
    EXPLICIT_NEGATIVE = "explicit_negative"  # thumbs down / "that's wrong"
    ABANDONED = "abandoned"           # session ended shortly after response
    EDITED_RESPONSE = "edited_response"  # user edited or copy-corrected the output


@dataclass
class SessionTurnEvent:
    session_id: str
    turn_index: int
    timestamp: float
    response_latency_ms: float
    response_token_count: int
    user_message: str
    agent_response_preview: str      # first 200 chars for signal extraction
    outcome_signal: Optional[TurnOutcomeSignal] = None
    time_to_next_turn_ms: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
```

## Solution 2: Implicit Signal Detector

```python
import re
from typing import Optional


_RETRY_PATTERNS = [
    r"\bthat('s| is) (wrong|incorrect|not right|not what I)\b",
    r"\bno[,.]? (I mean|I meant|I was asking)\b",
    r"\btry again\b",
    r"\bcan you redo\b",
    r"\bignore (that|the previous)\b",
    r"\bstill (wrong|not|incorrect)\b",
]

_CLARIFICATION_PATTERNS = [
    r"\bcan you (explain|clarify|elaborate)\b",
    r"\bwhat do you mean\b",
    r"\bI don'?t understand\b",
    r"\bcould you (be more|give more)\b",
    r"\bmore detail\b",
    r"\bcan you expand\b",
]

_POSITIVE_PATTERNS = [
    r"\b(thank(s| you)|perfect|great|exactly|awesome|that('s| is) (correct|right|helpful))\b",
    r"\b(well done|good job|nailed it)\b",
]

_NEGATIVE_PATTERNS = [
    r"\b(wrong|incorrect|that('s| is) not|you'?re wrong|bad answer|useless|unhelpful)\b",
    r"\b(terrible|awful|disappointing)\b",
]


class ImplicitSignalDetector:
    """
    Detects satisfaction signals from the text of the user's follow-up message
    and the time elapsed between the agent response and the follow-up.
    """

    def __init__(
        self,
        abandon_threshold_ms: float = 30_000.0,   # session end within 30s = abandoned
        retry_overlap_threshold: float = 0.5,      # token overlap > 50% = retry
    ):
        self._abandon_ms = abandon_threshold_ms
        self._retry_overlap = retry_overlap_threshold

    def detect(
        self,
        prev_user_message: str,
        follow_up_message: Optional[str],
        time_to_follow_up_ms: Optional[float],
        session_ended: bool = False,
    ) -> TurnOutcomeSignal:
        # Abandonment: session ended quickly after response
        if session_ended and (
            time_to_follow_up_ms is None or time_to_follow_up_ms < self._abandon_ms
        ):
            return TurnOutcomeSignal.ABANDONED

        if follow_up_message is None:
            return TurnOutcomeSignal.CONTINUED

        text = follow_up_message.lower()

        # Explicit signals first
        for pat in _POSITIVE_PATTERNS:
            if re.search(pat, text, re.IGNORECASE):
                return TurnOutcomeSignal.EXPLICIT_POSITIVE

        for pat in _NEGATIVE_PATTERNS:
            if re.search(pat, text, re.IGNORECASE):
                return TurnOutcomeSignal.EXPLICIT_NEGATIVE

        # Retry: semantic overlap with prior question + correction language
        for pat in _RETRY_PATTERNS:
            if re.search(pat, text, re.IGNORECASE):
                return TurnOutcomeSignal.RETRY

        if self._token_overlap(prev_user_message, follow_up_message) > self._retry_overlap:
            return TurnOutcomeSignal.RETRY

        # Clarification request
        for pat in _CLARIFICATION_PATTERNS:
            if re.search(pat, text, re.IGNORECASE):
                return TurnOutcomeSignal.CLARIFICATION_REQUEST

        return TurnOutcomeSignal.CONTINUED

    @staticmethod
    def _token_overlap(a: str, b: str) -> float:
        tokens_a = set(re.findall(r"\w+", a.lower()))
        tokens_b = set(re.findall(r"\w+", b.lower()))
        if not tokens_a or not tokens_b:
            return 0.0
        return len(tokens_a & tokens_b) / len(tokens_a | tokens_b)
```

## Solution 3: Session Satisfaction Tracker

```python
import time
from collections import defaultdict
from threading import Lock
from typing import Dict, List, Optional


class SessionSatisfactionTracker:
    """
    Accumulates turn events per session and computes a session-level
    satisfaction summary when the session closes or on demand.
    """

    def __init__(self, session_ttl_seconds: float = 3600.0):
        self._lock = Lock()
        self._sessions: Dict[str, List[SessionTurnEvent]] = defaultdict(list)
        self._session_start: Dict[str, float] = {}
        self._ttl = session_ttl_seconds

    def record_turn(self, event: SessionTurnEvent) -> None:
        with self._lock:
            if event.session_id not in self._session_start:
                self._session_start[event.session_id] = time.time()
            self._sessions[event.session_id].append(event)
            self._evict_stale()

    def session_summary(self, session_id: str) -> dict:
        with self._lock:
            turns = self._sessions.get(session_id, [])

        if not turns:
            return {"session_id": session_id, "turns": 0}

        signal_counts: Dict[str, int] = defaultdict(int)
        for t in turns:
            if t.outcome_signal:
                signal_counts[t.outcome_signal.value] += 1

        total = len(turns)
        retry_rate = signal_counts.get("retry", 0) / total
        clarification_rate = signal_counts.get("clarification_request", 0) / total
        positive_rate = signal_counts.get("explicit_positive", 0) / total
        negative_rate = signal_counts.get("explicit_negative", 0) / total
        abandoned = signal_counts.get("abandoned", 0) > 0

        satisfaction_score = (
            positive_rate * 1.0
            - negative_rate * 2.0
            - retry_rate * 1.5
            - clarification_rate * 0.5
            - (1.0 if abandoned else 0.0)
        )

        return {
            "session_id": session_id,
            "turns": total,
            "retry_rate": round(retry_rate, 4),
            "clarification_rate": round(clarification_rate, 4),
            "positive_rate": round(positive_rate, 4),
            "negative_rate": round(negative_rate, 4),
            "abandoned": abandoned,
            "satisfaction_score": round(satisfaction_score, 4),
            "signal_counts": dict(signal_counts),
        }

    def _evict_stale(self) -> None:
        cutoff = time.time() - self._ttl
        stale = [
            sid for sid, start in self._session_start.items() if start < cutoff
        ]
        for sid in stale:
            self._sessions.pop(sid, None)
            self._session_start.pop(sid, None)
```

## Solution 4: Aggregate Satisfaction Metrics Recorder

```python
import time
from collections import deque
from threading import Lock
from typing import Deque, Optional, Tuple


class AggregateSatisfactionMetricsRecorder:
    """
    Aggregates session satisfaction summaries into fleet-level metrics.
    Supports sliding window percentile queries for SLO tracking.
    """

    def __init__(self, max_records: int = 50_000):
        self._lock = Lock()
        self._records: Deque[Tuple[float, dict]] = deque()
        self._max = max_records

    def record(self, summary: dict) -> None:
        with self._lock:
            self._records.append((time.time(), summary))
            if len(self._records) > self._max:
                self._records.popleft()

    def aggregate(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        with self._lock:
            recent = [s for ts, s in self._records if ts >= cutoff]

        if not recent:
            return {"window_seconds": window_seconds, "sessions": 0}

        n = len(recent)
        retry_rates = [s["retry_rate"] for s in recent]
        clarification_rates = [s["clarification_rate"] for s in recent]
        scores = [s["satisfaction_score"] for s in recent]
        abandonment_count = sum(1 for s in recent if s.get("abandoned"))

        def avg(lst):
            return round(sum(lst) / len(lst), 4) if lst else 0.0

        return {
            "window_seconds": window_seconds,
            "sessions": n,
            "avg_satisfaction_score": avg(scores),
            "avg_retry_rate": avg(retry_rates),
            "avg_clarification_rate": avg(clarification_rates),
            "abandonment_rate": round(abandonment_count / n, 4),
            "sessions_with_explicit_positive": sum(
                1 for s in recent if s.get("positive_rate", 0) > 0
            ),
            "sessions_with_explicit_negative": sum(
                1 for s in recent if s.get("negative_rate", 0) > 0
            ),
        }
```

## Solution 5: Satisfaction Regression Detector

```python
import time
from typing import Optional


class SatisfactionRegressionDetector:
    """
    Compares satisfaction score averages between a baseline window and a
    recent window to detect regressions introduced by model or prompt changes.
    """

    def __init__(
        self,
        recorder: AggregateSatisfactionMetricsRecorder,
        regression_threshold: float = 0.15,
    ):
        self._recorder = recorder
        self._threshold = regression_threshold

    def check(
        self,
        baseline_window_seconds: float = 86400.0,
        recent_window_seconds: float = 3600.0,
    ) -> dict:
        baseline = self._recorder.aggregate(baseline_window_seconds)
        recent = self._recorder.aggregate(recent_window_seconds)

        if baseline["sessions"] < 10 or recent["sessions"] < 5:
            return {
                "status": "insufficient_data",
                "baseline_sessions": baseline["sessions"],
                "recent_sessions": recent["sessions"],
            }

        baseline_score = baseline["avg_satisfaction_score"]
        recent_score = recent["avg_satisfaction_score"]
        delta = recent_score - baseline_score
        regressed = delta < -self._threshold

        return {
            "status": "regression" if regressed else "ok",
            "baseline_avg_score": baseline_score,
            "recent_avg_score": recent_score,
            "delta": round(delta, 4),
            "threshold": -self._threshold,
            "regressed": regressed,
            "baseline_retry_rate": baseline["avg_retry_rate"],
            "recent_retry_rate": recent["avg_retry_rate"],
            "baseline_abandonment_rate": baseline["abandonment_rate"],
            "recent_abandonment_rate": recent["abandonment_rate"],
        }
```

## Solution 6: Satisfaction Proxy Dashboard

```python
import time


class SatisfactionProxyDashboard:
    """
    Combines session-level satisfaction tracking and fleet-level
    aggregation into a single operational view.
    """

    def __init__(
        self,
        tracker: SessionSatisfactionTracker,
        recorder: AggregateSatisfactionMetricsRecorder,
        regression_detector: SatisfactionRegressionDetector,
    ):
        self._tracker = tracker
        self._recorder = recorder
        self._detector = regression_detector

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "last_hour": self._recorder.aggregate(window_seconds=3600.0),
            "last_24h": self._recorder.aggregate(window_seconds=86400.0),
            "regression": self._detector.check(),
        }
```

## Comparison

| Approach | Implicit Signal Detection | Session Aggregation | Fleet Aggregation | Regression Detection | Dashboard |
|---|---|---|---|---|---|
| ImplicitSignalDetector | Yes (retry/clarify/abandon) | No | No | No | No |
| SessionSatisfactionTracker | No | Yes (per session) | No | No | No |
| AggregateSatisfactionMetricsRecorder | No | No | Yes (sliding window) | No | No |
| SatisfactionRegressionDetector | No | No | Via recorder | Yes | No |
| SatisfactionProxyDashboard | No | No | No | No | Yes |

**Best for production**: Track `avg_retry_rate` and `abandonment_rate` as the two most actionable leading indicators — both are observable without explicit feedback and correlate strongly with perceived quality. Alert when `avg_retry_rate` exceeds 0.20 in a one-hour window: this means one in five responses required the user to rephrase, which almost always indicates a prompt regression or model change. Use `SatisfactionRegressionDetector` on every deployment: a delta below -0.15 on `avg_satisfaction_score` should block a rollout the same way a failing integration test would.
