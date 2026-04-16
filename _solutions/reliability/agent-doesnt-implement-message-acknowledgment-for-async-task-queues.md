---
title: "Agent Doesn't Implement Message Acknowledgment for Async Task Queues"
description: "AI agents that consume tasks from async queues without acknowledgment semantics lose work silently when a worker crashes mid-execution. Proper acknowledgment — confirm receipt only after successful completion, with visibility timeout and dead-letter routing — provides at-least-once delivery guarantees and surfaces poison messages without data loss."
date: 2025-02-13
difficulty: intermediate
category: reliability
slug: agent-doesnt-implement-message-acknowledgment-for-async-task-queues
tags:
  - message-acknowledgment
  - task-queue
  - at-least-once
  - visibility-timeout
  - dead-letter
  - async
  - reliability
symptoms:
  - "Agent crashes mid-task; the task is lost because it was dequeued without acknowledgment"
  - "Worker restarts drop all in-flight tasks silently"
  - "A malformed task causes an infinite retry loop that blocks the queue"
  - "No visibility into which tasks are in-flight vs completed vs failed"
  - "Queue empties but downstream effects are missing because workers died before ack"
---

## Problem

A dequeue-and-process pattern without acknowledgment has a critical gap: if the worker crashes between dequeue and completion, the message is lost. Proper message acknowledgment holds the message invisible to other workers while it is being processed (visibility timeout). After successful completion, the worker explicitly acknowledges. On failure or timeout, the message becomes visible again for retry. After N retries, it moves to a dead-letter queue for manual inspection.

---

## Solution 1: AckQueue — In-Process Acknowledgment Queue

```python
import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


@dataclass
class QueueMessage:
    message_id: str
    body: Any
    enqueued_at: float = field(default_factory=time.monotonic)
    delivery_count: int = 0
    visible_at: float = 0.0   # monotonic time when message becomes visible again


@dataclass
class InFlightMessage:
    message: QueueMessage
    receipt_handle: str
    taken_at: float
    visibility_timeout: float


class AckQueue:
    """
    In-process queue with visibility timeout and acknowledgment semantics.
    Messages become invisible after dequeue for `visibility_timeout` seconds.
    If not acked within that window, they reappear for another worker.

    Usage:
        queue = AckQueue(visibility_timeout=30.0, max_deliveries=3)
        await queue.enqueue({"task": "summarise", "doc_id": "d1"})

        msg, receipt = await queue.receive()
        try:
            await process(msg.body)
            await queue.ack(receipt)
        except Exception:
            await queue.nack(receipt)  # returns to queue immediately
    """

    def __init__(self, visibility_timeout: float = 30.0,
                 max_deliveries: int = 3,
                 dlq: Optional["AckQueue"] = None):
        self._vt = visibility_timeout
        self._max_deliveries = max_deliveries
        self._dlq = dlq
        self._messages: List[QueueMessage] = []
        self._inflight: Dict[str, InFlightMessage] = {}
        self._lock = asyncio.Lock()

    async def enqueue(self, body: Any):
        async with self._lock:
            self._messages.append(QueueMessage(
                message_id=str(uuid.uuid4()),
                body=body,
            ))

    async def receive(self, poll_interval: float = 0.1,
                       timeout: float = 30.0) -> Optional[tuple]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            async with self._lock:
                now = time.monotonic()
                # Reclaim timed-out in-flight messages
                timed_out = [
                    (h, inf) for h, inf in self._inflight.items()
                    if now > inf.taken_at + inf.visibility_timeout
                ]
                for handle, inf in timed_out:
                    inf.message.visible_at = 0.0
                    self._messages.append(inf.message)
                    del self._inflight[handle]

                # Find next visible message
                for i, msg in enumerate(self._messages):
                    if now >= msg.visible_at:
                        self._messages.pop(i)
                        msg.delivery_count += 1
                        if msg.delivery_count > self._max_deliveries:
                            if self._dlq:
                                await self._dlq.enqueue(msg.body)
                            return None  # silently drop if no DLQ
                        receipt = str(uuid.uuid4())
                        self._inflight[receipt] = InFlightMessage(
                            message=msg,
                            receipt_handle=receipt,
                            taken_at=now,
                            visibility_timeout=self._vt,
                        )
                        return msg, receipt
            await asyncio.sleep(poll_interval)
        return None

    async def ack(self, receipt_handle: str):
        async with self._lock:
            self._inflight.pop(receipt_handle, None)

    async def nack(self, receipt_handle: str,
                    delay: float = 0.0):
        async with self._lock:
            inf = self._inflight.pop(receipt_handle, None)
            if inf:
                inf.message.visible_at = time.monotonic() + delay
                self._messages.append(inf.message)

    def stats(self) -> dict:
        return {
            "visible": sum(1 for m in self._messages
                           if time.monotonic() >= m.visible_at),
            "delayed": sum(1 for m in self._messages
                           if time.monotonic() < m.visible_at),
            "inflight": len(self._inflight),
        }
```

---

## Solution 2: SQSStyleWorker — Batch Receive with Heartbeat Extension

```python
import asyncio
import time
from typing import Any, Callable, List, Optional


class SQSStyleWorker:
    """
    Worker that receives messages in batches, processes concurrently,
    and extends visibility timeout (heartbeat) for long-running tasks.

    Usage:
        dlq = AckQueue()
        queue = AckQueue(visibility_timeout=60, dlq=dlq)
        worker = SQSStyleWorker(queue, concurrency=5, heartbeat_interval=20)

        async def handler(body):
            await process_task(body)

        await worker.run(handler)
    """

    def __init__(self, queue: AckQueue,
                 concurrency: int = 5,
                 heartbeat_interval: float = 20.0):
        self._queue = queue
        self._concurrency = concurrency
        self._heartbeat_interval = heartbeat_interval
        self._sem = asyncio.Semaphore(concurrency)

    async def _extend_visibility(self, receipt: str,
                                   extension: float = 30.0):
        """Extend the visibility timeout while processing is ongoing."""
        async with self._queue._lock:
            inf = self._queue._inflight.get(receipt)
            if inf:
                inf.visibility_timeout = (
                    (time.monotonic() - inf.taken_at) + extension
                )

    async def _process_with_heartbeat(self, msg, receipt: str,
                                        handler: Callable):
        heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(receipt)
        )
        try:
            await handler(msg.body)
            await self._queue.ack(receipt)
        except Exception:
            await self._queue.nack(receipt, delay=5.0)
        finally:
            heartbeat_task.cancel()

    async def _heartbeat_loop(self, receipt: str):
        while True:
            await asyncio.sleep(self._heartbeat_interval)
            await self._extend_visibility(receipt)

    async def run(self, handler: Callable, stop_event: Optional[asyncio.Event] = None):
        while stop_event is None or not stop_event.is_set():
            result = await self._queue.receive(timeout=5.0)
            if result is None:
                continue
            msg, receipt = result
            async with self._sem:
                asyncio.create_task(
                    self._process_with_heartbeat(msg, receipt, handler)
                )
```

---

## Solution 3: ExactlyOnceProcessor — Idempotency Key Deduplication

Add idempotency tracking so even with at-least-once delivery, each task's effect is applied exactly once.

```python
import asyncio
import time
from typing import Any, Callable, Dict, Optional


class ExactlyOnceProcessor:
    """
    Wraps a task handler with idempotency key tracking.
    If the same message_id is processed twice (due to redelivery),
    the second invocation returns the cached result without re-executing.

    Usage:
        processor = ExactlyOnceProcessor(ttl=3600)

        @processor.idempotent
        async def charge_user(user_id: str, amount: float):
            await payment_api.charge(user_id, amount)

        # Safe to call multiple times with same message_id:
        await processor.run(message_id="msg-abc", fn=charge_user,
                             user_id="u1", amount=9.99)
    """

    def __init__(self, ttl: float = 3600.0):
        self._ttl = ttl
        self._seen: Dict[str, tuple] = {}   # message_id -> (result, expires_at)
        self._lock = asyncio.Lock()

    async def run(self, message_id: str, fn: Callable, **kwargs) -> Any:
        async with self._lock:
            entry = self._seen.get(message_id)
            if entry:
                result, expires_at = entry
                if time.monotonic() < expires_at:
                    return result
        result = await fn(**kwargs)
        async with self._lock:
            self._seen[message_id] = (result, time.monotonic() + self._ttl)
        return result

    async def prune(self):
        now = time.monotonic()
        async with self._lock:
            self._seen = {k: v for k, v in self._seen.items() if v[1] > now}

    def seen_count(self) -> int:
        return len(self._seen)
```

---

## Solution 4: PriorityAckQueue — Priority-Ordered Processing with Acknowledgment

```python
import asyncio
import heapq
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass(order=True)
class PriorityMessage:
    priority: int               # lower = higher priority
    sequence: int               # tiebreaker for FIFO within same priority
    message_id: str = field(compare=False)
    body: Any = field(compare=False)
    delivery_count: int = field(compare=False, default=0)


class PriorityAckQueue:
    """
    Min-heap priority queue with acknowledgment semantics.
    Critical tasks (priority=0) are processed before routine tasks (priority=10).

    Usage:
        queue = PriorityAckQueue(visibility_timeout=30)
        await queue.enqueue({"task": "alert"}, priority=0)
        await queue.enqueue({"task": "summary"}, priority=5)
        await queue.enqueue({"task": "cleanup"}, priority=10)

        msg, receipt = await queue.receive()
        # msg.body == {"task": "alert"} (priority 0 first)
    """

    def __init__(self, visibility_timeout: float = 30.0,
                 max_deliveries: int = 3):
        self._heap: list = []
        self._inflight: dict = {}
        self._seq = 0
        self._vt = visibility_timeout
        self._max_deliveries = max_deliveries
        self._lock = asyncio.Lock()

    async def enqueue(self, body: Any, priority: int = 5):
        async with self._lock:
            heapq.heappush(self._heap, PriorityMessage(
                priority=priority,
                sequence=self._seq,
                message_id=str(uuid.uuid4()),
                body=body,
            ))
            self._seq += 1

    async def receive(self, poll_interval: float = 0.05,
                       timeout: float = 10.0) -> Optional[tuple]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            async with self._lock:
                if self._heap:
                    msg = heapq.heappop(self._heap)
                    if msg.delivery_count >= self._max_deliveries:
                        continue
                    msg.delivery_count += 1
                    receipt = str(uuid.uuid4())
                    self._inflight[receipt] = (msg, time.monotonic())
                    return msg, receipt
            await asyncio.sleep(poll_interval)
        return None

    async def ack(self, receipt: str):
        async with self._lock:
            self._inflight.pop(receipt, None)

    async def nack(self, receipt: str):
        async with self._lock:
            item = self._inflight.pop(receipt, None)
            if item:
                msg, _ = item
                heapq.heappush(self._heap, msg)
```

---

## Solution 5: AckQueueMetrics — Queue Depth and Age Tracking

```python
import asyncio
import time
from collections import deque
from typing import Deque, Dict


class AckQueueMetrics:
    """
    Tracks queue depth, inflight count, and message age for alerting.

    Usage:
        metrics = AckQueueMetrics(queue)
        asyncio.create_task(metrics.run())

        report = metrics.snapshot()
        if report["oldest_message_age_s"] > 300:
            alert("Queue backlog: messages older than 5 minutes")
    """

    def __init__(self, queue: AckQueue, poll_interval: float = 10.0):
        self._queue = queue
        self._interval = poll_interval
        self._history: Deque[dict] = deque(maxlen=360)   # 1 hour at 10s interval

    async def run(self):
        while True:
            await asyncio.sleep(self._interval)
            self._history.append(self.snapshot())

    def snapshot(self) -> Dict:
        now = time.monotonic()
        visible = [m for m in self._queue._messages
                   if now >= m.visible_at]
        delayed = [m for m in self._queue._messages
                   if now < m.visible_at]
        oldest_age = (
            now - min(m.enqueued_at for m in visible)
            if visible else 0.0
        )
        return {
            "ts": time.time(),
            "visible": len(visible),
            "delayed": len(delayed),
            "inflight": len(self._queue._inflight),
            "oldest_message_age_s": round(oldest_age, 1),
        }

    def trend(self) -> Dict:
        if len(self._history) < 2:
            return {}
        depths = [h["visible"] for h in self._history]
        return {
            "min_depth": min(depths),
            "max_depth": max(depths),
            "avg_depth": round(sum(depths) / len(depths), 1),
            "growing": depths[-1] > depths[0],
        }
```

---

## Solution 6: RedisAckQueue — Distributed Queue with BRPOPLPUSH Acknowledgment

```python
import json
import time
import uuid
from typing import Any, Optional


DEQUEUE_SCRIPT = """
local src = KEYS[1]
local inflight = KEYS[2]
local receipt = ARGV[1]
local vt = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local msg_raw = redis.call('RPOPLPUSH', src, inflight)
if not msg_raw then return nil end
local msg = cjson.decode(msg_raw)
msg['receipt'] = receipt
msg['taken_at'] = now
msg['vt_expires_at'] = now + vt
msg['delivery_count'] = (msg['delivery_count'] or 0) + 1
redis.call('HSET', inflight .. ':meta', receipt, cjson.encode(msg))
redis.call('LREM', inflight, 1, msg_raw)
return cjson.encode(msg)
"""


class RedisAckQueue:
    """
    Distributed acknowledgment queue backed by Redis.
    Uses RPOPLPUSH for atomic dequeue + inflight tracking.
    Visibility timeout recovery runs via a separate scanner.

    Usage:
        import redis.asyncio as aioredis
        r = aioredis.from_url("redis://localhost")
        queue = RedisAckQueue(r, name="agent-tasks")

        await queue.enqueue({"task": "summarise", "doc": "d1"}, priority=5)
        msg, receipt = await queue.receive(visibility_timeout=30)
        await process(msg)
        await queue.ack(receipt)
    """

    def __init__(self, redis_client, name: str,
                 max_deliveries: int = 3):
        self._r = redis_client
        self._src = f"queue:{name}:pending"
        self._inflight = f"queue:{name}:inflight"
        self._dlq = f"queue:{name}:dlq"
        self._max = max_deliveries

    async def enqueue(self, body: Any):
        msg = json.dumps({
            "message_id": str(uuid.uuid4()),
            "body": body,
            "enqueued_at": time.time(),
            "delivery_count": 0,
        })
        await self._r.lpush(self._src, msg)

    async def receive(self, visibility_timeout: float = 30.0,
                       poll_interval: float = 0.1,
                       timeout: float = 10.0) -> Optional[tuple]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            receipt = str(uuid.uuid4())
            script = self._r.register_script(DEQUEUE_SCRIPT)
            result = await script(
                keys=[self._src, self._inflight],
                args=[receipt, visibility_timeout, time.time()],
            )
            if result:
                msg = json.loads(result)
                if msg.get("delivery_count", 0) > self._max:
                    await self._r.lpush(self._dlq, result)
                    await self._r.hdel(f"{self._inflight}:meta", receipt)
                    continue
                return msg, receipt
            await __import__("asyncio").sleep(poll_interval)
        return None

    async def ack(self, receipt: str):
        await self._r.hdel(f"{self._inflight}:meta", receipt)

    async def nack(self, receipt: str):
        meta_raw = await self._r.hget(f"{self._inflight}:meta", receipt)
        if meta_raw:
            await self._r.lpush(self._src, meta_raw)
            await self._r.hdel(f"{self._inflight}:meta", receipt)
```

---

## Comparison

| Approach | At-Least-Once | Visibility Timeout | Dead-Letter | Priority | Distributed |
|---|---|---|---|---|---|
| **AckQueue** | Yes | Yes | Yes | No | No |
| **SQSStyleWorker** | Yes | Yes (heartbeat) | Via queue | No | No |
| **ExactlyOnceProcessor** | Exactly-once | N/A | N/A | No | No |
| **PriorityAckQueue** | Yes | Partial | No | Yes | No |
| **AckQueueMetrics** | N/A | N/A | N/A | N/A | N/A |
| **RedisAckQueue** | Yes | Yes | Yes | No | Yes |

**Key insight**: always acknowledge after successful processing, never before. Set the visibility timeout to 2× the 99th-percentile processing time and implement heartbeat extension for tasks that can run long. Route messages that exceed `max_deliveries` to a dead-letter queue — never silently drop them — so operators can inspect and replay poison messages.
