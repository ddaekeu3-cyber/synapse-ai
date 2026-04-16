---
title: "Agent Doesn't Implement Multi-Tenant Cost Attribution Dashboard"
description: "Agents serving multiple tenants with no per-tenant cost tracking make it impossible to bill accurately, identify wasteful usage, or enforce per-tenant budgets. Implement multi-tenant cost attribution to record every token, API call, and compute event against a tenant identifier — enabling per-tenant spend dashboards, budget enforcement, and anomaly detection."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-multi-tenant-cost-attribution-dashboard
tags: [cost-attribution, multi-tenant, billing, token-tracking, budget-enforcement, observability]
symptoms:
  - "Total LLM spend is known but impossible to break down by customer or team"
  - "One tenant's runaway agent consumes 80% of monthly token budget with no alert"
  - "No mechanism to charge tenants proportionally — flat rate regardless of actual usage"
  - "Finance team asks for per-customer AI cost — no such data exists"
  - "Cannot detect which tenant is driving unexpected API cost spikes"
---

## Why This Happens

Most agent frameworks record aggregate metrics — total tokens, total latency, total errors — without a tenant dimension. Multi-tenant cost attribution requires tagging every chargeable event (LLM call, embedding generation, tool execution, compute time) with a tenant identifier at the point of instrumentation, then aggregating by tenant for dashboards, billing, and budget enforcement. Without this tagging at the source, cost data cannot be reconstructed after the fact.

## Solution 1: Cost Event Model

```python
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

@dataclass
class CostEvent:
    event_id: str = field(default_factory=lambda: str(uuid.uuid4())[:12])
    tenant_id: str = ""
    user_id: str = ""
    session_id: str = ""
    agent_id: str = ""

    # What was consumed
    resource_type: str = ""       # "llm_tokens" | "embedding_tokens" | "tool_call" | "compute_ms"
    model: str = ""               # e.g., "claude-sonnet-4-6", "text-embedding-3-small"
    input_units: float = 0.0      # input tokens, embedding tokens, or ms
    output_units: float = 0.0     # output tokens (0 for non-generative resources)
    unit_label: str = "tokens"    # "tokens" | "ms" | "calls"

    # Pricing
    cost_usd: float = 0.0         # computed at record time
    price_per_input_unit: float = 0.0
    price_per_output_unit: float = 0.0

    timestamp: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "session_id": self.session_id,
            "agent_id": self.agent_id,
            "resource_type": self.resource_type,
            "model": self.model,
            "input_units": self.input_units,
            "output_units": self.output_units,
            "unit_label": self.unit_label,
            "cost_usd": round(self.cost_usd, 8),
            "timestamp": self.timestamp,
            "metadata": self.metadata,
        }
```

## Solution 2: Cost Meter

```python
import time
from dataclasses import dataclass
from typing import Dict, Optional

@dataclass
class ModelPricing:
    model: str
    input_price_per_1k: float    # USD per 1,000 input tokens
    output_price_per_1k: float   # USD per 1,000 output tokens

class CostMeter:
    """
    Records chargeable events per tenant.
    Computes USD cost at record time using a model pricing table.
    Emits CostEvents to registered sinks (database, metrics pipeline).
    """

    DEFAULT_PRICING = [
        ModelPricing("claude-opus-4-6",    0.015, 0.075),
        ModelPricing("claude-sonnet-4-6",  0.003, 0.015),
        ModelPricing("claude-haiku-4-5",   0.00025, 0.00125),
        ModelPricing("text-embedding-3-small", 0.00002, 0.0),
        ModelPricing("text-embedding-3-large", 0.00013, 0.0),
    ]

    def __init__(self):
        self._pricing: Dict[str, ModelPricing] = {
            p.model: p for p in self.DEFAULT_PRICING
        }
        self._sinks = []
        self._total_events = 0
        self._total_cost_usd = 0.0

    def register_model_pricing(self, pricing: ModelPricing) -> None:
        self._pricing[pricing.model] = pricing

    def add_sink(self, sink) -> None:
        self._sinks.append(sink)

    def _compute_cost(self, model: str, input_units: float, output_units: float) -> tuple:
        pricing = self._pricing.get(model)
        if not pricing:
            return 0.0, 0.0, 0.0
        input_cost = (input_units / 1000.0) * pricing.input_price_per_1k
        output_cost = (output_units / 1000.0) * pricing.output_price_per_1k
        return input_cost + output_cost, pricing.input_price_per_1k / 1000, pricing.output_price_per_1k / 1000

    def record_llm_call(
        self,
        tenant_id: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        session_id: str = "",
        user_id: str = "",
        agent_id: str = "",
        metadata: dict = None,
    ) -> CostEvent:
        cost, price_in, price_out = self._compute_cost(model, input_tokens, output_tokens)
        event = CostEvent(
            tenant_id=tenant_id,
            user_id=user_id,
            session_id=session_id,
            agent_id=agent_id,
            resource_type="llm_tokens",
            model=model,
            input_units=input_tokens,
            output_units=output_tokens,
            unit_label="tokens",
            cost_usd=cost,
            price_per_input_unit=price_in,
            price_per_output_unit=price_out,
            metadata=metadata or {},
        )
        self._emit(event)
        return event

    def record_tool_call(
        self,
        tenant_id: str,
        tool_name: str,
        cost_usd: float,
        session_id: str = "",
        agent_id: str = "",
    ) -> CostEvent:
        event = CostEvent(
            tenant_id=tenant_id,
            session_id=session_id,
            agent_id=agent_id,
            resource_type="tool_call",
            model=tool_name,
            input_units=1.0,
            unit_label="calls",
            cost_usd=cost_usd,
        )
        self._emit(event)
        return event

    def _emit(self, event: CostEvent) -> None:
        self._total_events += 1
        self._total_cost_usd += event.cost_usd
        for sink in self._sinks:
            try:
                sink(event)
            except Exception as exc:
                print(f"[cost_meter] sink error: {exc}")

    def global_stats(self) -> dict:
        return {
            "total_events": self._total_events,
            "total_cost_usd": round(self._total_cost_usd, 6),
        }
```

## Solution 3: Per-Tenant Cost Aggregator

```python
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional

@dataclass
class TenantCostSummary:
    tenant_id: str
    total_cost_usd: float
    input_tokens: int
    output_tokens: int
    llm_calls: int
    tool_calls: int
    by_model: Dict[str, float]
    by_agent: Dict[str, float]
    period_start: float
    period_end: float

class PerTenantCostAggregator:
    """
    Aggregates CostEvents per tenant for dashboard queries.
    Supports time-windowed queries and per-model/per-agent breakdowns.
    """

    def __init__(self):
        self._events: List[CostEvent] = []

    def ingest(self, event: CostEvent) -> None:
        self._events.append(event)

    def summary(
        self,
        tenant_id: str,
        since: Optional[float] = None,
        until: Optional[float] = None,
    ) -> TenantCostSummary:
        now = time.time()
        since = since or 0.0
        until = until or now

        tenant_events = [
            e for e in self._events
            if e.tenant_id == tenant_id
            and since <= e.timestamp <= until
        ]

        total_cost = sum(e.cost_usd for e in tenant_events)
        input_tokens = int(sum(e.input_units for e in tenant_events if e.resource_type == "llm_tokens"))
        output_tokens = int(sum(e.output_units for e in tenant_events if e.resource_type == "llm_tokens"))
        llm_calls = sum(1 for e in tenant_events if e.resource_type == "llm_tokens")
        tool_calls = sum(1 for e in tenant_events if e.resource_type == "tool_call")

        by_model: Dict[str, float] = defaultdict(float)
        by_agent: Dict[str, float] = defaultdict(float)
        for e in tenant_events:
            by_model[e.model] += e.cost_usd
            if e.agent_id:
                by_agent[e.agent_id] += e.cost_usd

        return TenantCostSummary(
            tenant_id=tenant_id,
            total_cost_usd=round(total_cost, 6),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            llm_calls=llm_calls,
            tool_calls=tool_calls,
            by_model={k: round(v, 6) for k, v in by_model.items()},
            by_agent={k: round(v, 6) for k, v in by_agent.items()},
            period_start=since,
            period_end=until,
        )

    def all_tenants_summary(
        self,
        since: Optional[float] = None,
        until: Optional[float] = None,
    ) -> List[TenantCostSummary]:
        tenant_ids = {e.tenant_id for e in self._events}
        return sorted(
            [self.summary(tid, since, until) for tid in tenant_ids],
            key=lambda s: s.total_cost_usd,
            reverse=True,
        )

    def top_tenants(self, n: int = 10, since: Optional[float] = None) -> List[dict]:
        summaries = self.all_tenants_summary(since=since)
        total = sum(s.total_cost_usd for s in summaries)
        return [
            {
                "rank": i + 1,
                "tenant_id": s.tenant_id,
                "cost_usd": s.total_cost_usd,
                "share_pct": round(s.total_cost_usd / max(total, 1e-9) * 100, 2),
                "llm_calls": s.llm_calls,
            }
            for i, s in enumerate(summaries[:n])
        ]
```

## Solution 4: Budget Enforcer

```python
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

@dataclass
class TenantBudget:
    tenant_id: str
    monthly_limit_usd: float
    daily_limit_usd: Optional[float] = None
    alert_threshold_pct: float = 0.8   # alert at 80% usage
    hard_stop: bool = False            # if True, block calls when limit reached

@dataclass
class BudgetCheckResult:
    tenant_id: str
    allowed: bool
    reason: str
    current_spend_usd: float
    limit_usd: float
    utilization_pct: float

class TenantBudgetEnforcer:
    """
    Checks per-tenant spend against configured budgets before each LLM call.
    Supports monthly and daily limits with configurable alert thresholds.
    Fires alert callbacks when thresholds are crossed.
    """

    def __init__(self, aggregator: PerTenantCostAggregator):
        self._aggregator = aggregator
        self._budgets: Dict[str, TenantBudget] = {}
        self._alert_handlers: List[Callable[[str, BudgetCheckResult], None]] = []
        self._alerted: Dict[str, set] = {}   # tenant_id -> set of fired alert types

    def register_budget(self, budget: TenantBudget) -> None:
        self._budgets[budget.tenant_id] = budget
        self._alerted[budget.tenant_id] = set()

    def add_alert_handler(self, handler: Callable[[str, BudgetCheckResult], None]) -> None:
        self._alert_handlers.append(handler)

    def _month_start(self) -> float:
        now = time.gmtime()
        import calendar
        return time.mktime((now.tm_year, now.tm_mon, 1, 0, 0, 0, 0, 0, 0))

    def _day_start(self) -> float:
        now = time.gmtime()
        return time.mktime((now.tm_year, now.tm_mon, now.tm_mday, 0, 0, 0, 0, 0, 0))

    def check(self, tenant_id: str, estimated_cost_usd: float = 0.0) -> BudgetCheckResult:
        budget = self._budgets.get(tenant_id)
        if not budget:
            return BudgetCheckResult(
                tenant_id=tenant_id, allowed=True, reason="no_budget_configured",
                current_spend_usd=0.0, limit_usd=float("inf"), utilization_pct=0.0,
            )

        # Check monthly budget
        monthly_summary = self._aggregator.summary(tenant_id, since=self._month_start())
        monthly_spend = monthly_summary.total_cost_usd + estimated_cost_usd
        monthly_util = monthly_spend / max(budget.monthly_limit_usd, 1e-9)

        if monthly_util >= 1.0 and budget.hard_stop:
            return BudgetCheckResult(
                tenant_id=tenant_id, allowed=False,
                reason=f"monthly_budget_exceeded ({monthly_spend:.4f} >= {budget.monthly_limit_usd})",
                current_spend_usd=monthly_spend,
                limit_usd=budget.monthly_limit_usd,
                utilization_pct=round(monthly_util * 100, 2),
            )

        result = BudgetCheckResult(
            tenant_id=tenant_id, allowed=True, reason="ok",
            current_spend_usd=monthly_spend,
            limit_usd=budget.monthly_limit_usd,
            utilization_pct=round(monthly_util * 100, 2),
        )

        # Fire threshold alert (once per threshold crossing)
        alert_key = f"monthly_{int(budget.alert_threshold_pct * 100)}"
        if (monthly_util >= budget.alert_threshold_pct and
                alert_key not in self._alerted[tenant_id]):
            self._alerted[tenant_id].add(alert_key)
            for handler in self._alert_handlers:
                try:
                    handler("budget_threshold_crossed", result)
                except Exception:
                    pass

        return result
```

## Solution 5: Cost Anomaly Detector

```python
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Deque, Dict, List, Optional

@dataclass
class CostAnomaly:
    tenant_id: str
    anomaly_type: str   # "spend_spike" | "token_burst" | "unusual_model"
    observed_value: float
    baseline_value: float
    z_score: float
    timestamp: float

class TenantCostAnomalyDetector:
    """
    Detects per-tenant cost anomalies by comparing current hourly spend
    to a rolling baseline of the previous N hours.
    Flags sudden spikes that deviate more than Z standard deviations.
    """

    def __init__(self, window_hours: int = 24, z_threshold: float = 3.0):
        self._window = window_hours
        self._z_thresh = z_threshold
        self._hourly_spend: Dict[str, Deque[float]] = defaultdict(
            lambda: deque(maxlen=window_hours)
        )
        self._anomalies: List[CostAnomaly] = []

    def record_hourly_spend(self, tenant_id: str, spend_usd: float) -> Optional[CostAnomaly]:
        history = self._hourly_spend[tenant_id]
        history.append(spend_usd)

        if len(history) < 4:
            return None   # not enough history

        baseline = list(history)[:-1]
        mean = sum(baseline) / len(baseline)
        variance = sum((x - mean) ** 2 for x in baseline) / max(len(baseline) - 1, 1)
        std = variance ** 0.5

        if std < 1e-9:
            return None   # no variance — can't compute z-score

        z = (spend_usd - mean) / std
        if abs(z) < self._z_thresh:
            return None

        anomaly = CostAnomaly(
            tenant_id=tenant_id,
            anomaly_type="spend_spike" if z > 0 else "spend_drop",
            observed_value=round(spend_usd, 6),
            baseline_value=round(mean, 6),
            z_score=round(z, 3),
            timestamp=time.time(),
        )
        self._anomalies.append(anomaly)
        return anomaly

    def recent_anomalies(self, hours: float = 24.0) -> List[CostAnomaly]:
        cutoff = time.time() - hours * 3600
        return [a for a in self._anomalies if a.timestamp >= cutoff]
```

## Solution 6: Cost Attribution Dashboard

```python
import time
from typing import Dict, List, Optional

class CostAttributionDashboard:
    """
    Assembles a full multi-tenant cost attribution report.
    Combines aggregator summaries, budget status, and anomaly alerts
    into a single dashboard payload for UI rendering or alerting.
    """

    def __init__(
        self,
        aggregator: PerTenantCostAggregator,
        enforcer: TenantBudgetEnforcer,
        anomaly_detector: TenantCostAnomalyDetector,
        meter: CostMeter,
    ):
        self._agg = aggregator
        self._enforcer = enforcer
        self._anomaly = anomaly_detector
        self._meter = meter

    def render(
        self,
        since: Optional[float] = None,
        top_n: int = 20,
    ) -> dict:
        now = time.time()
        since = since or (now - 30 * 86400)   # default: last 30 days

        top_tenants = self._agg.top_tenants(n=top_n, since=since)

        # Enrich with budget utilization
        for tenant_row in top_tenants:
            check = self._enforcer.check(tenant_row["tenant_id"])
            tenant_row["budget_utilization_pct"] = check.utilization_pct
            tenant_row["budget_limit_usd"] = check.limit_usd
            tenant_row["budget_allowed"] = check.allowed

        recent_anomalies = self._anomaly.recent_anomalies(hours=24.0)

        return {
            "generated_at": now,
            "period_start": since,
            "period_end": now,
            "global": self._meter.global_stats(),
            "top_tenants": top_tenants,
            "anomalies": [
                {
                    "tenant_id": a.tenant_id,
                    "type": a.anomaly_type,
                    "observed_usd": a.observed_value,
                    "baseline_usd": a.baseline_value,
                    "z_score": a.z_score,
                }
                for a in recent_anomalies
            ],
            "tenants_over_budget": [
                t for t in top_tenants if not t["budget_allowed"]
            ],
        }

    def per_tenant_detail(self, tenant_id: str, since: Optional[float] = None) -> dict:
        now = time.time()
        since = since or (now - 30 * 86400)
        summary = self._agg.summary(tenant_id, since=since)
        budget_check = self._enforcer.check(tenant_id)
        anomalies = [
            a for a in self._anomaly.recent_anomalies(hours=720)
            if a.tenant_id == tenant_id
        ]
        return {
            "tenant_id": tenant_id,
            "summary": {
                "total_cost_usd": summary.total_cost_usd,
                "input_tokens": summary.input_tokens,
                "output_tokens": summary.output_tokens,
                "llm_calls": summary.llm_calls,
                "tool_calls": summary.tool_calls,
            },
            "by_model": summary.by_model,
            "by_agent": summary.by_agent,
            "budget": {
                "limit_usd": budget_check.limit_usd,
                "utilization_pct": budget_check.utilization_pct,
                "allowed": budget_check.allowed,
            },
            "anomalies": [
                {"type": a.anomaly_type, "z_score": a.z_score, "ts": a.timestamp}
                for a in anomalies
            ],
        }
```

## Comparison

| Approach | Event Granularity | Budget Enforcement | Anomaly Detection | Dashboard |
|---|---|---|---|---|
| CostEvent | Per call | No | No | No |
| CostMeter | Per call (with pricing) | No | No | No |
| PerTenantCostAggregator | Aggregated | No | No | Partial |
| TenantBudgetEnforcer | N/A (check only) | Yes | No | No |
| TenantCostAnomalyDetector | Hourly rollup | No | Yes (Z-score) | No |
| CostAttributionDashboard | N/A (assembly) | Via enforcer | Via detector | Yes |

**Best for production**: Instrument every LLM call with `CostMeter.record_llm_call()` — pass `tenant_id` from request context. Connect `PerTenantCostAggregator` as a sink for real-time rollups. Register tenant budgets in `TenantBudgetEnforcer` and call `check()` before each expensive operation. Run `TenantCostAnomalyDetector.record_hourly_spend()` from a cron job every hour. Expose `CostAttributionDashboard.render()` as an internal admin endpoint and alert on `tenants_over_budget` in Slack/PagerDuty.
