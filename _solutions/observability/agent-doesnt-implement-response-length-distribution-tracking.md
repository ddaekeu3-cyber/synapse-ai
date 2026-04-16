---
title: "Agent Doesn't Implement Response Length Distribution Tracking"
description: "Agents that do not track response length distributions cannot detect when model outputs are systematically too short (truncated or under-specified), too long (verbose or repetitive), or bimodal (collapsing into either terse errors or wall-of-text replies). Implement response length distribution tracking with percentile reporting, anomaly detection, and per-intent breakdown to drive prompt and model configuration improvements."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-response-length-distribution-tracking
tags: [response-length, distribution-tracking, output-quality, token-metrics, verbosity-detection, response-analytics]
symptoms:
  - "P1 responses are suspiciously short — model may be truncating or refusing silently"
  - "P99 responses are 20× median — model occasionally generates runaway verbose outputs"
  - "No data on whether length varies by task type or user segment"
  - "Prompt changes that affect verbosity go undetected until users complain"
  - "Cannot distinguish truncation errors from intentionally brief answers"
---

## Why This Happens

Response length is a lagging signal for several quality issues: a model hitting its max_tokens limit silently truncates; a poorly-tuned prompt causes verbose repetition; a safety refusal produces a one-sentence response where a paragraph was expected. Without tracking the full distribution — not just the mean — these patterns are invisible. Mean response length masks bimodal distributions. P1 length reveals truncation. P99 length reveals runaway verbosity. Bucketed histograms reveal whether the distribution is shifting after a prompt change. Per-intent breakdown shows whether code generation is consistently longer than Q&A, or whether a specific intent has an unusually flat distribution.

## Solution 1: Response Length Sample

```python
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ResponseLengthSample:
    response_id: str
    char_count: int
    token_count_estimate: int
    session_id: str = ""
    intent: str = ""            # e.g. "code_generation", "qa", "summarization"
    model: str = ""
    max_tokens_configured: Optional[int] = None
    was_truncated: bool = False  # True if char_count near max_tokens limit
    recorded_at: float = field(default_factory=time.time)

    @classmethod
    def from_text(
        cls,
        response_text: str,
        chars_per_token: float = 4.0,
        **kwargs,
    ) -> "ResponseLengthSample":
        char_count = len(response_text)
        token_estimate = int(char_count / chars_per_token)
        max_tokens = kwargs.get("max_tokens_configured")
        was_truncated = (
            max_tokens is not None and token_estimate >= int(max_tokens * 0.97)
        )
        return cls(
            char_count=char_count,
            token_count_estimate=token_estimate,
            was_truncated=was_truncated,
            **kwargs,
        )
```

## Solution 2: Length Distribution Recorder

```python
import math
import time
from collections import deque
from threading import Lock
from typing import Deque, Dict, List, Optional, Tuple


class ResponseLengthDistributionRecorder:
    """
    Accumulates response length samples and provides percentile queries,
    histogram bucketing, and truncation rate tracking.
    """

    def __init__(self, max_samples: int = 50000):
        self._max = max_samples
        self._samples: Deque[ResponseLengthSample] = deque()
        self._lock = Lock()

    def record(self, sample: ResponseLengthSample) -> None:
        with self._lock:
            self._samples.append(sample)
            if len(self._samples) > self._max:
                self._samples.popleft()

    def percentile(
        self,
        pct: float,
        window_seconds: float = 3600.0,
        intent: Optional[str] = None,
    ) -> Optional[float]:
        values = self._filtered_chars(window_seconds, intent)
        if not values:
            return None
        values.sort()
        idx = min(int(len(values) * pct / 100.0), len(values) - 1)
        return float(values[idx])

    def histogram(
        self,
        window_seconds: float = 3600.0,
        bucket_size: int = 500,
        intent: Optional[str] = None,
    ) -> Dict[str, int]:
        values = self._filtered_chars(window_seconds, intent)
        buckets: Dict[str, int] = {}
        for v in values:
            bucket_label = f"{(v // bucket_size) * bucket_size}-{(v // bucket_size + 1) * bucket_size}"
            buckets[bucket_label] = buckets.get(bucket_label, 0) + 1
        return dict(sorted(buckets.items(), key=lambda x: int(x[0].split("-")[0])))

    def truncation_rate(self, window_seconds: float = 3600.0) -> float:
        cutoff = time.time() - window_seconds
        with self._lock:
            recent = [s for s in self._samples if s.recorded_at >= cutoff]
        if not recent:
            return 0.0
        return round(sum(1 for s in recent if s.was_truncated) / len(recent), 4)

    def _filtered_chars(
        self,
        window_seconds: float,
        intent: Optional[str],
    ) -> List[int]:
        cutoff = time.time() - window_seconds
        with self._lock:
            return [
                s.char_count
                for s in self._samples
                if s.recorded_at >= cutoff and (intent is None or s.intent == intent)
            ]

    def summary(
        self,
        window_seconds: float = 3600.0,
        intent: Optional[str] = None,
    ) -> dict:
        values = self._filtered_chars(window_seconds, intent)
        if not values:
            return {"window_seconds": window_seconds, "samples": 0, "intent": intent}
        values_sorted = sorted(values)
        mean = sum(values_sorted) / len(values_sorted)
        return {
            "window_seconds": window_seconds,
            "intent": intent,
            "samples": len(values_sorted),
            "mean_chars": round(mean, 1),
            "p1_chars": values_sorted[max(0, int(len(values_sorted) * 0.01))],
            "p10_chars": values_sorted[max(0, int(len(values_sorted) * 0.10))],
            "p50_chars": values_sorted[int(len(values_sorted) * 0.50)],
            "p90_chars": values_sorted[min(len(values_sorted) - 1, int(len(values_sorted) * 0.90))],
            "p99_chars": values_sorted[min(len(values_sorted) - 1, int(len(values_sorted) * 0.99))],
            "truncation_rate": self.truncation_rate(window_seconds),
        }
```

## Solution 3: Per-Intent Length Profiler

```python
from typing import Dict, List, Optional, Set


class PerIntentLengthProfiler:
    """
    Breaks down response length statistics by intent label.
    Identifies intents with unusual length profiles — very short (refusals),
    very long (verbose), or high variance (inconsistent).
    """

    def __init__(self, recorder: ResponseLengthDistributionRecorder):
        self._recorder = recorder

    def known_intents(self, window_seconds: float = 3600.0) -> Set[str]:
        cutoff = __import__("time").time() - window_seconds
        with self._recorder._lock:
            return {
                s.intent
                for s in self._recorder._samples
                if s.recorded_at >= cutoff and s.intent
            }

    def profile_all(self, window_seconds: float = 3600.0) -> Dict[str, dict]:
        return {
            intent: self._recorder.summary(window_seconds, intent)
            for intent in self.known_intents(window_seconds)
        }

    def outlier_intents(
        self,
        window_seconds: float = 3600.0,
        short_threshold_chars: int = 100,
        long_threshold_chars: int = 8000,
    ) -> List[dict]:
        outliers = []
        for intent, profile in self.profile_all(window_seconds).items():
            if profile["samples"] < 10:
                continue
            if profile["p50_chars"] < short_threshold_chars:
                outliers.append({
                    "intent": intent,
                    "issue": "suspiciously_short",
                    "p50_chars": profile["p50_chars"],
                })
            if profile["p99_chars"] > long_threshold_chars:
                outliers.append({
                    "intent": intent,
                    "issue": "runaway_verbose",
                    "p99_chars": profile["p99_chars"],
                })
        return outliers
```

## Solution 4: Length Shift Detector

```python
import time
from typing import Optional


class ResponseLengthShiftDetector:
    """
    Compares response length P50 between a baseline window and a recent window.
    Detects prompt changes or model updates that shift the length distribution.
    """

    def __init__(
        self,
        recorder: ResponseLengthDistributionRecorder,
        shift_threshold_pct: float = 20.0,
    ):
        self._recorder = recorder
        self._threshold = shift_threshold_pct / 100.0

    def detect(
        self,
        baseline_window_seconds: float = 86400.0,
        recent_window_seconds: float = 3600.0,
        intent: Optional[str] = None,
    ) -> dict:
        baseline_p50 = self._recorder.percentile(50, baseline_window_seconds, intent)
        recent_p50 = self._recorder.percentile(50, recent_window_seconds, intent)

        if baseline_p50 is None or recent_p50 is None:
            return {
                "status": "insufficient_data",
                "baseline_p50": baseline_p50,
                "recent_p50": recent_p50,
                "intent": intent,
            }

        change = (recent_p50 - baseline_p50) / max(baseline_p50, 1)
        shifted = abs(change) > self._threshold

        return {
            "status": "shifted" if shifted else "stable",
            "direction": "longer" if change > 0 else "shorter",
            "baseline_p50_chars": baseline_p50,
            "recent_p50_chars": recent_p50,
            "change_pct": round(change * 100, 1),
            "threshold_pct": self._threshold * 100,
            "intent": intent,
            "shifted": shifted,
        }
```

## Solution 5: Length Anomaly Alerter

```python
import time
from typing import List, Optional


class ResponseLengthAnomalyAlerter:
    """
    Fires alerts when length metrics cross configured thresholds.
    Covers truncation rate spikes and P1/P99 boundary violations.
    """

    def __init__(
        self,
        recorder: ResponseLengthDistributionRecorder,
        max_truncation_rate: float = 0.05,
        min_p1_chars: int = 30,
        max_p99_chars: int = 16000,
    ):
        self._recorder = recorder
        self._max_trunc = max_truncation_rate
        self._min_p1 = min_p1_chars
        self._max_p99 = max_p99_chars
        self._alert_history: List[dict] = []

    def check(self, window_seconds: float = 1800.0) -> List[dict]:
        alerts = []
        summary = self._recorder.summary(window_seconds)

        if summary.get("samples", 0) < 20:
            return []

        if summary["truncation_rate"] > self._max_trunc:
            alerts.append({
                "type": "truncation_rate_high",
                "value": summary["truncation_rate"],
                "threshold": self._max_trunc,
                "ts": time.time(),
            })

        if summary["p1_chars"] < self._min_p1:
            alerts.append({
                "type": "p1_too_short",
                "value": summary["p1_chars"],
                "threshold": self._min_p1,
                "ts": time.time(),
            })

        if summary["p99_chars"] > self._max_p99:
            alerts.append({
                "type": "p99_runaway_verbose",
                "value": summary["p99_chars"],
                "threshold": self._max_p99,
                "ts": time.time(),
            })

        self._alert_history.extend(alerts)
        return alerts
```

## Solution 6: Response Length Dashboard

```python
import time


class ResponseLengthDashboard:
    """
    Combines distribution summary, per-intent profiles, shift detection,
    and active alerts into a single operational report.
    """

    def __init__(
        self,
        recorder: ResponseLengthDistributionRecorder,
        intent_profiler: PerIntentLengthProfiler,
        shift_detector: ResponseLengthShiftDetector,
        alerter: ResponseLengthAnomalyAlerter,
    ):
        self._recorder = recorder
        self._profiler = intent_profiler
        self._shift = shift_detector
        self._alerter = alerter

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "overall_1h": self._recorder.summary(window_seconds=3600.0),
            "histogram_1h": self._recorder.histogram(window_seconds=3600.0),
            "intent_profiles": self._profiler.profile_all(window_seconds=3600.0),
            "outlier_intents": self._profiler.outlier_intents(window_seconds=3600.0),
            "length_shift": self._shift.detect(),
            "active_alerts": self._alerter.check(window_seconds=1800.0),
        }
```

## Comparison

| Approach | Percentile Tracking | Per-Intent Breakdown | Shift Detection | Truncation Detection | Alerts |
|---|---|---|---|---|---|
| ResponseLengthDistributionRecorder | Yes (P1–P99) | Via filter | No | Yes (rate) | No |
| PerIntentLengthProfiler | Via recorder | Yes | No | No | No |
| ResponseLengthShiftDetector | Via recorder | Optional | Yes (P50 delta) | No | No |
| ResponseLengthAnomalyAlerter | Via recorder | No | No | Yes | Yes |
| ResponseLengthDashboard | No | No | No | No | Yes |

**Best for production**: Track `was_truncated` by comparing estimated token count against `max_tokens_configured * 0.97` — a truncation rate above 5% means the configured limit is too low for the task and users are receiving cut-off answers silently. Alert on P1 < 30 chars to catch silent refusals or model errors returning empty-ish strings. Use `ResponseLengthShiftDetector` after every prompt change: a >20% P50 shift in either direction signals a meaningful behavior change worth reviewing before wider rollout. Break down by intent — code generation should have higher P90 than Q&A; if they converge, the model may be ignoring task type.
