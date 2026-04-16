---
layout: solution
title: "Agent Doesn't Implement Structured Concurrency Scope"
category: concurrency
description: "Use structured concurrency to give every spawned task a well-defined lifetime — tasks cannot outlive their scope, cancellation propagates reliably, and resources are always cleaned up when a scope exits, whether normally or via exception."
tags: [concurrency, asyncio, structured-concurrency, task-management, lifecycle, python]
---

# Agent Doesn't Implement Structured Concurrency Scope

Agents that spawn tasks with `asyncio.create_task()` without a governing scope create orphan tasks — tasks that outlive their parent, hold open connections, consume API quota after the request completes, or silently fail with no handler. Structured concurrency scopes give every task a defined lifetime, guaranteed cleanup, and reliable cancellation propagation.

## Option 1: TaskGroup as Scope Boundary (Python 3.11+)

```python
import anthropic
import asyncio

client = anthropic.AsyncAnthropic()

async def fetch_answer(question: str) -> dict:
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=100,
        messages=[{"role": "user", "content": question}],
    )
    return {"question": question, "answer": resp.content[0].text}

async def run_parallel_questions(questions: list[str]) -> list[dict]:
    """
    TaskGroup guarantees:
    - All tasks complete before the scope exits
    - If any task raises, all others are cancelled
    - No orphan tasks can escape the scope
    """
    results = []
    async with asyncio.TaskGroup() as tg:
        tasks = [tg.create_task(fetch_answer(q)) for q in questions]
    # Here, ALL tasks are done — no need to await individually
    results = [t.result() for t in tasks]
    return results

async def main():
    questions = [
        "What is Python?",
        "What is asyncio?",
        "What is a TaskGroup?",
    ]
    print(f"Running {len(questions)} questions in TaskGroup scope...")
    results = await run_parallel_questions(questions)
    for r in results:
        print(f"  Q: {r['question'][:40]!r}")
        print(f"  A: {r['answer'][:60]!r}\n")

asyncio.run(main())

# Expected Token Savings: TaskGroup prevents duplicate API calls from orphan tasks; clean scope = no resource leaks
# Environment: asyncio.TaskGroup requires Python 3.11+; use asyncio.gather for Python 3.9-3.10
```

## Option 2: Manual Scope with Cleanup Guarantee via Context Manager

```python
import anthropic
import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator

client = anthropic.AsyncAnthropic()

class TaskScope:
    """Structured concurrency scope: all tasks cancelled on __aexit__."""
    def __init__(self, name: str = "scope"):
        self.name  = name
        self._tasks: list[asyncio.Task] = []

    def spawn(self, coro, name: str = "") -> asyncio.Task:
        task = asyncio.create_task(coro, name=name or self.name)
        self._tasks.append(task)
        return task

    async def wait_all(self) -> list:
        if not self._tasks:
            return []
        done, pending = await asyncio.wait(self._tasks, return_when=asyncio.ALL_COMPLETED)
        return [t.result() for t in done if not t.exception()]

    async def cancel_all(self):
        for task in self._tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if exc_type is not None:
            print(f"  [SCOPE] {self.name} exiting with error — cancelling {len(self._tasks)} tasks")
        await self.cancel_all()
        return False  # don't suppress exceptions

async def model_call(label: str, prompt: str) -> dict:
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=80,
        messages=[{"role": "user", "content": prompt}],
    )
    return {"label": label, "result": resp.content[0].text}

async def main():
    # Happy path — all tasks complete
    print("=== Happy path ===")
    async with TaskScope("research") as scope:
        scope.spawn(model_call("q1", "What is Python?"),    "task-q1")
        scope.spawn(model_call("q2", "What is asyncio?"),   "task-q2")
        scope.spawn(model_call("q3", "What is SQLite?"),    "task-q3")
        results = await scope.wait_all()
    print(f"  Completed: {len(results)} results")
    for r in results:
        print(f"  [{r['label']}] {r['result'][:50]!r}")

    # Error path — scope cancels everything
    print("\n=== Error path ===")
    try:
        async with TaskScope("error-scope") as scope:
            scope.spawn(model_call("q4", "What is FastAPI?"), "task-q4")
            raise RuntimeError("Simulated error mid-scope")
    except RuntimeError as e:
        print(f"  Caught: {e} — tasks were cancelled")

asyncio.run(main())

# Expected Token Savings: cancel_all on error prevents ghost API calls from completing after scope exits
# Environment: asyncio; context manager pattern works for Python 3.9+; extend with timeout per scope
```

## Option 3: Scoped Task Registry with Lifetime Tracking

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum

client = anthropic.AsyncAnthropic()

class TaskStatus(Enum):
    RUNNING   = "running"
    DONE      = "done"
    CANCELLED = "cancelled"
    FAILED    = "failed"

@dataclass
class TrackedTask:
    name: str
    task: asyncio.Task
    start_ts: float = field(default_factory=time.monotonic)
    status: TaskStatus = TaskStatus.RUNNING
    duration_ms: float = 0.0

class ScopedRegistry:
    """Track all tasks in a scope; cancel stragglers on scope exit."""
    def __init__(self):
        self._tasks: dict[str, TrackedTask] = {}

    def register(self, name: str, coro) -> asyncio.Task:
        task = asyncio.create_task(coro, name=name)
        self._tasks[name] = TrackedTask(name=name, task=task)
        task.add_done_callback(lambda t, n=name: self._on_done(t, n))
        return task

    def _on_done(self, task: asyncio.Task, name: str):
        tracked = self._tasks.get(name)
        if not tracked:
            return
        tracked.duration_ms = (time.monotonic() - tracked.start_ts) * 1000
        if task.cancelled():
            tracked.status = TaskStatus.CANCELLED
        elif task.exception():
            tracked.status = TaskStatus.FAILED
        else:
            tracked.status = TaskStatus.DONE

    async def join(self) -> dict[str, object]:
        tasks = [t.task for t in self._tasks.values()]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        return {
            name: (r if not isinstance(r, Exception) else None)
            for name, r in zip(self._tasks, results)
        }

    async def cancel_pending(self):
        for tracked in self._tasks.values():
            if tracked.status == TaskStatus.RUNNING:
                tracked.task.cancel()
        await asyncio.gather(*[t.task for t in self._tasks.values()], return_exceptions=True)

    def report(self) -> list[dict]:
        return [
            {"name": t.name, "status": t.status.value, "duration_ms": round(t.duration_ms, 1)}
            for t in self._tasks.values()
        ]

async def ask(label: str, q: str) -> str:
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        messages=[{"role": "user", "content": q}],
    )
    return resp.content[0].text

async def always_fails():
    raise ValueError("This task always fails")

async def main():
    registry = ScopedRegistry()
    registry.register("q1", ask("q1", "What is Python?"))
    registry.register("q2", ask("q2", "What is asyncio?"))
    registry.register("q3", always_fails())
    registry.register("q4", ask("q4", "What is SQLite?"))

    results = await registry.join()
    await registry.cancel_pending()

    print("Task report:")
    for row in registry.report():
        print(f"  {row['name']:4s} [{row['status']:9s}] {row['duration_ms']:6.1f}ms")
    print(f"\nSuccessful results: {sum(1 for v in results.values() if v is not None)}")

asyncio.run(main())

# Expected Token Savings: Registry surfaces failed/cancelled tasks; prevents silent loss of partial results
# Environment: asyncio; add scope nesting by passing parent registry; extend with retry for FAILED tasks
```

## Option 4: Timeout-Scoped Execution with Partial Results

```python
import anthropic
import asyncio
import time

client = anthropic.AsyncAnthropic()

async def ask_with_label(label: str, prompt: str) -> dict:
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=100,
        messages=[{"role": "user", "content": prompt}],
    )
    return {"label": label, "result": resp.content[0].text}

async def timed_scope(coros: dict[str, object], timeout_s: float) -> dict:
    """
    Run all coros within a timeout scope.
    Collect partial results — don't discard completed tasks just because some timed out.
    """
    tasks = {label: asyncio.create_task(coro, name=label)
             for label, coro in coros.items()}
    t0 = time.monotonic()
    try:
        done, pending = await asyncio.wait(
            tasks.values(),
            timeout=timeout_s,
            return_when=asyncio.ALL_COMPLETED,
        )
    except Exception:
        done, pending = set(), set(tasks.values())

    elapsed = (time.monotonic() - t0) * 1000

    # Cancel stragglers — they won't complete after scope exits
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)

    results = {}
    for label, task in tasks.items():
        if task in done and not task.exception():
            results[label] = {"result": task.result(), "timed_out": False}
        elif task.cancelled() or task in pending:
            results[label] = {"result": None, "timed_out": True}
        else:
            results[label] = {"result": None, "error": str(task.exception())}

    print(f"  Scope complete in {elapsed:.0f}ms: "
          f"{len(done)}/{len(tasks)} done, {len(pending)} timed out")
    return results

async def slow_task(label: str, delay: float) -> dict:
    await asyncio.sleep(delay)
    return {"label": label, "result": f"completed after {delay}s"}

async def main():
    coros = {
        "fast_q":  ask_with_label("fast_q", "What is Python?"),
        "fast_q2": ask_with_label("fast_q2", "What is 2+2?"),
        "slow_1":  slow_task("slow_1", 5.0),  # will time out
        "slow_2":  slow_task("slow_2", 10.0), # will time out
    }
    results = await timed_scope(coros, timeout_s=3.0)
    for label, r in results.items():
        if r["timed_out"]:
            print(f"  [{label}] TIMED OUT")
        else:
            result = r.get("result") or {}
            out = result.get("result", "error") if isinstance(result, dict) else str(result)
            print(f"  [{label}] {out[:60]!r}")

asyncio.run(main())

# Expected Token Savings: Partial results preserved; timed-out tasks cancelled before they complete and charge tokens
# Environment: asyncio.wait with timeout; pending tasks always cancelled to prevent API calls completing after scope
```

## Option 5: Nursery Pattern — Child Tasks Cannot Outlive Parent

```python
import anthropic
import asyncio
from typing import Callable, Any

client = anthropic.AsyncAnthropic()

class Nursery:
    """
    Trio-inspired nursery: child tasks cannot outlive the nursery block.
    If any child fails, remaining children are cancelled.
    """
    def __init__(self, cancel_on_first_error: bool = True):
        self._tasks: list[asyncio.Task] = []
        self._cancel_on_error = cancel_on_first_error
        self._error: Exception | None = None

    def start_soon(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.append(task)
        if self._cancel_on_error:
            task.add_done_callback(self._check_error)
        return task

    def _check_error(self, task: asyncio.Task):
        if not task.cancelled() and task.exception():
            self._error = task.exception()
            self._cancel_remaining(exclude=task)

    def _cancel_remaining(self, exclude: asyncio.Task | None = None):
        for t in self._tasks:
            if t is not exclude and not t.done():
                t.cancel()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        # Wait for all tasks; cancel if exception from parent scope
        if exc_type:
            self._cancel_remaining()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        # Re-raise first child error if parent didn't already fail
        if exc_type is None and self._error:
            raise self._error
        return False

async def ask(q: str) -> str:
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=80,
        messages=[{"role": "user", "content": q}],
    )
    return resp.content[0].text

async def always_raises():
    raise RuntimeError("Child task failed")

async def main():
    # Happy path
    print("=== All tasks succeed ===")
    async with Nursery() as nursery:
        t1 = nursery.start_soon(ask("What is Python?"))
        t2 = nursery.start_soon(ask("What is asyncio?"))
    print(f"  t1: {t1.result()[:50]!r}")
    print(f"  t2: {t2.result()[:50]!r}")

    # Error path — nursery cancels siblings when one fails
    print("\n=== One task fails ===")
    try:
        async with Nursery(cancel_on_first_error=True) as nursery:
            t3 = nursery.start_soon(ask("What is SQLite?"))
            t4 = nursery.start_soon(always_raises())
    except RuntimeError as e:
        print(f"  Nursery caught: {e}")
        print(f"  t3 cancelled: {t3.cancelled()}")

asyncio.run(main())

# Expected Token Savings: cancel_on_first_error stops sibling API calls immediately on failure; no wasted tokens
# Environment: asyncio; nursery pattern mirrors trio.Nursery; extend with exception groups for Python 3.11+
```

## Option 6: Scope with Resource Cleanup Registry

```python
import anthropic
import asyncio
from typing import Callable, Awaitable

client = anthropic.AsyncAnthropic()

class ResourceScope:
    """
    Structured scope that tracks both tasks AND resources.
    Resources (connections, file handles, etc.) are cleaned up when scope exits,
    regardless of whether tasks succeeded or failed.
    """
    def __init__(self):
        self._tasks:    list[asyncio.Task] = []
        self._cleanups: list[Callable[[], Awaitable]] = []
        self._results:  list = []

    def spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.append(task)
        return task

    def register_cleanup(self, cleanup_coro_fn: Callable):
        """Register a cleanup coroutine to run on scope exit."""
        self._cleanups.append(cleanup_coro_fn)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        # 1. Cancel any pending tasks
        for task in self._tasks:
            if not task.done():
                task.cancel()
        results = await asyncio.gather(*self._tasks, return_exceptions=True)
        self._results = [r for r in results if not isinstance(r, (Exception, type(None)))]

        # 2. Run cleanups in reverse registration order (LIFO)
        for cleanup in reversed(self._cleanups):
            try:
                await cleanup()
            except Exception as e:
                print(f"  [CLEANUP ERROR] {e}")
        return False

# Simulated resources
_open_connections = 0

async def acquire_connection(label: str) -> str:
    global _open_connections
    _open_connections += 1
    print(f"  [OPEN ] connection-{label} (total: {_open_connections})")
    return f"conn-{label}"

async def release_connection(label: str):
    global _open_connections
    _open_connections -= 1
    print(f"  [CLOSE] connection-{label} (total: {_open_connections})")

async def ask_with_resource(label: str, prompt: str, conn: str) -> str:
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        messages=[{"role": "user", "content": prompt}],
    )
    return f"[{conn}] {resp.content[0].text}"

async def main():
    print("=== Resource scope ===")
    async with ResourceScope() as scope:
        conn_a = await acquire_connection("A")
        scope.register_cleanup(lambda: release_connection("A"))

        conn_b = await acquire_connection("B")
        scope.register_cleanup(lambda: release_connection("B"))

        scope.spawn(ask_with_resource("q1", "What is Python?",  conn_a))
        scope.spawn(ask_with_resource("q2", "What is asyncio?", conn_b))
        # Wait for tasks to finish (scope will cancel any remaining on exit)
        await asyncio.gather(*scope._tasks, return_exceptions=True)

    print(f"\nOpen connections after scope: {_open_connections}")
    print(f"Results collected: {len(scope._results)}")
    for r in scope._results:
        print(f"  {r[:70]!r}")

asyncio.run(main())

# Expected Token Savings: Cleanup registry prevents connection leaks even when tasks fail; scope guarantees teardown
# Environment: asyncio; LIFO cleanup order mirrors context manager protocol; extend with SQLite session cleanup
```

## Comparison

| Option | Scope Mechanism | Cancellation | Cleanup | Partial Results |
|--------|----------------|-------------|---------|----------------|
| 1 — TaskGroup | Python 3.11 builtin | All-or-nothing | Automatic | No |
| 2 — Context Manager | Manual `__aexit__` | On error | Manual | No |
| 3 — Task Registry | Done callbacks | cancel_pending() | No | Status per task |
| 4 — Timeout Scope | asyncio.wait | Stragglers only | No | Yes |
| 5 — Nursery Pattern | Done callback | First-error | No | No |
| 6 — Resource Scope | Context manager | On exit | LIFO registry | Yes |
