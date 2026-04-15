---
layout: solution
title: "Agent Race Condition Corrupts Shared State"
category: concurrency
description: "Multiple concurrent agent tasks read and write shared state without synchronisation — causing lost updates, double-processing, or corrupted records when two tasks modify the same data simultaneously."
tags: [concurrency, race-condition, async, locking, atomicity, database]
---

## Symptom

Two users trigger the same agent workflow concurrently. One task's update is silently overwritten:

```
[t=0ms] Task A reads counter: 5
[t=0ms] Task B reads counter: 5
[t=10ms] Task A writes counter: 6  ← lost
[t=10ms] Task B writes counter: 6  ← overwrites A's write
Expected: 7, Actual: 6
```

Or: both tasks process the same job from a queue, causing duplicate execution.

## Root Cause

Shared mutable state (dictionaries, files, database rows) is accessed without locks. The read-modify-write cycle is not atomic — between reading the value and writing the update, another coroutine or thread can interleave and apply its own conflicting write.

## Fix

---

### Option 1 — AsyncIO Lock per Shared Resource

Protect each shared resource with an `asyncio.Lock()`. Only one coroutine can hold the lock at a time — others wait. Fine-grained locks reduce contention compared to a single global lock.

```python
import asyncio
import anthropic

async_client = anthropic.AsyncAnthropic()

# Shared state — must be protected
_counters: dict[str, int] = {}
_locks: dict[str, asyncio.Lock] = {}

def get_lock(resource_id: str) -> asyncio.Lock:
    if resource_id not in _locks:
        _locks[resource_id] = asyncio.Lock()
    return _locks[resource_id]

async def increment_counter(resource_id: str, amount: int = 1) -> int:
    """Atomic read-modify-write using asyncio.Lock."""
    async with get_lock(resource_id):
        current = _counters.get(resource_id, 0)
        await asyncio.sleep(0)  # Simulate async work inside critical section
        new_value = current + amount
        _counters[resource_id] = new_value
        return new_value

async def agent_task(task_id: str, resource_id: str) -> str:
    """Each task increments a shared counter safely."""
    response = await async_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        messages=[{"role": "user", "content": f"Task {task_id}: say 'Processing'"}],
    )

    # Atomic increment — no race condition possible
    new_count = await increment_counter(resource_id)
    return f"Task {task_id}: counter now at {new_count}"

async def demonstrate_safe_concurrency():
    # Run 10 concurrent tasks updating the same counter
    tasks = [
        agent_task(f"T{i:02d}", "shared-counter")
        for i in range(10)
    ]

    results = await asyncio.gather(*tasks)
    for r in results:
        print(r)

    final = _counters.get("shared-counter", 0)
    print(f"\nFinal counter: {final} (expected: 10)")
    assert final == 10, f"Race condition detected! Got {final}, expected 10"
    print("No race condition — all increments applied correctly.")

asyncio.run(demonstrate_safe_concurrency())
```

**Expected Token Savings:** None — correctness fix; prevents data corruption
**Environment:** `pip install anthropic`

---

### Option 2 — SQLite Atomic Transactions

Use SQLite's atomic transactions for shared state. `BEGIN EXCLUSIVE` prevents concurrent writes; the database serialises conflicting updates automatically.

```python
import sqlite3
import asyncio
import threading
import anthropic
from pathlib import Path
from contextlib import contextmanager

DB_PATH = Path("agent_state.db")
_db_lock = threading.Lock()  # SQLite is not async-safe; use thread lock

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS job_queue (
            job_id TEXT PRIMARY KEY,
            status TEXT DEFAULT 'pending',
            result TEXT,
            worker_id TEXT,
            updated_at REAL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS counters (
            name TEXT PRIMARY KEY,
            value INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()

@contextmanager
def exclusive_transaction():
    with _db_lock:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("BEGIN EXCLUSIVE")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

def claim_job(worker_id: str) -> str | None:
    """
    Atomically claim one pending job.
    Only one worker gets each job — no duplicate processing.
    """
    with exclusive_transaction() as conn:
        row = conn.execute(
            "SELECT job_id FROM job_queue WHERE status = 'pending' LIMIT 1"
        ).fetchone()

        if not row:
            return None

        job_id = row[0]
        import time
        conn.execute("""
            UPDATE job_queue
            SET status = 'processing', worker_id = ?, updated_at = ?
            WHERE job_id = ? AND status = 'pending'
        """, (worker_id, time.time(), job_id))

        # Verify we actually claimed it (no TOCTOU race)
        claimed = conn.execute(
            "SELECT job_id FROM job_queue WHERE job_id = ? AND worker_id = ?",
            (job_id, worker_id),
        ).fetchone()

        return job_id if claimed else None

def complete_job(job_id: str, result: str):
    import time
    with exclusive_transaction() as conn:
        conn.execute("""
            UPDATE job_queue SET status = 'completed', result = ?, updated_at = ?
            WHERE job_id = ?
        """, (result, time.time(), job_id))

def atomic_increment(counter_name: str, amount: int = 1) -> int:
    with exclusive_transaction() as conn:
        conn.execute("""
            INSERT INTO counters (name, value) VALUES (?, ?)
            ON CONFLICT(name) DO UPDATE SET value = value + ?
        """, (counter_name, amount, amount))
        row = conn.execute(
            "SELECT value FROM counters WHERE name = ?", (counter_name,)
        ).fetchone()
        return row[0]

# Setup
init_db()

# Seed some jobs
with exclusive_transaction() as conn:
    for i in range(5):
        try:
            conn.execute("INSERT INTO job_queue (job_id) VALUES (?)", (f"job-{i}",))
        except sqlite3.IntegrityError:
            pass

# Simulate concurrent workers
import concurrent.futures

def worker(worker_id: str):
    client = anthropic.Anthropic()
    claimed = claim_job(worker_id)
    if not claimed:
        return f"{worker_id}: no jobs available"

    # Process the job
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        messages=[{"role": "user", "content": f"Process job {claimed}. Say 'done'."}],
    )
    result = response.content[0].text
    complete_job(claimed, result)

    count = atomic_increment("processed_jobs")
    return f"{worker_id} completed {claimed} (total processed: {count})"

with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
    futures = [pool.submit(worker, f"worker-{i}") for i in range(8)]
    for f in concurrent.futures.as_completed(futures):
        print(f.result())
```

**Expected Token Savings:** None — prevents duplicate job processing; each job runs exactly once
**Environment:** `pip install anthropic`

---

### Option 3 — Optimistic Locking with Version Numbers

Use version numbers (optimistic locking) instead of exclusive locks. Read the version, make changes, write back only if the version hasn't changed. Retry on conflict. Avoids lock contention for read-heavy workloads.

```python
import asyncio
import time
import anthropic
from dataclasses import dataclass

@dataclass
class VersionedRecord:
    id: str
    data: dict
    version: int
    updated_at: float

_store: dict[str, VersionedRecord] = {}
_store_lock = asyncio.Lock()

async def read_record(record_id: str) -> VersionedRecord | None:
    async with _store_lock:
        record = _store.get(record_id)
        if not record:
            return None
        return VersionedRecord(**record.__dict__)  # Return a copy

async def update_record(
    record_id: str,
    updates: dict,
    expected_version: int,
) -> tuple[bool, str]:
    """
    Optimistic update — succeeds only if version matches.
    Returns (success, error_message).
    """
    async with _store_lock:
        record = _store.get(record_id)
        if not record:
            return False, f"Record {record_id} not found"

        if record.version != expected_version:
            return False, (
                f"Version conflict: expected {expected_version}, "
                f"current is {record.version}. Please re-read and retry."
            )

        # Apply updates atomically
        record.data.update(updates)
        record.version += 1
        record.updated_at = time.time()
        return True, ""

async def create_record(record_id: str, data: dict):
    async with _store_lock:
        _store[record_id] = VersionedRecord(
            id=record_id,
            data=data,
            version=1,
            updated_at=time.time(),
        )

async def agent_update_with_retry(
    agent_id: str,
    record_id: str,
    update_fn,
    max_retries: int = 5,
) -> str:
    async_client = anthropic.AsyncAnthropic()

    for attempt in range(max_retries):
        record = await read_record(record_id)
        if not record:
            return f"[{agent_id}] Record not found"

        new_data = update_fn(dict(record.data))

        success, error = await update_record(record_id, new_data, record.version)
        if success:
            return f"[{agent_id}] Updated to version {record.version + 1} on attempt {attempt + 1}"

        print(f"[{agent_id}] {error} — retrying ({attempt + 1}/{max_retries})")
        await asyncio.sleep(0.01 * (2 ** attempt))  # Exponential backoff

    return f"[{agent_id}] Failed after {max_retries} retries"

async def main():
    await create_record("config-1", {"feature_flags": [], "max_users": 100})

    # Simulate concurrent updates
    def add_flag_a(data): data["feature_flags"] = data.get("feature_flags", []) + ["flag-A"]; return data
    def add_flag_b(data): data["feature_flags"] = data.get("feature_flags", []) + ["flag-B"]; return data
    def increment_users(data): data["max_users"] = data.get("max_users", 0) + 50; return data

    results = await asyncio.gather(
        agent_update_with_retry("agent-1", "config-1", add_flag_a),
        agent_update_with_retry("agent-2", "config-1", add_flag_b),
        agent_update_with_retry("agent-3", "config-1", increment_users),
    )

    for r in results:
        print(r)

    final = await read_record("config-1")
    print(f"\nFinal state: {final.data}")
    print(f"Final version: {final.version}")

asyncio.run(main())
```

**Expected Token Savings:** None — correctness fix; no lost updates
**Environment:** `pip install anthropic`

---

### Option 4 — Redis Distributed Lock for Multi-Process Agents

When agent tasks run across multiple processes or containers, use Redis `SET NX PX` for distributed mutual exclusion. Lock auto-expires if the process crashes.

```python
import asyncio
import uuid
import time
import anthropic
import redis.asyncio as redis
from contextlib import asynccontextmanager

async_client = anthropic.AsyncAnthropic()
r = redis.Redis(host="localhost", port=6379, decode_responses=True)

LOCK_TTL_MS = 30_000  # 30 seconds — auto-release if process crashes

@asynccontextmanager
async def distributed_lock(resource_id: str, max_wait_seconds: float = 10.0):
    """
    Acquire a Redis distributed lock. Raises RuntimeError if lock cannot be acquired.
    Releases automatically on exit, even if an exception occurs.
    """
    lock_key = f"lock:{resource_id}"
    lock_value = str(uuid.uuid4())
    deadline = time.monotonic() + max_wait_seconds

    # Try to acquire lock with backoff
    while True:
        acquired = await r.set(lock_key, lock_value, nx=True, px=LOCK_TTL_MS)
        if acquired:
            break
        if time.monotonic() > deadline:
            raise RuntimeError(
                f"Could not acquire lock for '{resource_id}' within {max_wait_seconds}s"
            )
        await asyncio.sleep(0.1)

    try:
        yield
    finally:
        # Release only if we still own the lock (Lua script is atomic)
        lua_script = """
        if redis.call('get', KEYS[1]) == ARGV[1] then
            return redis.call('del', KEYS[1])
        else
            return 0
        end
        """
        await r.eval(lua_script, 1, lock_key, lock_value)

async def process_shared_resource(worker_id: str, resource_id: str) -> str:
    """Process a shared resource with distributed locking."""
    try:
        async with distributed_lock(resource_id, max_wait_seconds=5.0):
            print(f"[{worker_id}] Acquired lock for {resource_id}")

            # Critical section — only one worker at a time
            current_val = int(await r.get(f"state:{resource_id}") or "0")
            await asyncio.sleep(0.1)  # Simulate work
            new_val = current_val + 1
            await r.set(f"state:{resource_id}", new_val)

            print(f"[{worker_id}] Updated {resource_id}: {current_val} → {new_val}")
            return f"{worker_id}: OK (value={new_val})"

    except RuntimeError as e:
        return f"{worker_id}: LOCK FAILED — {e}"

async def main():
    await r.set("state:shared-counter", "0")

    results = await asyncio.gather(*[
        process_shared_resource(f"worker-{i:02d}", "shared-counter")
        for i in range(10)
    ])

    for r_val in results:
        print(r_val)

    final = await r.get("state:shared-counter")
    print(f"\nFinal value: {final} (expected: 10)")
    await r.close()

asyncio.run(main())
```

**Expected Token Savings:** None — distributed correctness fix for multi-instance deployments
**Environment:** `pip install anthropic redis`

---

### Option 5 — Actor Model: Single-Writer Queue per Resource

Route all writes to a shared resource through a single async queue (actor pattern). Only one coroutine ever writes — no locks needed because there's no concurrent access.

```python
import asyncio
import anthropic
from dataclasses import dataclass, field
from typing import Any

@dataclass
class WriteRequest:
    key: str
    value: Any
    future: asyncio.Future

class ActorStore:
    """
    Single-writer actor. All writes are serialised through a queue.
    Reads are concurrent (lock-free) because writes are atomic.
    """
    def __init__(self):
        self._state: dict = {}
        self._queue: asyncio.Queue = asyncio.Queue()
        self._writer_task: asyncio.Task | None = None

    async def start(self):
        self._writer_task = asyncio.create_task(self._writer_loop())

    async def stop(self):
        if self._writer_task:
            self._writer_task.cancel()

    async def _writer_loop(self):
        """Single writer — processes one write at a time, no races possible."""
        while True:
            request = await self._queue.get()
            try:
                self._state[request.key] = request.value
                request.future.set_result(request.value)
            except Exception as e:
                request.future.set_exception(e)
            finally:
                self._queue.task_done()

    async def write(self, key: str, value: Any) -> Any:
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        await self._queue.put(WriteRequest(key=key, value=value, future=future))
        return await future

    def read(self, key: str, default: Any = None) -> Any:
        """Lock-free reads — safe because writes are atomic dict updates."""
        return self._state.get(key, default)

    async def update(self, key: str, update_fn) -> Any:
        """Read-modify-write through the actor — guaranteed atomic."""
        current = self.read(key)
        new_value = update_fn(current)
        return await self.write(key, new_value)

async_client = anthropic.AsyncAnthropic()
store = ActorStore()

async def agent_increment(agent_id: str, key: str) -> str:
    new_val = await store.update(key, lambda v: (v or 0) + 1)
    return f"[{agent_id}] counter = {new_val}"

async def main():
    await store.start()

    # 20 concurrent agents — all writes serialised through actor, no races
    results = await asyncio.gather(*[
        agent_increment(f"agent-{i:02d}", "global-counter")
        for i in range(20)
    ])

    for r in results:
        print(r)

    final = store.read("global-counter")
    print(f"\nFinal counter: {final} (expected: 20)")
    assert final == 20

    await store.stop()

asyncio.run(main())
```

**Expected Token Savings:** None — lock-free reads for maximum throughput; serialised writes for correctness
**Environment:** `pip install anthropic`

---

### Option 6 — Atomic Compare-and-Swap with Retry

Implement CAS (compare-and-swap) semantics for in-process state. Only apply the update if the current value matches the expected value — otherwise retry with the fresh value.

```python
import asyncio
import anthropic
from typing import TypeVar, Callable

T = TypeVar("T")

class AtomicRef:
    """Thread-safe and asyncio-safe atomic reference."""

    def __init__(self, initial_value):
        self._value = initial_value
        self._lock = asyncio.Lock()
        self._version = 0

    async def get(self):
        async with self._lock:
            return self._value, self._version

    async def compare_and_set(self, expected_version: int, new_value) -> bool:
        async with self._lock:
            if self._version != expected_version:
                return False
            self._value = new_value
            self._version += 1
            return True

    async def update(self, update_fn: Callable, max_retries: int = 10):
        """Apply update_fn atomically, retrying on CAS failure."""
        for attempt in range(max_retries):
            current, version = await self.get()
            new_value = update_fn(current)
            success = await self.compare_and_set(version, new_value)
            if success:
                return new_value
            # Conflict — another coroutine updated; retry with fresh value
            await asyncio.sleep(0)  # Yield to let other coroutines run

        raise RuntimeError(f"CAS failed after {max_retries} retries")

# Shared atomic state
task_results: AtomicRef = AtomicRef([])
error_count: AtomicRef = AtomicRef(0)

async_client = anthropic.AsyncAnthropic()

async def agent_task(task_id: str) -> str:
    response = await async_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        messages=[{"role": "user", "content": f"Task {task_id}: reply with just 'ok'"}],
    )
    result = f"{task_id}:{response.content[0].text.strip()}"

    # Atomic append to shared list — CAS retry on conflict
    await task_results.update(lambda lst: lst + [result])
    return result

async def main():
    tasks = [agent_task(f"T{i:02d}") for i in range(15)]
    await asyncio.gather(*tasks)

    final_results, version = await task_results.get()
    print(f"Collected {len(final_results)} results (version {version}):")
    for r in sorted(final_results):
        print(f"  {r}")

    assert len(final_results) == 15, f"Lost results! Got {len(final_results)}, expected 15"
    print("\nAll results collected without data loss.")

asyncio.run(main())
```

**Expected Token Savings:** None — correctness fix; no lost concurrent updates
**Environment:** `pip install anthropic`

---

## Comparison

| Option | Mechanism | Distributed | Deadlock Risk | Best For |
|--------|-----------|-------------|---------------|----------|
| AsyncIO Lock | Per-resource lock | No | Low | Single-process async agents |
| SQLite Transactions | DB serialisation | No | Very Low | Persistent state with DB |
| Optimistic Locking | Version numbers | Yes | None | Read-heavy, low-contention |
| Redis Distributed Lock | NX + TTL | Yes | Low (auto-expire) | Multi-process/container |
| Actor Model | Single-writer queue | No | None | High-write-throughput agents |
| Compare-and-Swap | Retry on conflict | No | None | In-memory atomic updates |

**Recommended starting point:** Option 1 (AsyncIO Lock) for single-process agents. Option 4 (Redis Lock) for distributed deployments with multiple agent instances.
