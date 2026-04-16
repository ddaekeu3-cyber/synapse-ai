---
title: "Agent Doesn't Implement Async Batch Processor for Embedding Workloads"
description: "Agents that embed documents one at a time pay per-request HTTP overhead on every call and leave embedding API throughput on the table — most providers process batches of 100–2048 texts in a single call with no extra latency. Implement an async batch processor that accumulates individual embed requests within a short collection window, fires a single batched API call, and routes results back to each original caller."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-async-batch-processor-for-embedding-workloads
tags: [batch-processing, embedding, async, throughput-optimization, api-efficiency, request-coalescing]
symptoms:
  - "Embedding 1000 documents makes 1000 separate API calls — each with TLS handshake overhead"
  - "Embedding API rate limit hit because too many small requests, not because tokens are exhausted"
  - "Total embedding time is 500ms × 1000 = 8 minutes; batch API would take 30 seconds"
  - "No mechanism to coalesce concurrent embed() calls from multiple agent coroutines"
  - "Embedding pipeline cannot scale because it is purely sequential"
---

## Why This Happens

The simplest embedding implementation is `await embed_api(text)` in a loop. This is correct but wasteful: each call incurs HTTP overhead, TLS negotiation, and API gateway processing. Embedding APIs accept arrays of texts and process them GPU-in-parallel for nearly the same wall-clock time as a single text. Batch processing requires collecting individual requests within a short window (5–50ms), assembling them into one API call, and demultiplexing the response array back to the individual futures that are awaiting results.

## Solution 1: Embed Request and Result

```python
import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class EmbedRequest:
    request_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    text: str = ""
    model: str = "text-embedding-3-small"
    submitted_at: float = field(default_factory=time.time)
    future: Optional[asyncio.Future] = field(default=None, repr=False)

    def __post_init__(self):
        if self.future is None:
            self.future = asyncio.get_event_loop().create_future()

    def resolve(self, embedding: List[float]) -> None:
        if not self.future.done():
            self.future.set_result(embedding)

    def reject(self, error: Exception) -> None:
        if not self.future.done():
            self.future.set_exception(error)

    async def await_result(self, timeout: float = 30.0) -> List[float]:
        return await asyncio.wait_for(self.future, timeout=timeout)
```

## Solution 2: Batch Collector

```python
import asyncio
import time
from typing import List, Optional


class BatchCollector:
    """
    Collects individual embed requests within a time window,
    then fires a batch call when the window closes or the batch is full.
    """

    def __init__(
        self,
        max_batch_size: int = 256,
        collection_window_ms: float = 20.0,
    ):
        self._max_size = max_batch_size
        self._window_ms = collection_window_ms
        self._pending: List[EmbedRequest] = []
        self._lock = asyncio.Lock()
        self._flush_event = asyncio.Event()
        self._window_task: Optional[asyncio.Task] = None

    async def add(self, request: EmbedRequest) -> None:
        async with self._lock:
            self._pending.append(request)
            if len(self._pending) >= self._max_size:
                self._flush_event.set()
            elif not self._window_task or self._window_task.done():
                self._window_task = asyncio.create_task(
                    self._window_timer()
                )

    async def _window_timer(self) -> None:
        await asyncio.sleep(self._window_ms / 1000.0)
        self._flush_event.set()

    async def drain(self) -> List[EmbedRequest]:
        """Wait for a flush signal and return all collected requests."""
        await self._flush_event.wait()
        async with self._lock:
            batch = list(self._pending)
            self._pending.clear()
            self._flush_event.clear()
            return batch

    def pending_count(self) -> int:
        return len(self._pending)
```

## Solution 3: Async Batch Embedding Processor

```python
import asyncio
import time
from typing import Callable, Dict, List, Optional


class AsyncBatchEmbeddingProcessor:
    """
    Runs a background loop that drains the batch collector,
    calls the embedding API with the full batch, and routes
    results back to each request's Future.
    """

    def __init__(
        self,
        collector: BatchCollector,
        embed_fn: Callable[[List[str], str], List[List[float]]],
        max_retries: int = 3,
        retry_delay_seconds: float = 1.0,
    ):
        self._collector = collector
        self._embed_fn = embed_fn
        self._max_retries = max_retries
        self._retry_delay = retry_delay_seconds
        self._processor_task: Optional[asyncio.Task] = None
        self._batches_processed = 0
        self._texts_processed = 0
        self._errors = 0

    def start(self) -> None:
        self._processor_task = asyncio.create_task(self._process_loop())

    def stop(self) -> None:
        if self._processor_task:
            self._processor_task.cancel()

    async def _process_loop(self) -> None:
        while True:
            batch = await self._collector.drain()
            if not batch:
                continue
            asyncio.create_task(self._process_batch(batch))

    async def _process_batch(self, requests: List[EmbedRequest]) -> None:
        texts = [r.text for r in requests]
        model = requests[0].model   # assume homogeneous model within batch

        for attempt in range(self._max_retries + 1):
            try:
                embeddings = await self._embed_fn(texts, model)
                for req, emb in zip(requests, embeddings):
                    req.resolve(emb)
                self._batches_processed += 1
                self._texts_processed += len(texts)
                return
            except Exception as exc:
                if attempt < self._max_retries:
                    await asyncio.sleep(self._retry_delay * (2 ** attempt))
                else:
                    self._errors += 1
                    for req in requests:
                        req.reject(exc)

    def stats(self) -> dict:
        return {
            "batches_processed": self._batches_processed,
            "texts_processed": self._texts_processed,
            "errors": self._errors,
            "avg_batch_size": round(
                self._texts_processed / max(self._batches_processed, 1), 1
            ),
            "pending": self._collector.pending_count(),
        }
```

## Solution 4: Batching Embedding Client

```python
from typing import List


class BatchingEmbeddingClient:
    """
    Drop-in replacement for a direct embedding API client.
    Callers use embed_one() — it queues the request and awaits the result.
    Multiple concurrent callers automatically form batches.
    """

    def __init__(
        self,
        processor: AsyncBatchEmbeddingProcessor,
        collector: BatchCollector,
        default_model: str = "text-embedding-3-small",
    ):
        self._processor = processor
        self._collector = collector
        self._default_model = default_model

    async def embed_one(
        self,
        text: str,
        model: Optional[str] = None,
        timeout: float = 30.0,
    ) -> List[float]:
        request = EmbedRequest(
            text=text,
            model=model or self._default_model,
        )
        await self._collector.add(request)
        return await request.await_result(timeout=timeout)

    async def embed_many(
        self,
        texts: List[str],
        model: Optional[str] = None,
        concurrency: int = 8,
    ) -> List[List[float]]:
        """Embed multiple texts with bounded concurrency."""
        sem = asyncio.Semaphore(concurrency)

        async def embed_with_sem(text: str) -> List[float]:
            async with sem:
                return await self.embed_one(text, model)

        return await asyncio.gather(*[embed_with_sem(t) for t in texts])


from typing import Optional
import asyncio
```

## Solution 5: Model-Partitioned Batch Router

```python
import asyncio
from typing import Callable, Dict


class ModelPartitionedBatchRouter:
    """
    Routes embed requests to model-specific batch processors.
    Different models have different batch size limits and latency profiles.
    """

    def __init__(self):
        self._processors: Dict[str, AsyncBatchEmbeddingProcessor] = {}
        self._collectors: Dict[str, BatchCollector] = {}

    def register_model(
        self,
        model: str,
        embed_fn: Callable,
        max_batch_size: int = 256,
        collection_window_ms: float = 20.0,
    ) -> None:
        collector = BatchCollector(
            max_batch_size=max_batch_size,
            collection_window_ms=collection_window_ms,
        )
        processor = AsyncBatchEmbeddingProcessor(
            collector=collector,
            embed_fn=embed_fn,
        )
        self._collectors[model] = collector
        self._processors[model] = processor
        processor.start()

    async def embed(self, text: str, model: str) -> list:
        collector = self._collectors.get(model)
        processor = self._processors.get(model)
        if not collector or not processor:
            raise KeyError(f"model '{model}' not registered")
        request = EmbedRequest(text=text, model=model)
        await collector.add(request)
        return await request.await_result()

    def stats(self) -> dict:
        return {
            model: proc.stats()
            for model, proc in self._processors.items()
        }

    def stop_all(self) -> None:
        for proc in self._processors.values():
            proc.stop()
```

## Solution 6: Batch Processing Health Monitor

```python
import time


class EmbeddingBatchHealthMonitor:
    """
    Monitors batch processor efficiency and alerts on degradation.
    """

    def __init__(
        self,
        processor: AsyncBatchEmbeddingProcessor,
        collector: BatchCollector,
        min_avg_batch_size: float = 10.0,
        max_error_rate: float = 0.02,
    ):
        self._processor = processor
        self._collector = collector
        self._min_batch = min_avg_batch_size
        self._max_errors = max_error_rate

    def check(self) -> dict:
        stats = self._processor.stats()
        alerts = []

        if stats["avg_batch_size"] < self._min_batch and stats["batches_processed"] > 10:
            alerts.append({
                "type": "small_batches",
                "avg_batch_size": stats["avg_batch_size"],
                "target": self._min_batch,
                "recommendation": "increase collection_window_ms or add more concurrent callers",
            })

        total = stats["batches_processed"] + stats["errors"]
        error_rate = stats["errors"] / max(total, 1)
        if error_rate > self._max_errors:
            alerts.append({
                "type": "high_error_rate",
                "error_rate": round(error_rate, 4),
                "recommendation": "check embedding API connectivity and rate limits",
            })

        if self._collector.pending_count() > 100:
            alerts.append({
                "type": "pending_queue_backing_up",
                "pending": self._collector.pending_count(),
                "recommendation": "processor may be stuck — check for unhandled exceptions",
            })

        return {
            "generated_at": time.time(),
            "healthy": len(alerts) == 0,
            "stats": stats,
            "alerts": alerts,
        }
```

## Comparison

| Approach | Collection Window | Batch API Call | Result Routing | Multi-Model | Monitoring |
|---|---|---|---|---|---|
| BatchCollector | Yes (timer + size) | No | No | No | No |
| AsyncBatchEmbeddingProcessor | Via collector | Yes | Yes (Futures) | No | No |
| BatchingEmbeddingClient | Via collector | Via processor | Via futures | No | No |
| ModelPartitionedBatchRouter | Per model | Per model | Via futures | Yes | No |
| EmbeddingBatchHealthMonitor | No | No | No | No | Yes |

**Best for production**: Set `collection_window_ms=20` as a starting point — this adds at most 20ms of latency in exchange for batching all concurrent requests. Use `embed_many()` for bulk ingestion pipelines with `concurrency=8` to avoid overwhelming the collector. Register separate model configs for small (fast, cheap) and large (accurate, expensive) embedding models via `ModelPartitionedBatchRouter`. Monitor `avg_batch_size` — if it stays below 5, either your traffic is low (acceptable) or callers are not using the batching client (fix the integration).
