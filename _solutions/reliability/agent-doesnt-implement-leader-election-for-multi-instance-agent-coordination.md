---
title: "Agent Doesn't Implement Leader Election for Multi-Instance Agent Coordination"
description: "Multi-instance agent deployments that run scheduled tasks, cache warming jobs, or singleton background workers without leader election execute those jobs on every instance simultaneously — duplicating work, causing conflicting writes, and burning compute. Implement leader election so only one instance performs singleton work at a time, with automatic failover when the leader goes down."
date: 2026-04-16
difficulty: advanced
category: reliability
slug: agent-doesnt-implement-leader-election-for-multi-instance-agent-coordination
tags: [leader-election, multi-instance, coordination, singleton, distributed-lock, failover]
symptoms:
  - "Scheduled cache warming job runs on all 10 instances simultaneously"
  - "Background metric aggregation runs N times and writes conflicting results"
  - "No mechanism to designate one instance as responsible for singleton tasks"
  - "All instances process the same work queue items — deduplication is the only guard"
  - "When one instance crashes, no other instance takes over its singleton responsibilities"
---

## Why This Happens

Horizontal scaling makes all instances equal by default. Without a coordination mechanism, every instance independently decides to run scheduled or background work, leading to duplicate execution. Leader election solves this by having instances compete for a distributed lock or lease; the winner becomes the leader and runs singleton work, while followers monitor the leader's liveness and stand ready to take over. The key requirement is that the lease has a TTL so a crashed leader's lock expires automatically and a follower can acquire it.

## Solution 1: Leader Lease

```python
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class LeaderLease:
    lease_id: str
    instance_id: str
    acquired_at: float
    expires_at: float
    lease_key: str          # what resource this lease covers, e.g. "cache_warmer"

    def is_valid(self) -> bool:
        return time.time() < self.expires_at

    def seconds_remaining(self) -> float:
        return max(0.0, self.expires_at - time.time())

    @classmethod
    def create(
        cls,
        instance_id: str,
        lease_key: str,
        ttl_seconds: float,
    ) -> "LeaderLease":
        now = time.time()
        return cls(
            lease_id=uuid.uuid4().hex,
            instance_id=instance_id,
            acquired_at=now,
            expires_at=now + ttl_seconds,
            lease_key=lease_key,
        )
```

## Solution 2: In-Process Leader Elector (single-host testing)

```python
import asyncio
import time
from threading import Lock
from typing import Dict, Optional


class InProcessLeaderElector:
    """
    In-memory leader election for single-process multi-coroutine agents.
    Use as a drop-in during development; replace with Redis-backed elector
    for multi-process deployments.
    """

    _shared: Dict[str, LeaderLease] = {}
    _lock: Lock = Lock()

    def __init__(
        self,
        instance_id: str,
        lease_key: str,
        ttl_seconds: float = 30.0,
        renew_interval_seconds: float = 10.0,
    ):
        self._instance_id = instance_id
        self._lease_key = lease_key
        self._ttl = ttl_seconds
        self._renew_interval = renew_interval_seconds
        self._current_lease: Optional[LeaderLease] = None
        self._renew_task: Optional[asyncio.Task] = None

    def try_acquire(self) -> bool:
        with self.__class__._lock:
            existing = self.__class__._shared.get(self._lease_key)
            if existing and existing.is_valid() and existing.instance_id != self._instance_id:
                return False
            lease = LeaderLease.create(self._instance_id, self._lease_key, self._ttl)
            self.__class__._shared[self._lease_key] = lease
            self._current_lease = lease
            return True

    def is_leader(self) -> bool:
        with self.__class__._lock:
            existing = self.__class__._shared.get(self._lease_key)
            return (
                existing is not None
                and existing.instance_id == self._instance_id
                and existing.is_valid()
            )

    def release(self) -> None:
        with self.__class__._lock:
            existing = self.__class__._shared.get(self._lease_key)
            if existing and existing.instance_id == self._instance_id:
                del self.__class__._shared[self._lease_key]
            self._current_lease = None

    async def start_renewal_loop(self) -> None:
        async def _renew():
            while True:
                await asyncio.sleep(self._renew_interval)
                if self._current_lease:
                    self.try_acquire()
        self._renew_task = asyncio.create_task(_renew())

    def stop_renewal(self) -> None:
        if self._renew_task:
            self._renew_task.cancel()
```

## Solution 3: Redis-Backed Leader Elector

```python
import asyncio
import time
from typing import Optional


class RedisLeaderElector:
    """
    Production leader election using Redis SET NX PX (set-if-not-exists with TTL).
    The lease key is a Redis string whose value is the instance_id.
    Leadership is renewed by resetting the TTL before expiry.
    """

    def __init__(
        self,
        redis_client,            # aioredis or redis.asyncio client
        instance_id: str,
        lease_key: str,
        ttl_seconds: float = 30.0,
        renew_interval_seconds: float = 10.0,
    ):
        self._redis = redis_client
        self._instance_id = instance_id
        self._lease_key = f"leader_election:{lease_key}"
        self._ttl_ms = int(ttl_seconds * 1000)
        self._renew_interval = renew_interval_seconds
        self._renew_task: Optional[asyncio.Task] = None

    async def try_acquire(self) -> bool:
        result = await self._redis.set(
            self._lease_key,
            self._instance_id,
            nx=True,
            px=self._ttl_ms,
        )
        return result is not None

    async def is_leader(self) -> bool:
        value = await self._redis.get(self._lease_key)
        if value is None:
            return False
        if isinstance(value, bytes):
            value = value.decode()
        return value == self._instance_id

    async def renew(self) -> bool:
        """Extend TTL only if we are still the leader."""
        if not await self.is_leader():
            return False
        await self._redis.pexpire(self._lease_key, self._ttl_ms)
        return True

    async def release(self) -> None:
        if await self.is_leader():
            await self._redis.delete(self._lease_key)

    async def start_renewal_loop(self) -> None:
        async def _loop():
            while True:
                await asyncio.sleep(self._renew_interval)
                await self.renew()
        self._renew_task = asyncio.create_task(_loop())

    def stop_renewal(self) -> None:
        if self._renew_task:
            self._renew_task.cancel()
```

## Solution 4: Leader-Gated Task Runner

```python
import asyncio
from typing import Any, Callable, Union


class LeaderGatedTaskRunner:
    """
    Wraps a background task so it only runs on the elected leader.
    Non-leaders poll for leadership and start the task when they win.
    """

    def __init__(
        self,
        elector: Union[InProcessLeaderElector, RedisLeaderElector],
        poll_interval_seconds: float = 5.0,
    ):
        self._elector = elector
        self._poll_interval = poll_interval_seconds

    async def run_if_leader(self, task_fn: Callable, *args: Any, **kwargs: Any) -> Any:
        """Run task_fn only if this instance is the leader."""
        acquired = await self._maybe_acquire()
        if not acquired:
            return None
        return await task_fn(*args, **kwargs)

    async def _maybe_acquire(self) -> bool:
        if hasattr(self._elector, 'try_acquire'):
            result = self._elector.try_acquire()
            if asyncio.iscoroutine(result):
                return await result
            return result
        return False

    async def loop_until_leader_then_run(
        self,
        task_fn: Callable,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Block until this instance wins leadership, then run the task."""
        while True:
            acquired = await self._maybe_acquire()
            if acquired:
                return await task_fn(*args, **kwargs)
            await asyncio.sleep(self._poll_interval)
```

## Solution 5: Leadership Change Monitor

```python
import asyncio
import time
from typing import Callable, Optional, Union


class LeadershipChangeMonitor:
    """
    Monitors leadership transitions and calls handlers when
    this instance gains or loses leadership.
    """

    def __init__(
        self,
        elector: Union[InProcessLeaderElector, RedisLeaderElector],
        check_interval_seconds: float = 5.0,
    ):
        self._elector = elector
        self._interval = check_interval_seconds
        self._was_leader = False
        self._on_gain: Optional[Callable] = None
        self._on_lose: Optional[Callable] = None

    def on_leadership_gained(self, fn: Callable) -> None:
        self._on_gain = fn

    def on_leadership_lost(self, fn: Callable) -> None:
        self._on_lose = fn

    async def monitor(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            is_leader = self._elector.is_leader()
            if asyncio.iscoroutine(is_leader):
                is_leader = await is_leader

            if is_leader and not self._was_leader:
                self._was_leader = True
                if self._on_gain:
                    await self._on_gain() if asyncio.iscoroutinefunction(self._on_gain) else self._on_gain()
            elif not is_leader and self._was_leader:
                self._was_leader = False
                if self._on_lose:
                    await self._on_lose() if asyncio.iscoroutinefunction(self._on_lose) else self._on_lose()
```

## Solution 6: Leader Election Dashboard

```python
import time
from typing import Union


class LeaderElectionDashboard:
    def __init__(
        self,
        elector: Union[InProcessLeaderElector, RedisLeaderElector],
        instance_id: str,
        lease_key: str,
    ):
        self._elector = elector
        self._instance_id = instance_id
        self._lease_key = lease_key

    async def render(self) -> dict:
        is_leader = self._elector.is_leader()
        if asyncio.iscoroutine(is_leader):
            is_leader = await is_leader
        return {
            "generated_at": time.time(),
            "instance_id": self._instance_id,
            "lease_key": self._lease_key,
            "is_leader": is_leader,
            "elector_type": type(self._elector).__name__,
        }
```

## Comparison

| Approach | Multi-Process Safe | Automatic Failover | Renewal | Leadership Events | Dashboard |
|---|---|---|---|---|---|
| InProcessLeaderElector | No (single process) | Via TTL | Yes (loop) | No | No |
| RedisLeaderElector | Yes (Redis NX) | Via TTL | Yes (loop) | No | No |
| LeaderGatedTaskRunner | Via elector | Via elector | Via elector | No | No |
| LeadershipChangeMonitor | Via elector | Via elector | No | Yes (gain/lose) | No |
| LeaderElectionDashboard | No | No | No | No | Yes |

**Best for production**: Use `RedisLeaderElector` with `ttl_seconds=30` and `renew_interval_seconds=10` — this gives a 3× safety margin between renewals and expiry, tolerating two missed renewals before the lease expires. Always set the TTL; without it, a crashed leader holds the lock forever. Register `on_leadership_gained` and `on_leadership_lost` handlers to start and stop singleton background workers gracefully — starting on gain prevents duplicate work, stopping on loss ensures the new leader can start cleanly. Monitor `is_leader` from the dashboard on all instances: if no instance is leader (gap between expiry and next election), background work is silently not running.
