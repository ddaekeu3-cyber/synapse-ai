---
layout: solution
title: "Agent Doesn't Implement Response Caching for Deterministic Queries"
category: performance
description: "Agent calls the LLM for identical, deterministic queries on every request — the same classification prompt, the same document summary, the same FAQ answer — when the result would be identical each time."
tags: [performance, token-cost, caching, latency, deterministic]
---

## Symptom

Monitoring shows the same prompt sent to the API dozens of times per minute with identical inputs:

```
[2026-04-15 10:01:02] classify("billing question") → NORMAL   [48ms, 312 tokens]
[2026-04-15 10:01:04] classify("billing question") → NORMAL   [51ms, 312 tokens]
[2026-04-15 10:01:07] classify("billing question") → NORMAL   [44ms, 312 tokens]
```

The model is deterministic at `temperature=0` — every call with the same input produces the same output. The agent is paying API cost and adding latency for work it already did 3 seconds ago.

## Root Cause

The LLM call has no caching layer. Common causes:

```python
import anthropic

client = anthropic.Anthropic(api_key="sk-live-...")

# Called on every user request — no cache
def classify_intent(text: str) -> str:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=20,
        temperature=0,  # deterministic — same input ALWAYS gives same output
        messages=[{"role": "user", "content": f"Classify intent: {text}"}]
    )
    return response.content[0].text.strip()
```

When `temperature=0` (or `top_p=1` with greedy decoding), the model is a pure function of its inputs. Calling it twice with the same inputs is wasted spend.

---

## Fix

### Option 1 — In-memory LRU cache with `functools.lru_cache`

For single-process agents, `lru_cache` adds caching in one line.

```python
import anthropic
from functools import lru_cache

client = anthropic.Anthropic(api_key="sk-live-...")

@lru_cache(maxsize=1024)
def classify_intent(text: str) -> str:
    """Cached — same text always returns same label at temperature=0."""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=20,
        temperature=0,
        messages=[{"role": "user", "content": f"Classify intent: {text}"}]
    )
    return response.content[0].text.strip()


# First call: hits API
label1 = classify_intent("billing question")
# Second call: returns from cache — zero tokens, zero latency
label2 = classify_intent("billing question")

print(label1, label2)  # NORMAL NORMAL
print(classify_intent.cache_info())
# CacheInfo(hits=1, misses=1, maxsize=1024, currsize=1)

# Expected Token Savings: 100% on repeated identical queries within process lifetime
# Environment: single-process agents, CLI tools, batch scripts
```

---

### Option 2 — TTL-aware cache with `cachetools`

Add time-to-live so cached results expire after a configured window. Balances freshness against cost.

```python
import anthropic
import hashlib
import time
from cachetools import TTLCache
from threading import Lock

client = anthropic.Anthropic(api_key="sk-live-...")

# 10,000 entries, 1-hour TTL
_cache: TTLCache = TTLCache(maxsize=10_000, ttl=3600)
_lock = Lock()


def _cache_key(model: str, system: str, text: str) -> str:
    payload = f"{model}|{system}|{text}"
    return hashlib.sha256(payload.encode()).hexdigest()


def classify_intent(text: str, system_prompt: str = "Classify intent.") -> str:
    key = _cache_key("claude-haiku-4-5-20251001", system_prompt, text)

    with _lock:
        if key in _cache:
            return _cache[key]

    # Cache miss — call API
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=20,
        temperature=0,
        system=system_prompt,
        messages=[{"role": "user", "content": text}],
    )
    result = response.content[0].text.strip()

    with _lock:
        _cache[key] = result

    return result


# Usage
print(classify_intent("billing question"))   # API call
print(classify_intent("billing question"))   # Cache hit
time.sleep(0)  # In production: after TTL expires, next call refreshes

# Expected Token Savings: near-100% for repeated queries within TTL window
# Environment: web servers handling many users asking similar questions; install: pip install cachetools
```

---

### Option 3 — Redis-backed distributed cache

For multi-process or multi-instance deployments, share the cache across all workers via Redis.

```python
import anthropic
import hashlib
import json
import redis

client = anthropic.Anthropic(api_key="sk-live-...")
redis_client = redis.Redis(host="localhost", port=6379, db=0, decode_responses=True)

CACHE_TTL_SECONDS = 3600  # 1 hour


def _cache_key(model: str, messages: list, system: str = "") -> str:
    payload = json.dumps({"model": model, "system": system, "messages": messages}, sort_keys=True)
    return "llm_cache:" + hashlib.sha256(payload.encode()).hexdigest()


def cached_create(
    model: str,
    messages: list[dict],
    max_tokens: int = 100,
    system: str = "",
    temperature: float = 0,
) -> str:
    """Wrapper around client.messages.create() with Redis caching."""
    if temperature != 0:
        # Non-deterministic — never cache
        response = client.messages.create(
            model=model, messages=messages, max_tokens=max_tokens,
            system=system, temperature=temperature,
        )
        return response.content[0].text.strip()

    key = _cache_key(model, messages, system)

    # Try cache first
    cached = redis_client.get(key)
    if cached:
        return cached

    # Cache miss
    response = client.messages.create(
        model=model, messages=messages, max_tokens=max_tokens,
        system=system, temperature=temperature,
    )
    result = response.content[0].text.strip()

    # Store with TTL
    redis_client.setex(key, CACHE_TTL_SECONDS, result)
    return result


# Usage — shared across all instances of your service
label = cached_create(
    model="claude-haiku-4-5-20251001",
    system="Classify as URGENT or NORMAL.",
    messages=[{"role": "user", "content": "server is down"}],
    max_tokens=10,
)
print(label)  # URGENT

# Expected Token Savings: 90%+ on popular queries across all workers
# Environment: horizontally scaled services; requires Redis
```

---

### Option 4 — Async cache with stampede protection

For async agents, prevent cache stampedes (multiple coroutines all missing cache simultaneously and all calling the API).

```python
import anthropic
import asyncio
import hashlib

client = anthropic.AsyncAnthropic(api_key="sk-live-...")

_cache: dict[str, str] = {}
_in_flight: dict[str, asyncio.Future] = {}
_lock = asyncio.Lock()


async def cached_classify(text: str) -> str:
    key = hashlib.md5(text.encode()).hexdigest()

    # Fast path: already cached
    if key in _cache:
        return _cache[key]

    async with _lock:
        # Double-check after acquiring lock
        if key in _cache:
            return _cache[key]

        # If another coroutine is already fetching this key, wait for it
        if key in _in_flight:
            return await asyncio.shield(_in_flight[key])

        # This coroutine is responsible for fetching
        future: asyncio.Future[str] = asyncio.get_event_loop().create_future()
        _in_flight[key] = future

    try:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=20,
            temperature=0,
            messages=[{"role": "user", "content": f"Classify: {text}"}],
        )
        result = response.content[0].text.strip()
        _cache[key] = result
        future.set_result(result)
        return result
    except Exception as exc:
        future.set_exception(exc)
        raise
    finally:
        async with _lock:
            _in_flight.pop(key, None)


async def main():
    # 10 concurrent requests for the same text — only 1 API call happens
    results = await asyncio.gather(
        *[cached_classify("billing question") for _ in range(10)]
    )
    print(set(results))  # {"NORMAL"} — all got the same result

asyncio.run(main())

# Expected Token Savings: N-1 API calls eliminated for N concurrent identical requests
# Environment: async agents under burst traffic with repeated queries
```

---

### Option 5 — Semantic cache using embeddings

Cache not just exact matches but semantically equivalent queries. "billing issue" and "question about my invoice" should hit the same cache entry.

```python
import anthropic
import numpy as np

client = anthropic.Anthropic(api_key="sk-live-...")

# Semantic cache: stores (embedding_vector, cached_answer) pairs
_semantic_cache: list[tuple[np.ndarray, str, str]] = []  # (embedding, query, answer)
SIMILARITY_THRESHOLD = 0.92


def embed(text: str) -> np.ndarray:
    """Get embedding using Voyage or similar. Here we simulate with a hash-based mock."""
    # In production: use voyageai.Client().embed([text]).embeddings[0]
    # Mock: deterministic random vector based on text
    rng = np.random.default_rng(hash(text) % (2**32))
    vec = rng.random(128)
    return vec / np.linalg.norm(vec)


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.dot(a, b))


def cached_classify(text: str) -> tuple[str, bool]:
    """Returns (label, cache_hit)."""
    query_emb = embed(text)

    # Search semantic cache
    for cached_emb, cached_query, cached_answer in _semantic_cache:
        sim = cosine_similarity(query_emb, cached_emb)
        if sim >= SIMILARITY_THRESHOLD:
            return cached_answer, True  # semantic hit

    # Miss — call API
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=20,
        temperature=0,
        messages=[{"role": "user", "content": f"Classify as URGENT or NORMAL: {text}"}],
    )
    answer = response.content[0].text.strip()
    _semantic_cache.append((query_emb, text, answer))
    return answer, False


queries = [
    "billing question",         # miss → API
    "question about my bill",   # semantic hit if similar enough
    "server is down",           # miss → API
    "the server crashed",       # semantic hit
]
for q in queries:
    label, hit = cached_classify(q)
    print(f"{'HIT ' if hit else 'MISS'} {label:6}  {q}")

# Expected Token Savings: captures near-duplicate queries; 60–80% hit rate on natural language variance
# Environment: customer-facing agents where users rephrase the same question differently
```

---

### Option 6 — Cache with automatic invalidation on prompt change

Cache is automatically invalidated when the system prompt or model changes, preventing stale responses after deployments.

```python
import anthropic
import hashlib
import json
import time
from dataclasses import dataclass, field

client = anthropic.Anthropic(api_key="sk-live-...")


@dataclass
class VersionedCache:
    """LRU-like cache that auto-invalidates when prompt version changes."""
    model: str
    system_prompt: str
    ttl: int = 3600
    _store: dict = field(default_factory=dict)

    @property
    def prompt_version(self) -> str:
        """Hash of model + system prompt — changes when either changes."""
        payload = f"{self.model}|{self.system_prompt}"
        return hashlib.md5(payload.encode()).hexdigest()[:8]

    def _key(self, user_input: str) -> str:
        return f"{self.prompt_version}:{hashlib.md5(user_input.encode()).hexdigest()}"

    def get(self, user_input: str) -> str | None:
        key = self._key(user_input)
        entry = self._store.get(key)
        if entry is None:
            return None
        value, expires_at = entry
        if time.time() > expires_at:
            del self._store[key]
            return None
        return value

    def set(self, user_input: str, value: str) -> None:
        key = self._key(user_input)
        self._store[key] = (value, time.time() + self.ttl)

    def classify(self, text: str) -> str:
        cached = self.get(text)
        if cached is not None:
            return cached

        response = client.messages.create(
            model=self.model,
            max_tokens=20,
            temperature=0,
            system=self.system_prompt,
            messages=[{"role": "user", "content": text}],
        )
        result = response.content[0].text.strip()
        self.set(text, result)
        return result


# v1 cache
cache_v1 = VersionedCache(
    model="claude-haiku-4-5-20251001",
    system_prompt="Classify tickets as URGENT or NORMAL.",
)
print(cache_v1.classify("server down"))    # API call
print(cache_v1.classify("server down"))    # Cache hit

# After prompt update — old cache entries are automatically stale (different version hash)
cache_v2 = VersionedCache(
    model="claude-haiku-4-5-20251001",
    system_prompt="Classify tickets as URGENT, NORMAL, or LOW.",  # changed
)
print(cache_v2.classify("server down"))    # API call — different version hash

# Expected Token Savings: 90%+ on stable queries; zero stale responses after prompt updates
# Environment: production agents that deploy prompt changes and need cache safety guarantees
```

---

## Comparison

| Option | Scope | TTL Support | Stampede Protection | Semantic Match | Invalidation |
|--------|-------|-------------|---------------------|----------------|--------------|
| 1 | Single process | No (process lifetime) | No | No | Process restart |
| 2 | Single process | Yes | Yes (lock) | No | TTL expiry |
| 3 | Multi-process | Yes | Redis atomic | No | TTL expiry |
| 4 | Async single process | No | Yes (Future) | No | Process restart |
| 5 | Single process | No | No | Yes | Manual |
| 6 | Single process | Yes | No | No | Prompt version hash |

**Recommended starting point:** Option 1 for scripts and batch jobs (zero dependencies). Option 2 for production web services (TTL + thread safety). Option 3 for multi-instance deployments. Always confirm `temperature=0` before caching — never cache non-deterministic outputs.
