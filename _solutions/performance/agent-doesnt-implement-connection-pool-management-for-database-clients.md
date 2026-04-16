---
title: "Agent Doesn't Implement Connection Pool Management for Database Clients"
description: "Agents that open a new database connection for every tool call pay TCP handshake and TLS negotiation overhead on each request, exhaust the database's connection limit under concurrent load, and leave idle connections open indefinitely. Implement connection pool management that maintains a reusable pool of authenticated connections, enforces per-pool concurrency limits, validates connections before checkout, and evicts stale connections proactively."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-connection-pool-management-for-database-clients
tags: [connection-pool, database-connections, pool-management, concurrency, resource-management, connection-reuse]
symptoms:
  - "Database logs show a new connection opened and closed for every query"
  - "Connection limit reached during load spikes — queries fail with 'too many clients'"
  - "Each query takes 150ms: 120ms for connection setup, 30ms for the query itself"
  - "Idle connections accumulate because nothing reclaims them after the agent session ends"
  - "No backpressure when all connections are in use — requests queue behind locked resources"
---

## Why This Happens

The simplest database access pattern is `conn = await connect(...); result = await conn.query(...); await conn.close()`. This is correct but expensive: each call pays full connection setup cost. Connection pooling reuses authenticated connections across calls, amortizing setup cost. A pool maintains a set of connections, checks them out to callers, validates them (pings the server), and returns them for reuse. Without eviction logic, stale connections accumulate after network interruptions and cause silent errors on the next use.

## Solution 1: Pool Configuration

```python
from dataclasses import dataclass


@dataclass
class PoolConfig:
    min_size: int = 2
    max_size: int = 10
    connection_timeout_seconds: float = 5.0
    idle_timeout_seconds: float = 300.0
    max_lifetime_seconds: float = 3600.0
    validation_query: str = "SELECT 1"
    validate_on_checkout: bool = True
    acquire_timeout_seconds: float = 10.0
```

## Solution 2: Pooled Connection Wrapper

```python
import time
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class PooledConnection:
    connection_id: str
    raw_conn: Any
    created_at: float = field(default_factory=time.time)
    last_used_at: float = field(default_factory=time.time)
    checkout_count: int = 0
    in_use: bool = False

    def touch(self) -> None:
        self.last_used_at = time.time()
        self.checkout_count += 1

    def idle_seconds(self) -> float:
        return time.time() - self.last_used_at

    def age_seconds(self) -> float:
        return time.time() - self.created_at

    def is_stale(self, config: PoolConfig) -> bool:
        if self.age_seconds() >= config.max_lifetime_seconds:
            return True
        if self.idle_seconds() >= config.idle_timeout_seconds:
            return True
        return False
```

## Solution 3: Async Connection Pool

```python
import asyncio
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator, Callable, List, Optional


class AsyncConnectionPool:
    """
    Maintains a pool of reusable async database connections.
    Connections are validated on checkout if validate_on_checkout=True.
    Stale connections are evicted by the background eviction task.
    Callers use acquire() as an async context manager.
    """

    def __init__(
        self,
        config: PoolConfig,
        connect_fn: Callable,        # async () -> raw_conn
        disconnect_fn: Callable,     # async (raw_conn) -> None
        validate_fn: Callable,       # async (raw_conn) -> bool
    ):
        self._config = config
        self._connect = connect_fn
        self._disconnect = disconnect_fn
        self._validate = validate_fn
        self._pool: List[PooledConnection] = []
        self._lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(config.max_size)
        self._eviction_task: Optional[asyncio.Task] = None
        self._total_created = 0
        self._total_evicted = 0
        self._checkout_failures = 0

    async def start(self) -> None:
        async with self._lock:
            for _ in range(self._config.min_size):
                conn = await self._make_connection()
                self._pool.append(conn)
        self._eviction_task = asyncio.create_task(self._eviction_loop())

    async def stop(self) -> None:
        if self._eviction_task:
            self._eviction_task.cancel()
        async with self._lock:
            for pc in self._pool:
                try:
                    await self._disconnect(pc.raw_conn)
                except Exception:
                    pass
            self._pool.clear()

    async def _make_connection(self) -> PooledConnection:
        raw = await asyncio.wait_for(
            self._connect(),
            timeout=self._config.connection_timeout_seconds,
        )
        self._total_created += 1
        return PooledConnection(
            connection_id=str(uuid.uuid4())[:8],
            raw_conn=raw,
        )

    @asynccontextmanager
    async def acquire(self) -> AsyncGenerator[Any, None]:
        try:
            await asyncio.wait_for(
                self._semaphore.acquire(),
                timeout=self._config.acquire_timeout_seconds,
            )
        except asyncio.TimeoutError:
            self._checkout_failures += 1
            raise TimeoutError(
                f"Could not acquire connection within "
                f"{self._config.acquire_timeout_seconds}s — pool exhausted"
            )

        pc = await self._checkout()
        try:
            yield pc.raw_conn
            pc.touch()
        except Exception:
            # On error, invalidate the connection rather than returning it
            await self._invalidate(pc)
            pc = None
            raise
        finally:
            if pc is not None:
                async with self._lock:
                    pc.in_use = False
            self._semaphore.release()

    async def _checkout(self) -> PooledConnection:
        async with self._lock:
            # Find an idle, non-stale connection
            for pc in self._pool:
                if not pc.in_use and not pc.is_stale(self._config):
                    pc.in_use = True
                    break
            else:
                # None available — create a new one if below max
                if len(self._pool) < self._config.max_size:
                    pc = await self._make_connection()
                    pc.in_use = True
                    self._pool.append(pc)
                else:
                    raise RuntimeError("Pool at max size with no idle connections")

        if self._config.validate_on_checkout:
            valid = await self._validate(pc.raw_conn)
            if not valid:
                await self._invalidate(pc)
                return await self._checkout()

        return pc

    async def _invalidate(self, pc: PooledConnection) -> None:
        try:
            await self._disconnect(pc.raw_conn)
        except Exception:
            pass
        async with self._lock:
            if pc in self._pool:
                self._pool.remove(pc)
        self._total_evicted += 1

    async def _eviction_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            await self._evict_stale()

    async def _evict_stale(self) -> None:
        async with self._lock:
            stale = [pc for pc in self._pool if pc.is_stale(self._config) and not pc.in_use]
        for pc in stale:
            await self._invalidate(pc)
        # Replenish to min_size
        async with self._lock:
            deficit = self._config.min_size - len(self._pool)
        for _ in range(max(0, deficit)):
            try:
                pc = await self._make_connection()
                async with self._lock:
                    self._pool.append(pc)
            except Exception:
                pass

    def stats(self) -> dict:
        idle = sum(1 for pc in self._pool if not pc.in_use)
        in_use = sum(1 for pc in self._pool if pc.in_use)
        return {
            "pool_size": len(self._pool),
            "idle": idle,
            "in_use": in_use,
            "total_created": self._total_created,
            "total_evicted": self._total_evicted,
            "checkout_failures": self._checkout_failures,
        }
```

## Solution 4: Named Pool Registry

```python
from typing import Dict, Optional


class ConnectionPoolRegistry:
    """
    Manages multiple named connection pools for different databases
    or connection tiers (read replica vs. primary, etc.).
    """

    def __init__(self):
        self._pools: Dict[str, AsyncConnectionPool] = {}

    async def register(
        self,
        name: str,
        pool: AsyncConnectionPool,
    ) -> None:
        await pool.start()
        self._pools[name] = pool

    def get(self, name: str) -> AsyncConnectionPool:
        pool = self._pools.get(name)
        if pool is None:
            raise KeyError(f"No pool registered under '{name}'")
        return pool

    async def stop_all(self) -> None:
        for pool in self._pools.values():
            await pool.stop()
        self._pools.clear()

    def all_stats(self) -> dict:
        return {name: pool.stats() for name, pool in self._pools.items()}
```

## Solution 5: Pool Health Monitor

```python
import time


class ConnectionPoolHealthMonitor:
    """
    Alerts when pool utilization is consistently high (indicating undersizing)
    or when eviction rate is abnormal (indicating network instability).
    """

    def __init__(
        self,
        pool: AsyncConnectionPool,
        high_utilization_threshold: float = 0.85,
        max_eviction_rate_per_hour: float = 20.0,
    ):
        self._pool = pool
        self._high_util = high_utilization_threshold
        self._max_eviction = max_eviction_rate_per_hour
        self._last_evicted = 0
        self._last_check_at = time.time()

    def check(self) -> dict:
        stats = self._pool.stats()
        alerts = []
        pool_size = max(stats["pool_size"], 1)
        utilization = stats["in_use"] / pool_size

        if utilization >= self._high_util:
            alerts.append({
                "type": "high_utilization",
                "utilization": round(utilization, 3),
                "recommendation": "increase max_size or reduce query latency",
            })

        now = time.time()
        elapsed_hours = (now - self._last_check_at) / 3600.0
        eviction_delta = stats["total_evicted"] - self._last_evicted
        eviction_rate = eviction_delta / max(elapsed_hours, 1e-9)

        if eviction_rate > self._max_eviction_rate_per_hour:
            alerts.append({
                "type": "high_eviction_rate",
                "evictions_per_hour": round(eviction_rate, 1),
                "recommendation": "check database connection stability and idle_timeout_seconds",
            })

        self._last_evicted = stats["total_evicted"]
        self._last_check_at = now

        return {
            "generated_at": now,
            "healthy": len(alerts) == 0,
            "stats": stats,
            "utilization": round(utilization, 3),
            "alerts": alerts,
        }
```

## Solution 6: Pool-Aware Query Executor

```python
from typing import Any, Callable, Optional


class PoolAwareQueryExecutor:
    """
    Executes database queries through a named pool.
    Provides a simple execute() interface that handles pool checkout,
    query execution, and connection return transparently.
    """

    def __init__(self, registry: ConnectionPoolRegistry, pool_name: str = "default"):
        self._registry = registry
        self._pool_name = pool_name

    async def execute(
        self,
        query_fn: Callable,   # async (conn) -> result
        pool_name: Optional[str] = None,
    ) -> Any:
        pool = self._registry.get(pool_name or self._pool_name)
        async with pool.acquire() as conn:
            return await query_fn(conn)
```

## Comparison

| Approach | Connection Reuse | Stale Eviction | Concurrency Limit | Validation | Multi-Pool |
|---|---|---|---|---|---|
| AsyncConnectionPool | Yes | Yes (background loop) | Yes (semaphore) | Yes (on checkout) | No |
| ConnectionPoolRegistry | Via pools | Via pools | Via pools | Via pools | Yes |
| ConnectionPoolHealthMonitor | No | No | No | No | No (per pool) |
| PoolAwareQueryExecutor | Via registry | Via pool | Via pool | Via pool | Via pool_name |

**Best for production**: Set `min_size=2` to keep warm connections ready and `max_size` to the database's per-client connection limit divided by the number of agent instances. Set `idle_timeout_seconds=300` (5 minutes) — shorter than most database server-side timeouts (typically 10 minutes) so the pool closes connections before the server does. Enable `validate_on_checkout=True` for all pools: a small ping adds ~1ms per checkout but prevents the dreaded "connection closed by server" error mid-query. Monitor `utilization` via `ConnectionPoolHealthMonitor` — sustained utilization above 85% means the pool is a latency bottleneck and `max_size` should be increased.
