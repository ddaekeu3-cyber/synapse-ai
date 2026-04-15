---
layout: solution
title: "Agent doesn't implement observability tracing"
category: general
description: "Agent has no structured tracing or metrics. When it misbehaves, operators have no visibility into tool call latency, token usage per step, or where in the reasoning chain the failure occurred."
tags: [observability, tracing, opentelemetry, metrics, logging, prometheus]
---

## Symptom

When your agent fails or behaves unexpectedly you have nothing to inspect: no span tree, no per-step latency, no token budget breakdown. You can only see the final output (or error), not the sequence of decisions that led there. Debugging becomes guesswork.

## Root Cause

Observability is treated as optional. Calls to `client.messages.create()` are made directly with no wrapper that captures start time, end time, input tokens, output tokens, tool names, or correlation IDs. There is no way to answer "which tool call took 8 seconds?" or "which step consumed 60 % of the token budget?".

## Fix

Wrap every API call in a span-like structure that records timing, token usage, tool names, and any errors. Choose the depth that fits your deployment: a simple structured logger is enough for a single-process agent; OpenTelemetry or Prometheus is appropriate for production services.

---

### Option 1 — Structured JSON span logger (zero dependencies)

```python
import anthropic
import json
import time
import uuid
import logging
from typing import Any

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("agent.trace")

client = anthropic.Anthropic(api_key="sk-live-...")


def _emit_span(span: dict) -> None:
    """Write a single span as a JSON line to the logger."""
    logger.info(json.dumps(span))


def traced_create(
    trace_id: str,
    step: int,
    **kwargs: Any,
) -> anthropic.types.Message:
    span_id = str(uuid.uuid4())[:8]
    tool_names = [t["name"] for t in kwargs.get("tools", [])]

    span: dict = {
        "trace_id": trace_id,
        "span_id": span_id,
        "step": step,
        "model": kwargs.get("model"),
        "tool_names": tool_names,
        "start_ts": time.time(),
    }

    try:
        response = client.messages.create(**kwargs)

        span["end_ts"] = time.time()
        span["latency_ms"] = round((span["end_ts"] - span["start_ts"]) * 1000)
        span["input_tokens"] = response.usage.input_tokens
        span["output_tokens"] = response.usage.output_tokens
        span["stop_reason"] = response.stop_reason
        span["status"] = "ok"

        _emit_span(span)
        return response

    except Exception as exc:
        span["end_ts"] = time.time()
        span["latency_ms"] = round((span["end_ts"] - span["start_ts"]) * 1000)
        span["status"] = "error"
        span["error"] = str(exc)
        _emit_span(span)
        raise


def run_agent(user_message: str) -> str:
    trace_id = str(uuid.uuid4())[:12]
    messages = [{"role": "user", "content": user_message}]
    step = 0

    while True:
        step += 1
        response = traced_create(
            trace_id=trace_id,
            step=step,
            model="claude-sonnet-4-6",
            max_tokens=1024,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            return response.content[0].text

        messages.append({"role": "assistant", "content": response.content})
        # handle tool calls …
        break

    return ""
```

**Expected Token Savings:** None — pure observability overhead with no model calls added.
**Environment:** Any Python environment; output streams to stdout or any log sink (CloudWatch, Datadog, Loki).

---

### Option 2 — OpenTelemetry-compatible trace context

```python
import anthropic
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class Span:
    trace_id: str
    span_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    parent_id: Optional[str] = None
    name: str = ""
    start_ns: int = field(default_factory=time.time_ns)
    end_ns: Optional[int] = None
    attributes: dict = field(default_factory=dict)
    status: str = "unset"  # ok | error

    def end(self) -> "Span":
        self.end_ns = time.time_ns()
        return self

    @property
    def duration_ms(self) -> float:
        if self.end_ns is None:
            return 0.0
        return (self.end_ns - self.start_ns) / 1_000_000

    def to_dict(self) -> dict:
        return {
            "traceId": self.trace_id,
            "spanId": self.span_id,
            "parentSpanId": self.parent_id,
            "name": self.name,
            "durationMs": round(self.duration_ms, 2),
            "status": self.status,
            "attributes": self.attributes,
        }


class OTelCompatTracer:
    def __init__(self) -> None:
        self.spans: list[Span] = []

    def start_span(self, name: str, parent: Optional[Span] = None) -> Span:
        span = Span(
            trace_id=parent.trace_id if parent else uuid.uuid4().hex,
            parent_id=parent.span_id if parent else None,
            name=name,
        )
        return span

    def finish(self, span: Span) -> None:
        span.end()
        self.spans.append(span)

    def export(self) -> list[dict]:
        return [s.to_dict() for s in self.spans]


tracer = OTelCompatTracer()
client = anthropic.Anthropic(api_key="sk-live-...")


def traced_agent_turn(
    messages: list[dict],
    parent_span: Optional[Span] = None,
) -> anthropic.types.Message:
    span = tracer.start_span("anthropic.messages.create", parent=parent_span)
    span.attributes["model"] = "claude-sonnet-4-6"
    span.attributes["message_count"] = len(messages)

    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            messages=messages,
        )
        span.attributes["input_tokens"] = response.usage.input_tokens
        span.attributes["output_tokens"] = response.usage.output_tokens
        span.attributes["stop_reason"] = response.stop_reason
        span.status = "ok"
        return response

    except Exception as exc:
        span.attributes["error"] = str(exc)
        span.status = "error"
        raise

    finally:
        tracer.finish(span)


def run_agent(user_message: str) -> None:
    root = tracer.start_span("agent.run")
    root.attributes["query_preview"] = user_message[:80]

    messages = [{"role": "user", "content": user_message}]
    response = traced_agent_turn(messages, parent_span=root)

    root.attributes["total_turns"] = 1
    root.status = "ok"
    tracer.finish(root)

    import json
    print(json.dumps(tracer.export(), indent=2))
```

**Expected Token Savings:** None — instrumentation layer only.
**Environment:** Drop-in replacement for OpenTelemetry SDK; compatible with OTLP exporters via a thin adapter.

---

### Option 3 — Per-request token usage tracker with budget enforcement

```python
import anthropic
from dataclasses import dataclass, field
from typing import Any


@dataclass
class TokenBudget:
    max_input: int = 50_000
    max_output: int = 10_000
    used_input: int = 0
    used_output: int = 0
    turns: int = 0

    def record(self, usage: anthropic.types.Usage) -> None:
        self.used_input += usage.input_tokens
        self.used_output += usage.output_tokens
        self.turns += 1

    def check(self) -> None:
        if self.used_input > self.max_input:
            raise RuntimeError(
                f"Input token budget exceeded: {self.used_input} / {self.max_input}"
            )
        if self.used_output > self.max_output:
            raise RuntimeError(
                f"Output token budget exceeded: {self.used_output} / {self.max_output}"
            )

    def summary(self) -> dict:
        return {
            "turns": self.turns,
            "input_used": self.used_input,
            "input_budget": self.max_input,
            "output_used": self.used_output,
            "output_budget": self.max_output,
            "input_pct": round(self.used_input / self.max_input * 100, 1),
            "output_pct": round(self.used_output / self.max_output * 100, 1),
        }


client = anthropic.Anthropic(api_key="sk-live-...")


def run_agent_with_budget(user_message: str) -> str:
    budget = TokenBudget(max_input=50_000, max_output=8_000)
    messages: list[dict] = [{"role": "user", "content": user_message}]

    for turn in range(10):
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            messages=messages,
        )

        budget.record(response.usage)
        budget.check()  # raises if over budget

        if response.stop_reason == "end_turn":
            print("Budget summary:", budget.summary())
            return response.content[0].text

        messages.append({"role": "assistant", "content": response.content})

    raise RuntimeError(f"Agent exceeded turn limit. Budget: {budget.summary()}")
```

**Expected Token Savings:** Prevents runaway agents from burning unbounded tokens — operational guard, not a cost-cutting measure.
**Environment:** Suitable for any sync agent loop; swap `check()` for a soft warning if hard stops are undesirable.

---

### Option 4 — LLM reasoning step logger with tool-call attribution

```python
import anthropic
import json
import time
from typing import Any

client = anthropic.Anthropic(api_key="sk-live-...")

TOOLS = [
    {
        "name": "search_docs",
        "description": "Search the documentation corpus.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    }
]


def fake_search(query: str) -> str:
    return f"Results for '{query}': [doc1, doc2, doc3]"


def log_step(step_type: str, data: dict) -> None:
    record = {"ts": round(time.time(), 3), "step": step_type, **data}
    print(json.dumps(record))


def run_agent(user_message: str) -> str:
    messages: list[dict] = [{"role": "user", "content": user_message}]
    step = 0

    while True:
        step += 1
        t0 = time.time()
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            tools=TOOLS,
            messages=messages,
        )
        latency_ms = round((time.time() - t0) * 1000)

        log_step(
            "llm_turn",
            {
                "step": step,
                "latency_ms": latency_ms,
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
                "stop_reason": response.stop_reason,
            },
        )

        if response.stop_reason == "end_turn":
            return response.content[0].text

        if response.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": response.content})
            tool_results = []

            for block in response.content:
                if block.type != "tool_use":
                    continue
                t_tool = time.time()
                result = fake_search(**block.input)
                tool_latency = round((time.time() - t_tool) * 1000)

                log_step(
                    "tool_call",
                    {
                        "tool": block.name,
                        "input": block.input,
                        "latency_ms": tool_latency,
                        "result_len": len(result),
                    },
                )

                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    }
                )

            messages.append({"role": "user", "content": tool_results})

    return ""
```

**Expected Token Savings:** None — instrumentation only.
**Environment:** Any multi-turn agent; log output is newline-delimited JSON consumable by jq, Splunk, or Elastic.

---

### Option 5 — Async trace collector with background flush

```python
import anthropic
import asyncio
import json
import time
import uuid
from collections import deque
from typing import Any


class AsyncTraceCollector:
    """Collects spans in a deque; flushes them asynchronously to avoid blocking hot path."""

    def __init__(self, flush_interval: float = 5.0, max_buffer: int = 1000) -> None:
        self._buffer: deque[dict] = deque(maxlen=max_buffer)
        self._flush_interval = flush_interval
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._flush_loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await self._flush()

    def record(self, span: dict) -> None:
        """Non-blocking: called from hot path."""
        self._buffer.append(span)

    async def _flush(self) -> None:
        async with self._lock:
            if not self._buffer:
                return
            batch = list(self._buffer)
            self._buffer.clear()
            # Replace with HTTP POST to your trace backend
            print(f"[TRACE FLUSH] {len(batch)} spans")
            for span in batch:
                print(json.dumps(span))

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(self._flush_interval)
            await self._flush()


collector = AsyncTraceCollector(flush_interval=2.0)
async_client = anthropic.AsyncAnthropic(api_key="sk-live-...")


async def traced_create_async(
    trace_id: str,
    step: int,
    **kwargs: Any,
) -> anthropic.types.Message:
    span: dict = {
        "trace_id": trace_id,
        "span_id": uuid.uuid4().hex[:8],
        "step": step,
        "model": kwargs.get("model"),
        "start_ts": time.time(),
    }

    try:
        response = await async_client.messages.create(**kwargs)
        span["latency_ms"] = round((time.time() - span["start_ts"]) * 1000)
        span["input_tokens"] = response.usage.input_tokens
        span["output_tokens"] = response.usage.output_tokens
        span["stop_reason"] = response.stop_reason
        span["status"] = "ok"
        return response

    except Exception as exc:
        span["latency_ms"] = round((time.time() - span["start_ts"]) * 1000)
        span["status"] = "error"
        span["error"] = str(exc)
        raise

    finally:
        collector.record(span)


async def run_agent_async(user_message: str) -> str:
    await collector.start()
    trace_id = uuid.uuid4().hex[:12]
    messages = [{"role": "user", "content": user_message}]

    response = await traced_create_async(
        trace_id=trace_id,
        step=1,
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=messages,
    )

    await collector.stop()
    return response.content[0].text


if __name__ == "__main__":
    asyncio.run(run_agent_async("Summarize the history of neural networks."))
```

**Expected Token Savings:** None — the background flush decouples I/O latency from the hot path without adding tokens.
**Environment:** `asyncio`-based agents; replace the `print` sink with an OTLP HTTP exporter or Datadog trace ingest.

---

### Option 6 — Prometheus metrics exporter for dashboard-ready visibility

```python
import anthropic
import time
from typing import Any

# Use prometheus_client if available; fall back to a lightweight stub.
try:
    from prometheus_client import Counter, Histogram, Gauge, start_http_server
    PROM_AVAILABLE = True
except ImportError:
    PROM_AVAILABLE = False


class _Stub:
    """Minimal stub so the rest of the code runs without prometheus_client."""
    def labels(self, **_: Any) -> "_Stub":
        return self
    def inc(self, *_: Any) -> None: ...
    def observe(self, *_: Any) -> None: ...
    def set(self, *_: Any) -> None: ...


def _make(kind: str, name: str, help_: str, labels: list[str], buckets: list | None = None):
    if not PROM_AVAILABLE:
        return _Stub()
    if kind == "counter":
        return Counter(name, help_, labels)
    if kind == "histogram":
        kwargs = {"buckets": buckets} if buckets else {}
        return Histogram(name, help_, labels, **kwargs)
    return Gauge(name, help_, labels)


TURN_LATENCY = _make(
    "histogram", "agent_turn_latency_seconds", "LLM turn latency", ["model"],
    buckets=[0.1, 0.5, 1, 2, 5, 10, 30],
)
INPUT_TOKENS = _make("counter", "agent_input_tokens_total", "Input tokens", ["model"])
OUTPUT_TOKENS = _make("counter", "agent_output_tokens_total", "Output tokens", ["model"])
TOOL_CALLS = _make("counter", "agent_tool_calls_total", "Tool invocations", ["tool_name"])
ERRORS = _make("counter", "agent_errors_total", "API errors", ["error_type"])

client = anthropic.Anthropic(api_key="sk-live-...")


def instrumented_create(**kwargs: Any) -> anthropic.types.Message:
    model = kwargs.get("model", "unknown")
    t0 = time.time()

    try:
        response = client.messages.create(**kwargs)

        TURN_LATENCY.labels(model=model).observe(time.time() - t0)
        INPUT_TOKENS.labels(model=model).inc(response.usage.input_tokens)
        OUTPUT_TOKENS.labels(model=model).inc(response.usage.output_tokens)

        for block in response.content:
            if hasattr(block, "type") and block.type == "tool_use":
                TOOL_CALLS.labels(tool_name=block.name).inc()

        return response

    except anthropic.RateLimitError:
        ERRORS.labels(error_type="rate_limit").inc()
        raise
    except anthropic.APIStatusError as exc:
        ERRORS.labels(error_type=f"http_{exc.status_code}").inc()
        raise


def run_agent(user_message: str) -> str:
    if PROM_AVAILABLE:
        start_http_server(8000)   # scrape at http://localhost:8000/metrics

    messages = [{"role": "user", "content": user_message}]
    response = instrumented_create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=messages,
    )
    return response.content[0].text


# Comparison table
# | Option | Dependency | Granularity | Backend |
# |--------|-----------|-------------|---------|
# | 1 JSON logger | stdlib | span per turn | stdout / log sink |
# | 2 OTel-compatible | dataclasses only | span tree | OTLP adapter |
# | 3 Token budget | stdlib | cumulative per run | inline check |
# | 4 Step + tool logger | stdlib | per turn + per tool | stdout / Elastic |
# | 5 Async flush | asyncio | per turn | HTTP trace backend |
# | 6 Prometheus | prometheus_client | time-series counters/histograms | Grafana / Alertmanager |
```

**Expected Token Savings:** None — metrics are side-channel data; they do not affect the model context.
**Environment:** Production Python services; the `/metrics` endpoint is compatible with any Prometheus scrape config.
