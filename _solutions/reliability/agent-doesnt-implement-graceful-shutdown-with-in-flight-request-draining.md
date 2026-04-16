---
title: "Agent Doesn't Implement Graceful Shutdown with In-Flight Request Draining"
description: "Agents that terminate immediately on SIGTERM abort all in-flight requests mid-execution — leaving tool calls incomplete, database transactions uncommitted, and users receiving connection reset errors. Implement graceful shutdown that stops accepting new requests, waits for in-flight requests to complete within a drain timeout, and forces termination only if draining exceeds the deadline."
date: 2026-04-16
difficulty: intermediate
category: reliability
slug: agent-doesnt-implement-graceful-shutdown-with-in-flight-request-draining
tags: [graceful-shutdown, sigterm, request-draining, in-flight, zero-downtime, shutdown-hook]
symptoms:
  - "Users receive 'connection reset' errors during deployments as the old pod is terminated"
  - "Tool calls are aborted mid-execution when the container receives SIGTERM"
  - "Database transactions are left uncommitted after abrupt process termination"
  - "No shutdown hook — the process exits immediately on signal regardless of in-flight work"
  - "Kubernetes rolling deployments cause brief error spikes from terminated pods"
---

## Why This Happens

Container orchestrators (Kubernetes, ECS) send SIGTERM before killing a process, giving it a grace period (typically 30 seconds) to finish in-flight work. Without a signal handler that stops new request intake and waits for active requests to complete, the process exits immediately and all active work is lost. Graceful shutdown requires: (1) catching SIGTERM, (2) marking the instance as shutting down (stop accepting new work), (3) waiting for in-flight work to complete, and (4) exiting cleanly.

## Solution 1: Shutdown State Tracker

```python
import asyncio
import signal
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class ShutdownPhase(str, Enum):
    RUNNING = "running"
    DRAINING = "draining"      # no new requests; existing requests still processing
    TERMINATED = "terminated"  # all done; safe to exit


@dataclass
class ShutdownState:
    phase: ShutdownPhase = ShutdownPhase.RUNNING
    drain_started_at: Optional[float] = None
    drain_deadline: Optional[float] = None
    shutdown_reason: str = ""
    in_flight_count: int = 0

    def is_accepting(self) -> bool:
        return self.phase == ShutdownPhase.RUNNING

    def drain_remaining_seconds(self) -> Optional[float]:
        if self.drain_deadline is None:
            return None
        return max(0.0, self.drain_deadline - time.time())
```

## Solution 2: In-Flight Request Tracker

```python
import asyncio
from contextlib import asynccontextmanager
from threading import Lock
from typing import AsyncGenerator, Set


class InFlightRequestTracker:
    """
    Tracks the count and IDs of currently executing requests.
    Provides an async event that fires when in-flight count reaches zero.
    """

    def __init__(self):
        self._active: Set[str] = set()
        self._lock = Lock()
        self._drained = asyncio.Event()
        self._drained.set()   # starts drained (no requests)

    def start(self, request_id: str) -> None:
        with self._lock:
            self._active.add(request_id)
            self._drained.clear()

    def finish(self, request_id: str) -> None:
        with self._lock:
            self._active.discard(request_id)
            if not self._active:
                self._drained.set()

    def count(self) -> int:
        with self._lock:
            return len(self._active)

    def active_ids(self) -> Set[str]:
        with self._lock:
            return set(self._active)

    async def wait_drained(self, timeout_seconds: float = 30.0) -> bool:
        try:
            await asyncio.wait_for(self._drained.wait(), timeout=timeout_seconds)
            return True
        except asyncio.TimeoutError:
            return False

    @asynccontextmanager
    async def track(self, request_id: str) -> AsyncGenerator[None, None]:
        self.start(request_id)
        try:
            yield
        finally:
            self.finish(request_id)
```

## Solution 3: Graceful Shutdown Manager

```python
import asyncio
import signal
import time
from typing import Callable, List, Optional


class GracefulShutdownManager:
    """
    Handles SIGTERM and SIGINT by transitioning to drain mode,
    waiting for in-flight requests, and executing shutdown hooks.
    """

    def __init__(
        self,
        state: ShutdownState,
        tracker: InFlightRequestTracker,
        drain_timeout_seconds: float = 30.0,
    ):
        self._state = state
        self._tracker = tracker
        self._drain_timeout = drain_timeout_seconds
        self._hooks: List[Callable] = []
        self._shutdown_complete = asyncio.Event()

    def register_hook(self, hook: Callable) -> None:
        """Register an async cleanup function to call after draining."""
        self._hooks.append(hook)

    def install_signal_handlers(self) -> None:
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(
                sig,
                lambda s=sig: asyncio.create_task(self._handle_signal(s))
            )

    async def _handle_signal(self, sig: signal.Signals) -> None:
        if self._state.phase != ShutdownPhase.RUNNING:
            return
        self._state.phase = ShutdownPhase.DRAINING
        self._state.drain_started_at = time.time()
        self._state.drain_deadline = time.time() + self._drain_timeout
        self._state.shutdown_reason = sig.name

        drained = await self._tracker.wait_drained(self._drain_timeout)

        # Run shutdown hooks regardless
        for hook in self._hooks:
            try:
                await hook()
            except Exception:
                pass

        self._state.phase = ShutdownPhase.TERMINATED
        self._shutdown_complete.set()

    async def wait_for_shutdown(self) -> None:
        await self._shutdown_complete.wait()

    def is_accepting(self) -> bool:
        return self._state.is_accepting()
```

## Solution 4: Shutdown-Aware Request Gate

```python
import secrets
from typing import Any, Callable


class ShutdownAwareRequestGate:
    """
    Entry point for all requests. Rejects new requests during drain/terminated phases
    and tracks in-flight work using the request tracker.
    """

    def __init__(
        self,
        shutdown_manager: GracefulShutdownManager,
        tracker: InFlightRequestTracker,
    ):
        self._manager = shutdown_manager
        self._tracker = tracker
        self._rejected_during_drain = 0

    async def handle(
        self,
        fn: Callable,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        if not self._manager.is_accepting():
            self._rejected_during_drain += 1
            raise ShutdownInProgressError()

        request_id = secrets.token_hex(8)
        async with self._tracker.track(request_id):
            return await fn(*args, **kwargs)

    def rejected_count(self) -> int:
        return self._rejected_during_drain


class ShutdownInProgressError(Exception):
    def __init__(self):
        super().__init__("agent is shutting down — retry on another instance")
```

## Solution 5: Drain Progress Reporter

```python
import time
from typing import Optional


class DrainProgressReporter:
    """
    Emits structured progress during the drain phase for logging
    and external health check endpoints.
    """

    def __init__(
        self,
        state: ShutdownState,
        tracker: InFlightRequestTracker,
    ):
        self._state = state
        self._tracker = tracker

    def report(self) -> dict:
        remaining = self._state.drain_remaining_seconds()
        return {
            "phase": self._state.phase.value,
            "in_flight": self._tracker.count(),
            "drain_remaining_seconds": round(remaining, 1) if remaining else None,
            "shutdown_reason": self._state.shutdown_reason,
            "active_request_ids": list(self._tracker.active_ids())[:10],
        }

    def is_safe_to_exit(self) -> bool:
        return (
            self._state.phase == ShutdownPhase.TERMINATED
            or (
                self._state.phase == ShutdownPhase.DRAINING
                and self._tracker.count() == 0
            )
        )
```

## Solution 6: Graceful Shutdown Dashboard

```python
import time


class GracefulShutdownDashboard:
    """Combines shutdown state, drain progress, and gate rejection stats."""

    def __init__(
        self,
        manager: GracefulShutdownManager,
        gate: ShutdownAwareRequestGate,
        reporter: DrainProgressReporter,
    ):
        self._manager = manager
        self._gate = gate
        self._reporter = reporter

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "drain_progress": self._reporter.report(),
            "rejections_during_drain": self._gate.rejected_count(),
            "safe_to_exit": self._reporter.is_safe_to_exit(),
        }
```

## Comparison

| Approach | Signal Handling | In-Flight Tracking | Drain Wait | New Request Rejection | Hooks |
|---|---|---|---|---|---|
| InFlightRequestTracker | No | Yes (async event) | Yes (wait_drained) | No | No |
| GracefulShutdownManager | Yes (SIGTERM/INT) | Via tracker | Via tracker | No | Yes |
| ShutdownAwareRequestGate | No | Via tracker | No | Yes (HTTP 503) | No |
| DrainProgressReporter | No | Via tracker | No | No | No |
| GracefulShutdownDashboard | No | No | No | No | No |

**Best for production**: Set `drain_timeout_seconds` equal to your Kubernetes `terminationGracePeriodSeconds` minus 5 seconds — the orchestrator hard-kills the process at its deadline, so the agent must complete draining before that. Return HTTP 503 for requests rejected during drain so load balancers immediately route new requests to healthy pods. Register a shutdown hook that closes database connection pools, flushes metrics, and cancels background timers — leaked connections outlive the process and can exhaust pool limits in the next deployment. Log `in_flight_count` every 2 seconds during drain for operational visibility into whether draining is making progress.
