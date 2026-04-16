---
title: "Agent Doesn't Implement Async Cache Invalidation"
description: "Agents with in-process caches serve stale data indefinitely after the underlying source changes — async cache invalidation broadcasts updates to all agent instances so stale entries are evicted the moment the source of truth changes."
difficulty: intermediate
category: concurrency
tags: [concurrency, cache, invalidation, pubsub, redis, async, distributed]
---

# Agent Doesn't Implement Async Cache Invalidation

## Problem

A cached tool result for "get_user_plan" returns "free" even after the user upgrades to "pro" — because the cache has a 5-minute TTL and no invalidation mechanism. In a multi-instance deployment, all instances serve stale data until TTL expires. Without async invalidation, the only options are very short TTLs (high cache miss rate) or manual cache clears (operationally brittle). Async invalidation lets the authoritative source push eviction events immediately when data changes.

**Symptoms:**
- Users see old plan/permission data minutes after an upgrade
- Configuration changes don't take effect until cache TTL expires
- Feature flag changes require waiting for TTL or a service restart
- Multi-instance deployments have inconsistent cached state
- Forced cache-busting workarounds (TTL=0 for "important" calls) eliminate all caching benefit

---

## Solution 1: In-Process Invalidation with asyncio.Event

Use an `asyncio.Event` per cache key; waiters are woken when a key is invalidated.

```python
import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class CacheEntry:
    value: Any
    cached_at: float
    ttl: float  # seconds; 0 = never expire without explicit invalidation

    def is_expired(self) -> bool:
        return self.ttl > 0 and (time.time() - self.cached_at) > self.ttl


class InvalidatableCache:
    def __init__(self):
        self._store: dict[str, CacheEntry] = {}
        self._events: dict[str, asyncio.Event] = {}  # key -> invalidation event
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> Optional[Any]:
        async with self._lock:
            entry = self._store.get(key)
            if entry and not entry.is_expired():
                return entry.value
            if key in self._store:
                del self._store[key]  # Expired
        return None

    async def set(self, key: str, value: Any, ttl: float = 300.0) -> None:
        async with self._lock:
            self._store[key] = CacheEntry(value=value, cached_at=time.time(), ttl=ttl)
            # Reset invalidation event for this key
            self._events[key] = asyncio.Event()

    async def invalidate(self, key: str) -> None:
        """Evict a key and notify all waiters."""
        async with self._lock:
            self._store.pop(key, None)
            event = self._events.get(key)
        if event:
            event.set()  # Wake all waiters
            print(f"[cache] Invalidated key={key}")

    async def wait_for_invalidation(self, key: str, timeout: float = 60.0) -> bool:
        """Block until a key is invalidated. Returns True if invalidated, False on timeout."""
        async with self._lock:
            event = self._events.get(key)
        if not event:
            return False
        try:
            await asyncio.wait_for(asyncio.shield(event.wait()), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False


cache = InvalidatableCache()


async def get_user_plan(user_id: str) -> str:
    """Get user plan from cache; re-fetch from DB on miss."""
    key = f"user_plan:{user_id}"
    cached = await cache.get(key)
    if cached is not None:
        print(f"[cache] Hit: {key} → {cached}")
        return cached

    # Simulate DB fetch
    await asyncio.sleep(0.05)
    plan = "free"  # In prod: query database
    await cache.set(key, plan, ttl=300.0)
    print(f"[cache] Miss: {key} → fetched '{plan}' from DB")
    return plan


async def handle_plan_upgrade(user_id: str, new_plan: str) -> None:
    """Called when user upgrades; immediately invalidates their cached plan."""
    print(f"[upgrade] User {user_id} upgraded to {new_plan}")
    # Persist to DB (omitted)
    # Immediately evict stale cache entry
    await cache.invalidate(f"user_plan:{user_id}")


async def demo():
    # Warm the cache
    plan1 = await get_user_plan("user_42")
    plan2 = await get_user_plan("user_42")  # Cache hit
    print(f"Before upgrade: {plan1}, {plan2}")

    # Simulate upgrade
    await handle_plan_upgrade("user_42", "pro")

    # Next read re-fetches from DB
    plan3 = await get_user_plan("user_42")
    print(f"After invalidation: {plan3}")

asyncio.run(demo())
```

---

## Solution 2: Redis Pub/Sub Invalidation Across Instances

Use Redis Pub/Sub to broadcast invalidation events to all agent instances when a key changes.

```python
import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any, Optional

# pip install redis[asyncio]
try:
    import redis.asyncio as aioredis
    HAS_REDIS = True
except ImportError:
    HAS_REDIS = False


@dataclass
class LocalEntry:
    value: Any
    expires_at: float

    def is_valid(self) -> bool:
        return time.monotonic() < self.expires_at


INVALIDATION_CHANNEL = "agent:cache:invalidate"


class RedisInvalidatableCache:
    def __init__(self, redis_url: str = "redis://localhost:6379", instance_id: str = "inst_0"):
        self._redis = aioredis.from_url(redis_url, decode_responses=True)
        self._local: dict[str, LocalEntry] = {}
        self._instance_id = instance_id
        self._listener_task: Optional[asyncio.Task] = None

    async def start_listener(self) -> None:
        """Start background task that listens for invalidation events."""
        self._listener_task = asyncio.create_task(self._listen_for_invalidations())
        print(f"[cache] Instance {self._instance_id} listening for invalidations")

    async def _listen_for_invalidations(self) -> None:
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(INVALIDATION_CHANNEL)
        async for message in pubsub.listen():
            if message["type"] != "message":
                continue
            try:
                event = json.loads(message["data"])
                key = event.get("key")
                sender = event.get("sender")
                if key and sender != self._instance_id:
                    self._local.pop(key, None)
                    print(f"[cache] {self._instance_id}: evicted '{key}' (from {sender})")
            except Exception as exc:
                print(f"[cache] Listener error: {exc}")

    async def get(self, key: str) -> Optional[Any]:
        local = self._local.get(key)
        if local and local.is_valid():
            return local.value

        # Try Redis
        raw = await self._redis.get(f"cache:{key}")
        if raw:
            value = json.loads(raw)
            self._local[key] = LocalEntry(value=value, expires_at=time.monotonic() + 60)
            return value
        return None

    async def set(self, key: str, value: Any, ttl: int = 300) -> None:
        await self._redis.setex(f"cache:{key}", ttl, json.dumps(value))
        self._local[key] = LocalEntry(value=value, expires_at=time.monotonic() + ttl)

    async def invalidate(self, key: str) -> None:
        """Evict locally and broadcast to all instances via pub/sub."""
        self._local.pop(key, None)
        await self._redis.delete(f"cache:{key}")
        event = json.dumps({"key": key, "sender": self._instance_id})
        await self._redis.publish(INVALIDATION_CHANNEL, event)
        print(f"[cache] {self._instance_id}: invalidated and broadcast '{key}'")

    async def close(self) -> None:
        if self._listener_task:
            self._listener_task.cancel()
        await self._redis.aclose()


async def demo():
    cache_a = RedisInvalidatableCache(instance_id="inst_A")
    cache_b = RedisInvalidatableCache(instance_id="inst_B")

    await cache_a.start_listener()
    await cache_b.start_listener()
    await asyncio.sleep(0.1)  # Let listeners connect

    # Instance A writes
    await cache_a.set("user_plan:42", "free", ttl=300)

    # Instance B reads (gets from Redis)
    plan = await cache_b.get("user_plan:42")
    print(f"B reads: {plan}")

    # Instance A invalidates (B should evict its local copy)
    await cache_a.invalidate("user_plan:42")
    await asyncio.sleep(0.1)  # Allow pub/sub delivery

    # B's local copy is now evicted
    local_b = cache_b._local.get("user_plan:42")
    print(f"B local after invalidation: {local_b}")

    await cache_a.close()
    await cache_b.close()

# asyncio.run(demo())
```

---

## Solution 3: Versioned Cache with Monotonic Version Counter

Attach a version number to each cache key; invalidation bumps the version so stale readers auto-miss.

```python
import asyncio
import time
from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class VersionedEntry:
    value: Any
    version: int
    cached_at: float


class VersionedCache:
    def __init__(self):
        self._store: dict[str, VersionedEntry] = {}
        self._versions: dict[str, int] = {}  # key → current valid version
        self._lock = asyncio.Lock()

    async def current_version(self, key: str) -> int:
        async with self._lock:
            return self._versions.get(key, 0)

    async def get(self, key: str) -> Optional[tuple[Any, int]]:
        """Returns (value, version) if valid, else None."""
        async with self._lock:
            current = self._versions.get(key, 0)
            entry = self._store.get(key)
            if entry and entry.version == current:
                return entry.value, entry.version
        return None

    async def set(self, key: str, value: Any) -> int:
        async with self._lock:
            version = self._versions.get(key, 0)
            self._store[key] = VersionedEntry(value=value, version=version, cached_at=time.time())
            return version

    async def invalidate(self, key: str) -> int:
        """Bump version — all entries with old version are now stale."""
        async with self._lock:
            new_version = self._versions.get(key, 0) + 1
            self._versions[key] = new_version
            print(f"[versioned] key='{key}' bumped to v{new_version}")
            return new_version

    async def get_or_fetch(self, key: str, fetch_fn, ttl_hint: float = 0) -> Any:
        """Fetch-through with automatic version check."""
        result = await self.get(key)
        if result is not None:
            value, version = result
            print(f"[versioned] Cache hit: key='{key}' v{version}")
            return value

        value = await fetch_fn()
        await self.set(key, value)
        return value


async def demo():
    cache = VersionedCache()

    async def fetch_config():
        await asyncio.sleep(0.01)
        return {"feature_flags": {"new_ui": True, "beta": False}}

    # Warm cache (v0)
    config = await cache.get_or_fetch("global_config", fetch_config)
    print(f"Config v0: {config}")

    # Cache hit (still v0)
    config2 = await cache.get_or_fetch("global_config", fetch_config)
    print(f"Config v0 hit: {config2}")

    # Config changes in DB → invalidate
    await cache.invalidate("global_config")

    # Miss — re-fetches
    config3 = await cache.get_or_fetch("global_config", fetch_config)
    print(f"Config after invalidation: {config3}")

asyncio.run(demo())
```

---

## Solution 4: Write-Through Cache with Invalidation Side-Effect

Wrap the write path: every database write automatically invalidates the affected cache keys.

```python
import asyncio
import functools
import time
from dataclasses import dataclass
from typing import Any, Callable, Coroutine, Optional


@dataclass
class CacheStore:
    _data: dict = None  # type: ignore

    def __post_init__(self):
        self._data = {}
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> Optional[Any]:
        async with self._lock:
            return self._data.get(key)

    async def set(self, key: str, value: Any) -> None:
        async with self._lock:
            self._data[key] = value

    async def delete(self, key: str) -> None:
        async with self._lock:
            self._data.pop(key, None)
            print(f"[cache] Invalidated: {key}")

    async def delete_pattern(self, prefix: str) -> int:
        async with self._lock:
            keys = [k for k in self._data if k.startswith(prefix)]
            for k in keys:
                del self._data[k]
            if keys:
                print(f"[cache] Invalidated {len(keys)} keys matching '{prefix}*'")
            return len(keys)


_cache = CacheStore()


def invalidates(*key_patterns: str):
    """
    Decorator: after the wrapped coroutine succeeds, invalidate all matching cache keys.
    Patterns support a simple {arg} substitution.
    """
    def decorator(fn: Callable[..., Coroutine]):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            result = await fn(*args, **kwargs)
            # Build actual keys using args
            fn_params = fn.__code__.co_varnames[:fn.__code__.co_argcount]
            bound = dict(zip(fn_params, args))
            bound.update(kwargs)
            for pattern in key_patterns:
                try:
                    key = pattern.format(**bound)
                    await _cache.delete(key)
                except KeyError:
                    # Pattern references an arg not in this call — delete by prefix
                    await _cache.delete_pattern(pattern.split("{")[0])
            return result
        return wrapper
    return decorator


# --- Domain layer ---

async def _fetch_user_from_db(user_id: str) -> dict:
    await asyncio.sleep(0.02)
    return {"user_id": user_id, "name": "Alice", "plan": "free"}


async def get_user(user_id: str) -> dict:
    key = f"user:{user_id}"
    cached = await _cache.get(key)
    if cached:
        return cached
    user = await _fetch_user_from_db(user_id)
    await _cache.set(key, user)
    return user


@invalidates("user:{user_id}", "user_plan:{user_id}")
async def update_user_plan(user_id: str, new_plan: str) -> bool:
    """Update plan in DB and automatically invalidate related cache keys."""
    await asyncio.sleep(0.02)  # Simulate DB write
    print(f"[db] Updated user {user_id} plan to {new_plan}")
    return True


@invalidates("user:{user_id}")
async def update_user_name(user_id: str, new_name: str) -> bool:
    await asyncio.sleep(0.01)
    return True


async def demo():
    # Warm cache
    u1 = await get_user("usr_42")
    u2 = await get_user("usr_42")  # Cache hit
    print(f"Before: {u1['plan']}, cache_hit={u1 is u2}")

    # Write → auto-invalidate
    await update_user_plan("usr_42", "pro")

    # Re-fetch from DB
    u3 = await get_user("usr_42")
    print(f"After update: freshly fetched = {u3}")

asyncio.run(demo())
```

---

## Solution 5: TTL-Backed Cache with Eager Refresh Before Expiry

Pre-emptively refresh cache entries that are close to expiry in the background, eliminating cold misses.

```python
import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine, Optional


@dataclass
class EagerEntry:
    value: Any
    expires_at: float
    refreshing: bool = False

    def age(self) -> float:
        return time.monotonic() - (self.expires_at - 300)  # Assuming 300s TTL

    def is_expired(self) -> bool:
        return time.monotonic() > self.expires_at

    def should_refresh(self, refresh_threshold: float = 0.8) -> bool:
        """Refresh when 80% of TTL has elapsed."""
        ttl = 300.0  # Default assumption
        elapsed = ttl - (self.expires_at - time.monotonic())
        return (elapsed / ttl) >= refresh_threshold and not self.refreshing


class EagerRefreshCache:
    def __init__(self, default_ttl: float = 300.0, refresh_threshold: float = 0.8):
        self._store: dict[str, EagerEntry] = {}
        self._lock = asyncio.Lock()
        self._default_ttl = default_ttl
        self._threshold = refresh_threshold

    async def get_or_fetch(
        self,
        key: str,
        fetch_fn: Callable[[], Coroutine],
        ttl: Optional[float] = None,
    ) -> Any:
        ttl = ttl or self._default_ttl

        async with self._lock:
            entry = self._store.get(key)
            if entry and not entry.is_expired():
                # Trigger background refresh if close to expiry
                if entry.should_refresh(self._threshold):
                    entry.refreshing = True
                    asyncio.create_task(self._background_refresh(key, fetch_fn, ttl))
                return entry.value

        # Miss — fetch synchronously
        value = await fetch_fn()
        async with self._lock:
            self._store[key] = EagerEntry(
                value=value,
                expires_at=time.monotonic() + ttl,
            )
        print(f"[eager] Fetched and cached: {key}")
        return value

    async def _background_refresh(
        self, key: str, fetch_fn: Callable, ttl: float
    ) -> None:
        print(f"[eager] Background refresh: {key}")
        try:
            value = await fetch_fn()
            async with self._lock:
                self._store[key] = EagerEntry(
                    value=value,
                    expires_at=time.monotonic() + ttl,
                )
        except Exception as exc:
            print(f"[eager] Refresh failed for {key}: {exc}")
            async with self._lock:
                if key in self._store:
                    self._store[key].refreshing = False

    async def invalidate(self, key: str) -> None:
        async with self._lock:
            self._store.pop(key, None)
        print(f"[eager] Invalidated: {key}")


async def demo():
    cache = EagerRefreshCache(default_ttl=5.0, refresh_threshold=0.6)
    calls = 0

    async def fetch_config():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return {"version": calls, "feature_enabled": True}

    # Initial fetch
    config = await cache.get_or_fetch("app_config", fetch_config)
    print(f"Config v{config['version']}")

    # Wait until close to expiry
    await asyncio.sleep(3.5)

    # This hit triggers background refresh
    config2 = await cache.get_or_fetch("app_config", fetch_config)
    print(f"Near-expiry hit: v{config2['version']}")

    # Background refresh runs
    await asyncio.sleep(0.2)

    # Now serving fresh value
    config3 = await cache.get_or_fetch("app_config", fetch_config)
    print(f"After refresh: v{config3['version']} (total_db_calls={calls})")

asyncio.run(demo())
```

---

## Solution 6: Cache Dependency Graph — Invalidate Cascading Keys

When a root resource changes, automatically invalidate all derived cache keys that depend on it.

```python
import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class DependencyCache:
    _store: dict[str, Any] = field(default_factory=dict)
    _deps: dict[str, set[str]] = field(default_factory=dict)   # parent → dependents
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    def register_dependency(self, key: str, depends_on: str) -> None:
        """Register that key should be invalidated when depends_on is invalidated."""
        self._deps.setdefault(depends_on, set()).add(key)

    async def get(self, key: str) -> Optional[Any]:
        async with self._lock:
            return self._store.get(key)

    async def set(self, key: str, value: Any) -> None:
        async with self._lock:
            self._store[key] = value

    async def invalidate(self, key: str, _visited: Optional[set] = None) -> list[str]:
        """Invalidate key and all keys that depend on it (cascading)."""
        if _visited is None:
            _visited = set()
        if key in _visited:
            return []
        _visited.add(key)

        invalidated = []
        async with self._lock:
            if key in self._store:
                del self._store[key]
                invalidated.append(key)
            dependents = set(self._deps.get(key, set()))

        # Cascade to dependents
        for dep in dependents:
            sub = await self.invalidate(dep, _visited)
            invalidated.extend(sub)

        if invalidated:
            print(f"[deps] Invalidated cascade: {invalidated}")
        return invalidated


async def demo():
    cache = DependencyCache()

    # Dependency graph:
    # user:42 → user_plan:42 → user_permissions:42
    #         → user_profile:42

    cache.register_dependency("user_plan:42",         depends_on="user:42")
    cache.register_dependency("user_permissions:42",  depends_on="user_plan:42")
    cache.register_dependency("user_profile:42",      depends_on="user:42")

    # Populate
    await cache.set("user:42",              {"id": 42, "name": "Alice"})
    await cache.set("user_plan:42",         "free")
    await cache.set("user_permissions:42",  ["read", "write"])
    await cache.set("user_profile:42",      {"bio": "Engineer"})

    print("Before invalidation:")
    for k in ["user:42", "user_plan:42", "user_permissions:42", "user_profile:42"]:
        v = await cache.get(k)
        print(f"  {k}: {v}")

    # Invalidating root cascades to all dependents
    invalidated = await cache.invalidate("user:42")
    print(f"\nInvalidated: {invalidated}")

    print("\nAfter invalidation:")
    for k in ["user:42", "user_plan:42", "user_permissions:42", "user_profile:42"]:
        v = await cache.get(k)
        print(f"  {k}: {v}")

asyncio.run(demo())
```

---

## Comparison

| Solution | Multi-Instance | Propagation | Complexity | Backend Required | Cascading |
|---|---|---|---|---|---|
| asyncio.Event per key | No (single process) | Instant in-process | Low | None | No |
| Redis Pub/Sub | Yes | Near-instant | Medium | Redis | No |
| Versioned counter | No | Implicit on miss | Low | None | No |
| Write-through decorator | No | Automatic on write | Low | None | No |
| Eager TTL refresh | No | Background refresh | Medium | None | No |
| Dependency graph cascade | No (single process) | Instant + cascade | Medium | None | Yes |

**Recommendation:** Start with Solution 4 (write-through decorator) — it's the simplest to add to existing code: decorate every DB write function with `@invalidates(key_pattern)` and cache entries are evicted automatically. Add Solution 2 (Redis Pub/Sub) when you scale to multiple agent instances. Use Solution 6 (dependency graph) when cached values derive from other cached values and you need cascading invalidation.
