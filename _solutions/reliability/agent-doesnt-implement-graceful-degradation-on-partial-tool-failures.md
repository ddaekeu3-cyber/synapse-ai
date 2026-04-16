---
title: "Agent Doesn't Implement Graceful Degradation on Partial Tool Failures"
description: "Agents that treat any tool failure as a fatal error stop mid-task when a single optional tool is unavailable — even when the remaining tools are sufficient to complete most of the work. Implement graceful degradation that classifies tools as required or optional, continues execution when optional tools fail, and surfaces a partial-success result with a clear accounting of what was skipped."
date: 2026-04-16
difficulty: intermediate
category: reliability
slug: agent-doesnt-implement-graceful-degradation-on-partial-tool-failures
tags: [graceful-degradation, partial-failure, tool-availability, fault-tolerance, partial-success, resilience]
symptoms:
  - "Agent aborts entirely when one optional enrichment tool returns a 503"
  - "No distinction between required tools (task cannot complete without) and optional tools"
  - "User receives an error response instead of a partial result with a note about what failed"
  - "Tool failure in step 3 of 10 causes steps 4–10 to never execute"
  - "No summary of which tools succeeded, which were skipped, and why"
---

## Why This Happens

Most tool orchestration code propagates exceptions upward immediately. When a news-enrichment tool fails, the exception bubbles through the same path as a required database lookup failure — both halt execution. Graceful degradation requires explicit tool classification (required vs. optional), exception interception at the tool call boundary, and a result aggregation layer that can return partial output. The key insight is that many agent tasks are decomposable: the core answer can be computed from required tools alone, with optional tools adding enrichment that can be omitted when unavailable.

## Solution 1: Tool Availability Classification

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class ToolRequirementLevel(str, Enum):
    REQUIRED = "required"       # task cannot complete without this tool
    PREFERRED = "preferred"     # significant quality loss if absent, but continuable
    OPTIONAL = "optional"       # enrichment only — skip silently on failure


@dataclass
class ToolAvailabilitySpec:
    tool_name: str
    requirement_level: ToolRequirementLevel
    fallback_value: Any = None          # value to use when optional tool fails
    skip_message: str = ""              # human-readable note for partial result
    max_latency_ms: float = 5000.0      # treat as failed if slower than this


@dataclass
class ToolExecutionOutcome:
    tool_name: str
    success: bool
    result: Any
    requirement_level: ToolRequirementLevel
    error: Optional[str] = None
    latency_ms: float = 0.0
    used_fallback: bool = False
```

## Solution 2: Degradation-Aware Tool Executor

```python
import asyncio
import time
from typing import Any, Callable, Dict, List


class DegradationAwareToolExecutor:
    """
    Executes a tool call and intercepts failures according to the tool's
    requirement level. Required tools raise; optional tools return fallback.
    """

    async def execute(
        self,
        spec: ToolAvailabilitySpec,
        fn: Callable,
        *args: Any,
        **kwargs: Any,
    ) -> ToolExecutionOutcome:
        start = time.time()
        try:
            result = await asyncio.wait_for(
                fn(*args, **kwargs),
                timeout=spec.max_latency_ms / 1000.0,
            )
            return ToolExecutionOutcome(
                tool_name=spec.tool_name,
                success=True,
                result=result,
                requirement_level=spec.requirement_level,
                latency_ms=round((time.time() - start) * 1000, 2),
            )
        except asyncio.TimeoutError as exc:
            error_msg = f"timeout after {spec.max_latency_ms}ms"
            return self._handle_failure(spec, error_msg, start)
        except Exception as exc:
            return self._handle_failure(spec, str(exc), start)

    def _handle_failure(
        self,
        spec: ToolAvailabilitySpec,
        error_msg: str,
        start: float,
    ) -> ToolExecutionOutcome:
        latency_ms = round((time.time() - start) * 1000, 2)
        if spec.requirement_level == ToolRequirementLevel.REQUIRED:
            raise ToolRequiredError(spec.tool_name, error_msg)
        return ToolExecutionOutcome(
            tool_name=spec.tool_name,
            success=False,
            result=spec.fallback_value,
            requirement_level=spec.requirement_level,
            error=error_msg,
            latency_ms=latency_ms,
            used_fallback=True,
        )


class ToolRequiredError(Exception):
    def __init__(self, tool_name: str, reason: str):
        super().__init__(f"required tool '{tool_name}' failed: {reason}")
        self.tool_name = tool_name
        self.reason = reason
```

## Solution 3: Partial Result Assembler

```python
from typing import Any, Dict, List, Optional


class PartialResultAssembler:
    """
    Aggregates tool execution outcomes into a partial result object.
    Provides clear accounting of what succeeded, what was skipped,
    and which skips degraded result quality.
    """

    def __init__(self, outcomes: List[ToolExecutionOutcome]):
        self._outcomes = outcomes

    def build(self, core_result: Any) -> dict:
        succeeded = [o for o in self._outcomes if o.success]
        failed_optional = [o for o in self._outcomes if not o.success and
                           o.requirement_level == ToolRequirementLevel.OPTIONAL]
        failed_preferred = [o for o in self._outcomes if not o.success and
                            o.requirement_level == ToolRequirementLevel.PREFERRED]

        skipped_notes = []
        for o in failed_optional + failed_preferred:
            note = f"{o.tool_name}: {o.error}"
            skipped_notes.append(note)

        quality = "full"
        if failed_preferred:
            quality = "degraded"
        elif failed_optional:
            quality = "partial"

        return {
            "result": core_result,
            "quality": quality,
            "tool_summary": {
                "total": len(self._outcomes),
                "succeeded": len(succeeded),
                "skipped_optional": len(failed_optional),
                "skipped_preferred": len(failed_preferred),
            },
            "skipped_notes": skipped_notes,
            "is_partial": bool(failed_optional or failed_preferred),
        }
```

## Solution 4: Graceful Degradation Orchestrator

```python
import asyncio
from typing import Any, Callable, Dict, List, Optional, Tuple


class GracefulDegradationOrchestrator:
    """
    Runs a set of tool calls with degradation awareness.
    Collects all outcomes and returns a partial result if optional
    tools fail — only raises if a required tool fails.
    """

    def __init__(self, executor: DegradationAwareToolExecutor):
        self._executor = executor

    async def run_all(
        self,
        calls: List[Tuple[ToolAvailabilitySpec, Callable, tuple, dict]],
    ) -> Tuple[List[ToolExecutionOutcome], bool]:
        """
        calls: list of (spec, fn, args, kwargs)
        Returns (outcomes, all_required_succeeded).
        Raises ToolRequiredError immediately if a required tool fails.
        """
        outcomes: List[ToolExecutionOutcome] = []
        for spec, fn, args, kwargs in calls:
            outcome = await self._executor.execute(spec, fn, *args, **kwargs)
            outcomes.append(outcome)
        return outcomes, True

    async def run_parallel_optional(
        self,
        required_calls: List[Tuple[ToolAvailabilitySpec, Callable, tuple, dict]],
        optional_calls: List[Tuple[ToolAvailabilitySpec, Callable, tuple, dict]],
    ) -> List[ToolExecutionOutcome]:
        """
        Runs required calls sequentially first (fail-fast), then
        optional calls in parallel (degrade on failure).
        """
        outcomes: List[ToolExecutionOutcome] = []
        for spec, fn, args, kwargs in required_calls:
            outcome = await self._executor.execute(spec, fn, *args, **kwargs)
            outcomes.append(outcome)

        if optional_calls:
            optional_tasks = [
                self._executor.execute(spec, fn, *args, **kwargs)
                for spec, fn, args, kwargs in optional_calls
            ]
            optional_results = await asyncio.gather(*optional_tasks, return_exceptions=False)
            outcomes.extend(optional_results)

        return outcomes
```

## Solution 5: Degradation Policy Registry

```python
from typing import Dict, List, Optional


class DegradationPolicyRegistry:
    """
    Maps task types to lists of ToolAvailabilitySpecs.
    Allows different degradation policies per agent task.
    """

    def __init__(self):
        self._policies: Dict[str, List[ToolAvailabilitySpec]] = {}

    def register(self, task_type: str, specs: List[ToolAvailabilitySpec]) -> None:
        self._policies[task_type] = specs

    def get(self, task_type: str) -> List[ToolAvailabilitySpec]:
        return self._policies.get(task_type, [])

    def required_tools(self, task_type: str) -> List[ToolAvailabilitySpec]:
        return [
            s for s in self.get(task_type)
            if s.requirement_level == ToolRequirementLevel.REQUIRED
        ]

    def optional_tools(self, task_type: str) -> List[ToolAvailabilitySpec]:
        return [
            s for s in self.get(task_type)
            if s.requirement_level != ToolRequirementLevel.REQUIRED
        ]
```

## Solution 6: Degradation Event Logger

```python
import time
from typing import List


class DegradationEventLogger:
    """
    Records every degradation event (optional/preferred tool skip)
    and surfaces patterns — which tools fail most and how often
    degradation affects result quality.
    """

    def __init__(self, max_records: int = 5000):
        self._records: List[dict] = []
        self._max = max_records

    def record(self, outcomes: List[ToolExecutionOutcome], task_type: str = "") -> None:
        for o in outcomes:
            if not o.success:
                if len(self._records) >= self._max:
                    self._records.pop(0)
                self._records.append({
                    "ts": time.time(),
                    "tool_name": o.tool_name,
                    "requirement_level": o.requirement_level.value,
                    "error": o.error,
                    "latency_ms": o.latency_ms,
                    "task_type": task_type,
                })

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [r for r in self._records if r["ts"] >= cutoff]
        tool_counts: dict = {}
        for r in recent:
            tool_counts[r["tool_name"]] = tool_counts.get(r["tool_name"], 0) + 1
        return {
            "window_seconds": window_seconds,
            "total_degradations": len(recent),
            "by_tool": tool_counts,
            "preferred_skips": sum(1 for r in recent if r["requirement_level"] == "preferred"),
            "optional_skips": sum(1 for r in recent if r["requirement_level"] == "optional"),
        }
```

## Comparison

| Approach | Required vs Optional | Fallback Values | Parallel Optional | Partial Result Report | Degradation Audit |
|---|---|---|---|---|---|
| ToolAvailabilitySpec | Yes (3 levels) | Yes | No | No | No |
| DegradationAwareToolExecutor | Yes | Yes (per spec) | No | No | No |
| PartialResultAssembler | Via outcomes | No | No | Yes | No |
| GracefulDegradationOrchestrator | Via executor | Via specs | Yes | No | No |
| DegradationPolicyRegistry | Yes (task-type) | No | No | No | No |
| DegradationEventLogger | No | No | No | No | Yes |

**Best for production**: Classify tools at registration time — never infer requirement level from error handling code. Set `ToolRequirementLevel.PREFERRED` for tools that noticeably degrade answer quality (e.g., a real-time price feed) and `OPTIONAL` for pure enrichment (e.g., a related-articles sidebar). Always include `skipped_notes` in the response to the user — "answer generated without live pricing data" is far better than a silent omission. Monitor `DegradationEventLogger.summary()` for tools with high skip rates: a preferred tool failing 30% of the time warrants an SLA conversation with the provider, not just a degradation policy.
