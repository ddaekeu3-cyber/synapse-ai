---
title: "Agent Doesn't Implement Error Budget Tracking for SLO Compliance"
description: "Agents that measure uptime and error rate but do not compute error budgets cannot answer whether they are on track to meet their SLOs for the month: a 2% error rate today may consume the entire monthly error budget in a week. Implement error budget tracking that converts SLO targets into a rolling budget of allowable failures, tracks actual consumption against that budget, and surfaces burn rate alerts when the agent is on pace to exhaust the budget before the period ends."
date: 2026-04-16
difficulty: advanced
category: observability
slug: agent-doesnt-implement-error-budget-tracking-for-slo-compliance
tags: [error-budget, slo, burn-rate, reliability-targets, budget-exhaustion, sre-observability]
symptoms:
  - "Error rate is monitored but no error budget calculation exists"
  - "Cannot determine whether current error rate will exhaust the monthly budget"
  - "SLO compliance is checked at end of month — no early warning when budget burns fast"
  - "No distinction between slow budget consumption (sustainable) and fast burns (incident)"
  - "On-call alerts fire on error rate spikes but not on cumulative budget burn"
---

## Why This Happens

An SLO is a commitment about a period, not a moment. A 99.5% monthly availability SLO means the service can be unavailable for about 3.6 hours per month. Monitoring instantaneous error rate misses the cumulative picture: a 1% error rate sustained for 5 days consumes more budget than a 10% spike that lasts 10 minutes. Error budget tracking converts the SLO into a count of allowable failures per period, tracks each failure against that budget, and alerts on burn rate — the rate at which the budget is being consumed — rather than just the raw error rate. A high burn rate means the SLO will be violated before the period ends, giving teams time to intervene.

## Solution 1: SLO Definition

```python
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class SLOPeriod(str, Enum):
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    ROLLING_30D = "rolling_30d"


@dataclass
class SLODefinition:
    name: str
    target_success_rate: float         # e.g. 0.995 for 99.5%
    period: SLOPeriod
    total_requests_estimate: Optional[int] = None   # expected requests in period

    @property
    def error_rate_budget(self) -> float:
        return 1.0 - self.target_success_rate

    def period_seconds(self) -> float:
        mapping = {
            SLOPeriod.DAILY: 86400.0,
            SLOPeriod.WEEKLY: 604800.0,
            SLOPeriod.MONTHLY: 2592000.0,
            SLOPeriod.ROLLING_30D: 2592000.0,
        }
        return mapping[self.period]

    def allowable_downtime_seconds(self) -> float:
        return self.period_seconds() * self.error_rate_budget

    def allowable_failures(self, total_requests: int) -> int:
        return int(total_requests * self.error_rate_budget)
```

## Solution 2: Error Budget State

```python
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ErrorBudgetState:
    slo_name: str
    period_start: float
    total_requests: int = 0
    total_errors: int = 0
    budget_consumed_pct: float = 0.0
    burn_rate_1h: float = 0.0       # budget consumed per hour (relative to period)
    burn_rate_6h: float = 0.0
    updated_at: float = field(default_factory=time.time)

    def success_rate(self) -> float:
        if self.total_requests == 0:
            return 1.0
        return round(1.0 - (self.total_errors / self.total_requests), 6)

    def time_elapsed_fraction(self, period_seconds: float) -> float:
        elapsed = time.time() - self.period_start
        return min(1.0, elapsed / period_seconds)

    def projected_budget_consumed_eop(self) -> Optional[float]:
        """Projects budget consumption at end of period at current burn rate."""
        elapsed_frac = self.time_elapsed_fraction(1.0)  # placeholder
        if elapsed_frac <= 0:
            return None
        return round(self.budget_consumed_pct / max(elapsed_frac, 0.001), 1)
```

## Solution 3: Error Budget Tracker

```python
import time
import threading
from collections import deque
from typing import Deque, Optional, Tuple


class ErrorBudgetTracker:
    """
    Tracks request outcomes against an SLO definition and maintains
    rolling error counts for burn rate calculation.
    """

    def __init__(
        self,
        slo: SLODefinition,
        window_seconds_1h: float = 3600.0,
        window_seconds_6h: float = 21600.0,
    ):
        self._slo = slo
        self._win_1h = window_seconds_1h
        self._win_6h = window_seconds_6h
        self._events: Deque[Tuple[float, bool]] = deque()  # (ts, is_error)
        self._period_start = time.time()
        self._lock = threading.Lock()

    def record(self, is_error: bool) -> None:
        with self._lock:
            self._events.append((time.time(), is_error))
            self._evict()

    def _evict(self) -> None:
        cutoff = time.time() - self._slo.period_seconds()
        while self._events and self._events[0][0] < cutoff:
            self._events.popleft()

    def _window_stats(self, window_seconds: float) -> Tuple[int, int]:
        cutoff = time.time() - window_seconds
        requests = errors = 0
        for ts, is_error in self._events:
            if ts >= cutoff:
                requests += 1
                if is_error:
                    errors += 1
        return requests, errors

    def _period_stats(self) -> Tuple[int, int]:
        return self._window_stats(self._slo.period_seconds())

    def compute_state(self) -> ErrorBudgetState:
        with self._lock:
            total_req, total_err = self._period_stats()
            req_1h, err_1h = self._window_stats(self._win_1h)
            req_6h, err_6h = self._window_stats(self._win_6h)

        error_budget = self._slo.error_rate_budget
        period_s = self._slo.period_seconds()

        # Budget consumed = actual error rate / budget error rate
        actual_err_rate = total_err / max(total_req, 1)
        budget_consumed_pct = round(actual_err_rate / max(error_budget, 0.0001) * 100, 2)

        # Burn rate = (error_rate_in_window / error_budget) * (period / window)
        err_rate_1h = err_1h / max(req_1h, 1)
        err_rate_6h = err_6h / max(req_6h, 1)
        burn_1h = round(err_rate_1h / max(error_budget, 0.0001) * (period_s / self._win_1h), 4)
        burn_6h = round(err_rate_6h / max(error_budget, 0.0001) * (period_s / self._win_6h), 4)

        state = ErrorBudgetState(
            slo_name=self._slo.name,
            period_start=self._period_start,
            total_requests=total_req,
            total_errors=total_err,
            budget_consumed_pct=budget_consumed_pct,
            burn_rate_1h=burn_1h,
            burn_rate_6h=burn_6h,
        )
        return state
```

## Solution 4: Burn Rate Alerter

```python
from dataclasses import dataclass
from typing import List


@dataclass
class BurnRateAlert:
    severity: str
    burn_rate: float
    window: str
    message: str
    budget_consumed_pct: float


class BurnRateAlerter:
    """
    Evaluates error budget state against burn rate thresholds.
    Uses multi-window burn rate alerts: fast burn on 1h window,
    slower sustained burn on 6h window.
    """

    # Google SRE-style burn rate thresholds
    CRITICAL_BURN_RATE = 14.4    # exhausts monthly budget in 2 hours
    HIGH_BURN_RATE = 6.0         # exhausts monthly budget in 5 hours
    MEDIUM_BURN_RATE = 3.0       # exhausts monthly budget in ~10 hours
    WARN_BUDGET_CONSUMED = 50.0  # consumed >50% of period budget

    def check(self, state: ErrorBudgetState) -> List[BurnRateAlert]:
        alerts = []

        if state.burn_rate_1h >= self.CRITICAL_BURN_RATE:
            alerts.append(BurnRateAlert(
                severity="critical",
                burn_rate=state.burn_rate_1h,
                window="1h",
                message=f"critical burn rate {state.burn_rate_1h:.1f}x — budget exhausted in ~{60/state.burn_rate_1h:.0f}m",
                budget_consumed_pct=state.budget_consumed_pct,
            ))
        elif state.burn_rate_1h >= self.HIGH_BURN_RATE:
            alerts.append(BurnRateAlert(
                severity="high",
                burn_rate=state.burn_rate_1h,
                window="1h",
                message=f"high burn rate {state.burn_rate_1h:.1f}x on 1h window",
                budget_consumed_pct=state.budget_consumed_pct,
            ))

        if state.burn_rate_6h >= self.MEDIUM_BURN_RATE:
            alerts.append(BurnRateAlert(
                severity="medium",
                burn_rate=state.burn_rate_6h,
                window="6h",
                message=f"sustained burn rate {state.burn_rate_6h:.1f}x on 6h window",
                budget_consumed_pct=state.budget_consumed_pct,
            ))

        if state.budget_consumed_pct >= self.WARN_BUDGET_CONSUMED:
            alerts.append(BurnRateAlert(
                severity="warning",
                burn_rate=0.0,
                window="period",
                message=f"budget {state.budget_consumed_pct:.1f}% consumed for this period",
                budget_consumed_pct=state.budget_consumed_pct,
            ))

        return alerts
```

## Solution 5: Multi-SLO Budget Registry

```python
import time
from typing import Dict, List


class MultiSLOBudgetRegistry:
    """
    Manages multiple SLO trackers and alerters in a single registry.
    Supports recording events by SLO name and querying all states at once.
    """

    def __init__(self):
        self._trackers: Dict[str, ErrorBudgetTracker] = {}
        self._alerters: Dict[str, BurnRateAlerter] = {}

    def register(self, slo: SLODefinition) -> None:
        self._trackers[slo.name] = ErrorBudgetTracker(slo)
        self._alerters[slo.name] = BurnRateAlerter()

    def record(self, slo_name: str, is_error: bool) -> None:
        tracker = self._trackers.get(slo_name)
        if tracker:
            tracker.record(is_error)

    def all_states(self) -> Dict[str, ErrorBudgetState]:
        return {name: t.compute_state() for name, t in self._trackers.items()}

    def all_alerts(self) -> Dict[str, List[BurnRateAlert]]:
        result = {}
        for name, tracker in self._trackers.items():
            state = tracker.compute_state()
            alerter = self._alerters[name]
            result[name] = alerter.check(state)
        return result
```

## Solution 6: Error Budget Dashboard

```python
import time


class ErrorBudgetDashboard:
    """
    Renders per-SLO budget states and active burn rate alerts
    into a single operational report.
    """

    def __init__(self, registry: MultiSLOBudgetRegistry):
        self._registry = registry

    def render(self) -> dict:
        states = self._registry.all_states()
        alerts = self._registry.all_alerts()

        slo_summaries = {}
        for name, state in states.items():
            slo_summaries[name] = {
                "success_rate": state.success_rate(),
                "budget_consumed_pct": state.budget_consumed_pct,
                "burn_rate_1h": state.burn_rate_1h,
                "burn_rate_6h": state.burn_rate_6h,
                "total_requests": state.total_requests,
                "total_errors": state.total_errors,
                "alert_count": len(alerts.get(name, [])),
                "highest_severity": (
                    alerts[name][0].severity if alerts.get(name) else "none"
                ),
            }

        all_active_alerts = [
            {**vars(alert), "slo_name": name}
            for name, slo_alerts in alerts.items()
            for alert in slo_alerts
        ]

        return {
            "generated_at": time.time(),
            "slos": slo_summaries,
            "active_alerts": all_active_alerts,
            "slos_at_risk": [
                name for name, s in slo_summaries.items()
                if s["budget_consumed_pct"] > 80 or s["burn_rate_1h"] > 3.0
            ],
        }
```

## Comparison

| Approach | Budget Calculation | Burn Rate | Multi-Window | Multi-SLO | Dashboard |
|---|---|---|---|---|---|
| ErrorBudgetTracker | Yes | Yes (1h + 6h) | Yes | No | No |
| BurnRateAlerter | No | Yes (thresholds) | Yes | No | No |
| MultiSLOBudgetRegistry | Via trackers | Via alerters | Via trackers | Yes | No |
| ErrorBudgetDashboard | No | No | No | Via registry | Yes |

**Best for production**: Use Google SRE-style multi-window burn rate thresholds: alert at 14.4× (2-hour exhaustion) for paging, 6× (5-hour) for ticket creation, and 3× sustained for trend monitoring. Record every agent request outcome — success or error — to the relevant SLO tracker immediately after the response is sent. Define separate SLOs for different agent capabilities (tool-assisted queries, direct responses, streaming sessions) so that a failure in one area does not mask healthy performance in another, and the responsible team gets the right alert.
