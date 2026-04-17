---
title: "Agent Doesn't Implement Async Task Result Polling with Timeout"
description: "Agents that submit tasks to async APIs — code execution sandboxes, long-running data pipelines, or external processing queues — and then block indefinitely waiting for results will stall the entire agent when a task hangs. Implement async task result polling with configurable intervals, total timeout enforcement, partial result delivery, and cancellation of tasks that exceed their deadline."
date: 2026-04-16
difficulty: intermediate
category: reliability
slug: agent-doesnt-implement-async-task-result-polling-with-timeout
tags: [async-polling, task-timeout, deadline-enforcement, partial-results, task-cancellation, long-running-tasks]
symptoms:
  - "Agent hangs indefinitely when a code execution sandbox does not return a result"
  - "No timeout on long-running tool calls — a stuck tool blocks all subsequent turns"
  - "Polling interval is fixed at 1 second regardless of expected task duration"
  - "No mechanism to cancel a submitted task when the user session ends"
  - "Partial results from async tasks not delivered — agent waits for full completion only"
---

## Why This Happens

Async task APIs return a task ID immediately and require polling to retrieve results. Agents that call `await api.wait_for_result(task_id)` without a timeout boundary rely on the external system to bound execution — a dangerous assumption. Long-running tasks may run forever if the external system has a bug, leaving the agent in an indefinite wait state. Proper polling requires a deadline computed at submission time, an adaptive interval that starts short and backs off, and a cancellation path that notifies the external API when the deadline is exceeded so resources are not wasted on abandoned tasks.

## Solution 1: Async Task Handle

```python
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class AsyncTaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


@dataclass
class AsyncTaskHandle:
    task_id: str
    tool_name: str
    submitted_at: float = field(default_factory=time.time)
    deadline: float = 0.0           # absolute time.time() deadline
    status: AsyncTaskStatus = AsyncTaskStatus.PENDING
    result: Any = None
    error: str = ""
    poll_count: int = 0
    last_polled_at: float = 0.0
    cancel_fn: Optional[callable] = None  # async fn to cancel the task

    def is_expired(self) -> bool:
        return self.deadline > 0 and time.time() > self.deadline

    def time_remaining_s(self) -> float:
        if self.deadline <= 0:
            return float("inf")
        return max(0.0, self.deadline - time.time())

    def elapsed_s(self) -> float:
        return time.time() - self.submitted_at
```

## Solution 2: Adaptive Polling Interval Calculator

```python
import math


class AdaptivePollingIntervalCalculator:
    """
    Computes the next polling interval using exponential backoff
    capped at a maximum interval. Starts fast for quick tasks,
    backs off for slow ones to reduce API call overhead.
    """

    def __init__(
        self,
        initial_interval_s: float = 0.5,
        max_interval_s: float = 10.0,
        backoff_factor: float = 1.5,
    ):
        self._initial = initial_interval_s
        self._max = max_interval_s
        self._factor = backoff_factor

    def interval_for_attempt(self, poll_count: int) -> float:
        interval = self._initial * (self._factor ** poll_count)
        return round(min(interval, self._max), 3)

    def total_wait_by_poll(self, n_polls: int) -> float:
        """Estimate total wait time for n polling attempts."""
        return sum(self.interval_for_attempt(i) for i in range(n_polls))
```

## Solution 3: Async Task Poller

```python
import asyncio
import time
from typing import Any, Callable, Optional


class AsyncTaskPoller:
    """
    Polls an async task API until the task completes, fails, or the
    deadline is exceeded. Supports cancellation and partial result delivery.
    """

    def __init__(
        self,
        interval_calculator: AdaptivePollingIntervalCalculator,
        poll_fn: Callable,         # async (task_id) -> (status, result, error)
        cancel_fn: Optional[Callable] = None,   # async (task_id) -> None
    ):
        self._poll_fn = poll_fn
        self._cancel_fn = cancel_fn
        self._interval_calc = interval_calculator
        self._total_polls = 0
        self._timeouts = 0

    async def poll_until_done(
        self,
        handle: AsyncTaskHandle,
        on_progress: Optional[Callable] = None,  # async (handle) -> None
    ) -> AsyncTaskHandle:
        """
        Polls the task until completion or deadline.
        Returns the updated handle with final status and result.
        """
        while True:
            if handle.is_expired():
                handle.status = AsyncTaskStatus.TIMED_OUT
                handle.error = f"task timed out after {handle.elapsed_s():.1f}s"
                self._timeouts += 1
                if self._cancel_fn:
                    try:
                        await self._cancel_fn(handle.task_id)
                    except Exception:
                        pass
                return handle

            interval = self._interval_calc.interval_for_attempt(handle.poll_count)

            # Don't wait longer than the remaining deadline allows
            remaining = handle.time_remaining_s()
            sleep_s = min(interval, remaining)
            if sleep_s > 0:
                await asyncio.sleep(sleep_s)

            try:
                status_str, result, error = await self._poll_fn(handle.task_id)
                handle.poll_count += 1
                handle.last_polled_at = time.time()
                self._total_polls += 1

                if status_str == "completed":
                    handle.status = AsyncTaskStatus.COMPLETED
                    handle.result = result
                    return handle
                elif status_str == "failed":
                    handle.status = AsyncTaskStatus.FAILED
                    handle.error = error or "task failed without error detail"
                    return handle
                elif status_str == "running":
                    handle.status = AsyncTaskStatus.RUNNING
                    if on_progress:
                        try:
                            await on_progress(handle)
                        except Exception:
                            pass
                # else "pending" — keep polling

            except Exception as exc:
                handle.poll_count += 1
                # Transient poll error — keep trying until deadline
                if handle.is_expired():
                    handle.status = AsyncTaskStatus.TIMED_OUT
                    handle.error = str(exc)[:200]
                    return handle

    def stats(self) -> dict:
        return {
            "total_polls": self._total_polls,
            "timeouts": self._timeouts,
        }
```

## Solution 4: Task Submission Manager

```python
import asyncio
import time
from typing import Any, Callable, Dict, List, Optional


class AsyncTaskSubmissionManager:
    """
    Manages task submission, tracking, and deadline enforcement.
    Provides a single submit-and-wait interface that handles the
    full async lifecycle.
    """

    def __init__(
        self,
        poller: AsyncTaskPoller,
        submit_fn: Callable,    # async (tool_name, args) -> task_id
        default_timeout_s: float = 60.0,
    ):
        self._poller = poller
        self._submit_fn = submit_fn
        self._default_timeout = default_timeout_s
        self._active: Dict[str, AsyncTaskHandle] = {}
        self._completed: List[AsyncTaskHandle] = []

    async def submit_and_wait(
        self,
        tool_name: str,
        args: dict,
        timeout_s: Optional[float] = None,
        on_progress: Optional[Callable] = None,
    ) -> AsyncTaskHandle:
        timeout = timeout_s or self._default_timeout
        task_id = await self._submit_fn(tool_name, args)
        handle = AsyncTaskHandle(
            task_id=task_id,
            tool_name=tool_name,
            deadline=time.time() + timeout,
        )
        self._active[task_id] = handle

        try:
            result = await self._poller.poll_until_done(handle, on_progress)
        finally:
            self._active.pop(task_id, None)
            self._completed.append(result)
            if len(self._completed) > 10000:
                self._completed = self._completed[-5000:]

        return result

    async def cancel_all_active(self) -> int:
        count = 0
        for handle in list(self._active.values()):
            handle.status = AsyncTaskStatus.CANCELLED
            count += 1
        self._active.clear()
        return count

    def stats(self) -> dict:
        completed = [h for h in self._completed]
        timeouts = sum(1 for h in completed if h.status == AsyncTaskStatus.TIMED_OUT)
        failures = sum(1 for h in completed if h.status == AsyncTaskStatus.FAILED)
        return {
            "active_tasks": len(self._active),
            "completed_tasks": len(completed),
            "timeouts": timeouts,
            "failures": failures,
            "timeout_rate": round(timeouts / max(len(completed), 1), 4),
        }
```

## Solution 5: Timeout Policy Registry

```python
from dataclasses import dataclass
from typing import Dict, Optional


@dataclass
class ToolTimeoutPolicy:
    tool_name: str
    timeout_s: float
    initial_poll_interval_s: float = 0.5
    max_poll_interval_s: float = 10.0
    cancel_on_timeout: bool = True


class ToolTimeoutPolicyRegistry:
    """
    Per-tool timeout policies. Tools with predictable duration get
    tight timeouts; tools with variable duration get generous ones.
    """

    def __init__(self, default_timeout_s: float = 60.0):
        self._policies: Dict[str, ToolTimeoutPolicy] = {}
        self._default = default_timeout_s

    def register(self, policy: ToolTimeoutPolicy) -> None:
        self._policies[policy.tool_name] = policy

    def get(self, tool_name: str) -> ToolTimeoutPolicy:
        return self._policies.get(
            tool_name,
            ToolTimeoutPolicy(tool_name=tool_name, timeout_s=self._default),
        )
```

## Solution 6: Async Task Dashboard

```python
import time


class AsyncTaskDashboard:
    """
    Combines submission manager stats, active task list, and poller
    stats into a single operational view.
    """

    def __init__(
        self,
        manager: AsyncTaskSubmissionManager,
        poller: AsyncTaskPoller,
    ):
        self._manager = manager
        self._poller = poller

    def render(self) -> dict:
        active = [
            {
                "task_id": h.task_id,
                "tool_name": h.tool_name,
                "elapsed_s": round(h.elapsed_s(), 2),
                "time_remaining_s": round(h.time_remaining_s(), 2),
                "poll_count": h.poll_count,
                "status": h.status.value,
            }
            for h in self._manager._active.values()
        ]
        return {
            "generated_at": time.time(),
            "active_tasks": active,
            "manager_stats": self._manager.stats(),
            "poller_stats": self._poller.stats(),
        }
```

## Comparison

| Approach | Deadline Enforcement | Adaptive Interval | Cancellation | Progress Callbacks | Per-Tool Policy |
|---|---|---|---|---|---|
| AdaptivePollingIntervalCalculator | No | Yes | No | No | No |
| AsyncTaskPoller | Yes (deadline check) | Via calculator | Yes | Yes | No |
| AsyncTaskSubmissionManager | Via poller | Via poller | Yes (cancel_all) | Via poller | No |
| ToolTimeoutPolicyRegistry | No | No | No | No | Yes |
| AsyncTaskDashboard | No | No | No | No | No |

**Best for production**: Always set a deadline at submission time — never rely on the external API to time out gracefully. Use `cancel_on_timeout=True` for tasks with external API costs (code execution, LLM calls) and `False` for idempotent read tasks where the result may still be useful if retrieved late. Set the initial polling interval to 10% of the expected task duration: a task that typically takes 5 seconds should start polling at 500ms, not 100ms. Monitor `timeout_rate` per tool — above 5% means the timeout is too aggressive or the tool is consistently slow and needs its timeout policy updated.
