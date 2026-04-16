---
title: "Agent Doesn't Implement Graceful Shutdown with In-Flight Task Completion"
description: "Agents that terminate immediately on SIGTERM interrupt active sessions mid-task: tool calls are abandoned, partial LLM responses are truncated, and database transactions are left uncommitted. Users receive no response and must retry from scratch. Implement graceful shutdown that catches termination signals, stops accepting new sessions, allows in-flight tasks to complete within a configurable drain window, and persists incomplete sessions for replay before exiting."
date: 2026-04-16
difficulty: intermediate
category: reliability
slug: agent-doesnt-implement-graceful-shutdown-with-in-flight-task-completion
tags: [graceful-shutdown, sigterm, drain-window, in-flight-tasks, deployment-safety, signal-handling]
symptoms:
  - "Deployments interrupt active user sessions — users see mid-sentence truncated responses"
  - "Tool calls abandoned on shutdown leave downstream services in inconsistent state"
  - "No drain window — SIGTERM immediately kills all in-flight requests"
  - "Kubernetes rolling deployments lose requests during pod termination"
  - "Database transactions left open after agent process exits"
---

## Why This Happens

Containerized agents receive SIGTERM before being forcibly killed. The default Python signal handler raises `SystemExit`, which unwinds the call stack and terminates immediately — interrupting any in-progress asyncio coroutines, thread pool tasks, and database transactions. Graceful shutdown requires three changes: catch SIGTERM and set a shutdown flag; stop the server from accepting new connections; and wait for in-flight tasks to complete up to a drain window duration before exiting. For tasks that cannot complete in time, state should be persisted for replay after the new instance starts.

## Solution 1: Shutdown State Machine

```python
import asyncio
import signal
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class ShutdownState(str, Enum):
    RUNNING = "running"
    DRAINING = "draining"      # no new tasks accepted; existing tasks finishing
    PERSISTING = "persisting"  # saving incomplete state for replay
    TERMINATED = "terminated"


@dataclass
class ShutdownContext:
    state: ShutdownState = ShutdownState.RUNNING
    shutdown_requested_at: Optional[float] = None
    drain_deadline: Optional[float] = None
    signal_received: Optional[str] = None
    in_flight_at_shutdown: int = 0
    completed_during_drain: int = 0
    persisted_for_replay: int = 0

    def is_accepting_new_tasks(self) -> bool:
        return self.state == ShutdownState.RUNNING

    def time_remaining_in_drain(self) -> float:
        if self.drain_deadline is None:
            return 0.0
        return max(0.0, self.drain_deadline - time.time())
```

## Solution 2: Signal Handler

```python
import asyncio
import signal
import time
from typing import Callable, Optional


class GracefulShutdownSignalHandler:
    """
    Registers SIGTERM and SIGINT handlers that transition the agent
    to draining mode rather than immediately terminating.
    """

    def __init__(
        self,
        context: ShutdownContext,
        drain_window_seconds: float = 30.0,
        on_shutdown: Optional[Callable] = None,
    ):
        self._ctx = context
        self._drain_window = drain_window_seconds
        self._on_shutdown = on_shutdown

    def register(self) -> None:
        signal.signal(signal.SIGTERM, self._handle)
        signal.signal(signal.SIGINT, self._handle)

    def register_async(self, loop: asyncio.AbstractEventLoop) -> None:
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, lambda s=sig: self._handle_async(s))

    def _handle(self, signum: int, frame) -> None:
        self._initiate_drain(signal.Signals(signum).name)

    def _handle_async(self, signum: int) -> None:
        self._initiate_drain(signal.Signals(signum).name)

    def _initiate_drain(self, signal_name: str) -> None:
        if self._ctx.state != ShutdownState.RUNNING:
            return   # already shutting down
        now = time.time()
        self._ctx.state = ShutdownState.DRAINING
        self._ctx.shutdown_requested_at = now
        self._ctx.drain_deadline = now + self._drain_window
        self._ctx.signal_received = signal_name
        if self._on_shutdown:
            self._on_shutdown(self._ctx)
```

## Solution 3: In-Flight Task Tracker

```python
import asyncio
import time
from contextlib import asynccontextmanager
from threading import Lock
from typing import Dict, Optional, Set


class InFlightTaskTracker:
    """
    Tracks active tasks so the shutdown coordinator can wait for them.
    Tasks register themselves on start and deregister on completion.
    """

    def __init__(self):
        self._tasks: Dict[str, float] = {}   # task_id -> started_at
        self._lock = Lock()

    def register(self, task_id: str) -> None:
        with self._lock:
            self._tasks[task_id] = time.time()

    def deregister(self, task_id: str) -> None:
        with self._lock:
            self._tasks.pop(task_id, None)

    def count(self) -> int:
        with self._lock:
            return len(self._tasks)

    def oldest_task_age_seconds(self) -> Optional[float]:
        with self._lock:
            if not self._tasks:
                return None
            return round(time.time() - min(self._tasks.values()), 2)

    @asynccontextmanager
    async def track(self, task_id: str):
        self.register(task_id)
        try:
            yield
        finally:
            self.deregister(task_id)
```

## Solution 4: Drain Coordinator

```python
import asyncio
import time
from typing import Optional


class DrainCoordinator:
    """
    Waits for in-flight tasks to complete during the drain window.
    Polls at short intervals and exits as soon as all tasks finish
    or the drain window expires.
    """

    def __init__(
        self,
        context: ShutdownContext,
        tracker: InFlightTaskTracker,
        poll_interval_seconds: float = 0.5,
    ):
        self._ctx = context
        self._tracker = tracker
        self._poll = poll_interval_seconds

    async def drain(self) -> dict:
        self._ctx.in_flight_at_shutdown = self._tracker.count()
        start = time.time()

        while self._tracker.count() > 0:
            remaining = self._ctx.time_remaining_in_drain()
            if remaining <= 0:
                break
            await asyncio.sleep(min(self._poll, remaining))

        drained_count = (
            self._ctx.in_flight_at_shutdown - self._tracker.count()
        )
        self._ctx.completed_during_drain = drained_count
        elapsed = round(time.time() - start, 2)

        return {
            "in_flight_at_shutdown": self._ctx.in_flight_at_shutdown,
            "completed_during_drain": drained_count,
            "still_in_flight": self._tracker.count(),
            "drain_elapsed_seconds": elapsed,
        }
```

## Solution 5: Incomplete Session Persister

```python
import json
import time
from pathlib import Path
from typing import Any, Dict, List


class IncompleteSessionPersister:
    """
    Persists state of sessions that could not complete during the drain window.
    On next startup, sessions can be restored and replayed.
    """

    def __init__(self, persist_path: str = "/tmp/agent_incomplete_sessions.json"):
        self._path = Path(persist_path)

    def persist(self, sessions: List[Dict[str, Any]]) -> int:
        existing = self._load()
        for session in sessions:
            session["persisted_at"] = time.time()
            existing[session.get("session_id", str(time.time()))] = session
        self._path.write_text(json.dumps(existing, indent=2))
        return len(sessions)

    def load_pending(self) -> List[Dict[str, Any]]:
        sessions = self._load()
        return list(sessions.values())

    def clear(self, session_id: str) -> None:
        sessions = self._load()
        sessions.pop(session_id, None)
        self._path.write_text(json.dumps(sessions, indent=2))

    def _load(self) -> dict:
        if not self._path.exists():
            return {}
        try:
            return json.loads(self._path.read_text())
        except (json.JSONDecodeError, OSError):
            return {}

    def count_pending(self) -> int:
        return len(self._load())
```

## Solution 6: Graceful Shutdown Orchestrator

```python
import asyncio
import time
from typing import Any, Callable, Dict, List, Optional


class GracefulShutdownOrchestrator:
    """
    Coordinates the full graceful shutdown sequence:
    1. Signal received → stop accepting new tasks
    2. Drain window → wait for in-flight tasks
    3. Persist incomplete → save unfinished sessions
    4. Cleanup → close connections, flush logs
    5. Exit
    """

    def __init__(
        self,
        context: ShutdownContext,
        signal_handler: GracefulShutdownSignalHandler,
        tracker: InFlightTaskTracker,
        drain_coordinator: DrainCoordinator,
        persister: IncompleteSessionPersister,
        cleanup_fn: Optional[Callable] = None,
    ):
        self._ctx = context
        self._signal = signal_handler
        self._tracker = tracker
        self._drain = drain_coordinator
        self._persister = persister
        self._cleanup = cleanup_fn

    def setup(self, loop: asyncio.AbstractEventLoop) -> None:
        self._signal.register_async(loop)

    async def wait_for_shutdown(self) -> dict:
        """Blocks until shutdown is requested, then runs the shutdown sequence."""
        while self._ctx.state == ShutdownState.RUNNING:
            await asyncio.sleep(0.5)

        drain_result = await self._drain.drain()

        # Persist incomplete sessions
        # (caller supplies incomplete session data)
        self._ctx.state = ShutdownState.PERSISTING
        pending_count = self._persister.count_pending()

        # Cleanup
        if self._cleanup:
            await self._cleanup()

        self._ctx.state = ShutdownState.TERMINATED

        return {
            "signal": self._ctx.signal_received,
            "shutdown_duration_seconds": round(
                time.time() - self._ctx.shutdown_requested_at, 2
            ),
            **drain_result,
            "pending_sessions_persisted": pending_count,
        }
```

## Comparison

| Approach | Signal Handling | In-Flight Tracking | Drain Window | State Persistence | Full Orchestration |
|---|---|---|---|---|---|
| GracefulShutdownSignalHandler | Yes (SIGTERM/SIGINT) | No | No | No | No |
| InFlightTaskTracker | No | Yes (context manager) | No | No | No |
| DrainCoordinator | No | Via tracker | Yes | No | No |
| IncompleteSessionPersister | No | No | No | Yes | No |
| GracefulShutdownOrchestrator | Via handler | Via tracker | Via coordinator | Via persister | Yes |

**Best for production**: Set `drain_window_seconds=30` and configure the container orchestrator's `terminationGracePeriodSeconds` to at least `drain_window + 10` — Kubernetes kills the pod after `terminationGracePeriodSeconds`, so the drain window must fit within it. Use `InFlightTaskTracker` as an async context manager (`async with tracker.track(task_id)`) in every request handler — this prevents forgetting to deregister on exceptions. Persist incomplete sessions to a shared store (Redis, database) rather than a local file so the next pod instance can pick up incomplete work even if it starts on a different host. Log the drain result as a structured event at shutdown: `drain_elapsed_seconds`, `completed_during_drain`, and `still_in_flight` tell you whether your drain window is calibrated correctly (still_in_flight > 0 means the window is too short).
