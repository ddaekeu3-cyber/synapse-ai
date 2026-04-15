---
layout: solution
title: "Agent doesn't cancel in-flight requests on timeout"
category: concurrency
description: "Agent sets an overall timeout but doesn't propagate cancellation to individual in-flight API calls and tool executions. When the timeout fires, the Python task is cancelled but the HTTP requests keep running in the background, consuming server resources and triggering unwanted side effects."
tags: [concurrency, timeout, cancellation, asyncio, httpx, resource-management, asyncio]
---

## Symptom

The agent has a 30-second overall timeout. When it fires, `asyncio.wait_for` raises `asyncio.TimeoutError` and the agent returns an error to the user. But the cancelled tool calls keep executing in the background — database writes complete, emails get sent, webhooks fire — even though the agent already reported failure. Users see "timed out" but the side effects still happen. Memory and connections leak until the orphaned tasks finish.

## Root Cause

`asyncio.wait_for` cancels the task's Python coroutine, but any `httpx` requests or `asyncio.create_task` calls that were already dispatched continue running unless they explicitly handle the cancellation signal. The `CancelledError` propagates up the coroutine stack, but background tasks that were detached with `asyncio.create_task` are not automatically cancelled — they have no parent relationship to the timed-out task.

## Fix

Propagate cancellation explicitly. Use `asyncio.TaskGroup` (Python 3.11+) or maintain a registry of child tasks and cancel them in the exception handler. For `httpx` requests, pass a `httpx.Timeout` that matches the remaining budget. For tool calls, check `task.cancelled()` before executing side-effecting operations.

---

### Option 1 — TaskGroup for structured cancellation (Python 3.11+)

```python
import anthropic
import asyncio

async_client = anthropic.AsyncAnthropic(api_key="sk-live-...")


async def fetch_weather(city: str) -> str:
    await asyncio.sleep(2)  # simulate slow external call
    return f"Weather in {city}: 22°C"


async def fetch_news(topic: str) -> str:
    await asyncio.sleep(3)  # simulate slow external call
    return f"News about {topic}: latest updates"


async def run_agent_with_task_group(user_message: str, timeout: float = 5.0) -> str:
    """
    TaskGroup propagates cancellation to ALL child tasks when any one fails
    or when the group itself is cancelled by wait_for.
    """
    weather_result: str | None = None
    news_result: str | None = None

    try:
        async with asyncio.timeout(timeout):  # Python 3.11+
            async with asyncio.TaskGroup() as tg:
                weather_task = tg.create_task(fetch_weather("London"))
                news_task = tg.create_task(fetch_news("AI"))

                # Wait for both — TaskGroup cancels all tasks if any raises
                # or if the outer timeout fires

        weather_result = weather_task.result()
        news_result = news_task.result()

    except* asyncio.TimeoutError:
        print("[Timeout] TaskGroup cancelled all child tasks")
        # At this point, fetch_weather and fetch_news are guaranteed cancelled
        weather_result = "weather unavailable (timeout)"
        news_result = "news unavailable (timeout)"
    except* Exception as eg:
        for e in eg.exceptions:
            print(f"[Error] {e}")

    context = f"Weather: {weather_result}\nNews: {news_result}"
    response = await async_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=256,
        messages=[{"role": "user", "content": f"{user_message}\n\n{context}"}],
    )
    return response.content[0].text


asyncio.run(run_agent_with_task_group("Summarize the situation", timeout=2.0))
```

**Expected Token Savings:** Zero token change; TaskGroup ensures no orphaned background tasks consuming resources — prevents memory leaks from unreferenced tasks and avoids duplicate side effects from "ghost" tool calls after timeout.
**Environment:** Python 3.11+ agents; TaskGroup is the preferred structured concurrency primitive — it makes cancellation propagation automatic and explicit at the same time.

---

### Option 2 — Manual task registry with explicit cancellation

```python
import anthropic
import asyncio
from dataclasses import dataclass, field

async_client = anthropic.AsyncAnthropic(api_key="sk-live-...")


@dataclass
class CancellationScope:
    """
    Tracks child tasks and cancels them all when the scope exits.
    Equivalent to TaskGroup for Python < 3.11.
    """
    _tasks: list[asyncio.Task] = field(default_factory=list)
    _cancelled: bool = False

    def create_task(self, coro) -> asyncio.Task:
        if self._cancelled:
            raise RuntimeError("Scope already cancelled")
        task = asyncio.create_task(coro)
        self._tasks.append(task)
        return task

    async def cancel_all(self, reason: str = "scope cancelled"):
        self._cancelled = True
        for task in self._tasks:
            if not task.done():
                task.cancel(msg=reason)
        # Wait for all cancellations to complete
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        print(f"[CancellationScope] Cancelled {len(self._tasks)} tasks: {reason}")

    async def wait_all(self) -> list:
        return await asyncio.gather(*self._tasks, return_exceptions=True)


async def run_tool(name: str, delay: float) -> str:
    """Simulated tool that respects cancellation."""
    try:
        await asyncio.sleep(delay)
        return f"{name} completed"
    except asyncio.CancelledError:
        print(f"[Tool:{name}] Cancelled cleanly")
        raise   # re-raise so the task shows as cancelled


async def run_agent_with_registry(user_message: str, timeout: float = 3.0) -> str:
    scope = CancellationScope()

    try:
        async with asyncio.timeout(timeout):
            t1 = scope.create_task(run_tool("weather", 1.5))
            t2 = scope.create_task(run_tool("news", 4.0))    # will be cancelled
            t3 = scope.create_task(run_tool("calendar", 1.0))

            results = await scope.wait_all()

    except asyncio.TimeoutError:
        await scope.cancel_all("overall timeout fired")
        results = ["timeout", "timeout", "timeout"]

    # Build context from available results
    tool_outputs = []
    for task, name in zip(scope._tasks, ["weather", "news", "calendar"]):
        if not task.cancelled() and not isinstance(task.result() if not task.cancelled() else None, BaseException):
            try:
                tool_outputs.append(task.result())
            except Exception:
                tool_outputs.append("unavailable")
        else:
            tool_outputs.append("unavailable (cancelled)")

    print(f"[Agent] Tool results: {tool_outputs}")
    response = await async_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=256,
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text


asyncio.run(run_agent_with_registry("What's happening today?", timeout=2.0))
```

**Expected Token Savings:** Zero token change; explicit registry prevents N orphaned tasks each holding open httpx connections — for 5 concurrent tool calls with 30s timeouts, this prevents up to 150 seconds of wasted httpx connection time per agent turn.
**Environment:** Python 3.10 and earlier; also useful when finer-grained control over cancellation order is needed (cancel lowest-priority tasks first).

---

### Option 3 — Per-request deadline propagation

```python
import anthropic
import asyncio
import time

async_client = anthropic.AsyncAnthropic(api_key="sk-live-...")


class DeadlineContext:
    """
    Carries a deadline timestamp through the call stack.
    Each sub-call computes its remaining budget from the shared deadline.
    """
    def __init__(self, deadline_seconds: float):
        self.deadline = time.monotonic() + deadline_seconds

    @property
    def remaining(self) -> float:
        return max(0.0, self.deadline - time.monotonic())

    @property
    def expired(self) -> bool:
        return time.monotonic() >= self.deadline

    def child_timeout(self, fraction: float = 1.0) -> float:
        """Return a timeout for a child operation as a fraction of remaining budget."""
        return self.remaining * fraction


async def fetch_with_deadline(url_label: str, ctx: DeadlineContext) -> str:
    """Tool call that respects the propagated deadline."""
    if ctx.expired:
        return f"{url_label}: skipped (deadline already exceeded)"

    timeout = ctx.child_timeout(0.8)   # use 80% of remaining budget
    print(f"[{url_label}] Starting with {timeout:.2f}s budget")
    try:
        async with asyncio.timeout(timeout):
            await asyncio.sleep(1.5)  # simulate work
            return f"{url_label}: data fetched"
    except asyncio.TimeoutError:
        return f"{url_label}: timed out (remaining was {timeout:.2f}s)"


async def run_agent_with_deadline(user_message: str, total_timeout: float = 5.0) -> str:
    ctx = DeadlineContext(total_timeout)

    # First tool call
    result_a = await fetch_with_deadline("weather_api", ctx)
    print(f"After weather: {ctx.remaining:.2f}s remaining")

    # Second tool call — uses remaining budget
    result_b = await fetch_with_deadline("news_api", ctx)
    print(f"After news: {ctx.remaining:.2f}s remaining")

    # LLM call — respects remaining deadline
    if ctx.expired:
        return "Request timed out before model call"

    async with asyncio.timeout(ctx.remaining):
        response = await async_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=256,
            messages=[{
                "role": "user",
                "content": f"{user_message}\n\n{result_a}\n{result_b}",
            }],
        )
    return response.content[0].text


asyncio.run(run_agent_with_deadline("What's the latest?", total_timeout=4.0))
```

**Expected Token Savings:** Zero token change; deadline propagation prevents the common pattern where 3 sequential tool calls each use the full timeout budget, causing actual total latency of 3× the per-call timeout — deadline context forces the total to stay within budget.
**Environment:** Multi-step agents with sequential tool calls; propagating the deadline through the call stack ensures that slow early tools reduce the budget for later tools rather than silently extending the total.

---

### Option 4 — Cancellation-safe tool execution with cleanup hooks

```python
import anthropic
import asyncio
from typing import Callable, Awaitable

async_client = anthropic.AsyncAnthropic(api_key="sk-live-...")


class CancellationSafeTool:
    """
    Wraps a tool execution with:
    1. Pre-cancellation check (skip if already cancelled)
    2. Cleanup hook that runs even on cancellation
    3. Idempotency guard to prevent re-execution after cancel
    """
    def __init__(
        self,
        name: str,
        fn: Callable[..., Awaitable],
        cleanup: Callable | None = None,
    ):
        self.name = name
        self.fn = fn
        self.cleanup = cleanup
        self._started = False
        self._completed = False

    async def execute(self, *args, **kwargs):
        if self._started:
            print(f"[{self.name}] Already started — skipping re-execution")
            return None

        self._started = True
        try:
            result = await self.fn(*args, **kwargs)
            self._completed = True
            print(f"[{self.name}] Completed successfully")
            return result
        except asyncio.CancelledError:
            print(f"[{self.name}] Cancelled — running cleanup")
            if self.cleanup:
                try:
                    if asyncio.iscoroutinefunction(self.cleanup):
                        await asyncio.shield(self.cleanup())  # cleanup survives cancellation
                    else:
                        self.cleanup()
                except Exception as e:
                    print(f"[{self.name}] Cleanup error: {e}")
            raise   # re-raise CancelledError


async def write_to_database(data: str) -> str:
    """Side-effecting tool — must not be partially executed."""
    await asyncio.sleep(2)
    return f"Written: {data}"


async def cleanup_partial_write():
    """Rollback the write if it was interrupted."""
    print("[DB] Rolling back partial write")
    await asyncio.sleep(0.1)


async def run_agent_safe_tools(user_message: str, timeout: float = 1.5) -> str:
    db_tool = CancellationSafeTool(
        name="db_write",
        fn=write_to_database,
        cleanup=cleanup_partial_write,
    )

    try:
        async with asyncio.timeout(timeout):
            result = await db_tool.execute("user_event_data")
    except asyncio.TimeoutError:
        print("[Agent] Timeout — cleanup was triggered automatically")
        result = "operation timed out"

    response = await async_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=128,
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text


asyncio.run(run_agent_safe_tools("Process this event", timeout=1.5))
```

**Expected Token Savings:** Zero token change; cleanup hooks prevent the most dangerous cancellation failure mode — partially executed side effects (half-written DB records, uncommitted transactions) that would require manual intervention to resolve.
**Environment:** Agents with transactional tool calls (database writes, payment processing, file system operations); the cleanup hook is the async equivalent of a `finally` block for cancellation scenarios.

---

### Option 5 — Timeout budget tracking with early exit

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass

async_client = anthropic.AsyncAnthropic(api_key="sk-live-...")


@dataclass
class BudgetTracker:
    """Tracks how much of the timeout budget has been spent and warns when running low."""
    total: float
    warn_threshold: float = 0.8   # warn when 80% of budget is spent

    def __post_init__(self):
        self._start = time.monotonic()
        self._checkpoints: list[tuple[str, float]] = []

    def checkpoint(self, label: str) -> float:
        elapsed = time.monotonic() - self._start
        self._checkpoints.append((label, elapsed))
        pct = elapsed / self.total
        if pct >= self.warn_threshold:
            print(f"[Budget] WARNING: {pct:.0%} spent after '{label}' ({elapsed:.2f}s / {self.total:.2f}s)")
        else:
            print(f"[Budget] {pct:.0%} spent after '{label}' ({elapsed:.2f}s / {self.total:.2f}s)")
        return self.total - elapsed

    @property
    def remaining(self) -> float:
        return max(0.0, self.total - (time.monotonic() - self._start))

    @property
    def should_abort(self) -> bool:
        """Return True if not enough budget remains for a meaningful operation."""
        return self.remaining < 0.5   # less than 500ms — skip remaining work

    def summary(self) -> str:
        lines = [f"Budget: {self.total:.1f}s total"]
        for label, elapsed in self._checkpoints:
            lines.append(f"  {elapsed:.2f}s — {label}")
        lines.append(f"  {self.total - self.remaining:.2f}s — total used")
        return "\n".join(lines)


async def tool_call(name: str, duration: float) -> str:
    await asyncio.sleep(duration)
    return f"{name} result"


async def run_agent_with_budget(user_message: str, timeout: float = 5.0) -> str:
    budget = BudgetTracker(total=timeout)

    try:
        async with asyncio.timeout(timeout):
            # Tool 1
            result_a = await tool_call("search", 1.2)
            remaining = budget.checkpoint("after search")

            if budget.should_abort:
                print("[Budget] Aborting before tool 2 — insufficient budget")
                result_b = "skipped"
            else:
                # Tool 2 gets only what's left (minus buffer for LLM call)
                result_b = await tool_call("fetch", min(1.0, remaining - 1.5))
                budget.checkpoint("after fetch")

            # LLM call with remaining budget
            if budget.should_abort:
                return "Timed out during tool calls"

            response = await async_client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=256,
                messages=[{
                    "role": "user",
                    "content": f"{user_message}\n\nContext: {result_a}, {result_b}",
                }],
            )
            budget.checkpoint("after LLM call")
            return response.content[0].text

    except asyncio.TimeoutError:
        print(f"[Agent] Hard timeout\n{budget.summary()}")
        return "Request timed out"


asyncio.run(run_agent_with_budget("Answer my question", timeout=4.0))
```

**Expected Token Savings:** Budget tracking adds zero API cost; the `should_abort` check prevents starting a new tool call with insufficient budget to complete it — avoids the worst case where a tool starts, consumes 300ms, then gets cancelled mid-flight, wasting the connection and triggering cleanup.
**Environment:** Agents with SLA requirements; the checkpoint log shows exactly where time was spent, making it easy to identify the slowest tool and optimize it.

---

### Option 6 — Graceful degradation on partial timeout

```python
import anthropic
import asyncio

async_client = anthropic.AsyncAnthropic(api_key="sk-live-...")


async def fetch_optional(name: str, coro, timeout: float) -> tuple[str, bool]:
    """
    Execute coro with a timeout.
    Returns (result, succeeded) — never raises.
    Partial results are better than no results for non-critical data.
    """
    try:
        async with asyncio.timeout(timeout):
            result = await coro
        return result, True
    except asyncio.TimeoutError:
        print(f"[Optional:{name}] Timed out after {timeout:.1f}s — using fallback")
        return f"{name}: unavailable", False
    except asyncio.CancelledError:
        print(f"[Optional:{name}] Cancelled — propagating")
        raise   # propagate cancellation upward
    except Exception as e:
        print(f"[Optional:{name}] Error: {e}")
        return f"{name}: error", False


async def run_agent_partial_results(user_message: str) -> str:
    """
    Fetch multiple data sources with individual timeouts.
    Partial data is used to answer — optional sources don't block the response.
    """
    # Critical: must succeed (longer timeout)
    weather, w_ok = await fetch_optional(
        "weather", asyncio.sleep(1, result="Sunny 22°C"), timeout=3.0
    )

    # Optional: nice to have (short timeout — don't block on it)
    news, n_ok = await fetch_optional(
        "news", asyncio.sleep(5, result="Latest news..."), timeout=1.5
    )
    stocks, s_ok = await fetch_optional(
        "stocks", asyncio.sleep(0.5, result="AAPL: $195"), timeout=2.0
    )

    # Build context from available data
    available = {
        "weather": weather if w_ok else None,
        "news": news if n_ok else None,
        "stocks": stocks if s_ok else None,
    }
    context_parts = [f"{k}: {v}" for k, v in available.items() if v is not None]
    degraded_parts = [k for k, v in available.items() if v is None]

    context = "\n".join(context_parts)
    if degraded_parts:
        context += f"\n(Note: {', '.join(degraded_parts)} data unavailable)"

    response = await async_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=256,
        messages=[{"role": "user", "content": f"{user_message}\n\n{context}"}],
    )
    return response.content[0].text


# Comparison table
# | Option | Cancellation Method | Python Version | Side-effect Safety |
# |--------|--------------------|--------------|--------------------|
# | 1 TaskGroup | Structured (automatic) | 3.11+ | All tasks cancelled together |
# | 2 Manual registry | Explicit cancel_all() | 3.8+ | Full control over order |
# | 3 Deadline ctx | Budget propagation | 3.11+ | Prevents over-budget starts |
# | 4 Cleanup hooks | asyncio.shield in cleanup | 3.8+ | Transactional safety |
# | 5 Budget tracker | should_abort check | 3.11+ | Proactive early exit |
# | 6 Graceful degrade | Per-task timeout | 3.11+ | Optional data non-blocking |

asyncio.run(run_agent_partial_results("What's happening today?"))
```

**Expected Token Savings:** Graceful degradation means the LLM call proceeds with partial context instead of being cancelled — for 3 optional data sources with 2 timing out, the agent still produces a useful answer rather than returning an error, avoiding a full retry round-trip (~1500 tokens).
**Environment:** Agents with optional enrichment data; critical tools get longer timeouts, optional tools get shorter ones — the agent answers with whatever data arrived on time.
