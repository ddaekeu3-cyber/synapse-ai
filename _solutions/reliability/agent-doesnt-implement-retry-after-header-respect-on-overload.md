---
title: "Agent Doesn't Implement Retry-After Header Respect on Overload"
description: "Agents that ignore Retry-After headers on 429/503 responses hammer the API during overload, causing cascading failures and extended outages instead of cooperating with backpressure signals."
difficulty: intermediate
category: reliability
tags: [reliability, retry, rate-limiting, backoff, 429, resilience, circuit-breaker]
---

# Agent Doesn't Implement Retry-After Header Respect on Overload

## Problem

When the Anthropic API (or any upstream service) returns a `429 Too Many Requests` or `503 Service Unavailable`, the response includes a `Retry-After` header indicating how long to wait. Agents that ignore this header and immediately retry create a thundering-herd effect that prolongs the overload, burns through quota faster, and results in cascading failures across all concurrent sessions.

**Symptoms:**
- `429` errors cluster in short bursts and grow longer with each retry
- Retry storms visible in API provider dashboards
- Multiple agents competing on the same API key amplify the problem
- Exponential backoff alone is insufficient when `Retry-After` signals a longer mandatory pause
- Outage duration is 3-5x longer than the provider's actual overload window

---

## Solution 1: Basic Retry-After Header Parsing

Parse `Retry-After` (seconds integer or HTTP-date) and sleep the correct duration before retrying.

```python
import asyncio
import time
from email.utils import parsedate_to_datetime
from typing import Optional
import httpx
import anthropic


def parse_retry_after(header_value: Optional[str]) -> float:
    """Parse Retry-After header; returns seconds to wait (minimum 1.0)."""
    if not header_value:
        return 1.0
    try:
        # Integer seconds format: "Retry-After: 30"
        return max(1.0, float(header_value.strip()))
    except ValueError:
        pass
    try:
        # HTTP-date format: "Retry-After: Wed, 21 Oct 2015 07:28:00 GMT"
        retry_dt = parsedate_to_datetime(header_value)
        wait = (retry_dt.timestamp() - time.time())
        return max(1.0, wait)
    except Exception:
        return 1.0


class RetryAfterAnthropicClient:
    def __init__(self, api_key: str, max_retries: int = 5):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)
        self.max_retries = max_retries

    async def create_with_retry(
        self,
        messages: list[dict],
        system: str = "",
        model: str = "claude-opus-4-6",
        max_tokens: int = 1024,
    ) -> anthropic.types.Message:
        last_exc = None

        for attempt in range(self.max_retries):
            try:
                return await self.client.messages.create(
                    model=model,
                    max_tokens=max_tokens,
                    system=system,
                    messages=messages,
                )
            except anthropic.RateLimitError as exc:
                last_exc = exc
                # Extract Retry-After from the underlying httpx response
                retry_after_header = None
                if hasattr(exc, "response") and exc.response is not None:
                    retry_after_header = exc.response.headers.get("retry-after")

                wait = parse_retry_after(retry_after_header)
                print(
                    f"[retry] 429 on attempt {attempt+1}/{self.max_retries}; "
                    f"Retry-After={retry_after_header!r} → waiting {wait:.1f}s"
                )
                await asyncio.sleep(wait)

            except anthropic.APIStatusError as exc:
                if exc.status_code in (503, 529):
                    last_exc = exc
                    retry_after_header = None
                    if hasattr(exc, "response") and exc.response is not None:
                        retry_after_header = exc.response.headers.get("retry-after")
                    wait = parse_retry_after(retry_after_header)
                    print(f"[retry] {exc.status_code} on attempt {attempt+1}; waiting {wait:.1f}s")
                    await asyncio.sleep(wait)
                else:
                    raise

        raise RuntimeError(f"All {self.max_retries} retries exhausted") from last_exc


async def demo():
    client = RetryAfterAnthropicClient(api_key="sk-...")
    response = await client.create_with_retry(
        messages=[{"role": "user", "content": "Hello!"}]
    )
    print(response.content[0].text)

# asyncio.run(demo())
```

---

## Solution 2: Exponential Backoff with Retry-After Floor

Use exponential backoff as the baseline but treat the `Retry-After` value as a mandatory floor, never sleeping less than the server specifies.

```python
import asyncio
import random
import time
from email.utils import parsedate_to_datetime
from typing import Optional
import anthropic


def _parse_retry_after(header: Optional[str]) -> Optional[float]:
    if not header:
        return None
    try:
        return float(header.strip())
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(header)
        return max(0.0, dt.timestamp() - time.time())
    except Exception:
        return None


class ExponentialBackoffWithFloor:
    def __init__(
        self,
        base_delay: float = 1.0,
        max_delay: float = 60.0,
        multiplier: float = 2.0,
        jitter: float = 0.25,
    ):
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.multiplier = multiplier
        self.jitter = jitter

    def compute(self, attempt: int, retry_after: Optional[float]) -> float:
        """Return wait seconds: max(exponential_backoff, retry_after_floor)."""
        expo = min(self.max_delay, self.base_delay * (self.multiplier ** attempt))
        # Add ±jitter%
        expo *= 1 + random.uniform(-self.jitter, self.jitter)
        if retry_after is not None:
            # Server's word is the floor; never retry sooner than instructed
            return max(expo, retry_after)
        return expo


class ResilientAnthropicClient:
    def __init__(self, api_key: str, max_retries: int = 6):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)
        self.max_retries = max_retries
        self.backoff = ExponentialBackoffWithFloor()

    async def complete(self, messages: list[dict], **kwargs) -> str:
        for attempt in range(self.max_retries):
            try:
                resp = await self.client.messages.create(
                    model=kwargs.get("model", "claude-opus-4-6"),
                    max_tokens=kwargs.get("max_tokens", 1024),
                    messages=messages,
                )
                return resp.content[0].text

            except (anthropic.RateLimitError, anthropic.APIStatusError) as exc:
                status = getattr(exc, "status_code", 429)
                if status not in (429, 503, 529) or attempt == self.max_retries - 1:
                    raise

                header = None
                if hasattr(exc, "response") and exc.response:
                    header = exc.response.headers.get("retry-after")

                retry_after = _parse_retry_after(header)
                wait = self.backoff.compute(attempt, retry_after)
                print(
                    f"[backoff] status={status} attempt={attempt+1} "
                    f"retry_after={retry_after} wait={wait:.2f}s"
                )
                await asyncio.sleep(wait)

        raise RuntimeError("Max retries exceeded")


async def demo():
    client = ResilientAnthropicClient(api_key="sk-...")
    text = await client.complete([{"role": "user", "content": "What is 2+2?"}])
    print(text)

# asyncio.run(demo())
```

---

## Solution 3: Per-Provider Retry-After State Tracker

When running multiple providers or multiple API keys, track `Retry-After` state per provider so a throttled provider is skipped until its cooldown expires.

```python
import asyncio
import time
from dataclasses import dataclass, field
from typing import Optional
import anthropic


@dataclass
class ProviderState:
    name: str
    api_key: str
    retry_after_until: float = 0.0   # epoch time when provider is usable again
    consecutive_errors: int = 0

    @property
    def is_available(self) -> bool:
        return time.monotonic() >= self.retry_after_until

    def set_retry_after(self, seconds: float) -> None:
        self.retry_after_until = time.monotonic() + seconds
        self.consecutive_errors += 1
        print(
            f"[provider] {self.name} throttled for {seconds:.1f}s "
            f"(errors={self.consecutive_errors})"
        )

    def reset_errors(self) -> None:
        self.consecutive_errors = 0
        self.retry_after_until = 0.0


class MultiKeyRetryRouter:
    """Routes requests across multiple API keys/providers, skipping throttled ones."""

    def __init__(self, providers: list[dict]):
        self._providers = [
            ProviderState(name=p["name"], api_key=p["api_key"])
            for p in providers
        ]
        self._clients: dict[str, anthropic.AsyncAnthropic] = {
            p.name: anthropic.AsyncAnthropic(api_key=p.api_key)
            for p in self._providers
        }
        self._lock = asyncio.Lock()

    def _available_providers(self) -> list[ProviderState]:
        return [p for p in self._providers if p.is_available]

    async def complete(self, messages: list[dict], max_tokens: int = 1024) -> str:
        for _ in range(len(self._providers) * 3):
            async with self._lock:
                available = self._available_providers()

            if not available:
                # All providers throttled — wait for the soonest one
                soonest = min(self._providers, key=lambda p: p.retry_after_until)
                wait = max(0.1, soonest.retry_after_until - time.monotonic())
                print(f"[router] All providers throttled; waiting {wait:.1f}s for {soonest.name}")
                await asyncio.sleep(wait)
                continue

            provider = available[0]  # Could be round-robin or weighted
            client = self._clients[provider.name]

            try:
                resp = await client.messages.create(
                    model="claude-opus-4-6",
                    max_tokens=max_tokens,
                    messages=messages,
                )
                async with self._lock:
                    provider.reset_errors()
                return resp.content[0].text

            except anthropic.RateLimitError as exc:
                header = None
                if hasattr(exc, "response") and exc.response:
                    header = exc.response.headers.get("retry-after")

                wait_secs = float(header) if header and header.replace(".", "").isdigit() else 30.0
                async with self._lock:
                    provider.set_retry_after(wait_secs)

            except anthropic.APIStatusError as exc:
                if exc.status_code in (503, 529):
                    async with self._lock:
                        provider.set_retry_after(10.0)
                else:
                    raise

        raise RuntimeError("Could not complete request: all providers exhausted")


async def demo():
    router = MultiKeyRetryRouter(providers=[
        {"name": "key-primary", "api_key": "sk-ant-primary..."},
        {"name": "key-fallback", "api_key": "sk-ant-fallback..."},
    ])
    text = await router.complete([{"role": "user", "content": "Hello!"}])
    print(text)

# asyncio.run(demo())
```

---

## Solution 4: Circuit Breaker That Opens on 429 Storm

Open the circuit breaker when consecutive 429s exceed a threshold; honor `Retry-After` as the half-open delay.

```python
import asyncio
import time
from enum import Enum
from typing import Optional
import anthropic


class CircuitState(Enum):
    CLOSED = "closed"       # Normal operation
    OPEN = "open"           # Rejecting all requests
    HALF_OPEN = "half_open" # One probe request allowed


class RateLimitCircuitBreaker:
    def __init__(
        self,
        failure_threshold: int = 3,       # Open after this many consecutive 429s
        default_open_duration: float = 30.0,  # Fallback if no Retry-After header
    ):
        self.failure_threshold = failure_threshold
        self.default_open_duration = default_open_duration
        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._open_until: float = 0.0
        self._lock = asyncio.Lock()

    @property
    def state(self) -> CircuitState:
        if self._state == CircuitState.OPEN:
            if time.monotonic() >= self._open_until:
                return CircuitState.HALF_OPEN
        return self._state

    async def call(self, coro) -> any:
        async with self._lock:
            current = self.state
            if current == CircuitState.OPEN:
                wait = self._open_until - time.monotonic()
                raise RuntimeError(f"Circuit OPEN — retry in {wait:.1f}s")
            if current == CircuitState.HALF_OPEN:
                print("[circuit] Half-open: probing...")
                self._state = CircuitState.HALF_OPEN

        try:
            result = await coro
            async with self._lock:
                self._consecutive_failures = 0
                self._state = CircuitState.CLOSED
                print("[circuit] Probe succeeded — CLOSED")
            return result

        except anthropic.RateLimitError as exc:
            async with self._lock:
                header = None
                if hasattr(exc, "response") and exc.response:
                    header = exc.response.headers.get("retry-after")

                retry_after = float(header) if header and header.strip().isdigit() else self.default_open_duration
                self._consecutive_failures += 1

                if self._consecutive_failures >= self.failure_threshold or current == CircuitState.HALF_OPEN:
                    self._state = CircuitState.OPEN
                    self._open_until = time.monotonic() + retry_after
                    print(
                        f"[circuit] OPENED for {retry_after:.1f}s "
                        f"(failures={self._consecutive_failures})"
                    )
                else:
                    print(f"[circuit] Failure {self._consecutive_failures}/{self.failure_threshold}")
            raise


class CircuitBreakerAgent:
    def __init__(self, api_key: str):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)
        self.breaker = RateLimitCircuitBreaker(failure_threshold=3)

    async def ask(self, message: str) -> str:
        async def _call():
            resp = await self.client.messages.create(
                model="claude-opus-4-6",
                max_tokens=512,
                messages=[{"role": "user", "content": message}],
            )
            return resp.content[0].text

        return await self.breaker.call(_call())


async def demo():
    agent = CircuitBreakerAgent(api_key="sk-...")
    for i in range(10):
        try:
            text = await agent.ask(f"Question {i}")
            print(f"[{i}] OK: {text[:40]}")
        except RuntimeError as e:
            print(f"[{i}] Blocked: {e}")
        await asyncio.sleep(0.5)

# asyncio.run(demo())
```

---

## Solution 5: Queue-Based Retry Scheduler with Retry-After Priority

Enqueue failed requests with their `retry_after` timestamp; a background scheduler replays them at the right time.

```python
import asyncio
import heapq
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional
import anthropic


@dataclass(order=True)
class ScheduledRequest:
    not_before: float           # epoch time — used for heap ordering
    request_id: str = field(compare=False)
    messages: list = field(compare=False)
    future: asyncio.Future = field(compare=False)
    attempt: int = field(default=0, compare=False)
    max_attempts: int = field(default=5, compare=False)


class RetryAfterScheduler:
    def __init__(self, api_key: str):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)
        self._heap: list[ScheduledRequest] = []
        self._lock = asyncio.Lock()
        self._running = False

    async def start(self):
        self._running = True
        asyncio.create_task(self._scheduler_loop())

    async def stop(self):
        self._running = False

    async def submit(self, messages: list[dict], request_id: str = "") -> str:
        """Submit a request; returns result when eventually completed."""
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        req = ScheduledRequest(
            not_before=time.time(),
            request_id=request_id or f"req_{int(time.time()*1000)}",
            messages=messages,
            future=fut,
        )
        async with self._lock:
            heapq.heappush(self._heap, req)
        return await fut

    async def _scheduler_loop(self):
        while self._running:
            now = time.time()
            req: Optional[ScheduledRequest] = None

            async with self._lock:
                if self._heap and self._heap[0].not_before <= now:
                    req = heapq.heappop(self._heap)

            if req is None:
                await asyncio.sleep(0.1)
                continue

            try:
                resp = await self.client.messages.create(
                    model="claude-opus-4-6",
                    max_tokens=512,
                    messages=req.messages,
                )
                req.future.set_result(resp.content[0].text)

            except anthropic.RateLimitError as exc:
                if req.attempt >= req.max_attempts:
                    req.future.set_exception(RuntimeError("Max retry attempts exceeded"))
                    continue

                header = None
                if hasattr(exc, "response") and exc.response:
                    header = exc.response.headers.get("retry-after")

                wait = float(header) if header and header.strip().isdigit() else 10.0
                req.attempt += 1
                req.not_before = time.time() + wait
                print(
                    f"[scheduler] {req.request_id} rescheduled in {wait:.1f}s "
                    f"(attempt {req.attempt})"
                )
                async with self._lock:
                    heapq.heappush(self._heap, req)

            except Exception as exc:
                req.future.set_exception(exc)


async def demo():
    scheduler = RetryAfterScheduler(api_key="sk-...")
    await scheduler.start()

    # Submit multiple requests; scheduler handles 429s transparently
    tasks = [
        scheduler.submit(
            [{"role": "user", "content": f"Question {i}"}],
            request_id=f"req_{i}",
        )
        for i in range(5)
    ]
    results = await asyncio.gather(*tasks)
    for i, text in enumerate(results):
        print(f"[{i}] {text[:60]}")

    await scheduler.stop()

# asyncio.run(demo())
```

---

## Solution 6: Retry-After Aware Streaming Restart

For streaming calls, capture 429s mid-stream and restart the stream from where it left off after the Retry-After delay.

```python
import asyncio
import time
from typing import AsyncIterator, Optional
import anthropic


class ResumableStreamingClient:
    """
    Streams a response; if a 429 occurs mid-stream, waits Retry-After seconds
    then restarts the stream with the partial response injected as context.
    """

    def __init__(self, api_key: str, max_restarts: int = 3):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)
        self.max_restarts = max_restarts

    async def stream(
        self,
        messages: list[dict],
        system: str = "",
        model: str = "claude-opus-4-6",
        max_tokens: int = 1024,
    ) -> AsyncIterator[str]:
        accumulated = ""
        restarts = 0
        current_messages = list(messages)

        while restarts <= self.max_restarts:
            try:
                async with self.client.messages.stream(
                    model=model,
                    max_tokens=max_tokens,
                    system=system,
                    messages=current_messages,
                ) as stream:
                    async for event in stream:
                        if hasattr(event, "type") and event.type == "content_block_delta":
                            chunk = getattr(event.delta, "text", "")
                            if chunk:
                                accumulated += chunk
                                yield chunk
                return  # Stream completed normally

            except anthropic.RateLimitError as exc:
                if restarts >= self.max_restarts:
                    raise

                header = None
                if hasattr(exc, "response") and exc.response:
                    header = exc.response.headers.get("retry-after")

                wait = float(header) if header and header.strip().isdigit() else 15.0
                restarts += 1
                print(
                    f"[stream] 429 mid-stream after {len(accumulated)} chars; "
                    f"restart {restarts}/{self.max_restarts} in {wait:.1f}s"
                )
                await asyncio.sleep(wait)

                # Inject partial response and ask model to continue
                if accumulated:
                    current_messages = list(messages) + [
                        {"role": "assistant", "content": accumulated},
                        {"role": "user", "content": "Please continue from where you left off."},
                    ]
                    yield "\n[Resuming after rate limit pause...]\n"


async def demo():
    client = ResumableStreamingClient(api_key="sk-...")
    print("Streaming: ", end="", flush=True)
    async for chunk in client.stream(
        messages=[{"role": "user", "content": "Write a detailed explanation of TCP/IP."}],
        max_tokens=800,
    ):
        print(chunk, end="", flush=True)
    print()

# asyncio.run(demo())
```

---

## Comparison

| Solution | Mechanism | Honors Retry-After | Multi-Provider | Streaming | Complexity |
|---|---|---|---|---|---|
| Basic header parsing | Sleep exact Retry-After value | Yes | No | No | Very Low |
| Exponential backoff + floor | max(expo_backoff, retry_after) | Yes | No | No | Low |
| Per-provider state tracker | Skip throttled providers | Yes | Yes | No | Medium |
| Circuit breaker | Open on 429 storm | Yes (open duration) | No | No | Medium |
| Queue-based scheduler | Heap ordered by retry_after | Yes | No | No | High |
| Streaming restart | Resume with partial context | Yes | No | Yes | High |

**Recommendation:** Start with Solution 2 (exponential backoff with `Retry-After` floor) for most agents — it's three lines of logic on top of standard retry. Add Solution 3 (per-provider state) when running multiple API keys or providers. Add Solution 4 (circuit breaker) for high-traffic services where a 429 storm can degrade the entire request pipeline.
