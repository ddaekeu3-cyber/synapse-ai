---
layout: solution
title: "Agent Doesn't Implement Dead Letter Queue for Failed Tasks"
category: general
description: "Agent silently drops tasks that fail after max retries — no record of what failed, no ability to replay, no alerting. Failed work disappears without a trace."
tags: [reliability, error-handling, dead-letter-queue, observability, retry]
---

## Symptom

Tasks fail and vanish. Users report work never completed. Log search reveals:

```
[ERROR] Task abc-123 failed after 3 retries: APIError 529
[ERROR] Task def-456 failed after 3 retries: Timeout
[ERROR] Task ghi-789 failed after 3 retries: ValidationError
```

But there is no way to know: what exactly failed, what the payload was, or how to retry it later. The tasks are gone.

## Root Cause

The retry loop raises an exception after max attempts without persisting the failed task anywhere:

```python
import anthropic
import asyncio

client = anthropic.AsyncAnthropic(api_key="sk-live-...")

async def process_task(task: dict) -> str:
    for attempt in range(3):
        try:
            response = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=512,
                messages=[{"role": "user", "content": task["prompt"]}]
            )
            return response.content[0].text
        except Exception:
            if attempt == 2:
                raise  # Task is lost — no DLQ, no alerting, no replay
            await asyncio.sleep(2 ** attempt)
```

After `raise`, the task payload, error context, and retry history are all discarded.

---

## Fix

### Option 1 — In-memory dead letter queue with replay

Collect failed tasks in a DLQ list. Expose a replay function to re-process them after the root cause is fixed.

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

client = anthropic.AsyncAnthropic(api_key="sk-live-...")


@dataclass
class DeadLetter:
    task_id: str
    payload: dict
    error: str
    failed_at: float
    attempt_count: int


class InMemoryDLQ:
    def __init__(self):
        self._letters: list[DeadLetter] = []

    def push(self, task_id: str, payload: dict, error: str, attempts: int) -> None:
        self._letters.append(DeadLetter(
            task_id=task_id,
            payload=payload,
            error=error,
            failed_at=time.time(),
            attempt_count=attempts,
        ))
        print(f"[DLQ] Task {task_id} dead-lettered after {attempts} attempts: {error}")

    def drain(self) -> list[DeadLetter]:
        items, self._letters = self._letters, []
        return items

    def __len__(self):
        return len(self._letters)


dlq = InMemoryDLQ()


async def process_task(task: dict, max_retries: int = 3) -> str | None:
    last_error = ""
    for attempt in range(max_retries):
        try:
            response = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=512,
                messages=[{"role": "user", "content": task["prompt"]}],
            )
            return response.content[0].text
        except Exception as exc:
            last_error = str(exc)
            if attempt < max_retries - 1:
                await asyncio.sleep(2 ** attempt)

    # All retries exhausted — send to DLQ instead of silently dropping
    dlq.push(task["id"], task, last_error, max_retries)
    return None


async def replay_dlq() -> list[str]:
    """Replay all dead-lettered tasks — call after fixing root cause."""
    items = dlq.drain()
    results = []
    for letter in items:
        result = await process_task(letter.payload)
        if result:
            results.append(result)
    return results


async def main():
    tasks = [
        {"id": "t1", "prompt": "Summarize AI trends in 2026"},
        {"id": "t2", "prompt": "What is the capital of France?"},
    ]
    results = await asyncio.gather(*[process_task(t) for t in tasks])
    print(f"Processed: {sum(1 for r in results if r)} / {len(tasks)}")
    print(f"DLQ depth: {len(dlq)}")

asyncio.run(main())

# Expected Token Savings: replay only failed tasks — no re-processing of successful ones
# Environment: async agents with background task processing
```

---

### Option 2 — File-based DLQ for persistence across restarts

Persist dead letters to disk so failures survive process restarts and can be inspected offline.

```python
import anthropic
import asyncio
import json
import time
import uuid
from pathlib import Path

client = anthropic.AsyncAnthropic(api_key="sk-live-...")

DLQ_PATH = Path("./dlq")
DLQ_PATH.mkdir(exist_ok=True)


def write_dead_letter(task: dict, error: str, attempts: int) -> Path:
    letter = {
        "task_id": task.get("id", str(uuid.uuid4())),
        "payload": task,
        "error": error,
        "attempts": attempts,
        "failed_at": time.time(),
        "failed_at_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    path = DLQ_PATH / f"{letter['task_id']}.json"
    path.write_text(json.dumps(letter, indent=2))
    print(f"[DLQ] Dead letter written: {path}")
    return path


def read_dead_letters() -> list[dict]:
    return [json.loads(p.read_text()) for p in DLQ_PATH.glob("*.json")]


def ack_dead_letter(task_id: str) -> None:
    """Remove from DLQ after successful replay."""
    path = DLQ_PATH / f"{task_id}.json"
    if path.exists():
        path.unlink()
        print(f"[DLQ] Acknowledged: {task_id}")


async def process_task(task: dict, max_retries: int = 3) -> str | None:
    last_error = ""
    for attempt in range(max_retries):
        try:
            response = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=512,
                messages=[{"role": "user", "content": task["prompt"]}],
            )
            return response.content[0].text
        except Exception as exc:
            last_error = str(exc)
            if attempt < max_retries - 1:
                await asyncio.sleep(2 ** attempt)

    write_dead_letter(task, last_error, max_retries)
    return None


async def replay_all() -> None:
    """Read DLQ from disk and replay each task."""
    letters = read_dead_letters()
    print(f"[DLQ] Replaying {len(letters)} dead letters...")
    for letter in letters:
        result = await process_task(letter["payload"])
        if result:
            ack_dead_letter(letter["task_id"])
            print(f"[DLQ] Replayed OK: {letter['task_id']}")
        else:
            print(f"[DLQ] Still failing: {letter['task_id']}")

# Expected Token Savings: failed task payloads survive restart — no need to regenerate them
# Environment: batch pipelines, cron jobs, any agent that restarts between runs
```

---

### Option 3 — Redis-backed DLQ with TTL and alerting

Production-grade DLQ using Redis sorted sets (scored by failure timestamp) with automatic alerting when depth exceeds threshold.

```python
import anthropic
import asyncio
import json
import time
import redis.asyncio as redis

client = anthropic.AsyncAnthropic(api_key="sk-live-...")
redis_client = redis.Redis(host="localhost", port=6379, db=0, decode_responses=True)

DLQ_KEY = "agent:dlq"
DLQ_TTL = 7 * 24 * 3600  # 7 days
ALERT_THRESHOLD = 10


async def push_to_dlq(task: dict, error: str, attempts: int) -> None:
    letter = json.dumps({
        "task_id": task.get("id"),
        "payload": task,
        "error": error,
        "attempts": attempts,
        "failed_at": time.time(),
    })
    # Store in Redis sorted set; score = failure timestamp for chronological replay
    await redis_client.zadd(DLQ_KEY, {letter: time.time()})
    await redis_client.expire(DLQ_KEY, DLQ_TTL)

    depth = await redis_client.zcard(DLQ_KEY)
    if depth >= ALERT_THRESHOLD:
        # Replace with real alerting: PagerDuty, Slack, etc.
        print(f"[ALERT] DLQ depth {depth} >= threshold {ALERT_THRESHOLD} — investigate!")


async def pop_from_dlq(batch_size: int = 10) -> list[dict]:
    """Get oldest N dead letters for replay."""
    raw_items = await redis_client.zrange(DLQ_KEY, 0, batch_size - 1, withscores=False)
    return [json.loads(item) for item in raw_items]


async def ack_dlq_item(letter_json: str) -> None:
    await redis_client.zrem(DLQ_KEY, letter_json)


async def process_task(task: dict, max_retries: int = 3) -> str | None:
    last_error = ""
    for attempt in range(max_retries):
        try:
            response = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=512,
                messages=[{"role": "user", "content": task["prompt"]}],
            )
            return response.content[0].text
        except Exception as exc:
            last_error = str(exc)
            if attempt < max_retries - 1:
                await asyncio.sleep(2 ** attempt)

    await push_to_dlq(task, last_error, max_retries)
    return None

# Expected Token Savings: shared DLQ across all workers; failed tasks never lost
# Environment: multi-process or multi-container agents with Redis available
```

---

### Option 4 — SQLite DLQ with error categorisation

Persist dead letters to SQLite and categorise errors so replay can target specific failure types (e.g., replay only rate-limit failures after cooldown).

```python
import anthropic
import asyncio
import sqlite3
import json
import time
from contextlib import contextmanager
from enum import StrEnum

client = anthropic.AsyncAnthropic(api_key="sk-live-...")

DB_PATH = "dlq.db"


class ErrorCategory(StrEnum):
    RATE_LIMIT = "rate_limit"
    TIMEOUT = "timeout"
    VALIDATION = "validation"
    UNKNOWN = "unknown"


def categorise_error(error: str) -> ErrorCategory:
    err = error.lower()
    if "429" in err or "rate limit" in err or "overloaded" in err:
        return ErrorCategory.RATE_LIMIT
    if "timeout" in err or "timed out" in err:
        return ErrorCategory.TIMEOUT
    if "validation" in err or "invalid" in err:
        return ErrorCategory.VALIDATION
    return ErrorCategory.UNKNOWN


@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS dead_letters (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                payload TEXT NOT NULL,
                error TEXT NOT NULL,
                error_category TEXT NOT NULL,
                attempts INTEGER NOT NULL,
                failed_at REAL NOT NULL,
                replayed_at REAL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_category ON dead_letters(error_category)")


def write_dead_letter(task: dict, error: str, attempts: int) -> None:
    category = categorise_error(error)
    with get_db() as conn:
        conn.execute(
            "INSERT INTO dead_letters (task_id, payload, error, error_category, attempts, failed_at) VALUES (?,?,?,?,?,?)",
            (task.get("id"), json.dumps(task), error, category, attempts, time.time()),
        )
    print(f"[DLQ] {category.upper()} | task={task.get('id')} | error={error[:60]}")


async def replay_by_category(category: ErrorCategory, max_retries: int = 3) -> int:
    """Replay only dead letters of a specific error category."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM dead_letters WHERE error_category=? AND replayed_at IS NULL",
            (category,),
        ).fetchall()

    replayed = 0
    for row in rows:
        task = json.loads(row["payload"])
        result = await process_task(task, max_retries)
        if result:
            with get_db() as conn:
                conn.execute(
                    "UPDATE dead_letters SET replayed_at=? WHERE id=?",
                    (time.time(), row["id"]),
                )
            replayed += 1

    return replayed


async def process_task(task: dict, max_retries: int = 3) -> str | None:
    init_db()
    last_error = ""
    for attempt in range(max_retries):
        try:
            response = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=512,
                messages=[{"role": "user", "content": task["prompt"]}],
            )
            return response.content[0].text
        except Exception as exc:
            last_error = str(exc)
            if attempt < max_retries - 1:
                await asyncio.sleep(2 ** attempt)

    write_dead_letter(task, last_error, max_retries)
    return None

# Expected Token Savings: targeted replay — only rate-limit failures replayed after cooldown
# Environment: agents with mixed failure modes where different errors need different remediation
```

---

### Option 5 — DLQ with automatic replay scheduler

Failed tasks are automatically retried on a schedule — rate-limit failures after 60 seconds, timeouts after 30, others after 5 minutes.

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass
from collections import defaultdict

client = anthropic.AsyncAnthropic(api_key="sk-live-...")

RETRY_DELAYS = {
    "rate_limit": 60,
    "timeout": 30,
    "unknown": 300,
}


@dataclass
class ScheduledReplay:
    task: dict
    error_type: str
    replay_after: float
    retry_number: int = 1


_scheduled: list[ScheduledReplay] = []
_lock = asyncio.Lock()


def classify_error(exc: Exception) -> str:
    msg = str(exc).lower()
    if "429" in msg or "rate" in msg:
        return "rate_limit"
    if "timeout" in msg:
        return "timeout"
    return "unknown"


async def schedule_replay(task: dict, error_type: str, retry_number: int) -> None:
    delay = RETRY_DELAYS.get(error_type, 300)
    item = ScheduledReplay(
        task=task,
        error_type=error_type,
        replay_after=time.time() + delay,
        retry_number=retry_number,
    )
    async with _lock:
        _scheduled.append(item)
    print(f"[DLQ] Scheduled retry #{retry_number} in {delay}s for task={task.get('id')} [{error_type}]")


async def process_task(task: dict, retry_number: int = 1, max_dlq_retries: int = 3) -> str | None:
    try:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            messages=[{"role": "user", "content": task["prompt"]}],
        )
        return response.content[0].text
    except Exception as exc:
        error_type = classify_error(exc)
        if retry_number <= max_dlq_retries:
            await schedule_replay(task, error_type, retry_number + 1)
        else:
            print(f"[DLQ] PERMANENT FAILURE after {retry_number} DLQ retries: task={task.get('id')}")
        return None


async def run_replay_loop(poll_interval: float = 5.0) -> None:
    """Background task that replays scheduled items when their delay expires."""
    while True:
        now = time.time()
        async with _lock:
            due = [s for s in _scheduled if s.replay_after <= now]
            for item in due:
                _scheduled.remove(item)

        for item in due:
            print(f"[DLQ] Replaying task={item.task.get('id')} retry #{item.retry_number}")
            await process_task(item.task, item.retry_number)

        await asyncio.sleep(poll_interval)


async def main():
    # Start background replay loop
    loop_task = asyncio.create_task(run_replay_loop())

    tasks = [{"id": f"t{i}", "prompt": f"Task {i} prompt"} for i in range(5)]
    await asyncio.gather(*[process_task(t) for t in tasks])

    await asyncio.sleep(120)  # Let scheduled replays run
    loop_task.cancel()

asyncio.run(main())

# Expected Token Savings: intelligent scheduling avoids hammering a rate-limited API
# Environment: long-running async agents that need automatic failure recovery without manual intervention
```

---

### Option 6 — DLQ with structured failure report

Generate a human-readable failure report from the DLQ to surface patterns — which prompts fail most, which error types dominate, estimated lost token cost.

```python
import anthropic
import asyncio
import time
from collections import Counter
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic(api_key="sk-live-...")

INPUT_TOKEN_COST_PER_1K = 0.00025   # claude-haiku-4-5 pricing
OUTPUT_TOKEN_COST_PER_1K = 0.00125


@dataclass
class FailureRecord:
    task_id: str
    prompt_preview: str
    error: str
    attempts: int
    estimated_input_tokens: int
    failed_at: float


@dataclass
class DLQStore:
    records: list[FailureRecord] = field(default_factory=list)

    def push(self, task: dict, error: str, attempts: int) -> None:
        prompt = task.get("prompt", "")
        self.records.append(FailureRecord(
            task_id=task.get("id", "?"),
            prompt_preview=prompt[:80],
            error=error,
            attempts=attempts,
            estimated_input_tokens=len(prompt.split()) * 4 // 3,  # rough estimate
            failed_at=time.time(),
        ))

    def report(self) -> str:
        if not self.records:
            return "DLQ is empty."

        total_attempts = sum(r.attempts for r in self.records)
        total_est_tokens = sum(r.estimated_input_tokens * r.attempts for r in self.records)
        estimated_cost = total_est_tokens / 1000 * INPUT_TOKEN_COST_PER_1K
        error_counts = Counter(r.error.split(":")[0] for r in self.records)

        lines = [
            f"=== DLQ Report ===",
            f"Total dead letters : {len(self.records)}",
            f"Total API attempts : {total_attempts}",
            f"Est. wasted tokens : {total_est_tokens:,}",
            f"Est. wasted cost   : ${estimated_cost:.4f}",
            f"",
            "Error breakdown:",
        ]
        for err_type, count in error_counts.most_common():
            lines.append(f"  {count:3d}x  {err_type}")
        lines.append("")
        lines.append("Failed tasks (preview):")
        for r in self.records[-5:]:
            lines.append(f"  [{r.task_id}] {r.prompt_preview!r:.60} | {r.error[:40]}")
        return "\n".join(lines)


dlq = DLQStore()


async def process_task(task: dict, max_retries: int = 3) -> str | None:
    last_error = ""
    for attempt in range(max_retries):
        try:
            response = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=512,
                messages=[{"role": "user", "content": task["prompt"]}],
            )
            return response.content[0].text
        except Exception as exc:
            last_error = str(exc)
            if attempt < max_retries - 1:
                await asyncio.sleep(2 ** attempt)

    dlq.push(task, last_error, max_retries)
    return None


async def main():
    tasks = [{"id": f"t{i}", "prompt": f"Summarize report #{i}"} for i in range(10)]
    await asyncio.gather(*[process_task(t) for t in tasks])
    print(dlq.report())

asyncio.run(main())

# Expected Token Savings: visibility into wasted spend; report guides remediation priority
# Environment: batch pipelines where operators need to understand failure blast radius
```

---

## Comparison

| Option | Persistence | Cross-Process | Auto-Replay | Error Categorisation | Alerting |
|--------|-------------|---------------|-------------|----------------------|----------|
| 1 | RAM only | No | Manual | No | No |
| 2 | Disk (JSON) | Yes (file share) | Manual | No | No |
| 3 | Redis | Yes | Manual | No | Yes |
| 4 | SQLite | Single node | Manual | Yes | No |
| 5 | RAM only | No | Automatic | Yes | No |
| 6 | RAM only | No | Manual | Yes | Report |

**Recommended starting point:** Option 2 (file-based) for batch jobs and scripts — zero dependencies, survives restarts, human-readable. Option 3 (Redis) for production web services. Combine Option 4's error categorisation with Option 3's persistence for a production-grade DLQ without a dedicated message queue service.
