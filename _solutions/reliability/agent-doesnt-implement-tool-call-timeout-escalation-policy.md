---
title: "Agent Doesn't Implement Tool Call Timeout Escalation Policy"
description: "Agents that apply a single fixed timeout to all tool calls use a value that is either too generous (allowing slow tools to block agent tasks for minutes) or too strict (prematurely failing tool calls that legitimately need extra time). Implement a timeout escalation policy that applies different timeouts by tool category, escalates the timeout on retry, cancels the operation and returns a structured timeout error when the escalation ceiling is reached, and logs timeout events for calibration."
date: 2026-04-16
difficulty: intermediate
category: reliability
slug: agent-doesnt-implement-tool-call-timeout-escalation-policy
tags: [timeout-escalation, tool-timeout, retry-timeout, adaptive-timeout, timeout-policy, tool-reliability]
symptoms:
  - "All tool calls share a single 30-second timeout — fast tools wait too long; slow tools are cut short"
  - "A transient slow API call fails immediately on retry because timeout does not increase"
  - "No differentiation between a database query (should be fast) and a file generation tool (may take minutes)"
  - "Timeout events are swallowed or logged generically — no way to calibrate per-tool timeout values"
  - "The agent waits the full timeout on every retry instead of failing faster on repeated timeouts"
---

## Why This Happens

A single global timeout is a reasonable starting point but breaks down once tools have diverse latency profiles. A database lookup that should complete in 200ms and a PDF generation tool that may need 30 seconds have nothing in common except that they are both "tools." Applying the same timeout to both means either the PDF tool is frequently killed prematurely or the database lookup holds the agent hostage for 30 seconds on failure. Timeout escalation addresses the retry scenario: if a tool timed out once, giving it slightly more time on the next attempt (up to a ceiling) balances between giving legitimate slow responses a chance and bounding the total wait time.

## Solution 1: Tool Timeout Policy

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Optional


class ToolCategory(str, Enum):
    FAST_LOOKUP = "fast_lookup"         # DB queries, cache reads: < 2s
    STANDARD_API = "standard_api"       # REST APIs: < 10s
    SLOW_API = "slow_api"               # LLM calls, external AI: < 60s
    FILE_OPERATION = "file_operation"   # file read/write/convert: < 30s
    BACKGROUND_JOB = "background_job"   # async jobs: up to 300s


DEFAULT_TIMEOUT_SECONDS: Dict[ToolCategory, float] = {
    ToolCategory.FAST_LOOKUP: 2.0,
    ToolCategory.STANDARD_API: 10.0,
    ToolCategory.SLOW_API: 60.0,
    ToolCategory.FILE_OPERATION: 30.0,
    ToolCategory.BACKGROUND_JOB: 300.0,
}


@dataclass
class ToolTimeoutPolicy:
    tool_name: str
    category: ToolCategory
    base_timeout_seconds: float
    max_timeout_seconds: float          # ceiling for escalation
    escalation_factor: float = 1.5     # multiply timeout on each retry
    max_attempts: int = 3
    timeout_on_first_attempt: Optional[float] = None  # override for attempt 1

    @classmethod
    def for_category(
        cls,
        tool_name: str,
        category: ToolCategory,
        escalation_factor: float = 1.5,
        max_attempts: int = 3,
    ) -> "ToolTimeoutPolicy":
        base = DEFAULT_TIMEOUT_SECONDS[category]
        return cls(
            tool_name=tool_name,
            category=category,
            base_timeout_seconds=base,
            max_timeout_seconds=base * (escalation_factor ** (max_attempts - 1)),
            escalation_factor=escalation_factor,
            max_attempts=max_attempts,
        )

    def timeout_for_attempt(self, attempt: int) -> float:
        if attempt == 1 and self.timeout_on_first_attempt is not None:
            return self.timeout_on_first_attempt
        escalated = self.base_timeout_seconds * (self.escalation_factor ** (attempt - 1))
        return min(escalated, self.max_timeout_seconds)
```

## Solution 2: Tool Timeout Policy Registry

```python
from typing import Dict, Optional


class ToolTimeoutPolicyRegistry:
    """
    Stores timeout policies per tool. Unregistered tools use a default
    STANDARD_API policy.
    """

    def __init__(self):
        self._policies: Dict[str, ToolTimeoutPolicy] = {}

    def register(self, policy: ToolTimeoutPolicy) -> None:
        self._policies[policy.tool_name] = policy

    def get(self, tool_name: str) -> ToolTimeoutPolicy:
        return self._policies.get(
            tool_name,
            ToolTimeoutPolicy.for_category(tool_name, ToolCategory.STANDARD_API),
        )

    def all_policies(self) -> list:
        return list(self._policies.values())
```

## Solution 3: Timeout Event Record

```python
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class TimeoutOutcome(str, Enum):
    TIMEOUT_THEN_SUCCESS = "timeout_then_success"
    TIMEOUT_CEILING_REACHED = "timeout_ceiling_reached"
    TIMEOUT_THEN_ERROR = "timeout_then_error"


@dataclass
class TimeoutEvent:
    tool_name: str
    attempt: int
    timeout_seconds: float
    actual_elapsed_ms: float
    outcome: Optional[TimeoutOutcome] = None
    session_id: str = ""
    recorded_at: float = field(default_factory=time.time)
```

## Solution 4: Escalating Timeout Executor

```python
import asyncio
import time
from typing import Any, Callable, List, Optional


class EscalatingTimeoutExecutor:
    """
    Executes a tool call with an escalating timeout on each retry.
    Returns the result on success or raises a structured error when
    the maximum attempts / timeout ceiling is reached.
    """

    def __init__(
        self,
        registry: ToolTimeoutPolicyRegistry,
        audit_logger: "TimeoutAuditLogger",
    ):
        self._registry = registry
        self._logger = audit_logger

    async def execute(
        self,
        tool_name: str,
        tool_fn: Callable,
        *args: Any,
        session_id: str = "",
        **kwargs: Any,
    ) -> dict:
        policy = self._registry.get(tool_name)
        timeout_events: List[TimeoutEvent] = []
        last_error: Optional[str] = None

        for attempt in range(1, policy.max_attempts + 1):
            timeout = policy.timeout_for_attempt(attempt)
            start = time.monotonic()

            try:
                result = await asyncio.wait_for(
                    tool_fn(*args, **kwargs),
                    timeout=timeout,
                )
                elapsed_ms = round((time.monotonic() - start) * 1000, 2)

                if timeout_events:
                    # Previous attempts timed out — record final success
                    for ev in timeout_events:
                        ev.outcome = TimeoutOutcome.TIMEOUT_THEN_SUCCESS
                        self._logger.record(ev)

                return {
                    "result": result,
                    "attempt": attempt,
                    "timeout_events": len(timeout_events),
                    "latency_ms": elapsed_ms,
                }

            except asyncio.TimeoutError:
                elapsed_ms = round((time.monotonic() - start) * 1000, 2)
                event = TimeoutEvent(
                    tool_name=tool_name,
                    attempt=attempt,
                    timeout_seconds=timeout,
                    actual_elapsed_ms=elapsed_ms,
                    session_id=session_id,
                )
                timeout_events.append(event)
                last_error = f"timeout after {timeout}s on attempt {attempt}"

                if attempt == policy.max_attempts:
                    for ev in timeout_events:
                        ev.outcome = TimeoutOutcome.TIMEOUT_CEILING_REACHED
                        self._logger.record(ev)
                    raise ToolTimeoutCeilingError(tool_name, policy, timeout_events)

            except Exception as exc:
                elapsed_ms = round((time.monotonic() - start) * 1000, 2)
                if timeout_events:
                    for ev in timeout_events:
                        ev.outcome = TimeoutOutcome.TIMEOUT_THEN_ERROR
                        self._logger.record(ev)
                raise

        raise ToolTimeoutCeilingError(tool_name, policy, timeout_events)


class ToolTimeoutCeilingError(Exception):
    def __init__(
        self,
        tool_name: str,
        policy: ToolTimeoutPolicy,
        events: List[TimeoutEvent],
    ):
        super().__init__(
            f"tool '{tool_name}' exceeded timeout ceiling after {len(events)} attempt(s)"
        )
        self.tool_name = tool_name
        self.policy = policy
        self.timeout_events = events
```

## Solution 5: Timeout Calibration Advisor

```python
from typing import Dict, List


class TimeoutCalibrationAdvisor:
    """
    Analyzes timeout event history and recommends per-tool timeout adjustments.
    A tool that times out frequently needs a longer base timeout;
    one that never reaches half its timeout may have too-generous settings.
    """

    def __init__(self, logger: "TimeoutAuditLogger"):
        self._logger = logger

    def advise(self, window_seconds: float = 86400.0) -> List[dict]:
        summary = self._logger.per_tool_summary(window_seconds)
        recommendations = []
        for tool_name, stats in summary.items():
            timeout_rate = stats.get("timeout_rate", 0)
            avg_timeout_used = stats.get("avg_timeout_seconds", 0)
            if timeout_rate > 0.10:
                recommendations.append({
                    "tool_name": tool_name,
                    "action": "increase_base_timeout",
                    "reason": f"timeout rate {timeout_rate:.1%} exceeds 10%",
                    "suggested_multiplier": 1.5,
                })
            elif timeout_rate == 0 and avg_timeout_used > 0:
                recommendations.append({
                    "tool_name": tool_name,
                    "action": "consider_reducing_timeout",
                    "reason": "zero timeouts in window — base timeout may be over-provisioned",
                })
        return recommendations
```

## Solution 6: Timeout Audit Logger

```python
import time
from typing import Dict, List


class TimeoutAuditLogger:
    """
    Records timeout events and provides per-tool statistics for calibration.
    """

    def __init__(self, max_records: int = 20000):
        self._max = max_records
        self._records: List[dict] = []

    def record(self, event: TimeoutEvent) -> None:
        if len(self._records) >= self._max:
            self._records.pop(0)
        self._records.append({
            "ts": time.time(),
            "tool_name": event.tool_name,
            "attempt": event.attempt,
            "timeout_seconds": event.timeout_seconds,
            "elapsed_ms": event.actual_elapsed_ms,
            "outcome": event.outcome.value if event.outcome else None,
            "session_id": event.session_id,
        })

    def per_tool_summary(self, window_seconds: float = 3600.0) -> Dict[str, dict]:
        cutoff = time.time() - window_seconds
        recent = [r for r in self._records if r["ts"] >= cutoff]

        by_tool: Dict[str, list] = {}
        for r in recent:
            by_tool.setdefault(r["tool_name"], []).append(r)

        summary = {}
        for tool, records in by_tool.items():
            ceiling_hits = sum(
                1 for r in records if r.get("outcome") == TimeoutOutcome.TIMEOUT_CEILING_REACHED.value
            )
            summary[tool] = {
                "events": len(records),
                "ceiling_hits": ceiling_hits,
                "timeout_rate": round(ceiling_hits / len(records), 4),
                "avg_timeout_seconds": round(
                    sum(r["timeout_seconds"] for r in records) / len(records), 2
                ),
            }
        return summary
```

## Comparison

| Approach | Per-Category Timeout | Escalation on Retry | Ceiling Enforcement | Calibration Advice | Audit Log |
|---|---|---|---|---|---|
| ToolTimeoutPolicy | Yes | Yes (escalation_factor) | Yes (max_timeout) | No | No |
| ToolTimeoutPolicyRegistry | Via policies | No | No | No | No |
| EscalatingTimeoutExecutor | Via registry | Yes | Yes (ceiling error) | No | Via logger |
| TimeoutCalibrationAdvisor | No | No | No | Yes | Via logger |
| TimeoutAuditLogger | No | No | No | No | Yes |

**Best for production**: Start with `escalation_factor=1.5` and `max_attempts=3` — this gives a tool with a 10-second base timeout 10s → 15s → 22.5s across three attempts, bounding total wait at ~47 seconds without allowing indefinite retries. Register every tool explicitly in the `ToolTimeoutPolicyRegistry` rather than relying on the default `STANDARD_API` category — the default is a fallback for unknown tools, not a deliberate policy. Run `TimeoutCalibrationAdvisor.advise()` weekly and update policies based on actual timeout rates: a tool with a 15% timeout rate on 10-second calls needs either a longer timeout or investigation of why it is slow. Emit `ToolTimeoutCeilingError` as a structured log event including `tool_name`, `attempt_count`, and `total_waited_ms` — this is the signal that a tool is systematically too slow for its current timeout budget.
