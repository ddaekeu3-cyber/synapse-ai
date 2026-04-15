---
layout: solution
title: "First Agent Response Is 10x Slower Than Subsequent Responses — Cold Start"
category: performance
description: "The first request to the agent takes 5–15 seconds while all subsequent requests complete in under 1 second. Caused by connection pool initialization, model loading, or session setup overhead."
tags: [cold-start, latency, connection-pool, performance, warm-up]
---

# First Agent Response Is 10x Slower Than Subsequent Responses — Cold Start

## Symptom
- First request after startup: 8–15 seconds
- Second and subsequent requests: <1 second
- Restart agent → first request is slow again
- Users complain about the "first message delay"
- Load testing shows P99 latency is dominated by cold-start requests

## Root Cause
Multiple factors compound on first request:
1. **Connection pool empty** — TCP + TLS handshake to Anthropic API (~200–500ms)
2. **DNS resolution** — first lookup isn't cached (~50–200ms)
3. **Session initialization** — system prompt, tool schemas, and session state built (~100–500ms)
4. **Python/Node import overhead** — if agent process starts on first request

## Fix

### Option 1: Connection pool prewarm on startup

```python
import httpx
import asyncio

# Pre-initialize HTTP client with connection pool
_client = httpx.AsyncClient(
    limits=httpx.Limits(max_connections=10, keepalive_expiry=30),
    timeout=httpx.Timeout(30.0)
)

async def prewarm_connections():
    """Make a minimal request on startup to initialize connection pool"""
    try:
        await _client.get("https://api.anthropic.com/", timeout=5)
    except Exception:
        pass  # Expected — just warming the connection

# Call on application startup
asyncio.create_task(prewarm_connections())
```

### Option 2: Send a ping request on startup

```python
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

async def prewarm_model(model="claude-haiku-4-5-20251001"):
    """Send minimal request to warm connection on startup"""
    await client.messages.create(
        model=model,
        max_tokens=1,
        messages=[{"role": "user", "content": "ping"}]
    )

# In your startup handler
@app.on_event("startup")
async def startup():
    asyncio.create_task(prewarm_model())
```

### Option 3: OpenClaw keepalive config

```yaml
# openclaw.config.yaml
http:
  connection_pool:
    max_connections: 10
    keep_alive: true
    keep_alive_timeout_ms: 30000
    prewarm_on_startup: true
  dns_cache:
    enabled: true
    ttl_seconds: 300
```

### Option 4: Separate slow initialization from first request

```python
async def initialize_agent():
    """Run all slow init at startup, not on first request"""
    # Build system prompt (may involve file reads)
    system_prompt = await build_system_prompt()
    # Load tool schemas
    tool_schemas = await load_tool_schemas()
    # Warm connection
    await prewarm_model()

    return AgentSession(system_prompt=system_prompt, tools=tool_schemas)

# Application startup
agent = await initialize_agent()  # Do this at startup
```

### Option 5: Keep-alive health check (for Docker/k8s)

```yaml
# docker-compose.yml
services:
  agent:
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8080/health"]
      interval: 30s        # Ping every 30s keeps connection warm
      timeout: 5s
      retries: 3
      start_period: 10s
```

The health check endpoint keeps the connection pool alive between real requests.

## Measurement

```python
import time

async def measure_cold_vs_warm():
    # First request (cold)
    start = time.time()
    await agent.complete("hello")
    cold_time = time.time() - start

    # Second request (warm)
    start = time.time()
    await agent.complete("hello")
    warm_time = time.time() - start

    print(f"Cold start: {cold_time:.2f}s | Warm: {warm_time:.2f}s")
    print(f"Cold start overhead: {cold_time - warm_time:.2f}s")
```

## Expected Token Savings
Cold start doesn't waste tokens, but wastes ~10s per agent restart.
Prewarm eliminates the UX degradation entirely.

## Environment
- Python/Node.js async agent backends
- Any deployment that restarts agents between sessions
- Source: direct measurement, Anthropic API connection profiling
