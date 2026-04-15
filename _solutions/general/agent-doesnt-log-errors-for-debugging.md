---
layout: solution
title: "Agent Doesn't Log Errors for Debugging"
category: general
description: "Agent swallows exceptions silently, prints bare strings with no context, or logs so verbosely that real errors are invisible, making production incidents impossible to diagnose."
tags: [general, logging, observability, debugging, reliability, structured-logging]
---

## Symptom

An on-call engineer receives an alert that the agent is returning generic "Something went wrong" responses. They check the logs and find: nothing (exceptions were caught and suppressed), or a wall of `DEBUG: token=...` lines with no errors visible, or bare `print("error!")` strings with no traceback, request ID, user ID, or timestamp. Reproducing the bug requires guesswork because the failure context was never captured.

## Root Cause

Agent code is often written to prioritise user-facing resilience (never crash, always return something) at the expense of operational visibility. `try/except Exception: pass` blocks silence errors entirely. `print()` statements lack structure, severity levels, and context fields. Without correlation IDs, it is impossible to trace a single request through multiple tool calls or LLM turns.

## Fix

### Option 1 — Structured logging with `structlog`

```python
import structlog
import anthropic
import uuid

# Configure structlog to output JSON in production, pretty-print in dev
structlog.configure(
    processors=[
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.JSONRenderer(),
    ]
)

log    = structlog.get_logger()
client = anthropic.Anthropic()

def ask(user_message: str, session_id: str | None = None) -> str:
    session_id = session_id or str(uuid.uuid4())[:8]
    request_log = log.bind(session_id=session_id, user_message=user_message[:80])

    request_log.info("agent.request.start")
    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=[{"role": "user", "content": user_message}],
        )
        reply = response.content[0].text
        request_log.info(
            "agent.request.complete",
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            stop_reason=response.stop_reason,
        )
        return reply
    except anthropic.RateLimitError as e:
        request_log.warning("agent.rate_limit", error=str(e))
        return "I'm temporarily unavailable due to high demand. Please try again."
    except anthropic.APIStatusError as e:
        request_log.error("agent.api_error", status_code=e.status_code, error=str(e))
        return "I encountered an API error. Please try again."
    except Exception as e:
        request_log.exception("agent.unexpected_error", error=str(e))
        raise   # re-raise unexpected errors — don't silently swallow

session = str(uuid.uuid4())[:8]
for msg in ["What is the capital of Japan?", "Tell me a Python tip."]:
    reply = ask(msg, session_id=session)
    print(f"Reply: {reply[:100]}\n")
```

**Expected Token Savings:** No token reduction; structured logs reduce MTTR (mean time to resolution) from hours to minutes by making errors immediately searchable.
**Environment:** All production agents; structlog is the baseline observability requirement.

---

### Option 2 — Python `logging` with request-scoped context

```python
import logging
import uuid
import contextvars
import anthropic

# Standard library logging with JSON-compatible formatter
logging.basicConfig(
    level=logging.INFO,
    format='{"time":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","message":"%(message)s","extra":%(extra_fields)s}',
)

# Context variable for request-scoped fields (works across async too)
request_context: contextvars.ContextVar[dict] = contextvars.ContextVar("request_context", default={})

class ContextFilter(logging.Filter):
    """Injects request-scoped context into every log record."""
    def filter(self, record: logging.LogRecord) -> bool:
        ctx = request_context.get({})
        record.extra_fields = str(ctx).replace("'", '"')
        return True

logger = logging.getLogger("agent")
logger.addFilter(ContextFilter())
client = anthropic.Anthropic()

def set_request_context(request_id: str, user_id: str | None = None) -> None:
    request_context.set({"request_id": request_id, "user_id": user_id or "anonymous"})

def ask(user_message: str, user_id: str | None = None) -> str:
    request_id = str(uuid.uuid4())
    set_request_context(request_id, user_id)

    logger.info(f"request.start | message={user_message[:60]!r}")
    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=[{"role": "user", "content": user_message}],
        )
        reply = response.content[0].text
        logger.info(
            f"request.complete | tokens={response.usage.input_tokens}+{response.usage.output_tokens}"
        )
        return reply
    except anthropic.BadRequestError as e:
        logger.warning(f"request.bad_request | error={e}")
        return "Your request contained invalid content. Please rephrase."
    except Exception:
        logger.exception("request.failed")
        return "An unexpected error occurred."

for msg in ["Hello!", "What is asyncio?"]:
    reply = ask(msg, user_id="user-42")
    print(f"Reply: {reply[:80]}\n")
```

**Expected Token Savings:** Request-scoped context (request_id, user_id) allows filtering logs to a single failed request; eliminates manual log archaeology.
**Environment:** Agents deployed without external logging libraries; stdlib logging with context injection provides 80% of structlog's value.

---

### Option 3 — Tool call audit log with full input/output capture

```python
import json
import time
import uuid
import logging
import anthropic

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log    = logging.getLogger("agent.tools")
client = anthropic.Anthropic()

TOOLS = [
    {
        "name": "get_weather",
        "description": "Get current weather for a city.",
        "input_schema": {
            "type": "object",
            "required": ["city"],
            "properties": {"city": {"type": "string"}},
        },
    }
]

def execute_tool(name: str, args: dict, call_id: str) -> tuple[str, bool]:
    """Execute tool and return (result_json, is_error)."""
    t0 = time.perf_counter()
    try:
        # Simulated tool
        if name == "get_weather":
            result = {"city": args["city"], "temp_c": 22, "condition": "sunny"}
            elapsed = time.perf_counter() - t0
            log.info(
                "tool.success | call_id=%s tool=%s city=%s elapsed_ms=%.1f",
                call_id, name, args.get("city"), elapsed * 1000,
            )
            return json.dumps(result), False
        raise ValueError(f"Unknown tool: {name}")
    except Exception as e:
        elapsed = time.perf_counter() - t0
        log.error(
            "tool.error | call_id=%s tool=%s args=%s error=%s elapsed_ms=%.1f",
            call_id, name, args, str(e), elapsed * 1000,
        )
        return json.dumps({"error": str(e)}), True

def run_agent(user_message: str) -> str:
    request_id = str(uuid.uuid4())[:8]
    log.info("agent.start | request_id=%s message=%r", request_id, user_message[:60])

    messages = [{"role": "user", "content": user_message}]
    step = 0
    while step < 8:
        step += 1
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            tools=TOOLS,
            messages=messages,
        )
        log.info(
            "agent.llm_call | request_id=%s step=%d stop_reason=%s tokens=%d+%d",
            request_id, step, response.stop_reason,
            response.usage.input_tokens, response.usage.output_tokens,
        )

        if response.stop_reason == "end_turn":
            reply = next((b.text for b in response.content if b.type == "text"), "")
            log.info("agent.complete | request_id=%s steps=%d", request_id, step)
            return reply

        messages.append({"role": "assistant", "content": response.content})
        results = []
        for b in response.content:
            if b.type == "tool_use":
                call_id = b.id[:8]
                result, is_error = execute_tool(b.name, b.input, call_id)
                results.append({
                    "type": "tool_result",
                    "tool_use_id": b.id,
                    "content": result,
                    "is_error": is_error,
                })
        messages.append({"role": "user", "content": results})

    log.warning("agent.max_steps | request_id=%s", request_id)
    return "Max steps reached."

print(run_agent("What is the weather in Tokyo?"))
```

**Expected Token Savings:** Tool audit logs capture every input and output; when a tool returns unexpected data, logs reveal the exact payload without requiring a reproduction run.
**Environment:** All tool-using agents; tool call logging is mandatory for production debugging.

---

### Option 4 — Error aggregation with rate-limited alerting

```python
import time
import collections
import logging
import anthropic

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log    = logging.getLogger("agent")
client = anthropic.Anthropic()

class ErrorAggregator:
    """Counts errors by type and alerts when rate exceeds threshold."""

    def __init__(self, window_seconds: int = 60, alert_threshold: int = 5):
        self.window   = window_seconds
        self.threshold = alert_threshold
        self._counts: dict[str, collections.deque] = collections.defaultdict(collections.deque)

    def record(self, error_type: str) -> None:
        now = time.monotonic()
        dq  = self._counts[error_type]
        dq.append(now)
        # Evict events outside the window
        while dq and dq[0] < now - self.window:
            dq.popleft()

        rate = len(dq)
        log.error("error.recorded | type=%s count_in_window=%d", error_type, rate)

        if rate >= self.threshold:
            self._alert(error_type, rate)

    def _alert(self, error_type: str, count: int) -> None:
        # In production: send to PagerDuty, Slack, Datadog, etc.
        log.critical(
            "ALERT: error spike | type=%s count=%d window=%ds — investigate immediately",
            error_type, count, self.window,
        )

aggregator = ErrorAggregator(window_seconds=60, alert_threshold=3)

def ask(user_message: str) -> str:
    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=[{"role": "user", "content": user_message}],
        )
        return response.content[0].text
    except anthropic.RateLimitError as e:
        aggregator.record("rate_limit")
        log.warning("rate_limit | %s", e)
        return "Service is busy. Please retry shortly."
    except anthropic.APIConnectionError as e:
        aggregator.record("connection_error")
        log.error("connection_error | %s", e)
        return "Network error. Please try again."
    except anthropic.BadRequestError as e:
        aggregator.record("bad_request")
        log.warning("bad_request | %s", e)
        return "Invalid request. Please rephrase."
    except Exception as e:
        aggregator.record("unexpected")
        log.exception("unexpected_error | %s", e)
        return "An unexpected error occurred."

# Simulate burst of errors to trigger alert
for i in range(5):
    # Intentionally pass a very long message to stress-test
    result = ask(f"Question {i}: " + "word " * 50)
    print(f"[{i}] {result[:60]}")
    time.sleep(0.1)
```

**Expected Token Savings:** Error rate monitoring detects systemic failures (bad API key, model deprecation, network partition) within seconds; reduces incident duration from hours to minutes.
**Environment:** Production agents serving multiple users; error aggregation distinguishes isolated failures from systemic outages.

---

### Option 5 — Distributed tracing with trace/span IDs

```python
import time
import uuid
import logging
import functools
import anthropic
from typing import Callable, Any

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log    = logging.getLogger("agent.trace")
client = anthropic.Anthropic()

class Span:
    """Lightweight trace span for correlating log events."""

    def __init__(self, name: str, trace_id: str, parent_id: str | None = None):
        self.name      = name
        self.span_id   = str(uuid.uuid4())[:8]
        self.trace_id  = trace_id
        self.parent_id = parent_id
        self.start     = time.perf_counter()
        self.tags: dict = {}

    def tag(self, **kwargs: Any) -> "Span":
        self.tags.update(kwargs)
        return self

    def __enter__(self) -> "Span":
        log.info("span.start | trace=%s span=%s name=%s parent=%s",
                 self.trace_id, self.span_id, self.name, self.parent_id or "root")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        elapsed = (time.perf_counter() - self.start) * 1000
        if exc_type:
            log.error("span.error | trace=%s span=%s name=%s elapsed_ms=%.1f error=%s",
                      self.trace_id, self.span_id, self.name, elapsed, exc_val)
        else:
            log.info("span.end | trace=%s span=%s name=%s elapsed_ms=%.1f tags=%s",
                     self.trace_id, self.span_id, self.name, elapsed, self.tags)
        return False  # do not suppress exceptions

def new_trace() -> str:
    return str(uuid.uuid4())[:8]

def run_traced_agent(user_message: str) -> str:
    trace_id = new_trace()

    with Span("agent.request", trace_id) as root_span:
        root_span.tag(message_len=len(user_message))
        messages = [{"role": "user", "content": user_message}]
        step = 0

        while step < 6:
            step += 1
            with Span("agent.llm_call", trace_id, root_span.span_id) as llm_span:
                response = client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=256,
                    messages=messages,
                )
                llm_span.tag(
                    step=step,
                    stop_reason=response.stop_reason,
                    input_tokens=response.usage.input_tokens,
                    output_tokens=response.usage.output_tokens,
                )

            if response.stop_reason == "end_turn":
                return next((b.text for b in response.content if b.type == "text"), "")

            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": b.id, "content": "{}"}
                for b in response.content if b.type == "tool_use"
            ]})

    return "completed"

print(run_traced_agent("Explain what a Python generator is."))
```

**Expected Token Savings:** Trace IDs allow filtering all log events for a single request across multiple services; identifies which LLM call or tool call is responsible for latency spikes.
**Environment:** Multi-service agents where a single user request spans the agent, tool servers, and databases.

---

### Option 6 — Debug mode with full request/response capture

```python
import json
import os
import logging
import anthropic

logging.basicConfig(level=logging.DEBUG if os.getenv("AGENT_DEBUG") else logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log    = logging.getLogger("agent")
client = anthropic.Anthropic()

DEBUG = os.getenv("AGENT_DEBUG", "").lower() in {"1", "true", "yes"}

def redact(text: str, max_len: int = 200) -> str:
    """Truncate long text for log safety."""
    if len(text) <= max_len:
        return text
    return text[:max_len] + f"...[{len(text) - max_len} chars truncated]"

def ask(user_message: str) -> str:
    if DEBUG:
        log.debug("REQUEST | message=%s", redact(user_message))

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=[{"role": "user", "content": user_message}],
        )
    except Exception as e:
        log.exception("API_CALL_FAILED | error=%s", e)
        raise

    if DEBUG:
        log.debug(
            "RESPONSE | stop_reason=%s input_tokens=%d output_tokens=%d",
            response.stop_reason,
            response.usage.input_tokens,
            response.usage.output_tokens,
        )
        for i, block in enumerate(response.content):
            if block.type == "text":
                log.debug("RESPONSE.block[%d] text=%s", i, redact(block.text))
            elif block.type == "tool_use":
                log.debug("RESPONSE.block[%d] tool=%s input=%s", i, block.name,
                          redact(json.dumps(block.input)))

    reply = next((b.text for b in response.content if b.type == "text"), "")
    log.info("agent.ok | tokens=%d+%d reply_len=%d",
             response.usage.input_tokens, response.usage.output_tokens, len(reply))
    return reply

# Normal mode: set AGENT_DEBUG=1 in env to see full request/response
for msg in ["What is a closure?", "How does GC work in Python?"]:
    print(ask(msg)[:100])
    print()
```

**Expected Token Savings:** Debug mode is off in production (no log overhead); enabling it for a specific session captures full context needed to reproduce failures without code changes.
**Environment:** All agents; debug flag controlled by environment variable allows selective verbosity without redeployment.

---

## Comparison

| Option | Log Structure | Request Correlation | Alert on Spikes | Best For |
|---|---|---|---|---|
| 1. `structlog` JSON | JSON, machine-readable | Yes (bind) | No | Production baseline — searchable logs |
| 2. stdlib + context vars | Semi-structured | Yes (ContextVar) | No | No extra dependencies |
| 3. Tool call audit log | Structured per call | Partial | No | Tool-heavy agents |
| 4. Error aggregation | Counts by type | No | Yes | Multi-user production agents |
| 5. Trace/span IDs | Full trace tree | Yes (trace_id) | No | Multi-service distributed agents |
| 6. Debug mode flag | Full request/response | No | No | Local debugging and incident reproduction |
