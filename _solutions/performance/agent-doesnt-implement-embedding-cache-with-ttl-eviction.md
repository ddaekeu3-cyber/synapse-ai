---
title: "Agent Doesn't Implement Embedding Cache with TTL Eviction"
description: "Agents that call the embedding API on every retrieval request pay per-call latency and cost for identical or near-identical strings that were embedded moments ago. Implement an embedding cache with TTL eviction and LRU overflow policy that returns cached vectors for repeated strings, reducing embedding API calls by 60–90% in typical conversational workloads."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-embedding-cache-with-ttl-eviction
tags: [embedding-cache, ttl-eviction, lru-cache, vector-cache, api-cost-reduction, retrieval-performance]
symptoms:
  - "Embedding API is called for the same query string multiple times within a session"
  - "Retrieval latency dominated by embedding call even though query text hasn't changed"
  - "No reuse of embeddings across turns for repeated sub-queries or shared filter terms"
  - "Embedding API cost scales linearly with turns even for repetitive conversations"
  - "Cache hit rate is never measured — every embedding call is treated as a cold call"
---

## Why This Happens

Embedding a string is deterministic: the same model and input always produce the same vector. Yet most RAG implementations call the embedding API unconditionally on every retrieval request, including when the same query string appears in the same session, across repeated tool calls, or when shared filter terms are embedded separately by each tool. A TTL cache prevents stale vectors from persisting after a model version change, while LRU overflow bounding prevents unbounded memory growth.

## Solution 1: Embedding Cache Entry

```python
import hashlib
import time
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class EmbeddingCacheEntry:
    cache_key: str
    vector: List[float]
    model: str
    input_text: str
    created_at: float = field(default_factory=time.time)
    last_accessed_at: float = field(default_factory=time.time)
    access_count: int = 0

    def is_expired(self, ttl_seconds: float) -> bool:
        return time.time() - self.created_at > ttl_seconds

    def touch(self) -> None:
        self.last_accessed_at = time.time()
        self.access_count += 1

    @staticmethod
    def make_key(text: str, model: str) -> str:
        payload = f"{model}:{text}"
        return hashlib.sha256(payload.encode()).hexdigest()
```

## Solution 2: TTL + LRU Embedding Cache

```python
import time
from collections import OrderedDict
from threading import Lock
from typing import Dict, List, Optional


class TTLLRUEmbeddingCache:
    """
    Embedding vector cache with TTL expiry and LRU eviction.
    Thread-safe for use across concurrent retrieval calls.
    """

    def __init__(
        self,
        max_entries: int = 2000,
        ttl_seconds: float = 3600.0,
    ):
        self._max = max_entries
        self._ttl = ttl_seconds
        self._store: OrderedDict[str, EmbeddingCacheEntry] = OrderedDict()
        self._lock = Lock()
        self._hits = 0
        self._misses = 0

    def get(self, text: str, model: str) -> Optional[List[float]]:
        key = EmbeddingCacheEntry.make_key(text, model)
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                self._misses += 1
                return None
            if entry.is_expired(self._ttl):
                del self._store[key]
                self._misses += 1
                return None
            # LRU: move to end (most recently used)
            self._store.move_to_end(key)
            entry.touch()
            self._hits += 1
            return entry.vector

    def put(self, text: str, model: str, vector: List[float]) -> None:
        key = EmbeddingCacheEntry.make_key(text, model)
        with self._lock:
            if key in self._store:
                self._store.move_to_end(key)
                self._store[key].vector = vector
                self._store[key].created_at = time.time()
                return
            # Evict LRU entries if at capacity
            while len(self._store) >= self._max:
                self._store.popitem(last=False)
            self._store[key] = EmbeddingCacheEntry(
                cache_key=key,
                vector=vector,
                model=model,
                input_text=text[:200],
            )

    def invalidate_model(self, model: str) -> int:
        """Remove all entries for a specific model (e.g., after model version upgrade)."""
        with self._lock:
            to_remove = [k for k, v in self._store.items() if v.model == model]
            for k in to_remove:
                del self._store[k]
            return len(to_remove)

    def stats(self) -> dict:
        with self._lock:
            total = self._hits + self._misses
            return {
                "entries": len(self._store),
                "max_entries": self._max,
                "ttl_seconds": self._ttl,
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": round(self._hits / total, 4) if total else 0.0,
            }
```

## Solution 3: Batch Embedding Cache Client

```python
from typing import Any, Callable, Dict, List, Tuple


class BatchEmbeddingCacheClient:
    """
    Accepts a batch of strings for embedding.
    Returns cached vectors for strings already in the cache,
    calls the embed_fn only for cache misses, then stores new vectors.
    """

    def __init__(
        self,
        cache: TTLLRUEmbeddingCache,
        model: str,
    ):
        self._cache = cache
        self._model = model

    async def embed_many(
        self,
        texts: List[str],
        embed_fn: Callable[[List[str], str], List[List[float]]],
    ) -> List[List[float]]:
        results: Dict[int, List[float]] = {}
        miss_indices: List[int] = []
        miss_texts: List[str] = []

        for i, text in enumerate(texts):
            cached = self._cache.get(text, self._model)
            if cached is not None:
                results[i] = cached
            else:
                miss_indices.append(i)
                miss_texts.append(text)

        if miss_texts:
            new_vectors = await embed_fn(miss_texts, self._model)
            for i, (idx, text) in enumerate(zip(miss_indices, miss_texts)):
                self._cache.put(text, self._model, new_vectors[i])
                results[idx] = new_vectors[i]

        return [results[i] for i in range(len(texts))]

    async def embed_one(
        self,
        text: str,
        embed_fn: Callable[[List[str], str], List[List[float]]],
    ) -> List[float]:
        vectors = await self.embed_many([text], embed_fn)
        return vectors[0]
```

## Solution 4: Session-Scoped Embedding Warmer

```python
from typing import Any, Callable, List


class SessionEmbeddingWarmer:
    """
    Pre-warms the embedding cache at session start with common
    query prefixes, filter terms, and tool parameter strings
    that are likely to be embedded repeatedly during the session.
    """

    def __init__(self, cache_client: BatchEmbeddingCacheClient):
        self._client = cache_client
        self._warmed_sessions: set = set()

    async def warm(
        self,
        session_id: str,
        seed_texts: List[str],
        embed_fn: Callable,
    ) -> dict:
        if session_id in self._warmed_sessions:
            return {"status": "already_warmed", "session_id": session_id}

        vectors = await self._client.embed_many(seed_texts, embed_fn)
        self._warmed_sessions.add(session_id)

        return {
            "status": "warmed",
            "session_id": session_id,
            "texts_embedded": len(seed_texts),
            "cache_stats": self._client._cache.stats(),
        }

    def evict_session(self, session_id: str) -> None:
        self._warmed_sessions.discard(session_id)
```

## Solution 5: Embedding Cache Cost Estimator

```python
import time
from typing import List


class EmbeddingCacheCostEstimator:
    """
    Estimates API cost savings from embedding cache hits.
    Tracks tokens saved and estimated dollar savings.
    """

    def __init__(
        self,
        cost_per_million_tokens: float = 0.02,
        avg_tokens_per_text: float = 20.0,
    ):
        self._cost_per_million = cost_per_million_tokens
        self._avg_tokens = avg_tokens_per_text
        self._saved_calls: List[float] = []
        self._recorded_at: List[float] = []

    def record_cache_hit(self, text_length_chars: int = 0) -> None:
        tokens = max(text_length_chars / 4, self._avg_tokens)
        self._saved_calls.append(tokens)
        self._recorded_at.append(time.time())

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent_tokens = [
            t for t, ts in zip(self._saved_calls, self._recorded_at)
            if ts >= cutoff
        ]
        total_tokens = sum(recent_tokens)
        cost_saved = total_tokens / 1_000_000 * self._cost_per_million
        return {
            "window_seconds": window_seconds,
            "cache_hits": len(recent_tokens),
            "tokens_saved_est": round(total_tokens, 0),
            "cost_saved_usd_est": round(cost_saved, 6),
        }
```

## Solution 6: Embedding Cache Dashboard

```python
import time


class EmbeddingCacheDashboard:
    """
    Combines cache statistics, cost savings, and health indicators
    into a single operational snapshot.
    """

    def __init__(
        self,
        cache: TTLLRUEmbeddingCache,
        cost_estimator: EmbeddingCacheCostEstimator,
    ):
        self._cache = cache
        self._cost = cost_estimator

    def render(self) -> dict:
        stats = self._cache.stats()
        fill_pct = round(stats["entries"] / max(stats["max_entries"], 1) * 100, 1)
        return {
            "generated_at": time.time(),
            "cache": {
                **stats,
                "fill_pct": fill_pct,
                "health": "healthy" if stats["hit_rate"] > 0.5 else "low_hit_rate",
            },
            "cost_savings": self._cost.summary(window_seconds=3600.0),
        }
```

## Comparison

| Approach | TTL Expiry | LRU Eviction | Batch Miss Fill | Session Warm | Cost Tracking |
|---|---|---|---|---|---|
| TTLLRUEmbeddingCache | Yes | Yes | No | No | No |
| BatchEmbeddingCacheClient | Via cache | Via cache | Yes | No | No |
| SessionEmbeddingWarmer | No | No | Via client | Yes | No |
| EmbeddingCacheCostEstimator | No | No | No | No | Yes |
| EmbeddingCacheDashboard | No | No | No | No | Yes (aggregate) |

**Best for production**: Set `ttl_seconds=3600` (1 hour) and call `invalidate_model()` as part of your model version rollout automation — stale vectors from an old embedding model silently degrade retrieval quality. Use `BatchEmbeddingCacheClient.embed_many()` for all retrieval calls: batching miss fills reduces API round trips. Call `SessionEmbeddingWarmer.warm()` at session start with a corpus of common domain terms (product names, frequent query prefixes) — a 50-string warm-up typically raises session hit rates above 70% within the first 5 turns. Alert when `hit_rate < 0.30` in production: it indicates query diversity is too high for the current cache size and `max_entries` should be increased.
