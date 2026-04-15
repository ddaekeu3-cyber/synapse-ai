---
layout: solution
title: "Anthropic API 529 Overloaded — Model Unavailable, Agent Crashes"
category: rate-limit
description: "Anthropic API returns HTTP 529 'Overloaded' during high traffic periods. Agent crashes or fails permanently instead of waiting and retrying with exponential backoff."
tags: [429, 529, overloaded, anthropic, fallback, retry]
---

# Anthropic API 529 Overloaded — Model Unavailable, Agent Crashes

## Symptom
- API returns `529 Overloaded` or `{"error": {"type": "overloaded_error"}}`
- Agent crashes rather than retrying
- Happens during peak hours (business hours UTC) or large batch jobs
- Sometimes lasts 30 seconds, sometimes 5–10 minutes
- Affects specific models more than others (Opus under high load)

## Root Cause
529 is an Anthropic-specific status for capacity overload — not a rate limit (429). Many retry implementations only handle 429, leaving 529 as an unhandled error. The correct response is exponential backoff + optional model fallback.

## Fix

### Option 1: Handle 529 in retry logic

```python
import asyncio, random
from anthropic import Anthropic, APIStatusError

client = Anthropic()

async def complete_with_retry(messages, model="claude-opus-4-6", max_retries=5):
    base_delay = 1.0

    for attempt in range(max_retries):
        try:
            return client.messages.create(
                model=model,
                max_tokens=4096,
                messages=messages
            )
        except APIStatusError as e:
            if e.status_code in (429, 529) and attempt < max_retries - 1:
                delay = base_delay * (2 ** attempt) + random.uniform(0, 1)
                print(f"API {e.status_code} — retrying in {delay:.1f}s (attempt {attempt+1})")
                await asyncio.sleep(delay)
            else:
                raise
```

### Option 2: Fallback to lighter model on 529

```python
MODEL_FALLBACK_CHAIN = [
    "claude-opus-4-6",
    "claude-sonnet-4-6",   # Fallback — usually less affected
    "claude-haiku-4-5-20251001",  # Emergency fallback
]

async def complete_with_fallback(messages, preferred_model="claude-opus-4-6"):
    models = MODEL_FALLBACK_CHAIN[MODEL_FALLBACK_CHAIN.index(preferred_model):]

    for model in models:
        for attempt in range(3):
            try:
                response = client.messages.create(
                    model=model,
                    max_tokens=4096,
                    messages=messages
                )
                if model != preferred_model:
                    print(f"Warning: Used fallback model {model}")
                return response
            except APIStatusError as e:
                if e.status_code == 529:
                    await asyncio.sleep(2 ** attempt)
                    continue
                raise
        # This model is overloaded too — try next
        print(f"Model {model} overloaded, trying next...")

    raise RuntimeError("All models in fallback chain are overloaded")
```

### Option 3: Check Anthropic status before batch jobs

```python
import httpx

async def check_anthropic_status():
    """Check status.anthropic.com before starting large batch"""
    async with httpx.AsyncClient() as client:
        resp = await client.get("https://status.anthropic.com/api/v2/status.json")
        status = resp.json()
        indicator = status['status']['indicator']
        if indicator != 'none':
            print(f"Warning: Anthropic status is '{indicator}' — {status['status']['description']}")
            return False
    return True
```

### Option 4: OpenClaw config — retry on 529

```yaml
# openclaw.config.yaml
providers:
  anthropic:
    retry:
      on_status_codes: [429, 529]
      max_attempts: 5
      initial_delay_ms: 1000
      backoff_multiplier: 2.0
      jitter: true
    fallback_model: claude-sonnet-4-6
```

## Expected Token Savings
Agent crashing on 529 and requiring manual restart: ~5,000 tokens + lost work
Automatic retry + fallback: 0 extra tokens, seamless recovery

## Environment
- Anthropic API (all models)
- Most common: Claude Opus during peak hours, large batch workloads
- Source: direct experience, Anthropic API documentation
