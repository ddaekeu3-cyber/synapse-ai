---
title: "Agent Doesn't Implement Graceful Degradation on Tool Unavailability"
description: "Agents that treat every tool as essential will fail completely when a single tool is unavailable — a database query tool times out and the entire agent task aborts rather than continuing with partial information. Implement graceful degradation that classifies tools by criticality, provides fallback behaviors for non-essential tools, surfaces partial results when some tools fail, and continues the task to the best possible completion rather than aborting."
date: 2026-04-16
difficulty: intermediate
category: reliability
slug: agent-doesnt-implement-graceful-degradation-on-tool-unavailability
tags: [graceful-degradation, tool-fallback, partial-results, fault-tolerance, tool-criticality, availability]
symptoms:
  - "Agent task fails completely when one of five tools is unavailable"
  - "No distinction between essential and optional tools — all failures are treated as fatal"
  - "Agent returns an error instead of partial results when a non-critical enrichment tool times out"
  - "No fallback behavior defined for any tool — the agent has no plan B"
  - "Users receive a hard failure on tasks that could have been 80% completed with available tools"
---

## Why This Happens

Tool invocation in most agent frameworks raises an exception on failure and the agent propagates the exception upward, aborting the task. There is no built-in notion of tool criticality or fallback. When a weather API tool, a secondary knowledge-base lookup, or an optional enrichment call fails, the agent treats it identically to a failure in the primary database tool that the task cannot proceed without. Graceful degradation requires classifying each tool as critical (task cannot complete without it) or optional (task can continue with reduced quality), defining fallback behaviors for optional tools, and composing partial results into the best possible response.

## Solution 1: Tool Criticality Descriptor

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, List, Optional


class ToolCriticality(str, Enum):
    CRITICAL = "critical"       # task aborts if this tool fails
    IMPORTANT = "important"     # retry; proceed with warning if all retries fail
    OPTIONAL = "optional"       # skip silently if unavailable; use fallback if defined
    ENRICHMENT = "enrichment"   # best-effort; never blocks task completion


@dataclass
class ToolDegradationPolicy:
    tool_name: str
    criticality: ToolCriticality
    max_retries: int = 2
    retry_delay_seconds: float = 0.5
    fallback_value: Any = None              # returned when tool is skipped
    fallback_fn: Optional[Callable] = None  # async fn() -> value; takes priority over fallback_value
    timeout_seconds: float = 10.0
    tags: List[str] = field(default_factory=list)
```

## Solution 2: Tool Criticality Registry

```python
from typing import Dict, Optional


class ToolCriticalityRegistry:
    """
    Stores degradation policies for all known tools.
    Tools not registered default to IMPORTANT criticality.
    """

    def __init__(self):
        self._policies: Dict[str, ToolDegradationPolicy] = {}

    def register(self, policy: ToolDegradationPolicy) -> None:
        self._policies[policy.tool_name] = policy

    def get(self, tool_name: str) -> ToolDegradationPolicy:
        return self._policies.get(
            tool_name,
            ToolDegradationPolicy(
                tool_name=tool_name,
                criticality=ToolCriticality.IMPORTANT,
            ),
        )

    def critical_tools(self) -> list:
        return [p for p in self._policies.values() if p.criticality == ToolCriticality.CRITICAL]

    def optional_tools(self) -> list:
        return [
            p for p in self._policies.values()
            if p.criticality in (ToolCriticality.OPTIONAL, ToolCriticality.ENRICHMENT)
        ]
```

## Solution 3: Degrading Tool Executor

```python
import asyncio
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Optional


class ToolExecutionOutcome(str, Enum):
    SUCCESS = "success"
    FALLBACK = "fallback"
    SKIPPED = "skipped"
    FAILED = "failed"          # only for CRITICAL tools; task should abort


@dataclass
class ToolExecutionResult:
    tool_name: str
    outcome: ToolExecutionOutcome
    value: Any
    attempts: int
    latency_ms: float
    error: Optional[str] = None
    is_degraded: bool = False


class DegradingToolExecutor:
    """
    Executes a tool according to its degradation policy.
    Returns a ToolExecutionResult regardless of outcome — callers
    never see raw exceptions from optional or enrichment tools.
    """

    def __init__(self, registry: ToolCriticalityRegistry):
        self._registry = registry

    async def execute(
        self,
        tool_name: str,
        tool_fn: Callable,
        *args: Any,
        **kwargs: Any,
    ) -> ToolExecutionResult:
        policy = self._registry.get(tool_name)
        start = time.monotonic()
        last_error = None

        for attempt in range(1, policy.max_retries + 2):
            try:
                value = await asyncio.wait_for(
                    tool_fn(*args, **kwargs),
                    timeout=policy.timeout_seconds,
                )
                latency_ms = round((time.monotonic() - start) * 1000, 2)
                return ToolExecutionResult(
                    tool_name=tool_name,
                    outcome=ToolExecutionOutcome.SUCCESS,
                    value=value,
                    attempts=attempt,
                    latency_ms=latency_ms,
                )
            except Exception as exc:
                last_error = str(exc)
                if attempt <= policy.max_retries:
                    await asyncio.sleep(policy.retry_delay_seconds)

        latency_ms = round((time.monotonic() - start) * 1000, 2)

        # All retries exhausted — apply criticality policy
        if policy.criticality == ToolCriticality.CRITICAL:
            return ToolExecutionResult(
                tool_name=tool_name,
                outcome=ToolExecutionOutcome.FAILED,
                value=None,
                attempts=policy.max_retries + 1,
                latency_ms=latency_ms,
                error=last_error,
                is_degraded=False,
            )

        # Non-critical: use fallback
        fallback = None
        if policy.fallback_fn:
            try:
                fallback = await policy.fallback_fn()
            except Exception:
                fallback = policy.fallback_value
        else:
            fallback = policy.fallback_value

        return ToolExecutionResult(
            tool_name=tool_name,
            outcome=ToolExecutionOutcome.FALLBACK if fallback is not None else ToolExecutionOutcome.SKIPPED,
            value=fallback,
            attempts=policy.max_retries + 1,
            latency_ms=latency_ms,
            error=last_error,
            is_degraded=True,
        )
```

## Solution 4: Partial Result Assembler

```python
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class PartialTaskResult:
    task_id: str
    completed: bool
    tool_results: Dict[str, ToolExecutionResult]
    final_value: Any
    degraded_tools: List[str]
    failed_tools: List[str]
    degradation_summary: str = ""

    @property
    def is_fully_degraded(self) -> bool:
        return len(self.failed_tools) > 0

    @property
    def degradation_level(self) -> str:
        if not self.degraded_tools and not self.failed_tools:
            return "none"
        if self.failed_tools:
            return "critical"
        if len(self.degraded_tools) > 2:
            return "high"
        return "partial"


class PartialResultAssembler:
    """
    Combines tool execution results into a PartialTaskResult.
    Aborts if any CRITICAL tool failed; otherwise assembles the best
    possible result from available data.
    """

    def assemble(
        self,
        task_id: str,
        results: List[ToolExecutionResult],
        compose_fn: Any,   # fn(Dict[tool_name, value]) -> final_value
    ) -> PartialTaskResult:
        tool_map = {r.tool_name: r for r in results}
        failed = [r.tool_name for r in results if r.outcome == ToolExecutionOutcome.FAILED]
        degraded = [r.tool_name for r in results if r.is_degraded]

        if failed:
            return PartialTaskResult(
                task_id=task_id,
                completed=False,
                tool_results=tool_map,
                final_value=None,
                degraded_tools=degraded,
                failed_tools=failed,
                degradation_summary=f"Task aborted: critical tool(s) failed: {', '.join(failed)}",
            )

        available_values = {r.tool_name: r.value for r in results}
        try:
            final_value = compose_fn(available_values)
        except Exception as exc:
            final_value = available_values

        summary_parts = []
        if degraded:
            summary_parts.append(f"{len(degraded)} tool(s) used fallback: {', '.join(degraded)}")

        return PartialTaskResult(
            task_id=task_id,
            completed=True,
            tool_results=tool_map,
            final_value=final_value,
            degraded_tools=degraded,
            failed_tools=[],
            degradation_summary="; ".join(summary_parts) if summary_parts else "all tools nominal",
        )
```

## Solution 5: Degradation Event Logger

```python
import time
from typing import List


class DegradationEventLogger:
    """
    Records tool degradation and fallback events for post-incident analysis.
    Identifies which tools degrade most often and which fallbacks are invoked.
    """

    def __init__(self, max_records: int = 5000):
        self._max = max_records
        self._records: List[dict] = []

    def record(self, result: ToolExecutionResult, session_id: str = "") -> None:
        if result.outcome == ToolExecutionOutcome.SUCCESS:
            return
        if len(self._records) >= self._max:
            self._records.pop(0)
        self._records.append({
            "ts": time.time(),
            "session_id": session_id,
            "tool_name": result.tool_name,
            "outcome": result.outcome.value,
            "attempts": result.attempts,
            "latency_ms": result.latency_ms,
            "error": result.error,
        })

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [r for r in self._records if r["ts"] >= cutoff]
        if not recent:
            return {"window_seconds": window_seconds, "events": 0}

        by_tool: dict = {}
        for r in recent:
            name = r["tool_name"]
            if name not in by_tool:
                by_tool[name] = {"fallback": 0, "skipped": 0, "failed": 0}
            by_tool[name][r["outcome"]] = by_tool[name].get(r["outcome"], 0) + 1

        return {
            "window_seconds": window_seconds,
            "events": len(recent),
            "by_tool": by_tool,
            "most_degraded": max(by_tool, key=lambda t: sum(by_tool[t].values())) if by_tool else None,
        }
```

## Solution 6: Graceful Degradation Dashboard

```python
import time


class GracefulDegradationDashboard:
    """
    Combines live degradation policy snapshot with recent event summary.
    """

    def __init__(
        self,
        registry: ToolCriticalityRegistry,
        logger: DegradationEventLogger,
    ):
        self._registry = registry
        self._logger = logger

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "policy_summary": {
                "critical_tools": [p.tool_name for p in self._registry.critical_tools()],
                "optional_tools": [p.tool_name for p in self._registry.optional_tools()],
            },
            "recent_degradation": self._logger.summary(window_seconds=3600.0),
        }
```

## Comparison

| Approach | Criticality Classification | Retry Logic | Fallback Execution | Partial Result Assembly | Audit Log |
|---|---|---|---|---|---|
| ToolDegradationPolicy | Yes (4 levels) | Yes (configurable) | Yes (value or fn) | No | No |
| ToolCriticalityRegistry | Via policies | No | No | No | No |
| DegradingToolExecutor | Via registry | Yes | Yes (with fallback_fn) | No | No |
| PartialResultAssembler | No | No | No | Yes (compose_fn) | No |
| DegradationEventLogger | No | No | No | No | Yes |
| GracefulDegradationDashboard | No | No | No | No | Yes (aggregate) |

**Best for production**: Register every tool with an explicit `ToolCriticality` — the default `IMPORTANT` is rarely the right choice and forces engineers to think about what the agent can and cannot complete without. Define `fallback_fn` rather than `fallback_value` for tools that have a cheaper alternative (e.g., fall back to a cached result or a lower-fidelity API when the primary is down). Set `max_retries=1` for enrichment tools and `max_retries=3` for important tools — over-retrying optional tools delays task completion without improving outcomes. Monitor `DegradationEventLogger.summary()` to identify tools that degrade frequently: a tool that falls back more than 5% of the time should be replaced or made more reliable rather than compensated for with ever-more-complex fallback logic.
