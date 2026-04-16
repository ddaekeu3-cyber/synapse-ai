---
layout: solution
title: "Agent Doesn't Implement Async Pipeline with Bounded Buffers"
category: concurrency
description: "Agent pipelines without bounded buffers either block producers or exhaust memory when downstream steps are slower than upstream ones. These patterns show how to build bounded async pipelines that apply backpressure correctly."
tags: [concurrency, pipeline, backpressure, asyncio, bounded-buffer, anthropic]
---

## Problem

An agent pipeline that fetches documents, enriches them with LLM calls, then writes results to a database fails when stages run at different speeds. Without bounded buffers, a fast document fetcher fills memory with thousands of unprocessed items, or a slow LLM enricher starves a waiting writer. Bounded async pipelines apply backpressure: producers block when buffers are full, consumers wait when buffers are empty, and the system reaches a stable equilibrium.

---

### Option 1: Two-Stage Pipeline with asyncio.Queue

Connect fetch → enrich stages with a bounded `asyncio.Queue` that blocks the producer when full.

```python
import asyncio
import anthropic

client = anthropic.AsyncAnthropic()

async def fetch_documents(queue: asyncio.Queue, items: list[str]) -> None:
    """Producer: puts raw documents into the queue."""
    for i, text in enumerate(items):
        await queue.put({"id": i, "text": text})
        print(f"[fetch] queued item {i} (queue size: {queue.qsize()})")
    await queue.put(None)  # sentinel

async def enrich_document(item: dict) -> dict:
    """LLM enrichment step."""
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=100,
        messages=[{
            "role": "user",
            "content": f"Classify in one word (technical/business/other): {item['text']}",
        }],
    )
    return {**item, "category": response.content[0].text.strip()}

async def enrich_worker(in_queue: asyncio.Queue, out_queue: asyncio.Queue) -> None:
    """Consumer of raw docs, producer of enriched docs."""
    while True:
        item = await in_queue.get()
        if item is None:
            await out_queue.put(None)  # propagate sentinel
            in_queue.task_done()
            break
        enriched = await enrich_document(item)
        await out_queue.put(enriched)
        print(f"[enrich] item {item['id']} → {enriched['category']}")
        in_queue.task_done()

async def write_results(queue: asyncio.Queue) -> list[dict]:
    """Sink: collects enriched results."""
    results = []
    while True:
        item = await queue.get()
        if item is None:
            queue.task_done()
            break
        results.append(item)
        print(f"[write] stored item {item['id']}")
        queue.task_done()
    return results

async def run_pipeline(documents: list[str], buffer_size: int = 3) -> list[dict]:
    raw_queue = asyncio.Queue(maxsize=buffer_size)
    enriched_queue = asyncio.Queue(maxsize=buffer_size)

    async with asyncio.TaskGroup() as tg:
        tg.create_task(fetch_documents(raw_queue, documents))
        tg.create_task(enrich_worker(raw_queue, enriched_queue))
        result_task = tg.create_task(write_results(enriched_queue))

    return result_task.result()

if __name__ == "__main__":
    docs = [
        "Machine learning model training on GPU clusters",
        "Q3 revenue forecast and budget allocation",
        "The weather today is sunny and warm",
        "Kubernetes deployment manifests for production",
        "Annual board meeting agenda",
    ]

    async def main():
        results = await run_pipeline(docs, buffer_size=2)
        print(f"\n=== Results ({len(results)} items) ===")
        for r in results:
            print(f"  [{r['id']}] {r['category']}: {r['text'][:40]}")
    asyncio.run(main())

# Expected Token Savings: Bounded buffer prevents queuing thousands of LLM calls at once; controls cost rate
# Environment: ANTHROPIC_API_KEY
```

---

### Option 2: Multi-Worker Parallel Stage with Concurrency Limit

Add multiple concurrent workers at the bottleneck stage using a semaphore-controlled pool.

```python
import asyncio
import anthropic

client = anthropic.AsyncAnthropic()

async def enricher_pool(
    in_queue: asyncio.Queue,
    out_queue: asyncio.Queue,
    n_workers: int = 3,
    semaphore: asyncio.Semaphore = None,
) -> None:
    """Run N concurrent enrichers, each respecting a shared semaphore."""
    if semaphore is None:
        semaphore = asyncio.Semaphore(n_workers)

    async def worker(worker_id: int):
        while True:
            item = await in_queue.get()
            if item is None:
                in_queue.put_nowait(None)  # re-queue sentinel for other workers
                in_queue.task_done()
                return

            async with semaphore:
                response = await client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=80,
                    messages=[{
                        "role": "user",
                        "content": f"Summarize in 10 words: {item['text']}",
                    }],
                )
                enriched = {**item, "summary": response.content[0].text.strip()}
                await out_queue.put(enriched)
                print(f"[worker-{worker_id}] processed item {item['id']}")
            in_queue.task_done()

    await asyncio.gather(*[worker(i) for i in range(n_workers)])
    await out_queue.put(None)  # single sentinel for sink

async def produce(queue: asyncio.Queue, items: list[str]):
    for i, text in enumerate(items):
        await queue.put({"id": i, "text": text})
    await queue.put(None)

async def consume(queue: asyncio.Queue) -> list[dict]:
    results = []
    while True:
        item = await queue.get()
        if item is None:
            break
        results.append(item)
    return results

async def run_parallel_pipeline(docs: list[str], n_workers: int = 3, buffer: int = 5) -> list[dict]:
    in_q = asyncio.Queue(maxsize=buffer)
    out_q = asyncio.Queue(maxsize=buffer)
    sem = asyncio.Semaphore(n_workers)

    producer_task = asyncio.create_task(produce(in_q, docs))
    pool_task = asyncio.create_task(enricher_pool(in_q, out_q, n_workers, sem))
    consumer_task = asyncio.create_task(consume(out_q))

    await asyncio.gather(producer_task, pool_task)
    return await consumer_task

if __name__ == "__main__":
    async def main():
        docs = [f"Document {i}: content about topic {i % 5}" for i in range(10)]
        results = await run_parallel_pipeline(docs, n_workers=3, buffer=4)
        print(f"\n{len(results)} items processed")
        for r in results[:3]:
            print(f"  [{r['id']}] {r['summary']}")
    asyncio.run(main())

# Expected Token Savings: N workers saturate the LLM API up to concurrency limit without over-queuing
# Environment: ANTHROPIC_API_KEY
```

---

### Option 3: Three-Stage Pipeline with Independent Buffer Sizes

Model fetch → classify → format as three stages with independently tuned buffer sizes between each.

```python
import asyncio
from dataclasses import dataclass
import anthropic

client = anthropic.AsyncAnthropic()

@dataclass
class PipelineItem:
    id: int
    raw_text: str
    category: str = ""
    formatted: str = ""
    stage: str = "raw"

async def stage_fetch(out: asyncio.Queue, texts: list[str]) -> None:
    for i, text in enumerate(texts):
        item = PipelineItem(id=i, raw_text=text)
        await out.put(item)
    await out.put(None)

async def stage_classify(in_q: asyncio.Queue, out_q: asyncio.Queue) -> None:
    while True:
        item = await in_q.get()
        if item is None:
            await out_q.put(None)
            in_q.task_done()
            return

        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=20,
            messages=[{
                "role": "user",
                "content": f"One word category for: {item.raw_text[:100]}",
            }],
        )
        item.category = response.content[0].text.strip().split()[0]
        item.stage = "classified"
        await out_q.put(item)
        in_q.task_done()

async def stage_format(in_q: asyncio.Queue, out_q: asyncio.Queue) -> None:
    while True:
        item = await in_q.get()
        if item is None:
            await out_q.put(None)
            in_q.task_done()
            return

        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=80,
            messages=[{
                "role": "user",
                "content": f"Format as '[{item.category}] ' + 10-word summary: {item.raw_text[:200]}",
            }],
        )
        item.formatted = response.content[0].text.strip()
        item.stage = "formatted"
        await out_q.put(item)
        in_q.task_done()

async def stage_sink(in_q: asyncio.Queue) -> list[PipelineItem]:
    results = []
    while True:
        item = await in_q.get()
        if item is None:
            in_q.task_done()
            break
        results.append(item)
        print(f"[sink] {item.id}: {item.formatted[:60]}")
        in_q.task_done()
    return results

async def three_stage_pipeline(docs: list[str]) -> list[PipelineItem]:
    # Independent buffer sizes tuned for each stage's throughput
    q1 = asyncio.Queue(maxsize=5)   # fetch → classify (generous: fetch is fast)
    q2 = asyncio.Queue(maxsize=3)   # classify → format (tight: classify is slow)
    q3 = asyncio.Queue(maxsize=4)   # format → sink

    tasks = [
        asyncio.create_task(stage_fetch(q1, docs)),
        asyncio.create_task(stage_classify(q1, q2)),
        asyncio.create_task(stage_format(q2, q3)),
    ]
    sink_task = asyncio.create_task(stage_sink(q3))

    await asyncio.gather(*tasks)
    return await sink_task

if __name__ == "__main__":
    async def main():
        docs = [
            "Machine learning model inference optimization techniques",
            "Company revenue grew 23% in Q3 compared to prior year",
            "New security vulnerability discovered in OpenSSL library",
            "Team lunch scheduled for Friday at 12pm downtown",
            "Kubernetes pod autoscaling based on custom metrics",
        ]
        results = await three_stage_pipeline(docs)
        print(f"\nProcessed {len(results)} items through 3 stages")
    asyncio.run(main())

# Expected Token Savings: Stages overlap in time (pipelining); total latency < sum of sequential latencies
# Environment: ANTHROPIC_API_KEY
```

---

### Option 4: Backpressure-Aware Pipeline with Rate Limiter

Integrate a token-bucket rate limiter into the pipeline to respect API rate limits without dropping items.

```python
import asyncio
import time
from dataclasses import dataclass, field
import anthropic

client = anthropic.AsyncAnthropic()

@dataclass
class TokenBucket:
    capacity: float        # max tokens
    refill_rate: float     # tokens per second
    _tokens: float = field(init=False)
    _last_refill: float = field(init=False)
    _lock: asyncio.Lock = field(init=False)

    def __post_init__(self):
        self._tokens = self.capacity
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, tokens: float = 1.0):
        async with self._lock:
            while True:
                now = time.monotonic()
                elapsed = now - self._last_refill
                self._tokens = min(self.capacity, self._tokens + elapsed * self.refill_rate)
                self._last_refill = now

                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return
                wait = (tokens - self._tokens) / self.refill_rate
                await asyncio.sleep(wait)

# Shared rate limiter: 5 LLM calls per second
rate_limiter = TokenBucket(capacity=5, refill_rate=5)

async def rate_limited_llm_call(text: str) -> str:
    await rate_limiter.acquire(1.0)
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=60,
        messages=[{"role": "user", "content": f"3-word summary: {text[:150]}"}],
    )
    return response.content[0].text.strip()

async def producer(queue: asyncio.Queue, items: list[str]):
    for i, text in enumerate(items):
        await queue.put((i, text))
        print(f"[producer] queued {i} (q={queue.qsize()})")
    await queue.put(None)

async def consumer(in_q: asyncio.Queue, out_q: asyncio.Queue, worker_id: int):
    while True:
        item = await in_q.get()
        if item is None:
            in_q.put_nowait(None)  # re-queue for siblings
            in_q.task_done()
            return
        idx, text = item
        start = time.monotonic()
        summary = await rate_limited_llm_call(text)
        elapsed = (time.monotonic() - start) * 1000
        print(f"[worker-{worker_id}] item {idx}: {summary[:30]} ({elapsed:.0f}ms)")
        await out_q.put({"id": idx, "text": text, "summary": summary})
        in_q.task_done()

async def sink(queue: asyncio.Queue) -> list[dict]:
    results = []
    while True:
        item = await queue.get()
        if item is None:
            break
        results.append(item)
    return sorted(results, key=lambda x: x["id"])

async def rate_limited_pipeline(docs: list[str], n_workers: int = 3, buffer: int = 5) -> list[dict]:
    in_q = asyncio.Queue(maxsize=buffer)
    out_q = asyncio.Queue(maxsize=buffer)

    worker_tasks = [asyncio.create_task(consumer(in_q, out_q, i)) for i in range(n_workers)]
    prod_task = asyncio.create_task(producer(in_q, docs))
    sink_task = asyncio.create_task(sink(out_q))

    await asyncio.gather(prod_task, *worker_tasks)
    await out_q.put(None)
    return await sink_task

if __name__ == "__main__":
    async def main():
        import time
        docs = [f"Article {i}: {['AI breakthrough', 'Stock market rally', 'Climate report'][i%3]}" for i in range(8)]
        start = time.monotonic()
        results = await rate_limited_pipeline(docs, n_workers=3)
        elapsed = time.monotonic() - start
        print(f"\n{len(results)} items in {elapsed:.1f}s ({len(results)/elapsed:.1f} items/s)")
    asyncio.run(main())

# Expected Token Savings: Rate limiter prevents 429 errors that waste tokens; smooth flow vs burst-retry
# Environment: ANTHROPIC_API_KEY
```

---

### Option 5: Priority Pipeline with Multiple Input Lanes

Route high-priority items through a fast lane while low-priority items use a slower, cheaper model.

```python
import asyncio
import anthropic
from dataclasses import dataclass

client = anthropic.AsyncAnthropic()

@dataclass
class PriorityItem:
    id: int
    text: str
    priority: str  # "high" | "low"
    result: str = ""

async def priority_router(
    items: list[PriorityItem],
    high_q: asyncio.Queue,
    low_q: asyncio.Queue,
) -> None:
    for item in items:
        if item.priority == "high":
            await high_q.put(item)
        else:
            await low_q.put(item)
    await high_q.put(None)
    await low_q.put(None)

async def high_priority_worker(in_q: asyncio.Queue, out_q: asyncio.Queue) -> None:
    """Fast lane: uses Sonnet for better quality."""
    while True:
        item = await in_q.get()
        if item is None:
            await out_q.put(None)
            in_q.task_done()
            return
        response = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=200,
            messages=[{"role": "user", "content": f"[HIGH PRIORITY] Analyze: {item.text}"}],
        )
        item.result = response.content[0].text.strip()
        item.result = f"[SONNET] {item.result[:80]}"
        await out_q.put(item)
        print(f"[high-lane] item {item.id}: done")
        in_q.task_done()

async def low_priority_worker(in_q: asyncio.Queue, out_q: asyncio.Queue) -> None:
    """Slow lane: uses Haiku for cost efficiency."""
    while True:
        item = await in_q.get()
        if item is None:
            await out_q.put(None)
            in_q.task_done()
            return
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=80,
            messages=[{"role": "user", "content": f"Brief summary: {item.text}"}],
        )
        item.result = f"[HAIKU] {response.content[0].text.strip()[:60]}"
        await out_q.put(item)
        print(f"[low-lane] item {item.id}: done")
        in_q.task_done()

async def merge_sink(high_out: asyncio.Queue, low_out: asyncio.Queue) -> list[PriorityItem]:
    results = []
    done_count = 0
    while done_count < 2:
        for q in [high_out, low_out]:
            try:
                item = q.get_nowait()
                if item is None:
                    done_count += 1
                else:
                    results.append(item)
            except asyncio.QueueEmpty:
                pass
        if done_count < 2:
            await asyncio.sleep(0.01)
    return sorted(results, key=lambda x: x.id)

async def priority_pipeline(items: list[PriorityItem]) -> list[PriorityItem]:
    high_in = asyncio.Queue(maxsize=3)
    low_in = asyncio.Queue(maxsize=5)
    high_out = asyncio.Queue(maxsize=3)
    low_out = asyncio.Queue(maxsize=5)

    await asyncio.gather(
        priority_router(items, high_in, low_in),
        high_priority_worker(high_in, high_out),
        low_priority_worker(low_in, low_out),
    )
    return await merge_sink(high_out, low_out)

if __name__ == "__main__":
    async def main():
        items = [
            PriorityItem(0, "Critical security incident: unauthorized access detected", "high"),
            PriorityItem(1, "Weekly newsletter content for subscribers", "low"),
            PriorityItem(2, "Production database outage affecting 10,000 users", "high"),
            PriorityItem(3, "Blog post draft about coding best practices", "low"),
            PriorityItem(4, "Payment processing failure on checkout page", "high"),
        ]
        results = await priority_pipeline(items)
        for r in results:
            print(f"  [{r.priority.upper()}] item {r.id}: {r.result[:70]}")
    asyncio.run(main())

# Expected Token Savings: Low-priority items use Haiku (4x cheaper); high-priority use Sonnet for quality
# Environment: ANTHROPIC_API_KEY
```

---

### Option 6: Resilient Pipeline with Dead-Letter Queue

Route failed items to a dead-letter queue for inspection and retry rather than silently dropping them.

```python
import asyncio
import time
from dataclasses import dataclass, field
import anthropic
from anthropic import APIError

client = anthropic.AsyncAnthropic()

@dataclass
class PipelineRecord:
    id: int
    text: str
    result: str = ""
    error: str = ""
    attempts: int = 0
    failed_at: float = 0.0

async def process_with_retry(item: PipelineRecord, max_attempts: int = 2) -> bool:
    for attempt in range(max_attempts):
        try:
            item.attempts += 1
            response = await asyncio.wait_for(
                client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=60,
                    messages=[{"role": "user", "content": f"Summarize: {item.text[:200]}"}],
                ),
                timeout=10.0,
            )
            item.result = response.content[0].text.strip()
            return True
        except (APIError, asyncio.TimeoutError) as e:
            item.error = str(e)
            if attempt < max_attempts - 1:
                await asyncio.sleep(0.5 * (2 ** attempt))
    item.failed_at = time.time()
    return False

async def pipeline_worker(
    in_q: asyncio.Queue,
    out_q: asyncio.Queue,
    dlq: asyncio.Queue,
    worker_id: int,
) -> None:
    while True:
        item = await in_q.get()
        if item is None:
            in_q.put_nowait(None)
            in_q.task_done()
            return

        success = await process_with_retry(item)
        if success:
            await out_q.put(item)
            print(f"[worker-{worker_id}] ✓ item {item.id}")
        else:
            await dlq.put(item)
            print(f"[worker-{worker_id}] ✗ item {item.id} → DLQ: {item.error[:50]}")
        in_q.task_done()

async def run_resilient_pipeline(docs: list[str], n_workers: int = 3) -> dict:
    in_q = asyncio.Queue(maxsize=5)
    out_q: asyncio.Queue[PipelineRecord] = asyncio.Queue()
    dlq: asyncio.Queue[PipelineRecord] = asyncio.Queue()

    # Seed input queue
    async def produce():
        for i, text in enumerate(docs):
            await in_q.put(PipelineRecord(id=i, text=text))
        await in_q.put(None)

    workers = [
        asyncio.create_task(pipeline_worker(in_q, out_q, dlq, i))
        for i in range(n_workers)
    ]

    await asyncio.gather(produce(), *workers)

    # Drain output and DLQ
    results, failed = [], []
    while not out_q.empty():
        results.append(out_q.get_nowait())
    while not dlq.empty():
        failed.append(dlq.get_nowait())

    print(f"\n[pipeline] {len(results)} succeeded, {len(failed)} in DLQ")
    if failed:
        print(f"[DLQ items]: {[f.id for f in failed]}")

    return {"results": results, "dead_letter": failed}

if __name__ == "__main__":
    async def main():
        docs = [f"Document {i}: content about {['AI', 'Finance', 'Health'][i % 3]}" for i in range(8)]
        output = await run_resilient_pipeline(docs)
        print(f"Success: {len(output['results'])}, DLQ: {len(output['dead_letter'])}")
    asyncio.run(main())

# Expected Token Savings: Failed items don't spin in retry loop; DLQ enables targeted reprocessing
# Environment: ANTHROPIC_API_KEY
```

---

## Comparison

| Option | Stages | Buffer Strategy | Workers | Best For |
|--------|--------|----------------|---------|----------|
| 1 | 2 (fetch → enrich) | Single bounded queue | 1 per stage | Simple linear pipelines |
| 2 | 2 with worker pool | Bounded + semaphore | N parallel | Bottleneck stage parallelization |
| 3 | 3 independent stages | Per-stage tuned sizes | 1 per stage | Multi-stage with different throughputs |
| 4 | 2 with rate limiter | Bounded + token bucket | N parallel | API rate-limit-constrained pipelines |
| 5 | Priority lanes (2 parallel) | Per-priority queues | 1 per lane | Mixed high/low priority workloads |
| 6 | 2 with DLQ | Bounded + dead-letter | N parallel | Production pipelines needing resilience |
