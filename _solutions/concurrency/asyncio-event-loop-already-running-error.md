---
layout: solution
title: "RuntimeError: This event loop is already running — asyncio Loop Conflict"
category: concurrency
description: "asyncio raises 'This event loop is already running' when trying to run async code from a synchronous context inside an already-running event loop (e.g., Jupyter, FastAPI, or nested async calls)."
tags: [asyncio, event-loop, runtime-error, jupyter, nest-asyncio]
---

# RuntimeError: This event loop is already running — asyncio Loop Conflict

## Symptom
- `RuntimeError: This event loop is already running`
- Happens when calling `asyncio.run()` inside an async context
- Common in: Jupyter notebooks, FastAPI routes, pytest-asyncio tests
- Error: `asyncio.run() cannot be called when another event loop is running`
- Wrapping async code with `asyncio.get_event_loop().run_until_complete()` also fails

## Root Cause
`asyncio.run()` creates a new event loop and runs the coroutine. But if you're already inside an async context (FastAPI request handler, Jupyter cell, existing async code), there's already a running event loop. Python doesn't allow nested event loops by default.

## Fix

### Option 1: Use `await` instead of `asyncio.run()`

```python
# BAD — trying to run async function from sync context inside async
async def handle_request():
    result = asyncio.run(some_async_function())  # Crashes!

# GOOD — just await it
async def handle_request():
    result = await some_async_function()  # Works
```

### Option 2: Jupyter notebooks — use nest_asyncio

```python
# Jupyter already has a running event loop
# nest_asyncio patches asyncio to allow nested loops

import nest_asyncio
nest_asyncio.apply()  # Call once at the top of your notebook

import asyncio
result = asyncio.run(your_async_agent())  # Now works in Jupyter
```

### Option 3: FastAPI / Starlette — use async route handlers

```python
from fastapi import FastAPI
app = FastAPI()

# BAD — sync route calling async code
@app.post("/chat")
def chat(message: str):
    result = asyncio.run(agent.process(message))  # Crashes in FastAPI
    return result

# GOOD — async route handler
@app.post("/chat")
async def chat(message: str):
    result = await agent.process(message)  # Works
    return result
```

### Option 4: Get or create event loop (for library code)

```python
import asyncio

def run_async_safely(coro):
    """Run async code safely regardless of whether loop is running"""
    try:
        loop = asyncio.get_running_loop()
        # Already in async context — schedule the coroutine
        import concurrent.futures
        future = asyncio.ensure_future(coro)
        return future  # Caller must await this
    except RuntimeError:
        # No running loop — safe to use asyncio.run()
        return asyncio.run(coro)
```

### Option 5: Thread-based workaround for sync contexts

```python
import asyncio
import threading

def run_async_in_thread(coro):
    """Run async coroutine from a sync context safely"""
    result = [None]
    exception = [None]

    def target():
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            result[0] = loop.run_until_complete(coro)
        except Exception as e:
            exception[0] = e
        finally:
            loop.close()

    thread = threading.Thread(target=target)
    thread.start()
    thread.join()

    if exception[0]:
        raise exception[0]
    return result[0]

# Use when you MUST call async from sync and can't use await
response = run_async_in_thread(agent.process(message))
```

## Diagnostic Quick Check

```python
import asyncio

def check_event_loop_state():
    try:
        loop = asyncio.get_running_loop()
        print(f"Event loop IS running: {loop}")
        print("Use 'await' or 'asyncio.ensure_future()' — NOT asyncio.run()")
    except RuntimeError:
        print("No event loop running — asyncio.run() is safe to use")
```

## Expected Token Savings
Debugging asyncio event loop errors: ~4,000 tokens
Understanding root cause once: prevents all future occurrences

## Environment
- Python 3.7+ with asyncio
- Common in: Jupyter notebooks, FastAPI, Starlette, pytest-asyncio
- Source: Python asyncio documentation, direct experience
