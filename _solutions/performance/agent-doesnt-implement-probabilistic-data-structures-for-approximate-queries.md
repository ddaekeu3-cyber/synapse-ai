---
title: "Agent Doesn't Implement Probabilistic Data Structures for Approximate Queries"
description: "Agents that answer count, membership, and frequency queries with exact data structures pay unnecessary memory and latency costs. Implement probabilistic data structures — Bloom filters for membership testing, HyperLogLog for cardinality estimation, Count-Min Sketch for frequency tracking — to answer approximate queries in constant space with configurable error bounds."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-probabilistic-data-structures-for-approximate-queries
tags: [bloom-filter, hyperloglog, count-min-sketch, probabilistic, memory-efficiency, performance]
symptoms:
  - "Agent loads entire seen-URLs set into memory to check if a URL was already fetched"
  - "Counting distinct users across millions of sessions requires full deduplication"
  - "Frequency estimation for tool call arguments requires maintaining exact counter maps"
  - "Memory grows unboundedly as the agent processes more unique items over its lifetime"
  - "Cache miss avoidance requires a full key lookup when a probabilistic pre-check would suffice"
---

## Why This Happens

Exact data structures (sets, dicts, sorted lists) require memory proportional to the number of unique items they track. For agents that process millions of tool call results, URL fetches, or session events, this memory grows without bound. Probabilistic data structures trade a small, configurable error probability for constant or sub-linear memory usage. A Bloom filter answers "was this URL seen before?" with zero false negatives and tunable false positives in a fixed-size bit array. HyperLogLog estimates distinct count within 2% error using kilobytes instead of gigabytes. Count-Min Sketch tracks top-frequency items in sub-linear space.

## Solution 1: Bloom Filter

```python
import hashlib
import math
from dataclasses import dataclass, field
from typing import List, Optional

class BloomFilter:
    """
    Space-efficient probabilistic set membership test.
    No false negatives: if is_member() returns False, the item was never added.
    Tunable false positive rate: lower rate = more bits required.
    Memory: ~9.6 bits per item at 1% false positive rate.
    """

    def __init__(
        self,
        expected_items: int,
        false_positive_rate: float = 0.01,
    ):
        self._n = expected_items
        self._p = false_positive_rate
        # Optimal bit array size and hash function count
        self._m = self._optimal_m(expected_items, false_positive_rate)
        self._k = self._optimal_k(self._m, expected_items)
        self._bits = bytearray(math.ceil(self._m / 8))
        self._count = 0

    @staticmethod
    def _optimal_m(n: int, p: float) -> int:
        return int(-n * math.log(p) / (math.log(2) ** 2))

    @staticmethod
    def _optimal_k(m: int, n: int) -> int:
        return max(1, int((m / n) * math.log(2)))

    def _hash_positions(self, item: str) -> List[int]:
        positions = []
        for i in range(self._k):
            h = hashlib.sha256(f"{i}:{item}".encode()).digest()
            pos = int.from_bytes(h[:4], "big") % self._m
            positions.append(pos)
        return positions

    def add(self, item: str) -> None:
        for pos in self._hash_positions(item):
            byte_idx = pos // 8
            bit_idx = pos % 8
            self._bits[byte_idx] |= (1 << bit_idx)
        self._count += 1

    def is_member(self, item: str) -> bool:
        """Returns True if item was probably added. Never returns false negatives."""
        for pos in self._hash_positions(item):
            byte_idx = pos // 8
            bit_idx = pos % 8
            if not (self._bits[byte_idx] & (1 << bit_idx)):
                return False
        return True

    @property
    def estimated_fill_rate(self) -> float:
        set_bits = sum(bin(b).count("1") for b in self._bits)
        return set_bits / self._m

    def stats(self) -> dict:
        return {
            "capacity": self._n,
            "items_added": self._count,
            "bit_array_bytes": len(self._bits),
            "hash_functions": self._k,
            "estimated_false_positive_rate": round(
                (1 - math.e ** (-self._k * self._count / self._m)) ** self._k, 6
            ),
            "fill_rate": round(self.estimated_fill_rate, 4),
        }
```

## Solution 2: HyperLogLog Cardinality Estimator

```python
import hashlib
import math
from typing import Optional

class HyperLogLog:
    """
    Estimates the number of distinct elements with ~2% error using
    sub-linear memory. Uses b-bit buckets to track maximum leading zeros
    in hash values — the core HyperLogLog algorithm.
    Memory: 2^b bytes where b is the precision parameter (default b=14 = 16KB).
    """

    def __init__(self, precision: int = 14):
        """precision (b): 4–16. Higher = more accurate, more memory."""
        if not 4 <= precision <= 16:
            raise ValueError("precision must be between 4 and 16")
        self._b = precision
        self._m = 1 << precision   # number of registers = 2^b
        self._registers = bytearray(self._m)
        self._alpha = self._get_alpha(self._m)

    @staticmethod
    def _get_alpha(m: int) -> float:
        if m == 16:
            return 0.673
        if m == 32:
            return 0.697
        if m == 64:
            return 0.709
        return 0.7213 / (1.0 + 1.079 / m)

    def _hash(self, item: str) -> int:
        return int.from_bytes(hashlib.sha256(item.encode()).digest()[:8], "big")

    def add(self, item: str) -> None:
        h = self._hash(item)
        bucket = h >> (64 - self._b)
        w = h & ((1 << (64 - self._b)) - 1)
        leading_zeros = (64 - self._b) - w.bit_length() + 1 if w else (64 - self._b + 1)
        self._registers[bucket] = max(self._registers[bucket], leading_zeros)

    def estimate_cardinality(self) -> int:
        raw = self._alpha * self._m ** 2 / sum(2.0 ** -r for r in self._registers)
        # Small range correction
        if raw <= 2.5 * self._m:
            zeros = self._registers.count(0)
            if zeros > 0:
                return int(self._m * math.log(self._m / zeros))
        return int(raw)

    def merge(self, other: "HyperLogLog") -> "HyperLogLog":
        if self._b != other._b:
            raise ValueError("cannot merge HyperLogLogs with different precision")
        merged = HyperLogLog(self._b)
        for i in range(self._m):
            merged._registers[i] = max(self._registers[i], other._registers[i])
        return merged

    def stats(self) -> dict:
        return {
            "precision": self._b,
            "registers": self._m,
            "memory_bytes": self._m,
            "estimated_cardinality": self.estimate_cardinality(),
            "relative_error": round(1.04 / math.sqrt(self._m), 4),
        }
```

## Solution 3: Count-Min Sketch

```python
import hashlib
import math
from typing import List, Optional

class CountMinSketch:
    """
    Probabilistic frequency estimation for streaming data.
    Answers "how many times was item X seen?" with over-count error bounded by epsilon.
    Memory: width × depth counters (typically ~50KB for epsilon=0.01, delta=0.01).
    Never under-counts: actual_count <= estimated_count <= actual_count + epsilon * total.
    """

    def __init__(
        self,
        epsilon: float = 0.01,   # error bound as fraction of total count
        delta: float = 0.01,     # probability of exceeding error bound
    ):
        self._width = math.ceil(math.e / epsilon)
        self._depth = math.ceil(math.log(1 / delta))
        self._counters = [[0] * self._width for _ in range(self._depth)]
        self._total = 0

    def _hash(self, item: str, row: int) -> int:
        h = hashlib.sha256(f"{row}:{item}".encode()).digest()
        return int.from_bytes(h[:4], "big") % self._width

    def add(self, item: str, count: int = 1) -> None:
        for row in range(self._depth):
            col = self._hash(item, row)
            self._counters[row][col] += count
        self._total += count

    def estimate(self, item: str) -> int:
        """Returns an upper-bound estimate of item's frequency."""
        return min(
            self._counters[row][self._hash(item, row)]
            for row in range(self._depth)
        )

    def heavy_hitters(self, threshold_fraction: float = 0.01) -> List[tuple]:
        """
        Cannot directly enumerate heavy hitters — caller must track candidates.
        Returns threshold count for reference.
        """
        threshold = int(self._total * threshold_fraction)
        return [("threshold", threshold, "total", self._total)]

    def stats(self) -> dict:
        return {
            "width": self._width,
            "depth": self._depth,
            "total_counted": self._total,
            "memory_counters": self._width * self._depth,
        }
```

## Solution 4: Approximate Membership Cache

```python
import time
from dataclasses import dataclass, field
from typing import Any, Optional

@dataclass
class ApproximateCacheStats:
    bloom_hits: int = 0      # bloom said "not member" — skip backend lookup
    bloom_misses: int = 0    # bloom said "maybe member" — check backend
    true_hits: int = 0       # backend confirmed membership
    false_positives: int = 0 # bloom said yes, backend said no

class ApproximateMembershipCache:
    """
    Two-tier membership check: Bloom filter as fast pre-check,
    exact backend as fallback for bloom positives.
    Skips backend lookup entirely for definitive bloom negatives.
    Useful for URL deduplication, seen-item tracking, cache miss avoidance.
    """

    def __init__(
        self,
        bloom: BloomFilter,
        exact_backend: Any,   # anything with .contains(item) -> bool
    ):
        self._bloom = bloom
        self._backend = exact_backend
        self._stats = ApproximateCacheStats()

    def is_member(self, item: str) -> bool:
        if not self._bloom.is_member(item):
            self._stats.bloom_hits += 1
            return False   # definitive: not a member

        self._stats.bloom_misses += 1
        exact = self._backend.contains(item)
        if exact:
            self._stats.true_hits += 1
        else:
            self._stats.false_positives += 1
        return exact

    def add(self, item: str) -> None:
        self._bloom.add(item)
        self._backend.add(item)

    def stats(self) -> dict:
        total = self._stats.bloom_hits + self._stats.bloom_misses
        return {
            "bloom_bypass_rate": round(self._stats.bloom_hits / max(total, 1), 4),
            "actual_false_positive_rate": round(
                self._stats.false_positives / max(self._stats.bloom_misses, 1), 6
            ),
            "bloom": self._bloom.stats(),
        }
```

## Solution 5: Sliding Window HyperLogLog

```python
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Optional

@dataclass
class WindowedHLLSnapshot:
    hll: HyperLogLog
    window_start: float
    window_end: float

class SlidingWindowHyperLogLog:
    """
    Estimates distinct count within a sliding time window.
    Maintains N HyperLogLog sketches, one per time bucket.
    On query, merges sketches within the window for an estimate.
    """

    def __init__(
        self,
        window_seconds: float = 3600.0,
        bucket_seconds: float = 60.0,
        precision: int = 12,
    ):
        self._window = window_seconds
        self._bucket = bucket_seconds
        self._precision = precision
        self._buckets: Deque[WindowedHLLSnapshot] = deque(
            maxlen=int(window_seconds / bucket_seconds) + 1
        )
        self._current: Optional[WindowedHLLSnapshot] = None

    def _get_or_create_current(self) -> WindowedHLLSnapshot:
        now = time.time()
        bucket_start = (now // self._bucket) * self._bucket
        if self._current and self._current.window_start == bucket_start:
            return self._current
        snap = WindowedHLLSnapshot(
            hll=HyperLogLog(self._precision),
            window_start=bucket_start,
            window_end=bucket_start + self._bucket,
        )
        if self._current:
            self._buckets.append(self._current)
        self._current = snap
        return snap

    def add(self, item: str) -> None:
        self._get_or_create_current().hll.add(item)

    def estimate(self) -> int:
        now = time.time()
        cutoff = now - self._window
        relevant = [
            snap for snap in self._buckets
            if snap.window_end >= cutoff
        ]
        if self._current:
            relevant.append(self._current)
        if not relevant:
            return 0
        merged = relevant[0].hll
        for snap in relevant[1:]:
            merged = merged.merge(snap.hll)
        return merged.estimate_cardinality()
```

## Solution 6: Probabilistic Query Router

```python
from dataclasses import dataclass
from typing import Any, Callable, Optional

class ProbabilisticQueryRouter:
    """
    Routes queries to exact or approximate backends based on acceptable error.
    Membership queries: Bloom filter -> exact fallback.
    Cardinality queries: HyperLogLog (fast) or exact count (slow).
    Frequency queries: Count-Min Sketch (fast) or exact counter (slow).
    """

    def __init__(
        self,
        bloom: BloomFilter,
        hll: HyperLogLog,
        cms: CountMinSketch,
        exact_membership_fn: Callable[[str], bool],
        exact_cardinality_fn: Callable[[], int],
        exact_frequency_fn: Callable[[str], int],
    ):
        self._bloom = bloom
        self._hll = hll
        self._cms = cms
        self._exact_membership = exact_membership_fn
        self._exact_cardinality = exact_cardinality_fn
        self._exact_frequency = exact_frequency_fn

    def membership(self, item: str, allow_approximate: bool = True) -> bool:
        if not allow_approximate:
            return self._exact_membership(item)
        if not self._bloom.is_member(item):
            return False
        return self._exact_membership(item)

    def cardinality(self, error_tolerance: float = 0.05) -> int:
        if error_tolerance >= 0.02:
            return self._hll.estimate_cardinality()
        return self._exact_cardinality()

    def frequency(self, item: str, error_tolerance: float = 0.01) -> int:
        if error_tolerance >= 0.01:
            return self._cms.estimate(item)
        return self._exact_frequency(item)

    def memory_summary(self) -> dict:
        return {
            "bloom_bytes": self._bloom.stats()["bit_array_bytes"],
            "hll_bytes": self._hll.stats()["memory_bytes"],
            "cms_counters": self._cms.stats()["memory_counters"],
        }
```

## Comparison

| Approach | Use Case | Error Type | Memory |
|---|---|---|---|
| BloomFilter | Membership testing | False positives only | ~10 bits/item at 1% FP |
| HyperLogLog | Distinct count | ~2% relative error | 2^b bytes (16KB at b=14) |
| CountMinSketch | Frequency estimation | Over-count bounded by ε×N | width × depth counters |
| ApproximateMembershipCache | Cache miss avoidance | Via Bloom FP rate | Bloom + exact backend |
| SlidingWindowHyperLogLog | Time-windowed distinct count | ~2% per window | N × 2^b bytes |
| ProbabilisticQueryRouter | Query routing | Configurable per query | Combined |

**Best for production**: Use `BloomFilter` for URL/item deduplication in crawling or fetching pipelines — a 10M-item filter with 1% FP rate uses ~12MB instead of hundreds of MB for a Python set. Use `HyperLogLog` for "how many distinct users/sessions/queries" metrics — exact distinct counts on millions of items are prohibitively expensive. Use `CountMinSketch` to track which tool arguments are most frequently used without unbounded counter growth. Size the structures at startup based on expected item count over the agent's operational lifetime.
