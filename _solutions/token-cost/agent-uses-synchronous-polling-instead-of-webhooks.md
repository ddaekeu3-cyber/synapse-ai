---
layout: solution
title: "Agent Uses Synchronous Polling Instead of Webhooks"
category: token-cost
description: "Agent polls an external API every few seconds asking 'is the job done yet?' — burning output tokens on repeated LLM reasoning turns and API calls — when a webhook would deliver the result once for free."
tags: [token-cost, performance, webhooks, polling, async]
---

## Symptom

Logs show the agent issuing the same tool call repeatedly, with the model re-reasoning each time:

```
[10:01:00] check_job_status(job_id="job-123") → {"status": "running", "progress": 20}
[10:01:05] check_job_status(job_id="job-123") → {"status": "running", "progress": 35}
[10:01:10] check_job_status(job_id="job-123") → {"status": "running", "progress": 51}
[10:01:15] check_job_status(job_id="job-123") → {"status": "running", "progress": 68}
[10:01:20] check_job_status(job_id="job-123") → {"status": "completed", "result": ...}
```

Each check costs one LLM turn (~500 tokens). A 20-second job triggers 4 unnecessary turns before the final result. At scale, polling burns more tokens than the actual task.

## Root Cause

The agent is designed around synchronous request-response rather than event-driven patterns:

```python
import anthropic
import time
import json

client = anthropic.Anthropic(api_key="sk-live-...")

tools = [{"name": "check_job_status", "description": "Check if job is done",
          "input_schema": {"type": "object", "properties": {"job_id": {"type": "string"}}, "required": ["job_id"]}}]

def poll_until_done(job_id: str) -> str:
    messages = [{"role": "user", "content": f"Check job {job_id} until it's done."}]

    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            tools=tools,
            messages=messages,
        )
        # Model reasons about the status each time — burning tokens for no value
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            return next(b.text for b in response.content if hasattr(b, "text"))

        # Tool call → check status → loop back
        for block in response.content:
            if block.type == "tool_use":
                status = check_job_api(block.input["job_id"])
                messages.append({"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(status)}
                ]})
                if status["status"] == "running":
                    time.sleep(5)  # Wait 5s before next LLM turn
```

---

## Fix

### Option 1 — Wait outside the LLM loop; call LLM only once on completion

Poll in Python (no LLM involved), then call the LLM exactly once when the result is ready.

```python
import anthropic
import time
import json

client = anthropic.Anthropic(api_key="sk-live-...")


def check_job_api(job_id: str) -> dict:
    """Simulated external API — replace with real HTTP call."""
    # In production: requests.get(f"https://api.example.com/jobs/{job_id}")
    return {"status": "completed", "result": {"rows_processed": 42_000}}


def wait_for_job(job_id: str, poll_interval: float = 5.0, timeout: float = 300.0) -> dict:
    """Pure Python polling — zero LLM tokens consumed during wait."""
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        status = check_job_api(job_id)

        if status["status"] == "completed":
            return status
        if status["status"] in ("failed", "cancelled"):
            raise RuntimeError(f"Job {job_id} ended with status: {status['status']}")

        time.sleep(poll_interval)

    raise TimeoutError(f"Job {job_id} did not complete within {timeout}s")


def process_job_result(job_id: str) -> str:
    """Wait (no LLM), then ask LLM to process the result once."""
    result = wait_for_job(job_id)  # No tokens used here

    # Single LLM call with the complete result
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{
            "role": "user",
            "content": f"Summarise this job result: {json.dumps(result)}"
        }]
    )
    return response.content[0].text.strip()


print(process_job_result("job-123"))

# Expected Token Savings: N polling turns eliminated; only 1 LLM call needed
# Environment: batch processing agents (data exports, ML training, report generation)
```

---

### Option 2 — Async polling with asyncio (non-blocking, no LLM turns)

Use `asyncio.sleep()` so the process stays responsive during the wait without burning LLM tokens.

```python
import asyncio
import anthropic
import json
import time

client = anthropic.AsyncAnthropic(api_key="sk-live-...")

# Simulated job store
_jobs = {"job-456": {"start": time.time(), "duration": 3.0}}


async def check_job_async(job_id: str) -> dict:
    """Async HTTP check — replace with aiohttp/httpx call."""
    job = _jobs.get(job_id, {})
    elapsed = time.time() - job.get("start", time.time())
    if elapsed >= job.get("duration", 5.0):
        return {"status": "completed", "result": {"rows": 10_000}}
    return {"status": "running", "progress": int(elapsed / job["duration"] * 100)}


async def wait_for_job_async(
    job_id: str,
    poll_interval: float = 1.0,
    timeout: float = 120.0,
) -> dict:
    """Async wait — zero LLM calls, non-blocking."""
    deadline = asyncio.get_event_loop().time() + timeout

    while asyncio.get_event_loop().time() < deadline:
        status = await check_job_async(job_id)

        if status["status"] == "completed":
            return status
        if status["status"] == "failed":
            raise RuntimeError(f"Job failed: {status}")

        await asyncio.sleep(poll_interval)  # Non-blocking sleep

    raise TimeoutError(f"Job {job_id} timed out")


async def process_async(job_id: str) -> str:
    result = await wait_for_job_async(job_id)  # Async wait — no tokens

    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        messages=[{"role": "user", "content": f"Job result: {json.dumps(result)}. Summarise."}]
    )
    return response.content[0].text.strip()


# Can run multiple jobs concurrently — each waits independently
async def main():
    results = await asyncio.gather(
        process_async("job-456"),
        process_async("job-456"),
    )
    for r in results:
        print(r)

asyncio.run(main())

# Expected Token Savings: async wait handles multiple jobs concurrently at zero LLM cost
# Environment: async agents running many parallel long-running jobs
```

---

### Option 3 — Webhook receiver: job delivers result to agent

Flip the model: register a webhook URL with the external service. When the job completes, the service POSTs the result to your agent — zero polling.

```python
import anthropic
import json
import asyncio
from fastapi import FastAPI, Request, BackgroundTasks
import httpx

app = FastAPI()
client = anthropic.Anthropic(api_key="sk-live-...")

# In-memory result store — use Redis in production
_job_results: dict[str, dict] = {}
_job_events: dict[str, asyncio.Event] = {}


@app.post("/webhook/job-complete")
async def receive_job_result(request: Request, background_tasks: BackgroundTasks):
    """External service posts here when the job completes."""
    payload = await request.json()
    job_id = payload.get("job_id")

    if job_id:
        _job_results[job_id] = payload
        event = _job_events.get(job_id)
        if event:
            event.set()  # Signal any waiter

    # Optionally process in background
    background_tasks.add_task(process_completed_job, job_id, payload)
    return {"received": True}


async def process_completed_job(job_id: str, result: dict) -> None:
    """Process the result with LLM after webhook delivery."""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        messages=[{
            "role": "user",
            "content": f"Job {job_id} completed. Result: {json.dumps(result)}. Summarise."
        }]
    )
    print(f"[job-{job_id}] {response.content[0].text}")


async def submit_job_and_wait(job_payload: dict, webhook_url: str) -> dict:
    """Submit job with webhook URL; wait for delivery."""
    job_id = job_payload["id"]
    event = asyncio.Event()
    _job_events[job_id] = event

    # Submit job with webhook callback URL
    async with httpx.AsyncClient() as http:
        await http.post(
            "https://api.example.com/jobs",
            json={**job_payload, "webhook_url": webhook_url},
        )

    # Wait for webhook delivery (no polling, no LLM turns during wait)
    try:
        await asyncio.wait_for(event.wait(), timeout=300.0)
        return _job_results.get(job_id, {})
    except asyncio.TimeoutError:
        raise TimeoutError(f"Job {job_id} webhook not received within 300s")
    finally:
        _job_events.pop(job_id, None)

# Expected Token Savings: 100% of polling LLM turns eliminated — result delivered once
# Environment: any agent integrated with services that support webhook callbacks
```

---

### Option 4 — Long-polling tool (single blocking call instead of many short ones)

When webhooks aren't available, use the API's long-polling endpoint (if it has one) to hold the connection open until the job completes.

```python
import anthropic
import httpx
import json
import time

client = anthropic.Anthropic(api_key="sk-live-...")


async def long_poll_job(job_id: str, timeout: float = 60.0) -> dict:
    """
    Long-polling: single HTTP call that blocks until the job completes or times out.
    Many APIs support this via ?wait=60 query parameter.
    """
    async with httpx.AsyncClient(timeout=timeout + 5) as http:
        # API holds the connection open up to 60s then returns result or timeout
        resp = await http.get(
            f"https://api.example.com/jobs/{job_id}/wait",
            params={"timeout": int(timeout)},
        )

        if resp.status_code == 200:
            return resp.json()
        elif resp.status_code == 202:
            # Still running after timeout — need to retry long-poll
            raise TimeoutError(f"Job not done after {timeout}s long-poll window")
        else:
            resp.raise_for_status()


def get_tools():
    return [{
        "name": "wait_for_job",
        "description": (
            "Wait for a job to complete using long-polling. "
            "Makes ONE API call that blocks until the result is ready (up to 60s). "
            "Returns the complete result when done. "
            "If not done within 60s, call again with the same job_id."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string"},
                "timeout_seconds": {"type": "integer", "default": 60},
            },
            "required": ["job_id"]
        }
    }]


# Agent calls wait_for_job once (or twice if >60s) instead of polling every 5s
# 1 tool call per 60s window vs 12 tool calls per 60s at 5s intervals
# Expected Token Savings: 11 of 12 LLM reasoning turns eliminated per 60s window
# Environment: APIs that support long-polling (?wait=N or similar)
```

---

### Option 5 — Callback queue with background worker

Store job IDs in a queue. A background worker polls (cheaply, no LLM) and triggers the LLM only on completion.

```python
import anthropic
import asyncio
import json
import time
from dataclasses import dataclass
from collections import deque

client = anthropic.AsyncAnthropic(api_key="sk-live-...")


@dataclass
class PendingJob:
    job_id: str
    submitted_at: float
    on_complete: asyncio.Future


_pending: deque[PendingJob] = deque()
_lock = asyncio.Lock()


async def _check_job_api(job_id: str) -> dict:
    """Cheap HTTP check — no LLM."""
    await asyncio.sleep(0.01)  # Simulated latency
    elapsed = time.time() - float(job_id.split("-")[-1]) if "-" in job_id else 5
    if elapsed > 2:
        return {"status": "completed", "result": {"rows": 5000}}
    return {"status": "running"}


async def _background_poller(interval: float = 2.0) -> None:
    """Polls all pending jobs every 2s — no LLM calls."""
    while True:
        async with _lock:
            still_pending = deque()
            for job in list(_pending):
                status = await _check_job_api(job.job_id)
                if status["status"] == "completed":
                    job.on_complete.set_result(status["result"])
                elif status["status"] == "failed":
                    job.on_complete.set_exception(RuntimeError("Job failed"))
                else:
                    still_pending.append(job)
            _pending.clear()
            _pending.extend(still_pending)

        await asyncio.sleep(interval)


async def submit_and_await(job_id: str) -> str:
    """Submit to queue; LLM processes result exactly once on completion."""
    loop = asyncio.get_event_loop()
    future: asyncio.Future = loop.create_future()
    job = PendingJob(job_id=job_id, submitted_at=time.time(), on_complete=future)

    async with _lock:
        _pending.append(job)

    result = await future  # Wait without LLM involvement

    # One LLM call when result arrives
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=150,
        messages=[{"role": "user", "content": f"Result ready: {json.dumps(result)}. Summarise."}]
    )
    return response.content[0].text.strip()


async def main():
    poller = asyncio.create_task(_background_poller())

    job_id = f"job-{time.time():.0f}"
    summary = await submit_and_await(job_id)
    print(f"Summary: {summary}")

    poller.cancel()

asyncio.run(main())

# Expected Token Savings: background poller uses zero LLM tokens; LLM called once per job
# Environment: high-throughput agents managing many concurrent long-running jobs
```

---

### Option 6 — Cost comparison tool: show polling vs webhook token burn

Instrument the agent to track and report the token cost of polling vs event-driven approaches.

```python
import anthropic
import time
from dataclasses import dataclass, field

client = anthropic.Anthropic(api_key="sk-live-...")


@dataclass
class TokenMeter:
    label: str
    input_tokens: int = 0
    output_tokens: int = 0
    calls: int = 0
    start: float = field(default_factory=time.monotonic)

    def record(self, response: anthropic.types.Message) -> None:
        self.input_tokens += response.usage.input_tokens
        self.output_tokens += response.usage.output_tokens
        self.calls += 1

    def report(self) -> str:
        elapsed = time.monotonic() - self.start
        total = self.input_tokens + self.output_tokens
        # Haiku pricing: $0.00025/1K input, $0.00125/1K output
        cost = self.input_tokens / 1000 * 0.00025 + self.output_tokens / 1000 * 0.00125
        return (
            f"{self.label}:\n"
            f"  Calls: {self.calls} | Elapsed: {elapsed:.1f}s\n"
            f"  Input: {self.input_tokens:,} | Output: {self.output_tokens:,} | Total: {total:,}\n"
            f"  Cost:  ${cost:.4f}"
        )


def polling_approach(job_result: dict, poll_count: int = 5) -> TokenMeter:
    """Simulate polling: N empty turns + 1 result turn."""
    meter = TokenMeter("POLLING")

    for i in range(poll_count - 1):
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=20,
            messages=[{"role": "user", "content": "Is the job done yet?"}]
        )
        meter.record(resp)

    # Final call with result
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=150,
        messages=[{"role": "user", "content": f"Job done: {job_result}. Summarise."}]
    )
    meter.record(resp)
    return meter


def webhook_approach(job_result: dict) -> TokenMeter:
    """Simulate webhook: 1 call when result arrives."""
    meter = TokenMeter("WEBHOOK")

    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=150,
        messages=[{"role": "user", "content": f"Job done: {job_result}. Summarise."}]
    )
    meter.record(resp)
    return meter


job_result = {"rows_processed": 50_000, "duration_seconds": 18, "status": "completed"}

polling_meter = polling_approach(job_result, poll_count=6)
webhook_meter = webhook_approach(job_result)

print(polling_meter.report())
print()
print(webhook_meter.report())

polling_cost = polling_meter.input_tokens / 1000 * 0.00025 + polling_meter.output_tokens / 1000 * 0.00125
webhook_cost = webhook_meter.input_tokens / 1000 * 0.00025 + webhook_meter.output_tokens / 1000 * 0.00125
print(f"\nSavings: ${polling_cost - webhook_cost:.4f} ({(1 - webhook_cost/polling_cost)*100:.0f}% cheaper with webhooks)")

# Expected Token Savings: typically 70–90% reduction in token cost per job
# Environment: use this to justify refactoring polling agents to webhook-driven architecture
```

---

## Comparison

| Option | LLM Calls During Wait | Requires Webhook Support | Concurrent Jobs | Complexity |
|--------|----------------------|--------------------------|-----------------|------------|
| 1 | 0 (sync Python wait) | No | No | Low |
| 2 | 0 (async wait) | No | Yes | Low |
| 3 | 0 (webhook push) | Yes | Yes | Medium |
| 4 | 0 (long-poll) | Long-poll endpoint | Yes | Low |
| 5 | 0 (background queue) | No | Yes | Medium |
| 6 | Benchmarking only | No | No | Low |

**Recommended starting point:** Option 1 for any synchronous batch job — move the `time.sleep()` loop out of the LLM message loop. Option 3 (webhook) for production integrations with services that support callbacks. Option 2 for async agents managing multiple concurrent jobs.
