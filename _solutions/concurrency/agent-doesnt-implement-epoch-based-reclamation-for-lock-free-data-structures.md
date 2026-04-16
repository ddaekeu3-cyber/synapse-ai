---
title: "Agent Doesn't Implement Epoch-Based Reclamation for Lock-Free Data Structures"
description: "AI agents that update shared state concurrently face a memory reclamation hazard: if one coroutine reads a node while another deletes it, a use-after-free occurs. Epoch-based reclamation (EBR) solves this without locks by grouping reclamation into epochs — memory is only freed when all readers have advanced past the epoch in which the deletion occurred."
date: 2025-02-12
difficulty: advanced
category: concurrency
slug: agent-doesnt-implement-epoch-based-reclamation-for-lock-free-data-structures
tags:
  - epoch-based-reclamation
  - ebr
  - lock-free
  - memory-reclamation
  - hazard-pointers
  - concurrency
  - asyncio
  - shared-state
symptoms:
  - "Lock-free cache deletion causes use-after-free when a reader accesses a just-deleted node"
  - "Agent panics with AttributeError on a node that another coroutine simultaneously removed"
  - "Concurrent read + delete on an in-memory graph produces corrupted results intermittently"
  - "Lock-free data structure cannot be safely cleared while readers are active"
  - "Memory of deleted nodes cannot be reclaimed because readers may still hold references"
---

## Problem

Lock-free data structures avoid mutex overhead by using compare-and-swap operations, but they cannot immediately free memory when a node is logically deleted. A concurrent reader may still hold a reference to that node. Without a reclamation protocol, either you leak memory (never free) or cause use-after-free (free immediately). Epoch-Based Reclamation (EBR) groups time into epochs; a thread announces which epoch it is in before reading. Memory deleted in epoch E is only freed after all threads have advanced past epoch E — guaranteed safe reclamation without locks.

---

## Solution 1: EpochManager — Global Epoch Advancement

```python
import asyncio
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set


@dataclass
class EpochRecord:
    """Per-coroutine record of current epoch participation."""
    participant_id: int
    current_epoch: int
    active: bool = False   # True while inside a critical section


class EpochManager:
    """
    Global epoch counter and participant registry for EBR.
    Maintains a global epoch and per-participant epoch records.
    Advances the global epoch when all active participants are current.

    Usage:
        mgr = EpochManager()
        participant_id = mgr.register()

        # Before reading shared data:
        mgr.enter(participant_id)
        value = shared_structure.read(key)
        mgr.exit(participant_id)

        # After deleting a node:
        mgr.retire(node, participant_id)
        mgr.try_advance()   # collect garbage if safe
    """

    def __init__(self):
        self._global_epoch: int = 0
        self._participants: Dict[int, EpochRecord] = {}
        self._retired: List[tuple] = []   # (epoch, obj, finaliser)
        self._next_id = 0
        self._lock = asyncio.Lock()

    def register(self) -> int:
        pid = self._next_id
        self._next_id += 1
        self._participants[pid] = EpochRecord(
            participant_id=pid,
            current_epoch=self._global_epoch,
        )
        return pid

    def enter(self, participant_id: int):
        """Announce participation in current epoch before reading."""
        rec = self._participants[participant_id]
        rec.current_epoch = self._global_epoch
        rec.active = True

    def exit(self, participant_id: int):
        """Signal that this participant has finished reading."""
        self._participants[participant_id].active = False

    def retire(self, obj: Any, finaliser: Optional[Callable] = None):
        """Schedule obj for reclamation after all readers pass current epoch."""
        self._retired.append((self._global_epoch, obj, finaliser))

    def try_advance(self) -> int:
        """
        Advance global epoch if all active participants are in the current epoch.
        Returns number of objects reclaimed.
        """
        active = [r for r in self._participants.values() if r.active]
        if active and any(r.current_epoch < self._global_epoch for r in active):
            return 0
        self._global_epoch += 1
        safe_epoch = self._global_epoch - 2
        to_free = [(e, obj, fn) for e, obj, fn in self._retired
                   if e <= safe_epoch]
        self._retired = [(e, obj, fn) for e, obj, fn in self._retired
                         if e > safe_epoch]
        for _, obj, fn in to_free:
            if fn:
                fn(obj)
        return len(to_free)

    @property
    def global_epoch(self) -> int:
        return self._global_epoch

    def stats(self) -> dict:
        return {
            "global_epoch": self._global_epoch,
            "participants": len(self._participants),
            "retired_pending": len(self._retired),
        }
```

---

## Solution 2: EBRGuard — Context Manager for Safe Critical Sections

```python
import asyncio
from contextlib import asynccontextmanager
from typing import Optional


class EBRGuard:
    """
    Async context manager that enters and exits an EBR critical section.
    Usage:
        guard = EBRGuard(epoch_mgr)
        pid = epoch_mgr.register()

        async with guard.read_section(pid):
            value = lock_free_map.get(key)

        # After the with block, the participant is marked inactive;
        # deleted nodes from this epoch can be reclaimed once all
        # other participants also exit.
    """

    def __init__(self, manager: EpochManager):
        self._mgr = manager

    @asynccontextmanager
    async def read_section(self, participant_id: int):
        self._mgr.enter(participant_id)
        try:
            yield self._mgr.global_epoch
        finally:
            self._mgr.exit(participant_id)
            self._mgr.try_advance()

    @asynccontextmanager
    async def write_section(self, participant_id: int):
        """Write section: enter epoch, do write, retire stale nodes, exit."""
        self._mgr.enter(participant_id)
        retired_nodes = []
        try:
            yield retired_nodes
        finally:
            for node in retired_nodes:
                self._mgr.retire(node)
            self._mgr.exit(participant_id)
            self._mgr.try_advance()
```

---

## Solution 3: EBRLockFreeMap — Epoch-Safe Concurrent Hash Map

A lock-free hash map that uses EBR for safe node deletion. Readers never block; deleted nodes are only freed after all active readers advance.

```python
import asyncio
import threading
from typing import Any, Dict, Generic, Optional, TypeVar

K = TypeVar("K")
V = TypeVar("V")


class EBRNode:
    __slots__ = ("key", "value", "deleted")

    def __init__(self, key, value):
        self.key = key
        self.value = value
        self.deleted = False


class EBRLockFreeMap:
    """
    Concurrent map with epoch-based reclamation.
    Reads are lock-free; writes acquire a per-bucket lock.
    Deleted nodes are scheduled for EBR reclamation.

    Usage:
        mgr = EpochManager()
        guard = EBRGuard(mgr)
        cache = EBRLockFreeMap(mgr, buckets=256)
        pid = mgr.register()

        async with guard.read_section(pid):
            value = cache.get("session-abc")

        async with guard.write_section(pid) as retired:
            old_node = cache.remove("session-old")
            if old_node:
                retired.append(old_node)
    """

    def __init__(self, manager: EpochManager, buckets: int = 256):
        self._mgr = manager
        self._buckets: List[Dict[Any, EBRNode]] = [{} for _ in range(buckets)]
        self._locks = [asyncio.Lock() for _ in range(buckets)]
        self._n_buckets = buckets

    def _bucket(self, key) -> int:
        return hash(key) % self._n_buckets

    def get(self, key) -> Optional[Any]:
        """Lock-free read — must be called inside an EBR read section."""
        b = self._bucket(key)
        node = self._buckets[b].get(key)
        if node is None or node.deleted:
            return None
        return node.value

    async def put(self, key, value):
        b = self._bucket(key)
        async with self._locks[b]:
            self._buckets[b][key] = EBRNode(key, value)

    async def remove(self, key) -> Optional[EBRNode]:
        b = self._bucket(key)
        async with self._locks[b]:
            node = self._buckets[b].pop(key, None)
            if node:
                node.deleted = True
                self._mgr.retire(node)
            return node

    async def update(self, key, fn) -> Optional[Any]:
        b = self._bucket(key)
        async with self._locks[b]:
            node = self._buckets[b].get(key)
            if node is None or node.deleted:
                return None
            new_value = fn(node.value)
            self._buckets[b][key] = EBRNode(key, new_value)
            node.deleted = True
            self._mgr.retire(node)
            return new_value

    def __len__(self) -> int:
        return sum(len(b) for b in self._buckets)
```

---

## Solution 4: HazardPointerSet — Alternative to EBR for Single-Node Protection

For cases where only one node needs protection at a time, hazard pointers are lighter than full EBR: a reader publishes a hazard pointer to a node; the deleter checks all hazard pointers before freeing.

```python
import asyncio
from typing import Any, List, Optional, Set


class HazardPointer:
    """A single slot that holds a reference to a protected node."""
    __slots__ = ("node",)

    def __init__(self):
        self.node: Optional[Any] = None


class HazardPointerSet:
    """
    Global registry of hazard pointers.
    Readers acquire a slot and write the node they're about to read.
    Deleters check all slots before freeing.

    Usage:
        hp_set = HazardPointerSet(max_threads=32)
        hp = hp_set.acquire()

        # Reader:
        hp.node = shared_node_ref       # protect before dereferencing
        value = hp.node.value           # safe to access
        hp.node = None                  # release protection
        hp_set.release(hp)

        # Deleter:
        node.deleted = True
        if hp_set.is_safe_to_free(node):
            del node
        else:
            deferred_free_list.append(node)
    """

    def __init__(self, max_slots: int = 64):
        self._slots: List[HazardPointer] = [HazardPointer() for _ in range(max_slots)]
        self._free: List[HazardPointer] = list(self._slots)
        self._lock = asyncio.Lock()

    async def acquire(self) -> HazardPointer:
        async with self._lock:
            if not self._free:
                raise RuntimeError("No hazard pointer slots available")
            return self._free.pop()

    async def release(self, hp: HazardPointer):
        hp.node = None
        async with self._lock:
            self._free.append(hp)

    def is_safe_to_free(self, node: Any) -> bool:
        return not any(slot.node is node for slot in self._slots)

    def scan_and_reclaim(self, candidates: List[Any],
                          finaliser=None) -> List[Any]:
        """Free nodes not protected by any hazard pointer."""
        protected = {slot.node for slot in self._slots if slot.node is not None}
        freed = []
        deferred = []
        for node in candidates:
            if node not in protected:
                if finaliser:
                    finaliser(node)
                freed.append(node)
            else:
                deferred.append(node)
        return deferred  # still needs retry
```

---

## Solution 5: EBRStatsMixin — Memory Pressure and Epoch Lag Monitoring

```python
import time
from dataclasses import dataclass
from typing import Dict


@dataclass
class EBRHealthSnapshot:
    global_epoch: int
    participants: int
    active_readers: int
    retired_pending: int
    max_participant_lag: int
    reclaimed_total: int
    timestamp: float


class EBRStatsMixin:
    """
    Mixin for EpochManager that adds metrics and alerts.

    Usage:
        class MonitoredEpochManager(EBRStatsMixin, EpochManager): pass
        mgr = MonitoredEpochManager()
        snap = mgr.health_snapshot()
        if snap.max_participant_lag > 10:
            logger.warning("EBR lag: a reader is stuck in an old epoch")
    """

    def __init__(self):
        super().__init__()
        self._reclaimed_total = 0

    def try_advance(self) -> int:
        freed = super().try_advance()
        self._reclaimed_total += freed
        return freed

    def health_snapshot(self) -> EBRHealthSnapshot:
        active = [r for r in self._participants.values() if r.active]
        lag = max(
            (self._global_epoch - r.current_epoch for r in self._participants.values()),
            default=0,
        )
        return EBRHealthSnapshot(
            global_epoch=self._global_epoch,
            participants=len(self._participants),
            active_readers=len(active),
            retired_pending=len(self._retired),
            max_participant_lag=lag,
            reclaimed_total=self._reclaimed_total,
            timestamp=time.time(),
        )

    def is_healthy(self, max_lag: int = 5,
                    max_retired: int = 10_000) -> bool:
        snap = self.health_snapshot()
        return (snap.max_participant_lag <= max_lag and
                snap.retired_pending <= max_retired)
```

---

## Solution 6: EBRAwareAgentStateCache — Production-Ready Integration

A production-ready agent state cache combining EBR-safe reads, write-side retirement, and background epoch advancement.

```python
import asyncio
import time
from typing import Any, Callable, Optional


class EBRAwareAgentStateCache:
    """
    Agent state cache using EBR for safe concurrent read/write.
    Reads are lock-free; deletes use EBR retirement.
    Background task advances epochs every N ms.

    Usage:
        cache = EBRAwareAgentStateCache(advance_interval_ms=100)
        asyncio.create_task(cache.run())

        # From any coroutine:
        pid = cache.register_reader()
        value = await cache.get(pid, "session:abc")
        await cache.put("session:abc", session_obj)
        await cache.delete("session:abc")
    """

    def __init__(self, advance_interval_ms: float = 100.0):
        self._mgr = EpochManager()
        self._guard = EBRGuard(self._mgr)
        self._map = EBRLockFreeMap(self._mgr, buckets=512)
        self._interval = advance_interval_ms / 1000.0

    def register_reader(self) -> int:
        return self._mgr.register()

    async def get(self, participant_id: int, key: str) -> Optional[Any]:
        async with self._guard.read_section(participant_id):
            return self._map.get(key)

    async def put(self, key: str, value: Any):
        await self._map.put(key, value)

    async def delete(self, key: str):
        await self._map.remove(key)

    async def update(self, key: str, fn: Callable) -> Optional[Any]:
        return await self._map.update(key, fn)

    async def run(self):
        while True:
            await asyncio.sleep(self._interval)
            self._mgr.try_advance()

    def health(self) -> dict:
        return self._mgr.stats()
```

---

## Comparison

| Approach | Reclamation | Lock-Free Reads | Memory Overhead | Python Overhead |
|---|---|---|---|---|
| **EpochManager** | Epoch-based | Yes | Low (epoch ints) | Low |
| **EBRGuard** | Epoch-based | Yes | Minimal | Minimal |
| **EBRLockFreeMap** | Epoch-based | Yes | Per-node retired list | Low |
| **HazardPointerSet** | Hazard-pointer | Yes | Per-slot pointer | Very low |
| **EBRStatsMixin** | Epoch-based | Yes | Stats counters | Minimal |
| **EBRAwareAgentStateCache** | Epoch-based | Yes | Full pipeline | Low |

**Key insight**: in CPython, the GIL makes true use-after-free impossible within a single process — but asyncio coroutines yield at `await` points, which can interleave reads and deletes on shared Python objects. EBR prevents the logical use-after-free where a reader accesses an object that has already been semantically invalidated and repurposed. Use `EBRAwareAgentStateCache` as a drop-in for any shared agent state that needs concurrent read access with safe deletes.
