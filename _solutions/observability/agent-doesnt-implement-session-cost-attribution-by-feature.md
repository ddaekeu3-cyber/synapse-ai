---
title: "Agent Doesn't Implement Session Cost Attribution by Feature"
description: "Agents that report only total session cost cannot answer 'which feature is responsible for 60% of our LLM spend?' or 'does the summarization feature cost more than retrieval-augmented generation?'. Implement session cost attribution that tags every LLM call with the feature that triggered it, accumulates cost per feature across sessions, and surfaces the top cost drivers with per-feature unit economics."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-session-cost-attribution-by-feature
tags: [cost-attribution, feature-cost, llm-spend, unit-economics, cost-observability, session-cost]
symptoms:
  - "Monthly LLM bill increases 40% but no data shows which feature caused the increase"
  - "Cannot tell whether the new summarization feature or the existing RAG pipeline costs more"
  - "Cost is tracked per session but not broken down by which agent capability was invoked"
  - "Product team cannot make cost/quality trade-offs without per-feature cost data"
  - "Cost per user is known but cost per feature per user is not"
---

## Why This Happens

LLM usage is typically billed by session or request, not by the product feature that triggered the request. Agents mix multiple capabilities — retrieval, summarization, planning, tool-calling — within a single session, and all costs roll up to a single session total. Attribution requires tagging each LLM call at invocation time with the feature name, accumulating tagged costs, and computing per-feature unit economics (cost per call, cost per user, cost per session). Without this tagging, cost reduction efforts are guesswork.

## Solution 1: Cost Attribution Tag

```python
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class CostAttributionTag:
    feature: str                     # e.g. "rag", "summarization", "planning", "tool_call"
    sub_feature: Optional[str] = None   # e.g. "document_retrieval", "final_answer"
    session_id: str = ""
    user_id: str = ""
    experiment_id: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def label(self) -> str:
        if self.sub_feature:
            return f"{self.feature}/{self.sub_feature}"
        return self.feature
```

## Solution 2: Attributed LLM Call Record

```python
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class AttributedLLMCallRecord:
    call_id: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost_usd: float
    tag: CostAttributionTag
    latency_ms: float
    recorded_at: float = field(default_factory=time.time)

    @property
    def feature(self) -> str:
        return self.tag.label()

    @classmethod
    def from_response(
        cls,
        call_id: str,
        model: str,
        usage: dict,
        tag: CostAttributionTag,
        latency_ms: float,
        cost_per_1k_prompt: float = 0.0,
        cost_per_1k_completion: float = 0.0,
    ) -> "AttributedLLMCallRecord":
        prompt = usage.get("prompt_tokens", 0)
        completion = usage.get("completion_tokens", 0)
        cost = (prompt * cost_per_1k_prompt + completion * cost_per_1k_completion) / 1000.0
        return cls(
            call_id=call_id,
            model=model,
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=prompt + completion,
            cost_usd=round(cost, 6),
            tag=tag,
            latency_ms=latency_ms,
        )
```

## Solution 3: Feature Cost Accumulator

```python
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class FeatureCostBucket:
    feature: str
    call_count: int = 0
    total_tokens: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_cost_usd: float = 0.0
    unique_sessions: set = field(default_factory=set)
    unique_users: set = field(default_factory=set)

    def add(self, rec: "AttributedLLMCallRecord") -> None:
        self.call_count += 1
        self.total_tokens += rec.total_tokens
        self.prompt_tokens += rec.prompt_tokens
        self.completion_tokens += rec.completion_tokens
        self.total_cost_usd += rec.cost_usd
        if rec.tag.session_id:
            self.unique_sessions.add(rec.tag.session_id)
        if rec.tag.user_id:
            self.unique_users.add(rec.tag.user_id)

    def cost_per_call(self) -> float:
        return round(self.total_cost_usd / max(self.call_count, 1), 6)

    def cost_per_session(self) -> Optional[float]:
        if not self.unique_sessions:
            return None
        return round(self.total_cost_usd / len(self.unique_sessions), 6)

    def cost_per_user(self) -> Optional[float]:
        if not self.unique_users:
            return None
        return round(self.total_cost_usd / len(self.unique_users), 6)


class FeatureCostAccumulator:
    """
    Accumulates attributed LLM call records into per-feature cost buckets.
    Supports a time window so only recent calls are included in reports.
    """

    def __init__(self, window_seconds: float = 86400.0):
        self._window = window_seconds
        self._records: List[AttributedLLMCallRecord] = []

    def record(self, rec: AttributedLLMCallRecord) -> None:
        self._records.append(rec)

    def _trim(self) -> None:
        cutoff = time.time() - self._window
        self._records = [r for r in self._records if r.recorded_at >= cutoff]

    def buckets(self) -> Dict[str, FeatureCostBucket]:
        self._trim()
        result: Dict[str, FeatureCostBucket] = {}
        for rec in self._records:
            feature = rec.feature
            if feature not in result:
                result[feature] = FeatureCostBucket(feature=feature)
            result[feature].add(rec)
        return result

    def total_cost_usd(self) -> float:
        self._trim()
        return round(sum(r.cost_usd for r in self._records), 4)

    def total_calls(self) -> int:
        self._trim()
        return len(self._records)
```

## Solution 4: Feature Cost Report Generator

```python
import time
from typing import List


class FeatureCostReportGenerator:
    """
    Generates ranked cost attribution reports from accumulated records.
    Computes each feature's share of total cost and unit economics.
    """

    def __init__(self, accumulator: FeatureCostAccumulator):
        self._accumulator = accumulator

    def generate(self) -> dict:
        buckets = self._accumulator.buckets()
        total_cost = self._accumulator.total_cost_usd()
        total_calls = self._accumulator.total_calls()

        features = []
        for feature, bucket in buckets.items():
            features.append({
                "feature": feature,
                "call_count": bucket.call_count,
                "total_cost_usd": round(bucket.total_cost_usd, 4),
                "pct_of_total_cost": round(
                    bucket.total_cost_usd / max(total_cost, 1e-9) * 100, 2
                ),
                "cost_per_call_usd": bucket.cost_per_call(),
                "cost_per_session_usd": bucket.cost_per_session(),
                "cost_per_user_usd": bucket.cost_per_user(),
                "total_tokens": bucket.total_tokens,
                "unique_sessions": len(bucket.unique_sessions),
                "unique_users": len(bucket.unique_users),
            })

        features.sort(key=lambda x: -x["total_cost_usd"])

        return {
            "generated_at": time.time(),
            "window_seconds": self._accumulator._window,
            "total_cost_usd": total_cost,
            "total_calls": total_calls,
            "features": features,
            "top_cost_driver": features[0]["feature"] if features else None,
        }
```

## Solution 5: Feature Cost Anomaly Detector

```python
import time
from typing import Dict, List, Optional


class FeatureCostAnomalyDetector:
    """
    Detects when a feature's cost share increases significantly
    compared to its recent historical share, indicating runaway usage.
    """

    def __init__(
        self,
        accumulator: FeatureCostAccumulator,
        comparison_window_seconds: float = 86400.0,
        spike_multiplier: float = 2.5,
        min_calls_to_alert: int = 20,
    ):
        self._accumulator = accumulator
        self._comp_window = comparison_window_seconds
        self._spike_mult = spike_multiplier
        self._min_calls = min_calls_to_alert
        self._baseline: Dict[str, float] = {}   # feature -> baseline cost_per_call

    def update_baseline(self) -> None:
        """Call periodically (e.g., daily) to refresh the cost-per-call baseline."""
        buckets = self._accumulator.buckets()
        for feature, bucket in buckets.items():
            if bucket.call_count >= self._min_calls:
                self._baseline[feature] = bucket.cost_per_call()

    def detect(self) -> List[dict]:
        if not self._baseline:
            return []
        buckets = self._accumulator.buckets()
        anomalies = []
        for feature, bucket in buckets.items():
            baseline_cpc = self._baseline.get(feature)
            if baseline_cpc is None or bucket.call_count < self._min_calls:
                continue
            current_cpc = bucket.cost_per_call()
            if current_cpc > baseline_cpc * self._spike_mult:
                anomalies.append({
                    "feature": feature,
                    "current_cost_per_call": current_cpc,
                    "baseline_cost_per_call": baseline_cpc,
                    "ratio": round(current_cpc / max(baseline_cpc, 1e-9), 2),
                    "recommendation": (
                        f"Feature '{feature}' cost per call is "
                        f"{current_cpc/baseline_cpc:.1f}× baseline — "
                        "check for prompt length regression or model change"
                    ),
                })
        return anomalies
```

## Solution 6: Feature Cost Dashboard

```python
import time


class FeatureCostDashboard:
    """
    Combines attribution report, anomaly detection, and trend summary
    into a single cost observability view.
    """

    def __init__(
        self,
        report_generator: FeatureCostReportGenerator,
        anomaly_detector: FeatureCostAnomalyDetector,
        budget_usd_per_day: Optional[float] = None,
    ):
        self._generator = report_generator
        self._detector = anomaly_detector
        self._daily_budget = budget_usd_per_day

    def render(self) -> dict:
        report = self._generator.generate()
        anomalies = self._detector.detect()

        alerts = []
        for anomaly in anomalies:
            alerts.append({
                "type": "cost_spike",
                "feature": anomaly["feature"],
                "ratio": anomaly["ratio"],
                "message": anomaly["recommendation"],
            })

        if self._daily_budget and report["total_cost_usd"] > self._daily_budget:
            alerts.append({
                "type": "budget_exceeded",
                "total_cost_usd": report["total_cost_usd"],
                "budget_usd": self._daily_budget,
                "overage_pct": round(
                    (report["total_cost_usd"] - self._daily_budget)
                    / self._daily_budget * 100, 1
                ),
            })

        return {
            "generated_at": time.time(),
            "summary": {
                "total_cost_usd": report["total_cost_usd"],
                "total_calls": report["total_calls"],
                "top_cost_driver": report["top_cost_driver"],
                "feature_count": len(report["features"]),
            },
            "attribution": report["features"],
            "alerts": alerts,
        }


from typing import Optional
```

## Comparison

| Approach | Per-Feature Tagging | Cost Accumulation | Unit Economics | Anomaly Detection | Dashboard |
|---|---|---|---|---|---|
| FeatureCostAccumulator | Via tags | Yes (windowed) | No | No | No |
| FeatureCostReportGenerator | Via accumulator | Via accumulator | Yes | No | No |
| FeatureCostAnomalyDetector | Via accumulator | Via accumulator | Via accumulator | Yes | No |
| FeatureCostDashboard | No | No | Via report | Via detector | Yes |

**Best for production**: Tag every LLM call with a `CostAttributionTag` at the call site — this is the only accurate way to attribute cost since a single session may invoke multiple features. Use `feature="rag"` and `sub_feature="retrieval"` or `sub_feature="synthesis"` to distinguish the retrieval step from the generation step in RAG pipelines. Set `window_seconds=86400` for daily cost reports and emit `FeatureCostDashboard.render()` to your metrics system every 15 minutes. Share per-feature cost-per-call with product teams so they can make informed decisions about prompt length, model choice, and caching — most teams discover that one feature accounts for 50%+ of cost once attribution is in place.
