---
layout: solution
title: "Agent Doesn't Implement Work Stealing for Uneven Task Distribution"
category: concurrency
description: "Agent assigns tasks to a fixed pool of workers at startup — some workers finish early and sit idle while overloaded workers fall behind. Total throughput is limited by the slowest worker, not by available capacity."
tags: [concurrency, work-stealing, throughput, load-balancing, worker-pool]
---

## Symptom

An agent distributes 100 tasks across 4 workers at startup:

```
Worker 0: [task1, task2, ..., task25]  → 25 tasks, avg 2s each = 50s  ← bottleneck
Worker 1: [task26, ..., task50]        → 25 tasks, avg 0.5s each = 12.5s → idle 37.5s
Worker 2: [task51, ..., task75]        → 25 tasks, avg 0.5s each = 12.5s → idle 37.5s
Worker 3: [task76, ..., task100]       → 25 tasks, avg 0.5s each = 12.5s → idle 37.5s

Total wall time: 50s (limited by Worker 0)
Possible with work stealing: ~16s (all workers busy until completion)
```

## Root Cause

Tasks are statically partitioned at dispatch time with no mechanism for idle workers to pick up work from busy workers:

```python
import asyncio
import anthropic

client = anthropic.Anthropic(api_key="sk-live-...")

# Anti-pattern: static partitioning
async def process_all(tasks: list) -> list:
    n_workers = 4
    chunks = [tasks[i::n_workers] for i in range(n_workers)]
    results = await asyncio.gather(*[
        process_chunk(chunk) for chunk in chunks
    ])
    return [r for sublist in results for r in sublist]
```

When task durations are heterogeneous (some take 0.1s, some take 10s), static partitioning wastes available worker capacity.

---

## Fix

### Option 1 — Shared asyncio.Queue: workers pull instead of being pushed

Replace static assignment with a shared queue. Workers pull tasks when ready, so fast workers naturally pick up more tasks.

```python
import anthropic
import asyncio
import time
import random

client = anthropic.AsyncAnthropic(api_key="sk-live-...")


async def worker(worker_id: int, queue: asyncio.Queue, results: list, semaphore: asyncio.Semaphore) -> None:
    """Pull tasks from shared queue until exhausted."""
    while True:
        try:
            task = queue.get_nowait()
        except asyncio.QueueEmpty:
            break

        try:
            async with semaphore:
                t0 = time.monotonic()
                # Simulate variable-duration work
                duration = task.get("duration", random.uniform(0.1, 2.0))
                await asyncio.sleep(duration)
                result = {"task_id": task["id"], "worker": worker_id, "duration": duration}
                results.append(result)
                elapsed = time.monotonic() - t0
                print(f"[W{worker_id}] task {task['id']} done in {elapsed:.2f}s")
        finally:
            queue.task_done()


async def process_with_queue(tasks: list[dict], n_workers: int = 4) -> list[dict]:
    queue: asyncio.Queue = asyncio.Queue()
    for task in tasks:
        await queue.put(task)

    results: list[dict] = []
    semaphore = asyncio.Semaphore(n_workers)

    workers = [
        asyncio.create_task(worker(i, queue, results, semaphore))
        for i in range(n_workers)
    ]

    await queue.join()
    await asyncio.gather(*workers)

    return results


# Mix of fast and slow tasks (simulating heterogeneous LLM call durations)
tasks = [
    {"id": i, "duration": 2.0 if i < 5 else 0.2}  # First 5 are slow
    for i in range(20)
]

t0 = time.monotonic()
results = asyncio.run(process_with_queue(tasks, n_workers=4))
elapsed = time.monotonic() - t0

print(f"\nCompleted {len(results)} tasks in {elapsed:.2f}s")
print(f"Workers used: {sorted(set(r['worker'] for r in results))}")

# Expected Token Savings: idle workers take slow-queue tasks → throughput maximised, session shorter
# Environment: any agent processing heterogeneous batches of tool calls or LLM sub-tasks
```

---

### Option 2 — Priority queue with work stealing between deques

Give each worker a private deque. Idle workers steal tasks from the back of the busiest worker's deque.

```python
import anthropic
import asyncio
import time
import random
from collections import deque

client = anthropic.AsyncAnthropic(api_key="sk-live-...")


class WorkStealingPool:
    def __init__(self, n_workers: int):
        self.n_workers = n_workers
        self.deques: list[deque] = [deque() for _ in range(n_workers)]
        self.lock = asyncio.Lock()
        self.results: list[dict] = []

    def distribute(self, tasks: list[dict]) -> None:
        """Initial round-robin distribution."""
        for i, task in enumerate(tasks):
            self.deques[i % self.n_workers].append(task)

    async def steal_task(self, thief_id: int) -> dict | None:
        """Try to steal a task from the busiest other worker."""
        async with self.lock:
            # Find the worker with the most tasks
            victim_id = max(
                (i for i in range(self.n_workers) if i != thief_id),
                key=lambda i: len(self.deques[i]),
                default=None
            )
            if victim_id is not None and len(self.deques[victim_id]) > 1:
                # Steal from the back (tasks victim hasn't started yet)
                task = self.deques[victim_id].pop()
                print(f"[W{thief_id}] STEAL task {task['id']} from W{victim_id}")
                return task
        return None

    async def run_worker(self, worker_id: int) -> None:
        while True:
            task = None

            # Try own deque first
            async with self.lock:
                if self.deques[worker_id]:
                    task = self.deques[worker_id].popleft()

            # Own deque empty — try stealing
            if task is None:
                task = await self.steal_task(worker_id)

            if task is None:
                # Nothing to steal either — done
                break

            # Execute task
            t0 = time.monotonic()
            duration = task.get("duration", 0.5)
            await asyncio.sleep(duration)

            self.results.append({
                "task_id": task["id"],
                "worker": worker_id,
                "stolen": task.get("stolen", False),
                "duration": time.monotonic() - t0
            })

    async def run(self, tasks: list[dict]) -> list[dict]:
        self.distribute(tasks)
        print(f"Initial distribution: {[len(d) for d in self.deques]}")

        await asyncio.gather(*[
            self.run_worker(i) for i in range(self.n_workers)
        ])
        return self.results


tasks = [
    {"id": i, "duration": 1.5 if i % 4 == 0 else 0.1}  # Every 4th task is slow
    for i in range(16)
]

pool = WorkStealingPool(n_workers=4)
t0 = time.monotonic()
results = asyncio.run(pool.run(tasks))
print(f"\n{len(results)} tasks in {time.monotonic() - t0:.2f}s")

# Expected Token Savings: stealing rebalances load dynamically; no idle workers during long LLM calls
# Environment: multi-agent orchestrators; parallel tool execution engines
```

---

### Option 3 — Dynamic worker scaling based on queue depth

Start with a minimum number of workers. Spawn additional workers when queue depth exceeds a threshold; retire them when the queue drains.

```python
import anthropic
import asyncio
import time

client = anthropic.AsyncAnthropic(api_key="sk-live-...")

MIN_WORKERS = 2
MAX_WORKERS = 8
SCALE_UP_THRESHOLD = 5   # Add worker when queue > 5 tasks
SCALE_DOWN_THRESHOLD = 1  # Remove worker when queue < 1 task


class DynamicWorkerPool:
    def __init__(self):
        self.queue: asyncio.Queue = asyncio.Queue()
        self.results: list[dict] = []
        self.active_workers: set[asyncio.Task] = set()
        self._worker_count = 0
        self._running = True

    async def _worker(self, worker_id: int) -> None:
        print(f"[pool] Worker {worker_id} started (total: {len(self.active_workers)})")
        try:
            while self._running:
                try:
                    task = await asyncio.wait_for(self.queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    if self.queue.empty() and len(self.active_workers) > MIN_WORKERS:
                        print(f"[pool] Worker {worker_id} retiring (queue empty)")
                        break
                    continue

                try:
                    duration = task.get("duration", 0.5)
                    await asyncio.sleep(duration)
                    self.results.append({"task_id": task["id"], "worker": worker_id})
                finally:
                    self.queue.task_done()
        finally:
            pass  # Cleanup handled by monitor

    def _spawn_worker(self) -> None:
        if len(self.active_workers) >= MAX_WORKERS:
            return
        self._worker_count += 1
        wid = self._worker_count
        task = asyncio.create_task(self._worker(wid))
        self.active_workers.add(task)
        task.add_done_callback(self.active_workers.discard)

    async def _monitor(self) -> None:
        """Scale workers based on queue depth."""
        while self._running or not self.queue.empty():
            depth = self.queue.qsize()
            current = len(self.active_workers)

            if depth > SCALE_UP_THRESHOLD and current < MAX_WORKERS:
                print(f"[pool] Scale UP: queue={depth}, workers={current} → {current + 1}")
                self._spawn_worker()

            await asyncio.sleep(0.5)

    async def run(self, tasks: list[dict]) -> list[dict]:
        for task in tasks:
            await self.queue.put(task)

        # Start minimum workers
        for _ in range(MIN_WORKERS):
            self._spawn_worker()

        monitor = asyncio.create_task(self._monitor())

        await self.queue.join()
        self._running = False
        monitor.cancel()

        if self.active_workers:
            await asyncio.gather(*self.active_workers, return_exceptions=True)

        return self.results


tasks = [{"id": i, "duration": 0.3} for i in range(30)]

pool = DynamicWorkerPool()
t0 = time.monotonic()
results = asyncio.run(pool.run(tasks))
print(f"\n{len(results)} tasks in {time.monotonic() - t0:.2f}s")

# Expected Token Savings: dynamic scaling fills all available concurrency → throughput optimised
# Environment: agents with burst workloads; pipelines with variable task arrival rates
```

---

### Option 4 — Least-loaded worker routing with task duration estimation

Before dispatching each task, ask Claude to estimate its duration, then route it to the worker with the lightest predicted load.

```python
import anthropic
import asyncio
import heapq
import time

client = anthropic.AsyncAnthropic(api_key="sk-live-...")


async def estimate_task_duration(task: dict) -> float:
    """Use Claude to estimate how long a task will take (in seconds)."""
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=50,
        system="Estimate task duration in seconds as a single number. Consider complexity.",
        messages=[{
            "role": "user",
            "content": f"Task: {task.get('description', str(task))}\nEstimate seconds:"
        }]
    )
    raw = response.content[0].text.strip()
    try:
        return float(''.join(c for c in raw if c.isdigit() or c == '.'))
    except ValueError:
        return 1.0  # Default estimate


class LeastLoadedDispatcher:
    def __init__(self, n_workers: int):
        self.n_workers = n_workers
        # Min-heap: (projected_finish_time, worker_id, task_queue)
        self.worker_loads: list[tuple[float, int]] = [(0.0, i) for i in range(n_workers)]
        heapq.heapify(self.worker_loads)
        self.worker_queues: list[asyncio.Queue] = [asyncio.Queue() for _ in range(n_workers)]
        self.results: list[dict] = []

    async def dispatch(self, tasks: list[dict]) -> None:
        """Dispatch tasks to least-loaded workers using duration estimates."""
        for task in tasks:
            # Estimate duration
            estimated = task.get("estimated_duration") or await estimate_task_duration(task)

            # Pick least-loaded worker
            load, worker_id = heapq.heappop(self.worker_loads)
            new_load = max(load, time.monotonic()) + estimated
            heapq.heappush(self.worker_loads, (new_load, worker_id))

            await self.worker_queues[worker_id].put(task)
            print(f"[dispatch] task {task['id']} → W{worker_id} (est={estimated:.1f}s, load={new_load:.1f})")

        # Signal workers to stop
        for q in self.worker_queues:
            await q.put(None)

    async def run_worker(self, worker_id: int) -> None:
        while True:
            task = await self.worker_queues[worker_id].get()
            if task is None:
                break
            duration = task.get("duration", 0.5)
            await asyncio.sleep(duration)
            self.results.append({"task_id": task["id"], "worker": worker_id})

    async def run(self, tasks: list[dict]) -> list[dict]:
        workers = [
            asyncio.create_task(self.run_worker(i))
            for i in range(self.n_workers)
        ]
        await self.dispatch(tasks)
        await asyncio.gather(*workers)
        return self.results


# Tasks with known durations (skip LLM estimation for demo)
tasks = [
    {"id": i, "duration": 2.0 if i < 3 else 0.3, "estimated_duration": 2.0 if i < 3 else 0.3}
    for i in range(12)
]

dispatcher = LeastLoadedDispatcher(n_workers=4)
t0 = time.monotonic()
results = asyncio.run(dispatcher.run(tasks))
print(f"\n{len(results)} tasks in {time.monotonic() - t0:.2f}s")

# Expected Token Savings: upfront duration estimation prevents load imbalance → workers stay busy
# Environment: pipelines with predictable task types where duration can be estimated
```

---

### Option 5 — Two-level queue: fast lane and slow lane workers

Classify tasks as fast or slow based on size/complexity, and route them to separate worker pools. Slow-lane workers don't block fast-lane tasks.

```python
import anthropic
import asyncio
import time

client = anthropic.AsyncAnthropic(api_key="sk-live-...")

FAST_THRESHOLD_CHARS = 500  # Tasks with input below this are "fast"


def classify_task(task: dict) -> str:
    """Classify task as 'fast' or 'slow' based on input size."""
    input_size = len(str(task.get("input", "")))
    return "fast" if input_size < FAST_THRESHOLD_CHARS else "slow"


async def process_task(task: dict, lane: str, worker_id: int) -> dict:
    """Simulate processing a task (replace with real LLM/tool call)."""
    duration = task.get("duration", 0.2 if lane == "fast" else 1.5)
    await asyncio.sleep(duration)
    return {"task_id": task["id"], "lane": lane, "worker": f"{lane[0]}{worker_id}"}


async def lane_worker(lane: str, worker_id: int, queue: asyncio.Queue, results: list) -> None:
    while True:
        try:
            task = await asyncio.wait_for(queue.get(), timeout=2.0)
        except asyncio.TimeoutError:
            break
        try:
            result = await process_task(task, lane, worker_id)
            results.append(result)
            print(f"[{lane}:{worker_id}] task {task['id']} complete")
        finally:
            queue.task_done()


async def run_two_lane_pool(tasks: list[dict], fast_workers: int = 3, slow_workers: int = 2) -> list[dict]:
    fast_queue: asyncio.Queue = asyncio.Queue()
    slow_queue: asyncio.Queue = asyncio.Queue()
    results: list[dict] = []

    # Route tasks to appropriate lane
    for task in tasks:
        lane = classify_task(task)
        if lane == "fast":
            await fast_queue.put(task)
        else:
            await slow_queue.put(task)

    print(f"[lanes] fast={fast_queue.qsize()} tasks, slow={slow_queue.qsize()} tasks")

    workers = [
        *[asyncio.create_task(lane_worker("fast", i, fast_queue, results)) for i in range(fast_workers)],
        *[asyncio.create_task(lane_worker("slow", i, slow_queue, results)) for i in range(slow_workers)],
    ]

    await asyncio.gather(fast_queue.join(), slow_queue.join())
    await asyncio.gather(*workers, return_exceptions=True)
    return results


# Mix of fast (short) and slow (long) tasks
tasks = [
    {"id": i, "input": "x" * (1000 if i % 5 == 0 else 100), "duration": 1.0 if i % 5 == 0 else 0.1}
    for i in range(20)
]

t0 = time.monotonic()
results = asyncio.run(run_two_lane_pool(tasks))
print(f"\n{len(results)} tasks in {time.monotonic() - t0:.2f}s")
fast_done = [r for r in results if r["lane"] == "fast"]
slow_done = [r for r in results if r["lane"] == "slow"]
print(f"Fast lane: {len(fast_done)}, Slow lane: {len(slow_done)}")

# Expected Token Savings: fast tasks never blocked by slow ones → user-facing latency stays low
# Environment: agents handling mixed quick-lookup and long-running LLM tasks simultaneously
```

---

### Option 6 — Continuation stealing: re-queue long tasks mid-execution

Break long tasks into checkpointed steps. If a step exceeds a time budget, save state and re-queue the continuation for another worker.

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic(api_key="sk-live-...")

STEP_BUDGET_SECS = 0.5  # Max time per step before yielding


@dataclass
class Continuation:
    task_id: int
    steps_remaining: list[str]
    accumulated_result: list[str] = field(default_factory=list)
    attempt: int = 0


async def process_step(step: str) -> tuple[str, float]:
    """Process one step; returns (result, duration)."""
    duration = len(step) * 0.01  # Longer steps take more time
    await asyncio.sleep(min(duration, 0.3))
    return f"result_of_{step}", duration


async def continuation_worker(
    worker_id: int,
    queue: asyncio.Queue,
    results: list[dict]
) -> None:
    while True:
        try:
            cont: Continuation = await asyncio.wait_for(queue.get(), timeout=1.5)
        except asyncio.TimeoutError:
            break

        try:
            deadline = time.monotonic() + STEP_BUDGET_SECS

            while cont.steps_remaining and time.monotonic() < deadline:
                step = cont.steps_remaining.pop(0)
                step_result, _ = await process_step(step)
                cont.accumulated_result.append(step_result)

            if cont.steps_remaining:
                # Budget exceeded — re-queue continuation
                cont.attempt += 1
                print(f"[W{worker_id}] task {cont.task_id}: {len(cont.steps_remaining)} steps remaining — re-queuing (attempt {cont.attempt})")
                await queue.put(cont)
            else:
                # All steps done
                results.append({
                    "task_id": cont.task_id,
                    "result": cont.accumulated_result,
                    "attempts": cont.attempt + 1,
                    "worker_final": worker_id
                })
                print(f"[W{worker_id}] task {cont.task_id} complete after {cont.attempt + 1} attempt(s)")
        finally:
            queue.task_done()


async def run_continuation_pool(tasks: list[dict], n_workers: int = 3) -> list[dict]:
    queue: asyncio.Queue = asyncio.Queue()

    for task in tasks:
        steps = task.get("steps", [f"step_{j}" * 5 for j in range(task.get("n_steps", 3))])
        cont = Continuation(task_id=task["id"], steps_remaining=steps)
        await queue.put(cont)

    results: list[dict] = []
    workers = [
        asyncio.create_task(continuation_worker(i, queue, results))
        for i in range(n_workers)
    ]

    await queue.join()
    await asyncio.gather(*workers, return_exceptions=True)
    return results


tasks = [
    {"id": i, "n_steps": 2 if i % 3 == 0 else 8}  # Mix of short and long tasks
    for i in range(9)
]

t0 = time.monotonic()
results = asyncio.run(run_continuation_pool(tasks, n_workers=3))
print(f"\n{len(results)} tasks in {time.monotonic() - t0:.2f}s")

# Expected Token Savings: time-sliced execution prevents any single task monopolising a worker
# Environment: long-horizon agent tasks; streaming pipelines with checkpointed intermediate state
```

---

## Comparison

| Option | Stealing Strategy | Dynamic Scaling | Complexity | Best For |
|--------|------------------|-----------------|------------|----------|
| 1 | Shared queue (pull) | No | Low | General-purpose; heterogeneous tasks |
| 2 | Deque stealing | No | Medium | Fine-grained load balancing |
| 3 | Dynamic spawn/retire | Yes | Medium | Burst workloads |
| 4 | Least-loaded routing | No | Medium | Predictable task durations |
| 5 | Fast/slow lanes | No | Low | Mixed latency requirements |
| 6 | Continuation re-queue | No | High | Long-running checkpointable tasks |

**Recommended starting point:** Option 1 (shared asyncio.Queue with pull-based workers) — the simplest and most effective approach for most agent workloads. Workers pull tasks when ready; no idle time; fast workers automatically pick up more work. Takes 20 lines to implement and eliminates static-partition bottlenecks entirely.
