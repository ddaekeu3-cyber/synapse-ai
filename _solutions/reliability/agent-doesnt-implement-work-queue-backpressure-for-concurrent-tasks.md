---
title: "Agent Doesn't Implement Work Queue Backpressure for Concurrent Tasks"
description: "Agents that accept work items without signalling backpressure allow producers to outrun consumers: the queue grows unbounded, memory usage climbs, and eventually the process crashes or all tasks fail together. Implement backpressure by coupling queue depth to producer rate — when the queue is filling, slow down producers with flow-control signals, reject with a 503-equivalent, or apply token-bucket throttling before enqueue."
date: 2026-04-16
difficulty: intermediate
category: reliability
slug: agent-doesnt-implement-work-queue-backpressure-for-concurrent-tasks
tags: [backpressure, work-queue, flow-control, overload-protection, concurrency, producer-consumer]
symptoms:
  - "Memory usage grows linearly during traffic spikes — queue depth has no upper bound"
  - "All enqueued tasks fail simultaneously when the worker pool is overwhelmed"
  - "No signal to callers that the system is overloaded — they keep submitting at full rate"
  - "Queue depth metric spikes to millions during load tests, causing OOM"
  - "Worker pool starvation: tasks time out in queue before a worker picks them up"
---

## Why This Happens

asyncio queues and thread-pool executors accept work items without feedback to the submitter. Producers submit at their natural rate; consumers drain at their natural rate. When production > consumption, the queue grows without bound. Backpressure inverts this: the queue signals its fullness to producers, who either slow down, retry later, or get a rejection they can handle gracefully. The mechanism varies by context — `asyncio.Queue(maxsize=N)` blocks the producer, a semaphore limits concurrent submissions, or a token bucket meters the submission rate.

## Solution 1: Bounded Work Item

```python
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional


class WorkItemStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    REJECTED = "rejected"
    EXPIRED = "expired"


@dataclass
class WorkItem:
    item_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    payload: Any = None
    priority: int = 0
    enqueued_at: float = field(default_factory=time.time)
    deadline_seconds: float = 60.0
    status: WorkItemStatus = WorkItemStatus.PENDING
    result: Any = None
    error: Optional[str] = None
    worker_id: Optional[str] = None

    def is_expired(self) -> bool:
        return time.time() - self.enqueued_at > self.deadline_seconds

    def queue_wait_ms(self) -> float:
        return (time.time() - self.enqueued_at) * 1000
```

## Solution 2: Backpressure-Aware Queue

```python
import asyncio
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, List, Optional


class BackpressureSignal(str, Enum):
    OK = "ok"
    SLOW_DOWN = "slow_down"     # queue at 70% capacity
    NEAR_FULL = "near_full"     # queue at 90% capacity
    FULL = "full"               # queue at 100% — reject


@dataclass
class EnqueueResult:
    accepted: bool
    signal: BackpressureSignal
    queue_depth: int
    queue_capacity: int
    item_id: str = ""
    rejection_reason: str = ""


class BackpressureQueue:
    """
    asyncio queue with backpressure signalling.
    Returns BackpressureSignal on every enqueue attempt so producers
    can react — slow down, back off, or accept rejection.
    """

    def __init__(
        self,
        capacity: int = 1000,
        slow_down_pct: float = 0.70,
        near_full_pct: float = 0.90,
    ):
        self._capacity = capacity
        self._slow_down_at = int(capacity * slow_down_pct)
        self._near_full_at = int(capacity * near_full_pct)
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=capacity)
        self._rejected = 0
        self._accepted = 0
        self._total_wait_ms = 0.0

    async def enqueue(self, item: WorkItem) -> EnqueueResult:
        depth = self._queue.qsize()

        # Determine backpressure signal
        if depth >= self._capacity:
            self._rejected += 1
            item.status = WorkItemStatus.REJECTED
            return EnqueueResult(
                accepted=False,
                signal=BackpressureSignal.FULL,
                queue_depth=depth,
                queue_capacity=self._capacity,
                item_id=item.item_id,
                rejection_reason="queue_full",
            )

        signal = BackpressureSignal.OK
        if depth >= self._near_full_at:
            signal = BackpressureSignal.NEAR_FULL
        elif depth >= self._slow_down_at:
            signal = BackpressureSignal.SLOW_DOWN

        try:
            self._queue.put_nowait(item)
            self._accepted += 1
            return EnqueueResult(
                accepted=True,
                signal=signal,
                queue_depth=depth + 1,
                queue_capacity=self._capacity,
                item_id=item.item_id,
            )
        except asyncio.QueueFull:
            self._rejected += 1
            return EnqueueResult(
                accepted=False,
                signal=BackpressureSignal.FULL,
                queue_depth=depth,
                queue_capacity=self._capacity,
                item_id=item.item_id,
                rejection_reason="queue_full_race",
            )

    async def dequeue(self, timeout: float = 1.0) -> Optional[WorkItem]:
        try:
            item = await asyncio.wait_for(self._queue.get(), timeout=timeout)
            self._total_wait_ms += item.queue_wait_ms()
            return item
        except asyncio.TimeoutError:
            return None

    def depth(self) -> int:
        return self._queue.qsize()

    def utilization(self) -> float:
        return round(self._queue.qsize() / self._capacity, 4)

    def stats(self) -> dict:
        return {
            "depth": self._queue.qsize(),
            "capacity": self._capacity,
            "utilization": self.utilization(),
            "accepted": self._accepted,
            "rejected": self._rejected,
            "rejection_rate": round(
                self._rejected / max(self._accepted + self._rejected, 1), 4
            ),
            "avg_wait_ms": round(
                self._total_wait_ms / max(self._accepted, 1), 1
            ),
        }
```

## Solution 3: Adaptive Producer with Backpressure Handling

```python
import asyncio
import time
from typing import Callable, List


class AdaptiveProducer:
    """
    Submits work items to a BackpressureQueue and adapts submission rate
    based on received backpressure signals.
    SLOW_DOWN → adds a configurable delay between submissions.
    NEAR_FULL → delays longer and logs a warning.
    FULL      → backs off exponentially and retries.
    """

    def __init__(
        self,
        queue: BackpressureQueue,
        base_delay_seconds: float = 0.0,
        slow_down_delay_seconds: float = 0.1,
        near_full_delay_seconds: float = 0.5,
        max_backoff_seconds: float = 30.0,
    ):
        self._queue = queue
        self._base_delay = base_delay_seconds
        self._slow_delay = slow_down_delay_seconds
        self._near_full_delay = near_full_delay_seconds
        self._max_backoff = max_backoff_seconds
        self._backoff = 1.0
        self._total_submitted = 0
        self._total_retried = 0

    async def submit(self, item: WorkItem, max_retries: int = 5) -> EnqueueResult:
        for attempt in range(max_retries + 1):
            result = await self._queue.enqueue(item)

            if result.signal == BackpressureSignal.SLOW_DOWN:
                await asyncio.sleep(self._slow_delay)
            elif result.signal == BackpressureSignal.NEAR_FULL:
                await asyncio.sleep(self._near_full_delay)

            if result.accepted:
                self._total_submitted += 1
                self._backoff = 1.0   # reset backoff on success
                return result

            if result.signal == BackpressureSignal.FULL:
                if attempt < max_retries:
                    delay = min(self._backoff, self._max_backoff)
                    self._backoff = min(self._backoff * 2, self._max_backoff)
                    self._total_retried += 1
                    await asyncio.sleep(delay)
                else:
                    return result

        return result

    def stats(self) -> dict:
        return {
            "total_submitted": self._total_submitted,
            "total_retried": self._total_retried,
            "current_backoff": self._backoff,
        }
```

## Solution 4: Work Queue Worker Pool

```python
import asyncio
import time
from typing import Callable, List, Optional


class WorkQueueWorkerPool:
    """
    Pool of async workers that drain a BackpressureQueue.
    Workers skip expired items and update item status throughout.
    """

    def __init__(
        self,
        queue: BackpressureQueue,
        worker_count: int = 4,
        process_fn: Optional[Callable] = None,
    ):
        self._queue = queue
        self._worker_count = worker_count
        self._process_fn = process_fn or self._noop
        self._workers: List[asyncio.Task] = []
        self._completed = 0
        self._failed = 0
        self._expired = 0

    async def start(self) -> None:
        self._workers = [
            asyncio.create_task(self._worker_loop(f"worker-{i}"))
            for i in range(self._worker_count)
        ]

    async def stop(self) -> None:
        for w in self._workers:
            w.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)

    async def _worker_loop(self, worker_id: str) -> None:
        while True:
            item = await self._queue.dequeue(timeout=1.0)
            if item is None:
                continue
            if item.is_expired():
                item.status = WorkItemStatus.EXPIRED
                self._expired += 1
                self._queue._queue.task_done()
                continue

            item.status = WorkItemStatus.RUNNING
            item.worker_id = worker_id
            try:
                item.result = await self._process_fn(item)
                item.status = WorkItemStatus.COMPLETED
                self._completed += 1
            except Exception as exc:
                item.status = WorkItemStatus.FAILED
                item.error = str(exc)
                self._failed += 1
            finally:
                self._queue._queue.task_done()

    @staticmethod
    async def _noop(item: WorkItem) -> None:
        await asyncio.sleep(0.01)

    def stats(self) -> dict:
        return {
            "workers": self._worker_count,
            "completed": self._completed,
            "failed": self._failed,
            "expired": self._expired,
        }
```

## Solution 5: Queue Depth Trend Detector

```python
import time
from collections import deque
from typing import Deque, Tuple


class QueueDepthTrendDetector:
    """
    Tracks queue depth over time to detect sustained overload trends.
    If depth is consistently above the warning threshold for more than
    trend_window_seconds, emits a sustained-overload signal.
    """

    def __init__(
        self,
        queue: BackpressureQueue,
        warning_utilization: float = 0.70,
        trend_window_seconds: float = 60.0,
    ):
        self._queue = queue
        self._warning = warning_utilization
        self._window = trend_window_seconds
        self._samples: Deque[Tuple[float, float]] = deque()   # (ts, utilization)

    def sample(self) -> None:
        self._samples.append((time.time(), self._queue.utilization()))
        cutoff = time.time() - self._window
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()

    def is_sustained_overload(self) -> bool:
        if len(self._samples) < 5:
            return False
        return all(u >= self._warning for _, u in self._samples)

    def trend_summary(self) -> dict:
        if not self._samples:
            return {"samples": 0}
        utils = [u for _, u in self._samples]
        return {
            "samples": len(utils),
            "avg_utilization": round(sum(utils) / len(utils), 4),
            "max_utilization": round(max(utils), 4),
            "sustained_overload": self.is_sustained_overload(),
        }
```

## Solution 6: Backpressure Dashboard

```python
import time


class BackpressureDashboard:
    """
    Combines queue stats, producer stats, worker stats, and trend detection
    into a single operational view. Emits alerts for overload conditions.
    """

    def __init__(
        self,
        queue: BackpressureQueue,
        producer: AdaptiveProducer,
        workers: WorkQueueWorkerPool,
        trend_detector: QueueDepthTrendDetector,
    ):
        self._queue = queue
        self._producer = producer
        self._workers = workers
        self._trend = trend_detector

    def render(self) -> dict:
        self._trend.sample()
        q = self._queue.stats()
        p = self._producer.stats()
        w = self._workers.stats()
        t = self._trend.trend_summary()

        alerts = []
        if q["rejection_rate"] > 0.05:
            alerts.append({
                "type": "high_rejection_rate",
                "value": q["rejection_rate"],
                "recommendation": "add workers or reduce submission rate",
            })
        if t.get("sustained_overload"):
            alerts.append({
                "type": "sustained_overload",
                "avg_utilization": t["avg_utilization"],
                "recommendation": "scale worker pool or apply upstream throttling",
            })
        if q["avg_wait_ms"] > 5000:
            alerts.append({
                "type": "high_queue_wait",
                "avg_wait_ms": q["avg_wait_ms"],
                "recommendation": "increase worker concurrency or reduce item deadlines",
            })

        return {
            "generated_at": time.time(),
            "queue": q,
            "producer": p,
            "workers": w,
            "trend": t,
            "alerts": alerts,
            "healthy": len(alerts) == 0,
        }
```

## Comparison

| Approach | Queue Depth Cap | Backpressure Signal | Producer Adaptation | Trend Detection |
|---|---|---|---|---|
| BackpressureQueue | Yes (maxsize) | Yes (OK/SLOW/FULL) | No | No |
| AdaptiveProducer | No | Reads signal | Yes (delay + backoff) | No |
| WorkQueueWorkerPool | No | No | No | No |
| QueueDepthTrendDetector | No | No | No | Yes (sliding window) |
| BackpressureDashboard | No | No | No | Via detector |

**Best for production**: Set queue capacity at 10–20× worker count — enough to absorb brief bursts without unbounded growth. Return the `BackpressureSignal` to API callers as HTTP headers (`X-Backpressure: slow_down`) so upstream services can self-throttle. Use `QueueDepthTrendDetector` to distinguish transient spikes (handle gracefully) from sustained overload (trigger auto-scaling). Tune `slow_down_pct=0.70` and `near_full_pct=0.90` — the gap between them is the backpressure ramp zone where producers slow progressively before hitting hard rejection.
