---
title: "Agent Doesn't Implement Stale-While-Revalidate Caching"
description: "Agent tool call results are either always fresh (slow) or cached until manual invalidation (stale); stale-while-revalidate returns cached data immediately and revalidates in the background, eliminating cache-miss latency spikes."
category: performance
difficulty: intermediate
tags: [caching, stale-while-revalidate, latency, background-refresh, asyncio, ttl, performance]
---

# Agent Doesn't Implement Stale-While-Revalidate Caching

## Problem

Agents that invalidate cache entries on expiry force the next caller to wait for a fresh fetch — causing a latency spike exactly when the cache is most needed (peak load, slow tool calls). The stale-while-revalidate (SWR) pattern returns stale data instantly and triggers a background refresh. The caller never waits for a slow tool call; the cache is refreshed behind the scenes. This is how CDNs serve content under load, and it applies equally well to agent tool call results.

## Solution 1: Basic Stale-While-Revalidate Cache

Return stale data if available; trigger background refresh if the entry is past its fresh TTL.

```python
import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

@dataclass
class CacheEntry:
    value: Any
    fetched_at: float
    fresh_ttl: float     # serve fresh for this many seconds
    stale_ttl: float     # serve stale for this many seconds after fresh_ttl
    refreshing: bool = False

    def is_fresh(self) -> bool:
        return (time.monotonic() - self.fetched_at) < self.fresh_ttl

    def is_stale(self) -> bool:
        age = time.monotonic() - self.fetched_at
        return self.fresh_ttl <= age < (self.fresh_ttl + self.stale_ttl)

    def is_expired(self) -> bool:
        age = time.monotonic() - self.fetched_at
        return age >= (self.fresh_ttl + self.stale_ttl)

class SWRCache:
    """
    Stale-While-Revalidate cache.
    - fresh_ttl: serve from cache, no fetch
    - fresh_ttl < age < fresh_ttl + stale_ttl: serve stale, fetch in background
    - age >= fresh_ttl + stale_ttl: block and fetch fresh
    """

    def __init__(self):
        self._store: dict[str, CacheEntry] = {}

    async def get(
        self,
        key: str,
        fetch_fn: Callable[[], Awaitable[Any]],
        fresh_ttl: float = 30.0,
        stale_ttl: float = 300.0,
    ) -> Any:
        entry = self._store.get(key)

        if entry is None or entry.is_expired():
            # Cache miss or fully expired: block and fetch
            value = await fetch_fn()
            self._store[key] = CacheEntry(
                value=value,
                fetched_at=time.monotonic(),
                fresh_ttl=fresh_ttl,
                stale_ttl=stale_ttl,
            )
            return value

        if entry.is_fresh():
            return entry.value  # fast path: serve from cache

        if entry.is_stale() and not entry.refreshing:
            # Serve stale immediately; refresh in background
            entry.refreshing = True
            asyncio.create_task(self._revalidate(key, fetch_fn, fresh_ttl, stale_ttl))
            return entry.value

        return entry.value  # refreshing already in progress

    async def _revalidate(
        self,
        key: str,
        fetch_fn: Callable[[], Awaitable[Any]],
        fresh_ttl: float,
        stale_ttl: float,
    ) -> None:
        try:
            value = await fetch_fn()
            self._store[key] = CacheEntry(
                value=value,
                fetched_at=time.monotonic(),
                fresh_ttl=fresh_ttl,
                stale_ttl=stale_ttl,
            )
        except Exception as exc:
            # Revalidation failed: keep stale entry, clear refreshing flag
            entry = self._store.get(key)
            if entry:
                entry.refreshing = False
            print(f"[swr] revalidation failed for {key!r}: {exc}")

    def invalidate(self, key: str) -> None:
        self._store.pop(key, None)

# Usage with agent tool calls
from anthropic import AsyncAnthropic

client = AsyncAnthropic()
cache = SWRCache()

async def fetch_weather(city: str) -> dict:
    """Simulates a slow external tool call."""
    await asyncio.sleep(0.5)  # network latency
    return {"city": city, "temp": 22, "condition": "sunny", "fetched_at": time.time()}

async def agent_weather_query(city: str) -> dict:
    # First call: blocks (cache miss)
    # Subsequent calls within fresh_ttl: instant
    # After fresh_ttl: instant (stale) + background refresh
    # After stale_ttl: blocks again
    weather = await cache.get(
        key=f"weather:{city}",
        fetch_fn=lambda: fetch_weather(city),
        fresh_ttl=30.0,    # serve fresh for 30s
        stale_ttl=120.0,   # serve stale for up to 2 min while refreshing
    )

    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{"role": "user", "content": f"Summarize weather: {weather}"}],
    )
    return {"weather": weather, "summary": resp.content[0].text}
```

**When to use**: Tool calls that fetch slowly-changing data (weather, exchange rates, DB aggregates). Eliminates the latency spike on cache expiry.

---

## Solution 2: SWR with Per-Key Staleness Thresholds

Different data has different acceptable staleness. Per-key configuration lets prices be fresher than news headlines.

```python
import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

@dataclass
class SWRPolicy:
    fresh_ttl: float      # serve fresh: no background fetch
    stale_ttl: float      # serve stale + revalidate in background
    error_ttl: float = 5.0  # how long to serve last-good value on fetch error

POLICIES: dict[str, SWRPolicy] = {
    "exchange_rate":   SWRPolicy(fresh_ttl=10,  stale_ttl=30),
    "news_headline":   SWRPolicy(fresh_ttl=300, stale_ttl=1800),
    "user_profile":    SWRPolicy(fresh_ttl=60,  stale_ttl=600),
    "db_aggregate":    SWRPolicy(fresh_ttl=30,  stale_ttl=300),
    "static_config":   SWRPolicy(fresh_ttl=3600, stale_ttl=86400),
}

@dataclass
class PolicyEntry:
    value: Any
    fetched_at: float
    policy: SWRPolicy
    refreshing: bool = False
    last_error: Exception | None = None

    def age(self) -> float:
        return time.monotonic() - self.fetched_at

    def state(self) -> str:
        a = self.age()
        if a < self.policy.fresh_ttl:
            return "fresh"
        if a < self.policy.fresh_ttl + self.policy.stale_ttl:
            return "stale"
        return "expired"

class PolicyAwareSWRCache:
    def __init__(self):
        self._store: dict[str, PolicyEntry] = {}

    def _get_policy(self, key: str) -> SWRPolicy:
        for prefix, policy in POLICIES.items():
            if key.startswith(prefix):
                return policy
        return SWRPolicy(fresh_ttl=60, stale_ttl=300)  # default

    async def get(self, key: str, fetch_fn: Callable[[], Awaitable[Any]]) -> tuple[Any, str]:
        """Returns (value, cache_state) where cache_state is 'fresh'|'stale'|'miss'."""
        policy = self._get_policy(key)
        entry = self._store.get(key)

        if entry is None or entry.state() == "expired":
            value = await fetch_fn()
            self._store[key] = PolicyEntry(value=value, fetched_at=time.monotonic(), policy=policy)
            return value, "miss"

        if entry.state() == "fresh":
            return entry.value, "fresh"

        # Stale: serve immediately, refresh in background
        if not entry.refreshing:
            entry.refreshing = True
            asyncio.create_task(self._bg_refresh(key, fetch_fn, policy))
        return entry.value, "stale"

    async def _bg_refresh(self, key: str, fetch_fn, policy: SWRPolicy):
        try:
            value = await fetch_fn()
            self._store[key] = PolicyEntry(value=value, fetched_at=time.monotonic(), policy=policy)
        except Exception as exc:
            entry = self._store.get(key)
            if entry:
                entry.refreshing = False
                entry.last_error = exc

cache = PolicyAwareSWRCache()

async def get_exchange_rate(pair: str) -> dict:
    await asyncio.sleep(0.2)
    return {"pair": pair, "rate": 1.085, "ts": time.time()}

async def agent_query_rate(pair: str) -> dict:
    from anthropic import AsyncAnthropic
    client = AsyncAnthropic()

    rate, state = await cache.get(
        key=f"exchange_rate:{pair}",
        fetch_fn=lambda: get_exchange_rate(pair),
    )

    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{"role": "user", "content": f"Exchange rate data ({state}): {rate}. Summarize briefly."}],
    )
    return {"rate": rate, "cache_state": state, "summary": resp.content[0].text}
```

**When to use**: Agents that query multiple data sources with different update frequencies. A single staleness threshold fits poorly across heterogeneous data.

---

## Solution 3: SWR with Conditional Fetch — ETag / Last-Modified Revalidation

On revalidation, send a conditional request (ETag or If-Modified-Since) so the server can return 304 Not Modified instead of re-sending the full payload.

```python
import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional
import httpx

@dataclass
class ConditionalEntry:
    value: Any
    fetched_at: float
    etag: Optional[str] = None
    last_modified: Optional[str] = None
    fresh_ttl: float = 60.0
    stale_ttl: float = 300.0
    refreshing: bool = False

    def is_fresh(self) -> bool:
        return (time.monotonic() - self.fetched_at) < self.fresh_ttl

    def is_stale(self) -> bool:
        age = time.monotonic() - self.fetched_at
        return self.fresh_ttl <= age < (self.fresh_ttl + self.stale_ttl)

class ConditionalSWRCache:
    def __init__(self):
        self._store: dict[str, ConditionalEntry] = {}

    async def get(self, key: str, url: str, fresh_ttl: float = 60, stale_ttl: float = 300) -> Any:
        entry = self._store.get(key)

        if entry is None or (not entry.is_fresh() and not entry.is_stale()):
            # Full fetch
            value, etag, last_modified = await self._full_fetch(url)
            self._store[key] = ConditionalEntry(
                value=value, fetched_at=time.monotonic(),
                etag=etag, last_modified=last_modified,
                fresh_ttl=fresh_ttl, stale_ttl=stale_ttl,
            )
            return value

        if entry.is_fresh():
            return entry.value

        if entry.is_stale() and not entry.refreshing:
            entry.refreshing = True
            asyncio.create_task(self._conditional_refresh(key, url, entry))

        return entry.value

    async def _full_fetch(self, url: str) -> tuple[Any, Optional[str], Optional[str]]:
        async with httpx.AsyncClient() as http:
            resp = await http.get(url)
            resp.raise_for_status()
            return (
                resp.json(),
                resp.headers.get("ETag"),
                resp.headers.get("Last-Modified"),
            )

    async def _conditional_refresh(self, key: str, url: str, entry: ConditionalEntry):
        headers = {}
        if entry.etag:
            headers["If-None-Match"] = entry.etag
        elif entry.last_modified:
            headers["If-Modified-Since"] = entry.last_modified

        try:
            async with httpx.AsyncClient() as http:
                resp = await http.get(url, headers=headers)

            if resp.status_code == 304:
                # Not modified: just update timestamp
                entry.fetched_at = time.monotonic()
                entry.refreshing = False
                return

            resp.raise_for_status()
            value = resp.json()
            self._store[key] = ConditionalEntry(
                value=value, fetched_at=time.monotonic(),
                etag=resp.headers.get("ETag"),
                last_modified=resp.headers.get("Last-Modified"),
                fresh_ttl=entry.fresh_ttl, stale_ttl=entry.stale_ttl,
            )
        except Exception as exc:
            entry.refreshing = False
            print(f"[swr:conditional] refresh failed for {key!r}: {exc}")
```

**When to use**: Tool calls that hit HTTP APIs supporting ETags (GitHub, REST APIs). Conditional revalidation reduces bandwidth and upstream load by ~70% for slowly-changing data.

---

## Solution 4: SWR with Jitter — Stagger Background Refreshes Under Load

When many cache entries expire simultaneously (e.g., after a server restart), background refreshes spike tool-call load. Add jitter to spread them.

```python
import asyncio
import random
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

@dataclass
class JitteredEntry:
    value: Any
    fetched_at: float
    fresh_ttl: float
    stale_ttl: float
    jitter_fraction: float = 0.1  # ±10% of fresh_ttl
    refreshing: bool = False

    def effective_fresh_ttl(self) -> float:
        jitter = self.fresh_ttl * self.jitter_fraction * (2 * random.random() - 1)
        return self.fresh_ttl + jitter

    def is_fresh(self) -> bool:
        return (time.monotonic() - self.fetched_at) < self.effective_fresh_ttl()

    def is_stale(self) -> bool:
        age = time.monotonic() - self.fetched_at
        return self.effective_fresh_ttl() <= age < (self.fresh_ttl + self.stale_ttl)

class JitteredSWRCache:
    def __init__(self, max_concurrent_refreshes: int = 10):
        self._store: dict[str, JitteredEntry] = {}
        self._sem = asyncio.Semaphore(max_concurrent_refreshes)

    async def get(
        self,
        key: str,
        fetch_fn: Callable[[], Awaitable[Any]],
        fresh_ttl: float = 60.0,
        stale_ttl: float = 300.0,
    ) -> Any:
        entry = self._store.get(key)

        if entry is None or (not entry.is_fresh() and not entry.is_stale()):
            async with self._sem:
                # Double-check after acquiring semaphore
                entry = self._store.get(key)
                if entry and (entry.is_fresh() or entry.is_stale()):
                    return entry.value
                value = await fetch_fn()
                self._store[key] = JitteredEntry(
                    value=value, fetched_at=time.monotonic(),
                    fresh_ttl=fresh_ttl, stale_ttl=stale_ttl,
                )
                return value

        if entry.is_fresh():
            return entry.value

        if entry.is_stale() and not entry.refreshing:
            entry.refreshing = True
            asyncio.create_task(self._bg_refresh(key, fetch_fn, entry))

        return entry.value

    async def _bg_refresh(self, key: str, fetch_fn, entry: JitteredEntry):
        # Random sleep 0–500ms to stagger simultaneous refreshes
        await asyncio.sleep(random.uniform(0, 0.5))
        async with self._sem:
            try:
                value = await fetch_fn()
                self._store[key] = JitteredEntry(
                    value=value, fetched_at=time.monotonic(),
                    fresh_ttl=entry.fresh_ttl, stale_ttl=entry.stale_ttl,
                )
            except Exception:
                entry.refreshing = False
```

**When to use**: Agents with large caches (1000+ keys) where coordinated expiry causes thundering herd. Jitter spreads refreshes across a window, capping peak upstream load.

---

## Solution 5: SWR Warming — Pre-populate Cache Before First Request

Pre-warm cache keys at agent startup so the first user request is never a cache miss.

```python
import asyncio
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

@dataclass
class WarmEntry:
    value: Any
    fetched_at: float
    fresh_ttl: float = 60.0
    stale_ttl: float = 300.0
    refreshing: bool = False

    def state(self) -> str:
        age = time.monotonic() - self.fetched_at
        if age < self.fresh_ttl:
            return "fresh"
        if age < self.fresh_ttl + self.stale_ttl:
            return "stale"
        return "expired"

class PrewarmedSWRCache:
    def __init__(self):
        self._store: dict[str, WarmEntry] = {}
        self._warming = False

    async def warm(self, keys: dict[str, Callable[[], Awaitable[Any]]], concurrency: int = 5) -> dict:
        """Pre-populate cache at startup. Returns {key: success/error}."""
        self._warming = True
        sem = asyncio.Semaphore(concurrency)
        results = {}

        async def warm_one(key: str, fetch_fn: Callable):
            async with sem:
                try:
                    value = await fetch_fn()
                    self._store[key] = WarmEntry(value=value, fetched_at=time.monotonic())
                    results[key] = "ok"
                except Exception as exc:
                    results[key] = f"error: {exc}"

        await asyncio.gather(*[warm_one(k, fn) for k, fn in keys.items()])
        self._warming = False
        return results

    async def get(self, key: str, fetch_fn: Callable[[], Awaitable[Any]]) -> Any:
        entry = self._store.get(key)

        if entry is None or entry.state() == "expired":
            value = await fetch_fn()
            self._store[key] = WarmEntry(value=value, fetched_at=time.monotonic())
            return value

        if entry.state() == "fresh":
            return entry.value

        if not entry.refreshing:
            entry.refreshing = True
            asyncio.create_task(self._bg_refresh(key, fetch_fn))

        return entry.value

    async def _bg_refresh(self, key: str, fetch_fn):
        try:
            value = await fetch_fn()
            self._store[key] = WarmEntry(value=value, fetched_at=time.monotonic())
        except Exception:
            entry = self._store.get(key)
            if entry:
                entry.refreshing = False

async def startup_warm_cache(cache: PrewarmedSWRCache):
    """Called once at agent startup."""
    warm_targets = {
        "config:system_prompt":  lambda: asyncio.sleep(0.1) or {"prompt": "You are a helpful assistant."},
        "config:tools":          lambda: asyncio.sleep(0.1) or {"tools": ["search", "calculator"]},
        "data:exchange_rates":   lambda: asyncio.sleep(0.3) or {"USD/EUR": 0.92},
        "data:product_catalog":  lambda: asyncio.sleep(0.5) or [{"id": 1, "name": "Widget"}],
    }
    results = await cache.warm(warm_targets, concurrency=4)
    print(f"Cache warmed: {results}")

cache = PrewarmedSWRCache()
```

**When to use**: Agents that serve predictable queries at startup (product catalogs, system configs, exchange rates). Pre-warming ensures p50 latency on the first request is identical to steady-state.

---

## Solution 6: SWR Metrics — Track Hit Rate, Staleness, and Revalidation Latency

Instrument the SWR cache to measure hit rate, stale-serve rate, and background refresh duration — the metrics needed to tune TTLs.

```python
import asyncio
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

@dataclass
class SWRMetrics:
    hits_fresh: int = 0
    hits_stale: int = 0
    misses: int = 0
    revalidations: int = 0
    revalidation_errors: int = 0
    revalidation_latencies: list[float] = field(default_factory=list)

    @property
    def hit_rate(self) -> float:
        total = self.hits_fresh + self.hits_stale + self.misses
        return (self.hits_fresh + self.hits_stale) / total if total else 0.0

    @property
    def fresh_hit_rate(self) -> float:
        total = self.hits_fresh + self.hits_stale + self.misses
        return self.hits_fresh / total if total else 0.0

    @property
    def p99_revalidation_ms(self) -> float:
        if not self.revalidation_latencies:
            return 0.0
        sorted_lats = sorted(self.revalidation_latencies)
        idx = int(len(sorted_lats) * 0.99)
        return sorted_lats[min(idx, len(sorted_lats) - 1)] * 1000

    def as_dict(self) -> dict:
        return {
            "hit_rate": round(self.hit_rate, 3),
            "fresh_hit_rate": round(self.fresh_hit_rate, 3),
            "hits_fresh": self.hits_fresh,
            "hits_stale": self.hits_stale,
            "misses": self.misses,
            "revalidations": self.revalidations,
            "revalidation_errors": self.revalidation_errors,
            "p99_revalidation_ms": round(self.p99_revalidation_ms, 1),
        }

@dataclass
class InstrumentedEntry:
    value: Any
    fetched_at: float
    fresh_ttl: float
    stale_ttl: float
    refreshing: bool = False

    def state(self) -> str:
        age = time.monotonic() - self.fetched_at
        if age < self.fresh_ttl:
            return "fresh"
        if age < self.fresh_ttl + self.stale_ttl:
            return "stale"
        return "expired"

class InstrumentedSWRCache:
    def __init__(self):
        self._store: dict[str, InstrumentedEntry] = {}
        self.metrics = SWRMetrics()

    async def get(
        self,
        key: str,
        fetch_fn: Callable[[], Awaitable[Any]],
        fresh_ttl: float = 60.0,
        stale_ttl: float = 300.0,
    ) -> Any:
        entry = self._store.get(key)

        if entry is None or entry.state() == "expired":
            self.metrics.misses += 1
            value = await fetch_fn()
            self._store[key] = InstrumentedEntry(
                value=value, fetched_at=time.monotonic(),
                fresh_ttl=fresh_ttl, stale_ttl=stale_ttl,
            )
            return value

        if entry.state() == "fresh":
            self.metrics.hits_fresh += 1
            return entry.value

        self.metrics.hits_stale += 1
        if not entry.refreshing:
            entry.refreshing = True
            asyncio.create_task(self._bg_refresh(key, fetch_fn, entry))
        return entry.value

    async def _bg_refresh(self, key: str, fetch_fn, entry: InstrumentedEntry):
        start = time.monotonic()
        self.metrics.revalidations += 1
        try:
            value = await fetch_fn()
            elapsed = time.monotonic() - start
            self.metrics.revalidation_latencies.append(elapsed)
            self._store[key] = InstrumentedEntry(
                value=value, fetched_at=time.monotonic(),
                fresh_ttl=entry.fresh_ttl, stale_ttl=entry.stale_ttl,
            )
        except Exception:
            self.metrics.revalidation_errors += 1
            entry.refreshing = False

    def report(self) -> dict:
        return self.metrics.as_dict()
```

**When to use**: Production agents where you need to tune TTLs based on real traffic. A high stale hit rate means fresh_ttl is too short; a high miss rate means stale_ttl is too short.

---

## Comparison

| Solution | Latency on Miss | Latency on Stale | Bandwidth Efficient | Thundering Herd | Cold Start | Best For |
|---|---|---|---|---|---|---|
| Basic SWR | Blocks | Instant | No | Possible | Cold | Getting started |
| Per-key policies | Blocks | Instant | No | Possible | Cold | Heterogeneous data sources |
| Conditional fetch | Blocks | Instant | Yes (304) | Possible | Cold | HTTP APIs with ETag support |
| Jittered SWR | Blocks | Instant | No | No | Cold | High-concurrency agents |
| Warmed SWR | Instant | Instant | No | No | Hot | Predictable startup queries |
| Instrumented SWR | Blocks | Instant | No | Possible | Cold | TTL tuning in production |

**Rule of thumb**: Use basic SWR (Solution 1) as the default. Set `fresh_ttl` to the data's natural update interval and `stale_ttl` to 5–10× that. Add jitter (Solution 4) when you have >100 concurrent users. Add warming (Solution 5) for any data fetched on every agent startup.
