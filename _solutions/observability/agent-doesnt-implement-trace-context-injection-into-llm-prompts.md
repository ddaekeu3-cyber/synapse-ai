---
title: "Agent doesn't implement trace context injection into LLM prompts"
description: "When debugging a multi-agent pipeline, there's no way to correlate a specific LLM response back to the distributed trace that triggered it. Trace IDs exist in the HTTP layer but never reach the model's input, so LLM reasoning and tool calls are invisible in traces."
difficulty: intermediate
category: observability
tags: [tracing, opentelemetry, distributed-tracing, correlation-id, debugging, multi-agent]
---

## Problem

In a distributed agent system, a single user request fans out across multiple services, model calls, and tool invocations. OpenTelemetry captures the HTTP spans, but the actual LLM prompt and response — the core of the agent's work — exist in a blind spot. When an incident occurs, you can see that `span: llm_call` took 3.2 seconds but not *what was asked*, *what was answered*, or *which tool calls the model chose to make*.

Trace context injection embeds the active trace ID and span ID into the LLM prompt (or as a system prompt prefix), enabling you to cross-reference every model response with its full distributed trace.

```python
# BAD: LLM calls create no trace context — invisible in Jaeger/Tempo
response = await client.messages.create(
    model="claude-sonnet-4-6",
    messages=[{"role": "user", "content": user_message}],
)
# Which trace triggered this? Unknown.
```

## Solution 1: Inject trace ID as a comment in the system prompt

Embed the current trace ID in a non-visible comment at the end of the system prompt. The model ignores it (it's after the instructions), but it appears in the stored prompt for later correlation.

```python
import asyncio
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from anthropic import AsyncAnthropic

# ── Setup minimal OTel tracer ─────────────────────────────────────────
exporter = InMemorySpanExporter()
provider = TracerProvider()
provider.add_span_processor(SimpleSpanProcessor(exporter))
trace.set_tracer_provider(provider)
tracer = trace.get_tracer("agent")

client = AsyncAnthropic()


def current_trace_context() -> dict[str, str]:
    """Extract current span's trace_id and span_id as hex strings."""
    ctx = trace.get_current_span().get_span_context()
    if not ctx.is_valid:
        return {}
    return {
        "trace_id": format(ctx.trace_id, "032x"),
        "span_id": format(ctx.span_id, "016x"),
    }


def inject_trace_into_system_prompt(system_prompt: str) -> str:
    """Append trace context as a hidden comment at the end of the system prompt."""
    tc = current_trace_context()
    if not tc:
        return system_prompt
    trace_comment = (
        f"\n\n<!-- trace_id={tc['trace_id']} span_id={tc['span_id']} -->"
    )
    return system_prompt + trace_comment


async def traced_llm_call(user_message: str, system: str = "You are a helpful assistant.") -> str:
    with tracer.start_as_current_span("llm.call") as span:
        enriched_system = inject_trace_into_system_prompt(system)
        tc = current_trace_context()

        # Set span attributes for the trace
        span.set_attribute("llm.model", "claude-sonnet-4-6")
        span.set_attribute("llm.trace_id", tc.get("trace_id", ""))
        span.set_attribute("llm.prompt_length", len(user_message))

        response = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=enriched_system,
            messages=[{"role": "user", "content": user_message}],
        )
        text = response.content[0].text
        span.set_attribute("llm.response_length", len(text))
        return text


async def main():
    with tracer.start_as_current_span("user.request") as root:
        tc = current_trace_context()
        print(f"Root trace_id: {tc['trace_id']}")
        result = await traced_llm_call("What is 2+2?")
        print(result)


asyncio.run(main())
```

## Solution 2: Structured trace metadata in tool call arguments

When the agent makes tool calls, inject the active trace context into the tool arguments so that downstream tool handlers can continue the trace.

```python
import asyncio
import json
from opentelemetry import trace
from opentelemetry.propagate import inject as otel_inject
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from typing import Any

exporter = InMemorySpanExporter()
provider = TracerProvider()
provider.add_span_processor(SimpleSpanProcessor(exporter))
trace.set_tracer_provider(provider)
tracer = trace.get_tracer("agent.tools")


def inject_trace_into_tool_args(args: dict[str, Any]) -> dict[str, Any]:
    """
    Add W3C traceparent header to tool arguments so downstream services
    can join the same distributed trace.
    """
    carrier: dict[str, str] = {}
    otel_inject(carrier)
    if carrier:
        args = dict(args)
        args["_trace_context"] = carrier
    return args


class TracedToolCaller:
    def __init__(self):
        self._calls: list[dict] = []

    async def call(self, tool_name: str, args: dict) -> Any:
        with tracer.start_as_current_span(f"tool.{tool_name}") as span:
            enriched_args = inject_trace_into_tool_args(args)
            span.set_attribute("tool.name", tool_name)
            span.set_attribute("tool.args_keys", list(args.keys()))

            # Simulate tool execution
            await asyncio.sleep(0.01)
            result = {"tool": tool_name, "status": "ok", "data": [1, 2, 3]}

            self._calls.append({
                "tool": tool_name,
                "trace_id": format(span.get_span_context().trace_id, "032x"),
                "span_id": format(span.get_span_context().span_id, "016x"),
            })
            return result

    def call_log(self) -> list[dict]:
        return self._calls


async def main():
    caller = TracedToolCaller()
    with tracer.start_as_current_span("agent.pipeline"):
        r1 = await caller.call("web_search", {"query": "AI agents"})
        r2 = await caller.call("summarize", {"text": str(r1["data"])})

    print("Tool calls with trace context:")
    for c in caller.call_log():
        print(f"  {c['tool']} → trace={c['trace_id'][:12]} span={c['span_id'][:8]}")


asyncio.run(main())
```

## Solution 3: LLM response annotation pipeline — stamp trace ID on every response

After receiving a model response, annotate it with the trace context before storing it in the conversation history or database. Enables log search by trace_id.

```python
import asyncio
import json
import time
from dataclasses import dataclass, field, asdict
from typing import Any
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

exporter = InMemorySpanExporter()
provider = TracerProvider()
provider.add_span_processor(SimpleSpanProcessor(exporter))
trace.set_tracer_provider(provider)
tracer = trace.get_tracer("agent.annotator")


@dataclass
class AnnotatedResponse:
    content: str
    model: str
    input_tokens: int
    output_tokens: int
    trace_id: str
    span_id: str
    timestamp: float = field(default_factory=time.time)
    metadata: dict = field(default_factory=dict)


class TracedResponseStore:
    """Stores annotated LLM responses with full trace context."""

    def __init__(self):
        self._responses: list[AnnotatedResponse] = []

    def record(self, response: AnnotatedResponse):
        self._responses.append(response)

    def find_by_trace(self, trace_id: str) -> list[AnnotatedResponse]:
        return [r for r in self._responses if r.trace_id == trace_id]

    def to_jsonl(self) -> str:
        return "\n".join(json.dumps(asdict(r)) for r in self._responses)


store = TracedResponseStore()


async def annotated_llm_call(
    messages: list[dict],
    system: str = "You are a helpful assistant.",
    **kwargs,
) -> AnnotatedResponse:
    """Make an LLM call and store the annotated response."""
    from anthropic import AsyncAnthropic
    client = AsyncAnthropic()

    with tracer.start_as_current_span("llm.annotated_call") as span:
        ctx = span.get_span_context()
        trace_id = format(ctx.trace_id, "032x")
        span_id = format(ctx.span_id, "016x")

        span.set_attribute("llm.trace_id", trace_id)

        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=system,
            messages=messages,
            **kwargs,
        )

        annotated = AnnotatedResponse(
            content=response.content[0].text,
            model=response.model,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            trace_id=trace_id,
            span_id=span_id,
        )
        store.record(annotated)
        return annotated


async def main():
    with tracer.start_as_current_span("root") as root_span:
        root_trace_id = format(root_span.get_span_context().trace_id, "032x")

        r = await annotated_llm_call([{"role": "user", "content": "What is 1+1?"}])
        print(f"Response: {r.content}")
        print(f"Stored with trace_id: {r.trace_id[:12]}")

        found = store.find_by_trace(root_trace_id)
        print(f"Found {len(found)} response(s) for trace {root_trace_id[:12]}")


asyncio.run(main())
```

## Solution 4: Baggage propagation — carry user/session context through the entire trace

Use OpenTelemetry baggage to propagate user ID, session ID, and agent type through every span. This appears in every span's attributes without manually threading the values through every function call.

```python
import asyncio
from opentelemetry import trace, baggage, context
from opentelemetry.baggage.propagation import W3CBaggagePropagator
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

exporter = InMemorySpanExporter()
provider = TracerProvider()
provider.add_span_processor(SimpleSpanProcessor(exporter))
trace.set_tracer_provider(provider)
tracer = trace.get_tracer("agent.baggage")


def set_request_baggage(user_id: str, session_id: str, agent_type: str) -> object:
    """Set baggage for this request. Returns the context token for cleanup."""
    ctx = baggage.set_baggage("user_id", user_id)
    ctx = baggage.set_baggage("session_id", session_id, context=ctx)
    ctx = baggage.set_baggage("agent_type", agent_type, context=ctx)
    token = context.attach(ctx)
    return token


def get_baggage_as_span_attrs(span: trace.Span):
    """Copy current baggage values onto the span for easier querying."""
    for key in ["user_id", "session_id", "agent_type"]:
        val = baggage.get_baggage(key)
        if val:
            span.set_attribute(f"baggage.{key}", val)


async def sub_agent_call(task: str) -> str:
    """Any nested call automatically inherits the baggage."""
    with tracer.start_as_current_span("sub_agent") as span:
        get_baggage_as_span_attrs(span)
        span.set_attribute("task", task)
        await asyncio.sleep(0.01)
        return f"result:{task}"


async def handle_request(user_id: str, session_id: str, message: str):
    token = set_request_baggage(user_id, session_id, agent_type="orchestrator")
    try:
        with tracer.start_as_current_span("orchestrator.handle") as span:
            get_baggage_as_span_attrs(span)
            result = await sub_agent_call(message)

        # All spans in this request will have user_id and session_id attributes
        finished = exporter.get_finished_spans()
        for s in finished[-2:]:
            attrs = dict(s.attributes or {})
            print(f"Span '{s.name}': user={attrs.get('baggage.user_id')} session={attrs.get('baggage.session_id')}")

        return result
    finally:
        context.detach(token)


asyncio.run(handle_request("user-42", "sess-abc", "search query"))
```

## Solution 5: Prompt-level trace log — write every prompt+response pair to a structured trace store

Create a dedicated prompt trace log that records every (trace_id, prompt, response, tokens, model) tuple. Enables offline analysis, cost attribution, and debugging by trace ID.

```python
import asyncio
import json
import hashlib
import time
from pathlib import Path
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from anthropic import AsyncAnthropic

exporter = InMemorySpanExporter()
provider = TracerProvider()
provider.add_span_processor(SimpleSpanProcessor(exporter))
trace.set_tracer_provider(provider)
tracer = trace.get_tracer("agent.prompt_trace")

PROMPT_TRACE_LOG = Path(".prompt_traces.jsonl")
client = AsyncAnthropic()


def _trace_ctx() -> dict[str, str]:
    ctx = trace.get_current_span().get_span_context()
    if not ctx.is_valid:
        return {"trace_id": "none", "span_id": "none"}
    return {
        "trace_id": format(ctx.trace_id, "032x"),
        "span_id": format(ctx.span_id, "016x"),
    }


def _prompt_hash(system: str, messages: list[dict]) -> str:
    payload = json.dumps({"system": system, "messages": messages}, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:12]


async def prompt_traced_call(
    messages: list[dict],
    system: str = "You are a helpful assistant.",
    model: str = "claude-haiku-4-5-20251001",
    max_tokens: int = 512,
) -> str:
    with tracer.start_as_current_span("llm.prompt_trace") as span:
        tc = _trace_ctx()
        prompt_hash = _prompt_hash(system, messages)

        span.set_attribute("prompt.hash", prompt_hash)
        span.set_attribute("llm.trace_id", tc["trace_id"])

        start = time.time()
        response = await client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=messages,
        )
        elapsed_ms = (time.time() - start) * 1000

        record = {
            **tc,
            "timestamp": start,
            "prompt_hash": prompt_hash,
            "model": model,
            "system_length": len(system),
            "user_message_length": sum(len(str(m)) for m in messages),
            "response_length": len(response.content[0].text),
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
            "latency_ms": round(elapsed_ms, 1),
        }

        with open(PROMPT_TRACE_LOG, "a") as f:
            f.write(json.dumps(record) + "\n")

        return response.content[0].text


async def main():
    with tracer.start_as_current_span("user.request"):
        result = await prompt_traced_call(
            [{"role": "user", "content": "What is the capital of Japan?"}]
        )
        print(result)
        print(f"Prompt trace written to: {PROMPT_TRACE_LOG}")


asyncio.run(main())
```

## Solution 6: Cross-agent trace stitching — propagate trace context via message headers in multi-agent calls

When one agent calls another via HTTP, inject the W3C `traceparent` header so the child agent's spans are stitched into the parent's trace.

```python
import asyncio
import httpx
from opentelemetry import trace
from opentelemetry.propagate import inject as otel_inject, extract as otel_extract
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

exporter = InMemorySpanExporter()
provider = TracerProvider()
provider.add_span_processor(SimpleSpanProcessor(exporter))
trace.set_tracer_provider(provider)
tracer = trace.get_tracer("agent.cross_agent")


class TracePropagatingClient:
    """
    HTTP client that automatically injects W3C traceparent/tracestate
    headers into every outgoing request, enabling cross-agent trace stitching.
    """

    async def post(self, url: str, json: dict) -> dict:
        headers: dict[str, str] = {}
        otel_inject(headers)   # adds traceparent (and tracestate if set)

        with tracer.start_as_current_span(
            "http.outbound", kind=trace.SpanKind.CLIENT
        ) as span:
            span.set_attribute("http.url", url)
            span.set_attribute("http.method", "POST")

            async with httpx.AsyncClient() as http:
                try:
                    resp = await http.post(url, json=json, headers=headers, timeout=10.0)
                    span.set_attribute("http.status_code", resp.status_code)
                    return resp.json()
                except httpx.RequestError as e:
                    span.record_exception(e)
                    raise


# ── Server-side: extract and continue the trace ───────────────────────
from fastapi import FastAPI, Request

app = FastAPI()


@app.post("/agent/execute")
async def execute(request: Request, body: dict):
    # Extract parent context from incoming headers
    parent_ctx = otel_extract(dict(request.headers))
    with tracer.start_as_current_span(
        "child_agent.execute",
        context=parent_ctx,
        kind=trace.SpanKind.SERVER,
    ) as span:
        span.set_attribute("agent.task", str(body.get("task", "")))
        # Child agent work — part of parent's trace
        await asyncio.sleep(0.05)
        return {"result": "ok", "task": body.get("task")}


async def main():
    client = TracePropagatingClient()
    with tracer.start_as_current_span("parent_agent.orchestrate"):
        # In a real setup this would hit a running child agent
        # client.post("http://child-agent/agent/execute", {"task": "search"})
        print("Trace propagation configured — parent spans linked to child")


asyncio.run(main())
```

## Comparison

| Approach | Overhead | Model-visible | Queryable | Cross-process | Bidirectional |
|---|---|---|---|---|---|
| System prompt comment injection | Negligible | Yes (comment) | Via log search | No | No |
| Tool arg trace injection | Negligible | Via args | In tool logs | Partial | No |
| Response annotation store | Low | No | Yes (indexed) | No | No |
| OTel baggage propagation | Negligible | No | Via spans | Yes | No |
| Prompt trace log | Low | No | Yes (JSONL) | No | No |
| W3C traceparent propagation | Negligible | No | Via Jaeger/Tempo | Yes | Yes |

**Recommendation**: Use **W3C traceparent propagation** (Solution 6) for cross-agent HTTP calls — this is the standard approach and works with any OpenTelemetry-compatible backend. Add **prompt trace logging** (Solution 5) for prompt-level cost analysis and debugging. Use **baggage propagation** (Solution 4) to thread user/session context through deep call stacks without explicit parameter passing.
