---
title: "Agent Doesn't Implement Wide Event Logging for Request Context"
description: "AI agents that emit narrow log lines (one message per operation) lose the causal thread connecting a user request to every tool call, LLM invocation, and downstream API hit. Wide event logging attaches the full request context as structured fields on a single event per logical unit of work, enabling Honeycomb-style arbitrarily-dimensional queries and exact root-cause isolation without log correlation gymnastics."
date: 2025-02-08
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-wide-event-logging-for-request-context
tags:
  - wide-events
  - structured-logging
  - observability
  - honeycomb
  - request-context
  - tracing
  - correlation
symptoms:
  - "Debugging a failed agent request requires joining 20 narrow log lines across multiple files"
  - "Tool call failures lack the user query, session ID, and model version that triggered them"
  - "Log search for 'all requests where tool X failed AND latency > 2s' is impossible"
  - "Each component logs independently with different field names for the same concept"
  - "Post-mortem requires manually reconstructing the causal chain from timestamps"
---

## Problem

Traditional narrow logging emits one line per operation: "Calling tool", "Tool returned", "LLM invoked". These lines share no common structure. Correlating them for a single request requires trace IDs threading through every component — and even then, you can only filter on dimensions that were logged. Wide event logging inverts this: accumulate every relevant fact about a request unit-of-work onto one structured event, emitted at the end. You can then query any combination of fields without pre-planning which correlations you'll need.

---

## Solution 1: WideEventBuilder — Accumulate-Then-Emit Pattern

```python
import time
import uuid
import logging
import json
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


logger = logging.getLogger(__name__)


@dataclass
class WideEvent:
    trace_id: str
    service: str
    name: str
    fields: Dict[str, Any] = field(default_factory=dict)
    start_time: float = field(default_factory=time.monotonic)
    wall_start: float = field(default_factory=time.time)
    duration_ms: Optional[float] = None
    error: Optional[str] = None
    level: str = "info"


class WideEventBuilder:
    """
    Accumulates structured fields throughout request processing,
    then emits a single wide event at the end of the logical unit.

    Usage:
        with WideEventBuilder.span("agent.request", service="agent") as evt:
            evt.add(user_id="u123", session_id="s456", model="gpt-4o")
            result = await call_llm(prompt)
            evt.add(prompt_tokens=result.usage.prompt_tokens,
                    completion_tokens=result.usage.completion_tokens,
                    model_latency_ms=result.latency_ms)
            for tc in result.tool_calls:
                evt.add_list("tool_calls", tc.name)
        # Single wide event emitted here with ALL fields
    """

    def __init__(self, name: str, service: str,
                 trace_id: Optional[str] = None):
        self._event = WideEvent(
            trace_id=trace_id or str(uuid.uuid4()),
            service=service,
            name=name,
        )

    def add(self, **kwargs):
        """Add arbitrary fields to the event."""
        self._event.fields.update(kwargs)
        return self

    def add_list(self, key: str, value: Any):
        """Append a value to a list field."""
        lst = self._event.fields.setdefault(key, [])
        lst.append(value)
        return self

    def set_error(self, exc: Exception):
        self._event.error = f"{type(exc).__name__}: {exc}"
        self._event.level = "error"
        return self

    def emit(self):
        self._event.duration_ms = (
            time.monotonic() - self._event.start_time
        ) * 1000
        payload = {
            "trace_id": self._event.trace_id,
            "service": self._event.service,
            "name": self._event.name,
            "duration_ms": round(self._event.duration_ms, 2),
            "wall_time": self._event.wall_start,
            "level": self._event.level,
            **self._event.fields,
        }
        if self._event.error:
            payload["error"] = self._event.error
        getattr(logger, self._event.level)(
            json.dumps(payload)
        )
        return payload

    @classmethod
    @contextmanager
    def span(cls, name: str, service: str = "agent",
             trace_id: Optional[str] = None):
        builder = cls(name, service, trace_id)
        try:
            yield builder
        except Exception as exc:
            builder.set_error(exc)
            raise
        finally:
            builder.emit()
```

---

## Solution 2: RequestContextStore — Async-Safe Context Propagation

Propagate the wide event builder through the async call stack without passing it explicitly to every function.

```python
import contextvars
import uuid
from typing import Optional

_current_event: contextvars.ContextVar[Optional["WideEventBuilder"]] = (
    contextvars.ContextVar("_current_event", default=None)
)


class RequestContextStore:
    """
    Async-safe store for the current request's wide event builder.
    Use add_to_current() anywhere in the call stack to annotate
    the enclosing request event without parameter threading.

    Usage:
        # At request entry point:
        async with RequestContextStore.request("agent.handle", service="agent") as evt:
            evt.add(user_id="u1", session_id="s1")
            await process_request()

        # Deep inside process_request, tool handlers, etc.:
        RequestContextStore.add_to_current(tool_name="web_search", cache_hit=True)
    """

    @classmethod
    def current(cls) -> Optional["WideEventBuilder"]:
        return _current_event.get()

    @classmethod
    def add_to_current(cls, **kwargs):
        evt = _current_event.get()
        if evt is not None:
            evt.add(**kwargs)

    @classmethod
    def append_to_current(cls, key: str, value):
        evt = _current_event.get()
        if evt is not None:
            evt.add_list(key, value)

    @classmethod
    def current_trace_id(cls) -> str:
        evt = _current_event.get()
        if evt is not None:
            return evt._event.trace_id
        return str(uuid.uuid4())

    @classmethod
    def request(cls, name: str, service: str = "agent",
                trace_id: Optional[str] = None):
        builder = WideEventBuilder(name, service, trace_id)
        token = _current_event.set(builder)

        class _Ctx:
            async def __aenter__(self_):
                return builder

            async def __aexit__(self_, exc_type, exc, tb):
                if exc is not None:
                    builder.set_error(exc)
                builder.emit()
                _current_event.reset(token)
                return False

        return _Ctx()
```

---

## Solution 3: AgentWideEventMiddleware — Automatic Request Wrapping

Wrap every agent invocation in a wide event span. Captures model, tokens, tools, latency, and errors without modifying inner logic.

```python
import time
from functools import wraps
from typing import Any, Callable, Optional


def wide_event_span(name: Optional[str] = None,
                    service: str = "agent",
                    capture_args: bool = False):
    """
    Decorator that wraps an async function in a wide event span.
    Captures return value metadata and any exception automatically.

    Usage:
        @wide_event_span("agent.tool_call", service="tools")
        async def call_web_search(query: str, max_results: int = 10):
            ...
    """
    def decorator(fn: Callable) -> Callable:
        span_name = name or f"{fn.__module__}.{fn.__qualname__}"

        @wraps(fn)
        async def wrapper(*args, **kwargs) -> Any:
            parent_trace = RequestContextStore.current_trace_id()
            with WideEventBuilder.span(span_name, service,
                                        trace_id=parent_trace) as evt:
                if capture_args:
                    evt.add(args=str(args[:3]), kwargs=str(list(kwargs.keys())))
                result = await fn(*args, **kwargs)
                if hasattr(result, "__len__"):
                    evt.add(result_size=len(result))
                return result
        return wrapper
    return decorator


class AgentWideEventMiddleware:
    """
    Middleware that wraps every agent handle() call with a wide event.
    Automatically captures: model, session_id, input tokens, output tokens,
    tool call names, total latency, and any errors.

    Usage:
        class MyAgent:
            middleware = AgentWideEventMiddleware(service="my-agent")

            async def handle(self, request: AgentRequest) -> AgentResponse:
                async with self.middleware.wrap(request) as evt:
                    response = await self._inner_handle(request)
                    evt.add(
                        output_tokens=response.usage.completion_tokens,
                        finish_reason=response.choices[0].finish_reason,
                    )
                    return response
    """

    def __init__(self, service: str = "agent"):
        self._service = service

    def wrap(self, request, trace_id: Optional[str] = None):
        builder = WideEventBuilder("agent.request", self._service, trace_id)

        # Extract common request fields
        for attr in ("session_id", "user_id", "model", "temperature"):
            val = getattr(request, attr, None)
            if val is not None:
                builder.add(**{attr: val})

        token = _current_event.set(builder)

        class _Ctx:
            async def __aenter__(self_):
                return builder

            async def __aexit__(self_, exc_type, exc, tb):
                if exc is not None:
                    builder.set_error(exc)
                builder.emit()
                _current_event.reset(token)
                return False

        return _Ctx()
```

---

## Solution 4: ToolCallRecorder — Per-Tool Span Nested in Request Event

Record each tool call as a nested sub-span inside the parent wide event, producing a roll-up of all tool activity on the request event.

```python
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ToolSpan:
    tool_name: str
    arguments: Dict[str, Any]
    result_summary: Optional[str] = None
    error: Optional[str] = None
    duration_ms: float = 0.0
    cache_hit: bool = False


class ToolCallRecorder:
    """
    Records tool call spans and rolls them up into the parent wide event.
    Call record() for each tool call; flush() adds the summary to the
    current request-context wide event.

    Usage:
        recorder = ToolCallRecorder()
        async with recorder.record("web_search", {"query": "LLM latency"}):
            result = await web_search(query="LLM latency")
        async with recorder.record("db_query", {"sql": "SELECT ..."}):
            rows = await db.execute(sql)
        recorder.flush()  # adds tool summary to current wide event
    """

    def __init__(self):
        self._spans: List[ToolSpan] = []

    def record(self, tool_name: str, arguments: Dict[str, Any]):
        span = ToolSpan(tool_name=tool_name, arguments=arguments)
        self._spans.append(span)
        recorder = self

        class _Ctx:
            async def __aenter__(self_):
                span._t0 = time.monotonic()
                return span

            async def __aexit__(self_, exc_type, exc, tb):
                span.duration_ms = (time.monotonic() - span._t0) * 1000
                if exc is not None:
                    span.error = f"{type(exc).__name__}: {exc}"
                return False

        return _Ctx()

    def flush(self):
        if not self._spans:
            return
        RequestContextStore.add_to_current(
            tool_call_count=len(self._spans),
            tool_names=[s.tool_name for s in self._spans],
            tool_errors=[s.tool_name for s in self._spans if s.error],
            tool_total_ms=round(sum(s.duration_ms for s in self._spans), 2),
            tool_max_ms=round(max(s.duration_ms for s in self._spans), 2),
            tool_cache_hits=sum(1 for s in self._spans if s.cache_hit),
        )
        self._spans.clear()
```

---

## Solution 5: WideEventSampler — Head-Based and Tail-Based Sampling

Emit all wide events in development; in production sample by trace rate or retain 100% of error/slow events.

```python
import random
import time
from typing import Callable, Optional


class WideEventSampler:
    """
    Pluggable sampler for wide events.
    Head-based: decide at span start (fast, low overhead).
    Tail-based: decide at span end based on outcome (captures all errors).

    Usage:
        sampler = WideEventSampler(
            base_rate=0.1,           # sample 10% of normal requests
            error_rate=1.0,          # sample 100% of errors
            slow_threshold_ms=2000,  # sample 100% of slow requests
        )
        # Plug into WideEventBuilder.emit():
        builder.set_sampler(sampler)
    """

    def __init__(self,
                 base_rate: float = 0.1,
                 error_rate: float = 1.0,
                 slow_threshold_ms: float = 2000.0):
        self._base_rate = base_rate
        self._error_rate = error_rate
        self._slow_threshold_ms = slow_threshold_ms
        self._emitted = 0
        self._dropped = 0

    def should_emit(self, event: "WideEvent") -> bool:
        if event.error:
            return random.random() < self._error_rate
        if (event.duration_ms or 0) > self._slow_threshold_ms:
            return True
        return random.random() < self._base_rate

    def record(self, emitted: bool):
        if emitted:
            self._emitted += 1
        else:
            self._dropped += 1

    @property
    def stats(self) -> dict:
        total = self._emitted + self._dropped
        return {
            "emitted": self._emitted,
            "dropped": self._dropped,
            "emit_rate": round(self._emitted / total, 3) if total else 0,
        }
```

---

## Solution 6: HoneycombWideEventExporter — Send to Honeycomb / OTLP

Export wide events to Honeycomb or any OTLP-compatible backend in addition to local structured logs.

```python
import json
import logging
import time
from typing import Any, Dict, List, Optional
from urllib.request import Request, urlopen
from urllib.error import URLError

logger = logging.getLogger(__name__)


class HoneycombWideEventExporter:
    """
    Exports wide events as Honeycomb Events (Honeycomb Events API)
    or as OTLP LogRecord batches.

    Usage:
        exporter = HoneycombWideEventExporter(
            api_key=os.environ["HONEYCOMB_API_KEY"],
            dataset="agent-prod",
        )
        # Attach to WideEventBuilder:
        WideEventBuilder.set_global_exporter(exporter)

        # Or call directly:
        exporter.send(event_payload)
    """

    HONEYCOMB_ENDPOINT = "https://api.honeycomb.io/1/events"

    def __init__(self, api_key: str, dataset: str,
                 batch_size: int = 50,
                 flush_interval_s: float = 5.0):
        self._api_key = api_key
        self._dataset = dataset
        self._batch_size = batch_size
        self._flush_interval = flush_interval_s
        self._queue: List[Dict[str, Any]] = []
        self._last_flush = time.monotonic()

    def send(self, payload: Dict[str, Any]):
        self._queue.append(payload)
        if (len(self._queue) >= self._batch_size or
                time.monotonic() - self._last_flush > self._flush_interval):
            self.flush()

    def flush(self):
        if not self._queue:
            return
        batch = self._queue[:]
        self._queue.clear()
        self._last_flush = time.monotonic()
        self._send_batch(batch)

    def _send_batch(self, events: List[Dict[str, Any]]):
        url = f"{self.HONEYCOMB_ENDPOINT}/{self._dataset}"
        body = json.dumps(events).encode()
        req = Request(
            url, data=body,
            headers={
                "X-Honeycomb-Team": self._api_key,
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urlopen(req, timeout=5) as resp:
                if resp.status not in (200, 202):
                    logger.warning("Honeycomb export HTTP %d", resp.status)
        except URLError as exc:
            logger.warning("Honeycomb export failed: %s", exc)

    def as_otlp_log_records(self, events: List[Dict[str, Any]]) -> dict:
        """Convert wide events to OTLP LogRecord format."""
        records = []
        for ev in events:
            records.append({
                "timeUnixNano": str(int(ev.get("wall_time", time.time()) * 1e9)),
                "severityText": ev.get("level", "INFO").upper(),
                "body": {"stringValue": json.dumps(ev)},
                "attributes": [
                    {"key": k, "value": {"stringValue": str(v)}}
                    for k, v in ev.items()
                    if k not in ("wall_time", "level")
                ],
            })
        return {
            "resourceLogs": [{
                "resource": {"attributes": [
                    {"key": "service.name",
                     "value": {"stringValue": "agent"}}
                ]},
                "scopeLogs": [{"logRecords": records}],
            }]
        }
```

---

## Comparison

| Approach | Granularity | Async-Safe | Sampling | Backend |
|---|---|---|---|---|
| **WideEventBuilder** | Per logical span | Yes (contextmanager) | No | stdout/logger |
| **RequestContextStore** | Per request (propagated) | Yes (contextvars) | No | stdout/logger |
| **AgentWideEventMiddleware** | Per agent.handle() | Yes | No | stdout/logger |
| **ToolCallRecorder** | Per tool call (rolled up) | Yes | No | stdout/logger |
| **WideEventSampler** | Head + tail sampling | Yes | Yes | stdout/logger |
| **HoneycombWideEventExporter** | Batched flush | Yes | Optional | Honeycomb/OTLP |

**Key insight**: emit one wide event per agent request, not one narrow line per operation. Attach every fact you might ever want to filter on — model, session, tool names, token counts, cache hits, error class — to that single event at emission time. This makes any future investigative query possible without code changes.
