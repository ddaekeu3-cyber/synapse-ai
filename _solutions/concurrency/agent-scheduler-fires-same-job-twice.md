---
layout: solution
title: "Agent Scheduler Fires the Same Job Twice — Duplicate Task Execution"
category: concurrency
description: "Scheduled agent job runs at 9:00 AM. Due to clock skew, two scheduler instances both trigger it. The job runs twice: emails sent twice, records processed twice, charges made twice."
tags: [concurrency, scheduler, duplicate, cron, idempotency, distributed-lock, at-most-once]
---

# Agent Scheduler Fires the Same Job Twice — Duplicate Task Execution

## Symptom
- Scheduled report sent to all users twice within seconds of each other
- Daily data processing job runs twice — duplicate records in database
- Cron job on two servers both trigger at the same time
- Job scheduler restarts a job before the previous run completes
- After deploy, in-flight job and new instance both run simultaneously
- k8s CronJob creates two pods for the same schedule slot

## Root Cause
Distributed schedulers run on multiple nodes. Without coordination, all nodes trigger the same job when the schedule fires. Clock skew, scheduler restarts, rolling deploys, and "at least once" job delivery guarantees all cause duplicate execution. The only reliable protection is distributed locking or database-level idempotency.

## Fix

### Option 1: Distributed lock using Redis

```python
import redis
import uuid
import time
import functools

redis_client = redis.Redis(host="redis", port=6379, decode_responses=True)

class DistributedJobLock:
    """
    Redis-based lock to ensure only one instance of a scheduled job runs at a time.
    """

    def __init__(self, job_name: str, ttl_seconds: int = 3600):
        self.job_name = job_name
        self.ttl = ttl_seconds
        self.lock_key = f"job_lock:{job_name}"
        self.instance_id = str(uuid.uuid4())[:8]

    def acquire(self) -> bool:
        """
        Try to acquire the lock. Returns True if acquired, False if already running.
        Uses SET NX EX for atomic acquire-with-expiry.
        """
        acquired = redis_client.set(
            self.lock_key,
            f"{self.instance_id}:{time.time()}",
            nx=True,    # Only set if key doesn't exist
            ex=self.ttl # Expire after TTL (prevents deadlock if job crashes)
        )
        if acquired:
            print(f"Job '{self.job_name}' lock acquired by {self.instance_id}")
        else:
            existing = redis_client.get(self.lock_key)
            print(f"Job '{self.job_name}' already running ({existing}) — skipping")
        return bool(acquired)

    def release(self):
        """Release the lock only if we own it"""
        current = redis_client.get(self.lock_key)
        if current and current.startswith(self.instance_id):
            redis_client.delete(self.lock_key)
            print(f"Job '{self.job_name}' lock released by {self.instance_id}")
        else:
            print(f"Job '{self.job_name}' lock not owned by us — not releasing")

    def __enter__(self):
        if not self.acquire():
            raise RuntimeError(f"Job '{self.job_name}' is already running")
        return self

    def __exit__(self, *args):
        self.release()

def single_instance_job(job_name: str, ttl: int = 3600):
    """Decorator: ensure job runs at most once at a time across all instances"""
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            lock = DistributedJobLock(job_name, ttl)
            try:
                with lock:
                    return fn(*args, **kwargs)
            except RuntimeError as e:
                print(f"Skipped: {e}")
                return None
        return wrapper
    return decorator

@single_instance_job("daily_report", ttl=7200)
def send_daily_report():
    """This job is guaranteed to run at most once at any time"""
    users = get_all_users()
    for user in users:
        send_report_email(user)
    print("Daily report sent")
```

### Option 2: Database job execution log (idempotent by job run ID)

```python
import sqlite3
from datetime import datetime, date

class JobExecutionLog:
    """
    Track completed job runs. Reject duplicate execution for the same slot.
    """

    def __init__(self, db_path: str = "job_log.db"):
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS job_runs (
                job_name TEXT NOT NULL,
                run_slot TEXT NOT NULL,   -- e.g., "daily_report:2025-04-15"
                started_at TEXT,
                completed_at TEXT,
                status TEXT DEFAULT 'running',
                instance_id TEXT,
                PRIMARY KEY (job_name, run_slot)
            )
        """)
        self.db.commit()

    def try_start(self, job_name: str, run_slot: str = None, instance_id: str = None) -> bool:
        """
        Try to register this job run. Returns True if this instance should run it.
        Returns False if another instance already started it.
        """
        if run_slot is None:
            run_slot = datetime.utcnow().strftime("%Y-%m-%dT%H:00")  # Hourly slot

        try:
            self.db.execute("""
                INSERT INTO job_runs (job_name, run_slot, started_at, instance_id)
                VALUES (?, ?, ?, ?)
            """, (job_name, run_slot, datetime.utcnow().isoformat(), instance_id or "unknown"))
            self.db.commit()
            print(f"Job '{job_name}' slot '{run_slot}' claimed")
            return True
        except sqlite3.IntegrityError:
            # Another instance already inserted this row
            existing = self.db.execute(
                "SELECT instance_id, status FROM job_runs WHERE job_name=? AND run_slot=?",
                (job_name, run_slot)
            ).fetchone()
            print(f"Job '{job_name}' slot '{run_slot}' already claimed by {existing}")
            return False

    def mark_complete(self, job_name: str, run_slot: str, status: str = "completed"):
        self.db.execute("""
            UPDATE job_runs SET status=?, completed_at=?
            WHERE job_name=? AND run_slot=?
        """, (status, datetime.utcnow().isoformat(), job_name, run_slot))
        self.db.commit()

log = JobExecutionLog()

def idempotent_job(job_name: str, slot_fn=None):
    """Decorator: skip job if already run for this time slot"""
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            slot = slot_fn() if slot_fn else datetime.utcnow().strftime("%Y-%m-%dT%H:00")
            if not log.try_start(job_name, slot):
                print(f"Skipping '{job_name}' — already ran for slot {slot}")
                return None
            try:
                result = fn(*args, **kwargs)
                log.mark_complete(job_name, slot, "completed")
                return result
            except Exception as e:
                log.mark_complete(job_name, slot, f"failed: {e}")
                raise
        return wrapper
    return decorator

@idempotent_job("daily_report", slot_fn=lambda: date.today().isoformat())
def send_daily_report():
    pass  # Runs at most once per day regardless of how many instances trigger it
```

### Option 3: Kubernetes CronJob — single concurrency policy

```yaml
# k8s CronJob with Forbid concurrency — guaranteed no duplicates
apiVersion: batch/v1
kind: CronJob
metadata:
  name: agent-daily-report
spec:
  schedule: "0 9 * * *"        # 9 AM daily
  concurrencyPolicy: Forbid     # Skip if previous job still running (NOT Replace)
  startingDeadlineSeconds: 300  # Give up if can't start within 5 min of schedule
  successfulJobsHistoryLimit: 3
  failedJobsHistoryLimit: 1
  jobTemplate:
    spec:
      # Retry on failure but don't duplicate:
      backoffLimit: 2
      activeDeadlineSeconds: 7200  # Max 2 hours
      template:
        spec:
          restartPolicy: OnFailure
          containers:
            - name: agent
              image: my-agent:latest
              env:
                - name: JOB_NAME
                  value: daily-report
                - name: JOB_SLOT
                  value: "$(date +%Y-%m-%d)"

# concurrencyPolicy options:
# Allow:  Multiple jobs can run simultaneously (dangerous for most agent tasks)
# Forbid: Skip new job if previous still running (safest)
# Replace: Cancel previous, start new (use only for idempotent jobs)
```

### Option 4: Atomic job claim with PostgreSQL advisory locks

```python
import psycopg2

def run_with_pg_advisory_lock(job_id: int, fn, *args, **kwargs):
    """
    Use PostgreSQL advisory lock — integer ID per job.
    Lock is released automatically when session closes.
    Works across all processes connected to same DB.
    """
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()

    # Try advisory lock — non-blocking
    cur.execute("SELECT pg_try_advisory_lock(%s)", (job_id,))
    acquired = cur.fetchone()[0]

    if not acquired:
        print(f"Job {job_id} already locked — skipping")
        conn.close()
        return None

    try:
        print(f"Job {job_id} lock acquired")
        return fn(*args, **kwargs)
    finally:
        cur.execute("SELECT pg_advisory_unlock(%s)", (job_id,))
        conn.commit()
        conn.close()
        print(f"Job {job_id} lock released")

# Usage:
DAILY_REPORT_JOB_ID = 1001  # Stable integer ID for this job

run_with_pg_advisory_lock(DAILY_REPORT_JOB_ID, send_daily_report)
```

### Option 5: Heartbeat lock — handles job crashes gracefully

```python
import threading
import time

class HeartbeatLock:
    """
    Redis lock with heartbeat — prevents lock expiry during long jobs,
    but releases if job crashes (heartbeat stops).
    """

    def __init__(self, key: str, ttl: int = 30):
        self.key = key
        self.ttl = ttl
        self.instance = str(uuid.uuid4())[:8]
        self._heartbeat_thread = None
        self._running = False

    def acquire(self) -> bool:
        acquired = redis_client.set(self.key, self.instance, nx=True, ex=self.ttl)
        if acquired:
            self._start_heartbeat()
        return bool(acquired)

    def _start_heartbeat(self):
        """Renew lock every TTL/2 seconds — keeps lock alive during long jobs"""
        self._running = True
        def heartbeat():
            while self._running:
                time.sleep(self.ttl // 2)
                if self._running:
                    # Renew only if we still own the lock
                    current = redis_client.get(self.key)
                    if current == self.instance:
                        redis_client.expire(self.key, self.ttl)
        self._heartbeat_thread = threading.Thread(target=heartbeat, daemon=True)
        self._heartbeat_thread.start()

    def release(self):
        self._running = False
        current = redis_client.get(self.key)
        if current == self.instance:
            redis_client.delete(self.key)

    def __enter__(self):
        if not self.acquire():
            raise RuntimeError(f"Lock {self.key} already held")
        return self

    def __exit__(self, *args):
        self.release()
```

## Scheduler Duplicate Execution Causes

| Cause | Description | Fix |
|---|---|---|
| Multiple scheduler nodes | 2+ servers run cron on same schedule | Distributed lock |
| Rolling deploy overlap | Old + new instance both start at schedule time | Lock + job log |
| k8s CronJob default | `concurrencyPolicy: Allow` by default | Set to `Forbid` |
| Scheduler restart | Missed-then-caught-up jobs fire twice | `startingDeadlineSeconds` |
| At-least-once delivery | Job queue delivers same job twice | Database idempotency key |
| Clock skew | Two servers' clocks differ by seconds | Lock with TTL tolerance |

## Expected Token Savings
Duplicate job sends 10,000 emails twice → incident + rollback: ~200,000 tokens + reputation
Distributed lock prevents duplicates: 0 wasted

## Environment
- Any distributed agent deployment with scheduled jobs; critical for email, billing, and data processing agents
- Source: direct experience; scheduler duplicates are the most common cause of double-email and double-charge incidents
