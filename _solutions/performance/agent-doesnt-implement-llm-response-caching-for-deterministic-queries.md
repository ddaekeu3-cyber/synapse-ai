---
title: "Agent Doesn't Implement LLM Response Caching for Deterministic Queries"
description: "Agents that call the LLM for every request — including repeated identical queries from different users, FAQ lookups, fixed classification tasks, and templated summaries — pay full inference cost and latency for responses that are identical to those already generated. Implement LLM response caching that stores responses keyed by a normalized prompt hash, serves cached responses for deterministic queries, and invalidates stale entries when the underlying data or model changes."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-llm-response-caching-for-deterministic-queries
tags: [llm-caching, response-cache, prompt-hash, inference-cost, cache-invalidation, deterministic-queries]
symptoms:
  - "Identical user queries result in identical LLM calls every time — no cache layer exists"
  - "FAQ and classification queries are sent to the LLM thousands of times per day"
  - "Inference cost grows linearly with query volume even when responses are identical"
  - "No distinction between deterministic and non-deterministic queries for caching eligibility"
  - "Response latency is always full LLM round-trip even for frequently repeated prompts"
---

## Why This Happens

LLM calls are expensive and slow. When the same prompt is submitted with `temperature=0`, the response is deterministic — the model produces the same output every time. Without a cache, every occurrence of this prompt incurs full inference cost and latency. Caching is safe for deterministic queries: classification tasks, FAQ answers, structured extraction from fixed templates, and summarization of unchanged documents. It is unsafe for queries that depend on current time, user-specific data, or random outputs. The implementation challenge is normalizing prompts to a canonical form before hashing (removing irrelevant whitespace, sorting message lists) and implementing a staleness policy that invalidates the cache when the system prompt, model version, or source data changes.

## Solution 1: Cache Key Builder

```python
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class LLMCacheKeyComponents:
    model: str
    messages: List[Dict[str, Any]]
    temperature: float = 0.0
    max_tokens: Optional[int] = None
    system_prompt_hash: str = ""
    extra_tags: List[str] = field(default_factory=list)


class LLMCacheKeyBuilder:
    """
    Builds a normalized cache key from LLM request parameters.
    Only requests with temperature=0 (or below a configured threshold)
    are considered deterministic and eligible for caching.
    """

    def __init__(self, deterministic_temp_threshold: float = 0.01):
        self._threshold = deterministic_temp_threshold

    def is_cacheable(self, components: LLMCacheKeyComponents) -> bool:
        return components.temperature <= self._threshold

    def build(self, components: LLMCacheKeyComponents) -> Optional[str]:
        if not self.is_cacheable(components):
            return None

        normalized = {
            "model": components.model,
            "messages": self._normalize_messages(components.messages),
            "max_tokens": components.max_tokens,
            "system_prompt_hash": components.system_prompt_hash,
            "tags": sorted(components.extra_tags),
        }
        payload = json.dumps(normalized, sort_keys=True, ensure_ascii=True)
        return "llmcache-" + hashlib.sha256(payload.encode()).hexdigest()[:32]

    @staticmethod
    def _normalize_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, str]]:
        normalized = []
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                content = content.strip()
            normalized.append({
                "role": msg.get("role", "user"),
                "content": content,
            })
        return normalized
```

## Solution 2: LLM Response Cache

```python
import time
import threading
from collections import OrderedDict
from typing import Any, Optional


class LLMResponseCache:
    """
    LRU cache for LLM responses with per-entry TTL.
    Thread-safe for concurrent agent requests.
    """

    def __init__(
        self,
        max_entries: int = 5000,
        default_ttl_seconds: float = 3600.0,
    ):
        self._max = max_entries
        self._default_ttl = default_ttl_seconds
        self._cache: OrderedDict = OrderedDict()
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0
        self._evictions = 0

    def get(self, key: str) -> Optional[Any]:
        with self._lock:
            if key not in self._cache:
                self._misses += 1
                return None
            entry = self._cache[key]
            if time.time() > entry["expires_at"]:
                del self._cache[key]
                self._misses += 1
                return None
            self._cache.move_to_end(key)
            self._hits += 1
            return entry["response"]

    def put(
        self,
        key: str,
        response: Any,
        ttl_seconds: Optional[float] = None,
    ) -> None:
        ttl = ttl_seconds if ttl_seconds is not None else self._default_ttl
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
            self._cache[key] = {
                "response": response,
                "stored_at": time.time(),
                "expires_at": time.time() + ttl,
            }
            if len(self._cache) > self._max:
                self._cache.popitem(last=False)
                self._evictions += 1

    def invalidate(self, key: str) -> None:
        with self._lock:
            self._cache.pop(key, None)

    def invalidate_prefix(self, prefix: str) -> int:
        with self._lock:
            to_delete = [k for k in self._cache if k.startswith(prefix)]
            for k in to_delete:
                del self._cache[k]
            return len(to_delete)

    def stats(self) -> dict:
        total = self._hits + self._misses
        with self._lock:
            size = len(self._cache)
        return {
            "size": size,
            "max_entries": self._max,
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": round(self._hits / max(total, 1), 4),
            "evictions": self._evictions,
        }
```

## Solution 3: Cached LLM Caller

```python
import time
from typing import Any, Callable, Dict, List, Optional


class CachedLLMCaller:
    """
    Wraps LLM API calls with cache lookup and storage.
    Skips the cache for non-deterministic requests (temperature > threshold).
    """

    def __init__(
        self,
        cache: LLMResponseCache,
        key_builder: LLMCacheKeyBuilder,
    ):
        self._cache = cache
        self._key_builder = key_builder
        self._calls_total = 0
        self._calls_cached = 0
        self._tokens_saved = 0

    async def call(
        self,
        llm_fn: Callable,
        messages: List[Dict[str, Any]],
        model: str,
        temperature: float = 0.0,
        max_tokens: Optional[int] = None,
        system_prompt_hash: str = "",
        cache_ttl_seconds: Optional[float] = None,
    ) -> dict:
        self._calls_total += 1

        components = LLMCacheKeyComponents(
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            system_prompt_hash=system_prompt_hash,
        )

        cache_key = self._key_builder.build(components)

        if cache_key:
            cached = self._cache.get(cache_key)
            if cached is not None:
                self._calls_cached += 1
                estimated_tokens = cached.get("usage", {}).get("total_tokens", 0)
                self._tokens_saved += estimated_tokens
                return {**cached, "cache_hit": True, "cache_key": cache_key}

        start = time.time()
        response = await llm_fn(
            messages=messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        latency_ms = round((time.time() - start) * 1000, 2)

        if cache_key:
            self._cache.put(cache_key, response, cache_ttl_seconds)

        return {**response, "cache_hit": False, "cache_key": cache_key, "latency_ms": latency_ms}

    def stats(self) -> dict:
        return {
            "calls_total": self._calls_total,
            "calls_cached": self._calls_cached,
            "cache_hit_rate": round(self._calls_cached / max(self._calls_total, 1), 4),
            "tokens_saved_est": self._tokens_saved,
            **self._cache.stats(),
        }
```

## Solution 4: Cache Invalidation Manager

```python
import hashlib
import time
from typing import List


class CacheInvalidationManager:
    """
    Tracks system prompt versions and model versions.
    When either changes, invalidates all cache entries
    that depend on the old version.
    """

    def __init__(self, cache: LLMResponseCache):
        self._cache = cache
        self._current_system_hash = ""
        self._current_model = ""
        self._invalidations: List[dict] = []

    def update_system_prompt(self, new_prompt: str) -> int:
        new_hash = hashlib.sha256(new_prompt.encode()).hexdigest()[:16]
        if new_hash == self._current_system_hash:
            return 0
        old_hash = self._current_system_hash
        self._current_system_hash = new_hash
        # In a real implementation, keys would embed the system hash
        # Here we flush all entries as a safe default
        count = self._cache.invalidate_prefix("llmcache-")
        self._invalidations.append({
            "reason": "system_prompt_change",
            "old_hash": old_hash,
            "new_hash": new_hash,
            "entries_invalidated": count,
            "ts": time.time(),
        })
        return count

    def update_model(self, new_model: str) -> int:
        if new_model == self._current_model:
            return 0
        old_model = self._current_model
        self._current_model = new_model
        count = self._cache.invalidate_prefix("llmcache-")
        self._invalidations.append({
            "reason": "model_change",
            "old_model": old_model,
            "new_model": new_model,
            "entries_invalidated": count,
            "ts": time.time(),
        })
        return count

    def system_prompt_hash(self) -> str:
        return self._current_system_hash

    def invalidation_history(self) -> list:
        return list(self._invalidations)
```

## Solution 5: Cache Warming Scheduler

```python
import asyncio
import time
from typing import Any, Callable, Dict, List


class CacheWarmingScheduler:
    """
    Pre-populates the cache with responses for known high-frequency prompts.
    Run at startup or after cache invalidation to restore hit rates quickly.
    """

    def __init__(self, caller: CachedLLMCaller):
        self._caller = caller
        self._warmed = 0
        self._failed = 0

    async def warm(
        self,
        prompts: List[Dict[str, Any]],
        llm_fn: Callable,
        model: str,
    ) -> dict:
        start = time.time()
        for prompt_spec in prompts:
            try:
                await self._caller.call(
                    llm_fn=llm_fn,
                    messages=prompt_spec.get("messages", []),
                    model=model,
                    temperature=0.0,
                    cache_ttl_seconds=prompt_spec.get("ttl_seconds"),
                )
                self._warmed += 1
            except Exception:
                self._failed += 1

        return {
            "warmed": self._warmed,
            "failed": self._failed,
            "duration_seconds": round(time.time() - start, 2),
        }
```

## Solution 6: LLM Cache Dashboard

```python
import time


class LLMCacheDashboard:
    """
    Combines cache statistics, invalidation history, and cost savings
    into a single operational view.
    """

    def __init__(
        self,
        caller: CachedLLMCaller,
        invalidation_manager: CacheInvalidationManager,
    ):
        self._caller = caller
        self._invalidation = invalidation_manager

    def render(self) -> dict:
        stats = self._caller.stats()
        return {
            "generated_at": time.time(),
            "cache_stats": stats,
            "estimated_cost_savings_tokens": stats.get("tokens_saved_est", 0),
            "recent_invalidations": self._invalidation.invalidation_history()[-5:],
            "current_system_prompt_hash": self._invalidation.system_prompt_hash(),
        }
```

## Comparison

| Approach | Key Normalization | LRU Eviction | TTL Expiry | Invalidation | Cost Tracking |
|---|---|---|---|---|---|
| LLMCacheKeyBuilder | Yes (JSON normalize) | No | No | No | No |
| LLMResponseCache | No | Yes (OrderedDict) | Yes | Yes (prefix) | No |
| CachedLLMCaller | Via key builder | Via cache | Via cache | No | Yes (tokens) |
| CacheInvalidationManager | No | No | No | Yes (system/model) | No |
| CacheWarmingScheduler | No | No | No | No | No |
| LLMCacheDashboard | No | No | No | No | Yes (dashboard) |

**Best for production**: Gate caching strictly on `temperature <= 0.01` — even a small temperature value makes responses non-deterministic and caching incorrect. Use per-entry `ttl_seconds` for time-sensitive content (e.g., 300 seconds for summaries of frequently updated documents) and longer TTLs (24 hours) for static FAQ responses. Call `CacheInvalidationManager.update_system_prompt()` on every deployment that changes the system prompt — a stale cache serving responses generated with the old prompt is a subtle correctness bug that is difficult to diagnose without explicit invalidation tracking.
