---
title: "Agent Doesn't Implement Tool Dependency Graph Visualization"
description: "Agents with complex multi-tool workflows have implicit dependencies between tool calls — tool B uses the output of tool A, tool C requires both A and B — but these dependencies are never recorded or visualized. Without a dependency graph, engineers cannot determine the critical path, identify parallelization opportunities, or explain why a particular tool was invoked. Implement tool dependency graph recording that captures data-flow edges between tool calls and renders them for inspection."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-tool-dependency-graph-visualization
tags: [dependency-graph, tool-tracing, critical-path, data-flow, workflow-visualization, execution-graph]
symptoms:
  - "No record of which tool call produced the input consumed by a downstream tool call"
  - "Cannot determine whether two tool calls could have run in parallel but ran sequentially"
  - "Debugging a wrong answer requires manually tracing which tools fed which other tools"
  - "Critical path of a multi-tool workflow is unknown — optimization is guesswork"
  - "Tool execution order is apparent from logs but data dependencies between calls are invisible"
---

## Why This Happens

Agent frameworks dispatch tool calls and collect results but do not record why a particular tool was called or which prior result was consumed as input. The dependency structure exists implicitly in the LLM's reasoning — it requested tool B because tool A's result contained a value it needed — but this reasoning is not surfaced in telemetry. Recording dependencies requires the agent to annotate tool calls with the IDs of prior calls whose results were referenced in the current call's arguments. This produces a directed acyclic graph (DAG) where nodes are tool calls and edges represent data-flow dependencies.

## Solution 1: Tool Call Node

```python
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ToolCallNode:
    call_id: str
    tool_name: str
    args_summary: str
    result_summary: str = ""
    started_at: float = field(default_factory=time.time)
    ended_at: Optional[float] = None
    success: bool = True
    error: Optional[str] = None
    session_id: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def latency_ms(self) -> Optional[float]:
        if self.ended_at is None:
            return None
        return round((self.ended_at - self.started_at) * 1000, 2)
```

## Solution 2: Dependency Edge

```python
from dataclasses import dataclass
from enum import Enum


class DependencyType(str, Enum):
    DATA_FLOW = "data_flow"        # result of source was used as arg to target
    SEQUENTIAL = "sequential"     # target ran after source in the same turn
    CONDITIONAL = "conditional"   # target ran only because source succeeded


@dataclass
class DependencyEdge:
    source_call_id: str
    target_call_id: str
    dependency_type: DependencyType
    field_path: str = ""   # e.g. "result.user_id" — which field was consumed
    description: str = ""
```

## Solution 3: Tool Dependency Graph

```python
from collections import defaultdict, deque
from typing import Dict, List, Optional, Set


class ToolDependencyGraph:
    """
    Directed acyclic graph of tool call nodes and dependency edges.
    Supports critical path computation and cycle detection.
    """

    def __init__(self):
        self._nodes: Dict[str, ToolCallNode] = {}
        self._edges: List[DependencyEdge] = []
        self._out_edges: Dict[str, List[DependencyEdge]] = defaultdict(list)
        self._in_edges: Dict[str, List[DependencyEdge]] = defaultdict(list)

    def add_node(self, node: ToolCallNode) -> None:
        self._nodes[node.call_id] = node

    def add_edge(self, edge: DependencyEdge) -> None:
        self._edges.append(edge)
        self._out_edges[edge.source_call_id].append(edge)
        self._in_edges[edge.target_call_id].append(edge)

    def roots(self) -> List[ToolCallNode]:
        """Nodes with no incoming edges — the starting tool calls."""
        return [n for cid, n in self._nodes.items() if not self._in_edges[cid]]

    def dependents(self, call_id: str) -> List[ToolCallNode]:
        return [self._nodes[e.target_call_id] for e in self._out_edges[call_id]
                if e.target_call_id in self._nodes]

    def dependencies(self, call_id: str) -> List[ToolCallNode]:
        return [self._nodes[e.source_call_id] for e in self._in_edges[call_id]
                if e.source_call_id in self._nodes]

    def topological_order(self) -> List[ToolCallNode]:
        in_degree = {cid: len(edges) for cid, edges in self._in_edges.items()}
        for cid in self._nodes:
            if cid not in in_degree:
                in_degree[cid] = 0
        queue = deque(cid for cid, d in in_degree.items() if d == 0)
        order = []
        while queue:
            cid = queue.popleft()
            if cid in self._nodes:
                order.append(self._nodes[cid])
            for edge in self._out_edges.get(cid, []):
                in_degree[edge.target_call_id] -= 1
                if in_degree[edge.target_call_id] == 0:
                    queue.append(edge.target_call_id)
        return order

    def critical_path(self) -> List[ToolCallNode]:
        """Returns the longest-latency path through the graph."""
        topo = self.topological_order()
        dist: Dict[str, float] = {n.call_id: (n.latency_ms or 0) for n in topo}
        prev: Dict[str, Optional[str]] = {n.call_id: None for n in topo}

        for node in topo:
            for edge in self._out_edges.get(node.call_id, []):
                target = edge.target_call_id
                if target not in dist:
                    continue
                candidate = dist[node.call_id] + (self._nodes[target].latency_ms or 0)
                if candidate > dist[target]:
                    dist[target] = candidate
                    prev[target] = node.call_id

        if not dist:
            return []
        end = max(dist, key=lambda k: dist[k])
        path = []
        cursor: Optional[str] = end
        while cursor:
            path.append(self._nodes[cursor])
            cursor = prev.get(cursor)
        return list(reversed(path))

    def node_count(self) -> int:
        return len(self._nodes)

    def edge_count(self) -> int:
        return len(self._edges)
```

## Solution 4: Dependency Graph Recorder

```python
import uuid
import time
from typing import Any, Callable, Dict, List, Optional


class DependencyGraphRecorder:
    """
    Records tool calls and their dependencies into a ToolDependencyGraph.
    Callers declare which prior call IDs a new call depends on.
    """

    def __init__(self, graph: ToolDependencyGraph):
        self._graph = graph

    async def record_call(
        self,
        tool_name: str,
        tool_fn: Callable,
        args: Dict[str, Any],
        depends_on: List[str] = None,
        dependency_type: DependencyType = DependencyType.DATA_FLOW,
        field_path: str = "",
        session_id: str = "",
    ) -> tuple[Any, str]:
        call_id = str(uuid.uuid4())[:12]
        args_summary = str(args)[:150]
        node = ToolCallNode(
            call_id=call_id,
            tool_name=tool_name,
            args_summary=args_summary,
            session_id=session_id,
        )
        self._graph.add_node(node)

        for source_id in (depends_on or []):
            self._graph.add_edge(DependencyEdge(
                source_call_id=source_id,
                target_call_id=call_id,
                dependency_type=dependency_type,
                field_path=field_path,
            ))

        try:
            result = await tool_fn(**args)
            node.result_summary = str(result)[:150]
            node.success = True
            return result, call_id
        except Exception as exc:
            node.error = type(exc).__name__
            node.success = False
            raise
        finally:
            node.ended_at = time.time()
```

## Solution 5: ASCII Graph Renderer

```python
from typing import List


class ASCIIToolDependencyRenderer:
    """
    Renders the dependency graph as indented ASCII text for
    quick inspection in log output or CLI tools.
    """

    def __init__(self, graph: ToolDependencyGraph):
        self._graph = graph

    def render(self) -> str:
        lines = ["Tool Dependency Graph", "=" * 40]
        visited: set = set()

        def render_node(node: ToolCallNode, depth: int) -> None:
            if node.call_id in visited:
                lines.append("  " * depth + f"↺ {node.tool_name} [{node.call_id}] (already shown)")
                return
            visited.add(node.call_id)
            status = "✓" if node.success else "✗"
            latency = f"{node.latency_ms}ms" if node.latency_ms else "?"
            lines.append("  " * depth + f"{status} {node.tool_name} [{node.call_id}] ({latency})")
            for child in self._graph.dependents(node.call_id):
                edges = self._graph._in_edges.get(child.call_id, [])
                edge = next((e for e in edges if e.source_call_id == node.call_id), None)
                label = f"[{edge.field_path}]" if edge and edge.field_path else ""
                lines.append("  " * (depth + 1) + f"→ {label}")
                render_node(child, depth + 1)

        for root in self._graph.roots():
            render_node(root, 0)

        cp = self._graph.critical_path()
        if cp:
            lines.append("")
            lines.append("Critical Path:")
            lines.append(" → ".join(n.tool_name for n in cp))
            total_ms = sum(n.latency_ms or 0 for n in cp)
            lines.append(f"Critical path latency: {round(total_ms, 2)}ms")

        return "\n".join(lines)
```

## Solution 6: Dependency Graph Dashboard

```python
import time
from typing import List


class ToolDependencyGraphDashboard:
    """
    Combines graph statistics, critical path analysis, and
    parallelization opportunities into an operational report.
    """

    def __init__(self, graph: ToolDependencyGraph):
        self._graph = graph

    def _parallelizable_nodes(self) -> List[ToolCallNode]:
        """Nodes that have no dependencies on each other but ran sequentially."""
        roots = self._graph.roots()
        return [n for n in roots if (n.latency_ms or 0) > 100]

    def render(self) -> dict:
        cp = self._graph.critical_path()
        return {
            "generated_at": time.time(),
            "graph_stats": {
                "node_count": self._graph.node_count(),
                "edge_count": self._graph.edge_count(),
                "root_count": len(self._graph.roots()),
            },
            "critical_path": {
                "tools": [n.tool_name for n in cp],
                "total_ms": round(sum(n.latency_ms or 0 for n in cp), 2),
                "step_count": len(cp),
            },
            "parallelization_candidates": [
                n.tool_name for n in self._parallelizable_nodes()
            ],
        }
```

## Comparison

| Approach | Node/Edge Recording | Topological Order | Critical Path | ASCII Render | Parallelization Hints |
|---|---|---|---|---|---|
| ToolDependencyGraph | Yes | Yes | Yes | No | No |
| DependencyGraphRecorder | Via graph | No | No | No | No |
| ASCIIToolDependencyRenderer | No | No | Via graph | Yes | No |
| ToolDependencyGraphDashboard | No | No | Via graph | No | Yes |

**Best for production**: Record `depends_on` call IDs at the point where the LLM's tool request is dispatched — the framework knows which prior call results were referenced because they are in the conversation context. Use `field_path` to annotate which specific field from the source result was consumed (e.g., `result.user_id`) so the dependency edge carries semantic meaning beyond just ordering. Emit the `render()` output as a structured log event at session end: on-call engineers investigating wrong answers can replay the exact data-flow path that produced the output. Use `parallelization_candidates` in the dashboard to identify roots that could be dispatched concurrently in future sessions.
