---
layout: solution
title: "Agent Leaks asyncio Tasks Without Keeping References"
category: concurrency
description: "asyncio.create_task() calls are not stored anywhere; Python's garbage collector cancels the tasks mid-execution, causing silent work loss with no exception raised."
tags: [concurrency, asyncio, memory, reliability, production]
---

## Symptom

The agent fires off background work with `asyncio.create_task(do_something())` but the task occasionally vanishes mid-run — no exception, no log line, just missing output. Under load or after a GC cycle the tasks are cancelled silently. The agent continues as if the work completed, downstream state is inconsistent, and the bug is nearly impossible to reproduce in a debugger.

## Root Cause

CPython's garbage collector can collect a `Task` object if no live reference points to it. `asyncio.create_task()` returns a `Task`, but if the caller discards that return value (`asyncio.create_task(coro())` with no assignment), the only reference is the weak reference held by the event loop. A GC cycle can collect the task before it finishes, causing the event loop to cancel it and log a terse warning: `Task was destroyed but it is pending!` — easily missed in production logs.

## Fix

### Option 1 — Store tasks in a module-level set

```python
import asyncio
import anthropic

client = anthropic.AsyncAnthropic()

# Module-level set keeps a strong reference to every live task
_background_tasks: set[asyncio.Task] = set()

def fire_and_track(coro) -> asyncio.Task:
    """Create a task, store it, and auto-remove when done."""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task

async def summarise(item_id: int) -> None:
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{"role": "user", "content": f"Summarise item {item_id} in one sentence."}],
    )
    print(f"[task] item {item_id}: {response.content[0].text[:60]}")

async def main():
    # Fire 20 tasks — all are tracked, none will be GC'd
    for i in range(20):
        fire_and_track(summarise(i))

    # Wait for all tracked tasks to finish
    if _background_tasks:
        await asyncio.gather(*_background_tasks, return_exceptions=True)

    print(f"[main] all tasks done; leaked refs: {len(_background_tasks)}")

asyncio.run(main())
```

**Expected Token Savings:** Every API call completes; no silent retries due to GC-cancelled tasks that were mid-flight when the model response arrived.
**Environment:** Any asyncio agent that uses fire-and-forget background tasks; minimum viable fix with no structural changes.

---

### Option 2 — TaskGroup for structured concurrency (Python 3.11+)

```python
import asyncio
import anthropic

client = anthropic.AsyncAnthropic()

async def analyse_topic(topic: str) -> dict:
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": f"Analyse the topic: {topic}"}],
    )
    return {"topic": topic, "analysis": response.content[0].text}

async def main(topics: list[str]) -> list[dict]:
    results: list[dict] = []

    # TaskGroup guarantees all tasks complete (or all are cancelled on first error)
    # No manual reference tracking needed — the group holds references
    async with asyncio.TaskGroup() as tg:
        task_handles = [tg.create_task(analyse_topic(t)) for t in topics]

    # All tasks are guaranteed done here — TaskGroup raised if any failed
    results = [t.result() for t in task_handles]
    print(f"[tg] completed {len(results)} analyses")
    return results

topics = ["quantum computing", "distributed systems", "prompt engineering"]
results = asyncio.run(main(topics))
for r in results:
    print(f"  {r['topic']}: {r['analysis'][:60]}")
```

**Expected Token Savings:** TaskGroup makes the concurrency structure explicit and exception-safe; no orphaned tasks means no wasted token spend from partial results that must be re-fetched.
**Environment:** Python 3.11+; preferred pattern for any bounded set of concurrent Claude calls.

---

### Option 3 — Task registry with cancellation and timeout support

```python
import asyncio
import anthropic
from typing import Callable, Coroutine, Any

client = anthropic.AsyncAnthropic()

class TaskRegistry:
    """Tracks all live tasks; supports named lookup, timeout, and bulk cancel."""

    def __init__(self):
        self._tasks: dict[str, asyncio.Task] = {}

    def create(self, name: str, coro: Coroutine, timeout: float | None = None) -> asyncio.Task:
        if name in self._tasks and not self._tasks[name].done():
            raise ValueError(f"Task '{name}' is already running")

        async def _wrapper():
            if timeout:
                return await asyncio.wait_for(coro, timeout=timeout)
            return await coro

        task = asyncio.create_task(_wrapper(), name=name)
        self._tasks[name] = task
        task.add_done_callback(lambda t: self._tasks.pop(name, None))
        return task

    async def wait_all(self) -> list:
        if not self._tasks:
            return []
        done, _ = await asyncio.wait(self._tasks.values())
        results = []
        for task in done:
            try:
                results.append(task.result())
            except Exception as e:
                print(f"[registry] task failed: {e}")
                results.append(None)
        return results

    async def cancel_all(self) -> None:
        for task in list(self._tasks.values()):
            task.cancel()
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        print(f"[registry] cancelled all tasks")

    @property
    def active_count(self) -> int:
        return len(self._tasks)

registry = TaskRegistry()

async def fetch_insight(topic: str) -> str:
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{"role": "user", "content": f"Give one insight about {topic}."}],
    )
    return response.content[0].text

async def main():
    topics = ["AI safety", "tokenomics", "vector databases", "agent memory"]
    for topic in topics:
        registry.create(f"insight-{topic}", fetch_insight(topic), timeout=30.0)

    print(f"[main] {registry.active_count} tasks running")
    results = await registry.wait_all()
    print(f"[main] {len([r for r in results if r])} tasks succeeded")

asyncio.run(main())
```

**Expected Token Savings:** Named registry prevents duplicate tasks (same prompt fired twice); timeout prevents runaway API calls from blocking the event loop indefinitely.
**Environment:** Long-running agents that spawn named background tasks; useful when tasks need to be individually monitored or cancelled.

---

### Option 4 — Weak-reference watchdog that alerts on leaked tasks

```python
import asyncio
import weakref
import anthropic

client = anthropic.AsyncAnthropic()

_all_tasks: set[asyncio.Task] = set()
_leaked_count = 0

def create_tracked_task(coro, name: str | None = None) -> asyncio.Task:
    """Create a task with leak detection via done-callback."""
    task = asyncio.create_task(coro, name=name)
    _all_tasks.add(task)

    def on_done(t: asyncio.Task):
        _all_tasks.discard(t)
        if t.cancelled():
            global _leaked_count
            _leaked_count += 1
            print(f"[watchdog] WARNING: task '{t.get_name()}' was cancelled — possible leak!")
        elif t.exception():
            print(f"[watchdog] task '{t.get_name()}' raised: {t.exception()}")

    task.add_done_callback(on_done)
    return task

async def process_item(item_id: int) -> str:
    await asyncio.sleep(0.01)  # simulate async work
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        messages=[{"role": "user", "content": f"One word for item {item_id}."}],
    )
    return response.content[0].text.strip()

async def main():
    tasks = [create_tracked_task(process_item(i), name=f"item-{i}") for i in range(10)]

    # Simulate a leak: cancel one task early
    tasks[3].cancel()

    results = await asyncio.gather(*tasks, return_exceptions=True)
    ok = [r for r in results if isinstance(r, str)]
    print(f"[main] {len(ok)}/10 tasks completed; {_leaked_count} leaked")

asyncio.run(main())
```

**Expected Token Savings:** Watchdog surfaces leaks in staging before they cause production token waste; cancelled mid-flight tasks are identified and can be retried.
**Environment:** Development and staging environments; CI/CD pipelines that run agent integration tests.

---

### Option 5 — Semaphore-bounded task pool with reference tracking

```python
import asyncio
import anthropic

client = anthropic.AsyncAnthropic()

class BoundedTaskPool:
    """
    Runs at most `max_concurrent` tasks at once.
    Keeps strong references to all live tasks.
    """

    def __init__(self, max_concurrent: int = 5):
        self._sem = asyncio.Semaphore(max_concurrent)
        self._tasks: set[asyncio.Task] = set()

    def submit(self, coro) -> asyncio.Task:
        async def _guarded():
            async with self._sem:
                return await coro

        task = asyncio.create_task(_guarded())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def join(self) -> list:
        """Wait for all pending tasks and collect results."""
        if not self._tasks:
            return []
        results = await asyncio.gather(*list(self._tasks), return_exceptions=True)
        return [r for r in results if not isinstance(r, Exception)]

async def call_claude(prompt: str) -> str:
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text

async def main():
    pool = BoundedTaskPool(max_concurrent=3)

    prompts = [f"Name one use case for topic {i}." for i in range(15)]
    for p in prompts:
        pool.submit(call_claude(p))

    print(f"[pool] {len(pool._tasks)} tasks queued (max 3 concurrent)")
    results = await pool.join()
    print(f"[pool] {len(results)} results collected")

asyncio.run(main())
```

**Expected Token Savings:** Bounded concurrency prevents API rate-limit hits; tracked references ensure all 15 prompts are answered, not just the ones the GC hadn't collected yet.
**Environment:** Batch processing agents that fan out many Claude calls; replaces ad-hoc `asyncio.create_task()` calls with a managed pool.

---

### Option 6 — Asyncio shield for non-cancellable critical tasks

```python
import asyncio
import anthropic

client = anthropic.AsyncAnthropic()

_critical_tasks: set[asyncio.Task] = set()

async def commit_result(item_id: int, result: str) -> None:
    """Simulate a critical write that must not be cancelled mid-flight."""
    await asyncio.sleep(0.05)  # e.g., database write
    print(f"[commit] item {item_id} persisted: {result[:40]}")

async def process_with_shield(item_id: int) -> str:
    """
    Run Claude API call normally; shield the commit so cancellation
    of the outer task does not interrupt the critical write.
    """
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{"role": "user", "content": f"Classify item {item_id}: urgent/normal/low."}],
    )
    result = response.content[0].text.strip()

    # asyncio.shield keeps the inner coroutine running even if this task is cancelled
    commit_task = asyncio.ensure_future(commit_result(item_id, result))
    _critical_tasks.add(commit_task)
    commit_task.add_done_callback(_critical_tasks.discard)

    await asyncio.shield(commit_task)
    return result

async def main():
    tasks = [asyncio.create_task(process_with_shield(i)) for i in range(8)]
    # Track all tasks — no fire-and-forget
    results = await asyncio.gather(*tasks, return_exceptions=True)
    ok = [r for r in results if isinstance(r, str)]
    print(f"[main] {len(ok)}/8 processed; {len(_critical_tasks)} commits still in flight")
    if _critical_tasks:
        await asyncio.gather(*_critical_tasks, return_exceptions=True)
    print("[main] all commits flushed")

asyncio.run(main())
```

**Expected Token Savings:** Shield prevents re-issuing the same Claude API call after a commit write was interrupted; ensures idempotency even when parent tasks are cancelled under load.
**Environment:** Agents with critical side effects (DB writes, webhook notifications) that must survive task cancellation.

---

## Comparison

| Option | Reference Tracking | Handles Cancellation | Bounded Concurrency | Leak Detection | Best For |
|---|---|---|---|---|---|
| 1. Set + discard | Module set | Via done-callback | No | No | Minimal fix; fire-and-forget tasks |
| 2. TaskGroup | Implicit (group owns) | Propagated | No | Yes (raises) | Python 3.11+; structured concurrency |
| 3. Task registry | Named dict | Bulk cancel | No | Via timeout | Named tasks; individual monitoring |
| 4. Watchdog | Set + callback | Detected + logged | No | Yes | Dev/staging; CI leak detection |
| 5. Bounded pool | Set + discard | Via gather | Yes (semaphore) | No | Rate-sensitive batch processing |
| 6. Shield | Set + discard | Partial (shield inner) | No | No | Critical writes that must not be interrupted |
