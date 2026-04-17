---
title: "Agent Doesn't Implement Multi-Level Cache Hierarchy for Tool Results"
description: "Agents that use a single flat cache for tool results miss optimization opportunities: an in-process memory cache handles microsecond-latency hits, a shared Redis layer handles multi-instance deduplication, and a persistent tier handles expensive results that survive restarts. Implement a multi-level cache hierarchy that checks L1 (in-process), then L2 (shared), then L3 (persistent) before executing the tool, promotes hits to higher levels, and provides per-level metrics."
date: 2026-04-16
difficulty: advanced
category: performance
slug: agent-doesnt-implement-multi-level-cache-hierarchy-for-tool-results
tags: [cache-hierarchy, multi-level-cache, l1-l2-l3, tool-result-caching, cache-promotion, redis-cache]
symptoms:
  - "Each agent instance maintains its own independent cache — identical tool calls across instances are not deduplicated"
  - "Expensive tool results are lost on restart because the cache is only in memory"
  - "A single flat cache has one TTL policy applied to all results regardless of cost or volatility"
  - "No per-level cache hit metrics — impossible to know how effective each cache tier is"
  - "Cache misses always go directly to the tool — no intermediate shared layer reduces tool load"
---

## Why This Happens

Single-level caches are the simplest implementation: store results in a dict, return on hit, call the tool on miss. This works for a single instance with moderate traffic but breaks down in three scenarios: multi-instance deployments (each instance has its own cold cache), restarts (in-memory cache is lost), and expensive tool calls (the result should survive far longer than standard TTL). A cache hierarchy solves all three: L1 (in-process LRU) handles hot keys with zero network latency; L2 (shared Redis or Memcached) deduplicates across instances; L3 (persistent storage) preserves expensive results across restarts. Results flow down on miss and up on hit (promotion).

## Solution 1: Cache Entry

```python
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class CacheLevel(str, Enum):
    L1 = "l1"    # in-process memory
    L2 = "l2"    # shared in-memory (Redis/Memcached)
    L3 = "l3"    # persistent (disk/database)
    MISS = "miss"


@dataclass
class CacheEntry:
    key: str
    value: Any
    tool_name: str
    created_at: float = field(default_factory=time.time)
    ttl_seconds: float = 300.0
    access_count: int = 0
    source_level: CacheLevel = CacheLevel.MISS

    def is_expired(self) -> bool:
        return time.time() - self.created_at > self.ttl_seconds

    def touch(self) -> None:
        self.access_count += 1

    @property
    def age_seconds(self) -> float:
        return round(time.time() - self.created_at, 1)

    @property
    def remaining_ttl(self) -> float:
        return max(0.0, self.ttl_seconds - (time.time() - self.created_at))
```

## Solution 2: L1 In-Process Cache

```python
from collections import OrderedDict
from threading import Lock
from typing import Optional


class L1InProcessCache:
    """
    LRU in-process cache with per-entry TTL.
    Microsecond latency; lost on process restart.
    """

    def __init__(self, max_entries: int = 500):
        self._max = max_entries
        self._store: OrderedDict[str, CacheEntry] = OrderedDict()
        self._lock = Lock()
        self._hits = 0
        self._misses = 0

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
            self._store.move_to_end(key)
            entry.touch()
            self._hits += 1
            return entry

    def put(self, entry: CacheEntry) -> None:
        with self._lock:
            if len(self._store) >= self._max and entry.key not in self._store:
                self._store.popitem(last=False)
            self._store[entry.key] = entry
            self._store.move_to_end(entry.key)

    def stats(self) -> dict:
        with self._lock:
            total = self._hits + self._misses
            return {
                "level": "l1",
                "entries": len(self._store),
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": round(self._hits / max(total, 1), 4),
            }
```

## Solution 3: L2 Shared Cache (Redis Adapter)

```python
import json
import time
from typing import Any, Callable, Optional


class L2SharedCache:
    """
    Shared cache using Redis (or any key-value store with TTL support).
    Provides cross-instance deduplication. Serializes values as JSON.
    Pass a no-op client for single-instance deployments.
    """

    def __init__(
        self,
        redis_client: Any,              # redis.Redis or compatible
        key_prefix: str = "agent:tool:",
    ):
        self._redis = redis_client
        self._prefix = key_prefix
        self._hits = 0
        self._misses = 0

    def _full_key(self, key: str) -> str:
        return f"{self._prefix}{key}"

    def get(self, key: str) -> Optional[CacheEntry]:
        try:
            raw = self._redis.get(self._full_key(key))
            if raw is None:
                self._misses += 1
                return None
            data = json.loads(raw)
            entry = CacheEntry(
                key=key,
                value=data["value"],
                tool_name=data["tool_name"],
                created_at=data["created_at"],
                ttl_seconds=data["ttl_seconds"],
                access_count=data.get("access_count", 0),
                source_level=CacheLevel.L2,
            )
            if entry.is_expired():
                self._redis.delete(self._full_key(key))
                self._misses += 1
                return None
            entry.touch()
            self._hits += 1
            return entry
        except Exception:
            self._misses += 1
            return None

    def put(self, entry: CacheEntry) -> None:
        try:
            data = {
                "value": entry.value,
                "tool_name": entry.tool_name,
                "created_at": entry.created_at,
                "ttl_seconds": entry.ttl_seconds,
                "access_count": entry.access_count,
            }
            ttl_int = max(1, int(entry.remaining_ttl))
            self._redis.setex(self._full_key(entry.key), ttl_int, json.dumps(data))
        except Exception:
            pass  # L2 failure is non-fatal; L1 still serves

    def stats(self) -> dict:
        total = self._hits + self._misses
        return {
            "level": "l2",
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": round(self._hits / max(total, 1), 4),
        }
```

## Solution 4: L3 Persistent Cache

```python
import json
import time
from pathlib import Path
from threading import Lock
from typing import Any, Optional


class L3PersistentCache:
    """
    Persistent file-based cache for expensive tool results that must
    survive restarts. Each entry is a separate JSON file.
    """

    def __init__(self, cache_dir: str = "/tmp/agent_l3_cache", max_files: int = 200):
        self._dir = Path(cache_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._max = max_files
        self._lock = Lock()
        self._hits = 0
        self._misses = 0

    def _path(self, key: str) -> Path:
        import hashlib
        safe_key = hashlib.sha256(key.encode()).hexdigest()
        return self._dir / f"{safe_key}.json"

    def get(self, key: str) -> Optional[CacheEntry]:
        path = self._path(key)
        try:
            with self._lock:
                if not path.exists():
                    self._misses += 1
                    return None
                data = json.loads(path.read_text())
            entry = CacheEntry(
                key=key,
                value=data["value"],
                tool_name=data["tool_name"],
                created_at=data["created_at"],
                ttl_seconds=data["ttl_seconds"],
                source_level=CacheLevel.L3,
            )
            if entry.is_expired():
                path.unlink(missing_ok=True)
                self._misses += 1
                return None
            self._hits += 1
            return entry
        except Exception:
            self._misses += 1
            return None

    def put(self, entry: CacheEntry) -> None:
        with self._lock:
            self._evict()
            path = self._path(entry.key)
            path.write_text(json.dumps({
                "value": entry.value,
                "tool_name": entry.tool_name,
                "created_at": entry.created_at,
                "ttl_seconds": entry.ttl_seconds,
            }))

    def _evict(self) -> None:
        files = sorted(self._dir.glob("*.json"), key=lambda p: p.stat().st_mtime)
        while len(files) >= self._max:
            files.pop(0).unlink(missing_ok=True)

    def stats(self) -> dict:
        total = self._hits + self._misses
        return {
            "level": "l3",
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": round(self._hits / max(total, 1), 4),
            "stored_files": len(list(self._dir.glob("*.json"))),
        }
```

## Solution 5: Multi-Level Cache

```python
import time
from typing import Any, Callable, Optional


class MultiLevelCache:
    """
    Checks L1 → L2 → L3 on miss. Promotes entries to higher levels on hit.
    Falls through to the tool on complete miss and populates all levels.
    """

    def __init__(
        self,
        l1: L1InProcessCache,
        l2: Optional[L2SharedCache] = None,
        l3: Optional[L3PersistentCache] = None,
    ):
        self._l1 = l1
        self._l2 = l2
        self._l3 = l3

    def get(self, key: str) -> Optional[CacheEntry]:
        # L1
        entry = self._l1.get(key)
        if entry:
            return entry

        # L2
        if self._l2:
            entry = self._l2.get(key)
            if entry:
                self._l1.put(entry)    # promote to L1
                return entry

        # L3
        if self._l3:
            entry = self._l3.get(key)
            if entry:
                self._l1.put(entry)    # promote to L1
                if self._l2:
                    self._l2.put(entry)  # promote to L2
                return entry

        return None

    def put(self, entry: CacheEntry, persist: bool = False) -> None:
        self._l1.put(entry)
        if self._l2:
            self._l2.put(entry)
        if persist and self._l3:
            self._l3.put(entry)

    async def get_or_execute(
        self,
        key: str,
        tool_name: str,
        tool_fn: Callable,
        ttl_seconds: float = 300.0,
        persist: bool = False,
        *args: Any,
        **kwargs: Any,
    ) -> dict:
        entry = self.get(key)
        if entry:
            return {
                "value": entry.value,
                "cache_hit": True,
                "source_level": entry.source_level.value,
                "age_seconds": entry.age_seconds,
            }

        result = await tool_fn(*args, **kwargs)
        new_entry = CacheEntry(
            key=key,
            value=result,
            tool_name=tool_name,
            ttl_seconds=ttl_seconds,
            source_level=CacheLevel.MISS,
        )
        self.put(new_entry, persist=persist)
        return {
            "value": result,
            "cache_hit": False,
            "source_level": "miss",
            "age_seconds": 0.0,
        }

    def all_stats(self) -> dict:
        stats = {"l1": self._l1.stats()}
        if self._l2:
            stats["l2"] = self._l2.stats()
        if self._l3:
            stats["l3"] = self._l3.stats()
        return stats
```

## Solution 6: Cache Hierarchy Dashboard

```python
import time


class CacheHierarchyDashboard:
    """Renders a snapshot of all cache level statistics."""

    def __init__(self, cache: MultiLevelCache):
        self._cache = cache

    def render(self) -> dict:
        stats = self._cache.all_stats()
        total_hits = sum(s.get("hits", 0) for s in stats.values())
        total_requests = sum(s.get("hits", 0) + s.get("misses", 0) for s in stats.values() if "l1" in s or True)
        l1_stats = stats.get("l1", {})
        total_reqs = l1_stats.get("hits", 0) + l1_stats.get("misses", 0)

        return {
            "generated_at": time.time(),
            "levels": stats,
            "overall_hit_rate": round(
                l1_stats.get("hits", 0) / max(total_reqs, 1), 4
            ),
            "l1_hit_rate": l1_stats.get("hit_rate", 0),
            "l2_hit_rate": stats.get("l2", {}).get("hit_rate", 0),
            "l3_hit_rate": stats.get("l3", {}).get("hit_rate", 0),
        }
```

## Comparison

| Approach | Latency | Scope | Survives Restart | TTL Support | Promotion |
|---|---|---|---|---|---|
| L1InProcessCache | Microseconds | Single instance | No | Yes | No |
| L2SharedCache | Milliseconds | Multi-instance | No (Redis restarts) | Yes (Redis TTL) | No |
| L3PersistentCache | Milliseconds | Single instance | Yes | Yes (checked on read) | No |
| MultiLevelCache | L1 speed on hit | All tiers | Via L3 | Per-entry | Yes (L3→L2→L1) |
| CacheHierarchyDashboard | No | No | No | No | No |

**Best for production**: Use `persist=True` only for tool results with high compute cost (vector search, external API aggregations) and long stable TTLs (≥1 hour) — persisting every result negates the L1 speed advantage and fills disk. Set L1 size to fit within the JVM/Python process heap budget (500 entries × avg entry size); L2 size is bounded by Redis memory. Tune TTLs per tool category: read-only database queries (5 minutes), external API calls (15 minutes), expensive computation (1 hour), static reference data (24 hours). Monitor per-level hit rates: if L2 hit rate is near zero in a multi-instance deployment, the key space is too fragmented (too many unique tool call signatures) and near-duplicate detection or argument normalization is needed before caching.
