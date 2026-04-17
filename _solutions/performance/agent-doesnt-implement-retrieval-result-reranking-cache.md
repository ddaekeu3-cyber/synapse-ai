---
title: "Agent Doesn't Implement Retrieval Result Reranking Cache"
description: "Agents that run a cross-encoder reranker on every retrieval result set pay reranking latency on every request, even when the same query retrieves the same candidate documents. Implement a reranking cache that stores scored rankings keyed by the query fingerprint and candidate document set fingerprint, returning cached rankings instantly and only invoking the reranker on cache misses."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-retrieval-result-reranking-cache
tags: [reranking-cache, cross-encoder, retrieval-optimization, candidate-fingerprint, latency-reduction, rag-performance]
symptoms:
  - "Reranking adds 500–2000ms to every retrieval call even for repeated queries"
  - "Same top-10 candidates returned for a popular query re-ranked identically every time"
  - "No measurement of reranking hit rate — no visibility into redundant scoring"
  - "Reranking API costs scale linearly with requests despite high query repetition"
  - "Cache populated for embeddings but not for the downstream reranking step"
---

## Why This Happens

RAG pipelines typically have two scoring steps: approximate nearest-neighbor retrieval (fast) followed by cross-encoder reranking (slow but accurate). Reranking scores every candidate document against the query using a more expensive model, adding significant latency. When the same query is repeated — or when a popular query retrieves the same candidate set from the vector store — the reranking computation is identical. Caching ranked results requires a composite key: the query (normalized) combined with the identity of the candidate set, so that a different retrieval for the same query does not serve a stale ranking.

## Solution 1: Reranking Cache Key

```python
import hashlib
import json
from dataclasses import dataclass
from typing import Any, List


@dataclass
class RetrievedCandidate:
    doc_id: str
    content: str
    retrieval_score: float
    metadata: dict = None

    def __post_init__(self):
        if self.metadata is None:
            self.metadata = {}


class RerankingCacheKey:
    """
    Computes a stable cache key from the query and the candidate document set.
    Two queries with identical text and the same candidate doc IDs produce
    the same cache key regardless of retrieval score order.
    """

    @staticmethod
    def compute(query: str, candidates: List[RetrievedCandidate], model_id: str) -> str:
        import re, unicodedata
        normalized_query = unicodedata.normalize("NFKC", query.strip())
        normalized_query = re.sub(r"\s+", " ", normalized_query).lower()

        # Sort doc IDs to be order-independent
        sorted_ids = sorted(c.doc_id for c in candidates)

        key_data = {
            "q": normalized_query,
            "docs": sorted_ids,
            "model": model_id,
        }
        raw = json.dumps(key_data, sort_keys=True)
        return hashlib.sha256(raw.encode()).hexdigest()[:32]
```

## Solution 2: Reranking Cache Entry

```python
import time
from dataclasses import dataclass, field
from typing import List, Tuple


@dataclass
class RerankingCacheEntry:
    cache_key: str
    ranked_results: List[Tuple[str, float]]   # [(doc_id, reranking_score), ...]
    model_id: str
    query_fingerprint: str
    candidate_count: int
    created_at: float = field(default_factory=time.time)
    last_hit_at: float = field(default_factory=time.time)
    hit_count: int = 0
    latency_saved_ms: float = 0.0     # estimated ms saved per hit

    def record_hit(self, latency_saved_ms: float = 0.0) -> None:
        self.hit_count += 1
        self.last_hit_at = time.time()
        self.latency_saved_ms += latency_saved_ms

    def age_s(self) -> float:
        return time.time() - self.created_at
```

## Solution 3: Reranking Result Cache

```python
import time
from collections import OrderedDict
from threading import Lock
from typing import Dict, List, Optional, Tuple


class RerankingResultCache:
    """
    LRU cache for reranking results with TTL-based expiry.
    Keys are composite query+candidate fingerprints.
    """

    def __init__(
        self,
        max_entries: int = 5000,
        ttl_seconds: float = 3600.0,
    ):
        self._cache: OrderedDict[str, RerankingCacheEntry] = OrderedDict()
        self._lock = Lock()
        self._max = max_entries
        self._ttl = ttl_seconds
        self._hits = 0
        self._misses = 0

    def get(
        self,
        cache_key: str,
        reranking_latency_estimate_ms: float = 500.0,
    ) -> Optional[List[Tuple[str, float]]]:
        with self._lock:
            entry = self._cache.get(cache_key)
            if entry is None:
                self._misses += 1
                return None
            if entry.age_s() > self._ttl:
                del self._cache[cache_key]
                self._misses += 1
                return None
            self._cache.move_to_end(cache_key)
            entry.record_hit(reranking_latency_estimate_ms)
            self._hits += 1
            return entry.ranked_results

    def put(
        self,
        cache_key: str,
        ranked_results: List[Tuple[str, float]],
        model_id: str,
        query_fingerprint: str,
        candidate_count: int,
    ) -> None:
        with self._lock:
            if cache_key in self._cache:
                self._cache.move_to_end(cache_key)
                self._cache[cache_key].ranked_results = ranked_results
                return
            if len(self._cache) >= self._max:
                self._cache.popitem(last=False)
            self._cache[cache_key] = RerankingCacheEntry(
                cache_key=cache_key,
                ranked_results=ranked_results,
                model_id=model_id,
                query_fingerprint=hashlib.sha256(query_fingerprint.encode()).hexdigest()[:12],
                candidate_count=candidate_count,
            )

    def stats(self) -> dict:
        total = self._hits + self._misses
        with self._lock:
            total_saved = sum(e.latency_saved_ms for e in self._cache.values())
        return {
            "entries": len(self._cache),
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": round(self._hits / max(total, 1), 4),
            "estimated_ms_saved": round(total_saved, 2),
        }

import hashlib
```

## Solution 4: Caching Reranker

```python
import time
from typing import Any, Callable, List, Optional, Tuple


class CachingReranker:
    """
    Wraps a reranking function with cache lookup and store.
    Returns cached ranking on hit; calls reranker and stores result on miss.
    """

    def __init__(
        self,
        cache: RerankingResultCache,
        rerank_fn: Callable,    # async (query, candidates) -> List[(doc_id, score)]
        model_id: str,
        reranking_latency_estimate_ms: float = 600.0,
    ):
        self._cache = cache
        self._rerank_fn = rerank_fn
        self._model_id = model_id
        self._latency_est = reranking_latency_estimate_ms
        self._api_calls = 0
        self._api_latency_total_ms = 0.0

    async def rerank(
        self,
        query: str,
        candidates: List[RetrievedCandidate],
    ) -> List[Tuple[str, float]]:
        if not candidates:
            return []

        cache_key = RerankingCacheKey.compute(query, candidates, self._model_id)
        cached = self._cache.get(cache_key, self._latency_est)
        if cached is not None:
            return cached

        # Cache miss — call the reranker
        start = time.time()
        ranked = await self._rerank_fn(query, candidates)
        latency_ms = round((time.time() - start) * 1000, 2)
        self._api_calls += 1
        self._api_latency_total_ms += latency_ms

        self._cache.put(
            cache_key=cache_key,
            ranked_results=ranked,
            model_id=self._model_id,
            query_fingerprint=query,
            candidate_count=len(candidates),
        )
        return ranked

    def stats(self) -> dict:
        return {
            "api_calls": self._api_calls,
            "mean_api_latency_ms": round(
                self._api_latency_total_ms / max(self._api_calls, 1), 2
            ),
            "cache_stats": self._cache.stats(),
        }
```

## Solution 5: Candidate Set Stability Tracker

```python
import time
from collections import defaultdict
from typing import Dict, List


class CandidateSetStabilityTracker:
    """
    Tracks how stable the candidate document set is for popular queries.
    High stability (same docs returned repeatedly) means the reranking cache
    will achieve high hit rates. Low stability means the cache is less useful.
    """

    def __init__(self, max_queries: int = 1000):
        self._doc_sets: Dict[str, List[frozenset]] = defaultdict(list)
        self._max = max_queries

    def record(self, query_fingerprint: str, doc_ids: List[str]) -> None:
        history = self._doc_sets[query_fingerprint]
        history.append(frozenset(doc_ids))
        if len(history) > 20:
            self._doc_sets[query_fingerprint] = history[-20:]

    def stability_score(self, query_fingerprint: str) -> float:
        """Returns 0.0–1.0, where 1.0 = always same candidate set."""
        history = self._doc_sets.get(query_fingerprint, [])
        if len(history) < 2:
            return 1.0
        reference = history[-1]
        jaccard_scores = [
            len(reference & s) / max(len(reference | s), 1)
            for s in history[:-1]
        ]
        return round(sum(jaccard_scores) / len(jaccard_scores), 4)
```

## Solution 6: Reranking Cache Dashboard

```python
import time


class RerankingCacheDashboard:
    """
    Combines caching reranker stats and cache entry details into an
    operational view for performance tuning.
    """

    def __init__(
        self,
        reranker: CachingReranker,
        stability_tracker: CandidateSetStabilityTracker,
    ):
        self._reranker = reranker
        self._stability = stability_tracker

    def render(self) -> dict:
        stats = self._reranker.stats()
        cache_stats = stats["cache_stats"]
        return {
            "generated_at": time.time(),
            "reranker_api": {
                "calls": stats["api_calls"],
                "mean_latency_ms": stats["mean_api_latency_ms"],
            },
            "cache": cache_stats,
            "estimated_latency_saved_ms": cache_stats.get("estimated_ms_saved", 0.0),
        }
```

## Comparison

| Approach | Composite Key | LRU Eviction | TTL Expiry | Candidate Stability | Savings Tracking |
|---|---|---|---|---|---|
| RerankingCacheKey | Yes (query + doc IDs) | No | No | No | No |
| RerankingResultCache | Via key | Yes | Yes | No | Yes (ms saved) |
| CachingReranker | Via key | Via cache | Via cache | No | Yes |
| CandidateSetStabilityTracker | No | No | No | Yes | No |
| RerankingCacheDashboard | No | No | No | Via tracker | Via reranker |

**Best for production**: Use `CandidateSetStabilityTracker` before deciding to deploy the cache — if candidate sets are highly variable (score below 0.5) for the target query distribution, the cache miss rate will be too high to justify the added complexity. Set TTL to match the document index refresh cycle: if the vector store is re-indexed every 6 hours, a TTL of 6 hours ensures cached rankings are never based on stale documents. Monitor `estimated_ms_saved` — at 600ms per reranking call, a 50% hit rate on 1000 requests/hour saves 5 minutes of cumulative user wait time per hour.
