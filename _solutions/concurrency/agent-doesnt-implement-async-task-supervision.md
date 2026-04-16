---
layout: solution
title: "Agent Doesn't Implement Async Task Supervision"
category: concurrency
description: "Supervise asyncio tasks so crashes are caught, logged, and recovered from automatically — preventing silent task death, propagating failures to a parent supervisor, and restarting workers within configurable retry budgets."
tags: [concurrency, asyncio, supervision, fault-tolerance, task-management, python]
---

# Agent Doesn't Implement Async Task Supervision

Agents that fire-and-forget asyncio tasks lose failures silently — a crashed tool worker, a stalled background processor, or an unhandled exception in a spawned coroutine leaves the agent in an inconsistent state with no indication anything went wrong. A supervisor catches task deaths, logs them, and decides whether to restart or escalate.

## Option 1: Simple Task Supervisor with Exception Capture

```python
import anthropic
import asyncio

client = anthropic.AsyncAnthropic()

async def supervised_task(name: str, coro, on_error=None):
    """Wrap a coroutine; catch and log any exception."""
    try:
        result = await coro
        print(f"  [DONE ] {name}")
        return result
    except asyncio.CancelledError:
        print(f"  [CANCEL] {name}")
        raise
    except Exception as e:
        print(f"  [ERROR] {name}: {type(e).__name__}: {e}")
        if on_error:
            on_error(name, e)
        return None

async def model_call(prompt: str) -> str:
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text

async def broken_task():
    raise RuntimeError("Simulated worker crash")

async def slow_task():
    await asyncio.sleep(0.5)
    return "slow result"

errors: list[tuple] = []

def record_error(name: str, exc: Exception):
    errors.append((name, type(exc).__name__, str(exc)))

async def main():
    tasks = await asyncio.gather(
        supervised_task("model_call_1", model_call("What is Python?")),
        supervised_task("broken_worker", broken_task(), on_error=record_error),
        supervised_task("slow_worker",   slow_task()),
        supervised_task("model_call_2", model_call("What is asyncio?")),
        return_exceptions=False,
    )
    results = [t for t in tasks if t is not None]
    print(f"\nCompleted: {len(results)}/{len(tasks)} tasks")
    print(f"Errors recorded: {errors}")

asyncio.run(main())

# Expected Token Savings: Supervised tasks prevent silent failure loops; errors surface immediately for retry decisions
# Environment: asyncio; return_exceptions=False propagates CancelledError; wrap with gather for fan-out supervision
```

## Option 2: Supervisor with Restart Policy

```python
import anthropic
import asyncio
import time

client = anthropic.AsyncAnthropic()

class RestartPolicy:
    NEVER    = "never"
    ALWAYS   = "always"
    ON_ERROR = "on_error"

async def supervised_worker(
    name: str,
    factory,
    policy: str = RestartPolicy.ON_ERROR,
    max_restarts: int = 3,
    restart_delay: float = 0.5,
) -> list:
    """Run a worker coroutine; restart according to policy."""
    results = []
    attempts = 0
    while True:
        try:
            result = await factory()
            results.append(result)
            print(f"  [OK    ] {name} attempt={attempts+1}")
            if policy != RestartPolicy.ALWAYS:
                break
        except asyncio.CancelledError:
            print(f"  [CANCEL] {name}")
            break
        except Exception as e:
            attempts += 1
            print(f"  [FAIL  ] {name} attempt={attempts}: {e}")
            if policy == RestartPolicy.NEVER or attempts >= max_restarts:
                print(f"  [GIVE_UP] {name} after {attempts} attempts")
                break
            await asyncio.sleep(restart_delay)
    return results

# Workers
_call_count = 0

async def flaky_worker():
    global _call_count
    _call_count += 1
    if _call_count < 3:
        raise ConnectionError(f"Transient failure #{_call_count}")
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        messages=[{"role": "user", "content": "Say 'success' in one word."}],
    )
    return resp.content[0].text.strip()

async def stable_worker():
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        messages=[{"role": "user", "content": "What is 2+2?"}],
    )
    return resp.content[0].text.strip()

async def always_fails():
    raise RuntimeError("Permanent failure")

async def main():
    results = await asyncio.gather(
        supervised_worker("flaky",  flaky_worker, policy=RestartPolicy.ON_ERROR, max_restarts=3),
        supervised_worker("stable", stable_worker, policy=RestartPolicy.NEVER),
        supervised_worker("broken", always_fails, policy=RestartPolicy.ON_ERROR, max_restarts=2),
    )
    for name, res in zip(["flaky", "stable", "broken"], results):
        print(f"  {name}: {res}")

asyncio.run(main())

# Expected Token Savings: ON_ERROR restart recovers transient failures without human intervention; NEVER skips wasted retries
# Environment: asyncio; tune restart_delay with exponential backoff for production; combine with circuit breaker
```

## Option 3: Hierarchical Supervisor Tree

```python
import anthropic
import asyncio
from dataclasses import dataclass, field
from typing import Callable, Awaitable

client = anthropic.AsyncAnthropic()

@dataclass
class SupervisorNode:
    name: str
    children: list["SupervisorNode"] = field(default_factory=list)
    tasks: list[Callable[[], Awaitable]] = field(default_factory=list)
    max_child_failures: int = 1  # escalate to parent after N child failures

async def run_supervisor(node: SupervisorNode, depth: int = 0) -> dict:
    indent = "  " * depth
    print(f"{indent}[SUP] {node.name} starting {len(node.tasks)} tasks, {len(node.children)} children")
    failures = 0
    results = {"name": node.name, "ok": [], "failed": [], "children": []}

    # Run direct tasks
    for task_fn in node.tasks:
        try:
            result = await task_fn()
            results["ok"].append(str(result)[:40])
            print(f"{indent}  ✓ {task_fn.__name__}: {str(result)[:40]!r}")
        except Exception as e:
            failures += 1
            results["failed"].append(f"{task_fn.__name__}: {e}")
            print(f"{indent}  ✗ {task_fn.__name__}: {e}")
            if failures > node.max_child_failures:
                print(f"{indent}  [ESCALATE] {node.name} exceeded failure budget")
                raise RuntimeError(f"Supervisor {node.name} failed: {failures} failures")

    # Run child supervisors
    for child in node.children:
        try:
            child_result = await run_supervisor(child, depth + 1)
            results["children"].append(child_result)
        except Exception as e:
            failures += 1
            print(f"{indent}  [CHILD FAIL] {child.name}: {e}")

    return results

# Build task functions
async def ask(q: str) -> str:
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        messages=[{"role": "user", "content": q}],
    )
    return resp.content[0].text.strip()

async def crash(): raise ValueError("Worker crashed")
async def task_a(): return await ask("What is Python?")
async def task_b(): return await ask("What is asyncio?")
async def task_c(): raise RuntimeError("Tool unavailable")
async def task_d(): return await ask("What is 2+2?")

# Supervisor tree
root = SupervisorNode(
    name="root",
    max_child_failures=2,
    children=[
        SupervisorNode("model-workers", tasks=[task_a, task_b], max_child_failures=1),
        SupervisorNode("tool-workers",  tasks=[task_c, task_d], max_child_failures=1),
    ],
)

async def main():
    result = await run_supervisor(root)
    print(f"\nRoot ok={len(result['ok'])} failed={len(result['failed'])}")

asyncio.run(main())

# Expected Token Savings: Tree isolation contains failures; model-workers and tool-workers fail independently
# Environment: asyncio; depth limits prevent infinite supervisor recursion; add Circuit Breaker at leaf level
```

## Option 4: Supervisor with Health Dashboard and SQLite Audit

```python
import anthropic
import asyncio
import sqlite3
import time
from dataclasses import dataclass, field
from enum import Enum

client = anthropic.AsyncAnthropic()
DB = "task_supervisor.db"

class TaskState(Enum):
    PENDING  = "pending"
    RUNNING  = "running"
    SUCCESS  = "success"
    FAILED   = "failed"
    RESTARTED = "restarted"

def init_db():
    con = sqlite3.connect(DB)
    con.execute("""
        CREATE TABLE IF NOT EXISTS task_log (
            task_id TEXT, name TEXT, state TEXT,
            attempt INTEGER, error TEXT, duration_ms REAL, ts REAL
        )
    """)
    con.commit(); con.close()

def log_task(task_id: str, name: str, state: TaskState,
             attempt: int = 1, error: str = "", duration_ms: float = 0.0):
    con = sqlite3.connect(DB)
    con.execute(
        "INSERT INTO task_log VALUES (?,?,?,?,?,?,?)",
        (task_id, name, state.value, attempt, error, duration_ms, time.time()),
    )
    con.commit(); con.close()

async def supervised_with_audit(
    task_id: str, name: str, coro_fn,
    max_attempts: int = 2,
) -> dict:
    init_db()
    for attempt in range(1, max_attempts + 1):
        log_task(task_id, name, TaskState.RUNNING, attempt)
        t0 = time.monotonic()
        try:
            result = await coro_fn()
            dur = (time.monotonic() - t0) * 1000
            log_task(task_id, name, TaskState.SUCCESS, attempt, duration_ms=dur)
            return {"task_id": task_id, "name": name, "result": result,
                    "attempts": attempt, "success": True}
        except asyncio.CancelledError:
            dur = (time.monotonic() - t0) * 1000
            log_task(task_id, name, TaskState.FAILED, attempt, "CancelledError", dur)
            raise
        except Exception as e:
            dur = (time.monotonic() - t0) * 1000
            state = TaskState.RESTARTED if attempt < max_attempts else TaskState.FAILED
            log_task(task_id, name, state, attempt, str(e), dur)
            if attempt < max_attempts:
                await asyncio.sleep(0.1 * attempt)
    return {"task_id": task_id, "name": name, "result": None,
            "attempts": max_attempts, "success": False}

def dashboard() -> list[dict]:
    con = sqlite3.connect(DB)
    rows = con.execute("""
        SELECT name, state, COUNT(*) cnt, ROUND(AVG(duration_ms),1) avg_ms
        FROM task_log GROUP BY name, state ORDER BY name, state
    """).fetchall()
    con.close()
    return [{"name": r[0], "state": r[1], "count": r[2], "avg_ms": r[3]} for r in rows]

import uuid

async def model_query(q: str) -> str:
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        messages=[{"role": "user", "content": q}],
    )
    return resp.content[0].text.strip()

_fail_count = 0
async def flaky():
    global _fail_count
    _fail_count += 1
    if _fail_count == 1:
        raise ValueError("First attempt always fails")
    return "recovered"

async def main():
    results = await asyncio.gather(
        supervised_with_audit(str(uuid.uuid4())[:6], "query_python",  lambda: model_query("What is Python?"),  2),
        supervised_with_audit(str(uuid.uuid4())[:6], "flaky_worker",  flaky, 2),
        supervised_with_audit(str(uuid.uuid4())[:6], "always_crash",  lambda: (_ for _ in ()).throw(RuntimeError("always")), 2),
        return_exceptions=True,
    )
    for r in results:
        if isinstance(r, dict):
            print(f"  {r['name']:15s} ok={r['success']} attempts={r['attempts']}")

    print("\nDashboard:")
    for row in dashboard():
        print(f"  {row['name']:15s} [{row['state']:9s}] x{row['count']} avg={row['avg_ms']}ms")

asyncio.run(main())

# Expected Token Savings: SQLite audit reveals which tasks fail most; target retries at high-failure tasks only
# Environment: log_task is synchronous; move to asyncio SQLite library for very high throughput
```

## Option 5: Supervisor with Backoff and Jitter

```python
import anthropic
import asyncio
import random
import time

client = anthropic.AsyncAnthropic()

async def exponential_backoff(attempt: int, base: float = 0.5, cap: float = 30.0) -> float:
    """Full jitter: delay = random(0, min(cap, base * 2^attempt))"""
    delay = random.uniform(0, min(cap, base * (2 ** attempt)))
    return delay

async def supervised_with_backoff(
    name: str,
    coro_fn,
    max_attempts: int = 4,
    base_delay: float = 0.1,
) -> dict:
    last_error = None
    for attempt in range(max_attempts):
        try:
            t0 = time.monotonic()
            result = await coro_fn()
            elapsed = (time.monotonic() - t0) * 1000
            print(f"  [OK ] {name} attempt={attempt+1} ({elapsed:.0f}ms)")
            return {"success": True, "result": result, "attempts": attempt + 1}
        except asyncio.CancelledError:
            raise
        except Exception as e:
            last_error = e
            delay = await exponential_backoff(attempt, base=base_delay)
            print(f"  [ERR] {name} attempt={attempt+1}: {e} | retry in {delay:.2f}s")
            if attempt < max_attempts - 1:
                await asyncio.sleep(delay)

    return {"success": False, "result": None, "attempts": max_attempts,
            "error": str(last_error)}

# Simulate tasks with varying reliability
_counts: dict[str, int] = {}

def make_flaky(name: str, fail_until: int):
    async def task():
        _counts[name] = _counts.get(name, 0) + 1
        if _counts[name] <= fail_until:
            raise ConnectionError(f"Not ready yet (attempt {_counts[name]})")
        resp = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=64,
            messages=[{"role": "user", "content": f"Say 'ok' for task {name}."}],
        )
        return resp.content[0].text.strip()
    task.__name__ = name
    return task

async def main():
    tasks = [
        supervised_with_backoff("task_a", make_flaky("task_a", 0), max_attempts=3),  # succeeds immediately
        supervised_with_backoff("task_b", make_flaky("task_b", 2), max_attempts=4),  # fails twice then ok
        supervised_with_backoff("task_c", make_flaky("task_c", 9), max_attempts=3),  # always fails (9 > 3)
    ]
    results = await asyncio.gather(*tasks)
    for r in results:
        print(f"  success={r['success']} attempts={r['attempts']} "
              f"result={str(r.get('result',''))[:30]!r}")

asyncio.run(main())

# Expected Token Savings: Jitter spreads retries; no thundering herd after transient API outages
# Environment: base_delay=0.1 for dev; 1.0+ for production API rate-limit recovery
```

## Option 6: Actor-Style Task Supervisor with Mailbox

```python
import anthropic
import asyncio
from dataclasses import dataclass, field
from typing import Any

client = anthropic.AsyncAnthropic()

@dataclass
class Message:
    type: str
    payload: Any = None
    reply_to: asyncio.Queue | None = None

class SupervisedActor:
    """Actor with a supervised mailbox — crashes restart the actor, mailbox survives."""
    def __init__(self, name: str, handler, max_restarts: int = 3):
        self.name = name
        self._handler = handler
        self._mailbox: asyncio.Queue[Message | None] = asyncio.Queue()
        self._max_restarts = max_restarts
        self._restarts = 0
        self._task: asyncio.Task | None = None

    def start(self):
        self._task = asyncio.create_task(self._run())
        return self

    async def send(self, msg_type: str, payload=None) -> Any:
        """Send a message and await a reply."""
        reply_q: asyncio.Queue = asyncio.Queue()
        await self._mailbox.put(Message(msg_type, payload, reply_q))
        return await reply_q.get()

    async def stop(self):
        await self._mailbox.put(None)
        if self._task:
            await self._task

    async def _run(self):
        print(f"  [ACTOR] {self.name} started")
        while True:
            msg = await self._mailbox.get()
            if msg is None:
                print(f"  [ACTOR] {self.name} stopped")
                break
            try:
                result = await self._handler(msg.type, msg.payload)
                if msg.reply_to:
                    await msg.reply_to.put(result)
            except Exception as e:
                print(f"  [CRASH] {self.name}: {e}")
                if msg.reply_to:
                    await msg.reply_to.put(f"ERROR: {e}")
                self._restarts += 1
                if self._restarts > self._max_restarts:
                    print(f"  [DEAD ] {self.name}: exceeded restart budget")
                    break
                print(f"  [RESTART] {self.name} ({self._restarts}/{self._max_restarts})")

async def model_actor_handler(msg_type: str, payload: Any) -> str:
    if msg_type == "ask":
        resp = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=64,
            messages=[{"role": "user", "content": str(payload)}],
        )
        return resp.content[0].text.strip()
    elif msg_type == "crash":
        raise RuntimeError("Intentional crash")
    return f"Unknown: {msg_type}"

async def main():
    actor = SupervisedActor("model-worker", model_actor_handler, max_restarts=2).start()

    r1 = await actor.send("ask", "What is Python?")
    print(f"  Reply 1: {r1[:60]!r}")

    r2 = await actor.send("crash")
    print(f"  Reply 2 (after crash): {r2!r}")

    r3 = await actor.send("ask", "What is 2+2?")
    print(f"  Reply 3 (post-restart): {r3[:60]!r}")

    await actor.stop()
    print(f"  Total restarts: {actor._restarts}")

asyncio.run(main())

# Expected Token Savings: Actor mailbox survives crashes; no request loss on restart unlike naive task recreation
# Environment: asyncio; scale to N actors with a supervisor pool; add dead-letter queue for failed messages
```

## Comparison

| Option | Restart Policy | Failure Propagation | Audit Log | Backoff |
|--------|---------------|--------------------|-----------|----|
| 1 — Simple Capture | None | on_error callback | List | No |
| 2 — Restart Policy | NEVER/ON_ERROR/ALWAYS | Exception propagation | No | Fixed delay |
| 3 — Supervisor Tree | Per-node budget | Escalate to parent | No | No |
| 4 — Audited Supervisor | Configurable | Return dict | SQLite | Linear |
| 5 — Backoff + Jitter | Max attempts | Return dict | No | Exponential + jitter |
| 6 — Actor + Mailbox | Max restarts | Error reply | No | No |
