---
title: "Agent Doesn't Implement Graceful Shutdown with In-Flight Request Draining"
description: "Agents that terminate immediately on SIGTERM abandon in-flight requests mid-execution: tool calls are interrupted, LLM API connections are dropped, and partial results are lost. Users receive abrupt disconnections during deployments and scaling events. Implement graceful shutdown that stops accepting new requests on SIGTERM, waits for in-flight requests to complete within a configurable drain window, and only exits after all active work is done or the timeout expires."
date: 2026-04-16
difficulty: intermediate
category: reliability
slug: agent-doesnt-implement-graceful-shutdown-with-in-flight-request-draining
tags: [graceful-shutdown, request-draining, sigterm, deployment, in-flight-requests, zero-downtime]
symptoms:
  - "Users receive connection resets during rolling deployments"
  - "In-flight LLM API calls are abandoned mid-stream on container restart"
  - "Tool call results are lost when agent process is killed during execution"
  - "No drain period between SIGTERM and process exit"
  - "Partial responses delivered to users when agent is terminated"
---

## Why This Happens

Default process termination on SIGTERM is immediate: the OS sends the signal, the process exits, all open connections are closed, and any in-flight work is abandoned. Agents that serve long-running requests — multi-step reasoning, streaming responses, batch processing — need a drain window between receiving the shutdown signal and actually exiting. The agent must stop accepting new connections, allow existing requests to run to completion, and only exit when all work is done or a maximum wait time expires.

## Solution 1: Shutdown State Machine

```python
import time
from enum import Enum
from threading import Lock
from typing import Optional


class ShutdownPhase(str, Enum):
    RUNNING = "running"
    DRAINING = "draining"     # accepting no new requests; waiting for in-flight
    TERMINATING = "terminating"  # drain window expired; hard stop
    STOPPED = "stopped"


class AgentShutdownState:
    """
    Thread-safe shutdown state machine.
    Transitions: RUNNING → DRAINING → TERMINATING → STOPPED
    """

    def __init__(self):
        self._lock = Lock()
        self._phase = ShutdownPhase.RUNNING
        self._shutdown_requested_at: Optional[float] = None

    def request_shutdown(self) -> None:
        with self._lock:
            if self._phase == ShutdownPhase.RUNNING:
                self._phase = ShutdownPhase.DRAINING
                self._shutdown_requested_at = time.time()

    def enter_terminating(self) -> None:
        with self._lock:
            if self._phase == ShutdownPhase.DRAINING:
                self._phase = ShutdownPhase.TERMINATING

    def enter_stopped(self) -> None:
        with self._lock:
            self._phase = ShutdownPhase.STOPPED

    def is_accepting(self) -> bool:
        with self._lock:
            return self._phase == ShutdownPhase.RUNNING

    def is_draining(self) -> bool:
        with self._lock:
            return self._phase == ShutdownPhase.DRAINING

    def phase(self) -> ShutdownPhase:
        with self._lock:
            return self._phase

    def time_in_drain_seconds(self) -> Optional[float]:
        with self._lock:
            if self._shutdown_requested_at is None:
                return None
            return time.time() - self._shutdown_requested_at
```

## Solution 2: In-Flight Request Tracker

```python
import asyncio
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from threading import Lock
from typing import Dict, Optional, Set


@dataclass
class InFlightRequest:
    request_id: str
    started_at: float
    description: str


class InFlightRequestTracker:
    """
    Tracks all active requests. Used during shutdown to know when
    all work has completed and the process can safely exit.
    """

    def __init__(self):
        self._lock = Lock()
        self._requests: Dict[str, InFlightRequest] = {}

    def register(self, description: str = "") -> str:
        request_id = uuid.uuid4().hex[:12]
        with self._lock:
            self._requests[request_id] = InFlightRequest(
                request_id=request_id,
                started_at=time.time(),
                description=description,
            )
        return request_id

    def complete(self, request_id: str) -> None:
        with self._lock:
            self._requests.pop(request_id, None)

    def count(self) -> int:
        with self._lock:
            return len(self._requests)

    def all_requests(self) -> list:
        with self._lock:
            return [
                {
                    "request_id": r.request_id,
                    "age_seconds": round(time.time() - r.started_at, 1),
                    "description": r.description,
                }
                for r in self._requests.values()
            ]

    @asynccontextmanager
    async def track(self, description: str = ""):
        request_id = self.register(description)
        try:
            yield request_id
        finally:
            self.complete(request_id)
```

## Solution 3: Signal Handler

```python
import asyncio
import signal
from typing import Optional


class GracefulShutdownSignalHandler:
    """
    Installs SIGTERM and SIGINT handlers that initiate graceful shutdown
    rather than immediate process termination.
    """

    def __init__(
        self,
        state: AgentShutdownState,
        loop: Optional[asyncio.AbstractEventLoop] = None,
    ):
        self._state = state
        self._loop = loop

    def install(self) -> None:
        loop = self._loop or asyncio.get_event_loop()

        def handle_signal(signum, frame):
            self._state.request_shutdown()

        signal.signal(signal.SIGTERM, handle_signal)
        signal.signal(signal.SIGINT, handle_signal)
```

## Solution 4: Drain Coordinator

```python
import asyncio
import time
from typing import Optional


class ShutdownDrainCoordinator:
    """
    Waits for all in-flight requests to complete within the drain window.
    After the window expires, forces termination.
    """

    def __init__(
        self,
        state: AgentShutdownState,
        tracker: InFlightRequestTracker,
        drain_timeout_seconds: float = 30.0,
        poll_interval_seconds: float = 0.5,
    ):
        self._state = state
        self._tracker = tracker
        self._drain_timeout = drain_timeout_seconds
        self._poll_interval = poll_interval_seconds

    async def wait_for_drain(self) -> dict:
        """
        Blocks until all requests complete or drain_timeout expires.
        Returns a drain report.
        """
        drain_start = time.time()
        while self._state.is_draining():
            in_flight = self._tracker.count()
            elapsed = time.time() - drain_start

            if in_flight == 0:
                self._state.enter_stopped()
                return {
                    "outcome": "clean_drain",
                    "elapsed_seconds": round(elapsed, 2),
                    "requests_abandoned": 0,
                }

            if elapsed >= self._drain_timeout:
                self._state.enter_terminating()
                abandoned = self._tracker.count()
                self._state.enter_stopped()
                return {
                    "outcome": "timeout",
                    "elapsed_seconds": round(elapsed, 2),
                    "requests_abandoned": abandoned,
                    "abandoned_requests": self._tracker.all_requests(),
                }

            await asyncio.sleep(self._poll_interval)

        return {
            "outcome": "stopped",
            "elapsed_seconds": round(time.time() - drain_start, 2),
            "requests_abandoned": 0,
        }
```

## Solution 5: Graceful Request Gate

```python
from contextlib import asynccontextmanager
from typing import Any


class RequestGateClosed(Exception):
    """Raised when a new request arrives during shutdown draining."""
    pass


class GracefulRequestGate:
    """
    Guards request entry points. Rejects new requests once shutdown
    begins. Existing in-flight requests continue to completion.
    """

    def __init__(
        self,
        state: AgentShutdownState,
        tracker: InFlightRequestTracker,
    ):
        self._state = state
        self._tracker = tracker

    @asynccontextmanager
    async def admit(self, description: str = ""):
        if not self._state.is_accepting():
            raise RequestGateClosed(
                "Agent is shutting down — no new requests accepted. "
                "Please retry on another instance."
            )
        async with self._tracker.track(description):
            yield
```

## Solution 6: Shutdown Orchestrator

```python
import asyncio
import time
from typing import Optional


class AgentShutdownOrchestrator:
    """
    Coordinates the full graceful shutdown sequence:
    1. Install signal handlers
    2. On SIGTERM: stop accepting new requests
    3. Wait for in-flight drain
    4. Log outcome and exit
    """

    def __init__(
        self,
        state: AgentShutdownState,
        signal_handler: GracefulShutdownSignalHandler,
        drain_coordinator: ShutdownDrainCoordinator,
        tracker: InFlightRequestTracker,
    ):
        self._state = state
        self._signal_handler = signal_handler
        self._drain = drain_coordinator
        self._tracker = tracker

    def setup(self) -> None:
        self._signal_handler.install()

    async def run_drain_on_shutdown(self) -> Optional[dict]:
        """
        Poll for shutdown signal and run drain when detected.
        Call this as a background task alongside the main server loop.
        """
        while not self._state.is_draining():
            await asyncio.sleep(0.5)

        report = await self._drain.wait_for_drain()
        return report

    def status(self) -> dict:
        return {
            "phase": self._state.phase().value,
            "in_flight_requests": self._tracker.count(),
            "time_in_drain_seconds": self._state.time_in_drain_seconds(),
            "active_requests": self._tracker.all_requests(),
        }
```

## Comparison

| Approach | SIGTERM Handling | New Request Rejection | Drain Wait | Timeout Enforcement | Status Reporting |
|---|---|---|---|---|---|
| AgentShutdownState | No | Via phase check | No | No | Yes (phase) |
| InFlightRequestTracker | No | No | No | No | Yes (count) |
| GracefulShutdownSignalHandler | Yes (SIGTERM+SIGINT) | No | No | No | No |
| ShutdownDrainCoordinator | No | No | Yes (poll) | Yes | Yes (report) |
| GracefulRequestGate | No | Yes (gate) | No | No | No |
| AgentShutdownOrchestrator | Via handler | Via gate | Via coordinator | Via coordinator | Yes |

**Best for production**: Set `drain_timeout_seconds=30` for most agents — long enough for P99 request completion but short enough to meet Kubernetes' default termination grace period of 30 seconds. Configure Kubernetes `terminationGracePeriodSeconds` to `drain_timeout + 5` to give the drain coordinator time to run before the kubelet sends SIGKILL. Use `RequestGateClosed` to return HTTP 503 with a `Retry-After: 1` header so load balancers redirect the client to a healthy instance rather than surfacing an error.
