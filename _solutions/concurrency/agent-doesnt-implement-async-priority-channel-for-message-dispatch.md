---
title: "Agent doesn't implement async priority channel for message dispatch"
description: "All messages are dispatched in FIFO order regardless of urgency. A low-priority background task blocks a time-sensitive user request for minutes, degrading real-time responsiveness."
difficulty: intermediate
category: concurrency
tags: [asyncio, priority-queue, message-dispatch, scheduling, backpressure]
---

## Problem

When an agent serves multiple concurrent callers — background batch jobs, interactive users, and internal heartbeats — they all share the same FIFO queue. A slow 50-page summarization job that arrived first blocks a one-word user reply for the entire duration of processing. Without priority channels, the agent optimizes for throughput at the expense of latency for high-value requests.

```python
# BAD: single FIFO queue — all messages equally delayed
queue = asyncio.Queue()
await queue.put({"msg": "batch_job", "user": "cron"})     # low priority
await queue.put({"msg": "hi there", "user": "alice"})     # high priority, but waits behind batch
```

## Solution 1: `asyncio.PriorityQueue` with integer priority levels

Python's built-in `PriorityQueue` orders items by their smallest value first. Assign lower numbers to higher-priority messages.

```python
import asyncio
import time
from dataclasses import dataclass, field
from typing import Any


PRIORITY_REALTIME = 0    # user-facing interactive requests
PRIORITY_HIGH = 10       # SLA-bound webhook deliveries
PRIORITY_NORMAL = 20     # standard API calls
PRIORITY_LOW = 50        # background jobs, batch processing
PRIORITY_IDLE = 100      # health checks, telemetry flushes


@dataclass(order=True)
class PrioritizedMessage:
    priority: int
    timestamp: float = field(default_factory=time.monotonic, compare=True)
    payload: Any = field(default=None, compare=False)

    def age_ms(self) -> float:
        return (time.monotonic() - self.timestamp) * 1000


class PriorityDispatcher:
    def __init__(self, maxsize: int = 1000):
        self._queue: asyncio.PriorityQueue[PrioritizedMessage] = asyncio.PriorityQueue(maxsize)
        self._dispatched = 0
        self._dropped = 0

    async def put(self, payload: Any, priority: int = PRIORITY_NORMAL):
        msg = PrioritizedMessage(priority=priority, payload=payload)
        try:
            self._queue.put_nowait(msg)
        except asyncio.QueueFull:
            self._dropped += 1
            raise RuntimeError(f"Dispatch queue full ({self._queue.maxsize}); message dropped")

    async def get(self) -> PrioritizedMessage:
        msg = await self._queue.get()
        self._dispatched += 1
        return msg

    @property
    def qsize(self) -> int:
        return self._queue.qsize()

    def stats(self) -> dict:
        return {
            "queued": self.qsize,
            "dispatched": self._dispatched,
            "dropped": self._dropped,
        }


# ── Worker ────────────────────────────────────────────────────────────
async def worker(dispatcher: PriorityDispatcher):
    while True:
        msg = await dispatcher.get()
        print(
            f"[P{msg.priority}] age={msg.age_ms():.0f}ms "
            f"payload={msg.payload}"
        )
        # Simulate processing time proportional to priority level
        await asyncio.sleep(0.01)
        dispatcher._queue.task_done()


async def main():
    dispatcher = PriorityDispatcher(maxsize=100)

    # Enqueue messages out of priority order
    await dispatcher.put("batch_job_1", PRIORITY_LOW)
    await dispatcher.put("batch_job_2", PRIORITY_LOW)
    await dispatcher.put("user_message", PRIORITY_REALTIME)
    await dispatcher.put("webhook_delivery", PRIORITY_HIGH)
    await dispatcher.put("healthcheck", PRIORITY_IDLE)
    await dispatcher.put("api_call", PRIORITY_NORMAL)

    task = asyncio.create_task(worker(dispatcher))
    await asyncio.sleep(0.2)
    task.cancel()
    print(dispatcher.stats())


asyncio.run(main())
```

## Solution 2: Multi-lane channel — separate queues per priority tier

Instead of a single priority queue, maintain one `asyncio.Queue` per tier. A round-robin drain loop pulls from the highest non-empty tier first.

```python
import asyncio
from typing import Any

TIERS = ["realtime", "high", "normal", "low", "idle"]


class MultiLaneChannel:
    """
    N separate queues, one per priority tier.
    get() always returns from the highest non-empty tier.
    No starvation guard here — see Solution 3 for that.
    """

    def __init__(self, maxsize_per_lane: int = 200):
        self._lanes: dict[str, asyncio.Queue] = {
            tier: asyncio.Queue(maxsize=maxsize_per_lane)
            for tier in TIERS
        }
        self._has_work = asyncio.Event()

    async def put(self, payload: Any, tier: str = "normal"):
        if tier not in self._lanes:
            raise ValueError(f"Unknown tier: {tier}")
        await self._lanes[tier].put(payload)
        self._has_work.set()

    async def get(self) -> tuple[str, Any]:
        """Returns (tier, payload). Blocks until any lane has a message."""
        while True:
            for tier in TIERS:
                try:
                    payload = self._lanes[tier].get_nowait()
                    # Clear the event if all lanes are now empty
                    if all(q.empty() for q in self._lanes.values()):
                        self._has_work.clear()
                    return tier, payload
                except asyncio.QueueEmpty:
                    continue
            # All lanes empty — wait for something to arrive
            self._has_work.clear()
            await self._has_work.wait()

    def depths(self) -> dict[str, int]:
        return {tier: q.qsize() for tier, q in self._lanes.items()}


# ── Usage ────────────────────────────────────────────────────────────
async def demo():
    ch = MultiLaneChannel()

    # Producer: mix of priorities
    await ch.put("batch-1", "low")
    await ch.put("user-hello", "realtime")
    await ch.put("api-call", "normal")
    await ch.put("webhook", "high")

    # Consumer: always gets highest-priority first
    for _ in range(4):
        tier, payload = await ch.get()
        print(f"[{tier}] {payload}")


asyncio.run(demo())
```

## Solution 3: Priority channel with starvation prevention via aging

Low-priority messages can starve indefinitely if high-priority messages arrive continuously. Age-based priority boosting ensures every message eventually gets processed.

```python
import asyncio
import time
import heapq
from dataclasses import dataclass, field
from typing import Any


@dataclass(order=True)
class AgedMessage:
    effective_priority: float    # lower = higher priority; decreases over time
    enqueue_time: float = field(default_factory=time.monotonic, compare=False)
    base_priority: int = field(default=20, compare=False)
    payload: Any = field(default=None, compare=False)


class AgingPriorityChannel:
    """
    Priority channel where effective_priority decreases (improves) over time
    at a configurable rate, preventing indefinite starvation.
    """

    def __init__(self, aging_rate: float = 1.0, check_interval: float = 0.05):
        self._heap: list[AgedMessage] = []
        self._lock = asyncio.Lock()
        self._not_empty = asyncio.Event()
        self._aging_rate = aging_rate        # priority units per second
        self._check_interval = check_interval

    async def put(self, payload: Any, priority: int = 20):
        msg = AgedMessage(
            effective_priority=float(priority),
            base_priority=priority,
            payload=payload,
        )
        async with self._lock:
            heapq.heappush(self._heap, msg)
            self._not_empty.set()

    async def get(self) -> Any:
        while True:
            await self._not_empty.wait()
            async with self._lock:
                if not self._heap:
                    self._not_empty.clear()
                    continue
                self._apply_aging()
                msg = heapq.heappop(self._heap)
                if not self._heap:
                    self._not_empty.clear()
                return msg.payload

    def _apply_aging(self):
        """Re-score all messages: reduce priority by aging_rate × age_seconds."""
        now = time.monotonic()
        rescored = []
        for msg in self._heap:
            age = now - msg.enqueue_time
            msg.effective_priority = msg.base_priority - (age * self._aging_rate)
            rescored.append(msg)
        self._heap = rescored
        heapq.heapify(self._heap)


# ── Demo ──────────────────────────────────────────────────────────────
async def demo():
    ch = AgingPriorityChannel(aging_rate=5.0)

    await ch.put("low-priority-task", priority=50)
    await asyncio.sleep(0.3)  # let it age
    await ch.put("high-priority-task", priority=10)

    # Low-priority task has aged: effective = 50 - (0.3 * 5) = 48.5
    # High-priority task: effective = 10
    # High gets dispatched first, but low won't starve indefinitely

    for _ in range(2):
        payload = await ch.get()
        print(payload)


asyncio.run(demo())
```

## Solution 4: Priority channel with backpressure and flow control

When the high-priority lane is full, push back on low-priority producers. Let high-priority producers always succeed; slow or reject low-priority traffic.

```python
import asyncio
from typing import Any


class BackpressurePriorityChannel:
    """
    Two lanes: fast (always accepts up to capacity) and slow (blocks when fast is full).
    High-priority senders are never blocked; low-priority senders experience backpressure.
    """

    FAST_CAPACITY = 50
    SLOW_CAPACITY = 500

    def __init__(self):
        self._fast: asyncio.Queue = asyncio.Queue(maxsize=self.FAST_CAPACITY)
        self._slow: asyncio.Queue = asyncio.Queue(maxsize=self.SLOW_CAPACITY)

    async def put_fast(self, payload: Any, timeout: float = 1.0):
        """High-priority put — never blocks (raises if truly full)."""
        try:
            self._fast.put_nowait(payload)
        except asyncio.QueueFull:
            raise RuntimeError("Fast lane at capacity — critical backlog")

    async def put_slow(self, payload: Any, timeout: float = 30.0):
        """Low-priority put — blocks up to `timeout` seconds."""
        try:
            await asyncio.wait_for(self._slow.put(payload), timeout=timeout)
        except asyncio.TimeoutError:
            raise RuntimeError("Slow lane producer timed out — system overloaded")

    async def get(self) -> tuple[str, Any]:
        """Always drain fast lane first; fall back to slow when empty."""
        try:
            return "fast", self._fast.get_nowait()
        except asyncio.QueueEmpty:
            pass

        # Wait on both queues simultaneously
        fast_get = asyncio.ensure_future(self._fast.get())
        slow_get = asyncio.ensure_future(self._slow.get())

        done, pending = await asyncio.wait(
            [fast_get, slow_get], return_when=asyncio.FIRST_COMPLETED
        )
        for t in pending:
            t.cancel()

        result = done.pop().result()
        lane = "fast" if result in [fast_get] else "slow"
        return lane, result

    def depths(self) -> dict:
        return {"fast": self._fast.qsize(), "slow": self._slow.qsize()}
```

## Solution 5: Typed priority channel with per-message deadline enforcement

Attach a deadline to each message. When the dispatcher dequeues a message past its deadline, it discards it and emits a metric instead of processing stale work.

```python
import asyncio
import time
import heapq
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass(order=True)
class DeadlinedMessage:
    priority: int
    deadline: float          # monotonic timestamp; float("inf") = no deadline
    payload: Any = field(default=None, compare=False)
    enqueued_at: float = field(default_factory=time.monotonic, compare=False)

    def is_expired(self) -> bool:
        return time.monotonic() > self.deadline

    def remaining_ms(self) -> float:
        return max(0.0, (self.deadline - time.monotonic()) * 1000)


class DeadlineAwarePriorityChannel:
    def __init__(self):
        self._heap: list[DeadlinedMessage] = []
        self._lock = asyncio.Lock()
        self._event = asyncio.Event()
        self.expired_count = 0
        self.dispatched_count = 0

    async def put(
        self,
        payload: Any,
        priority: int = 20,
        deadline_ms: Optional[float] = None,
    ):
        deadline = (
            time.monotonic() + deadline_ms / 1000
            if deadline_ms is not None
            else float("inf")
        )
        msg = DeadlinedMessage(priority=priority, deadline=deadline, payload=payload)
        async with self._lock:
            heapq.heappush(self._heap, msg)
            self._event.set()

    async def get(self) -> Any:
        while True:
            await self._event.wait()
            async with self._lock:
                while self._heap:
                    msg = heapq.heappop(self._heap)
                    if msg.is_expired():
                        self.expired_count += 1
                        print(f"[EXPIRED] Dropped message (priority={msg.priority})")
                        continue
                    self.dispatched_count += 1
                    if not self._heap:
                        self._event.clear()
                    return msg.payload
                self._event.clear()


# ── Demo ──────────────────────────────────────────────────────────────
async def demo():
    ch = DeadlineAwarePriorityChannel()

    await ch.put("urgent-task", priority=0, deadline_ms=500)
    await ch.put("normal-task", priority=20, deadline_ms=5000)
    await ch.put("slow-task", priority=50, deadline_ms=100)  # expires quickly

    await asyncio.sleep(0.2)  # let slow-task expire

    for _ in range(2):  # only 2 will be dispatched (slow expired)
        payload = await ch.get()
        print(f"Dispatched: {payload}")

    print(f"Expired: {ch.expired_count}, Dispatched: {ch.dispatched_count}")


asyncio.run(demo())
```

## Solution 6: Distributed priority channel backed by Redis sorted set

For multi-process agents, use Redis `ZADD`/`ZPOPMIN` to implement a cross-process priority queue. Score = priority × 1e12 + timestamp for stable secondary sort.

```python
import asyncio
import time
import json
from typing import Any

# Requires: pip install redis[asyncio]
try:
    import redis.asyncio as aioredis
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False


class RedisPriorityChannel:
    """
    Cross-process priority channel backed by a Redis sorted set.
    Score = priority * 1e12 + unix_timestamp_us
    Lower score = dequeued first (lower priority number = higher urgency).
    """

    def __init__(self, redis_url: str = "redis://localhost:6379", key: str = "agent:dispatch"):
        self._key = key
        self._redis_url = redis_url
        self._client: "aioredis.Redis | None" = None

    async def connect(self):
        if REDIS_AVAILABLE:
            self._client = aioredis.from_url(self._redis_url, decode_responses=True)

    async def close(self):
        if self._client:
            await self._client.aclose()

    def _score(self, priority: int) -> float:
        ts_us = int(time.time() * 1_000_000)
        return priority * 1e12 + ts_us

    async def put(self, payload: Any, priority: int = 20):
        if not self._client:
            raise RuntimeError("Not connected to Redis")
        score = self._score(priority)
        data = json.dumps({"payload": payload, "priority": priority})
        await self._client.zadd(self._key, {data: score})

    async def get(self, block_seconds: float = 0.1) -> Any | None:
        """Pop the highest-priority (lowest score) item."""
        if not self._client:
            raise RuntimeError("Not connected to Redis")
        # ZPOPMIN: atomically pop the lowest-score member
        items = await self._client.zpopmin(self._key, count=1)
        if not items:
            await asyncio.sleep(block_seconds)
            return None
        data_str, score = items[0]
        record = json.loads(data_str)
        return record["payload"]

    async def depth(self) -> int:
        if not self._client:
            return 0
        return await self._client.zcard(self._key)


# ── Usage (requires running Redis) ───────────────────────────────────
async def demo():
    if not REDIS_AVAILABLE:
        print("redis[asyncio] not installed — skipping Redis demo")
        return

    ch = RedisPriorityChannel()
    await ch.connect()

    await ch.put("batch_job", priority=50)
    await ch.put("user_message", priority=0)
    await ch.put("webhook", priority=10)

    for _ in range(3):
        payload = await ch.get()
        print(f"Dispatched: {payload}")

    await ch.close()


asyncio.run(demo())
```

## Comparison

| Approach | Cross-process | Starvation-free | Deadline support | Backpressure | Complexity |
|---|---|---|---|---|---|
| `asyncio.PriorityQueue` | No | No | No | No | Low |
| Multi-lane channel | No | No | No | No | Low |
| Aging priority channel | No | Yes | No | No | Medium |
| Backpressure priority channel | No | Partial | No | Yes | Medium |
| Deadline-aware channel | No | Partial | Yes | No | Medium |
| Redis sorted set channel | Yes | No | Partial | No | High |

**Recommendation**: Use **`asyncio.PriorityQueue`** (Solution 1) for simple single-process priority needs. Add **aging** (Solution 3) if low-priority tasks must eventually complete. Use **deadline-aware dispatch** (Solution 5) for real-time agent responses where stale answers are worthless. Use the **Redis channel** (Solution 6) when multiple agent processes share a dispatch queue.
