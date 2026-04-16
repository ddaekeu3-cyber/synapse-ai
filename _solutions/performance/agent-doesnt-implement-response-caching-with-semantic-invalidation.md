---
title: "Agent Doesn't Implement Response Caching with Semantic Invalidation"
description: "Agents that cache responses only by exact input hash miss the vast majority of cacheable queries — two users asking 'what's the weather in Paris?' and 'Paris weather today?' receive separate LLM calls despite being semantically equivalent. Implement response caching with semantic similarity matching for cache lookup and domain-aware invalidation that expires cached responses when underlying data changes."
date: 2026-04-16
difficulty: advanced
category: performance
slug: agent-doesnt-implement-response-caching-with-semantic-invalidation
tags: [response-caching, semantic-cache, cache-invalidation, similarity-lookup, llm-cache, cache-freshness]
symptoms:
  - "Paraphrased versions of the same question each trigger a full LLM call"
  - "Cache hit rate is below 5% because only exact-match queries hit the cache"
  - "No mechanism to invalidate cached responses when the underlying data they describe changes"
  - "Cached responses are served indefinitely even when the facts they contain are stale"
  - "High LLM cost despite many users asking similar questions"
---

## Why This Happens

Exact-match caching keys on the raw query string, so even minor paraphrasing causes a cache miss. Semantic caching keys on the embedding of the query and considers two queries a cache hit if their embeddings are within a cosine similarity threshold. Invalidation is harder than lookup: a cached response about stock prices becomes stale when the market closes; a cached response about a user's account balance becomes stale on any account activity. Semantic invalidation requires tagging each cached entry with the data domains it depends on and expiring those entries when the domain signals a data change.

## Solution 1: Semantic Cache Entry

```python
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, List, Optional


@dataclass
class SemanticCacheEntry:
    cache_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    query: str = ""
    query_embedding: List[float] = field(default_factory=list)
    response: Any = None
    domain_tags: List[str] = field(default_factory=list)  # e.g. ["weather", "paris"]
    created_at: float = field(default_factory=time.time)
    ttl_seconds: float = 300.0
    access_count: int = 0
    last_accessed_at: float = field(default_factory=time.time)

    def is_expired(self) -> bool:
        return time.time() - self.created_at > self.ttl_seconds

    def touch(self) -> None:
        self.access_count += 1
        self.last_accessed_at = time.time()
```

## Solution 2: Semantic Response Cache

```python
import math
import time
from threading import Lock
from typing import Any, Callable, Dict, List, Optional, Tuple


class SemanticResponseCache:
    """
    Caches agent responses keyed by query embedding.
    Performs approximate nearest-neighbor lookup using cosine similarity.
    Supports domain-tag-based invalidation.
    """

    def __init__(
        self,
        embed_fn: Callable[[str], List[float]],
        similarity_threshold: float = 0.92,
        max_entries: int = 5000,
    ):
        self._embed_fn = embed_fn
        self._threshold = similarity_threshold
        self._max = max_entries
        self._entries: Dict[str, SemanticCacheEntry] = {}
        self._lock = Lock()
        self._hits = 0
        self._misses = 0

    @staticmethod
    def _cosine(a: List[float], b: List[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(x * x for x in b))
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)

    def get(self, query: str) -> Optional[Tuple[Any, float]]:
        """Returns (response, similarity) or None on miss."""
        query_emb = self._embed_fn(query)
        now = time.time()

        with self._lock:
            best_sim = 0.0
            best_entry: Optional[SemanticCacheEntry] = None

            for entry in self._entries.values():
                if entry.is_expired():
                    continue
                sim = self._cosine(query_emb, entry.query_embedding)
                if sim > best_sim:
                    best_sim = sim
                    best_entry = entry

            if best_entry and best_sim >= self._threshold:
                best_entry.touch()
                self._hits += 1
                return best_entry.response, round(best_sim, 4)

        self._misses += 1
        return None

    def set(
        self,
        query: str,
        response: Any,
        domain_tags: List[str] = None,
        ttl_seconds: float = 300.0,
    ) -> None:
        query_emb = self._embed_fn(query)
        entry = SemanticCacheEntry(
            query=query,
            query_embedding=query_emb,
            response=response,
            domain_tags=domain_tags or [],
            ttl_seconds=ttl_seconds,
        )
        with self._lock:
            if len(self._entries) >= self._max:
                self._evict_lru()
            self._entries[entry.cache_id] = entry

    def _evict_lru(self) -> None:
        if not self._entries:
            return
        lru_id = min(self._entries, key=lambda k: self._entries[k].last_accessed_at)
        del self._entries[lru_id]

    def hit_rate(self) -> float:
        total = self._hits + self._misses
        return round(self._hits / max(total, 1), 4)

    def stats(self) -> dict:
        with self._lock:
            active = sum(1 for e in self._entries.values() if not e.is_expired())
        return {
            "entries": len(self._entries),
            "active_entries": active,
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": self.hit_rate(),
        }
```

## Solution 3: Domain Tag Invalidator

```python
import time
from threading import Lock
from typing import Dict, List


class DomainTagInvalidator:
    """
    Invalidates cache entries by domain tag when underlying data changes.
    Supports both immediate invalidation and scheduled future expiry.
    """

    def __init__(self, cache: SemanticResponseCache):
        self._cache = cache
        self._invalidation_log: List[dict] = []
        self._lock = Lock()

    def invalidate_domain(self, domain_tag: str) -> int:
        """Immediately expire all entries tagged with domain_tag."""
        invalidated = 0
        with self._cache._lock:
            for entry in self._cache._entries.values():
                if domain_tag in entry.domain_tags and not entry.is_expired():
                    # Force expiry by setting created_at to past
                    entry.ttl_seconds = 0
                    invalidated += 1

        self._invalidation_log.append({
            "domain": domain_tag,
            "invalidated_count": invalidated,
            "invalidated_at": time.time(),
        })
        return invalidated

    def invalidate_domains(self, domain_tags: List[str]) -> Dict[str, int]:
        return {tag: self.invalidate_domain(tag) for tag in domain_tags}

    def schedule_invalidation(
        self,
        domain_tag: str,
        delay_seconds: float,
    ) -> None:
        import threading
        timer = threading.Timer(
            delay_seconds,
            self.invalidate_domain,
            args=[domain_tag],
        )
        timer.daemon = True
        timer.start()

    def invalidation_history(self, limit: int = 20) -> List[dict]:
        return self._invalidation_log[-limit:]
```

## Solution 4: Cache-Backed Response Generator

```python
import time
from typing import Any, Callable, Dict, List, Optional


class CacheBackedResponseGenerator:
    """
    Checks the semantic cache before calling the LLM.
    Caches successful responses with domain tags for future invalidation.
    """

    def __init__(
        self,
        cache: SemanticResponseCache,
        generate_fn: Callable,
        domain_classifier: Optional[Callable[[str], List[str]]] = None,
        default_ttl_seconds: float = 300.0,
    ):
        self._cache = cache
        self._generate = generate_fn
        self._domain_classifier = domain_classifier
        self._default_ttl = default_ttl_seconds
        self._call_log: List[dict] = []

    async def generate(
        self,
        query: str,
        context: Optional[str] = None,
    ) -> dict:
        start = time.time()
        cache_result = self._cache.get(query)

        if cache_result is not None:
            response, similarity = cache_result
            elapsed_ms = round((time.time() - start) * 1000, 2)
            self._call_log.append({"cache_hit": True, "elapsed_ms": elapsed_ms})
            return {
                "response": response,
                "cache_hit": True,
                "similarity": similarity,
                "elapsed_ms": elapsed_ms,
            }

        response = await self._generate(query, context)
        elapsed_ms = round((time.time() - start) * 1000, 2)

        domain_tags = []
        if self._domain_classifier:
            domain_tags = self._domain_classifier(query)

        self._cache.set(
            query=query,
            response=response,
            domain_tags=domain_tags,
            ttl_seconds=self._default_ttl,
        )

        self._call_log.append({"cache_hit": False, "elapsed_ms": elapsed_ms})
        return {
            "response": response,
            "cache_hit": False,
            "similarity": None,
            "elapsed_ms": elapsed_ms,
            "domain_tags": domain_tags,
        }

    def cost_savings_estimate(self, cost_per_llm_call: float = 0.002) -> dict:
        hits = sum(1 for r in self._call_log if r["cache_hit"])
        return {
            "total_calls": len(self._call_log),
            "cache_hits": hits,
            "estimated_savings_usd": round(hits * cost_per_llm_call, 4),
        }
```

## Solution 5: Freshness Policy Manager

```python
from typing import Dict, Optional


DOMAIN_TTL_POLICIES: Dict[str, float] = {
    "weather": 600.0,        # 10 minutes
    "stock_price": 60.0,     # 1 minute
    "news": 1800.0,          # 30 minutes
    "account_balance": 30.0, # 30 seconds
    "documentation": 86400.0, # 24 hours
    "static_fact": 604800.0, # 7 days
}


class FreshnessPolicyManager:
    """
    Returns the appropriate TTL for a cache entry based on domain tags.
    Uses the shortest TTL among all applicable domains.
    """

    def __init__(self, custom_policies: Dict[str, float] = None):
        self._policies = {**DOMAIN_TTL_POLICIES, **(custom_policies or {})}
        self._default_ttl = 300.0

    def ttl_for_domains(self, domain_tags: list) -> float:
        applicable = [
            self._policies[tag]
            for tag in domain_tags
            if tag in self._policies
        ]
        return min(applicable) if applicable else self._default_ttl

    def register(self, domain: str, ttl_seconds: float) -> None:
        self._policies[domain] = ttl_seconds
```

## Solution 6: Semantic Cache Dashboard

```python
import time


class SemanticCacheDashboard:
    """
    Combines cache statistics, invalidation history, and
    cost savings estimates into a single operational view.
    """

    def __init__(
        self,
        cache: SemanticResponseCache,
        invalidator: DomainTagInvalidator,
        generator: CacheBackedResponseGenerator,
    ):
        self._cache = cache
        self._invalidator = invalidator
        self._generator = generator

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "cache_stats": self._cache.stats(),
            "recent_invalidations": self._invalidator.invalidation_history(limit=5),
            "cost_savings": self._generator.cost_savings_estimate(),
        }
```

## Comparison

| Approach | Semantic Lookup | Domain Invalidation | TTL Policy | Cost Tracking | Dashboard |
|---|---|---|---|---|---|
| SemanticResponseCache | Yes (cosine sim) | No | Per-entry TTL | No | No |
| DomainTagInvalidator | No | Yes (tag-based) | No | No | No |
| CacheBackedResponseGenerator | Via cache | Via invalidator | Via freshness | Yes | No |
| FreshnessPolicyManager | No | No | Yes (domain TTLs) | No | No |
| SemanticCacheDashboard | No | No | No | Via generator | Yes |

**Best for production**: Set `similarity_threshold=0.92` as a starting point — at this level, paraphrases of the same question reliably hit the cache while semantically different questions do not. Tag every cached entry with domain labels and use `FreshnessPolicyManager` to apply the shortest applicable TTL rather than a global default. Register a data-change webhook from your data sources (market data feed, weather API, user account service) to call `DomainTagInvalidator.invalidate_domain()` immediately on data updates — this gives sub-second invalidation latency instead of waiting for TTL expiry for time-critical domains.
