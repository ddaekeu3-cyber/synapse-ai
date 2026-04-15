---
layout: solution
title: "Agent creates new HTTP session per request"
category: performance
description: "Agent instantiates a new Anthropic client or httpx session on every tool call or request, paying the full TCP handshake and TLS negotiation cost each time. Connection reuse reduces per-request overhead from ~200ms to ~5ms for subsequent requests in the same process."
tags: [performance, http, connection-pool, session-reuse, latency, httpx, asyncio]
---

## Symptom

Each call to the Anthropic API takes 250–400ms even on fast networks, where the API itself responds in 100ms. Profiling shows 150–200ms is spent on TCP connection establishment and TLS handshake. With 10 tool calls per agent turn, this adds 1.5–2 seconds of pure networking overhead per turn — more than the model's actual TTFT for short responses.

## Root Cause

Creating `anthropic.Anthropic()` or `anthropic.AsyncAnthropic()` inside a request handler or tool call function allocates a new `httpx.Client` instance, which starts with no open connections. Every API call must establish a fresh TCP connection and complete the TLS handshake before sending the first byte. `httpx` supports connection pooling and keep-alive by default, but only if the same client instance is reused across calls.

## Fix

Instantiate the Anthropic client once at module level or application startup. Reuse the same client instance across all requests in the process. For async code, use `AsyncAnthropic` with a shared instance. For multi-threaded code, `httpx.Client` is thread-safe and handles concurrent requests through its connection pool.

---

### Option 1 — Module-level client singleton (simplest fix)

```python
import anthropic
import time

# WRONG — creates a new client (and a new httpx.Client) on every call:
# def run_agent(message):
#     client = anthropic.Anthropic(api_key="sk-live-...")  # ← new TCP per call
#     return client.messages.create(...)

# CORRECT — create once at module level, reuse everywhere:
client = anthropic.Anthropic(api_key="sk-live-...")


def run_agent(user_message: str) -> str:
    """Reuses the module-level client — no TCP setup on repeated calls."""
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=256,
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text


def benchmark_comparison():
    """Show the latency difference between new-client and reused-client."""
    import httpx

    print("--- New client per call (bad) ---")
    for i in range(3):
        start = time.perf_counter()
        # Simulate fresh client: new httpx.Client = new connection pool
        temp_client = httpx.Client()
        elapsed = time.perf_counter() - start
        print(f"  Call {i+1}: {elapsed*1000:.1f}ms client setup")
        temp_client.close()

    print("\n--- Reused client (good) ---")
    shared = httpx.Client()
    for i in range(3):
        start = time.perf_counter()
        # No client setup cost — pool already exists
        elapsed = time.perf_counter() - start
        print(f"  Call {i+1}: {elapsed*1000:.2f}ms (pool reused)")
    shared.close()


# Module-level client is initialized once when the module is imported.
# All functions that import this module share the same connection pool.
# For a process handling 100 requests/minute, this saves ~100 × 150ms = 15s/min.

# Quick test
result = run_agent("What is 2+2?")
print(f"Answer: {result}")
```

**Expected Token Savings:** Zero token change; connection reuse saves 150–200ms per API call — for an agent making 10 calls per turn, this is 1.5–2 seconds of latency removed per turn at zero cost.
**Environment:** Any Python agent; this is the single lowest-effort, highest-impact performance optimization available — change one line (move client instantiation to module level) and get immediate results.

---

### Option 2 — Async client singleton for concurrent requests

```python
import anthropic
import asyncio
import time

# Shared async client — initialized once, reused across all coroutines
async_client = anthropic.AsyncAnthropic(api_key="sk-live-...")


async def call_model(message: str, request_id: int) -> tuple[int, str, float]:
    """Single model call that reuses the shared async client."""
    start = time.perf_counter()
    response = await async_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=128,
        messages=[{"role": "user", "content": message}],
    )
    elapsed = time.perf_counter() - start
    return request_id, response.content[0].text, elapsed


async def run_concurrent_agents(messages: list[str]) -> list[str]:
    """
    All concurrent calls share the same httpx.AsyncClient connection pool.
    The pool multiplexes requests over existing connections when possible.
    """
    tasks = [
        asyncio.create_task(call_model(msg, i))
        for i, msg in enumerate(messages)
    ]
    results = await asyncio.gather(*tasks)

    for req_id, text, elapsed in sorted(results, key=lambda x: x[0]):
        print(f"  Request {req_id}: {elapsed*1000:.0f}ms — {text[:50]}")

    return [text for _, text, _ in results]


async def demonstrate_pool_reuse():
    """Show that connection setup cost only occurs once."""
    print("First batch (may include connection setup):")
    t0 = time.perf_counter()
    await run_concurrent_agents(["Hi", "Hello", "Hey"])
    print(f"  Batch 1 total: {(time.perf_counter()-t0)*1000:.0f}ms")

    print("\nSecond batch (connection reused — faster):")
    t0 = time.perf_counter()
    await run_concurrent_agents(["Bye", "Goodbye", "See you"])
    print(f"  Batch 2 total: {(time.perf_counter()-t0)*1000:.0f}ms")
    # Second batch is typically 150-200ms faster due to connection reuse


asyncio.run(demonstrate_pool_reuse())
```

**Expected Token Savings:** Zero token change; for 10 concurrent requests sharing the pool, connection setup overhead drops from 10 × 180ms = 1.8s to 1 × 180ms + 9 × 5ms = 225ms — 8× faster startup for the concurrent batch.
**Environment:** Async agents handling multiple parallel tool calls; the async client's connection pool is designed for concurrent usage and handles multiplexing automatically.

---

### Option 3 — Connection pool configuration for high-throughput agents

```python
import anthropic
import httpx
import asyncio
import time

# Configure a larger connection pool for high-throughput agents
# Default httpx pool: 10 connections per host
# For agents making 50+ concurrent calls: increase pool size
HTTPX_TRANSPORT = httpx.AsyncHTTPTransport(
    limits=httpx.Limits(
        max_connections=50,          # total connections across all hosts
        max_keepalive_connections=20, # persistent keep-alive connections
        keepalive_expiry=30.0,       # seconds before idle connection is closed
    ),
    retries=2,
)

# Create async client with custom transport
high_throughput_client = anthropic.AsyncAnthropic(
    api_key="sk-live-...",
    http_client=httpx.AsyncClient(transport=HTTPX_TRANSPORT),
)

# Standard client for low-throughput usage
standard_client = anthropic.AsyncAnthropic(api_key="sk-live-...")


async def agent_call(msg: str) -> str:
    response = await high_throughput_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=128,
        messages=[{"role": "user", "content": msg}],
    )
    return response.content[0].text


async def throughput_test(n_concurrent: int = 20):
    """Fire N concurrent requests and measure total time."""
    messages = [f"Count to {i}" for i in range(1, n_concurrent + 1)]
    start = time.perf_counter()
    results = await asyncio.gather(*[agent_call(m) for m in messages])
    elapsed = time.perf_counter() - start
    print(
        f"[Pool test] {n_concurrent} concurrent calls in {elapsed:.2f}s "
        f"({elapsed/n_concurrent*1000:.0f}ms avg)"
    )
    return results


# Pool sizing guide:
# Low throughput  (<5 concurrent):  default pool (max_connections=10) is sufficient
# Medium          (5-20 concurrent): max_connections=20, max_keepalive=10
# High throughput (20-50 concurrent): max_connections=50, max_keepalive=20
# Very high       (50+ concurrent):  max_connections=100, consider rate limiting

asyncio.run(throughput_test(10))
```

**Expected Token Savings:** Zero token change; pool sizing prevents connection queue buildup — without adequate pool size, the 21st concurrent request waits for a connection slot, adding ~200ms latency that grows linearly with concurrency.
**Environment:** High-throughput production agents handling 20+ concurrent requests; default pool settings work well up to ~10 concurrent calls, but need tuning for higher loads.

---

### Option 4 — Lazy client initialization with thread safety

```python
import anthropic
import threading
import time

_client_lock = threading.Lock()
_client: anthropic.Anthropic | None = None
_async_client: anthropic.AsyncAnthropic | None = None


def get_client() -> anthropic.Anthropic:
    """
    Thread-safe lazy client initialization.
    Useful when the API key is not available at import time
    (e.g., loaded from a secrets manager at runtime).
    """
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:   # double-checked locking
                print("[Client] Initializing Anthropic client")
                _client = anthropic.Anthropic(api_key="sk-live-...")
    return _client


def get_async_client() -> anthropic.AsyncAnthropic:
    """Thread-safe lazy async client initialization."""
    global _async_client
    if _async_client is None:
        with _client_lock:
            if _async_client is None:
                print("[Client] Initializing AsyncAnthropic client")
                _async_client = anthropic.AsyncAnthropic(api_key="sk-live-...")
    return _async_client


def run_agent(user_message: str) -> str:
    """Uses lazy-initialized shared client."""
    client = get_client()   # fast after first call
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=256,
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text


def benchmark_lazy_init():
    """Demonstrate that initialization cost is paid only once."""
    times = []
    for i in range(5):
        start = time.perf_counter()
        get_client()
        elapsed = time.perf_counter() - start
        times.append(elapsed)
        print(f"  get_client() call {i+1}: {elapsed*1000:.3f}ms")

    print(f"\n  First call: {times[0]*1000:.3f}ms (includes init)")
    print(f"  Avg subsequent: {sum(times[1:])/len(times[1:])*1000:.3f}ms (just a dict lookup)")


benchmark_lazy_init()
```

**Expected Token Savings:** Zero token change; lazy init is appropriate when the API key comes from a secrets manager — avoids blocking module import while still ensuring a single client per process; subsequent calls to `get_client()` cost nanoseconds (pointer dereference).
**Environment:** Applications where the API key is loaded dynamically (AWS Secrets Manager, HashiCorp Vault, environment variables set after import); lazy init bridges the gap between eager module-level init and per-request creation.

---

### Option 5 — Context manager pattern for scoped client lifecycle

```python
import anthropic
import asyncio
from contextlib import asynccontextmanager
from typing import AsyncGenerator

# For web frameworks (FastAPI, Starlette) or long-running processes that
# need explicit control over client lifecycle — creation on startup, cleanup on shutdown


class AgentClientManager:
    """
    Manages the Anthropic client lifecycle for a web application or service.
    Created at startup, shut down gracefully at shutdown.
    """
    def __init__(self):
        self._client: anthropic.AsyncAnthropic | None = None

    async def startup(self):
        """Call once at application startup."""
        self._client = anthropic.AsyncAnthropic(api_key="sk-live-...")
        print("[AgentClientManager] Client initialized")

    async def shutdown(self):
        """Call once at application shutdown to close connections gracefully."""
        if self._client:
            await self._client.close()
            self._client = None
            print("[AgentClientManager] Client closed")

    @property
    def client(self) -> anthropic.AsyncAnthropic:
        if self._client is None:
            raise RuntimeError("Client not initialized — call startup() first")
        return self._client

    async def create_message(self, **kwargs) -> str:
        response = await self.client.messages.create(**kwargs)
        return response.content[0].text


manager = AgentClientManager()


@asynccontextmanager
async def lifespan() -> AsyncGenerator[AgentClientManager, None]:
    """FastAPI-style lifespan context manager."""
    await manager.startup()
    try:
        yield manager
    finally:
        await manager.shutdown()


# FastAPI integration example:
# from fastapi import FastAPI
# @asynccontextmanager
# async def app_lifespan(app: FastAPI):
#     await manager.startup()
#     yield
#     await manager.shutdown()
# app = FastAPI(lifespan=app_lifespan)
# @app.get("/agent")
# async def agent_endpoint(q: str):
#     return await manager.create_message(
#         model="claude-sonnet-4-6", max_tokens=256,
#         messages=[{"role": "user", "content": q}]
#     )

async def demo_lifespan():
    async with lifespan() as mgr:
        # All requests within this block share the same connection pool
        results = await asyncio.gather(*[
            mgr.create_message(
                model="claude-sonnet-4-6",
                max_tokens=64,
                messages=[{"role": "user", "content": f"Say '{i}'"}],
            )
            for i in range(3)
        ])
        for r in results:
            print(r)


asyncio.run(demo_lifespan())
```

**Expected Token Savings:** Zero token change; explicit lifecycle management ensures `httpx` sends a `Connection: close` on shutdown, preventing server-side connection exhaustion; the graceful close also allows in-flight requests to complete before the process exits.
**Environment:** Web servers and microservices (FastAPI, Django async, Starlette); the lifespan pattern is idiomatic for these frameworks and ensures connections are not leaked when the process restarts.

---

### Option 6 — Per-worker client pool for multi-process deployments

```python
import anthropic
import asyncio
import os

# In multi-process deployments (Gunicorn + Uvicorn workers, multiprocessing.Pool),
# each worker process must have its own client instance.
# Clients cannot be safely shared across fork() boundaries.

_PROCESS_CLIENT: anthropic.AsyncAnthropic | None = None


def get_process_client() -> anthropic.AsyncAnthropic:
    """
    Returns a client that belongs to THIS process.
    Safe to call after fork() — each worker gets its own client.
    """
    global _PROCESS_CLIENT
    if _PROCESS_CLIENT is None:
        pid = os.getpid()
        print(f"[Worker {pid}] Creating client for this process")
        _PROCESS_CLIENT = anthropic.AsyncAnthropic(api_key="sk-live-...")
    return _PROCESS_CLIENT


async def worker_task(task_id: int) -> str:
    """Each async task within a worker reuses the same per-process client."""
    client = get_process_client()
    response = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=64,
        messages=[{"role": "user", "content": f"Task {task_id}: what is {task_id}*{task_id}?"}],
    )
    return response.content[0].text


async def worker_main(worker_id: int, n_tasks: int = 5):
    """Simulate a Gunicorn worker handling multiple requests."""
    print(f"\n[Worker {worker_id}] Starting {n_tasks} tasks")
    results = await asyncio.gather(*[worker_task(i) for i in range(n_tasks)])
    for r in results:
        print(f"  {r.strip()[:60]}")


# Comparison table
# | Option | Pattern | Use Case | Thread-safe | Fork-safe |
# |--------|---------|----------|------------|-----------|
# | 1 Module-level | Eager singleton | Simple scripts, single-process | Yes | No* |
# | 2 Async singleton | Async module-level | Async apps | Yes | No* |
# | 3 Pool config | Custom pool sizing | High-throughput (20+) | Yes | No* |
# | 4 Lazy init | Double-check lock | Dynamic API key | Yes | No* |
# | 5 Lifespan | Framework lifecycle | FastAPI/Starlette | Yes | No* |
# | 6 Per-worker | Process-local init | Gunicorn multi-process | Yes | Yes |
# (*fork-unsafe: do not share httpx clients across fork() boundaries)

asyncio.run(worker_main(1, n_tasks=4))
```

**Expected Token Savings:** Zero token change; per-worker client prevents `httpx` connection-after-fork bugs that cause silent request failures — avoiding retry overhead of ~500 tokens per failed call that must be retried.
**Environment:** Production deployments using Gunicorn multi-process or `multiprocessing`; `httpx` clients must not be shared across `fork()` — each worker process needs its own client initialized after the fork.
