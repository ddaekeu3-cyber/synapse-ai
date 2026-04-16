---
title: "Agent Doesn't Implement Batch Embedding Requests to Reduce API Round Trips"
description: "Agents that embed documents one at a time pay per-request overhead — HTTP connection setup, TLS handshake, authentication headers, and rate-limit token consumption — for every single text. Implement batch embedding that accumulates texts into configurable-size batches, dispatches them in a single API call, and fans results back to individual callers."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-batch-embedding-requests-to-reduce-api-round-trips
tags: [batch-embedding, api-efficiency, round-trip-reduction, embedding-throughput, vectorization, request-batching]
symptoms:
  - "Embedding 1000 documents takes 1000 API calls instead of 10 batched calls"
  - "Rate limit errors during bulk embedding because per-request overhead consumes quota faster than batch"
  - "Embedding latency per document is 150ms but p50 throughput is only 6 docs/second"
  - "No batching in place — each embed() call immediately fires an HTTP request"
  - "Cost per embedding is dominated by API overhead rather than model compute"
---

## Why This Happens

Embedding APIs charge per token but enforce rate limits per request. When texts are embedded one at a time, each call incurs the full HTTP overhead: connection pooling, request serialization, server-side dispatch, response deserialization. Most embedding APIs accept batches of 100–2048 texts per call, amortizing the per-request overhead across all texts in the batch. Without batching, the overhead-to-compute ratio is 1:1. With batching, it becomes 1:N where N is the batch size, reducing both latency per document and API call count by the same factor.

## Solution 1: Embedding Batch Accumulator

```python
import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple


@dataclass
class EmbeddingRequest:
    request_id: str
    text: str
    future: asyncio.Future
    enqueued_at: float = field(default_factory=time.time)


class EmbeddingBatchAccumulator:
    """
    Accumulates individual embedding requests and dispatches them in
    batches when either the batch size limit or a flush deadline is reached.
    """

    def __init__(
        self,
        max_batch_size: int = 100,
        flush_interval_seconds: float = 0.05,  # 50ms max wait
    ):
        self._max_batch = max_batch_size
        self._flush_interval = flush_interval_seconds
        self._queue: List[EmbeddingRequest] = []
        self._lock = asyncio.Lock()
        self._batch_count = 0
        self._total_texts = 0

    async def add(self, request_id: str, text: str) -> asyncio.Future:
        loop = asyncio.get_event_loop()
        future: asyncio.Future = loop.create_future()
        req = EmbeddingRequest(request_id=request_id, text=text, future=future)

        async with self._lock:
            self._queue.append(req)
            should_flush = len(self._queue) >= self._max_batch

        if should_flush:
            await self._flush()

        return future

    async def _flush(self) -> Optional[List[EmbeddingRequest]]:
        async with self._lock:
            if not self._queue:
                return None
            batch = self._queue[: self._max_batch]
            self._queue = self._queue[self._max_batch :]
            self._batch_count += 1
            self._total_texts += len(batch)
        return batch

    async def drain(self) -> Optional[List[EmbeddingRequest]]:
        """Flush all pending requests regardless of batch size."""
        async with self._lock:
            if not self._queue:
                return None
            batch = self._queue[:]
            self._queue = []
            self._batch_count += 1
            self._total_texts += len(batch)
        return batch

    def stats(self) -> dict:
        return {
            "batch_count": self._batch_count,
            "total_texts_batched": self._total_texts,
            "avg_batch_size": round(
                self._total_texts / max(self._batch_count, 1), 2
            ),
        }
```

## Solution 2: Batch Embedding Dispatcher

```python
import asyncio
from typing import Any, Callable, List


class BatchEmbeddingDispatcher:
    """
    Takes a batch of EmbeddingRequests, calls the embedding API once,
    and distributes results back to individual futures.
    """

    def __init__(
        self,
        embed_batch_fn: Callable[[List[str]], Any],
        # embed_batch_fn(texts) -> List[embedding_vector]
    ):
        self._embed_fn = embed_batch_fn
        self._api_calls = 0
        self._failed_batches = 0

    async def dispatch(self, batch: List[EmbeddingRequest]) -> None:
        if not batch:
            return
        texts = [req.text for req in batch]
        self._api_calls += 1
        try:
            embeddings = await self._embed_fn(texts)
            for req, embedding in zip(batch, embeddings):
                if not req.future.done():
                    req.future.set_result(embedding)
        except Exception as exc:
            self._failed_batches += 1
            for req in batch:
                if not req.future.done():
                    req.future.set_exception(exc)

    def stats(self) -> dict:
        return {
            "api_calls": self._api_calls,
            "failed_batches": self._failed_batches,
        }
```

## Solution 3: Periodic Flush Worker

```python
import asyncio
from typing import Optional


class PeriodicFlushWorker:
    """
    Runs a background loop that flushes the accumulator on a timer,
    ensuring requests are dispatched even if the batch size threshold
    is never reached (low-traffic periods).
    """

    def __init__(
        self,
        accumulator: EmbeddingBatchAccumulator,
        dispatcher: BatchEmbeddingDispatcher,
        flush_interval_seconds: float = 0.05,
    ):
        self._accumulator = accumulator
        self._dispatcher = dispatcher
        self._interval = flush_interval_seconds
        self._task: Optional[asyncio.Task] = None
        self._running = False

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
        # Final flush
        batch = await self._accumulator.drain()
        if batch:
            await self._dispatcher.dispatch(batch)

    async def _loop(self) -> None:
        while self._running:
            await asyncio.sleep(self._interval)
            batch = await self._accumulator.drain()
            if batch:
                await self._dispatcher.dispatch(batch)
```

## Solution 4: Batching Embedding Client

```python
import uuid
from typing import Any, List


class BatchingEmbeddingClient:
    """
    Drop-in replacement for a single-text embedding client.
    Internally batches concurrent calls through the accumulator.
    """

    def __init__(
        self,
        accumulator: EmbeddingBatchAccumulator,
        dispatcher: BatchEmbeddingDispatcher,
    ):
        self._accumulator = accumulator
        self._dispatcher = dispatcher

    async def embed(self, text: str) -> Any:
        """Embed a single text. Internally batched with concurrent calls."""
        request_id = str(uuid.uuid4())[:8]
        future = await self._accumulator.add(request_id, text)

        # Check if batch is full and dispatch immediately
        batch = await self._accumulator._flush()
        if batch:
            await self._dispatcher.dispatch(batch)

        return await future

    async def embed_many(self, texts: List[str]) -> List[Any]:
        """Embed multiple texts concurrently, fully utilizing batch dispatch."""
        futures = await asyncio.gather(
            *[self._accumulator.add(str(i), text) for i, text in enumerate(texts)]
        )
        # Flush all pending
        while True:
            batch = await self._accumulator.drain()
            if not batch:
                break
            await self._dispatcher.dispatch(batch)
        return await asyncio.gather(*futures)
```

## Solution 5: Batch Throughput Tracker

```python
import time
from collections import deque
from threading import Lock
from typing import Deque, Optional, Tuple


class BatchThroughputTracker:
    """
    Tracks texts-per-second and API-calls-per-second to quantify
    the throughput improvement from batching vs. single-call baseline.
    """

    def __init__(self, window_seconds: float = 60.0):
        self._window = window_seconds
        self._batch_log: Deque[Tuple[float, int]] = deque()  # (ts, batch_size)
        self._lock = Lock()

    def record_batch(self, batch_size: int) -> None:
        with self._lock:
            now = time.time()
            self._batch_log.append((now, batch_size))
            cutoff = now - self._window
            while self._batch_log and self._batch_log[0][0] < cutoff:
                self._batch_log.popleft()

    def throughput(self) -> dict:
        with self._lock:
            if not self._batch_log:
                return {"texts_per_second": 0.0, "batches_per_second": 0.0}
            now = time.time()
            cutoff = now - self._window
            recent = [(ts, sz) for ts, sz in self._batch_log if ts >= cutoff]

        if not recent:
            return {"texts_per_second": 0.0, "batches_per_second": 0.0}

        elapsed = now - recent[0][0] if len(recent) > 1 else 1.0
        total_texts = sum(sz for _, sz in recent)
        return {
            "texts_per_second": round(total_texts / max(elapsed, 0.001), 2),
            "batches_per_second": round(len(recent) / max(elapsed, 0.001), 2),
            "avg_batch_size": round(total_texts / len(recent), 2),
            "window_seconds": self._window,
        }
```

## Solution 6: Batch Embedding Dashboard

```python
import time


class BatchEmbeddingDashboard:
    """
    Combines accumulator stats, dispatcher stats, and throughput
    into an operational embedding pipeline health report.
    """

    def __init__(
        self,
        accumulator: EmbeddingBatchAccumulator,
        dispatcher: BatchEmbeddingDispatcher,
        throughput_tracker: BatchThroughputTracker,
    ):
        self._accumulator = accumulator
        self._dispatcher = dispatcher
        self._throughput = throughput_tracker

    def render(self) -> dict:
        acc_stats = self._accumulator.stats()
        disp_stats = self._dispatcher.stats()
        throughput = self._throughput.throughput()
        single_call_baseline_tps = 1000.0 / 150.0  # assume 150ms per single call

        return {
            "generated_at": time.time(),
            "accumulator": acc_stats,
            "dispatcher": disp_stats,
            "throughput": throughput,
            "efficiency": {
                "actual_tps": throughput["texts_per_second"],
                "single_call_baseline_tps": round(single_call_baseline_tps, 2),
                "speedup_factor": round(
                    throughput["texts_per_second"] / max(single_call_baseline_tps, 0.001), 2
                ),
            },
        }
```

## Comparison

| Approach | Request Accumulation | Batch Dispatch | Periodic Flush | Throughput Tracking | Dashboard |
|---|---|---|---|---|---|
| EmbeddingBatchAccumulator | Yes (size-triggered) | No | No | No | No |
| BatchEmbeddingDispatcher | No | Yes (single API call) | No | No | No |
| PeriodicFlushWorker | No | Via dispatcher | Yes (timer) | No | No |
| BatchingEmbeddingClient | Via accumulator | Via dispatcher | No | No | No |
| BatchThroughputTracker | No | No | No | Yes (tps/batch size) | No |
| BatchEmbeddingDashboard | No | No | No | No | Yes |

**Best for production**: Set `max_batch_size` to the embedding API's maximum (OpenAI supports 2048 inputs per call; Cohere supports 96). Use `flush_interval_seconds=0.05` (50ms) so that low-traffic periods don't leave individual requests waiting indefinitely. Run `PeriodicFlushWorker` as a long-lived background task for the process lifetime — it ensures drain happens even when batch size thresholds are never reached. Monitor `avg_batch_size` via the dashboard: consistently below 10 means concurrency is too low to benefit from batching, and you should look at parallelizing the upstream document pipeline rather than tuning batch parameters.
