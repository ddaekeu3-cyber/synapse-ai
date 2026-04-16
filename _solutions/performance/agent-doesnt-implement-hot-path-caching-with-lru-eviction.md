---
title: "Agent Doesn't Implement Hot-Path Caching with LRU Eviction"
slug: agent-doesnt-implement-hot-path-caching-with-lru-eviction
category: performance
tags: [caching, lru, performance, hot-path, memory, anthropic-sdk]
description: >
  The agent re-invokes the LLM for every request, including frequently repeated
  queries (FAQ answers, static configuration lookups, common code snippets) that
  never change between calls. A bounded LRU cache on the hot path eliminates
  redundant API calls, cuts latency to microseconds for cache hits, and keeps
  memory bounded by automatically evicting the least-recently-used entries.
symptoms:
  - Usage dashboard shows the same prompt hashes appearing thousands of times per day
  - Simple FAQ-style queries take the same 800 ms as complex generation tasks
  - No distinction between cacheable deterministic queries and non-cacheable creative ones
  - Cache is unbounded (dict) and grows without limit in long-running processes
related_solutions:
  - agent-doesnt-implement-semantic-query-cache-for-similar-requests
  - agent-doesnt-implement-request-deduplication-for-concurrent-callers
  - agent-doesnt-implement-prompt-token-budget-enforcement-per-request
---

## Problem

Not all LLM queries are equal: some (FAQ lookups, fixed template fills, static
code explanations) return the same answer every time and are prime caching
candidates. Others (creative writing, personalised responses, real-time data
lookups) must never be cached. An LRU cache with a configurable capacity and
TTL serves repeated hot-path queries from memory while automatically evicting
cold entries and respecting staleness windows.

---

## Solution 1 — `functools.lru_cache`-style Async LRU

Implement an async-aware LRU cache using an `OrderedDict` that evicts the
least-recently-used entry when capacity is exceeded.

```python
import anthropic
import asyncio
import hashlib
import time
from collections import OrderedDict
from dataclasses import dataclass


@dataclass
class CacheEntry:
    value:      str
    created_at: float
    hits:       int = 0


class AsyncLRUCache:
    def __init__(self, capacity: int = 256, ttl_s: float = 3600.0):
        self._capacity = capacity
        self._ttl      = ttl_s
        self._store: OrderedDict[str, CacheEntry] = OrderedDict()
        self._lock  = asyncio.Lock()
        self.hits   = 0
        self.misses = 0

    def _make_key(self, messages: list, model: str) -> str:
        payload = f"{model}::{messages}"
        return hashlib.sha256(payload.encode()).hexdigest()[:20]

    async def get(self, key: str) -> str | None:
        async with self._lock:
            entry = self._store.get(key)
            if entry is None:
                self.misses += 1
                return None
            if time.monotonic() - entry.created_at > self._ttl:
                del self._store[key]
                self.misses += 1
                return None
            # Move to end (most recently used)
            self._store.move_to_end(key)
            entry.hits += 1
            self.hits += 1
            return entry.value

    async def set(self, key: str, value: str) -> None:
        async with self._lock:
            if key in self._store:
                self._store.move_to_end(key)
            self._store[key] = CacheEntry(value=value, created_at=time.monotonic())
            while len(self._store) > self._capacity:
                evicted_key, _ = self._store.popitem(last=False)

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total else 0.0

    def stats(self) -> dict:
        return {
            "size":     len(self._store),
            "capacity": self._capacity,
            "hits":     self.hits,
            "misses":   self.misses,
            "hit_rate": f"{self.hit_rate:.0%}",
        }


_lru = AsyncLRUCache(capacity=512, ttl_s=1800.0)


async def cached_create(
    messages: list,
    model: str = "claude-sonnet-4-6",
    cacheable: bool = True,
    max_tokens: int = 512,
) -> tuple[str, bool]:
    """Returns (text, cache_hit)."""
    if not cacheable:
        client = anthropic.AsyncAnthropic()
        resp = await client.messages.create(model=model, max_tokens=max_tokens, messages=messages)
        return resp.content[0].text, False

    key = _lru._make_key(messages, model)
    cached = await _lru.get(key)
    if cached is not None:
        return cached, True

    client = anthropic.AsyncAnthropic()
    resp = await client.messages.create(model=model, max_tokens=max_tokens, messages=messages)
    text = resp.content[0].text
    await _lru.set(key, text)
    return text, False


async def demo_lru():
    questions = [
        "What does REST stand for?",
        "Define idempotency.",
        "What does REST stand for?",   # cache hit
        "What does REST stand for?",   # cache hit
        "Define idempotency.",          # cache hit
    ]
    import time as _time
    for q in questions:
        t0 = _time.monotonic()
        text, hit = await cached_create([{"role": "user", "content": q}])
        elapsed_ms = (_time.monotonic() - t0) * 1000
        src = "HIT " if hit else "MISS"
        print(f"[{src}] {elapsed_ms:6.1f}ms  {q[:35]:35s}  {text[:40]}")

    print(f"\nCache stats: {_lru.stats()}")


asyncio.run(demo_lru())
```

---

## Solution 2 — Two-Level LRU (L1 In-Process + L2 Redis)

Use a small, fast in-process LRU for the hottest entries and a larger Redis
LRU as L2. Cache misses flow L1 → L2 → API. L1 evictions fall back to L2
rather than going to the API immediately.

```python
import anthropic
import asyncio
import hashlib
import json
import time
from collections import OrderedDict


class L1Cache:
    """Small in-process LRU."""
    def __init__(self, capacity: int = 64, ttl_s: float = 300.0):
        self._store: OrderedDict = OrderedDict()
        self._capacity = capacity
        self._ttl = ttl_s
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> str | None:
        async with self._lock:
            e = self._store.get(key)
            if not e or time.monotonic() - e["ts"] > self._ttl:
                return None
            self._store.move_to_end(key)
            return e["v"]

    async def set(self, key: str, value: str) -> None:
        async with self._lock:
            self._store[key] = {"v": value, "ts": time.monotonic()}
            self._store.move_to_end(key)
            while len(self._store) > self._capacity:
                self._store.popitem(last=False)


class TwoLevelLRU:
    def __init__(self, redis_url: str = "redis://localhost:6379",
                 l1_cap: int = 64, l2_ttl_s: int = 3600):
        self._l1 = L1Cache(capacity=l1_cap, ttl_s=300.0)
        self._l2_ttl = l2_ttl_s
        self._redis_url = redis_url
        self._redis = None
        self.l1_hits = 0
        self.l2_hits = 0
        self.misses  = 0

    async def _get_redis(self):
        if self._redis is None:
            try:
                import redis.asyncio as aioredis
                self._redis = aioredis.from_url(self._redis_url, decode_responses=True)
            except ImportError:
                pass
        return self._redis

    def _key(self, messages: list, model: str) -> str:
        return "lru2:" + hashlib.md5(f"{model}::{messages}".encode()).hexdigest()

    async def get(self, messages: list, model: str) -> str | None:
        key = self._key(messages, model)
        # L1
        v = await self._l1.get(key)
        if v:
            self.l1_hits += 1
            return v
        # L2 Redis
        r = await self._get_redis()
        if r:
            try:
                raw = await r.get(key)
                if raw:
                    data = json.loads(raw)
                    await self._l1.set(key, data["v"])
                    self.l2_hits += 1
                    return data["v"]
            except Exception:
                pass
        self.misses += 1
        return None

    async def set(self, messages: list, model: str, value: str) -> None:
        key = self._key(messages, model)
        await self._l1.set(key, value)
        r = await self._get_redis()
        if r:
            try:
                await r.set(key, json.dumps({"v": value}), ex=self._l2_ttl)
            except Exception:
                pass

    def stats(self) -> dict:
        total = self.l1_hits + self.l2_hits + self.misses
        return {
            "l1_hits":  self.l1_hits,
            "l2_hits":  self.l2_hits,
            "misses":   self.misses,
            "l1_rate":  f"{self.l1_hits / max(total, 1):.0%}",
            "l2_rate":  f"{self.l2_hits / max(total, 1):.0%}",
        }


_two_level = TwoLevelLRU()


async def two_level_create(messages: list, model: str = "claude-sonnet-4-6") -> str:
    cached = await _two_level.get(messages, model)
    if cached:
        return cached
    client = anthropic.AsyncAnthropic()
    resp = await client.messages.create(model=model, max_tokens=256, messages=messages)
    text = resp.content[0].text
    await _two_level.set(messages, model, text)
    return text


async def demo_two_level():
    q = [{"role": "user", "content": "What is a Bloom filter?"}]
    for i in range(4):
        text = await two_level_create(q)
        print(f"Call {i+1}: {text[:50]}")
    print(_two_level.stats())


asyncio.run(demo_two_level())
```

---

## Solution 3 — Cacheable vs Non-Cacheable Request Classifier

Automatically classify each request as cacheable or non-cacheable based on
prompt characteristics (contains time references, personalised pronouns,
creative keywords) before deciding whether to check the cache.

```python
import anthropic
import asyncio
import hashlib
import re
import time
from collections import OrderedDict


NON_CACHEABLE_PATTERNS = [
    re.compile(r"\b(today|now|current|latest|recent|right now)\b", re.I),
    re.compile(r"\b(my|I am|I'm|your name|my account)\b", re.I),
    re.compile(r"\b(random|surprise|creative|imagine|invent|brainstorm)\b", re.I),
    re.compile(r"\b(weather|stock price|news)\b", re.I),
]

ALWAYS_CACHEABLE_PATTERNS = [
    re.compile(r"\b(define|what is|what does|explain|describe|list)\b", re.I),
    re.compile(r"\b(history of|how does|difference between)\b", re.I),
]


def is_cacheable(messages: list) -> tuple[bool, str]:
    """Returns (cacheable, reason)."""
    text = " ".join(
        m.get("content", "") for m in messages
        if isinstance(m.get("content"), str)
    )
    for p in NON_CACHEABLE_PATTERNS:
        if p.search(text):
            return False, f"matched non-cacheable pattern: {p.pattern[:30]}"
    for p in ALWAYS_CACHEABLE_PATTERNS:
        if p.search(text):
            return True, f"matched cacheable pattern: {p.pattern[:30]}"
    # Default: cache if last user message < 200 chars (likely factual)
    last_user = next(
        (m.get("content", "") for m in reversed(messages) if m.get("role") == "user"),
        ""
    )
    if len(last_user) < 200:
        return True, "short factual query (< 200 chars)"
    return False, "long query — likely requires fresh generation"


class ClassifiedLRU:
    def __init__(self, capacity: int = 256, ttl_s: float = 1800.0):
        self._store: OrderedDict = OrderedDict()
        self._cap = capacity
        self._ttl = ttl_s
        self._lock = asyncio.Lock()
        self.stats_cache = {"hits": 0, "misses": 0, "skipped": 0}

    def _key(self, messages: list, model: str) -> str:
        return hashlib.md5(f"{model}::{messages}".encode()).hexdigest()

    async def get(self, key: str) -> str | None:
        async with self._lock:
            e = self._store.get(key)
            if e and time.monotonic() - e["ts"] < self._ttl:
                self._store.move_to_end(key)
                self.stats_cache["hits"] += 1
                return e["v"]
            self.stats_cache["misses"] += 1
            return None

    async def set(self, key: str, value: str) -> None:
        async with self._lock:
            self._store[key] = {"v": value, "ts": time.monotonic()}
            self._store.move_to_end(key)
            while len(self._store) > self._cap:
                self._store.popitem(last=False)


_classified_lru = ClassifiedLRU()


async def smart_cached_create(messages: list, model: str = "claude-sonnet-4-6") -> tuple[str, str]:
    cacheable, reason = is_cacheable(messages)
    client = anthropic.AsyncAnthropic()

    if cacheable:
        key = _classified_lru._key(messages, model)
        cached = await _classified_lru.get(key)
        if cached:
            return cached, f"cache_hit ({reason})"
        resp = await client.messages.create(model=model, max_tokens=256, messages=messages)
        text = resp.content[0].text
        await _classified_lru.set(key, text)
        return text, f"cache_miss ({reason})"
    else:
        _classified_lru.stats_cache["skipped"] += 1
        resp = await client.messages.create(model=model, max_tokens=256, messages=messages)
        return resp.content[0].text, f"bypassed ({reason})"


async def demo_classifier():
    cases = [
        "What is a hash table?",
        "What is the weather today?",
        "Define eventual consistency.",
        "Tell me something creative and surprising.",
        "What is a hash table?",   # cache hit
    ]
    for q in cases:
        text, source = await smart_cached_create([{"role": "user", "content": q}])
        print(f"[{source[:30]:30s}] {q[:40]:40s} -> {text[:40]}")
    print(f"\nCache stats: {_classified_lru.stats_cache}")


asyncio.run(demo_classifier())
```

---

## Solution 4 — Tiered Capacity LRU (Small Fast + Large Slow)

Maintain two LRU tiers: a tiny "hot" tier (16 entries, no TTL) for the most
frequently accessed items and a larger "warm" tier (512 entries, 1 h TTL).
Hits in the hot tier are served with zero lock contention.

```python
import anthropic
import asyncio
import hashlib
import time
from collections import OrderedDict


class TieredLRU:
    def __init__(self, hot_cap: int = 16, warm_cap: int = 512, warm_ttl_s: float = 3600.0):
        self._hot:  OrderedDict = OrderedDict()
        self._warm: OrderedDict = OrderedDict()
        self._hot_cap  = hot_cap
        self._warm_cap = warm_cap
        self._warm_ttl = warm_ttl_s
        self._lock = asyncio.Lock()
        self.hot_hits = 0
        self.warm_hits = 0
        self.misses = 0

    async def get(self, key: str) -> str | None:
        async with self._lock:
            # Check hot tier first
            if key in self._hot:
                self._hot.move_to_end(key)
                self.hot_hits += 1
                return self._hot[key]["v"]
            # Check warm tier
            e = self._warm.get(key)
            if e and time.monotonic() - e["ts"] < self._warm_ttl:
                self._warm.move_to_end(key)
                # Promote to hot
                self._hot[key] = {"v": e["v"]}
                self._hot.move_to_end(key)
                if len(self._hot) > self._hot_cap:
                    evicted_k, evicted_v = self._hot.popitem(last=False)
                    # Demote back to warm
                    self._warm[evicted_k] = {"v": evicted_v["v"], "ts": time.monotonic()}
                    self._warm.move_to_end(evicted_k)
                self.warm_hits += 1
                return e["v"]
            self.misses += 1
            return None

    async def set(self, key: str, value: str) -> None:
        async with self._lock:
            # Write to warm tier
            self._warm[key] = {"v": value, "ts": time.monotonic()}
            self._warm.move_to_end(key)
            if len(self._warm) > self._warm_cap:
                self._warm.popitem(last=False)

    def stats(self) -> dict:
        total = self.hot_hits + self.warm_hits + self.misses
        return {
            "hot_hits":  self.hot_hits,
            "warm_hits": self.warm_hits,
            "misses":    self.misses,
            "hot_size":  len(self._hot),
            "warm_size": len(self._warm),
            "hit_rate":  f"{(self.hot_hits + self.warm_hits) / max(total, 1):.0%}",
        }


_tiered = TieredLRU(hot_cap=16, warm_cap=512)


async def tiered_create(messages: list, model: str = "claude-sonnet-4-6") -> str:
    key = hashlib.md5(f"{model}::{messages}".encode()).hexdigest()
    cached = await _tiered.get(key)
    if cached:
        return cached
    client = anthropic.AsyncAnthropic()
    resp = await client.messages.create(model=model, max_tokens=256, messages=messages)
    text = resp.content[0].text
    await _tiered.set(key, text)
    return text


async def demo_tiered():
    # Access same 3 questions repeatedly to fill hot tier
    questions = [
        [{"role": "user", "content": "What is CAP theorem?"}],
        [{"role": "user", "content": "Define Paxos."}],
        [{"role": "user", "content": "What is Raft?"}],
    ]
    for _ in range(3):
        for q in questions:
            await tiered_create(q)
    print(_tiered.stats())


asyncio.run(demo_tiered())
```

---

## Solution 5 — LRU Cache with Cache-Aside Pattern and Background Refresh

Pre-warm cache entries for known popular queries in the background. When a
cached entry is about to expire, refresh it asynchronously so callers never
experience a cold miss.

```python
import anthropic
import asyncio
import hashlib
import time
from collections import OrderedDict
from dataclasses import dataclass, field


REFRESH_BEFORE_EXPIRY_S = 300.0   # refresh if TTL < this


@dataclass
class Entry:
    value:       str
    created_at:  float
    refreshing:  bool = False


class RefreshingLRU:
    def __init__(self, capacity: int = 256, ttl_s: float = 1800.0):
        self._store: OrderedDict[str, Entry] = OrderedDict()
        self._cap = capacity
        self._ttl = ttl_s
        self._lock = asyncio.Lock()

    def _key(self, messages: list, model: str) -> str:
        return hashlib.md5(f"{model}::{messages}".encode()).hexdigest()

    def _is_expired(self, e: Entry) -> bool:
        return time.monotonic() - e.created_at > self._ttl

    def _is_near_expiry(self, e: Entry) -> bool:
        remaining = self._ttl - (time.monotonic() - e.created_at)
        return 0 < remaining < REFRESH_BEFORE_EXPIRY_S

    async def get(self, key: str) -> str | None:
        async with self._lock:
            e = self._store.get(key)
            if not e or self._is_expired(e):
                return None
            self._store.move_to_end(key)
            return e.value

    async def set(self, key: str, value: str) -> None:
        async with self._lock:
            self._store[key] = Entry(value=value, created_at=time.monotonic())
            self._store.move_to_end(key)
            while len(self._store) > self._cap:
                self._store.popitem(last=False)

    async def _background_refresh(self, key: str, messages: list, model: str) -> None:
        client = anthropic.AsyncAnthropic()
        try:
            resp = await client.messages.create(model=model, max_tokens=256, messages=messages)
            await self.set(key, resp.content[0].text)
            print(f"[refresh] key={key[:8]} refreshed")
        except Exception as e:
            print(f"[refresh] key={key[:8]} failed: {e}")
        finally:
            async with self._lock:
                e = self._store.get(key)
                if e:
                    e.refreshing = False

    async def get_or_refresh(
        self, messages: list, model: str = "claude-sonnet-4-6"
    ) -> str | None:
        key = self._key(messages, model)
        async with self._lock:
            e = self._store.get(key)
            if e and not self._is_expired(e):
                if self._is_near_expiry(e) and not e.refreshing:
                    e.refreshing = True
                    asyncio.create_task(self._background_refresh(key, messages, model))
                return e.value
        return None


_refreshing = RefreshingLRU(capacity=256, ttl_s=3600.0)


async def cache_aside_create(messages: list, model: str = "claude-sonnet-4-6") -> tuple[str, str]:
    key = _refreshing._key(messages, model)
    cached = await _refreshing.get_or_refresh(messages, model)
    if cached:
        return cached, "cache_hit"
    client = anthropic.AsyncAnthropic()
    resp = await client.messages.create(model=model, max_tokens=256, messages=messages)
    text = resp.content[0].text
    await _refreshing.set(key, text)
    return text, "cache_miss"


async def demo_cache_aside():
    q = [{"role": "user", "content": "What is eventual consistency?"}]
    for i in range(3):
        text, src = await cache_aside_create(q)
        print(f"[{src}] call {i+1}: {text[:50]}")


asyncio.run(demo_cache_aside())
```

---

## Solution 6 — Model-Specific LRU with Per-Model Hit Rate Tracking

Maintain a separate LRU cache per model tier. Haiku has a larger cache (cheaper
to populate); Opus has a smaller one (expensive, only cache if used often).
Track hit rates per model to guide capacity tuning.

```python
import anthropic
import asyncio
import hashlib
import time
from collections import OrderedDict


MODEL_CACHE_CONFIG = {
    "claude-haiku-4-5-20251001": {"capacity": 1024, "ttl_s": 7200.0},
    "claude-sonnet-4-6":          {"capacity": 512,  "ttl_s": 3600.0},
    "claude-opus-4-6":            {"capacity": 128,  "ttl_s": 1800.0},
}


class PerModelLRU:
    def __init__(self):
        self._caches: dict[str, dict] = {
            model: {
                "store":  OrderedDict(),
                "hits":   0,
                "misses": 0,
                "config": cfg,
            }
            for model, cfg in MODEL_CACHE_CONFIG.items()
        }
        self._lock = asyncio.Lock()

    def _key(self, messages: list) -> str:
        return hashlib.md5(str(messages).encode()).hexdigest()

    async def get(self, messages: list, model: str) -> str | None:
        c = self._caches.get(model)
        if not c:
            return None
        key = self._key(messages)
        async with self._lock:
            e = c["store"].get(key)
            if e and time.monotonic() - e["ts"] < c["config"]["ttl_s"]:
                c["store"].move_to_end(key)
                c["hits"] += 1
                return e["v"]
            c["misses"] += 1
            return None

    async def set(self, messages: list, model: str, value: str) -> None:
        c = self._caches.get(model)
        if not c:
            return
        key = self._key(messages)
        async with self._lock:
            c["store"][key] = {"v": value, "ts": time.monotonic()}
            c["store"].move_to_end(key)
            while len(c["store"]) > c["config"]["capacity"]:
                c["store"].popitem(last=False)

    def stats(self) -> dict:
        result = {}
        for model, c in self._caches.items():
            total = c["hits"] + c["misses"]
            result[model] = {
                "size":     len(c["store"]),
                "capacity": c["config"]["capacity"],
                "hit_rate": f"{c['hits'] / max(total, 1):.0%}",
                "hits":     c["hits"],
                "misses":   c["misses"],
            }
        return result


_per_model = PerModelLRU()


async def model_lru_create(
    messages: list,
    model: str = "claude-sonnet-4-6",
    max_tokens: int = 256,
) -> tuple[str, bool]:
    cached = await _per_model.get(messages, model)
    if cached:
        return cached, True
    client = anthropic.AsyncAnthropic()
    resp = await client.messages.create(model=model, max_tokens=max_tokens, messages=messages)
    text = resp.content[0].text
    await _per_model.set(messages, model, text)
    return text, False


async def demo_per_model():
    cases = [
        ("What is a hash?", "claude-haiku-4-5-20251001"),
        ("What is a hash?", "claude-sonnet-4-6"),
        ("What is a hash?", "claude-haiku-4-5-20251001"),  # hit
        ("What is a hash?", "claude-sonnet-4-6"),           # hit
    ]
    for q, model in cases:
        text, hit = await model_lru_create([{"role": "user", "content": q}], model=model)
        print(f"[{'HIT' if hit else 'MISS'}] [{model.split('-')[1]:6s}] {text[:40]}")

    print("\nPer-model cache stats:")
    for model, s in _per_model.stats().items():
        print(f"  {model}: {s}")


asyncio.run(demo_per_model())
```

---

## Comparison

| Approach | Eviction policy | TTL | Memory bound | Multi-level | Complexity |
|---|---|---|---|---|---|
| Async LRU (OrderedDict) | LRU | Yes | Yes (capacity) | No | Very low |
| Two-level L1+L2 Redis | LRU per level | Yes | Yes | Yes | Medium |
| Classified cacheable vs not | LRU | Yes | Yes | No | Low |
| Tiered hot/warm | LRU per tier | Warm only | Yes | Yes (in-process) | Medium |
| Background refresh LRU | LRU | Yes | Yes | No | Medium |
| Per-model LRU | LRU per model | Yes (per model) | Yes | No | Low |

**Rule of thumb:**
- Single-process service → async LRU (Solution 1) is sufficient and has near-zero overhead
- Multi-pod deployment → two-level L1+Redis (Solution 2) for cross-pod cache sharing
- Mixed cacheable/non-cacheable traffic → classifier (Solution 3) to avoid polluting cache with uncacheable queries
- Latency-critical hot paths → tiered hot/warm (Solution 4) for the fastest possible hit path
- Long TTLs with freshness requirements → background refresh (Solution 5) to avoid cold misses at expiry
- Multiple model tiers → per-model LRU (Solution 6) to tune capacity to cost per model
