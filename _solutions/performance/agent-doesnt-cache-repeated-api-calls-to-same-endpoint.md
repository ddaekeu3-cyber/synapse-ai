---
layout: solution
title: "Agent Makes Identical API Calls Repeatedly — No Response Cache"
category: performance
description: "Agent fetches the same GitHub user profile 20 times in one session. Or calls the same product lookup endpoint for the same SKU on every tool invocation. Each call takes 200ms and costs money. The data hasn't changed. The agent has no cache."
tags: [performance, caching, api-calls, deduplication, ttl, redis, in-process, cost, latency, memoization]
---

# Agent Makes Identical API Calls Repeatedly — No Response Cache

## Symptom
- Agent calls `GET /users/42` 15 times in one session — data doesn't change between calls
- Same product lookup repeated for every message in a multi-turn conversation
- Agent fetches feature flags on every tool invocation — 100 calls/minute to the same endpoint
- Exchange rate API called 50 times/hour — rate returns the same value for 30 minutes
- Agent re-fetches configuration on every request — config changes once per deploy
- Multiple parallel tool calls all fetch the same resource simultaneously

## Root Cause
Agents call tools without memory of what they've already fetched. Each tool invocation is stateless — the agent doesn't track "I already have this data." Without a response cache, every API call hits the network regardless of whether the data was recently fetched. The fix is to add caching at the tool layer — the agent calls the tool normally, but the tool implementation returns a cached response when available.

## Fix

### Option 1: In-process TTL cache — memoize tool responses

```python
import time
import hashlib
import json
from functools import wraps
from typing import Any, Callable, Optional

class TTLCache:
    """
    Simple in-process TTL cache for API responses.
    Thread-safe for single-process agents.
    """

    def __init__(self):
        self._store: dict[str, tuple[Any, float]] = {}  # key → (value, expires_at)
        self._hits = 0
        self._misses = 0

    def get(self, key: str) -> tuple[bool, Any]:
        """Returns (hit, value)"""
        if key in self._store:
            value, expires_at = self._store[key]
            if time.time() < expires_at:
                self._hits += 1
                return True, value
            else:
                del self._store[key]
        self._misses += 1
        return False, None

    def set(self, key: str, value: Any, ttl_seconds: float):
        self._store[key] = (value, time.time() + ttl_seconds)

    def invalidate(self, key: str):
        self._store.pop(key, None)

    def invalidate_prefix(self, prefix: str):
        """Remove all keys starting with prefix"""
        to_remove = [k for k in self._store if k.startswith(prefix)]
        for k in to_remove:
            del self._store[k]

    def clear_expired(self):
        now = time.time()
        self._store = {k: v for k, v in self._store.items() if v[1] > now}

    @property
    def stats(self) -> dict:
        total = self._hits + self._misses
        return {
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": f"{self._hits/total*100:.0f}%" if total > 0 else "0%",
            "cached_entries": len(self._store)
        }

_cache = TTLCache()

def cached_api_call(ttl_seconds: float = 60.0):
    """
    Decorator for tool functions — caches responses by arguments.
    Apply to any tool that fetches data that doesn't change frequently.
    """
    def decorator(fn: Callable) -> Callable:
        @wraps(fn)
        async def wrapper(*args, **kwargs):
            # Build cache key from function name + arguments
            key_data = {
                "fn": fn.__name__,
                "args": str(args),
                "kwargs": json.dumps(kwargs, sort_keys=True, default=str)
            }
            cache_key = hashlib.sha256(json.dumps(key_data).encode()).hexdigest()

            hit, cached_value = _cache.get(cache_key)
            if hit:
                print(f"Cache hit: {fn.__name__}({args}, {kwargs}) → returning cached result")
                return cached_value

            result = await fn(*args, **kwargs)
            _cache.set(cache_key, result, ttl_seconds)
            print(f"Cache miss: {fn.__name__} → fetched and cached for {ttl_seconds}s")
            return result
        return wrapper
    return decorator

# Apply to frequently-called tools:
@cached_api_call(ttl_seconds=300.0)  # Cache for 5 minutes
async def get_user_profile(user_id: int) -> dict:
    import httpx
    async with httpx.AsyncClient() as client:
        response = await client.get(f"https://api.example.com/users/{user_id}", timeout=10.0)
        return response.json()

@cached_api_call(ttl_seconds=3600.0)  # Cache for 1 hour
async def get_product_details(sku: str) -> dict:
    import httpx
    async with httpx.AsyncClient() as client:
        response = await client.get(f"https://api.example.com/products/{sku}", timeout=10.0)
        return response.json()

@cached_api_call(ttl_seconds=1800.0)  # Cache for 30 minutes
async def get_exchange_rate(from_currency: str, to_currency: str) -> float:
    import httpx
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"https://api.exchangerate.example.com/rates",
            params={"from": from_currency, "to": to_currency},
            timeout=10.0
        )
        return response.json()["rate"]
```

### Option 2: Session-scoped request deduplication — deduplicate within one agent run

```python
import asyncio
from typing import Awaitable

class RequestDeduplicator:
    """
    Deduplicates in-flight requests — if the same call is made concurrently,
    only one network request goes out and all callers share the result.
    Prevents the "thundering herd" when parallel tools call the same endpoint.
    """

    def __init__(self):
        self._in_flight: dict[str, asyncio.Future] = {}
        self._completed: dict[str, Any] = {}  # Session-level cache (no TTL)

    async def get_or_fetch(
        self,
        key: str,
        fetch_fn: Callable[[], Awaitable[Any]]
    ) -> Any:
        """
        Get a value, deduplicating concurrent fetches for the same key.
        """
        # Already completed in this session
        if key in self._completed:
            return self._completed[key]

        # Already in flight — wait for the existing request
        if key in self._in_flight:
            print(f"Dedup: waiting for in-flight request for '{key}'")
            return await self._in_flight[key]

        # Start a new request
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._in_flight[key] = future

        try:
            result = await fetch_fn()
            self._completed[key] = result
            future.set_result(result)
            return result
        except Exception as e:
            future.set_exception(e)
            raise
        finally:
            self._in_flight.pop(key, None)

    def clear_session(self):
        """Call between agent sessions to clear session-scoped cache"""
        self._completed.clear()
        self._in_flight.clear()

deduplicator = RequestDeduplicator()

async def get_user_profile_deduped(user_id: int) -> dict:
    """Fetch user profile — deduplicated across concurrent calls"""
    key = f"user_profile:{user_id}"
    return await deduplicator.get_or_fetch(
        key,
        fetch_fn=lambda: _fetch_user_profile_from_api(user_id)
    )

async def _fetch_user_profile_from_api(user_id: int) -> dict:
    import httpx
    async with httpx.AsyncClient() as client:
        response = await client.get(f"https://api.example.com/users/{user_id}", timeout=10.0)
        return response.json()
```

### Option 3: Redis cache for distributed agents — share cache across instances

```python
import redis
import json
import hashlib
import os
from typing import Any, Optional

class RedisAPICache:
    """
    Redis-backed API response cache — shared across multiple agent instances.
    Perfect for horizontally-scaled agents hitting the same upstream APIs.
    """

    def __init__(
        self,
        redis_url: str = None,
        key_prefix: str = "agent:api:"
    ):
        self.r = redis.Redis.from_url(
            redis_url or os.getenv("REDIS_URL", "redis://localhost:6379"),
            decode_responses=True
        )
        self.prefix = key_prefix

    def _cache_key(self, endpoint: str, params: dict) -> str:
        canonical = json.dumps({"endpoint": endpoint, "params": params}, sort_keys=True)
        return self.prefix + hashlib.sha256(canonical.encode()).hexdigest()[:24]

    def get(self, endpoint: str, params: dict = None) -> Optional[Any]:
        key = self._cache_key(endpoint, params or {})
        raw = self.r.get(key)
        if raw:
            return json.loads(raw)
        return None

    def set(self, endpoint: str, params: dict = None, value: Any = None, ttl_seconds: int = 300):
        key = self._cache_key(endpoint, params or {})
        self.r.setex(key, ttl_seconds, json.dumps(value))

    def delete(self, endpoint: str, params: dict = None):
        key = self._cache_key(endpoint, params or {})
        self.r.delete(key)

redis_cache = RedisAPICache()

async def cached_get(
    endpoint: str,
    params: dict = None,
    ttl_seconds: int = 300,
    force_refresh: bool = False
) -> Any:
    """
    GET request with Redis caching.
    Use force_refresh=True to bypass cache when data must be fresh.
    """
    import httpx

    if not force_refresh:
        cached = redis_cache.get(endpoint, params)
        if cached is not None:
            print(f"Redis cache hit: {endpoint}")
            return cached

    async with httpx.AsyncClient() as client:
        response = await client.get(endpoint, params=params, timeout=15.0)
        response.raise_for_status()
        data = response.json()

    redis_cache.set(endpoint, params, data, ttl_seconds=ttl_seconds)
    print(f"Redis cache set: {endpoint} (TTL={ttl_seconds}s)")
    return data
```

### Option 4: Cache-aside pattern with stale-while-revalidate

```python
import asyncio
import time
from typing import Any, Optional, Callable, Awaitable

class StaleWhileRevalidateCache:
    """
    Serves stale data immediately while fetching fresh data in the background.
    Eliminates latency spikes on cache expiry — always returns quickly.
    """

    def __init__(self):
        self._store: dict[str, dict] = {}

    def _entry(self, key: str) -> Optional[dict]:
        return self._store.get(key)

    def is_fresh(self, key: str, ttl: float) -> bool:
        entry = self._entry(key)
        return entry is not None and (time.time() - entry["fetched_at"]) < ttl

    def is_stale(self, key: str, stale_ttl: float) -> bool:
        """Stale but still usable (within stale window)"""
        entry = self._entry(key)
        return entry is not None and (time.time() - entry["fetched_at"]) < stale_ttl

    async def get(
        self,
        key: str,
        fetch_fn: Callable[[], Awaitable[Any]],
        ttl: float = 60.0,
        stale_ttl: float = 300.0  # Serve stale for up to 5 minutes
    ) -> Any:
        """
        Return data from cache.
        - If fresh (< ttl): return immediately
        - If stale (ttl < age < stale_ttl): return stale data, refresh in background
        - If expired (> stale_ttl): fetch synchronously
        """
        entry = self._entry(key)

        if entry is None:
            # Never fetched — fetch synchronously
            return await self._fetch_and_store(key, fetch_fn)

        age = time.time() - entry["fetched_at"]

        if age < ttl:
            return entry["data"]  # Fresh — return immediately

        if age < stale_ttl:
            # Stale but usable — return immediately, refresh in background
            if not entry.get("refreshing"):
                entry["refreshing"] = True
                asyncio.create_task(self._background_refresh(key, fetch_fn))
                print(f"Serving stale data for '{key}' (age={age:.0f}s), refreshing in background")
            return entry["data"]

        # Expired — must fetch synchronously
        return await self._fetch_and_store(key, fetch_fn)

    async def _fetch_and_store(self, key: str, fetch_fn: Callable) -> Any:
        data = await fetch_fn()
        self._store[key] = {"data": data, "fetched_at": time.time(), "refreshing": False}
        return data

    async def _background_refresh(self, key: str, fetch_fn: Callable):
        try:
            data = await fetch_fn()
            self._store[key] = {"data": data, "fetched_at": time.time(), "refreshing": False}
            print(f"Background refresh complete for '{key}'")
        except Exception as e:
            print(f"Background refresh failed for '{key}': {e}")
            if key in self._store:
                self._store[key]["refreshing"] = False

swr_cache = StaleWhileRevalidateCache()

async def get_config_swr(config_key: str) -> dict:
    """Get config with stale-while-revalidate — never slow, always eventually fresh"""
    return await swr_cache.get(
        key=f"config:{config_key}",
        fetch_fn=lambda: _fetch_config_from_api(config_key),
        ttl=60.0,       # Treat as fresh for 1 minute
        stale_ttl=600.0 # Accept stale data up to 10 minutes old
    )

async def _fetch_config_from_api(key: str) -> dict:
    import httpx
    async with httpx.AsyncClient() as client:
        r = await client.get(f"https://config.example.com/{key}", timeout=5.0)
        return r.json()
```

### Option 5: Batch cache lookup — fetch multiple items in one call

```python
from typing import Optional

class BatchCache:
    """
    Batch-aware cache: check which items are missing, fetch only those,
    then fill the cache. Minimizes API round trips for bulk lookups.
    """

    def __init__(self, ttl_seconds: float = 300.0):
        self._store: dict[str, tuple[Any, float]] = {}
        self.ttl = ttl_seconds

    def get_many(self, keys: list[str]) -> tuple[dict[str, Any], list[str]]:
        """
        Batch cache lookup.
        Returns (hits: dict, misses: list of keys not in cache)
        """
        hits = {}
        misses = []
        now = time.time()

        for key in keys:
            if key in self._store:
                value, expires_at = self._store[key]
                if now < expires_at:
                    hits[key] = value
                    continue
                else:
                    del self._store[key]
            misses.append(key)

        return hits, misses

    def set_many(self, items: dict[str, Any]):
        """Cache multiple items at once"""
        expires_at = time.time() + self.ttl
        for key, value in items.items():
            self._store[key] = (value, expires_at)

batch_cache = BatchCache(ttl_seconds=300.0)

async def get_users_batch(user_ids: list[int]) -> dict[int, dict]:
    """
    Fetch multiple user profiles — only calls API for cache misses.
    Single batch API call for all misses.
    """
    str_keys = [f"user:{uid}" for uid in user_ids]
    hits, miss_keys = batch_cache.get_many(str_keys)

    # Convert hits back to int-keyed dict
    results = {int(k.split(":")[1]): v for k, v in hits.items()}

    if miss_keys:
        miss_ids = [int(k.split(":")[1]) for k in miss_keys]
        print(f"Cache: {len(hits)} hits, {len(miss_ids)} misses — fetching batch")

        # Fetch all misses in one API call (if API supports batch)
        import httpx
        async with httpx.AsyncClient() as client:
            response = await client.get(
                "https://api.example.com/users/batch",
                params={"ids": ",".join(str(i) for i in miss_ids)},
                timeout=15.0
            )
        batch_data = {u["id"]: u for u in response.json()}

        # Cache the fetched results
        batch_cache.set_many({f"user:{uid}": data for uid, data in batch_data.items()})
        results.update(batch_data)

    return results
```

### Option 6: Cache TTL reference table by data type

```python
# Reference: appropriate TTLs by data freshness requirements

RECOMMENDED_TTL_SECONDS = {
    # Static/rarely changing data — long TTL
    "product_catalog": 3600,          # 1 hour — products don't change often
    "user_profile": 300,              # 5 minutes — profile changes occasionally
    "configuration": 600,             # 10 minutes — config changes on deploy
    "feature_flags": 60,              # 1 minute — flags may change during rollout
    "permissions": 120,               # 2 minutes — permissions change rarely

    # Dynamic data — short TTL
    "exchange_rates": 1800,           # 30 minutes — rates update hourly
    "stock_price": 15,                # 15 seconds — real-time data
    "weather": 900,                   # 15 minutes — changes slowly
    "inventory_count": 30,            # 30 seconds — changes frequently

    # Computed/aggregated data
    "dashboard_stats": 300,           # 5 minutes — recomputed periodically
    "search_results": 60,             # 1 minute — freshness matters
    "recommendation": 3600,           # 1 hour — ML inference is expensive

    # Don't cache
    "payment_status": 0,              # Always fetch fresh — financial data
    "auth_token_valid": 0,            # Always validate — security critical
    "otp_verification": 0,            # Never cache — one-time use
}

def get_recommended_ttl(data_type: str) -> int:
    """Get recommended TTL for a data type — use as default in tool wrappers"""
    return RECOMMENDED_TTL_SECONDS.get(data_type, 60)  # Default 60s if unknown
```

## Cache Strategy Comparison

| Strategy | Latency | Consistency | Infrastructure | Best For |
|---|---|---|---|---|
| In-process TTL | 0ms | Per-instance | None | Single-instance agents |
| Session dedup | 0ms | Per-session | None | Parallel tool calls |
| Redis shared | 1-3ms | Cross-instance | Redis | Distributed agents |
| Stale-while-revalidate | 0ms | Eventually fresh | In-memory | Low-latency requirements |
| Batch lookup | Varies | Fresh on miss | None | Bulk data access |

## Expected Token Savings
Agent calls same endpoint 50× in one session (each call visible in tool results): wastes tokens describing repeated fetches
Cached: 1 actual call, 49 instant returns — response fits in context once instead of 50×: significant context savings plus 98% latency reduction

## Environment
- Any agent that calls external APIs within a session or across multiple sessions; critical for agents that fetch user data, product catalogs, configuration, exchange rates, or any relatively-static data — caching is the highest-ROI performance optimization after model routing
- Source: direct experience; missing API response caching is the most common performance bottleneck in production agents, always discovered in the first week after launch
