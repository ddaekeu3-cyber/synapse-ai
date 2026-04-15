---
layout: solution
title: "Agent Uses Synchronous IO in Async Context"
category: performance
description: "Agent blocks the event loop with synchronous file reads, HTTP calls, or database queries inside async functions, serialising all concurrent requests."
tags: [performance, async, asyncio, io, concurrency, blocking]
---

## Symptom

An `async def` agent handler finishes one request at a time despite being deployed with an async framework. CPU is idle while the agent waits for file reads or HTTP responses. Adding more concurrent users makes every user slower rather than keeping latency flat. Profiling shows the event loop blocked for hundreds of milliseconds per call on `open()`, `requests.get()`, or `time.sleep()`.

## Root Cause

Python's `asyncio` event loop is single-threaded. Any synchronous call that blocks — `open()`, `requests.get()`, `sqlite3` queries, `time.sleep()` — freezes the entire loop until the call returns. Every other coroutine waiting to run is starved for the duration of the block. An `async def` function that contains synchronous IO is async in name only; it provides zero concurrency benefit.

## Fix

### Option 1 — Replace blocking file IO with `aiofiles`

```python
import asyncio
import aiofiles
import anthropic

client = anthropic.AsyncAnthropic()

# WRONG — blocks the event loop on every read
async def ask_from_file_blocking(path: str) -> str:
    with open(path) as f:          # blocks event loop
        content = f.read()
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": content}],
    )
    return response.content[0].text

# CORRECT — yields control to the event loop during IO
async def ask_from_file_async(path: str) -> str:
    async with aiofiles.open(path) as f:    # non-blocking
        content = await f.read()
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": content}],
    )
    return response.content[0].text

async def main() -> None:
    import tempfile, os
    # Create test files
    paths = []
    for i in range(4):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write(f"Summarise this document number {i}: " + "data " * 50)
            paths.append(f.name)

    import time
    # Sequential with blocking IO
    t0 = time.perf_counter()
    for p in paths:
        await ask_from_file_blocking(p)
    blocking_time = time.perf_counter() - t0

    # Concurrent with async IO
    t0 = time.perf_counter()
    await asyncio.gather(*[ask_from_file_async(p) for p in paths])
    async_time = time.perf_counter() - t0

    print(f"Blocking IO sequential: {blocking_time:.2f}s")
    print(f"Async IO concurrent:    {async_time:.2f}s")
    print(f"Speedup: {blocking_time / async_time:.1f}x")

    for p in paths:
        os.unlink(p)

asyncio.run(main())
```

**Expected Token Savings:** No token reduction, but concurrency improvement means the same tokens complete in 1/N the wall time for N parallel requests.
**Environment:** Any async agent that reads config files, prompts, or user documents from disk.

---

### Option 2 — Offload blocking calls to `asyncio.to_thread`

```python
import asyncio
import time
import requests
import anthropic

client = anthropic.AsyncAnthropic()

def fetch_url_sync(url: str) -> str:
    """Blocking HTTP call — cannot be changed (third-party library)."""
    response = requests.get(url, timeout=10)
    return response.text[:2000]

def read_config_sync(path: str) -> dict:
    """Blocking file read with heavy processing."""
    import json
    time.sleep(0.05)   # simulate slow parse
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return {"topic": "general knowledge"}

async def fetch_url_async(url: str) -> str:
    """Run blocking HTTP call in a thread pool — event loop stays free."""
    return await asyncio.to_thread(fetch_url_sync, url)

async def read_config_async(path: str) -> dict:
    return await asyncio.to_thread(read_config_sync, path)

async def answer_from_url(url: str, config_path: str) -> str:
    # Both blocking calls run concurrently in thread pool
    content, config = await asyncio.gather(
        fetch_url_async(url),
        read_config_async(config_path),
    )
    topic = config.get("topic", "general")
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{
            "role": "user",
            "content": f"Topic: {topic}\n\nContent:\n{content}\n\nSummarise in 2 sentences.",
        }],
    )
    return response.content[0].text

async def main() -> None:
    import tempfile, json, os
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump({"topic": "technology"}, f)
        cfg = f.name

    result = await answer_from_url("https://httpbin.org/get", cfg)
    print(result[:200])
    os.unlink(cfg)

asyncio.run(main())
```

**Expected Token Savings:** `asyncio.to_thread` moves blocking calls off the event loop with zero code refactoring of the underlying sync functions.
**Environment:** Agents using third-party sync libraries (requests, psycopg2, sqlite3) that cannot be replaced with async equivalents.

---

### Option 3 — Replace `requests` with `httpx` async client

```python
import asyncio
import httpx
import anthropic

# Shared async clients — created once, reused across all calls
http_client   = httpx.AsyncClient(timeout=30.0)
claude_client = anthropic.AsyncAnthropic()

async def fetch(url: str) -> str:
    response = await http_client.get(url)
    response.raise_for_status()
    return response.text[:3000]

async def research_and_answer(urls: list[str], question: str) -> str:
    # Fetch all URLs concurrently
    pages = await asyncio.gather(*[fetch(u) for u in urls], return_exceptions=True)

    context_parts = []
    for url, page in zip(urls, pages):
        if isinstance(page, Exception):
            print(f"[warn] failed to fetch {url}: {page}")
        else:
            context_parts.append(f"Source: {url}\n{page[:500]}")

    context = "\n\n---\n\n".join(context_parts)
    response = await claude_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{
            "role": "user",
            "content": f"Context:\n{context}\n\nQuestion: {question}",
        }],
    )
    return response.content[0].text

async def main() -> None:
    urls = [
        "https://httpbin.org/get",
        "https://httpbin.org/ip",
        "https://httpbin.org/headers",
    ]
    answer = await research_and_answer(urls, "What are the common fields across all responses?")
    print(answer[:300])
    await http_client.aclose()

asyncio.run(main())
```

**Expected Token Savings:** Concurrent HTTP fetches reduce total wall time proportionally to the number of sources; context quality improves because all sources are available before the LLM call.
**Environment:** Research agents that fetch multiple web sources before generating a response.

---

### Option 4 — Async database access with `aiosqlite`

```python
import asyncio
import aiosqlite
import anthropic
import tempfile
import os

DB_PATH = tempfile.mktemp(suffix=".db")
client  = anthropic.AsyncAnthropic()

async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS knowledge (
                id      INTEGER PRIMARY KEY,
                topic   TEXT NOT NULL,
                content TEXT NOT NULL
            )
        """)
        await db.executemany(
            "INSERT OR IGNORE INTO knowledge (id, topic, content) VALUES (?, ?, ?)",
            [
                (1, "python",  "Python is a high-level, interpreted programming language."),
                (2, "asyncio", "asyncio is Python's standard library for async IO."),
                (3, "claude",  "Claude is an AI assistant made by Anthropic."),
            ],
        )
        await db.commit()

async def lookup_topic(topic: str) -> str | None:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT content FROM knowledge WHERE topic = ?", (topic,)
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None

async def answer_with_db(question: str, topic: str) -> str:
    # DB lookup is async — event loop stays unblocked
    context = await lookup_topic(topic) or "No specific context found."

    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{
            "role": "user",
            "content": f"Context: {context}\n\nQuestion: {question}",
        }],
    )
    return response.content[0].text

async def main() -> None:
    await init_db()

    # Concurrent DB lookups + LLM calls
    questions = [
        ("What is Python?",   "python"),
        ("How does asyncio work?", "asyncio"),
        ("Who made Claude?",  "claude"),
    ]
    answers = await asyncio.gather(*[
        answer_with_db(q, t) for q, t in questions
    ])
    for (q, _), a in zip(questions, answers):
        print(f"Q: {q}\nA: {a[:100]}\n")

    os.unlink(DB_PATH)

asyncio.run(main())
```

**Expected Token Savings:** Async DB queries allow N agent requests to proceed in parallel; eliminates the serialisation bottleneck of synchronous database calls.
**Environment:** Agents with a knowledge base, user history, or configuration stored in SQLite or PostgreSQL (asyncpg).

---

### Option 5 — Replace `time.sleep` with `asyncio.sleep` in retry loops

```python
import asyncio
import random
import anthropic

client = anthropic.AsyncAnthropic()

# WRONG — blocks the event loop during backoff
async def call_with_blocking_retry(prompt: str, max_retries: int = 3) -> str:
    for attempt in range(max_retries):
        try:
            response = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=128,
                messages=[{"role": "user", "content": prompt}],
            )
            return response.content[0].text
        except anthropic.RateLimitError:
            import time
            wait = 2 ** attempt + random.random()
            time.sleep(wait)   # BLOCKS the event loop — all other coroutines frozen
    raise RuntimeError("Max retries exceeded")

# CORRECT — yields the event loop during backoff
async def call_with_async_retry(prompt: str, max_retries: int = 3) -> str:
    for attempt in range(max_retries):
        try:
            response = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=128,
                messages=[{"role": "user", "content": prompt}],
            )
            return response.content[0].text
        except anthropic.RateLimitError:
            wait = 2 ** attempt + random.random()
            print(f"[retry] attempt {attempt + 1}, waiting {wait:.1f}s")
            await asyncio.sleep(wait)   # yields — other coroutines run during wait
    raise RuntimeError("Max retries exceeded")

async def main() -> None:
    prompts = [
        "Name a planet.",
        "Name a colour.",
        "Name a fruit.",
        "Name an animal.",
    ]
    # All 4 calls run concurrently; retry waits don't freeze each other
    results = await asyncio.gather(*[call_with_async_retry(p) for p in prompts])
    for prompt, result in zip(prompts, results):
        print(f"{prompt} → {result.strip()[:40]}")

asyncio.run(main())
```

**Expected Token Savings:** `asyncio.sleep` costs zero tokens and costs zero event-loop time; `time.sleep` in a retry loop serialises all concurrent coroutines during the wait.
**Environment:** All async agents with retry logic; replacing `time.sleep` with `asyncio.sleep` is the single highest-leverage fix for blocking async code.

---

### Option 6 — Event loop health monitor: detect and warn on blocking calls

```python
import asyncio
import time
import functools
import anthropic

client = anthropic.AsyncAnthropic()

# Monkey-patch to detect accidental blocking calls in async context
_BLOCK_THRESHOLD_MS = 50   # warn if event loop is blocked longer than this

class BlockingCallDetector:
    """
    Installs a slow-callback logger on the event loop.
    Any callback that takes longer than threshold is logged.
    """
    def __init__(self, threshold_ms: float = 50):
        self.threshold = threshold_ms / 1000
        self._original_slow_callback = None

    def install(self, loop: asyncio.AbstractEventLoop) -> None:
        loop.set_debug(True)
        loop.slow_callback_duration = self.threshold
        print(f"[monitor] blocking call detector armed (>{self.threshold * 1000:.0f}ms threshold)")

    def remove(self, loop: asyncio.AbstractEventLoop) -> None:
        loop.set_debug(False)

def async_timed(fn):
    """Decorator that warns if an async function takes suspiciously long."""
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        t0 = time.perf_counter()
        result = await fn(*args, **kwargs)
        elapsed = time.perf_counter() - t0
        if elapsed > 1.0:
            print(f"[perf] {fn.__name__} took {elapsed:.2f}s — check for blocking IO inside")
        return result
    return wrapper

@async_timed
async def ask(prompt: str) -> str:
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text

@async_timed
async def ask_with_blocking_io(prompt: str, path: str) -> str:
    with open(path) as f:        # ← blocking call inside async fn
        context = f.read()
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": f"{context}\n\n{prompt}"}],
    )
    return response.content[0].text

async def main() -> None:
    loop = asyncio.get_running_loop()
    detector = BlockingCallDetector(threshold_ms=_BLOCK_THRESHOLD_MS)
    detector.install(loop)

    import tempfile, os
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        f.write("Context: " + "word " * 200)
        path = f.name

    # Clean async call — no warning
    r1 = await ask("What is the capital of France?")
    print(f"Clean call: {r1.strip()[:60]}")

    # Call with blocking IO — event loop debug mode will log the slow callback
    r2 = await ask_with_blocking_io("Summarise this.", path)
    print(f"Blocking IO call: {r2.strip()[:60]}")

    detector.remove(loop)
    os.unlink(path)

asyncio.run(main())
```

**Expected Token Savings:** Monitoring adds zero tokens; it surfaces blocking calls that cause latency regressions, enabling targeted fixes before they affect production throughput.
**Environment:** Development and staging environments for async agents; enables detection of accidental sync IO introduced during refactors.

---

## Comparison

| Option | Blocking Cause | Async Replacement | Production Ready |
|---|---|---|---|
| 1. `aiofiles` | `open()` / `f.read()` | `aiofiles.open()` | Yes |
| 2. `asyncio.to_thread` | Any sync library | Thread pool wrapper | Yes — for unmigrated libs |
| 3. `httpx` async | `requests.get()` | `httpx.AsyncClient` | Yes |
| 4. `aiosqlite` | `sqlite3` queries | `aiosqlite.connect()` | Yes |
| 5. `asyncio.sleep` | `time.sleep()` in retry | `await asyncio.sleep()` | Yes — mandatory |
| 6. Event loop monitor | Unknown blocking calls | Debug mode detector | Dev/staging only |
