---
title: "Agent Doesn't Implement Connection Pool Management for Tool Integrations"
description: "Agents that open a new HTTP or database connection for every tool call exhaust file descriptors, saturate the downstream service's connection limit, and introduce per-call TLS handshake latency. Implement connection pool management that reuses connections across tool calls, enforces per-tool pool size limits, monitors pool health, and gracefully handles pool exhaustion."
date: 2026-04-16
difficulty: intermediate
category: reliability
slug: agent-doesnt-implement-connection-pool-management-for-tool-integrations
tags: [connection-pool, http-pooling, database-pool, file-descriptors, tool-integrations, pool-exhaustion]
symptoms:
  - "Each tool call opens a new TCP connection causing TLS handshake overhead"
  - "File descriptor limit exceeded under concurrent tool call load"
  - "Downstream service rejects connections because the agent opens too many simultaneously"
  - "Database connection count grows unboundedly with active sessions"
  - "No visibility into how many connections each tool integration is holding"
---

## Why This Happens

Tool integrations instantiated inside the tool execution function create a new connection on every call. Even well-written HTTP clients reuse connections only if the same client instance is reused — but if a new `httpx.AsyncClient()` or `aiohttp.ClientSession()` is created per call, connection reuse is impossible. Connection pooling requires a shared pool per integration, injected into the tool call handler rather than created inside it, with a configurable maximum pool size that prevents the agent from overwhelming downstream services.

## Solution 1: Connection Pool Configuration

```python
from dataclasses import dataclass


@dataclass
class ConnectionPoolConfig:
    max_connections: int = 10
    max_keepalive_connections: int = 5
    keepalive_expiry_seconds: float = 30.0
    connect_timeout_seconds: float = 5.0
    read_timeout_seconds: float = 30.0
    pool_acquire_timeout_seconds: float = 10.0
    max_retries_on_connection_error: int = 2
```

## Solution 2: HTTP Connection Pool

```python
import asyncio
import time
from typing import Any, Dict, Optional


class HTTPConnectionPool:
    """
    Manages a shared async HTTP client with connection pooling for a single
    integration endpoint. Exposes acquire/release semantics compatible with
    asyncio context managers.
    """

    def __init__(
        self,
        base_url: str,
        config: ConnectionPoolConfig,
        headers: Optional[Dict[str, str]] = None,
    ):
        self._base_url = base_url
        self._config = config
        self._headers = headers or {}
        self._client: Optional[Any] = None
        self._lock = asyncio.Lock()
        self._request_count = 0
        self._error_count = 0
        self._created_at = time.time()

    async def _get_client(self) -> Any:
        try:
            import httpx
        except ImportError:
            raise RuntimeError("httpx is required for HTTP connection pooling")

        if self._client is None or self._client.is_closed:
            limits = httpx.Limits(
                max_connections=self._config.max_connections,
                max_keepalive_connections=self._config.max_keepalive_connections,
                keepalive_expiry=self._config.keepalive_expiry_seconds,
            )
            timeout = httpx.Timeout(
                connect=self._config.connect_timeout_seconds,
                read=self._config.read_timeout_seconds,
            )
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                headers=self._headers,
                limits=limits,
                timeout=timeout,
            )
        return self._client

    async def request(
        self,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> Any:
        client = await self._get_client()
        self._request_count += 1
        try:
            response = await client.request(method, path, **kwargs)
            return response
        except Exception as exc:
            self._error_count += 1
            raise

    async def close(self) -> None:
        async with self._lock:
            if self._client and not self._client.is_closed:
                await self._client.aclose()
                self._client = None

    def stats(self) -> dict:
        return {
            "base_url": self._base_url,
            "max_connections": self._config.max_connections,
            "request_count": self._request_count,
            "error_count": self._error_count,
            "error_rate": round(self._error_count / max(self._request_count, 1), 4),
            "uptime_seconds": round(time.time() - self._created_at, 1),
        }
```

## Solution 3: Database Connection Pool Wrapper

```python
import asyncio
import time
from typing import Any, Callable, Optional


class DatabaseConnectionPoolWrapper:
    """
    Wraps an async database pool (asyncpg, aiomysql, aiosqlite, etc.)
    with usage tracking and pool exhaustion detection.
    """

    def __init__(
        self,
        pool_factory: Callable,
        config: ConnectionPoolConfig,
        db_name: str = "",
    ):
        self._factory = pool_factory
        self._config = config
        self._db_name = db_name
        self._pool: Optional[Any] = None
        self._lock = asyncio.Lock()
        self._checkout_count = 0
        self._timeout_count = 0

    async def initialize(self) -> None:
        async with self._lock:
            if self._pool is None:
                self._pool = await self._factory(
                    min_size=1,
                    max_size=self._config.max_connections,
                    command_timeout=self._config.read_timeout_seconds,
                )

    async def acquire(self) -> Any:
        if self._pool is None:
            await self.initialize()
        try:
            conn = await asyncio.wait_for(
                self._pool.acquire(),
                timeout=self._config.pool_acquire_timeout_seconds,
            )
            self._checkout_count += 1
            return conn
        except asyncio.TimeoutError:
            self._timeout_count += 1
            raise RuntimeError(
                f"Database pool '{self._db_name}' exhausted: "
                f"could not acquire connection within "
                f"{self._config.pool_acquire_timeout_seconds}s"
            )

    async def release(self, conn: Any) -> None:
        if self._pool:
            await self._pool.release(conn)

    async def close(self) -> None:
        async with self._lock:
            if self._pool:
                await self._pool.close()
                self._pool = None

    def stats(self) -> dict:
        pool_size = getattr(self._pool, "_size", None) if self._pool else None
        return {
            "db_name": self._db_name,
            "max_connections": self._config.max_connections,
            "checkout_count": self._checkout_count,
            "timeout_count": self._timeout_count,
            "current_pool_size": pool_size,
        }
```

## Solution 4: Tool Integration Pool Registry

```python
from typing import Dict, Union


class ToolIntegrationPoolRegistry:
    """
    Central registry of connection pools keyed by integration name.
    Tools retrieve their pool from the registry rather than creating
    new connections inline.
    """

    def __init__(self):
        self._http_pools: Dict[str, HTTPConnectionPool] = {}
        self._db_pools: Dict[str, DatabaseConnectionPoolWrapper] = {}

    def register_http(
        self,
        name: str,
        pool: HTTPConnectionPool,
    ) -> None:
        self._http_pools[name] = pool

    def register_db(
        self,
        name: str,
        pool: DatabaseConnectionPoolWrapper,
    ) -> None:
        self._db_pools[name] = pool

    def http(self, name: str) -> HTTPConnectionPool:
        if name not in self._http_pools:
            raise KeyError(f"No HTTP pool registered for '{name}'")
        return self._http_pools[name]

    def db(self, name: str) -> DatabaseConnectionPoolWrapper:
        if name not in self._db_pools:
            raise KeyError(f"No DB pool registered for '{name}'")
        return self._db_pools[name]

    async def close_all(self) -> None:
        for pool in self._http_pools.values():
            await pool.close()
        for pool in self._db_pools.values():
            await pool.close()

    def all_stats(self) -> dict:
        return {
            "http_pools": {name: p.stats() for name, p in self._http_pools.items()},
            "db_pools": {name: p.stats() for name, p in self._db_pools.items()},
        }
```

## Solution 5: Pool Exhaustion Circuit Breaker

```python
import time
from typing import Dict


class PoolExhaustionCircuitBreaker:
    """
    Tracks pool timeout rates and opens a circuit breaker when a pool
    is consistently exhausted, preventing further requests from queuing
    up and amplifying the exhaustion.
    """

    def __init__(
        self,
        timeout_rate_threshold: float = 0.10,
        window_seconds: float = 60.0,
        open_duration_seconds: float = 30.0,
    ):
        self._threshold = timeout_rate_threshold
        self._window = window_seconds
        self._open_duration = open_duration_seconds
        self._pool_stats_history: Dict[str, list] = {}
        self._open_until: Dict[str, float] = {}

    def record_stats(self, pool_name: str, stats: dict) -> None:
        if pool_name not in self._pool_stats_history:
            self._pool_stats_history[pool_name] = []
        self._pool_stats_history[pool_name].append({
            "ts": time.time(),
            **stats,
        })

    def is_open(self, pool_name: str) -> bool:
        open_until = self._open_until.get(pool_name, 0)
        if time.time() < open_until:
            return True
        # Check recent timeout rate
        history = self._pool_stats_history.get(pool_name, [])
        cutoff = time.time() - self._window
        recent = [h for h in history if h["ts"] >= cutoff]
        if len(recent) < 3:
            return False
        latest = recent[-1]
        timeout_rate = latest.get("timeout_count", 0) / max(latest.get("checkout_count", 1), 1)
        if timeout_rate >= self._threshold:
            self._open_until[pool_name] = time.time() + self._open_duration
            return True
        return False
```

## Solution 6: Connection Pool Dashboard

```python
import time


class ConnectionPoolDashboard:
    """
    Renders a snapshot of all registered connection pool stats,
    circuit breaker states, and exhaustion events.
    """

    def __init__(
        self,
        registry: ToolIntegrationPoolRegistry,
        circuit_breaker: PoolExhaustionCircuitBreaker,
    ):
        self._registry = registry
        self._breaker = circuit_breaker

    def render(self) -> dict:
        all_stats = self._registry.all_stats()
        pool_health = {}
        for name in list(all_stats["http_pools"]) + list(all_stats["db_pools"]):
            pool_health[name] = {
                "circuit_open": self._breaker.is_open(name),
            }
        return {
            "generated_at": time.time(),
            "pools": all_stats,
            "pool_health": pool_health,
        }
```

## Comparison

| Approach | Connection Reuse | Per-Tool Config | Pool Registry | Exhaustion Detection | Dashboard |
|---|---|---|---|---|---|
| HTTPConnectionPool | Yes (shared client) | Via config | No | No | No |
| DatabaseConnectionPoolWrapper | Yes (async pool) | Via config | No | Yes (timeout count) | No |
| ToolIntegrationPoolRegistry | Via pools | Via registration | Yes | No | No |
| PoolExhaustionCircuitBreaker | No | No | No | Yes (circuit) | No |
| ConnectionPoolDashboard | No | No | Via registry | Via breaker | Yes |

**Best for production**: Initialize connection pools at agent startup before the first request, not lazily on the first tool call — lazy initialization under concurrent load causes a connection stampede where many coroutines race to create the pool simultaneously. Set `max_connections` to the downstream service's per-client connection limit, not to an arbitrary number; for most REST APIs this is 10-50 connections per client IP. Monitor `error_rate` per pool and alert when it exceeds 5% — consistently elevated error rates indicate network issues or downstream service degradation that should be investigated before the circuit breaker opens.
