---
title: "Agent Doesn't Implement Semantic Cache for Similar LLM Queries"
description: "Agents that cache only exact-match LLM queries miss the majority of cacheable traffic: slightly rephrased questions with identical intent — 'What is the capital of France?' vs 'What's France's capital?' — each hit the LLM and incur full latency and cost. Implement a semantic cache that embeds queries, finds near-identical cached responses by cosine similarity, and serves cached answers when similarity exceeds a confidence threshold."
date: 2026-04-16
difficulty: advanced
category: performance
slug: agent-doesnt-implement-semantic-cache-for-similar-llm-queries
tags: [semantic-cache, embedding-similarity, llm-caching, cosine-similarity, query-deduplication, cost-reduction]
symptoms:
  - "Rephrased but semantically identical questions each incur full LLM latency"
  - "Exact-match cache hit rate is under 5% even though question topics repeat frequently"
  - "No mechanism to recognize 'How do I reset my password?' and 'Steps to reset password' as equivalent"
  - "LLM cost grows linearly with request volume despite many semantically duplicate queries"
  - "Cache is populated but never hit because string equality is too strict"
---

## Why This Happens

Exact-match caches are efficient but brittle: a single word difference produces a cache miss even when two queries are semantically identical. Semantic caching embeds each incoming query into a vector, searches a vector index for the nearest cached query embedding, and serves the cached response if similarity exceeds a threshold. The embedding captures meaning rather than surface form, so paraphrases and minor rewrites hit the cache. The main engineering challenges are choosing the similarity threshold (too low = false hits; too high = no savings) and managing cache entries that become stale when the underlying knowledge changes.

## Solution 1: Semantic Cache Entry

```python
import time
from dataclasses import dataclass, field
from typing import Any, List, Optional


@dataclass
class SemanticCacheEntry:
    entry_id: str
    query_text: str
    query_embedding: List[float]
    response: Any
    model: str
    created_at: float = field(default_factory=time.time)
    last_hit_at: Optional[float] = None
    hit_count: int = 0
    ttl_seconds: float = 3600.0

    def is_expired(self) -> bool:
        return time.time() - self.created_at > self.ttl_seconds

    def record_hit(self) -> None:
        self.hit_count += 1
        self.last_hit_at = time.time()
```

## Solution 2: Cosine Similarity Index

```python
import math
import time
from threading import Lock
from typing import List, Optional, Tuple


class CosineSimilarityIndex:
    """
    Brute-force cosine similarity search over cached query embeddings.
    Suitable for caches up to ~50k entries. For larger caches, replace
    with an approximate nearest-neighbor index (FAISS, Annoy).
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

        results.sort(key=lambda x: -x[1])
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
        if len(a) != len(b):
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(x * x for x in b))
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)
```

## Solution 3: Semantic Cache

```python
import hashlib
import time
from typing import Any, Callable, List, Optional


class SemanticCache:
    """
    Semantic cache that finds near-duplicate queries by embedding similarity.
    Falls through to the LLM on cache miss and stores the new embedding + response.
    """

    def __init__(
        self,
        index: CosineSimilarityIndex,
        embed_fn: Callable[[str], List[float]],
        similarity_threshold: float = 0.92,
        default_ttl_seconds: float = 3600.0,
    ):
        self._index = index
        self._embed_fn = embed_fn
        self._threshold = similarity_threshold
        self._ttl = default_ttl_seconds
        self._hits = 0
        self._misses = 0
        self._stored = 0

    async def get(self, query: str) -> Optional[tuple]:
        """Returns (cached_response, similarity) or None on miss."""
        embedding = await self._embed_fn(query)
        results = self._index.search(embedding, top_k=1, min_similarity=self._threshold)
        if results:
            entry, similarity = results[0]
            entry.record_hit()
            self._hits += 1
            return entry.response, round(similarity, 4)
        self._misses += 1
        return None

    async def store(
        self,
        query: str,
        response: Any,
        model: str = "",
        ttl_seconds: Optional[float] = None,
    ) -> str:
        embedding = await self._embed_fn(query)
        entry_id = hashlib.sha256(query.encode()).hexdigest()[:12]
        entry = SemanticCacheEntry(
            entry_id=entry_id,
            query_text=query,
            query_embedding=embedding,
            response=response,
            model=model,
            ttl_seconds=ttl_seconds or self._ttl,
        )
        self._index.add(entry)
        self._stored += 1
        return entry_id

    def hit_rate(self) -> float:
        total = self._hits + self._misses
        return round(self._hits / max(total, 1), 4)

    def stats(self) -> dict:
        return {
            "hits": self._hits,
            "misses": self._misses,
            "stored": self._stored,
            "hit_rate": self.hit_rate(),
            "index_size": self._index.size(),
        }
```

## Solution 4: Cache-Aware LLM Caller

```python
import time
from typing import Any, Callable, Optional


class CacheAwareLLMCaller:
    """
    Wraps LLM calls with semantic cache lookup and storage.
    Measures latency savings from cache hits vs. direct LLM calls.
    """

    def __init__(
        self,
        cache: SemanticCache,
        llm_fn: Callable,
        model: str = "",
    ):
        self._cache = cache
        self._llm_fn = llm_fn
        self._model = model
        self._total_latency_saved_ms = 0.0
        self._llm_calls = 0

    async def call(self, query: str, **llm_kwargs) -> dict:
        start = time.time()

        hit = await self._cache.get(query)
        if hit is not None:
            response, similarity = hit
            latency_ms = round((time.time() - start) * 1000, 2)
            return {
                "response": response,
                "source": "semantic_cache",
                "similarity": similarity,
                "latency_ms": latency_ms,
            }

        self._llm_calls += 1
        response = await self._llm_fn(query, **llm_kwargs)
        latency_ms = round((time.time() - start) * 1000, 2)

        await self._cache.store(query, response, model=self._model)

        return {
            "response": response,
            "source": "llm",
            "similarity": None,
            "latency_ms": latency_ms,
        }

    def stats(self) -> dict:
        return {
            "llm_calls": self._llm_calls,
            **self._cache.stats(),
        }
```

## Solution 5: Similarity Threshold Tuner

```python
import math
from typing import List, Tuple


class SimilarityThresholdTuner:
    """
    Analyzes cache hit/false-positive rates across threshold values
    to help select an optimal similarity threshold.
    Requires a labeled sample of (query_a, query_b, is_semantically_identical) pairs.
    """

    def __init__(self, index: CosineSimilarityIndex, embed_fn):
        self._index = index
        self._embed_fn = embed_fn

    async def evaluate(
        self,
        samples: List[Tuple[str, str, bool]],
        thresholds: List[float] = None,
    ) -> List[dict]:
        if thresholds is None:
            thresholds = [0.80, 0.85, 0.88, 0.90, 0.92, 0.95, 0.97]

        pairs = []
        for query_a, query_b, is_identical in samples:
            emb_a = await self._embed_fn(query_a)
            emb_b = await self._embed_fn(query_b)
            sim = CosineSimilarityIndex._cosine(emb_a, emb_b)
            pairs.append((sim, is_identical))

        results = []
        for threshold in thresholds:
            tp = sum(1 for sim, ident in pairs if sim >= threshold and ident)
            fp = sum(1 for sim, ident in pairs if sim >= threshold and not ident)
            fn = sum(1 for sim, ident in pairs if sim < threshold and ident)
            precision = tp / max(tp + fp, 1)
            recall = tp / max(tp + fn, 1)
            f1 = 2 * precision * recall / max(precision + recall, 0.001)
            results.append({
                "threshold": threshold,
                "precision": round(precision, 4),
                "recall": round(recall, 4),
                "f1": round(f1, 4),
            })

        return sorted(results, key=lambda x: -x["f1"])
```

## Solution 6: Semantic Cache Dashboard

```python
import time


class SemanticCacheDashboard:
    """
    Combines cache stats, index size, and hit rate trends
    into an operational semantic cache health report.
    """

    def __init__(
        self,
        caller: CacheAwareLLMCaller,
        cache: SemanticCache,
        index: CosineSimilarityIndex,
    ):
        self._caller = caller
        self._cache = cache
        self._index = index

    def render(self) -> dict:
        stats = self._cache.stats()
        return {
            "generated_at": time.time(),
            "cache_stats": stats,
            "llm_calls_avoided": stats["hits"],
            "llm_calls_made": self._caller._llm_calls,
            "index": {
                "size": self._index.size(),
            },
            "similarity_threshold": self._cache._threshold,
            "ttl_seconds": self._cache._ttl,
        }
```

## Comparison

| Approach | Embedding Lookup | Similarity Threshold | TTL Expiry | Threshold Tuning | Dashboard |
|---|---|---|---|---|---|
| CosineSimilarityIndex | Yes (brute-force) | Yes (min_similarity) | Via entry | No | No |
| SemanticCache | Via index | Yes (configurable) | Yes | No | No |
| CacheAwareLLMCaller | Via cache | Via cache | Via cache | No | No |
| SimilarityThresholdTuner | Via index | No | No | Yes (F1 curve) | No |
| SemanticCacheDashboard | No | No | No | No | Yes |

**Best for production**: Start with `similarity_threshold=0.92` and run `SimilarityThresholdTuner` on a labeled sample of 200–500 real query pairs from your domain to validate the threshold before deploying. Use a short `ttl_seconds=1800` (30 min) for time-sensitive domains (news, prices) and longer `ttl_seconds=86400` for stable knowledge bases. Call `remove_expired()` on the index hourly to prevent stale entries from being searched. For caches exceeding 50,000 entries, replace `CosineSimilarityIndex` with FAISS — brute-force cosine over 50k 1536-dim vectors takes ~200ms per query, eliminating the latency benefit of caching.
