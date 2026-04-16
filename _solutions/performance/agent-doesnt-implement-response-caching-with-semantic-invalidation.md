---
title: "Agent Doesn't Implement Response Caching with Semantic Invalidation"
description: "Agents that cache responses by exact query string miss the majority of cache opportunities — semantically equivalent questions ('What is the capital of France?' and 'France capital?') produce separate cache entries. Conversely, caches that never invalidate return stale answers after source documents change. Implement response caching with semantic similarity matching for cache lookup and dependency-tracked invalidation when referenced documents are updated."
date: 2026-04-16
difficulty: advanced
category: performance
slug: agent-doesnt-implement-response-caching-with-semantic-invalidation
tags: [semantic-cache, cache-invalidation, response-caching, embedding-similarity, rag-cache, dependency-tracking]
symptoms:
  - "Cache hit rate below 5% because every question is slightly differently worded"
  - "Cached response returns a stale answer after the source document was updated"
  - "Semantically identical queries from different users each trigger a fresh LLM call"
  - "No tracking of which documents each cached answer depends on"
  - "Manual cache flush required after every document update"
---

## Why This Happens

Exact-match caching fails for natural language because users phrase the same question differently. Semantic caching uses embedding similarity to find cache entries that are close enough to the current query — within a configurable cosine similarity threshold. Invalidation is the harder problem: a cached answer about "company headcount" becomes stale when the headcount document is updated. Dependency tracking records which document IDs contributed to each cached answer, so when any of those documents change, the affected cache entries are invalidated automatically.

## Solution 1: Semantic Cache Entry

```python
import hashlib
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set


@dataclass
class SemanticCacheEntry:
    entry_id: str
    query_text: str
    query_embedding: List[float]
    response_text: str
    document_dependencies: Set[str]   # document_ids this response drew from
    tool_dependencies: Set[str]       # tool names used to produce this response
    created_at: float = field(default_factory=time.time)
    last_accessed_at: float = field(default_factory=time.time)
    access_count: int = 0
    ttl_seconds: float = 3600.0
    metadata: Dict[str, Any] = field(default_factory=dict)

    def is_expired(self) -> bool:
        return time.time() - self.created_at > self.ttl_seconds

    def touch(self) -> None:
        self.access_count += 1
        self.last_accessed_at = time.time()

    def age_seconds(self) -> float:
        return time.time() - self.created_at
```

## Solution 2: Embedding Similarity Matcher

```python
import math
from typing import List, Optional, Tuple


def cosine_similarity(a: List[float], b: List[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    mag_a = math.sqrt(sum(x * x for x in a))
    mag_b = math.sqrt(sum(x * x for x in b))
    return dot / (mag_a * mag_b + 1e-9)


class EmbeddingSimilarityMatcher:
    """
    Finds cache entries whose query embedding is within a similarity
    threshold of the incoming query embedding.
    Uses linear scan — replace with ANN index for >10k entries.
    """

    def __init__(self, similarity_threshold: float = 0.92):
        self._threshold = similarity_threshold

    def find_match(
        self,
        query_embedding: List[float],
        candidates: List[SemanticCacheEntry],
    ) -> Optional[Tuple[SemanticCacheEntry, float]]:
        """Returns (best_entry, similarity) or None if no match above threshold."""
        best: Optional[Tuple[SemanticCacheEntry, float]] = None
        for entry in candidates:
            if entry.is_expired():
                continue
            sim = cosine_similarity(query_embedding, entry.query_embedding)
            if sim >= self._threshold:
                if best is None or sim > best[1]:
                    best = (entry, sim)
        return best

    def find_all_matches(
        self,
        query_embedding: List[float],
        candidates: List[SemanticCacheEntry],
        top_k: int = 5,
    ) -> List[Tuple[SemanticCacheEntry, float]]:
        results = [
            (entry, cosine_similarity(query_embedding, entry.query_embedding))
            for entry in candidates
            if not entry.is_expired()
        ]
        results = [(e, s) for e, s in results if s >= self._threshold]
        results.sort(key=lambda x: -x[1])
        return results[:top_k]
```

## Solution 3: Dependency Tracker

```python
from typing import Dict, Set


class CacheDependencyTracker:
    """
    Tracks which cache entries depend on which documents.
    When a document is updated, the tracker returns all affected entry IDs
    so they can be invalidated.
    """

    def __init__(self):
        # document_id -> set of entry_ids that depend on it
        self._doc_to_entries: Dict[str, Set[str]] = {}
        # entry_id -> set of document_ids it depends on
        self._entry_to_docs: Dict[str, Set[str]] = {}

    def register(self, entry: SemanticCacheEntry) -> None:
        for doc_id in entry.document_dependencies:
            self._doc_to_entries.setdefault(doc_id, set()).add(entry.entry_id)
        self._entry_to_docs[entry.entry_id] = set(entry.document_dependencies)

    def unregister(self, entry_id: str) -> None:
        docs = self._entry_to_docs.pop(entry_id, set())
        for doc_id in docs:
            self._doc_to_entries.get(doc_id, set()).discard(entry_id)

    def entries_depending_on(self, document_id: str) -> Set[str]:
        return set(self._doc_to_entries.get(document_id, set()))

    def stats(self) -> dict:
        return {
            "tracked_entries": len(self._entry_to_docs),
            "tracked_documents": len(self._doc_to_entries),
        }
```

## Solution 4: Semantic Response Cache

```python
import asyncio
import time
import uuid
from typing import Any, Callable, Dict, List, Optional, Set, Tuple


class SemanticResponseCache:
    """
    Caches LLM responses keyed by query embedding similarity.
    Supports dependency-tracked invalidation: when a document changes,
    all entries that drew from it are automatically evicted.
    """

    def __init__(
        self,
        matcher: EmbeddingSimilarityMatcher,
        dep_tracker: CacheDependencyTracker,
        max_entries: int = 5_000,
        default_ttl_seconds: float = 3600.0,
    ):
        self._matcher = matcher
        self._deps = dep_tracker
        self._entries: Dict[str, SemanticCacheEntry] = {}
        self._max = max_entries
        self._default_ttl = default_ttl_seconds
        self._hits = 0
        self._misses = 0
        self._invalidations = 0

    async def get(
        self,
        query_embedding: List[float],
    ) -> Optional[Tuple[SemanticCacheEntry, float]]:
        candidates = list(self._entries.values())
        match = self._matcher.find_match(query_embedding, candidates)
        if match:
            entry, sim = match
            entry.touch()
            self._hits += 1
            return entry, sim
        self._misses += 1
        return None

    async def put(
        self,
        query_text: str,
        query_embedding: List[float],
        response_text: str,
        document_dependencies: Set[str] = None,
        tool_dependencies: Set[str] = None,
        ttl_seconds: Optional[float] = None,
        metadata: dict = None,
    ) -> SemanticCacheEntry:
        if len(self._entries) >= self._max:
            self._evict_lru()

        entry = SemanticCacheEntry(
            entry_id=str(uuid.uuid4())[:12],
            query_text=query_text,
            query_embedding=query_embedding,
            response_text=response_text,
            document_dependencies=document_dependencies or set(),
            tool_dependencies=tool_dependencies or set(),
            ttl_seconds=ttl_seconds or self._default_ttl,
            metadata=metadata or {},
        )
        self._entries[entry.entry_id] = entry
        self._deps.register(entry)
        return entry

    def invalidate_by_document(self, document_id: str) -> int:
        affected = self._deps.entries_depending_on(document_id)
        removed = 0
        for entry_id in affected:
            if self._entries.pop(entry_id, None):
                self._deps.unregister(entry_id)
                removed += 1
        self._invalidations += removed
        return removed

    def invalidate_entry(self, entry_id: str) -> bool:
        if self._entries.pop(entry_id, None):
            self._deps.unregister(entry_id)
            self._invalidations += 1
            return True
        return False

    def evict_expired(self) -> int:
        expired = [eid for eid, e in self._entries.items() if e.is_expired()]
        for eid in expired:
            self._entries.pop(eid, None)
            self._deps.unregister(eid)
        return len(expired)

    def _evict_lru(self) -> None:
        if not self._entries:
            return
        lru = min(self._entries.values(), key=lambda e: e.last_accessed_at)
        self._entries.pop(lru.entry_id, None)
        self._deps.unregister(lru.entry_id)

    def stats(self) -> dict:
        total = self._hits + self._misses
        return {
            "entries": len(self._entries),
            "max_entries": self._max,
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": round(self._hits / max(total, 1), 4),
            "invalidations": self._invalidations,
        }
```

## Solution 5: Cache-Aware Query Executor

```python
from typing import Any, Callable, List, Optional, Set


class CacheAwareQueryExecutor:
    """
    Wraps query execution with semantic cache lookup and population.
    On cache miss, executes the query, records document dependencies,
    and populates the cache for future similar queries.
    """

    def __init__(
        self,
        cache: SemanticResponseCache,
        embed_fn: Callable,    # async fn(text: str) -> List[float]
        min_similarity_to_cache: float = 0.85,
    ):
        self._cache = cache
        self._embed = embed_fn
        self._min_sim = min_similarity_to_cache

    async def execute(
        self,
        query_text: str,
        execute_fn: Callable,   # async fn(query) -> (response, doc_ids, tool_names)
        force_refresh: bool = False,
    ) -> dict:
        query_embedding = await self._embed(query_text)

        if not force_refresh:
            match = await self._cache.get(query_embedding)
            if match:
                entry, similarity = match
                return {
                    "response": entry.response_text,
                    "cache_hit": True,
                    "similarity": round(similarity, 4),
                    "entry_id": entry.entry_id,
                    "entry_age_seconds": round(entry.age_seconds(), 1),
                }

        # Cache miss — execute and cache
        response, doc_ids, tool_names = await execute_fn(query_text)

        entry = await self._cache.put(
            query_text=query_text,
            query_embedding=query_embedding,
            response_text=response,
            document_dependencies=set(doc_ids),
            tool_dependencies=set(tool_names),
        )
        return {
            "response": response,
            "cache_hit": False,
            "entry_id": entry.entry_id,
            "doc_dependencies": list(doc_ids),
        }
```

## Solution 6: Cache Health Monitor

```python
import time


class SemanticCacheHealthMonitor:
    """
    Monitors cache efficiency, invalidation frequency, and entry freshness.
    Recommends tuning when hit rate or similarity threshold is misconfigured.
    """

    def __init__(
        self,
        cache: SemanticResponseCache,
        target_hit_rate: float = 0.40,
    ):
        self._cache = cache
        self._target = target_hit_rate

    def check(self) -> dict:
        stats = self._cache.stats()
        expired = self._cache.evict_expired()
        alerts = []

        if stats["hit_rate"] < self._target and (stats["hits"] + stats["misses"]) > 100:
            alerts.append({
                "type": "low_hit_rate",
                "value": stats["hit_rate"],
                "target": self._target,
                "recommendation": "lower similarity_threshold or increase max_entries",
            })

        if stats["invalidations"] > stats["hits"] * 2:
            alerts.append({
                "type": "high_invalidation_rate",
                "invalidations": stats["invalidations"],
                "hits": stats["hits"],
                "recommendation": "documents change frequently — reduce TTL or use shorter-lived entries",
            })

        return {
            "generated_at": time.time(),
            "healthy": len(alerts) == 0,
            "cache_stats": stats,
            "expired_evicted": expired,
            "alerts": alerts,
        }
```

## Comparison

| Approach | Semantic Matching | Dependency Tracking | Invalidation | TTL Expiry |
|---|---|---|---|---|
| EmbeddingSimilarityMatcher | Yes (cosine) | No | No | No |
| CacheDependencyTracker | No | Yes | Via entries_depending_on | No |
| SemanticResponseCache | Via matcher | Via tracker | Yes (by doc) | Yes |
| CacheAwareQueryExecutor | Via cache | Via execute_fn | No | No |
| SemanticCacheHealthMonitor | No | No | No | Via evict_expired |

**Best for production**: Set `similarity_threshold=0.92` as a starting point — too low produces incorrect cache hits (different questions returning same answer); too high kills hit rate. Track `document_dependencies` for every cached answer by recording which chunk IDs from the vector store were retrieved. Trigger `invalidate_by_document()` in your document ingestion pipeline when any document is updated — this ensures cached answers are never stale. Target hit rate ≥ 40% for a knowledge-base agent with a stable corpus; expect 10–20% for a general-purpose agent with dynamic context.
