---
title: "Agent Doesn't Implement Parallel Tool Call Execution"
description: "Agents that execute tool calls sequentially waste wall-clock time when multiple tools are independent: a web search, a database lookup, and a knowledge-base retrieval that share no data dependencies are run one after another instead of concurrently. Implement parallel tool call execution with dependency analysis, concurrency limits, and partial-result handling so independent tools run simultaneously."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-parallel-tool-call-execution
tags: [parallel-execution, tool-concurrency, async-tools, latency-reduction, dependency-graph, asyncio]
symptoms:
  - "Agent takes 10 seconds to run three 3-second tool calls that have no dependencies"
  - "Tool calls are always executed in the order they appear in the LLM response"
  - "No analysis of which tool calls can be parallelized before execution begins"
  - "Concurrency limit is unbounded — all tools fire at once, overwhelming downstream APIs"
  - "A single slow tool blocks all other results from being delivered to the context"
---

## Why This Happens

LLMs often return multiple tool calls in a single response. Most agent frameworks iterate over the list and await each call in sequence, accumulating results into a list. When tool calls are independent — no tool's input depends on another tool's output — sequential execution adds the latencies instead of taking the maximum. Parallel execution requires identifying independence (no shared input variables), managing concurrency to avoid rate-limiting downstream services, and assembling partial results that arrive out of order back into a coherent context.

## Solution 1: Tool Call Node

```python
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set


@dataclass
class ToolCallNode:
    call_id: str
    tool_name: str
    arguments: Dict[str, Any]
    depends_on: Set[str] = field(default_factory=set)   # call_ids this depends on
    result: Optional[Any] = None
    error: Optional[str] = None
    started_at: Optional[float] = None
    finished_at: Optional[float] = None

    def latency_ms(self) -> Optional[float]:
        if self.started_at and self.finished_at:
            return round((self.finished_at - self.started_at) * 1000, 2)
        return None

    def is_complete(self) -> bool:
        return self.result is not None or self.error is not None
```

## Solution 2: Tool Call Dependency Analyzer

```python
import re
from typing import Dict, List, Set


class ToolCallDependencyAnalyzer:
    """
    Determines which tool calls are independent by checking whether
    any argument value references the output of another call.
    Uses a simple placeholder pattern: {{call_id.field}}.
    """

    REFERENCE_PATTERN = re.compile(r"\{\{([a-zA-Z0-9_\-]+)\.")

    def analyze(self, nodes: List[ToolCallNode]) -> List[ToolCallNode]:
        """
        Populates depends_on for each node by scanning argument values
        for references to other call IDs.
        """
        call_ids = {n.call_id for n in nodes}
        for node in nodes:
            refs = self._extract_refs(node.arguments)
            node.depends_on = refs & call_ids  # only refs to known call IDs
        return nodes

    def _extract_refs(self, obj: object) -> Set[str]:
        refs: Set[str] = set()
        if isinstance(obj, str):
            for match in self.REFERENCE_PATTERN.finditer(obj):
                refs.add(match.group(1))
        elif isinstance(obj, dict):
            for v in obj.values():
                refs |= self._extract_refs(v)
        elif isinstance(obj, list):
            for item in obj:
                refs |= self._extract_refs(item)
        return refs

    def independent_groups(
        self, nodes: List[ToolCallNode]
    ) -> List[List[ToolCallNode]]:
        """
        Returns nodes grouped into sequential waves where all nodes
        within a wave can be executed in parallel.
        """
        remaining = {n.call_id: n for n in nodes}
        completed: Set[str] = set()
        waves: List[List[ToolCallNode]] = []

        while remaining:
            wave = [
                n for n in remaining.values()
                if n.depends_on <= completed
            ]
            if not wave:
                # circular or unresolvable — run remaining sequentially
                wave = list(remaining.values())
            waves.append(wave)
            for n in wave:
                completed.add(n.call_id)
                del remaining[n.call_id]
        return waves
```

## Solution 3: Parallel Tool Executor

```python
import asyncio
import time
from typing import Any, Callable, Dict, List, Optional


class ParallelToolExecutor:
    """
    Executes tool call nodes in parallel within each dependency wave.
    Respects a concurrency limit using asyncio.Semaphore to avoid
    overwhelming downstream services.
    """

    def __init__(
        self,
        max_concurrency: int = 5,
        per_tool_timeout_seconds: float = 30.0,
    ):
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._timeout = per_tool_timeout_seconds

    async def execute_wave(
        self,
        wave: List[ToolCallNode],
        dispatch_fn: Callable[[ToolCallNode], Any],
    ) -> List[ToolCallNode]:
        tasks = [self._run_node(node, dispatch_fn) for node in wave]
        return await asyncio.gather(*tasks, return_exceptions=False)

    async def _run_node(
        self,
        node: ToolCallNode,
        dispatch_fn: Callable[[ToolCallNode], Any],
    ) -> ToolCallNode:
        async with self._semaphore:
            node.started_at = time.time()
            try:
                result = await asyncio.wait_for(
                    dispatch_fn(node),
                    timeout=self._timeout,
                )
                node.result = result
            except asyncio.TimeoutError:
                node.error = f"timeout after {self._timeout}s"
            except Exception as exc:
                node.error = str(exc)
            finally:
                node.finished_at = time.time()
        return node

    async def execute_all(
        self,
        waves: List[List[ToolCallNode]],
        dispatch_fn: Callable[[ToolCallNode], Any],
    ) -> List[ToolCallNode]:
        all_nodes: List[ToolCallNode] = []
        for wave in waves:
            completed = await self.execute_wave(wave, dispatch_fn)
            all_nodes.extend(completed)
        return all_nodes
```

## Solution 4: Partial Result Assembler

```python
from typing import Any, Dict, List, Optional


class PartialResultAssembler:
    """
    Collects tool call results — including partial failures — and
    assembles them into an ordered context payload. Nodes with errors
    are included with an error marker so the LLM can reason about
    which tools failed.
    """

    def __init__(self, include_errors_in_context: bool = True):
        self._include_errors = include_errors_in_context

    def assemble(self, nodes: List[ToolCallNode]) -> List[Dict[str, Any]]:
        context_items = []
        for node in nodes:
            if node.error:
                if self._include_errors:
                    context_items.append({
                        "tool_call_id": node.call_id,
                        "tool_name": node.tool_name,
                        "status": "error",
                        "error": node.error,
                        "latency_ms": node.latency_ms(),
                    })
            else:
                context_items.append({
                    "tool_call_id": node.call_id,
                    "tool_name": node.tool_name,
                    "status": "ok",
                    "result": node.result,
                    "latency_ms": node.latency_ms(),
                })
        return context_items

    def success_rate(self, nodes: List[ToolCallNode]) -> float:
        if not nodes:
            return 1.0
        successes = sum(1 for n in nodes if n.error is None and n.is_complete())
        return successes / len(nodes)
```

## Solution 5: Parallel Tool Call Pipeline

```python
import time
from typing import Any, Callable, Dict, List


class ParallelToolCallPipeline:
    """
    Orchestrates the full parallel tool call flow:
    parse → analyze → group into waves → execute in parallel → assemble.
    """

    def __init__(
        self,
        analyzer: ToolCallDependencyAnalyzer,
        executor: ParallelToolExecutor,
        assembler: PartialResultAssembler,
    ):
        self._analyzer = analyzer
        self._executor = executor
        self._assembler = assembler

    async def run(
        self,
        raw_tool_calls: List[Dict[str, Any]],
        dispatch_fn: Callable[[ToolCallNode], Any],
    ) -> dict:
        nodes = [
            ToolCallNode(
                call_id=tc.get("id", f"call_{i}"),
                tool_name=tc["name"],
                arguments=tc.get("arguments", {}),
            )
            for i, tc in enumerate(raw_tool_calls)
        ]

        self._analyzer.analyze(nodes)
        waves = self._analyzer.independent_groups(nodes)

        wall_start = time.time()
        completed = await self._executor.execute_all(waves, dispatch_fn)
        wall_ms = round((time.time() - wall_start) * 1000, 2)

        sequential_ms = sum(
            n.latency_ms() or 0 for n in completed
        )

        context = self._assembler.assemble(completed)
        return {
            "context_items": context,
            "wave_count": len(waves),
            "tool_count": len(completed),
            "wall_time_ms": wall_ms,
            "sequential_time_ms": round(sequential_ms, 2),
            "time_saved_ms": round(sequential_ms - wall_ms, 2),
            "success_rate": self._assembler.success_rate(completed),
        }
```

## Solution 6: Parallelism Savings Monitor

```python
import time
from typing import List


class ParallelismSavingsMonitor:
    """
    Accumulates pipeline run reports and surfaces aggregate
    time-savings from parallel execution over time.
    """

    def __init__(self):
        self._reports: List[dict] = []
        self._recorded_at: List[float] = []

    def record(self, report: dict) -> None:
        self._reports.append(report)
        self._recorded_at.append(time.time())

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [
            r for r, ts in zip(self._reports, self._recorded_at)
            if ts >= cutoff
        ]
        if not recent:
            return {"window_seconds": window_seconds, "runs": 0}

        total_saved = sum(r.get("time_saved_ms", 0) for r in recent)
        total_wall = sum(r.get("wall_time_ms", 0) for r in recent)
        avg_waves = sum(r.get("wave_count", 1) for r in recent) / len(recent)

        return {
            "window_seconds": window_seconds,
            "runs": len(recent),
            "total_time_saved_ms": round(total_saved, 2),
            "avg_time_saved_per_run_ms": round(total_saved / len(recent), 2),
            "avg_wave_count": round(avg_waves, 2),
            "speedup_pct": round(total_saved / max(total_wall + total_saved, 1) * 100, 1),
        }
```

## Comparison

| Approach | Dependency Analysis | Wave Grouping | Concurrent Execution | Partial Failures | Savings Tracking |
|---|---|---|---|---|---|
| ToolCallDependencyAnalyzer | Yes (reference scan) | Yes (waves) | No | No | No |
| ParallelToolExecutor | No | No | Yes (semaphore) | Yes | No |
| PartialResultAssembler | No | No | No | Yes (error marker) | No |
| ParallelToolCallPipeline | Via analyzer | Via analyzer | Via executor | Via assembler | No |
| ParallelismSavingsMonitor | No | No | No | No | Yes |

**Best for production**: Set `max_concurrency=5` as a safe default — this prevents a burst of 20 tool calls from issuing 20 simultaneous HTTP requests to the same upstream API. Use `{{call_id.field}}` reference syntax in tool argument templates so dependency analysis is explicit rather than heuristic. Monitor `speedup_pct` from `ParallelismSavingsMonitor`: if consistently below 10%, the LLM is not issuing multi-tool responses and parallelism provides no benefit — investigate whether the prompt encourages multi-tool batching. A `wave_count=1` on every run means all tools are independent and can always be parallelized.
