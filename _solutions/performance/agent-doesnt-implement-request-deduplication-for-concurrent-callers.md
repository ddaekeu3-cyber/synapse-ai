---
title: "Agent Doesn't Implement Request Deduplication for Concurrent Callers"
slug: agent-doesnt-implement-request-deduplication-for-concurrent-callers
category: performance
tags: [deduplication, concurrency, cache, coalescing, asyncio, anthropic-sdk]
description: >
  When multiple concurrent callers submit identical or near-identical prompts,
  the agent issues a separate API call for each one instead of coalescing them
  into a single in-flight request. This multiplies token spend, inflates
  latency variance, and can trigger rate limits unnecessarily.
symptoms:
  - API usage spikes when many users ask the same question simultaneously
  - Duplicate requests visible in usage dashboard for identical prompts
  - Rate-limit errors appear during traffic bursts of repeated queries
  - p99 latency is high because slow duplicate calls block behind each other
related_solutions:
  - agent-doesnt-implement-semantic-query-cache-for-similar-requests
  - agent-doesnt-implement-request-batching-for-bulk-inference
  - agent-doesnt-implement-load-shedding-under-overload
---

## Problem

Request deduplication — also called "request coalescing" or "single-flight" —
ensures that N concurrent callers asking the same question share a single
upstream API call. The first caller fires the request; the rest subscribe to
the same future and receive the result when it resolves. This is distinct from
caching: a cached result is from a *previous* call; coalescing collapses
*simultaneous* calls into one. Both are needed: coalescing handles the burst,
caching handles subsequent steady-state requests.

---

## Solution 1 — asyncio.Event Single-Flight (simplest)

Use an `asyncio.Event` as a rendezvous point. The first coroutine to see a key
claims the request; all others `await` the event and read the shared result.

```python
import anthropic
import asyncio
import hashlib
from dataclasses import dataclass, field


@dataclass
class InFlightEntry:
    event:  asyncio.Event = field(default_factory=asyncio.Event)
    result: str | None = None
    error:  Exception | None = None


_in_flight: dict[str, InFlightEntry] = {}
_lock = asyncio.Lock()


def _prompt_key(messages: list, model: str) -> str:
    payload = f"{model}::{messages}"
    return hashlib.md5(payload.encode()).hexdigest()


async def deduped_create(
    messages: list,
    model: str = "claude-sonnet-4-6",
    max_tokens: int = 512,
) -> str:
    key = _prompt_key(messages, model)

    async with _lock:
        if key in _in_flight:
            # Another coroutine is already fetching this
            entry = _in_flight[key]
            waiter = True
        else:
            entry = InFlightEntry()
            _in_flight[key] = entry
            waiter = False

    if waiter:
        await entry.event.wait()
        if entry.error:
            raise entry.error
        return entry.result

    # We are the owner — fire the request
    client = anthropic.AsyncAnthropic()
    try:
        resp = await client.messages.create(
            model=model, max_tokens=max_tokens, messages=messages
        )
        entry.result = resp.content[0].text
    except Exception as e:
        entry.error = e
    finally:
        async with _lock:
            del _in_flight[key]
        entry.event.set()

    if entry.error:
        raise entry.error
    return entry.result


async def simulate_concurrent_callers():
    messages = [{"role": "user", "content": "What is consistent hashing?"}]
    print("Launching 8 concurrent callers with identical prompt...")
    results = await asyncio.gather(
        *[deduped_create(messages) for _ in range(8)],
        return_exceptions=True,
    )
    unique = {r[:40] for r in results if isinstance(r, str)}
    print(f"Unique responses: {len(unique)}  (should be 1 — all shared one API call)")
    print(f"Sample: {results[0][:80]}")


asyncio.run(simulate_concurrent_callers())
```

---

## Solution 2 — asyncio.Future Single-Flight with TTL Cache

Upgrade Solution 1 by using `asyncio.Future` directly and promoting completed
results into a short-lived TTL cache so the next wave of callers (arriving
seconds later) also benefit without a new API call.

```python
import anthropic
import asyncio
import hashlib
import time
from dataclasses import dataclass


@dataclass
class CacheEntry:
    value: str
    expires_at: float


_futures:  dict[str, asyncio.Future] = {}
_cache:    dict[str, CacheEntry]     = {}
_lock = asyncio.Lock()
CACHE_TTL = 30.0   # seconds


def _key(messages: list, model: str) -> str:
    return hashlib.sha256(f"{model}::{messages}".encode()).hexdigest()[:16]


async def single_flight_create(
    messages: list,
    model: str = "claude-sonnet-4-6",
    max_tokens: int = 512,
) -> str:
    key = _key(messages, model)

    async with _lock:
        # 1. Check TTL cache first
        cached = _cache.get(key)
        if cached and time.monotonic() < cached.expires_at:
            print(f"[cache hit] key={key}")
            return cached.value

        # 2. Check in-flight
        if key in _futures:
            fut = _futures[key]
            print(f"[coalesce]  key={key}")
        else:
            fut = asyncio.get_event_loop().create_future()
            _futures[key] = fut
            fut = None   # signal: we are the owner
            print(f"[owner]     key={key}")

    if fut is not None:
        return await asyncio.shield(fut)

    # Owner: issue the real request
    owner_fut = _futures[key]
    client = anthropic.AsyncAnthropic()
    try:
        resp = await client.messages.create(
            model=model, max_tokens=max_tokens, messages=messages
        )
        result = resp.content[0].text
        owner_fut.set_result(result)
        async with _lock:
            _cache[key] = CacheEntry(result, time.monotonic() + CACHE_TTL)
    except Exception as e:
        owner_fut.set_exception(e)
        raise
    finally:
        async with _lock:
            _futures.pop(key, None)

    return result


async def demo():
    q = [{"role": "user", "content": "Define idempotency."}]

    # Wave 1: 6 simultaneous callers
    wave1 = await asyncio.gather(*[single_flight_create(q) for _ in range(6)])
    print(f"\n[wave1] all got same result: {len(set(r[:30] for r in wave1)) == 1}")

    # Wave 2: arrives within TTL — served from cache
    await asyncio.sleep(1)
    wave2 = await asyncio.gather(*[single_flight_create(q) for _ in range(4)])
    print(f"[wave2] cache served: {len(set(r[:30] for r in wave2)) == 1}")


asyncio.run(demo())
```

---

## Solution 3 — Key-Partitioned Singleflight with Stats

Partition the in-flight map by a hash of the request key to reduce lock
contention under high concurrency. Track coalesce counts to measure efficiency.

```python
import anthropic
import asyncio
import hashlib
from dataclasses import dataclass, field
from collections import defaultdict


@dataclass
class ShardStats:
    total_requests:   int = 0
    owner_requests:   int = 0
    coalesced:        int = 0

    @property
    def coalesce_rate(self) -> float:
        if self.total_requests == 0:
            return 0.0
        return self.coalesced / self.total_requests


NUM_SHARDS = 16

_shards: list[dict[str, asyncio.Future]] = [{} for _ in range(NUM_SHARDS)]
_locks:  list[asyncio.Lock]              = [asyncio.Lock() for _ in range(NUM_SHARDS)]
_stats:  list[ShardStats]               = [ShardStats() for _ in range(NUM_SHARDS)]


def _shard_idx(key: str) -> int:
    return int(key[:4], 16) % NUM_SHARDS


def _key(messages: list, model: str) -> str:
    return hashlib.md5(f"{model}::{messages}".encode()).hexdigest()


async def partitioned_dedup_create(
    messages: list,
    model: str = "claude-sonnet-4-6",
    max_tokens: int = 512,
) -> str:
    key   = _key(messages, model)
    shard = _shard_idx(key)
    lock  = _locks[shard]
    store = _shards[shard]
    stats = _stats[shard]

    async with lock:
        stats.total_requests += 1
        if key in store:
            fut = store[key]
            stats.coalesced += 1
            is_owner = False
        else:
            loop = asyncio.get_running_loop()
            fut  = loop.create_future()
            store[key] = fut
            stats.owner_requests += 1
            is_owner = True

    if not is_owner:
        return await asyncio.shield(fut)

    client = anthropic.AsyncAnthropic()
    try:
        resp = await client.messages.create(
            model=model, max_tokens=max_tokens, messages=messages
        )
        result = resp.content[0].text
        fut.set_result(result)
        return result
    except Exception as e:
        fut.set_exception(e)
        raise
    finally:
        async with lock:
            store.pop(key, None)


def global_stats() -> dict:
    total = sum(s.total_requests for s in _stats)
    coalesced = sum(s.coalesced for s in _stats)
    owners = sum(s.owner_requests for s in _stats)
    return {
        "total_requests": total,
        "owner_calls":    owners,
        "coalesced":      coalesced,
        "coalesce_rate":  f"{coalesced / max(total, 1) * 100:.1f}%",
        "api_calls_saved": coalesced,
    }


async def demo_partitioned():
    prompts = [
        [{"role": "user", "content": "Explain CAP theorem."}],
        [{"role": "user", "content": "Explain Paxos."}],
        [{"role": "user", "content": "Explain CAP theorem."}],  # duplicate
    ]

    tasks = []
    for _ in range(3):   # 3 waves
        for p in prompts:
            tasks.append(partitioned_dedup_create(p))

    results = await asyncio.gather(*tasks, return_exceptions=True)
    print(f"Completed {len(results)} requests")
    print(global_stats())


asyncio.run(demo_partitioned())
```

---

## Solution 4 — Streaming Deduplication (Broadcast Fan-Out)

For streaming responses, the first subscriber drives the stream while additional
subscribers receive chunks via an `asyncio.Queue` fan-out. All N callers see
every token in real time from a single upstream stream.

```python
import anthropic
import asyncio
import hashlib
from dataclasses import dataclass, field


SENTINEL = object()


@dataclass
class StreamBroadcast:
    """Drives one upstream stream and fans out to N subscriber queues."""
    chunks: list[str] = field(default_factory=list)
    done: bool = False
    error: Exception | None = None
    _subscribers: list[asyncio.Queue] = field(default_factory=list)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def subscribe(self) -> asyncio.Queue:
        async with self._lock:
            q: asyncio.Queue = asyncio.Queue()
            # Replay already-received chunks for late subscribers
            for chunk in self.chunks:
                await q.put(chunk)
            if self.done:
                await q.put(SENTINEL)
            elif self.error:
                await q.put(self.error)
            else:
                self._subscribers.append(q)
            return q

    async def push(self, chunk: str) -> None:
        async with self._lock:
            self.chunks.append(chunk)
            for q in self._subscribers:
                await q.put(chunk)

    async def finish(self, error: Exception | None = None) -> None:
        async with self._lock:
            self.done = True
            self.error = error
            payload = error if error else SENTINEL
            for q in self._subscribers:
                await q.put(payload)
            self._subscribers.clear()


_broadcasts: dict[str, StreamBroadcast] = {}
_bcast_lock = asyncio.Lock()


def _key(messages: list, model: str) -> str:
    return hashlib.md5(f"{model}::{messages}".encode()).hexdigest()


async def _drive_stream(key: str, messages: list, model: str, max_tokens: int):
    bcast = _broadcasts[key]
    client = anthropic.AsyncAnthropic()
    try:
        async with client.messages.stream(
            model=model, max_tokens=max_tokens, messages=messages
        ) as stream:
            async for chunk in stream.text_stream:
                await bcast.push(chunk)
        await bcast.finish()
    except Exception as e:
        await bcast.finish(error=e)
    finally:
        async with _bcast_lock:
            _broadcasts.pop(key, None)


async def deduped_stream(
    messages: list,
    model: str = "claude-sonnet-4-6",
    max_tokens: int = 512,
):
    """Async generator yielding text chunks; coalesces concurrent callers."""
    key = _key(messages, model)

    async with _bcast_lock:
        if key not in _broadcasts:
            bcast = StreamBroadcast()
            _broadcasts[key] = bcast
            asyncio.create_task(_drive_stream(key, messages, model, max_tokens))
        else:
            bcast = _broadcasts[key]

    queue = await bcast.subscribe()

    while True:
        item = await queue.get()
        if item is SENTINEL:
            return
        if isinstance(item, Exception):
            raise item
        yield item


async def subscriber(name: str, messages: list):
    text = ""
    async for chunk in deduped_stream(messages):
        text += chunk
    print(f"[{name}] received {len(text)} chars: {text[:50]}...")


async def demo_stream_dedup():
    messages = [{"role": "user", "content": "List 3 distributed consensus algorithms with descriptions."}]
    await asyncio.gather(
        subscriber("A", messages),
        subscriber("B", messages),
        subscriber("C", messages),
    )


asyncio.run(demo_stream_dedup())
```

---

## Solution 5 — Probabilistic Key Normalisation Before Dedup

Minor prompt variations (extra spaces, punctuation differences, case) break
exact-match deduplication. Normalize the key with lowercasing, whitespace
collapsing, and optional stop-word removal before hashing so near-identical
prompts share the same in-flight slot.

```python
import anthropic
import asyncio
import hashlib
import re
import string


STOPWORDS = {"a", "an", "the", "is", "are", "what", "which", "please", "tell", "me"}


def normalize_prompt(text: str, remove_stopwords: bool = False) -> str:
    text = text.lower().strip()
    text = re.sub(r'\s+', ' ', text)
    text = text.translate(str.maketrans('', '', string.punctuation))
    if remove_stopwords:
        tokens = [w for w in text.split() if w not in STOPWORDS]
        text = ' '.join(tokens)
    return text


def normalized_key(messages: list, model: str) -> str:
    parts = []
    for m in messages:
        content = m.get("content", "")
        if isinstance(content, str):
            parts.append(f"{m['role']}:{normalize_prompt(content, remove_stopwords=True)}")
        else:
            parts.append(f"{m['role']}:[complex]")
    payload = f"{model}::{'|'.join(parts)}"
    return hashlib.md5(payload.encode()).hexdigest()


_in_flight: dict[str, asyncio.Future] = {}
_lock = asyncio.Lock()


async def normalized_dedup_create(
    messages: list,
    model: str = "claude-sonnet-4-6",
    max_tokens: int = 512,
) -> str:
    key = normalized_key(messages, model)

    async with _lock:
        if key in _in_flight:
            fut = _in_flight[key]
            is_owner = False
        else:
            loop = asyncio.get_running_loop()
            fut = loop.create_future()
            _in_flight[key] = fut
            is_owner = True

    if not is_owner:
        return await asyncio.shield(fut)

    client = anthropic.AsyncAnthropic()
    try:
        resp = await client.messages.create(
            model=model, max_tokens=max_tokens, messages=messages
        )
        result = resp.content[0].text
        fut.set_result(result)
        return result
    except Exception as e:
        fut.set_exception(e)
        raise
    finally:
        async with _lock:
            _in_flight.pop(key, None)


async def demo_normalized():
    # These three prompts should all map to the same key
    variants = [
        [{"role": "user", "content": "What is idempotency?"}],
        [{"role": "user", "content": "what is idempotency"}],
        [{"role": "user", "content": "  What  is  idempotency?  "}],
    ]

    results = await asyncio.gather(*[normalized_dedup_create(v) for v in variants])
    keys = [normalized_key(v, "claude-sonnet-4-6") for v in variants]
    print(f"Keys: {set(keys)} (should be 1 unique key)")
    print(f"Results identical: {len(set(r[:30] for r in results)) == 1}")


asyncio.run(demo_normalized())
```

---

## Solution 6 — Distributed Deduplication via Redis Locks

In a multi-process or multi-pod deployment, process-local in-flight maps don't
help — two pods may both fire identical requests. Use a Redis lock to elect a
single owner across the fleet and store the result in Redis for all waiters.

```python
import anthropic
import asyncio
import hashlib
import json
import time
import redis.asyncio as aioredis


LOCK_TTL    = 30      # seconds — max time a single request can hold the lock
RESULT_TTL  = 60      # seconds — keep result for late arrivals
POLL_INTERVAL = 0.1   # seconds between polls for waiters


def _key(messages: list, model: str) -> str:
    return hashlib.md5(f"{model}::{messages}".encode()).hexdigest()


async def redis_dedup_create(
    messages: list,
    model: str = "claude-sonnet-4-6",
    max_tokens: int = 512,
    redis_url: str = "redis://localhost:6379",
) -> str:
    r = aioredis.from_url(redis_url, decode_responses=True)
    key      = _key(messages, model)
    lock_key = f"dedup:lock:{key}"
    res_key  = f"dedup:result:{key}"

    # Check if result already cached
    cached = await r.get(res_key)
    if cached:
        data = json.loads(cached)
        await r.aclose()
        return data["text"]

    # Try to acquire distributed lock
    acquired = await r.set(lock_key, "1", nx=True, ex=LOCK_TTL)

    if acquired:
        # We are the owner across the fleet
        client = anthropic.AsyncAnthropic()
        try:
            resp = await client.messages.create(
                model=model, max_tokens=max_tokens, messages=messages
            )
            result = resp.content[0].text
            await r.set(res_key, json.dumps({"text": result}), ex=RESULT_TTL)
            return result
        except Exception as e:
            raise
        finally:
            await r.delete(lock_key)
            await r.aclose()
    else:
        # Another pod owns this request — poll for result
        deadline = time.monotonic() + LOCK_TTL
        while time.monotonic() < deadline:
            cached = await r.get(res_key)
            if cached:
                data = json.loads(cached)
                await r.aclose()
                return data["text"]
            # Check if lock expired (owner crashed)
            still_locked = await r.exists(lock_key)
            if not still_locked and not cached:
                # Owner vanished — retry as new owner
                await r.aclose()
                return await redis_dedup_create(messages, model, max_tokens, redis_url)
            await asyncio.sleep(POLL_INTERVAL)

        await r.aclose()
        raise TimeoutError("Distributed dedup: timed out waiting for owner")


# Demo — requires Redis running on localhost:6379
async def demo_redis():
    messages = [{"role": "user", "content": "Explain the two-phase commit protocol."}]
    try:
        results = await asyncio.gather(
            *[redis_dedup_create(messages) for _ in range(4)],
            return_exceptions=True,
        )
        ok = [r for r in results if isinstance(r, str)]
        errs = [r for r in results if isinstance(r, Exception)]
        print(f"[redis-dedup] ok={len(ok)}  errors={len(errs)}")
        if ok:
            print(f"Sample: {ok[0][:80]}")
    except Exception as e:
        print(f"[redis-dedup] Redis not available in demo: {e}")


asyncio.run(demo_redis())
```

---

## Comparison

| Approach | Scope | Streaming | Near-duplicate | Distributed | Complexity |
|---|---|---|---|---|---|
| asyncio.Event single-flight | Single process | No | No | No | Very low |
| asyncio.Future + TTL cache | Single process | No | No | No | Low |
| Partitioned shard map | Single process | No | No | No | Medium |
| Stream broadcast fan-out | Single process | Yes | No | No | Medium |
| Normalized key dedup | Single process | No | Yes | No | Low |
| Redis distributed lock | Multi-process / multi-pod | No | No | Yes | High |

**Rule of thumb:**
- Single-process API server → asyncio.Future + TTL cache (Solution 2) is the sweet spot
- Streaming responses → broadcast fan-out (Solution 4)
- Typo/whitespace variants → normalize before hashing (Solution 5, layered on any other)
- Multi-pod Kubernetes deployment → Redis distributed lock (Solution 6)
