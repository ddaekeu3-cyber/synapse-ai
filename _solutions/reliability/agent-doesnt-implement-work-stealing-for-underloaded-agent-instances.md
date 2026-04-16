---
title: "Agent Doesn't Implement Work Stealing for Underloaded Agent Instances"
description: "Multi-agent deployments with per-instance task queues leave some workers idle while others are overloaded, reducing overall throughput. Implement work stealing so idle agents proactively pull tasks from overloaded peers to balance load dynamically."
date: 2026-04-16
difficulty: advanced
category: reliability
slug: agent-doesnt-implement-work-stealing-for-underloaded-agent-instances
tags: [work-stealing, load-balancing, concurrency, reliability, task-queue, throughput]
symptoms:
  - "Some agent instances are idle at 0% CPU while others queue 50+ tasks"
  - "Per-instance task queues create hot spots under uneven traffic distribution"
  - "Agent pool throughput plateaus at 60% despite available idle capacity"
  - "Static round-robin routing ignores real-time queue depth differences"
  - "Batch jobs finish in 10 minutes when balanced, 25 minutes without stealing"
---

## Why This Happens

Static task assignment — round-robin, consistent hashing — doesn't account for variance in task execution time. A worker that received three long-running tasks while a neighbor received three short ones ends up with a backlog while the neighbor is idle. Work stealing lets idle workers take tasks from the tails of overloaded workers' queues, achieving dynamic load balance without centralized coordination.

## Solution 1: Deque-Based Work-Stealing Queue

```python
import asyncio
from collections import deque
from dataclasses import dataclass
from typing import Any, Optional, List

@dataclass
class Task:
    task_id: str
    payload: Any
    priority: int = 0

class WorkStealingDeque:
    """
    Double-ended queue per worker. Workers push/pop from the front (LIFO).
    Thieves steal from the tail (FIFO) to minimize contention.
    This matches the classic Chase-Lev work-stealing deque semantics.
    """

    def __init__(self):
        self._deque: deque = deque()
        self._lock = asyncio.Lock()

    async def push(self, task: Task) -> None:
        """Owner pushes to the front."""
        async with self._lock:
            self._deque.appendleft(task)

    async def pop(self) -> Optional[Task]:
        """Owner pops from the front (LIFO — improves cache locality)."""
        async with self._lock:
            if self._deque:
                return self._deque.popleft()
            return None

    async def steal(self) -> Optional[Task]:
        """Thief steals from the tail (FIFO — steals oldest tasks)."""
        async with self._lock:
            if len(self._deque) > 1:  # Leave at least 1 for the owner
                return self._deque.pop()
            return None

    def size(self) -> int:
        return len(self._deque)

    def is_empty(self) -> bool:
        return len(self._deque) == 0
```

## Solution 2: Work-Stealing Pool Coordinator

```python
import asyncio
import random
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

@dataclass
class WorkerStats:
    worker_id: str
    tasks_executed: int = 0
    tasks_stolen: int = 0
    tasks_donated: int = 0
    idle_steals_attempted: int = 0
    total_execution_ms: float = 0.0

class WorkStealingPool:
    """
    Pool of async workers with work-stealing queues.
    Each worker owns a WorkStealingDeque. When idle, it randomly
    probes other workers' queues and steals from the largest.
    """

    def __init__(
        self,
        num_workers: int,
        execute_fn: Callable[[Task], asyncio.Coroutine],
        steal_threshold: int = 2,    # only steal if victim has > N tasks
        probe_count: int = 2,        # probe this many random workers on idle
    ):
        self._num_workers = num_workers
        self._execute_fn = execute_fn
        self._steal_threshold = steal_threshold
        self._probe_count = probe_count
        self._queues: Dict[str, WorkStealingDeque] = {}
        self._stats: Dict[str, WorkerStats] = {}
        self._running = False

    def _worker_ids(self) -> List[str]:
        return list(self._queues.keys())

    async def submit(self, task: Task, worker_id: Optional[str] = None) -> None:
        """Submit task to a specific worker or the least-loaded one."""
        if worker_id and worker_id in self._queues:
            target = worker_id
        else:
            # Route to least-loaded worker
            target = min(self._queues.keys(), key=lambda wid: self._queues[wid].size())
        await self._queues[target].push(task)

    async def start(self) -> None:
        self._running = True
        for i in range(self._num_workers):
            wid = f"worker-{i}"
            self._queues[wid] = WorkStealingDeque()
            self._stats[wid] = WorkerStats(worker_id=wid)
        workers = [asyncio.create_task(self._worker_loop(wid)) for wid in self._queues]
        await asyncio.gather(*workers)

    async def _worker_loop(self, worker_id: str) -> None:
        q = self._queues[worker_id]
        stats = self._stats[worker_id]

        while self._running:
            task = await q.pop()

            if task is None:
                # Try to steal from another worker
                task = await self._try_steal(worker_id)
                if task is not None:
                    stats.tasks_stolen += 1
                else:
                    stats.idle_steals_attempted += 1
                    await asyncio.sleep(0.005)  # brief idle sleep
                    continue

            t0 = time.monotonic()
            try:
                await self._execute_fn(task)
                stats.tasks_executed += 1
                stats.total_execution_ms += (time.monotonic() - t0) * 1000
            except Exception as exc:
                print(f"[work_steal] worker={worker_id} task={task.task_id} error: {exc}")

    async def _try_steal(self, thief_id: str) -> Optional[Task]:
        """Probe `probe_count` random workers; steal from the busiest."""
        all_ids = [wid for wid in self._worker_ids() if wid != thief_id]
        if not all_ids:
            return None
        victims = random.sample(all_ids, min(self._probe_count, len(all_ids)))
        best_victim = max(victims, key=lambda vid: self._queues[vid].size())
        if self._queues[best_victim].size() > self._steal_threshold:
            task = await self._queues[best_victim].steal()
            if task is not None:
                self._stats[best_victim].tasks_donated += 1
            return task
        return None

    def pool_stats(self) -> dict:
        return {wid: vars(s) for wid, s in self._stats.items()}
```

## Solution 3: Priority-Aware Work Stealing

```python
import asyncio
import heapq
from dataclasses import dataclass, field
from typing import Optional

@dataclass(order=True)
class PriorityTask:
    priority: int          # lower = higher priority
    task_id: str = field(compare=False)
    payload: object = field(compare=False)

class PriorityWorkStealingQueue:
    """
    Priority queue variant: owner always gets the highest-priority task.
    Thieves steal only from the low-priority tail to preserve priority ordering.
    """

    def __init__(self):
        self._heap: list = []   # min-heap by priority
        self._low_priority_buffer: list = []  # tasks eligible for stealing
        self._steal_priority_threshold: int = 5  # tasks with priority > threshold are stealable
        self._lock = asyncio.Lock()

    async def push(self, task: PriorityTask) -> None:
        async with self._lock:
            heapq.heappush(self._heap, task)

    async def pop(self) -> Optional[PriorityTask]:
        async with self._lock:
            if self._heap:
                return heapq.heappop(self._heap)
            return None

    async def steal(self) -> Optional[PriorityTask]:
        """Only steal low-priority tasks to avoid disrupting hot tasks."""
        async with self._lock:
            stealable = [t for t in self._heap if t.priority > self._steal_priority_threshold]
            if not stealable:
                return None
            # Remove the lowest-priority (highest number) task
            victim = max(stealable, key=lambda t: t.priority)
            self._heap.remove(victim)
            heapq.heapify(self._heap)
            return victim

    def stealable_count(self) -> int:
        return sum(1 for t in self._heap if t.priority > self._steal_priority_threshold)

    def total_size(self) -> int:
        return len(self._heap)
```

## Solution 4: Distributed Work Stealing via Redis

```python
import asyncio
import json
import time
import uuid
from typing import Optional

class RedisWorkStealingQueue:
    """
    Redis-backed work-stealing queue for distributed agent instances
    across multiple processes. Uses RPOPLPUSH for atomic steal operations.
    """

    def __init__(self, redis, instance_id: str, namespace: str = "wsteal"):
        self._redis = redis
        self._instance_id = instance_id
        self._own_key = f"{namespace}:queue:{instance_id}"
        self._namespace = namespace

    async def push(self, task: dict) -> None:
        await self._redis.lpush(self._own_key, json.dumps(task))

    async def pop(self) -> Optional[dict]:
        """Pop from own queue (non-blocking)."""
        data = await self._redis.lpop(self._own_key)
        return json.loads(data) if data else None

    async def steal_from(self, victim_instance_id: str) -> Optional[dict]:
        """
        Atomically move a task from victim's tail to own queue.
        RPOPLPUSH is atomic: no task is lost if the thief crashes.
        """
        victim_key = f"{self._namespace}:queue:{victim_instance_id}"
        data = await self._redis.rpoplpush(victim_key, self._own_key)
        if data:
            # Immediately pop it back out from own queue to process
            result = await self._redis.lpop(self._own_key)
            return json.loads(result) if result else None
        return None

    async def queue_lengths(self) -> dict:
        """Returns queue lengths for all known instances."""
        pattern = f"{self._namespace}:queue:*"
        keys = await self._redis.keys(pattern)
        lengths = {}
        for key in keys:
            instance = key.decode().split(":")[-1]
            lengths[instance] = await self._redis.llen(key)
        return lengths

    async def try_steal(self) -> Optional[dict]:
        """Find most overloaded peer and steal one task."""
        lengths = await self.queue_lengths()
        own_len = lengths.get(self._instance_id, 0)
        candidates = {
            iid: l for iid, l in lengths.items()
            if iid != self._instance_id and l > own_len + 2
        }
        if not candidates:
            return None
        victim = max(candidates, key=lambda k: candidates[k])
        return await self.steal_from(victim)
```

## Solution 5: Adaptive Steal Threshold Based on Queue Imbalance

```python
import asyncio
import time
from typing import Dict

class AdaptiveWorkStealingController:
    """
    Dynamically adjusts the steal threshold based on observed imbalance.
    High imbalance → lower threshold (steal more aggressively).
    Low imbalance → higher threshold (reduce steal overhead).
    """

    def __init__(self, pool: WorkStealingPool, target_imbalance_ratio: float = 0.2):
        self._pool = pool
        self._target = target_imbalance_ratio
        self._steal_threshold = 2

    def _current_imbalance(self) -> float:
        sizes = [q.size() for q in self._pool._queues.values()]
        if not sizes or max(sizes) == 0:
            return 0.0
        return (max(sizes) - min(sizes)) / max(sizes)

    async def adapt_loop(self, interval_seconds: float = 5.0) -> None:
        while True:
            await asyncio.sleep(interval_seconds)
            imbalance = self._current_imbalance()

            if imbalance > self._target + 0.1:
                # Too imbalanced — steal more aggressively
                self._steal_threshold = max(1, self._steal_threshold - 1)
            elif imbalance < self._target - 0.1:
                # Well-balanced — reduce steal overhead
                self._steal_threshold = min(10, self._steal_threshold + 1)

            self._pool._steal_threshold = self._steal_threshold
            print(
                f"[adaptive_steal] imbalance={imbalance:.2%} "
                f"threshold={self._steal_threshold}"
            )
```

## Solution 6: Work Stealing Metrics Dashboard

```python
import time
from typing import Dict, List

class WorkStealingMetrics:
    def __init__(self, pool: WorkStealingPool):
        self._pool = pool

    def summary(self) -> dict:
        stats_map = self._pool.pool_stats()
        total_executed = sum(s["tasks_executed"] for s in stats_map.values())
        total_stolen = sum(s["tasks_stolen"] for s in stats_map.values())
        total_donated = sum(s["tasks_donated"] for s in stats_map.values())
        idle_steals = sum(s["idle_steals_attempted"] for s in stats_map.values())

        avg_exec_ms = (
            sum(s["total_execution_ms"] for s in stats_map.values()) / max(total_executed, 1)
        )
        steal_rate = total_stolen / max(total_executed, 1)

        queue_sizes = {wid: self._pool._queues[wid].size() for wid in self._pool._queues}
        sizes = list(queue_sizes.values())
        imbalance = (max(sizes) - min(sizes)) / max(max(sizes), 1) if sizes else 0.0

        return {
            "workers": len(stats_map),
            "total_executed": total_executed,
            "total_stolen": total_stolen,
            "steal_rate": round(steal_rate, 4),
            "idle_steal_attempts": idle_steals,
            "avg_execution_ms": round(avg_exec_ms, 2),
            "current_queue_imbalance": round(imbalance, 4),
            "queue_sizes": queue_sizes,
            "per_worker": stats_map,
        }

    def print_summary(self) -> None:
        s = self.summary()
        print(
            f"[work_stealing] workers={s['workers']} executed={s['total_executed']} "
            f"stolen={s['total_stolen']} steal_rate={s['steal_rate']:.1%} "
            f"imbalance={s['current_queue_imbalance']:.1%} "
            f"avg_ms={s['avg_execution_ms']:.1f}"
        )
```

## Comparison

| Approach | Concurrency Model | Distributed | Priority-Aware | Adaptive |
|---|---|---|---|---|
| WorkStealingDeque | In-process asyncio | No | No | No |
| WorkStealingPool | In-process, N workers | No | No | Via AdaptiveController |
| PriorityWorkStealingQueue | In-process asyncio | No | Yes | No |
| RedisWorkStealingQueue | Multi-process via Redis | Yes | No | No |
| AdaptiveWorkStealingController | Wraps any pool | N/A | N/A | Yes (auto-tune threshold) |
| WorkStealingMetrics | Reporting layer | N/A | N/A | N/A |

**Best for production**: Use `WorkStealingPool` for single-process multi-coroutine agent pools. Use `RedisWorkStealingQueue` for multi-process or multi-host agent deployments. Wrap with `AdaptiveWorkStealingController` to auto-tune the steal threshold based on observed imbalance, and `WorkStealingMetrics` to expose the steal rate in your observability stack.
