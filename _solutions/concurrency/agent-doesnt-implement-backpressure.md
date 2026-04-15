---
layout: solution
title: "Agent doesn't implement backpressure"
category: concurrency
description: "Agent accepts work faster than it can process it, causing memory exhaustion, cascading timeouts, and silent task loss under load."
tags: [concurrency, backpressure, queue, stability, load, asyncio]
---

## Symptom

Under moderate load the agent runs fine, but when request rate spikes the process memory climbs steadily, latency balloons for all requests (even new ones), and eventually the process OOMs or the event loop stalls. Tasks queued before the spike are silently dropped or complete hours late. Callers receive no signal that the system is saturated.

```
Requests/sec: 10   → memory stable, p99 latency 400 ms   ✓
Requests/sec: 50   → memory climbing, p99 latency 4 s     ⚠
Requests/sec: 100  → OOM or stall, tasks dropped silently  ✗
```

## Root Cause

The agent uses an unbounded queue or `asyncio.gather()` without a semaphore. Every incoming request is immediately admitted and starts allocating memory (conversation history, tool results, model response buffers). When demand exceeds capacity the queue grows without bound. There is no mechanism to signal the caller to slow down or reject excess work.

## Fix

Apply backpressure at the admission point: bound the queue, reject or shed excess work early, and propagate capacity signals to callers so they can retry later.

---

### Option 1 — Bounded asyncio.Queue with immediate rejection

```python
import anthropic
import asyncio
import time

async_client = anthropic.AsyncAnthropic()

MAX_QUEUE_DEPTH = 10        # reject when queue exceeds this
MAX_WORKERS    = 4          # concurrent model calls

request_queue: asyncio.Queue = asyncio.Queue(maxsize=MAX_QUEUE_DEPTH)

async def process_request(user_message: str) -> str:
    response = await async_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text

async def worker(worker_id: int) -> None:
    while True:
        message, future = await request_queue.get()
        try:
            result = await process_request(message)
            future.set_result(result)
        except Exception as e:
            future.set_exception(e)
        finally:
            request_queue.task_done()

async def submit(message: str) -> str:
    """
    Submit a request. Raises RuntimeError immediately if the system is at
    capacity — the caller should back off rather than piling more work on.
    """
    loop = asyncio.get_event_loop()
    future: asyncio.Future = loop.create_future()

    try:
        request_queue.put_nowait((message, future))
        print(f"[QUEUE] depth={request_queue.qsize()}/{MAX_QUEUE_DEPTH}")
    except asyncio.QueueFull:
        raise RuntimeError(
            f"System at capacity (queue={MAX_QUEUE_DEPTH}). Retry after backoff."
        )

    return await future

async def main() -> None:
    # Start worker pool
    workers = [asyncio.create_task(worker(i)) for i in range(MAX_WORKERS)]

    # Simulate 20 concurrent requests against a system that can hold 10
    async def safe_submit(i: int) -> None:
        try:
            result = await submit(f"What is {i} squared? Answer with just the number.")
            print(f"[OK  {i:02d}] {result.strip()}")
        except RuntimeError as e:
            print(f"[SHED {i:02d}] {e}")

    await asyncio.gather(*[safe_submit(i) for i in range(20)])

    for w in workers:
        w.cancel()

asyncio.run(main())
```

**Expected Token Savings:** No direct token reduction; prevents OOM crashes and ensures the token budget is spent on work that can actually complete.

**Environment:** Single-process async agents; pair with HTTP 429 or 503 responses to propagate backpressure to upstream callers.

---

### Option 2 — Semaphore with timeout as a soft backpressure gate

```python
import anthropic
import asyncio
import time

async_client = anthropic.AsyncAnthropic()

CONCURRENCY_LIMIT = 5
ADMISSION_TIMEOUT = 2.0   # seconds to wait for a slot before rejecting

semaphore = asyncio.Semaphore(CONCURRENCY_LIMIT)

_inflight = 0
_rejected = 0

async def run_with_backpressure(message: str, request_id: int) -> str | None:
    global _inflight, _rejected

    try:
        # Wait up to ADMISSION_TIMEOUT for a concurrency slot
        acquired = await asyncio.wait_for(semaphore.acquire(), timeout=ADMISSION_TIMEOUT)
    except asyncio.TimeoutError:
        _rejected += 1
        print(f"[REJECT {request_id:02d}] No slot available after {ADMISSION_TIMEOUT}s — backpressure")
        return None

    _inflight += 1
    print(f"[START  {request_id:02d}] inflight={_inflight}")
    try:
        response = await async_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=64,
            messages=[{"role": "user", "content": message}],
        )
        return response.content[0].text
    finally:
        semaphore.release()
        _inflight -= 1

async def main() -> None:
    t0 = time.monotonic()

    tasks = [
        run_with_backpressure(f"Name one country starting with letter {chr(65+i)}.", i)
        for i in range(20)
    ]
    results = await asyncio.gather(*tasks)

    elapsed = time.monotonic() - t0
    completed = sum(1 for r in results if r is not None)
    print(f"\nCompleted: {completed}/20 in {elapsed:.1f}s | Rejected: {_rejected}")

asyncio.run(main())
```

**Expected Token Savings:** 0 direct savings; prevents runaway concurrency from triggering Anthropic rate limit errors which waste tokens on failed retries.

**Environment:** Most async agents; tune `ADMISSION_TIMEOUT` based on acceptable user-facing latency budget.

---

### Option 3 — Token bucket admission controller

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass, field

async_client = anthropic.AsyncAnthropic()

@dataclass
class TokenBucket:
    """
    Classic token bucket: refills at `rate` tokens/sec up to `capacity`.
    Each request consumes `cost` tokens. If the bucket is empty, the
    request is rejected immediately (no blocking).
    """
    capacity: float
    rate: float          # tokens per second
    _tokens: float = field(init=False)
    _last_refill: float = field(init=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)

    def __post_init__(self) -> None:
        self._tokens = self.capacity
        self._last_refill = time.monotonic()

    async def consume(self, cost: float = 1.0) -> bool:
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_refill
            self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
            self._last_refill = now

            if self._tokens >= cost:
                self._tokens -= cost
                return True   # admitted
            return False      # rejected — backpressure

    @property
    def level(self) -> float:
        return round(self._tokens, 2)

# Allow burst of 8, sustain at 3 requests/sec
bucket = TokenBucket(capacity=8.0, rate=3.0)

async def handle_request(message: str, req_id: int) -> str | None:
    admitted = await bucket.consume(cost=1.0)
    if not admitted:
        print(f"[THROTTLE {req_id:02d}] bucket={bucket.level:.1f} — rejected")
        return None

    print(f"[ADMIT   {req_id:02d}] bucket={bucket.level:.1f}")
    response = await async_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=32,
        messages=[{"role": "user", "content": message}],
    )
    return response.content[0].text

async def main() -> None:
    # Simulate 15 requests arriving in quick succession
    tasks = [
        handle_request(f"What is {i}+{i}? Reply with the number only.", i)
        for i in range(15)
    ]
    results = await asyncio.gather(*tasks)
    admitted  = sum(1 for r in results if r is not None)
    throttled = sum(1 for r in results if r is None)
    print(f"\nAdmitted: {admitted} | Throttled: {throttled}")

asyncio.run(main())
```

**Expected Token Savings:** Prevents token spend on requests that would hit upstream rate limits; sustained throughput stays within provider quota.

**Environment:** Ideal for API gateways and agents exposed to external callers; tune `capacity` and `rate` to match your Anthropic tier's requests-per-minute limit.

---

### Option 4 — Priority queue with backpressure for mixed workloads

```python
import anthropic
import asyncio
import heapq
import time
from dataclasses import dataclass, field
from enum import IntEnum

async_client = anthropic.AsyncAnthropic()

class Priority(IntEnum):
    HIGH   = 0
    NORMAL = 1
    LOW    = 2

@dataclass(order=True)
class PrioritizedRequest:
    priority: int
    sequence: int                        # tiebreaker for FIFO within same priority
    message: str = field(compare=False)
    future: asyncio.Future = field(compare=False)

MAX_QUEUE_SIZE = 20
WORKERS        = 3

class PriorityBackpressureQueue:
    def __init__(self, maxsize: int) -> None:
        self._heap: list[PrioritizedRequest] = []
        self._seq  = 0
        self._maxsize = maxsize
        self._lock = asyncio.Lock()
        self._not_empty = asyncio.Event()

    async def put(self, message: str, priority: Priority, future: asyncio.Future) -> bool:
        async with self._lock:
            if len(self._heap) >= self._maxsize:
                # Backpressure: drop the lowest-priority item if new item is higher
                if self._heap and self._heap[-1].priority > priority:
                    dropped = heapq.heappop(self._heap)
                    dropped.future.set_exception(RuntimeError("Dropped: lower priority evicted"))
                    print(f"[EVICT] Dropped LOW item to make room for priority={priority.name}")
                else:
                    return False  # queue full, new item also low priority — reject it

            self._seq += 1
            item = PrioritizedRequest(int(priority), self._seq, message, future)
            heapq.heappush(self._heap, item)
            self._not_empty.set()
            return True

    async def get(self) -> PrioritizedRequest:
        while True:
            async with self._lock:
                if self._heap:
                    item = heapq.heappop(self._heap)
                    if not self._heap:
                        self._not_empty.clear()
                    return item
            await self._not_empty.wait()

queue = PriorityBackpressureQueue(maxsize=MAX_QUEUE_SIZE)

async def worker() -> None:
    while True:
        item = await queue.get()
        try:
            response = await async_client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=32,
                messages=[{"role": "user", "content": item.message}],
            )
            item.future.set_result(response.content[0].text)
        except Exception as e:
            if not item.future.done():
                item.future.set_exception(e)

async def submit(message: str, priority: Priority) -> str:
    loop = asyncio.get_event_loop()
    future: asyncio.Future = loop.create_future()
    admitted = await queue.put(message, priority, future)
    if not admitted:
        raise RuntimeError(f"Queue full and priority={priority.name} not high enough to evict")
    return await future

async def main() -> None:
    workers = [asyncio.create_task(worker()) for _ in range(WORKERS)]

    async def safe_submit(msg: str, priority: Priority, req_id: int) -> None:
        try:
            result = await submit(msg, priority)
            print(f"[DONE  {req_id:02d} {priority.name:6}] {result.strip()[:40]}")
        except RuntimeError as e:
            print(f"[REJECT {req_id:02d} {priority.name:6}] {e}")

    await asyncio.gather(
        safe_submit("Urgent: What is 1+1?", Priority.HIGH,   0),
        safe_submit("Normal: Name a fruit.",  Priority.NORMAL, 1),
        safe_submit("Low: Tell me a fact.",   Priority.LOW,    2),
        safe_submit("Urgent: What is 2+2?", Priority.HIGH,   3),
        safe_submit("Low: Name a color.",     Priority.LOW,    4),
    )

    for w in workers:
        w.cancel()

asyncio.run(main())
```

**Expected Token Savings:** High-priority work completes reliably; low-priority work is shed cleanly during saturation rather than starving all tiers equally.

**Environment:** Multi-priority workloads (user-interactive vs. background batch); critical for production agents serving both SLA-bound and best-effort requests.

---

### Option 5 — Adaptive concurrency limiter based on observed latency

```python
import anthropic
import asyncio
import time
from collections import deque

async_client = anthropic.AsyncAnthropic()

class AdaptiveConcurrencyLimiter:
    """
    AIMD (Additive Increase, Multiplicative Decrease) concurrency controller.
    Increases limit when latency is healthy; decreases when latency spikes.
    """

    def __init__(
        self,
        initial_limit: int = 4,
        min_limit: int = 1,
        max_limit: int = 20,
        target_latency_ms: float = 1500.0,
        window: int = 10,           # rolling window for average latency
    ) -> None:
        self._limit = initial_limit
        self._min   = min_limit
        self._max   = max_limit
        self._target_ms = target_latency_ms
        self._latencies: deque[float] = deque(maxlen=window)
        self._inflight = 0
        self._lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(initial_limit)

    async def _adjust(self, latency_ms: float) -> None:
        async with self._lock:
            self._latencies.append(latency_ms)
            if len(self._latencies) < 3:
                return

            avg = sum(self._latencies) / len(self._latencies)

            if avg < self._target_ms and self._limit < self._max:
                # Additive increase
                self._limit += 1
                self._semaphore._value += 1      # extend semaphore capacity
                print(f"[ACL ↑] avg={avg:.0f}ms → limit={self._limit}")
            elif avg > self._target_ms * 1.5 and self._limit > self._min:
                # Multiplicative decrease
                self._limit = max(self._min, self._limit // 2)
                # Can't reduce semaphore count directly — drain happens naturally
                print(f"[ACL ↓] avg={avg:.0f}ms → limit={self._limit}")

    async def run(self, coro) -> any:
        await self._semaphore.acquire()
        self._inflight += 1
        t0 = time.monotonic()
        try:
            return await coro
        finally:
            latency_ms = (time.monotonic() - t0) * 1000
            self._semaphore.release()
            self._inflight -= 1
            await self._adjust(latency_ms)

limiter = AdaptiveConcurrencyLimiter(initial_limit=4, target_latency_ms=1000.0)

async def call_model(message: str) -> str:
    async def _inner():
        response = await async_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=32,
            messages=[{"role": "user", "content": message}],
        )
        return response.content[0].text

    return await limiter.run(_inner())

async def main() -> None:
    tasks = [
        call_model(f"What is the square root of {i*i}? Just the number.")
        for i in range(1, 16)
    ]
    results = await asyncio.gather(*tasks)
    for i, r in enumerate(results):
        print(f"[{i+1:02d}] {r.strip()}")

asyncio.run(main())
```

**Expected Token Savings:** 0 token reduction; prevents overload-driven 529 errors which waste tokens on failed requests; automatically right-sizes concurrency to current API performance.

**Environment:** Production agents where Anthropic API latency varies throughout the day; replaces static semaphore limits with self-tuning control.

---

### Option 6 — HTTP-layer backpressure: return 429 to upstream callers

```python
import anthropic
import asyncio
import time
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

async_client = anthropic.AsyncAnthropic()
app = FastAPI()

# Backpressure state
MAX_INFLIGHT = 6
QUEUE_TIMEOUT = 5.0   # seconds a request may wait for a slot

_semaphore = asyncio.Semaphore(MAX_INFLIGHT)
_inflight  = 0
_rejected  = 0

class ChatRequest(BaseModel):
    message: str

class ChatResponse(BaseModel):
    reply: str
    inflight_at_time: int

@app.middleware("http")
async def backpressure_middleware(request: Request, call_next):
    global _inflight, _rejected
    if request.url.path != "/chat":
        return await call_next(request)

    try:
        acquired = await asyncio.wait_for(_semaphore.acquire(), timeout=QUEUE_TIMEOUT)
    except asyncio.TimeoutError:
        _rejected += 1
        return JSONResponse(
            status_code=429,
            content={
                "error": "Too many requests",
                "retry_after_seconds": QUEUE_TIMEOUT,
                "inflight": _inflight,
                "rejected_total": _rejected,
            },
            headers={"Retry-After": str(int(QUEUE_TIMEOUT))},
        )

    _inflight += 1
    try:
        response = await call_next(request)
        return response
    finally:
        _semaphore.release()
        _inflight -= 1

@app.post("/chat", response_model=ChatResponse)
async def chat(body: ChatRequest) -> ChatResponse:
    response = await async_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{"role": "user", "content": body.message}],
    )
    return ChatResponse(
        reply=response.content[0].text,
        inflight_at_time=_inflight,
    )

@app.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "inflight": _inflight,
        "capacity": MAX_INFLIGHT,
        "load_pct": round(_inflight / MAX_INFLIGHT * 100),
        "rejected_total": _rejected,
    }

# Run with: uvicorn <module>:app --host 0.0.0.0 --port 8000
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
```

**Expected Token Savings:** Upstream callers receive 429 before the agent even allocates resources; prevents wasted API spend on requests that cannot complete within SLA.

**Environment:** FastAPI or Starlette agents exposed over HTTP; clients should handle 429 + `Retry-After` header for automatic backoff.

---

## Comparison

| Option | Mechanism | Caller Signal | Eviction Policy | Adaptive |
|--------|-----------|--------------|----------------|---------|
| 1 — Bounded Queue | maxsize rejection | RuntimeError | FIFO drop | No |
| 2 — Semaphore timeout | wait + timeout | RuntimeError | FIFO | No |
| 3 — Token bucket | rate-based admission | bool False | Immediate | No |
| 4 — Priority queue | heap + eviction | RuntimeError | Evict lowest priority | No |
| 5 — Adaptive AIMD | latency-driven limit | None (internal) | Natural drain | Yes |
| 6 — HTTP 429 middleware | semaphore + timeout | HTTP 429 + Retry-After | Timeout reject | No |

**Recommended default:** Option 2 (semaphore + timeout) for simple agents; Option 6 (HTTP 429 middleware) for externally-exposed services. Add Option 5 for production systems where optimal concurrency is unknown.
