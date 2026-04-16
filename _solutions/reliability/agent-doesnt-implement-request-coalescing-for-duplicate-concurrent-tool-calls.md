---
title: "Agent Doesn't Implement Request Coalescing for Duplicate Concurrent Tool Calls"
description: "Agents handling concurrent sessions that trigger the same tool call simultaneously — fetching the same URL, querying the same database row, or calling the same API endpoint — dispatch N identical requests instead of one. Implement request coalescing that deduplicates in-flight calls by cache key so that N callers waiting for the same result share a single upstream request and all receive the response when it resolves."
date: 2026-04-16
difficulty: advanced
category: reliability
slug: agent-doesnt-implement-request-coalescing-for-duplicate-concurrent-tool-calls
tags: [request-coalescing, deduplication, concurrent-sessions, thundering-herd, in-flight-dedup, upstream-load]
symptoms:
  - "Same API endpoint called N times simultaneously for N concurrent sessions with identical parameters"
  - "Cache miss storm on startup when many sessions request the same cold resource simultaneously"
  - "Upstream rate limit errors triggered by burst of identical requests from concurrent agent sessions"
  - "No mechanism to detect that two in-flight tool calls are logically equivalent"
  - "Tool call latency degrades under load because upstream is saturated with duplicate requests"
---

## Why This Happens

Each agent session dispatches tool calls independently. When ten sessions simultaneously need the same stock price, weather data, or configuration value, ten identical HTTP requests go out at the same moment. The upstream service receives a burst, may rate-limit, and each caller waits independently. Request coalescing recognizes that multiple callers waiting for logically identical results can share one in-flight request: the first caller dispatches, the rest subscribe to its future, and all receive the same response when it arrives. This collapses N upstream requests into 1 for the duration of any concurrent burst.

## Solution 1: Coalescing Key Generator

```python
import hashlib
import json
from typing import Any, Dict


class CoalescingKeyGenerator:
    """
    Produces a stable, deterministic key for a tool call based on
    tool name and arguments. Two calls with identical tool+args
    produce the same key and will be coalesced.
    """

    @staticmethod
    def generate(tool_name: str, args: Dict[str, Any]) -> str:
        canonical = json.dumps({"tool": tool_name, "args": args}, sort_keys=True)
        return hashlib.sha256(canonical.encode()).hexdigest()[:32]
```

## Solution 2: In-Flight Request Registry

```python
import asyncio
from typing import Any, Dict, Optional


class InFlightRequestRegistry:
    """
    Tracks futures for in-flight tool calls by coalescing key.
    A second caller for the same key receives the existing future
    rather than triggering a new upstream request.
    """

    def __init__(self):
        self._in_flight: Dict[str, asyncio.Future] = {}
        self._lock = asyncio.Lock()
        self._total_requests = 0
        self._coalesced_requests = 0

    async def get_or_register(
        self, key: str
    ) -> tuple[Optional[asyncio.Future], bool]:
        """
        Returns (future, is_new).
        is_new=True  → caller should execute the request and resolve the future.
        is_new=False → caller should await the returned future (already in-flight).
        """
        async with self._lock:
            self._total_requests += 1
            if key in self._in_flight:
                self._coalesced_requests += 1
                return self._in_flight[key], False
            future: asyncio.Future = asyncio.get_event_loop().create_future()
            self._in_flight[key] = future
            return future, True

    async def resolve(self, key: str, result: Any) -> None:
        async with self._lock:
            future = self._in_flight.pop(key, None)
        if future and not future.done():
            future.set_result(result)

    async def reject(self, key: str, exc: Exception) -> None:
        async with self._lock:
            future = self._in_flight.pop(key, None)
        if future and not future.done():
            future.set_exception(exc)

    def stats(self) -> dict:
        return {
            "total_requests": self._total_requests,
            "coalesced_requests": self._coalesced_requests,
            "coalescing_rate": round(
                self._coalesced_requests / max(self._total_requests, 1), 4
            ),
            "currently_in_flight": len(self._in_flight),
        }
```

## Solution 3: Coalescing Tool Dispatcher

```python
import asyncio
from typing import Any, Callable, Dict


class CoalescingToolDispatcher:
    """
    Wraps any async tool call with coalescing. Concurrent callers
    with identical tool+args share one in-flight request; the result
    is broadcast to all waiters when it resolves.
    """

    def __init__(
        self,
        key_gen: CoalescingKeyGenerator,
        registry: InFlightRequestRegistry,
    ):
        self._key_gen = key_gen
        self._registry = registry

    async def dispatch(
        self,
        tool_name: str,
        args: Dict[str, Any],
        tool_fn: Callable,
    ) -> Any:
        key = self._key_gen.generate(tool_name, args)
        future, is_new = await self._registry.get_or_register(key)

        if is_new:
            try:
                result = await tool_fn(**args)
                await self._registry.resolve(key, result)
                return result
            except Exception as exc:
                await self._registry.reject(key, exc)
                raise
        else:
            return await future
```

## Solution 4: Coalescing Result Cache

```python
import asyncio
import time
from typing import Any, Dict, Optional, Tuple


class CoalescingResultCache:
    """
    Short-lived result cache that holds coalesced responses for a
    configurable TTL. Prevents re-coalescing storms when the same
    key is requested immediately after a prior result was returned.
    """

    def __init__(self, ttl_seconds: float = 5.0, max_entries: int = 1000):
        self._ttl = ttl_seconds
        self._max = max_entries
        self._store: Dict[str, Tuple[Any, float]] = {}
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> Optional[Any]:
        async with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            value, expires_at = entry
            if time.time() > expires_at:
                del self._store[key]
                return None
            return value

    async def set(self, key: str, value: Any) -> None:
        async with self._lock:
            if len(self._store) >= self._max:
                # evict oldest
                oldest_key = min(self._store, key=lambda k: self._store[k][1])
                del self._store[oldest_key]
            self._store[key] = (value, time.time() + self._ttl)

    async def size(self) -> int:
        async with self._lock:
            return len(self._store)
```

## Solution 5: Cache-Backed Coalescing Dispatcher

```python
import asyncio
from typing import Any, Callable, Dict


class CacheBackedCoalescingDispatcher:
    """
    Combines short-lived result cache with in-flight coalescing.
    Order: cache hit → coalesce → dispatch → cache result.
    """

    def __init__(
        self,
        key_gen: CoalescingKeyGenerator,
        registry: InFlightRequestRegistry,
        cache: CoalescingResultCache,
    ):
        self._key_gen = key_gen
        self._registry = registry
        self._cache = cache
        self._cache_hits = 0
        self._dispatches = 0

    async def dispatch(
        self,
        tool_name: str,
        args: Dict[str, Any],
        tool_fn: Callable,
        cacheable: bool = True,
    ) -> Any:
        key = self._key_gen.generate(tool_name, args)
        self._dispatches += 1

        if cacheable:
            cached = await self._cache.get(key)
            if cached is not None:
                self._cache_hits += 1
                return cached

        future, is_new = await self._registry.get_or_register(key)

        if is_new:
            try:
                result = await tool_fn(**args)
                await self._registry.resolve(key, result)
                if cacheable:
                    await self._cache.set(key, result)
                return result
            except Exception as exc:
                await self._registry.reject(key, exc)
                raise
        else:
            return await future

    def stats(self) -> dict:
        return {
            "total_dispatches": self._dispatches,
            "cache_hits": self._cache_hits,
            "registry_stats": self._registry.stats(),
        }
```

## Solution 6: Coalescing Dashboard

```python
import asyncio
import time


class RequestCoalescingDashboard:
    """
    Combines registry stats, cache stats, and dispatcher stats
    into a single operational snapshot.
    """

    def __init__(
        self,
        dispatcher: CacheBackedCoalescingDispatcher,
        cache: CoalescingResultCache,
    ):
        self._dispatcher = dispatcher
        self._cache = cache

    async def render(self) -> dict:
        cache_size = await self._cache.size()
        stats = self._dispatcher.stats()
        return {
            "generated_at": time.time(),
            "dispatcher": stats,
            "cache": {"current_entries": cache_size},
            "efficiency": {
                "upstream_reduction_rate": stats["registry_stats"]["coalescing_rate"],
                "cache_hit_rate": round(
                    stats["cache_hits"] / max(stats["total_dispatches"], 1), 4
                ),
            },
        }
```

## Comparison

| Approach | In-Flight Dedup | Short-TTL Cache | Broadcast to Waiters | Cache+Coalesce | Dashboard |
|---|---|---|---|---|---|
| InFlightRequestRegistry | Yes | No | Yes (Future) | No | No |
| CoalescingToolDispatcher | Via registry | No | Via registry | No | No |
| CoalescingResultCache | No | Yes (TTL) | No | No | No |
| CacheBackedCoalescingDispatcher | Via registry | Via cache | Via registry | Yes | No |
| RequestCoalescingDashboard | No | No | No | No | Yes |

**Best for production**: Apply coalescing to read-only, idempotent tool calls only — never coalesce mutating calls (POST, DELETE, state-changing operations) because two callers wanting to create a resource should not share a single creation. Set `ttl_seconds=5` in `CoalescingResultCache` to absorb burst storms without serving stale data for longer than a request cycle. Monitor `upstream_reduction_rate`: a rate above 0.30 in steady state means concurrent sessions are heavily overlapping and a shared cache layer (Redis) would be more appropriate than per-instance coalescing.
