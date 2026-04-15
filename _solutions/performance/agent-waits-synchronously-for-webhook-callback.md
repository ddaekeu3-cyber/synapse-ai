---
layout: solution
title: "Agent Waits Synchronously for Webhook Callback — Hangs Until Timeout"
category: performance
description: "Agent triggers an async operation (payment, build, email send) that returns a result via webhook. Agent then busy-polls or blocks the thread waiting for the callback, wasting tokens and time."
tags: [webhook, async, polling, callback, performance, waiting, latency]
---

# Agent Waits Synchronously for Webhook Callback — Hangs Until Timeout

## Symptom
- Agent triggers an action that should return via webhook in 30 seconds
- Agent then loops: "checking status... checking status... not yet..."
- Agent times out after 5 minutes of polling
- Each poll turn consumes tokens and API calls
- System hangs waiting for external event that may never come

## Root Cause
Webhook-based APIs are inherently async — the caller doesn't wait for the result. When an agent tries to synchronously wait for a webhook response using polling in the conversation loop, it creates an expensive busy-wait pattern that burns tokens and blocks the agent from doing other work.

## Fix

### Option 1: Suspend agent and resume on webhook receipt

```python
import asyncio, json
from fastapi import FastAPI, Request

app = FastAPI()

# Store pending agent sessions waiting for webhooks
pending_sessions = {}  # session_id -> asyncio.Event

async def trigger_async_operation(session_id: str, operation_data: dict) -> str:
    """Trigger operation and return immediately — don't wait"""
    # Create event for this session
    event = asyncio.Event()
    pending_sessions[session_id] = {"event": event, "result": None}

    # Trigger the operation
    await send_to_external_service(operation_data, webhook_url=f"/webhook/{session_id}")

    return f"Operation triggered. Waiting for completion (session: {session_id})"

@app.post("/webhook/{session_id}")
async def receive_webhook(session_id: str, request: Request):
    """Called by external service when operation completes"""
    result = await request.json()

    if session_id in pending_sessions:
        pending_sessions[session_id]["result"] = result
        pending_sessions[session_id]["event"].set()  # Wake up waiting agent

    return {"received": True}

async def wait_for_webhook(session_id: str, timeout: float = 300.0) -> dict:
    """Wait for webhook with timeout — no polling"""
    session = pending_sessions.get(session_id)
    if not session:
        raise ValueError(f"No pending session: {session_id}")

    try:
        await asyncio.wait_for(session["event"].wait(), timeout=timeout)
        result = session["result"]
        del pending_sessions[session_id]
        return result
    except asyncio.TimeoutError:
        del pending_sessions[session_id]
        raise TimeoutError(f"Webhook not received within {timeout}s for session {session_id}")
```

### Option 2: Store pending state, check on next agent turn

```python
import sqlite3
from datetime import datetime, timedelta

# Instead of waiting in a loop, save state and let user come back
def save_pending_webhook(task_id: str, context: dict, expires_in_minutes: int = 30):
    """Save agent state while waiting for webhook"""
    db = sqlite3.connect("agent_state.db")
    db.execute("""
        INSERT INTO pending_webhooks (task_id, context, created_at, expires_at)
        VALUES (?, ?, ?, ?)
    """, (
        task_id,
        json.dumps(context),
        datetime.utcnow().isoformat(),
        (datetime.utcnow() + timedelta(minutes=expires_in_minutes)).isoformat()
    ))
    db.commit()

def check_and_resume_pending(task_id: str) -> dict | None:
    """On next agent turn, check if webhook arrived"""
    db = sqlite3.connect("agent_state.db")
    result = db.execute(
        "SELECT result FROM webhook_results WHERE task_id = ?", (task_id,)
    ).fetchone()
    return json.loads(result[0]) if result else None

# Agent sends response:
# "I've triggered the payment process. The result will be delivered via webhook.
# Come back or wait — I'll automatically continue when it arrives."
```

### Option 3: Polling with exponential backoff and token budget

```python
import asyncio, time

async def poll_with_budget(
    check_fn,
    max_polls: int = 10,
    initial_delay: float = 5.0,
    max_delay: float = 60.0,
    timeout: float = 300.0
) -> dict:
    """Poll for result with exponential backoff — not busy-wait"""
    start = time.time()
    delay = initial_delay

    for poll_num in range(max_polls):
        elapsed = time.time() - start
        if elapsed > timeout:
            raise TimeoutError(f"Polling timed out after {elapsed:.0f}s")

        result = await check_fn()
        if result and result.get("status") in ("completed", "failed", "done"):
            print(f"Result received on poll {poll_num + 1} after {elapsed:.0f}s")
            return result

        print(f"Poll {poll_num + 1}/{max_polls}: not ready. Waiting {delay:.0f}s...")
        await asyncio.sleep(delay)
        delay = min(delay * 2, max_delay)  # Exponential backoff, cap at max_delay

    raise TimeoutError(f"No result after {max_polls} polls")

# Usage — agent checks once with backoff instead of tight loop
async def check_payment_status(payment_id: str) -> dict:
    async with httpx.AsyncClient() as client:
        response = await client.get(f"https://api.stripe.com/v1/payment_intents/{payment_id}")
        return response.json()

result = await poll_with_budget(
    lambda: check_payment_status(payment_id),
    max_polls=8,        # Max 8 checks
    initial_delay=5.0,  # Start with 5s, then 10s, 20s, 40s, 60s, 60s, 60s, 60s
    timeout=300.0       # Give up after 5 minutes
)
```

### Option 4: Agent response that sets expectation correctly

```
System prompt:
"Async operation protocol:
1. When triggering an async operation (payment, build, email, etc.):
   a. Trigger it and get a task/operation ID
   b. Report: 'I've started [operation]. The ID is [ID]. This will complete asynchronously.'
   c. Do NOT try to wait for it in this response

2. If asked to check status:
   a. Make ONE status API call
   b. Report the current status
   c. If still pending: 'Still processing. Check again in [N] seconds.'
   d. Do NOT loop checking multiple times in one response

3. Maximum 3 status checks per conversation turn.
   If still pending after 3 checks: ask user to try again later."
```

### Option 5: Structured async task management

```python
from dataclasses import dataclass
from enum import Enum

class TaskStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"

@dataclass
class AsyncTask:
    task_id: str
    operation: str
    status: TaskStatus = TaskStatus.PENDING
    result: dict = None
    created_at: float = 0
    completed_at: float = None

    def is_terminal(self) -> bool:
        return self.status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.TIMED_OUT)

class AsyncTaskManager:
    def __init__(self):
        self._tasks = {}
        self._callbacks = {}  # task_id -> callback_fn

    def create_task(self, task_id: str, operation: str, callback=None) -> AsyncTask:
        task = AsyncTask(task_id=task_id, operation=operation, created_at=time.time())
        self._tasks[task_id] = task
        if callback:
            self._callbacks[task_id] = callback
        return task

    def complete_task(self, task_id: str, result: dict):
        if task_id in self._tasks:
            task = self._tasks[task_id]
            task.status = TaskStatus.COMPLETED
            task.result = result
            task.completed_at = time.time()
            # Fire callback if registered
            if task_id in self._callbacks:
                asyncio.create_task(self._callbacks[task_id](task))

task_mgr = AsyncTaskManager()
```

## Polling Efficiency Comparison

| Strategy | API calls per 5min | Token cost | Best for |
|---|---|---|---|
| Tight loop (1s interval) | 300 | Very high | Never — don't do this |
| Fixed 10s interval | 30 | High | Simple status checks |
| Exponential backoff | 8–10 | Low | Standard async ops |
| Webhook + suspend | 0 | Minimal | Long-running operations |
| Check on user turn | 0 | Zero | User-driven workflows |

## Expected Token Savings
Tight polling loop (30-minute wait): ~60 polls × 500 tokens = 30,000 tokens
Webhook suspend + resume: ~500 tokens total

## Environment
- Agents triggering payment processing, CI/CD builds, email delivery, or other async APIs
- Source: direct experience; async waiting is a common agent anti-pattern
