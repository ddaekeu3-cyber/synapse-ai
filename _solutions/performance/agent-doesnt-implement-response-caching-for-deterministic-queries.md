---
title: "Agent Doesn't Implement Response Caching for Deterministic Queries"
description: "Agents that re-execute LLM calls for identical or near-identical queries waste tokens and latency: the same FAQ question answered three times in a session, the same code explanation requested by different users, the same structured extraction run on the same document. Implement semantic response caching that stores LLM responses keyed by a normalized query fingerprint, serves cache hits without an LLM call, and expires entries based on content freshness requirements."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-response-caching-for-deterministic-queries
tags: [response-caching, llm-cache, semantic-cache, token-savings, deterministic-queries, cache-hit-rate]
symptoms:
  - "Same user question triggers a full LLM round-trip every time — no deduplication across sessions"
  - "Identical structured extraction prompts re-run on documents that haven't changed"
  - "No measurement of how many LLM calls could have been served from cache"
  - "Cache entries never expire — stale responses are served for queries about time-sensitive topics"
  - "Cache key is the raw prompt string — minor whitespace differences cause unnecessary cache misses"
---

## Why This Happens

LLM calls are expensive in both latency and tokens. Many agent queries are deterministic or near-deterministic: the same document summarization prompt produces the same output; a classification task on a fixed schema is stable across callers. Without a caching layer, every call goes to the model. Building an effective cache requires a normalization step (so equivalent queries hit the same key), a TTL policy matched to content freshness requirements, and a hit/miss tracking layer so the cache's effectiveness is measurable. Without measurement, the cache is invisible and its value cannot be justified.

## Solution 1: Query Normalizer

```python
import hashlib
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass
class NormalizedQuery:
    original: str
    normalized: str
    fingerprint: str
    model: str
    temperature: float
    cache_key: str


class QueryNormalizer:
    """
    Normalizes a query string and model parameters into a stable cache key.
    Strips cosmetic differences (whitespace, case for known patterns) that
    should not produce distinct cache entries.
    """

    def normalize(
        self,
        prompt: str,
        model: str = "",
        temperature: float = 0.0,
        system_prompt: str = "",
        extra_params: Optional[Dict[str, Any]] = None,
    ) -> NormalizedQuery:
        normalized = unicodedata.normalize("NFKC", prompt)
        normalized = re.sub(r"[ \t]+", " ", normalized)
        normalized = re.sub(r"\n{3,}", "\n\n", normalized)
        normalized = normalized.strip()

        key_parts = [
            f"m:{model}",
            f"t:{temperature:.2f}",
            f"s:{hashlib.sha256(system_prompt.encode()).hexdigest()[:8]}",
            f"p:{hashlib.sha256(normalized.encode()).hexdigest()}",
        ]
        if extra_params:
            import json
            key_parts.append(f"x:{hashlib.sha256(json.dumps(extra_params, sort_keys=True).encode()).hexdigest()[:8]}")

        cache_key = ":".join(key_parts)
        fingerprint = hashlib.sha256(cache_key.encode()).hexdigest()[:16]

        return NormalizedQuery(
            original=prompt,
            normalized=normalized,
            fingerprint=fingerprint,
            model=model,
            temperature=temperature,
            cache_key=cache_key,
        )
```

## Solution 2: TTL-Based Response Cache

```python
import time
from dataclasses import dataclass, field
from threading import Lock
from typing import Any, Dict, Optional


@dataclass
class CachedResponse:
    cache_key: str
    value: Any
    model: str
    token_count: int
    cached_at: float = field(default_factory=time.time)
    hit_count: int = 0
    ttl_seconds: float = 3600.0

    def is_expired(self) -> bool:
        return time.time() - self.cached_at > self.ttl_seconds

    def touch(self) -> None:
        self.hit_count += 1


class TTLResponseCache:
    """
    In-memory LRU cache with per-entry TTL. Entries are evicted on access
    if expired, and the cache is bounded by max_entries.
    """

    def __init__(self, max_entries: int = 1000):
        self._max = max_entries
        self._store: Dict[str, CachedResponse] = {}
        self._lock = Lock()

    def get(self, cache_key: str) -> Optional[CachedResponse]:
        with self._lock:
            entry = self._store.get(cache_key)
            if entry is None:
                return None
            if entry.is_expired():
                del self._store[cache_key]
                return None
            entry.touch()
            # LRU: move to end
            self._store[cache_key] = self._store.pop(cache_key)
            return entry

    def put(
        self,
        cache_key: str,
        value: Any,
        model: str = "",
        token_count: int = 0,
        ttl_seconds: float = 3600.0,
    ) -> CachedResponse:
        with self._lock:
            if len(self._store) >= self._max:
                # Evict oldest (first inserted / LRU head)
                oldest_key = next(iter(self._store))
                del self._store[oldest_key]
            entry = CachedResponse(
                cache_key=cache_key,
                value=value,
                model=model,
                token_count=token_count,
                ttl_seconds=ttl_seconds,
            )
            self._store[cache_key] = entry
            return entry

    def invalidate(self, cache_key: str) -> bool:
        with self._lock:
            return self._store.pop(cache_key, None) is not None

    def size(self) -> int:
        with self._lock:
            return len(self._store)
```

## Solution 3: TTL Policy Selector

```python
import re
from dataclasses import dataclass
from typing import List, Tuple


@dataclass
class CacheTTLPolicy:
    pattern: str           # regex matched against normalized prompt
    ttl_seconds: float
    label: str             # human-readable reason


DEFAULT_TTL_POLICIES: List[CacheTTLPolicy] = [
    CacheTTLPolicy(r"today|right now|current(ly)?|latest", 60.0, "time_sensitive"),
    CacheTTLPolicy(r"price|stock|weather|news", 300.0, "volatile_data"),
    CacheTTLPolicy(r"summarize|explain|translate|classify", 86400.0, "stable_nlp_task"),
    CacheTTLPolicy(r"extract|parse|convert", 43200.0, "deterministic_transform"),
]

DEFAULT_TTL_SECONDS = 3600.0


class TTLPolicySelector:
    """
    Selects the appropriate TTL for a query based on content signals.
    Lower TTL wins when multiple policies match.
    """

    def __init__(self, policies: List[CacheTTLPolicy] = None):
        self._policies = policies or DEFAULT_TTL_POLICIES

    def select(self, normalized_prompt: str) -> Tuple[float, str]:
        matched_ttl = DEFAULT_TTL_SECONDS
        matched_label = "default"
        for policy in self._policies:
            if re.search(policy.pattern, normalized_prompt, re.IGNORECASE):
                if policy.ttl_seconds < matched_ttl:
                    matched_ttl = policy.ttl_seconds
                    matched_label = policy.label
        return matched_ttl, matched_label
```

## Solution 4: Caching LLM Client

```python
import time
from typing import Any, Callable, Optional


class CachingLLMClient:
    """
    Wraps an LLM call function with query normalization and response caching.
    Returns cached responses immediately; records misses for billing tracking.
    """

    def __init__(
        self,
        cache: TTLResponseCache,
        normalizer: QueryNormalizer,
        ttl_selector: TTLPolicySelector,
        stats_recorder: "CacheStatsRecorder",
    ):
        self._cache = cache
        self._normalizer = normalizer
        self._ttl_selector = ttl_selector
        self._stats = stats_recorder

    async def call(
        self,
        prompt: str,
        llm_fn: Callable,
        model: str = "",
        temperature: float = 0.0,
        system_prompt: str = "",
        cacheable: bool = True,
        **kwargs: Any,
    ) -> dict:
        nq = self._normalizer.normalize(prompt, model, temperature, system_prompt)

        if cacheable and temperature == 0.0:
            cached = self._cache.get(nq.cache_key)
            if cached:
                self._stats.record_hit(model, cached.token_count)
                return {
                    "response": cached.value,
                    "cache_hit": True,
                    "cache_key": nq.cache_key,
                    "hit_count": cached.hit_count,
                }

        start = time.monotonic()
        raw_response = await llm_fn(prompt, model=model, temperature=temperature, system_prompt=system_prompt, **kwargs)
        latency_ms = round((time.monotonic() - start) * 1000, 2)

        token_count = raw_response.get("usage", {}).get("total_tokens", 0) if isinstance(raw_response, dict) else 0
        response_value = raw_response.get("content", raw_response) if isinstance(raw_response, dict) else raw_response

        if cacheable and temperature == 0.0:
            ttl, _ = self._ttl_selector.select(nq.normalized)
            self._cache.put(nq.cache_key, response_value, model, token_count, ttl)

        self._stats.record_miss(model, token_count, latency_ms)
        return {
            "response": response_value,
            "cache_hit": False,
            "cache_key": nq.cache_key,
            "latency_ms": latency_ms,
            "tokens_used": token_count,
        }
```

## Solution 5: Cache Stats Recorder

```python
import time
from collections import deque
from threading import Lock
from typing import Deque, Tuple


class CacheStatsRecorder:
    """
    Tracks cache hit/miss counts and estimated token savings.
    """

    def __init__(self, window_size: int = 10000):
        self._window = window_size
        self._hits: Deque[Tuple[float, str, int]] = deque(maxlen=window_size)
        self._misses: Deque[Tuple[float, str, int, float]] = deque(maxlen=window_size)
        self._lock = Lock()

    def record_hit(self, model: str, tokens_saved: int) -> None:
        with self._lock:
            self._hits.append((time.time(), model, tokens_saved))

    def record_miss(self, model: str, tokens_used: int, latency_ms: float) -> None:
        with self._lock:
            self._misses.append((time.time(), model, tokens_used, latency_ms))

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        with self._lock:
            recent_hits = [(ts, m, t) for ts, m, t in self._hits if ts >= cutoff]
            recent_misses = [(ts, m, t, l) for ts, m, t, l in self._misses if ts >= cutoff]

        total = len(recent_hits) + len(recent_misses)
        if total == 0:
            return {"window_seconds": window_seconds, "requests": 0}

        tokens_saved = sum(t for _, _, t in recent_hits)
        tokens_used = sum(t for _, _, t, _ in recent_misses)
        avg_miss_latency = (
            sum(l for _, _, _, l in recent_misses) / len(recent_misses)
            if recent_misses else 0.0
        )

        return {
            "window_seconds": window_seconds,
            "requests": total,
            "hits": len(recent_hits),
            "misses": len(recent_misses),
            "hit_rate": round(len(recent_hits) / total, 4),
            "tokens_saved_est": tokens_saved,
            "tokens_used": tokens_used,
            "savings_pct": round(tokens_saved / max(tokens_saved + tokens_used, 1) * 100, 1),
            "avg_miss_latency_ms": round(avg_miss_latency, 2),
        }
```

## Solution 6: Cache Warming Scheduler

```python
import asyncio
import time
from typing import Any, Callable, List, Tuple


class CacheWarmingScheduler:
    """
    Pre-populates the cache with responses to known high-frequency queries
    before traffic arrives. Runs at startup and on a configurable interval.
    """

    def __init__(
        self,
        client: CachingLLMClient,
        warm_queries: List[Tuple[str, str, float]],  # (prompt, model, temperature)
        interval_seconds: float = 3600.0,
    ):
        self._client = client
        self._queries = warm_queries
        self._interval = interval_seconds
        self._last_warm_at: float = 0.0
        self._warm_count = 0

    async def warm(self, llm_fn: Callable) -> dict:
        results = {"warmed": 0, "failed": 0, "skipped": 0}
        for prompt, model, temperature in self._queries:
            try:
                result = await self._client.call(
                    prompt=prompt, llm_fn=llm_fn, model=model,
                    temperature=temperature, cacheable=True,
                )
                if result.get("cache_hit"):
                    results["skipped"] += 1
                else:
                    results["warmed"] += 1
            except Exception:
                results["failed"] += 1
        self._last_warm_at = time.time()
        self._warm_count += 1
        return results

    async def run_loop(self, llm_fn: Callable) -> None:
        while True:
            await self.warm(llm_fn)
            await asyncio.sleep(self._interval)
```

## Comparison

| Approach | Query Normalization | TTL Policy | LRU Eviction | Hit/Miss Tracking | Cache Warming |
|---|---|---|---|---|---|
| QueryNormalizer | Yes (NFKC + whitespace) | No | No | No | No |
| TTLResponseCache | No | Per-entry TTL | Yes | No | No |
| TTLPolicySelector | No | Yes (regex patterns) | No | No | No |
| CachingLLMClient | Via normalizer | Via selector | Via cache | Via recorder | No |
| CacheStatsRecorder | No | No | No | Yes (tokens + latency) | No |
| CacheWarmingScheduler | No | No | No | No | Yes |

**Best for production**: Only cache responses where `temperature=0.0` — non-zero temperature requests are stochastic and caching them would suppress the diversity the caller explicitly requested. Set TTL by content type rather than a global value: code explanations can be cached for 24 hours; responses mentioning "today" or "current" should expire in under 5 minutes. Monitor `hit_rate` via `CacheStatsRecorder`: a hit rate below 5% means queries are too diverse to benefit from caching and the cache is just consuming memory. Pre-warm high-frequency FAQ and classification prompts at startup with `CacheWarmingScheduler` to eliminate cold-start latency for the first users of each deployment.
