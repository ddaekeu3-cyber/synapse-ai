---
layout: solution
title: "Agent Doesn't Implement Request Deduplication for Identical Concurrent Requests"
category: general
description: "Multiple identical requests arriving simultaneously all execute independently, wasting tokens, quota, and compute when a single execution would suffice."
tags: [general, deduplication, concurrency, caching, efficiency, stampede]
---

## Symptom

Identical requests execute redundantly in parallel:

```python
# 50 users ask "What are today's top news?" simultaneously
# Agent spawns 50 independent LLM calls with identical prompts
async def handle_request(user_id: str, query: str) -> str:
    return await llm_call(query)  # no deduplication

# All 50 requests arrive within 100ms of each other
# Result: 50 × 2,000 tokens = 100,000 tokens consumed
# Optimal: 1 LLM call → share result with all 50 waiters = 2,000 tokens

# Also common with tool calls:
# 10 agent workers all call get_stock_price("AAPL") at the same time
# → 10 identical external API calls, each returning the same value
```

Under load, a small set of popular queries causes a "cache stampede" — dozens of requests all miss the cache simultaneously and each launches an independent LLM call.

## Root Cause

Stateless request handlers with no coordination layer allow identical work to proceed in parallel. Even when a cache exists, requests that arrive before the first one populates the cache all miss simultaneously (thundering herd). Without request coalescing, every concurrent duplicate pays the full execution cost.

## Fix

---

### Option 1: asyncio.Future-Based Request Coalescing

Track in-flight requests by their cache key. New arrivals for the same key wait on the existing Future instead of starting a new execution.

```python
import asyncio
import hashlib
import time
import anthropic

client = anthropic.AsyncAnthropic()

class RequestCoalescer:
    """Coalesces concurrent identical requests into a single execution."""

    def __init__(self):
        self._in_flight: dict[str, asyncio.Future] = {}
        self.stats = {"coalesced": 0, "executed": 0}

    def _key(self, prompt: str, model: str) -> str:
        return hashlib.sha256(f"{model}:{prompt}".encode()).hexdigest()[:20]

    async def get_or_execute(self, prompt: str, model: str = "claude-haiku-4-5-20251001") -> str:
        key = self._key(prompt, model)

        # If already in flight, wait for it
        if key in self._in_flight:
            self.stats["coalesced"] += 1
            return await asyncio.shield(self._in_flight[key])

        # First request: create Future and register it
        future: asyncio.Future[str] = asyncio.get_event_loop().create_future()
        self._in_flight[key] = future

        try:
            response = await client.messages.create(
                model=model,
                max_tokens=512,
                messages=[{"role": "user", "content": prompt}],
            )
            result = response.content[0].text
            future.set_result(result)
            self.stats["executed"] += 1
            return result
        except Exception as e:
            future.set_exception(e)
            raise
        finally:
            # Remove from in-flight after all waiters have been notified
            await asyncio.sleep(0)  # yield so waiters can read the result
            self._in_flight.pop(key, None)

coalescer = RequestCoalescer()

async def handle_user_request(user_id: int, query: str) -> str:
    return await coalescer.get_or_execute(query)

async def main():
    query = "What are the main features of Python asyncio?"

    # 20 users ask the same question simultaneously
    start = time.perf_counter()
    results = await asyncio.gather(*[
        handle_user_request(i, query) for i in range(20)
    ])
    elapsed = time.perf_counter() - start

    print(f"20 requests completed in {elapsed:.2f}s")
    print(f"Coalescer stats: {coalescer.stats}")
    # → executed: 1, coalesced: 19
    # All 20 users got the same result from 1 LLM call
    assert all(r == results[0] for r in results), "All should get same result"

asyncio.run(main())
```

**Expected Token Savings:** 20 concurrent identical requests → 1 LLM call. Saves 19 × 2,000 tokens = 38,000 tokens. For a popular query asked by 100 users simultaneously: saves 99 × full execution cost.
**Environment:** Works within a single process. For multi-process deployments, use a distributed lock (Redis, Option 3) to coordinate across instances.

---

### Option 2: Stampede-Protected Cache — Lock-Based Population

Add a distributed lock around cache population so only one request populates the cache while others wait, preventing stampede on cache expiry.

```python
import asyncio
import hashlib
import time
from typing import Any
import anthropic

client = anthropic.AsyncAnthropic()

class StampedeProtectedCache:
    """Cache with lock-per-key to prevent thundering herd on cache miss."""

    def __init__(self, ttl: float = 300.0):
        self.ttl = ttl
        self._cache: dict[str, tuple[Any, float]] = {}  # key → (value, expires_at)
        self._locks: dict[str, asyncio.Lock] = {}
        self._lock_registry = asyncio.Lock()
        self.stats = {"hits": 0, "misses": 0, "wait_hits": 0}

    async def _get_lock(self, key: str) -> asyncio.Lock:
        async with self._lock_registry:
            if key not in self._locks:
                self._locks[key] = asyncio.Lock()
            return self._locks[key]

    def _get_cached(self, key: str) -> Any | None:
        entry = self._cache.get(key)
        if entry and time.monotonic() < entry[1]:
            return entry[0]
        return None

    async def get_or_compute(self, key: str, compute_fn) -> Any:
        # Fast path: check cache without lock
        cached = self._get_cached(key)
        if cached is not None:
            self.stats["hits"] += 1
            return cached

        # Slow path: acquire per-key lock
        lock = await self._get_lock(key)
        async with lock:
            # Check again inside lock (another coroutine may have populated it)
            cached = self._get_cached(key)
            if cached is not None:
                self.stats["wait_hits"] += 1
                return cached

            # We are the first: compute and cache
            result = await compute_fn()
            self._cache[key] = (result, time.monotonic() + self.ttl)
            self.stats["misses"] += 1
            return result

cache = StampedeProtectedCache(ttl=60.0)

async def llm_call(prompt: str) -> str:
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text

async def handle_request(request_id: int, prompt: str) -> str:
    key = hashlib.sha256(prompt.encode()).hexdigest()[:16]
    return await cache.get_or_compute(
        key,
        compute_fn=lambda: llm_call(prompt),
    )

async def main():
    prompt = "Explain the CAP theorem in one paragraph"

    # Simulate 30 concurrent requests for the same prompt
    start = time.perf_counter()
    results = await asyncio.gather(*[handle_request(i, prompt) for i in range(30)])
    elapsed = time.perf_counter() - start

    print(f"30 requests in {elapsed:.2f}s")
    print(f"Cache stats: {cache.stats}")
    # hits=0, misses=1, wait_hits=29
    # Only 1 LLM call; 29 waited and got the cached result

asyncio.run(main())
```

**Expected Token Savings:** Lock prevents stampede on cache miss — only 1 execution per unique key regardless of concurrent arrivals. For 30 simultaneous misses: saves 29 LLM calls × 1,500 tokens = 43,500 tokens. TTL ensures results are refreshed periodically.
**Environment:** Single-process async. Replace `asyncio.Lock` with a Redis lock (`redlock`) for multi-process deployments. TTL should match result freshness requirements — 60s for dynamic data, 3600s for stable facts.

---

### Option 3: Redis-Based Distributed Deduplication

Use Redis SETNX as a distributed lock so only one instance across all pods executes a given query, with all others waiting for the cached result.

```python
import asyncio
import hashlib
import json
import time
import redis.asyncio as aioredis  # pip install redis
import anthropic

client = anthropic.AsyncAnthropic()
r = aioredis.Redis(host="localhost", port=6379, decode_responses=True)

RESULT_TTL = 300      # 5 minutes for cached results
LOCK_TTL = 30         # 30s max execution time before lock expires
POLL_INTERVAL = 0.1   # Check for result every 100ms

def cache_key(prompt: str, model: str) -> str:
    h = hashlib.sha256(f"{model}:{prompt}".encode()).hexdigest()[:24]
    return f"llm:result:{h}"

def lock_key(prompt: str, model: str) -> str:
    h = hashlib.sha256(f"{model}:{prompt}".encode()).hexdigest()[:24]
    return f"llm:lock:{h}"

async def dedup_llm_call(prompt: str, model: str = "claude-haiku-4-5-20251001") -> str:
    c_key = cache_key(prompt, model)
    l_key = lock_key(prompt, model)

    # Check cache first
    cached = await r.get(c_key)
    if cached:
        return json.loads(cached)["text"]

    # Try to acquire lock (SETNX)
    acquired = await r.set(l_key, "1", nx=True, ex=LOCK_TTL)

    if acquired:
        # We hold the lock — execute and cache
        try:
            response = await client.messages.create(
                model=model,
                max_tokens=512,
                messages=[{"role": "user", "content": prompt}],
            )
            result = response.content[0].text
            await r.setex(c_key, RESULT_TTL, json.dumps({"text": result}))
            return result
        finally:
            await r.delete(l_key)
    else:
        # Another instance is working on this — poll for result
        deadline = time.monotonic() + LOCK_TTL + 5
        while time.monotonic() < deadline:
            cached = await r.get(c_key)
            if cached:
                return json.loads(cached)["text"]
            lock_exists = await r.exists(l_key)
            if not lock_exists:
                # Lock released but no result — retry from scratch
                return await dedup_llm_call(prompt, model)
            await asyncio.sleep(POLL_INTERVAL)
        raise TimeoutError(f"Waited too long for deduplicated result for key {c_key}")

async def main():
    try:
        prompt = "What are the SOLID principles?"
        results = await asyncio.gather(*[dedup_llm_call(prompt) for _ in range(10)])
        print(f"All results identical: {all(r == results[0] for r in results)}")
        print(f"Result preview: {results[0][:100]}...")
    except Exception as e:
        print(f"Redis not available or error: {e}")

asyncio.run(main())
```

**Expected Token Savings:** Redis coordination works across all pods in a cluster. For 10 pods each receiving 5 simultaneous requests for the same query: without dedup = 50 LLM calls; with Redis dedup = 1 LLM call across all pods = 98% reduction. Especially impactful for popular queries that spike across a distributed fleet.
**Environment:** Requires Redis. Lock TTL must exceed worst-case LLM latency. Use Lua scripts for atomic lock-and-result operations in production. Consider Redlock for true distributed locking.

---

### Option 4: Request Queue with Deduplication Map

Buffer all incoming requests in a queue. A single consumer processes requests; duplicates in the queue are merged and their results broadcast.

```python
import asyncio
import hashlib
from dataclasses import dataclass, field
from typing import Any
import anthropic

client = anthropic.AsyncAnthropic()

@dataclass
class QueuedRequest:
    key: str
    prompt: str
    model: str
    futures: list[asyncio.Future] = field(default_factory=list)

class DedupQueue:
    def __init__(self, max_concurrent: int = 3):
        self._pending: dict[str, QueuedRequest] = {}  # key → request
        self._order: list[str] = []  # insertion order
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._new_item = asyncio.Event()
        self.stats = {"requests": 0, "deduped": 0, "executed": 0}

    def _key(self, prompt: str, model: str) -> str:
        return hashlib.sha256(f"{model}:{prompt}".encode()).hexdigest()[:16]

    async def submit(self, prompt: str, model: str) -> Any:
        key = self._key(prompt, model)
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self.stats["requests"] += 1

        if key in self._pending:
            # Deduplicate: attach to existing request
            self._pending[key].futures.append(future)
            self.stats["deduped"] += 1
        else:
            req = QueuedRequest(key=key, prompt=prompt, model=model, futures=[future])
            self._pending[key] = req
            self._order.append(key)
            self._new_item.set()

        return await future

    async def _execute(self, req: QueuedRequest) -> None:
        async with self._semaphore:
            try:
                response = await client.messages.create(
                    model=req.model,
                    max_tokens=512,
                    messages=[{"role": "user", "content": req.prompt}],
                )
                result = response.content[0].text
                self.stats["executed"] += 1
                # Broadcast to all waiters
                for f in req.futures:
                    if not f.done():
                        f.set_result(result)
            except Exception as e:
                for f in req.futures:
                    if not f.done():
                        f.set_exception(e)

    async def run(self) -> None:
        """Consumer loop — run as a background task."""
        while True:
            await self._new_item.wait()
            self._new_item.clear()

            while self._order:
                key = self._order.pop(0)
                req = self._pending.pop(key, None)
                if req:
                    asyncio.create_task(self._execute(req))

queue = DedupQueue(max_concurrent=3)

async def main():
    # Start consumer
    consumer = asyncio.create_task(queue.run())

    prompts = [
        "What is machine learning?",
        "What is machine learning?",  # duplicate
        "What is machine learning?",  # duplicate
        "What is deep learning?",
        "What is deep learning?",     # duplicate
        "What is reinforcement learning?",
    ]

    results = await asyncio.gather(*[
        queue.submit(p, "claude-haiku-4-5-20251001") for p in prompts
    ])

    consumer.cancel()
    print(f"Stats: {queue.stats}")
    # requests=6, deduped=3, executed=3
    # 6 requests → 3 API calls
    print(f"ML answers identical: {results[0] == results[1] == results[2]}")

asyncio.run(main())
```

**Expected Token Savings:** Queue deduplication is most effective under burst load. For 6 requests (3 unique): saves 3 LLM calls × 1,000 tokens = 3,000 tokens. Under a 100-user spike with 5 unique queries: saves 95 LLM calls = significant quota preservation.
**Environment:** Single-process; run consumer as a long-lived background task. `max_concurrent` limits parallel API calls to avoid rate limits. For multi-process: use Redis pub/sub to coordinate result broadcast (Option 3).

---

### Option 5: Idempotency Key with Short-Circuit

Require callers to provide an idempotency key (like Stripe's pattern). Identical keys within a time window short-circuit to the cached result immediately.

```python
import asyncio
import hashlib
import time
from typing import Any
import anthropic

client = anthropic.AsyncAnthropic()

class IdempotencyStore:
    def __init__(self, ttl_seconds: float = 60.0):
        self.ttl = ttl_seconds
        self._store: dict[str, tuple[Any, float]] = {}  # key → (result, expires_at)
        self._in_progress: dict[str, asyncio.Future] = {}

    def _is_valid(self, key: str) -> bool:
        entry = self._store.get(key)
        return bool(entry and time.monotonic() < entry[1])

    async def execute_once(self, idempotency_key: str, coro_fn) -> Any:
        # Exact match: return cached result
        if self._is_valid(idempotency_key):
            return self._store[idempotency_key][0]

        # In progress: wait for it
        if idempotency_key in self._in_progress:
            return await self._in_progress[idempotency_key]

        # New: execute and cache
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._in_progress[idempotency_key] = future

        try:
            result = await coro_fn()
            self._store[idempotency_key] = (result, time.monotonic() + self.ttl)
            future.set_result(result)
            return result
        except Exception as e:
            future.set_exception(e)
            raise
        finally:
            self._in_progress.pop(idempotency_key, None)

store = IdempotencyStore(ttl_seconds=120.0)

async def answer_question(question: str, idempotency_key: str | None = None) -> str:
    # Auto-generate idempotency key from content if not provided
    ikey = idempotency_key or hashlib.sha256(question.encode()).hexdigest()[:20]

    return await store.execute_once(
        ikey,
        lambda: _do_llm_call(question),
    )

async def _do_llm_call(question: str) -> str:
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": question}],
    )
    return response.content[0].text

async def main():
    question = "What is the difference between TCP and UDP?"

    # Simulate: same question from multiple sources simultaneously
    tasks = [
        answer_question(question),                              # auto-keyed
        answer_question(question),                              # same key
        answer_question(question, idempotency_key="tcp-udp"),  # explicit key
        answer_question(question, idempotency_key="tcp-udp"),  # same explicit key
    ]
    results = await asyncio.gather(*tasks)
    print(f"All identical: {len(set(results)) == 1}")
    print(f"Result: {results[0][:100]}...")
    # Only 2 LLM calls (one per unique idempotency key)

asyncio.run(main())
```

**Expected Token Savings:** Idempotency keys enable callers to explicitly control deduplication granularity. Explicit keys allow deduplication across semantically equivalent but textually different requests. For retry-heavy clients: eliminates duplicate calls from network retries (typically 1-3 retries × full cost).
**Environment:** Pattern matches Stripe's idempotency key design. TTL should exceed the maximum retry window of your clients. Persist idempotency store to Redis for cross-restart guarantees.

---

### Option 6: Content-Hash Deduplication with Probabilistic Early Exit

Use a Bloom filter for O(1) duplicate detection before any lock or cache lookup, minimising overhead on the hot path for non-duplicate requests.

```python
import asyncio
import hashlib
import math
import time
from bitarray import bitarray  # pip install bitarray
import anthropic

client = anthropic.AsyncAnthropic()

class BloomFilter:
    """Space-efficient probabilistic duplicate detector."""

    def __init__(self, capacity: int = 10_000, error_rate: float = 0.01):
        # Optimal bit array and hash function count
        self.size = int(-capacity * math.log(error_rate) / math.log(2) ** 2)
        self.hash_count = int(self.size / capacity * math.log(2))
        self.bits = bitarray(self.size)
        self.bits.setall(0)

    def _hashes(self, item: str) -> list[int]:
        hashes = []
        for i in range(self.hash_count):
            h = hashlib.sha256(f"{i}:{item}".encode()).hexdigest()
            hashes.append(int(h, 16) % self.size)
        return hashes

    def add(self, item: str) -> None:
        for h in self._hashes(item):
            self.bits[h] = 1

    def might_contain(self, item: str) -> bool:
        return all(self.bits[h] for h in self._hashes(item))

class HybridDeduplicator:
    """Bloom filter for fast rejection + exact cache for confirmed dedup."""

    def __init__(self, ttl: float = 120.0):
        self.ttl = ttl
        self.bloom = BloomFilter(capacity=50_000, error_rate=0.001)
        self._exact: dict[str, tuple[str, float]] = {}  # key → (result, expires)
        self._in_flight: dict[str, asyncio.Future] = {}
        self.stats = {"bloom_miss": 0, "exact_hit": 0, "in_flight_hit": 0, "executed": 0}

    def _key(self, text: str) -> str:
        return hashlib.sha256(text.encode()).hexdigest()[:24]

    def _get_exact(self, key: str) -> str | None:
        entry = self._exact.get(key)
        if entry and time.monotonic() < entry[1]:
            return entry[0]
        return None

    async def execute(self, prompt: str, model: str = "claude-haiku-4-5-20251001") -> str:
        key = self._key(f"{model}:{prompt}")

        # Bloom filter fast path: if definitely new, skip all other checks
        if not self.bloom.might_contain(key):
            self.stats["bloom_miss"] += 1
            self.bloom.add(key)
            return await self._run_and_cache(key, prompt, model)

        # Bloom says maybe seen: check exact cache
        cached = self._get_exact(key)
        if cached:
            self.stats["exact_hit"] += 1
            return cached

        # Check in-flight
        if key in self._in_flight:
            self.stats["in_flight_hit"] += 1
            return await asyncio.shield(self._in_flight[key])

        return await self._run_and_cache(key, prompt, model)

    async def _run_and_cache(self, key: str, prompt: str, model: str) -> str:
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._in_flight[key] = future
        try:
            response = await client.messages.create(
                model=model,
                max_tokens=512,
                messages=[{"role": "user", "content": prompt}],
            )
            result = response.content[0].text
            self._exact[key] = (result, time.monotonic() + self.ttl)
            self.bloom.add(key)
            future.set_result(result)
            self.stats["executed"] += 1
            return result
        except Exception as e:
            future.set_exception(e)
            raise
        finally:
            await asyncio.sleep(0)
            self._in_flight.pop(key, None)

# Comparison table
"""
| Approach | Scope | Persistence | Overhead | Best For |
|---|---|---|---|---|
| Option 1: Future coalescing | In-process | Session | Minimal | Single-process burst |
| Option 2: Lock-based cache | In-process | TTL | Low | Stampede prevention |
| Option 3: Redis distributed | Multi-process | Persistent | Network | Clustered deployments |
| Option 4: Dedup queue | In-process | Session | Queue | Ordered processing |
| Option 5: Idempotency key | In-process | TTL | Low | Retry deduplication |
| Option 6: Bloom + exact | In-process | TTL | Minimal hot path | High-throughput |
"""

async def main():
    dedup = HybridDeduplicator(ttl=60.0)
    prompt = "Summarise the benefits of caching in distributed systems"

    results = await asyncio.gather(*[dedup.execute(prompt) for _ in range(25)])
    print(f"25 requests, all identical: {len(set(results)) == 1}")
    print(f"Stats: {dedup.stats}")
    # bloom_miss=1, exact_hit=~24, executed=1

asyncio.run(main())
```

**Expected Token Savings:** Bloom filter provides O(1) novelty detection with no lock contention on the hot path. For 25 identical requests: 1 LLM call + 24 cache hits. Savings: 24 × 1,500 tokens = 36,000 tokens. False positive rate of 0.1% means very occasionally an extra LLM call is made — acceptable trade-off for zero false negatives.
**Environment:** Requires `bitarray`. Bloom filter memory: ~1.2MB for 50K capacity at 0.1% error rate. Combine with Redis (Option 3) for cross-process deduplication in production.
