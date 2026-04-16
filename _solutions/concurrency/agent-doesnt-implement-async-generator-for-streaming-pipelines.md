---
layout: solution
title: "Agent Doesn't Implement Async Generator for Streaming Pipelines"
category: concurrency
description: "Use async generators to build composable, backpressure-aware streaming pipelines — transforming, filtering, and routing token streams between model calls without buffering entire responses in memory."
tags: [concurrency, async, streaming, generator, pipeline, python]
---

# Agent Doesn't Implement Async Generator for Streaming Pipelines

Agents that buffer entire model responses before processing them introduce latency, waste memory, and cannot compose multi-step streaming transforms. Async generators enable lazy, backpressure-aware pipelines that process tokens as they arrive — chaining transforms, filters, and routers with near-zero overhead.

## Option 1: Basic Async Generator Wrapping Stream

```python
import anthropic
import asyncio

client = anthropic.AsyncAnthropic()

async def stream_tokens(prompt: str, system: str = ""):
    """Async generator that yields text chunks from a streaming model call."""
    kwargs = {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 512,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        kwargs["system"] = system
    async with client.messages.stream(**kwargs) as stream:
        async for text in stream.text_stream:
            yield text

async def collect(gen) -> str:
    """Consume an async generator into a single string."""
    return "".join([chunk async for chunk in gen])

async def main():
    prompts = [
        "List 3 Python concurrency primitives.",
        "What does asyncio.gather() do?",
        "Name 2 uses of async generators.",
    ]
    for prompt in prompts:
        result = await collect(stream_tokens(prompt))
        print(f"Q: {prompt[:45]}")
        print(f"A: {result[:80]}\n")

asyncio.run(main())

# Expected Token Savings: No buffering overhead; tokens processed immediately as they arrive
# Environment: asyncio required; async generators compose with standard async for loops
```

## Option 2: Pipeline Transforms — Map, Filter, Chunk

```python
import anthropic
import asyncio
from typing import AsyncIterator

client = anthropic.AsyncAnthropic()

async def stream_tokens(prompt: str) -> AsyncIterator[str]:
    async with client.messages.stream(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        async for text in stream.text_stream:
            yield text

async def uppercase_transform(source: AsyncIterator[str]) -> AsyncIterator[str]:
    """Transform: uppercase all tokens."""
    async for token in source:
        yield token.upper()

async def filter_whitespace(source: AsyncIterator[str]) -> AsyncIterator[str]:
    """Filter: drop pure-whitespace tokens."""
    async for token in source:
        if token.strip():
            yield token

async def chunk_by_sentence(source: AsyncIterator[str], max_len: int = 80) -> AsyncIterator[str]:
    """Buffer tokens into sentence-length chunks."""
    buf = ""
    async for token in source:
        buf += token
        if any(c in buf for c in ".!?") and len(buf) >= max_len:
            yield buf.strip()
            buf = ""
    if buf.strip():
        yield buf.strip()

async def main():
    raw = stream_tokens("Explain Python asyncio in 3 sentences.")
    filtered = filter_whitespace(raw)
    chunked = chunk_by_sentence(filtered)

    print("Sentence chunks:")
    async for sentence in chunked:
        print(f"  [{len(sentence):3d}ch] {sentence[:70]}")

asyncio.run(main())

# Expected Token Savings: Zero-copy streaming; each transform adds ~0 memory overhead per token
# Environment: AsyncIterator type hints require Python 3.9+; compose as many transforms as needed
```

## Option 3: Fan-Out — Broadcast One Stream to Multiple Consumers

```python
import anthropic
import asyncio
from typing import AsyncIterator

client = anthropic.AsyncAnthropic()

async def stream_tokens(prompt: str) -> AsyncIterator[str]:
    async with client.messages.stream(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        async for text in stream.text_stream:
            yield text

async def broadcast(source: AsyncIterator[str], n: int):
    """Fan-out one source generator to n queues."""
    queues = [asyncio.Queue() for _ in range(n)]
    sentinel = object()

    async def producer():
        async for token in source:
            for q in queues:
                await q.put(token)
        for q in queues:
            await q.put(sentinel)

    async def consumer(queue: asyncio.Queue, name: str) -> str:
        chunks = []
        while True:
            item = await queue.get()
            if item is sentinel:
                break
            chunks.append(item)
        return "".join(chunks)

    prod_task = asyncio.create_task(producer())
    consumer_tasks = [
        asyncio.create_task(consumer(queues[i], f"consumer-{i}"))
        for i in range(n)
    ]
    await prod_task
    results = await asyncio.gather(*consumer_tasks)
    return results

async def main():
    source = stream_tokens("List 4 benefits of streaming APIs.")
    results = await broadcast(source, n=3)
    for i, r in enumerate(results):
        print(f"Consumer {i}: {r[:60]}")
    # All 3 get identical content from the same stream
    assert results[0] == results[1] == results[2]
    print("All consumers received identical stream ✓")

asyncio.run(main())

# Expected Token Savings: Single model call serves N consumers; no duplicate API requests
# Environment: asyncio; sentinel pattern is safe across queue types; extend with per-consumer transforms
```

## Option 4: Async Generator Pipeline with SQLite Audit Log

```python
import anthropic
import asyncio
import sqlite3
import time
from typing import AsyncIterator

client = anthropic.AsyncAnthropic()
DB = "stream_audit.db"

def init_db():
    con = sqlite3.connect(DB)
    con.execute("""
        CREATE TABLE IF NOT EXISTS stream_log (
            run_id TEXT, stage TEXT, tokens INTEGER,
            chars INTEGER, duration_ms REAL, ts REAL
        )
    """)
    con.commit(); con.close()

async def stream_tokens(prompt: str) -> AsyncIterator[str]:
    async with client.messages.stream(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        async for text in stream.text_stream:
            yield text

async def metered_stage(
    name: str, run_id: str, source: AsyncIterator[str]
) -> AsyncIterator[str]:
    """Wrap any stage with throughput metering logged to SQLite."""
    tokens = 0
    chars = 0
    t0 = time.monotonic()
    async for token in source:
        tokens += 1
        chars += len(token)
        yield token
    duration_ms = (time.monotonic() - t0) * 1000
    con = sqlite3.connect(DB)
    con.execute(
        "INSERT INTO stream_log VALUES (?,?,?,?,?,?)",
        (run_id, name, tokens, chars, duration_ms, time.time()),
    )
    con.commit(); con.close()

async def word_boundary_chunk(source: AsyncIterator[str]) -> AsyncIterator[str]:
    """Re-chunk token stream at word boundaries."""
    buf = ""
    async for token in source:
        buf += token
        if " " in buf or "\n" in buf:
            parts = buf.split(" ")
            for word in parts[:-1]:
                if word:
                    yield word + " "
            buf = parts[-1]
    if buf:
        yield buf

async def run_pipeline(prompt: str, run_id: str) -> str:
    init_db()
    raw     = stream_tokens(prompt)
    stage1  = metered_stage("raw",   run_id, raw)
    chunked = word_boundary_chunk(stage1)
    stage2  = metered_stage("words", run_id, chunked)

    result = "".join([t async for t in stage2])

    con = sqlite3.connect(DB)
    rows = con.execute(
        "SELECT stage, tokens, chars, duration_ms FROM stream_log WHERE run_id=?",
        (run_id,),
    ).fetchall()
    con.close()
    for row in rows:
        print(f"  [{row[0]:6s}] {row[1]:4d} tokens  {row[2]:5d} chars  {row[3]:.0f}ms")
    return result

async def main():
    result = await run_pipeline(
        "What are 3 advantages of using async generators in Python?",
        run_id="run-001",
    )
    print(f"\nOutput: {result[:100]}")

asyncio.run(main())

# Expected Token Savings: Per-stage metrics surface transform bottlenecks; no extra API calls
# Environment: SQLite audit log is append-only; run_id links stages for cross-stage analysis
```

## Option 5: Async Generator with Backpressure and Slow Consumer

```python
import anthropic
import asyncio
from typing import AsyncIterator

client = anthropic.AsyncAnthropic()

async def stream_tokens(prompt: str) -> AsyncIterator[str]:
    async with client.messages.stream(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        async for text in stream.text_stream:
            yield text

async def bounded_buffer(
    source: AsyncIterator[str],
    maxsize: int = 8,
) -> AsyncIterator[str]:
    """
    Bounded async queue between producer and consumer.
    Producer pauses when queue is full — true backpressure.
    """
    queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=maxsize)

    async def producer():
        async for token in source:
            await queue.put(token)  # blocks if queue full
        await queue.put(None)       # sentinel

    prod_task = asyncio.create_task(producer())
    while True:
        item = await queue.get()
        if item is None:
            break
        yield item
    await prod_task

async def slow_consumer(source: AsyncIterator[str], delay: float = 0.01) -> str:
    """Simulates a slow downstream consumer (e.g., writing to disk)."""
    chunks = []
    async for token in source:
        await asyncio.sleep(delay)  # simulate slow I/O
        chunks.append(token)
    return "".join(chunks)

async def main():
    # Without backpressure: producer races ahead, buffers everything
    # With bounded_buffer: producer blocks when consumer is slow
    prompt = "Explain backpressure in async streaming systems."
    raw    = stream_tokens(prompt)
    buffered = bounded_buffer(raw, maxsize=4)
    result = await slow_consumer(buffered, delay=0.005)
    print(f"Result ({len(result)} chars): {result[:100]}")

asyncio.run(main())

# Expected Token Savings: Backpressure prevents unbounded queue growth under slow consumers
# Environment: asyncio; maxsize=4-16 works well for network-bound consumers; tune to task profile
```

## Option 6: Multi-Stage Pipeline with Parallel Branch Merging

```python
import anthropic
import asyncio
from typing import AsyncIterator

client = anthropic.AsyncAnthropic()

async def stream_tokens(prompt: str, label: str = "") -> AsyncIterator[tuple[str, str]]:
    """Yield (label, token) tuples for multi-source merging."""
    async with client.messages.stream(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        async for text in stream.text_stream:
            yield (label, text)

async def merge_streams(*sources: AsyncIterator[tuple[str, str]]) -> AsyncIterator[tuple[str, str]]:
    """
    Merge N async generators into one, interleaved by arrival order.
    Uses asyncio tasks + a shared queue.
    """
    queue: asyncio.Queue[tuple[str, str] | None] = asyncio.Queue()
    active = len(sources)

    async def drain(source):
        async for item in source:
            await queue.put(item)
        await queue.put(None)

    tasks = [asyncio.create_task(drain(s)) for s in sources]
    finished = 0
    while finished < active:
        item = await queue.get()
        if item is None:
            finished += 1
        else:
            yield item
    await asyncio.gather(*tasks)

async def collect_labeled(gen: AsyncIterator[tuple[str, str]]) -> dict[str, str]:
    result: dict[str, str] = {}
    async for label, token in gen:
        result[label] = result.get(label, "") + token
    return result

async def main():
    s1 = stream_tokens("Define asyncio.gather() in one sentence.", "gather")
    s2 = stream_tokens("Define asyncio.Queue() in one sentence.",  "queue")
    s3 = stream_tokens("Define async generator in one sentence.",  "gen")

    merged = merge_streams(s1, s2, s3)
    results = await collect_labeled(merged)

    for label, text in results.items():
        print(f"[{label:6s}] {text.strip()[:80]}")

asyncio.run(main())

# Expected Token Savings: 3 parallel streams vs 3 sequential calls reduces wall-clock time ~3x
# Environment: asyncio; merge_streams works for any N sources; extend with priority queues if needed
```

## Comparison

| Option | Pattern | Backpressure | Composable | Audit |
|--------|---------|-------------|-----------|-------|
| 1 — Basic Wrapper | Raw stream → generator | Implicit | Yes | No |
| 2 — Map/Filter/Chunk | Transform pipeline | Implicit | Yes | No |
| 3 — Fan-Out Broadcast | Queue-based broadcast | Yes (queue) | Partial | No |
| 4 — Metered SQLite Audit | Stage timing + log | Implicit | Yes | Yes |
| 5 — Bounded Buffer | Backpressure queue | Yes (maxsize) | Yes | No |
| 6 — Parallel Branch Merge | Multi-source merge | Queue-based | Yes | No |
