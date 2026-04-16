---
title: "Agent Doesn't Implement W3C Trace Context and Baggage Propagation"
description: "Distributed traces break at LLM boundaries because W3C traceparent/tracestate headers and baggage are not propagated through agent calls, making cross-service correlation impossible."
difficulty: intermediate
category: observability
tags: [tracing, w3c, opentelemetry, distributed-systems, observability, baggage]
---

# Agent Doesn't Implement W3C Trace Context and Baggage Propagation

## Problem

When an AI agent calls external services or LLM APIs, trace context is dropped at each boundary. Logs show isolated spans with no parent-child relationships, making it impossible to correlate a user request through HTTP gateway → agent → LLM → tool calls → database. The W3C Trace Context specification (traceparent, tracestate) and Baggage headers exist precisely for this, but agents typically ignore them entirely.

**Symptoms:**
- Traces appear as disconnected single-span islands in Jaeger/Zipkin
- Cannot answer "how long did the LLM call take for request X?"
- User-ID or session-ID not visible in LLM provider logs
- Tool calls have no link back to originating user request
- Cost attribution by trace/request is impossible

---

## Solution 1: Manual W3C traceparent Injection

Manually parse and propagate `traceparent` header across HTTP calls without a heavy OTel SDK.

```python
import asyncio
import secrets
import struct
import time
import httpx
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class TraceContext:
    trace_id: str          # 32 hex chars (128-bit)
    span_id: str           # 16 hex chars (64-bit)
    trace_flags: str = "01"  # sampled

    def to_traceparent(self) -> str:
        return f"00-{self.trace_id}-{self.span_id}-{self.trace_flags}"

    def child_span(self) -> "TraceContext":
        """Create a child span inheriting the same trace_id."""
        return TraceContext(
            trace_id=self.trace_id,
            span_id=secrets.token_hex(8),
            trace_flags=self.trace_flags,
        )

    @classmethod
    def from_traceparent(cls, header: str) -> Optional["TraceContext"]:
        parts = header.split("-")
        if len(parts) < 4 or parts[0] != "00":
            return None
        return cls(trace_id=parts[1], span_id=parts[2], trace_flags=parts[3])

    @classmethod
    def new_root(cls) -> "TraceContext":
        return cls(
            trace_id=secrets.token_hex(16),
            span_id=secrets.token_hex(8),
        )


class TracedAnthropicClient:
    def __init__(self, api_key: str):
        import anthropic
        self.client = anthropic.AsyncAnthropic(api_key=api_key)

    async def create_message(
        self,
        ctx: TraceContext,
        model: str,
        messages: list,
        system: str = "",
        max_tokens: int = 1024,
    ) -> dict:
        child = ctx.child_span()

        # W3C traceparent injected as custom header forwarded by SDK
        extra_headers = {
            "traceparent": child.to_traceparent(),
            "X-Request-ID": child.span_id,
        }

        start = time.perf_counter()
        response = await self.client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=messages,
            extra_headers=extra_headers,
        )
        elapsed = time.perf_counter() - start

        print(
            f"[trace] span={child.span_id} parent={ctx.span_id} "
            f"model={model} latency={elapsed:.3f}s "
            f"input={response.usage.input_tokens} output={response.usage.output_tokens}"
        )
        return {"response": response, "span": child}


async def handle_request(incoming_headers: dict) -> str:
    # Extract or create root trace context from incoming request
    traceparent = incoming_headers.get("traceparent")
    ctx = (
        TraceContext.from_traceparent(traceparent)
        if traceparent
        else TraceContext.new_root()
    )
    print(f"[trace] root trace_id={ctx.trace_id} span={ctx.span_id}")

    client = TracedAnthropicClient(api_key="sk-...")
    result = await client.create_message(
        ctx=ctx,
        model="claude-opus-4-6",
        messages=[{"role": "user", "content": "Summarize distributed tracing."}],
        max_tokens=256,
    )
    return result["response"].content[0].text


# asyncio.run(handle_request({"traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"}))
```

---

## Solution 2: W3C Baggage Propagation with User Context

Carry user metadata (user_id, session_id, tenant) through every LLM call via the W3C Baggage header.

```python
import asyncio
import secrets
from typing import Optional
from urllib.parse import quote, unquote
import anthropic


class Baggage:
    """W3C Baggage: key=value pairs, comma-separated, URL-encoded."""

    def __init__(self, entries: Optional[dict] = None):
        self._entries: dict[str, str] = entries or {}

    def set(self, key: str, value: str) -> "Baggage":
        new = Baggage(dict(self._entries))
        new._entries[key] = value
        return new

    def get(self, key: str) -> Optional[str]:
        return self._entries.get(key)

    def to_header(self) -> str:
        return ",".join(
            f"{quote(k, safe='')}={quote(v, safe='')}"
            for k, v in self._entries.items()
        )

    @classmethod
    def from_header(cls, header: str) -> "Baggage":
        entries = {}
        for part in header.split(","):
            part = part.strip()
            if "=" in part:
                k, v = part.split("=", 1)
                entries[unquote(k)] = unquote(v)
        return cls(entries)

    def inject_into_system_prompt(self, system: str) -> str:
        """Attach baggage context to system prompt for LLM visibility."""
        if not self._entries:
            return system
        meta = ", ".join(f"{k}={v}" for k, v in self._entries.items())
        return f"{system}\n\n[Request context: {meta}]"


class BaggageAwareAgent:
    def __init__(self, api_key: str):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)

    async def run(
        self,
        user_message: str,
        trace_id: str,
        baggage: Baggage,
        system_prompt: str = "You are a helpful assistant.",
    ) -> str:
        span_id = secrets.token_hex(8)
        traceparent = f"00-{trace_id}-{span_id}-01"

        # Inject user context into system prompt so LLM sees it
        enriched_system = baggage.inject_into_system_prompt(system_prompt)

        response = await self.client.messages.create(
            model="claude-opus-4-6",
            max_tokens=512,
            system=enriched_system,
            messages=[{"role": "user", "content": user_message}],
            extra_headers={
                "traceparent": traceparent,
                "baggage": baggage.to_header(),
            },
        )

        user_id = baggage.get("user_id") or "unknown"
        print(
            f"[baggage] trace={trace_id} span={span_id} user={user_id} "
            f"tokens={response.usage.output_tokens}"
        )
        return response.content[0].text


async def demo():
    baggage = (
        Baggage()
        .set("user_id", "usr_9k2mxp")
        .set("session_id", "sess_7qt3wr")
        .set("tenant", "acme-corp")
        .set("plan", "enterprise")
    )

    agent = BaggageAwareAgent(api_key="sk-...")
    trace_id = secrets.token_hex(16)

    answer = await agent.run(
        user_message="What are my options for upgrading?",
        trace_id=trace_id,
        baggage=baggage,
    )
    print(answer)

# asyncio.run(demo())
```

---

## Solution 3: OpenTelemetry SDK Full Integration

Use the official OTel Python SDK with OTLP export for production-grade distributed tracing.

```python
import asyncio
from opentelemetry import trace, baggage, context
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.propagate import extract, inject
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from opentelemetry.baggage.propagation import W3CBaggagePropagator
from opentelemetry.propagators.composite import CompositePropagator
import anthropic


def setup_otel(service_name: str, otlp_endpoint: str = "http://localhost:4317"):
    provider = TracerProvider()
    exporter = OTLPSpanExporter(endpoint=otlp_endpoint, insecure=True)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

    # Install W3C TraceContext + W3C Baggage propagators
    from opentelemetry import propagate
    propagate.set_global_textmap(
        CompositePropagator([TraceContextTextMapPropagator(), W3CBaggagePropagator()])
    )
    return trace.get_tracer(service_name)


tracer = setup_otel("ai-agent")


class OTelAnthropicAgent:
    def __init__(self, api_key: str):
        self.anthropic = anthropic.AsyncAnthropic(api_key=api_key)

    async def process(
        self,
        user_message: str,
        incoming_headers: dict,
    ) -> str:
        # Extract context from upstream caller
        ctx = extract(incoming_headers)

        with tracer.start_as_current_span(
            "agent.process",
            context=ctx,
            kind=trace.SpanKind.SERVER,
        ) as root_span:
            root_span.set_attribute("user.message.length", len(user_message))

            # Retrieve baggage set by upstream
            user_id = baggage.get_baggage("user_id", context.get_current())
            if user_id:
                root_span.set_attribute("user.id", user_id)

            result = await self._call_llm(user_message)
            root_span.set_attribute("llm.output.length", len(result))
            return result

    async def _call_llm(self, message: str) -> str:
        with tracer.start_as_current_span(
            "anthropic.messages.create",
            kind=trace.SpanKind.CLIENT,
        ) as llm_span:
            # Inject current OTel context into outgoing headers
            outgoing: dict = {}
            inject(outgoing)

            llm_span.set_attribute("llm.model", "claude-opus-4-6")
            llm_span.set_attribute("llm.provider", "anthropic")

            response = await self.anthropic.messages.create(
                model="claude-opus-4-6",
                max_tokens=512,
                messages=[{"role": "user", "content": message}],
                extra_headers=outgoing,
            )

            llm_span.set_attribute("llm.usage.input_tokens", response.usage.input_tokens)
            llm_span.set_attribute("llm.usage.output_tokens", response.usage.output_tokens)

            return response.content[0].text


async def demo():
    agent = OTelAnthropicAgent(api_key="sk-...")

    # Simulate headers arriving from upstream gateway
    incoming = {
        "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
        "baggage": "user_id=usr_9k2mxp,tenant=acme",
    }
    result = await agent.process("Explain OTel baggage.", incoming)
    print(result)

# asyncio.run(demo())
```

---

## Solution 4: Trace Context Middleware for FastAPI

Middleware that automatically extracts/injects trace context on every request/response cycle.

```python
import asyncio
import secrets
import time
from dataclasses import dataclass
from typing import Callable, Optional
import anthropic
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse


@dataclass
class RequestSpan:
    trace_id: str
    span_id: str
    parent_span_id: Optional[str]
    start_time: float

    def child(self) -> "RequestSpan":
        return RequestSpan(
            trace_id=self.trace_id,
            span_id=secrets.token_hex(8),
            parent_span_id=self.span_id,
            start_time=time.perf_counter(),
        )

    def elapsed_ms(self) -> float:
        return (time.perf_counter() - self.start_time) * 1000

    def to_traceparent(self) -> str:
        return f"00-{self.trace_id}-{self.span_id}-01"


_span_context: dict = {}  # request_id -> RequestSpan (simplified; use contextvars in prod)


import contextvars

_current_span: contextvars.ContextVar[Optional[RequestSpan]] = contextvars.ContextVar(
    "_current_span", default=None
)


class TraceMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers", []))
        traceparent = headers.get(b"traceparent", b"").decode()

        if traceparent:
            parts = traceparent.split("-")
            span = RequestSpan(
                trace_id=parts[1] if len(parts) > 1 else secrets.token_hex(16),
                span_id=secrets.token_hex(8),
                parent_span_id=parts[2] if len(parts) > 2 else None,
                start_time=time.perf_counter(),
            )
        else:
            span = RequestSpan(
                trace_id=secrets.token_hex(16),
                span_id=secrets.token_hex(8),
                parent_span_id=None,
                start_time=time.perf_counter(),
            )

        token = _current_span.set(span)

        async def send_with_trace(message):
            if message["type"] == "http.response.start":
                headers_list = list(message.get("headers", []))
                headers_list.append(
                    (b"x-trace-id", span.trace_id.encode())
                )
                headers_list.append(
                    (b"x-span-id", span.span_id.encode())
                )
                message = {**message, "headers": headers_list}
                print(
                    f"[trace] trace={span.trace_id} span={span.span_id} "
                    f"elapsed={span.elapsed_ms():.1f}ms"
                )
            await send(message)

        try:
            await self.app(scope, receive, send_with_trace)
        finally:
            _current_span.reset(token)


app = FastAPI()
app.middleware("http")(lambda app: TraceMiddleware(app))


@app.post("/agent/chat")
async def chat(request: Request):
    body = await request.json()
    span = _current_span.get()

    client = anthropic.AsyncAnthropic(api_key="sk-...")
    child = span.child() if span else None

    headers = {}
    if child:
        headers["traceparent"] = child.to_traceparent()

    response = await client.messages.create(
        model="claude-opus-4-6",
        max_tokens=256,
        messages=[{"role": "user", "content": body.get("message", "")}],
        extra_headers=headers,
    )
    return JSONResponse({"reply": response.content[0].text, "trace_id": span.trace_id if span else None})
```

---

## Solution 5: Multi-Hop Trace Propagation Through Tool Calls

Preserve trace context as the agent fans out to multiple tool calls in parallel.

```python
import asyncio
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Callable
import httpx
import anthropic


@dataclass
class Span:
    trace_id: str
    span_id: str
    parent_id: Optional[str]
    name: str
    start: float = field(default_factory=time.perf_counter)
    tags: dict = field(default_factory=dict)

    def finish(self) -> float:
        return (time.perf_counter() - self.start) * 1000

    def to_traceparent(self) -> str:
        return f"00-{self.trace_id}-{self.span_id}-01"

    def child(self, name: str) -> "Span":
        return Span(
            trace_id=self.trace_id,
            span_id=secrets.token_hex(8),
            parent_id=self.span_id,
            name=name,
        )


from typing import Optional


class TracedToolExecutor:
    def __init__(self, parent_span: Span):
        self.parent = parent_span
        self.completed_spans: list[Span] = []

    async def call_tool(
        self,
        tool_name: str,
        url: str,
        payload: dict,
    ) -> dict:
        span = self.parent.child(f"tool.{tool_name}")
        async with httpx.AsyncClient() as client:
            response = await client.post(
                url,
                json=payload,
                headers={
                    "traceparent": span.to_traceparent(),
                    "X-Tool-Name": tool_name,
                },
                timeout=10.0,
            )
        elapsed = span.finish()
        span.tags["http.status_code"] = response.status_code
        span.tags["tool.latency_ms"] = elapsed
        self.completed_spans.append(span)
        print(f"[trace] {tool_name}: {elapsed:.1f}ms trace={span.trace_id}")
        return response.json()

    async def run_tools_parallel(self, tools: list[dict]) -> list[dict]:
        tasks = [
            self.call_tool(t["name"], t["url"], t["payload"])
            for t in tools
        ]
        return await asyncio.gather(*tasks, return_exceptions=True)


class MultiHopAgent:
    def __init__(self, api_key: str):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)

    async def run(self, user_query: str, incoming_traceparent: Optional[str] = None) -> str:
        # Build root span from incoming context or create new root
        if incoming_traceparent:
            parts = incoming_traceparent.split("-")
            root = Span(trace_id=parts[1], span_id=secrets.token_hex(8),
                        parent_id=parts[2], name="agent.run")
        else:
            root = Span(trace_id=secrets.token_hex(16), span_id=secrets.token_hex(8),
                        parent_id=None, name="agent.run")

        executor = TracedToolExecutor(parent_span=root)

        # Parallel tool calls each get their own child span
        tool_results = await executor.run_tools_parallel([
            {"name": "search", "url": "http://search-svc/query", "payload": {"q": user_query}},
            {"name": "context", "url": "http://context-svc/fetch", "payload": {"key": user_query}},
        ])

        llm_span = root.child("anthropic.messages.create")
        response = await self.client.messages.create(
            model="claude-opus-4-6",
            max_tokens=512,
            messages=[{"role": "user", "content": f"{user_query}\nContext: {tool_results}"}],
            extra_headers={"traceparent": llm_span.to_traceparent()},
        )
        print(f"[trace] LLM: {llm_span.finish():.1f}ms total_spans={len(executor.completed_spans)+2}")
        return response.content[0].text
```

---

## Solution 6: Trace-Aware Conversation History with Span Linking

Link every turn in a multi-turn conversation back to its originating trace, enabling per-turn latency analysis.

```python
import asyncio
import secrets
import time
from dataclasses import dataclass, field
from typing import Optional
import anthropic


@dataclass
class TurnSpan:
    trace_id: str           # Stable across entire conversation
    conversation_id: str    # Stable conversation identifier
    turn_index: int
    span_id: str = field(default_factory=lambda: secrets.token_hex(8))
    start: float = field(default_factory=time.perf_counter)
    input_tokens: int = 0
    output_tokens: int = 0

    def elapsed_ms(self) -> float:
        return (time.perf_counter() - self.start) * 1000

    def to_traceparent(self) -> str:
        return f"00-{self.trace_id}-{self.span_id}-01"

    def to_baggage(self) -> str:
        return (
            f"conversation_id={self.conversation_id},"
            f"turn_index={self.turn_index}"
        )


@dataclass
class ConversationTrace:
    conversation_id: str
    trace_id: str
    turns: list[TurnSpan] = field(default_factory=list)

    def next_turn(self) -> TurnSpan:
        span = TurnSpan(
            trace_id=self.trace_id,
            conversation_id=self.conversation_id,
            turn_index=len(self.turns),
        )
        self.turns.append(span)
        return span

    def summary(self) -> dict:
        return {
            "conversation_id": self.conversation_id,
            "trace_id": self.trace_id,
            "turns": len(self.turns),
            "total_input_tokens": sum(t.input_tokens for t in self.turns),
            "total_output_tokens": sum(t.output_tokens for t in self.turns),
            "avg_latency_ms": (
                sum(t.elapsed_ms() for t in self.turns) / len(self.turns)
                if self.turns else 0
            ),
        }


class TracedConversationAgent:
    def __init__(self, api_key: str):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)
        self.history: list[dict] = []

    async def chat(
        self,
        user_message: str,
        conv_trace: ConversationTrace,
        system: str = "You are a helpful assistant.",
    ) -> str:
        self.history.append({"role": "user", "content": user_message})
        turn = conv_trace.next_turn()

        response = await self.client.messages.create(
            model="claude-opus-4-6",
            max_tokens=512,
            system=system,
            messages=self.history,
            extra_headers={
                "traceparent": turn.to_traceparent(),
                "baggage": turn.to_baggage(),
            },
        )

        text = response.content[0].text
        turn.input_tokens = response.usage.input_tokens
        turn.output_tokens = response.usage.output_tokens
        self.history.append({"role": "assistant", "content": text})

        print(
            f"[trace] turn={turn.turn_index} span={turn.span_id} "
            f"latency={turn.elapsed_ms():.1f}ms "
            f"in={turn.input_tokens} out={turn.output_tokens}"
        )
        return text


async def demo():
    conv_trace = ConversationTrace(
        conversation_id=f"conv_{secrets.token_hex(6)}",
        trace_id=secrets.token_hex(16),
    )
    agent = TracedConversationAgent(api_key="sk-...")

    messages = [
        "What is distributed tracing?",
        "How does W3C Baggage differ from traceparent?",
        "Give me a Python example.",
    ]
    for msg in messages:
        reply = await agent.chat(msg, conv_trace)
        print(f"Assistant: {reply[:80]}...")

    print("\nConversation summary:", conv_trace.summary())

# asyncio.run(demo())
```

---

## Comparison

| Solution | Mechanism | OTel SDK | Baggage Support | Multi-hop | Complexity |
|---|---|---|---|---|---|
| Manual traceparent | Header injection | No | No | Manual | Low |
| W3C Baggage | Header + system prompt injection | No | Yes | Manual | Low |
| OTel SDK full | TracerProvider + OTLP export | Yes | Yes | Auto | High |
| FastAPI middleware | contextvars + response headers | No | Partial | No | Medium |
| Multi-hop parallel | Child spans per tool call | No | No | Yes | Medium |
| Conversation trace | Per-turn spans with stable trace_id | No | Yes | Yes | Low |

**Recommendation:** Use Solution 3 (OTel SDK) for production with OTLP export to Jaeger/Grafana Tempo. Use Solution 1 or 6 when you can't add SDK dependencies. Always propagate at minimum `traceparent` + `baggage: user_id=...` on every LLM call.
