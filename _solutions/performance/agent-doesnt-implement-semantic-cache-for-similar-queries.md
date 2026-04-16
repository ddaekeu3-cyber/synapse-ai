---
title: "Agent Doesn't Implement Semantic Cache for Similar Queries"
description: "Agents that cache only exact-match queries miss the majority of cacheable requests: 'What is the capital of France?' and 'Tell me France's capital city' are semantically identical but produce cache misses on exact-key lookup. Implement a semantic cache that uses embedding similarity to match new queries against cached responses, serving cached results when semantic similarity exceeds a threshold."
date: 2026-04-16
difficulty: advanced
category: performance
slug: agent-doesnt-implement-semantic-cache-for-similar-queries
tags: [semantic-cache, embedding-similarity, query-deduplication, cache-hit-rate, cosine-similarity, rag-optimization]
symptoms:
  - "Exact-match cache has <5% hit rate despite many semantically duplicate queries"
  - "Same question phrased differently causes a full LLM call every time"
  - "No measurement of semantic similarity between incoming and cached queries"
  - "Cache key is the raw query string — minor wording changes always miss"
  - "LLM cost scales linearly with user sessions despite high query repetition across sessions"
---

## Why This Happens

Exact-match caches are fast but brittle: any variation in whitespace, capitalization, or phrasing produces a miss. Semantic caches embed queries into vector space and find cached entries within a configurable similarity radius. Two queries at cosine similarity 0.95 almost certainly expect the same answer and the cached response is valid for both. Semantic caching requires embedding each incoming query, searching a vector index of cached query embeddings, and returning the stored response when the nearest neighbor exceeds the similarity threshold.

## Solution 1: Semantic Cache Entry

```python
import time
from dataclasses import dataclass, field
from typing import Any, List, Optional


@dataclass
class SemanticCacheEntry:
    query: str
    query_embedding: List[float]
    response: Any
    created_at: float = field(default_factory=time.time)
    hit_count: int = 0
    cache_key: str = ""
    metadata: dict = field(default_factory=dict)
    ttl_seconds: float = 3600.0

    def is_expired(self) -> bool:
        return time.time() - self.created_at > self.ttl_seconds

    def record_hit(self) -> None:
        self.hit_count += 1
```

## Solution 2: Cosine Similarity Index

```python
import math
from threading import Lock
from typing import List, Optional, Tuple


class CosineSimilarityIndex:
    """
    In-memory cosine similarity index for semantic cache lookup.
    Replace with FAISS or a vector database for large caches.
    """

    def __init__(self):
        self._entries: List[SemanticCacheEntry] = []
        self._lock = Lock()

    @staticmethod
    def _cosine(a: List[float], b: List[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(x * x for x in b))
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)

    def add(self, entry: SemanticCacheEntry) -> None:
        with self._lock:
            self._entries.append(entry)

    def find_nearest(
        self,
        query_embedding: List[float],
        threshold: float = 0.90,
        exclude_expired: bool = True,
    ) -> Optional[Tuple[SemanticCacheEntry, float]]:
        with self._lock:
            best_entry = None
            best_sim = -1.0
            for entry in self._entries:
                if exclude_expired and entry.is_expired():
                    continue
                sim = self._cosine(query_embedding, entry.query_embedding)
                if sim > best_sim:
                    best_sim = sim
                    best_entry = entry

        if best_entry and best_sim >= threshold:
            return best_entry, round(best_sim, 4)
        return None

    def remove_expired(self) -> int:
        with self._lock:
            before = len(self._entries)
            self._entries = [e for e in self._entries if not e.is_expired()]
            return before - len(self._entries)

    def size(self) -> int:
        with self._lock:
            return len(self._entries)
```

## Solution 3: Semantic Cache

```python
import hashlib
import time
from typing import Any, Callable, List, Optional


class SemanticCache:
    """
    Semantic query cache: stores responses keyed by query embedding,
    returns cached responses for semantically similar queries.
    """

    def __init__(
        self,
        index: CosineSimilarityIndex,
        embed_fn: Callable[[str], List[float]],
        similarity_threshold: float = 0.92,
        max_entries: int = 5000,
        ttl_seconds: float = 3600.0,
    ):
        self._index = index
        self._embed_fn = embed_fn
        self._threshold = similarity_threshold
        self._max_entries = max_entries
        self._ttl = ttl_seconds
        self._hits = 0
        self._misses = 0

    async def get(self, query: str) -> Optional[tuple]:
        """Returns (cached_response, similarity_score) or None on miss."""
        embedding = await self._embed_fn(query)
        result = self._index.find_nearest(embedding, self._threshold)
        if result:
            entry, sim = result
            entry.record_hit()
            self._hits += 1
            return entry.response, sim
        self._misses += 1
        return None

    async def put(self, query: str, response: Any, metadata: dict = None) -> str:
        embedding = await self._embed_fn(query)
        cache_key = hashlib.sha256(query.encode()).hexdigest()[:12]
        entry = SemanticCacheEntry(
            query=query,
            query_embedding=embedding,
            response=response,
            cache_key=cache_key,
            metadata=metadata or {},
            ttl_seconds=self._ttl,
        )
        if self._index.size() >= self._max_entries:
            removed = self._index.remove_expired()
            if removed == 0:
                pass  # eviction policy needed for production
        self._index.add(entry)
        return cache_key

    def stats(self) -> dict:
        total = self._hits + self._misses
        return {
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": round(self._hits / total, 4) if total else 0.0,
            "cache_size": self._index.size(),
            "similarity_threshold": self._threshold,
        }
```

## Solution 4: Semantic Cache Middleware

```python
from typing import Any, Callable, Optional


class SemanticCacheMiddleware:
    """
    Wraps an LLM call function with semantic cache lookup.
    Returns cached response on hit; calls LLM and stores result on miss.
    """

    def __init__(
        self,
        cache: SemanticCache,
        on_hit: Optional[Callable[[str, float], None]] = None,
        on_miss: Optional[Callable[[str], None]] = None,
    ):
        self._cache = cache
        self._on_hit = on_hit
        self._on_miss = on_miss

    async def call(
        self,
        query: str,
        llm_fn: Callable,
        cache_metadata: dict = None,
        *args: Any,
        **kwargs: Any,
    ) -> tuple:
        """Returns (response, from_cache: bool, similarity: float)."""
        hit = await self._cache.get(query)
        if hit is not None:
            response, sim = hit
            if self._on_hit:
                self._on_hit(query, sim)
            return response, True, sim

        if self._on_miss:
            self._on_miss(query)

        response = await llm_fn(query, *args, **kwargs)
        await self._cache.put(query, response, cache_metadata)
        return response, False, 0.0
```

## Solution 5: Threshold Calibrator

```python
import math
from typing import List, Tuple


class SemanticCacheThresholdCalibrator:
    """
    Analyzes a set of (query, expected_cache_match: bool) pairs to
    recommend the optimal similarity threshold for a given workload.
    """

    def calibrate(
        self,
        labeled_pairs: List[Tuple[List[float], List[float], bool]],
        # [(query_emb, candidate_emb, should_match)]
    ) -> dict:
        thresholds = [0.80, 0.85, 0.90, 0.92, 0.95, 0.97]
        results = []

        for threshold in thresholds:
            tp = fp = tn = fn = 0
            for q_emb, c_emb, should_match in labeled_pairs:
                dot = sum(x * y for x, y in zip(q_emb, c_emb))
                norm_q = math.sqrt(sum(x * x for x in q_emb))
                norm_c = math.sqrt(sum(x * x for x in c_emb))
                sim = dot / (norm_q * norm_c) if norm_q and norm_c else 0.0

                matched = sim >= threshold
                if matched and should_match:
                    tp += 1
                elif matched and not should_match:
                    fp += 1
                elif not matched and should_match:
                    fn += 1
                else:
                    tn += 1

            precision = tp / max(tp + fp, 1)
            recall = tp / max(tp + fn, 1)
            f1 = 2 * precision * recall / max(precision + recall, 0.001)
            results.append({
                "threshold": threshold,
                "precision": round(precision, 3),
                "recall": round(recall, 3),
                "f1": round(f1, 3),
                "false_positives": fp,
            })

        best = max(results, key=lambda r: r["f1"])
        return {"recommendation": best, "all_thresholds": results}
```

## Solution 6: Semantic Cache Savings Monitor

```python
import time
from threading import Lock
from typing import List


class SemanticCacheSavingsMonitor:
    """
    Tracks token and cost savings from semantic cache hits over time.
    """

    def __init__(self, cost_per_million_tokens: float = 3.0):
        self._records: List[dict] = []
        self._lock = Lock()
        self._cost_per_m = cost_per_million_tokens

    def record_hit(self, query: str, estimated_response_tokens: int, similarity: float) -> None:
        with self._lock:
            self._records.append({
                "ts": time.time(),
                "tokens_saved": estimated_response_tokens,
                "similarity": similarity,
            })
            if len(self._records) > 50000:
                self._records.pop(0)

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        with self._lock:
            recent = [r for r in self._records if r["ts"] >= cutoff]
        if not recent:
            return {"window_seconds": window_seconds, "hits": 0}
        total_tokens = sum(r["tokens_saved"] for r in recent)
        cost_saved = total_tokens / 1_000_000 * self._cost_per_m
        avg_sim = sum(r["similarity"] for r in recent) / len(recent)
        return {
            "window_seconds": window_seconds,
            "hits": len(recent),
            "total_tokens_saved": total_tokens,
            "estimated_cost_saved_usd": round(cost_saved, 4),
            "avg_similarity": round(avg_sim, 4),
        }
```

## Comparison

| Approach | Embedding Lookup | Similarity Threshold | TTL Expiry | Threshold Calibration | Cost Tracking |
|---|---|---|---|---|---|
| CosineSimilarityIndex | Yes (brute-force) | Yes | Yes | No | No |
| SemanticCache | Via index | Yes | Via index | No | No |
| SemanticCacheMiddleware | Via cache | Via cache | Via cache | No | No |
| SemanticCacheThresholdCalibrator | No | No | No | Yes (F1-based) | No |
| SemanticCacheSavingsMonitor | No | No | No | No | Yes |

**Best for production**: Use `similarity_threshold=0.92` as the starting point and calibrate with `SemanticCacheThresholdCalibrator` on a labeled sample of your actual query pairs — the right threshold varies significantly by domain (factual Q&A vs. creative writing). Replace `CosineSimilarityIndex` with FAISS or Qdrant for caches larger than 10,000 entries — brute-force cosine similarity is O(n) per lookup and becomes a bottleneck at scale. Set `ttl_seconds=3600` for dynamic data and `86400` for stable factual queries. Monitor `avg_similarity` via `SemanticCacheSavingsMonitor`: if it drops below 0.94, the cached entries being served may be slightly off-topic and the threshold should be increased.
