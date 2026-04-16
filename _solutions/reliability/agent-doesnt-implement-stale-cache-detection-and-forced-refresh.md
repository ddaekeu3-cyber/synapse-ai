---
title: "Agent Doesn't Implement Stale Cache Detection and Forced Refresh"
description: "Agents that cache tool results or LLM responses with fixed TTLs serve stale data long after it becomes incorrect: a weather tool cached for one hour serves yesterday's forecast, a stock price cached for five minutes serves a pre-crash price, and a document cached indefinitely serves a version that has since been corrected. Implement stale cache detection that validates cached entries against freshness criteria before serving them and forces refresh when content may have changed."
date: 2026-04-16
difficulty: intermediate
category: reliability
slug: agent-doesnt-implement-stale-cache-detection-and-forced-refresh
tags: [cache-staleness, ttl, forced-refresh, cache-validation, freshness, conditional-fetch]
symptoms:
  - "Agent serves outdated tool results after the underlying data has changed"
  - "Fixed TTL cache evicts fresh content and retains stale content equally"
  - "No mechanism to force cache refresh when the user signals data may be stale"
  - "Real-time queries (weather, stock prices) served from hours-old cache entries"
  - "Cache hit rate is high but accuracy is low — staleness undetected"
---

## Why This Happens

TTL-based caches treat all entries as equally fresh until the TTL expires. This model fails for data whose freshness requirement varies by content type: a static reference document can be cached for days, a real-time sensor reading for seconds. Fixed TTLs either cache too aggressively (serving stale data) or too conservatively (cache miss rate negates the performance benefit). Stale cache detection adds a second dimension: beyond TTL, entries are validated against content-specific freshness signals — a version tag, an ETag, a last-modified timestamp from the source — and forcibly refreshed when those signals indicate the cached version is outdated.

## Solution 1: Cache Entry with Freshness Metadata

```python
import time
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class FreshnessMetadata:
    etag: Optional[str] = None
    last_modified: Optional[float] = None   # unix timestamp from source
    version_tag: Optional[str] = None
    source_url: str = ""
    max_age_seconds: float = 300.0
    must_revalidate: bool = False  # always check source before serving


@dataclass
class CacheEntry:
    key: str
    value: Any
    cached_at: float = field(default_factory=time.time)
    freshness: FreshnessMetadata = field(default_factory=FreshnessMetadata)
    hit_count: int = 0
    last_accessed: float = field(default_factory=time.time)

    def is_expired(self) -> bool:
        return time.time() - self.cached_at > self.freshness.max_age_seconds

    def age_seconds(self) -> float:
        return round(time.time() - self.cached_at, 2)

    def touch(self) -> None:
        self.hit_count += 1
        self.last_accessed = time.time()
```

## Solution 2: Freshness Validator

```python
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class FreshnessDecision(str, Enum):
    FRESH = "fresh"
    STALE = "stale"
    REVALIDATE = "revalidate"   # serve stale while refreshing in background


@dataclass
class FreshnessCheckResult:
    decision: FreshnessDecision
    reason: str
    age_seconds: float
    etag_match: Optional[bool] = None


class FreshnessValidator:
    """
    Determines whether a cached entry is fresh, stale, or needs revalidation.
    Combines TTL check with ETag/version tag comparison when available.
    """

    def __init__(self, stale_while_revalidate_seconds: float = 30.0):
        self._swr = stale_while_revalidate_seconds

    def check(
        self,
        entry: CacheEntry,
        source_etag: Optional[str] = None,
        source_version: Optional[str] = None,
    ) -> FreshnessCheckResult:
        age = entry.age_seconds()

        # must_revalidate: always check source
        if entry.freshness.must_revalidate:
            return FreshnessCheckResult(
                decision=FreshnessDecision.REVALIDATE,
                reason="must_revalidate flag set",
                age_seconds=age,
            )

        # ETag mismatch: stale
        if source_etag and entry.freshness.etag:
            etag_match = source_etag == entry.freshness.etag
            if not etag_match:
                return FreshnessCheckResult(
                    decision=FreshnessDecision.STALE,
                    reason=f"ETag mismatch: cached={entry.freshness.etag} source={source_etag}",
                    age_seconds=age,
                    etag_match=False,
                )

        # Version tag mismatch: stale
        if source_version and entry.freshness.version_tag:
            if source_version != entry.freshness.version_tag:
                return FreshnessCheckResult(
                    decision=FreshnessDecision.STALE,
                    reason=f"Version mismatch: cached={entry.freshness.version_tag} source={source_version}",
                    age_seconds=age,
                )

        # Within TTL: fresh
        if not entry.is_expired():
            return FreshnessCheckResult(
                decision=FreshnessDecision.FRESH,
                reason=f"Within TTL ({age:.1f}s < {entry.freshness.max_age_seconds}s)",
                age_seconds=age,
            )

        # Past TTL but within stale-while-revalidate window
        if age < entry.freshness.max_age_seconds + self._swr:
            return FreshnessCheckResult(
                decision=FreshnessDecision.REVALIDATE,
                reason=f"Past TTL but within stale-while-revalidate window ({age:.1f}s)",
                age_seconds=age,
            )

        return FreshnessCheckResult(
            decision=FreshnessDecision.STALE,
            reason=f"Expired: age {age:.1f}s > TTL {entry.freshness.max_age_seconds}s",
            age_seconds=age,
        )
```

## Solution 3: Stale-Aware Cache

```python
import asyncio
import time
from threading import Lock
from typing import Any, Callable, Dict, Optional


class StaleAwareCache:
    """
    Cache that validates freshness before serving entries.
    Supports stale-while-revalidate: serve the stale entry immediately
    while triggering an async background refresh.
    """

    def __init__(
        self,
        validator: FreshnessValidator,
        max_entries: int = 10000,
    ):
        self._validator = validator
        self._max = max_entries
        self._entries: Dict[str, CacheEntry] = {}
        self._lock = Lock()
        self._stale_hits = 0
        self._fresh_hits = 0
        self._misses = 0

    def put(self, key: str, value: Any, freshness: FreshnessMetadata) -> None:
        with self._lock:
            if len(self._entries) >= self._max:
                oldest = min(self._entries.values(), key=lambda e: e.last_accessed)
                del self._entries[oldest.key]
            self._entries[key] = CacheEntry(key=key, value=value, freshness=freshness)

    def get(
        self,
        key: str,
        source_etag: Optional[str] = None,
        source_version: Optional[str] = None,
    ) -> tuple[Optional[Any], FreshnessDecision]:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                self._misses += 1
                return None, FreshnessDecision.STALE

            result = self._validator.check(entry, source_etag, source_version)

            if result.decision == FreshnessDecision.STALE:
                del self._entries[key]
                self._stale_hits += 1
                return None, FreshnessDecision.STALE

            if result.decision == FreshnessDecision.REVALIDATE:
                entry.touch()
                self._stale_hits += 1
                return entry.value, FreshnessDecision.REVALIDATE

            entry.touch()
            self._fresh_hits += 1
            return entry.value, FreshnessDecision.FRESH

    def invalidate(self, key: str) -> bool:
        with self._lock:
            if key in self._entries:
                del self._entries[key]
                return True
            return False

    def stats(self) -> dict:
        with self._lock:
            total = self._fresh_hits + self._stale_hits + self._misses
            return {
                "total_requests": total,
                "fresh_hits": self._fresh_hits,
                "stale_hits": self._stale_hits,
                "misses": self._misses,
                "fresh_hit_rate_pct": round(
                    self._fresh_hits / max(total, 1) * 100, 2
                ),
                "entry_count": len(self._entries),
            }
```

## Solution 4: Conditional Fetch Client

```python
import asyncio
from typing import Any, Callable, Optional


class ConditionalFetchClient:
    """
    Wraps a tool or API call with conditional fetch support.
    Passes cached ETag/Last-Modified to the source on revalidation;
    if the source returns 304 Not Modified, the cache entry is refreshed
    in place without re-transferring the full payload.
    """

    def __init__(
        self,
        fetch_fn: Callable,     # async fn(url, headers) -> (status, body, etag)
        cache: StaleAwareCache,
    ):
        self._fetch = fetch_fn
        self._cache = cache

    async def get(self, cache_key: str, url: str, max_age: float = 300.0) -> Any:
        cached_value, decision = self._cache.get(cache_key)

        if decision == FreshnessDecision.FRESH and cached_value is not None:
            return cached_value

        # Need to fetch — send conditional headers if we have an ETag
        with self._cache._lock:
            existing = self._cache._entries.get(cache_key)
            stored_etag = existing.freshness.etag if existing else None

        headers = {}
        if stored_etag:
            headers["If-None-Match"] = stored_etag

        status, body, new_etag = await self._fetch(url, headers)

        if status == 304 and cached_value is not None:
            # Not modified — refresh TTL only
            with self._cache._lock:
                if cache_key in self._cache._entries:
                    self._cache._entries[cache_key].cached_at = __import__("time").time()
            return cached_value

        freshness = FreshnessMetadata(
            etag=new_etag,
            source_url=url,
            max_age_seconds=max_age,
        )
        self._cache.put(cache_key, body, freshness)
        return body
```

## Solution 5: Forced Refresh Trigger

```python
import time
from typing import Callable, List, Optional


class ForcedRefreshTrigger:
    """
    Allows operators or user signals to force cache invalidation
    for specific keys or patterns, bypassing TTL.
    Records forced refreshes for audit.
    """

    def __init__(self, cache: StaleAwareCache):
        self._cache = cache
        self._refreshes: List[dict] = []

    def force_refresh(self, cache_key: str, reason: str = "") -> bool:
        evicted = self._cache.invalidate(cache_key)
        self._refreshes.append({
            "ts": time.time(),
            "key": cache_key,
            "reason": reason,
            "evicted": evicted,
        })
        return evicted

    def force_refresh_pattern(self, prefix: str, reason: str = "") -> int:
        with self._cache._lock:
            matching = [k for k in list(self._cache._entries.keys()) if k.startswith(prefix)]
        count = sum(1 for k in matching if self._cache.invalidate(k))
        self._refreshes.append({
            "ts": time.time(),
            "key": f"{prefix}*",
            "reason": reason,
            "evicted": count,
        })
        return count

    def recent_refreshes(self, limit: int = 20) -> List[dict]:
        return self._refreshes[-limit:]
```

## Solution 6: Stale Cache Dashboard

```python
import time


class StaleCacheDashboard:
    """
    Combines freshness statistics, stale hit rates, and forced
    refresh history into a single cache health view.
    """

    def __init__(
        self,
        cache: StaleAwareCache,
        refresh_trigger: ForcedRefreshTrigger,
    ):
        self._cache = cache
        self._trigger = refresh_trigger

    def render(self) -> dict:
        stats = self._cache.stats()
        stale_rate = round(
            stats["stale_hits"] / max(stats["total_requests"], 1) * 100, 2
        )
        return {
            "generated_at": time.time(),
            "cache_stats": stats,
            "stale_hit_rate_pct": stale_rate,
            "forced_refreshes_recent": self._trigger.recent_refreshes(10),
        }
```

## Comparison

| Approach | TTL Expiry | ETag Validation | Stale-While-Revalidate | Forced Invalidation | Dashboard |
|---|---|---|---|---|---|
| FreshnessValidator | Yes | Yes | Yes | No | No |
| StaleAwareCache | Via validator | Via validator | Via validator | Yes (invalidate) | No |
| ConditionalFetchClient | Via cache | Yes (If-None-Match) | Via cache | No | No |
| ForcedRefreshTrigger | No | No | No | Yes (key + pattern) | No |
| StaleCacheDashboard | No | No | No | No | Yes |

**Best for production**: Assign `max_age_seconds` based on data volatility — real-time APIs (weather, prices) should use 30-60 seconds, reference documents 3,600-86,400 seconds. Enable `must_revalidate=True` for any tool result that could cause incorrect action if served stale (e.g., account balance, inventory count, permission checks). Monitor `stale_hit_rate_pct` in `StaleCacheDashboard`: above 20% indicates TTLs are too short relative to request frequency (cache entries expire before they are reused) rather than a staleness problem — increase TTLs. Wire `ForcedRefreshTrigger.force_refresh_pattern()` to incident response runbooks: when a data source is known to have been corrected after an error, operators can invalidate the affected cache prefix immediately rather than waiting for TTLs to expire.
