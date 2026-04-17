---
title: "Agent Doesn't Implement Dependency Graph Visualization for Tool Chains"
description: "Agents with complex multi-tool workflows have no way for engineers to understand which tools feed into which, where parallelism is possible, and which tools are on the critical path. Implement dependency graph recording that captures tool-to-tool data flows during execution, renders the graph as an adjacency structure, and surfaces critical path analysis for latency optimization."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-dependency-graph-visualization-for-tool-chains
tags: [dependency-graph, tool-chain-visualization, critical-path, data-flow, dag-analysis, workflow-observability]
symptoms:
  - "No way to visualize which tools run sequentially vs. in parallel"
  - "Cannot identify which tool is on the critical path of a slow request"
  - "Tool chain structure is implicit in code — not captured at runtime"
  - "Parallelism opportunities are invisible without a data-flow graph"
  - "Post-incident analysis cannot show the exact execution topology for a slow request"
---

## Why This Happens

Tool chains are implemented as code — functions calling functions, asyncio.gather() for parallel execution — but the execution topology is never recorded as data. Engineers who want to understand the workflow must read the code, which may not reflect the actual runtime execution order (which can vary based on LLM decisions). Runtime dependency graph recording captures the actual execution structure: which tool produced data that another tool consumed, which tools ran concurrently, and how long each edge took. This graph is the foundation for critical path analysis and parallelism discovery.

## Solution 1: Tool Execution Node

```python
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Set


class NodeStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class ToolExecutionNode:
    node_id: str
    tool_name: str
    args_summary: Dict[str, str]    # truncated arg preview
    status: NodeStatus = NodeStatus.PENDING
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    latency_ms: Optional[float] = None
    error: Optional[str] = None
    result_summary: Optional[str] = None
    parent_node_ids: List[str] = field(default_factory=list)  # data dependencies
    metadata: Dict[str, Any] = field(default_factory=dict)

    def start(self) -> None:
        self.status = NodeStatus.RUNNING
        self.started_at = time.time()

    def complete(self, result_summary: str = "") -> None:
        self.status = NodeStatus.COMPLETED
        self.completed_at = time.time()
        self.result_summary = result_summary[:200]
        if self.started_at:
            self.latency_ms = round((self.completed_at - self.started_at) * 1000, 2)

    def fail(self, error: str) -> None:
        self.status = NodeStatus.FAILED
        self.completed_at = time.time()
        self.error = error[:200]
        if self.started_at:
            self.latency_ms = round((self.completed_at - self.started_at) * 1000, 2)

    @property
    def is_terminal(self) -> bool:
        return self.status in (NodeStatus.COMPLETED, NodeStatus.FAILED, NodeStatus.SKIPPED)
```

## Solution 2: Tool Dependency Graph Recorder

```python
import uuid
from threading import Lock
from typing import Dict, List, Optional, Set


class ToolDependencyGraphRecorder:
    """
    Records tool execution nodes and their data-dependency edges
    into a directed acyclic graph (DAG) per session.
    """

    def __init__(self):
        self._nodes: Dict[str, ToolExecutionNode] = {}
        self._edges: List[tuple] = []   # (from_node_id, to_node_id, edge_type)
        self._lock = Lock()
        self._active_nodes: Set[str] = set()

    def add_node(
        self,
        tool_name: str,
        args: dict,
        parent_node_ids: Optional[List[str]] = None,
    ) -> str:
        node_id = str(uuid.uuid4())[:10]
        args_summary = {
            k: str(v)[:50] for k, v in args.items()
        }
        node = ToolExecutionNode(
            node_id=node_id,
            tool_name=tool_name,
            args_summary=args_summary,
            parent_node_ids=parent_node_ids or [],
        )
        with self._lock:
            self._nodes[node_id] = node
            self._active_nodes.add(node_id)
            for parent_id in (parent_node_ids or []):
                self._edges.append((parent_id, node_id, "data_dependency"))
        return node_id

    def start_node(self, node_id: str) -> None:
        with self._lock:
            if node_id in self._nodes:
                self._nodes[node_id].start()

    def complete_node(self, node_id: str, result_summary: str = "") -> None:
        with self._lock:
            if node_id in self._nodes:
                self._nodes[node_id].complete(result_summary)
                self._active_nodes.discard(node_id)

    def fail_node(self, node_id: str, error: str) -> None:
        with self._lock:
            if node_id in self._nodes:
                self._nodes[node_id].fail(error)
                self._active_nodes.discard(node_id)

    def all_nodes(self) -> List[ToolExecutionNode]:
        with self._lock:
            return list(self._nodes.values())

    def all_edges(self) -> List[tuple]:
        with self._lock:
            return list(self._edges)

    def adjacency_dict(self) -> Dict[str, List[str]]:
        adj: Dict[str, List[str]] = {n.node_id: [] for n in self._nodes.values()}
        for from_id, to_id, _ in self._edges:
            if from_id in adj:
                adj[from_id].append(to_id)
        return adj
```

## Solution 3: Critical Path Analyzer

```python
from typing import Dict, List, Optional, Tuple


class CriticalPathAnalyzer:
    """
    Identifies the critical path in a tool execution DAG —
    the longest sequence of dependent tool calls by total latency.
    """

    def analyze(self, recorder: ToolDependencyGraphRecorder) -> dict:
        nodes = {n.node_id: n for n in recorder.all_nodes()}
        adj = recorder.adjacency_dict()

        # Build reverse adjacency for topological traversal
        in_degree: Dict[str, int] = {nid: 0 for nid in nodes}
        for from_id, to_id, _ in recorder.all_edges():
            in_degree[to_id] = in_degree.get(to_id, 0) + 1

        # Compute earliest completion time per node (forward pass)
        earliest: Dict[str, float] = {}
        topo_order = self._topological_sort(nodes, adj, in_degree)

        for node_id in topo_order:
            node = nodes[node_id]
            parent_max = max(
                (earliest.get(p, 0) + (nodes[p].latency_ms or 0))
                for p in node.parent_node_ids
            ) if node.parent_node_ids else 0
            earliest[node_id] = parent_max

        # Find critical path (longest path by latency sum)
        total_latency = {
            nid: earliest[nid] + (nodes[nid].latency_ms or 0)
            for nid in nodes
        }
        terminal_nodes = [nid for nid in nodes if not adj.get(nid)]
        if not terminal_nodes:
            return {"critical_path": [], "total_ms": 0}

        end_node = max(terminal_nodes, key=lambda nid: total_latency.get(nid, 0))

        path = self._trace_path(end_node, nodes, earliest, total_latency)

        return {
            "critical_path": [
                {
                    "node_id": nid,
                    "tool_name": nodes[nid].tool_name,
                    "latency_ms": nodes[nid].latency_ms,
                }
                for nid in path
            ],
            "total_critical_path_ms": round(total_latency.get(end_node, 0), 2),
            "total_nodes": len(nodes),
            "parallelizable_nodes": len(nodes) - len(path),
        }

    def _topological_sort(self, nodes, adj, in_degree) -> List[str]:
        from collections import deque
        queue = deque(nid for nid, deg in in_degree.items() if deg == 0)
        order = []
        while queue:
            nid = queue.popleft()
            order.append(nid)
            for child in adj.get(nid, []):
                in_degree[child] -= 1
                if in_degree[child] == 0:
                    queue.append(child)
        return order

    def _trace_path(self, end_node, nodes, earliest, total_latency) -> List[str]:
        path = [end_node]
        current = end_node
        while nodes[current].parent_node_ids:
            best_parent = max(
                nodes[current].parent_node_ids,
                key=lambda p: total_latency.get(p, 0),
            )
            path.append(best_parent)
            current = best_parent
        return list(reversed(path))
```

## Solution 4: Parallelism Opportunity Detector

```python
from typing import Dict, List, Set


class ParallelismOpportunityDetector:
    """
    Identifies tool calls that could run in parallel because they share
    no data dependency — both depend only on the same parent nodes.
    """

    def detect(self, recorder: ToolDependencyGraphRecorder) -> List[dict]:
        nodes = {n.node_id: n for n in recorder.all_nodes()}
        groups: List[Set[str]] = []

        # Group nodes by their parent set
        parent_groups: Dict[frozenset, List[str]] = {}
        for node in nodes.values():
            key = frozenset(node.parent_node_ids)
            if key not in parent_groups:
                parent_groups[key] = []
            parent_groups[key].append(node.node_id)

        opportunities = []
        for parent_set, group_ids in parent_groups.items():
            if len(group_ids) > 1:
                serial_time = sum(nodes[nid].latency_ms or 0 for nid in group_ids)
                parallel_time = max((nodes[nid].latency_ms or 0) for nid in group_ids)
                opportunities.append({
                    "node_ids": group_ids,
                    "tool_names": [nodes[nid].tool_name for nid in group_ids],
                    "serial_latency_ms": round(serial_time, 2),
                    "parallel_latency_ms": round(parallel_time, 2),
                    "potential_savings_ms": round(serial_time - parallel_time, 2),
                    "common_parents": list(parent_set),
                })

        return sorted(opportunities, key=lambda x: -x["potential_savings_ms"])
```

## Solution 5: Graph Serializer

```python
import json
import time
from typing import Any


class ToolDependencyGraphSerializer:
    """
    Serializes the dependency graph to JSON for storage, visualization,
    and offline analysis.
    """

    def serialize(self, recorder: ToolDependencyGraphRecorder) -> dict:
        nodes = recorder.all_nodes()
        edges = recorder.all_edges()

        return {
            "generated_at": time.time(),
            "nodes": [
                {
                    "id": n.node_id,
                    "tool_name": n.tool_name,
                    "status": n.status.value,
                    "latency_ms": n.latency_ms,
                    "args_summary": n.args_summary,
                    "result_summary": n.result_summary,
                    "error": n.error,
                    "started_at": n.started_at,
                }
                for n in nodes
            ],
            "edges": [
                {"from": from_id, "to": to_id, "type": etype}
                for from_id, to_id, etype in edges
            ],
            "adjacency": recorder.adjacency_dict(),
        }

    def to_mermaid(self, recorder: ToolDependencyGraphRecorder) -> str:
        """Renders the graph as a Mermaid flowchart string."""
        nodes = {n.node_id: n for n in recorder.all_nodes()}
        lines = ["graph LR"]
        for node in nodes.values():
            label = f"{node.tool_name}\\n{node.latency_ms}ms"
            style = ":::completed" if node.status == NodeStatus.COMPLETED else ":::failed"
            lines.append(f'    {node.node_id}["{label}"]')
        for from_id, to_id, _ in recorder.all_edges():
            lines.append(f"    {from_id} --> {to_id}")
        return "\n".join(lines)
```

## Solution 6: Tool Chain Dependency Dashboard

```python
import time


class ToolChainDependencyDashboard:
    """
    Combines critical path analysis, parallelism opportunities,
    and graph serialization into a single operational view.
    """

    def __init__(
        self,
        recorder: ToolDependencyGraphRecorder,
        critical_path_analyzer: CriticalPathAnalyzer,
        parallelism_detector: ParallelismOpportunityDetector,
        serializer: ToolDependencyGraphSerializer,
    ):
        self._recorder = recorder
        self._critical = critical_path_analyzer
        self._parallel = parallelism_detector
        self._serializer = serializer

    def render(self) -> dict:
        critical = self._critical.analyze(self._recorder)
        opportunities = self._parallel.detect(self._recorder)

        return {
            "generated_at": time.time(),
            "critical_path_analysis": critical,
            "parallelism_opportunities": opportunities[:5],
            "total_potential_savings_ms": sum(
                o["potential_savings_ms"] for o in opportunities
            ),
            "graph": self._serializer.serialize(self._recorder),
            "mermaid": self._serializer.to_mermaid(self._recorder),
        }
```

## Comparison

| Approach | Node Recording | Edge Recording | Critical Path | Parallelism Detection | Graph Export |
|---|---|---|---|---|---|
| ToolDependencyGraphRecorder | Yes | Yes (typed) | No | No | No |
| CriticalPathAnalyzer | No | Via recorder | Yes (longest path) | No | No |
| ParallelismOpportunityDetector | No | Via recorder | No | Yes | No |
| ToolDependencyGraphSerializer | No | Via recorder | No | No | Yes (JSON + Mermaid) |
| ToolChainDependencyDashboard | No | No | Via analyzer | Via detector | Via serializer |

**Best for production**: Record the dependency graph for every request in staging — you don't need it in production for every request, but having it for slow requests (sampled at P95+) provides invaluable optimization signal. Use `to_mermaid()` output to render flow diagrams in your debugging UI — engineers can instantly see whether a workflow is sequential when it should be parallel. Export critical path data to your metrics system as a gauge: the critical path latency is the minimum achievable end-to-end latency regardless of parallelism improvements elsewhere. Use `ParallelismOpportunityDetector` output to drive refactoring: if two tools consistently share the same parent set and have no dependency on each other, wrap them in `asyncio.gather()`.
