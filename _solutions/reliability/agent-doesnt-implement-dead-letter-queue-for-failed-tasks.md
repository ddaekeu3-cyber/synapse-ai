---
layout: solution
title: "Agent Doesn't Implement Dead-Letter Queue for Failed Tasks"
category: reliability
description: "When agent tasks fail permanently after exhausting retries, they are silently dropped. A dead-letter queue (DLQ) captures these failures for inspection, replay, and alerting without losing work."
tags: [reliability, dead-letter-queue, retry, error-handling, persistence, python]
---

## Problem

Agents that silently drop tasks after failure lose critical work and make debugging nearly impossible. Without a dead-letter queue, permanently failed tasks vanish — no audit trail, no replay capability, no alerting. Operators discover problems only when downstream systems notice missing results.

## Solutions

### Option 1: In-Memory DLQ with Structured Failure Records

```python
import anthropic
import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field, asdict
from collections import deque
from typing import Any, Callable, Optional

@dataclass
class TaskRecord:
    task_id: str
    payload: dict
    created_at: float
    attempt_count: int = 0
    max_attempts: int = 3
    last_error: Optional[str] = None
    last_attempt_at: Optional[float] = None
    dlq_enqueued_at: Optional[float] = None
    failure_reason: Optional[str] = None

class InMemoryDLQ:
    def __init__(self, max_size: int = 1000):
        self._queue: deque[TaskRecord] = deque(maxlen=max_size)
        self._stats = {"total_enqueued": 0, "total_replayed": 0}

    def enqueue(self, record: TaskRecord) -> None:
        record.dlq_enqueued_at = time.time()
        self._queue.append(record)
        self._stats["total_enqueued"] += 1
        print(f"[DLQ] Task {record.task_id} dead-lettered after "
              f"{record.attempt_count} attempts: {record.failure_reason}")

    def list_failed(self) -> list[TaskRecord]:
        return list(self._queue)

    def replay(self, task_id: str) -> Optional[TaskRecord]:
        for i, record in enumerate(self._queue):
            if record.task_id == task_id:
                record = self._queue[i]
                del self._queue[i]  # type: ignore[arg-type]
                record.attempt_count = 0
                record.dlq_enqueued_at = None
                self._stats["total_replayed"] += 1
                return record
        return None

    @property
    def stats(self) -> dict:
        return {**self._stats, "current_size": len(self._queue)}

class TaskProcessor:
    def __init__(self, dlq: InMemoryDLQ):
        self.client = anthropic.Anthropic()
        self.dlq = dlq

    def process(self, record: TaskRecord) -> str:
        record.attempt_count += 1
        record.last_attempt_at = time.time()

        try:
            response = self.client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=256,
                messages=[{"role": "user", "content": record.payload["prompt"]}],
            )
            return response.content[0].text
        except Exception as e:
            record.last_error = str(e)
            if record.attempt_count >= record.max_attempts:
                record.failure_reason = f"Exhausted {record.max_attempts} attempts. Last: {e}"
                self.dlq.enqueue(record)
                raise RuntimeError(f"Task {record.task_id} permanently failed") from e
            raise  # re-raise for retry

def run_demo():
    dlq = InMemoryDLQ()
    processor = TaskProcessor(dlq)

    tasks = [
        TaskRecord(task_id=str(uuid.uuid4()), payload={"prompt": "What is 2+2?"}, created_at=time.time()),
        TaskRecord(task_id="bad-task-1", payload={"prompt": ""}, created_at=time.time()),  # will fail
    ]

    for task in tasks:
        for attempt in range(task.max_attempts):
            try:
                result = processor.process(task)
                print(f"[OK] {task.task_id[:8]}: {result[:60]}")
                break
            except RuntimeError:
                break  # permanently failed, already in DLQ
            except Exception as e:
                print(f"[RETRY] {task.task_id[:8]} attempt {attempt+1}: {e}")
                time.sleep(0.5)

    print(f"\nDLQ stats: {dlq.stats}")
    for failed in dlq.list_failed():
        print(f"  Failed task {failed.task_id[:8]}: {failed.failure_reason}")

if __name__ == "__main__":
    run_demo()

# Expected Token Savings: N/A (reliability pattern, not token optimization)
# Environment: pip install anthropic
```

### Option 2: SQLite-Persisted DLQ with Replay and Expiry

```python
import anthropic
import sqlite3
import json
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Optional, Generator

@dataclass
class FailedTask:
    task_id: str
    payload_json: str
    attempt_count: int
    failure_reason: str
    dlq_enqueued_at: float
    expires_at: float  # auto-delete after TTL

class SQLiteDLQ:
    def __init__(self, db_path: str = "/tmp/agent_dlq.db", ttl_days: int = 7):
        self.db_path = db_path
        self.ttl_seconds = ttl_days * 86400
        self._init_db()

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS dead_letter_queue (
                    task_id TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL,
                    failure_reason TEXT NOT NULL,
                    dlq_enqueued_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    replayed_at REAL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_enqueued ON dead_letter_queue(dlq_enqueued_at)")

    @contextmanager
    def _conn(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def enqueue(self, task_id: str, payload: dict, attempt_count: int, reason: str) -> None:
        now = time.time()
        with self._conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO dead_letter_queue
                   (task_id, payload_json, attempt_count, failure_reason, dlq_enqueued_at, expires_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (task_id, json.dumps(payload), attempt_count, reason, now, now + self.ttl_seconds)
            )
        print(f"[DLQ] Persisted failed task {task_id[:8]}: {reason[:80]}")

    def list_pending(self, limit: int = 50) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT * FROM dead_letter_queue
                   WHERE expires_at > ? AND replayed_at IS NULL
                   ORDER BY dlq_enqueued_at DESC LIMIT ?""",
                (time.time(), limit)
            ).fetchall()
        return [dict(r) for r in rows]

    def mark_replayed(self, task_id: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE dead_letter_queue SET replayed_at = ? WHERE task_id = ?",
                (time.time(), task_id)
            )

    def purge_expired(self) -> int:
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM dead_letter_queue WHERE expires_at <= ?", (time.time(),))
            return cur.rowcount

    def stats(self) -> dict:
        with self._conn() as conn:
            total = conn.execute("SELECT COUNT(*) FROM dead_letter_queue").fetchone()[0]
            pending = conn.execute(
                "SELECT COUNT(*) FROM dead_letter_queue WHERE replayed_at IS NULL AND expires_at > ?",
                (time.time(),)
            ).fetchone()[0]
        return {"total": total, "pending_replay": pending}

def process_with_dlq(prompt: str, task_id: str, dlq: SQLiteDLQ,
                     max_attempts: int = 3) -> Optional[str]:
    client = anthropic.Anthropic()
    last_error = None

    for attempt in range(1, max_attempts + 1):
        try:
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=200,
                messages=[{"role": "user", "content": prompt}],
            )
            return response.content[0].text
        except anthropic.RateLimitError as e:
            last_error = e
            time.sleep(2 ** attempt)
        except anthropic.APIError as e:
            last_error = e
            break  # non-retryable

    # Permanently failed
    dlq.enqueue(
        task_id=task_id,
        payload={"prompt": prompt},
        attempt_count=attempt,
        reason=f"{type(last_error).__name__}: {last_error}"
    )
    return None

if __name__ == "__main__":
    dlq = SQLiteDLQ()
    tasks = [
        ("Summarize the water cycle in one sentence.", str(uuid.uuid4())),
        ("", str(uuid.uuid4())),  # empty prompt — likely to fail
    ]
    for prompt, tid in tasks:
        result = process_with_dlq(prompt, tid, dlq)
        if result:
            print(f"[OK] {result[:80]}")
    print(f"\nDLQ stats: {dlq.stats()}")
    for item in dlq.list_pending():
        print(f"  [{item['task_id'][:8]}] {item['failure_reason'][:70]}")
    dlq.purge_expired()

# Expected Token Savings: N/A (reliability pattern)
# Environment: pip install anthropic; sqlite3 is stdlib
```

### Option 3: Async DLQ with Priority Replay and Alerting

```python
import anthropic
import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Callable, Awaitable

class FailureSeverity(Enum):
    LOW = "low"        # Non-critical tasks
    MEDIUM = "medium"  # Business-impacting
    HIGH = "high"      # Revenue or safety critical

@dataclass
class DLQEntry:
    task_id: str
    payload: dict
    severity: FailureSeverity
    failure_reason: str
    attempt_count: int
    enqueued_at: float = field(default_factory=time.time)
    context: dict = field(default_factory=dict)

AlertHandler = Callable[[DLQEntry], Awaitable[None]]

class AsyncDLQ:
    def __init__(self):
        self._entries: list[DLQEntry] = []
        self._lock = asyncio.Lock()
        self._alert_handlers: list[AlertHandler] = []
        self._replay_queue: asyncio.PriorityQueue = asyncio.PriorityQueue()

    def add_alert_handler(self, handler: AlertHandler) -> None:
        self._alert_handlers.append(handler)

    async def enqueue(self, entry: DLQEntry) -> None:
        async with self._lock:
            self._entries.append(entry)

        # Fire alerts asynchronously
        await asyncio.gather(*[h(entry) for h in self._alert_handlers],
                             return_exceptions=True)

        # Queue for replay with priority (lower number = higher priority)
        priority = {"high": 0, "medium": 1, "low": 2}[entry.severity.value]
        await self._replay_queue.put((priority, entry.enqueued_at, entry))

    async def drain_for_replay(self, limit: int = 10) -> list[DLQEntry]:
        entries = []
        for _ in range(limit):
            if self._replay_queue.empty():
                break
            _, _, entry = await self._replay_queue.get()
            entries.append(entry)
        return entries

    async def stats(self) -> dict:
        async with self._lock:
            by_severity = {}
            for e in self._entries:
                by_severity[e.severity.value] = by_severity.get(e.severity.value, 0) + 1
            return {"total": len(self._entries), "by_severity": by_severity,
                    "replay_queue_size": self._replay_queue.qsize()}

async def alert_logger(entry: DLQEntry) -> None:
    severity_emoji = {"high": "🔴", "medium": "🟡", "low": "⚪"}
    emoji = severity_emoji[entry.severity.value]
    print(f"{emoji} [ALERT] {entry.severity.value.upper()} failure: "
          f"task={entry.task_id[:8]} reason={entry.failure_reason[:60]}")

async def process_task(client: anthropic.AsyncAnthropic, entry: DLQEntry,
                       dlq: AsyncDLQ, max_attempts: int = 3) -> Optional[str]:
    for attempt in range(1, max_attempts + 1):
        try:
            resp = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=150,
                messages=[{"role": "user", "content": entry.payload["prompt"]}],
            )
            return resp.content[0].text
        except anthropic.RateLimitError:
            await asyncio.sleep(2 ** attempt)
        except Exception as e:
            entry.failure_reason = f"{type(e).__name__}: {e}"
            entry.attempt_count = attempt
            await dlq.enqueue(entry)
            return None
    entry.failure_reason = f"Exhausted {max_attempts} attempts"
    entry.attempt_count = max_attempts
    await dlq.enqueue(entry)
    return None

async def main():
    client = anthropic.AsyncAnthropic()
    dlq = AsyncDLQ()
    dlq.add_alert_handler(alert_logger)

    tasks = [
        DLQEntry(str(uuid.uuid4()), {"prompt": "Name three planets."}, FailureSeverity.LOW, "", 0),
        DLQEntry(str(uuid.uuid4()), {"prompt": "Process payment for order 999."}, FailureSeverity.HIGH, "", 0),
    ]

    results = await asyncio.gather(*[process_task(client, t, dlq) for t in tasks])
    for task, result in zip(tasks, results):
        status = "OK" if result else "FAILED"
        print(f"[{status}] {task.task_id[:8]}: {(result or 'dead-lettered')[:60]}")

    print(f"\nDLQ stats: {await dlq.stats()}")

    # Replay high-priority failures
    for_replay = await dlq.drain_for_replay(limit=5)
    print(f"Replaying {len(for_replay)} entries (highest priority first)")
    for entry in for_replay:
        print(f"  [{entry.severity.value}] {entry.task_id[:8]}: {entry.failure_reason[:50]}")

if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: N/A (reliability pattern)
# Environment: pip install anthropic
```

### Option 4: DLQ with Exponential Backoff Replay Scheduler

```python
import anthropic
import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

@dataclass
class ScheduledReplay:
    task_id: str
    payload: dict
    failure_count: int  # Number of times this was dead-lettered
    next_retry_at: float
    backoff_seconds: float  # Current backoff interval

    def schedule_next(self, base: float = 60.0, max_backoff: float = 3600.0) -> None:
        self.failure_count += 1
        self.backoff_seconds = min(base * (2 ** self.failure_count), max_backoff)
        self.next_retry_at = time.time() + self.backoff_seconds

class BackoffReplayDLQ:
    def __init__(self, base_backoff: float = 5.0, max_backoff: float = 300.0):
        self._store: dict[str, ScheduledReplay] = {}
        self._base = base_backoff
        self._max = max_backoff
        self._lock = asyncio.Lock()

    async def enqueue(self, task_id: str, payload: dict) -> ScheduledReplay:
        async with self._lock:
            if task_id in self._store:
                entry = self._store[task_id]
                entry.schedule_next(self._base, self._max)
            else:
                backoff = self._base
                entry = ScheduledReplay(
                    task_id=task_id, payload=payload,
                    failure_count=1,
                    next_retry_at=time.time() + backoff,
                    backoff_seconds=backoff,
                )
                self._store[task_id] = entry
            print(f"[DLQ] {task_id[:8]} scheduled for replay in "
                  f"{entry.backoff_seconds:.0f}s (failure #{entry.failure_count})")
            return entry

    async def due_for_replay(self) -> list[ScheduledReplay]:
        now = time.time()
        async with self._lock:
            return [e for e in self._store.values() if e.next_retry_at <= now]

    async def remove(self, task_id: str) -> None:
        async with self._lock:
            self._store.pop(task_id, None)

    async def abandon(self, task_id: str, max_failures: int = 5) -> bool:
        async with self._lock:
            entry = self._store.get(task_id)
            if entry and entry.failure_count >= max_failures:
                print(f"[DLQ] Abandoning {task_id[:8]} after {entry.failure_count} failures")
                del self._store[task_id]
                return True
            return False

async def replay_worker(dlq: BackoffReplayDLQ, interval: float = 2.0):
    client = anthropic.AsyncAnthropic()
    print("[Replay worker] Started")
    for _ in range(5):  # bounded loop for demo
        await asyncio.sleep(interval)
        due = await dlq.due_for_replay()
        for entry in due:
            if await dlq.abandon(entry.task_id, max_failures=4):
                continue
            try:
                resp = await client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=100,
                    messages=[{"role": "user", "content": entry.payload["prompt"]}],
                )
                print(f"[Replay OK] {entry.task_id[:8]}: {resp.content[0].text[:60]}")
                await dlq.remove(entry.task_id)
            except Exception as e:
                print(f"[Replay FAIL] {entry.task_id[:8]}: {e}")
                await dlq.enqueue(entry.task_id, entry.payload)

async def main():
    dlq = BackoffReplayDLQ(base_backoff=1.0, max_backoff=30.0)

    # Simulate initial failures
    for i in range(3):
        tid = f"task-{i:04d}"
        await dlq.enqueue(tid, {"prompt": f"Describe item {i} briefly."})

    await replay_worker(dlq, interval=1.5)

if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: N/A (reliability pattern)
# Environment: pip install anthropic
```

### Option 5: DLQ with Failure Classification and Routing

```python
import anthropic
import asyncio
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Optional

class FailureClass(Enum):
    TRANSIENT = "transient"       # Retry-able: network, rate limit
    PERMANENT = "permanent"       # Non-retry: invalid input, auth failure
    DEGRADED = "degraded"         # Partial: timeout, incomplete response
    UNKNOWN = "unknown"           # Unclassified

def classify_failure(error: Exception) -> FailureClass:
    if isinstance(error, anthropic.RateLimitError):
        return FailureClass.TRANSIENT
    if isinstance(error, anthropic.AuthenticationError):
        return FailureClass.PERMANENT
    if isinstance(error, anthropic.BadRequestError):
        return FailureClass.PERMANENT
    if isinstance(error, (asyncio.TimeoutError, TimeoutError)):
        return FailureClass.DEGRADED
    if isinstance(error, anthropic.APIConnectionError):
        return FailureClass.TRANSIENT
    return FailureClass.UNKNOWN

@dataclass
class ClassifiedFailure:
    task_id: str
    payload: dict
    failure_class: FailureClass
    error_message: str
    attempt_count: int
    classified_at: float

class ClassifyingDLQ:
    def __init__(self):
        self._buckets: dict[FailureClass, list[ClassifiedFailure]] = {
            fc: [] for fc in FailureClass
        }

    def enqueue(self, task_id: str, payload: dict, error: Exception, attempts: int) -> ClassifiedFailure:
        fc = classify_failure(error)
        entry = ClassifiedFailure(
            task_id=task_id, payload=payload,
            failure_class=fc, error_message=str(error),
            attempt_count=attempts, classified_at=time.time()
        )
        self._buckets[fc].append(entry)
        print(f"[DLQ:{fc.value}] {task_id[:8]} after {attempts} attempts: {str(error)[:50]}")
        return entry

    def get_retryable(self) -> list[ClassifiedFailure]:
        return self._buckets[FailureClass.TRANSIENT] + self._buckets[FailureClass.DEGRADED]

    def get_permanent(self) -> list[ClassifiedFailure]:
        return self._buckets[FailureClass.PERMANENT]

    def summary(self) -> dict:
        return {fc.value: len(entries) for fc, entries in self._buckets.items()}

async def execute_with_dlq(client: anthropic.AsyncAnthropic, task_id: str,
                           payload: dict, dlq: ClassifyingDLQ,
                           max_attempts: int = 3) -> Optional[str]:
    last_error: Optional[Exception] = None
    for attempt in range(1, max_attempts + 1):
        try:
            async with asyncio.timeout(10.0):
                resp = await client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=100,
                    messages=[{"role": "user", "content": payload["prompt"]}],
                )
            return resp.content[0].text
        except anthropic.RateLimitError as e:
            last_error = e
            await asyncio.sleep(2 ** attempt)
        except Exception as e:
            last_error = e
            fc = classify_failure(e)
            if fc == FailureClass.PERMANENT:
                break  # no point retrying

    dlq.enqueue(task_id, payload, last_error or Exception("unknown"), attempt)
    return None

async def main():
    client = anthropic.AsyncAnthropic()
    dlq = ClassifyingDLQ()

    tasks = [
        ("task-ok", {"prompt": "What color is the sky?"}),
        ("task-empty", {"prompt": ""}),
        ("task-normal", {"prompt": "List two fruits."}),
    ]

    results = await asyncio.gather(*[
        execute_with_dlq(client, tid, payload, dlq)
        for tid, payload in tasks
    ])

    for (tid, _), result in zip(tasks, results):
        print(f"[{'OK' if result else 'DLQ'}] {tid}: {(result or 'dead-lettered')[:60]}")

    print(f"\nDLQ summary: {dlq.summary()}")
    retryable = dlq.get_retryable()
    permanent = dlq.get_permanent()
    print(f"Retryable: {len(retryable)}, Permanent: {len(permanent)}")

if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: N/A (reliability pattern)
# Environment: pip install anthropic
```

### Option 6: DLQ with Webhook Notification and Batch Replay

```python
import anthropic
import asyncio
import json
import time
import uuid
import hmac
import hashlib
from dataclasses import dataclass, field, asdict
from typing import Optional

@dataclass
class DLQItem:
    task_id: str
    payload: dict
    failure_reason: str
    attempt_count: int
    tenant_id: str
    enqueued_at: float = field(default_factory=time.time)
    tags: list[str] = field(default_factory=list)

class WebhookDLQ:
    def __init__(self, webhook_url: Optional[str] = None, webhook_secret: str = "secret"):
        self._items: dict[str, DLQItem] = {}
        self._webhook_url = webhook_url
        self._secret = webhook_secret.encode()

    def _sign_payload(self, body: str) -> str:
        return hmac.new(self._secret, body.encode(), hashlib.sha256).hexdigest()

    async def _notify(self, item: DLQItem) -> None:
        if not self._webhook_url:
            payload = json.dumps({"event": "task.dead_lettered", "task_id": item.task_id,
                                  "tenant": item.tenant_id, "reason": item.failure_reason})
            sig = self._sign_payload(payload)
            print(f"[WEBHOOK] Would POST to {self._webhook_url or 'none'}: "
                  f"sig={sig[:16]}... payload={payload[:80]}")
            return
        # Real implementation would use aiohttp/httpx here

    async def enqueue(self, item: DLQItem) -> None:
        self._items[item.task_id] = item
        await self._notify(item)

    async def batch_replay(self, tenant_id: Optional[str] = None,
                           tag_filter: Optional[str] = None) -> list[DLQItem]:
        candidates = [
            item for item in self._items.values()
            if (tenant_id is None or item.tenant_id == tenant_id)
            and (tag_filter is None or tag_filter in item.tags)
        ]
        # Remove from DLQ before replaying
        for item in candidates:
            del self._items[item.task_id]
        return candidates

    def stats(self) -> dict:
        by_tenant: dict[str, int] = {}
        for item in self._items.values():
            by_tenant[item.tenant_id] = by_tenant.get(item.tenant_id, 0) + 1
        return {"total": len(self._items), "by_tenant": by_tenant}

async def run_agent_with_dlq(client: anthropic.AsyncAnthropic,
                              task_id: str, payload: dict,
                              tenant_id: str, dlq: WebhookDLQ,
                              max_attempts: int = 2) -> Optional[str]:
    last_err = None
    for attempt in range(1, max_attempts + 1):
        try:
            resp = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=80,
                messages=[{"role": "user", "content": payload["prompt"]}],
            )
            return resp.content[0].text
        except Exception as e:
            last_err = e
            await asyncio.sleep(0.5)

    item = DLQItem(
        task_id=task_id, payload=payload,
        failure_reason=f"{type(last_err).__name__}: {last_err}",
        attempt_count=max_attempts, tenant_id=tenant_id,
        tags=payload.get("tags", []),
    )
    await dlq.enqueue(item)
    return None

async def main():
    client = anthropic.AsyncAnthropic()
    dlq = WebhookDLQ(webhook_secret="supersecret")

    task_specs = [
        ("t1", {"prompt": "Define entropy.", "tags": ["science"]}, "tenant-A"),
        ("t2", {"prompt": "Explain gravity.", "tags": ["science"]}, "tenant-A"),
        ("t3", {"prompt": "List colors.", "tags": ["art"]}, "tenant-B"),
    ]

    results = await asyncio.gather(*[
        run_agent_with_dlq(client, tid, payload, tenant, dlq)
        for tid, payload, tenant in task_specs
    ])

    for (tid, _, _), result in zip(task_specs, results):
        print(f"[{'OK' if result else 'DLQ'}] {tid}: {(result or 'queued for DLQ')[:60]}")

    print(f"\nDLQ stats: {dlq.stats()}")

    # Batch replay all tenant-A science tasks
    for_replay = await dlq.batch_replay(tenant_id="tenant-A", tag_filter="science")
    print(f"Replaying {len(for_replay)} tasks for tenant-A/science")

if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: N/A (reliability pattern)
# Environment: pip install anthropic
```

## Comparison

| Option | Storage | Replay | Alerting | Best For |
|--------|---------|--------|----------|----------|
| 1. In-Memory | RAM | Manual by ID | Console log | Dev/test |
| 2. SQLite | Disk + TTL | Query-based | None | Single-node prod |
| 3. Async + Priority | RAM | Priority queue | Async handlers | Low-latency |
| 4. Backoff Scheduler | RAM | Time-scheduled | Console | Retry storms |
| 5. Failure Classifier | RAM buckets | By class | None | Debugging |
| 6. Webhook + Batch | RAM | Tenant/tag filter | Signed webhooks | Multi-tenant |
