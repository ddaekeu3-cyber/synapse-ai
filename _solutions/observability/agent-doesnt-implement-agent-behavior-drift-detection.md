---
title: "Agent Doesn't Implement Agent Behavior Drift Detection"
description: "Agents that operate continuously without behavioral monitoring gradually drift from their baseline: response length distributions shift, tool selection patterns change, refusal rates creep up, and sentiment skews negative — all without triggering any alert. Implement behavior drift detection that continuously computes statistical features of agent outputs, compares them against a baseline window, and alerts when distributional shift exceeds a configurable threshold."
date: 2026-04-16
difficulty: advanced
category: observability
slug: agent-doesnt-implement-agent-behavior-drift-detection
tags: [behavior-drift, distributional-shift, behavioral-monitoring, output-analysis, anomaly-detection, llm-observability]
symptoms:
  - "Agent responses gradually became shorter over weeks but no alert fired"
  - "Tool selection frequency shifted significantly after a model update with no detection"
  - "Refusal rate doubled over 48 hours during a prompt injection campaign — unnoticed"
  - "No baseline of normal agent behavior to compare current behavior against"
  - "Model rollback decision made based on user complaints rather than behavioral metrics"
---

## Why This Happens

LLM behavior is statistically described, not deterministically defined. A single response tells you little; a distribution of responses reveals whether the agent is behaving as expected. Drift occurs when the current distribution diverges from the baseline — due to model updates, prompt changes, input distribution shifts, or adversarial pressure. Without continuous measurement of behavioral features (response length, tool call rates, refusal rates, sentiment) and statistical comparison against a baseline window, drift is invisible until it manifests as user complaints. Detection requires feature extraction from every response, a baseline snapshot, and a distance metric (KL divergence, population stability index, or simple mean shift) applied on a rolling basis.

## Solution 1: Behavioral Feature Extractor

```python
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class BehaviorSample:
    session_id: str
    request_id: str
    response_text: str
    tools_called: List[str] = field(default_factory=list)
    latency_ms: float = 0.0
    timestamp: float = 0.0

    # Extracted features — populated by BehavioralFeatureExtractor
    response_length: int = 0
    sentence_count: int = 0
    tool_call_count: int = 0
    unique_tools: int = 0
    is_refusal: bool = False
    has_apology: bool = False
    question_count: int = 0
    list_item_count: int = 0


class BehavioralFeatureExtractor:
    """
    Extracts numerical behavioral features from an agent response
    for use in drift detection.
    """

    REFUSAL_PATTERNS = re.compile(
        r"\b(cannot|can't|unable to|I'm sorry|I don't|not able to|I must decline|"
        r"against my|I apologize|I won't|outside my)\b",
        re.IGNORECASE,
    )
    APOLOGY_PATTERNS = re.compile(r"\b(sorry|apologize|apologies)\b", re.IGNORECASE)

    def extract(self, sample: BehaviorSample) -> BehaviorSample:
        text = sample.response_text
        sample.response_length = len(text)
        sample.sentence_count = len(re.split(r"[.!?]+", text))
        sample.tool_call_count = len(sample.tools_called)
        sample.unique_tools = len(set(sample.tools_called))
        sample.is_refusal = bool(self.REFUSAL_PATTERNS.search(text))
        sample.has_apology = bool(self.APOLOGY_PATTERNS.search(text))
        sample.question_count = text.count("?")
        sample.list_item_count = len(re.findall(r"^\s*[-*•]\s", text, re.MULTILINE))
        return sample

    def to_feature_vector(self, sample: BehaviorSample) -> Dict[str, float]:
        return {
            "response_length": float(sample.response_length),
            "sentence_count": float(sample.sentence_count),
            "tool_call_count": float(sample.tool_call_count),
            "unique_tools": float(sample.unique_tools),
            "is_refusal": 1.0 if sample.is_refusal else 0.0,
            "has_apology": 1.0 if sample.has_apology else 0.0,
            "question_count": float(sample.question_count),
            "list_item_count": float(sample.list_item_count),
        }
```

## Solution 2: Behavioral Baseline Snapshot

```python
import math
import time
from collections import deque
from threading import Lock
from typing import Deque, Dict, List, Optional, Tuple


class BehavioralBaselineSnapshot:
    """
    Computes mean and standard deviation for each behavioral feature
    over a sliding window of samples.
    """

    def __init__(self, window_size: int = 500):
        self._window = window_size
        self._samples: Deque[Dict[str, float]] = deque(maxlen=window_size)
        self._lock = Lock()
        self._snapshot_at: Optional[float] = None

    def add(self, features: Dict[str, float]) -> None:
        with self._lock:
            self._samples.append(features)

    def snapshot(self) -> Dict[str, Tuple[float, float]]:
        """Returns {feature: (mean, std)} for the current window."""
        with self._lock:
            if not self._samples:
                return {}
            keys = list(self._samples[0].keys())
            stats = {}
            for key in keys:
                values = [s[key] for s in self._samples if key in s]
                if not values:
                    continue
                mean = sum(values) / len(values)
                variance = sum((v - mean) ** 2 for v in values) / max(len(values) - 1, 1)
                stats[key] = (round(mean, 4), round(math.sqrt(variance), 4))
            self._snapshot_at = time.time()
            return stats

    def sample_count(self) -> int:
        with self._lock:
            return len(self._samples)
```

## Solution 3: Drift Detector

```python
import math
import time
from typing import Dict, List, Optional, Tuple


@dataclass
class DriftReport:
    feature: str
    baseline_mean: float
    baseline_std: float
    current_mean: float
    z_score: float
    drifted: bool
    drift_direction: str   # "up" | "down" | "none"


class ZScoreDriftDetector:
    """
    Detects behavioral drift by computing the Z-score of the current
    window mean against the baseline mean and std.
    A Z-score above the threshold signals statistically significant drift.
    """

    def __init__(
        self,
        z_threshold: float = 3.0,
        current_window_size: int = 100,
    ):
        self._z_threshold = z_threshold
        self._current_window_size = current_window_size
        self._current: List[Dict[str, float]] = []

    def add_current(self, features: Dict[str, float]) -> None:
        self._current.append(features)
        if len(self._current) > self._current_window_size:
            self._current.pop(0)

    def detect(
        self,
        baseline: Dict[str, Tuple[float, float]],
    ) -> List[DriftReport]:
        if not self._current or not baseline:
            return []

        reports = []
        keys = list(baseline.keys())
        for key in keys:
            values = [s[key] for s in self._current if key in s]
            if not values:
                continue
            current_mean = sum(values) / len(values)
            b_mean, b_std = baseline[key]
            if b_std == 0:
                continue
            z = (current_mean - b_mean) / b_std
            drifted = abs(z) >= self._z_threshold
            reports.append(DriftReport(
                feature=key,
                baseline_mean=b_mean,
                baseline_std=b_std,
                current_mean=round(current_mean, 4),
                z_score=round(z, 3),
                drifted=drifted,
                drift_direction="up" if z > 0 else "down" if z < 0 else "none",
            ))
        return reports
```

## Solution 4: Continuous Drift Monitor

```python
import time
from threading import Lock
from typing import Callable, List, Optional


class ContinuousBehaviorDriftMonitor:
    """
    Maintains a rolling baseline and a current window.
    On each new sample, updates both windows and checks for drift.
    Fires an alert callback when drift is detected.
    """

    def __init__(
        self,
        extractor: BehavioralFeatureExtractor,
        baseline: BehavioralBaselineSnapshot,
        detector: ZScoreDriftDetector,
        alert_fn: Optional[Callable[[List[DriftReport]], None]] = None,
        baseline_min_samples: int = 200,
        check_every_n: int = 10,
    ):
        self._extractor = extractor
        self._baseline = baseline
        self._detector = detector
        self._alert_fn = alert_fn or self._default_alert
        self._baseline_min = baseline_min_samples
        self._check_every = check_every_n
        self._sample_count = 0
        self._last_drift_reports: List[DriftReport] = []
        self._lock = Lock()

    @staticmethod
    def _default_alert(reports: List[DriftReport]) -> None:
        drifted = [r for r in reports if r.drifted]
        if drifted:
            import json
            print(json.dumps({
                "event": "behavior_drift_detected",
                "drifted_features": [
                    {"feature": r.feature, "z_score": r.z_score, "direction": r.drift_direction}
                    for r in drifted
                ],
            }))

    def observe(self, sample: BehaviorSample) -> Optional[List[DriftReport]]:
        enriched = self._extractor.extract(sample)
        features = self._extractor.to_feature_vector(enriched)

        with self._lock:
            self._baseline.add(features)
            self._detector.add_current(features)
            self._sample_count += 1

            if (
                self._sample_count % self._check_every == 0
                and self._baseline.sample_count() >= self._baseline_min
            ):
                snapshot = self._baseline.snapshot()
                reports = self._detector.detect(snapshot)
                self._last_drift_reports = reports
                drifted = [r for r in reports if r.drifted]
                if drifted:
                    self._alert_fn(reports)
                return reports
        return None

    def last_reports(self) -> List[DriftReport]:
        with self._lock:
            return list(self._last_drift_reports)
```

## Solution 5: Drift Event Store

```python
import time
from typing import List


class DriftEventStore:
    """
    Persists drift detection events for trend analysis and post-incident review.
    """

    def __init__(self, max_events: int = 5000):
        self._max = max_events
        self._events: List[dict] = []

    def record(self, reports: List[DriftReport]) -> None:
        drifted = [r for r in reports if r.drifted]
        if not drifted:
            return
        if len(self._events) >= self._max:
            self._events.pop(0)
        self._events.append({
            "ts": time.time(),
            "drifted_features": [
                {
                    "feature": r.feature,
                    "z_score": r.z_score,
                    "direction": r.drift_direction,
                    "baseline_mean": r.baseline_mean,
                    "current_mean": r.current_mean,
                }
                for r in drifted
            ],
        })

    def recent(self, window_seconds: float = 3600.0) -> List[dict]:
        cutoff = time.time() - window_seconds
        return [e for e in self._events if e["ts"] >= cutoff]

    def feature_drift_rate(
        self, feature: str, window_seconds: float = 3600.0
    ) -> float:
        recent = self.recent(window_seconds)
        if not recent:
            return 0.0
        hits = sum(
            1 for e in recent
            if any(f["feature"] == feature for f in e["drifted_features"])
        )
        return round(hits / len(recent), 4)
```

## Solution 6: Behavior Drift Dashboard

```python
import time
from typing import List


class BehaviorDriftDashboard:
    """
    Combines monitor status, recent drift reports, and event store
    trends into a single operational view for on-call engineers.
    """

    def __init__(
        self,
        monitor: ContinuousBehaviorDriftMonitor,
        event_store: DriftEventStore,
        tracked_features: List[str] = None,
    ):
        self._monitor = monitor
        self._events = event_store
        self._features = tracked_features or [
            "response_length", "is_refusal", "tool_call_count",
            "has_apology", "sentence_count",
        ]

    def render(self) -> dict:
        recent_events = self._events.recent(3600.0)
        last_reports = self._monitor.last_reports()
        currently_drifted = [r.feature for r in last_reports if r.drifted]

        return {
            "generated_at": time.time(),
            "currently_drifted_features": currently_drifted,
            "alert": len(currently_drifted) > 0,
            "last_reports": [
                {
                    "feature": r.feature,
                    "z_score": r.z_score,
                    "direction": r.drift_direction,
                    "baseline_mean": r.baseline_mean,
                    "current_mean": r.current_mean,
                    "drifted": r.drifted,
                }
                for r in last_reports
            ],
            "drift_rates_1h": {
                feature: self._events.feature_drift_rate(feature, 3600.0)
                for feature in self._features
            },
            "total_drift_events_1h": len(recent_events),
        }
```

## Comparison

| Approach | Feature Extraction | Baseline Tracking | Drift Detection | Event History | Dashboard |
|---|---|---|---|---|---|
| BehavioralFeatureExtractor | Yes (8 features) | No | No | No | No |
| BehavioralBaselineSnapshot | No | Yes (rolling window) | No | No | No |
| ZScoreDriftDetector | No | No | Yes (Z-score) | No | No |
| ContinuousBehaviorDriftMonitor | Via extractor | Via baseline | Via detector | No | No |
| DriftEventStore | No | No | No | Yes | No |
| BehaviorDriftDashboard | No | No | No | Via store | Yes |

**Best for production**: Set `baseline_min_samples=200` before enabling drift alerts — alerting on a small baseline produces false positives. Use a Z-score threshold of 3.0 for production (flags only 0.3% of samples under normal distribution) and 2.0 for staging where false positives are acceptable. Track `is_refusal` drift with highest priority — a sudden increase signals adversarial input or a misconfigured system prompt. Track `response_length` drift as an early indicator of model substitution or context window saturation. Run `DriftEventStore.feature_drift_rate()` daily to distinguish persistent drift (structural change) from transient spikes (load anomaly).
