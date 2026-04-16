---
title: "Agent Doesn't Implement Fallback Response When All Models Fail"
slug: agent-doesnt-implement-fallback-response-when-all-models-fail
category: reliability
tags: [reliability, fallback, degraded-mode, error-handling, anthropic-sdk, resilience]
description: >
  When the Anthropic API is unavailable, rate-limited, or returns repeated
  errors, the agent surfaces a raw exception to the user instead of a graceful
  degraded-mode response. Without a fallback strategy the service appears
  completely broken even when partial functionality is possible.
symptoms:
  - Users see "500 Internal Server Error" or raw stack traces when the API is down
  - A brief API outage causes complete service unavailability for minutes
  - No static or cached fallback keeps the UI functional during degraded mode
  - No alerting fires when the fallback activates — the team finds out from users
related_solutions:
  - agent-doesnt-implement-multi-region-failover-for-api-calls
  - agent-doesnt-implement-circuit-breaker-per-downstream-dependency
  - agent-doesnt-implement-response-validation-with-retry-on-malformed-output
---

## Problem

Even with retries and circuit breakers there are scenarios — sustained outage,
total rate-limit exhaustion, account suspension — where every model call fails.
At that point the agent needs a pre-planned response strategy: return a cached
answer if one exists, serve a static canned response, let the user know
their request is queued for later, or degrade to a rule-based no-LLM path.
Failing silently or surfacing a raw error is never acceptable.

---

## Solution 1 — Static Fallback Message Registry

Maintain a registry of static fallback messages keyed by intent category.
When all models fail, classify the user's query using simple keyword matching
and return the closest static answer.

```python
import anthropic
import asyncio
import re
from dataclasses import dataclass


FALLBACK_REGISTRY: dict[str, str] = {
    "greeting": "Hello! I'm temporarily experiencing technical difficulties. Please try again in a few minutes.",
    "pricing":  "Our pricing starts at $29/month. Visit our pricing page or contact sales@example.com for details.",
    "support":  "For urgent support please email support@example.com or call +1-800-555-0100. We aim to respond within 2 hours.",
    "account":  "For account-related issues please visit account.example.com or contact our support team.",
    "default":  "I'm temporarily unavailable due to a technical issue. Please try again shortly or contact support@example.com.",
}

INTENT_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\b(hello|hi|hey|good morning|greet)\b", re.I), "greeting"),
    (re.compile(r"\b(price|cost|plan|subscription|billing)\b", re.I), "pricing"),
    (re.compile(r"\b(help|support|issue|problem|bug|error)\b", re.I), "support"),
    (re.compile(r"\b(account|login|password|profile|settings)\b", re.I), "account"),
]


def classify_intent(text: str) -> str:
    for pattern, intent in INTENT_PATTERNS:
        if pattern.search(text):
            return intent
    return "default"


def static_fallback(user_message: str) -> str:
    intent = classify_intent(user_message)
    return FALLBACK_REGISTRY[intent]


async def resilient_create(
    messages: list,
    model: str = "claude-sonnet-4-6",
    max_retries: int = 2,
) -> str:
    client = anthropic.AsyncAnthropic()
    last_error: Exception | None = None

    for attempt in range(max_retries + 1):
        try:
            resp = await client.messages.create(
                model=model, max_tokens=512, messages=messages
            )
            return resp.content[0].text
        except (anthropic.APIStatusError, anthropic.APIConnectionError,
                anthropic.RateLimitError) as e:
            last_error = e
            print(f"[fallback] attempt {attempt} failed: {type(e).__name__}")
            if attempt < max_retries:
                await asyncio.sleep(2 ** attempt)

    # All attempts failed — use static fallback
    user_text = next(
        (m["content"] for m in reversed(messages)
         if m.get("role") == "user" and isinstance(m.get("content"), str)),
        "",
    )
    fallback = static_fallback(user_text)
    print(f"[fallback] serving static response  intent={classify_intent(user_text)}")
    return fallback


async def demo():
    queries = [
        "Hi there! How are you?",
        "What are your pricing plans?",
        "I have a bug in my integration.",
    ]
    for q in queries:
        result = await resilient_create([{"role": "user", "content": q}])
        print(f"Q: {q[:40]:40s}  -> {result[:60]}")


asyncio.run(demo())
```

---

## Solution 2 — Response Cache Fallback (Stale-While-Error)

Cache every successful LLM response with a TTL. On failure, serve the stale
cached response for the same (or semantically similar) query with a disclaimer
banner instead of an error.

```python
import anthropic
import asyncio
import hashlib
import time
from dataclasses import dataclass, field


@dataclass
class CacheEntry:
    text:       str
    created_at: float
    query_hash: str
    model:      str


_cache: dict[str, CacheEntry] = {}
CACHE_TTL = 3600   # 1 hour live TTL
STALE_TTL = 86400  # serve stale for up to 24 h on error

STALE_DISCLAIMER = "\n\n---\n*Note: This response was cached from an earlier session. Live AI is temporarily unavailable.*"


def _query_hash(messages: list) -> str:
    payload = str([(m["role"], m.get("content", "")) for m in messages])
    return hashlib.md5(payload.encode()).hexdigest()


def _get_fresh(key: str) -> CacheEntry | None:
    entry = _cache.get(key)
    if entry and time.time() - entry.created_at < CACHE_TTL:
        return entry
    return None


def _get_stale(key: str) -> CacheEntry | None:
    entry = _cache.get(key)
    if entry and time.time() - entry.created_at < STALE_TTL:
        return entry
    return None


def _store(key: str, text: str, messages: list, model: str) -> None:
    _cache[key] = CacheEntry(text=text, created_at=time.time(),
                              query_hash=key, model=model)


async def cache_fallback_create(
    messages: list,
    model: str = "claude-sonnet-4-6",
) -> tuple[str, str]:
    """Returns (text, source) where source is 'live' | 'cache_fresh' | 'cache_stale' | 'error'."""
    key = _query_hash(messages)

    # Check fresh cache first
    fresh = _get_fresh(key)
    if fresh:
        return fresh.text, "cache_fresh"

    client = anthropic.AsyncAnthropic()
    try:
        resp = await client.messages.create(
            model=model, max_tokens=512, messages=messages
        )
        text = resp.content[0].text
        _store(key, text, messages, model)
        return text, "live"
    except Exception as e:
        print(f"[cache-fallback] live call failed: {e}")

    # Serve stale cache with disclaimer
    stale = _get_stale(key)
    if stale:
        age_min = (time.time() - stale.created_at) / 60
        print(f"[cache-fallback] serving stale cache  age={age_min:.0f}min")
        return stale.text + STALE_DISCLAIMER, "cache_stale"

    # No cache at all
    return "I'm temporarily unavailable. Please try again shortly.", "error"


async def demo():
    messages = [{"role": "user", "content": "What is eventual consistency?"}]

    # First call: live
    text, source = await cache_fallback_create(messages)
    print(f"[{source}] {text[:60]}")

    # Second call: fresh cache
    text, source = await cache_fallback_create(messages)
    print(f"[{source}] {text[:60]}")


asyncio.run(demo())
```

---

## Solution 3 — Rule-Based No-LLM Degraded Mode

When all LLM calls fail, switch to a deterministic rule engine that handles
the most common intents without any AI — FAQ lookup, regex extraction, template
fill. This keeps the most-used paths working at 100 % even during a full outage.

```python
import anthropic
import asyncio
import re
from dataclasses import dataclass


FAQ_DB: dict[str, str] = {
    "reset password":      "Go to account.example.com/reset and enter your email address.",
    "cancel subscription": "Visit billing.example.com/cancel or email billing@example.com.",
    "export data":         "Data export is available under Settings > Privacy > Export Data.",
    "contact support":     "Email support@example.com or call +1-800-555-0100 (Mon-Fri 9am-6pm ET).",
    "api key":             "API keys are managed at platform.example.com/settings/api.",
}

EXTRACTION_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"(reset|forgot|lost).{0,10}password", re.I), "reset password"),
    (re.compile(r"(cancel|stop|end).{0,10}subscription", re.I), "cancel subscription"),
    (re.compile(r"export.{0,10}(data|account|history)", re.I), "export data"),
    (re.compile(r"(contact|reach|talk to).{0,10}support", re.I), "contact support"),
    (re.compile(r"api.{0,10}key", re.I), "api key"),
]


def rule_engine_respond(user_text: str) -> str | None:
    for pattern, faq_key in EXTRACTION_RULES:
        if pattern.search(user_text):
            return FAQ_DB[faq_key]
    return None


DEGRADED_DEFAULT = (
    "Our AI assistant is temporarily offline. "
    "For common questions:\n"
    "• Password reset: account.example.com/reset\n"
    "• Billing issues: billing.example.com\n"
    "• Technical support: support@example.com\n"
    "• Documentation: docs.example.com"
)


async def degraded_mode_create(
    messages: list,
    model: str = "claude-sonnet-4-6",
    max_retries: int = 1,
) -> tuple[str, bool]:
    """Returns (response_text, used_ai)."""
    client = anthropic.AsyncAnthropic()

    for attempt in range(max_retries + 1):
        try:
            resp = await client.messages.create(
                model=model, max_tokens=512, messages=messages
            )
            return resp.content[0].text, True
        except Exception as e:
            print(f"[degraded] attempt {attempt}: {type(e).__name__}")
            if attempt < max_retries:
                await asyncio.sleep(1)

    # Try rule engine
    user_text = next(
        (m["content"] for m in reversed(messages)
         if m.get("role") == "user" and isinstance(m.get("content"), str)),
        "",
    )
    rule_response = rule_engine_respond(user_text)
    if rule_response:
        print("[degraded] rule engine matched")
        return rule_response, False

    print("[degraded] serving static degraded message")
    return DEGRADED_DEFAULT, False


async def demo():
    cases = [
        "How do I reset my password?",
        "I want to cancel my subscription.",
        "What's the weather like?",
    ]
    for q in cases:
        text, used_ai = await degraded_mode_create([{"role": "user", "content": q}])
        source = "AI" if used_ai else "rules"
        print(f"[{source}] {q[:35]:35s} -> {text[:60]}")


asyncio.run(demo())
```

---

## Solution 4 — Request Queuing for Deferred Fulfillment

When all models fail, queue the user's request with a unique ID and a webhook
URL. A background worker retries the queue when the API recovers and delivers
the result asynchronously.

```python
import anthropic
import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field


@dataclass
class QueuedRequest:
    request_id:   str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    messages:     list = field(default_factory=list)
    model:        str  = "claude-sonnet-4-6"
    queued_at:    float = field(default_factory=time.time)
    webhook_url:  str  = ""
    attempts:     int  = 0
    result:       str | None = None
    completed_at: float | None = None


# In-memory queue — replace with Redis/SQS in production
_queue: asyncio.Queue = asyncio.Queue()
_results: dict[str, QueuedRequest] = {}


async def enqueue_request(
    messages: list,
    webhook_url: str = "",
    model: str = "claude-sonnet-4-6",
) -> str:
    req = QueuedRequest(messages=messages, model=model, webhook_url=webhook_url)
    _results[req.request_id] = req
    await _queue.put(req)
    print(f"[queue] enqueued request_id={req.request_id}")
    return req.request_id


async def _queue_worker() -> None:
    client = anthropic.AsyncAnthropic()
    while True:
        req = await _queue.get()
        req.attempts += 1
        try:
            resp = await client.messages.create(
                model=req.model, max_tokens=512, messages=req.messages
            )
            req.result = resp.content[0].text
            req.completed_at = time.time()
            elapsed = req.completed_at - req.queued_at
            print(f"[queue-worker] request_id={req.request_id}  elapsed={elapsed:.1f}s")
            if req.webhook_url:
                print(f"[queue-worker] POSTing result to {req.webhook_url}")
        except Exception as e:
            print(f"[queue-worker] failed attempt {req.attempts}: {e}")
            if req.attempts < 5:
                await asyncio.sleep(min(60, 2 ** req.attempts))
                await _queue.put(req)
        finally:
            _queue.task_done()


async def resilient_or_queue(
    messages: list,
    model: str = "claude-sonnet-4-6",
    webhook_url: str = "",
) -> dict:
    client = anthropic.AsyncAnthropic()
    try:
        resp = await asyncio.wait_for(
            client.messages.create(model=model, max_tokens=512, messages=messages),
            timeout=10.0,
        )
        return {"status": "ok", "text": resp.content[0].text}
    except Exception as e:
        print(f"[resilient-or-queue] live call failed ({type(e).__name__}) — queuing")
        request_id = await enqueue_request(messages, webhook_url, model)
        return {
            "status":     "queued",
            "request_id": request_id,
            "message":    (
                f"Your request is queued (ID: {request_id}). "
                f"We'll {'notify you via webhook' if webhook_url else 'process it'} when the service recovers."
            ),
        }


async def demo_queue():
    # Start background worker
    asyncio.create_task(_queue_worker())

    result = await resilient_or_queue(
        [{"role": "user", "content": "Explain distributed transactions."}],
        webhook_url="https://example.com/webhook",
    )
    print(f"Immediate response: {json.dumps(result, indent=2)}")

    if result["status"] == "queued":
        # Poll for result (in real life: use webhook)
        rid = result["request_id"]
        for _ in range(15):
            await asyncio.sleep(1)
            req = _results.get(rid)
            if req and req.result:
                print(f"Deferred result: {req.result[:80]}")
                break


asyncio.run(demo_queue())
```

---

## Solution 5 — Graceful Degradation with Feature Flags

Use feature flags to disable LLM-dependent features progressively as error
rates rise. At 50 % errors disable suggestions; at 80 % disable generation;
at 100 % show maintenance mode. This prevents cascading user-visible failures.

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass, field
from collections import deque
from enum import Enum


class DegradationLevel(Enum):
    FULL         = "full"        # all features on
    REDUCED      = "reduced"     # suggestions off
    MINIMAL      = "minimal"     # generation off, search only
    MAINTENANCE  = "maintenance" # static page only


@dataclass
class ErrorRateTracker:
    window_size: int = 50
    _outcomes: deque = field(default_factory=deque)

    def record(self, success: bool) -> None:
        self._outcomes.append(1 if success else 0)
        if len(self._outcomes) > self.window_size:
            self._outcomes.popleft()

    @property
    def error_rate(self) -> float:
        if not self._outcomes:
            return 0.0
        return 1.0 - sum(self._outcomes) / len(self._outcomes)

    @property
    def level(self) -> DegradationLevel:
        r = self.error_rate
        if r >= 0.99:
            return DegradationLevel.MAINTENANCE
        if r >= 0.80:
            return DegradationLevel.MINIMAL
        if r >= 0.50:
            return DegradationLevel.REDUCED
        return DegradationLevel.FULL


_tracker = ErrorRateTracker()
_last_level = DegradationLevel.FULL


MAINTENANCE_MESSAGE = (
    "We're experiencing a service outage. "
    "Please visit status.example.com for updates. "
    "Estimated recovery: within 30 minutes."
)

MINIMAL_MESSAGE = (
    "AI generation is temporarily disabled. "
    "Search and FAQ lookup are still available."
)


async def feature_flag_create(
    messages: list,
    model: str = "claude-sonnet-4-6",
    feature: str = "chat",
) -> tuple[str, DegradationLevel]:
    """Returns (text, degradation_level)."""
    global _last_level
    level = _tracker.level

    if level != _last_level:
        print(f"[degradation] level changed: {_last_level.value} -> {level.value}  error_rate={_tracker.error_rate:.0%}")
        _last_level = level

    if level == DegradationLevel.MAINTENANCE:
        return MAINTENANCE_MESSAGE, level

    if level == DegradationLevel.MINIMAL and feature in ("suggest", "generate"):
        return MINIMAL_MESSAGE, level

    client = anthropic.AsyncAnthropic()
    try:
        resp = await client.messages.create(
            model=model, max_tokens=512, messages=messages
        )
        _tracker.record(True)
        return resp.content[0].text, level
    except Exception as e:
        _tracker.record(False)
        print(f"[feature-flag] error: {type(e).__name__}  error_rate={_tracker.error_rate:.0%}")
        # Recurse: level may have just changed
        new_level = _tracker.level
        if new_level == DegradationLevel.MAINTENANCE:
            return MAINTENANCE_MESSAGE, new_level
        return f"This feature is temporarily unavailable. Error rate: {_tracker.error_rate:.0%}", new_level


async def demo_feature_flags():
    # Inject some failures to trigger degradation
    for _ in range(35):
        _tracker.record(False)

    queries = [
        ("What is a load balancer?", "chat"),
        ("Suggest improvements to my code.", "suggest"),
        ("Generate a report.", "generate"),
    ]
    for q, feature in queries:
        text, level = await feature_flag_create(
            [{"role": "user", "content": q}], feature=feature
        )
        print(f"[{level.value}] [{feature}] {q[:35]:35s} -> {text[:60]}")


asyncio.run(demo_feature_flags())
```

---

## Solution 6 — Multi-Provider Fallback Chain

Fall through a chain of providers: Anthropic primary → secondary Anthropic
region → open-source model endpoint → static fallback. Each step is tried
only if the previous one fails.

```python
import anthropic
import asyncio
import os
import time
from dataclasses import dataclass
from typing import Callable, Awaitable


@dataclass
class Provider:
    name:    str
    call_fn: Callable[[list], Awaitable[str]]
    timeout: float = 15.0


async def _anthropic_primary(messages: list) -> str:
    client = anthropic.AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    resp = await client.messages.create(
        model="claude-sonnet-4-6", max_tokens=512, messages=messages
    )
    return resp.content[0].text


async def _anthropic_haiku_fallback(messages: list) -> str:
    """Try cheaper/faster model on the same account as secondary."""
    client = anthropic.AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001", max_tokens=256, messages=messages
    )
    return resp.content[0].text


async def _static_fallback(messages: list) -> str:
    """Always succeeds — last resort."""
    user_text = next(
        (m["content"] for m in reversed(messages)
         if m.get("role") == "user" and isinstance(m.get("content"), str)),
        "your question",
    )
    return (
        f"I'm unable to process your request at this time. "
        f"Your query has been logged and our team will follow up. "
        f"For urgent matters please contact support@example.com."
    )


PROVIDER_CHAIN: list[Provider] = [
    Provider("anthropic-sonnet",  _anthropic_primary,         timeout=15.0),
    Provider("anthropic-haiku",   _anthropic_haiku_fallback,  timeout=8.0),
    Provider("static",            _static_fallback,           timeout=1.0),
]


async def chain_fallback_create(messages: list) -> tuple[str, str]:
    """Returns (text, provider_used)."""
    for provider in PROVIDER_CHAIN:
        t0 = time.monotonic()
        try:
            text = await asyncio.wait_for(
                provider.call_fn(messages),
                timeout=provider.timeout,
            )
            elapsed = time.monotonic() - t0
            print(f"[chain] provider={provider.name}  elapsed={elapsed:.2f}s  OK")
            return text, provider.name
        except Exception as e:
            elapsed = time.monotonic() - t0
            print(f"[chain] provider={provider.name}  elapsed={elapsed:.2f}s  FAIL: {type(e).__name__}")

    # Should never reach here because static_fallback always succeeds
    raise RuntimeError("All providers exhausted including static fallback")


async def demo_chain():
    messages = [{"role": "user", "content": "What is a distributed hash table?"}]
    text, provider = await chain_fallback_create(messages)
    print(f"\n[{provider}] {text[:100]}")


asyncio.run(demo_chain())
```

---

## Comparison

| Approach | AI required | Recovery speed | Personalization | Queue support | Complexity |
|---|---|---|---|---|---|
| Static fallback registry | No | Instant | None | No | Very low |
| Cache stale-while-error | No (on hit) | Instant (stale) | High (cached) | No | Low |
| Rule-based degraded mode | No | Instant | Medium | No | Low |
| Request queuing + webhook | No (immediate) | Deferred | Full | Yes | Medium |
| Feature flag degradation | Partial | Progressive | Partial | No | Medium |
| Multi-provider fallback chain | Depends on chain | Seconds | Full (if AI) | No | Medium |

**Rule of thumb:**
- Consumer-facing chat → static fallback + cache stale-while-error (Solutions 1 + 2) always ready
- Business-critical workflows → request queuing (Solution 4) so no request is lost
- API platform → feature flag degradation (Solution 5) to shed load gracefully
- Cost-sensitive → multi-provider chain (Solution 6) with Haiku as second tier before static
