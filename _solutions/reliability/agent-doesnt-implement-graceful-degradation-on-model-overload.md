---
layout: solution
title: "Agent Doesn't Implement Graceful Degradation on Model Overload"
category: reliability
description: "When Claude returns 529 overloaded errors or high latency, agents should degrade gracefully — falling back to cached responses, simpler models, reduced functionality, or queued retries — rather than failing hard or timing out the user."
tags: [reliability, graceful-degradation, overload, fallback, resilience, 529]
---

## Problem

Claude's API can return 529 (Overloaded) errors during traffic spikes or return responses too slowly for latency-sensitive applications. Agents that treat these as fatal errors will fail users unnecessarily. A well-designed degradation strategy maintains partial service quality: serving cached answers, falling back to a smaller model, returning a partial response, or queuing the request for retry.

## Solutions

### Option 1: Cached Response Fallback

```python
import anthropic
import hashlib
import time
from dataclasses import dataclass, field
from typing import Optional

client = anthropic.Anthropic()

@dataclass
class CacheEntry:
    response: str
    created_at: float
    hit_count: int = 0
    ttl_seconds: float = 3600  # 1 hour default

    @property
    def is_expired(self) -> bool:
        return time.time() - self.created_at > self.ttl_seconds

# In-memory cache (use Redis in production)
_response_cache: dict[str, CacheEntry] = {}

def cache_key(system: str, messages: list[dict]) -> str:
    content = system + str(messages)
    return hashlib.sha256(content.encode()).hexdigest()[:16]

def get_cached(key: str) -> Optional[str]:
    entry = _response_cache.get(key)
    if entry and not entry.is_expired:
        entry.hit_count += 1
        return entry.response
    return None

def cache_response(key: str, response: str, ttl: float = 3600):
    _response_cache[key] = CacheEntry(response=response, created_at=time.time(), ttl_seconds=ttl)

def call_with_cache_fallback(
    system: str,
    messages: list[dict],
    max_tokens: int = 300,
    model: str = "claude-haiku-4-5-20251001"
) -> dict:
    key = cache_key(system, messages)

    try:
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=messages
        )
        text = response.content[0].text
        cache_response(key, text)  # Cache successful response
        return {"text": text, "source": "live", "model": model}

    except anthropic.APIStatusError as e:
        if e.status_code == 529:  # Overloaded
            cached = get_cached(key)
            if cached:
                print(f"[Degraded] API overloaded — serving cached response (key: {key})")
                return {"text": cached, "source": "cache", "model": model}
            else:
                return {
                    "text": "The service is temporarily at capacity. Please try again in a moment.",
                    "source": "fallback_message",
                    "model": model
                }
        raise

# Usage
system = "You are a helpful assistant."
messages = [{"role": "user", "content": "What are the benefits of async programming?"}]

# First call populates cache
result = call_with_cache_fallback(system, messages)
print(f"[{result['source']}] {result['text'][:200]}")

# Subsequent calls on overload would serve from cache
result2 = call_with_cache_fallback(system, messages)
print(f"[{result2['source']}] {result2['text'][:100]}")

# Expected Token Savings: 100% on cache hits; no API calls made
# Environment: ANTHROPIC_API_KEY required
```

### Option 2: Cascading Model Downgrade

```python
import anthropic
import time
from dataclasses import dataclass

client = anthropic.Anthropic()

# Models ordered from most to least capable/expensive
MODEL_CASCADE = [
    ("claude-opus-4-6",         {"quality": "highest", "latency": "high"}),
    ("claude-sonnet-4-6",       {"quality": "high",    "latency": "medium"}),
    ("claude-haiku-4-5-20251001", {"quality": "good",  "latency": "low"}),
]

@dataclass
class CascadeResult:
    text: str
    model_used: str
    quality_level: str
    attempts: int
    degraded: bool

def call_with_cascade(
    messages: list[dict],
    system: str = "",
    max_tokens: int = 500,
    preferred_model: str = "claude-opus-4-6",
    retry_delay: float = 0.5
) -> CascadeResult:
    """
    Try preferred model first, cascade down on overload.
    """
    # Start from preferred model in cascade
    start_index = next(
        (i for i, (m, _) in enumerate(MODEL_CASCADE) if m == preferred_model),
        0
    )

    for attempt, (model, meta) in enumerate(MODEL_CASCADE[start_index:], 1):
        try:
            response = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=messages
            )
            return CascadeResult(
                text=response.content[0].text,
                model_used=model,
                quality_level=meta["quality"],
                attempts=attempt,
                degraded=(attempt > 1)
            )

        except anthropic.APIStatusError as e:
            if e.status_code in (529, 529):  # Overloaded
                if attempt <= len(MODEL_CASCADE) - start_index:
                    print(f"[Cascade] {model} overloaded, downgrading... (attempt {attempt})")
                    time.sleep(retry_delay)
                    continue
            raise  # Non-overload errors propagate immediately

        except anthropic.APIConnectionError:
            if attempt <= len(MODEL_CASCADE) - start_index:
                print(f"[Cascade] {model} unreachable, trying next model")
                continue
            raise

    # All models exhausted
    return CascadeResult(
        text="All model tiers are currently overloaded. Please retry shortly.",
        model_used="none",
        quality_level="none",
        attempts=len(MODEL_CASCADE) - start_index,
        degraded=True
    )

# Usage
result = call_with_cascade(
    messages=[{"role": "user", "content": "Explain the CAP theorem in distributed systems."}],
    system="You are a computer science professor.",
    preferred_model="claude-opus-4-6"
)

print(f"Model: {result.model_used} (quality: {result.quality_level})")
print(f"Degraded: {result.degraded} | Attempts: {result.attempts}")
print(f"Response: {result.text[:300]}")

# Expected Token Savings: Same tokens, but ensures response delivery even under load
# Environment: ANTHROPIC_API_KEY required; cascade incurs minimal overhead on happy path
```

### Option 3: Partial Response Streaming with Timeout

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass
from typing import Optional

client = anthropic.AsyncAnthropic()

@dataclass
class PartialResult:
    text: str
    is_complete: bool
    chars_received: int
    elapsed_ms: float
    timeout_triggered: bool
    graceful_stop_reason: str

async def stream_with_timeout(
    messages: list[dict],
    system: str = "",
    max_tokens: int = 500,
    model: str = "claude-haiku-4-5-20251001",
    soft_timeout_s: float = 5.0,   # Return partial after this
    hard_timeout_s: float = 10.0   # Abort entirely after this
) -> PartialResult:
    """
    Stream response, returning partial content if soft_timeout exceeded.
    Aborts completely at hard_timeout.
    """
    collected = []
    is_complete = False
    timeout_triggered = False
    graceful_stop = "completed"
    start = time.time()

    try:
        async with client.messages.stream(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=messages
        ) as stream:
            async for text_chunk in stream.text_stream:
                collected.append(text_chunk)
                elapsed = time.time() - start

                if elapsed > hard_timeout_s:
                    graceful_stop = f"hard_timeout ({hard_timeout_s}s)"
                    timeout_triggered = True
                    break

                if elapsed > soft_timeout_s and len(collected) > 10:
                    # We have enough for a partial response
                    graceful_stop = f"soft_timeout ({soft_timeout_s}s)"
                    timeout_triggered = True
                    # Add truncation indicator
                    collected.append(" [response truncated — full response available on retry]")
                    break

            else:
                is_complete = True  # Stream completed normally

    except anthropic.APIStatusError as e:
        if e.status_code == 529:
            return PartialResult(
                text="Service temporarily at capacity. Partial response unavailable.",
                is_complete=False,
                chars_received=0,
                elapsed_ms=(time.time() - start) * 1000,
                timeout_triggered=True,
                graceful_stop_reason="overloaded_529"
            )
        raise
    except asyncio.TimeoutError:
        graceful_stop = "asyncio_timeout"
        timeout_triggered = True

    full_text = "".join(collected)
    return PartialResult(
        text=full_text,
        is_complete=is_complete,
        chars_received=len(full_text),
        elapsed_ms=(time.time() - start) * 1000,
        timeout_triggered=timeout_triggered,
        graceful_stop_reason=graceful_stop
    )

async def main():
    result = await stream_with_timeout(
        messages=[{"role": "user", "content": "Write a detailed explanation of how neural networks learn."}],
        system="You are an AI educator.",
        soft_timeout_s=3.0,
        hard_timeout_s=8.0
    )

    print(f"Complete: {result.is_complete} | Timeout: {result.timeout_triggered}")
    print(f"Chars: {result.chars_received} | Elapsed: {result.elapsed_ms:.0f}ms")
    print(f"Stop reason: {result.graceful_stop_reason}")
    print(f"Text: {result.text[:400]}")

asyncio.run(main())

# Expected Token Savings: Partial responses avoid full token spend on slow responses
# Environment: ANTHROPIC_API_KEY required, uses asyncio streaming
```

### Option 4: Request Queue with Backpressure

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass, field
from typing import Optional
from asyncio import PriorityQueue

client = anthropic.AsyncAnthropic()

@dataclass(order=True)
class QueuedRequest:
    priority: int  # Lower = higher priority
    created_at: float = field(compare=False, default_factory=time.time)
    request_id: str = field(compare=False, default="")
    messages: list[dict] = field(compare=False, default_factory=list)
    system: str = field(compare=False, default="")
    max_tokens: int = field(compare=False, default=300)
    result_future: Optional[asyncio.Future] = field(compare=False, default=None)

class DegradingRequestQueue:
    def __init__(self, max_queue_size: int = 50, worker_count: int = 3):
        self.queue: PriorityQueue = PriorityQueue(maxsize=max_queue_size)
        self.worker_count = worker_count
        self._overload_count = 0
        self._success_count = 0
        self._model = "claude-haiku-4-5-20251001"

    @property
    def overload_rate(self) -> float:
        total = self._overload_count + self._success_count
        return self._overload_count / total if total > 0 else 0.0

    def _select_model(self) -> str:
        """Downgrade model when overload rate is high."""
        if self.overload_rate > 0.5:
            return "claude-haiku-4-5-20251001"
        return self._model

    async def _worker(self):
        """Process queued requests with backoff on overload."""
        backoff = 0.5
        while True:
            req: QueuedRequest = await self.queue.get()

            # Check if request has been waiting too long
            wait_time = time.time() - req.created_at
            if wait_time > 30:  # Request expired
                req.result_future.set_result({
                    "text": "Request expired in queue (waited >30s).",
                    "source": "expired"
                })
                self.queue.task_done()
                continue

            model = self._select_model()
            try:
                response = await client.messages.create(
                    model=model,
                    max_tokens=req.max_tokens,
                    system=req.system,
                    messages=req.messages
                )
                self._success_count += 1
                backoff = 0.5  # Reset backoff on success
                req.result_future.set_result({
                    "text": response.content[0].text,
                    "source": "live",
                    "model": model,
                    "queue_wait_ms": wait_time * 1000
                })

            except anthropic.APIStatusError as e:
                if e.status_code == 529:
                    self._overload_count += 1
                    print(f"[Queue] Overload (rate: {self.overload_rate:.0%}), backing off {backoff:.1f}s")
                    # Re-queue with increased backoff
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30)
                    await self.queue.put(req)  # Re-queue
                else:
                    req.result_future.set_exception(e)
            except Exception as e:
                req.result_future.set_exception(e)
            finally:
                self.queue.task_done()

    async def start(self):
        for _ in range(self.worker_count):
            asyncio.create_task(self._worker())

    async def submit(self, messages: list[dict], system: str = "", priority: int = 5) -> dict:
        loop = asyncio.get_event_loop()
        future = loop.create_future()
        req = QueuedRequest(
            priority=priority,
            request_id=f"req-{int(time.time()*1000)}",
            messages=messages,
            system=system,
            result_future=future
        )
        try:
            self.queue.put_nowait(req)
        except asyncio.QueueFull:
            return {"text": "Service at capacity — queue full. Try again shortly.", "source": "rejected"}

        return await future

async def main():
    queue = DegradingRequestQueue(max_queue_size=20, worker_count=2)
    await queue.start()

    # Submit multiple concurrent requests
    tasks = [
        queue.submit([{"role": "user", "content": f"What is {i}+{i*2}?"}], priority=i % 3)
        for i in range(5)
    ]
    results = await asyncio.gather(*tasks)

    for i, r in enumerate(results):
        print(f"[{i}] {r.get('source')} | {r.get('text', '')[:80]}")

asyncio.run(main())

# Expected Token Savings: Queue absorbs bursts; degraded model reduces per-request cost 80%
# Environment: ANTHROPIC_API_KEY required, uses asyncio PriorityQueue
```

### Option 5: Circuit Breaker with Reduced Functionality Mode

```python
import anthropic
import time
from dataclasses import dataclass, field
from enum import Enum

client = anthropic.Anthropic()

class CircuitState(str, Enum):
    CLOSED = "closed"        # Normal operation
    OPEN = "open"            # Blocking all requests
    HALF_OPEN = "half_open"  # Testing recovery

@dataclass
class CircuitBreaker:
    failure_threshold: int = 5      # Failures before opening
    recovery_timeout: float = 60.0  # Seconds before trying again
    success_threshold: int = 2      # Successes to close from half-open

    _state: CircuitState = field(default=CircuitState.CLOSED, init=False)
    _failure_count: int = field(default=0, init=False)
    _success_count: int = field(default=0, init=False)
    _last_failure_time: float = field(default=0.0, init=False)
    _total_opens: int = field(default=0, init=False)

    @property
    def state(self) -> CircuitState:
        if self._state == CircuitState.OPEN:
            if time.time() - self._last_failure_time > self.recovery_timeout:
                self._state = CircuitState.HALF_OPEN
                self._success_count = 0
                print(f"[Circuit] OPEN → HALF_OPEN (testing recovery)")
        return self._state

    def record_success(self):
        self._failure_count = 0
        if self._state == CircuitState.HALF_OPEN:
            self._success_count += 1
            if self._success_count >= self.success_threshold:
                self._state = CircuitState.CLOSED
                print(f"[Circuit] HALF_OPEN → CLOSED (recovered)")

    def record_failure(self):
        self._failure_count += 1
        self._last_failure_time = time.time()
        if self._failure_count >= self.failure_threshold:
            if self._state != CircuitState.OPEN:
                self._state = CircuitState.OPEN
                self._total_opens += 1
                print(f"[Circuit] → OPEN (failures: {self._failure_count}, total opens: {self._total_opens})")

REDUCED_RESPONSES = {
    "greeting": "Hello! How can I help you today?",
    "status": "Service is running in reduced capacity mode. Complex requests may be delayed.",
    "default": "I'm currently operating in reduced mode. Please try again shortly for full responses."
}

circuit = CircuitBreaker(failure_threshold=3, recovery_timeout=30.0)

def call_with_circuit_breaker(
    messages: list[dict],
    system: str = "",
    max_tokens: int = 300,
    model: str = "claude-haiku-4-5-20251001"
) -> dict:
    state = circuit.state

    if state == CircuitState.OPEN:
        # Serve reduced functionality
        user_text = messages[-1]["content"].lower() if messages else ""
        if "hello" in user_text or "hi " in user_text:
            fallback = REDUCED_RESPONSES["greeting"]
        elif "status" in user_text:
            fallback = REDUCED_RESPONSES["status"]
        else:
            fallback = REDUCED_RESPONSES["default"]

        print(f"[Circuit] OPEN — serving reduced response")
        return {"text": fallback, "source": "circuit_open", "state": state.value}

    try:
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=messages
        )
        circuit.record_success()
        return {
            "text": response.content[0].text,
            "source": "live",
            "state": state.value
        }

    except anthropic.APIStatusError as e:
        if e.status_code == 529:
            circuit.record_failure()
            return {
                "text": REDUCED_RESPONSES["default"],
                "source": "circuit_tripped",
                "state": circuit.state.value
            }
        raise

# Simulate overload scenario
for i in range(8):
    result = call_with_circuit_breaker(
        messages=[{"role": "user", "content": f"Request {i}: What is the weather like?"}]
    )
    print(f"[{i}] [{result['state']}] {result['source']}: {result['text'][:60]}")

# Expected Token Savings: 100% during OPEN state; prevents cascading failures
# Environment: ANTHROPIC_API_KEY required
```

### Option 6: Async Hedged Requests (Speculative Reliability)

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass

client = anthropic.AsyncAnthropic()

@dataclass
class HedgedResult:
    text: str
    winning_model: str
    total_attempts: int
    response_time_ms: float
    cancelled_requests: int
    was_hedged: bool

async def call_model(model: str, messages: list[dict], system: str, max_tokens: int) -> tuple[str, str]:
    """Return (model, response_text) on success."""
    response = await client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=messages
    )
    return model, response.content[0].text

async def hedged_request(
    messages: list[dict],
    system: str = "",
    max_tokens: int = 300,
    primary_model: str = "claude-sonnet-4-6",
    hedge_model: str = "claude-haiku-4-5-20251001",
    hedge_delay_s: float = 1.5  # Launch hedge after this delay if primary hasn't responded
) -> HedgedResult:
    """
    Send primary request, launch fallback after hedge_delay if no response.
    First to respond wins; other is cancelled.
    """
    start = time.time()
    tasks: list[asyncio.Task] = []
    cancelled = 0

    async def launch_after_delay(delay: float, model: str):
        await asyncio.sleep(delay)
        return await call_model(model, messages, system, max_tokens)

    # Primary request starts immediately
    primary_task = asyncio.create_task(
        call_model(primary_model, messages, system, max_tokens)
    )
    tasks.append(primary_task)

    # Hedge request starts after delay
    hedge_task = asyncio.create_task(
        launch_after_delay(hedge_delay_s, hedge_model)
    )
    tasks.append(hedge_task)

    was_hedged = False
    winning_model = primary_model
    text = ""

    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        winner = done.pop()
        winning_model, text = winner.result()
        was_hedged = (winning_model == hedge_model)

        # Cancel remaining tasks
        for task in pending:
            task.cancel()
            cancelled += 1
        # Await cancellations
        await asyncio.gather(*pending, return_exceptions=True)

    except Exception as e:
        # If primary fails, hedge may succeed
        for task in tasks:
            if not task.done():
                try:
                    winning_model, text = await task
                    break
                except Exception:
                    pass
        if not text:
            text = "All requests failed. Please retry."
            winning_model = "none"

    return HedgedResult(
        text=text,
        winning_model=winning_model,
        total_attempts=len(tasks),
        response_time_ms=(time.time() - start) * 1000,
        cancelled_requests=cancelled,
        was_hedged=was_hedged
    )

async def main():
    result = await hedged_request(
        messages=[{"role": "user", "content": "Summarize the benefits of microservices architecture in 3 bullet points."}],
        primary_model="claude-sonnet-4-6",
        hedge_model="claude-haiku-4-5-20251001",
        hedge_delay_s=2.0
    )

    print(f"Winner: {result.winning_model} (hedged: {result.was_hedged})")
    print(f"Time: {result.response_time_ms:.0f}ms | Cancelled: {result.cancelled_requests}")
    print(f"Response: {result.text[:300]}")

asyncio.run(main())

# Expected Token Savings: Cancels losing request; hedging costs 0 if primary wins within hedge_delay
# Environment: ANTHROPIC_API_KEY required, uses asyncio
```

## Comparison

| Option | Recovery Strategy | Latency Impact | User Experience | Best Use Case |
|--------|------------------|----------------|-----------------|---------------|
| Cached Response Fallback | Serve stale data | None | Transparent | Read-heavy, idempotent queries |
| Cascading Model Downgrade | Smaller model | +retry delay | Lower quality | Quality-adjustable workloads |
| Partial Streaming + Timeout | Return what arrived | Bounded | Truncated result | UX-sensitive real-time features |
| Request Queue + Backpressure | Delay and retry | +queue wait | Eventual consistency | Batch/async workflows |
| Circuit Breaker | Fixed fallback text | None | Degraded mode | High-traffic APIs, chat systems |
| Hedged Requests | Race two models | Reduced P99 | Transparent | Latency-critical production APIs |
