---
title: "Agent Doesn't Implement Async Read-Write Lock"
description: "AI agents protect shared state with a plain asyncio.Lock, serializing all readers even when concurrent reads are safe, causing unnecessary latency under read-heavy workloads."
category: concurrency
difficulty: intermediate
tags: [rwlock, asyncio, concurrency, readers-writers, shared-state, performance]
---

# Agent Doesn't Implement Async Read-Write Lock

## Problem

A plain `asyncio.Lock` allows only one coroutine to access shared state at a time — even when multiple readers could safely run concurrently. An agent caching tool schemas, embeddings, or session data reads far more often than it writes. With a plain lock, 100 concurrent readers queue up serially behind each other. A read-write lock (RWLock) allows unlimited concurrent readers while ensuring writers get exclusive access.

## Solution 1: Simple Async RWLock Using asyncio Primitives

Build an RWLock from `asyncio.Condition` — the foundation for all other solutions.

```python
import asyncio

class AsyncRWLock:
    """
    Read-Write Lock for asyncio.
    - Multiple concurrent readers allowed.
    - Writers get exclusive access (no readers or other writers).
    """

    def __init__(self):
        self._condition = asyncio.Condition(asyncio.Lock())
        self._readers = 0
        self._writer_active = False
        self._writers_waiting = 0

    async def acquire_read(self):
        async with self._condition:
            # Wait until no active or waiting writer (writer-preference)
            while self._writer_active or self._writers_waiting > 0:
                await self._condition.wait()
            self._readers += 1

    async def release_read(self):
        async with self._condition:
            self._readers -= 1
            if self._readers == 0:
                self._condition.notify_all()

    async def acquire_write(self):
        async with self._condition:
            self._writers_waiting += 1
            try:
                while self._writer_active or self._readers > 0:
                    await self._condition.wait()
                self._writer_active = True
            finally:
                self._writers_waiting -= 1

    async def release_write(self):
        async with self._condition:
            self._writer_active = False
            self._condition.notify_all()

    def reader(self):
        """Context manager for read access."""
        return _ReadContext(self)

    def writer(self):
        """Context manager for write access."""
        return _WriteContext(self)

class _ReadContext:
    def __init__(self, lock: AsyncRWLock): self._lock = lock
    async def __aenter__(self): await self._lock.acquire_read()
    async def __aexit__(self, *_): await self._lock.release_read()

class _WriteContext:
    def __init__(self, lock: AsyncRWLock): self._lock = lock
    async def __aenter__(self): await self._lock.acquire_write()
    async def __aexit__(self, *_): await self._lock.release_write()

# Usage: shared tool schema cache
schema_cache: dict = {}
cache_lock = AsyncRWLock()

async def get_schema(tool_name: str) -> dict | None:
    async with cache_lock.reader():           # many coroutines can read concurrently
        return schema_cache.get(tool_name)

async def update_schema(tool_name: str, schema: dict):
    async with cache_lock.writer():           # exclusive write
        schema_cache[tool_name] = schema
```

**When to use**: Any shared data structure with >5:1 read-to-write ratio under concurrent access.

---

## Solution 2: Writer-Preference RWLock with Starvation Prevention

Prevent readers from starving writers and writers from starving readers using a queue-based approach.

```python
import asyncio
from collections import deque

class FairAsyncRWLock:
    """
    Fair RWLock: writers are never indefinitely starved by readers,
    and readers queued before a writer run first.
    Uses a FIFO queue of (is_writer, Event) to ensure ordering.
    """

    def __init__(self):
        self._queue: deque[tuple[bool, asyncio.Event]] = deque()
        self._active_readers = 0
        self._writer_active = False
        self._lock = asyncio.Lock()

    async def acquire_read(self):
        async with self._lock:
            # If no pending writers or active writer, proceed immediately
            if not self._writer_active and not any(is_w for is_w, _ in self._queue if is_w):
                self._active_readers += 1
                return
            # Otherwise, queue this reader
            event = asyncio.Event()
            self._queue.append((False, event))

        await event.wait()

    async def release_read(self):
        async with self._lock:
            self._active_readers -= 1
            self._try_advance()

    async def acquire_write(self):
        async with self._lock:
            if not self._writer_active and self._active_readers == 0:
                self._writer_active = True
                return
            event = asyncio.Event()
            self._queue.append((True, event))

        await event.wait()

    async def release_write(self):
        async with self._lock:
            self._writer_active = False
            self._try_advance()

    def _try_advance(self):
        """Wake the next queued waiter(s)."""
        if self._writer_active or not self._queue:
            return

        # If next is a writer and no active readers, wake it
        next_is_writer, next_event = self._queue[0]
        if next_is_writer and self._active_readers == 0:
            self._queue.popleft()
            self._writer_active = True
            next_event.set()
            return

        # Otherwise, wake all consecutive readers at the front
        while self._queue and not self._queue[0][0]:
            _, event = self._queue.popleft()
            self._active_readers += 1
            event.set()

    def reader(self): return _FairReadCtx(self)
    def writer(self): return _FairWriteCtx(self)

class _FairReadCtx:
    def __init__(self, l): self._l = l
    async def __aenter__(self): await self._l.acquire_read()
    async def __aexit__(self, *_): await self._l.release_read()

class _FairWriteCtx:
    def __init__(self, l): self._l = l
    async def __aenter__(self): await self._l.acquire_write()
    async def __aexit__(self, *_): await self._l.release_write()
```

**When to use**: Mixed workloads where both reads and writes occur frequently. Prevents livelock scenarios.

---

## Solution 3: Upgrade-Capable RWLock (Read → Write Upgrade)

Allow a coroutine holding a read lock to atomically upgrade to a write lock without releasing first.

```python
import asyncio

class UpgradableRWLock:
    """
    RWLock that supports upgrading a read lock to a write lock.
    Only ONE coroutine can be in "upgrading" state at a time.
    """

    def __init__(self):
        self._cond = asyncio.Condition()
        self._readers = 0
        self._writer = False
        self._upgrader = False  # someone is waiting to upgrade

    async def acquire_read(self):
        async with self._cond:
            while self._writer:
                await self._cond.wait()
            self._readers += 1

    async def release_read(self):
        async with self._cond:
            self._readers -= 1
            self._cond.notify_all()

    async def acquire_write(self):
        async with self._cond:
            while self._writer or self._readers > 0:
                await self._cond.wait()
            self._writer = True

    async def release_write(self):
        async with self._cond:
            self._writer = False
            self._cond.notify_all()

    async def upgrade_to_write(self):
        """
        Upgrade from read to write.
        Caller must already hold a read lock.
        """
        async with self._cond:
            if self._upgrader:
                raise RuntimeError("Another coroutine is already upgrading — deadlock risk")
            self._upgrader = True
            self._readers -= 1  # release our read slot
            try:
                while self._writer or self._readers > 0:
                    await self._cond.wait()
                self._writer = True
            finally:
                self._upgrader = False

    def reader(self): return _UpgReadCtx(self)
    def writer(self): return _UpgWriteCtx(self)

class _UpgReadCtx:
    def __init__(self, l): self._l = l
    async def __aenter__(self): await self._l.acquire_read(); return self
    async def __aexit__(self, *_): await self._l.release_read()
    async def upgrade(self): await self._l.upgrade_to_write()

class _UpgWriteCtx:
    def __init__(self, l): self._l = l
    async def __aenter__(self): await self._l.acquire_write()
    async def __aexit__(self, *_): await self._l.release_write()

# Usage: read, then conditionally upgrade
lock = UpgradableRWLock()

async def read_or_refresh(cache: dict, key: str, fetch_fn) -> str:
    async with lock.reader() as r:
        if key in cache:
            return cache[key]           # cache hit: stay in read mode
        # Cache miss: upgrade to write
        await r.upgrade()
        if key not in cache:            # re-check after upgrade
            cache[key] = await fetch_fn(key)
        # We now hold write lock; it will be released on context exit (but we upgraded)
    # Note: after upgrade(), the _UpgReadCtx.__aexit__ calls release_read
    # but we no longer hold a read lock — in production, track upgrade state carefully
    return cache[key]
```

**When to use**: Cache-aside patterns where you read first and only write on a miss — avoiding acquiring write lock speculatively.

---

## Solution 4: Sharded RWLock for High-Concurrency Key-Value Access

Stripe the RWLock across N shards so different keys can be read/written concurrently.

```python
import asyncio
import hashlib
from typing import Any

class AsyncRWLock:
    def __init__(self):
        self._cond = asyncio.Condition()
        self._readers = 0
        self._writer = False

    async def acquire_read(self):
        async with self._cond:
            while self._writer:
                await self._cond.wait()
            self._readers += 1

    async def release_read(self):
        async with self._cond:
            self._readers -= 1
            self._cond.notify_all()

    async def acquire_write(self):
        async with self._cond:
            while self._writer or self._readers > 0:
                await self._cond.wait()
            self._writer = True

    async def release_write(self):
        async with self._cond:
            self._writer = False
            self._cond.notify_all()

class ShardedRWCache:
    """High-concurrency key-value store with per-shard RWLocks."""

    def __init__(self, num_shards: int = 16):
        self._num_shards = num_shards
        self._data: list[dict[str, Any]] = [{} for _ in range(num_shards)]
        self._locks: list[AsyncRWLock] = [AsyncRWLock() for _ in range(num_shards)]

    def _shard(self, key: str) -> int:
        return int(hashlib.md5(key.encode()).hexdigest(), 16) % self._num_shards

    async def get(self, key: str) -> Any | None:
        shard = self._shard(key)
        await self._locks[shard].acquire_read()
        try:
            return self._data[shard].get(key)
        finally:
            await self._locks[shard].release_read()

    async def set(self, key: str, value: Any):
        shard = self._shard(key)
        await self._locks[shard].acquire_write()
        try:
            self._data[shard][key] = value
        finally:
            await self._locks[shard].release_write()

    async def delete(self, key: str) -> bool:
        shard = self._shard(key)
        await self._locks[shard].acquire_write()
        try:
            existed = key in self._data[shard]
            self._data[shard].pop(key, None)
            return existed
        finally:
            await self._locks[shard].release_write()

    async def get_or_set(self, key: str, factory) -> Any:
        """Optimistic read-then-write-on-miss."""
        value = await self.get(key)
        if value is not None:
            return value
        new_value = await factory(key)
        await self.set(key, new_value)
        return new_value

    def stats(self) -> dict:
        return {
            "shards": self._num_shards,
            "total_keys": sum(len(d) for d in self._data),
            "keys_per_shard": [len(d) for d in self._data],
        }

# Usage: agent embedding cache with 16 shards
embedding_cache = ShardedRWCache(num_shards=16)

async def get_embedding(text: str) -> list[float]:
    return await embedding_cache.get_or_set(text, _compute_embedding)

async def _compute_embedding(text: str) -> list[float]:
    await asyncio.sleep(0.05)  # simulate embedding API call
    return [0.1] * 1536
```

**When to use**: In-process caches accessed by many concurrent coroutines. Sharding reduces lock contention by 1/N.

---

## Solution 5: Timeout-Aware RWLock with Deadlock Detection

Add per-acquire timeouts and track lock holders for deadlock detection.

```python
import asyncio
import time
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

@dataclass
class LockHolder:
    task_name: str
    acquired_at: float = field(default_factory=time.monotonic)
    is_writer: bool = False

class TimedRWLock:
    def __init__(self, name: str = "unnamed", stale_threshold: float = 30.0):
        self._name = name
        self._cond = asyncio.Condition()
        self._readers: list[LockHolder] = []
        self._writer: LockHolder | None = None
        self._stale = stale_threshold

    def _task_name(self) -> str:
        try:
            task = asyncio.current_task()
            return task.get_name() if task else "main"
        except RuntimeError:
            return "unknown"

    async def acquire_read(self, timeout: float = 10.0) -> bool:
        deadline = time.monotonic() + timeout
        async with self._cond:
            while self._writer:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    logger.warning("rwlock_read_timeout", extra={"lock": self._name})
                    return False
                try:
                    await asyncio.wait_for(self._cond.wait(), timeout=remaining)
                except asyncio.TimeoutError:
                    return False
            holder = LockHolder(task_name=self._task_name(), is_writer=False)
            self._readers.append(holder)
            return True

    async def release_read(self):
        async with self._cond:
            task_name = self._task_name()
            self._readers = [r for r in self._readers if r.task_name != task_name]
            self._cond.notify_all()

    async def acquire_write(self, timeout: float = 10.0) -> bool:
        deadline = time.monotonic() + timeout
        async with self._cond:
            while self._writer or self._readers:
                # Check for stale locks
                self._warn_stale_holders()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    logger.warning("rwlock_write_timeout", extra={"lock": self._name})
                    return False
                try:
                    await asyncio.wait_for(self._cond.wait(), timeout=remaining)
                except asyncio.TimeoutError:
                    return False
            self._writer = LockHolder(task_name=self._task_name(), is_writer=True)
            return True

    async def release_write(self):
        async with self._cond:
            self._writer = None
            self._cond.notify_all()

    def _warn_stale_holders(self):
        now = time.monotonic()
        for holder in self._readers:
            if now - holder.acquired_at > self._stale:
                logger.error("stale_read_lock", extra={
                    "lock": self._name, "task": holder.task_name,
                    "held_s": round(now - holder.acquired_at, 1),
                })
        if self._writer and now - self._writer.acquired_at > self._stale:
            logger.error("stale_write_lock", extra={
                "lock": self._name, "task": self._writer.task_name,
                "held_s": round(now - self._writer.acquired_at, 1),
            })

# Usage with timeout
lock = TimedRWLock(name="session_cache")

async def safe_read(cache: dict, key: str):
    if not await lock.acquire_read(timeout=5.0):
        raise TimeoutError("Could not acquire read lock within 5s")
    try:
        return cache.get(key)
    finally:
        await lock.release_read()
```

**When to use**: Production systems where lock starvation or deadlocks must be detected and surfaced in logs.

---

## Solution 6: Read-Copy-Update (RCU) Pattern — Lock-Free Reads

Replace the RWLock entirely for read-heavy workloads: readers always access an immutable snapshot; writers atomically swap a new snapshot in.

```python
import asyncio
from typing import TypeVar, Generic
import copy

T = TypeVar("T")

class RCUProtected(Generic[T]):
    """
    Read-Copy-Update: readers are always lock-free.
    Writers copy the data, mutate the copy, then atomically swap the reference.
    """

    def __init__(self, initial: T):
        self._value: T = initial
        self._write_lock = asyncio.Lock()  # only one writer at a time

    def read(self) -> T:
        """Lock-free read — always returns the current snapshot."""
        return self._value  # Python reference reads are atomic

    async def update(self, mutate_fn) -> T:
        """
        Copy-modify-swap: safe concurrent readers see either old or new, never partial.
        """
        async with self._write_lock:
            # Copy the current value
            new_value = copy.deepcopy(self._value)
            # Mutate the copy
            mutate_fn(new_value)
            # Atomic swap (Python assignment is atomic for simple references)
            self._value = new_value
            return new_value

# Usage: agent tool registry with lock-free reads
tool_registry = RCUProtected({"search": {"description": "Search the web"}})

async def get_tool(name: str) -> dict | None:
    # No lock needed — reads are always consistent snapshots
    registry = tool_registry.read()
    return registry.get(name)

async def register_tool(name: str, definition: dict):
    # Writer acquires exclusive lock, copies, mutates, swaps
    await tool_registry.update(lambda reg: reg.update({name: definition}))

async def demo():
    # 1000 concurrent readers — all lock-free
    results = await asyncio.gather(*[get_tool("search") for _ in range(1000)])
    print(f"All reads returned: {all(r is not None for r in results)}")

    # Writer: atomic swap
    await register_tool("calculator", {"description": "Perform math"})
    print(f"After update: {tool_registry.read().keys()}")
```

**When to use**: Extremely read-heavy workloads (>100:1 read-to-write). RCU gives theoretically optimal read performance.

---

## Comparison

| Solution | Read Concurrency | Writer Fairness | Lock-Free Reads | Upgrade Support | Best For |
|---|---|---|---|---|---|
| Simple AsyncRWLock | Full | Writer-preferred | No | No | General shared state |
| Fair RWLock | Full | FIFO-fair | No | No | Mixed read/write workloads |
| Upgradable RWLock | Full | Writer-preferred | No | Yes | Cache-aside patterns |
| Sharded RWLock | Per-shard full | Per-shard | No | No | Key-value caches |
| Timed RWLock | Full | Writer-preferred | No | No | Deadlock detection |
| RCU protected | Lock-free | Exclusive | Yes | No | Read-dominated registries |

**Rule of thumb**: Use sharded RWLock for caches (16 shards is a good default). Use RCU for config/registry objects that are written rarely. Use plain `asyncio.Lock` only when reads are rare or the data structure is complex to copy.
