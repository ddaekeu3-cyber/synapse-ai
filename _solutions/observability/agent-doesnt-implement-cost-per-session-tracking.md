---
title: "Agent Doesn't Implement Cost per Session Tracking"
description: "Agents that track total LLM token usage globally cannot identify which sessions, user tiers, or task types are responsible for the majority of API spend. A single expensive session consuming 500k tokens looks identical to 50 normal sessions in aggregate metrics. Implement per-session cost tracking that attributes token usage and estimated dollar spend to individual sessions, enabling cost anomaly detection and per-user billing."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-cost-per-session-tracking
tags: [cost-tracking, token-usage, per-session, billing, spend-attribution, llm-cost]
symptoms:
  - "Total API spend is known but no breakdown by session, user, or task type"
  - "A runaway session consuming 10× normal tokens is invisible in aggregate metrics"
  - "Cannot charge back costs to individual users or departments"
  - "No alerts when a single session exceeds a spend threshold"
  - "Cost optimization is impossible because high-cost usage patterns are unidentifiable"
---

## Why This Happens

LLM API billing is per-token globally. Without session-scoped counters, every token call increments the same global counter regardless of which session, user, or task generated it. Per-session cost tracking requires attaching a session identifier to every LLM call, accumulating token counts per session, and applying per-model pricing to convert token counts to dollar estimates. The session granularity also enables anomaly detection: a session that spends 10× the P99 session cost is a signal worth investigating.

## Solution 1: Model Pricing Registry

```python
from dataclasses import dataclass
from typing import Dict, Optional


@dataclass
class ModelPricing:
    model_id: str
    input_cost_per_million: float    # USD per 1M input tokens
    output_cost_per_million: float   # USD per 1M output tokens
    cached_input_discount: float = 0.50   # fraction of input cost for cache hits


DEFAULT_PRICING: Dict[str, ModelPricing] = {
    "claude-opus-4-6": ModelPricing(
        model_id="claude-opus-4-6",
        input_cost_per_million=15.00,
        output_cost_per_million=75.00,
    ),
    "claude-sonnet-4-6": ModelPricing(
        model_id="claude-sonnet-4-6",
        input_cost_per_million=3.00,
        output_cost_per_million=15.00,
    ),
    "claude-haiku-4-5-20251001": ModelPricing(
        model_id="claude-haiku-4-5-20251001",
        input_cost_per_million=0.80,
        output_cost_per_million=4.00,
    ),
}


class ModelPricingRegistry:
    def __init__(self, pricing: Dict[str, ModelPricing]):
        self._pricing = pricing

    def cost_usd(
        self,
        model_id: str,
        input_tokens: int,
        output_tokens: int,
        cached_input_tokens: int = 0,
    ) -> float:
        p = self._pricing.get(model_id)
        if p is None:
            # Unknown model — use a conservative estimate
            p = ModelPricing(model_id=model_id,
                             input_cost_per_million=5.0,
                             output_cost_per_million=20.0)
        input_cost = (input_tokens - cached_input_tokens) / 1_000_000 * p.input_cost_per_million
        cached_cost = cached_input_tokens / 1_000_000 * p.input_cost_per_million * (1 - p.cached_input_discount)
        output_cost = output_tokens / 1_000_000 * p.output_cost_per_million
        return input_cost + cached_cost + output_cost
```

## Solution 2: Per-Session Cost Accumulator

```python
import time
from dataclasses import dataclass, field
from threading import Lock
from typing import Dict, Optional


@dataclass
class SessionCostRecord:
    session_id: str
    user_id: str = ""
    task_type: str = ""
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cached_tokens: int = 0
    total_cost_usd: float = 0.0
    llm_call_count: int = 0
    started_at: float = field(default_factory=time.time)
    last_updated_at: float = field(default_factory=time.time)

    def cost_per_call(self) -> float:
        if self.llm_call_count == 0:
            return 0.0
        return round(self.total_cost_usd / self.llm_call_count, 6)


class PerSessionCostAccumulator:
    """
    Accumulates token usage and cost per session.
    Thread-safe — designed for concurrent LLM calls within a session.
    """

    def __init__(self, pricing_registry: ModelPricingRegistry):
        self._pricing = pricing_registry
        self._sessions: Dict[str, SessionCostRecord] = {}
        self._lock = Lock()

    def record_call(
        self,
        session_id: str,
        model_id: str,
        input_tokens: int,
        output_tokens: int,
        cached_input_tokens: int = 0,
        user_id: str = "",
        task_type: str = "",
    ) -> float:
        cost = self._pricing.cost_usd(model_id, input_tokens, output_tokens, cached_input_tokens)
        with self._lock:
            if session_id not in self._sessions:
                self._sessions[session_id] = SessionCostRecord(
                    session_id=session_id,
                    user_id=user_id,
                    task_type=task_type,
                )
            rec = self._sessions[session_id]
            rec.total_input_tokens += input_tokens
            rec.total_output_tokens += output_tokens
            rec.total_cached_tokens += cached_input_tokens
            rec.total_cost_usd += cost
            rec.llm_call_count += 1
            rec.last_updated_at = time.time()
        return cost

    def get(self, session_id: str) -> Optional[SessionCostRecord]:
        with self._lock:
            return self._sessions.get(session_id)

    def all_sessions(self) -> list:
        with self._lock:
            return list(self._sessions.values())

    def evict_old(self, max_age_seconds: float = 86400.0) -> int:
        cutoff = time.time() - max_age_seconds
        with self._lock:
            stale = [sid for sid, r in self._sessions.items()
                     if r.last_updated_at < cutoff]
            for sid in stale:
                del self._sessions[sid]
            return len(stale)
```

## Solution 3: Session Cost Anomaly Detector

```python
import time
from typing import List, Optional


class SessionCostAnomalyDetector:
    """
    Detects sessions with anomalously high costs using a rolling
    baseline of recent session costs. Flags sessions exceeding
    the baseline by a configurable multiple.
    """

    def __init__(
        self,
        accumulator: PerSessionCostAccumulator,
        anomaly_multiplier: float = 5.0,
        min_baseline_sessions: int = 20,
    ):
        self._acc = accumulator
        self._multiplier = anomaly_multiplier
        self._min_baseline = min_baseline_sessions

    def _baseline_p95(self, sessions: List[SessionCostRecord]) -> Optional[float]:
        if len(sessions) < self._min_baseline:
            return None
        costs = sorted(s.total_cost_usd for s in sessions)
        idx = min(int(len(costs) * 0.95), len(costs) - 1)
        return costs[idx]

    def scan(self, window_seconds: float = 3600.0) -> List[dict]:
        cutoff = time.time() - window_seconds
        sessions = [s for s in self._acc.all_sessions()
                    if s.last_updated_at >= cutoff]
        p95 = self._baseline_p95(sessions)
        if p95 is None or p95 == 0:
            return []

        threshold = p95 * self._multiplier
        anomalies = []
        for s in sessions:
            if s.total_cost_usd > threshold:
                anomalies.append({
                    "session_id": s.session_id,
                    "user_id": s.user_id,
                    "cost_usd": round(s.total_cost_usd, 6),
                    "threshold_usd": round(threshold, 6),
                    "multiple": round(s.total_cost_usd / p95, 2),
                    "llm_calls": s.llm_call_count,
                })
        return sorted(anomalies, key=lambda a: -a["cost_usd"])
```

## Solution 4: Cost Budget Enforcer

```python
from typing import Optional


class SessionCostBudgetEnforcer:
    """
    Enforces a per-session cost budget. Rejects LLM calls once
    a session has exceeded its budget, preventing runaway spend.
    """

    def __init__(
        self,
        accumulator: PerSessionCostAccumulator,
        default_budget_usd: float = 1.00,
        tier_budgets: Optional[dict] = None,
    ):
        self._acc = accumulator
        self._default = default_budget_usd
        self._tiers = tier_budgets or {}

    def _budget_for(self, user_id: str) -> float:
        return self._tiers.get(user_id, self._default)

    def check(self, session_id: str, user_id: str = "") -> dict:
        rec = self._acc.get(session_id)
        budget = self._budget_for(user_id)
        spent = rec.total_cost_usd if rec else 0.0
        remaining = max(0.0, budget - spent)
        return {
            "allowed": spent < budget,
            "spent_usd": round(spent, 6),
            "budget_usd": budget,
            "remaining_usd": round(remaining, 6),
            "pct_used": round(spent / budget * 100, 1) if budget else 0.0,
        }
```

## Solution 5: Cost Attribution Reporter

```python
import time
from typing import List


class CostAttributionReporter:
    """
    Groups session costs by user_id and task_type for chargebacks
    and cost allocation reports.
    """

    def __init__(self, accumulator: PerSessionCostAccumulator):
        self._acc = accumulator

    def by_user(self, window_seconds: float = 86400.0) -> List[dict]:
        cutoff = time.time() - window_seconds
        sessions = [s for s in self._acc.all_sessions()
                    if s.last_updated_at >= cutoff]
        user_costs: dict = {}
        for s in sessions:
            uid = s.user_id or "anonymous"
            if uid not in user_costs:
                user_costs[uid] = {"user_id": uid, "sessions": 0,
                                   "cost_usd": 0.0, "total_tokens": 0}
            user_costs[uid]["sessions"] += 1
            user_costs[uid]["cost_usd"] += s.total_cost_usd
            user_costs[uid]["total_tokens"] += s.total_input_tokens + s.total_output_tokens
        result = list(user_costs.values())
        for r in result:
            r["cost_usd"] = round(r["cost_usd"], 6)
        return sorted(result, key=lambda r: -r["cost_usd"])

    def by_task_type(self, window_seconds: float = 86400.0) -> List[dict]:
        cutoff = time.time() - window_seconds
        sessions = [s for s in self._acc.all_sessions()
                    if s.last_updated_at >= cutoff]
        task_costs: dict = {}
        for s in sessions:
            tt = s.task_type or "unknown"
            if tt not in task_costs:
                task_costs[tt] = {"task_type": tt, "sessions": 0, "cost_usd": 0.0}
            task_costs[tt]["sessions"] += 1
            task_costs[tt]["cost_usd"] += s.total_cost_usd
        result = list(task_costs.values())
        for r in result:
            r["cost_usd"] = round(r["cost_usd"], 6)
        return sorted(result, key=lambda r: -r["cost_usd"])
```

## Solution 6: Cost per Session Dashboard

```python
import time


class CostPerSessionDashboard:
    """Combines spend summary, anomalies, and attribution into one report."""

    def __init__(
        self,
        accumulator: PerSessionCostAccumulator,
        anomaly_detector: SessionCostAnomalyDetector,
        reporter: CostAttributionReporter,
    ):
        self._acc = accumulator
        self._anomaly = anomaly_detector
        self._reporter = reporter

    def render(self, window_seconds: float = 86400.0) -> dict:
        sessions = self._acc.all_sessions()
        total_cost = sum(s.total_cost_usd for s in sessions)
        avg_cost = total_cost / max(len(sessions), 1)
        anomalies = self._anomaly.scan(window_seconds)

        return {
            "generated_at": time.time(),
            "window_seconds": window_seconds,
            "total_sessions": len(sessions),
            "total_cost_usd": round(total_cost, 4),
            "avg_cost_per_session_usd": round(avg_cost, 6),
            "anomalies": anomalies,
            "top_users": self._reporter.by_user(window_seconds)[:10],
            "by_task_type": self._reporter.by_task_type(window_seconds),
        }
```

## Comparison

| Approach | Per-Session Accumulation | Dollar Estimation | Anomaly Detection | Budget Enforcement | Attribution |
|---|---|---|---|---|---|
| ModelPricingRegistry | No | Yes (token→USD) | No | No | No |
| PerSessionCostAccumulator | Yes | Via pricing | No | No | No |
| SessionCostAnomalyDetector | Via accumulator | No | Yes (P95 baseline) | No | No |
| SessionCostBudgetEnforcer | Via accumulator | No | No | Yes | No |
| CostAttributionReporter | Via accumulator | No | No | No | Yes (user+task) |

**Best for production**: Attach `session_id` and `user_id` to every LLM call at the middleware layer — retrofitting this later requires touching every call site. Keep pricing in `ModelPricingRegistry` as a configurable dict rather than hardcoded constants; model prices change frequently. Set session budget thresholds by user tier (`tier_budgets={"free": 0.10, "pro": 2.00}`) and enforce at the LLM client layer before the call — rejecting after the fact is too late. Alert when `anomalies` is non-empty: a session at 5× P95 cost almost always indicates a prompt loop, an unintended long-context call, or an abuse pattern.
