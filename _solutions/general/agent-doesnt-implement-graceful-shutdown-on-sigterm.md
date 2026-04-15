---
layout: solution
title: "Agent Doesn't Implement Graceful Shutdown on SIGTERM"
category: general
description: "How to handle SIGTERM and SIGINT signals in agent processes to complete in-flight requests, flush state, cancel pending work cleanly, and exit without data loss."
tags: [general, shutdown, sigterm, signal-handling, graceful, asyncio]
---

# Agent Doesn't Implement Graceful Shutdown on SIGTERM

Agents killed with SIGTERM (container shutdown, Kubernetes pod eviction, `systemctl stop`) without graceful handling drop in-flight API calls, corrupt partial writes, lose queued work, and leave external resources in undefined state. Graceful shutdown completes or checkpoints active work, flushes state, closes connections, and exits cleanly within a deadline.

## Option 1: Basic SIGTERM Handler with Shutdown Flag

Set a global shutdown flag on SIGTERM. Agent loop checks the flag after each step and exits cleanly.

```python
import anthropic
import signal
import time
import sys
from dataclasses import dataclass
from threading import Event

# Shared shutdown event — safe to check from any thread
_shutdown_event = Event()


def handle_sigterm(signum, frame):
    print(f"\n[SHUTDOWN] Signal {signum} received — initiating graceful shutdown")
    _shutdown_event.set()


# Register handlers for both SIGTERM (container stop) and SIGINT (Ctrl+C)
signal.signal(signal.SIGTERM, handle_sigterm)
signal.signal(signal.SIGINT, handle_sigterm)


def is_shutting_down() -> bool:
    return _shutdown_event.is_set()


def process_request(client: anthropic.Anthropic, prompt: str) -> str:
    """Single request processing — runs to completion even during shutdown."""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


def agent_worker_loop(prompts: list[str]):
    client = anthropic.Anthropic()
    processed = 0
    skipped = 0

    print(f"[AGENT] Starting with {len(prompts)} tasks")

    for i, prompt in enumerate(prompts):
        # Check shutdown flag before picking up new work
        if is_shutting_down():
            skipped = len(prompts) - i
            print(f"[SHUTDOWN] Stopping before task {i+1}. Skipping {skipped} remaining tasks.")
            break

        print(f"[AGENT] Processing task {i+1}/{len(prompts)}: {prompt[:40]}")

        try:
            result = process_request(client, prompt)
            processed += 1
            print(f"[AGENT] Done: {result[:60]}...")
        except Exception as e:
            print(f"[AGENT] Error on task {i+1}: {e}")

        # Simulate some processing time
        time.sleep(0.1)

    print(f"[AGENT] Shutdown complete. Processed: {processed}, Skipped: {skipped}")
    return processed, skipped


if __name__ == "__main__":
    tasks = [f"What is {n} squared?" for n in range(1, 20)]

    print("Send SIGTERM (kill -15 <pid>) or press Ctrl+C to test graceful shutdown")
    print(f"PID: {__import__('os').getpid()}")

    processed, skipped = agent_worker_loop(tasks)
    print(f"Exit: processed={processed} skipped={skipped}")
    sys.exit(0 if not is_shutting_down() else 130)

# Expected Token Savings: Prevents token waste on abandoned in-flight requests dropped by abrupt kills
# Environment: Long-running agent workers, batch processors, Kubernetes pods subject to eviction
```

## Option 2: Async Graceful Shutdown with Drain Timeout

Async agent that drains its in-flight task queue on SIGTERM, waiting up to a configurable deadline before force-cancelling.

```python
import anthropic
import asyncio
import signal
import time
from dataclasses import dataclass
from typing import Optional


@dataclass
class ShutdownController:
    drain_timeout_seconds: float = 15.0
    _shutdown_requested: bool = False
    _shutdown_time: Optional[float] = None
    _active_tasks: set = None

    def __post_init__(self):
        self._active_tasks = set()

    def request_shutdown(self):
        if not self._shutdown_requested:
            self._shutdown_requested = True
            self._shutdown_time = time.monotonic()
            print(f"[SHUTDOWN] Graceful shutdown requested. Drain timeout: {self.drain_timeout_seconds}s")

    @property
    def is_shutting_down(self) -> bool:
        return self._shutdown_requested

    @property
    def drain_deadline_exceeded(self) -> bool:
        if not self._shutdown_time:
            return False
        return (time.monotonic() - self._shutdown_time) > self.drain_timeout_seconds

    def register_task(self, task_id: str):
        self._active_tasks.add(task_id)

    def complete_task(self, task_id: str):
        self._active_tasks.discard(task_id)

    @property
    def active_count(self) -> int:
        return len(self._active_tasks)


controller = ShutdownController(drain_timeout_seconds=10.0)


def setup_async_signal_handlers(loop: asyncio.AbstractEventLoop):
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, controller.request_shutdown)


async def process_one_request(client: anthropic.AsyncAnthropic, task_id: str, prompt: str) -> str:
    controller.register_task(task_id)
    try:
        print(f"[TASK {task_id}] Starting: {prompt[:40]}")
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=150,
            messages=[{"role": "user", "content": prompt}],
        )
        result = response.content[0].text
        print(f"[TASK {task_id}] Done: {result[:50]}...")
        return result
    finally:
        controller.complete_task(task_id)


async def async_agent_with_graceful_drain(prompts: list[str]):
    client = anthropic.AsyncAnthropic()
    loop = asyncio.get_running_loop()
    setup_async_signal_handlers(loop)

    pending_tasks = asyncio.Queue()
    for i, p in enumerate(prompts):
        await pending_tasks.put((str(i), p))

    running = set()
    results = []

    while True:
        # Check if we should accept new work
        if controller.is_shutting_down:
            if controller.drain_deadline_exceeded:
                print(f"[SHUTDOWN] Drain timeout exceeded. Cancelling {len(running)} active tasks.")
                for t in running:
                    t.cancel()
                break
            if not running:
                print("[SHUTDOWN] All active tasks drained. Exiting cleanly.")
                break
            # Wait for active tasks to finish
            done, running = await asyncio.wait(running, timeout=1.0, return_when=asyncio.FIRST_COMPLETED)
            for d in done:
                try:
                    results.append(await d)
                except (asyncio.CancelledError, Exception) as e:
                    print(f"[SHUTDOWN] Task ended: {e}")
            print(f"[SHUTDOWN] Active tasks remaining: {len(running)}")
            continue

        # Normal operation: fill up to concurrency limit
        while len(running) < 3 and not pending_tasks.empty():
            task_id, prompt = await pending_tasks.get()
            t = asyncio.create_task(process_one_request(client, task_id, prompt))
            running.add(t)

        if not running:
            break  # All work complete

        done, running = await asyncio.wait(running, timeout=0.5, return_when=asyncio.FIRST_COMPLETED)
        for d in done:
            try:
                results.append(await d)
            except Exception as e:
                print(f"[AGENT] Task error: {e}")

    print(f"\n[AGENT] Completed {len(results)} tasks. Queue remaining: {pending_tasks.qsize()}")
    return results


async def main():
    tasks = [f"What is {chr(65+i)} in NATO phonetic alphabet?" for i in range(10)]
    print(f"PID: {__import__('os').getpid()} — send SIGTERM to test graceful drain")
    results = await async_agent_with_graceful_drain(tasks)
    print(f"Done. Results: {len(results)}")


if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: In-flight requests complete rather than being dropped; avoids re-processing after restart
# Environment: Kubernetes pods, ECS tasks, any containerized agent subject to rolling deploys or eviction
```

## Option 3: State Checkpoint on SIGTERM — Resume After Restart

On receiving SIGTERM, checkpoint current work-in-progress to disk so the next process can resume from where it stopped.

```python
import anthropic
import signal
import json
import os
import time
from dataclasses import dataclass, field, asdict
from typing import Optional
from pathlib import Path


CHECKPOINT_PATH = Path("agent_checkpoint.json")


@dataclass
class AgentCheckpoint:
    job_id: str
    prompts: list[str]
    completed_indices: list[int]
    results: dict          # index -> result
    saved_at: float = field(default_factory=time.time)
    interrupted: bool = False

    def save(self, path: Path = CHECKPOINT_PATH):
        data = asdict(self)
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
        print(f"[CHECKPOINT] Saved at step {len(self.completed_indices)}/{len(self.prompts)}")

    @staticmethod
    def load(path: Path = CHECKPOINT_PATH) -> Optional["AgentCheckpoint"]:
        if not path.exists():
            return None
        with open(path) as f:
            data = json.load(f)
        cp = AgentCheckpoint(**data)
        print(f"[CHECKPOINT] Resuming from step {len(cp.completed_indices)}/{len(cp.prompts)}")
        return cp

    def pending_indices(self) -> list[int]:
        done = set(self.completed_indices)
        return [i for i in range(len(self.prompts)) if i not in done]


# Global checkpoint reference for signal handler
_checkpoint: Optional[AgentCheckpoint] = None


def handle_sigterm(signum, frame):
    global _checkpoint
    print(f"\n[SHUTDOWN] SIGTERM received")
    if _checkpoint:
        _checkpoint.interrupted = True
        _checkpoint.save()
        print(f"[SHUTDOWN] Checkpoint saved. Run again to resume.")
    raise SystemExit(130)


signal.signal(signal.SIGTERM, handle_sigterm)
signal.signal(signal.SIGINT, handle_sigterm)


def run_resumable_agent(job_id: str, prompts: list[str]) -> dict:
    global _checkpoint
    client = anthropic.Anthropic()

    # Try to resume from checkpoint
    checkpoint = AgentCheckpoint.load()
    if checkpoint and checkpoint.job_id == job_id and checkpoint.interrupted:
        print(f"[RESUME] Resuming interrupted job '{job_id}'")
        _checkpoint = checkpoint
    else:
        # Fresh start
        checkpoint = AgentCheckpoint(
            job_id=job_id,
            prompts=prompts,
            completed_indices=[],
            results={},
        )
        _checkpoint = checkpoint

    pending = checkpoint.pending_indices()
    print(f"[AGENT] {len(pending)} tasks remaining of {len(prompts)} total")

    for i in pending:
        prompt = prompts[i]
        print(f"[AGENT] Processing [{i+1}/{len(prompts)}]: {prompt[:50]}")

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=150,
            messages=[{"role": "user", "content": prompt}],
        )

        result = response.content[0].text
        checkpoint.results[str(i)] = result
        checkpoint.completed_indices.append(i)
        checkpoint.interrupted = False

        # Save checkpoint after every completed task
        checkpoint.save()

        print(f"[AGENT] [{i+1}] Done: {result[:60]}...")

    # All done — clean up checkpoint
    if CHECKPOINT_PATH.exists():
        CHECKPOINT_PATH.unlink()
        print("[CHECKPOINT] Cleaned up checkpoint file")

    return checkpoint.results


if __name__ == "__main__":
    prompts = [f"Explain concept #{i}: {['recursion','closure','monads','currying','functors'][i % 5]}" for i in range(8)]
    print(f"PID: {os.getpid()} — SIGTERM to simulate interruption, run again to resume")
    results = run_resumable_agent("batch-001", prompts)
    print(f"\nCompleted {len(results)} tasks")

# Expected Token Savings: Zero re-work cost — completed tasks never reprocessed after restart
# Environment: Long batch jobs on preemptible VMs, spot instances, agents with expensive per-task processing
```

## Option 4: Connection and Resource Cleanup on Shutdown

Ensure all external connections (databases, message queues, HTTP clients) are properly closed before process exit.

```python
import anthropic
import signal
import sqlite3
import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class AgentResources:
    """Holds all external resources that need cleanup on shutdown."""
    db: Optional[sqlite3.Connection] = None
    client: Optional[anthropic.AsyncAnthropic] = None
    _closed: bool = False

    async def initialize(self):
        self.db = sqlite3.connect(":memory:")
        self.db.execute("CREATE TABLE IF NOT EXISTS results (id INTEGER PRIMARY KEY, output TEXT)")
        self.db.commit()
        self.client = anthropic.AsyncAnthropic()
        print("[RESOURCES] Initialized: DB, Anthropic client")

    async def close(self):
        if self._closed:
            return
        self._closed = True
        print("[RESOURCES] Starting cleanup...")

        if self.db:
            try:
                self.db.commit()  # Flush any pending transactions
                self.db.close()
                print("[RESOURCES] DB connection closed")
            except Exception as e:
                print(f"[RESOURCES] DB close error: {e}")

        if self.client:
            try:
                await self.client.close()
                print("[RESOURCES] HTTP client closed")
            except Exception as e:
                print(f"[RESOURCES] Client close error: {e}")

        print("[RESOURCES] Cleanup complete")


resources = AgentResources()
_shutdown = asyncio.Event()


def setup_signals(loop: asyncio.AbstractEventLoop):
    def on_signal():
        print("\n[SHUTDOWN] Signal received")
        _shutdown.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, on_signal)


async def save_result(task_id: int, output: str):
    if resources.db:
        resources.db.execute("INSERT INTO results (id, output) VALUES (?, ?)", (task_id, output))
        resources.db.commit()


async def process_task(task_id: int, prompt: str) -> str:
    if resources.client is None:
        raise RuntimeError("Client not initialized")
    response = await resources.client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=100,
        messages=[{"role": "user", "content": prompt}],
    )
    result = response.content[0].text
    await save_result(task_id, result)
    return result


async def main_with_resource_cleanup():
    loop = asyncio.get_running_loop()
    setup_signals(loop)

    await resources.initialize()

    prompts = [f"What is {i}+{i}?" for i in range(1, 12)]
    task_queue = asyncio.Queue()
    for i, p in enumerate(prompts):
        await task_queue.put((i, p))

    active = set()
    completed = 0

    try:
        while not (task_queue.empty() and not active):
            # Check for shutdown
            if _shutdown.is_set():
                print(f"[SHUTDOWN] Waiting for {len(active)} active tasks to finish...")
                if active:
                    await asyncio.gather(*active, return_exceptions=True)
                print(f"[SHUTDOWN] Drained. Completed: {completed}")
                break

            # Dispatch new tasks
            while len(active) < 3 and not task_queue.empty():
                task_id, prompt = await task_queue.get()
                t = asyncio.create_task(process_task(task_id, prompt))
                active.add(t)

            if not active:
                break

            done, active = await asyncio.wait(active, timeout=1.0, return_when=asyncio.FIRST_COMPLETED)
            for d in done:
                try:
                    result = await d
                    completed += 1
                    print(f"[TASK] Done ({completed}): {result[:40]}")
                except Exception as e:
                    print(f"[TASK] Error: {e}")

    finally:
        # Always clean up resources
        await resources.close()
        print(f"\n[AGENT] Exited cleanly. Completed: {completed}/{len(prompts)}")


if __name__ == "__main__":
    asyncio.run(main_with_resource_cleanup())

# Expected Token Savings: Prevents connection leak storms on restart; clean shutdown avoids timeout penalties
# Environment: Agents with database connections, message queues, persistent HTTP clients
```

## Option 5: Multi-Worker Coordinator Shutdown — Drain All Workers Before Exit

Coordinate shutdown across a pool of worker coroutines, ensuring all workers acknowledge the signal before exit.

```python
import anthropic
import asyncio
import signal
import time
from dataclasses import dataclass, field


@dataclass
class WorkerShutdownCoordinator:
    worker_count: int
    drain_timeout: float = 20.0
    _shutdown_event: asyncio.Event = field(default_factory=asyncio.Event)
    _worker_done_events: list = field(default_factory=list)
    _shutdown_time: float = 0.0

    def __post_init__(self):
        self._worker_done_events = [asyncio.Event() for _ in range(self.worker_count)]

    def signal_shutdown(self):
        if not self._shutdown_event.is_set():
            self._shutdown_event.set()
            self._shutdown_time = time.monotonic()
            print(f"[COORD] Shutdown signaled to {self.worker_count} workers")

    def worker_done(self, worker_id: int):
        self._worker_done_events[worker_id].set()
        print(f"[COORD] Worker {worker_id} reported done")

    async def wait_for_all_workers(self) -> bool:
        """Returns True if all workers finished within timeout."""
        try:
            await asyncio.wait_for(
                asyncio.gather(*[e.wait() for e in self._worker_done_events]),
                timeout=self.drain_timeout,
            )
            elapsed = time.monotonic() - self._shutdown_time
            print(f"[COORD] All workers done in {elapsed:.1f}s")
            return True
        except asyncio.TimeoutError:
            done = sum(1 for e in self._worker_done_events if e.is_set())
            print(f"[COORD] Timeout! {done}/{self.worker_count} workers finished")
            return False

    @property
    def should_stop(self) -> bool:
        return self._shutdown_event.is_set()


async def worker(
    worker_id: int,
    coord: WorkerShutdownCoordinator,
    task_queue: asyncio.Queue,
    client: anthropic.AsyncAnthropic,
):
    print(f"[W{worker_id}] Started")
    processed = 0

    try:
        while True:
            # Check shutdown before dequeuing
            if coord.should_stop and task_queue.empty():
                print(f"[W{worker_id}] Shutdown: queue empty")
                break

            try:
                prompt = await asyncio.wait_for(task_queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                if coord.should_stop:
                    break
                continue

            print(f"[W{worker_id}] Processing: {prompt[:40]}")
            response = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=80,
                messages=[{"role": "user", "content": prompt}],
            )
            processed += 1
            print(f"[W{worker_id}] Done ({processed}): {response.content[0].text[:40]}")
            task_queue.task_done()

    except asyncio.CancelledError:
        print(f"[W{worker_id}] Cancelled (processed {processed})")
    finally:
        coord.worker_done(worker_id)
        print(f"[W{worker_id}] Exited. Total processed: {processed}")


async def multi_worker_agent(num_workers: int = 3):
    client = anthropic.AsyncAnthropic()
    coord = WorkerShutdownCoordinator(worker_count=num_workers, drain_timeout=15.0)

    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, coord.signal_shutdown)
    loop.add_signal_handler(signal.SIGINT, coord.signal_shutdown)

    # Fill task queue
    task_queue = asyncio.Queue()
    prompts = [f"What is the {i}th Fibonacci number?" for i in range(1, 16)]
    for p in prompts:
        await task_queue.put(p)

    # Start workers
    workers = [
        asyncio.create_task(worker(i, coord, task_queue, client))
        for i in range(num_workers)
    ]

    print(f"[COORD] {num_workers} workers running. PID={__import__('os').getpid()}")

    # Wait for all work or shutdown
    await asyncio.gather(*workers, return_exceptions=True)
    all_clean = await coord.wait_for_all_workers()

    print(f"\n[COORD] Shutdown {'clean' if all_clean else 'forced'}. Queue remaining: {task_queue.qsize()}")
    await client.close()


if __name__ == "__main__":
    asyncio.run(multi_worker_agent(num_workers=3))

# Expected Token Savings: Prevents duplicate work from uncoordinated worker restarts after abrupt kill
# Environment: Parallel agent worker pools, multi-consumer message queue processors
```

## Option 6: Pre-Shutdown Hook System — Run Cleanup Callbacks in Order

Register cleanup hooks that run in priority order on SIGTERM: flush logs, save state, close connections, then exit.

```python
import anthropic
import signal
import time
import atexit
from dataclasses import dataclass, field
from typing import Callable, Optional


@dataclass
class ShutdownHook:
    name: str
    priority: int           # Lower = runs first
    callback: Callable
    timeout_seconds: float = 5.0


class ShutdownHookRegistry:
    def __init__(self):
        self._hooks: list[ShutdownHook] = []
        self._triggered: bool = False

    def register(self, name: str, callback: Callable, priority: int = 50, timeout: float = 5.0):
        self._hooks.append(ShutdownHook(
            name=name,
            priority=priority,
            callback=callback,
            timeout_seconds=timeout,
        ))
        self._hooks.sort(key=lambda h: h.priority)
        print(f"[HOOKS] Registered: '{name}' (priority={priority})")

    def run_all(self):
        if self._triggered:
            return
        self._triggered = True
        print(f"\n[HOOKS] Running {len(self._hooks)} shutdown hooks...")

        for hook in self._hooks:
            print(f"[HOOKS] Running: {hook.name}")
            start = time.monotonic()
            try:
                hook.callback()
                elapsed = time.monotonic() - start
                print(f"[HOOKS] Done: {hook.name} ({elapsed:.2f}s)")
            except Exception as e:
                print(f"[HOOKS] Error in {hook.name}: {e}")
            finally:
                elapsed = time.monotonic() - start
                if elapsed > hook.timeout_seconds:
                    print(f"[HOOKS] WARNING: {hook.name} exceeded timeout ({elapsed:.1f}s > {hook.timeout_seconds}s)")

        print("[HOOKS] All shutdown hooks complete")


registry = ShutdownHookRegistry()


def handle_signal(signum, frame):
    print(f"\n[SHUTDOWN] Signal {signum} received")
    registry.run_all()
    __import__("sys").exit(0)


signal.signal(signal.SIGTERM, handle_signal)
signal.signal(signal.SIGINT, handle_signal)
atexit.register(registry.run_all)  # Also runs on normal exit


# --- Agent setup ---

# Simulate agent state
_pending_results = []
_db_connection = None
_log_buffer = []


def flush_log_buffer():
    if _log_buffer:
        print(f"[LOG] Flushing {len(_log_buffer)} buffered log entries")
        for entry in _log_buffer:
            print(f"  LOG: {entry}")
        _log_buffer.clear()


def save_pending_results():
    if _pending_results:
        print(f"[STATE] Saving {len(_pending_results)} pending results to disk")
        import json
        with open("pending_results.json", "w") as f:
            json.dump(_pending_results, f)
        print("[STATE] Saved")


def close_db():
    global _db_connection
    if _db_connection:
        _db_connection.commit()
        _db_connection.close()
        _db_connection = None
        print("[DB] Connection closed and committed")


def send_shutdown_notification():
    print("[NOTIFY] Sending shutdown notification to monitoring system")
    # In real code: post to Slack/PagerDuty/metrics


# Register hooks in priority order
registry.register("flush_logs", flush_log_buffer, priority=10, timeout=2.0)
registry.register("save_state", save_pending_results, priority=20, timeout=5.0)
registry.register("close_db", close_db, priority=30, timeout=3.0)
registry.register("notify_shutdown", send_shutdown_notification, priority=40, timeout=2.0)


def run_agent():
    client = anthropic.Anthropic()
    prompts = [f"What is {i} cubed?" for i in range(1, 10)]

    for i, prompt in enumerate(prompts):
        _log_buffer.append(f"Processing prompt {i+1}: {prompt}")
        print(f"[AGENT] Task {i+1}: {prompt}")

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=60,
            messages=[{"role": "user", "content": prompt}],
        )
        result = response.content[0].text
        _pending_results.append({"prompt": prompt, "result": result})
        print(f"[AGENT] Result: {result[:60]}")

        time.sleep(0.2)  # Simulate processing


if __name__ == "__main__":
    import os
    print(f"PID: {os.getpid()} — send SIGTERM to test hook ordering")
    run_agent()
    print("[AGENT] All tasks complete")

# Expected Token Savings: Prevents restart overhead from lost state — checkpointed state means no re-processing
# Environment: Any production agent process; especially valuable for stateful long-running agents
```

## Comparison

| Option | Approach | Persistence | Concurrency Support | Best For |
|--------|----------|-------------|---------------------|----------|
| 1 Shutdown Flag | Event flag per loop iteration | None | Single-threaded | Simple worker loops |
| 2 Async Drain | Queue drain with timeout | In-memory | Async concurrent | Kubernetes pods, ECS tasks |
| 3 Checkpoint/Resume | JSON checkpoint on SIGTERM | File-based | Single-threaded | Preemptible VMs, spot instances |
| 4 Resource Cleanup | Explicit close per resource | DB commit | Async | Agents with DB/queue connections |
| 5 Worker Coordinator | Per-worker done events | In-memory | Multi-worker async | Parallel worker pools |
| 6 Hook Registry | Prioritized callback chain | File-based | Single-threaded | Complex agents with multiple subsystems |
