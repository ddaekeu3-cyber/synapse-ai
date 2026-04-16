---
title: "Agent Doesn't Implement Tool Dependency Health Graph"
description: "Agents that treat tools as independent components cannot reason about cascading failures: when a database tool depends on a connection pool that depends on a credentials service, a failure in the credentials service manifests as a cryptic database error. Implement a tool dependency health graph that models upstream dependencies, propagates health status through the dependency tree, and identifies the root cause of cascading failures."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-tool-dependency-health-graph
tags: [dependency-graph, health-propagation, root-cause, cascading-failure, tool-health, dependency-tracking]
symptoms:
  - "Database tool returns errors but the real cause is a failed credential refresh upstream"
  - "Health check shows five tools unhealthy with no indication they share a common dependency"
  - "On-call engineers spend 10 minutes tracing a cascading failure that started at one service"
  - "No model of which tools depend on which infrastructure components"
  - "All tool failures appear equally important — no way to identify the root cause node"
---

## Why This Happens

Tools are built on shared infrastructure: connection pools, credential stores, external APIs, internal microservices. When shared infrastructure fails, every tool that depends on it fails. Without a dependency graph, each tool failure is treated as independent — generating five separate alerts for what is actually one root cause. A dependency graph models these relationships explicitly, allowing health status to propagate from root cause nodes upward through dependent tools. The graph also enables impact analysis: before taking a dependency offline for maintenance, the graph shows which tools will be affected.

## Solution 1: Dependency Node

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional
import time


class NodeType(str, Enum):
    TOOL = "tool"                     # agent-facing tool
    INFRASTRUCTURE = "infrastructure"  # connection pool, cache, queue
    EXTERNAL_API = "external_api"     # third-party API
    CREDENTIAL = "credential"         # auth service, secret store
    DATABASE = "database"             # database or data store


class HealthStatus(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"


@dataclass
class DependencyNode:
    node_id: str
    name: str
    node_type: NodeType
    status: HealthStatus = HealthStatus.UNKNOWN
    last_checked_at: Optional[float] = None
    last_error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    dependency_ids: List[str] = field(default_factory=list)  # upstream deps

    def is_healthy(self) -> bool:
        return self.status == HealthStatus.HEALTHY

    def update_status(self, status: HealthStatus, error: Optional[str] = None) -> None:
        self.status = status
        self.last_checked_at = time.time()
        self.last_error = error
```

## Solution 2: Tool Dependency Graph

```python
from collections import deque
from typing import Dict, List, Optional, Set


class ToolDependencyGraph:
    """
    Directed acyclic graph of tool and infrastructure dependencies.
    Edges point from a node to its upstream dependencies.
    Provides traversal, root cause detection, and impact analysis.
    """

    def __init__(self):
        self._nodes: Dict[str, DependencyNode] = {}

    def add_node(self, node: DependencyNode) -> None:
        self._nodes[node.node_id] = node

    def add_dependency(self, node_id: str, depends_on_id: str) -> None:
        if node_id in self._nodes:
            if depends_on_id not in self._nodes[node_id].dependency_ids:
                self._nodes[node_id].dependency_ids.append(depends_on_id)

    def get(self, node_id: str) -> Optional[DependencyNode]:
        return self._nodes.get(node_id)

    def all_nodes(self) -> List[DependencyNode]:
        return list(self._nodes.values())

    def upstream_dependencies(self, node_id: str) -> List[DependencyNode]:
        """Returns all transitive upstream dependencies of a node (BFS)."""
        visited: Set[str] = set()
        queue = deque([node_id])
        result = []
        while queue:
            current = queue.popleft()
            if current in visited:
                continue
            visited.add(current)
            node = self._nodes.get(current)
            if node is None:
                continue
            if current != node_id:
                result.append(node)
            for dep_id in node.dependency_ids:
                if dep_id not in visited:
                    queue.append(dep_id)
        return result

    def downstream_dependents(self, node_id: str) -> List[DependencyNode]:
        """Returns all nodes that (transitively) depend on this node."""
        visited: Set[str] = set()
        queue = deque([node_id])
        result = []
        # Build reverse index
        reverse: Dict[str, List[str]] = {nid: [] for nid in self._nodes}
        for nid, node in self._nodes.items():
            for dep_id in node.dependency_ids:
                if dep_id in reverse:
                    reverse[dep_id].append(nid)

        while queue:
            current = queue.popleft()
            if current in visited:
                continue
            visited.add(current)
            if current != node_id:
                node = self._nodes.get(current)
                if node:
                    result.append(node)
            for dependent_id in reverse.get(current, []):
                if dependent_id not in visited:
                    queue.append(dependent_id)
        return result
```

## Solution 3: Health Status Propagator

```python
from typing import Dict, List


class HealthStatusPropagator:
    """
    Propagates health status through the dependency graph.
    A node is considered unhealthy if any of its transitive upstream
    dependencies are unhealthy — even if the node itself reports healthy.
    """

    def __init__(self, graph: ToolDependencyGraph):
        self._graph = graph

    def effective_status(self, node_id: str) -> HealthStatus:
        """
        Returns the effective health status accounting for upstream dependencies.
        """
        node = self._graph.get(node_id)
        if node is None:
            return HealthStatus.UNKNOWN

        if node.status == HealthStatus.UNHEALTHY:
            return HealthStatus.UNHEALTHY

        upstream = self._graph.upstream_dependencies(node_id)
        has_unhealthy = any(n.status == HealthStatus.UNHEALTHY for n in upstream)
        has_degraded = any(n.status == HealthStatus.DEGRADED for n in upstream)

        if has_unhealthy:
            return HealthStatus.UNHEALTHY
        if node.status == HealthStatus.DEGRADED or has_degraded:
            return HealthStatus.DEGRADED
        return node.status

    def propagated_statuses(self) -> Dict[str, HealthStatus]:
        return {
            node.node_id: self.effective_status(node.node_id)
            for node in self._graph.all_nodes()
        }
```

## Solution 4: Root Cause Identifier

```python
from typing import List


class RootCauseIdentifier:
    """
    Identifies root cause nodes: unhealthy nodes that have no unhealthy
    upstream dependencies. These are the origin of cascading failures.
    """

    def __init__(self, graph: ToolDependencyGraph):
        self._graph = graph

    def find_root_causes(self) -> List[DependencyNode]:
        root_causes = []
        for node in self._graph.all_nodes():
            if node.status != HealthStatus.UNHEALTHY:
                continue
            upstream = self._graph.upstream_dependencies(node.node_id)
            if not any(n.status == HealthStatus.UNHEALTHY for n in upstream):
                root_causes.append(node)
        return root_causes

    def impact_of(self, node_id: str) -> dict:
        """Returns which tools are impacted if this node fails."""
        dependents = self._graph.downstream_dependents(node_id)
        tool_nodes = [n for n in dependents if n.node_type == NodeType.TOOL]
        return {
            "node_id": node_id,
            "total_impacted": len(dependents),
            "impacted_tools": [n.name for n in tool_nodes],
            "impacted_node_ids": [n.node_id for n in dependents],
        }
```

## Solution 5: Dependency Health Checker

```python
import asyncio
from typing import Any, Callable, Dict


class DependencyHealthChecker:
    """
    Runs health check callables for each registered node and updates
    the graph with the results. Checks run concurrently.
    """

    def __init__(self, graph: ToolDependencyGraph):
        self._graph = graph
        self._check_fns: Dict[str, Callable] = {}

    def register_check(self, node_id: str, check_fn: Callable) -> None:
        """check_fn: async callable that returns (HealthStatus, error_str_or_None)"""
        self._check_fns[node_id] = check_fn

    async def run_checks(self) -> Dict[str, HealthStatus]:
        tasks = {
            node_id: asyncio.create_task(self._run_one(node_id, fn))
            for node_id, fn in self._check_fns.items()
        }
        results = {}
        for node_id, task in tasks.items():
            status, error = await task
            node = self._graph.get(node_id)
            if node:
                node.update_status(status, error)
            results[node_id] = status
        return results

    async def _run_one(
        self,
        node_id: str,
        check_fn: Callable,
    ):
        try:
            result = await asyncio.wait_for(check_fn(), timeout=10.0)
            if isinstance(result, tuple):
                return result
            return result, None
        except asyncio.TimeoutError:
            return HealthStatus.UNHEALTHY, "health check timed out"
        except Exception as exc:
            return HealthStatus.UNHEALTHY, str(exc)[:200]
```

## Solution 6: Dependency Health Graph Dashboard

```python
import time


class DependencyHealthGraphDashboard:
    """
    Combines propagated statuses, root cause analysis, and impact
    assessment into a single operational report.
    """

    def __init__(
        self,
        graph: ToolDependencyGraph,
        propagator: HealthStatusPropagator,
        root_cause_identifier: RootCauseIdentifier,
    ):
        self._graph = graph
        self._propagator = propagator
        self._rci = root_cause_identifier

    def render(self) -> dict:
        propagated = self._propagator.propagated_statuses()
        root_causes = self._rci.find_root_causes()

        node_summaries = []
        for node in self._graph.all_nodes():
            eff_status = propagated.get(node.node_id, HealthStatus.UNKNOWN)
            node_summaries.append({
                "node_id": node.node_id,
                "name": node.name,
                "type": node.node_type.value,
                "own_status": node.status.value,
                "effective_status": eff_status.value,
                "dependency_count": len(node.dependency_ids),
                "last_error": node.last_error,
            })

        return {
            "generated_at": time.time(),
            "total_nodes": len(node_summaries),
            "unhealthy_count": sum(
                1 for n in node_summaries if n["effective_status"] == "unhealthy"
            ),
            "root_causes": [
                {
                    "node_id": rc.node_id,
                    "name": rc.name,
                    "type": rc.node_type.value,
                    "error": rc.last_error,
                    "impact": self._rci.impact_of(rc.node_id),
                }
                for rc in root_causes
            ],
            "nodes": node_summaries,
        }
```

## Comparison

| Approach | Dependency Modeling | Health Propagation | Root Cause Detection | Impact Analysis | Dashboard |
|---|---|---|---|---|---|
| ToolDependencyGraph | Yes (DAG) | No | No | Partial (traversal) | No |
| HealthStatusPropagator | Via graph | Yes (transitive) | No | No | No |
| RootCauseIdentifier | Via graph | No | Yes | Yes (impact_of) | No |
| DependencyHealthChecker | Via graph | No | No | No | No |
| DependencyHealthGraphDashboard | No | Via propagator | Via RCI | Via RCI | Yes |

**Best for production**: Model the dependency graph at deploy time — tool → infrastructure → external_api relationships rarely change and should be explicit, not discovered at incident time. Run `DependencyHealthChecker.run_checks()` every 30 seconds and emit `DependencyHealthGraphDashboard.render()` to your metrics system. Alert on `root_causes` with non-empty lists rather than on individual node failures — one root cause alert is more actionable than five cascading alerts. Use `RootCauseIdentifier.impact_of()` during maintenance planning to predict which tools will be affected before taking a dependency offline.
