---
title: "Agent Doesn't Implement Token Budget Burn Rate Tracking"
description: "Agents that only check total token usage after a session ends cannot predict budget exhaustion mid-session, cannot throttle generation before hitting limits, and cannot alert when a deployment is consuming tokens 3× faster than expected. Implement token budget burn rate tracking that measures per-minute consumption, projects time-to-exhaustion for daily and monthly quotas, and fires alerts when burn rate deviates from baseline."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-token-budget-burn-rate-tracking
tags: [token-budget, burn-rate, cost-tracking, quota-management, token-forecasting, cost-observability]
symptoms:
  - "Daily token quota exhausted at 2pm with no warning that burn rate was accelerating"
  - "No per-session token breakdown — impossible to tell which sessions are expensive"
  - "Monthly cost spikes with no real-time signal to catch it before the billing cycle closes"
  - "Token usage is logged but never compared against a budget or baseline"
  - "Cannot answer 'how many tokens do we have left today?' without querying the billing API"
---

## Why This Happens

Token usage is typically recorded as a counter — total tokens used — without rate analysis. A counter alone cannot answer "are we burning faster than usual?" or "when will we run out?". Burn rate tracking requires a sliding window of recent usage, a budget reference point, and projection logic. Without these, quota exhaustion is always a surprise. Adding burn rate as a first-class metric turns token usage from a post-hoc cost report into a real-time operational signal.

## Solution 1: Token Usage Record

```python
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class TokenUsageRecord:
    session_id: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost_usd: Optional[float] = None
    recorded_at: float = field(default_factory=time.time)
    operation: str = "chat"   # "chat" | "embed" | "vision" | custom

    @classmethod
    def from_api_response(
        cls,
        session_id: str,
        model: str,
        usage: dict,
        cost_per_1k_prompt: float = 0.0,
        cost_per_1k_completion: float = 0.0,
        operation: str = "chat",
    ) -> "TokenUsageRecord":
        prompt = usage.get("prompt_tokens", 0)
        completion = usage.get("completion_tokens", 0)
        cost = (prompt * cost_per_1k_prompt + completion * cost_per_1k_completion) / 1000.0
        return cls(
            session_id=session_id,
            model=model,
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=prompt + completion,
            cost_usd=round(cost, 6),
            operation=operation,
        )
```

## Solution 2: Sliding Window Burn Rate Meter

```python
import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, List, Optional, Tuple


@dataclass
class BurnRateSample:
    tokens: int
    cost_usd: float
    timestamp: float


class SlidingWindowBurnRateMeter:
    """
    Accumulates token usage records in a sliding time window and
    computes tokens-per-minute and cost-per-hour burn rates.
    """

    def __init__(self, window_minutes: float = 60.0):
        self._window_seconds = window_minutes * 60
        self._samples: Deque[BurnRateSample] = deque()

    def record(self, rec: TokenUsageRecord) -> None:
        self._samples.append(BurnRateSample(
            tokens=rec.total_tokens,
            cost_usd=rec.cost_usd or 0.0,
            timestamp=rec.recorded_at,
        ))
        self._trim()

    def _trim(self) -> None:
        cutoff = time.time() - self._window_seconds
        while self._samples and self._samples[0].timestamp < cutoff:
            self._samples.popleft()

    def tokens_per_minute(self) -> float:
        self._trim()
        if not self._samples:
            return 0.0
        total_tokens = sum(s.tokens for s in self._samples)
        elapsed_minutes = self._window_seconds / 60.0
        return round(total_tokens / elapsed_minutes, 2)

    def cost_per_hour(self) -> float:
        self._trim()
        if not self._samples:
            return 0.0
        total_cost = sum(s.cost_usd for s in self._samples)
        elapsed_hours = self._window_seconds / 3600.0
        return round(total_cost / elapsed_hours, 4)

    def window_totals(self) -> dict:
        self._trim()
        return {
            "window_minutes": self._window_seconds / 60,
            "total_tokens": sum(s.tokens for s in self._samples),
            "total_cost_usd": round(sum(s.cost_usd for s in self._samples), 4),
            "sample_count": len(self._samples),
        }
```

## Solution 3: Budget Quota Tracker

```python
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class TokenBudget:
    budget_id: str
    period: str           # "daily" | "hourly" | "monthly"
    token_limit: int
    cost_limit_usd: Optional[float] = None
    reset_at: float = field(default_factory=time.time)
    tokens_used: int = 0
    cost_used_usd: float = 0.0

    def tokens_remaining(self) -> int:
        return max(0, self.token_limit - self.tokens_used)

    def pct_used(self) -> float:
        return round(self.tokens_used / max(self.token_limit, 1) * 100, 2)

    def is_exhausted(self) -> bool:
        return self.tokens_used >= self.token_limit

    def seconds_until_reset(self) -> float:
        period_seconds = {"hourly": 3600, "daily": 86400, "monthly": 2592000}
        period_s = period_seconds.get(self.period, 86400)
        elapsed = time.time() - self.reset_at
        remaining = period_s - (elapsed % period_s)
        return remaining


class BudgetQuotaTracker:
    """
    Accumulates token usage against one or more named budgets.
    Resets budget counters automatically when the period expires.
    """

    def __init__(self):
        self._budgets: dict = {}

    def register(self, budget: TokenBudget) -> None:
        self._budgets[budget.budget_id] = budget

    def consume(self, rec: TokenUsageRecord) -> None:
        for budget in self._budgets.values():
            self._maybe_reset(budget)
            budget.tokens_used += rec.total_tokens
            budget.cost_used_usd += rec.cost_usd or 0.0

    def _maybe_reset(self, budget: TokenBudget) -> None:
        period_seconds = {"hourly": 3600, "daily": 86400, "monthly": 2592000}
        period_s = period_seconds.get(budget.period, 86400)
        if time.time() - budget.reset_at >= period_s:
            budget.tokens_used = 0
            budget.cost_used_usd = 0.0
            budget.reset_at = time.time()

    def status(self, budget_id: str) -> Optional[dict]:
        budget = self._budgets.get(budget_id)
        if not budget:
            return None
        self._maybe_reset(budget)
        return {
            "budget_id": budget_id,
            "period": budget.period,
            "tokens_used": budget.tokens_used,
            "token_limit": budget.token_limit,
            "tokens_remaining": budget.tokens_remaining(),
            "pct_used": budget.pct_used(),
            "cost_used_usd": round(budget.cost_used_usd, 4),
            "exhausted": budget.is_exhausted(),
            "seconds_until_reset": round(budget.seconds_until_reset(), 1),
        }

    def all_statuses(self) -> list:
        return [self.status(bid) for bid in self._budgets]
```

## Solution 4: Burn Rate Forecaster

```python
import time
from typing import Optional


class BurnRateForecaster:
    """
    Projects when each budget will be exhausted based on current burn rate.
    Also detects when burn rate is anomalously high relative to baseline.
    """

    def __init__(
        self,
        meter: SlidingWindowBurnRateMeter,
        quota_tracker: BudgetQuotaTracker,
        baseline_tokens_per_minute: float,
        anomaly_multiplier: float = 3.0,
    ):
        self._meter = meter
        self._tracker = quota_tracker
        self._baseline_tpm = baseline_tokens_per_minute
        self._anomaly_mult = anomaly_multiplier

    def time_to_exhaustion_minutes(self, budget_id: str) -> Optional[float]:
        status = self._tracker.status(budget_id)
        if not status:
            return None
        remaining = status["tokens_remaining"]
        if remaining == 0:
            return 0.0
        tpm = self._meter.tokens_per_minute()
        if tpm <= 0:
            return None
        return round(remaining / tpm, 1)

    def is_anomalous(self) -> bool:
        tpm = self._meter.tokens_per_minute()
        return tpm > self._baseline_tpm * self._anomaly_mult

    def forecast(self) -> dict:
        tpm = self._meter.tokens_per_minute()
        cph = self._meter.cost_per_hour()
        forecasts = []
        for status in self._tracker.all_statuses():
            if not status:
                continue
            tte = self.time_to_exhaustion_minutes(status["budget_id"])
            forecasts.append({
                "budget_id": status["budget_id"],
                "period": status["period"],
                "pct_used": status["pct_used"],
                "exhausted": status["exhausted"],
                "time_to_exhaustion_minutes": tte,
                "seconds_until_reset": status["seconds_until_reset"],
            })
        return {
            "current_tpm": tpm,
            "baseline_tpm": self._baseline_tpm,
            "anomalous_burn_rate": self.is_anomalous(),
            "cost_per_hour_usd": cph,
            "budget_forecasts": forecasts,
        }
```

## Solution 5: Token Budget Alert Manager

```python
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional


@dataclass
class BudgetAlert:
    alert_type: str   # "pct_threshold" | "anomalous_rate" | "exhausted" | "tte_warning"
    budget_id: Optional[str]
    message: str
    severity: str     # "warning" | "critical"
    fired_at: float = field(default_factory=time.time)


class TokenBudgetAlertManager:
    """
    Fires alerts when budget usage crosses thresholds or burn rate is anomalous.
    Uses per-alert-type cooldowns to prevent alert storms.
    """

    def __init__(
        self,
        forecaster: BurnRateForecaster,
        warning_pct: float = 70.0,
        critical_pct: float = 90.0,
        tte_warning_minutes: float = 60.0,
        cooldown_seconds: float = 300.0,
    ):
        self._forecaster = forecaster
        self._warning_pct = warning_pct
        self._critical_pct = critical_pct
        self._tte_warning = tte_warning_minutes
        self._cooldown = cooldown_seconds
        self._last_fired: dict = {}
        self._handlers: List[Callable[[BudgetAlert], None]] = []

    def add_handler(self, fn: Callable[[BudgetAlert], None]) -> None:
        self._handlers.append(fn)

    def _can_fire(self, key: str) -> bool:
        last = self._last_fired.get(key, 0)
        if time.time() - last >= self._cooldown:
            self._last_fired[key] = time.time()
            return True
        return False

    def check(self) -> List[BudgetAlert]:
        forecast = self._forecaster.forecast()
        alerts = []

        if forecast["anomalous_burn_rate"] and self._can_fire("anomalous_rate"):
            alert = BudgetAlert(
                alert_type="anomalous_rate",
                budget_id=None,
                message=(
                    f"Token burn rate {forecast['current_tpm']:.1f} tpm is "
                    f"{forecast['current_tpm'] / max(forecast['baseline_tpm'], 0.01):.1f}× baseline"
                ),
                severity="warning",
            )
            alerts.append(alert)
            for h in self._handlers:
                try:
                    h(alert)
                except Exception:
                    pass

        for bf in forecast["budget_forecasts"]:
            bid = bf["budget_id"]
            pct = bf["pct_used"]
            tte = bf["time_to_exhaustion_minutes"]

            if bf["exhausted"] and self._can_fire(f"{bid}:exhausted"):
                alert = BudgetAlert(
                    alert_type="exhausted",
                    budget_id=bid,
                    message=f"Budget '{bid}' exhausted",
                    severity="critical",
                )
                alerts.append(alert)

            elif pct >= self._critical_pct and self._can_fire(f"{bid}:critical"):
                alert = BudgetAlert(
                    alert_type="pct_threshold",
                    budget_id=bid,
                    message=f"Budget '{bid}' at {pct:.1f}% (critical threshold {self._critical_pct}%)",
                    severity="critical",
                )
                alerts.append(alert)

            elif pct >= self._warning_pct and self._can_fire(f"{bid}:warning"):
                alert = BudgetAlert(
                    alert_type="pct_threshold",
                    budget_id=bid,
                    message=f"Budget '{bid}' at {pct:.1f}% (warning threshold {self._warning_pct}%)",
                    severity="warning",
                )
                alerts.append(alert)

            if tte is not None and tte <= self._tte_warning and not bf["exhausted"]:
                if self._can_fire(f"{bid}:tte"):
                    alert = BudgetAlert(
                        alert_type="tte_warning",
                        budget_id=bid,
                        message=f"Budget '{bid}' will exhaust in {tte:.0f} minutes at current rate",
                        severity="warning",
                    )
                    alerts.append(alert)

            for alert in alerts:
                for h in self._handlers:
                    try:
                        h(alert)
                    except Exception:
                        pass

        return alerts
```

## Solution 6: Token Budget Dashboard

```python
import time


class TokenBudgetDashboard:
    """
    Combines burn rate, budget status, forecasts, and alerts
    into a single observability report.
    """

    def __init__(
        self,
        meter: SlidingWindowBurnRateMeter,
        tracker: BudgetQuotaTracker,
        forecaster: BurnRateForecaster,
        alert_manager: TokenBudgetAlertManager,
    ):
        self._meter = meter
        self._tracker = tracker
        self._forecaster = forecaster
        self._alerts = alert_manager

    def render(self) -> dict:
        window = self._meter.window_totals()
        forecast = self._forecaster.forecast()
        alerts = self._alerts.check()

        return {
            "generated_at": time.time(),
            "burn_rate": {
                "tokens_per_minute": forecast["current_tpm"],
                "baseline_tpm": forecast["baseline_tpm"],
                "anomalous": forecast["anomalous_burn_rate"],
                "cost_per_hour_usd": forecast["cost_per_hour_usd"],
            },
            "window": window,
            "budgets": self._tracker.all_statuses(),
            "forecasts": forecast["budget_forecasts"],
            "active_alerts": [
                {
                    "type": a.alert_type,
                    "budget_id": a.budget_id,
                    "message": a.message,
                    "severity": a.severity,
                }
                for a in alerts
            ],
        }
```

## Comparison

| Approach | Real-Time Rate | Budget Tracking | Exhaustion Forecast | Anomaly Detection | Alerts |
|---|---|---|---|---|---|
| SlidingWindowBurnRateMeter | Yes (sliding window) | No | No | No | No |
| BudgetQuotaTracker | No | Yes (multi-period) | No | No | No |
| BurnRateForecaster | Via meter | Via tracker | Yes (minutes to exhaustion) | Yes | No |
| TokenBudgetAlertManager | Via forecaster | Via forecaster | Via forecaster | Yes | Yes (with cooldown) |
| TokenBudgetDashboard | Yes | Yes | Yes | Yes | Yes |

**Best for production**: Record every API response into `SlidingWindowBurnRateMeter` with a 60-minute window — this smooths over short spikes while still detecting sustained acceleration. Set your `baseline_tokens_per_minute` from the 7-day median in your metrics system and use `anomaly_multiplier=3.0` to catch genuine runaway consumption. Register both a daily and a monthly `TokenBudget` — the daily budget catches same-day runaway; the monthly catches slow creep. Wire `TokenBudgetAlertManager.add_handler()` to PagerDuty for critical alerts and Slack for warnings. Emit `TokenBudgetDashboard.render()` every 5 minutes to your metrics system so on-call engineers can see burn rate trends without waiting for a billing statement.
