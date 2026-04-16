---
title: "Agent Doesn't Implement Tombstone-Based Deletion for Distributed State"
description: "Agents that delete state with immediate hard deletes in distributed systems create inconsistency: a delete on replica A may not reach replica B before B reads the deleted key and treats it as valid. Implement tombstone-based deletion to mark deleted keys with a timestamp marker, propagate tombstones to all replicas, and purge them only after a configurable retention window — enabling conflict-free distributed deletion."
date: 2026-04-16
difficulty: advanced
category: reliability
slug: agent-doesnt-implement-tombstone-based-deletion-for-distributed-state
tags: [tombstone, distributed-state, soft-delete, consistency, eventual-consistency, conflict-resolution]
symptoms:
  - "Deleted agent memory resurfaces on page reload because a stale replica served the read"
  - "Race condition: delete and re-create of the same key results in the old value winning"
  - "Hard deletes on one node not propagated to all replicas before next read"
  - "No way to distinguish 'key never existed' from 'key was deleted' in distributed logs"
  - "Compaction removes tombstones before slow replicas have processed the deletion"
---

## Why This Happens

In distributed systems, deleting a key by simply removing it from a store creates a resurrection problem: if a replica that hasn't received the delete serves a read, the deleted value appears valid. Tombstones solve this by replacing deletion with a special marker (`{key, deleted: true, timestamp}`) that propagates through the same replication path as creates and updates. When two replicas reconcile, the tombstone wins over any earlier write — the key stays deleted. Tombstones are only physically purged (garbage collected) after a retention window long enough to guarantee all replicas have processed them.

## Solution 1: Tombstone Record

```python
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

@dataclass
class TombstoneRecord:
    key: str
    deleted_at: float = field(default_factory=time.time)
    deleted_by: str = ""           # agent or session that issued the delete
    causation_id: str = ""         # ID of the operation that caused the delete
    retention_seconds: float = 86400.0   # keep tombstone for 24h before purge
    purged: bool = False

    def is_purgeable(self) -> bool:
        return time.time() - self.deleted_at > self.retention_seconds

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "deleted": True,
            "deleted_at": self.deleted_at,
            "deleted_by": self.deleted_by,
            "causation_id": self.causation_id,
        }

@dataclass
class StateEntry:
    key: str
    value: Any
    version: int = 0
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    tombstone: Optional[TombstoneRecord] = None

    @property
    def is_deleted(self) -> bool:
        return self.tombstone is not None

    def effective_timestamp(self) -> float:
        if self.tombstone:
            return self.tombstone.deleted_at
        return self.updated_at
```

## Solution 2: Tombstone-Aware State Store

```python
import asyncio
import time
from typing import Any, Dict, Iterator, List, Optional, Tuple

class TombstoneAwareStateStore:
    """
    State store where deletes create tombstones instead of removing entries.
    Reads check tombstone status before returning values.
    Supports tombstone-first conflict resolution: if two replicas disagree,
    the one with a more recent tombstone wins.
    """

    def __init__(self, retention_seconds: float = 86400.0):
        self._entries: Dict[str, StateEntry] = {}
        self._retention = retention_seconds
        self._lock = asyncio.Lock()
        self._delete_count = 0
        self._purge_count = 0

    async def get(self, key: str) -> Optional[Any]:
        async with self._lock:
            entry = self._entries.get(key)
            if entry is None or entry.is_deleted:
                return None
            return entry.value

    async def set(
        self,
        key: str,
        value: Any,
        updated_by: str = "",
    ) -> StateEntry:
        async with self._lock:
            existing = self._entries.get(key)
            if existing and existing.is_deleted:
                # Resurrect: new write overrides tombstone
                # But only if the new write is more recent than the tombstone
                if time.time() > existing.tombstone.deleted_at:
                    entry = StateEntry(
                        key=key,
                        value=value,
                        version=(existing.version + 1),
                    )
                    self._entries[key] = entry
                    return entry
            elif existing:
                existing.value = value
                existing.version += 1
                existing.updated_at = time.time()
                return existing
            else:
                entry = StateEntry(key=key, value=value)
                self._entries[key] = entry
                return entry

    async def delete(
        self,
        key: str,
        deleted_by: str = "",
        causation_id: str = "",
    ) -> Optional[TombstoneRecord]:
        async with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            if entry.is_deleted:
                return entry.tombstone   # already deleted

            tombstone = TombstoneRecord(
                key=key,
                deleted_by=deleted_by,
                causation_id=causation_id,
                retention_seconds=self._retention,
            )
            entry.tombstone = tombstone
            entry.version += 1
            self._delete_count += 1
            return tombstone

    async def merge_tombstone(self, tombstone: TombstoneRecord) -> bool:
        """
        Apply a tombstone received from another replica.
        Returns True if the tombstone was applied (it was newer than local state).
        """
        async with self._lock:
            entry = self._entries.get(tombstone.key)
            if entry is None:
                # Create a deleted entry for the tombstone
                entry = StateEntry(
                    key=tombstone.key,
                    value=None,
                    tombstone=tombstone,
                )
                self._entries[tombstone.key] = entry
                return True

            # Tombstone wins if it's more recent than the last update
            if tombstone.deleted_at >= entry.effective_timestamp():
                entry.tombstone = tombstone
                entry.version += 1
                return True
        return False

    async def purge_expired_tombstones(self) -> int:
        async with self._lock:
            purgeable = [
                key for key, entry in self._entries.items()
                if entry.is_deleted and entry.tombstone.is_purgeable()
            ]
            for key in purgeable:
                self._entries[key].tombstone.purged = True
                del self._entries[key]
            self._purge_count += len(purgeable)
        return len(purgeable)

    def tombstone_count(self) -> int:
        return sum(1 for e in self._entries.values() if e.is_deleted)

    def stats(self) -> dict:
        live = sum(1 for e in self._entries.values() if not e.is_deleted)
        return {
            "live_entries": live,
            "tombstoned_entries": self.tombstone_count(),
            "total_deletes": self._delete_count,
            "total_purges": self._purge_count,
        }
```

## Solution 3: Tombstone Replicator

```python
import asyncio
import time
from typing import Callable, Dict, List, Optional

class TombstoneReplicator:
    """
    Propagates tombstones to all peer replicas.
    Tracks pending tombstones for replicas that were unreachable.
    On reconnect, sends all pending tombstones to catch up.
    """

    def __init__(self, local_store: TombstoneAwareStateStore):
        self._store = local_store
        self._peers: Dict[str, Callable] = {}   # peer_id -> async send function
        self._pending: Dict[str, List[TombstoneRecord]] = {}   # peer_id -> queue
        self._replicated_count = 0
        self._failed_count = 0

    def register_peer(
        self,
        peer_id: str,
        send_fn: Callable[[TombstoneRecord], None],
    ) -> None:
        self._peers[peer_id] = send_fn
        self._pending[peer_id] = []

    async def broadcast_tombstone(self, tombstone: TombstoneRecord) -> Dict[str, bool]:
        results = {}
        for peer_id, send_fn in self._peers.items():
            try:
                await send_fn(tombstone)
                results[peer_id] = True
                self._replicated_count += 1
            except Exception as exc:
                self._pending[peer_id].append(tombstone)
                results[peer_id] = False
                self._failed_count += 1
        return results

    async def sync_pending(self, peer_id: str) -> int:
        """Replay pending tombstones to a peer that was offline."""
        pending = self._pending.get(peer_id, [])
        if not pending:
            return 0
        send_fn = self._peers.get(peer_id)
        if not send_fn:
            return 0

        replayed = 0
        remaining = []
        for tombstone in pending:
            try:
                await send_fn(tombstone)
                replayed += 1
            except Exception:
                remaining.append(tombstone)

        self._pending[peer_id] = remaining
        return replayed

    def stats(self) -> dict:
        return {
            "total_replicated": self._replicated_count,
            "total_failed": self._failed_count,
            "pending_by_peer": {k: len(v) for k, v in self._pending.items()},
        }
```

## Solution 4: Tombstone Conflict Resolver

```python
import time
from dataclasses import dataclass
from typing import Any, List, Optional, Tuple

@dataclass
class ConflictResolution:
    key: str
    winner: str      # "local" | "remote" | "neither"
    applied: bool
    reason: str

class TombstoneConflictResolver:
    """
    Resolves conflicts between tombstones and live values during replication.
    Uses last-write-wins (LWW) with tombstone-bias: tombstone wins over
    a write at the same timestamp (prevents resurrection race conditions).
    """

    def resolve(
        self,
        key: str,
        local_entry: Optional[StateEntry],
        remote_entry: Optional[StateEntry],
    ) -> Tuple[StateEntry, ConflictResolution]:
        if local_entry is None and remote_entry is None:
            raise ValueError(f"both entries are None for key '{key}'")

        if local_entry is None:
            return remote_entry, ConflictResolution(key, "remote", True, "no_local")
        if remote_entry is None:
            return local_entry, ConflictResolution(key, "local", True, "no_remote")

        local_ts = local_entry.effective_timestamp()
        remote_ts = remote_entry.effective_timestamp()

        # Tombstone bias: tombstone wins at equal timestamps
        if local_entry.is_deleted and not remote_entry.is_deleted:
            if local_ts >= remote_ts:
                return local_entry, ConflictResolution(
                    key, "local", True, "tombstone_bias"
                )
        elif remote_entry.is_deleted and not local_entry.is_deleted:
            if remote_ts >= local_ts:
                return remote_entry, ConflictResolution(
                    key, "remote", True, "tombstone_bias"
                )

        # Last write wins
        if local_ts >= remote_ts:
            return local_entry, ConflictResolution(key, "local", True, "lww_local")
        return remote_entry, ConflictResolution(key, "remote", True, "lww_remote")
```

## Solution 5: Tombstone Retention Policy

```python
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

@dataclass
class RetentionPolicy:
    default_retention_seconds: float = 86400.0   # 24 hours
    per_namespace_retention: Dict[str, float] = None   # namespace -> seconds

    def __post_init__(self):
        if self.per_namespace_retention is None:
            self.per_namespace_retention = {}

    def retention_for(self, key: str) -> float:
        for namespace, retention in self.per_namespace_retention.items():
            if key.startswith(namespace):
                return retention
        return self.default_retention_seconds

class TombstoneRetentionManager:
    """
    Manages tombstone retention lifecycle.
    Ensures tombstones are kept long enough for slow replicas to process them
    but purged promptly enough to avoid unbounded storage growth.
    """

    def __init__(
        self,
        store: TombstoneAwareStateStore,
        policy: RetentionPolicy,
        min_replica_lag_seconds: float = 3600.0,
    ):
        self._store = store
        self._policy = policy
        self._min_lag = min_replica_lag_seconds

    def effective_retention(self, key: str) -> float:
        policy_retention = self._policy.retention_for(key)
        return max(policy_retention, self._min_lag * 2)

    async def run_gc_cycle(self) -> dict:
        """
        Garbage collect tombstones that have exceeded their retention period.
        Returns counts of tombstones eligible, purged, and retained.
        """
        live_tombstones = [
            entry for entry in self._store._entries.values()
            if entry.is_deleted and not entry.tombstone.purged
        ]
        eligible = [
            e for e in live_tombstones
            if e.tombstone.is_purgeable()
        ]
        purged = await self._store.purge_expired_tombstones()
        return {
            "total_tombstones": len(live_tombstones),
            "eligible_for_purge": len(eligible),
            "purged": purged,
            "retained": len(live_tombstones) - purged,
        }
```

## Solution 6: Tombstone Health Monitor

```python
import time
from typing import Dict

class TombstoneHealthMonitor:
    """
    Monitors tombstone accumulation and replication lag.
    Alerts when tombstone count exceeds thresholds or replication falls behind.
    """

    def __init__(
        self,
        store: TombstoneAwareStateStore,
        replicator: TombstoneReplicator,
        max_tombstone_ratio: float = 0.3,
    ):
        self._store = store
        self._replicator = replicator
        self._max_ratio = max_tombstone_ratio

    def check(self) -> dict:
        store_stats = self._store.stats()
        rep_stats = self._replicator.stats()
        total = store_stats["live_entries"] + store_stats["tombstoned_entries"]
        tombstone_ratio = store_stats["tombstoned_entries"] / max(total, 1)

        alerts = []
        if tombstone_ratio > self._max_ratio:
            alerts.append({
                "type": "high_tombstone_ratio",
                "ratio": round(tombstone_ratio, 4),
                "threshold": self._max_ratio,
                "recommendation": "run GC cycle or reduce retention period",
            })

        pending_total = sum(rep_stats["pending_by_peer"].values())
        if pending_total > 100:
            alerts.append({
                "type": "replication_lag",
                "total_pending": pending_total,
                "by_peer": rep_stats["pending_by_peer"],
                "recommendation": "trigger sync_pending() for lagging peers",
            })

        return {
            "healthy": len(alerts) == 0,
            "alerts": alerts,
            "store": store_stats,
            "replication": rep_stats,
            "tombstone_ratio": round(tombstone_ratio, 4),
        }
```

## Comparison

| Approach | Soft Delete | Conflict Resolution | Replication | GC |
|---|---|---|---|---|
| TombstoneAwareStateStore | Yes | Merge (timestamp) | No | Yes (purge_expired) |
| TombstoneReplicator | No | No | Yes (broadcast) | No |
| TombstoneConflictResolver | No | Yes (LWW + bias) | No | No |
| TombstoneRetentionManager | No | No | No | Yes (policy-based) |
| TombstoneHealthMonitor | No | No | No | No (alerts only) |

**Best for production**: Replace all hard deletes in shared agent state with `TombstoneAwareStateStore.delete()`. Set retention to at least 2× the maximum expected replica lag — typically 24–48 hours for geographically distributed replicas. Use `TombstoneReplicator` to propagate deletes across replicas immediately and buffer for offline replicas. Run `TombstoneRetentionManager.run_gc_cycle()` on a hourly schedule. Monitor `TombstoneHealthMonitor.check()` — tombstone ratios above 30% indicate deletes are outpacing GC or retention is too long.
