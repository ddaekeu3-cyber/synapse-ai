---
layout: solution
title: "Agent Doesn't Implement Request Deduplication"
category: concurrency
description: "Agent fires duplicate LLM calls for identical or near-identical requests arriving within the same time window — from retries, UI double-clicks, or parallel clients — wasting tokens and potentially returning inconsistent results."
tags: [concurrency, deduplication, caching, idempotency, efficiency, token-cost]
---

## Symptom

A user clicks "Submit" twice quickly. The frontend sends two identical requests within 200ms. The agent processes both, fires two LLM calls for the same input, and returns the same (or slightly different) answer twice. The billing dashboard shows doubled token usage for the same user query. In a multi-agent setup, two orchestrators simultaneously ask the same sub-agent for the same fact, triggering two expensive calls when one would suffice.

## Root Cause

HTTP requests are stateless by default — each request is processed independently. Without deduplication, concurrent identical requests each trigger their own LLM call. This is wasteful for deterministic queries (temperature=0, same prompt) and potentially inconsistent for probabilistic ones. The fix requires a short-lived request identity window: if two calls with the same content arrive within N seconds, only one LLM call is made and both callers receive the same response.

## Fix

### Option 1 — In-process request deduplication with asyncio.Lock per key

```python
import asyncio
import hashlib
import time
import anthropic

client = anthropic.AsyncAnthropic()

class RequestDeduplicator:
    """
    If two coroutines make identical requests within `window_s` seconds,
    only one LLM call is made — the second awaits the first's result.
    """

    def __init__(self, window_s: float = 5.0) -> None:
        self._window   = window_s
        self._inflight: dict[str, asyncio.Future] = {}
        self._cache:    dict[str, tuple[str, float]] = {}   # key → (result, expires_at)
        self._lock      = asyncio.Lock()

    def _key(self, messages: list[dict], system: str = "") -> str:
        payload = system + str(messages)
        return hashlib.sha256(payload.encode()).hexdigest()[:16]

    async def ask(self, messages: list[dict], system: str = "",
                  model: str = "claude-haiku-4-5-20251001", max_tokens: int = 128) -> str:
        key = self._key(messages, system)
        now = time.monotonic()

        async with self._lock:
            # Return cached result if within window
            if key in self._cache:
                result, expires_at = self._cache[key]
                if now < expires_at:
                    print(f"  [dedup] cache hit for {key}")
                    return result

            # If another coroutine is already processing this key, await its future
            if key in self._inflight:
                print(f"  [dedup] awaiting in-flight request {key}")
                future = self._inflight[key]
            else:
                # We are the first — create a future and start the call
                future = asyncio.get_event_loop().create_future()
                self._inflight[key] = future

        if not future.done() and key in self._inflight and self._inflight[key] is future:
            # We own this future — make the LLM call
            try:
                r = await client.messages.create(
                    model=model, max_tokens=max_tokens, system=system, messages=messages
                )
                result = r.content[0].text
                async with self._lock:
                    self._cache[key]    = (result, time.monotonic() + self._window)
                    del self._inflight[key]
                future.set_result(result)
                print(f"  [dedup] new call completed for {key} ({r.usage.output_tokens} tok)")
                return result
            except Exception as e:
                async with self._lock:
                    if key in self._inflight:
                        del self._inflight[key]
                if not future.done():
                    future.set_exception(e)
                raise
        else:
            return await future

_DEDUP = RequestDeduplicator(window_s=5.0)

async def main() -> None:
    question = "What is the capital of France?"
    messages = [{"role": "user", "content": question}]

    print("Sending 5 identical concurrent requests:")
    t0 = time.perf_counter()
    results = await asyncio.gather(*[_DEDUP.ask(messages) for _ in range(5)])
    elapsed = (time.perf_counter() - t0) * 1000

    print(f"\n5 requests completed in {elapsed:.0f}ms")
    unique = set(results)
    print(f"Unique responses: {len(unique)} (should be 1 due to dedup)")
    print(f"Answer: {results[0].strip()[:80]}")

asyncio.run(main())
```

**Expected Token Savings:** 5 identical concurrent requests produce 1 LLM call instead of 5 — saving 4 × (input + output tokens) = 80% token reduction for duplicate bursts; for a button that fires 2 requests on double-click, deduplication eliminates 50% of token cost for that pattern.
**Environment:** Async agents serving web UIs or APIs where duplicate requests arrive in bursts; asyncio.Future-based deduplication is the most efficient in-process solution.

---

### Option 2 — Content-hashed response cache with TTL

```python
import asyncio
import hashlib
import time
import anthropic

client = anthropic.AsyncAnthropic()

class TTLResponseCache:
    """
    Short-lived cache keyed by request hash.
    Deduplicates identical requests within the TTL window.
    """

    def __init__(self, ttl_s: float = 10.0, max_size: int = 500) -> None:
        self._ttl      = ttl_s
        self._max_size = max_size
        self._cache: dict[str, dict] = {}

    def _make_key(self, model: str, system: str, messages: list, max_tokens: int) -> str:
        raw = f"{model}|{system}|{max_tokens}|{messages}"
        return hashlib.sha256(raw.encode()).hexdigest()

    def get(self, key: str) -> str | None:
        entry = self._cache.get(key)
        if entry and time.monotonic() < entry["expires_at"]:
            entry["hits"] += 1
            return entry["value"]
        if entry:
            del self._cache[key]
        return None

    def set(self, key: str, value: str) -> None:
        # Evict oldest entries if cache is full
        if len(self._cache) >= self._max_size:
            oldest = min(self._cache.items(), key=lambda x: x[1]["created_at"])
            del self._cache[oldest[0]]
        self._cache[key] = {
            "value":      value,
            "created_at": time.monotonic(),
            "expires_at": time.monotonic() + self._ttl,
            "hits":       0,
        }

    @property
    def stats(self) -> dict:
        entries = list(self._cache.values())
        return {
            "entries": len(entries),
            "total_hits": sum(e["hits"] for e in entries),
        }

_CACHE = TTLResponseCache(ttl_s=30.0, max_size=1000)

async def ask_cached(question: str, model: str = "claude-haiku-4-5-20251001",
                     max_tokens: int = 128) -> tuple[str, bool]:
    """Returns (response, was_cached)."""
    system   = "Answer concisely."
    messages = [{"role": "user", "content": question}]
    key      = _CACHE._make_key(model, system, messages, max_tokens)

    cached = _CACHE.get(key)
    if cached is not None:
        return cached, True

    r = await client.messages.create(
        model=model, max_tokens=max_tokens, system=system, messages=messages
    )
    result = r.content[0].text
    _CACHE.set(key, result)
    return result, False

async def main() -> None:
    questions = [
        "What is Python?",
        "What is asyncio?",
        "What is Python?",    # duplicate — should hit cache
        "What is Python?",    # duplicate — should hit cache
        "What is asyncio?",   # duplicate — should hit cache
        "What is a decorator?",
    ]

    print("Processing requests:")
    for q in questions:
        result, cached = await ask_cached(q)
        status = "CACHE" if cached else "CALL "
        print(f"  [{status}] {q}: {result.strip()[:50]}")

    print(f"\nCache stats: {_CACHE.stats}")

asyncio.run(main())
```

**Expected Token Savings:** TTL cache with 30s window deduplicates all repeated questions within that window; for FAQ bots where the same 20 questions are asked by hundreds of users per hour, caching can reduce LLM calls by 90%+ on peak identical-query days.
**Environment:** FAQ agents, customer support bots, and any agent where many users ask the same questions; response caching is the most impactful optimisation for read-heavy workloads.

---

### Option 3 — Idempotency key: client-side deduplication via unique request IDs

```python
import asyncio
import time
import anthropic

client = anthropic.AsyncAnthropic()

class IdempotencyStore:
    """
    Clients pass an idempotency_key with their request.
    Duplicate keys within TTL return the same response as the original.
    """

    def __init__(self, ttl_s: float = 60.0) -> None:
        self._store: dict[str, dict] = {}
        self._ttl   = ttl_s

    def get(self, key: str) -> str | None:
        entry = self._store.get(key)
        if entry and time.monotonic() < entry["expires_at"]:
            print(f"  [idempotent] returning cached result for key={key!r}")
            return entry["result"]
        if entry:
            del self._store[key]
        return None

    def set(self, key: str, result: str) -> None:
        self._store[key] = {
            "result":     result,
            "created_at": time.monotonic(),
            "expires_at": time.monotonic() + self._ttl,
        }

_IDEM = IdempotencyStore(ttl_s=60.0)

async def ask(question: str, idempotency_key: str | None = None,
              model: str = "claude-haiku-4-5-20251001") -> str:
    """
    If idempotency_key is provided, duplicate calls return the original result.
    This prevents double-processing from network retries or UI double-clicks.
    """
    if idempotency_key:
        cached = _IDEM.get(idempotency_key)
        if cached is not None:
            return cached

    r = await client.messages.create(
        model=model,
        max_tokens=128,
        messages=[{"role": "user", "content": question}],
    )
    result = r.content[0].text.strip()
    print(f"  [new call] {r.usage.input_tokens}in/{r.usage.output_tokens}out tok")

    if idempotency_key:
        _IDEM.set(idempotency_key, result)

    return result

async def main() -> None:
    # Simulate a client that retries on network failure
    # Same idempotency_key = same LLM call result, no duplicate billing
    key = "req_abc123_classify_ticket_42"

    print("First request (new call):")
    r1 = await ask("Classify this ticket: 'I was charged twice'", idempotency_key=key)
    print(f"  Result: {r1[:60]}")

    print("\nRetry (idempotent — no new LLM call):")
    r2 = await ask("Classify this ticket: 'I was charged twice'", idempotency_key=key)
    print(f"  Result: {r2[:60]}")

    print(f"\nResults identical: {r1 == r2}")

    # Different key = new call
    print("\nDifferent key (new call):")
    r3 = await ask("Classify this ticket: 'App crashes on startup'", idempotency_key="req_def456")
    print(f"  Result: {r3[:60]}")

asyncio.run(main())
```

**Expected Token Savings:** Idempotency keys prevent duplicate billing when network retries hit the server — a common scenario in mobile apps and unreliable networks; each prevented duplicate call saves 100% of its token cost.
**Environment:** Agents exposed as APIs to clients over unreliable networks; idempotency keys are the industry-standard pattern (used by Stripe, Anthropic's own API) for preventing duplicate side effects.

---

### Option 4 — Deduplication window for streaming responses

```python
import asyncio
import hashlib
import time
import anthropic

client = anthropic.AsyncAnthropic()

class StreamingDeduplicator:
    """
    Deduplicates concurrent streaming requests.
    The first caller streams from the API; subsequent duplicates receive the
    completed response from cache once the stream finishes.
    """

    def __init__(self, window_s: float = 5.0) -> None:
        self._window    = window_s
        self._streaming: dict[str, asyncio.Future] = {}
        self._cache:     dict[str, tuple[str, float]] = {}
        self._lock       = asyncio.Lock()

    def _key(self, prompt: str) -> str:
        return hashlib.md5(prompt.encode()).hexdigest()[:12]

    async def ask_streaming(self, question: str) -> str:
        key = self._key(question)
        now = time.monotonic()

        async with self._lock:
            if key in self._cache:
                result, exp = self._cache[key]
                if now < exp:
                    print(f"  [stream-dedup] cache hit for {key}")
                    return result

            if key in self._streaming:
                print(f"  [stream-dedup] waiting for in-flight stream {key}")
                return await self._streaming[key]

            future = asyncio.get_event_loop().create_future()
            self._streaming[key] = future

        # Stream the response
        full_text = ""
        try:
            async with client.messages.stream(
                model="claude-haiku-4-5-20251001",
                max_tokens=128,
                messages=[{"role": "user", "content": question}],
            ) as stream:
                async for text in stream.text_stream:
                    full_text += text
            print(f"  [stream-dedup] stream complete for {key} ({len(full_text.split())} words)")
            async with self._lock:
                self._cache[key]       = (full_text, time.monotonic() + self._window)
                del self._streaming[key]
            future.set_result(full_text)
            return full_text
        except Exception as e:
            async with self._lock:
                if key in self._streaming:
                    del self._streaming[key]
            if not future.done():
                future.set_exception(e)
            raise

_SD = StreamingDeduplicator(window_s=5.0)

async def main() -> None:
    question = "Explain what a Python generator is."

    print("3 concurrent identical streaming requests:")
    t0 = time.perf_counter()
    results = await asyncio.gather(*[_SD.ask_streaming(question) for _ in range(3)])
    elapsed = (time.perf_counter() - t0) * 1000

    print(f"\n3 requests in {elapsed:.0f}ms")
    print(f"All identical: {len(set(results)) == 1}")
    print(f"Answer: {results[0].strip()[:120]}")

asyncio.run(main())
```

**Expected Token Savings:** Streaming deduplication ensures that 3 concurrent identical streaming requests produce 1 stream — the 2 waiters receive the complete response once the stream finishes; for streaming-heavy UIs where users trigger multiple concurrent requests, this eliminates all duplicate stream costs.
**Environment:** Agents with streaming enabled; streaming deduplication is more complex than non-streaming but essential when the primary interface is a streaming API.

---

### Option 5 — Distributed deduplication with Redis for multi-instance agents

```python
import hashlib
import json
import time
import anthropic

client = anthropic.Anthropic()

# Redis-based deduplication for multi-instance deployments
# Requires: pip install redis
# In production: replace FakeRedis with redis.Redis(host=..., port=...)

class FakeRedis:
    """Simulates Redis for demonstration — replace with real Redis in production."""
    def __init__(self):
        self._data: dict[str, tuple[str, float]] = {}

    def set(self, key: str, value: str, ex: int = 30) -> None:
        self._data[key] = (value, time.time() + ex)

    def get(self, key: str) -> str | None:
        entry = self._data.get(key)
        if entry and time.time() < entry[1]:
            return entry[0]
        return None

    def setnx(self, key: str, value: str) -> bool:
        """Set only if key doesn't exist. Returns True if set."""
        if self.get(key) is not None:
            return False
        self.set(key, value)
        return True

    def expire(self, key: str, seconds: int) -> None:
        entry = self._data.get(key)
        if entry:
            self._data[key] = (entry[0], time.time() + seconds)

_REDIS = FakeRedis()

DEDUP_TTL   = 30    # seconds
LOCK_TTL    = 10    # seconds — how long to hold the processing lock

def _request_key(messages: list[dict], model: str) -> str:
    raw = json.dumps({"model": model, "messages": messages}, sort_keys=True)
    return "dedup:" + hashlib.sha256(raw.encode()).hexdigest()[:20]

def _lock_key(req_key: str) -> str:
    return req_key + ":lock"

def ask_deduped(messages: list[dict], model: str = "claude-haiku-4-5-20251001",
                max_tokens: int = 128) -> tuple[str, str]:
    """Returns (response, source) where source is 'cache' or 'new'."""
    req_key  = _request_key(messages, model)
    lock_key = _lock_key(req_key)

    # Check result cache first
    cached = _REDIS.get(req_key)
    if cached:
        return cached, "cache"

    # Try to acquire lock — if another instance is processing, wait briefly
    acquired = _REDIS.setnx(lock_key, "1")
    if not acquired:
        # Another instance holds the lock — wait and retry from cache
        for _ in range(10):
            time.sleep(0.5)
            cached = _REDIS.get(req_key)
            if cached:
                return cached, "cache_after_wait"
        # Lock expired or timed out — proceed with our own call
        print(f"  [redis-dedup] lock wait expired — making independent call")

    try:
        _REDIS.expire(lock_key, LOCK_TTL)
        r = client.messages.create(model=model, max_tokens=max_tokens, messages=messages)
        result = r.content[0].text.strip()
        # Cache result and release lock
        _REDIS.set(req_key, result, ex=DEDUP_TTL)
        print(f"  [redis-dedup] new call → cached for {DEDUP_TTL}s")
        return result, "new"
    finally:
        # Release lock by expiring it immediately
        _REDIS.expire(lock_key, 0)

# Simulate 3 instances processing the same request
messages = [{"role": "user", "content": "What is a Python context manager?"}]

for i in range(4):
    result, source = ask_deduped(messages)
    print(f"  Instance {i+1} [{source}]: {result[:60]}")
```

**Expected Token Savings:** Redis-based deduplication works across multiple agent instances — critical in horizontally-scaled deployments where a single-process cache would miss cross-instance duplicates; 3 instances each receiving the same burst of 10 requests produce 1 LLM call instead of 30.
**Environment:** Multi-instance production agents deployed behind a load balancer; Redis deduplication is the distributed system equivalent of the in-process asyncio.Lock pattern.

---

### Option 6 — Semantic deduplication: deduplicate near-identical paraphrased questions

```python
import asyncio
import math
import time
import anthropic

client = anthropic.AsyncAnthropic()

def cosine_similarity(a: list[float], b: list[float]) -> float:
    dot   = sum(x * y for x, y in zip(a, b))
    mag_a = math.sqrt(sum(x**2 for x in a))
    mag_b = math.sqrt(sum(x**2 for x in b))
    return dot / (mag_a * mag_b + 1e-9)

def keyword_embed(text: str) -> list[float]:
    """Simplified embedding — replace with real embeddings in production."""
    vocab = ["python", "javascript", "java", "rust", "go", "language", "programming",
             "capital", "country", "city", "france", "germany", "japan",
             "what", "how", "when", "where", "explain", "describe", "define"]
    text_lower = text.lower()
    return [1.0 if w in text_lower else 0.0 for w in vocab]

class SemanticDeduplicator:
    """
    Deduplicates semantically similar (not just identical) questions.
    "What is Python?" and "Can you explain Python?" get the same cached answer.
    """

    def __init__(self, similarity_threshold: float = 0.85, ttl_s: float = 60.0) -> None:
        self._threshold = similarity_threshold
        self._ttl       = ttl_s
        self._entries:  list[dict] = []

    def find_similar(self, query_embed: list[float]) -> str | None:
        now = time.monotonic()
        for entry in self._entries:
            if now > entry["expires_at"]:
                continue
            sim = cosine_similarity(query_embed, entry["embed"])
            if sim >= self._threshold:
                print(f"  [semantic-dedup] similarity={sim:.2f} — returning cached answer")
                return entry["result"]
        return None

    def store(self, query_embed: list[float], result: str) -> None:
        self._entries.append({
            "embed":      query_embed,
            "result":     result,
            "expires_at": time.monotonic() + self._ttl,
        })

_SEM_DEDUP = SemanticDeduplicator(similarity_threshold=0.80, ttl_s=60.0)

async def ask_semantic_deduped(question: str) -> tuple[str, bool]:
    embed = keyword_embed(question)
    cached = _SEM_DEDUP.find_similar(embed)
    if cached:
        return cached, True

    r = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{"role": "user", "content": question}],
    )
    result = r.content[0].text.strip()
    _SEM_DEDUP.store(embed, result)
    print(f"  [semantic-dedup] new call → {r.usage.output_tokens} tokens")
    return result, False

async def main() -> None:
    questions = [
        "What is Python?",
        "Can you explain what Python is?",          # paraphrase → should dedup
        "What programming language is Python?",     # paraphrase → should dedup
        "What is the capital of France?",           # different topic — new call
        "Tell me the capital city of France.",      # paraphrase → should dedup
    ]
    for q in questions:
        result, cached = await ask_semantic_deduped(q)
        status = "CACHE" if cached else "CALL "
        print(f"  [{status}] {q[:50]}: {result[:50]}")

asyncio.run(main())
```

**Expected Token Savings:** Semantic deduplication catches paraphrases that exact-match deduplication misses — for user-facing agents where users rephrase the same question, semantic dedup can reduce LLM calls by 30-50% on common topics; the threshold controls the precision/recall tradeoff.
**Environment:** Customer-facing agents where users naturally rephrase common questions; semantic deduplication is most valuable in high-volume support bots where question clusters are identifiable.

---

## Comparison

| Option | Scope | Cross-Instance | Handles Paraphrases | Best For |
|---|---|---|---|---|
| 1. asyncio.Lock per key | In-process | No | No | Async agents, burst dedup |
| 2. TTL response cache | In-process | No | No | FAQ bots, repeated queries |
| 3. Idempotency keys | API-level | Yes (client-managed) | No | Retry-prone API clients |
| 4. Streaming deduplication | In-process | No | No | Streaming agents with concurrent users |
| 5. Redis distributed lock | Cross-instance | Yes | No | Horizontally-scaled deployments |
| 6. Semantic deduplication | In-process | No | Yes | High-volume support bots, UX-heavy |
