---
title: "Agent Doesn't Implement Batch Embedding Requests"
description: "Agents that call an embedding API once per document send N sequential or parallel HTTP requests for N documents, each with its own connection overhead and rate-limit cost. Implement batch embedding that accumulates documents into optimally sized batches, submits them in a single API call, maps results back to their originating documents, and respects the embedding model's token and item limits per batch."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-batch-embedding-requests
tags: [batch-embedding, embedding-api, throughput, rate-limiting, request-batching, token-efficiency]
symptoms:
  - "Agent sends one embedding API call per document — 500 documents triggers 500 HTTP requests"
  - "Embedding throughput is throttled by API rate limits that apply per-request, not per-token"
  - "No batching logic — documents are embedded as they arrive with no accumulation window"
  - "Batch size is hard-coded to 1 or a fixed small number regardless of the model's actual batch limit"
  - "Results arrive out of order and cannot be matched back to their source documents"
---

## Why This Happens

Most embedding APIs accept a list of texts in a single request and return a list of vectors in the same order. Developers who add embedding calls incrementally often start with a single-document call and never revisit the batching strategy. Single-document calls waste API quota on per-request overhead (authentication, TLS handshake, request routing), consume rate-limit tokens faster than necessary, and produce higher end-to-end latency when N documents must be embedded sequentially. Batching requires an accumulation buffer, a flush trigger (batch full or timeout), and a result mapper that preserves document identity through the round-trip.

## Solution 1: Embedding Request Item

```python
import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Any, List, Optional


@dataclass
class EmbeddingRequestItem:
    text: str
    item_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    metadata: dict = field(default_factory=dict)
    result_future: Optional[asyncio.Future] = field(default=None, repr=False)

    def __post_init__(self):
        if self.result_future is None:
            loop = asyncio.get_event_loop()
            self.result_future = loop.create_future()

    def set_result(self, vector: List[float]) -> None:
        if not self.result_future.done():
            self.result_future.set_result(vector)

    def set_error(self, exc: Exception) -> None:
        if not self.result_future.done():
            self.result_future.set_exception(exc)

    async def wait(self) -> List[float]:
        return await self.result_future
```

## Solution 2: Batch Size Calculator

```python
from dataclasses import dataclass
from typing import List


@dataclass
class EmbeddingModelLimits:
    max_items_per_batch: int = 2048
    max_tokens_per_batch: int = 8192
    max_chars_per_item: int = 8000
    tokens_per_char_estimate: float = 0.25


class BatchSizeCalculator:
    """
    Determines how many items can fit in a single embedding batch
    without exceeding the model's item or token limits.
    """

    def __init__(self, limits: EmbeddingModelLimits):
        self._limits = limits

    def compute_batch(
        self, items: List[EmbeddingRequestItem]
    ) -> tuple:
        """Returns (batch_items, remaining_items)."""
        batch: List[EmbeddingRequestItem] = []
        token_estimate = 0

        for item in items:
            text = item.text[:self._limits.max_chars_per_item]
            item_tokens = int(len(text) * self._limits.tokens_per_char_estimate)

            if len(batch) >= self._limits.max_items_per_batch:
                break
            if batch and token_estimate + item_tokens > self._limits.max_tokens_per_batch:
                break

            batch.append(item)
            token_estimate += item_tokens

        remaining = items[len(batch):]
        return batch, remaining

    def estimated_tokens(self, items: List[EmbeddingRequestItem]) -> int:
        return int(sum(
            len(item.text[:self._limits.max_chars_per_item]) * self._limits.tokens_per_char_estimate
            for item in items
        ))
```

## Solution 3: Batch Accumulation Buffer

```python
import asyncio
import time
from threading import Lock
from typing import Callable, List, Optional


class BatchAccumulationBuffer:
    """
    Accumulates embedding request items and flushes when either
    the batch is full or the flush interval elapses.
    """

    def __init__(
        self,
        flush_fn: Callable[[List[EmbeddingRequestItem]], None],
        max_batch_size: int = 256,
        flush_interval_seconds: float = 0.05,
    ):
        self._flush_fn = flush_fn
        self._max_size = max_batch_size
        self._interval = flush_interval_seconds
        self._buffer: List[EmbeddingRequestItem] = []
        self._lock = asyncio.Lock()
        self._last_flush = time.monotonic()

    async def add(self, item: EmbeddingRequestItem) -> None:
        async with self._lock:
            self._buffer.append(item)
            should_flush = (
                len(self._buffer) >= self._max_size
                or (time.monotonic() - self._last_flush) >= self._interval
            )
            if should_flush:
                batch = self._buffer[:]
                self._buffer.clear()
                self._last_flush = time.monotonic()
            else:
                batch = []

        if batch:
            await asyncio.get_event_loop().run_in_executor(None, self._flush_fn, batch)

    async def flush_remaining(self) -> None:
        async with self._lock:
            batch = self._buffer[:]
            self._buffer.clear()
        if batch:
            await asyncio.get_event_loop().run_in_executor(None, self._flush_fn, batch)

    def pending_count(self) -> int:
        return len(self._buffer)
```

## Solution 4: Batching Embedding Client

```python
import asyncio
import time
from typing import Any, Callable, Dict, List, Optional


class BatchingEmbeddingClient:
    """
    Accepts individual embed() calls and batches them transparently.
    Each caller awaits their future; the client flushes accumulated
    items to the underlying API in optimal batches.
    """

    def __init__(
        self,
        api_fn: Callable[[List[str]], List[List[float]]],  # fn(texts) -> vectors
        limits: EmbeddingModelLimits,
        flush_interval_seconds: float = 0.05,
        stats_recorder: Optional["BatchEmbeddingStatsRecorder"] = None,
    ):
        self._api = api_fn
        self._calculator = BatchSizeCalculator(limits)
        self._interval = flush_interval_seconds
        self._stats = stats_recorder
        self._pending: List[EmbeddingRequestItem] = []
        self._lock = asyncio.Lock()
        self._flush_task: Optional[asyncio.Task] = None

    async def embed(self, text: str, metadata: dict = None) -> List[float]:
        item = EmbeddingRequestItem(text=text, metadata=metadata or {})
        async with self._lock:
            self._pending.append(item)
            if self._flush_task is None or self._flush_task.done():
                self._flush_task = asyncio.create_task(self._scheduled_flush())
        return await item.wait()

    async def _scheduled_flush(self) -> None:
        await asyncio.sleep(self._interval)
        await self._flush()

    async def flush(self) -> None:
        await self._flush()

    async def _flush(self) -> None:
        async with self._lock:
            if not self._pending:
                return
            items_to_process = self._pending[:]
            self._pending.clear()

        while items_to_process:
            batch, items_to_process = self._calculator.compute_batch(items_to_process)
            await self._submit_batch(batch)

    async def _submit_batch(self, batch: List[EmbeddingRequestItem]) -> None:
        texts = [item.text for item in batch]
        start = time.monotonic()
        try:
            vectors = await asyncio.get_event_loop().run_in_executor(
                None, self._api, texts
            )
            latency_ms = round((time.monotonic() - start) * 1000, 2)
            if self._stats:
                self._stats.record_batch(len(batch), latency_ms, success=True)
            for item, vector in zip(batch, vectors):
                item.set_result(vector)
        except Exception as exc:
            latency_ms = round((time.monotonic() - start) * 1000, 2)
            if self._stats:
                self._stats.record_batch(len(batch), latency_ms, success=False)
            for item in batch:
                item.set_error(exc)
```

## Solution 5: Batch Embedding Stats Recorder

```python
import time
from collections import deque
from threading import Lock
from typing import Deque, Tuple


class BatchEmbeddingStatsRecorder:
    """
    Tracks batch sizes, latencies, and success rates to measure
    the efficiency gain from batching.
    """

    def __init__(self, window_size: int = 5000):
        self._window = window_size
        self._batches: Deque[Tuple[float, int, float, bool]] = deque(maxlen=window_size)
        # (ts, batch_size, latency_ms, success)
        self._lock = Lock()

    def record_batch(self, batch_size: int, latency_ms: float, success: bool) -> None:
        with self._lock:
            self._batches.append((time.time(), batch_size, latency_ms, success))

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        with self._lock:
            recent = [(ts, sz, lat, ok) for ts, sz, lat, ok in self._batches if ts >= cutoff]

        if not recent:
            return {"window_seconds": window_seconds, "batches": 0}

        total_batches = len(recent)
        total_items = sum(sz for _, sz, _, _ in recent)
        failed = sum(1 for _, _, _, ok in recent if not ok)
        latencies = [lat for _, _, lat, _ in recent]
        avg_batch = total_items / total_batches
        avg_latency = sum(latencies) / len(latencies)

        return {
            "window_seconds": window_seconds,
            "batches": total_batches,
            "total_items_embedded": total_items,
            "avg_batch_size": round(avg_batch, 1),
            "avg_latency_ms": round(avg_latency, 2),
            "failed_batches": failed,
            "success_rate": round(1 - failed / total_batches, 4),
            "estimated_single_call_latency_ms": round(avg_latency * avg_batch, 2),
            "latency_savings_pct": round(
                (1 - 1 / max(avg_batch, 1)) * 100, 1
            ),
        }
```

## Solution 6: Parallel Batch Dispatcher

```python
import asyncio
from typing import Any, Callable, List


class ParallelBatchDispatcher:
    """
    Splits a large list of texts into batches and dispatches them
    in parallel up to a concurrency limit, then collects all results.
    """

    def __init__(
        self,
        client: BatchingEmbeddingClient,
        max_concurrent_batches: int = 4,
    ):
        self._client = client
        self._semaphore = asyncio.Semaphore(max_concurrent_batches)

    async def embed_all(self, texts: List[str]) -> List[List[float]]:
        async def _embed_one(text: str) -> List[float]:
            async with self._semaphore:
                return await self._client.embed(text)

        tasks = [asyncio.create_task(_embed_one(text)) for text in texts]
        results = await asyncio.gather(*tasks)
        return list(results)
```

## Comparison

| Approach | Accumulation Buffer | Token-Aware Batching | Result Ordering | Throughput Stats | Parallel Dispatch |
|---|---|---|---|---|---|
| EmbeddingRequestItem | No | No | Via future | No | No |
| BatchSizeCalculator | No | Yes (items + tokens) | No | No | No |
| BatchAccumulationBuffer | Yes (size + timer) | No | No | No | No |
| BatchingEmbeddingClient | Via buffer | Via calculator | Yes (future per item) | Via recorder | No |
| BatchEmbeddingStatsRecorder | No | No | No | Yes | No |
| ParallelBatchDispatcher | No | No | Yes (gather order) | No | Yes |

**Best for production**: Set `flush_interval_seconds=0.05` (50ms) — this gives enough time for concurrent requests to accumulate into a meaningful batch without adding perceptible latency to individual embed calls. Use `BatchSizeCalculator` with the actual model's limits rather than a fixed batch size: OpenAI's text-embedding-3 accepts 2048 items per batch but has a token limit; sending 2048 items each containing 4000 tokens will be rejected. Monitor `avg_batch_size` via `BatchEmbeddingStatsRecorder`: if it stays near 1 after deploying batching, the flush interval is too short or requests are not sufficiently concurrent to accumulate. The `latency_savings_pct` metric approximates the per-item latency reduction assuming API latency is dominated by round-trip overhead rather than compute.
