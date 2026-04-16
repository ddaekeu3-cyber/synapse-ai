---
title: "Agent Doesn't Implement Leaky-Bucket Rate Limiter for Outbound Requests"
description: "AI agents that call external APIs without outbound rate limiting produce uneven burst traffic that triggers provider-side throttling, wastes retry budget, and starves concurrent workloads. A leaky-bucket rate limiter smooths the outflow to a guaranteed maximum rate."
date: 2025-01-31
difficulty: intermediate
category: reliability
slug: agent-doesnt-implement-leaky-bucket-rate-limiter-for-outbound-requests
tags:
  - rate-limiting
  - leaky-bucket
  - token-bucket
  - throttling
  - backpressure
  - reliability
symptoms:
  - "API calls succeed in bursts then hit 429 errors immediately after"
  - "Agent retries pile up and consume the entire retry budget within seconds"
  - "Concurrent agents starve each other on shared API keys"
  - "Tool call latency is bimodal: instant or very slow"
  - "Provider rate-limit windows reset and the agent immediately saturates them again"
---

## Problem

Agents often fire tool calls as fast as the LLM generates them, ignoring that downstream APIs enforce rate limits. Even with retry logic, uncontrolled bursts cause cascading 429s: each retry re-joins a queue that is already at capacity, burning the retry budget and extending actual latency by orders of magnitude.

A leaky-bucket model solves this by decoupling the arrival rate (bursty) from the departure rate (smooth). Requests accumulate in a fixed-capacity bucket; the bucket drains at a constant rate. If the bucket overflows the caller either waits or receives immediate backpressure.

---

## Solution 1: Pure Leaky-Bucket Rate Limiter

Classic leaky-bucket implemented with a monotonic clock and a single `asyncio.Lock`. Supports both blocking (`acquire`) and non-blocking (`try_acquire`) semantics.

```python
import asyncio
import time
from dataclasses import dataclass


class LeakyBucketRateLimiter:
    """
    Leaky-bucket limiter: capacity slots drain at `rate` per second.

    Usage:
        limiter = LeakyBucketRateLimiter(rate=10, capacity=20)
        async with limiter:
            response = await call_api()
    """

    def __init__(self, rate: float, capacity: int):
        self.rate = rate          # requests per second (drain rate)
        self.capacity = capacity  # max burst depth
        self._level: float = 0.0  # current bucket fill
        self._last_check = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, tokens: float = 1.0):
        """Block until `tokens` slots are available."""
        async with self._lock:
            self._drain()
            wait = 0.0
            if self._level + tokens > self.capacity:
                wait = (self._level + tokens - self.capacity) / self.rate
            if wait > 0:
                # Release lock while sleeping so other coroutines can drain
                pass
        if wait > 0:
            await asyncio.sleep(wait)
            async with self._lock:
                self._drain()
        async with self._lock:
            self._drain()
            self._level = min(self._level + tokens, self.capacity)

    def try_acquire(self, tokens: float = 1.0) -> bool:
        """Non-blocking; returns False if bucket is full."""
        self._drain()
        if self._level + tokens > self.capacity:
            return False
        self._level += tokens
        return True

    def _drain(self):
        now = time.monotonic()
        elapsed = now - self._last_check
        self._level = max(0.0, self._level - elapsed * self.rate)
        self._last_check = now

    @property
    def available(self) -> float:
        self._drain()
        return max(0.0, self.capacity - self._level)

    async def __aenter__(self):
        await self.acquire()
        return self

    async def __aexit__(self, *_):
        pass
```

---

## Solution 2: Multi-Key Rate Limiter (Per-API-Key Isolation)

When an agent manages multiple API credentials, each key needs its own leaky bucket. This ensures one key's burst does not starve another, and enables per-key reporting.

```python
import asyncio
import time
from typing import Dict


class MultiKeyLeakyBucketLimiter:
    """
    Maintains a separate leaky-bucket per API key.

    Usage:
        limiter = MultiKeyLeakyBucketLimiter(rate=10, capacity=30)
        async with limiter.key("openai-key-1"):
            ...
        async with limiter.key("openai-key-2"):
            ...
    """

    def __init__(self, rate: float, capacity: int):
        self.rate = rate
        self.capacity = capacity
        self._buckets: Dict[str, "LeakyBucketRateLimiter"] = {}
        self._meta_lock = asyncio.Lock()

    async def _get_bucket(self, key: str) -> "LeakyBucketRateLimiter":
        async with self._meta_lock:
            if key not in self._buckets:
                self._buckets[key] = LeakyBucketRateLimiter(
                    self.rate, self.capacity
                )
            return self._buckets[key]

    def key(self, api_key: str) -> "_BucketContext":
        return _BucketContext(self, api_key)

    def stats(self) -> Dict[str, float]:
        return {
            k: round(b.available, 2) for k, b in self._buckets.items()
        }


class _BucketContext:
    def __init__(self, limiter: MultiKeyLeakyBucketLimiter, key: str):
        self._limiter = limiter
        self._key = key

    async def __aenter__(self):
        bucket = await self._limiter._get_bucket(self._key)
        await bucket.acquire()
        return self

    async def __aexit__(self, *_):
        pass
```

---

## Solution 3: Priority-Aware Leaky Bucket

High-priority tasks (user-interactive) bypass the queue; low-priority tasks (background indexing) are throttled. The bucket itself remains the same but admission is gated by priority level.

```python
import asyncio
import time
from enum import IntEnum
from typing import Optional


class Priority(IntEnum):
    HIGH = 0
    NORMAL = 1
    LOW = 2


class PriorityLeakyBucket:
    """
    Three-tier priority leaky bucket.
    HIGH requests skip the line; NORMAL and LOW queue in FIFO.
    """

    def __init__(self, rate: float, capacity: int,
                 high_priority_reservation: float = 0.2):
        self._rate = rate
        self._capacity = capacity
        self._reservation = high_priority_reservation  # fraction reserved for HIGH
        self._level = 0.0
        self._last = time.monotonic()
        self._lock = asyncio.Lock()
        self._normal_queue: asyncio.Queue = asyncio.Queue()
        self._low_queue: asyncio.Queue = asyncio.Queue()

    async def acquire(self, priority: Priority = Priority.NORMAL):
        if priority == Priority.HIGH:
            await self._acquire_direct(reserved=True)
        elif priority == Priority.NORMAL:
            event = asyncio.Event()
            await self._normal_queue.put(event)
            await event.wait()
        else:
            event = asyncio.Event()
            await self._low_queue.put(event)
            await event.wait()

    async def _acquire_direct(self, reserved: bool = False):
        while True:
            async with self._lock:
                self._drain()
                limit = self._capacity if reserved else (
                    self._capacity * (1 - self._reservation)
                )
                if self._level + 1 <= limit:
                    self._level += 1
                    return
                wait = (self._level + 1 - limit) / self._rate
            await asyncio.sleep(wait)

    def _drain(self):
        now = time.monotonic()
        self._level = max(0.0, self._level - (now - self._last) * self._rate)
        self._last = now

    async def _dispatcher(self):
        """Background task: drain queued requests in priority order."""
        while True:
            await asyncio.sleep(1.0 / self._rate)
            async with self._lock:
                self._drain()
                if self._level + 1 <= self._capacity:
                    self._level += 1
                    for queue in (self._normal_queue, self._low_queue):
                        if not queue.empty():
                            event = await queue.get()
                            event.set()
                            break

    async def start(self):
        asyncio.create_task(self._dispatcher(), name="priority_bucket_dispatcher")
```

---

## Solution 4: Distributed Leaky Bucket (Redis-Backed)

For multi-process or multi-host deployments, a Redis `INCR` + `EXPIRE` pattern implements a shared leaky bucket. Each agent instance reads the global drain state before admitting a request.

```python
import asyncio
import time
from typing import Optional

try:
    import redis.asyncio as aioredis
except ImportError:
    aioredis = None  # type: ignore


class RedisLeakyBucket:
    """
    Redis-backed leaky bucket for cross-process rate limiting.

    Uses a sorted-set of request timestamps as the 'bucket'.
    Entries older than (capacity / rate) seconds are drained on each call.

    Usage:
        bucket = RedisLeakyBucket(redis_url="redis://localhost", rate=10, capacity=20)
        await bucket.connect()
        allowed = await bucket.acquire(key="openai")
    """

    def __init__(self, redis_url: str, rate: float, capacity: int):
        self.redis_url = redis_url
        self.rate = rate
        self.capacity = capacity
        self._window = capacity / rate  # sliding window in seconds
        self._redis: Optional[object] = None

    async def connect(self):
        if aioredis is None:
            raise ImportError("redis[asyncio] is required")
        self._redis = await aioredis.from_url(self.redis_url)

    async def acquire(self, key: str = "default") -> bool:
        """Returns True if request is admitted, False if bucket is full."""
        now = time.time()
        bucket_key = f"leaky:{key}"
        cutoff = now - self._window

        pipe = self._redis.pipeline()
        pipe.zremrangebyscore(bucket_key, "-inf", cutoff)
        pipe.zcard(bucket_key)
        pipe.zadd(bucket_key, {str(now): now})
        pipe.expire(bucket_key, int(self._window) + 1)
        results = await pipe.execute()

        count_before_add = results[1]
        if count_before_add >= self.capacity:
            # Undo the zadd
            await self._redis.zrem(bucket_key, str(now))
            return False
        return True

    async def wait_and_acquire(self, key: str = "default", timeout: float = 30.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if await self.acquire(key):
                return
            await asyncio.sleep(1.0 / self.rate)
        raise TimeoutError(f"Leaky bucket timeout for key={key}")
```

---

## Solution 5: Adaptive Rate Limiter (Adjusts to 429 Responses)

Automatically halves the effective rate on a 429 and gradually recovers. Wraps any async callable.

```python
import asyncio
import time
import logging
from typing import Callable, Any

logger = logging.getLogger(__name__)


class AdaptiveLeakyBucket:
    """
    Leaky bucket that backs off on 429 and recovers exponentially.

    Usage:
        bucket = AdaptiveLeakyBucket(initial_rate=20, min_rate=1, max_rate=50)
        result = await bucket.call(my_api_fn, arg1, arg2)
    """

    def __init__(self, initial_rate: float = 20.0,
                 min_rate: float = 1.0, max_rate: float = 100.0,
                 capacity: int = 40, recovery_factor: float = 1.1):
        self._rate = initial_rate
        self._min_rate = min_rate
        self._max_rate = max_rate
        self._capacity = capacity
        self._recovery_factor = recovery_factor
        self._level = 0.0
        self._last = time.monotonic()
        self._lock = asyncio.Lock()
        self._last_429 = 0.0
        self._recovery_task: asyncio.Task = None

    async def _acquire(self):
        while True:
            async with self._lock:
                now = time.monotonic()
                self._level = max(
                    0.0, self._level - (now - self._last) * self._rate
                )
                self._last = now
                if self._level + 1 <= self._capacity:
                    self._level += 1
                    return
                wait = (self._level + 1 - self._capacity) / self._rate
            await asyncio.sleep(wait)

    def _on_429(self):
        self._last_429 = time.monotonic()
        new_rate = max(self._min_rate, self._rate / 2)
        if new_rate != self._rate:
            logger.warning("Rate limit hit; reducing rate %.1f -> %.1f rps", self._rate, new_rate)
        self._rate = new_rate
        # Schedule recovery
        if self._recovery_task is None or self._recovery_task.done():
            self._recovery_task = asyncio.create_task(self._recover())

    async def _recover(self):
        await asyncio.sleep(10.0)
        while True:
            await asyncio.sleep(5.0)
            async with self._lock:
                new_rate = min(self._max_rate, self._rate * self._recovery_factor)
                if new_rate != self._rate:
                    logger.info("Recovering rate: %.1f -> %.1f rps", self._rate, new_rate)
                self._rate = new_rate
            if self._rate >= self._max_rate:
                break

    async def call(self, fn: Callable, *args, **kwargs) -> Any:
        await self._acquire()
        try:
            return await fn(*args, **kwargs)
        except Exception as exc:
            msg = str(exc).lower()
            if "429" in msg or "rate limit" in msg or "too many" in msg:
                self._on_429()
            raise
```

---

## Solution 6: Rate-Limited Agent Tool Executor

Drop-in wrapper for agent tool registries. All tool calls are funnelled through per-tool leaky buckets automatically.

```python
import asyncio
import time
from typing import Callable, Dict, Any, Optional
from dataclasses import dataclass


@dataclass
class ToolRateConfig:
    rate: float           # requests per second
    capacity: int         # burst size
    priority: int = 1     # higher = more important


class RateLimitedToolExecutor:
    """
    Wraps tool functions with per-tool leaky-bucket rate limiting.

    Usage:
        executor = RateLimitedToolExecutor()
        executor.register("web_search", web_search_fn, ToolRateConfig(rate=2, capacity=5))
        executor.register("db_query", db_query_fn, ToolRateConfig(rate=50, capacity=100))

        result = await executor.run("web_search", query="...")
    """

    def __init__(self, default_rate: float = 10.0, default_capacity: int = 20):
        self._tools: Dict[str, Callable] = {}
        self._buckets: Dict[str, LeakyBucketRateLimiter] = {}
        self._configs: Dict[str, ToolRateConfig] = {}
        self._default_rate = default_rate
        self._default_capacity = default_capacity
        self._call_counts: Dict[str, int] = {}
        self._wait_totals: Dict[str, float] = {}

    def register(self, name: str, fn: Callable,
                 config: Optional[ToolRateConfig] = None):
        self._tools[name] = fn
        cfg = config or ToolRateConfig(
            rate=self._default_rate, capacity=self._default_capacity
        )
        self._configs[name] = cfg
        self._buckets[name] = LeakyBucketRateLimiter(cfg.rate, cfg.capacity)
        self._call_counts[name] = 0
        self._wait_totals[name] = 0.0

    async def run(self, tool_name: str, **kwargs) -> Any:
        if tool_name not in self._tools:
            raise KeyError(f"Unknown tool: {tool_name}")
        bucket = self._buckets[tool_name]
        t0 = time.monotonic()
        await bucket.acquire()
        wait_ms = (time.monotonic() - t0) * 1000
        self._call_counts[tool_name] += 1
        self._wait_totals[tool_name] += wait_ms
        return await self._tools[tool_name](**kwargs)

    def stats(self) -> Dict[str, dict]:
        result = {}
        for name, count in self._call_counts.items():
            result[name] = {
                "calls": count,
                "avg_wait_ms": round(
                    self._wait_totals[name] / max(1, count), 2
                ),
                "available_slots": round(self._buckets[name].available, 1),
                "rate_rps": self._configs[name].rate,
            }
        return result
```

---

## Comparison

| Approach | Use Case | State Location | Priority Support |
|---|---|---|---|
| **Pure Leaky Bucket** | Single process, single API | In-process | No |
| **Multi-Key Limiter** | Multiple API credentials | In-process per key | No |
| **Priority Leaky Bucket** | Mixed interactive/batch workloads | In-process | Yes (3 tiers) |
| **Redis Leaky Bucket** | Multi-process / multi-host deployment | Redis sorted set | No |
| **Adaptive Rate Limiter** | Unknown provider limits, self-tuning | In-process | No |
| **Rate-Limited Tool Executor** | Agent tool registry with per-tool limits | In-process | Via config |

**Recommendation**: start with the Pure Leaky Bucket for single-instance agents; add the Redis backend when scaling horizontally; enable the Adaptive variant when provider limits are undocumented or variable.
