---
layout: solution
title: "Race Condition in Shared Agent State — Multiple Agents Corrupting Shared Data"
category: concurrency
description: "Two agent workers read the same counter, both increment it locally, both write back. One increment is lost. Classic read-modify-write race condition in shared agent state."
tags: [race-condition, concurrency, shared-state, locks, atomic, agents]
---

# Race Condition in Shared Agent State — Multiple Agents Corrupting Shared Data

## Symptom
- Counter that should reach 100 only reaches 73 after 100 concurrent increments
- Shared task list loses items when multiple agents update it simultaneously
- Two agents both "claim" the same work item — work gets done twice
- Database record shows stale data after concurrent updates
- State is inconsistent in unpredictable ways — bug doesn't reproduce deterministically

## Root Cause
Read-modify-write without atomicity. Agent A reads value (5), Agent B reads value (5), A writes 6, B writes 6 — one increment is lost. Any shared mutable state accessed by concurrent agents without locking is vulnerable.

## Fix

### Option 1: asyncio.Lock for in-process shared state

```python
import asyncio

class SharedAgentState:
    def __init__(self):
        self._lock = asyncio.Lock()
        self._counter = 0
        self._task_queue = []

    async def increment_counter(self) -> int:
        async with self._lock:
            self._counter += 1
            return self._counter

    async def claim_task(self) -> dict | None:
        async with self._lock:
            if self._task_queue:
                return self._task_queue.pop(0)  # Atomic claim
            return None

    async def add_tasks(self, tasks: list[dict]):
        async with self._lock:
            self._task_queue.extend(tasks)

# Single shared instance across all agents
state = SharedAgentState()

async def agent_worker(worker_id: int):
    while True:
        task = await state.claim_task()
        if task is None:
            break  # No more work
        # Process task without holding the lock
        result = await process_task(task)
        await state.increment_counter()
```

### Option 2: Redis atomic operations for distributed agents

```python
import redis.asyncio as redis

class DistributedAgentState:
    def __init__(self, redis_url: str):
        self.redis = redis.from_url(redis_url)

    async def increment_counter(self, key: str) -> int:
        """Atomic increment — no race condition possible"""
        return await self.redis.incr(key)

    async def claim_task(self, queue_key: str) -> str | None:
        """Atomic pop — only one agent gets each task"""
        return await self.redis.lpop(queue_key)

    async def set_if_not_exists(self, key: str, value: str, ttl: int = 60) -> bool:
        """Atomic check-and-set (distributed lock)"""
        return await self.redis.set(key, value, nx=True, ex=ttl)

# Usage
state = DistributedAgentState("redis://localhost:6379")

async def distributed_agent_worker(worker_id: int):
    while True:
        task_json = await state.claim_task("agent:task_queue")
        if task_json is None:
            break
        task = json.loads(task_json)
        await process_task(task)
        await state.increment_counter("agent:completed_count")
```

### Option 3: Database optimistic locking

```python
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

async def claim_work_item(session: AsyncSession, agent_id: str) -> dict | None:
    """Claim work item using optimistic locking"""
    # Find an unclaimed item
    result = await session.execute(
        sa.select(WorkItem)
        .where(WorkItem.status == "pending")
        .limit(1)
        .with_for_update(skip_locked=True)  # Skip rows locked by other agents
    )
    item = result.scalar_one_or_none()

    if item is None:
        return None

    # Mark as claimed by this agent
    item.status = "in_progress"
    item.claimed_by = agent_id
    item.claimed_at = datetime.utcnow()
    await session.commit()

    return item
```

### Option 4: Version-based optimistic concurrency

```python
async def update_shared_config(key: str, new_value: dict, expected_version: int) -> bool:
    """Update only if version matches — raises if someone else updated first"""
    async with db.transaction():
        current = await db.get(key)
        if current["version"] != expected_version:
            return False  # Someone else modified it — caller must retry

        await db.update(key, {
            **new_value,
            "version": expected_version + 1
        })
        return True

# Usage with retry
async def safe_update(key: str, transform_fn):
    for attempt in range(10):
        current = await db.get(key)
        new_value = transform_fn(current["data"])
        success = await update_shared_config(key, new_value, current["version"])
        if success:
            return
        await asyncio.sleep(0.1 * attempt)  # Backoff on conflict
    raise RuntimeError(f"Could not update {key} after 10 attempts — too much contention")
```

### Option 5: Message queue — eliminate shared state entirely

```python
import asyncio

async def coordinator(task_queue: asyncio.Queue, result_queue: asyncio.Queue):
    """Single coordinator owns state; agents communicate via queues"""
    completed = 0
    results = []

    while True:
        result = await result_queue.get()
        results.append(result)
        completed += 1
        print(f"Completed: {completed}")

async def agent_worker(worker_id: int, task_queue: asyncio.Queue, result_queue: asyncio.Queue):
    """Agents only read from task queue, write to result queue — no shared state"""
    while True:
        try:
            task = task_queue.get_nowait()
        except asyncio.QueueEmpty:
            break
        result = await process_task(task)
        await result_queue.put(result)
        task_queue.task_done()

# Setup
task_q = asyncio.Queue()
result_q = asyncio.Queue()
for task in tasks:
    await task_q.put(task)

await asyncio.gather(
    coordinator(task_q, result_q),
    *[agent_worker(i, task_q, result_q) for i in range(5)]
)
```

## Shared State Danger Zones

| Pattern | Risk | Fix |
|---|---|---|
| Global dict/counter | High | `asyncio.Lock` or atomic ops |
| File read-modify-write | High | File lock (`fcntl.flock`) |
| DB row update without lock | High | `SELECT FOR UPDATE` or optimistic version |
| Redis `GET` then `SET` | Medium | `INCR`, `SETNX`, or Lua script |
| In-memory queue `.pop()` | Medium | `asyncio.Queue` |
| Logging to same file | Low | `logging` module (thread-safe) |

## Expected Token Savings
Debugging race conditions is extremely hard — hours of debugging vs. 10 lines of locking code.

## Environment
- Multi-agent systems with shared state; most critical in distributed deployments
- Source: direct experience, classic concurrent programming patterns
