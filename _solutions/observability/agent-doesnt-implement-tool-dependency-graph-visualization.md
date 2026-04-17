---
title: "Agent Doesn't Implement Tool Dependency Graph Visualization"
description: "Agents with many tools have implicit dependency relationships that are invisible without tooling: tool A always runs before tool B because B needs A's output; tool C and D are always called together; tool E is never called when tool F succeeds. Without a dependency graph, engineers cannot optimize call ordering, identify redundant tool calls, or understand which tools are critical path. Implement tool dependency graph construction from execution traces and visualization-ready export."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-tool-dependency-graph-visualization
tags: [tool-dependency, dependency-graph, execution-tracing, call-ordering, graph-visualization, tool-analysis]
symptoms:
  - "No visibility into which tools are always called sequentially vs independently"
  - "Cannot identify which tool is on the critical latency path without manual tracing"
  - "Tool call ordering is implicit in code — no documentation or diagram exists"
  - "Redundant tool call patterns (always calling A before B even when B doesn't use A's output) are invisible"
  - "Engineers must read agent source code to understand tool execution topology"
---

## Why This Happens

Tool relationships emerge from agent behavior over thousands of sessions but are never made explicit. The agent framework knows which tools were called and when, but does not record whether one tool's output was used as input to the next — that relationship exists only in the LLM's reasoning. By observing co-occurrence patterns (tool A appears in the same turn as tool B), sequencing patterns (A always precedes B within a turn), and argument threading (output fields of A appear in arguments of B), a dependency graph can be inferred from execution traces without modifying tool implementations.

## Solution 1: Tool Execution Node

```python
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ToolExecutionNode:
    tool_name: str
    turn_index: int
    call_index: int          # order within the turn
    args: Dict[str, Any]
    result: Any
    latency_ms: float
    success: bool
    session_id: str
    timestamp: float

    def arg_values(self) -> set:
        """Flat set of all string argument values for threading detection."""
        values = set()
        for v in self._flatten(self.args):
            if isinstance(v, str) and len(v) >= 8:
                values.add(v)
        return values

    def result_values(self) -> set:
        """Flat set of all string result values for threading detection."""
        return {v for v in self._flatten(self.result) if isinstance(v, str) and len(v) >= 8}

    @staticmethod
    def _flatten(obj: Any, depth: int = 3) -> List[Any]:
        if depth == 0:
            return []
        if isinstance(obj, dict):
            return [v for val in obj.values() for v in ToolExecutionNode._flatten(val, depth - 1)]
        if isinstance(obj, list):
            return [v for item in obj for v in ToolExecutionNode._flatten(item, depth - 1)]
        return [obj]
```

## Solution 2: Dependency Edge

```python
from dataclasses import dataclass


@dataclass
class DependencyEdge:
    source_tool: str
    target_tool: str
    edge_type: str       # "sequence" | "co_occurrence" | "argument_thread"
    observation_count: int
    confidence: float    # 0.0 – 1.0

    def key(self) -> str:
        return f"{self.source_tool}->{self.target_tool}:{self.edge_type}"
```

## Solution 3: Dependency Graph Builder

```python
from collections import defaultdict
from typing import Dict, List, Tuple


class ToolDependencyGraphBuilder:
    """
    Infers tool dependency edges from a collection of execution traces.
    Three edge types:
      - sequence: A always appears before B in the same turn
      - co_occurrence: A and B appear together frequently
      - argument_thread: output values of A appear in args of B
    """

    def __init__(
        self,
        sequence_threshold: float = 0.70,    # A precedes B in >= 70% of co-occurrences
        cooccurrence_threshold: int = 5,     # must co-occur at least 5 times
        thread_threshold: float = 0.50,      # argument threading in >= 50% of co-occurrences
    ):
        self._seq_threshold = sequence_threshold
        self._coocc_threshold = cooccurrence_threshold
        self._thread_threshold = thread_threshold

    def build(self, traces: List[List[ToolExecutionNode]]) -> List[DependencyEdge]:
        """
        traces: list of turns, each turn is a list of ToolExecutionNodes in call order.
        """
        pair_counts: Dict[Tuple[str, str], int] = defaultdict(int)
        sequence_counts: Dict[Tuple[str, str], int] = defaultdict(int)
        thread_counts: Dict[Tuple[str, str], int] = defaultdict(int)

        for turn in traces:
            tool_names = [n.tool_name for n in turn]
            for i, node_a in enumerate(turn):
                for j, node_b in enumerate(turn):
                    if i == j:
                        continue
                    pair = (node_a.tool_name, node_b.tool_name)
                    pair_counts[pair] += 1

                    # Sequence: A appears before B
                    if i < j:
                        sequence_counts[pair] += 1

                    # Argument threading: result values of A in args of B
                    overlap = node_a.result_values() & node_b.arg_values()
                    if overlap:
                        thread_counts[pair] += 1

        edges = []
        for pair, count in pair_counts.items():
            if count < self._coocc_threshold:
                continue
            source, target = pair

            # Sequence edge
            seq_rate = sequence_counts.get(pair, 0) / count
            if seq_rate >= self._seq_threshold:
                edges.append(DependencyEdge(
                    source_tool=source,
                    target_tool=target,
                    edge_type="sequence",
                    observation_count=count,
                    confidence=round(seq_rate, 4),
                ))
            elif count >= self._coocc_threshold:
                edges.append(DependencyEdge(
                    source_tool=source,
                    target_tool=target,
                    edge_type="co_occurrence",
                    observation_count=count,
                    confidence=round(count / max(count, 1), 4),
                ))

            # Argument threading edge
            thread_rate = thread_counts.get(pair, 0) / count
            if thread_rate >= self._thread_threshold:
                edges.append(DependencyEdge(
                    source_tool=source,
                    target_tool=target,
                    edge_type="argument_thread",
                    observation_count=thread_counts.get(pair, 0),
                    confidence=round(thread_rate, 4),
                ))

        return edges
```

## Solution 4: Critical Path Analyzer

```python
from typing import Dict, List, Optional, Set


class CriticalPathAnalyzer:
    """
    Identifies the longest sequential dependency chain in the tool graph.
    Tools on the critical path dominate end-to-end latency.
    """

    def __init__(self, edges: List[DependencyEdge]):
        self._sequence_edges = [e for e in edges if e.edge_type == "sequence"]

    def _build_adjacency(self) -> Dict[str, List[str]]:
        adj: Dict[str, List[str]] = defaultdict(list)
        for e in self._sequence_edges:
            adj[e.source_tool].append(e.target_tool)
        return adj

    def critical_path(self) -> List[str]:
        adj = self._build_adjacency()
        all_nodes = {e.source_tool for e in self._sequence_edges} | \
                    {e.target_tool for e in self._sequence_edges}

        memo: Dict[str, List[str]] = {}

        def dfs(node: str) -> List[str]:
            if node in memo:
                return memo[node]
            if node not in adj or not adj[node]:
                memo[node] = [node]
                return [node]
            best = [node]
            for neighbor in adj[node]:
                path = [node] + dfs(neighbor)
                if len(path) > len(best):
                    best = path
            memo[node] = best
            return best

        longest = []
        for node in all_nodes:
            path = dfs(node)
            if len(path) > len(longest):
                longest = path

        return longest

    def parallelizable_groups(self) -> List[Set[str]]:
        """
        Returns sets of tools that have no sequential dependency between them
        and could be called in parallel.
        """
        adj = self._build_adjacency()
        all_nodes = {e.source_tool for e in self._sequence_edges} | \
                    {e.target_tool for e in self._sequence_edges}
        sequential_pairs = {(e.source_tool, e.target_tool) for e in self._sequence_edges}

        independent: Set[str] = set()
        for node in all_nodes:
            if not any(node in pair for pair in sequential_pairs):
                independent.add(node)

        return [independent] if independent else []
```

## Solution 5: Graph Export for Visualization

```python
import json
from typing import List


class DependencyGraphExporter:
    """
    Exports the dependency graph in formats suitable for visualization tools
    (Mermaid, DOT/Graphviz, and a JSON adjacency list).
    """

    def to_mermaid(self, edges: List[DependencyEdge]) -> str:
        lines = ["graph LR"]
        edge_style = {
            "sequence": "-->",
            "argument_thread": "-.->",
            "co_occurrence": "~~~",
        }
        for e in edges:
            arrow = edge_style.get(e.edge_type, "-->")
            label = f"|{e.edge_type} {e.confidence:.0%}|"
            lines.append(f"    {e.source_tool}{arrow}{label}{e.target_tool}")
        return "\n".join(lines)

    def to_dot(self, edges: List[DependencyEdge]) -> str:
        lines = ["digraph tool_dependencies {", '    rankdir=LR;']
        for e in edges:
            style = "solid" if e.edge_type == "sequence" else "dashed"
            color = "black" if e.edge_type == "argument_thread" else "gray"
            lines.append(
                f'    "{e.source_tool}" -> "{e.target_tool}" '
                f'[label="{e.edge_type}\\n{e.confidence:.0%}", '
                f'style={style}, color={color}];'
            )
        lines.append("}")
        return "\n".join(lines)

    def to_json(self, edges: List[DependencyEdge]) -> str:
        return json.dumps(
            [
                {
                    "source": e.source_tool,
                    "target": e.target_tool,
                    "type": e.edge_type,
                    "observations": e.observation_count,
                    "confidence": e.confidence,
                }
                for e in edges
            ],
            indent=2,
        )
```

## Solution 6: Dependency Graph Dashboard

```python
import time
from typing import List


class ToolDependencyGraphDashboard:
    """
    Combines graph construction, critical path analysis, and export
    into a single snapshot for engineering and architecture review.
    """

    def __init__(
        self,
        builder: ToolDependencyGraphBuilder,
        exporter: DependencyGraphExporter,
    ):
        self._builder = builder
        self._exporter = exporter

    def render(self, traces: List[List[ToolExecutionNode]]) -> dict:
        edges = self._builder.build(traces)
        analyzer = CriticalPathAnalyzer(edges)
        critical = analyzer.critical_path()
        parallel_groups = analyzer.parallelizable_groups()

        return {
            "generated_at": time.time(),
            "total_edges": len(edges),
            "edge_types": {
                t: sum(1 for e in edges if e.edge_type == t)
                for t in ("sequence", "co_occurrence", "argument_thread")
            },
            "critical_path": critical,
            "critical_path_length": len(critical),
            "parallelizable_tool_groups": [list(g) for g in parallel_groups],
            "mermaid": self._exporter.to_mermaid(edges),
            "dot": self._exporter.to_dot(edges),
        }
```

## Comparison

| Approach | Sequence Detection | Argument Threading | Critical Path | Parallel Groups | Export Formats |
|---|---|---|---|---|---|
| ToolDependencyGraphBuilder | Yes (rate-based) | Yes (value overlap) | No | No | No |
| CriticalPathAnalyzer | No | No | Yes (DFS) | Yes | No |
| DependencyGraphExporter | No | No | No | No | Mermaid, DOT, JSON |
| ToolDependencyGraphDashboard | Via builder | Via builder | Via analyzer | Via analyzer | Via exporter |

**Best for production**: Run `ToolDependencyGraphBuilder.build()` weekly over the prior week's execution traces and compare against the previous week's graph — new edges signal that the agent has started using tools in new combinations, which may indicate prompt drift or new user query patterns. Use the Mermaid output to embed a live dependency diagram in your engineering wiki. Tools that appear in `parallelizable_tool_groups` but are being called sequentially are immediate latency optimization targets — wrapping them in `asyncio.gather` should reduce turn latency by their individual round-trip times.
