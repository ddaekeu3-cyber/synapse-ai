---
title: "Agent Doesn't Implement Parallel Tool Execution with Dependency Graph"
description: "Agents that execute tool calls sequentially even when they are independent serialize work that could run in parallel — fetching user profile, weather data, and recent orders can all start simultaneously, but sequential execution makes users wait for the sum of all latencies instead of the maximum. Implement parallel tool execution with a dependency graph that executes independent tools concurrently and sequences only those tools whose inputs depend on prior outputs."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-parallel-tool-execution-with-dependency-graph
tags: [parallel-tool-execution, dependency-graph, concurrent-tools, dag-execution, latency-reduction, tool-orchestration]
symptoms:
  - "Five independent tool calls execute in sequence taking 5×500ms = 2.5s instead of 500ms"
  - "LLM requests all tools at once in one response but agent executes them one by one"
  - "No mechanism to express that tool B needs tool A's output while tool C is independent"
  - "Tool execution wall-clock time equals the sum of all tool latencies"
  - "Parallel tool calls supported by the LLM API are ignored by the execution layer"
---

## Why This Happens

When an LLM response includes multiple tool calls, agents commonly iterate over them and `await` each one before starting the next. This serializes independent work. Parallel execution requires checking which tool calls can start immediately (no unresolved dependencies) and which must wait for upstream results. The dependency graph makes this explicit: tools declare which context keys they read and which they write, and the executor fires all ready tools simultaneously.

## Solution 1: Tool Call Node

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, FrozenSet, List, Optional, Set


class NodeStatus(str, Enum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class ToolCallNode:
    node_id: str
    tool_name: str
    arguments: Dict[str, Any]
    depends_on: FrozenSet[str] = field(default_factory=frozenset)
    provides: FrozenSet[str] = field(default_factory=frozenset)
    status: NodeStatus = NodeStatus.PENDING
    result: Any = None
    error: Optional[str] = None
    started_at: Optional[float] = None
    ended_at: Optional[float] = None

    def duration_ms(self) -> Optional[float]:
        if self.started_at and self.ended_at:
            return round((self.ended_at - self.started_at) * 1000, 2)
        return None
```

## Solution 2: Dependency Graph Builder

```python
from typing import Dict, List, Set


class ToolDependencyGraph:
    """
    Builds an execution graph from a list of ToolCallNodes.
    Validates that:
    - No cycles exist
    - All declared dependencies are satisfiable by other nodes
    """

    def __init__(self, nodes: List[ToolCallNode]):
        self._nodes: Dict[str, ToolCallNode] = {n.node_id: n for n in nodes}
        self._validate()

    def _validate(self) -> None:
        provided: Dict[str, str] = {}   # key -> node_id that provides it
        for node in self._nodes.values():
            for key in node.provides:
                if key in provided:
                    raise ValueError(
                        f"Key '{key}' provided by both '{provided[key]}' and '{node.node_id}'"
                    )
                provided[key] = node.node_id

        # Check dependencies are satisfiable
        for node in self._nodes.values():
            for dep_key in node.depends_on:
                if dep_key not in provided:
                    raise ValueError(
                        f"Node '{node.node_id}' depends on '{dep_key}' "
                        f"but no node provides it"
                    )

    def ready_nodes(self, completed_keys: Set[str]) -> List[ToolCallNode]:
        """Returns nodes whose dependencies are all satisfied."""
        return [
            node for node in self._nodes.values()
            if node.status == NodeStatus.PENDING
            and node.depends_on.issubset(completed_keys)
        ]

    def all_nodes(self) -> List[ToolCallNode]:
        return list(self._nodes.values())

    def get(self, node_id: str) -> Optional[ToolCallNode]:
        return self._nodes.get(node_id)
```

## Solution 3: Parallel DAG Executor

```python
import asyncio
import time
from typing import Any, Callable, Dict, Set


class ParallelDAGExecutor:
    """
    Executes a ToolDependencyGraph in parallel topological order.
    Ready nodes (all dependencies satisfied) fire concurrently.
    Results are stored in a shared context dict keyed by `provides` keys.
    """

    def __init__(
        self,
        tool_registry: Dict[str, Callable],
        max_concurrency: int = 8,
    ):
        self._registry = tool_registry
        self._semaphore = asyncio.Semaphore(max_concurrency)

    async def execute(
        self,
        graph: ToolDependencyGraph,
        initial_context: Dict[str, Any] = None,
    ) -> Dict[str, Any]:
        context: Dict[str, Any] = dict(initial_context or {})
        completed_keys: Set[str] = set(context.keys())
        lock = asyncio.Lock()

        async def run_node(node: ToolCallNode) -> None:
            node.status = NodeStatus.RUNNING
            node.started_at = time.time()

            # Inject dependency values into arguments
            args = dict(node.arguments)
            async with lock:
                for key in node.depends_on:
                    if key in context:
                        args[key] = context[key]

            tool_fn = self._registry.get(node.tool_name)
            if tool_fn is None:
                node.status = NodeStatus.FAILED
                node.error = f"tool '{node.tool_name}' not registered"
                node.ended_at = time.time()
                return

            async with self._semaphore:
                try:
                    result = await tool_fn(**args)
                    node.result = result
                    node.status = NodeStatus.COMPLETED
                    node.ended_at = time.time()

                    # Publish provided keys to shared context
                    async with lock:
                        if isinstance(result, dict):
                            for key in node.provides:
                                if key in result:
                                    context[key] = result[key]
                                    completed_keys.add(key)
                        else:
                            for key in node.provides:
                                context[key] = result
                                completed_keys.add(key)
                except Exception as exc:
                    node.status = NodeStatus.FAILED
                    node.error = str(exc)[:200]
                    node.ended_at = time.time()

        # Iterative wave execution
        max_waves = len(graph.all_nodes()) + 1
        for _ in range(max_waves):
            async with lock:
                ready = graph.ready_nodes(completed_keys)
            if not ready:
                break
            for node in ready:
                node.status = NodeStatus.READY
            await asyncio.gather(*[run_node(node) for node in ready])
            async with lock:
                still_ready = graph.ready_nodes(completed_keys)
            if not still_ready and all(
                n.status in (NodeStatus.COMPLETED, NodeStatus.FAILED, NodeStatus.SKIPPED)
                for n in graph.all_nodes()
            ):
                break

        return context
```

## Solution 4: LLM Tool Call Parser

```python
import uuid
from typing import Any, Dict, List


class LLMToolCallParser:
    """
    Parses the tool_calls array from an LLM response into ToolCallNodes.
    Infers parallelism: all tool calls from the same LLM response turn
    are treated as independent (no inter-dependencies) by default.
    """

    @staticmethod
    def parse(
        llm_tool_calls: List[Dict[str, Any]],
        dependency_hints: Dict[str, List[str]] = None,
    ) -> List[ToolCallNode]:
        """
        llm_tool_calls: list of {"id": ..., "function": {"name": ..., "arguments": {...}}}
        dependency_hints: {tool_name: [keys_it_depends_on]}
        """
        hints = dependency_hints or {}
        nodes = []
        for tc in llm_tool_calls:
            fn = tc.get("function", tc)
            tool_name = fn.get("name", "")
            args = fn.get("arguments", {})
            if isinstance(args, str):
                import json
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}

            node = ToolCallNode(
                node_id=tc.get("id") or str(uuid.uuid4())[:8],
                tool_name=tool_name,
                arguments=args,
                depends_on=frozenset(hints.get(tool_name, [])),
                provides=frozenset([f"{tool_name}_result"]),
            )
            nodes.append(node)
        return nodes
```

## Solution 5: Execution Plan Visualizer

```python
from typing import List


class ExecutionPlanVisualizer:
    """
    Produces a human-readable execution plan showing which tools
    run in parallel and which are sequenced by dependencies.
    """

    def visualize(self, graph: ToolDependencyGraph) -> str:
        nodes = graph.all_nodes()
        # Group into waves
        completed_keys: set = set()
        waves = []
        remaining = [n for n in nodes]

        while remaining:
            wave = [n for n in remaining if n.depends_on.issubset(completed_keys)]
            if not wave:
                break
            waves.append(wave)
            for n in wave:
                completed_keys.update(n.provides)
                remaining.remove(n)

        lines = ["Execution Plan:"]
        for i, wave in enumerate(waves):
            tool_names = [n.tool_name for n in wave]
            parallel_note = "(parallel)" if len(wave) > 1 else "(sequential)"
            lines.append(f"  Wave {i+1} {parallel_note}: {', '.join(tool_names)}")

        return "\n".join(lines)
```

## Solution 6: DAG Execution Stats

```python
import time
from typing import List


class DAGExecutionStats:
    """
    Computes latency statistics from a completed DAG execution,
    including theoretical sequential time vs. actual parallel time.
    """

    def compute(self, graph: ToolDependencyGraph) -> dict:
        nodes = graph.all_nodes()
        completed = [n for n in nodes if n.status == NodeStatus.COMPLETED]
        if not completed:
            return {"error": "no completed nodes"}

        actual_start = min(n.started_at for n in completed)
        actual_end = max(n.ended_at for n in completed)
        actual_ms = (actual_end - actual_start) * 1000

        sequential_ms = sum(n.duration_ms() or 0 for n in completed)
        parallel_savings_ms = sequential_ms - actual_ms

        return {
            "node_count": len(nodes),
            "completed_count": len(completed),
            "failed_count": sum(1 for n in nodes if n.status == NodeStatus.FAILED),
            "actual_wall_clock_ms": round(actual_ms, 2),
            "sequential_equivalent_ms": round(sequential_ms, 2),
            "parallel_savings_ms": round(parallel_savings_ms, 2),
            "speedup_factor": round(sequential_ms / max(actual_ms, 1), 2),
            "per_node": [
                {"tool": n.tool_name, "ms": n.duration_ms(), "status": n.status.value}
                for n in nodes
            ],
        }
```

## Comparison

| Approach | Dependency Declaration | Parallel Execution | Context Propagation | LLM Integration | Stats |
|---|---|---|---|---|---|
| ToolDependencyGraph | Yes (provides/depends_on) | No | No | No | No |
| ParallelDAGExecutor | Via graph | Yes (wave-based) | Yes | No | No |
| LLMToolCallParser | No | No | No | Yes | No |
| ExecutionPlanVisualizer | Via graph | No | No | No | No |
| DAGExecutionStats | No | No | No | No | Yes |

**Best for production**: Treat all tool calls from a single LLM response turn as independent by default (LLMs do not currently express inter-tool dependencies). Use `dependency_hints` in `LLMToolCallParser` to encode the rare cases where tool B genuinely needs tool A's output — for example, a search tool followed by a summarize tool. Monitor `speedup_factor` from `DAGExecutionStats`: a factor of 1.0 means tools ran sequentially (check if dependency declarations are too conservative); a factor near N means N tools ran fully in parallel. The `parallel_savings_ms` is the wall-clock time saved per session — multiply by session volume to compute the aggregate latency improvement.
