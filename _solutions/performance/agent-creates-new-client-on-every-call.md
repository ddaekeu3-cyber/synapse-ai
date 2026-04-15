---
layout: solution
title: "Agent Creates a New API Client on Every Call"
category: performance
description: "Agent instantiates `anthropic.Anthropic()` or `anthropic.AsyncAnthropic()` inside the function that makes each API call — creating a new HTTP connection pool, loading credentials from environment, and initialising SSL context on every request. Adds 50–200ms overhead per call and causes connection exhaustion under load."
tags: [performance, client, connection-pool, singleton, async, latency, initialization]
---

## Symptom

At low volume, agent latency seems acceptable. Under moderate load (20+ concurrent users), latency spikes from 300ms to 2+ seconds. Network monitoring shows thousands of short-lived TCP connections being opened and closed. Memory usage grows steadily. The bottleneck is not the API — it's the agent creating a new `httpx.Client` (which the Anthropic SDK uses internally) for every single request.

Per-call client creation overhead: **50–200ms** (SSL handshake + connection pool init)
Connection pool reuse saving: **40–80ms per call** at sustained throughput

## Root Cause

`anthropic.Anthropic()` creates a new `httpx.Client` each time it's called. `httpx.Client` sets up SSL context, connection pool, and credentials. If this is called inside a per-request handler or tool function, none of these are reused across calls — negating the purpose of connection pooling entirely.

## Fix

---

### Option 1 — Module-Level Singleton Client

Create the client once at module import time. All calls in the module share the same connection pool.

```python
import anthropic

# Created once at import — connection pool shared across all calls in this module
client = anthropic.Anthropic()

# WRONG: creates a new client (and new connection pool) on every call
def bad_call(prompt: str) -> str:
    new_client = anthropic.Anthropic()   # <-- Do not do this
    response = new_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text

# RIGHT: reuses the module-level client
def good_call(prompt: str) -> str:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text

import time

prompts = [f"What is {i}*{i}? One number only." for i in range(1, 6)]

print("=== Shared client (correct) ===")
start = time.monotonic()
results = [good_call(p) for p in prompts]
print(f"5 calls in {(time.monotonic()-start)*1000:.0f}ms")

print("\n=== New client per call (wrong) ===")
start = time.monotonic()
results_bad = [bad_call(p) for p in prompts]
print(f"5 calls in {(time.monotonic()-start)*1000:.0f}ms (overhead visible at scale)")
```

**Expected Token Savings:** None — same tokens; 40–80ms latency reduction per call, connection pool reuse
**Environment:** `pip install anthropic`

---

### Option 2 — Async Singleton with Application Lifecycle Management

For async applications, create `AsyncAnthropic` once at startup and close it cleanly at shutdown. Pass via dependency injection rather than globals.

```python
import asyncio
import time
import anthropic
from contextlib import asynccontextmanager
from typing import AsyncGenerator

# Application-level client — created once, closed on shutdown
_async_client: anthropic.AsyncAnthropic | None = None

@asynccontextmanager
async def lifespan() -> AsyncGenerator[anthropic.AsyncAnthropic, None]:
    """
    Async context manager for application lifecycle.
    Compatible with FastAPI lifespan, or use directly in scripts.
    """
    global _async_client
    print("[Lifespan] Creating AsyncAnthropic client...")
    _async_client = anthropic.AsyncAnthropic()
    try:
        yield _async_client
    finally:
        print("[Lifespan] Closing AsyncAnthropic client...")
        await _async_client.close()
        _async_client = None

def get_client() -> anthropic.AsyncAnthropic:
    """Dependency getter — raises if client not initialised."""
    if _async_client is None:
        raise RuntimeError("Client not initialised. Use within lifespan() context.")
    return _async_client

async def classify(text: str) -> str:
    """Uses the shared client — no new client created."""
    client = get_client()
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=32,
        system="Classify as positive/negative/neutral. One word only.",
        messages=[{"role": "user", "content": text}],
    )
    return response.content[0].text.strip()

async def summarise(text: str) -> str:
    client = get_client()
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        system="Summarise in one sentence.",
        messages=[{"role": "user", "content": text}],
    )
    return response.content[0].text.strip()

async def main():
    async with lifespan():
        # Both functions share the same underlying connection pool
        texts = [
            "I absolutely love this product — best purchase this year!",
            "The quality was disappointing and customer service was unhelpful.",
            "Delivery was on time and the item is as described.",
        ]
        start = time.monotonic()
        results = await asyncio.gather(*[classify(t) for t in texts])
        for text, result in zip(texts, results):
            print(f"{result:10s} | {text[:50]}")
        print(f"\n3 concurrent calls in {(time.monotonic()-start)*1000:.0f}ms (shared pool)")

asyncio.run(main())

# FastAPI integration example:
# from fastapi import FastAPI, Depends
# app = FastAPI()
#
# @app.on_event("startup")
# async def startup():
#     global _async_client
#     _async_client = anthropic.AsyncAnthropic()
#
# @app.on_event("shutdown")
# async def shutdown():
#     if _async_client:
#         await _async_client.close()
#
# @app.get("/classify")
# async def classify_endpoint(text: str, client=Depends(get_client)):
#     ...
```

**Expected Token Savings:** None — same tokens; eliminates connection setup latency at scale
**Environment:** `pip install anthropic`

---

### Option 3 — Thread-Safe Client Pool for Sync Multi-Threaded Applications

For synchronous multi-threaded applications (e.g. Django, Flask with threading), maintain a pool of clients. Each thread borrows a client and returns it — avoiding both per-call creation and cross-thread sharing issues.

```python
import anthropic
import threading
import queue
import time
from contextlib import contextmanager
from typing import Generator

class SyncClientPool:
    """
    Thread-safe pool of Anthropic sync clients.
    Each client has its own httpx.Client — safe for concurrent thread access.
    """

    def __init__(self, pool_size: int = 5):
        self._pool: queue.Queue[anthropic.Anthropic] = queue.Queue(maxsize=pool_size)
        self._created = 0
        self._lock = threading.Lock()
        self._pool_size = pool_size

        # Pre-warm the pool at startup
        for _ in range(pool_size):
            self._pool.put(anthropic.Anthropic())
            self._created += 1
        print(f"[Pool] Initialised {pool_size} clients")

    @contextmanager
    def acquire(self, timeout: float = 10.0) -> Generator[anthropic.Anthropic, None, None]:
        """Borrow a client from the pool. Returns it when done."""
        try:
            client = self._pool.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError(f"No client available after {timeout}s — pool exhausted")
        try:
            yield client
        finally:
            self._pool.put(client)  # Always return to pool

    def stats(self) -> dict:
        return {
            "pool_size": self._pool_size,
            "available": self._pool.qsize(),
            "in_use": self._pool_size - self._pool.qsize(),
        }

# Application-level pool — created once
client_pool = SyncClientPool(pool_size=5)

def threaded_call(prompt: str) -> str:
    """Each thread borrows a client from the pool — no per-call creation."""
    with client_pool.acquire() as client:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=64,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text.strip()

def run_concurrent_threads(n: int = 8) -> list[str]:
    results = [None] * n
    errors = []

    def worker(idx: int):
        try:
            results[idx] = threaded_call(f"What is {idx * 7}? One number only.")
        except Exception as e:
            errors.append(str(e))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    start = time.monotonic()

    for t in threads:
        t.start()
    for t in threads:
        t.join()

    elapsed = (time.monotonic() - start) * 1000
    print(f"[Pool] {n} threads completed in {elapsed:.0f}ms | Stats: {client_pool.stats()}")
    if errors:
        print(f"[Pool] Errors: {errors}")
    return [r for r in results if r is not None]

results = run_concurrent_threads(8)
print(f"Results: {results}")
```

**Expected Token Savings:** None — same tokens; prevents connection exhaustion under thread load
**Environment:** `pip install anthropic`

---

### Option 4 — Lazy Singleton with Double-Checked Locking

For cases where the client should be created on first use (not at import), use a lazy singleton with thread-safe initialisation.

```python
import anthropic
import threading
from typing import Optional

class AnthropicClientSingleton:
    """
    Lazy singleton — client created on first use, not at import.
    Thread-safe via double-checked locking.
    """
    _instance: Optional[anthropic.Anthropic] = None
    _async_instance: Optional[anthropic.AsyncAnthropic] = None
    _lock = threading.Lock()

    @classmethod
    def get_sync(cls) -> anthropic.Anthropic:
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:  # Double-check after acquiring lock
                    cls._instance = anthropic.Anthropic()
                    print("[Singleton] Sync client created (first use)")
        return cls._instance

    @classmethod
    def get_async(cls) -> anthropic.AsyncAnthropic:
        if cls._async_instance is None:
            with cls._lock:
                if cls._async_instance is None:
                    cls._async_instance = anthropic.AsyncAnthropic()
                    print("[Singleton] Async client created (first use)")
        return cls._async_instance

    @classmethod
    def reset(cls):
        """For testing — reset the singletons."""
        with cls._lock:
            cls._instance = None
            cls._async_instance = None

# Convenience functions
def get_client() -> anthropic.Anthropic:
    return AnthropicClientSingleton.get_sync()

def get_async_client() -> anthropic.AsyncAnthropic:
    return AnthropicClientSingleton.get_async()

# Usage in agent functions — no client parameter needed
def ask(prompt: str, max_tokens: int = 256) -> str:
    response = get_client().messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text

import time, threading

# Demonstrate that concurrent calls still reuse one client
print("Calling from multiple threads...")
results = {}
def worker(idx):
    results[idx] = ask(f"What is the capital of France? One word.")

threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
for t in threads: t.start()
for t in threads: t.join()

print(f"Results: {results}")
print(f"Client created once? {AnthropicClientSingleton._instance is not None}")
```

**Expected Token Savings:** None — same tokens; eliminates repeated initialisation overhead
**Environment:** `pip install anthropic`

---

### Option 5 — Client Configuration Centralisation with Environment Validation

Centralise all client configuration — API key, timeout, retry policy, base URL — in one place. Validate at startup, share everywhere.

```python
import anthropic
import os
from dataclasses import dataclass, field
from typing import Optional

@dataclass
class ClientConfig:
    api_key: str = field(default_factory=lambda: os.environ.get("ANTHROPIC_API_KEY", ""))
    timeout: float = 60.0
    max_retries: int = 3
    base_url: Optional[str] = None   # Override for proxies / private deployments
    default_model: str = "claude-sonnet-4-6"
    default_max_tokens: int = 1024

    def __post_init__(self):
        if not self.api_key:
            raise EnvironmentError(
                "ANTHROPIC_API_KEY not set. "
                "Export it in your shell: export ANTHROPIC_API_KEY=sk-ant-..."
            )
        if not self.api_key.startswith("sk-ant-"):
            raise ValueError(f"API key format looks wrong — expected 'sk-ant-...'")
        if self.timeout < 1:
            raise ValueError(f"timeout must be >= 1s, got {self.timeout}")

class ConfiguredClientFactory:
    def __init__(self, config: ClientConfig):
        self.config = config
        self._sync_client: Optional[anthropic.Anthropic] = None
        self._async_client: Optional[anthropic.AsyncAnthropic] = None

    def sync(self) -> anthropic.Anthropic:
        if self._sync_client is None:
            kwargs = {
                "api_key": self.config.api_key,
                "timeout": self.config.timeout,
                "max_retries": self.config.max_retries,
            }
            if self.config.base_url:
                kwargs["base_url"] = self.config.base_url
            self._sync_client = anthropic.Anthropic(**kwargs)
            print(f"[Factory] Sync client created (timeout={self.config.timeout}s, retries={self.config.max_retries})")
        return self._sync_client

    def async_(self) -> anthropic.AsyncAnthropic:
        if self._async_client is None:
            kwargs = {
                "api_key": self.config.api_key,
                "timeout": self.config.timeout,
                "max_retries": self.config.max_retries,
            }
            if self.config.base_url:
                kwargs["base_url"] = self.config.base_url
            self._async_client = anthropic.AsyncAnthropic(**kwargs)
        return self._async_client

    def call(self, user_message: str, system: str = "", model: str = None, max_tokens: int = None) -> str:
        client = self.sync()
        kwargs = {
            "model": model or self.config.default_model,
            "max_tokens": max_tokens or self.config.default_max_tokens,
            "messages": [{"role": "user", "content": user_message}],
        }
        if system:
            kwargs["system"] = system
        response = client.messages.create(**kwargs)
        return response.content[0].text

# Application bootstrap
config = ClientConfig(
    timeout=30.0,
    max_retries=2,
    default_model="claude-haiku-4-5-20251001",
    default_max_tokens=256,
)
factory = ConfiguredClientFactory(config)

# All calls go through the factory — one client, centralised config
print(factory.call("What is 7 * 8? Just the number."))
print(factory.call("What is Python?", system="Answer in exactly one sentence.", max_tokens=100))
print(f"\nClient reused: {factory._sync_client is factory.sync()}")
```

**Expected Token Savings:** None — same tokens; centralised config prevents misconfiguration across call sites
**Environment:** `pip install anthropic`

---

### Option 6 — Connection Warmup and Health Check

Pre-warm the client connection at startup and run a lightweight health check. Ensures the first user request doesn't pay the cold-start penalty.

```python
import asyncio
import time
import anthropic
from typing import Optional

async_client = anthropic.AsyncAnthropic()

async def warmup_connection(timeout: float = 5.0) -> bool:
    """
    Send a minimal request to pre-establish the HTTPS connection.
    The first real user request then hits an already-open connection.
    """
    try:
        start = time.monotonic()
        response = await asyncio.wait_for(
            async_client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1,   # Minimal — just enough to open the connection
                messages=[{"role": "user", "content": "ping"}],
            ),
            timeout=timeout,
        )
        elapsed = (time.monotonic() - start) * 1000
        print(f"[Warmup] Connection established in {elapsed:.0f}ms")
        return True
    except asyncio.TimeoutError:
        print(f"[Warmup] Timed out after {timeout}s — proceeding without warmup")
        return False
    except Exception as e:
        print(f"[Warmup] Failed: {e} — proceeding without warmup")
        return False

async def health_check() -> dict:
    """Lightweight readiness check — returns status and latency."""
    try:
        start = time.monotonic()
        await async_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1,
            messages=[{"role": "user", "content": "ok"}],
        )
        latency_ms = (time.monotonic() - start) * 1000
        return {"status": "healthy", "latency_ms": round(latency_ms, 1)}
    except Exception as e:
        return {"status": "unhealthy", "error": str(e)}

async def user_request(prompt: str) -> str:
    response = await async_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text

async def main():
    print("=== Application startup ===")
    warmed = await warmup_connection()

    # Health check before serving traffic
    health = await health_check()
    print(f"[Health] {health}")

    if health["status"] != "healthy":
        raise RuntimeError("API health check failed — aborting startup")

    print("\n=== Serving requests (connection already warm) ===")
    # First request hits a warm connection — no SSL handshake penalty
    for i in range(3):
        start = time.monotonic()
        result = await user_request(f"What is {i+1} squared? One number.")
        elapsed = (time.monotonic() - start) * 1000
        print(f"Request {i+1}: {result.strip()!r} in {elapsed:.0f}ms")

asyncio.run(main())
```

**Expected Token Savings:** None — warmup costs 1 token; saves 50–200ms on first real user request
**Environment:** `pip install anthropic`

---

## Comparison

| Option | Client Lifetime | Thread Safety | Best For |
|--------|----------------|--------------|----------|
| Module-Level Singleton | Process lifetime | Single-thread / async | Scripts, simple agents |
| Async Lifecycle | App lifetime | Async safe | FastAPI, async web apps |
| Thread-Safe Pool | App lifetime | Multi-thread safe | Django, Flask, sync thread workers |
| Lazy Singleton | First use | Thread-safe (DLC) | Libraries, optional AI features |
| Config Factory | App lifetime | Configurable | Centralised config management |
| Warmup + Health Check | App lifetime | Async safe | Production services with SLA requirements |

**Recommended starting point:** Option 1 (Module-Level Singleton) — move `client = anthropic.Anthropic()` to the top of each module. A 30-second change that eliminates all per-call client creation overhead. For async apps, add Option 2's lifecycle management to ensure clean shutdown.
