---
layout: solution
title: "Agent Doesn't Implement Token Budget Hard Limits"
category: token-cost
description: "An agent runs batch jobs, multi-turn conversations, and recursive tool calls with no spending ceiling. A single runaway task or a prompt injection that triggers infinite tool calls can exhaust the entire monthly API budget in hours."
tags: [token-cost, budget, hard-limits, asyncio, middleware, cost-control, anthropic]
---

# Agent Doesn't Implement Token Budget Hard Limits

## Problem

A batch processing job calls Claude in a loop. Due to a bug, the loop condition never terminates. By morning, the job has made 50,000 API calls and consumed the entire month's token budget. There's no circuit breaker, no spend ceiling, and no alert — just a maxed-out bill and angry customers. Token budget limits are the financial equivalent of a circuit breaker.

## Solutions

### Option 1: Per-Request Token Counter with Hard Stop

```python
# budget/token_counter.py
"""
Track cumulative token usage across all API calls.
Raise BudgetExceededError when a configurable limit is hit.
Thread-safe for concurrent request environments.
"""
import threading
import time
import os
import anthropic
from dataclasses import dataclass, field


class BudgetExceededError(Exception):
    """Raised when a token budget limit is exceeded."""
    pass


@dataclass
class TokenBudget:
    max_input_tokens: int = int(os.environ.get("MAX_INPUT_TOKENS", "1_000_000"))
    max_output_tokens: int = int(os.environ.get("MAX_OUTPUT_TOKENS", "500_000"))
    max_total_tokens: int = int(os.environ.get("MAX_TOTAL_TOKENS", "2_000_000"))
    window_seconds: float = float(os.environ.get("BUDGET_WINDOW_SECONDS", "3600"))  # 1 hour

    input_used: int = field(default=0)
    output_used: int = field(default=0)
    window_start: float = field(default_factory=time.time)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def _reset_if_window_expired(self):
        now = time.time()
        if now - self.window_start > self.window_seconds:
            self.input_used = 0
            self.output_used = 0
            self.window_start = now

    def check_before_call(self, estimated_input: int = 0):
        """Call before making an API request. Raises if budget is exceeded."""
        with self._lock:
            self._reset_if_window_expired()
            projected_total = self.input_used + self.output_used + estimated_input
            if projected_total > self.max_total_tokens:
                raise BudgetExceededError(
                    f"Token budget exceeded: projected {projected_total:,} > "
                    f"limit {self.max_total_tokens:,} tokens "
                    f"(window: {self.window_seconds/3600:.1f}h)"
                )

    def record_usage(self, input_tokens: int, output_tokens: int):
        """Call after each API response to record actual usage."""
        with self._lock:
            self._reset_if_window_expired()
            self.input_used += input_tokens
            self.output_used += output_tokens
            total = self.input_used + self.output_used
            if total > self.max_total_tokens:
                raise BudgetExceededError(
                    f"Token budget exceeded after call: used {total:,} > "
                    f"limit {self.max_total_tokens:,} tokens"
                )

    @property
    def usage_summary(self) -> dict:
        with self._lock:
            total = self.input_used + self.output_used
            return {
                "input_used": self.input_used,
                "output_used": self.output_used,
                "total_used": total,
                "max_total": self.max_total_tokens,
                "utilization_pct": total / max(self.max_total_tokens, 1) * 100,
            }


# ── Global budget instance ─────────────────────────────────────────────────────
_budget = TokenBudget()


def ask_with_budget(user_message: str, max_tokens: int = 512) -> str:
    """Call Claude with budget enforcement."""
    # Estimate input tokens (rough: 1 token ≈ 4 chars)
    estimated_input = len(user_message) // 4 + 100
    _budget.check_before_call(estimated_input)

    client = anthropic.Anthropic()
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": user_message}],
    )
    _budget.record_usage(response.usage.input_tokens, response.usage.output_tokens)
    return response.content[0].text


def get_budget_status() -> dict:
    return _budget.usage_summary
```

**Expected Token Savings:** Prevents unbounded spending; typical runaway task savings: 90%+
**Environment:** `pip install anthropic`

---

### Option 2: Per-Task Budget with Context Manager

```python
# budget/task_budget.py
"""
Scoped token budget for a single task (e.g., one batch job, one user session).
Raises when the task-level budget is exceeded, independent of global limits.
"""
import asyncio
import time
import anthropic
from contextlib import asynccontextmanager
from dataclasses import dataclass, field


@dataclass
class TaskBudget:
    task_id: str
    max_tokens: int          # Total tokens this task may consume
    tokens_used: int = 0
    calls_made: int = 0
    started_at: float = field(default_factory=time.time)

    def consume(self, input_tokens: int, output_tokens: int):
        used = input_tokens + output_tokens
        self.tokens_used += used
        self.calls_made += 1
        if self.tokens_used > self.max_tokens:
            raise BudgetExceededError(
                f"Task {self.task_id!r} exceeded budget: "
                f"used {self.tokens_used:,} > limit {self.max_tokens:,} tokens "
                f"after {self.calls_made} calls"
            )

    @property
    def remaining(self) -> int:
        return max(0, self.max_tokens - self.tokens_used)

    @property
    def elapsed_seconds(self) -> float:
        return time.time() - self.started_at


class BudgetExceededError(Exception):
    pass


@asynccontextmanager
async def task_budget_context(task_id: str, max_tokens: int):
    """
    Context manager that enforces a per-task token budget.
    Usage:
        async with task_budget_context("batch-job-123", 10_000) as budget:
            result = await ask_with_task_budget("query", budget)
    """
    budget = TaskBudget(task_id=task_id, max_tokens=max_tokens)
    try:
        yield budget
    except BudgetExceededError:
        print(
            f"Task {task_id!r} stopped: used {budget.tokens_used:,}/{max_tokens:,} tokens "
            f"in {budget.elapsed_seconds:.1f}s over {budget.calls_made} calls"
        )
        raise
    finally:
        pass  # Log final usage here if needed


async def ask_with_task_budget(
    user_message: str,
    budget: TaskBudget,
    max_tokens: int = 512,
) -> str:
    """Make an API call within the task's budget."""
    # Pre-check: refuse if remaining budget is too small
    if budget.remaining < 50:
        raise BudgetExceededError(
            f"Task {budget.task_id!r} has only {budget.remaining} tokens remaining"
        )
    client = anthropic.AsyncAnthropic()
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=min(max_tokens, budget.remaining),
        messages=[{"role": "user", "content": user_message}],
    )
    budget.consume(response.usage.input_tokens, response.usage.output_tokens)
    return response.content[0].text


# ── Usage in a batch processor ────────────────────────────────────────────────
async def process_batch(items: list[str], budget_per_item: int = 1000):
    results = []
    for i, item in enumerate(items):
        try:
            async with task_budget_context(f"item-{i}", max_tokens=budget_per_item) as budget:
                result = await ask_with_task_budget(item, budget)
                results.append({"item": item, "result": result, "tokens": budget.tokens_used})
        except BudgetExceededError as e:
            results.append({"item": item, "error": str(e)})
    return results
```

**Expected Token Savings:** 100% beyond budget limit; typical batch job protection
**Environment:** `pip install anthropic`

---

### Option 3: Async Budget Middleware for FastAPI

```python
# budget/fastapi_middleware.py
"""
FastAPI middleware that enforces per-user and global token budgets.
Users who exceed their hourly budget receive 429 Too Many Requests.
Prevents one user from consuming disproportionate API capacity.
"""
import asyncio
import json
import time
from collections import defaultdict
from threading import Lock
import os
import anthropic
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware


# Config
GLOBAL_HOURLY_BUDGET = int(os.environ.get("GLOBAL_HOURLY_TOKEN_BUDGET", "5_000_000"))
PER_USER_HOURLY_BUDGET = int(os.environ.get("PER_USER_HOURLY_TOKEN_BUDGET", "50_000"))


class BudgetStore:
    def __init__(self):
        self._global: dict = {"used": 0, "window_start": time.time()}
        self._users: dict = defaultdict(lambda: {"used": 0, "window_start": time.time()})
        self._lock = Lock()

    def _reset_window(self, record: dict, window_size: float = 3600):
        if time.time() - record["window_start"] > window_size:
            record["used"] = 0
            record["window_start"] = time.time()

    def can_proceed(self, user_id: str, estimated_tokens: int = 1000) -> tuple[bool, str]:
        with self._lock:
            self._reset_window(self._global)
            self._reset_window(self._users[user_id])
            if self._global["used"] + estimated_tokens > GLOBAL_HOURLY_BUDGET:
                return False, f"Global token budget exhausted ({self._global['used']:,}/{GLOBAL_HOURLY_BUDGET:,})"
            if self._users[user_id]["used"] + estimated_tokens > PER_USER_HOURLY_BUDGET:
                return False, f"User token budget exhausted ({self._users[user_id]['used']:,}/{PER_USER_HOURLY_BUDGET:,})"
            return True, ""

    def record(self, user_id: str, tokens: int):
        with self._lock:
            self._global["used"] += tokens
            self._users[user_id]["used"] += tokens


_budget_store = BudgetStore()
app = FastAPI()
client = anthropic.AsyncAnthropic()


class BudgetMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        user_id = request.headers.get("X-User-ID", "anonymous")
        ok, reason = _budget_store.can_proceed(user_id)
        if not ok:
            return JSONResponse(
                {"error": "token_budget_exceeded", "detail": reason},
                status_code=429,
                headers={"Retry-After": "3600"},
            )
        return await call_next(request)


app.add_middleware(BudgetMiddleware)


@app.post("/api/agent/chat")
async def chat(request: Request):
    body = await request.json()
    user_id = request.headers.get("X-User-ID", "anonymous")
    user_message = body.get("message", "")

    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{"role": "user", "content": user_message}],
    )
    total_tokens = response.usage.input_tokens + response.usage.output_tokens
    _budget_store.record(user_id, total_tokens)

    return {
        "response": response.content[0].text,
        "tokens_used": total_tokens,
    }


@app.get("/api/budget/status")
async def budget_status(request: Request):
    user_id = request.headers.get("X-User-ID", "anonymous")
    _, _ = _budget_store.can_proceed(user_id, 0)
    return {
        "global_used": _budget_store._global["used"],
        "global_limit": GLOBAL_HOURLY_BUDGET,
    }
```

**Expected Token Savings:** Enforces hard limits; prevents >100% overage
**Environment:** `pip install fastapi anthropic uvicorn`

---

### Option 4: Extended Thinking Budget Control

```python
# budget/thinking_budget.py
"""
Claude's extended thinking feature has its own token budget (thinking_tokens).
Without control, long reasoning chains can multiply costs 3–10x.
This wrapper caps thinking tokens and falls back to standard mode when budget is tight.
"""
import anthropic
import os
from dataclasses import dataclass


@dataclass
class ThinkingBudgetConfig:
    # Maximum thinking tokens per request
    max_thinking_tokens: int = int(os.environ.get("MAX_THINKING_TOKENS", "5000"))
    # Only use extended thinking if this flag is enabled
    enable_extended_thinking: bool = os.environ.get("ENABLE_THINKING", "true").lower() == "true"
    # Disable extended thinking if remaining budget is below this threshold
    thinking_budget_reserve: int = int(os.environ.get("THINKING_RESERVE_TOKENS", "10000"))


_config = ThinkingBudgetConfig()
_global_thinking_tokens_used = 0


def ask_with_thinking_budget(
    user_message: str,
    complexity: str = "auto",  # "simple" | "complex" | "auto"
    remaining_budget: int = 100_000,
) -> dict:
    """
    Select whether to use extended thinking based on complexity and budget.
    Falls back to standard mode when budget is low.
    """
    global _global_thinking_tokens_used

    use_thinking = (
        _config.enable_extended_thinking
        and complexity != "simple"
        and remaining_budget > _config.thinking_budget_reserve
    )

    # For "auto" complexity, use thinking only for complex-looking messages
    if complexity == "auto" and len(user_message.split()) > 50:
        use_thinking = use_thinking and True
    elif complexity == "auto":
        use_thinking = False

    client = anthropic.Anthropic()

    if use_thinking:
        thinking_budget = min(
            _config.max_thinking_tokens,
            remaining_budget - _config.thinking_budget_reserve,
        )
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=thinking_budget + 1024,
            thinking={"type": "enabled", "budget_tokens": thinking_budget},
            messages=[{"role": "user", "content": user_message}],
        )
        thinking_tokens = sum(
            getattr(b, "thinking", "") and len(getattr(b, "thinking", "")) // 4
            for b in response.content
            if hasattr(b, "thinking")
        )
        _global_thinking_tokens_used += thinking_tokens
    else:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            messages=[{"role": "user", "content": user_message}],
        )

    text_output = next(
        (b.text for b in response.content if hasattr(b, "text")),
        "",
    )
    return {
        "text": text_output,
        "used_thinking": use_thinking,
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
    }
```

**Expected Token Savings:** 60–80% on requests that don't need extended thinking
**Environment:** `pip install anthropic`

---

### Option 5: Budget Alerts Before Exhaustion

```python
# budget/alerts.py
"""
Send alerts at configurable thresholds (e.g., 50%, 80%, 95% of budget)
so engineers can intervene before the budget is fully exhausted.
"""
import time
import os
import threading
import anthropic
from budget.token_counter import TokenBudget, BudgetExceededError


class AlertingBudget(TokenBudget):
    """TokenBudget with threshold-based alerts."""

    def __init__(self, alert_thresholds=(0.5, 0.8, 0.95), **kwargs):
        super().__init__(**kwargs)
        self.alert_thresholds = sorted(alert_thresholds)
        self._alerted_thresholds: set[float] = set()

    def record_usage(self, input_tokens: int, output_tokens: int):
        super().record_usage(input_tokens, output_tokens)
        self._check_thresholds()

    def _check_thresholds(self):
        utilization = (self.input_used + self.output_used) / max(self.max_total_tokens, 1)
        for threshold in self.alert_thresholds:
            if utilization >= threshold and threshold not in self._alerted_thresholds:
                self._alerted_thresholds.add(threshold)
                self._send_alert(threshold, utilization)

    def _send_alert(self, threshold: float, current_utilization: float):
        summary = self.usage_summary
        message = (
            f"TOKEN BUDGET ALERT: {threshold:.0%} threshold reached\n"
            f"  Used:        {summary['total_used']:,} tokens\n"
            f"  Limit:       {summary['max_total']:,} tokens\n"
            f"  Utilization: {summary['utilization_pct']:.1f}%\n"
            f"  Window:      {self.window_seconds/3600:.1f}h\n"
        )
        print(message)  # Replace with Slack/PagerDuty/email in production

        # Also write to a monitoring file for external scrapers
        alert_file = os.environ.get("BUDGET_ALERT_FILE", "/tmp/token_budget_alert.json")
        import json
        with open(alert_file, "w") as f:
            json.dump({
                "threshold": threshold,
                "utilization": current_utilization,
                "timestamp": time.time(),
                **summary,
            }, f, indent=2)


_alerting_budget = AlertingBudget(
    alert_thresholds=[0.5, 0.8, 0.95],
    max_total_tokens=int(os.environ.get("MAX_TOTAL_TOKENS", "2_000_000")),
)


def ask_with_alerts(user_message: str, max_tokens: int = 512) -> str:
    client = anthropic.Anthropic()
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": user_message}],
    )
    _alerting_budget.record_usage(response.usage.input_tokens, response.usage.output_tokens)
    return response.content[0].text
```

**Expected Token Savings:** Alert at 50% allows remediation before full exhaustion
**Environment:** `pip install anthropic`

---

### Option 6: SQLite-Backed Persistent Budget (Survives Restarts)

```python
# budget/persistent_budget.py
"""
SQLite-backed token budget that survives process restarts.
Critical for long-running batch jobs that may be restarted mid-run.
"""
import sqlite3
import time
import os
from pathlib import Path
import anthropic


DB_PATH = Path(os.environ.get("BUDGET_DB_PATH", "/tmp/token_budget.db"))
MAX_TOKENS = int(os.environ.get("MAX_TOTAL_TOKENS", "5_000_000"))
WINDOW_HOURS = float(os.environ.get("BUDGET_WINDOW_HOURS", "24"))


def _get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS token_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp REAL NOT NULL,
            input_tokens INTEGER NOT NULL,
            output_tokens INTEGER NOT NULL,
            task_id TEXT DEFAULT '',
            model TEXT DEFAULT ''
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_timestamp ON token_usage(timestamp)")
    conn.commit()
    return conn


def _get_window_usage(conn: sqlite3.Connection) -> int:
    cutoff = time.time() - WINDOW_HOURS * 3600
    row = conn.execute(
        "SELECT COALESCE(SUM(input_tokens + output_tokens), 0) "
        "FROM token_usage WHERE timestamp > ?",
        (cutoff,),
    ).fetchone()
    return row[0] if row else 0


def record_and_check(
    input_tokens: int,
    output_tokens: int,
    task_id: str = "",
    model: str = "",
) -> dict:
    """Record usage and check if budget is still available."""
    conn = _get_db()
    conn.execute(
        "INSERT INTO token_usage (timestamp, input_tokens, output_tokens, task_id, model) "
        "VALUES (?, ?, ?, ?, ?)",
        (time.time(), input_tokens, output_tokens, task_id, model),
    )
    conn.commit()
    window_total = _get_window_usage(conn)
    remaining = max(0, MAX_TOKENS - window_total)
    return {
        "window_total": window_total,
        "max_tokens": MAX_TOKENS,
        "remaining": remaining,
        "exhausted": window_total >= MAX_TOKENS,
    }


def ask_with_persistent_budget(
    user_message: str,
    task_id: str = "",
    max_tokens: int = 512,
) -> str:
    conn = _get_db()
    window_total = _get_window_usage(conn)
    if window_total >= MAX_TOKENS:
        raise Exception(
            f"Token budget exhausted: used {window_total:,}/{MAX_TOKENS:,} tokens "
            f"in the last {WINDOW_HOURS:.0f}h"
        )

    client = anthropic.Anthropic()
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": user_message}],
    )
    status = record_and_check(
        response.usage.input_tokens,
        response.usage.output_tokens,
        task_id=task_id,
        model="claude-haiku-4-5-20251001",
    )
    if status["exhausted"]:
        print(f"Budget exhausted after this call. Remaining: {status['remaining']:,} tokens")

    return response.content[0].text
```

**Expected Token Savings:** Full enforcement across restarts; prevents unbounded spend
**Environment:** `pip install anthropic` (stdlib sqlite3)

---

## Comparison Table

| Option | Scope | Persistence | Per-User Limits | Alerts | Extended Thinking |
|--------|-------|-------------|-----------------|--------|-------------------|
| 1: Global counter | Process | No (in-memory) | No | No | No |
| 2: Task context | Per-task | No | Via context | Implicit | No |
| 3: FastAPI middleware | Per-user + global | No | Yes | No | No |
| 4: Thinking budget | Per-request | No | No | No | Yes |
| 5: Alerting budget | Process | No | No | Yes (thresholds) | No |
| 6: SQLite persistent | Process + restarts | Yes | Via task_id | No | No |
