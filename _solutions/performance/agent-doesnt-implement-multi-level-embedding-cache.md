---
title: "Agent Doesn't Implement Multi-Level Embedding Cache"
description: "Agents that re-embed the same text on every request pay full embedding API latency and cost repeatedly. A single in-process LRU cache helps but evicts useful entries under memory pressure. Implement a multi-level embedding cache with a fast in-process L1 tier and a persistent L2 tier: L1 hits return in microseconds, L2 hits avoid the embedding API call, and only genuine misses reach the embedding model."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-multi-level-embedding-cache
tags: [embedding-cache, multi-level-cache, l1-l2-cache, vector-cache, latency-reduction, cost-optimization]
symptoms:
  - "Same document chunks are re-embedded every session — embedding API cost scales with requests not documents"
  - "Cache evicts hot embeddings under memory pressure, causing repeated API calls for frequent documents"
  - "Agent restart loses all cached embeddings — cold start re-embeds the entire corpus"
  - "No metrics on embedding cache hit rate — impossible to tune cache size"
  - "Cross-process agents embed the same text independently with no shared cache"
---

## Why This Happens

Most implementations use a single dict or LRU cache keyed by text. This is better than no cache, but has two failure modes: (1) the process-local cache is lost on restart, requiring full re-embedding of the corpus; and (2) under memory pressure the LRU evicts entries that will be needed again. A two-level cache fixes both: L1 is a small, fast in-process LRU for hot embeddings; L2 is a larger persistent store (disk or a KV store) for warm embeddings. Reads check L1 first, then L2, then the embedding API. Writes populate both levels. On restart, L1 is cold but L2 is warm.

## Solution 1: Embedding Cache Entry

```python
import hashlib
import time
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class EmbeddingCacheEntry:
    text_hash: str
    embedding: List[float]
    model: str
    dimensions: int
    created_at: float = field(default_factory=time.time)
    access_count: int = 0
    last_accessed_at: float = field(default_factory=time.time)

    def touch(self) -> None:
        self.access_count += 1
        self.last_accessed_at = time.time()

    def age_seconds(self) -> float:
        return time.time() - self.created_at


def make_cache_key(text: str, model: str) -> str:
    payload = f"{model}:{text}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
```

## Solution 2: L1 In-Process LRU Cache

```python
from collections import OrderedDict
from threading import RLock
from typing import Dict, Optional


class L1EmbeddingCache:
    """
    Fast in-process LRU cache for hot embeddings.
    Uses an OrderedDict to track access order in O(1).
    Thread-safe via RLock for concurrent agent coroutines.
    """

    def __init__(self, max_entries: int = 512):
        self._max = max_entries
        self._store: OrderedDict[str, EmbeddingCacheEntry] = OrderedDict()
        self._lock = RLock()
        self._hits = 0
        self._misses = 0

    def get(self, key: str) -> Optional[EmbeddingCacheEntry]:
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                self._misses += 1
                return None
            # Move to end (most recently used)
            self._store.move_to_end(key)
            entry.touch()
            self._hits += 1
            return entry

    def put(self, key: str, entry: EmbeddingCacheEntry) -> None:
        with self._lock:
            if key in self._store:
                self._store.move_to_end(key)
                self._store[key] = entry
                return
            self._store[key] = entry
            if len(self._store) > self._max:
                self._store.popitem(last=False)   # evict LRU

    def invalidate(self, key: str) -> None:
        with self._lock:
            self._store.pop(key, None)

    def stats(self) -> dict:
        total = self._hits + self._misses
        return {
            "level": "L1",
            "entries": len(self._store),
            "max_entries": self._max,
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": round(self._hits / max(total, 1), 4),
        }
```

## Solution 3: L2 Persistent Cache

```python
import json
import os
import struct
import time
from pathlib import Path
from typing import Optional


class L2EmbeddingCache:
    """
    Persistent on-disk cache for warm embeddings.
    Stores entries as JSON files under a directory keyed by text hash.
    Survives process restarts; survives across agents on the same machine.
    Evicts entries beyond a max_entries limit using LRU based on mtime.
    """

    def __init__(
        self,
        cache_dir: str,
        max_entries: int = 50_000,
        ttl_seconds: float = 7 * 86400.0,   # 7 days
    ):
        self._dir = Path(cache_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._max = max_entries
        self._ttl = ttl_seconds
        self._hits = 0
        self._misses = 0

    def _path(self, key: str) -> Path:
        # Use first 2 chars as subdirectory to avoid inode exhaustion
        subdir = self._dir / key[:2]
        subdir.mkdir(exist_ok=True)
        return subdir / f"{key}.json"

    def get(self, key: str) -> Optional[EmbeddingCacheEntry]:
        path = self._path(key)
        if not path.exists():
            self._misses += 1
            return None
        try:
            data = json.loads(path.read_text())
            entry = EmbeddingCacheEntry(**data)
            if entry.age_seconds() > self._ttl:
                path.unlink(missing_ok=True)
                self._misses += 1
                return None
            # Update mtime for LRU tracking
            path.touch()
            entry.touch()
            self._hits += 1
            return entry
        except Exception:
            self._misses += 1
            return None

    def put(self, key: str, entry: EmbeddingCacheEntry) -> None:
        try:
            path = self._path(key)
            data = {
                "text_hash": entry.text_hash,
                "embedding": entry.embedding,
                "model": entry.model,
                "dimensions": entry.dimensions,
                "created_at": entry.created_at,
                "access_count": entry.access_count,
                "last_accessed_at": entry.last_accessed_at,
            }
            path.write_text(json.dumps(data))
        except Exception:
            pass   # L2 failures are non-fatal

    def evict_expired(self) -> int:
        removed = 0
        cutoff = time.time() - self._ttl
        for path in self._dir.rglob("*.json"):
            if path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
                removed += 1
        return removed

    def evict_lru_to_limit(self) -> int:
        all_files = sorted(
            self._dir.rglob("*.json"), key=lambda p: p.stat().st_mtime
        )
        over = len(all_files) - self._max
        removed = 0
        for path in all_files[:max(over, 0)]:
            path.unlink(missing_ok=True)
            removed += 1
        return removed

    def stats(self) -> dict:
        total = self._hits + self._misses
        return {
            "level": "L2",
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": round(self._hits / max(total, 1), 4),
        }
```

## Solution 4: Multi-Level Cache Coordinator

```python
from typing import Callable, List, Optional


class MultiLevelEmbeddingCache:
    """
    Coordinates L1 and L2 caches with read-through and write-through.
    Read path: L1 → L2 → embedding API
    Write path: API result → L2 → L1 (populate both on miss)
    L2 hits are promoted to L1 to speed up repeated access.
    """

    def __init__(self, l1: L1EmbeddingCache, l2: L2EmbeddingCache):
        self._l1 = l1
        self._l2 = l2
        self._api_calls = 0

    async def get_or_embed(
        self,
        text: str,
        model: str,
        embed_fn: Callable[[str, str], List[float]],
    ) -> List[float]:
        key = make_cache_key(text, model)

        # L1 check
        entry = self._l1.get(key)
        if entry is not None:
            return entry.embedding

        # L2 check
        entry = self._l2.get(key)
        if entry is not None:
            # Promote to L1
            self._l1.put(key, entry)
            return entry.embedding

        # Cache miss — call embedding API
        self._api_calls += 1
        vector = await embed_fn(text, model)
        entry = EmbeddingCacheEntry(
            text_hash=key,
            embedding=vector,
            model=model,
            dimensions=len(vector),
        )
        # Write-through to both levels
        self._l2.put(key, entry)
        self._l1.put(key, entry)
        return vector

    def invalidate(self, text: str, model: str) -> None:
        key = make_cache_key(text, model)
        self._l1.invalidate(key)
        path = self._l2._path(key)
        path.unlink(missing_ok=True)

    def stats(self) -> dict:
        l1 = self._l1.stats()
        l2 = self._l2.stats()
        total_requests = l1["hits"] + l1["misses"]
        return {
            "l1": l1,
            "l2": l2,
            "api_calls": self._api_calls,
            "overall_hit_rate": round(
                1.0 - (self._api_calls / max(total_requests, 1)), 4
            ),
        }
```

## Solution 5: Batch Embedding Cache Warmer

```python
import asyncio
from typing import Callable, List, Tuple


class EmbeddingCacheWarmer:
    """
    Pre-populates the cache with embeddings for a known document corpus
    before the agent starts serving requests.
    Uses batched embedding calls to minimise API round-trips.
    """

    def __init__(
        self,
        cache: MultiLevelEmbeddingCache,
        batch_size: int = 32,
        concurrency: int = 4,
    ):
        self._cache = cache
        self._batch_size = batch_size
        self._concurrency = concurrency

    async def warm(
        self,
        texts: List[str],
        model: str,
        embed_fn: Callable[[str, str], List[float]],
    ) -> dict:
        sem = asyncio.Semaphore(self._concurrency)
        total = len(texts)
        cached = 0
        fetched = 0

        async def process(text: str) -> None:
            nonlocal cached, fetched
            key = make_cache_key(text, model)
            if self._cache._l2.get(key) is not None:
                cached += 1
                return
            async with sem:
                await self._cache.get_or_embed(text, model, embed_fn)
                fetched += 1

        await asyncio.gather(*[process(t) for t in texts])
        return {
            "total": total,
            "already_cached": cached,
            "newly_fetched": fetched,
            "cache_stats": self._cache.stats(),
        }
```

## Solution 6: Cache Health Monitor

```python
import time


class EmbeddingCacheHealthMonitor:
    """
    Monitors cache efficiency and triggers maintenance operations
    (L2 eviction, L1 resize recommendations) based on observed hit rates.
    """

    def __init__(
        self,
        cache: MultiLevelEmbeddingCache,
        l2_cache: L2EmbeddingCache,
        target_l1_hit_rate: float = 0.60,
        target_overall_hit_rate: float = 0.90,
    ):
        self._cache = cache
        self._l2 = l2_cache
        self._target_l1 = target_l1_hit_rate
        self._target_overall = target_overall_hit_rate

    def check(self) -> dict:
        stats = self._cache.stats()
        l1_hit_rate = stats["l1"]["hit_rate"]
        overall_hit_rate = stats["overall_hit_rate"]
        alerts = []

        if l1_hit_rate < self._target_l1:
            alerts.append({
                "type": "low_l1_hit_rate",
                "value": l1_hit_rate,
                "target": self._target_l1,
                "recommendation": "increase L1 max_entries or check for access pattern churn",
            })

        if overall_hit_rate < self._target_overall:
            alerts.append({
                "type": "low_overall_hit_rate",
                "value": overall_hit_rate,
                "target": self._target_overall,
                "recommendation": "run cache warmer or increase L2 TTL",
            })

        return {
            "generated_at": time.time(),
            "healthy": len(alerts) == 0,
            "stats": stats,
            "alerts": alerts,
        }

    def run_maintenance(self) -> dict:
        expired = self._l2.evict_expired()
        lru_evicted = self._l2.evict_lru_to_limit()
        return {
            "expired_evicted": expired,
            "lru_evicted": lru_evicted,
        }
```

## Comparison

| Approach | In-Process | Persistent | Read-Through | Write-Through | Warming |
|---|---|---|---|---|---|
| L1EmbeddingCache | Yes (LRU) | No | No | No | No |
| L2EmbeddingCache | No | Yes (disk) | No | No | No |
| MultiLevelEmbeddingCache | Via L1 | Via L2 | Yes | Yes | No |
| EmbeddingCacheWarmer | No | No | No | Via cache | Yes |
| EmbeddingCacheHealthMonitor | No | No | No | No | No (alerts) |

**Best for production**: Set L1 to 512–2048 entries (a few MB of float32 vectors) and L2 to 50k–500k entries on an SSD. Use `EmbeddingCacheWarmer` at agent startup to pre-populate L2 with your static corpus — warm starts eliminate cold-start re-embedding. Run `EmbeddingCacheHealthMonitor.run_maintenance()` nightly to purge stale L2 entries. Target overall hit rate ≥ 90% before considering larger embedding models — if you're calling the embedding API for the same text repeatedly, the bottleneck is the cache, not the model.
