---
title: "Agent Doesn't Implement Tool Call Queue Overflow Protection"
description: "Agents that dispatch tool calls into an unbounded in-memory queue can exhaust memory when bursts of concurrent sessions each trigger multiple tool calls simultaneously. An unbounded queue accepts work faster than it can be processed, growing until the process OOM-kills. Implement tool call queue overflow protection with a configurable capacity limit, back-pressure signaling, and overflow eviction policies that preserve the most important work."
date: 2026-04-16
difficulty: advanced
category: reliability
slug: agent-doesnt-implement-tool-call-queue-overflow-protection
tags: [queue-overflow, back-pressure, tool-dispatch, memory-protection, load-shedding, priority-eviction]
symptoms:
  - "Process OOM-killed during traffic bursts because the tool call queue grows without limit"
  - "No back-pressure: callers enqueue work faster than workers can process it"
  - "All queued tool calls are equally prioritized — critical calls dropped alongside low-priority ones"
  - "Queue depth is never measured — overflow only detected at OOM"
  - "No rejection signal returned to callers when the queue is full"
---

## Why This Happens

Unbounded queues are the default in most async frameworks: `asyncio.Queue()` without a maxsize grows indefinitely. Under a burst, enqueue operations succeed immediately while dequeue falls behind, causing memory to accumulate until the process dies. Bounded queues prevent this but require a decision about what to do when the queue is full: reject (back-pressure to caller), drop the lowest-priority item (eviction), or wait (blocking, which shifts the problem upstream). Overflow protection requires both a capacity limit and a deliberate overflow policy.

## Solution 1: Queue Item

```python
import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Callable, Dict, Optional


class ToolCallPriority(IntEnum):
    CRITICAL = 0
    HIGH = 1
    NORMAL = 2
    LOW = 3
    BACKGROUND = 4


@dataclass
class ToolCallQueueItem:
    tool_name: str
    args: Dict[str, Any]
    handler: Callable
    priority: ToolCallPriority = ToolCallPriority.NORMAL
    enqueued_at: float = field(default_factory=time.time)
    session_id: str = ""
    call_id: str = ""
    deadline: Optional[float] = None   # absolute timestamp; None = no deadline

    def is_expired(self) -> bool:
        return self.deadline is not None and time.time() > self.deadline

    def __lt__(self, other: "ToolCallQueueItem") -> bool:
        if self.priority != other.priority:
            return self.priority < other.priority   # lower value = higher priority
        return self.enqueued_at < other.enqueued_at
```

## Solution 2: Overflow Policy

```python
from enum import Enum


class OverflowPolicy(str, Enum):
    REJECT = "reject"                   # raise immediately, return error to caller
    DROP_LOWEST_PRIORITY = "drop_lowest"  # evict the lowest-priority item
    DROP_OLDEST = "drop_oldest"         # evict the oldest item regardless of priority
    WAIT = "wait"                       # block caller until space is available
```

## Solution 3: Bounded Tool Call Queue

```python
import asyncio
import heapq
import time
from threading import Lock
from typing import List, Optional


class BoundedToolCallQueue:
    """
    Priority queue with a hard capacity limit and configurable overflow policy.
    Items are ordered by (priority, enqueued_at). Expired items are silently
    discarded on dequeue.
    """

    def __init__(
        self,
        max_size: int = 1000,
        overflow_policy: OverflowPolicy = OverflowPolicy.REJECT,
    ):
        self._max = max_size
        self._policy = overflow_policy
        self._heap: List[ToolCallQueueItem] = []
        self._lock = asyncio.Lock()
        self._not_empty = asyncio.Event()
        self._enqueued = 0
        self._rejected = 0
        self._evicted = 0
        self._expired = 0

    async def enqueue(self, item: ToolCallQueueItem) -> bool:
        async with self._lock:
            if len(self._heap) < self._max:
                heapq.heappush(self._heap, item)
                self._enqueued += 1
                self._not_empty.set()
                return True

            if self._policy == OverflowPolicy.REJECT:
                self._rejected += 1
                return False

            if self._policy == OverflowPolicy.DROP_LOWEST_PRIORITY:
                worst = max(self._heap, key=lambda x: (x.priority, x.enqueued_at))
                if worst.priority > item.priority:
                    self._heap.remove(worst)
                    heapq.heapify(self._heap)
                    heapq.heappush(self._heap, item)
                    self._evicted += 1
                    self._enqueued += 1
                    return True
                else:
                    self._rejected += 1
                    return False

            if self._policy == OverflowPolicy.DROP_OLDEST:
                oldest = min(self._heap, key=lambda x: x.enqueued_at)
                self._heap.remove(oldest)
                heapq.heapify(self._heap)
                heapq.heappush(self._heap, item)
                self._evicted += 1
                self._enqueued += 1
                return True

        return False

    async def dequeue(self, timeout: float = 5.0) -> Optional[ToolCallQueueItem]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            async with self._lock:
                while self._heap:
                    item = heapq.heappop(self._heap)
                    if item.is_expired():
                        self._expired += 1
                        continue
                    if not self._heap:
                        self._not_empty.clear()
                    return item
                self._not_empty.clear()
            try:
                await asyncio.wait_for(self._not_empty.wait(), timeout=min(0.1, deadline - time.time()))
            except asyncio.TimeoutError:
                pass
        return None

    def depth(self) -> int:
        return len(self._heap)

    def utilization(self) -> float:
        return round(len(self._heap) / max(self._max, 1), 4)

    def stats(self) -> dict:
        return {
            "current_depth": self.depth(),
            "max_size": self._max,
            "utilization": self.utilization(),
            "total_enqueued": self._enqueued,
            "rejected": self._rejected,
            "evicted": self._evicted,
            "expired": self._expired,
            "overflow_policy": self._policy.value,
        }
```

## Solution 4: Queue Worker Pool

```python
import asyncio
import time
from typing import Optional


class ToolCallQueueWorkerPool:
    """
    Runs N concurrent workers that dequeue and execute tool calls.
    Tracks per-worker utilization and execution latency.
    """

    def __init__(
        self,
        queue: BoundedToolCallQueue,
        worker_count: int = 10,
    ):
        self._queue = queue
        self._worker_count = worker_count
        self._workers = []
        self._running = False
        self._processed = 0
        self._failed = 0

    async def start(self) -> None:
        self._running = True
        self._workers = [
            asyncio.ensure_future(self._worker(i))
            for i in range(self._worker_count)
        ]

    async def stop(self) -> None:
        self._running = False
        for w in self._workers:
            w.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)

    async def _worker(self, worker_id: int) -> None:
        while self._running:
            item = await self._queue.dequeue(timeout=1.0)
            if item is None:
                continue
            try:
                await item.handler(**item.args)
                self._processed += 1
            except Exception:
                self._failed += 1

    def stats(self) -> dict:
        return {
            "worker_count": self._worker_count,
            "processed": self._processed,
            "failed": self._failed,
            "queue_stats": self._queue.stats(),
        }
```

## Solution 5: Back-Pressure Signal

```python
import time
from typing import Callable, Optional


class QueueBackPressureSignal:
    """
    Monitors queue utilization and emits back-pressure signals to
    upstream session dispatchers when the queue crosses thresholds.
    Provides a check method that callers can poll before enqueuing.
    """

    def __init__(
        self,
        queue: BoundedToolCallQueue,
        warn_threshold: float = 0.70,
        reject_threshold: float = 0.90,
        on_pressure: Optional[Callable[[str, float], None]] = None,
    ):
        self._queue = queue
        self._warn = warn_threshold
        self._reject = reject_threshold
        self._on_pressure = on_pressure

    def check(self) -> tuple[bool, str]:
        util = self._queue.utilization()
        if util >= self._reject:
            if self._on_pressure:
                self._on_pressure("reject", util)
            return False, f"queue full ({util:.0%} utilized) — rejecting new work"
        if util >= self._warn:
            if self._on_pressure:
                self._on_pressure("warn", util)
            return True, f"queue under pressure ({util:.0%} utilized)"
        return True, "ok"
```

## Solution 6: Queue Overflow Dashboard

```python
import time


class ToolCallQueueOverflowDashboard:
    """
    Combines queue stats, worker pool stats, and back-pressure
    status into a single operational snapshot.
    """

    def __init__(
        self,
        pool: ToolCallQueueWorkerPool,
        back_pressure: QueueBackPressureSignal,
    ):
        self._pool = pool
        self._back_pressure = back_pressure

    def render(self) -> dict:
        pool_stats = self._pool.stats()
        bp_ok, bp_reason = self._back_pressure.check()
        return {
            "generated_at": time.time(),
            "queue": pool_stats["queue_stats"],
            "workers": {
                "count": pool_stats["worker_count"],
                "processed": pool_stats["processed"],
                "failed": pool_stats["failed"],
            },
            "back_pressure": {
                "accepting": bp_ok,
                "reason": bp_reason,
                "utilization": pool_stats["queue_stats"]["utilization"],
            },
        }
```

## Comparison

| Approach | Hard Capacity Limit | Overflow Policy | Priority Eviction | Back-Pressure Signal | Worker Pool |
|---|---|---|---|---|---|
| BoundedToolCallQueue | Yes | Yes (4 policies) | Via DROP_LOWEST | No | No |
| ToolCallQueueWorkerPool | Via queue | No | No | No | Yes |
| QueueBackPressureSignal | No | No | No | Yes (warn/reject) | No |
| ToolCallQueueOverflowDashboard | No | No | No | No | No |

**Best for production**: Use `OverflowPolicy.DROP_LOWEST_PRIORITY` rather than `REJECT` for most deployments — it allows critical calls to preempt background work under pressure rather than blanket-rejecting new work. Set `max_size` to `worker_count × 10` as a starting point: this gives each worker a 10-item backlog while preventing unbounded growth. Monitor `utilization` on a 10-second interval; alert when it exceeds 0.80 for more than 60 seconds — sustained high utilization indicates worker count needs scaling or tool call latency has regressed. Set `deadline` on tool calls to `time.time() + session_timeout` so abandoned sessions' pending calls are auto-discarded rather than processed after the user is gone.
