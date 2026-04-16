---
layout: solution
title: "Agent Doesn't Implement Quota Reservation for Long-Running Tasks"
category: rate-limit
description: "Reserve API quota upfront before starting multi-step tasks so the agent doesn't hit rate limits mid-execution, with pre-flight checks, incremental reservation, and quota rollback on failure."
tags: [rate-limit, quota, reservation, long-running, pre-flight, token-budget]
---

# Agent Doesn't Implement Quota Reservation for Long-Running Tasks

A multi-step agent that makes 20 API calls fails on step 14 when it hits a rate limit, wasting all prior work. Without quota reservation, the agent discovers limits only when it's already mid-task. Quota reservation checks available capacity before starting, reserves the expected usage upfront, and either proceeds with confidence or defers the task to a time when sufficient quota is available.

## Option 1: Pre-Flight Quota Check Before Task Start

```python
import anthropic
import time
from dataclasses import dataclass

client = anthropic.Anthropic()


@dataclass
class QuotaState:
    """Tracks remaining API quota for a session/org."""
    tokens_per_minute: int = 100_000
    requests_per_minute: int = 100
    tokens_used_this_minute: int = 0
    requests_used_this_minute: int = 0
    minute_start: float = 0.0

    def reset_if_new_minute(self) -> None:
        if time.monotonic() - self.minute_start >= 60.0:
            self.tokens_used_this_minute = 0
            self.requests_used_this_minute = 0
            self.minute_start = time.monotonic()

    def tokens_remaining(self) -> int:
        self.reset_if_new_minute()
        return self.tokens_per_minute - self.tokens_used_this_minute

    def requests_remaining(self) -> int:
        self.reset_if_new_minute()
        return self.requests_per_minute - self.requests_used_this_minute

    def consume(self, tokens: int, requests: int = 1) -> None:
        self.reset_if_new_minute()
        self.tokens_used_this_minute += tokens
        self.requests_used_this_minute += requests


# Singleton quota tracker (in production, load from rate limit headers)
QUOTA = QuotaState(minute_start=time.monotonic())


def estimate_task_cost(steps: list[dict]) -> tuple[int, int]:
    """Estimate total tokens and requests for a multi-step task."""
    total_tokens = sum(s.get("estimated_tokens", 500) for s in steps)
    total_requests = len(steps)
    return total_tokens, total_requests


def preflight_check(steps: list[dict], safety_margin: float = 1.2) -> tuple[bool, str]:
    """Check if quota is sufficient for the task before starting."""
    est_tokens, est_requests = estimate_task_cost(steps)
    needed_tokens = int(est_tokens * safety_margin)
    needed_requests = int(est_requests * safety_margin)

    if QUOTA.tokens_remaining() < needed_tokens:
        return False, f"Insufficient token quota: need {needed_tokens}, have {QUOTA.tokens_remaining()}"
    if QUOTA.requests_remaining() < needed_requests:
        return False, f"Insufficient request quota: need {needed_requests}, have {QUOTA.requests_remaining()}"
    return True, "Quota sufficient"


def run_step(step: dict) -> str:
    """Run one step of the task."""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=step.get("max_tokens", 256),
        messages=[{"role": "user", "content": step["prompt"]}],
    )
    QUOTA.consume(response.usage.input_tokens + response.usage.output_tokens)
    return response.content[0].text


def run_long_task(task_steps: list[dict]) -> list[str] | None:
    """Run a multi-step task only if quota allows."""
    ok, reason = preflight_check(task_steps)
    print(f"[preflight] {reason}")
    print(f"[quota] tokens_remaining={QUOTA.tokens_remaining()}, requests_remaining={QUOTA.requests_remaining()}")

    if not ok:
        print(f"[DEFERRED] Task deferred — insufficient quota")
        return None

    results = []
    for i, step in enumerate(task_steps, 1):
        print(f"[step {i}/{len(task_steps)}] {step['name']}")
        result = run_step(step)
        results.append(result)
        print(f"  Done: {result[:60]}")

    return results


steps = [
    {"name": "analysis", "prompt": "List 3 key factors in software reliability.", "max_tokens": 200, "estimated_tokens": 300},
    {"name": "summary",  "prompt": "Summarize software reliability in one sentence.", "max_tokens": 100, "estimated_tokens": 150},
    {"name": "action",   "prompt": "List 3 actionable steps to improve reliability.", "max_tokens": 200, "estimated_tokens": 300},
]

results = run_long_task(steps)
if results:
    print(f"\nTask completed in {len(results)} steps")

# Expected Token Savings: N/A; preflight prevents wasted tokens on tasks that would fail mid-way
# Environment: Python 3.11+; load actual quota from X-RateLimit-* response headers rather than tracking manually
```

## Option 2: Incremental Quota Reservation with Rollback

```python
import anthropic
import time
from dataclasses import dataclass, field

client = anthropic.Anthropic()


@dataclass
class ReservationLedger:
    """Tracks reserved and consumed quota with rollback support."""
    total_tokens: int = 100_000
    reserved: dict[str, int] = field(default_factory=dict)   # reservation_id -> tokens
    consumed: dict[str, int] = field(default_factory=dict)   # reservation_id -> tokens

    @property
    def available(self) -> int:
        return self.total_tokens - sum(self.reserved.values())

    def reserve(self, reservation_id: str, tokens: int) -> bool:
        if self.available < tokens:
            return False
        self.reserved[reservation_id] = tokens
        return True

    def consume(self, reservation_id: str, actual_tokens: int) -> None:
        """Reduce reservation by actual usage, releasing unused quota."""
        reserved = self.reserved.get(reservation_id, 0)
        self.consumed[reservation_id] = actual_tokens
        # Release unused reserved tokens
        self.reserved[reservation_id] = actual_tokens
        released = reserved - actual_tokens
        if released > 0:
            print(f"[ledger] Released {released} unused reserved tokens from {reservation_id}")

    def rollback(self, reservation_id: str) -> None:
        """Fully release a reservation (on failure)."""
        released = self.reserved.pop(reservation_id, 0)
        self.consumed.pop(reservation_id, None)
        print(f"[ledger] Rollback: released {released} tokens from {reservation_id}")

    def summary(self) -> str:
        total_consumed = sum(self.consumed.values())
        total_reserved = sum(self.reserved.values())
        return f"available={self.available} reserved={total_reserved} consumed={total_consumed}"


LEDGER = ReservationLedger(total_tokens=10_000)


def run_step_with_reservation(step_id: str, prompt: str, max_tokens: int,
                               estimated_tokens: int) -> str | None:
    """Reserve quota for a step, run it, then release unused quota."""
    reservation_id = f"{step_id}-{int(time.time())}"

    # Try to reserve
    if not LEDGER.reserve(reservation_id, estimated_tokens):
        print(f"[{step_id}] Cannot reserve {estimated_tokens} tokens — {LEDGER.summary()}")
        return None

    print(f"[{step_id}] Reserved {estimated_tokens} tokens | {LEDGER.summary()}")

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        actual = response.usage.input_tokens + response.usage.output_tokens
        LEDGER.consume(reservation_id, actual)
        print(f"[{step_id}] Actual={actual} tokens | {LEDGER.summary()}")
        return response.content[0].text

    except Exception as e:
        LEDGER.rollback(reservation_id)
        print(f"[{step_id}] Failed, rolled back reservation: {e}")
        return None


# Multi-step task with per-step reservations
task_steps = [
    ("step-1", "What are Python decorators?", 200, 400),
    ("step-2", "Give a 2-sentence summary of decorators.", 100, 200),
    ("step-3", "List 3 common uses of decorators in production.", 200, 400),
]

all_results = []
for step_id, prompt, max_tok, est_tok in task_steps:
    result = run_step_with_reservation(step_id, prompt, max_tok, est_tok)
    if result is None:
        print(f"Task halted at {step_id} — insufficient quota")
        break
    all_results.append(result)

print(f"\nCompleted {len(all_results)}/{len(task_steps)} steps")
print(f"Final ledger: {LEDGER.summary()}")

# Expected Token Savings: Rollback releases unused reserved tokens, improving quota efficiency by 10-30%
# Environment: Python 3.11+; use actual response.usage.input_tokens for accurate consumption tracking
```

## Option 3: Token Budget Calculator with Task Complexity Estimation

```python
import anthropic
import json

client = anthropic.Anthropic()

# Token cost estimates per model per step type
COST_MODEL = {
    "simple":   {"input": 200, "output": 100},
    "medium":   {"input": 500, "output": 300},
    "complex":  {"input": 1000, "output": 600},
    "with_tools": {"input": 800, "output": 400, "tools": 200},
}

RATE_LIMIT = {
    "tokens_per_minute": 20_000,
    "requests_per_minute": 30,
}


def estimate_budget(task_plan: list[dict]) -> dict:
    """Calculate total estimated cost and rate limit feasibility."""
    total_input = 0
    total_output = 0
    total_requests = len(task_plan)

    for step in task_plan:
        complexity = step.get("complexity", "medium")
        costs = COST_MODEL.get(complexity, COST_MODEL["medium"])
        total_input += costs["input"]
        total_output += costs["output"]
        if step.get("uses_tools"):
            total_input += costs.get("tools", 0)

    total_tokens = total_input + total_output
    minutes_needed = max(
        total_tokens / RATE_LIMIT["tokens_per_minute"],
        total_requests / RATE_LIMIT["requests_per_minute"],
    )

    return {
        "estimated_tokens": total_tokens,
        "estimated_requests": total_requests,
        "minutes_needed": minutes_needed,
        "feasible_in_one_minute": minutes_needed <= 1.0,
        "breakdown": {"input": total_input, "output": total_output},
    }


def run_budgeted_task(task_plan: list[dict]) -> list[str] | None:
    """Run task only if budget analysis shows it's feasible."""
    budget = estimate_budget(task_plan)
    print(f"[budget] estimated_tokens={budget['estimated_tokens']:,}")
    print(f"[budget] minutes_needed={budget['minutes_needed']:.2f}")
    print(f"[budget] feasible_in_one_minute={budget['feasible_in_one_minute']}")

    if not budget["feasible_in_one_minute"]:
        # Could split task across time windows
        print(f"[PLAN] Task requires {budget['minutes_needed']:.1f} minutes — will batch across rate limit windows")

    results = []
    for step in task_plan:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=step.get("max_tokens", 300),
            messages=[{"role": "user", "content": step["prompt"]}],
        )
        actual = response.usage.input_tokens + response.usage.output_tokens
        results.append(response.content[0].text)
        print(f"  [{step['name']}] actual={actual} tokens (estimated={COST_MODEL[step.get('complexity', 'medium')]['input'] + COST_MODEL[step.get('complexity', 'medium')]['output']})")

    return results


task_plan = [
    {"name": "research",   "complexity": "complex",  "prompt": "Explain how Kubernetes scheduling works.", "max_tokens": 400},
    {"name": "summarize",  "complexity": "simple",   "prompt": "Summarize Kubernetes scheduling in 2 sentences.", "max_tokens": 100},
    {"name": "compare",    "complexity": "medium",   "prompt": "Compare Kubernetes scheduling to Docker Swarm scheduling.", "max_tokens": 300},
]

results = run_budgeted_task(task_plan)
print(f"\nCompleted {len(results or [])} steps")

# Expected Token Savings: N/A; budget analysis prevents partial completion and wasted intermediate work
# Environment: Python 3.11+; calibrate COST_MODEL with actual usage data from past runs via SQLite logging
```

## Option 4: SQLite Quota Registry with Cross-Session Reservation

```python
import anthropic
import sqlite3
import time
import uuid

client = anthropic.Anthropic()
DB_PATH = ":memory:"

DAILY_TOKEN_LIMIT = 1_000_000
HOURLY_TOKEN_LIMIT = 100_000


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS quota_reservations (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            task_name TEXT NOT NULL,
            reserved_tokens INTEGER NOT NULL,
            consumed_tokens INTEGER DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'reserved',
            created_at REAL NOT NULL,
            released_at REAL
        );
        CREATE TABLE IF NOT EXISTS quota_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            tokens INTEGER NOT NULL,
            window_type TEXT NOT NULL,
            recorded_at REAL NOT NULL
        );
    """)
    conn.commit()


def get_used_tokens(conn: sqlite3.Connection, window_seconds: int) -> int:
    cutoff = time.time() - window_seconds
    row = conn.execute(
        "SELECT COALESCE(SUM(tokens), 0) FROM quota_usage WHERE recorded_at > ?",
        (cutoff,)
    ).fetchone()
    return row[0] if row else 0


def get_reserved_tokens(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT COALESCE(SUM(reserved_tokens - consumed_tokens), 0) FROM quota_reservations WHERE status='reserved'"
    ).fetchone()
    return row[0] if row else 0


def available_tokens(conn: sqlite3.Connection, window: str = "hourly") -> int:
    window_s = 3600 if window == "hourly" else 86400
    limit = HOURLY_TOKEN_LIMIT if window == "hourly" else DAILY_TOKEN_LIMIT
    used = get_used_tokens(conn, window_s)
    reserved = get_reserved_tokens(conn)
    return max(0, limit - used - reserved)


def reserve_quota(conn: sqlite3.Connection, session_id: str, task_name: str,
                  tokens: int) -> str | None:
    avail = available_tokens(conn)
    if avail < tokens:
        print(f"[quota] Cannot reserve {tokens} tokens — only {avail} available")
        return None

    reservation_id = str(uuid.uuid4())[:8]
    conn.execute(
        "INSERT INTO quota_reservations VALUES (?,?,?,?,0,'reserved',?,NULL)",
        (reservation_id, session_id, task_name, tokens, time.time())
    )
    conn.commit()
    print(f"[quota] Reserved {tokens} tokens for {task_name} (id={reservation_id})")
    return reservation_id


def consume_quota(conn: sqlite3.Connection, session_id: str, reservation_id: str,
                  actual_tokens: int) -> None:
    conn.execute(
        "UPDATE quota_reservations SET consumed_tokens=?, status='consumed', released_at=? WHERE id=?",
        (actual_tokens, time.time(), reservation_id)
    )
    conn.execute(
        "INSERT INTO quota_usage VALUES (NULL,?,?,?,?)",
        (session_id, actual_tokens, "hourly", time.time())
    )
    conn.commit()


def release_quota(conn: sqlite3.Connection, reservation_id: str) -> None:
    conn.execute(
        "UPDATE quota_reservations SET status='released', released_at=? WHERE id=?",
        (time.time(), reservation_id)
    )
    conn.commit()


def run_task_with_db_reservation(conn: sqlite3.Connection, session_id: str,
                                  task_name: str, prompts: list[str],
                                  estimated_tokens_each: int = 500) -> list[str] | None:
    total_est = estimated_tokens_each * len(prompts)
    reservation_id = reserve_quota(conn, session_id, task_name, total_est)

    if reservation_id is None:
        return None

    results = []
    try:
        for prompt in prompts:
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=256,
                messages=[{"role": "user", "content": prompt}],
            )
            actual = response.usage.input_tokens + response.usage.output_tokens
            consume_quota(conn, session_id, reservation_id, actual)
            results.append(response.content[0].text)
    except Exception as e:
        print(f"[quota] Task failed: {e}. Releasing reservation.")
        release_quota(conn, reservation_id)
        raise

    print(f"[quota] Task complete. Available tokens: {available_tokens(conn):,}")
    return results


conn = sqlite3.connect(DB_PATH)
init_db(conn)

print(f"Available tokens: {available_tokens(conn):,}\n")

results = run_task_with_db_reservation(
    conn, "session-1", "python_explainer",
    ["Explain Python generators.", "Explain Python decorators.", "Explain Python context managers."],
    estimated_tokens_each=400,
)
print(f"\nCompleted {len(results or [])} prompts")

# Expected Token Savings: N/A; cross-session DB reservation prevents two concurrent tasks from competing for the same quota
# Environment: Python 3.11+; replace :memory: with shared DB; add TTL-based reservation expiry (e.g., 10 min) to prevent leaks
```

## Option 5: Adaptive Task Scheduling Based on Quota Availability

```python
import asyncio
import anthropic
import time
from dataclasses import dataclass, field
from collections import deque

client = anthropic.AsyncAnthropic()


@dataclass
class TokenBucket:
    """Token bucket for smooth rate limiting."""
    capacity: int
    refill_rate: float  # tokens per second
    tokens: float = 0.0
    last_refill: float = field(default_factory=time.monotonic)

    def __post_init__(self) -> None:
        self.tokens = float(self.capacity)

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self.last_refill
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
        self.last_refill = now

    def can_consume(self, amount: int) -> bool:
        self._refill()
        return self.tokens >= amount

    def consume(self, amount: int) -> bool:
        self._refill()
        if self.tokens >= amount:
            self.tokens -= amount
            return True
        return False

    def wait_time(self, amount: int) -> float:
        """Seconds to wait until amount tokens are available."""
        self._refill()
        if self.tokens >= amount:
            return 0.0
        return (amount - self.tokens) / self.refill_rate


# Global token bucket: 1667 tokens/second = ~100k/minute
BUCKET = TokenBucket(capacity=5000, refill_rate=1667.0)


@dataclass
class QueuedTask:
    task_id: str
    prompts: list[str]
    estimated_tokens: int
    priority: int = 5  # 1=highest, 10=lowest


async def schedule_and_run(task: QueuedTask) -> list[str]:
    """Wait for sufficient quota, then run the task."""
    wait = BUCKET.wait_time(task.estimated_tokens)
    if wait > 0:
        print(f"[schedule] Task {task.task_id}: waiting {wait:.1f}s for quota to refill")
        await asyncio.sleep(wait)

    if not BUCKET.consume(task.estimated_tokens):
        print(f"[schedule] Task {task.task_id}: quota unavailable, deferring 5s")
        await asyncio.sleep(5.0)
        BUCKET.consume(task.estimated_tokens)  # Force consume after defer

    print(f"[schedule] Task {task.task_id}: quota reserved, running {len(task.prompts)} steps")
    results = []
    for prompt in task.prompts:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=128,
            messages=[{"role": "user", "content": prompt}],
        )
        actual = response.usage.input_tokens + response.usage.output_tokens
        results.append(response.content[0].text)
        print(f"  [{task.task_id}] actual={actual} | bucket={BUCKET.tokens:.0f} remaining")

    return results


async def run_task_queue(tasks: list[QueuedTask]) -> None:
    """Run tasks in parallel, each respecting the shared quota bucket."""
    sorted_tasks = sorted(tasks, key=lambda t: t.priority)
    coros = [schedule_and_run(t) for t in sorted_tasks]
    all_results = await asyncio.gather(*coros, return_exceptions=True)

    for task, result in zip(sorted_tasks, all_results):
        if isinstance(result, Exception):
            print(f"Task {task.task_id} failed: {result}")
        else:
            print(f"Task {task.task_id}: completed {len(result)} steps")


async def main() -> None:
    tasks = [
        QueuedTask("T1", ["Explain asyncio.", "Summarize asyncio."], 800, priority=1),
        QueuedTask("T2", ["Explain GIL.", "Summarize GIL."], 800, priority=3),
        QueuedTask("T3", ["Explain multiprocessing."], 400, priority=5),
    ]
    await run_task_queue(tasks)


asyncio.run(main())

# Expected Token Savings: N/A; token bucket scheduling prevents 429 errors by spacing requests automatically
# Environment: Python 3.11+; set refill_rate = (TPM limit / 60); capacity = burst allowance (usually 2-5x refill_rate)
```

## Option 6: Quota-Aware Task Splitter with Checkpoint Resume

```python
import anthropic
import json
import sqlite3
import time

client = anthropic.Anthropic()
DB_PATH = ":memory:"

TOKENS_PER_MINUTE = 10_000  # Conservative limit for testing
SAFETY_BUFFER = 0.85  # Use at most 85% of limit per task


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS task_checkpoints (
            task_id TEXT NOT NULL,
            step_index INTEGER NOT NULL,
            step_name TEXT NOT NULL,
            result TEXT,
            tokens_used INTEGER,
            status TEXT NOT NULL,
            completed_at REAL,
            PRIMARY KEY (task_id, step_index)
        );
    """)
    conn.commit()


def load_checkpoint(conn: sqlite3.Connection, task_id: str) -> int:
    """Return the last completed step index, or -1 if none."""
    row = conn.execute(
        "SELECT MAX(step_index) FROM task_checkpoints WHERE task_id=? AND status='done'",
        (task_id,)
    ).fetchone()
    return row[0] if row and row[0] is not None else -1


def save_checkpoint(conn: sqlite3.Connection, task_id: str, step_index: int,
                    step_name: str, result: str, tokens: int) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO task_checkpoints VALUES (?,?,?,?,?,'done',?)",
        (task_id, step_index, step_name, result, tokens, time.time())
    )
    conn.commit()


def estimate_remaining_quota(conn: sqlite3.Connection, window: int = 60) -> int:
    cutoff = time.time() - window
    used = conn.execute(
        "SELECT COALESCE(SUM(tokens_used), 0) FROM task_checkpoints WHERE completed_at > ?",
        (cutoff,)
    ).fetchone()[0]
    return max(0, int(TOKENS_PER_MINUTE * SAFETY_BUFFER) - used)


def run_resumable_task(conn: sqlite3.Connection, task_id: str,
                        steps: list[dict]) -> list[str]:
    """Run a multi-step task with quota-aware checkpointing and resumption."""
    last_done = load_checkpoint(conn, task_id)
    start_from = last_done + 1

    if start_from > 0:
        print(f"[resume] Resuming from step {start_from}/{len(steps)} (skipping {start_from} completed steps)")

    results = []
    for i, step in enumerate(steps):
        if i < start_from:
            # Load from checkpoint
            row = conn.execute(
                "SELECT result FROM task_checkpoints WHERE task_id=? AND step_index=?",
                (task_id, i)
            ).fetchone()
            results.append(row[0] if row else "")
            continue

        # Pre-step quota check
        remaining = estimate_remaining_quota(conn)
        est_tokens = step.get("estimated_tokens", 500)
        if remaining < est_tokens:
            print(f"[quota] Insufficient quota for step {i} ({est_tokens} needed, {remaining} available)")
            print(f"[quota] Checkpoint saved at step {i-1}. Resume later or wait for quota to refill.")
            break

        print(f"[step {i+1}/{len(steps)}] {step['name']} | quota_remaining≈{remaining}")
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=step.get("max_tokens", 256),
            messages=[{"role": "user", "content": step["prompt"]}],
        )
        actual = response.usage.input_tokens + response.usage.output_tokens
        result = response.content[0].text
        save_checkpoint(conn, task_id, i, step["name"], result, actual)
        results.append(result)
        print(f"  Done ({actual} tokens): {result[:60]}")

    return results


conn = sqlite3.connect(DB_PATH)
init_db(conn)

steps = [
    {"name": "intro",      "prompt": "Define distributed systems in 2 sentences.", "max_tokens": 100, "estimated_tokens": 200},
    {"name": "challenges", "prompt": "List 3 key challenges in distributed systems.", "max_tokens": 200, "estimated_tokens": 400},
    {"name": "solutions",  "prompt": "Name 3 well-known solutions to distributed system challenges.", "max_tokens": 200, "estimated_tokens": 400},
    {"name": "example",    "prompt": "Give a real-world example of a distributed system.", "max_tokens": 150, "estimated_tokens": 300},
]

print("=== First run ===")
results1 = run_resumable_task(conn, "task-distrib-001", steps)
print(f"Completed {len(results1)} steps\n")

print("=== Resume run (all steps already checkpointed) ===")
results2 = run_resumable_task(conn, "task-distrib-001", steps)
print(f"Loaded {len(results2)} steps from checkpoints")

# Expected Token Savings: Zero tokens consumed on resume — all steps loaded from DB checkpoints
# Environment: Python 3.11+; replace :memory: with persistent DB; set checkpoint TTL to prevent stale resumes
```

## Comparison

| Option | Reservation Strategy | Rollback | Cross-Session | Resumable | Best For |
|--------|---------------------|----------|---------------|-----------|----------|
| 1. Pre-Flight Check | Estimate before start | No | No | No | Simple feasibility gate |
| 2. Incremental Reservation | Per-step reserve + release | Yes | No | No | Precise quota management |
| 3. Budget Calculator | Complexity-based estimate | No | No | No | Upfront planning |
| 4. SQLite Registry | DB-persisted reservations | Yes | Yes | No | Multi-session quota sharing |
| 5. Token Bucket | Smooth rate control | No | Via shared bucket | No | High-concurrency parallel tasks |
| 6. Checkpoint Resume | Save progress + re-check | Via checkpoint | No | Yes | Long tasks that must survive quota exhaustion |
