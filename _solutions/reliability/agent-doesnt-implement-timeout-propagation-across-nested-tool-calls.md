---
title: "Agent Doesn't Implement Timeout Propagation Across Nested Tool Calls"
description: "Agents that set a timeout on the top-level tool call but not on nested sub-calls allow inner calls to run past the outer deadline, causing the outer timeout to fire while inner operations are still in progress — leaving dangling async tasks, unreleased connections, and confusing partial state. Implement deadline propagation that passes a shared deadline through every nested call so all sub-operations cancel as soon as the outer budget is exhausted."
date: 2026-04-16
difficulty: advanced
category: reliability
slug: agent-doesnt-implement-timeout-propagation-across-nested-tool-calls
tags: [timeout-propagation, deadline, nested-tool-calls, cancellation, async, context-deadline]
symptoms:
  - "Outer tool call times out but inner HTTP requests or DB queries continue running"
  - "Connection pool is exhausted by dangling queries from timed-out tool calls"
  - "Nested tool calls have hardcoded timeouts unrelated to the remaining outer budget"
  - "Cancellation of a parent call does not cancel in-flight child operations"
  - "No way to determine how much time budget remains when a nested call starts"
---

## Why This Happens

Each tool call sets its own independent timeout: the outer call has 10 s, the inner HTTP call has 8 s, the inner DB call has 5 s. When the outer call is cancelled at 10 s, the inner calls have their own timers and continue running until they expire independently. The fix is a deadline — an absolute point in time shared across all nested operations. Each nested call computes its remaining budget as `deadline - now` and uses that as its timeout, so all nested work naturally expires together when the outer deadline passes.

## Solution 1: Deadline Context

```python
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class DeadlineContext:
    """
    Carries an absolute deadline through nested tool calls.
    All sub-operations derive their timeout from the remaining budget.
    """
    deadline: float                     # absolute time.time() value
    call_id: str = ""
    tool_chain: list = field(default_factory=list)   # names of calls on the stack

    @staticmethod
    def with_budget(budget_seconds: float, call_id: str = "") -> "DeadlineContext":
        return DeadlineContext(
            deadline=time.time() + budget_seconds,
            call_id=call_id,
        )

    def remaining_seconds(self) -> float:
        return max(0.0, self.deadline - time.time())

    def is_expired(self) -> bool:
        return time.time() >= self.deadline

    def child(self, tool_name: str) -> "DeadlineContext":
        return DeadlineContext(
            deadline=self.deadline,
            call_id=self.call_id,
            tool_chain=self.tool_chain + [tool_name],
        )

    def remaining_ms(self) -> float:
        return round(self.remaining_seconds() * 1000, 2)
```

## Solution 2: Deadline-Aware Executor

```python
import asyncio
import time
from typing import Any, Callable


class DeadlineAwareExecutor:
    """
    Executes an async function with a timeout derived from the deadline context.
    Raises DeadlineExceededError if no budget remains before the call starts.
    """

    async def run(
        self,
        fn: Callable,
        ctx: DeadlineContext,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        remaining = ctx.remaining_seconds()
        if remaining <= 0:
            raise DeadlineExceededError(ctx, "no budget remaining before call")
        try:
            return await asyncio.wait_for(fn(*args, **kwargs), timeout=remaining)
        except asyncio.TimeoutError:
            raise DeadlineExceededError(ctx, f"exceeded after {ctx.remaining_ms()}ms remaining")

    async def run_with_margin(
        self,
        fn: Callable,
        ctx: DeadlineContext,
        safety_margin_seconds: float = 0.1,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Like run() but reserves a safety margin for cleanup."""
        remaining = ctx.remaining_seconds() - safety_margin_seconds
        if remaining <= 0:
            raise DeadlineExceededError(ctx, "insufficient budget after safety margin")
        try:
            return await asyncio.wait_for(fn(*args, **kwargs), timeout=remaining)
        except asyncio.TimeoutError:
            raise DeadlineExceededError(ctx, "deadline exceeded (with margin)")


class DeadlineExceededError(Exception):
    def __init__(self, ctx: DeadlineContext, reason: str):
        chain = " -> ".join(ctx.tool_chain) if ctx.tool_chain else "(root)"
        super().__init__(f"deadline exceeded [{chain}]: {reason}")
        self.ctx = ctx
        self.reason = reason
```

## Solution 3: Nested Tool Call Dispatcher with Deadline

```python
import time
from typing import Any, Callable, Dict, List, Optional


class DeadlinePropagatingDispatcher:
    """
    Dispatches nested tool calls with a child deadline context.
    Tracks the call chain and checks budget before each dispatch.
    """

    def __init__(self, executor: DeadlineAwareExecutor):
        self._executor = executor
        self._call_log: List[dict] = []

    async def dispatch(
        self,
        tool_name: str,
        fn: Callable,
        parent_ctx: DeadlineContext,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        child_ctx = parent_ctx.child(tool_name)

        if child_ctx.is_expired():
            raise DeadlineExceededError(child_ctx, "deadline already expired before dispatch")

        start = time.time()
        try:
            result = await self._executor.run(fn, child_ctx, *args, **kwargs)
            self._call_log.append({
                "tool": tool_name,
                "chain": child_ctx.tool_chain,
                "duration_ms": round((time.time() - start) * 1000, 2),
                "success": True,
                "remaining_ms_after": child_ctx.remaining_ms(),
            })
            return result
        except DeadlineExceededError:
            self._call_log.append({
                "tool": tool_name,
                "chain": child_ctx.tool_chain,
                "duration_ms": round((time.time() - start) * 1000, 2),
                "success": False,
                "error": "deadline_exceeded",
            })
            raise

    def call_log(self) -> List[dict]:
        return list(self._call_log)
```

## Solution 4: Deadline Budget Allocator

```python
from typing import Dict, List, Optional


class DeadlineBudgetAllocator:
    """
    Allocates deadline budgets across parallel sub-calls.
    When multiple tools run in parallel, each gets the full
    remaining budget (they share the deadline, not divide it).
    For sequential calls, reserves a minimum per step.
    """

    def __init__(self, min_per_step_seconds: float = 0.5):
        self._min_per_step = min_per_step_seconds

    def allocate_sequential(
        self,
        parent_ctx: DeadlineContext,
        tool_names: List[str],
    ) -> List[DeadlineContext]:
        """
        Returns one DeadlineContext per tool, each sharing the same
        absolute deadline. Each step must complete within whatever
        time remains when it starts.
        """
        return [parent_ctx.child(name) for name in tool_names]

    def can_fit_steps(
        self,
        parent_ctx: DeadlineContext,
        step_count: int,
    ) -> bool:
        return parent_ctx.remaining_seconds() >= step_count * self._min_per_step

    def allocate_parallel(
        self,
        parent_ctx: DeadlineContext,
        tool_names: List[str],
    ) -> List[DeadlineContext]:
        """All parallel calls share the same deadline — first to expire wins."""
        return [parent_ctx.child(name) for name in tool_names]
```

## Solution 5: Deadline Propagation Monitor

```python
import time
from typing import List


class DeadlinePropagationMonitor:
    """
    Tracks deadline budget consumption and expiry events.
    Surfaces patterns of near-deadline completions that indicate
    budget is too tight.
    """

    def __init__(self):
        self._events: List[dict] = []

    def record_completion(
        self,
        tool_name: str,
        call_id: str,
        remaining_ms: float,
        success: bool,
    ) -> None:
        self._events.append({
            "ts": time.time(),
            "tool": tool_name,
            "call_id": call_id,
            "remaining_ms": remaining_ms,
            "success": success,
            "near_deadline": remaining_ms < 200,
        })

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [e for e in self._events if e["ts"] >= cutoff]
        if not recent:
            return {"window_seconds": window_seconds, "calls": 0}
        expired = [e for e in recent if not e["success"]]
        near = [e for e in recent if e["near_deadline"] and e["success"]]
        return {
            "window_seconds": window_seconds,
            "calls": len(recent),
            "deadline_exceeded": len(expired),
            "near_deadline_completions": len(near),
            "exceeded_rate": round(len(expired) / len(recent), 4),
        }
```

## Solution 6: Deadline Dashboard

```python
import time


class DeadlinePropagationDashboard:
    """
    Combines dispatcher call log and monitor summary into
    a single snapshot for diagnosing deadline violations.
    """

    def __init__(
        self,
        dispatcher: DeadlinePropagatingDispatcher,
        monitor: DeadlinePropagationMonitor,
    ):
        self._dispatcher = dispatcher
        self._monitor = monitor

    def render(self) -> dict:
        call_log = self._dispatcher.call_log()
        failed_calls = [c for c in call_log if not c["success"]]
        slowest = sorted(call_log, key=lambda c: c.get("duration_ms", 0), reverse=True)[:5]
        return {
            "generated_at": time.time(),
            "total_calls": len(call_log),
            "failed_calls": len(failed_calls),
            "slowest_calls": slowest,
            "monitor": self._monitor.summary(window_seconds=3600.0),
        }
```

## Comparison

| Approach | Shared Deadline | Budget Check | Call Chain Tracking | Parallel Allocation | Monitoring |
|---|---|---|---|---|---|
| DeadlineContext | Yes (absolute) | Yes | Yes (chain list) | No | No |
| DeadlineAwareExecutor | Via context | Yes | No | No | No |
| DeadlinePropagatingDispatcher | Via context | Via executor | Yes | No | No |
| DeadlineBudgetAllocator | Via context | Yes (fit check) | No | Yes | No |
| DeadlinePropagationMonitor | No | No | No | No | Yes |

**Best for production**: Use an absolute deadline (`time.time() + budget`) rather than a relative timeout passed through each call — relative timeouts accumulate rounding errors and do not account for scheduling delays between calls. Set a safety margin of 100–200 ms via `run_with_margin()` so cleanup code (connection release, span finalization) can run after the tool call times out. Monitor `near_deadline_completions`: a high rate means budgets are too tight and occasional load spikes will tip calls into deadline violations — increase the outer budget or optimize the slowest step.
