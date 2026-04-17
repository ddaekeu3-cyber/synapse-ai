---
title: "Agent Doesn't Implement Parallel Tool Call Execution"
description: "Agents that execute tool calls sequentially when the LLM requests multiple independent tools in one turn waste wall-clock time proportional to the number of tools — three tools that each take 500ms take 1500ms sequentially but only 500ms in parallel. Implement parallel tool call execution that detects independent tool calls in a single LLM response and dispatches them concurrently using asyncio.gather, with per-tool timeouts and partial failure handling."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-parallel-tool-call-execution
tags: [parallel-execution, tool-calls, asyncio, concurrency, latency-reduction, multi-tool]
symptoms:
  - "Three simultaneous tool calls take 3× the time of one call — executed sequentially"
  - "LLM returns multiple tool_use blocks but agent loops over them one at a time"
  - "No fan-out logic for independent tool calls in the same LLM turn"
  - "Total agent response time scales linearly with number of tool calls regardless of dependencies"
  - "Parallel execution attempted but one slow tool blocks all others from returning"
---

## Why This Happens

Most agentic frameworks implement a simple loop: receive tool calls from the LLM, execute them one by one, collect results, send back. This is correct for dependent tool calls (where tool B needs tool A's output) but wasteful for independent calls (where A and B can run simultaneously). The LLM often returns multiple tool_use blocks in a single response when it determines that several actions can be taken in parallel. Without a dispatcher that identifies independent calls and uses asyncio.gather, sequential execution is the default and the performance loss scales with the number of parallel tool calls the model recommends.

## Solution 1: Tool Call Descriptor

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class ToolCallStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    TIMEOUT = "timeout"


@dataclass
class ToolCallDescriptor:
    call_id: str
    tool_name: str
    arguments: Dict[str, Any]
    dependencies: List[str] = field(default_factory=list)  # call_ids this depends on
    timeout_seconds: float = 30.0
    status: ToolCallStatus = ToolCallStatus.PENDING
    result: Optional[Any] = None
    error: Optional[str] = None
    started_at: Optional[float] = None
    finished_at: Optional[float] = None

    def duration_ms(self) -> Optional[float]:
        if self.started_at and self.finished_at:
            return round((self.finished_at - self.started_at) * 1000, 2)
        return None
```

## Solution 2: Dependency Analyzer

```python
from typing import Dict, List, Set


class ToolCallDependencyAnalyzer:
    """
    Analyzes a list of tool call descriptors to identify which calls
    can run in parallel (no declared dependencies between them).
    Returns execution waves: each wave can be executed concurrently.
    """

    def compute_waves(
        self,
        calls: List[ToolCallDescriptor],
    ) -> List[List[ToolCallDescriptor]]:
        """
        Returns a list of waves. All calls in a wave are independent
        and can be dispatched in parallel. Waves are ordered so that
        calls with declared dependencies run after their dependencies complete.
        """
        if not calls:
            return []

        call_map: Dict[str, ToolCallDescriptor] = {c.call_id: c for c in calls}
        in_degree: Dict[str, int] = {c.call_id: 0 for c in calls}
        dependents: Dict[str, List[str]] = {c.call_id: [] for c in calls}

        for call in calls:
            for dep_id in call.dependencies:
                if dep_id in call_map:
                    in_degree[call.call_id] += 1
                    dependents[dep_id].append(call.call_id)

        waves = []
        ready = [c for c in calls if in_degree[c.call_id] == 0]

        while ready:
            waves.append(list(ready))
            next_ready = []
            for call in ready:
                for dep_id in dependents[call.call_id]:
                    in_degree[dep_id] -= 1
                    if in_degree[dep_id] == 0:
                        next_ready.append(call_map[dep_id])
            ready = next_ready

        return waves

    def independent_calls(
        self,
        calls: List[ToolCallDescriptor],
    ) -> List[ToolCallDescriptor]:
        """Return only calls with no dependencies (suitable for immediate parallel dispatch)."""
        return [c for c in calls if not c.dependencies]
```

## Solution 3: Parallel Tool Dispatcher

```python
import asyncio
import time
from typing import Any, Callable, Dict, List


class ParallelToolDispatcher:
    """
    Executes independent tool calls concurrently using asyncio.gather.
    Applies per-tool timeouts and collects partial results on failure.
    """

    def __init__(
        self,
        tool_registry: Dict[str, Callable],
        default_timeout_seconds: float = 30.0,
        max_concurrent: int = 10,
    ):
        self._registry = tool_registry
        self._default_timeout = default_timeout_seconds
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._total_calls = 0
        self._parallel_batches = 0

    async def _execute_one(self, call: ToolCallDescriptor) -> ToolCallDescriptor:
        call.status = ToolCallStatus.RUNNING
        call.started_at = time.time()

        tool_fn = self._registry.get(call.tool_name)
        if tool_fn is None:
            call.status = ToolCallStatus.FAILED
            call.error = f"tool '{call.tool_name}' not registered"
            call.finished_at = time.time()
            return call

        timeout = call.timeout_seconds or self._default_timeout
        async with self._semaphore:
            try:
                call.result = await asyncio.wait_for(
                    tool_fn(**call.arguments),
                    timeout=timeout,
                )
                call.status = ToolCallStatus.SUCCESS
            except asyncio.TimeoutError:
                call.status = ToolCallStatus.TIMEOUT
                call.error = f"timed out after {timeout}s"
            except Exception as exc:
                call.status = ToolCallStatus.FAILED
                call.error = str(exc)
            finally:
                call.finished_at = time.time()

        return call

    async def dispatch_parallel(
        self,
        calls: List[ToolCallDescriptor],
    ) -> List[ToolCallDescriptor]:
        """Execute all calls concurrently, returning results in input order."""
        if not calls:
            return []
        self._total_calls += len(calls)
        self._parallel_batches += 1
        results = await asyncio.gather(
            *[self._execute_one(call) for call in calls],
            return_exceptions=False,
        )
        return list(results)

    async def dispatch_waves(
        self,
        waves: List[List[ToolCallDescriptor]],
    ) -> List[ToolCallDescriptor]:
        """Execute wave-by-wave, parallelizing within each wave."""
        all_results = []
        for wave in waves:
            wave_results = await self.dispatch_parallel(wave)
            all_results.extend(wave_results)
        return all_results

    def stats(self) -> dict:
        return {
            "total_calls": self._total_calls,
            "parallel_batches": self._parallel_batches,
            "avg_calls_per_batch": round(
                self._total_calls / max(self._parallel_batches, 1), 2
            ),
        }
```

## Solution 4: Execution Timing Report

```python
import time
from typing import List


class ParallelExecutionTimingReport:
    """
    Computes the wall-clock savings from parallel vs sequential execution
    for a set of completed tool call descriptors.
    """

    def compute(self, calls: List[ToolCallDescriptor]) -> dict:
        completed = [c for c in calls if c.duration_ms() is not None]
        if not completed:
            return {"calls": 0}

        durations = [c.duration_ms() for c in completed]
        sequential_ms = sum(durations)

        # Wall clock = max duration in the parallel batch
        wall_clock_ms = max(durations) if durations else 0.0

        return {
            "call_count": len(completed),
            "sequential_estimate_ms": round(sequential_ms, 2),
            "parallel_wall_clock_ms": round(wall_clock_ms, 2),
            "time_saved_ms": round(sequential_ms - wall_clock_ms, 2),
            "speedup_factor": round(sequential_ms / max(wall_clock_ms, 1), 2),
            "success_count": sum(1 for c in completed if c.status == ToolCallStatus.SUCCESS),
            "failure_count": sum(1 for c in completed if c.status in (ToolCallStatus.FAILED, ToolCallStatus.TIMEOUT)),
            "per_call": [
                {
                    "call_id": c.call_id,
                    "tool_name": c.tool_name,
                    "status": c.status.value,
                    "duration_ms": c.duration_ms(),
                }
                for c in completed
            ],
        }
```

## Solution 5: Partial Failure Handler

```python
from typing import Any, Dict, List, Optional


class PartialToolFailureHandler:
    """
    Processes a mixed list of successful and failed tool call results.
    Surfaces failed results as structured error messages safe for
    injection into the LLM context alongside successful results.
    """

    def __init__(self, fail_fast_on_required: bool = False):
        self._fail_fast = fail_fast_on_required

    def process(
        self,
        calls: List[ToolCallDescriptor],
        required_tool_names: set = None,
    ) -> dict:
        required = required_tool_names or set()
        successes = []
        failures = []

        for call in calls:
            if call.status == ToolCallStatus.SUCCESS:
                successes.append({
                    "call_id": call.call_id,
                    "tool_name": call.tool_name,
                    "result": call.result,
                })
            else:
                failures.append({
                    "call_id": call.call_id,
                    "tool_name": call.tool_name,
                    "error": call.error,
                    "status": call.status.value,
                    "context_message": (
                        f"Tool '{call.tool_name}' failed: {call.error}. "
                        f"The result for this tool is unavailable."
                    ),
                })

        has_required_failure = any(
            f["tool_name"] in required for f in failures
        )

        return {
            "all_succeeded": len(failures) == 0,
            "has_required_failure": has_required_failure,
            "successes": successes,
            "failures": failures,
            "context_for_llm": [s["result"] for s in successes] + [
                f["context_message"] for f in failures
            ],
        }
```

## Solution 6: Parallel Tool Execution Dashboard

```python
import time
from typing import List


class ParallelToolExecutionDashboard:
    """
    Aggregates timing reports across multiple parallel dispatch batches
    to surface cumulative speedup and per-tool failure rates.
    """

    def __init__(self, dispatcher: ParallelToolDispatcher):
        self._dispatcher = dispatcher
        self._reports: List[dict] = []

    def record(self, report: dict) -> None:
        self._reports.append(report)

    def render(self) -> dict:
        total_saved = sum(r.get("time_saved_ms", 0) for r in self._reports)
        total_calls = sum(r.get("call_count", 0) for r in self._reports)
        total_failures = sum(r.get("failure_count", 0) for r in self._reports)
        return {
            "generated_at": time.time(),
            "dispatcher_stats": self._dispatcher.stats(),
            "aggregate": {
                "total_calls": total_calls,
                "total_time_saved_ms": round(total_saved, 2),
                "total_failures": total_failures,
                "failure_rate": round(total_failures / max(total_calls, 1), 4),
            },
        }
```

## Comparison

| Approach | Dependency Analysis | Parallel Dispatch | Timeout per Tool | Partial Failure | Timing Report |
|---|---|---|---|---|---|
| ToolCallDependencyAnalyzer | Yes (wave computation) | No | No | No | No |
| ParallelToolDispatcher | No | Yes (asyncio.gather) | Yes (per-call) | Partial | No |
| ParallelExecutionTimingReport | No | No | No | No | Yes |
| PartialToolFailureHandler | No | No | No | Yes (LLM-safe) | No |
| ParallelToolExecutionDashboard | No | No | No | No | Aggregate |

**Best for production**: Default to parallel execution for all tool calls in a single LLM turn unless explicit dependencies are declared — most multi-tool LLM responses are independent. Set `max_concurrent=10` as the semaphore limit to prevent tool fan-out from overwhelming downstream APIs. Use `ToolCallDependencyAnalyzer.compute_waves()` only when your agentic framework supports declared inter-tool dependencies; for most agents, simple parallel dispatch of all calls in a turn is sufficient and correct. Monitor `speedup_factor` in `ParallelExecutionTimingReport`: consistently below 1.5× suggests tools are not truly independent or are being serialized by a shared resource (e.g., a rate-limited API).
