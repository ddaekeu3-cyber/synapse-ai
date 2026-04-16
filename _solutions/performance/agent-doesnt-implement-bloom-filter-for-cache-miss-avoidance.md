---
title: "Agent Doesn't Implement Bloom Filter for Cache Miss Avoidance"
description: "How to use Bloom filters, counting Bloom filters, and Cuckoo filters to avoid expensive cache lookups and backend calls for keys that are guaranteed not to exist — dramatically reducing wasted I/O in AI agent systems."
date: 2025-01-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-bloom-filter-for-cache-miss-avoidance
tags:
  - performance
  - bloom-filter
  - caching
  - probabilistic-data-structures
  - cache-miss
  - negative-caching
  - memory-efficiency
symptoms:
  - "Cache lookup followed by database query for every unknown key wastes I/O"
  - "Embedding cache hit rate low because non-existent keys still trigger Redis round-trips"
  - "Tool result cache always misses on first call per key, generating unnecessary backend requests"
  - "No way to tell if a key has ever been seen without querying the full cache"
  - "Negative lookups (key does not exist) are just as expensive as positive ones"
  - "Memory-efficient pre-filter needed before hitting the main cache layer"
---

## Why This Happens

Every cache lookup involves network I/O (Redis), disk I/O (database), or at minimum a hash-map traversal. For keys that *never exist* in the cache — first-time queries, hallucinated tool arguments, probing attacks — this I/O is entirely wasted. A Bloom filter is a probabilistic data structure that uses a tiny fraction of memory (typically 10 bits per element) to answer "has this key ever been inserted?" with **zero false negatives** and a configurable small false-positive rate. If the Bloom filter says no, skip the cache entirely. If it says yes (possibly false positive), proceed with the normal lookup.

For AI agents handling millions of queries, intercepting even 30% of cache lookups with a Bloom filter pre-check can eliminate enormous amounts of wasted I/O.

---

## Solution 1: Pure-Python Bloom Filter

A compact Bloom filter implementation with configurable capacity and false-positive rate.

```python
import math
import hashlib
from typing import Union

class BloomFilter:
    """
    Classic Bloom filter with k hash functions derived from double-hashing.
    Space-optimal for the given (n, p) parameters.
    """

    def __init__(self, capacity: int, false_positive_rate: float = 0.01):
        """
        capacity: expected number of elements to insert
        false_positive_rate: acceptable false-positive probability (0 < p < 1)
        """
        self.capacity = capacity
        self.fp_rate = false_positive_rate
        self.size = self._optimal_size(capacity, false_positive_rate)
        self.num_hashes = self._optimal_hashes(self.size, capacity)
        self._bits = bytearray(math.ceil(self.size / 8))
        self._count = 0

    @staticmethod
    def _optimal_size(n: int, p: float) -> int:
        return int(-n * math.log(p) / (math.log(2) ** 2))

    @staticmethod
    def _optimal_hashes(m: int, n: int) -> int:
        return max(1, int(m / n * math.log(2)))

    def _hashes(self, item: Union[str, bytes]) -> list[int]:
        """Generate k hash positions using double hashing (Kirsch-Mitzenmacher)."""
        if isinstance(item, str):
            item = item.encode("utf-8")
        h1 = int(hashlib.md5(item).hexdigest(), 16)
        h2 = int(hashlib.sha1(item).hexdigest(), 16)
        return [(h1 + i * h2) % self.size for i in range(self.num_hashes)]

    def _set_bit(self, pos: int) -> None:
        self._bits[pos // 8] |= 1 << (pos % 8)

    def _get_bit(self, pos: int) -> bool:
        return bool(self._bits[pos // 8] & (1 << (pos % 8)))

    def add(self, item: Union[str, bytes]) -> None:
        for pos in self._hashes(item):
            self._set_bit(pos)
        self._count += 1

    def __contains__(self, item: Union[str, bytes]) -> bool:
        """
        Returns False: item DEFINITELY not in set (zero false negatives).
        Returns True: item PROBABLY in set (may be a false positive).
        """
        return all(self._get_bit(pos) for pos in self._hashes(item))

    def __len__(self) -> int:
        return self._count

    @property
    def fill_ratio(self) -> float:
        set_bits = sum(bin(b).count("1") for b in self._bits)
        return set_bits / self.size

    @property
    def estimated_fp_rate(self) -> float:
        """Current false-positive rate based on fill ratio."""
        return (1 - math.exp(-self.num_hashes * self._count / self.size)) ** self.num_hashes

    def memory_bytes(self) -> int:
        return len(self._bits)


# --- Usage ---

def demo_bloom():
    bf = BloomFilter(capacity=100_000, false_positive_rate=0.01)

    # Add known cache keys
    for i in range(10_000):
        bf.add(f"embedding:{i}")

    # Check before cache lookup
    print("Known key in filter:", "embedding:500" in bf)      # True
    print("Unknown key in filter:", "embedding:99999" in bf)  # False (skip cache)
    print(f"Memory: {bf.memory_bytes() / 1024:.1f} KB")
    print(f"Est FP rate: {bf.estimated_fp_rate:.4f}")
```

---

## Solution 2: Bloom-Filtered Cache Wrapper

Wrap any cache with a Bloom filter pre-check. If the filter says "no", skip the cache entirely and return None immediately.

```python
import asyncio
from typing import Any, Callable, Awaitable, Optional

class BloomFilteredCache:
    """
    Cache wrapper that uses a Bloom filter to skip lookups for keys that
    have never been inserted — eliminating unnecessary I/O for cold keys.
    """

    def __init__(
        self,
        backing_cache,
        capacity: int = 1_000_000,
        fp_rate: float = 0.01,
    ):
        self._cache = backing_cache
        self._bloom = BloomFilter(capacity=capacity, false_positive_rate=fp_rate)
        self._stats = {"bloom_rejected": 0, "bloom_passed": 0, "cache_hits": 0, "cache_misses": 0}

    async def get(self, key: str) -> Optional[Any]:
        if key not in self._bloom:
            # Definite miss — skip cache I/O entirely
            self._stats["bloom_rejected"] += 1
            return None

        self._stats["bloom_passed"] += 1
        value = await self._cache.get(key)
        if value is not None:
            self._stats["cache_hits"] += 1
        else:
            self._stats["cache_misses"] += 1
        return value

    async def set(self, key: str, value: Any, ttl: Optional[float] = None) -> None:
        self._bloom.add(key)
        await self._cache.set(key, value, ttl=ttl)

    async def delete(self, key: str) -> None:
        # Note: standard Bloom filters can't remove entries.
        # Use CountingBloomFilter (Solution 3) if deletion is needed.
        await self._cache.delete(key)

    def stats(self) -> dict:
        total = self._stats["bloom_rejected"] + self._stats["bloom_passed"]
        skip_rate = self._stats["bloom_rejected"] / total if total else 0
        return {
            **self._stats,
            "bloom_skip_rate": round(skip_rate, 3),
            "bloom_fill_ratio": round(self._bloom.fill_ratio, 3),
            "bloom_estimated_fp_rate": round(self._bloom.estimated_fp_rate, 4),
        }


# --- Embedding cache with Bloom filter ---

class EmbeddingCache:
    def __init__(self):
        self._store: dict[str, Any] = {}

    async def get(self, key: str) -> Optional[Any]:
        return self._store.get(key)

    async def set(self, key: str, value: Any, ttl: Optional[float] = None) -> None:
        self._store[key] = value

    async def delete(self, key: str) -> None:
        self._store.pop(key, None)


async def demo_bloom_cache():
    base_cache = EmbeddingCache()
    cache = BloomFilteredCache(base_cache, capacity=500_000, fp_rate=0.005)

    # Populate some entries
    for i in range(1000):
        await cache.set(f"emb:{i}", [0.1 * i] * 128)

    # Known key — Bloom says yes, cache returns value
    val = await cache.get("emb:42")
    print(f"Known key: {'hit' if val else 'miss'}")

    # Unknown key — Bloom says no, zero I/O to backing cache
    val = await cache.get("emb:99999")
    print(f"Unknown key: {'hit' if val else 'miss (bloom filtered)'}")

    print("Stats:", cache.stats())
```

---

## Solution 3: Counting Bloom Filter (Supports Deletions)

Standard Bloom filters cannot remove entries. A counting Bloom filter uses counters instead of bits, enabling safe deletion at the cost of 4–8x more memory.

```python
class CountingBloomFilter:
    """
    Counting Bloom filter: each position stores a counter instead of a single bit.
    Supports deletion as long as items are not inserted more times than the counter width.
    """

    MAX_COUNT = 15  # 4-bit counters

    def __init__(self, capacity: int, false_positive_rate: float = 0.01):
        self.capacity = capacity
        self.fp_rate = false_positive_rate
        self.size = BloomFilter._optimal_size(capacity, false_positive_rate)
        self.num_hashes = BloomFilter._optimal_hashes(self.size, capacity)
        self._counters = bytearray(self.size)  # one byte per slot (can use nibbles for memory)
        self._count = 0

    def _hashes(self, item: str) -> list[int]:
        item_bytes = item.encode("utf-8") if isinstance(item, str) else item
        h1 = int(hashlib.md5(item_bytes).hexdigest(), 16)
        h2 = int(hashlib.sha1(item_bytes).hexdigest(), 16)
        return [(h1 + i * h2) % self.size for i in range(self.num_hashes)]

    def add(self, item: str) -> None:
        for pos in self._hashes(item):
            if self._counters[pos] < 255:
                self._counters[pos] += 1
        self._count += 1

    def remove(self, item: str) -> bool:
        """Remove an item. Returns False if item was not present (based on counters)."""
        positions = self._hashes(item)
        if not all(self._counters[pos] > 0 for pos in positions):
            return False  # Not present
        for pos in positions:
            self._counters[pos] -= 1
        self._count -= 1
        return True

    def __contains__(self, item: str) -> bool:
        return all(self._counters[pos] > 0 for pos in self._hashes(item))

    def __len__(self) -> int:
        return self._count
```

---

## Solution 4: Scalable Bloom Filter (Grows with Data)

A scalable Bloom filter adds new sub-filters as capacity fills, maintaining the target false-positive rate across unbounded data.

```python
class ScalableBloomFilter:
    """
    Bloom filter that grows dynamically by adding new sub-filters.
    Maintains the target FP rate regardless of how many elements are inserted.
    """

    SCALE_FACTOR = 2       # Each new filter is 2x larger
    FP_TIGHTENING = 0.9   # Each new filter has tighter FP rate

    def __init__(
        self,
        initial_capacity: int = 10_000,
        target_fp_rate: float = 0.01,
    ):
        self.target_fp_rate = target_fp_rate
        self._filters: list[BloomFilter] = []
        self._add_filter(initial_capacity, target_fp_rate * (1 - self.FP_TIGHTENING))

    def _add_filter(self, capacity: int, fp_rate: float) -> None:
        self._filters.append(BloomFilter(capacity=capacity, false_positive_rate=fp_rate))

    def _current_filter(self) -> BloomFilter:
        return self._filters[-1]

    def add(self, item: str) -> None:
        current = self._current_filter()
        # Grow if current filter is more than 90% full
        if current._count >= current.capacity * 0.9:
            new_capacity = current.capacity * self.SCALE_FACTOR
            new_fp = self.target_fp_rate * (self.FP_TIGHTENING ** len(self._filters))
            self._add_filter(new_capacity, max(new_fp, 1e-7))
        self._current_filter().add(item)

    def __contains__(self, item: str) -> bool:
        return any(item in f for f in self._filters)

    def __len__(self) -> int:
        return sum(len(f) for f in self._filters)

    @property
    def num_filters(self) -> int:
        return len(self._filters)

    def memory_bytes(self) -> int:
        return sum(f.memory_bytes() for f in self._filters)
```

---

## Solution 5: Bloom Filter for Negative Caching (404 Guard)

Use a Bloom filter specifically to track keys confirmed to *not exist* in the backend, preventing repeated lookups for the same missing key.

```python
import asyncio
import time
from typing import Any, Optional, Callable, Awaitable

class NegativeCacheBloomFilter:
    """
    Guards against repeated backend lookups for known-missing keys.
    On confirmed miss, adds key to Bloom filter. Subsequent lookups skip backend.
    """

    def __init__(
        self,
        capacity: int = 100_000,
        fp_rate: float = 0.01,
        negative_ttl: float = 300.0,   # How long to trust "not exists"
    ):
        self._bloom = BloomFilter(capacity=capacity, false_positive_rate=fp_rate)
        self._confirmed_missing: dict[str, float] = {}  # key -> expiry
        self._negative_ttl = negative_ttl
        self._stats = {"bloom_blocked": 0, "backend_saved": 0}

    def mark_missing(self, key: str) -> None:
        """Record that this key definitively does not exist in the backend."""
        self._bloom.add(key)
        self._confirmed_missing[key] = time.monotonic() + self._negative_ttl

    def is_known_missing(self, key: str) -> bool:
        """Returns True if key is definitely missing (with TTL check)."""
        if key not in self._bloom:
            return False
        exp = self._confirmed_missing.get(key, 0)
        if time.monotonic() < exp:
            self._stats["bloom_blocked"] += 1
            return True
        # TTL expired — allow re-check
        return False

    async def get_with_negative_cache(
        self,
        key: str,
        backend_fn: Callable[[str], Awaitable[Optional[Any]]],
    ) -> Optional[Any]:
        if self.is_known_missing(key):
            self._stats["backend_saved"] += 1
            return None  # Skip backend call

        result = await backend_fn(key)
        if result is None:
            self.mark_missing(key)

        return result


# --- Agent tool result negative cache ---

class ToolResultCache:
    def __init__(self):
        self._store: dict[str, Any] = {}
        self._neg_cache = NegativeCacheBloomFilter(capacity=200_000)

    async def get_tool_result(
        self,
        cache_key: str,
        compute_fn: Callable[[], Awaitable[Any]],
    ) -> Any:
        # Check negative cache first (is this key known to not exist?)
        async def fetch_from_store(k: str) -> Optional[Any]:
            return self._store.get(k)

        result = await self._neg_cache.get_with_negative_cache(cache_key, fetch_from_store)
        if result is not None:
            return result

        # Cache miss — compute and store
        result = await compute_fn()
        if result is not None:
            self._store[cache_key] = result
        return result
```

---

## Solution 6: Distributed Bloom Filter via Redis Bit Arrays

For multi-process agents, store the Bloom filter in Redis using SETBIT/GETBIT for cross-process sharing.

```python
import asyncio
import redis.asyncio as aioredis
import hashlib
import math

class RedisBloomFilter:
    """
    Distributed Bloom filter backed by Redis bit arrays.
    Shared across all agent processes for consistent cache-miss avoidance.
    """

    def __init__(
        self,
        redis_url: str,
        key: str,
        capacity: int = 1_000_000,
        fp_rate: float = 0.01,
    ):
        self.redis = aioredis.from_url(redis_url)
        self.key = key
        self.size = BloomFilter._optimal_size(capacity, fp_rate)
        self.num_hashes = BloomFilter._optimal_hashes(self.size, capacity)

    def _hashes(self, item: str) -> list[int]:
        item_bytes = item.encode("utf-8")
        h1 = int(hashlib.md5(item_bytes).hexdigest(), 16)
        h2 = int(hashlib.sha1(item_bytes).hexdigest(), 16)
        return [(h1 + i * h2) % self.size for i in range(self.num_hashes)]

    async def add(self, item: str) -> None:
        async with self.redis.pipeline(transaction=False) as pipe:
            for pos in self._hashes(item):
                pipe.setbit(self.key, pos, 1)
            await pipe.execute()

    async def contains(self, item: str) -> bool:
        async with self.redis.pipeline(transaction=False) as pipe:
            for pos in self._hashes(item):
                pipe.getbit(self.key, pos)
            bits = await pipe.execute()
        return all(bits)

    async def add_batch(self, items: list[str]) -> None:
        async with self.redis.pipeline(transaction=False) as pipe:
            for item in items:
                for pos in self._hashes(item):
                    pipe.setbit(self.key, pos, 1)
            await pipe.execute()

    async def memory_bytes(self) -> int:
        return math.ceil(self.size / 8)


# --- Usage with agent embedding cache ---

async def demo_redis_bloom():
    rbf = RedisBloomFilter("redis://localhost:6379", "agent:embedding_bloom", capacity=500_000)

    # On startup, pre-populate from existing cache keys
    existing_keys = [f"emb:{i}" for i in range(10_000)]
    await rbf.add_batch(existing_keys)

    # Pre-check before Redis embedding lookup
    key = "emb:42"
    if await rbf.contains(key):
        print(f"{key}: proceed with cache lookup")
    else:
        print(f"{key}: definite miss — skip lookup")
```

---

## Comparison

| Solution | Deletion Support | Scalable | Distributed | Memory | Best For |
|---|---|---|---|---|---|
| Pure-Python Bloom Filter | No | No | No | ~1.2 bytes/elem | Single-process, static dataset |
| Bloom-Filtered Cache Wrapper | No | No | No | ~1.2 bytes/elem | Drop-in cache pre-filter |
| Counting Bloom Filter | Yes | No | No | ~8 bytes/elem | Caches with key eviction |
| Scalable Bloom Filter | No | Yes | No | Grows dynamically | Unknown/unbounded key sets |
| Negative Cache Bloom Filter | No (TTL bypass) | No | No | ~1.2 bytes/elem | Avoiding repeated miss lookups |
| Redis Bloom Filter | No | No | Yes | ~size/8 bytes | Multi-process agents |

**Use the Bloom-filtered cache wrapper** as the default — it's the simplest integration and provides immediate I/O savings. **Switch to counting Bloom filter** if your cache evicts entries and you need safe key removal. **Use scalable Bloom filter** when total dataset size is unknown at startup. **Use the negative cache pattern** specifically to block repeat lookups for confirmed-missing keys. **Deploy Redis Bloom filter** when multiple agent processes share the same logical cache.
