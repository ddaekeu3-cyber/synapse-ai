---
title: "Agent Doesn't Implement Cost Attribution by Feature and User Segment"
description: "Agents that report only total token costs cannot answer 'which feature is most expensive?' or 'which user tier consumes the most?' — making cost optimization guesswork. Implement cost attribution that tags every LLM and tool call with feature labels and user segment metadata, aggregates costs per dimension, and identifies the top contributors to the monthly bill."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-cost-attribution-by-feature-and-user-segment
tags: [cost-attribution, token-cost, feature-cost, user-segment, billing, cost-optimization]
symptoms:
  - "Monthly LLM bill is $50K but no breakdown by product feature"
  - "Cannot tell whether the summarization feature or the Q&A feature costs more"
  - "Free-tier users and enterprise users share a single cost metric"
  - "Cost optimization efforts are blind — no data on which calls to optimize first"
  - "Token usage logged but not tagged with business context"
---

## Why This Happens

Token usage counters are infrastructure metrics. Business cost attribution requires enriching each API call with application context — which feature triggered it, which user segment owns it, which product area it belongs to. Without this enrichment at call time, the data cannot be reconstructed later. The pattern is simple: every LLM call is tagged with a `CostAttributionContext` that captures feature, user_tier, product_area, and session_id, and these tags are carried through to cost aggregation.

## Solution 1: Cost Attribution Context

```python
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class CostAttributionContext:
    """
    Business context tags attached to every LLM or tool API call.
    These tags are used to group and aggregate costs by dimension.
    """
    feature: str                    # e.g. "summarization", "qa", "code_review"
    user_tier: str                  # e.g. "free", "pro", "enterprise"
    product_area: str               # e.g. "dashboard", "api", "mobile"
    session_id: str = ""
    user_id: str = ""
    tenant_id: str = ""
    custom_tags: Dict[str, str] = field(default_factory=dict)

    def tag_key(self) -> tuple:
        return (self.feature, self.user_tier, self.product_area)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "feature": self.feature,
            "user_tier": self.user_tier,
            "product_area": self.product_area,
            "session_id": self.session_id,
            "user_id": self.user_id,
            "tenant_id": self.tenant_id,
            **self.custom_tags,
        }
```

## Solution 2: Attributed Cost Record

```python
import time
from dataclasses import dataclass, field
from typing import Optional


# Cost rates per 1K tokens (adjust to your provider/model)
MODEL_COST_RATES = {
    "claude-opus-4-6":     {"input": 0.015, "output": 0.075},
    "claude-sonnet-4-6":   {"input": 0.003, "output": 0.015},
    "claude-haiku-4-5":    {"input": 0.00025, "output": 0.00125},
    "gpt-4o":              {"input": 0.005, "output": 0.015},
    "gpt-4o-mini":         {"input": 0.00015, "output": 0.0006},
}


@dataclass
class AttributedCostRecord:
    attribution: CostAttributionContext
    model: str
    input_tokens: int
    output_tokens: int
    total_tokens: int
    input_cost_usd: float
    output_cost_usd: float
    total_cost_usd: float
    recorded_at: float = field(default_factory=time.time)

    @classmethod
    def from_usage(
        cls,
        attribution: CostAttributionContext,
        model: str,
        input_tokens: int,
        output_tokens: int,
    ) -> "AttributedCostRecord":
        rates = MODEL_COST_RATES.get(model, {"input": 0.003, "output": 0.015})
        input_cost = input_tokens * rates["input"] / 1000.0
        output_cost = output_tokens * rates["output"] / 1000.0
        return cls(
            attribution=attribution,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
            input_cost_usd=round(input_cost, 8),
            output_cost_usd=round(output_cost, 8),
            total_cost_usd=round(input_cost + output_cost, 8),
        )
```

## Solution 3: Cost Attribution Store

```python
import time
from collections import defaultdict
from threading import Lock
from typing import Dict, List, Optional


class CostAttributionStore:
    """
    Accumulates attributed cost records and provides aggregation
    by any combination of attribution dimensions.
    """

    def __init__(self, max_records: int = 100000, window_seconds: float = 86400.0):
        self._max = max_records
        self._window = window_seconds
        self._records: List[AttributedCostRecord] = []
        self._lock = Lock()

    def record(self, rec: AttributedCostRecord) -> None:
        with self._lock:
            self._records.append(rec)
            if len(self._records) > self._max:
                self._records.pop(0)

    def _trim(self, now: float) -> List[AttributedCostRecord]:
        cutoff = now - self._window
        return [r for r in self._records if r.recorded_at >= cutoff]

    def aggregate_by(
        self,
        dimension: str,   # "feature" | "user_tier" | "product_area" | "model"
        window_seconds: Optional[float] = None,
    ) -> Dict[str, dict]:
        now = time.time()
        cutoff = now - (window_seconds or self._window)
        with self._lock:
            recent = [r for r in self._records if r.recorded_at >= cutoff]

        agg: Dict[str, dict] = defaultdict(lambda: {
            "total_cost_usd": 0.0,
            "total_tokens": 0,
            "call_count": 0,
        })

        for rec in recent:
            key = getattr(rec.attribution, dimension, None) or getattr(rec, dimension, "unknown")
            agg[key]["total_cost_usd"] += rec.total_cost_usd
            agg[key]["total_tokens"] += rec.total_tokens
            agg[key]["call_count"] += 1

        # Sort by cost descending
        return dict(sorted(
            {k: {**v, "total_cost_usd": round(v["total_cost_usd"], 4)} for k, v in agg.items()}.items(),
            key=lambda x: -x[1]["total_cost_usd"]
        ))

    def top_contributors(self, n: int = 10, window_seconds: Optional[float] = None) -> List[dict]:
        now = time.time()
        cutoff = now - (window_seconds or self._window)
        with self._lock:
            recent = [r for r in self._records if r.recorded_at >= cutoff]

        agg: Dict[tuple, dict] = defaultdict(lambda: {"total_cost_usd": 0.0, "call_count": 0})
        for rec in recent:
            key = rec.attribution.tag_key()
            agg[key]["total_cost_usd"] += rec.total_cost_usd
            agg[key]["call_count"] += 1

        sorted_keys = sorted(agg.keys(), key=lambda k: -agg[k]["total_cost_usd"])[:n]
        return [
            {
                "feature": k[0],
                "user_tier": k[1],
                "product_area": k[2],
                "total_cost_usd": round(agg[k]["total_cost_usd"], 4),
                "call_count": agg[k]["call_count"],
            }
            for k in sorted_keys
        ]
```

## Solution 4: Attributed LLM Client Wrapper

```python
from typing import Any, Callable, Dict, Optional


class AttributedLLMClient:
    """
    Wraps an LLM client and automatically records attributed cost records
    for every completion call. Attribution context is passed per-call.
    """

    def __init__(
        self,
        base_client: Any,
        cost_store: CostAttributionStore,
    ):
        self._client = base_client
        self._store = cost_store

    async def complete(
        self,
        messages: list,
        model: str,
        attribution: CostAttributionContext,
        **kwargs,
    ) -> Any:
        response = await self._client.complete(messages=messages, model=model, **kwargs)

        usage = getattr(response, "usage", None) or {}
        if hasattr(usage, "__dict__"):
            usage = usage.__dict__

        input_tokens = usage.get("input_tokens", usage.get("prompt_tokens", 0))
        output_tokens = usage.get("output_tokens", usage.get("completion_tokens", 0))

        record = AttributedCostRecord.from_usage(
            attribution=attribution,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
        self._store.record(record)
        return response
```

## Solution 5: Cost Budget Enforcer

```python
from typing import Optional


class FeatureCostBudgetEnforcer:
    """
    Enforces per-feature cost budgets within a time window.
    Raises FeatureBudgetExceededError when a feature's spending
    exceeds its configured limit.
    """

    def __init__(
        self,
        store: CostAttributionStore,
        budgets: Dict[str, float],   # feature -> daily budget in USD
        window_seconds: float = 86400.0,
    ):
        self._store = store
        self._budgets = budgets
        self._window = window_seconds

    def check(self, attribution: CostAttributionContext) -> None:
        budget = self._budgets.get(attribution.feature)
        if budget is None:
            return
        agg = self._store.aggregate_by("feature", self._window)
        current_spend = agg.get(attribution.feature, {}).get("total_cost_usd", 0.0)
        if current_spend >= budget:
            raise FeatureBudgetExceededError(
                feature=attribution.feature,
                current_usd=current_spend,
                budget_usd=budget,
            )


class FeatureBudgetExceededError(Exception):
    def __init__(self, feature: str, current_usd: float, budget_usd: float):
        self.feature = feature
        self.current_usd = current_usd
        self.budget_usd = budget_usd
        super().__init__(
            f"feature '{feature}' has spent ${current_usd:.4f} "
            f"exceeding budget ${budget_usd:.4f}"
        )
```

## Solution 6: Cost Attribution Dashboard

```python
import time
from typing import Optional


class CostAttributionDashboard:
    """
    Renders a full cost breakdown report for the last 24 hours,
    sliced by feature, user_tier, product_area, and model.
    """

    def __init__(self, store: CostAttributionStore):
        self._store = store

    def render(self, window_seconds: float = 86400.0) -> dict:
        return {
            "generated_at": time.time(),
            "window_seconds": window_seconds,
            "by_feature": self._store.aggregate_by("feature", window_seconds),
            "by_user_tier": self._store.aggregate_by("user_tier", window_seconds),
            "by_product_area": self._store.aggregate_by("product_area", window_seconds),
            "by_model": self._store.aggregate_by("model", window_seconds),
            "top_contributors": self._store.top_contributors(10, window_seconds),
        }
```

## Comparison

| Approach | Per-Call Tagging | Dimension Aggregation | Budget Enforcement | Top Contributors | Dashboard |
|---|---|---|---|---|---|
| CostAttributionContext | Yes | No | No | No | No |
| AttributedCostRecord | Yes (with cost calc) | No | No | No | No |
| CostAttributionStore | Via records | Yes (any dimension) | No | Yes | No |
| AttributedLLMClient | Yes (auto-record) | Via store | No | No | No |
| FeatureCostBudgetEnforcer | No | Via store | Yes | No | No |
| CostAttributionDashboard | No | Via store | No | Via store | Yes |

**Best for production**: Tag every LLM call at the point of invocation — retrofitting attribution onto existing log data is unreliable. Use three dimensions at minimum: `feature` (what the agent is doing), `user_tier` (who is paying), and `model` (what you are paying for). Emit `CostAttributionDashboard.render()` daily to a Slack channel or metrics system so product and engineering see cost trends before they become billing surprises. Set `FeatureCostBudgetEnforcer` budgets at 120% of the previous month's per-feature spend to catch unexpected growth without blocking normal traffic.
