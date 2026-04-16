---
title: "Agent Doesn't Implement Batch Embedding for Bulk Retrieval Operations"
description: "Embed documents and queries in batches rather than one-by-one to reduce API round-trips, cut latency by 10-50x, and lower embedding costs for bulk retrieval workloads."
difficulty: intermediate
category: performance
tags: [performance, embeddings, batching, retrieval, rag, cost-optimization]
---

## Problem

Agents generate embeddings one document at a time inside loops—one API call per chunk. For 1000 documents, that's 1000 sequential round-trips averaging 50-200ms each, resulting in minutes of embedding time instead of seconds. Embedding APIs support batching, but agents don't use it, leaving the largest performance gain on the table.

## Solutions

### Option 1: Simple Batch Embedding with Size Control

Chunk documents into fixed-size batches and embed all batches concurrently.

```python
import asyncio
import httpx
import os
from dataclasses import dataclass

@dataclass
class EmbeddingResult:
    text: str
    embedding: list[float]
    index: int

async def embed_batch(
    texts: list[str],
    model: str = "text-embedding-3-small",
    batch_size: int = 100
) -> list[EmbeddingResult]:
    """Embed texts in batches, respecting API batch size limits."""
    api_key = os.environ.get("OPENAI_API_KEY", "")  # Or use any embedding provider
    results: list[EmbeddingResult] = []

    # Split into batches
    batches = [texts[i:i + batch_size] for i in range(0, len(texts), batch_size)]

    async with httpx.AsyncClient() as http:
        for batch_idx, batch in enumerate(batches):
            response = await http.post(
                "https://api.openai.com/v1/embeddings",
                headers={"Authorization": f"Bearer {api_key}"},
                json={"input": batch, "model": model},
                timeout=30.0,
            )
            response.raise_for_status()
            data = response.json()

            for item in data["data"]:
                global_idx = batch_idx * batch_size + item["index"]
                results.append(EmbeddingResult(
                    text=texts[global_idx],
                    embedding=item["embedding"],
                    index=global_idx,
                ))

    # Sort by original index
    results.sort(key=lambda r: r.index)
    return results

# --- Comparison: naive vs batch ---

async def naive_embed_one_by_one(texts: list[str]) -> list[list[float]]:
    """BAD: One API call per text."""
    import time
    api_key = os.environ.get("OPENAI_API_KEY", "")
    embeddings = []
    async with httpx.AsyncClient() as http:
        for text in texts:
            r = await http.post(
                "https://api.openai.com/v1/embeddings",
                headers={"Authorization": f"Bearer {api_key}"},
                json={"input": [text], "model": "text-embedding-3-small"},
                timeout=10.0
            )
            embeddings.append(r.json()["data"][0]["embedding"])
    return embeddings

async def demo():
    import time
    documents = [f"Document {i}: This describes concept {i % 10}." for i in range(50)]

    # Batch approach
    start = time.monotonic()
    results = await embed_batch(documents, batch_size=50)
    batch_time = time.monotonic() - start
    print(f"Batch embedding: {len(results)} docs in {batch_time:.2f}s")
    print(f"  ({batch_time / len(documents) * 1000:.1f}ms per doc)")

# asyncio.run(demo())
print("Batch embedding configured. Replace OpenAI key to run.")
```

### Option 2: Concurrent Batch Embedding with Rate Limiting

Embed multiple batches concurrently while respecting API rate limits.

```python
import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

@dataclass
class BatchEmbedder:
    """Concurrent batch embedder with rate limiting."""
    max_concurrent_batches: int = 5
    batch_size: int = 100
    requests_per_minute: int = 500

    _semaphore: asyncio.Semaphore = field(init=False)
    _request_times: list[float] = field(default_factory=list)

    def __post_init__(self):
        self._semaphore = asyncio.Semaphore(self.max_concurrent_batches)

    async def _rate_limit(self):
        """Simple sliding window rate limiter."""
        now = time.monotonic()
        window = 60.0
        self._request_times = [t for t in self._request_times if now - t < window]
        if len(self._request_times) >= self.requests_per_minute:
            sleep_until = self._request_times[0] + window
            await asyncio.sleep(max(0, sleep_until - now))
        self._request_times.append(time.monotonic())

    async def _embed_single_batch(self, batch: list[str], batch_idx: int) -> list[dict]:
        """Embed one batch — replace with actual API call."""
        async with self._semaphore:
            await self._rate_limit()
            await asyncio.sleep(0.05)  # Simulate API latency

            # Simulate embedding response
            return [
                {
                    "index": batch_idx * self.batch_size + i,
                    "embedding": [0.1 * (i % 10)] * 384,  # Mock 384-dim embedding
                    "text": text,
                }
                for i, text in enumerate(batch)
            ]

    async def embed_all(self, texts: list[str]) -> list[dict]:
        """Embed all texts using concurrent batches."""
        batches = [
            texts[i:i + self.batch_size]
            for i in range(0, len(texts), self.batch_size)
        ]

        tasks = [
            self._embed_single_batch(batch, idx)
            for idx, batch in enumerate(batches)
        ]

        batch_results = await asyncio.gather(*tasks)

        # Flatten and sort
        all_results = [item for batch in batch_results for item in batch]
        all_results.sort(key=lambda x: x["index"])
        return all_results

async def demo_concurrent_batching():
    import time

    embedder = BatchEmbedder(
        max_concurrent_batches=5,
        batch_size=50,
        requests_per_minute=300,
    )

    documents = [f"Article {i}: AI systems need careful evaluation." for i in range(500)]

    start = time.monotonic()
    results = await embedder.embed_all(documents)
    elapsed = time.monotonic() - start

    print(f"Embedded {len(results)} documents in {elapsed:.2f}s")
    print(f"  Throughput: {len(results) / elapsed:.0f} docs/sec")
    print(f"  Per-doc latency: {elapsed / len(results) * 1000:.1f}ms")
    print(f"  Batches used: {len(documents) // embedder.batch_size + 1}")

asyncio.run(demo_concurrent_batching())
```

### Option 3: Adaptive Batch Sizing Based on Token Length

Automatically size batches to stay within token limits rather than using fixed counts.

```python
import asyncio
from dataclasses import dataclass

MAX_TOKENS_PER_BATCH = 8192  # Typical embedding API limit

def estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 chars per token."""
    return max(1, len(text) // 4)

def adaptive_batches(texts: list[str], max_tokens: int = MAX_TOKENS_PER_BATCH) -> list[list[str]]:
    """Group texts into batches that fit within token budget."""
    batches: list[list[str]] = []
    current_batch: list[str] = []
    current_tokens = 0

    for text in texts:
        tokens = estimate_tokens(text)

        if tokens > max_tokens:
            # Single oversized text: truncate and embed alone
            truncated = text[:max_tokens * 4]
            batches.append([truncated])
            continue

        if current_tokens + tokens > max_tokens and current_batch:
            batches.append(current_batch)
            current_batch = []
            current_tokens = 0

        current_batch.append(text)
        current_tokens += tokens

    if current_batch:
        batches.append(current_batch)

    return batches

@dataclass
class AdaptiveBatchEmbedder:
    max_tokens_per_batch: int = 8192
    max_concurrent: int = 10

    async def _call_embedding_api(self, batch: list[str]) -> list[list[float]]:
        """Simulate embedding API call."""
        await asyncio.sleep(0.03)
        return [[0.1] * 384 for _ in batch]

    async def embed(self, texts: list[str]) -> list[list[float]]:
        batches = adaptive_batches(texts, self.max_tokens_per_batch)

        sem = asyncio.Semaphore(self.max_concurrent)

        async def embed_batch(batch: list[str], start_idx: int) -> tuple[int, list[list[float]]]:
            async with sem:
                embeddings = await self._call_embedding_api(batch)
                return start_idx, embeddings

        # Track start indices
        tasks = []
        start = 0
        for batch in batches:
            tasks.append(embed_batch(batch, start))
            start += len(batch)

        results = await asyncio.gather(*tasks)
        results.sort(key=lambda x: x[0])

        all_embeddings: list[list[float]] = []
        for _, embeddings in results:
            all_embeddings.extend(embeddings)

        return all_embeddings

async def demo_adaptive_batching():
    import time

    embedder = AdaptiveBatchEmbedder(max_tokens_per_batch=512, max_concurrent=8)

    # Mix of short and long documents
    texts = (
        ["Short doc." for _ in range(100)] +
        ["Medium length document with more content. " * 20 for _ in range(50)] +
        ["Very long document. " * 100 for _ in range(10)]
    )

    batches = adaptive_batches(texts, max_tokens=512)
    print(f"Documents: {len(texts)}")
    print(f"Adaptive batches: {len(batches)}")
    print(f"Avg batch size: {len(texts) / len(batches):.1f} docs")

    start = time.monotonic()
    embeddings = await embedder.embed(texts)
    elapsed = time.monotonic() - start

    print(f"Embedded in {elapsed:.2f}s → {len(embeddings)} vectors")

asyncio.run(demo_adaptive_batching())
```

### Option 4: Streaming Batch Embedder with Progress Reporting

Embed large corpora with real-time progress callbacks, useful for long-running indexing jobs.

```python
import asyncio
import time
from dataclasses import dataclass, field
from typing import Callable, Awaitable

ProgressCallback = Callable[[int, int, float], Awaitable[None]]

@dataclass
class StreamingBatchEmbedder:
    batch_size: int = 100
    max_concurrent: int = 5
    on_progress: ProgressCallback | None = None

    async def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        await asyncio.sleep(0.05)  # Simulate API
        return [[0.1] * 768 for _ in texts]

    async def embed_stream(self, texts: list[str]) -> list[list[float]]:
        total = len(texts)
        batches = [texts[i:i + self.batch_size] for i in range(0, total, self.batch_size)]

        results: list[tuple[int, list[list[float]]]] = []
        completed = 0
        start = time.monotonic()
        sem = asyncio.Semaphore(self.max_concurrent)
        lock = asyncio.Lock()

        async def process_batch(batch: list[str], batch_start: int):
            nonlocal completed
            async with sem:
                embeddings = await self._embed_batch(batch)
                async with lock:
                    results.append((batch_start, embeddings))
                    completed += len(batch)
                    if self.on_progress:
                        elapsed = time.monotonic() - start
                        await self.on_progress(completed, total, elapsed)

        tasks = [
            process_batch(batch, i * self.batch_size)
            for i, batch in enumerate(batches)
        ]
        await asyncio.gather(*tasks)

        results.sort(key=lambda x: x[0])
        all_embeddings: list[list[float]] = []
        for _, embs in results:
            all_embeddings.extend(embs)
        return all_embeddings

async def demo_streaming_progress():
    async def progress_cb(done: int, total: int, elapsed: float):
        pct = done / total * 100
        rate = done / elapsed if elapsed > 0 else 0
        eta = (total - done) / rate if rate > 0 else 0
        print(f"  Progress: {done}/{total} ({pct:.0f}%) | "
              f"{rate:.0f} docs/s | ETA {eta:.1f}s")

    embedder = StreamingBatchEmbedder(
        batch_size=50,
        max_concurrent=4,
        on_progress=progress_cb,
    )

    documents = [f"Document {i}" for i in range(500)]

    print(f"Embedding {len(documents)} documents...")
    start = time.monotonic()
    embeddings = await embedder.embed_stream(documents)
    total_time = time.monotonic() - start

    print(f"\nDone: {len(embeddings)} embeddings in {total_time:.2f}s")

asyncio.run(demo_streaming_progress())
```

### Option 5: Cache-Aware Batch Embedder

Skip re-embedding texts that have been embedded before, using content hashing as the cache key.

```python
import asyncio
import hashlib
import json
from pathlib import Path
from dataclasses import dataclass

CACHE_FILE = Path(".embedding_cache.json")

def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()

@dataclass
class CachedBatchEmbedder:
    batch_size: int = 100
    cache_file: Path = CACHE_FILE
    _cache: dict[str, list[float]] = None  # type: ignore

    def __post_init__(self):
        self._cache = self._load_cache()

    def _load_cache(self) -> dict[str, list[float]]:
        if self.cache_file.exists():
            return json.loads(self.cache_file.read_text())
        return {}

    def _save_cache(self):
        self.cache_file.write_text(json.dumps(self._cache))

    async def _embed_batch_api(self, texts: list[str]) -> list[list[float]]:
        await asyncio.sleep(0.05)
        return [[0.1 * (i % 10)] * 384 for i, _ in enumerate(texts)]

    async def embed(self, texts: list[str]) -> list[list[float]]:
        hashes = [content_hash(t) for t in texts]

        # Separate cached from uncached
        uncached_indices = [i for i, h in enumerate(hashes) if h not in self._cache]
        uncached_texts = [texts[i] for i in uncached_indices]

        cache_hits = len(texts) - len(uncached_texts)
        print(f"Cache: {cache_hits} hits, {len(uncached_texts)} misses "
              f"({cache_hits/len(texts)*100:.0f}% hit rate)")

        # Embed only uncached texts in batches
        if uncached_texts:
            batches = [
                uncached_texts[i:i + self.batch_size]
                for i in range(0, len(uncached_texts), self.batch_size)
            ]
            new_embeddings: list[list[float]] = []
            for batch in batches:
                batch_embs = await self._embed_batch_api(batch)
                new_embeddings.extend(batch_embs)

            # Store in cache
            for i, idx in enumerate(uncached_indices):
                self._cache[hashes[idx]] = new_embeddings[i]
            self._save_cache()

        # Return all embeddings in original order
        return [self._cache[h] for h in hashes]

async def demo_cached_batching():
    embedder = CachedBatchEmbedder(batch_size=50)

    docs = [f"Concept {i % 20}: AI agents need reliable infrastructure." for i in range(200)]

    print("First run (cold cache):")
    embeddings1 = await embedder.embed(docs)

    print("\nSecond run (warm cache):")
    embeddings2 = await embedder.embed(docs)

    print(f"\nEmbedding count: {len(embeddings1)}")
    print(f"Cache entries: {len(embedder._cache)}")
    print(f"Results match: {embeddings1 == embeddings2}")

asyncio.run(demo_cached_batching())
```

### Option 6: Query-Side Batch Embedding for Multi-Query Retrieval

Embed multiple search queries in a single API call before parallel vector search.

```python
import asyncio
import time
from dataclasses import dataclass
import random

@dataclass
class MultiQueryRetriever:
    """Embed all queries in one batch, then retrieve in parallel."""
    top_k: int = 5
    embedding_dim: int = 384

    async def _batch_embed_queries(self, queries: list[str]) -> list[list[float]]:
        """Single API call for all queries."""
        await asyncio.sleep(0.04)  # One round-trip regardless of query count
        return [
            [random.uniform(-1, 1) for _ in range(self.embedding_dim)]
            for _ in queries
        ]

    async def _vector_search(
        self, query_embedding: list[float], query_idx: int
    ) -> list[dict]:
        """Search vector store with one embedding."""
        await asyncio.sleep(0.02)  # Simulate vector DB query
        return [
            {"id": f"doc_{query_idx}_{i}", "score": random.uniform(0.7, 1.0)}
            for i in range(self.top_k)
        ]

    async def retrieve_for_queries(self, queries: list[str]) -> list[list[dict]]:
        """Full pipeline: batch embed → parallel search."""
        # Step 1: ONE batch embedding call for all queries
        start_embed = time.monotonic()
        embeddings = await self._batch_embed_queries(queries)
        embed_time = (time.monotonic() - start_embed) * 1000

        # Step 2: Parallel vector searches
        start_search = time.monotonic()
        search_tasks = [
            self._vector_search(emb, idx)
            for idx, emb in enumerate(embeddings)
        ]
        results = await asyncio.gather(*search_tasks)
        search_time = (time.monotonic() - start_search) * 1000

        print(f"  Batch embedded {len(queries)} queries in {embed_time:.0f}ms "
              f"(1 API call)")
        print(f"  Parallel search completed in {search_time:.0f}ms")
        return list(results)

async def demo_multi_query_retrieval():
    retriever = MultiQueryRetriever(top_k=3)

    # Scenario: RAG with query expansion (1 original + 4 variants)
    original_query = "How do I handle rate limiting in async Python?"
    expanded_queries = [
        original_query,
        "asyncio rate limiter implementation Python",
        "token bucket algorithm Python async",
        "backpressure handling aiohttp rate limit",
        "Python async API throttling best practices",
    ]

    print(f"Retrieving for {len(expanded_queries)} queries (1 batch embed call):")
    all_results = await retriever.retrieve_for_queries(expanded_queries)

    # Merge and deduplicate results
    seen = set()
    merged = []
    for results in all_results:
        for r in results:
            if r["id"] not in seen:
                seen.add(r["id"])
                merged.append(r)

    merged.sort(key=lambda x: x["score"], reverse=True)
    print(f"\nMerged {len(merged)} unique results across {len(expanded_queries)} queries")
    for r in merged[:5]:
        print(f"  {r['id']}: score={r['score']:.3f}")

asyncio.run(demo_multi_query_retrieval())
```

## Comparison

| Approach | API Calls | Throughput | Complexity | Best For |
|---|---|---|---|---|
| Simple Batch with Size Control | N/batch_size | 10-50x vs naive | Low | Basic bulk indexing |
| Concurrent Batch Embedding | N/batch_size (parallel) | 50-200x vs naive | Medium | High-volume indexing |
| Adaptive Token-Based Batching | Optimal | 10-50x | Medium | Variable-length documents |
| Streaming with Progress | Concurrent | 50-200x | Medium | Long-running indexing jobs |
| Cache-Aware Batching | Only misses | Up to ∞ for cached | Medium | Repeated/overlapping corpora |
| Multi-Query Batch Embedding | 1 for all queries | N/A (latency win) | Low | RAG query expansion |

**Choose Concurrent Batch Embedding** as the default for any bulk indexing workload—it's the single highest-impact optimization. **Choose Cache-Aware Batching** when your corpus changes incrementally (only new/modified documents need re-embedding). **Choose Multi-Query Batch Embedding** in RAG pipelines that use query expansion to ensure you pay for one embedding call regardless of how many query variants you generate.
