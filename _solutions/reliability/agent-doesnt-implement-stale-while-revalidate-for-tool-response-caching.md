---
title: "Agent Doesn't Implement Stale-While-Revalidate for Tool Response Caching"
description: "AI agents that use simple TTL caches either serve stale data after expiry (high latency on first post-expiry request) or block every caller during revalidation (thundering herd). The stale-while-revalidate pattern serves the cached value immediately — even if stale — while triggering a background refresh, eliminating both the latency spike and the stampede."
date: 2025-02-12
difficulty: intermediate
category: reliability
slug: agent-doesnt-implement-stale-while-revalidate-for-tool-response-caching
tags:
  - stale-while-revalidate
  - caching
  - background-refresh
  - thundering-herd
  - ttl
  - tool-response
  - latency
symptoms:
  - "First request after cache expiry blocks all concurrent callers while re-fetching"
  - "Cache miss causes a 2s latency spike on an otherwise sub-100ms tool call"
  - "Thundering herd: 50 concurrent requests all miss the same expired cache entry simultaneously"
  - "Agent either serves outdated data too long (TTL too high) or re-fetches too often (TTL too low)"
  - "No way to serve a cached value while a refresh is in progress"
---

## Problem

A simple TTL cache blocks on miss: when a key expires, the next caller waits for the full fetch latency before receiving a response. All concurrent callers pile up (thundering herd). The stale-while-revalidate (SWR) pattern — popularised by RFC 5861 and HTTP caching — serves the last-known value immediately if it exists (even if stale), and triggers an async refresh in the background. Only the very first cold miss blocks; every subsequent call gets instant response while the cache stays fresh.

---

## Solution 1: SWRCache — Core Stale-While-Revalidate Cache

```python
import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Generic, Optional, TypeVar

K = TypeVar("K")
V = TypeVar("V")


@dataclass
class CacheEntry:
    value: Any
    fetched_at: float
    ttl: float                        # serve fresh until fetched_at + ttl
    stale_ttl: float                  # serve stale until fetched_at + stale_ttl
    refreshing: bool = False

    def is_fresh(self) -> bool:
        return time.monotonic() < self.fetched_at + self.ttl

    def is_usable(self) -> bool:
        return time.monotonic() < self.fetched_at + self.stale_ttl


class SWRCache:
    """
    Stale-While-Revalidate cache.
    - Fresh (< ttl): serve immediately, no refresh.
    - Stale (ttl < age < stale_ttl): serve immediately, refresh in background.
    - Expired (age > stale_ttl): block until refreshed.

    Usage:
        cache = SWRCache(ttl=30, stale_ttl=300)

        async def get_weather(city: str) -> dict:
            return await cache.get(
                key=f"weather:{city}",
                fetch_fn=lambda: weather_api.fetch(city),
            )
    """

    def __init__(self, ttl: float = 60.0, stale_ttl: float = 600.0):
        self._ttl = ttl
        self._stale_ttl = stale_ttl
        self._entries: Dict[str, CacheEntry] = {}
        self._locks: Dict[str, asyncio.Lock] = {}
        self._global_lock = asyncio.Lock()

    async def _get_lock(self, key: str) -> asyncio.Lock:
        async with self._global_lock:
            if key not in self._locks:
                self._locks[key] = asyncio.Lock()
            return self._locks[key]

    async def get(self, key: str,
                  fetch_fn: Callable,
                  ttl: Optional[float] = None,
                  stale_ttl: Optional[float] = None) -> Any:
        _ttl = ttl or self._ttl
        _stale_ttl = stale_ttl or self._stale_ttl

        entry = self._entries.get(key)

        # Fresh hit: serve immediately
        if entry and entry.is_fresh():
            return entry.value

        # Stale hit: serve stale, kick off background refresh
        if entry and entry.is_usable():
            if not entry.refreshing:
                entry.refreshing = True
                asyncio.create_task(self._refresh(key, fetch_fn, _ttl, _stale_ttl))
            return entry.value

        # Cold miss or fully expired: block until fetched
        lock = await self._get_lock(key)
        async with lock:
            # Double-check after acquiring lock
            entry = self._entries.get(key)
            if entry and entry.is_usable():
                return entry.value
            value = await fetch_fn()
            self._entries[key] = CacheEntry(
                value=value,
                fetched_at=time.monotonic(),
                ttl=_ttl,
                stale_ttl=_stale_ttl,
            )
            return value

    async def _refresh(self, key: str, fetch_fn: Callable,
                        ttl: float, stale_ttl: float):
        try:
            value = await fetch_fn()
            self._entries[key] = CacheEntry(
                value=value, fetched_at=time.monotonic(),
                ttl=ttl, stale_ttl=stale_ttl,
            )
        except Exception:
            pass  # Keep stale value on refresh failure
        finally:
            entry = self._entries.get(key)
            if entry:
                entry.refreshing = False

    def invalidate(self, key: str):
        self._entries.pop(key, None)

    def stats(self) -> Dict[str, Any]:
        now = time.monotonic()
        fresh = sum(1 for e in self._entries.values() if e.is_fresh())
        stale = sum(1 for e in self._entries.values()
                    if not e.is_fresh() and e.is_usable())
        expired = len(self._entries) - fresh - stale
        return {"total": len(self._entries), "fresh": fresh,
                "stale": stale, "expired": expired}
```

---

## Solution 2: PerKeyTTLSWRCache — Per-Key TTL Configuration

Different tools have different freshness requirements. This cache allows per-key TTL overrides and uses consistent hashing for distributed deployments.

```python
import asyncio
import time
from typing import Any, Callable, Dict, NamedTuple, Optional


class TTLPolicy(NamedTuple):
    ttl: float
    stale_ttl: float
    min_refresh_interval: float = 5.0   # prevent refresh storms


class PerKeyTTLSWRCache:
    """
    SWR cache where each key (or key prefix) can have its own TTL policy.

    Usage:
        cache = PerKeyTTLSWRCache()
        cache.set_policy("weather:", TTLPolicy(ttl=300, stale_ttl=3600))
        cache.set_policy("stock:",   TTLPolicy(ttl=5,   stale_ttl=30))
        cache.set_policy("wiki:",    TTLPolicy(ttl=86400, stale_ttl=604800))

        result = await cache.get("weather:london", fetch_weather_london)
    """

    DEFAULT_POLICY = TTLPolicy(ttl=60, stale_ttl=600)

    def __init__(self):
        self._policies: Dict[str, TTLPolicy] = {}
        self._cache = SWRCache()
        self._last_refresh: Dict[str, float] = {}

    def set_policy(self, prefix: str, policy: TTLPolicy):
        self._policies[prefix] = policy

    def _policy_for(self, key: str) -> TTLPolicy:
        for prefix, policy in self._policies.items():
            if key.startswith(prefix):
                return policy
        return self.DEFAULT_POLICY

    async def get(self, key: str, fetch_fn: Callable) -> Any:
        policy = self._policy_for(key)
        # Rate-limit refreshes
        last = self._last_refresh.get(key, 0)
        if time.monotonic() - last < policy.min_refresh_interval:
            entry = self._cache._entries.get(key)
            if entry:
                return entry.value
        self._last_refresh[key] = time.monotonic()
        return await self._cache.get(
            key, fetch_fn,
            ttl=policy.ttl,
            stale_ttl=policy.stale_ttl,
        )

    def invalidate(self, pattern: str):
        keys = [k for k in self._cache._entries if k.startswith(pattern)]
        for k in keys:
            self._cache.invalidate(k)
```

---

## Solution 3: ThunderherdGuard — Coalesce Concurrent Miss Requests

When multiple coroutines request the same missing key simultaneously, only one fetch executes; the rest wait and share the result.

```python
import asyncio
from typing import Any, Callable, Dict


class ThunderherdGuard:
    """
    Coalesces concurrent requests for the same missing key.
    Only the first coroutine fetches; others await the same future.

    Usage:
        guard = ThunderherdGuard()
        # 100 concurrent callers → exactly 1 fetch
        results = await asyncio.gather(*[
            guard.get_or_fetch("weather:NYC", fetch_weather)
            for _ in range(100)
        ])
    """

    def __init__(self):
        self._inflight: Dict[str, asyncio.Future] = {}
        self._lock = asyncio.Lock()

    async def get_or_fetch(self, key: str, fetch_fn: Callable) -> Any:
        async with self._lock:
            if key in self._inflight:
                fut = self._inflight[key]
                in_flight = True
            else:
                fut = asyncio.get_event_loop().create_future()
                self._inflight[key] = fut
                in_flight = False

        if in_flight:
            return await asyncio.shield(fut)

        try:
            value = await fetch_fn()
            fut.set_result(value)
            return value
        except Exception as exc:
            fut.set_exception(exc)
            raise
        finally:
            async with self._lock:
                self._inflight.pop(key, None)
```

---

## Solution 4: SWRToolCache — Decorator for Agent Tool Functions

Drop-in decorator that adds SWR caching to any async tool function.

```python
import asyncio
import functools
import hashlib
import json
import time
from typing import Any, Callable, Optional


def swr_cached(ttl: float = 60.0,
               stale_ttl: float = 600.0,
               key_fn: Optional[Callable] = None,
               cache: Optional[SWRCache] = None):
    """
    Decorator that adds stale-while-revalidate caching to an async tool function.
    Cache key is derived from the function name + arguments by default.

    Usage:
        _cache = SWRCache(ttl=30, stale_ttl=300)

        @swr_cached(ttl=30, stale_ttl=300, cache=_cache)
        async def fetch_stock_price(ticker: str) -> dict:
            return await market_api.get_price(ticker)
    """
    _cache = cache or SWRCache(ttl=ttl, stale_ttl=stale_ttl)

    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs) -> Any:
            if key_fn:
                key = key_fn(*args, **kwargs)
            else:
                raw = f"{fn.__qualname__}:{json.dumps(args, default=str)}:{json.dumps(kwargs, sort_keys=True, default=str)}"
                key = hashlib.sha256(raw.encode()).hexdigest()[:16]

            async def fetch():
                return await fn(*args, **kwargs)

            return await _cache.get(key, fetch, ttl=ttl, stale_ttl=stale_ttl)

        wrapper.cache = _cache
        wrapper.invalidate = lambda *a, **kw: _cache.invalidate(
            key_fn(*a, **kw) if key_fn else hashlib.sha256(
                f"{fn.__qualname__}:{json.dumps(a, default=str)}".encode()
            ).hexdigest()[:16]
        )
        return wrapper
    return decorator
```

---

## Solution 5: TieredSWRCache — L1 (in-process) + L2 (Redis) SWR

Two-tier SWR cache: L1 is an in-process dict (microsecond latency), L2 is Redis (millisecond latency). SWR semantics apply at both tiers.

```python
import asyncio
import json
import time
from typing import Any, Callable, Optional


class TieredSWRCache:
    """
    Two-tier SWR cache: L1 in-process dict, L2 Redis.
    On L1 stale: return L1 value, trigger L2 check in background.
    On L1 miss: check L2, populate L1, trigger background refresh if L2 stale.
    On L2 miss: fetch from source, populate both tiers.

    Usage:
        import redis.asyncio as aioredis
        redis = aioredis.from_url("redis://localhost")
        cache = TieredSWRCache(redis, l1_ttl=5, l2_ttl=60, l2_stale_ttl=600)

        result = await cache.get("search:python+asyncio", search_fn)
    """

    def __init__(self, redis_client,
                 l1_ttl: float = 5.0,
                 l2_ttl: float = 60.0,
                 l2_stale_ttl: float = 600.0):
        self._l1 = SWRCache(ttl=l1_ttl, stale_ttl=l1_ttl * 10)
        self._redis = redis_client
        self._l2_ttl = l2_ttl
        self._l2_stale = l2_stale_ttl

    async def _l2_get(self, key: str) -> Optional[Any]:
        raw = await self._redis.get(f"swr:{key}")
        if raw is None:
            return None
        data = json.loads(raw)
        age = time.time() - data["fetched_at"]
        if age > self._l2_stale:
            return None
        return data["value"]

    async def _l2_set(self, key: str, value: Any):
        data = json.dumps({"value": value, "fetched_at": time.time()})
        await self._redis.setex(f"swr:{key}", int(self._l2_stale), data)

    async def get(self, key: str, fetch_fn: Callable) -> Any:
        async def l2_fetch():
            value = await self._l2_get(key)
            if value is not None:
                return value
            value = await fetch_fn()
            await self._l2_set(key, value)
            return value

        return await self._l1.get(key, l2_fetch)

    def invalidate(self, key: str):
        self._l1.invalidate(key)
        asyncio.create_task(self._redis.delete(f"swr:{key}"))
```

---

## Solution 6: SWRMetricsDashboard — Cache Health Monitoring

Track hit rates, stale serve rates, and background refresh durations to monitor SWR cache health.

```python
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Deque, Dict


@dataclass
class CacheHitRecord:
    key: str
    hit_type: str   # "fresh" | "stale" | "miss"
    timestamp: float


class SWRMetricsDashboard:
    """
    Collects and reports SWR cache metrics.
    Attach to SWRCache by monkey-patching the get() method or subclassing.

    Usage:
        metrics = SWRMetricsDashboard()
        cache = SWRCache(ttl=30, stale_ttl=300)

        # Instrument:
        original_get = cache.get
        async def instrumented_get(key, fetch_fn, **kw):
            entry = cache._entries.get(key)
            result = await original_get(key, fetch_fn, **kw)
            hit_type = "fresh" if (entry and entry.is_fresh()) else \
                       "stale" if (entry and entry.is_usable()) else "miss"
            metrics.record(key, hit_type)
            return result
        cache.get = instrumented_get

        print(metrics.report())
    """

    def __init__(self, window_s: float = 300.0):
        self._window = window_s
        self._records: Deque[CacheHitRecord] = deque(maxlen=10_000)

    def record(self, key: str, hit_type: str):
        self._records.append(CacheHitRecord(key, hit_type, time.monotonic()))

    def report(self) -> Dict:
        now = time.monotonic()
        recent = [r for r in self._records if now - r.timestamp < self._window]
        if not recent:
            return {}
        counts: Dict[str, int] = defaultdict(int)
        for r in recent:
            counts[r.hit_type] += 1
        total = len(recent)
        return {
            "window_s": self._window,
            "total_requests": total,
            "fresh_hits": counts["fresh"],
            "stale_hits": counts["stale"],
            "misses": counts["miss"],
            "fresh_rate": round(counts["fresh"] / total, 3),
            "stale_serve_rate": round(counts["stale"] / total, 3),
            "miss_rate": round(counts["miss"] / total, 3),
        }
```

---

## Comparison

| Approach | Eliminates Thundering Herd | Background Refresh | Per-Key TTL | Distributed | Metrics |
|---|---|---|---|---|---|
| **SWRCache** | Partial (lock per key) | Yes | No | No | No |
| **PerKeyTTLSWRCache** | Partial | Yes | Yes | No | No |
| **ThunderherdGuard** | Full (future coalescing) | No | No | No | No |
| **SWRToolCache (decorator)** | Partial | Yes | Yes | No | No |
| **TieredSWRCache** | Partial | Yes | No | Yes (Redis) | No |
| **SWRMetricsDashboard** | N/A | N/A | N/A | N/A | Yes |

**Key insight**: combine `ThunderherdGuard` with `SWRCache` for maximum robustness — the guard ensures only one fetch runs for a cold miss, while SWR ensures all subsequent callers get immediate responses even when the data is slightly stale. Set `stale_ttl` to 5–10× `ttl`; the worst case is a background refresh that fails, after which the stale value continues to be served until `stale_ttl` expires.
