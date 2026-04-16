---
title: "Agent Doesn't Implement Distributed Context Propagation for Cross-Service Traces"
description: "AI agents that call external tools, downstream APIs, or sub-agents lose the trace context at each service boundary. Without W3C TraceContext or B3 header propagation, every outbound call starts a new disconnected trace, making it impossible to reconstruct the full causal chain of a single user request across services. Distributed context propagation threads the trace_id and span_id through every hop automatically."
date: 2025-02-11
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-distributed-context-propagation-for-cross-service-traces
tags:
  - distributed-tracing
  - context-propagation
  - w3c-tracecontext
  - opentelemetry
  - trace-id
  - span-id
  - cross-service
  - observability
symptoms:
  - "Trace in Jaeger/Honeycomb shows only the agent span; tool call spans appear disconnected"
  - "Downstream service logs have no trace_id linking them to the originating user request"
  - "Sub-agent calls start fresh traces with no parent — impossible to correlate with root request"
  - "HTTP headers to external APIs carry no traceparent/tracestate header"
  - "Manual correlation of cross-service failures requires timestamp matching across dashboards"
---

## Problem

A user request handled by an agent may touch 5–10 services: an LLM API, a vector database, a web search tool, a downstream microservice, and one or more sub-agents. Without context propagation, each hop generates an independent trace. Tracing backends show 10 disconnected entries instead of one unified flamegraph. W3C TraceContext (RFC 9546) and OpenTelemetry define exactly how to carry `trace_id` and `span_id` across HTTP, gRPC, and message-queue boundaries.

---

## Solution 1: W3CTraceContext — Parse and Inject traceparent Headers

```python
import os
import secrets
import struct
from dataclasses import dataclass
from typing import Dict, Optional


@dataclass
class SpanContext:
    trace_id: str       # 32 hex chars (128-bit)
    span_id: str        # 16 hex chars (64-bit)
    trace_flags: int = 1   # 01 = sampled

    @property
    def traceparent(self) -> str:
        return f"00-{self.trace_id}-{self.span_id}-{self.trace_flags:02x}"

    @classmethod
    def new_root(cls) -> "SpanContext":
        return cls(
            trace_id=secrets.token_hex(16),
            span_id=secrets.token_hex(8),
        )

    def child(self) -> "SpanContext":
        return SpanContext(
            trace_id=self.trace_id,
            span_id=secrets.token_hex(8),
            trace_flags=self.trace_flags,
        )

    @classmethod
    def from_traceparent(cls, header: str) -> Optional["SpanContext"]:
        parts = header.strip().split("-")
        if len(parts) < 4 or parts[0] != "00":
            return None
        try:
            return cls(
                trace_id=parts[1],
                span_id=parts[2],
                trace_flags=int(parts[3], 16),
            )
        except (ValueError, IndexError):
            return None

    def is_sampled(self) -> bool:
        return bool(self.trace_flags & 1)


class W3CTraceContextPropagator:
    """
    Injects and extracts W3C TraceContext headers for HTTP requests.

    Usage:
        propagator = W3CTraceContextPropagator()
        ctx = SpanContext.new_root()

        # Outbound HTTP call:
        headers = propagator.inject(ctx.child())
        await http_client.get(url, headers=headers)

        # Inbound request handler:
        ctx = propagator.extract(incoming_headers)
        child = ctx.child() if ctx else SpanContext.new_root()
    """

    TRACEPARENT = "traceparent"
    TRACESTATE = "tracestate"

    def inject(self, ctx: SpanContext,
               extra_state: Optional[str] = None) -> Dict[str, str]:
        headers = {self.TRACEPARENT: ctx.traceparent}
        if extra_state:
            headers[self.TRACESTATE] = extra_state
        return headers

    def extract(self, headers: Dict[str, str]) -> Optional[SpanContext]:
        tp = headers.get(self.TRACEPARENT) or headers.get("Traceparent")
        if not tp:
            return None
        return SpanContext.from_traceparent(tp)

    def extract_or_new(self, headers: Dict[str, str]) -> SpanContext:
        ctx = self.extract(headers)
        return ctx if ctx is not None else SpanContext.new_root()
```

---

## Solution 2: PropagatingHTTPClient — Auto-Inject on Every Request

```python
import asyncio
import contextvars
from typing import Any, Dict, Optional

_current_span: contextvars.ContextVar[Optional[SpanContext]] = (
    contextvars.ContextVar("_current_span", default=None)
)


class TraceContextStore:
    """Async-safe store for the current span context."""

    @classmethod
    def current(cls) -> Optional[SpanContext]:
        return _current_span.get()

    @classmethod
    def set(cls, ctx: SpanContext):
        _current_span.set(ctx)

    @classmethod
    def child_context(cls) -> SpanContext:
        parent = _current_span.get()
        if parent:
            return parent.child()
        return SpanContext.new_root()


class PropagatingHTTPClient:
    """
    HTTP client that automatically injects W3C traceparent headers
    on every outbound request using the current async context's span.

    Usage:
        client = PropagatingHTTPClient()
        TraceContextStore.set(SpanContext.new_root())

        # All outbound requests automatically carry traceparent:
        result = await client.get("https://api.example.com/search?q=test")
        result2 = await client.post("https://tool.internal/run", json={"arg": 1})
    """

    def __init__(self, timeout_s: float = 30.0):
        self._propagator = W3CTraceContextPropagator()
        self._timeout = timeout_s

    def _inject_headers(self, extra: Optional[Dict] = None) -> Dict[str, str]:
        ctx = TraceContextStore.child_context()
        headers = self._propagator.inject(ctx)
        if extra:
            headers.update(extra)
        return headers

    async def get(self, url: str,
                  headers: Optional[Dict] = None) -> Any:
        import aiohttp
        merged = self._inject_headers(headers)
        async with aiohttp.ClientSession() as s:
            async with s.get(url, headers=merged,
                              timeout=aiohttp.ClientTimeout(total=self._timeout)) as r:
                return {"status": r.status, "body": await r.read(),
                        "headers": dict(r.headers)}

    async def post(self, url: str, json: Any = None,
                   headers: Optional[Dict] = None) -> Any:
        import aiohttp
        merged = self._inject_headers(headers)
        async with aiohttp.ClientSession() as s:
            async with s.post(url, json=json, headers=merged,
                               timeout=aiohttp.ClientTimeout(total=self._timeout)) as r:
                return {"status": r.status, "body": await r.read()}
```

---

## Solution 3: OTelAgentInstrumentation — OpenTelemetry Span Wrapping

Full OpenTelemetry instrumentation for agent steps, tool calls, and LLM invocations with automatic parent-child span relationships.

```python
import functools
import time
from contextlib import asynccontextmanager
from typing import Any, Callable, Optional

try:
    from opentelemetry import trace
    from opentelemetry.trace import SpanKind, Status, StatusCode
    from opentelemetry.propagate import inject, extract
    _OTEL = True
except ImportError:
    _OTEL = False


class AgentOTelTracer:
    """
    OpenTelemetry-based tracer for agent operations.
    Creates parent spans for each agent turn and child spans for each tool call.

    Usage:
        tracer = AgentOTelTracer(service_name="my-agent")

        async with tracer.agent_turn("handle_request",
                                      user_id="u1", session_id="s1") as span:
            span.set_attribute("model", "gpt-4o")
            async with tracer.tool_call("web_search",
                                         query="climate change"):
                result = await web_search("climate change")
    """

    def __init__(self, service_name: str = "agent"):
        if _OTEL:
            self._tracer = trace.get_tracer(service_name)
        self._service = service_name
        self._propagator = W3CTraceContextPropagator()

    @asynccontextmanager
    async def agent_turn(self, name: str, **attributes):
        if _OTEL:
            with self._tracer.start_as_current_span(
                name, kind=SpanKind.SERVER
            ) as span:
                for k, v in attributes.items():
                    span.set_attribute(f"agent.{k}", str(v))
                # Also propagate to context store for non-OTel paths
                otel_ctx = trace.get_current_span().get_span_context()
                if otel_ctx.is_valid:
                    TraceContextStore.set(SpanContext(
                        trace_id=format(otel_ctx.trace_id, "032x"),
                        span_id=format(otel_ctx.span_id, "016x"),
                    ))
                try:
                    yield span
                except Exception as exc:
                    span.record_exception(exc)
                    span.set_status(Status(StatusCode.ERROR, str(exc)))
                    raise
        else:
            ctx = SpanContext.new_root()
            TraceContextStore.set(ctx)
            yield ctx

    @asynccontextmanager
    async def tool_call(self, tool_name: str, **attributes):
        if _OTEL:
            with self._tracer.start_as_current_span(
                f"tool.{tool_name}", kind=SpanKind.CLIENT
            ) as span:
                span.set_attribute("tool.name", tool_name)
                for k, v in attributes.items():
                    span.set_attribute(f"tool.{k}", str(v))
                t0 = time.monotonic()
                try:
                    yield span
                    span.set_attribute(
                        "tool.duration_ms",
                        round((time.monotonic() - t0) * 1000, 2)
                    )
                except Exception as exc:
                    span.record_exception(exc)
                    span.set_status(Status(StatusCode.ERROR))
                    raise
        else:
            yield TraceContextStore.child_context()

    def headers_for_outbound(self) -> Dict[str, str]:
        """Return propagation headers to inject into outbound HTTP calls."""
        if _OTEL:
            headers: Dict[str, str] = {}
            inject(headers)
            return headers
        ctx = TraceContextStore.child_context()
        return self._propagator.inject(ctx)
```

---

## Solution 4: SubAgentContextForwarder — Trace Across Agent-to-Agent Calls

When one agent spawns or calls another, forward the trace context in the request payload or headers so the sub-agent's spans appear as children.

```python
import json
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class AgentRequest:
    task: str
    parameters: Dict[str, Any] = field(default_factory=dict)
    trace_context: Optional[Dict[str, str]] = None   # propagated headers


class SubAgentContextForwarder:
    """
    Injects and extracts trace context when calling sub-agents.
    Sub-agents extract the parent context and create child spans.

    Usage:
        forwarder = SubAgentContextForwarder()

        # Parent agent — calling sub-agent:
        request = forwarder.prepare_request("summarise", {"docs": docs})
        result = await sub_agent_client.call(request)

        # Sub-agent — receiving request:
        ctx = forwarder.extract_from_request(incoming_request)
        TraceContextStore.set(ctx.child())
        # All spans created now are children of the parent's trace
    """

    def __init__(self):
        self._propagator = W3CTraceContextPropagator()

    def prepare_request(self, task: str,
                         parameters: Dict[str, Any]) -> AgentRequest:
        ctx = TraceContextStore.child_context()
        return AgentRequest(
            task=task,
            parameters=parameters,
            trace_context=self._propagator.inject(ctx),
        )

    def extract_from_request(self, request: AgentRequest) -> SpanContext:
        if request.trace_context:
            ctx = self._propagator.extract(request.trace_context)
            if ctx:
                return ctx
        return SpanContext.new_root()

    def child_headers(self) -> Dict[str, str]:
        return self._propagator.inject(TraceContextStore.child_context())

    def enrich_log(self, record: Dict[str, Any]) -> Dict[str, Any]:
        """Add trace_id and span_id to any log record."""
        ctx = TraceContextStore.current()
        if ctx:
            record["trace_id"] = ctx.trace_id
            record["span_id"] = ctx.span_id
            record["sampled"] = ctx.is_sampled()
        return record
```

---

## Solution 5: TraceContextLogHandler — Inject trace_id into Every Log Line

Automatically add `trace_id` and `span_id` to every log record emitted during a traced request.

```python
import logging
from typing import Optional


class TraceContextLogFilter(logging.Filter):
    """
    Logging filter that injects the current span's trace_id and span_id
    into every log record emitted within the async context.

    Usage:
        handler = logging.StreamHandler()
        handler.addFilter(TraceContextLogFilter())
        logging.getLogger().addHandler(handler)

        async with tracer.agent_turn("handle"):
            logger.info("Processing request")
            # Log record automatically includes trace_id, span_id
    """

    def filter(self, record: logging.LogRecord) -> bool:
        ctx = TraceContextStore.current()
        if ctx:
            record.trace_id = ctx.trace_id
            record.span_id = ctx.span_id
            record.sampled = ctx.is_sampled()
        else:
            record.trace_id = "0" * 32
            record.span_id = "0" * 16
            record.sampled = False
        return True


def configure_trace_logging(fmt: Optional[str] = None):
    """Configure root logger to include trace fields in every line."""
    default_fmt = (
        "%(asctime)s %(levelname)s [%(trace_id)s/%(span_id)s] "
        "%(name)s: %(message)s"
    )
    handler = logging.StreamHandler()
    handler.addFilter(TraceContextLogFilter())
    handler.setFormatter(logging.Formatter(fmt or default_fmt))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
```

---

## Solution 6: ContextPropagationMiddleware — ASGI/FastAPI Integration

ASGI middleware that extracts incoming trace context, sets it on the async context store, and injects it into all outbound calls made during the request lifecycle.

```python
from typing import Callable


class TraceContextMiddleware:
    """
    ASGI middleware: extracts W3C traceparent from inbound request,
    sets it as the current span context, and adds it to response headers.

    Usage (FastAPI):
        from fastapi import FastAPI
        app = FastAPI()
        app.add_middleware(TraceContextMiddleware, service_name="agent-api")

        @app.post("/agent/chat")
        async def chat(request: ChatRequest):
            # TraceContextStore.current() is populated from inbound headers
            result = await agent.handle(request)
            return result
    """

    def __init__(self, app, service_name: str = "agent"):
        self._app = app
        self._propagator = W3CTraceContextPropagator()
        self._service = service_name

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await self._app(scope, receive, send)
            return

        headers = dict(scope.get("headers", []))
        headers_str = {k.decode(): v.decode() for k, v in headers.items()}
        ctx = self._propagator.extract_or_new(headers_str)
        child = ctx.child()
        token = _current_span.set(child)

        async def send_with_trace(message):
            if message["type"] == "http.response.start":
                trace_headers = self._propagator.inject(child)
                existing = list(message.get("headers", []))
                for k, v in trace_headers.items():
                    existing.append((k.encode(), v.encode()))
                message = {**message, "headers": existing}
            await send(message)

        try:
            await self._app(scope, receive, send_with_trace)
        finally:
            _current_span.reset(token)
```

---

## Comparison

| Approach | HTTP Propagation | Sub-Agent Propagation | Log Correlation | OTel Compatible | ASGI |
|---|---|---|---|---|---|
| **W3CTraceContextPropagator** | Yes | Manual | No | Yes (header format) | No |
| **PropagatingHTTPClient** | Automatic | No | No | Yes | No |
| **OTelAgentInstrumentation** | Yes (via inject) | No | Partial | Full | No |
| **SubAgentContextForwarder** | Via payload | Yes | Yes | Yes | No |
| **TraceContextLogFilter** | No | No | Yes | Yes | No |
| **TraceContextMiddleware** | Yes (inbound) | No | No | Yes | Yes |

**Key insight**: instrument two boundaries — inbound (middleware extracts traceparent) and outbound (PropagatingHTTPClient injects it). Every service in the call graph receives the same trace_id; spans appear as a unified tree in Jaeger, Honeycomb, or Grafana Tempo without any manual correlation.
