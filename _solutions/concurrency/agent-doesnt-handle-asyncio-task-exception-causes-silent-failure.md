---
layout: solution
title: "Agent doesn't handle asyncio task exceptions — causes silent failure"
category: concurrency
description: "asyncio.create_task() swallows exceptions by default. When a background LLM call raises, the task dies silently with no log line, no retry, and no user-visible error — the agent appears to hang."
tags: [asyncio, task-exception, error-handling, silent-failure, background-task, python]
---

## Symptom

The agent submits work via `asyncio.create_task()` and never hears back. No exception appears in logs. The agent either hangs waiting for a result that never arrives, or continues as if the task completed successfully with an empty result. Only a Python deprecation warning about "Task exception was never retrieved" appears at process exit — far too late to act on.

## Root Cause

`asyncio.create_task()` schedules a coroutine to run concurrently but returns a `Task` object whose exception is stored internally. If the task raises and nobody `await`s or `.result()`s the task, Python swallows the exception. The warning `Task exception was never retrieved` is emitted only when the task is garbage collected — typically at process shutdown. There is no automatic retry, no propagation to the parent coroutine, and no log entry unless you've explicitly attached a done callback.

---

## Option 1 — Done callback with `task.exception()` logging

**Attach a done callback to every `create_task` call. The callback runs when the task finishes and logs or re-raises any exception.**

```python
import asyncio
import logging
import anthropic

client = anthropic.AsyncAnthropic()
log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


def on_task_done(task: asyncio.Task) -> None:
    """Called when a background task finishes — log or handle any exception."""
    if task.cancelled():
        log.warning("Task %s was cancelled.", task.get_name())
        return
    exc = task.exception()
    if exc:
        log.error("Task %s raised: %s", task.get_name(), exc, exc_info=exc)
        # Optionally: push to a retry queue, alert, or increment a metric


def create_tracked_task(coro, name: str | None = None) -> asyncio.Task:
    """Wrapper around create_task that always attaches an exception callback."""
    task = asyncio.create_task(coro, name=name)
    task.add_done_callback(on_task_done)
    return task


async def llm_call(prompt: str) -> str:
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


async def failing_llm_call(prompt: str) -> str:
    raise ValueError(f"Simulated failure for: {prompt[:30]}")


async def main() -> None:
    # Good task — completes normally
    t1 = create_tracked_task(llm_call("What is 2+2?"), name="good-task")

    # Bad task — raises; callback will log the exception instead of swallowing it
    t2 = create_tracked_task(failing_llm_call("This will fail"), name="bad-task")

    results = await asyncio.gather(t1, t2, return_exceptions=True)
    for r in results:
        if isinstance(r, Exception):
            log.info("Gathered exception: %s", r)
        else:
            log.info("Result: %s", str(r)[:60])


asyncio.run(main())
```

**Expected Token Savings:** Catching exceptions immediately enables targeted retry of only the failed task — avoids re-running all tasks in a batch, saving up to 90% of retry tokens for single-failure scenarios.

**Environment:** Any asyncio agent using `create_task`; Python 3.8+; zero extra dependencies.

---

## Option 2 — `asyncio.gather` with `return_exceptions=True`

**Replace bare `create_task` fan-out with `asyncio.gather(..., return_exceptions=True)`. Every failure is surfaced as a value, not a silent void.**

```python
import asyncio
import anthropic

client = anthropic.AsyncAnthropic()


async def llm_call(prompt: str, idx: int) -> tuple[int, str]:
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}],
    )
    return idx, response.content[0].text


async def process_batch(prompts: list[str]) -> list[str]:
    coroutines = [llm_call(p, i) for i, p in enumerate(prompts)]

    # return_exceptions=True: exceptions become values, never silently dropped
    raw = await asyncio.gather(*coroutines, return_exceptions=True)

    results: list[str] = []
    failed: list[int] = []

    for item in raw:
        if isinstance(item, Exception):
            idx = raw.index(item)
            print(f"  Task {idx} failed: {item}")
            failed.append(idx)
        else:
            idx, text = item
            results.append(text)

    # Retry only the failed tasks
    if failed:
        print(f"  Retrying {len(failed)} failed tasks …")
        retry_coros = [llm_call(prompts[i], i) for i in failed]
        retry_raw = await asyncio.gather(*retry_coros, return_exceptions=True)
        for item in retry_raw:
            if not isinstance(item, Exception):
                _, text = item
                results.append(text)

    return results


async def main() -> None:
    prompts = [f"Summarise topic {i}" for i in range(8)]
    results = await process_batch(prompts)
    print(f"Got {len(results)} results from {len(prompts)} prompts.")


asyncio.run(main())
```

**Expected Token Savings:** Per-task exception isolation means only failed tasks are retried — for a 10-task batch with 1 failure, saves 9× the retry cost vs. re-running the whole batch.

**Environment:** Batch LLM processing pipelines; replaces `create_task` fan-outs with a single `gather` call.

---

## Option 3 — `asyncio.TaskGroup` (Python 3.11+) for structured error propagation

**`TaskGroup` automatically cancels sibling tasks and propagates exceptions to the parent scope — no callbacks or `return_exceptions` needed.**

```python
import asyncio
import anthropic

client = anthropic.AsyncAnthropic()


async def llm_call(prompt: str) -> str:
    response = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


async def process_with_task_group(prompts: list[str]) -> list[str]:
    results: list[str] = []

    try:
        async with asyncio.TaskGroup() as tg:
            tasks = [tg.create_task(llm_call(p)) for p in prompts]
        # If any task raises, TaskGroup cancels others and re-raises here
        results = [t.result() for t in tasks]
    except* anthropic.APIError as eg:
        print(f"API errors: {[str(e) for e in eg.exceptions]}")
        # Collect results from tasks that succeeded
        results = [t.result() for t in tasks if not t.cancelled() and t.exception() is None]
    except* Exception as eg:
        print(f"Unexpected errors: {eg.exceptions}")

    return results


async def main() -> None:
    prompts = [f"Define concept {i}" for i in range(5)]
    results = await process_with_task_group(prompts)
    print(f"Completed: {len(results)}/{len(prompts)}")
    for r in results:
        print(f"  {r[:60]}")


asyncio.run(main())
```

**Expected Token Savings:** Structured task groups prevent zombie tasks from consuming API quota after the parent has already failed — eliminates wasted spend on in-flight calls that would be discarded anyway.

**Environment:** Python 3.11+; best for workflows where all tasks must succeed or the operation should fail fast.

---

## Option 4 — Task registry with health monitoring

**Keep a registry of all active tasks. A watchdog coroutine polls for failed tasks and triggers recovery logic.**

```python
import asyncio
import time
import anthropic

client = anthropic.AsyncAnthropic()


class TaskRegistry:
    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task] = {}
        self._failures: list[tuple[str, Exception]] = []

    def register(self, name: str, task: asyncio.Task) -> asyncio.Task:
        self._tasks[name] = task
        task.add_done_callback(lambda t: self._on_done(name, t))
        return task

    def _on_done(self, name: str, task: asyncio.Task) -> None:
        self._tasks.pop(name, None)
        if not task.cancelled() and task.exception():
            self._failures.append((name, task.exception()))

    @property
    def active_count(self) -> int:
        return len(self._tasks)

    def pop_failures(self) -> list[tuple[str, Exception]]:
        failures, self._failures = self._failures[:], []
        return failures

    async def wait_all(self, timeout: float = 60) -> None:
        deadline = time.monotonic() + timeout
        while self._tasks:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                print(f"  Timeout: {len(self._tasks)} tasks still running")
                break
            await asyncio.sleep(0.1)


registry = TaskRegistry()


async def llm_call(prompt: str, fail: bool = False) -> str:
    if fail:
        raise ConnectionError(f"Simulated connection error for: {prompt[:20]}")
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


async def watchdog(check_interval: float = 1.0) -> None:
    """Poll for failed tasks and log them immediately."""
    while True:
        await asyncio.sleep(check_interval)
        for name, exc in registry.pop_failures():
            print(f"[WATCHDOG] Task '{name}' failed: {exc}")
            # Could enqueue for retry here


async def main() -> None:
    watchdog_task = asyncio.create_task(watchdog(), name="watchdog")

    for i in range(6):
        coro = llm_call(f"Task {i}", fail=(i == 3))   # task 3 will fail
        task = asyncio.create_task(coro, name=f"llm-{i}")
        registry.register(f"llm-{i}", task)

    await registry.wait_all(timeout=30)
    watchdog_task.cancel()
    print(f"Registry active after drain: {registry.active_count}")


asyncio.run(main())
```

**Expected Token Savings:** Immediate failure detection enables targeted retries within the same session — avoids discarding all in-flight work and starting from scratch, saving 80–100% of retry tokens for single-task failures.

**Environment:** Long-running agent daemons; pairs with alerting systems (PagerDuty, Slack) via the watchdog callback.

---

## Option 5 — Retry decorator for background tasks

**Wrap LLM coroutines with an async retry decorator. The task self-heals before the caller ever sees a failure.**

```python
import asyncio
import functools
import random
import anthropic

client = anthropic.AsyncAnthropic()


def async_retry(
    max_attempts: int = 3,
    base_delay: float = 1.0,
    exceptions: tuple = (Exception,),
):
    """Decorator: retry the coroutine on specified exceptions with exponential backoff."""
    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            for attempt in range(1, max_attempts + 1):
                try:
                    return await func(*args, **kwargs)
                except exceptions as exc:
                    if attempt == max_attempts:
                        raise
                    delay = base_delay * (2 ** (attempt - 1)) + random.uniform(0, 0.5)
                    print(f"  [{func.__name__}] attempt {attempt} failed: {exc} — retry in {delay:.1f}s")
                    await asyncio.sleep(delay)
        return wrapper
    return decorator


@async_retry(
    max_attempts=3,
    base_delay=2.0,
    exceptions=(anthropic.APIConnectionError, anthropic.RateLimitError),
)
async def resilient_llm_call(prompt: str) -> str:
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


async def main() -> None:
    prompts = [f"Explain concept {i}" for i in range(10)]
    tasks = [
        asyncio.create_task(resilient_llm_call(p), name=f"task-{i}")
        for i, p in enumerate(prompts)
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    ok = sum(1 for r in results if not isinstance(r, Exception))
    print(f"Completed: {ok}/{len(prompts)}")


asyncio.run(main())
```

**Expected Token Savings:** Self-healing tasks retry at the task level — no outer retry loop re-processes already-completed sibling tasks. For transient failures (network blips, 429s), saves 80–95% of retry overhead compared to restarting the full batch.

**Environment:** Any asyncio agent; decorator is framework-agnostic; tune `exceptions` to retry only on transient errors.

---

## Option 6 — Structured result type that captures success or failure

**Use a `Result` dataclass instead of exceptions. Tasks always complete successfully, carrying either a value or an error — never silently dying.**

```python
import asyncio
from dataclasses import dataclass
from typing import Generic, TypeVar
import anthropic

client = anthropic.AsyncAnthropic()
T = TypeVar("T")


@dataclass
class Ok(Generic[T]):
    value: T

    @property
    def is_ok(self) -> bool:
        return True


@dataclass
class Err(Generic[T]):
    error: Exception
    context: str = ""

    @property
    def is_ok(self) -> bool:
        return False


Result = Ok[T] | Err[T]


async def safe_llm_call(prompt: str, idx: int) -> Result[str]:
    """Never raises — wraps the result in Ok or Err."""
    try:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=[{"role": "user", "content": prompt}],
        )
        return Ok(response.content[0].text)
    except anthropic.RateLimitError as e:
        return Err(e, context=f"rate-limited on task {idx}")
    except anthropic.APIError as e:
        return Err(e, context=f"api error on task {idx}: {e.status_code}")
    except Exception as e:
        return Err(e, context=f"unexpected error on task {idx}")


async def main() -> None:
    prompts = [f"Summarise topic {i}" for i in range(8)]
    tasks = [
        asyncio.create_task(safe_llm_call(p, i))
        for i, p in enumerate(prompts)
    ]
    results: list[Result[str]] = await asyncio.gather(*tasks)

    successes = [r.value for r in results if r.is_ok]
    failures  = [(r.context, r.error) for r in results if not r.is_ok]

    print(f"OK: {len(successes)}, Failed: {len(failures)}")
    for ctx, err in failures:
        print(f"  FAIL [{ctx}]: {err}")
    for text in successes:
        print(f"  OK: {text[:60]}")


asyncio.run(main())
```

**Expected Token Savings:** Result-typed tasks are guaranteed to complete — no task is ever silently swallowed. Structured errors enable precise retry logic that targets only the failure mode (rate-limit vs. API error vs. network), avoiding blanket retries that waste tokens.

**Environment:** Production agents requiring exhaustive error accounting; teams comfortable with Result/Either patterns from functional programming.

---

## Comparison

| Option | Exception Visibility | Auto-retry | Siblings Cancelled on Failure | Complexity |
|--------|--------------------|-----------|-----------------------------|------------|
| 1. Done callback | Immediate log | No | No | Very Low |
| 2. `gather(return_exceptions=True)` | At gather point | Manual | No | Low |
| 3. `TaskGroup` (3.11+) | Structured propagation | No | Yes | Low |
| 4. Task registry + watchdog | Immediate log | No | No | Medium |
| 5. Retry decorator | Hidden (self-heals) | Yes | No | Low |
| 6. Result type | Always surfaced | Manual | No | Medium |

**Recommended path:** Add Option 1 (done callback) to every `create_task` call as a one-line safety net. Use Option 2 (`gather(return_exceptions=True)`) for batch fan-outs. Upgrade to Option 3 (`TaskGroup`) on Python 3.11+ for structured failure propagation in critical workflows.
