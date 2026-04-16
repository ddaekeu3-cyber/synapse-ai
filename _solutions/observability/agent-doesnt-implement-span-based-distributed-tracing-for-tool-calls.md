---
title: "Agent Doesn't Implement Span-Based Distributed Tracing for Tool Calls"
description: "Agents that log tool calls as isolated events cannot reconstruct the causal chain across a multi-step tool execution: which tool call triggered which downstream call, how latency accumulated across the chain, and where a failure originated. Implement span-based distributed tracing that propagates trace context through tool calls, records parent-child relationships, and produces a flame-graph-compatible trace for every agent turn."
date: 2026-04-16
difficulty: advanced
category: observability
slug: agent-doesnt-implement-span-based-distributed-tracing-for-tool-calls
tags: [distributed-tracing, spans, trace-context, tool-call-chain, flame-graph, opentelemetry]
symptoms:
  - "No way to reconstruct the sequence and causality of tool calls within a single agent turn"
  - "Latency breakdown across a multi-tool turn is unavailable — only total turn latency is measured"
  - "A failure in tool call 5 of 8 cannot be linked back to which earlier call triggered it"
  - "Tool calls appear as isolated log lines with no parent-child relationship"
  - "Cannot tell whether a slow turn was caused by one slow tool or many parallel slow tools"
---

## Why This Happens

Logging individual tool call start and end times produces a flat list of events with no causal structure. A distributed trace — a tree of spans where each span records a unit of work, its parent span, and timing — provides the causal structure needed to diagnose latency and failure. Span-based tracing requires propagating a trace context (trace ID + parent span ID) through every tool call and sub-call, recording span start/end times, and storing the spans in a way that allows the full trace to be reconstructed for a given turn.

## Solution 1: Trace Context Model

```python
import secrets
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class SpanStatus(str, Enum):
    OK = "ok"
    ERROR = "error"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"


@dataclass
class TraceContext:
    trace_id: str
    span_id: str
    parent_span_id: Optional[str] = None
    baggage: Dict[str, str] = field(default_factory=dict)

    @staticmethod
    def new_trace() -> "TraceContext":
        return TraceContext(
            trace_id=secrets.token_hex(16),
            span_id=secrets.token_hex(8),
        )

    def child_span(self) -> "TraceContext":
        return TraceContext(
            trace_id=self.trace_id,
            span_id=secrets.token_hex(8),
            parent_span_id=self.span_id,
            baggage=dict(self.baggage),
        )


@dataclass
class Span:
    trace_id: str
    span_id: str
    parent_span_id: Optional[str]
    name: str
    start_time: float
    end_time: Optional[float] = None
    status: SpanStatus = SpanStatus.OK
    attributes: Dict[str, Any] = field(default_factory=dict)
    events: List[dict] = field(default_factory=list)
    error: Optional[str] = None

    @property
    def duration_ms(self) -> Optional[float]:
        if self.end_time is None:
            return None
        return round((self.end_time - self.start_time) * 1000, 2)

    def add_event(self, name: str, attrs: Optional[Dict[str, Any]] = None) -> None:
        self.events.append({
            "name": name,
            "timestamp": time.time(),
            "attributes": attrs or {},
        })

    def finish(self, status: SpanStatus = SpanStatus.OK, error: str = "") -> None:
        self.end_time = time.time()
        self.status = status
        if error:
            self.error = error
```

## Solution 2: Span Recorder

```python
import time
from collections import defaultdict
from threading import Lock
from typing import Dict, List, Optional


class SpanRecorder:
    """
    Records completed spans in memory, indexed by trace_id.
    Evicts traces older than TTL to bound memory usage.
    """

    def __init__(self, max_traces: int = 1000, ttl_seconds: float = 3600.0):
        self._max = max_traces
        self._ttl = ttl_seconds
        self._traces: Dict[str, List[Span]] = defaultdict(list)
        self._trace_created: Dict[str, float] = {}
        self._lock = Lock()

    def record(self, span: Span) -> None:
        with self._lock:
            self._traces[span.trace_id].append(span)
            if span.trace_id not in self._trace_created:
                self._trace_created[span.trace_id] = time.time()
            self._evict()

    def get_trace(self, trace_id: str) -> List[Span]:
        with self._lock:
            return list(self._traces.get(trace_id, []))

    def _evict(self) -> None:
        if len(self._traces) <= self._max:
            return
        cutoff = time.time() - self._ttl
        stale = [
            tid for tid, ts in self._trace_created.items()
            if ts < cutoff
        ]
        # Evict oldest first if still over limit
        if not stale:
            oldest = sorted(self._trace_created.items(), key=lambda x: x[1])
            stale = [tid for tid, _ in oldest[:len(self._traces) - self._max]]
        for tid in stale:
            self._traces.pop(tid, None)
            self._trace_created.pop(tid, None)
```

## Solution 3: Span Builder Context Manager

```python
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator, Dict, Optional


class SpanBuilder:
    """
    Context manager that creates a child span, records it on exit,
    and propagates the child context for further nesting.
    """

    def __init__(self, recorder: SpanRecorder):
        self._recorder = recorder

    @asynccontextmanager
    async def span(
        self,
        name: str,
        parent_ctx: TraceContext,
        attributes: Optional[Dict[str, Any]] = None,
    ) -> AsyncGenerator[tuple, None]:
        child_ctx = parent_ctx.child_span()
        s = Span(
            trace_id=child_ctx.trace_id,
            span_id=child_ctx.span_id,
            parent_span_id=child_ctx.parent_span_id,
            name=name,
            start_time=time.time(),
            attributes=attributes or {},
        )
        try:
            yield s, child_ctx
            s.finish(SpanStatus.OK)
        except Exception as exc:
            s.finish(SpanStatus.ERROR, error=str(exc))
            raise
        finally:
            self._recorder.record(s)
```

## Solution 4: Traced Tool Call Executor

```python
import time
from typing import Any, Callable


class TracedToolCallExecutor:
    """
    Wraps tool call execution with span recording.
    Each tool call becomes a child span of the current turn's root span.
    """

    def __init__(self, span_builder: SpanBuilder):
        self._builder = span_builder

    async def execute(
        self,
        tool_name: str,
        parent_ctx: TraceContext,
        fn: Callable,
        *args: Any,
        **kwargs: Any,
    ) -> dict:
        async with self._builder.span(
            name=f"tool:{tool_name}",
            parent_ctx=parent_ctx,
            attributes={"tool.name": tool_name},
        ) as (span, child_ctx):
            span.add_event("tool_call_start", {"args_keys": list(kwargs.keys())})
            result = await fn(*args, **kwargs)
            span.add_event("tool_call_end")
            span.attributes["tool.success"] = True
            return {
                "result": result,
                "trace_context": child_ctx,
                "span_id": span.span_id,
                "duration_ms": span.duration_ms,
            }
```

## Solution 5: Trace Reconstructor

```python
from typing import Dict, List, Optional


class TraceReconstructor:
    """
    Reconstructs a span tree from a flat list of spans for a given trace.
    Produces a nested dict representation compatible with flame graph renderers.
    """

    def reconstruct(self, spans: List[Span]) -> Optional[dict]:
        if not spans:
            return None

        by_id: Dict[str, Span] = {s.span_id: s for s in spans}
        children: Dict[str, List[str]] = {s.span_id: [] for s in spans}

        root_id = None
        for span in spans:
            if span.parent_span_id and span.parent_span_id in by_id:
                children[span.parent_span_id].append(span.span_id)
            else:
                root_id = span.span_id

        if root_id is None:
            root_id = min(spans, key=lambda s: s.start_time).span_id

        return self._build_node(root_id, by_id, children)

    def _build_node(
        self,
        span_id: str,
        by_id: Dict[str, Span],
        children: Dict[str, List[str]],
    ) -> dict:
        span = by_id[span_id]
        child_nodes = [
            self._build_node(cid, by_id, children)
            for cid in sorted(children.get(span_id, []),
                               key=lambda cid: by_id[cid].start_time)
        ]
        return {
            "span_id": span.span_id,
            "name": span.name,
            "duration_ms": span.duration_ms,
            "status": span.status.value,
            "error": span.error,
            "attributes": span.attributes,
            "children": child_nodes,
            "child_count": len(child_nodes),
        }
```

## Solution 6: Trace Summary Dashboard

```python
import time
from typing import List, Optional


class TraceSummaryDashboard:
    """
    Computes per-trace statistics: total duration, slowest span,
    error spans, and depth — useful for latency breakdown reports.
    """

    def __init__(
        self,
        recorder: SpanRecorder,
        reconstructor: TraceReconstructor,
    ):
        self._recorder = recorder
        self._reconstructor = reconstructor

    def summarize_trace(self, trace_id: str) -> Optional[dict]:
        spans = self._recorder.get_trace(trace_id)
        if not spans:
            return None

        finished = [s for s in spans if s.end_time is not None]
        errors = [s for s in finished if s.status == SpanStatus.ERROR]
        slowest = max(finished, key=lambda s: s.duration_ms or 0, default=None)

        tree = self._reconstructor.reconstruct(spans)

        return {
            "trace_id": trace_id,
            "span_count": len(spans),
            "total_duration_ms": (
                round((max(s.end_time or 0 for s in finished) -
                       min(s.start_time for s in spans)) * 1000, 2)
                if finished else None
            ),
            "error_count": len(errors),
            "error_spans": [s.name for s in errors],
            "slowest_span": {
                "name": slowest.name,
                "duration_ms": slowest.duration_ms,
            } if slowest else None,
            "tree": tree,
            "generated_at": time.time(),
        }
```

## Comparison

| Approach | Trace Context Propagation | Span Recording | Async Context Manager | Tree Reconstruction | Dashboard |
|---|---|---|---|---|---|
| TraceContext / Span | Yes (model) | No | No | No | No |
| SpanRecorder | No | Yes (TTL+LRU) | No | No | No |
| SpanBuilder | Via context | Via recorder | Yes | No | No |
| TracedToolCallExecutor | Yes | Via builder | Via builder | No | No |
| TraceReconstructor | No | No | No | Yes (nested) | No |
| TraceSummaryDashboard | No | No | No | Via reconstructor | Yes |

**Best for production**: Create a root span at the start of each agent turn and pass `TraceContext` through every tool call — do not use a global thread-local context since async agents run concurrent turns. Export completed traces to an OpenTelemetry-compatible collector (Jaeger, Honeycomb, Datadog) by serializing spans in OTLP format; the `SpanRecorder` in-memory store is a development fallback only. Set `ttl_seconds=3600` in `SpanRecorder` to retain the last hour of traces for incident investigation. Use `TraceSummaryDashboard.summarize_trace()` in your P95 latency alert handler: when a turn exceeds the SLO, the trace pinpoints whether the slow span was an LLM call, a database lookup, or a downstream HTTP call.
