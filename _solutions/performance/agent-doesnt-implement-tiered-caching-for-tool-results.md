---
title: "Agent Doesn't Implement Tiered Caching for Tool Results"
description: "Agents with a single cache layer or no cache at all pay full tool latency on every call, even for deterministic lookups with identical arguments. Implement tiered caching with an in-process L1 cache (microsecond access), a shared in-memory L2 cache (millisecond access), and an optional persistent L3 cache, with per-tier TTLs, eviction policies, and hit rate tracking per tier."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-tiered-caching-for-tool-results
tags: [tiered-caching, l1-l2-cache, tool-result-cache, cache-hit-rate, eviction-policy, cache-warming]
symptoms:
  - "Same database lookup fires on every agent turn even when the result cannot have changed"
  - "Cache exists but is process-local — identical requests from parallel workers all miss"
  - "No TTL differentiation — static data (user profile) and live data (stock price) share the same TTL"
  - "Cache hit rate is unmeasured — no way to tell if the cache is effective"
  - "Cache misses do not promote results to faster tiers for subsequent hits"
---

## Why This Happens

A single cache layer conflates data with different staleness tolerances. User profiles change rarely and can be cached for hours; live prices change every second and should be cached for under a minute. A single TTL set to the conservative minimum wastes the cache for stable data; set to the maximum, it serves stale live data. Tiered caching separates the concerns: L1 is a tiny in-process dict for the hottest keys (sub-millisecond, no network); L2 is a larger shared store for same-datacenter workers; L3 is a persistent store for warm-start across process restarts. On a miss at L1, the result is fetched from L2 and promoted back to L1 for the next access.

## Solution 1: Cache Entry

```python
import time
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class CacheEntry:
    value: Any
    stored_at: float = field(default_factory=time.time)
    ttl_seconds: float = 60.0
    tier: str = "l1"
    access_count: int = 0

    def is_expired(self) -> bool:
        return time.time() - self.stored_at > self.ttl_seconds

    def touch(self) -> None:
        self.access_count += 1

    def age_seconds(self) -> float:
        return round(time.time() - self.stored_at, 3)
```

## Solution 2: L1 In-Process Cache

```python
import threading
import time
from collections import OrderedDict
from typing import Any, Optional


class L1InProcessCache:
    """
    LRU in-process cache with configurable max_entries and per-entry TTL.
    Thread-safe. Evicts expired entries on access and when capacity is exceeded.
    """

    def __init__(self, max_entries: int = 512, default_ttl_seconds: float = 60.0):
        self._max = max_entries
        self._default_ttl = default_ttl_seconds
        self._store: OrderedDict = OrderedDict()
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0

    def get(self, key: str) -> Optional[Any]:
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                self._misses += 1
                return None
            if entry.is_expired():
                del self._store[key]
                self._misses += 1
                return None
            # LRU: move to end
            self._store.move_to_end(key)
            entry.touch()
            self._hits += 1
            return entry.value

    def put(self, key: str, value: Any, ttl_seconds: Optional[float] = None) -> None:
        with self._lock:
            ttl = ttl_seconds if ttl_seconds is not None else self._default_ttl
            self._store[key] = CacheEntry(value=value, ttl_seconds=ttl, tier="l1")
            self._store.move_to_end(key)
            while len(self._store) > self._max:
                self._store.popitem(last=False)  # evict LRU

    def invalidate(self, key: str) -> None:
        with self._lock:
            self._store.pop(key, None)

    def stats(self) -> dict:
        total = self._hits + self._misses
        return {
            "tier": "l1",
            "entries": len(self._store),
            "max_entries": self._max,
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": round(self._hits / max(total, 1), 4),
        }
```

## Solution 3: L2 Shared Memory Cache

```python
import json
import threading
import time
from typing import Any, Optional


class L2SharedMemoryCache:
    """
    Simulated shared in-memory cache (dict + lock).
    In production, replace the storage backend with Redis or Memcached
    while keeping this interface unchanged.
    """

    def __init__(self, max_entries: int = 10_000, default_ttl_seconds: float = 300.0):
        self._max = max_entries
        self._default_ttl = default_ttl_seconds
        self._store: dict = {}
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0

    def get(self, key: str) -> Optional[Any]:
        with self._lock:
            entry = self._store.get(key)
        if entry is None:
            self._misses += 1
            return None
        if entry.is_expired():
            with self._lock:
                self._store.pop(key, None)
            self._misses += 1
            return None
        entry.touch()
        self._hits += 1
        return entry.value

    def put(self, key: str, value: Any, ttl_seconds: Optional[float] = None) -> None:
        ttl = ttl_seconds if ttl_seconds is not None else self._default_ttl
        entry = CacheEntry(value=value, ttl_seconds=ttl, tier="l2")
        with self._lock:
            if len(self._store) >= self._max:
                # Evict a random expired entry or the oldest
                to_evict = next(
                    (k for k, v in self._store.items() if v.is_expired()),
                    next(iter(self._store), None),
                )
                if to_evict:
                    del self._store[to_evict]
            self._store[key] = entry

    def invalidate(self, key: str) -> None:
        with self._lock:
            self._store.pop(key, None)

    def stats(self) -> dict:
        total = self._hits + self._misses
        return {
            "tier": "l2",
            "entries": len(self._store),
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": round(self._hits / max(total, 1), 4),
        }
```

## Solution 4: Tiered Cache Manager

```python
import hashlib
import json
from typing import Any, Callable, Dict, List, Optional


class TieredCacheManager:
    """
    Orchestrates L1 → L2 lookups with automatic promotion on miss.
    On L1 miss: checks L2, promotes result to L1.
    On L2 miss: calls the source function, writes to both tiers.
    Per-tool TTL configuration overrides tier defaults.
    """

    def __init__(self, l1: L1InProcessCache, l2: L2SharedMemoryCache):
        self._l1 = l1
        self._l2 = l2
        self._ttl_config: Dict[str, dict] = {}   # tool_name -> {l1_ttl, l2_ttl}

    def configure_tool(
        self,
        tool_name: str,
        l1_ttl_seconds: float,
        l2_ttl_seconds: float,
    ) -> None:
        self._ttl_config[tool_name] = {
            "l1": l1_ttl_seconds,
            "l2": l2_ttl_seconds,
        }

    def _cache_key(self, tool_name: str, args: dict) -> str:
        payload = json.dumps({"tool": tool_name, "args": args}, sort_keys=True, default=str)
        return hashlib.sha256(payload.encode()).hexdigest()[:20]

    async def get_or_fetch(
        self,
        tool_name: str,
        args: dict,
        fetch_fn: Callable,
    ) -> tuple:  # (value, source: "l1"|"l2"|"fetch")
        key = self._cache_key(tool_name, args)
        cfg = self._ttl_config.get(tool_name, {})
        l1_ttl = cfg.get("l1", 60.0)
        l2_ttl = cfg.get("l2", 300.0)

        # L1 check
        value = self._l1.get(key)
        if value is not None:
            return value, "l1"

        # L2 check
        value = self._l2.get(key)
        if value is not None:
            self._l1.put(key, value, ttl_seconds=l1_ttl)   # promote
            return value, "l2"

        # Fetch from source
        value = await fetch_fn(**args)
        self._l2.put(key, value, ttl_seconds=l2_ttl)
        self._l1.put(key, value, ttl_seconds=l1_ttl)
        return value, "fetch"

    def invalidate(self, tool_name: str, args: dict) -> None:
        key = self._cache_key(tool_name, args)
        self._l1.invalidate(key)
        self._l2.invalidate(key)

    def all_stats(self) -> List[dict]:
        return [self._l1.stats(), self._l2.stats()]
```

## Solution 5: Cache Warming Scheduler

```python
import asyncio
from typing import Any, Callable, Dict, List


class CacheWarmingScheduler:
    """
    Pre-populates the cache for known high-frequency lookups at startup
    and on a recurring schedule. Prevents cold-cache latency spikes
    after process restarts or cache evictions.
    """

    def __init__(self, manager: TieredCacheManager):
        self._manager = manager
        self._warm_tasks: List[dict] = []

    def register_warm_task(
        self,
        tool_name: str,
        args: Dict[str, Any],
        fetch_fn: Callable,
        interval_seconds: float = 3600.0,
    ) -> None:
        self._warm_tasks.append({
            "tool_name": tool_name,
            "args": args,
            "fetch_fn": fetch_fn,
            "interval": interval_seconds,
        })

    async def warm_all(self) -> dict:
        results = {"warmed": 0, "failed": 0}
        for task in self._warm_tasks:
            try:
                await self._manager.get_or_fetch(
                    task["tool_name"],
                    task["args"],
                    task["fetch_fn"],
                )
                results["warmed"] += 1
            except Exception:
                results["failed"] += 1
        return results

    async def run_loop(self) -> None:
        """Background task: re-warm each task at its interval."""
        while True:
            await self.warm_all()
            min_interval = min((t["interval"] for t in self._warm_tasks), default=3600.0)
            await asyncio.sleep(min_interval)
```

## Solution 6: Tiered Cache Dashboard

```python
import time


class TieredCacheDashboard:
    """Reports hit rates per tier and identifies tools with low cache effectiveness."""

    def __init__(self, manager: TieredCacheManager):
        self._manager = manager

    def render(self) -> dict:
        stats = self._manager.all_stats()
        alerts = []
        for tier_stats in stats:
            if tier_stats.get("hits", 0) + tier_stats.get("misses", 0) > 100:
                hr = tier_stats.get("hit_rate", 0)
                if hr < 0.50:
                    alerts.append({
                        "tier": tier_stats["tier"],
                        "hit_rate": hr,
                        "message": f"{tier_stats['tier'].upper()} hit rate {hr:.1%} below 50% — review TTL config or cache key design.",
                    })
        return {
            "generated_at": time.time(),
            "tier_stats": stats,
            "alerts": alerts,
            "healthy": len(alerts) == 0,
        }
```

## Comparison

| Approach | In-Process L1 | Shared L2 | Auto-Promotion | Per-Tool TTL | Cache Warming | Dashboard |
|---|---|---|---|---|---|---|
| L1InProcessCache | Yes (LRU) | No | No | Per-entry | No | No |
| L2SharedMemoryCache | No | Yes | No | Per-entry | No | No |
| TieredCacheManager | Via L1 | Via L2 | Yes (L2→L1) | Yes | No | No |
| CacheWarmingScheduler | Via manager | Via manager | Via manager | Via manager | Yes | No |
| TieredCacheDashboard | No | No | No | No | No | Yes |

**Best for production**: Configure per-tool TTLs explicitly — user profile lookups (L1: 5min, L2: 1hr), live price data (L1: 30s, L2: 60s), static reference data (L1: 1hr, L2: 24hr). The L2 key design is critical: use a deterministic hash of tool name + normalized args so all workers share the same cache entries. Register warm tasks for your top-10 most frequently called tools so they're cache-hot after a deployment or restart. Monitor `hit_rate` per tier: a healthy L1 rate is 60–90% for sequential agent workflows; below 40% means the L1 is too small or TTLs are too short.
