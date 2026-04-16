---
title: "Agent Doesn't Implement Write-Through Cache for Tool State"
description: "Agents that read tool state from the database on every call pay unnecessary latency, but naive in-memory caches silently serve stale data after writes. A write-through cache keeps the cache consistent by updating it synchronously on every write."
difficulty: intermediate
category: performance
tags: [cache, write-through, invalidation, redis, consistency, tool-state, performance]
---

## Problem

Agents frequently read the same tool configuration, user preferences, or shared state on every request. Caching reads is the obvious fix, but write-behind or cache-aside patterns create windows where the cache serves stale data. A write-through cache eliminates staleness by writing to both the cache and the backing store atomically on every mutation.

```python
# Broken: cache-aside with no write-through → stale reads after update
_cache: dict[str, dict] = {}

async def get_tool_config(tool_id: str) -> dict:
    if tool_id in _cache:
        return _cache[tool_id]       # stale if another process wrote to DB
    config = await db.fetch("SELECT * FROM tool_config WHERE id = $1", tool_id)
    _cache[tool_id] = config
    return config

async def update_tool_config(tool_id: str, updates: dict):
    await db.execute("UPDATE tool_config SET data = $1 WHERE id = $2",
                     updates, tool_id)
    # Cache NOT updated → stale until TTL expires
```

---

## Solution 1: Simple Write-Through Cache with TTL

```python
import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable

@dataclass
class CacheEntry:
    value: Any
    written_at: float = field(default_factory=time.monotonic)
    ttl: float = 60.0

    @property
    def is_expired(self) -> bool:
        return time.monotonic() - self.written_at > self.ttl

class WriteThroughCache:
    """
    Write-through cache: every write updates both cache and backing store.
    Reads are always served from cache (no miss after first population).
    """

    def __init__(self,
                 read_fn: Callable[[str], Awaitable[Any]],
                 write_fn: Callable[[str, Any], Awaitable[None]],
                 default_ttl: float = 60.0):
        self._store: dict[str, CacheEntry] = {}
        self._read_fn = read_fn     # load from DB / external store
        self._write_fn = write_fn   # persist to DB / external store
        self._default_ttl = default_ttl
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> Any | None:
        async with self._lock:
            entry = self._store.get(key)
            if entry and not entry.is_expired:
                return entry.value

        # Cache miss or expired: load from backing store
        value = await self._read_fn(key)
        async with self._lock:
            self._store[key] = CacheEntry(value=value, ttl=self._default_ttl)
        return value

    async def set(self, key: str, value: Any,
                  ttl: float | None = None) -> None:
        """Write-through: update both cache and backing store atomically."""
        # Write to backing store first
        await self._write_fn(key, value)
        # Then update cache (only on successful write)
        async with self._lock:
            self._store[key] = CacheEntry(
                value=value,
                ttl=ttl if ttl is not None else self._default_ttl
            )

    async def delete(self, key: str) -> None:
        """Invalidate cache and remove from backing store."""
        await self._write_fn(key, None)   # None signals deletion to write_fn
        async with self._lock:
            self._store.pop(key, None)

    async def invalidate(self, key: str) -> None:
        """Evict from cache without touching backing store."""
        async with self._lock:
            self._store.pop(key, None)

    def stats(self) -> dict:
        now = time.monotonic()
        entries = list(self._store.values())
        return {
            "total_entries": len(entries),
            "expired": sum(1 for e in entries if e.is_expired),
            "live": sum(1 for e in entries if not e.is_expired),
        }

# Usage
async def demo():
    db_store: dict[str, Any] = {}

    async def db_read(key: str) -> Any:
        return db_store.get(key)

    async def db_write(key: str, value: Any):
        if value is None:
            db_store.pop(key, None)
        else:
            db_store[key] = value

    cache = WriteThroughCache(db_read, db_write, default_ttl=300.0)

    await cache.set("tool:web_search:config", {"max_results": 10, "timeout": 5})
    config = await cache.get("tool:web_search:config")  # served from cache
    print(config)
```

---

## Solution 2: Write-Through Cache with Optimistic Locking (Version Vector)

```python
import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

@dataclass
class VersionedEntry:
    value: Any
    version: int
    written_at: float = field(default_factory=time.monotonic)

class OptimisticWriteThroughCache:
    """
    Version-tracked write-through cache.
    Concurrent writers use CAS (compare-and-swap) semantics:
    a write only succeeds if the writer's expected version matches current version.
    """

    def __init__(self):
        self._store: dict[str, VersionedEntry] = {}
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> tuple[Any, int]:
        """Returns (value, version). Version used for optimistic writes."""
        async with self._lock:
            entry = self._store.get(key)
            if entry:
                return entry.value, entry.version
        return None, 0

    async def set_if_version(self, key: str, value: Any,
                              expected_version: int,
                              write_fn: Any = None) -> bool:
        """
        CAS write: only succeeds if current version == expected_version.
        Returns True on success, False if version conflict.
        """
        async with self._lock:
            entry = self._store.get(key)
            current_version = entry.version if entry else 0

            if current_version != expected_version:
                return False  # stale write rejected

            new_version = current_version + 1
            # Persist to backing store before updating cache
            if write_fn:
                await write_fn(key, value, new_version)
            self._store[key] = VersionedEntry(value=value, version=new_version)
            return True

    async def force_set(self, key: str, value: Any,
                        write_fn: Any = None) -> int:
        """Unconditional write. Returns new version."""
        async with self._lock:
            entry = self._store.get(key)
            new_version = (entry.version + 1) if entry else 1
            if write_fn:
                await write_fn(key, value, new_version)
            self._store[key] = VersionedEntry(value=value, version=new_version)
            return new_version

async def demo_optimistic():
    cache = OptimisticWriteThroughCache()
    await cache.force_set("tool:config", {"rate_limit": 100})

    value, version = await cache.get("tool:config")

    # Two concurrent writers both read version 1
    ok1 = await cache.set_if_version("tool:config", {"rate_limit": 150}, version)
    ok2 = await cache.set_if_version("tool:config", {"rate_limit": 200}, version)
    print(f"Writer 1 succeeded: {ok1}")  # True
    print(f"Writer 2 succeeded: {ok2}")  # False (conflict)
```

---

## Solution 3: Redis Write-Through Cache for Multi-Process Agents

```python
import asyncio
import json
import time
from typing import Any, Callable, Awaitable

# Requires: pip install redis[asyncio]
import redis.asyncio as aioredis

class RedisWriteThroughCache:
    """
    Write-through cache backed by Redis.
    Multiple agent processes share the same cache — no stale reads
    across process boundaries.
    """

    def __init__(self, redis_url: str,
                 db_read: Callable[[str], Awaitable[Any]],
                 db_write: Callable[[str, Any], Awaitable[None]],
                 key_prefix: str = "wt:",
                 default_ttl: int = 300):
        self._redis_url = redis_url
        self._db_read = db_read
        self._db_write = db_write
        self._prefix = key_prefix
        self._ttl = default_ttl
        self._redis: aioredis.Redis | None = None

    async def connect(self):
        self._redis = await aioredis.from_url(self._redis_url)

    def _key(self, key: str) -> str:
        return f"{self._prefix}{key}"

    async def get(self, key: str) -> Any | None:
        raw = await self._redis.get(self._key(key))
        if raw is not None:
            return json.loads(raw)
        # Cache miss: load from DB and populate cache
        value = await self._db_read(key)
        if value is not None:
            await self._redis.setex(
                self._key(key), self._ttl, json.dumps(value)
            )
        return value

    async def set(self, key: str, value: Any,
                  ttl: int | None = None) -> None:
        """Write-through: DB first, then cache."""
        await self._db_write(key, value)
        await self._redis.setex(
            self._key(key),
            ttl if ttl is not None else self._ttl,
            json.dumps(value)
        )

    async def delete(self, key: str) -> None:
        await self._db_write(key, None)
        await self._redis.delete(self._key(key))

    async def multi_get(self, keys: list[str]) -> dict[str, Any]:
        """Batch get with pipeline for efficiency."""
        rkeys = [self._key(k) for k in keys]
        async with self._redis.pipeline() as pipe:
            for rk in rkeys:
                pipe.get(rk)
            results = await pipe.execute()

        output: dict[str, Any] = {}
        missing: list[str] = []
        for key, raw in zip(keys, results):
            if raw is not None:
                output[key] = json.loads(raw)
            else:
                missing.append(key)

        if missing:
            db_results = await asyncio.gather(
                *[self._db_read(k) for k in missing]
            )
            async with self._redis.pipeline() as pipe:
                for key, value in zip(missing, db_results):
                    if value is not None:
                        output[key] = value
                        pipe.setex(self._key(key), self._ttl, json.dumps(value))
                await pipe.execute()

        return output

    async def multi_set(self, items: dict[str, Any]) -> None:
        """Batch write-through."""
        await asyncio.gather(*[self._db_write(k, v) for k, v in items.items()])
        async with self._redis.pipeline() as pipe:
            for key, value in items.items():
                pipe.setex(self._key(key), self._ttl, json.dumps(value))
            await pipe.execute()
```

---

## Solution 4: Write-Through Cache with Tag-Based Invalidation

```python
import asyncio
from collections import defaultdict
from typing import Any, Callable, Awaitable

class TaggedWriteThroughCache:
    """
    Each cache entry can be associated with tags.
    Invalidating a tag evicts all entries with that tag.
    Useful for cascading invalidation: e.g., invalidate all
    tool configs for a given user when the user is deleted.
    """

    def __init__(self, write_fn: Callable[[str, Any], Awaitable[None]],
                 read_fn: Callable[[str], Awaitable[Any]]):
        self._store: dict[str, Any] = {}
        self._tags: dict[str, set[str]] = defaultdict(set)  # tag → {keys}
        self._key_tags: dict[str, set[str]] = defaultdict(set)  # key → {tags}
        self._write_fn = write_fn
        self._read_fn = read_fn
        self._lock = asyncio.Lock()

    async def set(self, key: str, value: Any,
                  tags: list[str] | None = None) -> None:
        await self._write_fn(key, value)
        async with self._lock:
            self._store[key] = value
            for tag in (tags or []):
                self._tags[tag].add(key)
                self._key_tags[key].add(tag)

    async def get(self, key: str) -> Any | None:
        async with self._lock:
            if key in self._store:
                return self._store[key]
        value = await self._read_fn(key)
        if value is not None:
            async with self._lock:
                self._store[key] = value
        return value

    async def invalidate_tag(self, tag: str) -> int:
        """Evict all cache entries associated with `tag`. Returns count evicted."""
        async with self._lock:
            keys_to_evict = set(self._tags.pop(tag, set()))
            for key in keys_to_evict:
                self._store.pop(key, None)
                self._key_tags.get(key, set()).discard(tag)
            return len(keys_to_evict)

    async def invalidate_key(self, key: str) -> None:
        async with self._lock:
            self._store.pop(key, None)
            for tag in self._key_tags.pop(key, set()):
                self._tags.get(tag, set()).discard(key)

# Usage
async def demo_tags():
    store: dict[str, Any] = {}

    cache = TaggedWriteThroughCache(
        write_fn=lambda k, v: asyncio.coroutine(lambda: store.update({k: v}))(),
        read_fn=lambda k: asyncio.coroutine(lambda: store.get(k))()
    )

    # Tag all user-1 configs together
    await cache.set("tool:search:user-1", {"limit": 10}, tags=["user:user-1"])
    await cache.set("tool:code:user-1", {"lang": "python"}, tags=["user:user-1"])
    await cache.set("tool:search:user-2", {"limit": 5}, tags=["user:user-2"])

    # When user-1 is deleted, invalidate everything tagged with user:user-1
    evicted = await cache.invalidate_tag("user:user-1")
    print(f"Evicted {evicted} entries for user-1")  # 2
```

---

## Solution 5: Write-Through with Read Coalescing

```python
import asyncio
from typing import Any, Callable, Awaitable

class CoalescingWriteThroughCache:
    """
    Combines write-through with read coalescing:
    concurrent reads for the same missing key share one DB fetch
    instead of each issuing a separate query.
    """

    def __init__(self, read_fn: Callable[[str], Awaitable[Any]],
                 write_fn: Callable[[str, Any], Awaitable[None]]):
        self._store: dict[str, Any] = {}
        self._in_flight: dict[str, asyncio.Future] = {}
        self._read_fn = read_fn
        self._write_fn = write_fn
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> Any | None:
        async with self._lock:
            # Cache hit
            if key in self._store:
                return self._store[key]
            # Already fetching: share the in-flight future
            if key in self._in_flight:
                fut = self._in_flight[key]
            else:
                fut = asyncio.get_event_loop().create_future()
                self._in_flight[key] = fut

        if not fut.done():
            # We are the "owner" of this fetch
            try:
                value = await self._read_fn(key)
                async with self._lock:
                    self._store[key] = value
                    self._in_flight.pop(key, None)
                fut.set_result(value)
            except Exception as e:
                async with self._lock:
                    self._in_flight.pop(key, None)
                fut.set_exception(e)
                raise

        return await asyncio.shield(fut)

    async def set(self, key: str, value: Any) -> None:
        """Write-through: persist first, then update cache."""
        await self._write_fn(key, value)
        async with self._lock:
            self._store[key] = value
            # Resolve any waiters with the new value
            if key in self._in_flight:
                fut = self._in_flight.pop(key)
                if not fut.done():
                    fut.set_result(value)
```

---

## Solution 6: Layered Write-Through Cache (L1 In-Process + L2 Redis)

```python
import asyncio
import json
import time
from typing import Any, Callable, Awaitable

class LayeredWriteThroughCache:
    """
    L1: in-process dict with short TTL (microsecond access)
    L2: Redis with longer TTL (shared across processes)
    Backing store: database

    Write-through at every layer: write propagates L1 → L2 → DB atomically.
    Read cascade: L1 miss → L2 → DB, with back-fill at each layer.
    """

    def __init__(self,
                 redis,
                 db_read: Callable[[str], Awaitable[Any]],
                 db_write: Callable[[str, Any], Awaitable[None]],
                 l1_ttl: float = 5.0,
                 l2_ttl: int = 300):
        self._l1: dict[str, tuple[Any, float]] = {}  # key → (value, expires_at)
        self._redis = redis
        self._db_read = db_read
        self._db_write = db_write
        self._l1_ttl = l1_ttl
        self._l2_ttl = l2_ttl
        self._lock = asyncio.Lock()

    def _l1_get(self, key: str) -> tuple[bool, Any]:
        entry = self._l1.get(key)
        if entry and time.monotonic() < entry[1]:
            return True, entry[0]
        return False, None

    def _l1_set(self, key: str, value: Any):
        self._l1[key] = (value, time.monotonic() + self._l1_ttl)

    async def _l2_get(self, key: str) -> tuple[bool, Any]:
        raw = await self._redis.get(f"l2:{key}")
        if raw:
            return True, json.loads(raw)
        return False, None

    async def _l2_set(self, key: str, value: Any):
        await self._redis.setex(f"l2:{key}", self._l2_ttl, json.dumps(value))

    async def get(self, key: str) -> Any | None:
        # L1
        hit, value = self._l1_get(key)
        if hit:
            return value

        # L2
        hit, value = await self._l2_get(key)
        if hit:
            self._l1_set(key, value)
            return value

        # DB
        value = await self._db_read(key)
        if value is not None:
            self._l1_set(key, value)
            await self._l2_set(key, value)
        return value

    async def set(self, key: str, value: Any) -> None:
        """Write-through all layers: DB → L2 → L1."""
        await self._db_write(key, value)          # 1. persist
        await self._l2_set(key, value)            # 2. warm L2
        self._l1_set(key, value)                  # 3. warm L1

    async def delete(self, key: str) -> None:
        """Invalidate all layers."""
        await self._db_write(key, None)
        await self._redis.delete(f"l2:{key}")
        async with self._lock:
            self._l1.pop(key, None)

    def l1_stats(self) -> dict:
        now = time.monotonic()
        total = len(self._l1)
        live = sum(1 for _, exp in self._l1.values() if exp > now)
        return {"l1_total": total, "l1_live": live, "l1_expired": total - live}
```

---

## Comparison

| Solution | Consistency | Multi-Process | Invalidation | Concurrent Safety | Best For |
|---|---|---|---|---|---|
| 1. Simple write-through + TTL | Strong | No | TTL expiry | asyncio.Lock | Single-process agents |
| 2. Optimistic + version CAS | Strong | Depends | Version conflict | asyncio.Lock | Concurrent writers |
| 3. Redis write-through | Strong | Yes | Key delete | Redis atomic | Multi-process / multi-host |
| 4. Tag-based invalidation | Strong | No | Tag sweep | asyncio.Lock | Cascading invalidation |
| 5. Read coalescing | Strong | No | Manual | Future sharing | High-concurrency cold starts |
| 6. Layered L1+L2+DB | Strong | Yes (L2 shared) | All layers | asyncio.Lock | Production, latency-sensitive |

**Key principle**: in a write-through cache, the cache is always updated *after* a successful write to the backing store — never before. If the backing store write fails, the cache is not updated, preserving consistency. This is the inverse of write-behind (async write), which risks data loss, and simpler than write-around (skip cache on write), which causes cold reads after updates.
