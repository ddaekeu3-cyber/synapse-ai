---
layout: solution
title: "Agent Holds Lock Too Long — Starves Other Workers"
category: concurrency
description: "Agent acquires a lock to access a shared resource and holds it for 30 seconds while doing slow I/O or LLM inference. Other workers queue up waiting. Throughput collapses. One slow agent blocks all others."
tags: [concurrency, lock, starvation, asyncio, deadlock, mutex, queue, throughput]
---

# Agent Holds Lock Too Long — Starves Other Workers

## Symptom
- 10 agent workers running, but throughput is the same as 1 — all waiting on one lock
- Lock wait time visible in profiling: 95% of time is lock contention, not actual work
- Single slow LLM call inside a locked section blocks all other workers for 60 seconds
- `asyncio.Lock` acquired but never released — deadlock after one agent crashes
- Workers queue up with increasing latency despite low CPU usage
- Database connection pool exhausted because workers hold connections too long

## Root Cause
Locks are held too broadly — the critical section includes slow I/O, LLM calls, or large data processing that should not be synchronized. The lock is acquired before the work starts and released after all of it finishes, when it only needed to protect a small state update. Additionally, if the locked code raises an exception, the lock may never be released, causing a deadlock for all waiting workers.

## Fix

### Option 1: Minimize critical section — lock only the state update

```python
import asyncio
from dataclasses import dataclass, field

@dataclass
class SharedAgentState:
    """
    Shared state with fine-grained locking.
    Lock protects only state reads/writes — not slow operations.
    """
    _results: dict[str, any] = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

async def process_item_wrong(item_id: str, lock: asyncio.Lock):
    """WRONG — holds lock during slow LLM call"""
    async with lock:
        data = await fetch_from_database(item_id)     # Slow — 200ms
        result = await call_llm(data)                  # Very slow — 5,000ms
        processed = transform_result(result)           # Fast — 1ms
        shared_results[item_id] = processed            # Fast — 1ms
    # Lock held for ~5,200ms — all other workers wait

async def process_item_correct(item_id: str, state: SharedAgentState):
    """RIGHT — lock held only during state update (< 1ms)"""
    # Do slow work WITHOUT the lock
    data = await fetch_from_database(item_id)          # 200ms, no lock
    result = await call_llm(data)                      # 5,000ms, no lock
    processed = transform_result(result)               # 1ms, no lock

    # Lock only the final state update
    async with state._lock:
        state._results[item_id] = processed            # < 1ms, locked
    # Lock held for < 1ms — minimal contention

# Benchmark difference:
# Wrong: 10 workers × 5,200ms = effectively sequential = 52,000ms total
# Right: 10 workers × 5,200ms parallel + 10 × 1ms locked = ~5,210ms total
```

### Option 2: Always release lock — use context manager

```python
import asyncio
import contextlib

# WRONG — lock not released if exception occurs
lock = asyncio.Lock()

async def unsafe_update(data: dict):
    await lock.acquire()
    result = await risky_operation(data)  # May raise!
    shared_state.update(result)
    lock.release()  # Never reached if risky_operation raises

# RIGHT — context manager guarantees release
async def safe_update(data: dict):
    async with lock:  # Released even if exception occurs
        result = await risky_operation(data)
        shared_state.update(result)

# RIGHT — explicit try/finally if you can't use async with
async def explicit_release(data: dict):
    await lock.acquire()
    try:
        result = await risky_operation(data)
        shared_state.update(result)
    finally:
        lock.release()  # Always released

# RIGHT — lock with timeout to prevent deadlock
async def update_with_timeout(data: dict, timeout: float = 5.0):
    try:
        acquired = await asyncio.wait_for(lock.acquire(), timeout=timeout)
    except asyncio.TimeoutError:
        raise RuntimeError(
            f"Could not acquire lock within {timeout}s — possible deadlock"
        )
    try:
        result = await risky_operation(data)
        shared_state.update(result)
    finally:
        lock.release()
```

### Option 3: Per-key locking — avoid global lock

```python
import asyncio
from collections import defaultdict
import weakref

class KeyedLock:
    """
    Per-key asyncio locks — different keys don't block each other.
    Workers processing different items run concurrently.
    Workers processing the SAME item are serialized.
    """

    def __init__(self):
        # Use weakref so unused locks are garbage collected
        self._locks: dict[str, asyncio.Lock] = {}
        self._meta_lock = asyncio.Lock()  # Protects _locks dict only

    async def get_lock(self, key: str) -> asyncio.Lock:
        async with self._meta_lock:
            if key not in self._locks:
                self._locks[key] = asyncio.Lock()
            return self._locks[key]

    @contextlib.asynccontextmanager
    async def locked(self, key: str):
        lock = await self.get_lock(key)
        async with lock:
            yield

    def cleanup(self, key: str):
        """Remove lock for key when no longer needed"""
        self._locks.pop(key, None)

keyed_locks = KeyedLock()

async def process_user(user_id: str, task: dict):
    # Workers for different users run in parallel
    # Workers for the SAME user are serialized
    async with keyed_locks.locked(user_id):
        # Read current user state
        state = await db.get_user_state(user_id)
        # Update state
        new_state = apply_task(state, task)
        await db.set_user_state(user_id, new_state)

# Instead of one global lock blocking everyone:
# user_001 and user_002 run concurrently
# Two tasks for user_001 run sequentially (correct behavior)
```

### Option 4: Work queue — decouple producers from serialized consumers

```python
import asyncio
from dataclasses import dataclass
from typing import Any

@dataclass
class WorkItem:
    item_id: str
    payload: dict
    result_future: asyncio.Future

class SerializedWorker:
    """
    Process work items sequentially without explicit locking.
    Single worker processes a queue — no lock contention possible.
    Multiple queues for different resource partitions.
    """

    def __init__(self, worker_id: str):
        self.worker_id = worker_id
        self._queue: asyncio.Queue[WorkItem] = asyncio.Queue()
        self._running = False

    async def submit(self, item_id: str, payload: dict) -> Any:
        """Submit work and await result — caller is non-blocking"""
        loop = asyncio.get_event_loop()
        future = loop.create_future()
        await self._queue.put(WorkItem(item_id, payload, future))
        return await future  # Waits for result

    async def run(self):
        """Process queue — runs in background"""
        self._running = True
        while self._running:
            try:
                item = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                try:
                    result = await self._process(item)
                    item.result_future.set_result(result)
                except Exception as e:
                    item.result_future.set_exception(e)
                finally:
                    self._queue.task_done()
            except asyncio.TimeoutError:
                continue  # No work — loop and check _running

    async def _process(self, item: WorkItem) -> Any:
        """Process a single work item — no lock needed"""
        data = await fetch_data(item.item_id)
        result = await call_llm(data)
        await update_shared_state(item.item_id, result)
        return result

# One worker per shared resource partition:
db_writer = SerializedWorker("db_writer")
asyncio.create_task(db_writer.run())

# Multiple agents can submit concurrently — worker serializes execution
results = await asyncio.gather(
    db_writer.submit("item_1", data_1),
    db_writer.submit("item_2", data_2),
    db_writer.submit("item_3", data_3),
)
```

### Option 5: Read-write lock — allow concurrent reads

```python
import asyncio

class ReadWriteLock:
    """
    Multiple concurrent readers OR one exclusive writer.
    Most agent state access is reads — allow them to run in parallel.
    Writers are rare (state updates) — serialize only those.
    """

    def __init__(self):
        self._readers = 0
        self._read_ready = asyncio.Condition()
        self._write_lock = asyncio.Lock()

    @contextlib.asynccontextmanager
    async def read(self):
        """Acquire read lock — concurrent with other readers"""
        async with self._read_ready:
            self._readers += 1
        try:
            yield
        finally:
            async with self._read_ready:
                self._readers -= 1
                if self._readers == 0:
                    self._read_ready.notify_all()

    @contextlib.asynccontextmanager
    async def write(self):
        """Acquire write lock — exclusive, waits for all readers"""
        async with self._write_lock:
            # Wait for all readers to finish
            async with self._read_ready:
                await self._read_ready.wait_for(lambda: self._readers == 0)
                yield

rw_lock = ReadWriteLock()

async def read_agent_config() -> dict:
    """Concurrent reads — multiple agents read config simultaneously"""
    async with rw_lock.read():
        return dict(agent_config)  # Read shared state

async def update_agent_config(updates: dict):
    """Exclusive write — waits for all readers, then updates"""
    async with rw_lock.write():
        agent_config.update(updates)
```

### Option 6: Semaphore — limit concurrency without full serialization

```python
import asyncio

# Instead of a mutex (1 at a time), use semaphore (N at a time)
# Allows controlled parallelism without full lock contention

class RateLimitedWorkerPool:
    """
    Limit concurrent workers without serializing all of them.
    Use when the bottleneck is a resource with limited capacity (DB connections, API rate).
    """

    def __init__(self, max_concurrent: int = 5):
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._active = 0
        self._completed = 0

    async def run(self, coro) -> any:
        """Run coroutine with semaphore-based concurrency limit"""
        async with self._semaphore:
            self._active += 1
            try:
                result = await coro
                self._completed += 1
                return result
            finally:
                self._active -= 1

    @property
    def stats(self) -> dict:
        return {"active": self._active, "completed": self._completed}

# Limit to 5 concurrent LLM calls (API rate limit friendly)
pool = RateLimitedWorkerPool(max_concurrent=5)

async def process_all_items(items: list[dict]) -> list[any]:
    """Process all items with max 5 concurrent — not 1, not unlimited"""
    tasks = [pool.run(process_item(item)) for item in items]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    print(f"Completed: {pool.stats}")
    return results

# 100 items with max_concurrent=5:
# - All 100 submitted at once
# - At most 5 run simultaneously
# - No single lock blocking all 95 others
```

## Locking Strategy Comparison

| Pattern | Concurrency | Use When |
|---|---|---|
| Global mutex | 1 at a time | Rarely — only for simple counters |
| Per-key lock | N (one per key) | Different keys need no coordination |
| Read-write lock | N readers OR 1 writer | Reads >> writes |
| Semaphore | Up to N | Rate limiting, connection pools |
| Work queue | 1 consumer, N producers | Ordered processing required |
| Lock-free (atomic) | Unlimited | Simple integer counters |

## Expected Token Savings
10 workers, 1 slow lock holder → 9 workers idle → task queue backs up → timeouts → retries: ~30,000 wasted tokens
Fine-grained locking → all 10 workers productive → 10× throughput, no retries

## Environment
- Any multi-worker agent system with shared state; critical for agents running concurrent tool calls, parallel processing pipelines, or multi-user session handling
- Source: direct experience; over-broad locks are the most common cause of concurrency that performs worse than single-threaded execution
