---
title: "Agent Doesn't Implement Token Budget Exhaustion Alerting"
description: "Agents that consume LLM tokens without tracking cumulative spend against a budget will exhaust API quotas silently — requests start failing with 429s or billing spikes appear days later with no warning. Implement token budget tracking with real-time consumption monitoring, configurable alert thresholds, and automatic request gating when the budget is nearly exhausted."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-token-budget-exhaustion-alerting
tags: [token-budget, quota-management, cost-alerting, rate-limiting, llm-spend, budget-gating]
symptoms:
  - "LLM API returns 429 quota-exceeded with no prior warning in dashboards"
  - "Monthly billing spikes discovered days after the billing period closes"
  - "No distinction between input, output, and cached token consumption in spend tracking"
  - "Agent continues making LLM calls after the budget threshold is crossed"
  - "No per-model or per-feature budget isolation — one runaway feature exhausts all quota"
---

## Why This Happens

LLM APIs report token consumption per response, but agents rarely accumulate these into a running total against a configured budget. Without a budget tracker, the first signal of quota exhaustion is a 429 error in production — not an alert at 80% consumption. Effective budget management requires accumulating token counts by model and feature, computing projected burn rate, alerting at configurable thresholds (50%, 80%, 95%), and gating new requests once the hard limit is reached. Budget periods (hourly, daily, monthly) must be handled with automatic reset at period boundaries.

## Solution 1: Token Budget Definition

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Optional
import time


class BudgetPeriod(str, Enum):
    HOURLY = "hourly"
    DAILY = "daily"
    MONTHLY = "monthly"
    ROLLING_24H = "rolling_24h"


@dataclass
class TokenBudget:
    budget_id: str
    display_name: str
    period: BudgetPeriod
    max_input_tokens: int
    max_output_tokens: int
    max_total_tokens: int
    alert_thresholds: list = field(default_factory=lambda: [0.5, 0.8, 0.95])
    hard_gate_at: float = 1.0     # gate requests at 100% by default
    model_scope: str = "*"         # "*" = all models, or specific model ID
    feature_scope: str = "*"       # "*" = all features, or specific feature tag

    def period_seconds(self) -> float:
        return {
            BudgetPeriod.HOURLY: 3600.0,
            BudgetPeriod.DAILY: 86400.0,
            BudgetPeriod.MONTHLY: 30 * 86400.0,
            BudgetPeriod.ROLLING_24H: 86400.0,
        }[self.period]
```

## Solution 2: Token Consumption Record

```python
from dataclasses import dataclass, field
import time


@dataclass
class TokenConsumptionRecord:
    model_id: str
    feature_tag: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    request_id: str = ""
    session_id: str = ""
    recorded_at: float = field(default_factory=time.time)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def billable_tokens(self) -> int:
        # Cache reads are cheaper; write tokens counted as input
        return self.input_tokens + self.output_tokens
```

## Solution 3: Token Budget Tracker

```python
import time
from collections import deque
from dataclasses import dataclass
from threading import Lock
from typing import Deque, Dict, List, Optional


@dataclass
class BudgetStatus:
    budget_id: str
    period: BudgetPeriod
    consumed_input: int
    consumed_output: int
    consumed_total: int
    max_total: int
    fraction_used: float
    period_start: float
    period_end: float
    projected_exhaustion_at: Optional[float]
    gated: bool


class TokenBudgetTracker:
    """
    Tracks cumulative token consumption against a budget for the current period.
    Resets automatically at period boundaries. Thread-safe.
    """

    def __init__(self, budget: TokenBudget):
        self._budget = budget
        self._lock = Lock()
        self._period_start = self._compute_period_start()
        self._consumed_input = 0
        self._consumed_output = 0
        self._recent: Deque[TokenConsumptionRecord] = deque(maxlen=10000)

    def _compute_period_start(self) -> float:
        now = time.time()
        import datetime
        dt = datetime.datetime.utcfromtimestamp(now)
        if self._budget.period == BudgetPeriod.HOURLY:
            return datetime.datetime(dt.year, dt.month, dt.day, dt.hour).timestamp()
        elif self._budget.period in (BudgetPeriod.DAILY, BudgetPeriod.ROLLING_24H):
            return datetime.datetime(dt.year, dt.month, dt.day).timestamp()
        elif self._budget.period == BudgetPeriod.MONTHLY:
            return datetime.datetime(dt.year, dt.month, 1).timestamp()
        return now

    def _maybe_reset(self) -> None:
        now = time.time()
        period_end = self._period_start + self._budget.period_seconds()
        if now >= period_end:
            self._period_start = self._compute_period_start()
            self._consumed_input = 0
            self._consumed_output = 0

    def record(self, record: TokenConsumptionRecord) -> None:
        with self._lock:
            self._maybe_reset()
            if self._budget.model_scope != "*" and record.model_id != self._budget.model_scope:
                return
            if self._budget.feature_scope != "*" and record.feature_tag != self._budget.feature_scope:
                return
            self._consumed_input += record.input_tokens
            self._consumed_output += record.output_tokens
            self._recent.append(record)

    def status(self) -> BudgetStatus:
        with self._lock:
            self._maybe_reset()
            total = self._consumed_input + self._consumed_output
            fraction = total / max(self._budget.max_total_tokens, 1)
            period_end = self._period_start + self._budget.period_seconds()

            # Burn rate projection
            elapsed = time.time() - self._period_start
            if elapsed > 0 and total > 0:
                rate = total / elapsed
                remaining = self._budget.max_total_tokens - total
                exhaustion_at = time.time() + (remaining / rate) if rate > 0 else None
            else:
                exhaustion_at = None

            return BudgetStatus(
                budget_id=self._budget.budget_id,
                period=self._budget.period,
                consumed_input=self._consumed_input,
                consumed_output=self._consumed_output,
                consumed_total=total,
                max_total=self._budget.max_total_tokens,
                fraction_used=round(fraction, 6),
                period_start=self._period_start,
                period_end=period_end,
                projected_exhaustion_at=exhaustion_at,
                gated=fraction >= self._budget.hard_gate_at,
            )
```

## Solution 4: Budget Alert Manager

```python
import time
from typing import Callable, Dict, List, Optional, Set


class BudgetAlertManager:
    """
    Fires alert callbacks when token budget consumption crosses configured
    thresholds. Each threshold fires at most once per budget period.
    """

    def __init__(
        self,
        budget: TokenBudget,
        tracker: TokenBudgetTracker,
        alert_fn: Callable[[dict], None],
    ):
        self._budget = budget
        self._tracker = tracker
        self._alert_fn = alert_fn
        self._fired_thresholds: Set[float] = set()
        self._current_period_start: float = 0.0

    def check_and_alert(self) -> List[dict]:
        status = self._tracker.status()

        # Reset fired thresholds at period boundary
        if status.period_start != self._current_period_start:
            self._fired_thresholds.clear()
            self._current_period_start = status.period_start

        fired = []
        for threshold in sorted(self._budget.alert_thresholds):
            if status.fraction_used >= threshold and threshold not in self._fired_thresholds:
                self._fired_thresholds.add(threshold)
                alert = {
                    "event": "budget_threshold_crossed",
                    "budget_id": self._budget.budget_id,
                    "threshold_pct": round(threshold * 100),
                    "fraction_used": status.fraction_used,
                    "consumed_total": status.consumed_total,
                    "max_total": status.max_total,
                    "period": self._budget.period.value,
                    "gated": status.gated,
                    "projected_exhaustion_at": status.projected_exhaustion_at,
                    "ts": time.time(),
                }
                try:
                    self._alert_fn(alert)
                except Exception:
                    pass
                fired.append(alert)
        return fired
```

## Solution 5: Budget Gate

```python
from dataclasses import dataclass


@dataclass
class BudgetGateDecision:
    allowed: bool
    budget_id: str
    fraction_used: float
    reason: str = ""


class TokenBudgetGate:
    """
    Guards LLM call sites — returns a decision before each call is made.
    Blocks requests when the budget's hard gate threshold is reached.
    Supports a soft-warn mode that logs but does not block.
    """

    def __init__(
        self,
        tracker: TokenBudgetTracker,
        budget: TokenBudget,
        soft_warn_only: bool = False,
    ):
        self._tracker = tracker
        self._budget = budget
        self._soft_warn = soft_warn_only
        self._blocked_count = 0

    def check(self, estimated_tokens: int = 0) -> BudgetGateDecision:
        status = self._tracker.status()

        if status.gated:
            self._blocked_count += 1
            reason = (
                f"budget exhausted: {status.fraction_used:.1%} used "
                f"({status.consumed_total}/{status.max_total} tokens)"
            )
            if self._soft_warn:
                return BudgetGateDecision(
                    allowed=True,
                    budget_id=self._budget.budget_id,
                    fraction_used=status.fraction_used,
                    reason=f"WARN: {reason}",
                )
            return BudgetGateDecision(
                allowed=False,
                budget_id=self._budget.budget_id,
                fraction_used=status.fraction_used,
                reason=reason,
            )

        return BudgetGateDecision(
            allowed=True,
            budget_id=self._budget.budget_id,
            fraction_used=status.fraction_used,
        )

    def blocked_count(self) -> int:
        return self._blocked_count
```

## Solution 6: Token Budget Dashboard

```python
import time
from typing import Dict, List


class TokenBudgetDashboard:
    """
    Aggregates status across multiple budget trackers and gates
    for a unified view of token spend across models and features.
    """

    def __init__(
        self,
        trackers: Dict[str, TokenBudgetTracker],
        gates: Dict[str, TokenBudgetGate],
        alert_managers: Dict[str, BudgetAlertManager],
    ):
        self._trackers = trackers
        self._gates = gates
        self._alert_managers = alert_managers

    def render(self) -> dict:
        statuses = {}
        for budget_id, tracker in self._trackers.items():
            status = tracker.status()
            gate = self._gates.get(budget_id)
            statuses[budget_id] = {
                "fraction_used": status.fraction_used,
                "consumed_total": status.consumed_total,
                "max_total": status.max_total,
                "period": status.period.value,
                "gated": status.gated,
                "blocked_requests": gate.blocked_count() if gate else 0,
                "projected_exhaustion_at": status.projected_exhaustion_at,
            }

        return {
            "generated_at": time.time(),
            "budgets": statuses,
            "any_gated": any(s["gated"] for s in statuses.values()),
        }
```

## Comparison

| Approach | Consumption Tracking | Period Reset | Threshold Alerts | Request Gating | Multi-Budget |
|---|---|---|---|---|---|
| TokenBudgetTracker | Yes (input/output) | Yes (auto) | No | No | No |
| BudgetAlertManager | Via tracker | Via tracker | Yes (multi-threshold) | No | No |
| TokenBudgetGate | Via tracker | Via tracker | No | Yes (hard + soft) | No |
| TokenBudgetDashboard | Via trackers | Via trackers | Via managers | Via gates | Yes |

**Best for production**: Set three budgets: one per-model (e.g. claude-opus-4-6 capped at 10M tokens/day), one per-feature (e.g. summarization capped at 2M/day), and one global daily cap. Alert at 50%, 80%, and 95% — the 50% alert is informational, the 80% alert is actionable, and the 95% alert means gating is imminent. Use `soft_warn_only=True` for internal tooling where blocking would break workflows, and `soft_warn_only=False` for user-facing features. Persist `TokenConsumptionRecord` to a database for month-end reconciliation against the LLM provider invoice — the in-memory tracker is authoritative for gating but lossy across restarts.
