---
layout: solution
title: "Agent Doesn't Gracefully Degrade When Model Is Unavailable"
category: general
description: "When the primary model returns a 529 overloaded error or 503, the agent crashes or returns a generic error. Users lose their work, queued requests vanish, and the system has no fallback."
tags: [reliability, fallback, circuit-breaker, model-routing, error-handling, availability, overloaded]
---

## Symptom

Anthropic returns HTTP 529 (overloaded) or 503 during peak hours. Your agent raises an unhandled `anthropic.APIStatusError`, the request is lost, and the user sees a cryptic error. There is no fallback to a lighter model, no request queuing, and no graceful degradation to cached or partial responses.

## Root Cause

The agent uses a single hard-coded model with no fallback strategy. `anthropic.APIStatusError` with status 529 is treated the same as a bug — it propagates up and crashes the request handler. There is no model cascade, no circuit breaker for sustained outages, and no queue for requests that arrive during a temporary overload window.

## Fix

---

### Option 1: Model Cascade — Try Heavy, Fall Back to Light

Try the preferred model first. On 529/503, automatically retry with a cheaper, less-loaded alternative.

```python
import time
import anthropic

client = anthropic.Anthropic()

# Ordered from preferred to fallback
MODEL_CASCADE = [
    "claude-opus-4-6",
    "claude-sonnet-4-6",
    "claude-haiku-4-5-20251001",
]

FALLBACK_ERRORS = {529, 503, 502, 500}


def cascade_create(
    messages: list[dict],
    max_tokens: int = 1024,
    system: str = "",
    **kwargs,
) -> tuple[anthropic.types.Message, str]:
    """
    Try each model in cascade order.
    Returns (response, model_used).
    Raises if all models fail.
    """
    last_error = None

    for model in MODEL_CASCADE:
        try:
            create_kwargs = dict(
                model=model,
                max_tokens=max_tokens,
                messages=messages,
                **kwargs,
            )
            if system:
                create_kwargs["system"] = system

            response = client.messages.create(**create_kwargs)
            if model != MODEL_CASCADE[0]:
                print(f"  [Fallback] Using {model} (primary unavailable)")
            return response, model

        except anthropic.APIStatusError as e:
            if e.status_code in FALLBACK_ERRORS:
                print(f"  [Model unavailable] {model}: HTTP {e.status_code} — trying next")
                last_error = e
                time.sleep(0.5)  # brief pause before trying next model
                continue
            raise  # non-retryable error (auth, bad request, etc.)

        except anthropic.APIConnectionError as e:
            print(f"  [Connection error] {model}: {e} — trying next")
            last_error = e
            continue

    raise RuntimeError(f"All models in cascade failed. Last error: {last_error}")


def chat(user_message: str, system: str = "You are a helpful assistant.") -> str:
    messages = [{"role": "user", "content": user_message}]
    response, model_used = cascade_create(messages, max_tokens=1024, system=system)

    reply = response.content[0].text
    print(f"  [Responded with {model_used}]")
    return reply


# Usage
print(chat("Summarize the key benefits of async programming in Python."))
```

**Expected Token Savings**: Haiku fallback costs ~15x less than Opus. During outages, users still get responses rather than errors — zero lost sessions.
**Environment**: Synchronous Python. Add `max_tokens` scaling per model to stay within their limits.

---

### Option 2: Circuit Breaker with Automatic Recovery

Track failure rates per model. Open the circuit after N failures; try recovery after cooldown.

```python
import time
import threading
import anthropic
from dataclasses import dataclass, field
from enum import Enum

client = anthropic.Anthropic()


class CircuitState(Enum):
    CLOSED = "closed"       # Normal operation
    OPEN = "open"           # Refusing requests
    HALF_OPEN = "half_open" # Testing recovery


@dataclass
class ModelCircuitBreaker:
    model: str
    failure_threshold: int = 3
    recovery_timeout: float = 60.0  # seconds

    _state: CircuitState = field(default=CircuitState.CLOSED, init=False)
    _failures: int = field(default=0, init=False)
    _opened_at: float = field(default=0.0, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    @property
    def state(self) -> CircuitState:
        with self._lock:
            if self._state == CircuitState.OPEN:
                if time.monotonic() - self._opened_at >= self.recovery_timeout:
                    self._state = CircuitState.HALF_OPEN
            return self._state

    def is_available(self) -> bool:
        return self.state != CircuitState.OPEN

    def record_success(self):
        with self._lock:
            self._failures = 0
            self._state = CircuitState.CLOSED

    def record_failure(self):
        with self._lock:
            self._failures += 1
            if self._failures >= self.failure_threshold:
                self._state = CircuitState.OPEN
                self._opened_at = time.monotonic()
                print(f"  [Circuit OPEN] {self.model} — {self._failures} failures, cooling down {self.recovery_timeout}s")

    def status(self) -> str:
        return f"{self.model}: {self.state.value} ({self._failures} failures)"


class ResilientModelRouter:
    MODEL_PRIORITY = [
        "claude-sonnet-4-6",
        "claude-haiku-4-5-20251001",
    ]

    def __init__(self):
        self.breakers = {m: ModelCircuitBreaker(m) for m in self.MODEL_PRIORITY}

    def create(self, messages: list[dict], **kwargs) -> anthropic.types.Message:
        available = [m for m in self.MODEL_PRIORITY if self.breakers[m].is_available()]

        if not available:
            raise RuntimeError("All models are circuit-broken. Try again later.")

        for model in available:
            breaker = self.breakers[model]
            try:
                response = client.messages.create(
                    model=model,
                    messages=messages,
                    **kwargs,
                )
                breaker.record_success()
                return response

            except anthropic.APIStatusError as e:
                if e.status_code in (529, 503, 502):
                    breaker.record_failure()
                    print(f"  [Failure recorded] {model}: HTTP {e.status_code}")
                    continue
                raise

        raise RuntimeError("All available models failed")

    def print_status(self):
        for breaker in self.breakers.values():
            print(f"  {breaker.status()}")


router = ResilientModelRouter()


def resilient_chat(user_message: str) -> str:
    response = router.create(
        messages=[{"role": "user", "content": user_message}],
        max_tokens=512,
    )
    return response.content[0].text


# Usage
for i in range(3):
    try:
        reply = resilient_chat(f"Query {i}: What is 2+2?")
        print(f"[{i}] {reply}")
    except Exception as e:
        print(f"[{i}] ERROR: {e}")

router.print_status()
```

**Expected Token Savings**: Circuit opens after 3 failures, immediately routing to fallback without waiting for timeouts. Saves 3–10s latency per failed attempt.
**Environment**: Thread-safe circuit breaker. Single-process. For multi-process, move state to Redis.

---

### Option 3: Request Queue with Drain on Recovery

Buffer incoming requests during outages. Drain the queue when the model becomes available again.

```python
import asyncio
import time
import anthropic
from dataclasses import dataclass, field
from typing import Any

client = anthropic.AsyncAnthropic()


@dataclass
class QueuedRequest:
    request_id: str
    messages: list[dict]
    max_tokens: int
    future: asyncio.Future
    enqueued_at: float = field(default_factory=time.monotonic)


class ResilientQueuedAgent:
    """
    Buffers requests during model outages.
    Drains queue with exponential backoff on recovery.
    """
    MAX_QUEUE_SIZE = 50
    MAX_WAIT_SECONDS = 120

    def __init__(self):
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=self.MAX_QUEUE_SIZE)
        self._model = "claude-haiku-4-5-20251001"
        self._running = False
        self._backoff = 1.0

    async def start(self):
        self._running = True
        asyncio.create_task(self._drain_loop())

    async def submit(self, request_id: str, messages: list[dict], max_tokens: int = 512) -> str:
        """Submit a request. Blocks until processed or timeout."""
        future = asyncio.get_event_loop().create_future()
        req = QueuedRequest(request_id, messages, max_tokens, future)

        try:
            self._queue.put_nowait(req)
        except asyncio.QueueFull:
            return "Service at capacity. Please retry in a moment."

        try:
            return await asyncio.wait_for(future, timeout=self.MAX_WAIT_SECONDS)
        except asyncio.TimeoutError:
            return f"Request {request_id} timed out after {self.MAX_WAIT_SECONDS}s"

    async def _drain_loop(self):
        while self._running:
            try:
                req: QueuedRequest = await asyncio.wait_for(
                    self._queue.get(), timeout=1.0
                )
            except asyncio.TimeoutError:
                continue

            wait_time = time.monotonic() - req.enqueued_at
            if wait_time > self.MAX_WAIT_SECONDS:
                req.future.set_result(f"Request expired after {wait_time:.0f}s in queue")
                continue

            try:
                response = await client.messages.create(
                    model=self._model,
                    max_tokens=req.max_tokens,
                    messages=req.messages,
                )
                req.future.set_result(response.content[0].text)
                self._backoff = 1.0  # reset backoff on success

            except anthropic.APIStatusError as e:
                if e.status_code in (529, 503):
                    # Re-queue and back off
                    print(f"  [Overloaded] Backing off {self._backoff:.1f}s, re-queuing {req.request_id}")
                    await asyncio.sleep(self._backoff)
                    self._backoff = min(self._backoff * 2, 30.0)  # cap at 30s

                    try:
                        self._queue.put_nowait(req)
                    except asyncio.QueueFull:
                        req.future.set_result("Queue full during retry. Please try again.")
                else:
                    req.future.set_exception(e)

            except Exception as e:
                req.future.set_exception(e)


agent = ResilientQueuedAgent()


async def demo():
    await agent.start()

    # Simulate concurrent users during partial outage
    tasks = [
        agent.submit(f"req_{i}", [{"role": "user", "content": f"Tell me fact #{i} about Python."}])
        for i in range(5)
    ]
    results = await asyncio.gather(*tasks)
    for i, r in enumerate(results):
        print(f"[req_{i}] {r[:80]}")


asyncio.run(demo())
```

**Expected Token Savings**: No requests are lost during outages. Exponential backoff prevents thundering herd on recovery.
**Environment**: Async Python. Queue size and timeout are configurable.

---

### Option 4: Cached Response Fallback

On model unavailability, return a cached response for identical or semantically similar recent requests.

```python
import hashlib
import json
import time
import anthropic
from collections import OrderedDict

client = anthropic.Anthropic()

# LRU cache: {cache_key → (response_text, timestamp)}
RESPONSE_CACHE: OrderedDict[str, tuple[str, float]] = OrderedDict()
CACHE_MAX_SIZE = 100
CACHE_TTL_SECONDS = 300  # 5 minutes


def make_cache_key(messages: list[dict], model: str) -> str:
    content = json.dumps(messages, sort_keys=True) + model
    return hashlib.sha256(content.encode()).hexdigest()[:16]


def cache_get(key: str) -> str | None:
    if key in RESPONSE_CACHE:
        text, ts = RESPONSE_CACHE[key]
        if time.time() - ts < CACHE_TTL_SECONDS:
            RESPONSE_CACHE.move_to_end(key)  # LRU update
            return text
        else:
            del RESPONSE_CACHE[key]
    return None


def cache_set(key: str, text: str):
    if key in RESPONSE_CACHE:
        RESPONSE_CACHE.move_to_end(key)
    RESPONSE_CACHE[key] = (text, time.time())
    if len(RESPONSE_CACHE) > CACHE_MAX_SIZE:
        RESPONSE_CACHE.popitem(last=False)


def create_with_cache_fallback(
    messages: list[dict],
    model: str = "claude-haiku-4-5-20251001",
    max_tokens: int = 512,
) -> tuple[str, str]:
    """
    Returns (response_text, source).
    source: "live" | "cache" | "degraded"
    """
    key = make_cache_key(messages, model)

    try:
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            messages=messages,
        )
        text = response.content[0].text
        cache_set(key, text)
        return text, "live"

    except anthropic.APIStatusError as e:
        if e.status_code in (529, 503, 502):
            # Try cache first
            cached = cache_get(key)
            if cached:
                print(f"  [Cache hit] Returning cached response (model unavailable)")
                return f"[Cached response — model temporarily unavailable]\n\n{cached}", "cache"

            # No cache — return degraded response
            user_msg = messages[-1].get("content", "") if messages else ""
            degraded = (
                f"The AI service is temporarily overloaded. "
                f"Your request was: '{user_msg[:100]}'. "
                f"Please try again in a few minutes."
            )
            return degraded, "degraded"
        raise

    except anthropic.APIConnectionError:
        cached = cache_get(key)
        if cached:
            return f"[Offline cache]\n\n{cached}", "cache"
        return "Cannot reach the AI service. Please check your connection.", "degraded"


def chat(message: str) -> str:
    messages = [{"role": "user", "content": message}]
    text, source = create_with_cache_fallback(messages)
    if source != "live":
        print(f"  [Source: {source}]")
    return text


# Prime the cache
chat("What is Python?")
chat("How do async functions work?")

# These would return cached on model unavailability:
print(chat("What is Python?"))
print(chat("How do async functions work?"))
```

**Expected Token Savings**: Zero API cost for cache hits. Especially valuable for FAQ-style agent queries that repeat across users.
**Environment**: In-memory LRU cache. For persistence across restarts, replace with Redis or SQLite.

---

### Option 5: Degraded Mode with Static Fallback Responses

Define a set of canned responses for the most common query types. Serve them during extended outages.

```python
import re
import anthropic

client = anthropic.Anthropic()

# Static fallback responses for common query patterns
FALLBACK_PATTERNS = [
    (r"\b(hello|hi|hey|greet)\b", "Hello! I'm currently experiencing reduced capacity. I can still help with basic questions."),
    (r"\b(help|what can you do|capabilities)\b", "I can help with coding, writing, analysis, and Q&A. The AI service is temporarily reduced — some complex requests may be delayed."),
    (r"\b(status|down|error|broken|not working)\b", "The AI service is experiencing temporary high load. Simpler queries will be processed first. Please retry in a few minutes."),
    (r"\b(code|programming|python|javascript|function)\b", "I can help with code questions. The service is under high load — please try your specific question and I'll do my best."),
    (r"\b(cancel|stop|abort|exit)\b", "Understood. Your session has been noted."),
]

GENERIC_FALLBACK = (
    "I'm currently experiencing high load and cannot process complex requests. "
    "Please try again in a few minutes, or rephrase your request more simply."
)


def get_static_fallback(user_message: str) -> str:
    msg_lower = user_message.lower()
    for pattern, response in FALLBACK_PATTERNS:
        if re.search(pattern, msg_lower):
            return response
    return GENERIC_FALLBACK


def chat_with_static_fallback(
    user_message: str,
    model: str = "claude-haiku-4-5-20251001",
) -> tuple[str, bool]:
    """
    Returns (response, used_fallback).
    """
    try:
        response = client.messages.create(
            model=model,
            max_tokens=512,
            messages=[{"role": "user", "content": user_message}],
        )
        return response.content[0].text, False

    except anthropic.APIStatusError as e:
        if e.status_code in (529, 503):
            fallback = get_static_fallback(user_message)
            print(f"  [Static fallback] HTTP {e.status_code}")
            return fallback, True
        raise

    except anthropic.APIConnectionError:
        return get_static_fallback(user_message), True


# Usage
queries = [
    "Hello there!",
    "Can you help me write a Python function?",
    "Is the service working?",
    "What is machine learning?",
]

for query in queries:
    reply, degraded = chat_with_static_fallback(query)
    status = "[DEGRADED]" if degraded else "[LIVE]"
    print(f"{status} {query[:40]!r}")
    print(f"  → {reply[:100]}\n")
```

**Expected Token Savings**: Zero cost during outages. Static responses maintain user experience baseline without any API calls.
**Environment**: Zero dependencies. Pattern list is maintainable in a config file.

---

### Option 6: Observability-First Degradation with Alerting

Track availability metrics. Alert on degradation. Log every fallback for post-mortem analysis.

```python
import time
import json
import sqlite3
import threading
import anthropic
from datetime import datetime, timezone
from dataclasses import dataclass

client = anthropic.Anthropic()

# Metrics store
metrics_conn = sqlite3.connect("model_availability.db", check_same_thread=False)
metrics_conn.execute("""
    CREATE TABLE IF NOT EXISTS availability_log (
        ts           REAL,
        model        TEXT,
        status       TEXT,  -- success | overloaded | error | fallback
        latency_ms   REAL,
        http_code    INTEGER
    )
""")
metrics_conn.commit()
metrics_lock = threading.Lock()


def log_event(model: str, status: str, latency_ms: float, http_code: int = 0):
    with metrics_lock:
        metrics_conn.execute(
            "INSERT INTO availability_log (ts, model, status, latency_ms, http_code) VALUES (?,?,?,?,?)",
            (time.time(), model, status, latency_ms, http_code),
        )
        metrics_conn.commit()


def get_availability_report(window_minutes: int = 5) -> dict:
    cutoff = time.time() - window_minutes * 60
    rows = metrics_conn.execute(
        "SELECT model, status, COUNT(*) FROM availability_log WHERE ts > ? GROUP BY model, status",
        (cutoff,),
    ).fetchall()

    report = {}
    for model, status, count in rows:
        report.setdefault(model, {})[status] = count

    for model, stats in report.items():
        total = sum(stats.values())
        success = stats.get("success", 0)
        report[model]["availability_pct"] = round(success / total * 100, 1) if total else 0

    return report


def alert(message: str):
    """Hook this to PagerDuty/Slack/email in production."""
    print(f"\n🚨 ALERT: {message}")


PRIMARY_MODEL = "claude-sonnet-4-6"
FALLBACK_MODEL = "claude-haiku-4-5-20251001"
ALERT_THRESHOLD_FAILURES = 3

_consecutive_failures = 0


def monitored_create(messages: list[dict], max_tokens: int = 512) -> tuple[str, str]:
    """
    Returns (response_text, model_used).
    Logs every event and alerts on sustained failures.
    """
    global _consecutive_failures

    for model in [PRIMARY_MODEL, FALLBACK_MODEL]:
        start = time.monotonic()
        try:
            response = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                messages=messages,
            )
            latency = (time.monotonic() - start) * 1000
            log_event(model, "success", latency)
            _consecutive_failures = 0
            return response.content[0].text, model

        except anthropic.APIStatusError as e:
            latency = (time.monotonic() - start) * 1000
            status = "overloaded" if e.status_code in (529, 503) else "error"
            log_event(model, status, latency, e.status_code)

            _consecutive_failures += 1
            if _consecutive_failures >= ALERT_THRESHOLD_FAILURES:
                alert(
                    f"Model {model} has failed {_consecutive_failures} consecutive times "
                    f"(HTTP {e.status_code}). Check https://status.anthropic.com"
                )

            if model == PRIMARY_MODEL:
                print(f"  [Primary model {model} unavailable: HTTP {e.status_code}]")
                print(f"  [Falling back to {FALLBACK_MODEL}]")
                log_event(FALLBACK_MODEL, "fallback", 0)
                continue
            raise

    raise RuntimeError("All models failed")


def chat(user_message: str) -> str:
    text, model = monitored_create(
        [{"role": "user", "content": user_message}]
    )
    return text


# Usage + report
for i in range(3):
    try:
        reply = chat(f"Question {i}: What is {['Python', 'async', 'caching'][i]}?")
        print(f"[{i}] {reply[:80]}")
    except Exception as e:
        print(f"[{i}] FAILED: {e}")

print("\n--- Availability Report (last 5 min) ---")
report = get_availability_report(5)
print(json.dumps(report, indent=2))
```

**Expected Token Savings**: Metrics reveal when fallback models are over-used — actionable signal to increase capacity or adjust routing thresholds.
**Environment**: SQLite metrics log. Wire `alert()` to your on-call system. Minimal overhead (~1ms per event).

---

| Option | Strategy | Latency Added | State | Best For |
|--------|---------|--------------|-------|---------|
| 1 | Model cascade | ~500ms per fallback | None | Simple, no infrastructure |
| 2 | Circuit breaker | ~0ms (skips open circuit) | In-memory | High-throughput services |
| 3 | Request queue | Variable (queued wait) | In-memory | Bursty traffic, no drops |
| 4 | Cache fallback | ~0ms cache hit | LRU dict | Repeated/FAQ queries |
| 5 | Static fallback patterns | ~0ms | None | Full outages, offline mode |
| 6 | Observability + alerting | ~1ms logging | SQLite | Production monitoring |
