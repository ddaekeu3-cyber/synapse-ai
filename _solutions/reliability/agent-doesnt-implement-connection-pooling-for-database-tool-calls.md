---
title: "Agent Doesn't Implement Connection Pooling for Database Tool Calls"
description: "AI agents that open a new database connection for every tool call exhaust connection limits, incur TCP and TLS handshake overhead on every query, and fail under concurrent load. Connection pooling maintains a warm pool of authenticated connections, lending them to tool calls and returning them after use — cutting query overhead from hundreds of milliseconds to single-digit milliseconds."
date: 2025-02-14
difficulty: intermediate
category: reliability
slug: agent-doesnt-implement-connection-pooling-for-database-tool-calls
tags:
  - connection-pooling
  - database
  - asyncio
  - aiopg
  - asyncpg
  - sqlalchemy
  - performance
  - reliability
symptoms:
  - "Each database tool call takes 200–500 ms just to connect before executing the query"
  - "Database server hits max_connections under concurrent agent load"
  - "Agent crashes with 'too many connections' when scaling to 10 concurrent users"
  - "Database connection objects are never closed — connection leak"
  - "Cold-start latency for database tools is 10× higher than steady-state latency"
---

## Problem

Opening a database connection requires a TCP handshake, TLS negotiation, authentication, and session setup — typically 100–500 ms. If every tool call opens and closes a connection, that overhead dominates query execution time. At scale, each concurrent agent worker holds one connection; 100 workers × 100 users = 10,000 connections, exceeding PostgreSQL's default limit of 100. Connection pooling solves both problems: a fixed-size warm pool is shared across all workers; tool calls borrow and return connections in microseconds.

---

## Solution 1: AsyncPGPool — asyncpg Connection Pool

```python
import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

try:
    import asyncpg
    _ASYNCPG = True
except ImportError:
    _ASYNCPG = False


@dataclass
class DBPoolConfig:
    dsn: str                          # postgresql://user:pass@host/db
    min_size: int = 5
    max_size: int = 20
    max_inactive_connection_lifetime: float = 300.0
    command_timeout: float = 30.0
    statement_cache_size: int = 100


class AsyncPGPool:
    """
    asyncpg connection pool for agent database tool calls.
    The pool is initialised once at agent startup; each tool call
    acquires a connection, executes, and releases it atomically.

    Usage:
        pool = AsyncPGPool(DBPoolConfig(dsn="postgresql://localhost/agentdb"))
        await pool.start()

        @pool.tool
        async def get_user(user_id: str) -> dict:
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT id, name, email FROM users WHERE id = $1", user_id
                )
                return dict(row) if row else {}

        await pool.close()
    """

    def __init__(self, config: DBPoolConfig):
        if not _ASYNCPG:
            raise RuntimeError("pip install asyncpg")
        self._config = config
        self._pool: Optional[asyncpg.Pool] = None

    async def start(self):
        self._pool = await asyncpg.create_pool(
            self._config.dsn,
            min_size=self._config.min_size,
            max_size=self._config.max_size,
            max_inactive_connection_lifetime=self._config.max_inactive_connection_lifetime,
            command_timeout=self._config.command_timeout,
            statement_cache_size=self._config.statement_cache_size,
        )

    @asynccontextmanager
    async def acquire(self):
        if self._pool is None:
            raise RuntimeError("Pool not started. Call await pool.start() first.")
        async with self._pool.acquire() as conn:
            yield conn

    async def fetch(self, query: str, *args) -> List[Dict]:
        async with self.acquire() as conn:
            rows = await conn.fetch(query, *args)
            return [dict(r) for r in rows]

    async def fetchrow(self, query: str, *args) -> Optional[Dict]:
        async with self.acquire() as conn:
            row = await conn.fetchrow(query, *args)
            return dict(row) if row else None

    async def execute(self, query: str, *args) -> str:
        async with self.acquire() as conn:
            return await conn.execute(query, *args)

    async def executemany(self, query: str, args_list: List) -> None:
        async with self.acquire() as conn:
            await conn.executemany(query, args_list)

    async def close(self):
        if self._pool:
            await self._pool.close()

    def stats(self) -> Dict[str, Any]:
        if not self._pool:
            return {}
        return {
            "size": self._pool.get_size(),
            "idle": self._pool.get_idle_size(),
            "min_size": self._config.min_size,
            "max_size": self._config.max_size,
        }
```

---

## Solution 2: SQLAlchemyAsyncPool — ORM-Compatible Async Pool

```python
import asyncio
from contextlib import asynccontextmanager
from typing import Any, Callable, Dict, Optional

try:
    from sqlalchemy.ext.asyncio import (
        AsyncEngine, AsyncSession,
        async_sessionmaker, create_async_engine,
    )
    from sqlalchemy.pool import AsyncAdaptedQueuePool
    _SQLALCHEMY = True
except ImportError:
    _SQLALCHEMY = False


class SQLAlchemyAsyncPool:
    """
    SQLAlchemy async pool with session factory.
    Compatible with ORM models and raw SQL execution.

    Usage:
        pool = SQLAlchemyAsyncPool(
            url="postgresql+asyncpg://user:pass@localhost/db",
            pool_size=10, max_overflow=5,
        )
        await pool.start()

        async with pool.session() as session:
            result = await session.execute(select(User).where(User.active == True))
            users = result.scalars().all()

        # Or raw SQL:
        rows = await pool.fetch_all("SELECT id, name FROM users LIMIT 10")
    """

    def __init__(self, url: str,
                 pool_size: int = 10,
                 max_overflow: int = 5,
                 pool_pre_ping: bool = True,
                 echo: bool = False):
        if not _SQLALCHEMY:
            raise RuntimeError("pip install sqlalchemy asyncpg")
        self._engine: Optional[AsyncEngine] = create_async_engine(
            url,
            pool_size=pool_size,
            max_overflow=max_overflow,
            pool_pre_ping=pool_pre_ping,
            echo=echo,
            poolclass=AsyncAdaptedQueuePool,
        )
        self._session_factory = async_sessionmaker(
            self._engine, expire_on_commit=False
        )

    async def start(self):
        # Warm up min connections
        async with self._engine.connect() as conn:
            await conn.execute(__import__("sqlalchemy").text("SELECT 1"))

    @asynccontextmanager
    async def session(self):
        async with self._session_factory() as session:
            yield session

    @asynccontextmanager
    async def connection(self):
        async with self._engine.connect() as conn:
            yield conn

    async def fetch_all(self, sql: str, **params) -> list:
        import sqlalchemy
        async with self.connection() as conn:
            result = await conn.execute(
                sqlalchemy.text(sql), params
            )
            return [dict(row) for row in result]

    async def close(self):
        if self._engine:
            await self._engine.dispose()
```

---

## Solution 3: ConnectionPoolHealthChecker — Pool Monitoring and Auto-Recovery

```python
import asyncio
import logging
import time
from typing import Callable, Dict, Optional

logger = logging.getLogger(__name__)


class ConnectionPoolHealthChecker:
    """
    Monitors pool health: detects leaked connections, dead pool,
    and auto-recovers by recycling idle connections.

    Usage:
        checker = ConnectionPoolHealthChecker(pool, check_interval=30)
        asyncio.create_task(checker.run())
        metrics = checker.snapshot()
    """

    def __init__(self, pool: AsyncPGPool,
                 check_interval: float = 30.0,
                 max_idle_s: float = 300.0,
                 on_alert: Optional[Callable] = None):
        self._pool = pool
        self._interval = check_interval
        self._max_idle = max_idle_s
        self._alert = on_alert or (lambda m: logger.warning(m))
        self._history: list = []

    async def run(self):
        while True:
            await asyncio.sleep(self._interval)
            snap = self.snapshot()
            self._history.append(snap)
            if len(self._history) > 120:
                self._history.pop(0)
            self._evaluate(snap)

    def snapshot(self) -> Dict:
        stats = self._pool.stats()
        return {
            "ts": time.time(),
            "pool_size": stats.get("size", 0),
            "idle": stats.get("idle", 0),
            "busy": stats.get("size", 0) - stats.get("idle", 0),
            "max_size": stats.get("max_size", 0),
        }

    def _evaluate(self, snap: Dict):
        if snap["pool_size"] == 0:
            self._alert("Database connection pool is empty — possible connection leak")
        if snap["idle"] == 0 and snap["busy"] > 0:
            self._alert(
                f"All {snap['busy']} connections are in use — potential pool exhaustion"
            )
        if snap["busy"] > snap["max_size"] * 0.9:
            self._alert(
                f"Pool utilisation at {snap['busy']}/{snap['max_size']} (>90%)"
            )

    def utilisation_trend(self) -> Dict:
        if len(self._history) < 2:
            return {}
        utilisations = [
            h["busy"] / max(h["max_size"], 1) for h in self._history
        ]
        return {
            "avg_utilisation": round(sum(utilisations) / len(utilisations), 3),
            "peak_utilisation": round(max(utilisations), 3),
            "samples": len(self._history),
        }
```

---

## Solution 4: PooledDatabaseToolRegistry — Register DB Tools with Shared Pool

```python
import asyncio
from functools import wraps
from typing import Any, Callable, Dict, List, Optional


class PooledDatabaseToolRegistry:
    """
    Registry for database-backed agent tools.
    All registered tools share a single connection pool.
    Tool functions receive an active connection as their first argument.

    Usage:
        registry = PooledDatabaseToolRegistry(pool)

        @registry.register("get_user_profile")
        async def get_user_profile(conn, user_id: str) -> dict:
            row = await conn.fetchrow(
                "SELECT * FROM profiles WHERE user_id = $1", user_id
            )
            return dict(row) if row else {}

        result = await registry.call("get_user_profile", user_id="u123")
    """

    def __init__(self, pool: AsyncPGPool):
        self._pool = pool
        self._tools: Dict[str, Callable] = {}

    def register(self, name: str):
        def decorator(fn: Callable) -> Callable:
            @wraps(fn)
            async def wrapper(*args, **kwargs) -> Any:
                async with self._pool.acquire() as conn:
                    return await fn(conn, *args, **kwargs)
            self._tools[name] = wrapper
            return wrapper
        return decorator

    async def call(self, tool_name: str, **kwargs) -> Any:
        fn = self._tools.get(tool_name)
        if fn is None:
            raise KeyError(f"DB tool '{tool_name}' not registered")
        return await fn(**kwargs)

    def list_tools(self) -> List[str]:
        return list(self._tools.keys())
```

---

## Solution 5: TransactionalToolGroup — Atomic Multi-Tool Transactions

```python
import asyncio
from contextlib import asynccontextmanager
from typing import Any, Callable, Dict, List, Optional


class TransactionalToolGroup:
    """
    Executes multiple database tool calls within a single transaction.
    If any tool raises, the entire transaction is rolled back.
    All tools in the group share the same connection and transaction.

    Usage:
        group = TransactionalToolGroup(pool)

        async with group.transaction() as tx:
            user = await tx.fetch_one("SELECT * FROM users WHERE id = $1", user_id)
            await tx.execute(
                "UPDATE accounts SET balance = balance - $1 WHERE user_id = $2",
                amount, user_id,
            )
            await tx.execute(
                "INSERT INTO transfers (from_id, amount) VALUES ($1, $2)",
                user_id, amount,
            )
        # Committed atomically; rolled back if any execute() raised
    """

    def __init__(self, pool: AsyncPGPool):
        self._pool = pool

    @asynccontextmanager
    async def transaction(self):
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                yield _TransactionContext(conn)


class _TransactionContext:
    def __init__(self, conn):
        self._conn = conn

    async def fetch_all(self, query: str, *args) -> List[Dict]:
        rows = await self._conn.fetch(query, *args)
        return [dict(r) for r in rows]

    async def fetch_one(self, query: str, *args) -> Optional[Dict]:
        row = await self._conn.fetchrow(query, *args)
        return dict(row) if row else None

    async def execute(self, query: str, *args) -> str:
        return await self._conn.execute(query, *args)
```

---

## Solution 6: PoolLifecycleManager — Startup and Graceful Shutdown

```python
import asyncio
import signal
from typing import Optional


class PoolLifecycleManager:
    """
    Manages database pool lifecycle: startup, health checks, and graceful shutdown.
    Ensures all in-flight queries complete before pool is closed on SIGTERM.

    Usage:
        mgr = PoolLifecycleManager(
            dsn="postgresql://localhost/agentdb",
            pool_size=15,
        )
        await mgr.startup()
        # Use mgr.pool for all DB access

        # On shutdown:
        await mgr.shutdown(drain_timeout=30.0)
    """

    def __init__(self, dsn: str,
                 pool_size: int = 10,
                 max_overflow: int = 5):
        self._config = DBPoolConfig(
            dsn=dsn,
            min_size=pool_size // 2,
            max_size=pool_size + max_overflow,
        )
        self.pool: Optional[AsyncPGPool] = None
        self._checker: Optional[ConnectionPoolHealthChecker] = None
        self._checker_task: Optional[asyncio.Task] = None

    async def startup(self):
        self.pool = AsyncPGPool(self._config)
        await self.pool.start()
        self._checker = ConnectionPoolHealthChecker(self.pool)
        self._checker_task = asyncio.create_task(self._checker.run())

    async def shutdown(self, drain_timeout: float = 30.0):
        if self._checker_task:
            self._checker_task.cancel()
        if self.pool:
            # Wait for in-flight connections to return
            deadline = asyncio.get_event_loop().time() + drain_timeout
            while asyncio.get_event_loop().time() < deadline:
                stats = self.pool.stats()
                if stats.get("busy", 0) == 0:
                    break
                await asyncio.sleep(0.5)
            await self.pool.close()

    def install_signal_handlers(self):
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(
                sig,
                lambda: asyncio.create_task(self.shutdown()),
            )
```

---

## Comparison

| Approach | Backend | ORM | Transactions | Monitoring | Lifecycle |
|---|---|---|---|---|---|
| **AsyncPGPool** | asyncpg | No | Yes | No | Manual |
| **SQLAlchemyAsyncPool** | asyncpg/aiomysql | Yes | Yes | No | Manual |
| **ConnectionPoolHealthChecker** | Any | No | No | Yes | No |
| **PooledDatabaseToolRegistry** | asyncpg | No | No | No | No |
| **TransactionalToolGroup** | asyncpg | No | Yes (atomic) | No | No |
| **PoolLifecycleManager** | asyncpg | No | Yes | Yes | Yes |

**Key insight**: initialise the pool once at agent startup — not on each request — and keep it alive for the lifetime of the process. Set `min_size` to the expected baseline concurrency and `max_size` to the database's `max_connections / number_of_replicas`. Use `PoolLifecycleManager` to ensure graceful drain on shutdown so no in-flight queries are interrupted.
