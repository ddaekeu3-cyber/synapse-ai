---
title: "Agent Doesn't Implement Quota-Based Usage Control Per User"
description: "AI agents allow any user to consume unlimited tokens and API calls, causing one heavy user to exhaust shared rate limits and budget for all other users, or enabling cost runaway attacks."
category: reliability
difficulty: intermediate
tags: [quota, rate-limiting, fairness, multi-tenant, cost-control, redis, asyncio]
---

# Agent Doesn't Implement Quota-Based Usage Control Per User

## Problem

Without per-user quotas, a single user submitting a large batch job or a runaway automated client can exhaust your Anthropic API rate limits, spike your monthly bill, and degrade service for everyone else. Quota control enforces fair resource allocation per user (or tenant), with daily/monthly caps, burst allowances, and graceful quota-exceeded responses instead of API errors cascading to all users.

## Solution 1: In-Memory Token Bucket Quota per User

Implement a token bucket per user with configurable daily limits and burst allowance.

```python
import asyncio
import time
from dataclasses import dataclass, field
from collections import defaultdict

@dataclass
class UserQuota:
    daily_token_limit: int          # e.g., 100_000 tokens/day
    burst_limit: int                 # e.g., 5_000 tokens burst
    tokens_used_today: int = 0
    burst_tokens: int = 0           # replenishes faster
    last_burst_refill: float = field(default_factory=time.monotonic)
    day_start: float = field(default_factory=time.monotonic)

    BURST_REFILL_RATE = 100         # tokens/second for burst bucket
    BURST_REFILL_INTERVAL = 1.0    # refill every second

    def _refill_burst(self):
        now = time.monotonic()
        elapsed = now - self.last_burst_refill
        if elapsed >= self.BURST_REFILL_INTERVAL:
            refill = int(elapsed * self.BURST_REFILL_RATE)
            self.burst_tokens = min(self.burst_limit, self.burst_tokens + refill)
            self.last_burst_refill = now

    def _reset_daily_if_needed(self):
        if time.monotonic() - self.day_start >= 86400:
            self.tokens_used_today = 0
            self.day_start = time.monotonic()

    def check_and_consume(self, tokens: int) -> tuple[bool, str]:
        """Returns (allowed, reason). Atomically check and debit if allowed."""
        self._reset_daily_if_needed()
        self._refill_burst()

        # Check daily limit
        if self.tokens_used_today + tokens > self.daily_token_limit:
            remaining = self.daily_token_limit - self.tokens_used_today
            return False, f"Daily quota exceeded. Remaining: {remaining} tokens"

        # Check burst bucket
        if tokens > self.burst_tokens:
            return False, f"Burst quota exceeded. Available burst: {self.burst_tokens} tokens"

        # Debit both buckets
        self.tokens_used_today += tokens
        self.burst_tokens -= tokens
        return True, "ok"

    def status(self) -> dict:
        self._reset_daily_if_needed()
        self._refill_burst()
        return {
            "daily_used": self.tokens_used_today,
            "daily_limit": self.daily_token_limit,
            "daily_remaining": self.daily_token_limit - self.tokens_used_today,
            "burst_available": self.burst_tokens,
        }

class UserQuotaManager:
    def __init__(self):
        self._quotas: dict[str, UserQuota] = {}
        self._lock = asyncio.Lock()
        self._PLANS = {
            "free": {"daily": 10_000, "burst": 1_000},
            "pro": {"daily": 100_000, "burst": 10_000},
            "enterprise": {"daily": 1_000_000, "burst": 50_000},
        }

    async def get_or_create(self, user_id: str, plan: str = "free") -> UserQuota:
        async with self._lock:
            if user_id not in self._quotas:
                limits = self._PLANS.get(plan, self._PLANS["free"])
                self._quotas[user_id] = UserQuota(
                    daily_token_limit=limits["daily"],
                    burst_limit=limits["burst"],
                    burst_tokens=limits["burst"],
                )
            return self._quotas[user_id]

    async def check(self, user_id: str, tokens: int, plan: str = "free") -> tuple[bool, str]:
        async with self._lock:
            quota = await self.get_or_create(user_id, plan)
            return quota.check_and_consume(tokens)

    async def status(self, user_id: str) -> dict:
        async with self._lock:
            quota = self._quotas.get(user_id)
            if not quota:
                return {"error": "user_not_found"}
            return quota.status()

quota_manager = UserQuotaManager()

async def guarded_agent_call(user_id: str, prompt: str, plan: str = "free") -> str:
    estimated_tokens = len(prompt.split()) * 3  # rough estimate
    allowed, reason = await quota_manager.check(user_id, estimated_tokens, plan)
    if not allowed:
        raise PermissionError(f"Quota exceeded for user {user_id}: {reason}")

    from anthropic import AsyncAnthropic
    resp = await AsyncAnthropic().messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text
```

**When to use**: Single-instance agents. For multi-instance, use Solution 2 (Redis-backed).

---

## Solution 2: Redis-Backed Sliding Window Quota (Distributed)

Use Redis sorted sets for exact sliding window quota tracking across multiple agent instances.

```python
import asyncio
import time
import redis.asyncio as aioredis

redis = aioredis.from_url("redis://localhost:6379")

QUOTA_PLANS = {
    "free":       {"tokens_per_hour": 10_000, "requests_per_min": 10},
    "pro":        {"tokens_per_hour": 100_000, "requests_per_min": 60},
    "enterprise": {"tokens_per_hour": 1_000_000, "requests_per_min": 600},
}

async def sliding_window_check(
    user_id: str,
    tokens: int,
    plan: str = "free",
) -> tuple[bool, dict]:
    """Sliding window rate check using Redis sorted sets."""
    now = time.time()
    limits = QUOTA_PLANS.get(plan, QUOTA_PLANS["free"])

    token_key = f"quota:tokens:{user_id}"
    req_key   = f"quota:requests:{user_id}"

    pipe = redis.pipeline(transaction=True)
    hour_ago = now - 3600
    min_ago  = now - 60

    # Remove old entries from both windows
    pipe.zremrangebyscore(token_key, 0, hour_ago)
    pipe.zremrangebyscore(req_key,   0, min_ago)

    # Get current usage in windows
    pipe.zrange(token_key, 0, -1, withscores=True)
    pipe.zcard(req_key)
    results = await pipe.execute()

    token_entries = results[2]  # list of (member, score) — score = timestamp
    current_requests = results[3]

    # Sum token usage in the window
    # We store token count as part of the member key: "timestamp:tokens"
    current_tokens = sum(
        int(member.decode().split(":")[1])
        for member, _ in token_entries
    )

    # Check limits
    if current_tokens + tokens > limits["tokens_per_hour"]:
        return False, {
            "reason": "hourly_token_limit_exceeded",
            "used": current_tokens,
            "limit": limits["tokens_per_hour"],
            "retry_after_s": 3600,
        }
    if current_requests >= limits["requests_per_min"]:
        return False, {
            "reason": "per_minute_request_limit_exceeded",
            "used": current_requests,
            "limit": limits["requests_per_min"],
            "retry_after_s": 60,
        }

    # Record this request
    ts_key = f"{now:.6f}:{tokens}"
    pipe2 = redis.pipeline()
    pipe2.zadd(token_key, {ts_key: now})
    pipe2.zadd(req_key, {str(now): now})
    pipe2.expire(token_key, 3700)  # slightly longer than window
    pipe2.expire(req_key, 70)
    await pipe2.execute()

    return True, {"used_tokens": current_tokens + tokens, "used_requests": current_requests + 1}

# FastAPI integration
from fastapi import FastAPI, Request, HTTPException, Header
app = FastAPI()

@app.post("/agent/chat")
async def chat(request: Request, x_user_id: str = Header(...), x_plan: str = Header("free")):
    body = await request.json()
    prompt = body.get("prompt", "")

    tokens = len(prompt.split()) * 3
    allowed, info = await sliding_window_check(x_user_id, tokens, x_plan)
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail=info,
            headers={"Retry-After": str(info.get("retry_after_s", 60))},
        )

    return {"response": "...", "quota": info}
```

**When to use**: Multi-instance deployments. Redis sorted sets give exact sliding window semantics at microsecond precision.

---

## Solution 3: Hierarchical Quota — Tenant → Team → User

Enforce quotas at multiple levels: tenant can't exceed its org limit; team can't exceed its slice; user can't exceed their share.

```python
import asyncio
from dataclasses import dataclass, field
from typing import Optional

@dataclass
class QuotaNode:
    name: str
    daily_token_limit: int
    tokens_used: int = 0
    children: list["QuotaNode"] = field(default_factory=list)
    parent: Optional["QuotaNode"] = field(default=None, repr=False)

    def available(self) -> int:
        self_available = self.daily_token_limit - self.tokens_used
        if self.parent:
            return min(self_available, self.parent.available())
        return self_available

    def consume(self, tokens: int) -> bool:
        """Consume tokens up the tree. All ancestors must have capacity."""
        if self.available() < tokens:
            return False
        self.tokens_used += tokens
        if self.parent:
            self.parent.tokens_used += tokens
        return True

    def reset(self):
        self.tokens_used = 0
        for child in self.children:
            child.reset()

class HierarchicalQuotaManager:
    def __init__(self):
        self._tenants: dict[str, QuotaNode] = {}
        self._teams: dict[str, QuotaNode] = {}
        self._users: dict[str, QuotaNode] = {}
        self._lock = asyncio.Lock()

    def setup_tenant(self, tenant_id: str, daily_limit: int):
        node = QuotaNode(name=tenant_id, daily_token_limit=daily_limit)
        self._tenants[tenant_id] = node

    def setup_team(self, tenant_id: str, team_id: str, daily_limit: int):
        tenant = self._tenants[tenant_id]
        team = QuotaNode(name=team_id, daily_token_limit=daily_limit, parent=tenant)
        tenant.children.append(team)
        self._teams[team_id] = team

    def setup_user(self, team_id: str, user_id: str, daily_limit: int):
        team = self._teams[team_id]
        user = QuotaNode(name=user_id, daily_token_limit=daily_limit, parent=team)
        team.children.append(user)
        self._users[user_id] = user

    async def check_and_consume(self, user_id: str, tokens: int) -> tuple[bool, str]:
        async with self._lock:
            user = self._users.get(user_id)
            if not user:
                return False, f"Unknown user: {user_id}"
            if user.consume(tokens):
                return True, "ok"
            # Diagnose which level blocked
            if user.available() < tokens:
                return False, f"User quota exceeded. Available: {user.daily_token_limit - user.tokens_used}"
            if user.parent and user.parent.available() < tokens:
                return False, f"Team quota exceeded. Team available: {user.parent.available()}"
            return False, "Tenant quota exceeded"

# Setup
mgr = HierarchicalQuotaManager()
mgr.setup_tenant("acme", daily_limit=1_000_000)
mgr.setup_team("acme", "eng-team", daily_limit=500_000)
mgr.setup_user("eng-team", "alice", daily_limit=100_000)
mgr.setup_user("eng-team", "bob", daily_limit=50_000)
```

**When to use**: Enterprise SaaS agents where tenant, team, and user budget allocation must be enforced independently.

---

## Solution 4: Quota with Soft and Hard Limits + Warning Notifications

Implement soft (warn) and hard (block) quota limits so users get advance notice before hitting the wall.

```python
import asyncio
import logging
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

@dataclass
class TieredQuota:
    user_id: str
    hard_limit: int             # block at this usage
    soft_limit: int             # warn at this usage (e.g., 80% of hard)
    window_seconds: float = 86400.0
    _used: int = 0
    _window_start: float = 0.0
    _soft_warned: bool = False

    def __post_init__(self):
        self._window_start = time.monotonic()

    def _reset_if_expired(self):
        if time.monotonic() - self._window_start >= self.window_seconds:
            self._used = 0
            self._soft_warned = False
            self._window_start = time.monotonic()

    def check_and_consume(self, tokens: int) -> tuple[str, dict]:
        """Returns ('ok' | 'warn' | 'block', info_dict)."""
        self._reset_if_expired()

        new_total = self._used + tokens
        pct = new_total / self.hard_limit

        if new_total > self.hard_limit:
            return "block", {
                "reason": "hard_limit_exceeded",
                "used": self._used,
                "hard_limit": self.hard_limit,
                "retry_after_s": int(self.window_seconds - (time.monotonic() - self._window_start)),
            }

        # Consume
        self._used = new_total

        if pct >= (self.soft_limit / self.hard_limit) and not self._soft_warned:
            self._soft_warned = True
            return "warn", {
                "reason": "approaching_limit",
                "used": self._used,
                "hard_limit": self.hard_limit,
                "percent_used": round(pct * 100, 1),
            }

        return "ok", {"used": self._used, "percent_used": round(pct * 100, 1)}

async def handle_with_tiered_quota(
    quota: TieredQuota,
    tokens: int,
    notify_fn,
) -> str:
    status, info = quota.check_and_consume(tokens)

    if status == "block":
        logger.warning("user_quota_blocked", extra={"user": quota.user_id, **info})
        raise PermissionError(f"Daily quota exceeded. {info}")

    if status == "warn":
        logger.info("user_quota_warning", extra={"user": quota.user_id, **info})
        asyncio.create_task(notify_fn(quota.user_id, info))

    return status

async def notify_user(user_id: str, info: dict):
    """Send email/Slack notification when user approaches quota."""
    logger.info("quota_warning_sent", extra={"user_id": user_id, "pct": info.get("percent_used")})
    # Hook into your notification system here
```

**When to use**: Consumer-facing agents where a good UX requires warning users before they're hard-blocked.

---

## Solution 5: Priority-Based Quota Sharing Under Pressure

When total demand exceeds capacity, allocate remaining quota preferentially to higher-priority users.

```python
import asyncio
import heapq
from dataclasses import dataclass, field
from typing import Any

@dataclass(order=True)
class QuotaRequest:
    priority: int          # lower = higher priority (1 = critical, 5 = bulk)
    sequence: int          # FIFO within same priority
    user_id: str = field(compare=False)
    tokens: int = field(compare=False)
    future: asyncio.Future = field(compare=False, default=None)

class PriorityQuotaGate:
    """Under pressure, high-priority users get quota first."""

    def __init__(self, total_tokens_per_second: int = 10_000):
        self._rate = total_tokens_per_second
        self._available = total_tokens_per_second
        self._heap: list[QuotaRequest] = []
        self._seq = 0
        self._lock = asyncio.Lock()
        self._refill_task: asyncio.Task | None = None

    async def start(self):
        self._refill_task = asyncio.create_task(self._refill_loop())

    async def _refill_loop(self):
        while True:
            await asyncio.sleep(1.0)
            async with self._lock:
                self._available = min(
                    self._rate,
                    self._available + self._rate,
                )
                await self._try_drain()

    async def _try_drain(self):
        """Grant queued requests in priority order if capacity allows."""
        while self._heap and self._available > 0:
            req = self._heap[0]
            if req.tokens <= self._available:
                heapq.heappop(self._heap)
                self._available -= req.tokens
                if not req.future.done():
                    req.future.set_result(True)
            else:
                break  # head of queue can't be served yet

    async def request(self, user_id: str, tokens: int, priority: int = 3) -> bool:
        """Block until quota is granted. Priority 1 = critical, 5 = bulk."""
        async with self._lock:
            if tokens <= self._available:
                self._available -= tokens
                return True  # immediate grant

            # Queue the request
            loop = asyncio.get_event_loop()
            fut = loop.create_future()
            self._seq += 1
            heapq.heappush(self._heap, QuotaRequest(
                priority=priority,
                sequence=self._seq,
                user_id=user_id,
                tokens=tokens,
                future=fut,
            ))

        return await asyncio.wait_for(fut, timeout=60.0)

gate = PriorityQuotaGate(total_tokens_per_second=50_000)

async def priority_guarded_call(user_id: str, tokens: int, priority: int = 3):
    granted = await gate.request(user_id, tokens, priority)
    if not granted:
        raise PermissionError("Quota request timed out")
    # Proceed with API call
```

**When to use**: Agents with tiered SLAs (premium users get quota before free users during bursts).

---

## Solution 6: Quota Dashboard API — Real-Time Usage Visibility

Expose a REST endpoint for users (and admins) to check their current quota status in real time.

```python
import asyncio
import time
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

app = FastAPI()

class QuotaStatus(BaseModel):
    user_id: str
    plan: str
    daily_tokens_used: int
    daily_tokens_limit: int
    daily_tokens_remaining: int
    hourly_tokens_used: int
    hourly_tokens_limit: int
    requests_per_minute_used: int
    requests_per_minute_limit: int
    reset_in_seconds: int
    over_limit: bool
    warning: bool

# In-memory store (replace with Redis in production)
_usage_store: dict[str, dict] = {}

def get_usage(user_id: str, plan: str = "free") -> dict:
    now = time.time()
    if user_id not in _usage_store:
        _usage_store[user_id] = {
            "plan": plan,
            "daily_tokens": 0,
            "hourly_tokens": 0,
            "requests_this_minute": 0,
            "day_start": now,
            "hour_start": now,
            "minute_start": now,
        }
    u = _usage_store[user_id]

    # Reset windows
    if now - u["day_start"] >= 86400:
        u["daily_tokens"] = 0
        u["day_start"] = now
    if now - u["hour_start"] >= 3600:
        u["hourly_tokens"] = 0
        u["hour_start"] = now
    if now - u["minute_start"] >= 60:
        u["requests_this_minute"] = 0
        u["minute_start"] = now

    return u

PLAN_LIMITS = {
    "free":       {"daily": 10_000, "hourly": 2_000, "rpm": 10},
    "pro":        {"daily": 100_000, "hourly": 20_000, "rpm": 60},
    "enterprise": {"daily": 1_000_000, "hourly": 200_000, "rpm": 600},
}

@app.get("/quota/status", response_model=QuotaStatus)
async def quota_status(x_user_id: str = Header(...), x_plan: str = Header("free")):
    if not x_user_id:
        raise HTTPException(status_code=401, detail="Missing user ID")

    usage = get_usage(x_user_id, x_plan)
    limits = PLAN_LIMITS.get(x_plan, PLAN_LIMITS["free"])
    now = time.time()
    reset_in = int(86400 - (now - usage["day_start"]))

    daily_remaining = max(0, limits["daily"] - usage["daily_tokens"])
    over_limit = usage["daily_tokens"] >= limits["daily"]
    warning = usage["daily_tokens"] >= limits["daily"] * 0.8

    return QuotaStatus(
        user_id=x_user_id,
        plan=x_plan,
        daily_tokens_used=usage["daily_tokens"],
        daily_tokens_limit=limits["daily"],
        daily_tokens_remaining=daily_remaining,
        hourly_tokens_used=usage["hourly_tokens"],
        hourly_tokens_limit=limits["hourly"],
        requests_per_minute_used=usage["requests_this_minute"],
        requests_per_minute_limit=limits["rpm"],
        reset_in_seconds=reset_in,
        over_limit=over_limit,
        warning=warning,
    )

@app.get("/admin/quota/{user_id}")
async def admin_quota(user_id: str, x_admin_key: str = Header(...)):
    if x_admin_key != "admin-secret":  # replace with real auth
        raise HTTPException(status_code=403)
    return get_usage(user_id)
```

**When to use**: Public-facing agents. Users should be able to see their quota status to avoid surprises.

---

## Comparison

| Solution | Distribution | Precision | Hierarchy | Priority | Visibility | Best For |
|---|---|---|---|---|---|---|
| In-memory token bucket | Single instance | Approximate | No | No | No | Simple single-process agents |
| Redis sliding window | Multi-instance | Exact | No | No | No | Distributed agents |
| Hierarchical quota | Single/Multi | Exact | Yes | No | No | Enterprise tenant/team/user |
| Soft + hard limits | Single | Exact | No | No | Warnings | Consumer UX |
| Priority quota gate | Single | Exact | No | Yes | No | Tiered SLA agents |
| Quota dashboard API | Any | Per-request | No | No | Yes | Self-service quota visibility |

**Rule of thumb**: Use Redis sliding window for distributed correctness. Add soft limits at 80% of hard limit. Expose a `/quota/status` endpoint from day one — users will ask.
