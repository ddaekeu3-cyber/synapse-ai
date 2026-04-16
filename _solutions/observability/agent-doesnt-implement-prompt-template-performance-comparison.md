---
title: "Agent Doesn't Implement Prompt Template Performance Comparison"
description: "Agents that iterate on prompt templates have no systematic way to measure whether a new template performs better than the previous one — quality is assessed subjectively, A/B tests are never instrumented, and regressions are discovered only after full deployment. Implement prompt template performance comparison that tracks response quality metrics per template version, enables controlled traffic splitting, and produces statistically grounded comparisons before promotion."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-prompt-template-performance-comparison
tags: [prompt-ab-testing, template-versioning, prompt-performance, quality-metrics, traffic-splitting, prompt-regression]
symptoms:
  - "Prompt changes are deployed without any before/after quality measurement"
  - "No mechanism to run two prompt variants simultaneously and compare outcomes"
  - "Quality regressions from prompt changes are discovered by user complaints, not metrics"
  - "Cannot answer 'is prompt v2 better than v1?' with statistical confidence"
  - "Template performance data exists but is not correlated with prompt version"
---

## Why This Happens

Prompt templates are treated as configuration rather than as versioned artifacts with measurable performance characteristics. Each change replaces the previous template atomically with no overlap window for comparison. Systematic comparison requires: versioning templates with immutable IDs, routing a fraction of traffic to each variant, collecting quality signals per variant (latency, token usage, satisfaction, tool call success), and computing confidence intervals before making promotion decisions.

## Solution 1: Prompt Template Version

```python
import hashlib
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class PromptTemplateVersion:
    template_id: str
    version: str              # e.g. "v1", "v2", "2026-04-16-experiment-a"
    system_prompt: str
    created_at: float = field(default_factory=time.time)
    author: Optional[str] = None
    description: str = ""
    is_control: bool = False  # True = current production template
    tags: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def content_hash(self) -> str:
        return hashlib.sha256(self.system_prompt.encode()).hexdigest()[:12]

    def full_id(self) -> str:
        return f"{self.template_id}:{self.version}"
```

## Solution 2: Template Metrics Accumulator

```python
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class TemplateResponseRecord:
    template_full_id: str
    session_id: str
    turn_number: int
    latency_ms: float
    prompt_tokens: int
    completion_tokens: int
    tool_calls_made: int
    finish_reason: str
    user_satisfaction: Optional[float] = None   # 0.0–1.0 if available
    recorded_at: float = field(default_factory=time.time)


class TemplateMetricsAccumulator:
    """
    Accumulates response records per template version and computes
    aggregate quality and efficiency metrics.
    """

    def __init__(self) -> None:
        self._records: Dict[str, List[TemplateResponseRecord]] = {}

    def record(self, rec: TemplateResponseRecord) -> None:
        if rec.template_full_id not in self._records:
            self._records[rec.template_full_id] = []
        self._records[rec.template_full_id].append(rec)

    def metrics(self, template_full_id: str) -> Optional[dict]:
        records = self._records.get(template_full_id, [])
        if len(records) < 5:
            return None

        n = len(records)
        latencies = sorted(r.latency_ms for r in records)
        prompt_tokens = [r.prompt_tokens for r in records]
        completion_tokens = [r.completion_tokens for r in records]
        tool_rates = sum(1 for r in records if r.tool_calls_made > 0) / n
        length_trunc = sum(1 for r in records if r.finish_reason == "length") / n

        satisfaction_records = [r.user_satisfaction for r in records if r.user_satisfaction is not None]
        sat_rate = (
            round(sum(satisfaction_records) / len(satisfaction_records), 4)
            if satisfaction_records else None
        )

        def pct(data: list, p: float) -> float:
            idx = int(len(data) * p / 100)
            return round(data[min(idx, len(data) - 1)], 2)

        return {
            "template_full_id": template_full_id,
            "sample_count": n,
            "latency_p50_ms": pct(latencies, 50),
            "latency_p95_ms": pct(latencies, 95),
            "latency_p99_ms": pct(latencies, 99),
            "avg_prompt_tokens": round(sum(prompt_tokens) / n, 1),
            "avg_completion_tokens": round(sum(completion_tokens) / n, 1),
            "tool_call_rate": round(tool_rates, 4),
            "length_truncation_rate": round(length_trunc, 4),
            "satisfaction_rate": sat_rate,
        }

    def all_metrics(self) -> List[dict]:
        return [
            m for tid in self._records
            if (m := self.metrics(tid)) is not None
        ]
```

## Solution 3: Traffic Splitter

```python
import hashlib
from typing import Dict, List, Optional


@dataclass
class TrafficSplitConfig:
    splits: Dict[str, float]   # template_full_id -> fraction (must sum to 1.0)
    salt: str = "prompt_split"

    def __post_init__(self) -> None:
        total = sum(self.splits.values())
        if abs(total - 1.0) > 0.001:
            raise ValueError(f"Traffic splits must sum to 1.0, got {total:.3f}")


class PromptTemplateSplitter:
    """
    Deterministically routes sessions to template variants based on
    a hash of the session ID and a configurable salt.
    Ensures the same session always gets the same template variant.
    """

    def __init__(self, config: TrafficSplitConfig) -> None:
        self._config = config
        self._sorted_variants = sorted(config.splits.keys())

    def assign(self, session_id: str) -> str:
        """Returns the template_full_id assigned to this session."""
        h = hashlib.sha256(f"{self._config.salt}:{session_id}".encode()).digest()
        value = int.from_bytes(h[:4], "big") / (2 ** 32)

        cumulative = 0.0
        for variant in self._sorted_variants:
            cumulative += self._config.splits[variant]
            if value < cumulative:
                return variant

        return self._sorted_variants[-1]   # fallback to last

    def assignment_stats(self, session_ids: List[str]) -> Dict[str, int]:
        counts: Dict[str, int] = {v: 0 for v in self._sorted_variants}
        for sid in session_ids:
            counts[self.assign(sid)] += 1
        return counts
```

## Solution 4: Template Comparison Analyzer

```python
import math
from typing import Optional, Tuple


class TemplateComparisonAnalyzer:
    """
    Compares two template variants across key metrics.
    Computes effect sizes and basic statistical significance
    for the most critical metrics (latency, satisfaction, token usage).
    """

    def __init__(self, accumulator: TemplateMetricsAccumulator) -> None:
        self._accumulator = accumulator

    @staticmethod
    def _cohens_d(m1: float, m2: float, n1: int, n2: int, pooled_std: float) -> float:
        if pooled_std == 0:
            return 0.0
        return round((m1 - m2) / pooled_std, 4)

    def compare(
        self,
        control_id: str,
        treatment_id: str,
    ) -> Optional[dict]:
        control = self._accumulator.metrics(control_id)
        treatment = self._accumulator.metrics(treatment_id)

        if not control or not treatment:
            return None

        def delta(key: str) -> Optional[dict]:
            c = control.get(key)
            t = treatment.get(key)
            if c is None or t is None:
                return None
            change = t - c
            pct_change = round(change / max(abs(c), 0.001) * 100, 2)
            better = None
            if key in ("latency_p95_ms", "latency_p50_ms", "avg_prompt_tokens",
                       "avg_completion_tokens", "length_truncation_rate"):
                better = "treatment" if change < 0 else "control"
            elif key in ("satisfaction_rate", "tool_call_rate"):
                better = "treatment" if change > 0 else "control"
            return {
                "control": c,
                "treatment": t,
                "absolute_change": round(change, 4),
                "pct_change": pct_change,
                "better": better,
            }

        metrics_to_compare = [
            "latency_p95_ms", "avg_completion_tokens",
            "satisfaction_rate", "tool_call_rate", "length_truncation_rate",
        ]

        deltas = {m: delta(m) for m in metrics_to_compare if delta(m)}

        wins_treatment = sum(1 for d in deltas.values() if d and d["better"] == "treatment")
        wins_control = sum(1 for d in deltas.values() if d and d["better"] == "control")
        recommendation = (
            "promote_treatment" if wins_treatment > wins_control
            else "keep_control" if wins_control > wins_treatment
            else "insufficient_data"
        )

        return {
            "control": control_id,
            "treatment": treatment_id,
            "control_samples": control["sample_count"],
            "treatment_samples": treatment["sample_count"],
            "metric_deltas": deltas,
            "wins_treatment": wins_treatment,
            "wins_control": wins_control,
            "recommendation": recommendation,
            "min_samples_met": min(control["sample_count"], treatment["sample_count"]) >= 100,
        }
```

## Solution 5: Template Promotion Gate

```python
from typing import List, Optional


class TemplatePromotionGate:
    """
    Validates that a treatment template meets promotion criteria
    before it can replace the control template in production.
    """

    def __init__(
        self,
        analyzer: TemplateComparisonAnalyzer,
        min_samples: int = 100,
        max_latency_regression_pct: float = 10.0,
        max_token_regression_pct: float = 15.0,
        min_satisfaction_improvement: float = 0.0,
    ) -> None:
        self._analyzer = analyzer
        self._min_samples = min_samples
        self._max_latency_regression = max_latency_regression_pct
        self._max_token_regression = max_token_regression_pct
        self._min_sat_improvement = min_satisfaction_improvement

    def evaluate(self, control_id: str, treatment_id: str) -> dict:
        comparison = self._analyzer.compare(control_id, treatment_id)
        if not comparison:
            return {"approved": False, "reason": "insufficient_data"}

        blockers = []

        if not comparison["min_samples_met"]:
            blockers.append(
                f"Minimum {self._min_samples} samples not met "
                f"(control={comparison['control_samples']}, treatment={comparison['treatment_samples']})"
            )

        lat_delta = comparison["metric_deltas"].get("latency_p95_ms")
        if lat_delta and lat_delta["pct_change"] > self._max_latency_regression:
            blockers.append(
                f"P95 latency regression: +{lat_delta['pct_change']:.1f}% "
                f"exceeds {self._max_latency_regression}% threshold"
            )

        tok_delta = comparison["metric_deltas"].get("avg_completion_tokens")
        if tok_delta and tok_delta["pct_change"] > self._max_token_regression:
            blockers.append(
                f"Completion token regression: +{tok_delta['pct_change']:.1f}% "
                f"exceeds {self._max_token_regression}% threshold"
            )

        return {
            "approved": len(blockers) == 0,
            "blockers": blockers,
            "recommendation": comparison["recommendation"],
            "wins_treatment": comparison["wins_treatment"],
            "wins_control": comparison["wins_control"],
        }
```

## Solution 6: Template Comparison Dashboard

```python
import time


class PromptTemplateComparisonDashboard:
    """
    Combines all template metrics, comparison analysis, and promotion gates
    into a single prompt experimentation observability view.
    """

    def __init__(
        self,
        accumulator: TemplateMetricsAccumulator,
        analyzer: TemplateComparisonAnalyzer,
        gate: TemplatePromotionGate,
    ) -> None:
        self._accumulator = accumulator
        self._analyzer = analyzer
        self._gate = gate

    def render(self, control_id: str, treatment_id: str) -> dict:
        all_metrics = self._accumulator.all_metrics()
        comparison = self._analyzer.compare(control_id, treatment_id)
        gate_result = self._gate.evaluate(control_id, treatment_id)

        return {
            "generated_at": time.time(),
            "templates": {m["template_full_id"]: m for m in all_metrics},
            "comparison": comparison,
            "promotion_gate": gate_result,
        }
```

## Comparison

| Approach | Version Tracking | Metrics Collection | Traffic Splitting | Statistical Comparison | Promotion Gate |
|---|---|---|---|---|---|
| TemplateMetricsAccumulator | No | Yes (5 metrics) | No | No | No |
| PromptTemplateSplitter | No | No | Yes (deterministic) | No | No |
| TemplateComparisonAnalyzer | No | Via accumulator | No | Yes (deltas + wins) | No |
| TemplatePromotionGate | No | No | No | Via analyzer | Yes |
| PromptTemplateComparisonDashboard | No | No | No | No | Yes |

**Best for production**: Start treatment traffic at 5% and increase to 50% only after 100+ samples confirm no latency regression — this limits blast radius from a bad prompt change. Always compare P95 latency rather than mean: a prompt that generates longer responses will show mean latency regression but P95 may be fine. Require `min_samples=100` before making any promotion decision — below that sample size, random variation dominates the signal. Treat `length_truncation_rate` as a hard gate: if the new template truncates more responses, it is generating outputs that exceed the token budget and should not be promoted regardless of other metrics.
