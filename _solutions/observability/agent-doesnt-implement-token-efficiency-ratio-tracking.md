---
title: "Agent Doesn't Implement Token Efficiency Ratio Tracking"
description: "Agents that track raw token counts cannot answer 'are we getting more value per token over time?' — a response that uses 2,000 tokens to answer a simple question is less efficient than one that uses 400 tokens for the same quality. Implement token efficiency ratio tracking that measures output quality per token spent, computes efficiency trends across model versions and prompt changes, and alerts when efficiency degrades without a corresponding quality improvement."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-token-efficiency-ratio-tracking
tags: [token-efficiency, tokens-per-answer, cost-quality, efficiency-ratio, prompt-optimization, value-per-token]
symptoms:
  - "Token usage increased 40% after a prompt change but response quality did not improve"
  - "No metric to compare token cost between two prompt variants for equivalent quality"
  - "Verbose model responses inflate token cost with no corresponding user value"
  - "Cannot distinguish 'expensive because complex' from 'expensive because inefficient'"
  - "Prompt iteration decisions are made without measuring token efficiency impact"
---

## Why This Happens

Token count is a cost metric, not a value metric. High token counts may be justified (complex reasoning, long documents) or wasteful (verbose preambles, unnecessary repetition). Without a denominator — a quality signal to divide the token count by — there is no way to compute efficiency. Token efficiency ratio divides quality output by token cost: a response that earns a 0.8 satisfaction rating at 500 tokens is more efficient than one earning 0.6 at 1,000 tokens. Tracking this ratio over time reveals whether prompt and model changes are improving or degrading cost-effectiveness.

## Solution 1: Efficiency Measurement Record

```python
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class EfficiencyMeasurementRecord:
    session_id: str
    turn_number: int
    model: str
    prompt_version: Optional[str]
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    quality_signal: Optional[float]   # 0.0–1.0 (satisfaction, task completion, etc.)
    quality_signal_type: str          # "explicit_rating" | "implicit" | "task_completion"
    response_length_chars: int
    tool_calls_made: int
    cost_usd: Optional[float] = None
    recorded_at: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def tokens_per_quality_point(self) -> Optional[float]:
        """Lower is better: fewer tokens per unit of quality."""
        if self.quality_signal is None or self.quality_signal == 0:
            return None
        return round(self.total_tokens / self.quality_signal, 2)

    def completion_ratio(self) -> float:
        """Fraction of total tokens used for completion (vs prompt)."""
        return round(self.completion_tokens / max(self.total_tokens, 1), 4)

    def chars_per_token(self) -> float:
        """Higher = denser, more information-packed responses."""
        return round(self.response_length_chars / max(self.completion_tokens, 1), 2)
```

## Solution 2: Efficiency Ratio Accumulator

```python
import time
from collections import defaultdict, deque
from typing import Dict, List, Optional, Tuple


class TokenEfficiencyAccumulator:
    """
    Accumulates efficiency records per (model, prompt_version) pair
    and computes rolling efficiency ratios.
    """

    def __init__(self, window_size: int = 500) -> None:
        self._window = window_size
        # (model, prompt_version) -> deque of records
        self._buckets: Dict[Tuple[str, Optional[str]], deque] = defaultdict(
            lambda: deque(maxlen=window_size)
        )

    def record(self, rec: EfficiencyMeasurementRecord) -> None:
        key = (rec.model, rec.prompt_version)
        self._buckets[key].append(rec)

    def efficiency_metrics(
        self,
        model: str,
        prompt_version: Optional[str] = None,
    ) -> Optional[dict]:
        key = (model, prompt_version)
        records = list(self._buckets.get(key, []))
        if len(records) < 5:
            return None

        n = len(records)
        total_tokens = [r.total_tokens for r in records]
        completion_tokens = [r.completion_tokens for r in records]
        quality_records = [r for r in records if r.quality_signal is not None]
        efficiency_records = [r for r in quality_records if r.tokens_per_quality_point() is not None]

        avg_total_tokens = round(sum(total_tokens) / n, 1)
        avg_completion = round(sum(completion_tokens) / n, 1)
        avg_quality = (
            round(sum(r.quality_signal for r in quality_records) / len(quality_records), 4)
            if quality_records else None
        )
        avg_tokens_per_quality = (
            round(sum(r.tokens_per_quality_point() for r in efficiency_records) / len(efficiency_records), 2)
            if efficiency_records else None
        )
        avg_chars_per_token = round(
            sum(r.chars_per_token() for r in records) / n, 2
        )

        return {
            "model": model,
            "prompt_version": prompt_version,
            "sample_count": n,
            "avg_total_tokens": avg_total_tokens,
            "avg_completion_tokens": avg_completion,
            "avg_quality_signal": avg_quality,
            "avg_tokens_per_quality_point": avg_tokens_per_quality,
            "avg_chars_per_token": avg_chars_per_token,
            "quality_sample_count": len(quality_records),
        }

    def all_buckets(self) -> List[dict]:
        return [
            m for (model, version) in self._buckets
            if (m := self.efficiency_metrics(model, version)) is not None
        ]
```

## Solution 3: Efficiency Trend Analyzer

```python
import time
from collections import deque
from typing import List, Optional


class EfficiencyTrendAnalyzer:
    """
    Detects efficiency trends over time within a single (model, prompt_version) bucket.
    Computes a simple linear trend on rolling efficiency ratios.
    """

    def __init__(self, accumulator: TokenEfficiencyAccumulator) -> None:
        self._accumulator = accumulator

    def _linear_trend(self, values: List[float]) -> float:
        """Returns slope (positive = degrading efficiency if metric is tokens_per_quality)."""
        n = len(values)
        if n < 3:
            return 0.0
        x_mean = (n - 1) / 2.0
        y_mean = sum(values) / n
        numerator = sum((i - x_mean) * (v - y_mean) for i, v in enumerate(values))
        denominator = sum((i - x_mean) ** 2 for i in range(n))
        if denominator == 0:
            return 0.0
        return round(numerator / denominator, 4)

    def trend(
        self,
        model: str,
        prompt_version: Optional[str] = None,
        window: int = 50,
    ) -> Optional[dict]:
        key = (model, prompt_version)
        records = list(self._accumulator._buckets.get(key, []))
        recent = records[-window:]
        efficiency_values = [
            r.tokens_per_quality_point()
            for r in recent
            if r.tokens_per_quality_point() is not None
        ]

        if len(efficiency_values) < 10:
            return None

        slope = self._linear_trend(efficiency_values)
        direction = "degrading" if slope > 0 else "improving" if slope < 0 else "stable"

        return {
            "model": model,
            "prompt_version": prompt_version,
            "efficiency_trend_slope": slope,
            "direction": direction,
            "sample_count": len(efficiency_values),
            "latest_tokens_per_quality": efficiency_values[-1],
            "earliest_tokens_per_quality": efficiency_values[0],
            "pct_change": round(
                (efficiency_values[-1] - efficiency_values[0]) / max(efficiency_values[0], 0.001) * 100, 2
            ),
        }
```

## Solution 4: Cross-Version Efficiency Comparator

```python
from typing import Dict, List, Optional, Tuple


class CrossVersionEfficiencyComparator:
    """
    Compares token efficiency between two (model, prompt_version) configurations.
    Helps answer "did our prompt optimization improve cost-efficiency?"
    """

    def __init__(self, accumulator: TokenEfficiencyAccumulator) -> None:
        self._accumulator = accumulator

    def compare(
        self,
        baseline: Tuple[str, Optional[str]],
        candidate: Tuple[str, Optional[str]],
    ) -> Optional[dict]:
        base_metrics = self._accumulator.efficiency_metrics(*baseline)
        cand_metrics = self._accumulator.efficiency_metrics(*candidate)

        if not base_metrics or not cand_metrics:
            return None

        def delta(key: str) -> Optional[dict]:
            b = base_metrics.get(key)
            c = cand_metrics.get(key)
            if b is None or c is None:
                return None
            change = c - b
            pct = round(change / max(abs(b), 0.001) * 100, 2)
            # For tokens_per_quality: lower is better
            # For avg_quality_signal: higher is better
            if key == "avg_tokens_per_quality_point":
                better = "candidate" if change < 0 else "baseline"
            elif key in ("avg_quality_signal", "avg_chars_per_token"):
                better = "candidate" if change > 0 else "baseline"
            else:
                better = None
            return {"baseline": b, "candidate": c, "pct_change": pct, "better": better}

        metrics = ["avg_tokens_per_quality_point", "avg_total_tokens",
                   "avg_quality_signal", "avg_chars_per_token"]
        deltas = {m: delta(m) for m in metrics if delta(m)}

        wins_cand = sum(1 for d in deltas.values() if d and d["better"] == "candidate")
        wins_base = sum(1 for d in deltas.values() if d and d["better"] == "baseline")

        return {
            "baseline": {"model": baseline[0], "prompt_version": baseline[1]},
            "candidate": {"model": candidate[0], "prompt_version": candidate[1]},
            "deltas": deltas,
            "wins_candidate": wins_cand,
            "wins_baseline": wins_base,
            "recommendation": (
                "prefer_candidate" if wins_cand > wins_base
                else "keep_baseline" if wins_base > wins_cand
                else "inconclusive"
            ),
        }
```

## Solution 5: Efficiency Alert Manager

```python
import time
from typing import Callable, List, Optional


class TokenEfficiencyAlertManager:
    """
    Fires alerts when token efficiency degrades beyond thresholds
    or when a prompt change causes unexplained token inflation.
    """

    def __init__(
        self,
        accumulator: TokenEfficiencyAccumulator,
        trend_analyzer: EfficiencyTrendAnalyzer,
        degradation_pct_alert: float = 20.0,
        cooldown_seconds: float = 3600.0,
        handler: Optional[Callable[[dict], None]] = None,
    ) -> None:
        self._accumulator = accumulator
        self._analyzer = trend_analyzer
        self._degradation_threshold = degradation_pct_alert
        self._cooldown = cooldown_seconds
        self._handler = handler
        self._last_fired: dict = {}

    def _can_fire(self, key: str) -> bool:
        last = self._last_fired.get(key, 0.0)
        if time.time() - last >= self._cooldown:
            self._last_fired[key] = time.time()
            return True
        return False

    def check(self) -> List[dict]:
        alerts = []
        for bucket_metrics in self._accumulator.all_buckets():
            model = bucket_metrics["model"]
            pv = bucket_metrics["prompt_version"]
            trend = self._analyzer.trend(model, pv)

            if trend and trend["direction"] == "degrading":
                if trend["pct_change"] >= self._degradation_threshold:
                    key = f"{model}:{pv}:degrading"
                    if self._can_fire(key):
                        alert = {
                            "type": "efficiency_degradation",
                            "model": model,
                            "prompt_version": pv,
                            "pct_change": trend["pct_change"],
                            "severity": "warning",
                            "message": (
                                f"Token efficiency degraded {trend['pct_change']:.1f}% "
                                f"for model '{model}' prompt '{pv}'"
                            ),
                        }
                        alerts.append(alert)
                        if self._handler:
                            try:
                                self._handler(alert)
                            except Exception:
                                pass

        return alerts
```

## Solution 6: Token Efficiency Dashboard

```python
import time


class TokenEfficiencyDashboard:
    """
    Combines efficiency metrics, trend analysis, cross-version comparison,
    and alerts into a single cost-quality operational view.
    """

    def __init__(
        self,
        accumulator: TokenEfficiencyAccumulator,
        trend_analyzer: EfficiencyTrendAnalyzer,
        alert_manager: TokenEfficiencyAlertManager,
    ) -> None:
        self._accumulator = accumulator
        self._analyzer = trend_analyzer
        self._alerts = alert_manager

    def render(self) -> dict:
        all_metrics = self._accumulator.all_buckets()
        alerts = self._alerts.check()

        trends = []
        for m in all_metrics:
            t = self._analyzer.trend(m["model"], m["prompt_version"])
            if t:
                trends.append(t)

        return {
            "generated_at": time.time(),
            "configurations": len(all_metrics),
            "metrics": all_metrics,
            "trends": trends,
            "degrading_configurations": sum(
                1 for t in trends if t["direction"] == "degrading"
            ),
            "active_alerts": alerts,
        }
```

## Comparison

| Approach | Per-Record Efficiency | Rolling Aggregation | Trend Detection | Cross-Version Compare | Alerts |
|---|---|---|---|---|---|
| EfficiencyMeasurementRecord | Yes (per call) | No | No | No | No |
| TokenEfficiencyAccumulator | No | Yes | No | No | No |
| EfficiencyTrendAnalyzer | No | Via accumulator | Yes (linear slope) | No | No |
| CrossVersionEfficiencyComparator | No | Via accumulator | No | Yes | No |
| TokenEfficiencyAlertManager | No | No | Via analyzer | No | Yes |

**Best for production**: Track `tokens_per_quality_point` as the primary efficiency metric rather than raw token count — a model that uses 20% more tokens but produces 40% higher quality is more efficient, and raw token tracking would incorrectly flag this as a regression. Set a minimum of 50 quality-signal samples before computing efficiency ratios — below that, a few outlier responses dominate the average. Alert on `pct_change >= 20%` in token-per-quality over a rolling window: smaller changes are within normal variance, but a 20%+ degradation almost always indicates a prompt issue, model change, or query distribution shift worth investigating.
