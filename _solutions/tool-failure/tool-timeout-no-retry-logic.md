---
layout: solution
title: "Tool Call Times Out and Agent Gives Up — No Retry Logic"
category: tool-failure
description: "Agent calls a tool that takes too long, hits a timeout, and immediately gives up or reports failure. The tool would have succeeded on retry, but the agent doesn't attempt it."
tags: [timeout, retry, tool-failure, network, resilience]
---

# Tool Call Times Out and Agent Gives Up — No Retry Logic

## Symptom
- Agent calls an external API, gets `TimeoutError` after 10s, reports failure
- Tool call fails on first attempt; agent moves on without retrying
- Sporadic failures in tools that usually work — always on first try
- Agent says "I was unable to fetch the data" for transient network issues
- Logs show single timeout with no retry attempts

## Root Cause
No retry logic in the tool execution layer. A single timeout is treated as permanent failure. Most timeouts are transient — the service was momentarily busy, a connection dropped, or a cold-start delay occurred. A simple retry with backoff resolves the majority of these failures.

## Fix

### Option 1: Retry decorator with exponential backoff

```python
import time, functools
from typing import Callable, TypeVar

T = TypeVar("T")

def retry(max_attempts=3, base_delay=1.0, max_delay=30.0, exceptions=(Exception,)):
    """Decorator: retry on specified exceptions with exponential backoff"""
    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(func)
        def wrapper(*args, **kwargs) -> T:
            delay = base_delay
            for attempt in range(1, max_attempts + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    if attempt == max_attempts:
                        raise  # Last attempt — propagate
                    jitter = delay * 0.1 * (2 * __import__('random').random() - 1)
                    sleep_time = min(delay + jitter, max_delay)
                    print(f"Attempt {attempt} failed: {e}. Retrying in {sleep_time:.1f}s...")
                    time.sleep(sleep_time)
                    delay *= 2  # Exponential backoff
        return wrapper
    return decorator

# Usage
@retry(max_attempts=3, base_delay=1.0, exceptions=(TimeoutError, ConnectionError))
def fetch_data(url: str) -> dict:
    import httpx
    response = httpx.get(url, timeout=10.0)
    response.raise_for_status()
    return response.json()
```

### Option 2: Async retry with tenacity

```python
from tenacity import (
    retry, stop_after_attempt, wait_exponential,
    retry_if_exception_type, before_sleep_log
)
import logging, httpx

logger = logging.getLogger(__name__)

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    retry=retry_if_exception_type((httpx.TimeoutException, httpx.ConnectError)),
    before_sleep=before_sleep_log(logger, logging.WARNING)
)
async def call_external_api(endpoint: str, payload: dict) -> dict:
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(endpoint, json=payload)
        response.raise_for_status()
        return response.json()
```

### Option 3: Retry inside the tool wrapper

```python
import asyncio

async def execute_tool_with_retry(
    tool_name: str,
    tool_input: dict,
    max_attempts: int = 3,
    timeout_seconds: float = 30.0
) -> dict:
    last_error = None

    for attempt in range(1, max_attempts + 1):
        try:
            result = await asyncio.wait_for(
                dispatch_tool(tool_name, tool_input),
                timeout=timeout_seconds
            )
            if attempt > 1:
                print(f"Tool {tool_name} succeeded on attempt {attempt}")
            return result

        except asyncio.TimeoutError:
            last_error = f"Timeout after {timeout_seconds}s"
            print(f"Tool {tool_name} attempt {attempt}/{max_attempts}: {last_error}")

        except Exception as e:
            if not is_retryable(e):
                raise  # Don't retry permanent errors (404, auth, validation)
            last_error = str(e)
            print(f"Tool {tool_name} attempt {attempt}/{max_attempts}: {e}")

        if attempt < max_attempts:
            await asyncio.sleep(2 ** (attempt - 1))  # 1s, 2s, 4s

    raise RuntimeError(f"Tool {tool_name} failed after {max_attempts} attempts: {last_error}")


def is_retryable(error: Exception) -> bool:
    """Determine if error is worth retrying"""
    error_str = str(error).lower()
    # Retry: transient network/server issues
    retryable = ["timeout", "connection", "503", "429", "500", "502", "504"]
    # Don't retry: client errors, auth, not found
    non_retryable = ["401", "403", "404", "400", "validation", "invalid"]
    if any(s in error_str for s in non_retryable):
        return False
    return any(s in error_str for s in retryable)
```

### Option 4: Retry guidance in system prompt

```
System prompt:
"When a tool call fails with a timeout or transient error:
1. Retry the same tool call up to 2 more times before reporting failure
2. Wait 2 seconds between retries
3. Only report failure after 3 total attempts
4. Distinguish transient errors (timeout, 503, 429) from permanent errors (404, 401, 400)
5. For permanent errors, report immediately — retrying won't help"
```

### Option 5: Circuit breaker to avoid retry storms

```python
from datetime import datetime, timedelta

class CircuitBreaker:
    def __init__(self, failure_threshold=5, reset_timeout=60):
        self.failures = 0
        self.threshold = failure_threshold
        self.reset_timeout = reset_timeout
        self.opened_at = None
        self.state = "closed"  # closed, open, half-open

    def call(self, func, *args, **kwargs):
        if self.state == "open":
            if datetime.now() > self.opened_at + timedelta(seconds=self.reset_timeout):
                self.state = "half-open"
            else:
                raise RuntimeError("Circuit breaker open — tool temporarily disabled")

        try:
            result = func(*args, **kwargs)
            self._on_success()
            return result
        except Exception as e:
            self._on_failure()
            raise

    def _on_success(self):
        self.failures = 0
        self.state = "closed"

    def _on_failure(self):
        self.failures += 1
        if self.failures >= self.threshold:
            self.state = "open"
            self.opened_at = datetime.now()
            print(f"Circuit breaker opened after {self.failures} failures")
```

## Retry Decision Matrix

| Error type | Retry? | Notes |
|---|---|---|
| `TimeoutError` | Yes | Transient — retry with longer timeout |
| `ConnectionError` | Yes | Network blip — retry after delay |
| HTTP 429 (rate limit) | Yes | Retry after `Retry-After` header |
| HTTP 503 (unavailable) | Yes | Server busy — retry with backoff |
| HTTP 500 (server error) | Yes (limited) | Maybe transient — max 2 retries |
| HTTP 401 (auth) | No | Key is wrong — retrying won't help |
| HTTP 404 (not found) | No | Resource doesn't exist |
| HTTP 400 (bad request) | No | Fix the request |
| `ValidationError` | No | Input is malformed |

## Expected Token Savings
Failed task + agent explanation + user retry: ~3,000 tokens
Built-in retry resolves silently: 0 extra tokens

## Environment
- Any agent calling external APIs, databases, or slow services
- Source: direct experience, industry standard resilience pattern
