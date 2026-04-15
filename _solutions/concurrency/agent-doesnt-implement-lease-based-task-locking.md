---
layout: solution
title: "Agent Doesn't Implement Lease-Based Task Locking"
category: concurrency
description: "When multiple agent instances run in parallel, they compete for the same tasks causing duplicate processing, race conditions, and wasted API calls. Lease-based locking assigns exclusive task ownership with automatic expiry, preventing duplicate execution without permanent deadlocks."
tags: [concurrency, locking, lease, distributed, task-queue, multi-agent]
---

# Agent Doesn't Implement Lease-Based Task Locking

## Problem

In distributed or multi-process agent deployments, multiple workers may pick up the same task simultaneously. Without exclusive ownership, tasks get processed twice, side effects are applied multiple times, and API costs double. Simple mutex locks fail because a crashed worker can hold a lock forever, blocking all others. Lease-based locking solves this: ownership automatically expires after a TTL, enabling safe re-claim by another worker.

## Why This Happens

Teams add `asyncio.Lock()` or `threading.Lock()` which work within a single process but fail across processes or restarts. Database-backed leases feel complex to implement. The failure mode (duplicate processing) is intermittent and hard to reproduce, so the problem is often discovered too late.

## Solutions

### Option 1: SQLite Lease Table — Atomic lease acquisition with TTL expiry

```python
import anthropic
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

LEASE_DB = Path("/tmp/task_leases.db")
LEASE_TTL = 60.0      # Lease expires after 60 seconds
HEARTBEAT_INTERVAL = 15.0  # Renew lease every 15 seconds


@dataclass
class Lease:
    task_id: str
    worker_id: str
    acquired_at: float
    expires_at: float

    @property
    def is_valid(self) -> bool:
        return time.time() < self.expires_at


class SQLiteLeaseManager:
    def __init__(self, db_path: Path = LEASE_DB, ttl: float = LEASE_TTL):
        self.db = db_path
        self.ttl = ttl
        self._init()

    def _init(self) -> None:
        with sqlite3.connect(self.db) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS leases (
                    task_id TEXT PRIMARY KEY,
                    worker_id TEXT NOT NULL,
                    acquired_at REAL NOT NULL,
                    expires_at REAL NOT NULL
                )
            """)

    def acquire(self, task_id: str, worker_id: str) -> Lease | None:
        """Atomically acquire lease. Returns Lease if successful, None if already held."""
        now = time.time()
        expires = now + self.ttl
        try:
            with sqlite3.connect(self.db) as conn:
                # Delete expired lease first (if any)
                conn.execute(
                    "DELETE FROM leases WHERE task_id = ? AND expires_at < ?",
                    (task_id, now)
                )
                # Try to insert new lease (fails if task_id already exists)
                conn.execute(
                    "INSERT INTO leases(task_id, worker_id, acquired_at, expires_at) VALUES (?,?,?,?)",
                    (task_id, worker_id, now, expires)
                )
            return Lease(task_id=task_id, worker_id=worker_id, acquired_at=now, expires_at=expires)
        except sqlite3.IntegrityError:
            return None  # Another worker holds the lease

    def renew(self, task_id: str, worker_id: str) -> bool:
        """Extend lease TTL. Returns False if lease was stolen or expired."""
        now = time.time()
        with sqlite3.connect(self.db) as conn:
            result = conn.execute(
                "UPDATE leases SET expires_at = ? WHERE task_id = ? AND worker_id = ? AND expires_at > ?",
                (now + self.ttl, task_id, worker_id, now)
            )
        return result.rowcount > 0

    def release(self, task_id: str, worker_id: str) -> bool:
        """Release lease after task completion."""
        with sqlite3.connect(self.db) as conn:
            result = conn.execute(
                "DELETE FROM leases WHERE task_id = ? AND worker_id = ?",
                (task_id, worker_id)
            )
        return result.rowcount > 0

    def get_holder(self, task_id: str) -> str | None:
        now = time.time()
        with sqlite3.connect(self.db) as conn:
            row = conn.execute(
                "SELECT worker_id FROM leases WHERE task_id = ? AND expires_at > ?",
                (task_id, now)
            ).fetchone()
        return row[0] if row else None


class LeasedTaskAgent:
    def __init__(self, worker_id: str | None = None):
        self.client = anthropic.Anthropic()
        self.worker_id = worker_id or str(uuid.uuid4())[:8]
        self.lease_mgr = SQLiteLeaseManager()

    def process_task(self, task_id: str, task_content: str) -> str | None:
        """Process task only if we can acquire the lease."""
        lease = self.lease_mgr.acquire(task_id, self.worker_id)
        if not lease:
            holder = self.lease_mgr.get_holder(task_id)
            print(f"[LEASE] Task {task_id} already held by {holder}. Skipping.")
            return None

        print(f"[LEASE] Worker {self.worker_id} acquired task {task_id}")
        try:
            response = self.client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=512,
                messages=[{"role": "user", "content": task_content}]
            )
            result = response.content[0].text
            self.lease_mgr.release(task_id, self.worker_id)
            print(f"[LEASE] Worker {self.worker_id} completed and released {task_id}")
            return result
        except Exception as e:
            # Don't release on error — let TTL expire, another worker will retry
            print(f"[LEASE] Worker {self.worker_id} failed task {task_id}: {e}")
            raise


# Usage: simulate two workers competing for the same task
mgr = SQLiteLeaseManager()
worker_a = LeasedTaskAgent("worker-A")
worker_b = LeasedTaskAgent("worker-B")

# Worker A acquires the task
result_a = worker_a.process_task("task-001", "Summarize: AI has transformed software engineering.")
# Worker B tries the same task — should be skipped
result_b = worker_b.process_task("task-001", "Summarize: AI has transformed software engineering.")

print(f"Worker A result: {result_a[:50] if result_a else 'SKIPPED'}")
print(f"Worker B result: {result_b[:50] if result_b else 'SKIPPED'}")

# Expected Token Savings: 100% savings on duplicate work prevented by leasing
# Environment: Distributed batch processors, multi-worker agent fleets, queue-based workflows
```

### Option 2: Redis Lease (SETNX Pattern) — Atomic distributed lock with expiry

```python
import anthropic
import time
import uuid
from dataclasses import dataclass

# Redis client (requires: pip install redis)
# from redis import Redis
# For demo, we simulate with an in-memory dict
class SimulatedRedis:
    """Simulates Redis SET NX PX behavior."""
    def __init__(self):
        self._store: dict[str, tuple[str, float]] = {}  # key -> (value, expire_at)

    def set(self, key: str, value: str, nx: bool = False, px: int = 0) -> bool:
        """SET key value [NX] [PX ms]. Returns True if set, False if NX and key exists."""
        now = time.time()
        # Expire stale entries
        if key in self._store and self._store[key][1] < now:
            del self._store[key]

        if nx and key in self._store:
            return False
        expire_at = now + px / 1000 if px else float("inf")
        self._store[key] = (value, expire_at)
        return True

    def get(self, key: str) -> str | None:
        entry = self._store.get(key)
        if not entry or entry[1] < time.time():
            return None
        return entry[0]

    def delete(self, key: str) -> int:
        return 1 if self._store.pop(key, None) else 0

    def pexpire(self, key: str, ms: int) -> bool:
        if key not in self._store:
            return False
        self._store[key] = (self._store[key][0], time.time() + ms / 1000)
        return True


redis = SimulatedRedis()

LOCK_TTL_MS = 60_000   # 60 second lock TTL
HEARTBEAT_MS = 15_000  # Renew every 15 seconds


@dataclass
class RedisLease:
    key: str
    token: str  # Unique token to prevent stealing other workers' locks
    acquired: bool

    def __bool__(self):
        return self.acquired


class RedisLeaseManager:
    def __init__(self, redis_client=None):
        self.redis = redis_client or redis

    def acquire(self, task_id: str, ttl_ms: int = LOCK_TTL_MS) -> RedisLease:
        key = f"task_lock:{task_id}"
        token = str(uuid.uuid4())
        acquired = self.redis.set(key, token, nx=True, px=ttl_ms)
        return RedisLease(key=key, token=token, acquired=acquired)

    def renew(self, lease: RedisLease, ttl_ms: int = LOCK_TTL_MS) -> bool:
        """Renew only if we still hold the lock (compare token)."""
        current = self.redis.get(lease.key)
        if current != lease.token:
            return False  # Lock was stolen or expired
        return self.redis.pexpire(lease.key, ttl_ms)

    def release(self, lease: RedisLease) -> bool:
        """Release only if we still hold the lock."""
        current = self.redis.get(lease.key)
        if current != lease.token:
            return False  # Already expired or stolen — don't delete someone else's lock
        return bool(self.redis.delete(lease.key))


class RedisLeasedAgent:
    def __init__(self, worker_id: str):
        self.client = anthropic.Anthropic()
        self.worker_id = worker_id
        self.lease_mgr = RedisLeaseManager()

    def process(self, task_id: str, content: str) -> str | None:
        lease = self.lease_mgr.acquire(task_id)
        if not lease:
            print(f"[REDIS] Worker {self.worker_id}: task {task_id} locked by another worker")
            return None

        print(f"[REDIS] Worker {self.worker_id}: acquired lock for {task_id}")
        try:
            response = self.client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=256,
                messages=[{"role": "user", "content": content}]
            )
            text = response.content[0].text
            self.lease_mgr.release(lease)
            return text
        except Exception:
            # Don't release — let TTL expire for automatic retry
            raise


# Usage
agent_1 = RedisLeasedAgent("worker-1")
agent_2 = RedisLeasedAgent("worker-2")

result1 = agent_1.process("job-42", "What is the capital of France?")
result2 = agent_2.process("job-42", "What is the capital of France?")  # Should skip

print(f"Worker 1: {result1}")
print(f"Worker 2: {result2}")

# Expected Token Savings: 100% on duplicate tasks; Redis atomic SET NX prevents any race condition
# Environment: Kubernetes agent fleets, Celery workers, any horizontally-scaled agent deployment
```

### Option 3: Async Lease Heartbeat — Background task keeps lease alive during long processing

```python
import anthropic
import asyncio
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

HEARTBEAT_DB = Path("/tmp/heartbeat_leases.db")
LEASE_TTL = 30.0
HEARTBEAT_INTERVAL = 8.0  # Heartbeat every 8s for a 30s TTL


@dataclass
class HeartbeatLease:
    task_id: str
    worker_id: str
    expires_at: float
    _heartbeat_task: asyncio.Task | None = None

    def cancel_heartbeat(self) -> None:
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()


class AsyncLeaseManager:
    def __init__(self, db_path: Path = HEARTBEAT_DB, ttl: float = LEASE_TTL):
        self.db = db_path
        self.ttl = ttl
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self.db) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS leases (
                    task_id TEXT PRIMARY KEY,
                    worker_id TEXT NOT NULL,
                    expires_at REAL NOT NULL
                )
            """)

    def _acquire_sync(self, task_id: str, worker_id: str) -> bool:
        now = time.time()
        try:
            with sqlite3.connect(self.db) as conn:
                conn.execute("DELETE FROM leases WHERE task_id=? AND expires_at<?", (task_id, now))
                conn.execute(
                    "INSERT INTO leases(task_id, worker_id, expires_at) VALUES (?,?,?)",
                    (task_id, worker_id, now + self.ttl)
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def _renew_sync(self, task_id: str, worker_id: str) -> bool:
        with sqlite3.connect(self.db) as conn:
            result = conn.execute(
                "UPDATE leases SET expires_at=? WHERE task_id=? AND worker_id=? AND expires_at>?",
                (time.time() + self.ttl, task_id, worker_id, time.time())
            )
        return result.rowcount > 0

    def _release_sync(self, task_id: str, worker_id: str) -> None:
        with sqlite3.connect(self.db) as conn:
            conn.execute("DELETE FROM leases WHERE task_id=? AND worker_id=?", (task_id, worker_id))

    @asynccontextmanager
    async def lease(self, task_id: str, worker_id: str):
        """Context manager: acquire lease, start heartbeat, release on exit."""
        acquired = await asyncio.get_event_loop().run_in_executor(
            None, self._acquire_sync, task_id, worker_id
        )
        if not acquired:
            raise RuntimeError(f"Could not acquire lease for {task_id}")

        async def heartbeat_loop():
            while True:
                await asyncio.sleep(HEARTBEAT_INTERVAL)
                renewed = await asyncio.get_event_loop().run_in_executor(
                    None, self._renew_sync, task_id, worker_id
                )
                if renewed:
                    print(f"[HEARTBEAT] Renewed lease for {task_id}")
                else:
                    print(f"[HEARTBEAT] Lost lease for {task_id} — stopping")
                    break

        heartbeat = asyncio.create_task(heartbeat_loop())
        print(f"[LEASE] {worker_id} acquired {task_id}, heartbeat started")
        try:
            yield
        finally:
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass
            await asyncio.get_event_loop().run_in_executor(
                None, self._release_sync, task_id, worker_id
            )
            print(f"[LEASE] {worker_id} released {task_id}")


class AsyncHeartbeatAgent:
    def __init__(self, worker_id: str):
        self.client = anthropic.AsyncAnthropic()
        self.worker_id = worker_id
        self.mgr = AsyncLeaseManager()

    async def process(self, task_id: str, content: str) -> str | None:
        try:
            async with self.mgr.lease(task_id, self.worker_id):
                # Simulate long-running processing
                await asyncio.sleep(0.1)
                response = await self.client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=256,
                    messages=[{"role": "user", "content": content}]
                )
                return response.content[0].text
        except RuntimeError as e:
            print(f"[SKIP] {self.worker_id}: {e}")
            return None


async def main():
    workers = [AsyncHeartbeatAgent(f"worker-{i}") for i in range(3)]
    tasks_content = {"task-A": "Explain async/await.", "task-B": "What is a mutex?"}

    # All workers compete for both tasks
    all_tasks = [
        worker.process(task_id, content)
        for task_id, content in tasks_content.items()
        for worker in workers
    ]
    results = await asyncio.gather(*all_tasks, return_exceptions=True)
    successful = [r for r in results if isinstance(r, str)]
    print(f"\nSuccessful completions: {len(successful)}/{len(tasks_content)} tasks (others skipped)")


asyncio.run(main())

# Expected Token Savings: Zero duplicate LLM calls in parallel fleets; heartbeat prevents deadlock
# Environment: Long-running async agents, multi-worker batch jobs, Celery/asyncio hybrid deployments
```

### Option 4: Optimistic Locking — Process without lock; detect conflict on save, retry once

```python
import anthropic
import sqlite3
import time
import hashlib
from dataclasses import dataclass
from pathlib import Path

TASK_DB = Path("/tmp/optimistic_tasks.db")


@dataclass
class Task:
    task_id: str
    content: str
    status: str       # pending / processing / done
    version: int      # Increment on each update — used for conflict detection
    result: str = ""


class OptimisticTaskStore:
    def __init__(self, db_path: Path = TASK_DB):
        self.db = db_path
        self._init()

    def _init(self) -> None:
        with sqlite3.connect(self.db) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    content TEXT,
                    status TEXT DEFAULT 'pending',
                    version INTEGER DEFAULT 0,
                    result TEXT DEFAULT ''
                )
            """)

    def create_task(self, task_id: str, content: str) -> None:
        with sqlite3.connect(self.db) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO tasks(task_id, content) VALUES (?,?)",
                (task_id, content)
            )

    def fetch_pending(self) -> Task | None:
        with sqlite3.connect(self.db) as conn:
            row = conn.execute(
                "SELECT task_id, content, status, version FROM tasks WHERE status='pending' LIMIT 1"
            ).fetchone()
        if not row:
            return None
        return Task(task_id=row[0], content=row[1], status=row[2], version=row[3])

    def complete_if_version_matches(self, task: Task, result: str) -> bool:
        """Atomic update: only succeeds if version hasn't changed since fetch."""
        with sqlite3.connect(self.db) as conn:
            updated = conn.execute(
                "UPDATE tasks SET status='done', result=?, version=version+1 "
                "WHERE task_id=? AND version=? AND status='pending'",
                (result, task.task_id, task.version)
            ).rowcount
        return updated > 0  # False = conflict (another worker updated first)


class OptimisticLockingAgent:
    def __init__(self, worker_id: str):
        self.client = anthropic.Anthropic()
        self.worker_id = worker_id
        self.store = OptimisticTaskStore()

    def process_next(self) -> str | None:
        task = self.store.fetch_pending()
        if not task:
            return None

        print(f"[OPT] {self.worker_id}: processing {task.task_id} (v{task.version})")

        # Process WITHOUT holding a lock
        response = self.client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=[{"role": "user", "content": task.content}]
        )
        result = response.content[0].text

        # Try to commit — fails if another worker already completed this task
        committed = self.store.complete_if_version_matches(task, result)
        if committed:
            print(f"[OPT] {self.worker_id}: committed {task.task_id} ✓")
            return result
        else:
            print(f"[OPT] {self.worker_id}: conflict on {task.task_id} — discarding result")
            return None  # Wasted one API call, but no corruption


# Usage
store = OptimisticTaskStore()
store.create_task("task-001", "What is the CAP theorem?")
store.create_task("task-002", "Explain eventual consistency.")

for worker_id in ["worker-A", "worker-B"]:
    agent = OptimisticLockingAgent(worker_id)
    agent.process_next()

# Expected Token Savings: Works well when conflicts are rare (<5% tasks); simple to implement
# Environment: Low-contention task queues, batch jobs where occasional retry is acceptable
```

### Option 5: Fencing Token Queue — Monotonic counter prevents stale workers from committing

```python
import anthropic
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

FENCE_DB = Path("/tmp/fenced_tasks.db")
LEASE_TTL = 45.0


@dataclass
class FencedLease:
    task_id: str
    worker_id: str
    fence_token: int   # Monotonically increasing — higher token = more recent lease
    expires_at: float


class FencedLeaseStore:
    """Fencing tokens prevent stale workers from committing outdated results."""

    def __init__(self, db_path: Path = FENCE_DB, ttl: float = LEASE_TTL):
        self.db = db_path
        self.ttl = ttl
        self._init()

    def _init(self) -> None:
        with sqlite3.connect(self.db) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS fenced_leases (
                    task_id TEXT PRIMARY KEY,
                    worker_id TEXT NOT NULL,
                    fence_token INTEGER NOT NULL,
                    expires_at REAL NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS task_results (
                    task_id TEXT PRIMARY KEY,
                    result TEXT,
                    completed_by TEXT,
                    fence_token INTEGER,
                    completed_at REAL
                )
            """)

    def acquire(self, task_id: str, worker_id: str) -> FencedLease | None:
        now = time.time()
        with sqlite3.connect(self.db) as conn:
            # Get current lease if exists
            existing = conn.execute(
                "SELECT fence_token, expires_at FROM fenced_leases WHERE task_id=?",
                (task_id,)
            ).fetchone()

            if existing and existing[1] > now:
                return None  # Active lease held by someone else

            # Issue new lease with incremented fencing token
            new_token = (existing[0] + 1) if existing else 1
            conn.execute(
                "INSERT OR REPLACE INTO fenced_leases(task_id, worker_id, fence_token, expires_at) "
                "VALUES (?,?,?,?)",
                (task_id, worker_id, new_token, now + self.ttl)
            )
        return FencedLease(
            task_id=task_id, worker_id=worker_id,
            fence_token=new_token, expires_at=now + self.ttl
        )

    def commit_result(self, lease: FencedLease, result: str) -> bool:
        """Commit only if our fencing token is still the latest."""
        with sqlite3.connect(self.db) as conn:
            current = conn.execute(
                "SELECT fence_token FROM fenced_leases WHERE task_id=?",
                (lease.task_id,)
            ).fetchone()

            if not current or current[0] != lease.fence_token:
                print(f"[FENCE] Stale token {lease.fence_token} (current: {current[0] if current else 'none'}) — rejecting")
                return False

            conn.execute(
                "INSERT OR REPLACE INTO task_results(task_id, result, completed_by, fence_token, completed_at) "
                "VALUES (?,?,?,?,?)",
                (lease.task_id, result, lease.worker_id, lease.fence_token, time.time())
            )
            conn.execute(
                "DELETE FROM fenced_leases WHERE task_id=?", (lease.task_id,)
            )
        return True


class FencedAgent:
    def __init__(self, worker_id: str):
        self.client = anthropic.Anthropic()
        self.worker_id = worker_id
        self.store = FencedLeaseStore()

    def process(self, task_id: str, content: str) -> str | None:
        lease = self.store.acquire(task_id, self.worker_id)
        if not lease:
            print(f"[FENCE] {self.worker_id}: cannot acquire {task_id}")
            return None

        print(f"[FENCE] {self.worker_id}: lease {task_id} with token {lease.fence_token}")

        response = self.client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=[{"role": "user", "content": content}]
        )
        result = response.content[0].text

        committed = self.store.commit_result(lease, result)
        return result if committed else None


# Usage
worker1 = FencedAgent("worker-1")
worker2 = FencedAgent("worker-2")

# Worker 1 acquires with token=1
lease1 = worker1.store.acquire("task-X", "worker-1")
print(f"Worker 1 lease token: {lease1.fence_token if lease1 else 'failed'}")

# Worker 2 acquires after worker 1's lease expires (simulate by direct acquire)
# In reality this would happen after TTL expiry
lease2 = FencedLeaseStore().acquire("task-X", "worker-2") if not lease1 else None
print(f"Worker 2 lease: {lease2}")

# Worker 1 tries to commit with stale token — should fail
if lease1:
    result = "Worker 1 answer"
    committed = worker1.store.commit_result(lease1, result)
    print(f"Worker 1 commit: {'success' if committed else 'rejected (stale token)'}")

# Expected Token Savings: Prevents stale worker commits; saves the cost of reprocessing corrupted results
# Environment: Distributed agent fleets, cloud-based multi-worker deployments, Kubernetes agent pods
```

### Option 6: Partitioned Task Ownership — Assign tasks to workers by consistent hash

```python
import anthropic
import hashlib
from dataclasses import dataclass, field

@dataclass
class WorkerPartition:
    worker_id: str
    total_workers: int
    worker_index: int

    def owns(self, task_id: str) -> bool:
        """Deterministic: worker owns tasks where hash(task_id) % total == my_index."""
        h = int(hashlib.sha256(task_id.encode()).hexdigest(), 16)
        return h % self.total_workers == self.worker_index

    def filter_owned(self, task_ids: list[str]) -> list[str]:
        return [t for t in task_ids if self.owns(t)]


class PartitionedAgent:
    def __init__(self, worker_id: str, worker_index: int, total_workers: int):
        self.client = anthropic.Anthropic()
        self.worker_id = worker_id
        self.partition = WorkerPartition(
            worker_id=worker_id,
            total_workers=total_workers,
            worker_index=worker_index,
        )

    def process_batch(self, task_queue: list[dict]) -> list[dict]:
        """Process only tasks that belong to this worker's partition."""
        owned = [t for t in task_queue if self.partition.owns(t["task_id"])]
        not_owned = [t for t in task_queue if not self.partition.owns(t["task_id"])]

        print(f"[PARTITION] Worker {self.worker_id} (index={self.partition.worker_index}): "
              f"owns {len(owned)}/{len(task_queue)} tasks, skipping {len(not_owned)}")

        results = []
        for task in owned:
            response = self.client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=128,
                messages=[{"role": "user", "content": task["content"]}]
            )
            results.append({
                "task_id": task["task_id"],
                "worker": self.worker_id,
                "result": response.content[0].text,
            })
        return results


# Usage: 3 workers, each owns ~1/3 of tasks
tasks = [
    {"task_id": f"task-{i:03d}", "content": f"What is concept number {i}?"}
    for i in range(12)
]

all_results = []
for idx in range(3):
    agent = PartitionedAgent(f"worker-{idx}", worker_index=idx, total_workers=3)
    worker_results = agent.process_batch(tasks)
    all_results.extend(worker_results)
    print(f"Worker {idx} processed: {len(worker_results)} tasks")

print(f"\nTotal: {len(all_results)}/{len(tasks)} tasks processed (should be all unique)")
task_ids_processed = {r["task_id"] for r in all_results}
print(f"Unique task IDs: {len(task_ids_processed)} (duplicates: {len(all_results) - len(task_ids_processed)})")

# Expected Token Savings: Zero coordination overhead, zero duplicate calls — pure partitioned ownership
# Environment: Stable worker fleets, Kafka consumer groups, statically-partitioned batch jobs
```

## Comparison

| Option | Mechanism | Cross-Process | Auto-Expiry | Best For |
|--------|-----------|--------------|-------------|----------|
| SQLite Lease | DB row + TTL | Yes | Yes | Single-host multi-process |
| Redis Lease (SETNX) | Atomic SET | Yes | Yes | Multi-host distributed systems |
| Async Heartbeat | Background renew | Yes (SQLite) | Yes | Long-running async tasks |
| Optimistic Locking | Version check on commit | Yes | N/A | Low-contention queues |
| Fencing Token | Monotonic counter | Yes | Yes | Distributed systems with stale workers |
| Partitioned Ownership | Consistent hash | No lock needed | N/A | Stable worker pools, Kafka-style |
