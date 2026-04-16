---
title: "Agent Doesn't Implement Optimistic Concurrency Control for Shared State"
description: "How to prevent lost updates and data corruption when multiple agents or requests concurrently modify the same shared state using optimistic locking, MVCC, CAS operations, and conflict resolution strategies."
date: 2025-01-16
difficulty: advanced
category: concurrency
slug: agent-doesnt-implement-optimistic-concurrency-control-for-shared-state
tags:
  - concurrency
  - optimistic-locking
  - mvcc
  - compare-and-swap
  - conflict-resolution
  - shared-state
  - race-conditions
symptoms:
  - "Multiple agents updating the same record simultaneously with last-write-wins corruption"
  - "Lost updates when concurrent requests modify shared configuration or memory"
  - "Stale reads causing agents to operate on outdated data"
  - "Inconsistent state after parallel tool executions touch overlapping resources"
  - "No version tracking on mutable shared objects"
  - "Retry storms when conflicts are detected but not gracefully resolved"
---

## Why This Happens

AI agents often maintain shared mutable state — conversation memory, tool caches, configuration objects, knowledge bases — and access this state from multiple coroutines, threads, or distributed workers simultaneously. Without concurrency control, two agents can read the same version, independently compute updates, and both write back, with the second write silently overwriting the first. This "lost update" anomaly is especially pernicious because it produces no errors: the system appears healthy while quietly discarding work.

Pessimistic locking (mutexes, database row locks) prevents lost updates but kills throughput when contention is low — which is the common case for agent workloads. Optimistic Concurrency Control (OCC) takes the opposite bet: assume conflicts are rare, proceed without locks, and detect collisions at write time using version numbers or checksums. On conflict, retry or merge rather than abort. This yields near-lock-free read performance while still guaranteeing consistency.

---

## Solution 1: Version-Stamped State with CAS Write

The simplest OCC pattern attaches a monotonically increasing version to every mutable object. Reads return `(value, version)`. Writes include the expected version and are rejected if the stored version has advanced — a Compare-And-Swap (CAS) semantic.

```python
import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Optional, TypeVar, Generic

T = TypeVar("T")

@dataclass
class VersionedValue(Generic[T]):
    value: T
    version: int
    updated_at: float = field(default_factory=time.time)
    updated_by: Optional[str] = None

class VersionConflictError(Exception):
    def __init__(self, key: str, expected: int, actual: int):
        super().__init__(
            f"Version conflict on '{key}': expected {expected}, got {actual}"
        )
        self.key = key
        self.expected = expected
        self.actual = actual

class OptimisticStore:
    """In-memory store with CAS (compare-and-swap) write semantics."""

    def __init__(self):
        self._data: dict[str, VersionedValue] = {}
        self._lock = asyncio.Lock()  # protects internal dict mutations only

    async def get(self, key: str) -> Optional[VersionedValue]:
        return self._data.get(key)

    async def put(
        self,
        key: str,
        value: Any,
        expected_version: int,  # -1 = "must not exist", 0 = "create or replace"
        writer_id: Optional[str] = None,
    ) -> VersionedValue:
        async with self._lock:
            existing = self._data.get(key)

            if expected_version == -1:
                # Caller asserts the key does not exist yet
                if existing is not None:
                    raise VersionConflictError(key, -1, existing.version)
                new_version = 1
            elif expected_version == 0:
                # Upsert — no version check
                new_version = (existing.version + 1) if existing else 1
            else:
                # Strict CAS
                current = existing.version if existing else 0
                if current != expected_version:
                    raise VersionConflictError(key, expected_version, current)
                new_version = expected_version + 1

            updated = VersionedValue(
                value=value,
                version=new_version,
                updated_at=time.time(),
                updated_by=writer_id,
            )
            self._data[key] = updated
            return updated

    async def delete(self, key: str, expected_version: int) -> bool:
        async with self._lock:
            existing = self._data.get(key)
            if existing is None:
                return False
            if existing.version != expected_version:
                raise VersionConflictError(key, expected_version, existing.version)
            del self._data[key]
            return True

# --- Usage ---

async def demo_cas():
    store = OptimisticStore()

    # Initial write
    v = await store.put("agent:memory", {"facts": ["earth is round"]}, expected_version=-1, writer_id="agent-1")
    print(f"Created version {v.version}")

    # Read
    snapshot = await store.get("agent:memory")

    # Simulate concurrent modification — another agent bumps the version
    await store.put("agent:memory", {"facts": ["earth is round", "sky is blue"]}, expected_version=1, writer_id="agent-2")

    # Our stale write should fail
    try:
        await store.put(
            "agent:memory",
            {"facts": ["earth is round", "water is wet"]},
            expected_version=snapshot.version,  # stale!
            writer_id="agent-1",
        )
    except VersionConflictError as e:
        print(f"Conflict detected: {e}")  # -> expected 1, got 2
```

---

## Solution 2: Retry-on-Conflict with Exponential Backoff

Detecting conflicts is only half the solution — the caller must retry with freshly read data. A decorator-based retry wrapper makes this transparent.

```python
import asyncio
import random
import functools
from typing import Callable, TypeVar, Awaitable

F = TypeVar("F", bound=Callable[..., Awaitable])

def optimistic_retry(
    max_attempts: int = 5,
    base_delay: float = 0.05,
    jitter: bool = True,
):
    """Retry an async function on VersionConflictError with exponential backoff."""

    def decorator(fn: F) -> F:
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            delay = base_delay
            for attempt in range(1, max_attempts + 1):
                try:
                    return await fn(*args, **kwargs)
                except VersionConflictError as exc:
                    if attempt == max_attempts:
                        raise RuntimeError(
                            f"Optimistic lock failed after {max_attempts} attempts on '{exc.key}'"
                        ) from exc
                    sleep = delay * (2 ** (attempt - 1))
                    if jitter:
                        sleep *= 0.5 + random.random()
                    await asyncio.sleep(sleep)
        return wrapper  # type: ignore
    return decorator


class AgentMemoryManager:
    def __init__(self, store: OptimisticStore):
        self.store = store

    @optimistic_retry(max_attempts=5)
    async def append_fact(self, agent_id: str, fact: str) -> None:
        """Read-modify-write with automatic conflict retry."""
        key = f"agent:{agent_id}:memory"
        snapshot = await self.store.get(key)

        if snapshot is None:
            new_value = {"facts": [fact]}
            expected = -1  # assert creation
        else:
            new_value = {"facts": snapshot.value["facts"] + [fact]}
            expected = snapshot.version  # CAS on read version

        await self.store.put(key, new_value, expected_version=expected, writer_id=agent_id)

    @optimistic_retry(max_attempts=3)
    async def update_config(self, key: str, patch: dict) -> VersionedValue:
        snapshot = await self.store.get(key)
        if snapshot is None:
            return await self.store.put(key, patch, expected_version=-1)

        merged = {**snapshot.value, **patch}
        return await self.store.put(key, merged, expected_version=snapshot.version)
```

---

## Solution 3: Multi-Version Concurrency Control (MVCC)

MVCC keeps all historical versions, allowing readers to see a consistent snapshot at a specific point in time without blocking writers.

```python
from __future__ import annotations
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Optional

@dataclass
class MVCCVersion:
    version: int
    value: Any
    created_at: float
    deleted: bool = False
    transaction_id: Optional[str] = None

class MVCCStore:
    """
    Simplified MVCC store.
    - Writers append new versions; old versions are retained.
    - Readers can request a snapshot at a specific timestamp.
    - Garbage collection prunes versions older than a retention window.
    """

    def __init__(self, retention_seconds: float = 300.0):
        self._versions: dict[str, list[MVCCVersion]] = defaultdict(list)
        self._retention = retention_seconds
        self._current_version: dict[str, int] = {}

    def write(self, key: str, value: Any, txn_id: Optional[str] = None) -> int:
        versions = self._versions[key]
        next_ver = (self._current_version.get(key, 0)) + 1
        versions.append(MVCCVersion(
            version=next_ver,
            value=value,
            created_at=time.time(),
            transaction_id=txn_id,
        ))
        self._current_version[key] = next_ver
        return next_ver

    def read(self, key: str, as_of_time: Optional[float] = None) -> Optional[MVCCVersion]:
        """
        Read the latest version at or before `as_of_time`.
        If as_of_time is None, returns the current latest.
        """
        versions = self._versions.get(key, [])
        if not versions:
            return None

        if as_of_time is None:
            # Return latest non-deleted version
            for v in reversed(versions):
                if not v.deleted:
                    return v
            return None

        # Return latest version committed before as_of_time
        candidate = None
        for v in versions:
            if v.created_at <= as_of_time and not v.deleted:
                candidate = v
        return candidate

    def read_range(
        self, key: str, from_version: int, to_version: Optional[int] = None
    ) -> list[MVCCVersion]:
        """Return all versions in [from_version, to_version]."""
        versions = self._versions.get(key, [])
        result = []
        for v in versions:
            if v.version < from_version:
                continue
            if to_version is not None and v.version > to_version:
                break
            result.append(v)
        return result

    def delete(self, key: str, txn_id: Optional[str] = None) -> int:
        """Logical delete — inserts a tombstone version."""
        versions = self._versions[key]
        next_ver = (self._current_version.get(key, 0)) + 1
        versions.append(MVCCVersion(
            version=next_ver,
            value=None,
            created_at=time.time(),
            deleted=True,
            transaction_id=txn_id,
        ))
        self._current_version[key] = next_ver
        return next_ver

    def gc(self) -> int:
        """Remove versions older than retention window. Returns pruned count."""
        cutoff = time.time() - self._retention
        pruned = 0
        for key, versions in self._versions.items():
            before = len(versions)
            # Keep at least one version per key
            filtered = [v for v in versions if v.created_at >= cutoff]
            if not filtered and versions:
                filtered = [versions[-1]]  # retain latest
            self._versions[key] = filtered
            pruned += before - len(filtered)
        return pruned

# --- Usage: snapshot isolation across concurrent agents ---

def demo_mvcc():
    store = MVCCStore()
    t0 = time.time()

    store.write("shared_plan", {"step": 1, "status": "pending"})
    snapshot_time = time.time()  # agent-A takes a read snapshot here

    # agent-B advances the plan
    store.write("shared_plan", {"step": 2, "status": "running"})
    store.write("shared_plan", {"step": 3, "status": "done"})

    # agent-A reads its consistent snapshot (sees step=1)
    old = store.read("shared_plan", as_of_time=snapshot_time)
    # agent-C reads current (sees step=3)
    current = store.read("shared_plan")
    print(f"Agent-A snapshot: {old.value}")    # step=1
    print(f"Agent-C current:  {current.value}")  # step=3
```

---

## Solution 4: Conflict Merge Strategies

When two agents modify different fields of the same object, a "conflict" can often be resolved by merging rather than aborting.

```python
from __future__ import annotations
import copy
from enum import Enum, auto
from typing import Any, Callable, Optional

class MergeStrategy(Enum):
    LAST_WRITE_WINS = auto()
    FIRST_WRITE_WINS = auto()
    DEEP_MERGE = auto()
    FIELD_LEVEL_CAS = auto()
    CUSTOM = auto()

class ConflictResolver:
    """
    Resolves OCC write conflicts using pluggable merge strategies.
    """

    def __init__(self, strategy: MergeStrategy = MergeStrategy.DEEP_MERGE,
                 custom_fn: Optional[Callable] = None):
        self.strategy = strategy
        self.custom_fn = custom_fn

    def resolve(
        self,
        base: Any,          # value at the version the writer read
        theirs: Any,        # current stored value (advanced by another writer)
        ours: Any,          # the update we want to apply
    ) -> Any:
        if self.strategy == MergeStrategy.LAST_WRITE_WINS:
            return ours
        elif self.strategy == MergeStrategy.FIRST_WRITE_WINS:
            return theirs
        elif self.strategy == MergeStrategy.DEEP_MERGE:
            return self._deep_merge(base, theirs, ours)
        elif self.strategy == MergeStrategy.FIELD_LEVEL_CAS:
            return self._field_level_cas(base, theirs, ours)
        elif self.strategy == MergeStrategy.CUSTOM:
            if self.custom_fn is None:
                raise ValueError("custom_fn required for CUSTOM strategy")
            return self.custom_fn(base, theirs, ours)
        raise ValueError(f"Unknown strategy: {self.strategy}")

    def _deep_merge(self, base: Any, theirs: Any, ours: Any) -> Any:
        """
        Three-way merge: apply our changes relative to base on top of theirs.
        For dicts, merge recursively. For lists, concatenate unique additions.
        """
        if not isinstance(ours, dict) or not isinstance(theirs, dict):
            return ours  # scalar: take ours

        result = copy.deepcopy(theirs)
        base_dict = base if isinstance(base, dict) else {}

        for key, our_val in ours.items():
            their_val = theirs.get(key)
            base_val = base_dict.get(key)

            if their_val == base_val:
                # They didn't change this field; apply ours
                result[key] = our_val
            elif our_val == base_val:
                # We didn't change this field; keep theirs
                result[key] = their_val
            elif isinstance(our_val, dict) and isinstance(their_val, dict):
                # Both changed a nested dict — recurse
                result[key] = self._deep_merge(base_val or {}, their_val, our_val)
            elif isinstance(our_val, list) and isinstance(their_val, list):
                # Merge lists: union of additions from both sides
                base_set = set(map(str, base_val or []))
                their_adds = [x for x in their_val if str(x) not in base_set]
                our_adds = [x for x in our_val if str(x) not in base_set]
                seen = set()
                merged = list(base_val or [])
                for item in their_adds + our_adds:
                    key_str = str(item)
                    if key_str not in seen:
                        merged.append(item)
                        seen.add(key_str)
                result[key] = merged
            else:
                # True conflict: prefer ours (configurable)
                result[key] = our_val

        return result

    def _field_level_cas(self, base: Any, theirs: Any, ours: Any) -> Any:
        """Only update fields that have not changed since our read."""
        if not isinstance(ours, dict):
            return ours
        result = copy.deepcopy(theirs)
        base_dict = base if isinstance(base, dict) else {}
        for key, our_val in ours.items():
            base_val = base_dict.get(key)
            their_val = theirs.get(key)
            # Only apply our change if they haven't changed this field
            if their_val == base_val:
                result[key] = our_val
        return result


class MergingOptimisticStore(OptimisticStore):
    """OptimisticStore that automatically merges conflicts instead of raising."""

    def __init__(self, resolver: ConflictResolver):
        super().__init__()
        self.resolver = resolver
        self._base_snapshots: dict[str, Any] = {}  # version -> base value

    async def put_with_merge(
        self,
        key: str,
        value: Any,
        expected_version: int,
        writer_id: Optional[str] = None,
    ) -> VersionedValue:
        """Write with automatic three-way merge on conflict."""
        async with self._lock:
            existing = self._data.get(key)
            current_ver = existing.version if existing else 0

            if current_ver == expected_version or expected_version <= 0:
                # No conflict
                new_version = current_ver + 1
            else:
                # Conflict: merge
                base_value = self._base_snapshots.get(f"{key}@{expected_version}")
                merged = self.resolver.resolve(
                    base=base_value,
                    theirs=existing.value,
                    ours=value,
                )
                value = merged
                new_version = current_ver + 1

            updated = VersionedValue(
                value=value,
                version=new_version,
                updated_at=time.time(),
                updated_by=writer_id,
            )
            self._data[key] = updated
            # Cache snapshot for future three-way merges
            self._base_snapshots[f"{key}@{new_version}"] = copy.deepcopy(value)
            return updated
```

---

## Solution 5: Distributed OCC with Redis WATCH/MULTI/EXEC

For agents running across multiple processes or hosts, OCC must be coordinated via an external store. Redis `WATCH`/`MULTI`/`EXEC` provides atomic CAS transactions.

```python
import asyncio
import json
import uuid
from typing import Any, Optional
import redis.asyncio as aioredis

class RedisOptimisticStore:
    """
    Distributed optimistic store backed by Redis.
    Uses WATCH + MULTI/EXEC for atomic CAS semantics.
    """

    VERSION_SUFFIX = ":version"

    def __init__(self, redis_url: str = "redis://localhost:6379"):
        self.redis = aioredis.from_url(redis_url, decode_responses=True)

    async def get(self, key: str) -> Optional[tuple[Any, int]]:
        """Returns (value, version) or None."""
        pipe = self.redis.pipeline()
        pipe.get(key)
        pipe.get(key + self.VERSION_SUFFIX)
        results = await pipe.execute()
        raw, ver_str = results
        if raw is None:
            return None
        return json.loads(raw), int(ver_str or 0)

    async def put(
        self,
        key: str,
        value: Any,
        expected_version: int,
        max_retries: int = 3,
    ) -> int:
        """
        Atomic CAS write. Returns new version on success.
        Raises VersionConflictError if version mismatch persists after retries.
        """
        ver_key = key + self.VERSION_SUFFIX

        for attempt in range(max_retries):
            async with self.redis.pipeline(transaction=True) as pipe:
                try:
                    # Watch both key and version key for changes
                    await pipe.watch(key, ver_key)

                    current_ver_str = await pipe.get(ver_key)
                    current_ver = int(current_ver_str) if current_ver_str else 0

                    if expected_version not in (-1, 0) and current_ver != expected_version:
                        await pipe.reset()
                        raise VersionConflictError(key, expected_version, current_ver)

                    new_version = current_ver + 1
                    pipe.multi()
                    pipe.set(key, json.dumps(value))
                    pipe.set(ver_key, str(new_version))
                    await pipe.execute()
                    return new_version

                except aioredis.WatchError:
                    # Another client modified the key between WATCH and EXEC
                    if attempt == max_retries - 1:
                        raise VersionConflictError(key, expected_version, -1)
                    await asyncio.sleep(0.01 * (2 ** attempt))

        raise VersionConflictError(key, expected_version, -1)

    async def delete(self, key: str, expected_version: int) -> bool:
        ver_key = key + self.VERSION_SUFFIX
        async with self.redis.pipeline(transaction=True) as pipe:
            await pipe.watch(key, ver_key)
            current_ver_str = await pipe.get(ver_key)
            current_ver = int(current_ver_str) if current_ver_str else 0
            if current_ver != expected_version:
                await pipe.reset()
                raise VersionConflictError(key, expected_version, current_ver)
            pipe.multi()
            pipe.delete(key, ver_key)
            await pipe.execute()
            return True

    async def atomic_increment(self, key: str) -> int:
        """Shortcut for numeric counters — Redis INCR is inherently atomic."""
        return await self.redis.incr(key)


# --- Distributed agent with Redis OCC ---

class DistributedAgent:
    def __init__(self, agent_id: str, store: RedisOptimisticStore):
        self.id = agent_id
        self.store = store

    async def update_shared_knowledge(self, fact: str) -> None:
        for attempt in range(5):
            result = await self.store.get("shared:knowledge")
            if result is None:
                value, version = {"facts": []}, -1
            else:
                value, version = result

            new_value = {**value, "facts": value.get("facts", []) + [fact]}
            try:
                new_ver = await self.store.put("shared:knowledge", new_value, version)
                print(f"[{self.id}] Wrote version {new_ver}")
                return
            except VersionConflictError:
                await asyncio.sleep(0.05 * (attempt + 1))

        raise RuntimeError(f"[{self.id}] Failed to update after 5 attempts")
```

---

## Solution 6: Transactional Read-Modify-Write Context Manager

A high-level context manager that wraps the full read-modify-write cycle, automatically retrying and merging, with telemetry.

```python
import asyncio
import time
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Optional, AsyncGenerator

logger = logging.getLogger(__name__)

@dataclass
class TransactionMetrics:
    key: str
    attempts: int = 0
    conflicts: int = 0
    duration_ms: float = 0.0
    success: bool = False

class TransactionalStateManager:
    """
    High-level OCC transaction manager with automatic retry, merge, and metrics.
    """

    def __init__(
        self,
        store: OptimisticStore,
        resolver: Optional[ConflictResolver] = None,
        max_retries: int = 5,
        metrics_callback: Optional[callable] = None,
    ):
        self.store = store
        self.resolver = resolver or ConflictResolver(MergeStrategy.DEEP_MERGE)
        self.max_retries = max_retries
        self.metrics_callback = metrics_callback

    @asynccontextmanager
    async def transaction(
        self, key: str, writer_id: Optional[str] = None
    ) -> AsyncGenerator[dict, None]:
        """
        Context manager for read-modify-write transactions.

        Usage:
            async with manager.transaction("config:global") as state:
                state["feature_flags"]["dark_mode"] = True
            # Automatically committed with OCC on exit
        """
        metrics = TransactionMetrics(key=key)
        start = time.monotonic()

        for attempt in range(1, self.max_retries + 1):
            metrics.attempts = attempt

            # Read current state
            snapshot = await self.store.get(key)
            if snapshot is None:
                current_value: Any = {}
                read_version = -1
                base_value = {}
            else:
                import copy
                current_value = copy.deepcopy(snapshot.value)
                read_version = snapshot.version
                base_value = copy.deepcopy(snapshot.value)

            # Yield mutable state to caller
            mutable = current_value if isinstance(current_value, dict) else current_value
            yield mutable

            # Attempt to commit
            try:
                await self.store.put(key, mutable, expected_version=read_version, writer_id=writer_id)
                metrics.success = True
                break

            except VersionConflictError:
                metrics.conflicts += 1
                logger.debug(
                    "OCC conflict on '%s' (attempt %d/%d)", key, attempt, self.max_retries
                )
                if attempt == self.max_retries:
                    metrics.duration_ms = (time.monotonic() - start) * 1000
                    if self.metrics_callback:
                        self.metrics_callback(metrics)
                    raise RuntimeError(
                        f"Transaction on '{key}' failed after {self.max_retries} attempts"
                    )

                # Merge with latest before retrying
                latest = await self.store.get(key)
                if latest:
                    merged = self.resolver.resolve(
                        base=base_value,
                        theirs=latest.value,
                        ours=mutable,
                    )
                    # Update mutable in-place for next yield
                    if isinstance(mutable, dict) and isinstance(merged, dict):
                        mutable.clear()
                        mutable.update(merged)
                    read_version = latest.version

                delay = 0.05 * (2 ** (attempt - 1))
                await asyncio.sleep(delay)

        metrics.duration_ms = (time.monotonic() - start) * 1000
        if self.metrics_callback:
            self.metrics_callback(metrics)


# --- Usage ---

async def demo_transactional():
    store = OptimisticStore()
    manager = TransactionalStateManager(
        store=store,
        resolver=ConflictResolver(MergeStrategy.DEEP_MERGE),
        metrics_callback=lambda m: logger.info("TX metrics: %s", m),
    )

    async def agent_update(agent_id: str, feature: str, enabled: bool):
        async with manager.transaction("config:features", writer_id=agent_id) as state:
            state.setdefault("features", {})
            state["features"][feature] = enabled
            state.setdefault("last_updater", agent_id)

    # Concurrent agents safely update different feature flags
    await asyncio.gather(
        agent_update("agent-1", "dark_mode", True),
        agent_update("agent-2", "beta_search", True),
        agent_update("agent-3", "analytics", False),
    )

    final = await store.get("config:features")
    print(f"Final config: {final.value}")
    # -> {"features": {"dark_mode": True, "beta_search": True, "analytics": False}, ...}
```

---

## Comparison

| Solution | Conflict Detection | Merge Strategy | Distribution | Best For |
|---|---|---|---|---|
| Version-Stamped CAS | Version integer | Abort & retry | In-process | Simple key-value state |
| Retry with Backoff | Version integer | Retry decorator | In-process | Read-modify-write loops |
| MVCC | Timestamp snapshots | Snapshot isolation | In-process | Analytics + audit trails |
| Three-Way Merge | Version + base snapshot | Field-level merge | In-process | Rich nested documents |
| Redis WATCH/MULTI | Redis WATCH | Abort & retry | Distributed | Multi-process agents |
| Transactional Context | Version integer | Pluggable resolver | In-process | High-level ergonomics |

**Choose version-stamped CAS** for simple counters and flags. **Choose MVCC** when agents need point-in-time consistent reads without blocking writers. **Choose three-way merge** for richly structured documents where concurrent edits to different fields should both succeed. **Choose Redis WATCH** for distributed deployments. **Use the transactional context manager** as a high-level wrapper when you want ergonomic transaction semantics with automatic retry and merge baked in.
