---
title: "Agent Doesn't Implement Work Queue Backpressure to Prevent Memory Exhaustion"
description: "Agents with unbounded work queues accept incoming tasks faster than they process them, causing the queue to grow until the process runs out of memory and is OOM-killed. Implement backpressure mechanisms that bound queue depth, signal producers to slow down, and shed excess load gracefully before memory pressure reaches critical levels."
date: 2026-04-16
difficulty: advanced
category: reliability
slug: agent-doesnt-implement-work-queue-backpressure-to-prevent-memory-exhaustion
tags: [backpressure, work-queue, memory-exhaustion, oom-prevention, load-shedding, bounded-queue]
symptoms:
  - "Agent process OOM-killed during traffic spikes after queue grows to millions of items"
  - "Memory usage climbs linearly with incoming request rate — no upper bound"
  - "No signal sent to callers that the agent is overwhelmed — requests enqueue silently"
  - "Queue depth metric is absent — operators cannot see saturation building"
  - "Garbage collector pressure spikes before OOM as enqueued objects accumulate"
---

## Why This Happens

Unbounded queues are the default in most async frameworks: `asyncio.Queue()` and Python's `queue.Queue()` grow without limit unless `maxsize` is specified. When a producer is faster than the consumer — a common condition under load spikes — items accumulate in the queue. Each item consumes memory. Without a depth limit and backpressure signal, the only bound is available RAM. Backpressure requires two mechanisms: a bounded queue that blocks or rejects producers when full (push-back), and a depth monitor that surfaces queue saturation before the bound is hit (early warning).

## Solution 1: Bounded Work Queue

```python
import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class BackpressureSignal(str, Enum):
    ACCEPTED = "accepted"
    QUEUED = "queued"
    BACKPRESSURE = "backpressure"   # queue near full, slow down
    REJECTED = "rejected"           # queue full, item dropped


@dataclass
class WorkItem:
    item_id: str
    payload: Any
    enqueued_at: float = field(default_factory=time.time)
    priority: int = 5


class BoundedWorkQueue:
    """
    Asyncio queue with configurable max depth.
    Returns a BackpressureSignal to producers so they can self-throttle
    before the queue reaches capacity.
    """

    def __init__(
        self,
        maxsize: int = 1000,
        backpressure_threshold: float = 0.80,
        reject_threshold: float = 0.95,
    ):
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self._maxsize = maxsize
        self._backpressure_ratio = backpressure_threshold
        self._reject_ratio = reject_threshold
        self._enqueued_total = 0
        self._rejected_total = 0
        self._backpressure_count = 0

    def depth(self) -> int:
        return self._queue.qsize()

    def utilization(self) -> float:
        return round(self._queue.qsize() / max(self._maxsize, 1), 4)

    async def enqueue(self, item: WorkItem) -> BackpressureSignal:
        util = self.utilization()
        if util >= self._reject_ratio:
            self._rejected_total += 1
            return BackpressureSignal.REJECTED

        if util >= self._backpressure_ratio:
            self._backpressure_count += 1
            try:
                await asyncio.wait_for(self._queue.put(item), timeout=0.1)
                self._enqueued_total += 1
                return BackpressureSignal.BACKPRESSURE
            except asyncio.TimeoutError:
                self._rejected_total += 1
                return BackpressureSignal.REJECTED

        await self._queue.put(item)
        self._enqueued_total += 1
        return BackpressureSignal.QUEUED

    async def dequeue(self) -> WorkItem:
        return await self._queue.get()

    def task_done(self) -> None:
        self._queue.task_done()

    def stats(self) -> dict:
        return {
            "depth": self.depth(),
            "maxsize": self._maxsize,
            "utilization": self.utilization(),
            "enqueued_total": self._enqueued_total,
            "rejected_total": self._rejected_total,
            "backpressure_count": self._backpressure_count,
        }
```

## Solution 2: Queue Depth Monitor

```python
import asyncio
import time
from typing import List, Optional


class QueueDepthMonitor:
    """
    Periodically samples queue depth and fires callbacks when
    depth crosses warning and critical thresholds.
    """

    def __init__(
        self,
        queue: BoundedWorkQueue,
        warn_utilization: float = 0.70,
        critical_utilization: float = 0.90,
        sample_interval_seconds: float = 5.0,
    ):
        self._queue = queue
        self._warn = warn_utilization
        self._critical = critical_utilization
        self._interval = sample_interval_seconds
        self._samples: List[dict] = []
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            util = self._queue.utilization()
            sample = {
                "ts": time.time(),
                "depth": self._queue.depth(),
                "utilization": util,
                "level": (
                    "critical" if util >= self._critical
                    else "warn" if util >= self._warn
                    else "ok"
                ),
            }
            self._samples.append(sample)
            if len(self._samples) > 1000:
                self._samples.pop(0)

    def current_level(self) -> str:
        util = self._queue.utilization()
        if util >= self._critical:
            return "critical"
        if util >= self._warn:
            return "warn"
        return "ok"

    def recent_samples(self, count: int = 12) -> List[dict]:
        return self._samples[-count:]
```

## Solution 3: Adaptive Producer Throttle

```python
import asyncio
import time
from typing import Optional


class AdaptiveProducerThrottle:
    """
    Adds adaptive delay to producers based on backpressure signals.
    When BACKPRESSURE is returned, the producer waits before submitting
    the next item — giving the consumer time to drain the queue.
    """

    def __init__(
        self,
        base_delay_ms: float = 10.0,
        max_delay_ms: float = 1000.0,
        backoff_factor: float = 2.0,
        recovery_factor: float = 0.5,
    ):
        self._base = base_delay_ms / 1000.0
        self._max = max_delay_ms / 1000.0
        self._backoff = backoff_factor
        self._recovery = recovery_factor
        self._current_delay: float = 0.0
        self._throttle_events = 0

    async def apply(self, signal: BackpressureSignal) -> None:
        if signal == BackpressureSignal.BACKPRESSURE:
            self._current_delay = min(
                max(self._current_delay, self._base) * self._backoff,
                self._max,
            )
            self._throttle_events += 1
            await asyncio.sleep(self._current_delay)
        elif signal == BackpressureSignal.ACCEPTED or signal == BackpressureSignal.QUEUED:
            self._current_delay = max(0.0, self._current_delay * self._recovery)

    def current_delay_ms(self) -> float:
        return round(self._current_delay * 1000, 2)

    def throttle_events(self) -> int:
        return self._throttle_events
```

## Solution 4: Load Shedder

```python
import time
from typing import Any, Callable, Optional


class WorkQueueLoadShedder:
    """
    Sits in front of the work queue and makes accept/reject decisions
    based on current queue depth, request priority, and time budget.
    """

    def __init__(
        self,
        queue: BoundedWorkQueue,
        min_priority_under_pressure: int = 3,
    ):
        self._queue = queue
        self._min_priority = min_priority_under_pressure
        self._shed_count = 0
        self._accepted_count = 0

    async def submit(self, item: WorkItem) -> dict:
        util = self._queue.utilization()

        # Under pressure, reject low-priority items immediately
        if util >= 0.80 and item.priority < self._min_priority:
            self._shed_count += 1
            return {
                "status": "shed",
                "reason": "low_priority_under_pressure",
                "queue_utilization": util,
            }

        signal = await self._queue.enqueue(item)

        if signal == BackpressureSignal.REJECTED:
            self._shed_count += 1
            return {"status": "rejected", "queue_utilization": util}

        self._accepted_count += 1
        return {
            "status": "accepted",
            "signal": signal.value,
            "queue_depth": self._queue.depth(),
        }

    def shed_rate(self) -> float:
        total = self._shed_count + self._accepted_count
        return round(self._shed_count / max(total, 1), 4)
```

## Solution 5: Memory Pressure Guard

```python
import os
import time
from typing import Optional


class MemoryPressureGuard:
    """
    Reads process RSS memory and triggers emergency queue pause
    when memory usage approaches a configured ceiling.
    Prevents OOM by stopping enqueuing before the kernel kills the process.
    """

    def __init__(
        self,
        warn_mb: float = 512.0,
        critical_mb: float = 768.0,
    ):
        self._warn_bytes = warn_mb * 1024 * 1024
        self._critical_bytes = critical_mb * 1024 * 1024

    def current_rss_bytes(self) -> Optional[int]:
        try:
            with open(f"/proc/{os.getpid()}/status") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        return int(line.split()[1]) * 1024
        except (OSError, ValueError):
            pass
        return None

    def pressure_level(self) -> str:
        rss = self.current_rss_bytes()
        if rss is None:
            return "unknown"
        if rss >= self._critical_bytes:
            return "critical"
        if rss >= self._warn_bytes:
            return "warn"
        return "ok"

    def should_pause_enqueue(self) -> bool:
        return self.pressure_level() == "critical"

    def stats(self) -> dict:
        rss = self.current_rss_bytes()
        return {
            "rss_mb": round(rss / 1024 / 1024, 1) if rss else None,
            "pressure_level": self.pressure_level(),
            "warn_mb": self._warn_bytes / 1024 / 1024,
            "critical_mb": self._critical_bytes / 1024 / 1024,
        }
```

## Solution 6: Backpressure Dashboard

```python
import time


class WorkQueueBackpressureDashboard:
    """
    Combines queue stats, depth monitor samples, load shedder stats,
    and memory pressure into a single operational health report.
    """

    def __init__(
        self,
        queue: BoundedWorkQueue,
        monitor: QueueDepthMonitor,
        shedder: WorkQueueLoadShedder,
        memory_guard: MemoryPressureGuard,
    ):
        self._queue = queue
        self._monitor = monitor
        self._shedder = shedder
        self._memory = memory_guard

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "queue": self._queue.stats(),
            "monitor_level": self._monitor.current_level(),
            "recent_depth_samples": self._monitor.recent_samples(6),
            "load_shedder": {
                "shed_count": self._shedder._shed_count,
                "accepted_count": self._shedder._accepted_count,
                "shed_rate": self._shedder.shed_rate(),
            },
            "memory": self._memory.stats(),
        }
```

## Comparison

| Approach | Bounded Queue | Backpressure Signal | Producer Throttle | Load Shedding | Memory Guard |
|---|---|---|---|---|---|
| BoundedWorkQueue | Yes (maxsize) | Yes (enum signal) | No | No | No |
| QueueDepthMonitor | Via queue | No | No | No | No |
| AdaptiveProducerThrottle | No | No | Yes (adaptive delay) | No | No |
| WorkQueueLoadShedder | Via queue | Via queue | No | Yes (priority-based) | No |
| MemoryPressureGuard | No | No | No | No | Yes (/proc/RSS) |
| WorkQueueBackpressureDashboard | No | No | No | No | Yes |

**Best for production**: Always set `maxsize` explicitly on every `asyncio.Queue` — the default unbounded behavior is a latent OOM risk. Set `backpressure_threshold=0.80` and `reject_threshold=0.95` to give producers a 15% window to self-throttle before hard rejection begins. Use `AdaptiveProducerThrottle` with exponential backoff so that a burst of backpressure signals slows producers progressively rather than causing them to hammer the queue repeatedly at the same rate. Monitor `shed_rate` via the dashboard: a sustained shed rate above 5% means the consumer pool is undersized and workers should be scaled up rather than relying on load shedding as the steady-state control.
