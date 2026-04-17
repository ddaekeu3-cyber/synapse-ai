---
title: "Agent Doesn't Implement Embedding Cache with Content-Addressed Storage"
description: "Agents that re-embed the same text on every retrieval query pay embedding API costs repeatedly for identical inputs. The same document chunk, tool description, or user query phrasing is embedded fresh on every call with no cache. Implement a content-addressed embedding cache that stores vectors keyed by the SHA-256 of normalized input text, serving cache hits at zero API cost and avoiding redundant network round-trips."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-embedding-cache-with-content-addressed-storage
tags: [embedding-cache, content-addressed, vector-cache, retrieval-optimization, api-cost, deduplication]
symptoms:
  - "Identical document chunks re-embedded on every retrieval query"
  - "Embedding API costs grow linearly with retrieval calls even for repeated content"
  - "Tool descriptions embedded fresh on every session initialization"
  - "No cache between embedding model calls for the same normalized text"
  - "Retrieval latency dominated by embedding API round-trips for known content"
---

## Why This Happens

Embedding calls are treated like any other API call: send text, receive vector, use vector, discard. The vector is not stored because the application has no concept of a text-to-vector mapping that persists beyond a single call. Content-addressed storage (CAS) solves this cleanly: the cache key is derived from the content itself (SHA-256 of normalized text), so the same text always maps to the same key regardless of when or where it is requested. A cache hit returns the stored vector immediately; a miss calls the embedding API, stores the result, and returns it. The cache never becomes stale because the key is the content.

## Solution 1: Content-Addressed Cache Key

```python
import hashlib
import re
import unicodedata
from typing import Optional


class EmbeddingCacheKey:
    """
    Produces a stable, content-addressed cache key for embedding inputs.
    Normalizes text before hashing to collapse trivial differences
    (extra whitespace, Unicode variants) into the same key.
    """

    def __init__(self, model_id: str):
        self._model_id = model_id

    def _normalize(self, text: str) -> str:
        text = unicodedata.normalize("NFKC", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text.lower()

    def compute(self, text: str) -> str:
        normalized = self._normalize(text)
        # Include model_id so different models have different cache spaces
        content = f"{self._model_id}::{normalized}"
        return hashlib.sha256(content.encode()).hexdigest()

    def compute_batch(self, texts: list) -> list:
        return [self.compute(t) for t in texts]
```

## Solution 2: In-Process LRU Embedding Cache

```python
import time
from collections import OrderedDict
from dataclasses import dataclass
from threading import Lock
from typing import Dict, List, Optional


@dataclass
class CachedEmbedding:
    vector: List[float]
    text_preview: str        # first 100 chars for debugging
    created_at: float
    hit_count: int = 0


class InProcessEmbeddingCache:
    """
    Thread-safe LRU cache for embedding vectors.
    Content-addressed: key = SHA-256 of normalized input + model_id.
    """

    def __init__(self, max_entries: int = 50000, ttl_seconds: Optional[float] = None):
        self._lock = Lock()
        self._cache: OrderedDict[str, CachedEmbedding] = OrderedDict()
        self._max = max_entries
        self._ttl = ttl_seconds
        self._hits = 0
        self._misses = 0

    def get(self, key: str) -> Optional[List[float]]:
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                self._misses += 1
                return None
            if self._ttl and time.time() - entry.created_at > self._ttl:
                del self._cache[key]
                self._misses += 1
                return None
            self._cache.move_to_end(key)
            entry.hit_count += 1
            self._hits += 1
            return entry.vector

    def put(self, key: str, vector: List[float], text_preview: str = "") -> None:
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                return
            while len(self._cache) >= self._max:
                self._cache.popitem(last=False)
            self._cache[key] = CachedEmbedding(
                vector=vector,
                text_preview=text_preview[:100],
                created_at=time.time(),
            )

    def stats(self) -> dict:
        with self._lock:
            total = self._hits + self._misses
            return {
                "entries": len(self._cache),
                "max_entries": self._max,
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": round(self._hits / max(total, 1), 4),
                "utilization": round(len(self._cache) / self._max, 4),
            }
```

## Solution 3: File-Backed Persistent Embedding Cache

```python
import json
import os
import struct
import time
from pathlib import Path
from threading import Lock
from typing import Dict, List, Optional


class FileBackedEmbeddingCache:
    """
    Persists embedding vectors to disk using a content-addressed directory layout.
    Each vector stored as a binary file at {base_dir}/{key[:2]}/{key}.bin
    alongside a JSON sidecar with metadata.
    Survives process restarts — serves as the L2 cache behind the in-process LRU.
    """

    def __init__(self, base_dir: str, max_age_days: float = 30.0):
        self._base = Path(base_dir)
        self._base.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._max_age = max_age_days * 86400

    def _paths(self, key: str):
        shard = key[:2]
        dir_path = self._base / shard
        dir_path.mkdir(exist_ok=True)
        return dir_path / f"{key}.bin", dir_path / f"{key}.json"

    def get(self, key: str) -> Optional[List[float]]:
        bin_path, meta_path = self._paths(key)
        if not bin_path.exists():
            return None
        try:
            stat = bin_path.stat()
            if time.time() - stat.st_mtime > self._max_age:
                bin_path.unlink(missing_ok=True)
                meta_path.unlink(missing_ok=True)
                return None
            data = bin_path.read_bytes()
            n = len(data) // 4
            return list(struct.unpack(f"{n}f", data))
        except (OSError, struct.error):
            return None

    def put(self, key: str, vector: List[float], metadata: dict = None) -> None:
        bin_path, meta_path = self._paths(key)
        with self._lock:
            try:
                bin_path.write_bytes(struct.pack(f"{len(vector)}f", *vector))
                if metadata:
                    meta_path.write_text(json.dumps(metadata))
            except OSError:
                pass

    def purge_old(self) -> int:
        removed = 0
        cutoff = time.time() - self._max_age
        for p in self._base.rglob("*.bin"):
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
                    meta = p.with_suffix(".json")
                    meta.unlink(missing_ok=True)
                    removed += 1
            except OSError:
                pass
        return removed
```

## Solution 4: Two-Level Caching Embedding Client

```python
import asyncio
from typing import Any, Callable, Dict, List, Optional


class TwoLevelCachingEmbeddingClient:
    """
    L1: in-process LRU (fast, bounded)
    L2: file-backed persistent cache (survives restarts, larger)
    On miss at both levels: calls the embedding API and populates both.
    """

    def __init__(
        self,
        key_fn: EmbeddingCacheKey,
        l1: InProcessEmbeddingCache,
        l2: FileBackedEmbeddingCache,
        embed_api_fn: Callable[[str], List[float]],
    ):
        self._key_fn = key_fn
        self._l1 = l1
        self._l2 = l2
        self._api = embed_api_fn
        self._api_calls = 0
        self._l1_hits = 0
        self._l2_hits = 0

    async def embed(self, text: str) -> List[float]:
        key = self._key_fn.compute(text)

        # L1 check
        vector = self._l1.get(key)
        if vector is not None:
            self._l1_hits += 1
            return vector

        # L2 check
        vector = self._l2.get(key)
        if vector is not None:
            self._l2_hits += 1
            self._l1.put(key, vector, text[:100])
            return vector

        # API call
        vector = await self._api(text)
        self._api_calls += 1
        self._l1.put(key, vector, text[:100])
        self._l2.put(key, vector, {"text_preview": text[:200]})
        return vector

    async def embed_batch(self, texts: List[str]) -> List[List[float]]:
        return await asyncio.gather(*[self.embed(t) for t in texts])

    def stats(self) -> dict:
        total = self._l1_hits + self._l2_hits + self._api_calls
        return {
            "total_requests": total,
            "l1_hits": self._l1_hits,
            "l2_hits": self._l2_hits,
            "api_calls": self._api_calls,
            "cache_hit_rate": round((self._l1_hits + self._l2_hits) / max(total, 1), 4),
            "l1_stats": self._l1.stats(),
        }
```

## Solution 5: Cache Warming Utility

```python
import asyncio
from typing import Callable, List


class EmbeddingCacheWarmer:
    """
    Pre-populates the embedding cache with known high-frequency texts
    at startup — tool descriptions, system prompt segments, common queries.
    """

    def __init__(self, client: TwoLevelCachingEmbeddingClient):
        self._client = client

    async def warm(
        self,
        texts: List[str],
        concurrency: int = 5,
    ) -> dict:
        semaphore = asyncio.Semaphore(concurrency)

        async def embed_one(text: str) -> bool:
            async with semaphore:
                try:
                    await self._client.embed(text)
                    return True
                except Exception:
                    return False

        results = await asyncio.gather(*[embed_one(t) for t in texts])
        return {
            "texts_warmed": len(texts),
            "succeeded": sum(results),
            "failed": sum(1 for r in results if not r),
        }
```

## Solution 6: Cache Efficiency Dashboard

```python
import time


class EmbeddingCacheDashboard:
    """
    Combines L1/L2 cache stats and API call savings into
    a single cost and performance view.
    """

    def __init__(
        self,
        client: TwoLevelCachingEmbeddingClient,
        cost_per_api_call: float = 0.0001,   # USD per embedding call
    ):
        self._client = client
        self._cost_per_call = cost_per_api_call

    def render(self) -> dict:
        stats = self._client.stats()
        calls_avoided = stats["l1_hits"] + stats["l2_hits"]
        cost_saved = calls_avoided * self._cost_per_call

        return {
            "generated_at": time.time(),
            "total_requests": stats["total_requests"],
            "cache_hit_rate": stats["cache_hit_rate"],
            "api_calls_made": stats["api_calls"],
            "api_calls_avoided": calls_avoided,
            "estimated_cost_saved_usd": round(cost_saved, 4),
            "l1_cache": stats["l1_stats"],
            "l2_hits": stats["l2_hits"],
        }
```

## Comparison

| Approach | Content-Addressed Key | In-Process LRU | File Persistence | Batch Embedding | Cost Tracking |
|---|---|---|---|---|---|
| EmbeddingCacheKey | Yes (SHA-256) | No | No | Yes (batch) | No |
| InProcessEmbeddingCache | Via key | Yes | No | No | Via stats |
| FileBackedEmbeddingCache | Via key | No | Yes | No | No |
| TwoLevelCachingEmbeddingClient | Via key | Via L1 | Via L2 | Yes | Via stats |
| EmbeddingCacheDashboard | No | No | No | No | Yes |

**Best for production**: Pre-warm the cache at startup with `EmbeddingCacheWarmer` using all tool descriptions and the most common document chunks — these are embedded on the first retrieval anyway, so warming just moves the cost to startup where it is amortized. Set `max_entries=50000` for the L1 LRU with `ttl_seconds=None` (no TTL for content-addressed vectors — they never become stale). Keep `FileBackedEmbeddingCache` with `max_age_days=30` and run `purge_old()` weekly: embedding model upgrades will invalidate old vectors, and purging by age handles this automatically when the model_id is changed.
