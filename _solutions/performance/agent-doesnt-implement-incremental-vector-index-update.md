---
title: "Agent Doesn't Implement Incremental Vector Index Update"
description: "Agents that rebuild their entire vector index whenever new documents arrive pay O(n) rebuild cost every time — a corpus of 100k documents takes minutes to re-index for a single new file. Implement incremental index updates that insert, update, and delete individual vectors without full rebuilds, track index dirty state for partial refresh, and merge small delta indexes into the main index during off-peak windows."
date: 2026-04-16
difficulty: advanced
category: performance
slug: agent-doesnt-implement-incremental-vector-index-update
tags: [vector-index, incremental-update, rag, embedding, index-management, delta-index]
symptoms:
  - "Adding one document triggers a full corpus re-embedding and index rebuild taking minutes"
  - "Vector search quality degrades because the index is never refreshed between batch rebuilds"
  - "Deleted documents still appear in search results — removals require a full rebuild"
  - "Index rebuild blocks vector search during rebuild — agent is unavailable for queries"
  - "No way to add documents to the live index without a restart"
---

## Why This Happens

Simple vector store implementations rebuild the entire index on every write: gather all embeddings, fit the ANN index, swap. For small corpora this is acceptable, but it scales as O(n log n) for most ANN indexes. Incremental updates require maintaining a secondary "delta index" for new and updated vectors, serving queries against both the main and delta indexes, and periodically merging the delta into the main index. Deletions use a tombstone set: deleted IDs are filtered from results at query time until the next merge removes them from the main index.

## Solution 1: Vector Record

```python
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class VectorRecordState(str, Enum):
    ACTIVE = "active"
    DELETED = "deleted"
    PENDING_MERGE = "pending_merge"


@dataclass
class VectorRecord:
    vector_id: str
    document_id: str
    chunk_index: int
    embedding: List[float]
    dimensions: int
    state: VectorRecordState = VectorRecordState.ACTIVE
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def mark_deleted(self) -> None:
        self.state = VectorRecordState.DELETED
        self.updated_at = time.time()
```

## Solution 2: Delta Index

```python
import time
from typing import Dict, List, Optional, Set, Tuple


class DeltaVectorIndex:
    """
    In-memory delta index for recently added or updated vectors.
    Serves queries via brute-force cosine similarity (acceptable for small deltas).
    Accumulates changes until merged into the main index.
    """

    def __init__(self, max_size: int = 10_000):
        self._vectors: Dict[str, VectorRecord] = {}
        self._tombstones: Set[str] = set()
        self._max_size = max_size
        self._insert_count = 0
        self._delete_count = 0

    def insert(self, record: VectorRecord) -> None:
        self._vectors[record.vector_id] = record
        self._tombstones.discard(record.vector_id)
        self._insert_count += 1

    def delete(self, vector_id: str) -> None:
        self._vectors.pop(vector_id, None)
        self._tombstones.add(vector_id)
        self._delete_count += 1

    def is_full(self) -> bool:
        return len(self._vectors) >= self._max_size

    def search(
        self,
        query_vector: List[float],
        top_k: int = 10,
    ) -> List[Tuple[str, float]]:
        """Returns (vector_id, similarity) sorted by descending similarity."""
        import math

        def cosine(a: List[float], b: List[float]) -> float:
            dot = sum(x * y for x, y in zip(a, b))
            mag_a = math.sqrt(sum(x * x for x in a))
            mag_b = math.sqrt(sum(x * x for x in b))
            return dot / (mag_a * mag_b + 1e-9)

        active = [
            (vid, cosine(query_vector, rec.embedding))
            for vid, rec in self._vectors.items()
            if rec.state == VectorRecordState.ACTIVE
        ]
        active.sort(key=lambda x: -x[1])
        return active[:top_k]

    def drain(self) -> Tuple[List[VectorRecord], Set[str]]:
        """Return all records and tombstones, then clear."""
        records = list(self._vectors.values())
        tombstones = set(self._tombstones)
        self._vectors.clear()
        self._tombstones.clear()
        self._insert_count = 0
        self._delete_count = 0
        return records, tombstones

    def stats(self) -> dict:
        return {
            "active_vectors": len(self._vectors),
            "tombstones": len(self._tombstones),
            "total_inserts": self._insert_count,
            "total_deletes": self._delete_count,
            "is_full": self.is_full(),
        }
```

## Solution 3: Main Vector Index

```python
import math
import time
from typing import Dict, List, Optional, Set, Tuple


class MainVectorIndex:
    """
    Persistent main index for the full corpus.
    In production, back this with FAISS, Hnswlib, or a vector DB.
    Here implemented with a flat in-memory store for clarity.
    Supports batch insert from delta merge and tombstone filtering.
    """

    def __init__(self):
        self._vectors: Dict[str, VectorRecord] = {}
        self._tombstones: Set[str] = set()
        self._last_merge_at: float = 0.0
        self._total_merges: int = 0

    def bulk_insert(self, records: List[VectorRecord]) -> int:
        inserted = 0
        for rec in records:
            if rec.state == VectorRecordState.ACTIVE:
                self._vectors[rec.vector_id] = rec
                self._tombstones.discard(rec.vector_id)
                inserted += 1
        return inserted

    def apply_tombstones(self, tombstones: Set[str]) -> int:
        removed = 0
        for vid in tombstones:
            if self._vectors.pop(vid, None):
                removed += 1
            self._tombstones.add(vid)
        return removed

    def search(
        self,
        query_vector: List[float],
        top_k: int = 10,
        exclude_ids: Optional[Set[str]] = None,
    ) -> List[Tuple[str, float, VectorRecord]]:
        exclude = (exclude_ids or set()) | self._tombstones

        def cosine(a: List[float], b: List[float]) -> float:
            dot = sum(x * y for x, y in zip(a, b))
            mag_a = math.sqrt(sum(x * x for x in a))
            mag_b = math.sqrt(sum(x * x for x in b))
            return dot / (mag_a * mag_b + 1e-9)

        results = [
            (vid, cosine(query_vector, rec.embedding), rec)
            for vid, rec in self._vectors.items()
            if vid not in exclude
        ]
        results.sort(key=lambda x: -x[1])
        return results[:top_k]

    def record_merge(self) -> None:
        self._last_merge_at = time.time()
        self._total_merges += 1

    def stats(self) -> dict:
        return {
            "total_vectors": len(self._vectors),
            "tombstones": len(self._tombstones),
            "last_merge_at": self._last_merge_at,
            "total_merges": self._total_merges,
        }
```

## Solution 4: Incremental Index Manager

```python
import asyncio
import time
from typing import List, Optional, Set, Tuple


class IncrementalVectorIndexManager:
    """
    Coordinates delta and main index for incremental updates.
    Writes go to the delta index; reads query both and merge results.
    Merge runs when the delta is full or on a scheduled interval.
    """

    def __init__(
        self,
        main_index: MainVectorIndex,
        delta_index: DeltaVectorIndex,
        merge_interval_seconds: float = 300.0,
        auto_merge: bool = True,
    ):
        self._main = main_index
        self._delta = delta_index
        self._merge_interval = merge_interval_seconds
        self._auto_merge = auto_merge
        self._merge_task: Optional[asyncio.Task] = None
        self._pending_merge = False

    def insert(self, record: VectorRecord) -> None:
        self._delta.insert(record)
        if self._delta.is_full():
            self._pending_merge = True

    def delete(self, vector_id: str) -> None:
        self._delta.delete(vector_id)
        self._main.apply_tombstones({vector_id})

    def search(
        self,
        query_vector: List[float],
        top_k: int = 10,
    ) -> List[Tuple[str, float]]:
        # Collect tombstones from delta for result filtering
        delta_tombstones = set(self._delta._tombstones)

        # Query both indexes
        main_results = self._main.search(
            query_vector, top_k=top_k * 2, exclude_ids=delta_tombstones
        )
        delta_results = self._delta.search(query_vector, top_k=top_k)

        # Merge and deduplicate by vector_id, keeping highest similarity
        seen: dict = {}
        for vid, score, _ in main_results:
            seen[vid] = score
        for vid, score in delta_results:
            if vid not in seen or score > seen[vid]:
                seen[vid] = score

        merged = sorted(seen.items(), key=lambda x: -x[1])
        return merged[:top_k]

    async def merge_delta(self) -> dict:
        records, tombstones = self._delta.drain()
        inserted = self._main.bulk_insert(records)
        removed = self._main.apply_tombstones(tombstones)
        self._main.record_merge()
        self._pending_merge = False
        return {
            "inserted": inserted,
            "tombstones_applied": removed,
            "delta_records_drained": len(records),
        }

    async def start_auto_merge(self) -> None:
        if self._auto_merge:
            self._merge_task = asyncio.create_task(self._merge_loop())

    async def _merge_loop(self) -> None:
        while True:
            await asyncio.sleep(self._merge_interval)
            if self._pending_merge or self._delta.stats()["active_vectors"] > 0:
                await self.merge_delta()

    def stats(self) -> dict:
        return {
            "main": self._main.stats(),
            "delta": self._delta.stats(),
            "pending_merge": self._pending_merge,
        }
```

## Solution 5: Index Change Log

```python
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import List


class ChangeType(str, Enum):
    INSERT = "insert"
    UPDATE = "update"
    DELETE = "delete"
    MERGE = "merge"


@dataclass
class IndexChangeEvent:
    change_type: ChangeType
    vector_id: str
    document_id: str = ""
    timestamp: float = field(default_factory=time.time)
    merge_stats: dict = field(default_factory=dict)


class IndexChangeLog:
    """
    Append-only log of all index mutations.
    Used for auditing, debugging staleness, and replaying changes to
    a replica index if it falls behind.
    """

    def __init__(self, max_entries: int = 50_000):
        self._log: List[IndexChangeEvent] = []
        self._max = max_entries

    def record(self, event: IndexChangeEvent) -> None:
        if len(self._log) >= self._max:
            self._log.pop(0)
        self._log.append(event)

    def since(self, timestamp: float) -> List[IndexChangeEvent]:
        return [e for e in self._log if e.timestamp >= timestamp]

    def summary(self) -> dict:
        recent = [e for e in self._log if e.timestamp >= time.time() - 3600]
        counts = {ct.value: 0 for ct in ChangeType}
        for e in recent:
            counts[e.change_type.value] += 1
        return {"changes_last_hour": len(recent), "by_type": counts}
```

## Solution 6: Index Health Monitor

```python
import time


class VectorIndexHealthMonitor:
    """
    Monitors delta fullness, time since last merge, and tombstone accumulation.
    Alerts when the delta is backing up or main index has stale tombstones.
    """

    def __init__(
        self,
        manager: IncrementalVectorIndexManager,
        max_delta_age_seconds: float = 600.0,
        max_tombstone_ratio: float = 0.10,
    ):
        self._manager = manager
        self._max_delta_age = max_delta_age_seconds
        self._max_tombstone_ratio = max_tombstone_ratio

    def check(self) -> dict:
        stats = self._manager.stats()
        main = stats["main"]
        delta = stats["delta"]
        alerts = []

        # Check if delta is filling up without merging
        if delta["is_full"]:
            alerts.append({
                "type": "delta_full",
                "recommendation": "trigger merge_delta() immediately",
            })

        # Check tombstone accumulation in main index
        total = main["total_vectors"] + main["tombstones"]
        if total > 0:
            ts_ratio = main["tombstones"] / total
            if ts_ratio > self._max_tombstone_ratio:
                alerts.append({
                    "type": "high_tombstone_ratio",
                    "ratio": round(ts_ratio, 4),
                    "threshold": self._max_tombstone_ratio,
                    "recommendation": "run full compaction to remove tombstoned entries",
                })

        # Check merge staleness
        last_merge = main.get("last_merge_at", 0)
        if last_merge > 0 and time.time() - last_merge > self._max_delta_age:
            alerts.append({
                "type": "stale_merge",
                "age_seconds": round(time.time() - last_merge, 0),
                "threshold": self._max_delta_age,
                "recommendation": "check auto_merge task is running",
            })

        return {
            "generated_at": time.time(),
            "healthy": len(alerts) == 0,
            "main_index_size": main["total_vectors"],
            "delta_size": delta["active_vectors"],
            "alerts": alerts,
        }
```

## Comparison

| Approach | Incremental Insert | Tombstone Delete | Query Both Indexes | Merge | Monitoring |
|---|---|---|---|---|---|
| DeltaVectorIndex | Yes (in-memory) | Yes | No | No (drain only) | No |
| MainVectorIndex | Via bulk_insert | Yes | Yes | Via record_merge | No |
| IncrementalVectorIndexManager | Via delta | Via both | Yes (merged) | Yes (async) | No |
| IndexChangeLog | No | No | No | No | Via log |
| VectorIndexHealthMonitor | No | No | No | No | Yes |

**Best for production**: Set delta `max_size=10_000` and merge on a 5-minute schedule. For FAISS-backed main indexes, replace the flat search in `MainVectorIndex` with `index.search()` — the delta stays as brute-force since it's small. Always apply tombstones to both indexes at delete time, not just at merge time — otherwise deleted documents appear in main-index results until the next merge. Run `VectorIndexHealthMonitor.check()` every minute; a `delta_full` alert means the merge loop is not running or is too slow.
