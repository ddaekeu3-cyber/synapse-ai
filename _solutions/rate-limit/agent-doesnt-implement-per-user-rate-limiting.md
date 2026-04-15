---
layout: solution
title: "Agent Doesn't Implement Per-User Rate Limiting"
category: rate-limit
description: "All users share the same API quota. One heavy user starves everyone else, costs spike unpredictably, and Anthropic rate limits hit the entire service."
tags: [rate-limiting, fairness, token-bucket, sliding-window, redis, quota]
---

## Symptom

Your agent handles multiple users but applies no per-user throttling. A single user running batch jobs consumes the full API quota, causing 429 errors for every other user. Monthly costs are impossible to predict, and premium users get the same treatment as free-tier users.

## Root Cause

Rate limiting is implemented (if at all) at the application level as a single global counter. There is no per-identity tracking, no tiered quota system, and no backpressure that returns informative errors to the right caller. The Anthropic SDK retry logic silently absorbs rate limit hits by waiting — but it waits using global backoff, blocking all concurrent requests for the same pool.

## Fix

---

### Option 1: In-Memory Token Bucket Per User

Simple token bucket with a fixed refill rate. Works for single-process deployments.

```python
import time
import threading
from dataclasses import dataclass, field
from collections import defaultdict
import anthropic

@dataclass
class TokenBucket:
    capacity: float          # max tokens (burst size)
    refill_rate: float       # tokens per second
    tokens: float = field(init=False)
    last_refill: float = field(init=False)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self):
        self.tokens = self.capacity
        self.last_refill = time.monotonic()

    def consume(self, amount: float = 1.0) -> tuple[bool, float]:
        """Returns (allowed, retry_after_seconds)."""
        with self.lock:
            now = time.monotonic()
            elapsed = now - self.last_refill
            self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
            self.last_refill = now

            if self.tokens >= amount:
                self.tokens -= amount
                return True, 0.0
            else:
                wait = (amount - self.tokens) / self.refill_rate
                return False, wait


class PerUserRateLimiter:
    """
    Tier-based token bucket rate limiter.

    Tiers:
      free:    10 requests/min, burst 5
      pro:     60 requests/min, burst 20
      premium: 300 requests/min, burst 100
    """
    TIER_CONFIG = {
        "free":    {"capacity": 5,   "refill_rate": 10 / 60},
        "pro":     {"capacity": 20,  "refill_rate": 60 / 60},
        "premium": {"capacity": 100, "refill_rate": 300 / 60},
    }

    def __init__(self):
        self._buckets: dict[str, TokenBucket] = {}
        self._lock = threading.Lock()

    def _get_bucket(self, user_id: str, tier: str) -> TokenBucket:
        with self._lock:
            if user_id not in self._buckets:
                cfg = self.TIER_CONFIG.get(tier, self.TIER_CONFIG["free"])
                self._buckets[user_id] = TokenBucket(**cfg)
            return self._buckets[user_id]

    def check(self, user_id: str, tier: str = "free") -> tuple[bool, float]:
        bucket = self._get_bucket(user_id, tier)
        return bucket.consume()


limiter = PerUserRateLimiter()
client = anthropic.Anthropic()


def chat(user_id: str, message: str, tier: str = "free") -> str:
    allowed, retry_after = limiter.check(user_id, tier)
    if not allowed:
        raise RateLimitError(
            f"Rate limit exceeded for user {user_id}. "
            f"Retry after {retry_after:.1f}s."
        )

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        messages=[{"role": "user", "content": message}],
    )
    return response.content[0].text


class RateLimitError(Exception):
    pass


# Usage
if __name__ == "__main__":
    for i in range(8):
        try:
            reply = chat("user_alice", f"Query {i}", tier="free")
            print(f"[{i}] OK: {reply[:40]}")
        except RateLimitError as e:
            print(f"[{i}] BLOCKED: {e}")
```

**Expected Token Savings**: Prevents runaway usage by single users; predictable per-tier spend.
**Environment**: Single-process Python, no external dependencies.

---

### Option 2: Redis Sliding Window Counter

Distributed rate limiting using Redis sorted sets. Safe across multiple workers/pods.

```python
import time
import redis
import anthropic
from functools import wraps

r = redis.Redis(host="localhost", port=6379, decode_responses=True)
client = anthropic.Anthropic()

TIER_LIMITS = {
    "free":    {"requests": 10,  "window_seconds": 60},
    "pro":     {"requests": 60,  "window_seconds": 60},
    "premium": {"requests": 300, "window_seconds": 60},
}


def sliding_window_check(user_id: str, tier: str = "free") -> tuple[bool, int]:
    """
    Sliding window using Redis sorted set.
    Returns (allowed, requests_remaining).
    """
    cfg = TIER_LIMITS.get(tier, TIER_LIMITS["free"])
    limit = cfg["requests"]
    window = cfg["window_seconds"]

    key = f"ratelimit:{user_id}"
    now = time.time()
    window_start = now - window

    pipe = r.pipeline()
    # Remove old entries outside the window
    pipe.zremrangebyscore(key, 0, window_start)
    # Count current requests in window
    pipe.zcard(key)
    # Add current request with timestamp as score
    pipe.zadd(key, {str(now): now})
    # Set expiry so keys clean themselves up
    pipe.expire(key, window * 2)
    results = pipe.execute()

    current_count = results[1]  # count before adding this request

    if current_count >= limit:
        # Remove the just-added entry (request not allowed)
        r.zrem(key, str(now))
        return False, 0

    return True, limit - current_count - 1


def rate_limited(tier_fn):
    """Decorator that extracts tier from user context."""
    def decorator(func):
        @wraps(func)
        def wrapper(user_id: str, *args, **kwargs):
            tier = tier_fn(user_id)
            allowed, remaining = sliding_window_check(user_id, tier)
            if not allowed:
                raise Exception(
                    f"Rate limit exceeded. Tier: {tier}. "
                    f"Retry in up to 60 seconds."
                )
            result = func(user_id, *args, **kwargs)
            print(f"  [{user_id}] {remaining} requests remaining this minute")
            return result
        return wrapper
    return decorator


def get_user_tier(user_id: str) -> str:
    # In production: DB lookup
    tiers = {"alice": "premium", "bob": "pro", "carol": "free"}
    return tiers.get(user_id, "free")


@rate_limited(get_user_tier)
def agent_chat(user_id: str, message: str) -> str:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{"role": "user", "content": message}],
    )
    return response.content[0].text


# Usage
if __name__ == "__main__":
    for i in range(5):
        try:
            reply = agent_chat("carol", f"Free tier query {i}")
            print(f"carol[{i}]: {reply[:50]}")
        except Exception as e:
            print(f"carol[{i}] BLOCKED: {e}")
```

**Expected Token Savings**: Exact per-minute enforcement across distributed workers.
**Environment**: Redis required. Scales horizontally.

---

### Option 3: Tiered Token-Cost Rate Limiting

Rate limit by token consumption, not just request count. Prevents users from gaming limits with single massive requests.

```python
import asyncio
import time
from dataclasses import dataclass
import anthropic

@dataclass
class TokenQuota:
    max_tokens_per_hour: int
    max_tokens_per_request: int
    used_this_hour: int = 0
    hour_start: float = 0.0

    def __post_init__(self):
        self.hour_start = time.monotonic()

    def reset_if_needed(self):
        now = time.monotonic()
        if now - self.hour_start >= 3600:
            self.used_this_hour = 0
            self.hour_start = now

    def can_spend(self, tokens: int) -> tuple[bool, str]:
        self.reset_if_needed()
        if tokens > self.max_tokens_per_request:
            return False, f"Request too large: {tokens} > {self.max_tokens_per_request} per-request limit"
        if self.used_this_hour + tokens > self.max_tokens_per_hour:
            remaining = self.max_tokens_per_hour - self.used_this_hour
            return False, f"Hourly quota exceeded. {remaining} tokens left this hour."
        return True, ""

    def record(self, tokens: int):
        self.reset_if_needed()
        self.used_this_hour += tokens


TIER_QUOTAS = {
    "free":    TokenQuota(max_tokens_per_hour=50_000,   max_tokens_per_request=2_000),
    "pro":     TokenQuota(max_tokens_per_hour=500_000,  max_tokens_per_request=10_000),
    "premium": TokenQuota(max_tokens_per_hour=5_000_000, max_tokens_per_request=50_000),
}

user_quotas: dict[str, TokenQuota] = {}


def get_quota(user_id: str, tier: str) -> TokenQuota:
    if user_id not in user_quotas:
        base = TIER_QUOTAS[tier]
        user_quotas[user_id] = TokenQuota(
            max_tokens_per_hour=base.max_tokens_per_hour,
            max_tokens_per_request=base.max_tokens_per_request,
        )
    return user_quotas[user_id]


async def token_aware_chat(
    user_id: str,
    message: str,
    tier: str = "free",
    max_tokens: int = 1024,
) -> str:
    quota = get_quota(user_id, tier)

    # Pre-check: estimate input cost (rough: 4 chars ≈ 1 token)
    estimated_input = len(message) // 4
    estimated_total = estimated_input + max_tokens
    allowed, reason = quota.can_spend(estimated_total)
    if not allowed:
        raise Exception(f"Token quota exceeded for {user_id}: {reason}")

    client = anthropic.AsyncAnthropic()
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": message}],
    )

    # Record actual usage
    actual_tokens = response.usage.input_tokens + response.usage.output_tokens
    quota.record(actual_tokens)

    print(
        f"[{user_id}] Used {actual_tokens} tokens. "
        f"Hour total: {quota.used_this_hour}/{quota.max_tokens_per_hour}"
    )
    return response.content[0].text


async def main():
    tasks = [
        token_aware_chat("alice", "Explain quantum computing", tier="pro", max_tokens=2048),
        token_aware_chat("bob", "Hello", tier="free", max_tokens=100),
        token_aware_chat("alice", "Write a 5000 word essay", tier="pro", max_tokens=8000),
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for r in results:
        if isinstance(r, Exception):
            print(f"ERROR: {r}")
        else:
            print(f"OK: {r[:60]}")


asyncio.run(main())
```

**Expected Token Savings**: Caps large-request abuse; hourly budgets prevent overnight runaway.
**Environment**: Async Python, in-memory per-process.

---

### Option 4: FastAPI Middleware with Rate Limit Headers

Production HTTP middleware that returns RFC 7231 compliant rate limit headers.

```python
import time
import asyncio
from collections import defaultdict
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
import anthropic

app = FastAPI()
client = anthropic.AsyncAnthropic()

# Per-user sliding window state: {user_id: [timestamp, ...]}
request_log: dict[str, list[float]] = defaultdict(list)
log_lock = asyncio.Lock()

RATE_LIMITS = {
    "free":    10,
    "pro":     60,
    "premium": 300,
}
WINDOW = 60  # seconds


def get_user_from_request(request: Request) -> tuple[str, str]:
    """Extract user_id and tier from API key header."""
    api_key = request.headers.get("X-API-Key", "")
    # In production: DB lookup from api_key
    mock_users = {
        "key_free_001": ("user_001", "free"),
        "key_pro_001":  ("user_002", "pro"),
        "key_prm_001":  ("user_003", "premium"),
    }
    return mock_users.get(api_key, ("anonymous", "free"))


@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    user_id, tier = get_user_from_request(request)
    limit = RATE_LIMITS.get(tier, RATE_LIMITS["free"])
    now = time.time()
    window_start = now - WINDOW

    async with log_lock:
        # Clean old entries
        request_log[user_id] = [t for t in request_log[user_id] if t > window_start]
        count = len(request_log[user_id])

        if count >= limit:
            oldest = request_log[user_id][0]
            retry_after = int(WINDOW - (now - oldest)) + 1
            return JSONResponse(
                status_code=429,
                content={"error": "rate_limit_exceeded", "retry_after": retry_after},
                headers={
                    "Retry-After": str(retry_after),
                    "X-RateLimit-Limit": str(limit),
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Reset": str(int(oldest + WINDOW)),
                },
            )

        request_log[user_id].append(now)
        remaining = limit - count - 1

    response = await call_next(request)
    response.headers["X-RateLimit-Limit"] = str(limit)
    response.headers["X-RateLimit-Remaining"] = str(remaining)
    response.headers["X-RateLimit-Reset"] = str(int(now + WINDOW))
    return response


@app.post("/chat")
async def chat_endpoint(request: Request):
    body = await request.json()
    message = body.get("message", "")
    _, tier = get_user_from_request(request)

    model = {
        "free":    "claude-haiku-4-5-20251001",
        "pro":     "claude-haiku-4-5-20251001",
        "premium": "claude-sonnet-4-6",
    }.get(tier, "claude-haiku-4-5-20251001")

    response = await client.messages.create(
        model=model,
        max_tokens=1024,
        messages=[{"role": "user", "content": message}],
    )
    return {"reply": response.content[0].text}
```

**Expected Token Savings**: RFC-compliant headers let clients back off gracefully; tier-model routing also reduces cost.
**Environment**: FastAPI service, single-process or behind sticky-session load balancer.

---

### Option 5: Leaky Bucket Queue with Priority Lanes

Requests drain at a fixed rate. High-tier users get a dedicated fast lane; free users wait in a slower lane.

```python
import asyncio
import time
from dataclasses import dataclass, field
from typing import Callable, Awaitable
import anthropic

@dataclass(order=True)
class QueuedRequest:
    priority: int          # lower = higher priority
    enqueued_at: float = field(compare=False)
    user_id: str = field(compare=False)
    coro_fn: Callable = field(compare=False)
    future: asyncio.Future = field(compare=False)


class PriorityLeakyBucket:
    """
    Processes requests at a fixed rate regardless of burst.
    Priority lanes ensure premium users get served first.
    """
    TIER_PRIORITY = {"premium": 0, "pro": 1, "free": 2}
    # Requests per second drained from the bucket
    DRAIN_RATE = 5.0  # 5 RPS global

    def __init__(self):
        self._queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self._running = False

    async def start(self):
        self._running = True
        asyncio.create_task(self._drain_loop())

    async def _drain_loop(self):
        interval = 1.0 / self.DRAIN_RATE
        while self._running:
            try:
                item: QueuedRequest = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                await asyncio.sleep(interval)
                continue

            wait_time = time.monotonic() - item.enqueued_at
            try:
                result = await item.coro_fn()
                item.future.set_result(result)
            except Exception as e:
                item.future.set_exception(e)

            await asyncio.sleep(interval)

    async def submit(self, user_id: str, tier: str, coro_fn: Callable) -> str:
        priority = self.TIER_PRIORITY.get(tier, 2)
        future = asyncio.get_event_loop().create_future()
        item = QueuedRequest(
            priority=priority,
            enqueued_at=time.monotonic(),
            user_id=user_id,
            coro_fn=coro_fn,
            future=future,
        )
        await self._queue.put(item)
        return await asyncio.wait_for(future, timeout=30.0)


bucket = PriorityLeakyBucket()
client = anthropic.AsyncAnthropic()


async def make_request(user_id: str, message: str) -> str:
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": message}],
    )
    return response.content[0].text


async def main():
    await bucket.start()

    # Simulate concurrent users at different tiers
    tasks = [
        bucket.submit("alice",   "premium", lambda: make_request("alice",   "Premium query 1")),
        bucket.submit("bob",     "free",    lambda: make_request("bob",     "Free query 1")),
        bucket.submit("carol",   "pro",     lambda: make_request("carol",   "Pro query 1")),
        bucket.submit("alice",   "premium", lambda: make_request("alice",   "Premium query 2")),
        bucket.submit("david",   "free",    lambda: make_request("david",   "Free query 2")),
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for i, r in enumerate(results):
        print(f"[{i}] {r[:60] if isinstance(r, str) else r}")


asyncio.run(main())
```

**Expected Token Savings**: Smooths burst traffic; prevents one user from monopolizing the drain.
**Environment**: Single async process. For multi-process, replace with Redis-backed queue.

---

### Option 6: Cost Budget Enforcer with Real-Time Spend Tracking

Hard dollar-cap per user per billing period, with warnings at 80% usage.

```python
import asyncio
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
import anthropic

# Anthropic pricing (per million tokens, as of 2025)
PRICING = {
    "claude-haiku-4-5-20251001":  {"input": 0.80,  "output": 4.00},
    "claude-sonnet-4-6":           {"input": 3.00,  "output": 15.00},
    "claude-opus-4-6":             {"input": 15.00, "output": 75.00},
}

TIER_MONTHLY_BUDGETS_USD = {
    "free":    1.00,
    "pro":     20.00,
    "premium": 200.00,
}


def init_db(path: str = "budgets.db") -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS spend (
            user_id TEXT,
            billing_month TEXT,
            spent_usd REAL DEFAULT 0.0,
            PRIMARY KEY (user_id, billing_month)
        )
    """)
    conn.commit()
    return conn


conn = init_db()


def current_month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def get_spend(user_id: str) -> float:
    row = conn.execute(
        "SELECT spent_usd FROM spend WHERE user_id=? AND billing_month=?",
        (user_id, current_month()),
    ).fetchone()
    return row[0] if row else 0.0


def record_spend(user_id: str, cost_usd: float):
    conn.execute(
        """
        INSERT INTO spend (user_id, billing_month, spent_usd) VALUES (?, ?, ?)
        ON CONFLICT(user_id, billing_month) DO UPDATE SET spent_usd = spent_usd + excluded.spent_usd
        """,
        (user_id, current_month(), cost_usd),
    )
    conn.commit()


def calculate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    prices = PRICING.get(model, PRICING["claude-haiku-4-5-20251001"])
    return (input_tokens * prices["input"] + output_tokens * prices["output"]) / 1_000_000


async def budget_aware_chat(
    user_id: str,
    tier: str,
    message: str,
    model: str = "claude-haiku-4-5-20251001",
    max_tokens: int = 1024,
) -> dict:
    budget = TIER_MONTHLY_BUDGETS_USD.get(tier, TIER_MONTHLY_BUDGETS_USD["free"])
    current_spend = get_spend(user_id)
    remaining = budget - current_spend

    if remaining <= 0:
        raise Exception(
            f"Monthly budget exhausted for {user_id}. "
            f"Budget: ${budget:.2f}, Spent: ${current_spend:.4f}"
        )

    # Warn at 80%
    if current_spend / budget >= 0.8:
        print(f"WARNING: {user_id} has used {current_spend/budget*100:.0f}% of monthly budget")

    client = anthropic.AsyncAnthropic()
    response = await client.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": message}],
    )

    cost = calculate_cost(model, response.usage.input_tokens, response.usage.output_tokens)
    record_spend(user_id, cost)

    return {
        "text": response.content[0].text,
        "cost_usd": cost,
        "spend_this_month": current_spend + cost,
        "budget_remaining": remaining - cost,
    }


async def main():
    result = await budget_aware_chat(
        user_id="alice",
        tier="free",
        message="Summarize the French Revolution in 3 sentences.",
    )
    print(f"Reply: {result['text'][:100]}")
    print(f"Cost: ${result['cost_usd']:.6f}")
    print(f"Remaining budget: ${result['budget_remaining']:.4f}")


asyncio.run(main())
```

**Expected Token Savings**: Hard dollar caps prevent surprise bills; 80% warning gives users time to upgrade.
**Environment**: SQLite for budget persistence. Swap to PostgreSQL for multi-instance deployments.

---

| Option | Mechanism | Distributed? | Granularity | Best For |
|--------|-----------|-------------|-------------|----------|
| 1 | Token bucket (in-memory) | No | Request | Single-process apps |
| 2 | Redis sliding window | Yes | Request | Multi-pod deployments |
| 3 | Token-cost quota | No | Token count | Preventing large-request abuse |
| 4 | FastAPI middleware | Depends | Request + headers | HTTP APIs with client SDKs |
| 5 | Priority leaky bucket | No | Throughput | SLA differentiation by tier |
| 6 | Dollar budget enforcer | SQLite | USD spend | Cost control per user/month |
