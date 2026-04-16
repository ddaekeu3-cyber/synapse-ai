---
title: "Agent Doesn't Implement Cooperative Cancellation for Long-Running Tool Calls"
description: "Agents that fire-and-forget long-running tool calls have no way to cancel them when the user abandons the request, the session times out, or a higher-priority task preempts the current one. Implement cooperative cancellation using cancellation tokens that propagate through tool chains, allowing cleanup handlers to run and resources to be released on cancellation."
date: 2026-04-16
difficulty: intermediate
category: reliability
slug: agent-doesnt-implement-cooperative-cancellation-for-long-running-tool-calls
tags: [cancellation, cooperative-cancellation, timeout, tool-calls, resource-cleanup, reliability]
symptoms:
  - "User abandons a long search but the underlying API calls keep running and consuming quota"
  - "Agent session times out but spawned subprocesses from tool calls keep running in the background"
  - "No way to interrupt a 10-minute batch tool call when a higher-priority request arrives"
  - "Cancellation of the agent task leaves database connections open and locks held"
  - "Tool calls started by a cancelled session still write to shared state after cancellation"
---

## Why This Happens

Python's asyncio tasks and threads have cancellation primitives, but tool call implementations rarely check for cancellation at yield points inside their logic. When the outer coroutine is cancelled, inner tool calls either run to completion (wasting resources) or terminate abruptly without cleanup (leaving locks, file handles, and DB transactions open). Cooperative cancellation solves this: a `CancellationToken` is created per request and passed to every tool call; each tool checks the token at safe yield points and raises `CancelledError` with cleanup on cancellation.

## Solution 1: Cancellation Token

```python
import asyncio
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional

class CancellationToken:
    """
    Cooperative cancellation primitive.
    Passed from the agent task down into every tool call.
    Tools check is_cancelled() at yield points and raise CancelledError.
    Supports cancellation callbacks for cleanup registration.
    """

    def __init__(self, deadline: Optional[float] = None):
        self._cancelled = False
        self._reason: str = ""
        self._deadline = deadline   # absolute monotonic time
        self._callbacks: List[Callable[[], None]] = []
        self._lock = asyncio.Lock()

    @property
    def is_cancelled(self) -> bool:
        if self._cancelled:
            return True
        if self._deadline and time.monotonic() > self._deadline:
            self._cancelled = True
            self._reason = "deadline_exceeded"
        return self._cancelled

    @property
    def reason(self) -> str:
        return self._reason

    async def cancel(self, reason: str = "cancelled") -> None:
        async with self._lock:
            if self._cancelled:
                return
            self._cancelled = True
            self._reason = reason
        for cb in self._callbacks:
            try:
                if asyncio.iscoroutinefunction(cb):
                    await cb()
                else:
                    cb()
            except Exception as exc:
                print(f"[cancellation] callback error: {exc}")

    def register_cleanup(self, callback: Callable) -> None:
        """Register a cleanup callback invoked on cancellation."""
        self._callbacks.append(callback)

    def check(self) -> None:
        """Raise CancelledError if cancelled. Call at yield points."""
        if self.is_cancelled:
            raise asyncio.CancelledError(f"task cancelled: {self._reason}")

    def linked(self, deadline_seconds: Optional[float] = None) -> "CancellationToken":
        """Return a child token that is cancelled when this token is cancelled."""
        deadline = None
        if deadline_seconds:
            deadline = time.monotonic() + deadline_seconds
        if self._deadline:
            deadline = min(self._deadline, deadline) if deadline else self._deadline
        child = CancellationToken(deadline=deadline)
        self.register_cleanup(lambda: asyncio.ensure_future(child.cancel("parent_cancelled")))
        return child
```

## Solution 2: Cancellable Tool Executor

```python
import asyncio
import time
from typing import Any, Callable, Coroutine, Optional

class CancellableToolExecutor:
    """
    Wraps async tool calls to respect a CancellationToken.
    Checks for cancellation before starting and after each await.
    Supports per-tool timeout that is bounded by the token's deadline.
    """

    def __init__(self, default_timeout_seconds: float = 60.0):
        self._default_timeout = default_timeout_seconds

    async def execute(
        self,
        tool_fn: Callable[..., Coroutine],
        *args,
        token: Optional[CancellationToken] = None,
        timeout_seconds: Optional[float] = None,
        tool_name: str = "unknown",
        **kwargs,
    ) -> Any:
        if token and token.is_cancelled:
            raise asyncio.CancelledError(f"[{tool_name}] cancelled before start")

        # Compute effective timeout
        effective_timeout = timeout_seconds or self._default_timeout
        if token and token._deadline:
            remaining = token._deadline - time.monotonic()
            if remaining <= 0:
                raise asyncio.CancelledError(f"[{tool_name}] deadline already passed")
            effective_timeout = min(effective_timeout, remaining)

        try:
            result = await asyncio.wait_for(
                tool_fn(*args, **kwargs),
                timeout=effective_timeout,
            )
        except asyncio.TimeoutError:
            if token:
                await token.cancel("tool_timeout")
            raise asyncio.CancelledError(f"[{tool_name}] timed out after {effective_timeout}s")
        except asyncio.CancelledError:
            raise
        except Exception:
            raise

        # Post-execution cancellation check
        if token and token.is_cancelled:
            raise asyncio.CancelledError(f"[{tool_name}] cancelled after completion")

        return result
```

## Solution 3: Cancellation-Aware Tool Base

```python
import asyncio
from typing import Any, AsyncIterator, Optional

class CancellableToolBase:
    """
    Base class for tools that need to check cancellation at yield points.
    Provides check_cancelled() and yield_point() helpers.
    Subclasses call yield_point() in loops and between expensive operations.
    """

    def __init__(self, token: Optional[CancellationToken] = None):
        self._token = token

    def check_cancelled(self) -> None:
        if self._token:
            self._token.check()

    async def yield_point(self) -> None:
        """
        Cooperative yield: gives the event loop a chance to process
        cancellation signals, then checks the token.
        """
        await asyncio.sleep(0)
        self.check_cancelled()

    async def run_with_cancellation(
        self,
        items,
        process_fn,
        check_every: int = 10,
    ) -> list:
        """
        Process an iterable with cancellation checks every N items.
        Returns partial results collected before cancellation.
        """
        results = []
        for i, item in enumerate(items):
            if i % check_every == 0:
                await self.yield_point()
            result = await process_fn(item)
            results.append(result)
        return results

    async def chunked_stream(
        self,
        source: AsyncIterator,
        chunk_size: int = 100,
    ) -> AsyncIterator:
        """Async generator that checks cancellation between chunks."""
        chunk = []
        async for item in source:
            self.check_cancelled()
            chunk.append(item)
            if len(chunk) >= chunk_size:
                yield chunk
                chunk = []
                await self.yield_point()
        if chunk:
            yield chunk
```

## Solution 4: Cascading Cancellation Scope

```python
import asyncio
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator, List, Optional

class CancellationScope:
    """
    Context manager that creates a cancellation scope with a deadline.
    Child scopes inherit parent's token and can add their own deadline.
    All tasks started within the scope are cancelled when the scope exits.
    """

    def __init__(
        self,
        token: Optional[CancellationToken] = None,
        deadline_seconds: Optional[float] = None,
    ):
        self._parent_token = token
        self._deadline_seconds = deadline_seconds
        self._token: Optional[CancellationToken] = None
        self._tasks: List[asyncio.Task] = []

    @property
    def token(self) -> CancellationToken:
        if self._token is None:
            raise RuntimeError("CancellationScope not entered")
        return self._token

    async def __aenter__(self) -> "CancellationScope":
        deadline = None
        if self._deadline_seconds:
            deadline = time.monotonic() + self._deadline_seconds
        if self._parent_token:
            self._token = self._parent_token.linked(self._deadline_seconds)
        else:
            self._token = CancellationToken(deadline=deadline)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> bool:
        # Cancel all tasks spawned within this scope
        for task in self._tasks:
            if not task.done():
                task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

        if exc_type is asyncio.CancelledError and self._token and self._token.is_cancelled:
            # Scope-level cancellation — suppress and return normally
            return True
        return False

    def spawn(self, coro) -> asyncio.Task:
        task = asyncio.ensure_future(coro)
        self._tasks.append(task)
        return task
```

## Solution 5: Cancellation Registry

```python
import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, Optional

@dataclass
class ActiveCancellation:
    token_id: str
    task_id: str
    tool_name: str
    created_at: float
    deadline: Optional[float]
    cancelled: bool = False
    cancel_reason: str = ""

class CancellationRegistry:
    """
    Tracks all active CancellationTokens per request/session.
    Allows external cancellation of in-flight tool calls by task ID.
    Used by timeout watchdogs and admin endpoints to cancel runaway tasks.
    """

    def __init__(self):
        self._tokens: Dict[str, CancellationToken] = {}
        self._meta: Dict[str, ActiveCancellation] = {}

    def register(
        self,
        token: CancellationToken,
        task_id: str,
        tool_name: str = "",
    ) -> str:
        token_id = str(uuid.uuid4())[:8]
        self._tokens[token_id] = token
        self._meta[token_id] = ActiveCancellation(
            token_id=token_id,
            task_id=task_id,
            tool_name=tool_name,
            created_at=time.time(),
            deadline=token._deadline,
        )
        return token_id

    def unregister(self, token_id: str) -> None:
        self._tokens.pop(token_id, None)
        self._meta.pop(token_id, None)

    async def cancel_task(self, task_id: str, reason: str = "admin_cancel") -> int:
        cancelled = 0
        for token_id, meta in list(self._meta.items()):
            if meta.task_id == task_id:
                token = self._tokens.get(token_id)
                if token and not token.is_cancelled:
                    await token.cancel(reason)
                    meta.cancelled = True
                    meta.cancel_reason = reason
                    cancelled += 1
        return cancelled

    async def expire_deadlines(self) -> int:
        expired = 0
        for token_id, token in list(self._tokens.items()):
            if token._deadline and time.monotonic() > token._deadline and not token.is_cancelled:
                await token.cancel("deadline_expired")
                expired += 1
        return expired

    def active_summary(self) -> dict:
        active = [m for m in self._meta.values() if not m.cancelled]
        return {
            "active_tokens": len(active),
            "by_tool": {
                m.tool_name: sum(1 for x in active if x.tool_name == m.tool_name)
                for m in active
            },
            "oldest_age_seconds": round(
                time.time() - min((m.created_at for m in active), default=time.time()), 2
            ) if active else 0,
        }
```

## Solution 6: Cancellation Health Monitor

```python
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Deque, Dict, List

@dataclass
class CancellationRecord:
    task_id: str
    tool_name: str
    reason: str
    duration_seconds: float
    timestamp: float

class CancellationHealthMonitor:
    """
    Tracks cancellation patterns to identify slow tools and timeout hotspots.
    High cancellation rate for a specific tool indicates it needs optimization.
    """

    def __init__(self, window_seconds: float = 3600.0):
        self._window = window_seconds
        self._records: Deque[CancellationRecord] = deque(maxlen=5000)
        self._by_tool: Dict[str, Deque[float]] = defaultdict(lambda: deque(maxlen=500))

    def record(
        self,
        task_id: str,
        tool_name: str,
        reason: str,
        started_at: float,
    ) -> None:
        duration = time.time() - started_at
        rec = CancellationRecord(
            task_id=task_id,
            tool_name=tool_name,
            reason=reason,
            duration_seconds=round(duration, 3),
            timestamp=time.time(),
        )
        self._records.append(rec)
        self._by_tool[tool_name].append(duration)

    def report(self) -> dict:
        cutoff = time.time() - self._window
        recent = [r for r in self._records if r.timestamp >= cutoff]
        by_tool: Dict[str, list] = defaultdict(list)
        for r in recent:
            by_tool[r.tool_name].append(r)

        tool_stats = {}
        for tool, recs in by_tool.items():
            durations = [r.duration_seconds for r in recs]
            tool_stats[tool] = {
                "cancellations": len(recs),
                "avg_duration_s": round(sum(durations) / len(durations), 3),
                "max_duration_s": round(max(durations), 3),
                "reasons": list({r.reason for r in recs}),
            }

        return {
            "total_cancellations": len(recent),
            "window_seconds": self._window,
            "by_tool": dict(sorted(
                tool_stats.items(), key=lambda x: x[1]["cancellations"], reverse=True
            )),
            "top_reason": max(
                {r.reason for r in recent},
                key=lambda reason: sum(1 for r in recent if r.reason == reason),
                default="none",
            ),
        }
```

## Comparison

| Approach | Token Propagation | Deadline Support | Cleanup Callbacks | Scope Management |
|---|---|---|---|---|
| CancellationToken | Manual (pass-down) | Yes (monotonic) | Yes | No |
| CancellableToolExecutor | Via token param | Yes (min of tool+token) | Via token | No |
| CancellableToolBase | Via self._token | Via token | Via token | No |
| CancellationScope | Automatic (spawn) | Yes (inherited) | Via token | Yes (task cleanup) |
| CancellationRegistry | External lookup | Via token | No | No |
| CancellationHealthMonitor | N/A | N/A | N/A | N/A |

**Best for production**: Create one `CancellationToken` per agent request with a deadline matching the request timeout. Pass it to every tool call via `CancellableToolExecutor.execute()`. For nested tool calls, use `token.linked()` to create child tokens with tighter deadlines. Register every token in `CancellationRegistry` so admin endpoints can cancel runaway tasks externally. Track patterns in `CancellationHealthMonitor` — tools with high cancellation rates and long durations are candidates for optimization or shorter per-tool deadlines.
