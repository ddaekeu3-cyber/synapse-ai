---
layout: solution
title: "Concurrent Agent Workers Collectively Exceed API Rate Limit"
category: rate-limit
description: "Each individual worker respects the rate limit, but 10 workers running simultaneously together exceed it. All workers hit 429 at the same time and retry simultaneously, making it worse."
tags: [rate-limit, concurrency, 429, workers, semaphore, token-bucket, coordination]
---

# Concurrent Agent Workers Collectively Exceed API Rate Limit

## Symptom
- Single worker: zero 429 errors. Ten workers: constant 429 storms
- All workers retry at the same time after 429, creating synchronized spikes
- Rate limit errors increase super-linearly with number of workers
- Retry-after header is 60 seconds but all 10 workers wake up simultaneously
- Adding jitter to one worker doesn't help when 9 others spike at the same time

## Root Cause
Per-worker rate limiting doesn't account for aggregate throughput. Each worker limits itself to N requests/second, but N workers × N requests/second = N² aggregate rate. Additionally, synchronized retry logic causes thundering herd: all workers wait the same duration, then all hammer the API simultaneously.

## Fix

### Option 1: Shared token bucket across all workers

```python
import asyncio
import time

class SharedTokenBucket:
    """
    Single rate limiter shared by all concurrent workers.
    Enforces aggregate throughput limit regardless of worker count.
    """

    def __init__(self, rate: float, capacity: float = None):
        """
        rate: tokens per second (= max requests per second across all workers)
        capacity: burst capacity (defaults to rate * 2)
        """
        self.rate = rate
        self.capacity = capacity or rate * 2
        self.tokens = self.capacity
        self.last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, tokens: float = 1.0):
        """Wait until token is available. Blocks until rate allows."""
        async with self._lock:
            while True:
                now = time.monotonic()
                elapsed = now - self.last_refill
                self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
                self.last_refill = now

                if self.tokens >= tokens:
                    self.tokens -= tokens
                    return

                # Calculate wait time for next token
                deficit = tokens - self.tokens
                wait = deficit / self.rate
                await asyncio.sleep(wait)

# One shared bucket for all workers:
# 50 requests/minute = 50/60 ≈ 0.833 per second
api_bucket = SharedTokenBucket(rate=50 / 60, capacity=5)

async def worker(worker_id: int, tasks: list):
    for task in tasks:
        await api_bucket.acquire()  # All workers draw from same bucket
        result = await call_api(task)
        process(result)

# Launch 10 workers — they collectively stay within 50 req/min
await asyncio.gather(*[worker(i, tasks[i]) for i in range(10)])
```

### Option 2: Centralized request queue with rate-controlled dispatcher

```python
import asyncio
from collections import deque

class RateLimitedDispatcher:
    """
    Single dispatcher serializes all API calls with rate control.
    Workers submit tasks; dispatcher sends at controlled rate.
    """

    def __init__(self, requests_per_second: float):
        self.interval = 1.0 / requests_per_second
        self.queue: asyncio.Queue = asyncio.Queue()
        self._running = False

    async def submit(self, coro) -> any:
        """Submit a coroutine and wait for its result"""
        future = asyncio.get_event_loop().create_future()
        await self.queue.put((coro, future))
        return await future

    async def run(self):
        """Dispatch one request per interval"""
        self._running = True
        last_sent = 0.0

        while self._running:
            coro, future = await self.queue.get()
            now = asyncio.get_event_loop().time()
            wait = self.interval - (now - last_sent)
            if wait > 0:
                await asyncio.sleep(wait)

            try:
                result = await coro
                future.set_result(result)
            except Exception as e:
                future.set_exception(e)
            finally:
                last_sent = asyncio.get_event_loop().time()
                self.queue.task_done()

    def stop(self):
        self._running = False

# Usage:
dispatcher = RateLimitedDispatcher(requests_per_second=0.8)  # 48 req/min

async def main():
    dispatch_task = asyncio.create_task(dispatcher.run())

    # Workers submit through dispatcher — rate is enforced globally
    async def worker(item):
        return await dispatcher.submit(call_api(item))

    results = await asyncio.gather(*[worker(item) for item in items])
    dispatcher.stop()
    await dispatch_task
    return results
```

### Option 3: Jittered retry with per-worker random offset

```python
import asyncio
import random

async def call_with_jittered_retry(
    fn,
    max_retries: int = 5,
    base_delay: float = 1.0,
    jitter_range: float = 2.0
) -> any:
    """
    Retry with exponential backoff + large random jitter.
    Prevents synchronized retry storms across workers.
    """
    for attempt in range(max_retries):
        try:
            return await fn()
        except RateLimitError as e:
            if attempt == max_retries - 1:
                raise

            retry_after = getattr(e, "retry_after", None) or base_delay * (2 ** attempt)

            # Add large random jitter — workers spread out over jitter_range seconds
            # With 10 workers and 5s jitter, they retry spread over 5s instead of simultaneously
            jitter = random.uniform(0, jitter_range)
            total_wait = retry_after + jitter

            print(f"Worker {asyncio.current_task().get_name()}: "
                  f"Rate limited. Waiting {total_wait:.1f}s (retry_after={retry_after:.0f}s, jitter={jitter:.1f}s)")
            await asyncio.sleep(total_wait)

# Each worker calls this — jitter ensures they don't all wake up simultaneously
result = await call_with_jittered_retry(lambda: api_client.complete(prompt))
```

### Option 4: Redis-based distributed rate limiter (multi-process/multi-host)

```python
import redis
import time

class RedisRateLimiter:
    """
    Distributed rate limiter using Redis sliding window.
    Works across multiple processes, containers, or hosts.
    """

    def __init__(self, redis_client: redis.Redis, key: str, limit: int, window_seconds: int):
        self.redis = redis_client
        self.key = key
        self.limit = limit
        self.window = window_seconds

    def acquire(self, timeout: float = 60.0) -> bool:
        """
        Try to acquire a rate limit slot.
        Blocks until slot available or timeout exceeded.
        """
        deadline = time.time() + timeout

        while time.time() < deadline:
            now = time.time()
            window_start = now - self.window

            pipe = self.redis.pipeline()
            # Sliding window: count requests in last window_seconds
            pipe.zremrangebyscore(self.key, 0, window_start)
            pipe.zcard(self.key)
            pipe.zadd(self.key, {str(now): now})
            pipe.expire(self.key, self.window * 2)
            _, count, _, _ = pipe.execute()

            if count < self.limit:
                return True  # Slot acquired

            # Wait a fraction of the window before retrying
            jitter = random.uniform(0.1, 0.5)
            time.sleep(jitter)

        return False  # Timed out

limiter = RedisRateLimiter(
    redis_client=redis.Redis(),
    key="api:rate:anthropic",
    limit=50,           # 50 requests
    window_seconds=60   # per 60 seconds
)

# In each worker (any process, any host):
if limiter.acquire():
    result = call_api(task)
else:
    raise RuntimeError("Could not acquire rate limit slot — API at capacity")
```

### Option 5: Adaptive rate limiting based on 429 feedback

```python
import asyncio
import time

class AdaptiveRateLimiter:
    """
    Dynamically adjust request rate based on 429 response feedback.
    Backs off when rate limited, gradually recovers when successful.
    """

    def __init__(self, initial_rps: float = 1.0, min_rps: float = 0.1, max_rps: float = 5.0):
        self.current_rps = initial_rps
        self.min_rps = min_rps
        self.max_rps = max_rps
        self._lock = asyncio.Lock()
        self._last_success = time.monotonic()

    async def on_success(self):
        """Gradually increase rate after sustained success"""
        async with self._lock:
            # Slow recovery: increase by 10% per successful call
            if time.monotonic() - self._last_success > 5.0:
                self.current_rps = min(self.max_rps, self.current_rps * 1.1)
                print(f"Rate increased to {self.current_rps:.2f} rps")
            self._last_success = time.monotonic()

    async def on_rate_limited(self, retry_after: float = None):
        """Immediately halve the rate on 429"""
        async with self._lock:
            self.current_rps = max(self.min_rps, self.current_rps * 0.5)
            print(f"Rate limited! Reduced to {self.current_rps:.2f} rps")
            wait = retry_after or (1.0 / self.current_rps)
            await asyncio.sleep(wait)

    async def acquire(self):
        await asyncio.sleep(1.0 / self.current_rps)

limiter = AdaptiveRateLimiter(initial_rps=0.8)

async def resilient_api_call(payload: dict) -> dict:
    while True:
        await limiter.acquire()
        try:
            result = await call_api(payload)
            await limiter.on_success()
            return result
        except RateLimitError as e:
            await limiter.on_rate_limited(getattr(e, "retry_after", None))
```

## Rate Limit Strategies Comparison

| Strategy | Scope | Overhead | Best for |
|---|---|---|---|
| Per-worker token bucket | Single worker | Minimal | 1-2 workers, same process |
| Shared token bucket | All workers, one process | Low | Multi-worker single process |
| Central dispatcher queue | All workers, one process | Medium | Bursty workloads |
| Redis sliding window | Cross-process / cross-host | Medium | Distributed agents |
| Adaptive rate limiter | Any scope | Low | Unknown rate limits |

## Expected Token Savings
429 storm from 10 workers + exponential retry: ~20,000 wasted tokens
Shared bucket eliminates 429s entirely: 0 wasted

## Environment
- Multi-worker agent pipelines; especially batch processing with asyncio.gather or thread pools
- Source: direct experience; concurrent workers sharing a rate-limited API is a near-universal scaling problem
