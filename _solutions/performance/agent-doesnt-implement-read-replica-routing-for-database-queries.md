---
title: "Agent Doesn't Implement Read Replica Routing for Database Queries"
description: "How to route agent read queries to replicas and writes to the primary — using automatic primary/replica detection, read-your-writes consistency, connection pools per role, and replica lag monitoring — to scale database throughput without upgrading hardware."
date: 2025-01-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-read-replica-routing-for-database-queries
tags:
  - performance
  - database
  - read-replica
  - connection-pooling
  - query-routing
  - scalability
  - read-your-writes
symptoms:
  - "All database queries go to the primary even though 90% are reads"
  - "Primary database CPU spikes when agents run analytics or embedding lookups"
  - "No read replicas configured despite having them available in cloud RDS/Aurora"
  - "Agent queries compete with write-heavy primary workloads causing latency"
  - "Replica lag not monitored — agents occasionally read stale data without knowing"
  - "Connection pool exhausted on primary during peak agent load"
---

## Why This Happens

AI agents issue a large volume of read queries — looking up knowledge base entries, fetching conversation history, retrieving user preferences, loading tool configurations. When all of these hit the primary database alongside writes, the primary becomes the bottleneck. Read replicas exist precisely to offload read traffic, but most agents default to a single database connection string and never route reads to replicas.

The critical challenge is maintaining read-your-writes consistency: after an agent writes a record, it should read back the latest value, not a stale replica copy. Proper replica routing handles this with session-level primary stickiness after writes.

---

## Solution 1: Simple Read/Write Router

A connection-level router that sends write operations to primary and read operations to a randomly selected replica.

```python
import asyncio
import random
import time
from dataclasses import dataclass, field
from typing import Any, Optional, Callable, Awaitable
from enum import Enum

class QueryRole(Enum):
    READ  = "read"
    WRITE = "write"

@dataclass
class DatabaseNode:
    host: str
    port: int
    role: QueryRole
    weight: float = 1.0   # For weighted replica selection
    max_lag_ms: float = 5000.0  # Max acceptable replica lag

@dataclass
class ConnectionPool:
    """Simulates a database connection pool per node."""
    node: DatabaseNode
    size: int = 10
    _connections: list = field(default_factory=list)
    _in_use: int = 0

    async def execute(self, query: str, params: tuple = ()) -> Any:
        """Execute query and return result."""
        # Simulate query execution
        await asyncio.sleep(0.001)
        return {"rows": [], "query": query, "node": self.node.host}


class ReadWriteRouter:
    """
    Routes database queries to primary (writes) or replicas (reads).
    Falls back to primary if no replicas are available.
    """

    def __init__(self):
        self._primary: Optional[ConnectionPool] = None
        self._replicas: list[ConnectionPool] = []

    def set_primary(self, node: DatabaseNode) -> None:
        self._primary = ConnectionPool(node)

    def add_replica(self, node: DatabaseNode) -> None:
        self._replicas.append(ConnectionPool(node))

    def _select_replica(self) -> Optional[ConnectionPool]:
        """Weighted random selection among healthy replicas."""
        if not self._replicas:
            return None
        weights = [r.node.weight for r in self._replicas]
        total = sum(weights)
        r = random.random() * total
        cumulative = 0.0
        for pool, weight in zip(self._replicas, weights):
            cumulative += weight
            if r <= cumulative:
                return pool
        return self._replicas[-1]

    async def execute_read(self, query: str, params: tuple = ()) -> Any:
        pool = self._select_replica() or self._primary
        if pool is None:
            raise RuntimeError("No database nodes available")
        return await pool.execute(query, params)

    async def execute_write(self, query: str, params: tuple = ()) -> Any:
        if self._primary is None:
            raise RuntimeError("No primary database node configured")
        return await self._primary.execute(query, params)

    async def execute(self, query: str, params: tuple = (), role: QueryRole = QueryRole.READ) -> Any:
        if role == QueryRole.WRITE:
            return await self.execute_write(query, params)
        return await self.execute_read(query, params)
```

---

## Solution 2: Session-Level Read-Your-Writes Consistency

After a write, route subsequent reads to the primary for a configurable window to guarantee read-your-writes consistency.

```python
import contextvars
import time
from typing import Optional

# Per-async-task session state
_session_state: contextvars.ContextVar["SessionRouterState"] = contextvars.ContextVar("_session_state")

@dataclass
class SessionRouterState:
    last_write_at: float = 0.0
    primary_sticky_until: float = 0.0

class ReadYourWritesRouter(ReadWriteRouter):
    """
    Extends ReadWriteRouter with read-your-writes consistency.
    After a write, reads are routed to primary for `sticky_window_ms` milliseconds.
    """

    def __init__(self, sticky_window_ms: float = 500.0):
        super().__init__()
        self.sticky_window_ms = sticky_window_ms

    def _get_state(self) -> SessionRouterState:
        state = _session_state.get(None)
        if state is None:
            state = SessionRouterState()
            _session_state.set(state)
        return state

    async def execute_write(self, query: str, params: tuple = ()) -> Any:
        result = await super().execute_write(query, params)
        # Mark session as needing primary reads for sticky_window_ms
        state = self._get_state()
        state.last_write_at = time.monotonic()
        state.primary_sticky_until = time.monotonic() + self.sticky_window_ms / 1000
        return result

    async def execute_read(self, query: str, params: tuple = ()) -> Any:
        state = self._get_state()
        now = time.monotonic()

        if now < state.primary_sticky_until:
            # Within sticky window — read from primary for consistency
            return await self._primary.execute(query, params)

        return await super().execute_read(query, params)

    async def execute(self, query: str, params: tuple = (), role: QueryRole = QueryRole.READ) -> Any:
        if role == QueryRole.WRITE:
            return await self.execute_write(query, params)
        return await self.execute_read(query, params)


# --- Usage pattern ---

async def agent_update_and_read(router: ReadYourWritesRouter):
    # Write — session becomes primary-sticky
    await router.execute(
        "UPDATE users SET preferences = $1 WHERE id = $2",
        ('{"theme": "dark"}', 42),
        role=QueryRole.WRITE,
    )

    # Read immediately after write — goes to primary (consistent)
    result = await router.execute(
        "SELECT preferences FROM users WHERE id = $1",
        (42,),
        role=QueryRole.READ,
    )
    # After sticky window expires, reads return to replicas
```

---

## Solution 3: Query Classifier (Automatic READ/WRITE Detection)

Automatically classify SQL queries as read or write without requiring callers to specify the role.

```python
import re

class QueryClassifier:
    """
    Classifies SQL queries as READ or WRITE based on the statement type.
    Handles CTEs, subqueries, and common edge cases.
    """

    READ_STMTS  = {"SELECT", "SHOW", "EXPLAIN", "DESCRIBE", "WITH"}
    WRITE_STMTS = {"INSERT", "UPDATE", "DELETE", "REPLACE", "MERGE",
                   "CREATE", "DROP", "ALTER", "TRUNCATE", "CALL", "EXEC"}

    # Patterns that make a SELECT act as a write
    WRITE_PATTERNS = [
        re.compile(r"\bFOR\s+UPDATE\b",    re.IGNORECASE),
        re.compile(r"\bFOR\s+SHARE\b",     re.IGNORECASE),
        re.compile(r"\bLOCK\s+IN\s+SHARE", re.IGNORECASE),
        re.compile(r"\bSELECT\s+INTO\b",   re.IGNORECASE),
    ]

    @classmethod
    def classify(cls, query: str) -> QueryRole:
        """Classify a SQL query as READ or WRITE."""
        # Strip comments and leading whitespace
        q = re.sub(r"--[^\n]*", "", query)
        q = re.sub(r"/\*.*?\*/", "", q, flags=re.DOTALL)
        q = q.strip()

        # Extract first keyword (handling CTEs: WITH ... SELECT vs WITH ... INSERT)
        first_word = q.split()[0].upper() if q.split() else ""

        if first_word in cls.WRITE_STMTS:
            return QueryRole.WRITE

        if first_word in cls.READ_STMTS:
            # Check for locking reads that require primary
            for pattern in cls.WRITE_PATTERNS:
                if pattern.search(q):
                    return QueryRole.WRITE
            # CTE: check the final statement
            if first_word == "WITH":
                match = re.search(r'\)\s+(SELECT|INSERT|UPDATE|DELETE)', q, re.IGNORECASE)
                if match:
                    final = match.group(1).upper()
                    return QueryRole.WRITE if final in cls.WRITE_STMTS else QueryRole.READ
            return QueryRole.READ

        # Unknown — default to write (safe)
        return QueryRole.WRITE


class AutoRoutingDatabase:
    """
    Database wrapper that automatically classifies and routes queries.
    Callers never need to specify READ/WRITE explicitly.
    """

    def __init__(self, router: ReadYourWritesRouter):
        self.router = router
        self.classifier = QueryClassifier()
        self._stats = {"reads": 0, "writes": 0, "auto_routed": 0}

    async def execute(self, query: str, params: tuple = ()) -> Any:
        role = self.classifier.classify(query)
        self._stats["auto_routed"] += 1
        if role == QueryRole.READ:
            self._stats["reads"] += 1
        else:
            self._stats["writes"] += 1
        return await self.router.execute(query, params, role=role)

    def stats(self) -> dict:
        total = self._stats["auto_routed"]
        return {
            **self._stats,
            "read_ratio": self._stats["reads"] / total if total else 0,
        }
```

---

## Solution 4: Replica Lag Monitor

Continuously monitor replica lag and automatically remove lagging replicas from the read pool.

```python
import asyncio
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

@dataclass
class ReplicaHealth:
    node: DatabaseNode
    lag_ms: float = 0.0
    is_healthy: bool = True
    last_checked: float = 0.0
    consecutive_failures: int = 0

class ReplicaLagMonitor:
    """
    Polls replica lag and marks nodes unhealthy when lag exceeds threshold.
    Automatically re-admits recovered replicas to the read pool.
    """

    def __init__(
        self,
        router: ReadWriteRouter,
        check_interval: float = 5.0,
        max_lag_ms: float = 5_000.0,
        recovery_threshold: float = 500.0,
    ):
        self.router = router
        self.check_interval = check_interval
        self.max_lag_ms = max_lag_ms
        self.recovery_threshold = recovery_threshold
        self._health: dict[str, ReplicaHealth] = {}
        self._monitor_task: asyncio.Task | None = None

    def start(self) -> None:
        self._monitor_task = asyncio.create_task(self._monitor_loop())

    async def _monitor_loop(self) -> None:
        while True:
            try:
                await self._check_all_replicas()
            except Exception as exc:
                logger.error("Replica lag check error: %s", exc)
            await asyncio.sleep(self.check_interval)

    async def _check_all_replicas(self) -> None:
        for pool in self.router._replicas:
            node_key = f"{pool.node.host}:{pool.node.port}"
            health = self._health.setdefault(node_key, ReplicaHealth(pool.node))

            lag_ms = await self._measure_lag(pool)
            health.lag_ms = lag_ms
            health.last_checked = time.monotonic()

            was_healthy = health.is_healthy
            if lag_ms > self.max_lag_ms:
                health.is_healthy = False
                health.consecutive_failures += 1
                if was_healthy:
                    logger.warning("Replica %s marked unhealthy: lag=%.0fms", node_key, lag_ms)
                    pool.node.weight = 0.0  # Exclude from selection
            elif lag_ms <= self.recovery_threshold:
                if not was_healthy:
                    logger.info("Replica %s recovered: lag=%.0fms", node_key, lag_ms)
                health.is_healthy = True
                health.consecutive_failures = 0
                pool.node.weight = 1.0  # Re-admit to pool

    async def _measure_lag(self, pool: ConnectionPool) -> float:
        """Query replication lag from the replica."""
        try:
            start = time.monotonic()
            # In production: SELECT EXTRACT(EPOCH FROM (NOW() - pg_last_xact_replay_timestamp())) * 1000
            await pool.execute("SELECT 1")  # Placeholder
            return (time.monotonic() - start) * 1000
        except Exception:
            return float("inf")

    def health_report(self) -> dict:
        return {
            key: {
                "lag_ms": h.lag_ms,
                "healthy": h.is_healthy,
                "failures": h.consecutive_failures,
            }
            for key, h in self._health.items()
        }
```

---

## Solution 5: Connection Pool Manager with Per-Role Sizing

Maintain separate, appropriately sized connection pools for primary and replicas.

```python
from dataclasses import dataclass
import asyncio

@dataclass
class PoolConfig:
    min_size: int = 2
    max_size: int = 10
    max_idle_ms: float = 30_000.0
    acquire_timeout_ms: float = 5_000.0


class PerRoleConnectionPoolManager:
    """
    Manages separate connection pools for primary (writes) and replicas (reads).
    Primary pool is smaller and reserved for writes; replica pools are larger.
    """

    def __init__(
        self,
        primary_config: PoolConfig | None = None,
        replica_config: PoolConfig | None = None,
    ):
        self.primary_config = primary_config or PoolConfig(min_size=2, max_size=5)
        self.replica_config = replica_config or PoolConfig(min_size=3, max_size=20)
        self._pools: dict[str, asyncio.Semaphore] = {}

    def _pool_key(self, node: DatabaseNode) -> str:
        return f"{node.role.value}:{node.host}:{node.port}"

    def get_pool(self, node: DatabaseNode) -> asyncio.Semaphore:
        key = self._pool_key(node)
        if key not in self._pools:
            config = self.primary_config if node.role == QueryRole.WRITE else self.replica_config
            self._pools[key] = asyncio.Semaphore(config.max_size)
        return self._pools[key]

    async def acquire(self, node: DatabaseNode) -> "asyncio.Semaphore":
        pool = self.get_pool(node)
        try:
            await asyncio.wait_for(pool.acquire(), timeout=self.primary_config.acquire_timeout_ms / 1000)
            return pool
        except asyncio.TimeoutError:
            raise RuntimeError(f"Connection pool exhausted for {node.host}:{node.port}")

    def pool_stats(self) -> dict:
        return {
            key: {"available": sem._value}
            for key, sem in self._pools.items()
        }
```

---

## Solution 6: Integrated Agent Database Client

A high-level client combining automatic query routing, read-your-writes, lag monitoring, and per-role pools.

```python
class AgentDatabaseClient:
    """
    Production-ready database client for AI agents.
    Combines: auto-routing, read-your-writes, lag monitoring, connection pooling.
    """

    def __init__(
        self,
        primary_dsn: str,
        replica_dsns: list[str],
        sticky_window_ms: float = 500.0,
        max_replica_lag_ms: float = 5_000.0,
    ):
        self.router = ReadYourWritesRouter(sticky_window_ms=sticky_window_ms)
        self.classifier = QueryClassifier()
        self.pool_manager = PerRoleConnectionPoolManager()

        # Configure nodes
        primary_node = DatabaseNode(
            host=primary_dsn.split("@")[-1].split("/")[0],
            port=5432,
            role=QueryRole.WRITE,
        )
        self.router.set_primary(ConnectionPool(primary_node))

        for i, dsn in enumerate(replica_dsns):
            replica_node = DatabaseNode(
                host=dsn.split("@")[-1].split("/")[0],
                port=5432,
                role=QueryRole.READ,
                weight=1.0,
                max_lag_ms=max_replica_lag_ms,
            )
            self.router.add_replica(ConnectionPool(replica_node))

        self.lag_monitor = ReplicaLagMonitor(
            self.router, max_lag_ms=max_replica_lag_ms
        )
        self._query_stats = {"total": 0, "reads_to_replica": 0, "reads_to_primary": 0}

    async def start(self) -> None:
        self.lag_monitor.start()

    async def query(self, sql: str, params: tuple = ()) -> Any:
        """Execute query with automatic routing."""
        self._query_stats["total"] += 1
        role = self.classifier.classify(sql)
        result = await self.router.execute(sql, params, role=role)
        return result

    async def fetch_one(self, sql: str, params: tuple = ()) -> Optional[dict]:
        result = await self.query(sql, params)
        rows = result.get("rows", [])
        return rows[0] if rows else None

    async def fetch_all(self, sql: str, params: tuple = ()) -> list[dict]:
        result = await self.query(sql, params)
        return result.get("rows", [])

    def health(self) -> dict:
        return {
            "pool_stats": self.pool_manager.pool_stats(),
            "replica_health": self.lag_monitor.health_report(),
            "query_stats": self._query_stats,
        }
```

---

## Comparison

| Solution | Read/Write Split | Consistency | Auto-Routing | Lag Monitoring | Best For |
|---|---|---|---|---|---|
| Simple Read/Write Router | Yes | None | No | No | Baseline replica routing |
| Read-Your-Writes Router | Yes | Per-session | No | No | Consistent agent reads after writes |
| Query Classifier | Yes | Per-session | Yes | No | Zero-change-required routing |
| Replica Lag Monitor | Yes | Via exclusion | No | Yes | Avoiding stale reads |
| Per-Role Pool Manager | Yes | None | No | No | Connection pool isolation |
| Integrated Agent Client | Yes | Per-session | Yes | Yes | Production deployments |

**Start with the integrated agent client** for a complete solution. **The query classifier is the highest-leverage single addition** — it routes reads automatically without any code changes to existing queries. **Always configure read-your-writes** to prevent the frustrating bug where an agent writes a record then immediately reads back the old value from a lagging replica. **Monitor replica lag** and exclude lagging replicas rather than serving stale data silently.
