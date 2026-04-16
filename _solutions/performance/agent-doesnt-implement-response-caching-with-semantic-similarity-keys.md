---
title: "Agent Doesn't Implement Response Caching with Semantic Similarity Keys"
description: "Agents that use exact-match caching miss the high cache hit rates achievable when semantically equivalent queries — 'What is the capital of France?' and 'Tell me the capital city of France' — are treated as the same request. Implement semantic similarity caching that embeds queries, retrieves cached responses for near-duplicate queries above a similarity threshold, and falls back to a live LLM call only for genuinely novel questions."
date: 2026-04-16
difficulty: advanced
category: performance
slug: agent-doesnt-implement-response-caching-with-semantic-similarity-keys
tags: [semantic-cache, similarity-search, embedding-cache, response-reuse, cache-hit-rate, llm-cost-reduction]
symptoms:
  - "Identical queries with minor wording variations each trigger separate LLM calls"
  - "FAQ-style questions are answered fresh on every call despite having stable answers"
  - "Cache hit rate is near zero because exact string matching rarely matches real queries"
  - "High LLM costs for workloads where many queries are semantically equivalent"
  - "No measurement of semantic similarity between queries to quantify reuse potential"
---

## Why This Happens

Exact-match caching (key = hash of input string) is simple but has near-zero hit rate on natural language inputs — users phrase the same question differently every time. Semantic caching uses an embedding model to map queries to a vector space where semantically similar queries are geometrically close. A cached response is returned when a new query's embedding is within a cosine similarity threshold of a cached entry's embedding. The threshold controls the quality-vs-hit-rate tradeoff: lower thresholds return more cache hits but risk serving stale or slightly-mismatched responses.

## Solution 1: Semantic Cache Entry

```python
import time
from dataclasses import dataclass, field
from typing import Any, List, Optional


@dataclass
class SemanticCacheEntry:
    cache_id: str
    query_text: str
    query_embedding: List[float]
    response: Any                   # the cached response value
    created_at: float = field(default_factory=time.time)
    last_hit_at: Optional[float] = None
    hit_count: int = 0
    ttl_seconds: Optional[float] = None

    def is_expired(self) -> bool:
        if self.ttl_seconds is None:
            return False
        return time.time() - self.created_at > self.ttl_seconds

    def record_hit(self) -> None:
        self.last_hit_at = time.time()
        self.hit_count += 1
```

## Solution 2: Embedding Index

```python
import math
import uuid
from threading import Lock
from typing import List, Optional, Tuple


class EmbeddingIndex:
    """
    Linear scan embedding index for semantic cache lookups.
    Replace with FAISS or Annoy for >10k cached entries.
    """

    def __init__(self):
        self._entries: List[SemanticCacheEntry] = []
        self._lock = Lock()

    def add(self, entry: SemanticCacheEntry) -> None:
        with self._lock:
            self._entries.append(entry)

    def search(
        self,
        query_embedding: List[float],
        top_k: int = 1,
        min_similarity: float = 0.92,
    ) -> List[Tuple[SemanticCacheEntry, float]]:
        with self._lock:
            active = [e for e in self._entries if not e.is_expired()]
        results = []
        for entry in active:
            sim = self._cosine(query_embedding, entry.query_embedding)
            if sim >= min_similarity:
                results.append((entry, sim))
        results.sort(key=lambda x: x[1], reverse=True)
        return results[:top_k]

    def remove_expired(self) -> int:
        with self._lock:
            before = len(self._entries)
            self._entries = [e for e in self._entries if not e.is_expired()]
            return before - len(self._entries)

    def size(self) -> int:
        with self._lock:
            return len(self._entries)

    @staticmethod
    def _cosine(a: List[float], b: List[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(x * x for x in b))
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)
```

## Solution 3: Semantic Cache

```python
import time
import uuid
from typing import Any, Callable, List, Optional, Tuple


class SemanticCache:
    """
    Cache that uses embedding similarity for key matching.
    Stores query embeddings and associated responses.
    """

    def __init__(
        self,
        index: EmbeddingIndex,
        similarity_threshold: float = 0.92,
        default_ttl_seconds: Optional[float] = 3600.0,
        max_entries: int = 5000,
    ):
        self._index = index
        self._threshold = similarity_threshold
        self._default_ttl = default_ttl_seconds
        self._max_entries = max_entries
        self._hits = 0
        self._misses = 0

    def get(
        self,
        query_embedding: List[float],
    ) -> Optional[Tuple[Any, float]]:
        """
        Returns (cached_response, similarity) if a match is found, else None.
        """
        results = self._index.search(
            query_embedding,
            top_k=1,
            min_similarity=self._threshold,
        )
        if not results:
            self._misses += 1
            return None
        entry, similarity = results[0]
        entry.record_hit()
        self._hits += 1
        return entry.response, similarity

    def put(
        self,
        query_text: str,
        query_embedding: List[float],
        response: Any,
        ttl_seconds: Optional[float] = None,
    ) -> SemanticCacheEntry:
        if self._index.size() >= self._max_entries:
            evicted = self._index.remove_expired()
            # If nothing expired, we simply stop adding (LRU eviction would go here)
        entry = SemanticCacheEntry(
            cache_id=uuid.uuid4().hex[:12],
            query_text=query_text,
            query_embedding=query_embedding,
            response=response,
            ttl_seconds=ttl_seconds or self._default_ttl,
        )
        self._index.add(entry)
        return entry

    def hit_rate(self) -> float:
        total = self._hits + self._misses
        if total == 0:
            return 0.0
        return round(self._hits / total, 4)

    def stats(self) -> dict:
        return {
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": self.hit_rate(),
            "entries": self._index.size(),
            "threshold": self._threshold,
        }
```

## Solution 4: Semantic Cache Client

```python
from typing import Any, Callable, List, Optional


class SemanticCacheClient:
    """
    Wraps an LLM call with semantic cache lookup and population.
    Callers call .query() — cache hit returns immediately, miss calls the LLM.
    """

    def __init__(
        self,
        cache: SemanticCache,
        embed_fn: Callable[[str], List[float]],  # async fn(text) -> embedding
    ):
        self._cache = cache
        self._embed_fn = embed_fn

    async def query(
        self,
        query_text: str,
        llm_fn: Callable,
        *args: Any,
        ttl_seconds: Optional[float] = None,
        **kwargs: Any,
    ) -> tuple:
        """
        Returns (response, cache_hit, similarity).
        """
        embedding = await self._embed_fn(query_text)
        cached = self._cache.get(embedding)
        if cached is not None:
            response, similarity = cached
            return response, True, similarity

        response = await llm_fn(*args, **kwargs)
        self._cache.put(
            query_text=query_text,
            query_embedding=embedding,
            response=response,
            ttl_seconds=ttl_seconds,
        )
        return response, False, 0.0
```

## Solution 5: Threshold Calibrator

```python
import math
from typing import List, Tuple


class SemanticCacheThresholdCalibrator:
    """
    Helps calibrate the similarity threshold by analyzing the distribution
    of cosine similarities between a sample of query pairs.
    Pairs with known identical intent should exceed the threshold;
    pairs with different intent should fall below it.
    """

    def __init__(self, index: EmbeddingIndex):
        self._index = index

    def recommend_threshold(
        self,
        same_intent_pairs: List[Tuple[List[float], List[float]]],
        different_intent_pairs: List[Tuple[List[float], List[float]]],
    ) -> dict:
        same_sims = [
            EmbeddingIndex._cosine(a, b)
            for a, b in same_intent_pairs
        ]
        diff_sims = [
            EmbeddingIndex._cosine(a, b)
            for a, b in different_intent_pairs
        ]

        if not same_sims or not diff_sims:
            return {"status": "insufficient_data"}

        same_min = min(same_sims)
        diff_max = max(diff_sims)
        separation = same_min - diff_max

        # Threshold at the midpoint of the gap
        recommended = round((same_min + diff_max) / 2, 3) if separation > 0 else 0.92

        return {
            "same_intent_min_similarity": round(same_min, 4),
            "different_intent_max_similarity": round(diff_max, 4),
            "separation": round(separation, 4),
            "recommended_threshold": recommended,
            "separable": separation > 0,
        }
```

## Solution 6: Semantic Cache Dashboard

```python
import time


class SemanticCacheDashboard:
    """
    Renders cache performance and threshold calibration recommendations.
    """

    def __init__(
        self,
        cache: SemanticCache,
        client: SemanticCacheClient,
    ):
        self._cache = cache
        self._client = client

    def render(self) -> dict:
        stats = self._cache.stats()
        return {
            "generated_at": time.time(),
            "cache_stats": stats,
            "estimated_llm_calls_saved": stats["hits"],
        }
```

## Comparison

| Approach | Embedding Lookup | Similarity Threshold | TTL Support | Hit Rate Tracking | Threshold Calibration |
|---|---|---|---|---|---|
| EmbeddingIndex | Yes (linear scan) | Yes | Via expiry | No | No |
| SemanticCache | Via index | Yes | Yes | Yes | No |
| SemanticCacheClient | Via cache | Via cache | Via cache | No | No |
| SemanticCacheThresholdCalibrator | No | No | No | No | Yes |
| SemanticCacheDashboard | No | No | No | No | No |

**Best for production**: Start with `similarity_threshold=0.95` and lower it only after measuring false positive rates — a threshold too low will return cached responses for queries with subtly different intent. Replace the linear scan `EmbeddingIndex` with a vector database (Pinecone, Weaviate, pgvector) when the cache grows beyond 5,000 entries — linear scan at 50k entries adds ~50ms per lookup. Set TTL based on answer stability: factual questions can cache for 24 hours, real-time data queries (stock prices, weather) should cache for minutes or not at all. Use `SemanticCacheThresholdCalibrator` with a labeled pair dataset from your specific domain — threshold requirements vary significantly between customer support (high similarity tolerance) and medical queries (low tolerance for semantic approximations).
