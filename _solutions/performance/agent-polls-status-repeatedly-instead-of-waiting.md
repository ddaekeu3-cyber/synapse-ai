---
layout: solution
title: "Agent Polls Status Every Second — Burning Tokens Waiting for Background Job"
category: performance
description: "Agent checks job status, file existence, or service health every few seconds in a loop. Each poll costs tokens and API calls. Polling every 1s over 5 minutes = 300 unnecessary tool calls."
tags: [polling, performance, token-waste, background-job, sleep]
---

# Agent Polls Status Every Second — Burning Tokens Waiting for Background Job

## Symptom
- Agent calls `check_job_status()` or `file_exists()` repeatedly in a tight loop
- Log shows: `tool: check_status → pending, check_status → pending, check_status → done`
- Each check costs 1 tool call + agent reasoning tokens
- Waiting for a 5-minute job: 300 status checks = 300 tool calls
- Agent also stays awake and consuming context the entire wait time

## Root Cause
No sleep/wait mechanism between checks. The agent is designed to be responsive and keeps checking. Without an explicit "wait N seconds between checks" instruction or a proper async notification mechanism, the agent defaults to immediate re-polling.

## Fix

### Option 1: Exponential backoff polling

```python
import asyncio, time

async def poll_with_backoff(check_fn, timeout=300, initial_interval=5, max_interval=60):
    """Poll with exponential backoff — much fewer calls than constant polling"""
    interval = initial_interval
    elapsed = 0
    attempt = 0

    while elapsed < timeout:
        result = await check_fn()
        if result.get('status') == 'complete':
            return result

        # Exponential backoff: 5s, 10s, 20s, 40s, 60s (capped)
        await asyncio.sleep(interval)
        elapsed += interval
        interval = min(interval * 2, max_interval)
        attempt += 1
        print(f"Still waiting... ({elapsed}s elapsed, {attempt} checks)")

    raise TimeoutError(f"Job not complete after {timeout}s")

# 5-minute job: constant 1s = 300 checks
# 5-minute job: exponential backoff = ~9 checks
```

### Option 2: Webhook/callback instead of polling

```python
# BAD — polling
while True:
    status = api.get_job_status(job_id)
    if status == "done":
        break
    time.sleep(1)

# GOOD — webhook callback
@app.post("/webhook/job-complete")
async def job_complete_webhook(job_id: str, result: dict):
    """API calls this when job finishes — no polling needed"""
    await notify_agent(job_id, result)

# Start job with webhook URL
job = api.start_job(data, webhook_url="https://your-agent.com/webhook/job-complete")
```

### Option 3: Instruct agent to wait

```
System prompt:
"When waiting for background jobs, files, or processes:
- Use sleep/wait between checks — do NOT poll continuously
- Minimum interval: 10 seconds for fast jobs, 60 seconds for slow jobs
- Pattern: start job → wait N seconds → check once → wait again if needed
- Never poll more than once per 10 seconds

Tool call limit for waiting: maximum 10 status checks per job.
If not done after 10 checks, report 'job still running' and stop checking."
```

### Option 4: Process monitoring with notification

```python
import asyncio, subprocess

async def run_and_notify(command, agent_callback):
    """Run process and notify agent when done — no polling"""
    proc = await asyncio.create_subprocess_shell(
        command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )

    stdout, stderr = await proc.communicate()
    exit_code = proc.returncode

    # Notify agent once when complete
    await agent_callback({
        "status": "complete" if exit_code == 0 else "failed",
        "exit_code": exit_code,
        "stdout": stdout.decode()[-2000:],  # Last 2000 chars
        "stderr": stderr.decode()[-500:]
    })
```

### Option 5: Token budget for polling

```python
MAX_POLL_CALLS = 10

async def bounded_poll(check_fn, interval=30):
    """Stop polling after max attempts"""
    for attempt in range(MAX_POLL_CALLS):
        result = await check_fn()
        if result['done']:
            return result
        if attempt < MAX_POLL_CALLS - 1:
            print(f"Check {attempt+1}/{MAX_POLL_CALLS} — waiting {interval}s")
            await asyncio.sleep(interval)

    return {"done": False, "message": f"Not complete after {MAX_POLL_CALLS} checks"}
```

## Token Cost Comparison

| Strategy | 5-min job | Token cost |
|----------|-----------|-----------|
| Poll every 1s | 300 checks | ~30,000 tokens |
| Poll every 10s | 30 checks | ~3,000 tokens |
| Exponential backoff | 9 checks | ~900 tokens |
| Webhook | 1 check | ~100 tokens |

## Expected Token Savings
Polling every 1s for 5 minutes: ~30,000 tokens
Exponential backoff: ~900 tokens — 97% savings

## Environment
- Any agent running background jobs, build processes, or async tasks
- Source: direct experience with agents polling CI/CD jobs
