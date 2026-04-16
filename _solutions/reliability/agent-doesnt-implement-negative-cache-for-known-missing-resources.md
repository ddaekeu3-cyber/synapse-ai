---
title: "Agent Doesn't Implement Negative Cache for Known Missing Resources"
description: "AI agents that repeatedly query a database, API, or vector store for resources that don't exist pay the full lookup cost on every retry—tool calls that return empty results still consume latency budget, API rate limit quota, and database connection time. A negative cache stores the absence of a resource for a short TTL, returning an immediate empty result for subsequent identical queries without hitting the backend."
date: 2025-02-22
difficulty: intermediate
category: reliability
slug: agent-doesnt-implement-negative-cache-for-known-missing-resources
tags:
  - negative-cache
  - caching
  - missing-resource
  - rate-limit
  - reliability
  - nxdomain
  - empty-result
symptoms:
  - "Agent retries a database lookup 5 times per conversation for a user that clearly doesn't exist"
  - "Rate limit quota consumed by repeated queries for the same non-existent document"
  - "Tool call latency unchanged after caching because 60% of queries return empty results"
  - "Vector store queried repeatedly for embeddings that were deleted in the last maintenance window"
  - "API calls to a webhook endpoint that has been returning 404 for hours still consuming quota"
---

## Problem

Caches typically store positive results: "document X has content Y". But when a query returns no result—a user ID not found in the database, a document that was deleted, an API endpoint that returns 404—the next identical query will make the same full round-trip to the same backend and get the same empty result. For agents that run multiple turns on the same data, this can mean 10-50 redundant backend calls per session for non-existent resources. A negative cache stores `(key → "does not exist")` entries with a short TTL (30-300 seconds), returning an immediate empty result without touching the backend. This pattern is well-established in DNS (NXDOMAIN caching) and HTTP (404 response caching).

---

## Solution 1: NegativeCache — TTL-Based Absence Cache

```python
import hashlib
import threading
import time
from typing import Any, Callable, Optional, Tuple


_SENTINEL = object()  # marks a cached negative entry


class NegativeCache:
    """
    Caches both positive results and known-missing (negative) entries.
    Negative entries expire after `negative_ttl` seconds; positive
    entries expire after `positive_ttl` seconds.

    Usage:
        cache = NegativeCache(positive_ttl=300, negative_ttl=60)

        result = cache.get("user:123")
        if result is NegativeCache.MISS:
            result = db.fetch_user(123)
            if result is None:
                cache.set_negative("user:123")
            else:
                cache.set_positive("user:123", result)

        if cache.is_negative("user:123"):
            return None   # skip backend call
    """

    MISS = _SENTINEL  # returned when key is not in cache at all
    NEGATIVE = "NEGATIVE"  # returned when key is cached as absent

    def __init__(
        self,
        positive_ttl: float = 300.0,
        negative_ttl: float = 60.0,
        max_size: int = 10_000,
    ):
        self._pos_ttl = positive_ttl
        self._neg_ttl = negative_ttl
        self._max = max_size
        self._store: dict = {}  # key -> (value_or_NEGATIVE, expiry)
        self._lock = threading.Lock()

    def get(self, key: str) -> Any:
        """
        Returns:
            NegativeCache.MISS      — not in cache at all
            NegativeCache.NEGATIVE  — cached as absent
            Any value               — cached positive result
        """
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return self.MISS
            value, expiry = entry
            if time.monotonic() > expiry:
                del self._store[key]
                return self.MISS
            return self.NEGATIVE if value is _SENTINEL else value

    def set_positive(self, key: str, value: Any):
        with self._lock:
            self._evict_if_full()
            self._store[key] = (value, time.monotonic() + self._pos_ttl)

    def set_negative(self, key: str):
        with self._lock:
            self._evict_if_full()
            self._store[key] = (_SENTINEL, time.monotonic() + self._neg_ttl)

    def is_negative(self, key: str) -> bool:
        return self.get(key) is self.NEGATIVE

    def invalidate(self, key: str):
        with self._lock:
            self._store.pop(key, None)

    def _evict_if_full(self):
        if len(self._store) >= self._max:
            now = time.monotonic()
            expired = [k for k, (_, exp) in self._store.items() if exp <= now]
            for k in expired:
                del self._store[k]
            # If still full, evict oldest 10%
            if len(self._store) >= self._max:
                oldest = sorted(self._store.items(), key=lambda x: x[1][1])
                for k, _ in oldest[:self._max // 10]:
                    del self._store[k]

    @property
    def stats(self) -> dict:
        with self._lock:
            now = time.monotonic()
            positive = sum(1 for v, exp in self._store.values()
                            if v is not _SENTINEL and exp > now)
            negative = sum(1 for v, exp in self._store.values()
                            if v is _SENTINEL and exp > now)
            return {"positive": positive, "negative": negative, "total": len(self._store)}
```

---

## Solution 2: NegativeCacheDecorator — Wrap Any Lookup Function

```python
import functools
import logging
from typing import Any, Callable, Optional, Tuple

logger = logging.getLogger(__name__)


def with_negative_cache(
    cache: NegativeCache,
    key_fn: Optional[Callable] = None,
    empty_values: Tuple = (None, [], {}, ""),
):
    """
    Decorator that wraps a lookup function with negative caching.
    Returns None immediately for known-missing keys without calling
    the wrapped function. Records None/empty returns as negative entries.

    Usage:
        cache = NegativeCache(negative_ttl=120)

        @with_negative_cache(cache, key_fn=lambda uid: f"user:{uid}")
        async def fetch_user(user_id: str) -> Optional[dict]:
            return await db.get_user(user_id)
    """
    def decorator(fn: Callable):
        @functools.wraps(fn)
        async def async_wrapper(*args, **kwargs):
            import asyncio
            key = key_fn(*args, **kwargs) if key_fn else str(args) + str(kwargs)
            cached = cache.get(key)
            if cached is NegativeCache.NEGATIVE:
                logger.debug("negative_cache_hit key=%s", key)
                return None
            if cached is not NegativeCache.MISS:
                logger.debug("positive_cache_hit key=%s", key)
                return cached

            result = await fn(*args, **kwargs)
            if result in empty_values or result == empty_values[0]:
                cache.set_negative(key)
                logger.debug("negative_cache_store key=%s", key)
            else:
                cache.set_positive(key, result)
            return result

        @functools.wraps(fn)
        def sync_wrapper(*args, **kwargs):
            key = key_fn(*args, **kwargs) if key_fn else str(args) + str(kwargs)
            cached = cache.get(key)
            if cached is NegativeCache.NEGATIVE:
                return None
            if cached is not NegativeCache.MISS:
                return cached
            result = fn(*args, **kwargs)
            if result in empty_values or result == empty_values[0]:
                cache.set_negative(key)
            else:
                cache.set_positive(key, result)
            return result

        import asyncio
        return async_wrapper if asyncio.iscoroutinefunction(fn) else sync_wrapper
    return decorator
```

---

## Solution 3: HTTP404NegativeCache — Cache HTTP Not-Found Responses

```python
import logging
import time
from typing import Dict, Optional, Tuple
from urllib.parse import urlparse

import aiohttp

logger = logging.getLogger(__name__)


class HTTP404NegativeCache:
    """
    Wraps an aiohttp.ClientSession and caches 404 and 410 responses
    to prevent redundant HTTP calls for resources that no longer exist.
    Also caches 429 responses with Retry-After header to enforce backoff.

    Usage:
        neg_cache = HTTP404NegativeCache(ttl_404=300, ttl_429=60)
        async with neg_cache.session() as session:
            resp = await neg_cache.get(session, "https://api.example.com/docs/123")
            if resp is None:
                return {"error": "resource not found (cached)"}
    """

    CACHED_NOT_FOUND = "CACHED_404"
    CACHED_RATE_LIMITED = "CACHED_429"

    def __init__(self, ttl_404: float = 300.0, ttl_410: float = 3600.0,
                  ttl_429: float = 60.0):
        self._ttls = {404: ttl_404, 410: ttl_410, 429: ttl_429}
        self._cache: Dict[str, Tuple[str, float]] = {}  # url -> (reason, expiry)

    def _is_cached(self, url: str) -> Optional[str]:
        entry = self._cache.get(url)
        if entry and time.monotonic() < entry[1]:
            return entry[0]
        self._cache.pop(url, None)
        return None

    def _store(self, url: str, reason: str, ttl: float):
        self._cache[url] = (reason, time.monotonic() + ttl)
        logger.info("http_negative_cache_stored url=%s reason=%s ttl=%.0f", url, reason, ttl)

    async def get(
        self, session: aiohttp.ClientSession, url: str, **kwargs
    ) -> Optional[aiohttp.ClientResponse]:
        cached_reason = self._is_cached(url)
        if cached_reason:
            logger.debug("http_negative_cache_hit url=%s reason=%s", url, cached_reason)
            return None

        async with session.get(url, **kwargs) as resp:
            if resp.status in self._ttls:
                ttl = self._ttls[resp.status]
                if resp.status == 429:
                    retry_after = resp.headers.get("Retry-After")
                    if retry_after:
                        try:
                            ttl = float(retry_after)
                        except ValueError:
                            pass
                self._store(url, f"HTTP_{resp.status}", ttl)
                return None
            return resp

    def invalidate(self, url: str):
        self._cache.pop(url, None)

    @property
    def cached_count(self) -> int:
        now = time.monotonic()
        return sum(1 for _, exp in self._cache.values() if exp > now)
```

---

## Solution 4: VectorStoreNegativeCache — Cache Missing Embedding Lookups

```python
import hashlib
import logging
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class VectorStoreNegativeCache:
    """
    Wraps a vector store client to cache known-missing document IDs.
    When a document lookup (by ID or metadata filter) returns no results,
    the query fingerprint is stored as a negative entry. Subsequent
    identical queries skip the ANN search entirely.

    Particularly useful after bulk document deletes: the agent may
    repeatedly query for documents removed in the last maintenance window.

    Usage:
        neg_cache = VectorStoreNegativeCache(vector_store=chroma_client, ttl=180)
        docs = await neg_cache.get_by_id("doc-deleted-001")
        # Returns [] from cache on second call — no ANN query made
    """

    def __init__(self, vector_store: Any, ttl: float = 180.0):
        self._store = vector_store
        self._ttl = ttl
        self._negatives: Dict[str, float] = {}  # fingerprint -> expiry

    def _fingerprint(self, *args) -> str:
        h = hashlib.sha256("|".join(str(a) for a in args).encode()).hexdigest()
        return h[:16]

    def _is_negative(self, fp: str) -> bool:
        exp = self._negatives.get(fp)
        if exp and time.monotonic() < exp:
            return True
        self._negatives.pop(fp, None)
        return False

    def _mark_negative(self, fp: str):
        self._negatives[fp] = time.monotonic() + self._ttl

    async def get_by_id(self, doc_id: str) -> List[Any]:
        fp = self._fingerprint("id", doc_id)
        if self._is_negative(fp):
            logger.debug("vector_negative_cache_hit doc_id=%s", doc_id)
            return []
        results = await self._store.get(ids=[doc_id])
        if not results or not results.get("documents"):
            self._mark_negative(fp)
            logger.info("vector_negative_cache_stored doc_id=%s ttl=%.0f", doc_id, self._ttl)
            return []
        return results

    async def query(self, query_embedding: List[float],
                     filter_dict: Optional[Dict] = None, k: int = 5) -> List[Any]:
        fp = self._fingerprint("query", str(filter_dict), k)
        if self._is_negative(fp):
            logger.debug("vector_negative_cache_hit filter=%s", filter_dict)
            return []
        results = await self._store.query(
            query_embeddings=[query_embedding],
            where=filter_dict,
            n_results=k,
        )
        docs = (results or {}).get("documents", [[]])[0]
        if not docs:
            self._mark_negative(fp)
        return docs

    def invalidate_all(self):
        """Call after a document ingestion to clear stale negatives."""
        self._negatives.clear()
        logger.info("vector_negative_cache_invalidated")

    @property
    def negative_count(self) -> int:
        now = time.monotonic()
        return sum(1 for exp in self._negatives.values() if exp > now)
```

---

## Solution 5: NegativeCacheMetrics — Track Cache Hit Rates and Savings

```python
import logging
import time
from dataclasses import dataclass, field
from typing import Dict

logger = logging.getLogger(__name__)


@dataclass
class NegativeCacheMetrics:
    """
    Tracks negative cache performance: hit rate, backend calls saved,
    and estimated latency savings. Integrates with any NegativeCache instance
    by wrapping its get/set methods.

    Usage:
        metrics = NegativeCacheMetrics(avg_backend_latency_ms=45)
        metrics.record_hit()              # negative cache hit — backend skipped
        metrics.record_miss_positive()    # cache miss, backend returned data
        metrics.record_miss_negative()    # cache miss, backend returned empty
        print(metrics.summary())
    """

    avg_backend_latency_ms: float = 50.0
    _hits: int = field(default=0, repr=False)
    _miss_positive: int = field(default=0, repr=False)
    _miss_negative: int = field(default=0, repr=False)
    _start: float = field(default_factory=time.monotonic, repr=False)

    def record_hit(self):
        self._hits += 1

    def record_miss_positive(self):
        self._miss_positive += 1

    def record_miss_negative(self):
        self._miss_negative += 1

    @property
    def total_queries(self) -> int:
        return self._hits + self._miss_positive + self._miss_negative

    @property
    def hit_rate_pct(self) -> float:
        total = self.total_queries
        return round(self._hits / total * 100, 1) if total else 0.0

    @property
    def backend_calls_saved(self) -> int:
        return self._hits

    @property
    def estimated_latency_saved_ms(self) -> float:
        return round(self._hits * self.avg_backend_latency_ms, 1)

    def summary(self) -> Dict:
        elapsed = round(time.monotonic() - self._start, 1)
        return {
            "total_queries": self.total_queries,
            "negative_cache_hits": self._hits,
            "hit_rate_pct": self.hit_rate_pct,
            "backend_calls_saved": self.backend_calls_saved,
            "estimated_latency_saved_ms": self.estimated_latency_saved_ms,
            "miss_positive": self._miss_positive,
            "miss_negative": self._miss_negative,
            "uptime_seconds": elapsed,
        }
```

---

## Solution 6: TieredNegativeCache — Local + Shared Negative Cache

```python
import json
import logging
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

_NEG_MARKER = "__NEGATIVE__"


class TieredNegativeCache:
    """
    Two-tier negative cache: L1 is an in-process NegativeCache (fast, not shared),
    L2 is a Redis-backed shared cache (shared across all agent instances).
    A negative result in L2 prevents all instances from hitting the backend
    for a missing resource—useful when multiple agents query the same external API.

    Usage:
        cache = TieredNegativeCache(
            redis_client=redis.from_url("redis://localhost:6379"),
            l1_neg_ttl=30,
            l2_neg_ttl=300,
        )
        result = await cache.get("user:missing-001")
        if result is None:
            result = await backend.fetch()
            if result is None:
                await cache.set_negative("user:missing-001")
            else:
                await cache.set_positive("user:missing-001", result)
    """

    def __init__(self, redis_client: Any,
                  l1_neg_ttl: float = 30.0,
                  l2_neg_ttl: float = 300.0,
                  l1_pos_ttl: float = 60.0):
        self._l1 = NegativeCache(positive_ttl=l1_pos_ttl, negative_ttl=l1_neg_ttl)
        self._redis = redis_client
        self._l2_neg_ttl = int(l2_neg_ttl)

    async def get(self, key: str) -> Any:
        # L1 check
        l1_result = self._l1.get(key)
        if l1_result is NegativeCache.NEGATIVE:
            return None
        if l1_result is not NegativeCache.MISS:
            return l1_result

        # L2 check
        try:
            raw = await self._redis.get(f"neg:{key}")
            if raw:
                data = json.loads(raw)
                if data == _NEG_MARKER:
                    self._l1.set_negative(key)
                    logger.debug("l2_negative_cache_hit key=%s", key)
                    return None
                self._l1.set_positive(key, data)
                return data
        except Exception as exc:
            logger.warning("l2_cache_get_failed key=%s error=%s", key, exc)

        return NegativeCache.MISS  # caller must query backend

    async def set_negative(self, key: str):
        self._l1.set_negative(key)
        try:
            await self._redis.setex(f"neg:{key}", self._l2_neg_ttl,
                                     json.dumps(_NEG_MARKER))
            logger.info("negative_cache_stored l1+l2 key=%s ttl=%d", key, self._l2_neg_ttl)
        except Exception as exc:
            logger.warning("l2_cache_set_failed key=%s error=%s", key, exc)

    async def set_positive(self, key: str, value: Any):
        self._l1.set_positive(key, value)
        try:
            await self._redis.setex(f"neg:{key}", 60, json.dumps(value, default=str))
        except Exception as exc:
            logger.warning("l2_cache_set_positive_failed key=%s error=%s", key, exc)

    async def invalidate(self, key: str):
        self._l1.invalidate(key)
        try:
            await self._redis.delete(f"neg:{key}")
        except Exception as exc:
            logger.warning("l2_cache_invalidate_failed key=%s error=%s", key, exc)
```

---

## Comparison

| Approach | Caches Absence | Positive Caching | Shared Across Instances | HTTP Support | Vector Store | Metrics |
|---|---|---|---|---|---|---|
| **NegativeCache** | Yes | Yes | No | No | No | Via stats |
| **with_negative_cache** | Yes | Yes | No | No | No | No |
| **HTTP404NegativeCache** | Yes (404/410/429) | No | No | Yes | No | No |
| **VectorStoreNegativeCache** | Yes | No | No | No | Yes | No |
| **NegativeCacheMetrics** | N/A | N/A | N/A | N/A | N/A | Yes |
| **TieredNegativeCache** | Yes | Yes | Yes (Redis) | No | No | No |

**Key insight**: the immediate fix is adding `NegativeCache` to any tool that frequently returns empty results—typically database user lookups and document fetch-by-ID. Set `negative_ttl=60` to start: this prevents duplicate empty queries within a single agent session (typically 30-120 seconds) without risking stale negative entries if the resource is created between agent turns. For multi-agent deployments sharing the same backend, use `TieredNegativeCache` so that one agent's discovery of a missing resource protects all others. Track savings with `NegativeCacheMetrics`—a 30% hit rate on empty lookups typically reduces per-turn latency by 15-40ms in production.
