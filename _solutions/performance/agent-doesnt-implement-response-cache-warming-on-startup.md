---
title: "Agent Doesn't Implement Response Cache Warming on Startup"
description: "Agents that start with a cold cache absorb full latency on every request during the warm-up period: the first hundreds of requests after deployment are significantly slower than steady-state because no cached responses exist. Implement cache warming on startup that pre-populates frequently-requested responses from historical access logs or a pre-defined seed set before the agent begins serving live traffic."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-response-cache-warming-on-startup
tags: [cache-warming, startup-performance, cold-cache, precomputation, response-cache, deployment-latency]
symptoms:
  - "P99 latency spikes after every deployment, recovering only after several minutes of traffic"
  - "First requests after restart are 5–10× slower than steady-state"
  - "Cache hit rate is 0% at startup and climbs slowly as traffic arrives"
  - "No mechanism to pre-populate cache with known high-frequency queries"
  - "Blue-green deployments show latency degradation on the green instance before traffic shifts"
---

## Why This Happens

Caches are populated by serving requests. Until a request has been served once, its result is not cached. On deployment, rolling restart, or scale-out, every new instance starts cold. If cache hit rate is 60% at steady state, the first 40% of capacity is consumed by cache misses before the cache warms up — and during high-traffic deployments, this cold period causes measurable latency spikes. Cache warming pre-runs a representative set of requests before the instance begins accepting live traffic, pre-populating the cache so the first real request benefits from a warm cache.

## Solution 1: Cache Warm-Up Seed

```python
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class WarmUpSeed:
    """A single cache warm-up entry: the request key and optionally a pre-computed value."""
    cache_key: str
    tool_name: str
    args: Dict[str, Any]
    priority: int = 0              # higher = warm up first
    expected_ttl_seconds: int = 3600
    pre_computed_value: Optional[Any] = None  # skip the actual call if provided
    category: str = ""


@dataclass
class WarmUpSeedSet:
    seeds: List[WarmUpSeed] = field(default_factory=list)
    description: str = ""
    created_at: float = field(default_factory=time.time)

    def sorted_by_priority(self) -> List[WarmUpSeed]:
        return sorted(self.seeds, key=lambda s: s.priority, reverse=True)

    def by_category(self, category: str) -> List[WarmUpSeed]:
        return [s for s in self.seeds if s.category == category]
```

## Solution 2: Historical Access Log Analyzer

```python
import time
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple


class HistoricalAccessLogAnalyzer:
    """
    Analyzes tool call access logs to identify the most frequently
    requested (tool_name, args) combinations for use as warm-up seeds.
    """

    def __init__(self, top_k: int = 100):
        self._top_k = top_k
        self._access_log: List[Tuple[float, str, str]] = []
        # (timestamp, tool_name, cache_key)

    def record_access(self, tool_name: str, cache_key: str) -> None:
        self._access_log.append((time.time(), tool_name, cache_key))
        if len(self._access_log) > 100_000:
            self._access_log.pop(0)

    def top_keys(
        self,
        window_seconds: float = 86400.0,
        min_hits: int = 3,
    ) -> List[Tuple[str, str, int]]:
        """Returns list of (tool_name, cache_key, hit_count) sorted by frequency."""
        cutoff = time.time() - window_seconds
        recent = [(tn, ck) for ts, tn, ck in self._access_log if ts >= cutoff]
        counts = Counter(recent)
        return [
            (tn, ck, count)
            for (tn, ck), count in counts.most_common(self._top_k)
            if count >= min_hits
        ]

    def generate_seed_set(
        self,
        args_resolver: Dict[str, Dict],   # cache_key -> args
        window_seconds: float = 86400.0,
    ) -> WarmUpSeedSet:
        top = self.top_keys(window_seconds=window_seconds)
        seeds = []
        for rank, (tool_name, cache_key, hit_count) in enumerate(top):
            seeds.append(WarmUpSeed(
                cache_key=cache_key,
                tool_name=tool_name,
                args=args_resolver.get(cache_key, {}),
                priority=hit_count,
            ))
        return WarmUpSeedSet(seeds=seeds, description=f"top-{len(seeds)} from last {window_seconds:.0f}s")
```

## Solution 3: Cache Warmer

```python
import asyncio
import time
from typing import Any, Callable, Dict, Optional


class CacheWarmer:
    """
    Executes warm-up seeds by either injecting pre-computed values
    directly into the cache or running the actual tool call to populate it.
    Respects a concurrency limit and overall time budget.
    """

    def __init__(
        self,
        cache_store,                          # any object with .set(key, value, ttl)
        dispatch_fn: Callable[[str, dict], Any],  # (tool_name, args) -> result
        max_concurrency: int = 5,
        time_budget_seconds: float = 30.0,
    ):
        self._cache = cache_store
        self._dispatch = dispatch_fn
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._budget = time_budget_seconds
        self._warmed_count = 0
        self._skipped_count = 0
        self._error_count = 0

    async def _warm_one(self, seed: WarmUpSeed) -> dict:
        async with self._semaphore:
            start = time.time()
            try:
                if seed.pre_computed_value is not None:
                    self._cache.set(seed.cache_key, seed.pre_computed_value, seed.expected_ttl_seconds)
                    result = seed.pre_computed_value
                else:
                    result = await self._dispatch(seed.tool_name, seed.args)
                    self._cache.set(seed.cache_key, result, seed.expected_ttl_seconds)
                self._warmed_count += 1
                return {"key": seed.cache_key, "success": True, "latency_ms": round((time.time() - start) * 1000, 2)}
            except Exception as exc:
                self._error_count += 1
                return {"key": seed.cache_key, "success": False, "error": str(exc)}

    async def warm(self, seed_set: WarmUpSeedSet) -> dict:
        deadline = time.time() + self._budget
        seeds = seed_set.sorted_by_priority()
        tasks = []

        for seed in seeds:
            if time.time() >= deadline:
                self._skipped_count += len(seeds) - len(tasks)
                break
            tasks.append(self._warm_one(seed))

        results = await asyncio.gather(*tasks)
        return {
            "warmed": self._warmed_count,
            "errors": self._error_count,
            "skipped": self._skipped_count,
            "time_used_seconds": round(self._budget - max(0, deadline - time.time()), 2),
            "results": list(results),
        }

    def stats(self) -> dict:
        return {
            "warmed_count": self._warmed_count,
            "error_count": self._error_count,
            "skipped_count": self._skipped_count,
        }
```

## Solution 4: Startup Cache Warming Orchestrator

```python
import asyncio
import time
from typing import Callable, List, Optional


class StartupCacheWarmingOrchestrator:
    """
    Runs cache warming at agent startup before live traffic is accepted.
    Supports readiness gating: the agent signals ready only after warming completes.
    """

    def __init__(
        self,
        warmer: CacheWarmer,
        seed_sets: List[WarmUpSeedSet],
        readiness_callback: Optional[Callable] = None,
    ):
        self._warmer = warmer
        self._seed_sets = seed_sets
        self._ready_callback = readiness_callback
        self._warming_complete = False
        self._warm_start = None
        self._warm_end = None
        self._warm_results = []

    async def run(self) -> dict:
        self._warm_start = time.time()
        all_results = []

        for seed_set in self._seed_sets:
            result = await self._warmer.warm(seed_set)
            all_results.append({
                "seed_set": seed_set.description,
                "result": result,
            })

        self._warm_end = time.time()
        self._warming_complete = True
        self._warm_results = all_results

        if self._ready_callback:
            await self._ready_callback()

        return {
            "warming_duration_seconds": round(self._warm_end - self._warm_start, 2),
            "seed_sets_processed": len(all_results),
            "total_warmed": sum(r["result"]["warmed"] for r in all_results),
            "total_errors": sum(r["result"]["errors"] for r in all_results),
            "details": all_results,
        }

    def is_ready(self) -> bool:
        return self._warming_complete
```

## Solution 5: Cache Hit Rate Monitor

```python
import time
from threading import Lock
from typing import Optional


class CacheHitRateMonitor:
    """
    Tracks cache hit and miss rates over sliding windows.
    Used to validate that warming was effective and to detect cache thrashing.
    """

    def __init__(self, window_seconds: int = 300):
        self._window = window_seconds
        self._events = []   # (ts, hit: bool)
        self._lock = Lock()

    def record_hit(self) -> None:
        with self._lock:
            self._events.append((time.time(), True))

    def record_miss(self) -> None:
        with self._lock:
            self._events.append((time.time(), False))

    def hit_rate(self, sub_window_seconds: Optional[int] = None) -> float:
        now = time.time()
        cutoff = now - (sub_window_seconds or self._window)
        with self._lock:
            recent = [hit for ts, hit in self._events if ts >= cutoff]
        if not recent:
            return 0.0
        return sum(recent) / len(recent)

    def summary(self) -> dict:
        return {
            "hit_rate_1m": round(self.hit_rate(60), 4),
            "hit_rate_5m": round(self.hit_rate(300), 4),
            "hit_rate_1h": round(self.hit_rate(3600), 4),
        }
```

## Solution 6: Cache Warming Dashboard

```python
import time


class CacheWarmingDashboard:
    """
    Combines orchestrator state, warmer stats, and hit rate monitor
    into a startup and steady-state health view.
    """

    def __init__(
        self,
        orchestrator: StartupCacheWarmingOrchestrator,
        warmer: CacheWarmer,
        hit_monitor: CacheHitRateMonitor,
    ):
        self._orchestrator = orchestrator
        self._warmer = warmer
        self._monitor = hit_monitor

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "warming_complete": self._orchestrator.is_ready(),
            "warming_duration_seconds": (
                round(self._orchestrator._warm_end - self._orchestrator._warm_start, 2)
                if self._orchestrator._warm_end else None
            ),
            "warmer_stats": self._warmer.stats(),
            "cache_hit_rates": self._monitor.summary(),
        }
```

## Comparison

| Approach | Pre-computed Inject | Live Call Warming | Concurrency Control | Readiness Gate | Hit Rate Tracking |
|---|---|---|---|---|---|
| CacheWarmer | Yes (pre_computed_value) | Yes (dispatch_fn) | Yes (semaphore) | No | No |
| HistoricalAccessLogAnalyzer | No | No | No | No | No |
| StartupCacheWarmingOrchestrator | Via warmer | Via warmer | Via warmer | Yes | No |
| CacheHitRateMonitor | No | No | No | No | Yes |
| CacheWarmingDashboard | No | No | No | No | Yes (aggregated) |

**Best for production**: Export the warm-up seed set from your production access logs nightly and commit it to the repository so every deployment starts with a current warm-up seed. Use `pre_computed_value` for responses that are expensive to compute and known to be stable (configuration lookups, schema definitions) — injecting them directly is instant and requires no downstream calls. Set `time_budget_seconds=30` and gate readiness on warming completion so load balancers do not route traffic to an instance until its cache is warm. Monitor `hit_rate_1m` immediately post-deployment: if it does not reach 40%+ within the first minute, the seed set is stale and needs refreshing.
