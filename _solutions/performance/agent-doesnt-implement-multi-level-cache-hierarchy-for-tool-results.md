---
title: "Agent Doesn't Implement Multi-Level Cache Hierarchy for Tool Results"
description: "Agents that use a single flat cache for all tool results apply the same eviction policy and TTL to both hot frequent queries and cold rare ones. A frequently-used database lookup should live in a fast in-process L1 cache; a rarely-used external API result should live in a slower shared L2 cache. Implement a multi-level cache hierarchy with separate policies per level that maximizes hit rate for hot data while managing memory and network costs for cold data."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-multi-level-cache-hierarchy-for-tool-results
tags: [cache-hierarchy, l1-l2-cache, cache-tiering, eviction-policy, cache-hit-rate, tool-result-caching]
symptoms:
  - "Frequently-accessed tool results evicted by single large LRU cache"
  - "Rarely-used results take the same fast cache slots as hot data"
  - "No differentiation between in-process and shared/remote cache layers"
  - "Cache TTL is the same for stable data and volatile data"
  - "Cache hit rate is low because eviction policy is not tuned to access patterns"
---

## Why This Happens

A single-level LRU cache treats all entries equally — a result accessed once an hour competes for slots with a result accessed a thousand times per minute. Multi-level caching resolves this by separating concerns: L1 (in-process, bounded memory, LRU) holds the hottest data with the fastest access; L2 (process-local with larger capacity or shared Redis) holds warm data at slightly higher access cost; L3 (distributed or persistent) holds cold data that shouldn't be recomputed but doesn't need to be fast. Promotions and demotions between levels happen automatically based on access frequency.

## Solution 1: Cache Entry

```python
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class CacheLevel(str, Enum):
    L1 = "l1"   # in-process, very small, very fast
    L2 = "l2"   # in-process, medium, LRU
    L3 = "l3"   # shared or persistent, large, slower


@dataclass
class CacheEntry:
    key: str
    value: Any
    level: CacheLevel
    created_at: float = field(default_factory=time.time)
    last_accessed_at: float = field(default_factory=time.time)
    access_count: int = 0
    ttl_seconds: Optional[float] = None

    def is_expired(self) -> bool:
        if self.ttl_seconds is None:
            return False
        return time.time() - self.created_at > self.ttl_seconds

    def touch(self) -> None:
        self.last_accessed_at = time.time()
        self.access_count += 1

    def age_seconds(self) -> float:
        return round(time.time() - self.created_at, 1)
```

## Solution 2: LRU Cache Layer

```python
from collections import OrderedDict
from threading import Lock
from typing import Any, Optional


class LRUCacheLayer:
    """
    Thread-safe LRU cache for a single cache level.
    Evicts least-recently-used entries when capacity is reached.
    """

    def __init__(
        self,
        level: CacheLevel,
        max_entries: int,
        default_ttl_seconds: Optional[float] = None,
    ):
        self._level = level
        self._max = max_entries
        self._default_ttl = default_ttl_seconds
        self._store: OrderedDict[str, CacheEntry] = OrderedDict()
        self._lock = Lock()
        self._hits = 0
        self._misses = 0
        self._evictions = 0

    def get(self, key: str) -> Optional[CacheEntry]:
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                self._misses += 1
                return None
            if entry.is_expired():
                del self._store[key]
                self._misses += 1
                return None
            # Move to end (most recently used)
            self._store.move_to_end(key)
            entry.touch()
            self._hits += 1
            return entry

    def put(
        self,
        key: str,
        value: Any,
        ttl_seconds: Optional[float] = None,
    ) -> CacheEntry:
        with self._lock:
            if key in self._store:
                self._store.move_to_end(key)
                entry = self._store[key]
                entry.value = value
                entry.created_at = time.time()
                entry.ttl_seconds = ttl_seconds or self._default_ttl
                return entry

            while len(self._store) >= self._max:
                # Evict least recently used (first item)
                self._store.popitem(last=False)
                self._evictions += 1

            entry = CacheEntry(
                key=key,
                value=value,
                level=self._level,
                ttl_seconds=ttl_seconds or self._default_ttl,
            )
            self._store[key] = entry
            return entry

    def invalidate(self, key: str) -> None:
        with self._lock:
            self._store.pop(key, None)

    def stats(self) -> dict:
        with self._lock:
            size = len(self._store)
        total = self._hits + self._misses
        return {
            "level": self._level.value,
            "size": size,
            "max": self._max,
            "hits": self._hits,
            "misses": self._misses,
            "evictions": self._evictions,
            "hit_rate": round(self._hits / max(total, 1), 4),
        }
```

## Solution 3: Multi-Level Cache

```python
from typing import Any, Optional


class MultiLevelCache:
    """
    Three-level cache hierarchy. Gets check L1 first, then L2, then L3.
    On a miss at Lk but hit at Lk+1, promotes the entry to Lk.
    """

    def __init__(
        self,
        l1: LRUCacheLayer,
        l2: LRUCacheLayer,
        l3: Optional[LRUCacheLayer] = None,
        l1_promotion_threshold: int = 3,  # access count before L2 -> L1 promotion
    ):
        self._l1 = l1
        self._l2 = l2
        self._l3 = l3
        self._promo_threshold = l1_promotion_threshold
        self._l2_to_l1_promotions = 0
        self._l3_to_l2_promotions = 0

    def get(self, key: str) -> Optional[Any]:
        # L1 check
        entry = self._l1.get(key)
        if entry is not None:
            return entry.value

        # L2 check
        entry = self._l2.get(key)
        if entry is not None:
            # Promote to L1 if accessed frequently enough
            if entry.access_count >= self._promo_threshold:
                self._l1.put(key, entry.value, ttl_seconds=entry.ttl_seconds)
                self._l2_to_l1_promotions += 1
            return entry.value

        # L3 check
        if self._l3 is not None:
            entry = self._l3.get(key)
            if entry is not None:
                # Promote to L2
                self._l2.put(key, entry.value, ttl_seconds=entry.ttl_seconds)
                self._l3_to_l2_promotions += 1
                return entry.value

        return None

    def put(
        self,
        key: str,
        value: Any,
        level: CacheLevel = CacheLevel.L2,
        ttl_seconds: Optional[float] = None,
    ) -> None:
        """Writes to the specified level and below."""
        if level == CacheLevel.L1:
            self._l1.put(key, value, ttl_seconds)
        elif level == CacheLevel.L2:
            self._l2.put(key, value, ttl_seconds)
        elif level == CacheLevel.L3 and self._l3:
            self._l3.put(key, value, ttl_seconds)

    def invalidate(self, key: str) -> None:
        self._l1.invalidate(key)
        self._l2.invalidate(key)
        if self._l3:
            self._l3.invalidate(key)

    def stats(self) -> dict:
        s = {
            "l1": self._l1.stats(),
            "l2": self._l2.stats(),
            "l2_to_l1_promotions": self._l2_to_l1_promotions,
        }
        if self._l3:
            s["l3"] = self._l3.stats()
            s["l3_to_l2_promotions"] = self._l3_to_l2_promotions
        return s
```

## Solution 4: Tool Result Cache Advisor

```python
from typing import Optional


class ToolResultCacheAdvisor:
    """
    Recommends which cache level to write a tool result to based on
    the tool's characteristics and the result's estimated stability.
    """

    # (tool_name_pattern, ttl_seconds, write_level)
    TOOL_POLICIES = [
        ("*_config*", 3600.0, CacheLevel.L1),       # config reads: hot, stable
        ("*_search*", 300.0, CacheLevel.L2),          # search: warm, semi-volatile
        ("*_external*", 600.0, CacheLevel.L2),        # external APIs: warm
        ("*_analytics*", 1800.0, CacheLevel.L3),      # analytics: cold, stable
        ("*_realtime*", 30.0, CacheLevel.L1),         # real-time: hot, volatile
    ]

    def advise(
        self,
        tool_name: str,
        result_size_chars: int,
    ) -> tuple:
        """Returns (write_level, ttl_seconds)."""
        import fnmatch
        for pattern, ttl, level in self.TOOL_POLICIES:
            if fnmatch.fnmatch(tool_name.lower(), pattern):
                # Large results go to L2 or L3 regardless of pattern
                if result_size_chars > 10000 and level == CacheLevel.L1:
                    return CacheLevel.L2, ttl
                return level, ttl
        # Default: L2, 5 minutes
        return CacheLevel.L2, 300.0
```

## Solution 5: Cache Warming Strategy

```python
import asyncio
from typing import Any, Callable, List


class CacheWarmingStrategy:
    """
    Pre-populates L1 and L2 caches with known-frequent keys at startup.
    Prevents cold-start cache misses for high-traffic tool calls.
    """

    def __init__(self, cache: MultiLevelCache):
        self._cache = cache
        self._warmed_keys: List[str] = []

    async def warm(
        self,
        warm_fn: Callable[[str], Any],  # async fn(key) -> value
        keys: List[str],
        level: CacheLevel = CacheLevel.L2,
        ttl_seconds: float = 3600.0,
        concurrency: int = 5,
    ) -> dict:
        semaphore = asyncio.Semaphore(concurrency)
        successes = 0
        failures = 0

        async def _warm_one(key: str) -> None:
            nonlocal successes, failures
            async with semaphore:
                try:
                    value = await warm_fn(key)
                    self._cache.put(key, value, level=level, ttl_seconds=ttl_seconds)
                    self._warmed_keys.append(key)
                    successes += 1
                except Exception:
                    failures += 1

        await asyncio.gather(*[_warm_one(k) for k in keys])
        return {"warmed": successes, "failed": failures, "total": len(keys)}
```

## Solution 6: Cache Hierarchy Dashboard

```python
import time


class CacheHierarchyDashboard:
    """
    Renders multi-level cache stats, promotion rates, and advisor coverage.
    """

    def __init__(self, cache: MultiLevelCache):
        self._cache = cache

    def render(self) -> dict:
        stats = self._cache.stats()
        # Compute aggregate hit rate across levels
        total_hits = sum(stats[level].get("hits", 0) for level in ("l1", "l2", "l3") if level in stats)
        total_requests = sum(
            stats[level].get("hits", 0) + stats[level].get("misses", 0)
            for level in ("l1", "l2", "l3") if level in stats
        )
        return {
            "generated_at": time.time(),
            "level_stats": stats,
            "aggregate_hit_rate": round(total_hits / max(total_requests, 1), 4),
        }
```

## Comparison

| Approach | LRU Eviction | Level Promotion | TTL Per Level | Advisor | Warming |
|---|---|---|---|---|---|
| LRUCacheLayer | Yes | No | Yes | No | No |
| MultiLevelCache | Via layers | Yes (access count) | Via layers | No | No |
| ToolResultCacheAdvisor | No | No | Yes (per pattern) | Yes | No |
| CacheWarmingStrategy | No | No | No | No | Yes |
| CacheHierarchyDashboard | No | No | No | No | No |

**Best for production**: Size L1 to hold the top 1% of queries by access frequency — for most agent workloads this is 50-200 entries. Size L2 at 5-10× L1 to hold the warm tier without competing with application heap. Use `ToolResultCacheAdvisor` to avoid hard-coding tool-to-level mappings — the pattern matching keeps the policy in one place and testable. Monitor `aggregate_hit_rate` via the dashboard: below 0.60 means the cache is undersized or TTLs are too short; above 0.95 means the workload is highly repetitive and you may be caching stale data for too long. Run `CacheWarmingStrategy.warm()` at startup for the 20-30 most common queries — a warm cache eliminates cold-start latency spikes after deployments.
