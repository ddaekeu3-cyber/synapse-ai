---
layout: solution
title: "Agent Doesn't Implement Request Tracing with Correlation IDs"
category: general
description: "Agent logs and errors contain no shared identifier linking a user request to its downstream tool calls, model invocations, and sub-agent actions — making it impossible to diagnose failures in production without grepping through thousands of unrelated log lines."
tags: [general, tracing, observability, correlation-id, logging, debugging]
---

# Agent Doesn't Implement Request Tracing with Correlation IDs

## Problem

When an agent fails in production, the error log shows a tool timeout. But which user request caused it? Which turn in the conversation? Which sub-agent? Without correlation IDs, every log line is isolated — you can't reconstruct the chain of events that led to the failure.

**Root cause:** No shared trace context is injected at the start of each request and propagated through all downstream calls, tool executions, and model invocations.

**Symptoms:**
- "We see the error in the logs but can't find the original user request"
- Tool errors can't be correlated to user-reported failures
- Multi-agent tasks produce logs across services with no linkage
- Debugging requires manually reconstructing timelines from timestamps

---

## Option 1: UUID Correlation ID Injected at Request Boundary

Generate a UUID at the start of each user request; attach it to every log line and tool result.

```python
import anthropic
import json
import uuid
import time
import logging
from contextvars import ContextVar

client = anthropic.Anthropic()

# Context variable — automatically inherited by sub-calls in same thread
correlation_id: ContextVar[str] = ContextVar("correlation_id", default="unset")
request_start: ContextVar[float] = ContextVar("request_start", default=0.0)

# Structured logger that auto-includes correlation_id
class CorrelatedLogger:
    def __init__(self, name: str):
        self._logger = logging.getLogger(name)
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(message)s"
        )

    def _log(self, level: str, msg: str, **extra):
        cid = correlation_id.get()
        elapsed = time.time() - request_start.get() if request_start.get() else 0
        record = {
            "correlation_id": cid,
            "elapsed_ms": round(elapsed * 1000),
            "message": msg,
            **extra
        }
        print(f"[{level}] {json.dumps(record)}")

    def info(self, msg: str, **kw): self._log("INFO", msg, **kw)
    def warn(self, msg: str, **kw): self._log("WARN", msg, **kw)
    def error(self, msg: str, **kw): self._log("ERROR", msg, **kw)

log = CorrelatedLogger("agent")

def tool_call_with_tracing(tool_name: str, tool_input: dict) -> dict:
    cid = correlation_id.get()
    log.info("tool_call_start", tool=tool_name, input_keys=list(tool_input.keys()))
    start = time.time()

    # Simulate tool execution
    time.sleep(0.05)
    result = {"tool": tool_name, "input": tool_input, "status": "ok", "trace_id": cid}

    duration_ms = round((time.time() - start) * 1000)
    log.info("tool_call_complete", tool=tool_name, duration_ms=duration_ms)
    return result

tools = [
    {
        "name": "search",
        "description": "Search for information",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"]
        }
    },
    {
        "name": "calculate",
        "description": "Perform a calculation",
        "input_schema": {
            "type": "object",
            "properties": {"expression": {"type": "string"}},
            "required": ["expression"]
        }
    }
]

def handle_request(user_query: str, user_id: str = "anon") -> str:
    # Inject correlation ID at request boundary
    cid = str(uuid.uuid4())[:8]  # Short form for readability
    correlation_id.set(cid)
    request_start.set(time.time())

    log.info("request_start", user_id=user_id, query_preview=user_query[:60])

    messages = [{"role": "user", "content": user_query}]
    turn = 0

    while True:
        turn += 1
        log.info("model_call", turn=turn)

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=tools,
            messages=messages,
            # Attach correlation ID in metadata (for Anthropic's logs if supported)
        )

        log.info("model_response", turn=turn, stop_reason=response.stop_reason,
                 input_tokens=response.usage.input_tokens, output_tokens=response.usage.output_tokens)

        if response.stop_reason == "end_turn":
            answer = next(b.text for b in response.content if hasattr(b, "text"))
            total_ms = round((time.time() - request_start.get()) * 1000)
            log.info("request_complete", turns=turn, total_ms=total_ms)
            return answer

        if response.stop_reason != "tool_use":
            log.warn("unexpected_stop", stop_reason=response.stop_reason)
            break

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            result = tool_call_with_tracing(block.name, block.input)
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": json.dumps(result)
            })

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

    log.error("request_failed", turns=turn)
    return "Error"

# Two concurrent requests get distinct correlation IDs
print(handle_request("Search for Python async patterns and calculate 2^10", user_id="user-42"))

# Expected Token Savings: ~0% (tracing is pure observability; zero API token impact)
# Environment: Any production agent; essential for debugging multi-turn, multi-tool failures
```

---

## Option 2: Hierarchical Trace with Span IDs

Add parent/child span relationships so you can reconstruct the full call tree.

```python
import anthropic
import json
import uuid
import time
from dataclasses import dataclass, field
from contextlib import contextmanager

client = anthropic.Anthropic()

@dataclass
class Span:
    span_id: str
    parent_id: str | None
    trace_id: str
    name: str
    start_time: float = field(default_factory=time.time)
    end_time: float | None = None
    tags: dict = field(default_factory=dict)
    status: str = "ok"

    def finish(self, status: str = "ok", **tags):
        self.end_time = time.time()
        self.status = status
        self.tags.update(tags)
        duration_ms = round((self.end_time - self.start_time) * 1000)
        print(json.dumps({
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_id": self.parent_id,
            "name": self.name,
            "duration_ms": duration_ms,
            "status": self.status,
            **self.tags
        }))

class Tracer:
    def __init__(self):
        self._trace_id: str | None = None
        self._current_span: Span | None = None
        self._spans: list[Span] = []

    def start_trace(self, name: str, **tags) -> Span:
        self._trace_id = str(uuid.uuid4())[:12]
        span = Span(
            span_id=str(uuid.uuid4())[:8],
            parent_id=None,
            trace_id=self._trace_id,
            name=name,
            tags=tags
        )
        self._current_span = span
        self._spans.append(span)
        return span

    @contextmanager
    def span(self, name: str, **tags):
        parent_id = self._current_span.span_id if self._current_span else None
        s = Span(
            span_id=str(uuid.uuid4())[:8],
            parent_id=parent_id,
            trace_id=self._trace_id or "no-trace",
            name=name,
            tags=tags
        )
        prev = self._current_span
        self._current_span = s
        self._spans.append(s)
        try:
            yield s
            s.finish("ok")
        except Exception as e:
            s.finish("error", error=str(e))
            raise
        finally:
            self._current_span = prev

    @property
    def trace_id(self) -> str:
        return self._trace_id or "no-trace"

tracer = Tracer()

def run_traced_agent(query: str) -> str:
    root = tracer.start_trace("agent_request", query=query[:60])

    tools = [
        {
            "name": "fetch_data",
            "description": "Fetch data by key",
            "input_schema": {
                "type": "object",
                "properties": {"key": {"type": "string"}},
                "required": ["key"]
            }
        }
    ]

    messages = [{"role": "user", "content": query}]
    turn = 0

    try:
        while True:
            turn += 1
            with tracer.span(f"model_turn_{turn}", turn=turn) as model_span:
                response = client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=512,
                    tools=tools,
                    messages=messages
                )
                model_span.tags["stop_reason"] = response.stop_reason
                model_span.tags["output_tokens"] = response.usage.output_tokens

            if response.stop_reason == "end_turn":
                root.finish("ok", turns=turn)
                return next(b.text for b in response.content if hasattr(b, "text"))

            if response.stop_reason != "tool_use":
                break

            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                with tracer.span(f"tool_{block.name}", tool=block.name, input=str(block.input)[:80]):
                    time.sleep(0.03)  # Simulate tool work
                    result = {"key": block.input.get("key"), "value": "mock_data", "trace_id": tracer.trace_id}
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result)
                })

            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": tool_results})

    except Exception as e:
        root.finish("error", error=str(e))
        raise

    root.finish("incomplete")
    return "Done"

result = run_traced_agent("Fetch data for keys: alpha, beta, and summarize the results")
print(f"\nTrace ID: {tracer.trace_id}")

# Expected Token Savings: ~0% (spans are local metadata; no API tokens consumed)
# Environment: Microservice architectures; agents calling other agents; Jaeger/Zipkin/OpenTelemetry integration
```

---

## Option 3: OpenTelemetry-Compatible Trace Export

Emit traces in OTLP-compatible format for integration with Datadog, Honeycomb, or Jaeger.

```python
import anthropic
import json
import uuid
import time
from dataclasses import dataclass, field

client = anthropic.Anthropic()

@dataclass
class OTelSpan:
    """Simplified OpenTelemetry-compatible span."""
    trace_id: str
    span_id: str
    parent_span_id: str | None
    name: str
    kind: str = "INTERNAL"  # INTERNAL, SERVER, CLIENT, PRODUCER, CONSUMER
    start_time_ns: int = field(default_factory=lambda: int(time.time() * 1e9))
    end_time_ns: int | None = None
    attributes: dict = field(default_factory=dict)
    status_code: str = "OK"  # OK, ERROR, UNSET
    status_message: str = ""

    def end(self, status: str = "OK", **attrs):
        self.end_time_ns = int(time.time() * 1e9)
        self.status_code = status
        self.attributes.update(attrs)

    def to_otlp_dict(self) -> dict:
        duration_ms = round((self.end_time_ns - (self.start_time_ns or 0)) / 1e6) if self.end_time_ns else None
        return {
            "traceId": self.trace_id,
            "spanId": self.span_id,
            "parentSpanId": self.parent_span_id,
            "name": self.name,
            "kind": self.kind,
            "startTimeUnixNano": self.start_time_ns,
            "endTimeUnixNano": self.end_time_ns,
            "durationMs": duration_ms,
            "attributes": self.attributes,
            "status": {"code": self.status_code, "message": self.status_message}
        }

class OTelTracer:
    def __init__(self, service_name: str, export_fn=None):
        self.service_name = service_name
        self.export_fn = export_fn or self._default_export
        self._spans: list[OTelSpan] = []

    def _default_export(self, span: OTelSpan):
        """Default: print to stdout (replace with OTLP exporter in production)."""
        d = span.to_otlp_dict()
        d["service.name"] = self.service_name
        print(f"[otel] {json.dumps(d)}")

    def start_span(
        self,
        name: str,
        trace_id: str | None = None,
        parent_id: str | None = None,
        kind: str = "INTERNAL",
        **attrs
    ) -> OTelSpan:
        span = OTelSpan(
            trace_id=trace_id or uuid.uuid4().hex,
            span_id=uuid.uuid4().hex[:16],
            parent_span_id=parent_id,
            name=name,
            kind=kind,
            attributes={"service.name": self.service_name, **attrs}
        )
        self._spans.append(span)
        return span

    def finish_span(self, span: OTelSpan, status: str = "OK", **attrs):
        span.end(status=status, **attrs)
        self.export_fn(span)

otel = OTelTracer("synapse-agent")

def run_otel_traced_agent(query: str, user_id: str = "anon") -> str:
    # Root span for the entire request
    root = otel.start_span(
        "agent.request",
        kind="SERVER",
        **{"user.id": user_id, "query.length": len(query)}
    )

    tools = [
        {
            "name": "lookup",
            "description": "Look up information",
            "input_schema": {
                "type": "object",
                "properties": {"topic": {"type": "string"}},
                "required": ["topic"]
            }
        }
    ]

    messages = [{"role": "user", "content": query}]
    turn = 0

    while True:
        turn += 1
        llm_span = otel.start_span(
            "anthropic.messages.create",
            trace_id=root.trace_id,
            parent_id=root.span_id,
            kind="CLIENT",
            **{"llm.model": "claude-haiku-4-5-20251001", "llm.turn": turn}
        )

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=tools,
            messages=messages
        )

        otel.finish_span(llm_span,
            **{
                "llm.input_tokens": response.usage.input_tokens,
                "llm.output_tokens": response.usage.output_tokens,
                "llm.stop_reason": response.stop_reason
            }
        )

        if response.stop_reason == "end_turn":
            answer = next(b.text for b in response.content if hasattr(b, "text"))
            otel.finish_span(root, **{"agent.turns": turn, "agent.status": "complete"})
            return answer

        if response.stop_reason != "tool_use":
            otel.finish_span(root, status="ERROR", **{"error": "unexpected_stop"})
            break

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue

            tool_span = otel.start_span(
                f"tool.{block.name}",
                trace_id=root.trace_id,
                parent_id=root.span_id,
                kind="CLIENT",
                **{"tool.name": block.name, "tool.input": str(block.input)[:100]}
            )
            time.sleep(0.04)  # Simulate tool work
            result = {"topic": block.input.get("topic"), "result": "mock_info"}
            otel.finish_span(tool_span, **{"tool.success": True})

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": json.dumps(result)
            })

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

    return "Done"

result = run_otel_traced_agent("What are best practices for distributed tracing?", user_id="user-99")
print(f"\nResult: {result[:100]}...")

# Expected Token Savings: ~0% (OTLP export is out-of-band; no API token impact)
# Environment: Production services with Datadog APM, Honeycomb, Jaeger, or AWS X-Ray integration
```

---

## Option 4: Correlation ID Propagated Through Tool Results

Embed the correlation ID in every tool result so downstream services can link back to the originating request.

```python
import anthropic
import json
import uuid
import time

client = anthropic.Anthropic()

class RequestContext:
    def __init__(self, correlation_id: str, user_id: str = "anon", session_id: str = ""):
        self.correlation_id = correlation_id
        self.user_id = user_id
        self.session_id = session_id or str(uuid.uuid4())[:8]
        self.created_at = time.time()
        self.tool_call_count = 0
        self.model_call_count = 0

    def as_trace_header(self) -> dict:
        return {
            "X-Correlation-ID": self.correlation_id,
            "X-Session-ID": self.session_id,
            "X-User-ID": self.user_id
        }

    def annotate_result(self, result: dict) -> dict:
        """Embed correlation context into tool results for downstream tracing."""
        return {
            **result,
            "_trace": {
                "correlation_id": self.correlation_id,
                "session_id": self.session_id,
                "tool_call_num": self.tool_call_count,
                "elapsed_ms": round((time.time() - self.created_at) * 1000)
            }
        }

def mock_external_service(endpoint: str, payload: dict, headers: dict) -> dict:
    """Simulate external service that honors X-Correlation-ID."""
    cid = headers.get("X-Correlation-ID", "unknown")
    # The external service logs: "Received request from correlation_id={cid}"
    return {
        "endpoint": endpoint,
        "echo_correlation_id": cid,  # Service echoes it back
        "result": f"data_from_{endpoint.replace('/', '_')}",
        "service_request_id": str(uuid.uuid4())[:8]
    }

ctx: RequestContext | None = None

tools = [
    {
        "name": "call_service",
        "description": "Call an external service endpoint",
        "input_schema": {
            "type": "object",
            "properties": {
                "endpoint": {"type": "string"},
                "method": {"type": "string", "enum": ["GET", "POST"]}
            },
            "required": ["endpoint"]
        }
    }
]

def run_propagated_trace_agent(query: str, user_id: str = "anon") -> str:
    global ctx
    ctx = RequestContext(
        correlation_id=str(uuid.uuid4()),
        user_id=user_id
    )
    print(f"[trace] Request started: correlation_id={ctx.correlation_id[:8]}...")

    messages = [{"role": "user", "content": query}]

    while True:
        ctx.model_call_count += 1
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=tools,
            messages=messages
        )

        if response.stop_reason == "end_turn":
            answer = next(b.text for b in response.content if hasattr(b, "text"))
            print(f"[trace] Request done: {ctx.model_call_count} model calls, "
                  f"{ctx.tool_call_count} tool calls, "
                  f"{round((time.time() - ctx.created_at)*1000)}ms total")
            return answer

        if response.stop_reason != "tool_use":
            break

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue

            ctx.tool_call_count += 1
            # Pass correlation ID to every external service call
            raw_result = mock_external_service(
                endpoint=block.input["endpoint"],
                payload=block.input,
                headers=ctx.as_trace_header()
            )
            # Annotate result with trace context before returning to agent
            annotated = ctx.annotate_result(raw_result)
            print(f"[trace] Tool {block.name} result echoed correlation_id: "
                  f"{raw_result.get('echo_correlation_id', 'N/A')[:8]}...")

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": json.dumps(annotated)
            })

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

    return "Done"

result = run_propagated_trace_agent(
    "Call /api/users, /api/orders, and /api/inventory, then summarize the data",
    user_id="user-007"
)
print(result)

# Expected Token Savings: ~0% (trace headers are tiny; annotated results add ~50 bytes per tool result)
# Environment: Microservice architectures; agents that call external APIs that support W3C trace context
```

---

## Option 5: Trace Sampling — Full Traces for Errors, Sampled for Success

Emit full detailed traces only for failed requests; sample 10% of successful ones to control log volume.

```python
import anthropic
import json
import uuid
import time
import random
from dataclasses import dataclass, field

client = anthropic.Anthropic()

@dataclass
class SampledTrace:
    trace_id: str
    events: list[dict] = field(default_factory=list)
    status: str = "pending"
    sample_rate: float = 0.1  # 10% of successful traces

    def record(self, event_type: str, **data):
        self.events.append({
            "type": event_type,
            "time_ms": round(time.time() * 1000),
            **data
        })

    def should_export(self) -> bool:
        if self.status == "error":
            return True  # Always export errors
        return random.random() < self.sample_rate  # Sample successes

    def export(self, sink=print):
        if not self.should_export():
            return
        sink(json.dumps({
            "trace_id": self.trace_id,
            "status": self.status,
            "event_count": len(self.events),
            "events": self.events,
            "sampled": self.status != "error"
        }, indent=2))

def run_sampled_trace_agent(query: str, inject_error: bool = False) -> str:
    trace = SampledTrace(trace_id=str(uuid.uuid4())[:12], sample_rate=0.1)
    trace.record("request_start", query=query[:80])

    tools = [
        {
            "name": "analyze",
            "description": "Analyze input data",
            "input_schema": {
                "type": "object",
                "properties": {"data": {"type": "string"}},
                "required": ["data"]
            }
        }
    ]

    messages = [{"role": "user", "content": query}]
    turn = 0

    try:
        while True:
            turn += 1
            trace.record("model_call_start", turn=turn)

            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=512,
                tools=tools,
                messages=messages
            )

            trace.record("model_call_end", turn=turn,
                        stop_reason=response.stop_reason,
                        output_tokens=response.usage.output_tokens)

            if response.stop_reason == "end_turn":
                answer = next(b.text for b in response.content if hasattr(b, "text"))
                trace.status = "ok"
                trace.record("request_complete", turns=turn)
                trace.export()
                return answer

            if response.stop_reason != "tool_use":
                break

            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue

                trace.record("tool_call", tool=block.name, input=str(block.input)[:60])

                if inject_error:
                    raise RuntimeError("Simulated tool failure for trace demo")

                result = {"data": block.input.get("data"), "analysis": "mock_result"}
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result)
                })

            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": tool_results})

    except Exception as e:
        trace.status = "error"
        trace.record("error", message=str(e))
        trace.export()  # Always export errors
        return f"Error: {e}"

    trace.status = "incomplete"
    trace.export()
    return "Done"

# Normal request (sampled at 10%)
print("=== Normal request ===")
run_sampled_trace_agent("Analyze this data: sales_q1=150k, sales_q2=180k")

# Error request (always exported)
print("\n=== Error request ===")
run_sampled_trace_agent("Analyze this data: revenue=500k", inject_error=True)

# Expected Token Savings: ~0% (sampling reduces storage/egress cost, not API tokens)
# Environment: High-volume production agents where full tracing of every request is too expensive
```

---

## Option 6: Multi-Agent Trace Propagation

Propagate the same trace ID across orchestrator and sub-agents so their logs are unified.

```python
import anthropic
import json
import uuid
import time
from dataclasses import dataclass

client = anthropic.Anthropic()

@dataclass
class TraceContext:
    trace_id: str
    span_id: str
    agent_role: str
    depth: int = 0

    def child_context(self, role: str) -> "TraceContext":
        return TraceContext(
            trace_id=self.trace_id,  # Same trace ID
            span_id=uuid.uuid4().hex[:8],  # New span ID
            agent_role=role,
            depth=self.depth + 1
        )

    def log(self, event: str, **data):
        indent = "  " * self.depth
        print(f"{indent}[trace={self.trace_id[:8]} span={self.span_id} role={self.agent_role}] "
              f"{event} {json.dumps(data) if data else ''}")

def run_sub_agent(ctx: TraceContext, subtask: str) -> str:
    """A sub-agent that runs with an inherited trace context."""
    ctx.log("sub_agent_start", subtask=subtask[:50])

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{"role": "user", "content": f"Complete this subtask: {subtask}"}]
    )
    result = response.content[0].text
    ctx.log("sub_agent_complete", output_tokens=response.usage.output_tokens)
    return result

def run_orchestrator_with_trace(query: str, user_id: str = "anon") -> str:
    """Orchestrator agent that spawns sub-agents with shared trace context."""
    root_ctx = TraceContext(
        trace_id=uuid.uuid4().hex[:12],
        span_id=uuid.uuid4().hex[:8],
        agent_role="orchestrator"
    )
    root_ctx.log("orchestrator_start", user_id=user_id, query=query[:60])

    tools = [
        {
            "name": "delegate_to_subagent",
            "description": "Delegate a subtask to a specialized sub-agent",
            "input_schema": {
                "type": "object",
                "properties": {
                    "subtask": {"type": "string"},
                    "agent_role": {"type": "string"}
                },
                "required": ["subtask", "agent_role"]
            }
        }
    ]

    messages = [{"role": "user", "content": query}]

    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=tools,
            messages=messages
        )
        root_ctx.log("model_response", stop_reason=response.stop_reason)

        if response.stop_reason == "end_turn":
            answer = next(b.text for b in response.content if hasattr(b, "text"))
            root_ctx.log("orchestrator_complete")
            return answer

        if response.stop_reason != "tool_use":
            break

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue

            # Create child context with same trace_id, new span_id
            sub_ctx = root_ctx.child_context(block.input.get("agent_role", "sub-agent"))
            sub_ctx.log("delegation_received", subtask=block.input["subtask"][:50])

            # Sub-agent runs under the same trace
            sub_result = run_sub_agent(sub_ctx, block.input["subtask"])

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": json.dumps({
                    "result": sub_result,
                    "trace_id": root_ctx.trace_id,  # Propagated
                    "sub_span_id": sub_ctx.span_id
                })
            })

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

    return "Done"

result = run_orchestrator_with_trace(
    "Research Python async patterns and write a code example. Delegate each part.",
    user_id="user-42"
)
print(f"\nFinal: {result[:150]}...")

# Expected Token Savings: ~0% (trace propagation is metadata; no API token cost)
# Environment: Multi-agent systems (orchestrator + specialists); any agent that spawns sub-agents
```

---

## Comparison

| Option | ID Scope | Hierarchy | Downstream Propagation | Export Format | Best For |
|--------|----------|-----------|----------------------|---------------|----------|
| 1. UUID Correlation | Request | None | No | JSON logs | Simple production agents |
| 2. Span Hierarchy | Request + span | Parent/child | No | Custom spans | Call-tree reconstruction |
| 3. OpenTelemetry | Trace + span | Full OTLP | Via headers | OTLP JSON | Datadog/Honeycomb/Jaeger integration |
| 4. Result Propagation | Request | None | Yes (tool results) | Headers + body | Multi-service architectures |
| 5. Sampled Traces | Request | None | No | JSON (sampled) | High-volume production cost control |
| 6. Multi-Agent Propagation | Trace + span | Parent/child | Yes (sub-agents) | Console | Multi-agent orchestration systems |
