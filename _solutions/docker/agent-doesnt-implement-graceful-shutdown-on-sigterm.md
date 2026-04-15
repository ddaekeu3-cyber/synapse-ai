---
layout: solution
title: "Agent Doesn't Implement Graceful Shutdown on SIGTERM"
category: docker
description: "When Kubernetes or Docker stops a container, the agent process receives SIGTERM but ignores it. In-flight requests are cut off mid-response, active tool calls are abandoned, and queued work is silently lost."
tags: [docker, graceful-shutdown, sigterm, kubernetes, asyncio, fastapi, signal-handling]
---

# Agent Doesn't Implement Graceful Shutdown on SIGTERM

## Problem

`kubectl rollout restart` sends SIGTERM to the agent container. The process has no signal handler, so the OS terminates it immediately. Streaming responses are cut off mid-token, Anthropic API calls are cancelled without cleanup, and any work in the asyncio event loop is discarded. Users see broken responses; retries cause duplicate side effects; billing is charged for truncated calls.

## Solutions

### Option 1: asyncio Signal Handler with In-Flight Request Drain

```python
# main.py
"""
Register SIGTERM/SIGINT handlers that stop accepting new requests,
wait for in-flight requests to complete, then shut down cleanly.
"""
import asyncio
import signal
import logging
import os
import anthropic
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# Track active requests
_active_requests: set[asyncio.Task] = set()
_shutting_down = False
_shutdown_event = asyncio.Event()
DRAIN_TIMEOUT = int(os.environ.get("SHUTDOWN_DRAIN_TIMEOUT", "30"))  # seconds


def _handle_sigterm():
    global _shutting_down
    if _shutting_down:
        return
    _shutting_down = True
    logger.info("SIGTERM received — draining %d in-flight requests (timeout: %ds)",
                len(_active_requests), DRAIN_TIMEOUT)
    _shutdown_event.set()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Register signal handlers in the event loop
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, _handle_sigterm)
    loop.add_signal_handler(signal.SIGINT, _handle_sigterm)
    logger.info("Agent started — signal handlers registered")
    yield
    # Shutdown phase: wait for active requests to finish
    if _active_requests:
        logger.info("Waiting for %d active requests to complete...", len(_active_requests))
        try:
            await asyncio.wait_for(
                asyncio.gather(*_active_requests, return_exceptions=True),
                timeout=DRAIN_TIMEOUT,
            )
            logger.info("All requests drained cleanly.")
        except asyncio.TimeoutError:
            logger.warning("Drain timeout — %d requests still active, forcing shutdown",
                           len(_active_requests))


app = FastAPI(lifespan=lifespan)
client = anthropic.AsyncAnthropic()


@app.middleware("http")
async def track_request(request, call_next):
    if _shutting_down:
        raise HTTPException(status_code=503, detail="Server is shutting down")
    task = asyncio.current_task()
    _active_requests.add(task)
    try:
        return await call_next(request)
    finally:
        _active_requests.discard(task)


@app.post("/api/agent/chat")
async def chat(body: dict):
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{"role": "user", "content": body.get("message", "")}],
    )
    return {"response": response.content[0].text}
```

```dockerfile
# Dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# uvicorn handles SIGTERM → lifespan shutdown
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
```

**Expected Token Savings:** Not applicable — reliability infrastructure
**Environment:** `pip install fastapi anthropic uvicorn`

---

### Option 2: Graceful Shutdown for Background Worker Process

```python
# worker.py
"""
Background task worker (not HTTP): handles SIGTERM by finishing the
current task, checkpointing progress, then exiting cleanly.
"""
import asyncio
import signal
import logging
import os
import anthropic

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


class GracefulWorker:
    def __init__(self):
        self.client = anthropic.AsyncAnthropic()
        self._should_stop = False
        self._current_task_id: str | None = None

    def setup_signals(self):
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGTERM, self._on_sigterm)
        loop.add_signal_handler(signal.SIGINT, self._on_sigterm)

    def _on_sigterm(self):
        logger.info("SIGTERM received — will stop after current task '%s' completes",
                    self._current_task_id)
        self._should_stop = True

    async def _checkpoint(self, task_id: str, progress: dict):
        """Persist progress so the task can resume after restart."""
        import json
        checkpoint_file = f"/tmp/checkpoint_{task_id}.json"
        with open(checkpoint_file, "w") as f:
            json.dump({"task_id": task_id, "progress": progress}, f)
        logger.info("Checkpointed task %s", task_id)

    async def process_task(self, task: dict) -> dict:
        task_id = task["id"]
        self._current_task_id = task_id
        logger.info("Processing task %s", task_id)

        response = await self.client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            messages=[{"role": "user", "content": task["prompt"]}],
        )
        result = {"task_id": task_id, "output": response.content[0].text}
        self._current_task_id = None
        return result

    async def run(self, task_queue):
        """Main work loop — stops cleanly at task boundaries."""
        self.setup_signals()
        logger.info("Worker started")

        while not self._should_stop:
            try:
                task = await asyncio.wait_for(task_queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue  # Re-check _should_stop

            try:
                result = await self.process_task(task)
                await task_queue.task_done()
                logger.info("Task %s completed", result["task_id"])
            except Exception as e:
                logger.error("Task %s failed: %s", task.get("id"), e)
                await self._checkpoint(task.get("id", "unknown"), {"error": str(e)})

        logger.info("Worker exiting cleanly after SIGTERM")


async def main():
    queue: asyncio.Queue = asyncio.Queue()
    # Seed some test tasks
    for i in range(5):
        await queue.put({"id": f"task-{i}", "prompt": f"What is {i}+{i}?"})

    worker = GracefulWorker()
    await worker.run(queue)


if __name__ == "__main__":
    asyncio.run(main())
```

**Expected Token Savings:** Not applicable — work preservation on shutdown
**Environment:** `pip install anthropic`

---

### Option 3: Docker STOPSIGNAL + Pre-Stop Hook

```dockerfile
# Dockerfile — explicit STOPSIGNAL and longer stop timeout
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# Tell Docker to send SIGTERM (not SIGKILL) — this is the default but explicit is clearer
STOPSIGNAL SIGTERM

# Give the container 60s to drain before Docker sends SIGKILL
# Override per-container with: docker stop --time=60 <container>
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--timeout-graceful-shutdown", "45"]
```

```yaml
# k8s/deployment.yaml — Kubernetes graceful termination configuration
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ai-agent
spec:
  template:
    spec:
      # Give pods 60s to drain before forced kill
      terminationGracePeriodSeconds: 60
      containers:
        - name: agent
          image: your-registry/ai-agent:latest
          lifecycle:
            preStop:
              exec:
                # Wait 5s for load balancer to stop routing traffic
                # before the process starts shutting down
                command: ["/bin/sh", "-c", "sleep 5"]
          env:
            - name: SHUTDOWN_DRAIN_TIMEOUT
              value: "45"
```

```python
# scripts/preStop.py
"""
Pre-stop hook: signal the app to stop accepting new requests,
then wait until all active requests finish or timeout expires.
Called by Kubernetes lifecycle.preStop.exec before SIGTERM.
"""
import sys
import time
import urllib.request
import urllib.error

# Tell the app to enter drain mode via an internal endpoint
try:
    urllib.request.urlopen("http://localhost:8000/internal/drain", timeout=2)
    print("Drain mode activated")
except Exception as e:
    print(f"Could not activate drain mode: {e}", file=sys.stderr)

# Wait for active requests to drop to zero (max 50s)
deadline = time.time() + 50
while time.time() < deadline:
    try:
        with urllib.request.urlopen("http://localhost:8000/internal/active-count", timeout=2) as resp:
            import json
            data = json.loads(resp.read())
            count = data.get("active", 0)
            if count == 0:
                print("All requests drained")
                sys.exit(0)
            print(f"Waiting for {count} active requests...")
    except Exception:
        pass
    time.sleep(1)

print("Drain timeout — proceeding with shutdown", file=sys.stderr)
```

**Expected Token Savings:** Not applicable — Kubernetes deployment reliability
**Environment:** stdlib + Docker + Kubernetes

---

### Option 4: Streaming Response Graceful Cancellation

```python
# api/streaming.py
"""
For streaming agent responses: handle SIGTERM by completing the current
chunk, sending a graceful end-of-stream marker, then closing the connection.
Prevents clients from receiving half a sentence with no terminator.
"""
import asyncio
import signal
import json
import anthropic
from fastapi import FastAPI
from fastapi.responses import StreamingResponse

app = FastAPI()
_draining = False
_active_streams: set[asyncio.Task] = set()


def _on_sigterm():
    global _draining
    _draining = True
    print(f"SIGTERM: draining {len(_active_streams)} active streams")


@app.on_event("startup")
async def startup():
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, _on_sigterm)


async def _stream_with_graceful_shutdown(user_message: str):
    """SSE generator that sends a graceful termination event on shutdown."""
    client = anthropic.AsyncAnthropic()
    task = asyncio.current_task()
    _active_streams.add(task)

    try:
        async with client.messages.stream(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            messages=[{"role": "user", "content": user_message}],
        ) as stream:
            async for text in stream.text_stream:
                if _draining:
                    # Send graceful shutdown event and stop
                    yield f"event: shutdown\ndata: {json.dumps({'reason': 'server_restart'})}\n\n"
                    return
                yield f"data: {json.dumps({'text': text})}\n\n"

        final = await stream.get_final_message()
        yield f"event: done\ndata: {json.dumps({'stop_reason': final.stop_reason})}\n\n"

    except asyncio.CancelledError:
        yield f"event: error\ndata: {json.dumps({'error': 'connection_cancelled'})}\n\n"
    finally:
        _active_streams.discard(task)


@app.post("/api/agent/stream")
async def stream_chat(body: dict):
    if _draining:
        from fastapi import HTTPException
        raise HTTPException(503, "Server is shutting down")
    return StreamingResponse(
        _stream_with_graceful_shutdown(body.get("message", "")),
        media_type="text/event-stream",
    )


@app.get("/internal/active-count")
async def active_count():
    return {"active": len(_active_streams)}


@app.post("/internal/drain")
async def start_drain():
    global _draining
    _draining = True
    return {"draining": True, "active_streams": len(_active_streams)}
```

**Expected Token Savings:** Not applicable — user experience reliability
**Environment:** `pip install fastapi anthropic uvicorn`

---

### Option 5: Celery Worker Graceful Shutdown

```python
# workers/celery_agent.py
"""
Celery-based agent task worker with graceful shutdown.
Celery supports warm shutdown (finish current task then stop) natively
via --max-tasks-per-child or the revoke() API.
"""
import os
import signal
import logging
import anthropic
from celery import Celery
from celery.signals import worker_shutdown, worker_ready

logger = logging.getLogger(__name__)
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

celery_app = Celery("agent_worker", broker=REDIS_URL, backend=REDIS_URL)
celery_app.conf.update(
    # Acknowledge task only after it completes (not when picked up)
    # This means if the worker dies mid-task, the task is re-queued
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    # Graceful shutdown: wait up to 60s for current tasks to finish
    worker_cancel_long_running_tasks_on_connection_loss=True,
)


@worker_ready.connect
def on_worker_ready(sender, **kwargs):
    logger.info("Agent worker ready — task_acks_late=True, graceful shutdown enabled")


@worker_shutdown.connect
def on_worker_shutdown(sender, **kwargs):
    logger.info("Agent worker shutting down — current tasks will complete before exit")


@celery_app.task(
    bind=True,
    max_retries=3,
    soft_time_limit=55,   # Soft limit: raises SoftTimeLimitExceeded (catchable)
    time_limit=60,        # Hard limit: SIGKILL after 60s
    name="agent.process_message",
)
def process_message(self, task_id: str, user_message: str, webhook_url: str = ""):
    """
    Process an agent task. On SoftTimeLimitExceeded, checkpoint and reschedule.
    """
    from celery.exceptions import SoftTimeLimitExceeded

    try:
        client = anthropic.Anthropic()
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            messages=[{"role": "user", "content": user_message}],
        )
        result = response.content[0].text

        if webhook_url:
            import httpx
            httpx.post(webhook_url, json={"task_id": task_id, "result": result}, timeout=10)

        return {"task_id": task_id, "result": result, "status": "completed"}

    except SoftTimeLimitExceeded:
        logger.warning("Task %s hit soft time limit — retrying", task_id)
        raise self.retry(countdown=5, exc=SoftTimeLimitExceeded("soft limit"))

    except Exception as exc:
        logger.error("Task %s failed: %s", task_id, exc)
        raise self.retry(exc=exc, countdown=10)
```

```bash
# Start worker with warm shutdown support
celery -A workers.celery_agent worker \
  --loglevel=info \
  --concurrency=4 \
  --max-tasks-per-child=100 \
  --shutdown-timeout=60
```

**Expected Token Savings:** Not applicable — task queue reliability
**Environment:** `pip install celery anthropic redis httpx`

---

### Option 6: Shutdown Test — Verify Graceful Behavior

```python
# tests/test_graceful_shutdown.py
"""
Integration tests that verify graceful shutdown behavior:
- In-flight requests complete before shutdown.
- New requests are rejected with 503 after SIGTERM.
- Active count drops to zero before process exits.
"""
import asyncio
import os
import signal
import subprocess
import time
import pytest
import httpx


@pytest.fixture(scope="module")
def server_process():
    """Start the agent server as a subprocess."""
    proc = subprocess.Popen(
        ["uvicorn", "main:app", "--port", "8001"],
        env={**os.environ, "SHUTDOWN_DRAIN_TIMEOUT": "10"},
    )
    time.sleep(2)  # Wait for startup
    yield proc
    if proc.poll() is None:
        proc.terminate()
        proc.wait(timeout=15)


def test_server_starts(server_process):
    resp = httpx.get("http://localhost:8001/health", timeout=5)
    assert resp.status_code == 200


def test_in_flight_request_completes_after_sigterm(server_process):
    """Start a long request, then send SIGTERM — request should complete."""
    import threading

    results = []

    def make_request():
        try:
            resp = httpx.post(
                "http://localhost:8001/api/agent/chat",
                json={"message": "Count to 10 slowly."},
                timeout=30,
            )
            results.append(resp.status_code)
        except Exception as e:
            results.append(f"error: {e}")

    # Start request in background
    t = threading.Thread(target=make_request)
    t.start()
    time.sleep(0.2)  # Let request start

    # Send SIGTERM
    server_process.send_signal(signal.SIGTERM)

    # Request should still complete
    t.join(timeout=20)
    assert results, "Request never completed"
    assert results[0] == 200, f"Request failed after SIGTERM: {results[0]}"


def test_new_requests_rejected_after_sigterm(server_process):
    """After SIGTERM is sent, new requests should receive 503."""
    server_process.send_signal(signal.SIGTERM)
    time.sleep(0.5)  # Give the handler time to set _shutting_down=True

    try:
        resp = httpx.post(
            "http://localhost:8001/api/agent/chat",
            json={"message": "hello"},
            timeout=5,
        )
        # Should be 503 or connection refused
        assert resp.status_code == 503
    except httpx.ConnectError:
        pass  # Process already exited — also acceptable
```

**Expected Token Savings:** Not applicable — reliability verification
**Environment:** `pip install pytest httpx`

---

## Comparison Table

| Option | Handles SIGTERM | Drains In-Flight | Rejects New Requests | Checkpoints Work | Kubernetes-Ready |
|--------|----------------|------------------|---------------------|------------------|------------------|
| 1: asyncio handler | Yes | Yes (asyncio.wait) | Yes (503) | No | Yes |
| 2: Worker loop | Yes | Yes (task boundary) | Yes (loop exit) | Yes | Yes |
| 3: Docker/K8s config | Via runtime | Via terminationGrace | Via preStop | No | Yes |
| 4: Streaming cancel | Yes | Yes (SSE close) | Yes (503) | No | Yes |
| 5: Celery warm shutdown | Yes (built-in) | Yes (acks_late) | Yes (revoke) | Via retry | Yes |
| 6: Shutdown tests | N/A (test) | Verified | Verified | N/A | Via CI |
