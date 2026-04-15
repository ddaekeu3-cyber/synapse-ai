---
layout: solution
title: "Rate Limit Backpressure Not Propagated to Producer — Queue Overflow"
category: rate-limit
description: "Producer sends tasks faster than the API rate limit allows. Worker hits 429s and drops tasks. Producer has no feedback mechanism — keeps sending at full speed, overwhelming the queue."
tags: [backpressure, rate-limit, queue, producer-consumer, overflow, 429]
---

# Rate Limit Backpressure Not Propagated to Producer — Queue Overflow

## Symptom
- Workers getting 429s; producer keeps feeding tasks at the same rate
- Queue depth grows unbounded until memory exhaustion or OOM
- Tasks dropped under load — some never processed
- Retry storms: workers retry 429s → more load → more 429s
- No signal from consumer to producer to slow down

## Root Cause
Producer-consumer decoupling without backpressure. The producer doesn't know the consumer is hitting rate limits and has no mechanism to slow down. Classic backpressure problem: producer must be able to receive signals from downstream to regulate its own rate.

## Fix

### Option 1: Bounded queue as implicit backpressure

```python
import asyncio

async def producer(queue: asyncio.Queue, tasks: list):
    for task in tasks:
        # put() blocks when queue is full — natural backpressure
        await queue.put(task)
        print(f"Queued task. Queue size: {queue.qsize()}")

async def consumer(queue: asyncio.Queue, rate_limiter):
    while True:
        task = await queue.get()
        await rate_limiter.wait()  # Respect rate limit
        try:
            await process_task(task)
        except RateLimitError:
            await queue.put(task)  # Requeue on 429
            await asyncio.sleep(60)  # Back off
        finally:
            queue.task_done()

# Bounded queue: blocks producer when 10 tasks are pending
# This naturally limits producer to consumer's rate
queue = asyncio.Queue(maxsize=10)
```

### Option 2: Token bucket rate limiter shared between producer and consumer

```python
import asyncio, time

class TokenBucket:
    """Rate limiter using token bucket algorithm"""
    def __init__(self, rate: float, capacity: int):
        self.rate = rate        # tokens per second
        self.capacity = capacity
        self.tokens = capacity
        self.last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, tokens: int = 1):
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self.last_refill
            # Refill tokens based on elapsed time
            self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
            self.last_refill = now

            if self.tokens < tokens:
                # Not enough tokens — wait for refill
                wait_time = (tokens - self.tokens) / self.rate
                await asyncio.sleep(wait_time)
                self.tokens = 0
            else:
                self.tokens -= tokens

# Shared between all producers and consumers
# 10 requests per second, burst of 20
rate_limiter = TokenBucket(rate=10, capacity=20)

async def producer_with_backpressure(tasks: list):
    for task in tasks:
        await rate_limiter.acquire()  # Producer waits here
        await submit_task(task)

async def consumer():
    while True:
        task = await queue.get()
        await rate_limiter.acquire()  # Consumer also rate-limited
        await process_task(task)
```

### Option 3: Semaphore-based concurrency limit

```python
import asyncio

MAX_CONCURRENT = 5  # Match to your API tier's concurrent request limit

async def process_all_tasks(tasks: list) -> list:
    semaphore = asyncio.Semaphore(MAX_CONCURRENT)

    async def process_one(task):
        async with semaphore:  # Only N tasks run concurrently
            return await call_api(task)

    return await asyncio.gather(*[process_one(t) for t in tasks])
```

### Option 4: Adaptive rate based on 429 responses

```python
import asyncio, time

class AdaptiveRateLimiter:
    def __init__(self, initial_rps: float = 10.0):
        self.rps = initial_rps
        self.min_interval = 1.0 / initial_rps
        self.last_call = 0
        self._lock = asyncio.Lock()

    async def wait(self):
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self.last_call
            if elapsed < self.min_interval:
                await asyncio.sleep(self.min_interval - elapsed)
            self.last_call = time.monotonic()

    def on_rate_limited(self, retry_after: float = None):
        """Slow down when we hit a 429"""
        self.rps = max(1.0, self.rps * 0.5)  # Halve the rate
        self.min_interval = 1.0 / self.rps
        print(f"Rate limited. Reduced to {self.rps:.1f} RPS")

    def on_success(self):
        """Gradually speed up after successful calls"""
        self.rps = min(50.0, self.rps * 1.05)  # Increase by 5%
        self.min_interval = 1.0 / self.rps

limiter = AdaptiveRateLimiter(initial_rps=5.0)

async def call_with_adaptive_backpressure(task) -> dict:
    await limiter.wait()
    try:
        result = await api_call(task)
        limiter.on_success()
        return result
    except RateLimitError as e:
        retry_after = getattr(e, "retry_after", 60)
        limiter.on_rate_limited(retry_after)
        await asyncio.sleep(retry_after)
        return await call_with_adaptive_backpressure(task)  # Retry
```

### Option 5: Expose backpressure signal to orchestrator

```python
from dataclasses import dataclass
import asyncio

@dataclass
class WorkerStatus:
    queue_depth: int
    error_rate: float
    current_rps: float

class BackpressureSignal:
    def __init__(self):
        self._paused = asyncio.Event()
        self._paused.set()  # Start unpaused

    def pause(self):
        """Signal producer to stop sending"""
        self._paused.clear()
        print("Backpressure: pausing producer")

    def resume(self):
        """Signal producer it can send again"""
        self._paused.set()
        print("Backpressure: resuming producer")

    async def wait_if_paused(self):
        """Producer calls this before each task"""
        await self._paused.wait()

signal = BackpressureSignal()

async def worker_monitoring_loop(queue: asyncio.Queue):
    """Pause producer when queue is full or error rate is high"""
    while True:
        if queue.qsize() > queue.maxsize * 0.8:
            signal.pause()
        elif queue.qsize() < queue.maxsize * 0.3:
            signal.resume()
        await asyncio.sleep(1)

async def producer(tasks):
    for task in tasks:
        await signal.wait_if_paused()  # Respects backpressure
        await queue.put(task)
```

## Backpressure Mechanism Comparison

| Mechanism | Complexity | Works distributed? | Best for |
|---|---|---|---|
| Bounded queue | Low | No | Single process |
| Token bucket | Medium | With Redis | Multi-process |
| Semaphore | Low | No | Concurrency cap |
| Adaptive rate | Medium | No | API rate limits |
| Explicit signal | Medium | With pub/sub | Orchestrated systems |

## Expected Token Savings
Not applicable — prevents task loss and 429 storm escalation.

## Environment
- High-throughput agent pipelines; multi-worker API consumers
- Source: direct experience with production agent deployments at scale
