---
title: "Agent Doesn't Implement Async Read-Ahead for Tool Results"
description: "AI agents wait for each tool call to complete before deciding what to fetch next, creating sequential latency chains when predictable follow-up fetches could be prefetched in parallel."
category: performance
difficulty: intermediate
tags: [read-ahead, prefetch, pipeline, latency, asyncio, tool-calls, speculation]
---

# Agent Doesn't Implement Async Read-Ahead for Tool Results

## Problem

Many agent workflows have predictable data access patterns: after reading a document index, the agent always fetches specific documents; after querying a database, it always fetches related records. Without read-ahead, each fetch waits for the previous one to complete. Async read-ahead launches follow-up fetches speculatively while the current result is being processed, hiding fetch latency behind computation time.

## Solution 1: Speculative Prefetch After Index Lookup

When fetching an index that will deterministically lead to specific document fetches, start those fetches immediately.

```python
import asyncio
from typing import Any

async def fetch_document_index(query: str) -> list[str]:
    """Returns list of document IDs relevant to query."""
    await asyncio.sleep(0.2)  # simulate search API
    return ["doc_1", "doc_2", "doc_3", "doc_4", "doc_5"]

async def fetch_document(doc_id: str) -> dict:
    """Fetch a single document by ID."""
    await asyncio.sleep(0.15)  # simulate document fetch
    return {"id": doc_id, "content": f"Content of {doc_id}", "tokens": 500}

async def read_ahead_search(query: str, top_k: int = 3) -> list[dict]:
    """
    Fetch index, then immediately start prefetching all top-k documents
    before processing the index result.
    """
    # Step 1: Fetch index
    index_task = asyncio.create_task(fetch_document_index(query))

    # Step 2: While index is loading, prepare prefetch infrastructure
    prefetch_tasks: dict[str, asyncio.Task] = {}

    doc_ids = await index_task  # wait for index

    # Step 3: Start ALL document fetches simultaneously (read-ahead)
    for doc_id in doc_ids[:top_k]:
        prefetch_tasks[doc_id] = asyncio.create_task(fetch_document(doc_id))

    # Step 4: Collect results (most are already in-flight or done)
    results = []
    for doc_id in doc_ids[:top_k]:
        doc = await prefetch_tasks[doc_id]
        results.append(doc)

    return results

# Without read-ahead: 0.2 + (3 × 0.15) = 0.65s
# With read-ahead:    0.2 + 0.15       = 0.35s  (46% faster)
```

**When to use**: Search-then-fetch patterns. The index response tells you exactly what to fetch next.

---

## Solution 2: Pipeline Stage Read-Ahead with Bounded Buffer

Pipeline multi-stage tool calls so stage N+1 starts as soon as stage N produces its first result.

```python
import asyncio
from typing import AsyncIterator, TypeVar

T = TypeVar("T")

async def pipelined_tool_chain(
    items: list[str],
    stage1_fn,   # e.g., search → [ids]
    stage2_fn,   # e.g., fetch document by id → doc
    stage3_fn,   # e.g., extract summary from doc → summary
    buffer_size: int = 4,
) -> AsyncIterator[Any]:
    """
    Pipeline: stage1 → stage2 → stage3
    Each stage processes items as they arrive from the previous stage,
    without waiting for all items at each stage.
    """
    # Stage 1 → Stage 2 buffer
    s1_to_s2: asyncio.Queue = asyncio.Queue(maxsize=buffer_size)
    # Stage 2 → Stage 3 buffer
    s2_to_s3: asyncio.Queue = asyncio.Queue(maxsize=buffer_size)

    DONE = object()

    async def run_stage1():
        for item in items:
            result = await stage1_fn(item)
            # result may be a list of ids
            if isinstance(result, list):
                for r in result:
                    await s1_to_s2.put(r)
            else:
                await s1_to_s2.put(result)
        await s1_to_s2.put(DONE)

    async def run_stage2():
        while True:
            item = await s1_to_s2.get()
            if item is DONE:
                await s2_to_s3.put(DONE)
                return
            result = await stage2_fn(item)
            await s2_to_s3.put(result)

    async def run_stage3(output_queue: asyncio.Queue):
        while True:
            item = await s2_to_s3.get()
            if item is DONE:
                await output_queue.put(DONE)
                return
            result = await stage3_fn(item)
            await output_queue.put(result)

    output_q: asyncio.Queue = asyncio.Queue()

    # Start all stages concurrently
    asyncio.create_task(run_stage1())
    asyncio.create_task(run_stage2())
    asyncio.create_task(run_stage3(output_q))

    # Yield results as they arrive
    while True:
        result = await output_q.get()
        if result is DONE:
            break
        yield result

# Usage: pipeline search → fetch → summarize
async def demo():
    queries = ["machine learning", "distributed systems", "async python"]

    async def search(q: str) -> list[str]:
        await asyncio.sleep(0.1)
        return [f"{q}_doc1", f"{q}_doc2"]

    async def fetch(doc_id: str) -> dict:
        await asyncio.sleep(0.15)
        return {"id": doc_id, "content": f"content of {doc_id}"}

    async def summarize(doc: dict) -> str:
        await asyncio.sleep(0.05)
        return f"Summary: {doc['id']}"

    results = []
    async for summary in pipelined_tool_chain(queries, search, fetch, summarize):
        results.append(summary)
    print(f"Got {len(results)} summaries")
```

**When to use**: Multi-stage agent pipelines where each stage's output feeds the next. Ideal for RAG pipelines.

---

## Solution 3: Predictive Prefetch Based on Access History

Track which tool calls commonly follow each other; proactively start the predicted follow-up.

```python
import asyncio
import time
from collections import defaultdict, Counter
from typing import Any, Callable, Awaitable

class AccessPatternPredictor:
    """Track tool → follow-up tool pairs; predict and prefetch follow-ups."""

    def __init__(self, top_k: int = 3, min_count: int = 5):
        self._transitions: dict[str, Counter] = defaultdict(Counter)
        self._top_k = top_k
        self._min_count = min_count
        self._last_tool: str | None = None

    def record(self, tool_name: str):
        if self._last_tool:
            self._transitions[self._last_tool][tool_name] += 1
        self._last_tool = tool_name

    def predict_next(self, tool_name: str) -> list[str]:
        """Return top-k predicted follow-up tools."""
        counts = self._transitions.get(tool_name, Counter())
        return [
            t for t, count in counts.most_common(self._top_k)
            if count >= self._min_count
        ]

class PredictivePrefetcher:
    def __init__(self):
        self._predictor = AccessPatternPredictor()
        self._prefetch_cache: dict[str, asyncio.Task] = {}
        self._tool_registry: dict[str, Callable[..., Awaitable[Any]]] = {}

    def register_tool(self, name: str, fn: Callable):
        self._tool_registry[name] = fn

    async def call(self, tool_name: str, **kwargs) -> Any:
        # Check if this was already prefetched
        cache_key = f"{tool_name}:{hash(str(sorted(kwargs.items())))}"
        if cache_key in self._prefetch_cache:
            result = await self._prefetch_cache.pop(cache_key)
            self._predictor.record(tool_name)
            self._start_prefetches(tool_name, kwargs)
            return result

        # Call normally
        fn = self._tool_registry[tool_name]
        result = await fn(**kwargs)
        self._predictor.record(tool_name)

        # Start predicted follow-up prefetches
        self._start_prefetches(tool_name, kwargs)
        return result

    def _start_prefetches(self, current_tool: str, current_kwargs: dict):
        predicted = self._predictor.predict_next(current_tool)
        for next_tool in predicted:
            fn = self._tool_registry.get(next_tool)
            if fn:
                # Prefetch with same kwargs (heuristic; adjust per use case)
                key = f"{next_tool}:{hash(str(sorted(current_kwargs.items())))}"
                if key not in self._prefetch_cache:
                    self._prefetch_cache[key] = asyncio.create_task(fn(**current_kwargs))

prefetcher = PredictivePrefetcher()

# Register tools
async def search_tool(query: str) -> list[str]: await asyncio.sleep(0.1); return ["doc1", "doc2"]
async def fetch_tool(query: str) -> dict: await asyncio.sleep(0.15); return {"docs": ["doc1"]}
async def rank_tool(query: str) -> list: await asyncio.sleep(0.05); return ["doc1"]

prefetcher.register_tool("search", search_tool)
prefetcher.register_tool("fetch", fetch_tool)
prefetcher.register_tool("rank", rank_tool)
```

**When to use**: Agents with stable tool-call patterns. After training the predictor on 100+ sessions, prefetch accuracy typically reaches 70–80%.

---

## Solution 4: Bounded Async Prefetch Window

Maintain a sliding window of N prefetch slots; always keep N requests in-flight for maximum throughput.

```python
import asyncio
import time
from typing import Any, Callable, Awaitable, Iterator

class BoundedPrefetchWindow:
    """
    Maintain a sliding window of in-flight requests.
    Consumer processes results in order; prefetcher stays N ahead.
    """

    def __init__(self, window_size: int = 4):
        self._window = window_size

    async def fetch_all(
        self,
        items: list[Any],
        fetch_fn: Callable[[Any], Awaitable[Any]],
    ) -> list[Any]:
        """Fetch all items with a sliding window of concurrent requests."""
        results: list[Any | None] = [None] * len(items)
        in_flight: dict[int, asyncio.Task] = {}

        idx = 0  # next item to dispatch

        # Prime the window
        while idx < len(items) and idx < self._window:
            in_flight[idx] = asyncio.create_task(fetch_fn(items[idx]))
            idx += 1

        # Process results in order, dispatch new requests as slots open
        for result_idx in range(len(items)):
            # Wait for the result at result_idx
            results[result_idx] = await in_flight.pop(result_idx)

            # Dispatch next item if available (keeps window full)
            if idx < len(items):
                in_flight[idx] = asyncio.create_task(fetch_fn(items[idx]))
                idx += 1

        return results

    async def stream_fetch(
        self,
        items: list[Any],
        fetch_fn: Callable[[Any], Awaitable[Any]],
    ):
        """Stream results as they arrive (out-of-order) within the window."""
        sem = asyncio.Semaphore(self._window)
        queue: asyncio.Queue = asyncio.Queue()

        async def bounded_fetch(item: Any):
            async with sem:
                result = await fetch_fn(item)
                await queue.put(result)

        tasks = [asyncio.create_task(bounded_fetch(item)) for item in items]

        for _ in items:
            yield await queue.get()

        await asyncio.gather(*tasks)

# Usage
fetcher = BoundedPrefetchWindow(window_size=8)

async def fetch_chunk(chunk_id: int) -> dict:
    await asyncio.sleep(0.1)  # simulate tool call
    return {"chunk_id": chunk_id, "data": f"chunk_{chunk_id}"}

async def demo():
    t0 = time.monotonic()
    # 20 sequential fetches without read-ahead: 20 × 0.1 = 2.0s
    # With window=8: ceil(20/8) × 0.1 ≈ 0.3s
    results = await fetcher.fetch_all(list(range(20)), fetch_chunk)
    print(f"20 fetches in {time.monotonic()-t0:.2f}s (window=8)")
    assert len(results) == 20
```

**When to use**: Pagination, chunked document retrieval, batch embedding. Window size = min(API concurrency limit, memory budget).

---

## Solution 5: Async Generator Read-Ahead with Lookahead Buffer

Wrap a slow async generator with a read-ahead buffer so consumers never wait for the next item.

```python
import asyncio
from typing import AsyncIterator, TypeVar

T = TypeVar("T")

async def with_lookahead(
    source: AsyncIterator[T],
    buffer_size: int = 4,
) -> AsyncIterator[T]:
    """
    Buffer `buffer_size` items ahead of consumption.
    Consumer never waits unless buffer is empty (source too slow).
    """
    queue: asyncio.Queue[T | object] = asyncio.Queue(maxsize=buffer_size)
    DONE = object()

    async def producer():
        try:
            async for item in source:
                await queue.put(item)  # blocks if buffer full (backpressure)
        finally:
            await queue.put(DONE)

    producer_task = asyncio.create_task(producer())

    try:
        while True:
            item = await queue.get()
            if item is DONE:
                break
            yield item
    finally:
        producer_task.cancel()
        try:
            await producer_task
        except asyncio.CancelledError:
            pass

# Usage: database cursor with read-ahead
async def slow_db_cursor(query: str) -> AsyncIterator[dict]:
    """Simulates a slow database that returns rows one at a time."""
    for i in range(20):
        await asyncio.sleep(0.05)  # per-row latency
        yield {"row": i, "data": f"row_{i}_data"}

async def process_with_readahead(query: str) -> list[dict]:
    results = []
    async for row in with_lookahead(slow_db_cursor(query), buffer_size=8):
        # Processing happens while next 8 rows are being fetched
        await asyncio.sleep(0.01)  # simulate processing
        results.append(row)
    return results

# Without read-ahead: 20 × (0.05 + 0.01) = 1.2s
# With read-ahead=8:  ~(20/8) × 0.05 + 20 × 0.01 ≈ 0.32s (73% faster)
```

**When to use**: Streaming tool results (database cursors, paginated API responses, vector store results).

---

## Solution 6: Tool Result Cache with Async Prefill

Pre-populate the tool result cache for known upcoming queries while the agent processes the current result.

```python
import asyncio
import time
from typing import Any, Callable, Awaitable

class PrefillCache:
    """Cache that proactively fills entries for anticipated future keys."""

    def __init__(self, ttl_seconds: float = 60.0):
        self._store: dict[str, tuple[float, Any]] = {}
        self._loading: dict[str, asyncio.Task] = {}
        self._ttl = ttl_seconds

    async def get(self, key: str, fetch_fn: Callable[[], Awaitable[Any]]) -> Any:
        """Get from cache, triggering a fresh fetch if stale."""
        cached = self._store.get(key)
        if cached:
            ts, value = cached
            if time.monotonic() - ts < self._ttl:
                return value

        if key in self._loading:
            return await self._loading[key]

        task = asyncio.create_task(fetch_fn())
        self._loading[key] = task
        try:
            value = await task
            self._store[key] = (time.monotonic(), value)
            return value
        finally:
            self._loading.pop(key, None)

    def prefill(self, key: str, fetch_fn: Callable[[], Awaitable[Any]]) -> asyncio.Task:
        """Start background fetch for a key we expect to need soon."""
        if key in self._store:
            ts, _ = self._store[key]
            if time.monotonic() - ts < self._ttl:
                return None  # already fresh
        if key in self._loading:
            return self._loading[key]

        task = asyncio.create_task(self._background_fill(key, fetch_fn))
        self._loading[key] = task
        return task

    async def _background_fill(self, key: str, fetch_fn: Callable[[], Awaitable[Any]]):
        try:
            value = await fetch_fn()
            self._store[key] = (time.monotonic(), value)
            return value
        finally:
            self._loading.pop(key, None)

cache = PrefillCache(ttl_seconds=300)

async def agent_with_prefill(pages: list[str]) -> list[dict]:
    """Process pages sequentially but prefetch the next page while processing current."""
    results = []
    for i, page_id in enumerate(pages):
        # Prefetch the next page now, while we process this one
        if i + 1 < len(pages):
            next_id = pages[i + 1]
            cache.prefill(next_id, lambda nid=next_id: fetch_page(nid))

        # Get current page (likely already cached if prefilled)
        page = await cache.get(page_id, lambda pid=page_id: fetch_page(pid))
        result = await process_page(page)
        results.append(result)

    return results

async def fetch_page(page_id: str) -> dict:
    await asyncio.sleep(0.2)
    return {"id": page_id, "content": f"Content of {page_id}"}

async def process_page(page: dict) -> dict:
    await asyncio.sleep(0.05)
    return {"processed": page["id"]}

# With prefill: process_time + max(fetch_time, process_time) per page
# ≈ 0.05 + 0.05 × N ≈ 10× faster than 0.25 × N for long page lists
```

**When to use**: Sequential workflows where the next item is known before the current one finishes processing.

---

## Comparison

| Solution | Look-ahead Depth | Ordering Preserved | Backpressure | Predictive | Best For |
|---|---|---|---|---|---|
| Speculative post-index prefetch | 1 level | Yes | No | No | Search → fetch workflows |
| Pipeline stage read-ahead | Unbounded | Yes (bounded buffer) | Yes | No | Multi-stage RAG pipelines |
| Pattern-based predictor | 1 level | N/A | No | Yes | Stable access-pattern agents |
| Bounded window | N items | Yes | Yes | No | Paginated / chunked fetches |
| Async generator lookahead | Buffer size | Yes | Yes | No | Streaming cursor results |
| Prefill cache | 1 ahead | Yes | No | Partial | Sequential page processing |

**Rule of thumb**: Always prefetch the next page/document while processing the current one — this alone cuts sequential I/O latency by 50–80% for typical agent workflows.
