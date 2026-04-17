---
title: "Agent Doesn't Implement Lease-Based Distributed Task Locking"
description: "Agents deployed across multiple instances that process tasks from a shared queue without distributed locking execute the same task concurrently — two instances pick up the same job, call the same tool twice, and produce duplicate side effects. Implement lease-based distributed task locking where each instance acquires a time-bounded lease before processing, releases it on completion, and relies on lease expiry for automatic recovery when a worker crashes mid-task."
date: 2026-04-16
difficulty: advanced
category: reliability
slug: agent-doesnt-implement-lease-based-distributed-task-locking
tags: [distributed-locking, task-deduplication, lease-management, worker-coordination, at-most-once, crash-recovery]
symptoms:
  - "Same task processed by two agent instances simultaneously — duplicate tool calls and side effects"
  - "Tasks stuck in processing state permanently when a worker crashes mid-execution"
  - "No mechanism to distinguish a crashed worker from a slow one"
  - "Restarting one agent instance causes it to re-process tasks still held by another instance"
  - "No audit trail of which instance processed which task"
---

## Why This Happens

Shared task queues without locks allow multiple consumers to dequeue the same item. Even with queue-level visibility timeouts, agent processes that take longer than the timeout will have their tasks requeued and processed again by another worker. Lease-based locking separates dequeue from processing: a worker acquires an exclusive lease on a task ID before starting work, renews the lease periodically while working, and releases it on completion. If the worker crashes, the lease expires after a configurable TTL and another worker can safely acquire it. This gives at-most-once semantics for non-idempotent operations.

## Solution 1: Task Lease Record

```python
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class LeaseStatus(str, Enum):
    ACQUIRED = "acquired"
    RELEASED = "released"
    EXPIRED = "expired"
    FAILED = "failed"


@dataclass
class TaskLease:
    task_id: str
    lease_id: str = field(default_factory=lambda: str(uuid.uuid4())[:16])
    worker_id: str = ""
    acquired_at: float = field(default_factory=time.time)
    expires_at: float = 0.0
    renewed_at: float = field(default_factory=time.time)
    status: LeaseStatus = LeaseStatus.ACQUIRED
    renewal_count: int = 0

    def is_expired(self) -> bool:
        return time.time() > self.expires_at

    def ttl_remaining(self) -> float:
        return max(0.0, self.expires_at - time.time())

    def extend(self, ttl_seconds: float) -> None:
        self.expires_at = time.time() + ttl_seconds
        self.renewed_at = time.time()
        self.renewal_count += 1
```

## Solution 2: In-Memory Lease Store

```python
import time
from threading import Lock
from typing import Dict, List, Optional


class InMemoryLeaseStore:
    """
    Lease store backed by an in-memory dict.
    For production use, replace with Redis SETNX + EXPIRE for
    cross-instance lease coordination.
    """

    def __init__(self):
        self._leases: Dict[str, TaskLease] = {}
        self._lock = Lock()

    def acquire(
        self,
        task_id: str,
        worker_id: str,
        ttl_seconds: float = 30.0,
    ) -> Optional[TaskLease]:
        """
        Returns a TaskLease if acquired, None if task already leased.
        """
        with self._lock:
            existing = self._leases.get(task_id)
            if existing and not existing.is_expired() and existing.status == LeaseStatus.ACQUIRED:
                return None   # task locked by another worker

            lease = TaskLease(
                task_id=task_id,
                worker_id=worker_id,
                expires_at=time.time() + ttl_seconds,
            )
            self._leases[task_id] = lease
            return lease

    def renew(self, task_id: str, lease_id: str, ttl_seconds: float = 30.0) -> bool:
        """Returns True if renewal succeeded (lease still owned by this lease_id)."""
        with self._lock:
            lease = self._leases.get(task_id)
            if not lease or lease.lease_id != lease_id:
                return False
            if lease.is_expired():
                return False
            lease.extend(ttl_seconds)
            return True

    def release(self, task_id: str, lease_id: str) -> bool:
        """Returns True if successfully released."""
        with self._lock:
            lease = self._leases.get(task_id)
            if not lease or lease.lease_id != lease_id:
                return False
            lease.status = LeaseStatus.RELEASED
            del self._leases[task_id]
            return True

    def fail(self, task_id: str, lease_id: str) -> bool:
        with self._lock:
            lease = self._leases.get(task_id)
            if not lease or lease.lease_id != lease_id:
                return False
            lease.status = LeaseStatus.FAILED
            del self._leases[task_id]
            return True

    def expired_leases(self) -> List[TaskLease]:
        with self._lock:
            return [
                l for l in self._leases.values()
                if l.is_expired() and l.status == LeaseStatus.ACQUIRED
            ]

    def stats(self) -> dict:
        with self._lock:
            return {
                "active_leases": len(self._leases),
                "expired": sum(1 for l in self._leases.values() if l.is_expired()),
            }
```

## Solution 3: Lease Renewal Background Task

```python
import asyncio
import time
from typing import Dict


class LeaseRenewalManager:
    """
    Maintains a background renewal task for each active lease.
    Renews leases at (ttl / 3) intervals to prevent expiry during long processing.
    Cancels renewal when the lease is released or fails.
    """

    def __init__(
        self,
        store: InMemoryLeaseStore,
        renewal_ttl_s: float = 30.0,
    ):
        self._store = store
        self._ttl = renewal_ttl_s
        self._tasks: Dict[str, asyncio.Task] = {}

    async def start_renewal(self, lease: TaskLease) -> None:
        interval = self._ttl / 3.0

        async def renew_loop():
            while True:
                await asyncio.sleep(interval)
                ok = self._store.renew(lease.task_id, lease.lease_id, self._ttl)
                if not ok:
                    break  # lease lost — stop renewing

        task = asyncio.create_task(renew_loop())
        self._tasks[lease.lease_id] = task

    def stop_renewal(self, lease_id: str) -> None:
        task = self._tasks.pop(lease_id, None)
        if task and not task.done():
            task.cancel()
```

## Solution 4: Lease-Protected Task Executor

```python
import asyncio
import time
from typing import Any, Callable, Optional


class LeaseProtectedTaskExecutor:
    """
    Acquires a lease before executing a task function.
    Runs background lease renewal during execution.
    Releases or marks the lease failed based on outcome.
    Records execution history for audit.
    """

    def __init__(
        self,
        store: InMemoryLeaseStore,
        renewal_manager: LeaseRenewalManager,
        worker_id: str,
        lease_ttl_s: float = 30.0,
    ):
        self._store = store
        self._renewal = renewal_manager
        self._worker_id = worker_id
        self._ttl = lease_ttl_s
        self._execution_log: list = []

    async def execute(
        self,
        task_id: str,
        task_fn: Callable,
        *args: Any,
        **kwargs: Any,
    ) -> dict:
        lease = self._store.acquire(task_id, self._worker_id, self._ttl)
        if lease is None:
            return {
                "task_id": task_id,
                "status": "skipped",
                "reason": "lease_held_by_another_worker",
            }

        await self._renewal.start_renewal(lease)
        start = time.time()
        try:
            result = await task_fn(*args, **kwargs)
            latency_ms = round((time.time() - start) * 1000, 2)
            self._store.release(task_id, lease.lease_id)
            self._renewal.stop_renewal(lease.lease_id)
            record = {
                "task_id": task_id,
                "status": "completed",
                "latency_ms": latency_ms,
                "worker_id": self._worker_id,
                "lease_id": lease.lease_id,
                "renewals": lease.renewal_count,
                "ts": time.time(),
            }
        except Exception as exc:
            latency_ms = round((time.time() - start) * 1000, 2)
            self._store.fail(task_id, lease.lease_id)
            self._renewal.stop_renewal(lease.lease_id)
            record = {
                "task_id": task_id,
                "status": "failed",
                "error": str(exc)[:300],
                "latency_ms": latency_ms,
                "worker_id": self._worker_id,
                "lease_id": lease.lease_id,
                "ts": time.time(),
            }

        self._execution_log.append(record)
        if len(self._execution_log) > 10000:
            self._execution_log = self._execution_log[-5000:]
        return record
```

## Solution 5: Expired Lease Reaper

```python
import asyncio
import time
from typing import Callable, List


class ExpiredLeaseReaper:
    """
    Periodically scans for expired leases and makes their tasks
    available for re-acquisition. Fires a callback so the task
    queue can re-enqueue them.
    """

    def __init__(
        self,
        store: InMemoryLeaseStore,
        on_expired: Callable[[str], None],   # callback(task_id)
        poll_interval_s: float = 10.0,
    ):
        self._store = store
        self._on_expired = on_expired
        self._interval = poll_interval_s
        self._reaped_count = 0

    async def run_forever(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            expired = self._store.expired_leases()
            for lease in expired:
                # Remove expired lease from store
                with self._store._lock:
                    if self._store._leases.get(lease.task_id) is lease:
                        del self._store._leases[lease.task_id]
                self._reaped_count += 1
                try:
                    self._on_expired(lease.task_id)
                except Exception:
                    pass

    def stats(self) -> dict:
        return {"reaped_count": self._reaped_count}
```

## Solution 6: Distributed Locking Dashboard

```python
import time


class DistributedLockingDashboard:
    """
    Combines lease store stats, executor history, and reaper stats
    into a single view for worker coordination health.
    """

    def __init__(
        self,
        store: InMemoryLeaseStore,
        executor: LeaseProtectedTaskExecutor,
        reaper: ExpiredLeaseReaper,
    ):
        self._store = store
        self._executor = executor
        self._reaper = reaper

    def render(self) -> dict:
        log = self._executor._execution_log
        recent = [r for r in log if time.time() - r["ts"] < 3600]
        completed = sum(1 for r in recent if r["status"] == "completed")
        failed = sum(1 for r in recent if r["status"] == "failed")
        skipped = sum(1 for r in recent if r["status"] == "skipped")
        return {
            "generated_at": time.time(),
            "lease_store": self._store.stats(),
            "last_hour": {
                "completed": completed,
                "failed": failed,
                "skipped_already_leased": skipped,
                "mean_latency_ms": round(
                    sum(r.get("latency_ms", 0) for r in recent if r["status"] == "completed")
                    / max(completed, 1), 2
                ),
            },
            "reaper": self._reaper.stats(),
        }
```

## Comparison

| Approach | Lease Acquisition | Auto-Renewal | Crash Recovery | Audit Log | Expired Reaping |
|---|---|---|---|---|---|
| InMemoryLeaseStore | Yes (atomic check) | No | Via TTL expiry | No | No |
| LeaseRenewalManager | No | Yes (TTL/3 interval) | No | No | No |
| LeaseProtectedTaskExecutor | Via store | Via renewal | Via store TTL | Yes | No |
| ExpiredLeaseReaper | No | No | Yes (re-enqueue) | No | Yes |
| DistributedLockingDashboard | No | No | No | Via executor | Via reaper |

**Best for production**: Use Redis `SET key value NX PX ttl_ms` for cross-instance lease acquisition — the in-memory store above works only within a single process. Set `lease_ttl_s` to 2× the expected task duration so that a task running at normal speed never needs to race against expiry. Keep `renewal_interval = ttl / 3` so three missed renewals exhaust the TTL — this tolerates two GC pauses or network blips before the lease expires. Monitor `skipped_already_leased` counts: consistently above 5% means multiple workers are competing for the same tasks and queue partitioning should be implemented.
