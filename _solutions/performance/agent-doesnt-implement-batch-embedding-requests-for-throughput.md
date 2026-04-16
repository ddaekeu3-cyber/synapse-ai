---
title: "Agent Doesn't Implement Batch Embedding Requests for Throughput"
description: "Agents that embed text one document at a time issue a separate API call per document — paying per-request overhead (network RTT, authentication, rate limit consumption) for each item even when the embedding provider supports batching dozens of texts in a single call. Implement request batching that accumulates embedding requests within a time window or up to a size limit, dispatches them as a single API call, and routes results back to each caller."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-batch-embedding-requests-for-throughput
tags: [batch-embedding, throughput-optimization, request-batching, embedding-api, latency-amortization, vectorization]
symptoms:
  - "Embedding 100 documents makes 100 separate API calls instead of 1-2 batched calls"
  - "Embedding throughput is bottlenecked by per-request network RTT rather than model compute"
  - "Rate limit errors appear during bulk indexing even though total token volume is within quota"
  - "Wall-clock time for bulk embedding is proportional to document count, not total token count"
  - "No reuse of embedding API connections across concurrent embedding requests"
---

## Why This Happens

Embedding APIs (OpenAI, Cohere, Anthropic) accept arrays of texts in a single request and return an array of vectors. An agent that calls `embed(text)` in a loop issues one HTTP request per document — each with its own TCP handshake overhead, authentication header, and rate limit counter increment. Batching amortizes these costs: 100 documents in one call uses one rate limit slot and one network RTT. The implementation challenge is that callers are often concurrent and asynchronous — a batcher must collect requests from multiple coroutines, wait for a batch window to fill, dispatch once, and route each result back to the correct awaiting caller.

## Solution 1: Embedding Request

```python
import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, List, Optional


@dataclass
class EmbeddingRequest:
    text: str
    request_id: str = field(default_factory=lambda: f"req-{time.time_ns()}")
    future: asyncio.Future = field(default_factory=asyncio.Future)
    enqueued_at: float = field(default_factory=time.time)
    metadata: dict = field(default_factory=dict)

    def resolve(self, embedding: List[float]) -> None:
        if not self.future.done():
            self.future.set_result(embedding)

    def reject(self, exc: Exception) -> None:
        if not self.future.done():
            self.future.set_exception(exc)
```

## Solution 2: Batch Accumulator

```python
import asyncio
import time
from typing import Callable, List, Optional


class EmbeddingBatchAccumulator:
    """
    Collects EmbeddingRequest objects and flushes them as a batch
    when either the batch size limit or the time window is reached.
    """

    def __init__(
        self,
        max_batch_size: int = 96,
        max_wait_ms: float = 20.0,
        flush_fn: Optional[Callable] = None,
    ):
        self._max_size = max_batch_size
        self._max_wait = max_wait_ms / 1000.0
        self._flush_fn = flush_fn
        self._pending: List[EmbeddingRequest] = []
        self._lock = asyncio.Lock()
        self._flush_task: Optional[asyncio.Task] = None
        self._total_batches = 0
        self._total_requests = 0

    async def add(self, request: EmbeddingRequest) -> None:
        async with self._lock:
            self._pending.append(request)
            if len(self._pending) >= self._max_size:
                await self._flush_locked()
            elif self._flush_task is None or self._flush_task.done():
                self._flush_task = asyncio.ensure_future(self._schedule_flush())

    async def _schedule_flush(self) -> None:
        await asyncio.sleep(self._max_wait)
        async with self._lock:
            if self._pending:
                await self._flush_locked()

    async def _flush_locked(self) -> None:
        if not self._pending:
            return
        batch = self._pending[:]
        self._pending = []
        self._total_batches += 1
        self._total_requests += len(batch)
        if self._flush_fn:
            asyncio.ensure_future(self._flush_fn(batch))

    def stats(self) -> dict:
        return {
            "total_batches_dispatched": self._total_batches,
            "total_requests_batched": self._total_requests,
            "avg_batch_size": round(
                self._total_requests / max(self._total_batches, 1), 2
            ),
        }
```

## Solution 3: Batch Embedding Dispatcher

```python
import asyncio
import time
from typing import Any, Callable, List


class BatchEmbeddingDispatcher:
    """
    Takes a batch of EmbeddingRequests, calls the embedding API once
    with all texts, and resolves each request's future with its vector.
    """

    def __init__(
        self,
        embed_fn: Callable[[List[str]], Any],  # async fn([texts]) -> [[float]]
        max_retries: int = 2,
        retry_delay_seconds: float = 1.0,
    ):
        self._embed_fn = embed_fn
        self._max_retries = max_retries
        self._retry_delay = retry_delay_seconds
        self._dispatch_latencies: List[float] = []

    async def dispatch(self, batch: List[EmbeddingRequest]) -> None:
        texts = [req.text for req in batch]
        start = time.time()

        for attempt in range(self._max_retries + 1):
            try:
                embeddings = await self._embed_fn(texts)
                elapsed = time.time() - start
                self._dispatch_latencies.append(elapsed * 1000)
                if len(self._dispatch_latencies) > 1000:
                    self._dispatch_latencies.pop(0)

                for req, emb in zip(batch, embeddings):
                    req.resolve(emb)
                return
            except Exception as exc:
                if attempt < self._max_retries:
                    await asyncio.sleep(self._retry_delay * (2 ** attempt))
                else:
                    for req in batch:
                        req.reject(exc)

    def p95_dispatch_latency_ms(self) -> Optional[float]:
        if not self._dispatch_latencies:
            return None
        sorted_lats = sorted(self._dispatch_latencies)
        idx = min(int(len(sorted_lats) * 0.95), len(sorted_lats) - 1)
        return round(sorted_lats[idx], 2)
```

## Solution 4: Batching Embedding Client

```python
import asyncio
from typing import Any, Callable, List


class BatchingEmbeddingClient:
    """
    Drop-in replacement for a single-text embedding call.
    Callers await embed(text) and receive a vector — batching is
    transparent, happening automatically in the accumulator.
    """

    def __init__(
        self,
        embed_fn: Callable[[List[str]], Any],
        max_batch_size: int = 96,
        max_wait_ms: float = 20.0,
    ):
        self._dispatcher = BatchEmbeddingDispatcher(embed_fn)
        self._accumulator = EmbeddingBatchAccumulator(
            max_batch_size=max_batch_size,
            max_wait_ms=max_wait_ms,
            flush_fn=self._dispatcher.dispatch,
        )

    async def embed(self, text: str, **metadata) -> List[float]:
        """Embed a single text. Batching is handled automatically."""
        request = EmbeddingRequest(text=text, metadata=metadata)
        await self._accumulator.add(request)
        return await request.future

    async def embed_many(self, texts: List[str]) -> List[List[float]]:
        """Embed multiple texts concurrently, respecting batch limits."""
        futures = [self.embed(text) for text in texts]
        return await asyncio.gather(*futures)

    def stats(self) -> dict:
        acc_stats = self._accumulator.stats()
        return {
            **acc_stats,
            "p95_dispatch_latency_ms": self._dispatcher.p95_dispatch_latency_ms(),
        }
```

## Solution 5: Batch Size Optimizer

```python
import time
from typing import List, Optional


class BatchSizeOptimizer:
    """
    Monitors batch fill rates and latency to suggest an optimal
    max_batch_size and max_wait_ms configuration.
    """

    def __init__(self):
        self._batch_sizes: List[int] = []
        self._wait_times_ms: List[float] = []
        self._recorded_at: List[float] = []

    def record_batch(self, batch_size: int, wait_time_ms: float) -> None:
        self._batch_sizes.append(batch_size)
        self._wait_times_ms.append(wait_time_ms)
        self._recorded_at.append(time.time())

    def recommend(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent_sizes = [
            s for s, ts in zip(self._batch_sizes, self._recorded_at)
            if ts >= cutoff
        ]
        recent_waits = [
            w for w, ts in zip(self._wait_times_ms, self._recorded_at)
            if ts >= cutoff
        ]
        if not recent_sizes:
            return {"status": "insufficient_data"}

        avg_size = sum(recent_sizes) / len(recent_sizes)
        p95_size = sorted(recent_sizes)[min(int(len(recent_sizes) * 0.95), len(recent_sizes) - 1)]
        avg_wait = sum(recent_waits) / len(recent_waits) if recent_waits else 0

        return {
            "avg_batch_size": round(avg_size, 1),
            "p95_batch_size": p95_size,
            "avg_wait_ms": round(avg_wait, 1),
            "recommendation": {
                "max_batch_size": max(32, p95_size + 10),
                "max_wait_ms": max(10.0, avg_wait * 1.5),
            },
        }
```

## Solution 6: Batch Embedding Dashboard

```python
import time


class BatchEmbeddingDashboard:
    """
    Summarizes batching efficiency and throughput for operational visibility.
    """

    def __init__(
        self,
        client: BatchingEmbeddingClient,
        optimizer: BatchSizeOptimizer,
    ):
        self._client = client
        self._optimizer = optimizer

    def render(self, window_seconds: float = 3600.0) -> dict:
        return {
            "generated_at": time.time(),
            "client_stats": self._client.stats(),
            "batch_size_recommendation": self._optimizer.recommend(window_seconds),
        }
```

## Comparison

| Approach | Request Queuing | Batch Dispatch | Transparent API | Size Optimization | Dashboard |
|---|---|---|---|---|---|
| EmbeddingBatchAccumulator | Yes (time+size) | No | No | No | No |
| BatchEmbeddingDispatcher | No | Yes (retry) | No | No | No |
| BatchingEmbeddingClient | Via accumulator | Via dispatcher | Yes | No | No |
| BatchSizeOptimizer | No | No | No | Yes | No |
| BatchEmbeddingDashboard | No | No | No | No | Yes |

**Best for production**: Set `max_batch_size=96` (OpenAI's limit) and `max_wait_ms=20` — 20ms wait is imperceptible to users but collects enough concurrent requests from parallel tool calls to fill most batches. Monitor `avg_batch_size` via the dashboard: if it stays below 5, the workload is too sequential for batching to help and the wait window adds latency without benefit. For bulk indexing jobs, call `embed_many()` directly with your full document list — the accumulator will split it into provider-sized batches automatically without you managing the chunking. If the embedding provider has a tokens-per-minute limit rather than requests-per-minute, add token counting to `EmbeddingRequest` and flush when the accumulated token count exceeds the per-batch token ceiling.
