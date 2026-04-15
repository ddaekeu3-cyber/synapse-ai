---
layout: solution
title: "Agent pays full price for idempotent re-runs"
category: token-cost
description: "Agent re-runs the same expensive computation on every invocation even when the inputs haven't changed, burning tokens on work that could be skipped or served from a result cache."
tags: [token-cost, caching, idempotency, efficiency, memoization]
---

## Symptom

The same user question, the same document, or the same structured task is processed from scratch every time it arrives, even if the identical request was handled minutes or hours ago. Token meters show the same high per-request cost whether the request is novel or a repeat. On high-traffic endpoints, 30–70% of requests are duplicates.

```
Request 1: "Summarize Q3 earnings report" → 4,200 tokens (compute)
Request 2: "Summarize Q3 earnings report" → 4,200 tokens (identical — wasted)
Request 3: "Summarize Q3 earnings report" → 4,200 tokens (identical — wasted)
```

## Root Cause

The agent has no result cache at the application layer. Every invocation goes straight to the model API regardless of whether the same computation was recently performed. The Anthropic API is stateless — it does not de-duplicate across requests — so the application must implement idempotent caching itself.

## Fix

Cache the output keyed by a hash of the inputs (model, system prompt, messages). Return cached results for identical inputs within a configurable TTL. For prompt-cache-eligible content, combine application-level result caching with Anthropic's prompt caching for maximum savings.

---

### Option 1 — In-memory result cache with SHA-256 key

```python
import anthropic
import hashlib
import json
import time
from dataclasses import dataclass, field

client = anthropic.Anthropic()

@dataclass
class CachedResult:
    text: str
    created_at: float
    model: str
    tokens_saved: int = 0

class ResultCache:
    def __init__(self, ttl_seconds: float = 300.0) -> None:
        self._store: dict[str, CachedResult] = {}
        self._ttl = ttl_seconds
        self.hits = 0
        self.misses = 0
        self.tokens_saved = 0

    def _key(self, model: str, system: str, messages: list[dict]) -> str:
        payload = json.dumps({"model": model, "system": system, "messages": messages}, sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()

    def get(self, model: str, system: str, messages: list[dict]) -> CachedResult | None:
        key = self._key(model, system, messages)
        entry = self._store.get(key)
        if entry and (time.monotonic() - entry.created_at) < self._ttl:
            self.hits += 1
            return entry
        self.misses += 1
        return None

    def set(self, model: str, system: str, messages: list[dict], text: str, tokens: int) -> None:
        key = self._key(model, system, messages)
        self._store[key] = CachedResult(text=text, created_at=time.monotonic(), model=model)

    def record_save(self, tokens: int) -> None:
        self.tokens_saved += tokens
        self._store[list(self._store.keys())[-1]].tokens_saved = tokens

    def stats(self) -> dict:
        total = self.hits + self.misses
        return {
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": f"{self.hits/total*100:.0f}%" if total else "0%",
            "tokens_saved": self.tokens_saved,
        }

cache = ResultCache(ttl_seconds=300)

def cached_completion(model: str, system: str, messages: list[dict], max_tokens: int) -> tuple[str, bool]:
    cached = cache.get(model, system, messages)
    if cached:
        print(f"[CACHE HIT] returning cached result")
        return cached.text, True

    response = client.messages.create(
        model=model, max_tokens=max_tokens, system=system, messages=messages,
    )
    text = response.content[0].text
    total_tokens = response.usage.input_tokens + response.usage.output_tokens
    cache.set(model, system, messages, text, total_tokens)
    print(f"[CACHE MISS] computed: {total_tokens} tokens")
    return text, False

# Simulate repeated requests
MODEL  = "claude-haiku-4-5-20251001"
SYSTEM = "Summarize the document concisely."
DOC    = "Q3 2025 earnings: revenue $4.2B (+18% YoY), operating income $1.1B, EPS $2.34."

requests = [
    [{"role": "user", "content": f"Summarize: {DOC}"}],
    [{"role": "user", "content": f"Summarize: {DOC}"}],   # identical
    [{"role": "user", "content": "Summarize: Q3 revenue was $4.2B."}],  # different
    [{"role": "user", "content": f"Summarize: {DOC}"}],   # identical again
]

for i, msgs in enumerate(requests):
    text, from_cache = cached_completion(MODEL, SYSTEM, msgs, max_tokens=128)
    print(f"[{i+1}] from_cache={from_cache}: {text[:60]}\n")

print("Cache stats:", json.dumps(cache.stats(), indent=2))
```

**Expected Token Savings:** 40–70% token reduction for high-repeat workloads; cache hit rate of 50% halves effective token spend; TTL prevents serving stale results.

**Environment:** Single-process API servers; for multi-process deployments, replace the dict with Redis (see Option 3).

---

### Option 2 — File-system cache for persistent cross-restart reuse

```python
import anthropic
import hashlib
import json
import time
from pathlib import Path

client = anthropic.Anthropic()

CACHE_DIR = Path("/tmp/agent_result_cache")
CACHE_DIR.mkdir(exist_ok=True)
CACHE_TTL = 3600.0   # 1 hour

def cache_path(key: str) -> Path:
    return CACHE_DIR / f"{key[:2]}/{key}.json"

def make_key(model: str, system: str, messages: list) -> str:
    raw = json.dumps({"m": model, "s": system, "msgs": messages}, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()

def read_cache(key: str) -> str | None:
    p = cache_path(key)
    if not p.exists():
        return None
    entry = json.loads(p.read_text())
    if time.time() - entry["ts"] > CACHE_TTL:
        p.unlink(missing_ok=True)
        return None
    return entry["text"]

def write_cache(key: str, text: str, tokens: int) -> None:
    p = cache_path(key)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"text": text, "ts": time.time(), "tokens": tokens}))

def cached_call(model: str, system: str, user_message: str, max_tokens: int = 256) -> tuple[str, bool]:
    messages = [{"role": "user", "content": user_message}]
    key = make_key(model, system, messages)

    cached = read_cache(key)
    if cached:
        print(f"[FS CACHE HIT] key={key[:8]}")
        return cached, True

    resp = client.messages.create(
        model=model, system=system, max_tokens=max_tokens, messages=messages,
    )
    text = resp.content[0].text
    tokens = resp.usage.input_tokens + resp.usage.output_tokens
    write_cache(key, text, tokens)
    print(f"[FS CACHE MISS] key={key[:8]} tokens={tokens}")
    return text, False

MODEL  = "claude-haiku-4-5-20251001"
SYSTEM = "You are a concise technical writer."

# First call — computes and caches
text1, hit1 = cached_call(MODEL, SYSTEM, "Explain what a cache key is in one sentence.")
print(f"Result: {text1}\n")

# Second call — cache hit (survives process restart)
text2, hit2 = cached_call(MODEL, SYSTEM, "Explain what a cache key is in one sentence.")
print(f"Cached: {text2[:80]}\n")

print(f"Token cost avoided on hit: {'yes' if hit2 else 'no'}")
```

**Expected Token Savings:** File-system cache persists across restarts and deployments; useful for expensive document analysis tasks where the same files are processed repeatedly; 0 API cost on cache hits.

**Environment:** Single-server deployments; replace with S3 or GCS for horizontally scaled agents.

---

### Option 3 — Distributed Redis cache for multi-instance agents

```python
import anthropic
import hashlib
import json
import time

client = anthropic.Anthropic()

# Redis client — install: pip install redis
try:
    import redis
    redis_client = redis.Redis(host="localhost", port=6379, db=0, decode_responses=True)
    redis_client.ping()
    REDIS_AVAILABLE = True
except Exception:
    REDIS_AVAILABLE = False
    redis_client = None

# Fallback to in-memory if Redis not available (for demo)
_mem_cache: dict[str, dict] = {}

CACHE_TTL = 600   # seconds

def make_key(model: str, system: str, messages: list) -> str:
    raw = json.dumps({"m": model, "s": system, "msgs": messages}, sort_keys=True)
    return "agent:" + hashlib.sha256(raw.encode()).hexdigest()

def cache_get(key: str) -> str | None:
    if REDIS_AVAILABLE:
        val = redis_client.get(key)
        return val
    entry = _mem_cache.get(key)
    if entry and time.monotonic() - entry["ts"] < CACHE_TTL:
        return entry["text"]
    return None

def cache_set(key: str, text: str) -> None:
    if REDIS_AVAILABLE:
        redis_client.setex(key, CACHE_TTL, text)
    else:
        _mem_cache[key] = {"text": text, "ts": time.monotonic()}

def distributed_cached_completion(
    model: str,
    system: str,
    messages: list[dict],
    max_tokens: int,
) -> tuple[str, str]:
    key = make_key(model, system, messages)
    backend = "redis" if REDIS_AVAILABLE else "memory"

    cached = cache_get(key)
    if cached:
        return cached, f"HIT ({backend})"

    resp = client.messages.create(
        model=model, system=system, max_tokens=max_tokens, messages=messages,
    )
    text = resp.content[0].text
    cache_set(key, text)
    tokens = resp.usage.input_tokens + resp.usage.output_tokens
    return text, f"MISS — {tokens} tokens computed"

# Demo
MODEL   = "claude-haiku-4-5-20251001"
SYSTEM  = "You are a helpful assistant."
MESSAGE = [{"role": "user", "content": "What is an idempotent operation?"}]

for i in range(4):
    text, status = distributed_cached_completion(MODEL, SYSTEM, MESSAGE, max_tokens=128)
    print(f"[{i+1}] {status}: {text[:60]}")
```

**Expected Token Savings:** Same result as in-memory cache but shared across all instances; with 4 horizontally scaled agents, a 50% hit rate saves 50% of total API spend across the entire fleet.

**Environment:** Kubernetes or multi-process deployments; Redis TTL auto-expires entries without a background job; key prefix (`agent:`) allows namespace isolation per agent type.

---

### Option 4 — Content-addressed cache with input normalization

```python
import anthropic
import hashlib
import json
import re
import time

client = anthropic.Anthropic()

_cache: dict[str, tuple[str, float]] = {}
TTL = 300.0

def normalize_message(text: str) -> str:
    """Normalize whitespace, punctuation, and case so minor variations map to the same key."""
    text = text.strip().lower()
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"[^\w\s]", "", text)
    return text

def content_key(model: str, system: str, messages: list[dict]) -> str:
    normalized_msgs = [
        {"role": m["role"], "content": normalize_message(str(m.get("content", "")))}
        for m in messages
    ]
    raw = json.dumps({"model": model, "system": normalize_message(system), "msgs": normalized_msgs}, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()

def normalized_cached_call(model: str, system: str, messages: list[dict], max_tokens: int) -> tuple[str, bool]:
    key = content_key(model, system, messages)
    cached, ts = _cache.get(key, (None, 0))
    if cached and time.monotonic() - ts < TTL:
        print(f"[NORM HIT] key={key[:8]}")
        return cached, True

    resp = client.messages.create(model=model, system=system, max_tokens=max_tokens, messages=messages)
    text = resp.content[0].text
    _cache[key] = (text, time.monotonic())
    tokens = resp.usage.input_tokens + resp.usage.output_tokens
    print(f"[NORM MISS] key={key[:8]} tokens={tokens}")
    return text, False

MODEL  = "claude-haiku-4-5-20251001"
SYSTEM = "You are a helpful assistant."

# These should all map to the same cache key after normalization
variants = [
    [{"role": "user", "content": "What is machine learning?"}],
    [{"role": "user", "content": "what is machine learning?"}],       # different case
    [{"role": "user", "content": "  What is machine learning?  "}],   # extra whitespace
    [{"role": "user", "content": "What is machine learning"}],         # no question mark
]

for i, msgs in enumerate(variants):
    text, hit = normalized_cached_call(MODEL, SYSTEM, msgs, max_tokens=128)
    print(f"[variant {i+1}] hit={hit}: {text[:60]}\n")
```

**Expected Token Savings:** Normalization collapses near-duplicate requests that differ only in whitespace, punctuation, or case; in practice catches 10–20% additional cache hits beyond exact-key matching.

**Environment:** Consumer-facing agents where users rephrase the same question slightly differently; combine with semantic deduplication for highest hit rates.

---

### Option 5 — Prompt caching + result caching: double-stacked savings

```python
import anthropic
import hashlib
import json
import time

client = anthropic.Anthropic()

# Large static context eligible for prompt caching
STATIC_CONTEXT = """
## Product Documentation (1,200 words)

Section 1: Getting Started
Users can sign up at app.example.com. After registration, an API key is issued
automatically. The key begins with 'sk-live-' followed by 32 characters.

Section 2: Authentication
All API requests must include the header: Authorization: Bearer sk-live-<key>.
Requests without this header return HTTP 401 Unauthorized.

Section 3: Rate Limits
Free tier: 100 requests/minute. Pro: 1000/minute. Enterprise: custom.
Rate limit headers: X-RateLimit-Limit, X-RateLimit-Remaining, X-RateLimit-Reset.

Section 4: Webhooks
Configure webhooks at Settings > Integrations. Events: created, updated, deleted.
Webhook payloads are signed with HMAC-SHA256 using your webhook secret.

Section 5: Errors
Standard HTTP status codes. Error body: {"error": {"code": str, "message": str}}.
Retry-able errors: 429, 500, 502, 503. Non-retryable: 400, 401, 403, 404.
""" * 3   # make it large enough to benefit from prompt caching (~1200 tokens)

# Application-level result cache
_result_cache: dict[str, tuple[str, float]] = {}
RESULT_TTL = 600.0

def double_cached_qa(question: str) -> tuple[str, str]:
    """
    Combines:
    1. Application result cache (avoids API call entirely on repeat questions)
    2. Anthropic prompt cache (saves input tokens on new questions with same context)
    """
    key = hashlib.sha256(question.lower().strip().encode()).hexdigest()
    cached, ts = _result_cache.get(key, (None, 0))

    if cached and time.monotonic() - ts < RESULT_TTL:
        return cached, "result-cache-hit"

    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=[
            {
                "type": "text",
                "text": f"You are a helpful assistant. Answer using the documentation below.\n\n{STATIC_CONTEXT}",
                "cache_control": {"type": "ephemeral"},   # prompt-cache the large context
            }
        ],
        extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
        messages=[{"role": "user", "content": question}],
    )

    text = resp.content[0].text
    _result_cache[key] = (text, time.monotonic())

    cache_read    = getattr(resp.usage, "cache_read_input_tokens", 0)
    cache_created = getattr(resp.usage, "cache_creation_input_tokens", 0)
    detail = f"api-call (cache_read={cache_read} cache_write={cache_created})"
    return text, detail

questions = [
    "How do I authenticate API requests?",
    "What are the rate limits?",
    "How do I authenticate API requests?",   # result cache hit
    "How are webhooks signed?",
    "What are the rate limits?",             # result cache hit
]

for q in questions:
    text, source = double_cached_qa(q)
    print(f"[{source}] Q: {q[:45]}")
    print(f"  A: {text[:80]}\n")
```

**Expected Token Savings:** Application result cache: 100% savings on repeat questions (no API call). Prompt caching: 60–80% input token savings on new questions. Combined: first question pays full price once; all subsequent identical questions are free; new questions with the same context pay only output + small fraction of input.

**Environment:** Documentation Q&A agents with a large static knowledge base; the combination is the most token-efficient pattern available.

---

### Option 6 — Idempotency key pattern: caller controls deduplication window

```python
import anthropic
import hashlib
import json
import time
from dataclasses import dataclass

client = anthropic.Anthropic()

@dataclass
class IdempotentResponse:
    text: str
    idempotency_key: str
    cached: bool
    computed_at: float

_idem_store: dict[str, IdempotentResponse] = {}
IDEM_TTL = 86400.0   # 24 hours — long window for idempotent operations

def idempotent_completion(
    idempotency_key: str,
    model: str,
    system: str,
    messages: list[dict],
    max_tokens: int,
) -> IdempotentResponse:
    """
    Caller provides an idempotency key — same key always returns the same result
    within the TTL window. Useful for webhook retry scenarios where the same
    event may trigger the agent multiple times.
    """
    stored = _idem_store.get(idempotency_key)
    if stored and (time.monotonic() - stored.computed_at) < IDEM_TTL:
        print(f"[IDEM] Key '{idempotency_key}' — returning cached response")
        return IdempotentResponse(
            text=stored.text,
            idempotency_key=idempotency_key,
            cached=True,
            computed_at=stored.computed_at,
        )

    resp = client.messages.create(
        model=model, system=system, max_tokens=max_tokens, messages=messages,
    )
    text = resp.content[0].text
    tokens = resp.usage.input_tokens + resp.usage.output_tokens
    print(f"[IDEM] Key '{idempotency_key}' — computed ({tokens} tokens)")

    result = IdempotentResponse(
        text=text,
        idempotency_key=idempotency_key,
        cached=False,
        computed_at=time.monotonic(),
    )
    _idem_store[idempotency_key] = result
    return result

MODEL  = "claude-haiku-4-5-20251001"
SYSTEM = "Classify the support ticket into: billing, technical, general."

# Simulate webhook delivering the same event 3 times (common in distributed systems)
ticket_id = "TICKET-8821"
ticket_text = "I was charged twice for my Pro subscription this month."

for attempt in range(1, 4):
    idem_key = f"classify:{ticket_id}"  # same key → same result
    result = idempotent_completion(
        idempotency_key=idem_key,
        model=MODEL,
        system=SYSTEM,
        messages=[{"role": "user", "content": ticket_text}],
        max_tokens=32,
    )
    print(f"Attempt {attempt}: cached={result.cached} → {result.text.strip()}\n")

# Different ticket — different key, always computed fresh
result2 = idempotent_completion(
    idempotency_key="classify:TICKET-8822",
    model=MODEL, system=SYSTEM,
    messages=[{"role": "user", "content": "The app crashes on iOS 17."}],
    max_tokens=32,
)
print(f"New ticket: cached={result2.cached} → {result2.text.strip()}")
```

**Expected Token Savings:** Eliminates duplicate processing in webhook retry storms — a common pattern where a payment event triggers the agent 3–5 times; caller-controlled keys are more precise than content hashing for event-driven architectures.

**Environment:** Event-driven agents triggered by webhooks, message queues (SQS, Kafka), or job schedulers; idempotency keys are a standard pattern in payment and billing systems.

---

## Comparison

| Option | Cache Scope | Persistence | Multi-Instance | Dedup Mechanism |
|--------|------------|-------------|---------------|----------------|
| 1 — In-memory dict | Process | None | No | SHA-256 of inputs |
| 2 — File system | Disk | Cross-restart | No | SHA-256 → file path |
| 3 — Redis | Shared memory | TTL-based | Yes | SHA-256 → Redis key |
| 4 — Normalized key | Process | None | No | Normalized SHA-256 |
| 5 — Double-stacked | Process + API | Process only | No | SHA-256 + prompt cache |
| 6 — Idempotency key | Process | None | No | Caller-provided key |

**Recommended default:** Option 3 (Redis) for production multi-instance deployments — shared cache eliminates duplicate computation across all agent instances. Combine with Option 5 (prompt caching) for the large static context that every agent request includes.
