---
title: "Agent Doesn't Implement Embedding Cache with TTL Eviction"
description: "Agents that recompute embeddings for the same text on every request pay embedding API costs repeatedly: a user query phrase that appears in hundreds of conversations triggers a new embedding API call each time. Implement an embedding cache with TTL-based eviction that stores computed embeddings keyed by text hash, reducing redundant API calls and latency for frequently-embedded content."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-embedding-cache-with-ttl-eviction
tags: [embedding-cache, ttl-eviction, api-cost-reduction, vector-reuse, cache-efficiency, semantic-search]
symptoms:
  - "Same text is embedded multiple times across different conversations"
  - "Embedding API costs grow linearly with request volume despite repeated queries"
  - "Embedding latency adds 100–500ms to every request even for cached-eligible content"
  - "No distinction between stable content (documents) and ephemeral content (queries)"
  - "Cache memory grows unboundedly — no eviction policy"
---

## Why This Happens

Embedding computation is treated as a pure function — same input always produces same output — but without caching, it pays the API cost on every call. Caching is skipped because text content appears to vary per request. In practice, a large fraction of embedded text is repeated: system prompts, document chunks, and common query phrasings recur across conversations. A cache keyed by SHA-256 of normalized text, with LRU eviction and TTL expiry, eliminates the majority of redundant embedding calls.

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
    model_id: str
    text_length: int
    created_at: float = field(default_factory=time.time)
    last_accessed_at: float = field(default_factory=time.time)
    access_count: int = 0
    ttl_seconds: float = 86400.0   # default 24h TTL

    def is_expired(self) -> bool:
        return time.time() - self.created_at > self.ttl_seconds

    def touch(self) -> None:
        self.last_accessed_at = time.time()
        self.access_count += 1

    @staticmethod
    def make_key(text: str, model_id: str) -> str:
        normalized = " ".join(text.lower().split())
        content = f"{model_id}:{normalized}"
        return hashlib.sha256(content.encode()).hexdigest()
```

## Solution 2: LRU Embedding Cache

```python
import time
from collections import OrderedDict
from threading import Lock
from typing import Dict, List, Optional, Tuple


class LRUEmbeddingCache:
    """
    LRU cache for embeddings with TTL eviction.
    Entries are evicted by LRU order when capacity is exceeded,
    and by TTL when they expire.
    """

    def __init__(
        self,
        max_entries: int = 50000,
        default_ttl_seconds: float = 86400.0,
        cleanup_interval_seconds: float = 300.0,
    ):
        self._max = max_entries
        self._default_ttl = default_ttl_seconds
        self._cleanup_interval = cleanup_interval_seconds
        self._cache: OrderedDict = OrderedDict()
        self._lock = Lock()
        self._hits = 0
        self._misses = 0
        self._evictions = 0
        self._last_cleanup = time.time()

    def get(self, key: str) -> Optional[List[float]]:
        with self._lock:
            if key not in self._cache:
                self._misses += 1
                return None
            entry: EmbeddingCacheEntry = self._cache[key]
            if entry.is_expired():
                del self._cache[key]
                self._evictions += 1
                self._misses += 1
                return None
            # Move to end (most recently used)
            self._cache.move_to_end(key)
            entry.touch()
            self._hits += 1
            return entry.embedding

    def put(
        self,
        key: str,
        entry: EmbeddingCacheEntry,
    ) -> None:
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                self._cache[key] = entry
                return

            if len(self._cache) >= self._max:
                # Evict least recently used
                self._cache.popitem(last=False)
                self._evictions += 1

            self._cache[key] = entry
            self._maybe_cleanup()

    def _maybe_cleanup(self) -> None:
        now = time.time()
        if now - self._last_cleanup < self._cleanup_interval:
            return
        self._last_cleanup = now
        expired = [k for k, e in self._cache.items() if e.is_expired()]
        for k in expired:
            del self._cache[k]
            self._evictions += 1

    def hit_rate(self) -> float:
        total = self._hits + self._misses
        return self._hits / total if total > 0 else 0.0

    def stats(self) -> dict:
        with self._lock:
            return {
                "size": len(self._cache),
                "max_size": self._max,
                "hits": self._hits,
                "misses": self._misses,
                "evictions": self._evictions,
                "hit_rate": round(self.hit_rate(), 4),
            }
```

## Solution 3: Cached Embedding Client

```python
import time
from typing import Callable, List, Optional


class CachedEmbeddingClient:
    """
    Wraps an embedding API client with transparent caching.
    Cache hits skip the API call entirely; misses populate the cache.
    Supports per-content-type TTL overrides.
    """

    CONTENT_TYPE_TTLS = {
        "document": 604800.0,   # 7 days — stable content
        "query": 3600.0,        # 1 hour — user queries change more
        "system_prompt": 2592000.0,  # 30 days — very stable
    }

    def __init__(
        self,
        cache: LRUEmbeddingCache,
        embed_fn: Callable[[str], List[float]],
        model_id: str = "text-embedding-ada-002",
    ):
        self._cache = cache
        self._embed_fn = embed_fn
        self._model_id = model_id
        self._api_calls = 0
        self._total_calls = 0

    async def embed(
        self,
        text: str,
        content_type: str = "query",
        force_refresh: bool = False,
    ) -> List[float]:
        self._total_calls += 1
        key = EmbeddingCacheEntry.make_key(text, self._model_id)

        if not force_refresh:
            cached = self._cache.get(key)
            if cached is not None:
                return cached

        # Cache miss — call the API
        self._api_calls += 1
        start = time.time()
        embedding = await self._embed_fn(text)
        latency_ms = round((time.time() - start) * 1000, 2)

        ttl = self.CONTENT_TYPE_TTLS.get(content_type, 86400.0)
        entry = EmbeddingCacheEntry(
            text_hash=key,
            embedding=embedding,
            model_id=self._model_id,
            text_length=len(text),
            ttl_seconds=ttl,
        )
        self._cache.put(key, entry)
        return embedding

    async def embed_batch(
        self,
        texts: list,
        content_type: str = "document",
    ) -> List[List[float]]:
        results = [None] * len(texts)
        uncached_indices = []
        uncached_texts = []

        for i, text in enumerate(texts):
            key = EmbeddingCacheEntry.make_key(text, self._model_id)
            cached = self._cache.get(key)
            if cached is not None:
                results[i] = cached
            else:
                uncached_indices.append(i)
                uncached_texts.append(text)

        if uncached_texts:
            self._api_calls += len(uncached_texts)
            embeddings = await self._batch_embed_fn(uncached_texts)
            ttl = self.CONTENT_TYPE_TTLS.get(content_type, 86400.0)
            for idx, (text, emb) in zip(uncached_indices, zip(uncached_texts, embeddings)):
                key = EmbeddingCacheEntry.make_key(text, self._model_id)
                entry = EmbeddingCacheEntry(
                    text_hash=key, embedding=emb,
                    model_id=self._model_id, text_length=len(text), ttl_seconds=ttl,
                )
                self._cache.put(key, entry)
                results[idx] = emb

        return results

    async def _batch_embed_fn(self, texts: list) -> List[List[float]]:
        import asyncio
        return await asyncio.gather(*[self._embed_fn(t) for t in texts])

    def api_call_rate(self) -> float:
        return self._api_calls / max(self._total_calls, 1)

    def stats(self) -> dict:
        return {
            "total_calls": self._total_calls,
            "api_calls": self._api_calls,
            "api_call_rate": round(self.api_call_rate(), 4),
            "cache_stats": self._cache.stats(),
        }
```

## Solution 4: Cache Persistence Manager

```python
import json
import time
from pathlib import Path
from typing import List


class EmbeddingCachePersistenceManager:
    """
    Saves and loads the embedding cache to disk so warm-up
    state survives agent restarts.
    """

    def __init__(self, cache: LRUEmbeddingCache, path: str = "/tmp/embedding_cache.json"):
        self._cache = cache
        self._path = Path(path)

    def save(self, max_entries: int = 10000) -> int:
        entries = []
        with self._cache._lock:
            items = list(self._cache._cache.items())[-max_entries:]
            for key, entry in items:
                if not entry.is_expired():
                    entries.append({
                        "key": key,
                        "embedding": entry.embedding[:100],  # save partial for space
                        "model_id": entry.model_id,
                        "text_length": entry.text_length,
                        "created_at": entry.created_at,
                        "ttl_seconds": entry.ttl_seconds,
                        "access_count": entry.access_count,
                    })

        self._path.write_text(json.dumps(entries))
        return len(entries)

    def load(self) -> int:
        if not self._path.exists():
            return 0
        try:
            entries = json.loads(self._path.read_text())
        except Exception:
            return 0

        loaded = 0
        for data in entries:
            entry = EmbeddingCacheEntry(
                text_hash=data["key"],
                embedding=data["embedding"],
                model_id=data["model_id"],
                text_length=data["text_length"],
                created_at=data["created_at"],
                ttl_seconds=data["ttl_seconds"],
                access_count=data.get("access_count", 0),
            )
            if not entry.is_expired():
                self._cache.put(data["key"], entry)
                loaded += 1
        return loaded
```

## Solution 5: Cache Efficiency Analyzer

```python
import time
from typing import List


class EmbeddingCacheEfficiencyAnalyzer:
    """
    Analyzes cache usage patterns to recommend optimal TTL and max_entries settings.
    """

    def __init__(self, cache: LRUEmbeddingCache, client: CachedEmbeddingClient):
        self._cache = cache
        self._client = client

    def analyze(self) -> dict:
        cache_stats = self._cache.stats()
        client_stats = self._client.stats()
        hit_rate = cache_stats["hit_rate"]
        api_rate = client_stats["api_call_rate"]

        recommendations = []
        if hit_rate < 0.3:
            recommendations.append("Low hit rate (<30%) — consider increasing max_entries or TTL")
        if cache_stats["size"] >= cache_stats["max_size"] * 0.95:
            recommendations.append("Cache near capacity — increase max_entries to reduce evictions")
        if cache_stats["evictions"] > cache_stats["hits"] * 0.1:
            recommendations.append("High eviction rate — max_entries may be too small")

        api_cost_saved = client_stats["total_calls"] - client_stats["api_calls"]
        return {
            "hit_rate": hit_rate,
            "api_call_rate": api_rate,
            "estimated_api_calls_saved": api_cost_saved,
            "cache_size": cache_stats["size"],
            "recommendations": recommendations,
        }
```

## Solution 6: Embedding Cache Dashboard

```python
import time


class EmbeddingCacheDashboard:
    """
    Combines cache stats, client stats, and efficiency analysis
    into a single cost optimization health view.
    """

    def __init__(
        self,
        cache: LRUEmbeddingCache,
        client: CachedEmbeddingClient,
        analyzer: EmbeddingCacheEfficiencyAnalyzer,
    ):
        self._cache = cache
        self._client = client
        self._analyzer = analyzer

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "cache_stats": self._cache.stats(),
            "client_stats": self._client.stats(),
            "efficiency": self._analyzer.analyze(),
        }
```

## Comparison

| Approach | LRU Eviction | TTL Expiry | Batch Support | Persistence | Efficiency Analysis |
|---|---|---|---|---|---|
| LRUEmbeddingCache | Yes | Yes | No | No | No |
| CachedEmbeddingClient | Via cache | Via cache | Yes | No | No |
| EmbeddingCachePersistenceManager | No | Via entries | No | Yes | No |
| EmbeddingCacheEfficiencyAnalyzer | No | No | No | No | Yes |
| EmbeddingCacheDashboard | No | No | No | No | Yes (combined) |

**Best for production**: Set TTL by content type — `system_prompt` embeddings can be cached for 30 days, document chunk embeddings for 7 days, and user query embeddings for 1 hour. Use SHA-256 of normalized (lowercased, whitespace-collapsed) text as the cache key — this collapses near-identical queries like "What is X?" and "what is x?" into a single entry. Set `max_entries=50,000` as a starting point: at 1536 dimensions × 4 bytes × 50,000 entries = 307MB, which is manageable for most deployments. Load the cache from disk at startup using `EmbeddingCachePersistenceManager.load()` to avoid cold-start latency on commonly-embedded content.
