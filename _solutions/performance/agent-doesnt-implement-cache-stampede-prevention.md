---
title: "Agent Doesn't Implement Cache Stampede Prevention"
description: "How to prevent thundering-herd cache stampedes — where many concurrent requests simultaneously miss a cold or expired cache and overwhelm the backend — using probabilistic early expiry, mutex coalescing, background refresh, and request coalescing patterns."
date: 2025-01-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-cache-stampede-prevention
tags:
  - performance
  - caching
  - stampede
  - thundering-herd
  - concurrency
  - background-refresh
  - probabilistic-expiry
symptoms:
  - "Sudden spike in API calls when a popular cached value expires"
  - "Model inference latency spikes after cache TTL expiry under load"
  - "Hundreds of concurrent requests all hit the backend simultaneously on cold start"
  - "Redis or database overwhelmed immediately after a cache flush"
  - "P99 latency spikes correlate with cache TTL intervals"
  - "Duplicate LLM calls for identical prompts sent within milliseconds of each other"
---

## Why This Happens

When a cached value expires under high concurrent load, every in-flight request simultaneously detects the cache miss and races to recompute the value. If recomputation is expensive (LLM call, database query, embedding generation), all N concurrent requests trigger N backend calls simultaneously — the thundering herd. This is especially damaging for AI agents where a single LLM call takes seconds and costs money.

The root cause is that cache expiry is a global event visible to all concurrent readers, but cache population is not coordinated. Solutions fall into three categories: *prevent* simultaneous expiry (probabilistic early renewal), *coalesce* concurrent misses (single-flight), or *serve stale* during recomputation (background refresh).

---

## Solution 1: Probabilistic Early Expiry (XFetch Algorithm)

XFetch probabilistically recomputes a cache entry *before* it expires, with probability that increases as expiry approaches. Early recomputation by one request prevents the synchronized expiry stampede.

```python
import asyncio
import math
import random
import time
from dataclasses import dataclass
from typing import Any, Callable, Awaitable, Optional

@dataclass
class CacheEntry:
    value: Any
    expires_at: float
    delta: float  # time taken to compute this value (seconds)

class XFetchCache:
    """
    Probabilistic cache stampede prevention using the XFetch algorithm.

    A request recomputes early if:
        current_time + beta * delta * ln(random()) > expires_at - ttl

    beta controls aggressiveness (higher = earlier recomputation).
    """

    def __init__(self, beta: float = 1.0):
        self.beta = beta
        self._store: dict[str, CacheEntry] = {}
        self._lock = asyncio.Lock()

    def _should_recompute(self, entry: CacheEntry) -> bool:
        """XFetch early recomputation decision."""
        # Higher beta or longer delta -> earlier probabilistic refresh
        t = time.monotonic()
        # Probability of early refresh increases as expiry approaches
        early_recompute_threshold = entry.expires_at - self.beta * entry.delta * math.log(random.random() + 1e-10)
        return t >= early_recompute_threshold

    async def get_or_compute(
        self,
        key: str,
        compute_fn: Callable[[], Awaitable[Any]],
        ttl: float,
    ) -> Any:
        """
        Return cached value, or recompute if expired / probabilistically early.
        """
        async with self._lock:
            entry = self._store.get(key)
            now = time.monotonic()

            # Hard expiry
            if entry is not None and now < entry.expires_at:
                if not self._should_recompute(entry):
                    return entry.value
                # Probabilistic early refresh — fall through to recompute

        # Recompute outside lock to allow concurrent reads of stale value
        start = time.monotonic()
        value = await compute_fn()
        delta = time.monotonic() - start

        async with self._lock:
            self._store[key] = CacheEntry(
                value=value,
                expires_at=time.monotonic() + ttl,
                delta=delta,
            )
        return value

    def invalidate(self, key: str) -> None:
        self._store.pop(key, None)


# --- Usage ---

async def demo_xfetch():
    cache = XFetchCache(beta=1.5)

    async def expensive_llm_call() -> str:
        await asyncio.sleep(2.0)  # Simulate LLM latency
        return "LLM response"

    # First call populates cache
    result = await cache.get_or_compute("system:prompt", expensive_llm_call, ttl=60.0)
    print(f"Result: {result}")

    # Subsequent calls return cached — with probabilistic early refresh near expiry
    result2 = await cache.get_or_compute("system:prompt", expensive_llm_call, ttl=60.0)
    print(f"Cached: {result2}")
```

---

## Solution 2: Single-Flight (Request Coalescing)

When multiple concurrent requests miss the same cache key simultaneously, only one recomputes while the rest wait and share the result. This is the most effective stampede prevention for synchronous caches.

```python
import asyncio
from typing import Any, Callable, Awaitable

class SingleFlightGroup:
    """
    Ensures that only one in-flight computation runs per key at a time.
    All concurrent callers for the same key share the single result.
    """

    def __init__(self):
        self._calls: dict[str, asyncio.Future] = {}
        self._lock = asyncio.Lock()

    async def do(
        self,
        key: str,
        compute_fn: Callable[[], Awaitable[Any]],
    ) -> Any:
        """
        Execute compute_fn once per key; deduplicate concurrent callers.
        """
        async with self._lock:
            if key in self._calls:
                # Join the in-flight call
                future = self._calls[key]
            else:
                # Be the leader: create a future others will wait on
                future = asyncio.get_event_loop().create_future()
                self._calls[key] = future

        if not future.done():
            # Are we the leader?
            async with self._lock:
                if self._calls.get(key) is future and not future.done():
                    # We are leader — compute
                    try:
                        result = await compute_fn()
                        future.set_result(result)
                    except Exception as exc:
                        future.set_exception(exc)
                    finally:
                        async with self._lock:
                            self._calls.pop(key, None)
                    return result

        # Wait for leader's result
        return await asyncio.shield(future)


class SingleFlightCache:
    """Cache with single-flight stampede prevention."""

    def __init__(self, ttl: float = 60.0):
        self._store: dict[str, tuple[Any, float]] = {}
        self._sf = SingleFlightGroup()
        self._ttl = ttl

    async def get(
        self,
        key: str,
        compute_fn: Callable[[], Awaitable[Any]],
        ttl: Optional[float] = None,
    ) -> Any:
        ttl = ttl or self._ttl
        now = time.monotonic()

        # Fast path: cache hit
        if key in self._store:
            value, exp = self._store[key]
            if now < exp:
                return value

        # Slow path: deduplicated miss
        async def _compute_and_store():
            value = await compute_fn()
            self._store[key] = (value, time.monotonic() + ttl)
            return value

        return await self._sf.do(key, _compute_and_store)

    def invalidate(self, key: str) -> None:
        self._store.pop(key, None)


# --- Usage: many concurrent requests share one LLM call ---

async def demo_single_flight():
    cache = SingleFlightCache(ttl=300.0)
    call_count = 0

    async def fetch_model_config() -> dict:
        nonlocal call_count
        call_count += 1
        await asyncio.sleep(1.0)
        return {"model": "claude-3", "max_tokens": 4096}

    # 50 concurrent requests — only 1 backend call
    tasks = [cache.get("model:config", fetch_model_config) for _ in range(50)]
    results = await asyncio.gather(*tasks)
    print(f"Backend calls: {call_count}")  # -> 1
    print(f"All same result: {len(set(str(r) for r in results)) == 1}")  # -> True
```

---

## Solution 3: Stale-While-Revalidate with Background Refresh

Serve the stale cached value immediately while asynchronously refreshing in the background. Callers never block on recomputation after the first population.

```python
import asyncio
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

@dataclass
class SWREntry:
    value: Any
    fresh_until: float   # serve fresh before this time
    stale_until: float   # serve stale (while refreshing) before this time
    refreshing: bool = False

class StaleWhileRevalidateCache:
    """
    Stale-While-Revalidate cache pattern.
    - Within fresh_ttl: return cached value directly
    - Between fresh_ttl and stale_ttl: return stale value + trigger background refresh
    - Beyond stale_ttl: block and recompute synchronously
    """

    def __init__(self, fresh_ttl: float = 30.0, stale_ttl: float = 300.0):
        self.fresh_ttl = fresh_ttl
        self.stale_ttl = stale_ttl
        self._store: dict[str, SWREntry] = {}
        self._lock = asyncio.Lock()
        self._sf = SingleFlightGroup()

    async def get(
        self,
        key: str,
        compute_fn: Callable[[], Awaitable[Any]],
    ) -> Any:
        now = time.monotonic()

        async with self._lock:
            entry = self._store.get(key)

        if entry is not None:
            if now < entry.fresh_until:
                # Fresh — serve directly
                return entry.value

            if now < entry.stale_until:
                # Stale but within grace window — serve stale + refresh in background
                if not entry.refreshing:
                    async with self._lock:
                        if not entry.refreshing:
                            entry.refreshing = True
                    asyncio.create_task(self._background_refresh(key, compute_fn))
                return entry.value

        # Fully expired or not yet cached — block and compute
        return await self._compute_and_store(key, compute_fn)

    async def _compute_and_store(self, key: str, compute_fn: Callable) -> Any:
        async def _inner():
            value = await compute_fn()
            now = time.monotonic()
            async with self._lock:
                self._store[key] = SWREntry(
                    value=value,
                    fresh_until=now + self.fresh_ttl,
                    stale_until=now + self.stale_ttl,
                    refreshing=False,
                )
            return value
        return await self._sf.do(key, _inner)

    async def _background_refresh(self, key: str, compute_fn: Callable) -> None:
        try:
            await self._compute_and_store(key, compute_fn)
            logger.debug("Background refresh complete for key: %s", key)
        except Exception as exc:
            logger.warning("Background refresh failed for key '%s': %s", key, exc)
            # Reset refreshing flag so next request can retry
            async with self._lock:
                entry = self._store.get(key)
                if entry:
                    entry.refreshing = False

    def invalidate(self, key: str) -> None:
        self._store.pop(key, None)
```

---

## Solution 4: Distributed Stampede Lock with Redis

For caches backed by Redis (multi-process agents), use a distributed mutex to ensure only one process recomputes on a miss.

```python
import asyncio
import uuid
import redis.asyncio as aioredis
from typing import Optional

class RedisStampedeGuard:
    """
    Distributed stampede prevention using Redis SET NX (mutex).
    One process holds the recompute lock; others poll and wait for the result.
    """

    LOCK_TTL = 30      # seconds — lock expires if holder crashes
    POLL_INTERVAL = 0.1  # seconds between polls for followers

    def __init__(self, redis_url: str = "redis://localhost:6379"):
        self.redis = aioredis.from_url(redis_url, decode_responses=True)

    def _lock_key(self, key: str) -> str:
        return f"stampede_lock:{key}"

    def _value_key(self, key: str) -> str:
        return f"cache_value:{key}"

    async def get_or_compute(
        self,
        key: str,
        compute_fn: Callable[[], Awaitable[Any]],
        ttl: int = 300,
        max_wait: float = 35.0,
    ) -> Any:
        import json

        # Fast path: value already in cache
        cached = await self.redis.get(self._value_key(key))
        if cached:
            return json.loads(cached)

        lock_id = str(uuid.uuid4())
        lock_key = self._lock_key(key)

        # Try to acquire the recompute lock
        acquired = await self.redis.set(lock_key, lock_id, nx=True, ex=self.LOCK_TTL)

        if acquired:
            # We are the recomputer
            try:
                value = await compute_fn()
                await self.redis.set(self._value_key(key), json.dumps(value), ex=ttl)
                return value
            finally:
                # Release lock only if we still own it
                stored = await self.redis.get(lock_key)
                if stored == lock_id:
                    await self.redis.delete(lock_key)
        else:
            # We are a follower — poll until the leader populates the value
            deadline = time.monotonic() + max_wait
            while time.monotonic() < deadline:
                await asyncio.sleep(self.POLL_INTERVAL)
                cached = await self.redis.get(self._value_key(key))
                if cached:
                    return json.loads(cached)
                # Check if lock is still held (leader may have crashed)
                lock_held = await self.redis.exists(lock_key)
                if not lock_held:
                    # Lock expired without result — retry as potential new leader
                    return await self.get_or_compute(key, compute_fn, ttl, max_wait)

            raise TimeoutError(f"Cache recomputation for '{key}' did not complete within {max_wait}s")
```

---

## Solution 5: Layered Cache with Stampede Protection

Combine L1 (in-process), L2 (Redis), and stampede protection into a unified cache hierarchy.

```python
class LayeredAntiStampedeCache:
    """
    L1 (process memory) + L2 (Redis) + single-flight per layer.
    L1 miss -> check L2 -> miss -> single-flight recompute -> populate both.
    """

    def __init__(
        self,
        redis_url: str,
        l1_ttl: float = 10.0,
        l2_ttl: int = 300,
    ):
        self.l1 = SingleFlightCache(ttl=l1_ttl)
        self.l2 = RedisStampedeGuard(redis_url)
        self.l2_ttl = l2_ttl

    async def get(
        self,
        key: str,
        compute_fn: Callable[[], Awaitable[Any]],
    ) -> Any:
        # L1 hit: fast path
        async def populate_from_l2_or_compute() -> Any:
            return await self.l2.get_or_compute(key, compute_fn, ttl=self.l2_ttl)

        return await self.l1.get(key, populate_from_l2_or_compute)

    def invalidate_local(self, key: str) -> None:
        """Invalidate L1 only (e.g., after receiving a cache invalidation event)."""
        self.l1.invalidate(key)
```

---

## Solution 6: Adaptive TTL Jitter

Add random jitter to TTL values so that entries populated at the same time don't all expire simultaneously, spreading the recomputation load over time.

```python
import random
from typing import Any, Callable, Awaitable

class JitteredTTLCache:
    """
    Prevents correlated expiry by randomizing TTL with a configurable jitter factor.
    All cache operations have inherent stampede resistance from TTL spread.
    """

    def __init__(self, base_ttl: float = 60.0, jitter_fraction: float = 0.25):
        self.base_ttl = base_ttl
        self.jitter_fraction = jitter_fraction
        self._store: dict[str, tuple[Any, float]] = {}
        self._sf = SingleFlightGroup()

    def _jittered_ttl(self) -> float:
        jitter = self.base_ttl * self.jitter_fraction
        return self.base_ttl + random.uniform(-jitter, jitter)

    async def get(
        self,
        key: str,
        compute_fn: Callable[[], Awaitable[Any]],
    ) -> Any:
        now = time.monotonic()

        if key in self._store:
            value, exp = self._store[key]
            if now < exp:
                return value

        async def _compute():
            value = await compute_fn()
            ttl = self._jittered_ttl()
            self._store[key] = (value, time.monotonic() + ttl)
            return value

        return await self._sf.do(key, _compute)

    def warm_keys(self, keys: list[str], values: dict[str, Any]) -> None:
        """Pre-populate keys with jittered TTLs to spread expiry over time."""
        for key in keys:
            if key in values:
                self._store[key] = (values[key], time.monotonic() + self._jittered_ttl())
```

---

## Comparison

| Solution | Mechanism | Concurrent Misses | Stale Serving | Distributed | Best For |
|---|---|---|---|---|---|
| XFetch Probabilistic | Early probabilistic refresh | 1 recomputes early | No | No | Expiry-driven stampedes |
| Single-Flight | Deduplicate concurrent misses | 1 computes, rest wait | No | No | High concurrency, same key |
| Stale-While-Revalidate | Background refresh | 1 background refresh | Yes | No | Low latency tolerance |
| Redis Distributed Lock | SET NX mutex | 1 computes, rest poll | No | Yes | Multi-process agents |
| Layered Cache | L1+L2 hierarchy | Per-layer dedup | No | Yes | Production multi-instance |
| Jittered TTL | TTL spread | Reduces simultaneous expiry | No | No | Batch-populated caches |

**Use single-flight** as the default for any async in-process cache — it's the simplest and most effective stampede prevention. **Add stale-while-revalidate** when fresh data is preferable but latency spikes are unacceptable. **Add XFetch** on top of TTL-based caches when load spikes at expiry time are observed in production. **Use Redis distributed lock** for multi-process deployments. **Always jitter TTLs** when pre-warming or bulk-populating caches to prevent synchronized mass expiry.
