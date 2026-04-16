---
title: "Agent Doesn't Implement Tool Call Timeout Escalation"
description: "Agents that apply the same fixed timeout to every tool call either time out too aggressively on legitimately slow operations or wait too long on genuinely stuck ones. Implement timeout escalation that starts with a short initial timeout, retries with progressively longer timeouts on recoverable failures, and applies a hard ceiling regardless of how many escalations have occurred."
date: 2026-04-16
difficulty: intermediate
category: reliability
slug: agent-doesnt-implement-tool-call-timeout-escalation
tags: [timeout-escalation, adaptive-timeout, tool-reliability, progressive-timeout, hard-ceiling, retry-strategy]
symptoms:
  - "Tool calls time out at 5s during legitimate slow operations that occasionally take 12s"
  - "No distinction between a timeout on first attempt and a timeout after three retries"
  - "Same 30s timeout applied to a fast cache lookup and a slow database export"
  - "Hard timeout is set so high that stuck tool calls block threads for minutes"
  - "Timeout is never escalated — every retry uses the same initial timeout value"
---

## Why This Happens

A single fixed timeout is a compromise that fails in both directions: too short, and legitimate slow operations are aborted; too long, and stuck operations hold resources. Timeout escalation starts with a short timeout that catches most errors quickly, and progressively extends it on each retry attempt up to a hard ceiling. This captures the common case (short timeout succeeds) without permanently penalizing operations that occasionally need more time, while the hard ceiling prevents runaway waits.

## Solution 1: Timeout Escalation Policy

```python
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class TimeoutEscalationPolicy:
    """
    Defines timeout progression across retry attempts.
    Each attempt uses the corresponding timeout, or the last if attempts > len(timeouts).
    """
    initial_timeout_seconds: float = 5.0
    escalation_steps: List[float] = field(default_factory=lambda: [10.0, 20.0, 45.0])
    hard_ceiling_seconds: float = 60.0
    max_attempts: int = 4

    def timeout_for_attempt(self, attempt: int) -> float:
        """attempt is 0-indexed."""
        if attempt == 0:
            return min(self.initial_timeout_seconds, self.hard_ceiling_seconds)
        step_idx = min(attempt - 1, len(self.escalation_steps) - 1)
        return min(self.escalation_steps[step_idx], self.hard_ceiling_seconds)

    def all_timeouts(self) -> List[float]:
        return [self.timeout_for_attempt(i) for i in range(self.max_attempts)]
```

## Solution 2: Timeout Escalation Executor

```python
import asyncio
import time
from typing import Any, Callable, Optional


class TimeoutEscalationRecord:
    def __init__(self, tool_name: str):
        self.tool_name = tool_name
        self.attempts: list = []  # [(attempt, timeout_used, outcome, duration_ms)]
        self.total_start = time.time()

    def record_attempt(self, attempt: int, timeout: float, outcome: str, duration_ms: float) -> None:
        self.attempts.append({
            "attempt": attempt,
            "timeout_seconds": timeout,
            "outcome": outcome,
            "duration_ms": duration_ms,
        })

    def to_dict(self) -> dict:
        return {
            "tool_name": self.tool_name,
            "total_attempts": len(self.attempts),
            "total_elapsed_ms": round((time.time() - self.total_start) * 1000, 2),
            "attempts": self.attempts,
        }


class TimeoutEscalationExecutor:
    """
    Executes a tool call with progressive timeout escalation.
    Each failed timeout attempt retries with a longer timeout until the ceiling.
    """

    def __init__(self, policy: TimeoutEscalationPolicy):
        self._policy = policy

    async def execute(
        self,
        fn: Callable,
        tool_name: str = "tool",
        *args: Any,
        **kwargs: Any,
    ) -> tuple:
        """Returns (result, TimeoutEscalationRecord)."""
        record = TimeoutEscalationRecord(tool_name)
        last_exc = None

        for attempt in range(self._policy.max_attempts):
            timeout = self._policy.timeout_for_attempt(attempt)
            start = time.time()

            try:
                result = await asyncio.wait_for(
                    fn(*args, **kwargs),
                    timeout=timeout,
                )
                duration_ms = round((time.time() - start) * 1000, 2)
                record.record_attempt(attempt, timeout, "success", duration_ms)
                return result, record

            except asyncio.TimeoutError as exc:
                duration_ms = round((time.time() - start) * 1000, 2)
                record.record_attempt(attempt, timeout, "timeout", duration_ms)
                last_exc = exc
                # Continue to next attempt with longer timeout
                continue

            except Exception as exc:
                duration_ms = round((time.time() - start) * 1000, 2)
                record.record_attempt(attempt, timeout, f"error:{type(exc).__name__}", duration_ms)
                last_exc = exc
                break  # Non-timeout errors are not retried by escalation

        raise asyncio.TimeoutError(
            f"Tool '{tool_name}' timed out after {len(record.attempts)} attempts. "
            f"Timeouts: {[a['timeout_seconds'] for a in record.attempts]}"
        ) from last_exc
```

## Solution 3: Per-Tool Escalation Policy Registry

```python
from typing import Dict, Optional


class PerToolEscalationPolicyRegistry:
    """
    Stores per-tool timeout escalation policies.
    Falls back to a default policy for unregistered tools.
    """

    def __init__(self, default_policy: Optional[TimeoutEscalationPolicy] = None):
        self._policies: Dict[str, TimeoutEscalationPolicy] = {}
        self._default = default_policy or TimeoutEscalationPolicy()

    def register(self, tool_name: str, policy: TimeoutEscalationPolicy) -> None:
        self._policies[tool_name] = policy

    def get(self, tool_name: str) -> TimeoutEscalationPolicy:
        return self._policies.get(tool_name, self._default)

    def register_fast_tool(self, tool_name: str) -> None:
        """Cache lookups, simple queries — short timeouts, few escalations."""
        self.register(tool_name, TimeoutEscalationPolicy(
            initial_timeout_seconds=2.0,
            escalation_steps=[5.0],
            hard_ceiling_seconds=10.0,
            max_attempts=2,
        ))

    def register_slow_tool(self, tool_name: str) -> None:
        """Data exports, PDF generation — longer initial, more headroom."""
        self.register(tool_name, TimeoutEscalationPolicy(
            initial_timeout_seconds=15.0,
            escalation_steps=[30.0, 60.0, 120.0],
            hard_ceiling_seconds=180.0,
            max_attempts=4,
        ))
```

## Solution 4: Escalation-Integrated Tool Dispatcher

```python
from typing import Any, Callable, Dict, Optional


class EscalationIntegratedToolDispatcher:
    """
    Dispatches tool calls using per-tool timeout escalation policies.
    Records and surfaces escalation statistics.
    """

    def __init__(
        self,
        registry: PerToolEscalationPolicyRegistry,
        tool_registry: Dict[str, Callable],
        escalation_log_fn: Optional[Callable[[dict], None]] = None,
    ):
        self._registry = registry
        self._tools = tool_registry
        self._log = escalation_log_fn
        self._total = 0
        self._escalations = 0

    async def dispatch(
        self,
        tool_name: str,
        **kwargs: Any,
    ) -> Any:
        tool_fn = self._tools.get(tool_name)
        if not tool_fn:
            raise KeyError(f"Tool '{tool_name}' not registered")

        policy = self._registry.get(tool_name)
        executor = TimeoutEscalationExecutor(policy)
        self._total += 1

        result, record = await executor.execute(tool_fn, tool_name, **kwargs)

        if len(record.attempts) > 1:
            self._escalations += 1
            if self._log:
                self._log(record.to_dict())

        return result

    def stats(self) -> dict:
        return {
            "total_dispatches": self._total,
            "escalation_events": self._escalations,
            "escalation_rate": round(self._escalations / max(self._total, 1), 4),
        }
```

## Solution 5: Timeout Escalation History Tracker

```python
import time
from threading import Lock
from typing import List


class TimeoutEscalationHistoryTracker:
    """
    Records escalation events to surface which tools most frequently
    require timeout escalation and whether escalation succeeds.
    """

    def __init__(self):
        self._records: List[dict] = []
        self._lock = Lock()

    def record(self, escalation_record: dict) -> None:
        with self._lock:
            self._records.append({"ts": time.time(), **escalation_record})
            if len(self._records) > 10000:
                self._records.pop(0)

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        with self._lock:
            recent = [r for r in self._records if r.get("ts", 0) >= cutoff]
        if not recent:
            return {"window_seconds": window_seconds, "escalations": 0}

        by_tool: dict = {}
        for r in recent:
            name = r.get("tool_name", "unknown")
            by_tool[name] = by_tool.get(name, 0) + 1

        return {
            "window_seconds": window_seconds,
            "escalations": len(recent),
            "by_tool": dict(sorted(by_tool.items(), key=lambda x: -x[1])),
        }
```

## Solution 6: Timeout Escalation Dashboard

```python
import time


class TimeoutEscalationDashboard:
    """
    Combines dispatcher stats, escalation history, and policy overview.
    """

    def __init__(
        self,
        dispatcher: EscalationIntegratedToolDispatcher,
        history: TimeoutEscalationHistoryTracker,
        registry: PerToolEscalationPolicyRegistry,
    ):
        self._dispatcher = dispatcher
        self._history = history
        self._registry = registry

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "dispatcher_stats": self._dispatcher.stats(),
            "escalation_history_1h": self._history.summary(3600.0),
            "registered_tools": list(self._registry._policies.keys()),
        }
```

## Comparison

| Approach | Per-Attempt Timeout | Escalation Steps | Hard Ceiling | Per-Tool Policy | History Tracking |
|---|---|---|---|---|---|
| TimeoutEscalationPolicy | Yes | Yes | Yes | No | No |
| TimeoutEscalationExecutor | Yes | Yes | Via policy | No | Yes (per-call) |
| PerToolEscalationPolicyRegistry | No | No | No | Yes | No |
| EscalationIntegratedToolDispatcher | Via executor | Via executor | Via policy | Via registry | No |
| TimeoutEscalationHistoryTracker | No | No | No | No | Yes (aggregate) |

**Best for production**: Register every tool with an explicit policy rather than relying on the default — the difference between a cache lookup (2s ceiling) and a report generator (180s ceiling) is too large to handle with one default. Set `max_attempts=3` for most tools: the marginal value of a 4th retry after three timeouts is minimal and wastes time. Monitor `escalation_rate` via `EscalationIntegratedToolDispatcher.stats()`: a rate above 5% for a specific tool indicates its baseline performance has degraded and the initial timeout should be increased, or the tool needs a circuit breaker. Log every escalation event to `TimeoutEscalationHistoryTracker` for post-incident analysis.
