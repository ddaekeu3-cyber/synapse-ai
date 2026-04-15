---
layout: solution
title: "Agent Queues Requests Indefinitely Without Timeout"
category: rate-limit
description: "When rate-limited, the agent queues requests and waits indefinitely — holding connections open, consuming memory, and blocking other users while the queue grows without bound."
tags: [rate-limit, timeout, queue, backpressure, async, reliability]
---

## Symptom

Under load, the agent's request queue grows without bound. After 5 minutes, users' requests are still pending. Memory climbs, connections time out at the load balancer, and the agent process eventually OOMs:

```
Queue depth: 1  (t=0s)
Queue depth: 47 (t=30s)
Queue depth: 312 (t=60s)
MemoryError: unable to allocate ...
```

Or worse — requests sit silently pending with no feedback to the caller.

## Root Cause

Retry logic implements unlimited retries with no maximum wait time. A naive `while True: retry_after_sleep()` loop holds the coroutine open indefinitely. Without a deadline, rate-limited requests accumulate in memory until they time out at the infrastructure layer — usually with a worse error than a clean "service busy" response.

## Fix

---

### Option 1 — Absolute Deadline per Request

Set a hard wall-clock deadline for every request. If the request hasn't succeeded by the deadline, raise `TimeoutError` — no exceptions. This bounds memory and latency.

```python
import time
import asyncio
import anthropic
from anthropic import RateLimitError, APIStatusError

client = anthropic.AsyncAnthropic()

async def call_with_deadline(
    messages: list[dict],
    deadline_seconds: float = 30.0,
    max_tokens: int = 512,
) -> str:
    """
    Retry on rate limit until the absolute deadline is reached.
    Raises TimeoutError if the deadline expires.
    """
    deadline = time.monotonic() + deadline_seconds
    attempt = 0
    base_delay = 1.0

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(
                f"Request timed out after {deadline_seconds}s "
                f"({attempt} attempts made)"
            )

        try:
            response = await asyncio.wait_for(
                client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=max_tokens,
                    messages=messages,
                ),
                timeout=min(remaining, 15.0),  # Per-call timeout
            )
            return response.content[0].text

        except RateLimitError as e:
            retry_after = float(e.response.headers.get("retry-after", base_delay))
            wait = min(retry_after, remaining - 0.5)  # Don't wait past deadline
            if wait <= 0:
                raise TimeoutError(f"Rate limited and deadline expired after {attempt} attempts")

            attempt += 1
            print(f"Rate limited. Waiting {wait:.1f}s (attempt {attempt}, {remaining:.1f}s left)")
            await asyncio.sleep(wait)
            base_delay = min(base_delay * 2, 30.0)

        except asyncio.TimeoutError:
            raise TimeoutError(f"Individual call timed out at {deadline_seconds}s deadline")

async def main():
    try:
        result = await call_with_deadline(
            messages=[{"role": "user", "content": "Hello!"}],
            deadline_seconds=20.0,
        )
        print(f"Success: {result[:100]}")
    except TimeoutError as e:
        print(f"Request abandoned: {e}")
        # Return a graceful fallback to the user
        print("Fallback: Service is busy. Please try again in a moment.")

asyncio.run(main())
```

**Expected Token Savings:** None — reliability fix; prevents runaway memory from queued requests
**Environment:** `pip install anthropic`

---

### Option 2 — Bounded Queue with Backpressure

Implement a bounded asyncio queue. When the queue is full, new requests are immediately rejected with a `503 Service Busy` response rather than blocking. The queue size is the only memory you'll ever consume.

```python
import asyncio
import time
import anthropic
from dataclasses import dataclass
from typing import Any

@dataclass
class QueuedRequest:
    messages: list[dict]
    future: asyncio.Future
    enqueued_at: float
    timeout_seconds: float

class BoundedRequestQueue:
    def __init__(
        self,
        max_queue_size: int = 20,
        worker_count: int = 3,
        request_timeout: float = 30.0,
    ):
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=max_queue_size)
        self._worker_count = worker_count
        self._request_timeout = request_timeout
        self._client = anthropic.AsyncAnthropic()
        self._workers: list[asyncio.Task] = []

    async def start(self):
        self._workers = [
            asyncio.create_task(self._worker(i))
            for i in range(self._worker_count)
        ]

    async def stop(self):
        for worker in self._workers:
            worker.cancel()

    async def submit(self, messages: list[dict]) -> str:
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        request = QueuedRequest(
            messages=messages,
            future=future,
            enqueued_at=time.monotonic(),
            timeout_seconds=self._request_timeout,
        )

        try:
            self._queue.put_nowait(request)
        except asyncio.QueueFull:
            raise RuntimeError(
                f"Service busy: queue is full ({self._queue.maxsize} pending requests). "
                "Please try again later."
            )

        return await asyncio.wait_for(future, timeout=self._request_timeout)

    async def _worker(self, worker_id: int):
        while True:
            request = await self._queue.get()

            # Check if the request has already expired
            elapsed = time.monotonic() - request.enqueued_at
            if elapsed > request.timeout_seconds:
                request.future.set_exception(
                    TimeoutError(f"Request expired in queue after {elapsed:.1f}s")
                )
                self._queue.task_done()
                continue

            try:
                response = await self._client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=512,
                    messages=request.messages,
                )
                result = response.content[0].text
                if not request.future.done():
                    request.future.set_result(result)

            except Exception as e:
                if not request.future.done():
                    request.future.set_exception(e)

            finally:
                self._queue.task_done()

async def main():
    queue = BoundedRequestQueue(max_queue_size=5, worker_count=2, request_timeout=15.0)
    await queue.start()

    # Submit multiple requests
    tasks = []
    for i in range(8):  # More than queue size
        try:
            task = asyncio.create_task(
                queue.submit([{"role": "user", "content": f"Request {i}: What is {i}+{i}?"}])
            )
            tasks.append((i, task))
        except RuntimeError as e:
            print(f"Request {i} rejected: {e}")

    for i, task in tasks:
        try:
            result = await task
            print(f"Request {i} result: {result[:60]}")
        except (TimeoutError, Exception) as e:
            print(f"Request {i} failed: {e}")

    await queue.stop()

asyncio.run(main())
```

**Expected Token Savings:** None — prevents OOM from unbounded queue growth
**Environment:** `pip install anthropic`

---

### Option 3 — Token Bucket with Shed Load

Use a token bucket to control the rate at which requests are sent. If the bucket is empty and the request can't be served within its patience window, shed the load immediately.

```python
import asyncio
import time
import anthropic

class TokenBucket:
    """
    Replenishes `rate` tokens per second up to `capacity`.
    consume() returns True if the token was available, False if empty.
    """
    def __init__(self, capacity: float, rate: float):
        self._capacity = capacity
        self._rate = rate
        self._tokens = capacity
        self._last_refill = time.monotonic()

    def _refill(self):
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
        self._last_refill = now

    def consume(self, tokens: float = 1.0) -> bool:
        self._refill()
        if self._tokens >= tokens:
            self._tokens -= tokens
            return True
        return False

    @property
    def wait_time(self) -> float:
        self._refill()
        deficit = 1.0 - self._tokens
        return max(0.0, deficit / self._rate)

# 5 requests per second, burst up to 10
bucket = TokenBucket(capacity=10, rate=5)
client = anthropic.AsyncAnthropic()

async def call_with_token_bucket(
    messages: list[dict],
    patience_seconds: float = 5.0,
) -> str:
    deadline = time.monotonic() + patience_seconds

    while True:
        if bucket.consume():
            response = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=256,
                messages=messages,
            )
            return response.content[0].text

        wait = bucket.wait_time
        remaining = deadline - time.monotonic()

        if wait > remaining:
            raise RuntimeError(
                f"Load shed: bucket empty, {wait:.1f}s to refill, only {remaining:.1f}s patience left"
            )

        print(f"Bucket empty — waiting {wait:.2f}s")
        await asyncio.sleep(wait)

async def main():
    results = await asyncio.gather(
        *[
            call_with_token_bucket(
                [{"role": "user", "content": f"What is {i}?"}],
                patience_seconds=3.0,
            )
            for i in range(15)
        ],
        return_exceptions=True,
    )

    success = sum(1 for r in results if isinstance(r, str))
    shed = sum(1 for r in results if isinstance(r, RuntimeError))
    print(f"Success: {success}, Shed: {shed}")

asyncio.run(main())
```

**Expected Token Savings:** None — rate control; prevents exceeding API quota
**Environment:** `pip install anthropic`

---

### Option 4 — Request Priority Queue with Expiry

Use a priority queue where high-priority requests (e.g. interactive user requests) preempt low-priority batch jobs. Expired requests are purged without waiting.

```python
import asyncio
import heapq
import time
import anthropic
from dataclasses import dataclass, field
from enum import IntEnum

class Priority(IntEnum):
    HIGH = 0    # Interactive user requests
    NORMAL = 1  # Standard API calls
    LOW = 2     # Background batch jobs

@dataclass(order=True)
class PrioritisedRequest:
    priority: int
    enqueued_at: float = field(compare=False)
    timeout_seconds: float = field(compare=False)
    messages: list[dict] = field(compare=False)
    future: asyncio.Future = field(compare=False)

class PriorityRequestQueue:
    def __init__(self, worker_count: int = 2):
        self._heap: list[PrioritisedRequest] = []
        self._lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(0)
        self._client = anthropic.AsyncAnthropic()
        self._workers = [
            asyncio.create_task(self._worker()) for _ in range(worker_count)
        ]

    async def submit(
        self,
        messages: list[dict],
        priority: Priority = Priority.NORMAL,
        timeout: float = 30.0,
    ) -> str:
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        request = PrioritisedRequest(
            priority=priority,
            enqueued_at=time.monotonic(),
            timeout_seconds=timeout,
            messages=messages,
            future=future,
        )
        async with self._lock:
            heapq.heappush(self._heap, request)
        self._semaphore.release()
        return await asyncio.wait_for(future, timeout=timeout)

    async def _worker(self):
        while True:
            await self._semaphore.acquire()
            async with self._lock:
                if not self._heap:
                    continue
                request = heapq.heappop(self._heap)

            elapsed = time.monotonic() - request.enqueued_at
            if elapsed > request.timeout_seconds:
                if not request.future.done():
                    request.future.set_exception(
                        TimeoutError(f"Request expired after {elapsed:.1f}s in queue")
                    )
                continue

            try:
                response = await self._client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=256,
                    messages=request.messages,
                )
                if not request.future.done():
                    request.future.set_result(response.content[0].text)
            except Exception as e:
                if not request.future.done():
                    request.future.set_exception(e)

async def main():
    queue = PriorityRequestQueue(worker_count=2)

    # Submit mixed-priority requests
    tasks = [
        queue.submit([{"role": "user", "content": "HIGH: urgent query"}], Priority.HIGH, timeout=10.0),
        queue.submit([{"role": "user", "content": "LOW: batch job 1"}], Priority.LOW, timeout=60.0),
        queue.submit([{"role": "user", "content": "LOW: batch job 2"}], Priority.LOW, timeout=60.0),
        queue.submit([{"role": "user", "content": "HIGH: another urgent query"}], Priority.HIGH, timeout=10.0),
        queue.submit([{"role": "user", "content": "NORMAL: regular request"}], Priority.NORMAL, timeout=20.0),
    ]

    results = await asyncio.gather(*tasks, return_exceptions=True)
    for i, r in enumerate(results):
        status = "OK" if isinstance(r, str) else f"ERR: {r}"
        print(f"Request {i}: {status[:80]}")

asyncio.run(main())
```

**Expected Token Savings:** None — fairness and reliability; high-priority work isn't starved by batch jobs
**Environment:** `pip install anthropic`

---

### Option 5 — Circuit Breaker Pattern

Track consecutive failures. After N failures, open the circuit and return immediate errors for a cooldown period before retrying. Prevents flooding a struggling API.

```python
import time
import asyncio
import anthropic
from enum import Enum

class CircuitState(Enum):
    CLOSED = "closed"       # Normal operation
    OPEN = "open"           # Failing — reject immediately
    HALF_OPEN = "half_open" # Testing recovery

class CircuitBreaker:
    def __init__(
        self,
        failure_threshold: int = 5,
        success_threshold: int = 2,
        cooldown_seconds: float = 60.0,
    ):
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._last_failure_time: float = 0
        self._failure_threshold = failure_threshold
        self._success_threshold = success_threshold
        self._cooldown = cooldown_seconds

    def _transition(self, state: CircuitState):
        print(f"[Circuit] {self._state.value} → {state.value}")
        self._state = state

    def record_success(self):
        self._failure_count = 0
        if self._state == CircuitState.HALF_OPEN:
            self._success_count += 1
            if self._success_count >= self._success_threshold:
                self._success_count = 0
                self._transition(CircuitState.CLOSED)

    def record_failure(self):
        self._failure_count += 1
        self._last_failure_time = time.monotonic()
        if self._state == CircuitState.HALF_OPEN:
            self._transition(CircuitState.OPEN)
        elif self._failure_count >= self._failure_threshold:
            self._transition(CircuitState.OPEN)

    def can_attempt(self) -> bool:
        if self._state == CircuitState.CLOSED:
            return True
        if self._state == CircuitState.OPEN:
            if time.monotonic() - self._last_failure_time > self._cooldown:
                self._transition(CircuitState.HALF_OPEN)
                return True
            return False
        # HALF_OPEN: allow one probe
        return True

circuit = CircuitBreaker(failure_threshold=3, cooldown_seconds=30.0)
client = anthropic.AsyncAnthropic()

async def call_with_circuit_breaker(messages: list[dict]) -> str:
    if not circuit.can_attempt():
        raise RuntimeError(
            "Service circuit is OPEN. Requests are paused for recovery. "
            "Please try again later."
        )

    try:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=messages,
        )
        circuit.record_success()
        return response.content[0].text

    except (anthropic.RateLimitError, anthropic.APIStatusError) as e:
        circuit.record_failure()
        raise RuntimeError(f"API call failed: {e}") from e

async def main():
    for i in range(10):
        try:
            result = await call_with_circuit_breaker(
                [{"role": "user", "content": f"Query {i}"}]
            )
            print(f"[{i}] OK: {result[:60]}")
        except RuntimeError as e:
            print(f"[{i}] REJECTED: {e}")
        await asyncio.sleep(0.5)

asyncio.run(main())
```

**Expected Token Savings:** None — prevents cascading failures; saves failed API call costs
**Environment:** `pip install anthropic`

---

### Option 6 — Timeout Middleware with User-Facing Status

Wrap the agent call in a middleware that tracks wait time and surfaces status updates to the user if they're waiting more than N seconds.

```python
import asyncio
import time
import anthropic
from anthropic import RateLimitError

client = anthropic.AsyncAnthropic()

async def call_with_status_updates(
    messages: list[dict],
    max_wait_seconds: float = 45.0,
    status_interval: float = 5.0,
    status_callback=None,
) -> str:
    """
    Retries on rate limit with user-visible status updates.
    status_callback(message: str) is called to inform the user.
    """
    if status_callback is None:
        status_callback = lambda msg: print(f"[STATUS] {msg}")

    start = time.monotonic()
    deadline = start + max_wait_seconds
    last_status = start
    attempt = 0

    while True:
        elapsed = time.monotonic() - start
        remaining = deadline - time.monotonic()

        if remaining <= 0:
            status_callback(
                "Request timed out after waiting too long. "
                "Please try again when the service is less busy."
            )
            raise TimeoutError(f"Timed out after {elapsed:.0f}s ({attempt} attempts)")

        try:
            response = await asyncio.wait_for(
                client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=512,
                    messages=messages,
                ),
                timeout=min(remaining, 15.0),
            )
            if attempt > 0:
                status_callback(f"Request completed after {elapsed:.0f}s wait.")
            return response.content[0].text

        except RateLimitError as e:
            retry_after = float(e.response.headers.get("retry-after", 5.0))
            wait = min(retry_after, remaining - 1.0)

            if wait <= 0:
                raise TimeoutError("Rate limited and deadline is too close")

            attempt += 1
            now = time.monotonic()

            if now - last_status >= status_interval:
                status_callback(
                    f"Service is busy — retrying in {wait:.0f}s "
                    f"(attempt {attempt}, {remaining:.0f}s remaining)"
                )
                last_status = now

            await asyncio.sleep(wait)

        except asyncio.TimeoutError:
            raise TimeoutError("Individual API call timed out")

async def main():
    status_messages = []

    def collect_status(msg: str):
        status_messages.append(msg)
        print(f"  → {msg}")

    try:
        result = await call_with_status_updates(
            messages=[{"role": "user", "content": "What is the meaning of life?"}],
            max_wait_seconds=30.0,
            status_callback=collect_status,
        )
        print(f"Result: {result[:100]}")
    except TimeoutError as e:
        print(f"Final timeout: {e}")

asyncio.run(main())
```

**Expected Token Savings:** None — UX fix; keeps users informed rather than silently hung
**Environment:** `pip install anthropic`

---

## Comparison

| Option | Bounding Mechanism | Memory Safety | User Feedback | Best For |
|--------|-------------------|---------------|---------------|----------|
| Absolute Deadline | Wall-clock TTL | Yes | No | Simple request handlers |
| Bounded Queue | Fixed queue depth | Yes | Immediate reject | High-concurrency services |
| Token Bucket | Rate tokens | Yes | Immediate shed | Throughput-sensitive APIs |
| Priority Queue | Heap with expiry | Yes | On expiry | Mixed-priority workloads |
| Circuit Breaker | Failure counter | Yes | Immediate reject | Cascading failure prevention |
| Status Middleware | Deadline + callback | Yes | Polling updates | Interactive user-facing agents |

**Recommended starting point:** Option 1 (Absolute Deadline) for all request handlers; add Option 5 (Circuit Breaker) for production services under sustained load.
