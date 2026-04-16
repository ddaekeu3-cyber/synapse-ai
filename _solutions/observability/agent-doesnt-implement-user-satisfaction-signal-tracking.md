---
title: "Agent Doesn't Implement User Satisfaction Signal Tracking"
description: "Agents that only track technical metrics (latency, error rates, token counts) have no visibility into whether users are actually satisfied with responses — low latency and zero errors do not mean users got what they needed. Implement user satisfaction signal tracking that collects explicit feedback, infers implicit signals from behavioral patterns, correlates satisfaction with technical metrics, and alerts when satisfaction drops across a cohort."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-user-satisfaction-signal-tracking
tags: [user-satisfaction, feedback-tracking, implicit-signals, csat, behavioral-signals, quality-metrics]
symptoms:
  - "Error rate is 0% but users are abandoning sessions after the first response"
  - "No mechanism to distinguish 'technically correct' from 'actually helpful' responses"
  - "Thumbs-up/thumbs-down feedback exists in the UI but is never logged or analyzed"
  - "Cannot correlate model version changes with changes in user satisfaction"
  - "No alert when satisfaction drops after a prompt change is deployed"
---

## Why This Happens

Technical observability (latency, errors, token usage) measures whether the agent is functioning, not whether it is useful. A response that is factually wrong but delivered in 200ms with zero errors will look healthy in every technical dashboard. User satisfaction tracking requires a separate signal layer: explicit ratings when users provide them, implicit signals inferred from behavior (follow-up questions, session abandonment, regeneration requests), and aggregation logic that separates signal from noise.

## Solution 1: Satisfaction Signal Types

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional
import time


class SignalType(str, Enum):
    EXPLICIT_THUMBS_UP = "thumbs_up"
    EXPLICIT_THUMBS_DOWN = "thumbs_down"
    EXPLICIT_RATING = "rating"          # 1–5 numeric
    IMPLICIT_REGENERATE = "regenerate"  # user hit "regenerate"
    IMPLICIT_ABANDON = "abandon"        # session ended after 1 exchange
    IMPLICIT_FOLLOW_UP_CLARIFY = "clarify"  # user asked clarifying question
    IMPLICIT_COPY = "copy"              # user copied response text
    IMPLICIT_SHARE = "share"            # user shared response
    IMPLICIT_CONTINUE = "continue"      # user continued productively


class SignalSentiment(str, Enum):
    POSITIVE = "positive"
    NEGATIVE = "negative"
    NEUTRAL = "neutral"


SIGNAL_SENTIMENTS = {
    SignalType.EXPLICIT_THUMBS_UP: SignalSentiment.POSITIVE,
    SignalType.EXPLICIT_THUMBS_DOWN: SignalSentiment.NEGATIVE,
    SignalType.IMPLICIT_REGENERATE: SignalSentiment.NEGATIVE,
    SignalType.IMPLICIT_ABANDON: SignalSentiment.NEGATIVE,
    SignalType.IMPLICIT_FOLLOW_UP_CLARIFY: SignalSentiment.NEGATIVE,
    SignalType.IMPLICIT_COPY: SignalSentiment.POSITIVE,
    SignalType.IMPLICIT_SHARE: SignalSentiment.POSITIVE,
    SignalType.IMPLICIT_CONTINUE: SignalSentiment.POSITIVE,
    SignalType.EXPLICIT_RATING: SignalSentiment.NEUTRAL,  # determined by value
}


@dataclass
class SatisfactionSignal:
    session_id: str
    turn_number: int
    signal_type: SignalType
    sentiment: SignalSentiment
    rating_value: Optional[float] = None   # for EXPLICIT_RATING: 1.0–5.0
    model: Optional[str] = None
    user_id: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    recorded_at: float = field(default_factory=time.time)

    def score(self) -> float:
        """Normalized satisfaction score 0.0–1.0."""
        if self.signal_type == SignalType.EXPLICIT_RATING and self.rating_value:
            return (self.rating_value - 1.0) / 4.0
        if self.sentiment == SignalSentiment.POSITIVE:
            return 1.0
        if self.sentiment == SignalSentiment.NEGATIVE:
            return 0.0
        return 0.5
```

## Solution 2: Signal Recorder

```python
from collections import deque
from typing import Deque, Dict, List, Optional


class SatisfactionSignalRecorder:
    """
    Records satisfaction signals with a rolling window per session.
    Provides per-session and fleet-wide aggregation.
    """

    def __init__(
        self,
        window_size: int = 10_000,
        session_history_size: int = 50,
    ) -> None:
        self._window_size = window_size
        self._global: Deque[SatisfactionSignal] = deque(maxlen=window_size)
        self._by_session: Dict[str, Deque[SatisfactionSignal]] = {}
        self._session_history = session_history_size

    def record(self, signal: SatisfactionSignal) -> None:
        self._global.append(signal)
        if signal.session_id not in self._by_session:
            self._by_session[signal.session_id] = deque(maxlen=self._session_history)
        self._by_session[signal.session_id].append(signal)

    def for_session(self, session_id: str) -> List[SatisfactionSignal]:
        return list(self._by_session.get(session_id, []))

    def recent(self, limit: int = 1000) -> List[SatisfactionSignal]:
        signals = list(self._global)
        return signals[-limit:]

    def since(self, timestamp: float) -> List[SatisfactionSignal]:
        return [s for s in self._global if s.recorded_at >= timestamp]
```

## Solution 3: Satisfaction Score Aggregator

```python
import time
from collections import defaultdict
from typing import Dict, List, Optional, Tuple


class SatisfactionScoreAggregator:
    """
    Computes satisfaction rates, signal distributions, and per-model
    breakdowns from raw signals. Separates explicit from implicit signals.
    """

    def __init__(self, recorder: SatisfactionSignalRecorder) -> None:
        self._recorder = recorder

    def _compute_rate(self, signals: List[SatisfactionSignal]) -> Tuple[float, int]:
        """Returns (satisfaction_rate 0–1, sample_count)."""
        if not signals:
            return 0.0, 0
        weighted_score = sum(s.score() for s in signals)
        return round(weighted_score / len(signals), 4), len(signals)

    def aggregate(
        self,
        window_seconds: float = 3600.0,
        min_samples: int = 10,
    ) -> dict:
        since = time.time() - window_seconds
        signals = self._recorder.since(since)

        explicit = [s for s in signals if "explicit" in s.signal_type.value]
        implicit = [s for s in signals if "implicit" in s.signal_type.value]

        explicit_rate, explicit_n = self._compute_rate(explicit)
        implicit_rate, implicit_n = self._compute_rate(implicit)
        overall_rate, overall_n = self._compute_rate(signals)

        by_type: Dict[str, int] = defaultdict(int)
        for s in signals:
            by_type[s.signal_type.value] += 1

        by_model: Dict[str, list] = defaultdict(list)
        for s in signals:
            if s.model:
                by_model[s.model].append(s)
        model_rates = {
            model: self._compute_rate(sigs)
            for model, sigs in by_model.items()
            if len(sigs) >= min_samples
        }

        return {
            "window_seconds": window_seconds,
            "total_signals": overall_n,
            "overall_satisfaction_rate": overall_rate,
            "explicit": {"rate": explicit_rate, "count": explicit_n},
            "implicit": {"rate": implicit_rate, "count": implicit_n},
            "by_signal_type": dict(by_type),
            "by_model": {
                model: {"rate": rate, "count": count}
                for model, (rate, count) in model_rates.items()
            },
        }
```

## Solution 4: Session Satisfaction Analyzer

```python
from typing import List, Optional


class SessionSatisfactionAnalyzer:
    """
    Analyzes a single session's signal history to produce a
    per-session satisfaction assessment.
    """

    def __init__(self, recorder: SatisfactionSignalRecorder) -> None:
        self._recorder = recorder

    def analyze(self, session_id: str) -> dict:
        signals = self._recorder.for_session(session_id)
        if not signals:
            return {"session_id": session_id, "status": "no_signals"}

        positive = sum(1 for s in signals if s.sentiment == SignalSentiment.POSITIVE)
        negative = sum(1 for s in signals if s.sentiment == SignalSentiment.NEGATIVE)
        regenerations = sum(1 for s in signals if s.signal_type == SignalType.IMPLICIT_REGENERATE)
        abandoned = any(s.signal_type == SignalType.IMPLICIT_ABANDON for s in signals)
        explicit_down = any(s.signal_type == SignalType.EXPLICIT_THUMBS_DOWN for s in signals)

        overall_score = round(sum(s.score() for s in signals) / len(signals), 4)

        status = "satisfied"
        if explicit_down or abandoned:
            status = "unsatisfied"
        elif regenerations >= 2:
            status = "struggling"
        elif overall_score < 0.40:
            status = "at_risk"

        return {
            "session_id": session_id,
            "status": status,
            "overall_score": overall_score,
            "signal_count": len(signals),
            "positive_signals": positive,
            "negative_signals": negative,
            "regenerations": regenerations,
            "abandoned": abandoned,
        }
```

## Solution 5: Satisfaction Drop Alert Manager

```python
import time
from typing import Callable, List, Optional


class SatisfactionDropAlertManager:
    """
    Fires alerts when satisfaction rate drops below threshold
    or when a sudden decline is detected relative to baseline.
    """

    def __init__(
        self,
        aggregator: SatisfactionScoreAggregator,
        warning_rate: float = 0.65,
        critical_rate: float = 0.50,
        baseline_window_seconds: float = 86400.0,
        current_window_seconds: float = 3600.0,
        drop_alert_threshold: float = 0.10,
        handler: Optional[Callable[[dict], None]] = None,
        cooldown_seconds: float = 1800.0,
    ) -> None:
        self._aggregator = aggregator
        self._warning = warning_rate
        self._critical = critical_rate
        self._baseline_window = baseline_window_seconds
        self._current_window = current_window_seconds
        self._drop_threshold = drop_alert_threshold
        self._handler = handler
        self._cooldown = cooldown_seconds
        self._last_fired: Dict[str, float] = {}

    def _can_fire(self, key: str) -> bool:
        last = self._last_fired.get(key, 0.0)
        if time.time() - last >= self._cooldown:
            self._last_fired[key] = time.time()
            return True
        return False

    def check(self) -> List[dict]:
        current = self._aggregator.aggregate(self._current_window)
        baseline = self._aggregator.aggregate(self._baseline_window)
        alerts = []

        rate = current["overall_satisfaction_rate"]
        base_rate = baseline["overall_satisfaction_rate"]
        drop = base_rate - rate

        if rate <= self._critical and self._can_fire("critical"):
            alerts.append({
                "type": "satisfaction_critical",
                "current_rate": rate,
                "threshold": self._critical,
                "severity": "critical",
                "message": f"User satisfaction at {rate*100:.1f}% — critical threshold breached",
            })
        elif rate <= self._warning and self._can_fire("warning"):
            alerts.append({
                "type": "satisfaction_warning",
                "current_rate": rate,
                "threshold": self._warning,
                "severity": "warning",
            })

        if drop >= self._drop_threshold and current["total_signals"] >= 20:
            if self._can_fire("drop"):
                alerts.append({
                    "type": "satisfaction_drop",
                    "drop": round(drop, 4),
                    "current_rate": rate,
                    "baseline_rate": base_rate,
                    "severity": "warning",
                    "message": (
                        f"Satisfaction dropped {drop*100:.1f}pp from baseline "
                        f"({base_rate*100:.1f}% → {rate*100:.1f}%)"
                    ),
                })

        for alert in alerts:
            if self._handler:
                try:
                    self._handler(alert)
                except Exception:
                    pass

        return alerts
```

## Solution 6: User Satisfaction Dashboard

```python
import time


class UserSatisfactionDashboard:
    """
    Combines signal aggregation, per-model breakdown, and alerts
    into a single user quality operational view.
    """

    def __init__(
        self,
        recorder: SatisfactionSignalRecorder,
        aggregator: SatisfactionScoreAggregator,
        alert_manager: SatisfactionDropAlertManager,
    ) -> None:
        self._recorder = recorder
        self._aggregator = aggregator
        self._alerts = alert_manager

    def render(self) -> dict:
        hourly = self._aggregator.aggregate(3600.0)
        daily = self._aggregator.aggregate(86400.0)
        alerts = self._alerts.check()

        return {
            "generated_at": time.time(),
            "satisfaction": {
                "last_hour": {
                    "rate_pct": round(hourly["overall_satisfaction_rate"] * 100, 1),
                    "signals": hourly["total_signals"],
                    "explicit_rate_pct": round(hourly["explicit"]["rate"] * 100, 1),
                },
                "last_day": {
                    "rate_pct": round(daily["overall_satisfaction_rate"] * 100, 1),
                    "signals": daily["total_signals"],
                },
            },
            "by_signal_type": hourly["by_signal_type"],
            "by_model": hourly["by_model"],
            "active_alerts": alerts,
        }
```

## Comparison

| Approach | Explicit Signals | Implicit Signals | Per-Session Analysis | Drop Detection | Dashboard |
|---|---|---|---|---|---|
| SatisfactionSignalRecorder | Yes | Yes | No | No | No |
| SatisfactionScoreAggregator | Yes | Yes | No | No | No |
| SessionSatisfactionAnalyzer | No | No | Yes | No | No |
| SatisfactionDropAlertManager | No | No | No | Yes (vs baseline) | No |
| UserSatisfactionDashboard | No | No | No | Via manager | Yes |

**Best for production**: Weight explicit thumbs-down signals 3× more heavily than implicit signals — a user explicitly expressing dissatisfaction is a much stronger signal than abandonment (which has many causes). Set a minimum of 20 signals before enabling drop alerts to avoid false positives from statistical noise in low-traffic periods. Correlate satisfaction drops with deployment events: if satisfaction drops 10pp within 2 hours of a prompt change, that change is the likely cause. Track satisfaction separately per model version to detect silent quality regressions when providers update their models.
