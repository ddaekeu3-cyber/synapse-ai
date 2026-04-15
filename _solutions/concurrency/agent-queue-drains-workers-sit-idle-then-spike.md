---
layout: solution
title: "Agent Queue Drains — Workers Sit Idle, Then Spike When Refilled"
category: concurrency
description: "Agent workers poll a queue at fixed intervals. Queue drains. All workers sit idle polling an empty queue every 5 seconds. Queue fills again. All workers wake up simultaneously and spike CPU and API rate limits. Boom-bust cycle repeats."
tags: [concurrency, queue, polling, backpressure, idle, spike, rate-limit, adaptive, workers]
---

# Agent Queue Drains — Workers Sit Idle, Then Spike When Refilled

## Symptom
- Workers poll queue every 5 seconds — CPU shows 0% for 4.9s then 100% for 0.1s
- When queue has work, all 10 workers grab items simultaneously — rate limit hit
- Queue drains to zero — 10 workers sitting idle, burning compute for sleep loops
- New batch arrives — 10 workers wake simultaneously — API 429 storm
- Metrics show bimodal throughput: either 0 items/sec or 100 items/sec, never stable
- Cloud costs high because idle workers still run — could use fewer workers when idle

## Root Cause
Fixed-interval polling is inherently oscillating. All workers poll on the same cycle — when work arrives, they all react at once. Empty queue polling wastes compute and causes thundering herd. The fixes are: (1) replace polling with blocking wait or push notification, (2) add jitter to worker wakeups, (3) implement backpressure to pace work according to downstream capacity, and (4) implement adaptive polling that slows down when idle and speeds up when busy.

## Fix

### Option 1: asyncio.Queue — blocking wait, no polling needed

```python
import asyncio
from dataclasses import dataclass
from typing import Any

@dataclass
class WorkItem:
    item_id: str
    payload: dict
    priority: int = 0

async def producer(queue: asyncio.Queue, items: list[WorkItem]):
    """Add items to queue as they arrive"""
    for item in items:
        await queue.put(item)
        print(f"Queued: {item.item_id}")

async def worker(
    worker_id: int,
    queue: asyncio.Queue,
    process_fn,
    shutdown_event: asyncio.Event
):
    """
    Worker that blocks on queue.get() — no polling, no sleep loop.
    Wakes up ONLY when work arrives. Zero idle CPU.
    """
    print(f"Worker {worker_id} started — waiting for work")

    while not shutdown_event.is_set():
        try:
            # Block until an item arrives — no busy polling
            item = await asyncio.wait_for(
                queue.get(),
                timeout=1.0  # Check shutdown_event every second
            )
        except asyncio.TimeoutError:
            continue  # No item — check shutdown flag and wait again

        try:
            print(f"Worker {worker_id}: processing {item.item_id}")
            await process_fn(item)
        except Exception as e:
            print(f"Worker {worker_id}: failed on {item.item_id}: {e}")
        finally:
            queue.task_done()  # Signal item processed

async def run_worker_pool(
    process_fn,
    num_workers: int = 5,
    max_queue_size: int = 100
):
    """
    Worker pool with blocking queue — no thundering herd, no idle polling.
    """
    queue: asyncio.Queue[WorkItem] = asyncio.Queue(maxsize=max_queue_size)
    shutdown = asyncio.Event()

    # Start workers — they block on queue.get()
    workers = [
        asyncio.create_task(worker(i, queue, process_fn, shutdown))
        for i in range(num_workers)
    ]

    return queue, shutdown, workers

# Usage:
queue, shutdown, workers = await run_worker_pool(
    process_fn=process_agent_task,
    num_workers=5
)

# Push work — workers wake up immediately without polling:
await queue.put(WorkItem("task_1", {"data": "..."}))
await queue.put(WorkItem("task_2", {"data": "..."}))

# When done:
await queue.join()  # Wait for all items to be processed
shutdown.set()
await asyncio.gather(*workers)
```

### Option 2: Adaptive polling — slow when idle, fast when busy

```python
import asyncio
import time

class AdaptivePoller:
    """
    Polling interval adapts to queue fullness.
    Empty queue → poll slowly (save CPU)
    Full queue → poll quickly (maximize throughput)
    Adds jitter to prevent synchronized worker wakeups.
    """

    def __init__(
        self,
        min_interval: float = 0.1,    # Min sleep when busy
        max_interval: float = 30.0,   # Max sleep when idle
        backoff_factor: float = 2.0,  # Double sleep on each empty poll
        jitter_fraction: float = 0.3  # Random jitter as fraction of interval
    ):
        self.min_interval = min_interval
        self.max_interval = max_interval
        self.backoff_factor = backoff_factor
        self.jitter = jitter_fraction
        self._current_interval = min_interval
        self._consecutive_empty = 0

    def record_result(self, got_work: bool):
        """Update polling interval based on whether work was found"""
        import random
        if got_work:
            # Reset to fast polling — more work likely available
            self._current_interval = self.min_interval
            self._consecutive_empty = 0
        else:
            # Back off — queue is likely empty
            self._consecutive_empty += 1
            self._current_interval = min(
                self._current_interval * self.backoff_factor,
                self.max_interval
            )

    async def sleep(self):
        """Sleep with jitter — prevents synchronized wakeups"""
        import random
        jitter = self._current_interval * self.jitter * (random.random() * 2 - 1)
        sleep_time = max(self.min_interval, self._current_interval + jitter)
        await asyncio.sleep(sleep_time)

    @property
    def stats(self) -> dict:
        return {
            "current_interval_ms": round(self._current_interval * 1000),
            "consecutive_empty": self._consecutive_empty,
            "state": "idle" if self._current_interval > 5 else "active"
        }

async def adaptive_polling_worker(
    worker_id: int,
    fetch_fn,   # Async function to fetch work items
    process_fn  # Async function to process one item
):
    """
    Worker with adaptive polling — no thundering herd.
    Each worker has its own independent poller with jitter.
    """
    poller = AdaptivePoller(
        min_interval=0.1,
        max_interval=30.0,
        jitter_fraction=0.4  # 40% jitter = workers desynchronize naturally
    )

    while True:
        items = await fetch_fn(limit=10)

        if items:
            poller.record_result(got_work=True)
            for item in items:
                await process_fn(item)
        else:
            poller.record_result(got_work=False)
            print(f"Worker {worker_id}: idle — next poll in {poller.stats['current_interval_ms']}ms")

        await poller.sleep()
```

### Option 3: Redis queue with blocking pop — push replaces polling

```python
import redis.asyncio as aioredis
import asyncio
import json
import os

class RedisWorkQueue:
    """
    Redis-backed work queue with blocking pop.
    Workers block on BRPOP — zero CPU usage when idle.
    Producer pushes to queue — workers wake up immediately.
    No polling, no thundering herd.
    """

    def __init__(self, queue_name: str, redis_url: str = None):
        self.queue_name = queue_name
        self.redis_url = redis_url or os.environ.get("REDIS_URL", "redis://localhost:6379")
        self._redis: aioredis.Redis | None = None

    async def connect(self):
        self._redis = await aioredis.from_url(self.redis_url)

    async def push(self, item: dict, priority: int = 0):
        """
        Push item to queue.
        Workers blocked on BRPOP will wake up immediately.
        """
        payload = json.dumps(item)
        if priority > 0:
            # Higher priority → push to front
            await self._redis.lpush(self.queue_name, payload)
        else:
            await self._redis.rpush(self.queue_name, payload)

    async def pop_blocking(self, timeout: float = 5.0) -> dict | None:
        """
        Block until an item is available — no polling loop needed.
        Returns None on timeout (check for shutdown signal).
        """
        result = await self._redis.brpop(
            self.queue_name,
            timeout=int(timeout)
        )
        if result is None:
            return None  # Timeout — no item available
        _, raw = result
        return json.loads(raw)

    async def queue_length(self) -> int:
        return await self._redis.llen(self.queue_name)

async def redis_worker(
    worker_id: int,
    queue: RedisWorkQueue,
    process_fn,
    shutdown: asyncio.Event,
    semaphore: asyncio.Semaphore
):
    """
    Worker that blocks on Redis BRPOP — no polling, wakes only when work arrives.
    Semaphore limits concurrent workers to prevent API rate limit spikes.
    """
    print(f"Worker {worker_id}: connected to Redis queue, waiting for work...")

    while not shutdown.is_set():
        item = await queue.pop_blocking(timeout=2.0)

        if item is None:
            continue  # Timeout — check shutdown flag

        async with semaphore:  # Limit concurrent processing
            try:
                await process_fn(item)
            except Exception as e:
                print(f"Worker {worker_id}: error processing {item.get('id')}: {e}")
                # Optional: push to dead letter queue
                await queue.push({"_failed": True, "_error": str(e), **item})

async def start_redis_worker_pool(
    queue_name: str,
    num_workers: int = 5,
    max_concurrent: int = 3  # Rate-limit-friendly concurrency
):
    queue = RedisWorkQueue(queue_name)
    await queue.connect()

    shutdown = asyncio.Event()
    semaphore = asyncio.Semaphore(max_concurrent)  # At most 3 concurrent API calls

    workers = [
        asyncio.create_task(
            redis_worker(i, queue, process_item, shutdown, semaphore)
        )
        for i in range(num_workers)
    ]

    return queue, shutdown, workers
```

### Option 4: Backpressure — pace producers to consumer capacity

```python
import asyncio
from dataclasses import dataclass

@dataclass
class BackpressureController:
    """
    Prevents queue overflow by slowing producers when queue is full.
    Prevents queue drain by signaling producers to send more when low.
    """
    queue: asyncio.Queue
    low_watermark: int    # Start accepting more work below this level
    high_watermark: int   # Stop accepting new work above this level

    def should_accept_work(self) -> bool:
        """Producer should check this before submitting work"""
        return self.queue.qsize() < self.high_watermark

    def needs_more_work(self) -> bool:
        """Signal to producer that queue is running low"""
        return self.queue.qsize() < self.low_watermark

    async def submit(self, item, timeout: float = 30.0) -> bool:
        """
        Submit work with backpressure — blocks if queue is full.
        Returns True if submitted, False if timed out.
        """
        try:
            await asyncio.wait_for(self.queue.put(item), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            print(f"Backpressure: queue full ({self.queue.qsize()} items), producer blocked")
            return False

    @property
    def stats(self) -> dict:
        size = self.queue.qsize()
        return {
            "queue_size": size,
            "accepting": self.should_accept_work(),
            "needs_work": self.needs_more_work(),
            "pressure": "high" if size > self.high_watermark * 0.8 else
                        "low" if size < self.low_watermark else "normal"
        }

async def producer_with_backpressure(
    source_fn,
    controller: BackpressureController,
    batch_size: int = 10
):
    """
    Producer that respects backpressure.
    Slows down when queue is full, speeds up when low.
    """
    while True:
        if controller.needs_more_work():
            # Queue is low — fetch a batch
            items = await source_fn(batch_size)
            if not items:
                await asyncio.sleep(5.0)  # No work available — wait
                continue

            for item in items:
                submitted = await controller.submit(item, timeout=10.0)
                if not submitted:
                    # Put item back in source and slow down
                    print("Backpressure: slowing producer")
                    break

        else:
            # Queue has enough work — wait
            await asyncio.sleep(1.0)

        stats = controller.stats
        if stats["pressure"] != "normal":
            print(f"Queue pressure: {stats}")
```

### Option 5: Rate-limited worker with token bucket

```python
import asyncio
import time

class TokenBucketRateLimiter:
    """
    Rate limit worker throughput — prevents API rate limit spikes
    when queue fills and all workers process simultaneously.
    """

    def __init__(self, rate_per_second: float, burst_size: int = None):
        self.rate = rate_per_second
        self.burst = burst_size or int(rate_per_second * 2)
        self._tokens = float(self.burst)
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, tokens: float = 1.0):
        """Acquire tokens — waits if bucket is empty"""
        async with self._lock:
            await self._refill()

            while self._tokens < tokens:
                deficit = tokens - self._tokens
                wait = deficit / self.rate
                await asyncio.sleep(wait)
                await self._refill()

            self._tokens -= tokens

    async def _refill(self):
        now = time.monotonic()
        elapsed = now - self._last_refill
        refill = elapsed * self.rate
        self._tokens = min(self.burst, self._tokens + refill)
        self._last_refill = now

# Shared limiter — all workers share the same rate budget:
api_limiter = TokenBucketRateLimiter(rate_per_second=10.0, burst_size=20)

async def rate_limited_worker(worker_id: int, queue: asyncio.Queue, process_fn):
    """Worker that respects shared rate limit — no spike on queue refill"""
    while True:
        item = await queue.get()
        try:
            # Acquire rate limit token before calling API
            await api_limiter.acquire()
            await process_fn(item)
        finally:
            queue.task_done()

# With 10 workers sharing 10 req/s limiter:
# Queue fills → all 10 workers wake up → rate limiter spaces out API calls
# → steady 10 req/s instead of 100 req/s spike → no 429 errors
```

### Option 6: Queue health monitoring

```python
import asyncio
import time
from dataclasses import dataclass, field
from collections import deque

@dataclass
class QueueHealthMonitor:
    """Monitor queue health — detect boom-bust cycles and idle workers"""
    queue: asyncio.Queue
    _samples: deque = field(default_factory=lambda: deque(maxlen=60))
    _last_sample_time: float = field(default_factory=time.monotonic)

    async def monitor_loop(self, interval: float = 5.0):
        """Sample queue depth every N seconds — detect patterns"""
        while True:
            await asyncio.sleep(interval)
            depth = self.queue.qsize()
            self._samples.append((time.monotonic(), depth))
            self._analyze()

    def _analyze(self):
        if len(self._samples) < 3:
            return

        depths = [s[1] for s in self._samples]
        recent = depths[-10:]

        # Detect boom-bust: alternating 0 and high values
        if recent:
            zeros = sum(1 for d in recent if d == 0)
            highs = sum(1 for d in recent if d > 20)
            if zeros > 3 and highs > 3:
                print(
                    "QUEUE HEALTH: Boom-bust cycle detected — "
                    f"{zeros} empty samples and {highs} full samples in last {len(recent)} checks. "
                    "Consider adaptive polling or push-based queue."
                )

        # Detect persistent idle
        if all(d == 0 for d in recent) and len(recent) >= 5:
            print(
                f"QUEUE HEALTH: Queue has been empty for {len(recent) * 5}+ seconds. "
                "Workers are idle — consider scaling down."
            )

        # Detect persistent overflow
        max_size = self.queue.maxsize or 1000
        if all(d > max_size * 0.8 for d in recent):
            print(
                f"QUEUE HEALTH: Queue consistently at {max_size*0.8:.0f}%+ capacity. "
                "Consider adding workers or increasing queue size."
            )

    @property
    def current_stats(self) -> dict:
        if not self._samples:
            return {}
        depths = [s[1] for s in list(self._samples)[-10:]]
        return {
            "current_depth": self.queue.qsize(),
            "avg_depth_10s": round(sum(depths) / len(depths), 1),
            "max_depth_10s": max(depths),
            "min_depth_10s": min(depths),
            "empty_rate": f"{sum(1 for d in depths if d==0)/len(depths)*100:.0f}%"
        }
```

## Queue Strategy Comparison

| Strategy | Idle CPU | Thundering Herd | Complexity | Best For |
|---|---|---|---|---|
| Fixed-interval polling | High | High | Low | Never use |
| Adaptive polling + jitter | Low | Low | Medium | Simple polling scenarios |
| asyncio.Queue (blocking) | Zero | Zero | Low | Single-process agents |
| Redis BRPOP | Zero | Zero | Medium | Multi-process, distributed |
| Message broker (SQS, RabbitMQ) | Zero | Zero | High | Production scale |
| Rate-limited workers | N/A | Eliminated | Medium | API-rate-limited processing |

## Expected Token Savings
Thundering herd → 429 storm → 5 retries × 10 workers: ~50,000 extra tokens in one spike
Rate-limited steady processing: 0 retries, 0 wasted tokens

## Environment
- Any agent running a worker pool that processes from a queue; critical for batch processing agents, email senders, webhook processors, and any agent consuming from a task queue
- Source: direct experience; polling boom-bust cycles are the most common cause of unpredictable latency and rate limit violations in production agent deployments
