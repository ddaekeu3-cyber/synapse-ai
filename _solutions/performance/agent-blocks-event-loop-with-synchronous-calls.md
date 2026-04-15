---
layout: solution
title: "Agent Blocks Event Loop with Synchronous API Calls"
category: performance
description: "Agent uses synchronous HTTP calls inside an async application, blocking the event loop and preventing other coroutines from running — causing latency spikes, timeouts, and throughput collapse under load."
tags: [performance, async, event-loop, concurrency, httpx, asyncio]
---

## Symptom

Under moderate load, response time for all users spikes simultaneously. One slow external API call freezes the entire server for its duration:

```
[t=0ms]  Request A begins — calls external API (sync)
[t=0ms]  Request B arrives — blocked, can't execute
[t=320ms] External API responds
[t=320ms] Request B begins processing (320ms of unnecessary delay)
```

CPU shows 0% utilisation during the blocked period. The event loop is alive but frozen.

## Root Cause

Using `requests.get()` or any synchronous blocking I/O call inside `async def` functions does not release the event loop. While the thread waits for the network response, no other coroutines can run. In async frameworks (FastAPI, aiohttp, Claude's async SDK), this causes cascading latency for all concurrent requests.

## Fix

---

### Option 1 — Replace `requests` with `httpx.AsyncClient`

Drop-in replacement: swap `requests.get()` for `httpx.AsyncClient.get()`. Async HTTP releases the event loop while waiting for network I/O.

```python
import asyncio
import httpx
import anthropic

async_client = anthropic.AsyncAnthropic()

async def fetch_external_data(url: str) -> dict:
    # ✓ Async HTTP — does not block the event loop
    async with httpx.AsyncClient(timeout=10.0) as http:
        response = await http.get(url)
        response.raise_for_status()
        return response.json()

async def agent_with_async_http(user_message: str) -> str:
    # Fetch external data concurrently with other coroutines
    external_data = await fetch_external_data("https://httpbin.org/json")

    messages = [{
        "role": "user",
        "content": f"{user_message}\n\nContext: {external_data}",
    }]

    response = await async_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=messages,
    )
    return response.content[0].text

async def handle_concurrent_requests():
    """
    Demonstrate that async HTTP allows true concurrency.
    Both requests run in parallel — neither blocks the other.
    """
    tasks = [
        agent_with_async_http("Summarise this data briefly."),
        agent_with_async_http("What is the key insight here?"),
        agent_with_async_http("List the main points."),
    ]

    import time
    start = time.monotonic()
    results = await asyncio.gather(*tasks)
    elapsed = time.monotonic() - start

    print(f"Completed {len(results)} requests in {elapsed:.2f}s")
    for i, r in enumerate(results):
        print(f"Result {i + 1}: {r[:60]}...")

asyncio.run(handle_concurrent_requests())
```

**Expected Token Savings:** None — throughput fix; eliminates event loop blocking
**Environment:** `pip install anthropic httpx`

---

### Option 2 — Run Sync Code in a Thread Pool

When you cannot avoid synchronous libraries (legacy SDKs, database drivers), offload them to a thread pool executor so the event loop is free while the thread blocks.

```python
import asyncio
import time
import requests  # Synchronous — but we'll offload it safely
import anthropic

async_client = anthropic.AsyncAnthropic()
_thread_pool = None

def get_thread_pool():
    global _thread_pool
    if _thread_pool is None:
        from concurrent.futures import ThreadPoolExecutor
        _thread_pool = ThreadPoolExecutor(max_workers=10)
    return _thread_pool

def _sync_fetch(url: str) -> dict:
    """Blocking function — runs in thread pool, not in event loop."""
    response = requests.get(url, timeout=10)
    response.raise_for_status()
    return response.json()

async def fetch_in_thread(url: str) -> dict:
    """
    Offload blocking I/O to a thread.
    The event loop is free while the thread waits for the network.
    """
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(get_thread_pool(), _sync_fetch, url)
    return result

def _sync_db_query(query: str) -> list[dict]:
    """Simulate a blocking database call."""
    time.sleep(0.1)  # Simulates DB latency
    return [{"id": 1, "query": query, "result": "some data"}]

async def db_query_in_thread(query: str) -> list[dict]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(get_thread_pool(), _sync_db_query, query)

async def agent_with_threaded_io(question: str) -> str:
    # Both run concurrently without blocking the event loop
    api_data, db_data = await asyncio.gather(
        fetch_in_thread("https://httpbin.org/uuid"),
        db_query_in_thread("SELECT * FROM users LIMIT 5"),
    )

    response = await async_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{
            "role": "user",
            "content": (
                f"{question}\n"
                f"API data: {api_data}\n"
                f"DB data: {db_data}"
            ),
        }],
    )
    return response.content[0].text

async def main():
    start = time.monotonic()
    results = await asyncio.gather(*[
        agent_with_threaded_io(f"Analyse this dataset #{i}")
        for i in range(5)
    ])
    elapsed = time.monotonic() - start
    print(f"5 requests completed in {elapsed:.2f}s (true concurrent)")

asyncio.run(main())
```

**Expected Token Savings:** None — parallelism fix; I/O overlap reduces wall-clock time
**Environment:** `pip install anthropic requests`

---

### Option 3 — AsyncIO Semaphore to Limit Concurrent External Calls

Async calls should be concurrent but not unlimited — too many simultaneous connections exhaust connection pools. Use a semaphore to limit parallelism while keeping calls non-blocking.

```python
import asyncio
import httpx
import anthropic

async_client = anthropic.AsyncAnthropic()

# Limit to 5 concurrent external HTTP calls
_http_semaphore = asyncio.Semaphore(5)
# Limit to 3 concurrent Anthropic API calls
_api_semaphore = asyncio.Semaphore(3)

async def bounded_http_get(url: str) -> dict:
    async with _http_semaphore:
        async with httpx.AsyncClient(timeout=10.0) as http:
            response = await http.get(url)
            response.raise_for_status()
            return response.json()

async def bounded_llm_call(messages: list[dict]) -> str:
    async with _api_semaphore:
        response = await async_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=messages,
        )
        return response.content[0].text

async def process_single_item(item_id: int) -> str:
    # Async HTTP — event loop free while waiting
    data = await bounded_http_get(f"https://httpbin.org/anything?id={item_id}")

    # Async LLM call — event loop free while waiting
    return await bounded_llm_call([{
        "role": "user",
        "content": f"Summarise: {str(data)[:200]}",
    }])

async def process_batch(item_ids: list[int]) -> list[str]:
    """
    Process all items concurrently, bounded by semaphores.
    The event loop is never blocked.
    """
    tasks = [process_single_item(item_id) for item_id in item_ids]
    return await asyncio.gather(*tasks)

import time

async def main():
    item_ids = list(range(12))
    start = time.monotonic()
    results = await process_batch(item_ids)
    elapsed = time.monotonic() - start

    print(f"Processed {len(results)} items in {elapsed:.2f}s")
    print(f"Avg per item: {elapsed / len(results) * 1000:.0f}ms")

asyncio.run(main())
```

**Expected Token Savings:** None — throughput optimisation; bounded concurrency prevents connection exhaustion
**Environment:** `pip install anthropic httpx`

---

### Option 4 — FastAPI Integration with Async Agent Endpoint

Show how to correctly integrate async Claude calls into a FastAPI server — never blocking the event loop, allowing the server to handle concurrent requests efficiently.

```python
import asyncio
import httpx
import anthropic
from contextlib import asynccontextmanager

# In a real FastAPI app:
# from fastapi import FastAPI
# from pydantic import BaseModel

# Simulated FastAPI-style implementation for demonstration
class AppState:
    anthropic_client: anthropic.AsyncAnthropic = None
    http_client: httpx.AsyncClient = None

app_state = AppState()

async def startup():
    """Called once at application startup — initialise shared clients."""
    app_state.anthropic_client = anthropic.AsyncAnthropic()
    app_state.http_client = httpx.AsyncClient(
        timeout=10.0,
        limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
    )
    print("Async clients initialised")

async def shutdown():
    """Clean up connections gracefully."""
    await app_state.http_client.aclose()
    print("Connections closed")

async def chat_endpoint(user_message: str, context_url: str | None = None) -> dict:
    """
    Equivalent of a FastAPI POST /chat endpoint.
    All I/O is async — event loop is never blocked.
    """
    context = ""
    if context_url:
        try:
            response = await app_state.http_client.get(context_url)
            context = f"\nContext: {response.text[:500]}"
        except httpx.RequestError:
            context = "\n(External context unavailable)"

    messages = [{"role": "user", "content": user_message + context}]

    response = await app_state.anthropic_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=messages,
    )

    return {"reply": response.content[0].text, "model": response.model}

async def simulate_concurrent_requests():
    """Simulate 10 concurrent users — none blocks the others."""
    await startup()

    import time
    start = time.monotonic()

    results = await asyncio.gather(*[
        chat_endpoint(
            user_message=f"Request {i}: What is {i} squared?",
            context_url="https://httpbin.org/uuid" if i % 2 == 0 else None,
        )
        for i in range(10)
    ])

    elapsed = time.monotonic() - start
    print(f"10 concurrent requests completed in {elapsed:.2f}s")
    for i, r in enumerate(results):
        print(f"[{i}] {r['reply'][:50]}...")

    await shutdown()

asyncio.run(simulate_concurrent_requests())
```

**Expected Token Savings:** None — server architecture fix; enables high-concurrency deployments
**Environment:** `pip install anthropic httpx`

---

### Option 5 — Async Streaming to Reduce Time-to-First-Token

Use streaming to return the first tokens to the user immediately, without waiting for the full response. The event loop stays free between stream chunks.

```python
import asyncio
import httpx
import anthropic

async_client = anthropic.AsyncAnthropic()

async def stream_response(user_message: str) -> str:
    """
    Stream response tokens as they arrive.
    User sees output immediately — no blocking wait for full completion.
    """
    collected = []

    async with async_client.messages.stream(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        messages=[{"role": "user", "content": user_message}],
    ) as stream:
        async for text in stream.text_stream:
            collected.append(text)
            print(text, end="", flush=True)  # Stream to output

    print()  # Newline after stream
    return "".join(collected)

async def parallel_streams(questions: list[str]) -> list[str]:
    """
    Multiple streaming responses in parallel.
    Each stream is independent and non-blocking.
    """
    results = await asyncio.gather(*[
        stream_response(q) for q in questions
    ])
    return results

async def streaming_with_prefetch(user_message: str, data_url: str) -> str:
    """
    Prefetch external data while streaming — true async parallelism.
    """
    async with httpx.AsyncClient(timeout=10.0) as http:
        # Start both concurrently
        data_task = asyncio.create_task(http.get(data_url))

        stream_task = asyncio.create_task(
            stream_response(user_message)
        )

        # Wait for data (usually fast) and stream result
        data_response = await data_task
        context = data_response.text[:200]
        print(f"\n[Prefetched context: {context[:50]}...]")

        return await stream_task

import time

async def main():
    print("=== Sequential streaming ===")
    start = time.monotonic()
    await stream_response("List 3 interesting facts about Python.")
    print(f"Elapsed: {time.monotonic() - start:.2f}s\n")

    print("=== Parallel streaming ===")
    start = time.monotonic()
    await parallel_streams([
        "What is asyncio?",
        "What is httpx?",
    ])
    print(f"Both streams elapsed: {time.monotonic() - start:.2f}s")

asyncio.run(main())
```

**Expected Token Savings:** None — UX improvement; time-to-first-token reduced by 60–80%
**Environment:** `pip install anthropic httpx`

---

### Option 6 — Event Loop Health Monitor

Add a loop lag monitor that detects when the event loop is being blocked. Alerts when latency exceeds a threshold — pinpoints which code paths are causing the block.

```python
import asyncio
import time
import anthropic
import requests  # Intentionally sync for demonstration

async_client = anthropic.AsyncAnthropic()

class EventLoopMonitor:
    """
    Measures event loop lag by scheduling heartbeat ticks.
    If a tick is delayed beyond threshold, something is blocking the loop.
    """
    def __init__(self, tick_interval: float = 0.05, lag_threshold: float = 0.1):
        self._interval = tick_interval
        self._threshold = lag_threshold
        self._max_lag: float = 0.0
        self._violation_count: int = 0
        self._running = False
        self._task: asyncio.Task | None = None

    async def _monitor(self):
        while self._running:
            before = time.monotonic()
            await asyncio.sleep(self._interval)
            actual_elapsed = time.monotonic() - before
            lag = actual_elapsed - self._interval

            if lag > self._threshold:
                self._violation_count += 1
                self._max_lag = max(self._max_lag, lag)
                print(
                    f"[LOOP LAG] {lag * 1000:.0f}ms lag detected! "
                    f"(threshold: {self._threshold * 1000:.0f}ms)"
                )

    def start(self):
        self._running = True
        self._task = asyncio.create_task(self._monitor())

    async def stop(self) -> dict:
        self._running = False
        if self._task:
            self._task.cancel()
        return {
            "max_lag_ms": self._max_lag * 1000,
            "violations": self._violation_count,
        }

async def bad_agent_call(message: str) -> str:
    """Blocking sync HTTP inside async — WRONG."""
    response = requests.get("https://httpbin.org/delay/0.2", timeout=5)  # Blocks!
    data = response.json()

    llm_response = await async_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{"role": "user", "content": message}],
    )
    return llm_response.content[0].text

async def good_agent_call(message: str) -> str:
    """Non-blocking async HTTP — CORRECT."""
    import httpx
    async with httpx.AsyncClient(timeout=5.0) as http:
        response = await http.get("https://httpbin.org/delay/0.2")
        data = response.json()

    llm_response = await async_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{"role": "user", "content": message}],
    )
    return llm_response.content[0].text

async def main():
    monitor = EventLoopMonitor(tick_interval=0.05, lag_threshold=0.15)
    monitor.start()

    print("=== Testing BLOCKING call (bad) ===")
    await bad_agent_call("Quick question: what is 1+1?")
    stats = await monitor.stop()
    print(f"Blocking stats: {stats}")

    monitor2 = EventLoopMonitor(tick_interval=0.05, lag_threshold=0.15)
    monitor2.start()

    print("\n=== Testing ASYNC call (good) ===")
    await good_agent_call("Quick question: what is 2+2?")
    stats2 = await monitor2.stop()
    print(f"Async stats: {stats2}")

asyncio.run(main())
```

**Expected Token Savings:** None — observability tool; identifies blocking code before it causes production incidents
**Environment:** `pip install anthropic httpx requests`

---

## Comparison

| Option | Fix Type | Complexity | When to Use |
|--------|----------|------------|-------------|
| httpx.AsyncClient | Drop-in replacement | Low | Always — replace requests in async code |
| Thread Pool Executor | Offload sync code | Medium | When stuck with sync libraries |
| Asyncio Semaphore | Bounded concurrency | Low | When too many concurrent calls crash the server |
| FastAPI Integration | Architecture pattern | Medium | Building async web services |
| Async Streaming | UX + throughput | Low | User-facing chat applications |
| Event Loop Monitor | Diagnostics | Medium | Debugging mystery latency spikes |

**Recommended starting point:** Option 1 (httpx.AsyncClient) — replace all `requests` calls in async code immediately. Add Option 3 (Semaphore) to cap concurrency at safe levels.
