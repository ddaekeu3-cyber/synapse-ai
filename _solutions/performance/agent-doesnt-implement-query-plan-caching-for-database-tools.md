---
title: "Agent Doesn't Implement Query Plan Caching for Database Tools"
description: "Agents that issue repeated SQL queries re-parse and re-plan identical statements on every call, wasting CPU and adding latency. Implement prepared statement reuse, plan cache warming, and parameterized query pooling to eliminate planning overhead."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-query-plan-caching-for-database-tools
tags: [query-plan-cache, prepared-statements, database, performance, sql, caching]
symptoms:
  - "Database CPU spikes on repeated identical queries from agent tool calls"
  - "pg_stat_statements shows parse/plan time equal to execution time"
  - "Same SQL string submitted as unprepared query on every agent turn"
  - "Tool call latency is dominated by query planning, not data retrieval"
  - "Connection pool exhausted because each query opens a new prepared statement namespace"
---

## Why This Happens

Most agent tool implementations build SQL strings and execute them directly. Each execution causes the database to tokenize, parse, and plan the query from scratch. For PostgreSQL, this can exceed the actual I/O cost for simple queries against warm indexes. Prepared statements let the database cache the plan after the first parse, reducing subsequent executions to parameter binding + execution only.

## Solution 1: Prepared Statement Pool with Async Connection Affinity

```python
import asyncio
import hashlib
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

@dataclass
class PreparedStatement:
    name: str       # server-side statement name
    sql: str
    param_types: Tuple[str, ...]
    conn_id: int    # prepared statements are per-connection

class PreparedStatementPool:
    """
    Maintains a registry of prepared statements per connection.
    On first use of a SQL template, PREPARE it on the connection.
    Subsequent calls EXECUTE the cached plan.
    """

    def __init__(self, pool):
        self._pool = pool
        # conn_id -> {sql_hash -> PreparedStatement}
        self._registry: Dict[int, Dict[str, PreparedStatement]] = {}
        self._lock = asyncio.Lock()

    def _sql_hash(self, sql: str) -> str:
        return hashlib.sha1(sql.encode()).hexdigest()[:16]

    def _stmt_name(self, sql_hash: str, conn_id: int) -> str:
        return f"plan_{sql_hash}_{conn_id}"

    async def execute(self, sql: str, params: Tuple = ()) -> List[dict]:
        async with self._pool.acquire() as conn:
            conn_id = id(conn)
            sql_hash = self._sql_hash(sql)
            registry = self._registry.setdefault(conn_id, {})

            if sql_hash not in registry:
                async with self._lock:
                    if sql_hash not in registry:
                        name = self._stmt_name(sql_hash, conn_id)
                        await conn.execute(f"PREPARE {name} AS {sql}")
                        registry[sql_hash] = PreparedStatement(
                            name=name, sql=sql,
                            param_types=(), conn_id=conn_id,
                        )

            stmt = registry[sql_hash]
            placeholders = ", ".join(f"${i+1}" for i in range(len(params)))
            rows = await conn.fetch(f"EXECUTE {stmt.name} ({placeholders})", *params)
            return [dict(r) for r in rows]

    async def deallocate_all(self, conn_id: int) -> None:
        registry = self._registry.pop(conn_id, {})
        # Called when connection is returned to pool or closed
        async with self._pool.acquire() as conn:
            for stmt in registry.values():
                try:
                    await conn.execute(f"DEALLOCATE {stmt.name}")
                except Exception:
                    pass


# Usage in agent tool
class DatabaseQueryTool:
    def __init__(self, stmt_pool: PreparedStatementPool):
        self._pool = stmt_pool

    async def get_user(self, user_id: int) -> Optional[dict]:
        rows = await self._pool.execute(
            "SELECT id, name, email FROM users WHERE id = $1", (user_id,)
        )
        return rows[0] if rows else None

    async def list_orders(self, user_id: int, limit: int = 50) -> List[dict]:
        return await self._pool.execute(
            "SELECT order_id, total, status FROM orders WHERE user_id = $1 ORDER BY created_at DESC LIMIT $2",
            (user_id, limit),
        )
```

## Solution 2: Query Template Registry with Pre-Warmed Plans

```python
import asyncio
from typing import Callable, Dict, List, NamedTuple

class QueryTemplate(NamedTuple):
    name: str
    sql: str
    description: str

QUERY_TEMPLATES: List[QueryTemplate] = [
    QueryTemplate("get_user_by_id",
                  "SELECT * FROM users WHERE id = $1",
                  "Fetch single user by PK"),
    QueryTemplate("list_user_orders",
                  "SELECT * FROM orders WHERE user_id = $1 AND status = $2 ORDER BY created_at DESC LIMIT $3",
                  "Paginated orders for user"),
    QueryTemplate("upsert_agent_state",
                  "INSERT INTO agent_state (session_id, state_json, updated_at) VALUES ($1, $2, NOW()) "
                  "ON CONFLICT (session_id) DO UPDATE SET state_json = EXCLUDED.state_json, updated_at = NOW()",
                  "Save or update agent session state"),
    QueryTemplate("find_similar_embeddings",
                  "SELECT chunk_id, content, embedding <=> $1 AS distance FROM embeddings ORDER BY distance LIMIT $2",
                  "ANN search via pgvector cosine distance"),
]

class QueryPlanWarmer:
    """
    At startup, PREPAREs all registered query templates on every connection
    in the pool so the first real call is plan-cache-hot.
    """

    def __init__(self, pool, templates: List[QueryTemplate]):
        self._pool = pool
        self._templates = templates

    async def warm(self) -> None:
        tasks = [self._warm_single(tmpl) for tmpl in self._templates]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for tmpl, result in zip(self._templates, results):
            if isinstance(result, Exception):
                print(f"[plan_warmer] failed to prepare '{tmpl.name}': {result}")
            else:
                print(f"[plan_warmer] prepared '{tmpl.name}'")

    async def _warm_single(self, tmpl: QueryTemplate) -> None:
        async with self._pool.acquire() as conn:
            # PREPARE with the canonical statement name
            existing = await conn.fetchval(
                "SELECT name FROM pg_prepared_statements WHERE name = $1", tmpl.name
            )
            if not existing:
                await conn.execute(f"PREPARE {tmpl.name} AS {tmpl.sql}")

class PreWarmedQueryRunner:
    def __init__(self, pool, templates: List[QueryTemplate]):
        self._pool = pool
        self._by_name = {t.name: t for t in templates}

    async def run(self, template_name: str, *params) -> List[dict]:
        tmpl = self._by_name.get(template_name)
        if tmpl is None:
            raise ValueError(f"Unknown query template: {template_name}")
        placeholders = ", ".join(f"${i+1}" for i in range(len(params)))
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(f"EXECUTE {template_name} ({placeholders})", *params)
            return [dict(r) for r in rows]
```

## Solution 3: LRU Plan Cache with Hit-Rate Tracking

```python
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Optional

@dataclass
class PlanCacheEntry:
    sql_hash: str
    sql: str
    prepared_name: str
    hits: int = 0
    misses: int = 0
    last_used: float = field(default_factory=time.monotonic)
    avg_execution_ms: float = 0.0

class LRUPlanCache:
    """
    LRU cache for prepared statement metadata.
    Evicts least-recently-used entries when capacity is reached.
    """

    def __init__(self, capacity: int = 256):
        self._capacity = capacity
        self._cache: OrderedDict[str, PlanCacheEntry] = OrderedDict()

    def get(self, sql_hash: str) -> Optional[PlanCacheEntry]:
        if sql_hash not in self._cache:
            return None
        self._cache.move_to_end(sql_hash)
        entry = self._cache[sql_hash]
        entry.hits += 1
        entry.last_used = time.monotonic()
        return entry

    def put(self, entry: PlanCacheEntry) -> Optional[str]:
        """Returns the evicted prepared_name if cache was full, else None."""
        evicted_name = None
        if entry.sql_hash in self._cache:
            self._cache.move_to_end(entry.sql_hash)
        else:
            if len(self._cache) >= self._capacity:
                _, evicted = self._cache.popitem(last=False)
                evicted_name = evicted.prepared_name
            self._cache[entry.sql_hash] = entry
        return evicted_name

    def hit_rate(self) -> float:
        total_hits = sum(e.hits for e in self._cache.values())
        total_misses = sum(e.misses for e in self._cache.values())
        total = total_hits + total_misses
        return total_hits / total if total > 0 else 0.0

    def top_queries(self, n: int = 10) -> list:
        return sorted(self._cache.values(), key=lambda e: e.hits, reverse=True)[:n]


class CachedQueryExecutor:
    def __init__(self, pool, plan_cache: LRUPlanCache):
        self._pool = pool
        self._plan_cache = plan_cache

    async def execute(self, sql: str, params: tuple = ()) -> list:
        import hashlib
        sql_hash = hashlib.sha1(sql.encode()).hexdigest()[:16]
        entry = self._plan_cache.get(sql_hash)

        async with self._pool.acquire() as conn:
            if entry is None:
                # First time: prepare and cache
                name = f"plan_{sql_hash}"
                await conn.execute(f"PREPARE {name} AS {sql}")
                entry = PlanCacheEntry(sql_hash=sql_hash, sql=sql, prepared_name=name)
                evicted = self._plan_cache.put(entry)
                if evicted:
                    try:
                        await conn.execute(f"DEALLOCATE {evicted}")
                    except Exception:
                        pass

            t0 = time.monotonic()
            placeholders = ", ".join(f"${i+1}" for i in range(len(params)))
            rows = await conn.fetch(f"EXECUTE {entry.prepared_name} ({placeholders})", *params)
            elapsed_ms = (time.monotonic() - t0) * 1000
            entry.avg_execution_ms = (entry.avg_execution_ms * 0.9 + elapsed_ms * 0.1)
            return [dict(r) for r in rows]
```

## Solution 4: Parameterized Query Builder that Prevents Plan Fragmentation

```python
import re
from typing import Any, List, Tuple

class ParameterizedQueryBuilder:
    """
    Ensures that dynamic values are always extracted as parameters
    rather than interpolated into the SQL string. This prevents
    per-value plan cache fragmentation (a new plan for every user_id literal).
    """

    def __init__(self):
        self._sql_parts: List[str] = []
        self._params: List[Any] = []
        self._counter: int = 0

    def literal(self, text: str) -> "ParameterizedQueryBuilder":
        self._sql_parts.append(text)
        return self

    def param(self, value: Any) -> "ParameterizedQueryBuilder":
        self._counter += 1
        self._sql_parts.append(f"${self._counter}")
        self._params.append(value)
        return self

    def build(self) -> Tuple[str, tuple]:
        return " ".join(self._sql_parts), tuple(self._params)

    @staticmethod
    def safe_identifier(name: str) -> str:
        """Validate table/column names to allow safe literal inclusion."""
        if not re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', name):
            raise ValueError(f"Unsafe SQL identifier: {name!r}")
        return name


def build_agent_query(table: str, filters: dict, limit: int) -> Tuple[str, tuple]:
    """
    Build a parameterized query from agent-supplied filter dict.
    Values go to params; table/column names are validated identifiers.
    """
    builder = ParameterizedQueryBuilder()
    safe_table = ParameterizedQueryBuilder.safe_identifier(table)
    builder.literal(f"SELECT * FROM {safe_table} WHERE 1=1")

    for col, val in filters.items():
        safe_col = ParameterizedQueryBuilder.safe_identifier(col)
        if val is None:
            builder.literal(f"AND {safe_col} IS NULL")
        elif isinstance(val, list):
            placeholders = []
            for v in val:
                builder._counter += 1
                placeholders.append(f"${builder._counter}")
                builder._params.append(v)
            builder.literal(f"AND {safe_col} IN ({', '.join(placeholders)})")
        else:
            builder.literal(f"AND {safe_col} =").param(val)

    builder.literal("LIMIT").param(limit)
    return builder.build()
```

## Solution 5: Query Plan Cache Metrics Exporter

```python
import asyncio
import time
from dataclasses import dataclass, field
from typing import Dict

@dataclass
class QueryMetrics:
    sql_hash: str
    call_count: int = 0
    total_ms: float = 0.0
    plan_cache_hits: int = 0
    plan_cache_misses: int = 0
    errors: int = 0

class QueryMetricsCollector:
    def __init__(self):
        self._metrics: Dict[str, QueryMetrics] = {}

    def record(self, sql_hash: str, elapsed_ms: float, was_cache_hit: bool, error: bool = False) -> None:
        m = self._metrics.setdefault(sql_hash, QueryMetrics(sql_hash=sql_hash))
        m.call_count += 1
        m.total_ms += elapsed_ms
        if was_cache_hit:
            m.plan_cache_hits += 1
        else:
            m.plan_cache_misses += 1
        if error:
            m.errors += 1

    def summary(self) -> dict:
        total_calls = sum(m.call_count for m in self._metrics.values())
        total_hits = sum(m.plan_cache_hits for m in self._metrics.values())
        return {
            "unique_queries": len(self._metrics),
            "total_calls": total_calls,
            "plan_cache_hit_rate": total_hits / total_calls if total_calls else 0.0,
            "avg_latency_ms": (
                sum(m.total_ms for m in self._metrics.values()) / total_calls
                if total_calls else 0.0
            ),
            "top_slow_queries": sorted(
                [{"hash": m.sql_hash, "avg_ms": m.total_ms / m.call_count}
                 for m in self._metrics.values() if m.call_count > 0],
                key=lambda x: x["avg_ms"], reverse=True,
            )[:5],
        }

    async def export_loop(self, interval_seconds: float = 60.0) -> None:
        while True:
            await asyncio.sleep(interval_seconds)
            s = self.summary()
            print(
                f"[query_plan_cache] unique={s['unique_queries']} "
                f"calls={s['total_calls']} "
                f"hit_rate={s['plan_cache_hit_rate']:.1%} "
                f"avg_ms={s['avg_latency_ms']:.2f}"
            )
```

## Solution 6: Unified QueryPlanCacheAdapter for Agent Tools

```python
import asyncio
import hashlib
import time
from typing import Any, List, Optional, Tuple

class QueryPlanCacheAdapter:
    """
    Full-stack adapter: parameterized builder + LRU plan cache
    + metrics. Drop-in replacement for raw asyncpg pool.execute().
    """

    def __init__(self, pool, capacity: int = 512):
        self._pool = pool
        self._plan_cache = LRUPlanCache(capacity=capacity)
        self._metrics = QueryMetricsCollector()

    async def execute(self, sql: str, params: Tuple = ()) -> List[dict]:
        sql_hash = hashlib.sha1(sql.encode()).hexdigest()[:16]
        cache_hit = self._plan_cache.get(sql_hash) is not None
        t0 = time.monotonic()
        error = False
        try:
            result = await self._prepared_execute(sql, sql_hash, params)
            return result
        except Exception as exc:
            error = True
            raise
        finally:
            elapsed = (time.monotonic() - t0) * 1000
            self._metrics.record(sql_hash, elapsed, was_cache_hit=cache_hit, error=error)

    async def _prepared_execute(self, sql: str, sql_hash: str, params: tuple) -> List[dict]:
        entry = self._plan_cache.get(sql_hash)
        async with self._pool.acquire() as conn:
            if entry is None:
                name = f"plan_{sql_hash}"
                await conn.execute(f"PREPARE {name} AS {sql}")
                entry = PlanCacheEntry(sql_hash=sql_hash, sql=sql, prepared_name=name)
                evicted = self._plan_cache.put(entry)
                if evicted:
                    try:
                        await conn.execute(f"DEALLOCATE {evicted}")
                    except Exception:
                        pass
            placeholders = ", ".join(f"${i+1}" for i in range(len(params)))
            rows = await conn.fetch(
                f"EXECUTE {entry.prepared_name} ({placeholders})", *params
            )
            return [dict(r) for r in rows]

    def metrics_summary(self) -> dict:
        return self._metrics.summary()

    def plan_cache_hit_rate(self) -> float:
        return self._plan_cache.hit_rate()
```

## Comparison

| Approach | Plan Reuse | Crash Safe | Connection Affinity | Observability |
|---|---|---|---|---|
| PreparedStatementPool | Per-connection PREPARE | No (in-memory) | Yes (conn_id keyed) | Print logs |
| QueryPlanWarmer | Startup pre-PREPARE | Yes (DB-side) | No (any conn) | Startup log |
| LRUPlanCache + CachedQueryExecutor | LRU eviction | No | No | Hit rate + avg ms |
| ParameterizedQueryBuilder | Prevents fragmentation | N/A | N/A | None |
| QueryMetricsCollector | N/A | N/A | N/A | Full metrics export |
| QueryPlanCacheAdapter | LRU + metrics combined | No | No | Full |

**Best for production**: Use `QueryPlanWarmer` at startup to pre-PREPARE all known query templates, then `QueryPlanCacheAdapter` for dynamic queries. Use `ParameterizedQueryBuilder` to ensure values are never interpolated into SQL strings (prevents both plan fragmentation and SQL injection).
