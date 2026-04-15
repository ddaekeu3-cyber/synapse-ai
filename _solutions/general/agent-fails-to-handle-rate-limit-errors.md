---
layout: solution
title: "Agent Crashes or Hangs on API Rate Limit 429 Errors"
category: general
description: "The agent receives a 429 Too Many Requests from the Anthropic API and either crashes, retries immediately (making the problem worse), or waits forever. It doesn't read the Retry-After header. It has no exponential backoff. Under load, all concurrent workers hit the limit at the same time — thundering herd — and none of them back off."
tags: [rate-limit, 429, exponential-backoff, retry, throttling, thundering-herd, anthropic-api, resilience]
---

# Agent Crashes or Hangs on API Rate Limit 429 Errors

## Symptom
- `anthropic.RateLimitError` exceptions crash the agent mid-task
- Retries are immediate — hitting the same limit over and over
- Logs show hundreds of 429 errors in rapid succession
- Multiple concurrent workers all fail at the same time (thundering herd)
- Agent ignores the `Retry-After` header and uses fixed sleep instead
- No backoff → rate limit lasts longer than necessary
- Agent gives up after one retry and reports failure to user

## Root Cause
The Anthropic API enforces tokens-per-minute (TPM) and requests-per-minute (RPM) limits. When an agent hits a 429, immediate retry amplifies the problem: all retrying workers compete for the same rate-limited window. The `Retry-After` header tells clients exactly how long to wait, but most agents ignore it. Exponential backoff with jitter is the standard fix — it staggers retries across time so individual workers don't all collide on the same window boundary.

## Fix

### Option 1: Built-in retry with the Anthropic SDK — enable automatic backoff

```python
import anthropic

# The Anthropic SDK has built-in retry logic — use it:
client = anthropic.Anthropic(
    max_retries=4,        # Retry up to 4 times (default is 2)
    timeout=60.0,         # Per-request timeout
)

# The SDK automatically retries on 429 and 529 with exponential backoff.
# For async:
async_client = anthropic.AsyncAnthropic(
    max_retries=4,
    timeout=60.0,
)

# Verify retry behavior with a simple call:
response = client.messages.create(
    model="claude-sonnet-4-6",
    max_tokens=1024,
    messages=[{"role": "user", "content": "Hello"}]
)
# If rate-limited, the SDK retries automatically — no extra code needed.
```

### Option 2: Custom exponential backoff with jitter — full control

```python
import asyncio
import random
import time
import logging
import anthropic
from typing import Any, Callable, Awaitable

logger = logging.getLogger(__name__)

async def call_with_backoff(
    fn: Callable[..., Awaitable[Any]],
    *args,
    max_retries: int = 6,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    jitter_factor: float = 0.25,
    **kwargs
) -> Any:
    """
    Call an async function with exponential backoff + jitter on rate limit errors.
    Reads Retry-After header when available.
    """
    last_exc = None

    for attempt in range(max_retries + 1):
        try:
            return await fn(*args, **kwargs)

        except anthropic.RateLimitError as exc:
            last_exc = exc

            if attempt == max_retries:
                logger.error(f"Rate limit: exhausted {max_retries} retries. Giving up.")
                raise

            # Try to read Retry-After header from the response
            retry_after = None
            if hasattr(exc, "response") and exc.response is not None:
                retry_after_header = exc.response.headers.get("retry-after")
                if retry_after_header:
                    try:
                        retry_after = float(retry_after_header)
                    except (ValueError, TypeError):
                        pass

            if retry_after:
                wait = retry_after
                logger.warning(f"Rate limited (attempt {attempt + 1}). Retry-After={wait:.1f}s")
            else:
                # Exponential backoff: 1s, 2s, 4s, 8s, 16s, 32s ...
                exponential = base_delay * (2 ** attempt)
                # Add jitter: ±25% to stagger concurrent workers
                jitter = exponential * jitter_factor * (2 * random.random() - 1)
                wait = min(exponential + jitter, max_delay)
                logger.warning(
                    f"Rate limited (attempt {attempt + 1}/{max_retries}). "
                    f"Waiting {wait:.1f}s before retry."
                )

            await asyncio.sleep(wait)

        except anthropic.APIStatusError as exc:
            # 529 = Anthropic overloaded — treat like rate limit
            if exc.status_code == 529:
                last_exc = exc
                if attempt == max_retries:
                    raise
                wait = min(base_delay * (2 ** attempt), max_delay)
                logger.warning(f"API overloaded (529). Waiting {wait:.1f}s")
                await asyncio.sleep(wait)
            else:
                raise  # Don't retry on other errors

    raise last_exc

# Usage:
async def call_claude(prompt: str) -> str:
    client = anthropic.AsyncAnthropic(max_retries=0)  # Disable SDK retries; we handle them
    response = await call_with_backoff(
        client.messages.create,
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}]
    )
    return response.content[0].text
```

### Option 3: Token bucket rate limiter — stay under the limit proactively

```python
import asyncio
import time
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

@dataclass
class RateLimiter:
    """
    Token bucket rate limiter — prevents hitting the API rate limit in the first place.
    Set limits slightly below the actual API limits to leave headroom.
    """
    requests_per_minute: float = 50.0    # Anthropic default varies by tier
    tokens_per_minute: int = 100_000     # Adjust based on your plan

    _request_tokens: float = field(init=False)
    _last_refill: float = field(init=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def __post_init__(self):
        self._request_tokens = self.requests_per_minute
        self._last_refill = time.monotonic()

    def _refill(self):
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._request_tokens = min(
            self.requests_per_minute,
            self._request_tokens + elapsed * (self.requests_per_minute / 60.0)
        )
        self._last_refill = now

    async def acquire(self, estimated_tokens: int = 1000):
        """
        Wait until a request slot is available.
        estimated_tokens: estimated total tokens for this request (input + output).
        """
        async with self._lock:
            self._refill()
            while self._request_tokens < 1.0:
                wait = (1.0 - self._request_tokens) / (self.requests_per_minute / 60.0)
                logger.debug(f"Rate limiter: waiting {wait:.1f}s for request slot")
                await asyncio.sleep(wait)
                self._refill()
            self._request_tokens -= 1.0

# Singleton rate limiter — shared across all workers:
_rate_limiter = RateLimiter(requests_per_minute=45)  # Stay under the 50 RPM limit

async def rate_limited_call(prompt: str, max_tokens: int = 1024) -> str:
    """Rate-limited Claude call — waits before sending if needed."""
    await _rate_limiter.acquire(estimated_tokens=len(prompt.split()) * 1.5 + max_tokens)

    client = anthropic.AsyncAnthropic()
    response = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}]
    )
    return response.content[0].text

# Process many requests without hitting rate limits:
async def batch_process(prompts: list[str]) -> list[str]:
    # Concurrent requests are rate-limited globally by the shared limiter:
    return await asyncio.gather(*[rate_limited_call(p) for p in prompts])
```

### Option 4: Circuit breaker — stop hammering a rate-limited API

```python
import asyncio
import time
import logging
import anthropic
from enum import Enum
from typing import Any, Callable, Awaitable

logger = logging.getLogger(__name__)

class CircuitState(Enum):
    CLOSED = "closed"       # Normal operation
    OPEN = "open"           # Failing — reject calls immediately
    HALF_OPEN = "half_open" # Testing recovery

class CircuitBreaker:
    """
    Circuit breaker for the Anthropic API.
    After N failures, opens the circuit and rejects calls for a cooldown period.
    Prevents thundering herd on a rate-limited endpoint.
    """

    def __init__(
        self,
        failure_threshold: int = 5,
        cooldown_seconds: float = 60.0,
        half_open_max_calls: int = 2
    ):
        self.failure_threshold = failure_threshold
        self.cooldown = cooldown_seconds
        self.half_open_max = half_open_max_calls

        self._state = CircuitState.CLOSED
        self._failures = 0
        self._last_failure_time: float = 0
        self._half_open_calls = 0
        self._lock = asyncio.Lock()

    async def call(self, fn: Callable[..., Awaitable[Any]], *args, **kwargs) -> Any:
        async with self._lock:
            if self._state == CircuitState.OPEN:
                if time.monotonic() - self._last_failure_time >= self.cooldown:
                    self._state = CircuitState.HALF_OPEN
                    self._half_open_calls = 0
                    logger.info("Circuit breaker: HALF_OPEN — testing recovery")
                else:
                    remaining = self.cooldown - (time.monotonic() - self._last_failure_time)
                    raise RuntimeError(
                        f"Circuit OPEN — API rate limited. Retry in {remaining:.0f}s"
                    )

            if self._state == CircuitState.HALF_OPEN:
                if self._half_open_calls >= self.half_open_max:
                    raise RuntimeError("Circuit HALF_OPEN — waiting for test calls to complete")
                self._half_open_calls += 1

        try:
            result = await fn(*args, **kwargs)
            async with self._lock:
                self._failures = 0
                if self._state == CircuitState.HALF_OPEN:
                    self._state = CircuitState.CLOSED
                    logger.info("Circuit breaker: CLOSED — API recovered")
            return result

        except (anthropic.RateLimitError, anthropic.APIStatusError) as exc:
            async with self._lock:
                self._failures += 1
                self._last_failure_time = time.monotonic()

                if self._failures >= self.failure_threshold or self._state == CircuitState.HALF_OPEN:
                    self._state = CircuitState.OPEN
                    logger.warning(
                        f"Circuit breaker: OPEN after {self._failures} failures. "
                        f"Cooling down for {self.cooldown}s"
                    )
            raise

circuit = CircuitBreaker(failure_threshold=5, cooldown_seconds=60.0)

async def resilient_call(prompt: str) -> str:
    client = anthropic.AsyncAnthropic()
    response = await circuit.call(
        client.messages.create,
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}]
    )
    return response.content[0].text
```

### Option 5: Request queue with worker pool — control concurrency at the source

```python
import asyncio
import logging
import anthropic
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

@dataclass
class QueuedRequest:
    prompt: str
    max_tokens: int
    future: asyncio.Future = field(default_factory=lambda: asyncio.get_event_loop().create_future())

class RateLimitedWorkerPool:
    """
    A pool of N workers that process Claude requests.
    Limits concurrency to prevent rate limit spikes.
    All overflow requests wait in queue rather than firing simultaneously.
    """

    def __init__(
        self,
        n_workers: int = 3,               # Max concurrent Claude calls
        requests_per_minute: float = 40.0, # Target RPM (below API limit)
    ):
        self._n_workers = n_workers
        self._rpm = requests_per_minute
        self._queue: asyncio.Queue[QueuedRequest] = asyncio.Queue()
        self._workers: list[asyncio.Task] = []
        self._min_interval = 60.0 / requests_per_minute

    async def start(self):
        for i in range(self._n_workers):
            task = asyncio.create_task(self._worker(i))
            self._workers.append(task)
        logger.info(f"Worker pool started: {self._n_workers} workers, {self._rpm} RPM target")

    async def stop(self):
        for worker in self._workers:
            worker.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)

    async def submit(self, prompt: str, max_tokens: int = 1024) -> str:
        req = QueuedRequest(prompt=prompt, max_tokens=max_tokens)
        await self._queue.put(req)
        return await req.future

    async def _worker(self, worker_id: int):
        client = anthropic.AsyncAnthropic(max_retries=3)
        last_call_time = 0.0

        while True:
            req = await self._queue.get()
            try:
                # Enforce minimum interval between requests (this worker)
                now = asyncio.get_event_loop().time()
                elapsed = now - last_call_time
                if elapsed < self._min_interval:
                    await asyncio.sleep(self._min_interval - elapsed)

                response = await client.messages.create(
                    model="claude-sonnet-4-6",
                    max_tokens=req.max_tokens,
                    messages=[{"role": "user", "content": req.prompt}]
                )
                last_call_time = asyncio.get_event_loop().time()
                result = response.content[0].text

                if not req.future.done():
                    req.future.set_result(result)

            except Exception as exc:
                logger.error(f"Worker {worker_id}: request failed: {exc}")
                if not req.future.done():
                    req.future.set_exception(exc)
            finally:
                self._queue.task_done()

# Usage — 100 requests go through 3 workers at controlled rate:
pool = RateLimitedWorkerPool(n_workers=3, requests_per_minute=40.0)
await pool.start()
results = await asyncio.gather(*[pool.submit(f"Summarize item {i}") for i in range(100)])
await pool.stop()
```

### Option 6: Rate limit monitoring — track usage and warn before hitting limits

```python
import asyncio
import time
import logging
from collections import deque
from typing import Callable, Awaitable, Any
import anthropic

logger = logging.getLogger(__name__)

class RateLimitMonitor:
    """
    Track request and token usage. Log warnings when approaching limits.
    Expose usage metrics for dashboards or alerting.
    """

    def __init__(
        self,
        rpm_limit: int = 50,
        tpm_limit: int = 100_000,
        warn_pct: float = 0.80   # Warn at 80% of limit
    ):
        self.rpm_limit = rpm_limit
        self.tpm_limit = tpm_limit
        self.warn_pct = warn_pct
        self._request_times: deque[float] = deque()
        self._token_times: deque[tuple[float, int]] = deque()  # (time, tokens)

    def _cleanup_old(self):
        """Remove entries older than 60 seconds."""
        cutoff = time.monotonic() - 60.0
        while self._request_times and self._request_times[0] < cutoff:
            self._request_times.popleft()
        while self._token_times and self._token_times[0][0] < cutoff:
            self._token_times.popleft()

    def record(self, input_tokens: int, output_tokens: int):
        now = time.monotonic()
        self._request_times.append(now)
        total_tokens = input_tokens + output_tokens
        self._token_times.append((now, total_tokens))
        self._cleanup_old()

        rpm_used = len(self._request_times)
        tpm_used = sum(t for _, t in self._token_times)

        if rpm_used >= self.rpm_limit * self.warn_pct:
            logger.warning(
                f"Rate limit warning: {rpm_used}/{self.rpm_limit} RPM "
                f"({rpm_used/self.rpm_limit*100:.0f}%)"
            )

        if tpm_used >= self.tpm_limit * self.warn_pct:
            logger.warning(
                f"Token limit warning: {tpm_used:,}/{self.tpm_limit:,} TPM "
                f"({tpm_used/self.tpm_limit*100:.0f}%)"
            )

    def stats(self) -> dict:
        self._cleanup_old()
        return {
            "rpm_used": len(self._request_times),
            "rpm_limit": self.rpm_limit,
            "tpm_used": sum(t for _, t in self._token_times),
            "tpm_limit": self.tpm_limit,
        }

monitor = RateLimitMonitor(rpm_limit=50, tpm_limit=100_000)

async def monitored_call(prompt: str) -> str:
    client = anthropic.AsyncAnthropic(max_retries=4)
    response = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}]
    )
    monitor.record(
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens
    )
    return response.content[0].text
```

## Rate Limit Strategy by Traffic Pattern

| Pattern | Best Fix | Why |
|---|---|---|
| Occasional 429 errors | SDK built-in retry (Option 1) | Zero-effort, good enough for most |
| Predictable burst traffic | Token bucket limiter (Option 3) | Prevents 429 before it happens |
| Unpredictable spikes | Exponential backoff (Option 2) | Handles surprises gracefully |
| Many concurrent workers | Worker pool + queue (Option 5) | Limits concurrency at the source |
| Repeated failures | Circuit breaker (Option 4) | Prevents thundering herd |
| Production monitoring | Rate limit monitor (Option 6) | Visibility before hitting limits |

## Expected Token Savings
Naive immediate retry on 429 → N×rate-limited requests, extended ban window: potentially thousands of wasted calls
Exponential backoff → waits out the window, succeeds on next attempt: 0 wasted calls after backoff

## Environment
- Any agent making Anthropic API calls at scale (>10 req/min); rate limit errors increase non-linearly with concurrency — at 5 concurrent workers, a single limit event becomes 5 simultaneous retries, all hitting the same window; use the SDK's built-in max_retries for simple cases and add a worker pool for high-concurrency agents
- Source: direct experience; the thundering herd on 429 retry is the most common cause of sustained rate limit outages (what should be a 5-second stall becomes a 5-minute outage when all workers retry immediately)
