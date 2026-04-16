---
title: "Agent Doesn't Implement Rate Limit-Aware Request Scheduling"
description: "Agents that dispatch tool and LLM calls without tracking provider rate limits burst all requests simultaneously, hit the rate limit immediately, and then stall while waiting for the window to reset — turning a 2-second operation into a 60-second one. Implement rate limit-aware request scheduling that tracks token and request consumption, queues excess requests, and dispatches them as capacity becomes available."
date: 2026-04-16
difficulty: advanced
category: reliability
slug: agent-doesnt-implement-rate-limit-aware-request-scheduling
tags: [rate-limiting, request-scheduling, token-bucket, backpressure, api-quota, adaptive-dispatch]
symptoms:
  - "Agent hits 429 rate limit 2 seconds into a batch operation and stalls for 60 seconds"
  - "No tracking of requests-per-minute or tokens-per-minute against provider limits"
  - "Retry after 429 uses fixed sleep instead of reading Retry-After header"
  - "Parallel tool calls all fire simultaneously, saturating the rate limit instantly"
  - "Rate limit errors treated as transient failures and retried immediately — compounding the problem"
---

## Why This Happens

Agents that dispatch requests as fast as possible inevitably burst into rate limits. API rate limits are typically expressed as requests-per-minute (RPM) and tokens-per-minute (TPM). Without tracking current consumption against these limits, the agent has no way to know it is approaching the limit until the 429 arrives. Rate limit-aware scheduling requires maintaining a token bucket (or leaky bucket) counter for each limit dimension, checking available capacity before dispatch, and queuing requests when capacity is insufficient rather than dispatching and recovering from 429s.

## Solution 1: Rate Limit Specification

```python
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class RateLimitSpec:
    """Describes the rate limits for one API provider or endpoint."""
    name: str
    requests_per_minute: Optional[int] = None
    tokens_per_minute: Optional[int] = None
    requests_per_day: Optional[int] = None
    burst_multiplier: float = 1.0     # allow short bursts above sustained rate
    window_seconds: float = 60.0
```

## Solution 2: Token Bucket Rate Limiter

```python
import time
from threading import Lock
from typing import Optional


class TokenBucketRateLimiter:
    """
    Token bucket implementation for rate limit tracking.
    Refills at a constant rate; dispatch consumes tokens.
    Returns wait_seconds=0 if capacity is available, else the time to wait.
    """

    def __init__(
        self,
        capacity: float,
        refill_rate_per_second: float,
        initial_tokens: Optional[float] = None,
    ):
        self._capacity = capacity
        self._rate = refill_rate_per_second
        self._tokens = initial_tokens if initial_tokens is not None else capacity
        self._last_refill = time.time()
        self._lock = Lock()

    def _refill(self) -> None:
        now = time.time()
        elapsed = now - self._last_refill
        new_tokens = elapsed * self._rate
        self._tokens = min(self._capacity, self._tokens + new_tokens)
        self._last_refill = now

    def try_consume(self, amount: float = 1.0) -> float:
        """
        Returns 0 if tokens were consumed, else seconds to wait.
        """
        with self._lock:
            self._refill()
            if self._tokens >= amount:
                self._tokens -= amount
                return 0.0
            deficit = amount - self._tokens
            return deficit / self._rate

    def available(self) -> float:
        with self._lock:
            self._refill()
            return round(self._tokens, 2)

    def utilization(self) -> float:
        with self._lock:
            self._refill()
            return round(1.0 - self._tokens / self._capacity, 4)
```

## Solution 3: Multi-Dimension Rate Limit Guard

```python
import asyncio
import time
from typing import Dict, Optional


class RateLimitExceededError(Exception):
    def __init__(self, dimension: str, wait_seconds: float):
        super().__init__(f"Rate limit exceeded on '{dimension}', wait {wait_seconds:.1f}s")
        self.dimension = dimension
        self.wait_seconds = wait_seconds


class MultiDimensionRateLimitGuard:
    """
    Guards a rate-limited resource with multiple token buckets (RPM, TPM, RPD).
    Checks all dimensions before dispatch; returns max wait time if any limit is exceeded.
    """

    def __init__(self, spec: RateLimitSpec):
        self._spec = spec
        self._buckets: Dict[str, TokenBucketRateLimiter] = {}
        if spec.requests_per_minute:
            self._buckets["rpm"] = TokenBucketRateLimiter(
                capacity=spec.requests_per_minute * spec.burst_multiplier,
                refill_rate_per_second=spec.requests_per_minute / 60.0,
            )
        if spec.tokens_per_minute:
            self._buckets["tpm"] = TokenBucketRateLimiter(
                capacity=spec.tokens_per_minute * spec.burst_multiplier,
                refill_rate_per_second=spec.tokens_per_minute / 60.0,
            )
        if spec.requests_per_day:
            self._buckets["rpd"] = TokenBucketRateLimiter(
                capacity=spec.requests_per_day,
                refill_rate_per_second=spec.requests_per_day / 86400.0,
            )

    def check(self, token_count: int = 0) -> float:
        """Returns max wait seconds across all dimensions. 0 = proceed."""
        costs = {"rpm": 1.0, "tpm": float(token_count), "rpd": 1.0}
        max_wait = 0.0
        for dim, bucket in self._buckets.items():
            cost = costs.get(dim, 1.0)
            if cost > 0:
                wait = bucket.try_consume(cost)
                if wait > 0:
                    # Undo the consume (bucket didn't actually consume if wait > 0)
                    max_wait = max(max_wait, wait)
        return max_wait

    async def acquire(self, token_count: int = 0) -> None:
        """Waits until capacity is available, then acquires."""
        while True:
            wait = self.check(token_count)
            if wait == 0:
                return
            await asyncio.sleep(min(wait, 1.0))

    def status(self) -> dict:
        return {
            dim: {
                "available": bucket.available(),
                "utilization": bucket.utilization(),
            }
            for dim, bucket in self._buckets.items()
        }
```

## Solution 4: Rate-Aware Request Scheduler

```python
import asyncio
import time
from typing import Any, Callable, Dict, Optional


class ScheduledRequest:
    def __init__(self, fn: Callable, token_count: int, priority: int):
        self.fn = fn
        self.token_count = token_count
        self.priority = priority
        self.future: asyncio.Future = asyncio.get_event_loop().create_future()
        self.enqueued_at = time.time()


class RateAwareRequestScheduler:
    """
    Queues requests and dispatches them as rate limit capacity becomes available.
    Higher-priority requests are dispatched first when capacity is limited.
    """

    def __init__(
        self,
        guard: MultiDimensionRateLimitGuard,
        max_queue_size: int = 200,
    ):
        self._guard = guard
        self._max_queue = max_queue_size
        self._queue: asyncio.PriorityQueue = asyncio.PriorityQueue(maxsize=max_queue_size)
        self._running = False
        self._dispatcher_task: Optional[asyncio.Task] = None
        self._dispatched = 0
        self._queued_total = 0

    async def start(self) -> None:
        self._running = True
        self._dispatcher_task = asyncio.create_task(self._dispatch_loop())

    async def stop(self) -> None:
        self._running = False
        if self._dispatcher_task:
            self._dispatcher_task.cancel()

    async def submit(self, fn: Callable, token_count: int = 0, priority: int = 5) -> Any:
        req = ScheduledRequest(fn, token_count, priority)
        await self._queue.put((priority, time.time(), req))
        self._queued_total += 1
        return await req.future

    async def _dispatch_loop(self) -> None:
        while self._running:
            try:
                _, _, req = await asyncio.wait_for(self._queue.get(), timeout=0.1)
            except asyncio.TimeoutError:
                continue

            await self._guard.acquire(req.token_count)
            self._dispatched += 1

            try:
                result = await req.fn()
                if not req.future.done():
                    req.future.set_result(result)
            except Exception as exc:
                if not req.future.done():
                    req.future.set_exception(exc)

    def stats(self) -> dict:
        return {
            "dispatched": self._dispatched,
            "queued_total": self._queued_total,
            "queue_depth": self._queue.qsize(),
            "guard_status": self._guard.status(),
        }
```

## Solution 5: Retry-After Header Parser

```python
import time
from typing import Any, Optional


class RetryAfterParser:
    """
    Parses Retry-After headers from 429 responses to determine
    the correct wait time rather than using a fixed backoff.
    """

    def parse_wait(self, response_headers: dict, fallback_seconds: float = 60.0) -> float:
        retry_after = response_headers.get("Retry-After") or response_headers.get("retry-after")
        if retry_after is None:
            return fallback_seconds
        try:
            return float(retry_after)
        except ValueError:
            pass
        # HTTP-date format
        import email.utils
        try:
            parsed_dt = email.utils.parsedate_to_datetime(retry_after)
            wait = (parsed_dt.timestamp() - time.time())
            return max(0.0, wait)
        except Exception:
            return fallback_seconds

    def parse_x_ratelimit(self, headers: dict) -> dict:
        """Extracts x-ratelimit-* headers for proactive capacity tracking."""
        return {
            "remaining_requests": int(headers.get("x-ratelimit-remaining-requests", -1)),
            "remaining_tokens": int(headers.get("x-ratelimit-remaining-tokens", -1)),
            "reset_requests": headers.get("x-ratelimit-reset-requests", ""),
            "reset_tokens": headers.get("x-ratelimit-reset-tokens", ""),
        }
```

## Solution 6: Rate Limit Dashboard

```python
import time


class RateLimitDashboard:
    """
    Combines guard status, scheduler stats, and utilization trends.
    """

    def __init__(
        self,
        scheduler: RateAwareRequestScheduler,
        guard: MultiDimensionRateLimitGuard,
    ):
        self._scheduler = scheduler
        self._guard = guard

    def render(self) -> dict:
        stats = self._scheduler.stats()
        return {
            "generated_at": time.time(),
            "queue_depth": stats["queue_depth"],
            "dispatched": stats["dispatched"],
            "bucket_status": stats["guard_status"],
            "pressure": max(
                (v["utilization"] for v in stats["guard_status"].values()),
                default=0.0,
            ),
        }
```

## Comparison

| Approach | Token Bucket | Multi-Dimension | Request Queuing | Retry-After Parsing | Dashboard |
|---|---|---|---|---|---|
| TokenBucketRateLimiter | Yes | No | No | No | No |
| MultiDimensionRateLimitGuard | Yes (per dim) | Yes (RPM+TPM+RPD) | No | No | No |
| RateAwareRequestScheduler | Via guard | Via guard | Yes (priority queue) | No | No |
| RetryAfterParser | No | No | No | Yes | No |
| RateLimitDashboard | No | No | No | No | Yes |

**Best for production**: Initialize `MultiDimensionRateLimitGuard` with 80% of the actual provider limit as the capacity — this leaves a 20% headroom buffer and prevents hitting the real limit under bursty conditions. Parse `x-ratelimit-remaining-*` headers from each response and update the token bucket accordingly; this keeps the scheduler's model synchronized with the provider's actual view of consumption. Set `max_queue_size=200` to avoid unbounded memory growth; if the queue fills, apply backpressure to callers rather than dropping requests. Alert when queue depth exceeds 50 for more than 30 seconds — this indicates systematic over-subscription that requires reducing concurrent agent operations.
