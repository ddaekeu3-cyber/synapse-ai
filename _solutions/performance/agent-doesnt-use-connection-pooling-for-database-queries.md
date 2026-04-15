---
layout: solution
title: "Agent Doesn't Use Connection Pooling for Database Queries"
category: performance
description: "Agent tool handlers open a new database connection for each tool call, paying TCP handshake and authentication overhead on every query — adding 50–500ms latency per tool call."
tags: [performance, database, connection-pooling, asyncio, latency, tools]
---

## Symptom

Database tool calls take 200–800ms each, even for simple queries that return in <5ms once connected. Load testing reveals that the agent creates hundreds of short-lived connections per minute, exhausting the database's connection limit. `pg_stat_activity` shows thousands of connections in the "authentication" state. Tool call latency is dominated by connection setup, not query execution.

## Root Cause

The tool handler creates a new database connection inside the handler function: `conn = psycopg2.connect(DATABASE_URL)`. Since each tool call invokes a fresh handler invocation, a new connection is established and torn down every time. TCP handshake, TLS negotiation, PostgreSQL authentication, and session initialization can take 100–500ms — dwarfing the actual query time of 1–10ms. This is compounded when tool calls run in parallel.

## Fix

### Option 1: Module-level synchronous connection pool (psycopg2 + psycopg2-pool)

```python
import anthropic
from psycopg2 import pool as pg_pool
import os

# Create pool ONCE at module level — shared across all tool calls
_db_pool = pg_pool.ThreadedConnectionPool(
    minconn=2,
    maxconn=10,
    dsn=os.environ.get("DATABASE_URL", "postgresql://localhost/mydb"),
)

client = anthropic.Anthropic()


def execute_query(sql: str, params: tuple = ()) -> list[dict]:
    """Execute a query using a pooled connection."""
    conn = _db_pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            if cur.description:
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]
            conn.commit()
            return []
    except Exception:
        conn.rollback()
        raise
    finally:
        _db_pool.putconn(conn)  # Return to pool — never close


TOOLS = [
    {
        "name": "query_users",
        "description": "Query users from the database",
        "input_schema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["active", "inactive", "all"], "default": "active"},
                "limit": {"type": "integer", "default": 10},
            },
        },
    },
    {
        "name": "get_user_orders",
        "description": "Get orders for a specific user",
        "input_schema": {
            "type": "object",
            "properties": {
                "user_id": {"type": "string"},
                "days": {"type": "integer", "default": 30},
            },
            "required": ["user_id"],
        },
    },
]


def handle_tool(name: str, inputs: dict) -> str:
    if name == "query_users":
        status = inputs.get("status", "active")
        limit = inputs.get("limit", 10)
        where = "" if status == "all" else f"WHERE status = '{status}'"
        # Simulated — replace with actual execute_query call
        return f"Query: SELECT * FROM users {where} LIMIT {limit} (pooled connection)"
    if name == "get_user_orders":
        user_id = inputs["user_id"]
        days = inputs.get("days", 30)
        return f"Query: SELECT * FROM orders WHERE user_id='{user_id}' AND created_at > NOW() - INTERVAL '{days} days' (pooled)"
    return "Unknown tool"


messages = [{"role": "user", "content": "Show me active users and their recent orders"}]

while True:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        tools=TOOLS,
        messages=messages,
    )
    messages.append({"role": "assistant", "content": response.content})

    if response.stop_reason == "end_turn":
        print(next(b.text for b in response.content if b.type == "text"))
        break

    results = []
    for block in response.content:
        if block.type == "tool_use":
            result = handle_tool(block.name, block.input)
            print(f"[{block.name}] {result[:100]}")
            results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})

    messages.append({"role": "user", "content": results})
```

**Expected Token Savings:** Indirect — pooling cuts tool latency from 200–500ms to 1–10ms; faster tools reduce time-to-completion and enable more parallel tool calls.
**Environment:** Python 3.9+; requires `psycopg2-binary` (`pip install psycopg2-binary`); `ThreadedConnectionPool` is thread-safe.

---

### Option 2: Async connection pool with asyncpg

```python
import asyncio
import os
import anthropic

try:
    import asyncpg
    HAS_ASYNCPG = True
except ImportError:
    HAS_ASYNCPG = False

client = anthropic.AsyncAnthropic()

_pool: "asyncpg.Pool | None" = None
_pool_lock = asyncio.Lock()


async def get_pool() -> "asyncpg.Pool":
    """Lazy-initialize the connection pool (once per process)."""
    global _pool
    if _pool is not None:
        return _pool
    async with _pool_lock:
        if _pool is None:  # Double-check after lock
            _pool = await asyncpg.create_pool(
                dsn=os.environ.get("DATABASE_URL", "postgresql://localhost/mydb"),
                min_size=2,
                max_size=10,
                command_timeout=30,
                max_inactive_connection_lifetime=300,
            )
            print("[DB] Connection pool initialized")
    return _pool


async def query(sql: str, *args) -> list[dict]:
    """Execute a query using the pool — connection is borrowed and returned automatically."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *args)
        return [dict(row) for row in rows]


async def execute(sql: str, *args) -> str:
    """Execute a DML statement using the pool."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(sql, *args)
        return result


TOOLS = [
    {
        "name": "search_products",
        "description": "Search products by name or category",
        "input_schema": {
            "type": "object",
            "properties": {
                "search_term": {"type": "string"},
                "category": {"type": "string"},
                "in_stock": {"type": "boolean", "default": True},
                "limit": {"type": "integer", "default": 20},
            },
            "required": ["search_term"],
        },
    },
    {
        "name": "update_inventory",
        "description": "Update product stock quantity",
        "input_schema": {
            "type": "object",
            "properties": {
                "product_id": {"type": "string"},
                "quantity_delta": {"type": "integer"},
            },
            "required": ["product_id", "quantity_delta"],
        },
    },
]


async def handle_tool_async(name: str, inputs: dict) -> str:
    if name == "search_products":
        # In production: await query("SELECT * FROM products WHERE name ILIKE $1...", f"%{inputs['search_term']}%")
        return f"[Pooled async query] Found 5 products matching '{inputs['search_term']}'"
    if name == "update_inventory":
        # In production: await execute("UPDATE products SET stock = stock + $1 WHERE id = $2", inputs['quantity_delta'], inputs['product_id'])
        return f"[Pooled async execute] Updated stock for product {inputs['product_id']} by {inputs['quantity_delta']}"
    return "Unknown tool"


async def run_agent(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]

    while True:
        response = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            tools=TOOLS,
            messages=messages,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            return next(b.text for b in response.content if b.type == "text")

        results = []
        # Run tool calls in parallel — pool handles concurrent connections
        async def run_one(block):
            result = await handle_tool_async(block.name, block.input)
            return {"type": "tool_result", "tool_use_id": block.id, "content": result}

        tool_blocks = [b for b in response.content if b.type == "tool_use"]
        results = await asyncio.gather(*[run_one(b) for b in tool_blocks])
        messages.append({"role": "user", "content": list(results)})


result = asyncio.run(run_agent("Search for 'laptop' products and update inventory for product-42 by adding 10 units"))
print(result)
```

**Expected Token Savings:** Async pool + parallel tool execution compounds savings; 3 parallel tool calls complete in max(t1, t2, t3) instead of t1+t2+t3.
**Environment:** Python 3.11+; requires `asyncpg` (`pip install asyncpg`); pool handles concurrent async requests efficiently.

---

### Option 3: SQLAlchemy async engine with connection pool

```python
import asyncio
import os
import anthropic

try:
    from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy import text
    HAS_SQLALCHEMY = True
except ImportError:
    HAS_SQLALCHEMY = False

client = anthropic.AsyncAnthropic()

# Create engine ONCE — pool is embedded in the engine
_engine = None


def get_engine():
    global _engine
    if _engine is None and HAS_SQLALCHEMY:
        _engine = create_async_engine(
            os.environ.get("ASYNC_DATABASE_URL", "postgresql+asyncpg://localhost/mydb"),
            pool_size=5,
            max_overflow=10,
            pool_pre_ping=True,          # Validate connections before use
            pool_recycle=3600,           # Recycle connections every hour
            echo=False,
        )
    return _engine


async def db_query(sql: str, params: dict | None = None) -> list[dict]:
    """Execute a parameterized query using the SQLAlchemy async pool."""
    if not HAS_SQLALCHEMY:
        return [{"simulated": True, "sql": sql[:80]}]

    engine = get_engine()
    async with AsyncSession(engine) as session:
        result = await session.execute(text(sql), params or {})
        rows = result.mappings().all()
        return [dict(row) for row in rows]


TOOLS = [
    {
        "name": "get_analytics",
        "description": "Get usage analytics for a date range",
        "input_schema": {
            "type": "object",
            "properties": {
                "start_date": {"type": "string", "description": "ISO 8601 date"},
                "end_date": {"type": "string", "description": "ISO 8601 date"},
                "metric": {"type": "string", "enum": ["pageviews", "sessions", "conversions", "revenue"]},
                "group_by": {"type": "string", "enum": ["day", "week", "month"], "default": "day"},
            },
            "required": ["start_date", "end_date", "metric"],
        },
    },
    {
        "name": "get_top_pages",
        "description": "Get top performing pages by metric",
        "input_schema": {
            "type": "object",
            "properties": {
                "metric": {"type": "string", "enum": ["pageviews", "bounce_rate", "avg_time"]},
                "limit": {"type": "integer", "default": 10},
            },
            "required": ["metric"],
        },
    },
]


async def handle_tool(name: str, inputs: dict) -> str:
    if name == "get_analytics":
        # Production: result = await db_query("SELECT date, SUM(...) FROM analytics WHERE date BETWEEN :start AND :end", {"start": inputs["start_date"], "end": inputs["end_date"]})
        return f"Analytics [{inputs['metric']}] from {inputs['start_date']} to {inputs['end_date']} grouped by {inputs.get('group_by', 'day')}: 30 data points (pooled connection)"

    if name == "get_top_pages":
        return f"Top {inputs.get('limit', 10)} pages by {inputs['metric']}: [page1, page2, ...] (pooled connection)"

    return "Unknown tool"


async def analytics_agent(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]

    while True:
        response = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            tools=TOOLS,
            messages=messages,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            return next(b.text for b in response.content if b.type == "text")

        results = []
        for block in response.content:
            if block.type == "tool_use":
                result = await handle_tool(block.name, block.input)
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})

        messages.append({"role": "user", "content": results})


result = asyncio.run(analytics_agent("Show me pageview analytics for April 2026 and the top 5 pages by views"))
print(result)
```

**Expected Token Savings:** SQLAlchemy pool manages connection lifecycle automatically; `pool_pre_ping` prevents stale connections that cause tool call failures.
**Environment:** Python 3.10+; requires `sqlalchemy>=2.0` and `asyncpg` (`pip install sqlalchemy asyncpg`).

---

### Option 4: Connection pool health monitoring and auto-recovery

```python
import time
import threading
import os
from dataclasses import dataclass, field
import anthropic

client = anthropic.Anthropic()


@dataclass
class PoolMetrics:
    total_queries: int = 0
    total_latency_ms: float = 0.0
    connection_errors: int = 0
    pool_exhausted_count: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def record_query(self, latency_ms: float) -> None:
        with self._lock:
            self.total_queries += 1
            self.total_latency_ms += latency_ms

    def record_error(self, error_type: str) -> None:
        with self._lock:
            if error_type == "pool_exhausted":
                self.pool_exhausted_count += 1
            else:
                self.connection_errors += 1

    @property
    def avg_latency_ms(self) -> float:
        return self.total_latency_ms / max(self.total_queries, 1)

    def report(self) -> str:
        return (
            f"Pool metrics: queries={self.total_queries}, "
            f"avg_latency={self.avg_latency_ms:.1f}ms, "
            f"errors={self.connection_errors}, "
            f"pool_exhausted={self.pool_exhausted_count}"
        )


metrics = PoolMetrics()


class ManagedDBPool:
    """
    Simulated connection pool with health monitoring.
    Replace the simulation with actual psycopg2/asyncpg pool in production.
    """

    def __init__(self, min_conn: int = 2, max_conn: int = 10):
        self.min_conn = min_conn
        self.max_conn = max_conn
        self._available = max_conn  # Simulated
        self._lock = threading.Lock()

    def execute(self, sql: str, params: tuple = ()) -> list[dict]:
        start = time.perf_counter()

        with self._lock:
            if self._available <= 0:
                metrics.record_error("pool_exhausted")
                raise RuntimeError("Connection pool exhausted — too many concurrent queries")
            self._available -= 1

        try:
            # Simulate query execution (1–5ms with pooled connection)
            time.sleep(0.003)
            return [{"result": f"row for: {sql[:50]}"}]
        except Exception as e:
            metrics.record_error("query_error")
            raise
        finally:
            with self._lock:
                self._available += 1
            latency = (time.perf_counter() - start) * 1000
            metrics.record_query(latency)

    def health_check(self) -> dict:
        return {
            "available_connections": self._available,
            "max_connections": self.max_conn,
            "utilization_pct": round((1 - self._available / self.max_conn) * 100, 1),
        }


pool = ManagedDBPool(min_conn=2, max_conn=10)

TOOLS = [
    {
        "name": "query_sales",
        "description": "Query sales data",
        "input_schema": {
            "type": "object",
            "properties": {
                "period": {"type": "string", "enum": ["today", "week", "month", "quarter"]},
                "region": {"type": "string"},
            },
            "required": ["period"],
        },
    },
    {
        "name": "db_health",
        "description": "Check database connection pool health",
        "input_schema": {"type": "object", "properties": {}},
    },
]


def handle_tool(name: str, inputs: dict) -> str:
    if name == "query_sales":
        try:
            rows = pool.execute(
                f"SELECT * FROM sales WHERE period=? AND region=?",
                (inputs["period"], inputs.get("region", "all")),
            )
            return f"Sales data for {inputs['period']}: {len(rows)} rows. {metrics.report()}"
        except RuntimeError as e:
            return f"ERROR: {e}. Try again in a moment."

    if name == "db_health":
        health = pool.health_check()
        return f"Pool health: {health}. {metrics.report()}"

    return "Unknown tool"


messages = [{"role": "user", "content": "Check the database health and query this week's sales for the US region"}]

while True:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        tools=TOOLS,
        messages=messages,
    )
    messages.append({"role": "assistant", "content": response.content})

    if response.stop_reason == "end_turn":
        print(next(b.text for b in response.content if b.type == "text"))
        break

    results = []
    for block in response.content:
        if block.type == "tool_use":
            result = handle_tool(block.name, block.input)
            results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})

    messages.append({"role": "user", "content": results})

print(f"\nFinal: {metrics.report()}")
```

**Expected Token Savings:** Health monitoring exposes pool exhaustion before it causes tool failures; prevents error-recovery turns.
**Environment:** Python 3.9+; replace simulated pool with `ThreadedConnectionPool` for production use.

---

### Option 5: Per-worker connection ownership for multi-process agents

```python
import os
import asyncio
import anthropic
from concurrent.futures import ProcessPoolExecutor

client = anthropic.Anthropic()

# Each worker process owns its own connection pool
# Pool is initialized lazily when the first query runs in that process
_worker_pool = None


def init_worker_pool() -> None:
    """Called once per worker process — not per query."""
    global _worker_pool
    import time
    pid = os.getpid()
    print(f"[Worker {pid}] Initializing DB connection pool")
    time.sleep(0.01)  # Simulate pool setup time (happens once per worker)
    _worker_pool = {"pid": pid, "connections": 3, "ready": True}


def run_db_query_in_worker(query_data: dict) -> dict:
    """Execute in a worker process — uses the process-local pool."""
    global _worker_pool
    pid = os.getpid()

    if _worker_pool is None:
        init_worker_pool()

    import time
    start = time.perf_counter()
    time.sleep(0.002)  # Simulate pooled query (2ms)
    latency = (time.perf_counter() - start) * 1000

    return {
        "result": f"Query '{query_data['sql'][:40]}' completed",
        "latency_ms": round(latency, 2),
        "worker_pid": pid,
        "pool_ready": _worker_pool["ready"],
    }


async def parallel_db_queries(queries: list[dict]) -> list[dict]:
    """Submit multiple DB queries to a process pool — each worker has its own connection pool."""
    loop = asyncio.get_event_loop()
    with ProcessPoolExecutor(max_workers=4, initializer=init_worker_pool) as executor:
        futures = [
            loop.run_in_executor(executor, run_db_query_in_worker, q)
            for q in queries
        ]
        results = await asyncio.gather(*futures)
    return list(results)


async def main():
    queries = [
        {"sql": "SELECT COUNT(*) FROM orders WHERE status='pending'"},
        {"sql": "SELECT SUM(revenue) FROM sales WHERE month='April'"},
        {"sql": "SELECT * FROM users WHERE last_login < NOW() - INTERVAL '30 days'"},
        {"sql": "SELECT AVG(score) FROM reviews WHERE product_id='prod-42'"},
    ]

    print(f"Running {len(queries)} parallel DB queries across worker pool...")
    results = await parallel_db_queries(queries)

    for r in results:
        print(f"  Worker {r['worker_pid']}: {r['result']} ({r['latency_ms']:.1f}ms)")


asyncio.run(main())
```

**Expected Token Savings:** Per-worker pools eliminate connection setup overhead entirely for high-throughput agents; 4 workers × 3 connections = 12 pooled connections for parallel tool calls.
**Environment:** Python 3.11+; `ProcessPoolExecutor` with `initializer` ensures pool setup happens once per worker process, not per query.

---

### Option 6: Connection pool with statement caching for repeated queries

```python
import hashlib
import time
from functools import lru_cache
import anthropic

client = anthropic.Anthropic()


class CachedQueryPool:
    """
    Simulated pool with two-level optimization:
    1. Connection pooling — reuse connections
    2. Query result caching — skip DB for repeated identical queries
    """

    def __init__(self, pool_size: int = 5, cache_ttl_seconds: int = 60):
        self._pool_size = pool_size
        self._cache: dict[str, tuple[list, float]] = {}  # query_hash → (result, timestamp)
        self._cache_ttl = cache_ttl_seconds
        self._query_count = 0
        self._cache_hits = 0

    def _cache_key(self, sql: str, params: tuple) -> str:
        payload = f"{sql}::{params}"
        return hashlib.sha256(payload.encode()).hexdigest()[:16]

    def execute(self, sql: str, params: tuple = (), use_cache: bool = True) -> list[dict]:
        if use_cache:
            key = self._cache_key(sql, params)
            cached_result, cached_at = self._cache.get(key, (None, 0))
            if cached_result is not None and time.time() - cached_at < self._cache_ttl:
                self._cache_hits += 1
                return cached_result  # Cache hit — no DB round-trip

        # Execute against DB (simulated)
        self._query_count += 1
        time.sleep(0.002)  # Simulate 2ms pooled query
        result = [{"sql": sql[:60], "params": params, "rows": 5}]

        if use_cache:
            self._cache[key] = (result, time.time())

        return result

    def stats(self) -> str:
        total = self._query_count + self._cache_hits
        hit_rate = (self._cache_hits / max(total, 1)) * 100
        return (
            f"Pool stats: db_queries={self._query_count}, "
            f"cache_hits={self._cache_hits}/{total} ({hit_rate:.0f}% hit rate)"
        )


pool = CachedQueryPool(pool_size=5, cache_ttl_seconds=30)

TOOLS = [
    {
        "name": "lookup_config",
        "description": "Look up a configuration value (cached — safe to call repeatedly)",
        "input_schema": {
            "type": "object",
            "properties": {
                "config_key": {"type": "string"},
            },
            "required": ["config_key"],
        },
    },
    {
        "name": "get_live_count",
        "description": "Get live row count (not cached — always fresh)",
        "input_schema": {
            "type": "object",
            "properties": {
                "table": {"type": "string"},
            },
            "required": ["table"],
        },
    },
]

messages = [{"role": "user", "content": "Check the max_connections config and get the current count of active_sessions"}]

while True:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        tools=TOOLS,
        messages=messages,
    )
    messages.append({"role": "assistant", "content": response.content})

    if response.stop_reason == "end_turn":
        print(next(b.text for b in response.content if b.type == "text"))
        break

    results = []
    for block in response.content:
        if block.type == "tool_use":
            if block.name == "lookup_config":
                rows = pool.execute(
                    "SELECT value FROM config WHERE key = %s",
                    (block.input["config_key"],),
                    use_cache=True,
                )
                result = f"Config '{block.input['config_key']}': 100 (cached={pool._cache_hits > 0})"
            elif block.name == "get_live_count":
                rows = pool.execute(
                    f"SELECT COUNT(*) FROM {block.input['table']}",
                    use_cache=False,  # Live count — never cache
                )
                result = f"Live count of {block.input['table']}: 1,247 rows"
            else:
                result = "Unknown tool"

            print(f"[{block.name}] {result[:80]} | {pool.stats()}")
            results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})

    messages.append({"role": "user", "content": results})
```

**Expected Token Savings:** Query result caching eliminates repeat DB round-trips for stable data (configs, reference tables); pooling eliminates connection overhead for all queries.
**Environment:** Python 3.9+; replace simulation with real DB library; `use_cache=False` for queries that must be fresh.

---

| Option | Approach | Pool Type | Best For |
|--------|----------|----------|----------|
| 1 | psycopg2 ThreadedConnectionPool | Sync thread-safe | Synchronous agents |
| 2 | asyncpg pool | Async native | Async agents with parallel tools |
| 3 | SQLAlchemy async engine | ORM + pool | SQLAlchemy codebases |
| 4 | Pool with health monitoring | Monitored sync | Production observability |
| 5 | Per-worker process pool | Process-isolated | Multi-process agents |
| 6 | Pool + query result cache | Two-level cache | High-read, stable data |
