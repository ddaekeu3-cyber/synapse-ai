---
layout: solution
title: "Agent doesn't implement backpressure — causes memory exhaustion"
category: general
description: "Agent enqueues tasks faster than workers can process them, causing an unbounded queue that consumes all available memory and eventually crashes the process with OOM."
tags: [backpressure, asyncio, queue, memory, rate-limiting, flow-control]
---

## Symptom

Under load the agent process RAM climbs steadily until the OS kills it with `SIGKILL` (OOM). Logs show queue depth growing without bound while worker throughput stays flat. Requests near the crash point receive no response at all because the process dies mid-flight.

## Root Cause

The producer loop submits tasks as fast as it receives them (`queue.put_nowait` or unbounded `asyncio.Queue()`). Workers are CPU/IO-bound or rate-limited by the upstream LLM API, so they cannot keep up. No upper bound on queue size means heap memory grows until the process is killed. The crash is non-deterministic and hard to reproduce in low-traffic environments.

---

## Option 1 — Bounded `asyncio.Queue` with producer backpressure

**Set `maxsize` on the queue. `await queue.put()` blocks the producer when full — simple, zero extra dependencies.**

```python
import asyncio
import anthropic

client = anthropic.AsyncAnthropic()

MAX_QUEUE = 50   # hard cap; tune to available memory


async def worker(queue: asyncio.Queue, worker_id: int) -> None:
    while True:
        prompt: str = await queue.get()
        try:
            response = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=256,
                messages=[{"role": "user", "content": prompt}],
            )
            print(f"[W{worker_id}] {response.content[0].text[:60]}")
        except Exception as exc:
            print(f"[W{worker_id}] error: {exc}")
        finally:
            queue.task_done()


async def producer(queue: asyncio.Queue, prompts: list[str]) -> None:
    for prompt in prompts:
        # Blocks here if queue is full — backpressure applied
        await queue.put(prompt)
        print(f"Enqueued (depth={queue.qsize()}): {prompt[:40]}")


async def main() -> None:
    queue: asyncio.Queue = asyncio.Queue(maxsize=MAX_QUEUE)

    workers = [asyncio.create_task(worker(queue, i)) for i in range(5)]

    prompts = [f"Explain topic {i}" for i in range(200)]
    await producer(queue, prompts)

    await queue.join()          # wait until all tasks are done
    for w in workers:
        w.cancel()


asyncio.run(main())
```

**Expected Token Savings:** No direct token reduction, but prevents OOM crashes that force all in-flight requests to be retried — avoids paying twice for up to `MAX_QUEUE` requests per crash.

**Environment:** Any asyncio agent with a producer/consumer split; Python 3.10+.

---

## Option 2 — `asyncio.Semaphore` to cap concurrent LLM calls

**Instead of a queue, use a semaphore to limit the number of simultaneous `messages.create` calls — natural backpressure without explicit queuing.**

```python
import asyncio
import anthropic

client = anthropic.AsyncAnthropic()

MAX_CONCURRENT = 10   # max simultaneous LLM calls


async def call_llm(sem: asyncio.Semaphore, prompt: str, idx: int) -> str:
    async with sem:   # blocks if MAX_CONCURRENT tasks are already running
        response = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )
        result = response.content[0].text
        print(f"[{idx}] done: {result[:50]}")
        return result


async def main() -> None:
    sem = asyncio.Semaphore(MAX_CONCURRENT)
    prompts = [f"Summarise article {i}" for i in range(100)]

    # All tasks are created up front, but only MAX_CONCURRENT run at once
    tasks = [
        asyncio.create_task(call_llm(sem, p, i))
        for i, p in enumerate(prompts)
    ]
    results = await asyncio.gather(*tasks)
    print(f"Completed {len(results)} requests.")


asyncio.run(main())
```

**Expected Token Savings:** Prevents request storms that trigger 429 rate-limit errors and exponential back-off — keeps throughput at the sustainable maximum and eliminates wasted retry tokens.

**Environment:** Batch processing pipelines; pairs naturally with the Anthropic SDK's built-in retry logic.

---

## Option 3 — Token-bucket producer with rate-aware queue drain

**Combine a bounded queue with a token-bucket producer so ingestion rate stays below LLM API rate limits.**

```python
import asyncio
import time
import anthropic

client = anthropic.AsyncAnthropic()


class TokenBucket:
    """Allows at most `rate` tokens per second with burst up to `capacity`."""

    def __init__(self, rate: float, capacity: float) -> None:
        self.rate = rate
        self.capacity = capacity
        self._tokens = capacity
        self._last = time.monotonic()

    async def acquire(self) -> None:
        while True:
            now = time.monotonic()
            self._tokens = min(
                self.capacity,
                self._tokens + (now - self._last) * self.rate,
            )
            self._last = now
            if self._tokens >= 1:
                self._tokens -= 1
                return
            wait = (1 - self._tokens) / self.rate
            await asyncio.sleep(wait)


async def worker(queue: asyncio.Queue, wid: int) -> None:
    while True:
        prompt = await queue.get()
        try:
            r = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=256,
                messages=[{"role": "user", "content": prompt}],
            )
            print(f"[W{wid}] {r.content[0].text[:50]}")
        finally:
            queue.task_done()


async def main() -> None:
    bucket = TokenBucket(rate=5.0, capacity=10.0)   # 5 req/s, burst 10
    queue: asyncio.Queue = asyncio.Queue(maxsize=30)

    workers = [asyncio.create_task(worker(queue, i)) for i in range(5)]

    prompts = [f"Task {i}" for i in range(80)]
    for p in prompts:
        await bucket.acquire()   # rate-limited admission
        await queue.put(p)       # blocked if queue full (backpressure)

    await queue.join()
    for w in workers:
        w.cancel()


asyncio.run(main())
```

**Expected Token Savings:** Sustained rate limiting prevents 429 errors that trigger costly retries — typical saving of 10–25% of total tokens on high-volume pipelines.

**Environment:** Pipelines that must stay within Anthropic's API rate limits while maximising throughput.

---

## Option 4 — Memory-aware admission control

**Check current process RSS before enqueuing. If memory exceeds a threshold, pause the producer until workers drain the queue.**

```python
import asyncio
import os
import anthropic

try:
    import psutil
    _proc = psutil.Process(os.getpid())
    def rss_mb() -> float:
        return _proc.memory_info().rss / 1_048_576
except ImportError:
    def rss_mb() -> float:
        # Fallback: read /proc/self/status on Linux
        try:
            with open("/proc/self/status") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        return int(line.split()[1]) / 1024
        except FileNotFoundError:
            pass
        return 0.0

client = anthropic.AsyncAnthropic()
MAX_RSS_MB = 512   # pause producer above this threshold


async def worker(queue: asyncio.Queue) -> None:
    while True:
        prompt = await queue.get()
        try:
            r = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=256,
                messages=[{"role": "user", "content": prompt}],
            )
            print(f"  result: {r.content[0].text[:50]}")
        finally:
            queue.task_done()


async def producer(queue: asyncio.Queue, prompts: list[str]) -> None:
    for prompt in prompts:
        # Memory-based backpressure: pause if RSS too high
        while rss_mb() > MAX_RSS_MB:
            print(f"RSS {rss_mb():.0f} MB > {MAX_RSS_MB} MB — pausing …")
            await asyncio.sleep(1)

        await queue.put(prompt)


async def main() -> None:
    queue: asyncio.Queue = asyncio.Queue(maxsize=100)
    workers = [asyncio.create_task(worker(queue)) for _ in range(4)]
    prompts = [f"Heavy context task {i}" * 10 for i in range(500)]
    await producer(queue, prompts)
    await queue.join()
    for w in workers:
        w.cancel()


asyncio.run(main())
```

**Expected Token Savings:** Prevents OOM crashes that drop all buffered requests — avoids needing to re-submit every queued task after a crash.

**Environment:** Agents handling large context windows where each task consumes significant heap; Linux/macOS with `psutil` installed.

---

## Option 5 — Redis stream as durable backpressure queue

**Use a Redis stream with `MAXLEN` to bound queue size in a durable, multi-process-safe way. Consumers use `XREADGROUP` for at-least-once delivery.**

```python
import asyncio
import json
import anthropic
import redis.asyncio as aioredis

client = anthropic.AsyncAnthropic()
STREAM = "agent:tasks"
GROUP = "workers"
MAX_STREAM_LEN = 200   # Redis trims automatically


async def enqueue(r: aioredis.Redis, prompt: str) -> None:
    await r.xadd(
        STREAM,
        {"prompt": prompt},
        maxlen=MAX_STREAM_LEN,   # backpressure: oldest entries dropped
        approximate=True,
    )


async def worker(r: aioredis.Redis, consumer_id: str) -> None:
    while True:
        entries = await r.xreadgroup(
            GROUP, consumer_id, {STREAM: ">"}, count=1, block=1000
        )
        if not entries:
            continue
        _, messages = entries[0]
        for msg_id, fields in messages:
            prompt = fields[b"prompt"].decode()
            try:
                resp = await client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=256,
                    messages=[{"role": "user", "content": prompt}],
                )
                print(f"[{consumer_id}] {resp.content[0].text[:50]}")
                await r.xack(STREAM, GROUP, msg_id)
            except Exception as exc:
                print(f"[{consumer_id}] error: {exc}")


async def main() -> None:
    r = await aioredis.from_url("redis://localhost:6379")

    # Create consumer group (idempotent)
    try:
        await r.xgroup_create(STREAM, GROUP, id="0", mkstream=True)
    except Exception:
        pass

    # Start workers
    workers = [
        asyncio.create_task(worker(r, f"w-{i}")) for i in range(3)
    ]

    # Enqueue — Redis silently trims if stream exceeds MAX_STREAM_LEN
    for i in range(500):
        await enqueue(r, f"Summarise document {i}")

    await asyncio.gather(*workers)


asyncio.run(main())
```

**Expected Token Savings:** Redis stream durability means tasks survive agent restarts — eliminates re-submission cost entirely for already-enqueued work.

**Environment:** Multi-process or multi-host agent deployments; requires Redis 5+, `redis-py>=4.2`.

---

## Option 6 — Structured backpressure with `aiormq` / RabbitMQ prefetch

**Use RabbitMQ `basic_qos(prefetch_count=N)` so each worker fetches only what it can handle — server-side flow control with no extra code in the agent.**

```python
import asyncio
import json
import anthropic
import aio_pika

client = anthropic.AsyncAnthropic()
QUEUE_NAME = "agent.tasks"
MAX_PREFETCH = 5   # each worker holds at most 5 unacked messages


async def process_message(message: aio_pika.IncomingMessage) -> None:
    async with message.process():   # auto-ack on success, nack on exception
        payload = json.loads(message.body)
        prompt = payload["prompt"]

        response = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )
        print(f"Done: {response.content[0].text[:60]}")


async def run_worker(worker_id: int) -> None:
    connection = await aio_pika.connect_robust("amqp://guest:guest@localhost/")
    async with connection:
        channel = await connection.channel()
        await channel.set_qos(prefetch_count=MAX_PREFETCH)

        queue = await channel.declare_queue(QUEUE_NAME, durable=True)
        async with queue.iterator() as q:
            print(f"[W{worker_id}] listening …")
            async for message in q:
                await process_message(message)


async def publish_tasks(prompts: list[str]) -> None:
    connection = await aio_pika.connect_robust("amqp://guest:guest@localhost/")
    async with connection:
        channel = await connection.channel()
        await channel.declare_queue(QUEUE_NAME, durable=True)
        for prompt in prompts:
            await channel.default_exchange.publish(
                aio_pika.Message(
                    body=json.dumps({"prompt": prompt}).encode(),
                    delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                ),
                routing_key=QUEUE_NAME,
            )


async def main() -> None:
    prompts = [f"Analyse report {i}" for i in range(200)]
    await publish_tasks(prompts)

    workers = [asyncio.create_task(run_worker(i)) for i in range(4)]
    await asyncio.gather(*workers)


asyncio.run(main())
```

**Expected Token Savings:** Server-side flow control prevents any worker from holding unprocessable messages — reduces duplicate processing from worker crashes by up to 100% for messages within the prefetch window.

**Environment:** Production agents with RabbitMQ; `aio_pika>=9.0`; pairs with Kubernetes horizontal pod autoscaling.

---

## Comparison

| Option | Backpressure Mechanism | Durability | Multi-process | Complexity |
|--------|----------------------|-----------|--------------|------------|
| 1. Bounded `asyncio.Queue` | Queue `maxsize` blocks producer | None | No | Very Low |
| 2. `asyncio.Semaphore` | Semaphore blocks callers | None | No | Low |
| 3. Token bucket + queue | Rate-limited admission | None | No | Medium |
| 4. Memory-aware admission | RSS threshold pause | None | No | Medium |
| 5. Redis stream `MAXLEN` | Server-side trim | Durable | Yes | Medium |
| 6. RabbitMQ `prefetch_count` | Broker-side flow control | Durable | Yes | High |

**Recommended path:** Start with Option 1 (bounded `asyncio.Queue`) for single-process agents — zero dependencies, immediate protection. Add Option 2 (semaphore) when you want to cap LLM concurrency independently of queue depth. Use Option 5 (Redis) or Option 6 (RabbitMQ) for multi-worker deployments where durability across restarts matters.
