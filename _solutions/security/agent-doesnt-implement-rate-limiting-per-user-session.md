---
title: "Agent Doesn't Implement Rate Limiting Per User Session"
description: "Six solutions for enforcing per-session, per-user, and per-IP rate limits to prevent abuse, protect costs, and ensure fair resource sharing across agent users."
difficulty: intermediate
category: security
tags: [rate-limiting, security, abuse-prevention, token-bucket, session, fairness]
---

# Agent Doesn't Implement Rate Limiting Per User Session

Without per-session rate limits, a single user can monopolize all capacity, drain budgets with automated abuse, or accidentally trigger runaway loops. Rate limiting enforces fairness, cost control, and basic abuse prevention. These six solutions range from simple per-session counters to sophisticated adaptive and distributed rate limiters.

## Solution 1: Token Bucket Rate Limiter Per Session

Classic token bucket: each session gets a bucket of tokens that refills at a fixed rate. Burst is allowed up to bucket capacity.

```python
import asyncio
import time
from dataclasses import dataclass, field
from anthropic import AsyncAnthropic


@dataclass
class TokenBucket:
    """Token bucket for rate limiting: allows burst up to capacity, refills at rate/second."""
    capacity: float          # Max tokens (burst limit)
    refill_rate: float       # Tokens added per second
    tokens: float = field(init=False)
    last_refill: float = field(default_factory=time.time, init=False)

    def __post_init__(self):
        self.tokens = self.capacity  # Start full

    def _refill(self):
        now = time.time()
        elapsed = now - self.last_refill
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
        self.last_refill = now

    def consume(self, tokens: float = 1.0) -> bool:
        """Returns True if consumed; False if rate limit exceeded."""
        self._refill()
        if self.tokens >= tokens:
            self.tokens -= tokens
            return True
        return False

    @property
    def wait_time(self) -> float:
        """Seconds until next token is available."""
        self._refill()
        deficit = 1.0 - self.tokens
        if deficit <= 0:
            return 0.0
        return deficit / self.refill_rate


class RateLimitExceededError(Exception):
    def __init__(self, session_id: str, wait_seconds: float):
        super().__init__(
            f"Rate limit exceeded for session '{session_id}'. "
            f"Retry in {wait_seconds:.1f}s."
        )
        self.session_id = session_id
        self.wait_seconds = wait_seconds


class PerSessionRateLimiter:
    def __init__(
        self,
        requests_per_minute: float = 10.0,
        burst_capacity: float = 5.0,
    ):
        self._sessions: dict[str, TokenBucket] = {}
        self._rpm = requests_per_minute
        self._burst = burst_capacity

    def _get_bucket(self, session_id: str) -> TokenBucket:
        if session_id not in self._sessions:
            self._sessions[session_id] = TokenBucket(
                capacity=self._burst,
                refill_rate=self._rpm / 60.0,
            )
        return self._sessions[session_id]

    def check(self, session_id: str, cost: float = 1.0) -> None:
        """Raises RateLimitExceededError if rate limit hit."""
        bucket = self._get_bucket(session_id)
        if not bucket.consume(cost):
            raise RateLimitExceededError(session_id, bucket.wait_time)

    def remaining_tokens(self, session_id: str) -> float:
        return self._get_bucket(session_id).tokens

    def evict_inactive(self, idle_seconds: float = 3600.0):
        """Remove buckets for sessions inactive for idle_seconds."""
        cutoff = time.time() - idle_seconds
        stale = [sid for sid, b in self._sessions.items() if b.last_refill < cutoff]
        for sid in stale:
            del self._sessions[sid]


_RATE_LIMITER = PerSessionRateLimiter(requests_per_minute=20.0, burst_capacity=5.0)


class RateLimitedAgent:
    def __init__(self, limiter: PerSessionRateLimiter = _RATE_LIMITER):
        self.client = AsyncAnthropic()
        self.limiter = limiter

    async def chat(self, message: str, session_id: str) -> str:
        self.limiter.check(session_id)  # Raises if over limit

        response = await self.client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            messages=[{"role": "user", "content": message}],
        )
        return response.content[0].text

    async def safe_chat(self, message: str, session_id: str) -> dict:
        """Returns result or rate limit info without raising."""
        try:
            text = await self.chat(message, session_id)
            return {
                "success": True,
                "response": text,
                "remaining_tokens": self.limiter.remaining_tokens(session_id),
            }
        except RateLimitExceededError as e:
            return {
                "success": False,
                "error": "rate_limited",
                "retry_after_seconds": e.wait_seconds,
            }


async def demo_token_bucket():
    agent = RateLimitedAgent(PerSessionRateLimiter(requests_per_minute=6.0, burst_capacity=3.0))
    session = "user_abc"

    # First 3 succeed (burst), rest are rate limited
    for i in range(6):
        result = await agent.safe_chat(f"Question {i}", session)
        if result["success"]:
            print(f"  Request {i}: OK (tokens_left={result['remaining_tokens']:.1f})")
        else:
            print(f"  Request {i}: RATE LIMITED (retry_in={result['retry_after_seconds']:.1f}s)")
```

## Solution 2: Multi-Dimensional Rate Limiting (Requests + Tokens + Cost)

Enforce separate limits on request count, token consumption, and dollar cost — each checked independently.

```python
import asyncio
import time
from dataclasses import dataclass, field
from anthropic import AsyncAnthropic

PRICING = {
    "claude-haiku-4-5-20251001": {"input": 0.80e-6, "output": 4.00e-6},
}


@dataclass
class MultiDimBucket:
    """Tracks requests, tokens, and cost in a rolling time window."""
    window_seconds: float = 60.0
    max_requests: int = 10
    max_tokens: int = 50_000
    max_cost_usd: float = 0.10

    _events: list[dict] = field(default_factory=list)

    def _prune(self):
        cutoff = time.time() - self.window_seconds
        self._events = [e for e in self._events if e["ts"] >= cutoff]

    def _totals(self) -> tuple[int, int, float]:
        self._prune()
        reqs = len(self._events)
        toks = sum(e["tokens"] for e in self._events)
        cost = sum(e["cost"] for e in self._events)
        return reqs, toks, cost

    def check(self, estimated_tokens: int = 1000, estimated_cost: float = 0.001) -> tuple[bool, str]:
        reqs, toks, cost = self._totals()
        if reqs >= self.max_requests:
            return False, f"request limit ({reqs}/{self.max_requests} per {self.window_seconds:.0f}s)"
        if toks + estimated_tokens > self.max_tokens:
            return False, f"token limit ({toks+estimated_tokens}/{self.max_tokens} per {self.window_seconds:.0f}s)"
        if cost + estimated_cost > self.max_cost_usd:
            return False, f"cost limit (${cost+estimated_cost:.3f}/${self.max_cost_usd} per {self.window_seconds:.0f}s)"
        return True, ""

    def record(self, tokens: int, cost: float):
        self._prune()
        self._events.append({"ts": time.time(), "tokens": tokens, "cost": cost})

    def stats(self) -> dict:
        reqs, toks, cost = self._totals()
        return {
            "requests": reqs, "max_requests": self.max_requests,
            "tokens": toks, "max_tokens": self.max_tokens,
            "cost_usd": round(cost, 4), "max_cost_usd": self.max_cost_usd,
        }


class MultiDimRateLimiter:
    def __init__(self, **bucket_kwargs):
        self._sessions: dict[str, MultiDimBucket] = {}
        self._bucket_kwargs = bucket_kwargs

    def _bucket(self, session_id: str) -> MultiDimBucket:
        if session_id not in self._sessions:
            self._sessions[session_id] = MultiDimBucket(**self._bucket_kwargs)
        return self._sessions[session_id]

    def check(self, session_id: str, est_tokens: int = 1000, est_cost: float = 0.001) -> tuple[bool, str]:
        return self._bucket(session_id).check(est_tokens, est_cost)

    def record(self, session_id: str, tokens: int, cost: float):
        self._bucket(session_id).record(tokens, cost)

    def stats(self, session_id: str) -> dict:
        return self._bucket(session_id).stats()


class MultiDimAgent:
    def __init__(self):
        self.client = AsyncAnthropic()
        self.limiter = MultiDimRateLimiter(
            window_seconds=60,
            max_requests=8,
            max_tokens=30_000,
            max_cost_usd=0.05,
        )

    async def chat(self, message: str, session_id: str, model: str = "claude-haiku-4-5-20251001") -> str:
        est_tokens = len(message) // 2 + 512
        est_cost = est_tokens * PRICING[model]["output"]
        allowed, reason = self.limiter.check(session_id, est_tokens, est_cost)
        if not allowed:
            raise RateLimitExceededError(session_id, 0)

        response = await self.client.messages.create(
            model=model,
            max_tokens=1024,
            messages=[{"role": "user", "content": message}],
        )
        actual_tokens = response.usage.input_tokens + response.usage.output_tokens
        actual_cost = (
            response.usage.input_tokens * PRICING[model]["input"]
            + response.usage.output_tokens * PRICING[model]["output"]
        )
        self.limiter.record(session_id, actual_tokens, actual_cost)
        return response.content[0].text

    def session_stats(self, session_id: str) -> dict:
        return self.limiter.stats(session_id)
```

## Solution 3: IP-Based and Session-Based Dual-Layer Limiting

Enforce both per-IP (broad) and per-session (fine) limits; block IPs generating too many sessions.

```python
import asyncio
import time
from collections import defaultdict
from dataclasses import dataclass, field
from anthropic import AsyncAnthropic


@dataclass
class SlidingWindowCounter:
    window_seconds: float
    max_count: int
    _events: list[float] = field(default_factory=list)

    def _prune(self):
        cutoff = time.time() - self.window_seconds
        self._events = [t for t in self._events if t >= cutoff]

    def record(self) -> bool:
        """Returns True if within limit; False if exceeded."""
        self._prune()
        if len(self._events) >= self.max_count:
            return False
        self._events.append(time.time())
        return True

    @property
    def count(self) -> int:
        self._prune()
        return len(self._events)

    @property
    def remaining(self) -> int:
        return max(0, self.max_count - self.count)


class DualLayerRateLimiter:
    """
    Layer 1: Per-IP limits (broad throttle)
    Layer 2: Per-session limits (fine throttle)
    Layer 3: Per-IP session count limit (prevents session farming)
    """

    def __init__(
        self,
        ip_rpm: int = 60,           # Requests per minute per IP
        session_rpm: int = 10,      # Requests per minute per session
        max_sessions_per_ip: int = 5,  # Max concurrent sessions per IP
    ):
        self._ip_counters: dict[str, SlidingWindowCounter] = {}
        self._session_counters: dict[str, SlidingWindowCounter] = {}
        self._ip_sessions: dict[str, set[str]] = defaultdict(set)
        self._ip_rpm = ip_rpm
        self._session_rpm = session_rpm
        self._max_sessions = max_sessions_per_ip

    def _ip_counter(self, ip: str) -> SlidingWindowCounter:
        if ip not in self._ip_counters:
            self._ip_counters[ip] = SlidingWindowCounter(60.0, self._ip_rpm)
        return self._ip_counters[ip]

    def _session_counter(self, session_id: str) -> SlidingWindowCounter:
        if session_id not in self._session_counters:
            self._session_counters[session_id] = SlidingWindowCounter(60.0, self._session_rpm)
        return self._session_counters[session_id]

    def register_session(self, session_id: str, ip: str) -> tuple[bool, str]:
        """Call when a new session starts. Returns (allowed, reason)."""
        if len(self._ip_sessions[ip]) >= self._max_sessions:
            return False, f"IP {ip} exceeded max sessions ({self._max_sessions})"
        self._ip_sessions[ip].add(session_id)
        return True, ""

    def check(self, session_id: str, ip: str) -> tuple[bool, str]:
        if not self._ip_counter(ip).record():
            return False, f"IP rate limit exceeded ({self._ip_rpm} rpm)"
        if not self._session_counter(session_id).record():
            return False, f"Session rate limit exceeded ({self._session_rpm} rpm)"
        return True, ""

    def end_session(self, session_id: str, ip: str):
        self._ip_sessions[ip].discard(session_id)

    def stats(self, session_id: str, ip: str) -> dict:
        return {
            "session_requests_remaining": self._session_counter(session_id).remaining,
            "ip_requests_remaining": self._ip_counter(ip).remaining,
            "ip_sessions": len(self._ip_sessions.get(ip, set())),
        }


class DualLayerAgent:
    def __init__(self):
        self.client = AsyncAnthropic()
        self.limiter = DualLayerRateLimiter(ip_rpm=30, session_rpm=8, max_sessions_per_ip=3)

    def new_session(self, session_id: str, ip: str) -> bool:
        allowed, reason = self.limiter.register_session(session_id, ip)
        if not allowed:
            print(f"[RATE] Session blocked: {reason}")
        return allowed

    async def chat(self, message: str, session_id: str, ip: str) -> str:
        allowed, reason = self.limiter.check(session_id, ip)
        if not allowed:
            raise RateLimitExceededError(session_id, 5.0)

        response = await self.client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            messages=[{"role": "user", "content": message}],
        )
        return response.content[0].text

    def end_session(self, session_id: str, ip: str):
        self.limiter.end_session(session_id, ip)
```

## Solution 4: Adaptive Rate Limiting Based on User Tier

Free users get a tighter limit; paid users get more; enterprise users get the highest allocation.

```python
import asyncio
import time
from dataclasses import dataclass
from enum import Enum
from anthropic import AsyncAnthropic


class UserTier(Enum):
    FREE = "free"
    PAID = "paid"
    ENTERPRISE = "enterprise"


@dataclass
class TierLimits:
    requests_per_minute: int
    tokens_per_hour: int
    max_burst: int


TIER_LIMITS = {
    UserTier.FREE: TierLimits(requests_per_minute=5, tokens_per_hour=50_000, max_burst=2),
    UserTier.PAID: TierLimits(requests_per_minute=30, tokens_per_hour=500_000, max_burst=10),
    UserTier.ENTERPRISE: TierLimits(requests_per_minute=200, tokens_per_hour=5_000_000, max_burst=50),
}


class TieredRateLimiter:
    def __init__(self):
        self._session_data: dict[str, dict] = {}

    def _init_session(self, session_id: str, tier: UserTier):
        limits = TIER_LIMITS[tier]
        self._session_data[session_id] = {
            "tier": tier,
            "limits": limits,
            "minute_events": [],
            "hour_tokens": [],
            "burst_tokens": limits.max_burst,
            "burst_refill_at": time.time(),
        }

    def _get_or_init(self, session_id: str, tier: UserTier) -> dict:
        if session_id not in self._session_data:
            self._init_session(session_id, tier)
        return self._session_data[session_id]

    def check_and_record(
        self, session_id: str, tier: UserTier, tokens: int = 0
    ) -> tuple[bool, str]:
        data = self._get_or_init(session_id, tier)
        limits: TierLimits = data["limits"]
        now = time.time()

        # Refill burst tokens
        elapsed = now - data["burst_refill_at"]
        refill = int(elapsed * limits.requests_per_minute / 60)
        if refill > 0:
            data["burst_tokens"] = min(limits.max_burst, data["burst_tokens"] + refill)
            data["burst_refill_at"] = now

        # Prune old events
        data["minute_events"] = [t for t in data["minute_events"] if now - t < 60]
        data["hour_tokens"] = [(t, tok) for t, tok in data["hour_tokens"] if now - t < 3600]

        # Check limits
        if data["burst_tokens"] <= 0:
            return False, f"burst limit reached for {tier.value} tier"
        if len(data["minute_events"]) >= limits.requests_per_minute:
            return False, f"per-minute limit ({limits.requests_per_minute} rpm) for {tier.value} tier"
        hour_total = sum(tok for _, tok in data["hour_tokens"])
        if hour_total + tokens > limits.tokens_per_hour:
            return False, f"hourly token limit ({limits.tokens_per_hour}) for {tier.value} tier"

        # Record
        data["burst_tokens"] -= 1
        data["minute_events"].append(now)
        if tokens > 0:
            data["hour_tokens"].append((now, tokens))
        return True, ""

    def get_headers(self, session_id: str, tier: UserTier) -> dict:
        """Return rate limit headers for API response."""
        data = self._get_or_init(session_id, tier)
        limits = data["limits"]
        return {
            "X-RateLimit-Limit-Requests": str(limits.requests_per_minute),
            "X-RateLimit-Remaining-Requests": str(
                limits.requests_per_minute - len(data["minute_events"])
            ),
            "X-RateLimit-Limit-Tokens": str(limits.tokens_per_hour),
            "X-RateLimit-Tier": tier.value,
        }


class TieredAgent:
    def __init__(self):
        self.client = AsyncAnthropic()
        self.limiter = TieredRateLimiter()

    async def chat(
        self, message: str, session_id: str, tier: UserTier = UserTier.FREE
    ) -> dict:
        estimated_tokens = len(message) + 512
        allowed, reason = self.limiter.check_and_record(session_id, tier, estimated_tokens)
        if not allowed:
            return {
                "success": False,
                "error": "rate_limited",
                "reason": reason,
                "headers": self.limiter.get_headers(session_id, tier),
            }
        response = await self.client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            messages=[{"role": "user", "content": message}],
        )
        return {
            "success": True,
            "response": response.content[0].text,
            "headers": self.limiter.get_headers(session_id, tier),
        }


async def demo_tiered():
    agent = TieredAgent()
    for i in range(8):
        tier = UserTier.FREE if i < 4 else UserTier.PAID
        result = await agent.chat(f"Question {i}", session_id="s1", tier=tier)
        print(f"Request {i} [{tier.value}]: {'OK' if result['success'] else 'BLOCKED: ' + result.get('reason', '')}")
```

## Solution 5: Leaky Bucket with Request Queuing

Smooth out burst traffic by queuing requests; process at a fixed rate so downstream isn't overwhelmed.

```python
import asyncio
import time
from dataclasses import dataclass, field
from anthropic import AsyncAnthropic


@dataclass
class LeakyBucket:
    """Processes requests at a fixed rate; queues bursts."""
    rate_per_second: float = 0.5    # 1 request per 2 seconds
    max_queue: int = 10             # Max queued requests

    _queue: asyncio.Queue = field(init=False)
    _last_processed: float = field(default_factory=time.time, init=False)

    def __post_init__(self):
        self._queue = asyncio.Queue(maxsize=self.max_queue)

    async def submit(self, coro) -> asyncio.Future:
        """Submit a coroutine to be processed at the rate-limited rate."""
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        try:
            self._queue.put_nowait((coro, future))
        except asyncio.QueueFull:
            future.set_exception(
                RateLimitExceededError("leaky_bucket", 1.0 / self.rate_per_second)
            )
        return future

    async def run(self):
        """Process the queue at the configured rate."""
        while True:
            coro, future = await self._queue.get()
            # Enforce rate: wait until enough time has passed
            now = time.time()
            elapsed = now - self._last_processed
            wait = max(0.0, (1.0 / self.rate_per_second) - elapsed)
            if wait > 0:
                await asyncio.sleep(wait)

            try:
                result = await coro
                if not future.done():
                    future.set_result(result)
            except Exception as e:
                if not future.done():
                    future.set_exception(e)
            finally:
                self._last_processed = time.time()
                self._queue.task_done()


class LeakyBucketAgent:
    def __init__(self, rate_per_second: float = 0.5, max_queue: int = 20):
        self.client = AsyncAnthropic()
        self._bucket = LeakyBucket(rate_per_second=rate_per_second, max_queue=max_queue)
        self._runner: asyncio.Task | None = None

    async def start(self):
        self._runner = asyncio.create_task(self._bucket.run())

    async def stop(self):
        if self._runner:
            self._runner.cancel()

    async def chat(self, message: str) -> str:
        async def _do_chat():
            response = await self.client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=512,
                messages=[{"role": "user", "content": message}],
            )
            return response.content[0].text

        future = await self._bucket.submit(_do_chat())
        return await future


async def demo_leaky_bucket():
    agent = LeakyBucketAgent(rate_per_second=2.0, max_queue=10)
    await agent.start()

    start = time.time()
    messages = [f"Q{i}" for i in range(6)]
    results = await asyncio.gather(
        *[agent.chat(m) for m in messages],
        return_exceptions=True,
    )
    elapsed = time.time() - start
    ok = sum(1 for r in results if not isinstance(r, Exception))
    print(f"Processed {ok}/{len(messages)} in {elapsed:.1f}s (rate=2/s)")
    await agent.stop()
```

## Solution 6: Redis-Backed Distributed Session Rate Limiter

Rate limit per session across multiple agent processes using Redis atomic operations.

```python
import asyncio
import time
from anthropic import AsyncAnthropic

# Requires: redis[asyncio]  (pip install redis)


class RedisSessionRateLimiter:
    """
    Sliding window rate limiter backed by Redis sorted sets.
    Works correctly across multiple agent processes.
    """

    def __init__(
        self,
        redis_url: str = "redis://localhost:6379",
        requests_per_minute: int = 10,
        key_prefix: str = "rl:session:",
        ttl_seconds: int = 120,
    ):
        try:
            import redis.asyncio as aioredis
            self._redis = aioredis.from_url(redis_url, decode_responses=True)
            self._available = True
        except ImportError:
            self._redis = None
            self._available = False
        self._rpm = requests_per_minute
        self._prefix = key_prefix
        self._ttl = ttl_seconds
        self._local_fallback: dict[str, list[float]] = {}

    def _local_check(self, session_id: str) -> bool:
        now = time.time()
        cutoff = now - 60
        events = self._local_fallback.get(session_id, [])
        events = [t for t in events if t >= cutoff]
        if len(events) >= self._rpm:
            return False
        events.append(now)
        self._local_fallback[session_id] = events
        return True

    async def is_allowed(self, session_id: str) -> tuple[bool, int]:
        """Returns (allowed, requests_in_window)."""
        if not self._available or self._redis is None:
            return self._local_check(session_id), 0

        key = f"{self._prefix}{session_id}"
        now = time.time()
        window_start = now - 60.0

        pipe = self._redis.pipeline()
        # Atomically: remove old entries, add current, count, set TTL
        pipe.zremrangebyscore(key, "-inf", window_start)
        pipe.zadd(key, {str(now): now})
        pipe.zcard(key)
        pipe.expire(key, self._ttl)
        results = await pipe.execute()

        count = results[2]
        allowed = count <= self._rpm
        if not allowed:
            # Remove the entry we just added (we're rejecting this request)
            await self._redis.zrem(key, str(now))
        return allowed, count

    async def remaining(self, session_id: str) -> int:
        if not self._available or self._redis is None:
            events = self._local_fallback.get(session_id, [])
            return max(0, self._rpm - len(events))
        key = f"{self._prefix}{session_id}"
        count = await self._redis.zcard(key)
        return max(0, self._rpm - count)


class DistributedRateLimitedAgent:
    def __init__(self):
        self.client = AsyncAnthropic()
        self.limiter = RedisSessionRateLimiter(requests_per_minute=10)

    async def chat(self, message: str, session_id: str) -> str:
        allowed, count = await self.limiter.is_allowed(session_id)
        if not allowed:
            remaining = await self.limiter.remaining(session_id)
            raise RateLimitExceededError(
                session_id,
                60.0 / max(self.limiter._rpm, 1),
            )

        response = await self.client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            messages=[{"role": "user", "content": message}],
        )
        return response.content[0].text


async def demo_distributed():
    agent = DistributedRateLimitedAgent()
    results = await asyncio.gather(
        *[agent.chat(f"Q{i}", "session_x") for i in range(6)],
        return_exceptions=True,
    )
    for i, r in enumerate(results):
        if isinstance(r, RateLimitExceededError):
            print(f"  Request {i}: RATE LIMITED")
        else:
            print(f"  Request {i}: OK — {str(r)[:40]}")
```

## Comparison Table

| Solution | Algorithm | Burst Support | Multi-Process | Tier Support | Best For |
|---|---|---|---|---|---|
| Token Bucket | Token refill | Yes (burst capacity) | No (in-process) | No | Single-process agents with burst tolerance |
| Multi-Dimensional | Sliding window | No | No | No | Request + token + cost enforcement |
| Dual-Layer IP+Session | Sliding window | No | No | No | Abuse prevention, session farming |
| Tiered (User Plans) | Sliding window | Yes (burst field) | No | Yes | SaaS with free/paid/enterprise tiers |
| Leaky Bucket | Queue + fixed rate | Yes (queue depth) | No | No | Smooth downstream load, queue-based |
| Redis Distributed | Sorted set sliding window | No | Yes | No | Multi-container/multi-host deployments |

**Recommended**: Start with **Token Bucket** (Solution 1) for its simplicity and burst support. Add **Multi-Dimensional** (Solution 2) when you need simultaneous cost control alongside request throttling. Use **Tiered** (Solution 4) for SaaS products with multiple subscription plans. Switch to **Redis Distributed** (Solution 6) as soon as your agents scale beyond a single process.
