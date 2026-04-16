---
title: "Agent Doesn't Implement Embedding Model Result Caching"
description: "Agents that call an embedding model on every request re-embed identical or near-identical strings repeatedly: the same tool description embedded for every planning step, the same user query embedded twice for retrieval and reranking, the same document chunk re-embedded across sessions. Implement embedding result caching with exact-match and normalized-key lookups to eliminate redundant embedding calls and reduce both latency and cost."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-embedding-model-result-caching
tags: [embedding-cache, vector-cache, token-cost, latency-reduction, cache-hit-rate, rag-optimization]
symptoms:
  - "Embedding model called identically on every agent planning loop iteration"
  - "Same document chunk embedded multiple times across overlapping retrieval queries"
  - "Tool descriptions re-embedded on every conversation turn"
  - "Embedding API cost scales linearly with request count despite repeated inputs"
  - "No measurement of embedding cache hit rate or token savings"
---

## Why This Happens

Embedding calls are treated as stateless API calls: the same string in means the same vector out, but without a cache the call goes to the model every time. Agent frameworks that call embedding models inside planning loops, retrieval pipelines, and reranking steps can easily embed the same string dozens of times per session. A simple content-keyed cache with LRU eviction and TTL eliminates the vast majority of these calls because embedding inputs are highly repetitive — tool descriptions never change, document chunks change rarely, and user queries within a session often repeat.

## Solution 1: Embedding Cache Entry

```python
import time
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class EmbeddingCacheEntry:
    text: str
    vector: List[float]
    model: str
    created_at: float = field(default_factory=time.time)
    hit_count: int = 0
    token_count: Optional[int] = None

    def is_expired(self, ttl_seconds: float) -> bool:
        return time.time() - self.created_at > ttl_seconds

    def record_hit(self) -> None:
        self.hit_count += 1
```

## Solution 2: LRU Embedding Cache

```python
import hashlib
import time
from collections import OrderedDict
from threading import Lock
from typing import Dict, List, Optional, Tuple


class LRUEmbeddingCache:
    """
    Thread-safe LRU cache for embedding vectors keyed by (normalized_text, model).
    Evicts least-recently-used entries when capacity is reached.
    """

    def __init__(
        self,
        max_entries: int = 10000,
        ttl_seconds: float = 3600.0,
    ):
        self._max = max_entries
        self._ttl = ttl_seconds
        self._cache: OrderedDict[str, EmbeddingCacheEntry] = OrderedDict()
        self._lock = Lock()
        self._hits = 0
        self._misses = 0

    @staticmethod
    def _cache_key(text: str, model: str) -> str:
        normalized = " ".join(text.lower().split())
        return hashlib.sha256(f"{model}:{normalized}".encode()).hexdigest()

    def get(self, text: str, model: str) -> Optional[List[float]]:
        key = self._cache_key(text, model)
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                self._misses += 1
                return None
            if entry.is_expired(self._ttl):
                del self._cache[key]
                self._misses += 1
                return None
            self._cache.move_to_end(key)
            entry.record_hit()
            self._hits += 1
            return entry.vector

    def put(
        self,
        text: str,
        model: str,
        vector: List[float],
        token_count: Optional[int] = None,
    ) -> None:
        key = self._cache_key(text, model)
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                return
            if len(self._cache) >= self._max:
                self._cache.popitem(last=False)
            self._cache[key] = EmbeddingCacheEntry(
                text=text,
                vector=vector,
                model=model,
                token_count=token_count,
            )

    def stats(self) -> dict:
        with self._lock:
            total = self._hits + self._misses
            return {
                "entries": len(self._cache),
                "capacity": self._max,
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": round(self._hits / total, 4) if total else 0.0,
            }
```

## Solution 3: Cached Embedding Client

```python
from typing import Any, Callable, List, Optional


class CachedEmbeddingClient:
    """
    Wraps any embedding function with LRU caching.
    Falls back to the underlying model on cache miss and populates the cache.
    """

    def __init__(
        self,
        embed_fn: Callable[[str, str], Any],  # (text, model) -> vector
        cache: LRUEmbeddingCache,
        default_model: str = "text-embedding-3-small",
    ):
        self._embed_fn = embed_fn
        self._cache = cache
        self._model = default_model
        self._tokens_saved = 0

    async def embed(
        self,
        text: str,
        model: Optional[str] = None,
    ) -> List[float]:
        effective_model = model or self._model
        cached = self._cache.get(text, effective_model)
        if cached is not None:
            return cached

        vector = await self._embed_fn(text, effective_model)
        estimated_tokens = max(1, len(text) // 4)
        self._cache.put(text, effective_model, vector, token_count=estimated_tokens)
        return vector

    async def embed_batch(
        self,
        texts: list,
        model: Optional[str] = None,
    ) -> List[List[float]]:
        effective_model = model or self._model
        results = []
        uncached_indices = []
        uncached_texts = []

        for i, text in enumerate(texts):
            cached = self._cache.get(text, effective_model)
            if cached is not None:
                results.append((i, cached))
            else:
                uncached_indices.append(i)
                uncached_texts.append(text)

        for text in uncached_texts:
            vector = await self._embed_fn(text, effective_model)
            self._cache.put(text, effective_model, vector)

        all_vectors = [None] * len(texts)
        for i, vec in results:
            all_vectors[i] = vec
        for idx, text in zip(uncached_indices, uncached_texts):
            all_vectors[idx] = self._cache.get(text, effective_model)

        return all_vectors

    def cache_stats(self) -> dict:
        return self._cache.stats()
```

## Solution 4: Embedding Cache Warmer

```python
import asyncio
from typing import Any, Callable, List, Optional


class EmbeddingCacheWarmer:
    """
    Pre-populates the embedding cache with known-stable strings
    (tool descriptions, system prompt segments, static document chunks)
    before the first user request arrives.
    """

    def __init__(
        self,
        client: CachedEmbeddingClient,
        concurrency: int = 5,
    ):
        self._client = client
        self._concurrency = concurrency
        self._warmed = 0

    async def warm(self, texts: List[str], model: Optional[str] = None) -> dict:
        sem = asyncio.Semaphore(self._concurrency)

        async def _warm_one(text: str) -> None:
            async with sem:
                await self._client.embed(text, model)
                self._warmed += 1

        await asyncio.gather(*[_warm_one(t) for t in texts])
        return {
            "texts_warmed": len(texts),
            "total_warmed": self._warmed,
            "cache_stats": self._client.cache_stats(),
        }
```

## Solution 5: Per-Scope Embedding Cache Namespace

```python
import hashlib
from typing import List, Optional


class NamespacedEmbeddingCache:
    """
    Wraps an LRUEmbeddingCache with a namespace prefix so that
    different scopes (session, tenant, tool-registry) share one
    backing cache without key collisions.
    """

    def __init__(self, backing_cache: LRUEmbeddingCache, namespace: str):
        self._cache = backing_cache
        self._ns = namespace

    def _ns_text(self, text: str) -> str:
        return f"[{self._ns}]{text}"

    def get(self, text: str, model: str) -> Optional[List[float]]:
        return self._cache.get(self._ns_text(text), model)

    def put(
        self,
        text: str,
        model: str,
        vector: List[float],
        token_count: Optional[int] = None,
    ) -> None:
        self._cache.put(self._ns_text(text), model, vector, token_count)

    def stats(self) -> dict:
        return {"namespace": self._ns, **self._cache.stats()}
```

## Solution 6: Embedding Cache Cost Monitor

```python
import time
from threading import Lock
from typing import List


class EmbeddingCacheCostMonitor:
    """
    Tracks token savings from cache hits over time.
    Estimates API cost avoided based on a configurable price per token.
    """

    def __init__(
        self,
        tokens_per_char: float = 0.25,
        cost_per_million_tokens: float = 0.02,  # USD, e.g. text-embedding-3-small
    ):
        self._tokens_per_char = tokens_per_char
        self._cost_per_m = cost_per_million_tokens
        self._samples: List[dict] = []
        self._lock = Lock()

    def record_snapshot(self, cache_stats: dict) -> None:
        with self._lock:
            self._samples.append({
                "ts": time.time(),
                **cache_stats,
            })
            if len(self._samples) > 5000:
                self._samples.pop(0)

    def estimate_savings(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        with self._lock:
            recent = [s for s in self._samples if s["ts"] >= cutoff]

        if not recent:
            return {"window_seconds": window_seconds, "samples": 0}

        latest = recent[-1]
        hits = latest.get("hits", 0)
        hit_rate = latest.get("hit_rate", 0.0)
        total_calls = hits + latest.get("misses", 0)

        avg_tokens_per_call = 200  # estimate
        tokens_saved = hits * avg_tokens_per_call
        cost_saved = tokens_saved / 1_000_000 * self._cost_per_m

        return {
            "window_seconds": window_seconds,
            "total_calls": total_calls,
            "cache_hits": hits,
            "hit_rate": hit_rate,
            "estimated_tokens_saved": tokens_saved,
            "estimated_cost_saved_usd": round(cost_saved, 6),
        }
```

## Comparison

| Approach | LRU Eviction | TTL Expiry | Batch Embed | Cache Warming | Cost Tracking |
|---|---|---|---|---|---|
| LRUEmbeddingCache | Yes | Yes | No | No | No |
| CachedEmbeddingClient | Via cache | Via cache | Yes (partial) | No | No |
| EmbeddingCacheWarmer | No | No | Via client | Yes | No |
| NamespacedEmbeddingCache | Via backing | Via backing | No | No | No |
| EmbeddingCacheCostMonitor | No | No | No | No | Yes |

**Best for production**: Set `max_entries=50000` and `ttl_seconds=86400` for tool descriptions and static document chunks — these never change and a 24-hour TTL is safe. Use `EmbeddingCacheWarmer` at startup to pre-populate tool descriptions before the first request; cold-start embedding latency for 50 tool descriptions at 50ms each adds 2.5 seconds that can be entirely eliminated. Monitor `hit_rate` via `EmbeddingCacheCostMonitor`: for a typical RAG pipeline, a hit rate below 40% means query diversity is high and the cache size should be increased, not reduced. Use `NamespacedEmbeddingCache` when different tenants should not share cached embeddings for privacy reasons.
