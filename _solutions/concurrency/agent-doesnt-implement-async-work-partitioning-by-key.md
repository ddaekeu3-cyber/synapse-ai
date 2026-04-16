---
title: "Agent Doesn't Implement Async Work Partitioning by Key"
description: "AI agents process all incoming tasks in a single shared queue, causing one user's heavy workload to starve others and making per-user ordering guarantees impossible."
category: concurrency
difficulty: intermediate
tags: [partitioning, sharding, asyncio, queue, isolation, fairness, multi-tenant]
---

# Agent Doesn't Implement Async Work Partitioning by Key

## Problem

A single shared task queue mixes tasks from all users. One user submitting 100 tool-call tasks can delay every other user's single request. Additionally, tasks for the same user need to be processed in order (conversation turns must not interleave), but a global queue with multiple workers makes per-user ordering impossible without extra coordination. Partitioning work by key (e.g., user ID, session ID, tenant ID) solves both problems.

## Solution 1: Fixed Key-to-Worker Mapping with asyncio.Queue per Partition

Assign each key to a dedicated worker lane using consistent hashing. Same key → same worker → natural ordering.

```python
import asyncio
import hashlib
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable

@dataclass
class Task:
    key: str         # partition key (e.g., user_id, session_id)
    payload: Any
    future: asyncio.Future = field(default_factory=asyncio.get_event_loop().create_future)

class KeyPartitionedExecutor:
    def __init__(self, num_partitions: int = 16, queue_size: int = 100):
        self._num_partitions = num_partitions
        self._queues: list[asyncio.Queue] = [
            asyncio.Queue(maxsize=queue_size) for _ in range(num_partitions)
        ]
        self._workers: list[asyncio.Task] = []

    def _partition(self, key: str) -> int:
        h = int(hashlib.md5(key.encode()).hexdigest(), 16)
        return h % self._num_partitions

    async def start(self, handler: Callable[[Any], Awaitable[Any]]):
        for i, q in enumerate(self._queues):
            self._workers.append(
                asyncio.create_task(self._worker_loop(q, handler, name=f"partition-{i}"))
            )

    async def _worker_loop(self, queue: asyncio.Queue, handler, name: str):
        while True:
            task: Task = await queue.get()
            try:
                result = await handler(task.payload)
                if not task.future.done():
                    task.future.set_result(result)
            except Exception as e:
                if not task.future.done():
                    task.future.set_exception(e)
            finally:
                queue.task_done()

    async def submit(self, key: str, payload: Any) -> asyncio.Future:
        partition = self._partition(key)
        task = Task(key=key, payload=payload)
        try:
            self._queues[partition].put_nowait(task)
        except asyncio.QueueFull:
            raise RuntimeError(f"Partition {partition} queue full for key={key}")
        return task.future

    async def shutdown(self):
        for w in self._workers:
            w.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)

# Usage
async def handle_agent_request(payload: dict) -> str:
    await asyncio.sleep(0.1)  # simulate work
    return f"processed: {payload}"

async def main():
    executor = KeyPartitionedExecutor(num_partitions=8)
    await executor.start(handle_agent_request)

    # User A's tasks always go to the same partition, preserving order
    fut1 = await executor.submit("user_A", {"turn": 1, "prompt": "Hello"})
    fut2 = await executor.submit("user_A", {"turn": 2, "prompt": "Continue"})
    result1, result2 = await asyncio.gather(fut1, fut2)
    print(result1, result2)
```

**When to use**: Multi-tenant agents where per-user ordering matters. 16 partitions handles thousands of concurrent users.

---

## Solution 2: Dynamic Partition Discovery with Consistent Hashing Ring

Use a consistent hash ring so adding/removing workers redistributes minimal keys.

```python
import asyncio
import hashlib
import bisect
from typing import Any

class ConsistentHashRing:
    def __init__(self, nodes: list[str], replicas: int = 150):
        self._replicas = replicas
        self._ring: list[tuple[int, str]] = []
        for node in nodes:
            self._add_node(node)

    def _add_node(self, node: str):
        for i in range(self._replicas):
            h = int(hashlib.md5(f"{node}:{i}".encode()).hexdigest(), 16)
            bisect.insort(self._ring, (h, node))

    def _remove_node(self, node: str):
        self._ring = [(h, n) for h, n in self._ring if n != node]

    def get_node(self, key: str) -> str:
        if not self._ring:
            raise RuntimeError("Empty ring")
        h = int(hashlib.md5(key.encode()).hexdigest(), 16)
        idx = bisect.bisect_left(self._ring, (h, ""))
        if idx >= len(self._ring):
            idx = 0
        return self._ring[idx][1]

class RingPartitionedPool:
    def __init__(self, worker_ids: list[str]):
        self._ring = ConsistentHashRing(worker_ids)
        self._queues: dict[str, asyncio.Queue] = {
            wid: asyncio.Queue(maxsize=200) for wid in worker_ids
        }
        self._workers: list[asyncio.Task] = []

    async def start(self, handler):
        for wid, q in self._queues.items():
            self._workers.append(
                asyncio.create_task(self._worker(q, handler, wid))
            )

    async def _worker(self, queue: asyncio.Queue, handler, worker_id: str):
        while True:
            key, payload, fut = await queue.get()
            try:
                result = await handler(payload)
                fut.set_result(result)
            except Exception as e:
                fut.set_exception(e)
            finally:
                queue.task_done()

    async def submit(self, key: str, payload: Any):
        worker_id = self._ring.get_node(key)
        loop = asyncio.get_event_loop()
        fut = loop.create_future()
        await self._queues[worker_id].put((key, payload, fut))
        return fut

    def queue_depths(self) -> dict[str, int]:
        return {wid: q.qsize() for wid, q in self._queues.items()}
```

**When to use**: Dynamic agent pools where workers scale in/out. Consistent hashing minimizes remapping on worker addition.

---

## Solution 3: Key-Level Concurrency Limit (One-at-a-Time per Key)

Ensure only one task per key runs at a time (serial per user) while allowing cross-key parallelism.

```python
import asyncio
from collections import defaultdict

class SerialPerKeyExecutor:
    """Tasks with the same key run serially; different keys run in parallel."""

    def __init__(self):
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._pending: dict[str, int] = defaultdict(int)

    async def run(self, key: str, coro):
        self._pending[key] += 1
        try:
            async with self._locks[key]:  # only one task per key at a time
                return await coro
        finally:
            self._pending[key] -= 1
            # Clean up lock reference if no more pending tasks for this key
            if self._pending[key] == 0:
                del self._pending[key]
                if key in self._locks and not self._locks[key].locked():
                    del self._locks[key]

    def active_keys(self) -> list[str]:
        return [k for k, v in self._pending.items() if v > 0]

executor = SerialPerKeyExecutor()

async def process_user_turn(user_id: str, turn: int, prompt: str) -> str:
    async def _handle():
        # This never runs concurrently for the same user_id
        await asyncio.sleep(0.05)
        return f"user={user_id} turn={turn}: response"

    return await executor.run(user_id, _handle())

async def demo():
    # User A's turns are serialized; User B runs concurrently with User A
    results = await asyncio.gather(
        process_user_turn("user_A", 1, "hello"),
        process_user_turn("user_A", 2, "continue"),  # waits for turn 1
        process_user_turn("user_B", 1, "hi"),        # runs in parallel with user_A
    )
    print(results)
```

**When to use**: Conversation agents where message ordering within a session is critical.

---

## Solution 4: Partitioned Priority Queue — High-Priority Keys Jump the Queue

Partition work by key AND priority so premium users' tasks preempt standard users within their partition.

```python
import asyncio
import heapq
from dataclasses import dataclass, field
from typing import Any

@dataclass(order=True)
class PrioritizedTask:
    priority: int         # lower = higher priority (1=urgent, 5=bulk)
    sequence: int         # tie-break: earlier submission wins
    key: str = field(compare=False)
    payload: Any = field(compare=False)
    future: asyncio.Future = field(compare=False, default=None)

class PriorityPartitionedQueue:
    def __init__(self, num_partitions: int = 8):
        self._num_partitions = num_partitions
        self._heaps: list[list] = [[] for _ in range(num_partitions)]
        self._events: list[asyncio.Event] = [asyncio.Event() for _ in range(num_partitions)]
        self._counters = [0] * num_partitions
        self._workers: list[asyncio.Task] = []

    def _partition(self, key: str) -> int:
        import hashlib
        return int(hashlib.md5(key.encode()).hexdigest(), 16) % self._num_partitions

    async def submit(self, key: str, payload: Any, priority: int = 3) -> asyncio.Future:
        p = self._partition(key)
        loop = asyncio.get_event_loop()
        fut = loop.create_future()
        self._counters[p] += 1
        task = PrioritizedTask(
            priority=priority,
            sequence=self._counters[p],
            key=key,
            payload=payload,
            future=fut,
        )
        heapq.heappush(self._heaps[p], task)
        self._events[p].set()
        return fut

    async def start(self, handler):
        for i in range(self._num_partitions):
            self._workers.append(
                asyncio.create_task(self._worker(i, handler))
            )

    async def _worker(self, partition: int, handler):
        heap = self._heaps[partition]
        event = self._events[partition]
        while True:
            if not heap:
                event.clear()
                await event.wait()
            if not heap:
                continue
            task = heapq.heappop(heap)
            try:
                result = await handler(task.payload)
                task.future.set_result(result)
            except Exception as e:
                task.future.set_exception(e)

# Usage: priority 1 = urgent (premium), priority 5 = bulk (free tier)
async def demo():
    pool = PriorityPartitionedQueue(num_partitions=4)
    await pool.start(lambda p: asyncio.sleep(0.01))

    # Bulk task submitted first, but premium task runs first within same partition
    bulk_fut = await pool.submit("user_free", {"q": "bulk"}, priority=5)
    prem_fut = await pool.submit("user_premium", {"q": "urgent"}, priority=1)
    await asyncio.gather(bulk_fut, prem_fut)
```

**When to use**: SaaS agents with tiered service levels. Premium users get sub-second response even under bulk load.

---

## Solution 5: Sticky Session Partitioning with Rebalancing on Overload

Keep a user on the same worker for cache locality, but rebalance when a partition becomes overloaded.

```python
import asyncio
import time
from collections import defaultdict

class StickyPartitionRouter:
    """Routes keys to workers with stickiness; rebalances on overload."""

    def __init__(self, num_workers: int = 8, overload_threshold: int = 50):
        self._num_workers = num_workers
        self._threshold = overload_threshold
        self._key_to_worker: dict[str, int] = {}
        self._worker_loads: list[int] = [0] * num_workers
        self._queues: list[asyncio.Queue] = [asyncio.Queue(maxsize=200) for _ in range(num_workers)]
        self._lock = asyncio.Lock()

    async def _assign_worker(self, key: str) -> int:
        async with self._lock:
            if key in self._key_to_worker:
                assigned = self._key_to_worker[key]
                # Check if current assignment is overloaded
                if self._worker_loads[assigned] < self._threshold:
                    return assigned
                # Rebalance: find least loaded worker
                new_worker = min(range(self._num_workers), key=lambda i: self._worker_loads[i])
                self._key_to_worker[key] = new_worker
                return new_worker
            # First time: assign to least loaded
            worker = min(range(self._num_workers), key=lambda i: self._worker_loads[i])
            self._key_to_worker[key] = worker
            return worker

    async def submit(self, key: str, payload) -> asyncio.Future:
        worker_id = await self._assign_worker(key)
        loop = asyncio.get_event_loop()
        fut = loop.create_future()
        self._worker_loads[worker_id] += 1
        await self._queues[worker_id].put((key, payload, fut, worker_id))
        return fut

    async def start(self, handler):
        for i, q in enumerate(self._queues):
            asyncio.create_task(self._worker_loop(q, handler))

    async def _worker_loop(self, queue: asyncio.Queue, handler):
        while True:
            key, payload, fut, wid = await queue.get()
            try:
                result = await handler(payload)
                fut.set_result(result)
            except Exception as e:
                fut.set_exception(e)
            finally:
                self._worker_loads[wid] = max(0, self._worker_loads[wid] - 1)
                queue.task_done()

    def load_report(self) -> dict:
        return {f"worker_{i}": self._worker_loads[i] for i in range(self._num_workers)}
```

**When to use**: Agents with per-user in-memory caches (embedding caches, tool result caches). Stickiness improves cache hit rate.

---

## Solution 6: Multi-Level Partitioning — Tenant → User → Session

Hierarchical partitioning: tenant gets dedicated resources; within tenant, users are partitioned; within user, sessions are ordered.

```python
import asyncio
import hashlib
from dataclasses import dataclass
from typing import Any

@dataclass
class HierarchicalKey:
    tenant_id: str
    user_id: str
    session_id: str

    def tenant_partition(self, n: int) -> int:
        return int(hashlib.md5(self.tenant_id.encode()).hexdigest(), 16) % n

    def user_partition(self, n: int) -> int:
        return int(hashlib.md5(f"{self.tenant_id}:{self.user_id}".encode()).hexdigest(), 16) % n

    def session_key(self) -> str:
        return f"{self.tenant_id}:{self.user_id}:{self.session_id}"

class HierarchicalPartitionedPool:
    def __init__(self, num_tenant_partitions: int = 4, users_per_partition: int = 4):
        self._tenant_p = num_tenant_partitions
        self._user_p = users_per_partition
        total = num_tenant_partitions * users_per_partition
        self._queues: list[asyncio.Queue] = [asyncio.Queue(maxsize=100) for _ in range(total)]
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._session_lock_guard = asyncio.Lock()

    def _queue_index(self, key: HierarchicalKey) -> int:
        t = key.tenant_partition(self._tenant_p)
        u = key.user_partition(self._user_p)
        return t * self._user_p + u

    async def _get_session_lock(self, session_key: str) -> asyncio.Lock:
        async with self._session_lock_guard:
            if session_key not in self._session_locks:
                self._session_locks[session_key] = asyncio.Lock()
            return self._session_locks[session_key]

    async def submit(self, key: HierarchicalKey, payload: Any, handler) -> Any:
        # Level 1: queue by tenant+user partition
        queue_idx = self._queue_index(key)
        # Level 2: serialize within session
        session_lock = await self._get_session_lock(key.session_key())
        loop = asyncio.get_event_loop()
        fut = loop.create_future()

        async def _run():
            async with session_lock:  # session-level serialization
                try:
                    result = await handler(payload)
                    fut.set_result(result)
                except Exception as e:
                    fut.set_exception(e)

        await self._queues[queue_idx].put(_run)
        return fut

    async def start(self):
        for q in self._queues:
            asyncio.create_task(self._worker(q))

    async def _worker(self, queue: asyncio.Queue):
        while True:
            coro_fn = await queue.get()
            asyncio.create_task(coro_fn())
            queue.task_done()

# Usage
pool = HierarchicalPartitionedPool(num_tenant_partitions=4, users_per_partition=4)

key = HierarchicalKey(tenant_id="acme", user_id="alice", session_id="sess-123")
# acme's tasks stay in acme's partition; alice's sessions are serialized within acme's partition
```

**When to use**: Enterprise multi-tenant agents where tenant isolation, user fairness, and session ordering all matter simultaneously.

---

## Comparison

| Solution | Ordering Guarantee | Fairness | Priority Support | Rebalancing | Best For |
|---|---|---|---|---|---|
| Fixed key→worker mapping | Per-key FIFO | Even (hash-based) | No | No | Simple multi-tenant agents |
| Consistent hash ring | Per-key FIFO | Even (ring-based) | No | On node add/remove | Elastic worker pools |
| Serial per key | Per-key serial | Parallel across keys | No | N/A | Conversation turn ordering |
| Priority partitioned | Per-key FIFO | Priority-weighted | Yes | No | Tiered SLA agents |
| Sticky with rebalance | Per-key FIFO | Load-aware | No | On overload | Cache-affinity agents |
| Hierarchical | Per-session serial | Tenant-isolated | No | N/A | Enterprise multi-tenant |

**Rule of thumb**: Use key-partitioned queues for any multi-user agent. 16 partitions is a good default — enough isolation without too many goroutines.
