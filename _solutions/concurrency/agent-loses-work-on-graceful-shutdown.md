---
layout: solution
title: "Agent Loses In-Progress Work on Graceful Shutdown — SIGTERM Kills Mid-Task"
category: concurrency
description: "Kubernetes sends SIGTERM. Docker compose stops the container. The agent is halfway through a 50-step task. Python process exits. All in-progress work is lost. Next restart starts from scratch — or worse, partially-applied changes corrupt state."
tags: [concurrency, shutdown, sigterm, graceful, checkpoint, kubernetes, docker, signal-handling, task-recovery, asyncio]
---

# Agent Loses In-Progress Work on Graceful Shutdown — SIGTERM Kills Mid-Task

## Symptom
- Kubernetes rolling deploy sends SIGTERM — agent exits mid-task, work lost
- Docker container stops during a 10-minute background job — partial writes leave corrupted state
- Agent is 80% through processing 1,000 records when it's killed — restarts from record 1
- `asyncio.CancelledError` propagates up through the task graph, nothing is saved
- Partially-written files are left behind — next run fails to parse them
- Agent sends a message but doesn't record it — on restart it sends again (duplicate)

## Root Cause
Agents that don't handle SIGTERM explicitly will be killed mid-execution after the OS default timeout (usually 10-30 seconds). Any in-progress work that hasn't been committed to durable storage is lost. The fix is to intercept SIGTERM, trigger a graceful shutdown sequence: checkpoint current progress, finish the current atomic unit of work, then exit cleanly.

## Fix

### Option 1: SIGTERM handler with asyncio — checkpoint before exit

```python
import asyncio
import signal
import json
import os
from pathlib import Path
from dataclasses import dataclass, asdict
from typing import Optional

@dataclass
class TaskCheckpoint:
    task_id: str
    items_processed: int
    items_total: int
    last_item_id: str
    partial_result: dict
    started_at: float
    checkpointed_at: float

class GracefulShutdownManager:
    """
    Handles SIGTERM/SIGINT for asyncio agents.
    On shutdown signal: finish current item, checkpoint progress, exit clean.
    """

    def __init__(
        self,
        checkpoint_dir: str = "/data/checkpoints",
        shutdown_timeout: float = 25.0  # Must be < Kubernetes terminationGracePeriodSeconds
    ):
        self.checkpoint_dir = Path(checkpoint_dir)
        self.checkpoint_dir.mkdir(parents=True, exist_ok=True)
        self.shutdown_timeout = shutdown_timeout
        self._shutdown_event = asyncio.Event()
        self._current_checkpoint: Optional[TaskCheckpoint] = None

    def register_signals(self):
        """Register SIGTERM and SIGINT handlers"""
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self._handle_signal)
        print(f"Graceful shutdown registered — timeout: {self.shutdown_timeout}s")

    def _handle_signal(self):
        print("Shutdown signal received — finishing current item then exiting")
        self._shutdown_event.set()

    @property
    def should_shutdown(self) -> bool:
        return self._shutdown_event.is_set()

    async def wait_for_shutdown(self):
        await self._shutdown_event.wait()

    def update_checkpoint(self, checkpoint: TaskCheckpoint):
        """Update in-memory checkpoint — call after each item"""
        self._current_checkpoint = checkpoint

    def save_checkpoint(self, task_id: str):
        """Write checkpoint to disk — call before shutdown"""
        if self._current_checkpoint is None:
            return

        path = self.checkpoint_dir / f"{task_id}.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self._current_checkpoint), indent=2))
        tmp.replace(path)  # Atomic write
        print(f"Checkpoint saved: {path} ({self._current_checkpoint.items_processed}/{self._current_checkpoint.items_total} items)")

    def load_checkpoint(self, task_id: str) -> Optional[TaskCheckpoint]:
        """Load checkpoint from previous run — resume from where we left off"""
        path = self.checkpoint_dir / f"{task_id}.json"
        if not path.exists():
            return None
        data = json.loads(path.read_text())
        print(f"Resuming from checkpoint: {data['items_processed']}/{data['items_total']} items already processed")
        return TaskCheckpoint(**data)

    def clear_checkpoint(self, task_id: str):
        """Remove checkpoint after successful completion"""
        path = self.checkpoint_dir / f"{task_id}.json"
        path.unlink(missing_ok=True)

shutdown = GracefulShutdownManager()

async def process_items_with_checkpoint(
    task_id: str,
    items: list,
    process_fn
) -> dict:
    """
    Process items with checkpoint/resume support.
    Safe to kill at any point — resumes from last checkpoint on restart.
    """
    import time

    # Resume from checkpoint if available
    checkpoint = shutdown.load_checkpoint(task_id)
    start_index = checkpoint.items_processed if checkpoint else 0
    results = checkpoint.partial_result if checkpoint else {}

    print(f"Processing {len(items)} items starting from index {start_index}")

    for i, item in enumerate(items[start_index:], start=start_index):
        if shutdown.should_shutdown:
            print(f"Shutdown requested — stopping after item {i}")
            break

        # Process one item (atomic unit of work)
        result = await process_fn(item)
        results[item["id"]] = result

        # Update checkpoint after each item
        shutdown.update_checkpoint(TaskCheckpoint(
            task_id=task_id,
            items_processed=i + 1,
            items_total=len(items),
            last_item_id=str(item["id"]),
            partial_result=results,
            started_at=checkpoint.started_at if checkpoint else time.time(),
            checkpointed_at=time.time()
        ))

    # Save checkpoint on normal exit too (allows inspection)
    shutdown.save_checkpoint(task_id)

    if not shutdown.should_shutdown:
        # All done — clear checkpoint
        shutdown.clear_checkpoint(task_id)
        print(f"Task {task_id} complete: {len(items)} items processed")

    return results

async def main():
    shutdown.register_signals()

    # Run task with graceful shutdown support
    items = [{"id": i, "data": f"item_{i}"} for i in range(1000)]

    async def process_item(item):
        await asyncio.sleep(0.1)  # Simulate work
        return {"processed": True, "id": item["id"]}

    try:
        results = await asyncio.wait_for(
            process_items_with_checkpoint("batch_001", items, process_item),
            timeout=None
        )
    except asyncio.CancelledError:
        shutdown.save_checkpoint("batch_001")
        print("Task cancelled — checkpoint saved")

asyncio.run(main())
```

### Option 2: Context manager for atomic task units

```python
import asyncio
import signal
from contextlib import asynccontextmanager
from typing import AsyncIterator

class AtomicTaskUnit:
    """
    Ensures an in-progress unit of work is either fully committed or rolled back.
    Shutdown is deferred until the current unit completes.
    """

    def __init__(self):
        self._shutdown_requested = asyncio.Event()
        self._unit_in_progress = asyncio.Event()
        self._committed_count = 0

    def request_shutdown(self):
        self._shutdown_requested.set()
        if not self._unit_in_progress.is_set():
            print("No unit in progress — shutting down immediately")
        else:
            print("Unit in progress — will shut down after it completes")

    @asynccontextmanager
    async def unit(self, unit_id: str) -> AsyncIterator[None]:
        """
        Context manager for an atomic unit of work.
        Shutdown is deferred until context exits.
        """
        self._unit_in_progress.set()
        try:
            yield
            self._committed_count += 1
        except Exception:
            print(f"Unit {unit_id} failed — rolling back")
            raise
        finally:
            self._unit_in_progress.clear()
            # Check if shutdown was requested while unit was running
            if self._shutdown_requested.is_set():
                raise asyncio.CancelledError(f"Shutdown after unit {unit_id}")

    @property
    def should_continue(self) -> bool:
        return not self._shutdown_requested.is_set()

atomic = AtomicTaskUnit()

# Register signal handler
def handle_shutdown():
    atomic.request_shutdown()

asyncio.get_event_loop().add_signal_handler(signal.SIGTERM, handle_shutdown)

async def process_with_atomic_units(records: list):
    """Process records where each record is an atomic unit"""
    for i, record in enumerate(records):
        if not atomic.should_continue:
            print(f"Stopping before record {i} — shutdown requested")
            break

        try:
            async with atomic.unit(f"record_{record['id']}"):
                # All work inside here is atomic — either all succeeds or rolls back
                await write_to_database(record)
                await send_confirmation(record)
                await update_index(record)
                # If shutdown is signaled while here, we finish this block first

        except asyncio.CancelledError:
            print(f"Shutdown after completing record {record['id']}")
            break

    print(f"Processed {atomic._committed_count} records before shutdown")

async def write_to_database(record): await asyncio.sleep(0.05)
async def send_confirmation(record): await asyncio.sleep(0.02)
async def update_index(record): await asyncio.sleep(0.01)
```

### Option 3: Pre-shutdown hook — drain queue before exit

```python
import asyncio
import signal
from asyncio import Queue

class DrainOnShutdown:
    """
    When SIGTERM arrives, drain the current queue before exiting.
    New items are rejected; in-flight items complete.
    """

    def __init__(self, queue: Queue, drain_timeout: float = 20.0):
        self.queue = queue
        self.drain_timeout = drain_timeout
        self._accepting = True
        self._workers: list[asyncio.Task] = []
        self._shutdown_initiated = False

    async def put(self, item) -> bool:
        """Add item to queue. Returns False if shutdown in progress."""
        if not self._accepting:
            return False
        await self.queue.put(item)
        return True

    def register_shutdown(self):
        loop = asyncio.get_event_loop()
        loop.add_signal_handler(signal.SIGTERM, lambda: asyncio.create_task(self.shutdown()))
        loop.add_signal_handler(signal.SIGINT, lambda: asyncio.create_task(self.shutdown()))

    async def shutdown(self):
        if self._shutdown_initiated:
            return
        self._shutdown_initiated = True
        print("SIGTERM received — draining queue before shutdown")

        # Stop accepting new items
        self._accepting = False

        # Wait for queue to drain (with timeout)
        remaining = self.queue.qsize()
        print(f"Draining {remaining} remaining items (timeout: {self.drain_timeout}s)")

        try:
            await asyncio.wait_for(self.queue.join(), timeout=self.drain_timeout)
            print("Queue drained successfully — clean shutdown")
        except asyncio.TimeoutError:
            print(f"Drain timeout after {self.drain_timeout}s — {self.queue.qsize()} items remaining")
            # Cancel worker tasks
            for task in self._workers:
                task.cancel()

    async def worker(self, worker_id: int, process_fn):
        """Worker that processes items and marks them done"""
        while True:
            try:
                item = await asyncio.wait_for(self.queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                if self._shutdown_initiated and self.queue.empty():
                    break  # No more items and shutting down
                continue
            except asyncio.CancelledError:
                break

            try:
                await process_fn(item)
            except Exception as e:
                print(f"Worker {worker_id} error on item {item}: {e}")
            finally:
                self.queue.task_done()

    async def start_workers(self, n_workers: int, process_fn):
        """Start worker tasks"""
        for i in range(n_workers):
            task = asyncio.create_task(self.worker(i, process_fn))
            self._workers.append(task)
        return self._workers

# Usage:
queue = Queue(maxsize=500)
drain = DrainOnShutdown(queue, drain_timeout=20.0)
drain.register_shutdown()

async def process_message(msg: dict):
    await asyncio.sleep(0.1)  # Simulate processing
    print(f"Processed: {msg}")

workers = await drain.start_workers(n_workers=5, process_fn=process_message)
```

### Option 4: Kubernetes-aware shutdown — respect terminationGracePeriodSeconds

```yaml
# kubernetes/deployment.yaml
apiVersion: apps/v1
kind: Deployment
spec:
  template:
    spec:
      # Give the agent 60 seconds to shut down gracefully
      terminationGracePeriodSeconds: 60
      containers:
      - name: agent
        image: my-agent:latest
        lifecycle:
          preStop:
            exec:
              # Send graceful shutdown signal before SIGTERM
              command: ["/bin/sh", "-c", "kill -TERM 1 && sleep 5"]
        env:
        - name: SHUTDOWN_TIMEOUT
          value: "50"  # Must be < terminationGracePeriodSeconds
```

```python
import asyncio
import signal
import os
import sys

SHUTDOWN_TIMEOUT = int(os.getenv("SHUTDOWN_TIMEOUT", "25"))

class KubernetesGracefulShutdown:
    """
    Kubernetes-aware graceful shutdown.
    Handles the full lifecycle:
    1. preStop hook fires (optional)
    2. SIGTERM sent by kubelet
    3. terminationGracePeriodSeconds countdown begins
    4. SIGKILL after grace period
    """

    def __init__(self):
        self._shutdown = asyncio.Event()
        self._exit_code = 0
        self._reason = ""

    def setup(self):
        loop = asyncio.get_event_loop()
        loop.add_signal_handler(signal.SIGTERM, self._on_sigterm)
        loop.add_signal_handler(signal.SIGINT, self._on_sigint)
        print(f"Kubernetes graceful shutdown ready (timeout: {SHUTDOWN_TIMEOUT}s)")

    def _on_sigterm(self):
        print(f"SIGTERM received — will exit within {SHUTDOWN_TIMEOUT}s")
        self._reason = "SIGTERM"
        self._shutdown.set()

    def _on_sigint(self):
        print("SIGINT received — exiting")
        self._reason = "SIGINT"
        self._exit_code = 130
        self._shutdown.set()

    async def run_until_shutdown(self, main_task_fn, cleanup_fn=None):
        """
        Run main_task_fn until shutdown signal, then run cleanup_fn.
        Exits cleanly within SHUTDOWN_TIMEOUT seconds.
        """
        main_task = asyncio.create_task(main_task_fn())

        # Wait for either task completion or shutdown signal
        done, pending = await asyncio.wait(
            [main_task, asyncio.create_task(self._shutdown.wait())],
            return_when=asyncio.FIRST_COMPLETED
        )

        if self._shutdown.is_set():
            print(f"Shutdown triggered by {self._reason} — running cleanup")

            # Cancel main task
            main_task.cancel()
            try:
                await asyncio.wait_for(main_task, timeout=1.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass

            # Run cleanup with remaining time budget
            if cleanup_fn:
                try:
                    await asyncio.wait_for(cleanup_fn(), timeout=SHUTDOWN_TIMEOUT - 5)
                    print("Cleanup completed successfully")
                except asyncio.TimeoutError:
                    print(f"Cleanup timed out after {SHUTDOWN_TIMEOUT - 5}s")

        sys.exit(self._exit_code)

k8s = KubernetesGracefulShutdown()

async def agent_main():
    """Main agent loop"""
    while True:
        await asyncio.sleep(1)
        # ... agent work here

async def cleanup():
    """Cleanup on shutdown — checkpoint, close connections, flush logs"""
    print("Flushing pending writes...")
    await asyncio.sleep(2)  # Flush work
    print("Closing database connections...")
    await asyncio.sleep(1)
    print("Cleanup done")

async def run():
    k8s.setup()
    await k8s.run_until_shutdown(agent_main, cleanup)

asyncio.run(run())
```

### Option 5: Work-item acknowledgment pattern — never lose a message

```python
import asyncio
import json
from pathlib import Path
from dataclasses import dataclass, field
import time

@dataclass
class WorkItem:
    id: str
    payload: dict
    attempts: int = 0
    claimed_at: float = field(default_factory=time.time)

class AcknowledgingWorkQueue:
    """
    Work queue where items are only removed after explicit acknowledgment.
    On restart, unacknowledged items are automatically re-queued.
    Survives any shutdown — no work is ever lost.
    """

    def __init__(self, state_file: str = "/data/work_queue.json"):
        self.state_file = Path(state_file)
        self._pending: dict[str, WorkItem] = {}
        self._in_flight: dict[str, WorkItem] = {}
        self._load()

    def _load(self):
        """Load state — re-queue any in-flight items from previous run"""
        if not self.state_file.exists():
            return
        data = json.loads(self.state_file.read_text())
        self._pending = {k: WorkItem(**v) for k, v in data.get("pending", {}).items()}

        # Re-queue items that were in-flight when last killed
        for item_id, item_data in data.get("in_flight", {}).items():
            item = WorkItem(**item_data)
            item.attempts += 1
            self._pending[item_id] = item
            print(f"Re-queued in-flight item {item_id} (attempt {item.attempts})")

        print(f"Loaded queue: {len(self._pending)} pending items")

    def _save(self):
        """Persist queue state atomically"""
        data = {
            "pending": {k: v.__dict__ for k, v in self._pending.items()},
            "in_flight": {k: v.__dict__ for k, v in self._in_flight.items()}
        }
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(self.state_file)

    def enqueue(self, item_id: str, payload: dict):
        """Add item to queue"""
        if item_id not in self._pending and item_id not in self._in_flight:
            self._pending[item_id] = WorkItem(id=item_id, payload=payload)
            self._save()

    def claim(self) -> WorkItem | None:
        """Claim next pending item — it moves to in-flight"""
        if not self._pending:
            return None
        item_id, item = next(iter(self._pending.items()))
        del self._pending[item_id]
        item.claimed_at = time.time()
        self._in_flight[item_id] = item
        self._save()
        return item

    def acknowledge(self, item_id: str):
        """Mark item as complete — removes from queue permanently"""
        self._in_flight.pop(item_id, None)
        self._save()

    def nack(self, item_id: str):
        """Mark item as failed — returns to pending for retry"""
        item = self._in_flight.pop(item_id, None)
        if item:
            item.attempts += 1
            self._pending[item_id] = item
            self._save()

    @property
    def size(self) -> dict:
        return {"pending": len(self._pending), "in_flight": len(self._in_flight)}

queue = AcknowledgingWorkQueue()

async def process_with_ack(process_fn, max_attempts: int = 3):
    """Process items with acknowledgment — safe to kill at any point"""
    while True:
        item = queue.claim()
        if not item:
            await asyncio.sleep(1.0)
            continue

        if item.attempts >= max_attempts:
            print(f"Item {item.id} exceeded max attempts — skipping")
            queue.acknowledge(item.id)  # Dead-letter
            continue

        try:
            await process_fn(item.payload)
            queue.acknowledge(item.id)
            print(f"Processed and acknowledged: {item.id}")
        except Exception as e:
            print(f"Failed item {item.id}: {e} — will retry (attempt {item.attempts + 1})")
            queue.nack(item.id)
```

### Option 6: Graceful shutdown health endpoint — signal readiness to load balancer

```python
from aiohttp import web
import asyncio

class ShutdownAwareHealthServer:
    """
    Health endpoint that signals to load balancers that the agent is shutting down.
    Returns 503 on /health when shutdown begins — removes agent from rotation
    before SIGTERM arrives.
    """

    def __init__(self, port: int = 8080):
        self.port = port
        self._healthy = True
        self._shutting_down = False
        self._app = web.Application()
        self._app.router.add_get("/health", self._health)
        self._app.router.add_get("/ready", self._ready)
        self._app.router.add_post("/shutdown", self._initiate_shutdown)

    async def _health(self, request: web.Request) -> web.Response:
        """Liveness — return 200 as long as process is alive"""
        return web.json_response({"status": "alive"})

    async def _ready(self, request: web.Request) -> web.Response:
        """Readiness — return 503 when shutting down to drain traffic"""
        if self._shutting_down:
            return web.json_response(
                {"status": "shutting_down", "accepting_traffic": False},
                status=503
            )
        if not self._healthy:
            return web.json_response({"status": "not_ready"}, status=503)
        return web.json_response({"status": "ready", "accepting_traffic": True})

    async def _initiate_shutdown(self, request: web.Request) -> web.Response:
        """preStop hook can call this to signal graceful shutdown"""
        print("Shutdown initiated via HTTP — stopping traffic")
        self._shutting_down = True
        return web.json_response({"status": "shutdown_initiated"})

    def signal_shutdown(self):
        """Call this on SIGTERM to immediately stop accepting new requests"""
        self._shutting_down = True

    async def start(self):
        runner = web.AppRunner(self._app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", self.port)
        await site.start()
        print(f"Health server running on port {self.port}")

health_server = ShutdownAwareHealthServer(port=8080)
```

## Shutdown Sequence for Kubernetes Agents

| Step | Action | Timing |
|---|---|---|
| preStop hook fires | Call `/shutdown` endpoint — agent stops accepting new requests | T+0 |
| SIGTERM arrives | Signal handler fires — sets shutdown event | T+5s |
| Finish current unit | Complete the in-progress atomic unit of work | T+5s → T+30s |
| Checkpoint progress | Write current state to durable storage | T+30s |
| Close connections | Close DB, HTTP, message queue connections | T+35s |
| Process exits 0 | Clean exit — Kubernetes marks pod as terminated | T+40s |
| SIGKILL (if not done) | Kubernetes force-kills after terminationGracePeriodSeconds | T+60s |

## Expected Token Savings
Agent killed mid-task → restart → re-explain task → partial state debugging: ~25,000 tokens overhead
Graceful shutdown with checkpoint → restart resumes from last checkpoint: 0 re-explanation cost

## Environment
- Any agent deployed in Kubernetes or Docker; critical for long-running batch tasks, message queue processors, and any agent that performs multi-step operations with side effects — graceful shutdown is required for zero-work-loss deployments
- Source: direct experience; SIGTERM handling is the most commonly missing production-readiness feature in first-generation agent deployments
