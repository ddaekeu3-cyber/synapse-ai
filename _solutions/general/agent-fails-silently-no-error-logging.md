---
layout: solution
title: "Agent Fails Silently — No Error Logging Makes Debugging Impossible"
category: general
description: "Agent task fails with no trace. No logs, no error message, no stack trace. The failure is invisible until user notices missing output. Root cause impossible to determine after the fact."
tags: [logging, error-handling, debugging, observability, silent-failure, monitoring]
---

# Agent Fails Silently — No Error Logging Makes Debugging Impossible

## Symptom
- Agent task appears to run but produces no output
- No error message in logs — just silence
- `except Exception: pass` swallowing all errors
- User reports "it stopped working" — no information to debug
- Works in development, fails silently in production

## Root Cause
Broad exception handlers that suppress errors, missing logging setup, or bare `try/except` blocks without logging. In async code, unhandled exceptions in tasks are silently swallowed unless explicitly logged or awaited.

## Fix

### Option 1: Never suppress exceptions without logging

```python
import logging, traceback

logger = logging.getLogger(__name__)

# WRONG — exception swallowed, no trace
try:
    result = await process_task(task)
except Exception:
    pass  # Silent failure

# WRONG — printed but not logged, lost in production
try:
    result = await process_task(task)
except Exception as e:
    print(f"Error: {e}")  # Not in log aggregator

# RIGHT — logged with full context
try:
    result = await process_task(task)
except Exception as e:
    logger.exception(  # Logs error + full stack trace
        f"Task failed: task_id={task.id} type={task.type}",
        extra={"task_id": task.id, "task_type": task.type}
    )
    raise  # Re-raise so caller knows it failed
```

### Option 2: Structured logging setup

```python
import logging, json, sys
from datetime import datetime

def setup_logging(level: str = "INFO"):
    """Configure structured JSON logging for production"""
    class JSONFormatter(logging.Formatter):
        def format(self, record):
            log_entry = {
                "timestamp": datetime.utcnow().isoformat() + "Z",
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
                "module": record.module,
                "function": record.funcName,
                "line": record.lineno,
            }
            if record.exc_info:
                log_entry["exception"] = self.formatException(record.exc_info)
            if hasattr(record, "task_id"):
                log_entry["task_id"] = record.task_id
            return json.dumps(log_entry)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JSONFormatter())

    root_logger = logging.getLogger()
    root_logger.addHandler(handler)
    root_logger.setLevel(getattr(logging, level.upper()))

setup_logging("INFO")
```

### Option 3: Catch and log unhandled asyncio task exceptions

```python
import asyncio, logging

logger = logging.getLogger(__name__)

def handle_task_exception(task: asyncio.Task):
    """Called when a task raises an unhandled exception"""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.exception(
            f"Unhandled exception in task {task.get_name()}",
            exc_info=(type(exc), exc, exc.__traceback__)
        )

async def create_monitored_task(coro, name: str = None) -> asyncio.Task:
    """Create task with error monitoring"""
    task = asyncio.create_task(coro, name=name)
    task.add_done_callback(handle_task_exception)
    return task

# Global handler for all unhandled exceptions
def setup_asyncio_exception_handler():
    loop = asyncio.get_event_loop()
    def handler(loop, context):
        exc = context.get("exception")
        if exc:
            logger.exception(
                f"Unhandled asyncio exception: {context.get('message')}",
                exc_info=(type(exc), exc, exc.__traceback__)
            )
        else:
            logger.error(f"Asyncio error: {context.get('message')}")
    loop.set_exception_handler(handler)
```

### Option 4: Decorator for automatic error logging

```python
import functools, logging, time
from typing import Callable, TypeVar

logger = logging.getLogger(__name__)
T = TypeVar("T")

def log_errors(func: Callable) -> Callable:
    """Decorator: log any exception with context before re-raising"""
    @functools.wraps(func)
    async def async_wrapper(*args, **kwargs):
        start = time.time()
        try:
            result = await func(*args, **kwargs)
            elapsed = time.time() - start
            logger.debug(f"{func.__name__} succeeded in {elapsed:.2f}s")
            return result
        except Exception as e:
            elapsed = time.time() - start
            logger.exception(
                f"{func.__name__} failed after {elapsed:.2f}s: {type(e).__name__}: {e}"
            )
            raise

    @functools.wraps(func)
    def sync_wrapper(*args, **kwargs):
        start = time.time()
        try:
            result = func(*args, **kwargs)
            return result
        except Exception as e:
            elapsed = time.time() - start
            logger.exception(
                f"{func.__name__} failed after {elapsed:.2f}s: {type(e).__name__}: {e}"
            )
            raise

    if asyncio.iscoroutinefunction(func):
        return async_wrapper
    return sync_wrapper

# Usage
@log_errors
async def process_agent_task(task: dict) -> dict:
    return await do_work(task)
```

### Option 5: Context-rich error logging

```python
import logging
from contextlib import asynccontextmanager

logger = logging.getLogger(__name__)

@asynccontextmanager
async def task_context(task_id: str, task_type: str, **extra):
    """Context manager that enriches all logs and catches errors"""
    log_context = {"task_id": task_id, "task_type": task_type, **extra}
    logger.info(f"Task started", extra=log_context)

    try:
        yield log_context
        logger.info(f"Task completed successfully", extra=log_context)
    except Exception as e:
        logger.exception(
            f"Task failed: {type(e).__name__}: {e}",
            extra={**log_context, "error_type": type(e).__name__, "error": str(e)}
        )
        raise

# Usage
async def run_task(task):
    async with task_context(task["id"], task["type"], user_id=task["user_id"]):
        result = await process(task)
        return result
```

### Option 6: Alert on error rate increase

```python
import time
from collections import deque

class ErrorRateMonitor:
    def __init__(self, window_seconds: int = 60, threshold: float = 0.1):
        self.window = window_seconds
        self.threshold = threshold
        self.events = deque()  # (timestamp, is_error)

    def record(self, is_error: bool):
        now = time.time()
        self.events.append((now, is_error))
        self._cleanup(now)

        error_rate = self._error_rate()
        if error_rate > self.threshold:
            logger.warning(
                f"High error rate: {error_rate:.0%} in last {self.window}s",
                extra={"error_rate": error_rate, "window_seconds": self.window}
            )

    def _cleanup(self, now: float):
        cutoff = now - self.window
        while self.events and self.events[0][0] < cutoff:
            self.events.popleft()

    def _error_rate(self) -> float:
        if not self.events:
            return 0.0
        errors = sum(1 for _, is_error in self.events if is_error)
        return errors / len(self.events)

monitor = ErrorRateMonitor(window_seconds=60, threshold=0.1)
```

## Logging Levels Reference

| Level | Use for |
|---|---|
| `DEBUG` | Detailed trace info (tool calls, API requests) |
| `INFO` | Task start/completion, state transitions |
| `WARNING` | Recoverable issues, rate limit approaching |
| `ERROR` | Task failed but system running |
| `CRITICAL` | System-level failure, immediate action needed |

## Expected Token Savings
Debugging silent failure without logs: ~15,000 tokens (extensive investigation)
Comprehensive logging: error diagnosed in seconds from logs

## Environment
- All production agent deployments
- Source: direct experience; the single most common cause of long debugging sessions
