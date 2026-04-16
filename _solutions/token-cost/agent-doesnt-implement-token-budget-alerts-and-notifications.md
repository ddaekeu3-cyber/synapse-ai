---
layout: solution
title: "Agent Doesn't Implement Token Budget Alerts and Notifications"
category: token-cost
description: "Agents that consume tokens without budget alerts let costs accumulate silently until the billing spike hits. Token budget alerts with thresholds, webhook notifications, and per-user tracking prevent surprise invoices."
tags: [token-cost, budget, alerts, notifications, webhook, sqlite, monitoring]
---

# Agent Doesn't Implement Token Budget Alerts and Notifications

## Problem

Without token budget alerts, there's no signal when a single agent run consumes 10× the expected tokens — until the end-of-month invoice arrives. Long conversations, runaway retry loops, and misconfigured system prompts can all trigger token spikes that go completely unnoticed.

Token budget alerts with configurable thresholds, per-session tracking, and outbound notifications catch these spikes in real time.

---

## Option 1: Simple Per-Session Budget with Console Alert

```python
import anthropic
from dataclasses import dataclass

@dataclass
class SessionBudget:
    session_id: str
    max_input_tokens: int
    max_output_tokens: int
    warn_at_pct: float = 0.80

    input_tokens_used: int = 0
    output_tokens_used: int = 0

    def record(self, input_tokens: int, output_tokens: int):
        self.input_tokens_used += input_tokens
        self.output_tokens_used += output_tokens
        self._check()

    def _check(self):
        in_pct = self.input_tokens_used / self.max_input_tokens
        out_pct = self.output_tokens_used / self.max_output_tokens

        if self.input_tokens_used > self.max_input_tokens:
            print(f"[BUDGET EXCEEDED] session={self.session_id} input_tokens={self.input_tokens_used}/{self.max_input_tokens}")
        elif in_pct >= self.warn_at_pct:
            print(f"[BUDGET WARNING] session={self.session_id} input={in_pct:.0%} used")

        if self.output_tokens_used > self.max_output_tokens:
            print(f"[BUDGET EXCEEDED] session={self.session_id} output_tokens={self.output_tokens_used}/{self.max_output_tokens}")
        elif out_pct >= self.warn_at_pct:
            print(f"[BUDGET WARNING] session={self.session_id} output={out_pct:.0%} used")

    def is_over_budget(self) -> bool:
        return (
            self.input_tokens_used > self.max_input_tokens
            or self.output_tokens_used > self.max_output_tokens
        )

    def summary(self) -> str:
        return (
            f"session={self.session_id} "
            f"input={self.input_tokens_used}/{self.max_input_tokens} "
            f"output={self.output_tokens_used}/{self.max_output_tokens}"
        )


def run_budgeted_session(prompts: list[str], session_id: str = "sess-001"):
    budget = SessionBudget(
        session_id=session_id,
        max_input_tokens=2000,
        max_output_tokens=500,
        warn_at_pct=0.70,
    )
    client = anthropic.Anthropic()
    messages = []

    for prompt in prompts:
        if budget.is_over_budget():
            print(f"[BLOCKED] Session over budget — rejecting: {prompt[:40]}")
            break

        messages.append({"role": "user", "content": prompt})
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=128,
            messages=messages,
        )
        text = response.content[0].text
        messages.append({"role": "assistant", "content": text})
        budget.record(response.usage.input_tokens, response.usage.output_tokens)
        print(f"[Turn] {prompt[:35]} → {text[:50]}")

    print(f"\nBudget summary: {budget.summary()}")


if __name__ == "__main__":
    run_budgeted_session([
        "What is Python?",
        "Explain async/await in detail.",
        "What is machine learning? Give examples.",
        "Describe REST APIs thoroughly.",
        "How does HTTPS work? Be comprehensive.",
    ])
# Expected Token Savings: Prevents budget overruns by blocking requests after threshold exceeded
# Environment: pip install anthropic
```

---

## Option 2: Multi-Tier Alert Thresholds with Cost Estimation

```python
import anthropic
import json
from dataclasses import dataclass, field
from enum import Enum

MODEL_PRICING = {
    "claude-haiku-4-5-20251001": {"input": 0.80,  "output": 4.00},
    "claude-sonnet-4-6":         {"input": 3.00,  "output": 15.00},
    "claude-opus-4-6":           {"input": 15.00, "output": 75.00},
}

class AlertLevel(Enum):
    OK = "ok"
    INFO = "info"          # 50% consumed
    WARNING = "warning"    # 75% consumed
    CRITICAL = "critical"  # 90% consumed
    EXCEEDED = "exceeded"  # >100%


@dataclass
class CostBudget:
    session_id: str
    model: str
    max_cost_usd: float

    input_tokens: int = 0
    output_tokens: int = 0
    alerts_fired: list[str] = field(default_factory=list)

    _last_level: AlertLevel = AlertLevel.OK

    THRESHOLDS = [
        (1.00, AlertLevel.EXCEEDED),
        (0.90, AlertLevel.CRITICAL),
        (0.75, AlertLevel.WARNING),
        (0.50, AlertLevel.INFO),
    ]

    def cost_usd(self) -> float:
        pricing = MODEL_PRICING.get(self.model, {"input": 3.00, "output": 15.00})
        return (
            (self.input_tokens / 1_000_000) * pricing["input"]
            + (self.output_tokens / 1_000_000) * pricing["output"]
        )

    def pct_used(self) -> float:
        return self.cost_usd() / max(self.max_cost_usd, 0.000001)

    def record(self, input_tokens: int, output_tokens: int):
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self._fire_alerts()

    def _fire_alerts(self):
        pct = self.pct_used()
        cost = self.cost_usd()

        for threshold, level in self.THRESHOLDS:
            if pct >= threshold and self._last_level != level:
                self._last_level = level
                msg = (
                    f"[{level.value.upper()}] session={self.session_id} "
                    f"cost=${cost:.6f}/{self.max_cost_usd:.4f} ({pct:.0%}) "
                    f"tokens={self.input_tokens}in/{self.output_tokens}out"
                )
                self.alerts_fired.append(msg)
                self._notify(level, msg)
                break

    def _notify(self, level: AlertLevel, msg: str):
        icons = {
            AlertLevel.INFO: "ℹ️",
            AlertLevel.WARNING: "⚠️",
            AlertLevel.CRITICAL: "🔴",
            AlertLevel.EXCEEDED: "🚨",
        }
        print(f"{icons.get(level, '')} {msg}")

    def is_exceeded(self) -> bool:
        return self.pct_used() >= 1.0

    def report(self) -> dict:
        return {
            "session_id": self.session_id,
            "model": self.model,
            "cost_usd": round(self.cost_usd(), 6),
            "budget_usd": self.max_cost_usd,
            "pct_used": round(self.pct_used() * 100, 1),
            "alerts_fired": len(self.alerts_fired),
        }


def run_tiered_budget_session(model: str = "claude-haiku-4-5-20251001"):
    budget = CostBudget(
        session_id="demo-001",
        model=model,
        max_cost_usd=0.0005,  # Very tight for demo
    )
    client = anthropic.Anthropic()

    prompts = [
        "What is Python?",
        "Explain machine learning.",
        "Describe neural networks in detail.",
        "How does transformer architecture work?",
        "What are the key advances in LLMs?",
    ]

    for prompt in prompts:
        if budget.is_exceeded():
            print(f"[BLOCKED] Budget exceeded — stopping at prompt: {prompt[:40]}")
            break
        r = client.messages.create(
            model=model,
            max_tokens=96,
            messages=[{"role": "user", "content": prompt}],
        )
        budget.record(r.usage.input_tokens, r.usage.output_tokens)
        print(f"  {prompt[:40]} → {r.content[0].text[:40]}")

    print(f"\nFinal report: {json.dumps(budget.report(), indent=2)}")


if __name__ == "__main__":
    run_tiered_budget_session()
# Expected Token Savings: Hard stop at 100% budget prevents runaway token consumption
# Environment: pip install anthropic; json is stdlib
```

---

## Option 3: SQLite Budget Tracker with Per-User Quotas

```python
import sqlite3
import json
import anthropic
from datetime import datetime, date

MODEL_PRICING = {
    "claude-haiku-4-5-20251001": {"input": 0.80, "output": 4.00},
    "claude-sonnet-4-6":         {"input": 3.00, "output": 15.00},
    "claude-opus-4-6":           {"input": 15.00, "output": 75.00},
}

class UserTokenQuota:
    """Per-user daily token quota with SQLite persistence."""

    def __init__(self, db_path: str = ":memory:"):
        self.conn = sqlite3.connect(db_path)
        self._init_schema()

    def _init_schema(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS user_quotas (
                user_id TEXT PRIMARY KEY,
                daily_token_limit INTEGER DEFAULT 50000,
                daily_cost_limit_usd REAL DEFAULT 0.10
            );
            CREATE TABLE IF NOT EXISTS usage_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT,
                model TEXT,
                input_tokens INTEGER,
                output_tokens INTEGER,
                cost_usd REAL,
                used_date TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS alert_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT,
                alert_type TEXT,
                message TEXT,
                fired_at TEXT DEFAULT (datetime('now'))
            );
        """)
        self.conn.commit()

    def set_quota(self, user_id: str, daily_tokens: int = 50000, daily_cost_usd: float = 0.10):
        self.conn.execute(
            "INSERT OR REPLACE INTO user_quotas (user_id, daily_token_limit, daily_cost_limit_usd) VALUES (?,?,?)",
            (user_id, daily_tokens, daily_cost_usd),
        )
        self.conn.commit()

    def _cost(self, model: str, input_t: int, output_t: int) -> float:
        p = MODEL_PRICING.get(model, {"input": 3.00, "output": 15.00})
        return (input_t / 1_000_000) * p["input"] + (output_t / 1_000_000) * p["output"]

    def record_usage(self, user_id: str, model: str, input_tokens: int, output_tokens: int) -> dict:
        today = date.today().isoformat()
        cost = self._cost(model, input_tokens, output_tokens)

        self.conn.execute(
            "INSERT INTO usage_log (user_id, model, input_tokens, output_tokens, cost_usd, used_date) VALUES (?,?,?,?,?,?)",
            (user_id, model, input_tokens, output_tokens, round(cost, 8), today),
        )
        self.conn.commit()
        return self._check_quota(user_id, today)

    def _check_quota(self, user_id: str, today: str) -> dict:
        quota = self.conn.execute(
            "SELECT daily_token_limit, daily_cost_limit_usd FROM user_quotas WHERE user_id=?",
            (user_id,),
        ).fetchone()
        if not quota:
            return {"status": "no_quota_set"}

        token_limit, cost_limit = quota

        totals = self.conn.execute(
            "SELECT SUM(input_tokens+output_tokens), SUM(cost_usd) FROM usage_log WHERE user_id=? AND used_date=?",
            (user_id, today),
        ).fetchone()
        total_tokens = totals[0] or 0
        total_cost = totals[1] or 0.0

        token_pct = total_tokens / token_limit
        cost_pct = total_cost / cost_limit

        alerts = []
        for metric, pct, used, limit, unit in [
            ("tokens", token_pct, total_tokens, token_limit, "tok"),
            ("cost",   cost_pct,  total_cost,  cost_limit,  "USD"),
        ]:
            if pct >= 1.0:
                level = "EXCEEDED"
            elif pct >= 0.90:
                level = "CRITICAL"
            elif pct >= 0.75:
                level = "WARNING"
            else:
                continue

            msg = f"user={user_id} {metric}={used:.4g}/{limit:.4g}{unit} ({pct:.0%})"
            self.conn.execute(
                "INSERT INTO alert_log (user_id, alert_type, message) VALUES (?,?,?)",
                (user_id, f"{metric}_{level.lower()}", msg),
            )
            alerts.append({"level": level, "metric": metric, "message": msg})

        self.conn.commit()

        for alert in alerts:
            icons = {"EXCEEDED": "🚨", "CRITICAL": "🔴", "WARNING": "⚠️"}
            print(f"{icons.get(alert['level'], '')} [{alert['level']}] {alert['message']}")

        return {
            "user_id": user_id,
            "date": today,
            "total_tokens": total_tokens,
            "total_cost_usd": round(total_cost, 6),
            "token_pct": round(token_pct * 100, 1),
            "cost_pct": round(cost_pct * 100, 1),
            "alerts": alerts,
            "blocked": token_pct >= 1.0 or cost_pct >= 1.0,
        }

    def daily_report(self, today: str | None = None) -> list[dict]:
        today = today or date.today().isoformat()
        rows = self.conn.execute(
            """SELECT user_id, SUM(input_tokens+output_tokens), SUM(cost_usd), COUNT(*)
               FROM usage_log WHERE used_date=? GROUP BY user_id""",
            (today,),
        ).fetchall()
        return [{"user": r[0], "tokens": r[1], "cost_usd": round(r[2], 6), "calls": r[3]} for r in rows]


def run_multi_user_quota_demo():
    quota = UserTokenQuota()
    client = anthropic.Anthropic()

    # Setup quotas
    quota.set_quota("user-alice", daily_tokens=500, daily_cost_usd=0.001)
    quota.set_quota("user-bob",   daily_tokens=5000, daily_cost_usd=0.01)

    users_prompts = [
        ("user-alice", "What is Python?"),
        ("user-alice", "Explain machine learning."),
        ("user-bob",   "What is Python?"),
        ("user-alice", "Describe neural networks."),
        ("user-bob",   "Explain REST APIs."),
    ]

    model = "claude-haiku-4-5-20251001"
    for user_id, prompt in users_prompts:
        r = client.messages.create(
            model=model,
            max_tokens=64,
            messages=[{"role": "user", "content": prompt}],
        )
        status = quota.record_usage(user_id, model, r.usage.input_tokens, r.usage.output_tokens)
        blocked = status.get("blocked", False)
        print(f"[{user_id}] {prompt[:35]} → {r.content[0].text[:40]}{' [BLOCKED NEXT]' if blocked else ''}")

    print(f"\nDaily Report: {json.dumps(quota.daily_report(), indent=2)}")


if __name__ == "__main__":
    run_multi_user_quota_demo()
# Expected Token Savings: Per-user quotas prevent one user from exhausting shared token budget
# Environment: pip install anthropic; sqlite3, json, datetime are stdlib
```

---

## Option 4: Webhook Alert Dispatcher

```python
import asyncio
import json
import sqlite3
import anthropic
from dataclasses import dataclass
from datetime import datetime

@dataclass
class AlertPayload:
    session_id: str
    level: str           # "warning", "critical", "exceeded"
    metric: str          # "tokens", "cost"
    value: float
    limit: float
    pct: float
    model: str
    timestamp: str

    def to_dict(self) -> dict:
        return {
            "alert_type": "token_budget",
            "session_id": self.session_id,
            "level": self.level,
            "metric": self.metric,
            "value": self.value,
            "limit": self.limit,
            "percent_used": round(self.pct * 100, 1),
            "model": self.model,
            "timestamp": self.timestamp,
        }


class AsyncBudgetAlerter:
    """
    Async token budget tracker that dispatches webhook alerts
    without blocking the main agent event loop.
    """

    MODEL_PRICING = {
        "claude-haiku-4-5-20251001": {"input": 0.80, "output": 4.00},
        "claude-sonnet-4-6":         {"input": 3.00, "output": 15.00},
    }

    def __init__(
        self,
        session_id: str,
        max_tokens: int = 10000,
        max_cost_usd: float = 0.05,
        webhook_url: str | None = None,
        db_path: str = ":memory:",
    ):
        self.session_id = session_id
        self.max_tokens = max_tokens
        self.max_cost_usd = max_cost_usd
        self.webhook_url = webhook_url
        self._tokens_used = 0
        self._cost_usd = 0.0
        self._alerts_sent = set()
        self._queue: asyncio.Queue[AlertPayload] = asyncio.Queue()
        self.conn = sqlite3.connect(db_path)
        self._init_db()

    def _init_db(self):
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS alert_dispatch_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT,
                level TEXT,
                metric TEXT,
                value REAL,
                limit_value REAL,
                pct REAL,
                dispatched_at TEXT DEFAULT (datetime('now'))
            )
        """)
        self.conn.commit()

    def _cost(self, model: str, input_t: int, output_t: int) -> float:
        p = self.MODEL_PRICING.get(model, {"input": 3.00, "output": 15.00})
        return (input_t / 1_000_000) * p["input"] + (output_t / 1_000_000) * p["output"]

    def record(self, model: str, input_tokens: int, output_tokens: int):
        self._tokens_used += input_tokens + output_tokens
        self._cost_usd += self._cost(model, input_tokens, output_tokens)

        token_pct = self._tokens_used / self.max_tokens
        cost_pct = self._cost_usd / self.max_cost_usd
        now = datetime.utcnow().isoformat()

        for metric, pct, value, limit in [
            ("tokens", token_pct, self._tokens_used, float(self.max_tokens)),
            ("cost", cost_pct, self._cost_usd, self.max_cost_usd),
        ]:
            if pct >= 1.0:
                level = "exceeded"
            elif pct >= 0.90:
                level = "critical"
            elif pct >= 0.75:
                level = "warning"
            else:
                continue

            alert_key = f"{metric}_{level}"
            if alert_key not in self._alerts_sent:
                self._alerts_sent.add(alert_key)
                payload = AlertPayload(
                    session_id=self.session_id,
                    level=level,
                    metric=metric,
                    value=round(value, 6),
                    limit=limit,
                    pct=round(pct, 4),
                    model=model,
                    timestamp=now,
                )
                self._queue.put_nowait(payload)

    async def dispatch_loop(self, stop: asyncio.Event):
        """Background task that drains the alert queue and sends webhooks."""
        while not stop.is_set() or not self._queue.empty():
            try:
                payload = self._queue.get_nowait()
                await self._send_alert(payload)
            except asyncio.QueueEmpty:
                await asyncio.sleep(0.05)

    async def _send_alert(self, payload: AlertPayload):
        icons = {"warning": "⚠️", "critical": "🔴", "exceeded": "🚨"}
        print(f"{icons.get(payload.level, '')} ALERT [{payload.level.upper()}] "
              f"{payload.metric}={payload.value:.4g}/{payload.limit:.4g} ({payload.pct*100:.0f}%)")

        if self.webhook_url:
            # In production: await httpx.AsyncClient().post(self.webhook_url, json=payload.to_dict())
            print(f"   [Webhook] Would POST to {self.webhook_url}: {json.dumps(payload.to_dict())[:80]}")

        self.conn.execute(
            "INSERT INTO alert_dispatch_log (session_id, level, metric, value, limit_value, pct) VALUES (?,?,?,?,?,?)",
            (payload.session_id, payload.level, payload.metric, payload.value, payload.limit, payload.pct),
        )
        self.conn.commit()

    def is_exceeded(self) -> bool:
        return (
            self._tokens_used > self.max_tokens
            or self._cost_usd > self.max_cost_usd
        )


async def run_async_budget_agent():
    alerter = AsyncBudgetAlerter(
        session_id="async-session-001",
        max_tokens=400,
        max_cost_usd=0.0008,
        webhook_url="https://hooks.example.com/token-alerts",
    )
    client = anthropic.AsyncAnthropic()
    stop = asyncio.Event()
    dispatch_task = asyncio.create_task(alerter.dispatch_loop(stop))

    model = "claude-haiku-4-5-20251001"
    prompts = [
        "What is Python?",
        "Explain async/await.",
        "What is a REST API?",
        "How does HTTPS work?",
    ]

    for prompt in prompts:
        if alerter.is_exceeded():
            print(f"[BLOCKED] Budget exceeded — skipping: {prompt[:40]}")
            continue
        r = await client.messages.create(
            model=model,
            max_tokens=96,
            messages=[{"role": "user", "content": prompt}],
        )
        alerter.record(model, r.usage.input_tokens, r.usage.output_tokens)
        print(f"  {prompt[:35]} → {r.content[0].text[:50]}")

    stop.set()
    await dispatch_task
    print(f"\nSession complete. Tokens: {alerter._tokens_used} | Cost: ${alerter._cost_usd:.6f}")


if __name__ == "__main__":
    asyncio.run(run_async_budget_agent())
# Expected Token Savings: Hard stop after budget exceeded prevents further API calls
# Environment: pip install anthropic; asyncio, sqlite3, json are stdlib
```

---

## Option 5: Rolling Window Budget with Rate Alerts

```python
import sqlite3
import json
import time
import anthropic
from datetime import datetime, timedelta
from collections import deque

class RollingWindowBudget:
    """
    Tracks token consumption over a rolling time window (e.g., per hour).
    Fires alerts when the rolling rate exceeds configured thresholds.
    """

    def __init__(
        self,
        window_sec: int = 3600,     # 1 hour rolling window
        max_tokens_per_window: int = 100_000,
        max_cost_per_window_usd: float = 1.00,
        db_path: str = ":memory:",
    ):
        self.window_sec = window_sec
        self.max_tokens = max_tokens_per_window
        self.max_cost = max_cost_per_window_usd
        self._calls: deque[dict] = deque()
        self.conn = sqlite3.connect(db_path)
        self._init_db()

    def _init_db(self):
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS rolling_alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                window_sec INTEGER,
                tokens_in_window INTEGER,
                cost_in_window REAL,
                alert_type TEXT,
                fired_at TEXT DEFAULT (datetime('now'))
            )
        """)
        self.conn.commit()

    def _prune(self):
        cutoff = time.time() - self.window_sec
        while self._calls and self._calls[0]["ts"] < cutoff:
            self._calls.popleft()

    def _window_totals(self) -> tuple[int, float]:
        return (
            sum(c["tokens"] for c in self._calls),
            sum(c["cost"] for c in self._calls),
        )

    def record(self, model: str, input_tokens: int, output_tokens: int) -> dict:
        pricing = {"claude-haiku-4-5-20251001": {"input": 0.80, "output": 4.00}}.get(
            model, {"input": 3.00, "output": 15.00}
        )
        cost = (input_tokens / 1_000_000) * pricing["input"] + (output_tokens / 1_000_000) * pricing["output"]
        self._calls.append({"ts": time.time(), "tokens": input_tokens + output_tokens, "cost": cost})
        self._prune()
        return self._check_and_alert()

    def _check_and_alert(self) -> dict:
        total_tokens, total_cost = self._window_totals()
        token_pct = total_tokens / self.max_tokens
        cost_pct = total_cost / self.max_cost

        alerts = []
        for metric, pct in [("tokens", token_pct), ("cost", cost_pct)]:
            if pct >= 1.0:
                level = "exceeded"
            elif pct >= 0.90:
                level = "critical"
            elif pct >= 0.75:
                level = "warning"
            else:
                continue

            alert_type = f"{metric}_{level}"
            self.conn.execute(
                "INSERT INTO rolling_alerts (window_sec, tokens_in_window, cost_in_window, alert_type) VALUES (?,?,?,?)",
                (self.window_sec, total_tokens, round(total_cost, 8), alert_type),
            )
            self.conn.commit()
            msg = f"[{level.upper()}] {metric}: {total_tokens if metric == 'tokens' else f'${total_cost:.4f}'} / {self.window_sec}s window"
            alerts.append(msg)
            print(f"{'🚨' if level == 'exceeded' else '⚠️'} {msg}")

        return {
            "tokens_in_window": total_tokens,
            "cost_in_window": round(total_cost, 6),
            "token_pct": round(token_pct * 100, 1),
            "cost_pct": round(cost_pct * 100, 1),
            "alerts": alerts,
            "calls_in_window": len(self._calls),
        }

    def status(self) -> dict:
        self._prune()
        tokens, cost = self._window_totals()
        return {
            "window_sec": self.window_sec,
            "calls": len(self._calls),
            "tokens": tokens,
            "cost_usd": round(cost, 6),
            "token_pct": round(tokens / self.max_tokens * 100, 1),
            "cost_pct": round(cost / self.max_cost * 100, 1),
        }


def run_rolling_budget_demo():
    budget = RollingWindowBudget(
        window_sec=60,
        max_tokens_per_window=300,
        max_cost_per_window_usd=0.001,
    )
    client = anthropic.Anthropic()
    model = "claude-haiku-4-5-20251001"

    prompts = [
        "What is Python?",
        "Explain machine learning.",
        "What is a REST API?",
        "How does HTTPS work?",
        "What is Docker?",
    ]

    for prompt in prompts:
        r = client.messages.create(
            model=model,
            max_tokens=64,
            messages=[{"role": "user", "content": prompt}],
        )
        status = budget.record(model, r.usage.input_tokens, r.usage.output_tokens)
        print(f"  {prompt[:35]} → tokens_in_window={status['tokens_in_window']} ({status['token_pct']}%)")

    print(f"\nFinal window status: {json.dumps(budget.status(), indent=2)}")


if __name__ == "__main__":
    run_rolling_budget_demo()
# Expected Token Savings: Rate-based control prevents token spikes within any rolling window
# Environment: pip install anthropic; sqlite3, json, time, collections are stdlib
```

---

## Option 6: Tenant-Aware Budget with Escalation Chain

```python
import sqlite3
import json
import anthropic
from datetime import date, datetime
from dataclasses import dataclass

@dataclass
class EscalationContact:
    name: str
    channel: str   # "email", "slack", "pagerduty"
    address: str
    min_level: str # "warning", "critical", "exceeded"

ESCALATION_CHAIN = [
    EscalationContact("dev-team",  "slack",    "#agent-alerts",    "warning"),
    EscalationContact("on-call",   "pagerduty","oncall@example.com","critical"),
    EscalationContact("cto",       "email",    "cto@example.com",  "exceeded"),
]

class TenantBudgetManager:
    """
    Multi-tenant budget manager with per-tenant limits and escalation chain.
    """

    MODEL_PRICING = {
        "claude-haiku-4-5-20251001": {"input": 0.80, "output": 4.00},
        "claude-sonnet-4-6":         {"input": 3.00, "output": 15.00},
    }

    def __init__(self, db_path: str = ":memory:"):
        self.conn = sqlite3.connect(db_path)
        self._init_schema()

    def _init_schema(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS tenant_limits (
                tenant_id TEXT PRIMARY KEY,
                daily_token_limit INTEGER,
                daily_cost_limit_usd REAL
            );
            CREATE TABLE IF NOT EXISTS tenant_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id TEXT,
                model TEXT,
                input_tokens INTEGER,
                output_tokens INTEGER,
                cost_usd REAL,
                usage_date TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS escalations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id TEXT,
                level TEXT,
                contact_name TEXT,
                channel TEXT,
                address TEXT,
                message TEXT,
                sent_at TEXT DEFAULT (datetime('now'))
            );
        """)
        self.conn.commit()

    def set_tenant(self, tenant_id: str, daily_tokens: int, daily_cost_usd: float):
        self.conn.execute(
            "INSERT OR REPLACE INTO tenant_limits (tenant_id, daily_token_limit, daily_cost_limit_usd) VALUES (?,?,?)",
            (tenant_id, daily_tokens, daily_cost_usd),
        )
        self.conn.commit()

    def _cost(self, model: str, it: int, ot: int) -> float:
        p = self.MODEL_PRICING.get(model, {"input": 3.00, "output": 15.00})
        return (it / 1_000_000) * p["input"] + (ot / 1_000_000) * p["output"]

    def record(self, tenant_id: str, model: str, input_tokens: int, output_tokens: int) -> dict:
        today = date.today().isoformat()
        cost = self._cost(model, input_tokens, output_tokens)

        self.conn.execute(
            "INSERT INTO tenant_usage (tenant_id, model, input_tokens, output_tokens, cost_usd, usage_date) VALUES (?,?,?,?,?,?)",
            (tenant_id, model, input_tokens, output_tokens, round(cost, 8), today),
        )
        self.conn.commit()

        limits = self.conn.execute(
            "SELECT daily_token_limit, daily_cost_limit_usd FROM tenant_limits WHERE tenant_id=?",
            (tenant_id,),
        ).fetchone()
        if not limits:
            return {"status": "no_limits"}

        totals = self.conn.execute(
            "SELECT SUM(input_tokens+output_tokens), SUM(cost_usd) FROM tenant_usage WHERE tenant_id=? AND usage_date=?",
            (tenant_id, today),
        ).fetchone()
        total_tokens = totals[0] or 0
        total_cost = totals[1] or 0.0

        token_pct = total_tokens / limits[0]
        cost_pct = total_cost / limits[1]

        level = None
        if token_pct >= 1.0 or cost_pct >= 1.0:
            level = "exceeded"
        elif token_pct >= 0.90 or cost_pct >= 0.90:
            level = "critical"
        elif token_pct >= 0.75 or cost_pct >= 0.75:
            level = "warning"

        if level:
            self._escalate(tenant_id, level, total_tokens, limits[0], total_cost, limits[1])

        return {
            "tenant_id": tenant_id,
            "tokens": total_tokens,
            "cost_usd": round(total_cost, 6),
            "token_pct": round(token_pct * 100, 1),
            "cost_pct": round(cost_pct * 100, 1),
            "level": level or "ok",
        }

    def _escalate(self, tenant_id: str, level: str, tokens: int, token_limit: int, cost: float, cost_limit: float):
        LEVELS = {"warning": 1, "critical": 2, "exceeded": 3}
        msg = (
            f"Tenant {tenant_id} budget {level}: "
            f"tokens={tokens}/{token_limit} ({tokens/token_limit:.0%}), "
            f"cost=${cost:.4f}/${cost_limit:.4f} ({cost/cost_limit:.0%})"
        )
        for contact in ESCALATION_CHAIN:
            if LEVELS.get(contact.min_level, 99) <= LEVELS.get(level, 0):
                self.conn.execute(
                    "INSERT INTO escalations (tenant_id, level, contact_name, channel, address, message) VALUES (?,?,?,?,?,?)",
                    (tenant_id, level, contact.name, contact.channel, contact.address, msg),
                )
                icons = {"warning": "⚠️", "critical": "🔴", "exceeded": "🚨"}
                print(f"{icons.get(level, '')} [{contact.channel}→{contact.name}] {msg[:80]}")
        self.conn.commit()

    def tenant_report(self) -> list[dict]:
        today = date.today().isoformat()
        rows = self.conn.execute(
            "SELECT tenant_id, SUM(input_tokens+output_tokens), SUM(cost_usd), COUNT(*) FROM tenant_usage WHERE usage_date=? GROUP BY tenant_id",
            (today,),
        ).fetchall()
        return [{"tenant": r[0], "tokens": r[1], "cost": round(r[2], 6), "calls": r[3]} for r in rows]


def run_tenant_budget_demo():
    mgr = TenantBudgetManager()
    mgr.set_tenant("acme-corp",   daily_tokens=300, daily_cost_usd=0.0005)
    mgr.set_tenant("startup-xyz", daily_tokens=1000, daily_cost_usd=0.002)

    client = anthropic.Anthropic()
    model = "claude-haiku-4-5-20251001"

    calls = [
        ("acme-corp",   "What is Python?"),
        ("acme-corp",   "Explain async/await."),
        ("startup-xyz", "What is machine learning?"),
        ("acme-corp",   "Describe REST APIs."),
        ("startup-xyz", "How does HTTPS work?"),
        ("acme-corp",   "What is Docker?"),
    ]

    for tenant_id, prompt in calls:
        r = client.messages.create(
            model=model, max_tokens=64,
            messages=[{"role": "user", "content": prompt}],
        )
        status = mgr.record(tenant_id, model, r.usage.input_tokens, r.usage.output_tokens)
        print(f"  [{tenant_id}] {prompt[:30]} → tokens={status['tokens']} ({status['token_pct']}%) [{status['level']}]")

    print(f"\nTenant Report: {json.dumps(mgr.tenant_report(), indent=2)}")


if __name__ == "__main__":
    run_tenant_budget_demo()
# Expected Token Savings: Multi-tier escalation ensures high-consumption tenants are caught before runaway
# Environment: pip install anthropic; sqlite3, json, datetime are stdlib
```

---

## Comparison

| Option | Scope | Alert Channels | SQLite | Async | Webhook | Best For |
|--------|-------|---------------|--------|-------|---------|----------|
| 1 | Per-session | Console | No | No | No | Single-session budget enforcement |
| 2 | Per-session + cost | Console (tiered) | No | No | No | Cost-aware multi-tier warnings |
| 3 | Per-user daily | Console + SQLite | Yes | No | No | Multi-user quota management |
| 4 | Per-session | Console + webhook | Yes | Yes | Yes | Async agents with outbound alerts |
| 5 | Rolling window | Console + SQLite | Yes | No | No | Rate-based burst detection |
| 6 | Per-tenant daily | Console + escalation | Yes | No | No | Multi-tenant SaaS deployments |
