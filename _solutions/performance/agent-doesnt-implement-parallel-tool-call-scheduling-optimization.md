---
title: "Agent Doesn't Implement Parallel Tool Call Scheduling Optimization"
description: "Agents that execute tool calls sequentially even when they are independent waste wall-clock time proportional to the number of tools called. A response that requires five independent lookups taking 200ms each takes 1,000ms sequentially but only 200ms in parallel. Implement parallel tool call scheduling that detects independence between tool calls, dispatches independent groups concurrently, and serializes only calls with explicit data dependencies."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-parallel-tool-call-scheduling-optimization
tags: [parallel-tool-calls, scheduling-optimization, concurrency, dependency-analysis, wall-clock-latency, tool-parallelism]
symptoms:
  - "Five independent database lookups execute sequentially, taking 5× longer than necessary"
  - "Agent wall-clock latency is dominated by serialized tool calls that have no dependencies"
  - "Tool calls that write then read are executed in the wrong order because scheduling is naive"
  - "No grouping of tool calls by independence before dispatch"
  - "LLM waits for all sequential tool results before generating the next response"
---

## Why This Happens

Sequential tool execution is the default because it requires no dependency analysis. The agent loop runs each tool call, waits for the result, then proceeds to the next. Parallelization requires identifying which calls are independent (no shared input/output) and dispatching them as a group. Dependency detection can be as simple as checking whether a call's arguments reference the output of a prior call, or as sophisticated as a static analysis of argument patterns. Even a conservative heuristic — parallelize all calls in the same LLM response — significantly reduces latency for the common case where the LLM emits multiple independent tool calls.

## Solution 1: Tool Call Dependency Analyzer

```python
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set


@dataclass
class ToolCallNode:
    call_id: str
    tool_name: str
    args: Any
    depends_on: Set[str] = field(default_factory=set)  # call_ids this depends on

    def has_dependencies(self) -> bool:
        return len(self.depends_on) > 0


class ToolCallDependencyAnalyzer:
    """
    Analyzes tool call arguments to detect data dependencies between calls.
    A call B depends on call A if B's arguments contain a reference to A's
    call_id or a known result-reference pattern.
    """

    _RESULT_REF_PATTERN = re.compile(r"\{\{result:([a-zA-Z0-9_\-]+)\}\}")

    def analyze(self, calls: List[ToolCallNode]) -> List[ToolCallNode]:
        """
        Returns the same calls annotated with dependency sets.
        Detects {{result:call_id}} references in string arguments.
        """
        call_ids = {c.call_id for c in calls}
        for call in calls:
            deps = self._extract_refs(call.args)
            call.depends_on = deps & call_ids
        return calls

    def _extract_refs(self, args: Any) -> Set[str]:
        refs = set()
        if isinstance(args, str):
            for match in self._RESULT_REF_PATTERN.finditer(args):
                refs.add(match.group(1))
        elif isinstance(args, dict):
            for v in args.values():
                refs |= self._extract_refs(v)
        elif isinstance(args, list):
            for item in args:
                refs |= self._extract_refs(item)
        return refs
```

## Solution 2: Execution Wave Planner

```python
from typing import Dict, List, Set


class ExecutionWavePlanner:
    """
    Groups tool calls into sequential waves where all calls within a wave
    are independent of each other and can be executed in parallel.
    Calls in wave N may depend on calls from waves 0..N-1.
    """

    def plan(self, calls: List[ToolCallNode]) -> List[List[ToolCallNode]]:
        """
        Returns a list of waves. Each wave is a list of independent calls.
        """
        remaining = list(calls)
        completed: Set[str] = set()
        waves: List[List[ToolCallNode]] = []

        while remaining:
            wave = [
                c for c in remaining
                if c.depends_on.issubset(completed)
            ]
            if not wave:
                # Circular dependency or unresolvable — add all remaining as one wave
                waves.append(remaining)
                break
            waves.append(wave)
            wave_ids = {c.call_id for c in wave}
            completed |= wave_ids
            remaining = [c for c in remaining if c.call_id not in wave_ids]

        return waves

    def parallelism_ratio(self, calls: List[ToolCallNode], waves: List[List[ToolCallNode]]) -> float:
        """
        Ratio of sequential steps (waves) to total calls.
        Lower = more parallelism. 1.0 = fully sequential.
        """
        if not calls:
            return 1.0
        return round(len(waves) / len(calls), 4)
```

## Solution 3: Parallel Wave Executor

```python
import asyncio
import time
from typing import Any, Callable, Dict, List


class WaveExecutionResult:
    def __init__(self):
        self.results: Dict[str, Any] = {}
        self.errors: Dict[str, Exception] = {}
        self.wave_durations_ms: List[float] = []
        self.total_duration_ms: float = 0.0


class ParallelWaveExecutor:
    """
    Executes tool call waves in sequence, dispatching all calls
    within each wave in parallel using asyncio.gather.
    """

    def __init__(self, max_concurrency: int = 10):
        self._semaphore = asyncio.Semaphore(max_concurrency)

    async def _execute_call(
        self,
        call: ToolCallNode,
        dispatch_fn: Callable,
        resolved_args: Any,
    ) -> tuple:
        async with self._semaphore:
            try:
                result = await dispatch_fn(call.tool_name, resolved_args)
                return call.call_id, result, None
            except Exception as exc:
                return call.call_id, None, exc

    def _resolve_args(self, args: Any, results: Dict[str, Any]) -> Any:
        """Substitute {{result:call_id}} references with actual results."""
        import re
        if isinstance(args, str):
            def replace(m):
                cid = m.group(1)
                val = results.get(cid, m.group(0))
                return str(val)
            return re.sub(r"\{\{result:([a-zA-Z0-9_\-]+)\}\}", replace, args)
        elif isinstance(args, dict):
            return {k: self._resolve_args(v, results) for k, v in args.items()}
        elif isinstance(args, list):
            return [self._resolve_args(item, results) for item in args]
        return args

    async def execute(
        self,
        waves: List[List[ToolCallNode]],
        dispatch_fn: Callable,
    ) -> WaveExecutionResult:
        outcome = WaveExecutionResult()
        total_start = time.time()

        for wave in waves:
            wave_start = time.time()
            tasks = [
                self._execute_call(
                    call,
                    dispatch_fn,
                    self._resolve_args(call.args, outcome.results),
                )
                for call in wave
            ]
            wave_results = await asyncio.gather(*tasks)
            for call_id, result, error in wave_results:
                if error:
                    outcome.errors[call_id] = error
                else:
                    outcome.results[call_id] = result
            outcome.wave_durations_ms.append(
                round((time.time() - wave_start) * 1000, 2)
            )

        outcome.total_duration_ms = round((time.time() - total_start) * 1000, 2)
        return outcome
```

## Solution 4: Scheduling Savings Estimator

```python
from typing import Dict, List


class SchedulingSavingsEstimator:
    """
    Estimates the latency saved by parallel scheduling compared to
    sequential execution, given per-call duration estimates.
    """

    def estimate(
        self,
        waves: List[List[ToolCallNode]],
        durations_ms: Dict[str, float],
        default_duration_ms: float = 300.0,
    ) -> dict:
        # Sequential: sum of all call durations
        all_calls = [c for wave in waves for c in wave]
        sequential_ms = sum(
            durations_ms.get(c.call_id, default_duration_ms) for c in all_calls
        )

        # Parallel: sum of max duration per wave
        parallel_ms = sum(
            max(
                (durations_ms.get(c.call_id, default_duration_ms) for c in wave),
                default=0.0,
            )
            for wave in waves
        )

        saved_ms = sequential_ms - parallel_ms
        speedup = round(sequential_ms / max(parallel_ms, 1), 2)

        return {
            "sequential_ms": round(sequential_ms, 1),
            "parallel_ms": round(parallel_ms, 1),
            "saved_ms": round(saved_ms, 1),
            "speedup_factor": speedup,
            "wave_count": len(waves),
            "call_count": len(all_calls),
        }
```

## Solution 5: Parallel Scheduler Pipeline

```python
from typing import Any, Callable, List


class ParallelToolCallSchedulerPipeline:
    """
    Full pipeline: analyze dependencies → plan waves → execute in parallel.
    """

    def __init__(
        self,
        analyzer: ToolCallDependencyAnalyzer,
        planner: ExecutionWavePlanner,
        executor: ParallelWaveExecutor,
    ):
        self._analyzer = analyzer
        self._planner = planner
        self._executor = executor

    async def run(
        self,
        calls: List[ToolCallNode],
        dispatch_fn: Callable,
    ) -> dict:
        annotated = self._analyzer.analyze(calls)
        waves = self._planner.plan(annotated)
        result = await self._executor.execute(waves, dispatch_fn)
        return {
            "results": result.results,
            "errors": {k: str(v) for k, v in result.errors.items()},
            "wave_count": len(waves),
            "wave_durations_ms": result.wave_durations_ms,
            "total_duration_ms": result.total_duration_ms,
            "parallelism_ratio": self._planner.parallelism_ratio(annotated, waves),
        }
```

## Solution 6: Scheduling Efficiency Dashboard

```python
import time
from collections import deque
from threading import Lock
from typing import Deque


class SchedulingEfficiencyDashboard:
    """
    Tracks scheduling execution reports and surfaces aggregate
    parallelism statistics for optimization decisions.
    """

    def __init__(self, max_records: int = 10_000):
        self._records: Deque[dict] = deque(maxlen=max_records)
        self._lock = Lock()

    def record(self, pipeline_result: dict) -> None:
        with self._lock:
            self._records.append({**pipeline_result, "recorded_at": time.time()})

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        with self._lock:
            recent = [r for r in self._records if r.get("recorded_at", 0) >= cutoff]
        if not recent:
            return {"requests": 0}
        avg_ratio = sum(r.get("parallelism_ratio", 1.0) for r in recent) / len(recent)
        avg_waves = sum(r.get("wave_count", 1) for r in recent) / len(recent)
        return {
            "requests": len(recent),
            "avg_parallelism_ratio": round(avg_ratio, 4),
            "avg_wave_count": round(avg_waves, 2),
            "fully_sequential_pct": round(
                sum(1 for r in recent if r.get("parallelism_ratio", 1.0) >= 0.99) / len(recent) * 100, 1
            ),
        }
```

## Comparison

| Approach | Dependency Detection | Wave Planning | Parallel Dispatch | Speedup Estimation | Monitoring |
|---|---|---|---|---|---|
| ToolCallDependencyAnalyzer | Yes (ref pattern) | No | No | No | No |
| ExecutionWavePlanner | Via analyzer | Yes | No | No | No |
| ParallelWaveExecutor | No | Via planner | Yes (asyncio) | No | No |
| SchedulingSavingsEstimator | No | No | No | Yes | No |
| ParallelToolCallSchedulerPipeline | Via analyzer | Via planner | Via executor | No | No |
| SchedulingEfficiencyDashboard | No | No | No | No | Yes |

**Best for production**: Apply conservative parallelism by default — all tool calls in a single LLM response that use no `{{result:...}}` references are safe to parallelize. Use `max_concurrency=5` as the semaphore ceiling to prevent thundering herd against a single backend. Monitor `avg_parallelism_ratio`: values above 0.7 mean most tool call groups are nearly sequential — this suggests the LLM's tool call patterns should be reviewed and tool designs reconsidered. A ratio below 0.3 means the scheduler is finding good parallelism opportunities.
