---
title: "Agent Doesn't Implement Distributed Mutex for Singleton Critical Sections"
description: "Multi-instance agent deployments where several replicas can execute the same critical section concurrently — schema migrations, singleton task scheduling, shared state initialization — produce data corruption and duplicate work. Implement a distributed mutex using a shared backend (Redis, database, or DynamoDB) with TTL-based lease expiry and fencing tokens to ensure only one agent executes the critical section at a time."
date: 2026-04-16
difficulty: advanced
category: reliability
slug: agent-doesnt-implement-distributed-mutex-for-singleton-critical-sections
tags: [distributed-mutex, critical-section, lease, fencing-token, distributed-locking, singleton]
symptoms:
  - "Schema migration runs twice when two agent instances start simultaneously"
  - "Scheduled task executes on every replica instead of one designated leader"
  - "Shared initialization logic runs in parallel across instances, corrupting shared state"
  - "Lock TTL expires during a long operation — another instance acquires the lock mid-flight"
  - "No fencing token — stale lock holder overwrites changes made by the new lock holder"
---

## Why This Happens

In-process locks (`asyncio.Lock`, `threading.Lock`) protect against concurrency within one process but provide no isolation across multiple agent instances sharing the same backend. A distributed mutex uses a shared atomic store — typically Redis SET NX PX, a database row with SELECT FOR UPDATE, or a cloud lock service — to ensure mutual exclusion across processes. Fencing tokens (monotonically increasing version numbers returned with the lock) prevent stale lock holders from corrupting state: every write to shared state must include the fencing token, and the backend rejects writes with an older token.

## Solution 1: Distributed Mutex Lock

```python
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class DistributedLock:
    lock_name: str
    holder_id: str = field(default_factory=lambda: str(uuid.uuid4())[:12])
    acquired_at: float = field(default_factory=time.time)
    ttl_seconds: float = 30.0
    fencing_token: int = 0       # monotonically increasing; used to detect stale holders
    auto_renew: bool = False

    def is_expired(self) -> bool:
        return time.time() - self.acquired_at > self.ttl_seconds

    def remaining_seconds(self) -> float:
        return max(0.0, self.ttl_seconds - (time.time() - self.acquired_at))

    def time_held_seconds(self) -> float:
        return time.time() - self.acquired_at
```

## Solution 2: In-Memory Distributed Mutex Backend (for single-process testing)

```python
import asyncio
import time
from typing import Dict, Optional, Tuple


class InMemoryMutexBackend:
    """
    Single-process implementation of the distributed mutex backend.
    Use this for testing; replace with Redis or database backend in production.
    """

    def __init__(self):
        self._locks: Dict[str, Tuple[str, float, int]] = {}
        # lock_name -> (holder_id, expires_at, fencing_token)
        self._fencing_counter: Dict[str, int] = {}
        self._lock = asyncio.Lock()

    async def acquire(
        self, lock_name: str, holder_id: str, ttl_seconds: float
    ) -> Optional[int]:
        """
        Attempt to acquire the lock. Returns fencing_token on success, None on failure.
        """
        async with self._lock:
            now = time.time()
            existing = self._locks.get(lock_name)

            if existing:
                current_holder, expires_at, token = existing
                if now < expires_at and current_holder != holder_id:
                    return None   # lock held by another

            # Acquire or renew
            self._fencing_counter[lock_name] = (
                self._fencing_counter.get(lock_name, 0) + 1
            )
            token = self._fencing_counter[lock_name]
            self._locks[lock_name] = (holder_id, now + ttl_seconds, token)
            return token

    async def release(self, lock_name: str, holder_id: str) -> bool:
        async with self._lock:
            existing = self._locks.get(lock_name)
            if existing and existing[0] == holder_id:
                del self._locks[lock_name]
                return True
            return False

    async def renew(
        self, lock_name: str, holder_id: str, ttl_seconds: float
    ) -> bool:
        async with self._lock:
            existing = self._locks.get(lock_name)
            if existing and existing[0] == holder_id:
                _, _, token = existing
                self._locks[lock_name] = (
                    holder_id, time.time() + ttl_seconds, token
                )
                return True
            return False

    async def get_holder(self, lock_name: str) -> Optional[Tuple[str, float, int]]:
        async with self._lock:
            existing = self._locks.get(lock_name)
            if not existing:
                return None
            holder_id, expires_at, token = existing
            if time.time() >= expires_at:
                del self._locks[lock_name]
                return None
            return existing
```

## Solution 3: Distributed Mutex Manager

```python
import asyncio
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional


class LockAcquisitionError(Exception):
    pass


class DistributedMutexManager:
    """
    Manages distributed lock acquisition, renewal, and release.
    Provides an async context manager for safe critical section execution.
    Optionally renews the lock in the background to prevent TTL expiry
    during long-running critical sections.
    """

    def __init__(
        self,
        backend: InMemoryMutexBackend,
        default_ttl_seconds: float = 30.0,
        retry_attempts: int = 5,
        retry_delay_seconds: float = 1.0,
        renew_interval_ratio: float = 0.5,
    ):
        self._backend = backend
        self._default_ttl = default_ttl_seconds
        self._retry_attempts = retry_attempts
        self._retry_delay = retry_delay_seconds
        self._renew_ratio = renew_interval_ratio
        self._acquired_count = 0
        self._failed_count = 0
        self._contention_count = 0

    @asynccontextmanager
    async def lock(
        self,
        lock_name: str,
        ttl_seconds: Optional[float] = None,
        holder_id: Optional[str] = None,
        auto_renew: bool = True,
    ) -> AsyncIterator[DistributedLock]:
        import uuid
        ttl = ttl_seconds or self._default_ttl
        hid = holder_id or str(uuid.uuid4())[:12]
        dl = DistributedLock(
            lock_name=lock_name,
            holder_id=hid,
            ttl_seconds=ttl,
            auto_renew=auto_renew,
        )

        # Acquire with retries
        token = None
        for attempt in range(self._retry_attempts):
            token = await self._backend.acquire(lock_name, hid, ttl)
            if token is not None:
                break
            if attempt < self._retry_attempts - 1:
                self._contention_count += 1
                await asyncio.sleep(self._retry_delay * (attempt + 1))

        if token is None:
            self._failed_count += 1
            raise LockAcquisitionError(
                f"could not acquire lock '{lock_name}' after {self._retry_attempts} attempts"
            )

        dl.fencing_token = token
        self._acquired_count += 1
        renew_task = None

        if auto_renew:
            renew_interval = ttl * self._renew_ratio
            renew_task = asyncio.create_task(
                self._renew_loop(lock_name, hid, ttl, renew_interval)
            )

        try:
            yield dl
        finally:
            if renew_task:
                renew_task.cancel()
                try:
                    await renew_task
                except asyncio.CancelledError:
                    pass
            await self._backend.release(lock_name, hid)

    async def _renew_loop(
        self, lock_name: str, holder_id: str, ttl: float, interval: float
    ) -> None:
        while True:
            await asyncio.sleep(interval)
            renewed = await self._backend.renew(lock_name, holder_id, ttl)
            if not renewed:
                break   # lock was lost — stop renewing

    def stats(self) -> dict:
        return {
            "acquired": self._acquired_count,
            "failed": self._failed_count,
            "contention_events": self._contention_count,
        }
```

## Solution 4: Fencing Token Validator

```python
from dataclasses import dataclass
from typing import Dict, Optional


@dataclass
class FencedWrite:
    resource_id: str
    fencing_token: int
    value: object


class FencingTokenValidator:
    """
    Guards shared state writes with fencing token comparison.
    Rejects writes whose fencing token is older than the last accepted token
    for that resource — prevents stale lock holders from corrupting state.
    """

    def __init__(self):
        self._last_token: Dict[str, int] = {}

    def validate_write(self, write: FencedWrite) -> bool:
        """
        Returns True if the write is allowed (token is current or newer).
        Returns False if the write is from a stale lock holder.
        """
        last = self._last_token.get(write.resource_id, 0)
        if write.fencing_token < last:
            return False   # stale write — reject
        self._last_token[write.resource_id] = write.fencing_token
        return True

    def current_token(self, resource_id: str) -> int:
        return self._last_token.get(resource_id, 0)
```

## Solution 5: Lock Health Monitor

```python
import time
from typing import List


class DistributedLockHealthMonitor:
    """
    Checks for expired or contested locks and emits health alerts.
    Helps detect lock leaks (acquired but never released) and high contention.
    """

    def __init__(
        self,
        backend: InMemoryMutexBackend,
        mutex_manager: DistributedMutexManager,
        high_contention_threshold: int = 10,
    ):
        self._backend = backend
        self._manager = mutex_manager
        self._threshold = high_contention_threshold

    async def check(self) -> dict:
        stats = self._manager.stats()
        alerts = []

        if stats["failed"] > 0:
            alerts.append({
                "type": "lock_acquisition_failures",
                "count": stats["failed"],
                "recommendation": "increase retry_attempts or reduce critical section duration",
            })

        if stats["contention_events"] > self._threshold:
            alerts.append({
                "type": "high_lock_contention",
                "count": stats["contention_events"],
                "recommendation": "review lock granularity — too many agents competing for same lock",
            })

        return {
            "generated_at": time.time(),
            "healthy": len(alerts) == 0,
            "stats": stats,
            "alerts": alerts,
        }
```

## Solution 6: Redis Mutex Backend (production)

```python
class RedisMutexBackend:
    """
    Production Redis-backed distributed mutex.
    Uses SET NX PX for atomic acquisition and Lua script for safe release.
    Requires redis-py: pip install redis[asyncio]
    """

    RELEASE_SCRIPT = """
    if redis.call("get", KEYS[1]) == ARGV[1] then
        return redis.call("del", KEYS[1])
    else
        return 0
    end
    """

    def __init__(self, redis_client, key_prefix: str = "agent:lock:"):
        self._redis = redis_client
        self._prefix = key_prefix
        self._fencing_key = key_prefix + "fence:"

    def _key(self, lock_name: str) -> str:
        return self._prefix + lock_name

    async def acquire(
        self, lock_name: str, holder_id: str, ttl_seconds: float
    ):
        key = self._key(lock_name)
        ttl_ms = int(ttl_seconds * 1000)
        set_result = await self._redis.set(
            key, holder_id, nx=True, px=ttl_ms
        )
        if not set_result:
            return None
        fence_key = self._fencing_key + lock_name
        token = await self._redis.incr(fence_key)
        return token

    async def release(self, lock_name: str, holder_id: str) -> bool:
        key = self._key(lock_name)
        result = await self._redis.eval(
            self.RELEASE_SCRIPT, 1, key, holder_id
        )
        return bool(result)

    async def renew(
        self, lock_name: str, holder_id: str, ttl_seconds: float
    ) -> bool:
        key = self._key(lock_name)
        current = await self._redis.get(key)
        if current and current.decode() == holder_id:
            ttl_ms = int(ttl_seconds * 1000)
            await self._redis.pexpire(key, ttl_ms)
            return True
        return False

    async def get_holder(self, lock_name: str):
        key = self._key(lock_name)
        holder = await self._redis.get(key)
        return holder.decode() if holder else None
```

## Comparison

| Approach | Atomic Acquire | TTL Expiry | Auto Renew | Fencing Token | Backend |
|---|---|---|---|---|---|
| InMemoryMutexBackend | Yes (asyncio.Lock) | Yes | No | Yes | In-process |
| DistributedMutexManager | Via backend | Via backend | Yes | Via backend | Pluggable |
| FencingTokenValidator | No | No | No | Yes (guards writes) | In-process |
| RedisMutexBackend | Yes (SET NX PX) | Yes (PX) | Via expire | Yes (INCR) | Redis |

**Best for production**: Use `RedisMutexBackend` with a Redis instance that has persistence enabled (`appendfsync always`). Set TTL to 2× the expected critical section duration; enable `auto_renew=True` for operations that can run longer than the TTL. Always pass the fencing token to every write operation and validate it with `FencingTokenValidator` — this prevents the split-brain scenario where a process pauses, loses the lock to another, then resumes and overwrites. Use `DistributedMutexManager` with `retry_attempts=3` and exponential delays to handle transient contention without spinning.
