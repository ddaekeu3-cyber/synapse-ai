---
title: "Agent Doesn't Implement Anomaly Detection for Agent Output Patterns"
description: "Agents that emit outputs without monitoring for statistical anomalies will silently produce degraded responses — repetitive text, abnormal length distributions, unusual tool call sequences, or sudden shifts in response structure — with no alert until a user complains. Implement output pattern anomaly detection that tracks baselines for response length, vocabulary entropy, tool call frequency, and structural patterns, and alerts when current outputs deviate significantly from historical norms."
date: 2026-04-16
difficulty: advanced
category: observability
slug: agent-doesnt-implement-anomaly-detection-for-agent-output-patterns
tags: [anomaly-detection, output-monitoring, pattern-analysis, behavioral-drift, entropy-tracking, statistical-baseline]
symptoms:
  - "Agent starts producing repetitive or truncated responses and no alert fires"
  - "Tool call frequency spikes suddenly (loop or storm) but no anomaly is detected"
  - "Response length distribution shifts from median 400 tokens to median 20 tokens — silently"
  - "No baseline exists for what 'normal' agent output looks like"
  - "Prompt injection or jailbreak causes output format changes that go unnoticed for hours"
---

## Why This Happens

Agents are stateless by design — each response is generated independently. Without a monitoring layer that accumulates output statistics over time and compares each new output against a rolling baseline, there is no mechanism to detect when the distribution of outputs shifts. Length anomalies, vocabulary collapse (repetitive text), structural anomalies (missing JSON fields, unexpected tool calls), and frequency anomalies (sudden bursts of tool invocations) all manifest as statistical deviations from a learned baseline. Detecting them requires a sliding window of historical observations, a distance function for each dimension, and a threshold system that flags outliers.

## Solution 1: Output Feature Extractor

```python
import math
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class OutputFeatures:
    response_length_chars: int
    response_length_words: int
    sentence_count: int
    vocabulary_entropy: float       # Shannon entropy of word distribution
    unique_word_ratio: float        # unique words / total words
    tool_call_count: int
    has_json_block: bool
    has_code_block: bool
    repetition_ratio: float         # fraction of 4-grams that appear more than once
    avg_word_length: float
    metadata: Dict[str, float] = field(default_factory=dict)


class OutputFeatureExtractor:
    """
    Extracts statistical features from a single agent response text.
    """

    def extract(self, text: str, tool_call_count: int = 0) -> OutputFeatures:
        words = re.findall(r"\b\w+\b", text.lower())
        word_count = len(words)
        sentences = re.split(r"[.!?]+", text)
        sentence_count = max(1, sum(1 for s in sentences if s.strip()))

        entropy = self._entropy(words)
        unique_ratio = len(set(words)) / max(word_count, 1)
        repetition = self._repetition_ratio(words)
        avg_word_len = sum(len(w) for w in words) / max(word_count, 1)

        return OutputFeatures(
            response_length_chars=len(text),
            response_length_words=word_count,
            sentence_count=sentence_count,
            vocabulary_entropy=round(entropy, 4),
            unique_word_ratio=round(unique_ratio, 4),
            tool_call_count=tool_call_count,
            has_json_block=bool(re.search(r"```json", text, re.IGNORECASE)),
            has_code_block=bool(re.search(r"```", text)),
            repetition_ratio=round(repetition, 4),
            avg_word_length=round(avg_word_len, 4),
        )

    @staticmethod
    def _entropy(words: List[str]) -> float:
        if not words:
            return 0.0
        freq: dict = {}
        for w in words:
            freq[w] = freq.get(w, 0) + 1
        total = len(words)
        return -sum((c / total) * math.log2(c / total) for c in freq.values())

    @staticmethod
    def _repetition_ratio(words: List[str]) -> float:
        if len(words) < 4:
            return 0.0
        ngrams = [tuple(words[i:i+4]) for i in range(len(words) - 3)]
        if not ngrams:
            return 0.0
        seen: dict = {}
        for ng in ngrams:
            seen[ng] = seen.get(ng, 0) + 1
        repeated = sum(1 for c in seen.values() if c > 1)
        return repeated / len(ngrams)
```

## Solution 2: Rolling Baseline Tracker

```python
import time
from collections import deque
from threading import Lock
from typing import Deque, Optional, Tuple


class RollingBaselineTracker:
    """
    Maintains a sliding window of feature observations and computes
    mean and standard deviation for each numeric feature dimension.
    """

    def __init__(self, window_size: int = 200):
        self._window = window_size
        self._lock = Lock()
        self._observations: Deque[OutputFeatures] = deque(maxlen=window_size)

    def record(self, features: OutputFeatures) -> None:
        with self._lock:
            self._observations.append(features)

    def baseline(self) -> Optional[dict]:
        with self._lock:
            obs = list(self._observations)
        if len(obs) < 10:
            return None   # insufficient data

        def stats(values):
            n = len(values)
            mean = sum(values) / n
            variance = sum((v - mean) ** 2 for v in values) / n
            return round(mean, 4), round(variance ** 0.5, 4)

        return {
            "length_chars": stats([o.response_length_chars for o in obs]),
            "length_words": stats([o.response_length_words for o in obs]),
            "vocabulary_entropy": stats([o.vocabulary_entropy for o in obs]),
            "unique_word_ratio": stats([o.unique_word_ratio for o in obs]),
            "tool_call_count": stats([o.tool_call_count for o in obs]),
            "repetition_ratio": stats([o.repetition_ratio for o in obs]),
            "sample_count": len(obs),
        }
```

## Solution 3: Z-Score Anomaly Detector

```python
from typing import List, Optional, Tuple


class ZScoreAnomalyDetector:
    """
    Computes z-scores for each feature dimension against the rolling baseline.
    Flags features whose absolute z-score exceeds the threshold.
    """

    def __init__(self, z_threshold: float = 3.0):
        self._threshold = z_threshold

    def detect(
        self,
        features: OutputFeatures,
        baseline: dict,
    ) -> dict:
        anomalies = []
        scores = {}

        checks = [
            ("length_chars", features.response_length_chars),
            ("length_words", features.response_length_words),
            ("vocabulary_entropy", features.vocabulary_entropy),
            ("unique_word_ratio", features.unique_word_ratio),
            ("tool_call_count", features.tool_call_count),
            ("repetition_ratio", features.repetition_ratio),
        ]

        for dim_name, value in checks:
            if dim_name not in baseline:
                continue
            mean, std = baseline[dim_name]
            if std < 1e-6:
                continue   # no variance in baseline
            z = (value - mean) / std
            scores[dim_name] = round(z, 3)
            if abs(z) >= self._threshold:
                direction = "high" if z > 0 else "low"
                anomalies.append({
                    "dimension": dim_name,
                    "z_score": round(z, 3),
                    "direction": direction,
                    "observed": value,
                    "baseline_mean": mean,
                    "baseline_std": std,
                })

        return {
            "is_anomaly": len(anomalies) > 0,
            "anomaly_count": len(anomalies),
            "anomalies": anomalies,
            "z_scores": scores,
        }
```

## Solution 4: Structural Pattern Validator

```python
import re
from dataclasses import dataclass
from typing import List, Optional


@dataclass
class StructuralExpectation:
    """Encodes expected structural properties of agent responses in a given context."""
    min_length_chars: int = 10
    max_length_chars: int = 8000
    max_tool_calls: int = 20
    max_repetition_ratio: float = 0.30
    min_vocabulary_entropy: float = 1.5
    requires_json: bool = False
    forbidden_patterns: List[str] = None


class StructuralPatternValidator:
    """
    Validates an OutputFeatures record against hard structural expectations.
    Catches absolute violations that z-score detection might miss when the
    baseline itself has drifted.
    """

    def validate(
        self,
        features: OutputFeatures,
        expectation: StructuralExpectation,
    ) -> dict:
        violations = []

        if features.response_length_chars < expectation.min_length_chars:
            violations.append({
                "rule": "min_length",
                "observed": features.response_length_chars,
                "threshold": expectation.min_length_chars,
            })

        if features.response_length_chars > expectation.max_length_chars:
            violations.append({
                "rule": "max_length",
                "observed": features.response_length_chars,
                "threshold": expectation.max_length_chars,
            })

        if features.tool_call_count > expectation.max_tool_calls:
            violations.append({
                "rule": "max_tool_calls",
                "observed": features.tool_call_count,
                "threshold": expectation.max_tool_calls,
            })

        if features.repetition_ratio > expectation.max_repetition_ratio:
            violations.append({
                "rule": "max_repetition",
                "observed": features.repetition_ratio,
                "threshold": expectation.max_repetition_ratio,
            })

        if features.vocabulary_entropy < expectation.min_vocabulary_entropy:
            violations.append({
                "rule": "min_entropy",
                "observed": features.vocabulary_entropy,
                "threshold": expectation.min_vocabulary_entropy,
            })

        if expectation.requires_json and not features.has_json_block:
            violations.append({"rule": "requires_json_block", "observed": False})

        return {
            "valid": len(violations) == 0,
            "violation_count": len(violations),
            "violations": violations,
        }
```

## Solution 5: Output Anomaly Alert Manager

```python
import time
from typing import Callable, List, Optional


class OutputAnomalyAlertManager:
    """
    Combines z-score and structural anomaly signals into alert events.
    Applies cooldown to prevent alert storms on sustained degradation.
    """

    def __init__(
        self,
        alert_fn: Optional[Callable[[dict], None]] = None,
        cooldown_seconds: float = 60.0,
        min_anomaly_dimensions: int = 1,
    ):
        self._alert_fn = alert_fn or (lambda a: None)
        self._cooldown = cooldown_seconds
        self._min_dims = min_anomaly_dimensions
        self._last_alert_at: Optional[float] = None
        self._alert_count = 0

    def evaluate(
        self,
        zscore_result: dict,
        structural_result: dict,
        session_id: str = "",
    ) -> Optional[dict]:
        has_zscore_anomaly = (
            zscore_result.get("is_anomaly")
            and zscore_result.get("anomaly_count", 0) >= self._min_dims
        )
        has_structural_violation = not structural_result.get("valid", True)

        if not has_zscore_anomaly and not has_structural_violation:
            return None

        now = time.time()
        if self._last_alert_at and (now - self._last_alert_at) < self._cooldown:
            return None   # within cooldown

        alert = {
            "ts": now,
            "session_id": session_id,
            "zscore_anomalies": zscore_result.get("anomalies", []),
            "structural_violations": structural_result.get("violations", []),
            "z_scores": zscore_result.get("z_scores", {}),
            "alert_type": self._classify(zscore_result, structural_result),
        }
        self._last_alert_at = now
        self._alert_count += 1
        self._alert_fn(alert)
        return alert

    @staticmethod
    def _classify(zscore_result: dict, structural_result: dict) -> str:
        anomalies = [a["dimension"] for a in zscore_result.get("anomalies", [])]
        if "repetition_ratio" in anomalies or "vocabulary_entropy" in anomalies:
            return "repetition_or_entropy_drift"
        if "tool_call_count" in anomalies:
            return "tool_call_frequency_anomaly"
        if "length_chars" in anomalies or "length_words" in anomalies:
            return "length_distribution_shift"
        if not structural_result.get("valid", True):
            return "structural_violation"
        return "multi_dimensional_drift"
```

## Solution 6: Output Anomaly Detection Dashboard

```python
import time
from typing import List, Optional


class OutputAnomalyDetectionDashboard:
    """
    Accumulates anomaly detection results and surfaces drift trends,
    alert rates, and the most frequently anomalous dimensions.
    """

    def __init__(self, max_records: int = 2000):
        self._max = max_records
        self._results: List[dict] = []
        self._timestamps: List[float] = []

    def record(self, zscore_result: dict, structural_result: dict) -> None:
        if len(self._results) >= self._max:
            self._results.pop(0)
            self._timestamps.pop(0)
        self._results.append({
            "zscore": zscore_result,
            "structural": structural_result,
        })
        self._timestamps.append(time.time())

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [
            r for r, ts in zip(self._results, self._timestamps)
            if ts >= cutoff
        ]
        if not recent:
            return {"window_seconds": window_seconds, "samples": 0}

        total = len(recent)
        zscore_hits = sum(1 for r in recent if r["zscore"].get("is_anomaly"))
        structural_hits = sum(1 for r in recent if not r["structural"].get("valid", True))

        dim_counts: dict = {}
        for r in recent:
            for a in r["zscore"].get("anomalies", []):
                dim = a["dimension"]
                dim_counts[dim] = dim_counts.get(dim, 0) + 1

        return {
            "window_seconds": window_seconds,
            "samples": total,
            "zscore_anomaly_rate": round(zscore_hits / total, 4),
            "structural_violation_rate": round(structural_hits / total, 4),
            "top_anomalous_dimensions": sorted(
                dim_counts.items(), key=lambda kv: kv[1], reverse=True
            )[:5],
        }
```

## Comparison

| Approach | Feature Extraction | Statistical Baseline | Z-Score Detection | Hard Rule Validation | Alert Management | Dashboard |
|---|---|---|---|---|---|---|
| OutputFeatureExtractor | Yes (7 dims) | No | No | No | No | No |
| RollingBaselineTracker | No | Yes (sliding window) | No | No | No | No |
| ZScoreAnomalyDetector | No | Via tracker | Yes | No | No | No |
| StructuralPatternValidator | No | No | No | Yes (hard limits) | No | No |
| OutputAnomalyAlertManager | No | No | Via detector | Via validator | Yes (cooldown) | No |
| OutputAnomalyDetectionDashboard | No | No | No | No | No | Yes |

**Best for production**: Seed the baseline with at least 50 observations from a known-healthy session before enabling alerts — a cold baseline produces too many false positives. Set `z_threshold=3.0` for production alerting; lower thresholds (2.5) are appropriate in staging where you want early warning during load testing. The `repetition_ratio` dimension is the highest-signal indicator of output degradation: a spike above 0.25 almost always correlates with prompt injection, context poisoning, or a stuck generation loop. Set `cooldown_seconds=120` to suppress alert storms when the agent enters a sustained degraded state — the first alert is sufficient; subsequent identical alerts add noise without value.
