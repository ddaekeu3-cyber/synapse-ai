---
title: "Agent Doesn't Implement Connection Pool Pre-Warming for Database Tools"
description: "Agents that create database connections lazily — on the first tool call that needs one — pay a cold-connection penalty on every new session: TCP handshake, TLS negotiation, authentication, and server-side session setup can add hundreds of milliseconds before a query runs. Implement connection pool pre-warming that establishes a minimum pool size during agent startup so that first-query latency matches steady-state latency."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-connection-pool-pre-warming-for-database-tools
tags: [connection-pool, pre-warming, database-latency, cold-connection, pool-management, startup-optimization]
symptoms:
  - "First database tool call in a session is 3–10× slower than subsequent calls"
  - "Connection pool shows 0 active connections at session start — lazy initialization"
  - "P99 latency is inflated by cold-connection overhead that vanishes after the first call"
  - "Scale-to-zero deployments pay full connection cost on every invocation"
  - "No mechanism to verify pool health before serving the first request"
---

## Why This Happens

Connection pools defer connection creation until a caller requests a connection — this is the default behavior in most database drivers. When an agent starts and immediately handles a user request that triggers a database tool call, the pool creates connections on demand: TCP connect, TLS handshake, authentication, and optionally a session-level `SET` or schema introspection. On fast networks this is 20–50 ms; across cloud regions or with TLS mutual auth it can reach 500 ms. Pre-warming fills the pool to `min_size` during initialization, before any request arrives, so the first tool call gets a ready connection from the pool instead of paying the creation cost.

## Solution 1: Connection Pool Warm-Up Config

```python
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class PoolWarmUpConfig:
    min_connections: int = 5           # connections to pre-create at startup
    max_connections: int = 20          # pool ceiling
    warm_up_timeout_seconds: float = 10.0
    health_check_query: str = "SELECT 1"
    connect_timeout_seconds: float = 3.0
    retry_attempts: int = 3
    retry_delay_seconds: float = 0.5
    databases: List[str] = field(default_factory=list)  # named pools to warm
```

## Solution 2: Database Connection Factory

```python
import asyncio
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional


@dataclass
class ConnectionDescriptor:
    database_name: str
    dsn: str
    pool_min: int
    pool_max: int
    connect_timeout: float
    health_check_query: str


class DatabaseConnectionFactory:
    """
    Creates and validates individual database connections.
    Wraps driver-level connect calls with timeout and retry.
    """

    def __init__(self, connect_fn: Callable[[str, float], Any]):
        # connect_fn(dsn, timeout) -> connection object
        self._connect = connect_fn

    async def create(self, descriptor: ConnectionDescriptor) -> Any:
        last_error: Optional[Exception] = None
        for attempt in range(3):
            try:
                conn = await asyncio.wait_for(
                    self._connect(descriptor.dsn, descriptor.connect_timeout),
                    timeout=descriptor.connect_timeout,
                )
                return conn
            except asyncio.TimeoutError as e:
                last_error = e
                await asyncio.sleep(0.5 * (attempt + 1))
            except Exception as e:
                last_error = e
                await asyncio.sleep(0.5 * (attempt + 1))
        raise ConnectionError(
            f"Failed to connect to {descriptor.database_name} after 3 attempts: {last_error}"
        )

    async def health_check(self, conn: Any, query: str) -> bool:
        try:
            await asyncio.wait_for(conn.execute(query), timeout=2.0)
            return True
        except Exception:
            return False
```

## Solution 3: Connection Pool Pre-Warmer

```python
import asyncio
import time
from typing import Any, Dict, List, Optional


class ConnectionPoolPreWarmer:
    """
    Fills a named pool to its minimum size before the agent begins
    serving requests. Runs health checks on each new connection and
    discards unhealthy ones, retrying until min_connections is reached
    or the timeout expires.
    """

    def __init__(
        self,
        factory: DatabaseConnectionFactory,
        config: PoolWarmUpConfig,
    ):
        self._factory = factory
        self._config = config
        self._pools: Dict[str, List[Any]] = {}
        self._warm_up_stats: Dict[str, dict] = {}

    async def warm_up(self, descriptor: ConnectionDescriptor) -> dict:
        name = descriptor.database_name
        self._pools[name] = []
        start = time.time()
        created = 0
        failed = 0
        deadline = start + self._config.warm_up_timeout_seconds

        while created < self._config.min_connections:
            if time.time() >= deadline:
                break
            try:
                conn = await self._factory.create(descriptor)
                healthy = await self._factory.health_check(
                    conn, descriptor.health_check_query
                )
                if healthy:
                    self._pools[name].append(conn)
                    created += 1
                else:
                    await conn.close()
                    failed += 1
            except Exception:
                failed += 1

        elapsed_ms = round((time.time() - start) * 1000, 2)
        stats = {
            "database": name,
            "connections_created": created,
            "connections_failed": failed,
            "target": self._config.min_connections,
            "reached_target": created >= self._config.min_connections,
            "elapsed_ms": elapsed_ms,
        }
        self._warm_up_stats[name] = stats
        return stats

    def acquire(self, database_name: str) -> Optional[Any]:
        pool = self._pools.get(database_name, [])
        if pool:
            return pool.pop(0)
        return None   # pool exhausted — caller creates a new connection

    def release(self, database_name: str, conn: Any) -> None:
        pool = self._pools.setdefault(database_name, [])
        if len(pool) < self._config.max_connections:
            pool.append(conn)

    def stats(self) -> Dict[str, dict]:
        return {
            name: {
                **self._warm_up_stats.get(name, {}),
                "available_connections": len(pool),
            }
            for name, pool in self._pools.items()
        }
```

## Solution 4: Pre-Warmed Database Tool Executor

```python
import asyncio
import time
from typing import Any, Callable, Optional


class PreWarmedDatabaseToolExecutor:
    """
    Executes database tool calls using pre-warmed connections.
    Acquires from the warm pool first; falls back to a new connection
    if the pool is empty. Returns connections to the pool after use.
    """

    def __init__(
        self,
        warmer: ConnectionPoolPreWarmer,
        factory: DatabaseConnectionFactory,
    ):
        self._warmer = warmer
        self._factory = factory
        self._calls = 0
        self._pool_hits = 0
        self._pool_misses = 0

    async def execute(
        self,
        database_name: str,
        descriptor: ConnectionDescriptor,
        query_fn: Callable[[Any], Any],
    ) -> Any:
        self._calls += 1
        conn = self._warmer.acquire(database_name)
        pool_hit = conn is not None

        if not pool_hit:
            self._pool_misses += 1
            conn = await self._factory.create(descriptor)
        else:
            self._pool_hits += 1

        try:
            result = await query_fn(conn)
            return result
        finally:
            self._warmer.release(database_name, conn)

    def hit_rate(self) -> float:
        if self._calls == 0:
            return 0.0
        return round(self._pool_hits / self._calls, 4)

    def stats(self) -> dict:
        return {
            "total_calls": self._calls,
            "pool_hits": self._pool_hits,
            "pool_misses": self._pool_misses,
            "hit_rate": self.hit_rate(),
        }
```

## Solution 5: Multi-Database Warm-Up Coordinator

```python
import asyncio
import time
from typing import Dict, List


class MultiDatabaseWarmUpCoordinator:
    """
    Warms up connection pools for multiple databases concurrently
    during agent startup. Reports which pools reached their target
    and which fell short due to timeouts or connection failures.
    """

    def __init__(
        self,
        warmer: ConnectionPoolPreWarmer,
        descriptors: List[ConnectionDescriptor],
    ):
        self._warmer = warmer
        self._descriptors = descriptors

    async def run(self) -> dict:
        start = time.time()
        tasks = [
            self._warmer.warm_up(desc)
            for desc in self._descriptors
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        succeeded = []
        failed = []
        for desc, result in zip(self._descriptors, results):
            if isinstance(result, Exception):
                failed.append({
                    "database": desc.database_name,
                    "error": str(result),
                })
            else:
                if result["reached_target"]:
                    succeeded.append(result)
                else:
                    failed.append(result)

        return {
            "total_databases": len(self._descriptors),
            "fully_warmed": len(succeeded),
            "partial_or_failed": len(failed),
            "total_elapsed_ms": round((time.time() - start) * 1000, 2),
            "succeeded": succeeded,
            "failed": failed,
        }
```

## Solution 6: Pool Pre-Warming Dashboard

```python
import time


class ConnectionPoolPreWarmingDashboard:
    """
    Combines warm-up stats, pool utilization, and executor hit rate
    into a single operational snapshot.
    """

    def __init__(
        self,
        warmer: ConnectionPoolPreWarmer,
        executor: PreWarmedDatabaseToolExecutor,
        coordinator_result: dict = None,
    ):
        self._warmer = warmer
        self._executor = executor
        self._coordinator_result = coordinator_result or {}

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "warm_up_summary": self._coordinator_result,
            "pool_stats": self._warmer.stats(),
            "executor_stats": self._executor.stats(),
            "health": {
                "all_pools_warmed": self._coordinator_result.get("partial_or_failed", 1) == 0,
                "pool_hit_rate": self._executor.hit_rate(),
            },
        }
```

## Comparison

| Approach | Min Pool Fill | Health Check | Multi-DB Concurrent | Hit-Rate Tracking | Fallback on Miss |
|---|---|---|---|---|---|
| ConnectionPoolPreWarmer | Yes | Yes | No | No | No |
| DatabaseConnectionFactory | No | Yes (per-conn) | No | No | No |
| PreWarmedDatabaseToolExecutor | Via warmer | No | No | Yes | Yes |
| MultiDatabaseWarmUpCoordinator | Via warmer | Via warmer | Yes | No | No |
| ConnectionPoolPreWarmingDashboard | No | No | No | No | No |

**Best for production**: Run `MultiDatabaseWarmUpCoordinator.run()` as the last step of agent initialization, before the HTTP server begins accepting requests. Set `min_connections=5` for most databases and `warm_up_timeout_seconds=10` — if the database is unhealthy enough to fail 5 connections in 10 seconds, the agent should refuse to start rather than serve degraded. Monitor `executor.hit_rate()`: a rate below 0.80 in steady state means the pool is undersized for the session concurrency and `min_connections` should be increased. For serverless deployments where connections cannot persist across invocations, use pre-warming to establish connections to a PgBouncer or ProxySQL sidecar that maintains long-lived upstream connections.
