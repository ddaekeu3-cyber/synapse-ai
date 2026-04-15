---
layout: solution
title: "Agent Hits Rate Limits Without Backoff"
category: rate-limit
description: "Agent immediately retries on 429 errors, hammering the API and making the outage worse instead of waiting."
tags: [rate-limit, retry, backoff, resilience, anthropic-sdk]
---

## Symptom

Your agent receives a `429 Too Many Requests` response and immediately retries — or crashes. In the retry case, the rapid re-requests keep the rate limit active longer, the agent burns through its retry budget in seconds, and the task fails. In the crash case, every 429 terminates the run and requires a manual restart.

## Root Cause

HTTP 429 responses include a `Retry-After` header indicating how many seconds to wait. Without reading this header and backing off, every retry arrives while the limit is still active. Naive fixed-interval retries are nearly as bad: they don't respect the server's actual cooldown period and cause thundering-herd problems when multiple agent instances retry in sync.

## Fix

### Option 1 — Minimal exponential backoff with jitter

```python
import time
import random
import anthropic

client = anthropic.Anthropic()

def create_with_backoff(
    messages: list,
    model: str = "claude-haiku-4-5-20251001",
    max_tokens: int = 1024,
    max_retries: int = 6,
) -> anthropic.types.Message:
    base_delay = 1.0
    for attempt in range(max_retries):
        try:
            return client.messages.create(
                model=model,
                max_tokens=max_tokens,
                messages=messages,
            )
        except anthropic.RateLimitError as e:
            if attempt == max_retries - 1:
                raise
            # Exponential backoff with full jitter
            cap = min(base_delay * (2 ** attempt), 60.0)
            delay = random.uniform(0, cap)
            print(f"[rate-limit] attempt {attempt + 1} — sleeping {delay:.1f}s")
            time.sleep(delay)

    raise RuntimeError("unreachable")


response = create_with_backoff([{"role": "user", "content": "Hello"}])
print(response.content[0].text)
```

**Expected Token Savings:** No direct savings, but prevents wasted retries that consume quota without producing output.
**Environment:** Any agent making sequential API calls; drop-in replacement for `client.messages.create()`.

---

### Option 2 — Respect the `Retry-After` header

```python
import time
import random
import anthropic
import httpx

client = anthropic.Anthropic()

def create_respecting_retry_after(
    messages: list,
    model: str = "claude-sonnet-4-6",
    max_tokens: int = 1024,
    max_retries: int = 5,
) -> anthropic.types.Message:
    for attempt in range(max_retries):
        try:
            return client.messages.create(
                model=model,
                max_tokens=max_tokens,
                messages=messages,
            )
        except anthropic.RateLimitError as exc:
            if attempt == max_retries - 1:
                raise

            # The SDK wraps the raw response; extract Retry-After if present
            retry_after: float | None = None
            raw = getattr(exc, "response", None)
            if isinstance(raw, httpx.Response):
                header = raw.headers.get("retry-after")
                if header and header.isdigit():
                    retry_after = float(header)

            if retry_after is not None:
                delay = retry_after + random.uniform(0.1, 1.0)  # small jitter on top
                print(f"[rate-limit] Retry-After={retry_after:.0f}s — waiting {delay:.1f}s")
            else:
                delay = min(2 ** attempt + random.uniform(0, 1), 60.0)
                print(f"[rate-limit] no Retry-After header — backoff {delay:.1f}s")

            time.sleep(delay)

    raise RuntimeError("unreachable")


resp = create_respecting_retry_after([{"role": "user", "content": "Summarise the Iliad."}])
print(resp.content[0].text)
```

**Expected Token Savings:** Eliminates wasted retries that arrive before the server's cooldown expires; respects actual server cadence.
**Environment:** Production agents where precise cooldown compliance matters (e.g., shared API key across teams).

---

### Option 3 — Async retry decorator for concurrent agents

```python
import asyncio
import random
import functools
import anthropic

client = anthropic.AsyncAnthropic()

def async_retry_on_rate_limit(max_retries: int = 6, base_delay: float = 1.0):
    """Decorator that adds exponential backoff to any async function."""
    def decorator(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            for attempt in range(max_retries):
                try:
                    return await fn(*args, **kwargs)
                except anthropic.RateLimitError:
                    if attempt == max_retries - 1:
                        raise
                    delay = min(base_delay * (2 ** attempt), 60.0)
                    jitter = random.uniform(0, delay * 0.1)
                    print(f"[rate-limit] attempt {attempt + 1}, sleeping {delay + jitter:.1f}s")
                    await asyncio.sleep(delay + jitter)
        return wrapper
    return decorator


@async_retry_on_rate_limit(max_retries=5)
async def ask(prompt: str, model: str = "claude-haiku-4-5-20251001") -> str:
    response = await client.messages.create(
        model=model,
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


async def main():
    prompts = [
        "What is 2 + 2?",
        "Name three planets.",
        "What colour is the sky?",
        "Who wrote Hamlet?",
        "Define entropy.",
    ]
    results = await asyncio.gather(*[ask(p) for p in prompts])
    for prompt, result in zip(prompts, results):
        print(f"Q: {prompt!r} → {result!r}")

asyncio.run(main())
```

**Expected Token Savings:** Prevents wasted concurrent retries from piling on at the same instant (thundering herd).
**Environment:** Async agents making many parallel calls; decorator can be applied to any coroutine.

---

### Option 4 — Token-bucket rate limiter to avoid 429s proactively

```python
import asyncio
import time
import anthropic

client = anthropic.AsyncAnthropic()

class TokenBucket:
    """Proactive rate limiter: consumes tokens before each request."""

    def __init__(self, rate: float, capacity: float):
        self.rate = rate          # tokens added per second
        self.capacity = capacity  # max burst
        self._tokens = capacity
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, tokens: float = 1.0) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                elapsed = now - self._last_refill
                self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
                self._last_refill = now

                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return

                wait = (tokens - self._tokens) / self.rate
                await asyncio.sleep(wait)


# Haiku: 50 RPM on free tier → ~0.83 RPS; leave headroom at 0.7 RPS
limiter = TokenBucket(rate=0.7, capacity=5.0)


async def safe_create(messages: list, model: str = "claude-haiku-4-5-20251001") -> str:
    await limiter.acquire()
    response = await client.messages.create(
        model=model,
        max_tokens=512,
        messages=messages,
    )
    return response.content[0].text


async def main():
    tasks = [
        safe_create([{"role": "user", "content": f"Question {i}"}])
        for i in range(20)
    ]
    results = await asyncio.gather(*tasks)
    print(f"Completed {len(results)} requests without a single 429.")

asyncio.run(main())
```

**Expected Token Savings:** Zero 429 errors means zero wasted retry tokens; proactive throttling is more efficient than reactive backoff.
**Environment:** Batch processing pipelines with predictable volume; combine with option 1 as a fallback.

---

### Option 5 — Retry queue with priority lanes

```python
import asyncio
import heapq
import time
import random
import anthropic
from dataclasses import dataclass, field
from enum import IntEnum

client = anthropic.AsyncAnthropic()


class Priority(IntEnum):
    HIGH   = 0
    NORMAL = 1
    LOW    = 2


@dataclass(order=True)
class Request:
    priority: Priority
    enqueued_at: float = field(compare=False)
    messages: list     = field(compare=False)
    future: asyncio.Future = field(compare=False)


class RetryQueue:
    def __init__(self, concurrency: int = 3):
        self._heap: list[Request] = []
        self._sem  = asyncio.Semaphore(concurrency)
        self._lock = asyncio.Lock()

    async def submit(self, messages: list, priority: Priority = Priority.NORMAL) -> str:
        loop = asyncio.get_event_loop()
        fut  = loop.create_future()
        req  = Request(priority=priority, enqueued_at=time.monotonic(), messages=messages, future=fut)
        async with self._lock:
            heapq.heappush(self._heap, req)
        asyncio.ensure_future(self._process())
        return await fut

    async def _process(self):
        async with self._lock:
            if not self._heap:
                return
            req = heapq.heappop(self._heap)

        async with self._sem:
            for attempt in range(6):
                try:
                    resp = await client.messages.create(
                        model="claude-haiku-4-5-20251001",
                        max_tokens=256,
                        messages=req.messages,
                    )
                    req.future.set_result(resp.content[0].text)
                    return
                except anthropic.RateLimitError:
                    if attempt == 5:
                        req.future.set_exception(Exception("rate limit exhausted"))
                        return
                    delay = min(2 ** attempt + random.uniform(0, 1), 60.0)
                    await asyncio.sleep(delay)


async def main():
    queue = RetryQueue(concurrency=3)
    tasks = [
        queue.submit([{"role": "user", "content": "Hi"}], Priority.HIGH),
        queue.submit([{"role": "user", "content": "Summarise X"}], Priority.NORMAL),
        queue.submit([{"role": "user", "content": "Batch item"}], Priority.LOW),
    ]
    results = await asyncio.gather(*tasks)
    for r in results:
        print(r)

asyncio.run(main())
```

**Expected Token Savings:** High-priority requests complete first; low-priority requests absorb retry delays, protecting SLA-sensitive flows.
**Environment:** Multi-priority agents (e.g., interactive user requests vs. background batch jobs).

---

### Option 6 — SDK-level retry configuration via `max_retries`

```python
import anthropic

# The Anthropic SDK has built-in retry logic; configure it at client creation
client = anthropic.Anthropic(
    max_retries=4,          # default is 2; increase for rate-limit-heavy workloads
    timeout=httpx_timeout,  # optional: set per-request timeout
)

# For fine-grained control, override per request:
import httpx
httpx_timeout = httpx.Timeout(30.0, connect=5.0)

client = anthropic.Anthropic(
    max_retries=4,
    timeout=httpx_timeout,
)

def ask(prompt: str) -> str:
    # max_retries can also be overridden per call via a with_options pattern
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


# The SDK uses exponential backoff internally; override only if you need custom logic
print(ask("Explain quantum entanglement."))
print(ask("What is the capital of Japan?"))

# To disable built-in retries and handle yourself:
client_no_retry = anthropic.Anthropic(max_retries=0)

# To check what the SDK will retry, inspect the response headers:
import anthropic._models as _m  # anthropic.APIStatusError exposes .response
try:
    client_no_retry.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1,
        messages=[{"role": "user", "content": "ping"}],
    )
except anthropic.RateLimitError as e:
    print("status:", e.status_code)
    print("headers:", dict(e.response.headers))
```

**Expected Token Savings:** SDK-managed retries avoid error-path token waste; increasing `max_retries` reduces manual retry scaffolding.
**Environment:** Simple agents where SDK defaults are sufficient; start here before building custom retry logic.

---

## Comparison

| Option | Approach | Jitter | Header Aware | Async | Best For |
|---|---|---|---|---|---|
| 1. Exponential + jitter | Reactive | Full | No | No | Simple sequential agents |
| 2. Retry-After header | Reactive | Minimal | Yes | No | Shared API key, strict compliance |
| 3. Async decorator | Reactive | Decorrelated | No | Yes | Concurrent async workloads |
| 4. Token bucket | Proactive | N/A | No | Yes | Predictable batch pipelines |
| 5. Priority queue | Reactive + queued | Yes | No | Yes | Mixed-priority agents |
| 6. SDK max_retries | Built-in | SDK default | Yes | Both | Quickest setup, standard use |
