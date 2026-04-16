---
title: "Agent Doesn't Implement Response Caching with Semantic Similarity"
description: "Agents that only cache exact-match queries miss the opportunity to serve cached responses for semantically equivalent questions — 'What is the capital of France?' and 'Tell me the capital city of France' should hit the same cache entry. Implement semantic similarity caching that embeds incoming queries, searches a vector cache for near-duplicate entries, and returns the cached response when similarity exceeds a confidence threshold."
date: 2026-04-16
difficulty: advanced
category: performance
slug: agent-doesnt-implement-response-caching-with-semantic-similarity
tags: [semantic-cache, vector-cache, embedding-cache, similarity-search, cache-hit-rate, llm-cost-reduction]
symptoms:
  - "Cache hit rate is below 5% despite many semantically identical queries"
  - "Exact-match cache only fires on copy-pasted queries, not paraphrases"
  - "Token cost is high because reformulations of the same question all miss cache"
  - "No mechanism to detect that two queries are asking the same thing differently"
  - "FAQ-style queries pay full LLM cost on every paraphrase variant"
---

## Why This Happens

Exact-match caching uses a hash of the raw query string as the cache key. Two queries that differ by one word — "What's the weather in Paris?" vs "How's the weather in Paris?" — produce completely different hashes and both miss the cache independently. Semantic caching replaces the hash lookup with a nearest-neighbor search over query embeddings. If the nearest cached embedding is within a cosine distance threshold, the cached response is returned without calling the LLM.

## Solution 1: Semantic Cache Entry

```python
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class SemanticCacheEntry:
    entry_id: str
    query_text: str
    query_embedding: List[float]
    response: Any
    model: str
    created_at: float = field(default_factory=time.time)
    hit_count: int = 0
    last_hit_at: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def record_hit(self) -> None:
        self.hit_count += 1
        self.last_hit_at = time.time()

    def age_seconds(self) -> float:
        return time.time() - self.created_at
```

## Solution 2: Cosine Similarity Calculator

```python
import math
from typing import List, Optional, Tuple


class CosineSimilarityCalculator:
    """
    Computes cosine similarity between two embedding vectors.
    Returns a value in [-1, 1] where 1.0 = identical direction.
    """

    @staticmethod
    def similarity(a: List[float], b: List[float]) -> float:
        if len(a) != len(b):
            raise ValueError(f"Embedding dimension mismatch: {len(a)} vs {len(b)}")
        dot = sum(x * y for x, y in zip(a, b))
        mag_a = math.sqrt(sum(x * x for x in a))
        mag_b = math.sqrt(sum(x * x for x in b))
        if mag_a == 0 or mag_b == 0:
            return 0.0
        return round(dot / (mag_a * mag_b), 6)

    @staticmethod
    def nearest(
        query: List[float],
        candidates: List[Tuple[str, List[float]]],  # (entry_id, embedding)
        top_k: int = 1,
    ) -> List[Tuple[str, float]]:
        """Returns list of (entry_id, similarity) sorted by similarity descending."""
        scores = [
            (entry_id, CosineSimilarityCalculator.similarity(query, emb))
            for entry_id, emb in candidates
        ]
        scores.sort(key=lambda x: -x[1])
        return scores[:top_k]
```

## Solution 3: Semantic Vector Cache Store

```python
import uuid
import time
from typing import Any, Dict, List, Optional, Tuple


class SemanticVectorCacheStore:
    """
    In-process vector cache for LLM responses.
    Stores embeddings and performs linear nearest-neighbor search.
    For large caches (>10K entries), replace with a vector database.
    """

    def __init__(
        self,
        similarity_threshold: float = 0.92,
        ttl_seconds: float = 3600.0,
        max_entries: int = 2048,
    ) -> None:
        self._threshold = similarity_threshold
        self._ttl = ttl_seconds
        self._max = max_entries
        self._entries: Dict[str, SemanticCacheEntry] = {}
        self._hits = 0
        self._misses = 0
        self._calculator = CosineSimilarityCalculator()

    def _evict_expired(self) -> None:
        expired = [eid for eid, e in self._entries.items() if e.age_seconds() > self._ttl]
        for eid in expired:
            del self._entries[eid]

    def _evict_lru(self) -> None:
        if len(self._entries) >= self._max:
            # Evict least-recently-hit entry
            lru_id = min(
                self._entries,
                key=lambda eid: self._entries[eid].last_hit_at or self._entries[eid].created_at,
            )
            del self._entries[lru_id]

    def lookup(
        self,
        query_embedding: List[float],
        model: Optional[str] = None,
    ) -> Optional[SemanticCacheEntry]:
        self._evict_expired()
        candidates = [
            (eid, e.query_embedding)
            for eid, e in self._entries.items()
            if model is None or e.model == model
        ]
        if not candidates:
            self._misses += 1
            return None

        ranked = self._calculator.nearest(query_embedding, candidates, top_k=1)
        if not ranked:
            self._misses += 1
            return None

        best_id, best_sim = ranked[0]
        if best_sim < self._threshold:
            self._misses += 1
            return None

        entry = self._entries[best_id]
        entry.record_hit()
        entry.metadata["last_similarity"] = best_sim
        self._hits += 1
        return entry

    def store(
        self,
        query_text: str,
        query_embedding: List[float],
        response: Any,
        model: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SemanticCacheEntry:
        self._evict_expired()
        self._evict_lru()

        entry = SemanticCacheEntry(
            entry_id=str(uuid.uuid4())[:8],
            query_text=query_text,
            query_embedding=query_embedding,
            response=response,
            model=model,
            metadata=metadata or {},
        )
        self._entries[entry.entry_id] = entry
        return entry

    def stats(self) -> dict:
        total = self._hits + self._misses
        return {
            "entries": len(self._entries),
            "max_entries": self._max,
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": round(self._hits / max(total, 1), 4),
            "similarity_threshold": self._threshold,
            "ttl_seconds": self._ttl,
        }
```

## Solution 4: Semantic Cache LLM Wrapper

```python
from typing import Any, Callable, Dict, List, Optional


class SemanticCacheLLMWrapper:
    """
    Wraps an LLM call with semantic cache lookup.
    On cache hit: returns cached response immediately.
    On cache miss: calls LLM, stores result, returns response.
    Requires an embedding function to convert query text to vectors.
    """

    def __init__(
        self,
        cache_store: SemanticVectorCacheStore,
        embed_fn: Callable[[str], List[float]],   # async or sync embedding function
        llm_fn: Callable,                          # async LLM call function
    ) -> None:
        self._store = cache_store
        self._embed_fn = embed_fn
        self._llm_fn = llm_fn

    async def chat(
        self,
        query: str,
        model: str,
        messages: List[Dict[str, str]],
        max_tokens: int = 1024,
        bypass_cache: bool = False,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        if not bypass_cache:
            embedding = await self._embed_fn(query)
            cached = self._store.lookup(embedding, model=model)
            if cached:
                return {
                    **cached.response,
                    "_cache": {
                        "hit": True,
                        "similarity": cached.metadata.get("last_similarity"),
                        "original_query": cached.query_text,
                        "hit_count": cached.hit_count,
                    },
                }

        response = await self._llm_fn(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            **kwargs,
        )

        if not bypass_cache:
            self._store.store(
                query_text=query,
                query_embedding=embedding,
                response=response,
                model=model,
            )

        return {**response, "_cache": {"hit": False}}
```

## Solution 5: Cache Quality Monitor

```python
import time
from typing import List


class SemanticCacheQualityMonitor:
    """
    Monitors cache quality — hit rate, threshold calibration,
    and freshness — and alerts when tuning is needed.
    """

    def __init__(
        self,
        cache_store: SemanticVectorCacheStore,
        target_hit_rate: float = 0.20,
        min_hit_rate_alert: float = 0.05,
    ) -> None:
        self._store = cache_store
        self._target = target_hit_rate
        self._min = min_hit_rate_alert

    def check(self) -> List[dict]:
        stats = self._store.stats()
        alerts = []

        total = stats["hits"] + stats["misses"]
        if total > 100 and stats["hit_rate"] < self._min:
            alerts.append({
                "type": "low_semantic_hit_rate",
                "hit_rate": stats["hit_rate"],
                "target": self._target,
                "recommendation": (
                    "Consider lowering similarity_threshold (e.g. 0.88) "
                    "or pre-warming cache with common queries."
                ),
            })

        fill_pct = stats["entries"] / max(stats["max_entries"], 1)
        if fill_pct > 0.90:
            alerts.append({
                "type": "cache_near_capacity",
                "fill_pct": round(fill_pct * 100, 1),
                "recommendation": "Increase max_entries or reduce ttl_seconds.",
            })

        return alerts

    def report(self) -> dict:
        return {
            "generated_at": time.time(),
            "stats": self._store.stats(),
            "alerts": self.check(),
        }
```

## Solution 6: Semantic Cache Dashboard

```python
import time


class SemanticCacheDashboard:
    """
    Combines cache stats, quality alerts, and entry distribution
    into a single performance observability report.
    """

    def __init__(
        self,
        cache_store: SemanticVectorCacheStore,
        monitor: SemanticCacheQualityMonitor,
    ) -> None:
        self._store = cache_store
        self._monitor = monitor

    def render(self) -> dict:
        stats = self._store.stats()
        alerts = self._monitor.check()

        return {
            "generated_at": time.time(),
            "cache": {
                "entries": stats["entries"],
                "hit_rate_pct": round(stats["hit_rate"] * 100, 1),
                "hits": stats["hits"],
                "misses": stats["misses"],
                "similarity_threshold": stats["similarity_threshold"],
                "ttl_seconds": stats["ttl_seconds"],
            },
            "active_alerts": alerts,
        }
```

## Comparison

| Approach | Similarity Matching | TTL + LRU Eviction | LLM Integration | Quality Monitoring | Dashboard |
|---|---|---|---|---|---|
| CosineSimilarityCalculator | Yes (linear scan) | No | No | No | No |
| SemanticVectorCacheStore | Via calculator | Yes | No | No | No |
| SemanticCacheLLMWrapper | Via store | Via store | Yes | No | No |
| SemanticCacheQualityMonitor | No | No | No | Yes | No |
| SemanticCacheDashboard | No | No | No | Via monitor | Yes |

**Best for production**: Start with `similarity_threshold=0.92` and measure hit rate for one week before tuning — 0.92 is conservative enough to avoid false positives on semantically different questions. Use a fast embedding model (e.g. `text-embedding-3-small`) rather than a full model for cache lookups — the embedding call should be 10× cheaper than the LLM call to ensure cache lookup has positive ROI. For large deployments (>10K cached entries), replace the linear scan in `SemanticVectorCacheStore` with a vector database (pgvector, Qdrant, or Pinecone) — linear scan scales as O(n) and becomes the bottleneck above 10K entries. Never cache responses to queries containing user-specific data (account balances, private messages) — partition the cache by content type and disable semantic caching for personalized queries.
