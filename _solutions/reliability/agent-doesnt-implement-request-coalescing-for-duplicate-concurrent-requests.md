---
title: "Agent Doesn't Implement Request Coalescing for Duplicate Concurrent Requests"
description: "When multiple agent sessions issue identical requests simultaneously, each one hits the upstream API independently, multiplying cost and load. Implement request coalescing to collapse concurrent identical requests into a single upstream call whose result is shared by all waiters."
date: 2026-04-16
difficulty: intermediate
category: reliability
slug: agent-doesnt-implement-request-coalescing-for-duplicate-concurrent-requests
tags: [request-coalescing, deduplication, concurrency, reliability, caching, thundering-herd]
symptoms:
  - "100 concurrent sessions asking the same question fire 100 identical LLM requests"
  - "Cache miss storm: all callers miss simultaneously on a cold cache and hit the DB in parallel"
  - "Embedding API billed N times for the same text submitted N times concurrently"
  - "Rate limit errors on embedding/search APIs when multiple agents embed the same query"
  - "Upstream CPU spikes on repeated identical tool calls from parallel agent instances"
---

## Why This Happens

Request caching handles sequential duplicates well but fails for concurrent ones: if 50 requests for the same key arrive before the first response returns, all 50 miss the empty cache and issue upstream calls. Request coalescing solves the concurrent case by parking all callers behind a single in-flight future. When the first response arrives, every waiter receives the same result. This is also called "single-flight" or "request deduplication."

## Solution 1: Single-Flight Coalescer with asyncio.Future

```python
import asyncio
from typing import Any, Callable, Dict, Optional, Awaitable

class SingleFlightCoalescer:
    """
    Collapses concurrent identical requests into a single upstream call.
    All callers with the same key wait for the same Future.
    On completion, the result is broadcast to all waiters then evicted.
    """

    def __init__(self):
        self._in_flight: Dict[str, asyncio.Future] = {}
        self._lock = asyncio.Lock()

    async def do(self, key: str, fn: Callable[[], Awaitable[Any]]) -> Any:
        async with self._lock:
            if key in self._in_flight:
                future = self._in_flight[key]
                # Already in-flight — wait for it
                waiting = True
            else:
                # First caller — create a future and kick off the request
                future = asyncio.get_event_loop().create_future()
                self._in_flight[key] = future
                waiting = False

        if waiting:
            return await asyncio.shield(future)

        try:
            result = await fn()
            async with self._lock:
                self._in_flight.pop(key, None)
            future.set_result(result)
            return result
        except Exception as exc:
            async with self._lock:
                self._in_flight.pop(key, None)
            future.set_exception(exc)
            raise

    def in_flight_count(self) -> int:
        return len(self._in_flight)


# Usage: embedding coalescer
class CoalescedEmbedder:
    def __init__(self, embed_fn: Callable[[str], Awaitable[list]]):
        self._embed = embed_fn
        self._coalescer = SingleFlightCoalescer()

    async def embed(self, text: str) -> list:
        # Normalize key so whitespace/case variants also coalesce
        key = text.strip().lower()
        return await self._coalescer.do(key, lambda: self._embed(text))
```

## Solution 2: Coalescer with Result Cache (Coalesce + Cache)

```python
import asyncio
import time
from typing import Any, Callable, Dict, Optional, Awaitable

class CoalescingCache:
    """
    Combines single-flight coalescing with a TTL result cache.
    - Concurrent identical requests: coalesced (one upstream call).
    - Sequential identical requests within TTL: served from cache.
    - After TTL expiry: next caller triggers a new single-flight.
    """

    def __init__(self, ttl_seconds: float = 60.0):
        self._ttl = ttl_seconds
        self._cache: Dict[str, tuple] = {}      # key -> (value, expires_at)
        self._in_flight: Dict[str, asyncio.Future] = {}
        self._lock = asyncio.Lock()

    async def get_or_fetch(self, key: str, fn: Callable[[], Awaitable[Any]]) -> Any:
        async with self._lock:
            # Check cache first
            cached = self._cache.get(key)
            if cached is not None:
                value, expires_at = cached
                if time.monotonic() < expires_at:
                    return value
                else:
                    del self._cache[key]

            # Check in-flight
            if key in self._in_flight:
                future = self._in_flight[key]
                is_leader = False
            else:
                future = asyncio.get_event_loop().create_future()
                self._in_flight[key] = future
                is_leader = True

        if not is_leader:
            return await asyncio.shield(future)

        try:
            result = await fn()
            async with self._lock:
                self._cache[key] = (result, time.monotonic() + self._ttl)
                self._in_flight.pop(key, None)
            future.set_result(result)
            return result
        except Exception as exc:
            async with self._lock:
                self._in_flight.pop(key, None)
            future.set_exception(exc)
            raise

    def invalidate(self, key: str) -> None:
        self._cache.pop(key, None)

    def cache_size(self) -> int:
        return len(self._cache)
```

## Solution 3: Batch Coalescer (Accumulate Window then Batch-Execute)

```python
import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Awaitable

@dataclass
class PendingRequest:
    key: str
    future: asyncio.Future
    queued_at: float = field(default_factory=time.monotonic)

class WindowedBatchCoalescer:
    """
    Accumulates distinct requests over a short time window, then
    executes them all in a single batch call. Ideal for embedding
    APIs that accept batch inputs.
    """

    def __init__(
        self,
        batch_fn: Callable[[List[str]], Awaitable[Dict[str, Any]]],
        window_ms: float = 20.0,
        max_batch_size: int = 100,
    ):
        self._batch_fn = batch_fn
        self._window = window_ms / 1000.0
        self._max_size = max_batch_size
        self._pending: Dict[str, List[asyncio.Future]] = {}
        self._lock = asyncio.Lock()
        self._flush_scheduled = False

    async def request(self, key: str) -> Any:
        future = asyncio.get_event_loop().create_future()
        async with self._lock:
            if key not in self._pending:
                self._pending[key] = []
            self._pending[key].append(future)
            should_schedule = not self._flush_scheduled
            if len(self._pending) >= self._max_size:
                # Flush immediately on max size
                asyncio.create_task(self._flush())
                self._flush_scheduled = True
                should_schedule = False

        if should_schedule:
            async with self._lock:
                if not self._flush_scheduled:
                    self._flush_scheduled = True
                    asyncio.get_event_loop().call_later(self._window, lambda: asyncio.create_task(self._flush()))

        return await future

    async def _flush(self) -> None:
        async with self._lock:
            if not self._pending:
                self._flush_scheduled = False
                return
            batch = dict(self._pending)
            self._pending.clear()
            self._flush_scheduled = False

        keys = list(batch.keys())
        try:
            results = await self._batch_fn(keys)
            for key, futures in batch.items():
                result = results.get(key)
                for f in futures:
                    if not f.done():
                        f.set_result(result)
        except Exception as exc:
            for futures in batch.values():
                for f in futures:
                    if not f.done():
                        f.set_exception(exc)
```

## Solution 4: Keyed Semaphore for Resource-Limited Coalescing

```python
import asyncio
from typing import Any, Callable, Dict, Awaitable

class KeyedSemaphoreCoalescer:
    """
    Limits concurrent in-flight requests per key to exactly 1.
    Additional callers queue up and re-use the result of the in-flight request.
    Unlike single-flight, this version serializes retries (new call after each completion).
    """

    def __init__(self):
        self._semaphores: Dict[str, asyncio.Semaphore] = {}
        self._results: Dict[str, Any] = {}
        self._errors: Dict[str, Exception] = {}
        self._lock = asyncio.Lock()

    async def _get_semaphore(self, key: str) -> asyncio.Semaphore:
        async with self._lock:
            if key not in self._semaphores:
                self._semaphores[key] = asyncio.Semaphore(1)
            return self._semaphores[key]

    async def execute(self, key: str, fn: Callable[[], Awaitable[Any]]) -> Any:
        sem = await self._get_semaphore(key)
        async with sem:
            # If a prior caller just finished and stored result, reuse it briefly
            if key in self._results:
                return self._results.pop(key)
            if key in self._errors:
                raise self._errors.pop(key)
            try:
                result = await fn()
                self._results[key] = result
                return result
            except Exception as exc:
                self._errors[key] = exc
                raise
```

## Solution 5: Distributed Coalescer with Redis Lock

```python
import asyncio
import time
import uuid
from typing import Any, Callable, Awaitable, Optional

class RedisCoalescingCache:
    """
    Distributed coalescer: uses Redis SETNX as a lock so only one
    process across a cluster performs the upstream call. Others
    poll for the cached result.
    """

    def __init__(self, redis, ttl_seconds: int = 60, lock_timeout: int = 10):
        self._redis = redis
        self._ttl = ttl_seconds
        self._lock_ttl = lock_timeout

    async def get_or_fetch(
        self, key: str, fn: Callable[[], Awaitable[Any]], poll_interval: float = 0.05
    ) -> Any:
        import json
        cache_key = f"coalesce:result:{key}"
        lock_key = f"coalesce:lock:{key}"
        lock_value = str(uuid.uuid4())

        # Check cache
        cached = await self._redis.get(cache_key)
        if cached:
            return json.loads(cached)

        # Try to acquire lock
        acquired = await self._redis.set(lock_key, lock_value, nx=True, ex=self._lock_ttl)

        if acquired:
            try:
                result = await fn()
                await self._redis.setex(cache_key, self._ttl, json.dumps(result))
                return result
            finally:
                # Release lock only if we still own it
                lua = """
                if redis.call('get', KEYS[1]) == ARGV[1] then
                    return redis.call('del', KEYS[1])
                end
                return 0
                """
                await self._redis.eval(lua, 1, lock_key, lock_value)
        else:
            # Another process is fetching — poll until result appears
            deadline = time.monotonic() + self._lock_ttl
            while time.monotonic() < deadline:
                await asyncio.sleep(poll_interval)
                cached = await self._redis.get(cache_key)
                if cached:
                    return json.loads(cached)
                # Check if lock is still held (process may have crashed)
                if not await self._redis.exists(lock_key):
                    # Lock released without caching — retry from scratch
                    return await self.get_or_fetch(key, fn, poll_interval)
            raise TimeoutError(f"Coalescing wait timeout for key={key}")
```

## Solution 6: Coalescer Metrics and Deduplication Stats

```python
import asyncio
import time
from dataclasses import dataclass

@dataclass
class CoalescerStats:
    requests_received: int = 0
    upstream_calls_made: int = 0
    requests_coalesced: int = 0
    errors: int = 0
    total_wait_ms: float = 0.0

class InstrumentedCoalescer:
    """Wraps SingleFlightCoalescer and tracks deduplication efficiency."""

    def __init__(self, inner: SingleFlightCoalescer):
        self._inner = inner
        self._stats = CoalescerStats()

    async def do(self, key: str, fn, is_leader: bool = False) -> Any:
        self._stats.requests_received += 1
        t0 = time.monotonic()

        was_in_flight = key in self._inner._in_flight

        try:
            result = await self._inner.do(key, fn)
            elapsed = (time.monotonic() - t0) * 1000
            self._stats.total_wait_ms += elapsed
            if was_in_flight:
                self._stats.requests_coalesced += 1
            else:
                self._stats.upstream_calls_made += 1
            return result
        except Exception:
            self._stats.errors += 1
            raise

    def stats(self) -> dict:
        r = self._stats
        total = max(r.requests_received, 1)
        return {
            "requests_received": r.requests_received,
            "upstream_calls_made": r.upstream_calls_made,
            "requests_coalesced": r.requests_coalesced,
            "coalesce_ratio": r.requests_coalesced / total,
            "avg_wait_ms": r.total_wait_ms / total,
            "errors": r.errors,
        }
```

## Comparison

| Approach | Handles Concurrent | Handles Sequential | Distributed | Use Case |
|---|---|---|---|---|
| SingleFlightCoalescer | Yes | No | No | Short-lived in-process burst |
| CoalescingCache | Yes | Yes (TTL) | No | Standard cache + coalesce combo |
| WindowedBatchCoalescer | Yes (batched) | No | No | Batch APIs (embeddings, classification) |
| KeyedSemaphoreCoalescer | Yes (serialized) | Partial | No | Retry-safe sequential dedup |
| RedisCoalescingCache | Yes | Yes (TTL) | Yes (multi-process) | Distributed agent clusters |
| InstrumentedCoalescer | Via inner | Via inner | Via inner | Metrics wrapper for any coalescer |

**Best for production**: Use `CoalescingCache` (coalesce + TTL cache) for most agent tools. Use `WindowedBatchCoalescer` for embedding and classification APIs that accept batch inputs. For multi-process deployments, use `RedisCoalescingCache`. Wrap with `InstrumentedCoalescer` to track coalesce ratio in dashboards.
