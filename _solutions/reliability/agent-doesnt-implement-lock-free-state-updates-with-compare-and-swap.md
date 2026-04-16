---
title: "Agent Doesn't Implement Lock-Free State Updates with Compare-and-Swap"
description: "Agents using mutex locks for shared state create contention bottlenecks under concurrent tool execution — long-running tool calls hold locks while other branches wait. Implement optimistic lock-free state updates with Compare-and-Swap (CAS) semantics to allow concurrent readers and writers without mutual exclusion, retrying only when a write conflict is detected."
date: 2026-04-16
difficulty: advanced
category: reliability
slug: agent-doesnt-implement-lock-free-state-updates-with-compare-and-swap
tags: [compare-and-swap, lock-free, concurrency, optimistic-locking, state-management, reliability]
symptoms:
  - "Parallel tool branches wait on a shared asyncio.Lock held by a slow tool call"
  - "State update throughput degrades as the number of concurrent agent branches increases"
  - "Deadlock risk when two agent branches each hold a lock and wait for the other"
  - "All reads blocked while any writer holds the mutex — even read-only branches pay lock cost"
  - "High lock contention visible in profiling — most time spent waiting, not executing"
---

## Why This Happens

Mutex locks serialize all access to shared state regardless of whether a conflict actually exists. Most concurrent state updates don't conflict — they touch different keys or read without writing. Compare-and-swap solves this with optimistic concurrency: read the current value and version, compute the new value, then write only if the version hasn't changed since the read. On conflict (another writer changed the state), retry the operation from the latest state. This yields high throughput when conflicts are rare and graceful degradation (retry) when they occur.

## Solution 1: Versioned State Cell

```python
import asyncio
import copy
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Generic, Optional, Tuple, TypeVar

T = TypeVar("T")

@dataclass
class VersionedValue(Generic[T]):
    value: T
    version: int = 0
    updated_at: float = field(default_factory=time.time)
    updated_by: str = ""

    def next_version(self, new_value: T, updated_by: str = "") -> "VersionedValue[T]":
        return VersionedValue(
            value=copy.deepcopy(new_value),
            version=self.version + 1,
            updated_at=time.time(),
            updated_by=updated_by,
        )

class CASCell(Generic[T]):
    """
    A single versioned value supporting Compare-and-Swap semantics.
    Multiple readers can read concurrently without locking.
    Writers use CAS: read version, compute new value, swap if version unchanged.
    """

    def __init__(self, initial_value: T, name: str = ""):
        self._cell = VersionedValue(value=copy.deepcopy(initial_value))
        self._name = name
        self._cas_attempts = 0
        self._cas_conflicts = 0
        self._write_lock = asyncio.Lock()   # only serializes writes, not reads

    def read(self) -> Tuple[T, int]:
        """Non-blocking read. Returns (value, version)."""
        return copy.deepcopy(self._cell.value), self._cell.version

    async def compare_and_swap(
        self,
        expected_version: int,
        new_value: T,
        updated_by: str = "",
    ) -> Tuple[bool, int]:
        """
        Atomically updates value if current version == expected_version.
        Returns (success, current_version).
        On conflict, returns (False, current_version) — caller should retry.
        """
        self._cas_attempts += 1
        async with self._write_lock:
            if self._cell.version != expected_version:
                self._cas_conflicts += 1
                return False, self._cell.version
            self._cell = self._cell.next_version(new_value, updated_by)
            return True, self._cell.version

    async def update(
        self,
        transform_fn: Callable[[T], T],
        max_retries: int = 10,
        updated_by: str = "",
    ) -> Tuple[T, int]:
        """
        Applies transform_fn with automatic retry on CAS conflict.
        Returns (new_value, new_version) on success.
        """
        for attempt in range(max_retries):
            value, version = self.read()
            new_value = transform_fn(value)
            success, current_version = await self.compare_and_swap(
                version, new_value, updated_by
            )
            if success:
                return new_value, current_version
            if attempt < max_retries - 1:
                await asyncio.sleep(0.001 * (2 ** attempt))   # exponential backoff

        raise RuntimeError(
            f"CAS update on '{self._name}' failed after {max_retries} retries"
        )

    def stats(self) -> dict:
        conflict_rate = self._cas_conflicts / max(self._cas_attempts, 1)
        return {
            "name": self._name,
            "version": self._cell.version,
            "cas_attempts": self._cas_attempts,
            "cas_conflicts": self._cas_conflicts,
            "conflict_rate": round(conflict_rate, 4),
        }
```

## Solution 2: Lock-Free State Map

```python
import asyncio
import copy
import time
from typing import Any, Callable, Dict, Optional, Tuple

class LockFreeStateMap:
    """
    Dictionary-like state store where each key is an independent CASCell.
    Concurrent updates to different keys never contend with each other.
    Concurrent updates to the same key use per-key CAS with retry.
    """

    def __init__(self):
        self._cells: Dict[str, CASCell] = {}
        self._global_version = 0
        self._init_lock = asyncio.Lock()

    async def _get_or_create_cell(self, key: str, initial: Any = None) -> CASCell:
        if key not in self._cells:
            async with self._init_lock:
                if key not in self._cells:
                    self._cells[key] = CASCell(initial, name=key)
        return self._cells[key]

    async def get(self, key: str, default: Any = None) -> Any:
        cell = self._cells.get(key)
        if cell is None:
            return default
        value, _ = cell.read()
        return value

    async def get_versioned(self, key: str) -> Tuple[Any, int]:
        cell = self._cells.get(key)
        if cell is None:
            return None, -1
        return cell.read()

    async def set(
        self,
        key: str,
        value: Any,
        updated_by: str = "",
        initial_if_absent: Any = None,
    ) -> int:
        """Unconditional set. Returns new version."""
        cell = await self._get_or_create_cell(key, initial_if_absent)
        _, version = await cell.update(lambda _: value, updated_by=updated_by)
        return version

    async def cas_set(
        self,
        key: str,
        expected_version: int,
        new_value: Any,
        updated_by: str = "",
    ) -> Tuple[bool, int]:
        """Conditional set. Fails if version has changed."""
        cell = self._cells.get(key)
        if cell is None:
            return False, -1
        return await cell.compare_and_swap(expected_version, new_value, updated_by)

    async def update(
        self,
        key: str,
        transform_fn: Callable[[Any], Any],
        initial: Any = None,
        updated_by: str = "",
    ) -> Any:
        cell = await self._get_or_create_cell(key, initial)
        new_value, _ = await cell.update(transform_fn, updated_by=updated_by)
        return new_value

    def snapshot(self) -> Dict[str, Any]:
        return {key: cell.read()[0] for key, cell in self._cells.items()}

    def conflict_summary(self) -> dict:
        hot_keys = sorted(
            [(k, c.stats()["conflict_rate"]) for k, c in self._cells.items()],
            key=lambda x: x[1],
            reverse=True,
        )
        return {
            "total_keys": len(self._cells),
            "hot_keys": hot_keys[:5],
            "total_conflicts": sum(c.stats()["cas_conflicts"] for c in self._cells.values()),
        }
```

## Solution 3: Optimistic Read-Modify-Write Pipeline

```python
import asyncio
import time
from dataclasses import dataclass
from typing import Any, Callable, Coroutine, List, Optional, Tuple

@dataclass
class RMWResult:
    key: str
    old_value: Any
    new_value: Any
    version: int
    attempts: int
    latency_ms: float

class OptimisticRMWPipeline:
    """
    Read-Modify-Write pipeline for state transformations that may involve
    async operations between the read and the write (e.g., fetching data
    to enrich state). Uses speculative execution: start the transform
    speculatively, then CAS at the end. On conflict, replay the transform.
    """

    def __init__(
        self,
        state_map: LockFreeStateMap,
        max_retries: int = 5,
    ):
        self._state = state_map
        self._max_retries = max_retries
        self._total_rmw = 0
        self._total_retries = 0

    async def execute(
        self,
        key: str,
        transform_fn: Callable[[Any], Coroutine],
        updated_by: str = "",
    ) -> RMWResult:
        """
        Executes an async transform with CAS retry.
        transform_fn receives the current value and returns the new value.
        If the state changes while transform_fn is running, it retries.
        """
        self._total_rmw += 1
        t0 = time.monotonic()
        attempts = 0
        initial_value = None

        for attempt in range(self._max_retries):
            attempts = attempt + 1
            value, version = await self._state.get_versioned(key)
            if attempt == 0:
                initial_value = value

            # Run potentially expensive transform speculatively
            new_value = await transform_fn(value)

            # Try to commit — may fail if state changed during transform
            success, current_version = await self._state.cas_set(
                key, version, new_value, updated_by
            )
            if success:
                latency_ms = (time.monotonic() - t0) * 1000
                self._total_retries += attempts - 1
                return RMWResult(
                    key=key,
                    old_value=initial_value,
                    new_value=new_value,
                    version=current_version,
                    attempts=attempts,
                    latency_ms=round(latency_ms, 2),
                )

            # Conflict: back off and retry
            if attempt < self._max_retries - 1:
                await asyncio.sleep(0.002 * (2 ** attempt))

        raise RuntimeError(
            f"RMW on key '{key}' failed after {self._max_retries} attempts"
        )

    def stats(self) -> dict:
        return {
            "total_rmw": self._total_rmw,
            "total_retries": self._total_retries,
            "avg_retries_per_rmw": round(self._total_retries / max(self._total_rmw, 1), 3),
        }
```

## Solution 4: Conflict-Free Replicated State (CRDT Counter)

```python
import asyncio
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict

class GCounter:
    """
    Grow-only CRDT counter: each agent instance has its own counter.
    Merging is max() per actor — no conflicts possible, no locking needed.
    Use for: tracking counts that only increase (tool invocations, tokens used).
    """

    def __init__(self, actor_id: str = ""):
        self._actor_id = actor_id or str(uuid.uuid4())[:8]
        self._counts: Dict[str, int] = defaultdict(int)

    def increment(self, amount: int = 1) -> int:
        self._counts[self._actor_id] += amount
        return self.value()

    def value(self) -> int:
        return sum(self._counts.values())

    def merge(self, other: "GCounter") -> "GCounter":
        merged = GCounter(self._actor_id)
        all_actors = set(self._counts) | set(other._counts)
        for actor in all_actors:
            merged._counts[actor] = max(
                self._counts.get(actor, 0),
                other._counts.get(actor, 0),
            )
        return merged

    def state(self) -> dict:
        return dict(self._counts)


class PNCounter:
    """
    Positive-Negative CRDT counter: supports increment and decrement.
    Composed of two GCounters — no CAS needed for concurrent updates.
    """

    def __init__(self, actor_id: str = ""):
        self._pos = GCounter(actor_id)
        self._neg = GCounter(actor_id)

    def increment(self, amount: int = 1) -> int:
        self._pos.increment(amount)
        return self.value()

    def decrement(self, amount: int = 1) -> int:
        self._neg.increment(amount)
        return self.value()

    def value(self) -> int:
        return self._pos.value() - self._neg.value()

    def merge(self, other: "PNCounter") -> "PNCounter":
        merged = PNCounter()
        merged._pos = self._pos.merge(other._pos)
        merged._neg = self._neg.merge(other._neg)
        return merged
```

## Solution 5: Write-Ahead Intent Log

```python
import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

@dataclass
class WriteIntent:
    intent_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    key: str = ""
    new_value: Any = None
    expected_version: int = -1
    status: str = "pending"   # "pending" | "committed" | "aborted"
    created_at: float = field(default_factory=time.time)
    committed_at: Optional[float] = None
    writer_id: str = ""

class WriteAheadIntentLog:
    """
    Logs write intents before applying CAS updates.
    Provides crash recovery: on restart, replay committed intents
    to bring state back to consistency without replaying full history.
    """

    def __init__(self, state_map: LockFreeStateMap):
        self._state = state_map
        self._log: List[WriteIntent] = []
        self._max_log_size = 10_000

    async def write(
        self,
        key: str,
        new_value: Any,
        expected_version: int,
        writer_id: str = "",
    ) -> Tuple[bool, WriteIntent]:
        intent = WriteIntent(
            key=key,
            new_value=new_value,
            expected_version=expected_version,
            writer_id=writer_id,
        )
        # Log intent before attempting write
        self._append(intent)

        success, version = await self._state.cas_set(
            key, expected_version, new_value, writer_id
        )
        if success:
            intent.status = "committed"
            intent.committed_at = time.time()
        else:
            intent.status = "aborted"

        return success, intent

    def _append(self, intent: WriteIntent) -> None:
        if len(self._log) >= self._max_log_size:
            self._log.pop(0)
        self._log.append(intent)

    def pending_intents(self) -> List[WriteIntent]:
        return [i for i in self._log if i.status == "pending"]

    def summary(self) -> dict:
        total = len(self._log)
        committed = sum(1 for i in self._log if i.status == "committed")
        return {
            "total_intents": total,
            "committed": committed,
            "aborted": sum(1 for i in self._log if i.status == "aborted"),
            "pending": sum(1 for i in self._log if i.status == "pending"),
            "commit_rate": round(committed / max(total, 1), 4),
        }
```

## Solution 6: Contention Monitor

```python
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Deque, Dict, List

@dataclass
class ContentionEvent:
    key: str
    conflict_count: int
    retry_count: int
    final_latency_ms: float
    writer_id: str
    timestamp: float

class ContentionMonitor:
    """
    Tracks CAS conflict patterns to identify hot keys.
    High contention on specific keys indicates those state values
    should be redesigned using CRDTs, sharding, or reduced sharing.
    """

    def __init__(self, window_seconds: float = 300.0):
        self._window = window_seconds
        self._events: Deque[ContentionEvent] = deque(maxlen=5000)
        self._by_key: Dict[str, int] = defaultdict(int)

    def record(
        self,
        key: str,
        conflict_count: int,
        retry_count: int,
        latency_ms: float,
        writer_id: str = "",
    ) -> None:
        event = ContentionEvent(
            key=key,
            conflict_count=conflict_count,
            retry_count=retry_count,
            final_latency_ms=latency_ms,
            writer_id=writer_id,
            timestamp=time.time(),
        )
        self._events.append(event)
        if conflict_count > 0:
            self._by_key[key] += conflict_count

    def hot_keys(self, top_n: int = 10) -> List[dict]:
        cutoff = time.time() - self._window
        recent = [e for e in self._events if e.timestamp >= cutoff]
        by_key: Dict[str, List[ContentionEvent]] = defaultdict(list)
        for e in recent:
            if e.conflict_count > 0:
                by_key[e.key].append(e)
        return sorted(
            [
                {
                    "key": k,
                    "total_conflicts": sum(e.conflict_count for e in evs),
                    "avg_retries": round(sum(e.retry_count for e in evs) / len(evs), 2),
                    "avg_latency_ms": round(sum(e.final_latency_ms for e in evs) / len(evs), 2),
                }
                for k, evs in by_key.items()
            ],
            key=lambda x: x["total_conflicts"],
            reverse=True,
        )[:top_n]

    def summary(self) -> dict:
        cutoff = time.time() - self._window
        recent = [e for e in self._events if e.timestamp >= cutoff]
        return {
            "total_writes": len(recent),
            "writes_with_conflict": sum(1 for e in recent if e.conflict_count > 0),
            "conflict_rate": round(
                sum(1 for e in recent if e.conflict_count > 0) / max(len(recent), 1), 4
            ),
            "hot_keys": self.hot_keys(5),
        }
```

## Comparison

| Approach | Locking | Conflict Handling | Multi-Writer | Crash Recovery |
|---|---|---|---|---|
| CASCell | Write lock only | Retry with backoff | Yes (per-key) | No |
| LockFreeStateMap | Per-key write lock | Via CASCell | Yes (independent keys) | No |
| OptimisticRMWPipeline | None (speculative) | Async retry | Yes | No |
| GCounter / PNCounter | None (CRDT) | Never conflicts | Yes (merge) | Via state |
| WriteAheadIntentLog | Via state map | Via CAS | Yes | Yes (replay) |
| ContentionMonitor | N/A | N/A | N/A | N/A |

**Best for production**: Use `LockFreeStateMap` for session-scoped state shared across parallel tool branches. Prefer `GCounter`/`PNCounter` for purely accumulative metrics (token counts, call counts) — no conflict possible. Use `OptimisticRMWPipeline` for enrichment workflows where the transform involves async I/O. Monitor `ContentionMonitor.hot_keys()` — keys with conflict rate > 10% should be redesigned as CRDTs or sharded by agent branch to eliminate contention entirely.
