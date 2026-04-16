---
title: "Agent Doesn't Implement Request Batching for Bulk Inference"
description: "AI agents fire one API call per item when processing large datasets, ignoring the Anthropic Message Batches API and asyncio concurrency — resulting in serial latency, wasted throughput, and 2–5x higher per-token costs compared to properly batched workloads."
problem_description: |
  When an agent needs to process hundreds or thousands of items — classifying support tickets, summarizing documents, extracting entities from records — the naive implementation sends one `messages.create()` call per item. This serial approach exhausts rate limits quickly, accumulates latency linearly with item count, and misses the 50% cost discount available through the Anthropic Message Batches API. Without batching, what could be a single batch job becomes thousands of individual API round-trips, each with its own overhead and rate-limit exposure.
category: token-cost
difficulty: intermediate
tags: [batching, bulk-inference, message-batches-api, cost-optimization, throughput]
---

## Solution 1: asyncio.gather Concurrent Fan-Out

Replace serial `await client.messages.create()` loops with `asyncio.gather()` across all items, bounded by a Semaphore to respect rate limits — zero additional infrastructure required.

```python
import asyncio
from anthropic import AsyncAnthropic
from dataclasses import dataclass
from typing import Any


@dataclass
class InferenceResult:
    item_id: str
    input: str
    output: str | None
    error: str | None = None


async def process_single(
    client: AsyncAnthropic,
    sem: asyncio.Semaphore,
    item_id: str,
    text: str,
    system_prompt: str,
    model: str,
    max_tokens: int,
) -> InferenceResult:
    async with sem:
        try:
            response = await client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system_prompt,
                messages=[{"role": "user", "content": text}],
            )
            return InferenceResult(
                item_id=item_id,
                input=text,
                output=response.content[0].text,
            )
        except Exception as e:
            return InferenceResult(
                item_id=item_id,
                input=text,
                output=None,
                error=str(e),
            )


async def bulk_infer_concurrent(
    items: list[dict[str, str]],  # [{"id": ..., "text": ...}]
    system_prompt: str,
    model: str = "claude-haiku-4-5-20251001",
    max_tokens: int = 256,
    concurrency: int = 20,
) -> list[InferenceResult]:
    client = AsyncAnthropic()
    sem = asyncio.Semaphore(concurrency)

    tasks = [
        process_single(client, sem, item["id"], item["text"],
                       system_prompt, model, max_tokens)
        for item in items
    ]

    results = await asyncio.gather(*tasks, return_exceptions=False)
    return list(results)


# Usage
async def main():
    tickets = [
        {"id": f"ticket_{i}", "text": f"Customer issue #{i}: my order hasn't arrived."}
        for i in range(100)
    ]

    results = await bulk_infer_concurrent(
        items=tickets,
        system_prompt="Classify this support ticket as: billing, shipping, or technical. Reply with one word.",
        concurrency=20,
    )

    successes = [r for r in results if r.error is None]
    print(f"Processed {len(successes)}/{len(results)} successfully")
    for r in results[:3]:
        print(f"  {r.item_id}: {r.output}")

asyncio.run(main())
```

## Solution 2: Anthropic Message Batches API

Use the official `client.beta.messages.batches` endpoint to submit up to 10,000 requests in one API call, receiving a 50% cost discount with asynchronous processing.

```python
import asyncio
import time
from anthropic import AsyncAnthropic
from anthropic.types.beta.messages import MessageBatch, BatchRequestCounts


async def submit_batch(
    client: AsyncAnthropic,
    items: list[dict[str, str]],
    system_prompt: str,
    model: str = "claude-haiku-4-5-20251001",
    max_tokens: int = 256,
) -> str:
    """Submit a batch job and return the batch ID."""
    requests = [
        {
            "custom_id": item["id"],
            "params": {
                "model": model,
                "max_tokens": max_tokens,
                "system": system_prompt,
                "messages": [{"role": "user", "content": item["text"]}],
            },
        }
        for item in items
    ]

    batch = await client.beta.messages.batches.create(requests=requests)
    print(f"Batch submitted: {batch.id} ({len(requests)} requests)")
    return batch.id


async def poll_until_complete(
    client: AsyncAnthropic,
    batch_id: str,
    poll_interval: float = 10.0,
    timeout: float = 3600.0,
) -> MessageBatch:
    """Poll batch status until complete or timed out."""
    deadline = time.time() + timeout

    while time.time() < deadline:
        batch = await client.beta.messages.batches.retrieve(batch_id)
        counts: BatchRequestCounts = batch.request_counts

        print(
            f"  Status: {batch.processing_status} | "
            f"processing={counts.processing} "
            f"succeeded={counts.succeeded} "
            f"errored={counts.errored}"
        )

        if batch.processing_status == "ended":
            return batch

        await asyncio.sleep(poll_interval)

    raise TimeoutError(f"Batch {batch_id} did not complete within {timeout}s")


async def collect_results(
    client: AsyncAnthropic,
    batch_id: str,
) -> dict[str, str | None]:
    """Stream results from completed batch."""
    results: dict[str, str | None] = {}

    async for result in await client.beta.messages.batches.results(batch_id):
        custom_id = result.custom_id
        if result.result.type == "succeeded":
            results[custom_id] = result.result.message.content[0].text
        else:
            results[custom_id] = None
            print(f"  Failed: {custom_id} — {result.result.error}")

    return results


async def run_batch_inference(
    items: list[dict[str, str]],
    system_prompt: str,
    model: str = "claude-haiku-4-5-20251001",
    max_tokens: int = 256,
) -> dict[str, str | None]:
    client = AsyncAnthropic()

    # Split into chunks of 10,000 (API limit per batch)
    chunk_size = 10_000
    all_results: dict[str, str | None] = {}

    for chunk_start in range(0, len(items), chunk_size):
        chunk = items[chunk_start:chunk_start + chunk_size]
        batch_id = await submit_batch(client, chunk, system_prompt, model, max_tokens)
        await poll_until_complete(client, batch_id)
        chunk_results = await collect_results(client, batch_id)
        all_results.update(chunk_results)

    return all_results


# Usage
async def main():
    items = [
        {"id": f"doc_{i}", "text": f"Document {i}: This quarterly report covers financial results."}
        for i in range(50)
    ]

    results = await run_batch_inference(
        items=items,
        system_prompt="Summarize in one sentence.",
        max_tokens=100,
    )
    print(f"Collected {len(results)} results")
    for k, v in list(results.items())[:2]:
        print(f"  {k}: {v}")

asyncio.run(main())
```

## Solution 3: Windowed Micro-Batch Queue with Back-Pressure

Buffer incoming inference requests into time-windowed micro-batches dispatched via a consumer task — enables streaming request ingestion without overloading the API or dropping work under burst load.

```python
import asyncio
from anthropic import AsyncAnthropic
from dataclasses import dataclass, field
from typing import Any


@dataclass
class InferenceRequest:
    request_id: str
    text: str
    future: asyncio.Future = field(default_factory=asyncio.Future)


class MicroBatchInferenceQueue:
    def __init__(
        self,
        system_prompt: str,
        model: str = "claude-haiku-4-5-20251001",
        max_tokens: int = 256,
        batch_size: int = 10,
        flush_interval: float = 0.5,
        concurrency: int = 5,
    ):
        self.system_prompt = system_prompt
        self.model = model
        self.max_tokens = max_tokens
        self.batch_size = batch_size
        self.flush_interval = flush_interval
        self.concurrency = concurrency

        self._queue: asyncio.Queue[InferenceRequest] = asyncio.Queue()
        self._client = AsyncAnthropic()
        self._sem = asyncio.Semaphore(concurrency)
        self._running = False
        self._consumer_task: asyncio.Task | None = None

    async def start(self):
        self._running = True
        self._consumer_task = asyncio.create_task(self._consumer_loop())

    async def stop(self):
        self._running = False
        if self._consumer_task:
            self._consumer_task.cancel()
            try:
                await self._consumer_task
            except asyncio.CancelledError:
                pass

    async def infer(self, request_id: str, text: str) -> str:
        """Submit a request and await its result."""
        req = InferenceRequest(request_id=request_id, text=text)
        await self._queue.put(req)
        return await req.future

    async def _consumer_loop(self):
        while self._running:
            batch = await self._collect_batch()
            if batch:
                asyncio.create_task(self._dispatch_batch(batch))

    async def _collect_batch(self) -> list[InferenceRequest]:
        """Collect up to batch_size items within flush_interval."""
        batch: list[InferenceRequest] = []

        try:
            # Block on first item
            first = await asyncio.wait_for(self._queue.get(), timeout=self.flush_interval)
            batch.append(first)
        except asyncio.TimeoutError:
            return batch

        # Drain remaining items non-blocking
        deadline = asyncio.get_event_loop().time() + self.flush_interval
        while len(batch) < self.batch_size:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                break
            try:
                item = await asyncio.wait_for(self._queue.get(), timeout=remaining)
                batch.append(item)
            except asyncio.TimeoutError:
                break

        return batch

    async def _dispatch_batch(self, batch: list[InferenceRequest]):
        """Process a micro-batch concurrently."""
        async def process_one(req: InferenceRequest):
            async with self._sem:
                try:
                    response = await self._client.messages.create(
                        model=self.model,
                        max_tokens=self.max_tokens,
                        system=self.system_prompt,
                        messages=[{"role": "user", "content": req.text}],
                    )
                    req.future.set_result(response.content[0].text)
                except Exception as e:
                    req.future.set_exception(e)

        await asyncio.gather(*[process_one(r) for r in batch])


# Usage
async def main():
    queue = MicroBatchInferenceQueue(
        system_prompt="Classify as positive, negative, or neutral. Reply with one word.",
        batch_size=10,
        flush_interval=0.3,
        concurrency=5,
    )
    await queue.start()

    reviews = [
        f"Review {i}: This product {'exceeded' if i % 2 == 0 else 'disappointed'} my expectations."
        for i in range(25)
    ]

    tasks = [queue.infer(f"review_{i}", text) for i, text in enumerate(reviews)]
    results = await asyncio.gather(*tasks)

    await queue.stop()
    print(f"Classified {len(results)} reviews")
    for r in results[:5]:
        print(f"  {r}")

asyncio.run(main())
```

## Solution 4: Adaptive Token-Aware Batch Sizing

Dynamically adjust batch size based on estimated token counts per item to stay within model context limits and rate-limit token budgets — prevents 400 errors from oversized batches.

```python
import asyncio
import math
from anthropic import AsyncAnthropic
from dataclasses import dataclass


@dataclass
class BatchItem:
    item_id: str
    text: str
    estimated_tokens: int = 0


def estimate_tokens(text: str) -> int:
    """Rough approximation: ~4 chars per token."""
    return math.ceil(len(text) / 4)


def build_token_aware_batches(
    items: list[dict[str, str]],
    system_prompt: str,
    max_tokens_per_request: int = 4096,
    max_batch_tokens: int = 100_000,
    max_batch_size: int = 50,
) -> list[list[BatchItem]]:
    """Group items into batches that stay within token budget."""
    system_tokens = estimate_tokens(system_prompt)
    batches: list[list[BatchItem]] = []
    current_batch: list[BatchItem] = []
    current_tokens = 0

    for item in items:
        user_tokens = estimate_tokens(item["text"])
        # Per-request overhead: system + user + response budget
        request_tokens = system_tokens + user_tokens + max_tokens_per_request

        if (
            current_batch
            and (current_tokens + request_tokens > max_batch_tokens
                 or len(current_batch) >= max_batch_size)
        ):
            batches.append(current_batch)
            current_batch = []
            current_tokens = 0

        batch_item = BatchItem(
            item_id=item["id"],
            text=item["text"],
            estimated_tokens=request_tokens,
        )
        current_batch.append(batch_item)
        current_tokens += request_tokens

    if current_batch:
        batches.append(current_batch)

    return batches


async def process_token_aware_batch(
    client: AsyncAnthropic,
    batch: list[BatchItem],
    system_prompt: str,
    model: str,
    max_tokens: int,
    sem: asyncio.Semaphore,
) -> list[dict]:
    async def process_one(item: BatchItem) -> dict:
        async with sem:
            try:
                resp = await client.messages.create(
                    model=model,
                    max_tokens=max_tokens,
                    system=system_prompt,
                    messages=[{"role": "user", "content": item.text}],
                )
                return {"id": item.item_id, "output": resp.content[0].text, "error": None}
            except Exception as e:
                return {"id": item.item_id, "output": None, "error": str(e)}

    return await asyncio.gather(*[process_one(item) for item in batch])


async def adaptive_bulk_infer(
    items: list[dict[str, str]],
    system_prompt: str,
    model: str = "claude-haiku-4-5-20251001",
    max_tokens: int = 256,
    concurrency: int = 15,
) -> list[dict]:
    client = AsyncAnthropic()
    sem = asyncio.Semaphore(concurrency)

    batches = build_token_aware_batches(items, system_prompt, max_tokens_per_request=max_tokens)
    print(f"Split {len(items)} items into {len(batches)} token-aware batches")

    all_results: list[dict] = []
    for i, batch in enumerate(batches):
        batch_tokens = sum(b.estimated_tokens for b in batch)
        print(f"  Batch {i+1}/{len(batches)}: {len(batch)} items, ~{batch_tokens:,} tokens")
        results = await process_token_aware_batch(
            client, batch, system_prompt, model, max_tokens, sem
        )
        all_results.extend(results)

    return all_results


# Usage
async def main():
    # Mix of short and long documents
    items = [
        {"id": f"item_{i}", "text": "Short text. " * (1 + i % 10)}
        for i in range(200)
    ]

    results = await adaptive_bulk_infer(
        items=items,
        system_prompt="Summarize in one sentence.",
        max_tokens=128,
        concurrency=15,
    )
    print(f"Total results: {len(results)}")

asyncio.run(main())
```

## Solution 5: Streaming Batch with Progress Tracking and Retry

Process large item sets with per-item streaming, real-time progress reporting, and exponential-backoff retry — suitable for long-running bulk jobs that need visibility and resilience.

```python
import asyncio
import time
from anthropic import AsyncAnthropic
from dataclasses import dataclass, field
from typing import AsyncIterator


@dataclass
class BatchProgress:
    total: int
    completed: int = 0
    failed: int = 0
    retried: int = 0
    start_time: float = field(default_factory=time.time)

    @property
    def elapsed(self) -> float:
        return time.time() - self.start_time

    @property
    def rate(self) -> float:
        return self.completed / max(self.elapsed, 0.001)

    @property
    def eta(self) -> float:
        remaining = self.total - self.completed - self.failed
        return remaining / max(self.rate, 0.001)

    def report(self) -> str:
        return (
            f"[{self.completed + self.failed}/{self.total}] "
            f"ok={self.completed} err={self.failed} retry={self.retried} "
            f"rate={self.rate:.1f}/s eta={self.eta:.0f}s"
        )


async def infer_with_retry(
    client: AsyncAnthropic,
    sem: asyncio.Semaphore,
    item_id: str,
    text: str,
    system_prompt: str,
    model: str,
    max_tokens: int,
    max_retries: int = 3,
) -> tuple[str, str | None, int]:
    """Returns (item_id, output_or_None, retry_count)."""
    last_error = None

    for attempt in range(max_retries + 1):
        if attempt > 0:
            backoff = min(2 ** attempt, 30)
            await asyncio.sleep(backoff)

        async with sem:
            try:
                output_parts = []
                async with client.messages.stream(
                    model=model,
                    max_tokens=max_tokens,
                    system=system_prompt,
                    messages=[{"role": "user", "content": text}],
                ) as stream:
                    async for chunk in stream.text_stream:
                        output_parts.append(chunk)

                return item_id, ''.join(output_parts), attempt
            except Exception as e:
                last_error = str(e)
                if "rate_limit" not in str(e).lower() and attempt >= 1:
                    break  # Non-retryable error

    return item_id, None, max_retries


async def streaming_batch_with_progress(
    items: list[dict[str, str]],
    system_prompt: str,
    model: str = "claude-haiku-4-5-20251001",
    max_tokens: int = 256,
    concurrency: int = 10,
    progress_interval: float = 5.0,
) -> AsyncIterator[dict]:
    client = AsyncAnthropic()
    sem = asyncio.Semaphore(concurrency)
    progress = BatchProgress(total=len(items))

    result_queue: asyncio.Queue[dict | None] = asyncio.Queue()

    async def process_item(item: dict):
        item_id, output, retries = await infer_with_retry(
            client, sem, item["id"], item["text"],
            system_prompt, model, max_tokens,
        )
        progress.retried += retries
        if output is not None:
            progress.completed += 1
        else:
            progress.failed += 1

        await result_queue.put({
            "id": item_id,
            "output": output,
            "retries": retries,
        })

    async def progress_reporter():
        while progress.completed + progress.failed < len(items):
            await asyncio.sleep(progress_interval)
            print(f"Progress: {progress.report()}")

    tasks = [asyncio.create_task(process_item(item)) for item in items]
    reporter = asyncio.create_task(progress_reporter())

    received = 0
    while received < len(items):
        result = await result_queue.get()
        yield result
        received += 1

    await asyncio.gather(*tasks, return_exceptions=True)
    reporter.cancel()
    print(f"Final: {progress.report()}")


# Usage
async def main():
    items = [
        {"id": f"article_{i}", "text": f"Article {i}: Recent developments in AI have accelerated."}
        for i in range(30)
    ]

    results = []
    async for result in streaming_batch_with_progress(
        items=items,
        system_prompt="Summarize in 10 words or fewer.",
        concurrency=8,
    ):
        results.append(result)
        if len(results) % 10 == 0:
            print(f"Received {len(results)} results so far")

    print(f"Done. Total: {len(results)}")

asyncio.run(main())
```

## Solution 6: Priority-Ordered Batch Scheduler with SLA Tracking

Assign priority tiers to inference requests so high-SLA items (user-facing) get processed before background batch work — enabling fair resource sharing within the same concurrency pool.

```python
import asyncio
import heapq
import time
from dataclasses import dataclass, field
from enum import IntEnum
from anthropic import AsyncAnthropic


class Priority(IntEnum):
    CRITICAL = 0    # Real-time user-facing (process immediately)
    HIGH = 1        # Interactive batch (seconds SLA)
    NORMAL = 2      # Background batch (minutes SLA)
    LOW = 3         # Offline bulk (hours SLA)


@dataclass(order=True)
class PrioritizedRequest:
    priority: int
    enqueued_at: float
    request_id: str = field(compare=False)
    text: str = field(compare=False)
    future: asyncio.Future = field(compare=False, default_factory=asyncio.Future)
    sla_seconds: float = field(compare=False, default=60.0)


class PriorityBatchScheduler:
    def __init__(
        self,
        system_prompt: str,
        model: str = "claude-haiku-4-5-20251001",
        max_tokens: int = 256,
        concurrency: int = 20,
    ):
        self.system_prompt = system_prompt
        self.model = model
        self.max_tokens = max_tokens
        self._heap: list[PrioritizedRequest] = []
        self._heap_lock = asyncio.Lock()
        self._sem = asyncio.Semaphore(concurrency)
        self._client = AsyncAnthropic()
        self._metrics: dict[Priority, list[float]] = {p: [] for p in Priority}
        self._running = False
        self._scheduler_task: asyncio.Task | None = None

    async def start(self):
        self._running = True
        self._scheduler_task = asyncio.create_task(self._scheduler_loop())

    async def stop(self):
        self._running = False
        if self._scheduler_task:
            self._scheduler_task.cancel()
            try:
                await self._scheduler_task
            except asyncio.CancelledError:
                pass

    async def submit(
        self,
        request_id: str,
        text: str,
        priority: Priority = Priority.NORMAL,
        sla_seconds: float = 60.0,
    ) -> asyncio.Future:
        req = PrioritizedRequest(
            priority=int(priority),
            enqueued_at=time.time(),
            request_id=request_id,
            text=text,
            sla_seconds=sla_seconds,
        )
        async with self._heap_lock:
            heapq.heappush(self._heap, req)
        return req.future

    async def _scheduler_loop(self):
        while self._running:
            req = await self._pop_next()
            if req:
                asyncio.create_task(self._execute(req))
            else:
                await asyncio.sleep(0.05)

    async def _pop_next(self) -> PrioritizedRequest | None:
        async with self._heap_lock:
            if not self._heap:
                return None

            # Check for SLA violations — escalate priority
            now = time.time()
            for req in self._heap:
                age = now - req.enqueued_at
                if age > req.sla_seconds * 0.8 and req.priority > int(Priority.HIGH):
                    req.priority = int(Priority.HIGH)

            heapq.heapify(self._heap)
            return heapq.heappop(self._heap)

    async def _execute(self, req: PrioritizedRequest):
        start = time.time()
        async with self._sem:
            try:
                response = await self._client.messages.create(
                    model=self.model,
                    max_tokens=self.max_tokens,
                    system=self.system_prompt,
                    messages=[{"role": "user", "content": req.text}],
                )
                latency = time.time() - start
                self._metrics[Priority(req.priority)].append(latency)

                sla_met = latency <= req.sla_seconds
                if not sla_met:
                    print(f"SLA MISS: {req.request_id} latency={latency:.2f}s sla={req.sla_seconds}s")

                req.future.set_result(response.content[0].text)
            except Exception as e:
                req.future.set_exception(e)

    def get_metrics(self) -> dict[str, dict]:
        report = {}
        for priority, latencies in self._metrics.items():
            if latencies:
                report[priority.name] = {
                    "count": len(latencies),
                    "avg_latency": sum(latencies) / len(latencies),
                    "max_latency": max(latencies),
                    "p95_latency": sorted(latencies)[int(len(latencies) * 0.95)],
                }
        return report


# Usage
async def main():
    scheduler = PriorityBatchScheduler(
        system_prompt="Classify as urgent, normal, or low. One word reply.",
        concurrency=10,
    )
    await scheduler.start()

    # Mix of priorities
    futures = []
    for i in range(50):
        priority = Priority.CRITICAL if i % 20 == 0 else (
            Priority.HIGH if i % 5 == 0 else Priority.NORMAL
        )
        sla = 2.0 if priority == Priority.CRITICAL else 10.0
        fut = await scheduler.submit(
            request_id=f"req_{i}",
            text=f"Request {i}: Process this item immediately." if priority == Priority.CRITICAL
                 else f"Request {i}: This can wait a bit.",
            priority=priority,
            sla_seconds=sla,
        )
        futures.append(fut)

    results = await asyncio.gather(*futures, return_exceptions=True)
    await scheduler.stop()

    successes = sum(1 for r in results if not isinstance(r, Exception))
    print(f"Completed: {successes}/{len(results)}")
    print(f"Metrics: {scheduler.get_metrics()}")

asyncio.run(main())
```

## Comparison

| Approach | Throughput | Cost Reduction | Complexity | Latency | Best For |
|---|---|---|---|---|---|
| asyncio.gather Fan-Out | High | 0% (parallel calls) | Low | Low | General bulk tasks, quick wins |
| Message Batches API | Very High | 50% | Low | High (async) | Large offline workloads |
| Micro-Batch Queue | High | 0% | Medium | Low | Streaming ingestion, live queues |
| Token-Aware Sizing | High | 0% | Medium | Low | Mixed-length documents |
| Streaming + Retry | Medium | 0% | Medium | Low | Long jobs needing visibility |
| Priority Scheduler | Medium | 0% | High | Variable | Mixed SLA workloads |
