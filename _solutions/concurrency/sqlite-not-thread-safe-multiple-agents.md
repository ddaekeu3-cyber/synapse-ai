---
layout: solution
title: "SQLite Database Errors with Multiple Agent Workers — Not Thread-Safe"
category: concurrency
description: "Multiple agent workers share a SQLite database. Workers get 'database is locked', 'ProgrammingError: SQLite objects created in a thread can only be used in that same thread', or data corruption."
tags: [sqlite, thread-safety, concurrency, database, locking, workers]
---

# SQLite Database Errors with Multiple Agent Workers — Not Thread-Safe

## Symptom
- `sqlite3.OperationalError: database is locked`
- `ProgrammingError: SQLite objects created in a thread can only be used in that same thread`
- Data disappears or is partially written under concurrent load
- Works with one worker, fails with multiple
- Error rate increases with number of concurrent workers

## Root Cause
SQLite has a single-writer lock. Only one writer at a time, and the default lock timeout is very short. `sqlite3.Connection` objects are also not thread-safe — creating a connection in one thread and using it in another raises `ProgrammingError`. Many agent frameworks create one connection at startup and share it across workers.

## Fix

### Option 1: Create connection per thread/task (safest)

```python
import sqlite3
import threading

# WRONG — shared connection across threads
shared_conn = sqlite3.connect("agent.db")

def worker(task):
    shared_conn.execute("INSERT ...")  # ProgrammingError in another thread!

# RIGHT — create connection per thread using threading.local
_local = threading.local()

def get_connection() -> sqlite3.Connection:
    """Get thread-local SQLite connection"""
    if not hasattr(_local, "conn"):
        _local.conn = sqlite3.connect(
            "agent.db",
            timeout=30,  # Wait up to 30s for write lock
            check_same_thread=True  # Enforce thread safety
        )
        _local.conn.execute("PRAGMA journal_mode=WAL")  # Better concurrent reads
    return _local.conn

def worker(task):
    conn = get_connection()  # Gets THIS thread's connection
    conn.execute("INSERT INTO tasks VALUES (?)", (task["id"],))
    conn.commit()
```

### Option 2: Enable WAL mode for better concurrency

```python
import sqlite3

def create_optimized_db(path: str) -> sqlite3.Connection:
    """Configure SQLite for concurrent agent access"""
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False)

    # WAL mode allows concurrent readers + one writer
    conn.execute("PRAGMA journal_mode=WAL")
    # Increase busy timeout (wait longer before giving up on lock)
    conn.execute("PRAGMA busy_timeout=30000")  # 30 seconds
    # Synchronous mode balance (NORMAL = safer than OFF, faster than FULL)
    conn.execute("PRAGMA synchronous=NORMAL")

    conn.row_factory = sqlite3.Row  # Named columns
    return conn
```

### Option 3: Serialize writes through a queue

```python
import asyncio, sqlite3, queue, threading
from typing import Any

class SerializedSQLite:
    """Single-writer SQLite via write queue — thread-safe reads, serialized writes"""

    def __init__(self, path: str):
        self.path = path
        self._write_queue = queue.Queue()
        self._writer_thread = threading.Thread(target=self._write_loop, daemon=True)
        self._writer_thread.start()

    def _write_loop(self):
        conn = sqlite3.connect(self.path)
        conn.execute("PRAGMA journal_mode=WAL")
        while True:
            sql, params, result_future = self._write_queue.get()
            if sql is None:
                break
            try:
                cursor = conn.execute(sql, params or ())
                conn.commit()
                result_future.set_result(cursor.lastrowid)
            except Exception as e:
                result_future.set_exception(e)

    async def write(self, sql: str, params: tuple = ()) -> int:
        """Thread-safe write — serialized through queue"""
        loop = asyncio.get_event_loop()
        future = loop.create_future()
        self._write_queue.put((sql, params, future))
        return await future

    def read(self, sql: str, params: tuple = ()) -> list:
        """Thread-safe read — each call creates its own connection"""
        conn = sqlite3.connect(self.path)
        conn.execute("PRAGMA journal_mode=WAL")
        result = conn.execute(sql, params).fetchall()
        conn.close()
        return result

db = SerializedSQLite("agent.db")
```

### Option 4: Use SQLAlchemy with connection pool

```python
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

# For SQLite with multiple threads
engine = create_engine(
    "sqlite:///agent.db",
    connect_args={
        "timeout": 30,
        "check_same_thread": False
    },
    # StaticPool: single connection, thread-safe via lock
    poolclass=StaticPool,
    pool_pre_ping=True
)

# Better: use SQLite with NullPool for per-request connections
from sqlalchemy.pool import NullPool

engine = create_engine(
    "sqlite:///agent.db",
    connect_args={"timeout": 30},
    poolclass=NullPool  # New connection each time — fully thread-safe
)

from sqlalchemy.orm import sessionmaker

SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)

def get_db_session():
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
```

### Option 5: Migrate to PostgreSQL for production multi-agent use

```python
# SQLite is great for single-agent, development, and testing
# For multiple concurrent agents in production, use PostgreSQL

# asyncpg — async PostgreSQL client
import asyncpg, os

async def get_pg_pool() -> asyncpg.Pool:
    return await asyncpg.create_pool(
        dsn=os.environ["DATABASE_URL"],
        min_size=5,
        max_size=20  # Scale with number of workers
    )

pool = None

async def init():
    global pool
    pool = await get_pg_pool()

async def write_task_result(task_id: str, result: dict):
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO task_results (task_id, result) VALUES ($1, $2)",
            task_id, json.dumps(result)
        )
```

## SQLite Concurrency Limits

| Scenario | SQLite ok? | Notes |
|---|---|---|
| 1 agent, all operations | Yes | Default config fine |
| Multiple agents, read-heavy | Yes | WAL mode + per-thread connections |
| Multiple agents, write-heavy | Marginal | WAL + serialize writes |
| 10+ concurrent writers | No | Use PostgreSQL |
| Production multi-agent | No | Use PostgreSQL or MySQL |

## Expected Token Savings
Debugging SQLite locking errors: ~4,000 tokens
WAL mode + per-thread connections prevents most issues: 0 wasted

## Environment
- Multi-worker agent pipelines using SQLite as shared state
- Source: direct experience; SQLite's concurrency limitations are well-documented
