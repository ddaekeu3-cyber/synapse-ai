---
layout: solution
title: "Agent Doesn't Use Connection Pooling"
category: performance
description: "Agent opens a new TCP connection, TLS handshake, and HTTP session for every API call, adding 100-500ms of overhead per request that could be amortised across many calls."
tags: [performance, connection-pooling, http, latency, httpx, async]
---

## Symptom

Every LLM call takes 400-600ms even for short completions where the model itself responds in under 100ms. Network traces show a full TCP three-way handshake and TLS negotiation on every request. Under load, the agent saturates local port resources as hundreds of short-lived connections in TIME_WAIT state accumulate. Response time for the 10th request is identical to the 1st — no warmup benefit.

## Root Cause

HTTP/1.1 connections can be reused (keep-alive) across multiple requests, and HTTP/2 multiplexes multiple requests over a single connection. But both require the client to maintain a persistent connection object. When `anthropic.Anthropic()` or `httpx.Client()` is instantiated on every call, the connection is torn down and rebuilt each time. The Anthropic SDK uses `httpx` internally — reusing the SDK client across calls automatically enables connection pooling.

## Fix

### Option 1 — Reuse a single `Anthropic` client across all calls

```python
import time
import anthropic

# WRONG — new TCP+TLS handshake on every call
def ask_bad(question: str) -> str:
    client = anthropic.Anthropic()   # ← recreated every call
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        messages=[{"role": "user", "content": question}],
    )
    return response.content[0].text

# CORRECT — client created once, connection reused
_client = anthropic.Anthropic()   # module-level singleton

def ask_good(question: str) -> str:
    response = _client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        messages=[{"role": "user", "content": question}],
    )
    return response.content[0].text

questions = [
    "What is Python?",
    "What is asyncio?",
    "What is a decorator?",
    "What is a context manager?",
    "What is a generator?",
]

print("Without pooling (new client each call):")
t0 = time.perf_counter()
for q in questions:
    ask_bad(q)
bad_time = time.perf_counter() - t0
print(f"  Total: {bad_time:.2f}s ({bad_time/len(questions)*1000:.0f}ms/call avg)")

print("With pooling (shared client):")
t0 = time.perf_counter()
for q in questions:
    ask_good(q)
good_time = time.perf_counter() - t0
print(f"  Total: {good_time:.2f}s ({good_time/len(questions)*1000:.0f}ms/call avg)")
print(f"  Speedup: {bad_time/good_time:.1f}x")
```

**Expected Token Savings:** No token reduction; connection reuse eliminates 100-400ms of TCP+TLS overhead per call — up to 40% latency reduction for short completions.
**Environment:** All agents; creating a single module-level client is the simplest and most impactful performance fix available.

---

### Option 2 — Async client with connection pooling across concurrent calls

```python
import asyncio
import time
import anthropic

# Single async client shared across all concurrent coroutines
_async_client = anthropic.AsyncAnthropic()

async def ask(question: str, sem: asyncio.Semaphore) -> str:
    async with sem:
        response = await _async_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=64,
            messages=[{"role": "user", "content": question}],
        )
        return response.content[0].text

async def main() -> None:
    questions = [f"Name a {animal}." for animal in ["mammal", "bird", "reptile", "fish", "insect"]]
    sem = asyncio.Semaphore(5)

    # New client per call (simulated by measuring cold-start overhead)
    print("Shared async client with pooled connections:")
    t0 = time.perf_counter()
    results = await asyncio.gather(*[ask(q, sem) for q in questions])
    elapsed = time.perf_counter() - t0
    print(f"  {len(questions)} concurrent calls in {elapsed:.2f}s")
    for q, r in zip(questions, results):
        print(f"  {q}: {r.strip()[:40]}")

asyncio.run(main())
```

**Expected Token Savings:** Shared async client allows the underlying `httpx.AsyncClient` connection pool to serve all concurrent coroutines; HTTP/2 multiplexing sends multiple requests over one connection.
**Environment:** All async agents; the async client's connection pool is shared across all coroutines running in the same event loop.

---

### Option 3 — Configure `httpx` connection pool limits explicitly

```python
import httpx
import anthropic

# Configure pool size to match expected concurrency
HTTP_CLIENT = httpx.Client(
    limits=httpx.Limits(
        max_connections=20,          # total simultaneous connections
        max_keepalive_connections=10, # idle connections to keep warm
        keepalive_expiry=30.0,       # seconds before idle connection closes
    ),
    timeout=httpx.Timeout(30.0),
)

# Inject configured client into the Anthropic SDK
client = anthropic.Anthropic(http_client=HTTP_CLIENT)

def ask(question: str) -> str:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{"role": "user", "content": question}],
    )
    return response.content[0].text

import time

questions = [
    "What is a hash function?",
    "What is memoisation?",
    "What is tail recursion?",
]
t0 = time.perf_counter()
for q in questions:
    r = ask(q)
    print(f"Q: {q}")
    print(f"A: {r.strip()[:100]}\n")
elapsed = time.perf_counter() - t0
print(f"3 calls in {elapsed:.2f}s with explicit pool config")

# Clean up
HTTP_CLIENT.close()
```

**Expected Token Savings:** Explicit pool configuration prevents connection starvation under burst load; `max_keepalive_connections=10` keeps 10 warm connections ready, reducing cold-start latency on burst traffic.
**Environment:** High-throughput agents serving many users simultaneously; tune `max_connections` to match your concurrency level.

---

### Option 4 — Async pool with retry on connection errors

```python
import asyncio
import httpx
import anthropic

# Async HTTP client with tuned pool settings
ASYNC_HTTP_CLIENT = httpx.AsyncClient(
    limits=httpx.Limits(
        max_connections=50,
        max_keepalive_connections=20,
        keepalive_expiry=60.0,
    ),
    timeout=httpx.Timeout(connect=5.0, read=60.0, write=10.0, pool=5.0),
)

async_client = anthropic.AsyncAnthropic(http_client=ASYNC_HTTP_CLIENT)

async def ask_with_retry(question: str, max_retries: int = 3) -> str:
    for attempt in range(max_retries):
        try:
            response = await async_client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=128,
                messages=[{"role": "user", "content": question}],
            )
            return response.content[0].text
        except httpx.PoolTimeout:
            if attempt == max_retries - 1:
                raise
            wait = 0.5 * (2 ** attempt)
            print(f"  [pool] connection pool exhausted, retrying in {wait:.1f}s")
            await asyncio.sleep(wait)
        except httpx.RemoteProtocolError as e:
            # Stale keep-alive connection — retry once with a fresh one
            if attempt == 0:
                print(f"  [pool] stale connection detected, retrying")
                continue
            raise

    return "max retries exceeded"

async def main() -> None:
    questions = [
        "What is a coroutine?",
        "What is an event loop?",
        "What is a future in asyncio?",
    ]
    import time
    t0 = time.perf_counter()
    results = await asyncio.gather(*[ask_with_retry(q) for q in questions])
    elapsed = time.perf_counter() - t0
    print(f"{len(questions)} async calls with pooling in {elapsed:.2f}s")
    for q, r in zip(questions, results):
        print(f"  Q: {q} → {r.strip()[:60]}")

    await ASYNC_HTTP_CLIENT.aclose()

asyncio.run(main())
```

**Expected Token Savings:** Pool timeout retry prevents PoolTimeout errors from crashing the agent when burst traffic temporarily exhausts the pool; stale connection retry handles server-side keep-alive timeouts gracefully.
**Environment:** Production async agents; pool timeout retry is essential when `max_connections` is set below peak burst concurrency.

---

### Option 5 — Connection pool health monitoring

```python
import asyncio
import time
import httpx
import anthropic

class MonitoredPool:
    """Wraps AsyncAnthropic with connection pool metrics."""

    def __init__(self, max_connections: int = 20):
        self._http = httpx.AsyncClient(
            limits=httpx.Limits(
                max_connections=max_connections,
                max_keepalive_connections=max_connections // 2,
            ),
        )
        self._client  = anthropic.AsyncAnthropic(http_client=self._http)
        self.max_conn = max_connections
        self._calls   = 0
        self._errors  = 0
        self._total_ms: float = 0.0

    async def ask(self, question: str) -> str:
        t0 = time.perf_counter()
        try:
            response = await self._client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=64,
                messages=[{"role": "user", "content": question}],
            )
            self._calls += 1
            self._total_ms += (time.perf_counter() - t0) * 1000
            return response.content[0].text
        except Exception as e:
            self._errors += 1
            raise

    @property
    def stats(self) -> dict:
        avg_ms = self._total_ms / self._calls if self._calls else 0
        return {
            "calls":   self._calls,
            "errors":  self._errors,
            "avg_ms":  round(avg_ms, 1),
        }

    async def close(self) -> None:
        await self._http.aclose()

async def main() -> None:
    pool = MonitoredPool(max_connections=10)
    sem  = asyncio.Semaphore(5)

    async def call(q: str) -> str:
        async with sem:
            return await pool.ask(q)

    questions = [f"Give one word for category: {c}" for c in
                 ["fruit", "animal", "colour", "country", "sport"]]

    results = await asyncio.gather(*[call(q) for q in questions], return_exceptions=True)

    for q, r in zip(questions, results):
        print(f"  {q}: {str(r).strip()[:40]}")

    print(f"\nPool stats: {pool.stats}")
    await pool.close()

asyncio.run(main())
```

**Expected Token Savings:** Pool metrics reveal connection starvation (avg_ms spikes) before it causes user-visible errors; proactive pool size tuning prevents latency regressions.
**Environment:** Production agents with SLA requirements; pool monitoring feeds into dashboards and alerts.

---

### Option 6 — Application-level connection lifecycle management

```python
import asyncio
import contextlib
import httpx
import anthropic

class AgentConnectionManager:
    """
    Manages the Anthropic client lifecycle.
    Use as an async context manager to guarantee clean pool teardown.
    """
    def __init__(self, max_connections: int = 20, keepalive: float = 60.0):
        self._http_client: httpx.AsyncClient | None = None
        self._client: anthropic.AsyncAnthropic | None = None
        self._max_connections = max_connections
        self._keepalive = keepalive

    async def __aenter__(self) -> "AgentConnectionManager":
        self._http_client = httpx.AsyncClient(
            limits=httpx.Limits(
                max_connections=self._max_connections,
                max_keepalive_connections=self._max_connections // 2,
                keepalive_expiry=self._keepalive,
            ),
        )
        self._client = anthropic.AsyncAnthropic(http_client=self._http_client)
        print(f"[pool] opened (max_connections={self._max_connections})")
        return self

    async def __aexit__(self, *args) -> None:
        if self._http_client:
            await self._http_client.aclose()
            print("[pool] closed — all connections released")

    async def ask(self, question: str) -> str:
        if not self._client:
            raise RuntimeError("Client not initialised — use as context manager")
        response = await self._client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=128,
            messages=[{"role": "user", "content": question}],
        )
        return response.content[0].text

async def run_agent_session(questions: list[str]) -> list[str]:
    async with AgentConnectionManager(max_connections=10) as mgr:
        results = await asyncio.gather(*[mgr.ask(q) for q in questions])
        return list(results)

async def main() -> None:
    import time
    questions = [
        "What is dependency injection?",
        "What is a singleton pattern?",
        "What is the observer pattern?",
    ]
    t0 = time.perf_counter()
    results = await run_agent_session(questions)
    elapsed = time.perf_counter() - t0
    print(f"{len(questions)} calls in {elapsed:.2f}s")
    for q, r in zip(questions, results):
        print(f"  Q: {q[:40]}")
        print(f"  A: {r.strip()[:80]}\n")

asyncio.run(main())
```

**Expected Token Savings:** Context manager guarantees clean pool teardown; leaked connections consume server-side resources and eventually cause connection refused errors; proper teardown prevents resource exhaustion in long-running services.
**Environment:** Long-lived services (FastAPI, Flask) where the agent client must be shut down cleanly when the server stops; async context manager integrates with FastAPI lifespan events.

---

## Comparison

| Option | Setup Complexity | Pool Control | Monitoring | Best For |
|---|---|---|---|---|
| 1. Module-level singleton | None | Default | No | Single-threaded scripts — instant win |
| 2. Shared async client | Low | Default | No | Async agents — default pattern |
| 3. Explicit `httpx` limits | Low | Full | No | Tuned throughput under known concurrency |
| 4. Pool + retry | Medium | Full | No | Production async with burst traffic |
| 5. Monitored pool | Medium | Full | Yes | SLA-bound services |
| 6. Context manager lifecycle | Medium | Full | No | Server frameworks with clean shutdown |
