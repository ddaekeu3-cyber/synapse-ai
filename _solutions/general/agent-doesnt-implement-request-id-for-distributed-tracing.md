---
layout: solution
title: "Agent Doesn't Implement Request ID for Distributed Tracing"
category: general
description: "Agent makes API calls without attaching a correlation ID, making it impossible to trace a specific request through logs, debug failures in production, or link user-reported errors to log entries."
tags: [observability, tracing, logging, debugging, request-id, correlation]
---

## Symptom

A user reports that their request failed at 14:32 UTC. The logs show hundreds of concurrent requests with no way to identify which log lines belong to that user's session. Debugging requires filtering by approximate timestamp and guessing. Multi-step agent tasks produce log entries across 10+ API calls with no shared identifier linking them. Post-mortem analysis is manual and error-prone.

## Root Cause

Each `client.messages.create()` call is independent — the Anthropic SDK generates a random ID on the server, but the agent never records or propagates this. The agent has no concept of a "request context" that flows through a multi-step task. Log entries from different tools, sub-agents, or retry attempts cannot be correlated. The `anthropic-request-id` response header exists but is never read or forwarded.

## Fix

### Option 1: UUID request ID attached to every API call and log entry

```python
import uuid
import logging
import anthropic

# Structured logger — use structlog in production
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s request_id=%(request_id)s %(message)s",
)


class RequestContext:
    """Carries a request ID through the lifetime of a single user request."""

    def __init__(self, request_id: str | None = None):
        self.request_id = request_id or str(uuid.uuid4())
        self.logger = logging.LoggerAdapter(
            logging.getLogger(__name__),
            {"request_id": self.request_id},
        )

    def log(self, level: str, message: str, **extra) -> None:
        getattr(self.logger, level)(f"{message} {extra}" if extra else message)


client = anthropic.Anthropic()


def call_claude(ctx: RequestContext, messages: list[dict], **kwargs) -> anthropic.types.Message:
    ctx.log("info", "API call started", model=kwargs.get("model", "claude-sonnet-4-6"))

    response = client.messages.create(
        messages=messages,
        model=kwargs.get("model", "claude-sonnet-4-6"),
        max_tokens=kwargs.get("max_tokens", 512),
    )

    # Capture the server-side request ID from the response headers
    server_request_id = getattr(response, "_request_id", None)

    ctx.log(
        "info",
        "API call complete",
        anthropic_request_id=server_request_id,
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
        stop_reason=response.stop_reason,
    )
    return response


def run_agent_task(user_message: str) -> str:
    ctx = RequestContext()  # One ID per user request
    ctx.log("info", "Task started", user_message=user_message[:80])

    messages = [{"role": "user", "content": user_message}]
    response = call_claude(ctx, messages)

    ctx.log("info", "Task complete")
    return response.content[0].text


# Each task gets a unique, traceable ID
result = run_agent_task("Explain what a correlation ID is in distributed systems.")
print(result[:200])
```

**Expected Token Savings:** Indirect — tracing enables faster debugging which reduces operational overhead and retry costs from misdiagnosed failures.
**Environment:** Python 3.9+; swap `logging.LoggerAdapter` for structlog or OpenTelemetry in production.

---

### Option 2: Context variable propagation through async call chains

```python
import asyncio
import uuid
from contextvars import ContextVar
import anthropic

client = anthropic.AsyncAnthropic()

# ContextVar is async-safe — each asyncio Task gets its own copy
_request_id: ContextVar[str] = ContextVar("request_id", default="unset")
_session_id: ContextVar[str] = ContextVar("session_id", default="unset")


def get_request_id() -> str:
    return _request_id.get()


def get_session_id() -> str:
    return _session_id.get()


def log(message: str, **extra) -> None:
    rid = get_request_id()
    sid = get_session_id()
    parts = [f"request_id={rid}", f"session_id={sid}"]
    parts += [f"{k}={v}" for k, v in extra.items()]
    print(f"[LOG] {message} | {' '.join(parts)}")


async def call_tool_a(data: str) -> str:
    log("tool_a started")  # Automatically has request_id via ContextVar
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        messages=[{"role": "user", "content": f"Tool A processes: {data}"}],
    )
    log("tool_a complete")
    return response.content[0].text


async def call_tool_b(data: str) -> str:
    log("tool_b started")
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        messages=[{"role": "user", "content": f"Tool B analyzes: {data}"}],
    )
    log("tool_b complete")
    return response.content[0].text


async def handle_request(session_id: str, user_message: str) -> str:
    # Set context vars — automatically propagated to all awaited calls
    _request_id.set(str(uuid.uuid4()))
    _session_id.set(session_id)

    log("request started", message_length=len(user_message))

    # Both tools run with the same request_id in their logs
    result_a, result_b = await asyncio.gather(
        call_tool_a(user_message),
        call_tool_b(user_message),
    )

    log("request complete")
    return f"{result_a}\n{result_b}"


async def main():
    # Simulate two concurrent requests — each gets its own request_id
    results = await asyncio.gather(
        handle_request("session-alice", "Question about Python"),
        handle_request("session-bob", "Question about asyncio"),
    )
    for r in results:
        print(f"Result: {r[:80]}\n")


asyncio.run(main())
```

**Expected Token Savings:** Zero direct savings — but ContextVar propagation costs nothing and makes debugging multi-step async agents tractable.
**Environment:** Python 3.7+; `contextvars.ContextVar` is natively async-safe; compatible with FastAPI, aiohttp, and any async framework.

---

### Option 3: OpenTelemetry span wrapping for API calls

```python
import uuid
import anthropic
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any


# Lightweight span implementation (replace with opentelemetry-sdk in production)
@dataclass
class Span:
    name: str
    trace_id: str
    span_id: str
    parent_id: str | None
    attributes: dict[str, Any] = field(default_factory=dict)
    events: list[dict] = field(default_factory=list)
    _ended: bool = False

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value

    def add_event(self, name: str, **attrs) -> None:
        self.events.append({"name": name, **attrs})

    def end(self) -> None:
        self._ended = True
        print(
            f"[SPAN] {self.name} "
            f"trace={self.trace_id[:8]} span={self.span_id[:8]} "
            f"parent={self.parent_id[:8] if self.parent_id else 'root'} "
            f"attrs={self.attributes}"
        )


_active_span: Span | None = None


@contextmanager
def start_span(name: str):
    global _active_span
    parent = _active_span
    span = Span(
        name=name,
        trace_id=parent.trace_id if parent else str(uuid.uuid4()),
        span_id=str(uuid.uuid4()),
        parent_id=parent.span_id if parent else None,
    )
    _active_span = span
    try:
        yield span
    except Exception as e:
        span.set_attribute("error", str(e))
        raise
    finally:
        span.end()
        _active_span = parent


client = anthropic.Anthropic()


def traced_api_call(messages: list[dict], model: str = "claude-sonnet-4-6", max_tokens: int = 512) -> str:
    with start_span("anthropic.messages.create") as span:
        span.set_attribute("model", model)
        span.set_attribute("input_messages", len(messages))

        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            messages=messages,
        )

        span.set_attribute("input_tokens", response.usage.input_tokens)
        span.set_attribute("output_tokens", response.usage.output_tokens)
        span.set_attribute("stop_reason", response.stop_reason)
        return response.content[0].text


def run_multi_step_task(user_input: str) -> str:
    with start_span("agent.task") as task_span:
        task_span.set_attribute("user_input_length", len(user_input))

        # Step 1: plan
        with start_span("agent.plan"):
            plan = traced_api_call(
                [{"role": "user", "content": f"Create a brief plan for: {user_input}"}],
                model="claude-haiku-4-5-20251001",
                max_tokens=128,
            )

        # Step 2: execute
        with start_span("agent.execute"):
            result = traced_api_call(
                [
                    {"role": "user", "content": user_input},
                    {"role": "assistant", "content": f"My plan: {plan}"},
                    {"role": "user", "content": "Now execute the plan."},
                ],
                max_tokens=512,
            )

        task_span.set_attribute("steps_completed", 2)
        return result


print(run_multi_step_task("Explain distributed tracing to a junior developer"))
```

**Expected Token Savings:** Tracing reveals which steps are slowest and most expensive — guides targeted optimization.
**Environment:** Python 3.9+; replace stub with `opentelemetry-sdk` + `opentelemetry-exporter-jaeger` for production.

---

### Option 4: Request ID injected into system prompt for self-reporting

```python
import uuid
import anthropic

client = anthropic.Anthropic()


def make_system_prompt_with_request_id(request_id: str, base_system: str) -> str:
    """
    Inject the request ID into the system prompt so the model can reference it.
    Useful when the agent generates reports or error messages that need to be traceable.
    """
    return f"""{base_system}

<request_metadata>
request_id: {request_id}
</request_metadata>

If you encounter an error or cannot complete a task, include the request_id in your response so the user can reference it when contacting support."""


def build_request_context(user_id: str) -> dict:
    return {
        "request_id": str(uuid.uuid4()),
        "user_id": user_id,
        "session_id": str(uuid.uuid4())[:8],
    }


def run_agent(user_id: str, user_message: str) -> tuple[str, dict]:
    ctx = build_request_context(user_id)

    system = make_system_prompt_with_request_id(
        ctx["request_id"],
        "You are a helpful assistant.",
    )

    # Log the outgoing request
    print(f"[REQ {ctx['request_id'][:8]}] user={user_id} message={user_message[:60]}")

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )

    reply = response.content[0].text

    # Log the response
    print(
        f"[RES {ctx['request_id'][:8]}] "
        f"tokens={response.usage.input_tokens}+{response.usage.output_tokens} "
        f"stop={response.stop_reason}"
    )

    return reply, ctx


# Users get a request ID they can cite
reply, ctx = run_agent("user-42", "Summarize the benefits of request tracing.")
print(f"\nResponse:\n{reply[:300]}")
print(f"\nSupport reference: request_id={ctx['request_id']}")
```

**Expected Token Savings:** Adds ~20 tokens per call (request_id injection) — negligible vs. the debugging cost it prevents.
**Environment:** Python 3.9+; request_id in system prompt enables users to self-report traceable errors.

---

### Option 5: Middleware-style request ID wrapper for FastAPI

```python
import uuid
import time
from contextvars import ContextVar
from typing import Callable

import anthropic

# In a real FastAPI app, this would be a proper middleware
_request_id: ContextVar[str] = ContextVar("request_id", default="")
_request_log: list[dict] = []  # In production: structured logging or OTLP

client = anthropic.Anthropic()


class TracedAnthropicClient:
    """
    Wrapper around the Anthropic client that automatically attaches
    the current request ID to every API call's log entry.
    """

    def __init__(self, api_key: str | None = None):
        self._client = anthropic.Anthropic(api_key=api_key)

    def messages_create(self, **kwargs) -> anthropic.types.Message:
        rid = _request_id.get() or str(uuid.uuid4())
        model = kwargs.get("model", "unknown")
        start = time.perf_counter()

        log_entry = {
            "request_id": rid,
            "event": "anthropic_call",
            "model": model,
            "ts": time.time(),
        }

        try:
            response = self._client.messages.create(**kwargs)
            log_entry.update({
                "status": "ok",
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
                "latency_ms": round((time.perf_counter() - start) * 1000),
            })
            return response
        except Exception as e:
            log_entry.update({"status": "error", "error": str(e)})
            raise
        finally:
            _request_log.append(log_entry)
            print(f"[TRACE] {log_entry}")


traced_client = TracedAnthropicClient()


def handle_web_request(user_id: str, message: str) -> dict:
    """Simulates a FastAPI route handler."""
    request_id = str(uuid.uuid4())
    _request_id.set(request_id)

    start = time.perf_counter()
    print(f"[REQ] request_id={request_id} user_id={user_id}")

    try:
        response = traced_client.messages_create(
            model="claude-sonnet-4-6",
            max_tokens=256,
            messages=[{"role": "user", "content": message}],
        )
        reply = response.content[0].text

        return {
            "request_id": request_id,
            "response": reply,
            "latency_ms": round((time.perf_counter() - start) * 1000),
        }
    except Exception as e:
        return {
            "request_id": request_id,
            "error": str(e),
            "latency_ms": round((time.perf_counter() - start) * 1000),
        }


# Simulate requests
r1 = handle_web_request("alice", "What is distributed tracing?")
r2 = handle_web_request("bob", "How does a correlation ID work?")

print(f"\nRequest log ({len(_request_log)} entries):")
for entry in _request_log:
    print(f"  {entry}")
```

**Expected Token Savings:** Middleware pattern costs nothing per token; prevents expensive manual log correlation sessions.
**Environment:** Python 3.9+; `ContextVar` is thread- and async-safe; drop-in for FastAPI middleware.

---

### Option 6: Request ID propagation in multi-agent orchestration

```python
import uuid
import asyncio
import anthropic

client = anthropic.AsyncAnthropic()


class TraceContext:
    """Propagates trace context across agent boundaries."""

    def __init__(self, trace_id: str | None = None, parent_span_id: str | None = None):
        self.trace_id = trace_id or str(uuid.uuid4())
        self.span_id = str(uuid.uuid4())
        self.parent_span_id = parent_span_id
        self._spans: list[dict] = []

    def child(self, name: str) -> "TraceContext":
        """Create a child span with the same trace_id."""
        child_ctx = TraceContext(trace_id=self.trace_id, parent_span_id=self.span_id)
        return child_ctx

    def log(self, event: str, **attrs) -> None:
        span = {
            "trace_id": self.trace_id[:8],
            "span_id": self.span_id[:8],
            "parent": self.parent_span_id[:8] if self.parent_span_id else "root",
            "event": event,
            **attrs,
        }
        self._spans.append(span)
        print(f"[TRACE] trace={span['trace_id']} span={span['span_id']} parent={span['parent']} | {event} {attrs}")


async def sub_agent_summarize(ctx: TraceContext, text: str) -> str:
    """Sub-agent that receives and propagates trace context."""
    child_ctx = ctx.child("sub_agent.summarize")
    child_ctx.log("started", text_length=len(text))

    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{"role": "user", "content": f"Summarize: {text}"}],
    )

    child_ctx.log("complete", tokens=response.usage.input_tokens + response.usage.output_tokens)
    return response.content[0].text


async def sub_agent_classify(ctx: TraceContext, text: str) -> str:
    """Another sub-agent sharing the same trace."""
    child_ctx = ctx.child("sub_agent.classify")
    child_ctx.log("started")

    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=32,
        messages=[{"role": "user", "content": f"Classify as technical/business/other: {text[:100]}"}],
    )

    child_ctx.log("complete")
    return response.content[0].text


async def orchestrator(user_message: str) -> dict:
    """Root orchestrator — creates the trace and passes context to sub-agents."""
    ctx = TraceContext()
    ctx.log("orchestrator_started", user_message=user_message[:60])

    # Both sub-agents share the same trace_id — their spans are linked
    summary, classification = await asyncio.gather(
        sub_agent_summarize(ctx, user_message),
        sub_agent_classify(ctx, user_message),
    )

    ctx.log("orchestrator_complete", sub_agents=2)
    return {
        "trace_id": ctx.trace_id,
        "summary": summary,
        "classification": classification,
    }


result = asyncio.run(orchestrator(
    "Distributed tracing allows you to follow a request as it travels through multiple services."
))
print(f"\nResult: {result}")
print(f"\nAll log entries linked by trace_id={result['trace_id'][:8]}")
```

**Expected Token Savings:** Trace context propagation across sub-agents makes failures instantly attributable — eliminates multi-hour debugging sessions that lead to expensive manual re-runs.
**Environment:** Python 3.11+; trace context pattern is compatible with OpenTelemetry W3C trace context standard.

---

| Option | Approach | Propagation Scope | Best For |
|--------|----------|------------------|----------|
| 1 | UUID + LoggerAdapter | Single process | Simple agents |
| 2 | ContextVar | Async call chains | Async microservices |
| 3 | OpenTelemetry spans | Full trace tree | Production observability |
| 4 | Request ID in system prompt | User-visible errors | Support traceability |
| 5 | Client wrapper middleware | HTTP request boundary | FastAPI/web agents |
| 6 | Context propagation to sub-agents | Multi-agent orchestration | Orchestrator + sub-agents |
