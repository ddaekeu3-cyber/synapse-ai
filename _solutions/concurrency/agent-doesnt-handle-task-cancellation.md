---
layout: solution
title: "Agent Doesn't Handle Task Cancellation"
category: concurrency
description: "Cancelling a parent task leaves child coroutines running in the background, leaking API calls, file handles, and database connections."
tags: [concurrency, asyncio, cancellation, resource-leak, reliability]
---

## Symptom

A user cancels a long-running agent task — or a timeout fires — but the agent continues making API calls in the background. Orphaned coroutines consume rate-limit quota, hold open database transactions, and write partial results to shared state. The process accumulates ghost tasks until it runs out of memory or file descriptors.

## Root Cause

`asyncio.Task.cancel()` raises `CancelledError` in the coroutine at its next `await` point. If the coroutine catches all exceptions with a bare `except:` clause, or uses `asyncio.shield()` without intent, the cancellation is swallowed and the task continues. Child tasks spawned with `asyncio.create_task()` are not automatically cancelled when the parent is cancelled — they must be cancelled explicitly.

## Fix

### Option 1 — Propagate CancelledError: never swallow it

```python
import asyncio
import anthropic

client = anthropic.AsyncAnthropic()

async def ask(prompt: str) -> str:
    try:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text
    except asyncio.CancelledError:
        print("[task] API call cancelled — cleaning up")
        raise  # CRITICAL: always re-raise CancelledError

async def multi_step_task(steps: list[str]) -> list[str]:
    results = []
    for step in steps:
        try:
            result = await ask(step)
            results.append(result)
        except asyncio.CancelledError:
            print(f"[task] cancelled after {len(results)}/{len(steps)} steps")
            raise  # propagate upward
    return results

async def main():
    task = asyncio.create_task(
        multi_step_task([f"Explain concept {i}" for i in range(10)])
    )
    # Cancel after 1 second
    await asyncio.sleep(1)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        print("[main] task was cancelled cleanly")

asyncio.run(main())
```

**Expected Token Savings:** Cancelled tasks stop immediately at the next await; no orphaned API calls after cancellation.
**Environment:** Any async agent; this is the baseline — `CancelledError` must never be swallowed.

---

### Option 2 — Cancel child tasks explicitly in a TaskGroup

```python
import asyncio
import anthropic

client = anthropic.AsyncAnthropic()

async def fetch_answer(sem: asyncio.Semaphore, prompt: str) -> str:
    async with sem:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=128,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text

async def parallel_research(topics: list[str], timeout: float = 3.0) -> list[str]:
    sem     = asyncio.Semaphore(3)
    results = []

    try:
        # Python 3.11+ TaskGroup cancels all child tasks if one raises or the group is cancelled
        async with asyncio.timeout(timeout):
            async with asyncio.TaskGroup() as tg:
                tasks = [tg.create_task(fetch_answer(sem, f"Briefly explain: {t}")) for t in topics]

        results = [t.result() for t in tasks if not t.cancelled()]

    except* asyncio.CancelledError:
        print("[research] cancelled — TaskGroup cancelled all children")
    except TimeoutError:
        print("[research] timed out — TaskGroup cancelled all children automatically")
    except* Exception as eg:
        print(f"[research] errors in {len(eg.exceptions)} task(s)")

    return results

async def main():
    topics = ["quantum mechanics", "black holes", "CRISPR", "blockchain", "relativity"]
    results = await parallel_research(topics, timeout=5.0)
    print(f"Got {len(results)} results")
    for r in results:
        print(f"  {r[:80]}")

asyncio.run(main())
```

**Expected Token Savings:** TaskGroup cancels all children on timeout — no orphaned API calls run past the deadline.
**Environment:** Python 3.11+ parallel agent tasks; TaskGroup is the modern replacement for manual gather + cancel.

---

### Option 3 — Manual child-task registry with cleanup

```python
import asyncio
import anthropic

client = anthropic.AsyncAnthropic()

class TaskRegistry:
    """Tracks all spawned tasks and cancels them on shutdown."""

    def __init__(self):
        self._tasks: set[asyncio.Task] = set()

    def spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def cancel_all(self) -> None:
        if not self._tasks:
            return
        print(f"[registry] cancelling {len(self._tasks)} task(s)")
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

registry = TaskRegistry()

async def background_worker(worker_id: int) -> None:
    try:
        for i in range(10):
            response = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=64,
                messages=[{"role": "user", "content": f"Worker {worker_id}, step {i}"}],
            )
            print(f"[worker-{worker_id}] step {i}: {response.content[0].text[:40]}")
            await asyncio.sleep(0.1)
    except asyncio.CancelledError:
        print(f"[worker-{worker_id}] cancelled cleanly")
        raise

async def main():
    for i in range(4):
        registry.spawn(background_worker(i))

    await asyncio.sleep(1.5)
    print("[main] shutting down")
    await registry.cancel_all()
    print("[main] all tasks cancelled")

asyncio.run(main())
```

**Expected Token Savings:** Registry ensures no task is forgotten on shutdown; all in-flight API calls are cancelled together.
**Environment:** Long-running agents with dynamic task spawning; pairs with graceful shutdown handling.

---

### Option 4 — Timeout per step with partial result preservation

```python
import asyncio
import anthropic

client = anthropic.AsyncAnthropic()

STEP_TIMEOUT = 5.0   # seconds per individual step

async def timed_ask(prompt: str) -> str | None:
    try:
        async with asyncio.timeout(STEP_TIMEOUT):
            response = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=256,
                messages=[{"role": "user", "content": prompt}],
            )
            return response.content[0].text
    except TimeoutError:
        print(f"[timeout] step timed out after {STEP_TIMEOUT}s: {prompt[:50]!r}")
        return None
    except asyncio.CancelledError:
        print(f"[cancelled] step cancelled: {prompt[:50]!r}")
        raise

async def run_pipeline(steps: list[str]) -> dict:
    completed: list[dict] = []
    skipped:   list[str]  = []

    for i, step in enumerate(steps):
        result = await timed_ask(step)
        if result is not None:
            completed.append({"step": i, "prompt": step, "result": result})
        else:
            skipped.append(step)

    return {
        "completed": len(completed),
        "skipped":   len(skipped),
        "results":   completed,
    }

async def main():
    steps = [
        "Name the capital of France.",
        "List three programming languages.",
        "What is 100 * 42?",
        "Describe the water cycle in one sentence.",
        "Name a famous physicist.",
    ]
    summary = await run_pipeline(steps)
    print(f"Completed: {summary['completed']}/{summary['completed'] + summary['skipped']}")
    for r in summary["results"]:
        print(f"  step {r['step']}: {r['result'][:60]}")

asyncio.run(main())
```

**Expected Token Savings:** Per-step timeout limits runaway costs; completed steps are preserved, so no work is wasted on timeout.
**Environment:** Multi-step pipelines where individual steps may hang; preserves partial results for checkpointing.

---

### Option 5 — Cooperative cancellation via an event flag

```python
import asyncio
import anthropic

client = anthropic.AsyncAnthropic()

class CancellableAgent:
    def __init__(self):
        self._cancel_event = asyncio.Event()
        self._results: list[str] = []

    def request_cancel(self) -> None:
        print("[agent] cancellation requested")
        self._cancel_event.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel_event.is_set()

    async def run(self, prompts: list[str]) -> list[str]:
        for i, prompt in enumerate(prompts):
            if self.cancelled:
                print(f"[agent] stopping at step {i} (cooperative cancellation)")
                break

            try:
                response = await client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=128,
                    messages=[{"role": "user", "content": prompt}],
                )
                self._results.append(response.content[0].text)
                print(f"[agent] step {i} done")
            except asyncio.CancelledError:
                print(f"[agent] hard-cancelled at step {i}")
                raise

        return self._results

async def main():
    agent   = CancellableAgent()
    prompts = [f"Tell me about topic {i}" for i in range(20)]

    run_task = asyncio.create_task(agent.run(prompts))

    # Simulate external cancellation request after 2 steps
    await asyncio.sleep(0.5)
    agent.request_cancel()

    results = await run_task
    print(f"Completed {len(results)} steps before cancellation")

asyncio.run(main())
```

**Expected Token Savings:** Cooperative cancellation completes the current API call before stopping — no wasted partial calls; stops cleanly between steps.
**Environment:** Long-running agents where abrupt cancellation would waste the in-progress call; event-based cancel is gentler than Task.cancel().

---

### Option 6 — Context manager for scoped task lifecycle

```python
import asyncio
import contextlib
import anthropic

client = anthropic.AsyncAnthropic()

@contextlib.asynccontextmanager
async def task_scope():
    """Context manager that cancels all tasks created within its scope on exit."""
    tasks: list[asyncio.Task] = []
    _original_create = asyncio.create_task

    def tracked_create(coro, *, name=None):
        task = _original_create(coro, name=name)
        tasks.append(task)
        return task

    # Monkey-patch create_task within scope
    asyncio.create_task = tracked_create
    try:
        yield tasks
    finally:
        asyncio.create_task = _original_create
        alive = [t for t in tasks if not t.done()]
        if alive:
            print(f"[scope] cancelling {len(alive)} task(s) on scope exit")
            for t in alive:
                t.cancel()
            await asyncio.gather(*alive, return_exceptions=True)

async def worker(worker_id: int) -> None:
    try:
        for step in range(5):
            resp = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=64,
                messages=[{"role": "user", "content": f"Worker {worker_id} step {step}"}],
            )
            print(f"[w{worker_id}] {resp.content[0].text[:40]}")
            await asyncio.sleep(0.2)
    except asyncio.CancelledError:
        print(f"[w{worker_id}] cancelled")
        raise

async def main():
    async with task_scope() as tasks:
        for i in range(3):
            asyncio.create_task(worker(i))
        # Scope exits after 1 second — all tasks cancelled automatically
        await asyncio.sleep(1.0)
    print(f"[main] scope exited, {len([t for t in tasks if t.cancelled()])} tasks were cancelled")

asyncio.run(main())
```

**Expected Token Savings:** Scoped lifecycle guarantees all child tasks are cancelled when the scope exits, regardless of how exit occurs (normal, exception, or timeout).
**Environment:** Test harnesses, request handlers, or any scoped operation where all child work must terminate with the scope.

---

## Comparison

| Option | Cancellation Trigger | Child Task Handling | Partial Results | Best For |
|---|---|---|---|---|
| 1. Re-raise CancelledError | External cancel | Manual | No | Baseline — every coroutine must do this |
| 2. TaskGroup | Timeout / any failure | Automatic | Filtered | Python 3.11+ parallel tasks |
| 3. Task registry | External cancel | Explicit bulk cancel | No | Dynamic task spawning |
| 4. Per-step timeout | Timeout per step | N/A (sequential) | Yes | Multi-step pipelines with hangs |
| 5. Event flag | Cooperative request | N/A | Yes | Between-step cancellation |
| 6. Context manager | Scope exit | Automatic | No | Scoped operations, tests |
