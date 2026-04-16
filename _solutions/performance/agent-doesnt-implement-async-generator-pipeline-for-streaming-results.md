---
title: "Agent Doesn't Implement Async Generator Pipeline for Streaming Results"
description: "AI agents collect all results before returning them — batching complete model responses, tool results, and sub-agent outputs into a single blocking call. Async generator pipelines deliver results incrementally as they become available, reducing time-to-first-result from seconds to milliseconds."
problem_description: |
  An agent that processes 50 documents returns all 50 summaries at once after waiting for the slowest item. An agent that calls 3 tools in sequence buffers each result before the next step. A multi-step reasoning chain delivers the final answer only after every intermediate step completes. Async generator pipelines flip this: each result is yielded the moment it's ready, consumers receive partial results immediately, and memory usage stays bounded regardless of dataset size. The pattern composes — generators can be chained, filtered, mapped, and merged without buffering.
category: performance
difficulty: intermediate
tags: [async-generator, streaming, pipeline, throughput, time-to-first-result]
---

## Solution 1: Basic Async Generator for Bulk Inference

Replace a list-returning bulk inference function with an async generator — callers receive each result as it completes rather than waiting for all.

```python
import asyncio
from anthropic import AsyncAnthropic
from dataclasses import dataclass
from typing import AsyncIterator


@dataclass
class InferenceResult:
    item_id: str
    text: str
    latency_ms: float


async def bulk_infer_streaming(
    client: AsyncAnthropic,
    items: list[dict[str, str]],  # [{"id": ..., "text": ...}]
    system_prompt: str,
    model: str = "claude-haiku-4-5-20251001",
    max_tokens: int = 128,
    concurrency: int = 10,
) -> AsyncIterator[InferenceResult]:
    """Yield results as they complete — no waiting for all."""
    import time

    sem = asyncio.Semaphore(concurrency)
    queue: asyncio.Queue[InferenceResult | Exception | None] = asyncio.Queue()

    async def process_one(item: dict):
        async with sem:
            start = time.monotonic()
            try:
                response = await client.messages.create(
                    model=model,
                    max_tokens=max_tokens,
                    system=system_prompt,
                    messages=[{"role": "user", "content": item["text"]}],
                )
                latency = (time.monotonic() - start) * 1000
                await queue.put(InferenceResult(item["id"], response.content[0].text, round(latency, 1)))
            except Exception as e:
                await queue.put(e)

    # Launch all tasks concurrently
    tasks = [asyncio.create_task(process_one(item)) for item in items]

    # Signal completion when all tasks are done
    async def wait_and_signal():
        await asyncio.gather(*tasks, return_exceptions=True)
        await queue.put(None)

    asyncio.create_task(wait_and_signal())

    # Yield results as they arrive
    received = 0
    while True:
        item = await queue.get()
        if item is None:
            return
        if isinstance(item, Exception):
            print(f"[error] {item}")
            continue
        yield item
        received += 1


# Usage
async def main():
    client = AsyncAnthropic()

    documents = [
        {"id": f"doc_{i}", "text": f"Document {i}: Cloud computing enables scalable infrastructure."}
        for i in range(10)
    ]

    import time
    start = time.monotonic()
    count = 0

    async for result in bulk_infer_streaming(
        client, documents, "Summarize in one sentence.", concurrency=5
    ):
        elapsed = (time.monotonic() - start) * 1000
        print(f"[+{elapsed:.0f}ms] {result.item_id} ({result.latency_ms}ms): {result.text[:60]}")
        count += 1

    print(f"\nTotal: {count} results in {(time.monotonic() - start) * 1000:.0f}ms")

asyncio.run(main())
```

## Solution 2: Chained Generator Pipeline — Map, Filter, Transform

Compose async generators into a pipeline where each stage transforms the stream without buffering — enabling parallel map/filter/transform with bounded memory.

```python
import asyncio
from anthropic import AsyncAnthropic
from typing import AsyncIterator, TypeVar, Callable, Awaitable

T = TypeVar("T")
U = TypeVar("U")


async def amap(
    gen: AsyncIterator[T],
    fn: Callable[[T], Awaitable[U]],
    concurrency: int = 5,
) -> AsyncIterator[U]:
    """Apply async fn to each item, preserving order with bounded concurrency."""
    sem = asyncio.Semaphore(concurrency)
    queue: asyncio.Queue = asyncio.Queue()

    async def process(item: T):
        async with sem:
            result = await fn(item)
            await queue.put(result)

    async def run():
        tasks = []
        async for item in gen:
            tasks.append(asyncio.create_task(process(item)))
        await asyncio.gather(*tasks, return_exceptions=True)
        await queue.put(None)

    asyncio.create_task(run())

    while True:
        item = await queue.get()
        if item is None:
            return
        yield item


async def afilter(
    gen: AsyncIterator[T],
    predicate: Callable[[T], bool],
) -> AsyncIterator[T]:
    """Filter items synchronously as they arrive."""
    async for item in gen:
        if predicate(item):
            yield item


async def abatch(
    gen: AsyncIterator[T],
    size: int,
) -> AsyncIterator[list[T]]:
    """Group items into batches of `size`."""
    batch: list[T] = []
    async for item in gen:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


# --- Pipeline stages ---

async def source_documents(count: int) -> AsyncIterator[dict]:
    """Generate source items."""
    for i in range(count):
        yield {"id": f"doc_{i}", "text": f"Doc {i}: content about AI agent performance.", "score": i * 0.1}
        await asyncio.sleep(0)  # Yield control


async def classify_document(client: AsyncAnthropic, doc: dict) -> dict:
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=20,
        system="Reply with one word: technical or general.",
        messages=[{"role": "user", "content": doc["text"]}],
    )
    doc["classification"] = response.content[0].text.strip().lower()
    return doc


async def summarize_document(client: AsyncAnthropic, doc: dict) -> dict:
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        messages=[{"role": "user", "content": f"Summarize: {doc['text']}"}],
    )
    doc["summary"] = response.content[0].text
    return doc


# Usage
async def main():
    client = AsyncAnthropic()

    # Pipeline: source → classify → filter technical → summarize → batch
    pipeline = source_documents(8)
    pipeline = amap(pipeline, lambda d: classify_document(client, d), concurrency=4)
    pipeline = afilter(pipeline, lambda d: "technical" in d.get("classification", ""))
    pipeline = amap(pipeline, lambda d: summarize_document(client, d), concurrency=3)
    pipeline = abatch(pipeline, size=3)

    async for batch in pipeline:
        print(f"\nBatch of {len(batch)} technical docs:")
        for doc in batch:
            print(f"  [{doc['id']}] {doc.get('summary', '')[:60]}")

asyncio.run(main())
```

## Solution 3: Merge Multiple Async Generators

Combine results from N concurrent async generators into a single stream — receiving results from the fastest generator first without waiting for slower ones.

```python
import asyncio
from anthropic import AsyncAnthropic
from dataclasses import dataclass
from typing import AsyncIterator


@dataclass
class SourcedResult:
    source: str
    item_id: str
    text: str


async def amerge(*generators: AsyncIterator) -> AsyncIterator:
    """Merge multiple async generators, yielding whichever produces next."""
    queue: asyncio.Queue = asyncio.Queue()
    active = len(generators)

    async def drain(gen: AsyncIterator, label: str):
        nonlocal active
        try:
            async for item in gen:
                await queue.put(item)
        finally:
            active -= 1
            await queue.put(None)  # Signal this generator is done

    for i, gen in enumerate(generators):
        asyncio.create_task(drain(gen, f"gen_{i}"))

    finished = 0
    while finished < len(generators):
        item = await queue.get()
        if item is None:
            finished += 1
        else:
            yield item


async def model_source(
    client: AsyncAnthropic,
    source_name: str,
    items: list[dict],
    model: str,
    max_tokens: int = 64,
) -> AsyncIterator[SourcedResult]:
    sem = asyncio.Semaphore(3)

    async def process(item: dict):
        async with sem:
            response = await client.messages.create(
                model=model,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": item["text"]}],
            )
            return SourcedResult(source_name, item["id"], response.content[0].text)

    queue: asyncio.Queue = asyncio.Queue()
    tasks = [asyncio.create_task(process(item)) for item in items]

    async def collect():
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for r in results:
            if not isinstance(r, Exception):
                await queue.put(r)
        await queue.put(None)

    asyncio.create_task(collect())

    while True:
        item = await queue.get()
        if item is None:
            return
        yield item


# Usage — merge results from two model sources simultaneously
async def main():
    client = AsyncAnthropic()

    items_a = [{"id": f"a_{i}", "text": f"Question A{i}: what is caching?"} for i in range(4)]
    items_b = [{"id": f"b_{i}", "text": f"Question B{i}: what is sharding?"} for i in range(4)]

    source_a = model_source(client, "haiku", items_a, "claude-haiku-4-5-20251001")
    source_b = model_source(client, "haiku2", items_b, "claude-haiku-4-5-20251001")

    import time
    start = time.monotonic()

    async for result in amerge(source_a, source_b):
        elapsed = (time.monotonic() - start) * 1000
        print(f"[+{elapsed:.0f}ms] [{result.source}] {result.item_id}: {result.text[:50]}")

asyncio.run(main())
```

## Solution 4: Windowed Streaming Aggregator

Buffer a sliding window of streamed results, emit aggregates (counts, averages, top-K) as new results arrive — enabling real-time dashboards without waiting for batch completion.

```python
import asyncio
import time
from anthropic import AsyncAnthropic
from collections import deque
from dataclasses import dataclass, field
from typing import AsyncIterator


@dataclass
class ScoredResult:
    item_id: str
    text: str
    score: float  # e.g., output length as quality proxy
    timestamp: float = field(default_factory=time.monotonic)


@dataclass
class WindowAggregate:
    window_size: int
    count: int
    avg_score: float
    max_score: float
    min_score: float
    top_item: str


async def streaming_window_aggregator(
    source: AsyncIterator[ScoredResult],
    window_size: int = 5,
    emit_every: int = 1,
) -> AsyncIterator[WindowAggregate]:
    """Yield window aggregates as results stream in."""
    window: deque[ScoredResult] = deque(maxlen=window_size)
    count = 0

    async for result in source:
        window.append(result)
        count += 1

        if count % emit_every == 0 and len(window) >= 1:
            scores = [r.score for r in window]
            top = max(window, key=lambda r: r.score)
            yield WindowAggregate(
                window_size=len(window),
                count=count,
                avg_score=sum(scores) / len(scores),
                max_score=max(scores),
                min_score=min(scores),
                top_item=top.item_id,
            )


async def scored_inference_stream(
    client: AsyncAnthropic,
    items: list[dict],
    model: str = "claude-haiku-4-5-20251001",
    concurrency: int = 5,
) -> AsyncIterator[ScoredResult]:
    sem = asyncio.Semaphore(concurrency)
    queue: asyncio.Queue = asyncio.Queue()

    async def process(item: dict):
        async with sem:
            response = await client.messages.create(
                model=model,
                max_tokens=128,
                messages=[{"role": "user", "content": item["text"]}],
            )
            text = response.content[0].text
            score = min(len(text) / 100.0, 1.0)  # Proxy quality score
            await queue.put(ScoredResult(item["id"], text, score))

    tasks = [asyncio.create_task(process(item)) for item in items]

    async def finish():
        await asyncio.gather(*tasks, return_exceptions=True)
        await queue.put(None)

    asyncio.create_task(finish())

    while True:
        item = await queue.get()
        if item is None:
            return
        yield item


# Usage
async def main():
    client = AsyncAnthropic()
    items = [
        {"id": f"q_{i}", "text": f"Question {i}: explain {'briefly' if i % 2 else 'in detail'} what REST is."}
        for i in range(10)
    ]

    source = scored_inference_stream(client, items)
    aggregator = streaming_window_aggregator(source, window_size=3, emit_every=2)

    async for agg in aggregator:
        print(f"[window={agg.window_size}, n={agg.count}] "
              f"avg={agg.avg_score:.2f} max={agg.max_score:.2f} "
              f"top={agg.top_item}")

asyncio.run(main())
```

## Solution 5: Backpressure-Aware Generator with Bounded Buffer

Implement a generator with configurable buffer size — if the consumer is slow, the producer blocks rather than accumulating unbounded results in memory.

```python
import asyncio
from anthropic import AsyncAnthropic
from dataclasses import dataclass
from typing import AsyncIterator


@dataclass
class BoundedResult:
    item_id: str
    text: str
    queue_depth_at_yield: int


class BackpressureGenerator:
    def __init__(
        self,
        buffer_size: int = 10,
        model: str = "claude-haiku-4-5-20251001",
        max_tokens: int = 128,
        concurrency: int = 5,
    ):
        self.buffer_size = buffer_size
        self.model = model
        self.max_tokens = max_tokens
        self.concurrency = concurrency
        self._total_produced = 0
        self._total_consumed = 0

    async def stream(
        self,
        client: AsyncAnthropic,
        items: list[dict],
        system_prompt: str = "Answer briefly.",
    ) -> AsyncIterator[BoundedResult]:
        # Bounded queue creates natural backpressure
        queue: asyncio.Queue = asyncio.Queue(maxsize=self.buffer_size)
        sem = asyncio.Semaphore(self.concurrency)

        async def produce(item: dict):
            async with sem:
                try:
                    response = await client.messages.create(
                        model=self.model,
                        max_tokens=self.max_tokens,
                        system=system_prompt,
                        messages=[{"role": "user", "content": item["text"]}],
                    )
                    result = BoundedResult(
                        item_id=item["id"],
                        text=response.content[0].text,
                        queue_depth_at_yield=queue.qsize(),
                    )
                    # This blocks if queue is full — backpressure!
                    await queue.put(result)
                    self._total_produced += 1
                except Exception as e:
                    print(f"[producer] Error for {item['id']}: {e}")

        async def run_producers():
            await asyncio.gather(*[produce(item) for item in items], return_exceptions=True)
            await queue.put(None)  # Sentinel

        asyncio.create_task(run_producers())

        while True:
            result = await queue.get()
            if result is None:
                break
            self._total_consumed += 1
            yield result

    def stats(self) -> dict:
        return {
            "produced": self._total_produced,
            "consumed": self._total_consumed,
        }


# Usage
async def main():
    client = AsyncAnthropic()
    generator = BackpressureGenerator(buffer_size=3, concurrency=5)

    items = [
        {"id": f"item_{i}", "text": f"What is concept {i}? Explain briefly."}
        for i in range(8)
    ]

    import time
    start = time.monotonic()

    async for result in generator.stream(client, items):
        elapsed = (time.monotonic() - start) * 1000
        print(f"[+{elapsed:.0f}ms] {result.item_id} (q={result.queue_depth_at_yield}): {result.text[:50]}")
        await asyncio.sleep(0.2)  # Slow consumer — triggers backpressure

    print(f"\nStats: {generator.stats()}")

asyncio.run(main())
```

## Solution 6: Multi-Stage Pipeline with Per-Stage Metrics

Build a named, metered pipeline where each stage reports throughput and latency — enabling identification of bottleneck stages without external profiling tools.

```python
import asyncio
import time
from anthropic import AsyncAnthropic
from dataclasses import dataclass, field
from typing import AsyncIterator, Callable, Awaitable, TypeVar

T = TypeVar("T")
U = TypeVar("U")


@dataclass
class StageMetrics:
    name: str
    items_in: int = 0
    items_out: int = 0
    items_dropped: int = 0
    total_latency_ms: float = 0.0

    @property
    def avg_latency_ms(self) -> float:
        return self.total_latency_ms / max(self.items_out, 1)

    @property
    def throughput_ratio(self) -> float:
        return self.items_out / max(self.items_in, 1)

    def report(self) -> dict:
        return {
            "stage": self.name,
            "in": self.items_in,
            "out": self.items_out,
            "dropped": self.items_dropped,
            "avg_latency_ms": round(self.avg_latency_ms, 1),
            "pass_rate": round(self.throughput_ratio, 2),
        }


class MeteredPipeline:
    def __init__(self):
        self._stages: list[StageMetrics] = []

    def metered_stage(
        self,
        name: str,
        fn: Callable[[T], Awaitable[U | None]],
        concurrency: int = 5,
    ) -> Callable[[AsyncIterator[T]], AsyncIterator[U]]:
        metrics = StageMetrics(name=name)
        self._stages.append(metrics)

        async def stage(source: AsyncIterator[T]) -> AsyncIterator[U]:
            sem = asyncio.Semaphore(concurrency)
            queue: asyncio.Queue = asyncio.Queue()

            async def process(item: T):
                metrics.items_in += 1
                start = time.monotonic()
                async with sem:
                    try:
                        result = await fn(item)
                        latency = (time.monotonic() - start) * 1000
                        metrics.total_latency_ms += latency
                        if result is not None:
                            metrics.items_out += 1
                            await queue.put(result)
                        else:
                            metrics.items_dropped += 1
                    except Exception as e:
                        metrics.items_dropped += 1
                        print(f"[{name}] Error: {e}")

            async def run():
                tasks = []
                async for item in source:
                    tasks.append(asyncio.create_task(process(item)))
                await asyncio.gather(*tasks, return_exceptions=True)
                await queue.put(None)

            asyncio.create_task(run())

            while True:
                item = await queue.get()
                if item is None:
                    return
                yield item

        return stage

    def metrics_report(self) -> list[dict]:
        return [s.report() for s in self._stages]


# Usage
async def main():
    client = AsyncAnthropic()
    pipeline = MeteredPipeline()

    # Define stage functions
    async def classify(item: dict) -> dict | None:
        r = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=10,
            system="Reply: technical or general. One word.",
            messages=[{"role": "user", "content": item["text"]}],
        )
        label = r.content[0].text.strip().lower()
        item["label"] = label
        return item if "technical" in label else None  # Filter non-technical

    async def summarize(item: dict) -> dict:
        r = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=64,
            messages=[{"role": "user", "content": f"Summarize: {item['text']}"}],
        )
        item["summary"] = r.content[0].text
        return item

    # Build metered pipeline
    classify_stage = pipeline.metered_stage("classify", classify, concurrency=4)
    summarize_stage = pipeline.metered_stage("summarize", summarize, concurrency=3)

    async def source() -> AsyncIterator[dict]:
        for i in range(10):
            yield {"id": f"doc_{i}", "text": f"Document {i}: {'API design principles' if i % 2 else 'Weekend trip ideas'}."}

    results = []
    async for item in summarize_stage(classify_stage(source())):
        results.append(item)
        print(f"[{item['id']}|{item['label']}] {item.get('summary', '')[:60]}")

    print(f"\nPipeline metrics:")
    for report in pipeline.metrics_report():
        print(f"  {report}")

asyncio.run(main())
```

## Comparison

| Approach | Memory Bound | Order Preserved | Composable | Backpressure | Best For |
|---|---|---|---|---|---|
| Basic Async Generator | Yes | No (arrival order) | Yes | No | Simple bulk inference streaming |
| Chained Map/Filter | Yes | No | Yes | No | Multi-stage ETL pipelines |
| Merged Generators | Yes | No | Yes | No | Multi-source fan-in |
| Windowed Aggregator | Yes | N/A | Yes | No | Real-time analytics dashboards |
| Backpressure-Aware | Yes | No | Partial | Yes | Slow consumer / bounded memory |
| Metered Pipeline | Yes | No | Yes | No | Production observability needs |
