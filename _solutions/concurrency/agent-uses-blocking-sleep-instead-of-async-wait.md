---
layout: solution
title: "Agent uses blocking sleep instead of async wait"
category: concurrency
description: "Agent calls time.sleep() inside an async function, freezing the entire event loop and preventing other coroutines from making progress during waits — retry delays, polling intervals, and rate-limit backoff all become blocking."
tags: [concurrency, asyncio, blocking, sleep, event-loop, performance]
---

## Symptom

The agent handles multiple concurrent requests, but they execute one at a time even though the code uses `async def`. Adding more requests does not improve throughput. CPU sits at 0 % during retry waits. A single slow operation (rate-limit backoff, polling loop) delays every other in-flight request.

## Root Cause

`time.sleep(n)` is a blocking call. When called inside a coroutine, it surrenders no control to the event loop — it simply freezes the OS thread for `n` seconds. Every other coroutine waiting to run is also frozen. The fix is `await asyncio.sleep(n)`, which suspends only the calling coroutine and lets the event loop schedule other work.

The same problem applies to other blocking operations used inside `async def`: `requests.get()`, synchronous file I/O, `subprocess.run()`, and CPU-bound loops.

## Fix

Replace every `time.sleep()` inside a coroutine with `await asyncio.sleep()`. Move blocking I/O to a thread pool via `asyncio.to_thread()`. Move CPU-bound work to a `ProcessPoolExecutor`.

---

### Option 1 — Direct replacement: `time.sleep` → `await asyncio.sleep`

```python
import anthropic
import asyncio
import time

async_client = anthropic.AsyncAnthropic(api_key="sk-live-...")


# BEFORE: blocks the event loop for every retry wait
async def create_with_retry_broken(messages: list[dict]) -> anthropic.types.Message:
    for attempt in range(5):
        try:
            return await async_client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=256,
                messages=messages,
            )
        except anthropic.RateLimitError:
            time.sleep(2 ** attempt)   # ← BLOCKS the event loop
    raise RuntimeError("Max retries exceeded")


# AFTER: yields control to the event loop during each wait
async def create_with_retry(messages: list[dict]) -> anthropic.types.Message:
    for attempt in range(5):
        try:
            return await async_client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=256,
                messages=messages,
            )
        except anthropic.RateLimitError:
            wait = 2 ** attempt
            print(f"Rate limited — waiting {wait}s (attempt {attempt + 1})")
            await asyncio.sleep(wait)  # ← yields; other coroutines run
    raise RuntimeError("Max retries exceeded")


async def main() -> None:
    messages = [{"role": "user", "content": "Say hello."}]
    t0 = time.perf_counter()

    # Both calls run concurrently; neither blocks the other during backoff
    results = await asyncio.gather(
        create_with_retry(messages),
        create_with_retry(messages),
    )
    print(f"Both calls finished in {time.perf_counter() - t0:.2f}s")
    for r in results:
        print(r.content[0].text)


asyncio.run(main())
```

**Expected Token Savings:** None — pure concurrency fix; parallel execution means the same work completes in 1× latency instead of N× when N requests overlap.
**Environment:** Any `asyncio`-based agent; the minimal required fix.

---

### Option 2 — Polling loop with `asyncio.sleep` instead of `time.sleep`

```python
import anthropic
import asyncio

async_client = anthropic.AsyncAnthropic(api_key="sk-live-...")


async def fake_long_job(job_id: str) -> str:
    """Simulates a job that takes a few seconds to complete."""
    await asyncio.sleep(3)
    return f"result_for_{job_id}"


# Job status cache (in real usage, query an external API or DB)
_job_results: dict[str, str | None] = {}


async def start_job(job_id: str) -> None:
    _job_results[job_id] = None
    result = await fake_long_job(job_id)
    _job_results[job_id] = result


async def poll_until_done(
    job_id: str,
    interval: float = 0.5,
    timeout: float = 30.0,
) -> str:
    """Poll job status without blocking the event loop."""
    elapsed = 0.0
    while elapsed < timeout:
        status = _job_results.get(job_id)
        if status is not None:
            return status
        await asyncio.sleep(interval)   # ← non-blocking poll
        elapsed += interval
    raise TimeoutError(f"Job {job_id!r} did not finish within {timeout}s")


async def run_agent_with_job(user_message: str) -> str:
    job_id = "job_abc123"
    # Start the job and poll concurrently with the LLM call
    job_task = asyncio.create_task(start_job(job_id))
    poll_task = asyncio.create_task(poll_until_done(job_id))

    llm_task = asyncio.create_task(
        async_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            messages=[{"role": "user", "content": user_message}],
        )
    )

    job_result, llm_response = await asyncio.gather(poll_task, llm_task)
    _ = job_task  # already completed

    return f"{llm_response.content[0].text}\n\nJob result: {job_result}"


asyncio.run(run_agent_with_job("Analyze this dataset."))
```

**Expected Token Savings:** None — but the polling and LLM call overlap, cutting wall-clock latency by the job duration.
**Environment:** Agents that wait for async jobs (batch processing, webhooks, DB migrations) while also calling the LLM.

---

### Option 3 — Move blocking I/O to `asyncio.to_thread`

```python
import anthropic
import asyncio
import time

async_client = anthropic.AsyncAnthropic(api_key="sk-live-...")


def blocking_file_read(path: str) -> str:
    """Synchronous file read — safe to call from a thread pool."""
    time.sleep(0.1)  # simulate slow network-mounted filesystem
    return f"Contents of {path}: [large document text here]"


def blocking_db_query(query: str) -> list[dict]:
    """Synchronous DB call — safe to call from a thread pool."""
    time.sleep(0.2)  # simulate query latency
    return [{"id": 1, "value": "row1"}, {"id": 2, "value": "row2"}]


async def run_agent(user_message: str, doc_path: str) -> str:
    # Run blocking operations concurrently in thread pool
    doc_content, db_rows = await asyncio.gather(
        asyncio.to_thread(blocking_file_read, doc_path),
        asyncio.to_thread(blocking_db_query, "SELECT * FROM items LIMIT 10"),
    )

    context = f"Document:\n{doc_content}\n\nDatabase rows:\n{db_rows}"
    full_message = f"{user_message}\n\nContext:\n{context}"

    response = await async_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[{"role": "user", "content": full_message}],
    )
    return response.content[0].text


asyncio.run(run_agent("Summarize the document and list key DB entries.", "/data/report.txt"))
```

**Expected Token Savings:** None — but file read and DB query run in parallel, removing ~0.3s of sequential blocking.
**Environment:** Agents that gather context from blocking sources (files, synchronous DB drivers, legacy SDKs) before calling the LLM.

---

### Option 4 — Exponential backoff with jitter using `asyncio.sleep`

```python
import anthropic
import asyncio
import random

async_client = anthropic.AsyncAnthropic(api_key="sk-live-...")


async def create_with_backoff(
    messages: list[dict],
    max_attempts: int = 6,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
) -> anthropic.types.Message:
    """Exponential backoff with full jitter — never blocks the event loop."""
    for attempt in range(max_attempts):
        try:
            return await async_client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=1024,
                messages=messages,
            )
        except anthropic.RateLimitError as exc:
            if attempt == max_attempts - 1:
                raise
            # Full jitter: random value in [0, min(max_delay, base * 2^attempt)]
            cap = min(max_delay, base_delay * (2 ** attempt))
            wait = random.uniform(0, cap)
            print(f"Rate limited (attempt {attempt + 1}/{max_attempts}) — sleeping {wait:.1f}s")
            await asyncio.sleep(wait)   # ← non-blocking
        except anthropic.APIStatusError as exc:
            if exc.status_code in {500, 502, 503, 529}:
                if attempt == max_attempts - 1:
                    raise
                wait = min(max_delay, base_delay * (2 ** attempt))
                await asyncio.sleep(wait)
            else:
                raise


async def run_batch(messages_list: list[list[dict]]) -> list[str]:
    """Run multiple requests concurrently with independent backoff per request."""
    responses = await asyncio.gather(
        *[create_with_backoff(m) for m in messages_list],
        return_exceptions=True,
    )
    results = []
    for r in responses:
        if isinstance(r, Exception):
            results.append(f"ERROR: {r}")
        else:
            results.append(r.content[0].text)
    return results


asyncio.run(run_batch([
    [{"role": "user", "content": "What is 2+2?"}],
    [{"role": "user", "content": "What is the capital of France?"}],
    [{"role": "user", "content": "Explain asyncio in one sentence."}],
]))
```

**Expected Token Savings:** None — but batch throughput scales with concurrency when some requests hit rate limits and others do not.
**Environment:** High-throughput agents sending many parallel requests; individual backoff without blocking ensures healthy requests are not penalized.

---

### Option 5 — Semaphore-gated concurrency with `asyncio.sleep` rate pacing

```python
import anthropic
import asyncio
import time

async_client = anthropic.AsyncAnthropic(api_key="sk-live-...")

# Limit to 5 concurrent LLM calls; pace to ≤10 requests/second
MAX_CONCURRENT = 5
MIN_INTERVAL = 0.1   # 100ms between request starts → 10 req/s max

_semaphore = asyncio.Semaphore(MAX_CONCURRENT)
_last_request_time: float = 0.0
_pace_lock = asyncio.Lock()


async def paced_create(messages: list[dict]) -> anthropic.types.Message:
    global _last_request_time

    async with _semaphore:
        async with _pace_lock:
            now = asyncio.get_event_loop().time()
            gap = now - _last_request_time
            if gap < MIN_INTERVAL:
                await asyncio.sleep(MIN_INTERVAL - gap)  # ← non-blocking pace
            _last_request_time = asyncio.get_event_loop().time()

        return await async_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=messages,
        )


async def process_queue(items: list[str]) -> list[str]:
    tasks = [
        paced_create([{"role": "user", "content": item}])
        for item in items
    ]
    responses = await asyncio.gather(*tasks)
    return [r.content[0].text for r in responses]


async def main() -> None:
    items = [f"Classify sentiment: '{text}'" for text in [
        "I love this product",
        "This is terrible",
        "It's okay I guess",
        "Absolutely fantastic",
        "Worst experience ever",
        "Pretty good overall",
        "Not what I expected",
        "Highly recommend",
    ]]
    t0 = time.perf_counter()
    results = await process_queue(items)
    print(f"Processed {len(results)} items in {time.perf_counter() - t0:.2f}s")


asyncio.run(main())
```

**Expected Token Savings:** None — but the pace limiter prevents 429 errors without blocking other in-flight requests.
**Environment:** Batch classification or enrichment pipelines where you want to stay under a requests-per-second limit without serializing work.

---

### Option 6 — Detect accidental `time.sleep` in async context via linting

```python
import anthropic
import asyncio
import ast
import inspect
from pathlib import Path


def find_blocking_sleeps(source: str, filename: str = "<string>") -> list[str]:
    """
    Static analysis: find time.sleep() calls inside async functions.
    Returns a list of warning messages.
    """
    tree = ast.parse(source, filename=filename)
    warnings: list[str] = []

    for node in ast.walk(tree):
        if not isinstance(node, (ast.AsyncFunctionDef,)):
            continue
        func_name = node.name
        for child in ast.walk(node):
            if not isinstance(child, ast.Call):
                continue
            # Match time.sleep(...)
            func = child.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "sleep"
                and isinstance(func.value, ast.Name)
                and func.value.id == "time"
            ):
                warnings.append(
                    f"{filename}:{child.lineno}: "
                    f"async def {func_name!r} calls time.sleep() — "
                    f"use 'await asyncio.sleep()' instead"
                )
    return warnings


# Example: scan the current file
sample_code = '''
import time
import asyncio

async def bad_retry():
    time.sleep(2)   # blocks event loop!
    return "done"

async def good_retry():
    await asyncio.sleep(2)  # correct
    return "done"
'''

issues = find_blocking_sleeps(sample_code, "agent.py")
for issue in issues:
    print(f"WARNING: {issue}")


# Runtime guard: monkey-patch time.sleep to raise in async context
import time as _time_module

_original_sleep = _time_module.sleep


def _guarded_sleep(seconds: float) -> None:
    try:
        loop = asyncio.get_running_loop()
        # We are inside a running event loop — this is a bug
        import warnings
        warnings.warn(
            f"time.sleep({seconds}) called from async context — "
            "use 'await asyncio.sleep()' to avoid blocking the event loop.",
            RuntimeWarning,
            stacklevel=2,
        )
    except RuntimeError:
        pass  # No running loop — blocking sleep is acceptable
    _original_sleep(seconds)


# Activate the guard in development (remove in production):
# _time_module.sleep = _guarded_sleep


# Comparison table
# | Option | Technique | Use Case |
# |--------|-----------|---------|
# | 1 Direct replacement | asyncio.sleep | Basic retry loops |
# | 2 Polling loop | asyncio.sleep + create_task | Job status polling |
# | 3 Thread pool | asyncio.to_thread | Blocking I/O libraries |
# | 4 Jitter backoff | asyncio.sleep + random | High-concurrency rate limits |
# | 5 Semaphore + pacing | asyncio.sleep + Semaphore | Throughput-limited batches |
# | 6 Static + runtime lint | ast + monkey-patch | Catch regressions early |
```

**Expected Token Savings:** None — the AST scanner and monkey-patch add zero tokens; they prevent the performance regression from being re-introduced.
**Environment:** Teams with multiple contributors; add the static scanner to CI (`pre-commit` or a pytest fixture) to catch future violations automatically.
