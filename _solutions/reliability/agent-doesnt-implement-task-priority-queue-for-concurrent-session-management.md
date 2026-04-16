---
title: "Agent Doesn't Implement Task Priority Queue for Concurrent Session Management"
description: "Agents that treat all concurrent sessions equally under load allow low-priority background tasks to consume the same compute and API capacity as urgent interactive sessions — a batch summarization job scheduled at the same priority as a real-time user query will starve the user if resources are constrained. Implement a task priority queue that ranks sessions by urgency, processes high-priority tasks first, and preempts or throttles lower-priority work when capacity is limited."
date: 2026-04-16
difficulty: intermediate
category: reliability
slug: agent-doesnt-implement-task-priority-queue-for-concurrent-session-management
tags: [priority-queue, session-management, concurrency, task-scheduling, preemption, resource-fairness]
symptoms:
  - "Interactive user sessions delayed because background batch jobs consume all API capacity"
  - "No distinction between urgent real-time tasks and deferrable background processing"
  - "All sessions receive equal resources regardless of business priority"
  - "SLA for interactive sessions violated during batch processing windows"
  - "No mechanism to defer or throttle low-priority work when capacity is constrained"
---

## Why This Happens

Concurrency primitives like asyncio semaphores or thread pools enforce capacity limits but not priority ordering. When multiple sessions compete for a limited number of LLM API slots, first-come-first-served scheduling gives equal treatment to a background batch job submitted milliseconds before an interactive user query. Priority queues invert this: higher-priority tasks always acquire capacity first. Implementing priority for LLM-backed agents requires tagging tasks with priority levels, queuing them in a priority-ordered structure, and dispatching them through a capacity-limited gate that always serves the highest-priority pending task first.

## Solution 1: Task Priority Levels

```python
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Dict, Optional
import time
import uuid


class TaskPriority(IntEnum):
    CRITICAL = 0      # system alerts, SLA-bound operations
    HIGH = 1          # interactive user sessions
    NORMAL = 2        # standard API requests
    LOW = 3           # background processing, non-urgent summaries
    BATCH = 4         # bulk jobs, scheduled tasks


@dataclass
class PrioritizedTask:
    task_id: str
    priority: TaskPriority
    session_id: str
    payload: Any
    enqueued_at: float = field(default_factory=time.time)
    deadline: Optional[float] = None   # unix timestamp; None = no deadline
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __lt__(self, other: "PrioritizedTask") -> bool:
        # Lower priority value = higher urgency; break ties by enqueue time
        if self.priority != other.priority:
            return self.priority < other.priority
        return self.enqueued_at < other.enqueued_at

    def is_expired(self) -> bool:
        if self.deadline is None:
            return False
        return time.time() > self.deadline

    @classmethod
    def create(
        cls,
        priority: TaskPriority,
        session_id: str,
        payload: Any,
        deadline_seconds: Optional[float] = None,
    ) -> "PrioritizedTask":
        return cls(
            task_id=uuid.uuid4().hex,
            priority=priority,
            session_id=session_id,
            payload=payload,
            deadline=time.time() + deadline_seconds if deadline_seconds else None,
        )
```

## Solution 2: Priority Queue

```python
import heapq
import time
from threading import Lock
from typing import List, Optional


class AgentTaskPriorityQueue:
    """
    Thread-safe priority queue for agent tasks.
    Always returns the highest-priority (lowest priority value) task.
    Expired tasks (past deadline) are dropped silently on dequeue.
    """

    def __init__(self, max_size: int = 10000):
        self._heap: List[PrioritizedTask] = []
        self._max = max_size
        self._lock = Lock()
        self._total_enqueued = 0
        self._total_dropped_expired = 0
        self._total_dropped_overflow = 0

    def enqueue(self, task: PrioritizedTask) -> bool:
        with self._lock:
            if len(self._heap) >= self._max:
                # Drop the lowest-priority task if new task is higher priority
                if self._heap and task < self._heap[-1]:
                    self._heap.sort()   # ensure heap property
                    self._heap.pop()
                    self._total_dropped_overflow += 1
                else:
                    self._total_dropped_overflow += 1
                    return False

            heapq.heappush(self._heap, task)
            self._total_enqueued += 1
            return True

    def dequeue(self) -> Optional[PrioritizedTask]:
        with self._lock:
            while self._heap:
                task = heapq.heappop(self._heap)
                if task.is_expired():
                    self._total_dropped_expired += 1
                    continue
                return task
            return None

    def peek_priority(self) -> Optional[TaskPriority]:
        with self._lock:
            if self._heap:
                return self._heap[0].priority
            return None

    def depth(self) -> int:
        with self._lock:
            return len(self._heap)

    def stats(self) -> dict:
        with self._lock:
            by_priority: dict = {}
            for task in self._heap:
                p = task.priority.name
                by_priority[p] = by_priority.get(p, 0) + 1
            return {
                "depth": len(self._heap),
                "total_enqueued": self._total_enqueued,
                "total_dropped_expired": self._total_dropped_expired,
                "total_dropped_overflow": self._total_dropped_overflow,
                "by_priority": by_priority,
            }
```

## Solution 3: Priority-Aware Dispatcher

```python
import asyncio
import time
from typing import Any, Callable, Dict, Optional


class PriorityAwareDispatcher:
    """
    Processes tasks from the priority queue through a capacity-limited gate.
    Higher-priority tasks always acquire a worker slot before lower-priority tasks.
    Supports per-priority concurrency limits to prevent starvation.
    """

    def __init__(
        self,
        queue: AgentTaskPriorityQueue,
        total_capacity: int = 10,
        per_priority_limits: Optional[Dict[TaskPriority, int]] = None,
    ):
        self._queue = queue
        self._total_sem = asyncio.Semaphore(total_capacity)
        self._priority_sems: Dict[TaskPriority, asyncio.Semaphore] = {}
        if per_priority_limits:
            for priority, limit in per_priority_limits.items():
                self._priority_sems[priority] = asyncio.Semaphore(limit)
        self._processed = 0
        self._processing_times: list = []

    async def dispatch_next(
        self,
        handler_fn: Callable,   # async fn(task: PrioritizedTask) -> Any
    ) -> Optional[dict]:
        task = self._queue.dequeue()
        if task is None:
            return None

        priority_sem = self._priority_sems.get(task.priority)

        async def _execute() -> None:
            start = time.time()
            try:
                await handler_fn(task)
            finally:
                elapsed = round((time.time() - start) * 1000, 2)
                self._processing_times.append(elapsed)
                if len(self._processing_times) > 1000:
                    self._processing_times.pop(0)
                self._processed += 1

        async with self._total_sem:
            if priority_sem:
                async with priority_sem:
                    await _execute()
            else:
                await _execute()

        return {"task_id": task.task_id, "priority": task.priority.name}

    def stats(self) -> dict:
        avg_ms = (
            round(sum(self._processing_times) / len(self._processing_times), 2)
            if self._processing_times else None
        )
        return {
            "processed_tasks": self._processed,
            "avg_processing_ms": avg_ms,
        }
```

## Solution 4: Deadline Monitor

```python
import time
from typing import List


class TaskDeadlineMonitor:
    """
    Tracks tasks with deadlines and reports how many are at risk
    of expiring while waiting in the queue.
    """

    def __init__(self, queue: AgentTaskPriorityQueue):
        self._queue = queue
        self._deadline_violations: List[dict] = []

    def check(self, warn_seconds_before_deadline: float = 5.0) -> List[dict]:
        now = time.time()
        at_risk = []
        with self._queue._lock:
            for task in self._queue._heap:
                if task.deadline is not None:
                    time_remaining = task.deadline - now
                    if time_remaining <= warn_seconds_before_deadline:
                        at_risk.append({
                            "task_id": task.task_id,
                            "priority": task.priority.name,
                            "session_id": task.session_id,
                            "seconds_remaining": round(time_remaining, 2),
                            "expired": time_remaining <= 0,
                        })
        return at_risk

    def record_violation(self, task: PrioritizedTask) -> None:
        self._deadline_violations.append({
            "ts": time.time(),
            "task_id": task.task_id,
            "priority": task.priority.name,
            "session_id": task.session_id,
        })

    def violation_count(self, window_seconds: float = 3600.0) -> int:
        cutoff = time.time() - window_seconds
        return sum(1 for v in self._deadline_violations if v["ts"] >= cutoff)
```

## Solution 5: Priority Queue Load Balancer

```python
from typing import Dict, List


class PriorityQueueLoadBalancer:
    """
    Routes incoming tasks to one of multiple priority queues based
    on session metadata. Enables separate queues for different tenants
    or workload types while maintaining global priority ordering.
    """

    def __init__(self):
        self._queues: Dict[str, AgentTaskPriorityQueue] = {}
        self._routing_rules: Dict[str, str] = {}  # session_pattern -> queue_name

    def add_queue(self, name: str, queue: AgentTaskPriorityQueue) -> None:
        self._queues[name] = queue

    def add_routing_rule(self, session_prefix: str, queue_name: str) -> None:
        self._routing_rules[session_prefix] = queue_name

    def route(self, task: PrioritizedTask) -> bool:
        for prefix, queue_name in self._routing_rules.items():
            if task.session_id.startswith(prefix):
                queue = self._queues.get(queue_name)
                if queue:
                    return queue.enqueue(task)

        # Default queue
        default = self._queues.get("default")
        return default.enqueue(task) if default else False

    def aggregate_stats(self) -> Dict[str, dict]:
        return {name: q.stats() for name, q in self._queues.items()}
```

## Solution 6: Priority Queue Dashboard

```python
import time


class TaskPriorityQueueDashboard:
    """
    Combines queue depth, priority distribution, dispatcher throughput,
    and deadline violation counts into a single scheduling health view.
    """

    def __init__(
        self,
        queue: AgentTaskPriorityQueue,
        dispatcher: PriorityAwareDispatcher,
        deadline_monitor: TaskDeadlineMonitor,
    ):
        self._queue = queue
        self._dispatcher = dispatcher
        self._monitor = deadline_monitor

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "queue": self._queue.stats(),
            "dispatcher": self._dispatcher.stats(),
            "at_risk_tasks": len(self._monitor.check()),
            "deadline_violations_1h": self._monitor.violation_count(3600.0),
        }
```

## Comparison

| Approach | Priority Ordering | Capacity Limiting | Deadline Enforcement | Multi-Queue Routing | Dashboard |
|---|---|---|---|---|---|
| AgentTaskPriorityQueue | Yes (heap) | No | Expiry on dequeue | No | No |
| PriorityAwareDispatcher | Via queue | Yes (semaphore) | No | No | No |
| TaskDeadlineMonitor | No | No | Yes (pre-expiry warn) | No | No |
| PriorityQueueLoadBalancer | Via queues | Via queues | No | Yes | No |
| TaskPriorityQueueDashboard | No | No | No | No | Yes |

**Best for production**: Set `per_priority_limits` in `PriorityAwareDispatcher` to cap BATCH tasks at 20% of total capacity — this prevents batch jobs from consuming all workers even when the queue is deep. Use `TaskPriority.HIGH` for interactive sessions and `TaskPriority.BATCH` for scheduled jobs; never use `TaskPriority.CRITICAL` for routine work — reserve it for health checks and circuit breaker resets that must run even during overload. Set `deadline_seconds=30` for interactive sessions: if a session waits more than 30 seconds in the queue, it has already missed its response-time SLA and should be expired rather than processed. Monitor `total_dropped_overflow` in queue stats: a non-zero value indicates the queue is persistently overloaded and either capacity needs to increase or lower-priority work needs to be throttled at the ingestion layer.
