---
layout: solution
title: "Parallel Agent Requests Hit Rate Limit 10x Faster Than Expected"
category: rate-limit
description: "Running multiple agents in parallel or processing a batch causes 429 errors within seconds. Concurrent requests consume the rate limit budget much faster than sequential requests."
tags: [rate-limit, 429, parallel, concurrent, batch, rpm]
---

# Parallel Agent Requests Hit Rate Limit 10x Faster Than Expected

## Symptom
- Single agent runs fine for hours without hitting rate limits
- Running 10 agents in parallel → 429 errors within seconds
- Batch processing job hits rate limit after 30 seconds
- RPM (requests per minute) limit exceeded immediately at batch start
- Adding more workers makes the problem worse

## Root Cause
Rate limits apply globally across all concurrent requests. If your limit is 60 RPM (1/second average) and you fire 10 parallel requests simultaneously, you're sending 10 requests in 1 second — consuming 10x your per-second budget in one burst.

## Fix

### Option 1: Rate-limited async semaphore

```python
import asyncio, time

class RateLimiter:
    def __init__(self, max_per_minute):
        self.max_per_minute = max_per_minute
        self.semaphore = asyncio.Semaphore(max(1, max_per_minute // 10))  # Burst limit
        self.request_times = []

    async def acquire(self):
        async with self.semaphore:
            # Enforce per-minute limit
            now = time.time()
            self.request_times = [t for t in self.request_times if now - t < 60]

            if len(self.request_times) >= self.max_per_minute:
                oldest = self.request_times[0]
                sleep_time = 60 - (now - oldest) + 0.1
                await asyncio.sleep(sleep_time)

            self.request_times.append(time.time())

limiter = RateLimiter(max_per_minute=50)  # Stay under 60 RPM limit

async def rate_limited_complete(messages):
    await limiter.acquire()
    return await client.messages.create(...)
```

### Option 2: Batch with concurrency control

```python
async def process_batch(items, max_concurrent=5, delay_between=1.0):
    """Process items with controlled concurrency"""
    semaphore = asyncio.Semaphore(max_concurrent)
    results = []

    async def process_one(item):
        async with semaphore:
            result = await agent.process(item)
            await asyncio.sleep(delay_between)  # Rate limiting delay
            return result

    tasks = [process_one(item) for item in items]
    return await asyncio.gather(*tasks)

# Process 100 items with max 5 concurrent, 1s between each = ~5 RPM per slot
results = await process_batch(items, max_concurrent=5, delay_between=1.0)
```

### Option 3: Use Anthropic's built-in batch API for large batches

```python
# For bulk processing (not real-time), use Message Batches API
# Up to 10,000 requests per batch, processed over 24 hours
# Same quality, lower cost, no rate limit concerns

import anthropic

client = anthropic.Anthropic()

batch = client.messages.batches.create(
    requests=[
        {
            "custom_id": f"item-{i}",
            "params": {
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 1024,
                "messages": [{"role": "user", "content": item}]
            }
        }
        for i, item in enumerate(items)
    ]
)

# Poll for results
while batch.processing_status == "in_progress":
    await asyncio.sleep(60)
    batch = client.messages.batches.retrieve(batch.id)
```

### Option 4: Respect Retry-After header

```python
import asyncio
from anthropic import RateLimitError

async def complete_with_retry(messages, max_retries=5):
    for attempt in range(max_retries):
        try:
            return await client.messages.create(...)
        except RateLimitError as e:
            if attempt == max_retries - 1:
                raise
            # Use Retry-After header if available
            retry_after = getattr(e, 'retry_after', None) or (2 ** attempt)
            print(f"Rate limited — waiting {retry_after}s")
            await asyncio.sleep(retry_after)
```

### Option 5: OpenClaw config — global rate limiter

```yaml
# openclaw.config.yaml
providers:
  anthropic:
    rate_limit:
      max_rpm: 50              # Stay 15% under your 60 RPM limit
      max_concurrent: 5        # Never more than 5 in-flight
      queue_overflow: wait     # Queue requests, don't drop them
      queue_timeout_ms: 30000
```

## Expected Token Savings
Batch job crashing at start → restarting with backoff: ~20,000 tokens lost per crash
Rate limiter from the start: batch completes without interruption

## Environment
- Any agent deployment processing batches or parallel requests
- Anthropic API tiers: Free (3 RPM), Build (50 RPM), Scale (2000 RPM)
- Source: direct experience, Anthropic rate limit documentation
