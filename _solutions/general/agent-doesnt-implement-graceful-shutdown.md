---
layout: solution
title: "Agent Doesn't Implement Graceful Shutdown"
category: general
description: "SIGTERM kills the process mid-task, leaving in-progress work abandoned, partial writes on disk, and no resumption point."
tags: [general, shutdown, signal-handling, reliability, production]
---

## Symptom

When the container orchestrator, process supervisor, or deployment pipeline sends SIGTERM, the agent process dies instantly. Any in-flight API call is abandoned, partial file writes are left on disk, database transactions are uncommitted, and the task queue has no record of what was completed. On restart, the agent either re-runs work it already finished or silently skips work it never committed.

## Root Cause

Python's default SIGTERM behaviour is immediate termination. Unlike SIGKILL, SIGTERM can be caught — but without an explicit signal handler the effect is the same. Long-running agents need to catch SIGTERM, finish or checkpoint the current unit of work, flush state to durable storage, and then exit cleanly within the orchestrator's grace period (typically 30 seconds).

## Fix

### Option 1 — Minimal SIGTERM handler with a shutdown flag

```python
import signal
import time
import anthropic

client = anthropic.Anthropic()

# Global shutdown flag — checked between tasks
_shutdown_requested = False

def _handle_sigterm(signum, frame):
    global _shutdown_requested
    print("[shutdown] SIGTERM received — finishing current task then stopping")
    _shutdown_requested = True

signal.signal(signal.SIGTERM, _handle_sigterm)
signal.signal(signal.SIGINT,  _handle_sigterm)  # Ctrl-C too

def process_task(task_id: int) -> str:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": f"Summarise topic {task_id}."}],
    )
    return response.content[0].text

def run_worker(tasks: list[int]) -> None:
    for task_id in tasks:
        if _shutdown_requested:
            print(f"[shutdown] stopping before task {task_id}")
            break
        print(f"[worker] processing task {task_id}")
        result = process_task(task_id)
        print(f"[worker] task {task_id} done: {result[:60]}")
        time.sleep(0.1)  # simulate work between tasks
    print("[shutdown] worker exited cleanly")

run_worker(list(range(20)))
```

**Expected Token Savings:** Prevents wasted tokens from tasks that would be abandoned mid-call and retried from scratch.
**Environment:** Any long-running agent worker; minimum viable shutdown handling; suitable as a starting point.

---

### Option 2 — asyncio shutdown with task cancellation and cleanup

```python
import asyncio
import signal
import anthropic

client = anthropic.AsyncAnthropic()

async def process_item(item_id: int) -> str:
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{"role": "user", "content": f"Describe item {item_id}."}],
    )
    return response.content[0].text

async def worker(queue: asyncio.Queue) -> None:
    while True:
        try:
            item_id = await asyncio.wait_for(queue.get(), timeout=1.0)
        except asyncio.TimeoutError:
            continue
        try:
            result = await process_item(item_id)
            print(f"[worker] item {item_id}: {result[:60]}")
        except asyncio.CancelledError:
            print(f"[shutdown] worker cancelled mid-task {item_id} — requeuing")
            await queue.put(item_id)  # put back so it can be resumed
            raise
        finally:
            queue.task_done()

async def main():
    queue: asyncio.Queue = asyncio.Queue()
    for i in range(15):
        await queue.put(i)

    loop    = asyncio.get_running_loop()
    workers = [asyncio.create_task(worker(queue)) for _ in range(3)]

    def _shutdown():
        print("[shutdown] signal received — cancelling workers")
        for w in workers:
            w.cancel()

    loop.add_signal_handler(signal.SIGTERM, _shutdown)
    loop.add_signal_handler(signal.SIGINT,  _shutdown)

    try:
        await asyncio.gather(*workers, return_exceptions=True)
    finally:
        await client.close()
        print(f"[shutdown] {queue.qsize()} items remaining in queue")

asyncio.run(main())
```

**Expected Token Savings:** Cancelled tasks are requeued rather than lost; no duplicate API calls on restart.
**Environment:** Async worker pools; asyncio signal handlers integrate cleanly with the event loop.

---

### Option 3 — Checkpoint-based shutdown: save progress to disk

```python
import signal
import json
import os
import time
import anthropic

client = anthropic.Anthropic()

CHECKPOINT_FILE = "/tmp/agent_checkpoint.json"

def save_checkpoint(completed: list[int], pending: list[int]) -> None:
    tmp = CHECKPOINT_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"completed": completed, "pending": pending}, f)
    os.replace(tmp, CHECKPOINT_FILE)

def load_checkpoint() -> tuple[list[int], list[int]]:
    if os.path.exists(CHECKPOINT_FILE):
        with open(CHECKPOINT_FILE) as f:
            data = json.load(f)
        print(f"[checkpoint] resuming: {len(data['completed'])} done, {len(data['pending'])} pending")
        return data["completed"], data["pending"]
    return [], list(range(20))  # fresh start

_shutdown = False

def _handle_signal(signum, frame):
    global _shutdown
    print("[shutdown] signal received")
    _shutdown = True

signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT,  _handle_signal)

def run():
    completed, pending = load_checkpoint()

    while pending and not _shutdown:
        task_id = pending[0]
        print(f"[worker] task {task_id}")

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=128,
            messages=[{"role": "user", "content": f"Process task {task_id}."}],
        )
        print(f"  → {response.content[0].text[:60]}")

        pending.pop(0)
        completed.append(task_id)
        save_checkpoint(completed, pending)   # atomic write after each task

    if _shutdown:
        print(f"[shutdown] checkpointed — {len(completed)} done, {len(pending)} remain")
    else:
        print("[worker] all tasks complete")
        if os.path.exists(CHECKPOINT_FILE):
            os.remove(CHECKPOINT_FILE)

run()
```

**Expected Token Savings:** Zero duplicate work on restart; each task is completed exactly once regardless of when SIGTERM arrives.
**Environment:** Batch processing agents run by cron, Kubernetes Jobs, or systemd services with restart policies.

---

### Option 4 — Context manager for resource cleanup on shutdown

```python
import signal
import contextlib
import asyncio
import anthropic

class GracefulAgent:
    def __init__(self):
        self._client  = anthropic.AsyncAnthropic()
        self._running = True
        self._tasks:  list[asyncio.Task] = []

    async def __aenter__(self):
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGTERM, self._request_shutdown)
        loop.add_signal_handler(signal.SIGINT,  self._request_shutdown)
        return self

    async def __aexit__(self, *_):
        await self._cleanup()

    def _request_shutdown(self):
        print("[shutdown] signal received — draining in-flight tasks")
        self._running = False
        # Do not cancel tasks here; let them finish naturally

    async def _cleanup(self):
        if self._tasks:
            print(f"[shutdown] waiting for {len(self._tasks)} in-flight tasks")
            results = await asyncio.gather(*self._tasks, return_exceptions=True)
            errors  = [r for r in results if isinstance(r, Exception)]
            if errors:
                print(f"[shutdown] {len(errors)} tasks errored during cleanup")
        await self._client.close()
        print("[shutdown] clean exit")

    async def ask(self, prompt: str) -> str:
        task = asyncio.ensure_future(self._client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=128,
            messages=[{"role": "user", "content": prompt}],
        ))
        self._tasks.append(task)
        try:
            response = await task
            return response.content[0].text
        finally:
            with contextlib.suppress(ValueError):
                self._tasks.remove(task)

    async def run_loop(self, items: list[str]) -> None:
        for item in items:
            if not self._running:
                print("[shutdown] loop stopped cleanly")
                break
            result = await self.ask(item)
            print(f"  {item!r}: {result[:60]}")


async def main():
    async with GracefulAgent() as agent:
        await agent.run_loop([f"Tell me about topic {i}" for i in range(10)])

asyncio.run(main())
```

**Expected Token Savings:** In-flight requests complete before shutdown; no orphaned calls that consume tokens without producing usable output.
**Environment:** Async agents with complex resource lifecycles (database connections, file handles, HTTP sessions).

---

### Option 5 — Graceful shutdown with timeout enforcement

```python
import signal
import threading
import time
import sys
import anthropic

client = anthropic.Anthropic()

GRACE_PERIOD_SECONDS = 25  # leave 5s headroom under typical 30s SIGTERM grace

_shutdown_event = threading.Event()

def _handle_signal(signum, frame):
    print(f"[shutdown] signal {signum} received — {GRACE_PERIOD_SECONDS}s grace period starts")
    _shutdown_event.set()

    # Hard kill after grace period expires
    def _force_exit():
        time.sleep(GRACE_PERIOD_SECONDS)
        print("[shutdown] grace period expired — forcing exit")
        sys.exit(1)

    t = threading.Thread(target=_force_exit, daemon=True)
    t.start()

signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT,  _handle_signal)

def do_work(task_id: int) -> str:
    """Each unit of work should complete within the grace period."""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": f"Complete task {task_id}."}],
    )
    return response.content[0].text

def main():
    tasks = list(range(30))
    completed = []

    for task_id in tasks:
        if _shutdown_event.is_set():
            print(f"[shutdown] {len(tasks) - len(completed)} tasks not started — exiting")
            break

        print(f"[worker] task {task_id}")
        result = do_work(task_id)
        completed.append(task_id)
        print(f"  done: {result[:50]}")

    print(f"[shutdown] completed {len(completed)}/{len(tasks)} tasks")

main()
```

**Expected Token Savings:** Enforced grace period ensures in-progress API calls finish; hard exit after deadline prevents container orchestrators from SIGKILL-ing mid-write.
**Environment:** Kubernetes pods, ECS tasks, or systemd services where terminationGracePeriodSeconds is set.

---

### Option 6 — Pre-shutdown hook: flush buffers and notify downstream

```python
import signal
import atexit
import json
import os
import anthropic

client = anthropic.Anthropic()

# In-memory result buffer flushed on shutdown
_result_buffer: list[dict] = []
_flush_path     = "/tmp/agent_results_buffer.json"

def flush_buffer() -> None:
    if not _result_buffer:
        return
    existing = []
    if os.path.exists(_flush_path):
        with open(_flush_path) as f:
            existing = json.load(f)

    all_results = existing + _result_buffer
    tmp = _flush_path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(all_results, f, indent=2)
    os.replace(tmp, _flush_path)
    print(f"[shutdown] flushed {len(_result_buffer)} buffered results to {_flush_path}")
    _result_buffer.clear()

def notify_downstream(reason: str) -> None:
    """Post a shutdown notification to a webhook or message queue."""
    import urllib.request
    payload = json.dumps({"event": "agent_shutdown", "reason": reason, "buffered": len(_result_buffer)})
    try:
        req = urllib.request.Request(
            "https://httpbin.org/post",   # replace with your webhook
            data=payload.encode(),
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=5)
        print("[shutdown] downstream notified")
    except Exception as e:
        print(f"[shutdown] notification failed: {e}")

def _on_shutdown(signum, frame):
    print("[shutdown] pre-shutdown hooks running")
    flush_buffer()
    notify_downstream("SIGTERM")
    raise SystemExit(0)

signal.signal(signal.SIGTERM, _on_shutdown)
signal.signal(signal.SIGINT,  _on_shutdown)
atexit.register(flush_buffer)  # also flush on normal exit

def process(task_id: int) -> None:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{"role": "user", "content": f"Process item {task_id}."}],
    )
    _result_buffer.append({"task_id": task_id, "result": response.content[0].text})
    # Flush every 10 items proactively
    if len(_result_buffer) >= 10:
        flush_buffer()

for i in range(25):
    process(i)

print("All tasks done.")
```

**Expected Token Savings:** Prevents re-processing of buffered results on restart; downstream systems receive a shutdown event rather than inferring it from a missed heartbeat.
**Environment:** Agents that buffer results before writing to a database or message queue; producer agents in a pipeline.

---

## Comparison

| Option | Mechanism | State Preserved | Async Safe | Restart Safe | Best For |
|---|---|---|---|---|---|
| 1. Shutdown flag | Global bool | No | No | No | Simple sequential workers |
| 2. Task cancellation | asyncio cancel | Queue | Yes | Partial | Async worker pools |
| 3. Checkpoint | Atomic file write | Full | No | Yes | Batch jobs with restart policy |
| 4. Context manager | `__aexit__` cleanup | In-flight drained | Yes | No | Complex async resource lifecycle |
| 5. Grace timeout | Threading timer | Partial | No | No | K8s / systemd with grace period |
| 6. Pre-shutdown hooks | Signal + atexit | Buffer flushed | No | Yes | Pipeline producers, buffered writers |
