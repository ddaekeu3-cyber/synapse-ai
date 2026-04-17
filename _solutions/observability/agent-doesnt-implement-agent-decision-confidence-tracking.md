---
title: "Agent Doesn't Implement Agent Decision Confidence Tracking"
description: "Agents that produce outputs without any confidence signal make it impossible to distinguish high-certainty answers from speculative ones — both are presented identically to users and downstream systems. Implement decision confidence tracking that extracts or estimates confidence from LLM outputs, classifies decisions by confidence tier, surfaces low-confidence decisions for human review, and tracks confidence distributions over time."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-agent-decision-confidence-tracking
tags: [confidence-tracking, decision-quality, human-review, uncertainty-quantification, llm-calibration, output-classification]
symptoms:
  - "Agent presents speculative answers with the same confidence as factual ones"
  - "No mechanism to route low-confidence decisions for human review"
  - "Cannot measure whether agent confidence correlates with actual accuracy"
  - "Downstream systems treat all agent outputs as equally reliable"
  - "No record of which decisions were uncertain — post-incident analysis is blind"
---

## Why This Happens

LLMs do not natively expose a calibrated confidence score per response. Some APIs provide log probabilities, but most production deployments suppress them. Without an explicit confidence signal, agents must extract confidence from the response text (hedging phrases, uncertainty markers) or prompt the model to self-report confidence as a structured field. Tracking these signals over time reveals whether confidence correlates with correctness and which topics consistently produce low-confidence outputs.

## Solution 1: Confidence Signal Extractor

```python
import re
from dataclasses import dataclass
from enum import Enum
from typing import List, Optional, Tuple


class ConfidenceTier(str, Enum):
    HIGH = "high"           # >= 0.80
    MEDIUM = "medium"       # >= 0.50
    LOW = "low"             # >= 0.25
    VERY_LOW = "very_low"   # < 0.25


@dataclass
class ConfidenceSignal:
    score: float                    # 0.0 – 1.0
    tier: ConfidenceTier
    method: str                     # "self_report" | "hedge_detection" | "log_prob" | "default"
    raw_value: Optional[str] = None
    hedge_phrases: List[str] = None

    def __post_init__(self):
        if self.hedge_phrases is None:
            self.hedge_phrases = []

    @staticmethod
    def from_score(score: float, method: str, **kwargs) -> "ConfidenceSignal":
        if score >= 0.80:
            tier = ConfidenceTier.HIGH
        elif score >= 0.50:
            tier = ConfidenceTier.MEDIUM
        elif score >= 0.25:
            tier = ConfidenceTier.LOW
        else:
            tier = ConfidenceTier.VERY_LOW
        return ConfidenceSignal(score=round(score, 4), tier=tier, method=method, **kwargs)


HEDGE_PATTERNS: List[Tuple[str, float]] = [
    (r"\b(I'm not sure|I am not sure)\b", -0.25),
    (r"\b(I think|I believe|I suspect)\b", -0.10),
    (r"\b(probably|likely|possibly|perhaps|maybe)\b", -0.10),
    (r"\b(might|could|may)\b", -0.08),
    (r"\b(it appears|it seems|it looks like)\b", -0.10),
    (r"\b(to my knowledge|as far as I know)\b", -0.15),
    (r"\b(I cannot confirm|I can't confirm|cannot verify)\b", -0.30),
    (r"\b(uncertain|unclear|ambiguous)\b", -0.20),
    (r"\b(definitely|certainly|absolutely|clearly)\b", +0.10),
    (r"\b(confirmed|verified|established)\b", +0.10),
]


class HedgeDetectionExtractor:
    """
    Estimates confidence from the presence of hedging and certainty phrases
    in the agent's response text. Starts at a baseline of 0.75 and adjusts.
    """

    BASELINE = 0.75

    def extract(self, response_text: str) -> ConfidenceSignal:
        score = self.BASELINE
        found_hedges = []
        for pattern, delta in HEDGE_PATTERNS:
            matches = re.findall(pattern, response_text, re.IGNORECASE)
            if matches:
                score += delta * len(matches)
                found_hedges.extend(matches)
        score = max(0.05, min(0.99, score))
        return ConfidenceSignal.from_score(
            score, method="hedge_detection", hedge_phrases=found_hedges[:5]
        )
```

## Solution 2: Self-Reported Confidence Extractor

```python
import json
import re
from typing import Optional


class SelfReportedConfidenceExtractor:
    """
    Extracts a self-reported confidence score from structured LLM output.
    The LLM is prompted to include a JSON block with a 'confidence' field.
    Falls back to hedge detection if the structured field is absent.
    """

    CONFIDENCE_BLOCK_PATTERN = re.compile(
        r'\{[^}]*"confidence"\s*:\s*([0-9.]+)[^}]*\}', re.IGNORECASE
    )

    def __init__(self, fallback_extractor: HedgeDetectionExtractor):
        self._fallback = fallback_extractor

    def extract(self, response_text: str) -> ConfidenceSignal:
        match = self.CONFIDENCE_BLOCK_PATTERN.search(response_text)
        if match:
            try:
                raw_score = float(match.group(1))
                score = max(0.0, min(1.0, raw_score))
                return ConfidenceSignal.from_score(
                    score, method="self_report", raw_value=match.group(1)
                )
            except ValueError:
                pass
        return self._fallback.extract(response_text)

    @staticmethod
    def system_prompt_addition() -> str:
        return (
            'At the end of your response, always include a JSON confidence block: '
            '{"confidence": 0.0-1.0} where 1.0 = completely certain, '
            '0.0 = complete uncertainty.'
        )
```

## Solution 3: Decision Confidence Record

```python
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class DecisionConfidenceRecord:
    session_id: str
    decision_id: str
    timestamp: float = field(default_factory=time.time)
    question_preview: str = ""        # first 200 chars of user question
    response_preview: str = ""        # first 200 chars of agent response
    confidence: Optional[ConfidenceSignal] = None
    tool_calls_made: int = 0
    routed_for_review: bool = False
    outcome_label: Optional[str] = None   # filled in post-hoc if verified


class DecisionConfidenceStore:
    """
    Accumulates confidence records per session for trend analysis.
    """

    def __init__(self, max_records: int = 50_000):
        self._records: list = []
        self._max = max_records
        self._lock = __import__("threading").Lock()

    def record(self, rec: DecisionConfidenceRecord) -> None:
        with self._lock:
            self._records.append(rec)
            if len(self._records) > self._max:
                self._records.pop(0)

    def recent(self, window_seconds: float = 3600.0) -> list:
        cutoff = time.time() - window_seconds
        with self._lock:
            return [r for r in self._records if r.timestamp >= cutoff]

    def low_confidence_decisions(
        self, window_seconds: float = 3600.0, tiers: list = None
    ) -> list:
        tiers = tiers or [ConfidenceTier.LOW, ConfidenceTier.VERY_LOW]
        return [
            r for r in self.recent(window_seconds)
            if r.confidence and r.confidence.tier in tiers
        ]
```

## Solution 4: Low-Confidence Review Router

```python
from typing import Callable, List, Optional


class LowConfidenceReviewRouter:
    """
    Routes decisions below a confidence threshold to a human review queue.
    Integrates with any queue backend via an injectable enqueue function.
    """

    def __init__(
        self,
        enqueue_fn: Callable[[dict], None],
        review_threshold: float = 0.50,
        very_low_threshold: float = 0.25,
    ):
        self._enqueue = enqueue_fn
        self._review_threshold = review_threshold
        self._very_low_threshold = very_low_threshold
        self._routed_count = 0

    def evaluate_and_route(self, record: DecisionConfidenceRecord) -> bool:
        if record.confidence is None:
            return False
        score = record.confidence.score
        if score >= self._review_threshold:
            return False

        priority = "urgent" if score < self._very_low_threshold else "normal"
        self._enqueue({
            "decision_id": record.decision_id,
            "session_id": record.session_id,
            "confidence_score": score,
            "confidence_tier": record.confidence.tier.value,
            "question": record.question_preview,
            "response": record.response_preview,
            "priority": priority,
        })
        record.routed_for_review = True
        self._routed_count += 1
        return True

    def stats(self) -> dict:
        return {"total_routed": self._routed_count}
```

## Solution 5: Confidence Calibration Tracker

```python
import time
from typing import List, Optional, Tuple


class ConfidenceCalibrationTracker:
    """
    Measures calibration: whether self-reported confidence correlates with
    actual accuracy. Requires post-hoc outcome labels on decision records.
    """

    def __init__(self, store: DecisionConfidenceStore):
        self._store = store

    def calibration_by_tier(self, window_seconds: float = 86400.0) -> dict:
        labeled = [
            r for r in self._store.recent(window_seconds)
            if r.confidence and r.outcome_label is not None
        ]
        if not labeled:
            return {"status": "insufficient_labeled_data"}

        tier_stats: dict = {}
        for rec in labeled:
            tier = rec.confidence.tier.value
            correct = rec.outcome_label == "correct"
            if tier not in tier_stats:
                tier_stats[tier] = {"total": 0, "correct": 0, "scores": []}
            tier_stats[tier]["total"] += 1
            if correct:
                tier_stats[tier]["correct"] += 1
            tier_stats[tier]["scores"].append(rec.confidence.score)

        result = {}
        for tier, stats in tier_stats.items():
            result[tier] = {
                "count": stats["total"],
                "accuracy": round(stats["correct"] / stats["total"], 3),
                "avg_confidence": round(sum(stats["scores"]) / len(stats["scores"]), 3),
            }
        return result
```

## Solution 6: Decision Confidence Dashboard

```python
import time
from typing import List


class DecisionConfidenceDashboard:
    """
    Operational view of confidence distribution, low-confidence rate,
    review routing volume, and calibration summary.
    """

    def __init__(
        self,
        store: DecisionConfidenceStore,
        router: LowConfidenceReviewRouter,
        calibration: ConfidenceCalibrationTracker,
    ):
        self._store = store
        self._router = router
        self._calibration = calibration

    def render(self, window_seconds: float = 3600.0) -> dict:
        recent = self._store.recent(window_seconds)
        low_conf = self._store.low_confidence_decisions(window_seconds)

        tier_dist: dict = {}
        for rec in recent:
            if rec.confidence:
                t = rec.confidence.tier.value
                tier_dist[t] = tier_dist.get(t, 0) + 1

        avg_confidence = (
            sum(r.confidence.score for r in recent if r.confidence) /
            max(sum(1 for r in recent if r.confidence), 1)
        )

        return {
            "generated_at": time.time(),
            "window_seconds": window_seconds,
            "total_decisions": len(recent),
            "avg_confidence": round(avg_confidence, 3),
            "tier_distribution": tier_dist,
            "low_confidence_count": len(low_conf),
            "low_confidence_rate": round(len(low_conf) / max(len(recent), 1), 3),
            "review_router": self._router.stats(),
            "calibration_24h": self._calibration.calibration_by_tier(86400.0),
        }
```

## Comparison

| Approach | Score Extraction | Tier Classification | Review Routing | Calibration Tracking | Dashboard |
|---|---|---|---|---|---|
| HedgeDetectionExtractor | Yes (regex) | Via from_score | No | No | No |
| SelfReportedConfidenceExtractor | Yes (JSON) | Via from_score | No | No | No |
| DecisionConfidenceStore | No | No | No | No | No |
| LowConfidenceReviewRouter | No | Via signal | Yes (pluggable) | No | No |
| ConfidenceCalibrationTracker | No | Via records | No | Yes | No |
| DecisionConfidenceDashboard | No | No | No | No | Yes |

**Best for production**: Combine both extraction methods — include `SelfReportedConfidenceExtractor.system_prompt_addition()` in the system prompt and fall back to `HedgeDetectionExtractor` when the structured field is absent. Route decisions below 0.50 for human review and flag decisions below 0.25 as urgent — these often indicate the agent is being asked about topics outside its knowledge or is receiving contradictory context. Track calibration monthly: if HIGH-confidence decisions have accuracy below 85%, the model is overconfident and the review threshold should be raised. Never surface raw confidence scores to end users — they miscalibrate expectations; instead use them internally for routing and quality control.
