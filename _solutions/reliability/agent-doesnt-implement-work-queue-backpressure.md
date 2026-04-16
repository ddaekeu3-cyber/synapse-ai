---
title: "Agent Doesn't Implement Work Queue Backpressure"
description: "Agents that accept and enqueue every incoming request without backpressure fill their work queues unboundedly during load spikes — consuming memory until the process crashes, then losing all queued work. Implement work queue backpressure that enforces queue depth limits, rejects new work with a retryable error when the queue is full, and signals upstream producers to slow down."
date: 2026-04-16
difficulty: intermediate
category: reliability
slug: agent-doesnt-implement-work-queue-backpressure
tags: [backpressure, work-queue, queue-depth, load-shedding, memory-pressure, overflow-protection]
symptoms:
  - "Agent process OOM-killed during traffic spikes because the in-memory queue grew unboundedly"
  - "All queued work is lost on crash — no overflow persistence or rejection signaling"
  - "Upstream producers are never told to slow down — they keep sending at full rate"
  - "Queue depth is never measured — no visibility into how full the queue is"
  - "Work items from 10 minutes ago are still queued when fresh work arrives"
---

## Why This Happens

Unbounded queues absorb any amount of work — they never reject. This feels safe but is the opposite: under sustained overload, the queue grows until memory is exhausted, and then the process crashes, losing all enqueued work. A bounded queue with backpressure rejects work when full, forcing the caller to retry later or apply their own backpressure. The queue depth limit converts an unbounded memory failure into a bounded, observable, retryable capacity signal.

## Solution 1: Bounded Work Queue

```python
import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class EnqueueResult(str, Enum):
    ACCEPTED = "accepted"
    REJECTED_FULL = "rejected_full"
    REJECTED_DRAINING = "rejected_draining"


@dataclass
class WorkItem:
    item_id: str
    payload: Any
    priority: int = 0          # higher = processed first
    enqueued_at: float = field(default_factory=time.time)
    deadline: Optional[float] = None   # absolute deadline; None = no expiry

    def is_expired(self) -> bool:
        if self.deadline is None:
            return False
        return time.time() > self.deadline


class BoundedWorkQueue:
    """
    Async work queue with a hard depth limit.
    Returns REJECTED_FULL immediately when at capacity — no blocking.
    Supports graceful drain mode that stops accepting new work.
    """

    def __init__(self, max_depth: int = 500):
        self._max = max_depth
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=max_depth)
        self._draining = False
        self._rejected = 0
        self._accepted = 0
        self._expired_dropped = 0

    def enqueue(self, item: WorkItem) -> EnqueueResult:
        if self._draining:
            self._rejected += 1
            return EnqueueResult.REJECTED_DRAINING
        try:
            self._queue.put_nowait(item)
            self._accepted += 1
            return EnqueueResult.ACCEPTED
        except asyncio.QueueFull:
            self._rejected += 1
            return EnqueueResult.REJECTED_FULL

    async def dequeue(self, timeout_seconds: float = 1.0) -> Optional[WorkItem]:
        try:
            item = await asyncio.wait_for(self._queue.get(), timeout=timeout_seconds)
            if item.is_expired():
                self._expired_dropped += 1
                self._queue.task_done()
                return None
            return item
        except asyncio.TimeoutError:
            return None

    def task_done(self) -> None:
        self._queue.task_done()

    def depth(self) -> int:
        return self._queue.qsize()

    def fill_fraction(self) -> float:
        return round(self._queue.qsize() / self._max, 4)

    def start_drain(self) -> None:
        self._draining = True

    def stop_drain(self) -> None:
        self._draining = False

    def stats(self) -> dict:
        return {
            "depth": self.depth(),
            "max_depth": self._max,
            "fill_fraction": self.fill_fraction(),
            "accepted": self._accepted,
            "rejected": self._rejected,
            "expired_dropped": self._expired_dropped,
            "rejection_rate": round(
                self._rejected / max(self._accepted + self._rejected, 1), 4
            ),
            "draining": self._draining,
        }
```

## Solution 2: Backpressure Signal Emitter

```python
import time
from typing import Callable, List, Optional


class BackpressureSignalEmitter:
    """
    Monitors queue fill fraction and emits backpressure signals
    to registered upstream producers when thresholds are crossed.
    """

    def __init__(
        self,
        queue: BoundedWorkQueue,
        warn_threshold: float = 0.70,
        shed_threshold: float = 0.90,
    ):
        self._queue = queue
        self._warn = warn_threshold
        self._shed = shed_threshold
        self._listeners: List[Callable[[str, float], None]] = []
        self._last_state = "ok"

    def register_listener(self, fn: Callable[[str, float], None]) -> None:
        """fn(state, fill_fraction) called on state transitions."""
        self._listeners.append(fn)

    def evaluate(self) -> str:
        fill = self._queue.fill_fraction()
        if fill >= self._shed:
            state = "shed"
        elif fill >= self._warn:
            state = "warn"
        else:
            state = "ok"

        if state != self._last_state:
            self._last_state = state
            for fn in self._listeners:
                try:
                    fn(state, fill)
                except Exception:
                    pass

        return state

    def should_accept(self) -> bool:
        return self.evaluate() != "shed"

    def current_state(self) -> dict:
        fill = self._queue.fill_fraction()
        return {
            "state": self._last_state,
            "fill_fraction": fill,
            "accepting": fill < self._shed,
        }
```

## Solution 3: Priority Queue with Expiry Eviction

```python
import heapq
import time
from threading import Lock
from typing import List, Optional, Tuple


class PriorityWorkQueue:
    """
    Min-heap priority queue that evicts expired items on enqueue
    to prevent stale work from blocking high-priority fresh work.
    Higher priority value = processed first (stored negated in heap).
    """

    def __init__(self, max_depth: int = 500):
        self._max = max_depth
        self._heap: List[Tuple[int, float, WorkItem]] = []
        self._lock = Lock()
        self._counter = 0
        self._evicted = 0

    def enqueue(self, item: WorkItem) -> EnqueueResult:
        with self._lock:
            self._evict_expired()
            if len(self._heap) >= self._max:
                return EnqueueResult.REJECTED_FULL
            self._counter += 1
            heapq.heappush(self._heap, (-item.priority, self._counter, item))
            return EnqueueResult.ACCEPTED

    def dequeue(self) -> Optional[WorkItem]:
        with self._lock:
            while self._heap:
                _, _, item = heapq.heappop(self._heap)
                if not item.is_expired():
                    return item
                self._evicted += 1
            return None

    def _evict_expired(self) -> None:
        now = time.time()
        fresh = [
            entry for entry in self._heap
            if entry[2].deadline is None or entry[2].deadline > now
        ]
        evicted = len(self._heap) - len(fresh)
        if evicted > 0:
            self._evicted += evicted
            heapq.heapify(fresh)
            self._heap = fresh

    def depth(self) -> int:
        with self._lock:
            return len(self._heap)

    def fill_fraction(self) -> float:
        return round(self.depth() / self._max, 4)

    def evicted_count(self) -> int:
        return self._evicted
```

## Solution 4: Queue Worker Pool

```python
import asyncio
import time
from typing import Any, Callable, Optional


class QueueWorkerPool:
    """
    Runs N concurrent workers that drain a BoundedWorkQueue.
    Each worker dequeues one item, processes it, and loops.
    Workers stop cleanly when the queue is drained and shutdown is requested.
    """

    def __init__(
        self,
        queue: BoundedWorkQueue,
        worker_count: int = 4,
    ):
        self._queue = queue
        self._worker_count = worker_count
        self._tasks: list = []
        self._processed = 0
        self._errors = 0
        self._running = False

    async def start(self, process_fn: Callable[[WorkItem], Any]) -> None:
        self._running = True
        self._tasks = [
            asyncio.create_task(self._worker(process_fn, i))
            for i in range(self._worker_count)
        ]

    async def _worker(self, process_fn: Callable, worker_id: int) -> None:
        while self._running:
            item = await self._queue.dequeue(timeout_seconds=0.5)
            if item is None:
                continue
            try:
                await process_fn(item)
                self._processed += 1
            except Exception:
                self._errors += 1
            finally:
                self._queue.task_done()

    async def shutdown(self) -> None:
        self._running = False
        await asyncio.gather(*self._tasks, return_exceptions=True)

    def stats(self) -> dict:
        return {
            "workers": self._worker_count,
            "processed": self._processed,
            "errors": self._errors,
            "running": self._running,
        }
```

## Solution 5: Backpressure-Aware Request Acceptor

```python
import time
from typing import Any, Optional


class BackpressureAwareRequestAcceptor:
    """
    Entry point for incoming requests. Checks backpressure state
    before enqueuing. Returns a structured accept/reject decision
    with retry guidance for rejected callers.
    """

    def __init__(
        self,
        queue: BoundedWorkQueue,
        emitter: BackpressureSignalEmitter,
    ):
        self._queue = queue
        self._emitter = emitter

    def accept(self, item: WorkItem) -> dict:
        if not self._emitter.should_accept():
            return {
                "accepted": False,
                "reason": "queue_full",
                "retry_after_seconds": 5.0,
                "fill_fraction": self._queue.fill_fraction(),
                "http_status": 503,
            }

        result = self._queue.enqueue(item)
        if result == EnqueueResult.ACCEPTED:
            return {
                "accepted": True,
                "item_id": item.item_id,
                "queue_depth": self._queue.depth(),
                "fill_fraction": self._queue.fill_fraction(),
                "http_status": 202,
            }

        return {
            "accepted": False,
            "reason": result.value,
            "retry_after_seconds": 3.0,
            "fill_fraction": self._queue.fill_fraction(),
            "http_status": 503,
        }
```

## Solution 6: Work Queue Backpressure Dashboard

```python
import time


class WorkQueueBackpressureDashboard:
    """Combines queue stats, backpressure state, and worker metrics."""

    def __init__(
        self,
        queue: BoundedWorkQueue,
        emitter: BackpressureSignalEmitter,
        worker_pool: QueueWorkerPool,
    ):
        self._queue = queue
        self._emitter = emitter
        self._pool = worker_pool

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "queue": self._queue.stats(),
            "backpressure": self._emitter.current_state(),
            "workers": self._pool.stats(),
            "health": (
                "healthy" if self._queue.fill_fraction() < 0.70
                else "degraded" if self._queue.fill_fraction() < 0.90
                else "critical"
            ),
        }
```

## Comparison

| Approach | Bounded Depth | Rejection | Backpressure Signals | Priority + Expiry | Worker Pool |
|---|---|---|---|---|---|
| BoundedWorkQueue | Yes | Yes (immediate) | No | No | No |
| BackpressureSignalEmitter | Via queue | No | Yes (listeners) | No | No |
| PriorityWorkQueue | Yes | Yes | No | Yes (both) | No |
| QueueWorkerPool | No | No | No | No | Yes |
| BackpressureAwareRequestAcceptor | Via queue | Yes (+HTTP 503) | Via emitter | No | No |

**Best for production**: Set `max_depth` based on memory budget: each work item in a typical agent queue occupies 1–5 KB, so a 500-item queue uses 0.5–2.5 MB — well within budget. Return HTTP 503 with `Retry-After: 5` for rejected requests; callers that respect this naturally implement client-side backpressure without coordination. Use `PriorityWorkQueue` when interactive user requests must not be delayed by background batch jobs — assign interactive requests priority 10 and background jobs priority 1. Monitor `fill_fraction` as a leading indicator: sustained fill above 0.70 means worker throughput is below ingestion rate and more workers or faster processing are needed.
