---
layout: solution
title: "Agent Doesn't Implement Priority Queue for Task Scheduling"
category: concurrency
description: "Agent processes all incoming tasks in FIFO order, causing urgent high-priority requests to wait behind lower-priority batch jobs — degrading latency for time-sensitive operations."
tags: [concurrency, priority-queue, scheduling, latency, asyncio]
---

## Symptom

A critical alert task sits in the queue for 45 seconds behind 20 low-priority batch summarization jobs. User-facing requests take 5× longer than their expected SLA because they wait for bulk processing to finish. Adding more workers helps throughput but doesn't fix the latency problem for urgent tasks. The queue has no concept of urgency.

## Root Cause

The agent uses `asyncio.Queue()` or a simple list as its task queue. Both process items in FIFO order — first in, first out — regardless of priority. A batch job submitted at 10:00:00 blocks a critical alert submitted at 10:00:01. The system has no mechanism to promote urgent work ahead of queued backlog.

## Fix

### Option 1: asyncio.PriorityQueue with priority levels

```python
import asyncio
import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any
import anthropic

client = anthropic.AsyncAnthropic()


class Priority(IntEnum):
    CRITICAL = 0   # Alerts, user-blocking requests — process immediately
    HIGH = 1       # Interactive user requests
    NORMAL = 2     # Standard agent tasks
    LOW = 3        # Batch processing, background jobs
    BACKGROUND = 4 # Cleanup, analytics, non-urgent enrichment


@dataclass(order=True)
class PrioritizedTask:
    priority: int                            # Lower = higher priority
    submitted_at: float                      # Tiebreaker: FIFO within same priority
    task_id: str = field(compare=False)
    payload: dict = field(compare=False)
    prompt: str = field(compare=False)

    @classmethod
    def create(cls, task_id: str, prompt: str, priority: Priority, payload: dict | None = None) -> "PrioritizedTask":
        return cls(
            priority=priority.value,
            submitted_at=time.monotonic(),
            task_id=task_id,
            payload=payload or {},
            prompt=prompt,
        )


async def process_task(task: PrioritizedTask) -> dict:
    """Execute a task using Claude."""
    wait_time = time.monotonic() - task.submitted_at
    print(f"[{Priority(task.priority).name}] Processing {task.task_id} (waited {wait_time:.2f}s)")

    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{"role": "user", "content": task.prompt}],
    )
    return {
        "task_id": task.task_id,
        "priority": Priority(task.priority).name,
        "wait_time_s": round(wait_time, 3),
        "result": response.content[0].text[:80],
    }


async def priority_worker(queue: asyncio.PriorityQueue, worker_id: int) -> None:
    """Worker that processes tasks from the priority queue."""
    while True:
        task = await queue.get()
        if task is None:  # Shutdown signal
            queue.task_done()
            break
        try:
            result = await process_task(task)
            print(f"  Worker {worker_id} completed: {result['task_id']} in {result['wait_time_s']}s")
        except Exception as e:
            print(f"  Worker {worker_id} error on {task.task_id}: {e}")
        finally:
            queue.task_done()


async def main():
    queue: asyncio.PriorityQueue = asyncio.PriorityQueue()

    # Start workers
    workers = [asyncio.create_task(priority_worker(queue, i)) for i in range(3)]

    # Simulate mixed-priority task submission (in real use: tasks come from API/webhook)
    tasks = [
        PrioritizedTask.create("batch-1", "Summarize document #1", Priority.BACKGROUND),
        PrioritizedTask.create("batch-2", "Summarize document #2", Priority.BACKGROUND),
        PrioritizedTask.create("batch-3", "Summarize document #3", Priority.BACKGROUND),
        PrioritizedTask.create("user-req-1", "Answer: what is 2+2?", Priority.HIGH),
        PrioritizedTask.create("alert-1", "CRITICAL: Summarize security alert", Priority.CRITICAL),
        PrioritizedTask.create("normal-1", "Generate weekly report", Priority.NORMAL),
        PrioritizedTask.create("user-req-2", "What's the capital of France?", Priority.HIGH),
    ]

    for task in tasks:
        await queue.put(task)
        print(f"Queued [{Priority(task.priority).name}] {task.task_id}")

    await asyncio.sleep(0.01)  # Let workers start
    await queue.join()

    # Shutdown workers
    for _ in workers:
        await queue.put(None)
    await asyncio.gather(*workers)


asyncio.run(main())
```

**Expected Token Savings:** Priority scheduling doesn't reduce tokens but prevents SLA breaches that trigger expensive retries and escalation workflows.
**Environment:** Python 3.11+; `asyncio.PriorityQueue` is stdlib; `@dataclass(order=True)` provides comparison for queue ordering.

---

### Option 2: Multi-tier queue with dedicated workers per priority band

```python
import asyncio
import time
import anthropic
from dataclasses import dataclass

client = anthropic.AsyncAnthropic()


@dataclass
class Task:
    task_id: str
    prompt: str
    priority: str  # "critical", "high", "normal", "low"
    submitted_at: float = 0.0

    def __post_init__(self):
        if not self.submitted_at:
            self.submitted_at = time.monotonic()


class MultiTierScheduler:
    """
    Separate queues per priority tier.
    Critical tasks have dedicated workers that are never starved by lower tiers.
    Lower tier workers check higher tiers before picking up their own work.
    """

    TIERS = ["critical", "high", "normal", "low"]
    WORKERS_PER_TIER = {"critical": 2, "high": 3, "normal": 2, "low": 1}

    def __init__(self):
        self.queues = {tier: asyncio.Queue() for tier in self.TIERS}
        self._completed: list[dict] = []
        self._workers: list[asyncio.Task] = []

    async def submit(self, task: Task) -> None:
        if task.priority not in self.queues:
            task.priority = "normal"
        await self.queues[task.priority].put(task)

    async def _get_highest_priority_task(self, own_tier: str) -> Task | None:
        """
        Worker checks all tiers at or above its own tier before processing its own queue.
        This lets lower-tier workers assist with critical/high load when idle.
        """
        own_index = self.TIERS.index(own_tier)
        for tier in self.TIERS[:own_index + 1]:
            try:
                return self.queues[tier].get_nowait()
            except asyncio.QueueEmpty:
                continue
        return None

    async def _worker(self, worker_id: str, own_tier: str) -> None:
        while True:
            # Try to get a task — prefer higher priority tiers
            task = await self._get_highest_priority_task(own_tier)
            if task is None:
                # No immediate work — wait on own queue
                task = await self.queues[own_tier].get()

            if task is None:  # Shutdown signal
                break

            wait = round(time.monotonic() - task.submitted_at, 3)
            print(f"  [{worker_id}] Processing [{task.priority}] {task.task_id} (waited {wait}s)")

            response = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=64,
                messages=[{"role": "user", "content": task.prompt}],
            )

            self._completed.append({
                "task_id": task.task_id,
                "priority": task.priority,
                "worker": worker_id,
                "wait_s": wait,
                "result": response.content[0].text[:40],
            })

            # Mark done on the correct queue
            try:
                self.queues[task.priority].task_done()
            except Exception:
                pass

    async def start(self) -> None:
        for tier, count in self.WORKERS_PER_TIER.items():
            for i in range(count):
                worker = asyncio.create_task(self._worker(f"{tier}-w{i}", tier))
                self._workers.append(worker)

    async def drain(self) -> None:
        for queue in self.queues.values():
            await queue.join()

    def report(self) -> None:
        print(f"\n=== Completion Report ({len(self._completed)} tasks) ===")
        by_priority: dict[str, list] = {}
        for c in self._completed:
            by_priority.setdefault(c["priority"], []).append(c["wait_s"])
        for priority in self.TIERS:
            if priority in by_priority:
                waits = by_priority[priority]
                print(f"  {priority}: {len(waits)} tasks, avg_wait={sum(waits)/len(waits):.3f}s, max_wait={max(waits):.3f}s")


async def main():
    scheduler = MultiTierScheduler()
    await scheduler.start()

    tasks = [
        Task("bg-1", "Analyze log file 1", "low"),
        Task("bg-2", "Analyze log file 2", "low"),
        Task("bg-3", "Analyze log file 3", "low"),
        Task("norm-1", "Generate daily summary", "normal"),
        Task("user-1", "Answer user question about billing", "high"),
        Task("alert-1", "CRITICAL: Summarize downtime alert", "critical"),
        Task("user-2", "Help user reset password", "high"),
        Task("alert-2", "CRITICAL: Process security event", "critical"),
        Task("bg-4", "Cleanup old records summary", "low"),
    ]

    for task in tasks:
        await scheduler.submit(task)

    await asyncio.sleep(0.1)
    await scheduler.drain()
    scheduler.report()


asyncio.run(main())
```

**Expected Token Savings:** Dedicated critical workers ensure urgent tasks start within milliseconds; prevents SLA breaches that trigger expensive human escalation.
**Environment:** Python 3.11+; separate queues eliminate head-of-line blocking between priority tiers.

---

### Option 3: Preemption — interrupt low-priority work for critical tasks

```python
import asyncio
import time
import anthropic

client = anthropic.AsyncAnthropic()


class PreemptibleTask:
    def __init__(self, task_id: str, prompt: str, priority: int):
        self.task_id = task_id
        self.prompt = prompt
        self.priority = priority  # 0=critical, higher=lower priority
        self.cancel_event = asyncio.Event()
        self.submitted_at = time.monotonic()
        self.result: str | None = None

    def preempt(self) -> None:
        """Signal this task to pause/cancel so a higher-priority task can run."""
        self.cancel_event.set()
        print(f"  [PREEMPT] {self.task_id} signaled to yield")


class PreemptingScheduler:
    """
    Scheduler that can preempt running low-priority tasks when a higher-priority
    task arrives. Preempted tasks are re-queued for later completion.
    """

    def __init__(self, num_workers: int = 2):
        self.queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self.num_workers = num_workers
        self._current_tasks: dict[int, PreemptibleTask | None] = {i: None for i in range(num_workers)}
        self._results: list[dict] = []

    async def submit(self, task: PreemptibleTask) -> None:
        await self.queue.put((task.priority, task.submitted_at, task))

        # Preempt the lowest-priority running task if this task has higher priority
        for worker_id, running in self._current_tasks.items():
            if running and task.priority < running.priority:
                print(f"  [SCHEDULER] {task.task_id} (p={task.priority}) preempting {running.task_id} (p={running.priority})")
                running.preempt()
                break

    async def _process_task(self, task: PreemptibleTask, worker_id: int) -> bool:
        """
        Process a task with cancellation support.
        Returns True if completed, False if preempted.
        """
        task.cancel_event.clear()
        self._current_tasks[worker_id] = task
        wait = round(time.monotonic() - task.submitted_at, 3)
        print(f"  [Worker {worker_id}] Starting [{task.priority}] {task.task_id} (waited {wait}s)")

        try:
            # Use streaming to allow mid-stream preemption
            chunks = []
            async with client.messages.stream(
                model="claude-haiku-4-5-20251001",
                max_tokens=128,
                messages=[{"role": "user", "content": task.prompt}],
            ) as stream:
                async for text in stream.text_stream:
                    if task.cancel_event.is_set():
                        print(f"  [Worker {worker_id}] {task.task_id} preempted mid-stream")
                        return False
                    chunks.append(text)

            task.result = "".join(chunks)
            self._results.append({
                "task_id": task.task_id,
                "priority": task.priority,
                "result": task.result[:60],
                "wait_s": wait,
            })
            return True

        except asyncio.CancelledError:
            return False
        finally:
            self._current_tasks[worker_id] = None

    async def worker(self, worker_id: int) -> None:
        while True:
            _, _, task = await self.queue.get()
            if task is None:
                break

            completed = await self._process_task(task, worker_id)

            if not completed:
                # Re-queue the preempted task (it will wait again)
                task.submitted_at = time.monotonic()  # Reset wait timer
                await self.queue.put((task.priority, task.submitted_at, task))

            self.queue.task_done()

    async def run(self, tasks: list[PreemptibleTask]) -> list[dict]:
        workers = [asyncio.create_task(self.worker(i)) for i in range(self.num_workers)]

        # Submit tasks with a small delay to simulate real-world arrival
        for i, task in enumerate(tasks):
            await self.queue.put((task.priority, task.submitted_at, task))
            await asyncio.sleep(0.05 * i)

        await self.queue.join()
        for _ in workers:
            await self.queue.put((999, 0, None))
        await asyncio.gather(*workers)
        return self._results


async def main():
    scheduler = PreemptingScheduler(num_workers=2)

    tasks = [
        PreemptibleTask("batch-A", "Long analysis task A", priority=3),
        PreemptibleTask("batch-B", "Long analysis task B", priority=3),
        PreemptibleTask("urgent-1", "CRITICAL: Process alert NOW", priority=0),
        PreemptibleTask("high-1", "User blocking request", priority=1),
        PreemptibleTask("batch-C", "Background report C", priority=3),
    ]

    results = await scheduler.run(tasks)
    print(f"\nCompleted {len(results)} tasks:")
    for r in sorted(results, key=lambda x: x["priority"]):
        print(f"  [{r['priority']}] {r['task_id']}: waited {r['wait_s']}s")


asyncio.run(main())
```

**Expected Token Savings:** Preemption ensures critical tasks start immediately even during high load; partial results from preempted tasks are discarded (small cost) vs. full SLA breach cost.
**Environment:** Python 3.11+; streaming enables mid-stream preemption; suitable for agents where critical latency < 1 second.

---

### Option 4: Deadline-aware priority scheduling

```python
import asyncio
import time
import anthropic
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic()


@dataclass(order=True)
class DeadlineTask:
    """Task with a deadline. Effective priority increases as deadline approaches."""
    deadline_at: float       # Unix timestamp — primary sort key
    priority_base: int       # Secondary sort: explicit priority within same deadline band
    task_id: str = field(compare=False)
    prompt: str = field(compare=False)
    submitted_at: float = field(compare=False, default_factory=time.monotonic)

    @classmethod
    def with_deadline_seconds(cls, task_id: str, prompt: str, deadline_s: float, base_priority: int = 5) -> "DeadlineTask":
        return cls(
            deadline_at=time.monotonic() + deadline_s,
            priority_base=base_priority,
            task_id=task_id,
            prompt=prompt,
        )

    @property
    def time_remaining(self) -> float:
        return max(0.0, self.deadline_at - time.monotonic())

    @property
    def is_overdue(self) -> bool:
        return time.monotonic() > self.deadline_at


class DeadlineScheduler:
    def __init__(self, num_workers: int = 3):
        self.queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self.num_workers = num_workers
        self._results: list[dict] = []
        self._overdue: list[str] = []

    async def submit(self, task: DeadlineTask) -> None:
        await self.queue.put(task)
        print(f"Queued {task.task_id} (deadline in {task.time_remaining:.1f}s)")

    async def _worker(self, worker_id: int) -> None:
        while True:
            task = await self.queue.get()
            if task is None:
                break

            if task.is_overdue:
                print(f"  [OVERDUE] {task.task_id} missed deadline by {-task.time_remaining:.1f}s — processing anyway")
                self._overdue.append(task.task_id)

            wait = time.monotonic() - task.submitted_at
            response = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=64,
                messages=[{"role": "user", "content": task.prompt}],
            )

            self._results.append({
                "task_id": task.task_id,
                "deadline_remaining_s": round(task.time_remaining, 3),
                "wait_s": round(wait, 3),
                "overdue": task.is_overdue,
                "result": response.content[0].text[:40],
            })

            print(f"  [Worker {worker_id}] Done: {task.task_id} (deadline in {task.time_remaining:.1f}s, waited {wait:.2f}s)")
            self.queue.task_done()

    async def run_until_empty(self) -> None:
        workers = [asyncio.create_task(self._worker(i)) for i in range(self.num_workers)]
        await self.queue.join()
        for _ in workers:
            await self.queue.put(None)
        await asyncio.gather(*workers)

    def report(self) -> None:
        print(f"\nResults: {len(self._results)} tasks, {len(self._overdue)} overdue")
        for r in self._results:
            status = "OVERDUE" if r["overdue"] else f"+{r['deadline_remaining_s']}s remaining"
            print(f"  {r['task_id']}: waited {r['wait_s']:.2f}s [{status}]")


async def main():
    scheduler = DeadlineScheduler(num_workers=2)

    # Tasks with different deadlines — shorter deadline = higher effective priority
    tasks = [
        DeadlineTask.with_deadline_seconds("report-weekly", "Generate weekly report", deadline_s=60, base_priority=3),
        DeadlineTask.with_deadline_seconds("user-req-1", "Answer user question", deadline_s=5, base_priority=2),
        DeadlineTask.with_deadline_seconds("alert-sla", "Process SLA alert", deadline_s=2, base_priority=1),
        DeadlineTask.with_deadline_seconds("batch-cleanup", "Cleanup old data", deadline_s=300, base_priority=5),
        DeadlineTask.with_deadline_seconds("user-req-2", "Process user upload", deadline_s=10, base_priority=2),
    ]

    for task in tasks:
        await scheduler.submit(task)

    await scheduler.run_until_empty()
    scheduler.report()


asyncio.run(main())
```

**Expected Token Savings:** Deadline-aware scheduling maximizes SLA compliance; tasks processed in order of urgency, minimizing costly deadline breaches.
**Environment:** Python 3.11+; deadline-sorted queue is a natural fit for SLA-bounded agent work.

---

### Option 5: Token-budget-aware priority scheduling

```python
import asyncio
import time
import anthropic
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic()

# Token budget: expensive tasks should not block cheap urgent tasks
TOKEN_COSTS = {
    "critical": 256,
    "high": 512,
    "normal": 1024,
    "low": 2048,
    "background": 4096,
}


@dataclass(order=True)
class TokenAwareTask:
    priority: int
    submitted_at: float
    task_id: str = field(compare=False)
    prompt: str = field(compare=False)
    tier: str = field(compare=False)
    max_tokens: int = field(compare=False)


class TokenBudgetScheduler:
    """
    Priority scheduler that also respects per-tier token budgets.
    Prevents background tasks from consuming the model's concurrent capacity.
    """

    def __init__(self, total_token_budget: int = 8192):
        self.queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self.total_budget = total_token_budget
        self._tokens_in_flight = 0
        self._budget_lock = asyncio.Lock()
        self._results: list[dict] = []

    async def _acquire_tokens(self, tokens: int) -> bool:
        async with self._budget_lock:
            if self._tokens_in_flight + tokens > self.total_budget:
                return False  # Budget exceeded — task must wait
            self._tokens_in_flight += tokens
            return True

    async def _release_tokens(self, tokens: int) -> None:
        async with self._budget_lock:
            self._tokens_in_flight = max(0, self._tokens_in_flight - tokens)

    async def submit(self, task_id: str, prompt: str, tier: str) -> None:
        priority = {"critical": 0, "high": 1, "normal": 2, "low": 3, "background": 4}.get(tier, 2)
        max_tokens = TOKEN_COSTS.get(tier, 1024)
        task = TokenAwareTask(
            priority=priority,
            submitted_at=time.monotonic(),
            task_id=task_id,
            prompt=prompt,
            tier=tier,
            max_tokens=max_tokens,
        )
        await self.queue.put(task)

    async def worker(self, worker_id: int) -> None:
        while True:
            task = await self.queue.get()
            if task is None:
                break

            # Wait until token budget is available (with retry)
            for attempt in range(5):
                acquired = await self._acquire_tokens(task.max_tokens)
                if acquired:
                    break
                await asyncio.sleep(0.1 * (attempt + 1))
                if attempt == 4:
                    print(f"  [Worker {worker_id}] Token budget exhausted for {task.task_id} — deferring")
                    await self.queue.put(task)  # Re-queue
                    self.queue.task_done()
                    break
            else:
                continue

            try:
                wait = round(time.monotonic() - task.submitted_at, 3)
                response = await client.messages.create(
                    model="claude-haiku-4-5-20251001" if task.tier in ("low", "background") else "claude-sonnet-4-6",
                    max_tokens=task.max_tokens,
                    messages=[{"role": "user", "content": task.prompt}],
                )
                actual_tokens = response.usage.output_tokens
                self._results.append({
                    "task_id": task.task_id,
                    "tier": task.tier,
                    "wait_s": wait,
                    "tokens_used": actual_tokens,
                })
                print(f"  [Worker {worker_id}] {task.task_id} ({task.tier}): {actual_tokens} tokens, waited {wait}s")
            finally:
                await self._release_tokens(task.max_tokens)
                self.queue.task_done()

    async def run(self, tasks: list[tuple[str, str, str]]) -> None:
        workers = [asyncio.create_task(self.worker(i)) for i in range(3)]
        for task_id, prompt, tier in tasks:
            await self.submit(task_id, prompt, tier)
        await self.queue.join()
        for _ in workers:
            await self.queue.put(None)
        await asyncio.gather(*workers)
        total_tokens = sum(r["tokens_used"] for r in self._results)
        print(f"\nTotal tokens used: {total_tokens:,} across {len(self._results)} tasks")


scheduler = TokenBudgetScheduler(total_token_budget=4096)
asyncio.run(scheduler.run([
    ("bg-1", "Generate detailed monthly report", "background"),
    ("critical-1", "Summarize alert", "critical"),
    ("user-1", "Answer quick question", "high"),
    ("bg-2", "Analyze usage patterns", "background"),
    ("critical-2", "Process security event", "critical"),
]))
```

**Expected Token Savings:** Token-budget scheduling prevents background tasks from consuming all model capacity, keeping headroom for critical requests — reducing critical task latency without increasing cost.
**Environment:** Python 3.11+; budget tracking adds minimal overhead; model tier selection (Haiku for low, Sonnet for high) provides additional cost savings.

---

### Option 6: FastAPI endpoint with priority queue integration

```python
import asyncio
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import IntEnum
import anthropic

client = anthropic.AsyncAnthropic()


class TaskPriority(IntEnum):
    CRITICAL = 0
    HIGH = 1
    NORMAL = 2
    LOW = 3


@dataclass(order=True)
class QueuedTask:
    priority: int
    submitted_at: float
    task_id: str = field(compare=False)
    prompt: str = field(compare=False)
    result_future: asyncio.Future = field(compare=False)


_queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
_workers: list[asyncio.Task] = []


async def queue_worker(worker_id: int) -> None:
    while True:
        task = await _queue.get()
        if task is None:
            break
        try:
            wait_ms = round((time.monotonic() - task.submitted_at) * 1000)
            response = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=256,
                messages=[{"role": "user", "content": task.prompt}],
            )
            task.result_future.set_result({
                "task_id": task.task_id,
                "result": response.content[0].text,
                "wait_ms": wait_ms,
                "worker_id": worker_id,
            })
        except Exception as e:
            if not task.result_future.done():
                task.result_future.set_exception(e)
        finally:
            _queue.task_done()


@asynccontextmanager
async def lifespan(app=None):
    # Startup
    global _workers
    _workers = [asyncio.create_task(queue_worker(i)) for i in range(5)]
    print(f"Priority queue scheduler started with {len(_workers)} workers")
    yield
    # Shutdown
    for _ in _workers:
        await _queue.put(None)
    await asyncio.gather(*_workers)


async def submit_task(prompt: str, priority: TaskPriority = TaskPriority.NORMAL) -> dict:
    """
    Submit a task and await its result.
    Higher priority tasks jump ahead in the queue.
    """
    loop = asyncio.get_event_loop()
    future: asyncio.Future = loop.create_future()
    task = QueuedTask(
        priority=priority.value,
        submitted_at=time.monotonic(),
        task_id=str(uuid.uuid4())[:8],
        prompt=prompt,
        result_future=future,
    )
    await _queue.put(task)
    return await asyncio.wait_for(future, timeout=30.0)


async def demo():
    # Simulate lifespan startup
    workers = [asyncio.create_task(queue_worker(i)) for i in range(3)]

    # Simulate concurrent requests with different priorities
    results = await asyncio.gather(
        submit_task("Process batch job A", TaskPriority.LOW),
        submit_task("Process batch job B", TaskPriority.LOW),
        submit_task("Answer user question", TaskPriority.HIGH),
        submit_task("Handle critical alert", TaskPriority.CRITICAL),
        submit_task("Generate report", TaskPriority.NORMAL),
    )

    print("\nResults (ordered by completion):")
    for r in results:
        print(f"  [{r['task_id']}] waited {r['wait_ms']}ms on worker {r['worker_id']}")

    for _ in workers:
        await _queue.put(None)
    await asyncio.gather(*workers)


asyncio.run(demo())
```

**Expected Token Savings:** Web-facing priority queue ensures interactive requests complete in <500ms while batch jobs run in background; prevents user-facing latency spikes that require expensive retries.
**Environment:** Python 3.11+; future-based result delivery integrates cleanly with FastAPI/aiohttp endpoints; workers survive multiple requests.

---

| Option | Approach | Preemption | Best For |
|--------|----------|-----------|----------|
| 1 | asyncio.PriorityQueue | None | Simple priority ordering |
| 2 | Multi-tier separate queues | None | Throughput isolation per tier |
| 3 | Preempting running tasks | Yes (stream cancel) | Sub-second critical latency |
| 4 | Deadline-aware scheduling | None | SLA-bounded task queues |
| 5 | Token-budget-aware priority | None | Cost-controlled mixed workloads |
| 6 | FastAPI future-based queue | None | Web API integration |
