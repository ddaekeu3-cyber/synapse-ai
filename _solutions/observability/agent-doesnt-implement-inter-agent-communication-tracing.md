---
title: "Agent Doesn't Implement Inter-Agent Communication Tracing"
description: "Multi-agent systems where agents delegate tasks to sub-agents or communicate via message passing have no visibility into the full call graph when tracing is limited to single agents. A failure in a sub-agent is invisible at the orchestrator level; latency from a slow worker agent is unattributed; loops between agents are undetectable. Implement inter-agent communication tracing that propagates trace context across agent boundaries and reconstructs the full call graph for any multi-agent session."
date: 2026-04-16
difficulty: advanced
category: observability
slug: agent-doesnt-implement-inter-agent-communication-tracing
tags: [inter-agent, multi-agent, distributed-tracing, call-graph, trace-propagation, orchestrator]
symptoms:
  - "Sub-agent failures appear as generic errors at the orchestrator without detail"
  - "Cannot determine which agent in a chain caused end-to-end latency"
  - "Agent delegation loops are undetectable — no cycle detection in the call graph"
  - "Each agent logs independently with no shared trace identifier linking them"
  - "Debugging a multi-agent failure requires correlating logs from multiple services manually"
---

## Why This Happens

Single-agent tracing instruments one process. When an orchestrator agent spawns a sub-agent or sends a task to a worker agent, the trace context — trace ID, span ID, parent span — must be explicitly passed along with the task payload. Without this propagation, the sub-agent starts a new root trace with no relationship to the orchestrator's span. The full end-to-end call graph is invisible: what looks like a 10-second orchestrator span actually consists of 0.5s of orchestrator work plus 9.5s waiting for three sequential sub-agent responses, but this breakdown does not appear in any trace.

## Solution 1: Agent Trace Context

```python
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class AgentTraceContext:
    trace_id: str            # shared across the entire multi-agent session
    span_id: str             # unique to this agent's current operation
    parent_span_id: Optional[str]   # None for root; parent agent's span_id otherwise
    agent_id: str
    agent_role: str          # "orchestrator" | "planner" | "executor" | "worker"
    depth: int               # delegation depth: 0 = root orchestrator
    created_at: float = field(default_factory=time.time)
    baggage: Dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def create_root(agent_id: str, agent_role: str = "orchestrator") -> "AgentTraceContext":
        return AgentTraceContext(
            trace_id=uuid.uuid4().hex,
            span_id=uuid.uuid4().hex[:12],
            parent_span_id=None,
            agent_id=agent_id,
            agent_role=agent_role,
            depth=0,
        )

    def spawn_child(self, child_agent_id: str, child_role: str) -> "AgentTraceContext":
        return AgentTraceContext(
            trace_id=self.trace_id,
            span_id=uuid.uuid4().hex[:12],
            parent_span_id=self.span_id,
            agent_id=child_agent_id,
            agent_role=child_role,
            depth=self.depth + 1,
            baggage=dict(self.baggage),
        )

    def to_headers(self) -> Dict[str, str]:
        return {
            "X-Trace-Id": self.trace_id,
            "X-Span-Id": self.span_id,
            "X-Parent-Span-Id": self.parent_span_id or "",
            "X-Agent-Id": self.agent_id,
            "X-Agent-Role": self.agent_role,
            "X-Trace-Depth": str(self.depth),
        }

    @staticmethod
    def from_headers(headers: Dict[str, str], agent_id: str, agent_role: str) -> "AgentTraceContext":
        return AgentTraceContext(
            trace_id=headers.get("X-Trace-Id", uuid.uuid4().hex),
            span_id=uuid.uuid4().hex[:12],
            parent_span_id=headers.get("X-Span-Id") or None,
            agent_id=agent_id,
            agent_role=agent_role,
            depth=int(headers.get("X-Trace-Depth", "0")) + 1,
        )
```

## Solution 2: Agent Span Recorder

```python
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class AgentSpan:
    trace_id: str
    span_id: str
    parent_span_id: Optional[str]
    agent_id: str
    agent_role: str
    operation: str
    started_at: float
    ended_at: Optional[float] = None
    status: str = "in_progress"   # "ok" | "error" | "in_progress"
    error: Optional[str] = None
    attributes: Dict[str, Any] = field(default_factory=dict)
    child_span_ids: List[str] = field(default_factory=list)

    def duration_ms(self) -> Optional[float]:
        if self.ended_at is None:
            return None
        return round((self.ended_at - self.started_at) * 1000, 2)


class AgentSpanRecorder:
    """
    Records spans for a single agent's operations within a trace.
    Thread-safe; supports concurrent span recording.
    """

    def __init__(self):
        self._lock = __import__("threading").Lock()
        self._spans: Dict[str, AgentSpan] = {}

    def start_span(
        self,
        ctx: AgentTraceContext,
        operation: str,
        attributes: Optional[Dict[str, Any]] = None,
    ) -> AgentSpan:
        span = AgentSpan(
            trace_id=ctx.trace_id,
            span_id=ctx.span_id,
            parent_span_id=ctx.parent_span_id,
            agent_id=ctx.agent_id,
            agent_role=ctx.agent_role,
            operation=operation,
            started_at=time.time(),
            attributes=attributes or {},
        )
        with self._lock:
            self._spans[span.span_id] = span
        return span

    def end_span(
        self,
        span: AgentSpan,
        status: str = "ok",
        error: Optional[str] = None,
    ) -> None:
        with self._lock:
            span.ended_at = time.time()
            span.status = status
            span.error = error

    def add_child(self, parent_span_id: str, child_span_id: str) -> None:
        with self._lock:
            parent = self._spans.get(parent_span_id)
            if parent:
                parent.child_span_ids.append(child_span_id)

    def all_spans(self) -> List[AgentSpan]:
        with self._lock:
            return list(self._spans.values())
```

## Solution 3: Distributed Trace Collector

```python
import time
from collections import defaultdict
from threading import Lock
from typing import Dict, List, Optional


class DistributedAgentTraceCollector:
    """
    Aggregates spans from multiple agents by trace_id.
    Each agent ships its spans here so the full call graph is visible.
    """

    def __init__(self, max_traces: int = 1000):
        self._lock = Lock()
        self._traces: Dict[str, List[AgentSpan]] = defaultdict(list)
        self._trace_started: Dict[str, float] = {}
        self._max = max_traces

    def ingest(self, spans: List[AgentSpan]) -> None:
        with self._lock:
            for span in spans:
                tid = span.trace_id
                if tid not in self._trace_started:
                    self._trace_started[tid] = span.started_at
                self._traces[tid].append(span)
            self._evict()

    def get_trace(self, trace_id: str) -> List[AgentSpan]:
        with self._lock:
            return list(self._traces.get(trace_id, []))

    def _evict(self) -> None:
        if len(self._traces) <= self._max:
            return
        oldest = sorted(self._trace_started, key=lambda k: self._trace_started[k])
        for tid in oldest[:len(self._traces) - self._max]:
            del self._traces[tid]
            del self._trace_started[tid]
```

## Solution 4: Call Graph Reconstructor

```python
from typing import Dict, List, Optional, Set


@dataclass
class CallGraphNode:
    span_id: str
    agent_id: str
    agent_role: str
    operation: str
    duration_ms: Optional[float]
    status: str
    depth: int
    children: List["CallGraphNode"]


class AgentCallGraphReconstructor:
    """
    Reconstructs a tree of CallGraphNodes from a flat list of spans.
    Detects cycles (loops) in the delegation chain.
    """

    def reconstruct(self, spans: List[AgentSpan]) -> Optional[CallGraphNode]:
        span_map = {s.span_id: s for s in spans}
        children_map: Dict[str, List[str]] = defaultdict(list)

        for span in spans:
            if span.parent_span_id and span.parent_span_id in span_map:
                children_map[span.parent_span_id].append(span.span_id)

        # Find root: no parent or parent not in this trace
        roots = [s for s in spans if not s.parent_span_id or s.parent_span_id not in span_map]
        if not roots:
            return None

        root = min(roots, key=lambda s: s.started_at)
        return self._build_node(root, span_map, children_map, depth=0, visited=set())

    def _build_node(
        self,
        span: AgentSpan,
        span_map: Dict[str, AgentSpan],
        children_map: Dict[str, List[str]],
        depth: int,
        visited: Set[str],
    ) -> CallGraphNode:
        if span.span_id in visited:
            # Cycle detected — truncate
            return CallGraphNode(
                span_id=span.span_id,
                agent_id=span.agent_id,
                agent_role=span.agent_role,
                operation="[CYCLE DETECTED]",
                duration_ms=None,
                status="cycle",
                depth=depth,
                children=[],
            )
        visited = visited | {span.span_id}
        children = [
            self._build_node(span_map[cid], span_map, children_map, depth + 1, visited)
            for cid in children_map.get(span.span_id, [])
            if cid in span_map
        ]
        return CallGraphNode(
            span_id=span.span_id,
            agent_id=span.agent_id,
            agent_role=span.agent_role,
            operation=span.operation,
            duration_ms=span.duration_ms(),
            status=span.status,
            depth=depth,
            children=sorted(children, key=lambda n: n.span_id),
        )

    def has_cycle(self, root: CallGraphNode) -> bool:
        def search(node: CallGraphNode) -> bool:
            if node.status == "cycle":
                return True
            return any(search(c) for c in node.children)
        return search(root)
```

## Solution 5: Critical Path Extractor

```python
from typing import List, Optional


class InterAgentCriticalPathExtractor:
    """
    Finds the longest duration path through the call graph.
    This is the chain of agent spans that dominated end-to-end latency.
    """

    def extract(self, root: CallGraphNode) -> List[CallGraphNode]:
        def longest_path(node: CallGraphNode) -> List[CallGraphNode]:
            if not node.children:
                return [node]
            child_paths = [longest_path(c) for c in node.children]
            best = max(child_paths, key=lambda p: sum(n.duration_ms or 0 for n in p))
            return [node] + best

        return longest_path(root)

    def critical_path_ms(self, path: List[CallGraphNode]) -> float:
        return round(sum(n.duration_ms or 0 for n in path), 2)
```

## Solution 6: Inter-Agent Trace Dashboard

```python
import time
from typing import List, Optional


class InterAgentTraceDashboard:
    """
    Renders a full multi-agent trace: call graph, critical path,
    cycle detection, and per-agent span summary.
    """

    def __init__(
        self,
        collector: DistributedAgentTraceCollector,
        reconstructor: AgentCallGraphReconstructor,
        critical_path_extractor: InterAgentCriticalPathExtractor,
    ):
        self._collector = collector
        self._reconstructor = reconstructor
        self._extractor = critical_path_extractor

    def render(self, trace_id: str) -> dict:
        spans = self._collector.get_trace(trace_id)
        if not spans:
            return {"trace_id": trace_id, "error": "trace not found"}

        root = self._reconstructor.reconstruct(spans)
        cycle = self._reconstructor.has_cycle(root) if root else False
        critical_path = self._extractor.extract(root) if root and not cycle else []
        critical_ms = self._extractor.critical_path_ms(critical_path)

        agents = list({s.agent_id for s in spans})
        errored = [s for s in spans if s.status == "error"]

        return {
            "generated_at": time.time(),
            "trace_id": trace_id,
            "total_spans": len(spans),
            "agents_involved": agents,
            "cycle_detected": cycle,
            "critical_path": [
                {"agent_id": n.agent_id, "operation": n.operation, "duration_ms": n.duration_ms}
                for n in critical_path
            ],
            "critical_path_total_ms": critical_ms,
            "errored_spans": [
                {"agent_id": s.agent_id, "operation": s.operation, "error": s.error}
                for s in errored
            ],
        }
```

## Comparison

| Approach | Context Propagation | Span Recording | Multi-Agent Collection | Cycle Detection | Critical Path |
|---|---|---|---|---|---|
| AgentTraceContext | Yes (headers) | No | No | No | No |
| AgentSpanRecorder | No | Yes | No | No | No |
| DistributedAgentTraceCollector | No | No | Yes (by trace_id) | No | No |
| AgentCallGraphReconstructor | No | No | No | Yes | No |
| InterAgentCriticalPathExtractor | No | No | No | No | Yes |
| InterAgentTraceDashboard | No | No | No | No | Yes (renders) |

**Best for production**: Always propagate `AgentTraceContext.to_headers()` in every inter-agent message payload or HTTP call — treat trace context as mandatory as authentication headers. Set `max_traces=1000` in `DistributedAgentTraceCollector` with a TTL of 30 minutes; traces older than that are no longer relevant for live debugging. Alert immediately on `cycle_detected=True` in the dashboard: an agent delegation cycle means work is being duplicated in a loop and will run until quota is exhausted. Use critical path data to prioritize optimization: if the critical path is always `orchestrator → search_agent → summarizer_agent`, that chain is the only one that reduces end-to-end latency when sped up.
