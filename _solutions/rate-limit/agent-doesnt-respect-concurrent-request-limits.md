---
layout: solution
title: "Agent Doesn't Respect Concurrent Request Limits"
category: rate-limit
description: "Agent fires dozens of parallel API calls simultaneously, immediately hitting concurrency or token-per-minute rate limits and triggering cascading 429 errors."
tags: [rate-limit, concurrency, asyncio, semaphore, throttling, reliability]
---

## Symptom

A batch job launches 50 LLM calls simultaneously. The first few succeed; the rest return `429 Too Many Requests`. Retries with exponential backoff make things worse as they pile onto an already-saturated queue. Throughput drops below what a simple sequential loop would achieve. Error logs show a burst of 429s at t=0 followed by a slow trickle of successes, but total job time is longer than expected.

## Root Cause

`asyncio.gather(*[call() for _ in range(50)])` submits all 50 requests to the API simultaneously with no pacing. Anthropic enforces both requests-per-minute (RPM) and tokens-per-minute (TPM) limits. A burst of 50 simultaneous requests saturates one or both limits immediately. The SDK's built-in retry logic retries with backoff, but if all 50 are retrying simultaneously the retry storm itself saturates the limit.

## Fix

### Option 1 — `asyncio.Semaphore` to cap concurrent in-flight requests

```python
import asyncio
import time
import anthropic

client = anthropic.AsyncAnthropic()

async def call_llm(prompt: str, sem: asyncio.Semaphore, idx: int) -> str:
    async with sem:   # at most N calls in flight at any time
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=64,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text.strip()

async def batch_process(prompts: list[str], max_concurrent: int = 5) -> list[str]:
    sem = asyncio.Semaphore(max_concurrent)
    tasks = [call_llm(p, sem, i) for i, p in enumerate(prompts)]
    return await asyncio.gather(*tasks, return_exceptions=True)

async def main() -> None:
    prompts = [f"Name one country on continent number {i % 7 + 1}." for i in range(20)]

    t0 = time.perf_counter()
    results = await batch_process(prompts, max_concurrent=5)
    elapsed = time.perf_counter() - t0

    successes = sum(1 for r in results if not isinstance(r, Exception))
    errors    = sum(1 for r in results if isinstance(r, Exception))
    print(f"Processed {len(prompts)} prompts in {elapsed:.1f}s")
    print(f"Successes: {successes}, Errors: {errors}")
    for i, r in enumerate(results[:5]):
        print(f"  [{i}] {str(r)[:60]}")

asyncio.run(main())
```

**Expected Token Savings:** Semaphore-bounded concurrency eliminates 429 errors; each prevented 429 avoids a backoff wait of 2-60 seconds and a wasted retry call.
**Environment:** Any batch agent using `asyncio.gather`; semaphore is the simplest concurrency limiter and should be the default for all parallel LLM calls.

---

### Option 2 — Token-aware rate limiter using a sliding window

```python
import asyncio
import time
import collections
import anthropic

client = anthropic.AsyncAnthropic()

class TokenRateLimiter:
    """
    Sliding-window token-per-minute limiter.
    Tracks estimated token consumption and delays when approaching the limit.
    """
    def __init__(self, max_tpm: int = 40_000, window_seconds: int = 60):
        self.max_tpm = max_tpm
        self.window  = window_seconds
        self._events: collections.deque = collections.deque()   # (timestamp, tokens)
        self._lock   = asyncio.Lock()

    def _evict_old(self) -> None:
        cutoff = time.monotonic() - self.window
        while self._events and self._events[0][0] < cutoff:
            self._events.popleft()

    def _tokens_in_window(self) -> int:
        return sum(t for _, t in self._events)

    async def acquire(self, estimated_tokens: int) -> None:
        async with self._lock:
            while True:
                self._evict_old()
                used = self._tokens_in_window()
                if used + estimated_tokens <= self.max_tpm:
                    self._events.append((time.monotonic(), estimated_tokens))
                    return
                # Need to wait until oldest event expires
                wait = self._events[0][0] + self.window - time.monotonic() + 0.1
                await asyncio.sleep(max(0.1, wait))

limiter = TokenRateLimiter(max_tpm=40_000)

async def rate_limited_call(prompt: str, idx: int) -> str:
    estimated_tokens = len(prompt.split()) * 2 + 100   # rough estimate
    await limiter.acquire(estimated_tokens)

    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{"role": "user", "content": prompt}],
    )
    actual_tokens = response.usage.input_tokens + response.usage.output_tokens
    print(f"  [{idx}] {actual_tokens} tokens used | {str(response.content[0].text)[:40]}")
    return response.content[0].text

async def main() -> None:
    prompts = [
        f"Summarise this topic in one sentence: topic_{i}"
        for i in range(10)
    ]
    t0 = time.perf_counter()
    results = await asyncio.gather(*[rate_limited_call(p, i) for i, p in enumerate(prompts)])
    elapsed = time.perf_counter() - t0
    print(f"\n{len(prompts)} calls completed in {elapsed:.1f}s")

asyncio.run(main())
```

**Expected Token Savings:** TPM limiter prevents 429 errors caused by token bursts; staying under TPM limits consistently is more important than RPM for token-heavy workloads.
**Environment:** Agents processing variable-length inputs where token consumption per call varies significantly.

---

### Option 3 — Leaky bucket request scheduler

```python
import asyncio
import time
import anthropic

client = anthropic.AsyncAnthropic()

class LeakyBucketScheduler:
    """
    Leaky bucket: requests exit the bucket at a fixed rate (requests per second).
    Smooths bursty traffic into a steady stream.
    """
    def __init__(self, rate_per_second: float = 2.0):
        self.interval    = 1.0 / rate_per_second
        self._next_slot  = time.monotonic()
        self._lock       = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now  = time.monotonic()
            wait = max(0.0, self._next_slot - now)
            self._next_slot = max(now, self._next_slot) + self.interval
        if wait > 0:
            await asyncio.sleep(wait)

scheduler = LeakyBucketScheduler(rate_per_second=3.0)   # max 3 req/s

async def scheduled_call(prompt: str, idx: int) -> str:
    await scheduler.acquire()
    t0 = time.perf_counter()
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        messages=[{"role": "user", "content": prompt}],
    )
    elapsed = time.perf_counter() - t0
    text = response.content[0].text.strip()
    print(f"  [t={time.monotonic():.1f}] [{idx:02d}] {elapsed:.2f}s | {text[:50]}")
    return text

async def main() -> None:
    prompts = [f"What is {i} squared?" for i in range(12)]
    t0 = time.monotonic()
    results = await asyncio.gather(*[scheduled_call(p, i) for i, p in enumerate(prompts)])
    elapsed = time.monotonic() - t0
    print(f"\n{len(prompts)} requests in {elapsed:.1f}s at ~3 req/s")

asyncio.run(main())
```

**Expected Token Savings:** Leaky bucket guarantees a constant request rate regardless of how many are queued; no 429s if rate ≤ API limit; no wasted retry tokens.
**Environment:** High-volume batch jobs where a predictable throughput rate is more important than minimum latency.

---

### Option 4 — Adaptive concurrency: back off on 429, scale up on success

```python
import asyncio
import time
import anthropic

client = anthropic.AsyncAnthropic()

class AdaptiveConcurrencyController:
    """
    Starts at an initial concurrency level and adjusts dynamically:
    - 429 error → halve the concurrency
    - Sustained success → slowly increase concurrency
    """
    def __init__(self, initial: int = 5, min_conc: int = 1, max_conc: int = 20):
        self.concurrency = initial
        self.min_conc    = min_conc
        self.max_conc    = max_conc
        self._sem        = asyncio.Semaphore(initial)
        self._lock       = asyncio.Lock()
        self._successes  = 0
        self._scale_up_after = 10   # increase after N consecutive successes

    async def _set_concurrency(self, new: int) -> None:
        """Adjust semaphore to new concurrency level."""
        new = max(self.min_conc, min(self.max_conc, new))
        if new == self.concurrency:
            return
        diff = new - self.concurrency
        self.concurrency = new
        if diff > 0:
            for _ in range(diff):
                self._sem.release()
        # Reducing: acquisitions will block until released naturally
        print(f"  [adaptive] concurrency → {self.concurrency}")

    async def on_success(self) -> None:
        async with self._lock:
            self._successes += 1
            if self._successes >= self._scale_up_after:
                self._successes = 0
                await self._set_concurrency(self.concurrency + 1)

    async def on_rate_limit(self) -> None:
        async with self._lock:
            self._successes = 0
            await self._set_concurrency(self.concurrency // 2)

    async def __aenter__(self):
        await self._sem.acquire()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        self._sem.release()

controller = AdaptiveConcurrencyController(initial=5)

async def adaptive_call(prompt: str, idx: int) -> str:
    async with controller:
        for attempt in range(4):
            try:
                response = await client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=64,
                    messages=[{"role": "user", "content": prompt}],
                )
                await controller.on_success()
                return response.content[0].text.strip()
            except anthropic.RateLimitError:
                await controller.on_rate_limit()
                wait = 2 ** attempt
                print(f"  [429] backing off {wait}s (concurrency now {controller.concurrency})")
                await asyncio.sleep(wait)
    return "max retries exceeded"

async def main() -> None:
    prompts = [f"Name a random animal starting with letter #{i % 26}." for i in range(25)]
    t0 = time.perf_counter()
    results = await asyncio.gather(*[adaptive_call(p, i) for i, p in enumerate(prompts)])
    elapsed = time.perf_counter() - t0
    successes = sum(1 for r in results if r != "max retries exceeded")
    print(f"\n{successes}/{len(prompts)} succeeded in {elapsed:.1f}s | final concurrency: {controller.concurrency}")

asyncio.run(main())
```

**Expected Token Savings:** Adaptive controller self-tunes to the actual API capacity; avoids both under-utilisation (too conservative) and over-utilisation (too aggressive), maximising throughput per token.
**Environment:** Long-running batch jobs where API capacity may vary; adaptive control is more robust than a fixed semaphore size.

---

### Option 5 — Priority queue: high-priority requests bypass the queue

```python
import asyncio
import heapq
import time
import anthropic

client = anthropic.AsyncAnthropic()

class PriorityRequestQueue:
    """
    Priority queue for LLM requests.
    Priority 0 = highest (interactive user requests).
    Priority 9 = lowest (background batch jobs).
    """
    def __init__(self, max_concurrent: int = 5):
        self._heap: list = []
        self._counter   = 0
        self._sem       = asyncio.Semaphore(max_concurrent)
        self._cond      = asyncio.Condition()

    async def submit(self, coro, priority: int = 5) -> any:
        """Submit a coroutine with given priority. Lower number = higher priority."""
        fut = asyncio.get_event_loop().create_future()
        async with self._cond:
            heapq.heappush(self._heap, (priority, self._counter, coro, fut))
            self._counter += 1
            self._cond.notify()
        return await fut

    async def run(self, num_workers: int = 5) -> None:
        """Run worker coroutines that drain the queue."""
        async def worker():
            while True:
                async with self._cond:
                    while not self._heap:
                        await self._cond.wait()
                    _, _, coro, fut = heapq.heappop(self._heap)
                async with self._sem:
                    try:
                        result = await coro
                        fut.set_result(result)
                    except Exception as e:
                        fut.set_exception(e)

        await asyncio.gather(*[worker() for _ in range(num_workers)])

async def llm_call(prompt: str) -> str:
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text.strip()

async def main() -> None:
    queue = PriorityRequestQueue(max_concurrent=3)

    async def demo() -> None:
        # Mix of high-priority (interactive) and low-priority (batch) requests
        tasks = []
        for i in range(5):
            prompt = f"Batch task {i}: name a random fruit."
            tasks.append(asyncio.create_task(
                queue.submit(llm_call(prompt), priority=9)
            ))
        for i in range(3):
            prompt = f"Interactive user {i}: what time is it?"
            tasks.append(asyncio.create_task(
                queue.submit(llm_call(prompt), priority=0)
            ))

        results = await asyncio.gather(*tasks, return_exceptions=True)
        for r in results:
            print(f"  {str(r)[:60]}")

    # Run queue and demo concurrently
    queue_task = asyncio.create_task(queue.run(num_workers=3))
    await demo()
    queue_task.cancel()

asyncio.run(main())
```

**Expected Token Savings:** Priority queue ensures interactive requests are never delayed by batch jobs; prevents user-visible latency spikes caused by background batch work contending for the same concurrency slots.
**Environment:** Multi-tenant agents serving both interactive users and background batch processing simultaneously.

---

### Option 6 — Rate limit headers: use API response headers to self-tune

```python
import asyncio
import time
import anthropic
from typing import NamedTuple

client = anthropic.AsyncAnthropic()

class RateLimitState(NamedTuple):
    requests_limit:    int | None
    requests_remaining: int | None
    tokens_limit:      int | None
    tokens_remaining:  int | None
    reset_requests_in: float | None   # seconds until request limit resets
    reset_tokens_in:   float | None   # seconds until token limit resets

def parse_rate_limit_headers(response) -> RateLimitState:
    """Extract rate limit state from Anthropic response headers."""
    h = getattr(response, "_response", None)
    if h is None:
        return RateLimitState(None, None, None, None, None, None)
    headers = getattr(h, "headers", {})

    def _int(key: str) -> int | None:
        val = headers.get(key)
        return int(val) if val else None

    def _secs(key: str) -> float | None:
        # Header is like "30s" or "1.5s"
        val = headers.get(key, "")
        if val.endswith("s"):
            try:
                return float(val[:-1])
            except ValueError:
                pass
        return None

    return RateLimitState(
        requests_limit=    _int("anthropic-ratelimit-requests-limit"),
        requests_remaining=_int("anthropic-ratelimit-requests-remaining"),
        tokens_limit=      _int("anthropic-ratelimit-tokens-limit"),
        tokens_remaining=  _int("anthropic-ratelimit-tokens-remaining"),
        reset_requests_in= _secs("anthropic-ratelimit-requests-reset"),
        reset_tokens_in=   _secs("anthropic-ratelimit-tokens-reset"),
    )

class HeaderAwareRateLimiter:
    def __init__(self, safety_margin: float = 0.1):
        self.margin = safety_margin   # slow down when < 10% remaining
        self._lock  = asyncio.Lock()
        self._pause_until: float = 0.0

    async def on_response(self, state: RateLimitState) -> None:
        async with self._lock:
            req_rem = state.requests_remaining
            req_lim = state.requests_limit
            if req_rem is not None and req_lim is not None and req_lim > 0:
                fraction_remaining = req_rem / req_lim
                if fraction_remaining < self.margin:
                    reset = state.reset_requests_in or 5.0
                    print(f"  [rate] {req_rem}/{req_lim} requests remaining — pausing {reset:.1f}s")
                    self._pause_until = time.monotonic() + reset

    async def wait_if_needed(self) -> None:
        async with self._lock:
            wait = self._pause_until - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)

limiter = HeaderAwareRateLimiter(safety_margin=0.15)

async def header_aware_call(prompt: str, idx: int) -> str:
    await limiter.wait_if_needed()
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        messages=[{"role": "user", "content": prompt}],
    )
    state = parse_rate_limit_headers(response)
    await limiter.on_response(state)
    text = response.content[0].text.strip()
    print(f"  [{idx}] remaining={state.requests_remaining} | {text[:50]}")
    return text

async def main() -> None:
    prompts = [f"Name planet number {i % 8 + 1} from the Sun." for i in range(10)]
    results = await asyncio.gather(*[header_aware_call(p, i) for i, p in enumerate(prompts)])
    print(f"\nCompleted {len(results)} calls")

asyncio.run(main())
```

**Expected Token Savings:** Header-aware limiting responds to actual API state rather than estimates; avoids both unnecessary throttling (too conservative) and 429s (too aggressive); the most accurate pacing strategy available.
**Environment:** Production agents with predictable access to response headers; complements semaphore limiting as a dynamic adjustment layer.

---

## Comparison

| Option | Mechanism | Adapts Dynamically | Handles TPM | Best For |
|---|---|---|---|---|
| 1. `asyncio.Semaphore` | Fixed concurrency cap | No | No | Simple batch jobs — default choice |
| 2. Token-aware sliding window | TPM sliding window | Partial | Yes | Token-heavy variable-length requests |
| 3. Leaky bucket | Fixed request rate | No | No | Smooth constant-throughput workloads |
| 4. Adaptive concurrency | Self-tunes on 429 | Yes | No | Long-running jobs with unknown API capacity |
| 5. Priority queue | Priority-based ordering | No | No | Mixed interactive + batch workloads |
| 6. Header-aware limiter | API-reported state | Yes | Yes | Production agents with header access |
