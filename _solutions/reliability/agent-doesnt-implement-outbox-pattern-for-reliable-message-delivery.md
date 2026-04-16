---
title: "Agent Doesn't Implement Outbox Pattern for Reliable Message Delivery"
description: "Without a transactional outbox, agents lose messages when a process crashes after persisting state but before publishing events, causing silent data loss and inconsistency."
difficulty: intermediate
category: reliability
tags: [outbox, messaging, at-least-once, transactional, sqlite, postgres, reliability]
---

## Problem

An agent saves state and then publishes a message to a queue or downstream service as two separate operations. If the process crashes between the write and the publish, the state is persisted but the message is never sent. Conversely, if the message is sent first and then the write fails, the consumer acts on data that doesn't exist. This dual-write problem causes silent data inconsistency that is difficult to detect and recover from.

```python
# Broken: state write and message publish are not atomic
async def complete_task(task_id: str, result: dict):
    await db.execute("UPDATE tasks SET status='done', result=? WHERE id=?",
                     [json.dumps(result), task_id])
    # CRASH HERE → message never sent, state says 'done', downstream never notified
    await message_queue.publish("task.completed", {"task_id": task_id, "result": result})
```

The outbox pattern solves this by writing both the state change and the outgoing message inside the **same database transaction**. A separate publisher process reads undelivered outbox entries and forwards them, retrying until acknowledged.

---

## Solution 1: Basic Outbox Table with Polling Publisher

```python
import asyncio
import json
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Callable, Awaitable

# Schema (run once at startup):
# CREATE TABLE outbox (
#   id TEXT PRIMARY KEY,
#   topic TEXT NOT NULL,
#   payload TEXT NOT NULL,
#   created_at REAL NOT NULL,
#   published_at REAL
# );

@dataclass
class OutboxEntry:
    id: str
    topic: str
    payload: dict
    created_at: float
    published_at: float | None

class TransactionalOutbox:
    def __init__(self, db_path: str):
        self.db_path = db_path

    def write_with_outbox(self, state_sql: str, state_params: list,
                          topic: str, payload: dict) -> None:
        """Write state change + outbox entry atomically (sync, called from async via run_in_executor)."""
        conn = sqlite3.connect(self.db_path)
        try:
            with conn:  # begins/commits transaction atomically
                conn.execute(state_sql, state_params)
                conn.execute(
                    "INSERT INTO outbox (id, topic, payload, created_at) VALUES (?, ?, ?, ?)",
                    [str(uuid.uuid4()), topic, json.dumps(payload), time.time()]
                )
        finally:
            conn.close()

    def claim_unpublished(self, limit: int = 10) -> list[OutboxEntry]:
        conn = sqlite3.connect(self.db_path)
        try:
            rows = conn.execute(
                "SELECT id, topic, payload, created_at, published_at "
                "FROM outbox WHERE published_at IS NULL ORDER BY created_at LIMIT ?",
                [limit]
            ).fetchall()
            return [OutboxEntry(r[0], r[1], json.loads(r[2]), r[3], r[4]) for r in rows]
        finally:
            conn.close()

    def mark_published(self, entry_id: str) -> None:
        conn = sqlite3.connect(self.db_path)
        try:
            with conn:
                conn.execute("UPDATE outbox SET published_at=? WHERE id=?",
                             [time.time(), entry_id])
        finally:
            conn.close()

class OutboxPublisher:
    def __init__(self, outbox: TransactionalOutbox,
                 publish_fn: Callable[[str, dict], Awaitable[None]],
                 poll_interval: float = 1.0):
        self.outbox = outbox
        self.publish_fn = publish_fn
        self.poll_interval = poll_interval
        self._running = False

    async def run(self):
        self._running = True
        while self._running:
            entries = await asyncio.get_event_loop().run_in_executor(
                None, self.outbox.claim_unpublished
            )
            for entry in entries:
                try:
                    await self.publish_fn(entry.topic, entry.payload)
                    await asyncio.get_event_loop().run_in_executor(
                        None, self.outbox.mark_published, entry.id
                    )
                except Exception as e:
                    print(f"Publish failed for {entry.id}: {e}, will retry")
            await asyncio.sleep(self.poll_interval)

    def stop(self):
        self._running = False

# Usage
async def demo():
    outbox = TransactionalOutbox("agent.db")

    async def publish(topic: str, payload: dict):
        print(f"[MQ] → {topic}: {payload}")

    publisher = OutboxPublisher(outbox, publish)
    asyncio.create_task(publisher.run())

    # Atomic: state + message in one transaction
    await asyncio.get_event_loop().run_in_executor(
        None,
        outbox.write_with_outbox,
        "UPDATE tasks SET status='done' WHERE id=?", ["task-1"],
        "task.completed", {"task_id": "task-1", "result": "ok"}
    )
```

---

## Solution 2: Async Outbox with aiosqlite and WAL Mode

```python
import aiosqlite
import asyncio
import json
import time
import uuid

class AsyncOutbox:
    def __init__(self, db_path: str):
        self.db_path = db_path

    async def initialize(self):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("""
                CREATE TABLE IF NOT EXISTS outbox (
                    id TEXT PRIMARY KEY,
                    topic TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    attempt_count INTEGER DEFAULT 0,
                    last_attempt_at REAL,
                    published_at REAL,
                    created_at REAL NOT NULL
                )
            """)
            await db.commit()

    async def transact(self, operations: list[tuple[str, list]],
                       messages: list[tuple[str, dict]]):
        """
        Atomically execute multiple SQL operations AND enqueue outbox messages.
        operations: [(sql, params), ...]
        messages: [(topic, payload), ...]
        """
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("BEGIN IMMEDIATE")
            try:
                for sql, params in operations:
                    await db.execute(sql, params)
                for topic, payload in messages:
                    await db.execute(
                        "INSERT INTO outbox (id, topic, payload, created_at) VALUES (?, ?, ?, ?)",
                        [str(uuid.uuid4()), topic, json.dumps(payload), time.time()]
                    )
                await db.commit()
            except Exception:
                await db.execute("ROLLBACK")
                raise

    async def fetch_pending(self, limit: int = 20,
                            retry_delay: float = 5.0) -> list[dict]:
        """Fetch entries that haven't been published and are past their retry delay."""
        cutoff = time.time() - retry_delay
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                """SELECT id, topic, payload, attempt_count FROM outbox
                   WHERE published_at IS NULL
                   AND (last_attempt_at IS NULL OR last_attempt_at < ?)
                   ORDER BY created_at LIMIT ?""",
                [cutoff, limit]
            ) as cursor:
                rows = await cursor.fetchall()
        return [{"id": r[0], "topic": r[1],
                 "payload": json.loads(r[2]), "attempts": r[3]} for r in rows]

    async def record_attempt(self, entry_id: str, success: bool):
        async with aiosqlite.connect(self.db_path) as db:
            if success:
                await db.execute(
                    "UPDATE outbox SET published_at=? WHERE id=?",
                    [time.time(), entry_id]
                )
            else:
                await db.execute(
                    "UPDATE outbox SET attempt_count=attempt_count+1, last_attempt_at=? WHERE id=?",
                    [time.time(), entry_id]
                )
            await db.commit()

# Example: agent completes a task atomically
async def agent_complete_task(outbox: AsyncOutbox, task_id: str, result: str):
    await outbox.transact(
        operations=[
            ("UPDATE tasks SET status='done', result=? WHERE id=?", [result, task_id])
        ],
        messages=[
            ("task.completed", {"task_id": task_id, "result": result}),
            ("audit.event", {"event": "task_complete", "task_id": task_id}),
        ]
    )
```

---

## Solution 3: Dead-Letter Queue and Max-Retry Outbox

```python
import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Awaitable

@dataclass
class OutboxMessage:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    topic: str = ""
    payload: dict = field(default_factory=dict)
    max_retries: int = 5
    attempt_count: int = 0
    next_retry_at: float = field(default_factory=time.time)
    created_at: float = field(default_factory=time.time)
    dead_lettered_at: float | None = None
    dead_letter_reason: str | None = None

class InMemoryOutbox:
    """In-memory outbox for single-process agents; swap for DB-backed in production."""

    def __init__(self):
        self._pending: dict[str, OutboxMessage] = {}
        self._dead_letter: list[OutboxMessage] = []
        self._lock = asyncio.Lock()

    async def enqueue(self, topic: str, payload: dict,
                      max_retries: int = 5) -> str:
        msg = OutboxMessage(topic=topic, payload=payload, max_retries=max_retries)
        async with self._lock:
            self._pending[msg.id] = msg
        return msg.id

    async def claim_ready(self, limit: int = 10) -> list[OutboxMessage]:
        now = time.time()
        async with self._lock:
            ready = [m for m in self._pending.values()
                     if m.next_retry_at <= now]
            return ready[:limit]

    async def ack(self, msg_id: str):
        async with self._lock:
            self._pending.pop(msg_id, None)

    async def nack(self, msg_id: str, error: str):
        async with self._lock:
            msg = self._pending.get(msg_id)
            if not msg:
                return
            msg.attempt_count += 1
            if msg.attempt_count >= msg.max_retries:
                msg.dead_lettered_at = time.time()
                msg.dead_letter_reason = error
                self._dead_letter.append(msg)
                del self._pending[msg_id]
                print(f"[DLQ] Message {msg_id} on topic '{msg.topic}' dead-lettered: {error}")
            else:
                # Exponential backoff: 1s, 2s, 4s, 8s, 16s
                backoff = 2 ** msg.attempt_count
                msg.next_retry_at = time.time() + backoff
                print(f"[Outbox] Retry {msg.attempt_count}/{msg.max_retries} "
                      f"for {msg_id} in {backoff}s")

    def dead_letter_count(self) -> int:
        return len(self._dead_letter)

    def drain_dead_letters(self) -> list[OutboxMessage]:
        dlq = list(self._dead_letter)
        self._dead_letter.clear()
        return dlq

class RetryingOutboxPublisher:
    def __init__(self, outbox: InMemoryOutbox,
                 publish_fn: Callable[[str, dict], Awaitable[None]],
                 poll_interval: float = 0.5):
        self.outbox = outbox
        self.publish_fn = publish_fn
        self.poll_interval = poll_interval

    async def run(self):
        while True:
            ready = await self.outbox.claim_ready()
            tasks = [self._deliver(msg) for msg in ready]
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.sleep(self.poll_interval)

    async def _deliver(self, msg: OutboxMessage):
        try:
            await self.publish_fn(msg.topic, msg.payload)
            await self.outbox.ack(msg.id)
        except Exception as e:
            await self.outbox.nack(msg.id, str(e))
```

---

## Solution 4: Idempotent Consumer with Deduplication Key

```python
import hashlib
import json
from typing import Callable, Awaitable

class IdempotentConsumer:
    """
    Tracks which outbox message IDs have already been processed.
    The producer includes a dedup key; the consumer skips already-seen keys.
    """

    def __init__(self, store_fn: Callable[[str], Awaitable[bool]],
                 mark_fn: Callable[[str], Awaitable[None]]):
        """
        store_fn(dedup_key) -> True if already processed
        mark_fn(dedup_key) -> mark as processed
        """
        self.store_fn = store_fn
        self.mark_fn = mark_fn

    async def process(self, message: dict,
                      handler: Callable[[dict], Awaitable[None]]) -> bool:
        """Returns True if handled, False if duplicate skipped."""
        dedup_key = message.get("outbox_id") or self._derive_key(message)
        if await self.store_fn(dedup_key):
            print(f"[Idempotent] Skipping duplicate: {dedup_key}")
            return False
        await handler(message)
        await self.mark_fn(dedup_key)
        return True

    def _derive_key(self, message: dict) -> str:
        canonical = json.dumps(message, sort_keys=True)
        return hashlib.sha256(canonical.encode()).hexdigest()

# In-memory deduplication store (use Redis/DB in production)
class InMemoryDeduplicationStore:
    def __init__(self, ttl: float = 3600):
        self._seen: dict[str, float] = {}
        self.ttl = ttl

    async def is_seen(self, key: str) -> bool:
        import time
        entry = self._seen.get(key)
        if entry and time.time() - entry < self.ttl:
            return True
        return False

    async def mark_seen(self, key: str):
        import time
        self._seen[key] = time.time()

# Outbox producer stamps every message with its ID
def stamp_outbox_message(topic: str, payload: dict, outbox_id: str) -> dict:
    return {"outbox_id": outbox_id, "topic": topic, **payload}

# Consumer usage
async def demo_consumer():
    store = InMemoryDeduplicationStore()
    consumer = IdempotentConsumer(store.is_seen, store.mark_seen)

    async def handle_task_completed(msg: dict):
        print(f"Processing task: {msg['task_id']}")

    msg = stamp_outbox_message("task.completed",
                               {"task_id": "t-1"}, "outbox-abc-123")
    await consumer.process(msg, handle_task_completed)  # handled
    await consumer.process(msg, handle_task_completed)  # skipped (duplicate)
```

---

## Solution 5: PostgreSQL-Backed Outbox with SKIP LOCKED

```python
import asyncio
import json
import time
import uuid
from typing import Callable, Awaitable

# Requires asyncpg: pip install asyncpg
# Schema:
# CREATE TABLE outbox (
#   id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
#   topic TEXT NOT NULL,
#   payload JSONB NOT NULL,
#   attempt_count INT DEFAULT 0,
#   scheduled_at TIMESTAMPTZ DEFAULT NOW(),
#   published_at TIMESTAMPTZ,
#   created_at TIMESTAMPTZ DEFAULT NOW()
# );

class PostgresOutbox:
    def __init__(self, pool):  # asyncpg Pool
        self.pool = pool

    async def write_with_state(self, state_sql: str, state_args: list,
                               topic: str, payload: dict) -> str:
        """Atomically write state + outbox entry in a single transaction."""
        msg_id = str(uuid.uuid4())
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(state_sql, *state_args)
                await conn.execute(
                    """INSERT INTO outbox (id, topic, payload)
                       VALUES ($1, $2, $3)""",
                    msg_id, topic, json.dumps(payload)
                )
        return msg_id

    async def claim_batch(self, batch_size: int = 20) -> list[dict]:
        """Use SKIP LOCKED for safe multi-publisher scenarios."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT id, topic, payload, attempt_count FROM outbox
                   WHERE published_at IS NULL AND scheduled_at <= NOW()
                   ORDER BY created_at
                   LIMIT $1
                   FOR UPDATE SKIP LOCKED""",
                batch_size
            )
        return [{"id": str(r["id"]), "topic": r["topic"],
                 "payload": json.loads(r["payload"]),
                 "attempts": r["attempt_count"]} for r in rows]

    async def ack(self, msg_id: str):
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE outbox SET published_at=NOW() WHERE id=$1", msg_id
            )

    async def nack(self, msg_id: str, max_retries: int = 5):
        """Exponential backoff schedule."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT attempt_count FROM outbox WHERE id=$1", msg_id
            )
            if not row:
                return
            attempt = row["attempt_count"] + 1
            if attempt >= max_retries:
                # Move to dead-letter by setting scheduled_at far in the future
                await conn.execute(
                    """UPDATE outbox
                       SET attempt_count=$1, scheduled_at=NOW() + INTERVAL '30 days'
                       WHERE id=$2""",
                    attempt, msg_id
                )
            else:
                delay_seconds = 2 ** attempt
                await conn.execute(
                    """UPDATE outbox
                       SET attempt_count=$1,
                           scheduled_at=NOW() + ($2 || ' seconds')::INTERVAL
                       WHERE id=$3""",
                    attempt, str(delay_seconds), msg_id
                )
```

---

## Solution 6: Outbox with Change Data Capture (CDC) Relay

```python
import asyncio
import json
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Awaitable

@dataclass
class CDCEvent:
    table: str
    operation: str  # INSERT, UPDATE, DELETE
    old_row: dict | None
    new_row: dict | None
    lsn: int  # log sequence number

class OutboxCDCRelay:
    """
    Simulates a CDC relay: watches the outbox table for INSERT events
    and forwards them. In production, use Debezium, pg_logical, or
    SQLite WAL reader.
    """

    def __init__(self, publish_fn: Callable[[str, dict], Awaitable[None]]):
        self.publish_fn = publish_fn
        self._event_queue: asyncio.Queue[CDCEvent] = asyncio.Queue()
        self._published_lsns: set[int] = set()

    def feed_cdc_event(self, event: CDCEvent):
        """Called by CDC connector when outbox table changes."""
        self._event_queue.put_nowait(event)

    async def relay_loop(self):
        """Continuously relay outbox INSERT events."""
        while True:
            event = await self._event_queue.get()
            if event.operation != "INSERT":
                continue
            if event.lsn in self._published_lsns:
                continue  # already relayed
            row = event.new_row
            if not row or row.get("published_at"):
                continue
            try:
                await self.publish_fn(row["topic"], json.loads(row["payload"]))
                self._published_lsns.add(event.lsn)
                print(f"[CDC Relay] Published LSN {event.lsn}: {row['topic']}")
            except Exception as e:
                print(f"[CDC Relay] Failed LSN {event.lsn}: {e}, will retry via polling")

class HybridOutboxPublisher:
    """
    Primary path: CDC relay (low latency, ~ms).
    Fallback path: polling (catches missed CDC events).
    """

    def __init__(self, cdc_relay: OutboxCDCRelay,
                 poll_outbox: Callable[[], Awaitable[list[dict]]],
                 publish_fn: Callable[[str, dict], Awaitable[None]],
                 ack_fn: Callable[[str], Awaitable[None]],
                 poll_interval: float = 10.0):
        self.cdc_relay = cdc_relay
        self.poll_outbox = poll_outbox
        self.publish_fn = publish_fn
        self.ack_fn = ack_fn
        self.poll_interval = poll_interval

    async def run(self):
        cdc_task = asyncio.create_task(self.cdc_relay.relay_loop())
        poll_task = asyncio.create_task(self._poll_loop())
        await asyncio.gather(cdc_task, poll_task)

    async def _poll_loop(self):
        """Catch-up sweep for any messages CDC missed."""
        while True:
            await asyncio.sleep(self.poll_interval)
            pending = await self.poll_outbox()
            for msg in pending:
                try:
                    await self.publish_fn(msg["topic"], msg["payload"])
                    await self.ack_fn(msg["id"])
                except Exception as e:
                    print(f"[Poll Fallback] Failed {msg['id']}: {e}")
```

---

## Comparison

| Solution | Atomicity | Delivery | Ordering | Complexity | Best For |
|---|---|---|---|---|---|
| 1. SQLite + polling | ✓ (transaction) | At-least-once | FIFO | Low | Single-process agents |
| 2. aiosqlite + WAL | ✓ | At-least-once | FIFO | Low | Async single-node |
| 3. In-memory + DLQ | ✓ (in-proc) | At-least-once | FIFO | Low-Med | Testing / ephemeral |
| 4. Idempotent consumer | N/A (consumer) | Exactly-once effect | Any | Med | Preventing double-process |
| 5. Postgres + SKIP LOCKED | ✓ | At-least-once | FIFO | Med-High | Multi-publisher, prod |
| 6. CDC relay | ✓ | At-least-once (dual) | LSN order | High | Low-latency + resilient |

**Key principle**: the outbox and the domain state must live in the **same database** and be written in the **same transaction**. The publisher is a separate concern that can fail and retry safely because outbox delivery is idempotent once consumers deduplicate by `outbox_id`.
