---
title: "Agent Doesn't Implement Graceful Shutdown with In-Flight Request Draining"
description: "Agents that terminate immediately on SIGTERM drop all in-flight requests mid-execution — a user's multi-step tool chain is aborted halfway, producing partial results or corrupted state. Implement graceful shutdown that catches termination signals, stops accepting new requests, waits for in-flight requests to complete within a drain timeout, and then exits cleanly — ensuring no request is abandoned without either completing or returning a proper error."
date: 2026-04-16
difficulty: intermediate
category: reliability
slug: agent-doesnt-implement-graceful-shutdown-with-in-flight-request-draining
tags: [graceful-shutdown, sigterm, request-draining, in-flight-requests, zero-downtime, deployment]
symptoms:
  - "Deployments cause visible request failures — users see errors during rolling restarts"
  - "SIGTERM kills the process immediately regardless of how many requests are mid-execution"
  - "Long-running tool chains are interrupted mid-step during deployment windows"
  - "No mechanism to prevent the load balancer from sending new traffic while draining"
  - "Container orchestrator kills the pod before in-flight LLM streaming responses complete"
---

## Why This Happens

Container orchestrators (Kubernetes, ECS) send SIGTERM to the process when they want it to stop, then follow with SIGKILL after a grace period. A process that exits on SIGTERM immediately drops all active connections. A properly implemented graceful shutdown intercepts SIGTERM, marks the server as shutting down (so health checks begin failing, causing the load balancer to stop routing new traffic), then waits for all in-flight requests to complete before exiting. The wait is bounded by a drain timeout — requests that do not complete within the timeout are abandoned with an error, and the process exits before the orchestrator issues SIGKILL.

## Solution 1: Shutdown State Manager

```python
import asyncio
import signal
import time
from enum import Enum
from threading import Lock
from typing import Optional


class ShutdownPhase(str, Enum):
    RUNNING = "running"
    DRAINING = "draining"    # accepting no new requests, draining in-flight
    STOPPED = "stopped"


class ShutdownStateManager:
    """
    Tracks the current shutdown phase and provides thread-safe
    state transitions. Exposes a readiness flag for health checks.
    """

    def __init__(self):
        self._phase = ShutdownPhase.RUNNING
        self._shutdown_initiated_at: Optional[float] = None
        self._lock = Lock()

    def initiate_shutdown(self) -> None:
        with self._lock:
            if self._phase == ShutdownPhase.RUNNING:
                self._phase = ShutdownPhase.DRAINING
                self._shutdown_initiated_at = time.time()

    def mark_stopped(self) -> None:
        with self._lock:
            self._phase = ShutdownPhase.STOPPED

    def is_accepting_requests(self) -> bool:
        with self._lock:
            return self._phase == ShutdownPhase.RUNNING

    def is_draining(self) -> bool:
        with self._lock:
            return self._phase == ShutdownPhase.DRAINING

    def phase(self) -> ShutdownPhase:
        with self._lock:
            return self._phase

    def drain_duration_seconds(self) -> Optional[float]:
        with self._lock:
            if self._shutdown_initiated_at is None:
                return None
            return time.time() - self._shutdown_initiated_at
```

## Solution 2: In-Flight Request Tracker

```python
import asyncio
import time
from contextlib import asynccontextmanager
from threading import Lock
from typing import AsyncIterator, Dict, Set


class InFlightRequestTracker:
    """
    Tracks the set of currently executing requests.
    Provides a barrier that resolves when all in-flight requests complete.
    """

    def __init__(self):
        self._requests: Dict[str, float] = {}  # request_id -> started_at
        self._lock = Lock()
        self._drain_event = asyncio.Event()

    @asynccontextmanager
    async def track(self, request_id: str) -> AsyncIterator[None]:
        with self._lock:
            self._requests[request_id] = time.time()
        try:
            yield
        finally:
            with self._lock:
                self._requests.pop(request_id, None)
                if not self._requests:
                    self._drain_event.set()

    def in_flight_count(self) -> int:
        with self._lock:
            return len(self._requests)

    def in_flight_ids(self) -> list:
        with self._lock:
            return list(self._requests.keys())

    async def wait_for_drain(self, timeout_seconds: float = 30.0) -> bool:
        """
        Wait until all in-flight requests complete or timeout expires.
        Returns True if all drained, False if timeout exceeded.
        """
        with self._lock:
            if not self._requests:
                return True
            self._drain_event.clear()

        try:
            await asyncio.wait_for(self._drain_event.wait(), timeout=timeout_seconds)
            return True
        except asyncio.TimeoutError:
            return False
```

## Solution 3: Signal Handler

```python
import asyncio
import signal
import sys
from typing import Callable, Optional


class GracefulShutdownSignalHandler:
    """
    Registers SIGTERM and SIGINT handlers that initiate graceful shutdown
    instead of immediately terminating the process.
    """

    def __init__(
        self,
        shutdown_state: ShutdownStateManager,
        on_shutdown: Optional[Callable] = None,
    ):
        self._state = shutdown_state
        self._on_shutdown = on_shutdown
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def register(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        loop.add_signal_handler(signal.SIGTERM, self._handle_signal)
        loop.add_signal_handler(signal.SIGINT, self._handle_signal)

    def _handle_signal(self) -> None:
        if not self._state.is_accepting_requests():
            return  # already shutting down
        self._state.initiate_shutdown()
        if self._on_shutdown and self._loop:
            self._loop.call_soon_threadsafe(
                lambda: asyncio.ensure_future(self._on_shutdown())
            )
```

## Solution 4: Graceful Shutdown Orchestrator

```python
import asyncio
import time
from typing import Optional


class GracefulShutdownOrchestrator:
    """
    Coordinates the full shutdown sequence:
    1. Initiate shutdown (mark as draining, fail health checks)
    2. Wait for load balancer to stop routing new traffic
    3. Wait for in-flight requests to complete
    4. Exit cleanly
    """

    def __init__(
        self,
        state: ShutdownStateManager,
        tracker: InFlightRequestTracker,
        drain_timeout_seconds: float = 30.0,
        lb_propagation_delay_seconds: float = 5.0,
    ):
        self._state = state
        self._tracker = tracker
        self._drain_timeout = drain_timeout_seconds
        self._lb_delay = lb_propagation_delay_seconds

    async def shutdown(self) -> dict:
        start = time.time()
        self._state.initiate_shutdown()

        # Give load balancer time to see failing health checks and stop routing
        if self._lb_delay > 0:
            await asyncio.sleep(self._lb_delay)

        in_flight_at_start = self._tracker.in_flight_count()
        remaining_drain_timeout = max(self._drain_timeout - self._lb_delay, 1.0)

        drained = await self._tracker.wait_for_drain(remaining_drain_timeout)

        self._state.mark_stopped()
        elapsed = time.time() - start

        return {
            "shutdown_completed": True,
            "drained_cleanly": drained,
            "in_flight_at_shutdown": in_flight_at_start,
            "remaining_in_flight": self._tracker.in_flight_count(),
            "total_shutdown_seconds": round(elapsed, 2),
        }
```

## Solution 5: Request Gate

```python
from typing import Any, Callable


class RequestGate:
    """
    Guards request handlers: passes requests through when running,
    returns a 503 Service Unavailable response when draining or stopped.
    """

    SERVICE_UNAVAILABLE_BODY = {
        "error": "service_shutting_down",
        "message": "The agent is shutting down. Please retry shortly.",
        "retry_after_seconds": 5,
    }

    def __init__(self, state: ShutdownStateManager):
        self._state = state

    def is_open(self) -> bool:
        return self._state.is_accepting_requests()

    async def gate(
        self,
        request_fn: Callable,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        if not self._state.is_accepting_requests():
            return self.SERVICE_UNAVAILABLE_BODY
        return await request_fn(*args, **kwargs)
```

## Solution 6: Graceful Shutdown Dashboard

```python
import time


class GracefulShutdownDashboard:
    """
    Provides a real-time view of shutdown progress for operators.
    """

    def __init__(
        self,
        state: ShutdownStateManager,
        tracker: InFlightRequestTracker,
    ):
        self._state = state
        self._tracker = tracker

    def render(self) -> dict:
        phase = self._state.phase()
        drain_duration = self._state.drain_duration_seconds()
        return {
            "generated_at": time.time(),
            "shutdown_phase": phase.value,
            "accepting_requests": self._state.is_accepting_requests(),
            "in_flight_count": self._tracker.in_flight_count(),
            "in_flight_ids": self._tracker.in_flight_ids()[:10],
            "drain_duration_seconds": round(drain_duration, 1) if drain_duration else None,
        }
```

## Comparison

| Approach | Signal Handling | Request Gating | Drain Wait | LB Propagation | Status Dashboard |
|---|---|---|---|---|---|
| ShutdownStateManager | No | Via phase check | No | No | No |
| InFlightRequestTracker | No | No | Yes (asyncio.Event) | No | No |
| GracefulShutdownSignalHandler | Yes (SIGTERM/SIGINT) | No | No | No | No |
| GracefulShutdownOrchestrator | Via handler | No | Via tracker | Yes (sleep) | No |
| RequestGate | No | Yes (503 when draining) | No | No | No |
| GracefulShutdownDashboard | No | No | No | No | Yes |

**Best for production**: Set `drain_timeout_seconds=30` and `lb_propagation_delay_seconds=5` — Kubernetes default terminationGracePeriodSeconds is 30 seconds, giving 5 seconds for load balancer convergence and 25 seconds for actual request draining. Ensure the Kubernetes liveness probe continues to pass during draining (the process is still alive) while the readiness probe fails (so no new requests are routed). Return HTTP 503 from the readiness endpoint the moment `initiate_shutdown()` is called — this is the signal that causes Kubernetes ingress controllers and service meshes to stop routing. Log the `GracefulShutdownDashboard.render()` output every 5 seconds during draining so operators can monitor drain progress in deployment logs.
