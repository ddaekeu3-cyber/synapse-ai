---
title: "Agent Doesn't Implement Cooperative Cancellation with Structured Concurrency"
description: "AI agents spawn concurrent tasks without a cancellation protocol — when a user aborts a request, the agent's underlying LLM calls, tool executions, and sub-tasks keep running until they complete or time out, wasting tokens, money, and downstream resources."
problem_description: |
  When a user cancels a multi-step agent task — or when an orchestrator decides to abort a sub-agent — there's no mechanism to propagate that cancellation to in-flight work. An agent might have spawned 5 parallel tool calls, 3 model queries, and 2 sub-agents; a cancellation request reaches the top-level handler but the underlying coroutines run to completion anyway. This wastes API quota, accumulates costs, and holds database connections or file handles. Structured concurrency with cooperative cancellation ensures that cancelling a parent task tree-kills all descendant work immediately and cleanly.
category: concurrency
difficulty: advanced
tags: [cancellation, structured-concurrency, asyncio, cooperative, task-management]
---

## Solution 1: asyncio.TaskGroup with Automatic Cancellation Propagation

Use Python 3.11+ `asyncio.TaskGroup` — when any task in the group raises, all siblings are cancelled automatically, giving structured cancellation semantics for free.

```python
import asyncio
from anthropic import AsyncAnthropic
from dataclasses import dataclass


@dataclass
class ParallelResult:
    task_name: str
    result: str | None
    error: str | None = None


async def model_task(
    client: AsyncAnthropic,
    name: str,
    prompt: str,
    cancel_event: asyncio.Event,
) -> ParallelResult:
    """A model call that respects a cancellation event."""
    if cancel_event.is_set():
        return ParallelResult(name, None, "cancelled_before_start")

    try:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=128,
            messages=[{"role": "user", "content": prompt}],
        )
        if cancel_event.is_set():
            return ParallelResult(name, None, "cancelled_after_completion")
        return ParallelResult(name, response.content[0].text)
    except asyncio.CancelledError:
        print(f"[{name}] Cancelled mid-flight")
        raise  # Must re-raise for structured concurrency to work


async def run_with_taskgroup(
    client: AsyncAnthropic,
    tasks: list[tuple[str, str]],  # [(name, prompt), ...]
    timeout: float = 10.0,
) -> list[ParallelResult]:
    cancel_event = asyncio.Event()
    results: list[ParallelResult] = []

    async def run_task(name: str, prompt: str):
        result = await model_task(client, name, prompt, cancel_event)
        results.append(result)

    try:
        async with asyncio.timeout(timeout):
            async with asyncio.TaskGroup() as tg:
                for name, prompt in tasks:
                    tg.create_task(run_task(name, prompt))
    except TimeoutError:
        cancel_event.set()
        print("[TaskGroup] Timeout — all tasks cancelled")
    except* Exception as eg:
        cancel_event.set()
        for exc in eg.exceptions:
            print(f"[TaskGroup] Error: {exc}")

    return results


# Usage
async def main():
    client = AsyncAnthropic()

    task_list = [
        ("summarizer", "Summarize the benefits of REST APIs in one sentence."),
        ("classifier", "Classify this text as technical or non-technical: 'asyncio is great'."),
        ("extractor", "Extract the key concept from: 'rate limiting protects APIs from abuse'."),
    ]

    results = await run_with_taskgroup(client, task_list, timeout=15.0)
    for r in results:
        status = "OK" if r.result else f"FAILED ({r.error})"
        print(f"[{r.task_name}] {status}: {(r.result or '')[:60]}")

asyncio.run(main())
```

## Solution 2: CancellationToken Pattern for Cross-Coroutine Propagation

Implement an explicit `CancellationToken` object passed through the call tree — enabling any level of the hierarchy to check for cancellation at safe checkpoints without relying on exception propagation.

```python
import asyncio
import time
from anthropic import AsyncAnthropic
from dataclasses import dataclass, field


@dataclass
class CancellationToken:
    _cancelled: bool = field(default=False, init=False)
    _reason: str | None = field(default=None, init=False)
    _cancel_time: float | None = field(default=None, init=False)

    def cancel(self, reason: str = "user_requested"):
        self._cancelled = True
        self._reason = reason
        self._cancel_time = time.time()

    @property
    def is_cancelled(self) -> bool:
        return self._cancelled

    @property
    def reason(self) -> str | None:
        return self._reason

    def raise_if_cancelled(self):
        if self._cancelled:
            raise asyncio.CancelledError(f"Cancelled: {self._reason}")

    def child_token(self) -> "CancellationToken":
        """Returns a child token that mirrors parent cancellation."""
        child = CancellationToken()
        # Child inherits parent state — in production, link them via callback
        if self._cancelled:
            child.cancel(self._reason or "parent_cancelled")
        return child


async def tool_call_with_cancellation(
    name: str,
    token: CancellationToken,
    delay: float = 0.5,
) -> str:
    """Simulated tool call that checks cancellation at checkpoints."""
    token.raise_if_cancelled()
    await asyncio.sleep(delay / 2)

    token.raise_if_cancelled()  # Checkpoint
    await asyncio.sleep(delay / 2)

    token.raise_if_cancelled()  # Final checkpoint before returning
    return f"{name}: completed"


async def model_call_with_cancellation(
    client: AsyncAnthropic,
    prompt: str,
    token: CancellationToken,
) -> str:
    token.raise_if_cancelled()

    # Run model call with periodic cancellation check
    model_task = asyncio.create_task(
        client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=128,
            messages=[{"role": "user", "content": prompt}],
        )
    )

    # Poll for cancellation while model runs
    while not model_task.done():
        if token.is_cancelled:
            model_task.cancel()
            raise asyncio.CancelledError(f"Model call cancelled: {token.reason}")
        await asyncio.sleep(0.1)

    response = await model_task
    token.raise_if_cancelled()
    return response.content[0].text


async def multi_step_agent(
    client: AsyncAnthropic,
    user_query: str,
    token: CancellationToken,
) -> dict:
    results = {}

    # Step 1: Tool calls in parallel
    token.raise_if_cancelled()
    tool_tokens = [token.child_token() for _ in range(3)]

    try:
        tool_results = await asyncio.gather(
            tool_call_with_cancellation("web_search", tool_tokens[0], delay=0.3),
            tool_call_with_cancellation("db_query", tool_tokens[1], delay=0.5),
            tool_call_with_cancellation("cache_lookup", tool_tokens[2], delay=0.1),
            return_exceptions=True,
        )
        results["tools"] = [r for r in tool_results if not isinstance(r, Exception)]
    except asyncio.CancelledError:
        raise

    # Step 2: Model synthesis
    token.raise_if_cancelled()
    context = "\n".join(results.get("tools", []))
    results["synthesis"] = await model_call_with_cancellation(
        client,
        f"Context: {context}\n\nAnswer: {user_query}",
        token,
    )

    return results


# Usage
async def main():
    client = AsyncAnthropic()
    token = CancellationToken()

    # Schedule cancellation after 0.8 seconds
    async def cancel_after(delay: float):
        await asyncio.sleep(delay)
        token.cancel("user_pressed_stop")
        print("[main] Cancellation issued")

    cancel_task = asyncio.create_task(cancel_after(0.8))

    try:
        result = await multi_step_agent(client, "What is the weather in Tokyo?", token)
        print(f"Result: {result}")
    except asyncio.CancelledError as e:
        print(f"Agent cancelled: {e}")
    finally:
        cancel_task.cancel()

asyncio.run(main())
```

## Solution 3: Deadline-Propagating Context

Pass a deadline timestamp through the entire call tree — each function reduces its own timeout to fit within the remaining deadline budget, preventing any single subtask from consuming the full budget.

```python
import asyncio
import time
from anthropic import AsyncAnthropic
from dataclasses import dataclass


@dataclass
class DeadlineContext:
    deadline: float  # Unix timestamp

    @classmethod
    def with_timeout(cls, seconds: float) -> "DeadlineContext":
        return cls(deadline=time.monotonic() + seconds)

    @property
    def remaining(self) -> float:
        return max(0.0, self.deadline - time.monotonic())

    @property
    def expired(self) -> bool:
        return time.monotonic() >= self.deadline

    def child_context(self, max_seconds: float) -> "DeadlineContext":
        """Creates a child context with at most max_seconds remaining."""
        child_deadline = min(self.deadline, time.monotonic() + max_seconds)
        return DeadlineContext(deadline=child_deadline)

    def raise_if_expired(self):
        if self.expired:
            raise asyncio.TimeoutError(f"Deadline exceeded (remaining was 0s)")


async def tool_with_deadline(
    name: str,
    ctx: DeadlineContext,
    simulated_duration: float = 1.0,
) -> str:
    ctx.raise_if_expired()
    timeout = min(ctx.remaining, simulated_duration * 1.5)

    try:
        async with asyncio.timeout(timeout):
            await asyncio.sleep(simulated_duration)
            return f"{name}: success"
    except asyncio.TimeoutError:
        raise asyncio.TimeoutError(f"{name}: deadline exceeded")


async def model_with_deadline(
    client: AsyncAnthropic,
    prompt: str,
    ctx: DeadlineContext,
    max_tokens: int = 128,
) -> str:
    ctx.raise_if_expired()
    remaining = ctx.remaining

    if remaining < 1.0:
        raise asyncio.TimeoutError("Insufficient time for model call")

    try:
        async with asyncio.timeout(remaining):
            response = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
            return response.content[0].text
    except asyncio.TimeoutError:
        raise asyncio.TimeoutError(f"Model call exceeded deadline (had {remaining:.1f}s)")


async def deadline_aware_pipeline(
    client: AsyncAnthropic,
    query: str,
    total_deadline_seconds: float = 10.0,
) -> dict:
    root_ctx = DeadlineContext.with_timeout(total_deadline_seconds)
    results = {}

    # Phase 1: parallel tools — give each up to 3s but within overall budget
    tool_ctx = root_ctx.child_context(max_seconds=3.0)
    print(f"Phase 1 budget: {tool_ctx.remaining:.1f}s")

    tool_results = await asyncio.gather(
        tool_with_deadline("search", tool_ctx, 0.5),
        tool_with_deadline("db", tool_ctx, 0.3),
        return_exceptions=True,
    )
    results["tools"] = [r for r in tool_results if isinstance(r, str)]
    errors = [r for r in tool_results if isinstance(r, Exception)]
    if errors:
        print(f"Phase 1 errors: {[str(e) for e in errors]}")

    # Phase 2: model synthesis — remaining budget after tools
    model_ctx = root_ctx.child_context(max_seconds=6.0)
    print(f"Phase 2 budget: {model_ctx.remaining:.1f}s")

    context = "; ".join(results["tools"])
    results["answer"] = await model_with_deadline(
        client,
        f"Based on: {context}\n\n{query}",
        model_ctx,
    )

    print(f"Completed with {root_ctx.remaining:.1f}s remaining")
    return results


# Usage
async def main():
    client = AsyncAnthropic()
    try:
        result = await deadline_aware_pipeline(
            client,
            "Summarize the findings.",
            total_deadline_seconds=15.0,
        )
        print(f"Answer: {result.get('answer', '')[:100]}")
    except asyncio.TimeoutError as e:
        print(f"Pipeline failed: {e}")

asyncio.run(main())
```

## Solution 4: Scope-Based Resource Cleanup on Cancellation

Use async context managers to guarantee cleanup (releasing locks, closing connections, writing partial results) even when a task is cancelled mid-execution.

```python
import asyncio
import contextlib
from anthropic import AsyncAnthropic
from dataclasses import dataclass, field
from typing import AsyncIterator


@dataclass
class AgentResources:
    """Tracks resources acquired during agent execution."""
    acquired_locks: list[str] = field(default_factory=list)
    open_connections: list[str] = field(default_factory=list)
    partial_results: list[str] = field(default_factory=list)
    cleanup_actions: list[str] = field(default_factory=list)

    def record_partial(self, result: str):
        self.partial_results.append(result)

    def report(self) -> dict:
        return {
            "partial_results": self.partial_results,
            "cleanup_actions": self.cleanup_actions,
        }


@contextlib.asynccontextmanager
async def managed_lock(name: str, resources: AgentResources) -> AsyncIterator[None]:
    resources.acquired_locks.append(name)
    print(f"[lock] Acquired: {name}")
    try:
        yield
    finally:
        resources.acquired_locks.remove(name)
        resources.cleanup_actions.append(f"released_lock:{name}")
        print(f"[lock] Released: {name} (cleanup)")


@contextlib.asynccontextmanager
async def managed_connection(name: str, resources: AgentResources) -> AsyncIterator[None]:
    resources.open_connections.append(name)
    print(f"[conn] Opened: {name}")
    try:
        yield
    finally:
        if name in resources.open_connections:
            resources.open_connections.remove(name)
        resources.cleanup_actions.append(f"closed_conn:{name}")
        print(f"[conn] Closed: {name} (cleanup)")


async def cancellable_pipeline(
    client: AsyncAnthropic,
    query: str,
    resources: AgentResources,
    cancel_token: asyncio.Event,
) -> str:
    async with managed_connection("db_pool", resources):
        async with managed_lock("user_session_lock", resources):

            # Step 1
            if cancel_token.is_set():
                raise asyncio.CancelledError("cancelled_at_step1")

            response1 = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=64,
                messages=[{"role": "user", "content": f"Step 1: classify: {query}"}],
            )
            resources.record_partial(response1.content[0].text)
            print(f"Step 1 complete: {response1.content[0].text[:40]}")

            # Step 2 — check cancellation at checkpoint
            if cancel_token.is_set():
                raise asyncio.CancelledError("cancelled_between_steps")

            async with managed_connection("cache_conn", resources):
                response2 = await client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=128,
                    messages=[{"role": "user", "content": f"Step 2: elaborate: {query}"}],
                )
                resources.record_partial(response2.content[0].text)
                print(f"Step 2 complete: {response2.content[0].text[:40]}")

            return response2.content[0].text


# Usage
async def main():
    client = AsyncAnthropic()
    resources = AgentResources()
    cancel_token = asyncio.Event()

    # Set cancellation after 0.5 seconds
    async def set_cancel():
        await asyncio.sleep(0.5)
        cancel_token.set()
        print("[main] Cancel signal sent")

    cancel_task = asyncio.create_task(set_cancel())

    try:
        result = await cancellable_pipeline(
            client, "Explain distributed tracing.", resources, cancel_token
        )
        print(f"Result: {result[:100]}")
    except asyncio.CancelledError as e:
        print(f"Pipeline cancelled: {e}")
    finally:
        cancel_task.cancel()

    print(f"\nResource cleanup report: {resources.report()}")
    print(f"Open connections remaining: {resources.open_connections}")
    print(f"Held locks remaining: {resources.acquired_locks}")

asyncio.run(main())
```

## Solution 5: Cancellation-Aware Streaming with Partial Result Saving

Cancel a streaming response mid-flight but save whatever tokens have arrived — enabling graceful partial results rather than total loss when users abort long generations.

```python
import asyncio
from anthropic import AsyncAnthropic
from dataclasses import dataclass, field


@dataclass
class StreamResult:
    text: str
    complete: bool
    tokens_received: int
    cancelled: bool = False
    cancel_reason: str | None = None


async def cancellable_stream(
    client: AsyncAnthropic,
    prompt: str,
    cancel_event: asyncio.Event,
    max_tokens: int = 512,
    system: str = "Be thorough and detailed.",
) -> StreamResult:
    """Stream a response, stopping cleanly if cancel_event fires."""
    tokens: list[str] = []
    cancelled = False
    cancel_reason = None

    try:
        async with client.messages.stream(
            model="claude-haiku-4-5-20251001",
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            async for token in stream.text_stream:
                if cancel_event.is_set():
                    cancelled = True
                    cancel_reason = "user_cancelled"
                    print(f"[stream] Cancelled after {len(tokens)} tokens")
                    break
                tokens.append(token)

    except asyncio.CancelledError:
        cancelled = True
        cancel_reason = "task_cancelled"

    text = "".join(tokens)
    if cancelled and text:
        text += "\n\n[Response truncated — generation cancelled]"

    return StreamResult(
        text=text,
        complete=not cancelled,
        tokens_received=len(tokens),
        cancelled=cancelled,
        cancel_reason=cancel_reason,
    )


async def stream_with_user_abort(
    client: AsyncAnthropic,
    prompt: str,
    abort_after_seconds: float | None = None,
) -> StreamResult:
    cancel_event = asyncio.Event()

    async def auto_cancel():
        if abort_after_seconds:
            await asyncio.sleep(abort_after_seconds)
            cancel_event.set()
            print(f"[auto-cancel] Cancelling after {abort_after_seconds}s")

    cancel_task = asyncio.create_task(auto_cancel())

    try:
        result = await cancellable_stream(client, prompt, cancel_event)
    finally:
        cancel_task.cancel()

    return result


# Usage
async def main():
    client = AsyncAnthropic()

    # Full completion
    print("=== Full stream ===")
    result = await stream_with_user_abort(
        client,
        "What is TCP/IP? Explain in detail.",
    )
    print(f"Complete: {result.complete}, tokens: {result.tokens_received}")
    print(f"Text: {result.text[:150]}\n")

    # Cancelled mid-stream
    print("=== Cancelled stream ===")
    result = await stream_with_user_abort(
        client,
        "Write a very detailed essay about the history of the internet.",
        abort_after_seconds=1.0,
    )
    print(f"Complete: {result.complete}, tokens: {result.tokens_received}, reason: {result.cancel_reason}")
    print(f"Partial text: {result.text[:150]}")

asyncio.run(main())
```

## Solution 6: Nursery Pattern — Supervised Task Trees with Cancellation Budget

Implement a nursery that tracks all child tasks, enforces a cancellation budget (max time to wait for cleanup), and guarantees all resources are released before returning.

```python
import asyncio
import time
from anthropic import AsyncAnthropic
from dataclasses import dataclass, field
from typing import Callable, Coroutine, Any


@dataclass
class NurseryStats:
    started: int = 0
    completed: int = 0
    cancelled: int = 0
    failed: int = 0
    cleanup_time_ms: float = 0.0


class TaskNursery:
    """
    Supervised task scope: all child tasks are cancelled and awaited
    before the nursery exits, regardless of how it exits.
    """

    def __init__(self, cleanup_budget_seconds: float = 5.0):
        self._tasks: list[asyncio.Task] = []
        self._cleanup_budget = cleanup_budget_seconds
        self._cancel_event = asyncio.Event()
        self.stats = NurseryStats()

    def spawn(
        self,
        coro: Coroutine,
        name: str | None = None,
    ) -> asyncio.Task:
        task = asyncio.create_task(coro, name=name)
        self._tasks.append(task)
        self.stats.started += 1
        return task

    def cancel_all(self, reason: str = "nursery_cancelled"):
        self._cancel_event.set()
        for task in self._tasks:
            if not task.done():
                task.cancel(msg=reason)

    async def __aenter__(self) -> "TaskNursery":
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> bool:
        if exc_type is not None:
            # Exception in the nursery body — cancel all children
            self.cancel_all("nursery_body_exception")

        cleanup_start = time.monotonic()
        try:
            async with asyncio.timeout(self._cleanup_budget):
                results = await asyncio.gather(*self._tasks, return_exceptions=True)
                for r in results:
                    if isinstance(r, asyncio.CancelledError):
                        self.stats.cancelled += 1
                    elif isinstance(r, Exception):
                        self.stats.failed += 1
                    else:
                        self.stats.completed += 1
        except asyncio.TimeoutError:
            print(f"[nursery] Cleanup budget exceeded — force-cancelling stragglers")
            for task in self._tasks:
                if not task.done():
                    task.cancel()
        finally:
            self.stats.cleanup_time_ms = (time.monotonic() - cleanup_start) * 1000

        return False  # Don't suppress exceptions


async def model_subtask(
    client: AsyncAnthropic,
    name: str,
    prompt: str,
    delay: float = 0.0,
) -> str:
    if delay > 0:
        await asyncio.sleep(delay)
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        messages=[{"role": "user", "content": prompt}],
    )
    return f"{name}: {response.content[0].text[:60]}"


# Usage
async def main():
    client = AsyncAnthropic()

    # Normal completion
    print("=== Normal nursery run ===")
    async with TaskNursery(cleanup_budget_seconds=10.0) as nursery:
        t1 = nursery.spawn(model_subtask(client, "task1", "What is REST?"))
        t2 = nursery.spawn(model_subtask(client, "task2", "What is GraphQL?"))
        t3 = nursery.spawn(model_subtask(client, "task3", "What is gRPC?"))

    results = []
    for t in [t1, t2, t3]:
        if not t.cancelled() and not t.exception():
            results.append(t.result())

    for r in results:
        print(f"  {r}")
    print(f"Stats: {nursery.stats}")

    # Cancelled nursery — all children get cleanup budget
    print("\n=== Cancelled nursery ===")
    async with TaskNursery(cleanup_budget_seconds=3.0) as nursery:
        nursery.spawn(model_subtask(client, "bg1", "Explain caching.", delay=0.0))
        nursery.spawn(model_subtask(client, "bg2", "Explain indexing.", delay=0.0))
        await asyncio.sleep(0.1)
        nursery.cancel_all("user_cancelled")

    print(f"Stats after cancellation: {nursery.stats}")

asyncio.run(main())
```

## Comparison

| Approach | Propagation | Cleanup | Partial Results | Complexity | Best For |
|---|---|---|---|---|---|
| TaskGroup (native) | Automatic | On exception | No | Very Low | Python 3.11+ parallel tasks |
| CancellationToken | Manual checkpoints | Manual | Yes | Low | Cross-layer cancellation signals |
| Deadline Context | Time-based | Timeout | No | Low | Latency-budget enforcement |
| Scope-Based Cleanup | via finally | Guaranteed | Yes | Medium | Resource-holding pipelines |
| Cancellable Stream | Per-token checkpoint | Automatic | Yes | Low | Long streaming generations |
| Nursery Pattern | Automatic on exit | Budgeted | Yes | Medium | Complex supervised task trees |
