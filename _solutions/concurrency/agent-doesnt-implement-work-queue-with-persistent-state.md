---
layout: solution
title: "Agent Doesn't Implement Work Queue with Persistent State"
category: concurrency
description: "AI agents that hold pending tasks in memory lose all work on restart or crash. A persistent work queue with SQLite state survives restarts, prevents duplicate execution, and enables reliable task scheduling."
tags: [concurrency, work-queue, persistence, sqlite, retry, reliability, async]
---

# Agent Doesn't Implement Work Queue with Persistent State

## Problem

In-memory queues are invisible to restarts. When an agent crashes mid-task or restarts for a deployment, all pending work vanishes. Tasks get lost, users receive no response, and there's no audit trail of what ran or failed.

A persistent work queue stores task state in SQLite so tasks survive restarts, execute exactly once, and are retried on transient failures.

---

## Option 1: Simple SQLite-Backed FIFO Queue

```python
import sqlite3
import json
import uuid
import asyncio
import anthropic
from datetime import datetime

class PersistentQueue:
    """Simple FIFO work queue backed by SQLite."""

    def __init__(self, db_path: str = "work_queue.db"):
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self._init_schema()

    def _init_schema(self):
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                started_at TEXT,
                completed_at TEXT,
                result TEXT,
                error TEXT
            )
        """)
        self.conn.commit()

    def enqueue(self, payload: dict) -> str:
        task_id = str(uuid.uuid4())[:12]
        self.conn.execute(
            "INSERT INTO tasks (task_id, payload, created_at) VALUES (?,?,?)",
            (task_id, json.dumps(payload), datetime.utcnow().isoformat()),
        )
        self.conn.commit()
        return task_id

    def dequeue(self) -> dict | None:
        row = self.conn.execute(
            "SELECT task_id, payload FROM tasks WHERE status='pending' ORDER BY created_at LIMIT 1"
        ).fetchone()
        if not row:
            return None
        task_id, payload = row
        self.conn.execute(
            "UPDATE tasks SET status='running', started_at=? WHERE task_id=?",
            (datetime.utcnow().isoformat(), task_id),
        )
        self.conn.commit()
        return {"task_id": task_id, "payload": json.loads(payload)}

    def complete(self, task_id: str, result: str):
        self.conn.execute(
            "UPDATE tasks SET status='done', completed_at=?, result=? WHERE task_id=?",
            (datetime.utcnow().isoformat(), result, task_id),
        )
        self.conn.commit()

    def fail(self, task_id: str, error: str):
        self.conn.execute(
            "UPDATE tasks SET status='failed', error=? WHERE task_id=?",
            (error, task_id),
        )
        self.conn.commit()

    def stats(self) -> dict:
        rows = self.conn.execute(
            "SELECT status, COUNT(*) FROM tasks GROUP BY status"
        ).fetchall()
        return {r[0]: r[1] for r in rows}


def process_task(task: dict) -> str:
    client = anthropic.Anthropic()
    prompt = task["payload"].get("prompt", "Say hello.")
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


def run_worker(queue: PersistentQueue, max_tasks: int = 5):
    processed = 0
    while processed < max_tasks:
        task = queue.dequeue()
        if not task:
            break
        print(f"[Worker] Processing task {task['task_id']}")
        try:
            result = process_task(task)
            queue.complete(task["task_id"], result)
            print(f"[Worker] Done: {result[:60]}")
        except Exception as e:
            queue.fail(task["task_id"], str(e))
            print(f"[Worker] Failed: {e}")
        processed += 1

    print(f"\nQueue stats: {queue.stats()}")


if __name__ == "__main__":
    q = PersistentQueue(db_path=":memory:")

    # Enqueue tasks (survives restart because they're in SQLite)
    for prompt in [
        "What is 2+2?",
        "Name a programming language.",
        "What color is the sky?",
    ]:
        task_id = q.enqueue({"prompt": prompt})
        print(f"Enqueued {task_id}: {prompt}")

    print()
    run_worker(q)
# Expected Token Savings: 0% direct — persistence prevents re-work on crash, saving redundant API calls
# Environment: pip install anthropic; sqlite3, json, uuid are stdlib
```

---

## Option 2: Retry-Aware Queue with Backoff

```python
import sqlite3
import json
import uuid
import time
import anthropic
from datetime import datetime, timedelta
from dataclasses import dataclass

@dataclass
class RetryPolicy:
    max_attempts: int = 3
    base_delay_sec: float = 1.0
    backoff_factor: float = 2.0

class RetryQueue:
    """Persistent queue with automatic retry and exponential backoff."""

    def __init__(self, db_path: str = ":memory:", policy: RetryPolicy | None = None):
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.policy = policy or RetryPolicy()
        self._init_schema()

    def _init_schema(self):
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                last_error TEXT
            )
        """)
        self.conn.commit()

    def enqueue(self, payload: dict) -> str:
        task_id = str(uuid.uuid4())[:10]
        now = datetime.utcnow().isoformat()
        self.conn.execute(
            "INSERT INTO tasks (task_id, payload, next_attempt_at, created_at) VALUES (?,?,?,?)",
            (task_id, json.dumps(payload), now, now),
        )
        self.conn.commit()
        return task_id

    def dequeue_ready(self) -> dict | None:
        now = datetime.utcnow().isoformat()
        row = self.conn.execute(
            """SELECT task_id, payload, attempts FROM tasks
               WHERE status='pending' AND next_attempt_at <= ?
               ORDER BY next_attempt_at LIMIT 1""",
            (now,),
        ).fetchone()
        if not row:
            return None
        task_id, payload, attempts = row
        self.conn.execute(
            "UPDATE tasks SET status='running', attempts=attempts+1 WHERE task_id=?",
            (task_id,),
        )
        self.conn.commit()
        return {"task_id": task_id, "payload": json.loads(payload), "attempts": attempts + 1}

    def complete(self, task_id: str):
        self.conn.execute(
            "UPDATE tasks SET status='done' WHERE task_id=?", (task_id,)
        )
        self.conn.commit()

    def retry_or_fail(self, task_id: str, error: str):
        row = self.conn.execute(
            "SELECT attempts FROM tasks WHERE task_id=?", (task_id,)
        ).fetchone()
        attempts = row[0]

        if attempts >= self.policy.max_attempts:
            self.conn.execute(
                "UPDATE tasks SET status='dead_letter', last_error=? WHERE task_id=?",
                (error, task_id),
            )
            print(f"[Queue] Task {task_id} moved to dead letter after {attempts} attempts")
        else:
            delay = self.policy.base_delay_sec * (self.policy.backoff_factor ** (attempts - 1))
            next_at = (datetime.utcnow() + timedelta(seconds=delay)).isoformat()
            self.conn.execute(
                "UPDATE tasks SET status='pending', last_error=?, next_attempt_at=? WHERE task_id=?",
                (error, next_at, task_id),
            )
            print(f"[Queue] Task {task_id} scheduled retry in {delay:.1f}s (attempt {attempts}/{self.policy.max_attempts})")

        self.conn.commit()

    def stats(self) -> dict:
        rows = self.conn.execute(
            "SELECT status, COUNT(*), AVG(attempts) FROM tasks GROUP BY status"
        ).fetchall()
        return {r[0]: {"count": r[1], "avg_attempts": round(r[2] or 0, 1)} for r in rows}


def run_retry_worker(queue: RetryQueue, rounds: int = 3):
    client = anthropic.Anthropic()

    for round_n in range(rounds):
        print(f"\n--- Round {round_n + 1} ---")
        task = queue.dequeue_ready()
        if not task:
            print("No ready tasks.")
            continue

        print(f"[Worker] Task {task['task_id']} (attempt {task['attempts']})")
        try:
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=64,
                messages=[{"role": "user", "content": task["payload"]["prompt"]}],
            )
            queue.complete(task["task_id"])
            print(f"[Worker] Completed: {response.content[0].text[:50]}")
        except Exception as e:
            queue.retry_or_fail(task["task_id"], str(e))

    print(f"\nFinal stats: {json.dumps(queue.stats(), indent=2)}")


if __name__ == "__main__":
    q = RetryQueue(policy=RetryPolicy(max_attempts=3, base_delay_sec=0.1))
    q.enqueue({"prompt": "What is the capital of France?"})
    q.enqueue({"prompt": "Name a fruit."})
    run_retry_worker(q)
# Expected Token Savings: Prevents duplicate LLM calls on retried tasks by tracking attempts
# Environment: pip install anthropic; sqlite3, json, uuid, time are stdlib
```

---

## Option 3: Async Worker Pool with Persistent Queue

```python
import asyncio
import sqlite3
import json
import uuid
import anthropic
from datetime import datetime
from dataclasses import dataclass

@dataclass
class WorkerConfig:
    concurrency: int = 3
    poll_interval_sec: float = 0.1

class AsyncPersistentQueue:
    def __init__(self, db_path: str = ":memory:"):
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self._lock = asyncio.Lock()
        self._init_schema()

    def _init_schema(self):
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY,
                payload TEXT,
                status TEXT DEFAULT 'pending',
                worker_id TEXT,
                created_at TEXT,
                completed_at TEXT,
                result TEXT
            )
        """)
        self.conn.commit()

    async def enqueue(self, payload: dict) -> str:
        task_id = str(uuid.uuid4())[:10]
        async with self._lock:
            self.conn.execute(
                "INSERT INTO tasks (task_id, payload, created_at) VALUES (?,?,?)",
                (task_id, json.dumps(payload), datetime.utcnow().isoformat()),
            )
            self.conn.commit()
        return task_id

    async def claim(self, worker_id: str) -> dict | None:
        async with self._lock:
            row = self.conn.execute(
                "SELECT task_id, payload FROM tasks WHERE status='pending' ORDER BY created_at LIMIT 1"
            ).fetchone()
            if not row:
                return None
            task_id, payload = row
            self.conn.execute(
                "UPDATE tasks SET status='running', worker_id=? WHERE task_id=?",
                (worker_id, task_id),
            )
            self.conn.commit()
            return {"task_id": task_id, "payload": json.loads(payload)}

    async def complete(self, task_id: str, result: str):
        async with self._lock:
            self.conn.execute(
                "UPDATE tasks SET status='done', completed_at=?, result=? WHERE task_id=?",
                (datetime.utcnow().isoformat(), result[:200], task_id),
            )
            self.conn.commit()

    def all_done(self) -> bool:
        row = self.conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE status IN ('pending','running')"
        ).fetchone()
        return row[0] == 0

    def summary(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT task_id, status, result FROM tasks ORDER BY created_at"
        ).fetchall()
        return [{"id": r[0], "status": r[1], "result": (r[2] or "")[:60]} for r in rows]


async def worker(worker_id: str, queue: AsyncPersistentQueue, config: WorkerConfig):
    client = anthropic.AsyncAnthropic()
    while True:
        task = await queue.claim(worker_id)
        if not task:
            await asyncio.sleep(config.poll_interval_sec)
            if queue.all_done():
                return
            continue

        print(f"[{worker_id}] Processing {task['task_id']}")
        try:
            response = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=64,
                messages=[{"role": "user", "content": task["payload"]["prompt"]}],
            )
            result = response.content[0].text
            await queue.complete(task["task_id"], result)
            print(f"[{worker_id}] Done {task['task_id']}: {result[:40]}")
        except Exception as e:
            await queue.complete(task["task_id"], f"ERROR: {e}")


async def run_pool(prompts: list[str], config: WorkerConfig | None = None):
    config = config or WorkerConfig(concurrency=3)
    queue = AsyncPersistentQueue()

    for p in prompts:
        await queue.enqueue({"prompt": p})

    workers = [
        asyncio.create_task(worker(f"w{i}", queue, config))
        for i in range(config.concurrency)
    ]

    await asyncio.gather(*workers)

    print("\nResults:")
    for item in queue.summary():
        print(f"  [{item['id']}] {item['status']}: {item['result']}")


if __name__ == "__main__":
    prompts = [
        "What is Python?",
        "Name a color.",
        "What is 5*5?",
        "Say 'hello'.",
        "Name a country.",
    ]
    asyncio.run(run_pool(prompts))
# Expected Token Savings: None — pool parallelism reduces wall time, not token count
# Environment: pip install anthropic; asyncio, sqlite3 are stdlib
```

---

## Option 4: Priority Queue with Task Dependencies

```python
import sqlite3
import json
import uuid
import anthropic
from datetime import datetime
from enum import IntEnum

class Priority(IntEnum):
    CRITICAL = 0
    HIGH = 1
    NORMAL = 2
    LOW = 3
    BACKGROUND = 4

class DependencyQueue:
    """
    Persistent priority queue with task dependency tracking.
    A task only becomes 'ready' once all its dependencies complete.
    """

    def __init__(self, db_path: str = ":memory:"):
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self._init_schema()

    def _init_schema(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY,
                payload TEXT,
                priority INTEGER DEFAULT 2,
                status TEXT DEFAULT 'waiting',
                created_at TEXT,
                result TEXT
            );
            CREATE TABLE IF NOT EXISTS dependencies (
                task_id TEXT,
                depends_on TEXT,
                PRIMARY KEY (task_id, depends_on)
            );
        """)
        self.conn.commit()

    def enqueue(
        self,
        payload: dict,
        priority: Priority = Priority.NORMAL,
        depends_on: list[str] | None = None,
    ) -> str:
        task_id = str(uuid.uuid4())[:10]
        deps = depends_on or []
        status = "waiting" if deps else "pending"

        self.conn.execute(
            "INSERT INTO tasks (task_id, payload, priority, status, created_at) VALUES (?,?,?,?,?)",
            (task_id, json.dumps(payload), int(priority), status, datetime.utcnow().isoformat()),
        )
        for dep in deps:
            self.conn.execute(
                "INSERT OR IGNORE INTO dependencies (task_id, depends_on) VALUES (?,?)",
                (task_id, dep),
            )
        self.conn.commit()
        return task_id

    def _unlock_dependents(self, completed_task_id: str):
        """Mark dependent tasks as pending if all their deps are done."""
        waiting = self.conn.execute(
            "SELECT DISTINCT task_id FROM dependencies WHERE depends_on=?",
            (completed_task_id,),
        ).fetchall()

        for (waiter_id,) in waiting:
            unmet = self.conn.execute(
                """SELECT COUNT(*) FROM dependencies d
                   JOIN tasks t ON d.depends_on = t.task_id
                   WHERE d.task_id=? AND t.status != 'done'""",
                (waiter_id,),
            ).fetchone()[0]
            if unmet == 0:
                self.conn.execute(
                    "UPDATE tasks SET status='pending' WHERE task_id=?", (waiter_id,)
                )

        self.conn.commit()

    def dequeue(self) -> dict | None:
        row = self.conn.execute(
            "SELECT task_id, payload, priority FROM tasks WHERE status='pending' ORDER BY priority, created_at LIMIT 1"
        ).fetchone()
        if not row:
            return None
        task_id, payload, priority = row
        self.conn.execute(
            "UPDATE tasks SET status='running' WHERE task_id=?", (task_id,)
        )
        self.conn.commit()
        return {"task_id": task_id, "payload": json.loads(payload), "priority": priority}

    def complete(self, task_id: str, result: str):
        self.conn.execute(
            "UPDATE tasks SET status='done', result=? WHERE task_id=?",
            (result[:200], task_id),
        )
        self.conn.commit()
        self._unlock_dependents(task_id)

    def get_result(self, task_id: str) -> str | None:
        row = self.conn.execute(
            "SELECT result FROM tasks WHERE task_id=?", (task_id,)
        ).fetchone()
        return row[0] if row else None

    def run_all(self, max_iterations: int = 20) -> dict[str, str]:
        client = anthropic.Anthropic()
        results = {}

        for _ in range(max_iterations):
            task = self.dequeue()
            if not task:
                waiting = self.conn.execute(
                    "SELECT COUNT(*) FROM tasks WHERE status='pending'"
                ).fetchone()[0]
                if waiting == 0:
                    break
                continue

            print(f"[Queue] Running task {task['task_id']} (priority={task['priority']})")
            payload = task["payload"]
            prompt = payload.get("prompt", "Say OK.")

            # Inject dependency results into prompt if requested
            if "inject_deps" in payload:
                dep_results = []
                for dep_id in payload["inject_deps"]:
                    dep_result = self.get_result(dep_id)
                    if dep_result:
                        dep_results.append(dep_result)
                if dep_results:
                    prompt += "\n\nContext from prior steps:\n" + "\n".join(dep_results)

            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=64,
                messages=[{"role": "user", "content": prompt}],
            )
            result = response.content[0].text
            self.complete(task["task_id"], result)
            results[task["task_id"]] = result
            print(f"  → {result[:50]}")

        return results


if __name__ == "__main__":
    q = DependencyQueue()

    # Step 1: gather facts (can run in parallel)
    t1 = q.enqueue({"prompt": "Name the capital of France."}, Priority.HIGH)
    t2 = q.enqueue({"prompt": "Name the capital of Germany."}, Priority.HIGH)

    # Step 2: depends on both facts
    t3 = q.enqueue(
        {"prompt": "What do these two cities have in common?", "inject_deps": [t1, t2]},
        Priority.NORMAL,
        depends_on=[t1, t2],
    )

    print(f"Tasks: t1={t1}, t2={t2}, t3={t3}\n")
    q.run_all()
# Expected Token Savings: Avoids redundant upstream calls by reusing completed task results
# Environment: pip install anthropic; sqlite3, json, uuid are stdlib
```

---

## Option 5: Dead Letter Queue with Human Review Interface

```python
import sqlite3
import json
import uuid
import anthropic
from datetime import datetime

class DeadLetterQueue:
    """
    Persistent queue where failed tasks go to a dead-letter partition
    for inspection and optional replay.
    """

    MAX_ATTEMPTS = 2

    def __init__(self, db_path: str = ":memory:"):
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self._init_schema()

    def _init_schema(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY,
                payload TEXT,
                status TEXT DEFAULT 'pending',
                attempts INTEGER DEFAULT 0,
                created_at TEXT,
                last_error TEXT,
                partition TEXT DEFAULT 'main'
            );
            CREATE TABLE IF NOT EXISTS task_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT,
                event TEXT,
                detail TEXT,
                occurred_at TEXT DEFAULT (datetime('now'))
            );
        """)
        self.conn.commit()

    def _log(self, task_id: str, event: str, detail: str = ""):
        self.conn.execute(
            "INSERT INTO task_log (task_id, event, detail) VALUES (?,?,?)",
            (task_id, event, detail),
        )

    def enqueue(self, payload: dict) -> str:
        task_id = str(uuid.uuid4())[:10]
        self.conn.execute(
            "INSERT INTO tasks (task_id, payload, created_at) VALUES (?,?,?)",
            (task_id, json.dumps(payload), datetime.utcnow().isoformat()),
        )
        self._log(task_id, "enqueued")
        self.conn.commit()
        return task_id

    def dequeue(self) -> dict | None:
        row = self.conn.execute(
            "SELECT task_id, payload, attempts FROM tasks WHERE status='pending' AND partition='main' LIMIT 1"
        ).fetchone()
        if not row:
            return None
        task_id, payload, attempts = row
        self.conn.execute(
            "UPDATE tasks SET status='running', attempts=attempts+1 WHERE task_id=?", (task_id,)
        )
        self._log(task_id, "dequeued", f"attempt {attempts+1}")
        self.conn.commit()
        return {"task_id": task_id, "payload": json.loads(payload), "attempts": attempts + 1}

    def complete(self, task_id: str, result: str):
        self.conn.execute(
            "UPDATE tasks SET status='done' WHERE task_id=?", (task_id,)
        )
        self._log(task_id, "completed", result[:100])
        self.conn.commit()

    def fail(self, task_id: str, error: str):
        row = self.conn.execute(
            "SELECT attempts FROM tasks WHERE task_id=?", (task_id,)
        ).fetchone()
        attempts = row[0]

        if attempts >= self.MAX_ATTEMPTS:
            self.conn.execute(
                "UPDATE tasks SET status='dead_letter', partition='dlq', last_error=? WHERE task_id=?",
                (error, task_id),
            )
            self._log(task_id, "dead_lettered", error[:200])
            print(f"[DLQ] Task {task_id} moved to dead-letter queue: {error[:60]}")
        else:
            self.conn.execute(
                "UPDATE tasks SET status='pending', last_error=? WHERE task_id=?",
                (error, task_id),
            )
            self._log(task_id, "retrying", error[:100])

        self.conn.commit()

    def replay_dlq(self) -> int:
        """Move dead-letter tasks back to main queue for retry."""
        self.conn.execute(
            "UPDATE tasks SET status='pending', partition='main', attempts=0, last_error=NULL WHERE partition='dlq'"
        )
        replayed = self.conn.execute("SELECT changes()").fetchone()[0]
        self.conn.commit()
        print(f"[DLQ] Replayed {replayed} tasks")
        return replayed

    def inspect_dlq(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT task_id, payload, last_error, attempts FROM tasks WHERE partition='dlq'"
        ).fetchall()
        return [{"id": r[0], "payload": json.loads(r[1]), "error": r[2], "attempts": r[3]} for r in rows]

    def run_demo(self, prompts: list[str]):
        client = anthropic.Anthropic()

        for p in prompts:
            self.enqueue({"prompt": p})

        # Process with simulated failures
        for _ in range(len(prompts) * 2):  # extra rounds for retries
            task = self.dequeue()
            if not task:
                break

            try:
                # Simulate failure for specific prompt
                if "fail" in task["payload"].get("prompt", "").lower():
                    raise ValueError("Simulated failure for testing")

                response = client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=64,
                    messages=[{"role": "user", "content": task["payload"]["prompt"]}],
                )
                self.complete(task["task_id"], response.content[0].text)
                print(f"[Worker] Done {task['task_id']}: {response.content[0].text[:40]}")
            except Exception as e:
                self.fail(task["task_id"], str(e))

        dlq = self.inspect_dlq()
        if dlq:
            print(f"\nDead-letter queue ({len(dlq)} items):")
            for item in dlq:
                print(f"  {item['id']}: {item['error'][:60]}")


if __name__ == "__main__":
    q = DeadLetterQueue()
    q.run_demo([
        "What is 3+3?",
        "Please fail on this task",
        "Name a country.",
        "Please fail on this too",
    ])
# Expected Token Savings: Prevents infinite retry loops that would burn tokens on unrecoverable tasks
# Environment: pip install anthropic; sqlite3, json, uuid are stdlib
```

---

## Option 6: Distributed-Safe Queue with Lease-Based Claiming

```python
import sqlite3
import json
import uuid
import asyncio
import anthropic
from datetime import datetime, timedelta

LEASE_DURATION_SEC = 30  # Worker must renew or task becomes reclaimable

class LeaseQueue:
    """
    Persistent queue with lease-based task ownership.
    If a worker dies mid-task, the lease expires and another worker picks it up.
    Prevents task loss on crash without complex distributed locking.
    """

    def __init__(self, db_path: str = ":memory:"):
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self._init_schema()

    def _init_schema(self):
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY,
                payload TEXT,
                status TEXT DEFAULT 'pending',
                worker_id TEXT,
                lease_expires_at TEXT,
                created_at TEXT,
                attempts INTEGER DEFAULT 0,
                result TEXT
            )
        """)
        self.conn.commit()

    def enqueue(self, payload: dict) -> str:
        task_id = str(uuid.uuid4())[:10]
        self.conn.execute(
            "INSERT INTO tasks (task_id, payload, created_at) VALUES (?,?,?)",
            (task_id, json.dumps(payload), datetime.utcnow().isoformat()),
        )
        self.conn.commit()
        return task_id

    def claim(self, worker_id: str) -> dict | None:
        """Claim a task with a time-limited lease. Also reclaims expired leases."""
        now = datetime.utcnow()
        now_iso = now.isoformat()
        lease_end = (now + timedelta(seconds=LEASE_DURATION_SEC)).isoformat()

        # Reclaim tasks whose lease expired (worker died)
        self.conn.execute(
            """UPDATE tasks SET status='pending', worker_id=NULL, lease_expires_at=NULL
               WHERE status='running' AND lease_expires_at < ?""",
            (now_iso,),
        )

        row = self.conn.execute(
            "SELECT task_id, payload FROM tasks WHERE status='pending' ORDER BY created_at LIMIT 1"
        ).fetchone()

        if not row:
            self.conn.commit()
            return None

        task_id, payload = row
        self.conn.execute(
            "UPDATE tasks SET status='running', worker_id=?, lease_expires_at=?, attempts=attempts+1 WHERE task_id=?",
            (worker_id, lease_end, task_id),
        )
        self.conn.commit()
        return {"task_id": task_id, "payload": json.loads(payload), "worker_id": worker_id}

    def renew_lease(self, task_id: str, worker_id: str) -> bool:
        """Extend lease. Call periodically to prevent reclaim on long tasks."""
        new_expiry = (datetime.utcnow() + timedelta(seconds=LEASE_DURATION_SEC)).isoformat()
        self.conn.execute(
            "UPDATE tasks SET lease_expires_at=? WHERE task_id=? AND worker_id=? AND status='running'",
            (new_expiry, task_id, worker_id),
        )
        self.conn.commit()
        changed = self.conn.execute("SELECT changes()").fetchone()[0]
        return changed > 0

    def complete(self, task_id: str, worker_id: str, result: str):
        self.conn.execute(
            "UPDATE tasks SET status='done', result=? WHERE task_id=? AND worker_id=?",
            (result[:200], task_id, worker_id),
        )
        self.conn.commit()

    def stats(self) -> dict:
        rows = self.conn.execute(
            "SELECT status, COUNT(*), AVG(attempts) FROM tasks GROUP BY status"
        ).fetchall()
        return {r[0]: {"count": r[1], "avg_attempts": round(r[2] or 0, 1)} for r in rows}


async def lease_worker(worker_id: str, queue: LeaseQueue):
    client = anthropic.AsyncAnthropic()
    while True:
        task = queue.claim(worker_id)
        if not task:
            await asyncio.sleep(0.05)
            # Check if all tasks are done
            stats = queue.stats()
            if stats.get("pending", {}).get("count", 0) == 0 and stats.get("running", {}).get("count", 0) == 0:
                return
            continue

        print(f"[{worker_id}] Claimed {task['task_id']}")
        try:
            response = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=64,
                messages=[{"role": "user", "content": task["payload"]["prompt"]}],
            )
            queue.complete(task["task_id"], worker_id, response.content[0].text)
            print(f"[{worker_id}] Completed {task['task_id']}: {response.content[0].text[:40]}")
        except Exception as e:
            # Don't complete — lease will expire and another worker picks it up
            print(f"[{worker_id}] Error on {task['task_id']}: {e}. Lease will expire.")
            await asyncio.sleep(0.1)


async def run_lease_demo():
    queue = LeaseQueue()
    prompts = ["What is AI?", "Name a planet.", "What is 7*8?", "Name a fruit."]
    for p in prompts:
        queue.enqueue({"prompt": p})

    workers = [
        asyncio.create_task(lease_worker(f"worker-{i}", queue))
        for i in range(2)
    ]
    await asyncio.gather(*workers)
    print(f"\nFinal stats: {queue.stats()}")


if __name__ == "__main__":
    asyncio.run(run_lease_demo())
# Expected Token Savings: 0% direct — lease recovery prevents lost work re-submission
# Environment: pip install anthropic; sqlite3, asyncio, json, uuid are stdlib
```

---

## Comparison

| Option | Persistence | Retry | Priority | Dependencies | Crash Recovery | Best For |
|--------|-------------|-------|----------|--------------|----------------|----------|
| 1 | SQLite | No | FIFO | No | On restart | Simple task pipelines |
| 2 | SQLite | Exponential backoff | FIFO | No | On restart | Transient-failure resilience |
| 3 | SQLite | No | FIFO | No | On restart | High-throughput parallel processing |
| 4 | SQLite | No | Priority levels | DAG deps | On restart | Multi-step pipelines with ordering |
| 5 | SQLite + DLQ | Limited | FIFO | No | On restart | Production with human review |
| 6 | SQLite + leases | Via lease expiry | FIFO | No | Mid-task crash | Multi-process/distributed workers |
