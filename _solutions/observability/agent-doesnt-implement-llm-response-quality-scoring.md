---
title: "Agent Doesn't Implement LLM Response Quality Scoring"
description: "Agents that emit LLM responses without quality scoring have no signal for detecting gradual degradation: responses become less coherent, less relevant, or shorter over time with no alert. Implement automated response quality scoring that measures coherence, relevance to the user query, completeness, and format compliance on every response, tracks score distributions over time, and alerts when quality metrics fall below baseline thresholds."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-llm-response-quality-scoring
tags: [response-quality, quality-scoring, coherence, relevance-scoring, output-monitoring, quality-drift]
symptoms:
  - "Response quality degraded after a prompt change but no metric caught it until user complaints"
  - "No automated signal for whether LLM responses are relevant to the user's query"
  - "Response length distribution shifts from verbose to terse with no quality alert"
  - "Format compliance (JSON, markdown structure) is never measured — violations go unnoticed"
  - "A/B testing prompt variants has no quantitative quality comparison — just subjective review"
---

## Why This Happens

LLM response quality is treated as a subjective property that only humans can evaluate. While human evaluation is the gold standard, automated quality proxies — heuristic and model-based — can catch large regressions instantly and cheaply. Without any automated quality scoring, regressions are discovered through user complaints, which arrive hours or days after deployment. A scoring layer that runs on every response provides continuous quality monitoring with no human cost, allowing teams to catch prompt regressions, model drift, and edge cases automatically.

## Solution 1: Quality Dimension

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class QualityDimension(str, Enum):
    RELEVANCE = "relevance"           # response addresses the query
    COHERENCE = "coherence"           # internal logical consistency
    COMPLETENESS = "completeness"     # all required elements present
    CONCISENESS = "conciseness"       # no excessive padding or repetition
    FORMAT_COMPLIANCE = "format_compliance"   # matches expected format
    GROUNDEDNESS = "groundedness"     # claims supported by provided context


@dataclass
class DimensionScore:
    dimension: QualityDimension
    score: float                      # 0.0 to 1.0
    rationale: str = ""               # brief explanation
    confidence: float = 1.0           # scorer confidence in this score

    def weighted(self, weight: float) -> float:
        return self.score * weight * self.confidence


@dataclass
class ResponseQualityReport:
    session_id: str
    query: str
    response: str
    dimension_scores: Dict[QualityDimension, DimensionScore]
    composite_score: float             # weighted average
    passed_threshold: bool
    scorer_version: str = "1.0"
    scored_at: float = field(default_factory=__import__("time").time)
    metadata: Dict[str, Any] = field(default_factory=dict)
```

## Solution 2: Heuristic Quality Scorers

```python
import math
import re
from typing import Set


class RelevanceScorer:
    """
    Estimates relevance by measuring token overlap between query and response.
    High overlap suggests the response addresses the query's topic.
    """

    def score(self, query: str, response: str) -> DimensionScore:
        def tokens(text: str) -> Set[str]:
            return set(re.findall(r"\b\w{3,}\b", text.lower()))

        query_tokens = tokens(query)
        response_tokens = tokens(response)

        if not query_tokens:
            return DimensionScore(QualityDimension.RELEVANCE, 0.5, "empty query")

        overlap = len(query_tokens & response_tokens)
        score = min(1.0, overlap / max(len(query_tokens) * 0.6, 1))

        return DimensionScore(
            QualityDimension.RELEVANCE,
            round(score, 4),
            f"token overlap: {overlap}/{len(query_tokens)} query terms",
        )


class CoherenceScorer:
    """
    Estimates coherence via sentence transition quality and vocabulary consistency.
    Incoherent responses tend to have high vocabulary variance and short sentences.
    """

    def score(self, response: str) -> DimensionScore:
        sentences = [s.strip() for s in re.split(r"[.!?]+", response) if len(s.strip()) > 10]
        if len(sentences) < 2:
            return DimensionScore(QualityDimension.COHERENCE, 0.6, "too short to assess")

        avg_len = sum(len(s.split()) for s in sentences) / len(sentences)
        # Very short avg sentence length signals fragmented output
        len_score = min(1.0, avg_len / 15.0)

        # Vocabulary reuse across sentences signals topical coherence
        all_words = [set(re.findall(r"\b\w{4,}\b", s.lower())) for s in sentences]
        if len(all_words) >= 2:
            overlaps = []
            for i in range(len(all_words) - 1):
                union = all_words[i] | all_words[i+1]
                inter = all_words[i] & all_words[i+1]
                overlaps.append(len(inter) / max(len(union), 1))
            vocab_score = sum(overlaps) / len(overlaps)
        else:
            vocab_score = 0.5

        score = round((len_score * 0.4 + vocab_score * 0.6), 4)
        return DimensionScore(
            QualityDimension.COHERENCE,
            min(1.0, score),
            f"avg sentence length={avg_len:.1f}, vocab continuity={vocab_score:.2f}",
        )


class ConcisenessScorer:
    """
    Penalizes repetitive content (high n-gram repetition) and excessive length.
    """

    def score(self, response: str, max_expected_chars: int = 3000) -> DimensionScore:
        words = re.findall(r"\b\w+\b", response.lower())
        if not words:
            return DimensionScore(QualityDimension.CONCISENESS, 0.0, "empty response")

        # N-gram repetition penalty
        if len(words) >= 4:
            ngrams = [tuple(words[i:i+4]) for i in range(len(words) - 3)]
            seen: dict = {}
            for ng in ngrams:
                seen[ng] = seen.get(ng, 0) + 1
            rep_ratio = sum(1 for c in seen.values() if c > 1) / len(ngrams)
        else:
            rep_ratio = 0.0

        # Length penalty
        length_penalty = min(1.0, len(response) / max(max_expected_chars, 1))
        length_score = 1.0 - max(0.0, length_penalty - 1.0)

        score = round((1.0 - rep_ratio) * 0.7 + length_score * 0.3, 4)
        return DimensionScore(
            QualityDimension.CONCISENESS,
            max(0.0, min(1.0, score)),
            f"repetition_ratio={rep_ratio:.3f}",
        )


class FormatComplianceScorer:
    """
    Checks whether the response matches an expected format (JSON, markdown, plain text).
    """

    def score(self, response: str, expected_format: str = "text") -> DimensionScore:
        if expected_format == "json":
            try:
                __import__("json").loads(response.strip())
                return DimensionScore(QualityDimension.FORMAT_COMPLIANCE, 1.0, "valid JSON")
            except Exception:
                # Check for partial JSON
                has_json = bool(re.search(r"\{.*\}", response, re.DOTALL))
                return DimensionScore(QualityDimension.FORMAT_COMPLIANCE, 0.3 if has_json else 0.0, "invalid JSON")

        if expected_format == "markdown":
            has_structure = bool(re.search(r"(#{1,6}\s|\*\*|\- |\d+\. )", response))
            score = 0.8 if has_structure else 0.4
            return DimensionScore(QualityDimension.FORMAT_COMPLIANCE, score, f"markdown={'detected' if has_structure else 'absent'}")

        return DimensionScore(QualityDimension.FORMAT_COMPLIANCE, 1.0, "no format constraint")
```

## Solution 3: Composite Quality Scorer

```python
import time
from typing import Dict, Optional


class CompositeQualityScorer:
    """
    Runs all dimension scorers and computes a weighted composite score.
    """

    DEFAULT_WEIGHTS = {
        QualityDimension.RELEVANCE: 0.35,
        QualityDimension.COHERENCE: 0.25,
        QualityDimension.CONCISENESS: 0.20,
        QualityDimension.FORMAT_COMPLIANCE: 0.20,
    }

    def __init__(
        self,
        weights: Optional[Dict[QualityDimension, float]] = None,
        passing_threshold: float = 0.65,
        expected_format: str = "text",
        max_expected_chars: int = 3000,
    ):
        self._weights = weights or self.DEFAULT_WEIGHTS
        self._threshold = passing_threshold
        self._format = expected_format
        self._max_chars = max_expected_chars
        self._relevance = RelevanceScorer()
        self._coherence = CoherenceScorer()
        self._conciseness = ConcisenessScorer()
        self._format_scorer = FormatComplianceScorer()

    def score(
        self,
        query: str,
        response: str,
        session_id: str = "",
    ) -> ResponseQualityReport:
        dim_scores = {
            QualityDimension.RELEVANCE: self._relevance.score(query, response),
            QualityDimension.COHERENCE: self._coherence.score(response),
            QualityDimension.CONCISENESS: self._conciseness.score(response, self._max_chars),
            QualityDimension.FORMAT_COMPLIANCE: self._format_scorer.score(response, self._format),
        }

        total_weight = sum(self._weights.get(d, 0) for d in dim_scores)
        composite = sum(
            dim_scores[d].weighted(self._weights.get(d, 0))
            for d in dim_scores
        ) / max(total_weight, 1e-9)

        return ResponseQualityReport(
            session_id=session_id,
            query=query,
            response=response,
            dimension_scores=dim_scores,
            composite_score=round(composite, 4),
            passed_threshold=composite >= self._threshold,
        )
```

## Solution 4: Quality Score Time Series

```python
import time
from collections import deque
from threading import Lock
from typing import Deque, Optional, Tuple


class QualityScoreTimeSeries:
    """
    Maintains a rolling window of composite quality scores for trend analysis.
    """

    def __init__(self, window_size: int = 5000):
        self._window = window_size
        self._series: Deque[Tuple[float, float, str]] = deque(maxlen=window_size)
        # (timestamp, composite_score, session_id)
        self._lock = Lock()

    def record(self, report: ResponseQualityReport) -> None:
        with self._lock:
            self._series.append((report.scored_at, report.composite_score, report.session_id))

    def percentile(self, pct: float, window_seconds: float = 3600.0) -> Optional[float]:
        cutoff = time.time() - window_seconds
        with self._lock:
            values = sorted(s for ts, s, _ in self._series if ts >= cutoff)
        if not values:
            return None
        idx = min(int(len(values) * pct / 100.0), len(values) - 1)
        return round(values[idx], 4)

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        with self._lock:
            recent = [(ts, s) for ts, s, _ in self._series if ts >= cutoff]
        if not recent:
            return {"window_seconds": window_seconds, "samples": 0}
        scores = [s for _, s in recent]
        return {
            "window_seconds": window_seconds,
            "samples": len(scores),
            "mean": round(sum(scores) / len(scores), 4),
            "p10": self.percentile(10, window_seconds),
            "p50": self.percentile(50, window_seconds),
            "p25": self.percentile(25, window_seconds),
            "below_threshold_rate": round(sum(1 for s in scores if s < 0.65) / len(scores), 4),
        }
```

## Solution 5: Quality Regression Detector

```python
import time
from typing import Optional


class QualityRegressionDetector:
    """
    Compares recent quality score distribution against a baseline window.
    Alerts when the mean score drops significantly.
    """

    def __init__(
        self,
        time_series: QualityScoreTimeSeries,
        regression_threshold_drop: float = 0.05,
        min_samples: int = 20,
    ):
        self._ts = time_series
        self._threshold = regression_threshold_drop
        self._min_samples = min_samples

    def check(
        self,
        baseline_window_seconds: float = 86400.0,
        recent_window_seconds: float = 3600.0,
    ) -> dict:
        baseline = self._ts.summary(baseline_window_seconds)
        recent = self._ts.summary(recent_window_seconds)

        if baseline["samples"] < self._min_samples or recent["samples"] < self._min_samples:
            return {"status": "insufficient_data", "baseline": baseline, "recent": recent}

        baseline_mean = baseline.get("mean", 0)
        recent_mean = recent.get("mean", 0)
        drop = baseline_mean - recent_mean
        regressed = drop >= self._threshold

        return {
            "status": "regression" if regressed else "ok",
            "baseline_mean": baseline_mean,
            "recent_mean": recent_mean,
            "score_drop": round(drop, 4),
            "threshold": self._threshold,
            "regressed": regressed,
        }
```

## Solution 6: Quality Dashboard

```python
import time


class ResponseQualityDashboard:
    """
    Combines quality time series, regression detection, and dimension
    breakdowns into a single operational quality report.
    """

    def __init__(
        self,
        scorer: CompositeQualityScorer,
        time_series: QualityScoreTimeSeries,
        regression_detector: QualityRegressionDetector,
    ):
        self._scorer = scorer
        self._ts = time_series
        self._regression = regression_detector

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "quality_summary_1h": self._ts.summary(3600.0),
            "quality_summary_24h": self._ts.summary(86400.0),
            "regression_check": self._regression.check(),
            "passing_threshold": self._scorer._threshold,
            "dimension_weights": {
                d.value: w for d, w in self._scorer._weights.items()
            },
        }
```

## Comparison

| Approach | Relevance | Coherence | Conciseness | Format Check | Trend Tracking | Regression Alert |
|---|---|---|---|---|---|---|
| RelevanceScorer | Yes (token overlap) | No | No | No | No | No |
| CoherenceScorer | No | Yes (sentence + vocab) | No | No | No | No |
| ConcisenessScorer | No | No | Yes (n-gram rep) | No | No | No |
| FormatComplianceScorer | No | No | No | Yes (JSON/MD) | No | No |
| CompositeQualityScorer | Via subscorers | Via subscorers | Via subscorers | Via subscorers | No | No |
| QualityRegressionDetector | No | No | No | No | Via time series | Yes |

**Best for production**: These heuristic scorers are fast and cheap but imperfect — use them as a regression signal, not an absolute quality gate. A composite score drop of 0.05 over 24 hours almost always correlates with a real quality change, even if individual scores have noise. Set `passing_threshold=0.65` as the minimum and alert on-call when more than 10% of responses fall below it in a one-hour window. For highest-stakes workflows (medical, legal, financial), supplement heuristic scoring with a dedicated LLM-as-judge call sampled at 5% of responses — this provides ground-truth calibration for the heuristic scores and catches dimension-specific regressions the heuristics miss.
