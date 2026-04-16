---
title: "Agent Doesn't Implement Cost Forecasting and Budget Alerts"
description: "Six solutions for forecasting AI agent token spend, alerting before budget exhaustion, and enforcing hard spending limits in production."
difficulty: intermediate
category: observability
tags: [cost, budget, forecasting, alerts, token-usage, spend-control]
---

# Agent Doesn't Implement Cost Forecasting and Budget Alerts

Without cost visibility, agents can spend $500 overnight on a runaway loop while the team sleeps. Cost forecasting and budget alerts turn reactive billing surprises into proactive controls: predict when the budget will be exhausted, alert before it happens, and enforce hard limits automatically.

## Solution 1: Rolling Window Cost Tracker with Threshold Alerts

Track spend over a rolling time window; fire callbacks when thresholds are crossed.

```python
import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable
from anthropic import AsyncAnthropic

# Token pricing (USD per 1M tokens)
PRICING = {
    "claude-haiku-4-5-20251001": {"input": 0.80, "output": 4.00},
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00},
    "claude-opus-4-6": {"input": 15.00, "output": 75.00},
}


@dataclass
class CostEvent:
    timestamp: float
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    session_id: str = ""


def compute_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    p = PRICING.get(model, {"input": 3.0, "output": 15.0})
    return input_tokens * p["input"] / 1_000_000 + output_tokens * p["output"] / 1_000_000


@dataclass
class BudgetThreshold:
    name: str
    window_seconds: float   # Rolling window (e.g., 3600 = 1 hour)
    limit_usd: float        # Alert when spend in window exceeds this
    callback: Callable      # Called with (threshold, current_spend)
    triggered: bool = False
    cooldown_seconds: float = 300.0  # Don't re-alert within this period
    last_triggered_at: float = 0.0

    def can_trigger(self) -> bool:
        return time.time() - self.last_triggered_at >= self.cooldown_seconds

    def trigger(self, current_spend: float):
        if self.can_trigger():
            self.triggered = True
            self.last_triggered_at = time.time()
            self.callback(self, current_spend)


class RollingCostTracker:
    def __init__(self, thresholds: list[BudgetThreshold] | None = None):
        self._events: deque[CostEvent] = deque()
        self._thresholds = thresholds or []
        self._total_cost = 0.0
        self._lock = asyncio.Lock()

    async def record(self, model: str, input_tokens: int, output_tokens: int, session_id: str = "") -> float:
        cost = compute_cost(model, input_tokens, output_tokens)
        event = CostEvent(
            timestamp=time.time(),
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
            session_id=session_id,
        )
        async with self._lock:
            self._events.append(event)
            self._total_cost += cost
            self._check_thresholds()
        return cost

    def _prune(self, window_seconds: float) -> list[CostEvent]:
        cutoff = time.time() - window_seconds
        return [e for e in self._events if e.timestamp >= cutoff]

    def _check_thresholds(self):
        for threshold in self._thresholds:
            window_events = self._prune(threshold.window_seconds)
            window_spend = sum(e.cost_usd for e in window_events)
            if window_spend >= threshold.limit_usd:
                threshold.trigger(window_spend)

    def window_spend(self, window_seconds: float) -> float:
        return sum(e.cost_usd for e in self._prune(window_seconds))

    def total_spend(self) -> float:
        return self._total_cost

    def spend_summary(self) -> dict:
        return {
            "total_usd": round(self._total_cost, 4),
            "last_hour_usd": round(self.window_spend(3600), 4),
            "last_10min_usd": round(self.window_spend(600), 4),
            "last_1min_usd": round(self.window_spend(60), 4),
            "event_count": len(self._events),
        }


class BudgetAwareAgent:
    def __init__(self, hourly_limit_usd: float = 10.0, daily_limit_usd: float = 50.0):
        self.client = AsyncAnthropic()

        def on_hourly_alert(threshold: BudgetThreshold, spend: float):
            print(f"[ALERT] Hourly budget {threshold.name}: ${spend:.2f} >= ${threshold.limit_usd}")

        def on_daily_alert(threshold: BudgetThreshold, spend: float):
            print(f"[ALERT] Daily budget {threshold.name}: ${spend:.2f} >= ${threshold.limit_usd}")

        self.tracker = RollingCostTracker(thresholds=[
            BudgetThreshold("hourly_warning", 3600, hourly_limit_usd * 0.8, on_hourly_alert),
            BudgetThreshold("hourly_critical", 3600, hourly_limit_usd, on_hourly_alert),
            BudgetThreshold("daily_warning", 86400, daily_limit_usd * 0.8, on_daily_alert),
            BudgetThreshold("daily_critical", 86400, daily_limit_usd, on_daily_alert),
        ])

    async def chat(self, message: str, model: str = "claude-haiku-4-5-20251001") -> str:
        response = await self.client.messages.create(
            model=model,
            max_tokens=1024,
            messages=[{"role": "user", "content": message}],
        )
        await self.tracker.record(
            model, response.usage.input_tokens, response.usage.output_tokens
        )
        return response.content[0].text

    def cost_summary(self) -> dict:
        return self.tracker.spend_summary()


async def demo_rolling_tracker():
    agent = BudgetAwareAgent(hourly_limit_usd=0.01, daily_limit_usd=0.05)
    messages = ["What is Python?", "Explain async/await.", "What is a semaphore?"]
    for msg in messages:
        await agent.chat(msg)
    print(agent.cost_summary())
```

## Solution 2: Linear Spend Forecast with Time-to-Exhaustion

Fit a linear model to recent spend rate; forecast when the budget will be exhausted and alert ahead of time.

```python
import asyncio
import time
from collections import deque
from dataclasses import dataclass
from anthropic import AsyncAnthropic


@dataclass
class SpendPoint:
    timestamp: float
    cumulative_usd: float


class SpendForecaster:
    def __init__(self, budget_usd: float, window_minutes: float = 30.0):
        self.budget_usd = budget_usd
        self.window_s = window_minutes * 60
        self._points: deque[SpendPoint] = deque()
        self._total = 0.0

    def record(self, cost_usd: float):
        self._total += cost_usd
        now = time.time()
        self._points.append(SpendPoint(now, self._total))
        # Prune old points
        cutoff = now - self.window_s
        while self._points and self._points[0].timestamp < cutoff:
            self._points.popleft()

    def _spend_rate_per_second(self) -> float | None:
        """Linear regression slope: USD/second over the observation window."""
        if len(self._points) < 2:
            return None
        pts = list(self._points)
        n = len(pts)
        t0 = pts[0].timestamp
        xs = [p.timestamp - t0 for p in pts]
        ys = [p.cumulative_usd - pts[0].cumulative_usd for p in pts]

        sum_x = sum(xs)
        sum_y = sum(ys)
        sum_xx = sum(x * x for x in xs)
        sum_xy = sum(x * y for x, y in zip(xs, ys))
        denom = n * sum_xx - sum_x ** 2
        if denom == 0:
            return None
        slope = (n * sum_xy - sum_x * sum_y) / denom
        return max(0.0, slope)

    def forecast(self) -> dict:
        rate = self._spend_rate_per_second()
        remaining = max(0.0, self.budget_usd - self._total)
        if rate is None or rate == 0:
            return {
                "total_spent_usd": round(self._total, 4),
                "budget_usd": self.budget_usd,
                "remaining_usd": round(remaining, 4),
                "spend_rate_per_hour": None,
                "time_to_exhaustion_minutes": None,
                "forecast_confidence": "insufficient_data",
            }
        rate_per_hour = rate * 3600
        time_to_exhaustion_s = remaining / rate if rate > 0 else float("inf")
        return {
            "total_spent_usd": round(self._total, 4),
            "budget_usd": self.budget_usd,
            "remaining_usd": round(remaining, 4),
            "spend_rate_per_hour_usd": round(rate_per_hour, 4),
            "time_to_exhaustion_minutes": round(time_to_exhaustion_s / 60, 1),
            "pct_used": round(self._total / self.budget_usd * 100, 1),
            "forecast_confidence": "linear",
        }

    def will_exhaust_within(self, minutes: float) -> bool:
        f = self.forecast()
        tte = f.get("time_to_exhaustion_minutes")
        return tte is not None and tte <= minutes


class ForecastingAgent:
    def __init__(self, budget_usd: float = 1.0, alert_minutes_ahead: float = 15.0):
        self.client = AsyncAnthropic()
        self.forecaster = SpendForecaster(budget_usd)
        self.alert_ahead = alert_minutes_ahead
        self._alert_sent = False

    async def chat(self, message: str, model: str = "claude-haiku-4-5-20251001") -> str:
        response = await self.client.messages.create(
            model=model,
            max_tokens=1024,
            messages=[{"role": "user", "content": message}],
        )
        cost = compute_cost(model, response.usage.input_tokens, response.usage.output_tokens)
        self.forecaster.record(cost)

        if not self._alert_sent and self.forecaster.will_exhaust_within(self.alert_minutes_ahead):
            f = self.forecaster.forecast()
            print(
                f"[FORECAST ALERT] Budget exhaustion in ~{f['time_to_exhaustion_minutes']:.1f} min "
                f"at ${f['spend_rate_per_hour_usd']:.3f}/hr. "
                f"Spent: ${f['total_spent_usd']:.4f} / ${f['budget_usd']}"
            )
            self._alert_sent = True

        return response.content[0].text

    def forecast(self) -> dict:
        return self.forecaster.forecast()
```

## Solution 3: Hard Budget Enforcer with Circuit Breaker

Stop all LLM calls automatically when the hard budget limit is hit; require explicit reset to resume.

```python
import asyncio
from dataclasses import dataclass, field
from anthropic import AsyncAnthropic


class BudgetExhaustedError(Exception):
    def __init__(self, spent: float, limit: float):
        super().__init__(f"Budget exhausted: ${spent:.4f} >= ${limit:.4f}")
        self.spent = spent
        self.limit = limit


@dataclass
class HardBudgetConfig:
    daily_limit_usd: float = 10.0
    session_limit_usd: float = 1.0
    per_request_limit_usd: float = 0.05
    warning_pct: float = 0.80  # Warn at 80% of limit


class HardBudgetEnforcer:
    def __init__(self, config: HardBudgetConfig):
        self.config = config
        self._daily_spent = 0.0
        self._session_spent = 0.0
        self._request_count = 0
        self._blocked = False
        self._block_reason = ""

    def check(self, estimated_cost: float = 0.0):
        """Raise BudgetExhaustedError if any limit would be breached."""
        if self._blocked:
            raise BudgetExhaustedError(self._daily_spent, self.config.daily_limit_usd)

        if self._daily_spent + estimated_cost >= self.config.daily_limit_usd:
            self._blocked = True
            self._block_reason = "daily_limit"
            raise BudgetExhaustedError(
                self._daily_spent + estimated_cost,
                self.config.daily_limit_usd,
            )
        if self._session_spent + estimated_cost >= self.config.session_limit_usd:
            raise BudgetExhaustedError(
                self._session_spent + estimated_cost,
                self.config.session_limit_usd,
            )
        if estimated_cost >= self.config.per_request_limit_usd:
            raise BudgetExhaustedError(estimated_cost, self.config.per_request_limit_usd)

    def record(self, cost: float):
        self._daily_spent += cost
        self._session_spent += cost
        self._request_count += 1

        daily_pct = self._daily_spent / self.config.daily_limit_usd
        session_pct = self._session_spent / self.config.session_limit_usd

        if daily_pct >= self.config.warning_pct:
            print(
                f"[BUDGET WARNING] Daily: ${self._daily_spent:.4f} / ${self.config.daily_limit_usd} "
                f"({daily_pct:.0%})"
            )
        if session_pct >= self.config.warning_pct:
            print(
                f"[BUDGET WARNING] Session: ${self._session_spent:.4f} / "
                f"${self.config.session_limit_usd} ({session_pct:.0%})"
            )

    def reset_session(self):
        self._session_spent = 0.0

    def reset_daily(self):
        self._daily_spent = 0.0
        self._blocked = False
        self._block_reason = ""

    @property
    def summary(self) -> dict:
        return {
            "daily_spent": round(self._daily_spent, 4),
            "daily_limit": self.config.daily_limit_usd,
            "session_spent": round(self._session_spent, 4),
            "session_limit": self.config.session_limit_usd,
            "request_count": self._request_count,
            "blocked": self._blocked,
            "block_reason": self._block_reason,
        }


class HardLimitAgent:
    def __init__(self, config: HardBudgetConfig | None = None):
        self.client = AsyncAnthropic()
        self.enforcer = HardBudgetEnforcer(config or HardBudgetConfig(
            daily_limit_usd=5.0,
            session_limit_usd=0.50,
            per_request_limit_usd=0.02,
        ))

    async def chat(self, message: str, model: str = "claude-haiku-4-5-20251001") -> str:
        # Estimate cost before making the call (rough heuristic)
        estimated_input = len(message.split()) * 1.3  # ~1.3 tokens per word
        estimated_output = 512
        estimated_cost = compute_cost(model, int(estimated_input), estimated_output)
        self.enforcer.check(estimated_cost)  # Raises if over budget

        response = await self.client.messages.create(
            model=model,
            max_tokens=1024,
            messages=[{"role": "user", "content": message}],
        )
        actual_cost = compute_cost(model, response.usage.input_tokens, response.usage.output_tokens)
        self.enforcer.record(actual_cost)
        return response.content[0].text

    async def safe_chat(self, message: str) -> str | None:
        """Returns None instead of raising when budget is exhausted."""
        try:
            return await self.chat(message)
        except BudgetExhaustedError as e:
            print(f"[BLOCKED] {e}")
            return None
```

## Solution 4: Per-User and Per-Team Budget Allocation

Assign individual spend budgets to users and teams; prevent any single actor from monopolizing the budget.

```python
import asyncio
import time
from collections import defaultdict
from dataclasses import dataclass, field
from anthropic import AsyncAnthropic


@dataclass
class ActorBudget:
    actor_id: str
    daily_limit_usd: float
    _spent: float = 0.0
    _day_start: float = field(default_factory=time.time)

    def _maybe_reset(self):
        if time.time() - self._day_start >= 86400:
            self._spent = 0.0
            self._day_start = time.time()

    def record(self, cost: float):
        self._maybe_reset()
        self._spent += cost

    @property
    def remaining(self) -> float:
        self._maybe_reset()
        return max(0.0, self.daily_limit_usd - self._spent)

    @property
    def exhausted(self) -> bool:
        return self.remaining <= 0.0001

    @property
    def utilization(self) -> float:
        self._maybe_reset()
        return self._spent / max(self.daily_limit_usd, 0.0001)


class PerActorBudgetManager:
    def __init__(
        self,
        user_daily_limit: float = 0.50,
        team_daily_limit: float = 5.0,
        global_daily_limit: float = 20.0,
    ):
        self.user_limit = user_daily_limit
        self.team_limit = team_daily_limit
        self._user_budgets: dict[str, ActorBudget] = {}
        self._team_budgets: dict[str, ActorBudget] = {}
        self._global = ActorBudget("global", global_daily_limit)
        self._user_team_map: dict[str, str] = {}

    def register_user(self, user_id: str, team_id: str, limit: float | None = None):
        self._user_budgets[user_id] = ActorBudget(user_id, limit or self.user_limit)
        self._user_team_map[user_id] = team_id
        if team_id not in self._team_budgets:
            self._team_budgets[team_id] = ActorBudget(team_id, self.team_limit)

    def check_and_record(self, user_id: str, cost: float) -> tuple[bool, str]:
        """Returns (allowed, reason). If not allowed, returns reason."""
        user_budget = self._user_budgets.get(user_id)
        if user_budget and user_budget.exhausted:
            return False, f"User {user_id} daily budget exhausted (${user_budget.daily_limit_usd})"

        team_id = self._user_team_map.get(user_id)
        if team_id:
            team_budget = self._team_budgets.get(team_id)
            if team_budget and team_budget.exhausted:
                return False, f"Team {team_id} daily budget exhausted (${team_budget.daily_limit_usd})"

        if self._global.exhausted:
            return False, f"Global daily budget exhausted (${self._global.daily_limit_usd})"

        # Record the cost
        if user_budget:
            user_budget.record(cost)
        if team_id and team_id in self._team_budgets:
            self._team_budgets[team_id].record(cost)
        self._global.record(cost)
        return True, ""

    def budget_report(self) -> dict:
        return {
            "global": {
                "spent": round(self._global._spent, 4),
                "limit": self._global.daily_limit_usd,
                "utilization_pct": round(self._global.utilization * 100, 1),
            },
            "teams": {
                tid: {
                    "spent": round(b._spent, 4),
                    "limit": b.daily_limit_usd,
                    "utilization_pct": round(b.utilization * 100, 1),
                }
                for tid, b in self._team_budgets.items()
            },
            "users": {
                uid: {
                    "spent": round(b._spent, 4),
                    "limit": b.daily_limit_usd,
                    "exhausted": b.exhausted,
                }
                for uid, b in self._user_budgets.items()
            },
        }


class MultiTenantAgent:
    def __init__(self, budget_manager: PerActorBudgetManager):
        self.client = AsyncAnthropic()
        self.budgets = budget_manager

    async def chat(self, message: str, user_id: str, model: str = "claude-haiku-4-5-20251001") -> str:
        # Pre-check with estimated cost
        estimated_cost = compute_cost(model, len(message), 512)
        allowed, reason = self.budgets.check_and_record(user_id, 0)  # Check only
        if not allowed:
            raise PermissionError(f"Request blocked: {reason}")

        response = await self.client.messages.create(
            model=model,
            max_tokens=1024,
            messages=[{"role": "user", "content": message}],
        )
        actual_cost = compute_cost(model, response.usage.input_tokens, response.usage.output_tokens)
        # Record actual cost (deduct the 0 pre-check and add real cost)
        self.budgets.check_and_record(user_id, actual_cost)
        return response.content[0].text
```

## Solution 5: Anomaly Detection for Runaway Agent Cost Spikes

Detect when spend rate suddenly accelerates beyond normal variance using Z-score analysis.

```python
import asyncio
import math
import statistics
import time
from collections import deque
from dataclasses import dataclass, field
from anthropic import AsyncAnthropic


@dataclass
class SpendWindow:
    window_seconds: float
    _costs: deque = field(default_factory=deque)
    _timestamps: deque = field(default_factory=deque)

    def add(self, cost: float):
        now = time.time()
        self._costs.append(cost)
        self._timestamps.append(now)
        # Prune old entries
        cutoff = now - self.window_seconds
        while self._timestamps and self._timestamps[0] < cutoff:
            self._timestamps.popleft()
            self._costs.popleft()

    @property
    def rate_per_minute(self) -> float:
        if len(self._timestamps) < 2:
            return 0.0
        elapsed = self._timestamps[-1] - self._timestamps[0]
        if elapsed <= 0:
            return 0.0
        return sum(self._costs) / (elapsed / 60)


class CostAnomalyDetector:
    def __init__(
        self,
        baseline_window_minutes: float = 60.0,
        spike_z_threshold: float = 3.0,   # Alert if rate > mean + N*std
        min_samples: int = 10,
    ):
        self._baseline = SpendWindow(baseline_window_minutes * 60)
        self._short = SpendWindow(60.0)  # 1-minute window for current rate
        self._z_threshold = spike_z_threshold
        self._min_samples = min_samples
        self._historical_rates: list[float] = []
        self._anomalies_detected = 0
        self._alert_callbacks: list = []

    def on_anomaly(self, callback):
        self._alert_callbacks.append(callback)

    def record(self, cost: float) -> dict:
        self._baseline.add(cost)
        self._short.add(cost)

        current_rate = self._short.rate_per_minute
        self._historical_rates.append(current_rate)
        if len(self._historical_rates) > 1000:
            self._historical_rates = self._historical_rates[-500:]

        result = {
            "cost": cost,
            "current_rate_per_min": round(current_rate, 4),
            "anomaly": False,
            "z_score": None,
        }

        if len(self._historical_rates) < self._min_samples:
            return result

        mean = statistics.mean(self._historical_rates)
        try:
            std = statistics.stdev(self._historical_rates)
        except statistics.StatisticsError:
            return result

        if std == 0:
            return result

        z = (current_rate - mean) / std
        result["z_score"] = round(z, 2)
        result["baseline_rate_per_min"] = round(mean, 4)

        if z > self._z_threshold:
            self._anomalies_detected += 1
            result["anomaly"] = True
            result["severity"] = "critical" if z > self._z_threshold * 2 else "warning"
            for cb in self._alert_callbacks:
                cb(result)

        return result


class AnomalyDetectingAgent:
    def __init__(self):
        self.client = AsyncAnthropic()
        self.detector = CostAnomalyDetector(spike_z_threshold=2.5)

        @self.detector.on_anomaly
        def alert(info: dict):
            print(
                f"[ANOMALY] Cost spike! Rate: ${info['current_rate_per_min']:.4f}/min "
                f"(z={info['z_score']:.1f}, severity={info.get('severity')})"
            )

    async def chat(self, message: str, model: str = "claude-haiku-4-5-20251001") -> str:
        response = await self.client.messages.create(
            model=model,
            max_tokens=1024,
            messages=[{"role": "user", "content": message}],
        )
        cost = compute_cost(model, response.usage.input_tokens, response.usage.output_tokens)
        detection = self.detector.record(cost)
        if detection.get("anomaly"):
            print(f"  → Potential runaway agent! Z-score: {detection['z_score']}")
        return response.content[0].text
```

## Solution 6: Daily Cost Report with Projected Month-End Spend

Generate a daily cost report with model breakdown and month-end projection; emit to Slack/email/webhook.

```python
import asyncio
import json
import time
from collections import defaultdict
from dataclasses import dataclass, field
from anthropic import AsyncAnthropic


@dataclass
class DailyUsageRecord:
    date: str  # YYYY-MM-DD
    model_usage: dict[str, dict] = field(default_factory=lambda: defaultdict(
        lambda: {"input_tokens": 0, "output_tokens": 0, "requests": 0, "cost_usd": 0.0}
    ))
    total_cost_usd: float = 0.0
    total_requests: int = 0

    def record(self, model: str, input_tokens: int, output_tokens: int):
        cost = compute_cost(model, input_tokens, output_tokens)
        self.model_usage[model]["input_tokens"] += input_tokens
        self.model_usage[model]["output_tokens"] += output_tokens
        self.model_usage[model]["requests"] += 1
        self.model_usage[model]["cost_usd"] += cost
        self.total_cost_usd += cost
        self.total_requests += 1

    def to_report(self) -> dict:
        return {
            "date": self.date,
            "total_cost_usd": round(self.total_cost_usd, 4),
            "total_requests": self.total_requests,
            "by_model": {
                model: {
                    "requests": data["requests"],
                    "input_tokens": data["input_tokens"],
                    "output_tokens": data["output_tokens"],
                    "cost_usd": round(data["cost_usd"], 4),
                    "pct_of_total": round(
                        data["cost_usd"] / max(self.total_cost_usd, 0.0001) * 100, 1
                    ),
                }
                for model, data in self.model_usage.items()
            },
        }


class DailyCostReporter:
    def __init__(self, monthly_budget_usd: float = 500.0):
        self.monthly_budget = monthly_budget_usd
        self._daily_records: dict[str, DailyUsageRecord] = {}
        self._webhook_url: str | None = None

    def set_webhook(self, url: str):
        self._webhook_url = url

    def _today(self) -> str:
        return time.strftime("%Y-%m-%d")

    def _current_day_of_month(self) -> int:
        return int(time.strftime("%d"))

    def _days_in_month(self) -> int:
        import calendar
        t = time.localtime()
        return calendar.monthrange(t.tm_year, t.tm_mon)[1]

    def record(self, model: str, input_tokens: int, output_tokens: int):
        today = self._today()
        if today not in self._daily_records:
            self._daily_records[today] = DailyUsageRecord(date=today)
        self._daily_records[today].record(model, input_tokens, output_tokens)

    def month_to_date_spend(self) -> float:
        prefix = time.strftime("%Y-%m")
        return sum(
            rec.total_cost_usd
            for date, rec in self._daily_records.items()
            if date.startswith(prefix)
        )

    def projected_month_end(self) -> float:
        """Linear projection based on spend so far this month."""
        day = self._current_day_of_month()
        days = self._days_in_month()
        mtd = self.month_to_date_spend()
        if day == 0:
            return 0.0
        return mtd * days / day

    def daily_report(self, date: str | None = None) -> dict:
        target = date or self._today()
        record = self._daily_records.get(target)
        if not record:
            return {"date": target, "total_cost_usd": 0.0, "total_requests": 0}

        mtd = self.month_to_date_spend()
        projection = self.projected_month_end()
        return {
            **record.to_report(),
            "month_to_date_usd": round(mtd, 2),
            "projected_month_end_usd": round(projection, 2),
            "monthly_budget_usd": self.monthly_budget,
            "projected_vs_budget_pct": round(projection / max(self.monthly_budget, 0.01) * 100, 1),
            "alert": projection > self.monthly_budget,
        }

    async def emit_report(self, report: dict):
        """Emit to webhook (Slack, Teams, PagerDuty, etc.)."""
        if not self._webhook_url:
            print(json.dumps(report, indent=2))
            return
        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                await session.post(
                    self._webhook_url,
                    json={"text": f"Daily Cost Report:\n```{json.dumps(report, indent=2)}```"},
                )
        except Exception as e:
            print(f"[REPORT] Webhook delivery failed: {e}")
            print(json.dumps(report, indent=2))


class ReportingAgent:
    def __init__(self, monthly_budget: float = 50.0):
        self.client = AsyncAnthropic()
        self.reporter = DailyCostReporter(monthly_budget)

    async def chat(self, message: str, model: str = "claude-haiku-4-5-20251001") -> str:
        response = await self.client.messages.create(
            model=model,
            max_tokens=1024,
            messages=[{"role": "user", "content": message}],
        )
        self.reporter.record(model, response.usage.input_tokens, response.usage.output_tokens)
        return response.content[0].text

    async def emit_daily_report(self):
        report = self.reporter.daily_report()
        await self.reporter.emit_report(report)
        return report


async def demo_reporting():
    agent = ReportingAgent(monthly_budget=10.0)
    for msg in ["What is Python?", "Explain asyncio.", "What is a semaphore?"]:
        await agent.chat(msg)
    report = await agent.emit_daily_report()
    if report.get("alert"):
        print(f"[BUDGET ALERT] Projected ${report['projected_month_end_usd']:.2f} > budget ${report['monthly_budget_usd']}")
```

## Comparison Table

| Solution | Detection Type | Granularity | Forecast | Hard Stop | Best For |
|---|---|---|---|---|---|
| Rolling Window Tracker | Threshold crossing | Per window (1m/1h/24h) | No | No | Real-time alerting at configurable thresholds |
| Linear Forecaster | Time-to-exhaustion | Rate-based projection | Yes | No | Early warning before budget runs out |
| Hard Budget Enforcer | Hard limit with circuit breaker | Per request/session/day | No | Yes | Strict spend caps in production |
| Per-Actor Budgets | Per-user/team limit | User + team + global | No | Yes | Multi-tenant SaaS with usage quotas |
| Anomaly Detector | Statistical spike (Z-score) | Per-minute rate | No | No (alert only) | Detecting runaway loops or attacks |
| Daily Reporter + Projection | Month-end projection | Daily with model breakdown | Yes (linear) | No | Finance/ops reporting, monthly budget review |

**Recommended**: Implement **Rolling Window Tracker** (Solution 1) first for real-time threshold alerts, **Hard Budget Enforcer** (Solution 3) for automatic shutdown protection, and **Linear Forecaster** (Solution 2) for proactive early warnings. Combine with **Daily Reporter** (Solution 6) for finance-grade reporting. Add **Per-Actor Budgets** (Solution 4) as soon as you have multiple users or teams.
