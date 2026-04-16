---
title: "Agent Doesn't Implement Distributed Trace Propagation"
slug: agent-doesnt-implement-distributed-trace-propagation
category: observability
tags: [tracing, opentelemetry, distributed, observability, context-propagation, anthropic-sdk]
description: >
  The agent makes API calls without injecting or extracting trace context,
  making it impossible to correlate a user-visible latency spike with the
  specific model call, tool execution, or downstream service that caused it.
  Every hop appears as an isolated island in the trace viewer.
symptoms:
  - Traces break at agent boundaries — no parent-child relationship visible
  - Cannot determine which LLM call caused a slow request end-to-end
  - Tool call latencies are invisible in distributed traces
  - No trace ID to pass to Anthropic support when debugging slow requests
related_solutions:
  - agent-doesnt-implement-cost-per-conversation-tracking
  - agent-doesnt-implement-agent-decision-explainability-dashboard
  - agent-doesnt-implement-cooperative-cancellation-with-structured-concurrency
---

## Problem

In a multi-service architecture the agent is rarely an endpoint — it calls tool
services, databases, and external APIs before returning a response. Without
trace context propagation each hop generates its own disconnected trace, making
it impossible to answer "which downstream call made this user request slow?" or
"which tool caused the 500 ms spike on Thursday?" Proper distributed tracing
requires injecting a W3C `traceparent` header into every outgoing call and
extracting it from every incoming request.

---

## Solution 1 — Manual Trace Context with contextvars (Zero Dependencies)

Store a `TraceContext` in a `contextvars.ContextVar` and log span data
structured as JSON. No OpenTelemetry SDK required — works anywhere.

```python
import anthropic
import asyncio
import contextvars
import json
import time
import uuid
from dataclasses import dataclass, field, asdict


@dataclass
class TraceContext:
    trace_id: str
    parent_span_id: str | None
    span_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    started_at: float = field(default_factory=time.monotonic)
    attributes: dict = field(default_factory=dict)

    def child(self, name: str) -> "SpanContext":
        return SpanContext(
            trace_id=self.trace_id,
            parent_span_id=self.span_id,
            name=name,
        )

    @property
    def traceparent(self) -> str:
        """W3C traceparent header value."""
        return f"00-{self.trace_id}-{self.span_id}-01"


@dataclass
class SpanContext(TraceContext):
    name: str = ""
    ended_at: float = 0.0
    status: str = "ok"
    events: list = field(default_factory=list)

    def end(self, status: str = "ok") -> None:
        self.ended_at = time.monotonic()
        self.status = status

    @property
    def duration_ms(self) -> float:
        end = self.ended_at or time.monotonic()
        return (end - self.started_at) * 1000

    def emit(self) -> None:
        record = {
            "trace_id":      self.trace_id,
            "span_id":       self.span_id,
            "parent_span_id": self.parent_span_id,
            "name":          self.name,
            "duration_ms":   round(self.duration_ms, 2),
            "status":        self.status,
            "attributes":    self.attributes,
        }
        print(json.dumps(record))


_current_trace: contextvars.ContextVar[TraceContext | None] = contextvars.ContextVar(
    "current_trace", default=None
)


def new_trace(name: str = "root") -> SpanContext:
    span = SpanContext(
        trace_id=uuid.uuid4().hex,
        parent_span_id=None,
        name=name,
    )
    _current_trace.set(span)
    return span


def current_span() -> TraceContext | None:
    return _current_trace.get()


async def traced_llm_call(messages: list, model: str = "claude-sonnet-4-6") -> str:
    parent = current_span()
    span = SpanContext(
        trace_id=parent.trace_id if parent else uuid.uuid4().hex,
        parent_span_id=parent.span_id if parent else None,
        name="llm.create",
    )
    span.attributes["model"] = model
    token = _current_trace.set(span)

    client = anthropic.AsyncAnthropic()
    try:
        resp = await client.messages.create(
            model=model,
            max_tokens=512,
            messages=messages,
            extra_headers={"traceparent": span.traceparent},
        )
        span.attributes["input_tokens"]  = resp.usage.input_tokens
        span.attributes["output_tokens"] = resp.usage.output_tokens
        span.end("ok")
        return resp.content[0].text
    except Exception as e:
        span.end("error")
        span.attributes["error"] = str(e)
        raise
    finally:
        span.emit()
        _current_trace.reset(token)


async def main():
    root = new_trace("agent.request")
    root.attributes["user_id"] = "u-42"

    # Hop 1
    answer = await traced_llm_call(
        [{"role": "user", "content": "What is quorum in distributed systems?"}]
    )
    print(f"\nAnswer: {answer[:80]}\n")

    root.end("ok")
    root.emit()


asyncio.run(main())
```

---

## Solution 2 — OpenTelemetry SDK with OTLP Export

Use the official OpenTelemetry Python SDK to instrument every API call, inject
W3C trace headers, and export spans to an OTLP collector (Jaeger, Grafana
Tempo, Honeycomb, etc.).

```python
import anthropic
import asyncio

# pip install opentelemetry-sdk opentelemetry-exporter-otlp-proto-grpc
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.resources import Resource
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.propagate import inject, extract
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator


def setup_tracer(service_name: str = "synapse-agent") -> trace.Tracer:
    resource = Resource(attributes={"service.name": service_name})
    provider = TracerProvider(resource=resource)
    # Export to local collector; set OTEL_EXPORTER_OTLP_ENDPOINT in prod
    exporter = OTLPSpanExporter(endpoint="http://localhost:4317", insecure=True)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    return trace.get_tracer(__name__)


tracer = setup_tracer()


async def otel_llm_call(
    messages: list,
    model: str = "claude-sonnet-4-6",
    extra_attrs: dict | None = None,
) -> str:
    with tracer.start_as_current_span(
        "anthropic.messages.create",
        kind=trace.SpanKind.CLIENT,
    ) as span:
        # Inject trace context into headers for propagation to downstream tools
        headers: dict[str, str] = {}
        inject(headers)

        span.set_attribute("llm.model", model)
        span.set_attribute("llm.message_count", len(messages))
        for k, v in (extra_attrs or {}).items():
            span.set_attribute(k, v)

        client = anthropic.AsyncAnthropic()
        try:
            resp = await client.messages.create(
                model=model,
                max_tokens=1024,
                messages=messages,
                extra_headers=headers,
            )
            span.set_attribute("llm.input_tokens",  resp.usage.input_tokens)
            span.set_attribute("llm.output_tokens", resp.usage.output_tokens)
            span.set_attribute("llm.stop_reason",   resp.stop_reason)
            span.set_status(trace.StatusCode.OK)
            return resp.content[0].text
        except Exception as e:
            span.record_exception(e)
            span.set_status(trace.StatusCode.ERROR, str(e))
            raise


async def tool_call_span(tool_name: str, tool_input: dict) -> dict:
    """Example tool instrumented as a child span."""
    with tracer.start_as_current_span(
        f"tool.{tool_name}",
        kind=trace.SpanKind.INTERNAL,
    ) as span:
        span.set_attribute("tool.name", tool_name)
        span.set_attribute("tool.input", str(tool_input)[:256])
        await asyncio.sleep(0.01)   # simulate tool work
        result = {"status": "ok", "data": "42"}
        span.set_attribute("tool.output", str(result)[:256])
        return result


async def agent_request(user_query: str) -> str:
    with tracer.start_as_current_span("agent.request", kind=trace.SpanKind.SERVER) as root:
        root.set_attribute("query.length", len(user_query))

        # Tool call as child span
        tool_result = await tool_call_span("search", {"q": user_query})

        # LLM call as child span
        messages = [
            {"role": "user", "content": f"{user_query}\nContext: {tool_result['data']}"}
        ]
        answer = await otel_llm_call(messages, extra_attrs={"feature": "chat"})
        root.set_attribute("response.length", len(answer))
        return answer


# Note: OTLPSpanExporter requires a running collector. Run in dev with:
# docker run -p 4317:4317 jaegertracing/all-in-one
try:
    result = asyncio.run(agent_request("Explain consistent hashing."))
    print(result[:120])
except Exception as e:
    print(f"[otel] collector not running in demo: {e}")
```

---

## Solution 3 — Trace Baggage for Cross-Service User Attribution

Use W3C Baggage to carry user-identifying metadata (user_id, tenant_id,
feature) through every service boundary so all spans in a trace are
automatically tagged without manual attribute injection at each hop.

```python
import anthropic
import asyncio
import contextvars
import uuid
from dataclasses import dataclass, field


@dataclass
class Baggage:
    """Simple W3C Baggage implementation without OTel dependency."""
    items: dict[str, str] = field(default_factory=dict)

    @classmethod
    def parse(cls, header: str) -> "Baggage":
        b = cls()
        for entry in header.split(","):
            entry = entry.strip()
            if "=" in entry:
                k, v = entry.split("=", 1)
                b.items[k.strip()] = v.strip()
        return b

    def encode(self) -> str:
        return ", ".join(f"{k}={v}" for k, v in self.items.items())

    def get(self, key: str, default: str = "") -> str:
        return self.items.get(key, default)

    def set(self, key: str, value: str) -> "Baggage":
        self.items[key] = value
        return self


_baggage_cv: contextvars.ContextVar[Baggage] = contextvars.ContextVar(
    "baggage", default=Baggage()
)
_trace_id_cv: contextvars.ContextVar[str] = contextvars.ContextVar(
    "trace_id", default=""
)


def init_trace(user_id: str, tenant_id: str, feature: str) -> str:
    trace_id = uuid.uuid4().hex
    _trace_id_cv.set(trace_id)
    bag = Baggage()
    bag.set("user_id",   user_id)
    bag.set("tenant_id", tenant_id)
    bag.set("feature",   feature)
    _baggage_cv.set(bag)
    return trace_id


def outgoing_headers() -> dict[str, str]:
    """Headers to inject into every outgoing HTTP/gRPC call."""
    trace_id = _trace_id_cv.get()
    span_id  = uuid.uuid4().hex[:16]
    return {
        "traceparent": f"00-{trace_id}-{span_id}-01",
        "baggage":     _baggage_cv.get().encode(),
    }


def log_span(name: str, duration_ms: float, **extra) -> None:
    bag = _baggage_cv.get()
    print({
        "name":      name,
        "trace_id":  _trace_id_cv.get(),
        "user_id":   bag.get("user_id"),
        "tenant_id": bag.get("tenant_id"),
        "feature":   bag.get("feature"),
        "duration_ms": round(duration_ms, 2),
        **extra,
    })


async def instrumented_create(messages: list, model: str = "claude-sonnet-4-6") -> str:
    import time
    headers = outgoing_headers()
    client = anthropic.AsyncAnthropic()
    t0 = time.monotonic()
    resp = await client.messages.create(
        model=model,
        max_tokens=512,
        messages=messages,
        extra_headers=headers,
    )
    log_span(
        "llm.create",
        (time.monotonic() - t0) * 1000,
        model=model,
        input_tokens=resp.usage.input_tokens,
        output_tokens=resp.usage.output_tokens,
    )
    return resp.content[0].text


async def handle(user_id: str, tenant_id: str, query: str) -> str:
    init_trace(user_id=user_id, tenant_id=tenant_id, feature="chat")
    return await instrumented_create([{"role": "user", "content": query}])


result = asyncio.run(handle("u-99", "acme", "What is a Merkle tree?"))
print(result[:80])
```

---

## Solution 4 — Span Sampling to Control Trace Volume

Instrument all calls but apply head-based sampling so only a fraction of traces
are exported. High-priority traces (errors, slow requests, specific users) are
always sampled; routine fast requests are sampled at a lower rate.

```python
import anthropic
import asyncio
import random
import time
import json
import uuid


class SamplingDecision:
    RECORD_AND_SAMPLE = "record"
    DROP              = "drop"


def make_sampling_decision(
    trace_id: str,
    latency_hint_ms: float = 0.0,
    is_error: bool = False,
    user_tier: str = "standard",
    base_rate: float = 0.10,
) -> str:
    """
    Always sample: errors, premium users, slow requests.
    Otherwise: sample at base_rate using trace_id for consistency.
    """
    if is_error:
        return SamplingDecision.RECORD_AND_SAMPLE
    if user_tier == "premium":
        return SamplingDecision.RECORD_AND_SAMPLE
    if latency_hint_ms > 5000:
        return SamplingDecision.RECORD_AND_SAMPLE
    # Deterministic: same trace_id always gets same decision
    seed = int(trace_id[:8], 16) / 0xFFFFFFFF
    return (SamplingDecision.RECORD_AND_SAMPLE
            if seed < base_rate else SamplingDecision.DROP)


def emit_span(span: dict) -> None:
    """In production: send to OTLP exporter, Datadog agent, etc."""
    print(f"[TRACE] {json.dumps(span)}")


async def sampled_llm_call(
    messages: list,
    model: str = "claude-sonnet-4-6",
    user_tier: str = "standard",
    trace_id: str | None = None,
) -> str:
    trace_id = trace_id or uuid.uuid4().hex
    span_id  = uuid.uuid4().hex[:16]
    t0 = time.monotonic()

    client = anthropic.AsyncAnthropic()
    error = None
    try:
        resp = await client.messages.create(
            model=model,
            max_tokens=512,
            messages=messages,
            extra_headers={"traceparent": f"00-{trace_id}-{span_id}-01"},
        )
        text = resp.content[0].text
        usage = {"in": resp.usage.input_tokens, "out": resp.usage.output_tokens}
    except Exception as e:
        error = str(e)
        text = ""
        usage = {}
        raise
    finally:
        elapsed_ms = (time.monotonic() - t0) * 1000
        decision = make_sampling_decision(
            trace_id,
            latency_hint_ms=elapsed_ms,
            is_error=error is not None,
            user_tier=user_tier,
        )
        if decision == SamplingDecision.RECORD_AND_SAMPLE:
            emit_span({
                "trace_id":   trace_id,
                "span_id":    span_id,
                "name":       "llm.create",
                "model":      model,
                "duration_ms": round(elapsed_ms, 2),
                "usage":       usage,
                "sampled":    True,
                "error":      error,
            })
        else:
            pass   # drop — no export cost

    return text


async def main():
    # Simulate 5 requests; ~10% base sample rate
    for i in range(5):
        tier = "premium" if i == 2 else "standard"
        try:
            await sampled_llm_call(
                [{"role": "user", "content": f"Question {i}: explain CRDT."}],
                user_tier=tier,
            )
        except Exception:
            pass


asyncio.run(main())
```

---

## Solution 5 — Tool-Call Span Injection for Multi-Step Agents

For agents that use `tools`, automatically wrap each tool invocation in a
child span so the trace viewer shows exactly how long each tool call took
relative to the surrounding LLM calls.

```python
import anthropic
import asyncio
import time
import uuid
import json
from contextlib import contextmanager
from dataclasses import dataclass, field


@dataclass
class Span:
    trace_id: str
    span_id:  str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    parent_id: str | None = None
    name: str = ""
    start: float = field(default_factory=time.monotonic)
    attrs: dict = field(default_factory=dict)
    _ended: bool = False

    def end(self, **attrs) -> None:
        if not self._ended:
            self.attrs.update(attrs)
            elapsed = (time.monotonic() - self.start) * 1000
            print(json.dumps({
                "trace": self.trace_id,
                "span":  self.span_id,
                "parent": self.parent_id,
                "name":  self.name,
                "ms":    round(elapsed, 2),
                **self.attrs,
            }))
            self._ended = True


TOOLS = [
    {
        "name": "get_weather",
        "description": "Get current weather for a city.",
        "input_schema": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    },
    {
        "name": "get_time",
        "description": "Get current time for a timezone.",
        "input_schema": {
            "type": "object",
            "properties": {"tz": {"type": "string"}},
            "required": ["tz"],
        },
    },
]


async def execute_tool(tool_name: str, tool_input: dict, parent_span: Span) -> str:
    span = Span(trace_id=parent_span.trace_id, parent_id=parent_span.span_id,
                name=f"tool.{tool_name}", attrs={"tool.input": json.dumps(tool_input)})
    await asyncio.sleep(0.05)   # simulate tool latency
    result = f"[{tool_name}({tool_input})] => mocked_result"
    span.end(**{"tool.output": result[:64]})
    return result


async def traced_agentic_loop(user_query: str) -> str:
    trace_id = uuid.uuid4().hex
    root = Span(trace_id=trace_id, name="agent.loop", attrs={"query": user_query[:64]})

    client = anthropic.AsyncAnthropic()
    messages = [{"role": "user", "content": user_query}]

    while True:
        llm_span = Span(trace_id=trace_id, parent_id=root.span_id, name="llm.create")
        resp = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            tools=TOOLS,
            messages=messages,
            extra_headers={"traceparent": f"00-{trace_id}-{llm_span.span_id}-01"},
        )
        llm_span.end(
            stop_reason=resp.stop_reason,
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens,
        )

        if resp.stop_reason == "end_turn":
            text = next((b.text for b in resp.content if hasattr(b, "text")), "")
            root.end(final_length=len(text))
            return text

        # Process tool calls
        tool_results = []
        for block in resp.content:
            if block.type == "tool_use":
                result = await execute_tool(block.name, block.input, root)
                tool_results.append({
                    "type":       "tool_result",
                    "tool_use_id": block.id,
                    "content":    result,
                })

        messages.append({"role": "assistant", "content": resp.content})
        messages.append({"role": "user",      "content": tool_results})


result = asyncio.run(traced_agentic_loop(
    "What's the weather in Tokyo and what time is it there?"
))
print(f"\nFinal answer: {result[:120]}")
```

---

## Solution 6 — Trace Propagation Through Message Queue (Async Tasks)

When an agent enqueues work for an async worker (Celery, asyncio.Queue,
SQS), embed the trace context in the message envelope so the worker can
resume the trace as a child span — preventing trace breaks at queue boundaries.

```python
import anthropic
import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field


@dataclass
class TraceEnvelope:
    """Wraps any task payload with trace context for queue propagation."""
    payload: dict
    trace_id: str
    parent_span_id: str
    baggage: dict = field(default_factory=dict)
    enqueued_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "payload":        self.payload,
            "trace_id":       self.trace_id,
            "parent_span_id": self.parent_span_id,
            "baggage":        self.baggage,
            "enqueued_at":    self.enqueued_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "TraceEnvelope":
        return cls(**d)


def emit_span(name: str, trace_id: str, span_id: str, parent_id: str | None,
              duration_ms: float, **attrs) -> None:
    print(json.dumps({
        "name": name, "trace": trace_id, "span": span_id,
        "parent": parent_id, "ms": round(duration_ms, 2), **attrs,
    }))


async def enqueue_llm_task(
    queue: asyncio.Queue,
    messages: list,
    trace_id: str,
    parent_span_id: str,
    user_id: str = "anon",
) -> None:
    envelope = TraceEnvelope(
        payload={"messages": messages},
        trace_id=trace_id,
        parent_span_id=parent_span_id,
        baggage={"user_id": user_id},
    )
    await queue.put(envelope.to_dict())
    print(f"[producer] enqueued with trace={trace_id[:8]} parent={parent_span_id}")


async def worker(queue: asyncio.Queue, model: str = "claude-sonnet-4-6") -> None:
    while True:
        raw = await queue.get()
        if raw is None:
            break
        envelope = TraceEnvelope.from_dict(raw)

        # Resume trace as child of the enqueuing span
        span_id = uuid.uuid4().hex[:16]
        queue_wait_ms = (time.time() - envelope.enqueued_at) * 1000
        t0 = time.monotonic()

        client = anthropic.AsyncAnthropic()
        try:
            resp = await client.messages.create(
                model=model,
                max_tokens=512,
                messages=envelope.payload["messages"],
                extra_headers={
                    "traceparent": f"00-{envelope.trace_id}-{span_id}-01",
                },
            )
            text = resp.content[0].text
            elapsed = (time.monotonic() - t0) * 1000
            emit_span(
                "worker.llm.create",
                envelope.trace_id, span_id, envelope.parent_span_id,
                elapsed,
                queue_wait_ms=round(queue_wait_ms, 2),
                user_id=envelope.baggage.get("user_id"),
                input_tokens=resp.usage.input_tokens,
                output_tokens=resp.usage.output_tokens,
            )
            print(f"[worker] result: {text[:60]}")
        except Exception as e:
            elapsed = (time.monotonic() - t0) * 1000
            emit_span("worker.llm.create", envelope.trace_id, span_id,
                      envelope.parent_span_id, elapsed, error=str(e))
        finally:
            queue.task_done()


async def demo_queue_propagation():
    queue: asyncio.Queue = asyncio.Queue()

    # Start worker
    worker_task = asyncio.create_task(worker(queue))

    # Simulate two incoming requests each with their own trace
    for i in range(2):
        trace_id     = uuid.uuid4().hex
        root_span_id = uuid.uuid4().hex[:16]
        t0 = time.monotonic()

        await enqueue_llm_task(
            queue,
            [{"role": "user", "content": f"Request {i}: explain eventual consistency."}],
            trace_id=trace_id,
            parent_span_id=root_span_id,
            user_id=f"u-{i}",
        )
        emit_span("http.handler", trace_id, root_span_id, None,
                  (time.monotonic() - t0) * 1000, request_id=i)

    await queue.join()
    await queue.put(None)
    await worker_task


asyncio.run(demo_queue_propagation())
```

---

## Comparison

| Approach | SDK dependency | Export target | Reconnection support | Overhead | Complexity |
|---|---|---|---|---|---|
| Manual contextvars + JSON | None | stdout / any log sink | No | Minimal | Low |
| OpenTelemetry SDK + OTLP | otel-sdk, otlp-exporter | Jaeger / Tempo / Honeycomb | Yes (OTLP retry) | Low | Medium |
| W3C Baggage propagation | None | Any structured logger | No | Minimal | Low |
| Head-based sampling | None | Configurable | No | Minimal | Medium |
| Tool-call span injection | None | stdout / any log sink | No | Minimal | Medium |
| Queue envelope propagation | None | Any worker + log sink | No | Minimal | Medium |

**Rule of thumb:**
- Greenfield service → OpenTelemetry SDK from day one; it's the standard
- Existing service with no OTel budget → manual contextvars + JSON logs (Solution 1)
- High-volume inference → add head-based sampling (Solution 4) on top of any approach
- Async workers / queues → always propagate trace context in the message envelope (Solution 6)
