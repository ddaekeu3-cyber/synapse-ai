---
layout: solution
title: "Agent Doesn't Implement Adaptive Retry with Jitter"
category: performance
description: "Prevent retry storms and cascading failures by implementing exponential backoff with jitter, adaptive delays, and per-error-type retry policies."
tags: [retry, backoff, jitter, resilience, rate-limiting, exponential-backoff]
---

# Agent Doesn't Implement Adaptive Retry with Jitter

Naive retries fire immediately or at fixed intervals — amplifying load on an already-stressed API and triggering retry storms that affect all clients. Adaptive retry with jitter spreads retries over a randomized window, adjusts delay based on error type, and stops retrying when the budget is exhausted.

## Option 1: Exponential Backoff with Full Jitter

```python
import asyncio
import random
import anthropic

client = anthropic.AsyncAnthropic()


async def call_with_backoff(
    prompt: str,
    max_retries: int = 5,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
) -> str:
    for attempt in range(max_retries + 1):
        try:
            r = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=256,
                messages=[{"role": "user", "content": prompt}],
            )
            return r.content[0].text

        except anthropic.RateLimitError as e:
            if attempt == max_retries:
                raise
            # Full jitter: sleep uniformly in [0, cap]
            cap = min(base_delay * (2 ** attempt), max_delay)
            delay = random.uniform(0, cap)
            print(f"[RETRY] Rate limit (attempt {attempt+1}/{max_retries}), sleeping {delay:.1f}s")
            await asyncio.sleep(delay)

        except anthropic.APIStatusError as e:
            if e.status_code in (500, 502, 503, 529) and attempt < max_retries:
                cap = min(base_delay * (2 ** attempt), max_delay)
                delay = random.uniform(0, cap)
                print(f"[RETRY] Server error {e.status_code}, sleeping {delay:.1f}s")
                await asyncio.sleep(delay)
            else:
                raise

    raise RuntimeError("Unreachable")


async def main() -> None:
    result = await call_with_backoff("Summarize exponential backoff in one paragraph.")
    print(result)


asyncio.run(main())

# Expected Token Savings: N/A (resilience pattern); prevents duplicate charges from retry storms
# Environment: Python 3.11+, asyncio; tune base_delay and max_delay for your SLA
```

## Option 2: Decorrelated Jitter (AWS-Style)

```python
import asyncio
import random
import anthropic

client = anthropic.AsyncAnthropic()


async def call_decorrelated_jitter(
    prompt: str,
    max_retries: int = 6,
    base: float = 1.0,
    cap: float = 60.0,
) -> str:
    """
    Decorrelated jitter: sleep = min(cap, random(base, prev_sleep * 3))
    Produces a wider spread than full jitter, avoids synchronized retries across clients.
    """
    sleep = base
    for attempt in range(max_retries + 1):
        try:
            r = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=256,
                messages=[{"role": "user", "content": prompt}],
            )
            return r.content[0].text

        except (anthropic.RateLimitError, anthropic.APIStatusError) as e:
            if attempt == max_retries:
                raise
            is_retryable = isinstance(e, anthropic.RateLimitError) or (
                isinstance(e, anthropic.APIStatusError) and e.status_code in (500, 502, 503, 529)
            )
            if not is_retryable:
                raise
            sleep = min(cap, random.uniform(base, sleep * 3))
            print(f"[RETRY] attempt={attempt+1}, decorrelated_sleep={sleep:.2f}s")
            await asyncio.sleep(sleep)

    raise RuntimeError("Unreachable")


async def main() -> None:
    result = await call_decorrelated_jitter("What is decorrelated jitter?")
    print(result)


asyncio.run(main())

# Expected Token Savings: Decorrelated jitter reduces API server load vs. synchronized retries
# Environment: Python 3.11+; effective when many clients retry same endpoint simultaneously
```

## Option 3: Per-Error-Type Retry Policy with Budget

```python
import asyncio
import random
import time
import anthropic
from dataclasses import dataclass, field
from enum import Enum

client = anthropic.AsyncAnthropic()


class ErrorType(Enum):
    RATE_LIMIT = "rate_limit"
    SERVER_ERROR = "server_error"
    TIMEOUT = "timeout"
    NON_RETRYABLE = "non_retryable"


@dataclass
class RetryPolicy:
    max_attempts: int
    base_delay: float
    max_delay: float
    jitter: float = 0.5  # fraction of delay to randomize


POLICIES: dict[ErrorType, RetryPolicy] = {
    ErrorType.RATE_LIMIT:   RetryPolicy(max_attempts=6, base_delay=2.0, max_delay=120.0),
    ErrorType.SERVER_ERROR: RetryPolicy(max_attempts=4, base_delay=1.0, max_delay=30.0),
    ErrorType.TIMEOUT:      RetryPolicy(max_attempts=3, base_delay=5.0, max_delay=20.0),
    ErrorType.NON_RETRYABLE: RetryPolicy(max_attempts=0, base_delay=0, max_delay=0),
}


def classify_error(e: Exception) -> ErrorType:
    if isinstance(e, anthropic.RateLimitError):
        return ErrorType.RATE_LIMIT
    if isinstance(e, asyncio.TimeoutError):
        return ErrorType.TIMEOUT
    if isinstance(e, anthropic.APIStatusError):
        if e.status_code in (500, 502, 503, 529):
            return ErrorType.SERVER_ERROR
    return ErrorType.NON_RETRYABLE


def jittered_delay(policy: RetryPolicy, attempt: int) -> float:
    base = min(policy.base_delay * (2 ** attempt), policy.max_delay)
    return base * (1 + random.uniform(-policy.jitter, policy.jitter))


@dataclass
class RetryStats:
    attempts: int = 0
    total_delay: float = 0.0
    error_types: list[str] = field(default_factory=list)


async def call_with_policy(prompt: str, timeout: float = 30.0) -> tuple[str, RetryStats]:
    stats = RetryStats()
    attempt_counts: dict[ErrorType, int] = {e: 0 for e in ErrorType}

    while True:
        stats.attempts += 1
        try:
            r = await asyncio.wait_for(
                client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=256,
                    messages=[{"role": "user", "content": prompt}],
                ),
                timeout=timeout,
            )
            return r.content[0].text, stats

        except Exception as e:
            etype = classify_error(e)
            policy = POLICIES[etype]
            attempt_counts[etype] += 1

            if attempt_counts[etype] > policy.max_attempts or etype == ErrorType.NON_RETRYABLE:
                raise

            delay = jittered_delay(policy, attempt_counts[etype] - 1)
            stats.total_delay += delay
            stats.error_types.append(etype.value)
            print(f"[RETRY] {etype.value} (attempt {attempt_counts[etype]}), delay={delay:.2f}s")
            await asyncio.sleep(delay)


async def main() -> None:
    result, stats = await call_with_policy("Explain retry policies in distributed systems.")
    print(result)
    print(f"\n[STATS] attempts={stats.attempts}, total_delay={stats.total_delay:.1f}s")


asyncio.run(main())

# Expected Token Savings: Per-type policies avoid over-retrying non-transient errors
# Environment: Python 3.11+; extend POLICIES dict for custom error types
```

## Option 4: SQLite-Backed Retry Tracker with Adaptive Delay Learning

```python
import asyncio
import random
import sqlite3
import time
import anthropic

DB_PATH = "retry_tracker.db"
client = anthropic.AsyncAnthropic()


def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS retry_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL, error_type TEXT, attempt INTEGER,
            delay_used REAL, succeeded INTEGER
        )
    """)
    conn.commit()
    return conn


def get_adaptive_base_delay(conn: sqlite3.Connection, error_type: str, default: float = 1.0) -> float:
    """Compute adaptive base delay from recent success rate."""
    rows = conn.execute("""
        SELECT AVG(delay_used), SUM(succeeded), COUNT(*)
        FROM retry_events
        WHERE error_type=? AND ts > ?
    """, (error_type, time.time() - 3600)).fetchone()

    avg_delay, successes, total = rows
    if total and total > 3 and avg_delay:
        success_rate = (successes or 0) / total
        if success_rate < 0.3:
            return min(avg_delay * 1.5, 60.0)   # increase delay if often failing
        if success_rate > 0.8:
            return max(avg_delay * 0.8, 0.5)    # decrease if mostly succeeding
        return avg_delay
    return default


async def adaptive_retry_call(prompt: str) -> str:
    conn = init_db()
    max_retries = 5

    for attempt in range(max_retries + 1):
        try:
            r = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=256,
                messages=[{"role": "user", "content": prompt}],
            )
            if attempt > 0:
                conn.execute(
                    "INSERT INTO retry_events VALUES (NULL,?,?,?,?,1)",
                    (time.time(), "success", attempt, 0.0),
                )
                conn.commit()
            conn.close()
            return r.content[0].text

        except anthropic.RateLimitError:
            if attempt == max_retries:
                conn.close()
                raise
            base = get_adaptive_base_delay(conn, "rate_limit", default=2.0)
            cap = min(base * (2 ** attempt), 120.0)
            delay = random.uniform(0, cap)
            conn.execute(
                "INSERT INTO retry_events VALUES (NULL,?,?,?,?,0)",
                (time.time(), "rate_limit", attempt, delay),
            )
            conn.commit()
            print(f"[ADAPTIVE] rate_limit attempt={attempt+1}, adaptive_base={base:.2f}, delay={delay:.2f}s")
            await asyncio.sleep(delay)

        except anthropic.APIStatusError as e:
            if e.status_code in (500, 502, 503, 529) and attempt < max_retries:
                base = get_adaptive_base_delay(conn, "server_error", default=1.0)
                delay = random.uniform(0, min(base * (2 ** attempt), 30.0))
                conn.execute(
                    "INSERT INTO retry_events VALUES (NULL,?,?,?,?,0)",
                    (time.time(), "server_error", attempt, delay),
                )
                conn.commit()
                await asyncio.sleep(delay)
            else:
                conn.close()
                raise

    conn.close()
    raise RuntimeError("Max retries exceeded")


async def main() -> None:
    result = await adaptive_retry_call("What are the benefits of adaptive retry strategies?")
    print(result)


asyncio.run(main())

# Expected Token Savings: Adaptive delay reduces wasted retries by learning from past failures
# Environment: Python 3.11+, SQLite3; DB accumulates retry history across agent runs
```

## Option 5: Token-Bucket Rate Limiter with Retry Queue

```python
import asyncio
import random
import time
import anthropic
from collections import deque
from dataclasses import dataclass

client = anthropic.AsyncAnthropic()


@dataclass
class TokenBucket:
    capacity: float
    refill_rate: float  # tokens per second
    _tokens: float = 0.0
    _last_refill: float = 0.0

    def __post_init__(self) -> None:
        self._tokens = self.capacity
        self._last_refill = time.monotonic()

    def consume(self, tokens: float = 1.0) -> bool:
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self.capacity, self._tokens + elapsed * self.refill_rate)
        self._last_refill = now
        if self._tokens >= tokens:
            self._tokens -= tokens
            return True
        return False

    def wait_time(self, tokens: float = 1.0) -> float:
        deficit = tokens - self._tokens
        return max(0.0, deficit / self.refill_rate)


# Global rate limiter: 5 requests/sec, burst capacity 10
RATE_LIMITER = TokenBucket(capacity=10.0, refill_rate=5.0)
RETRY_QUEUE: deque[asyncio.Future] = deque()


async def rate_limited_call(prompt: str, max_retries: int = 4) -> str:
    for attempt in range(max_retries + 1):
        # Wait for token bucket
        wait = RATE_LIMITER.wait_time()
        if wait > 0:
            jittered_wait = wait * (1 + random.uniform(0, 0.3))
            print(f"[BUCKET] Waiting {jittered_wait:.2f}s for rate limit token")
            await asyncio.sleep(jittered_wait)

        RATE_LIMITER.consume()

        try:
            r = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=256,
                messages=[{"role": "user", "content": prompt}],
            )
            return r.content[0].text

        except anthropic.RateLimitError:
            if attempt == max_retries:
                raise
            # Server-side limit hit despite client-side bucket — back off harder
            delay = (2 ** attempt) + random.uniform(0, 2)
            print(f"[BUCKET] Server rate limit, backoff={delay:.2f}s")
            await asyncio.sleep(delay)

        except anthropic.APIStatusError as e:
            if e.status_code in (500, 502, 503) and attempt < max_retries:
                delay = random.uniform(1, 2 ** attempt)
                await asyncio.sleep(delay)
            else:
                raise

    raise RuntimeError("Unreachable")


async def main() -> None:
    prompts = [f"Count to {i} briefly." for i in range(1, 6)]
    results = await asyncio.gather(*[rate_limited_call(p) for p in prompts])
    for i, r in enumerate(results):
        print(f"Result {i+1}: {r[:60]}")


asyncio.run(main())

# Expected Token Savings: Token bucket prevents server-side 429s, avoiding wasted retry attempts
# Environment: Python 3.11+; tune capacity and refill_rate to match your Anthropic tier limits
```

## Option 6: Retry with Circuit Breaker Integration

```python
import asyncio
import random
import time
import anthropic
from dataclasses import dataclass
from enum import Enum

client = anthropic.AsyncAnthropic()


class CBState(Enum):
    CLOSED = "closed"       # Normal operation
    OPEN = "open"           # Failing — reject fast
    HALF_OPEN = "half_open" # Testing recovery


@dataclass
class CircuitBreaker:
    failure_threshold: int = 5
    recovery_timeout: float = 30.0
    half_open_max: int = 2

    _state: CBState = CBState.CLOSED
    _failures: int = 0
    _successes: int = 0
    _last_failure_time: float = 0.0

    def record_success(self) -> None:
        self._failures = 0
        if self._state == CBState.HALF_OPEN:
            self._successes += 1
            if self._successes >= self.half_open_max:
                print("[CB] Recovered — CLOSED")
                self._state = CBState.CLOSED
                self._successes = 0

    def record_failure(self) -> None:
        self._failures += 1
        self._last_failure_time = time.monotonic()
        if self._failures >= self.failure_threshold:
            print(f"[CB] Failure threshold reached — OPEN")
            self._state = CBState.OPEN

    def allow_request(self) -> bool:
        if self._state == CBState.CLOSED:
            return True
        if self._state == CBState.OPEN:
            if time.monotonic() - self._last_failure_time > self.recovery_timeout:
                print("[CB] Recovery timeout — HALF_OPEN")
                self._state = CBState.HALF_OPEN
                self._successes = 0
                return True
            return False
        return True  # HALF_OPEN


CB = CircuitBreaker()


async def call_with_cb_and_retry(prompt: str, max_retries: int = 4) -> str:
    for attempt in range(max_retries + 1):
        if not CB.allow_request():
            raise RuntimeError(f"Circuit breaker OPEN — fast-fail after {CB._failures} failures")

        try:
            r = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=256,
                messages=[{"role": "user", "content": prompt}],
            )
            CB.record_success()
            return r.content[0].text

        except anthropic.RateLimitError:
            if attempt == max_retries:
                CB.record_failure()
                raise
            delay = random.uniform(0, min(2.0 * (2 ** attempt), 60.0))
            print(f"[RETRY] RateLimit attempt={attempt+1}, delay={delay:.2f}s")
            await asyncio.sleep(delay)

        except anthropic.APIStatusError as e:
            CB.record_failure()
            if e.status_code in (500, 502, 503, 529) and attempt < max_retries:
                delay = random.uniform(1, 2 ** attempt)
                await asyncio.sleep(delay)
            else:
                raise

    CB.record_failure()
    raise RuntimeError("Max retries exceeded")


async def main() -> None:
    result = await call_with_cb_and_retry("Explain circuit breakers in distributed systems.")
    print(result)
    print(f"\n[CB STATE] {CB._state.value}, failures={CB._failures}")


asyncio.run(main())

# Expected Token Savings: Circuit breaker stops retry cascades entirely during outages
# Environment: Python 3.11+; integrate CB instance as a singleton per agent process
```

## Comparison

| Option | Jitter Strategy | Retry Budget | Adaptive | Circuit Breaker | Best For |
|--------|----------------|-------------|----------|-----------------|----------|
| 1. Full Jitter | `[0, cap]` uniform | Per-error-type max | No | No | Simple baseline |
| 2. Decorrelated | `[base, prev*3]` | Fixed | No | No | Multi-client contention |
| 3. Per-Error Policy | Jittered exp | Per error class | No | No | Fine-grained control |
| 4. Adaptive SQLite | Jittered exp | Fixed | Yes | No | Long-running agents |
| 5. Token Bucket | Rate-limited | Fixed | No | No | High-throughput agents |
| 6. Circuit Breaker | Full jitter | Fixed | No | Yes | Production resilience |
