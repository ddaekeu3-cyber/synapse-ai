---
layout: solution
title: "Agent Doesn't Propagate Cancellation to Subagents"
category: concurrency
description: "When the orchestrator is cancelled or times out, spawned subagents continue running in the background, wasting tokens and quota."
tags: [concurrency, cancellation, subagents, asyncio, cleanup, resource-leak]
---

## Symptom

Orchestrator cancels but background subagents keep running:

```python
async def orchestrate(user_query: str):
    # Spawns 5 parallel subagent calls
    tasks = [
        asyncio.create_task(subagent_call(f"subtask_{i}", user_query))
        for i in range(5)
    ]
    # User cancels after 3 seconds
    result = await asyncio.gather(*tasks)
    return result

# User presses Ctrl+C or request times out after 3s:
# asyncio.CancelledError raised in orchestrate()
# BUT: the 5 subagent_call tasks continue running in background
# Each still consuming API quota, tokens, and time
# No cleanup: connections left open, partial results discarded silently
```

After the orchestrator exits, 5 orphaned API calls continue to completion, consuming full token quota with no one to receive the results.

## Root Cause

`asyncio.create_task()` decouples the child task from the parent coroutine. When the parent is cancelled, `CancelledError` is raised in the parent but not propagated to tasks that were already scheduled. The tasks run to completion unless explicitly cancelled. Most agent orchestrators don't implement cleanup handlers, so cancellation of the top-level coroutine silently orphans all in-flight work.

## Fix

---

### Option 1: Task Group with Automatic Cancellation on First Failure

Use `asyncio.TaskGroup` (Python 3.11+) which cancels all sibling tasks when any task raises an exception, including `CancelledError`.

```python
import asyncio
import anthropic

client = anthropic.AsyncAnthropic()

async def subagent_call(task_id: str, prompt: str) -> str:
    """Single subagent LLM call."""
    print(f"[{task_id}] Starting...")
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}],
    )
    print(f"[{task_id}] Done.")
    return response.content[0].text

async def orchestrate_with_task_group(query: str) -> list[str]:
    """TaskGroup cancels all siblings if any task is cancelled or raises."""
    results = []
    try:
        async with asyncio.TaskGroup() as tg:
            tasks = [
                tg.create_task(subagent_call(f"sub_{i}", f"{query} — part {i}"))
                for i in range(5)
            ]
        results = [t.result() for t in tasks]
    except* asyncio.CancelledError:
        print("Orchestrator cancelled — all subagents cancelled by TaskGroup")
        raise
    except* Exception as eg:
        print(f"Subagent errors: {eg.exceptions}")
    return results

async def main():
    try:
        # Simulate timeout after 2 seconds
        result = await asyncio.wait_for(
            orchestrate_with_task_group("Analyse quarterly revenue"),
            timeout=2.0,
        )
        print(f"Got {len(result)} results")
    except asyncio.TimeoutError:
        print("Timed out — all subagents were cancelled by TaskGroup")

asyncio.run(main())
```

**Expected Token Savings:** On cancellation, TaskGroup immediately cancels all in-flight tasks. For 5 concurrent 1,000-token calls cancelled at 50% completion, saves ~2,500 tokens. For batch jobs with 50+ subagents, saves tens of thousands of tokens per cancelled run.
**Environment:** Requires Python 3.11+. Best choice for new code — zero manual cleanup needed.

---

### Option 2: CancellationToken Pattern — Cooperative Cancellation Across Processes

For subagents that run in separate processes or across network boundaries, use a shared cancellation token (flag) that subagents poll periodically.

```python
import asyncio
import anthropic
from dataclasses import dataclass, field

@dataclass
class CancellationToken:
    _cancelled: bool = False
    _reason: str = ""

    def cancel(self, reason: str = ""):
        self._cancelled = True
        self._reason = reason

    @property
    def is_cancelled(self) -> bool:
        return self._cancelled

    @property
    def reason(self) -> str:
        return self._reason

    def raise_if_cancelled(self):
        if self._cancelled:
            raise asyncio.CancelledError(self._reason)

client = anthropic.AsyncAnthropic()

async def subagent_with_token(
    task_id: str,
    prompt: str,
    cancel_token: CancellationToken,
    checkpoint_interval: int = 1,
) -> str | None:
    """Subagent that checks cancellation token before each API call."""
    cancel_token.raise_if_cancelled()
    print(f"[{task_id}] Starting")

    # Multi-step subagent: checks token between steps
    for step in range(3):
        cancel_token.raise_if_cancelled()  # Check before each step
        print(f"[{task_id}] Step {step}")

        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=128,
            messages=[{"role": "user", "content": f"{prompt} (step {step})"}],
        )
        await asyncio.sleep(0)  # yield to event loop
        cancel_token.raise_if_cancelled()  # Check after each step

    return response.content[0].text

async def orchestrate_with_token(query: str, timeout: float = 5.0) -> list[str]:
    cancel_token = CancellationToken()
    tasks = [
        asyncio.create_task(
            subagent_with_token(f"sub_{i}", f"{query} part {i}", cancel_token)
        )
        for i in range(5)
    ]

    try:
        done, pending = await asyncio.wait(tasks, timeout=timeout)
        if pending:
            # Timeout: signal all subagents to stop at next checkpoint
            cancel_token.cancel(reason="orchestrator timeout")
            for t in pending:
                t.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            print(f"Cancelled {len(pending)} subagents after timeout")

        return [t.result() for t in done if not t.cancelled() and not t.exception()]
    except Exception:
        cancel_token.cancel(reason="orchestrator error")
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise

asyncio.run(orchestrate_with_token("Summarise reports", timeout=3.0))
```

**Expected Token Savings:** Cooperative cancellation stops subagents at the next checkpoint (between steps), not mid-call. Avoids completing expensive in-progress API calls unnecessarily. Saves all tokens from steps that hadn't started yet.
**Environment:** Works across process/thread boundaries if token is stored in shared memory or Redis. Checkpoint interval is a trade-off: finer = faster cancellation, coarser = fewer overhead checks.

---

### Option 3: Structured Cleanup with `contextlib.AsyncExitStack`

Register cleanup handlers for each subagent resource so cancellation always triggers proper teardown, even if it occurs mid-setup.

```python
import asyncio
import contextlib
import anthropic
from dataclasses import dataclass

@dataclass
class SubagentHandle:
    task_id: str
    task: asyncio.Task
    started_at: float
    tokens_used: int = 0

async def cleanup_subagent(handle: SubagentHandle):
    """Cancel a subagent and wait for it to finish cleanly."""
    if not handle.task.done():
        handle.task.cancel()
        try:
            await handle.task
        except (asyncio.CancelledError, Exception):
            pass
    import time
    elapsed = time.monotonic() - handle.started_at
    print(f"[{handle.task_id}] Cleaned up after {elapsed:.2f}s, tokens: {handle.tokens_used}")

client = anthropic.AsyncAnthropic()

async def subagent_call(task_id: str, prompt: str) -> tuple[str, int]:
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}],
    )
    tokens = response.usage.input_tokens + response.usage.output_tokens
    return response.content[0].text, tokens

async def orchestrate_with_cleanup(query: str, timeout: float = 10.0) -> list[str]:
    import time
    handles: list[SubagentHandle] = []

    async with contextlib.AsyncExitStack() as stack:
        # Register global cancellation cleanup
        async def cancel_all():
            print(f"Cleaning up {len(handles)} subagents...")
            await asyncio.gather(*(cleanup_subagent(h) for h in handles), return_exceptions=True)

        stack.push_async_callback(cancel_all)

        # Spawn subagents
        for i in range(5):
            task = asyncio.create_task(subagent_call(f"sub_{i}", f"{query} section {i}"))
            handle = SubagentHandle(task_id=f"sub_{i}", task=task, started_at=time.monotonic())
            handles.append(handle)

        try:
            # Wait with timeout
            done, pending = await asyncio.wait(
                [h.task for h in handles],
                timeout=timeout,
            )

            results = []
            for handle in handles:
                if handle.task in done and not handle.task.exception():
                    text, tokens = handle.task.result()
                    handle.tokens_used = tokens
                    results.append(text)
            return results

        except asyncio.CancelledError:
            print("Orchestrator cancelled")
            raise
        # AsyncExitStack.cancel_all() fires here in all exit paths

async def main():
    try:
        results = await asyncio.wait_for(
            orchestrate_with_cleanup("Analyse market segments", timeout=10.0),
            timeout=3.0,
        )
        print(f"Results: {len(results)}")
    except asyncio.TimeoutError:
        print("Overall timeout — all subagents cleaned up via exit stack")

asyncio.run(main())
```

**Expected Token Savings:** `AsyncExitStack` guarantees cleanup runs even on unexpected exceptions or cancellation. Prevents resource leaks (open HTTP connections, streaming responses) that accumulate token charges. Log output provides visibility into actual token consumption per subagent.
**Environment:** Works in Python 3.10+. Exit stack pattern is composable — add other resource cleanups (database connections, file handles) alongside subagent cancellation.

---

### Option 4: Nursery Pattern — Structured Concurrency with `anyio`

Use `anyio`'s nursery, which enforces that all child tasks finish (or are cancelled) before the nursery exits, matching the Trio structured concurrency model.

```python
import anyio  # pip install anyio
import anthropic
from anyio import create_task_group, move_on_after

client = anthropic.AsyncAnthropic()

async def subagent_call(task_id: str, prompt: str, results: list) -> None:
    try:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=[{"role": "user", "content": prompt}],
        )
        results.append((task_id, response.content[0].text))
        print(f"[{task_id}] Completed")
    except anyio.get_cancelled_exc_class():
        print(f"[{task_id}] Cancelled by nursery")
        raise

async def orchestrate_with_nursery(query: str, timeout: float = 5.0) -> list[tuple[str, str]]:
    results: list[tuple[str, str]] = []

    # move_on_after: cancels entire nursery (and all children) after timeout
    with move_on_after(timeout) as cancel_scope:
        async with create_task_group() as tg:
            for i in range(5):
                tg.start_soon(
                    subagent_call,
                    f"sub_{i}",
                    f"{query} — part {i}",
                    results,
                )
        # When nursery exits (normally or cancelled), ALL tasks are done/cancelled

    if cancel_scope.cancelled_caught:
        print(f"Timeout: got {len(results)}/{5} results before cancellation")
    else:
        print(f"Completed all 5 subagents: {len(results)} results")

    return results

anyio.run(orchestrate_with_nursery, "Process customer feedback", 3.0)
```

**Expected Token Savings:** `anyio` nursery enforces structured concurrency at the library level — impossible for child tasks to outlive the nursery scope. On timeout, all 5 tasks are cancelled immediately, saving all remaining tokens from unstarted or mid-execution calls.
**Environment:** Requires `anyio` (compatible with asyncio and Trio backends). Recommended for new async agent code — structured concurrency eliminates the entire class of orphaned-task bugs.

---

### Option 5: Cancellation via `asyncio.Event` for Streaming Subagents

For subagents that use streaming responses, cancellation must interrupt the stream. Use a shared `asyncio.Event` to signal mid-stream abort.

```python
import asyncio
import anthropic

client = anthropic.AsyncAnthropic()

async def streaming_subagent(
    task_id: str,
    prompt: str,
    cancel_event: asyncio.Event,
) -> str:
    """Stream response and abort if cancel_event is set."""
    collected = []
    try:
        async with client.messages.stream(
            model="claude-haiku-4-5-20251001",
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            async for text in stream.text_stream:
                if cancel_event.is_set():
                    print(f"[{task_id}] Stream aborted at {len(collected)} chunks")
                    break  # Exits stream cleanly
                collected.append(text)
                await asyncio.sleep(0)  # yield to event loop
    except asyncio.CancelledError:
        print(f"[{task_id}] Task cancelled mid-stream")
        raise

    return "".join(collected)

async def orchestrate_streaming(query: str, timeout: float = 3.0) -> list[str]:
    cancel_event = asyncio.Event()
    tasks = [
        asyncio.create_task(
            streaming_subagent(f"sub_{i}", f"{query} angle {i}", cancel_event)
        )
        for i in range(4)
    ]

    async def cancel_after(delay: float):
        await asyncio.sleep(delay)
        print("Timeout reached — setting cancel event")
        cancel_event.set()
        # Give streams 0.5s to finish current chunk cleanly
        await asyncio.sleep(0.5)
        for t in tasks:
            if not t.done():
                t.cancel()

    timeout_task = asyncio.create_task(cancel_after(timeout))

    try:
        results = await asyncio.gather(*tasks, return_exceptions=True)
        timeout_task.cancel()
        return [r for r in results if isinstance(r, str)]
    except Exception:
        cancel_event.set()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise

asyncio.run(orchestrate_streaming("Write creative variations on", timeout=2.0))
```

**Expected Token Savings:** Streaming with mid-stream abort stops billing at the point of cancellation (Anthropic bills for tokens streamed, not max_tokens). For a 500-token response cancelled at 50%, saves ~250 tokens per subagent. With 4 concurrent streams, saves ~1,000 tokens per cancellation event.
**Environment:** Works with `anthropic` streaming API. The cancel_event approach is gentler than task.cancel() — allows the current chunk to complete before aborting, preventing malformed partial outputs.

---

### Option 6: Hierarchical Cancellation Registry — Track All Spawned Tasks

Maintain a central registry of all live tasks so any component can cancel the entire tree, including deeply nested subagents.

```python
import asyncio
import weakref
from contextlib import asynccontextmanager
import anthropic

class TaskRegistry:
    """Central registry of all live tasks; supports hierarchical cancellation."""

    def __init__(self):
        self._tasks: dict[str, weakref.ref[asyncio.Task]] = {}
        self._parent_map: dict[str, str] = {}  # child_id → parent_id
        self._lock = asyncio.Lock()

    async def register(self, task_id: str, task: asyncio.Task, parent_id: str | None = None):
        async with self._lock:
            self._tasks[task_id] = weakref.ref(task)
            if parent_id:
                self._parent_map[task_id] = parent_id
        task.add_done_callback(lambda t: asyncio.create_task(self._unregister(task_id)))

    async def _unregister(self, task_id: str):
        async with self._lock:
            self._tasks.pop(task_id, None)
            self._parent_map.pop(task_id, None)

    async def cancel_subtree(self, root_id: str):
        """Cancel root and all descendants."""
        async with self._lock:
            # Find all descendants
            to_cancel = {root_id}
            children = {c for c, p in self._parent_map.items() if p in to_cancel}
            while children:
                to_cancel |= children
                children = {c for c, p in self._parent_map.items() if p in to_cancel - children}

        for task_id in to_cancel:
            ref = self._tasks.get(task_id)
            task = ref() if ref else None
            if task and not task.done():
                task.cancel()
                print(f"Cancelled: {task_id}")

registry = TaskRegistry()
client = anthropic.AsyncAnthropic()

@asynccontextmanager
async def managed_task(task_id: str, coro, parent_id: str | None = None):
    task = asyncio.create_task(coro)
    await registry.register(task_id, task, parent_id)
    try:
        yield task
    except asyncio.CancelledError:
        await registry.cancel_subtree(task_id)
        raise

async def leaf_subagent(task_id: str, prompt: str) -> str:
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text

async def nested_orchestrator(orch_id: str, query: str) -> list[str]:
    subtasks = []
    for i in range(3):
        child_id = f"{orch_id}.sub_{i}"
        task = asyncio.create_task(leaf_subagent(child_id, f"{query} part {i}"))
        await registry.register(child_id, task, parent_id=orch_id)
        subtasks.append(task)
    return await asyncio.gather(*subtasks)

async def main():
    root_task = asyncio.create_task(nested_orchestrator("root", "Analyse financials"))
    await registry.register("root", root_task)

    # Simulate cancellation after 1 second
    await asyncio.sleep(1.0)
    print("Cancelling root and all descendants...")
    await registry.cancel_subtree("root")
    await asyncio.gather(root_task, return_exceptions=True)
    print("All tasks cancelled")

# Comparison table
"""
| Approach | Python Version | Cross-Process | Streaming | Nested | Overhead |
|---|---|---|---|---|---|
| Option 1: TaskGroup | 3.11+ | No | No | No | None |
| Option 2: CancellationToken | Any | Yes (Redis) | Yes | Yes | Poll cost |
| Option 3: AsyncExitStack | 3.10+ | No | No | No | Minimal |
| Option 4: anyio nursery | Any+anyio | No | No | No | None |
| Option 5: Event+stream | Any | No | Yes | No | Minimal |
| Option 6: Registry | Any | No | No | Yes | Registry lock |
"""

asyncio.run(main())
```

**Expected Token Savings:** Registry ensures no orphaned tasks survive cancellation at any nesting depth. For an orchestrator with 3 levels of subagents (1 → 5 → 3 = 15 leaf tasks), a root cancellation saves all 15 in-flight API calls. At 500 tokens each, saves up to 7,500 tokens per cancellation event.
**Environment:** Weakref-based registry avoids preventing garbage collection of completed tasks. Use Redis or a database-backed registry for cross-process scenarios where subagents run in separate pods.
