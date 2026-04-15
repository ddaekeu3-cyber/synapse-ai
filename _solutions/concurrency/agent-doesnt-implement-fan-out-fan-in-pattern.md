---
layout: solution
title: "Agent Doesn't Implement Fan-Out Fan-In Pattern"
category: concurrency
description: "Agent processes a list of items sequentially when they could all be dispatched in parallel and their results aggregated, multiplying total latency by item count."
tags: [concurrency, asyncio, performance, parallelism, production]
---

## Symptom

Processing 20 items takes 40 seconds because the agent calls `client.messages.create()` for each item one at a time. All 20 API calls are independent — none depends on the result of another — but the agent runs them sequentially. Wall-clock time is 20× the per-item latency. Users wait, and the agent's throughput is bounded by a single serial pipeline rather than the API's actual concurrency capacity.

## Root Cause

Sequential processing is the natural default: `for item in items: result = process(item)`. Without explicit concurrency, each iteration blocks until the previous one completes. Developers familiar with sync code often don't reach for `asyncio.gather()` or `concurrent.futures` unless they've hit the performance wall. The fix is the fan-out/fan-in pattern: dispatch all work concurrently, then collect all results.

## Fix

### Option 1 — asyncio.gather() for pure fan-out/fan-in

```python
import asyncio
import time
import anthropic

client = anthropic.AsyncAnthropic()

async def process_item(item: str) -> dict:
    """Independent task — result doesn't depend on any other item."""
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        messages=[{"role": "user", "content": f"Classify this as positive/negative/neutral: {item}"}],
    )
    return {"item": item, "sentiment": response.content[0].text.strip()}

async def sequential(items: list[str]) -> list[dict]:
    """BAD: one at a time."""
    return [await process_item(item) for item in items]

async def fan_out_gather(items: list[str]) -> list[dict]:
    """GOOD: all at once, results collected in order."""
    return await asyncio.gather(*[process_item(item) for item in items])

async def main():
    items = [
        "The product is excellent!",
        "This is the worst purchase I've made.",
        "It works fine, nothing special.",
        "Absolutely love it, highly recommend!",
        "Disappointed — doesn't match the description.",
    ]

    t0 = time.monotonic()
    seq_results = await sequential(items)
    seq_ms = int((time.monotonic() - t0) * 1000)

    t0 = time.monotonic()
    par_results = await fan_out_gather(items)
    par_ms = int((time.monotonic() - t0) * 1000)

    print(f"Sequential: {seq_ms}ms | Parallel: {par_ms}ms | Speedup: {seq_ms/max(par_ms,1):.1f}×")
    print(f"Results: {[r['sentiment'] for r in par_results]}")

asyncio.run(main())
```

**Expected Token Savings:** Same total tokens; wall-clock time drops from N × latency to ~1 × latency; faster results reduce timeout-triggered retries and improve user experience.
**Environment:** Any list of independent Claude API calls; classification, summarisation, translation of multiple items.

---

### Option 2 — Semaphore-bounded fan-out to respect rate limits

```python
import asyncio
import time
import anthropic

client = anthropic.AsyncAnthropic()

async def fan_out_with_limit(
    items: list[str],
    max_concurrent: int = 5,
) -> list[dict]:
    """
    Fan out with a semaphore — at most max_concurrent requests in flight.
    Prevents hitting API rate limits while still parallelising.
    """
    sem = asyncio.Semaphore(max_concurrent)

    async def guarded_process(item: str) -> dict:
        async with sem:
            response = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=128,
                messages=[{"role": "user", "content": f"Summarise in one sentence: {item}"}],
            )
            return {"item": item[:40], "summary": response.content[0].text.strip()}

    t0 = time.monotonic()
    results = await asyncio.gather(*[guarded_process(item) for item in items])
    ms = int((time.monotonic() - t0) * 1000)
    print(f"[fan-out] {len(items)} items, max {max_concurrent} concurrent → {ms}ms")
    return list(results)

async def main():
    articles = [
        f"Article {i}: " + "Long content about topic " * 5
        for i in range(20)
    ]
    results = await fan_out_with_limit(articles, max_concurrent=5)
    print(f"Processed {len(results)} articles")
    for r in results[:3]:
        print(f"  {r['item']}: {r['summary'][:60]}")

asyncio.run(main())
```

**Expected Token Savings:** Semaphore prevents 429 rate-limit errors that would require retry delays; 5× concurrency gives ~5× throughput without exceeding API limits.
**Environment:** Batch processing with Anthropic API rate limits; production agents where uncontrolled fan-out would exhaust RPM quotas.

---

### Option 3 — Fan-out with partial failure handling

```python
import asyncio
import time
from dataclasses import dataclass
from typing import Any
import anthropic

client = anthropic.AsyncAnthropic()

@dataclass
class TaskResult:
    item: str
    success: bool
    value: Any = None
    error: str = ""

async def safe_process(item: str, sem: asyncio.Semaphore) -> TaskResult:
    async with sem:
        try:
            response = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=64,
                messages=[{"role": "user", "content": f"Extract the main number from: {item}"}],
            )
            return TaskResult(item=item, success=True, value=response.content[0].text.strip())
        except anthropic.RateLimitError:
            await asyncio.sleep(1)
            return TaskResult(item=item, success=False, error="rate_limited")
        except Exception as e:
            return TaskResult(item=item, success=False, error=str(e))

async def fan_out_with_error_handling(items: list[str]) -> dict:
    """
    Fan out all items; collect both successes and failures.
    Never raises — returns a summary with success/failure breakdown.
    """
    sem = asyncio.Semaphore(8)
    tasks = [asyncio.create_task(safe_process(item, sem)) for item in items]
    results: list[TaskResult] = await asyncio.gather(*tasks)

    successes = [r for r in results if r.success]
    failures  = [r for r in results if not r.success]

    return {
        "total":    len(items),
        "success":  len(successes),
        "failed":   len(failures),
        "results":  [(r.item[:30], r.value) for r in successes],
        "errors":   [(r.item[:30], r.error) for r in failures],
    }

async def main():
    items = [
        "The temperature reached 42 degrees.",
        "Revenue grew by 15% year-over-year.",
        "3 out of 5 reviewers recommended it.",
        "This contains no numbers whatsoever.",
        "The deadline is in 7 days.",
    ]
    summary = await fan_out_with_error_handling(items)
    print(f"[fan-in] {summary['success']}/{summary['total']} succeeded")
    for item, value in summary["results"]:
        print(f"  {item}: {value}")

asyncio.run(main())
```

**Expected Token Savings:** Partial failures don't abort the entire batch — successful results are returned immediately; only failed items need to be retried, reducing total token spend on error recovery.
**Environment:** Agents where some items are expected to fail (noisy input data, transient API errors); production batch jobs that must report partial success.

---

### Option 4 — Ordered fan-out with chunked batches

```python
import asyncio
import time
import anthropic

client = anthropic.AsyncAnthropic()

async def process_chunk(chunk: list[str], chunk_idx: int) -> list[dict]:
    """Fan-out within a chunk, then return chunk results in order."""
    async def process_one(item: str, idx: int) -> dict:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=32,
            messages=[{"role": "user", "content": f"One-word topic: {item}"}],
        )
        return {"chunk": chunk_idx, "idx": idx, "item": item[:30], "topic": response.content[0].text.strip()}

    return await asyncio.gather(*[process_one(item, i) for i, item in enumerate(chunk)])

async def chunked_fan_out(
    items: list[str],
    chunk_size: int = 5,
) -> list[dict]:
    """
    Fan out in chunks: each chunk fans out fully, chunks execute sequentially.
    Useful for ordered processing with bounded parallelism per batch.
    """
    chunks = [items[i:i+chunk_size] for i in range(0, len(items), chunk_size)]
    all_results: list[dict] = []

    t0 = time.monotonic()
    for chunk_idx, chunk in enumerate(chunks):
        chunk_results = await process_chunk(chunk, chunk_idx)
        all_results.extend(chunk_results)
        elapsed = int((time.monotonic() - t0) * 1000)
        print(f"[fan-out] chunk {chunk_idx+1}/{len(chunks)} done ({elapsed}ms elapsed)")

    return all_results

async def main():
    items = [f"Article about {topic}" for topic in
             ["AI", "climate", "finance", "health", "tech",
              "sports", "culture", "science", "travel", "food"]]

    results = await chunked_fan_out(items, chunk_size=5)
    print(f"\nProcessed {len(results)} items in {len(results)//5} chunks")
    for r in results:
        print(f"  chunk={r['chunk']} idx={r['idx']}: {r['topic']}")

asyncio.run(main())
```

**Expected Token Savings:** Chunked fan-out provides 5× speedup per chunk while keeping memory and API pressure bounded; chunk-sequential execution makes progress visible and allows checkpoint-per-chunk.
**Environment:** Large batches (1000+ items) where unbounded fan-out would overwhelm the API or exhaust memory; ordered processing requirements.

---

### Option 5 — asyncio.as_completed() for streaming fan-in results

```python
import asyncio
import time
import anthropic

client = anthropic.AsyncAnthropic()

async def stream_fan_in(items: list[str]) -> None:
    """
    Fan out all tasks, but process each result as soon as it arrives
    rather than waiting for all tasks to finish.
    Useful when results can be acted on immediately (streaming to user, writing to DB).
    """
    sem = asyncio.Semaphore(8)

    async def process(item: str) -> dict:
        async with sem:
            response = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=64,
                messages=[{"role": "user", "content": f"Rate quality 1-10: {item}"}],
            )
            return {"item": item[:40], "rating": response.content[0].text.strip()}

    t0 = time.monotonic()
    tasks = [asyncio.create_task(process(item)) for item in items]
    completed = 0

    # Process results as they arrive — no waiting for slowest task
    for coro in asyncio.as_completed(tasks):
        result = await coro
        completed += 1
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        print(f"[fan-in] {completed}/{len(items)} done at {elapsed_ms}ms: "
              f"{result['item']} → {result['rating']}")

    total_ms = int((time.monotonic() - t0) * 1000)
    print(f"[fan-in] all {len(items)} results collected in {total_ms}ms")

async def main():
    reviews = [
        f"Review {i}: {'great ' if i % 3 else 'poor '} product quality"
        for i in range(8)
    ]
    await stream_fan_in(reviews)

asyncio.run(main())
```

**Expected Token Savings:** First result is available as soon as the fastest task completes — no head-of-line blocking on slow items; streaming fan-in reduces time-to-first-result by the variance in per-item latency.
**Environment:** Real-time dashboards, streaming UIs, pipeline stages where downstream processing can start on partial results.

---

### Option 6 — ProcessPoolExecutor for CPU-bound pre/post processing

```python
import asyncio
import time
import concurrent.futures
import json
import anthropic

client = anthropic.AsyncAnthropic()

# CPU-bound pre-processing (tokenisation, parsing, validation)
def cpu_preprocess(raw_item: str) -> dict:
    """Heavy CPU work — runs in a process pool, not the asyncio event loop."""
    import hashlib
    import re
    words     = re.findall(r'\w+', raw_item.lower())
    word_count = len(words)
    checksum  = hashlib.md5(raw_item.encode()).hexdigest()[:8]
    return {"text": raw_item, "word_count": word_count, "checksum": checksum}

def cpu_postprocess(item: dict, llm_output: str) -> dict:
    """CPU-bound result formatting and validation."""
    return {
        "checksum":   item["checksum"],
        "word_count": item["word_count"],
        "summary":    llm_output.strip()[:100],
        "valid":      len(llm_output.strip()) > 10,
    }

async def hybrid_fan_out(raw_items: list[str]) -> list[dict]:
    """
    Fan-out pattern with CPU-bound work in ProcessPoolExecutor
    and I/O-bound LLM calls in asyncio.
    """
    loop = asyncio.get_running_loop()
    sem  = asyncio.Semaphore(5)

    with concurrent.futures.ProcessPoolExecutor(max_workers=4) as pool:
        # Phase 1: CPU pre-processing fan-out (parallel, off event loop)
        preprocessed: list[dict] = await asyncio.gather(*[
            loop.run_in_executor(pool, cpu_preprocess, item)
            for item in raw_items
        ])

        # Phase 2: LLM API fan-out (async, bounded by semaphore)
        async def llm_call(item: dict) -> dict:
            async with sem:
                response = await client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=64,
                    messages=[{"role": "user", "content": f"Summarise ({item['word_count']} words): {item['text']}"}],
                )
                llm_out = response.content[0].text
            # Phase 3: CPU post-processing (back in pool)
            return await loop.run_in_executor(pool, cpu_postprocess, item, llm_out)

        results = await asyncio.gather(*[llm_call(item) for item in preprocessed])

    return list(results)

async def main():
    documents = [
        f"Document {i}: " + ("content about science and technology " * (i % 3 + 1))
        for i in range(10)
    ]
    t0 = time.monotonic()
    results = await hybrid_fan_out(documents)
    ms = int((time.monotonic() - t0) * 1000)
    print(f"[hybrid] {len(results)} documents in {ms}ms")
    for r in results[:3]:
        print(f"  [{r['checksum']}] valid={r['valid']}: {r['summary'][:50]}")

asyncio.run(main())
```

**Expected Token Savings:** Off-loading CPU preprocessing to process pool prevents it from blocking the event loop during API calls; pipeline throughput is maximised when CPU and I/O phases overlap.
**Environment:** Agents with heavy pre-processing (PDF parsing, image resizing, tokenisation) followed by LLM calls; data pipelines with both compute-intensive and I/O-intensive stages.

---

## Comparison

| Option | Concurrency Model | Result Order | Partial Failure | Backpressure | Best For |
|---|---|---|---|---|---|
| 1. asyncio.gather() | Unlimited async | Preserved | Raises on first | No | Small, safe batches (< 20 items) |
| 2. Semaphore-bounded | Limited async | Preserved | Raises on first | Via semaphore | Rate-limit-aware production batches |
| 3. Partial failure | Semaphore + exception catch | Preserved | Collected | Via semaphore | Noisy inputs; must report partial success |
| 4. Chunked batches | Chunk-parallel | Preserved | Per-chunk | Chunk size | Large batches; checkpoint-per-chunk |
| 5. as_completed() | Semaphore + streaming | Arrival order | Per-task | Via semaphore | Streaming dashboards; fastest first result |
| 6. ProcessPoolExecutor | CPU pool + asyncio | Preserved | Raises | Via semaphore | CPU-heavy pre/post processing pipelines |
