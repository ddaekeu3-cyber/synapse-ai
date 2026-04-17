---
title: "Agent Doesn't Implement Distributed Trace Propagation Across Tool Calls"
description: "Agents that invoke tools as HTTP or RPC calls without propagating trace context produce disconnected spans: the agent's trace ends at the tool call boundary and the downstream service starts a new unrelated trace, making it impossible to reconstruct end-to-end latency or identify which agent request caused a downstream error. Implement W3C Trace Context propagation that carries trace ID and span ID through every tool call header, links child spans to the agent's root span, and produces a complete end-to-end trace."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-distributed-trace-propagation-across-tool-calls
tags: [distributed-tracing, trace-propagation, w3c-trace-context, span-linking, opentelemetry, tool-call-tracing]
symptoms:
  - "Tool call spans appear as orphaned root spans in Jaeger/Zipkin — not linked to the agent trace"
  - "Cannot determine end-to-end latency for an agent task because traces are fragmented"
  - "A downstream service error cannot be traced back to the specific agent session that caused it"
  - "Each tool call starts a fresh trace with no parent context"
  - "No correlation between agent request IDs and downstream service logs"
---

## Why This Happens

Distributed tracing requires two things: creating spans for each unit of work, and propagating the current trace context to downstream services via HTTP headers (W3C `traceparent`/`tracestate` or B3). Most agent frameworks create a span for the agent invocation but pass no trace headers when calling tool HTTP endpoints, message queues, or RPC services. The downstream service has no parent span to attach to, so it starts a new root span. The two traces exist independently in the trace backend and cannot be joined. Fixing this requires injecting trace context into every outbound tool call and extracting it at the receiving end.

## Solution 1: Trace Context

```python
import os
import random
import time
from dataclasses import dataclass, field
from typing import Optional


def _random_hex(n_bytes: int) -> str:
    return os.urandom(n_bytes).hex()


@dataclass
class TraceContext:
    trace_id: str                           # 16-byte hex (128-bit)
    span_id: str                            # 8-byte hex (64-bit)
    parent_span_id: Optional[str] = None
    sampled: bool = True
    trace_state: str = ""
    started_at: float = field(default_factory=time.monotonic)

    @classmethod
    def new_root(cls, sampled: bool = True) -> "TraceContext":
        return cls(
            trace_id=_random_hex(16),
            span_id=_random_hex(8),
            sampled=sampled,
        )

    def child_span(self) -> "TraceContext":
        return TraceContext(
            trace_id=self.trace_id,
            span_id=_random_hex(8),
            parent_span_id=self.span_id,
            sampled=self.sampled,
            trace_state=self.trace_state,
        )

    def traceparent_header(self) -> str:
        flags = "01" if self.sampled else "00"
        return f"00-{self.trace_id}-{self.span_id}-{flags}"

    @classmethod
    def from_traceparent(cls, header: str, trace_state: str = "") -> Optional["TraceContext"]:
        parts = header.strip().split("-")
        if len(parts) < 4 or parts[0] != "00":
            return None
        return cls(
            trace_id=parts[1],
            span_id=_random_hex(8),   # new span for this service
            parent_span_id=parts[2],
            sampled=parts[3] == "01",
            trace_state=trace_state,
        )
```

## Solution 2: Span Recorder

```python
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class SpanStatus(str, Enum):
    OK = "ok"
    ERROR = "error"
    UNSET = "unset"


@dataclass
class Span:
    name: str
    trace_id: str
    span_id: str
    parent_span_id: Optional[str]
    started_at: float
    ended_at: Optional[float] = None
    status: SpanStatus = SpanStatus.UNSET
    attributes: Dict[str, Any] = field(default_factory=dict)
    events: List[dict] = field(default_factory=list)

    @property
    def duration_ms(self) -> Optional[float]:
        if self.ended_at is None:
            return None
        return round((self.ended_at - self.started_at) * 1000, 2)

    def end(self, status: SpanStatus = SpanStatus.OK, error: str = "") -> None:
        self.ended_at = time.monotonic()
        self.status = status
        if error:
            self.attributes["error.message"] = error

    def add_event(self, name: str, attributes: Dict[str, Any] = None) -> None:
        self.events.append({"name": name, "ts": time.monotonic(), "attributes": attributes or {}})

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "started_at": self.started_at,
            "duration_ms": self.duration_ms,
            "status": self.status.value,
            "attributes": self.attributes,
            "events": self.events,
        }


class SpanRecorder:
    """
    In-process span collector. In production, replace with an OTLP exporter.
    """

    def __init__(self, max_spans: int = 10000):
        self._max = max_spans
        self._spans: List[Span] = []

    def record(self, span: Span) -> None:
        if len(self._spans) >= self._max:
            self._spans.pop(0)
        self._spans.append(span)

    def get_trace(self, trace_id: str) -> List[Span]:
        return [s for s in self._spans if s.trace_id == trace_id]

    def span_count(self) -> int:
        return len(self._spans)
```

## Solution 3: Tracer

```python
import time
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional


class AgentTracer:
    """
    Creates and manages spans for agent operations. Maintains a current
    context stack so child spans are automatically linked to their parent.
    """

    def __init__(self, recorder: SpanRecorder, service_name: str = "agent"):
        self._recorder = recorder
        self._service = service_name
        self._current: Optional[TraceContext] = None

    def start_trace(self, operation_name: str, attributes: Dict[str, Any] = None) -> Span:
        ctx = TraceContext.new_root()
        self._current = ctx
        span = Span(
            name=operation_name,
            trace_id=ctx.trace_id,
            span_id=ctx.span_id,
            parent_span_id=None,
            started_at=time.monotonic(),
            attributes={"service.name": self._service, **(attributes or {})},
        )
        return span

    def start_child_span(self, operation_name: str, attributes: Dict[str, Any] = None) -> tuple:
        """Returns (Span, child TraceContext)."""
        if self._current is None:
            ctx = TraceContext.new_root()
            self._current = ctx
        child_ctx = self._current.child_span()
        span = Span(
            name=operation_name,
            trace_id=child_ctx.trace_id,
            span_id=child_ctx.span_id,
            parent_span_id=child_ctx.parent_span_id,
            started_at=time.monotonic(),
            attributes={"service.name": self._service, **(attributes or {})},
        )
        return span, child_ctx

    def finish_span(self, span: Span, status: SpanStatus = SpanStatus.OK, error: str = "") -> None:
        span.end(status, error)
        self._recorder.record(span)

    def current_context(self) -> Optional[TraceContext]:
        return self._current

    def inject_headers(self, child_ctx: TraceContext) -> dict:
        headers = {"traceparent": child_ctx.traceparent_header()}
        if child_ctx.trace_state:
            headers["tracestate"] = child_ctx.trace_state
        return headers
```

## Solution 4: Trace-Propagating Tool Executor

```python
import time
from typing import Any, Callable, Dict, Optional


class TracePropagatingToolExecutor:
    """
    Executes tool calls with W3C trace context injected into outbound headers.
    Creates a child span for each tool call and records it in the tracer.
    """

    def __init__(self, tracer: AgentTracer):
        self._tracer = tracer

    async def execute(
        self,
        tool_name: str,
        tool_fn: Callable,
        *args: Any,
        extra_headers: Optional[Dict[str, str]] = None,
        **kwargs: Any,
    ) -> dict:
        span, child_ctx = self._tracer.start_child_span(
            f"tool/{tool_name}",
            attributes={"tool.name": tool_name},
        )
        trace_headers = self._tracer.inject_headers(child_ctx)
        if extra_headers:
            trace_headers.update(extra_headers)

        span.add_event("tool.start")
        try:
            # Pass headers to the tool function — tools that make HTTP calls
            # should forward these headers to downstream services
            result = await tool_fn(*args, headers=trace_headers, **kwargs)
            span.attributes["tool.outcome"] = "success"
            self._tracer.finish_span(span, SpanStatus.OK)
            return {
                "result": result,
                "trace_id": child_ctx.trace_id,
                "span_id": child_ctx.span_id,
            }
        except Exception as exc:
            span.attributes["tool.outcome"] = "error"
            self._tracer.finish_span(span, SpanStatus.ERROR, str(exc))
            raise
```

## Solution 5: Inbound Trace Extractor

```python
from typing import Optional


class InboundTraceExtractor:
    """
    Extracts W3C trace context from inbound webhook or callback headers.
    Used by tool servers that receive calls from the agent and need to
    continue the trace on the server side.
    """

    def extract(self, headers: dict) -> Optional[TraceContext]:
        traceparent = headers.get("traceparent") or headers.get("Traceparent")
        tracestate = headers.get("tracestate", "") or headers.get("Tracestate", "")
        if not traceparent:
            return None
        return TraceContext.from_traceparent(traceparent, tracestate)

    def extract_or_new(self, headers: dict) -> TraceContext:
        ctx = self.extract(headers)
        return ctx if ctx is not None else TraceContext.new_root()
```

## Solution 6: Trace Summary Reporter

```python
import time
from typing import List, Optional


class TraceSummaryReporter:
    """
    Summarizes a complete agent trace: total duration, span count,
    slowest spans, and any error spans.
    """

    def __init__(self, recorder: SpanRecorder):
        self._recorder = recorder

    def report(self, trace_id: str) -> dict:
        spans = self._recorder.get_trace(trace_id)
        if not spans:
            return {"trace_id": trace_id, "found": False}

        completed = [s for s in spans if s.ended_at is not None]
        error_spans = [s for s in completed if s.status == SpanStatus.ERROR]

        total_duration_ms = None
        root_spans = [s for s in completed if s.parent_span_id is None]
        if root_spans:
            root = root_spans[0]
            total_duration_ms = root.duration_ms

        slowest = sorted(completed, key=lambda s: s.duration_ms or 0, reverse=True)[:5]

        return {
            "trace_id": trace_id,
            "found": True,
            "span_count": len(spans),
            "error_span_count": len(error_spans),
            "total_duration_ms": total_duration_ms,
            "slowest_spans": [
                {"name": s.name, "duration_ms": s.duration_ms, "span_id": s.span_id}
                for s in slowest
            ],
            "error_spans": [
                {"name": s.name, "error": s.attributes.get("error.message"), "span_id": s.span_id}
                for s in error_spans
            ],
        }
```

## Comparison

| Approach | Trace ID Generation | W3C Header Injection | Child Span Linking | Server-Side Extraction | Trace Summary |
|---|---|---|---|---|---|
| TraceContext | Yes (128-bit) | Yes (traceparent) | Yes (child_span()) | Via from_traceparent | No |
| SpanRecorder | No | No | No | No | No |
| AgentTracer | Via TraceContext | Yes (inject_headers) | Yes (start_child_span) | No | No |
| TracePropagatingToolExecutor | No | Via tracer | Via tracer | No | No |
| InboundTraceExtractor | No | No | No | Yes | No |
| TraceSummaryReporter | No | No | No | No | Yes |

**Best for production**: Replace `SpanRecorder` with an OTLP exporter pointed at your trace backend (Jaeger, Tempo, Honeycomb) — the in-process recorder is for development only. Always propagate both `traceparent` and `tracestate` headers: `tracestate` carries vendor-specific sampling decisions and dropping it breaks Datadog and Honeycomb's sampling pipelines. Set span names as `tool/<tool_name>` rather than just the tool name so span hierarchies are readable at a glance in the trace UI. Attach `session_id` and `user_id` as span attributes on the root span: this is the critical link between a trace and a user complaint, and without it post-incident analysis requires correlating timestamps across multiple systems.
