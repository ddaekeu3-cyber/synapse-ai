---
title: "Agent Doesn't Implement Dependency Graph Tracing for Multi-Agent Workflows"
description: "Multi-agent systems that emit flat log lines lose the causal structure of execution: which sub-agent was spawned by which parent, which tool calls were triggered by which agent turn, and which downstream failures originated from which upstream decision. Implement dependency graph tracing that records parent-child span relationships, propagates trace context across agent boundaries, and reconstructs the full execution DAG for debugging and latency attribution."
date: 2026-04-16
difficulty: advanced
category: observability
slug: agent-doesnt-implement-dependency-graph-tracing-for-multi-agent-workflows
tags: [dependency-graph, distributed-tracing, multi-agent, span-propagation, execution-dag, latency-attribution]
symptoms:
  - "Cannot tell which sub-agent invocation caused a downstream tool failure"
  - "Logs from parallel agents are interleaved with no structural link between them"
  - "Critical path analysis is impossible — no data on which agent span was slowest"
  - "A failing orchestration run produces no causal chain, only a flat error message"
  - "Retry storms are invisible — the same tool gets called 12 times with no parent linkage"
---

## Why This Happens

Flat logging treats every event as independent. A multi-agent workflow is a DAG: the orchestrator spawns sub-agents, each sub-agent calls tools, and tool results feed back into subsequent LLM turns. Without a trace context that propagates across these boundaries, each agent sees only its own events. Dependency graph tracing assigns every unit of work a span ID and a parent span ID, links them at creation time, and stores enough metadata to reconstruct the causal chain after the fact. This is the same model as OpenTelemetry distributed tracing but applied to agent-to-agent relationships.

## Solution 1: Trace Span

```python
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class SpanKind(str, Enum):
    ORCHESTRATOR = "orchestrator"   # top-level agent coordinating others
    AGENT = "agent"                 # sub-agent spawned by orchestrator
    TOOL_CALL = "tool_call"         # individual tool invocation
    LLM_TURN = "llm_turn"          # single LLM completion call
    DECISION = "decision"           # routing/branching decision point


class SpanStatus(str, Enum):
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class TraceSpan:
    span_id: str
    trace_id: str
    parent_span_id: Optional[str]
    kind: SpanKind
    name: str
    started_at: float = field(default_factory=time.time)
    ended_at: Optional[float] = None
    status: SpanStatus = SpanStatus.RUNNING
    attributes: Dict[str, Any] = field(default_factory=dict)
    events: List[dict] = field(default_factory=list)
    error: Optional[str] = None

    @classmethod
    def create(
        cls,
        name: str,
        kind: SpanKind,
        trace_id: str,
        parent_span_id: Optional[str] = None,
        attributes: Optional[Dict[str, Any]] = None,
    ) -> "TraceSpan":
        return cls(
            span_id=uuid.uuid4().hex[:16],
            trace_id=trace_id,
            parent_span_id=parent_span_id,
            kind=kind,
            name=name,
            attributes=attributes or {},
        )

    def end(self, status: SpanStatus = SpanStatus.SUCCESS, error: Optional[str] = None) -> None:
        self.ended_at = time.time()
        self.status = status
        self.error = error

    def add_event(self, name: str, attributes: Optional[Dict[str, Any]] = None) -> None:
        self.events.append({"name": name, "ts": time.time(), "attrs": attributes or {}})

    def duration_ms(self) -> Optional[float]:
        if self.ended_at is None:
            return None
        return round((self.ended_at - self.started_at) * 1000, 2)
```

## Solution 2: Trace Context Propagator

```python
import contextvars
from dataclasses import dataclass
from typing import Dict, Optional


@dataclass
class TraceContext:
    trace_id: str
    current_span_id: str


_TRACE_CTX: contextvars.ContextVar[Optional[TraceContext]] = contextvars.ContextVar(
    "trace_ctx", default=None
)


class TraceContextPropagator:
    """
    Propagates trace context across async boundaries using contextvars.
    Each agent or tool call reads the ambient context to set its parent span.
    Serializes context to/from headers for cross-process propagation.
    """

    @staticmethod
    def set(ctx: TraceContext) -> contextvars.Token:
        return _TRACE_CTX.set(ctx)

    @staticmethod
    def get() -> Optional[TraceContext]:
        return _TRACE_CTX.get()

    @staticmethod
    def reset(token: contextvars.Token) -> None:
        _TRACE_CTX.reset(token)

    @staticmethod
    def to_headers(ctx: TraceContext) -> Dict[str, str]:
        return {
            "x-trace-id": ctx.trace_id,
            "x-parent-span-id": ctx.current_span_id,
        }

    @staticmethod
    def from_headers(headers: Dict[str, str]) -> Optional[TraceContext]:
        trace_id = headers.get("x-trace-id")
        span_id = headers.get("x-parent-span-id")
        if trace_id and span_id:
            return TraceContext(trace_id=trace_id, current_span_id=span_id)
        return None
```

## Solution 3: Span Recorder

```python
import threading
from typing import Dict, List, Optional


class SpanRecorder:
    """
    Thread-safe store for all spans in a trace.
    Provides parent-child lookup and subtree extraction.
    """

    def __init__(self):
        self._spans: Dict[str, TraceSpan] = {}
        self._lock = threading.Lock()

    def record(self, span: TraceSpan) -> None:
        with self._lock:
            self._spans[span.span_id] = span

    def get(self, span_id: str) -> Optional[TraceSpan]:
        return self._spans.get(span_id)

    def children_of(self, parent_span_id: str) -> List[TraceSpan]:
        return [
            s for s in self._spans.values()
            if s.parent_span_id == parent_span_id
        ]

    def all_spans(self) -> List[TraceSpan]:
        with self._lock:
            return list(self._spans.values())

    def by_trace(self, trace_id: str) -> List[TraceSpan]:
        return [s for s in self._spans.values() if s.trace_id == trace_id]

    def subtree(self, root_span_id: str) -> List[TraceSpan]:
        """BFS from root_span_id; returns root + all descendants."""
        result = []
        queue = [root_span_id]
        while queue:
            current = queue.pop(0)
            span = self._spans.get(current)
            if span:
                result.append(span)
                queue.extend(c.span_id for c in self.children_of(current))
        return result
```

## Solution 4: Agent Tracer

```python
import asyncio
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator, Callable, Dict, Optional


class AgentTracer:
    """
    Context-manager-based tracer for multi-agent workflows.
    Creates spans automatically, propagates context to child agents,
    and records all spans to the shared SpanRecorder.
    """

    def __init__(self, recorder: SpanRecorder):
        self._recorder = recorder

    def new_trace(self) -> str:
        return uuid.uuid4().hex

    @asynccontextmanager
    async def start_span(
        self,
        name: str,
        kind: SpanKind,
        attributes: Optional[Dict[str, Any]] = None,
    ) -> AsyncGenerator[TraceSpan, None]:
        ctx = TraceContextPropagator.get()

        if ctx is None:
            trace_id = self.new_trace()
            parent_span_id = None
        else:
            trace_id = ctx.trace_id
            parent_span_id = ctx.current_span_id

        span = TraceSpan.create(
            name=name,
            kind=kind,
            trace_id=trace_id,
            parent_span_id=parent_span_id,
            attributes=attributes or {},
        )
        self._recorder.record(span)

        new_ctx = TraceContext(trace_id=trace_id, current_span_id=span.span_id)
        token = TraceContextPropagator.set(new_ctx)
        try:
            yield span
            span.end(SpanStatus.SUCCESS)
        except Exception as exc:
            span.end(SpanStatus.FAILED, error=str(exc)[:300])
            raise
        finally:
            TraceContextPropagator.reset(token)

    async def trace_tool_call(
        self,
        tool_name: str,
        tool_fn: Callable,
        **kwargs: Any,
    ) -> Any:
        async with self.start_span(
            name=f"tool:{tool_name}",
            kind=SpanKind.TOOL_CALL,
            attributes={"tool_name": tool_name, "args": list(kwargs.keys())},
        ) as span:
            result = await tool_fn(**kwargs)
            span.attributes["result_type"] = type(result).__name__
            return result
```

## Solution 5: DAG Reconstructor

```python
from typing import Any, Dict, List, Optional, Tuple


class DAGNode:
    def __init__(self, span: TraceSpan):
        self.span = span
        self.children: List["DAGNode"] = []

    def to_dict(self, depth: int = 0) -> dict:
        return {
            "span_id": self.span.span_id,
            "name": self.span.name,
            "kind": self.span.kind,
            "status": self.span.status,
            "duration_ms": self.span.duration_ms(),
            "depth": depth,
            "children": [c.to_dict(depth + 1) for c in self.children],
        }


class ExecutionDAGReconstructor:
    """
    Rebuilds the causal execution DAG from a flat list of spans.
    Identifies the critical path (longest chain of sequential spans).
    """

    def __init__(self, recorder: SpanRecorder):
        self._recorder = recorder

    def reconstruct(self, trace_id: str) -> Optional[DAGNode]:
        spans = self._recorder.by_trace(trace_id)
        if not spans:
            return None

        by_id = {s.span_id: DAGNode(s) for s in spans}
        root = None
        for span in spans:
            node = by_id[span.span_id]
            if span.parent_span_id and span.parent_span_id in by_id:
                by_id[span.parent_span_id].children.append(node)
            elif span.parent_span_id is None:
                root = node

        return root

    def critical_path(self, root: DAGNode) -> List[DAGNode]:
        """Returns the path from root to the slowest leaf by cumulative duration."""
        def _longest(node: DAGNode) -> Tuple[float, List[DAGNode]]:
            dur = node.span.duration_ms() or 0.0
            if not node.children:
                return dur, [node]
            best_dur, best_path = max(
                (_longest(c) for c in node.children),
                key=lambda x: x[0],
            )
            return dur + best_dur, [node] + best_path

        _, path = _longest(root)
        return path

    def summary(self, trace_id: str) -> dict:
        spans = self._recorder.by_trace(trace_id)
        root = self.reconstruct(trace_id)
        failed = [s for s in spans if s.status == SpanStatus.FAILED]
        tool_spans = [s for s in spans if s.kind == SpanKind.TOOL_CALL]
        agent_spans = [s for s in spans if s.kind == SpanKind.AGENT]

        critical = []
        if root:
            critical = [n.span.name for n in self.critical_path(root)]

        return {
            "trace_id": trace_id,
            "total_spans": len(spans),
            "agent_count": len(agent_spans),
            "tool_call_count": len(tool_spans),
            "failed_spans": [s.name for s in failed],
            "critical_path": critical,
            "dag": root.to_dict() if root else None,
        }
```

## Solution 6: Trace Export and Alert Manager

```python
import json
import time
from typing import Callable, List, Optional


class TraceExporter:
    """
    Serializes completed traces to JSONL for ingestion into
    external observability systems (Jaeger, Tempo, DataDog).
    Also fires alerts when traces contain failures or exceed latency SLOs.
    """

    def __init__(
        self,
        recorder: SpanRecorder,
        reconstructor: ExecutionDAGReconstructor,
        latency_slo_ms: float = 30000.0,
    ):
        self._recorder = recorder
        self._reconstructor = reconstructor
        self._slo_ms = latency_slo_ms
        self._alert_handlers: List[Callable[[dict], None]] = []

    def add_alert_handler(self, fn: Callable[[dict], None]) -> None:
        self._alert_handlers.append(fn)

    def export_trace(self, trace_id: str) -> List[str]:
        """Returns list of JSONL lines, one per span."""
        spans = self._recorder.by_trace(trace_id)
        lines = []
        for span in spans:
            record = {
                "trace_id": span.trace_id,
                "span_id": span.span_id,
                "parent_span_id": span.parent_span_id,
                "name": span.name,
                "kind": span.kind,
                "status": span.status,
                "started_at": span.started_at,
                "ended_at": span.ended_at,
                "duration_ms": span.duration_ms(),
                "attributes": span.attributes,
                "events": span.events,
                "error": span.error,
            }
            lines.append(json.dumps(record))
        return lines

    def check_and_alert(self, trace_id: str) -> List[dict]:
        summary = self._reconstructor.summary(trace_id)
        alerts = []

        if summary["failed_spans"]:
            alerts.append({
                "type": "trace_failure",
                "trace_id": trace_id,
                "failed_spans": summary["failed_spans"],
                "severity": "critical",
            })

        spans = self._recorder.by_trace(trace_id)
        root_spans = [s for s in spans if s.parent_span_id is None]
        for root in root_spans:
            dur = root.duration_ms() or 0.0
            if dur > self._slo_ms:
                alerts.append({
                    "type": "latency_slo_breach",
                    "trace_id": trace_id,
                    "root_span": root.name,
                    "duration_ms": dur,
                    "slo_ms": self._slo_ms,
                    "severity": "warning",
                })

        for alert in alerts:
            for handler in self._alert_handlers:
                try:
                    handler(alert)
                except Exception:
                    pass

        return alerts
```

## Comparison

| Approach | Span Creation | Context Propagation | DAG Reconstruction | Critical Path | Export/Alert |
|---|---|---|---|---|---|
| TraceSpan | Yes | No | No | No | No |
| TraceContextPropagator | No | Yes (contextvars + headers) | No | No | No |
| SpanRecorder | No | No | Partial (subtree) | No | No |
| AgentTracer | Yes (context manager) | Via propagator | No | No | No |
| ExecutionDAGReconstructor | No | No | Yes | Yes | No |
| TraceExporter | No | No | Via reconstructor | Via reconstructor | Yes (JSONL + alerts) |

**Best for production**: Wrap every agent entry point and every tool call with `AgentTracer.start_span()` — this single change gives you the full parent-child graph without any manual ID threading. Use `TraceContextPropagator.to_headers()` when spawning sub-agents over HTTP or message queues, and `from_headers()` at the receiving end, to propagate the trace across process boundaries. Call `ExecutionDAGReconstructor.summary()` at the end of every orchestration run and emit it to your metrics system — the critical path tells you immediately which agent or tool is the bottleneck. Wire `TraceExporter.check_and_alert()` to PagerDuty for `trace_failure` alerts and Slack for `latency_slo_breach` warnings.
