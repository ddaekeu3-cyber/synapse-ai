---
title: "Agent Doesn't Implement Batch Embedding Requests for Multiple Documents"
description: "Agents that embed documents one at a time with individual API calls pay per-request overhead — HTTP connection setup, authentication, and queuing latency — multiplied by the number of documents. Embedding APIs support batch requests that process N documents in a single call. Implement batch embedding that groups documents into optimally-sized batches, respects token and count limits, and reduces total embedding latency and cost."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-batch-embedding-requests-for-multiple-documents
tags: [batch-embedding, embedding-efficiency, api-batching, token-optimization, vector-embedding, throughput]
symptoms:
  - "Embedding 100 documents takes 100 API calls instead of a handful of batch requests"
  - "Embedding pipeline is the bottleneck because each document is a separate HTTP round-trip"
  - "Rate limits are hit on request count rather than token count due to excessive individual calls"
  - "No awareness of per-batch token limits — oversized documents crash the embedding call"
  - "Total embedding cost is higher than necessary due to per-request overhead charges"
---

## Why This Happens

Most agent embedding pipelines call the embedding API once per document because that is the simplest implementation: `for doc in documents: embed(doc)`. Embedding APIs expose batch endpoints that accept arrays of inputs — OpenAI's `embeddings` endpoint accepts up to 2048 inputs per request and processes them in parallel server-side. The per-call overhead (TLS, routing, queue admission) is paid once per batch regardless of batch size. Batching requires awareness of two limits: the maximum number of inputs per request and the maximum total tokens per request. Documents must be grouped such that neither limit is exceeded, and oversized individual documents must be truncated or split.

## Solution 1: Embedding Batch Planner

```python
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class EmbeddingDocument:
    doc_id: str
    text: str
    token_count: Optional[int] = None
    metadata: dict = field(default_factory=dict)


@dataclass
class EmbeddingBatchLimits:
    max_inputs_per_batch: int = 512
    max_tokens_per_batch: int = 100_000
    max_chars_per_document: int = 8000    # ~2000 tokens at 4 chars/token
    tokens_per_char_estimate: float = 0.25


class EmbeddingBatchPlanner:
    """
    Groups documents into batches respecting both input-count and
    token-count limits. Documents exceeding max_chars are truncated.
    """

    def __init__(self, limits: EmbeddingBatchLimits):
        self._limits = limits

    def _estimate_tokens(self, text: str) -> int:
        return max(1, int(len(text) * self._limits.tokens_per_char_estimate))

    def _truncate(self, doc: EmbeddingDocument) -> EmbeddingDocument:
        if len(doc.text) <= self._limits.max_chars_per_document:
            return doc
        return EmbeddingDocument(
            doc_id=doc.doc_id,
            text=doc.text[: self._limits.max_chars_per_document],
            metadata={**doc.metadata, "truncated": True},
        )

    def plan(self, documents: List[EmbeddingDocument]) -> List[List[EmbeddingDocument]]:
        batches: List[List[EmbeddingDocument]] = []
        current_batch: List[EmbeddingDocument] = []
        current_tokens = 0

        for doc in documents:
            doc = self._truncate(doc)
            doc_tokens = self._estimate_tokens(doc.text)

            if (
                len(current_batch) >= self._limits.max_inputs_per_batch
                or current_tokens + doc_tokens > self._limits.max_tokens_per_batch
            ):
                if current_batch:
                    batches.append(current_batch)
                current_batch = []
                current_tokens = 0

            current_batch.append(doc)
            current_tokens += doc_tokens

        if current_batch:
            batches.append(current_batch)

        return batches
```

## Solution 2: Batch Embedding Caller

```python
import asyncio
from typing import Any, Callable, Dict, List


class BatchEmbeddingCaller:
    """
    Calls the embedding API with a batch of documents and returns
    a mapping of doc_id → embedding vector.
    Handles API errors per batch without failing the entire job.
    """

    def __init__(
        self,
        embed_fn: Callable[[List[str]], List[Any]],
        concurrency: int = 4,
    ):
        # embed_fn(texts) -> list of embedding vectors, same order as input
        self._embed_fn = embed_fn
        self._semaphore = asyncio.Semaphore(concurrency)
        self._batches_called = 0
        self._documents_embedded = 0
        self._batch_errors = 0

    async def embed_batch(
        self, batch: List[EmbeddingDocument]
    ) -> Dict[str, Any]:
        texts = [doc.text for doc in batch]
        async with self._semaphore:
            try:
                vectors = await self._embed_fn(texts)
                self._batches_called += 1
                self._documents_embedded += len(batch)
                return {doc.doc_id: vec for doc, vec in zip(batch, vectors)}
            except Exception as exc:
                self._batch_errors += 1
                raise RuntimeError(
                    f"Embedding batch of {len(batch)} documents failed: {exc}"
                ) from exc

    def stats(self) -> dict:
        return {
            "batches_called": self._batches_called,
            "documents_embedded": self._documents_embedded,
            "batch_errors": self._batch_errors,
        }
```

## Solution 3: Parallel Batch Embedding Pipeline

```python
import asyncio
import time
from typing import Any, Dict, List, Optional


class ParallelBatchEmbeddingPipeline:
    """
    Plans batches and embeds them in parallel up to the configured
    concurrency limit. Returns all embeddings and a timing report.
    """

    def __init__(
        self,
        planner: EmbeddingBatchPlanner,
        caller: BatchEmbeddingCaller,
    ):
        self._planner = planner
        self._caller = caller

    async def embed_all(
        self,
        documents: List[EmbeddingDocument],
    ) -> dict:
        start = time.time()
        batches = self._planner.plan(documents)

        tasks = [self._caller.embed_batch(batch) for batch in batches]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        merged: Dict[str, Any] = {}
        errors = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                errors.append({"batch_index": i, "error": str(result)})
            else:
                merged.update(result)

        elapsed_ms = round((time.time() - start) * 1000, 2)
        return {
            "embeddings": merged,
            "total_documents": len(documents),
            "embedded_count": len(merged),
            "batch_count": len(batches),
            "failed_batches": len(errors),
            "errors": errors,
            "elapsed_ms": elapsed_ms,
            "docs_per_second": round(len(merged) / max(elapsed_ms / 1000, 0.001), 1),
        }
```

## Solution 4: Embedding Cache Layer

```python
import hashlib
import time
from typing import Any, Dict, List, Optional, Tuple


class EmbeddingCacheLayer:
    """
    Caches embeddings by content hash so unchanged documents are not
    re-embedded on repeated pipeline runs. Returns cache hits immediately
    and only passes cache misses to the embedding pipeline.
    """

    def __init__(self, ttl_seconds: float = 3600.0, max_entries: int = 50000):
        self._ttl = ttl_seconds
        self._max = max_entries
        self._store: Dict[str, Tuple[Any, float]] = {}
        self._hits = 0
        self._misses = 0

    def _key(self, text: str) -> str:
        return hashlib.sha256(text.encode()).hexdigest()[:32]

    def get(self, text: str) -> Optional[Any]:
        key = self._key(text)
        entry = self._store.get(key)
        if entry is None:
            self._misses += 1
            return None
        vector, expires_at = entry
        if time.time() > expires_at:
            del self._store[key]
            self._misses += 1
            return None
        self._hits += 1
        return vector

    def set(self, text: str, vector: Any) -> None:
        if len(self._store) >= self._max:
            oldest = min(self._store, key=lambda k: self._store[k][1])
            del self._store[oldest]
        self._store[self._key(text)] = (vector, time.time() + self._ttl)

    def filter_uncached(
        self, documents: List[EmbeddingDocument]
    ) -> Tuple[Dict[str, Any], List[EmbeddingDocument]]:
        cached: Dict[str, Any] = {}
        uncached: List[EmbeddingDocument] = []
        for doc in documents:
            vec = self.get(doc.text)
            if vec is not None:
                cached[doc.doc_id] = vec
            else:
                uncached.append(doc)
        return cached, uncached

    def store_results(self, embeddings: Dict[str, Any], documents: List[EmbeddingDocument]) -> None:
        text_by_id = {doc.doc_id: doc.text for doc in documents}
        for doc_id, vector in embeddings.items():
            if doc_id in text_by_id:
                self.set(text_by_id[doc_id], vector)

    def stats(self) -> dict:
        return {
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": round(self._hits / max(self._hits + self._misses, 1), 4),
            "cached_entries": len(self._store),
        }
```

## Solution 5: Cache-Aware Batch Embedding Executor

```python
import time
from typing import Any, Dict, List


class CacheAwareBatchEmbeddingExecutor:
    """
    Combines the cache layer with the parallel batch pipeline.
    Serves cached embeddings immediately and batches only the misses.
    """

    def __init__(
        self,
        cache: EmbeddingCacheLayer,
        pipeline: ParallelBatchEmbeddingPipeline,
    ):
        self._cache = cache
        self._pipeline = pipeline

    async def embed(self, documents: List[EmbeddingDocument]) -> dict:
        start = time.time()
        cached_embeddings, uncached_docs = self._cache.filter_uncached(documents)

        pipeline_result = {"embeddings": {}, "batch_count": 0, "elapsed_ms": 0}
        if uncached_docs:
            pipeline_result = await self._pipeline.embed_all(uncached_docs)
            self._cache.store_results(pipeline_result["embeddings"], uncached_docs)

        all_embeddings = {**cached_embeddings, **pipeline_result["embeddings"]}
        return {
            "embeddings": all_embeddings,
            "total_documents": len(documents),
            "cache_hits": len(cached_embeddings),
            "api_calls_made": pipeline_result["batch_count"],
            "elapsed_ms": round((time.time() - start) * 1000, 2),
            "cache_stats": self._cache.stats(),
        }
```

## Solution 6: Batch Embedding Dashboard

```python
import time


class BatchEmbeddingDashboard:
    """
    Reports pipeline efficiency, cache performance, and per-run metrics.
    """

    def __init__(
        self,
        executor: CacheAwareBatchEmbeddingExecutor,
        caller: BatchEmbeddingCaller,
        cache: EmbeddingCacheLayer,
    ):
        self._executor = executor
        self._caller = caller
        self._cache = cache

    def render(self) -> dict:
        caller_stats = self._caller.stats()
        cache_stats = self._cache.stats()
        return {
            "generated_at": time.time(),
            "api_usage": caller_stats,
            "cache": cache_stats,
            "efficiency": {
                "batch_error_rate": round(
                    caller_stats["batch_errors"] / max(caller_stats["batches_called"], 1), 4
                ),
                "cache_hit_rate": cache_stats["hit_rate"],
                "avg_batch_size": round(
                    caller_stats["documents_embedded"] / max(caller_stats["batches_called"], 1), 1
                ),
            },
        }
```

## Comparison

| Approach | Batch Planning | Parallel Dispatch | Content Cache | Per-Doc Truncation | Dashboard |
|---|---|---|---|---|---|
| EmbeddingBatchPlanner | Yes (token + count limits) | No | No | Yes | No |
| BatchEmbeddingCaller | No | Yes (semaphore) | No | No | No |
| ParallelBatchEmbeddingPipeline | Via planner | Via caller | No | Via planner | No |
| EmbeddingCacheLayer | No | No | Yes (SHA-256 + TTL) | No | No |
| CacheAwareBatchEmbeddingExecutor | Via pipeline | Via pipeline | Via cache | No | No |
| BatchEmbeddingDashboard | No | No | No | No | Yes |

**Best for production**: Set `max_inputs_per_batch=512` for OpenAI's text-embedding-3 models and `max_tokens_per_batch=300_000` — these are the documented limits. Use `concurrency=4` for the caller to stay well under rate limits while maintaining throughput. Monitor `avg_batch_size`: if consistently below 10, documents are arriving too slowly to benefit from batching and a queue-based collector that accumulates documents for 100 ms before dispatching will improve efficiency. Set `ttl_seconds=3600` in the cache — embeddings for the same text are deterministic, so the TTL is conservative; increase to 86400 for static document corpora.
