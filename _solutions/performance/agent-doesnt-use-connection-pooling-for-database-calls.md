---
layout: solution
title: "Agent Doesn't Use Connection Pooling for Database Calls"
category: performance
description: "Agent opens a new database connection for every query — exhausting connection limits under load, adding 50-200ms overhead per request, and causing cascading failures."
tags: [performance, database, connection-pool, postgresql, asyncio, scalability]
---

## Symptom

Agent creates a new database connection per query:

```python
# Tool called on every agent turn:
async def query_user_data(user_id: str) -> dict:
    # New connection every call — takes 50-200ms to establish
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        row = await conn.fetchrow("SELECT * FROM users WHERE id = $1", user_id)
        return dict(row)
    finally:
        await conn.close()  # Connection torn down immediately after use

# Under load: 50 concurrent agent requests
# → 50 simultaneous TCP connections to PostgreSQL
# PostgreSQL default max_connections = 100
# → Other services can't connect: "FATAL: sorry, too many clients already"
# → Connection establishment overhead: 50 × 150ms = 7.5 seconds wasted per batch
```

Each new connection requires TCP handshake, SSL negotiation, and PostgreSQL auth — 50-200ms overhead that adds up to seconds of latency per agent turn.

## Root Cause

Database connections are expensive to establish and limited by the server's `max_connections` setting. Without a pool, each agent tool call acquires and immediately releases a connection. Under concurrency, this exhausts the connection limit, creates authentication overhead on every query, and prevents connection reuse that would otherwise be free.

## Fix

---

### Option 1: asyncpg Connection Pool — Async-First PostgreSQL Pool

Use `asyncpg.create_pool()` for a process-wide connection pool. Connections are checked out for each query and returned to the pool.

```python
import asyncio
import asyncpg
import anthropic
from contextlib import asynccontextmanager

# Global pool — created once, shared across all tool calls
_pool: asyncpg.Pool | None = None

async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            dsn="postgresql://user:pass@localhost/mydb",
            min_size=2,     # Keep 2 connections always warm
            max_size=10,    # Never exceed 10 simultaneous connections
            max_inactive_connection_lifetime=300,  # Recycle idle connections after 5 min
            command_timeout=10,  # Per-query timeout
        )
    return _pool

async def close_pool() -> None:
    global _pool
    if _pool:
        await _pool.close()
        _pool = None

# Tool function — reuses connections from pool
async def query_user(user_id: str) -> dict | None:
    pool = await get_pool()
    async with pool.acquire() as conn:  # Checkout from pool (usually <1ms)
        row = await conn.fetchrow(
            "SELECT id, name, email, created_at FROM users WHERE id = $1",
            user_id,
        )
        return dict(row) if row else None

async def insert_event(user_id: str, event_type: str, data: dict) -> str:
    pool = await get_pool()
    async with pool.acquire() as conn:
        event_id = await conn.fetchval(
            "INSERT INTO events (user_id, event_type, data) VALUES ($1, $2, $3) RETURNING id",
            user_id, event_type, str(data),
        )
        return str(event_id)

# Agent with database tools
client = anthropic.AsyncAnthropic()

async def run_agent(user_query: str) -> str:
    tools = [
        {
            "name": "query_user",
            "description": "Look up a user by ID",
            "input_schema": {
                "type": "object",
                "properties": {"user_id": {"type": "string"}},
                "required": ["user_id"],
            },
        }
    ]

    messages = [{"role": "user", "content": user_query}]
    for _ in range(5):
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=tools,
            messages=messages,
        )
        tool_uses = [b for b in response.content if b.type == "tool_use"]
        if not tool_uses:
            return response.content[0].text

        # Execute all tool calls concurrently — pool handles connection sharing
        import asyncio
        results = await asyncio.gather(*[
            query_user(tu.input["user_id"]) for tu in tool_uses
        ])

        messages.append({"role": "assistant", "content": response.content})
        messages.append({
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": tu.id, "content": str(r)}
                for tu, r in zip(tool_uses, results)
            ],
        })

    return "Max turns"

async def main():
    await get_pool()  # Warm up pool on startup
    try:
        result = await run_agent("What is user 42's email address?")
        print(result)
    finally:
        await close_pool()

asyncio.run(main())
```

**Expected Token Savings:** Zero direct token savings, but 150ms/query latency reduction × N queries/session reduces total session time. Shorter sessions mean fewer accumulated context tokens from intermediate turns. For 10 queries/session: saves 1.5 seconds = reduces time-based compute costs.
**Environment:** `min_size=2` keeps connections warm for low-traffic periods. `max_size=10` prevents overwhelming PostgreSQL (adjust based on your DB's `max_connections`). Always call `close_pool()` on shutdown to release connections cleanly.

---

### Option 2: SQLAlchemy Async Pool — ORM-Compatible Connection Pooling

Use SQLAlchemy's async engine with built-in connection pooling. Works with any SQLAlchemy-compatible database and provides ORM features.

```python
import asyncio
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy import text
import anthropic

# Engine with pool — created once per process
engine = create_async_engine(
    "postgresql+asyncpg://user:pass@localhost/mydb",
    pool_size=5,          # Base pool size
    max_overflow=10,      # Allow up to 5+10=15 connections under spike
    pool_timeout=30,      # Wait up to 30s for a connection before error
    pool_recycle=1800,    # Recycle connections every 30 min (prevents stale connections)
    pool_pre_ping=True,   # Verify connection is alive before handing out
    echo=False,           # Set True for SQL logging
)

AsyncSessionFactory = async_sessionmaker(engine, expire_on_commit=False)

async def fetch_user(user_id: str) -> dict | None:
    async with AsyncSessionFactory() as session:
        result = await session.execute(
            text("SELECT id, name, email FROM users WHERE id = :uid"),
            {"uid": user_id},
        )
        row = result.mappings().one_or_none()
        return dict(row) if row else None

async def fetch_recent_orders(user_id: str, limit: int = 5) -> list[dict]:
    async with AsyncSessionFactory() as session:
        result = await session.execute(
            text("SELECT id, total, created_at FROM orders WHERE user_id = :uid ORDER BY created_at DESC LIMIT :lim"),
            {"uid": user_id, "lim": limit},
        )
        return [dict(r) for r in result.mappings()]

# Pool stats helper
async def pool_stats() -> dict:
    pool = engine.pool
    return {
        "size": pool.size(),
        "checked_in": pool.checkedin(),
        "checked_out": pool.checkedout(),
        "overflow": pool.overflow(),
        "invalid": pool.invalid(),
    }

client = anthropic.AsyncAnthropic()

async def agent_with_db(query: str) -> str:
    # Log pool health on each agent call (remove in high-throughput prod)
    stats = await pool_stats()
    print(f"Pool: {stats}")

    tools = [
        {"name": "get_user", "description": "Get user details",
         "input_schema": {"type": "object", "properties": {"user_id": {"type": "string"}}, "required": ["user_id"]}},
        {"name": "get_orders", "description": "Get recent orders",
         "input_schema": {"type": "object", "properties": {"user_id": {"type": "string"}}, "required": ["user_id"]}},
    ]

    tool_registry = {"get_user": fetch_user, "get_orders": fetch_recent_orders}
    messages = [{"role": "user", "content": query}]

    for _ in range(5):
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=tools,
            messages=messages,
        )
        tool_uses = [b for b in response.content if b.type == "tool_use"]
        if not tool_uses:
            return response.content[0].text

        results = await asyncio.gather(*[
            tool_registry[tu.name](**tu.input) for tu in tool_uses
        ])

        messages.append({"role": "assistant", "content": response.content})
        messages.append({
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tu.id, "content": str(r)}
                        for tu, r in zip(tool_uses, results)],
        })
    return "Max turns"

async def main():
    result = await agent_with_db("Show me the last 3 orders for user 42")
    print(result)
    await engine.dispose()  # Clean shutdown

asyncio.run(main())
```

**Expected Token Savings:** `pool_pre_ping=True` prevents failed queries from corrupt connections, eliminating retry loops (each ~400 tokens). `pool_recycle` prevents stale connection errors that cause tool failures requiring correction turns.
**Environment:** `pool_size + max_overflow` must stay well below PostgreSQL's `max_connections`. For RDS, set `max_overflow=0` and `pool_size` to 1/3 of instance's max connections. Monitor `checked_out` metric — if consistently near max, increase pool size or add read replicas.

---

### Option 3: PgBouncer Connection Pooler — External Pool for Multi-Process Agents

Deploy PgBouncer as a connection multiplexer. All agent processes connect to PgBouncer (which pools the actual DB connections), making pooling transparent to the application.

```python
import asyncio
import asyncpg
import anthropic

# Connect to PgBouncer instead of PostgreSQL directly
# PgBouncer pools actual DB connections; application sees unlimited "connections"
PGBOUNCER_URL = "postgresql://user:pass@pgbouncer:5432/mydb"

_pool: asyncpg.Pool | None = None

async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        # Smaller pool to PgBouncer — it handles the heavy multiplexing
        _pool = await asyncpg.create_pool(
            dsn=PGBOUNCER_URL,
            min_size=1,
            max_size=5,  # PgBouncer multiplexes these 5 into many DB connections
            # PgBouncer transaction mode: prepare statements not supported
            statement_cache_size=0,  # Disable prepared statement cache for PgBouncer
        )
    return _pool

async def execute_query(sql: str, *args) -> list[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *args)
        return [dict(r) for r in rows]

async def run_analytics_query(metric: str, start_date: str, end_date: str) -> dict:
    rows = await execute_query(
        """
        SELECT DATE(created_at) as date, COUNT(*) as count, SUM(amount) as total
        FROM events
        WHERE metric = $1 AND created_at BETWEEN $2::date AND $3::date
        GROUP BY DATE(created_at)
        ORDER BY date
        """,
        metric, start_date, end_date,
    )
    return {
        "metric": metric,
        "data": [{"date": str(r["date"]), "count": r["count"], "total": float(r["total"] or 0)}
                 for r in rows],
    }

# Agent tool
client = anthropic.AsyncAnthropic()

async def run_analytics_agent(user_question: str) -> str:
    tools = [
        {
            "name": "query_analytics",
            "description": "Query analytics metrics for a date range",
            "input_schema": {
                "type": "object",
                "properties": {
                    "metric": {"type": "string"},
                    "start_date": {"type": "string", "description": "YYYY-MM-DD"},
                    "end_date": {"type": "string", "description": "YYYY-MM-DD"},
                },
                "required": ["metric", "start_date", "end_date"],
            },
        }
    ]
    messages = [{"role": "user", "content": user_question}]

    for _ in range(5):
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=tools,
            messages=messages,
        )
        tool_uses = [b for b in response.content if b.type == "tool_use"]
        if not tool_uses:
            return response.content[0].text

        results = await asyncio.gather(*[
            run_analytics_query(**tu.input) for tu in tool_uses
        ])
        messages.append({"role": "assistant", "content": response.content})
        messages.append({
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tu.id, "content": str(r)}
                        for tu, r in zip(tool_uses, results)],
        })
    return "Max turns"

print("Note: requires PgBouncer running at pgbouncer:5432")
print("PgBouncer config: pool_mode=transaction, max_client_conn=1000, default_pool_size=20")
```

**Expected Token Savings:** PgBouncer in transaction mode multiplexes 1,000 application "connections" through 20 actual DB connections. Eliminates `max_connections` exhaustion errors entirely — each error would have required a retry turn (~400 tokens).
**Environment:** PgBouncer `transaction` mode is incompatible with prepared statements — set `statement_cache_size=0` in asyncpg. `session` mode supports prepared statements but has lower multiplexing efficiency. Use Docker: `pgbouncer:latest` with appropriate `pgbouncer.ini`.

---

### Option 4: Redis Connection Pool for Caching Frequent DB Queries

Add a Redis cache layer in front of the database. Frequent agent queries hit Redis (sub-millisecond) instead of PostgreSQL (10-50ms).

```python
import asyncio
import json
import time
import hashlib
import redis.asyncio as aioredis
import asyncpg
import anthropic

# Redis pool for caching
redis_pool = aioredis.ConnectionPool.from_url(
    "redis://localhost:6379",
    max_connections=20,
    decode_responses=True,
)
redis_client = aioredis.Redis(connection_pool=redis_pool)

# PostgreSQL pool for source-of-truth queries
_pg_pool: asyncpg.Pool | None = None

async def get_pg_pool() -> asyncpg.Pool:
    global _pg_pool
    if _pg_pool is None:
        _pg_pool = await asyncpg.create_pool("postgresql://user:pass@localhost/mydb",
                                              min_size=2, max_size=8)
    return _pg_pool

def cache_key(query: str, *args) -> str:
    content = f"{query}:{':'.join(str(a) for a in args)}"
    return f"agent:db:{hashlib.sha256(content.encode()).hexdigest()[:16]}"

async def cached_query(sql: str, *args, ttl: int = 60) -> list[dict]:
    """Query with Redis cache. ttl=0 bypasses cache (for write-heavy data)."""
    if ttl == 0:
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(sql, *args)
            return [dict(r) for r in rows]

    key = cache_key(sql, *args)
    cached = await redis_client.get(key)
    if cached:
        return json.loads(cached)

    # Cache miss: query DB and cache result
    pool = await get_pg_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *args)
        result = [dict(r) for r in rows]

    await redis_client.setex(key, ttl, json.dumps(result, default=str))
    return result

async def get_product_catalog(category: str) -> list[dict]:
    """Product catalog changes infrequently — cache for 5 minutes."""
    return await cached_query(
        "SELECT id, name, price, stock FROM products WHERE category = $1 ORDER BY name",
        category,
        ttl=300,
    )

async def get_live_inventory(product_id: str) -> dict:
    """Inventory changes frequently — no cache."""
    results = await cached_query(
        "SELECT stock, reserved FROM inventory WHERE product_id = $1",
        product_id,
        ttl=0,  # No cache for live inventory
    )
    return results[0] if results else {}

client = anthropic.AsyncAnthropic()

async def shopping_agent(user_query: str) -> str:
    tools = [
        {"name": "get_catalog", "description": "Get products by category",
         "input_schema": {"type": "object", "properties": {"category": {"type": "string"}}, "required": ["category"]}},
        {"name": "check_inventory", "description": "Check live inventory for a product",
         "input_schema": {"type": "object", "properties": {"product_id": {"type": "string"}}, "required": ["product_id"]}},
    ]
    tool_fns = {"get_catalog": lambda **kw: get_product_catalog(kw["category"]),
                "check_inventory": lambda **kw: get_live_inventory(kw["product_id"])}
    messages = [{"role": "user", "content": user_query}]

    for _ in range(5):
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=tools,
            messages=messages,
        )
        tool_uses = [b for b in response.content if b.type == "tool_use"]
        if not tool_uses:
            return response.content[0].text

        results = await asyncio.gather(*[tool_fns[tu.name](**tu.input) for tu in tool_uses])
        messages.append({"role": "assistant", "content": response.content})
        messages.append({
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tu.id, "content": str(r)}
                        for tu, r in zip(tool_uses, results)],
        })
    return "Max turns"

asyncio.run(shopping_agent("What electronics are available and is product P-123 in stock?"))
```

**Expected Token Savings:** Cache hits serve results in <1ms vs 20-50ms for DB queries. For a session with 10 catalog queries (5 unique, repeated twice): 5 cache hits save 5 × 50ms = 250ms query time. More importantly, cache prevents stale-connection errors that cause tool failures requiring correction turns (~400 tokens each).
**Environment:** TTL strategy: static reference data (product catalog, config) → 5-15 min TTL; user-specific data → 30-60s TTL; live inventory/prices → no cache (ttl=0). Redis pool `max_connections=20` is separate from PostgreSQL pool.

---

### Option 5: Connection Pool with Query Batching — Coalesce Multiple Queries

When the agent needs multiple queries in one turn, batch them into a single DB round-trip using `executemany` or multi-row fetches.

```python
import asyncio
import asyncpg
import anthropic

_pool: asyncpg.Pool | None = None

async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool("postgresql://user:pass@localhost/mydb",
                                          min_size=2, max_size=8)
    return _pool

async def fetch_users_batch(user_ids: list[str]) -> dict[str, dict]:
    """Fetch multiple users in ONE query instead of N queries."""
    if not user_ids:
        return {}
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, name, email FROM users WHERE id = ANY($1::text[])",
            user_ids,
        )
        return {str(r["id"]): dict(r) for r in rows}

async def fetch_orders_batch(user_ids: list[str], limit_per_user: int = 3) -> dict[str, list]:
    """Fetch recent orders for multiple users in one query."""
    if not user_ids:
        return {}
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT DISTINCT ON (user_id) user_id, id, total, created_at
            FROM orders
            WHERE user_id = ANY($1::text[])
            ORDER BY user_id, created_at DESC
            """,
            user_ids,
        )
        result: dict[str, list] = {uid: [] for uid in user_ids}
        for r in rows:
            result[str(r["user_id"])].append({"id": r["id"], "total": float(r["total"])})
        return result

client = anthropic.AsyncAnthropic()

async def run_batch_agent(query: str, user_ids: list[str]) -> str:
    # Pre-fetch all needed data in two batched queries instead of 2N individual queries
    users, orders = await asyncio.gather(
        fetch_users_batch(user_ids),
        fetch_orders_batch(user_ids),
    )

    # Inject into context (no tool calls needed — data is pre-loaded)
    user_context = "\n".join(
        f"User {uid}: {users.get(uid, {}).get('name', 'unknown')} — "
        f"{len(orders.get(uid, []))} recent orders"
        for uid in user_ids
    )

    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=f"Available user data:\n{user_context}",
        messages=[{"role": "user", "content": query}],
    )
    return response.content[0].text

result = asyncio.run(run_batch_agent(
    "Which of these users has the most recent order activity?",
    ["user_1", "user_2", "user_3", "user_4", "user_5"],
))
print(result)
```

**Expected Token Savings:** 5 users in 1 query vs 5 separate queries: 4 fewer round-trips to DB = 4 × 30ms saved = 120ms. Batching also reduces context clutter — pre-loaded data injected once vs 5 tool-call/result pairs (~200 tokens overhead each). Net context savings: ~800 tokens per 5-user batch.
**Environment:** `ANY($1::text[])` works for PostgreSQL array lookups. For MySQL, use `WHERE id IN (...)`. Batch size should be bounded — for very large ID sets (>1000), chunk into batches of 500.

---

### Option 6: Pool Health Monitoring — Detect and Recover from Pool Exhaustion

Monitor pool health metrics and automatically reduce load or alert when the pool approaches exhaustion.

```python
import asyncio
import time
import asyncpg
import anthropic
from dataclasses import dataclass

@dataclass
class PoolHealth:
    total: int
    available: int
    in_use: int
    max_size: int
    utilisation_pct: float
    is_healthy: bool

_pool: asyncpg.Pool | None = None

async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            "postgresql://user:pass@localhost/mydb",
            min_size=2, max_size=10,
        )
    return _pool

async def check_pool_health() -> PoolHealth:
    pool = await get_pool()
    total = pool.get_size()
    available = pool.get_idle_size()
    in_use = total - available
    max_size = 10  # matches max_size above
    utilisation = (in_use / max_size) * 100
    return PoolHealth(
        total=total,
        available=available,
        in_use=in_use,
        max_size=max_size,
        utilisation_pct=utilisation,
        is_healthy=utilisation < 80,
    )

async def query_with_health_check(sql: str, *args, timeout: float = 10.0) -> list[dict]:
    pool = await get_pool()
    health = await check_pool_health()

    if health.utilisation_pct > 90:
        print(f"[WARN] Pool {health.utilisation_pct:.0f}% utilised — consider reducing concurrency")

    try:
        async with asyncio.timeout(timeout):
            async with pool.acquire() as conn:
                rows = await conn.fetch(sql, *args)
                return [dict(r) for r in rows]
    except asyncio.TimeoutError:
        health2 = await check_pool_health()
        raise RuntimeError(
            f"DB query timed out after {timeout}s. "
            f"Pool health: {health2.utilisation_pct:.0f}% utilised, "
            f"{health2.available} connections available."
        )

# Comparison table
"""
| Approach | Best For | Pool Type | Requires | Latency Reduction |
|---|---|---|---|---|
| Option 1: asyncpg pool | Async Python | In-process | asyncpg | 150ms/query |
| Option 2: SQLAlchemy async | ORM workflows | In-process | sqlalchemy | 150ms/query |
| Option 3: PgBouncer | Multi-process fleets | External | PgBouncer | 150ms/query |
| Option 4: Redis cache | Read-heavy queries | In-process | redis | 50ms → <1ms |
| Option 5: Query batching | N+1 query patterns | In-process | asyncpg | N × 30ms |
| Option 6: Health monitoring | Production resilience | In-process | asyncpg | Prevents failures |
"""

async def main():
    health = await check_pool_health()
    print(f"Pool health: {health}")
    rows = await query_with_health_check("SELECT 1 as alive")
    print(f"DB alive: {rows}")

asyncio.run(main())
```

**Expected Token Savings:** Health monitoring prevents pool exhaustion errors that cause tool failures requiring correction turns (~400 tokens each). Early warning at 80% utilisation gives time to add read replicas or throttle incoming requests before failures occur. Each prevented failure: saves 1-3 correction turns × 400 tokens.
**Environment:** Log `utilisation_pct` to your metrics system (Datadog, CloudWatch, Prometheus). Alert when consistently >70%. If utilisation is routinely high, either increase `max_size` (if DB allows) or add PgBouncer (Option 3) to handle more application connections.
