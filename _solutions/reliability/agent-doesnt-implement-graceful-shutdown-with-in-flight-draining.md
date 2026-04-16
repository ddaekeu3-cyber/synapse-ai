---
layout: solution
title: "Agent Doesn't Implement Graceful Shutdown with In-Flight Request Draining"
category: reliability
description: "Agents that terminate abruptly on SIGTERM or pod eviction drop in-flight requests, corrupt partial writes, and leave users without responses. Graceful shutdown stops accepting new work, waits for active requests to finish, then exits cleanly."
tags: [reliability, shutdown, draining, signals, kubernetes, python]
---

## Problem

When a process receives SIGTERM (Kubernetes pod eviction, rolling deploy, scale-down), it has a brief window to finish work before SIGKILL fires. Agents without graceful shutdown handling drop in-flight API calls mid-stream, lose queued tasks, and leave database transactions uncommitted. Users see connection resets; operators see data inconsistency.

## Solutions

### Option 1: SIGTERM Handler with Active Request Counter

```python
import anthropic
import asyncio
import signal
import time
from dataclasses import dataclass, field
from typing import Optional

@dataclass
class ShutdownState:
    is_shutting_down: bool = False
    active_requests: int = 0
    shutdown_deadline: float = 0.0
    drain_timeout_seconds: float = 30.0

    def begin_shutdown(self) -> None:
        self.is_shutting_down = True
        self.shutdown_deadline = time.monotonic() + self.drain_timeout_seconds
        print(f"[SHUTDOWN] Draining {self.active_requests} in-flight requests "
              f"(deadline in {self.drain_timeout_seconds:.0f}s)")

    @property
    def time_remaining(self) -> float:
        return max(0.0, self.shutdown_deadline - time.monotonic())

    @property
    def timed_out(self) -> bool:
        return self.is_shutting_down and time.monotonic() >= self.shutdown_deadline

state = ShutdownState()

async def process_request(client: anthropic.AsyncAnthropic,
                           request_id: str, prompt: str) -> Optional[str]:
    if state.is_shutting_down:
        print(f"[REJECT] {request_id}: server shutting down")
        return None

    state.active_requests += 1
    print(f"[START] {request_id} (active={state.active_requests})")
    try:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=80,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text
    except Exception as e:
        print(f"[ERROR] {request_id}: {e}")
        return None
    finally:
        state.active_requests -= 1
        print(f"[DONE] {request_id} (active={state.active_requests})")

async def drain_and_exit(loop: asyncio.AbstractEventLoop) -> None:
    state.begin_shutdown()
    while state.active_requests > 0 and not state.timed_out:
        await asyncio.sleep(0.1)

    if state.active_requests > 0:
        print(f"[SHUTDOWN] Drain timeout — {state.active_requests} requests still active")
    else:
        print(f"[SHUTDOWN] All requests drained cleanly")

    loop.stop()

async def main():
    client = anthropic.AsyncAnthropic()
    loop = asyncio.get_running_loop()

    def handle_sigterm():
        print("\n[SIGTERM received]")
        asyncio.create_task(drain_and_exit(loop))

    loop.add_signal_handler(signal.SIGTERM, handle_sigterm)
    loop.add_signal_handler(signal.SIGINT, handle_sigterm)

    # Simulate concurrent requests
    prompts = [
        ("req-001", "Explain gravity in one sentence."),
        ("req-002", "What is photosynthesis?"),
        ("req-003", "Name three elements."),
        ("req-004", "What is 7 × 8?"),
    ]

    tasks = [asyncio.create_task(process_request(client, rid, p))
             for rid, p in prompts]

    # Simulate SIGTERM mid-flight
    await asyncio.sleep(0.3)
    handle_sigterm()

    results = await asyncio.gather(*tasks, return_exceptions=True)
    for (rid, _), result in zip(prompts, results):
        if isinstance(result, str):
            print(f"[RESULT] {rid}: {result[:50]}")
        elif result is None:
            print(f"[RESULT] {rid}: rejected or failed")

if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: N/A — graceful shutdown preserves completed work, avoiding re-runs
# Environment: pip install anthropic
```

### Option 2: Context Manager with Request Lifecycle Tracking

```python
import anthropic
import asyncio
import contextlib
import time
from dataclasses import dataclass, field
from typing import Optional

@dataclass
class RequestTracker:
    _active: dict[str, float] = field(default_factory=dict)  # id → start_time
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _shutdown: asyncio.Event = field(default_factory=asyncio.Event)
    _all_done: asyncio.Event = field(default_factory=asyncio.Event)
    drain_timeout: float = 25.0

    def __post_init__(self):
        self._all_done.set()  # Initially nothing active

    @contextlib.asynccontextmanager
    async def track(self, request_id: str):
        if self._shutdown.is_set():
            raise RuntimeError(f"Server is shutting down, rejecting {request_id}")
        async with self._lock:
            self._active[request_id] = time.monotonic()
            self._all_done.clear()
        try:
            yield
        finally:
            async with self._lock:
                elapsed = time.monotonic() - self._active.pop(request_id, time.monotonic())
                print(f"[LIFECYCLE] {request_id} completed in {elapsed*1000:.0f}ms "
                      f"({len(self._active)} remaining)")
                if not self._active:
                    self._all_done.set()

    async def initiate_shutdown(self) -> None:
        self._shutdown.set()
        print(f"[DRAIN] Waiting for {len(self._active)} active requests "
              f"(timeout={self.drain_timeout}s)")
        try:
            await asyncio.wait_for(self._all_done.wait(), timeout=self.drain_timeout)
            print("[DRAIN] All requests completed cleanly")
        except asyncio.TimeoutError:
            print(f"[DRAIN TIMEOUT] {len(self._active)} requests abandoned: "
                  f"{list(self._active.keys())}")

    @property
    def active_count(self) -> int:
        return len(self._active)

tracker = RequestTracker()

async def handle_request(client: anthropic.AsyncAnthropic,
                          request_id: str, prompt: str) -> Optional[str]:
    try:
        async with tracker.track(request_id):
            response = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=60,
                messages=[{"role": "user", "content": prompt}],
            )
            return response.content[0].text
    except RuntimeError as e:
        print(f"[REJECTED] {e}")
        return None

async def main():
    client = anthropic.AsyncAnthropic()

    # Start requests concurrently
    tasks = [
        asyncio.create_task(handle_request(client, f"req-{i:03d}", p))
        for i, p in enumerate([
            "What is the speed of light?",
            "Name a planet.",
            "Define entropy.",
            "What is Python?",
        ])
    ]

    # Trigger graceful shutdown after brief delay
    await asyncio.sleep(0.5)
    asyncio.create_task(tracker.initiate_shutdown())

    # Try one more request (should be rejected)
    rejected = await handle_request(client, "req-late", "This should be rejected.")
    print(f"Late request: {'rejected' if rejected is None else 'accepted'}")

    results = await asyncio.gather(*tasks, return_exceptions=True)
    successful = sum(1 for r in results if isinstance(r, str))
    print(f"\nCompleted: {successful}/{len(tasks)} requests")

if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: N/A — context manager ensures clean teardown without abandoned calls
# Environment: pip install anthropic
```

### Option 3: HTTP Server with /healthz + /drain Endpoints

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass, field
from typing import Optional

@dataclass
class ServerHealth:
    ready: bool = True            # Readiness: should we receive traffic?
    live: bool = True             # Liveness: is the process healthy?
    draining: bool = False        # Are we actively draining?
    active_requests: int = 0
    total_served: int = 0
    drain_started_at: Optional[float] = None

    def status(self) -> dict:
        return {
            "ready": self.ready and not self.draining,
            "live": self.live,
            "draining": self.draining,
            "active": self.active_requests,
            "total_served": self.total_served,
        }

class GracefulAgentServer:
    def __init__(self, drain_timeout: float = 30.0):
        self.health = ServerHealth()
        self._drain_timeout = drain_timeout
        self._drain_event = asyncio.Event()
        self._lock = asyncio.Lock()

    async def begin_drain(self) -> None:
        async with self._lock:
            self.health.ready = False
            self.health.draining = True
            self.health.drain_started_at = time.monotonic()

        print(f"[DRAIN] Starting drain, {self.health.active_requests} active requests")

        deadline = time.monotonic() + self._drain_timeout
        while self.health.active_requests > 0:
            if time.monotonic() >= deadline:
                print(f"[DRAIN TIMEOUT] Forcing shutdown with "
                      f"{self.health.active_requests} active requests")
                break
            await asyncio.sleep(0.05)

        self.health.live = False
        elapsed = time.monotonic() - (self.health.drain_started_at or time.monotonic())
        print(f"[DRAIN COMPLETE] {elapsed*1000:.0f}ms | "
              f"served={self.health.total_served}")
        self._drain_event.set()

    async def handle_request(self, client: anthropic.AsyncAnthropic,
                              request_id: str, prompt: str) -> Optional[dict]:
        if not self.health.ready or self.health.draining:
            return {"error": "503 Service Unavailable — draining", "id": request_id}

        async with self._lock:
            self.health.active_requests += 1

        try:
            t0 = time.monotonic()
            response = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=60,
                messages=[{"role": "user", "content": prompt}],
            )
            latency = (time.monotonic() - t0) * 1000
            async with self._lock:
                self.health.total_served += 1
            return {
                "id": request_id,
                "text": response.content[0].text,
                "latency_ms": latency,
            }
        except Exception as e:
            return {"error": str(e), "id": request_id}
        finally:
            async with self._lock:
                self.health.active_requests -= 1

    async def run(self, client: anthropic.AsyncAnthropic,
                  requests: list[tuple[str, str]]) -> None:
        # Handle requests
        tasks = [
            asyncio.create_task(self.handle_request(client, rid, p))
            for rid, p in requests
        ]

        # Simulate drain signal mid-flight
        await asyncio.sleep(0.4)
        print(f"\n[SERVER] Health: {self.health.status()}")
        drain_task = asyncio.create_task(self.begin_drain())

        results = await asyncio.gather(*tasks, return_exceptions=True)
        await drain_task

        for result in results:
            if isinstance(result, dict):
                if "error" in result:
                    print(f"[503] {result['id']}: {result['error'][:40]}")
                else:
                    print(f"[OK] {result['id']} {result['latency_ms']:.0f}ms: "
                          f"{result['text'][:45]}")

async def main():
    client = anthropic.AsyncAnthropic()
    server = GracefulAgentServer(drain_timeout=20.0)
    await server.run(client, [
        ("r1", "What is AI?"), ("r2", "Define recursion."),
        ("r3", "Name a planet."), ("r4", "What is 6×7?"),
    ])

if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: N/A — k8s readiness probe integration prevents traffic to draining pods
# Environment: pip install anthropic
```

### Option 4: Worker Pool with Graceful Worker Shutdown

```python
import anthropic
import asyncio
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

class WorkerState(Enum):
    IDLE = "idle"
    BUSY = "busy"
    DRAINING = "draining"
    STOPPED = "stopped"

@dataclass
class Worker:
    worker_id: str
    state: WorkerState = WorkerState.IDLE
    current_request_id: Optional[str] = None
    requests_handled: int = 0
    started_at: float = field(default_factory=time.monotonic)

class GracefulWorkerPool:
    def __init__(self, n_workers: int = 3, drain_timeout: float = 30.0):
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=50)
        self._workers: dict[str, Worker] = {}
        self._n_workers = n_workers
        self._drain_timeout = drain_timeout
        self._shutdown = asyncio.Event()
        self._client = anthropic.AsyncAnthropic()

    async def _worker_loop(self, worker: Worker) -> None:
        while not self._shutdown.is_set() or not self._queue.empty():
            try:
                request_id, prompt = await asyncio.wait_for(
                    self._queue.get(), timeout=0.5
                )
            except asyncio.TimeoutError:
                if self._shutdown.is_set():
                    break
                continue

            worker.state = WorkerState.BUSY
            worker.current_request_id = request_id
            try:
                response = await self._client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=60,
                    messages=[{"role": "user", "content": prompt}],
                )
                worker.requests_handled += 1
                print(f"[{worker.worker_id}] {request_id}: "
                      f"{response.content[0].text[:50]}")
            except Exception as e:
                print(f"[{worker.worker_id}] ERROR {request_id}: {e}")
            finally:
                worker.current_request_id = None
                worker.state = (WorkerState.DRAINING if self._shutdown.is_set()
                                 else WorkerState.IDLE)
                self._queue.task_done()

        worker.state = WorkerState.STOPPED
        elapsed = time.monotonic() - worker.started_at
        print(f"[{worker.worker_id}] Stopped after {elapsed:.1f}s, "
              f"handled {worker.requests_handled} requests")

    async def start(self) -> list[asyncio.Task]:
        tasks = []
        for i in range(self._n_workers):
            wid = f"worker-{i}"
            worker = Worker(wid)
            self._workers[wid] = worker
            tasks.append(asyncio.create_task(self._worker_loop(worker)))
        return tasks

    async def submit(self, request_id: str, prompt: str) -> bool:
        if self._shutdown.is_set():
            print(f"[REJECT] {request_id}: pool shutting down")
            return False
        try:
            self._queue.put_nowait((request_id, prompt))
            return True
        except asyncio.QueueFull:
            return False

    async def shutdown(self) -> None:
        print(f"\n[POOL] Initiating shutdown. Queue: {self._queue.qsize()} pending. "
              f"Busy workers: {sum(1 for w in self._workers.values() if w.state == WorkerState.BUSY)}")
        self._shutdown.set()
        try:
            await asyncio.wait_for(self._queue.join(), timeout=self._drain_timeout)
            print("[POOL] All queued work completed")
        except asyncio.TimeoutError:
            print(f"[POOL] Drain timeout — {self._queue.qsize()} tasks abandoned")

    def status(self) -> dict:
        return {w.worker_id: w.state.value for w in self._workers.values()}

async def main():
    pool = GracefulWorkerPool(n_workers=2, drain_timeout=20.0)
    worker_tasks = await pool.start()

    requests = [(f"req-{i:03d}", f"What is {i}+{i}?") for i in range(8)]
    for rid, prompt in requests:
        accepted = await pool.submit(rid, prompt)
        print(f"[SUBMIT] {rid}: {'accepted' if accepted else 'rejected'}")

    await asyncio.sleep(1.0)
    await pool.shutdown()

    for t in worker_tasks:
        t.cancel()
    await asyncio.gather(*worker_tasks, return_exceptions=True)
    print(f"Final worker states: {pool.status()}")

if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: N/A — queue drain ensures no work is dropped on shutdown
# Environment: pip install anthropic
```

### Option 5: Streaming Request Draining with Partial Result Persistence

```python
import anthropic
import asyncio
import signal
import time
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

@dataclass
class PartialResult:
    request_id: str
    prompt: str
    partial_text: str
    is_complete: bool
    saved_at: float = field(default_factory=time.time)

class StreamingGracefulShutdown:
    def __init__(self, checkpoint_dir: str = "/tmp/stream_checkpoints"):
        self._active_streams: dict[str, dict] = {}
        self._checkpoint_dir = Path(checkpoint_dir)
        self._checkpoint_dir.mkdir(parents=True, exist_ok=True)
        self._shutting_down = asyncio.Event()
        self._lock = asyncio.Lock()

    def _save_partial(self, result: PartialResult) -> None:
        path = self._checkpoint_dir / f"{result.request_id}.json"
        path.write_text(json.dumps({
            "request_id": result.request_id,
            "prompt": result.prompt,
            "partial_text": result.partial_text,
            "is_complete": result.is_complete,
            "saved_at": result.saved_at,
        }))

    async def stream_request(self, client: anthropic.AsyncAnthropic,
                              request_id: str, prompt: str) -> str:
        async with self._lock:
            self._active_streams[request_id] = {
                "prompt": prompt,
                "accumulated": "",
                "started_at": time.monotonic(),
            }

        accumulated = ""
        complete = False
        try:
            async with client.messages.stream(
                model="claude-haiku-4-5-20251001",
                max_tokens=150,
                messages=[{"role": "user", "content": prompt}],
            ) as stream:
                async for chunk in stream.text_stream:
                    if self._shutting_down.is_set():
                        print(f"[DRAIN] {request_id}: shutdown signal received, "
                              f"saving {len(accumulated)} chars of partial output")
                        break
                    accumulated += chunk
                    async with self._lock:
                        self._active_streams[request_id]["accumulated"] = accumulated
                else:
                    complete = True
        finally:
            # Always checkpoint before exiting
            self._save_partial(PartialResult(
                request_id=request_id, prompt=prompt,
                partial_text=accumulated, is_complete=complete,
            ))
            async with self._lock:
                self._active_streams.pop(request_id, None)

            print(f"[{request_id}] {'Complete' if complete else 'Partial'}: "
                  f"{accumulated[:60]}{'...' if len(accumulated)>60 else ''}")
        return accumulated

    async def shutdown(self, timeout: float = 10.0) -> None:
        print(f"\n[SHUTDOWN] Signaling {len(self._active_streams)} active streams")
        self._shutting_down.set()

        deadline = time.monotonic() + timeout
        while self._active_streams and time.monotonic() < deadline:
            await asyncio.sleep(0.1)

        remaining = len(self._active_streams)
        if remaining:
            print(f"[SHUTDOWN] {remaining} streams still active at deadline")
        else:
            print("[SHUTDOWN] All streams drained cleanly")

async def main():
    client = anthropic.AsyncAnthropic()
    server = StreamingGracefulShutdown()

    requests = [
        ("stream-1", "Write a short story about a robot who learns to dream."),
        ("stream-2", "Explain quantum entanglement in simple terms."),
        ("stream-3", "Describe the water cycle step by step."),
    ]

    tasks = [asyncio.create_task(server.stream_request(client, rid, p))
             for rid, p in requests]

    # Trigger shutdown mid-stream
    await asyncio.sleep(0.8)
    await server.shutdown(timeout=5.0)

    await asyncio.gather(*tasks, return_exceptions=True)
    print(f"\nCheckpoints saved to {server._checkpoint_dir}")

if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: Partial results saved — no need to re-run entire request on restart
# Environment: pip install anthropic
```

### Option 6: Kubernetes-Ready Shutdown with Preemption Hook

```python
import anthropic
import asyncio
import os
import signal
import time
from dataclasses import dataclass, field
from typing import Optional

@dataclass
class K8sShutdownConfig:
    """Configuration matching typical Kubernetes pod termination behavior."""
    pre_stop_sleep_seconds: float = 5.0   # k8s preStop hook sleep
    termination_grace_period: float = 30.0  # terminationGracePeriodSeconds
    readiness_drain_seconds: float = 3.0   # Time to stop receiving new traffic

class K8sGracefulAgent:
    def __init__(self, config: K8sShutdownConfig = K8sShutdownConfig()):
        self._config = config
        self._ready = asyncio.Event()
        self._ready.set()
        self._shutting_down = False
        self._active: dict[str, float] = {}
        self._lock = asyncio.Lock()
        self._client = anthropic.AsyncAnthropic()
        self._pid = os.getpid()

    def is_ready(self) -> bool:
        """k8s readiness probe: /readyz"""
        return self._ready.is_set() and not self._shutting_down

    def is_alive(self) -> bool:
        """k8s liveness probe: /livez"""
        return True  # Always alive until SIGKILL

    async def _pre_stop_hook(self) -> None:
        """Mimics k8s preStop hook: sleep to allow load balancer drain."""
        print(f"[preStop] Sleeping {self._config.pre_stop_sleep_seconds}s "
              f"for load balancer drain")
        await asyncio.sleep(self._config.pre_stop_sleep_seconds)

    async def _drain_active(self) -> None:
        """Wait for active requests to complete."""
        timeout = (self._config.termination_grace_period -
                   self._config.pre_stop_sleep_seconds)
        deadline = time.monotonic() + timeout
        while self._active and time.monotonic() < deadline:
            active_ids = list(self._active.keys())
            print(f"[drain] Waiting for: {active_ids[:3]}{'...' if len(active_ids)>3 else ''}")
            await asyncio.sleep(0.5)
        if self._active:
            print(f"[drain] Abandoned {len(self._active)} requests at deadline")
        else:
            print(f"[drain] All {len(self._active)} requests completed")

    async def shutdown(self) -> None:
        print(f"[SIGTERM] PID {self._pid} beginning graceful shutdown")
        self._shutting_down = True
        self._ready.clear()  # Fail readiness probe immediately

        # Simulate k8s preStop + traffic drain
        await asyncio.sleep(self._config.readiness_drain_seconds)
        await self._pre_stop_hook()
        await self._drain_active()

        total_grace = self._config.termination_grace_period
        print(f"[SHUTDOWN] Complete within {total_grace}s grace period")

    async def handle(self, request_id: str, prompt: str) -> Optional[str]:
        if self._shutting_down:
            return None
        async with self._lock:
            self._active[request_id] = time.monotonic()

        try:
            response = await self._client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=60,
                messages=[{"role": "user", "content": prompt}],
            )
            return response.content[0].text
        finally:
            async with self._lock:
                self._active.pop(request_id, None)

async def main():
    config = K8sShutdownConfig(
        pre_stop_sleep_seconds=1.0,
        termination_grace_period=15.0,
        readiness_drain_seconds=0.5,
    )
    agent = K8sGracefulAgent(config)

    print(f"[READY] ready={agent.is_ready()} live={agent.is_alive()}")

    tasks = [
        asyncio.create_task(agent.handle(f"req-{i}", p))
        for i, p in enumerate(["What is AI?", "Name a planet.", "Define entropy."])
    ]

    await asyncio.sleep(0.3)
    print(f"[PROBE] ready={agent.is_ready()}, active={len(agent._active)}")
    await agent.shutdown()

    results = await asyncio.gather(*tasks, return_exceptions=True)
    for i, r in enumerate(results):
        print(f"req-{i}: {str(r)[:60] if r else 'None'}")

if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: PreStop hook prevents dropped requests during rolling deploys
# Environment: pip install anthropic
```

## Comparison

| Option | Signal Handling | Drain Mechanism | Partial Persistence | K8s Ready | Best For |
|--------|----------------|-----------------|---------------------|-----------|----------|
| 1. Request counter | SIGTERM/SIGINT | Wait + timeout | No | No | Simple services |
| 2. Context manager | Manual trigger | Event wait | No | No | Clean lifecycle |
| 3. HTTP drain endpoint | Manual trigger | Queue join | No | Readiness probe | REST APIs |
| 4. Worker pool drain | Shutdown event | Queue join | No | No | Worker pools |
| 5. Stream checkpointing | Shutdown event | Stream interrupt | Yes | No | Streaming agents |
| 6. k8s preStop pattern | SIGTERM | preStop + drain | No | Full (ready+live) | Kubernetes |
