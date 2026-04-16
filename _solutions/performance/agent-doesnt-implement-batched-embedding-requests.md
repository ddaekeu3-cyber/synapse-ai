---
title: "Agent Doesn't Implement Batched Embedding Requests"
description: "Agents that send one embedding API call per text string incur per-request overhead for every item: embedding 50 document chunks one at a time makes 50 round-trips when a single batched call would suffice. Implement batched embedding requests that accumulate texts up to a configurable batch size, dispatch them in a single API call, and distribute results back to waiting callers."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-batched-embedding-requests
tags: [batching, embedding, api-efficiency, throughput, latency-amortization, rag-optimization]
symptoms:
  - "Embedding 100 chunks makes 100 serial API calls instead of 1-2 batched calls"
  - "Embedding latency is 50× higher than necessary due to per-request overhead"
  - "Rate limiter triggers because individual calls arrive faster than batched ones would"
  - "No measurement of how many calls could have been batched together"
  - "Concurrent embedding requests for the same session are never coalesced"
---

## Why This Happens

Embedding APIs accept arrays of inputs and return arrays of vectors in one call. Agents that call `embed(text)` in a loop convert an O(1) batch operation into O(n) round-trips. Each round-trip incurs network latency, TLS overhead, and API request accounting. Batching requires collecting pending embed requests within a short time window or up to a maximum batch size, dispatching them together, and returning each caller's result from the shared response. The main complexity is coordinating asynchronous callers who independently request embeddings at different times.

## Solution 1: Pending Embedding Request

```python
import asyncio
import time
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class PendingEmbeddingRequest:
    text: str
    model: str
    future: asyncio.Future = field(default_factory=asyncio.get_event_loop().create_future, repr=False)
    enqueued_at: float = field(default_factory=time.time)
    token_count: Optional[int] = None
```

## Solution 2: Embedding Batch Accumulator

```python
import asyncio
import time
from typing import Any, Callable, List, Optional


class EmbeddingBatchAccumulator:
    """
    Accumulates embedding requests and dispatches them as a batch
    when either the batch size limit or the flush interval is reached.
    """

    def __init__(
        self,
        embed_batch_fn: Callable[[List[str], str], Any],
        max_batch_size: int = 100,
        flush_interval_seconds: float = 0.05,   # 50ms window
        default_model: str = "text-embedding-3-small",
    ):
        self._embed_fn = embed_batch_fn
        self._max_batch = max_batch_size
        self._flush_interval = flush_interval_seconds
        self._model = default_model
        self._pending: List[PendingEmbeddingRequest] = []
        self._lock = asyncio.Lock()
        self._flush_task: Optional[asyncio.Task] = None
        self._total_batches = 0
        self._total_requests = 0

    async def embed(self, text: str, model: Optional[str] = None) -> List[float]:
        loop = asyncio.get_event_loop()
        request = PendingEmbeddingRequest(
            text=text,
            model=model or self._model,
            future=loop.create_future(),
        )

        async with self._lock:
            self._pending.append(request)
            should_flush = len(self._pending) >= self._max_batch
            if should_flush:
                await self._flush()
            elif self._flush_task is None or self._flush_task.done():
                self._flush_task = asyncio.create_task(self._scheduled_flush())

        return await request.future

    async def _scheduled_flush(self) -> None:
        await asyncio.sleep(self._flush_interval)
        async with self._lock:
            if self._pending:
                await self._flush()

    async def _flush(self) -> None:
        if not self._pending:
            return
        batch = list(self._pending)
        self._pending = []
        self._total_batches += 1
        self._total_requests += len(batch)

        texts = [r.text for r in batch]
        model = batch[0].model

        try:
            vectors = await self._embed_fn(texts, model)
            for request, vector in zip(batch, vectors):
                if not request.future.done():
                    request.future.set_result(vector)
        except Exception as exc:
            for request in batch:
                if not request.future.done():
                    request.future.set_exception(exc)

    def stats(self) -> dict:
        avg_batch = round(self._total_requests / max(self._total_batches, 1), 2)
        return {
            "total_batches_dispatched": self._total_batches,
            "total_requests": self._total_requests,
            "avg_batch_size": avg_batch,
            "pending": len(self._pending),
        }
```

## Solution 3: Model-Segregated Batch Manager

```python
from typing import Any, Callable, Dict, List, Optional


class ModelSegregatedBatchManager:
    """
    Maintains a separate EmbeddingBatchAccumulator per model name.
    Ensures requests for different models are not mixed in one batch.
    """

    def __init__(
        self,
        embed_batch_fn: Callable[[List[str], str], Any],
        max_batch_size: int = 100,
        flush_interval_seconds: float = 0.05,
    ):
        self._embed_fn = embed_batch_fn
        self._max_batch = max_batch_size
        self._flush_interval = flush_interval_seconds
        self._accumulators: Dict[str, EmbeddingBatchAccumulator] = {}

    def _get_accumulator(self, model: str) -> EmbeddingBatchAccumulator:
        if model not in self._accumulators:
            self._accumulators[model] = EmbeddingBatchAccumulator(
                embed_batch_fn=self._embed_fn,
                max_batch_size=self._max_batch,
                flush_interval_seconds=self._flush_interval,
                default_model=model,
            )
        return self._accumulators[model]

    async def embed(self, text: str, model: str) -> List[float]:
        return await self._get_accumulator(model).embed(text, model)

    async def embed_many(self, texts: List[str], model: str) -> List[List[float]]:
        import asyncio
        return list(await asyncio.gather(*[self.embed(t, model) for t in texts]))

    def stats(self) -> dict:
        return {
            model: acc.stats()
            for model, acc in self._accumulators.items()
        }
```

## Solution 4: Batch Size Optimizer

```python
import time
from threading import Lock
from typing import List, Optional


class BatchSizeOptimizer:
    """
    Tracks batch dispatch latency and adjusts max_batch_size to stay
    within a target latency budget. Larger batches amortize overhead
    but increase latency for individual callers.
    """

    def __init__(
        self,
        target_latency_ms: float = 200.0,
        min_batch_size: int = 10,
        max_batch_size: int = 200,
    ):
        self._target = target_latency_ms
        self._min = min_batch_size
        self._max = max_batch_size
        self._current = 50
        self._latencies: List[float] = []
        self._lock = Lock()

    def record_batch(self, batch_size: int, latency_ms: float) -> None:
        with self._lock:
            self._latencies.append(latency_ms)
            if len(self._latencies) > 100:
                self._latencies.pop(0)
            self._adjust()

    def _adjust(self) -> None:
        if len(self._latencies) < 10:
            return
        avg = sum(self._latencies[-10:]) / 10
        if avg > self._target * 1.2 and self._current > self._min:
            self._current = max(self._min, int(self._current * 0.9))
        elif avg < self._target * 0.8 and self._current < self._max:
            self._current = min(self._max, int(self._current * 1.1))

    def recommended_batch_size(self) -> int:
        with self._lock:
            return self._current
```

## Solution 5: Batch Throughput Monitor

```python
import time
from threading import Lock
from typing import List


class EmbeddingBatchThroughputMonitor:
    """
    Records batch statistics over time to surface throughput gains
    from batching vs. individual call overhead.
    """

    def __init__(self):
        self._records: List[dict] = []
        self._lock = Lock()

    def record(self, batch_size: int, latency_ms: float) -> None:
        with self._lock:
            self._records.append({
                "ts": time.time(),
                "batch_size": batch_size,
                "latency_ms": latency_ms,
                "ms_per_item": round(latency_ms / max(batch_size, 1), 2),
            })
            if len(self._records) > 10000:
                self._records.pop(0)

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        with self._lock:
            recent = [r for r in self._records if r["ts"] >= cutoff]
        if not recent:
            return {"window_seconds": window_seconds, "batches": 0}

        sizes = [r["batch_size"] for r in recent]
        ms_per_items = [r["ms_per_item"] for r in recent]
        total_requests = sum(sizes)
        return {
            "window_seconds": window_seconds,
            "batches": len(recent),
            "total_embedding_requests": total_requests,
            "avg_batch_size": round(sum(sizes) / len(sizes), 2),
            "max_batch_size": max(sizes),
            "avg_ms_per_item": round(sum(ms_per_items) / len(ms_per_items), 2),
            "api_calls_saved_est": total_requests - len(recent),
        }
```

## Solution 6: Batched Embedding Dashboard

```python
import time


class BatchedEmbeddingDashboard:
    """
    Combines manager stats, throughput monitor, and optimizer state.
    """

    def __init__(
        self,
        manager: ModelSegregatedBatchManager,
        monitor: EmbeddingBatchThroughputMonitor,
        optimizer: BatchSizeOptimizer,
    ):
        self._manager = manager
        self._monitor = monitor
        self._optimizer = optimizer

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "per_model_stats": self._manager.stats(),
            "throughput_1h": self._monitor.summary(3600.0),
            "recommended_batch_size": self._optimizer.recommended_batch_size(),
        }
```

## Comparison

| Approach | Time-Window Batching | Size-Limit Batching | Per-Model Segregation | Adaptive Sizing | Throughput Tracking |
|---|---|---|---|---|---|
| EmbeddingBatchAccumulator | Yes (50ms) | Yes | No | No | Yes (counters) |
| ModelSegregatedBatchManager | Via accumulator | Via accumulator | Yes | No | Via accumulators |
| BatchSizeOptimizer | No | No | No | Yes | No |
| EmbeddingBatchThroughputMonitor | No | No | No | No | Yes (over time) |

**Best for production**: Set `flush_interval_seconds=0.05` (50ms) for interactive workloads and `0.2` for batch processing pipelines — the window trades individual request latency for higher batch fill rates. Set `max_batch_size=100` for OpenAI's `text-embedding-3-small` (its documented batch limit). Monitor `avg_batch_size` via `EmbeddingBatchThroughputMonitor`: if it stays below 5, the request arrival rate is too low to benefit from batching and the flush interval should be increased. Track `api_calls_saved_est` — at 100 items per call, batching reduces API call overhead by 99% for bulk indexing workflows.
