---
layout: solution
title: "Agent Doesn't Implement Rate Limiting Per User"
category: concurrency
description: "Agent applies a single global rate limit across all users, allowing one abusive user to exhaust the API budget for everyone, or applies no per-user limit at all."
tags: [concurrency, rate-limiting, fairness, cost, abuse-prevention]
---

## Symptom

One user sends 500 requests in a minute and the agent burns through the entire API budget, throttling or billing-shocking all other users:

```
[10:01] user_id=attacker: 142 requests (91% of capacity)
[10:01] user_id=user_a:   3 requests (queued, waiting)
[10:01] user_id=user_b:   1 request  (queued, waiting)
[10:01] global 429 from Anthropic — all users affected
```

Or the agent has no rate limiting at all, making it trivially abusable.

## Root Cause

The global semaphore or rate limiter is shared across all users:

```python
import asyncio
import anthropic

client = anthropic.AsyncAnthropic(api_key="sk-live-...")

# One semaphore shared by everyone — unfair and exploitable
_global_sem = asyncio.Semaphore(10)

async def handle_request(user_id: str, prompt: str) -> str:
    async with _global_sem:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=[{"role": "user", "content": prompt}]
        )
        return response.content[0].text
```

A single user who fires 10 concurrent requests monopolises the semaphore.

---

## Fix

### Option 1 — Per-user token bucket with `asyncio.Semaphore`

Create a separate semaphore per user. Cap each user's concurrency independently.

```python
import asyncio
import anthropic
from collections import defaultdict

client = anthropic.AsyncAnthropic(api_key="sk-live-...")

PER_USER_CONCURRENCY = 3   # max simultaneous requests per user
GLOBAL_CONCURRENCY   = 20  # total across all users

_user_semaphores: dict[str, asyncio.Semaphore] = defaultdict(
    lambda: asyncio.Semaphore(PER_USER_CONCURRENCY)
)
_global_sem = asyncio.Semaphore(GLOBAL_CONCURRENCY)


async def handle_request(user_id: str, prompt: str) -> str:
    user_sem = _user_semaphores[user_id]

    # Per-user gate first, then global gate
    async with user_sem:
        async with _global_sem:
            response = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=256,
                messages=[{"role": "user", "content": prompt}]
            )
            return response.content[0].text


async def main():
    # Attacker fires 10 concurrent requests — only 3 get through at once
    attacker_tasks = [handle_request("attacker", f"Prompt {i}") for i in range(10)]
    # Legitimate user gets fair share of global slots
    legit_task = handle_request("user_a", "What is 2+2?")

    results = await asyncio.gather(*attacker_tasks, legit_task, return_exceptions=True)
    print(f"Completed: {sum(1 for r in results if not isinstance(r, Exception))}")

asyncio.run(main())

# Expected Token Savings: prevents one user from blocking others; budget shared fairly
# Environment: multi-user async agents on shared infrastructure
```

---

### Option 2 — Sliding window rate limiter per user

Enforce a request-per-minute limit per user using a sliding time window.

```python
import asyncio
import time
import anthropic
from collections import defaultdict, deque

client = anthropic.AsyncAnthropic(api_key="sk-live-...")

REQUESTS_PER_MINUTE = 20    # per user
WINDOW_SECONDS      = 60

# Per-user request timestamps
_user_request_times: dict[str, deque] = defaultdict(deque)
_lock = asyncio.Lock()


async def check_rate_limit(user_id: str) -> float:
    """Returns 0 if allowed, or seconds to wait if rate limited."""
    now = time.monotonic()
    async with _lock:
        dq = _user_request_times[user_id]
        # Evict timestamps outside the window
        while dq and now - dq[0] > WINDOW_SECONDS:
            dq.popleft()

        if len(dq) >= REQUESTS_PER_MINUTE:
            # How long until the oldest request falls out of the window
            wait = WINDOW_SECONDS - (now - dq[0])
            return max(0.0, wait)

        dq.append(now)
        return 0.0


async def handle_request(user_id: str, prompt: str) -> str:
    wait = await check_rate_limit(user_id)
    if wait > 0:
        raise PermissionError(f"Rate limited: retry in {wait:.1f}s")

    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}]
    )
    return response.content[0].text


async def main():
    # 25 rapid requests from one user — first 20 succeed, rest are rate limited
    tasks = [handle_request("heavy_user", f"Request {i}") for i in range(25)]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    ok    = sum(1 for r in results if isinstance(r, str))
    limit = sum(1 for r in results if isinstance(r, PermissionError))
    print(f"Allowed: {ok}, Rate-limited: {limit}")

asyncio.run(main())

# Expected Token Savings: hard cap at 20 req/min per user prevents runaway spend
# Environment: API endpoints with multiple users and predictable usage patterns
```

---

### Option 3 — Token-cost-aware rate limiting

Rate limit not by request count but by estimated token cost per user. A user who asks short questions gets more requests; a user generating long documents gets fewer.

```python
import asyncio
import time
import anthropic
from collections import defaultdict, deque
from dataclasses import dataclass

client = anthropic.AsyncAnthropic(api_key="sk-live-...")

TOKEN_BUDGET_PER_USER_PER_MINUTE = 50_000  # tokens
WINDOW_SECONDS = 60


@dataclass
class TokenUsageEntry:
    timestamp: float
    tokens: int


_user_usage: dict[str, deque] = defaultdict(deque)
_lock = asyncio.Lock()


async def consume_budget(user_id: str, tokens_requested: int) -> bool:
    """Returns True if within budget, False if rate limited."""
    now = time.monotonic()
    async with _lock:
        dq = _user_usage[user_id]
        # Evict old entries
        while dq and now - dq[0].timestamp > WINDOW_SECONDS:
            dq.popleft()

        used = sum(e.tokens for e in dq)
        if used + tokens_requested > TOKEN_BUDGET_PER_USER_PER_MINUTE:
            return False  # Rate limited

        dq.append(TokenUsageEntry(timestamp=now, tokens=tokens_requested))
        return True


async def handle_request(user_id: str, prompt: str, max_tokens: int = 512) -> str:
    # Estimate input tokens (rough: 1 token ≈ 4 chars)
    estimated_tokens = len(prompt) // 4 + max_tokens

    allowed = await consume_budget(user_id, estimated_tokens)
    if not allowed:
        raise PermissionError(f"Token budget exceeded for user {user_id}")

    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}]
    )

    # Update with actual usage
    actual = response.usage.input_tokens + response.usage.output_tokens
    async with _lock:
        dq = _user_usage[user_id]
        # Adjust: remove estimate, add actual
        if dq:
            last = dq[-1]
            dq[-1] = TokenUsageEntry(last.timestamp, actual)

    return response.content[0].text


async def main():
    # User requesting many large outputs hits budget faster
    tasks = [handle_request("big_user", "Write a long essay on " + f"topic {i}", max_tokens=1000)
             for i in range(20)]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    ok = sum(1 for r in results if isinstance(r, str))
    limited = sum(1 for r in results if isinstance(r, PermissionError))
    print(f"Allowed: {ok}, Budget-limited: {limited}")

asyncio.run(main())

# Expected Token Savings: heavy users get fewer large requests; light users unaffected
# Environment: mixed-use agents where request size varies dramatically across users
```

---

### Option 4 — Redis-backed per-user rate limiter (multi-process)

For horizontally scaled deployments, use Redis atomic increment + TTL for cross-process per-user rate limiting.

```python
import asyncio
import anthropic
import redis.asyncio as redis

client = anthropic.AsyncAnthropic(api_key="sk-live-...")
redis_client = redis.Redis(host="localhost", port=6379, db=0)

REQUESTS_PER_MINUTE = 20
WINDOW_SECONDS = 60


async def check_rate_limit_redis(user_id: str) -> bool:
    """Returns True if request is allowed."""
    key = f"rate_limit:{user_id}"

    # Atomic increment + set expiry on first request
    count = await redis_client.incr(key)
    if count == 1:
        await redis_client.expire(key, WINDOW_SECONDS)

    return count <= REQUESTS_PER_MINUTE


async def remaining_quota(user_id: str) -> int:
    key = f"rate_limit:{user_id}"
    count = int(await redis_client.get(key) or 0)
    return max(0, REQUESTS_PER_MINUTE - count)


async def handle_request(user_id: str, prompt: str) -> str:
    allowed = await check_rate_limit_redis(user_id)
    if not allowed:
        quota = await remaining_quota(user_id)
        raise PermissionError(f"Rate limit exceeded. Quota: {quota}/{REQUESTS_PER_MINUTE} req/min")

    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}]
    )
    return response.content[0].text


async def main():
    tasks = [handle_request("user_123", f"Question {i}") for i in range(25)]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    ok      = sum(1 for r in results if isinstance(r, str))
    limited = sum(1 for r in results if isinstance(r, PermissionError))
    print(f"Allowed: {ok}, Rate-limited: {limited}")

asyncio.run(main())

# Expected Token Savings: shared Redis counter prevents any user from saturating
#   the global API budget regardless of how many service instances are running
# Environment: horizontally scaled multi-process agents; requires Redis
```

---

### Option 5 — Tiered rate limits by user plan

Apply different rate limits based on the user's subscription tier. Free tier users get strict limits; paid tier users get higher allowances.

```python
import asyncio
import time
import anthropic
from collections import defaultdict, deque
from enum import StrEnum

client = anthropic.AsyncAnthropic(api_key="sk-live-...")

WINDOW_SECONDS = 60


class Tier(StrEnum):
    FREE = "free"
    PRO  = "pro"
    ENT  = "enterprise"


TIER_LIMITS = {
    Tier.FREE: {"requests": 5,   "max_tokens": 256},
    Tier.PRO:  {"requests": 60,  "max_tokens": 2048},
    Tier.ENT:  {"requests": 500, "max_tokens": 8192},
}

_user_times: dict[str, deque] = defaultdict(deque)
_lock = asyncio.Lock()


async def is_allowed(user_id: str, tier: Tier) -> bool:
    now = time.monotonic()
    limit = TIER_LIMITS[tier]["requests"]
    async with _lock:
        dq = _user_times[user_id]
        while dq and now - dq[0] > WINDOW_SECONDS:
            dq.popleft()
        if len(dq) >= limit:
            return False
        dq.append(now)
        return True


async def handle_request(user_id: str, tier: Tier, prompt: str) -> str:
    if not await is_allowed(user_id, tier):
        limit = TIER_LIMITS[tier]["requests"]
        raise PermissionError(
            f"Rate limit exceeded for {tier} tier ({limit} req/min). Upgrade to increase limit."
        )

    max_tokens = TIER_LIMITS[tier]["max_tokens"]
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}]
    )
    return response.content[0].text


async def main():
    # Free user hits limit quickly
    free_tasks = [handle_request(f"free_user", Tier.FREE, f"Q{i}") for i in range(10)]
    results = await asyncio.gather(*free_tasks, return_exceptions=True)
    ok = sum(1 for r in results if isinstance(r, str))
    limited = sum(1 for r in results if isinstance(r, PermissionError))
    print(f"Free tier — Allowed: {ok}, Limited: {limited}")

    # Pro user gets 60/min
    pro_result = await handle_request("pro_user", Tier.PRO, "Detailed analysis please")
    print(f"Pro tier result: {pro_result[:60]}")

asyncio.run(main())

# Expected Token Savings: free-tier users can't burn paid-tier budgets; plan upsell path clear
# Environment: SaaS agents with tiered subscription plans
```

---

### Option 6 — Rate limit with graceful queuing instead of immediate rejection

Instead of immediately rejecting rate-limited requests, queue them and serve them when the user's window resets.

```python
import asyncio
import time
import anthropic
from collections import defaultdict, deque
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic(api_key="sk-live-...")

REQUESTS_PER_MINUTE = 10
WINDOW_SECONDS = 60
MAX_QUEUE_SIZE = 5   # max queued requests per user before hard rejection


@dataclass
class UserState:
    request_times: deque = field(default_factory=deque)
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=MAX_QUEUE_SIZE))


_users: dict[str, UserState] = defaultdict(UserState)
_lock = asyncio.Lock()


async def wait_for_slot(user_id: str) -> None:
    """Block until the user has a request slot available."""
    while True:
        now = time.monotonic()
        async with _lock:
            state = _users[user_id]
            dq = state.request_times
            while dq and now - dq[0] > WINDOW_SECONDS:
                dq.popleft()

            if len(dq) < REQUESTS_PER_MINUTE:
                dq.append(now)
                return  # Slot acquired

            # Wait until oldest request falls out of window
            wait_time = WINDOW_SECONDS - (now - dq[0]) + 0.1

        await asyncio.sleep(wait_time)


async def handle_request(user_id: str, prompt: str, timeout: float = 30.0) -> str:
    state = _users[user_id]

    # Hard reject if the queue is full
    if state.queue.full():
        raise PermissionError(f"Queue full for user {user_id}. Try again later.")

    try:
        await asyncio.wait_for(wait_for_slot(user_id), timeout=timeout)
    except asyncio.TimeoutError:
        raise PermissionError(f"Rate limit queue timeout after {timeout}s")

    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}]
    )
    return response.content[0].text


async def main():
    # 15 requests from one user — first 10 get immediate slots, next 5 wait, rest rejected
    tasks = [handle_request("user_a", f"Request {i}", timeout=5.0) for i in range(15)]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    ok      = sum(1 for r in results if isinstance(r, str))
    limited = sum(1 for r in results if isinstance(r, PermissionError))
    print(f"Served: {ok}, Rejected: {limited}")

asyncio.run(main())

# Expected Token Savings: graceful queuing improves UX vs hard 429; no wasted API calls
# Environment: interactive agents where users prefer waiting over immediate rejection
```

---

## Comparison

| Option | Per-User Isolation | Multi-Process | Queue Support | Tier-Aware | Cost-Aware |
|--------|-------------------|---------------|---------------|------------|------------|
| 1 | Semaphore | No | No | No | No |
| 2 | Sliding window | No | No | No | No |
| 3 | Token budget | No | No | No | Yes |
| 4 | Redis atomic | Yes | No | No | No |
| 5 | Tiered limits | No | No | Yes | No |
| 6 | Queue + window | No | Yes | No | No |

**Recommended starting point:** Option 2 (sliding window per user) for most agents — simple, zero dependencies, prevents abuse. Add Option 4 (Redis) when running multiple service instances. Add Option 5 (tiered) when monetising with subscription plans.
