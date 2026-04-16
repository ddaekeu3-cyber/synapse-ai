---
title: "Agent Doesn't Implement Deadline-Aware Task Prioritization"
description: "AI agents that process tasks in FIFO order miss SLA deadlines when low-priority work crowds out urgent requests. Learn six patterns for deadline-aware scheduling that preempts, reorders, and escalates tasks based on time-to-deadline and business priority."
date: 2026-04-16
difficulty: advanced
category: reliability
slug: agent-doesnt-implement-deadline-aware-task-prioritization
tags: [scheduling, priority, deadline, SLA, queue, reliability]
symptoms:
  - "High-priority urgent tasks wait behind low-priority batch jobs"
  - "SLA breaches because FIFO queue doesn't account for deadlines"
  - "Time-sensitive notifications sent hours late because queue was backed up"
  - "No visibility into which tasks are about to miss their deadline"
  - "Long-running low-value tasks block short high-value tasks"
---

## The Problem

Most AI agent task queues use FIFO ordering: whatever arrived first gets processed first. This works when all tasks have equal urgency, but fails badly in production where tasks have wildly different SLAs — a real-time user query has a 2-second deadline while a nightly report has an 8-hour window. Without deadline-aware prioritization, a backlog of batch jobs can cause the real-time query to miss its SLA entirely.

Effective scheduling considers both urgency (how much time remains before the deadline) and priority (business importance), and reorders the queue dynamically as deadlines approach.

```python
# ❌ FIFO — processes in arrival order regardless of deadline
queue = asyncio.Queue()
await queue.put(batch_report_task)      # 8-hour SLA, arrives first
await queue.put(user_query_task)        # 2-second SLA, arrives second
# batch_report processed first → user query misses SLA

# ✓ Deadline-aware priority queue
scheduler = DeadlineScheduler()
scheduler.submit(batch_report_task, deadline_seconds=28800, priority=1)
scheduler.submit(user_query_task, deadline_seconds=2, priority=10)
next_task = scheduler.next()  # → user_query_task (deadline imminent)
```

---

## Solution 1: Earliest Deadline First (EDF) Priority Queue

A heap-based queue that always serves the task whose deadline is soonest, regardless of arrival order.

```python
import heapq
import time
import asyncio
from dataclasses import dataclass, field
from typing import Any
import uuid


@dataclass(order=True)
class ScheduledTask:
    deadline: float          # Unix timestamp
    priority: int            # Lower = higher priority (tiebreaker)
    arrival: float           # For secondary sort
    task_id: str = field(compare=False)
    payload: Any = field(compare=False)
    submitted_at: float = field(compare=False, default_factory=time.time)

    def time_to_deadline(self) -> float:
        return self.deadline - time.time()

    def is_expired(self) -> bool:
        return time.time() > self.deadline


class EDFScheduler:
    """Earliest Deadline First scheduler backed by a min-heap."""

    def __init__(self):
        self._heap: list[ScheduledTask] = []
        self._task_map: dict[str, ScheduledTask] = {}
        self._cancelled: set[str] = set()
        self._stats = {"submitted": 0, "expired": 0, "completed": 0}

    def submit(
        self,
        payload: Any,
        deadline_seconds: float,
        priority: int = 5,
        task_id: str | None = None,
    ) -> str:
        tid = task_id or str(uuid.uuid4())
        task = ScheduledTask(
            deadline=time.time() + deadline_seconds,
            priority=priority,
            arrival=time.time(),
            task_id=tid,
            payload=payload,
        )
        heapq.heappush(self._heap, task)
        self._task_map[tid] = task
        self._stats["submitted"] += 1
        return tid

    def next(self) -> ScheduledTask | None:
        """Pop the task with the earliest deadline. Skips expired and cancelled."""
        while self._heap:
            task = heapq.heappop(self._heap)
            if task.task_id in self._cancelled:
                continue
            if task.is_expired():
                self._stats["expired"] += 1
                self._on_expire(task)
                continue
            return task
        return None

    def cancel(self, task_id: str):
        self._cancelled.add(task_id)

    def peek_at_risk(self, warn_threshold_seconds: float = 30.0) -> list[ScheduledTask]:
        """Return tasks whose deadline is within warn_threshold_seconds."""
        now = time.time()
        return [
            t for t in self._heap
            if t.task_id not in self._cancelled
            and 0 < t.deadline - now <= warn_threshold_seconds
        ]

    def queue_depth(self) -> dict:
        active = [t for t in self._heap if t.task_id not in self._cancelled]
        return {
            "total": len(active),
            "at_risk": len(self.peek_at_risk()),
            "expired": self._stats["expired"],
            "stats": self._stats,
        }

    def _on_expire(self, task: ScheduledTask):
        print(
            f"[scheduler] EXPIRED: task {task.task_id} "
            f"(missed deadline by {time.time() - task.deadline:.1f}s)"
        )

    async def run_loop(self, worker, poll_interval: float = 0.01):
        """Continuously dequeue and process tasks."""
        while True:
            task = self.next()
            if task:
                try:
                    await worker(task)
                    self._stats["completed"] += 1
                except Exception as e:
                    print(f"[scheduler] Task {task.task_id} failed: {e}")
            else:
                await asyncio.sleep(poll_interval)
```

---

## Solution 2: Multi-Level Priority Queue with Time-Based Escalation

Tasks start in a lower-priority tier and automatically escalate to higher tiers as their deadline approaches. Prevents starvation while still prioritizing urgent work.

```python
import asyncio
import time
from dataclasses import dataclass, field
from typing import Any
from enum import IntEnum
from collections import deque


class Tier(IntEnum):
    CRITICAL = 0    # < 10% of SLA remaining
    HIGH = 1        # < 30% of SLA remaining
    NORMAL = 2      # < 70% of SLA remaining
    LOW = 3         # > 70% of SLA remaining


@dataclass
class TieredTask:
    task_id: str
    payload: Any
    sla_seconds: float
    base_priority: int          # Business priority 1-10
    submitted_at: float = field(default_factory=time.time)
    current_tier: Tier = Tier.LOW

    def sla_remaining_fraction(self) -> float:
        elapsed = time.time() - self.submitted_at
        return max(0.0, 1.0 - elapsed / self.sla_seconds)

    def compute_tier(self) -> Tier:
        f = self.sla_remaining_fraction()
        if f < 0.10:
            return Tier.CRITICAL
        elif f < 0.30:
            return Tier.HIGH
        elif f < 0.70:
            return Tier.NORMAL
        return Tier.LOW

    def effective_priority(self) -> tuple[int, int, float]:
        """(tier, -base_priority, submission_time) — lower is better."""
        return (self.current_tier, -self.base_priority, self.submitted_at)


class MultiLevelEscalatingScheduler:
    """
    4-tier queue with automatic escalation as deadlines approach.
    Background task re-evaluates tiers every `rescan_interval` seconds.
    """

    def __init__(self, rescan_interval: float = 5.0):
        self._queues: dict[Tier, deque[TieredTask]] = {t: deque() for t in Tier}
        self._all_tasks: dict[str, TieredTask] = {}
        self._rescan_interval = rescan_interval
        self._running = False

    def submit(self, task_id: str, payload: Any,
               sla_seconds: float, base_priority: int = 5) -> TieredTask:
        task = TieredTask(task_id=task_id, payload=payload,
                          sla_seconds=sla_seconds, base_priority=base_priority)
        tier = task.compute_tier()
        task.current_tier = tier
        self._queues[tier].append(task)
        self._all_tasks[task_id] = task
        return task

    def next(self) -> TieredTask | None:
        """Return highest-priority available task."""
        for tier in Tier:
            q = self._queues[tier]
            while q:
                task = q[0]
                if task.task_id not in self._all_tasks:
                    q.popleft()
                    continue
                # Check if SLA is already missed
                if task.sla_remaining_fraction() <= 0:
                    q.popleft()
                    del self._all_tasks[task.task_id]
                    print(f"[sched] SLA missed: {task.task_id}")
                    continue
                q.popleft()
                del self._all_tasks[task.task_id]
                return task
        return None

    def escalate_all(self):
        """Re-evaluate tiers and move tasks to higher queues as needed."""
        escalated = 0
        for task_id, task in list(self._all_tasks.items()):
            new_tier = task.compute_tier()
            if new_tier < task.current_tier:
                # Remove from current tier queue (mark as stale)
                # Re-add to higher tier
                task.current_tier = new_tier
                self._queues[new_tier].appendleft(task)
                escalated += 1
        if escalated:
            print(f"[sched] Escalated {escalated} tasks")

    async def _escalation_loop(self):
        while self._running:
            self.escalate_all()
            await asyncio.sleep(self._rescan_interval)

    async def start(self):
        self._running = True
        asyncio.create_task(self._escalation_loop())

    def stop(self):
        self._running = False

    def queue_summary(self) -> dict:
        return {
            tier.name: {
                "depth": len(self._queues[tier]),
                "tasks": [t.task_id for t in list(self._queues[tier])[:5]],
            }
            for tier in Tier
        }
```

---

## Solution 3: Preemptive Task Scheduler

When a high-priority task arrives while a low-priority task is running, preempt the running task, checkpoint its state, and resume it after the high-priority task completes.

```python
import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine


@dataclass
class PreemptibleTask:
    task_id: str
    payload: Any
    priority: int               # Higher = more important
    deadline: float
    coroutine_factory: Callable  # Factory to (re)create the coroutine
    checkpoint: Any = None      # Saved state for resumption
    preempted_count: int = 0


class PreemptiveScheduler:
    """
    Runs tasks with preemption: if a higher-priority task arrives,
    the current task is cancelled (with checkpoint saved) and the
    high-priority task runs first. Preempted tasks are re-queued.
    """

    def __init__(self, preemption_check_interval: float = 0.5):
        self._ready: list[PreemptibleTask] = []
        self._current: PreemptibleTask | None = None
        self._current_asyncio_task: asyncio.Task | None = None
        self._check_interval = preemption_check_interval
        self._preemption_count = 0

    def submit(self, task: PreemptibleTask):
        self._ready.append(task)
        self._ready.sort(key=lambda t: (-t.priority, t.deadline))

    async def _run_with_checkpoint(self, task: PreemptibleTask) -> Any:
        """Run task coroutine, allowing checkpoint injection."""
        coro = task.coroutine_factory(task.checkpoint)
        return await coro

    async def run_loop(self):
        while True:
            if not self._ready:
                await asyncio.sleep(0.01)
                continue

            next_task = self._ready[0]

            # Preempt current task if next has higher priority
            if (self._current is not None and
                    next_task.priority > self._current.priority + 1):
                print(
                    f"[preempt] Preempting task {self._current.task_id} "
                    f"(pri={self._current.priority}) for "
                    f"{next_task.task_id} (pri={next_task.priority})"
                )
                # Cancel current task
                if self._current_asyncio_task and not self._current_asyncio_task.done():
                    self._current_asyncio_task.cancel()
                    try:
                        await self._current_asyncio_task
                    except asyncio.CancelledError:
                        pass
                # Re-queue preempted task
                self._current.preempted_count += 1
                self._preemption_count += 1
                self._ready.append(self._current)
                self._ready.sort(key=lambda t: (-t.priority, t.deadline))
                self._current = None

            if self._current is None and self._ready:
                self._current = self._ready.pop(0)
                self._current_asyncio_task = asyncio.create_task(
                    self._run_with_checkpoint(self._current)
                )

            # Periodically check for new higher-priority tasks
            await asyncio.sleep(self._check_interval)

            if self._current_asyncio_task and self._current_asyncio_task.done():
                if not self._current_asyncio_task.cancelled():
                    exc = self._current_asyncio_task.exception()
                    if exc:
                        print(f"[preempt] Task {self._current.task_id} failed: {exc}")
                self._current = None
                self._current_asyncio_task = None
```

---

## Solution 4: SLA Tracking with Prometheus Metrics

Track SLA compliance in real time: measure deadline miss rate, time-to-process per priority tier, and queue wait time to detect scheduling failures before they impact users.

```python
import time
from dataclasses import dataclass, field
from collections import defaultdict
from statistics import mean, median


@dataclass
class SLARecord:
    task_id: str
    priority: int
    sla_seconds: float
    submitted_at: float
    started_at: float | None = None
    completed_at: float | None = None
    missed: bool = False


class SLATracker:
    """Tracks SLA compliance per priority level. Exports Prometheus-format metrics."""

    def __init__(self):
        self._records: dict[str, SLARecord] = {}
        self._completed: list[SLARecord] = []

    def record_submission(self, task_id: str, priority: int, sla_seconds: float):
        self._records[task_id] = SLARecord(
            task_id=task_id, priority=priority,
            sla_seconds=sla_seconds, submitted_at=time.time(),
        )

    def record_start(self, task_id: str):
        if task_id in self._records:
            self._records[task_id].started_at = time.time()

    def record_completion(self, task_id: str):
        rec = self._records.pop(task_id, None)
        if not rec:
            return
        now = time.time()
        rec.completed_at = now
        deadline = rec.submitted_at + rec.sla_seconds
        rec.missed = now > deadline
        self._completed.append(rec)

        if rec.missed:
            overrun = now - deadline
            print(
                f"[SLA] MISSED: {task_id} priority={rec.priority} "
                f"overrun={overrun:.2f}s sla={rec.sla_seconds}s"
            )

    def stats_by_priority(self) -> dict[int, dict]:
        by_priority: dict[int, list[SLARecord]] = defaultdict(list)
        for rec in self._completed:
            by_priority[rec.priority].append(rec)

        result = {}
        for priority, records in by_priority.items():
            wait_times = [
                (r.started_at - r.submitted_at)
                for r in records if r.started_at
            ]
            processing_times = [
                (r.completed_at - r.started_at)
                for r in records if r.started_at and r.completed_at
            ]
            missed = [r for r in records if r.missed]
            result[priority] = {
                "count": len(records),
                "miss_rate": len(missed) / len(records) if records else 0,
                "avg_wait_s": mean(wait_times) if wait_times else 0,
                "p95_wait_s": sorted(wait_times)[int(len(wait_times) * 0.95)] if wait_times else 0,
                "avg_processing_s": mean(processing_times) if processing_times else 0,
            }
        return result

    def pending_at_risk(self, warn_fraction: float = 0.20) -> list[SLARecord]:
        """Pending tasks with less than warn_fraction of SLA remaining."""
        now = time.time()
        at_risk = []
        for rec in self._records.values():
            remaining = (rec.submitted_at + rec.sla_seconds) - now
            fraction_left = remaining / rec.sla_seconds
            if fraction_left < warn_fraction:
                at_risk.append(rec)
        return sorted(at_risk, key=lambda r: r.submitted_at + r.sla_seconds)

    def prometheus_metrics(self, prefix: str = "agent_scheduler") -> str:
        lines = []
        stats = self.stats_by_priority()
        for priority, s in stats.items():
            lbl = f'priority="{priority}"'
            lines += [
                f'{prefix}_task_count{{{lbl}}} {s["count"]}',
                f'{prefix}_sla_miss_rate{{{lbl}}} {s["miss_rate"]:.4f}',
                f'{prefix}_wait_p95_seconds{{{lbl}}} {s["p95_wait_s"]:.3f}',
            ]
        pending = len(self._records)
        at_risk = len(self.pending_at_risk())
        lines += [
            f'{prefix}_pending_tasks {pending}',
            f'{prefix}_at_risk_tasks {at_risk}',
        ]
        return "\n".join(lines)
```

---

## Solution 5: Adaptive Priority with Aging

Prevent starvation of low-priority tasks by gradually increasing their effective priority as they wait. After waiting long enough, even a low-priority task gets processed before a newly arrived high-priority task.

```python
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class AgingTask:
    task_id: str
    payload: Any
    base_priority: float        # 0.0 (low) to 1.0 (high)
    submitted_at: float = field(default_factory=time.time)
    aging_rate: float = 0.01    # Priority boost per second waited

    def effective_priority(self) -> float:
        wait_time = time.time() - self.submitted_at
        return min(1.0, self.base_priority + self.aging_rate * wait_time)

    def estimated_wait_before_starvation(self) -> float:
        """Seconds until this task reaches max priority regardless of base."""
        if self.base_priority >= 1.0:
            return 0.0
        return (1.0 - self.base_priority) / self.aging_rate


class AgingPriorityScheduler:
    """
    Priority queue with aging: low-priority tasks gradually become high-priority.
    Prevents starvation while still prioritizing urgent work.
    """

    def __init__(self, aging_rate: float = 0.005):
        self.default_aging_rate = aging_rate
        self._tasks: list[AgingTask] = []

    def submit(self, task_id: str, payload: Any,
               base_priority: float = 0.3) -> AgingTask:
        task = AgingTask(
            task_id=task_id,
            payload=payload,
            base_priority=base_priority,
            aging_rate=self.default_aging_rate,
        )
        self._tasks.append(task)
        return task

    def next(self) -> AgingTask | None:
        if not self._tasks:
            return None
        # Select task with highest effective priority (considering aging)
        best = max(self._tasks, key=lambda t: t.effective_priority())
        self._tasks.remove(best)
        return best

    def priority_snapshot(self) -> list[dict]:
        now = time.time()
        return sorted([
            {
                "task_id": t.task_id,
                "base_priority": t.base_priority,
                "effective_priority": t.effective_priority(),
                "wait_seconds": now - t.submitted_at,
            }
            for t in self._tasks
        ], key=lambda x: -x["effective_priority"])

    def starvation_risk(self) -> list[dict]:
        """Tasks that have been waiting dangerously long."""
        return [
            s for s in self.priority_snapshot()
            if s["wait_seconds"] > 60 and s["base_priority"] < 0.3
        ]
```

---

## Solution 6: Deadline-Aware Work Stealing Scheduler

Distribute tasks across worker pools, with idle workers stealing tasks approaching their deadlines from overloaded workers to prevent SLA misses.

```python
import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable
from collections import deque


@dataclass
class StealableTask:
    task_id: str
    payload: Any
    deadline: float
    priority: int = 5
    owner_worker: int = -1

    def urgency(self) -> float:
        """Higher = more urgent. Negative = expired."""
        remaining = self.deadline - time.time()
        if remaining <= 0:
            return float('inf')  # Expired tasks are "maximally urgent" for logging
        return 1.0 / remaining  # Closer deadline = higher urgency


class WorkerLocalQueue:
    def __init__(self, worker_id: int):
        self.worker_id = worker_id
        self._queue: deque[StealableTask] = deque()

    def push(self, task: StealableTask):
        task.owner_worker = self.worker_id
        # Insert sorted by deadline (earliest first)
        inserted = False
        for i, t in enumerate(self._queue):
            if task.deadline < t.deadline:
                self._queue.insert(i, task)
                inserted = True
                break
        if not inserted:
            self._queue.append(task)

    def pop(self) -> StealableTask | None:
        return self._queue.popleft() if self._queue else None

    def steal_most_urgent(self) -> StealableTask | None:
        """Idle workers steal the most urgent task from this queue."""
        if not self._queue:
            return None
        # Find the task with earliest deadline
        best = min(self._queue, key=lambda t: t.deadline)
        self._queue.remove(best)
        return best

    def __len__(self):
        return len(self._queue)


class DeadlineAwareWorkStealingScheduler:
    """
    N worker queues. Idle workers steal the most deadline-critical tasks
    from overloaded workers to prevent SLA misses.
    """

    def __init__(self, num_workers: int = 4):
        self.num_workers = num_workers
        self._worker_queues = [WorkerLocalQueue(i) for i in range(num_workers)]
        self._next_worker = 0
        self._steal_stats = {"steals": 0, "prevented_misses": 0}

    def submit(self, task: StealableTask):
        # Round-robin initial assignment
        worker_id = self._next_worker % self.num_workers
        self._next_worker += 1
        self._worker_queues[worker_id].push(task)

    def get_task(self, worker_id: int) -> StealableTask | None:
        """Worker tries own queue first, then steals from most loaded peer."""
        task = self._worker_queues[worker_id].pop()
        if task:
            return task

        # Try to steal from the worker with the most urgent pending task
        best_task = None
        best_urgency = -1.0
        best_source = -1

        for i, q in enumerate(self._worker_queues):
            if i == worker_id or not q:
                continue
            candidate = q._queue[0] if q._queue else None
            if candidate:
                urgency = candidate.urgency()
                if urgency > best_urgency:
                    best_urgency = urgency
                    best_task = candidate
                    best_source = i

        if best_source >= 0 and best_task:
            stolen = self._worker_queues[best_source].steal_most_urgent()
            if stolen:
                self._steal_stats["steals"] += 1
                if stolen.deadline - time.time() < 5.0:
                    self._steal_stats["prevented_misses"] += 1
                print(
                    f"[steal] Worker {worker_id} stole {stolen.task_id} "
                    f"from worker {best_source} "
                    f"(deadline in {stolen.deadline - time.time():.1f}s)"
                )
                return stolen

        return None

    def queue_summary(self) -> dict:
        return {
            f"worker_{i}": {
                "depth": len(q),
                "most_urgent_deadline_in": (
                    (min(t.deadline for t in q._queue) - time.time())
                    if q._queue else None
                ),
            }
            for i, q in enumerate(self._worker_queues)
        }

    def steal_stats(self) -> dict:
        return self._steal_stats
```

---

## Comparison

| Pattern | Prevents Starvation | Handles Preemption | SLA Visibility | Best For |
|---|---|---|---|---|
| EDF priority queue | No (FIFO within same deadline) | No | Via `peek_at_risk` | Homogeneous tasks with clear deadlines |
| Multi-level escalation | Yes (tier escalation) | No | Via tier distribution | Mixed SLA classes with background jobs |
| Preemptive scheduler | Yes (re-queues preempted) | Yes | No | Real-time vs batch coexistence |
| SLA tracker + Prometheus | N/A (observability only) | N/A | Full metrics | All production agents needing SLA dashboards |
| Aging priority | Yes (built-in) | No | Via snapshot | Single queue, prevent starvation of low-pri tasks |
| Work stealing | Yes (load balancing) | No | Via queue summary | Multi-worker agents with uneven load |

**Recommendations:**
- Use **EDF scheduler** (Solution 1) as the baseline for any agent with distinct SLA tiers — it's simple and effective.
- Add **multi-level escalation** (Solution 2) when you have both interactive (< 1s SLA) and batch (> 1h SLA) tasks in the same agent.
- Use **aging priority** (Solution 5) to ensure no task ever waits indefinitely, even if mis-prioritized at submission.
- Deploy **SLA tracker** (Solution 4) in all production agents — SLA visibility is what allows you to detect scheduling failures before users do.
- Use **work stealing** (Solution 6) when multiple parallel workers serve a shared task pool and load is uneven.
- Combine EDF + aging + SLA tracking for a complete production scheduling system.
