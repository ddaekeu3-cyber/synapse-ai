---
layout: solution
title: "Telegram Sends Duplicate Responses — Webhook Retry Causes Double Processing"
category: concurrency
description: "Telegram retries webhook delivery when your server is slow to respond. Agent processes the same message twice, sending two responses to the user."
tags: [telegram, webhook, deduplication, idempotency]
---

# Telegram Sends Duplicate Responses — Webhook Retry Causes Double Processing

## Symptom
- User sends one message, bot responds twice
- Second response usually arrives 3–5 seconds after the first
- Only happens under load or when processing is slow (>5s)
- Does not happen in polling mode — only webhook mode

## Root Cause
Telegram's webhook delivery requires a 200 OK response within 5 seconds. If your agent takes longer (e.g., LLM inference + tool calls), Telegram retries delivery. Without message deduplication, the agent processes and responds to the same message twice.

## Fix

### Option 1: Respond 200 immediately, process async

```python
from fastapi import FastAPI, Request, BackgroundTasks

app = FastAPI()

@app.post("/webhook")
async def webhook(request: Request, background_tasks: BackgroundTasks):
    update = await request.json()
    # Respond to Telegram immediately
    background_tasks.add_task(process_update, update)
    return {"ok": True}  # 200 within milliseconds

async def process_update(update):
    # Process here — Telegram doesn't wait for this
    await agent.handle(update)
```

### Option 2: Deduplicate by update_id

```python
import asyncio
from collections import deque

_processed_ids = deque(maxlen=10000)

async def webhook(update):
    update_id = update.get('update_id')
    if update_id in _processed_ids:
        return  # Already processed — discard duplicate
    _processed_ids.append(update_id)
    await process_update(update)
```

### Option 3: Redis-based deduplication (for multi-replica deployments)

```python
import redis.asyncio as redis

r = redis.Redis()

async def webhook(update):
    update_id = str(update['update_id'])
    was_set = await r.set(f"update:{update_id}", "1", nx=True, ex=300)
    if not was_set:
        return  # Another replica already processed this
    await process_update(update)
```

## Expected Token Savings
Debugging duplicate response issue: ~8,000 tokens
This fix: ~200 tokens to implement

## Environment
- Telegram Bot API (webhook mode)
- Any Python/Node.js agent backend
- Source: direct experience, common pattern in Telegram bot development
