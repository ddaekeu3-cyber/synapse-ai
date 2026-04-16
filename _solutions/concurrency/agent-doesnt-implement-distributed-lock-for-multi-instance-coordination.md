---
title: "Agent Doesn't Implement Distributed Lock for Multi-Instance Coordination"
description: "AI agents deployed as multiple instances that share state — task queues, scheduled jobs, singleton operations — require distributed locking to prevent duplicate execution. Without a distributed lock, two instances simultaneously claim the same task, run the same scheduled job twice, or corrupt shared state with concurrent writes. Redis SETNX, Postgres advisory locks, and optimistic concurrency provide mutually exclusive access windows across agent processes."
date: 2025-02-17
difficulty: advanced
category: concurrency
slug: agent-doesnt-implement-distributed-lock-for-multi-instance-coordination
tags:
  - distributed-lock
  - multi-instance
  - coordination
  - redis
  - postgres
  - concurrency
  - mutex
symptoms:
  - "Two agent instances claim and process the same task simultaneously"
  - "A scheduled job runs twice when two agent pods start at the same second"
  - "No lock mechanism exists for operations that must execute on exactly one instance"
  - "Shared counter incremented by multiple instances produces incorrect totals"
  - "Agent restart causes a concurrent execution window where old and new instance both hold the resource"
---

## Problem

Horizontal scaling creates a class of bugs that never appear in single-instance deployments: two processes reading the same queue entry, two pods writing to the same record, two cron workers running the same job. A distributed lock gives exactly one instance exclusive access to a named resource for a bounded time window (TTL). The lock is acquired atomically using a backend-provided primitive (Redis `SET NX PX`, Postgres `pg_try_advisory_lock`, DynamoDB conditional write), held while work proceeds, and released when done or when the TTL expires — preventing a crashed holder from blocking all others forever.

---

## Solution 1: RedisDistributedLock — SET NX PX with Fencing Token

```python
import asyncio
import logging
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class LockAcquisition:
    lock_name: str
    token: str           # Unique value used to identify this holder
    acquired_at: float
    ttl_s: float
    holder_id: str       # Instance identifier

    def is_expired(self) -> bool:
        return time.monotonic() - self.acquired_at > self.ttl_s


class RedisDistributedLock:
    """
    Distributed mutex using Redis SET NX PX. Uses a unique random token
    per acquisition so that only the lock holder can release it —
    preventing a slow process from releasing a lock re-acquired by
    another instance after TTL expiry.

    Usage:
        lock = RedisDistributedLock(redis_client, "task:process:batch_001", ttl_s=30)
        async with lock.acquire() as acq:
            # Only one instance executes this block at a time
            await process_batch("batch_001")
        # Lock released automatically on exit (or after TTL on crash)
    """

    # Lua script for safe release: only release if token matches
    RELEASE_SCRIPT = """
    if redis.call("get", KEYS[1]) == ARGV[1] then
        return redis.call("del", KEYS[1])
    else
        return 0
    end
    """

    def __init__(self, redis_client,
                  lock_name: str,
                  ttl_s: float = 30.0,
                  retry_interval_s: float = 0.1,
                  max_retries: int = 50,
                  holder_id: Optional[str] = None):
        self._redis = redis_client
        self._name = lock_name
        self._ttl = ttl_s
        self._retry_interval = retry_interval_s
        self._max_retries = max_retries
        self._holder_id = holder_id or str(uuid.uuid4())[:8]

    @asynccontextmanager
    async def acquire(self, timeout_s: Optional[float] = None):
        """Async context manager that acquires the lock or raises TimeoutError."""
        acq = await self._try_acquire(timeout_s)
        if acq is None:
            raise asyncio.TimeoutError(
                f"Could not acquire lock '{self._name}' "
                f"within {timeout_s or self._max_retries * self._retry_interval}s"
            )
        try:
            yield acq
        finally:
            await self._release(acq.token)

    async def _try_acquire(self, timeout_s: Optional[float] = None) -> Optional[LockAcquisition]:
        token = str(uuid.uuid4())
        deadline = time.monotonic() + (timeout_s or self._max_retries * self._retry_interval)
        ttl_ms = int(self._ttl * 1000)
        attempts = 0

        while time.monotonic() < deadline:
            result = await self._redis.set(
                self._name, token,
                nx=True,          # Only set if Not eXists
                px=ttl_ms,        # Expire after TTL milliseconds
            )
            if result:
                logger.debug(
                    "lock_acquired name=%s token=%s holder=%s ttl_s=%.1f",
                    self._name, token[:8], self._holder_id, self._ttl,
                )
                return LockAcquisition(
                    lock_name=self._name,
                    token=token,
                    acquired_at=time.monotonic(),
                    ttl_s=self._ttl,
                    holder_id=self._holder_id,
                )
            attempts += 1
            await asyncio.sleep(self._retry_interval)

        logger.warning(
            "lock_acquisition_failed name=%s attempts=%d", self._name, attempts
        )
        return None

    async def _release(self, token: str):
        try:
            result = await self._redis.eval(
                self.RELEASE_SCRIPT, 1, self._name, token
            )
            if result:
                logger.debug("lock_released name=%s token=%s", self._name, token[:8])
            else:
                logger.warning(
                    "lock_release_skipped name=%s token=%s "
                    "(already expired or taken by another holder)",
                    self._name, token[:8],
                )
        except Exception as exc:
            logger.error("lock_release_error name=%s error=%s", self._name, exc)

    async def is_held(self) -> bool:
        """Returns True if the lock is currently held by any instance."""
        return await self._redis.exists(self._name) == 1
```

---

## Solution 2: PostgresAdvisoryLock — Session-Scoped Postgres Locks

```python
import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Optional

logger = logging.getLogger(__name__)


class PostgresAdvisoryLock:
    """
    Uses Postgres advisory locks (pg_try_advisory_lock / pg_advisory_unlock)
    for distributed coordination. Advisory locks are session-scoped — the
    lock is automatically released if the database connection closes,
    providing automatic cleanup on process crash without TTL management.

    Usage:
        lock = PostgresAdvisoryLock(db_pool, lock_id=12345)
        async with lock.acquire() as held:
            if not held:
                return  # Another instance holds the lock
            await run_singleton_job()
    """

    def __init__(self, db_pool,
                  lock_id: int,
                  lock_class_id: int = 1,
                  retry_interval_s: float = 0.5,
                  max_retries: int = 20):
        self._pool = db_pool
        self._lock_id = lock_id
        self._class_id = lock_class_id
        self._retry_interval = retry_interval_s
        self._max_retries = max_retries
        self._conn = None  # Hold connection for session lock lifetime

    @asynccontextmanager
    async def acquire(self, wait: bool = True):
        """
        Acquires the advisory lock. If wait=False, yields False immediately
        if the lock is already held. If wait=True, retries until acquired.
        """
        conn = await self._pool.acquire()
        try:
            if wait:
                acquired = await self._acquire_with_retry(conn)
            else:
                acquired = await self._try_once(conn)

            yield acquired
            if acquired:
                await conn.execute("SELECT pg_advisory_unlock($1, $2)",
                                    self._class_id, self._lock_id)
                logger.debug(
                    "advisory_lock_released id=%d:%d",
                    self._class_id, self._lock_id,
                )
        finally:
            await self._pool.release(conn)

    async def _try_once(self, conn) -> bool:
        row = await conn.fetchrow(
            "SELECT pg_try_advisory_lock($1, $2)",
            self._class_id, self._lock_id,
        )
        acquired = row[0]
        if acquired:
            logger.debug(
                "advisory_lock_acquired id=%d:%d",
                self._class_id, self._lock_id,
            )
        return acquired

    async def _acquire_with_retry(self, conn) -> bool:
        for attempt in range(self._max_retries):
            if await self._try_once(conn):
                return True
            await asyncio.sleep(self._retry_interval)
        logger.warning(
            "advisory_lock_timeout id=%d:%d attempts=%d",
            self._class_id, self._lock_id, self._max_retries,
        )
        return False
```

---

## Solution 3: LockHeartbeatRenewer — Extend TTL While Work Continues

```python
import asyncio
import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)


class LockHeartbeatRenewer:
    """
    Periodically extends the TTL of a held Redis distributed lock so that
    long-running work does not cause the lock to expire before completion.
    Runs in a background task and stops automatically if the lock is lost.

    Usage:
        lock = RedisDistributedLock(redis, "etl:job:2025-01", ttl_s=60)
        async with lock.acquire() as acq:
            async with LockHeartbeatRenewer(redis, acq, renew_every_s=20):
                await long_running_etl_job()   # Takes ~45 seconds
    """

    def __init__(self, redis_client,
                  acquisition: LockAcquisition,
                  renew_every_s: float = 10.0):
        self._redis = redis_client
        self._acq = acquisition
        self._interval = renew_every_s
        self._task: Optional[asyncio.Task] = None
        self._renewals = 0
        self._lost = False

    async def __aenter__(self):
        self._task = asyncio.create_task(self._renew_loop())
        return self

    async def __aexit__(self, *exc):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._lost:
            raise RuntimeError(
                f"Lock '{self._acq.lock_name}' was lost during execution "
                f"(renewed {self._renewals} times before loss)"
            )

    async def _renew_loop(self):
        while True:
            await asyncio.sleep(self._interval)
            renewed = await self._extend_ttl()
            if not renewed:
                self._lost = True
                logger.critical(
                    "lock_lost name=%s token=%s renewals=%d",
                    self._acq.lock_name, self._acq.token[:8], self._renewals,
                )
                return
            self._renewals += 1

    async def _extend_ttl(self) -> bool:
        """Extend TTL if token still matches (we still hold the lock)."""
        EXTEND_SCRIPT = """
        if redis.call("get", KEYS[1]) == ARGV[1] then
            return redis.call("pexpire", KEYS[1], ARGV[2])
        else
            return 0
        end
        """
        try:
            ttl_ms = int(self._acq.ttl_s * 1000)
            result = await self._redis.eval(
                EXTEND_SCRIPT, 1,
                self._acq.lock_name,
                self._acq.token,
                str(ttl_ms),
            )
            return bool(result)
        except Exception as exc:
            logger.warning("lock_renew_error: %s", exc)
            return False
```

---

## Solution 4: OptimisticLock — Version-Based Concurrent Update Protection

```python
import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class VersionedRecord:
    key: str
    value: Any
    version: int


class OptimisticLock:
    """
    Implements optimistic locking for shared state updates: reads the current
    version, applies the update, then writes back only if the version hasn't
    changed. Retries on version conflict. Unlike pessimistic locking, this
    allows concurrent reads and only serializes writes.

    Usage:
        opt_lock = OptimisticLock(
            read_fn=lambda k: db.get(k),
            write_fn=lambda k, v, ver: db.compare_and_swap(k, v, expected_version=ver),
        )
        result = await opt_lock.update(
            key="agent:counter",
            transform=lambda v: (v or 0) + 1,
        )
    """

    def __init__(self, read_fn: Callable, write_fn: Callable,
                  max_retries: int = 10,
                  retry_base_s: float = 0.05):
        self._read = read_fn
        self._write = write_fn
        self._max_retries = max_retries
        self._retry_base = retry_base_s
        self._conflicts = 0

    async def update(self, key: str,
                      transform: Callable[[Any], Any]) -> Any:
        """Apply transform to key's value atomically. Returns new value."""
        for attempt in range(self._max_retries):
            record: VersionedRecord = await self._read(key)
            current_value = record.value if record else None
            current_version = record.version if record else 0

            new_value = transform(current_value)
            success = await self._write(key, new_value, current_version)

            if success:
                logger.debug(
                    "optimistic_update_ok key=%s attempts=%d",
                    key, attempt + 1,
                )
                return new_value

            # Version conflict — another writer updated concurrently
            self._conflicts += 1
            backoff = self._retry_base * (2 ** attempt)
            logger.debug(
                "optimistic_conflict key=%s attempt=%d backoff=%.3f",
                key, attempt, backoff,
            )
            await asyncio.sleep(backoff)

        raise RuntimeError(
            f"Optimistic lock failed for key '{key}' "
            f"after {self._max_retries} attempts ({self._conflicts} total conflicts)"
        )

    def conflict_count(self) -> int:
        return self._conflicts
```

---

## Solution 5: SingletonJobGuard — Ensure a Job Runs on Exactly One Instance

```python
import asyncio
import hashlib
import logging
import time
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


class SingletonJobGuard:
    """
    Ensures a recurring job (cron, maintenance, scheduled task) executes
    on exactly one agent instance at a time. Instances that fail to acquire
    the lock skip the run silently. The next scheduled run starts a fresh
    acquisition race.

    Usage:
        guard = SingletonJobGuard(
            redis_client,
            job_name="daily_cleanup",
            ttl_s=600,   # Max execution time
        )

        @guard.singleton
        async def daily_cleanup():
            await delete_expired_sessions()

        # Call from all instances — only one will execute
        await daily_cleanup()
    """

    def __init__(self, redis_client,
                  job_name: str,
                  ttl_s: float = 300.0,
                  holder_id: Optional[str] = None):
        self._lock = RedisDistributedLock(
            redis_client,
            lock_name=f"singleton:job:{job_name}",
            ttl_s=ttl_s,
            max_retries=1,        # Don't wait — just skip if busy
            retry_interval_s=0.0,
            holder_id=holder_id,
        )
        self._job_name = job_name
        self._runs = 0
        self._skips = 0

    def singleton(self, fn: Callable) -> Callable:
        """Decorator that skips execution if another instance holds the lock."""
        import functools

        @functools.wraps(fn)
        async def wrapper(*args, **kwargs) -> Optional[Any]:
            try:
                async with self._lock.acquire(timeout_s=0.2):
                    self._runs += 1
                    t0 = time.monotonic()
                    result = await fn(*args, **kwargs)
                    logger.info(
                        "singleton_job_completed job=%s elapsed_ms=%.0f",
                        self._job_name, (time.monotonic() - t0) * 1000,
                    )
                    return result
            except asyncio.TimeoutError:
                self._skips += 1
                logger.debug(
                    "singleton_job_skipped job=%s (lock held by another instance)",
                    self._job_name,
                )
                return None

        return wrapper

    def stats(self) -> dict:
        return {
            "job": self._job_name,
            "runs": self._runs,
            "skips": self._skips,
        }
```

---

## Solution 6: DistributedLockManager — Unified Multi-Backend Lock Manager

```python
import asyncio
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class DistributedLockManager:
    """
    Manages a pool of named distributed locks across multiple backends.
    Provides a unified API regardless of whether the backend is Redis,
    Postgres advisory locks, or in-memory locks for testing.

    Usage:
        manager = DistributedLockManager(redis_client=redis, db_pool=pg_pool)
        manager.configure("task:claim", ttl_s=30, backend="redis")
        manager.configure("scheduled:cleanup", ttl_s=600, backend="postgres", lock_id=42)

        async with manager.lock("task:claim") as held:
            if held:
                await claim_task()
    """

    def __init__(self, redis_client=None, db_pool=None,
                  holder_id: Optional[str] = None):
        self._redis = redis_client
        self._db = db_pool
        self._configs: Dict[str, Dict] = {}
        self._holder_id = holder_id
        self._acquisitions = 0
        self._failures = 0

    def configure(self, lock_name: str,
                   ttl_s: float = 30.0,
                   backend: str = "redis",
                   lock_id: Optional[int] = None,
                   **kwargs):
        self._configs[lock_name] = {
            "ttl_s": ttl_s,
            "backend": backend,
            "lock_id": lock_id,
            **kwargs,
        }

    def _make_lock(self, lock_name: str):
        cfg = self._configs.get(lock_name, {"backend": "redis", "ttl_s": 30.0})
        backend = cfg.get("backend", "redis")

        if backend == "redis" and self._redis:
            return RedisDistributedLock(
                self._redis, lock_name,
                ttl_s=cfg["ttl_s"],
                holder_id=self._holder_id,
            )
        if backend == "postgres" and self._db:
            lock_id = cfg.get("lock_id") or hash(lock_name) & 0x7FFFFFFF
            return PostgresAdvisoryLock(self._db, lock_id=lock_id)

        raise ValueError(
            f"No backend configured for lock '{lock_name}' (backend={backend})"
        )

    def lock(self, lock_name: str):
        """Returns an async context manager for the named lock."""
        return self._make_lock(lock_name).acquire()

    async def is_any_held(self, lock_names: List[str]) -> Dict[str, bool]:
        results = {}
        for name in lock_names:
            try:
                lk = self._make_lock(name)
                if hasattr(lk, "is_held"):
                    results[name] = await lk.is_held()
                else:
                    results[name] = None
            except Exception:
                results[name] = None
        return results

    def health_report(self) -> Dict[str, Any]:
        return {
            "configured_locks": list(self._configs.keys()),
            "acquisitions": self._acquisitions,
            "failures": self._failures,
            "holder_id": self._holder_id,
        }
```

---

## Comparison

| Approach | TTL Expiry | Fencing Token | Heartbeat Renewal | Postgres | Optimistic | Integrated |
|---|---|---|---|---|---|---|
| **RedisDistributedLock** | Yes | Yes | No | No | No | No |
| **PostgresAdvisoryLock** | Session | No | No | Yes | No | No |
| **LockHeartbeatRenewer** | Extended | Via lock | Yes | No | No | No |
| **OptimisticLock** | N/A | No | N/A | No | Yes | No |
| **SingletonJobGuard** | Via lock | Via lock | No | No | No | No |
| **DistributedLockManager** | Yes | Yes | No | Yes | No | Yes |

**Key insight**: always use a fencing token (unique random value per acquisition) for lock release — never release a lock by key name alone. The Lua script `if GET(key) == token then DEL(key)` is the only correct release pattern; without it, a slow process that had its lock expire can release the new holder's lock. Set TTL to `max_expected_execution_time * 1.5` and use `LockHeartbeatRenewer` for operations longer than 30 seconds. Prefer Postgres advisory locks when you already have a database connection — they are automatically released on connection drop, eliminating the "dead holder with un-expired TTL" failure mode entirely.
