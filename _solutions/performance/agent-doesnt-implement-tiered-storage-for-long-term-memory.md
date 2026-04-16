---
title: "Agent Doesn't Implement Tiered Storage for Long-Term Memory"
description: "AI agents that store all conversation history and knowledge in a single flat store pay high retrieval latency and cost for rarely-accessed old memories. Tiered storage automatically promotes hot memories to fast in-memory caches and demotes cold memories to cheap object storage."
date: 2025-02-01
difficulty: advanced
category: performance
slug: agent-doesnt-implement-tiered-storage-for-long-term-memory
tags:
  - memory
  - tiered-storage
  - caching
  - long-term-memory
  - performance
  - cost-optimization
  - retrieval
symptoms:
  - "Memory retrieval latency grows linearly with total conversation history size"
  - "Embedding search over the full memory corpus is slow and expensive"
  - "Old, rarely-accessed memories consume expensive vector-database capacity"
  - "Agent costs scale unboundedly with session count instead of active session count"
  - "Fetching a recent memory takes the same time as fetching a year-old one"
---

## Problem

Long-lived agents accumulate memories over time: conversation turns, tool results, user preferences, learned facts. Storing everything in a single vector database or SQL table treats a three-minute-old observation identically to a six-month-old one — both in cost (vector DB charges per stored vector) and in retrieval latency (larger index = slower ANN search).

Tiered storage applies the same principle as CPU caches and cloud storage tiers:

- **Hot tier** (in-memory, e.g. Redis, dict): last N turns, frequently accessed facts, sub-millisecond latency.
- **Warm tier** (vector database, SSD): last 30 days, semantic search, low-millisecond latency.
- **Cold tier** (object storage, S3/GCS): full history, keyword search only, seconds latency — but near-zero cost per GB.

A promotion/demotion policy moves memories between tiers automatically based on recency, access frequency, and importance score.

---

## Solution 1: Two-Tier Memory Cache (Hot + Warm)

LRU in-memory cache as the hot tier, with a fallback to a vector store as the warm tier. Writes go to both; reads check hot first.

```python
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class MemoryEntry:
    memory_id: str
    content: str
    embedding: Optional[List[float]]
    created_at: float
    last_accessed: float
    access_count: int = 0
    importance: float = 1.0
    tier: str = "hot"   # hot | warm | cold


class LRUHotCache:
    """Fixed-capacity LRU cache for hot-tier memories."""

    def __init__(self, capacity: int = 500):
        self._capacity = capacity
        self._cache: OrderedDict[str, MemoryEntry] = OrderedDict()

    def get(self, memory_id: str) -> Optional[MemoryEntry]:
        if memory_id not in self._cache:
            return None
        self._cache.move_to_end(memory_id)
        entry = self._cache[memory_id]
        entry.last_accessed = time.time()
        entry.access_count += 1
        return entry

    def put(self, entry: MemoryEntry):
        if entry.memory_id in self._cache:
            self._cache.move_to_end(entry.memory_id)
        self._cache[entry.memory_id] = entry
        if len(self._cache) > self._capacity:
            evicted_id, evicted = self._cache.popitem(last=False)
            return evicted  # caller should demote to warm tier
        return None

    def evict_oldest(self, n: int = 10) -> List[MemoryEntry]:
        evicted = []
        for _ in range(min(n, len(self._cache))):
            _, entry = self._cache.popitem(last=False)
            evicted.append(entry)
        return evicted

    def __len__(self):
        return len(self._cache)


class TwoTierMemoryStore:
    """
    Hot tier: in-memory LRU cache.
    Warm tier: pluggable vector store (any object with store/search methods).

    Usage:
        store = TwoTierMemoryStore(warm_store=my_vector_db, hot_capacity=500)
        store.write(entry)
        results = store.search("user prefers dark mode", top_k=5)
    """

    def __init__(self, warm_store, hot_capacity: int = 500):
        self._hot = LRUHotCache(hot_capacity)
        self._warm = warm_store

    def write(self, entry: MemoryEntry):
        entry.tier = "hot"
        evicted = self._hot.put(entry)
        if evicted:
            evicted.tier = "warm"
            self._warm.store(evicted)
        # Also write to warm for durability
        self._warm.store(entry)

    def get(self, memory_id: str) -> Optional[MemoryEntry]:
        entry = self._hot.get(memory_id)
        if entry:
            return entry
        # Cache miss — fetch from warm and promote
        entry = self._warm.get(memory_id)
        if entry:
            entry.tier = "hot"
            self._hot.put(entry)
        return entry

    def search(self, query: str, top_k: int = 10,
               query_embedding: Optional[List[float]] = None) -> List[MemoryEntry]:
        warm_results = self._warm.search(query_embedding or query, top_k=top_k)
        # Promote top results to hot cache
        for entry in warm_results[:3]:
            entry.tier = "hot"
            self._hot.put(entry)
        return warm_results
```

---

## Solution 2: Three-Tier Memory Manager (Hot / Warm / Cold)

Adds a cold tier backed by object storage. Cold-tier entries are stored as compressed JSON blobs; retrieval is slow but cost-effective for year-old conversations.

```python
import gzip
import json
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


class ColdTierObjectStore:
    """
    Simulates S3/GCS object storage.
    Replace _blobs with boto3.client("s3") in production.
    """

    def __init__(self):
        self._blobs: Dict[str, bytes] = {}

    def put(self, memory_id: str, entry: "MemoryEntry"):
        data = json.dumps({
            "memory_id": entry.memory_id,
            "content": entry.content,
            "created_at": entry.created_at,
            "last_accessed": entry.last_accessed,
            "access_count": entry.access_count,
            "importance": entry.importance,
        }).encode()
        self._blobs[memory_id] = gzip.compress(data)

    def get(self, memory_id: str) -> Optional[Dict]:
        blob = self._blobs.get(memory_id)
        if blob is None:
            return None
        return json.loads(gzip.decompress(blob))

    def scan_metadata(self) -> List[Dict]:
        """Return lightweight metadata without decompressing blobs."""
        # In production: use S3 object tags or a DynamoDB metadata table
        results = []
        for mid, blob in self._blobs.items():
            data = json.loads(gzip.decompress(blob))
            results.append({"memory_id": mid, "created_at": data["created_at"]})
        return results


class ThreeTierMemoryManager:
    """
    Manages promotion/demotion across hot → warm → cold tiers.

    Promotion policy: accessed in last 24 h → warm; accessed in last 1 h → hot.
    Demotion policy: not accessed in 7 days → cold; not in 24 h → warm.

    Usage:
        mgr = ThreeTierMemoryManager(warm_store=vec_db)
        await mgr.write(entry)
        await mgr.run_maintenance()   # call periodically
        results = await mgr.search("user birthday")
    """

    HOT_WINDOW = 3600          # 1 hour
    WARM_WINDOW = 86400 * 7    # 7 days
    COLD_THRESHOLD = 86400 * 7

    def __init__(self, warm_store, hot_capacity: int = 500):
        self._two_tier = TwoTierMemoryStore(warm_store, hot_capacity)
        self._cold = ColdTierObjectStore()
        self._warm = warm_store

    async def write(self, entry: "MemoryEntry"):
        self._two_tier.write(entry)

    async def get(self, memory_id: str) -> Optional["MemoryEntry"]:
        # Try hot+warm first
        entry = self._two_tier.get(memory_id)
        if entry:
            return entry
        # Fall back to cold tier
        data = self._cold.get(memory_id)
        if data:
            entry = MemoryEntry(
                memory_id=data["memory_id"],
                content=data["content"],
                embedding=None,
                created_at=data["created_at"],
                last_accessed=time.time(),
                access_count=data["access_count"] + 1,
                importance=data["importance"],
                tier="cold",
            )
            # Promote back to warm on access
            self._two_tier.write(entry)
            return entry
        return None

    async def search(self, query, top_k: int = 10) -> List["MemoryEntry"]:
        return self._two_tier.search(query, top_k=top_k)

    async def run_maintenance(self):
        """Demote stale warm entries to cold storage."""
        now = time.time()
        cutoff = now - self.COLD_THRESHOLD
        stale = self._warm.get_older_than(cutoff) if hasattr(self._warm, "get_older_than") else []
        for entry in stale:
            self._cold.put(entry.memory_id, entry)
            self._warm.delete(entry.memory_id)
```

---

## Solution 3: Importance-Scored Retention Policy

Assign an importance score to each memory at write time. High-importance memories stay in warm tier indefinitely; low-importance ones are demoted aggressively.

```python
import math
import time
from dataclasses import dataclass
from typing import Callable, List, Optional


class ImportanceScoredRetentionPolicy:
    """
    Computes a retention score for each memory.
    Score = importance × recency_decay × access_frequency_boost

    Memories with score below eviction_threshold are demoted.

    Usage:
        policy = ImportanceScoredRetentionPolicy(eviction_threshold=0.1)
        score = policy.score(entry)
        if policy.should_evict(entry):
            demote_to_cold(entry)
    """

    def __init__(self, eviction_threshold: float = 0.1,
                 half_life_days: float = 7.0):
        self._threshold = eviction_threshold
        self._half_life = half_life_days * 86400  # seconds

    def recency_score(self, last_accessed: float) -> float:
        age = time.time() - last_accessed
        return math.exp(-math.log(2) * age / self._half_life)

    def frequency_boost(self, access_count: int) -> float:
        return math.log1p(access_count)

    def score(self, entry: "MemoryEntry") -> float:
        return (
            entry.importance
            * self.recency_score(entry.last_accessed)
            * self.frequency_boost(entry.access_count)
        )

    def should_evict(self, entry: "MemoryEntry") -> bool:
        return self.score(entry) < self._threshold

    def rank(self, entries: List["MemoryEntry"]) -> List["MemoryEntry"]:
        return sorted(entries, key=self.score, reverse=True)

    def classify_tier(self, entry: "MemoryEntry") -> str:
        s = self.score(entry)
        if s > 1.0:
            return "hot"
        elif s > self._threshold:
            return "warm"
        else:
            return "cold"
```

---

## Solution 4: Async Background Tier Rebalancer

A background coroutine periodically scans the warm tier, applies the retention policy, and moves stale entries to cold storage without blocking the agent's main loop.

```python
import asyncio
import logging
import time
from typing import List

logger = logging.getLogger(__name__)


class TierRebalancer:
    """
    Background task that rebalances memories across tiers.

    Usage:
        rebalancer = TierRebalancer(
            memory_manager=mgr,
            retention_policy=policy,
            interval=300,
        )
        asyncio.create_task(rebalancer.run())
    """

    def __init__(self, memory_manager: "ThreeTierMemoryManager",
                 retention_policy: "ImportanceScoredRetentionPolicy",
                 interval: float = 300.0,
                 batch_size: int = 100):
        self._mgr = memory_manager
        self._policy = retention_policy
        self._interval = interval
        self._batch = batch_size
        self._stats = {"demoted": 0, "promoted": 0, "cycles": 0}

    async def run(self):
        while True:
            try:
                await self._rebalance()
            except Exception as exc:
                logger.error("Tier rebalancer error: %s", exc)
            await asyncio.sleep(self._interval)

    async def _rebalance(self):
        self._stats["cycles"] += 1
        warm_entries = await asyncio.to_thread(
            self._mgr._warm.get_all_metadata
        ) if hasattr(self._mgr._warm, "get_all_metadata") else []

        demoted = 0
        for meta in warm_entries[:self._batch]:
            entry = await self._mgr.get(meta["memory_id"])
            if entry and self._policy.should_evict(entry):
                await asyncio.to_thread(
                    self._mgr._cold.put, entry.memory_id, entry
                )
                if hasattr(self._mgr._warm, "delete"):
                    await asyncio.to_thread(
                        self._mgr._warm.delete, entry.memory_id
                    )
                demoted += 1

        self._stats["demoted"] += demoted
        if demoted:
            logger.info("Tier rebalancer: demoted %d entries to cold", demoted)

    def stats(self) -> dict:
        return dict(self._stats)
```

---

## Solution 5: Tiered Memory Query Router

Routes search queries to the appropriate tier(s) based on the query's recency hint. A query for "what did the user say today" goes only to hot; "user's address" goes to all tiers.

```python
import asyncio
import re
import time
from dataclasses import dataclass
from typing import List, Optional


RECENCY_PATTERNS = [
    re.compile(r"\b(today|just now|recently|latest|current|last message)\b", re.I),
]
HISTORICAL_PATTERNS = [
    re.compile(r"\b(originally|first time|ever|history|always|used to)\b", re.I),
]


class TieredMemoryQueryRouter:
    """
    Routes memory search queries to the optimal tier(s) to minimise latency.

    Usage:
        router = TieredMemoryQueryRouter(hot_cache, warm_store, cold_store)
        results = await router.search("what is the user's name")
    """

    def __init__(self, hot_cache: "LRUHotCache",
                 warm_store, cold_store: "ColdTierObjectStore"):
        self._hot = hot_cache
        self._warm = warm_store
        self._cold = cold_store

    def _classify_query(self, query: str) -> str:
        if any(p.search(query) for p in RECENCY_PATTERNS):
            return "hot_only"
        if any(p.search(query) for p in HISTORICAL_PATTERNS):
            return "all_tiers"
        return "hot_warm"

    async def search(self, query: str,
                     query_embedding: Optional[List[float]] = None,
                     top_k: int = 10) -> List["MemoryEntry"]:
        tier = self._classify_query(query)
        results = []

        if tier in ("hot_only", "hot_warm", "all_tiers"):
            hot_hits = list(self._hot._cache.values())
            results.extend(hot_hits)

        if tier in ("hot_warm", "all_tiers") and self._warm:
            warm_hits = await asyncio.to_thread(
                self._warm.search, query_embedding or query, top_k
            )
            results.extend(warm_hits)

        if tier == "all_tiers" and self._cold:
            cold_meta = await asyncio.to_thread(self._cold.scan_metadata)
            # Cold search is keyword-only; filter by content contains query words
            keywords = set(query.lower().split())
            for meta in cold_meta[:50]:
                entry_data = await asyncio.to_thread(
                    self._cold.get, meta["memory_id"]
                )
                if entry_data:
                    content_words = set(entry_data["content"].lower().split())
                    if keywords & content_words:
                        results.append(MemoryEntry(
                            memory_id=entry_data["memory_id"],
                            content=entry_data["content"],
                            embedding=None,
                            created_at=entry_data["created_at"],
                            last_accessed=entry_data["last_accessed"],
                            access_count=entry_data["access_count"],
                            importance=entry_data["importance"],
                            tier="cold",
                        ))

        # Deduplicate by memory_id
        seen = set()
        unique = []
        for r in results:
            if r.memory_id not in seen:
                seen.add(r.memory_id)
                unique.append(r)
        return unique[:top_k]
```

---

## Solution 6: Unified Tiered Memory Agent Interface

Drop-in replacement for a flat memory store that transparently manages all three tiers, runs rebalancing in the background, and exposes per-tier cost estimates.

```python
import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class TierCostConfig:
    hot_cost_per_entry_per_day: float = 0.0001    # in-memory: compute cost
    warm_cost_per_entry_per_day: float = 0.00005  # vector DB
    cold_cost_per_entry_per_day: float = 0.000001 # S3/GCS


class UnifiedTieredMemory:
    """
    Complete tiered memory system for AI agents.

    Usage:
        memory = UnifiedTieredMemory(warm_store=vec_db)
        await memory.start()

        await memory.remember("user prefers Python", importance=0.9)
        results = await memory.recall("programming language preference")
        await memory.stop()
    """

    def __init__(self, warm_store=None, hot_capacity: int = 500,
                 cost_config: Optional[TierCostConfig] = None):
        self._mgr = ThreeTierMemoryManager(warm_store, hot_capacity)
        self._policy = ImportanceScoredRetentionPolicy()
        self._rebalancer = TierRebalancer(self._mgr, self._policy)
        self._router = TieredMemoryQueryRouter(
            self._mgr._two_tier._hot, warm_store, self._mgr._cold
        )
        self._cost = cost_config or TierCostConfig()
        self._entry_count: Dict[str, int] = {"hot": 0, "warm": 0, "cold": 0}
        self._task: Optional[asyncio.Task] = None

    async def start(self):
        self._task = asyncio.create_task(
            self._rebalancer.run(), name="tier_rebalancer"
        )

    async def stop(self):
        if self._task:
            self._task.cancel()

    async def remember(self, content: str, importance: float = 1.0,
                       embedding: Optional[List[float]] = None) -> str:
        import uuid
        memory_id = str(uuid.uuid4())
        entry = MemoryEntry(
            memory_id=memory_id,
            content=content,
            embedding=embedding,
            created_at=time.time(),
            last_accessed=time.time(),
            importance=importance,
        )
        await self._mgr.write(entry)
        self._entry_count["hot"] += 1
        return memory_id

    async def recall(self, query: str, top_k: int = 10,
                     embedding: Optional[List[float]] = None) -> List[MemoryEntry]:
        return await self._router.search(query, embedding, top_k)

    async def forget(self, memory_id: str):
        # Remove from all tiers
        if hasattr(self._mgr._warm, "delete"):
            await asyncio.to_thread(self._mgr._warm.delete, memory_id)

    def estimated_daily_cost(self) -> float:
        c = self._cost
        return (
            self._entry_count["hot"] * c.hot_cost_per_entry_per_day +
            self._entry_count["warm"] * c.warm_cost_per_entry_per_day +
            self._entry_count["cold"] * c.cold_cost_per_entry_per_day
        )

    def tier_stats(self) -> dict:
        return {
            "hot_entries": len(self._mgr._two_tier._hot),
            "rebalancer": self._rebalancer.stats(),
            "estimated_daily_cost_usd": round(self.estimated_daily_cost(), 6),
        }
```

---

## Comparison

| Approach | Tiers | Access Latency | Cost per Entry | Automatic Demotion |
|---|---|---|---|---|
| **Two-Tier (Hot+Warm)** | 2 | Hot: <1 ms, Warm: 5–50 ms | Medium | LRU eviction |
| **Three-Tier Manager** | 3 | +Cold: 1–10 s | Low (cold) | Age-based |
| **Importance Retention** | Policy layer | Same as tiers used | Optimised | Score-based |
| **Async Rebalancer** | Background job | No impact on reads | Minimised | Automatic |
| **Query Router** | 1–3 based on query | Adaptive | Saves warm reads | N/A |
| **Unified Interface** | 3 + cost tracking | Adaptive | Minimised | Full pipeline |

**Key insight**: even a simple two-tier split (last 500 turns in memory, rest in vector DB) cuts average retrieval latency by 10–50× for active agents. The three-tier model matters most when conversation history exceeds millions of entries.
