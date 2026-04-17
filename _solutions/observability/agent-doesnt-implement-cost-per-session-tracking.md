---
title: "Agent Doesn't Implement Cost Per Session Tracking"
description: "Agents that aggregate LLM costs at the account level cannot determine which sessions, users, or task types are responsible for cost spikes — making cost optimization blind and unit economics impossible to compute. Implement cost per session tracking that maps every LLM API response to its session, accumulates token costs using provider price tables, and surfaces per-user and per-task-type cost breakdowns."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-cost-per-session-tracking
tags: [cost-tracking, token-cost, session-economics, llm-cost, unit-economics, billing-observability]
symptoms:
  - "Monthly LLM bill doubled but no visibility into which feature or user drove the increase"
  - "Cannot compute cost per conversation or cost per task completion"
  - "High-token sessions from a single power user inflate average costs invisibly"
  - "No alerting when a session exceeds a cost budget — discovered only on the monthly invoice"
  - "Unit economics (cost per successful task) cannot be calculated without per-session attribution"
---

## Why This Happens

LLM providers bill at the account level. Every API response includes token usage but the session context is not passed to the provider — it exists only in the agent. Without an interception layer that captures token counts per response and attributes them to a session, all cost data is lost at the call boundary. Cost per session requires three components: a price table keyed on model and token type (input/output/cached), a session cost accumulator that sums charges across all LLM calls within a session, and a reporting layer that computes aggregates by user, task type, and time window.

## Solution 1: Provider Price Table

```python
from dataclasses import dataclass, field
from typing import Dict, Optional


@dataclass
class ModelPricing:
    model_id: str
    input_cost_per_1k_tokens: float       # USD
    output_cost_per_1k_tokens: float      # USD
    cache_write_cost_per_1k_tokens: float = 0.0
    cache_read_cost_per_1k_tokens: float = 0.0


# Approximate pricing — update from provider docs
DEFAULT_PRICE_TABLE: Dict[str, ModelPricing] = {
    "claude-opus-4-6": ModelPricing(
        model_id="claude-opus-4-6",
        input_cost_per_1k_tokens=0.015,
        output_cost_per_1k_tokens=0.075,
        cache_write_cost_per_1k_tokens=0.01875,
        cache_read_cost_per_1k_tokens=0.0015,
    ),
    "claude-sonnet-4-6": ModelPricing(
        model_id="claude-sonnet-4-6",
        input_cost_per_1k_tokens=0.003,
        output_cost_per_1k_tokens=0.015,
        cache_write_cost_per_1k_tokens=0.00375,
        cache_read_cost_per_1k_tokens=0.0003,
    ),
    "claude-haiku-4-5-20251001": ModelPricing(
        model_id="claude-haiku-4-5-20251001",
        input_cost_per_1k_tokens=0.0008,
        output_cost_per_1k_tokens=0.004,
        cache_write_cost_per_1k_tokens=0.001,
        cache_read_cost_per_1k_tokens=0.00008,
    ),
}


class ProviderPriceTable:
    def __init__(self, pricing: Dict[str, ModelPricing] = None):
        self._table = pricing or DEFAULT_PRICE_TABLE

    def compute_cost(
        self,
        model_id: str,
        input_tokens: int,
        output_tokens: int,
        cache_write_tokens: int = 0,
        cache_read_tokens: int = 0,
    ) -> float:
        pricing = self._table.get(model_id)
        if not pricing:
            # Unknown model — use a conservative default
            pricing = ModelPricing(
                model_id=model_id,
                input_cost_per_1k_tokens=0.01,
                output_cost_per_1k_tokens=0.03,
            )
        cost = (
            input_tokens * pricing.input_cost_per_1k_tokens / 1000
            + output_tokens * pricing.output_cost_per_1k_tokens / 1000
            + cache_write_tokens * pricing.cache_write_cost_per_1k_tokens / 1000
            + cache_read_tokens * pricing.cache_read_cost_per_1k_tokens / 1000
        )
        return round(cost, 8)
```

## Solution 2: LLM Call Cost Record

```python
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class LLMCallCostRecord:
    session_id: str
    user_id: str
    model_id: str
    task_type: str           # e.g. "chat", "tool_use", "summarization"
    input_tokens: int
    output_tokens: int
    cache_write_tokens: int
    cache_read_tokens: int
    cost_usd: float
    call_id: str = ""
    recorded_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if not self.call_id:
            import hashlib
            self.call_id = hashlib.sha256(
                f"{self.session_id}:{self.recorded_at}".encode()
            ).hexdigest()[:12]
```

## Solution 3: Session Cost Accumulator

```python
import time
from collections import defaultdict
from dataclasses import dataclass, field
from threading import Lock
from typing import Dict, List, Optional


@dataclass
class SessionCostSummary:
    session_id: str
    user_id: str
    total_cost_usd: float
    total_input_tokens: int
    total_output_tokens: int
    call_count: int
    started_at: float
    last_call_at: float
    task_types: Dict[str, float]   # task_type -> cost


class SessionCostAccumulator:
    """
    Accumulates LLM call costs per session.
    Supports per-session, per-user, and per-task-type queries.
    """

    def __init__(self, max_sessions: int = 10000):
        self._max = max_sessions
        self._records: Dict[str, List[LLMCallCostRecord]] = defaultdict(list)
        self._lock = Lock()

    def record(self, cost_record: LLMCallCostRecord) -> None:
        with self._lock:
            if (
                len(self._records) >= self._max
                and cost_record.session_id not in self._records
            ):
                # Evict oldest session
                oldest = min(
                    self._records,
                    key=lambda sid: self._records[sid][-1].recorded_at
                )
                del self._records[oldest]
            self._records[cost_record.session_id].append(cost_record)

    def session_summary(self, session_id: str) -> Optional[SessionCostSummary]:
        with self._lock:
            records = self._records.get(session_id, [])
        if not records:
            return None
        task_costs: Dict[str, float] = defaultdict(float)
        for r in records:
            task_costs[r.task_type] += r.cost_usd
        return SessionCostSummary(
            session_id=session_id,
            user_id=records[0].user_id,
            total_cost_usd=round(sum(r.cost_usd for r in records), 8),
            total_input_tokens=sum(r.input_tokens for r in records),
            total_output_tokens=sum(r.output_tokens for r in records),
            call_count=len(records),
            started_at=records[0].recorded_at,
            last_call_at=records[-1].recorded_at,
            task_types=dict(task_costs),
        )

    def user_total(self, user_id: str, window_seconds: float = 86400.0) -> float:
        cutoff = time.time() - window_seconds
        with self._lock:
            total = 0.0
            for records in self._records.values():
                for r in records:
                    if r.user_id == user_id and r.recorded_at >= cutoff:
                        total += r.cost_usd
        return round(total, 6)

    def top_sessions_by_cost(self, n: int = 10) -> List[SessionCostSummary]:
        with self._lock:
            session_ids = list(self._records.keys())
        summaries = [self.session_summary(sid) for sid in session_ids]
        summaries = [s for s in summaries if s is not None]
        summaries.sort(key=lambda s: s.total_cost_usd, reverse=True)
        return summaries[:n]
```

## Solution 4: Cost Budget Alert Manager

```python
import time
from typing import Callable, Dict, Optional


class SessionCostBudgetAlertManager:
    """
    Monitors session costs against per-session and per-user budgets.
    Fires an alert callback when a budget is exceeded.
    """

    def __init__(
        self,
        session_budget_usd: Optional[float] = None,
        user_daily_budget_usd: Optional[float] = None,
        alert_fn: Optional[Callable[[dict], None]] = None,
    ):
        self._session_budget = session_budget_usd
        self._user_daily_budget = user_daily_budget_usd
        self._alert = alert_fn or self._default_alert
        self._alerted_sessions: Dict[str, float] = {}
        self._alerted_users: Dict[str, float] = {}

    @staticmethod
    def _default_alert(event: dict) -> None:
        import json
        print(json.dumps({"event": "cost_budget_exceeded", **event}))

    def check_session(
        self,
        session_summary: SessionCostSummary,
    ) -> bool:
        if self._session_budget is None:
            return False
        if session_summary.total_cost_usd < self._session_budget:
            return False
        sid = session_summary.session_id
        if sid in self._alerted_sessions:
            return False
        self._alerted_sessions[sid] = time.time()
        self._alert({
            "type": "session_budget",
            "session_id": sid,
            "user_id": session_summary.user_id,
            "cost_usd": session_summary.total_cost_usd,
            "budget_usd": self._session_budget,
        })
        return True

    def check_user(self, user_id: str, total_cost_usd: float) -> bool:
        if self._user_daily_budget is None:
            return False
        if total_cost_usd < self._user_daily_budget:
            return False
        key = f"{user_id}:{time.strftime('%Y-%m-%d')}"
        if key in self._alerted_users:
            return False
        self._alerted_users[key] = time.time()
        self._alert({
            "type": "user_daily_budget",
            "user_id": user_id,
            "cost_usd": total_cost_usd,
            "budget_usd": self._user_daily_budget,
        })
        return True
```

## Solution 5: Cost Interceptor

```python
import time
from typing import Any, Callable, Optional


class LLMCostInterceptor:
    """
    Wraps LLM API calls to extract token usage and compute cost
    for every response, attributing it to the current session.
    """

    def __init__(
        self,
        price_table: ProviderPriceTable,
        accumulator: SessionCostAccumulator,
        alert_manager: Optional[SessionCostBudgetAlertManager] = None,
    ):
        self._prices = price_table
        self._accumulator = accumulator
        self._alerts = alert_manager

    async def call(
        self,
        llm_fn: Callable,
        session_id: str,
        user_id: str,
        model_id: str,
        task_type: str = "chat",
        **kwargs: Any,
    ) -> Any:
        response = await llm_fn(**kwargs)

        # Extract usage — works for Anthropic SDK response format
        usage = getattr(response, "usage", None)
        if usage:
            input_tokens = getattr(usage, "input_tokens", 0)
            output_tokens = getattr(usage, "output_tokens", 0)
            cache_write = getattr(usage, "cache_creation_input_tokens", 0)
            cache_read = getattr(usage, "cache_read_input_tokens", 0)
            cost = self._prices.compute_cost(
                model_id, input_tokens, output_tokens, cache_write, cache_read
            )
            record = LLMCallCostRecord(
                session_id=session_id,
                user_id=user_id,
                model_id=model_id,
                task_type=task_type,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_write_tokens=cache_write,
                cache_read_tokens=cache_read,
                cost_usd=cost,
            )
            self._accumulator.record(record)

            if self._alerts:
                summary = self._accumulator.session_summary(session_id)
                if summary:
                    self._alerts.check_session(summary)
                user_total = self._accumulator.user_total(user_id)
                self._alerts.check_user(user_id, user_total)

        return response
```

## Solution 6: Cost Per Session Dashboard

```python
import time


class CostPerSessionDashboard:
    """
    Renders a cost breakdown report for operational and finance visibility.
    """

    def __init__(
        self,
        accumulator: SessionCostAccumulator,
        price_table: ProviderPriceTable,
    ):
        self._accumulator = accumulator
        self._prices = price_table

    def render(self) -> dict:
        top_sessions = self._accumulator.top_sessions_by_cost(10)
        return {
            "generated_at": time.time(),
            "top_sessions_by_cost": [
                {
                    "session_id": s.session_id,
                    "user_id": s.user_id,
                    "total_cost_usd": s.total_cost_usd,
                    "call_count": s.call_count,
                    "input_tokens": s.total_input_tokens,
                    "output_tokens": s.total_output_tokens,
                    "task_types": s.task_types,
                }
                for s in top_sessions
            ],
        }
```

## Comparison

| Approach | Price Computation | Session Attribution | Budget Alerting | Usage Interception | Dashboard |
|---|---|---|---|---|---|
| ProviderPriceTable | Yes (multi-model) | No | No | No | No |
| SessionCostAccumulator | No | Yes (per-session) | No | No | No |
| SessionCostBudgetAlertManager | No | No | Yes (session+user) | No | No |
| LLMCostInterceptor | Via price table | Via accumulator | Via alert manager | Yes | No |
| CostPerSessionDashboard | No | No | No | No | Yes |

**Best for production**: Update the price table from provider documentation on every model release — costs change frequently and stale prices produce misleading unit economics. Set `session_budget_usd` to 10x the expected average session cost as an anomaly alert threshold rather than a hard cap — you want to investigate outlier sessions, not block legitimate heavy users. Track `task_type` per call so that `top_sessions_by_cost` can show whether a high-cost session was caused by many tool calls, long summaries, or a single runaway agentic loop. Export `CostPerSessionDashboard.render()` to a finance dashboard weekly — the `task_types` breakdown reveals which agent capabilities drive the most cost and where optimization effort is best spent.
