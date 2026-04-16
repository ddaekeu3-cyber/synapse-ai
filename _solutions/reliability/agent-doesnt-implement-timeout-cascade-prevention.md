---
title: "Agent Doesn't Implement Timeout Cascade Prevention"
slug: agent-doesnt-implement-timeout-cascade-prevention
category: reliability
tags: [timeout, cascade, deadline, asyncio, circuit-breaker, anthropic-sdk]
description: >
  The agent applies a single global timeout to every API call, causing a slow
  upstream response to exhaust all downstream callers and collapse the entire
  request graph — a timeout cascade. Without per-hop deadlines, hedging, and
  budget propagation the failure blast radius grows with the call depth.
symptoms:
  - One slow model response causes dozens of waiting callers to time out together
  - Timeout spikes cluster in correlated bursts rather than individual failures
  - Deep tool-call chains fail completely when any single hop is slow
  - No visibility into which hop consumed the time budget
related_solutions:
  - agent-doesnt-implement-circuit-breaker-per-downstream-dependency
  - agent-doesnt-implement-multi-region-failover-for-api-calls
  - agent-doesnt-implement-cooperative-cancellation-with-structured-concurrency
---

## Problem

A cascade happens when a single timeout propagates upward through a call graph:
the outermost caller sets `timeout=30s`, the inner API call takes 29 s, every
queued caller is now simultaneously starved of its remaining 1 s and all fail at
once. Classic mitigations — deadline propagation, per-hop budgets, hedged
requests, and jitter on retries — must be implemented together. Using only a
flat timeout solves nothing.

---

## Solution 1 — Per-Hop Deadline with Remaining-Budget Propagation

Attach a `Deadline` object to every request. Each hop checks the remaining
budget before issuing a downstream call; if the budget is already exhausted the
hop fails fast instead of waiting.

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass


@dataclass
class Deadline:
    """Tracks an absolute expiry shared across all hops in a request graph."""
    expires_at: float

    @classmethod
    def from_now(cls, seconds: float) -> "Deadline":
        return cls(expires_at=time.monotonic() + seconds)

    @property
    def remaining(self) -> float:
        return max(0.0, self.expires_at - time.monotonic())

    @property
    def expired(self) -> bool:
        return self.remaining == 0.0

    def check(self) -> None:
        if self.expired:
            raise TimeoutError("Deadline already expired before call")

    def hop_timeout(self, fraction: float = 0.80) -> float:
        """Reserve `fraction` of remaining budget for this hop."""
        return self.remaining * fraction


async def call_with_deadline(
    client: anthropic.AsyncAnthropic,
    messages: list,
    deadline: Deadline,
    model: str = "claude-sonnet-4-6",
    hop_fraction: float = 0.80,
) -> str:
    deadline.check()
    hop_timeout = deadline.hop_timeout(hop_fraction)
    if hop_timeout < 0.5:
        raise TimeoutError(f"Insufficient budget: {hop_timeout:.2f}s remaining")

    try:
        response = await asyncio.wait_for(
            client.messages.create(
                model=model,
                max_tokens=1024,
                messages=messages,
            ),
            timeout=hop_timeout,
        )
        return response.content[0].text
    except asyncio.TimeoutError:
        remaining = deadline.remaining
        raise TimeoutError(
            f"Hop timed out after {hop_timeout:.1f}s; "
            f"{remaining:.1f}s left in overall deadline"
        )


async def multi_hop_agent(user_query: str, total_budget_s: float = 20.0) -> str:
    client = anthropic.AsyncAnthropic()
    deadline = Deadline.from_now(total_budget_s)

    # Hop 1: classify intent
    intent = await call_with_deadline(
        client,
        [{"role": "user", "content": f"Classify in one word: {user_query}"}],
        deadline,
        model="claude-haiku-4-5-20251001",
        hop_fraction=0.25,
    )
    print(f"[hop1] intent={intent.strip()!r}  remaining={deadline.remaining:.1f}s")

    # Hop 2: generate answer using remaining budget
    answer = await call_with_deadline(
        client,
        [{"role": "user", "content": user_query}],
        deadline,
        model="claude-sonnet-4-6",
        hop_fraction=0.80,
    )
    print(f"[hop2] remaining={deadline.remaining:.1f}s")
    return answer


import asyncio
result = asyncio.run(multi_hop_agent("Explain Byzantine fault tolerance.", total_budget_s=30.0))
print(result[:120])
```

---

## Solution 2 — Hedged Requests with Automatic Cancellation

Issue a second identical request after a short delay (the "hedge"). The first
response wins and the loser is cancelled. Hedging cuts tail latency without
increasing error rates; combine it with a per-request timeout to bound worst
case.

```python
import anthropic
import asyncio
import time


async def _single_attempt(
    client: anthropic.AsyncAnthropic,
    messages: list,
    model: str,
    attempt_id: int,
) -> tuple[int, str]:
    resp = await client.messages.create(
        model=model, max_tokens=1024, messages=messages
    )
    return attempt_id, resp.content[0].text


async def hedged_create(
    messages: list,
    model: str = "claude-sonnet-4-6",
    hedge_after_s: float = 2.0,
    hard_timeout_s: float = 20.0,
) -> str:
    """
    Launch first attempt immediately. If it hasn't returned after `hedge_after_s`,
    launch a second identical attempt. Return whichever finishes first.
    """
    client = anthropic.AsyncAnthropic()

    async def race() -> str:
        # Start first attempt
        task_a = asyncio.create_task(_single_attempt(client, messages, model, 0))

        # Wait for hedge delay or task_a finishing
        try:
            done, _ = await asyncio.wait({task_a}, timeout=hedge_after_s)
            if done:
                _, text = task_a.result()
                print("[hedge] won on first attempt (no hedge needed)")
                return text
        except Exception:
            pass

        # Launch hedge
        print(f"[hedge] launching second attempt after {hedge_after_s}s")
        task_b = asyncio.create_task(_single_attempt(client, messages, model, 1))

        # Return first winner, cancel loser
        for coro in asyncio.as_completed([task_a, task_b]):
            try:
                attempt_id, text = await coro
                print(f"[hedge] attempt {attempt_id} won")
                # Cancel the loser
                for t in (task_a, task_b):
                    if not t.done():
                        t.cancel()
                return text
            except asyncio.CancelledError:
                continue
            except Exception as e:
                print(f"[hedge] attempt failed: {e}")
                continue
        raise RuntimeError("All hedged attempts failed")

    return await asyncio.wait_for(race(), timeout=hard_timeout_s)


result = asyncio.run(hedged_create(
    [{"role": "user", "content": "Summarise the CAP theorem in two sentences."}],
    hedge_after_s=3.0,
    hard_timeout_s=25.0,
))
print(result)
```

---

## Solution 3 — Timeout Budget Middleware (httpx-level)

Intercept every outgoing httpx request and inject a `timeout` derived from a
shared `contextvars.ContextVar` budget. Any coroutine that creates an
`AsyncAnthropic` client automatically inherits the propagated budget.

```python
import anthropic
import asyncio
import contextvars
import time
import httpx
from typing import Any


_deadline_cv: contextvars.ContextVar[float | None] = contextvars.ContextVar(
    "deadline", default=None
)


def set_deadline(seconds: float) -> None:
    _deadline_cv.set(time.monotonic() + seconds)


def remaining_budget() -> float | None:
    deadline = _deadline_cv.get()
    if deadline is None:
        return None
    return max(0.0, deadline - time.monotonic())


class DeadlineTransport(httpx.AsyncBaseTransport):
    """Wraps another transport and injects deadline-derived timeouts."""

    def __init__(self, wrapped: httpx.AsyncBaseTransport, min_timeout: float = 1.0):
        self._wrapped = wrapped
        self._min_timeout = min_timeout

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        budget = remaining_budget()
        if budget is not None:
            if budget < self._min_timeout:
                raise TimeoutError(f"Deadline exhausted: {budget:.2f}s left")
            # Inject timeout into request extensions
            request.extensions["timeout"] = {
                "connect": min(budget * 0.2, 5.0),
                "read":    budget * 0.8,
                "write":   min(budget * 0.1, 3.0),
                "pool":    min(budget * 0.1, 2.0),
            }
        return await self._wrapped.handle_async_request(request)


def make_deadline_client() -> anthropic.AsyncAnthropic:
    transport = DeadlineTransport(httpx.AsyncHTTPTransport())
    http_client = httpx.AsyncClient(transport=transport)
    return anthropic.AsyncAnthropic(http_client=http_client)


async def agent_turn(messages: list) -> str:
    client = make_deadline_client()
    resp = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=messages,
    )
    return resp.content[0].text


async def handle_request(query: str) -> str:
    set_deadline(15.0)   # 15-second budget for this entire request
    return await agent_turn([{"role": "user", "content": query}])


result = asyncio.run(handle_request("What is eventual consistency?"))
print(result[:100])
print(f"Budget remaining: {remaining_budget():.2f}s")
```

---

## Solution 4 — Adaptive Timeout Scaling Based on p95 Latency

Track a rolling p95 of actual API latency and scale each per-hop timeout to
`max(min_floor, p95 * multiplier)`. This prevents cascades caused by a static
timeout that was set too aggressively during initial tuning.

```python
import anthropic
import asyncio
import time
import math
from collections import deque


class LatencyTracker:
    """Maintains a sliding window of latency samples and computes percentiles."""

    def __init__(self, window: int = 200, percentile: float = 95.0):
        self._samples: deque[float] = deque(maxlen=window)
        self._pct = percentile

    def record(self, latency_s: float) -> None:
        self._samples.append(latency_s)

    def percentile(self) -> float | None:
        if not self._samples:
            return None
        sorted_s = sorted(self._samples)
        idx = math.ceil(self._pct / 100 * len(sorted_s)) - 1
        return sorted_s[max(0, idx)]

    def adaptive_timeout(
        self,
        multiplier: float = 2.0,
        min_floor: float = 3.0,
        max_ceil: float = 60.0,
    ) -> float:
        p = self.percentile()
        if p is None:
            return min_floor
        return min(max_ceil, max(min_floor, p * multiplier))


_tracker = LatencyTracker()


async def timed_create(
    client: anthropic.AsyncAnthropic,
    messages: list,
    model: str = "claude-sonnet-4-6",
) -> str:
    timeout = _tracker.adaptive_timeout()
    start = time.monotonic()
    try:
        resp = await asyncio.wait_for(
            client.messages.create(model=model, max_tokens=512, messages=messages),
            timeout=timeout,
        )
        latency = time.monotonic() - start
        _tracker.record(latency)
        print(f"[adaptive] latency={latency:.2f}s  timeout_used={timeout:.2f}s")
        return resp.content[0].text
    except asyncio.TimeoutError:
        latency = time.monotonic() - start
        _tracker.record(latency)   # record the observed latency before giving up
        print(f"[adaptive] TIMEOUT after {timeout:.2f}s  p95={_tracker.percentile():.2f}s")
        raise


async def warm_up_and_query():
    client = anthropic.AsyncAnthropic()
    questions = [
        "What is idempotency?",
        "Explain backpressure in one sentence.",
        "What is the two-generals problem?",
    ]
    for q in questions:
        try:
            text = await timed_create(client, [{"role": "user", "content": q}])
            print(f"  -> {text[:60]}")
        except TimeoutError:
            print("  -> timed out")

    print(f"\nFinal p95: {_tracker.percentile():.2f}s")
    print(f"Next adaptive timeout: {_tracker.adaptive_timeout():.2f}s")


asyncio.run(warm_up_and_query())
```

---

## Solution 5 — Token-Count-Aware Timeout Estimation

Before calling the API, count input tokens and compute a timeout proportional
to expected processing time. Short prompts get tight timeouts; long prompts get
more room. This prevents false timeouts on legitimately large requests.

```python
import anthropic
import asyncio
import time


# Empirical throughput estimates (tokens/sec) — tune for your traffic mix
THROUGHPUT_TPS = {
    "claude-haiku-4-5-20251001":  120,
    "claude-sonnet-4-6":           70,
    "claude-opus-4-6":             35,
}
OVERHEAD_S   = 2.0   # connection + TTFT overhead
SAFETY_MULT  = 2.5   # headroom multiplier
MIN_TIMEOUT  = 5.0
MAX_TIMEOUT  = 90.0


def estimate_timeout(
    model: str,
    input_tokens: int,
    max_tokens: int,
) -> float:
    tps = THROUGHPUT_TPS.get(model, 60)
    total_tokens = input_tokens + max_tokens
    estimated_s = total_tokens / tps + OVERHEAD_S
    return min(MAX_TIMEOUT, max(MIN_TIMEOUT, estimated_s * SAFETY_MULT))


async def smart_timeout_create(
    messages: list,
    model: str = "claude-sonnet-4-6",
    max_tokens: int = 1024,
) -> str:
    client = anthropic.AsyncAnthropic()

    # Count tokens first (fast, no generation cost)
    token_count = await client.messages.count_tokens(
        model=model,
        messages=messages,
    )
    input_tokens = token_count.input_tokens
    timeout = estimate_timeout(model, input_tokens, max_tokens)

    print(
        f"[smart-timeout] input={input_tokens} tokens  "
        f"max_output={max_tokens}  timeout={timeout:.1f}s"
    )

    start = time.monotonic()
    try:
        resp = await asyncio.wait_for(
            client.messages.create(
                model=model,
                max_tokens=max_tokens,
                messages=messages,
            ),
            timeout=timeout,
        )
        elapsed = time.monotonic() - start
        print(f"[smart-timeout] completed in {elapsed:.2f}s")
        return resp.content[0].text
    except asyncio.TimeoutError:
        elapsed = time.monotonic() - start
        raise TimeoutError(
            f"Exceeded estimated timeout of {timeout:.1f}s "
            f"for {input_tokens} input tokens"
        )


short = asyncio.run(smart_timeout_create(
    [{"role": "user", "content": "Hi"}],
    max_tokens=64,
))
print(short[:80])
```

---

## Solution 6 — Jittered Retry with Exponential Backoff and Cascade Breaker

Retries without jitter cause synchronized retry storms that look like cascades.
Add full-jitter exponential backoff and a shared `CascadeBreaker` that trips
open when too many timeouts cluster in a short window, shedding load before the
cascade widens.

```python
import anthropic
import asyncio
import random
import time
from dataclasses import dataclass, field
from collections import deque


@dataclass
class CascadeBreaker:
    window_s: float = 10.0
    max_timeouts: int = 5
    cooldown_s: float = 20.0
    _events: deque = field(default_factory=deque)
    _tripped_at: float = 0.0

    def record_timeout(self) -> None:
        now = time.monotonic()
        self._events.append(now)
        # Prune old events
        while self._events and now - self._events[0] > self.window_s:
            self._events.popleft()
        if len(self._events) >= self.max_timeouts:
            self._tripped_at = now
            print(f"[cascade-breaker] TRIPPED — {len(self._events)} timeouts in {self.window_s}s")

    def is_open(self) -> bool:
        if self._tripped_at == 0.0:
            return False
        if time.monotonic() - self._tripped_at > self.cooldown_s:
            print("[cascade-breaker] cooldown elapsed — half-open")
            self._tripped_at = 0.0
            self._events.clear()
            return False
        return True

    def record_success(self) -> None:
        self._events.clear()


_cascade_breaker = CascadeBreaker()


async def resilient_create(
    messages: list,
    model: str = "claude-sonnet-4-6",
    base_timeout: float = 10.0,
    max_retries: int = 3,
    base_delay: float = 1.0,
) -> str:
    if _cascade_breaker.is_open():
        raise RuntimeError("CascadeBreaker open — shedding request")

    client = anthropic.AsyncAnthropic()

    for attempt in range(max_retries + 1):
        # Full-jitter exponential backoff
        if attempt > 0:
            cap = base_delay * (2 ** attempt)
            sleep_s = random.uniform(0, cap)
            print(f"[retry] attempt={attempt}  sleep={sleep_s:.2f}s")
            await asyncio.sleep(sleep_s)

        try:
            resp = await asyncio.wait_for(
                client.messages.create(
                    model=model, max_tokens=512, messages=messages
                ),
                timeout=base_timeout,
            )
            _cascade_breaker.record_success()
            return resp.content[0].text

        except asyncio.TimeoutError:
            _cascade_breaker.record_timeout()
            if _cascade_breaker.is_open():
                raise RuntimeError("CascadeBreaker opened during retries")
            if attempt == max_retries:
                raise TimeoutError(f"All {max_retries + 1} attempts timed out")

        except anthropic.RateLimitError:
            if attempt == max_retries:
                raise
            # Rate-limit backoff is longer
            await asyncio.sleep(random.uniform(5, 15))

    raise RuntimeError("Unreachable")


async def concurrent_requests():
    queries = [
        "What is RAFT consensus?",
        "Explain Paxos briefly.",
        "What is vector clocks?",
        "Define linearizability.",
    ]
    tasks = [
        resilient_create([{"role": "user", "content": q}], base_timeout=15.0)
        for q in queries
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for q, r in zip(queries, results):
        if isinstance(r, Exception):
            print(f"FAIL [{q[:30]}]: {r}")
        else:
            print(f"OK   [{q[:30]}]: {r[:60]}")


asyncio.run(concurrent_requests())
```

---

## Comparison

| Approach | Cascade prevention mechanism | Tail-latency impact | Complexity |
|---|---|---|---|
| Per-hop deadline propagation | Fail fast when budget gone | Eliminates runaway hops | Low |
| Hedged requests | Parallel retry cuts p99 | Significant reduction | Medium |
| httpx-level deadline middleware | Automatic for all HTTP calls | No extra code per call | Medium |
| Adaptive p95 timeout scaling | Self-calibrates to real latency | Prevents over-tight timeouts | Medium |
| Token-count-aware estimation | Right-sizes timeout per request | No false timeouts on large prompts | Medium |
| Jittered retry + cascade breaker | Prevents retry storms + load shedding | Stops cascades at scale | High |

**Rule of thumb:**
- Start with per-hop deadline propagation — it's free and prevents the most common cascade
- Add hedged requests if p99 latency matters to UX
- Use cascade breaker in high-concurrency services (>50 RPS) to shed load before a slow API response spreads
