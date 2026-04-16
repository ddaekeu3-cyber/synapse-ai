---
title: "Agent Doesn't Implement Stale Tool Result Detection and Refresh"
description: "Agents that cache tool results without freshness tracking continue using stale data — a stock price fetched 2 hours ago, a user record cached before a permission change, a weather reading from this morning — potentially producing incorrect or dangerous outputs. Implement stale tool result detection that tracks result age, marks results as stale when they exceed domain-specific TTLs, and triggers a transparent refresh before the stale data reaches the LLM."
date: 2026-04-16
difficulty: intermediate
category: reliability
slug: agent-doesnt-implement-stale-tool-result-detection-and-refresh
tags: [stale-data, cache-invalidation, ttl, tool-result-freshness, data-refresh, cache-staleness]
symptoms:
  - "Agent answers a question about stock price using a value fetched 3 hours ago"
  - "User permission changes are not reflected because the role was cached at session start"
  - "No TTL configured per tool type — all results are cached indefinitely or never cached"
  - "LLM context contains results from multiple timestamps with no staleness indication"
  - "Stale results produce incorrect agent decisions that could have been avoided by a re-fetch"
---

## Why This Happens

Tool result caching is beneficial for reducing latency and API calls, but only when the cache respects data freshness requirements. Different data has radically different TTLs: a user's name may be valid for days, their account balance for seconds, a weather reading for minutes. Without per-domain TTL configuration and active staleness checking, the agent silently uses outdated data. Stale detection requires recording the fetch timestamp alongside each cached result and checking it against the domain TTL before serving the cached value.

## Solution 1: Cached Tool Result with Freshness

```python
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class FreshnessStatus(str, Enum):
    FRESH = "fresh"
    STALE = "stale"
    EXPIRED = "expired"       # past hard expiry — must not be used


@dataclass
class FreshToolResult:
    tool_name: str
    result: Any
    fetched_at: float = field(default_factory=time.time)
    ttl_seconds: float = 300.0          # soft TTL — stale but usable with warning
    hard_expiry_seconds: float = 3600.0  # hard TTL — result must not be used
    metadata: Dict[str, Any] = field(default_factory=dict)

    def age_seconds(self) -> float:
        return time.time() - self.fetched_at

    def freshness(self) -> FreshnessStatus:
        age = self.age_seconds()
        if age > self.hard_expiry_seconds:
            return FreshnessStatus.EXPIRED
        if age > self.ttl_seconds:
            return FreshnessStatus.STALE
        return FreshnessStatus.FRESH

    def is_usable(self) -> bool:
        return self.freshness() != FreshnessStatus.EXPIRED
```

## Solution 2: Domain TTL Registry

```python
from dataclasses import dataclass
from typing import Dict, Optional


@dataclass
class DomainFreshnessPolicy:
    tool_name: str
    ttl_seconds: float
    hard_expiry_seconds: float
    refresh_on_stale: bool = True     # automatically refresh when stale
    description: str = ""


DEFAULT_TTL_POLICIES: Dict[str, DomainFreshnessPolicy] = {
    "get_stock_price": DomainFreshnessPolicy(
        tool_name="get_stock_price",
        ttl_seconds=30,
        hard_expiry_seconds=300,
        description="Market prices change by the second",
    ),
    "get_user_profile": DomainFreshnessPolicy(
        tool_name="get_user_profile",
        ttl_seconds=300,
        hard_expiry_seconds=3600,
        description="Profile updates are infrequent but permissions can change",
    ),
    "get_weather": DomainFreshnessPolicy(
        tool_name="get_weather",
        ttl_seconds=600,
        hard_expiry_seconds=7200,
        description="Weather changes slowly",
    ),
    "search_knowledge_base": DomainFreshnessPolicy(
        tool_name="search_knowledge_base",
        ttl_seconds=3600,
        hard_expiry_seconds=86400,
        description="Knowledge base is relatively static",
    ),
}


class DomainTTLRegistry:
    def __init__(self, policies: Dict[str, DomainFreshnessPolicy]):
        self._policies = policies

    def get_policy(self, tool_name: str) -> Optional[DomainFreshnessPolicy]:
        return self._policies.get(tool_name)

    def ttl_for(self, tool_name: str) -> float:
        policy = self._policies.get(tool_name)
        return policy.ttl_seconds if policy else 300.0

    def hard_expiry_for(self, tool_name: str) -> float:
        policy = self._policies.get(tool_name)
        return policy.hard_expiry_seconds if policy else 3600.0

    def should_auto_refresh(self, tool_name: str) -> bool:
        policy = self._policies.get(tool_name)
        return policy.refresh_on_stale if policy else False
```

## Solution 3: Freshness-Aware Tool Result Cache

```python
import time
from threading import Lock
from typing import Any, Dict, Optional


class FreshnessAwareToolResultCache:
    """
    Caches tool results with per-tool TTL enforcement.
    Returns None for expired results and flags stale results for refresh.
    """

    def __init__(self, ttl_registry: DomainTTLRegistry, max_entries: int = 1000):
        self._registry = ttl_registry
        self._max = max_entries
        self._cache: Dict[str, FreshToolResult] = {}
        self._lock = Lock()
        self._hits = 0
        self._stale_hits = 0
        self._misses = 0
        self._expired_evictions = 0

    def put(self, key: str, tool_name: str, result: Any) -> None:
        ttl = self._registry.ttl_for(tool_name)
        hard = self._registry.hard_expiry_for(tool_name)
        with self._lock:
            if len(self._cache) >= self._max:
                # Evict oldest entry
                oldest = min(self._cache.items(), key=lambda x: x[1].fetched_at)
                del self._cache[oldest[0]]
            self._cache[key] = FreshToolResult(
                tool_name=tool_name,
                result=result,
                ttl_seconds=ttl,
                hard_expiry_seconds=hard,
            )

    def get(self, key: str) -> Optional[FreshToolResult]:
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                self._misses += 1
                return None
            freshness = entry.freshness()
            if freshness == FreshnessStatus.EXPIRED:
                del self._cache[key]
                self._expired_evictions += 1
                self._misses += 1
                return None
            if freshness == FreshnessStatus.STALE:
                self._stale_hits += 1
            else:
                self._hits += 1
            return entry

    def stats(self) -> dict:
        total = self._hits + self._stale_hits + self._misses
        return {
            "entries": len(self._cache),
            "hits": self._hits,
            "stale_hits": self._stale_hits,
            "misses": self._misses,
            "expired_evictions": self._expired_evictions,
            "hit_rate": round((self._hits + self._stale_hits) / max(total, 1), 4),
            "fresh_hit_rate": round(self._hits / max(total, 1), 4),
        }
```

## Solution 4: Stale-Aware Tool Call Dispatcher

```python
import time
from typing import Any, Callable


class StaleAwareToolCallDispatcher:
    """
    Checks cache before every tool call. Returns cached result if fresh.
    On stale hit: if auto_refresh is enabled, fetches fresh data;
    otherwise returns stale result with a staleness annotation.
    """

    def __init__(
        self,
        cache: FreshnessAwareToolResultCache,
        ttl_registry: DomainTTLRegistry,
    ):
        self._cache = cache
        self._registry = ttl_registry

    async def dispatch(
        self,
        tool_name: str,
        cache_key: str,
        fn: Callable,
        **kwargs: Any,
    ) -> dict:
        cached = self._cache.get(cache_key)

        if cached is not None and cached.freshness() == FreshnessStatus.FRESH:
            return {"result": cached.result, "source": "cache_fresh", "age_seconds": cached.age_seconds()}

        if cached is not None and cached.freshness() == FreshnessStatus.STALE:
            if self._registry.should_auto_refresh(tool_name):
                result = await fn(**kwargs)
                self._cache.put(cache_key, tool_name, result)
                return {"result": result, "source": "refreshed", "age_seconds": 0.0}
            else:
                return {
                    "result": cached.result,
                    "source": "cache_stale",
                    "age_seconds": cached.age_seconds(),
                    "warning": f"result is {cached.age_seconds():.0f}s old (TTL: {cached.ttl_seconds}s)",
                }

        # Cache miss or expired — fetch fresh
        result = await fn(**kwargs)
        self._cache.put(cache_key, tool_name, result)
        return {"result": result, "source": "fresh_fetch", "age_seconds": 0.0}
```

## Solution 5: Staleness Audit Reporter

```python
import time
from typing import List


class StalenessAuditReporter:
    """
    Scans all cached entries and reports which are stale or near-expiry.
    Use at turn start to proactively refresh critical stale data.
    """

    def __init__(self, cache: FreshnessAwareToolResultCache):
        self._cache = cache

    def scan(self) -> dict:
        with self._cache._lock:
            entries = list(self._cache._cache.values())

        fresh = [e for e in entries if e.freshness() == FreshnessStatus.FRESH]
        stale = [e for e in entries if e.freshness() == FreshnessStatus.STALE]
        expired = [e for e in entries if e.freshness() == FreshnessStatus.EXPIRED]

        stale_report = [
            {
                "tool_name": e.tool_name,
                "age_seconds": round(e.age_seconds(), 1),
                "ttl_seconds": e.ttl_seconds,
                "overage_seconds": round(e.age_seconds() - e.ttl_seconds, 1),
            }
            for e in stale
        ]

        return {
            "generated_at": time.time(),
            "total_entries": len(entries),
            "fresh": len(fresh),
            "stale": len(stale),
            "expired": len(expired),
            "stale_entries": stale_report,
        }
```

## Solution 6: Freshness Dashboard

```python
import time


class ToolResultFreshnessDashboard:
    """Combines cache stats, staleness audit, and freshness health into a single report."""

    def __init__(
        self,
        cache: FreshnessAwareToolResultCache,
        reporter: StalenessAuditReporter,
    ):
        self._cache = cache
        self._reporter = reporter

    def render(self) -> dict:
        scan = self._reporter.scan()
        stats = self._cache.stats()
        stale_rate = scan["stale"] / max(scan["total_entries"], 1)
        return {
            "generated_at": time.time(),
            "cache_stats": stats,
            "freshness_scan": scan,
            "stale_rate": round(stale_rate, 4),
            "health": (
                "healthy" if stale_rate < 0.10
                else "degraded" if stale_rate < 0.30
                else "critical"
            ),
        }
```

## Comparison

| Approach | Per-Domain TTL | Soft/Hard Expiry | Auto-Refresh | Staleness Audit | Dashboard |
|---|---|---|---|---|---|
| FreshToolResult | Yes (per entry) | Yes (both) | No | No | No |
| DomainTTLRegistry | Yes (by tool) | Yes | Yes (policy) | No | No |
| FreshnessAwareToolResultCache | Via registry | Via entries | No | No | No |
| StaleAwareToolCallDispatcher | Via registry | Via cache | Yes (conditional) | No | No |
| StalenessAuditReporter | No | No | No | Yes | No |

**Best for production**: Configure TTLs per tool in `DomainTTLRegistry` at startup — never use a single global TTL for all tools. Set `refresh_on_stale=True` only for tools where freshness is safety-critical (account balance, permissions, inventory count); for informational tools (weather, news), a stale annotation is sufficient. Run `StalenessAuditReporter.scan()` at the start of each turn and proactively refresh stale entries for tools that will likely be needed — this amortizes refresh latency across turns rather than causing a blocking refresh mid-generation. Monitor `stale_hit_rate` from cache stats: if it exceeds 20%, TTLs are too long for the actual data change rate and should be halved.
