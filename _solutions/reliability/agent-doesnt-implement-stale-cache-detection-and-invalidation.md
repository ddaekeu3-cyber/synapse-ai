---
title: "Agent Doesn't Implement Stale Cache Detection and Invalidation"
description: "Agents that cache tool results without detecting staleness serve outdated data indefinitely — a cached user record reflects a deleted account, a cached price is an hour old during a volatile market, a cached search result predates a critical news event. Implement stale cache detection using TTL, version tags, and dependency-based invalidation to ensure cached results remain trustworthy."
date: 2026-04-16
difficulty: intermediate
category: reliability
slug: agent-doesnt-implement-stale-cache-detection-and-invalidation
tags: [cache-invalidation, stale-cache, ttl, version-tagging, dependency-invalidation, cache-freshness]
symptoms:
  - "Agent answers with account details for a user who deleted their account yesterday"
  - "Cached API responses never expire — data from weeks ago is served as current"
  - "No mechanism to invalidate a cache entry when the underlying data changes"
  - "Cache hit rate is high but answer accuracy is low due to stale data"
  - "Cannot tell when a cached result was last verified against the source"
---

## Why This Happens

Caches are populated on first access and returned on subsequent accesses. Without a staleness policy, cache entries live forever. TTL-based expiry handles the common case (data that changes on a known schedule) but misses two scenarios: (1) data that changes unpredictably — a user's subscription status can change at any moment; (2) data whose staleness depends on other data — a cached recommendation depends on a user profile that has since changed. Robust cache invalidation requires TTL plus version tagging (invalidate when a version token changes) plus dependency tracking (invalidate B when A changes).

## Solution 1: Cache Entry with Staleness Metadata

```python
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, List, Optional, Set


class StalenessReason(str, Enum):
    FRESH = "fresh"
    TTL_EXPIRED = "ttl_expired"
    VERSION_MISMATCH = "version_mismatch"
    DEPENDENCY_INVALIDATED = "dependency_invalidated"
    MANUAL_INVALIDATION = "manual_invalidation"
    SOURCE_CHANGED = "source_changed"


@dataclass
class CacheEntry:
    key: str
    value: Any
    created_at: float = field(default_factory=time.time)
    last_verified_at: float = field(default_factory=time.time)
    ttl_seconds: float = 300.0
    version_tag: Optional[str] = None      # e.g., ETag, last-modified hash
    dependency_keys: Set[str] = field(default_factory=set)  # keys this entry depends on
    invalidated: bool = False
    invalidation_reason: Optional[StalenessReason] = None

    @property
    def age_seconds(self) -> float:
        return time.time() - self.created_at

    @property
    def is_ttl_expired(self) -> bool:
        return self.age_seconds > self.ttl_seconds

    @property
    def is_stale(self) -> bool:
        return self.invalidated or self.is_ttl_expired

    def staleness_reason(self) -> StalenessReason:
        if self.invalidated:
            return self.invalidation_reason or StalenessReason.MANUAL_INVALIDATION
        if self.is_ttl_expired:
            return StalenessReason.TTL_EXPIRED
        return StalenessReason.FRESH
```

## Solution 2: Cache Staleness Detector

```python
import hashlib
import json
from typing import Any, Optional


class CacheStalenessDetector:
    """
    Detects staleness beyond TTL: checks version tags against a source
    and evaluates whether dependency keys have been invalidated.
    """

    def check_version(
        self,
        entry: CacheEntry,
        current_version: Optional[str],
    ) -> bool:
        """Returns True if the version has changed (entry is stale)."""
        if entry.version_tag is None or current_version is None:
            return False
        return entry.version_tag != current_version

    def compute_version_tag(self, data: Any) -> str:
        """Compute a version tag from a data snapshot."""
        serialized = json.dumps(data, sort_keys=True, default=str)
        return hashlib.sha256(serialized.encode()).hexdigest()[:16]

    def is_stale_by_dependency(
        self,
        entry: CacheEntry,
        invalidated_keys: set,
    ) -> bool:
        """Returns True if any dependency key has been invalidated."""
        return bool(entry.dependency_keys & invalidated_keys)

    def assess(
        self,
        entry: CacheEntry,
        current_version: Optional[str] = None,
        invalidated_keys: Optional[set] = None,
    ) -> StalenessReason:
        if entry.invalidated:
            return entry.invalidation_reason or StalenessReason.MANUAL_INVALIDATION
        if entry.is_ttl_expired:
            return StalenessReason.TTL_EXPIRED
        if current_version and self.check_version(entry, current_version):
            return StalenessReason.VERSION_MISMATCH
        if invalidated_keys and self.is_stale_by_dependency(entry, invalidated_keys):
            return StalenessReason.DEPENDENCY_INVALIDATED
        return StalenessReason.FRESH
```

## Solution 3: Smart Cache Store

```python
import time
from threading import Lock
from typing import Any, Dict, List, Optional, Set


class SmartCacheStore:
    """
    Cache store with TTL, version tag checking, and dependency-based invalidation.
    Tracks which entries depend on which keys for cascade invalidation.
    """

    def __init__(self, detector: CacheStalenessDetector):
        self._entries: Dict[str, CacheEntry] = {}
        self._invalidated_keys: Set[str] = set()
        self._detector = detector
        self._lock = Lock()
        self._hits = 0
        self._misses = 0
        self._stale_evictions = 0

    def set(
        self,
        key: str,
        value: Any,
        ttl_seconds: float = 300.0,
        version_tag: Optional[str] = None,
        dependency_keys: Optional[Set[str]] = None,
    ) -> CacheEntry:
        entry = CacheEntry(
            key=key,
            value=value,
            ttl_seconds=ttl_seconds,
            version_tag=version_tag,
            dependency_keys=dependency_keys or set(),
        )
        with self._lock:
            self._entries[key] = entry
        return entry

    def get(
        self,
        key: str,
        current_version: Optional[str] = None,
    ) -> tuple:
        """Returns (value, staleness_reason). None value means cache miss."""
        with self._lock:
            entry = self._entries.get(key)
            invalidated = set(self._invalidated_keys)

        if entry is None:
            self._misses += 1
            return None, StalenessReason.TTL_EXPIRED

        reason = self._detector.assess(entry, current_version, invalidated)
        if reason != StalenessReason.FRESH:
            self._stale_evictions += 1
            with self._lock:
                self._entries.pop(key, None)
            return None, reason

        self._hits += 1
        entry.last_verified_at = time.time()
        return entry.value, StalenessReason.FRESH

    def invalidate(
        self,
        key: str,
        reason: StalenessReason = StalenessReason.MANUAL_INVALIDATION,
    ) -> int:
        """Invalidates a key and all entries that depend on it. Returns count."""
        count = 0
        with self._lock:
            self._invalidated_keys.add(key)
            if key in self._entries:
                self._entries[key].invalidated = True
                self._entries[key].invalidation_reason = reason
                count += 1

            # Cascade: invalidate all entries depending on this key
            for entry in list(self._entries.values()):
                if key in entry.dependency_keys and not entry.invalidated:
                    entry.invalidated = True
                    entry.invalidation_reason = StalenessReason.DEPENDENCY_INVALIDATED
                    count += 1
        return count

    def evict_expired(self) -> int:
        cutoff = time.time()
        with self._lock:
            expired = [
                k for k, e in self._entries.items()
                if e.is_ttl_expired or e.invalidated
            ]
            for k in expired:
                del self._entries[k]
        return len(expired)

    def stats(self) -> dict:
        total = self._hits + self._misses
        with self._lock:
            entry_count = len(self._entries)
        return {
            "entries": entry_count,
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": round(self._hits / total, 3) if total > 0 else 0.0,
            "stale_evictions": self._stale_evictions,
        }
```

## Solution 4: Version-Aware Tool Cache Wrapper

```python
import hashlib
import json
import time
from typing import Any, Callable, Dict, Optional, Set


class VersionAwareToolCacheWrapper:
    """
    Wraps tool calls with smart caching. Computes a version tag from the
    result and stores dependency keys so related cache entries can be
    cascade-invalidated when upstream data changes.
    """

    def __init__(
        self,
        cache: SmartCacheStore,
        detector: CacheStalenessDetector,
    ):
        self._cache = cache
        self._detector = detector

    def _cache_key(self, tool_name: str, args: dict) -> str:
        args_hash = hashlib.md5(
            json.dumps(args, sort_keys=True, default=str).encode()
        ).hexdigest()[:10]
        return f"{tool_name}:{args_hash}"

    async def call(
        self,
        tool_name: str,
        tool_fn: Callable,
        args: Dict[str, Any],
        ttl_seconds: float = 300.0,
        dependency_keys: Optional[Set[str]] = None,
    ) -> Any:
        key = self._cache_key(tool_name, args)

        cached_value, reason = self._cache.get(key)
        if cached_value is not None:
            return cached_value

        result = await tool_fn(**args)
        version_tag = self._detector.compute_version_tag(result)
        self._cache.set(
            key=key,
            value=result,
            ttl_seconds=ttl_seconds,
            version_tag=version_tag,
            dependency_keys=dependency_keys,
        )
        return result

    def invalidate_tool(self, tool_name: str, args: dict) -> int:
        key = self._cache_key(tool_name, args)
        return self._cache.invalidate(key, StalenessReason.MANUAL_INVALIDATION)
```

## Solution 5: Cache Staleness Monitor

```python
import time
from threading import Lock
from typing import List


class CacheStalenessMonitor:
    """
    Monitors staleness event rates and alerts when evictions
    due to version mismatches or dependency invalidations spike —
    which indicates upstream data is changing faster than expected.
    """

    def __init__(self):
        self._events: List[dict] = []
        self._lock = Lock()

    def record_eviction(self, key: str, reason: StalenessReason) -> None:
        with self._lock:
            self._events.append({
                "ts": time.time(),
                "key": key,
                "reason": reason.value,
            })

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        with self._lock:
            recent = [e for e in self._events if e["ts"] >= cutoff]

        reason_counts: dict = {}
        for e in recent:
            r = e["reason"]
            reason_counts[r] = reason_counts.get(r, 0) + 1

        return {
            "window_seconds": window_seconds,
            "total_evictions": len(recent),
            "by_reason": reason_counts,
            "unexpected_invalidations": reason_counts.get("version_mismatch", 0)
            + reason_counts.get("dependency_invalidated", 0),
        }
```

## Solution 6: Cache Health Dashboard

```python
import time


class SmartCacheHealthDashboard:
    """
    Combines cache statistics, staleness event rates, and eviction reasons
    into a single operational view.
    """

    def __init__(
        self,
        cache: SmartCacheStore,
        monitor: CacheStalenessMonitor,
    ):
        self._cache = cache
        self._monitor = monitor

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "cache_stats": self._cache.stats(),
            "staleness_summary_1h": self._monitor.summary(3600.0),
        }
```

## Comparison

| Approach | TTL Expiry | Version Tag Checks | Dependency Invalidation | Cascade Invalidation | Staleness Monitoring |
|---|---|---|---|---|---|
| CacheEntry | Yes | Yes (tag stored) | Yes (dep keys stored) | No | No |
| CacheStalenessDetector | Via entry | Yes (tag compare) | Yes (key intersection) | No | No |
| SmartCacheStore | Via entry | Via detector | Via detector | Yes | No |
| VersionAwareToolCacheWrapper | Via store | Via detector | Via store | Via store | No |
| CacheStalenessMonitor | No | No | No | No | Yes |
| SmartCacheHealthDashboard | No | No | No | No | Yes |

**Best for production**: Use short TTLs (60–300s) for user-specific data (account status, permissions) and longer TTLs (1–24h) for slowly changing reference data (product catalog, config). Register entity-level dependency keys (e.g., `user:{user_id}`) when caching results that depend on a user record — when the user record changes, call `cache.invalidate("user:{user_id}")` to cascade-invalidate all dependent entries in one operation. Monitor `unexpected_invalidations` in `CacheStalenessMonitor`: if version mismatches spike, your TTL is longer than the data's actual change frequency and should be reduced. Run `evict_expired()` on a background scheduler every 60 seconds to prevent memory growth from stale entries.
