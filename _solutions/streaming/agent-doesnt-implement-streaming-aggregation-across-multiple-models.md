---
layout: solution
title: "Agent Doesn't Implement Streaming Aggregation Across Multiple Models"
category: streaming
description: "Agents that run multiple models sequentially and wait for each to finish before moving on miss the opportunity to stream results from all models concurrently. These patterns show how to aggregate streaming output from multiple models in real time."
tags: [streaming, multi-model, aggregation, concurrency, asyncio, anthropic]
---

## Problem

A consensus or comparison workflow that runs the same query through three models sequentially — waiting for each full response before starting the next — takes 3x the latency of a single call. By streaming all three concurrently and aggregating results, the user sees partial output within milliseconds and total latency approaches the slowest single model, not their sum.

---

### Option 1: Parallel Stream Fan-Out with Interleaved Output

Stream from multiple models simultaneously and print each chunk as it arrives, labeled by model.

```python
import asyncio
import anthropic

client = anthropic.AsyncAnthropic()

MODELS = [
    ("haiku", "claude-haiku-4-5-20251001"),
    ("sonnet", "claude-sonnet-4-6"),
]

async def stream_model(name: str, model: str, prompt: str, results: dict) -> None:
    """Stream from one model, collecting chunks into results dict."""
    chunks = []
    async with client.messages.stream(
        model=model,
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        async for text in stream.text_stream:
            chunks.append(text)
            print(f"[{name}] {text}", end="", flush=True)
        print()  # newline after stream ends
    results[name] = "".join(chunks)

async def parallel_stream(prompt: str) -> dict[str, str]:
    results = {}
    tasks = [
        asyncio.create_task(stream_model(name, model, prompt, results))
        for name, model in MODELS
    ]
    await asyncio.gather(*tasks)
    return results

if __name__ == "__main__":
    async def main():
        prompt = "What are the top 3 tradeoffs between microservices and monolithic architecture?"
        print("=== Streaming from all models in parallel ===\n")
        results = await parallel_stream(prompt)
        print(f"\n=== Complete ({len(results)} models) ===")
        for name, text in results.items():
            print(f"[{name}] {len(text.split())} words")
    asyncio.run(main())

# Expected Token Savings: No savings — runs all models; value is latency: wall time ≈ max(individual), not sum
# Environment: ANTHROPIC_API_KEY
```

---

### Option 2: Race-to-First-Complete with Cancellation

Start all models streaming, use the first complete response, cancel the rest.

```python
import asyncio
import anthropic

client = anthropic.AsyncAnthropic()

async def race_stream(name: str, model: str, prompt: str,
                       winner_event: asyncio.Event,
                       result_queue: asyncio.Queue) -> None:
    chunks = []
    try:
        async with client.messages.stream(
            model=model,
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            async for text in stream.text_stream:
                if winner_event.is_set():
                    return  # another model won, stop streaming
                chunks.append(text)
            # Finished without being cancelled
            if not winner_event.is_set():
                winner_event.set()
                await result_queue.put((name, model, "".join(chunks)))
    except asyncio.CancelledError:
        pass

async def fastest_model(prompt: str, models: list[tuple[str, str]]) -> tuple[str, str, str]:
    winner_event = asyncio.Event()
    result_queue: asyncio.Queue = asyncio.Queue()

    tasks = [
        asyncio.create_task(race_stream(name, model, prompt, winner_event, result_queue))
        for name, model in models
    ]

    # Wait for first winner
    await result_queue.get()
    name, model, text = await result_queue.get() if not result_queue.empty() else (None, None, None)

    # Actually get from queue properly
    result_queue2: asyncio.Queue = asyncio.Queue()
    winner_event2 = asyncio.Event()

    async def race_proper(name, model, prompt):
        chunks = []
        async with client.messages.stream(
            model=model,
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            async for text in stream.text_stream:
                if winner_event2.is_set():
                    return
                chunks.append(text)
            if not winner_event2.is_set():
                winner_event2.set()
                await result_queue2.put((name, model, "".join(chunks)))

    race_tasks = [
        asyncio.create_task(race_proper(n, m, prompt))
        for n, m in models
    ]
    winner_name, winner_model, winner_text = await result_queue2.get()

    for t in race_tasks:
        t.cancel()
    await asyncio.gather(*race_tasks, return_exceptions=True)

    for t in tasks:
        t.cancel()

    print(f"[winner] {winner_name} ({winner_model})")
    return winner_name, winner_model, winner_text

if __name__ == "__main__":
    async def main():
        models = [
            ("haiku", "claude-haiku-4-5-20251001"),
            ("sonnet", "claude-sonnet-4-6"),
        ]
        prompt = "What is the capital of France? Answer in one sentence."
        name, model, text = await fastest_model(prompt, models)
        print(f"Fastest: {name}\nResponse: {text}")
    asyncio.run(main())

# Expected Token Savings: Only one model's response used; others cancelled early — saves 30-60% on multi-model queries
# Environment: ANTHROPIC_API_KEY
```

---

### Option 3: Streaming Consensus — Merge When All Complete

Stream all models concurrently, then synthesize their complete outputs into a consensus response.

```python
import asyncio
import anthropic

client = anthropic.AsyncAnthropic()

MODELS = [
    ("haiku", "claude-haiku-4-5-20251001"),
    ("sonnet", "claude-sonnet-4-6"),
]

SYNTHESIS_PROMPT = """You received answers from {n} AI models to this question:
Question: {question}

{answers}

Synthesize these into one best answer. If they agree, confirm. If they differ, note the disagreement and provide the most accurate answer. Be concise."""

async def collect_stream(name: str, model: str, prompt: str) -> tuple[str, str]:
    chunks = []
    async with client.messages.stream(
        model=model,
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        async for text in stream.text_stream:
            chunks.append(text)
            print(f"[{name}] {text}", end="", flush=True)
    print()
    return name, "".join(chunks)

async def streaming_consensus(question: str) -> str:
    print("=== Collecting responses (streaming in parallel) ===\n")

    # Collect all responses concurrently
    responses = await asyncio.gather(*[
        collect_stream(name, model, question)
        for name, model in MODELS
    ])

    print("\n=== Synthesizing consensus ===\n")

    answers_text = "\n\n".join(
        f"Model {name}:\n{text}" for name, text in responses
    )

    synthesis = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{
            "role": "user",
            "content": SYNTHESIS_PROMPT.format(
                n=len(responses),
                question=question,
                answers=answers_text,
            ),
        }],
    )
    return synthesis.content[0].text

if __name__ == "__main__":
    async def main():
        question = "Should I use async/await or threading for I/O-bound tasks in Python? Explain briefly."
        consensus = await streaming_consensus(question)
        print(f"\n=== Consensus ===\n{consensus}")
    asyncio.run(main())

# Expected Token Savings: Parallel collection; synthesis at Haiku cost; wall time = max(models), not sum
# Environment: ANTHROPIC_API_KEY
```

---

### Option 4: Progressive Streaming Aggregator with Live Updates

Push partial results from all models into a shared aggregator that updates a live display as chunks arrive.

```python
import asyncio
import time
from dataclasses import dataclass, field
import anthropic

client = anthropic.AsyncAnthropic()

@dataclass
class StreamAggregator:
    model_names: list[str]
    _buffers: dict[str, list[str]] = field(default_factory=dict)
    _done: dict[str, bool] = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _update_callbacks: list = field(default_factory=list)

    def __post_init__(self):
        for name in self.model_names:
            self._buffers[name] = []
            self._done[name] = False

    async def add_chunk(self, model_name: str, chunk: str):
        async with self._lock:
            self._buffers[model_name].append(chunk)
        for cb in self._update_callbacks:
            await cb(model_name, chunk)

    async def mark_done(self, model_name: str):
        async with self._lock:
            self._done[model_name] = True

    @property
    def all_done(self) -> bool:
        return all(self._done.values())

    def get_text(self, model_name: str) -> str:
        return "".join(self._buffers[model_name])

    def summary(self) -> dict[str, int]:
        return {name: len("".join(buf).split()) for name, buf in self._buffers.items()}

def print_progress(aggregator: StreamAggregator):
    summary = aggregator.summary()
    parts = " | ".join(f"{name}:{words}w" for name, words in summary.items())
    print(f"\r[progress] {parts}   ", end="", flush=True)

async def stream_to_aggregator(
    model_name: str,
    model_id: str,
    prompt: str,
    aggregator: StreamAggregator,
) -> None:
    try:
        async with client.messages.stream(
            model=model_id,
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            async for text in stream.text_stream:
                await aggregator.add_chunk(model_name, text)
    finally:
        await aggregator.mark_done(model_name)

async def live_aggregate(prompt: str) -> StreamAggregator:
    models = [
        ("haiku", "claude-haiku-4-5-20251001"),
        ("sonnet", "claude-sonnet-4-6"),
    ]

    agg = StreamAggregator(model_names=[n for n, _ in models])

    # Progress display callback
    async def on_chunk(model_name: str, chunk: str):
        print_progress(agg)

    agg._update_callbacks.append(on_chunk)

    start = time.monotonic()
    await asyncio.gather(*[
        stream_to_aggregator(name, model_id, prompt, agg)
        for name, model_id in models
    ])
    elapsed = time.monotonic() - start

    print(f"\n[done in {elapsed:.1f}s] {agg.summary()}")
    return agg

if __name__ == "__main__":
    async def main():
        prompt = "Explain the difference between strong and eventual consistency in distributed systems."
        agg = await live_aggregate(prompt)
        print("\n=== Haiku response ===")
        print(agg.get_text("haiku")[:300])
        print("\n=== Sonnet response ===")
        print(agg.get_text("sonnet")[:300])
    asyncio.run(main())

# Expected Token Savings: Aggregator is zero-cost; progress display shows real-time competition between models
# Environment: ANTHROPIC_API_KEY
```

---

### Option 5: Streaming Weighted Ensemble — Interleave Tokens by Confidence

Assign weights to models; emit tokens from the highest-confidence model first, fall back to others.

```python
import asyncio
from dataclasses import dataclass, field
import anthropic

client = anthropic.AsyncAnthropic()

@dataclass
class ModelStream:
    name: str
    model_id: str
    weight: float
    chunks: list[str] = field(default_factory=list)
    done: bool = False
    tokens: int = 0

async def buffered_stream(ms: ModelStream, prompt: str, ready: asyncio.Event) -> None:
    async with client.messages.stream(
        model=ms.model_id,
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        async for text in stream.text_stream:
            ms.chunks.append(text)
            ms.tokens += len(text.split())
            if not ready.is_set() and ms.tokens >= 20:
                ready.set()  # signal that this stream has enough to start displaying
    ms.done = True
    ready.set()

async def weighted_ensemble_stream(prompt: str, output_callback=None) -> dict[str, str]:
    streams = [
        ModelStream("haiku", "claude-haiku-4-5-20251001", weight=0.4),
        ModelStream("sonnet", "claude-sonnet-4-6", weight=0.6),
    ]

    ready_events = [asyncio.Event() for _ in streams]
    tasks = [
        asyncio.create_task(buffered_stream(ms, prompt, ev))
        for ms, ev in zip(streams, ready_events)
    ]

    # Wait for the highest-weight model to have some content
    primary = max(streams, key=lambda s: s.weight)
    primary_event = ready_events[streams.index(primary)]
    await primary_event.wait()

    # Stream primary model's output first
    cursor = 0
    results = {}
    print(f"[primary={primary.name}] streaming output:\n")
    while not primary.done or cursor < len(primary.chunks):
        while cursor < len(primary.chunks):
            chunk = primary.chunks[cursor]
            if output_callback:
                output_callback(primary.name, chunk)
            else:
                print(chunk, end="", flush=True)
            cursor += 1
        if not primary.done:
            await asyncio.sleep(0.01)

    print(f"\n[primary stream complete: {primary.tokens} tokens]")
    results[primary.name] = "".join(primary.chunks)

    # Collect remaining streams
    await asyncio.gather(*tasks)
    for ms in streams:
        if ms.name != primary.name:
            results[ms.name] = "".join(ms.chunks)

    return results

if __name__ == "__main__":
    async def main():
        prompt = "What is the CAP theorem and why does it matter for distributed databases?"
        results = await weighted_ensemble_stream(prompt)
        print(f"\n\n=== All streams complete ===")
        for name, text in results.items():
            print(f"[{name}] {len(text.split())} words")
    asyncio.run(main())

# Expected Token Savings: Primary (high-weight) output shown immediately; secondary buffered without blocking UX
# Environment: ANTHROPIC_API_KEY
```

---

### Option 6: Streaming Diff — Show Changes Between Model Versions

Stream two model versions of the same query and highlight where they differ in real time.

```python
import asyncio
import difflib
import anthropic

client = anthropic.AsyncAnthropic()

async def collect_full_stream(model: str, prompt: str) -> str:
    chunks = []
    async with client.messages.stream(
        model=model,
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        async for text in stream.text_stream:
            chunks.append(text)
    return "".join(chunks)

def word_diff(text_a: str, text_b: str, label_a: str = "A", label_b: str = "B") -> str:
    words_a = text_a.split()
    words_b = text_b.split()
    diff = list(difflib.ndiff(words_a, words_b))

    output = []
    for token in diff:
        if token.startswith("  "):
            output.append(token[2:])
        elif token.startswith("- "):
            output.append(f"[-{token[2:]}]")
        elif token.startswith("+ "):
            output.append(f"[+{token[2:]}]")
    return " ".join(output)

async def streaming_diff(prompt: str) -> dict:
    print("Streaming both models in parallel...\n")

    model_a = ("haiku", "claude-haiku-4-5-20251001")
    model_b = ("sonnet", "claude-sonnet-4-6")

    text_a, text_b = await asyncio.gather(
        collect_full_stream(model_a[1], prompt),
        collect_full_stream(model_b[1], prompt),
    )

    diff = word_diff(text_a, text_b, model_a[0], model_b[0])

    # Compute similarity
    words_a = set(text_a.lower().split())
    words_b = set(text_b.lower().split())
    similarity = len(words_a & words_b) / max(len(words_a | words_b), 1)

    return {
        model_a[0]: text_a,
        model_b[0]: text_b,
        "diff": diff,
        "similarity": similarity,
        "agreement": similarity > 0.6,
    }

if __name__ == "__main__":
    async def main():
        prompt = "Is it better to use PostgreSQL or MongoDB for a user profile service? Give a one-paragraph answer."

        result = await streaming_diff(prompt)
        print(f"=== Haiku ===\n{result['haiku'][:300]}")
        print(f"\n=== Sonnet ===\n{result['sonnet'][:300]}")
        print(f"\n=== Diff (similarity={result['similarity']:.0%}, {'AGREE' if result['agreement'] else 'DIVERGE'}) ===")
        print(result["diff"][:500])
    asyncio.run(main())

# Expected Token Savings: Parallel streaming halves latency vs sequential; diff is computed locally (zero tokens)
# Environment: ANTHROPIC_API_KEY
```

---

## Comparison

| Option | Strategy | Latency | Output | Best For |
|--------|----------|---------|--------|----------|
| 1 | Fan-out, interleaved chunks | max(models) | All streams labeled | Debugging multi-model behavior |
| 2 | Race, cancel losers | min(models) | Fastest response only | Latency-critical single answer |
| 3 | Collect all, synthesize | max(models) + synthesis | Consensus | Factual correctness via voting |
| 4 | Live aggregator with progress | max(models) | All complete responses | UX with real-time visibility |
| 5 | Weighted primary + buffer | max(models) | Primary first | UX-first with fallback quality |
| 6 | Parallel collect + diff | max(models) | Diff view | Model comparison, regression testing |
