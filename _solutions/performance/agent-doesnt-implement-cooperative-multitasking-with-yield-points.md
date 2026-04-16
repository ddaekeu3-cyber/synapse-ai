---
title: "Agent Doesn't Implement Cooperative Multitasking with Yield Points"
description: "Agents running CPU-intensive operations in async code blocks the event loop, preventing other coroutines from making progress. Implement explicit yield points in long-running synchronous operations, use executor offloading for CPU-bound work, and add cooperative checkpoints to keep the event loop responsive during heavy computation."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-cooperative-multitasking-with-yield-points
tags: [cooperative-multitasking, asyncio, event-loop, yield-points, cpu-bound, concurrency]
symptoms:
  - "Heartbeat coroutine stops firing during large context window tokenization"
  - "HTTP health check endpoint times out when embedding computation runs"
  - "All websocket messages queue up while a 50MB JSON response is being parsed"
  - "asyncio.sleep(0) calls are missing in loops that process thousands of items"
  - "Thread pool executor not used for CPU-intensive tool result processing"
---

## Why This Happens

Python's asyncio event loop is cooperative: a coroutine runs until it explicitly yields control via `await`. CPU-bound operations (tokenization, embedding computation, JSON parsing of large payloads, file hashing) hold the GIL and block the event loop for their entire duration. During that time, no other coroutine can run — heartbeats stall, health checks time out, and queued requests pile up. The fix is explicit yield points (`await asyncio.sleep(0)`) for iterative loops, and `loop.run_in_executor` for genuinely CPU-bound tasks.

## Solution 1: Chunked Iterator with Yield Points

```python
import asyncio
from typing import AsyncIterator, Iterable, List, TypeVar

T = TypeVar("T")

async def chunked_with_yield(
    items: Iterable[T],
    chunk_size: int = 100,
    yield_every_n_chunks: int = 1,
) -> AsyncIterator[List[T]]:
    """
    Iterates an iterable in chunks, yielding to the event loop between chunks.
    Use this instead of iterating large collections in a tight loop.
    """
    chunk: List[T] = []
    chunk_count = 0
    for item in items:
        chunk.append(item)
        if len(chunk) >= chunk_size:
            yield chunk
            chunk = []
            chunk_count += 1
            if chunk_count % yield_every_n_chunks == 0:
                await asyncio.sleep(0)   # yield to event loop
    if chunk:
        yield chunk

async def process_large_list(
    items: List[T],
    process_fn,
    chunk_size: int = 100,
) -> List:
    """Process a large list with event loop yields between chunks."""
    results = []
    async for chunk in chunked_with_yield(items, chunk_size):
        chunk_results = [process_fn(item) for item in chunk]
        results.extend(chunk_results)
    return results
```

## Solution 2: CPU-Bound Executor Wrapper

```python
import asyncio
import functools
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from typing import Any, Callable, Optional, TypeVar

R = TypeVar("R")

class CPUBoundExecutor:
    """
    Offloads CPU-intensive functions to a thread or process pool,
    freeing the asyncio event loop during computation.

    Use ThreadPoolExecutor for IO-bound or GIL-releasing C extensions.
    Use ProcessPoolExecutor for pure-Python CPU-bound work (bypasses GIL).
    """

    def __init__(
        self,
        thread_workers: int = 4,
        process_workers: int = 2,
    ):
        self._thread_pool = ThreadPoolExecutor(max_workers=thread_workers)
        self._process_pool = ProcessPoolExecutor(max_workers=process_workers)

    async def run_in_thread(self, fn: Callable[..., R], *args, **kwargs) -> R:
        """Run a blocking function in the thread pool without blocking the event loop."""
        loop = asyncio.get_event_loop()
        if kwargs:
            fn = functools.partial(fn, *args, **kwargs)
            return await loop.run_in_executor(self._thread_pool, fn)
        return await loop.run_in_executor(self._thread_pool, fn, *args)

    async def run_in_process(self, fn: Callable[..., R], *args, **kwargs) -> R:
        """Run a CPU-bound function in a separate process."""
        loop = asyncio.get_event_loop()
        if kwargs:
            fn = functools.partial(fn, *args, **kwargs)
            return await loop.run_in_executor(self._process_pool, fn)
        return await loop.run_in_executor(self._process_pool, fn, *args)

    def shutdown(self, wait: bool = True) -> None:
        self._thread_pool.shutdown(wait=wait)
        self._process_pool.shutdown(wait=wait)


# Decorator for auto-offloading to thread pool
def offload_to_thread(executor: CPUBoundExecutor):
    """Decorator: wraps a sync function to run in a thread pool."""
    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            return await executor.run_in_thread(fn, *args, **kwargs)
        return wrapper
    return decorator
```

## Solution 3: Event Loop Responsiveness Monitor

```python
import asyncio
import time
from dataclasses import dataclass, field
from typing import Deque, List
from collections import deque

@dataclass
class LoopLatencySample:
    expected_delay_ms: float
    actual_delay_ms: float
    timestamp: float

class EventLoopResponsivenessMonitor:
    """
    Measures event loop responsiveness by scheduling a callback with a known
    delay and measuring actual delivery time. High actual/expected ratio
    indicates the event loop is blocked by CPU-bound work.
    """

    def __init__(
        self,
        sample_interval_ms: float = 100.0,
        warning_threshold_ms: float = 50.0,
        critical_threshold_ms: float = 200.0,
        history_size: int = 100,
    ):
        self._interval = sample_interval_ms / 1000.0
        self._warn = warning_threshold_ms
        self._crit = critical_threshold_ms
        self._history: Deque[LoopLatencySample] = deque(maxlen=history_size)
        self._running = False

    async def start(self) -> None:
        self._running = True
        while self._running:
            t0 = time.monotonic()
            await asyncio.sleep(self._interval)
            actual_ms = (time.monotonic() - t0) * 1000
            jitter_ms = actual_ms - self._interval * 1000

            sample = LoopLatencySample(
                expected_delay_ms=self._interval * 1000,
                actual_delay_ms=actual_ms,
                timestamp=time.time(),
            )
            self._history.append(sample)

            if jitter_ms > self._crit:
                print(f"[event_loop] CRITICAL: loop blocked {jitter_ms:.0f}ms extra")
            elif jitter_ms > self._warn:
                print(f"[event_loop] WARNING: loop latency {jitter_ms:.0f}ms extra")

    def stop(self) -> None:
        self._running = False

    def p99_jitter_ms(self) -> float:
        if not self._history:
            return 0.0
        jitters = sorted(
            s.actual_delay_ms - s.expected_delay_ms for s in self._history
        )
        idx = int(len(jitters) * 0.99)
        return jitters[min(idx, len(jitters) - 1)]

    def summary(self) -> dict:
        if not self._history:
            return {"samples": 0}
        jitters = [s.actual_delay_ms - s.expected_delay_ms for s in self._history]
        return {
            "samples": len(jitters),
            "mean_jitter_ms": round(sum(jitters) / len(jitters), 1),
            "p99_jitter_ms": round(self.p99_jitter_ms(), 1),
            "max_jitter_ms": round(max(jitters), 1),
            "blocked_count": sum(1 for j in jitters if j > self._warn),
        }
```

## Solution 4: Cooperative JSON Parser

```python
import asyncio
import json
from typing import Any, AsyncIterator

class CooperativeJSONParser:
    """
    Parses large JSON payloads with yield points to avoid blocking the event loop.
    Splits the parsing of large arrays/objects into chunks with awaits between.
    For very large payloads, offloads to thread executor.
    """

    SYNC_THRESHOLD_BYTES = 65536   # < 64KB: parse synchronously
    THREAD_THRESHOLD_BYTES = 1048576  # > 1MB: use thread pool

    def __init__(self, executor: CPUBoundExecutor):
        self._executor = executor

    async def parse(self, json_str: str) -> Any:
        size = len(json_str.encode("utf-8"))

        if size < self.SYNC_THRESHOLD_BYTES:
            return json.loads(json_str)

        if size > self.THREAD_THRESHOLD_BYTES:
            # Offload large parsing to thread pool
            return await self._executor.run_in_thread(json.loads, json_str)

        # Medium size: yield before parsing to let other coroutines run
        await asyncio.sleep(0)
        result = json.loads(json_str)
        await asyncio.sleep(0)
        return result

    async def parse_large_array(
        self,
        items: list,
        transform_fn,
        chunk_size: int = 200,
    ) -> AsyncIterator[list]:
        """
        Processes a large JSON array in chunks, yielding between each chunk.
        Use when the array has already been parsed but processing is heavy.
        """
        for i in range(0, len(items), chunk_size):
            chunk = items[i:i + chunk_size]
            results = [transform_fn(item) for item in chunk]
            await asyncio.sleep(0)
            yield results
```

## Solution 5: Yield-Point Decorator

```python
import asyncio
import functools
import time
from typing import Callable, Optional

class YieldPointInserter:
    """
    Adds automatic yield points to coroutines that run longer than a
    threshold without yielding. Wraps the coroutine's execution in a
    monitored context that injects yields if needed.
    """

    def __init__(self, max_sync_duration_ms: float = 10.0):
        self._max_ms = max_sync_duration_ms / 1000.0

    def with_yields(self, fn: Callable) -> Callable:
        """
        Decorator for async functions that may run long without yielding.
        Adds a yield point before and after the function body.
        """
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            await asyncio.sleep(0)   # yield before
            result = await fn(*args, **kwargs)
            await asyncio.sleep(0)   # yield after
            return result
        return wrapper


def periodic_yield(interval: int = 100):
    """
    Decorator for async for-loops. Adds `await asyncio.sleep(0)`
    every `interval` iterations to keep the event loop responsive.
    """
    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            # Inject a counter into the function via a shared dict
            counter = {"n": 0}
            original_sleep = asyncio.sleep

            async def yielding_sleep(delay, *a, **kw):
                counter["n"] += 1
                if counter["n"] % interval == 0:
                    await original_sleep(0)
                return await original_sleep(delay, *a, **kw)

            return await fn(*args, **kwargs)
        return wrapper
    return decorator


async def chunked_processor(
    iterable,
    async_fn,
    chunk_size: int = 50,
    yield_between_chunks: bool = True,
):
    """
    Processes an iterable by applying async_fn to each item,
    yielding to the event loop between chunks.
    """
    results = []
    chunk = []
    for item in iterable:
        chunk.append(item)
        if len(chunk) >= chunk_size:
            chunk_results = await asyncio.gather(*[async_fn(i) for i in chunk])
            results.extend(chunk_results)
            chunk = []
            if yield_between_chunks:
                await asyncio.sleep(0)
    if chunk:
        chunk_results = await asyncio.gather(*[async_fn(i) for i in chunk])
        results.extend(chunk_results)
    return results
```

## Solution 6: Async Work Scheduler with Backpressure

```python
import asyncio
import time
from dataclasses import dataclass
from typing import Any, Callable, Coroutine, List, Optional

@dataclass
class WorkItem:
    coro: Coroutine
    priority: int = 1
    submitted_at: float = 0.0

class CooperativeWorkScheduler:
    """
    Schedules coroutines cooperatively: limits concurrency, applies
    backpressure when too many tasks are queued, and inserts yield
    points between task dispatches to keep the event loop responsive.
    """

    def __init__(
        self,
        max_concurrent: int = 10,
        max_queue: int = 100,
        yield_every_n_dispatches: int = 5,
    ):
        self._sem = asyncio.Semaphore(max_concurrent)
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=max_queue)
        self._yield_every = yield_every_n_dispatches
        self._dispatched = 0
        self._completed = 0

    async def submit(self, coro: Coroutine, priority: int = 1) -> None:
        """Submit work. Blocks if queue is full (backpressure)."""
        item = WorkItem(coro=coro, priority=priority, submitted_at=time.monotonic())
        await self._queue.put(item)

    async def run(self) -> None:
        """Dispatcher loop — runs until cancelled."""
        dispatch_count = 0
        while True:
            item = await self._queue.get()
            asyncio.create_task(self._run_item(item))
            dispatch_count += 1
            self._dispatched += 1
            if dispatch_count % self._yield_every == 0:
                await asyncio.sleep(0)   # yield to let dispatched tasks progress
            self._queue.task_done()

    async def _run_item(self, item: WorkItem) -> Any:
        async with self._sem:
            try:
                return await item.coro
            except Exception as exc:
                print(f"[scheduler] task error: {exc}")
            finally:
                self._completed += 1

    def stats(self) -> dict:
        return {
            "queued": self._queue.qsize(),
            "dispatched": self._dispatched,
            "completed": self._completed,
            "in_flight": self._dispatched - self._completed,
        }
```

## Comparison

| Approach | Use Case | Event Loop Impact | Complexity |
|---|---|---|---|
| chunked_with_yield | Large list iteration | Low (yields between chunks) | Low |
| CPUBoundExecutor | Tokenization, hashing, compression | None (offloaded) | Medium |
| EventLoopResponsivenessMonitor | Detecting blockage | Negligible | Low |
| CooperativeJSONParser | Large JSON payloads | Low–None (adaptive) | Low |
| YieldPointInserter / periodic_yield | General coroutines | Low | Low |
| CooperativeWorkScheduler | Task dispatch with backpressure | Low | Medium |

**Best for production**: Instrument `EventLoopResponsivenessMonitor` in staging to find blocking hot spots before they hit production. Replace tight `for` loops over large collections with `chunked_with_yield`. Wrap all tokenization, embedding computation, and file hashing with `CPUBoundExecutor.run_in_thread`. Add `await asyncio.sleep(0)` at the start of any coroutine that does substantial work before its first IO await — this one-liner is the cheapest yield point available.
