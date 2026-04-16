---
title: "Agent doesn't implement async batch collector with timeout"
description: "Individual items are processed one at a time, even when the downstream API supports batch requests. Without a batch collector, the agent makes N API calls instead of one, exhausting rate limits and inflating cost."
difficulty: intermediate
category: concurrency
tags: [batching, async, rate-limiting, throughput, debounce, timeout]
---

## Problem

Many downstream APIs support batch requests (embedding N texts at once, inserting N database rows, sending N emails). When the agent processes items one at a time, it makes N round trips instead of one — multiplying latency, hitting per-request rate limits sooner, and paying per-call fees N times over.

A batch collector accumulates individual items and flushes them together when either a size threshold is reached or a maximum wait time elapses. This reduces API calls from N to ceil(N/batch_size) while bounding the delay any single item waits.

```python
# BAD: one API call per item — N calls for N items
async def process_all(items):
    for item in items:
        await embed_api(item)  # 100 items = 100 API calls
```

## Solution 1: Simple debounce batch collector with size and time limits

Accumulate items in a buffer. Flush when the buffer reaches `max_size` or `max_wait_seconds` elapses, whichever comes first.

```python
import asyncio
from typing import Any, Callable, Awaitable
from dataclasses import dataclass, field


@dataclass
class BatchResult:
    item: Any
    future: "asyncio.Future[Any]"


class BatchCollector:
    """
    Accumulates items and flushes them in batches.
    Each caller awaits its own Future; when the batch is processed,
    all Futures in that batch are resolved together.
    """

    def __init__(
        self,
        flush_fn: Callable[[list[Any]], Awaitable[list[Any]]],
        max_size: int = 32,
        max_wait_seconds: float = 0.05,
    ):
        self.flush_fn = flush_fn
        self.max_size = max_size
        self.max_wait = max_wait_seconds
        self._pending: list[BatchResult] = []
        self._lock = asyncio.Lock()
        self._flush_task: asyncio.Task | None = None

    async def submit(self, item: Any) -> Any:
        """Submit one item; returns the result when the batch is flushed."""
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()

        async with self._lock:
            self._pending.append(BatchResult(item=item, future=future))

            if len(self._pending) >= self.max_size:
                # Flush immediately — batch is full
                await self._flush_locked()
            elif self._flush_task is None or self._flush_task.done():
                # Start the timeout-based flush
                self._flush_task = asyncio.create_task(self._timeout_flush())

        return await future

    async def _timeout_flush(self):
        await asyncio.sleep(self.max_wait)
        async with self._lock:
            if self._pending:
                await self._flush_locked()

    async def _flush_locked(self):
        """Must be called with self._lock held."""
        batch = self._pending[:]
        self._pending.clear()
        if self._flush_task and not self._flush_task.done():
            self._flush_task.cancel()
            self._flush_task = None

        # Run the batch flush outside the lock to avoid blocking submitters
        asyncio.create_task(self._dispatch(batch))

    async def _dispatch(self, batch: list[BatchResult]):
        items = [b.item for b in batch]
        try:
            results = await self.flush_fn(items)
            for b, result in zip(batch, results):
                if not b.future.done():
                    b.future.set_result(result)
        except Exception as e:
            for b in batch:
                if not b.future.done():
                    b.future.set_exception(e)


# ── Example: batch embedding API ─────────────────────────────────────
call_count = 0


async def batch_embed(texts: list[str]) -> list[list[float]]:
    global call_count
    call_count += 1
    await asyncio.sleep(0.02)  # simulated API latency
    print(f"[API] batch_embed called with {len(texts)} items (call #{call_count})")
    return [[0.1] * 128 for _ in texts]


async def main():
    collector = BatchCollector(flush_fn=batch_embed, max_size=8, max_wait_seconds=0.05)

    # 20 concurrent callers — should result in 3 batches instead of 20 calls
    texts = [f"text-{i}" for i in range(20)]
    results = await asyncio.gather(*[collector.submit(t) for t in texts])
    print(f"Got {len(results)} embeddings in {call_count} API calls (vs 20 without batching)")


asyncio.run(main())
```

## Solution 2: Priority-aware batch collector

Items with different priorities accumulate in the same collector but high-priority items trigger immediate flushing when they arrive, while low-priority items wait for the timeout.

```python
import asyncio
import time
from typing import Any, Callable, Awaitable


PRIORITY_HIGH = 0
PRIORITY_NORMAL = 10
PRIORITY_LOW = 50


class PriorityBatchCollector:
    def __init__(
        self,
        flush_fn: Callable[[list[Any]], Awaitable[list[Any]]],
        max_size: int = 32,
        max_wait_normal: float = 0.1,
        max_wait_low: float = 0.5,
        high_priority_flush_size: int = 1,  # flush immediately on any high-priority item
    ):
        self.flush_fn = flush_fn
        self.max_size = max_size
        self.max_wait_normal = max_wait_normal
        self.max_wait_low = max_wait_low
        self.high_flush_size = high_priority_flush_size

        self._queue: list[tuple[int, Any, "asyncio.Future"]] = []
        self._lock = asyncio.Lock()
        self._timer_task: asyncio.Task | None = None

    async def submit(self, item: Any, priority: int = PRIORITY_NORMAL) -> Any:
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()

        async with self._lock:
            self._queue.append((priority, item, future))

            has_high = any(p == PRIORITY_HIGH for p, _, _ in self._queue)
            should_flush_now = (
                len(self._queue) >= self.max_size or
                (has_high and len(self._queue) >= self.high_flush_size)
            )

            if should_flush_now:
                await self._flush_locked()
            elif self._timer_task is None or self._timer_task.done():
                wait = self.max_wait_normal if priority <= PRIORITY_NORMAL else self.max_wait_low
                self._timer_task = asyncio.create_task(self._timer_flush(wait))

        return await future

    async def _timer_flush(self, wait: float):
        await asyncio.sleep(wait)
        async with self._lock:
            if self._queue:
                await self._flush_locked()

    async def _flush_locked(self):
        batch = self._queue[:]
        self._queue.clear()
        if self._timer_task and not self._timer_task.done():
            self._timer_task.cancel()
        asyncio.create_task(self._dispatch(batch))

    async def _dispatch(self, batch: list[tuple[int, Any, "asyncio.Future"]]):
        items = [item for _, item, _ in batch]
        try:
            results = await self.flush_fn(items)
            for (_, _, fut), result in zip(batch, results):
                if not fut.done():
                    fut.set_result(result)
        except Exception as e:
            for _, _, fut in batch:
                if not fut.done():
                    fut.set_exception(e)


# ── Demo ──────────────────────────────────────────────────────────────
async def mock_api(items: list[str]) -> list[str]:
    await asyncio.sleep(0.01)
    return [f"result:{x}" for x in items]


async def demo():
    col = PriorityBatchCollector(mock_api, max_size=10, max_wait_normal=0.05)
    low = asyncio.create_task(col.submit("low-task", PRIORITY_LOW))
    norm = asyncio.create_task(col.submit("normal-task", PRIORITY_NORMAL))
    high = asyncio.create_task(col.submit("urgent-task", PRIORITY_HIGH))
    results = await asyncio.gather(high, norm, low)
    print(results)


asyncio.run(demo())
```

## Solution 3: Window-based batch collector with backpressure

When the downstream API is slower than the producer, enforce backpressure: block submitters if the inflight count exceeds a threshold, preventing runaway queue growth.

```python
import asyncio
from typing import Any, Callable, Awaitable


class BackpressureBatchCollector:
    """
    Batch collector that limits inflight batches.
    When the max inflight limit is reached, new submissions block until
    an inflight batch completes.
    """

    def __init__(
        self,
        flush_fn: Callable[[list[Any]], Awaitable[list[Any]]],
        max_size: int = 32,
        max_wait: float = 0.1,
        max_inflight: int = 4,
    ):
        self.flush_fn = flush_fn
        self.max_size = max_size
        self.max_wait = max_wait
        self._inflight_sem = asyncio.Semaphore(max_inflight)
        self._pending: list[tuple[Any, "asyncio.Future"]] = []
        self._lock = asyncio.Lock()
        self._flush_task: asyncio.Task | None = None
        self.total_batches = 0

    async def submit(self, item: Any) -> Any:
        # Backpressure: wait if too many batches are inflight
        async with self._inflight_sem:
            pass  # just checking capacity — release immediately

        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()

        async with self._lock:
            self._pending.append((item, future))
            if len(self._pending) >= self.max_size:
                await self._flush_locked()
            elif not self._flush_task or self._flush_task.done():
                self._flush_task = asyncio.create_task(self._timed_flush())

        return await future

    async def _timed_flush(self):
        await asyncio.sleep(self.max_wait)
        async with self._lock:
            if self._pending:
                await self._flush_locked()

    async def _flush_locked(self):
        batch = self._pending[:]
        self._pending.clear()
        if self._flush_task and not self._flush_task.done():
            self._flush_task.cancel()
        asyncio.create_task(self._dispatch(batch))

    async def _dispatch(self, batch: list[tuple[Any, "asyncio.Future"]]):
        async with self._inflight_sem:
            self.total_batches += 1
            items = [item for item, _ in batch]
            try:
                results = await self.flush_fn(items)
                for (_, fut), result in zip(batch, results):
                    if not fut.done():
                        fut.set_result(result)
            except Exception as e:
                for _, fut in batch:
                    if not fut.done():
                        fut.set_exception(e)
```

## Solution 4: Typed batch collector with per-item error isolation

Each item in a batch can succeed or fail independently. Wrap results in a `BatchItemResult` so that one item's API error doesn't fail the entire batch.

```python
import asyncio
from dataclasses import dataclass
from typing import Any, Callable, Awaitable, Generic, TypeVar

T = TypeVar("T")
R = TypeVar("R")


@dataclass
class BatchItemResult(Generic[R]):
    ok: bool
    value: R | None = None
    error: Exception | None = None


class IsolatedBatchCollector(Generic[T, R]):
    def __init__(
        self,
        flush_fn: Callable[[list[T]], Awaitable[list[BatchItemResult[R]]]],
        max_size: int = 32,
        max_wait: float = 0.05,
    ):
        self.flush_fn = flush_fn
        self.max_size = max_size
        self.max_wait = max_wait
        self._pending: list[tuple[T, "asyncio.Future[BatchItemResult[R]]"]] = []
        self._lock = asyncio.Lock()
        self._timer: asyncio.Task | None = None

    async def submit(self, item: T) -> BatchItemResult[R]:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[BatchItemResult[R]] = loop.create_future()

        async with self._lock:
            self._pending.append((item, future))
            if len(self._pending) >= self.max_size:
                await self._flush_locked()
            elif not self._timer or self._timer.done():
                self._timer = asyncio.create_task(self._timed_flush())

        return await future

    async def _timed_flush(self):
        await asyncio.sleep(self.max_wait)
        async with self._lock:
            if self._pending:
                await self._flush_locked()

    async def _flush_locked(self):
        batch = self._pending[:]
        self._pending.clear()
        if self._timer and not self._timer.done():
            self._timer.cancel()
        asyncio.create_task(self._dispatch(batch))

    async def _dispatch(self, batch):
        items = [item for item, _ in batch]
        try:
            results = await self.flush_fn(items)
            if len(results) != len(batch):
                # Pad with errors if API returns wrong count
                results += [BatchItemResult(ok=False, error=ValueError("Missing result"))] * (len(batch) - len(results))
            for (_, fut), result in zip(batch, results):
                if not fut.done():
                    fut.set_result(result)
        except Exception as e:
            for _, fut in batch:
                if not fut.done():
                    fut.set_result(BatchItemResult(ok=False, error=e))


# ── Usage ────────────────────────────────────────────────────────────
async def batch_translate(texts: list[str]) -> list[BatchItemResult[str]]:
    results = []
    for t in texts:
        if "fail" in t:
            results.append(BatchItemResult(ok=False, error=ValueError(f"Cannot translate: {t}")))
        else:
            results.append(BatchItemResult(ok=True, value=f"[translated] {t}"))
    return results


async def main():
    col: IsolatedBatchCollector[str, str] = IsolatedBatchCollector(batch_translate, max_size=5)
    items = ["hello", "world", "fail-this", "async patterns"]
    results = await asyncio.gather(*[col.submit(t) for t in items])
    for item, result in zip(items, results):
        if result.ok:
            print(f"  OK: {result.value}")
        else:
            print(f"  FAIL [{item}]: {result.error}")


asyncio.run(main())
```

## Solution 5: Adaptive batch size based on API response times

Measure the time each batch takes to process. Grow the batch size when the API is fast; shrink it when the API is slow to maintain a target latency per item.

```python
import asyncio
import time
from typing import Any, Callable, Awaitable


class AdaptiveBatchCollector:
    """
    Dynamically adjusts batch size to maintain `target_latency_ms` per item.
    Grows batch size when API is fast; shrinks when it's slow.
    """

    def __init__(
        self,
        flush_fn: Callable[[list[Any]], Awaitable[list[Any]]],
        min_size: int = 1,
        max_size: int = 128,
        initial_size: int = 16,
        target_latency_ms: float = 50.0,
        max_wait: float = 0.1,
    ):
        self.flush_fn = flush_fn
        self.min_size = min_size
        self.max_size = max_size
        self.current_size = initial_size
        self.target_ms = target_latency_ms
        self.max_wait = max_wait
        self._pending: list[tuple[Any, "asyncio.Future"]] = []
        self._lock = asyncio.Lock()
        self._timer: asyncio.Task | None = None
        self._latency_ema: float = target_latency_ms
        self._ema_alpha: float = 0.3

    def _update_size(self, actual_latency_ms: float):
        """Adjust batch size based on measured latency EMA."""
        self._latency_ema = (
            self._ema_alpha * actual_latency_ms
            + (1 - self._ema_alpha) * self._latency_ema
        )
        ratio = self.target_ms / max(self._latency_ema, 1)
        new_size = int(self.current_size * ratio)
        self.current_size = max(self.min_size, min(self.max_size, new_size))

    async def submit(self, item: Any) -> Any:
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()

        async with self._lock:
            self._pending.append((item, future))
            if len(self._pending) >= self.current_size:
                await self._flush_locked()
            elif not self._timer or self._timer.done():
                self._timer = asyncio.create_task(self._timed_flush())

        return await future

    async def _timed_flush(self):
        await asyncio.sleep(self.max_wait)
        async with self._lock:
            if self._pending:
                await self._flush_locked()

    async def _flush_locked(self):
        batch = self._pending[:self.current_size]
        self._pending = self._pending[self.current_size:]
        if self._timer and not self._timer.done():
            self._timer.cancel()
        asyncio.create_task(self._dispatch(batch))
        # If more pending after flush, start next timer
        if self._pending:
            self._timer = asyncio.create_task(self._timed_flush())

    async def _dispatch(self, batch: list[tuple[Any, "asyncio.Future"]]):
        items = [item for item, _ in batch]
        start = time.monotonic()
        try:
            results = await self.flush_fn(items)
            elapsed_ms = (time.monotonic() - start) * 1000
            self._update_size(elapsed_ms)
            print(f"Batch size={len(items)} latency={elapsed_ms:.0f}ms → next={self.current_size}")
            for (_, fut), result in zip(batch, results):
                if not fut.done():
                    fut.set_result(result)
        except Exception as e:
            for _, fut in batch:
                if not fut.done():
                    fut.set_exception(e)
```

## Solution 6: Cross-process batch collector using Redis pub/sub coordination

For multi-process agent deployments, use Redis to coordinate batch collection so multiple worker processes contribute items to a shared batch.

```python
import asyncio
import json
import time
import uuid
from typing import Any, Callable, Awaitable

try:
    import redis.asyncio as aioredis
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False

BATCH_QUEUE_KEY = "agent:batch:embed"
RESULT_CHANNEL_PREFIX = "agent:batch:result:"
BATCH_SIZE = 32
BATCH_TIMEOUT = 0.1  # seconds


class RedisCoordinatedBatcher:
    """
    Cross-process batch coordinator.
    Workers push items to Redis; one elected leader flushes the batch.
    Results are delivered via Redis pub/sub.
    """

    def __init__(
        self,
        redis_url: str,
        flush_fn: Callable[[list[Any]], Awaitable[list[Any]]],
        batch_size: int = BATCH_SIZE,
        timeout: float = BATCH_TIMEOUT,
    ):
        self.redis_url = redis_url
        self.flush_fn = flush_fn
        self.batch_size = batch_size
        self.timeout = timeout
        self._client: "aioredis.Redis | None" = None

    async def connect(self):
        if REDIS_AVAILABLE:
            self._client = aioredis.from_url(self.redis_url, decode_responses=True)

    async def submit(self, item: Any) -> Any:
        if not self._client:
            raise RuntimeError("Not connected")

        item_id = str(uuid.uuid4())
        entry = json.dumps({"id": item_id, "item": item, "ts": time.time()})

        # Push to shared queue
        await self._client.rpush(BATCH_QUEUE_KEY, entry)
        queue_len = await self._client.llen(BATCH_QUEUE_KEY)

        if queue_len >= self.batch_size:
            await self._try_flush()

        # Subscribe to result channel and wait
        pubsub = self._client.pubsub()
        result_channel = f"{RESULT_CHANNEL_PREFIX}{item_id}"
        await pubsub.subscribe(result_channel)

        deadline = time.monotonic() + 5.0
        async for message in pubsub.listen():
            if message["type"] == "message":
                result = json.loads(message["data"])
                await pubsub.unsubscribe(result_channel)
                return result
            if time.monotonic() > deadline:
                raise TimeoutError(f"No result for {item_id}")

    async def _try_flush(self):
        """Atomically pop up to batch_size items and flush them."""
        if not self._client:
            return
        items_raw = await self._client.lmpop(1, BATCH_QUEUE_KEY, count=self.batch_size, direction="LEFT")
        if not items_raw:
            return

        entries = [json.loads(r) for r in items_raw[1]]
        items = [e["item"] for e in entries]
        results = await self.flush_fn(items)

        # Publish results
        for entry, result in zip(entries, results):
            channel = f"{RESULT_CHANNEL_PREFIX}{entry['id']}"
            await self._client.publish(channel, json.dumps(result))
```

## Comparison

| Approach | Cross-process | Backpressure | Priority support | Adaptive size | Error isolation |
|---|---|---|---|---|---|
| Simple debounce collector | No | No | No | No | No |
| Priority-aware collector | No | No | Yes | No | No |
| Backpressure collector | No | Yes | No | No | No |
| Isolated error collector | No | No | No | No | Yes |
| Adaptive size collector | No | No | No | Yes | No |
| Redis coordinated batcher | Yes | No | No | No | No |

**Recommendation**: Use the **simple debounce collector** (Solution 1) for most single-process use cases — it handles 90% of scenarios with minimal complexity. Add **error isolation** (Solution 4) when individual item failures must not abort the entire batch. Use the **Redis coordinated batcher** (Solution 6) for multi-process deployments where batch efficiency needs to span worker boundaries.
