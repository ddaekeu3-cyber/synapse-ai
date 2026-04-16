---
title: "Agent Doesn't Implement Prompt Effectiveness Scoring"
description: "Agents that never measure whether their prompts produce useful responses cannot detect prompt degradation after model updates, cannot compare variants, and cannot identify which prompt templates are responsible for poor outputs. Implement prompt effectiveness scoring that evaluates response quality against expected criteria, tracks scores over time per prompt template, detects regression after model or prompt changes, and surfaces low-performing templates for revision."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-prompt-effectiveness-scoring
tags: [prompt-effectiveness, prompt-quality, response-scoring, prompt-regression, ab-testing, prompt-observability]
symptoms:
  - "No way to tell whether a prompt change improved or degraded response quality"
  - "Model update degrades a specific prompt template but the regression goes undetected for weeks"
  - "Two prompt variants are deployed but there is no data on which performs better"
  - "Support tickets spike after a prompt change but there is no per-template quality history"
  - "Prompt authors guess at quality based on subjective review rather than measured signals"
---

## Why This Happens

Most agents log inputs and outputs but never score them. Without a quality signal, every prompt looks equally good until a human complains. Effectiveness scoring attaches a numeric quality estimate to each response — derived from structural checks, LLM-as-judge evaluation, or task-specific heuristics — and accumulates those scores per prompt template over a rolling window. When scores drop after a deployment, the regression is visible immediately rather than weeks later through support escalations.

## Solution 1: Prompt Response Record

```python
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class PromptResponseRecord:
    record_id: str = field(default_factory=lambda: str(uuid.uuid4())[:12])
    template_id: str = ""
    template_version: str = "1.0"
    prompt_text: str = ""
    response_text: str = ""
    model: str = ""
    latency_ms: float = 0.0
    token_count: int = 0
    effectiveness_score: Optional[float] = None   # 0.0–1.0
    score_components: Dict[str, float] = field(default_factory=dict)
    scorer_id: str = ""
    created_at: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def is_scored(self) -> bool:
        return self.effectiveness_score is not None

    def is_high_quality(self, threshold: float = 0.75) -> bool:
        return self.effectiveness_score is not None and self.effectiveness_score >= threshold

    def is_low_quality(self, threshold: float = 0.40) -> bool:
        return self.effectiveness_score is not None and self.effectiveness_score < threshold
```

## Solution 2: Structural Response Scorer

```python
import re
from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass
class ScoringCriterion:
    name: str
    weight: float
    required_patterns: List[str] = field(default_factory=list)   # regex patterns that MUST match
    forbidden_patterns: List[str] = field(default_factory=list)  # regex patterns that MUST NOT match
    min_length: int = 0
    max_length: int = 100_000


from dataclasses import field


class StructuralResponseScorer:
    """
    Scores responses against structural criteria without calling an LLM.
    Fast and cheap — suitable for every response in production.
    Checks: required content patterns, forbidden patterns, length bounds.
    """

    def __init__(self, criteria: List[ScoringCriterion]):
        self._criteria = criteria

    def score(self, record: PromptResponseRecord) -> PromptResponseRecord:
        response = record.response_text
        components: Dict[str, float] = {}
        total_weight = sum(c.weight for c in self._criteria)

        for criterion in self._criteria:
            score = 1.0

            # Length check
            length = len(response)
            if length < criterion.min_length or length > criterion.max_length:
                score = 0.0
            else:
                # Required patterns
                for pattern in criterion.required_patterns:
                    if not re.search(pattern, response, re.IGNORECASE | re.DOTALL):
                        score *= 0.5

                # Forbidden patterns
                for pattern in criterion.forbidden_patterns:
                    if re.search(pattern, response, re.IGNORECASE | re.DOTALL):
                        score *= 0.3

            components[criterion.name] = round(score, 4)

        weighted_sum = sum(
            components[c.name] * c.weight
            for c in self._criteria
        )
        composite = weighted_sum / max(total_weight, 1e-9)

        record.effectiveness_score = round(composite, 4)
        record.score_components = components
        record.scorer_id = "structural"
        return record

    def describe_failure(self, record: PromptResponseRecord) -> List[str]:
        """Returns human-readable descriptions of which criteria failed."""
        failures = []
        for criterion in self._criteria:
            comp_score = record.score_components.get(criterion.name, 1.0)
            if comp_score < 0.5:
                failures.append(
                    f"criterion '{criterion.name}' scored {comp_score:.2f} "
                    f"(weight={criterion.weight})"
                )
        return failures
```

## Solution 3: Prompt Template Score Tracker

```python
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional


@dataclass
class TemplateScoreWindow:
    template_id: str
    template_version: str
    window_size: int = 200
    scores: Deque[float] = field(default_factory=deque)
    timestamps: Deque[float] = field(default_factory=deque)

    def add(self, score: float) -> None:
        self.scores.append(score)
        self.timestamps.append(time.time())
        if len(self.scores) > self.window_size:
            self.scores.popleft()
            self.timestamps.popleft()

    def mean(self) -> Optional[float]:
        if not self.scores:
            return None
        return round(sum(self.scores) / len(self.scores), 4)

    def p10(self) -> Optional[float]:
        if not self.scores:
            return None
        sorted_scores = sorted(self.scores)
        idx = max(0, int(len(sorted_scores) * 0.10) - 1)
        return round(sorted_scores[idx], 4)

    def recent_mean(self, n: int = 20) -> Optional[float]:
        recent = list(self.scores)[-n:]
        if not recent:
            return None
        return round(sum(recent) / len(recent), 4)

    def sample_count(self) -> int:
        return len(self.scores)


class PromptTemplateScoreTracker:
    """
    Maintains a rolling score window per (template_id, template_version) pair.
    Records scored PromptResponseRecords and exposes per-template statistics.
    """

    def __init__(self, window_size: int = 200):
        self._window_size = window_size
        self._windows: Dict[str, TemplateScoreWindow] = {}

    def _key(self, template_id: str, version: str) -> str:
        return f"{template_id}::{version}"

    def record(self, rec: PromptResponseRecord) -> None:
        if not rec.is_scored():
            return
        key = self._key(rec.template_id, rec.template_version)
        if key not in self._windows:
            self._windows[key] = TemplateScoreWindow(
                template_id=rec.template_id,
                template_version=rec.template_version,
                window_size=self._window_size,
            )
        self._windows[key].add(rec.effectiveness_score)

    def stats(self, template_id: str, version: str) -> Optional[dict]:
        key = self._key(template_id, version)
        win = self._windows.get(key)
        if not win:
            return None
        return {
            "template_id": template_id,
            "version": version,
            "sample_count": win.sample_count(),
            "mean_score": win.mean(),
            "p10_score": win.p10(),
            "recent_mean_20": win.recent_mean(20),
        }

    def all_template_stats(self) -> List[dict]:
        result = []
        for win in self._windows.values():
            result.append({
                "template_id": win.template_id,
                "version": win.template_version,
                "sample_count": win.sample_count(),
                "mean_score": win.mean(),
                "p10_score": win.p10(),
                "recent_mean_20": win.recent_mean(20),
            })
        return sorted(result, key=lambda x: (x["mean_score"] or 0))
```

## Solution 4: Prompt Regression Detector

```python
import time
from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass
class PromptRegressionEvent:
    template_id: str
    old_version: str
    new_version: str
    old_mean: float
    new_mean: float
    drop: float
    detected_at: float


class PromptRegressionDetector:
    """
    Detects when a new prompt template version produces significantly
    lower effectiveness scores than the previous version.
    Fires a regression event when the mean drop exceeds the threshold
    and enough samples exist in both windows.
    """

    def __init__(
        self,
        tracker: PromptTemplateScoreTracker,
        min_drop_to_alert: float = 0.10,
        min_samples_per_version: int = 30,
    ):
        self._tracker = tracker
        self._min_drop = min_drop_to_alert
        self._min_samples = min_samples_per_version
        self._known_versions: Dict[str, List[str]] = {}   # template_id -> [versions]
        self._regression_events: List[PromptRegressionEvent] = []

    def observe_version(self, template_id: str, version: str) -> None:
        versions = self._known_versions.setdefault(template_id, [])
        if version not in versions:
            versions.append(version)

    def check_regressions(self) -> List[PromptRegressionEvent]:
        new_events = []
        for template_id, versions in self._known_versions.items():
            if len(versions) < 2:
                continue
            # Compare each consecutive pair
            for i in range(len(versions) - 1):
                old_v = versions[i]
                new_v = versions[i + 1]
                old_stats = self._tracker.stats(template_id, old_v)
                new_stats = self._tracker.stats(template_id, new_v)
                if not old_stats or not new_stats:
                    continue
                if (old_stats["sample_count"] < self._min_samples
                        or new_stats["sample_count"] < self._min_samples):
                    continue
                old_mean = old_stats["mean_score"] or 0.0
                new_mean = new_stats["mean_score"] or 0.0
                drop = old_mean - new_mean
                if drop >= self._min_drop:
                    event = PromptRegressionEvent(
                        template_id=template_id,
                        old_version=old_v,
                        new_version=new_v,
                        old_mean=round(old_mean, 4),
                        new_mean=round(new_mean, 4),
                        drop=round(drop, 4),
                        detected_at=time.time(),
                    )
                    self._regression_events.append(event)
                    new_events.append(event)
        return new_events

    def recent_regressions(self, window_seconds: float = 86400.0) -> List[PromptRegressionEvent]:
        cutoff = time.time() - window_seconds
        return [e for e in self._regression_events if e.detected_at >= cutoff]
```

## Solution 5: Prompt A/B Comparator

```python
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


@dataclass
class ABVariant:
    variant_id: str
    template_id: str
    template_version: str
    traffic_weight: float = 0.5   # fraction of traffic to send to this variant


@dataclass
class ABExperimentResult:
    experiment_id: str
    variant_a: ABVariant
    variant_b: ABVariant
    a_mean: float
    b_mean: float
    a_samples: int
    b_samples: int
    winner: Optional[str]   # variant_id or None if no clear winner
    confidence: str         # "low" | "medium" | "high"
    delta: float


class PromptABComparator:
    """
    Compares two prompt template variants using accumulated effectiveness scores.
    Determines a winner when the score difference is statistically meaningful
    (approximated by sample count and magnitude of delta).
    """

    def __init__(
        self,
        tracker: PromptTemplateScoreTracker,
        min_samples: int = 50,
        significant_delta: float = 0.05,
    ):
        self._tracker = tracker
        self._min_samples = min_samples
        self._delta = significant_delta
        self._experiments: Dict[str, Tuple[ABVariant, ABVariant]] = {}

    def register_experiment(
        self,
        experiment_id: str,
        variant_a: ABVariant,
        variant_b: ABVariant,
    ) -> None:
        self._experiments[experiment_id] = (variant_a, variant_b)

    def evaluate(self, experiment_id: str) -> Optional[ABExperimentResult]:
        pair = self._experiments.get(experiment_id)
        if not pair:
            return None
        va, vb = pair

        stats_a = self._tracker.stats(va.template_id, va.template_version)
        stats_b = self._tracker.stats(vb.template_id, vb.template_version)

        if not stats_a or not stats_b:
            return None

        a_mean = stats_a["mean_score"] or 0.0
        b_mean = stats_b["mean_score"] or 0.0
        a_n = stats_a["sample_count"]
        b_n = stats_b["sample_count"]
        delta = abs(a_mean - b_mean)

        if a_n < self._min_samples or b_n < self._min_samples:
            confidence = "low"
            winner = None
        elif delta < self._delta:
            confidence = "medium"
            winner = None
        elif min(a_n, b_n) >= self._min_samples * 3:
            confidence = "high"
            winner = va.variant_id if a_mean > b_mean else vb.variant_id
        else:
            confidence = "medium"
            winner = va.variant_id if a_mean > b_mean else vb.variant_id

        return ABExperimentResult(
            experiment_id=experiment_id,
            variant_a=va,
            variant_b=vb,
            a_mean=round(a_mean, 4),
            b_mean=round(b_mean, 4),
            a_samples=a_n,
            b_samples=b_n,
            winner=winner,
            confidence=confidence,
            delta=round(delta, 4),
        )

    def all_results(self) -> List[ABExperimentResult]:
        results = []
        for exp_id in self._experiments:
            result = self.evaluate(exp_id)
            if result:
                results.append(result)
        return results
```

## Solution 6: Prompt Effectiveness Dashboard

```python
import time
from typing import List, Optional


class PromptEffectivenessDashboard:
    """
    Aggregates per-template scores, regression events, and A/B results
    into a single observability report.
    Surfaces low-performing templates and prompts requiring attention.
    """

    def __init__(
        self,
        tracker: PromptTemplateScoreTracker,
        regression_detector: PromptRegressionDetector,
        ab_comparator: PromptABComparator,
        low_quality_threshold: float = 0.50,
    ):
        self._tracker = tracker
        self._detector = regression_detector
        self._ab = ab_comparator
        self._low_threshold = low_quality_threshold

    def render(self) -> dict:
        all_stats = self._tracker.all_template_stats()
        regressions = self._detector.check_regressions()
        ab_results = self._ab.all_results()

        low_performing = [
            s for s in all_stats
            if (s["mean_score"] or 1.0) < self._low_threshold
            and s["sample_count"] >= 20
        ]

        alerts = []
        for event in regressions:
            alerts.append({
                "type": "prompt_regression",
                "template_id": event.template_id,
                "old_version": event.old_version,
                "new_version": event.new_version,
                "drop": event.drop,
                "recommendation": (
                    f"Roll back {event.template_id} to version {event.old_version} "
                    f"or investigate changes between {event.old_version} and {event.new_version}"
                ),
            })
        for s in low_performing:
            alerts.append({
                "type": "low_performing_template",
                "template_id": s["template_id"],
                "version": s["version"],
                "mean_score": s["mean_score"],
                "recommendation": "Review prompt structure and scoring criteria for this template",
            })

        return {
            "generated_at": time.time(),
            "template_count": len(all_stats),
            "templates": all_stats,
            "ab_experiments": [
                {
                    "experiment_id": r.experiment_id,
                    "winner": r.winner,
                    "confidence": r.confidence,
                    "a_mean": r.a_mean,
                    "b_mean": r.b_mean,
                    "delta": r.delta,
                }
                for r in ab_results
            ],
            "alerts": alerts,
            "summary": {
                "healthy_templates": len([
                    s for s in all_stats if (s["mean_score"] or 0) >= self._low_threshold
                ]),
                "low_performing_templates": len(low_performing),
                "regression_events_24h": len(self._detector.recent_regressions(86400)),
                "active_ab_experiments": len(ab_results),
            },
        }
```

## Comparison

| Approach | Scoring | Per-Template History | Regression Detection | A/B Comparison | Dashboard |
|---|---|---|---|---|---|
| StructuralResponseScorer | Yes (pattern/length) | No | No | No | No |
| PromptTemplateScoreTracker | No | Yes (rolling window) | No | No | No |
| PromptRegressionDetector | No | Via tracker | Yes (version delta) | No | No |
| PromptABComparator | No | Via tracker | No | Yes (confidence level) | No |
| PromptEffectivenessDashboard | No | Via tracker | Via detector | Via comparator | Yes |

**Best for production**: Attach `StructuralResponseScorer` to every prompt template with at least two criteria: a required pattern (e.g., the response must contain a conclusion section) and a max-length guard (responses longer than 4000 tokens indicate prompt failure). Feed every scored record into `PromptTemplateScoreTracker` with a 200-sample window. Run `PromptRegressionDetector.check_regressions()` after every model or prompt deployment — set `min_drop_to_alert=0.08` to catch subtle degradations. Use `PromptABComparator` for any prompt rewrite: deploy both versions, collect 50+ samples each, and let the data pick the winner rather than intuition.
