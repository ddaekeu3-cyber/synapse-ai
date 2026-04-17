---
title: "Agent Doesn't Implement Distributed Trace Propagation Across Tool Calls"
description: "Agents that generate traces without propagating trace context across tool calls produce disconnected spans: the LLM call has one trace ID, each tool call has its own unrelated trace ID, and the webhook notification has a third. Reconstructing the full request flow requires manual correlation by timestamp. Implement W3C TraceContext propagation so every operation in a request shares a single trace ID and spans form a complete causal tree."
date: 2026-04-16
difficulty: advanced
category: observability
slug: agent-doesnt-implement-distributed-trace-propagation-across-tool-calls
tags: [distributed-tracing, trace-propagation, opentelemetry, w3c-tracecontext, span-correlation, request-flow]
symptoms:
  - "Tool call spans appear as disconnected root spans with no parent trace"
  - "Cannot reconstruct full request flow from traces — LLM call and tool calls are isolated"
  - "External service traces do not link back to the originating agent request"
  - "Trace IDs are not forwarded in HTTP headers when tools make outbound calls"
  - "Waterfall views in Jaeger or Zipkin show isolated spans instead of a unified tree"
---

## Why This Happens

Distributed tracing requires two things: generating spans with parent-child relationships, and propagating trace context across process and service boundaries. Most agents generate spans for individual operations but do not propagate the trace context when those operations make outbound calls. A tool that fetches data from an external API must inject `traceparent` headers so the external service's trace links back to the agent's root span. Without propagation, every hop starts a new trace and the end-to-end request flow is invisible.

## Solution 1: Trace Context

```python
import os
import random
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class TraceContext:
    """W3C TraceContext-compatible trace context."""
    trace_id: str          # 16-byte hex (32 chars)
    span_id: str           # 8-byte hex (16 chars)
    trace_flags: str = "01"   # sampled
    parent_span_id: Optional[str] = None
    baggage: dict = field(default_factory=dict)

    @classmethod
    def new_root(cls) -> "TraceContext":
        return cls(
            trace_id=os.urandom(16).hex(),
            span_id=os.urandom(8).hex(),
        )

    def child(self, new_span_id: Optional[str] = None) -> "TraceContext":
        return TraceContext(
            trace_id=self.trace_id,
            span_id=new_span_id or os.urandom(8).hex(),
            trace_flags=self.trace_flags,
            parent_span_id=self.span_id,
            baggage=dict(self.baggage),
        )

    @property
    def traceparent(self) -> str:
        return f"00-{self.trace_id}-{self.span_id}-{self.trace_flags}"

    @classmethod
    def from_traceparent(cls, header: str) -> Optional["TraceContext"]:
        parts = header.split("-")
        if len(parts) != 4 or parts[0] != "00":
            return None
        return cls(
            trace_id=parts[1],
            span_id=parts[2],
            trace_flags=parts[3],
        )

    def to_headers(self) -> dict:
        headers = {"traceparent": self.traceparent}
        if self.baggage:
            headers["baggage"] = ",".join(f"{k}={v}" for k, v in self.baggage.items())
        return headers
```

## Solution 2: Span Recorder

```python
import time
from dataclasses import dataclass, field
from enum import Enum
from threading import Lock
from typing import Any, Dict, List, Optional


class SpanStatus(str, Enum):
    OK = "ok"
    ERROR = "error"
    UNSET = "unset"


@dataclass
class Span:
    trace_id: str
    span_id: str
    parent_span_id: Optional[str]
    operation_name: str
    start_time: float = field(default_factory=time.time)
    end_time: Optional[float] = None
    status: SpanStatus = SpanStatus.UNSET
    attributes: Dict[str, Any] = field(default_factory=dict)
    events: List[dict] = field(default_factory=list)
    error_message: str = ""

    @property
    def duration_ms(self) -> Optional[float]:
        if self.end_time is None:
            return None
        return round((self.end_time - self.start_time) * 1000, 2)

    def finish(self, status: SpanStatus = SpanStatus.OK, error: str = "") -> None:
        self.end_time = time.time()
        self.status = status
        self.error_message = error

    def add_event(self, name: str, attributes: dict = None) -> None:
        self.events.append({
            "name": name,
            "ts": time.time(),
            "attributes": attributes or {},
        })

    def to_dict(self) -> dict:
        return {
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "operation": self.operation_name,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "duration_ms": self.duration_ms,
            "status": self.status.value,
            "attributes": self.attributes,
            "events": self.events,
            "error": self.error_message,
        }


class SpanRecorder:
    """In-memory span store for development and testing."""

    def __init__(self, max_spans: int = 50000):
        self._spans: List[Span] = []
        self._max = max_spans
        self._lock = Lock()

    def record(self, span: Span) -> None:
        with self._lock:
            self._spans.append(span)
            if len(self._spans) > self._max:
                self._spans.pop(0)

    def by_trace(self, trace_id: str) -> List[Span]:
        with self._lock:
            return [s for s in self._spans if s.trace_id == trace_id]

    def recent(self, n: int = 100) -> List[Span]:
        with self._lock:
            return list(self._spans[-n:])
```

## Solution 3: Tracer

```python
import contextlib
import time
from typing import Any, Dict, Optional


class AgentTracer:
    """
    Creates and manages spans with W3C TraceContext propagation.
    Integrates with SpanRecorder for export.
    """

    def __init__(self, service_name: str, recorder: SpanRecorder):
        self._service = service_name
        self._recorder = recorder

    def start_span(
        self,
        operation_name: str,
        context: Optional[TraceContext] = None,
        attributes: Optional[Dict[str, Any]] = None,
    ) -> tuple:
        """Returns (span, child_context)."""
        if context is None:
            ctx = TraceContext.new_root()
            span = Span(
                trace_id=ctx.trace_id,
                span_id=ctx.span_id,
                parent_span_id=None,
                operation_name=operation_name,
            )
        else:
            child_ctx = context.child()
            span = Span(
                trace_id=child_ctx.trace_id,
                span_id=child_ctx.span_id,
                parent_span_id=child_ctx.parent_span_id,
                operation_name=operation_name,
            )
            ctx = child_ctx

        span.attributes["service.name"] = self._service
        if attributes:
            span.attributes.update(attributes)

        return span, ctx

    @contextlib.asynccontextmanager
    async def span(
        self,
        operation_name: str,
        context: Optional[TraceContext] = None,
        attributes: Optional[Dict[str, Any]] = None,
    ):
        span, child_ctx = self.start_span(operation_name, context, attributes)
        try:
            yield span, child_ctx
            span.finish(SpanStatus.OK)
        except Exception as exc:
            span.finish(SpanStatus.ERROR, error=str(exc))
            raise
        finally:
            self._recorder.record(span)
```

## Solution 4: Trace-Propagating Tool Dispatcher

```python
import time
from typing import Any, Callable, Dict, Optional


class TracePropagatingToolDispatcher:
    """
    Wraps tool calls with trace context propagation.
    Injects traceparent headers into HTTP-based tools and
    creates child spans for every tool call.
    """

    def __init__(self, tracer: AgentTracer):
        self._tracer = tracer

    async def dispatch(
        self,
        tool_name: str,
        args: Dict[str, Any],
        fn: Callable,
        parent_context: Optional[TraceContext] = None,
    ) -> dict:
        async with self._tracer.span(
            f"tool.{tool_name}",
            context=parent_context,
            attributes={"tool.name": tool_name},
        ) as (span, child_ctx):
            span.add_event("tool_call_start", {"args_keys": list(args.keys())})

            # Inject trace headers into args if tool accepts them
            if "headers" in args and isinstance(args["headers"], dict):
                args["headers"].update(child_ctx.to_headers())
            elif "_trace_context" in str(args):
                args["_trace_context"] = child_ctx.traceparent

            result = await fn(tool_name=tool_name, args=args, trace_context=child_ctx)
            span.add_event("tool_call_complete")
            span.attributes["tool.success"] = True

            return {
                "result": result,
                "trace_id": child_ctx.trace_id,
                "span_id": child_ctx.span_id,
            }
```

## Solution 5: Trace Context Extractor

```python
from typing import Dict, Optional


class TraceContextExtractor:
    """
    Extracts W3C TraceContext from inbound HTTP headers or message metadata.
    Used to continue a trace started by an upstream caller.
    """

    @staticmethod
    def from_headers(headers: Dict[str, str]) -> Optional[TraceContext]:
        traceparent = headers.get("traceparent") or headers.get("Traceparent")
        if not traceparent:
            return None
        ctx = TraceContext.from_traceparent(traceparent)
        if ctx is None:
            return None

        baggage_header = headers.get("baggage", "")
        if baggage_header:
            for item in baggage_header.split(","):
                if "=" in item:
                    k, v = item.strip().split("=", 1)
                    ctx.baggage[k.strip()] = v.strip()

        return ctx

    @staticmethod
    def from_message(message: dict) -> Optional[TraceContext]:
        """Extracts trace context from agent message metadata."""
        meta = message.get("_trace", {})
        traceparent = meta.get("traceparent", "")
        if not traceparent:
            return None
        return TraceContext.from_traceparent(traceparent)

    @staticmethod
    def inject_into_message(message: dict, context: TraceContext) -> dict:
        message["_trace"] = {"traceparent": context.traceparent}
        return message
```

## Solution 6: Trace Dashboard

```python
import time
from typing import List


class TraceDashboard:
    """
    Provides a summary of recent traces and span health for operational visibility.
    """

    def __init__(self, recorder: SpanRecorder):
        self._recorder = recorder

    def trace_summary(self, trace_id: str) -> dict:
        spans = self._recorder.by_trace(trace_id)
        if not spans:
            return {"trace_id": trace_id, "spans": 0}

        root = next((s for s in spans if s.parent_span_id is None), spans[0])
        errors = [s for s in spans if s.status == SpanStatus.ERROR]
        finished = [s for s in spans if s.end_time is not None]

        return {
            "trace_id": trace_id,
            "root_operation": root.operation_name,
            "span_count": len(spans),
            "error_count": len(errors),
            "total_duration_ms": round(
                (max(s.end_time or 0 for s in finished) - root.start_time) * 1000, 2
            ) if finished else None,
            "services": list({s.attributes.get("service.name", "unknown") for s in spans}),
        }

    def recent_errors(self, n: int = 20) -> List[dict]:
        recent = self._recorder.recent(500)
        error_spans = [s for s in recent if s.status == SpanStatus.ERROR]
        return [s.to_dict() for s in error_spans[-n:]]

    def render(self) -> dict:
        recent = self._recorder.recent(1000)
        total = len(recent)
        errors = sum(1 for s in recent if s.status == SpanStatus.ERROR)
        return {
            "generated_at": time.time(),
            "recent_span_count": total,
            "error_span_count": errors,
            "error_rate": round(errors / max(total, 1), 4),
            "unique_traces": len({s.trace_id for s in recent}),
        }
```

## Comparison

| Approach | W3C TraceContext | Child Span Creation | Header Injection | Context Extraction | Dashboard |
|---|---|---|---|---|---|
| TraceContext | Yes (traceparent) | Yes (.child()) | Yes (.to_headers()) | Yes (from_traceparent) | No |
| AgentTracer | Via context | Yes | No | No | No |
| TracePropagatingToolDispatcher | Via tracer | Via tracer | Yes (headers arg) | No | No |
| TraceContextExtractor | No | No | No | Yes (headers+message) | No |
| TraceDashboard | No | No | No | No | Yes |

**Best for production**: Use OpenTelemetry SDK instead of this custom implementation for production deployments — it handles export to Jaeger, Zipkin, and OTLP collectors automatically. This implementation is valuable for testing propagation logic without SDK dependency. Always propagate `traceparent` in both HTTP headers (for tool calls to external services) and message metadata (for agent-to-agent handoffs). Set `trace_flags="01"` (sampled) on all root spans but use head-based sampling at the entry point to control volume — once a trace is sampled, propagate that decision through all child spans via the flags byte.
