---
layout: solution
title: "Agent Doesn't Track Token Cost Per User and Tenant"
category: token-cost
description: "Agents that aggregate all API costs into a single account total can't identify which users or tenants are driving spend, making cost allocation, billing, and abuse detection impossible."
tags: [token-cost, billing, multi-tenant, tracking, sqlite, budget]
---

# Agent Doesn't Track Token Cost Per User and Tenant

When all API calls share one account, you know the total spend but not who spent it. You can't bill tenants accurately, can't alert on users exceeding their quota, and can't identify which feature or customer is responsible for a spike. Cost attribution requires tracking input tokens, output tokens, and model tier per request, per user.

## Why This Happens

Token tracking feels like a billing concern, not an engineering concern. Developers defer it until the bill arrives and someone asks "who's spending all this?"

---

## Option 1: SQLite Per-Request Cost Log

Record every API call's token usage to SQLite with user ID, tenant ID, model, and computed cost.

```python
import sqlite3
import time
import anthropic
from contextlib import contextmanager

client = anthropic.Anthropic()

DB_PATH = "token_usage.db"

# Anthropic pricing (USD per million tokens, as of 2025)
MODEL_PRICING = {
    "claude-haiku-4-5-20251001": {"input": 0.80, "output": 4.00},
    "claude-sonnet-4-6":         {"input": 3.00, "output": 15.00},
    "claude-opus-4-6":           {"input": 15.00, "output": 75.00},
}


def init_db(db_path: str = DB_PATH):
    with sqlite3.connect(db_path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS token_usage (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts          REAL NOT NULL,
                user_id     TEXT NOT NULL,
                tenant_id   TEXT NOT NULL,
                model       TEXT NOT NULL,
                input_tok   INTEGER NOT NULL,
                output_tok  INTEGER NOT NULL,
                cost_usd    REAL NOT NULL,
                request_id  TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_user ON token_usage(user_id, ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tenant ON token_usage(tenant_id, ts)")
        conn.commit()


def log_usage(
    user_id: str,
    tenant_id: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    request_id: str | None = None,
    db_path: str = DB_PATH,
):
    pricing = MODEL_PRICING.get(model, {"input": 3.00, "output": 15.00})
    cost = (input_tokens * pricing["input"] + output_tokens * pricing["output"]) / 1_000_000

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO token_usage (ts, user_id, tenant_id, model, input_tok, output_tok, cost_usd, request_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (time.time(), user_id, tenant_id, model, input_tokens, output_tokens, cost, request_id),
        )
        conn.commit()


def run_tracked(
    prompt: str,
    user_id: str,
    tenant_id: str,
    model: str = "claude-haiku-4-5-20251001",
) -> str:
    response = client.messages.create(
        model=model,
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    log_usage(
        user_id=user_id,
        tenant_id=tenant_id,
        model=model,
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
    )
    return response.content[0].text


def get_tenant_summary(tenant_id: str, db_path: str = DB_PATH) -> dict:
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT SUM(input_tok), SUM(output_tok), SUM(cost_usd), COUNT(*)
            FROM token_usage WHERE tenant_id = ?
            """,
            (tenant_id,),
        ).fetchone()
    return {
        "tenant_id": tenant_id,
        "total_input_tokens": row[0] or 0,
        "total_output_tokens": row[1] or 0,
        "total_cost_usd": round(row[2] or 0, 6),
        "total_requests": row[3] or 0,
    }


def get_top_users(tenant_id: str, limit: int = 10, db_path: str = DB_PATH) -> list[dict]:
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT user_id, SUM(cost_usd) as total_cost, SUM(input_tok+output_tok) as total_tokens
            FROM token_usage WHERE tenant_id = ?
            GROUP BY user_id ORDER BY total_cost DESC LIMIT ?
            """,
            (tenant_id, limit),
        ).fetchall()
    return [
        {"user_id": r[0], "cost_usd": round(r[1], 6), "total_tokens": r[2]}
        for r in rows
    ]


if __name__ == "__main__":
    init_db()
    run_tracked("Hello!", user_id="alice", tenant_id="acme")
    run_tracked("Summarize this.", user_id="bob", tenant_id="acme")
    print(get_tenant_summary("acme"))
    print(get_top_users("acme"))
```

**Expected Token Savings:** Visibility into spend by user/tenant; enables quota enforcement before the month-end bill.

**Environment:** Any multi-tenant agent; SQLite for small deployments, swap for PostgreSQL at scale.

---

## Option 2: In-Memory Cost Accumulator with Budget Alerts

Track costs in memory per tenant with configurable budget thresholds and alert callbacks.

```python
import asyncio
import time
from dataclasses import dataclass, field
from collections import defaultdict
from typing import Callable, Awaitable
import anthropic

client = anthropic.AsyncAnthropic()

MODEL_PRICING = {
    "claude-haiku-4-5-20251001": {"input": 0.80, "output": 4.00},
    "claude-sonnet-4-6":         {"input": 3.00, "output": 15.00},
    "claude-opus-4-6":           {"input": 15.00, "output": 75.00},
}


@dataclass
class TenantBudget:
    tenant_id: str
    monthly_limit_usd: float
    alert_threshold: float = 0.8  # alert at 80% of limit
    spent_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    requests: int = 0
    alerted: bool = False

    def add_cost(self, cost: float, input_tok: int, output_tok: int):
        self.spent_usd += cost
        self.input_tokens += input_tok
        self.output_tokens += output_tok
        self.requests += 1

    @property
    def budget_used_fraction(self) -> float:
        return self.spent_usd / self.monthly_limit_usd if self.monthly_limit_usd > 0 else 0.0

    @property
    def over_budget(self) -> bool:
        return self.spent_usd >= self.monthly_limit_usd


AlertCallback = Callable[[str, float, float], Awaitable[None]]


class CostTracker:
    def __init__(self, alert_callback: AlertCallback | None = None):
        self._budgets: dict[str, TenantBudget] = {}
        self._alert_cb = alert_callback

    def register_tenant(self, tenant_id: str, monthly_limit_usd: float, alert_threshold: float = 0.8):
        self._budgets[tenant_id] = TenantBudget(
            tenant_id=tenant_id,
            monthly_limit_usd=monthly_limit_usd,
            alert_threshold=alert_threshold,
        )

    def compute_cost(self, model: str, input_tokens: int, output_tokens: int) -> float:
        pricing = MODEL_PRICING.get(model, {"input": 3.00, "output": 15.00})
        return (input_tokens * pricing["input"] + output_tokens * pricing["output"]) / 1_000_000

    async def record(self, tenant_id: str, model: str, input_tokens: int, output_tokens: int):
        cost = self.compute_cost(model, input_tokens, output_tokens)
        budget = self._budgets.get(tenant_id)

        if budget:
            budget.add_cost(cost, input_tokens, output_tokens)

            if (
                not budget.alerted
                and budget.budget_used_fraction >= budget.alert_threshold
                and self._alert_cb
            ):
                budget.alerted = True
                await self._alert_cb(
                    tenant_id,
                    budget.spent_usd,
                    budget.monthly_limit_usd,
                )

    def is_over_budget(self, tenant_id: str) -> bool:
        budget = self._budgets.get(tenant_id)
        return budget.over_budget if budget else False

    def summary(self, tenant_id: str) -> dict:
        budget = self._budgets.get(tenant_id)
        if not budget:
            return {"error": "tenant not found"}
        return {
            "tenant_id": tenant_id,
            "spent_usd": round(budget.spent_usd, 6),
            "limit_usd": budget.monthly_limit_usd,
            "used_pct": f"{budget.budget_used_fraction:.1%}",
            "requests": budget.requests,
            "input_tokens": budget.input_tokens,
            "output_tokens": budget.output_tokens,
        }


async def alert(tenant_id: str, spent: float, limit: float):
    print(f"[ALERT] Tenant {tenant_id} at {spent/limit:.0%} of budget (${spent:.4f}/${limit:.2f})")


tracker = CostTracker(alert_callback=alert)
tracker.register_tenant("acme", monthly_limit_usd=50.0, alert_threshold=0.8)
tracker.register_tenant("startup", monthly_limit_usd=10.0, alert_threshold=0.9)


async def run_tracked(prompt: str, tenant_id: str, user_id: str) -> str:
    if tracker.is_over_budget(tenant_id):
        raise RuntimeError(f"Tenant '{tenant_id}' has exceeded monthly budget")

    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )
    await tracker.record(
        tenant_id=tenant_id,
        model="claude-haiku-4-5-20251001",
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
    )
    return response.content[0].text


async def main():
    for i in range(5):
        await run_tracked(f"Query {i}", tenant_id="acme", user_id=f"user-{i}")
    print(tracker.summary("acme"))


if __name__ == "__main__":
    asyncio.run(main())
```

**Expected Token Savings:** Hard budget enforcement prevents runaway costs; alerts at 80% give time to act before hitting the ceiling.

**Environment:** SaaS platforms; multi-tenant agents with per-customer billing.

---

## Option 3: FastAPI Middleware for Automatic Cost Attribution

Intercept every agent response at the middleware level and log cost without touching handler code.

```python
import time
import sqlite3
import json
from fastapi import FastAPI, Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp
import anthropic

client = anthropic.Anthropic()
app = FastAPI()

MODEL_PRICING = {
    "claude-haiku-4-5-20251001": {"input": 0.80, "output": 4.00},
    "claude-sonnet-4-6":         {"input": 3.00, "output": 15.00},
}

DB = "usage.db"
with sqlite3.connect(DB) as c:
    c.execute("""
        CREATE TABLE IF NOT EXISTS usage (
            ts REAL, user_id TEXT, tenant_id TEXT,
            model TEXT, input_tok INTEGER, output_tok INTEGER, cost_usd REAL,
            endpoint TEXT
        )
    """)
    c.commit()


class CostAttributionMiddleware(BaseHTTPMiddleware):
    """Log token cost from X-Usage response header set by route handlers."""

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)

        usage_header = response.headers.get("X-Token-Usage")
        if usage_header:
            try:
                usage = json.loads(usage_header)
                pricing = MODEL_PRICING.get(usage["model"], {"input": 3.0, "output": 15.0})
                cost = (
                    usage["input_tokens"] * pricing["input"]
                    + usage["output_tokens"] * pricing["output"]
                ) / 1_000_000

                with sqlite3.connect(DB) as conn:
                    conn.execute(
                        "INSERT INTO usage VALUES (?,?,?,?,?,?,?,?)",
                        (
                            time.time(),
                            usage.get("user_id", "anonymous"),
                            usage.get("tenant_id", "default"),
                            usage["model"],
                            usage["input_tokens"],
                            usage["output_tokens"],
                            cost,
                            str(request.url.path),
                        ),
                    )
                    conn.commit()
            except Exception as e:
                print(f"[CostMiddleware] Failed to log: {e}")

        return response


app.add_middleware(CostAttributionMiddleware)


@app.post("/agent/run")
async def run_agent(prompt: str, user_id: str = "anon", tenant_id: str = "default"):
    model = "claude-haiku-4-5-20251001"
    response = client.messages.create(
        model=model,
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )
    result = response.content[0].text

    # Set usage header for middleware to pick up
    usage_data = json.dumps({
        "model": model,
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
        "user_id": user_id,
        "tenant_id": tenant_id,
    })

    return Response(
        content=json.dumps({"result": result}),
        media_type="application/json",
        headers={"X-Token-Usage": usage_data},
    )


@app.get("/usage/{tenant_id}")
def get_tenant_usage(tenant_id: str):
    with sqlite3.connect(DB) as conn:
        row = conn.execute(
            "SELECT SUM(cost_usd), SUM(input_tok), SUM(output_tok), COUNT(*) FROM usage WHERE tenant_id=?",
            (tenant_id,),
        ).fetchone()
    return {
        "tenant_id": tenant_id,
        "cost_usd": round(row[0] or 0, 6),
        "input_tokens": row[1] or 0,
        "output_tokens": row[2] or 0,
        "requests": row[3] or 0,
    }
```

**Expected Token Savings:** Zero-overhead attribution — handlers don't change; middleware captures all costs automatically.

**Environment:** FastAPI; drop-in for existing agents without refactoring every endpoint.

---

## Option 4: Per-User Token Quota Enforcement

Enforce a hard token quota per user per day, blocking requests once the limit is hit.

```python
import time
import asyncio
import sqlite3
import anthropic
from fastapi import FastAPI, HTTPException

client = anthropic.AsyncAnthropic()
app = FastAPI()

DB = "quotas.db"
with sqlite3.connect(DB) as c:
    c.execute("""
        CREATE TABLE IF NOT EXISTS daily_usage (
            user_id TEXT,
            date    TEXT,
            tokens  INTEGER,
            PRIMARY KEY (user_id, date)
        )
    """)
    c.commit()

# Quota: tokens per day per user
DEFAULT_DAILY_TOKEN_QUOTA = 100_000


def today() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


def get_usage_today(user_id: str) -> int:
    with sqlite3.connect(DB) as conn:
        row = conn.execute(
            "SELECT tokens FROM daily_usage WHERE user_id=? AND date=?",
            (user_id, today()),
        ).fetchone()
    return row[0] if row else 0


def add_usage(user_id: str, tokens: int):
    with sqlite3.connect(DB) as conn:
        conn.execute(
            """
            INSERT INTO daily_usage (user_id, date, tokens) VALUES (?, ?, ?)
            ON CONFLICT(user_id, date) DO UPDATE SET tokens = tokens + excluded.tokens
            """,
            (user_id, today(), tokens),
        )
        conn.commit()


def check_quota(user_id: str, estimated_tokens: int, quota: int = DEFAULT_DAILY_TOKEN_QUOTA):
    used = get_usage_today(user_id)
    if used + estimated_tokens > quota:
        raise HTTPException(
            status_code=429,
            detail={
                "error": "daily_token_quota_exceeded",
                "used_today": used,
                "quota": quota,
                "resets": f"{today()}T24:00:00Z",
            },
        )


@app.post("/agent/run")
async def run_agent(prompt: str, user_id: str = "anon"):
    # Rough pre-flight estimate: 1 token ≈ 4 chars
    estimated = len(prompt) // 4 + 512  # prompt tokens + expected output
    check_quota(user_id, estimated)

    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )

    actual_tokens = response.usage.input_tokens + response.usage.output_tokens
    add_usage(user_id, actual_tokens)

    return {
        "result": response.content[0].text,
        "tokens_used": actual_tokens,
        "daily_remaining": DEFAULT_DAILY_TOKEN_QUOTA - get_usage_today(user_id),
    }


@app.get("/usage/{user_id}")
def user_usage(user_id: str):
    used = get_usage_today(user_id)
    return {
        "user_id": user_id,
        "used_today": used,
        "quota": DEFAULT_DAILY_TOKEN_QUOTA,
        "remaining": max(0, DEFAULT_DAILY_TOKEN_QUOTA - used),
    }
```

**Expected Token Savings:** Hard quota blocks over-consumption before it hits the API; eliminates abuse-driven cost spikes.

**Environment:** Consumer-facing agents; free-tier / paid-tier quota enforcement.

---

## Option 5: Cost Dashboard Data Aggregation

Build time-series cost aggregation queries for a dashboard: daily, by model, by tenant.

```python
import sqlite3
import time
from dataclasses import dataclass
from typing import Any
import anthropic

DB = "token_usage.db"
MODEL_PRICING = {
    "claude-haiku-4-5-20251001": {"input": 0.80, "output": 4.00},
    "claude-sonnet-4-6":         {"input": 3.00, "output": 15.00},
    "claude-opus-4-6":           {"input": 15.00, "output": 75.00},
}


class CostDashboard:
    def __init__(self, db_path: str = DB):
        self._db = db_path

    def _q(self, sql: str, params: tuple = ()) -> list[tuple]:
        with sqlite3.connect(self._db) as conn:
            return conn.execute(sql, params).fetchall()

    def daily_cost(self, days: int = 30) -> list[dict]:
        since = time.time() - days * 86400
        rows = self._q(
            """
            SELECT DATE(ts, 'unixepoch') as day, SUM(cost_usd), SUM(input_tok+output_tok), COUNT(*)
            FROM token_usage WHERE ts >= ?
            GROUP BY day ORDER BY day
            """,
            (since,),
        )
        return [{"day": r[0], "cost_usd": round(r[1], 6), "tokens": r[2], "requests": r[3]} for r in rows]

    def by_model(self, tenant_id: str | None = None) -> list[dict]:
        where = "WHERE tenant_id = ?" if tenant_id else ""
        params = (tenant_id,) if tenant_id else ()
        rows = self._q(
            f"""
            SELECT model, SUM(cost_usd), SUM(input_tok), SUM(output_tok), COUNT(*)
            FROM token_usage {where}
            GROUP BY model ORDER BY SUM(cost_usd) DESC
            """,
            params,
        )
        return [
            {
                "model": r[0],
                "cost_usd": round(r[1], 6),
                "input_tokens": r[2],
                "output_tokens": r[3],
                "requests": r[4],
            }
            for r in rows
        ]

    def top_tenants(self, limit: int = 10) -> list[dict]:
        rows = self._q(
            """
            SELECT tenant_id, SUM(cost_usd), SUM(input_tok+output_tok), COUNT(*)
            FROM token_usage
            GROUP BY tenant_id ORDER BY SUM(cost_usd) DESC LIMIT ?
            """,
            (limit,),
        )
        return [
            {"tenant_id": r[0], "cost_usd": round(r[1], 6), "tokens": r[2], "requests": r[3]}
            for r in rows
        ]

    def hourly_spike_detection(self, multiplier: float = 3.0) -> list[dict]:
        """Return hours where cost exceeds multiplier * average hourly cost."""
        rows = self._q(
            """
            WITH hourly AS (
                SELECT strftime('%Y-%m-%d %H:00', ts, 'unixepoch') as hour,
                       SUM(cost_usd) as cost
                FROM token_usage GROUP BY hour
            )
            SELECT hour, cost FROM hourly
            WHERE cost > ? * (SELECT AVG(cost) FROM hourly)
            ORDER BY cost DESC LIMIT 20
            """,
            (multiplier,),
        )
        return [{"hour": r[0], "cost_usd": round(r[1], 6)} for r in rows]


if __name__ == "__main__":
    dash = CostDashboard()
    print("Daily costs (last 30d):", dash.daily_cost(30))
    print("By model:", dash.by_model())
    print("Top tenants:", dash.top_tenants(5))
    print("Spike hours:", dash.hourly_spike_detection(2.0))
```

**Expected Token Savings:** Identifies cost spikes and top spenders; data needed to optimize or throttle before the bill arrives.

**Environment:** Any SQLite-tracked agent; adapt SQL for PostgreSQL/BigQuery at scale.

---

## Option 6: Prometheus Metrics for Token Cost Observability

Expose token usage as Prometheus metrics for Grafana dashboards and alerting.

```python
# pip install prometheus-client
from prometheus_client import Counter, Histogram, Gauge, start_http_server
import anthropic
import time
import asyncio

client = anthropic.AsyncAnthropic()

# Prometheus metrics
token_input_total = Counter(
    "agent_input_tokens_total",
    "Total input tokens consumed",
    ["model", "tenant_id", "user_id"],
)
token_output_total = Counter(
    "agent_output_tokens_total",
    "Total output tokens consumed",
    ["model", "tenant_id", "user_id"],
)
cost_usd_total = Counter(
    "agent_cost_usd_total",
    "Total USD cost of LLM calls",
    ["model", "tenant_id"],
)
request_duration = Histogram(
    "agent_request_duration_seconds",
    "LLM call duration",
    ["model"],
    buckets=[0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0],
)

MODEL_PRICING = {
    "claude-haiku-4-5-20251001": {"input": 0.80, "output": 4.00},
    "claude-sonnet-4-6":         {"input": 3.00, "output": 15.00},
}


async def tracked_call(
    prompt: str,
    user_id: str,
    tenant_id: str,
    model: str = "claude-haiku-4-5-20251001",
) -> str:
    start = time.monotonic()
    try:
        response = await client.messages.create(
            model=model,
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )
    finally:
        request_duration.labels(model=model).observe(time.monotonic() - start)

    pricing = MODEL_PRICING.get(model, {"input": 3.0, "output": 15.0})
    cost = (
        response.usage.input_tokens * pricing["input"]
        + response.usage.output_tokens * pricing["output"]
    ) / 1_000_000

    token_input_total.labels(model=model, tenant_id=tenant_id, user_id=user_id).inc(
        response.usage.input_tokens
    )
    token_output_total.labels(model=model, tenant_id=tenant_id, user_id=user_id).inc(
        response.usage.output_tokens
    )
    cost_usd_total.labels(model=model, tenant_id=tenant_id).inc(cost)

    return response.content[0].text


async def main():
    # Expose metrics on :9090
    start_http_server(9090)
    print("Prometheus metrics on :9090/metrics")

    for i in range(5):
        await tracked_call(f"Hello {i}", user_id="alice", tenant_id="acme")
        await asyncio.sleep(1)


if __name__ == "__main__":
    asyncio.run(main())
```

**Expected Token Savings:** Real-time cost visibility in Grafana; alert rules fire before budget is exceeded.

**Environment:** Production agents with Prometheus + Grafana; pairs with alertmanager for budget alerts.

---

## Comparison

| Option | Storage | Tenant Isolation | Real-Time Alert | Dashboard | Budget Enforcement |
|--------|---------|-----------------|-----------------|-----------|-------------------|
| 1. SQLite log | SQLite | Yes | No | Query-based | No |
| 2. In-memory accumulator | RAM | Yes | Yes (callback) | No | Yes |
| 3. FastAPI middleware | SQLite | Yes | No | Query-based | No |
| 4. Per-user quota | SQLite | Per-user | No | No | Yes (hard block) |
| 5. Aggregation queries | SQLite | Yes | No | Full | No |
| 6. Prometheus metrics | Time-series | Yes | Alertmanager | Grafana | No |
