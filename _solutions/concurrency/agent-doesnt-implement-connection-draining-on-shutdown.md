---
layout: solution
title: "Agent Doesn't Implement Connection Draining on Shutdown"
category: concurrency
description: "Agent terminates immediately on SIGTERM, aborting in-flight API calls mid-stream, corrupting partial writes, and leaving downstream services with dangling connections."
tags: [shutdown, sigterm, graceful, connection-draining, reliability]
---

## Symptom

During deployment rollouts or container restarts, active requests return `Connection reset by peer`, partial tool results are stored, streaming responses cut off mid-sentence, and users see errors that disappear on retry. Log entries show requests that started but never completed. Database records are left in intermediate states.

## Root Cause

The agent process registers no SIGTERM handler (or uses the default handler which immediately exits). When Kubernetes or Docker sends SIGTERM before SIGKILL, the process dies while requests are still in-flight. Anthropic API calls in `client.messages.create()` or active stream iterators are abandoned, raising `ConnectionError` on the caller side. Without a drain window, there is no opportunity to finish current work, flush buffers, or signal downstream systems.

## Fix

### Option 1: Signal handler with drain flag and asyncio event

```python
import asyncio
import signal
import anthropic

client = anthropic.AsyncAnthropic()
_shutdown_event = asyncio.Event()
_active_requests: set[asyncio.Task] = set()


def _handle_sigterm(sig, frame):
    print(f"SIGTERM received — beginning graceful drain")
    # Set event from signal handler (thread-safe in asyncio)
    asyncio.get_event_loop().call_soon_threadsafe(_shutdown_event.set)


signal.signal(signal.SIGTERM, _handle_sigterm)
signal.signal(signal.SIGINT, _handle_sigterm)


async def tracked_request(user_id: str, message: str) -> str:
    """Run an API call and track it so shutdown can wait for it."""
    task = asyncio.current_task()
    _active_requests.add(task)
    try:
        response = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            messages=[{"role": "user", "content": message}],
        )
        return response.content[0].text
    finally:
        _active_requests.discard(task)


async def drain_and_exit(timeout: float = 30.0) -> None:
    """Wait for all in-flight requests to complete, then exit."""
    print(f"Draining {len(_active_requests)} active requests (timeout: {timeout}s)...")

    if _active_requests:
        try:
            await asyncio.wait_for(
                asyncio.gather(*_active_requests, return_exceptions=True),
                timeout=timeout,
            )
            print("All requests completed cleanly.")
        except asyncio.TimeoutError:
            print(f"Drain timeout reached — {len(_active_requests)} requests still running, force-exiting.")


async def main():
    # Simulate concurrent requests
    tasks = [
        asyncio.create_task(tracked_request(f"user-{i}", f"Tell me fact #{i} about Python async"))
        for i in range(5)
    ]

    # Wait for shutdown signal
    await _shutdown_event.wait()
    await drain_and_exit(timeout=30.0)

    # Cancel any tasks we launched that haven't finished
    for t in tasks:
        if not t.done():
            t.cancel()


asyncio.run(main())
```

**Expected Token Savings:** Indirect — prevents duplicate billing from retried requests that failed due to abrupt shutdown.
**Environment:** Python 3.11+; asyncio; suitable for any long-running async agent process.

---

### Option 2: FastAPI lifespan with drain window

```python
import asyncio
import contextlib
from contextlib import asynccontextmanager

import anthropic
from fastapi import FastAPI, Request

client = anthropic.AsyncAnthropic()
_active: set[asyncio.Task] = set()
DRAIN_TIMEOUT = 25.0  # seconds — must be less than Kubernetes terminationGracePeriodSeconds


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    FastAPI lifespan context manager.
    Startup: initialize resources.
    Shutdown: wait for in-flight requests to complete.
    """
    # Startup
    print("Agent service starting up")
    yield
    # Shutdown (triggered by SIGTERM from container orchestrator)
    print(f"Shutdown signal received — draining {len(_active)} active requests")
    if _active:
        done, pending = await asyncio.wait(list(_active), timeout=DRAIN_TIMEOUT)
        if pending:
            print(f"Drain timeout: cancelling {len(pending)} requests")
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
    print("Drain complete — process exiting")


app = FastAPI(lifespan=lifespan)


async def call_claude(prompt: str) -> str:
    response = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


@app.post("/chat")
async def chat_endpoint(request: Request, body: dict):
    task = asyncio.create_task(call_claude(body.get("message", "")))
    _active.add(task)
    try:
        result = await task
        return {"response": result}
    finally:
        _active.discard(task)


@app.get("/health")
async def health():
    return {"status": "ok", "active_requests": len(_active)}
```

**Expected Token Savings:** Zero wasted API calls — all in-flight requests complete before process exits.
**Environment:** Python 3.10+; FastAPI with uvicorn; set `terminationGracePeriodSeconds: 60` in Kubernetes pod spec.

---

### Option 3: Streaming response with drain-safe iteration

```python
import asyncio
import signal
import sys
import anthropic

client = anthropic.AsyncAnthropic()
_draining = False
_stream_count = 0


def request_drain():
    global _draining
    _draining = True
    print("Drain requested — finishing active streams before exit", file=sys.stderr)


signal.signal(signal.SIGTERM, lambda s, f: request_drain())


async def stream_with_drain_awareness(prompt: str) -> str:
    """
    Stream a response while respecting drain state.
    On drain: finish the current stream before yielding control.
    """
    global _stream_count
    _stream_count += 1
    collected = []

    try:
        async with client.messages.stream(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            async for text in stream.text_stream:
                collected.append(text)
                # Do NOT abort mid-stream even if draining
                # The stream will complete naturally
                print(text, end="", flush=True)

        print()  # newline after stream
        return "".join(collected)
    finally:
        _stream_count -= 1
        if _draining and _stream_count == 0:
            print("All streams complete — safe to exit", file=sys.stderr)
            sys.exit(0)


async def multi_stream_agent():
    prompts = [
        "Explain connection draining in one paragraph.",
        "What is a graceful shutdown in distributed systems?",
        "How does Kubernetes handle pod termination?",
    ]

    # Run streams concurrently
    results = await asyncio.gather(*[stream_with_drain_awareness(p) for p in prompts])
    return results


asyncio.run(multi_stream_agent())
```

**Expected Token Savings:** Streaming responses that complete naturally avoid the cost of re-requesting truncated outputs.
**Environment:** Python 3.11+; streaming API; handles concurrent streams.

---

### Option 4: Worker pool with drainable queue

```python
import asyncio
import signal
import anthropic
from dataclasses import dataclass
from typing import Any

client = anthropic.AsyncAnthropic()


@dataclass
class WorkItem:
    user_id: str
    message: str
    future: asyncio.Future


class DrainableWorkerPool:
    """
    Fixed-size worker pool that processes a queue of API calls.
    On SIGTERM: stops accepting new work, finishes existing queue, then exits.
    """

    def __init__(self, num_workers: int = 5, drain_timeout: float = 30.0):
        self.queue: asyncio.Queue[WorkItem | None] = asyncio.Queue()
        self.num_workers = num_workers
        self.drain_timeout = drain_timeout
        self._shutdown = asyncio.Event()
        self._workers: list[asyncio.Task] = []

    async def start(self):
        self._workers = [
            asyncio.create_task(self._worker(i))
            for i in range(self.num_workers)
        ]

    async def submit(self, user_id: str, message: str) -> asyncio.Future:
        if self._shutdown.is_set():
            raise RuntimeError("Worker pool is shutting down — not accepting new work")
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        await self.queue.put(WorkItem(user_id, message, future))
        return future

    async def _worker(self, worker_id: int):
        while True:
            item = await self.queue.get()
            if item is None:  # Poison pill
                self.queue.task_done()
                break
            try:
                response = await client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=256,
                    messages=[{"role": "user", "content": item.message}],
                )
                item.future.set_result(response.content[0].text)
            except Exception as e:
                item.future.set_exception(e)
            finally:
                self.queue.task_done()

    async def drain_and_stop(self):
        """Stop accepting new work, finish queued items, then stop workers."""
        self._shutdown.set()
        print(f"Draining pool: {self.queue.qsize()} items queued, {self.num_workers} workers active")

        # Wait for queue to drain
        try:
            await asyncio.wait_for(self.queue.join(), timeout=self.drain_timeout)
            print("Queue drained successfully")
        except asyncio.TimeoutError:
            print(f"Drain timeout after {self.drain_timeout}s — forcing stop")

        # Send poison pills to stop workers
        for _ in self._workers:
            await self.queue.put(None)
        await asyncio.gather(*self._workers, return_exceptions=True)


pool = DrainableWorkerPool(num_workers=3)


async def main():
    await pool.start()

    # Register shutdown
    loop = asyncio.get_event_loop()
    loop.add_signal_handler(signal.SIGTERM, lambda: asyncio.create_task(pool.drain_and_stop()))

    # Submit work
    futures = [
        await pool.submit(f"user-{i}", f"Give me a one-line tip about Python async #{i}")
        for i in range(10)
    ]

    results = await asyncio.gather(*futures)
    for r in results:
        print(r[:80])

    await pool.drain_and_stop()


asyncio.run(main())
```

**Expected Token Savings:** Queued work is never dropped — all submitted requests complete, preventing retry cost.
**Environment:** Python 3.11+; queue-based worker pool; suitable for batch processing agents.

---

### Option 5: HTTP server with readiness probe and drain gate

```python
import asyncio
import signal
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread, Event

import anthropic

client = anthropic.Anthropic()

_ready = True          # Readiness probe state
_draining = Event()    # Set when shutdown begins
_request_count = 0     # Active request counter
_lock = asyncio.Lock() if hasattr(asyncio, 'Lock') else None


class ProbeHandler(BaseHTTPRequestHandler):
    """Minimal HTTP server for Kubernetes probes on a separate port."""

    def do_GET(self):
        if self.path == "/ready":
            # Return 503 during drain so load balancer stops sending traffic
            if _draining.is_set():
                self.send_response(503)
                self.end_headers()
                self.wfile.write(b'{"status":"draining"}')
            else:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"status":"ready"}')
        elif self.path == "/health":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"status":"alive"}')
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # Suppress probe logs


def start_probe_server(port: int = 8081):
    server = HTTPServer(("", port), ProbeHandler)
    t = Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server


def handle_sigterm(sig, frame):
    global _ready
    _ready = False
    _draining.set()
    print("SIGTERM: readiness probe → 503, waiting for in-flight requests to finish")


signal.signal(signal.SIGTERM, handle_sigterm)


def run_agent_request(message: str) -> str:
    """Synchronous API call — tracked for drain awareness."""
    global _request_count

    if _draining.is_set():
        raise RuntimeError("Agent is draining — rejecting new requests")

    _request_count += 1
    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            messages=[{"role": "user", "content": message}],
        )
        return response.content[0].text
    finally:
        _request_count -= 1


def drain_loop(timeout: float = 30.0):
    """Block until all active requests complete or timeout."""
    deadline = time.time() + timeout
    while _request_count > 0 and time.time() < deadline:
        print(f"Waiting for {_request_count} requests...")
        time.sleep(0.5)
    if _request_count > 0:
        print(f"Force exit — {_request_count} requests still active")
    else:
        print("Clean drain complete")


probe_server = start_probe_server()
print("Agent running — probe server on :8081")

# Normal operation
try:
    result = run_agent_request("What is connection draining?")
    print(result[:200])
except RuntimeError as e:
    print(f"Request rejected: {e}")

# Simulate graceful shutdown
handle_sigterm(None, None)
drain_loop(timeout=30.0)
probe_server.shutdown()
```

**Expected Token Savings:** Readiness probe ensures load balancer drains traffic before SIGKILL, so all requests complete without retry.
**Environment:** Python 3.9+; synchronous; pairs with Kubernetes readiness probes on port 8081.

---

### Option 6: Checkpoint-based drain for long-running tasks

```python
import asyncio
import json
import os
import signal
import anthropic

client = anthropic.AsyncAnthropic()
CHECKPOINT_FILE = "/tmp/agent_checkpoint.json"
_shutdown_requested = False


def request_shutdown(sig, frame):
    global _shutdown_requested
    _shutdown_requested = True
    print("Shutdown requested — will stop at next checkpoint")


signal.signal(signal.SIGTERM, request_shutdown)


class CheckpointedAgent:
    """
    Long-running agent that saves progress at each step.
    On SIGTERM: finishes current step, saves checkpoint, then exits cleanly.
    Restarts pick up from checkpoint, never losing completed work.
    """

    def __init__(self, task_id: str):
        self.task_id = task_id
        self.completed_steps: list[int] = []
        self.results: dict[int, str] = {}
        self._load_checkpoint()

    def _load_checkpoint(self):
        if os.path.exists(CHECKPOINT_FILE):
            with open(CHECKPOINT_FILE) as f:
                data = json.load(f)
            if data.get("task_id") == self.task_id:
                self.completed_steps = data.get("completed_steps", [])
                self.results = {int(k): v for k, v in data.get("results", {}).items()}
                print(f"Resumed from checkpoint: {len(self.completed_steps)} steps done")

    def _save_checkpoint(self):
        with open(CHECKPOINT_FILE, "w") as f:
            json.dump({
                "task_id": self.task_id,
                "completed_steps": self.completed_steps,
                "results": self.results,
            }, f, indent=2)

    def _clear_checkpoint(self):
        if os.path.exists(CHECKPOINT_FILE):
            os.remove(CHECKPOINT_FILE)

    async def run_steps(self, steps: list[str]) -> dict[int, str]:
        for i, step_prompt in enumerate(steps):
            if i in self.completed_steps:
                print(f"Step {i}: skipped (already done)")
                continue

            # Run this step — even if shutdown was requested, finish it
            print(f"Step {i}: running...")
            response = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=256,
                messages=[{"role": "user", "content": step_prompt}],
            )
            self.results[i] = response.content[0].text
            self.completed_steps.append(i)

            # Save after every step
            self._save_checkpoint()
            print(f"Step {i}: done, checkpoint saved")

            # Check shutdown AFTER completing and checkpointing the step
            if _shutdown_requested:
                remaining = [j for j in range(len(steps)) if j not in self.completed_steps]
                print(f"Shutdown: stopping after step {i}. Remaining: {remaining}")
                return self.results

        self._clear_checkpoint()
        print("All steps complete — checkpoint cleared")
        return self.results


async def main():
    agent = CheckpointedAgent(task_id="research-task-001")
    steps = [
        "List 3 benefits of graceful shutdown in microservices",
        "Describe the Kubernetes pod termination lifecycle in 2 sentences",
        "What is a drain window and why does it matter?",
        "How does a readiness probe help with zero-downtime deploys?",
        "Summarize best practices for connection draining in one paragraph",
    ]
    results = await agent.run_steps(steps)
    print(f"\nCompleted {len(results)}/{len(steps)} steps")
    for i, result in sorted(results.items()):
        print(f"\n--- Step {i} ---\n{result[:150]}")


asyncio.run(main())
```

**Expected Token Savings:** Checkpoint-based drain means completed steps are never re-run after restart — critical for multi-step tasks with expensive intermediate calls.
**Environment:** Python 3.11+; file-based checkpoint; replace with Redis/DB for distributed deployments.

---

| Option | Approach | Drain Mechanism | Best For |
|--------|----------|----------------|----------|
| 1 | asyncio.Event + task set | Wait for gathered tasks | Async microservices |
| 2 | FastAPI lifespan | asyncio.wait + timeout | FastAPI/uvicorn apps |
| 3 | Stream-aware drain flag | Natural stream completion | Streaming agents |
| 4 | Queue poison pill | queue.join() | Worker pool agents |
| 5 | Readiness probe 503 | Counter + load balancer | Kubernetes services |
| 6 | Step checkpoint | Save-then-check-shutdown | Long multi-step tasks |
