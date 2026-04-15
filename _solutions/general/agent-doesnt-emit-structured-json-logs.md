---
layout: solution
title: "Agent Doesn't Emit Structured JSON Logs"
category: general
description: "Agent uses print() or unstructured logging; log lines can't be parsed, queried, or alerted on in production observability stacks."
tags: [general, logging, observability, production, debugging]
---

## Symptom

In production, the agent emits lines like `Processing task 42 — done in 1.2s` that operators can't filter, aggregate, or alert on. When a Claude API call fails, the only evidence is a stack trace with no context about which user, session, or task triggered it. Log queries in Datadog, CloudWatch, or Loki fail because the field names are inconsistent across log lines. On-call engineers spend hours manually grepping through logs instead of querying structured fields.

## Root Cause

`print()` and Python's default `logging.basicConfig()` emit flat strings. Production observability tools expect JSON objects with stable field names so they can index, aggregate, and alert on specific fields (e.g., `level`, `task_id`, `model`, `latency_ms`, `error_code`). Without structured fields, every log becomes opaque text that requires regex parsing — fragile, slow, and error-prone.

## Fix

### Option 1 — python-json-logger: drop-in structured replacement

```python
import logging
import anthropic
from pythonjsonlogger import jsonlogger  # pip install python-json-logger

# Configure once at startup
handler = logging.StreamHandler()
handler.setFormatter(jsonlogger.JsonFormatter(
    fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
))
logging.root.setLevel(logging.INFO)
logging.root.addHandler(handler)

log = logging.getLogger("agent")

client = anthropic.Anthropic()

def process_task(task_id: int, user_id: str) -> str:
    log.info("task_started", extra={"task_id": task_id, "user_id": user_id})
    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=128,
            messages=[{"role": "user", "content": f"Summarise task {task_id}."}],
        )
        result = response.content[0].text
        log.info("task_completed", extra={
            "task_id":        task_id,
            "user_id":        user_id,
            "input_tokens":   response.usage.input_tokens,
            "output_tokens":  response.usage.output_tokens,
            "model":          response.model,
        })
        return result
    except anthropic.APIError as e:
        log.error("api_error", extra={
            "task_id":    task_id,
            "user_id":    user_id,
            "status":     getattr(e, "status_code", None),
            "error_type": type(e).__name__,
        })
        raise

process_task(42, "user-abc")
```

**Expected Token Savings:** Structured logs make it trivial to query average `input_tokens` per task and identify expensive patterns without re-running anything.
**Environment:** Any Python agent; python-json-logger is a one-line addition to any existing logging setup.

---

### Option 2 — structlog: rich context binding with processors

```python
import structlog
import anthropic
import time

# pip install structlog
structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(20),  # INFO level
    logger_factory=structlog.PrintLoggerFactory(),
)

log = structlog.get_logger()

client = anthropic.Anthropic()

def run_agent_session(session_id: str, tasks: list[str]) -> None:
    # Bind session-level context — appears in every subsequent log call
    session_log = log.bind(session_id=session_id, agent="summariser")

    for i, task in enumerate(tasks):
        task_log = session_log.bind(task_index=i, task_preview=task[:40])
        task_log.info("task_start")

        t0 = time.monotonic()
        try:
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=256,
                messages=[{"role": "user", "content": task}],
            )
            latency_ms = int((time.monotonic() - t0) * 1000)
            task_log.info("task_ok", latency_ms=latency_ms,
                          input_tokens=response.usage.input_tokens,
                          output_tokens=response.usage.output_tokens)
        except Exception as e:
            latency_ms = int((time.monotonic() - t0) * 1000)
            task_log.error("task_error", latency_ms=latency_ms,
                           error=str(e), error_type=type(e).__name__)

run_agent_session(
    session_id="sess-001",
    tasks=["Summarise the history of Python.", "Explain asyncio in one paragraph."],
)
```

**Expected Token Savings:** Context binding (`session_id`, `task_index`) lets you trace total token spend per session without writing any aggregation code — your log ingestion tool does it for free.
**Environment:** Complex agents with multiple nested contexts (session → task → tool call); structlog's processor pipeline makes it easy to add fields globally.

---

### Option 3 — Correlation ID propagation across tool calls

```python
import logging
import uuid
import functools
import anthropic
from pythonjsonlogger import jsonlogger

handler = logging.StreamHandler()
handler.setFormatter(jsonlogger.JsonFormatter())
logging.root.setLevel(logging.INFO)
logging.root.addHandler(handler)
log = logging.getLogger("agent")

client = anthropic.Anthropic()

def with_correlation_id(func):
    """Decorator that generates a correlation_id and threads it through all log calls."""
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        cid = str(uuid.uuid4())[:8]
        bound = log.getChild(func.__name__)
        # Pass cid via kwargs so inner functions can log it
        return func(*args, **kwargs, _cid=cid, _log=bound)
    return wrapper

@with_correlation_id
def agent_run(user_request: str, _cid: str = "", _log=None) -> str:
    _log.info("agent_run_start", extra={"cid": _cid, "request_preview": user_request[:60]})

    tools = [{"name": "get_data", "description": "Fetch data.",
               "input_schema": {"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"]}}]

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        tools=tools,
        messages=[{"role": "user", "content": user_request}],
    )

    _log.info("llm_response", extra={
        "cid":           _cid,
        "stop_reason":   response.stop_reason,
        "input_tokens":  response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
        "num_blocks":    len(response.content),
    })

    for block in response.content:
        if block.type == "tool_use":
            _log.info("tool_call", extra={
                "cid":       _cid,
                "tool":      block.name,
                "input_key": block.input.get("key", ""),
            })

    _log.info("agent_run_end", extra={"cid": _cid})
    return response.content[0].text if response.content else ""

agent_run("Fetch data for key=abc123 and summarise it.")
```

**Expected Token Savings:** Correlation IDs let you reconstruct the full token spend for a single user request across multiple log lines — essential for per-request cost attribution.
**Environment:** Multi-tool agents in production; essential for distributed tracing when multiple services share a log sink.

---

### Option 4 — Log levels with automatic alert thresholds

```python
import logging
import anthropic
import time
from pythonjsonlogger import jsonlogger

handler = logging.StreamHandler()
handler.setFormatter(jsonlogger.JsonFormatter())
logging.root.setLevel(logging.DEBUG)
logging.root.addHandler(handler)
log = logging.getLogger("agent")

client = anthropic.Anthropic()

# Thresholds for automatic level escalation
LATENCY_WARN_MS  = 5_000
LATENCY_ERROR_MS = 15_000
TOKEN_WARN       = 50_000

def call_claude(prompt: str, task_id: str) -> str:
    t0 = time.monotonic()
    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )
        latency_ms = int((time.monotonic() - t0) * 1000)
        total_tokens = response.usage.input_tokens + response.usage.output_tokens

        # Choose log level based on observed metrics
        if latency_ms >= LATENCY_ERROR_MS:
            level = logging.ERROR
        elif latency_ms >= LATENCY_WARN_MS or total_tokens >= TOKEN_WARN:
            level = logging.WARNING
        else:
            level = logging.INFO

        log.log(level, "claude_call", extra={
            "task_id":       task_id,
            "latency_ms":    latency_ms,
            "input_tokens":  response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
            "total_tokens":  total_tokens,
            "model":         response.model,
        })
        return response.content[0].text

    except anthropic.RateLimitError as e:
        latency_ms = int((time.monotonic() - t0) * 1000)
        log.warning("rate_limit", extra={"task_id": task_id, "latency_ms": latency_ms,
                                          "retry_after": getattr(e, "retry_after", None)})
        raise
    except anthropic.APIError as e:
        latency_ms = int((time.monotonic() - t0) * 1000)
        log.error("api_error", extra={"task_id": task_id, "latency_ms": latency_ms,
                                       "status": getattr(e, "status_code", None),
                                       "error": str(e)})
        raise

call_claude("Summarise the benefits of structured logging.", task_id="task-001")
```

**Expected Token Savings:** Automatic WARNING on high token counts surfaces runaway prompts in your alert system before they accumulate into a large bill at month end.
**Environment:** Production agents with SLA requirements; integrates with any alerting system that watches log levels (PagerDuty, Opsgenie, CloudWatch Alarms).

---

### Option 5 — OpenTelemetry logs with trace context

```python
import anthropic
import time
import uuid
# pip install opentelemetry-api opentelemetry-sdk opentelemetry-exporter-otlp-proto-grpc
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor
import logging
from pythonjsonlogger import jsonlogger

# Set up OTel tracing (swap ConsoleSpanExporter for OTLP in production)
provider = TracerProvider()
provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
trace.set_tracer_provider(provider)
tracer = trace.get_tracer("agent")

# Standard JSON logger — will include trace IDs
handler = logging.StreamHandler()
handler.setFormatter(jsonlogger.JsonFormatter())
logging.root.setLevel(logging.INFO)
logging.root.addHandler(handler)
log = logging.getLogger("agent")

client = anthropic.Anthropic()

def get_trace_context() -> dict:
    span = trace.get_current_span()
    ctx = span.get_span_context()
    if ctx.is_valid:
        return {
            "trace_id": format(ctx.trace_id, "032x"),
            "span_id":  format(ctx.span_id, "016x"),
        }
    return {}

def run_task(task_id: str, prompt: str) -> str:
    with tracer.start_as_current_span(f"agent.task.{task_id}") as span:
        span.set_attribute("task.id", task_id)
        span.set_attribute("prompt.length", len(prompt))
        ctx = get_trace_context()

        log.info("task_start", extra={"task_id": task_id, **ctx})
        t0 = time.monotonic()

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=[{"role": "user", "content": prompt}],
        )

        latency_ms = int((time.monotonic() - t0) * 1000)
        span.set_attribute("tokens.input",  response.usage.input_tokens)
        span.set_attribute("tokens.output", response.usage.output_tokens)
        span.set_attribute("latency_ms",    latency_ms)

        log.info("task_done", extra={
            "task_id":      task_id,
            "latency_ms":   latency_ms,
            "input_tokens": response.usage.input_tokens,
            **ctx,
        })
        return response.content[0].text

run_task("t-001", "What are the benefits of distributed tracing?")
```

**Expected Token Savings:** Linking logs to traces lets you see exactly which Claude call contributed to a slow trace — enables per-span token attribution for cost optimisation.
**Environment:** Agents deployed behind API gateways with existing OTel infrastructure (Jaeger, Tempo, Honeycomb).

---

### Option 6 — Cloud-native log sink: emit to CloudWatch / GCP Logging

```python
import logging
import json
import sys
import anthropic
import time

client = anthropic.Anthropic()

class CloudStructuredFormatter(logging.Formatter):
    """
    Emits JSON logs compatible with AWS CloudWatch Logs Insights and GCP Cloud Logging.
    AWS expects 'level'; GCP expects 'severity'.
    """
    SEVERITY_MAP = {
        "DEBUG":    "DEBUG",
        "INFO":     "INFO",
        "WARNING":  "WARNING",
        "ERROR":    "ERROR",
        "CRITICAL": "CRITICAL",
    }

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp":  self.formatTime(record, "%Y-%m-%dT%H:%M:%S.%f"),
            "severity":   self.SEVERITY_MAP.get(record.levelname, "DEFAULT"),
            "level":      record.levelname,
            "logger":     record.name,
            "message":    record.getMessage(),
        }
        # Merge any extra fields passed via extra={}
        for key, val in record.__dict__.items():
            if key not in logging.LogRecord.__dict__ and not key.startswith("_"):
                if key not in ("msg", "args", "exc_info", "exc_text", "stack_info",
                               "lineno", "funcName", "created", "msecs", "relativeCreated",
                               "thread", "threadName", "processName", "process",
                               "levelname", "levelno", "name", "pathname", "filename",
                               "module", "message"):
                    payload[key] = val
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload)

handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(CloudStructuredFormatter())
logging.root.setLevel(logging.INFO)
logging.root.addHandler(handler)
log = logging.getLogger("agent.cloud")

def process_batch(items: list[str], batch_id: str) -> list[str]:
    log.info("batch_start", extra={"batch_id": batch_id, "item_count": len(items)})
    results = []
    for idx, item in enumerate(items):
        t0 = time.monotonic()
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=128,
            messages=[{"role": "user", "content": item}],
        )
        ms = int((time.monotonic() - t0) * 1000)
        log.info("item_processed", extra={
            "batch_id":      batch_id,
            "item_index":    idx,
            "latency_ms":    ms,
            "input_tokens":  response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
        })
        results.append(response.content[0].text)
    log.info("batch_done", extra={"batch_id": batch_id, "processed": len(results)})
    return results

process_batch(
    ["What is asyncio?", "What is structlog?"],
    batch_id="batch-2026-001",
)
```

**Expected Token Savings:** CloudWatch Logs Insights queries on `input_tokens` and `output_tokens` fields give per-batch cost dashboards for free — no extra monitoring infrastructure needed.
**Environment:** Agents deployed on AWS (Lambda, ECS, EC2) or GCP (Cloud Run, GKE); logs ship automatically to the cloud provider's native log ingestion.

---

## Comparison

| Option | Library | Context Binding | Trace Integration | Cloud Ready | Best For |
|---|---|---|---|---|---|
| 1. python-json-logger | python-json-logger | Manual `extra={}` | No | Yes | Drop-in for existing `logging` users |
| 2. structlog | structlog | `bind()` — automatic | Via processor | Yes | Complex nested contexts; processor pipeline |
| 3. Correlation ID | python-json-logger | Manual propagation | Manual | Yes | Multi-tool request tracing |
| 4. Log levels + alerts | python-json-logger | Manual `extra={}` | No | Yes | Ops teams; automatic alerting on thresholds |
| 5. OpenTelemetry | pythonjsonlogger + OTel | Span context auto-inject | Yes (OTel) | Yes (OTLP) | Full observability stack; distributed tracing |
| 6. Cloud-native sink | Custom formatter | Manual `extra={}` | No | Native | AWS / GCP deployments; no extra libraries |
