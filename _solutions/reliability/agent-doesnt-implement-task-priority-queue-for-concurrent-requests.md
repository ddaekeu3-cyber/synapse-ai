---
title: "Agent Doesn't Implement Task Priority Queue for Concurrent Requests"
description: "Agents that process all incoming requests with equal priority under load allow low-importance background tasks to starve high-importance user requests: a scheduled report generation job consuming all worker threads prevents real-time user queries from being served. Implement a priority queue that ensures urgent requests are processed first, with starvation prevention for lower-priority work."
date: 2026-04-16
difficulty: advanced
category: reliability
slug: agent-doesnt-implement-task-priority-queue-for-concurrent-requests
tags: [priority-queue, task-scheduling, request-prioritization, starvation-prevention, concurrency, load-management]
symptoms:
  - "Real-time user requests wait behind queued background jobs during peak load"
  - "No priority distinction between interactive queries and scheduled batch tasks"
  - "P99 latency for high-priority requests degrades identically to low-priority requests under load"
  - "Background tasks starve indefinitely when high-priority load is sustained"
  - "Queue depth monitoring does not distinguish between request types"
---

## Why This Happens

First-in-first-out queues treat all requests identically. Under load, a burst of low-priority background tasks fills the queue and blocks all subsequent high-priority requests until the burst drains. Priority queues assign numerical priority to each request and always dequeue the highest-priority available request. Without starvation prevention, low-priority requests can wait indefinitely if high-priority requests arrive continuously — a bounded wait time (priority aging) is required to ensure all requests eventually execute.

## Solution 1: Priority Task

```python
import time
import uuid
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Callable, Dict, Optional


class TaskPriority(IntEnum):
    CRITICAL = 0     # system-level operations (health checks, circuit breakers)
    HIGH = 1         # real-time user interactive requests
    NORMAL = 2       # standard user requests
    LOW = 3          # background jobs, report generation
    BATCH = 4        # scheduled batch tasks


@dataclass(order=True)
class PriorityTask:
    effective_priority: float = field(compare=True)  # lower = higher priority
    task_id: str = field(compare=False, default_factory=lambda: str(uuid.uuid4())[:16])
    base_priority: TaskPriority = field(compare=False, default=TaskPriority.NORMAL)
    fn: Callable = field(compare=False, default=None)
    args: Dict[str, Any] = field(compare=False, default_factory=dict)
    created_at: float = field(compare=False, default_factory=time.time)
    deadline: Optional[float] = field(compare=False, default=None)
    conversation_id: str = field(compare=False, default="")
    metadata: Dict[str, Any] = field(compare=False, default_factory=dict)

    @property
    def age_seconds(self) -> float:
        return time.time() - self.created_at

    @property
    def is_expired(self) -> bool:
        return self.deadline is not None and time.time() > self.deadline
```

## Solution 2: Priority Queue with Aging

```python
import asyncio
import heapq
import time
from threading import Lock
from typing import List, Optional


class AgingPriorityQueue:
    """
    Priority queue that applies aging to prevent starvation.
    Every `aging_interval_seconds`, each waiting task's effective priority
    is decreased by `aging_step`, eventually elevating low-priority tasks.
    """

    def __init__(
        self,
        max_size: int = 10000,
        aging_interval_seconds: float = 30.0,
        aging_step: float = 0.5,   # decrease effective priority by this amount
    ):
        self._heap: List[PriorityTask] = []
        self._max = max_size
        self._lock = Lock()
        self._aging_interval = aging_interval_seconds
        self._aging_step = aging_step
        self._last_aging = time.time()
        self._enqueue_count = 0
        self._dequeue_count = 0
        self._dropped_count = 0

    def put(self, task: PriorityTask) -> bool:
        with self._lock:
            if len(self._heap) >= self._max:
                self._dropped_count += 1
                return False
            task.effective_priority = float(task.base_priority)
            heapq.heappush(self._heap, task)
            self._enqueue_count += 1
            self._maybe_age()
            return True

    def get(self, timeout_seconds: float = 0.1) -> Optional[PriorityTask]:
        with self._lock:
            self._maybe_age()
            while self._heap:
                task = heapq.heappop(self._heap)
                self._dequeue_count += 1
                if task.is_expired:
                    continue   # skip expired tasks
                return task
        return None

    def _maybe_age(self) -> None:
        now = time.time()
        if now - self._last_aging < self._aging_interval:
            return
        self._last_aging = now
        for task in self._heap:
            task.effective_priority = max(0.0, task.effective_priority - self._aging_step)
        heapq.heapify(self._heap)

    def size(self) -> int:
        with self._lock:
            return len(self._heap)

    def size_by_priority(self) -> dict:
        with self._lock:
            result: dict = {}
            for task in self._heap:
                name = task.base_priority.name
                result[name] = result.get(name, 0) + 1
            return result

    def stats(self) -> dict:
        return {
            "enqueue_count": self._enqueue_count,
            "dequeue_count": self._dequeue_count,
            "dropped_count": self._dropped_count,
            "current_size": self.size(),
            "by_priority": self.size_by_priority(),
        }
```

## Solution 3: Priority Worker Pool

```python
import asyncio
import time
from typing import Callable, Optional


class PriorityWorkerPool:
    """
    Runs a fixed number of async workers that process tasks from
    the priority queue in priority order.
    """

    def __init__(
        self,
        queue: AgingPriorityQueue,
        num_workers: int = 10,
        poll_interval_seconds: float = 0.05,
    ):
        self._queue = queue
        self._num_workers = num_workers
        self._poll_interval = poll_interval_seconds
        self._running = False
        self._processed = 0
        self._errors = 0
        self._latencies: list = []

    async def _worker(self) -> None:
        while self._running:
            task = self._queue.get()
            if task is None:
                await asyncio.sleep(self._poll_interval)
                continue

            start = time.time()
            try:
                await task.fn(**task.args)
                self._processed += 1
            except Exception:
                self._errors += 1

            latency_ms = (time.time() - start) * 1000
            self._latencies.append(latency_ms)
            if len(self._latencies) > 10000:
                self._latencies.pop(0)

    async def start(self) -> None:
        self._running = True
        self._workers = [
            asyncio.ensure_future(self._worker())
            for _ in range(self._num_workers)
        ]

    async def stop(self) -> None:
        self._running = False
        await asyncio.gather(*self._workers, return_exceptions=True)

    def stats(self) -> dict:
        p99 = None
        if self._latencies:
            sorted_lat = sorted(self._latencies)
            p99 = round(sorted_lat[int(len(sorted_lat) * 0.99)], 2)
        return {
            "processed": self._processed,
            "errors": self._errors,
            "p99_latency_ms": p99,
            "queue_stats": self._queue.stats(),
        }
```

## Solution 4: Priority-Aware Request Classifier

```python
from typing import Any, Dict


class RequestPriorityClassifier:
    """
    Classifies incoming requests by priority based on source,
    content type, and agent metadata.
    """

    def classify(self, request: Dict[str, Any]) -> TaskPriority:
        source = request.get("source", "")
        request_type = request.get("type", "")
        is_interactive = request.get("interactive", True)
        user_tier = request.get("user_tier", "standard")

        # System operations always critical
        if request_type in ("health_check", "circuit_breaker", "system"):
            return TaskPriority.CRITICAL

        # Premium users get high priority
        if user_tier == "premium" and is_interactive:
            return TaskPriority.HIGH

        # Interactive user sessions
        if is_interactive and source == "user":
            return TaskPriority.NORMAL

        # Background/scheduled jobs
        if source in ("scheduler", "cron", "batch"):
            return TaskPriority.BATCH

        # Non-interactive requests
        if not is_interactive:
            return TaskPriority.LOW

        return TaskPriority.NORMAL
```

## Solution 5: Deadline-Aware Task Submitter

```python
import time
from typing import Any, Callable, Dict, Optional


class DeadlineAwareTaskSubmitter:
    """
    Submits tasks to the priority queue with optional deadlines.
    Tasks that exceed their deadline are automatically skipped.
    """

    def __init__(
        self,
        queue: AgingPriorityQueue,
        classifier: RequestPriorityClassifier,
    ):
        self._queue = queue
        self._classifier = classifier
        self._submit_count = 0
        self._rejected_count = 0

    def submit(
        self,
        fn: Callable,
        args: Dict[str, Any],
        request_meta: Dict[str, Any],
        deadline_seconds: Optional[float] = None,
        conversation_id: str = "",
    ) -> Optional[str]:
        priority = self._classifier.classify(request_meta)
        deadline = time.time() + deadline_seconds if deadline_seconds else None

        task = PriorityTask(
            effective_priority=float(priority),
            base_priority=priority,
            fn=fn,
            args=args,
            deadline=deadline,
            conversation_id=conversation_id,
            metadata=request_meta,
        )

        accepted = self._queue.put(task)
        if accepted:
            self._submit_count += 1
            return task.task_id
        else:
            self._rejected_count += 1
            return None

    def stats(self) -> dict:
        return {
            "submit_count": self._submit_count,
            "rejected_count": self._rejected_count,
            "rejection_rate": round(self._rejected_count / max(self._submit_count, 1), 4),
        }
```

## Solution 6: Priority Queue Dashboard

```python
import time


class PriorityQueueDashboard:
    """
    Combines queue depth by priority, worker pool stats, and
    submitter stats into a load management health view.
    """

    def __init__(
        self,
        queue: AgingPriorityQueue,
        worker_pool: PriorityWorkerPool,
        submitter: DeadlineAwareTaskSubmitter,
    ):
        self._queue = queue
        self._pool = worker_pool
        self._submitter = submitter

    def render(self) -> dict:
        queue_stats = self._queue.stats()
        pool_stats = self._pool.stats()
        submit_stats = self._submitter.stats()

        high_prio_count = queue_stats["by_priority"].get("HIGH", 0) + queue_stats["by_priority"].get("CRITICAL", 0)
        batch_count = queue_stats["by_priority"].get("BATCH", 0)

        return {
            "generated_at": time.time(),
            "queue_stats": queue_stats,
            "pool_stats": pool_stats,
            "submitter_stats": submit_stats,
            "high_priority_waiting": high_prio_count,
            "batch_waiting": batch_count,
            "alert": queue_stats["dropped_count"] > 0 or submit_stats["rejection_rate"] > 0.05,
        }
```

## Comparison

| Approach | Priority Ordering | Starvation Prevention | Deadline Support | Classification | Dashboard |
|---|---|---|---|---|---|
| AgingPriorityQueue | Yes (heapq) | Yes (aging) | Yes (skip expired) | No | No |
| PriorityWorkerPool | Via queue | Via queue | Via queue | No | No |
| RequestPriorityClassifier | No | No | No | Yes | No |
| DeadlineAwareTaskSubmitter | Via queue | Via queue | Yes | Via classifier | No |
| PriorityQueueDashboard | No | No | No | No | Yes |

**Best for production**: Reserve `CRITICAL` and `HIGH` priorities exclusively for real-time interactive requests — overuse of high priority eliminates the benefit. Set `aging_step=0.5` and `aging_interval_seconds=30`: a BATCH task (priority=4) will be promoted to CRITICAL level (priority=0) after 4 minutes of waiting (4/0.5 × 30s), preventing indefinite starvation. Set `max_size` based on memory: each `PriorityTask` object is lightweight (~200 bytes), so 10,000-task queues use about 2MB. Alert on `rejection_rate > 0.05` — a 5%+ rejection rate means the queue is full and either workers need scaling or low-priority task submission needs throttling.
