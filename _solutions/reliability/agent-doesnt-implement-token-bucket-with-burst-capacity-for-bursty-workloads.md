---
title: "Agent Doesn't Implement Token Bucket with Burst Capacity for Bursty Workloads"
description: "AI agents that use a plain rate limiter (fixed window or leaky bucket) reject valid bursts that arrive within credit. The token bucket algorithm accumulates unused capacity up to a configurable burst ceiling, smoothing short spikes while enforcing a long-run average rate — exactly the pattern needed for agents that receive bursty user traffic or make bursty tool calls."
date: 2025-02-09
difficulty: intermediate
category: reliability
slug: agent-doesnt-implement-token-bucket-with-burst-capacity-for-bursty-workloads
tags:
  - token-bucket
  - rate-limiting
  - burst-capacity
  - throttling
  - asyncio
  - backpressure
  - traffic-shaping
symptoms:
  - "Agent rejects legitimate requests during a short spike even though average rate is within limit"
  - "Fixed-window rate limiter creates thundering-herd at window boundary resets"
  - "Agent tool calls are rejected by downstream API despite total hourly volume being fine"
  - "Rate limiter has no concept of saved-up capacity from quiet periods"
  - "Burst of 10 simultaneous user messages rejected even though previous minute was idle"
---

## Problem

Fixed-window rate limiters reset at clock boundaries, causing thundering-herd resets. Leaky buckets drain at a fixed rate and discard bursts that exceed the pipe. The token bucket model fills at the average rate but allows consuming tokens faster than they arrive — up to a burst ceiling. This is the right primitive for agents serving human users (whose requests are naturally bursty) and for agents calling APIs that publish both a per-second and a burst limit.

---

## Solution 1: AsyncTokenBucket — Core Implementation

```python
import asyncio
import time
from dataclasses import dataclass
from typing import Optional


@dataclass
class TokenBucketConfig:
    rate: float          # tokens replenished per second (long-run average)
    capacity: float      # maximum tokens (burst ceiling)
    initial: Optional[float] = None   # starting tokens (default: full bucket)


class AsyncTokenBucket:
    """
    Token bucket rate limiter with configurable burst capacity.
    Thread-safe for asyncio (single event loop); uses a lock for coroutine safety.

    Usage:
        bucket = AsyncTokenBucket(TokenBucketConfig(rate=10, capacity=50))

        # Acquire 1 token (waits if bucket is empty)
        async with bucket:
            await call_llm(prompt)

        # Acquire N tokens at once (e.g., proportional to prompt size)
        await bucket.acquire(tokens=5)
        result = await call_embedding_api(text)
    """

    def __init__(self, config: TokenBucketConfig):
        self._rate = config.rate
        self._capacity = config.capacity
        self._tokens = config.initial if config.initial is not None else config.capacity
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    def _refill(self):
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(
            self._capacity,
            self._tokens + elapsed * self._rate,
        )
        self._last_refill = now

    async def acquire(self, tokens: float = 1.0):
        """Block until `tokens` are available, then consume them."""
        while True:
            async with self._lock:
                self._refill()
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return
                deficit = tokens - self._tokens
                wait_s = deficit / self._rate
            await asyncio.sleep(wait_s)

    async def try_acquire(self, tokens: float = 1.0) -> bool:
        """Non-blocking: return True if tokens available, False otherwise."""
        async with self._lock:
            self._refill()
            if self._tokens >= tokens:
                self._tokens -= tokens
                return True
            return False

    @property
    def available(self) -> float:
        self._refill()
        return self._tokens

    async def __aenter__(self):
        await self.acquire()
        return self

    async def __aexit__(self, *_):
        pass
```

---

## Solution 2: HierarchicalTokenBucket — Global + Per-User Limits

Apply a global bucket (total agent capacity) and a per-user bucket simultaneously. A request must satisfy both to proceed.

```python
import asyncio
import time
from typing import Dict, Optional


class HierarchicalTokenBucket:
    """
    Two-level token bucket: global (total capacity) and per-key (per-user/tenant).
    A request must consume tokens from both levels.

    Usage:
        limiter = HierarchicalTokenBucket(
            global_rate=100, global_capacity=500,
            per_key_rate=5,  per_key_capacity=20,
        )
        await limiter.acquire(key="user-123")
        await tool_call()
    """

    def __init__(self,
                 global_rate: float, global_capacity: float,
                 per_key_rate: float, per_key_capacity: float):
        self._global = AsyncTokenBucket(
            TokenBucketConfig(global_rate, global_capacity)
        )
        self._per_key_rate = per_key_rate
        self._per_key_capacity = per_key_capacity
        self._buckets: Dict[str, AsyncTokenBucket] = {}
        self._create_lock = asyncio.Lock()

    async def _get_bucket(self, key: str) -> AsyncTokenBucket:
        if key not in self._buckets:
            async with self._create_lock:
                if key not in self._buckets:
                    self._buckets[key] = AsyncTokenBucket(
                        TokenBucketConfig(self._per_key_rate, self._per_key_capacity)
                    )
        return self._buckets[key]

    async def acquire(self, key: str, tokens: float = 1.0):
        user_bucket = await self._get_bucket(key)
        # Acquire from both; user bucket first (cheaper to retry)
        await user_bucket.acquire(tokens)
        await self._global.acquire(tokens)

    async def try_acquire(self, key: str, tokens: float = 1.0) -> bool:
        user_bucket = await self._get_bucket(key)
        if not await user_bucket.try_acquire(tokens):
            return False
        if not await self._global.try_acquire(tokens):
            # Refund user bucket
            user_bucket._tokens = min(user_bucket._capacity,
                                       user_bucket._tokens + tokens)
            return False
        return True

    def stats(self) -> dict:
        return {
            "global_available": round(self._global.available, 2),
            "active_keys": len(self._buckets),
            "per_key_available": {
                k: round(b.available, 2)
                for k, b in list(self._buckets.items())
            },
        }
```

---

## Solution 3: TokenBucketMiddleware — Tool Call Throttle with Cost Mapping

Different tools have different costs. Assign token costs per tool name; the bucket enforces the total cost rate.

```python
import asyncio
from functools import wraps
from typing import Any, Callable, Dict, Optional


class ToolCostRegistry:
    """Maps tool names to token costs. Default cost = 1."""

    def __init__(self, costs: Optional[Dict[str, float]] = None):
        self._costs = costs or {}
        self._default = 1.0

    def cost_of(self, tool_name: str) -> float:
        return self._costs.get(tool_name, self._default)

    def register(self, tool_name: str, cost: float):
        self._costs[tool_name] = cost


class TokenBucketToolMiddleware:
    """
    Wraps tool call functions with token-bucket throttling.
    Each tool consumes a configurable number of tokens per call.

    Usage:
        bucket = AsyncTokenBucket(TokenBucketConfig(rate=20, capacity=100))
        costs = ToolCostRegistry({
            "web_search":    5,   # expensive: external API
            "db_query":      2,   # moderate
            "format_output": 0.5, # cheap: local
        })
        middleware = TokenBucketToolMiddleware(bucket, costs)

        @middleware.throttle("web_search")
        async def web_search(query: str): ...

        @middleware.throttle("db_query")
        async def db_query(sql: str): ...
    """

    def __init__(self, bucket: AsyncTokenBucket, costs: ToolCostRegistry):
        self._bucket = bucket
        self._costs = costs

    def throttle(self, tool_name: str):
        cost = self._costs.cost_of(tool_name)

        def decorator(fn: Callable) -> Callable:
            @wraps(fn)
            async def wrapper(*args, **kwargs) -> Any:
                await self._bucket.acquire(cost)
                return await fn(*args, **kwargs)
            return wrapper
        return decorator

    async def call(self, tool_name: str, fn: Callable, *args, **kwargs) -> Any:
        cost = self._costs.cost_of(tool_name)
        await self._bucket.acquire(cost)
        return await fn(*args, **kwargs)
```

---

## Solution 4: AdaptiveBurstBucket — Auto-Tunes Burst Ceiling

Observes actual traffic patterns and adjusts the burst ceiling upward during consistently low-utilisation periods, downward when approaching capacity.

```python
import asyncio
import time
from collections import deque
from typing import Deque


class AdaptiveBurstBucket:
    """
    Token bucket that auto-adjusts burst ceiling based on observed utilisation.
    During idle periods the ceiling grows; during sustained saturation it shrinks.

    Usage:
        bucket = AdaptiveBurstBucket(
            rate=10, min_capacity=20, max_capacity=200,
            adjust_interval=60.0,
        )
        asyncio.create_task(bucket.auto_adjust())
        async with bucket:
            await agent_step()
    """

    def __init__(self, rate: float,
                 min_capacity: float = 10,
                 max_capacity: float = 1000,
                 adjust_interval: float = 60.0,
                 target_utilisation: float = 0.7):
        self._rate = rate
        self._capacity = min_capacity * 2
        self._min_cap = min_capacity
        self._max_cap = max_capacity
        self._adjust_interval = adjust_interval
        self._target_util = target_utilisation
        self._tokens = self._capacity
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()
        self._request_times: Deque[float] = deque(maxlen=1000)

    def _refill(self):
        now = time.monotonic()
        self._tokens = min(self._capacity,
                           self._tokens + (now - self._last_refill) * self._rate)
        self._last_refill = now

    async def acquire(self, tokens: float = 1.0):
        self._request_times.append(time.monotonic())
        while True:
            async with self._lock:
                self._refill()
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return
                wait_s = (tokens - self._tokens) / self._rate
            await asyncio.sleep(wait_s)

    async def auto_adjust(self):
        while True:
            await asyncio.sleep(self._adjust_interval)
            now = time.monotonic()
            window = self._adjust_interval
            recent = sum(1 for t in self._request_times if now - t < window)
            utilisation = (recent / window) / self._rate
            async with self._lock:
                if utilisation < self._target_util * 0.5:
                    self._capacity = min(self._max_cap, self._capacity * 1.2)
                elif utilisation > self._target_util:
                    self._capacity = max(self._min_cap, self._capacity * 0.9)

    async def __aenter__(self):
        await self.acquire()
        return self

    async def __aexit__(self, *_):
        pass
```

---

## Solution 5: RedisTokenBucket — Distributed Rate Limiting

Share a single token bucket across multiple agent replicas using Redis atomic Lua scripts.

```python
import time
from typing import Optional


REFILL_SCRIPT = """
local key = KEYS[1]
local rate = tonumber(ARGV[1])
local capacity = tonumber(ARGV[2])
local tokens_needed = tonumber(ARGV[3])
local now = tonumber(ARGV[4])

local data = redis.call('HMGET', key, 'tokens', 'last_refill')
local tokens = tonumber(data[1]) or capacity
local last_refill = tonumber(data[2]) or now

local elapsed = now - last_refill
tokens = math.min(capacity, tokens + elapsed * rate)

if tokens >= tokens_needed then
    tokens = tokens - tokens_needed
    redis.call('HMSET', key, 'tokens', tokens, 'last_refill', now)
    redis.call('EXPIRE', key, 3600)
    return 1
else
    redis.call('HMSET', key, 'tokens', tokens, 'last_refill', now)
    redis.call('EXPIRE', key, 3600)
    return 0
end
"""


class RedisTokenBucket:
    """
    Token bucket backed by Redis for multi-replica agents.
    Uses a Lua script for atomic read-modify-write.

    Usage:
        import redis.asyncio as aioredis
        client = aioredis.from_url("redis://localhost")
        bucket = RedisTokenBucket(client, key="agent:ratelimit",
                                   rate=50, capacity=200)
        if await bucket.try_acquire():
            await call_tool()
        else:
            raise RateLimitExceeded()
    """

    def __init__(self, redis_client, key: str,
                 rate: float, capacity: float):
        self._redis = redis_client
        self._key = key
        self._rate = rate
        self._capacity = capacity
        self._script = None

    async def _get_script(self):
        if self._script is None:
            self._script = self._redis.register_script(REFILL_SCRIPT)
        return self._script

    async def try_acquire(self, tokens: float = 1.0) -> bool:
        script = await self._get_script()
        result = await script(
            keys=[self._key],
            args=[self._rate, self._capacity, tokens, time.time()],
        )
        return bool(result)

    async def acquire(self, tokens: float = 1.0,
                      poll_interval: float = 0.05):
        while not await self.try_acquire(tokens):
            await __import__("asyncio").sleep(poll_interval)

    async def remaining(self) -> float:
        data = await self._redis.hmget(self._key, "tokens", "last_refill")
        tokens = float(data[0] or self._capacity)
        last = float(data[1] or time.time())
        return min(self._capacity, tokens + (time.time() - last) * self._rate)
```

---

## Solution 6: TokenBucketObservabilityMixin — Metrics and Alerts

Attach metrics counters and alert thresholds to any token bucket.

```python
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional


@dataclass
class BucketMetrics:
    acquired: int = 0
    rejected: int = 0
    waited_total_s: float = 0.0
    burst_events: int = 0        # acquisitions that drained > 50% of capacity
    refill_starvation_events: int = 0

    @property
    def rejection_rate(self) -> float:
        total = self.acquired + self.rejected
        return self.rejected / total if total else 0.0

    @property
    def avg_wait_ms(self) -> float:
        return (self.waited_total_s / self.acquired * 1000) if self.acquired else 0.0


class ObservableTokenBucket(AsyncTokenBucket):
    """
    Token bucket with built-in metrics collection and alert callbacks.

    Usage:
        def on_high_rejection(metrics):
            alerting.fire("token_bucket_rejection_spike",
                          rate=metrics.rejection_rate)

        bucket = ObservableTokenBucket(
            TokenBucketConfig(rate=10, capacity=50),
            alert_rejection_rate=0.2,
            on_alert=on_high_rejection,
        )
        async with bucket:
            await tool_call()
        print(bucket.metrics)
    """

    def __init__(self, config: TokenBucketConfig,
                 alert_rejection_rate: float = 0.3,
                 on_alert: Optional[Callable] = None):
        super().__init__(config)
        self._metrics = BucketMetrics()
        self._alert_rate = alert_rejection_rate
        self._on_alert = on_alert

    async def acquire(self, tokens: float = 1.0):
        t0 = time.monotonic()
        await super().acquire(tokens)
        waited = time.monotonic() - t0
        self._metrics.acquired += 1
        self._metrics.waited_total_s += waited
        if tokens > self._capacity * 0.5:
            self._metrics.burst_events += 1

    async def try_acquire(self, tokens: float = 1.0) -> bool:
        ok = await super().try_acquire(tokens)
        if ok:
            self._metrics.acquired += 1
        else:
            self._metrics.rejected += 1
            if (self._on_alert and
                    self._metrics.rejection_rate > self._alert_rate):
                self._on_alert(self._metrics)
        return ok

    @property
    def metrics(self) -> BucketMetrics:
        return self._metrics

    def snapshot(self) -> Dict:
        m = self._metrics
        return {
            "acquired": m.acquired,
            "rejected": m.rejected,
            "rejection_rate": round(m.rejection_rate, 4),
            "avg_wait_ms": round(m.avg_wait_ms, 2),
            "burst_events": m.burst_events,
            "available_tokens": round(self.available, 2),
        }
```

---

## Comparison

| Approach | Burst Support | Distributed | Per-User | Adaptive | Metrics |
|---|---|---|---|---|---|
| **AsyncTokenBucket** | Yes | No | No | No | No |
| **HierarchicalTokenBucket** | Yes | No | Yes | No | No |
| **ToolCostMiddleware** | Yes | No | No | No | No |
| **AdaptiveBurstBucket** | Yes (auto-tunes) | No | No | Yes | No |
| **RedisTokenBucket** | Yes | Yes | Via key | No | No |
| **ObservableTokenBucket** | Yes | No | No | No | Yes |

**Key insight**: always set `capacity` to 2–5× `rate` for interactive agents — this absorbs a burst of simultaneous messages without rejecting valid requests. Use `HierarchicalTokenBucket` when you need fairness across users sharing a shared pool, and `RedisTokenBucket` for horizontally-scaled deployments.
