---
title: "Agent Doesn't Implement Token Budget Exhaustion Forecasting"
description: "Agents operating under a monthly or daily token budget that track only current consumption cannot predict when the budget will be exhausted: a usage spike mid-month depletes the budget in week 2 with no warning until the limit hits. Implement token budget exhaustion forecasting that projects consumption forward based on recent velocity and alerts when the projected exhaustion date falls before the budget period ends."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-token-budget-exhaustion-forecasting
tags: [token-budget, exhaustion-forecast, consumption-velocity, cost-forecasting, budget-alert, capacity-planning]
symptoms:
  - "Monthly token budget exhausted on day 18 with no prior warning"
  - "No projection of when the current consumption rate will hit the budget ceiling"
  - "Budget consumption tracked as a snapshot but never extrapolated forward"
  - "Spike in token usage undetected until the 429 budget-exceeded error arrives"
  - "Cannot distinguish a healthy usage increase from an approaching exhaustion event"
---

## Why This Happens

Token budget monitoring that shows "50% consumed" is useful but insufficient. Without a velocity measurement — tokens consumed per hour or per day — there is no way to project when the remaining 50% will be consumed. If the velocity doubled last week, the remaining budget may last only 5 more days instead of the expected 15. Exhaustion forecasting requires a rolling consumption rate, a linear (or exponential) projection, and an alert threshold that fires when the projected exhaustion date falls within a configurable warning window.

## Solution 1: Token Budget Specification

```python
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class TokenBudgetSpec:
    budget_id: str
    total_tokens: int
    period_start: float                       # Unix timestamp
    period_end: float                         # Unix timestamp
    soft_limit_pct: float = 0.80             # warn at 80%
    alert_days_before_exhaustion: float = 3.0  # alert if exhaustion < N days away

    @property
    def period_duration_seconds(self) -> float:
        return self.period_end - self.period_start

    @property
    def period_elapsed_seconds(self) -> float:
        return max(0.0, time.time() - self.period_start)

    @property
    def period_remaining_seconds(self) -> float:
        return max(0.0, self.period_end - time.time())

    @property
    def period_remaining_days(self) -> float:
        return self.period_remaining_seconds / 86400
```

## Solution 2: Token Consumption Velocity Tracker

```python
import time
from collections import deque
from threading import Lock
from typing import Deque, Optional, Tuple


class TokenConsumptionVelocityTracker:
    """
    Tracks token consumption events and computes rolling velocity.
    Uses a sliding window to measure tokens-per-second for projection.
    """

    def __init__(
        self,
        velocity_window_seconds: float = 86400.0,  # 24h window for velocity
        max_events: int = 100000,
    ):
        self._window = velocity_window_seconds
        self._events: Deque[Tuple[float, int]] = deque(maxlen=max_events)
        # (timestamp, token_count)
        self._lock = Lock()
        self._total_consumed = 0

    def record(self, tokens: int) -> None:
        with self._lock:
            self._events.append((time.time(), tokens))
            self._total_consumed += tokens

    def velocity_per_second(self, window_seconds: Optional[float] = None) -> float:
        """Returns tokens consumed per second over the given window."""
        w = window_seconds or self._window
        cutoff = time.time() - w
        with self._lock:
            recent = [(ts, count) for ts, count in self._events if ts >= cutoff]
        if not recent:
            return 0.0
        total = sum(count for _, count in recent)
        actual_window = time.time() - recent[0][0]
        return total / max(actual_window, 1.0)

    def velocity_per_day(self, window_seconds: Optional[float] = None) -> float:
        return self.velocity_per_second(window_seconds) * 86400

    def total_consumed(self) -> int:
        with self._lock:
            return self._total_consumed
```

## Solution 3: Budget Exhaustion Forecaster

```python
import time
from typing import Optional


@dataclasses_or_dataclass
from dataclasses import dataclass

@dataclass
class ExhaustionForecast:
    budget_id: str
    total_tokens: int
    consumed_tokens: int
    remaining_tokens: int
    velocity_per_day: float
    days_remaining_budget: float          # budget period days left
    days_until_exhaustion: Optional[float]  # None if velocity is 0
    will_exhaust_in_period: bool
    exhaustion_date: Optional[float]       # Unix timestamp
    utilization_pct: float
    alert_needed: bool


class TokenBudgetExhaustionForecaster:
    """
    Projects when the remaining token budget will be consumed based on
    current consumption velocity.
    """

    def __init__(
        self,
        spec: TokenBudgetSpec,
        tracker: TokenConsumptionVelocityTracker,
    ):
        self._spec = spec
        self._tracker = tracker

    def forecast(self, velocity_window_seconds: float = 86400.0) -> ExhaustionForecast:
        consumed = self._tracker.total_consumed()
        remaining = max(0, self._spec.total_tokens - consumed)
        velocity = self._tracker.velocity_per_day(velocity_window_seconds)
        utilization = consumed / max(self._spec.total_tokens, 1)
        days_remaining = self._spec.period_remaining_days

        if velocity > 0:
            days_until = remaining / velocity
            exhaustion_ts = time.time() + days_until * 86400
            will_exhaust = days_until < days_remaining
        else:
            days_until = None
            exhaustion_ts = None
            will_exhaust = False

        alert_needed = (
            utilization >= self._spec.soft_limit_pct
            or (
                days_until is not None
                and days_until < self._spec.alert_days_before_exhaustion
                and days_until < days_remaining
            )
        )

        return ExhaustionForecast(
            budget_id=self._spec.budget_id,
            total_tokens=self._spec.total_tokens,
            consumed_tokens=consumed,
            remaining_tokens=remaining,
            velocity_per_day=round(velocity, 0),
            days_remaining_budget=round(days_remaining, 2),
            days_until_exhaustion=round(days_until, 2) if days_until is not None else None,
            will_exhaust_in_period=will_exhaust,
            exhaustion_date=exhaustion_ts,
            utilization_pct=round(utilization * 100, 2),
            alert_needed=alert_needed,
        )
```

## Solution 4: Budget Alert Manager

```python
import time
from typing import Callable, List, Optional


class BudgetExhaustionAlert:
    def __init__(self, forecast: ExhaustionForecast, fired_at: float):
        self.forecast = forecast
        self.fired_at = fired_at

    def message(self) -> str:
        f = self.forecast
        if f.days_until_exhaustion is not None:
            return (
                f"Budget '{f.budget_id}': {f.utilization_pct}% consumed, "
                f"exhaustion in {f.days_until_exhaustion:.1f} days "
                f"({f.days_remaining_budget:.1f} days remain in period). "
                f"Velocity: {f.velocity_per_day:,.0f} tokens/day."
            )
        return f"Budget '{f.budget_id}': {f.utilization_pct}% consumed, velocity near zero."


class BudgetExhaustionAlertManager:
    """
    Fires alerts when exhaustion forecasts indicate risk.
    Implements cooldown to prevent alert storms.
    """

    def __init__(
        self,
        forecaster: TokenBudgetExhaustionForecaster,
        alert_fn: Optional[Callable[[BudgetExhaustionAlert], None]] = None,
        cooldown_seconds: float = 3600.0,
    ):
        self._forecaster = forecaster
        self._alert_fn = alert_fn
        self._cooldown = cooldown_seconds
        self._last_fired: float = 0.0
        self._alerts: List[BudgetExhaustionAlert] = []

    def evaluate(self) -> Optional[BudgetExhaustionAlert]:
        now = time.time()
        if now - self._last_fired < self._cooldown:
            return None

        forecast = self._forecaster.forecast()
        if not forecast.alert_needed:
            return None

        alert = BudgetExhaustionAlert(forecast, now)
        self._last_fired = now
        self._alerts.append(alert)
        if self._alert_fn:
            self._alert_fn(alert)
        return alert

    def recent_alerts(self, n: int = 10) -> List[dict]:
        return [
            {"fired_at": a.fired_at, "message": a.message()}
            for a in self._alerts[-n:]
        ]
```

## Solution 5: Multi-Budget Portfolio Monitor

```python
from typing import Dict, List


class MultiBudgetPortfolioMonitor:
    """
    Monitors multiple token budgets (e.g. per provider, per team, per model)
    and returns a fleet-wide exhaustion risk report.
    """

    def __init__(self):
        self._forecasters: Dict[str, TokenBudgetExhaustionForecaster] = {}

    def register(self, budget_id: str, forecaster: TokenBudgetExhaustionForecaster) -> None:
        self._forecasters[budget_id] = forecaster

    def at_risk_budgets(self, velocity_window_seconds: float = 86400.0) -> List[dict]:
        at_risk = []
        for budget_id, forecaster in self._forecasters.items():
            forecast = forecaster.forecast(velocity_window_seconds)
            if forecast.alert_needed or forecast.will_exhaust_in_period:
                at_risk.append({
                    "budget_id": budget_id,
                    "utilization_pct": forecast.utilization_pct,
                    "days_until_exhaustion": forecast.days_until_exhaustion,
                    "days_remaining_budget": forecast.days_remaining_budget,
                    "velocity_per_day": forecast.velocity_per_day,
                })
        return sorted(at_risk, key=lambda x: x.get("days_until_exhaustion") or float("inf"))
```

## Solution 6: Token Budget Exhaustion Dashboard

```python
import time


class TokenBudgetExhaustionDashboard:
    """
    Renders current budget status, velocity, forecast, and recent alerts.
    """

    def __init__(
        self,
        forecaster: TokenBudgetExhaustionForecaster,
        alert_manager: BudgetExhaustionAlertManager,
    ):
        self._forecaster = forecaster
        self._alert_manager = alert_manager

    def render(self) -> dict:
        forecast = self._forecaster.forecast()
        return {
            "generated_at": time.time(),
            "budget_id": forecast.budget_id,
            "utilization_pct": forecast.utilization_pct,
            "consumed_tokens": forecast.consumed_tokens,
            "remaining_tokens": forecast.remaining_tokens,
            "velocity_per_day": forecast.velocity_per_day,
            "days_remaining_in_period": forecast.days_remaining_budget,
            "days_until_exhaustion": forecast.days_until_exhaustion,
            "will_exhaust_in_period": forecast.will_exhaust_in_period,
            "alert_needed": forecast.alert_needed,
            "recent_alerts": self._alert_manager.recent_alerts(5),
        }
```

## Comparison

| Approach | Velocity Measurement | Exhaustion Projection | Alert on Risk | Multi-Budget | Dashboard |
|---|---|---|---|---|---|
| TokenConsumptionVelocityTracker | Yes (rolling window) | No | No | No | No |
| TokenBudgetExhaustionForecaster | Via tracker | Yes (linear) | No | No | No |
| BudgetExhaustionAlertManager | Via forecaster | Via forecaster | Yes (cooldown) | No | No |
| MultiBudgetPortfolioMonitor | Via forecasters | Via forecasters | No | Yes | No |
| TokenBudgetExhaustionDashboard | No | No | No | No | Yes |

**Best for production**: Use a 24-hour velocity window for projections — shorter windows amplify noise from usage spikes; longer windows miss genuine velocity increases. Set `alert_days_before_exhaustion=3.0` for high-criticality budgets: 3 days gives enough time to negotiate a limit increase or throttle non-essential agent operations. Fire alerts at both the soft limit (80% consumed) and the time-based threshold (exhaustion in <3 days) — the soft limit catches slow steady consumption; the time-based threshold catches sudden velocity spikes. Monitor `velocity_per_day` weekly: a 2× velocity increase week-over-week indicates either organic growth or a runaway agent that needs investigation.
