---
layout: solution
title: "Agent Doesn't Throttle Tool Calls to External APIs"
category: concurrency
description: "Agent fires parallel tool calls to rate-limited external APIs without any throttling, causing 429 errors, account bans, or cascading failures — while the error handling retries make the problem worse."
tags: [concurrency, rate-limiting, tool-calls, external-apis, throttling]
---

## Symptom

An agent with parallel tool calls hammers an external API:

```
[10:01:00.001] GET /api/user/1  → 200
[10:01:00.002] GET /api/user/2  → 200
[10:01:00.003] GET /api/user/3  → 429 Too Many Requests
[10:01:00.004] GET /api/user/4  → 429 Too Many Requests
[10:01:00.005] GET /api/user/5  → 429 Too Many Requests
[10:01:00.006] Retry user/3    → 429  ← retry storm begins
```

The retries amplify the original problem. Eventually the account is rate-limited for minutes or the API bans the IP.

## Root Cause

The agent executes all tool calls in parallel with `asyncio.gather()` and no rate control:

```python
import asyncio
import httpx
import anthropic

client = anthropic.AsyncAnthropic(api_key="sk-live-...")

async def fetch_user(user_id: int) -> dict:
    async with httpx.AsyncClient() as http:
        resp = await http.get(f"https://api.example.com/users/{user_id}")
        return resp.json()

# All 50 calls fire simultaneously — external API allows only 10/sec
async def fetch_all_users(user_ids: list[int]) -> list[dict]:
    return await asyncio.gather(*[fetch_user(uid) for uid in user_ids])
```

---

## Fix

### Option 1 — Semaphore-based concurrency limit

Wrap external calls in a semaphore to cap simultaneous requests regardless of how many tool calls the agent queues.

```python
import asyncio
import anthropic
import httpx

client = anthropic.AsyncAnthropic(api_key="sk-live-...")

# External API allows max 5 concurrent connections
_api_sem = asyncio.Semaphore(5)


async def fetch_user(user_id: int) -> dict:
    async with _api_sem:  # At most 5 in-flight at once
        async with httpx.AsyncClient(timeout=10.0) as http:
            resp = await http.get(f"https://api.example.com/users/{user_id}")
            resp.raise_for_status()
            return resp.json()


async def fetch_all_users(user_ids: list[int]) -> list[dict]:
    """Concurrent but throttled — never exceeds 5 simultaneous calls."""
    results = await asyncio.gather(
        *[fetch_user(uid) for uid in user_ids],
        return_exceptions=True,
    )
    return [r for r in results if not isinstance(r, Exception)]


async def main():
    # Agent tool handler: 50 user IDs, max 5 concurrent external calls
    users = await fetch_all_users(list(range(1, 51)))
    print(f"Fetched {len(users)} users")

asyncio.run(main())

# Expected Token Savings: no 429 retries → no wasted tokens on retry prompts
# Environment: any agent that uses tool calls to fetch from rate-limited REST APIs
```

---

### Option 2 — Token bucket rate limiter (requests per second)

Enforce a requests-per-second limit using a token bucket. Requests wait for a token rather than failing.

```python
import asyncio
import time
import anthropic
import httpx

client = anthropic.AsyncAnthropic(api_key="sk-live-...")


class TokenBucket:
    """Async token bucket: allows `rate` requests per second."""

    def __init__(self, rate: float, burst: int = 1):
        self.rate = rate        # tokens per second
        self.burst = burst      # max tokens stored
        self.tokens = burst
        self.last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self.last_refill
            self.tokens = min(self.burst, self.tokens + elapsed * self.rate)
            self.last_refill = now

            if self.tokens >= 1:
                self.tokens -= 1
                return

            # Wait for a token to become available
            wait = (1 - self.tokens) / self.rate

        await asyncio.sleep(wait)
        await self.acquire()  # Re-check after sleep


# External API: 10 requests per second
_bucket = TokenBucket(rate=10, burst=15)


async def fetch_user(user_id: int) -> dict:
    await _bucket.acquire()
    async with httpx.AsyncClient(timeout=10.0) as http:
        resp = await http.get(f"https://api.example.com/users/{user_id}")
        resp.raise_for_status()
        return resp.json()


async def fetch_all_users(user_ids: list[int]) -> list[dict]:
    results = await asyncio.gather(
        *[fetch_user(uid) for uid in user_ids],
        return_exceptions=True,
    )
    return [r for r in results if isinstance(r, dict)]


async def main():
    # 30 requests at 10 RPS → completes in ~3 seconds cleanly
    users = await fetch_all_users(list(range(1, 31)))
    print(f"Fetched {len(users)}/30 users without 429s")

asyncio.run(main())

# Expected Token Savings: 429 rate: 0%; compared to unthrottled (typically 40–80% 429 rate)
# Environment: agents hitting REST APIs with documented RPS limits
```

---

### Option 3 — Exponential backoff with jitter on 429

When the external API does return 429, back off with jitter instead of immediately retrying and amplifying the problem.

```python
import asyncio
import random
import anthropic
import httpx

client = anthropic.AsyncAnthropic(api_key="sk-live-...")

_api_sem = asyncio.Semaphore(8)


async def fetch_with_backoff(
    url: str,
    max_retries: int = 4,
    base_delay: float = 1.0,
) -> dict:
    async with _api_sem:
        for attempt in range(max_retries):
            try:
                async with httpx.AsyncClient(timeout=10.0) as http:
                    resp = await http.get(url)

                    if resp.status_code == 429:
                        # Respect Retry-After header if present
                        retry_after = float(resp.headers.get("Retry-After", base_delay))
                        # Add jitter: ±25% of the wait time to spread retries
                        jitter = retry_after * 0.25 * (2 * random.random() - 1)
                        wait = min(retry_after + jitter, 60.0)
                        print(f"[429] {url} — waiting {wait:.1f}s (attempt {attempt+1})")
                        await asyncio.sleep(wait)
                        base_delay *= 2  # Exponential increase for subsequent retries
                        continue

                    resp.raise_for_status()
                    return resp.json()

            except httpx.TimeoutException:
                wait = base_delay * (2 ** attempt) + random.uniform(0, 1)
                await asyncio.sleep(wait)

        raise RuntimeError(f"All {max_retries} retries failed for {url}")


async def fetch_all_users(user_ids: list[int]) -> list[dict]:
    urls = [f"https://api.example.com/users/{uid}" for uid in user_ids]
    results = await asyncio.gather(
        *[fetch_with_backoff(url) for url in urls],
        return_exceptions=True,
    )
    return [r for r in results if isinstance(r, dict)]


async def main():
    users = await fetch_all_users(list(range(1, 21)))
    print(f"Fetched {len(users)} users with backoff protection")

asyncio.run(main())

# Expected Token Savings: jitter prevents thundering herd — retries spread over time
# Environment: external APIs that return 429 with Retry-After headers (Twitter, GitHub, Stripe)
```

---

### Option 4 — Per-domain rate limit registry

Different tools call different external APIs with different rate limits. Maintain a registry of per-domain limits.

```python
import asyncio
import time
import anthropic
import httpx
from urllib.parse import urlparse

client = anthropic.AsyncAnthropic(api_key="sk-live-...")


class DomainRateLimiter:
    """Per-domain semaphore + rate tracking."""

    def __init__(self):
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self._request_times: dict[str, list[float]] = {}
        self._lock = asyncio.Lock()

        # Configure limits per domain
        self._limits = {
            "api.github.com":    {"concurrency": 5,  "rps": 10},
            "api.stripe.com":    {"concurrency": 10, "rps": 25},
            "api.sendgrid.com":  {"concurrency": 3,  "rps": 5},
            "default":           {"concurrency": 5,  "rps": 10},
        }

    def _get_limit(self, domain: str) -> dict:
        return self._limits.get(domain, self._limits["default"])

    async def _get_semaphore(self, domain: str) -> asyncio.Semaphore:
        async with self._lock:
            if domain not in self._semaphores:
                limit = self._get_limit(domain)
                self._semaphores[domain] = asyncio.Semaphore(limit["concurrency"])
            return self._semaphores[domain]

    async def wait_for_slot(self, domain: str) -> None:
        sem = await self._get_semaphore(domain)
        await sem.acquire()

    def release(self, domain: str) -> None:
        if domain in self._semaphores:
            self._semaphores[domain].release()


_limiter = DomainRateLimiter()


async def throttled_get(url: str) -> dict:
    domain = urlparse(url).netloc

    await _limiter.wait_for_slot(domain)
    try:
        async with httpx.AsyncClient(timeout=15.0) as http:
            resp = await http.get(url)
            resp.raise_for_status()
            return resp.json()
    finally:
        _limiter.release(domain)


async def main():
    # Mix of GitHub and Stripe calls — each domain throttled independently
    github_urls = [f"https://api.github.com/repos/user/repo-{i}" for i in range(10)]
    stripe_urls = [f"https://api.stripe.com/v1/customers/cus_{i:04d}" for i in range(15)]

    all_urls = github_urls + stripe_urls
    results = await asyncio.gather(
        *[throttled_get(url) for url in all_urls],
        return_exceptions=True,
    )
    ok = sum(1 for r in results if isinstance(r, dict))
    print(f"Fetched {ok}/{len(all_urls)} across mixed domains")

asyncio.run(main())

# Expected Token Savings: per-domain limits prevent one API's quota from blocking others
# Environment: agents with tool libraries spanning multiple third-party services
```

---

### Option 5 — Queue-based throttler with priority lanes

Use an `asyncio.PriorityQueue` so urgent tool calls (user-facing) proceed ahead of background calls (analytics, logging).

```python
import asyncio
import anthropic
import httpx
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine

client = anthropic.AsyncAnthropic(api_key="sk-live-...")

PRIORITY_URGENT = 0
PRIORITY_NORMAL = 1
PRIORITY_BACKGROUND = 2


@dataclass(order=True)
class WorkItem:
    priority: int
    coro_fn: Callable = field(compare=False)
    args: tuple = field(compare=False, default_factory=tuple)
    result_future: asyncio.Future = field(compare=False, default_factory=asyncio.Future)


class ThrottledExecutor:
    def __init__(self, workers: int = 5):
        self._queue: asyncio.PriorityQueue[WorkItem] = asyncio.PriorityQueue()
        self._workers = workers
        self._started = False

    async def _worker(self) -> None:
        while True:
            item = await self._queue.get()
            try:
                result = await item.coro_fn(*item.args)
                item.result_future.set_result(result)
            except Exception as exc:
                item.result_future.set_exception(exc)
            finally:
                self._queue.task_done()

    async def start(self) -> None:
        if not self._started:
            for _ in range(self._workers):
                asyncio.create_task(self._worker())
            self._started = True

    async def submit(
        self,
        coro_fn: Callable,
        *args: Any,
        priority: int = PRIORITY_NORMAL,
    ) -> Any:
        await self.start()
        loop = asyncio.get_event_loop()
        item = WorkItem(priority=priority, coro_fn=coro_fn, args=args, result_future=loop.create_future())
        await self._queue.put(item)
        return await item.result_future


_executor = ThrottledExecutor(workers=5)


async def fetch_url(url: str) -> dict:
    async with httpx.AsyncClient(timeout=10.0) as http:
        resp = await http.get(url)
        resp.raise_for_status()
        return resp.json()


async def main():
    await _executor.start()

    # Mix of urgent and background tool calls
    tasks = [
        _executor.submit(fetch_url, f"https://api.example.com/user/1", priority=PRIORITY_URGENT),
        _executor.submit(fetch_url, f"https://api.example.com/analytics/1", priority=PRIORITY_BACKGROUND),
        _executor.submit(fetch_url, f"https://api.example.com/user/2", priority=PRIORITY_URGENT),
        _executor.submit(fetch_url, f"https://api.example.com/analytics/2", priority=PRIORITY_BACKGROUND),
    ]

    results = await asyncio.gather(*tasks, return_exceptions=True)
    print(f"Completed: {sum(1 for r in results if not isinstance(r, Exception))}")

asyncio.run(main())

# Expected Token Savings: user-facing calls complete first → shorter perceived latency → fewer timeout retries
# Environment: agents mixing real-time user queries with background analytics tool calls
```

---

### Option 6 — Adaptive throttle based on API response headers

Dynamically adjust the throttle rate based on `X-RateLimit-Remaining` and `X-RateLimit-Reset` headers returned by the API.

```python
import asyncio
import time
import anthropic
import httpx

client = anthropic.AsyncAnthropic(api_key="sk-live-...")


class AdaptiveThrottler:
    def __init__(self, initial_delay: float = 0.1):
        self.delay = initial_delay    # seconds between requests
        self.min_delay = 0.05
        self.max_delay = 5.0
        self._lock = asyncio.Lock()
        self._last_request = 0.0

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_request
            if elapsed < self.delay:
                await asyncio.sleep(self.delay - elapsed)
            self._last_request = time.monotonic()

    def adapt(self, response: httpx.Response) -> None:
        """Update delay based on rate-limit headers."""
        remaining = response.headers.get("X-RateLimit-Remaining")
        reset_at   = response.headers.get("X-RateLimit-Reset")
        limit      = response.headers.get("X-RateLimit-Limit")

        if remaining is None or limit is None:
            return

        remaining_pct = int(remaining) / max(int(limit), 1)

        if remaining_pct < 0.10:
            # Under 10% remaining — slow down aggressively
            if reset_at:
                reset_in = max(0, float(reset_at) - time.time())
                self.delay = reset_in / max(int(remaining), 1) if int(remaining) > 0 else self.max_delay
            else:
                self.delay = min(self.delay * 2, self.max_delay)
        elif remaining_pct > 0.50:
            # Plenty of quota — speed up
            self.delay = max(self.delay * 0.8, self.min_delay)


_throttler = AdaptiveThrottler(initial_delay=0.1)


async def adaptive_fetch(url: str) -> dict:
    await _throttler.wait()

    async with httpx.AsyncClient(timeout=10.0) as http:
        resp = await http.get(url)
        _throttler.adapt(resp)   # Tune rate for next request

        if resp.status_code == 429:
            retry_after = float(resp.headers.get("Retry-After", 5))
            await asyncio.sleep(retry_after)
            return await adaptive_fetch(url)  # One retry after mandatory wait

        resp.raise_for_status()
        return resp.json()


async def main():
    # Sequential adaptive requests — rate auto-tunes to API response headers
    for i in range(20):
        result = await adaptive_fetch(f"https://api.example.com/items/{i}")
        print(f"Item {i}: OK | delay={_throttler.delay:.3f}s")

asyncio.run(main())

# Expected Token Savings: adaptive delay maximises throughput without hitting 429 limits
# Environment: APIs that expose X-RateLimit headers (GitHub, Twitter, Shopify)
```

---

## Comparison

| Option | Mechanism | Adapts to API | Per-Domain | Priority Support | Complexity |
|--------|-----------|---------------|------------|------------------|------------|
| 1 | Semaphore | No | No | No | Low |
| 2 | Token bucket | No | No | No | Low |
| 3 | Backoff + jitter | Retry-After header | No | No | Low |
| 4 | Per-domain registry | No | Yes | No | Medium |
| 5 | Priority queue | No | No | Yes | Medium |
| 6 | Adaptive headers | Yes (RateLimit-*) | No | No | Medium |

**Recommended starting point:** Option 1 (semaphore) for any agent making parallel tool calls — add `async with _api_sem:` in one line and immediately prevent 429 storms. Add Option 3's jitter backoff for production APIs where occasional 429s are unavoidable. Add Option 4 when the agent calls multiple different external services.
