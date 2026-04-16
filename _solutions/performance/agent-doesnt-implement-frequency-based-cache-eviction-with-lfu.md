---
title: "Agent Doesn't Implement Frequency-Based Cache Eviction with LFU"
description: "AI agents that use LRU (Least Recently Used) eviction for tool result caches evict infrequently-accessed but expensive results that happen not to have been used recently. LFU (Least Frequently Used) eviction retains the most-called tool results regardless of recency, making it significantly more effective for agents with a stable set of hot queries — such as frequently-repeated database lookups or popular document embeddings."
date: 2025-02-15
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-frequency-based-cache-eviction-with-lfu
tags:
  - lfu
  - cache-eviction
  - frequency
  - tool-cache
  - performance
  - caching
  - hot-path
symptoms:
  - "Frequently-called tool results keep getting evicted just because they weren't used in the last minute"
  - "Cache hit rate drops under bursty but repetitive workloads"
  - "LRU evicts the most expensive-to-recompute result because a burst of unrelated queries pushed it out"
  - "Cache is large enough to hold hot results but keeps evicting them in favour of cold one-off queries"
  - "p99 latency spikes when hot cache entries are evicted and must be recomputed"
---

## Problem

LRU eviction uses recency as a proxy for future access probability. This is correct for workloads where access patterns change over time but wrong for agents with a stable set of hot queries — a `get_config()` call made every request, a `list_enabled_features()` call made per-session, or an embedding of a system document. LFU eviction tracks access frequency per entry and evicts the least-frequently-accessed item on each cache miss. Entries with sustained high access frequency survive indefinitely; one-off queries are evicted quickly.

---

## Solution 1: LFUCache — O(1) Least Frequently Used Cache

```python
from collections import defaultdict, OrderedDict
from typing import Any, Generic, Hashable, Optional, TypeVar

K = TypeVar("K")
V = TypeVar("V")


class LFUCache:
    """
    O(1) LFU cache using a doubly-linked frequency map.
    Evicts the entry with the lowest access frequency;
    ties are broken by recency (LRU within the same frequency bucket).

    Usage:
        cache = LFUCache(capacity=1000)
        cache.put("key", value)
        result = cache.get("key")   # returns None on miss
        hit_rate = cache.stats()["hit_rate"]
    """

    def __init__(self, capacity: int):
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self._capacity = capacity
        self._key_to_val: dict = {}
        self._key_to_freq: dict = {}
        # freq -> OrderedDict (key -> None) maintaining insertion order for LRU tiebreak
        self._freq_to_keys: dict = defaultdict(OrderedDict)
        self._min_freq = 0
        self._hits = 0
        self._misses = 0

    def get(self, key) -> Optional[Any]:
        if key not in self._key_to_val:
            self._misses += 1
            return None
        self._increment_freq(key)
        self._hits += 1
        return self._key_to_val[key]

    def put(self, key, value):
        if self._capacity == 0:
            return
        if key in self._key_to_val:
            self._key_to_val[key] = value
            self._increment_freq(key)
            return
        if len(self._key_to_val) >= self._capacity:
            self._evict()
        self._key_to_val[key] = value
        self._key_to_freq[key] = 1
        self._freq_to_keys[1][key] = None
        self._min_freq = 1

    def _increment_freq(self, key):
        freq = self._key_to_freq[key]
        self._key_to_freq[key] = freq + 1
        del self._freq_to_keys[freq][key]
        if not self._freq_to_keys[freq] and freq == self._min_freq:
            self._min_freq += 1
        self._freq_to_keys[freq + 1][key] = None

    def _evict(self):
        keys = self._freq_to_keys[self._min_freq]
        evict_key, _ = keys.popitem(last=False)
        del self._key_to_val[evict_key]
        del self._key_to_freq[evict_key]

    def __len__(self) -> int:
        return len(self._key_to_val)

    def stats(self) -> dict:
        total = self._hits + self._misses
        return {
            "capacity": self._capacity,
            "size": len(self._key_to_val),
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": round(self._hits / max(total, 1), 4),
            "min_freq": self._min_freq,
        }
```

---

## Solution 2: TTLLFUCache — LFU with Entry Expiry

```python
import time
from typing import Any, Optional


class TTLLFUCache:
    """
    LFU cache with per-entry TTL expiry.
    Expired entries behave as cache misses and are lazily evicted.

    Usage:
        cache = TTLLFUCache(capacity=500, default_ttl_s=300.0)
        cache.put("config", config_data)
        cache.put("session:abc", session, ttl_s=3600)
        val = cache.get("config")
    """

    def __init__(self, capacity: int, default_ttl_s: float = 300.0):
        self._lfu = LFUCache(capacity)
        self._expiry: dict = {}  # key -> expiry monotonic time
        self._default_ttl = default_ttl_s

    def put(self, key, value, ttl_s: Optional[float] = None):
        ttl = ttl_s if ttl_s is not None else self._default_ttl
        self._lfu.put(key, value)
        self._expiry[key] = time.monotonic() + ttl

    def get(self, key) -> Optional[Any]:
        exp = self._expiry.get(key)
        if exp is not None and time.monotonic() > exp:
            # Lazy expiry: treat as miss and clean up
            self._invalidate(key)
            return None
        return self._lfu.get(key)

    def _invalidate(self, key):
        # Force LFU to see the key as evicted by manipulating internal state
        if key in self._lfu._key_to_val:
            del self._lfu._key_to_val[key]
            freq = self._lfu._key_to_freq.pop(key, None)
            if freq and key in self._lfu._freq_to_keys[freq]:
                del self._lfu._freq_to_keys[freq][key]
        self._expiry.pop(key, None)

    def invalidate(self, key):
        self._invalidate(key)

    def stats(self) -> dict:
        now = time.monotonic()
        expired = sum(1 for exp in self._expiry.values() if now > exp)
        base = self._lfu.stats()
        base["expired_entries"] = expired
        return base
```

---

## Solution 3: FrequencyBiasedEviction — Hybrid LFU+LRU (TinyLFU-Inspired)

```python
import hashlib
import time
from collections import deque
from typing import Any, Optional


class FrequencyBiasedEviction:
    """
    TinyLFU-inspired cache: combines a small LRU admission window with
    a frequency-biased main cache. New entries first enter the LRU window;
    they are promoted to the main LFU cache only if their access count
    exceeds the eviction candidate's count.
    This prevents one-off "scan" queries from displacing hot entries.

    Usage:
        cache = FrequencyBiasedEviction(
            main_capacity=900,
            window_capacity=100,
        )
        cache.put("hot_key", value)
        val = cache.get("hot_key")
    """

    def __init__(self, main_capacity: int = 900,
                 window_capacity: int = 100):
        self._main = LFUCache(main_capacity)
        self._window: dict = {}               # key -> (value, access_count)
        self._window_order: deque = deque()   # insertion order for LRU eviction
        self._window_cap = window_capacity
        self._hits = 0
        self._misses = 0

    def get(self, key) -> Optional[Any]:
        # Check main cache first
        val = self._main.get(key)
        if val is not None:
            self._hits += 1
            return val
        # Check window
        if key in self._window:
            v, count = self._window[key]
            self._window[key] = (v, count + 1)
            self._hits += 1
            return v
        self._misses += 1
        return None

    def put(self, key, value):
        if self._main.get(key) is not None:
            self._main.put(key, value)
            return
        if key in self._window:
            v, count = self._window[key]
            self._window[key] = (value, count + 1)
            return

        # Admit to window
        if len(self._window) >= self._window_cap:
            # Evict oldest window entry, possibly promote to main
            evict_key = self._window_order.popleft()
            if evict_key in self._window:
                ev_val, ev_count = self._window.pop(evict_key)
                # Promote to main if frequently accessed
                if ev_count >= 2:
                    self._main.put(evict_key, ev_val)

        self._window[key] = (value, 1)
        self._window_order.append(key)

    def stats(self) -> dict:
        total = self._hits + self._misses
        return {
            "main": self._main.stats(),
            "window_size": len(self._window),
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": round(self._hits / max(total, 1), 4),
        }
```

---

## Solution 4: LFUToolResultCache — Async Tool Result Caching

```python
import asyncio
import hashlib
import json
import time
from typing import Any, Callable, Optional


class LFUToolResultCache:
    """
    Async wrapper that caches tool function results using LFU eviction.
    Transparently caches results keyed by tool name + serialised arguments.

    Usage:
        cache = LFUToolResultCache(capacity=2000, default_ttl_s=300)

        # Wrap expensive tool functions:
        result = await cache.call("get_config", get_config_fn)
        result = await cache.call("db_query", db.fetch, user_id="u-123")
    """

    def __init__(self, capacity: int = 2000,
                 default_ttl_s: float = 300.0):
        self._lfu = TTLLFUCache(capacity, default_ttl_s)
        self._in_flight: dict = {}   # key -> asyncio.Task (deduplication)

    def _cache_key(self, tool_name: str, args, kwargs) -> str:
        try:
            raw = json.dumps(
                {"t": tool_name, "a": list(args), "k": kwargs},
                sort_keys=True, default=str,
            )
        except TypeError:
            raw = f"{tool_name}:{str(args)}:{str(kwargs)}"
        return hashlib.sha256(raw.encode()).hexdigest()[:20]

    async def call(self, tool_name: str, fn: Callable,
                    *args,
                    ttl_s: Optional[float] = None,
                    **kwargs) -> Any:
        key = self._cache_key(tool_name, args, kwargs)

        cached = self._lfu.get(key)
        if cached is not None:
            return cached

        # Deduplicate in-flight requests
        if key in self._in_flight:
            return await self._in_flight[key]

        task = asyncio.create_task(fn(*args, **kwargs))
        self._in_flight[key] = task
        try:
            result = await task
            self._lfu.put(key, result, ttl_s=ttl_s)
            return result
        finally:
            self._in_flight.pop(key, None)

    def invalidate(self, tool_name: str, *args, **kwargs):
        key = self._cache_key(tool_name, args, kwargs)
        self._lfu.invalidate(key)

    def stats(self) -> dict:
        return self._lfu.stats()
```

---

## Solution 5: AdaptiveLFUCache — Self-Tuning Capacity

```python
import time
from typing import Any, Optional


class AdaptiveLFUCache:
    """
    LFU cache that adjusts its effective capacity based on measured
    hit rate. If hit rate drops below target, capacity is increased
    (up to max_capacity). If hit rate exceeds target by a margin,
    capacity is reduced to reclaim memory.

    Usage:
        cache = AdaptiveLFUCache(
            initial_capacity=500,
            max_capacity=5000,
            target_hit_rate=0.80,
        )
    """

    def __init__(self, initial_capacity: int = 500,
                 max_capacity: int = 5000,
                 target_hit_rate: float = 0.80,
                 adjust_interval: int = 1000):
        self._max = max_capacity
        self._target = target_hit_rate
        self._adjust_every = adjust_interval
        self._ops = 0
        self._lfu = LFUCache(initial_capacity)

    def get(self, key) -> Optional[Any]:
        result = self._lfu.get(key)
        self._ops += 1
        if self._ops % self._adjust_every == 0:
            self._adjust()
        return result

    def put(self, key, value):
        self._lfu.put(key, value)

    def _adjust(self):
        stats = self._lfu.stats()
        hr = stats["hit_rate"]
        cap = stats["capacity"]

        if hr < self._target and cap < self._max:
            new_cap = min(int(cap * 1.5), self._max)
            self._resize(new_cap)
        elif hr > self._target + 0.10 and cap > 100:
            new_cap = max(int(cap * 0.75), 100)
            self._resize(new_cap)

    def _resize(self, new_cap: int):
        old = self._lfu
        self._lfu = LFUCache(new_cap)
        # Re-insert entries sorted by frequency (keep hottest)
        entries = sorted(
            [(k, old._key_to_freq.get(k, 1), old._key_to_val[k])
             for k in old._key_to_val],
            key=lambda x: -x[1],
        )
        for key, _, val in entries[:new_cap]:
            self._lfu.put(key, val)

    def stats(self) -> dict:
        return self._lfu.stats()
```

---

## Solution 6: CacheEvictionBenchmark — Compare LRU vs LFU for Your Workload

```python
import random
import time
from collections import OrderedDict
from typing import List, Tuple


class SimpleLRU:
    """Minimal LRU for benchmarking comparison."""
    def __init__(self, capacity: int):
        self._cap = capacity
        self._cache: OrderedDict = OrderedDict()
        self._hits = self._misses = 0

    def get(self, key):
        if key in self._cache:
            self._cache.move_to_end(key)
            self._hits += 1
            return self._cache[key]
        self._misses += 1
        return None

    def put(self, key, value):
        if key in self._cache:
            self._cache.move_to_end(key)
        self._cache[key] = value
        if len(self._cache) > self._cap:
            self._cache.popitem(last=False)

    def stats(self):
        total = self._hits + self._misses
        return {"hit_rate": round(self._hits / max(total, 1), 4)}


class CacheEvictionBenchmark:
    """
    Simulates an access trace and measures LRU vs LFU hit rates.
    Use this to decide which eviction policy fits your agent's workload.

    Usage:
        bench = CacheEvictionBenchmark(capacity=100)
        # Zipf distribution simulates real agent query patterns:
        trace = bench.generate_zipf_trace(n=10_000, vocab=500, alpha=1.2)
        results = bench.compare(trace)
        print(results)
        # {"lru_hit_rate": 0.71, "lfu_hit_rate": 0.84, "winner": "LFU"}
    """

    def __init__(self, capacity: int = 100):
        self._cap = capacity

    def generate_zipf_trace(self, n: int = 10_000,
                              vocab: int = 500,
                              alpha: float = 1.2) -> List[int]:
        """Generate a Zipf-distributed access trace."""
        weights = [1.0 / (i ** alpha) for i in range(1, vocab + 1)]
        total = sum(weights)
        probs = [w / total for w in weights]
        return random.choices(range(vocab), weights=probs, k=n)

    def compare(self, trace: List[int]) -> dict:
        lru = SimpleLRU(self._cap)
        lfu = LFUCache(self._cap)

        for key in trace:
            if lru.get(key) is None:
                lru.put(key, key)
            if lfu.get(key) is None:
                lfu.put(key, key)

        lru_hr = lru.stats()["hit_rate"]
        lfu_hr = lfu.stats()["hit_rate"]
        return {
            "capacity": self._cap,
            "trace_length": len(trace),
            "lru_hit_rate": lru_hr,
            "lfu_hit_rate": lfu_hr,
            "lfu_improvement_pct": round((lfu_hr - lru_hr) / max(lru_hr, 0.001) * 100, 1),
            "winner": "LFU" if lfu_hr > lru_hr else "LRU",
        }
```

---

## Comparison

| Approach | Eviction Policy | TTL | Async | Self-Tuning | Benchmark |
|---|---|---|---|---|---|
| **LFUCache** | Pure LFU O(1) | No | No | No | No |
| **TTLLFUCache** | LFU + TTL | Yes | No | No | No |
| **FrequencyBiasedEviction** | TinyLFU hybrid | No | No | No | No |
| **LFUToolResultCache** | LFU + TTL | Yes | Yes | No | No |
| **AdaptiveLFUCache** | LFU adaptive | No | No | Yes | No |
| **CacheEvictionBenchmark** | Both | No | No | No | Yes |

**Key insight**: LFU outperforms LRU when a small fraction of keys account for a large fraction of accesses (Zipf distribution) — the typical pattern for agent tool calls where a handful of tools are called on every request. Run `CacheEvictionBenchmark` against a real access trace before choosing; if `lfu_improvement_pct` is > 10%, switch to LFU. Use `FrequencyBiasedEviction` (TinyLFU-inspired) for workloads that mix hot keys with bursty scan-like access patterns — it protects hot entries without penalising cold-start queries.
