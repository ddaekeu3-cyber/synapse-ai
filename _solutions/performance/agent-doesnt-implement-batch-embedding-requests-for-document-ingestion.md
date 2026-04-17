---
title: "Agent Doesn't Implement Batch Embedding Requests for Document Ingestion"
description: "Agents that embed documents one at a time during ingestion make N sequential API calls for N documents, each incurring network round-trip overhead. Implement batch embedding that groups documents into optimally sized batches, embeds them in parallel within API rate limits, and reports throughput and cost savings compared to sequential embedding."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-batch-embedding-requests-for-document-ingestion
tags: [batch-embedding, document-ingestion, embedding-throughput, rate-limiting, parallelism, vector-indexing]
symptoms:
  - "Embedding 10,000 documents takes hours because each is embedded individually"
  - "Embedding API calls are sequential with no parallelism"
  - "Each document embedding incurs a separate HTTP round trip"
  - "No batching despite the embedding API supporting up to 2048 inputs per call"
  - "Ingestion pipeline throughput bottlenecked by embedding latency, not compute"
---

## Why This Happens

Embedding APIs accept lists of strings in a single call (e.g., OpenAI's `input` field, Anthropic's batch API). Developers unfamiliar with this feature call the API once per document, turning O(1) network overhead into O(N). Even when batching is known, optimal batch size requires balancing API limits (max inputs per call, max tokens per call) against throughput. Parallel batch dispatch with rate-limit awareness compounds the savings — instead of sequential calls, multiple batches fly concurrently within the API's allowed request-per-minute window.

## Solution 1: Embedding Batch Planner

```python
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class EmbeddingDocument:
    doc_id: str
    text: str
    metadata: dict = field(default_factory=dict)
    token_estimate: Optional[int] = None

    def estimated_tokens(self) -> int:
        if self.token_estimate is not None:
            return self.token_estimate
        return max(1, len(self.text) // 4)


@dataclass
class EmbeddingBatch:
    batch_id: int
    documents: List[EmbeddingDocument]

    @property
    def texts(self) -> List[str]:
        return [d.text for d in self.documents]

    @property
    def total_tokens(self) -> int:
        return sum(d.estimated_tokens() for d in self.documents)


class EmbeddingBatchPlanner:
    """
    Splits a list of documents into optimally sized batches respecting
    the embedding API's per-call limits on input count and total tokens.
    """

    def __init__(
        self,
        max_inputs_per_batch: int = 2048,
        max_tokens_per_batch: int = 300_000,
        min_inputs_per_batch: int = 1,
    ):
        self._max_inputs = max_inputs_per_batch
        self._max_tokens = max_tokens_per_batch
        self._min_inputs = min_inputs_per_batch

    def plan(self, documents: List[EmbeddingDocument]) -> List[EmbeddingBatch]:
        batches: List[EmbeddingBatch] = []
        current: List[EmbeddingDocument] = []
        current_tokens = 0
        batch_id = 0

        for doc in documents:
            doc_tokens = doc.estimated_tokens()
            if (
                current
                and (
                    len(current) >= self._max_inputs
                    or current_tokens + doc_tokens > self._max_tokens
                )
            ):
                batches.append(EmbeddingBatch(batch_id=batch_id, documents=current))
                batch_id += 1
                current = []
                current_tokens = 0
            current.append(doc)
            current_tokens += doc_tokens

        if current:
            batches.append(EmbeddingBatch(batch_id=batch_id, documents=current))

        return batches
```

## Solution 2: Rate-Limited Batch Embedding Client

```python
import asyncio
import time
from typing import Any, Callable, List, Tuple


class RateLimitedBatchEmbeddingClient:
    """
    Embeds document batches concurrently while respecting API rate limits.
    Uses a semaphore to cap concurrent requests and a token bucket for RPM.
    """

    def __init__(
        self,
        embed_fn: Callable[[List[str]], List[List[float]]],
        max_concurrent_requests: int = 5,
        requests_per_minute: int = 60,
    ):
        self._embed_fn = embed_fn
        self._semaphore = asyncio.Semaphore(max_concurrent_requests)
        self._rpm = requests_per_minute
        self._min_interval = 60.0 / requests_per_minute
        self._last_request_time = 0.0
        self._rate_lock = asyncio.Lock()

    async def _throttled_embed(self, texts: List[str]) -> List[List[float]]:
        async with self._rate_lock:
            now = time.time()
            wait = self._min_interval - (now - self._last_request_time)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request_time = time.time()

        async with self._semaphore:
            if asyncio.iscoroutinefunction(self._embed_fn):
                return await self._embed_fn(texts)
            return await asyncio.to_thread(self._embed_fn, texts)

    async def embed_batches(
        self,
        batches: List[EmbeddingBatch],
    ) -> List[Tuple[EmbeddingBatch, List[List[float]]]]:
        tasks = [self._throttled_embed(batch.texts) for batch in batches]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        output = []
        for batch, result in zip(batches, results):
            if isinstance(result, Exception):
                raise RuntimeError(
                    f"Batch {batch.batch_id} embedding failed: {result}"
                ) from result
            output.append((batch, result))
        return output
```

## Solution 3: Batch Embedding Result Collector

```python
from dataclasses import dataclass, field
from typing import Dict, List, Tuple


@dataclass
class EmbeddedDocument:
    doc_id: str
    text: str
    embedding: List[float]
    metadata: dict = field(default_factory=dict)
    batch_id: int = 0


class BatchEmbeddingResultCollector:
    """
    Assembles per-document embedding results from batch API responses,
    preserving the original document order and metadata.
    """

    def collect(
        self,
        batch_results: List[Tuple[EmbeddingBatch, List[List[float]]]],
    ) -> List[EmbeddedDocument]:
        embedded: List[EmbeddedDocument] = []
        for batch, embeddings in batch_results:
            if len(embeddings) != len(batch.documents):
                raise ValueError(
                    f"Batch {batch.batch_id}: expected {len(batch.documents)} embeddings, "
                    f"got {len(embeddings)}"
                )
            for doc, embedding in zip(batch.documents, embeddings):
                embedded.append(EmbeddedDocument(
                    doc_id=doc.doc_id,
                    text=doc.text,
                    embedding=embedding,
                    metadata=doc.metadata,
                    batch_id=batch.batch_id,
                ))
        return embedded
```

## Solution 4: Batch Ingestion Pipeline

```python
import time
from typing import Any, Callable, List, Optional


class BatchIngestionPipeline:
    """
    End-to-end pipeline: plan batches, embed concurrently, collect results,
    and optionally index into a vector store. Reports throughput metrics.
    """

    def __init__(
        self,
        planner: EmbeddingBatchPlanner,
        client: RateLimitedBatchEmbeddingClient,
        collector: BatchEmbeddingResultCollector,
        index_fn: Optional[Callable[[List[EmbeddedDocument]], None]] = None,
    ):
        self._planner = planner
        self._client = client
        self._collector = collector
        self._index_fn = index_fn

    async def ingest(self, documents: List[EmbeddingDocument]) -> dict:
        start = time.time()

        batches = self._planner.plan(documents)
        batch_results = await self._client.embed_batches(batches)
        embedded = self._collector.collect(batch_results)

        if self._index_fn:
            await asyncio.to_thread(self._index_fn, embedded)

        elapsed = time.time() - start
        total_tokens = sum(b.total_tokens for b in batches)

        return {
            "documents_ingested": len(embedded),
            "batches_used": len(batches),
            "total_tokens_estimated": total_tokens,
            "elapsed_seconds": round(elapsed, 2),
            "docs_per_second": round(len(embedded) / elapsed, 1) if elapsed > 0 else 0,
            "tokens_per_second": round(total_tokens / elapsed, 0) if elapsed > 0 else 0,
        }
```

## Solution 5: Incremental Batch Progress Tracker

```python
import time
from threading import Lock
from typing import List, Optional


class IncrementalBatchProgressTracker:
    """
    Tracks progress of large ingestion jobs so long-running pipelines
    can report completion percentage and estimated time remaining.
    """

    def __init__(self, total_documents: int):
        self._total = total_documents
        self._completed = 0
        self._start_time = time.time()
        self._lock = Lock()
        self._batch_times: List[float] = []

    def record_batch_complete(self, batch_size: int, elapsed_ms: float) -> None:
        with self._lock:
            self._completed += batch_size
            self._batch_times.append(elapsed_ms)

    def progress(self) -> dict:
        with self._lock:
            completed = self._completed
            batch_times = list(self._batch_times)

        elapsed = time.time() - self._start_time
        pct = completed / self._total if self._total > 0 else 0.0
        rate = completed / elapsed if elapsed > 0 else 0.0
        eta = (self._total - completed) / rate if rate > 0 else None

        return {
            "total": self._total,
            "completed": completed,
            "remaining": self._total - completed,
            "percent": round(pct * 100, 1),
            "elapsed_seconds": round(elapsed, 1),
            "docs_per_second": round(rate, 1),
            "eta_seconds": round(eta, 0) if eta is not None else None,
            "batches_completed": len(batch_times),
            "avg_batch_ms": round(sum(batch_times) / len(batch_times), 1) if batch_times else 0.0,
        }
```

## Solution 6: Batch Embedding Throughput Dashboard

```python
import time
from typing import List


class BatchEmbeddingThroughputDashboard:
    """
    Compares sequential vs. batch embedding performance
    and surfaces throughput metrics for capacity planning.
    """

    def __init__(self):
        self._run_reports: List[dict] = []

    def record_run(self, report: dict) -> None:
        self._run_reports.append({**report, "recorded_at": time.time()})

    def render(self) -> dict:
        if not self._run_reports:
            return {"runs": 0}

        recent = self._run_reports[-10:]
        avg_dps = sum(r["docs_per_second"] for r in recent) / len(recent)
        avg_batch_size = sum(
            r["documents_ingested"] / max(r["batches_used"], 1)
            for r in recent
        ) / len(recent)

        return {
            "generated_at": time.time(),
            "total_runs": len(self._run_reports),
            "avg_docs_per_second": round(avg_dps, 1),
            "avg_batch_size": round(avg_batch_size, 1),
            "total_documents_ingested": sum(r["documents_ingested"] for r in self._run_reports),
            "recent_runs": recent,
        }
```

## Comparison

| Approach | Batch Planning | Concurrent Embedding | Rate Limiting | Progress Tracking | Throughput Reporting |
|---|---|---|---|---|---|
| EmbeddingBatchPlanner | Yes (token+count limits) | No | No | No | No |
| RateLimitedBatchEmbeddingClient | No | Yes (semaphore) | Yes (RPM) | No | No |
| BatchEmbeddingResultCollector | No | No | No | No | No |
| BatchIngestionPipeline | Via planner | Via client | Via client | No | Yes |
| IncrementalBatchProgressTracker | No | No | No | Yes (ETA) | No |
| BatchEmbeddingThroughputDashboard | No | No | No | No | Yes |

**Best for production**: Use `max_inputs_per_batch=512` rather than the API maximum of 2048 — smaller batches reduce retry cost on failure and fit more comfortably within token limits for long documents. Set `max_concurrent_requests=5` with `requests_per_minute=3000` for OpenAI's text-embedding-3-small tier: this saturates the rate limit without queuing. For multi-million document ingestion jobs, checkpoint completed batch IDs to disk so restarts resume from the last successful batch rather than re-embedding from scratch. Monitor `docs_per_second` in `BatchEmbeddingThroughputDashboard` — a sudden drop indicates rate limiting or API degradation.
