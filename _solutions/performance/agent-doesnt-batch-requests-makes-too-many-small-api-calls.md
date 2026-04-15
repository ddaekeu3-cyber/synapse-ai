---
layout: solution
title: "Agent Makes Too Many Small API Calls Instead of Batching"
category: performance
description: "The agent processes 1,000 records and makes 1,000 individual API calls — one per record. It embeds documents one at a time when the API accepts 100 at once. It calls Claude once per sentence to classify it, instead of classifying a batch of 20 sentences per call. Latency is N×single_call_latency instead of ⌈N/batch_size⌉×single_call_latency."
tags: [performance, batching, throughput, embeddings, classification, bulk-api, parallelism, cost-optimization]
---

# Agent Makes Too Many Small API Calls Instead of Batching

## Symptom
- Processing 500 items takes 10 minutes because it makes 500 serial API calls
- Embeddings computed one document at a time instead of in batches
- Claude called once per row of a CSV instead of once for a chunk of rows
- API costs are high despite low output volume (overhead dominates)
- Request rate is well under the rate limit but throughput is still low
- Network round-trip latency multiplied by N items is the bottleneck, not compute

## Root Cause
Most AI APIs — embeddings, classification, extraction — accept batched inputs. Calling them one item at a time wastes time on network round-trips (typically 50–200ms each) that could be amortized over a batch. For Claude, a single prompt with 20 classification items is cheaper (in tokens and latency) than 20 separate prompts. The fix is to group items into batches, process each batch in one call, and run multiple batches in parallel.

## Fix

### Option 1: Batch embeddings — use the API's native batch input

```python
import anthropic
import math
import asyncio
from typing import Callable, Any

client = anthropic.Anthropic()

# WRONG — one API call per document:
def embed_documents_one_by_one(documents: list[str]) -> list[list[float]]:
    embeddings = []
    for doc in documents:
        # Pseudo-code — replace with your actual embedding API:
        response = client.embeddings.create(model="voyage-3", input=doc)
        embeddings.append(response.data[0].embedding)
    return embeddings
# For 1000 docs: 1000 round trips × 100ms = 100 seconds

# RIGHT — batch API call, all documents in one request:
def embed_documents_batched(
    documents: list[str],
    batch_size: int = 96  # Voyage-3 accepts up to 128 per batch
) -> list[list[float]]:
    all_embeddings = []
    for i in range(0, len(documents), batch_size):
        batch = documents[i:i + batch_size]
        # Replace with actual embedding API:
        import voyageai
        vo = voyageai.Client()
        result = vo.embed(batch, model="voyage-3", input_type="document")
        all_embeddings.extend(result.embeddings)
    return all_embeddings
# For 1000 docs: ⌈1000/96⌉ = 11 round trips × 100ms = 1.1 seconds

# With concurrent batches — even faster:
async def embed_documents_concurrent(
    documents: list[str],
    batch_size: int = 96,
    max_concurrent: int = 5
) -> list[list[float]]:
    """
    Split into batches, run up to max_concurrent in parallel.
    """
    batches = [documents[i:i + batch_size] for i in range(0, len(documents), batch_size)]
    semaphore = asyncio.Semaphore(max_concurrent)
    results = [None] * len(batches)

    async def embed_batch(idx: int, batch: list[str]):
        async with semaphore:
            import voyageai
            vo = voyageai.AsyncClient()
            result = await vo.embed(batch, model="voyage-3", input_type="document")
            results[idx] = result.embeddings

    await asyncio.gather(*[embed_batch(i, b) for i, b in enumerate(batches)])

    # Flatten results in order:
    return [emb for batch_result in results for emb in batch_result]
```

### Option 2: Batch Claude classification — N items per prompt instead of 1

```python
import anthropic
import json
import asyncio
from typing import Any

client = anthropic.Anthropic()

# WRONG — one Claude call per item:
def classify_items_one_by_one(items: list[str], categories: list[str]) -> list[str]:
    results = []
    for item in items:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",  # Use Haiku for classification
            max_tokens=20,
            messages=[{
                "role": "user",
                "content": f"Classify into one of {categories}: {item}"
            }]
        )
        results.append(response.content[0].text.strip())
    return results
# For 500 items: 500 calls × 200ms = 100 seconds, 500× overhead

# RIGHT — batch N items per call:
def classify_items_batched(
    items: list[str],
    categories: list[str],
    batch_size: int = 20
) -> list[str]:
    """
    Classify items in batches of batch_size using a single Claude call per batch.
    Returns classifications in the same order as input items.
    """
    all_results = []
    categories_str = ", ".join(categories)

    for i in range(0, len(items), batch_size):
        batch = items[i:i + batch_size]
        numbered = "\n".join(f"{j+1}. {item}" for j, item in enumerate(batch))

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=batch_size * 15,  # ~15 tokens per classification
            messages=[{
                "role": "user",
                "content": (
                    f"Classify each item into exactly one of these categories: {categories_str}\n\n"
                    f"Items:\n{numbered}\n\n"
                    f"Reply with a JSON array of {len(batch)} strings, one classification per item, "
                    f"in the same order. Example: [\"category1\", \"category2\"]"
                )
            }]
        )

        try:
            batch_results = json.loads(response.content[0].text.strip())
            all_results.extend(batch_results[:len(batch)])
        except (json.JSONDecodeError, IndexError):
            # Fallback: repeat "unknown" for malformed response
            all_results.extend(["unknown"] * len(batch))

    return all_results
# For 500 items: ⌈500/20⌉ = 25 calls × 200ms = 5 seconds, 20× fewer calls

# With parallel batches:
async def classify_items_parallel(
    items: list[str],
    categories: list[str],
    batch_size: int = 20,
    max_concurrent: int = 5
) -> list[str]:
    async_client = anthropic.AsyncAnthropic()
    categories_str = ", ".join(categories)
    batches = [items[i:i + batch_size] for i in range(0, len(items), batch_size)]
    semaphore = asyncio.Semaphore(max_concurrent)
    results = [None] * len(batches)

    async def classify_batch(idx: int, batch: list[str]):
        async with semaphore:
            numbered = "\n".join(f"{j+1}. {item}" for j, item in enumerate(batch))
            response = await async_client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=batch_size * 15,
                messages=[{
                    "role": "user",
                    "content": (
                        f"Classify each into one of: {categories_str}\n\n"
                        f"Items:\n{numbered}\n\n"
                        f"Reply with JSON array of {len(batch)} strings."
                    )
                }]
            )
            try:
                results[idx] = json.loads(response.content[0].text.strip())
            except json.JSONDecodeError:
                results[idx] = ["unknown"] * len(batch)

    await asyncio.gather(*[classify_batch(i, b) for i, b in enumerate(batches)])
    return [label for batch_result in results for label in batch_result]
```

### Option 3: Anthropic Batch API — async bulk processing at 50% cost

```python
import anthropic
import time
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)
client = anthropic.Anthropic()

def submit_batch_job(
    prompts: list[dict],
    model: str = "claude-sonnet-4-6",
    max_tokens: int = 1024
) -> str:
    """
    Submit a batch of requests to the Anthropic Batch API.
    Returns the batch ID. Results are available asynchronously.
    50% cheaper than individual requests. Processes within 24 hours.
    """
    requests = [
        {
            "custom_id": f"request-{i}",
            "params": {
                "model": model,
                "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": prompt["content"]}]
            }
        }
        for i, prompt in enumerate(prompts)
    ]

    batch = client.beta.messages.batches.create(requests=requests)
    logger.info(f"Batch submitted: id={batch.id}, requests={len(requests)}")
    return batch.id

def wait_for_batch(batch_id: str, poll_interval: int = 60) -> list[dict]:
    """
    Poll until the batch completes, then return all results.
    In production, use a webhook instead of polling.
    """
    while True:
        batch = client.beta.messages.batches.retrieve(batch_id)
        status = batch.processing_status

        logger.info(
            f"Batch {batch_id}: {status} — "
            f"succeeded={batch.request_counts.succeeded}, "
            f"errored={batch.request_counts.errored}"
        )

        if status == "ended":
            break

        time.sleep(poll_interval)

    # Collect results in original order:
    results_by_id = {}
    for result in client.beta.messages.batches.results(batch_id):
        if result.result.type == "succeeded":
            results_by_id[result.custom_id] = result.result.message.content[0].text
        else:
            results_by_id[result.custom_id] = None

    # Return in submission order:
    return [results_by_id.get(f"request-{i}") for i in range(len(results_by_id))]

# Usage — 1000 requests in a single batch job at 50% cost:
prompts = [{"content": f"Summarize this text: {text}"} for text in my_texts]
batch_id = submit_batch_job(prompts, model="claude-sonnet-4-6", max_tokens=256)
# ... do other work ...
results = wait_for_batch(batch_id)  # Results when ready
```

### Option 4: Dynamic batch size — adapt to item size and rate limits

```python
import asyncio
import time
import logging
from typing import Any, Callable, Awaitable

logger = logging.getLogger(__name__)

class AdaptiveBatcher:
    """
    Dynamically adjusts batch size based on:
    - Item length (longer items → smaller batches to stay under token limits)
    - Rate limit feedback (429 → reduce concurrency)
    - Throughput target
    """

    def __init__(
        self,
        process_fn: Callable[[list[Any]], Awaitable[list[Any]]],
        max_batch_tokens: int = 8000,  # Max tokens per batch
        avg_tokens_per_item: int = 100,
        max_concurrent: int = 5,
        min_batch_size: int = 1,
        max_batch_size: int = 50
    ):
        self._process = process_fn
        self._max_tokens = max_batch_tokens
        self._avg_tokens = avg_tokens_per_item
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._min_batch = min_batch_size
        self._max_batch = max_batch_size
        self._current_batch_size = max_batch_tokens // avg_tokens_per_item

    def _estimate_tokens(self, item: Any) -> int:
        """Rough token estimate for an item."""
        if isinstance(item, str):
            return len(item.split()) + 10  # word count + overhead
        return self._avg_tokens

    def _split_into_batches(self, items: list[Any]) -> list[list[Any]]:
        """Split items into batches that stay under token budget."""
        batches = []
        current_batch = []
        current_tokens = 0

        for item in items:
            item_tokens = self._estimate_tokens(item)
            if current_batch and (
                current_tokens + item_tokens > self._max_tokens or
                len(current_batch) >= self._current_batch_size
            ):
                batches.append(current_batch)
                current_batch = []
                current_tokens = 0
            current_batch.append(item)
            current_tokens += item_tokens

        if current_batch:
            batches.append(current_batch)
        return batches

    async def process_all(self, items: list[Any]) -> list[Any]:
        """Process all items with adaptive batching and concurrency."""
        batches = self._split_into_batches(items)
        logger.info(
            f"AdaptiveBatcher: {len(items)} items → {len(batches)} batches "
            f"(avg size: {len(items)/len(batches):.1f})"
        )

        results = [None] * len(batches)

        async def process_batch(idx: int, batch: list):
            async with self._semaphore:
                try:
                    result = await self._process(batch)
                    results[idx] = result
                except Exception as exc:
                    if "429" in str(exc) or "rate" in str(exc).lower():
                        # Back off and reduce batch size
                        self._current_batch_size = max(
                            self._min_batch,
                            self._current_batch_size // 2
                        )
                        logger.warning(
                            f"Rate limited. Reducing batch size to {self._current_batch_size}"
                        )
                        await asyncio.sleep(10)
                    raise

        await asyncio.gather(*[process_batch(i, b) for i, b in enumerate(batches)])
        return [item for batch_result in results for item in (batch_result or [])]
```

### Option 5: Request coalescing — merge concurrent requests within a time window

```python
import asyncio
import time
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable

logger = logging.getLogger(__name__)

@dataclass
class PendingItem:
    data: Any
    future: asyncio.Future = field(default_factory=lambda: asyncio.get_event_loop().create_future())

class RequestCoalescer:
    """
    Collect individual requests that arrive within a short time window,
    then process them together as a single batch.
    Useful when requests arrive from concurrent sources (webhooks, API endpoints).
    """

    def __init__(
        self,
        process_batch: Callable[[list[Any]], Awaitable[list[Any]]],
        window_ms: float = 50.0,    # Collect for 50ms, then dispatch
        max_batch_size: int = 50
    ):
        self._process = process_batch
        self._window = window_ms / 1000.0
        self._max_batch = max_batch_size
        self._pending: list[PendingItem] = []
        self._timer_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    async def submit(self, item: Any) -> Any:
        """Submit an item. Returns when its batch is processed."""
        loop = asyncio.get_event_loop()
        pending = PendingItem(data=item, future=loop.create_future())

        async with self._lock:
            self._pending.append(pending)
            if len(self._pending) >= self._max_batch:
                # Batch is full — dispatch immediately
                await self._dispatch()
            elif self._timer_task is None or self._timer_task.done():
                # Start the coalescing window timer
                self._timer_task = asyncio.create_task(self._window_timer())

        return await pending.future

    async def _window_timer(self):
        """Wait for the coalescing window, then dispatch."""
        await asyncio.sleep(self._window)
        async with self._lock:
            if self._pending:
                await self._dispatch()

    async def _dispatch(self):
        """Process all pending items as a single batch."""
        if not self._pending:
            return

        batch = self._pending[:]
        self._pending = []

        items = [p.data for p in batch]
        logger.debug(f"Dispatching coalesced batch of {len(items)} items")

        try:
            results = await self._process(items)
            for pending, result in zip(batch, results):
                if not pending.future.done():
                    pending.future.set_result(result)
        except Exception as exc:
            for pending in batch:
                if not pending.future.done():
                    pending.future.set_exception(exc)

# Usage — requests arriving from many concurrent HTTP handlers are coalesced:
async def classify_batch(texts: list[str]) -> list[str]:
    import anthropic
    client = anthropic.AsyncAnthropic()
    numbered = "\n".join(f"{i+1}. {t}" for i, t in enumerate(texts))
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=len(texts) * 15,
        messages=[{
            "role": "user",
            "content": f"Classify each as positive/negative/neutral:\n{numbered}\nJSON array:"
        }]
    )
    import json
    return json.loads(response.content[0].text)

coalescer = RequestCoalescer(process_batch=classify_batch, window_ms=50, max_batch_size=20)

# 50 concurrent HTTP handlers all call coalescer.submit() at roughly the same time.
# They are automatically grouped into batches of up to 20 and processed together:
results = await asyncio.gather(*[coalescer.submit(f"text {i}") for i in range(50)])
```

### Option 6: Pipeline batching — overlap batch processing stages

```python
import asyncio
import logging
from typing import Any, Callable, Awaitable, AsyncIterator

logger = logging.getLogger(__name__)

async def pipeline_batch_process(
    items: list[Any],
    stage_fns: list[Callable[[list[Any]], Awaitable[list[Any]]]],
    batch_size: int = 20,
    buffer_size: int = 3   # Number of batches to buffer between stages
) -> list[Any]:
    """
    Multi-stage pipeline with batching at each stage.
    Stage N+1 starts processing as soon as Stage N finishes the first batch.
    Overlap reduces total latency vs. sequential stage processing.

    Example stages: [chunk_documents, embed_chunks, classify_embeddings]
    """
    # Split into batches
    batches = [items[i:i + batch_size] for i in range(0, len(items), batch_size)]
    results = [None] * len(batches)

    # Create queues between pipeline stages
    queues = [asyncio.Queue(maxsize=buffer_size) for _ in range(len(stage_fns) - 1)]

    async def run_stage(stage_idx: int, fn: Callable):
        """Process batches through one pipeline stage."""
        if stage_idx == 0:
            # First stage reads from input batches
            for batch_idx, batch in enumerate(batches):
                result = await fn(batch)
                if queues:
                    await queues[0].put((batch_idx, result))
                else:
                    results[batch_idx] = result
        elif stage_idx == len(stage_fns) - 1:
            # Last stage writes to output
            in_queue = queues[stage_idx - 1]
            for _ in range(len(batches)):
                batch_idx, batch = await in_queue.get()
                result = await fn(batch)
                results[batch_idx] = result
                in_queue.task_done()
        else:
            # Middle stages: read from previous queue, write to next queue
            in_queue = queues[stage_idx - 1]
            out_queue = queues[stage_idx]
            for _ in range(len(batches)):
                batch_idx, batch = await in_queue.get()
                result = await fn(batch)
                await out_queue.put((batch_idx, result))
                in_queue.task_done()

    # Run all stages concurrently (they pipeline automatically via queues):
    await asyncio.gather(*[run_stage(i, fn) for i, fn in enumerate(stage_fns)])

    # Flatten batches back into ordered list:
    return [item for batch_result in results for item in batch_result]

# Example: 3-stage document processing pipeline
async def chunk_stage(docs: list[str]) -> list[list[str]]:
    return [doc.split(". ") for doc in docs]  # Simple sentence chunking

async def embed_stage(chunks_list: list[list[str]]) -> list[list[float]]:
    # Embed all chunks in this batch together:
    all_chunks = [c for chunks in chunks_list for c in chunks]
    # ... call embedding API in batch ...
    return [[0.1] * 768 for _ in chunks_list]  # Placeholder

async def classify_stage(embeddings: list[list[float]]) -> list[str]:
    return ["category_a"] * len(embeddings)  # Placeholder

docs = [f"Document {i} content here." for i in range(100)]
results = await pipeline_batch_process(docs, [chunk_stage, embed_stage, classify_stage])
```

## Batching Strategy by Use Case

| Use Case | Batch Size | API / Method | Speedup |
|---|---|---|---|
| Text classification with Claude | 15–25 items per prompt | `messages.create()` | 10–20× |
| Embedding documents | 64–128 per call | Voyage / OpenAI batch endpoint | 20–50× |
| Large bulk jobs (>1,000 items) | Unlimited | Anthropic Batch API | 50% cost + async |
| Concurrent HTTP handlers | Variable (coalesced) | RequestCoalescer | 5–15× |
| Multi-step pipelines | 20–50 per stage | Pipeline with queues | 2–5× on top |
| Real-time with latency SLA | 5–10 items, 50ms window | Coalescer with window | 3–8× |

## Expected Token Savings
Per-item prompt overhead (system prompt + instructions) for 500 items, 1 item/call: 500 × 200 = 100,000 overhead tokens
Same 500 items in 25 batches of 20: 25 × 200 = 5,000 overhead tokens — 95% reduction in prompt overhead
Latency: 500 × 200ms = 100s → 25 × 200ms = 5s (with 5 concurrent batches: 1s total)

## Environment
- Any agent doing bulk document processing, classification, embedding, or extraction; batching is the highest single-impact optimization for throughput-constrained agents — a 20× speedup from batching is more valuable than any model or infrastructure change; implement batching before parallelism, before caching, before anything else
- Source: direct experience; the single-item-per-call pattern appears in ~70% of first-version agent implementations and is the most common cause of "the agent is too slow for production" feedback
