---
title: "Agent Doesn't Implement Embedding Model Caching Across Requests"
description: "Agents that call the embedding API for every retrieval query re-embed identical or near-identical strings on every request, paying API costs and adding latency for results already computed. Implement an embedding cache that stores vectors keyed by normalized text, serves cache hits in microseconds, and persists the cache to disk so it survives restarts without a full re-warm."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-embedding-model-caching-across-requests
tags: [embedding-cache, vector-cache, api-cost-reduction, semantic-cache, lru-cache, embedding-persistence]
symptoms:
  - "Embedding API called for the same system prompt or tool description on every request"
  - "Retrieval latency dominated by embedding call even though query text rarely changes"
  - "Embedding API costs scale linearly with request volume despite repeated inputs"
  - "Cache is in-memory only — warm-up cost paid again after every restart"
  - "No measurement of cache hit rate — no visibility into redundant embedding calls"
---

## Why This Happens

Embedding models produce deterministic outputs: the same input text always produces the same vector. Despite this, agents typically call the embedding API on every request, even for strings that have not changed since the last call — system prompts, tool descriptions, few-shot examples, and common query prefixes. A keyed cache with normalized input text as the key serves these calls locally at zero cost. The cache is most effective for inputs with high repetition: system context and standard queries. It requires normalization (whitespace, unicode) so minor formatting differences do not create spurious cache misses.

## Solution 1: Embedding Cache Entry

```python
import hashlib
import time
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class EmbeddingCacheEntry:
    text_fingerprint: str        # SHA-256[:24] of normalized text
    embedding: List[float]
    model_id: str
    token_count: int
    created_at: float = field(default_factory=time.time)
    last_hit_at: float = field(default_factory=time.time)
    hit_count: int = 0

    def record_hit(self) -> None:
        self.hit_count += 1
        self.last_hit_at = time.time()

    def age_s(self) -> float:
        return time.time() - self.created_at

    def estimated_cost_saved(self, cost_per_million_tokens: float) -> float:
        return self.hit_count * self.token_count * cost_per_million_tokens / 1_000_000
```

## Solution 2: Text Normalizer

```python
import re
import unicodedata


class EmbeddingTextNormalizer:
    """
    Normalizes text before computing a cache key.
    Ensures minor whitespace or unicode differences do not cause cache misses.
    """

    @staticmethod
    def normalize(text: str) -> str:
        # Unicode normalization
        text = unicodedata.normalize("NFKC", text)
        # Collapse whitespace
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    @staticmethod
    def fingerprint(normalized_text: str, model_id: str) -> str:
        key = f"{model_id}::{normalized_text}"
        return hashlib.sha256(key.encode()).hexdigest()[:24]

    import hashlib
```

## Solution 3: LRU Embedding Cache

```python
import time
from collections import OrderedDict
from threading import Lock
from typing import Dict, List, Optional


class LRUEmbeddingCache:
    """
    In-memory LRU embedding cache with TTL-based expiry.
    Thread-safe for use across concurrent request handlers.
    """

    def __init__(
        self,
        max_entries: int = 10000,
        ttl_seconds: float = 86400.0,   # 24-hour default TTL
    ):
        self._max = max_entries
        self._ttl = ttl_seconds
        self._cache: OrderedDict[str, EmbeddingCacheEntry] = OrderedDict()
        self._lock = Lock()
        self._hits = 0
        self._misses = 0

    def get(self, fingerprint: str) -> Optional[EmbeddingCacheEntry]:
        with self._lock:
            entry = self._cache.get(fingerprint)
            if entry is None:
                self._misses += 1
                return None
            if entry.age_s() > self._ttl:
                del self._cache[fingerprint]
                self._misses += 1
                return None
            # Move to end (most recently used)
            self._cache.move_to_end(fingerprint)
            entry.record_hit()
            self._hits += 1
            return entry

    def put(self, fingerprint: str, entry: EmbeddingCacheEntry) -> None:
        with self._lock:
            if fingerprint in self._cache:
                self._cache.move_to_end(fingerprint)
                self._cache[fingerprint] = entry
                return
            if len(self._cache) >= self._max:
                # Evict least recently used
                self._cache.popitem(last=False)
            self._cache[fingerprint] = entry

    def stats(self) -> dict:
        with self._lock:
            total = self._hits + self._misses
            return {
                "entries": len(self._cache),
                "max_entries": self._max,
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": round(self._hits / max(total, 1), 4),
            }
```

## Solution 4: Persistent Embedding Cache

```python
import json
import os
from pathlib import Path
from threading import Lock
from typing import Dict, List, Optional


class PersistentEmbeddingCache:
    """
    Wraps LRUEmbeddingCache with disk persistence.
    Loads entries from disk at startup and flushes to disk on demand.
    Survives process restarts without requiring a full re-warm.
    """

    def __init__(
        self,
        memory_cache: LRUEmbeddingCache,
        persist_path: str = "/tmp/embedding_cache.json",
        max_persist_entries: int = 5000,
    ):
        self._cache = memory_cache
        self._path = Path(persist_path)
        self._max_persist = max_persist_entries
        self._lock = Lock()
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text())
            for fp, entry_data in data.items():
                entry = EmbeddingCacheEntry(
                    text_fingerprint=fp,
                    embedding=entry_data["embedding"],
                    model_id=entry_data["model_id"],
                    token_count=entry_data["token_count"],
                    created_at=entry_data.get("created_at", 0.0),
                    hit_count=entry_data.get("hit_count", 0),
                )
                self._cache.put(fp, entry)
        except Exception:
            pass  # corrupt cache — start fresh

    def flush(self) -> int:
        """Write current cache state to disk. Returns number of entries written."""
        with self._cache._lock:
            entries = list(self._cache._cache.items())

        # Keep the most recently used entries up to max_persist
        recent = entries[-self._max_persist:]
        data = {
            fp: {
                "embedding": entry.embedding,
                "model_id": entry.model_id,
                "token_count": entry.token_count,
                "created_at": entry.created_at,
                "hit_count": entry.hit_count,
            }
            for fp, entry in recent
        }
        try:
            self._path.write_text(json.dumps(data))
        except OSError:
            pass
        return len(data)

    def get(self, fingerprint: str) -> Optional[EmbeddingCacheEntry]:
        return self._cache.get(fingerprint)

    def put(self, fingerprint: str, entry: EmbeddingCacheEntry) -> None:
        self._cache.put(fingerprint, entry)
```

## Solution 5: Caching Embedding Client

```python
import time
from typing import Callable, List, Optional


class CachingEmbeddingClient:
    """
    Wraps an embedding API call with cache lookup and store.
    Returns cached vectors immediately; only calls the API on misses.
    """

    def __init__(
        self,
        cache: PersistentEmbeddingCache,
        normalizer: EmbeddingTextNormalizer,
        embed_fn: Callable,    # async (text: str, model_id: str) -> List[float]
        model_id: str,
        tokens_per_char: float = 0.25,
    ):
        self._cache = cache
        self._normalizer = normalizer
        self._embed_fn = embed_fn
        self._model_id = model_id
        self._tokens_per_char = tokens_per_char
        self._api_calls = 0
        self._api_tokens = 0

    async def embed(self, text: str) -> List[float]:
        normalized = self._normalizer.normalize(text)
        fingerprint = self._normalizer.fingerprint(normalized, self._model_id)

        entry = self._cache.get(fingerprint)
        if entry is not None:
            return entry.embedding

        # Cache miss — call the API
        start = time.time()
        vector = await self._embed_fn(normalized, self._model_id)
        latency_ms = round((time.time() - start) * 1000, 2)
        self._api_calls += 1
        token_count = max(1, int(len(normalized) * self._tokens_per_char))
        self._api_tokens += token_count

        new_entry = EmbeddingCacheEntry(
            text_fingerprint=fingerprint,
            embedding=vector,
            model_id=self._model_id,
            token_count=token_count,
        )
        self._cache.put(fingerprint, new_entry)
        return vector

    async def embed_batch(self, texts: List[str]) -> List[List[float]]:
        return [await self.embed(t) for t in texts]

    def stats(self) -> dict:
        return {
            "api_calls": self._api_calls,
            "api_tokens": self._api_tokens,
            "cache_stats": self._cache._cache.stats(),
        }
```

## Solution 6: Embedding Cache Dashboard

```python
import time


class EmbeddingCacheDashboard:
    """
    Combines cache stats and API call metrics into a cost-savings view.
    """

    def __init__(
        self,
        client: CachingEmbeddingClient,
        cost_per_million_tokens: float = 0.10,
    ):
        self._client = client
        self._cost = cost_per_million_tokens

    def render(self) -> dict:
        client_stats = self._client.stats()
        cache_stats = client_stats["cache_stats"]
        api_tokens = client_stats["api_tokens"]
        hit_rate = cache_stats["hit_rate"]
        hits = cache_stats["hits"]
        misses = cache_stats["misses"]

        # Estimate tokens that would have been billed without cache
        total_requests = hits + misses
        avg_tokens_per_call = api_tokens / max(misses, 1)
        saved_tokens = int(hits * avg_tokens_per_call)
        saved_cost = round(saved_tokens * self._cost / 1_000_000, 4)

        return {
            "generated_at": time.time(),
            "cache": cache_stats,
            "api_calls_made": client_stats["api_calls"],
            "api_tokens_billed": api_tokens,
            "estimated_saved_tokens": saved_tokens,
            "estimated_saved_cost_usd": saved_cost,
            "effective_hit_rate": hit_rate,
        }
```

## Comparison

| Approach | LRU Eviction | TTL Expiry | Disk Persistence | Batch Embedding | Cost Estimation |
|---|---|---|---|---|---|
| LRUEmbeddingCache | Yes | Yes | No | No | No |
| PersistentEmbeddingCache | Via memory cache | Via memory cache | Yes | No | No |
| CachingEmbeddingClient | Via cache | Via cache | Via cache | Yes | No |
| EmbeddingCacheDashboard | No | No | No | No | Yes |

**Best for production**: Flush `PersistentEmbeddingCache` to disk every 5 minutes and on graceful shutdown — losing the cache on restart adds cold-start latency and API cost. Normalize text before fingerprinting to collapse whitespace variants that would otherwise cause spurious misses. Set TTL to 24 hours for system prompts and tool descriptions (stable) and 1 hour for user query embeddings (diverse). Monitor `hit_rate` — below 40% on a production system means the query distribution is highly varied and the cache provides limited benefit; above 70% means the cache is covering most requests and flush frequency should be increased to protect warm state across restarts.
