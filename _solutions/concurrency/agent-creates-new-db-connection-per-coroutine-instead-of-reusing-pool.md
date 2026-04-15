---
layout: solution
title: "Agent Creates New DB Connection Per Coroutine Instead of Reusing Pool"
category: concurrency
description: "Each asyncio coroutine opens a fresh PostgreSQL connection, exhausting max_connections within seconds under load and causing connection refused errors for all subsequent operations."
tags: [concurrency, database, connection-pool, asyncio, postgresql, performance]
---

## Symptom

Under moderate load (10+ concurrent agent tasks) the database begins refusing connections with `FATAL: sorry, too many clients already`. Each agent coroutine calls `asyncpg.connect()` at the start of its work and closes it at the end, but with hundreds of coroutines in flight simultaneously the connection count hits PostgreSQL's `max_connections` (default 100). The agent loop crashes or hangs, and the database server's memory is saturated with idle connections from coroutines waiting for their turn.

## Root Cause

`asyncpg.connect()` opens a new TCP connection and runs the full PostgreSQL authentication handshake on every call. In a sync program this is acceptable because connections are long-lived. In async programs where thousands of coroutines can be in flight, each awaiting `connect()` independently, the connection count scales with coroutine count rather than with actual concurrent DB operations. The fix is a shared connection pool that recycles a fixed number of connections across all coroutines.

## Fix

### Option 1 — asyncpg pool singleton

```python
import asyncio
import asyncpg
import anthropic

client = anthropic.AsyncAnthropic()

# Module-level pool — created once, shared by all coroutines
_pool: asyncpg.Pool | None = None

async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            dsn="postgresql://localhost/demo",
            min_size=2,
            max_size=10,          # at most 10 DB connections regardless of coroutine count
            max_inactive_connection_lifetime=300,
            command_timeout=30,
        )
    return _pool

async def fetch_user(user_id: int) -> dict:
    """Borrow a connection from the pool, never create a new one."""
    pool = await get_pool()
    async with pool.acquire() as conn:            # returns to pool when block exits
        row = await conn.fetchrow(
            "SELECT id, name, email FROM users WHERE id = $1", user_id
        )
    return dict(row) if row else {}

async def process_user_task(user_id: int) -> str:
    user = await fetch_user(user_id)
    if not user:
        return f"User {user_id} not found."
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{
            "role": "user",
            "content": f"Write a brief welcome message for {user.get('name', 'user')}.",
        }],
    )
    return response.content[0].text

async def main():
    # 50 concurrent coroutines — only 10 DB connections ever open
    tasks = [process_user_task(i) for i in range(50)]
    results = await asyncio.gather(*tasks)
    print(f"[pool] processed {len(results)} tasks")
    pool = await get_pool()
    await pool.close()

asyncio.run(main())
```

**Expected Token Savings:** Pool reuse eliminates TCP handshake latency per call (typically 5–50 ms per connection); faster DB round-trips mean less wall-clock time per token-generating call.
**Environment:** Any asyncio agent that queries PostgreSQL; essential for agents with concurrent tool calls.

---

### Option 2 — SQLAlchemy async engine with pool configuration

```python
import asyncio
from contextlib import asynccontextmanager
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy import text
import anthropic

client = anthropic.AsyncAnthropic()

# Engine holds the connection pool; pool_size + max_overflow = max connections
engine = create_async_engine(
    "postgresql+asyncpg://localhost/demo",
    pool_size=5,         # persistent connections kept alive
    max_overflow=5,      # temporary connections under peak load
    pool_timeout=30,     # seconds to wait for a free connection
    pool_recycle=1800,   # recycle connections older than 30 min (avoids stale sockets)
    pool_pre_ping=True,  # test connections on checkout (handles DB restarts)
    echo=False,
)

AsyncSessionFactory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

@asynccontextmanager
async def db_session():
    """Context manager: borrow a session from the pool."""
    async with AsyncSessionFactory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise

async def get_order_count(customer_id: int) -> int:
    async with db_session() as session:
        result = await session.execute(
            text("SELECT COUNT(*) FROM orders WHERE customer_id = :cid"),
            {"cid": customer_id},
        )
        return result.scalar_one()

async def agent_task(customer_id: int) -> str:
    count = await get_order_count(customer_id)
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{
            "role": "user",
            "content": f"Customer {customer_id} has {count} orders. Write a retention message.",
        }],
    )
    return response.content[0].text

async def main():
    tasks = [agent_task(i) for i in range(30)]
    results = await asyncio.gather(*tasks)
    print(f"[sqlalchemy] processed {len(results)} tasks with pool_size=5")
    await engine.dispose()

asyncio.run(main())
```

**Expected Token Savings:** SQLAlchemy's `pool_pre_ping` avoids failed queries from stale connections that would waste a full Claude API call; `pool_recycle` prevents silent socket errors in long-running agents.
**Environment:** Agents that use ORM models alongside raw SQL; projects already using SQLAlchemy for schema management.

---

### Option 3 — Connection context manager with semaphore guard

```python
import asyncio
import asyncpg
import anthropic
from contextlib import asynccontextmanager

client = anthropic.AsyncAnthropic()

# Semaphore caps concurrent DB operations independent of pool size
_pool: asyncpg.Pool | None = None
_db_semaphore = asyncio.Semaphore(8)  # at most 8 concurrent DB queries

@asynccontextmanager
async def db_conn():
    """Acquire semaphore slot then borrow a pool connection."""
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            "postgresql://localhost/demo",
            min_size=2,
            max_size=8,
        )
    async with _db_semaphore:
        async with _pool.acquire() as conn:
            yield conn

async def query_inventory(product_id: int) -> dict:
    async with db_conn() as conn:
        row = await conn.fetchrow(
            "SELECT product_id, stock, reorder_level FROM inventory WHERE product_id = $1",
            product_id,
        )
    return dict(row) if row else {"product_id": product_id, "stock": 0}

async def reorder_agent(product_id: int) -> str:
    inv = await query_inventory(product_id)
    if inv["stock"] <= inv.get("reorder_level", 10):
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=128,
            messages=[{
                "role": "user",
                "content": f"Product {product_id} has {inv['stock']} units left. Draft a reorder email.",
            }],
        )
        return response.content[0].text
    return f"Product {product_id} stock OK ({inv['stock']} units)."

async def main():
    tasks = [reorder_agent(i) for i in range(40)]
    results = await asyncio.gather(*tasks)
    print(f"[semaphore] processed {len(results)} agents; max 8 concurrent DB queries")

asyncio.run(main())
```

**Expected Token Savings:** Semaphore prevents DB query storms that would delay Claude API calls waiting for DB results; bounded concurrency keeps per-query latency predictable.
**Environment:** Agents where DB query throughput is a bottleneck; useful when the pool alone isn't enough to prevent query pile-up.

---

### Option 4 — Pool health monitoring and auto-reconnect

```python
import asyncio
import asyncpg
import time
import anthropic

client = anthropic.AsyncAnthropic()

class ManagedPool:
    """asyncpg pool with health checks and connection-count telemetry."""

    def __init__(self, dsn: str, min_size: int = 2, max_size: int = 10):
        self.dsn = dsn
        self.min_size = min_size
        self.max_size = max_size
        self._pool: asyncpg.Pool | None = None
        self._created_at: float = 0

    async def _init(self):
        self._pool = await asyncpg.create_pool(
            self.dsn, min_size=self.min_size, max_size=self.max_size,
            max_inactive_connection_lifetime=60,
        )
        self._created_at = time.monotonic()
        print(f"[pool] created ({self.min_size}–{self.max_size} connections)")

    async def acquire(self):
        if self._pool is None:
            await self._init()
        return self._pool.acquire()

    async def stats(self) -> dict:
        if not self._pool:
            return {}
        return {
            "size":      self._pool.get_size(),
            "idle":      self._pool.get_idle_size(),
            "in_use":    self._pool.get_size() - self._pool.get_idle_size(),
            "age_secs":  int(time.monotonic() - self._created_at),
        }

    async def health_check(self) -> bool:
        try:
            async with await self.acquire() as conn:
                await conn.fetchval("SELECT 1")
            return True
        except Exception as e:
            print(f"[pool] health check failed: {e}")
            if self._pool:
                await self._pool.close()
                self._pool = None
            return False

    async def close(self):
        if self._pool:
            await self._pool.close()

pool = ManagedPool("postgresql://localhost/demo", min_size=2, max_size=10)

async def db_query(sql: str, *args):
    async with await pool.acquire() as conn:
        return await conn.fetch(sql, *args)

async def agent_task(task_id: int) -> str:
    rows = await db_query("SELECT id FROM tasks WHERE status='pending' LIMIT 1")
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        messages=[{"role": "user", "content": f"Summarise task {task_id}."}],
    )
    return response.content[0].text

async def main():
    # Periodic stats logging
    async def log_stats():
        for _ in range(3):
            await asyncio.sleep(1)
            s = await pool.stats()
            print(f"[pool] stats: {s}")

    stats_task = asyncio.create_task(log_stats())
    agent_tasks = [agent_task(i) for i in range(20)]
    await asyncio.gather(stats_task, *agent_tasks, return_exceptions=True)
    await pool.close()

asyncio.run(main())
```

**Expected Token Savings:** Health monitoring catches stale pools before they cause Claude API calls to fail waiting on broken DB results; telemetry helps right-size the pool to avoid over-provisioning.
**Environment:** Long-running agent services (days/weeks uptime); agents that must survive database restarts or network blips.

---

### Option 5 — Connection limiter semaphore without a pool (lightweight alternative)

```python
import asyncio
import asyncpg
import anthropic

client = anthropic.AsyncAnthropic()

# When you cannot use a pool (e.g., serverless functions that don't share state),
# use a semaphore to prevent connection explosions within a single invocation.
MAX_CONCURRENT_CONNECTIONS = 5
_sem = asyncio.Semaphore(MAX_CONCURRENT_CONNECTIONS)

async def run_query(dsn: str, sql: str, *args):
    """Create a connection only when the semaphore allows; close immediately after."""
    async with _sem:
        conn = await asyncpg.connect(dsn)
        try:
            return await conn.fetch(sql, *args)
        finally:
            await conn.close()

async def process_batch(dsn: str, item_ids: list[int]) -> list[dict]:
    async def handle(item_id: int) -> dict:
        rows = await run_query(
            dsn,
            "SELECT id, payload FROM items WHERE id = $1",
            item_id,
        )
        if not rows:
            return {"id": item_id, "summary": "not found"}
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=64,
            messages=[{"role": "user", "content": f"Summarise payload for item {item_id}."}],
        )
        return {"id": item_id, "summary": response.content[0].text}

    results = await asyncio.gather(*[handle(i) for i in item_ids])
    return list(results)

async def main():
    dsn = "postgresql://localhost/demo"
    # 50 items but only 5 concurrent DB connections at any moment
    results = await process_batch(dsn, list(range(50)))
    print(f"[sem] processed {len(results)} items, max {MAX_CONCURRENT_CONNECTIONS} concurrent connections")

asyncio.run(main())
```

**Expected Token Savings:** Semaphore-bounded connections prevent DB saturation in serverless/short-lived contexts; each Claude API call that follows a DB query is guaranteed to have valid data.
**Environment:** AWS Lambda, Google Cloud Functions, or Azure Functions where persistent pool state cannot be shared across invocations.

---

### Option 6 — PgBouncer external pooler integration

```python
import asyncio
import asyncpg
import anthropic

client = anthropic.AsyncAnthropic()

# PgBouncer runs on port 6432 (default) and multiplexes connections to PostgreSQL.
# Application code is identical to direct PostgreSQL — only the DSN changes.
# PgBouncer configuration (pgbouncer.ini):
#   [pgbouncer]
#   listen_port = 6432
#   pool_mode = transaction     # one server connection per transaction (most efficient)
#   max_client_conn = 1000      # clients (agent coroutines)
#   default_pool_size = 20      # actual PostgreSQL connections

PGBOUNCER_DSN = "postgresql://localhost:6432/demo"  # point at PgBouncer, not Postgres

_pool: asyncpg.Pool | None = None

async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            PGBOUNCER_DSN,
            min_size=1,
            max_size=20,      # PgBouncer handles the real multiplexing
            statement_cache_size=0,  # required for PgBouncer transaction mode
        )
    return _pool

async def fetch_events(after_id: int) -> list[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, event_type, payload FROM events WHERE id > $1 ORDER BY id LIMIT 100",
            after_id,
        )
    return [dict(r) for r in rows]

async def event_processor(after_id: int) -> str:
    events = await fetch_events(after_id)
    if not events:
        return "No new events."
    summary_input = "\n".join(f"- {e['event_type']}: {e['payload']}" for e in events[:10])
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": f"Summarise these events:\n{summary_input}"}],
    )
    return response.content[0].text

async def main():
    tasks = [event_processor(i * 100) for i in range(30)]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    ok = [r for r in results if not isinstance(r, Exception)]
    print(f"[pgbouncer] {len(ok)}/{len(results)} tasks succeeded via PgBouncer pool")
    pool = await get_pool()
    await pool.close()

asyncio.run(main())
```

**Expected Token Savings:** PgBouncer transaction-mode pooling allows thousands of agent coroutines to share 20 server connections; no application code changes beyond the DSN; removes connection overhead entirely from Claude API call latency path.
**Environment:** High-throughput multi-process agent deployments (Celery, Gunicorn, Kubernetes pods); recommended when multiple Python processes each run their own asyncpg pool.

---

## Comparison

| Option | Approach | Max Connections | Persistent State | Multi-process Safe | Best For |
|---|---|---|---|---|---|
| 1. asyncpg singleton | Module-level pool | Fixed (max_size) | Yes | No | Single-process asyncio agents |
| 2. SQLAlchemy async | ORM pool | pool_size + overflow | Yes | No | ORM-based agents; mixed SQL/ORM |
| 3. Semaphore + pool | Pool with concurrency cap | Semaphore limit | Yes | No | Query storm prevention |
| 4. Health monitoring | Pool + health checks | Fixed (max_size) | Yes | No | Long-running services, HA agents |
| 5. Semaphore only | Per-call connect + sem | Semaphore limit | No | Yes | Serverless / short-lived functions |
| 6. PgBouncer | External pooler | OS socket limit | External | Yes | Multi-process; highest throughput |
