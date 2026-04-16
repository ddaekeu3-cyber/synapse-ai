---
title: "Agent Doesn't Implement Tool Execution Timeout Escalation"
description: "Agents that apply a single fixed timeout to all tool executions either time out too aggressively on slow-but-legitimate operations or hang indefinitely on stuck tools that never respond. Implement graduated timeout escalation that starts with a soft deadline, logs a warning, attempts a graceful cancellation, and finally applies a hard kill after a second deadline — giving slow tools a chance to complete while guaranteeing eventual termination."
date: 2026-04-16
difficulty: intermediate
category: reliability
slug: agent-doesnt-implement-tool-execution-timeout-escalation
tags: [timeout-escalation, graceful-cancellation, hard-timeout, tool-lifecycle, hung-tool, deadline-management]
symptoms:
  - "Slow but legitimate tool calls cancelled prematurely by aggressive single timeout"
  - "Hung tool calls never terminate — agent waits indefinitely with no fallback"
  - "Single timeout value applied uniformly to fast in-memory and slow network tools"
  - "No warning period before cancellation — tool gets no chance for graceful cleanup"
  - "Tool execution thread leaks when cancellation is not properly awaited"
---

## Why This Happens

A single timeout is a binary choice: too short causes false positives (cancelling slow-but-valid operations), too long allows hung tools to block the agent. Timeout escalation applies multiple deadlines: a soft deadline triggers a warning log and optional graceful-stop signal; a hard deadline kills the coroutine unconditionally. Between the two, the tool has a grace window to clean up or return a partial result. This pattern is standard in OS process management (SIGTERM → wait → SIGKILL) and applies equally to async coroutines.

## Solution 1: Escalation Policy

```python
from dataclasses import dataclass
from typing import Optional


@dataclass
class TimeoutEscalationPolicy:
    soft_timeout_seconds: float     # warn + attempt graceful stop
    hard_timeout_seconds: float     # unconditional cancellation
    tool_name: str = ""
    log_warning: bool = True
    return_partial_on_soft: bool = False

    def __post_init__(self) -> None:
        if self.hard_timeout_seconds <= self.soft_timeout_seconds:
            raise ValueError(
                f"hard_timeout ({self.hard_timeout_seconds}) must exceed "
                f"soft_timeout ({self.soft_timeout_seconds})"
            )

    @property
    def grace_window_seconds(self) -> float:
        return self.hard_timeout_seconds - self.soft_timeout_seconds


DEFAULT_POLICIES = {
    "database": TimeoutEscalationPolicy(soft_timeout_seconds=5.0, hard_timeout_seconds=15.0),
    "external_http": TimeoutEscalationPolicy(soft_timeout_seconds=10.0, hard_timeout_seconds=30.0),
    "file_system": TimeoutEscalationPolicy(soft_timeout_seconds=3.0, hard_timeout_seconds=8.0),
    "llm": TimeoutEscalationPolicy(soft_timeout_seconds=30.0, hard_timeout_seconds=90.0),
    "default": TimeoutEscalationPolicy(soft_timeout_seconds=10.0, hard_timeout_seconds=30.0),
}
```

## Solution 2: Escalating Timeout Executor

```python
import asyncio
import time
from typing import Any, Callable, Optional


class EscalatingTimeoutExecutor:
    """
    Executes a coroutine with soft + hard timeout escalation.
    Soft expiry: logs warning and sends cancellation signal.
    Hard expiry: forcibly cancels the task and raises TimeoutEscalationError.
    """

    def __init__(self):
        self._soft_hits = 0
        self._hard_hits = 0
        self._completed = 0

    async def execute(
        self,
        fn: Callable,
        policy: TimeoutEscalationPolicy,
        *args,
        **kwargs,
    ) -> dict:
        start = time.time()
        task = asyncio.create_task(fn(*args, **kwargs))
        soft_expired = False

        try:
            result = await asyncio.wait_for(
                asyncio.shield(task), timeout=policy.soft_timeout_seconds
            )
            self._completed += 1
            return {
                "result": result,
                "outcome": "completed",
                "latency_ms": round((time.time() - start) * 1000, 2),
            }
        except asyncio.TimeoutError:
            soft_expired = True
            self._soft_hits += 1

        # Soft deadline expired — attempt graceful cancellation in grace window
        task.cancel()
        try:
            await asyncio.wait_for(
                asyncio.shield(task), timeout=policy.grace_window_seconds
            )
            # Task completed during grace window
            self._completed += 1
            return {
                "result": None,
                "outcome": "completed_in_grace",
                "latency_ms": round((time.time() - start) * 1000, 2),
            }
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass

        # Hard deadline — force cancel
        if not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

        self._hard_hits += 1
        raise TimeoutEscalationError(
            tool_name=policy.tool_name,
            soft_timeout=policy.soft_timeout_seconds,
            hard_timeout=policy.hard_timeout_seconds,
            elapsed=round(time.time() - start, 2),
        )

    def stats(self) -> dict:
        return {
            "completed": self._completed,
            "soft_timeout_hits": self._soft_hits,
            "hard_timeout_hits": self._hard_hits,
        }


class TimeoutEscalationError(Exception):
    def __init__(self, tool_name: str, soft_timeout: float, hard_timeout: float, elapsed: float):
        super().__init__(
            f"Tool '{tool_name}' exceeded hard timeout {hard_timeout}s "
            f"(soft={soft_timeout}s, elapsed={elapsed}s)"
        )
        self.tool_name = tool_name
        self.soft_timeout = soft_timeout
        self.hard_timeout = hard_timeout
        self.elapsed = elapsed
```

## Solution 3: Policy Registry

```python
from typing import Dict, Optional


class TimeoutPolicyRegistry:
    """
    Maps tool names and categories to escalation policies.
    Falls back through tool-name -> category -> default.
    """

    def __init__(
        self,
        category_policies: Dict[str, TimeoutEscalationPolicy] = None,
        tool_policies: Dict[str, TimeoutEscalationPolicy] = None,
    ):
        self._categories = category_policies or dict(DEFAULT_POLICIES)
        self._tools = tool_policies or {}

    def register_tool(self, tool_name: str, policy: TimeoutEscalationPolicy) -> None:
        self._tools[tool_name] = policy

    def get(self, tool_name: str, category: str = "default") -> TimeoutEscalationPolicy:
        if tool_name in self._tools:
            pol = self._tools[tool_name]
        elif category in self._categories:
            pol = self._categories[category]
        else:
            pol = self._categories["default"]
        # Return copy with tool name set
        return TimeoutEscalationPolicy(
            soft_timeout_seconds=pol.soft_timeout_seconds,
            hard_timeout_seconds=pol.hard_timeout_seconds,
            tool_name=tool_name,
        )
```

## Solution 4: Timeout Event Recorder

```python
import time
from collections import Counter
from typing import List


class TimeoutEventRecorder:
    """
    Records soft and hard timeout events per tool for trend analysis.
    """

    def __init__(self, max_records: int = 5000):
        self._max = max_records
        self._records: List[dict] = []

    def record_soft(self, tool_name: str, elapsed: float) -> None:
        self._append({"type": "soft", "tool_name": tool_name, "elapsed": elapsed})

    def record_hard(self, tool_name: str, elapsed: float) -> None:
        self._append({"type": "hard", "tool_name": tool_name, "elapsed": elapsed})

    def _append(self, record: dict) -> None:
        record["ts"] = time.time()
        if len(self._records) >= self._max:
            self._records.pop(0)
        self._records.append(record)

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [r for r in self._records if r["ts"] >= cutoff]
        if not recent:
            return {"window_seconds": window_seconds, "timeout_events": 0}
        soft = [r for r in recent if r["type"] == "soft"]
        hard = [r for r in recent if r["type"] == "hard"]
        tool_counts: Counter = Counter(r["tool_name"] for r in recent)
        return {
            "window_seconds": window_seconds,
            "timeout_events": len(recent),
            "soft_timeouts": len(soft),
            "hard_timeouts": len(hard),
            "top_timing_out_tools": tool_counts.most_common(5),
        }
```

## Solution 5: Adaptive Policy Adjuster

```python
from typing import Dict


class AdaptiveTimeoutPolicyAdjuster:
    """
    Monitors timeout frequency per tool and automatically increases
    the soft timeout for tools that consistently time out, up to a cap.
    """

    def __init__(
        self,
        registry: TimeoutPolicyRegistry,
        recorder: TimeoutEventRecorder,
        soft_increase_step: float = 5.0,
        max_soft_seconds: float = 60.0,
        trigger_rate: float = 0.10,    # increase if >10% of calls soft-timeout
    ):
        self._registry = registry
        self._recorder = recorder
        self._step = soft_increase_step
        self._max_soft = max_soft_seconds
        self._trigger = trigger_rate
        self._adjustments: Dict[str, int] = {}

    def adjust_if_needed(
        self,
        tool_name: str,
        category: str,
        total_calls: int,
        window_seconds: float = 3600.0,
    ) -> Optional[TimeoutEscalationPolicy]:
        if total_calls < 20:
            return None
        summary = self._recorder.summary(window_seconds)
        soft_count = sum(
            1 for r in self._recorder._records
            if r.get("tool_name") == tool_name and r.get("type") == "soft"
        )
        rate = soft_count / max(total_calls, 1)
        if rate < self._trigger:
            return None

        current = self._registry.get(tool_name, category)
        new_soft = min(current.soft_timeout_seconds + self._step, self._max_soft)
        new_hard = new_soft * 3.0
        new_policy = TimeoutEscalationPolicy(
            soft_timeout_seconds=new_soft,
            hard_timeout_seconds=new_hard,
            tool_name=tool_name,
        )
        self._registry.register_tool(tool_name, new_policy)
        self._adjustments[tool_name] = self._adjustments.get(tool_name, 0) + 1
        return new_policy
```

## Solution 6: Timeout Escalation Dashboard

```python
import time


class TimeoutEscalationDashboard:
    """
    Combines executor stats, timeout event summary, and policy registry
    into an operational timeout health report.
    """

    def __init__(
        self,
        executor: EscalatingTimeoutExecutor,
        recorder: TimeoutEventRecorder,
        registry: TimeoutPolicyRegistry,
    ):
        self._executor = executor
        self._recorder = recorder
        self._registry = registry

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "executor_stats": self._executor.stats(),
            "timeout_summary_1h": self._recorder.summary(window_seconds=3600.0),
            "registered_tool_policies": {
                name: {
                    "soft_seconds": pol.soft_timeout_seconds,
                    "hard_seconds": pol.hard_timeout_seconds,
                }
                for name, pol in self._registry._tools.items()
            },
        }
```

## Comparison

| Approach | Soft Deadline | Grace Window | Hard Kill | Policy Registry | Adaptive Adjustment |
|---|---|---|---|---|---|
| TimeoutEscalationPolicy | Yes (configurable) | Yes | Yes | No | No |
| EscalatingTimeoutExecutor | Yes | Yes | Yes | No | No |
| TimeoutPolicyRegistry | No | No | No | Yes | No |
| TimeoutEventRecorder | No | No | No | No | No |
| AdaptiveTimeoutPolicyAdjuster | No | No | No | Via registry | Yes |
| TimeoutEscalationDashboard | No | No | No | No | Yes |

**Best for production**: Set `grace_window_seconds` to 20–30% of the hard timeout — enough time for a database query to finish committing but not enough to block the agent meaningfully. Log soft timeouts as warnings with the tool name and elapsed time; log hard timeouts as errors — this distinction helps distinguish "slow but ok" from "truly hung". Use `AdaptiveTimeoutPolicyAdjuster` only as a diagnostic aid — if a tool needs its timeout increased more than once, the underlying tool performance issue should be fixed rather than masked with larger timeouts. Monitor `hard_timeouts` in the recorder: any non-zero hard timeout rate indicates a tool that is not reliably returning within its expected execution time.
