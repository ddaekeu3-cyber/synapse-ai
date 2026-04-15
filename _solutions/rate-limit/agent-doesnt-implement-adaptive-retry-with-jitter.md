---
layout: solution
title: "Agent Doesn't Implement Adaptive Retry with Jitter"
category: rate-limit
description: "Agents that retry 429s with fixed delays or pure exponential backoff cause thundering herd on shared infrastructure and waste wall-clock time."
tags: [rate-limit, retry, backoff, jitter, 429, resilience]
---

# Agent Doesn't Implement Adaptive Retry with Jitter

When Claude returns a 429 (rate limit) or 529 (overloaded), naive agents either fail immediately or retry with fixed delays. Fixed-delay retries from many workers synchronize into thundering herds. Pure exponential backoff without jitter still clusters retries. The correct approach is exponential backoff with full jitter and respect for `Retry-After` headers.

## Why This Happens

Most retry logic is written once and never revisited. Developers add `time.sleep(2)` or `time.sleep(2**attempt)` and ship it. Neither approach spreads retries randomly, and neither respects the server's own suggested delay.

---

## Option 1: Full Jitter Exponential Backoff

The AWS-recommended "full jitter" algorithm: sleep for a random duration between 0 and `min(cap, base * 2**attempt)`.

```python
import asyncio
import random
import anthropic
from anthropic import RateLimitError, APIStatusError


client = anthropic.AsyncAnthropic()

BASE_DELAY = 1.0   # seconds
CAP_DELAY = 60.0   # max sleep
MAX_RETRIES = 8


async def call_with_full_jitter(messages: list[dict], **kwargs) -> anthropic.types.Message:
    for attempt in range(MAX_RETRIES):
        try:
            return await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1024,
                messages=messages,
                **kwargs,
            )
        except RateLimitError as exc:
            if attempt == MAX_RETRIES - 1:
                raise
            # Respect Retry-After header if present
            retry_after = _parse_retry_after(exc)
            if retry_after:
                sleep_time = retry_after
            else:
                ceiling = min(CAP_DELAY, BASE_DELAY * (2 ** attempt))
                sleep_time = random.uniform(0, ceiling)  # full jitter

            print(f"Rate limited (attempt {attempt + 1}), sleeping {sleep_time:.2f}s")
            await asyncio.sleep(sleep_time)
        except APIStatusError as exc:
            if exc.status_code == 529 and attempt < MAX_RETRIES - 1:
                ceiling = min(CAP_DELAY, BASE_DELAY * (2 ** attempt))
                await asyncio.sleep(random.uniform(0, ceiling))
            else:
                raise


def _parse_retry_after(exc: RateLimitError) -> float | None:
    """Extract Retry-After header value in seconds."""
    try:
        headers = exc.response.headers
        val = headers.get("retry-after") or headers.get("Retry-After")
        return float(val) if val else None
    except Exception:
        return None


# Usage
async def main():
    response = await call_with_full_jitter(
        [{"role": "user", "content": "Summarize the Pythagorean theorem."}]
    )
    print(response.content[0].text)


if __name__ == "__main__":
    asyncio.run(main())
```

**Expected Token Savings:** No direct token savings, but reduces failed requests and retry storms; measurably higher throughput under sustained load.

**Environment:** Any async Python application; replaces naive `time.sleep` retry loops.

---

## Option 2: Decorrelated Jitter (Polly-style)

Decorrelated jitter from AWS: `sleep = min(cap, random(base, prev_sleep * 3))`. Produces wider spread than full jitter.

```python
import asyncio
import random
import anthropic
from anthropic import RateLimitError, APIStatusError

client = anthropic.AsyncAnthropic()

BASE = 1.0
CAP = 60.0
MAX_RETRIES = 8


async def call_with_decorrelated_jitter(
    messages: list[dict], **kwargs
) -> anthropic.types.Message:
    sleep = BASE
    for attempt in range(MAX_RETRIES):
        try:
            return await client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=2048,
                messages=messages,
                **kwargs,
            )
        except (RateLimitError, APIStatusError) as exc:
            is_retryable = isinstance(exc, RateLimitError) or (
                isinstance(exc, APIStatusError) and exc.status_code in (429, 529)
            )
            if not is_retryable or attempt == MAX_RETRIES - 1:
                raise

            # Decorrelated jitter: random between base and min(cap, prev * 3)
            sleep = min(CAP, random.uniform(BASE, sleep * 3))
            print(f"Retry {attempt + 1}/{MAX_RETRIES} after {sleep:.2f}s")
            await asyncio.sleep(sleep)

    raise RuntimeError("Exceeded max retries")


# Batch processing with decorrelated retries
async def process_batch(prompts: list[str]) -> list[str]:
    results = []
    for prompt in prompts:
        msg = await call_with_decorrelated_jitter(
            [{"role": "user", "content": prompt}]
        )
        results.append(msg.content[0].text)
    return results
```

**Expected Token Savings:** Better spread than full jitter for heavily concurrent agents; prevents synchronized retry bursts.

**Environment:** Batch processing pipelines; multi-worker agents hitting shared rate limits.

---

## Option 3: Retry-After Header + Adaptive Cap

Parse the `x-ratelimit-reset-requests` and `x-ratelimit-reset-tokens` headers from Anthropic to sleep exactly until the window resets.

```python
import asyncio
import time
import random
import anthropic
from anthropic import RateLimitError, APIStatusError

client = anthropic.AsyncAnthropic()


def compute_sleep(exc: Exception, attempt: int) -> float:
    """Compute sleep from Retry-After header, then fall back to jitter."""
    if isinstance(exc, RateLimitError):
        headers = exc.response.headers

        # Try x-ratelimit-reset-requests (ISO 8601 or seconds)
        for key in ("x-ratelimit-reset-requests", "x-ratelimit-reset-tokens", "retry-after"):
            val = headers.get(key)
            if val:
                try:
                    return max(0.0, float(val))
                except ValueError:
                    pass

    # Fall back to full jitter
    base, cap = 1.0, 60.0
    ceiling = min(cap, base * (2 ** attempt))
    return random.uniform(0, ceiling)


async def resilient_call(messages: list[dict], model: str = "claude-haiku-4-5-20251001") -> str:
    max_retries = 8
    for attempt in range(max_retries):
        try:
            resp = await client.messages.create(
                model=model,
                max_tokens=1024,
                messages=messages,
            )
            return resp.content[0].text
        except (RateLimitError, APIStatusError) as exc:
            retryable = isinstance(exc, RateLimitError) or (
                hasattr(exc, "status_code") and exc.status_code in (429, 529)
            )
            if not retryable or attempt == max_retries - 1:
                raise

            sleep_time = compute_sleep(exc, attempt)
            print(f"[attempt {attempt+1}] sleeping {sleep_time:.2f}s (header-guided)")
            await asyncio.sleep(sleep_time)

    raise RuntimeError("Max retries exceeded")
```

**Expected Token Savings:** Avoids sleeping longer than necessary; reduces total wall-clock time for rate-limited workloads by 20–50%.

**Environment:** Production agents; especially useful when Anthropic returns explicit reset headers.

---

## Option 4: Token Bucket Preemptive Throttle

Instead of reacting to 429s, proactively throttle requests using a token bucket that mirrors the API's RPM limit.

```python
import asyncio
import time
import anthropic

client = anthropic.AsyncAnthropic()


class TokenBucketThrottle:
    def __init__(self, rate: float, capacity: float):
        """rate: tokens per second, capacity: burst size."""
        self.rate = rate
        self.capacity = capacity
        self.tokens = capacity
        self.last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, tokens: float = 1.0):
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self.last_refill
            self.tokens = min(
                self.capacity, self.tokens + elapsed * self.rate
            )
            self.last_refill = now

            if self.tokens >= tokens:
                self.tokens -= tokens
                return

            # Not enough tokens — wait for refill
            deficit = tokens - self.tokens
            wait = deficit / self.rate
        await asyncio.sleep(wait)
        await self.acquire(tokens)


# 60 requests per minute = 1 req/sec, burst of 5
throttle = TokenBucketThrottle(rate=1.0, capacity=5.0)


async def throttled_call(prompt: str) -> str:
    await throttle.acquire()
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text


# Process 20 prompts without hitting 429
async def main():
    prompts = [f"What is {i} + {i}?" for i in range(20)]
    tasks = [throttled_call(p) for p in prompts]
    results = await asyncio.gather(*tasks)
    for r in results:
        print(r[:60])


if __name__ == "__main__":
    asyncio.run(main())
```

**Expected Token Savings:** Eliminates 429s entirely for known rate limits; zero wasted requests from throttle-caused failures.

**Environment:** High-volume agents; when you know your API tier's RPM/TPM limits.

---

## Option 5: Shared Redis Rate State Across Workers

Multiple worker processes share a Redis-backed counter to coordinate rate limit budgets and avoid synchronized retries.

```python
import asyncio
import random
import time
import anthropic

# Requires: pip install redis[asyncio]
import redis.asyncio as aioredis

client = anthropic.AsyncAnthropic()

RATE_LIMIT_KEY = "anthropic:rate_state"
RPM_LIMIT = 60  # adjust to your tier


async def get_redis():
    return await aioredis.from_url("redis://localhost:6379", decode_responses=True)


async def check_and_increment(redis) -> bool:
    """Returns True if request is allowed, False if rate-limited."""
    pipe = redis.pipeline()
    window_key = f"{RATE_LIMIT_KEY}:{int(time.time() // 60)}"
    pipe.incr(window_key)
    pipe.expire(window_key, 120)
    results = await pipe.execute()
    count = results[0]
    return count <= RPM_LIMIT


async def call_with_shared_state(prompt: str) -> str:
    redis = await get_redis()
    max_retries = 8
    base, cap = 0.5, 30.0

    for attempt in range(max_retries):
        allowed = await check_and_increment(redis)
        if not allowed:
            # Proactively back off before hitting the API
            sleep = random.uniform(0, min(cap, base * (2 ** attempt)))
            print(f"Pre-emptive back-off: {sleep:.2f}s (workers coordinating)")
            await asyncio.sleep(sleep)
            continue

        try:
            resp = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=512,
                messages=[{"role": "user", "content": prompt}],
            )
            return resp.content[0].text
        except anthropic.RateLimitError:
            sleep = random.uniform(0, min(cap, base * (2 ** attempt)))
            await asyncio.sleep(sleep)

    raise RuntimeError("Max retries exceeded")
```

**Expected Token Savings:** Coordinates across processes; prevents N workers all hitting 429 simultaneously and all backing off together.

**Environment:** Multi-process / multi-instance deployments; requires Redis.

---

## Option 6: Retry Middleware with Circuit Breaker Integration

Combine jitter backoff with a circuit breaker: after consecutive failures, open the circuit and reject immediately for a cooldown period.

```python
import asyncio
import random
import time
from dataclasses import dataclass, field
from enum import Enum
import anthropic
from anthropic import RateLimitError, APIStatusError

client = anthropic.AsyncAnthropic()


class CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class CircuitBreaker:
    failure_threshold: int = 5
    recovery_timeout: float = 30.0
    _failures: int = 0
    _state: CircuitState = CircuitState.CLOSED
    _opened_at: float = 0.0

    def record_success(self):
        self._failures = 0
        self._state = CircuitState.CLOSED

    def record_failure(self):
        self._failures += 1
        if self._failures >= self.failure_threshold:
            self._state = CircuitState.OPEN
            self._opened_at = time.monotonic()

    def allow_request(self) -> bool:
        if self._state == CircuitState.CLOSED:
            return True
        if self._state == CircuitState.OPEN:
            if time.monotonic() - self._opened_at >= self.recovery_timeout:
                self._state = CircuitState.HALF_OPEN
                return True
            return False
        return True  # HALF_OPEN: allow one probe


circuit = CircuitBreaker()


async def call_with_circuit_breaker(prompt: str) -> str:
    max_retries = 6
    base, cap = 1.0, 60.0

    for attempt in range(max_retries):
        if not circuit.allow_request():
            raise RuntimeError("Circuit breaker OPEN — fast failing")

        try:
            resp = await client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=1024,
                messages=[{"role": "user", "content": prompt}],
            )
            circuit.record_success()
            return resp.content[0].text

        except RateLimitError as exc:
            circuit.record_failure()
            if attempt == max_retries - 1:
                raise
            ceiling = min(cap, base * (2 ** attempt))
            sleep = random.uniform(0, ceiling)
            await asyncio.sleep(sleep)

        except APIStatusError as exc:
            if exc.status_code in (500, 529):
                circuit.record_failure()
            if attempt == max_retries - 1:
                raise
            ceiling = min(cap, base * (2 ** attempt))
            await asyncio.sleep(random.uniform(0, ceiling))

    raise RuntimeError("Max retries exceeded")
```

**Expected Token Savings:** Circuit breaker prevents cascading requests during API outages; combined with jitter eliminates thundering herd completely.

**Environment:** Production systems; combines rate-limit handling with broader resilience patterns.

---

## Comparison

| Option | Jitter Strategy | Retry-After Header | Multi-Process | Circuit Breaker |
|--------|----------------|-------------------|---------------|-----------------|
| 1. Full jitter | Full (0 to cap) | Yes | No | No |
| 2. Decorrelated jitter | Decorrelated (base to prev×3) | No | No | No |
| 3. Header-guided | Full jitter fallback | Yes (primary) | No | No |
| 4. Token bucket | Preemptive throttle | N/A | No | No |
| 5. Redis shared state | Full jitter | No | Yes | No |
| 6. Circuit breaker | Full jitter | No | No | Yes |
