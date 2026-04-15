---
layout: solution
title: "Agent Processes Requests Sequentially When Parallel Is Possible"
category: concurrency
description: "Agent awaits each subtask one at a time even when the subtasks are independent, making total latency the sum of all subtask latencies instead of the maximum."
tags: [concurrency, asyncio, performance, latency, parallelism, gather]
---

## Symptom

A research agent fetches summaries for 5 independent documents and takes 15 seconds — 3 seconds per document. Parallelising the 5 calls would take 3 seconds total. A multi-tool agent calls `get_weather`, `get_news`, and `get_stocks` one after another for a morning briefing, taking 9 seconds instead of 3. The bottleneck is not the API rate limit or model capacity; it is sequential `await` calls for tasks that have no dependency on each other.

## Root Cause

Developers new to `asyncio` write `result = await some_coroutine()` inside a loop, which awaits each coroutine to completion before starting the next. This is correct when each call depends on the previous result. When the calls are independent, it wastes wall time equal to `(N-1) × mean_latency`. The fix is `asyncio.gather()` or `asyncio.TaskGroup`, which runs all coroutines concurrently and waits for all of them.

## Fix

### Option 1 — Replace sequential `await` loop with `asyncio.gather`

```python
import asyncio
import time
import anthropic

client = anthropic.AsyncAnthropic()

async def summarise(text: str, label: str) -> str:
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{"role": "user", "content": f"Summarise in one sentence:\n\n{text}"}],
    )
    return f"[{label}] {response.content[0].text.strip()}"

DOCUMENTS = [
    ("doc-1", "The Eiffel Tower was built in 1889 for the World's Fair. It stands 330 metres tall."),
    ("doc-2", "Python was created by Guido van Rossum and first released in 1991."),
    ("doc-3", "Photosynthesis converts sunlight, water, and CO₂ into glucose and oxygen."),
    ("doc-4", "The Great Wall of China stretches over 21,000 kilometres."),
    ("doc-5", "DNA is a double-helix molecule that encodes genetic information."),
]

async def sequential() -> list[str]:
    results = []
    for label, text in DOCUMENTS:
        result = await summarise(text, label)   # ← waits for each before starting next
        results.append(result)
    return results

async def parallel() -> list[str]:
    return await asyncio.gather(
        *[summarise(text, label) for label, text in DOCUMENTS]
    )

async def main() -> None:
    t0 = time.perf_counter()
    seq_results = await sequential()
    seq_time = time.perf_counter() - t0
    print(f"Sequential: {seq_time:.2f}s")

    t0 = time.perf_counter()
    par_results = await parallel()
    par_time = time.perf_counter() - t0
    print(f"Parallel:   {par_time:.2f}s")
    print(f"Speedup:    {seq_time / par_time:.1f}x")

    for r in par_results:
        print(f"  {r[:100]}")

asyncio.run(main())
```

**Expected Token Savings:** Same token count; wall time reduces from N × latency to max(latencies) — typically 3-5× faster for 5 independent calls.
**Environment:** Any agent that summarises, classifies, or extracts from multiple independent documents, URLs, or data sources.

---

### Option 2 — Parallel tool execution via `asyncio.gather`

```python
import asyncio
import json
import anthropic

client = anthropic.AsyncAnthropic()

# Simulated async tools
async def get_weather(city: str) -> dict:
    await asyncio.sleep(0.3)   # simulate network latency
    return {"city": city, "temp_c": 22, "condition": "sunny"}

async def get_news(topic: str) -> dict:
    await asyncio.sleep(0.4)
    return {"topic": topic, "headline": f"Latest news on {topic}: significant developments expected."}

async def get_calendar(date: str) -> dict:
    await asyncio.sleep(0.2)
    return {"date": date, "events": ["10am standup", "2pm design review"]}

TOOL_REGISTRY = {
    "get_weather":  get_weather,
    "get_news":     get_news,
    "get_calendar": get_calendar,
}

TOOLS = [
    {
        "name": "get_weather",
        "description": "Get current weather for a city.",
        "input_schema": {"type": "object", "required": ["city"],  "properties": {"city":  {"type": "string"}}},
    },
    {
        "name": "get_news",
        "description": "Get latest news on a topic.",
        "input_schema": {"type": "object", "required": ["topic"], "properties": {"topic": {"type": "string"}}},
    },
    {
        "name": "get_calendar",
        "description": "Get today's calendar events.",
        "input_schema": {"type": "object", "required": ["date"],  "properties": {"date":  {"type": "string"}}},
    },
]

async def run_agent(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]
    import time

    for _ in range(6):
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=TOOLS,
            messages=messages,
        )
        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if b.type == "text"), "")

        messages.append({"role": "assistant", "content": response.content})

        # Execute all tool calls in PARALLEL
        tool_calls = [b for b in response.content if b.type == "tool_use"]
        if not tool_calls:
            continue

        t0 = time.perf_counter()
        async def call_tool(block):
            fn = TOOL_REGISTRY[block.name]
            result = await fn(**block.input)
            return {"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(result)}

        results = await asyncio.gather(*[call_tool(b) for b in tool_calls])
        elapsed = time.perf_counter() - t0
        print(f"  [tools] {len(tool_calls)} calls in {elapsed:.2f}s (parallel)")
        messages.append({"role": "user", "content": list(results)})

    return "max steps reached"

asyncio.run(run_agent("Give me a morning briefing: weather in Paris, AI news, and today's calendar for 2024-01-15."))
```

**Expected Token Savings:** Parallel tool execution reduces multi-tool latency from sum to max; for 3 tools at 300/400/200ms, sequential = 900ms, parallel = 400ms.
**Environment:** Multi-tool agents like morning briefings, research aggregators, and dashboard generators.

---

### Option 3 — Semaphore-bounded parallel processing

```python
import asyncio
import anthropic

client = anthropic.AsyncAnthropic()

async def classify(text: str, sem: asyncio.Semaphore) -> dict:
    """Classify text with bounded concurrency."""
    async with sem:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=32,
            temperature=0,
            messages=[{
                "role": "user",
                "content": f"Classify sentiment as POSITIVE/NEGATIVE/NEUTRAL. Reply with one word.\n\nText: {text}",
            }],
        )
        label = response.content[0].text.strip().upper()
        return {"text": text[:50], "label": label}

async def batch_classify(texts: list[str], max_concurrent: int = 5) -> list[dict]:
    """Classify all texts in parallel, limited to max_concurrent at a time."""
    sem = asyncio.Semaphore(max_concurrent)
    return await asyncio.gather(*[classify(t, sem) for t in texts])

async def main() -> None:
    import time
    texts = [
        "This product is absolutely amazing!",
        "Terrible service, never again.",
        "It arrived on time.",
        "Best purchase I've made this year.",
        "Completely broken out of the box.",
        "Average quality, nothing special.",
        "Highly recommend to everyone!",
        "Waste of money.",
        "Does exactly what it says.",
        "Disappointed with the results.",
    ]

    t0 = time.perf_counter()
    results = await batch_classify(texts, max_concurrent=5)
    elapsed = time.perf_counter() - t0

    print(f"Classified {len(texts)} texts in {elapsed:.2f}s (max 5 concurrent)")
    for r in results:
        print(f"  {r['label']:10} | {r['text']}")

asyncio.run(main())
```

**Expected Token Savings:** Semaphore-bounded parallelism processes N items in ceil(N/max_concurrent) waves instead of N sequential calls; cost is identical, wall time reduces by max_concurrent×.
**Environment:** Batch classification, sentiment analysis, or extraction pipelines where rate limits require bounded concurrency.

---

### Option 4 — `asyncio.TaskGroup` for structured concurrency (Python 3.11+)

```python
import asyncio
import anthropic

client = anthropic.AsyncAnthropic()

async def translate(text: str, target_lang: str) -> str:
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": f"Translate to {target_lang}: {text}"}],
    )
    return response.content[0].text.strip()

async def translate_to_many(text: str, languages: list[str]) -> dict[str, str]:
    """Translate the same text to multiple languages in parallel."""
    results: dict[str, str] = {}

    async with asyncio.TaskGroup() as tg:
        tasks = {
            lang: tg.create_task(translate(text, lang))
            for lang in languages
        }
    # All tasks complete when the TaskGroup exits
    return {lang: task.result() for lang, task in tasks.items()}

async def multi_step_research(topic: str) -> dict:
    """
    Stage 1: two independent lookups in parallel.
    Stage 2: synthesis (depends on stage 1 results).
    """
    async with asyncio.TaskGroup() as tg:
        summary_task     = tg.create_task(translate(f"Summarise {topic} in English", "English"))
        key_points_task  = tg.create_task(translate(f"List 3 key facts about {topic}", "English"))

    summary    = summary_task.result()
    key_points = key_points_task.result()

    # Stage 2: synthesis depends on stage 1
    synth_response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{
            "role": "user",
            "content": f"Combine these into a short brief:\n\nSummary: {summary}\n\nKey points: {key_points}",
        }],
    )
    return {
        "summary":    summary,
        "key_points": key_points,
        "brief":      synth_response.content[0].text,
    }

async def main() -> None:
    import time

    # Parallel translation
    t0 = time.perf_counter()
    translations = await translate_to_many(
        "The sky is blue.",
        ["French", "Spanish", "German", "Japanese"],
    )
    print(f"4 translations in {time.perf_counter() - t0:.2f}s:")
    for lang, text in translations.items():
        print(f"  {lang}: {text}")

    # Staged parallel research
    print()
    t0 = time.perf_counter()
    result = await multi_step_research("quantum computing")
    print(f"Research in {time.perf_counter() - t0:.2f}s:")
    print(f"Brief: {result['brief'][:200]}")

asyncio.run(main())
```

**Expected Token Savings:** TaskGroup provides structured concurrency with automatic cancellation on any task failure; same parallelism benefit as `gather` with cleaner error propagation.
**Environment:** Python 3.11+ projects; TaskGroup is the preferred modern pattern over `asyncio.gather` for structured concurrency.

---

### Option 5 — Fan-out/fan-in pattern for map-reduce workflows

```python
import asyncio
import anthropic

client = anthropic.AsyncAnthropic()

async def chunk_and_summarise(chunk: str, index: int) -> str:
    """Fan-out: summarise one chunk."""
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{"role": "user", "content": f"Summarise this section concisely:\n\n{chunk}"}],
    )
    return response.content[0].text.strip()

async def synthesise(summaries: list[str]) -> str:
    """Fan-in: merge all summaries into a final answer."""
    combined = "\n\n".join(f"Section {i+1}: {s}" for i, s in enumerate(summaries))
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{
            "role": "user",
            "content": f"Merge these section summaries into one coherent summary:\n\n{combined}",
        }],
    )
    return response.content[0].text.strip()

async def summarise_long_document(document: str, chunk_size: int = 500) -> str:
    """Map-reduce summarisation: chunk → parallel summarise → synthesise."""
    # Split into chunks
    words  = document.split()
    chunks = [
        " ".join(words[i : i + chunk_size])
        for i in range(0, len(words), chunk_size)
    ]
    print(f"  [fan-out] {len(chunks)} chunks → parallel summarise")

    # Fan-out: summarise all chunks in parallel
    summaries = await asyncio.gather(
        *[chunk_and_summarise(chunk, i) for i, chunk in enumerate(chunks)]
    )
    print(f"  [fan-in]  {len(summaries)} summaries → synthesise")

    # Fan-in: synthesise all summaries
    return await synthesise(list(summaries))

async def main() -> None:
    import time
    # Simulate a long document
    document = " ".join([
        "Artificial intelligence is transforming every industry.",
        "Machine learning models can now perform tasks once thought impossible.",
        "Natural language processing enables computers to understand human text.",
        "Computer vision allows machines to interpret visual information.",
        "Reinforcement learning trains agents through reward and punishment.",
        "These technologies are being applied in healthcare, finance, and education.",
        "Ethical considerations are increasingly important as AI systems grow more powerful.",
        "Researchers are working on alignment, interpretability, and safety.",
    ] * 10)

    t0 = time.perf_counter()
    result = await summarise_long_document(document)
    elapsed = time.perf_counter() - t0
    print(f"  Summarised {len(document.split())} words in {elapsed:.2f}s")
    print(f"  Result: {result[:200]}")

asyncio.run(main())
```

**Expected Token Savings:** Fan-out parallelism reduces map phase from O(N) sequential latency to O(1); total latency is max(chunk_latency) + synthesise_latency instead of sum(chunk_latency) + synthesise_latency.
**Environment:** Long document summarisation, multi-source research, and any map-reduce LLM workflow.

---

### Option 6 — Queue-based worker pool for high-volume parallel processing

```python
import asyncio
import time
import anthropic

client = anthropic.AsyncAnthropic()

async def worker(worker_id: int, queue: asyncio.Queue, results: list) -> None:
    """Worker coroutine that pulls tasks from the queue and processes them."""
    while True:
        try:
            item_id, text = queue.get_nowait()
        except asyncio.QueueEmpty:
            break

        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=64,
            temperature=0,
            messages=[{"role": "user", "content": f"Classify as spam or not-spam. One word only.\n\n{text}"}],
        )
        label = response.content[0].text.strip().upper()
        results.append({"id": item_id, "label": label, "worker": worker_id})
        queue.task_done()

async def parallel_classify(texts: list[str], num_workers: int = 4) -> list[dict]:
    """Classify texts using a fixed pool of worker coroutines."""
    queue: asyncio.Queue = asyncio.Queue()
    for i, text in enumerate(texts):
        await queue.put((i, text))

    results: list[dict] = []
    workers = [
        asyncio.create_task(worker(i, queue, results))
        for i in range(num_workers)
    ]
    await asyncio.gather(*workers)
    results.sort(key=lambda r: r["id"])
    return results

async def main() -> None:
    texts = [
        "Congratulations! You've won a $1000 gift card. Click now!",
        "Can we reschedule tomorrow's meeting to 3pm?",
        "URGENT: Your account will be suspended. Verify now.",
        "Please review the attached quarterly report.",
        "Free iPhone giveaway! Limited time offer!",
        "Hi, just checking in on the project status.",
        "You have been selected for a special prize!",
        "The deployment is complete and all tests pass.",
    ]

    t0 = time.perf_counter()
    results = await parallel_classify(texts, num_workers=4)
    elapsed = time.perf_counter() - t0

    print(f"Classified {len(texts)} items in {elapsed:.2f}s with 4 workers")
    for r in results:
        print(f"  [{r['id']}] {r['label']:10} (worker {r['worker']}) | {texts[r['id']][:50]}")

asyncio.run(main())
```

**Expected Token Savings:** Worker pool processes N items with W workers in ceil(N/W) waves; throughput scales linearly with workers up to rate limit; same token cost as sequential, fraction of the wall time.
**Environment:** High-volume classification, moderation, or extraction pipelines where the item count is large and bounded concurrency is required.

---

## Comparison

| Option | Pattern | Best For | Python Requirement |
|---|---|---|---|
| 1. `asyncio.gather` | Simple parallel map | Independent subtasks | 3.7+ |
| 2. Parallel tool execution | Multi-tool agents | Morning briefings, dashboards | 3.7+ |
| 3. Semaphore-bounded | Rate-limited batch work | Large batches with concurrency limits | 3.7+ |
| 4. `asyncio.TaskGroup` | Structured concurrency | Staged pipelines, error propagation | 3.11+ |
| 5. Fan-out/fan-in | Map-reduce workflows | Long document summarisation | 3.7+ |
| 6. Worker pool queue | High-volume streaming | Continuous item streams | 3.7+ |
