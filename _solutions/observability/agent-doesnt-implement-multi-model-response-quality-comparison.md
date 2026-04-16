---
title: "Agent Doesn't Implement Multi-Model Response Quality Comparison"
description: "Agents that use a single model without comparing its outputs against alternatives cannot detect quality regressions after model updates, identify which model performs better on specific task categories, or make data-driven routing decisions. Implement multi-model response quality comparison that samples a fraction of requests, runs them through multiple models, scores the responses on defined quality dimensions, and surfaces quality deltas that inform model selection."
date: 2026-04-16
difficulty: advanced
category: observability
slug: agent-doesnt-implement-multi-model-response-quality-comparison
tags: [model-comparison, quality-scoring, shadow-model, a-b-testing, response-quality, model-evaluation]
symptoms:
  - "No baseline exists to detect whether a model update degraded response quality"
  - "Cannot determine whether a cheaper model performs acceptably for common tasks"
  - "Quality comparisons happen through manual review, not automated scoring"
  - "No data exists on which model performs best by task category or prompt type"
  - "Model selection decisions are made on cost/latency without quality evidence"
---

## Why This Happens

Model quality is multi-dimensional and task-specific. A model that excels at summarization may underperform on code generation. A cheaper model may match the primary model on 80% of tasks but fail on the 20% that matter most. Without systematic comparison, model selection relies on benchmarks that may not reflect the actual distribution of tasks the agent handles. Shadow evaluation runs the same request through multiple models simultaneously, scores the responses on dimensions relevant to the use case (completeness, accuracy, format adherence, conciseness), and accumulates evidence about relative quality over real production traffic.

## Solution 1: Quality Dimension

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional


class QualityDimension(str, Enum):
    COMPLETENESS = "completeness"      # did it address all parts of the question
    ACCURACY = "accuracy"              # factual correctness (requires ground truth)
    FORMAT_ADHERENCE = "format"        # followed formatting instructions
    CONCISENESS = "conciseness"        # not verbose or padded
    INSTRUCTION_FOLLOW = "instruction" # followed all constraints in the prompt
    SAFETY = "safety"                  # did not produce harmful content


@dataclass
class QualityScorer:
    dimension: QualityDimension
    score_fn: Callable          # fn(response: str, prompt: str, **kwargs) -> float (0-1)
    weight: float = 1.0
    description: str = ""


@dataclass
class ResponseQualityScore:
    model_id: str
    prompt_hash: str
    dimension_scores: Dict[str, float] = field(default_factory=dict)
    weighted_total: float = 0.0
    latency_ms: float = 0.0
    token_count: int = 0
    response_preview: str = ""    # first 200 chars for debugging

    def overall_score(self) -> float:
        return round(self.weighted_total, 4)
```

## Solution 2: Response Quality Evaluator

```python
import hashlib
import re
import time
from typing import Any, Dict, List, Optional


class ResponseQualityEvaluator:
    """
    Applies a set of quality scorers to a model response and
    produces a ResponseQualityScore.
    """

    def __init__(self, scorers: List[QualityScorer]):
        self._scorers = scorers
        self._total_weight = sum(s.weight for s in scorers) or 1.0

    def evaluate(
        self,
        model_id: str,
        prompt: str,
        response: str,
        latency_ms: float = 0.0,
        token_count: int = 0,
    ) -> ResponseQualityScore:
        prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()[:12]
        dimension_scores: Dict[str, float] = {}
        weighted_sum = 0.0

        for scorer in self._scorers:
            try:
                score = scorer.score_fn(response, prompt)
                score = max(0.0, min(1.0, float(score)))
            except Exception:
                score = 0.0
            dimension_scores[scorer.dimension.value] = round(score, 4)
            weighted_sum += score * scorer.weight

        return ResponseQualityScore(
            model_id=model_id,
            prompt_hash=prompt_hash,
            dimension_scores=dimension_scores,
            weighted_total=round(weighted_sum / self._total_weight, 4),
            latency_ms=latency_ms,
            token_count=token_count,
            response_preview=response[:200],
        )


def heuristic_scorers() -> List[QualityScorer]:
    """Built-in heuristic scorers that require no ground truth."""

    def conciseness(response: str, prompt: str) -> float:
        words = len(response.split())
        if words > 2000:
            return 0.3
        if words > 800:
            return 0.7
        return 1.0

    def format_adherence(response: str, prompt: str) -> float:
        if "```" in prompt and "```" not in response:
            return 0.5
        if "json" in prompt.lower() and not response.strip().startswith("{"):
            return 0.6
        return 1.0

    def instruction_follow(response: str, prompt: str) -> float:
        refusals = ["i cannot", "i am unable", "i'm not able", "i won't"]
        if any(r in response.lower() for r in refusals):
            return 0.2
        return 1.0

    return [
        QualityScorer(QualityDimension.CONCISENESS, conciseness, weight=0.3),
        QualityScorer(QualityDimension.FORMAT_ADHERENCE, format_adherence, weight=0.4),
        QualityScorer(QualityDimension.INSTRUCTION_FOLLOW, instruction_follow, weight=0.3),
    ]
```

## Solution 3: Shadow Model Runner

```python
import asyncio
import time
from typing import Any, Callable, Dict, List, Optional


class ShadowModelRunner:
    """
    Runs a request against the primary model and shadow models concurrently.
    Returns primary result immediately; shadow evaluations happen in background.
    """

    def __init__(
        self,
        primary_model_id: str,
        shadow_model_ids: List[str],
        sample_rate: float = 0.10,   # fraction of requests to shadow-evaluate
    ):
        self._primary = primary_model_id
        self._shadows = shadow_model_ids
        self._rate = sample_rate
        self._sampled = 0
        self._skipped = 0

    def _should_sample(self) -> bool:
        import random
        return random.random() < self._rate

    async def run(
        self,
        call_fn: Callable,          # async fn(model_id, prompt, **kwargs) -> (response, latency_ms, tokens)
        prompt: str,
        evaluator: "ResponseQualityEvaluator",
        comparison_recorder: "ModelComparisonRecorder",
        **kwargs,
    ) -> str:
        # Always call primary
        primary_response, primary_latency, primary_tokens = await call_fn(
            self._primary, prompt, **kwargs
        )

        if not self._should_sample():
            self._skipped += 1
            return primary_response

        self._sampled += 1

        async def shadow_eval(model_id: str) -> None:
            try:
                shadow_response, shadow_latency, shadow_tokens = await call_fn(
                    model_id, prompt, **kwargs
                )
                primary_score = evaluator.evaluate(
                    self._primary, prompt, primary_response, primary_latency, primary_tokens
                )
                shadow_score = evaluator.evaluate(
                    model_id, prompt, shadow_response, shadow_latency, shadow_tokens
                )
                comparison_recorder.record(primary_score, shadow_score)
            except Exception:
                pass   # shadow failures must not affect primary response

        tasks = [shadow_eval(mid) for mid in self._shadows]
        asyncio.create_task(asyncio.gather(*tasks, return_exceptions=True))

        return primary_response

    def stats(self) -> dict:
        return {"sampled": self._sampled, "skipped": self._skipped}
```

## Solution 4: Model Comparison Recorder

```python
import time
import threading
from collections import defaultdict
from typing import Dict, List, Tuple


class ModelComparisonRecorder:
    """
    Accumulates pairwise quality comparisons between primary and shadow models.
    Tracks win/loss/tie rates and dimension-level deltas.
    """

    def __init__(self):
        self._comparisons: List[Tuple[ResponseQualityScore, ResponseQualityScore]] = []
        self._lock = threading.Lock()

    def record(
        self,
        primary: ResponseQualityScore,
        shadow: ResponseQualityScore,
    ) -> None:
        with self._lock:
            self._comparisons.append((primary, shadow))
            if len(self._comparisons) > 10000:
                self._comparisons.pop(0)

    def summary(self, shadow_model_id: str, window: int = 1000) -> dict:
        with self._lock:
            pairs = [
                (p, s) for p, s in self._comparisons[-window:]
                if s.model_id == shadow_model_id
            ]

        if not pairs:
            return {"model_id": shadow_model_id, "comparisons": 0}

        primary_wins = sum(1 for p, s in pairs if p.weighted_total > s.weighted_total + 0.02)
        shadow_wins = sum(1 for p, s in pairs if s.weighted_total > p.weighted_total + 0.02)
        ties = len(pairs) - primary_wins - shadow_wins

        avg_primary = sum(p.weighted_total for p, _ in pairs) / len(pairs)
        avg_shadow = sum(s.weighted_total for _, s in pairs) / len(pairs)

        dim_deltas: Dict[str, float] = defaultdict(float)
        for p, s in pairs:
            for dim in p.dimension_scores:
                dim_deltas[dim] += s.dimension_scores.get(dim, 0) - p.dimension_scores.get(dim, 0)
        for dim in dim_deltas:
            dim_deltas[dim] = round(dim_deltas[dim] / len(pairs), 4)

        return {
            "shadow_model_id": shadow_model_id,
            "comparisons": len(pairs),
            "primary_win_rate": round(primary_wins / len(pairs), 4),
            "shadow_win_rate": round(shadow_wins / len(pairs), 4),
            "tie_rate": round(ties / len(pairs), 4),
            "avg_primary_score": round(avg_primary, 4),
            "avg_shadow_score": round(avg_shadow, 4),
            "quality_delta": round(avg_shadow - avg_primary, 4),
            "dimension_deltas": dict(dim_deltas),
        }
```

## Solution 5: Quality Regression Detector

```python
from typing import Optional


class QualityRegressionDetector:
    """
    Detects quality regressions after model updates by comparing
    recent quality scores to a baseline period.
    """

    def __init__(
        self,
        recorder: ModelComparisonRecorder,
        regression_threshold: float = -0.05,   # 5% quality drop triggers alert
    ):
        self._recorder = recorder
        self._threshold = regression_threshold
        self._baselines: dict = {}

    def snapshot_baseline(self, model_id: str, window: int = 500) -> None:
        summary = self._recorder.summary(model_id, window)
        self._baselines[model_id] = summary.get("avg_shadow_score")

    def check(self, model_id: str, window: int = 100) -> dict:
        baseline = self._baselines.get(model_id)
        summary = self._recorder.summary(model_id, window)
        recent_score = summary.get("avg_shadow_score")

        if baseline is None or recent_score is None:
            return {"status": "no_baseline", "model_id": model_id}

        delta = recent_score - baseline
        regressed = delta < self._threshold

        return {
            "model_id": model_id,
            "status": "regression" if regressed else "ok",
            "baseline_score": baseline,
            "recent_score": recent_score,
            "delta": round(delta, 4),
            "regressed": regressed,
        }
```

## Solution 6: Model Quality Dashboard

```python
import time


class ModelQualityDashboard:
    """
    Combines shadow runner stats, comparison summaries per model,
    and regression checks into a single model quality view.
    """

    def __init__(
        self,
        runner: ShadowModelRunner,
        recorder: ModelComparisonRecorder,
        regression_detector: QualityRegressionDetector,
    ):
        self._runner = runner
        self._recorder = recorder
        self._regression = regression_detector

    def render(self) -> dict:
        shadow_summaries = {
            mid: self._recorder.summary(mid)
            for mid in self._runner._shadows
        }
        regression_checks = {
            mid: self._regression.check(mid)
            for mid in self._runner._shadows
        }
        return {
            "generated_at": time.time(),
            "shadow_runner": self._runner.stats(),
            "model_comparisons": shadow_summaries,
            "regression_status": regression_checks,
        }
```

## Comparison

| Approach | Heuristic Scoring | Shadow Execution | Pairwise Comparison | Regression Detection | Dashboard |
|---|---|---|---|---|---|
| ResponseQualityEvaluator | Yes (pluggable) | No | No | No | No |
| ShadowModelRunner | No | Yes (async) | No | No | No |
| ModelComparisonRecorder | No | No | Yes (win/loss) | No | No |
| QualityRegressionDetector | No | No | Via recorder | Yes | No |
| ModelQualityDashboard | No | No | No | No | Yes |

**Best for production**: Set `sample_rate=0.05` for shadow evaluation in high-traffic production environments — 5% sampling provides statistically significant quality signals without doubling inference costs. Always fire shadow calls as background tasks so the primary response is never delayed by shadow evaluation. Use `QualityRegressionDetector.snapshot_baseline()` immediately after each model deployment and check for regressions after 100 shadow samples — this provides a data-driven rollback trigger rather than waiting for user complaints.
