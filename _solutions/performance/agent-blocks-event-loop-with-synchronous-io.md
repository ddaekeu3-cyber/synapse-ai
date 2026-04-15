---
layout: solution
title: "Agent Blocks the Event Loop with Synchronous I/O"
category: performance
description: "Your async agent handles 10 concurrent sessions, but all 10 freeze whenever one session calls requests.get(), open().read(), or time.sleep(). The event loop is blocked by synchronous I/O. Response latency spikes from 200ms to 8 seconds. asyncio.gather() provides no benefit because every task blocks the thread."
tags: [performance, asyncio, event-loop, blocking-io, sync-io, httpx, aiofiles, threadpool, concurrency]
---

# Agent Blocks the Event Loop with Synchronous I/O

## Symptom
- `asyncio.gather()` runs tasks sequentially instead of in parallel
- All sessions freeze when one session makes an HTTP request
- Response latency for session B is `n × latency_of_session_A` instead of `max(latency_A, latency_B)`
- `time.sleep()` in one coroutine pauses all other coroutines
- File reads with `open().read()` stall the event loop for large files
- Profiling shows the event loop thread is blocked, not waiting on I/O
- `asyncio.get_event_loop().is_running()` is True but no other coroutines make progress

## Root Cause
Python's `asyncio` event loop runs on a single thread. A `async def` function that calls any blocking operation — `requests.get()`, `open().read()`, `time.sleep()`, CPU-heavy code — holds the GIL and blocks the event loop thread. All other coroutines waiting to run are frozen until the blocking call returns. The solution is to replace blocking calls with async equivalents (`httpx.AsyncClient`, `aiofiles`, `asyncio.sleep`) or offload CPU-bound work to a thread pool with `asyncio.to_thread()`.

## Fix

### Option 1: Replace requests with httpx.AsyncClient — drop-in async HTTP

```python
import asyncio
import httpx
import anthropic
from typing import Any

# WRONG — blocks the event loop, freezes all other sessions:
def BAD_fetch_context(url: str) -> str:
    import requests
    response = requests.get(url, timeout=10)  # Blocks event loop thread
    return response.text

# RIGHT — async HTTP, event loop remains unblocked:
async def fetch_context(url: str) -> str:
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(url)
        response.raise_for_status()
        return response.text

# Share a single client across calls for connection pooling:
_shared_client: httpx.AsyncClient | None = None

async def get_shared_client() -> httpx.AsyncClient:
    global _shared_client
    if _shared_client is None or _shared_client.is_closed:
        _shared_client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0),
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20)
        )
    return _shared_client

async def fetch_and_answer(url: str, question: str) -> str:
    """Fetch context and query Claude — both are non-blocking."""
    client_http = await get_shared_client()
    context = await client_http.get(url)
    context.raise_for_status()

    client_ai = anthropic.AsyncAnthropic()
    response = await client_ai.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[{
            "role": "user",
            "content": f"Context:\n{context.text[:4000]}\n\nQuestion: {question}"
        }]
    )
    return response.content[0].text

# 10 concurrent fetches — none block each other:
async def handle_10_sessions():
    tasks = [
        fetch_and_answer(f"https://example.com/doc/{i}", f"Summarize doc {i}")
        for i in range(10)
    ]
    results = await asyncio.gather(*tasks)
    return results
```

### Option 2: asyncio.to_thread() — offload blocking calls to thread pool

```python
import asyncio
import time
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, TypeVar

logger = logging.getLogger(__name__)
T = TypeVar("T")

# Shared thread pool for blocking I/O operations
_thread_pool = ThreadPoolExecutor(max_workers=20, thread_name_prefix="blocking-io")

async def run_blocking(fn: Callable[..., T], *args, **kwargs) -> T:
    """
    Run a blocking function in a thread pool without blocking the event loop.
    Use this for any code you can't rewrite to be async.
    """
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        _thread_pool,
        lambda: fn(*args, **kwargs)
    )

# Or use Python 3.9+ asyncio.to_thread():
async def run_blocking_simple(fn: Callable[..., T], *args) -> T:
    return await asyncio.to_thread(fn, *args)

# BEFORE (blocking):
def read_large_config_sync(path: str) -> dict:
    import json
    with open(path) as f:
        return json.load(f)  # Blocks for large files

# AFTER (non-blocking):
async def read_large_config(path: str) -> dict:
    import json
    content = await run_blocking(open(path).read)
    return json.loads(content)

# For legacy libraries that don't support async (e.g., old database drivers):
def legacy_db_query(sql: str, params: tuple) -> list:
    import sqlite3
    conn = sqlite3.connect("/tmp/agent.db")
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return rows

async def async_db_query(sql: str, params: tuple) -> list:
    return await run_blocking(legacy_db_query, sql, params)

# Replace time.sleep() with asyncio.sleep():
async def retry_with_backoff(fn, max_retries: int = 3) -> Any:
    for attempt in range(max_retries):
        try:
            return await fn()
        except Exception as exc:
            if attempt == max_retries - 1:
                raise
            wait = 2 ** attempt  # Exponential backoff
            logger.info(f"Retry {attempt + 1}/{max_retries} after {wait}s: {exc}")
            await asyncio.sleep(wait)  # Non-blocking — other coroutines run during wait
```

### Option 3: aiofiles for async file I/O — unblock file reads/writes

```python
import asyncio
import aiofiles  # pip install aiofiles
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# WRONG — blocks the event loop on large files:
def BAD_load_knowledge_base(path: str) -> str:
    with open(path, "r") as f:
        return f.read()  # Blocks during disk I/O

# RIGHT — async file read:
async def load_knowledge_base(path: str) -> str:
    async with aiofiles.open(path, "r") as f:
        return await f.read()

# Async streaming read for very large files:
async def stream_large_file(path: str, chunk_size: int = 65536):
    """Read a large file in chunks without blocking the event loop."""
    async with aiofiles.open(path, "r") as f:
        while True:
            chunk = await f.read(chunk_size)
            if not chunk:
                break
            yield chunk

async def process_large_document(path: str) -> list[str]:
    """Process a large document line by line — fully non-blocking."""
    results = []
    async with aiofiles.open(path, "r") as f:
        async for line in f:
            line = line.strip()
            if line:
                results.append(line)
    return results

# Async write with fsync for durability:
async def save_agent_state(state: dict, path: str):
    tmp_path = path + ".tmp"
    async with aiofiles.open(tmp_path, "w") as f:
        await f.write(json.dumps(state, indent=2))
        await f.flush()
        import os
        os.fsync(f.fileno())  # OK — fsync is fast and worth blocking for
    # Atomic rename — much faster than overwrite:
    Path(tmp_path).rename(path)
    logger.info(f"State saved to {path}")

# Concurrent file reads — all run in parallel:
async def load_multiple_configs(paths: list[str]) -> list[str]:
    return await asyncio.gather(*[load_knowledge_base(p) for p in paths])
```

### Option 4: Blocking call detector — find sync I/O in production

```python
import asyncio
import time
import functools
import logging
import traceback
from typing import Any, Callable

logger = logging.getLogger(__name__)

class BlockingCallDetector:
    """
    Monitors the event loop for coroutines that block too long.
    Reports the stack trace of the blocking call.
    """

    def __init__(self, threshold_ms: float = 50.0):
        self.threshold_ms = threshold_ms
        self._slow_callbacks: list[dict] = []

    def install(self):
        """Install monitoring on the running event loop."""
        loop = asyncio.get_event_loop()
        loop.set_debug(True)
        loop.slow_callback_duration = self.threshold_ms / 1000.0
        logger.info(f"Blocking call detector installed (threshold={self.threshold_ms}ms)")

    @staticmethod
    def wrap_sync_function(fn: Callable) -> Callable:
        """
        Decorator that warns when a sync function is called from async context
        without being wrapped in to_thread().
        """
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            # Check if we're inside the event loop thread
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    logger.warning(
                        f"BLOCKING CALL: {fn.__qualname__}() called from async context! "
                        f"This will block the event loop. Wrap with asyncio.to_thread().\n"
                        f"{''.join(traceback.format_stack(limit=8))}"
                    )
            except RuntimeError:
                pass  # No event loop — we're in a sync context, fine
            return fn(*args, **kwargs)
        return wrapper

# Decorate known blocking functions to catch accidental sync calls:
import requests as _requests
_original_get = _requests.get

@BlockingCallDetector.wrap_sync_function
def patched_requests_get(*args, **kwargs):
    return _original_get(*args, **kwargs)

# _requests.get = patched_requests_get  # Uncomment to enable in dev

# Measure event loop lag to detect blocking:
async def event_loop_lag_monitor(threshold_ms: float = 100.0, interval: float = 1.0):
    """
    Measures actual event loop tick latency.
    High lag means blocking calls are occurring.
    """
    while True:
        start = time.perf_counter()
        await asyncio.sleep(0)  # Yield control — immediately resumable
        lag_ms = (time.perf_counter() - start) * 1000

        if lag_ms > threshold_ms:
            logger.warning(
                f"Event loop lag: {lag_ms:.1f}ms (threshold: {threshold_ms}ms). "
                f"A blocking call is likely occurring."
            )
        elif lag_ms > 10:
            logger.debug(f"Event loop tick: {lag_ms:.1f}ms")

        await asyncio.sleep(interval)

# Run lag monitor as background task:
async def start_monitoring():
    asyncio.create_task(event_loop_lag_monitor(threshold_ms=100.0))
    detector = BlockingCallDetector(threshold_ms=50.0)
    detector.install()
```

### Option 5: Async Claude client — use AsyncAnthropic for non-blocking LLM calls

```python
import asyncio
import anthropic
import logging
from typing import AsyncIterator

logger = logging.getLogger(__name__)

# WRONG — synchronous Anthropic client blocks event loop:
def BAD_call_claude(prompt: str) -> str:
    client = anthropic.Anthropic()  # Sync client
    response = client.messages.create(  # Blocks event loop thread!
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}]
    )
    return response.content[0].text

# RIGHT — use AsyncAnthropic:
async def call_claude(prompt: str, system: str = "") -> str:
    client = anthropic.AsyncAnthropic()
    kwargs = {
        "model": "claude-sonnet-4-6",
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": prompt}]
    }
    if system:
        kwargs["system"] = system
    response = await client.messages.create(**kwargs)
    return response.content[0].text

# Async streaming — non-blocking, yields tokens as they arrive:
async def stream_claude(prompt: str) -> AsyncIterator[str]:
    client = anthropic.AsyncAnthropic()
    async with client.messages.stream(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}]
    ) as stream:
        async for text in stream.text_stream:
            yield text

# Run N LLM calls concurrently — none block each other:
async def parallel_llm_calls(prompts: list[str]) -> list[str]:
    """
    All N API calls run concurrently.
    Total time ≈ max(individual_times), not sum(individual_times).
    """
    return await asyncio.gather(*[call_claude(p) for p in prompts])

# Shared client across requests (connection reuse):
_async_client: anthropic.AsyncAnthropic | None = None

def get_async_client() -> anthropic.AsyncAnthropic:
    global _async_client
    if _async_client is None:
        _async_client = anthropic.AsyncAnthropic(
            max_retries=3,
            timeout=60.0
        )
    return _async_client

async def call_claude_shared(prompt: str) -> str:
    client = get_async_client()
    response = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}]
    )
    return response.content[0].text
```

### Option 6: CPU-bound work in ProcessPoolExecutor — bypass the GIL

```python
import asyncio
import logging
from concurrent.futures import ProcessPoolExecutor
from typing import Any

logger = logging.getLogger(__name__)

# CPU-bound functions must be module-level (picklable) for ProcessPoolExecutor:
def compute_embeddings_cpu(texts: list[str]) -> list[list[float]]:
    """CPU-intensive embedding computation — runs in separate process."""
    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer("all-MiniLM-L6-v2")
    return model.encode(texts).tolist()

def tokenize_and_chunk(document: str, chunk_size: int = 512) -> list[str]:
    """CPU-intensive tokenization — runs in separate process."""
    import re
    sentences = re.split(r'(?<=[.!?])\s+', document)
    chunks = []
    current = []
    length = 0
    for sentence in sentences:
        words = sentence.split()
        if length + len(words) > chunk_size and current:
            chunks.append(" ".join(current))
            current = words
            length = len(words)
        else:
            current.extend(words)
            length += len(words)
    if current:
        chunks.append(" ".join(current))
    return chunks

# Process pool for CPU work:
_process_pool = ProcessPoolExecutor(max_workers=4)

async def embed_documents(texts: list[str]) -> list[list[float]]:
    """Non-blocking embedding — computation runs in another process."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_process_pool, compute_embeddings_cpu, texts)

async def chunk_document(document: str) -> list[str]:
    """Non-blocking chunking — CPU work in another process."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_process_pool, tokenize_and_chunk, document)

# Full pipeline — I/O and CPU-bound work, all non-blocking:
async def process_document_pipeline(url: str) -> str:
    import httpx
    import anthropic

    # Step 1: Fetch document (async I/O)
    async with httpx.AsyncClient(timeout=30.0) as http:
        response = await http.get(url)
        document = response.text

    # Step 2: Chunk document (CPU-bound — offloaded to process)
    chunks = await chunk_document(document)

    # Step 3: Embed chunks (CPU-bound — offloaded to process)
    embeddings = await embed_documents(chunks[:10])  # First 10 chunks

    # Step 4: Ask Claude (async I/O)
    ai_client = anthropic.AsyncAnthropic()
    summary_response = await ai_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=[{
            "role": "user",
            "content": f"Summarize this document in 3 bullet points:\n\n{document[:3000]}"
        }]
    )
    return summary_response.content[0].text
```

## Blocking vs Non-Blocking Equivalents

| Blocking (avoid in async) | Non-blocking replacement | Notes |
|---|---|---|
| `requests.get()` | `httpx.AsyncClient().get()` | Drop-in for most cases |
| `open().read()` | `aiofiles.open().read()` | Requires `aiofiles` package |
| `time.sleep(n)` | `await asyncio.sleep(n)` | Must `await` |
| `anthropic.Anthropic()` | `anthropic.AsyncAnthropic()` | Both from same package |
| `subprocess.run()` | `asyncio.create_subprocess_exec()` | Full async subprocess |
| CPU computation | `asyncio.to_thread()` or `ProcessPoolExecutor` | to_thread for I/O-bound; process for CPU |
| `sqlite3.connect()` | `aiosqlite` | pip install aiosqlite |
| Legacy sync library | `loop.run_in_executor(thread_pool, fn)` | Universal fallback |

## Performance Impact
Sync HTTP call (500ms) in 10-session server: total throughput = 5 req/s (10 sessions × 500ms, serialized)
Async HTTP call (500ms) in 10-session server: total throughput = 20 req/s (10 concurrent, ~500ms total)
Speedup: 4× with zero additional infrastructure

## Environment
- Any agent using `async def` handlers with legacy sync libraries; the bug is invisible until you measure throughput — single-user testing shows no problem because there's nothing else to block; the bug first appears under load (5+ concurrent users); profile with `asyncio.get_event_loop().slow_callback_duration = 0.05` before going to production
- Source: direct experience; replacing `requests` with `httpx.AsyncClient` is the single highest-ROI performance fix for async agents, improving throughput 3–10× with a one-line change
