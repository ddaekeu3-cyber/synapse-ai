---
title: "Agent Doesn't Implement Response Cache for Deterministic Queries"
description: "Agents that re-invoke the LLM for repeated identical or near-identical queries pay full inference cost each time — a FAQ bot answering 'What are your business hours?' a thousand times a day makes a thousand LLM calls when one cached response would suffice. Implement a response cache for deterministic queries that stores LLM responses keyed on a normalized query hash, serves cache hits without model inference, and applies TTL-based expiry for time-sensitive content."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-response-cache-for-deterministic-queries
tags: [response-cache, llm-cache, deterministic-queries, cost-reduction, cache-hit-rate, semantic-cache]
symptoms:
  - "Identical user questions trigger separate LLM calls each time"
  - "FAQ-style queries consume the same token budget as novel complex questions"
  - "No cache layer between user requests and LLM inference for stable content"
  - "Response latency identical for a repeated question as for a first-time question"
  - "Monthly LLM cost scales linearly with request volume even for repetitive query patterns"
---

## Why This Happens

LLM APIs are stateless: every call to the API is priced and timed independently. For questions with deterministic or near-deterministic answers (FAQ, policy lookups, calculation results, static explanations), calling the model repeatedly is wasteful. A response cache stores the model's answer the first time and returns it directly on subsequent identical queries. The challenge is defining cache key equivalence: exact-match caching misses paraphrased questions; semantic caching (embedding similarity) trades exactness for coverage but risks returning a cached response for a question that is similar but not equivalent. A practical solution uses exact-match caching for high-confidence hits and a configurable similarity threshold for near-match caching.

## Solution 1: Cache Key Strategy

```python
import hashlib
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional


class CacheKeyStrategy(str, Enum):
    EXACT = "exact"           # SHA-256 of normalized query text
    SEMANTIC = "semantic"     # cosine similarity of query embedding
    HYBRID = "hybrid"         # exact first, semantic fallback


@dataclass
class CacheKeyConfig:
    strategy: CacheKeyStrategy = CacheKeyStrategy.EXACT
    normalize_whitespace: bool = True
    normalize_case: bool = True
    strip_punctuation: bool = False
    semantic_similarity_threshold: float = 0.92
    include_system_prompt_hash: bool = True   # different system prompts = different cache


class QueryNormalizer:
    """Normalizes query text before hashing to improve exact-match hit rates."""

    def __init__(self, config: CacheKeyConfig):
        self._config = config

    def normalize(self, query: str) -> str:
        if self._config.normalize_whitespace:
            query = re.sub(r"\s+", " ", query).strip()
        if self._config.normalize_case:
            query = query.lower()
        if self._config.strip_punctuation:
            query = re.sub(r"[^\w\s]", "", query)
        return query

    def exact_key(self, query: str, system_prompt: str = "") -> str:
        normalized = self.normalize(query)
        if self._config.include_system_prompt_hash:
            sp_hash = hashlib.sha256(system_prompt.encode()).hexdigest()[:8]
            raw = f"{sp_hash}:{normalized}"
        else:
            raw = normalized
        return hashlib.sha256(raw.encode()).hexdigest()
```

## Solution 2: Response Cache Entry

```python
import time
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class ResponseCacheEntry:
    cache_key: str
    query_normalized: str
    response: Any
    model_id: str
    input_tokens: int
    output_tokens: int
    cached_at: float = field(default_factory=time.time)
    hit_count: int = 0
    ttl_seconds: Optional[float] = None

    def is_expired(self) -> bool:
        if self.ttl_seconds is None:
            return False
        return time.time() - self.cached_at > self.ttl_seconds

    def estimated_cost_usd(
        self,
        input_cost_per_1k: float = 0.003,
        output_cost_per_1k: float = 0.015,
    ) -> float:
        return round(
            self.input_tokens * input_cost_per_1k / 1000
            + self.output_tokens * output_cost_per_1k / 1000,
            6,
        )
```

## Solution 3: Exact Match Response Cache

```python
import time
from collections import OrderedDict
from threading import Lock
from typing import Optional


class ExactMatchResponseCache:
    """
    LRU cache keyed on normalized query hash.
    Evicts expired and LRU entries to stay within capacity.
    """

    def __init__(
        self,
        max_entries: int = 5000,
        default_ttl_seconds: Optional[float] = 3600.0,
    ):
        self._max = max_entries
        self._default_ttl = default_ttl_seconds
        self._cache: OrderedDict[str, ResponseCacheEntry] = OrderedDict()
        self._lock = Lock()
        self._hits = 0
        self._misses = 0
        self._evictions = 0

    def get(self, cache_key: str) -> Optional[ResponseCacheEntry]:
        with self._lock:
            entry = self._cache.get(cache_key)
            if entry is None:
                self._misses += 1
                return None
            if entry.is_expired():
                del self._cache[cache_key]
                self._misses += 1
                self._evictions += 1
                return None
            entry.hit_count += 1
            self._cache.move_to_end(cache_key)
            self._hits += 1
            return entry

    def put(
        self,
        entry: ResponseCacheEntry,
        ttl_seconds: Optional[float] = None,
    ) -> None:
        if ttl_seconds is not None:
            entry.ttl_seconds = ttl_seconds
        elif entry.ttl_seconds is None:
            entry.ttl_seconds = self._default_ttl
        with self._lock:
            if cache_key := entry.cache_key:
                if cache_key in self._cache:
                    self._cache.move_to_end(cache_key)
                else:
                    if len(self._cache) >= self._max:
                        self._cache.popitem(last=False)
                        self._evictions += 1
                self._cache[cache_key] = entry

    def stats(self) -> dict:
        with self._lock:
            total = self._hits + self._misses
            total_hits_saved = sum(e.hit_count for e in self._cache.values())
        return {
            "entries": len(self._cache),
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": round(self._hits / max(total, 1), 4),
            "evictions": self._evictions,
            "total_cache_hits_served": total_hits_saved,
        }
```

## Solution 4: Caching LLM Client

```python
import time
from typing import Any, Callable, Optional


class CachingLLMClient:
    """
    Wraps an LLM client with exact-match response caching.
    On a cache hit, returns the stored response immediately.
    On a miss, invokes the model and stores the result.
    """

    def __init__(
        self,
        llm_fn: Callable,
        cache: ExactMatchResponseCache,
        normalizer: QueryNormalizer,
        model_id: str = "claude-sonnet-4-6",
        cacheable_query_check: Optional[Callable[[str], bool]] = None,
    ):
        self._llm = llm_fn
        self._cache = cache
        self._normalizer = normalizer
        self._model_id = model_id
        self._is_cacheable = cacheable_query_check or self._default_cacheable
        self._tokens_saved = 0

    @staticmethod
    def _default_cacheable(query: str) -> bool:
        """Heuristic: short, question-like queries are more likely to be cacheable."""
        return len(query) < 500

    async def complete(
        self,
        query: str,
        system_prompt: str = "",
        ttl_seconds: Optional[float] = 3600.0,
        **kwargs: Any,
    ) -> dict:
        if not self._is_cacheable(query):
            response = await self._llm(query=query, system_prompt=system_prompt, **kwargs)
            return {"response": response, "cache_hit": False, "cache_key": None}

        cache_key = self._normalizer.exact_key(query, system_prompt)
        cached = self._cache.get(cache_key)

        if cached:
            self._tokens_saved += cached.input_tokens + cached.output_tokens
            return {
                "response": cached.response,
                "cache_hit": True,
                "cache_key": cache_key,
                "cached_at": cached.cached_at,
                "hit_count": cached.hit_count,
            }

        # Cache miss: call model
        start = time.time()
        response = await self._llm(query=query, system_prompt=system_prompt, **kwargs)
        latency_ms = (time.time() - start) * 1000

        # Extract token usage if available
        usage = getattr(response, "usage", None)
        input_tokens = getattr(usage, "input_tokens", 0) if usage else 0
        output_tokens = getattr(usage, "output_tokens", 0) if usage else 0

        entry = ResponseCacheEntry(
            cache_key=cache_key,
            query_normalized=self._normalizer.normalize(query),
            response=response,
            model_id=self._model_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            ttl_seconds=ttl_seconds,
        )
        self._cache.put(entry)

        return {
            "response": response,
            "cache_hit": False,
            "cache_key": cache_key,
            "latency_ms": round(latency_ms, 2),
        }

    def tokens_saved(self) -> int:
        return self._tokens_saved
```

## Solution 5: Cache Warming Loader

```python
from typing import Any, Callable, List


class ResponseCacheWarmingLoader:
    """
    Pre-populates the response cache with answers to known high-frequency
    queries before the agent starts serving traffic.
    """

    def __init__(self, client: CachingLLMClient):
        self._client = client

    async def warm(
        self,
        seed_queries: List[dict],   # list of {query, system_prompt, ttl_seconds}
    ) -> dict:
        warmed = 0
        failed = 0
        for seed in seed_queries:
            try:
                result = await self._client.complete(
                    query=seed["query"],
                    system_prompt=seed.get("system_prompt", ""),
                    ttl_seconds=seed.get("ttl_seconds", 86400.0),
                )
                if not result["cache_hit"]:
                    warmed += 1
            except Exception:
                failed += 1
        return {"warmed": warmed, "already_cached": len(seed_queries) - warmed - failed, "failed": failed}
```

## Solution 6: Response Cache Dashboard

```python
import time


class ResponseCacheDashboard:
    """
    Combines cache stats and token savings into an operational report.
    """

    def __init__(
        self,
        cache: ExactMatchResponseCache,
        client: CachingLLMClient,
        input_cost_per_1k: float = 0.003,
        output_cost_per_1k: float = 0.015,
    ):
        self._cache = cache
        self._client = client
        self._input_cost = input_cost_per_1k
        self._output_cost = output_cost_per_1k

    def render(self) -> dict:
        stats = self._cache.stats()
        tokens_saved = self._client.tokens_saved()
        # Rough cost savings estimate (assume 50/50 input/output split)
        cost_saved = round(
            tokens_saved / 2 * self._input_cost / 1000
            + tokens_saved / 2 * self._output_cost / 1000,
            4,
        )
        return {
            "generated_at": time.time(),
            "cache_stats": stats,
            "tokens_saved": tokens_saved,
            "estimated_cost_saved_usd": cost_saved,
        }
```

## Comparison

| Approach | Exact Match Caching | TTL Expiry | LLM Wrapping | Cache Warming | Cost Tracking |
|---|---|---|---|---|---|
| ExactMatchResponseCache | Yes (SHA-256) | Yes | No | No | No |
| CachingLLMClient | Via cache | Via cache | Yes | No | Token savings |
| ResponseCacheWarmingLoader | No | No | Via client | Yes | No |
| ResponseCacheDashboard | No | No | No | No | Yes |

**Best for production**: Set `default_ttl_seconds=3600` for general queries and longer TTLs (86400s) for truly static content like policy documents or product descriptions. Use `cacheable_query_check` to exclude queries that contain personal pronouns ("my", "I", "mine"), dates, or account-specific terms — these are unlikely to be shared across users. Pre-warm the cache with your top-20 FAQ queries at startup using `ResponseCacheWarmingLoader` — this ensures the first users after a deployment get cached responses immediately. Monitor `hit_rate` daily: below 20% suggests your query distribution is too varied for caching to help; above 60% suggests strong repetition that warrants expanding the cache size to capture more of the long tail.
