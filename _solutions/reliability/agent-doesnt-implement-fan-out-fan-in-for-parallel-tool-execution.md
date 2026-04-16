---
title: "Agent Doesn't Implement Fan-Out Fan-In for Parallel Tool Execution"
description: "Agents that execute tool calls sequentially in a loop pay the sum of all tool latencies per turn. When tools are independent, they can be launched in parallel and their results collected once all complete. Implement fan-out fan-in that dispatches multiple tool calls concurrently, enforces a per-turn timeout, handles partial failures gracefully, and merges results in deterministic order regardless of completion sequence."
date: 2026-04-16
difficulty: intermediate
category: reliability
slug: agent-doesnt-implement-fan-out-fan-in-for-parallel-tool-execution
tags: [fan-out, fan-in, parallel-tools, concurrent-execution, asyncio-gather, latency-reduction]
symptoms:
  - "Three independent tool calls execute sequentially: total latency = T1 + T2 + T3 instead of max(T1,T2,T3)"
  - "LLM requests two lookups simultaneously but the agent serializes them"
  - "No timeout on the combined tool batch — one slow tool stalls the entire turn"
  - "Tool results arrive out of order but the agent waits for sequential completion"
  - "Partial tool failures abort all other in-progress calls unnecessarily"
---

## Why This Happens

The standard tool-calling loop processes tool calls one at a time: call tool, get result, feed back to LLM, repeat. When the LLM requests multiple tools in a single response, they are often independent — the answer from tool A does not affect the arguments to tool B. Sequential execution wastes the overlap window. Fan-out fan-in uses `asyncio.gather` or equivalent to launch all independent tool calls at once and collect results when the slowest one finishes, reducing turn latency to the maximum of individual tool latencies rather than their sum.

## Solution 1: Tool Call Batch Definition

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional


class ToolCallStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


@dataclass
class ToolCallSpec:
    call_id: str
    tool_name: str
    args: Dict[str, Any]
    tool_fn: Callable
    timeout_seconds: Optional[float] = None   # overrides batch default


@dataclass
class ToolCallResult:
    call_id: str
    tool_name: str
    status: ToolCallStatus
    value: Any = None
    error: Optional[str] = None
    latency_ms: float = 0.0

    def succeeded(self) -> bool:
        return self.status == ToolCallStatus.SUCCEEDED

    def available(self) -> bool:
        return self.status == ToolCallStatus.SUCCEEDED and self.value is not None


@dataclass
class ToolBatchResult:
    results: List[ToolCallResult]
    total_latency_ms: float
    max_tool_latency_ms: float

    def succeeded_count(self) -> int:
        return sum(1 for r in self.results if r.succeeded())

    def failed_count(self) -> int:
        return len(self.results) - self.succeeded_count()

    def by_id(self) -> Dict[str, ToolCallResult]:
        return {r.call_id: r for r in self.results}
```

## Solution 2: Fan-Out Executor

```python
import asyncio
import time
from typing import List


class FanOutExecutor:
    """
    Launches all tool calls in a batch concurrently using asyncio.gather.
    Applies per-call and batch-level timeouts independently.
    Never lets one tool's failure cancel others.
    """

    def __init__(self, default_timeout_seconds: float = 30.0) -> None:
        self._default_timeout = default_timeout_seconds

    async def _execute_one(self, spec: ToolCallSpec) -> ToolCallResult:
        timeout = spec.timeout_seconds or self._default_timeout
        start = time.time()
        try:
            value = await asyncio.wait_for(
                spec.tool_fn(**spec.args),
                timeout=timeout,
            )
            latency = (time.time() - start) * 1000
            return ToolCallResult(
                call_id=spec.call_id,
                tool_name=spec.tool_name,
                status=ToolCallStatus.SUCCEEDED,
                value=value,
                latency_ms=round(latency, 2),
            )
        except asyncio.TimeoutError:
            latency = (time.time() - start) * 1000
            return ToolCallResult(
                call_id=spec.call_id,
                tool_name=spec.tool_name,
                status=ToolCallStatus.TIMED_OUT,
                error=f"Tool '{spec.tool_name}' timed out after {timeout}s",
                latency_ms=round(latency, 2),
            )
        except Exception as exc:
            latency = (time.time() - start) * 1000
            return ToolCallResult(
                call_id=spec.call_id,
                tool_name=spec.tool_name,
                status=ToolCallStatus.FAILED,
                error=str(exc)[:300],
                latency_ms=round(latency, 2),
            )

    async def execute_batch(
        self,
        specs: List[ToolCallSpec],
        batch_timeout_seconds: Optional[float] = None,
    ) -> List[ToolCallResult]:
        """
        Fan out: launch all specs concurrently.
        Returns results in the same order as input specs.
        """
        if not specs:
            return []

        tasks = [self._execute_one(spec) for spec in specs]

        if batch_timeout_seconds:
            try:
                results = await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=False),
                    timeout=batch_timeout_seconds,
                )
            except asyncio.TimeoutError:
                # Cancel all pending tasks and return timed-out results
                results = []
                for task in tasks:
                    if not task.done():
                        task.cancel()
                        results.append(None)
                    else:
                        results.append(task.result() if not task.exception() else None)
                return [
                    r or ToolCallResult(
                        call_id=specs[i].call_id,
                        tool_name=specs[i].tool_name,
                        status=ToolCallStatus.TIMED_OUT,
                        error=f"Batch timeout of {batch_timeout_seconds}s exceeded",
                    )
                    for i, r in enumerate(results)
                ]
        else:
            results = await asyncio.gather(*tasks, return_exceptions=False)

        return list(results)
```

## Solution 3: Fan-In Result Merger

```python
from typing import Any, Dict, List, Optional


class FanInResultMerger:
    """
    Collects fan-out results and merges them into a structured context
    ready to feed back to the LLM. Preserves call order, annotates
    failures, and produces a summary for LLM context injection.
    """

    def merge(
        self,
        results: List[ToolCallResult],
        include_errors: bool = True,
    ) -> Dict[str, Any]:
        merged: Dict[str, Any] = {}
        errors: Dict[str, str] = {}

        for result in results:
            if result.succeeded():
                merged[result.call_id] = result.value
            elif include_errors and result.error:
                errors[result.call_id] = f"{result.status.value}: {result.error}"

        return {"results": merged, "errors": errors}

    def to_llm_context(
        self,
        results: List[ToolCallResult],
        call_id_to_tool_name: Optional[Dict[str, str]] = None,
    ) -> str:
        """
        Formats merged results as a structured string for LLM context injection.
        """
        lines = []
        for result in results:
            label = result.tool_name
            if call_id_to_tool_name:
                label = call_id_to_tool_name.get(result.call_id, result.tool_name)

            if result.succeeded():
                lines.append(f"[{label}] {result.value}")
            else:
                lines.append(f"[{label}] ERROR: {result.error}")

        return "\n".join(lines)
```

## Solution 4: Fan-Out Fan-In Orchestrator

```python
import asyncio
import time
from typing import Any, Callable, Dict, List, Optional


class FanOutFanInOrchestrator:
    """
    High-level orchestrator that accepts a list of tool call specifications,
    executes them in parallel, collects results in order, and returns a
    merged context ready for LLM consumption.
    """

    def __init__(
        self,
        executor: FanOutExecutor,
        merger: FanInResultMerger,
        default_batch_timeout_seconds: float = 45.0,
    ) -> None:
        self._executor = executor
        self._merger = merger
        self._batch_timeout = default_batch_timeout_seconds
        self._batch_count = 0
        self._total_tools_executed = 0
        self._total_wall_ms = 0.0
        self._total_serial_ms = 0.0   # sum of individual latencies (for savings calc)

    async def run(
        self,
        specs: List[ToolCallSpec],
        batch_timeout_seconds: Optional[float] = None,
    ) -> ToolBatchResult:
        if not specs:
            return ToolBatchResult(results=[], total_latency_ms=0.0, max_tool_latency_ms=0.0)

        self._batch_count += 1
        self._total_tools_executed += len(specs)

        start = time.time()
        results = await self._executor.execute_batch(
            specs,
            batch_timeout_seconds=batch_timeout_seconds or self._batch_timeout,
        )
        wall_ms = (time.time() - start) * 1000
        serial_ms = sum(r.latency_ms for r in results)
        max_ms = max((r.latency_ms for r in results), default=0.0)

        self._total_wall_ms += wall_ms
        self._total_serial_ms += serial_ms

        return ToolBatchResult(
            results=results,
            total_latency_ms=round(wall_ms, 2),
            max_tool_latency_ms=round(max_ms, 2),
        )

    def latency_savings(self) -> dict:
        saved = max(0.0, self._total_serial_ms - self._total_wall_ms)
        return {
            "batches_executed": self._batch_count,
            "tools_executed": self._total_tools_executed,
            "total_wall_ms": round(self._total_wall_ms, 2),
            "total_serial_ms": round(self._total_serial_ms, 2),
            "latency_saved_ms": round(saved, 2),
            "savings_pct": round(saved / max(self._total_serial_ms, 1) * 100, 1),
        }
```

## Solution 5: Dependency-Aware Batch Planner

```python
from typing import Dict, List, Set


class DependencyAwareBatchPlanner:
    """
    Groups tool calls into sequential stages based on declared dependencies.
    Tools with no dependencies run in stage 0; tools that depend on stage 0
    results run in stage 1, and so on.
    Maximizes parallelism while respecting data flow constraints.
    """

    def __init__(self) -> None:
        self._dependencies: Dict[str, Set[str]] = {}   # call_id -> set of call_ids it depends on

    def declare_dependency(self, call_id: str, depends_on: str) -> None:
        self._dependencies.setdefault(call_id, set()).add(depends_on)

    def plan(self, specs: List[ToolCallSpec]) -> List[List[ToolCallSpec]]:
        """Returns a list of stages; each stage can be fan-out executed in parallel."""
        id_to_spec = {s.call_id: s for s in specs}
        remaining = set(id_to_spec.keys())
        completed: Set[str] = set()
        stages: List[List[ToolCallSpec]] = []

        while remaining:
            # Find all calls whose dependencies are satisfied
            ready = [
                cid for cid in remaining
                if self._dependencies.get(cid, set()).issubset(completed)
            ]
            if not ready:
                raise ValueError(f"Circular dependency detected among: {remaining}")

            stage = [id_to_spec[cid] for cid in ready]
            stages.append(stage)
            completed.update(ready)
            remaining -= set(ready)

        return stages
```

## Solution 6: Fan-Out Dashboard

```python
import time


class FanOutDashboard:
    """
    Reports fan-out execution stats including parallelism efficiency
    and latency savings versus serial execution.
    """

    def __init__(self, orchestrator: FanOutFanInOrchestrator) -> None:
        self._orchestrator = orchestrator

    def render(self) -> dict:
        savings = self._orchestrator.latency_savings()
        avg_tools_per_batch = round(
            savings["tools_executed"] / max(savings["batches_executed"], 1), 1
        )

        return {
            "generated_at": time.time(),
            "execution_stats": savings,
            "avg_tools_per_batch": avg_tools_per_batch,
            "parallelism_efficiency_pct": savings["savings_pct"],
        }
```

## Comparison

| Approach | Parallel Execution | Per-Call Timeout | Result Ordering | Dependency Planning | Savings Reporting |
|---|---|---|---|---|---|
| FanOutExecutor | Yes (asyncio.gather) | Yes | Yes (index-preserved) | No | No |
| FanInResultMerger | No | No | Yes | No | No |
| FanOutFanInOrchestrator | Via executor | Via executor | Via executor | No | Yes |
| DependencyAwareBatchPlanner | No | No | No | Yes (stage-based) | No |
| FanOutDashboard | No | No | No | No | Yes |

**Best for production**: Set a batch timeout slightly above P95 of your slowest tool — this ensures the fast tools are never held hostage to the slow one for more than one turn. Use `DependencyAwareBatchPlanner` only when tool arguments genuinely depend on prior results; most LLM tool calls are independent and can run in a single stage. Monitor `savings_pct`: below 20% means tools are mostly sequential (high dependencies) and the overhead of parallel dispatch may not be worth it; above 60% confirms the approach is delivering real latency reduction. Never assume independence — verify by checking whether any tool argument is constructed from another tool's response.
