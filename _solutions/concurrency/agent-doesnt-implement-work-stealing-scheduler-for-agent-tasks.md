---
title: "Agent Doesn't Implement Work-Stealing Scheduler for Agent Tasks"
description: "Six solutions for balancing task load across agent workers using work-stealing, adaptive routing, and dynamic rebalancing strategies."
difficulty: advanced
category: concurrency
tags: [work-stealing, scheduling, load-balancing, asyncio, workers, throughput]
---

# Agent Doesn't Implement Work-Stealing Scheduler for Agent Tasks

Without load balancing, agent workers starve while others are overloaded: one worker handles 40 tasks while three sit idle. Work-stealing distributes tasks dynamically so idle workers pull from overloaded peers' queues. These six solutions range from simple deque-based stealing to priority-aware and latency-adaptive schedulers.

## Solution 1: Classic Deque-Based Work Stealing

Each worker owns a double-ended queue; idle workers steal from the tail of busiest peers.

```python
import asyncio
import random
import uuid
from collections import deque
from dataclasses import dataclass, field
from anthropic import AsyncAnthropic


@dataclass
class Task:
    task_id: str
    message: str
    result: asyncio.Future = field(default_factory=asyncio.Future)


class WorkStealingWorker:
    def __init__(self, worker_id: str, client: AsyncAnthropic):
        self.worker_id = worker_id
        self.client = client
        self.deque: deque[Task] = deque()
        self._lock = asyncio.Lock()
        self.tasks_processed = 0

    async def push(self, task: Task):
        async with self._lock:
            self.deque.appendleft(task)  # Push to left (local end)

    async def pop(self) -> Task | None:
        async with self._lock:
            if self.deque:
                return self.deque.popleft()
        return None

    async def steal(self) -> Task | None:
        """Steal from right end (oldest task)."""
        async with self._lock:
            if len(self.deque) > 1:  # Leave at least one for owner
                return self.deque.pop()
        return None

    async def run(self, all_workers: list["WorkStealingWorker"]):
        model = "claude-haiku-4-5-20251001"
        while True:
            task = await self.pop()
            if task is None:
                # Try stealing from a random other worker
                candidates = [w for w in all_workers if w is not self]
                if candidates:
                    victim = max(candidates, key=lambda w: len(w.deque))
                    task = await victim.steal()
            if task is None:
                await asyncio.sleep(0.01)
                continue

            try:
                response = await self.client.messages.create(
                    model=model,
                    max_tokens=256,
                    messages=[{"role": "user", "content": task.message}],
                )
                task.result.set_result(response.content[0].text)
                self.tasks_processed += 1
            except Exception as e:
                task.result.set_exception(e)


class WorkStealingScheduler:
    def __init__(self, n_workers: int = 4):
        self.client = AsyncAnthropic()
        self.workers = [
            WorkStealingWorker(f"worker-{i}", self.client)
            for i in range(n_workers)
        ]
        self._round_robin_idx = 0
        self._runner_tasks: list[asyncio.Task] = []

    async def start(self):
        self._runner_tasks = [
            asyncio.create_task(w.run(self.workers))
            for w in self.workers
        ]

    async def stop(self):
        for t in self._runner_tasks:
            t.cancel()

    async def submit(self, message: str) -> asyncio.Future:
        """Submit to the least-loaded worker."""
        task = Task(task_id=str(uuid.uuid4()), message=message)
        worker = min(self.workers, key=lambda w: len(w.deque))
        await worker.push(task)
        return task.result

    async def run_all(self, messages: list[str]) -> list[str]:
        await self.start()
        futures = [await self.submit(m) for m in messages]
        results = await asyncio.gather(*futures)
        await self.stop()
        print("\n=== Worker stats ===")
        for w in self.workers:
            print(f"  {w.worker_id}: processed={w.tasks_processed}")
        return list(results)


async def demo_work_stealing():
    scheduler = WorkStealingScheduler(n_workers=4)
    messages = [f"What is {i} squared?" for i in range(16)]
    results = await scheduler.run_all(messages)
    print(f"Got {len(results)} results")
```

## Solution 2: Priority-Aware Work Stealing with Task Classes

Tasks carry priority; stealing always takes the highest-priority task from the victim's queue.

```python
import asyncio
import heapq
import time
import uuid
from dataclasses import dataclass, field
from enum import IntEnum
from anthropic import AsyncAnthropic


class Priority(IntEnum):
    CRITICAL = 0
    HIGH = 1
    NORMAL = 2
    LOW = 3


@dataclass(order=True)
class PriorityTask:
    priority: Priority
    enqueued_at: float = field(default_factory=time.time)
    task_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    message: str = field(default="", compare=False)
    result: asyncio.Future = field(default_factory=asyncio.Future, compare=False)


class PriorityWorker:
    def __init__(self, worker_id: str):
        self.worker_id = worker_id
        self._heap: list[PriorityTask] = []
        self._lock = asyncio.Lock()
        self.processed = 0

    async def push(self, task: PriorityTask):
        async with self._lock:
            heapq.heappush(self._heap, task)

    async def pop(self) -> PriorityTask | None:
        async with self._lock:
            if self._heap:
                return heapq.heappop(self._heap)
        return None

    async def steal_highest_priority(self) -> PriorityTask | None:
        """Steal highest-priority task (index 0 in heap)."""
        async with self._lock:
            if len(self._heap) > 1:
                # Remove and return the root (highest priority)
                task = heapq.heappop(self._heap)
                return task
        return None

    @property
    def queue_size(self) -> int:
        return len(self._heap)

    @property
    def top_priority(self) -> int:
        if self._heap:
            return self._heap[0].priority
        return Priority.LOW + 1  # Sentinel: higher number = less urgent


class PriorityWorkStealingPool:
    def __init__(self, n_workers: int = 4):
        self.client = AsyncAnthropic()
        self.workers = [PriorityWorker(f"w{i}") for i in range(n_workers)]
        self._tasks: list[asyncio.Task] = []

    async def _worker_loop(self, worker: PriorityWorker):
        while True:
            task = await worker.pop()
            if task is None:
                # Find most urgent task across all peers
                peers = [w for w in self.workers if w is not worker and w.queue_size > 0]
                if peers:
                    # Steal from worker with most urgent (lowest priority int) task
                    victim = min(peers, key=lambda w: w.top_priority)
                    task = await victim.steal_highest_priority()
            if task is None:
                await asyncio.sleep(0.005)
                continue

            try:
                response = await self.client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=256,
                    messages=[{"role": "user", "content": task.message}],
                )
                task.result.set_result(response.content[0].text)
                worker.processed += 1
            except Exception as e:
                task.result.set_exception(e)

    async def start(self):
        self._tasks = [
            asyncio.create_task(self._worker_loop(w)) for w in self.workers
        ]

    async def stop(self):
        for t in self._tasks:
            t.cancel()

    async def submit(
        self, message: str, priority: Priority = Priority.NORMAL
    ) -> asyncio.Future:
        task = PriorityTask(priority=priority, message=message)
        # Route critical tasks to least-loaded worker; others round-robin
        if priority == Priority.CRITICAL:
            worker = min(self.workers, key=lambda w: w.queue_size)
        else:
            worker = random.choice(self.workers)
        await worker.push(task)
        return task.result


import random


async def demo_priority_stealing():
    pool = PriorityWorkStealingPool(n_workers=3)
    await pool.start()

    futures = []
    priorities = [Priority.LOW, Priority.NORMAL, Priority.HIGH, Priority.CRITICAL]
    for i in range(12):
        p = priorities[i % 4]
        f = await pool.submit(f"Task {i}: priority={p.name}", priority=p)
        futures.append((p, f))

    results = await asyncio.gather(*[f for _, f in futures])
    await pool.stop()

    for (p, _), r in zip(futures, results):
        print(f"[{p.name:8s}] {r[:60]}")
```

## Solution 3: Latency-Adaptive Routing with Work Stealing Fallback

Primary routing sends tasks to the fastest workers (lowest EMA latency); stealing kicks in on queue buildup.

```python
import asyncio
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from anthropic import AsyncAnthropic


@dataclass
class LatencyStats:
    ema: float = 1.0
    alpha: float = 0.2
    sample_count: int = 0

    def update(self, latency: float):
        self.sample_count += 1
        self.ema = self.alpha * latency + (1 - self.alpha) * self.ema


@dataclass
class AdaptiveTask:
    task_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    message: str = ""
    enqueued_at: float = field(default_factory=time.time)
    result: asyncio.Future = field(default_factory=asyncio.Future)


class AdaptiveWorker:
    STEAL_THRESHOLD = 3  # Steal if victim has this many more tasks

    def __init__(self, worker_id: str, client: AsyncAnthropic):
        self.worker_id = worker_id
        self.client = client
        self.queue: deque[AdaptiveTask] = deque()
        self.latency = LatencyStats()
        self._lock = asyncio.Lock()
        self.idle_steals = 0

    @property
    def queue_size(self) -> int:
        return len(self.queue)

    @property
    def estimated_wait(self) -> float:
        return self.queue_size * self.latency.ema

    async def enqueue(self, task: AdaptiveTask):
        async with self._lock:
            self.queue.append(task)

    async def dequeue(self) -> AdaptiveTask | None:
        async with self._lock:
            if self.queue:
                return self.queue.popleft()
        return None

    async def steal_from(self, victim: "AdaptiveWorker") -> AdaptiveTask | None:
        async with victim._lock:
            if len(victim.queue) > self.STEAL_THRESHOLD:
                return victim.queue.pop()  # Take oldest
        return None

    async def run(self, all_workers: list["AdaptiveWorker"]):
        while True:
            task = await self.dequeue()
            if task is None:
                # Find most overloaded peer
                candidates = [
                    w for w in all_workers
                    if w is not self and w.queue_size > self.STEAL_THRESHOLD
                ]
                if candidates:
                    victim = max(candidates, key=lambda w: w.queue_size)
                    task = await self.steal_from(victim)
                    if task:
                        self.idle_steals += 1
            if task is None:
                await asyncio.sleep(0.01)
                continue

            start = time.perf_counter()
            try:
                response = await self.client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=256,
                    messages=[{"role": "user", "content": task.message}],
                )
                elapsed = time.perf_counter() - start
                self.latency.update(elapsed)
                task.result.set_result(response.content[0].text)
            except Exception as e:
                elapsed = time.perf_counter() - start
                self.latency.update(elapsed)
                task.result.set_exception(e)


class AdaptiveScheduler:
    def __init__(self, n_workers: int = 4):
        self.client = AsyncAnthropic()
        self.workers = [AdaptiveWorker(f"w{i}", self.client) for i in range(n_workers)]
        self._runner_tasks: list[asyncio.Task] = []

    async def start(self):
        self._runner_tasks = [
            asyncio.create_task(w.run(self.workers)) for w in self.workers
        ]

    async def stop(self):
        for t in self._runner_tasks:
            t.cancel()
        print("\n=== Adaptive Scheduler Stats ===")
        for w in self.workers:
            print(
                f"  {w.worker_id}: ema_latency={w.latency.ema:.2f}s "
                f"samples={w.latency.sample_count} steals={w.idle_steals}"
            )

    async def submit(self, message: str) -> asyncio.Future:
        """Route to worker with smallest estimated wait time."""
        task = AdaptiveTask(message=message)
        best = min(self.workers, key=lambda w: w.estimated_wait)
        await best.enqueue(task)
        return task.result

    async def run_batch(self, messages: list[str]) -> list[str]:
        await self.start()
        futures = [await self.submit(m) for m in messages]
        results = await asyncio.gather(*futures)
        await self.stop()
        return list(results)
```

## Solution 4: Token-Budget-Aware Task Routing

Route large tasks (estimated high token usage) to workers with remaining token budget; steal only budget-compatible tasks.

```python
import asyncio
import time
import uuid
from dataclasses import dataclass, field
from collections import deque
from anthropic import AsyncAnthropic


@dataclass
class TokenBudgetTask:
    task_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    message: str = ""
    estimated_tokens: int = 500  # Caller hint
    result: asyncio.Future = field(default_factory=asyncio.Future)


class BudgetedWorker:
    def __init__(
        self,
        worker_id: str,
        client: AsyncAnthropic,
        tokens_per_minute: int = 10_000,
    ):
        self.worker_id = worker_id
        self.client = client
        self.tpm_limit = tokens_per_minute
        self._tokens_used_this_minute = 0
        self._window_start = time.time()
        self._queue: deque[TokenBudgetTask] = deque()
        self._lock = asyncio.Lock()
        self.processed = 0
        self.rejected = 0

    @property
    def tokens_remaining(self) -> int:
        now = time.time()
        if now - self._window_start >= 60:
            self._tokens_used_this_minute = 0
            self._window_start = now
        return max(0, self.tpm_limit - self._tokens_used_this_minute)

    def can_accept(self, estimated_tokens: int) -> bool:
        return self.tokens_remaining >= estimated_tokens

    async def enqueue(self, task: TokenBudgetTask) -> bool:
        if not self.can_accept(task.estimated_tokens):
            return False
        async with self._lock:
            self._queue.append(task)
        return True

    async def steal(self, max_tokens: int) -> TokenBudgetTask | None:
        """Steal a task that fits in caller's remaining budget."""
        async with self._lock:
            for i, task in enumerate(self._queue):
                if task.estimated_tokens <= max_tokens and len(self._queue) > 1:
                    del self._queue[i]
                    return task
        return None

    async def run(self, all_workers: list["BudgetedWorker"]):
        while True:
            task: TokenBudgetTask | None = None
            async with self._lock:
                if self._queue:
                    task = self._queue.popleft()

            if task is None:
                # Steal a budget-compatible task
                my_budget = self.tokens_remaining
                peers = [w for w in all_workers if w is not self and len(w._queue) > 0]
                for victim in sorted(peers, key=lambda w: len(w._queue), reverse=True):
                    task = await victim.steal(my_budget)
                    if task:
                        break

            if task is None:
                await asyncio.sleep(0.01)
                continue

            if not self.can_accept(task.estimated_tokens):
                # Re-queue for later
                async with self._lock:
                    self._queue.appendleft(task)
                await asyncio.sleep(0.5)
                continue

            try:
                response = await self.client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=min(task.estimated_tokens, 1024),
                    messages=[{"role": "user", "content": task.message}],
                )
                self._tokens_used_this_minute += response.usage.output_tokens
                task.result.set_result(response.content[0].text)
                self.processed += 1
            except Exception as e:
                task.result.set_exception(e)


class TokenAwareScheduler:
    def __init__(self, n_workers: int = 3, tpm_per_worker: int = 8000):
        self.client = AsyncAnthropic()
        self.workers = [
            BudgetedWorker(f"w{i}", self.client, tpm_per_worker)
            for i in range(n_workers)
        ]
        self._tasks: list[asyncio.Task] = []

    async def start(self):
        self._tasks = [
            asyncio.create_task(w.run(self.workers)) for w in self.workers
        ]

    async def stop(self):
        for t in self._tasks:
            t.cancel()

    async def submit(self, message: str, estimated_tokens: int = 500) -> asyncio.Future:
        task = TokenBudgetTask(message=message, estimated_tokens=estimated_tokens)
        # Try worker with most remaining budget
        for worker in sorted(self.workers, key=lambda w: w.tokens_remaining, reverse=True):
            if await worker.enqueue(task):
                return task.result
        # All workers over budget — queue in least-loaded
        min_worker = min(self.workers, key=lambda w: len(w._queue))
        async with min_worker._lock:
            min_worker._queue.append(task)
        return task.result
```

## Solution 5: Cooperative Work Sharing via Shared Work Queue with Affinity

Workers prefer tasks that share context with recent work (prompt affinity); steal only when their affinity queue is empty.

```python
import asyncio
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from anthropic import AsyncAnthropic


@dataclass
class AffinityTask:
    task_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    message: str = ""
    affinity_key: str = "default"  # Group related tasks (e.g., user_id, session_id)
    result: asyncio.Future = field(default_factory=asyncio.Future)


class AffinityWorker:
    def __init__(self, worker_id: str, client: AsyncAnthropic, affinity_keys: list[str]):
        self.worker_id = worker_id
        self.client = client
        self.affinity_keys = set(affinity_keys)
        self._local: deque[AffinityTask] = deque()
        self._lock = asyncio.Lock()
        self.processed = 0

    async def push(self, task: AffinityTask):
        async with self._lock:
            self._local.append(task)

    async def pop(self) -> AffinityTask | None:
        async with self._lock:
            # Prefer affinity tasks
            for i, t in enumerate(self._local):
                if t.affinity_key in self.affinity_keys:
                    del self._local[i]
                    return t
            if self._local:
                return self._local.popleft()
        return None

    async def steal_non_affinity(self) -> AffinityTask | None:
        async with self._lock:
            for i, t in enumerate(self._local):
                if t.affinity_key not in self.affinity_keys and len(self._local) > 1:
                    del self._local[i]
                    return t
        return None

    async def run(self, all_workers: list["AffinityWorker"]):
        while True:
            task = await self.pop()
            if task is None:
                # Steal from busiest worker — prefer tasks we have affinity for
                peers = [w for w in all_workers if w is not self and len(w._local) > 0]
                for victim in sorted(peers, key=lambda w: len(w._local), reverse=True):
                    task = await victim.steal_non_affinity()
                    if task:
                        break

            if task is None:
                await asyncio.sleep(0.01)
                continue

            try:
                response = await self.client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=256,
                    messages=[{"role": "user", "content": task.message}],
                )
                task.result.set_result(response.content[0].text)
                self.processed += 1
            except Exception as e:
                task.result.set_exception(e)


class AffinityScheduler:
    def __init__(self):
        self.client = AsyncAnthropic()
        self.workers = [
            AffinityWorker(f"w{i}", self.client, affinity_keys=[f"user_{i}", f"user_{i+4}"])
            for i in range(4)
        ]
        self._affinity_map: dict[str, AffinityWorker] = {}
        for w in self.workers:
            for key in w.affinity_keys:
                self._affinity_map[key] = w
        self._runner_tasks: list[asyncio.Task] = []

    async def start(self):
        self._runner_tasks = [
            asyncio.create_task(w.run(self.workers)) for w in self.workers
        ]

    async def stop(self):
        for t in self._runner_tasks:
            t.cancel()

    async def submit(self, message: str, affinity_key: str = "default") -> asyncio.Future:
        task = AffinityTask(message=message, affinity_key=affinity_key)
        # Route to affinity worker if exists, else least-loaded
        worker = self._affinity_map.get(
            affinity_key,
            min(self.workers, key=lambda w: len(w._local)),
        )
        await worker.push(task)
        return task.result
```

## Solution 6: Metrics-Driven Rebalancer with Periodic Steal Decisions

A background rebalancer task monitors queue depths every N seconds and migrates tasks to underloaded workers.

```python
import asyncio
import time
import uuid
from dataclasses import dataclass, field
from collections import deque
from anthropic import AsyncAnthropic


@dataclass
class RebalancerTask:
    task_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    message: str = ""
    result: asyncio.Future = field(default_factory=asyncio.Future)
    submitted_at: float = field(default_factory=time.time)


class RebalancedWorker:
    def __init__(self, worker_id: str, client: AsyncAnthropic):
        self.worker_id = worker_id
        self.client = client
        self._queue: deque[RebalancerTask] = deque()
        self._lock = asyncio.Lock()
        self.processed = 0
        self.received_migrations = 0

    @property
    def queue_depth(self) -> int:
        return len(self._queue)

    async def enqueue(self, task: RebalancerTask):
        async with self._lock:
            self._queue.append(task)

    async def migrate_out(self, n: int) -> list[RebalancerTask]:
        """Remove n tasks from tail for migration."""
        async with self._lock:
            migrated = []
            for _ in range(min(n, len(self._queue) - 1)):
                migrated.append(self._queue.pop())
            return migrated

    async def run(self):
        while True:
            task: RebalancerTask | None = None
            async with self._lock:
                if self._queue:
                    task = self._queue.popleft()
            if task is None:
                await asyncio.sleep(0.01)
                continue
            try:
                response = await self.client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=256,
                    messages=[{"role": "user", "content": task.message}],
                )
                task.result.set_result(response.content[0].text)
                self.processed += 1
            except Exception as e:
                task.result.set_exception(e)


class RebalancingScheduler:
    def __init__(self, n_workers: int = 4, rebalance_interval: float = 2.0):
        self.client = AsyncAnthropic()
        self.workers = [RebalancedWorker(f"w{i}", self.client) for i in range(n_workers)]
        self.rebalance_interval = rebalance_interval
        self._tasks: list[asyncio.Task] = []
        self._rebalance_count = 0

    async def _rebalancer(self):
        while True:
            await asyncio.sleep(self.rebalance_interval)
            depths = [w.queue_depth for w in self.workers]
            avg = sum(depths) / len(depths)
            overloaded = [w for w in self.workers if w.queue_depth > avg * 1.5]
            underloaded = [w for w in self.workers if w.queue_depth < avg * 0.5]
            if not overloaded or not underloaded:
                continue

            for victim in overloaded:
                excess = int(victim.queue_depth - avg)
                if excess <= 0:
                    continue
                migrated = await victim.migrate_out(excess)
                for task in migrated:
                    target = min(underloaded, key=lambda w: w.queue_depth)
                    await target.enqueue(task)
                    target.received_migrations += 1
                self._rebalance_count += 1
                print(
                    f"[REBALANCE] Moved {len(migrated)} tasks from "
                    f"{victim.worker_id} -> {target.worker_id}"
                )

    async def start(self):
        self._tasks = [asyncio.create_task(w.run()) for w in self.workers]
        self._tasks.append(asyncio.create_task(self._rebalancer()))

    async def stop(self):
        for t in self._tasks:
            t.cancel()
        print(f"\nTotal rebalance events: {self._rebalance_count}")
        for w in self.workers:
            print(
                f"  {w.worker_id}: processed={w.processed} "
                f"migrations_received={w.received_migrations}"
            )

    async def submit(self, message: str) -> asyncio.Future:
        task = RebalancerTask(message=message)
        worker = min(self.workers, key=lambda w: w.queue_depth)
        await worker.enqueue(task)
        return task.result

    async def run_batch(self, messages: list[str]) -> list[str]:
        await self.start()
        # Submit in bursts to create imbalance
        futures = []
        for i, msg in enumerate(messages):
            # First half goes to worker 0 to create initial imbalance
            if i < len(messages) // 2:
                task = RebalancerTask(message=msg)
                await self.workers[0].enqueue(task)
                futures.append(task.result)
            else:
                futures.append(await self.submit(msg))
        results = await asyncio.gather(*futures)
        await self.stop()
        return list(results)
```

## Comparison Table

| Solution | Stealing Strategy | Priority Support | Token Awareness | Rebalancing | Best For |
|---|---|---|---|---|---|
| Classic Deque | Steal from busiest tail | No | No | Continuous | General-purpose task pools |
| Priority-Aware | Steal highest-priority task | Yes (IntEnum) | No | Continuous | Mixed-urgency workloads |
| Latency-Adaptive | Steal when queue > threshold | No | No | Continuous | Heterogeneous task durations |
| Token-Budget | Steal budget-compatible only | No | Yes | On enqueue | Rate-limited API pools |
| Affinity | Steal non-affinity tasks first | No | No | Continuous | Session/user-partitioned work |
| Metrics Rebalancer | Periodic bulk migration | No | No | Periodic (2s) | Batch processing pipelines |

**Recommended**: Use **Classic Deque** (Solution 1) as a baseline for most asyncio agent pools. Add **Latency-Adaptive** (Solution 3) when task durations vary significantly. Use **Token-Budget** (Solution 4) when operating near API rate limits. Combine **Priority-Aware** with **Affinity** for production multi-tenant systems.
