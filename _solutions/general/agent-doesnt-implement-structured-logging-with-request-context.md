---
layout: solution
title: "Agent Doesn't Implement Structured Logging with Request Context"
category: general
description: "Agents using print() or unstructured logging make it impossible to correlate events across a multi-step task, trace failures to their origin, or query logs in production."
tags: [general, logging, observability, structured-logs, trace-id, fastapi]
---

# Agent Doesn't Implement Structured Logging with Request Context

`print(f"Got response: {result}")` is fine for local debugging but fails in production. Without structured JSON logs with a consistent `request_id` or `trace_id` field, it's impossible to filter all events for a single agent run, correlate tool calls to the request that triggered them, or set up alerts on error rates.

## Why This Happens

Structured logging has a learning curve. The standard `logging` module defaults to text format. Developers reach for `print()` and never revisit it as the system grows.

---

## Option 1: structlog with Request ID via contextvars

Use `structlog` with `contextvars` to bind a `request_id` at the start of each request that flows through all subsequent log calls automatically.

```python
import uuid
import contextvars
import structlog
import anthropic
from fastapi import FastAPI, Request

# Context variable holding the current request's log context
_log_ctx: contextvars.ContextVar[dict] = contextvars.ContextVar("log_ctx", default={})

structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.BoundLogger,
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory(),
)

log = structlog.get_logger()
app = FastAPI()
client = anthropic.Anthropic()


@app.middleware("http")
async def inject_request_id(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(
        request_id=request_id,
        path=request.url.path,
        method=request.method,
    )
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response


@app.post("/agent/run")
async def run_agent(prompt: str):
    log.info("agent.run.started", prompt_length=len(prompt))

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        result = response.content[0].text
        log.info(
            "agent.run.completed",
            output_tokens=response.usage.output_tokens,
            input_tokens=response.usage.input_tokens,
        )
        return {"result": result}
    except Exception as exc:
        log.error("agent.run.failed", error=str(exc), exc_info=True)
        raise
```

**Expected Token Savings:** No direct savings; enables fast incident triage by filtering `request_id=X` in your log aggregator.

**Environment:** FastAPI + structlog; any Python async web framework.

---

## Option 2: Standard Library logging with JSON Formatter

Use Python's built-in `logging` module with a custom JSON formatter — no extra dependencies.

```python
import json
import logging
import time
import uuid
import contextvars
from typing import Any
import anthropic

_request_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default=""
)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        log_entry: dict[str, Any] = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": _request_id.get(""),
        }
        # Merge any extra fields passed to the log call
        for key, value in record.__dict__.items():
            if key not in (
                "name", "msg", "args", "levelname", "levelno", "pathname",
                "filename", "module", "exc_info", "exc_text", "stack_info",
                "lineno", "funcName", "created", "msecs", "relativeCreated",
                "thread", "threadName", "processName", "process", "message",
            ):
                log_entry[key] = value

        if record.exc_info:
            log_entry["exception"] = self.formatException(record.exc_info)

        return json.dumps(log_entry, default=str)


def setup_logging(level: str = "INFO"):
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)


setup_logging()
log = logging.getLogger("agent")
client = anthropic.Anthropic()


def run_with_context(prompt: str, request_id: str | None = None) -> str:
    rid = request_id or str(uuid.uuid4())
    token = _request_id.set(rid)

    try:
        log.info("Starting agent run", extra={"prompt_chars": len(prompt)})

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text
        log.info(
            "Agent run complete",
            extra={
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
                "model": response.model,
            },
        )
        return text
    except anthropic.RateLimitError:
        log.warning("Rate limited", extra={"retry_after": "unknown"})
        raise
    except Exception:
        log.exception("Agent run failed")
        raise
    finally:
        _request_id.reset(token)


if __name__ == "__main__":
    result = run_with_context("What is 2+2?")
    print(result)
```

**Expected Token Savings:** Zero-dependency structured logging; JSON output is directly ingestible by Datadog, CloudWatch, Loki.

**Environment:** Any Python project; production deployments where adding structlog is restricted.

---

## Option 3: Trace Span Logging for Multi-Step Pipelines

Log each pipeline step as a span with `start_time`, `duration_ms`, and `parent_span_id` for timeline reconstruction.

```python
import time
import uuid
import json
import logging
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Generator
import anthropic

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger("trace")
client = anthropic.Anthropic()


@dataclass
class Span:
    name: str
    trace_id: str
    span_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    parent_id: str = ""
    start_time: float = field(default_factory=time.monotonic)
    attributes: dict = field(default_factory=dict)

    def finish(self, **extra):
        duration = (time.monotonic() - self.start_time) * 1000
        log.info(
            json.dumps({
                "type": "span",
                "trace_id": self.trace_id,
                "span_id": self.span_id,
                "parent_id": self.parent_id,
                "name": self.name,
                "duration_ms": round(duration, 2),
                **self.attributes,
                **extra,
            }, default=str)
        )


@contextmanager
def span(
    name: str,
    trace_id: str,
    parent_id: str = "",
    **attrs,
) -> Generator[Span, None, None]:
    s = Span(name=name, trace_id=trace_id, parent_id=parent_id, attributes=attrs)
    try:
        yield s
        s.finish(status="ok")
    except Exception as exc:
        s.finish(status="error", error=str(exc))
        raise


def run_pipeline(document: str) -> str:
    trace_id = str(uuid.uuid4())[:16]

    with span("pipeline", trace_id, document_chars=len(document)) as root:
        # Step 1: Extract
        with span("extract", trace_id, parent_id=root.span_id) as s1:
            extract_resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=512,
                messages=[{"role": "user", "content": f"Extract key facts:\n{document[:5000]}"}],
            )
            s1.attributes["tokens"] = extract_resp.usage.input_tokens + extract_resp.usage.output_tokens
            extracted = extract_resp.content[0].text

        # Step 2: Summarize
        with span("summarize", trace_id, parent_id=root.span_id) as s2:
            summary_resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=256,
                messages=[{"role": "user", "content": f"Summarize:\n{extracted}"}],
            )
            s2.attributes["tokens"] = summary_resp.usage.input_tokens + summary_resp.usage.output_tokens
            return summary_resp.content[0].text


if __name__ == "__main__":
    result = run_pipeline("Long document content " * 100)
    print("\nResult:", result[:100])
```

**Expected Token Savings:** Pipeline spans expose which step is slowest and most expensive; guides optimization.

**Environment:** Multi-step agent pipelines; logs consumed by Jaeger, Zipkin, or any trace-aware aggregator.

---

## Option 4: Request-Scoped Log Buffer for Error Replay

Buffer all log events for a request in memory; emit them only on error, suppressing noise from successful requests.

```python
import uuid
import json
import time
import logging
from contextlib import contextmanager
from collections import deque
from typing import Generator
import anthropic

client = anthropic.Anthropic()


class BufferedLogger:
    """Buffers log records; flushes to output only on error (or if debug=True)."""

    def __init__(self, request_id: str, max_records: int = 100):
        self.request_id = request_id
        self._buffer: deque[dict] = deque(maxlen=max_records)

    def log(self, level: str, event: str, **fields):
        self._buffer.append({
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "level": level,
            "event": event,
            "request_id": self.request_id,
            **fields,
        })

    def info(self, event: str, **fields):
        self.log("INFO", event, **fields)

    def warning(self, event: str, **fields):
        self.log("WARNING", event, **fields)

    def error(self, event: str, **fields):
        self.log("ERROR", event, **fields)

    def flush(self):
        """Emit all buffered records."""
        for record in self._buffer:
            print(json.dumps(record, default=str))
        self._buffer.clear()

    def flush_on_error(self, exc: Exception):
        self.error("request.failed", error=str(exc), type=type(exc).__name__)
        self.flush()


@contextmanager
def request_logging(debug: bool = False) -> Generator[BufferedLogger, None, None]:
    logger = BufferedLogger(request_id=str(uuid.uuid4()))
    try:
        yield logger
        if debug:
            logger.flush()
        # Success: buffer discarded silently
    except Exception as exc:
        logger.flush_on_error(exc)
        raise


def run_agent(prompt: str) -> str:
    with request_logging() as log:
        log.info("agent.start", prompt_length=len(prompt))

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )

        log.info(
            "agent.llm.done",
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )

        result = response.content[0].text
        log.info("agent.complete", result_length=len(result))
        return result


if __name__ == "__main__":
    # Success: nothing printed (buffer discarded)
    run_agent("Hello")

    # Error: full request history printed for debugging
    try:
        run_agent("" * 0)  # simulate error
    except Exception:
        pass
```

**Expected Token Savings:** Eliminates log noise from healthy requests; only failed requests emit logs, reducing log ingestion costs.

**Environment:** High-volume production agents; any Python app; pairs well with Loki or CloudWatch.

---

## Option 5: OpenTelemetry Tracing Integration

Emit traces to an OTLP collector (Jaeger, Tempo, Honeycomb) using the OpenTelemetry SDK.

```python
# pip install opentelemetry-sdk opentelemetry-exporter-otlp-proto-grpc
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
import anthropic

# Configure OTel tracer
resource = Resource.create({"service.name": "synapse-agent"})
provider = TracerProvider(resource=resource)
exporter = OTLPSpanExporter(endpoint="http://localhost:4317", insecure=True)
provider.add_span_processor(BatchSpanProcessor(exporter))
trace.set_tracer_provider(provider)

tracer = trace.get_tracer("synapse.agent")
client = anthropic.Anthropic()


def run_agent(prompt: str, user_id: str = "anonymous") -> str:
    with tracer.start_as_current_span("agent.run") as root_span:
        root_span.set_attribute("user.id", user_id)
        root_span.set_attribute("prompt.length", len(prompt))

        with tracer.start_as_current_span("llm.call") as llm_span:
            llm_span.set_attribute("model", "claude-haiku-4-5-20251001")
            try:
                response = client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=1024,
                    messages=[{"role": "user", "content": prompt}],
                )
                llm_span.set_attribute("tokens.input", response.usage.input_tokens)
                llm_span.set_attribute("tokens.output", response.usage.output_tokens)
                result = response.content[0].text
            except Exception as exc:
                llm_span.record_exception(exc)
                llm_span.set_status(trace.StatusCode.ERROR, str(exc))
                raise

        root_span.set_attribute("result.length", len(result))
        return result


def run_pipeline(document: str) -> dict:
    with tracer.start_as_current_span("pipeline") as pipeline_span:
        pipeline_span.set_attribute("document.chars", len(document))

        with tracer.start_as_current_span("step.extract"):
            extract = run_agent(f"Extract facts: {document[:3000]}")

        with tracer.start_as_current_span("step.summarize"):
            summary = run_agent(f"Summarize: {extract}")

        return {"extract": extract, "summary": summary}


if __name__ == "__main__":
    result = run_pipeline("Some long document " * 100)
    print(result["summary"][:200])
```

**Expected Token Savings:** Full distributed traces in Jaeger/Honeycomb; visualize which pipeline step costs the most tokens.

**Environment:** Production; requires OTLP collector; integrates with Grafana, Honeycomb, Datadog APM.

---

## Option 6: Async Structured Logging with aiohttp

Structured JSON logging in an async aiohttp agent server with per-request context binding.

```python
import asyncio
import json
import time
import uuid
import logging
from aiohttp import web
import anthropic

client = anthropic.AsyncAnthropic()


class AsyncJsonLogger:
    def __init__(self, name: str):
        self._log = logging.getLogger(name)
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        self._log.handlers = [handler]
        self._log.setLevel(logging.DEBUG)
        self._ctx: dict = {}

    def bind(self, **fields):
        self._ctx.update(fields)
        return self

    def _emit(self, level: str, event: str, **fields):
        record = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "level": level,
            "event": event,
            **self._ctx,
            **fields,
        }
        self._log.log(
            getattr(logging, level),
            json.dumps(record, default=str),
        )

    def info(self, event: str, **f): self._emit("INFO", event, **f)
    def warning(self, event: str, **f): self._emit("WARNING", event, **f)
    def error(self, event: str, **f): self._emit("ERROR", event, **f)


async def agent_handler(request: web.Request) -> web.Response:
    log = AsyncJsonLogger("agent").bind(
        request_id=request.headers.get("X-Request-ID", str(uuid.uuid4())),
        remote=request.remote,
    )

    try:
        body = await request.json()
        prompt = body.get("prompt", "")
        log.info("request.received", prompt_length=len(prompt))

        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        result = response.content[0].text
        log.info(
            "request.completed",
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )
        return web.json_response({"result": result})

    except Exception as exc:
        log.error("request.error", error=str(exc))
        return web.json_response({"error": str(exc)}, status=500)


app = web.Application()
app.router.add_post("/agent", agent_handler)

if __name__ == "__main__":
    web.run_app(app, port=8080)
```

**Expected Token Savings:** Structured logs with `request_id` enable instant per-request cost queries in your log aggregator.

**Environment:** aiohttp servers; async Python agents; any log aggregation backend.

---

## Comparison

| Option | Format | Request Correlation | External Dependency | Best For |
|--------|--------|--------------------|--------------------|----------|
| 1. structlog + contextvars | JSON | Auto via contextvars | structlog | FastAPI production |
| 2. stdlib JSON formatter | JSON | Manual bind | None | Zero-dep logging |
| 3. Span trace logging | JSON spans | Trace + span IDs | None | Pipeline timing |
| 4. Buffered error-only | JSON | Request ID | None | Noise reduction |
| 5. OpenTelemetry | OTLP traces | Full distributed | OTel SDK + collector | APM integration |
| 6. async aiohttp | JSON | Per-request bind | None | aiohttp servers |
