---
title: "Agent doesn't implement async context manager resource lifecycle"
description: "Agents acquire HTTP clients, database connections, file handles, and semaphores at the start of a request but never guarantee cleanup on cancellation, exception, or timeout — causing resource leaks that accumulate until the process crashes."
difficulty: intermediate
category: concurrency
tags: [asyncio, context-manager, resource-lifecycle, cleanup, aiohttp, connection-pooling]
---

## Problem

Every async resource — an `aiohttp.ClientSession`, a database connection, an asyncio `Lock`, a temporary file — must be released even when the coroutine is cancelled, raises an exception, or times out. Without `async with` and proper `__aenter__`/`__aexit__` semantics, any early exit path leaks the resource. A thousand cancelled requests later, the agent has exhausted its file descriptor limit, connection pool, or memory.

```python
# BAD: resource leak on exception or cancellation
async def call_tool(url: str) -> dict:
    session = aiohttp.ClientSession()   # created
    resp = await session.get(url)       # if this raises or is cancelled...
    data = await resp.json()
    await session.close()               # ...this never runs
    return data
```

## Solution 1: Standard async context manager with `async with`

The simplest fix: use the library's built-in async context manager so `__aexit__` always runs, even on cancellation.

```python
import asyncio
import aiohttp
from typing import Any


async def call_tool(url: str) -> dict[str, Any]:
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            resp.raise_for_status()
            return await resp.json()


# ── Reusable session shared across calls ────────────────────────────
class ToolClient:
    """Share one session for the lifetime of the agent; close on shutdown."""

    def __init__(self):
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> "ToolClient":
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30),
            connector=aiohttp.TCPConnector(limit=20),
        )
        return self

    async def __aexit__(self, *args):
        if self._session:
            await self._session.close()
            self._session = None

    async def get(self, url: str) -> dict:
        assert self._session, "ToolClient not entered"
        async with self._session.get(url) as resp:
            resp.raise_for_status()
            return await resp.json()


# ── Usage ────────────────────────────────────────────────────────────
async def agent_run():
    async with ToolClient() as client:
        result = await client.get("https://api.example.com/data")
        print(result)


asyncio.run(agent_run())
```

## Solution 2: Custom `AsyncContextManager` base class with guaranteed cleanup stack

Build a cleanup stack (similar to `contextlib.AsyncExitStack`) that runs all registered cleanup functions in reverse order, even when one of them raises.

```python
import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator, Callable, Awaitable
import aiohttp
import tempfile
import os


class AsyncResourceStack:
    """
    Accumulates async cleanup callbacks and runs them in LIFO order on exit.
    Exceptions in cleanup are logged but do not abort subsequent cleanups.
    """

    def __init__(self):
        self._cleanups: list[Callable[[], Awaitable[None]]] = []

    def push(self, fn: Callable[[], Awaitable[None]]):
        self._cleanups.append(fn)

    async def __aenter__(self) -> "AsyncResourceStack":
        return self

    async def __aexit__(self, *args):
        errors = []
        for cleanup in reversed(self._cleanups):
            try:
                await cleanup()
            except Exception as e:
                errors.append(e)
        if errors:
            raise ExceptionGroup("Resource cleanup errors", errors)

    @asynccontextmanager
    async def open_session(self) -> AsyncIterator[aiohttp.ClientSession]:
        session = aiohttp.ClientSession()
        self.push(session.close)
        yield session

    @asynccontextmanager
    async def open_tempfile(self, suffix: str = ".tmp") -> AsyncIterator[str]:
        fd, path = tempfile.mkstemp(suffix=suffix)
        os.close(fd)
        self.push(lambda: asyncio.to_thread(os.unlink, path) if os.path.exists(path) else asyncio.sleep(0))
        yield path


# ── Usage ────────────────────────────────────────────────────────────
async def agent_with_stack():
    async with AsyncResourceStack() as stack:
        async with stack.open_session() as session:
            async with stack.open_tempfile(".json") as tmp_path:
                # Work with session and tmp_path — both cleaned up on exit
                async with session.get("https://httpbin.org/get") as resp:
                    data = await resp.json()
                with open(tmp_path, "w") as f:
                    import json
                    json.dump(data, f)
                print(f"Written to {tmp_path}")
    # Session closed, temp file deleted — even if an exception occurred


asyncio.run(agent_with_stack())
```

## Solution 3: Resource pool with context-managed acquisition

Instead of creating resources on demand, pre-create a bounded pool and acquire from it. Each acquisition is a context manager that returns the resource to the pool on exit.

```python
import asyncio
from typing import TypeVar, Generic, AsyncIterator
from contextlib import asynccontextmanager
import aiohttp

T = TypeVar("T")


class AsyncPool(Generic[T]):
    """
    Generic async resource pool. Resources are created lazily and returned on release.
    Blocked acquisitions time out after `acquire_timeout` seconds.
    """

    def __init__(
        self,
        factory,
        size: int = 10,
        acquire_timeout: float = 5.0,
    ):
        self._factory = factory
        self._size = size
        self._timeout = acquire_timeout
        self._sem = asyncio.Semaphore(size)
        self._resources: asyncio.Queue[T] = asyncio.Queue()
        self._created = 0

    async def _get_or_create(self) -> T:
        try:
            return self._resources.get_nowait()
        except asyncio.QueueEmpty:
            resource = await self._factory()
            self._created += 1
            return resource

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[T]:
        try:
            await asyncio.wait_for(self._sem.acquire(), timeout=self._timeout)
        except asyncio.TimeoutError:
            raise TimeoutError(f"Pool exhausted: no resource available within {self._timeout}s")
        resource = await self._get_or_create()
        try:
            yield resource
        finally:
            await self._resources.put(resource)
            self._sem.release()

    async def close_all(self):
        while not self._resources.empty():
            resource = await self._resources.get()
            if hasattr(resource, "close"):
                await resource.close()


# ── Specialised HTTP session pool ─────────────────────────────────────
async def create_session() -> aiohttp.ClientSession:
    return aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=30),
        connector=aiohttp.TCPConnector(limit=5),
    )


session_pool = AsyncPool(factory=create_session, size=5)


async def pooled_get(url: str) -> dict:
    async with session_pool.acquire() as session:
        async with session.get(url) as resp:
            resp.raise_for_status()
            return await resp.json()


# ── Usage ────────────────────────────────────────────────────────────
async def main():
    try:
        results = await asyncio.gather(*[
            pooled_get("https://httpbin.org/get") for _ in range(8)
        ])
        print(f"Got {len(results)} results")
    finally:
        await session_pool.close_all()


asyncio.run(main())
```

## Solution 4: `contextlib.AsyncExitStack` for dynamic resource composition

When the set of resources to acquire is determined at runtime (e.g., different tools require different connections), use `AsyncExitStack` to compose context managers dynamically.

```python
import asyncio
from contextlib import AsyncExitStack
from typing import Any
import aiohttp


class DynamicToolRunner:
    """
    Acquires exactly the resources each tool requires, releases all of them
    on completion — including on exception or timeout.
    """

    RESOURCE_REGISTRY = {
        "web_search": lambda: aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10)
        ),
        "database": lambda: aiohttp.ClientSession(  # placeholder
            timeout=aiohttp.ClientTimeout(total=5)
        ),
    }

    async def run(self, tool_names: list[str], task: dict) -> dict[str, Any]:
        async with AsyncExitStack() as stack:
            resources: dict[str, aiohttp.ClientSession] = {}

            for name in tool_names:
                factory = self.RESOURCE_REGISTRY.get(name)
                if factory:
                    resource = factory()
                    session = await stack.enter_async_context(resource)
                    resources[name] = session

            # Also register a cleanup side-effect
            stack.callback(lambda: print(f"Cleaned up {list(resources.keys())}"))

            return await self._execute(resources, task)

    async def _execute(
        self, resources: dict[str, Any], task: dict
    ) -> dict[str, Any]:
        results = {}
        if "web_search" in resources:
            async with resources["web_search"].get("https://httpbin.org/get") as r:
                results["web"] = await r.json()
        return results


# ── Usage ────────────────────────────────────────────────────────────
async def main():
    runner = DynamicToolRunner()
    result = await runner.run(["web_search"], {"query": "AI agent patterns"})
    print(result.keys())


asyncio.run(main())
```

## Solution 5: Cancellation-safe resource guard using `asyncio.shield`

Some cleanup operations (e.g., committing a transaction, flushing a buffer) must survive even if the parent task is cancelled. Wrap them in `asyncio.shield`.

```python
import asyncio
from typing import Any


class CancellationSafeResource:
    """
    A resource whose cleanup is shielded from cancellation.
    The cleanup runs to completion even if the caller is cancelled.
    """

    def __init__(self, name: str):
        self.name = name
        self._buffer: list[str] = []
        self._flushed = False

    async def write(self, data: str):
        self._buffer.append(data)

    async def flush(self):
        """Must complete even on cancellation — shields itself."""
        await asyncio.shield(self._do_flush())

    async def _do_flush(self):
        await asyncio.sleep(0.1)  # simulate I/O
        print(f"[{self.name}] Flushed {len(self._buffer)} items")
        self._buffer.clear()
        self._flushed = True

    async def __aenter__(self) -> "CancellationSafeResource":
        return self

    async def __aexit__(self, exc_type, exc, tb):
        try:
            await self.flush()
        except asyncio.CancelledError:
            # shield ensures flush completes; re-raise to propagate cancellation
            raise


async def agent_task():
    async with CancellationSafeResource("output-buffer") as res:
        for i in range(5):
            await res.write(f"item-{i}")
        # Simulate cancellation mid-task
        await asyncio.sleep(0.05)
        raise asyncio.CancelledError()
    # __aexit__ flushes before propagating the cancellation


async def main():
    task = asyncio.create_task(agent_task())
    try:
        await task
    except asyncio.CancelledError:
        print("Task cancelled — but flush completed safely")


asyncio.run(main())
```

## Solution 6: Resource lifecycle manager with health checking and auto-reconnection

For long-running agents, resources can go stale (closed connections, expired tokens). A lifecycle manager periodically health-checks resources and replaces them transparently.

```python
import asyncio
import time
from typing import TypeVar, Generic, Callable, Awaitable
from contextlib import asynccontextmanager, suppress

T = TypeVar("T")


class LifecycleManagedResource(Generic[T]):
    """
    Holds a single resource that is automatically recreated if health check fails.
    Provides an async context manager for safe acquisition.
    """

    def __init__(
        self,
        factory: Callable[[], Awaitable[T]],
        health_check: Callable[[T], Awaitable[bool]],
        closer: Callable[[T], Awaitable[None]],
        health_interval: float = 30.0,
        max_age_seconds: float = 300.0,
    ):
        self._factory = factory
        self._health_check = health_check
        self._closer = closer
        self._interval = health_interval
        self._max_age = max_age_seconds

        self._resource: T | None = None
        self._created_at: float = 0.0
        self._lock = asyncio.Lock()
        self._monitor_task: asyncio.Task | None = None

    async def start(self):
        self._resource = await self._factory()
        self._created_at = time.monotonic()
        self._monitor_task = asyncio.create_task(self._monitor(), name="resource-lifecycle-monitor")

    async def stop(self):
        if self._monitor_task:
            self._monitor_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._monitor_task
        if self._resource:
            await self._closer(self._resource)
            self._resource = None

    async def _monitor(self):
        while True:
            await asyncio.sleep(self._interval)
            async with self._lock:
                if self._resource is None:
                    continue
                age = time.monotonic() - self._created_at
                try:
                    healthy = await self._health_check(self._resource)
                except Exception:
                    healthy = False

                if not healthy or age > self._max_age:
                    print(f"Resource unhealthy or expired (age={age:.0f}s) — recreating")
                    with suppress(Exception):
                        await self._closer(self._resource)
                    self._resource = await self._factory()
                    self._created_at = time.monotonic()

    @asynccontextmanager
    async def acquire(self):
        async with self._lock:
            if self._resource is None:
                raise RuntimeError("Resource not started")
            yield self._resource


# ── Example: managed aiohttp session ─────────────────────────────────
import aiohttp


async def make_session() -> aiohttp.ClientSession:
    return aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))


async def check_session(session: aiohttp.ClientSession) -> bool:
    return not session.closed


async def close_session(session: aiohttp.ClientSession):
    await session.close()


async def main():
    mgr = LifecycleManagedResource(
        factory=make_session,
        health_check=check_session,
        closer=close_session,
        health_interval=10.0,
        max_age_seconds=60.0,
    )
    await mgr.start()
    try:
        async with mgr.acquire() as session:
            async with session.get("https://httpbin.org/get") as resp:
                data = await resp.json()
                print(data.get("url"))
    finally:
        await mgr.stop()


asyncio.run(main())
```

## Comparison

| Approach | Handles cancellation | Handles exceptions | Pooled | Dynamic composition | Auto-reconnect |
|---|---|---|---|---|---|
| `async with` built-in | Yes | Yes | No | No | No |
| Custom cleanup stack | Yes | Yes (all run) | No | Yes | No |
| Resource pool | Yes | Yes | Yes | No | No |
| `AsyncExitStack` | Yes | Yes | No | Yes | No |
| Cancellation-safe shield | Yes (flush survives) | Yes | No | No | No |
| Lifecycle manager | Yes | Yes | No | No | Yes |

**Recommendation**: Use **`async with` built-in** (Solution 1) for single-use resources. Use **`AsyncExitStack`** (Solution 4) when composing a dynamic set of resources at runtime. Use **Lifecycle manager** (Solution 6) for long-running agents where connections go stale over hours of operation.
