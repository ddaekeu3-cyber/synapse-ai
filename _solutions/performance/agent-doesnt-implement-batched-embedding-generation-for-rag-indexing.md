---
title: "Agent Doesn't Implement Batched Embedding Generation for RAG Indexing"
description: "RAG pipelines that call the embedding API once per document pay maximum per-request overhead: each API call incurs network round-trip cost and rate-limit quota regardless of the payload size. Batching multiple documents into a single embedding request reduces network overhead by 10–100× and dramatically improves indexing throughput. Implement an embedding batcher that accumulates documents up to the API's max batch size, fires requests concurrently, and handles partial batch failures with per-document retry."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-batched-embedding-generation-for-rag-indexing
tags: [batched-embeddings, rag-indexing, embedding-throughput, batch-api, parallel-embedding, indexing-performance]
symptoms:
  - "Indexing 10,000 documents takes 3 hours because embeddings fire one document at a time"
  - "Embedding API rate limit is hit after 100 requests even though each request has only 1 document"
  - "No batching logic — each document gets its own API call regardless of batch size limits"
  - "Embedding API supports batches of 100 documents but the agent sends batches of 1"
  - "Re-indexing after a schema change takes all weekend due to sequential embedding calls"
---

## Why This Happens

The simplest embedding integration is `embed(document)` — one call per document. This is correct for interactive queries (one document at a time) but wrong for bulk indexing where hundreds or thousands of documents need embeddings. The embedding API typically supports batches of 16–2048 documents per call. Without batching, the agent pays per-request overhead (auth, network round-trip, rate-limit token) for each document individually, achieving a fraction of the API's theoretical throughput. Batched embedding generation groups documents, fires concurrent batch requests, and reassembles results in the original document order.

## Solution 1: Embedding Request and Result

```python
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class EmbeddingRequest:
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    texts: List[str] = field(default_factory=list)
    model: str = "text-embedding-3-small"
    metadata: Dict[str, Any] = field(default_factory=dict)
    submitted_at: float = field(default_factory=time.time)


@dataclass
class EmbeddingResult:
    request_id: str
    embeddings: List[List[float]]     # one vector per input text
    model: str
    tokens_used: int = 0
    latency_ms: float = 0.0
    error: Optional[str] = None

    def is_success(self) -> bool:
        return self.error is None and len(self.embeddings) > 0

    def get_embedding(self, index: int) -> Optional[List[float]]:
        if 0 <= index < len(self.embeddings):
            return self.embeddings[index]
        return None
```

## Solution 2: Document Embedding Batcher

```python
import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple


@dataclass
class BatchConfig:
    max_batch_size: int = 100             # max documents per API call
    max_tokens_per_batch: int = 800_000   # approximate token limit
    max_concurrent_batches: int = 5
    retry_failed_items: bool = True
    max_retries: int = 2
    retry_delay_seconds: float = 1.0


class DocumentEmbeddingBatcher:
    """
    Splits a document list into optimal batches, fires them concurrently,
    and reassembles results in original order. Handles partial failures
    by retrying individual documents from failed batches.
    """

    def __init__(self, config: BatchConfig):
        self._config = config
        self._total_docs = 0
        self._total_batches = 0
        self._total_tokens = 0
        self._failed_docs = 0

    def _estimate_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)

    def _create_batches(self, texts: List[str]) -> List[Tuple[List[int], List[str]]]:
        """Returns list of (original_indices, texts) batches."""
        batches = []
        current_indices: List[int] = []
        current_texts: List[str] = []
        current_tokens = 0

        for i, text in enumerate(texts):
            tok = self._estimate_tokens(text)
            if (
                len(current_texts) >= self._config.max_batch_size
                or current_tokens + tok > self._config.max_tokens_per_batch
            ) and current_texts:
                batches.append((list(current_indices), list(current_texts)))
                current_indices, current_texts, current_tokens = [], [], 0

            current_indices.append(i)
            current_texts.append(text)
            current_tokens += tok

        if current_texts:
            batches.append((current_indices, current_texts))

        return batches

    async def embed_all(
        self,
        texts: List[str],
        embed_fn: Callable[[List[str], str], Any],
        model: str = "text-embedding-3-small",
    ) -> List[Optional[List[float]]]:
        """
        Returns a list of embeddings in the same order as the input texts.
        None indicates a failed embedding for that document.
        """
        results: List[Optional[List[float]]] = [None] * len(texts)
        batches = self._create_batches(texts)
        self._total_batches += len(batches)
        self._total_docs += len(texts)

        semaphore = asyncio.Semaphore(self._config.max_concurrent_batches)

        async def embed_batch(
            indices: List[int],
            batch_texts: List[str],
            attempt: int = 0,
        ) -> None:
            async with semaphore:
                start = time.time()
                try:
                    embeddings = await embed_fn(batch_texts, model)
                    latency_ms = (time.time() - start) * 1000
                    for i, idx in enumerate(indices):
                        if i < len(embeddings):
                            results[idx] = embeddings[i]
                except Exception as exc:
                    if attempt < self._config.max_retries and self._config.retry_failed_items:
                        await asyncio.sleep(self._config.retry_delay_seconds * (2 ** attempt))
                        await embed_batch(indices, batch_texts, attempt + 1)
                    else:
                        self._failed_docs += len(indices)

        tasks = [
            asyncio.create_task(embed_batch(indices, batch_texts))
            for indices, batch_texts in batches
        ]
        await asyncio.gather(*tasks)
        return results

    def stats(self) -> dict:
        return {
            "total_documents": self._total_docs,
            "total_batches": self._total_batches,
            "avg_batch_size": round(
                self._total_docs / max(self._total_batches, 1), 1
            ),
            "failed_documents": self._failed_docs,
            "failure_rate": round(self._failed_docs / max(self._total_docs, 1), 4),
        }
```

## Solution 3: Streaming Index Builder

```python
import asyncio
from typing import Any, AsyncIterator, Callable, Dict, List, Optional


class StreamingIndexBuilder:
    """
    Processes a stream of documents for indexing without loading all
    documents into memory. Accumulates documents into batches and
    emits embedding results as they complete.
    """

    def __init__(
        self,
        batcher: DocumentEmbeddingBatcher,
        embed_fn: Callable,
        model: str = "text-embedding-3-small",
        buffer_size: int = 500,
    ):
        self._batcher = batcher
        self._embed_fn = embed_fn
        self._model = model
        self._buffer_size = buffer_size

    async def index_stream(
        self,
        document_stream: AsyncIterator[dict],  # each doc has "id" and "text"
    ) -> AsyncIterator[dict]:
        """
        Yields {"doc_id": str, "embedding": List[float] or None} per document.
        """
        buffer: List[dict] = []

        async def flush(docs: List[dict]):
            texts = [d["text"] for d in docs]
            embeddings = await self._batcher.embed_all(texts, self._embed_fn, self._model)
            for doc, emb in zip(docs, embeddings):
                yield {"doc_id": doc["id"], "embedding": emb, "text": doc["text"]}

        async for doc in document_stream:
            buffer.append(doc)
            if len(buffer) >= self._buffer_size:
                async for result in flush(buffer):
                    yield result
                buffer.clear()

        if buffer:
            async for result in flush(buffer):
                yield result
```

## Solution 4: Embedding Throughput Monitor

```python
import time
from collections import deque
from typing import Deque


class EmbeddingThroughputMonitor:
    """
    Tracks embedding throughput in documents/second and tokens/second.
    Detects when throughput drops below expected levels (API throttling,
    slow batches, oversized documents).
    """

    def __init__(self, window_seconds: float = 60.0):
        self._window = window_seconds
        self._events: Deque[dict] = deque()

    def record_batch(self, doc_count: int, token_count: int, latency_ms: float) -> None:
        self._events.append({
            "ts": time.time(),
            "docs": doc_count,
            "tokens": token_count,
            "latency_ms": latency_ms,
        })

    def _trim(self) -> None:
        cutoff = time.time() - self._window
        while self._events and self._events[0]["ts"] < cutoff:
            self._events.popleft()

    def throughput(self) -> dict:
        self._trim()
        if not self._events:
            return {"docs_per_second": 0, "tokens_per_second": 0}
        total_docs = sum(e["docs"] for e in self._events)
        total_tokens = sum(e["tokens"] for e in self._events)
        elapsed = max(
            self._events[-1]["ts"] - self._events[0]["ts"],
            0.001,
        )
        avg_latency = sum(e["latency_ms"] for e in self._events) / len(self._events)
        return {
            "docs_per_second": round(total_docs / elapsed, 1),
            "tokens_per_second": round(total_tokens / elapsed, 0),
            "avg_batch_latency_ms": round(avg_latency, 1),
            "batches_in_window": len(self._events),
        }
```

## Solution 5: Failed Document Requeue

```python
import asyncio
from typing import Any, Callable, Dict, List, Optional, Tuple


class FailedDocumentRequeue:
    """
    Collects documents that failed embedding (returned None)
    and retries them individually with exponential backoff.
    Useful for documents that fail in batch but succeed individually
    (e.g., oversized documents that fit within single-doc API limits).
    """

    def __init__(
        self,
        embed_fn: Callable,
        model: str = "text-embedding-3-small",
        max_retries: int = 3,
    ):
        self._embed_fn = embed_fn
        self._model = model
        self._max_retries = max_retries
        self._requeued = 0
        self._recovered = 0

    async def retry_failed(
        self,
        failed_items: List[Tuple[str, str]],  # (doc_id, text)
    ) -> Dict[str, Optional[List[float]]]:
        results: Dict[str, Optional[List[float]]] = {}
        self._requeued += len(failed_items)

        for doc_id, text in failed_items:
            for attempt in range(self._max_retries):
                try:
                    embeddings = await self._embed_fn([text], self._model)
                    if embeddings and embeddings[0]:
                        results[doc_id] = embeddings[0]
                        self._recovered += 1
                        break
                except Exception:
                    await asyncio.sleep(2 ** attempt)
            else:
                results[doc_id] = None

        return results

    def stats(self) -> dict:
        return {
            "requeued": self._requeued,
            "recovered": self._recovered,
            "recovery_rate": round(self._recovered / max(self._requeued, 1), 4),
        }
```

## Solution 6: Batched Embedding Dashboard

```python
import time


class BatchedEmbeddingDashboard:
    """Combines batcher stats, throughput metrics, and requeue recovery stats."""

    def __init__(
        self,
        batcher: DocumentEmbeddingBatcher,
        monitor: EmbeddingThroughputMonitor,
        requeue: FailedDocumentRequeue,
    ):
        self._batcher = batcher
        self._monitor = monitor
        self._requeue = requeue

    def render(self) -> dict:
        batcher_stats = self._batcher.stats()
        throughput = self._monitor.throughput()
        requeue_stats = self._requeue.stats()

        alerts = []
        if batcher_stats["failure_rate"] > 0.05:
            alerts.append({
                "type": "high_failure_rate",
                "rate": batcher_stats["failure_rate"],
                "message": "More than 5% of documents failed embedding — check API quotas and document sizes.",
            })
        if throughput.get("docs_per_second", 100) < 10 and batcher_stats["total_documents"] > 100:
            alerts.append({
                "type": "low_throughput",
                "docs_per_second": throughput.get("docs_per_second"),
                "message": "Embedding throughput below 10 docs/sec — check batch size and concurrency settings.",
            })

        return {
            "generated_at": time.time(),
            "batcher_stats": batcher_stats,
            "throughput": throughput,
            "requeue_stats": requeue_stats,
            "alerts": alerts,
            "healthy": len(alerts) == 0,
        }
```

## Comparison

| Approach | Batching | Concurrency | Order Preservation | Failed Retry | Throughput Tracking |
|---|---|---|---|---|---|
| DocumentEmbeddingBatcher | Yes (token-aware) | Yes (semaphore) | Yes | Yes (per-batch) | No |
| StreamingIndexBuilder | Via batcher | Via batcher | Yes | Via batcher | No |
| EmbeddingThroughputMonitor | No | No | No | No | Yes |
| FailedDocumentRequeue | No | No | No | Yes (per-doc) | No |
| BatchedEmbeddingDashboard | No | No | No | No | Yes |

**Best for production**: Set `max_batch_size` to the API's documented maximum (100 for OpenAI text-embedding-3, 96 for Anthropic's embedding endpoint). Set `max_concurrent_batches=5` to stay well within rate limits while maximizing throughput. The token-aware batching in `_create_batches` is critical: without it, batches containing long documents will hit token-per-request limits and fail. Use `FailedDocumentRequeue` as a second-pass sweep after the main batch run — documents that fail in a mixed batch often succeed when sent individually, particularly oversized ones that were near the token limit.
