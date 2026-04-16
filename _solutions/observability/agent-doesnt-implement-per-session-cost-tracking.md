---
title: "Agent Doesn't Implement Per-Session Cost Tracking"
description: "Agents that never measure token consumption per session cannot detect runaway sessions, enforce per-user spend limits, or attribute infrastructure costs to features. A single misbehaving session with a looping tool call can consume thousands of dollars of API budget before anyone notices. Implement per-session cost tracking that accumulates token usage across all LLM calls and tool invocations in a session, enforces spend limits, and surfaces cost-per-session metrics for budgeting and anomaly detection."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-per-session-cost-tracking
tags: [cost-tracking, token-usage, session-budget, spend-limit, llm-cost, observability]
symptoms:
  - "No visibility into how much a single user session costs in API tokens"
  - "Runaway sessions with looping tool calls exhaust monthly API budget undetected"
  - "Cannot attribute LLM infrastructure cost to specific features or user cohorts"
  - "Cost anomalies discovered only at the end of the billing cycle"
  - "No per-session spend limit — any session can consume unlimited tokens"
---

## Why This Happens

LLM API costs are usage-based and accumulate per token. Agents that make multiple LLM calls per user session — for planning, tool use, reflection, and response generation — can consume thousands of tokens per task. Without per-session accounting, there is no mechanism to detect that one session has spent 100× the average, to enforce a per-user budget, or to correlate high cost with specific agent behaviors. Cost tracking requires capturing input and output token counts from each API response, mapping them to a session identifier, and accumulating totals with price-per-token conversion.

## Solution 1: Token Usage Record

```python
from dataclasses import dataclass, field
from typing import Optional
import time


@dataclass
class ModelPricing:
    model_id: str
    input_price_per_1k: float     # USD per 1000 input tokens
    output_price_per_1k: float    # USD per 1000 output tokens
    cache_read_price_per_1k: float = 0.0
    cache_write_price_per_1k: float = 0.0


@dataclass
class TokenUsageRecord:
    session_id: str
    call_id: str
    model_id: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0
    recorded_at: float = field(default_factory=time.time)
    call_purpose: str = ""        # "planning" | "tool_use" | "response" | "reflection"

    def compute_cost(self, pricing: ModelPricing) -> float:
        cost = (
            self.input_tokens / 1000.0 * pricing.input_price_per_1k
            + self.output_tokens / 1000.0 * pricing.output_price_per_1k
            + self.cache_read_tokens / 1000.0 * pricing.cache_read_price_per_1k
            + self.cache_write_tokens / 1000.0 * pricing.cache_write_price_per_1k
        )
        self.cost_usd = round(cost, 6)
        return self.cost_usd
```

## Solution 2: Model Pricing Registry

```python
from typing import Dict, Optional


class ModelPricingRegistry:
    """
    Maps model IDs to their pricing configurations.
    Provides a fallback estimate for unregistered models.
    """

    def __init__(self):
        self._pricing: Dict[str, ModelPricing] = {}
        self._default = ModelPricing(
            model_id="__default__",
            input_price_per_1k=0.003,
            output_price_per_1k=0.015,
        )

    def register(self, pricing: ModelPricing) -> None:
        self._pricing[pricing.model_id] = pricing

    def get(self, model_id: str) -> ModelPricing:
        for registered_id, pricing in self._pricing.items():
            if model_id.startswith(registered_id) or registered_id in model_id:
                return pricing
        return self._default

    def default_registry(self) -> "ModelPricingRegistry":
        models = [
            ModelPricing("claude-opus-4", 0.015, 0.075, 0.0015, 0.015),
            ModelPricing("claude-sonnet-4", 0.003, 0.015, 0.0003, 0.003),
            ModelPricing("claude-haiku-4", 0.0008, 0.004, 0.00008, 0.0008),
            ModelPricing("gpt-4o", 0.005, 0.015),
            ModelPricing("gpt-4o-mini", 0.00015, 0.0006),
        ]
        for m in models:
            self.register(m)
        return self
```

## Solution 3: Per-Session Cost Accumulator

```python
import time
from collections import defaultdict
from threading import Lock
from typing import Dict, List, Optional


class PerSessionCostAccumulator:
    """
    Accumulates token usage and cost per session.
    Enforces optional per-session spend limits and raises when exceeded.
    """

    def __init__(
        self,
        pricing_registry: ModelPricingRegistry,
        session_spend_limit_usd: Optional[float] = None,
        session_ttl_seconds: float = 3600.0,
    ):
        self._registry = pricing_registry
        self._limit = session_spend_limit_usd
        self._ttl = session_ttl_seconds
        self._sessions: Dict[str, dict] = {}
        self._records: Dict[str, List[TokenUsageRecord]] = defaultdict(list)
        self._lock = Lock()

    def record(self, record: TokenUsageRecord) -> float:
        """Records usage, returns cumulative session cost. Raises if limit exceeded."""
        pricing = self._registry.get(record.model_id)
        record.compute_cost(pricing)

        with self._lock:
            self._evict_expired()
            session = self._sessions.setdefault(record.session_id, {
                "total_cost_usd": 0.0,
                "total_input_tokens": 0,
                "total_output_tokens": 0,
                "call_count": 0,
                "started_at": time.time(),
                "last_active": time.time(),
            })
            session["total_cost_usd"] += record.cost_usd
            session["total_input_tokens"] += record.input_tokens
            session["total_output_tokens"] += record.output_tokens
            session["call_count"] += 1
            session["last_active"] = time.time()
            self._records[record.session_id].append(record)

            cumulative = session["total_cost_usd"]

        if self._limit is not None and cumulative >= self._limit:
            raise SessionSpendLimitExceeded(
                session_id=record.session_id,
                cumulative_usd=cumulative,
                limit_usd=self._limit,
            )
        return cumulative

    def session_summary(self, session_id: str) -> Optional[dict]:
        with self._lock:
            s = self._sessions.get(session_id)
            if not s:
                return None
            return {
                "session_id": session_id,
                **s,
                "call_count": s["call_count"],
            }

    def _evict_expired(self) -> None:
        now = time.time()
        expired = [
            sid for sid, s in self._sessions.items()
            if now - s["last_active"] > self._ttl
        ]
        for sid in expired:
            del self._sessions[sid]
            self._records.pop(sid, None)


class SessionSpendLimitExceeded(Exception):
    def __init__(self, session_id: str, cumulative_usd: float, limit_usd: float):
        super().__init__(
            f"Session '{session_id}' exceeded spend limit: "
            f"${cumulative_usd:.4f} >= ${limit_usd:.4f}"
        )
        self.session_id = session_id
        self.cumulative_usd = cumulative_usd
        self.limit_usd = limit_usd
```

## Solution 4: Cost Anomaly Detector

```python
import time
from typing import List, Optional


class SessionCostAnomalyDetector:
    """
    Compares a session's current cost against historical session averages
    to detect runaway sessions before they exhaust the monthly budget.
    """

    def __init__(
        self,
        accumulator: PerSessionCostAccumulator,
        anomaly_multiplier: float = 5.0,  # flag if cost > N× average
        min_sessions_for_baseline: int = 10,
    ):
        self._accum = accumulator
        self._multiplier = anomaly_multiplier
        self._min_baseline = min_sessions_for_baseline
        self._completed_costs: List[float] = []

    def record_completed_session(self, session_id: str) -> None:
        summary = self._accum.session_summary(session_id)
        if summary:
            self._completed_costs.append(summary["total_cost_usd"])

    def check_anomaly(self, session_id: str) -> dict:
        summary = self._accum.session_summary(session_id)
        if not summary:
            return {"status": "unknown_session"}

        current_cost = summary["total_cost_usd"]

        if len(self._completed_costs) < self._min_baseline:
            return {
                "status": "insufficient_baseline",
                "current_cost_usd": current_cost,
                "baseline_sessions": len(self._completed_costs),
            }

        avg = sum(self._completed_costs) / len(self._completed_costs)
        is_anomaly = current_cost > avg * self._multiplier

        return {
            "status": "anomaly" if is_anomaly else "normal",
            "current_cost_usd": round(current_cost, 4),
            "average_session_cost_usd": round(avg, 4),
            "anomaly_multiplier": self._multiplier,
            "is_anomaly": is_anomaly,
        }
```

## Solution 5: Aggregate Cost Reporter

```python
import time
from typing import Dict, List


class AggregateCostReporter:
    """
    Accumulates completed session cost records and reports aggregate
    spend by model, time window, and call purpose for billing attribution.
    """

    def __init__(self):
        self._completed: List[dict] = []

    def record_session_end(
        self,
        session_id: str,
        accumulator: PerSessionCostAccumulator,
        records_by_purpose: Optional[Dict[str, float]] = None,
    ) -> None:
        summary = accumulator.session_summary(session_id)
        if not summary:
            return
        self._completed.append({
            "ts": time.time(),
            "session_id": session_id,
            "total_cost_usd": summary["total_cost_usd"],
            "total_input_tokens": summary["total_input_tokens"],
            "total_output_tokens": summary["total_output_tokens"],
            "call_count": summary["call_count"],
            "by_purpose": records_by_purpose or {},
        })

    def report(self, window_seconds: float = 86400.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [s for s in self._completed if s["ts"] >= cutoff]
        if not recent:
            return {"window_seconds": window_seconds, "sessions": 0}

        total_cost = sum(s["total_cost_usd"] for s in recent)
        costs = sorted(s["total_cost_usd"] for s in recent)
        p95_idx = min(int(len(costs) * 0.95), len(costs) - 1)

        return {
            "window_seconds": window_seconds,
            "sessions": len(recent),
            "total_cost_usd": round(total_cost, 4),
            "avg_cost_per_session_usd": round(total_cost / len(recent), 4),
            "p95_session_cost_usd": round(costs[p95_idx], 4),
            "max_session_cost_usd": round(max(costs), 4),
        }
```

## Solution 6: Per-Session Cost Dashboard

```python
import time


class PerSessionCostDashboard:
    """
    Combines live session summaries, anomaly detection, and aggregate
    cost reporting into a single operational and billing view.
    """

    def __init__(
        self,
        accumulator: PerSessionCostAccumulator,
        anomaly_detector: SessionCostAnomalyDetector,
        reporter: AggregateCostReporter,
    ):
        self._accum = accumulator
        self._detector = anomaly_detector
        self._reporter = reporter

    def render(self, active_session_ids: list = None) -> dict:
        active_summaries = {}
        anomaly_checks = {}
        for sid in (active_session_ids or []):
            summary = self._accum.session_summary(sid)
            if summary:
                active_summaries[sid] = summary
                anomaly_checks[sid] = self._detector.check_anomaly(sid)

        return {
            "generated_at": time.time(),
            "spend_limit_usd": self._accum._limit,
            "active_sessions": active_summaries,
            "anomaly_checks": anomaly_checks,
            "aggregate_24h": self._reporter.report(86400.0),
            "aggregate_1h": self._reporter.report(3600.0),
        }
```

## Comparison

| Approach | Per-Call Tracking | Spend Limit | Anomaly Detection | Aggregate Reporting | Dashboard |
|---|---|---|---|---|---|
| TokenUsageRecord | Yes (per call) | No | No | No | No |
| PerSessionCostAccumulator | Via records | Yes | No | No | No |
| SessionCostAnomalyDetector | No | No | Yes (baseline) | No | No |
| AggregateCostReporter | No | No | No | Yes | No |
| PerSessionCostDashboard | No | No | No | No | Yes |

**Best for production**: Pull `input_tokens` and `output_tokens` from the API response object after every LLM call — do not estimate from character counts, as batched and cached calls distort estimates significantly. Set `session_spend_limit_usd` based on your p99 session cost from `AggregateCostReporter`: a limit of 5-10× the average catches runaway loops without affecting normal sessions. Register Claude and GPT model IDs in `ModelPricingRegistry` with current prices — update this configuration when providers change pricing rather than hardcoding in multiple places. Monitor `SessionCostAnomalyDetector.check_anomaly()` for active sessions: a session that crosses 5× the average cost in under a minute is almost certainly a tool-call loop, not a complex task, and should be terminated automatically.
