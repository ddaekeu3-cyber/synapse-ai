---
title: "Agent Doesn't Implement LLM Response Quality Scoring Pipeline"
description: "Agents that ship responses without automated quality scoring accumulate silent quality regressions: a prompt change degrades answer faithfulness, a model update changes tone, or a context truncation produces hallucinations — none of which triggers an alert because quality is never measured. Implement an automated quality scoring pipeline that evaluates every response on faithfulness, relevance, completeness, and groundedness, and alerts when scores drop below baseline."
date: 2026-04-16
difficulty: advanced
category: observability
slug: agent-doesnt-implement-llm-response-quality-scoring-pipeline
tags: [quality-scoring, faithfulness, hallucination-detection, response-evaluation, ragas, quality-regression]
symptoms:
  - "Prompt change degraded answer quality — discovered by user complaints, not monitoring"
  - "No automated measurement of response faithfulness to retrieved context"
  - "Quality metrics exist in eval runs but are never measured in production"
  - "Cannot detect when a model update causes hallucination rate to increase"
  - "No baseline quality score to compare against after deployments"
---

## Why This Happens

Quality evaluation is treated as an offline activity — run before deployment on a benchmark dataset. Production traffic contains the real distribution of queries, including edge cases not covered by the benchmark. Without online quality scoring, regressions in production are invisible until users complain. Online quality scoring applies lightweight automated evaluations to a sample of production responses, comparing scores against a rolling baseline to detect regressions in real time.

## Solution 1: Quality Dimension Definitions

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class QualityDimension(str, Enum):
    FAITHFULNESS = "faithfulness"         # response supported by context
    RELEVANCE = "relevance"               # response addresses the query
    COMPLETENESS = "completeness"         # response covers all asked aspects
    GROUNDEDNESS = "groundedness"         # claims traceable to source
    CONCISENESS = "conciseness"           # no unnecessary verbosity
    COHERENCE = "coherence"               # logical flow and consistency


@dataclass
class QualityScore:
    dimension: QualityDimension
    score: float                          # 0.0 – 1.0
    confidence: float = 1.0              # scorer's confidence in this score
    explanation: str = ""
    evidence: List[str] = field(default_factory=list)


@dataclass
class ResponseQualityReport:
    response_id: str
    query: str
    response: str
    context_used: List[str]
    scores: List[QualityScore]
    overall_score: float = 0.0
    flagged: bool = False
    flag_reasons: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.scores:
            self.overall_score = round(
                sum(s.score * s.confidence for s in self.scores)
                / sum(s.confidence for s in self.scores),
                4,
            )
```

## Solution 2: Faithfulness Scorer

```python
import re
from typing import List


class FaithfulnessScorer:
    """
    Estimates faithfulness by checking what fraction of factual claims
    in the response can be grounded in the provided context.
    Uses sentence-level overlap as a lightweight proxy for full NLI.
    """

    def score(
        self,
        response: str,
        context_chunks: List[str],
    ) -> QualityScore:
        sentences = self._split_sentences(response)
        if not sentences:
            return QualityScore(
                dimension=QualityDimension.FAITHFULNESS,
                score=1.0,
                explanation="empty response",
            )

        context_text = " ".join(context_chunks).lower()
        grounded = 0
        evidence = []

        for sentence in sentences:
            key_phrases = self._extract_key_phrases(sentence)
            if not key_phrases:
                grounded += 1
                continue
            matched = any(phrase in context_text for phrase in key_phrases)
            if matched:
                grounded += 1
                evidence.append(sentence[:80])

        score = grounded / len(sentences)
        return QualityScore(
            dimension=QualityDimension.FAITHFULNESS,
            score=round(score, 4),
            explanation=f"{grounded}/{len(sentences)} sentences grounded in context",
            evidence=evidence[:5],
        )

    @staticmethod
    def _split_sentences(text: str) -> List[str]:
        return [s.strip() for s in re.split(r"[.!?]\s+", text) if len(s.strip()) > 20]

    @staticmethod
    def _extract_key_phrases(sentence: str) -> List[str]:
        words = sentence.lower().split()
        if len(words) < 4:
            return []
        # Extract overlapping 3-grams as key phrases
        return [
            " ".join(words[i:i+3])
            for i in range(len(words) - 2)
        ]
```

## Solution 3: Relevance Scorer

```python
import math
from typing import List


class RelevanceScorer:
    """
    Scores response relevance to the query using TF-IDF cosine similarity
    as a lightweight proxy for semantic relevance.
    """

    @staticmethod
    def _tokenize(text: str) -> List[str]:
        import re
        return re.findall(r"\b[a-zA-Z]{3,}\b", text.lower())

    @staticmethod
    def _tf(tokens: List[str]) -> dict:
        counts: dict = {}
        for t in tokens:
            counts[t] = counts.get(t, 0) + 1
        total = max(len(tokens), 1)
        return {t: c / total for t, c in counts.items()}

    def score(self, query: str, response: str) -> QualityScore:
        query_tokens = self._tokenize(query)
        response_tokens = self._tokenize(response)

        if not query_tokens or not response_tokens:
            return QualityScore(
                dimension=QualityDimension.RELEVANCE,
                score=0.5,
                explanation="insufficient tokens for scoring",
            )

        query_tf = self._tf(query_tokens)
        response_tf = self._tf(response_tokens)

        vocab = set(query_tf) | set(response_tf)
        dot = sum(query_tf.get(t, 0) * response_tf.get(t, 0) for t in vocab)
        norm_q = math.sqrt(sum(v * v for v in query_tf.values()))
        norm_r = math.sqrt(sum(v * v for v in response_tf.values()))

        if norm_q == 0 or norm_r == 0:
            sim = 0.0
        else:
            sim = dot / (norm_q * norm_r)

        return QualityScore(
            dimension=QualityDimension.RELEVANCE,
            score=round(min(sim * 2.0, 1.0), 4),   # scale up — cosine is conservative
            explanation=f"query-response term overlap: {sim:.4f}",
        )
```

## Solution 4: Quality Scoring Pipeline

```python
import time
import uuid
from typing import Callable, List, Optional


class QualityScoringPipeline:
    """
    Applies multiple quality scorers to a response and produces
    a ResponseQualityReport. Scorers are run sequentially for
    simplicity; replace with asyncio.gather for high-throughput.
    """

    def __init__(
        self,
        scorers: List[Callable],
        alert_threshold: float = 0.70,
        sample_rate: float = 0.10,   # score 10% of production responses
    ):
        self._scorers = scorers
        self._threshold = alert_threshold
        self._sample_rate = sample_rate
        self._scored_count = 0
        self._skipped_count = 0

    def should_score(self) -> bool:
        import random
        return random.random() < self._sample_rate

    def score(
        self,
        query: str,
        response: str,
        context_chunks: List[str],
        response_id: Optional[str] = None,
    ) -> Optional[ResponseQualityReport]:
        if not self.should_score():
            self._skipped_count += 1
            return None

        self._scored_count += 1
        scores = []
        for scorer in self._scorers:
            score = scorer(query=query, response=response, context_chunks=context_chunks)
            if score:
                scores.append(score)

        report = ResponseQualityReport(
            response_id=response_id or uuid.uuid4().hex[:16],
            query=query[:200],
            response=response[:500],
            context_used=context_chunks[:3],
            scores=scores,
        )

        # Flag if below threshold
        if report.overall_score < self._threshold:
            report.flagged = True
            report.flag_reasons = [
                f"{s.dimension.value} score {s.score:.2f} below threshold {self._threshold}"
                for s in scores
                if s.score < self._threshold
            ]

        return report

    def stats(self) -> dict:
        return {
            "scored": self._scored_count,
            "skipped": self._skipped_count,
            "sample_rate": self._sample_rate,
        }
```

## Solution 5: Quality Score Baseline Tracker

```python
import time
from collections import deque
from threading import Lock
from typing import Deque, Dict, Optional, Tuple


class QualityScoreBaselineTracker:
    """
    Tracks quality score distributions per dimension over a rolling window.
    Detects regressions when recent scores fall significantly below baseline.
    """

    def __init__(self, window_seconds: float = 86400.0):
        self._window = window_seconds
        self._samples: Dict[str, Deque[Tuple[float, float]]] = {}
        # dimension -> deque of (ts, score)
        self._lock = Lock()

    def record(self, report: ResponseQualityReport) -> None:
        now = time.time()
        with self._lock:
            for score in report.scores:
                dim = score.dimension.value
                if dim not in self._samples:
                    self._samples[dim] = deque()
                self._samples[dim].append((now, score.score))
                self._trim(dim, now)

    def _trim(self, dim: str, now: float) -> None:
        cutoff = now - self._window
        q = self._samples[dim]
        while q and q[0][0] < cutoff:
            q.popleft()

    def baseline(self, dimension: str) -> Optional[float]:
        with self._lock:
            q = self._samples.get(dimension, deque())
            if len(q) < 20:
                return None
            scores = sorted(s for _, s in q)
            idx = int(len(scores) * 0.50)
            return round(scores[idx], 4)

    def regression_check(self, recent_window: float = 3600.0) -> List[dict]:
        now = time.time()
        alerts = []
        with self._lock:
            for dim, samples in self._samples.items():
                baseline_scores = sorted(s for _, s in samples)
                baseline = baseline_scores[int(len(baseline_scores) * 0.50)] if len(baseline_scores) >= 20 else None
                if baseline is None:
                    continue
                recent = [s for ts, s in samples if ts >= now - recent_window]
                if not recent:
                    continue
                recent_mean = sum(recent) / len(recent)
                if recent_mean < baseline * 0.90:   # 10% degradation threshold
                    alerts.append({
                        "dimension": dim,
                        "baseline": round(baseline, 4),
                        "recent_mean": round(recent_mean, 4),
                        "regression_pct": round((baseline - recent_mean) / baseline * 100, 1),
                    })
        return alerts
```

## Solution 6: Quality Score Dashboard

```python
import time


class QualityScoreDashboard:
    def __init__(
        self,
        pipeline: QualityScoringPipeline,
        baseline_tracker: QualityScoreBaselineTracker,
    ):
        self._pipeline = pipeline
        self._tracker = baseline_tracker

    def render(self) -> dict:
        baselines = {}
        for dim in [d.value for d in QualityDimension]:
            b = self._tracker.baseline(dim)
            if b is not None:
                baselines[dim] = b

        return {
            "generated_at": time.time(),
            "pipeline_stats": self._pipeline.stats(),
            "dimension_baselines": baselines,
            "regression_alerts": self._tracker.regression_check(3600.0),
        }
```

## Comparison

| Approach | Faithfulness Scoring | Relevance Scoring | Regression Detection | Sampling | Dashboard |
|---|---|---|---|---|---|
| FaithfulnessScorer | Yes (3-gram overlap) | No | No | No | No |
| RelevanceScorer | No | Yes (TF-IDF cosine) | No | No | No |
| QualityScoringPipeline | Via scorers | Via scorers | No | Yes (rate) | No |
| QualityScoreBaselineTracker | No | No | Yes (P50 baseline) | No | No |
| QualityScoreDashboard | No | No | No | No | Yes |

**Best for production**: Start with `sample_rate=0.10` (score 10% of production traffic) to keep scoring overhead low — at 1000 requests/hour, that is 100 scored responses, sufficient for statistical detection. Replace the heuristic scorers with LLM-as-judge calls (send query, response, and context to a judge model with a structured scoring rubric) for higher accuracy; use the heuristic scorers only when LLM-as-judge latency is unacceptable. Set regression alerts at 10% below the P50 baseline per dimension and send them to the team that owns the prompt — a faithfulness regression after a prompt change is almost certainly causally related.
