---
title: "Agent Doesn't Implement Tool Result Caching with Semantic Key Matching"
description: "Agents that cache tool results using exact parameter matching miss cache hits when two logically equivalent queries differ in phrasing or formatting — 'What is the weather in New York?' and 'NYC weather today' should return the same cached result but produce different cache keys. Implement semantic key matching that embeds query text and uses cosine similarity to find semantically equivalent prior results, increasing effective cache hit rates without sacrificing accuracy."
date: 2026-04-16
difficulty: advanced
category: performance
slug: agent-doesnt-implement-tool-result-caching-with-semantic-key-matching
tags: [semantic-cache, embedding-similarity, tool-cache, cache-hit-rate, cosine-similarity, query-equivalence]
symptoms:
  - "Cache hit rate is low despite many repeated queries due to minor phrasing differences"
  - "Exact-match cache keys cause misses for semantically identical tool calls"
  - "Same web search result fetched multiple times per session with different query wordings"
  - "No measurement of how many cache misses were near-hits (semantically close but not exact)"
  - "Cache effectiveness is much lower than expected given the repetitive nature of tool calls"
---

## Why This Happens

Exact-match caches compute a hash of the serialized tool arguments. Two calls with arguments `{"query": "weather NYC"}` and `{"query": "New York weather today"}` produce different hashes and both hit the upstream API. Semantic caching addresses this by embedding the query text and comparing the embedding against stored embeddings from previous calls. If the cosine similarity exceeds a threshold, the cached result is returned. The challenge is cost: embedding the query requires an API call. Semantic caching is worthwhile when the cost of the embedding call is lower than the cost of the tool call it prevents — which is typically true for expensive external API calls but not for fast internal lookups.

## Solution 1: Semantic Cache Entry

```python
import hashlib
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class SemanticCacheEntry:
    cache_id: str
    tool_name: str
    query_text: str
    query_embedding: List[float]
    result: Any
    exact_key: str             # SHA-256 of serialized args (for exact-match fast path)
    created_at: float = field(default_factory=time.time)
    ttl_seconds: float = 3600.0
    hit_count: int = 0
    quality_score: float = 1.0

    def is_expired(self) -> bool:
        return time.time() - self.created_at > self.ttl_seconds

    @staticmethod
    def make_exact_key(tool_name: str, args: Dict[str, Any]) -> str:
        import json
        raw = json.dumps({"tool": tool_name, "args": args}, sort_keys=True)
        return hashlib.sha256(raw.encode()).hexdigest()
```

## Solution 2: Cosine Similarity Computer

```python
import math
from typing import List


class CosineSimilarityComputer:
    """
    Computes cosine similarity between two embedding vectors.
    Used to determine whether two queries are semantically equivalent.
    """

    @staticmethod
    def compute(a: List[float], b: List[float]) -> float:
        if len(a) != len(b) or not a:
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(x * x for x in b))
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return round(dot / (norm_a * norm_b), 6)

    @staticmethod
    def find_best_match(
        query_embedding: List[float],
        candidates: List[SemanticCacheEntry],
        threshold: float,
    ) -> tuple[Optional[SemanticCacheEntry], float]:
        best_entry = None
        best_sim = 0.0
        for entry in candidates:
            if entry.is_expired():
                continue
            sim = CosineSimilarityComputer.compute(query_embedding, entry.query_embedding)
            if sim > best_sim:
                best_sim = sim
                best_entry = entry
        if best_sim >= threshold:
            return best_entry, best_sim
        return None, best_sim
```

## Solution 3: Semantic Cache Store

```python
import time
from collections import defaultdict
from threading import Lock
from typing import Any, Callable, Dict, List, Optional


class SemanticCacheStore:
    """
    Stores cache entries per tool name and supports both exact-match
    and semantic-similarity lookups. Evicts expired entries on access.
    """

    def __init__(
        self,
        similarity_threshold: float = 0.92,
        max_entries_per_tool: int = 500,
    ):
        self._threshold = similarity_threshold
        self._max = max_entries_per_tool
        self._entries: Dict[str, List[SemanticCacheEntry]] = defaultdict(list)
        self._lock = Lock()
        self._exact_hits = 0
        self._semantic_hits = 0
        self._misses = 0

    def get(
        self,
        tool_name: str,
        exact_key: str,
        query_embedding: Optional[List[float]],
    ) -> tuple[Optional[Any], str, float]:
        """Returns (result, hit_type, similarity). hit_type: 'exact'|'semantic'|'miss'"""
        with self._lock:
            entries = [e for e in self._entries[tool_name] if not e.is_expired()]
            self._entries[tool_name] = entries

            # Exact-match fast path
            for entry in entries:
                if entry.exact_key == exact_key:
                    entry.hit_count += 1
                    self._exact_hits += 1
                    return entry.result, "exact", 1.0

            # Semantic match
            if query_embedding:
                match, sim = CosineSimilarityComputer.find_best_match(
                    query_embedding, entries, self._threshold
                )
                if match:
                    match.hit_count += 1
                    self._semantic_hits += 1
                    return match.result, "semantic", sim

            self._misses += 1
            return None, "miss", 0.0

    def set(self, entry: SemanticCacheEntry) -> None:
        with self._lock:
            tool_entries = self._entries[entry.tool_name]
            if len(tool_entries) >= self._max:
                # Evict oldest expired first, then lowest quality
                tool_entries = [e for e in tool_entries if not e.is_expired()]
                if len(tool_entries) >= self._max:
                    tool_entries = sorted(tool_entries, key=lambda e: e.quality_score)[1:]
            tool_entries.append(entry)
            self._entries[entry.tool_name] = tool_entries

    def stats(self) -> dict:
        total = self._exact_hits + self._semantic_hits + self._misses
        return {
            "exact_hits": self._exact_hits,
            "semantic_hits": self._semantic_hits,
            "misses": self._misses,
            "total_lookups": total,
            "exact_hit_rate": round(self._exact_hits / max(total, 1), 4),
            "semantic_hit_rate": round(self._semantic_hits / max(total, 1), 4),
            "overall_hit_rate": round((self._exact_hits + self._semantic_hits) / max(total, 1), 4),
            "similarity_threshold": self._threshold,
        }
```

## Solution 4: Semantic Cache Tool Wrapper

```python
import time
import uuid
from typing import Any, Callable, Dict, List, Optional


class SemanticCacheToolWrapper:
    """
    Wraps a tool call with semantic caching. Embeds the query,
    checks the cache, and only calls the upstream tool on a miss.
    Stores the result with its embedding for future semantic matching.
    """

    def __init__(
        self,
        store: SemanticCacheStore,
        embed_fn: Callable[[str], List[float]],
        query_extractor: Callable[[Dict[str, Any]], str] = None,
        ttl_seconds: float = 3600.0,
    ):
        self._store = store
        self._embed = embed_fn
        self._extract_query = query_extractor or (lambda args: str(args))
        self._ttl = ttl_seconds

    async def call(
        self,
        tool_name: str,
        args: Dict[str, Any],
        handler: Callable,
    ) -> tuple[Any, dict]:
        exact_key = SemanticCacheEntry.make_exact_key(tool_name, args)
        query_text = self._extract_query(args)

        # Get embedding (may skip for non-text tools)
        try:
            query_embedding = await self._embed(query_text)
        except Exception:
            query_embedding = None

        result, hit_type, similarity = self._store.get(tool_name, exact_key, query_embedding)

        if hit_type != "miss":
            return result, {"cache_hit": True, "hit_type": hit_type, "similarity": similarity}

        # Cache miss — call the tool
        start = time.time()
        result = await handler(**args)
        latency_ms = round((time.time() - start) * 1000, 2)

        if query_embedding:
            entry = SemanticCacheEntry(
                cache_id=str(uuid.uuid4())[:12],
                tool_name=tool_name,
                query_text=query_text,
                query_embedding=query_embedding,
                result=result,
                exact_key=exact_key,
                ttl_seconds=self._ttl,
            )
            self._store.set(entry)

        return result, {"cache_hit": False, "hit_type": "miss", "latency_ms": latency_ms}
```

## Solution 5: Near-Miss Analyzer

```python
from typing import List


class SemanticNearMissAnalyzer:
    """
    Identifies cache misses that were semantically close to existing
    entries but fell below the threshold. Used to tune the similarity
    threshold and discover query normalization opportunities.
    """

    def __init__(self, store: SemanticCacheStore, near_miss_floor: float = 0.80):
        self._store = store
        self._floor = near_miss_floor
        self._near_misses: List[dict] = []

    def record_miss(
        self,
        tool_name: str,
        query_text: str,
        best_similarity: float,
        best_match_query: str = "",
    ) -> None:
        if best_similarity >= self._floor:
            self._near_misses.append({
                "tool_name": tool_name,
                "query": query_text[:100],
                "best_match": best_match_query[:100],
                "similarity": best_similarity,
            })

    def report(self) -> dict:
        if not self._near_misses:
            return {"near_misses": 0}
        avg_sim = sum(n["similarity"] for n in self._near_misses) / len(self._near_misses)
        return {
            "near_misses": len(self._near_misses),
            "avg_similarity": round(avg_sim, 4),
            "examples": self._near_misses[:5],
            "recommendation": (
                f"Consider lowering threshold to {round(avg_sim - 0.02, 2)} to capture these hits"
                if avg_sim > self._store._threshold - 0.05 else "threshold appears well-calibrated"
            ),
        }
```

## Solution 6: Semantic Cache Dashboard

```python
import time


class SemanticCacheDashboard:
    """
    Combines cache stats, near-miss analysis, and threshold guidance
    into a single tuning and operational report.
    """

    def __init__(
        self,
        store: SemanticCacheStore,
        analyzer: SemanticNearMissAnalyzer,
    ):
        self._store = store
        self._analyzer = analyzer

    def render(self) -> dict:
        stats = self._store.stats()
        near_miss_report = self._analyzer.report()
        return {
            "generated_at": time.time(),
            "cache_stats": stats,
            "near_miss_analysis": near_miss_report,
            "efficiency": {
                "overall_hit_rate": stats["overall_hit_rate"],
                "semantic_lift": round(
                    stats["semantic_hit_rate"] / max(stats["exact_hit_rate"] + stats["semantic_hit_rate"], 0.001), 4
                ),
            },
        }
```

## Comparison

| Approach | Exact-Match Fast Path | Semantic Similarity | Near-Miss Detection | TTL Eviction | Dashboard |
|---|---|---|---|---|---|
| SemanticCacheStore | Yes | Yes (cosine) | No | Yes (on access) | No |
| SemanticCacheToolWrapper | Via store | Via store | No | Via store | No |
| CosineSimilarityComputer | No | Yes (compute + find) | No | No | No |
| SemanticNearMissAnalyzer | No | No | Yes | No | No |
| SemanticCacheDashboard | No | No | Via analyzer | No | Yes |

**Best for production**: Set `similarity_threshold=0.92` as the starting point — below 0.85 risks false hits (returning wrong results for different-meaning queries). Only apply semantic caching to tools with expensive upstream calls (external APIs, LLM calls); for fast database lookups, exact-match caching is sufficient and avoids embedding overhead. Use `SemanticNearMissAnalyzer` to calibrate the threshold on real traffic — a high near-miss rate with average similarity of 0.89 suggests the threshold can be safely lowered to 0.88. Cache the embedding of the query itself rather than re-embedding on every lookup by storing embeddings in the cache entry — the embedding cost is paid once at write time.
