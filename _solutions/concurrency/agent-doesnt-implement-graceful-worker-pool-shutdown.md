---
layout: solution
title: "Agent Doesn't Implement Graceful Worker Pool Shutdown"
category: concurrency
description: "Agent worker pool is killed abruptly on SIGTERM — in-flight tasks are lost, partially written files are corrupted, and external API calls are abandoned mid-stream, leaving downstream systems in inconsistent state."
tags: [concurrency, shutdown, worker-pool, reliability, signal-handling]
---

## Symptom

On deployment or restart, the agent process receives SIGTERM and dies immediately:

```
[14:32:01] SIGTERM received
[14:32:01] Process killed
[14:32:02] ERROR: 12 tasks were in-flight at shutdown
[14:32:02] ERROR: output/report-2026-04-15.csv — file truncated at row 847
[14:32:02] ERROR: Stripe charge ch_xyz — charge created but webhook not confirmed
```

Tasks mid-execution are abandoned with no record of their state.

## Root Cause

The worker pool has no signal handler — `asyncio.run()` or `threading.Thread` are started without any teardown logic:

```python
import asyncio
import anthropic

client = anthropic.AsyncAnthropic(api_key="sk-live-...")

async def process_task(task: dict) -> str:
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": task["prompt"]}]
    )
    return response.content[0].text

async def main():
    tasks = [{"prompt": f"Task {i}"} for i in range(100)]
    # No signal handler — SIGTERM kills instantly, all in-flight tasks lost
    await asyncio.gather(*[process_task(t) for t in tasks])

asyncio.run(main())
```

---

## Fix

### Option 1 — SIGTERM handler with asyncio cancellation and drain

Register a SIGTERM handler that cancels new work and waits for in-flight tasks to complete.

```python
import asyncio
import signal
import anthropic

client = anthropic.AsyncAnthropic(api_key="sk-live-...")

_shutdown_event = asyncio.Event()
_in_flight: set[asyncio.Task] = set()


def handle_sigterm(signum, frame):
    print("[shutdown] SIGTERM received — draining in-flight tasks...")
    _shutdown_event.set()


async def process_task(task_id: int) -> str:
    task = asyncio.current_task()
    _in_flight.add(task)
    try:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=[{"role": "user", "content": f"Process task {task_id}"}]
        )
        return response.content[0].text
    finally:
        _in_flight.discard(task)


async def worker_pool(task_ids: list[int], concurrency: int = 5) -> list[str]:
    sem = asyncio.Semaphore(concurrency)
    results = []

    async def bounded_task(tid: int) -> str | None:
        if _shutdown_event.is_set():
            print(f"[shutdown] Skipping task {tid} — shutdown in progress")
            return None
        async with sem:
            return await process_task(tid)

    tasks = [asyncio.create_task(bounded_task(tid)) for tid in task_ids]

    # Wait for all tasks or shutdown signal
    done, pending = await asyncio.wait(
        tasks,
        return_when=asyncio.ALL_COMPLETED,
    )

    for t in done:
        result = t.result()
        if result is not None:
            results.append(result)

    return results


async def main():
    signal.signal(signal.SIGTERM, handle_sigterm)
    signal.signal(signal.SIGINT, handle_sigterm)

    task_ids = list(range(1, 51))
    results = await worker_pool(task_ids)
    print(f"Completed {len(results)} tasks before shutdown")

asyncio.run(main())

# Expected Token Savings: in-flight tasks complete → no duplicate API calls on restart
# Environment: long-running async agent processes deployed via Kubernetes or systemd
```

---

### Option 2 — Context manager worker pool with guaranteed drain

Wrap the worker pool in an async context manager. `__aexit__` always drains before releasing.

```python
import asyncio
import signal
import anthropic
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic(api_key="sk-live-...")


@dataclass
class WorkerPool:
    concurrency: int = 5
    _sem: asyncio.Semaphore = field(init=False)
    _tasks: set[asyncio.Task] = field(default_factory=set, init=False)
    _stopping: bool = field(default=False, init=False)

    def __post_init__(self):
        self._sem = asyncio.Semaphore(self.concurrency)

    async def submit(self, coro) -> asyncio.Task:
        if self._stopping:
            raise RuntimeError("Worker pool is shutting down")

        async def wrapper():
            async with self._sem:
                return await coro

        task = asyncio.create_task(wrapper())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def shutdown(self, cancel_pending: bool = False) -> None:
        self._stopping = True
        if self._tasks:
            print(f"[pool] Draining {len(self._tasks)} in-flight tasks...")
            if cancel_pending:
                for t in list(self._tasks):
                    t.cancel()
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
        print("[pool] All tasks drained.")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.shutdown()


async def process(task_id: int) -> str:
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=50,
        messages=[{"role": "user", "content": f"Task {task_id}: reply OK"}]
    )
    return response.content[0].text


async def main():
    loop = asyncio.get_running_loop()

    async with WorkerPool(concurrency=5) as pool:
        # Install shutdown handler that triggers pool drain
        def on_signal():
            asyncio.create_task(pool.shutdown())

        loop.add_signal_handler(signal.SIGTERM, on_signal)
        loop.add_signal_handler(signal.SIGINT, on_signal)

        tasks = [await pool.submit(process(i)) for i in range(20)]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        print(f"Completed: {sum(1 for r in results if isinstance(r, str))}")

asyncio.run(main())

# Expected Token Savings: drain guarantees all in-flight calls complete → no wasted retries
# Environment: agents with async context managers and structured concurrency
```

---

### Option 3 — Thread pool with join on shutdown

For CPU-bound or sync workers using `concurrent.futures.ThreadPoolExecutor`, call `executor.shutdown(wait=True)` in the signal handler.

```python
import signal
import threading
import concurrent.futures
import anthropic
import time

client = anthropic.Anthropic(api_key="sk-live-...")

_executor = concurrent.futures.ThreadPoolExecutor(max_workers=5)
_shutdown_event = threading.Event()
_futures: list[concurrent.futures.Future] = []
_futures_lock = threading.Lock()


def process_task(task_id: int) -> str:
    if _shutdown_event.is_set():
        return f"task-{task_id}: skipped (shutdown)"

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=50,
        messages=[{"role": "user", "content": f"Process task {task_id}"}]
    )
    return response.content[0].text


def graceful_shutdown(signum, frame):
    print(f"\n[shutdown] Signal {signum} — stopping new submissions, waiting for {len(_futures)} tasks...")
    _shutdown_event.set()

    # Wait for all submitted futures to complete
    done, not_done = concurrent.futures.wait(
        _futures,
        timeout=30.0,  # Hard timeout after 30s
        return_when=concurrent.futures.ALL_COMPLETED,
    )

    if not_done:
        print(f"[shutdown] {len(not_done)} tasks timed out — cancelling")
        for f in not_done:
            f.cancel()

    _executor.shutdown(wait=False)
    print(f"[shutdown] Drain complete. {len(done)} tasks finished.")
    raise SystemExit(0)


signal.signal(signal.SIGTERM, graceful_shutdown)
signal.signal(signal.SIGINT, graceful_shutdown)


def main():
    for task_id in range(30):
        if _shutdown_event.is_set():
            break
        future = _executor.submit(process_task, task_id)
        with _futures_lock:
            _futures.append(future)

    # Wait for all futures
    for f in concurrent.futures.as_completed(_futures):
        try:
            result = f.result()
            print(f"Done: {result[:40]}")
        except Exception as e:
            print(f"Error: {e}")


main()

# Expected Token Savings: thread workers complete API calls → no duplicate charges on external services
# Environment: sync agents using ThreadPoolExecutor for parallel tool execution
```

---

### Option 4 — Checkpoint completed tasks before shutdown

Before shutting down, write completed task IDs to a checkpoint file. On restart, skip already-completed tasks.

```python
import asyncio
import signal
import json
from pathlib import Path
import anthropic

client = anthropic.AsyncAnthropic(api_key="sk-live-...")

CHECKPOINT_FILE = Path(".worker_checkpoint.json")
_shutdown_event = asyncio.Event()


def load_checkpoint() -> set[int]:
    if CHECKPOINT_FILE.exists():
        data = json.loads(CHECKPOINT_FILE.read_text())
        completed = set(data.get("completed", []))
        print(f"[checkpoint] Resuming — {len(completed)} tasks already done")
        return completed
    return set()


def save_checkpoint(completed: set[int]) -> None:
    CHECKPOINT_FILE.write_text(json.dumps({"completed": sorted(completed)}))


async def process_task(task_id: int, completed: set[int]) -> int | None:
    if task_id in completed:
        return None  # Already done — skip

    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=50,
        messages=[{"role": "user", "content": f"Process task {task_id}"}]
    )
    _ = response.content[0].text

    completed.add(task_id)
    save_checkpoint(completed)  # Persist after each completion
    return task_id


async def main():
    loop = asyncio.get_running_loop()
    completed = load_checkpoint()

    def on_signal():
        print("[shutdown] Signal received — saving checkpoint and draining...")
        _shutdown_event.set()

    loop.add_signal_handler(signal.SIGTERM, on_signal)
    loop.add_signal_handler(signal.SIGINT, on_signal)

    task_ids = list(range(1, 101))
    sem = asyncio.Semaphore(5)

    async def bounded(tid: int):
        if _shutdown_event.is_set():
            return None
        async with sem:
            return await process_task(tid, completed)

    tasks = [asyncio.create_task(bounded(tid)) for tid in task_ids if tid not in completed]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    done = sum(1 for r in results if isinstance(r, int))
    print(f"[checkpoint] Session complete: {done} new tasks, {len(completed)} total")

    if len(completed) == len(task_ids):
        CHECKPOINT_FILE.unlink(missing_ok=True)  # All done — clean up

asyncio.run(main())

# Expected Token Savings: on restart, skip already-completed tasks — no redundant API calls
# Environment: large batch jobs that may need multiple runs to complete
```

---

### Option 5 — FastAPI lifespan with structured shutdown

For FastAPI-based agents, use the `lifespan` context manager to drain the worker pool before the process exits.

```python
import asyncio
import anthropic
from contextlib import asynccontextmanager
from fastapi import FastAPI, BackgroundTasks
from fastapi.responses import JSONResponse

client = anthropic.AsyncAnthropic(api_key="sk-live-...")

_active_tasks: set[asyncio.Task] = set()
_accepting = True


async def run_agent_task(prompt: str) -> str:
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}]
    )
    return response.content[0].text


def track_task(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _active_tasks.add(task)
    task.add_done_callback(_active_tasks.discard)
    return task


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _accepting
    print("[lifespan] Worker pool started")
    yield
    # Shutdown phase: stop accepting, drain in-flight tasks
    _accepting = False
    if _active_tasks:
        print(f"[lifespan] Draining {len(_active_tasks)} active tasks...")
        await asyncio.gather(*list(_active_tasks), return_exceptions=True)
    print("[lifespan] All tasks drained — process exiting cleanly")


app = FastAPI(lifespan=lifespan)


@app.post("/process")
async def process_endpoint(body: dict):
    if not _accepting:
        return JSONResponse({"error": "Server shutting down"}, status_code=503)

    prompt = body.get("prompt", "")
    task = track_task(run_agent_task(prompt))

    # Wait for result
    result = await task
    return {"result": result}


@app.get("/health")
def health():
    return {"status": "ok" if _accepting else "draining", "active_tasks": len(_active_tasks)}

# Run with: uvicorn app:app
# SIGTERM triggers lifespan __aexit__ — Uvicorn handles this automatically

# Expected Token Savings: lifespan drain ensures all API calls complete → no wasted tokens
# Environment: FastAPI agents deployed via Docker/Kubernetes with graceful termination periods
```

---

### Option 6 — Retry-safe shutdown: mark tasks as "in-progress" before starting

Write each task to a "running" store before starting, and "completed" after finishing. On restart, resume "running" tasks (they may need retry).

```python
import asyncio
import signal
import json
from pathlib import Path
from enum import StrEnum
import anthropic

client = anthropic.AsyncAnthropic(api_key="sk-live-...")

STATE_FILE = Path(".task_state.json")


class TaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


def load_state() -> dict[str, dict]:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


def update_task(state: dict, task_id: str, status: TaskStatus, result: str = "") -> None:
    state[task_id] = {"status": status, "result": result}
    save_state(state)


async def process_task(task_id: str, prompt: str, state: dict) -> str:
    update_task(state, task_id, TaskStatus.RUNNING)
    try:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=100,
            messages=[{"role": "user", "content": prompt}]
        )
        result = response.content[0].text
        update_task(state, task_id, TaskStatus.COMPLETED, result)
        return result
    except Exception as e:
        update_task(state, task_id, TaskStatus.FAILED, str(e))
        raise


async def main():
    loop = asyncio.get_running_loop()
    shutdown_event = asyncio.Event()
    loop.add_signal_handler(signal.SIGTERM, shutdown_event.set)
    loop.add_signal_handler(signal.SIGINT, shutdown_event.set)

    state = load_state()

    # Identify tasks to run (pending or previously-running tasks from crashed session)
    all_tasks = {f"task-{i}": f"Prompt for task {i}" for i in range(20)}
    to_run = {
        tid: prompt for tid, prompt in all_tasks.items()
        if state.get(tid, {}).get("status") not in (TaskStatus.COMPLETED,)
    }

    running_count = sum(1 for s in state.values() if s.get("status") == TaskStatus.RUNNING)
    if running_count:
        print(f"[recovery] {running_count} tasks were interrupted last session — will retry")

    sem = asyncio.Semaphore(5)
    active: set[asyncio.Task] = set()

    async def bounded(tid: str, prompt: str):
        if shutdown_event.is_set():
            return
        async with sem:
            await process_task(tid, prompt, state)

    for tid, prompt in to_run.items():
        if shutdown_event.is_set():
            break
        task = asyncio.create_task(bounded(tid, prompt))
        active.add(task)
        task.add_done_callback(active.discard)

    await asyncio.gather(*list(active), return_exceptions=True)

    completed = sum(1 for s in state.values() if s.get("status") == TaskStatus.COMPLETED)
    print(f"Done: {completed}/{len(all_tasks)} tasks completed")

asyncio.run(main())

# Expected Token Savings: tasks interrupted during shutdown are retried on restart → no silent data loss
# Environment: mission-critical batch agents where every task must eventually complete
```

---

## Comparison

| Option | In-Flight Protection | Restart-Safe | Hard Timeout | FastAPI | Complexity |
|--------|---------------------|--------------|--------------|---------|------------|
| 1 | Yes (event + wait) | No | No | No | Low |
| 2 | Yes (context manager) | No | No | No | Medium |
| 3 | Yes (thread join) | No | Yes (30s) | No | Low |
| 4 | Yes + checkpoint | Yes (skip done) | No | No | Medium |
| 5 | Yes (lifespan) | No | No | Yes | Low |
| 6 | Yes + state machine | Yes (retry interrupted) | No | No | Medium |

**Recommended starting point:** Option 1 for async agents — add the SIGTERM handler and `asyncio.wait()` drain in under 20 lines. Option 5 for FastAPI agents — Uvicorn calls lifespan `__aexit__` automatically on SIGTERM. Add Option 4's checkpoint for large batch jobs that can't afford to restart from scratch.
