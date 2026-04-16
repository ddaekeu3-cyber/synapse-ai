---
title: "Agent Doesn't Implement Lease Renewal for Long-Running Tasks"
description: "AI agents that claim distributed tasks via a lease (Redis lock, DynamoDB TTL, database row lock) but don't renew those leases while working lose ownership mid-task. A competing worker then picks up the same task, leading to duplicate execution. Lease renewal runs a background coroutine that extends the TTL every N seconds while work is in progress, and relinquishes the lease immediately on completion or failure."
date: 2025-02-15
difficulty: intermediate
category: reliability
slug: agent-doesnt-implement-lease-renewal-for-long-running-tasks
tags:
  - lease
  - distributed-lock
  - redis
  - ttl
  - duplicate-execution
  - long-running
  - reliability
symptoms:
  - "Two agent workers execute the same task because the first worker's Redis lock expired"
  - "Task takes 45 seconds but the lock TTL is 30 seconds — second worker picks it up"
  - "Agent crashes mid-task without releasing the lock; next invocation must wait for TTL"
  - "No background renewal — lock TTL must be set conservatively large causing slow failover"
  - "Duplicate database writes appear when two workers process the same task simultaneously"
---

## Problem

A distributed task lock (Redis `SET NX EX`, DynamoDB conditional write, PostgreSQL advisory lock) has a fixed TTL. If the task takes longer than the TTL — due to a slow external API, a large file, or temporary throttling — another worker assumes the first worker died and steals the task. Both workers now execute the same task in parallel, causing duplicate side effects. Lease renewal extends the TTL in the background every T/2 seconds (where T is the TTL), so the lease remains valid for as long as the worker is alive and making progress.

---

## Solution 1: RedisLeaseRenewer — Background TTL Extension

```python
import asyncio
import logging
import time
import uuid
from contextlib import asynccontextmanager
from typing import Optional

logger = logging.getLogger(__name__)


class RedisLeaseRenewer:
    """
    Acquires a Redis lease (SET NX PX) and renews it in the background
    while a task runs. Releases the lease atomically on exit.

    Usage:
        renewer = RedisLeaseRenewer(redis_client, ttl_ms=30_000)

        async with renewer.lease("task:job-42") as acquired:
            if not acquired:
                return  # another worker holds the lease
            await do_long_running_work()
            # lease is released automatically on exit
    """

    RELEASE_LUA = """
    if redis.call("get", KEYS[1]) == ARGV[1] then
        return redis.call("del", KEYS[1])
    else
        return 0
    end
    """
    RENEW_LUA = """
    if redis.call("get", KEYS[1]) == ARGV[1] then
        return redis.call("pexpire", KEYS[1], ARGV[2])
    else
        return 0
    end
    """

    def __init__(self, redis, ttl_ms: int = 30_000,
                 renew_interval_s: Optional[float] = None):
        self._redis = redis
        self._ttl_ms = ttl_ms
        self._renew_interval = renew_interval_s or (ttl_ms / 2000)

    @asynccontextmanager
    async def lease(self, key: str):
        token = str(uuid.uuid4())
        acquired = await self._redis.set(
            key, token, nx=True, px=self._ttl_ms
        )
        if not acquired:
            yield False
            return

        renew_task = asyncio.create_task(
            self._renew_loop(key, token)
        )
        try:
            yield True
        finally:
            renew_task.cancel()
            try:
                await renew_task
            except asyncio.CancelledError:
                pass
            await self._release(key, token)

    async def _renew_loop(self, key: str, token: str):
        while True:
            await asyncio.sleep(self._renew_interval)
            result = await self._redis.eval(
                self.RENEW_LUA, 1, key, token, str(self._ttl_ms)
            )
            if result == 0:
                logger.error(
                    "lease_renewal_failed key=%s — lease stolen or expired", key
                )
                return
            logger.debug("lease_renewed key=%s ttl_ms=%d", key, self._ttl_ms)

    async def _release(self, key: str, token: str):
        result = await self._redis.eval(self.RELEASE_LUA, 1, key, token)
        if result == 0:
            logger.warning(
                "lease_release_no_op key=%s — lease had already expired or been stolen",
                key,
            )
        else:
            logger.debug("lease_released key=%s", key)
```

---

## Solution 2: LeaseWithHeartbeat — Detect Stalled Workers

```python
import asyncio
import logging
import time
import uuid
from contextlib import asynccontextmanager
from typing import Callable, Optional

logger = logging.getLogger(__name__)


class LeaseWithHeartbeat:
    """
    Extends RedisLeaseRenewer with a heartbeat that tracks the last time
    the worker made progress. If the worker is alive but not making progress
    (deadlocked internally), the heartbeat stops updating and the lease
    expires naturally rather than being renewed forever.

    Usage:
        lease = LeaseWithHeartbeat(redis, ttl_ms=30_000, stall_timeout_s=60)

        async with lease.lease("task:job-42") as ctx:
            if not ctx:
                return
            for chunk in large_file_chunks:
                await process(chunk)
                ctx.heartbeat()   # signals progress to the renewer
    """

    def __init__(self, redis, ttl_ms: int = 30_000,
                 stall_timeout_s: float = 60.0,
                 renew_interval_s: Optional[float] = None):
        self._redis = redis
        self._ttl_ms = ttl_ms
        self._stall = stall_timeout_s
        self._renew_interval = renew_interval_s or (ttl_ms / 2000)

    @asynccontextmanager
    async def lease(self, key: str):
        token = str(uuid.uuid4())
        acquired = await self._redis.set(key, token, nx=True, px=self._ttl_ms)
        if not acquired:
            yield None
            return

        ctx = _HeartbeatContext()
        renew_task = asyncio.create_task(
            self._renew_loop(key, token, ctx)
        )
        try:
            yield ctx
        finally:
            renew_task.cancel()
            try:
                await renew_task
            except asyncio.CancelledError:
                pass
            await self._release(key, token)

    async def _renew_loop(self, key: str, token: str,
                           ctx: "_HeartbeatContext"):
        while True:
            await asyncio.sleep(self._renew_interval)
            stall_s = time.monotonic() - ctx.last_heartbeat
            if stall_s > self._stall:
                logger.error(
                    "lease_stall_detected key=%s stall_s=%.0f — not renewing",
                    key, stall_s,
                )
                return  # lease expires naturally; another worker can retry

            result = await self._redis.eval(
                RedisLeaseRenewer.RENEW_LUA, 1, key, token, str(self._ttl_ms)
            )
            if result == 0:
                logger.error("lease_renewal_lost key=%s", key)
                return

    async def _release(self, key: str, token: str):
        await self._redis.eval(RedisLeaseRenewer.RELEASE_LUA, 1, key, token)


class _HeartbeatContext:
    def __init__(self):
        self.last_heartbeat = time.monotonic()

    def heartbeat(self):
        self.last_heartbeat = time.monotonic()
```

---

## Solution 3: PostgresAdvisoryLeaseholder — DB-Native Lease Renewal

```python
import asyncio
import logging
import time
from contextlib import asynccontextmanager
from typing import Optional

logger = logging.getLogger(__name__)


class PostgresAdvisoryLeaseholder:
    """
    Uses PostgreSQL advisory locks as leases. Advisory locks are
    session-scoped (released on disconnect) rather than TTL-scoped,
    so renewal is not needed — but the connection must be kept alive.
    A keepalive ping prevents the connection from being dropped by
    PgBouncer or network idle timeouts.

    Usage:
        leaseholder = PostgresAdvisoryLeaseholder(pool)

        async with leaseholder.try_lock(lock_id=42) as acquired:
            if not acquired:
                return
            await do_work()
    """

    def __init__(self, pool, keepalive_interval_s: float = 10.0):
        self._pool = pool
        self._keepalive = keepalive_interval_s

    @asynccontextmanager
    async def try_lock(self, lock_id: int):
        async with self._pool.acquire() as conn:
            acquired = await conn.fetchval(
                "SELECT pg_try_advisory_lock($1)", lock_id
            )
            if not acquired:
                yield False
                return

            keepalive_task = asyncio.create_task(
                self._keepalive_loop(conn)
            )
            try:
                yield True
            finally:
                keepalive_task.cancel()
                try:
                    await keepalive_task
                except asyncio.CancelledError:
                    pass
                await conn.execute(
                    "SELECT pg_advisory_unlock($1)", lock_id
                )

    async def _keepalive_loop(self, conn):
        while True:
            await asyncio.sleep(self._keepalive)
            await conn.fetchval("SELECT 1")
            logger.debug("advisory_lock_keepalive sent")
```

---

## Solution 4: LeaseMonitor — Observe Lease Health Across Workers

```python
import asyncio
import logging
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class LeaseMonitor:
    """
    Tracks lease acquisition, renewal, and release events in a local
    in-process store. Surfaces metrics for alerting on lease theft,
    stalled workers, and renewal failures.

    Usage:
        monitor = LeaseMonitor()
        renewer = MonitoredLeaseRenewer(redis, monitor=monitor)

        async with renewer.lease("task:42") as ok:
            ...

        print(monitor.report())
    """

    def __init__(self):
        self._leases: Dict[str, Dict[str, Any]] = {}
        self._stats = {
            "acquired": 0, "released": 0,
            "renewals": 0, "renewal_failures": 0,
            "thefts": 0, "stalls": 0,
        }

    def on_acquired(self, key: str):
        self._leases[key] = {"acquired_at": time.time(), "renewals": 0}
        self._stats["acquired"] += 1

    def on_renewed(self, key: str):
        if key in self._leases:
            self._leases[key]["renewals"] += 1
            self._leases[key]["last_renewed"] = time.time()
        self._stats["renewals"] += 1

    def on_renewal_failed(self, key: str):
        self._stats["renewal_failures"] += 1
        self._stats["thefts"] += 1
        logger.error("lease_theft_detected key=%s", key)

    def on_released(self, key: str):
        self._leases.pop(key, None)
        self._stats["released"] += 1

    def on_stall(self, key: str):
        self._stats["stalls"] += 1

    def active_leases(self) -> Dict[str, Dict]:
        return dict(self._leases)

    def report(self) -> Dict[str, Any]:
        return {
            "active_leases": len(self._leases),
            **self._stats,
        }
```

---

## Solution 5: TaskLeaseCoordinator — Claim, Work, Complete Pattern

```python
import asyncio
import logging
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


class TaskLeaseCoordinator:
    """
    High-level coordinator: fetches tasks from a queue, acquires a lease
    per task, executes the handler, and marks the task done — all with
    automatic lease renewal throughout execution.

    Usage:
        coord = TaskLeaseCoordinator(
            redis=redis_client,
            queue_name="agent:tasks",
            handler=process_task,
            concurrency=5,
            lease_ttl_ms=60_000,
        )
        await coord.run()
    """

    def __init__(self, redis, queue_name: str,
                 handler: Callable,
                 concurrency: int = 5,
                 lease_ttl_ms: int = 60_000):
        self._redis = redis
        self._queue = queue_name
        self._handler = handler
        self._concurrency = concurrency
        self._renewer = RedisLeaseRenewer(redis, ttl_ms=lease_ttl_ms)
        self._sem = asyncio.Semaphore(concurrency)

    async def run(self):
        while True:
            task_id = await self._redis.blpop(self._queue, timeout=5)
            if task_id:
                _, tid = task_id
                asyncio.create_task(self._process(tid.decode()))

    async def _process(self, task_id: str):
        async with self._sem:
            lease_key = f"lease:{task_id}"
            async with self._renewer.lease(lease_key) as acquired:
                if not acquired:
                    logger.debug("task_already_claimed task=%s", task_id)
                    return
                try:
                    await self._handler(task_id)
                    await self._redis.sadd("tasks:done", task_id)
                    logger.info("task_completed task=%s", task_id)
                except Exception as exc:
                    await self._redis.lpush("tasks:failed", task_id)
                    logger.error("task_failed task=%s error=%s", task_id, exc)
```

---

## Solution 6: LeaseRenewalMiddleware — Transparent Renewal for Any Async Function

```python
import asyncio
import functools
import logging
import uuid
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


class LeaseRenewalMiddleware:
    """
    Decorator that wraps any async function with lease acquisition and
    renewal. The wrapped function does not need to know about leasing.

    Usage:
        middleware = LeaseRenewalMiddleware(redis, ttl_ms=45_000)

        @middleware.with_lease(key_fn=lambda task_id: f"lease:{task_id}")
        async def process_task(task_id: str) -> dict:
            return await expensive_operation(task_id)

        # Only one worker executes process_task for each task_id at a time.
        result = await process_task("job-99")
    """

    def __init__(self, redis, ttl_ms: int = 30_000):
        self._renewer = RedisLeaseRenewer(redis, ttl_ms=ttl_ms)

    def with_lease(self, key_fn: Callable[..., str],
                   skip_if_locked: bool = True):
        def decorator(fn: Callable) -> Callable:
            @functools.wraps(fn)
            async def wrapper(*args, **kwargs) -> Any:
                key = key_fn(*args, **kwargs)
                async with self._renewer.lease(key) as acquired:
                    if not acquired:
                        if skip_if_locked:
                            logger.debug("lease_skipped key=%s fn=%s", key, fn.__name__)
                            return None
                        raise RuntimeError(f"Could not acquire lease for {key}")
                    return await fn(*args, **kwargs)
            return wrapper
        return decorator
```

---

## Comparison

| Approach | Backend | Renewal | Stall Detection | Monitoring | Decorator |
|---|---|---|---|---|---|
| **RedisLeaseRenewer** | Redis | Yes | No | No | No |
| **LeaseWithHeartbeat** | Redis | Conditional | Yes | No | No |
| **PostgresAdvisoryLeaseholder** | PostgreSQL | Keepalive | No | No | No |
| **LeaseMonitor** | Any | No | No | Yes | No |
| **TaskLeaseCoordinator** | Redis | Yes | No | Partial | No |
| **LeaseRenewalMiddleware** | Redis | Yes | No | No | Yes |

**Key insight**: set the lease TTL to 2–3× the expected task duration, and renew every TTL/2 seconds. This gives a full TTL window of grace if the renewal loop misses one cycle due to event-loop congestion. Add `LeaseWithHeartbeat` for tasks that may stall internally — if the worker is alive but not making progress after `stall_timeout_s`, let the lease expire so another worker can retry rather than keeping a deadlocked worker in exclusive possession forever.
