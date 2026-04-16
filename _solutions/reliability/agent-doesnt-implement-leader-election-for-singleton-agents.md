---
title: "Agent Doesn't Implement Leader Election for Singleton Agents"
description: "Running multiple instances of an agent without leader election causes duplicate work, split-brain state mutations, and conflicting decisions when only one instance should act at a time."
difficulty: advanced
category: reliability
tags: [leader-election, singleton, distributed, redis, etcd, coordination, reliability]
---

## Problem

Horizontally scaled agents may run multiple replicas for availability, but some operations must be performed by exactly one instance: scheduled jobs, singleton coordinators, unique event processors. Without leader election, every replica fires the job simultaneously, causing duplicate side effects, double-charged payments, and inconsistent state.

```python
# Broken: every replica runs the cron job
import asyncio

async def scheduled_cleanup():
    while True:
        await asyncio.sleep(3600)
        # ALL replicas run this — records deleted multiple times,
        # audit log shows duplicate entries
        await db.execute("DELETE FROM expired_sessions WHERE expires_at < NOW()")

asyncio.create_task(scheduled_cleanup())
```

---

## Solution 1: Redis-Based Leader Election with TTL Heartbeat

```python
import asyncio
import os
import time
import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator

# Requires: pip install redis[asyncio]
import redis.asyncio as aioredis

LEADER_KEY = "agent:leader"
HEARTBEAT_INTERVAL = 5.0   # seconds
LEADER_TTL = 15            # seconds (must be > HEARTBEAT_INTERVAL)
INSTANCE_ID = str(uuid.uuid4())

class RedisLeaderElection:
    def __init__(self, redis_url: str, key: str = LEADER_KEY,
                 ttl: int = LEADER_TTL,
                 heartbeat_interval: float = HEARTBEAT_INTERVAL):
        self._redis_url = redis_url
        self._key = key
        self._ttl = ttl
        self._heartbeat_interval = heartbeat_interval
        self._instance_id = INSTANCE_ID
        self._is_leader = False
        self._redis: aioredis.Redis | None = None
        self._heartbeat_task: asyncio.Task | None = None

    async def connect(self):
        self._redis = await aioredis.from_url(self._redis_url,
                                               decode_responses=True)

    async def try_acquire(self) -> bool:
        """Attempt to become leader. Returns True if successful."""
        result = await self._redis.set(
            self._key, self._instance_id,
            nx=True,    # Only set if Not eXists
            ex=self._ttl
        )
        self._is_leader = result is not None
        return self._is_leader

    async def renew(self) -> bool:
        """Renew leadership lease. Returns False if leadership was lost."""
        # Only renew if we still own the key (atomic Lua script)
        script = """
        if redis.call("get", KEYS[1]) == ARGV[1] then
            return redis.call("expire", KEYS[1], ARGV[2])
        else
            return 0
        end
        """
        result = await self._redis.eval(
            script, 1, self._key, self._instance_id, str(self._ttl)
        )
        self._is_leader = bool(result)
        return self._is_leader

    async def release(self):
        """Voluntarily release leadership."""
        script = """
        if redis.call("get", KEYS[1]) == ARGV[1] then
            return redis.call("del", KEYS[1])
        else
            return 0
        end
        """
        await self._redis.eval(script, 1, self._key, self._instance_id)
        self._is_leader = False

    async def _heartbeat_loop(self):
        while self._is_leader:
            await asyncio.sleep(self._heartbeat_interval)
            renewed = await self.renew()
            if not renewed:
                print(f"[LeaderElection] {self._instance_id}: lost leadership")
                break

    @asynccontextmanager
    async def campaign(self) -> AsyncIterator[bool]:
        """Context manager: yields True if this instance wins election."""
        acquired = await self.try_acquire()
        if acquired:
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
            print(f"[LeaderElection] {self._instance_id}: became leader")
        try:
            yield acquired
        finally:
            if acquired:
                if self._heartbeat_task:
                    self._heartbeat_task.cancel()
                await self.release()
                print(f"[LeaderElection] {self._instance_id}: released leadership")

    @property
    def is_leader(self) -> bool:
        return self._is_leader

# Usage: singleton scheduled task
async def run_singleton_job(redis_url: str):
    election = RedisLeaderElection(redis_url)
    await election.connect()

    while True:
        async with election.campaign() as am_leader:
            if am_leader:
                await perform_singleton_work()
            else:
                # Follower: wait and retry
                await asyncio.sleep(HEARTBEAT_INTERVAL)

async def perform_singleton_work():
    print("[Leader] Running exclusive cleanup job")
    await asyncio.sleep(1)  # actual work here
```

---

## Solution 2: File-Based Lock for Single-Host Multi-Process Agents

```python
import asyncio
import fcntl
import os
import signal
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

class FileLockLeaderElection:
    """
    For single-host deployments: use an exclusive file lock + PID file.
    Surviving replicas can detect dead leaders via stale PID files.
    """

    def __init__(self, lock_dir: str = "/tmp", job_name: str = "agent-job"):
        self.lock_path = Path(lock_dir) / f"{job_name}.lock"
        self.pid_path = Path(lock_dir) / f"{job_name}.pid"
        self._fd: int | None = None

    def try_acquire(self) -> bool:
        """Non-blocking exclusive file lock."""
        try:
            self._fd = os.open(str(self.lock_path),
                               os.O_CREAT | os.O_WRONLY | os.O_TRUNC)
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # Write PID to lock file for diagnostics
            os.write(self._fd, str(os.getpid()).encode())
            return True
        except (IOError, OSError):
            if self._fd is not None:
                os.close(self._fd)
                self._fd = None
            return False

    def release(self):
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
        self.lock_path.unlink(missing_ok=True)

    def current_leader_pid(self) -> int | None:
        """Read PID of current leader (for diagnostics)."""
        try:
            content = self.lock_path.read_text().strip()
            return int(content) if content else None
        except (FileNotFoundError, ValueError):
            return None

    def is_leader_alive(self) -> bool:
        pid = self.current_leader_pid()
        if pid is None:
            return False
        try:
            os.kill(pid, 0)  # signal 0: check existence without sending
            return True
        except (ProcessLookupError, PermissionError):
            return False

async def run_with_file_lock(job_name: str, work_fn, retry_interval: float = 5.0):
    election = FileLockLeaderElection(job_name=job_name)
    while True:
        if election.try_acquire():
            print(f"[FileLock] PID {os.getpid()} acquired lock for {job_name}")
            try:
                await work_fn()
            finally:
                election.release()
            return
        else:
            leader_pid = election.current_leader_pid()
            print(f"[FileLock] Another instance (PID {leader_pid}) is leader. "
                  f"Retrying in {retry_interval}s")
            await asyncio.sleep(retry_interval)
```

---

## Solution 3: Database Row Lock Leader Election (PostgreSQL Advisory Locks)

```python
import asyncio
import hashlib
from typing import Callable, Awaitable

# Requires: pip install asyncpg
import asyncpg

def _job_lock_id(job_name: str) -> int:
    """Derive a stable integer lock ID from a job name (fits PostgreSQL bigint)."""
    h = hashlib.sha256(job_name.encode()).digest()
    val = int.from_bytes(h[:8], "big")
    return val & 0x7FFFFFFFFFFFFFFF  # ensure positive

class PostgresAdvisoryLockElection:
    """
    Uses PostgreSQL session-level advisory locks for leader election.
    Lock is automatically released when the connection closes (crash-safe).
    """

    def __init__(self, pool: asyncpg.Pool, job_name: str):
        self.pool = pool
        self.job_name = job_name
        self.lock_id = _job_lock_id(job_name)
        self._conn: asyncpg.Connection | None = None

    async def try_acquire(self) -> bool:
        """Non-blocking attempt to acquire advisory lock."""
        self._conn = await self.pool.acquire()
        acquired = await self._conn.fetchval(
            "SELECT pg_try_advisory_lock($1)", self.lock_id
        )
        if not acquired:
            await self.pool.release(self._conn)
            self._conn = None
        return bool(acquired)

    async def release(self):
        if self._conn:
            await self._conn.fetchval(
                "SELECT pg_advisory_unlock($1)", self.lock_id
            )
            await self.pool.release(self._conn)
            self._conn = None

    async def run_as_leader(self, work_fn: Callable[[], Awaitable[None]],
                            retry_interval: float = 10.0):
        """Retry until elected, then run work function exclusively."""
        while True:
            if await self.try_acquire():
                print(f"[PGLock] Acquired leader lock for '{self.job_name}'")
                try:
                    await work_fn()
                finally:
                    await self.release()
                    print(f"[PGLock] Released leader lock for '{self.job_name}'")
                return
            else:
                await asyncio.sleep(retry_interval)

    async def wait_for_leadership(self):
        """Block until this instance is elected leader (uses blocking advisory lock)."""
        self._conn = await self.pool.acquire()
        # pg_advisory_lock blocks until available — leader crash releases it instantly
        await self._conn.execute("SELECT pg_advisory_lock($1)", self.lock_id)
        print(f"[PGLock] Became leader for '{self.job_name}' via blocking lock")
```

---

## Solution 4: In-Process Leader Election for Worker Pools

```python
import asyncio
import time
from dataclasses import dataclass, field

@dataclass
class WorkerState:
    worker_id: int
    is_leader: bool = False
    last_heartbeat: float = field(default_factory=time.monotonic)

class InProcessLeaderElection:
    """
    Elect one leader among a pool of async workers in the same process.
    Useful for worker pools where only one worker should perform singleton tasks.
    """

    def __init__(self, num_workers: int, heartbeat_interval: float = 2.0,
                 election_timeout: float = 6.0):
        self._workers: dict[int, WorkerState] = {}
        self._leader_id: int | None = None
        self._lock = asyncio.Lock()
        self.num_workers = num_workers
        self.heartbeat_interval = heartbeat_interval
        self.election_timeout = election_timeout

    async def register(self, worker_id: int):
        async with self._lock:
            self._workers[worker_id] = WorkerState(worker_id=worker_id)

    async def heartbeat(self, worker_id: int):
        async with self._lock:
            if worker_id in self._workers:
                self._workers[worker_id].last_heartbeat = time.monotonic()

    async def elect(self) -> int | None:
        """
        Elect the leader: lowest-ID alive worker wins.
        Called periodically by a coordinator or by workers themselves.
        """
        async with self._lock:
            now = time.monotonic()
            alive = [
                w for w in self._workers.values()
                if now - w.last_heartbeat <= self.election_timeout
            ]
            if not alive:
                self._leader_id = None
                return None

            new_leader = min(alive, key=lambda w: w.worker_id)

            if self._leader_id != new_leader.worker_id:
                # Clear old leader
                if self._leader_id is not None:
                    old = self._workers.get(self._leader_id)
                    if old:
                        old.is_leader = False
                # Elect new
                new_leader.is_leader = True
                self._leader_id = new_leader.worker_id
                print(f"[Election] Worker {new_leader.worker_id} elected leader")

            return self._leader_id

    async def is_leader(self, worker_id: int) -> bool:
        async with self._lock:
            return self._leader_id == worker_id

async def worker_loop(election: InProcessLeaderElection, worker_id: int):
    await election.register(worker_id)
    while True:
        await election.heartbeat(worker_id)
        await election.elect()

        if await election.is_leader(worker_id):
            print(f"[Worker {worker_id}] Running singleton task as leader")
            await asyncio.sleep(1)  # do singleton work
        else:
            await asyncio.sleep(0.5)  # follower idle

# Launch a pool where only one worker does singleton work
async def demo():
    election = InProcessLeaderElection(num_workers=3)
    workers = [asyncio.create_task(worker_loop(election, i)) for i in range(3)]
    await asyncio.gather(*workers)
```

---

## Solution 5: Fencing Token Pattern for Safe Leader Actions

```python
import asyncio
import time
from dataclasses import dataclass

@dataclass
class FencingToken:
    """Monotonically increasing token issued to each leader election winner."""
    token: int
    leader_id: str
    issued_at: float

class FencedLeaderElection:
    """
    Combines Redis leader election with a fencing token.
    Each new leader receives a higher token than the previous one.
    Downstream services reject requests from old leaders (stale tokens).
    Prevents split-brain writes after a leader is preempted.
    """

    def __init__(self, redis_url: str, key: str = "agent:leader:fenced"):
        self._redis_url = redis_url
        self._key = key
        self._token_key = f"{key}:token"
        self._redis = None

    async def connect(self):
        import redis.asyncio as aioredis
        self._redis = await aioredis.from_url(self._redis_url,
                                               decode_responses=True)

    async def try_acquire_with_token(self, leader_id: str,
                                      ttl: int = 15) -> FencingToken | None:
        """Acquire leadership and get a fencing token atomically."""
        script = """
        -- Try to become leader
        if redis.call("set", KEYS[1], ARGV[1], "NX", "EX", ARGV[2]) then
            -- Increment global token counter
            local token = redis.call("incr", KEYS[2])
            return token
        else
            return nil
        end
        """
        token_val = await self._redis.eval(
            script, 2, self._key, self._token_key,
            leader_id, str(ttl)
        )
        if token_val is None:
            return None
        return FencingToken(
            token=int(token_val),
            leader_id=leader_id,
            issued_at=time.time()
        )

class FencedStorage:
    """
    Storage that rejects writes from old leaders using fencing tokens.
    """

    def __init__(self):
        self._max_seen_token = 0
        self._data: dict[str, str] = {}
        self._lock = asyncio.Lock()

    async def write(self, key: str, value: str,
                    fencing_token: FencingToken) -> bool:
        async with self._lock:
            if fencing_token.token < self._max_seen_token:
                print(f"[FencedStorage] Rejected stale write: "
                      f"token={fencing_token.token} < max={self._max_seen_token}")
                return False
            self._max_seen_token = max(self._max_seen_token, fencing_token.token)
            self._data[key] = value
            return True
```

---

## Solution 6: Raft-Inspired Single-Node Epoch Guard

```python
import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Awaitable

@dataclass
class Epoch:
    number: int
    leader_id: str
    started_at: float = field(default_factory=time.monotonic)

class EpochGuard:
    """
    Lightweight epoch-based guard: any async operation can check whether
    it still belongs to the current epoch before committing side effects.
    Prevents a deposed leader from completing stale work.
    """

    def __init__(self):
        self._epoch: Epoch | None = None
        self._lock = asyncio.Lock()

    async def new_epoch(self, leader_id: str) -> Epoch:
        async with self._lock:
            prev = self._epoch.number if self._epoch else 0
            self._epoch = Epoch(number=prev + 1, leader_id=leader_id)
            return self._epoch

    async def is_current(self, epoch: Epoch) -> bool:
        async with self._lock:
            return self._epoch is not None and self._epoch.number == epoch.number

    async def assert_current(self, epoch: Epoch):
        if not await self.is_current(epoch):
            raise EpochExpiredError(
                f"Epoch {epoch.number} expired (current: "
                f"{self._epoch.number if self._epoch else None})"
            )

class EpochExpiredError(RuntimeError):
    pass

class EpochAwareLeader:
    """
    Leader that checks epoch validity at every critical step.
    """

    def __init__(self, election: RedisLeaderElection, guard: EpochGuard):
        self._election = election
        self._guard = guard
        self._instance_id = str(uuid.uuid4())[:8]

    async def run_safe_job(self, steps: list[Callable[[Epoch], Awaitable[None]]]):
        """Run a multi-step job, aborting immediately if leadership is lost."""
        async with self._election.campaign() as am_leader:
            if not am_leader:
                return

            epoch = await self._guard.new_epoch(self._instance_id)
            print(f"[EpochLeader] Starting job at epoch {epoch.number}")

            for i, step in enumerate(steps):
                try:
                    await self._guard.assert_current(epoch)
                    await step(epoch)
                    print(f"[EpochLeader] Step {i+1} completed in epoch {epoch.number}")
                except EpochExpiredError:
                    print(f"[EpochLeader] Leadership lost at step {i+1}, aborting")
                    return
```

---

## Comparison

| Solution | Crash Recovery | Network Split | Fencing | Complexity | Best For |
|---|---|---|---|---|---|
| 1. Redis TTL + heartbeat | Auto (TTL expiry) | Risk of split-brain | No | Low | Most cloud deployments |
| 2. File lock + PID | Auto (OS releases) | N/A (single host) | No | Low | Single-host multi-process |
| 3. PG advisory lock | Auto (conn close) | Depends on PG | No | Low | Already using Postgres |
| 4. In-process election | N/A (same process) | N/A | No | Low | Worker pool coordination |
| 5. Fencing token | Via token rejection | Safe with fencing | Yes | Med | Distributed storage writes |
| 6. Epoch guard | Manual epoch bump | Requires election | Yes | Med | Multi-step critical jobs |

**Key principle**: the lock TTL must be longer than the heartbeat interval but short enough to recover quickly from crashes. For crash safety, always use `NX` (set-if-not-exists) + `EX` (expiry) in a single atomic Redis command. For split-brain protection, pair with fencing tokens so storage layers can reject writes from deposed leaders.
