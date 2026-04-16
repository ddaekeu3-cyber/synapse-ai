---
title: "Agent Doesn't Implement Streaming Aggregation for Parallel Responses"
description: "Agents that run multiple LLM calls in parallel wait for all streams to complete before processing any output; streaming aggregation delivers tokens from each stream as they arrive, reducing time-to-first-token for downstream consumers."
category: performance
difficulty: advanced
tags: [streaming, aggregation, parallel, asyncio, time-to-first-token, latency, async-generator]
---

# Agent Doesn't Implement Streaming Aggregation for Parallel Responses

## Problem

An agent running N parallel LLM calls with `asyncio.gather()` waits for the slowest call to finish before returning any output. If one call takes 5 seconds, the user sees nothing for 5 seconds even though 4 other calls finished in 1 second. Streaming aggregation delivers tokens from each stream as they arrive — a downstream consumer can start processing the first completed stream within ~300ms while others are still generating. This is especially valuable for fan-out agents (one question → multiple model calls → merged answer) where individual streams can be processed independently.

## Solution 1: Round-Robin Stream Merger — Deliver Tokens as They Arrive

Merge N concurrent async streams into a single output iterator that yields events from whichever stream produces next.

```python
import asyncio
from typing import AsyncIterator, Any
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

async def stream_to_queue(
    stream_coro,
    queue: asyncio.Queue,
    stream_id: int,
    sentinel: object,
) -> None:
    """Feed a streaming LLM call into a shared queue."""
    try:
        async with await stream_coro as stream:
            async for event in stream:
                await queue.put((stream_id, event))
    except Exception as exc:
        await queue.put((stream_id, exc))
    finally:
        await queue.put((stream_id, sentinel))

async def merge_streams(
    stream_coros: list,
    buffer_size: int = 256,
) -> AsyncIterator[tuple[int, Any]]:
    """
    Merge N streaming LLM calls into one async iterator.
    Yields (stream_id, event) in arrival order — fastest streams first.
    """
    DONE = object()
    queue: asyncio.Queue = asyncio.Queue(buffer_size)

    feeders = [
        asyncio.create_task(stream_to_queue(coro, queue, i, DONE))
        for i, coro in enumerate(stream_coros)
    ]

    active = len(stream_coros)
    while active > 0:
        stream_id, item = await queue.get()
        if item is DONE:
            active -= 1
        elif isinstance(item, Exception):
            yield stream_id, item
            active -= 1
        else:
            yield stream_id, item

    for feeder in feeders:
        feeder.cancel()

async def parallel_streaming_agent(
    questions: list[str],
) -> AsyncIterator[dict]:
    """
    Fan out to N parallel streams; yield tokens from whichever arrives first.
    """
    stream_coros = [
        client.messages.stream(
            model="claude-haiku-4-5-20251001",
            max_tokens=128,
            messages=[{"role": "user", "content": q}],
        )
        for q in questions
    ]

    stream_texts: dict[int, list[str]] = {i: [] for i in range(len(questions))}

    async for stream_id, event in merge_streams(stream_coros):
        if isinstance(event, Exception):
            yield {"stream_id": stream_id, "error": str(event)}
            continue
        if hasattr(event, "type") and event.type == "content_block_delta":
            if hasattr(event.delta, "text"):
                text = event.delta.text
                stream_texts[stream_id].append(text)
                yield {
                    "stream_id": stream_id,
                    "question": questions[stream_id],
                    "token": text,
                    "event_type": "token",
                }
        elif hasattr(event, "type") and event.type == "message_stop":
            full_text = "".join(stream_texts[stream_id])
            yield {
                "stream_id": stream_id,
                "question": questions[stream_id],
                "full_response": full_text,
                "event_type": "complete",
            }

# Usage:
async def demo():
    questions = [
        "What is quantum entanglement?",
        "Explain recursion simply.",
        "What causes rainbows?",
    ]
    async for event in parallel_streaming_agent(questions):
        if event.get("event_type") == "complete":
            print(f"[stream {event['stream_id']}] DONE: {event['full_response'][:60]}")
        elif event.get("event_type") == "token":
            print(f"[stream {event['stream_id']}] {event['token']}", end="", flush=True)
```

**When to use**: Fan-out agents (one user query → multiple specialized sub-queries answered in parallel). Streaming aggregation means the user sees the first answer within ~300ms instead of waiting for the slowest.

---

## Solution 2: First-Complete Wins — Return the Fastest Stream

When multiple model calls answer the same question, return the first complete response and cancel the rest.

```python
import asyncio
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

async def stream_to_completion(stream_coro, label: str) -> tuple[str, str]:
    """Stream a single call to completion. Returns (label, full_text)."""
    parts = []
    async with await stream_coro as stream:
        async for text in stream.text_stream:
            parts.append(text)
    return label, "".join(parts)

async def fastest_stream(
    prompts_with_labels: list[tuple[str, str]],
    model: str = "claude-haiku-4-5-20251001",
) -> tuple[str, str]:
    """
    Run multiple identical (or similar) queries in parallel.
    Return the label and text of whichever completes first.
    Cancel the remaining streams to save tokens.
    """
    tasks = [
        asyncio.create_task(
            stream_to_completion(
                client.messages.stream(
                    model=model,
                    max_tokens=256,
                    messages=[{"role": "user", "content": prompt}],
                ),
                label,
            )
        )
        for label, prompt in prompts_with_labels
    ]

    # Return as soon as any one completes
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

    # Cancel the losers — they've used tokens but we don't need their output
    for task in pending:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    label, text = next(iter(done)).result()
    return label, text

async def hedged_agent_call(user_message: str) -> dict:
    """
    Send the same prompt to two models simultaneously; use whichever replies first.
    This is request hedging applied to LLM streaming.
    """
    label, text = await fastest_stream([
        ("haiku",  user_message),
        ("sonnet", user_message),
    ])
    return {"response": text, "won_by": label}
```

**When to use**: Latency-sensitive agents where tail latency matters. The hedged pattern reduces p99 latency by 30–50% when one model has occasional slow responses.

---

## Solution 3: Streaming Pipeline — Process Each Completed Stream Independently

Rather than collecting all streams then merging, pass each completed stream to a downstream processor as soon as it finishes.

```python
import asyncio
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

async def stream_complete(stream_coro) -> str:
    """Collect a streaming call to completion."""
    parts = []
    async with await stream_coro as stream:
        async for text in stream.text_stream:
            parts.append(text)
    return "".join(parts)

async def streaming_pipeline(
    queries: list[str],
    processor,
    max_concurrent: int = 3,
) -> list[dict]:
    """
    Process each query through:
    1. LLM streaming call
    2. processor (runs as soon as the stream completes)

    Concurrency-limited to max_concurrent simultaneous streams.
    Downstream processing starts as each stream finishes — no waiting for others.
    """
    sem = asyncio.Semaphore(max_concurrent)
    results = []
    results_lock = asyncio.Lock()

    async def run_one(query: str, index: int) -> None:
        async with sem:
            text = await stream_complete(
                client.messages.stream(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=128,
                    messages=[{"role": "user", "content": query}],
                )
            )
        # Process immediately after stream completes — don't wait for other streams
        processed = await processor(query, text)
        async with results_lock:
            results.append({"index": index, "query": query, "result": processed})

    await asyncio.gather(*[run_one(q, i) for i, q in enumerate(queries)])
    return sorted(results, key=lambda x: x["index"])

# Example processor: summarize and classify
async def summarize_and_classify(query: str, response: str) -> dict:
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        messages=[{
            "role": "user",
            "content": f"In 5 words: {response}\nSentiment (positive/neutral/negative):",
        }],
    )
    return {"summary": response[:100], "meta": resp.content[0].text}

async def demo():
    queries = [f"Explain concept {i} briefly." for i in range(10)]
    results = await streaming_pipeline(queries, summarize_and_classify, max_concurrent=3)
    return results
```

**When to use**: Agents with a processing step after each LLM call (e.g., classify → route → respond). Pipeline parallelism hides the latency of downstream processing behind concurrent LLM calls.

---

## Solution 4: Token Budget Distribution — Allocate Max Tokens Proportionally

When running multiple parallel streams with a total token budget, distribute the budget proportionally so fast streams don't exhaust the budget for slow ones.

```python
import asyncio
import math
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

def distribute_token_budget(
    n_streams: int,
    total_budget: int,
    priorities: list[float] | None = None,
) -> list[int]:
    """
    Allocate max_tokens across N parallel streams.
    Optional priorities (weights) allocate more tokens to higher-priority streams.
    """
    if priorities is None:
        priorities = [1.0] * n_streams

    total_weight = sum(priorities)
    raw = [total_budget * (p / total_weight) for p in priorities]
    allocated = [max(64, math.floor(r)) for r in raw]  # minimum 64 tokens per stream

    # Fix rounding to hit total exactly
    while sum(allocated) > total_budget:
        # Reduce the stream with the largest allocation
        max_idx = allocated.index(max(allocated))
        allocated[max_idx] -= 1

    return allocated

async def budget_aware_parallel_streams(
    queries: list[str],
    total_token_budget: int = 2048,
    priorities: list[float] | None = None,
) -> list[str]:
    """Run parallel streams with proportionally distributed token budgets."""
    allocations = distribute_token_budget(len(queries), total_token_budget, priorities)

    async def run_one(query: str, max_tokens: int) -> str:
        parts = []
        async with await client.messages.stream(
            model="claude-haiku-4-5-20251001",
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": query}],
        ) as stream:
            async for text in stream.text_stream:
                parts.append(text)
        return "".join(parts)

    results = await asyncio.gather(*[
        run_one(q, alloc)
        for q, alloc in zip(queries, allocations)
    ])
    return list(results)

async def demo():
    # High-priority query gets 3× token budget
    queries = ["Detailed analysis of market trends", "Quick summary: what is AI?", "Short hello"]
    priorities = [3.0, 1.0, 0.5]  # market trends gets ~67% of budget
    responses = await budget_aware_parallel_streams(queries, total_token_budget=1024, priorities=priorities)
    return [{"query": q, "response": r[:100]} for q, r in zip(queries, responses)]
```

**When to use**: Agents with a fixed context or cost budget across N parallel calls. Budget distribution prevents a low-priority stream from consuming tokens that a high-priority stream needed.

---

## Solution 5: Incremental Aggregation — Build the Final Answer as Streams Complete

For fan-out research agents, incrementally build the final answer as each sub-stream completes rather than waiting for all to finish.

```python
import asyncio
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

async def incremental_research_agent(
    main_question: str,
    sub_questions: list[str],
) -> AsyncIterator[dict]:
    """
    1. Fan out main_question into N sub-questions (already provided).
    2. Stream each sub-question answer.
    3. As each sub-answer arrives, incrementally synthesize a partial answer.
    4. Final synthesis when all sub-answers are in.
    """
    # Launch all sub-streams concurrently
    sub_answer_queue: asyncio.Queue = asyncio.Queue()
    DONE = object()

    async def fetch_sub_answer(idx: int, question: str) -> None:
        parts = []
        async with await client.messages.stream(
            model="claude-haiku-4-5-20251001",
            max_tokens=128,
            messages=[{"role": "user", "content": question}],
        ) as stream:
            async for text in stream.text_stream:
                parts.append(text)
        await sub_answer_queue.put((idx, question, "".join(parts)))

    feeders = [
        asyncio.create_task(fetch_sub_answer(i, q))
        for i, q in enumerate(sub_questions)
    ]

    completed_answers: list[tuple[int, str, str]] = []
    remaining = len(sub_questions)

    while remaining > 0:
        idx, question, answer = await sub_answer_queue.get()
        completed_answers.append((idx, question, answer))
        remaining -= 1

        # Yield intermediate synthesis as each answer arrives
        partial_context = "\n".join(
            f"Q{i}: {q}\nA{i}: {a}"
            for i, q, a in sorted(completed_answers)
        )
        partial_resp = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=64,
            messages=[{
                "role": "user",
                "content": f"Partial research context:\n{partial_context}\n\nBrief partial answer to: {main_question}",
            }],
        )
        yield {
            "answers_so_far": len(completed_answers),
            "total_answers": len(sub_questions),
            "partial_answer": partial_resp.content[0].text,
            "is_final": remaining == 0,
        }

    for f in feeders:
        if not f.done():
            f.cancel()

# Re-declare with proper typing for Python
from typing import AsyncIterator as AI

async def demo():
    main_q = "What are the key factors driving AI adoption in enterprise?"
    sub_qs = [
        "What business benefits drive enterprise AI adoption?",
        "What technical barriers slow enterprise AI adoption?",
        "What governance challenges affect enterprise AI adoption?",
    ]
    async for update in incremental_research_agent(main_q, sub_qs):
        status = "FINAL" if update["is_final"] else f"partial ({update['answers_so_far']}/{update['total_answers']})"
        print(f"[{status}] {update['partial_answer'][:80]}")
```

**When to use**: Research agents that fan out to multiple sources. Incremental synthesis means the user sees a partial answer within seconds of the first sub-source completing, rather than waiting for the slowest.

---

## Solution 6: Backpressure-Aware Stream Buffer — Don't Overwhelm Slow Consumers

When a downstream consumer (WebSocket, UI, API) is slower than the aggregate token rate from N parallel streams, buffer with backpressure instead of dropping tokens.

```python
import asyncio
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

class BackpressureStreamBuffer:
    """
    Buffers tokens from N parallel LLM streams.
    Applies backpressure: if the consumer is slow, producers wait
    rather than dropping tokens or unboundedly buffering.
    """

    def __init__(self, capacity: int = 512):
        self._queue: asyncio.Queue = asyncio.Queue(capacity)
        self._done_count = 0
        self._total_streams = 0
        self._lock = asyncio.Lock()

    async def produce(self, stream_id: int, stream_coro) -> None:
        try:
            async with await stream_coro as stream:
                async for text in stream.text_stream:
                    # This will block if the queue is full (backpressure)
                    await self._queue.put({"stream_id": stream_id, "token": text, "done": False})
        except Exception as exc:
            await self._queue.put({"stream_id": stream_id, "error": str(exc), "done": True})
            return
        finally:
            async with self._lock:
                self._done_count += 1
                if self._done_count == self._total_streams:
                    await self._queue.put(None)  # termination sentinel
            await self._queue.put({"stream_id": stream_id, "token": "", "done": True})

    async def consume(self):
        """Async generator that yields tokens in arrival order."""
        while True:
            item = await self._queue.get()
            if item is None:
                return
            yield item

    async def run(self, stream_coros: list) -> "BackpressureStreamBuffer":
        self._total_streams = len(stream_coros)
        for i, coro in enumerate(stream_coros):
            asyncio.create_task(self.produce(i, coro))
        return self

async def slow_consumer_demo():
    """Simulate a slow WebSocket consumer that can only handle 10 tokens/sec."""
    queries = ["Explain machine learning.", "What is the internet?", "Define recursion."]

    buf = BackpressureStreamBuffer(capacity=32)
    await buf.run([
        client.messages.stream(
            model="claude-haiku-4-5-20251001",
            max_tokens=64,
            messages=[{"role": "user", "content": q}],
        )
        for q in queries
    ])

    token_count = 0
    async for item in buf.consume():
        if item.get("token"):
            token_count += 1
            # Simulate slow consumer (e.g., WebSocket with congestion)
            await asyncio.sleep(0.02)

    return {"tokens_delivered": token_count}
```

**When to use**: Agents serving streaming output to browser clients via WebSocket or SSE. Backpressure prevents memory blowout when the network is slow — producers slow down instead of buffering infinitely.

---

## Comparison

| Solution | Time-to-First-Token | Handles Slow Streams | Consumer Backpressure | Best For |
|---|---|---|---|---|
| Round-robin merger | Optimal | Yes | No | General fan-out streaming |
| First-complete wins | Optimal (one stream) | Yes (discards others) | No | Request hedging |
| Streaming pipeline | Per-stream | Yes | Semaphore | Batch processing |
| Token budget distribution | Same | Yes | No | Cost-constrained agents |
| Incremental aggregation | Per sub-answer | Yes | No | Research fan-out agents |
| Backpressure buffer | Optimal | Yes | Yes | Slow WebSocket consumers |

**Rule of thumb**: Use the round-robin merger (Solution 1) for general fan-out. Use first-complete wins (Solution 2) for request hedging. Add backpressure buffering (Solution 6) when serving streaming output to browser clients that may have slow connections.
