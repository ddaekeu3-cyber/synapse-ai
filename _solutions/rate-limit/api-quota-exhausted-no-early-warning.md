---
layout: solution
title: "API Monthly Quota Exhausted With No Early Warning — Agent Goes Dark"
category: rate-limit
description: "Agent hits monthly API quota at 3 AM. All calls return 429 or 402 until next month. No alert was sent when approaching 80% usage. Team discovers it when users report the agent is down."
tags: [quota, billing, alert, monitoring, 429, spending-limit, usage]
---

# API Monthly Quota Exhausted With No Early Warning — Agent Goes Dark

## Symptom
- All agent API calls suddenly return 429 or 402 Payment Required
- Error: `Your monthly spending limit has been reached`
- Agent stops working completely — no graceful degradation
- No alert before quota was hit
- Team discovers the outage hours later from user complaints

## Root Cause
API quotas and spending limits reset monthly. Without usage monitoring and early warning alerts, the first signal that quota is exhausted is a production outage. Many teams set spending limits for cost control but don't monitor approach to that limit.

## Fix

### Option 1: Set up usage alerts in Anthropic Console

```
In Anthropic Console (console.anthropic.com):
1. Go to Settings → Billing & Usage
2. Set email alerts at 50%, 80%, and 100% of monthly spending limit
3. Add multiple team emails — don't rely on a single person
4. Review usage dashboard weekly

For programmatic monitoring, check the usage API:
GET https://api.anthropic.com/v1/usage
(Check Anthropic documentation for current usage API availability)
```

### Option 2: Track token usage in your own system

```python
import sqlite3, time
from datetime import datetime, date
from dataclasses import dataclass, field

@dataclass
class UsageTracker:
    db_path: str = "agent_usage.db"
    monthly_budget_usd: float = 100.0  # Your spending limit
    alert_threshold: float = 0.8  # Alert at 80%

    def __post_init__(self):
        self._init_db()

    def _init_db(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS api_calls (
                id INTEGER PRIMARY KEY,
                timestamp TEXT,
                input_tokens INTEGER,
                output_tokens INTEGER,
                cache_read_tokens INTEGER,
                model TEXT,
                cost_usd REAL
            )
        """)
        conn.commit()
        conn.close()

    def _estimate_cost(self, input_tokens: int, output_tokens: int, model: str) -> float:
        """Rough cost estimate — check Anthropic pricing for current rates"""
        rates = {
            "claude-sonnet-4-6": (0.000003, 0.000015),  # per input/output token
            "claude-opus-4-6": (0.000015, 0.000075),
            "claude-haiku-4-5-20251001": (0.00000025, 0.00000125),
        }
        in_rate, out_rate = rates.get(model, (0.000003, 0.000015))
        return (input_tokens * in_rate) + (output_tokens * out_rate)

    def record(self, response, model: str):
        cost = self._estimate_cost(
            response.usage.input_tokens,
            response.usage.output_tokens,
            model
        )
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            INSERT INTO api_calls (timestamp, input_tokens, output_tokens, model, cost_usd)
            VALUES (?, ?, ?, ?, ?)
        """, (datetime.utcnow().isoformat(), response.usage.input_tokens,
              response.usage.output_tokens, model, cost))
        conn.commit()
        conn.close()

        # Check if approaching limit
        monthly_spend = self.get_monthly_spend()
        if monthly_spend > self.monthly_budget_usd * self.alert_threshold:
            self._alert(monthly_spend)

    def get_monthly_spend(self) -> float:
        month_start = date.today().replace(day=1).isoformat()
        conn = sqlite3.connect(self.db_path)
        result = conn.execute(
            "SELECT SUM(cost_usd) FROM api_calls WHERE timestamp >= ?",
            (month_start,)
        ).fetchone()[0]
        conn.close()
        return result or 0.0

    def _alert(self, current_spend: float):
        pct = (current_spend / self.monthly_budget_usd) * 100
        print(f"ALERT: Monthly API spend at ${current_spend:.2f} ({pct:.0f}% of ${self.monthly_budget_usd} budget)")
        # Send to Slack, email, PagerDuty, etc.

tracker = UsageTracker(monthly_budget_usd=100.0)
```

### Option 3: Graceful degradation when quota is exhausted

```python
import anthropic

class QuotaAwareAgent:
    def __init__(self, client: anthropic.Anthropic):
        self.client = client
        self.quota_exhausted = False
        self.fallback_responses = {
            "unavailable": "The AI assistant is temporarily unavailable due to high usage. Please try again later or contact support.",
        }

    async def complete(self, messages: list, **kwargs) -> str:
        if self.quota_exhausted:
            return self.fallback_responses["unavailable"]

        try:
            response = self.client.messages.create(messages=messages, **kwargs)
            return response.content[0].text

        except anthropic.RateLimitError as e:
            error_str = str(e)
            if "spending limit" in error_str or "quota" in error_str.lower():
                self.quota_exhausted = True
                self._send_alert(f"API quota exhausted: {e}")
                return self.fallback_responses["unavailable"]
            raise  # Re-raise if it's a different rate limit issue

    def _send_alert(self, message: str):
        # Alert team immediately
        print(f"CRITICAL: {message}")
        # send_slack_message(ALERT_CHANNEL, f":red_circle: {message}")
        # send_email(ONCALL_EMAIL, "API Quota Exhausted", message)
```

### Option 4: Budget reservation system

```python
import asyncio

class BudgetReservation:
    """Reserve budget per task — refuse new tasks when budget nearly exhausted"""

    def __init__(self, monthly_budget_usd: float, reservation_per_task_usd: float = 0.10):
        self.monthly_budget = monthly_budget_usd
        self.reservation_per_task = reservation_per_task_usd
        self.spent = 0.0
        self.reserved = 0.0
        self._lock = asyncio.Lock()

    async def reserve(self) -> bool:
        """Reserve budget for a task. Returns False if not enough budget."""
        async with self._lock:
            available = self.monthly_budget - self.spent - self.reserved
            if available < self.reservation_per_task:
                pct = (self.spent / self.monthly_budget) * 100
                print(f"Budget nearly exhausted ({pct:.0f}% used). Refusing new task.")
                return False
            self.reserved += self.reservation_per_task
            return True

    async def release(self, actual_cost: float):
        """Release reservation and record actual cost"""
        async with self._lock:
            self.reserved -= self.reservation_per_task
            self.spent += actual_cost

budget = BudgetReservation(monthly_budget_usd=100.0)

async def run_task_with_budget_check(task):
    if not await budget.reserve():
        return {"error": "Service temporarily unavailable — monthly budget limit approaching"}

    try:
        result = await call_api(task)
        actual_cost = estimate_cost(result.usage)
        return result
    finally:
        await budget.release(actual_cost)
```

### Option 5: Daily quota distribution

```python
from datetime import date
import calendar

def get_daily_budget(monthly_budget: float) -> float:
    """Distribute monthly budget across days to avoid early exhaustion"""
    today = date.today()
    days_in_month = calendar.monthrange(today.year, today.month)[1]
    days_remaining = days_in_month - today.day + 1
    return monthly_budget / days_in_month  # Even daily distribution

def get_remaining_daily_budget(monthly_budget: float, current_daily_spend: float) -> float:
    daily_budget = get_daily_budget(monthly_budget)
    remaining = daily_budget - current_daily_spend
    if remaining < 0:
        print(f"Daily budget exceeded (${current_daily_spend:.2f} / ${daily_budget:.2f})")
    return max(0, remaining)
```

## Quota Exhaustion Checklist

| Action | When |
|---|---|
| Set 50% usage alert | Setup |
| Set 80% usage alert | Setup |
| Set 95% usage alert | Setup |
| Add graceful degradation | Before launch |
| Test degraded behavior | Before launch |
| Add usage to monitoring dashboard | Before launch |
| Review monthly usage trend | Weekly |
| Adjust budget limits | Monthly |

## Expected Token Savings
Not about token savings — about preventing production outages from quota exhaustion.

## Environment
- Production agents with monthly spending limits; critical for high-traffic agents
- Source: direct experience with production quota exhaustion incidents
