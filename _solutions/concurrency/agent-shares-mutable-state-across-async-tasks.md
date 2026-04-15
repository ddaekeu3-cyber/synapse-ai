---
layout: solution
title: "Agent Shares Mutable State Across Async Tasks"
category: concurrency
description: "Multiple concurrent agent tasks read and write the same dict, list, or counter without locks, causing silent data corruption."
tags: [concurrency, asyncio, race-condition, thread-safety, data-integrity]
---

## Symptom

Your async agent spawns multiple tasks — tool calls, sub-agents, or parallel lookups — and they all update a shared results dict, history list, or running total. Occasionally counts are wrong, list entries go missing, or two tasks overwrite each other's results. The bug doesn't reproduce reliably and is absent in unit tests that run tasks sequentially.

## Root Cause

Python's asyncio is cooperative: a task yields control only at `await` points. But compound operations like `d[k] = d.get(k, 0) + 1` or `results.append(item); total += 1` are not atomic — another task can run between the read and the write. On CPython the GIL prevents true parallelism for pure Python, but coroutines interleave at every `await`, which is enough to corrupt shared mutable state.

## Fix

### Option 1 — asyncio.Lock for a shared counter

```python
import asyncio
import anthropic

client = anthropic.AsyncAnthropic()

class SharedCounter:
    def __init__(self):
        self.value = 0
        self._lock = asyncio.Lock()

    async def increment(self, amount: int = 1) -> int:
        async with self._lock:
            self.value += amount
            return self.value

token_counter = SharedCounter()

async def ask(prompt: str) -> str:
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}],
    )
    tokens_used = response.usage.input_tokens + response.usage.output_tokens
    total = await token_counter.increment(tokens_used)
    print(f"[tokens] this call={tokens_used}, running total={total}")
    return response.content[0].text

async def main():
    prompts = [f"Question {i}: explain concept {i}" for i in range(8)]
    results = await asyncio.gather(*[ask(p) for p in prompts])
    print(f"Final token count: {token_counter.value}")
    print(f"Got {len(results)} answers")

asyncio.run(main())
```

**Expected Token Savings:** No savings, but prevents silent count corruption that would make cost tracking unreliable.
**Environment:** Any async agent accumulating per-call metrics (token counts, error totals, latency sums).

---

### Option 2 — asyncio.Lock guarding a shared dict

```python
import asyncio
import anthropic

client = anthropic.AsyncAnthropic()

class SafeResultStore:
    def __init__(self):
        self._data: dict[str, str] = {}
        self._lock = asyncio.Lock()

    async def set(self, key: str, value: str) -> None:
        async with self._lock:
            self._data[key] = value

    async def get(self, key: str) -> str | None:
        async with self._lock:
            return self._data.get(key)

    async def snapshot(self) -> dict[str, str]:
        async with self._lock:
            return dict(self._data)  # return a copy, not the live dict


store = SafeResultStore()

async def research_topic(topic: str) -> None:
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": f"Briefly explain: {topic}"}],
    )
    answer = response.content[0].text
    await store.set(topic, answer)
    print(f"[done] {topic!r}")

async def main():
    topics = ["gravity", "photosynthesis", "inflation", "entropy", "recursion"]
    await asyncio.gather(*[research_topic(t) for t in topics])
    results = await store.snapshot()
    for topic, answer in results.items():
        print(f"\n{topic}:\n{answer[:120]}...")

asyncio.run(main())
```

**Expected Token Savings:** None directly; prevents data loss where two coroutines write to the same key simultaneously.
**Environment:** Parallel research agents that collect results into a shared dictionary before synthesis.

---

### Option 3 — asyncio.Queue for producer/consumer pipeline

```python
import asyncio
import anthropic

client = anthropic.AsyncAnthropic()

async def producer(queue: asyncio.Queue, prompts: list[str]) -> None:
    """Enqueue prompts one by one; no shared mutable state needed."""
    for prompt in prompts:
        await queue.put(prompt)
    # Signal workers to stop
    for _ in range(NUM_WORKERS):
        await queue.put(None)

NUM_WORKERS = 3

async def worker(worker_id: int, queue: asyncio.Queue, results: asyncio.Queue) -> None:
    while True:
        prompt = await queue.get()
        if prompt is None:
            break
        try:
            response = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=256,
                messages=[{"role": "user", "content": prompt}],
            )
            await results.put((prompt, response.content[0].text))
        except Exception as e:
            await results.put((prompt, f"ERROR: {e}"))
        finally:
            queue.task_done()

async def consumer(results: asyncio.Queue, expected: int) -> list[tuple[str, str]]:
    collected = []
    for _ in range(expected):
        item = await results.get()
        collected.append(item)
    return collected

async def main():
    prompts = [f"Describe {topic}" for topic in
               ["the moon", "TCP/IP", "jazz music", "black holes", "democracy",
                "neural networks", "plate tectonics", "supply chains"]]

    work_q    = asyncio.Queue()
    result_q  = asyncio.Queue()

    workers = [asyncio.create_task(worker(i, work_q, result_q)) for i in range(NUM_WORKERS)]
    asyncio.create_task(producer(work_q, prompts))

    pairs = await consumer(result_q, len(prompts))
    await asyncio.gather(*workers)

    for prompt, answer in pairs:
        print(f"Q: {prompt!r}\nA: {answer[:100]}...\n")

asyncio.run(main())
```

**Expected Token Savings:** None directly; Queue eliminates shared mutable state entirely, making the pipeline naturally race-free.
**Environment:** High-throughput batch agents; Queue is the canonical async pattern for producer/consumer workloads.

---

### Option 4 — asyncio.Lock on a growing history list

```python
import asyncio
import anthropic

client = anthropic.AsyncAnthropic()

class SafeHistory:
    """Thread-safe (asyncio-safe) conversation history shared across sub-agents."""

    def __init__(self):
        self._messages: list[dict] = []
        self._lock = asyncio.Lock()

    async def append(self, role: str, content: str) -> None:
        async with self._lock:
            self._messages.append({"role": role, "content": content})

    async def snapshot(self) -> list[dict]:
        async with self._lock:
            return list(self._messages)  # shallow copy is safe for immutable strings

    async def length(self) -> int:
        async with self._lock:
            return len(self._messages)


history = SafeHistory()

async def sub_agent(agent_id: int, task: str) -> None:
    await history.append("user", f"[agent-{agent_id}] {task}")
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=await history.snapshot(),
    )
    reply = response.content[0].text
    await history.append("assistant", f"[agent-{agent_id}] {reply}")
    print(f"[agent-{agent_id}] done, history length={await history.length()}")

async def main():
    tasks = [
        sub_agent(1, "What is 2+2?"),
        sub_agent(2, "Name a planet."),
        sub_agent(3, "What colour is grass?"),
    ]
    await asyncio.gather(*tasks)
    final = await history.snapshot()
    print(f"\nFinal history ({len(final)} messages):")
    for m in final:
        print(f"  {m['role']}: {m['content'][:60]}")

asyncio.run(main())
```

**Expected Token Savings:** Prevents duplicate or missing messages in history, avoiding wasted tokens from corrupted context.
**Environment:** Multi-sub-agent systems that share a single conversation history or memory buffer.

---

### Option 5 — Task-local state via contextvars (no locking needed)

```python
import asyncio
import contextvars
import anthropic

client = anthropic.AsyncAnthropic()

# Each task gets its own copy of these variables — no lock required
request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="unknown")
token_count_var: contextvars.ContextVar[int] = contextvars.ContextVar("token_count", default=0)

async def ask_with_context(prompt: str, req_id: str) -> str:
    # Set task-local values; other tasks are unaffected
    request_id_var.set(req_id)
    token_count_var.set(0)

    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}],
    )

    tokens = response.usage.input_tokens + response.usage.output_tokens
    token_count_var.set(token_count_var.get() + tokens)

    print(f"[{request_id_var.get()}] tokens={token_count_var.get()}")
    return response.content[0].text

async def main():
    tasks = [
        ask_with_context("Explain gravity.", "req-001"),
        ask_with_context("What is ATP?",     "req-002"),
        ask_with_context("Define entropy.",  "req-003"),
    ]
    results = await asyncio.gather(*tasks)
    for r in results:
        print(r[:80])

asyncio.run(main())
```

**Expected Token Savings:** None directly; ContextVar eliminates the need for locks entirely by giving each coroutine its own state.
**Environment:** Request-scoped tracing, per-request token budgets, or any state that should not be shared across tasks.

---

### Option 6 — Immutable result aggregation with asyncio.gather return values

```python
import asyncio
import anthropic
from dataclasses import dataclass

client = anthropic.AsyncAnthropic()

@dataclass(frozen=True)
class TaskResult:
    """Immutable result — safe to read from any coroutine without locks."""
    prompt: str
    answer: str
    input_tokens: int
    output_tokens: int

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


async def run_task(prompt: str) -> TaskResult:
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}],
    )
    return TaskResult(
        prompt=prompt,
        answer=response.content[0].text,
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
    )

async def main():
    prompts = [
        "What is a quasar?",
        "Explain machine learning.",
        "What is DNA?",
        "Define recursion.",
        "What is the speed of light?",
    ]

    # gather() returns results in order — no shared state needed
    results: list[TaskResult] = await asyncio.gather(*[run_task(p) for p in prompts])

    total_tokens = sum(r.total_tokens for r in results)
    for r in results:
        print(f"Q: {r.prompt!r}")
        print(f"A: {r.answer[:100]}...")
        print(f"   tokens={r.total_tokens}\n")

    print(f"Total tokens across all tasks: {total_tokens}")

asyncio.run(main())
```

**Expected Token Savings:** None directly; returning immutable values from each task and aggregating after `gather()` is the simplest race-free pattern.
**Environment:** Pure embarrassingly-parallel workloads where tasks are independent; the preferred design when possible.

---

## Comparison

| Option | Mechanism | Shared State? | Lock Overhead | Best For |
|---|---|---|---|---|
| 1. Lock + counter | asyncio.Lock | Yes (guarded) | Minimal | Running totals, metrics |
| 2. Lock + dict | asyncio.Lock | Yes (guarded) | Minimal | Keyed result collection |
| 3. Queue pipeline | asyncio.Queue | No | None | Producer/consumer pipelines |
| 4. Lock + history | asyncio.Lock | Yes (guarded) | Minimal | Shared conversation history |
| 5. ContextVar | contextvars | No (per-task) | None | Per-request state, tracing |
| 6. Immutable gather | Return values | No | None | Independent parallel tasks |
