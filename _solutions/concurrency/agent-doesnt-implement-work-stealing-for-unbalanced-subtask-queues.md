---
layout: solution
title: "Agent Doesn't Implement Work-Stealing for Unbalanced Subtask Queues"
category: concurrency
description: "Implement work-stealing schedulers so idle workers pull tasks from overloaded workers, preventing starvation and maximizing CPU/API throughput across heterogeneous subtask loads."
tags: [concurrency, work-stealing, scheduling, load-balancing, async, parallelism, throughput]
---

# Agent Doesn't Implement Work-Stealing for Unbalanced Subtask Queues

## Problem

Static worker assignment leaves fast workers idle while slow workers overflow. A parallel agent that splits 100 tasks evenly across 4 workers may finish with worker A done in 2 s while workers B–D each hold 30 s tasks. The pipeline idles at 75% efficiency. Without work-stealing, the fix is manual load balancing — which requires predicting task durations you don't know in advance.

## Solution Options

### Option 1: Simple Shared-Queue Work-Stealing

```python
import anthropic
import asyncio
import random
from dataclasses import dataclass


@dataclass
class Task:
    task_id: int
    prompt: str
    estimated_tokens: int = 100  # hint for load estimation


async def process_task(client: anthropic.AsyncAnthropic, task: Task) -> dict:
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=task.estimated_tokens,
        messages=[{"role": "user", "content": task.prompt}],
    )
    return {"task_id": task.task_id, "result": resp.content[0].text[:60]}


async def worker(
    worker_id: int,
    shared_queue: asyncio.Queue,
    client: anthropic.AsyncAnthropic,
    results: list,
) -> None:
    while True:
        try:
            task: Task = shared_queue.get_nowait()
        except asyncio.QueueEmpty:
            break
        result = await process_task(client, task)
        result["worker_id"] = worker_id
        results.append(result)
        shared_queue.task_done()


async def main() -> None:
    client = anthropic.AsyncAnthropic()
    # Heterogeneous tasks — some short, some long
    tasks = [
        Task(i, f"Define term {i} in one word", estimated_tokens=10 if i % 3 == 0 else 128)
        for i in range(12)
    ]
    random.shuffle(tasks)  # ensure uneven natural distribution

    queue: asyncio.Queue = asyncio.Queue()
    for t in tasks:
        await queue.put(t)

    results: list = []
    # 4 workers all pulling from one shared queue — natural work-stealing
    workers = [worker(i, queue, client, results) for i in range(4)]
    await asyncio.gather(*workers)

    for r in sorted(results, key=lambda x: x["task_id"]):
        print(f"[task={r['task_id']:02d}][worker={r['worker_id']}] {r['result']}")

    await client.close()


if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: No extra tokens; idle workers drain queue instead of sitting empty
# Environment: Batch pipelines with heterogeneous task durations and a fixed worker count
```

---

### Option 2: Per-Worker Deque with Steal-from-Back

```python
import anthropic
import asyncio
import random
from collections import deque
from dataclasses import dataclass, field


@dataclass
class Task:
    task_id: int
    prompt: str


class WorkerDeque:
    """Double-ended queue: worker pops from front (LIFO locality); stealer pops from back."""

    def __init__(self) -> None:
        self._deque: deque[Task] = deque()
        self._lock = asyncio.Lock()

    async def push(self, task: Task) -> None:
        async with self._lock:
            self._deque.appendleft(task)

    async def pop_local(self) -> Task | None:
        async with self._lock:
            return self._deque.popleft() if self._deque else None

    async def steal(self) -> Task | None:
        async with self._lock:
            return self._deque.pop() if self._deque else None

    def __len__(self) -> int:
        return len(self._deque)


async def worker(
    worker_id: int,
    own_deque: WorkerDeque,
    all_deques: list[WorkerDeque],
    client: anthropic.AsyncAnthropic,
    results: list,
    stats: dict,
) -> None:
    steals = 0
    processed = 0
    while True:
        task = await own_deque.pop_local()

        if task is None:
            # Attempt to steal from the busiest other worker
            victim = max(
                (d for i, d in enumerate(all_deques) if i != worker_id),
                key=len,
                default=None,
            )
            if victim and len(victim) > 0:
                task = await victim.steal()
                if task:
                    steals += 1

        if task is None:
            # Check if all queues are truly empty
            if all(len(d) == 0 for d in all_deques):
                break
            await asyncio.sleep(0.001)
            continue

        resp = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=64,
            messages=[{"role": "user", "content": task.prompt}],
        )
        results.append({
            "task_id": task.task_id,
            "worker_id": worker_id,
            "stolen": steals > 0,
            "result": resp.content[0].text[:40],
        })
        processed += 1

    stats[worker_id] = {"processed": processed, "steals": steals}


async def main() -> None:
    client = anthropic.AsyncAnthropic()
    num_workers = 4
    tasks = [Task(i, f"Explain concept {i} briefly") for i in range(16)]

    deques = [WorkerDeque() for _ in range(num_workers)]
    # Initial uneven distribution: worker 0 gets 12 tasks, others get fewer
    distribution = [12, 2, 1, 1]
    idx = 0
    for i, count in enumerate(distribution):
        for _ in range(count):
            if idx < len(tasks):
                await deques[i].push(tasks[idx])
                idx += 1

    results: list = []
    stats: dict = {}
    await asyncio.gather(
        *[worker(i, deques[i], deques, client, results, stats) for i in range(num_workers)]
    )

    print("=== Work-Stealing Stats ===")
    for wid, s in stats.items():
        print(f"  Worker {wid}: processed={s['processed']}, steals={s['steals']}")

    await client.close()


if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: No extra tokens; deque-based stealing gives locality + load balance
# Environment: Fine-grained parallel subtask execution with known hot workers
```

---

### Option 3: Priority Work-Stealing with Task Weights

```python
import anthropic
import asyncio
import heapq
import time
from dataclasses import dataclass, field


@dataclass(order=True)
class WeightedTask:
    weight: int  # higher = heavier; stealer prefers lighter tasks
    task_id: int = field(compare=False)
    prompt: str = field(compare=False)


class WeightedWorkerQueue:
    def __init__(self, worker_id: int) -> None:
        self.worker_id = worker_id
        self._heap: list[WeightedTask] = []
        self._lock = asyncio.Lock()

    async def push(self, task: WeightedTask) -> None:
        async with self._lock:
            heapq.heappush(self._heap, task)

    async def pop(self) -> WeightedTask | None:
        async with self._lock:
            return heapq.heappop(self._heap) if self._heap else None

    async def steal_lightest(self) -> WeightedTask | None:
        """Steal the lightest task (most likely to finish quickly)."""
        async with self._lock:
            if not self._heap:
                return None
            # Find index of minimum weight
            min_idx = self._heap.index(min(self._heap))
            task = self._heap[min_idx]
            self._heap[min_idx] = self._heap[-1]
            self._heap.pop()
            heapq.heapify(self._heap)
            return task

    def total_weight(self) -> int:
        return sum(t.weight for t in self._heap)

    def __len__(self) -> int:
        return len(self._heap)


async def weighted_worker(
    worker_id: int,
    own_queue: WeightedWorkerQueue,
    all_queues: list[WeightedWorkerQueue],
    client: anthropic.AsyncAnthropic,
    results: list,
) -> None:
    while True:
        task = await own_queue.pop()

        if task is None:
            # Steal from most overloaded queue (by total weight)
            victim = max(
                (q for q in all_queues if q.worker_id != worker_id),
                key=lambda q: q.total_weight(),
                default=None,
            )
            if victim and victim.total_weight() > 0:
                task = await victim.steal_lightest()

        if task is None:
            if all(len(q) == 0 for q in all_queues):
                break
            await asyncio.sleep(0.002)
            continue

        start = time.perf_counter()
        resp = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=task.weight * 2,
            messages=[{"role": "user", "content": task.prompt}],
        )
        elapsed = (time.perf_counter() - start) * 1000
        results.append({
            "task_id": task.task_id,
            "worker_id": worker_id,
            "weight": task.weight,
            "elapsed_ms": round(elapsed),
            "result": resp.content[0].text[:40],
        })


async def main() -> None:
    client = anthropic.AsyncAnthropic()
    num_workers = 3

    # Mix of light (weight=1) and heavy (weight=64) tasks
    tasks = [
        WeightedTask(weight=1, task_id=i, prompt=f"Say '{i}'")
        for i in range(6)
    ] + [
        WeightedTask(weight=64, task_id=i + 6, prompt=f"Explain topic {i} in detail")
        for i in range(6)
    ]

    queues = [WeightedWorkerQueue(i) for i in range(num_workers)]
    # Dump all tasks onto worker 0 (extreme imbalance)
    for t in tasks:
        await queues[0].push(t)

    results: list = []
    await asyncio.gather(
        *[weighted_worker(i, queues[i], queues, client, results) for i in range(num_workers)]
    )

    by_worker: dict[int, int] = {}
    for r in results:
        by_worker[r["worker_id"]] = by_worker.get(r["worker_id"], 0) + 1
    print("Tasks per worker:", by_worker)

    await client.close()


if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: No extra tokens; lightest-task stealing minimizes latency tail
# Environment: Mixed-complexity batch jobs where task cost varies by orders of magnitude
```

---

### Option 4: Adaptive Work-Stealing with Load Monitor

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass


@dataclass
class Task:
    task_id: int
    prompt: str
    max_tokens: int = 128


class AdaptiveStealingPool:
    """
    Workers monitor their own throughput and steal
    proactively when their rate drops below the pool average.
    """

    def __init__(self, num_workers: int) -> None:
        self.num_workers = num_workers
        self._queues: list[asyncio.Queue] = [asyncio.Queue() for _ in range(num_workers)]
        self._completed = [0] * num_workers
        self._steal_count = [0] * num_workers
        self._start_time = time.monotonic()

    def distribute(self, tasks: list[Task]) -> None:
        """Round-robin initial distribution."""
        for i, task in enumerate(tasks):
            self._queues[i % self.num_workers].put_nowait(task)

    def _throughput(self, worker_id: int) -> float:
        elapsed = time.monotonic() - self._start_time
        return self._completed[worker_id] / max(elapsed, 0.001)

    def _pool_avg_throughput(self) -> float:
        elapsed = time.monotonic() - self._start_time
        return sum(self._completed) / max(elapsed, 0.001)

    async def _try_steal(self, worker_id: int) -> Task | None:
        busiest = max(
            range(self.num_workers),
            key=lambda i: self._queues[i].qsize() if i != worker_id else -1,
        )
        if busiest == worker_id:
            return None
        if self._queues[busiest].qsize() <= 1:
            return None
        try:
            task = self._queues[busiest].get_nowait()
            self._queues[busiest].task_done()
            self._steal_count[worker_id] += 1
            return task
        except asyncio.QueueEmpty:
            return None

    async def run_worker(
        self,
        worker_id: int,
        client: anthropic.AsyncAnthropic,
        results: list,
    ) -> None:
        while True:
            task: Task | None = None

            # Check if our throughput is below pool average → steal
            my_rate = self._throughput(worker_id)
            pool_rate = self._pool_avg_throughput()
            if my_rate < pool_rate * 0.5 and self._completed[worker_id] > 0:
                task = await self._try_steal(worker_id)

            if task is None:
                try:
                    task = self._queues[worker_id].get_nowait()
                except asyncio.QueueEmpty:
                    task = await self._try_steal(worker_id)

            if task is None:
                if all(q.empty() for q in self._queues):
                    break
                await asyncio.sleep(0.005)
                continue

            resp = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=task.max_tokens,
                messages=[{"role": "user", "content": task.prompt}],
            )
            self._completed[worker_id] += 1
            results.append({
                "task_id": task.task_id,
                "worker_id": worker_id,
                "result": resp.content[0].text[:40],
            })

    async def run(self, client: anthropic.AsyncAnthropic) -> list:
        results: list = []
        await asyncio.gather(
            *[self.run_worker(i, client, results) for i in range(self.num_workers)]
        )
        print("Completed per worker:", self._completed)
        print("Steals per worker:", self._steal_count)
        return results


async def main() -> None:
    client = anthropic.AsyncAnthropic()
    pool = AdaptiveStealingPool(num_workers=4)

    tasks = [Task(i, f"Name one {['fruit', 'animal', 'country', 'color'][i % 4]}") for i in range(20)]
    pool.distribute(tasks)

    results = await pool.run(client)
    print(f"Total results: {len(results)}")
    await client.close()


if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: No extra tokens; throughput-triggered stealing self-tunes to actual API latency
# Environment: Production agents where network jitter causes per-worker throughput variance
```

---

### Option 5: Work-Stealing with Continuation Tasks

```python
import anthropic
import asyncio
import uuid
from dataclasses import dataclass, field


@dataclass
class Task:
    task_id: str = field(default_factory=lambda: uuid.uuid4().hex[:6])
    prompt: str = ""
    continuation_of: str | None = None  # parent task ID


class ContinuationStealingPool:
    """
    Supports continuation tasks — a task can spawn follow-up tasks
    that are enqueued back into the shared pool for any idle worker.
    This enables recursive fan-out with automatic load balancing.
    """

    def __init__(self, num_workers: int) -> None:
        self.num_workers = num_workers
        self._queue: asyncio.Queue[Task | None] = asyncio.Queue()
        self._active = 0
        self._lock = asyncio.Lock()
        self._done = asyncio.Event()

    def submit(self, task: Task) -> None:
        self._queue.put_nowait(task)

    async def _worker(
        self,
        worker_id: int,
        client: anthropic.AsyncAnthropic,
        results: list,
    ) -> None:
        while True:
            try:
                task = await asyncio.wait_for(self._queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                async with self._lock:
                    if self._active == 0 and self._queue.empty():
                        self._done.set()
                        return
                continue

            if task is None:
                return

            async with self._lock:
                self._active += 1

            resp = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=64,
                messages=[{"role": "user", "content": task.prompt}],
            )
            text = resp.content[0].text.strip()
            results.append({
                "task_id": task.task_id,
                "parent": task.continuation_of,
                "worker_id": worker_id,
                "result": text[:50],
            })

            # Fan-out: each task spawns 2 follow-ups (depth capped externally)
            if task.continuation_of is None:  # only root tasks spawn children
                for i in range(2):
                    child = Task(
                        prompt=f"Follow-up {i} on: {text[:30]}",
                        continuation_of=task.task_id,
                    )
                    self.submit(child)

            async with self._lock:
                self._active -= 1
            self._queue.task_done()

    async def run(self, client: anthropic.AsyncAnthropic) -> list:
        results: list = []
        workers = [
            asyncio.create_task(self._worker(i, client, results))
            for i in range(self.num_workers)
        ]
        await self._done.wait()
        for w in workers:
            w.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        return results


async def main() -> None:
    client = anthropic.AsyncAnthropic()
    pool = ContinuationStealingPool(num_workers=4)

    root_tasks = [Task(prompt=f"Name a famous {topic}") for topic in ["scientist", "painter", "musician"]]
    for t in root_tasks:
        pool.submit(t)

    results = await pool.run(client)
    print(f"Total tasks processed: {len(results)} (3 roots → 6 children)")
    for r in results:
        parent = r["parent"] or "root"
        print(f"  [{r['worker_id']}] {r['task_id']} (parent={parent[:6]}) → {r['result']}")

    await client.close()


if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: No extra tokens; fan-out tasks immediately available to idle workers
# Environment: Recursive agent pipelines (tree-of-thought, multi-step research) with dynamic task generation
```

---

### Option 6: Work-Stealing Pool with Metrics and Rebalance Threshold

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass, field


@dataclass
class Task:
    task_id: int
    prompt: str
    priority: int = 0  # higher = more urgent


@dataclass
class WorkerStats:
    worker_id: int
    processed: int = 0
    stolen_from: int = 0
    stolen_by: int = 0
    total_ms: float = 0.0

    @property
    def avg_ms(self) -> float:
        return self.total_ms / max(self.processed, 1)


class MetricsDrivenStealingPool:
    """
    Steals when queue imbalance exceeds a configurable threshold.
    Reports per-worker metrics after completion.
    """

    STEAL_THRESHOLD = 3  # steal only if victim has 3+ more tasks than thief

    def __init__(self, num_workers: int) -> None:
        self.num_workers = num_workers
        self._queues: list[list[Task]] = [[] for _ in range(num_workers)]
        self._locks = [asyncio.Lock() for _ in range(num_workers)]
        self.stats = [WorkerStats(i) for i in range(num_workers)]

    def load(self, tasks: list[Task]) -> None:
        # Intentionally skewed: worker 0 gets 80% of tasks
        split = int(len(tasks) * 0.8)
        for t in tasks[:split]:
            self._queues[0].append(t)
        remaining = tasks[split:]
        for i, t in enumerate(remaining):
            self._queues[1 + (i % (self.num_workers - 1))].append(t)

    async def _pop(self, worker_id: int) -> Task | None:
        async with self._locks[worker_id]:
            if self._queues[worker_id]:
                return self._queues[worker_id].pop(0)
        return None

    async def _steal(self, thief_id: int) -> Task | None:
        my_size = len(self._queues[thief_id])
        victim_id = max(
            (i for i in range(self.num_workers) if i != thief_id),
            key=lambda i: len(self._queues[i]),
        )
        victim_size = len(self._queues[victim_id])
        if victim_size - my_size < self.STEAL_THRESHOLD:
            return None
        async with self._locks[victim_id]:
            if self._queues[victim_id]:
                task = self._queues[victim_id].pop()  # steal from back
                self.stats[thief_id].stolen_by += 1
                self.stats[victim_id].stolen_from += 1
                return task
        return None

    async def _worker(self, worker_id: int, client: anthropic.AsyncAnthropic) -> None:
        while True:
            task = await self._pop(worker_id) or await self._steal(worker_id)
            if task is None:
                if all(not q for q in self._queues):
                    break
                await asyncio.sleep(0.002)
                continue

            start = time.perf_counter()
            resp = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=32,
                messages=[{"role": "user", "content": task.prompt}],
            )
            elapsed_ms = (time.perf_counter() - start) * 1000
            s = self.stats[worker_id]
            s.processed += 1
            s.total_ms += elapsed_ms

    async def run(self, client: anthropic.AsyncAnthropic) -> None:
        await asyncio.gather(*[self._worker(i, client) for i in range(self.num_workers)])

    def report(self) -> None:
        print(f"\n{'Worker':<8} {'Processed':>10} {'StoleFrom':>10} {'StolenBy':>10} {'AvgMs':>8}")
        print("-" * 50)
        for s in self.stats:
            print(f"{s.worker_id:<8} {s.processed:>10} {s.stolen_from:>10} {s.stolen_by:>10} {s.avg_ms:>7.0f}ms")


async def main() -> None:
    client = anthropic.AsyncAnthropic()
    pool = MetricsDrivenStealingPool(num_workers=4)

    tasks = [Task(i, f"Translate 'hello' to language {i}") for i in range(20)]
    pool.load(tasks)

    print("Initial queue sizes:", [len(q) for q in pool._queues])
    await pool.run(client)
    pool.report()
    await client.close()


if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: No extra tokens; threshold prevents noisy stealing on already-balanced queues
# Environment: Multi-worker batch processors that need observable rebalancing behavior
```

---

## Comparison

| Option | Approach | Best For | Steal Strategy | Complexity |
|--------|----------|----------|----------------|------------|
| 1 | Single shared queue | Simplest work-stealing | Implicit (shared queue) | Very Low |
| 2 | Per-worker deque, steal from back | Cache-friendly locality + balance | Back of victim deque | Medium |
| 3 | Priority deque, steal lightest | Mixed-weight tasks | Min-weight from victim | Medium |
| 4 | Adaptive throughput-triggered steal | API latency variance | Rate-comparison triggered | Medium-High |
| 5 | Continuation fan-out tasks | Recursive / tree pipelines | Shared queue with continuations | Medium-High |
| 6 | Threshold-based with metrics | Observable production pools | Threshold-guarded back-steal | High |
