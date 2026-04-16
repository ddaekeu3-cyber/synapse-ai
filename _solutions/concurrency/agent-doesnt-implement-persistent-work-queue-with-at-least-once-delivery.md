---
title: "Agent Doesn't Implement Persistent Work Queue with At-Least-Once Delivery"
description: "AI agents that keep pending tasks in memory lose all in-flight work on crash or restart. A persistent work queue with at-least-once delivery guarantees every task is eventually processed, even after process failures, network partitions, or OOM kills."
date: 2025-01-31
difficulty: advanced
category: concurrency
slug: agent-doesnt-implement-persistent-work-queue-with-at-least-once-delivery
tags:
  - work-queue
  - at-least-once
  - durability
  - crash-recovery
  - messaging
  - reliability
  - idempotency
symptoms:
  - "Tasks disappear silently when the agent process is killed mid-execution"
  - "Restarting the agent after a crash starts with an empty task queue"
  - "Tool call results are lost if the response handler crashes before persisting"
  - "Users must manually resubmit failed tasks after incidents"
  - "No way to tell which tasks were processed vs still pending after a restart"
---

## Problem

In-memory task queues (`asyncio.Queue`, `collections.deque`) are ephemeral: a SIGKILL, OOM, or container restart wipes everything. Agents with long-running pipelines — multi-step reasoning, batch document processing, agentic workflows — need durability guarantees equivalent to those offered by message brokers.

At-least-once delivery means every accepted task is processed at least once, even if the worker dies mid-execution. The complementary property — exactly-once — requires idempotent handlers or distributed transactions that are far more expensive. For most agent workloads, at-least-once plus idempotency on the handler side is the right trade-off.

---

## Solution 1: SQLite-Backed Persistent Queue

Durable queue persisted to a local SQLite database. Tasks survive process restarts. Workers claim tasks with a visibility timeout: if not acknowledged within the window, the task is re-queued automatically.

```python
import asyncio
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass
class Task:
    task_id: str
    queue: str
    payload: Dict[str, Any]
    attempts: int
    created_at: float
    visible_at: float
    status: str   # pending | processing | done | failed


class SQLitePersistentQueue:
    """
    At-least-once work queue backed by SQLite.
    Tasks are invisible to other workers for `visibility_timeout` seconds
    after being claimed. If not ack'd in time, they become visible again.

    Usage:
        q = SQLitePersistentQueue("agent_tasks.db", visibility_timeout=30)
        q.setup()

        # Producer:
        q.enqueue("tool_calls", {"tool": "web_search", "query": "..."})

        # Worker:
        task = q.dequeue("tool_calls")
        if task:
            process(task)
            q.ack(task.task_id)
    """

    def __init__(self, db_path: str, visibility_timeout: float = 30.0,
                 max_attempts: int = 5):
        self._db_path = db_path
        self._vis_timeout = visibility_timeout
        self._max_attempts = max_attempts

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self._db_path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def setup(self):
        with self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id     TEXT PRIMARY KEY,
                    queue       TEXT NOT NULL,
                    payload     TEXT NOT NULL,
                    attempts    INTEGER DEFAULT 0,
                    created_at  REAL NOT NULL,
                    visible_at  REAL NOT NULL,
                    status      TEXT NOT NULL DEFAULT 'pending'
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_queue_visible ON tasks(queue, visible_at, status)")

    def enqueue(self, queue: str, payload: Dict[str, Any]) -> str:
        task_id = str(uuid.uuid4())
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO tasks(task_id,queue,payload,created_at,visible_at,status) VALUES(?,?,?,?,?,'pending')",
                (task_id, queue, json.dumps(payload), time.time(), time.time()),
            )
        return task_id

    def dequeue(self, queue: str) -> Optional[Task]:
        now = time.time()
        with self._conn() as conn:
            row = conn.execute(
                """SELECT task_id, payload, attempts, created_at
                   FROM tasks
                   WHERE queue=? AND status='pending' AND visible_at<=?
                   ORDER BY created_at LIMIT 1""",
                (queue, now),
            ).fetchone()
            if not row:
                return None
            task_id, payload_str, attempts, created_at = row
            conn.execute(
                "UPDATE tasks SET status='processing', attempts=attempts+1, visible_at=? WHERE task_id=?",
                (now + self._vis_timeout, task_id),
            )
        return Task(
            task_id=task_id, queue=queue,
            payload=json.loads(payload_str),
            attempts=attempts + 1,
            created_at=created_at,
            visible_at=now + self._vis_timeout,
            status="processing",
        )

    def ack(self, task_id: str):
        with self._conn() as conn:
            conn.execute(
                "UPDATE tasks SET status='done' WHERE task_id=?", (task_id,)
            )

    def nack(self, task_id: str, delay: float = 5.0):
        """Return task to queue after `delay` seconds."""
        with self._conn() as conn:
            conn.execute("""
                UPDATE tasks SET status='pending', visible_at=?
                WHERE task_id=? AND attempts < ?
            """, (time.time() + delay, task_id, self._max_attempts))
            conn.execute("""
                UPDATE tasks SET status='failed'
                WHERE task_id=? AND attempts >= ?
            """, (task_id, self._max_attempts))

    def requeue_stalled(self) -> int:
        """Re-enqueue tasks that were claimed but never ack'd."""
        with self._conn() as conn:
            cur = conn.execute("""
                UPDATE tasks SET status='pending', visible_at=?
                WHERE status='processing' AND visible_at < ?
            """, (time.time(), time.time()))
            return cur.rowcount

    def stats(self, queue: str) -> Dict[str, int]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) FROM tasks WHERE queue=? GROUP BY status",
                (queue,),
            ).fetchall()
        return {status: count for status, count in rows}
```

---

## Solution 2: Async Worker Pool Over Persistent Queue

Runs N concurrent workers against the SQLite queue. Each worker claims, processes, and acks tasks in a loop. Includes a requeue-stalled background job.

```python
import asyncio
import logging
from typing import Callable, Awaitable

logger = logging.getLogger(__name__)


class PersistentQueueWorkerPool:
    """
    N async workers draining a SQLitePersistentQueue.

    Usage:
        pool = PersistentQueueWorkerPool(queue, queue_name="tool_calls",
                                         handler=my_handler, concurrency=4)
        await pool.run()   # blocks; cancel to stop
    """

    def __init__(self, queue: SQLitePersistentQueue, queue_name: str,
                 handler: Callable[[Task], Awaitable[None]],
                 concurrency: int = 4, poll_interval: float = 0.5):
        self._q = queue
        self._queue_name = queue_name
        self._handler = handler
        self._concurrency = concurrency
        self._poll_interval = poll_interval

    async def run(self):
        workers = [
            asyncio.create_task(self._worker(i), name=f"worker-{i}")
            for i in range(self._concurrency)
        ]
        requeue_task = asyncio.create_task(self._requeue_loop(), name="requeue")
        await asyncio.gather(*workers, requeue_task)

    async def _worker(self, worker_id: int):
        while True:
            task = await asyncio.to_thread(self._q.dequeue, self._queue_name)
            if task is None:
                await asyncio.sleep(self._poll_interval)
                continue
            try:
                await self._handler(task)
                await asyncio.to_thread(self._q.ack, task.task_id)
                logger.debug("worker-%d acked %s", worker_id, task.task_id)
            except Exception as exc:
                logger.warning("worker-%d nacking %s: %s", worker_id, task.task_id, exc)
                await asyncio.to_thread(self._q.nack, task.task_id)

    async def _requeue_loop(self):
        while True:
            await asyncio.sleep(15.0)
            requeued = await asyncio.to_thread(self._q.requeue_stalled)
            if requeued:
                logger.info("Requeued %d stalled tasks", requeued)
```

---

## Solution 3: Redis Stream Queue (Distributed At-Least-Once)

Use Redis Streams with consumer groups for distributed at-least-once delivery across multiple agent hosts. Pending Entry List (PEL) tracks unacked messages and enables automatic redelivery.

```python
import asyncio
import json
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

try:
    import redis.asyncio as aioredis
    HAS_REDIS = True
except ImportError:
    HAS_REDIS = False


@dataclass
class StreamTask:
    message_id: str
    payload: Dict[str, Any]
    delivery_count: int


class RedisStreamQueue:
    """
    At-least-once queue backed by Redis Streams + consumer groups.

    Producer:
        q = RedisStreamQueue(redis_url, stream="agent:tasks", group="workers")
        await q.connect()
        await q.enqueue({"tool": "db_query", "sql": "..."})

    Consumer:
        tasks = await q.dequeue(consumer_id="worker-1", batch=10)
        for t in tasks:
            await process(t)
            await q.ack(t.message_id)

        # Claim stalled messages from crashed workers:
        await q.claim_stalled(consumer_id="worker-1", min_idle_ms=30000)
    """

    def __init__(self, redis_url: str, stream: str = "agent:tasks",
                 group: str = "workers", max_len: int = 100_000):
        if not HAS_REDIS:
            raise ImportError("redis[asyncio] required")
        self._redis_url = redis_url
        self._stream = stream
        self._group = group
        self._max_len = max_len
        self._redis = None

    async def connect(self):
        self._redis = await aioredis.from_url(self._redis_url)
        try:
            await self._redis.xgroup_create(
                self._stream, self._group, id="0", mkstream=True
            )
        except Exception as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def enqueue(self, payload: Dict[str, Any]) -> str:
        msg_id = await self._redis.xadd(
            self._stream,
            {"payload": json.dumps(payload)},
            maxlen=self._max_len,
            approximate=True,
        )
        return msg_id.decode()

    async def dequeue(self, consumer_id: str,
                      batch: int = 10, block_ms: int = 500) -> List[StreamTask]:
        results = await self._redis.xreadgroup(
            self._group, consumer_id,
            {self._stream: ">"},
            count=batch, block=block_ms,
        )
        tasks = []
        if results:
            for _, messages in results:
                for msg_id, fields in messages:
                    payload = json.loads(fields[b"payload"])
                    tasks.append(StreamTask(
                        message_id=msg_id.decode(),
                        payload=payload,
                        delivery_count=1,
                    ))
        return tasks

    async def ack(self, message_id: str):
        await self._redis.xack(self._stream, self._group, message_id)

    async def claim_stalled(self, consumer_id: str,
                             min_idle_ms: int = 30_000,
                             batch: int = 10) -> List[StreamTask]:
        """Claim messages sitting unacked in another consumer's PEL."""
        result = await self._redis.xautoclaim(
            self._stream, self._group, consumer_id,
            min_idle_time=min_idle_ms, start_id="0-0", count=batch,
        )
        tasks = []
        for msg_id, fields in result[1]:
            if fields:
                payload = json.loads(fields[b"payload"])
                tasks.append(StreamTask(
                    message_id=msg_id.decode(),
                    payload=payload,
                    delivery_count=2,
                ))
        return tasks
```

---

## Solution 4: Outbox Pattern for Tool-Call Side Effects

Write task + tool-call result atomically in the same database transaction using the outbox pattern. A relay process reads the outbox and delivers events, guaranteeing no lost results even if the agent crashes between saving the result and emitting the event.

```python
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional


@dataclass
class OutboxEvent:
    event_id: str
    aggregate_id: str
    event_type: str
    payload: Dict[str, Any]
    created_at: float
    delivered: bool


class OutboxStore:
    """
    Transactional outbox.  Both the domain update and the outbox insert
    happen in a single SQLite transaction.

    Usage:
        store = OutboxStore("agent.db")
        store.setup()

        # Atomically save tool result + schedule delivery event:
        with store.transaction() as tx:
            tx.save_result(task_id, result)
            tx.append_event("tool.result", task_id, {"result": result})

        # Relay loop delivers events exactly-once to downstream consumers:
        relay = OutboxRelay(store, deliver_fn=send_to_webhook)
        await relay.run()
    """

    def __init__(self, db_path: str):
        self._db_path = db_path

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self._db_path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def setup(self):
        with self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS task_results (
                    task_id TEXT PRIMARY KEY,
                    result  TEXT,
                    saved_at REAL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS outbox (
                    event_id     TEXT PRIMARY KEY,
                    aggregate_id TEXT,
                    event_type   TEXT,
                    payload      TEXT,
                    created_at   REAL,
                    delivered    INTEGER DEFAULT 0
                )
            """)

    @contextmanager
    def transaction(self):
        conn = sqlite3.connect(self._db_path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        tx = OutboxTransaction(conn)
        try:
            yield tx
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def undelivered_events(self, batch: int = 50) -> List[OutboxEvent]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT event_id, aggregate_id, event_type, payload, created_at "
                "FROM outbox WHERE delivered=0 ORDER BY created_at LIMIT ?",
                (batch,),
            ).fetchall()
        return [
            OutboxEvent(r[0], r[1], r[2], json.loads(r[3]), r[4], False)
            for r in rows
        ]

    def mark_delivered(self, event_id: str):
        with self._conn() as conn:
            conn.execute(
                "UPDATE outbox SET delivered=1 WHERE event_id=?", (event_id,)
            )


class OutboxTransaction:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def save_result(self, task_id: str, result: Any):
        self._conn.execute(
            "INSERT OR REPLACE INTO task_results VALUES(?,?,?)",
            (task_id, json.dumps(result), time.time()),
        )

    def append_event(self, event_type: str, aggregate_id: str,
                     payload: Dict[str, Any]):
        self._conn.execute(
            "INSERT INTO outbox(event_id,aggregate_id,event_type,payload,created_at) VALUES(?,?,?,?,?)",
            (str(uuid.uuid4()), aggregate_id, event_type,
             json.dumps(payload), time.time()),
        )


class OutboxRelay:
    def __init__(self, store: OutboxStore,
                 deliver_fn: Callable[[OutboxEvent], Any],
                 poll_interval: float = 1.0):
        self._store = store
        self._deliver = deliver_fn
        self._poll_interval = poll_interval

    async def run(self):
        import asyncio
        while True:
            events = await asyncio.to_thread(self._store.undelivered_events)
            for event in events:
                try:
                    await self._deliver(event)
                    await asyncio.to_thread(self._store.mark_delivered, event.event_id)
                except Exception:
                    pass  # retry on next poll
            await asyncio.sleep(self._poll_interval)
```

---

## Solution 5: Idempotency Key Registry

Every task carries an idempotency key. Workers check the registry before processing; duplicate deliveries (due to at-least-once redelivery) are detected and skipped automatically.

```python
import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Dict, Optional


class IdempotencyRegistry:
    """
    Tracks which idempotency keys have been successfully processed.
    Workers call `claim` before processing and `complete` on success.

    Usage:
        registry = IdempotencyRegistry("idempotency.db", ttl=86400)
        registry.setup()

        ikey = task.payload.get("idempotency_key") or task.task_id
        if registry.claim(ikey):
            result = process(task)
            registry.complete(ikey, result)
        else:
            result = registry.get_result(ikey)   # already done
    """

    def __init__(self, db_path: str, ttl: float = 86400.0):
        self._db_path = db_path
        self._ttl = ttl

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self._db_path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def setup(self):
        with self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS idempotency_keys (
                    ikey       TEXT PRIMARY KEY,
                    status     TEXT NOT NULL,   -- processing | complete
                    result     TEXT,
                    claimed_at REAL,
                    expires_at REAL
                )
            """)

    def claim(self, ikey: str) -> bool:
        """Returns True if this worker successfully claimed the key."""
        now = time.time()
        with self._conn() as conn:
            # Purge expired
            conn.execute("DELETE FROM idempotency_keys WHERE expires_at < ?", (now,))
            row = conn.execute(
                "SELECT status FROM idempotency_keys WHERE ikey=?", (ikey,)
            ).fetchone()
            if row:
                return False   # already claimed or complete
            try:
                conn.execute(
                    "INSERT INTO idempotency_keys(ikey,status,claimed_at,expires_at) VALUES(?,'processing',?,?)",
                    (ikey, now, now + self._ttl),
                )
                return True
            except sqlite3.IntegrityError:
                return False

    def complete(self, ikey: str, result: Any):
        with self._conn() as conn:
            conn.execute(
                "UPDATE idempotency_keys SET status='complete', result=? WHERE ikey=?",
                (json.dumps(result), ikey),
            )

    def get_result(self, ikey: str) -> Optional[Any]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT result FROM idempotency_keys WHERE ikey=? AND status='complete'",
                (ikey,),
            ).fetchone()
        return json.loads(row[0]) if row and row[0] else None
```

---

## Solution 6: Unified Durable Agent Task Runner

Combines the SQLite queue, worker pool, idempotency registry, and outbox into a single facade that agents use for all persistent task execution.

```python
import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Callable, Awaitable, Dict, Optional

logger = logging.getLogger(__name__)


@dataclass
class DurableTaskConfig:
    db_path: str = "agent_durable.db"
    queue_name: str = "agent_tasks"
    concurrency: int = 4
    visibility_timeout: float = 30.0
    max_attempts: int = 5
    idempotency_ttl: float = 86400.0


class DurableAgentTaskRunner:
    """
    Facade combining SQLitePersistentQueue + IdempotencyRegistry + OutboxStore.

    Usage:
        async def handle_task(task: Task):
            result = await run_tool(task.payload["tool"], task.payload["args"])
            return result

        runner = DurableAgentTaskRunner(DurableTaskConfig(), handler=handle_task)
        await runner.setup()
        runner.submit({"tool": "web_search", "args": {"q": "..."}, "idempotency_key": "req-123"})
        await runner.run()
    """

    def __init__(self, config: DurableTaskConfig,
                 handler: Callable[["Task"], Awaitable[Any]]):
        self._config = config
        self._handler = handler
        self._queue = SQLitePersistentQueue(
            config.db_path,
            visibility_timeout=config.visibility_timeout,
            max_attempts=config.max_attempts,
        )
        self._idem = IdempotencyRegistry(
            config.db_path + ".idem",
            ttl=config.idempotency_ttl,
        )
        self._outbox = OutboxStore(config.db_path + ".outbox")

    async def setup(self):
        await asyncio.to_thread(self._queue.setup)
        await asyncio.to_thread(self._idem.setup)
        await asyncio.to_thread(self._outbox.setup)

    def submit(self, payload: Dict[str, Any]) -> str:
        return self._queue.enqueue(self._config.queue_name, payload)

    async def run(self):
        pool = PersistentQueueWorkerPool(
            self._queue,
            queue_name=self._config.queue_name,
            handler=self._durable_handler,
            concurrency=self._config.concurrency,
        )
        await pool.run()

    async def _durable_handler(self, task: "Task"):
        ikey = task.payload.get("idempotency_key", task.task_id)
        claimed = await asyncio.to_thread(self._idem.claim, ikey)
        if not claimed:
            logger.debug("Skipping duplicate task %s (ikey=%s)", task.task_id, ikey)
            return
        try:
            result = await self._handler(task)
            await asyncio.to_thread(self._idem.complete, ikey, result)
            with self._outbox.transaction() as tx:
                tx.save_result(task.task_id, result)
                tx.append_event("task.completed", task.task_id, {"result": result})
        except Exception as exc:
            logger.error("Task %s failed: %s", task.task_id, exc)
            raise
```

---

## Comparison

| Approach | Durability | Distribution | Redelivery Mechanism |
|---|---|---|---|
| **SQLite Persistent Queue** | Local disk (WAL) | Single host | Visibility timeout |
| **Async Worker Pool** | Via SQLite | Single host | Requeue-stalled loop |
| **Redis Stream Queue** | Redis AOF/RDB | Multi-host | PEL + XAUTOCLAIM |
| **Outbox Pattern** | Transactional DB | Any DB backend | Relay poll loop |
| **Idempotency Registry** | Local SQLite | Per-host | Deduplication |
| **Unified Durable Runner** | SQLite + outbox | Single host | All of the above |

**Recommendation**: use the SQLite queue for single-host agents; Redis Streams when scaling to multiple workers across hosts. Always pair at-least-once delivery with idempotency checks in the handler — assume every task may be delivered twice.
