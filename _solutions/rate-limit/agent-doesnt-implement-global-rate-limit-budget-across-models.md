---
layout: solution
title: "Agent Doesn't Implement Global Rate Limit Budget Across Models"
category: rate-limit
description: "Agent uses multiple Claude model tiers (Haiku, Sonnet, Opus) without tracking a unified rate limit budget, causing one model to exhaust shared API quota while others are throttled unnecessarily."
tags: [rate-limit, budget, multi-model, tokens, throttling, quota]
---

# Agent Doesn't Implement Global Rate Limit Budget Across Models

## Problem

An agent routes simple tasks to Haiku and complex tasks to Sonnet and Opus. The API account shares a single token-per-minute (TPM) quota across all models. Without a global budget tracker, the Haiku tier can burn through the shared quota, causing Sonnet and Opus calls to hit rate limits — even though those are higher-priority requests.

---

## Option 1: Shared Token Counter with Per-Model Weights

Track a single global token counter. Assign weights to each model tier so high-priority models always have headroom.

```python
import anthropic
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

@dataclass
class ModelBudget:
    model: str
    priority: int          # 1=highest
    reserved_tpm: int      # tokens reserved for this model per minute
    used_this_minute: int = 0
    total_calls: int = 0
    total_tokens: int = 0

class GlobalRateLimitBudget:
    def __init__(self, total_tpm: int = 100_000):
        self.total_tpm = total_tpm
        self.global_used = 0
        self.window_start = time.monotonic()
        self.budgets: dict[str, ModelBudget] = {
            "claude-haiku-4-5-20251001": ModelBudget("claude-haiku-4-5-20251001", priority=3, reserved_tpm=20_000),
            "claude-sonnet-4-6":         ModelBudget("claude-sonnet-4-6",         priority=2, reserved_tpm=50_000),
            "claude-opus-4-6":           ModelBudget("claude-opus-4-6",           priority=1, reserved_tpm=30_000),
        }
        self._lock = threading.Lock()

    def _reset_if_new_window(self):
        now = time.monotonic()
        if now - self.window_start >= 60.0:
            self.global_used = 0
            self.window_start = now
            for b in self.budgets.values():
                b.used_this_minute = 0

    def can_call(self, model: str, estimated_tokens: int = 1000) -> tuple[bool, str]:
        with self._lock:
            self._reset_if_new_window()
            budget = self.budgets.get(model)
            if not budget:
                return True, ""  # Unknown model — allow
            if self.global_used + estimated_tokens > self.total_tpm:
                return False, f"Global TPM limit reached ({self.global_used}/{self.total_tpm})"
            if budget.used_this_minute + estimated_tokens > budget.reserved_tpm:
                return False, f"{model} reserved quota exhausted ({budget.used_this_minute}/{budget.reserved_tpm})"
            return True, ""

    def record_usage(self, model: str, tokens_used: int):
        with self._lock:
            self.global_used += tokens_used
            budget = self.budgets.get(model)
            if budget:
                budget.used_this_minute += tokens_used
                budget.total_tokens += tokens_used
                budget.total_calls += 1

    def stats(self) -> dict:
        with self._lock:
            return {
                "global_used": self.global_used,
                "global_tpm": self.total_tpm,
                "global_pct": self.global_used / self.total_tpm,
                "models": {
                    m: {"used": b.used_this_minute, "reserved": b.reserved_tpm,
                        "calls": b.total_calls}
                    for m, b in self.budgets.items()
                }
            }

budget_manager = GlobalRateLimitBudget(total_tpm=100_000)
client = anthropic.Anthropic()

def call_with_global_budget(
    model: str,
    messages: list[dict],
    max_tokens: int = 256,
    estimated_tokens: int = 500,
    max_wait: float = 60.0
) -> Optional[anthropic.types.Message]:
    waited = 0.0
    while waited < max_wait:
        allowed, reason = budget_manager.can_call(model, estimated_tokens)
        if allowed:
            response = client.messages.create(
                model=model, max_tokens=max_tokens, messages=messages
            )
            actual = response.usage.input_tokens + response.usage.output_tokens
            budget_manager.record_usage(model, actual)
            return response
        print(f"[budget] {reason} — waiting 5s")
        time.sleep(5.0)
        waited += 5.0
    print(f"[budget] Timeout waiting for {model} budget")
    return None

# Demo: calls across three model tiers
for model, prompt in [
    ("claude-haiku-4-5-20251001", "What is 2+2?"),
    ("claude-sonnet-4-6",         "Explain gradient descent."),
    ("claude-haiku-4-5-20251001", "Name a planet."),
]:
    response = call_with_global_budget(model, [{"role": "user", "content": prompt}])
    if response:
        print(f"[{model.split('-')[1]}] {response.content[0].text[:60]}")

print(f"\nGlobal stats: {budget_manager.stats()['global_used']} tokens used")

# Expected Token Savings: Reserved per-model quotas ensure Opus always has headroom even when Haiku is busy. Priority ordering prevents low-priority models from starving high-priority ones.
# Environment: ANTHROPIC_API_KEY required. Uses threading (stdlib).
```

---

## Option 2: Sliding Window Budget with Priority Preemption

Use a sliding 60-second window instead of a fixed reset. High-priority models can preempt low-priority budget allocations.

```python
import anthropic
import threading
import time
from collections import deque
from dataclasses import dataclass

@dataclass
class TokenUsageRecord:
    model: str
    tokens: int
    timestamp: float
    priority: int

class SlidingWindowBudget:
    def __init__(self, total_tpm: int = 80_000, window_seconds: float = 60.0):
        self.total_tpm = total_tpm
        self.window = window_seconds
        self.records: deque[TokenUsageRecord] = deque()
        self._lock = threading.Lock()
        self.model_priority = {
            "claude-opus-4-6":           1,
            "claude-sonnet-4-6":         2,
            "claude-haiku-4-5-20251001": 3,
        }

    def _evict_old(self):
        cutoff = time.monotonic() - self.window
        while self.records and self.records[0].timestamp < cutoff:
            self.records.popleft()

    def current_usage(self) -> int:
        with self._lock:
            self._evict_old()
            return sum(r.tokens for r in self.records)

    def usage_by_priority(self) -> dict[int, int]:
        with self._lock:
            self._evict_old()
            result: dict[int, int] = {}
            for r in self.records:
                result[r.priority] = result.get(r.priority, 0) + r.tokens
            return result

    def can_call(self, model: str, estimated_tokens: int) -> tuple[bool, str]:
        with self._lock:
            self._evict_old()
            total_used = sum(r.tokens for r in self.records)
            priority = self.model_priority.get(model, 3)

            if total_used + estimated_tokens > self.total_tpm:
                # Check if we can preempt lower-priority usage
                lower_priority_tokens = sum(
                    r.tokens for r in self.records if r.priority > priority
                )
                if total_used - lower_priority_tokens + estimated_tokens <= self.total_tpm:
                    return True, f"preempted {lower_priority_tokens} lower-priority tokens"
                return False, f"Budget exhausted: {total_used}/{self.total_tpm} TPM"

            return True, ""

    def record(self, model: str, tokens: int):
        with self._lock:
            self.records.append(TokenUsageRecord(
                model=model,
                tokens=tokens,
                timestamp=time.monotonic(),
                priority=self.model_priority.get(model, 3)
            ))

sliding_budget = SlidingWindowBudget(total_tpm=80_000)
client = anthropic.Anthropic()

def call_with_sliding_budget(model: str, prompt: str, estimated_tokens: int = 500) -> str:
    allowed, note = sliding_budget.can_call(model, estimated_tokens)
    if not allowed:
        return f"[RATE_LIMITED] {note}"

    response = client.messages.create(
        model=model, max_tokens=256,
        messages=[{"role": "user", "content": prompt}]
    )
    actual = response.usage.input_tokens + response.usage.output_tokens
    sliding_budget.record(model, actual)

    usage = sliding_budget.current_usage()
    print(f"[{model.split('-')[1]}] used={actual} | window_total={usage}/{sliding_budget.total_tpm}")
    return response.content[0].text

results = []
for model, prompt in [
    ("claude-haiku-4-5-20251001", "What color is the sky?"),
    ("claude-sonnet-4-6",         "What is machine learning?"),
    ("claude-opus-4-6",           "Explain consciousness briefly."),
    ("claude-haiku-4-5-20251001", "What is water made of?"),
]:
    result = call_with_sliding_budget(model, prompt)
    results.append(result[:60])

print(f"\nCompleted {len(results)} calls")
print(f"Priority usage: {sliding_budget.usage_by_priority()}")

# Expected Token Savings: Sliding window is more accurate than fixed-minute resets — allows burst usage while respecting sustained limits. Priority preemption ensures Opus never waits behind Haiku backlog.
# Environment: ANTHROPIC_API_KEY required. Uses threading, collections.deque (stdlib).
```

---

## Option 3: SQLite-Persisted Cross-Process Budget

Share rate limit state across multiple agent processes using SQLite, so workers on different machines respect the same global quota.

```python
import anthropic
import sqlite3
import time
import os
from dataclasses import dataclass
from typing import Optional

DB_PATH = ":memory:"  # Use file path in production: "/tmp/rate_limit.db"
TOTAL_TPM = 100_000
WINDOW_SECONDS = 60.0

def init_budget_db(path: str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS token_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            model TEXT,
            tokens INTEGER,
            worker_id TEXT,
            recorded_at REAL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_time ON token_usage(recorded_at)")
    conn.commit()
    return conn

def get_window_usage(conn: sqlite3.Connection) -> int:
    cutoff = time.time() - WINDOW_SECONDS
    row = conn.execute(
        "SELECT COALESCE(SUM(tokens), 0) FROM token_usage WHERE recorded_at >= ?",
        (cutoff,)
    ).fetchone()
    return row[0]

def get_model_usage(conn: sqlite3.Connection, model: str) -> int:
    cutoff = time.time() - WINDOW_SECONDS
    row = conn.execute(
        "SELECT COALESCE(SUM(tokens), 0) FROM token_usage WHERE model=? AND recorded_at >= ?",
        (model, cutoff)
    ).fetchone()
    return row[0]

MODEL_LIMITS = {
    "claude-haiku-4-5-20251001": 30_000,
    "claude-sonnet-4-6":         50_000,
    "claude-opus-4-6":           20_000,
}

def check_budget(conn: sqlite3.Connection, model: str, estimated: int) -> tuple[bool, str]:
    global_used = get_window_usage(conn)
    if global_used + estimated > TOTAL_TPM:
        return False, f"Global TPM: {global_used}/{TOTAL_TPM}"
    model_limit = MODEL_LIMITS.get(model, TOTAL_TPM)
    model_used = get_model_usage(conn, model)
    if model_used + estimated > model_limit:
        return False, f"{model.split('-')[1]} limit: {model_used}/{model_limit}"
    return True, ""

def record_usage(conn: sqlite3.Connection, model: str, tokens: int, worker_id: str):
    conn.execute(
        "INSERT INTO token_usage (model, tokens, worker_id, recorded_at) VALUES (?,?,?,?)",
        (model, tokens, worker_id, time.time())
    )
    conn.commit()

def prune_old_records(conn: sqlite3.Connection):
    cutoff = time.time() - WINDOW_SECONDS * 2
    conn.execute("DELETE FROM token_usage WHERE recorded_at < ?", (cutoff,))
    conn.commit()

client = anthropic.Anthropic()
conn = init_budget_db()
worker_id = f"worker_{os.getpid()}"

def call_with_db_budget(
    model: str,
    prompt: str,
    estimated_tokens: int = 600,
    max_wait: float = 30.0
) -> Optional[str]:
    waited = 0.0
    while waited < max_wait:
        allowed, reason = check_budget(conn, model, estimated_tokens)
        if allowed:
            response = client.messages.create(
                model=model, max_tokens=256,
                messages=[{"role": "user", "content": prompt}]
            )
            actual = response.usage.input_tokens + response.usage.output_tokens
            record_usage(conn, model, actual, worker_id)
            prune_old_records(conn)
            global_used = get_window_usage(conn)
            print(f"[{model.split('-')[1]}] {actual} tokens | global={global_used}/{TOTAL_TPM}")
            return response.content[0].text
        print(f"[wait] {reason}")
        time.sleep(5.0)
        waited += 5.0
    return None

for model, prompt in [
    ("claude-haiku-4-5-20251001", "Name the days of the week."),
    ("claude-sonnet-4-6",         "What is Bernoulli's principle?"),
    ("claude-haiku-4-5-20251001", "What is photosynthesis?"),
]:
    result = call_with_db_budget(model, prompt)
    if result:
        print(f"Result: {result[:60]}\n")

# Expected Token Savings: SQLite persistence enables multi-process quota sharing — no more one worker starving another. Cross-process visibility prevents collective over-quota errors.
# Environment: ANTHROPIC_API_KEY required. Uses sqlite3 (stdlib). Change DB_PATH to shared file for multi-process use.
```

---

## Option 4: Async Budget with Model Downgrade on Quota Pressure

When quota pressure is high, automatically downgrade from an expensive model to a cheaper one while respecting quality requirements.

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass

@dataclass
class QuotaState:
    used_tokens: int = 0
    window_start: float = 0.0
    total_tpm: int = 80_000
    downgrades: int = 0
    blocks: int = 0

    def reset_if_needed(self):
        if time.monotonic() - self.window_start >= 60.0:
            self.used_tokens = 0
            self.window_start = time.monotonic()

    @property
    def pressure(self) -> float:
        return self.used_tokens / self.total_tpm

MODEL_FALLBACK_CHAIN = {
    "claude-opus-4-6":           ["claude-sonnet-4-6", "claude-haiku-4-5-20251001"],
    "claude-sonnet-4-6":         ["claude-haiku-4-5-20251001"],
    "claude-haiku-4-5-20251001": [],
}

HIGH_PRESSURE_THRESHOLD = 0.75   # Downgrade at 75% quota used
BLOCK_THRESHOLD = 0.95           # Block non-essential at 95%

quota = QuotaState(window_start=time.monotonic())
client = anthropic.AsyncAnthropic()
_quota_lock = asyncio.Lock()

async def call_with_downgrade(
    preferred_model: str,
    prompt: str,
    essential: bool = True,
    max_tokens: int = 256
) -> dict:
    async with _quota_lock:
        quota.reset_if_needed()
        pressure = quota.pressure

    if not essential and pressure >= BLOCK_THRESHOLD:
        quota.blocks += 1
        return {"blocked": True, "reason": f"Non-essential request blocked at {pressure:.0%} quota pressure"}

    # Determine actual model based on pressure
    model = preferred_model
    if pressure >= HIGH_PRESSURE_THRESHOLD:
        fallbacks = MODEL_FALLBACK_CHAIN.get(preferred_model, [])
        if fallbacks:
            model = fallbacks[0]
            quota.downgrades += 1
            print(f"[downgrade] {preferred_model.split('-')[1]} → {model.split('-')[1]} (pressure={pressure:.0%})")

    try:
        response = await client.messages.create(
            model=model, max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}]
        )
        actual_tokens = response.usage.input_tokens + response.usage.output_tokens
        async with _quota_lock:
            quota.used_tokens += actual_tokens

        return {
            "result": response.content[0].text,
            "model_used": model,
            "preferred_model": preferred_model,
            "was_downgraded": model != preferred_model,
            "tokens": actual_tokens
        }
    except anthropic.RateLimitError:
        return {"rate_limited": True, "model": model}

async def main():
    tasks = [
        ("claude-sonnet-4-6",         "Explain DNA replication.", True),
        ("claude-haiku-4-5-20251001", "What is 5*5?",            True),
        ("claude-sonnet-4-6",         "What is the speed of light?", True),
        ("claude-opus-4-6",           "Summarize recent AI news.",   False),  # non-essential
        ("claude-haiku-4-5-20251001", "List 3 colors.",           True),
    ]

    results = await asyncio.gather(*[
        call_with_downgrade(model, prompt, essential)
        for model, prompt, essential in tasks
    ])

    downgrades = sum(1 for r in results if r.get("was_downgraded"))
    blocked = sum(1 for r in results if r.get("blocked"))
    print(f"\nCompleted: {len(results)} tasks")
    print(f"Downgrades: {downgrades}, Blocked: {blocked}")
    print(f"Global quota used: {quota.used_tokens}/{quota.total_tpm} ({quota.pressure:.0%})")

asyncio.run(main())

# Expected Token Savings: Automatic downgrade from Sonnet to Haiku at high pressure saves 3–5x per-token cost. Non-essential blocking saves 100% of those tokens. Both preserve headroom for critical Opus calls.
# Environment: ANTHROPIC_API_KEY required. Uses asyncio (stdlib).
```

---

## Option 5: Per-Tenant Budget with Model Allocation

In multi-tenant systems, allocate per-tenant token budgets and enforce per-model maximums to prevent one tenant from starving others.

```python
import anthropic
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

@dataclass
class TenantAllocation:
    tenant_id: str
    total_tpm: int
    model_limits: dict[str, int]  # model → max TPM for this tenant
    used: dict[str, int] = field(default_factory=dict)
    window_start: float = field(default_factory=time.monotonic)

    def reset_if_needed(self):
        if time.monotonic() - self.window_start >= 60.0:
            self.used = {}
            self.window_start = time.monotonic()

    def total_used(self) -> int:
        return sum(self.used.values())

    def model_used(self, model: str) -> int:
        return self.used.get(model, 0)

    def can_use(self, model: str, tokens: int) -> tuple[bool, str]:
        self.reset_if_needed()
        if self.total_used() + tokens > self.total_tpm:
            return False, f"Tenant {self.tenant_id} total TPM exhausted ({self.total_used()}/{self.total_tpm})"
        model_limit = self.model_limits.get(model, self.total_tpm)
        if self.model_used(model) + tokens > model_limit:
            return False, f"Tenant {self.tenant_id} {model.split('-')[1]} limit ({self.model_used(model)}/{model_limit})"
        return True, ""

    def record(self, model: str, tokens: int):
        self.used[model] = self.used.get(model, 0) + tokens

TENANT_CONFIGS = {
    "free": TenantAllocation("free", total_tpm=10_000,
        model_limits={"claude-haiku-4-5-20251001": 10_000}),
    "pro": TenantAllocation("pro", total_tpm=50_000,
        model_limits={"claude-haiku-4-5-20251001": 30_000, "claude-sonnet-4-6": 20_000}),
    "enterprise": TenantAllocation("enterprise", total_tpm=100_000,
        model_limits={"claude-haiku-4-5-20251001": 40_000, "claude-sonnet-4-6": 40_000, "claude-opus-4-6": 20_000}),
}

_lock = threading.Lock()
client = anthropic.Anthropic()

def call_for_tenant(
    tenant_id: str,
    model: str,
    prompt: str,
    estimated_tokens: int = 500
) -> Optional[dict]:
    with _lock:
        allocation = TENANT_CONFIGS.get(tenant_id)
        if not allocation:
            return {"error": f"Unknown tenant: {tenant_id}"}
        allowed, reason = allocation.can_use(model, estimated_tokens)

    if not allowed:
        print(f"[tenant-limit] {reason}")
        return {"blocked": True, "reason": reason}

    try:
        response = client.messages.create(
            model=model, max_tokens=256,
            messages=[{"role": "user", "content": prompt}]
        )
        actual = response.usage.input_tokens + response.usage.output_tokens
        with _lock:
            allocation.record(model, actual)
        return {
            "result": response.content[0].text[:80],
            "tenant": tenant_id,
            "model": model.split('-')[1],
            "tokens": actual
        }
    except anthropic.RateLimitError as e:
        return {"rate_limited": True, "error": str(e)}

for tenant, model, prompt in [
    ("free",       "claude-haiku-4-5-20251001", "What is Python?"),
    ("pro",        "claude-sonnet-4-6",          "Explain microservices."),
    ("enterprise", "claude-opus-4-6",            "Describe AI safety."),
    ("free",       "claude-sonnet-4-6",          "Upgrade attempt"),  # Should be blocked
]:
    result = call_for_tenant(tenant, model, prompt)
    if result:
        status = "BLOCKED" if result.get("blocked") else "OK"
        print(f"[{tenant}/{result.get('model', 'N/A')}] {status}: {str(result.get('result', result.get('reason', '')))[:60]}")

# Expected Token Savings: Per-tenant limits prevent free-tier users from impacting enterprise SLAs. Model-specific limits ensure free tenants can't use expensive Sonnet/Opus models.
# Environment: ANTHROPIC_API_KEY required. Uses threading (stdlib).
```

---

## Option 6: Adaptive Budget with Exponential Backoff Queue

Queue requests when budget is exceeded, retry with exponential backoff, and reprioritize by model tier when budget recovers.

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

@dataclass(order=True)
class BudgetedRequest:
    priority: int                       # lower = higher priority
    enqueued_at: float = field(compare=False)
    model: str = field(compare=False)
    prompt: str = field(compare=False)
    estimated_tokens: int = field(compare=False)
    attempt: int = field(compare=False, default=0)
    result: asyncio.Future = field(compare=False, default=None)

MODEL_PRIORITY = {
    "claude-opus-4-6":           1,
    "claude-sonnet-4-6":         2,
    "claude-haiku-4-5-20251001": 3,
}

class AdaptiveBudgetQueue:
    def __init__(self, tpm: int = 60_000):
        self.tpm = tpm
        self.used = 0
        self.window_start = time.monotonic()
        self._queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self._lock = asyncio.Lock()
        self._client = anthropic.AsyncAnthropic()

    def _reset_if_needed(self):
        if time.monotonic() - self.window_start >= 60.0:
            self.used = 0
            self.window_start = time.monotonic()

    @property
    def available(self) -> int:
        self._reset_if_needed()
        return max(0, self.tpm - self.used)

    async def submit(self, model: str, prompt: str, estimated_tokens: int = 600) -> str:
        loop = asyncio.get_event_loop()
        future = loop.create_future()
        priority = MODEL_PRIORITY.get(model, 3)
        req = BudgetedRequest(
            priority=priority,
            enqueued_at=time.monotonic(),
            model=model,
            prompt=prompt,
            estimated_tokens=estimated_tokens,
            result=future
        )
        await self._queue.put(req)
        return await future

    async def process_loop(self, max_iterations: int = 50):
        for _ in range(max_iterations):
            if self._queue.empty():
                await asyncio.sleep(0.1)
                continue

            req = await self._queue.get()
            async with self._lock:
                self._reset_if_needed()
                if self.available < req.estimated_tokens:
                    # Re-queue with backoff
                    wait = min(2.0 ** req.attempt, 30.0)
                    req.attempt += 1
                    print(f"[backoff] {req.model.split('-')[1]} attempt {req.attempt}, wait={wait:.1f}s")
                    await asyncio.sleep(wait)
                    await self._queue.put(req)
                    continue

            try:
                response = await self._client.messages.create(
                    model=req.model, max_tokens=256,
                    messages=[{"role": "user", "content": req.prompt}]
                )
                actual = response.usage.input_tokens + response.usage.output_tokens
                async with self._lock:
                    self.used += actual
                wait_secs = time.monotonic() - req.enqueued_at
                print(f"[done] {req.model.split('-')[1]} wait={wait_secs:.2f}s tokens={actual}")
                if not req.result.done():
                    req.result.set_result(response.content[0].text)
            except Exception as exc:
                if not req.result.done():
                    req.result.set_exception(exc)

async def main():
    queue = AdaptiveBudgetQueue(tpm=60_000)
    processor = asyncio.create_task(queue.process_loop(max_iterations=30))

    requests = [
        ("claude-haiku-4-5-20251001", "Name a fruit."),
        ("claude-sonnet-4-6",         "What is a neural network?"),
        ("claude-opus-4-6",           "Explain Gödel's incompleteness theorem briefly."),
        ("claude-haiku-4-5-20251001", "What is the boiling point of water?"),
    ]

    results = await asyncio.gather(*[
        queue.submit(model, prompt)
        for model, prompt in requests
    ])

    processor.cancel()
    for i, r in enumerate(results):
        print(f"Result {i}: {r[:60]}")

asyncio.run(main())

# Expected Token Savings: Priority queue ensures Opus requests drain first even when submitted last. Exponential backoff prevents thundering herd after rate limit recovery. Adaptive scheduling maximizes throughput.
# Environment: ANTHROPIC_API_KEY required. Uses asyncio (stdlib).
```

---

## Comparison

| Option | Budget Scope | Multi-Process | Model Downgrade | Priority | Best For |
|--------|-------------|---------------|-----------------|----------|----------|
| 1: Shared Counter + Weights | Global + per-model | No (in-memory) | No | Reserved quota | Single-process multi-model agents |
| 2: Sliding Window + Preemption | 60s sliding | No | No | Priority preemption | Bursty traffic patterns |
| 3: SQLite Cross-Process | Global + per-model | Yes | No | No | Multi-worker distributed agents |
| 4: Async Downgrade | Global pressure | No | Yes (auto) | Essential flag | Cost-sensitive async agents |
| 5: Per-Tenant Allocation | Per-tenant + per-model | No | No | Tenant tier | Multi-tenant SaaS platforms |
| 6: Adaptive Backoff Queue | Global TPM | No | No | Model priority | High-throughput queued workloads |
