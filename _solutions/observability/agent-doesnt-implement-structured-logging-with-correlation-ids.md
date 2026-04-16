---
layout: solution
title: "Agent Doesn't Implement Structured Logging with Correlation IDs"
category: observability
description: "Agent emits unstructured print statements with no trace IDs, making it impossible to correlate log lines across turns, tools, and sub-agents in production."
tags: [observability, logging, correlation-id, structured-logs, tracing]
---

# Agent Doesn't Implement Structured Logging with Correlation IDs

## Problem

Agents that log with `print()` or unstructured `logging.info()` produce log lines that can't be grouped by conversation, turn, or tool call. In production, when 100 concurrent sessions interleave in the same log stream, there is no way to reconstruct a single agent's execution path, attribute errors to a specific user, or measure per-turn latency. Without correlation IDs and structured fields, observability is impossible at scale.

## Solution Options

### Option 1: Basic Structured Logger with Correlation ID

```python
import anthropic
import json
import logging
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any

# Configure root logger to output JSON
class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        log = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)),
            "level": record.levelname,
            "msg": record.getMessage(),
        }
        # Merge structured fields from extra
        for key, val in record.__dict__.items():
            if key not in ("msg", "args", "levelname", "created", "filename",
                          "lineno", "funcName", "name", "levelno", "pathname",
                          "exc_info", "exc_text", "stack_info", "thread",
                          "threadName", "processName", "process", "msecs",
                          "relativeCreated", "module", "taskName"):
                log[key] = val
        return json.dumps(log)

handler = logging.StreamHandler()
handler.setFormatter(JSONFormatter())
logger = logging.getLogger("agent")
logger.addHandler(handler)
logger.setLevel(logging.INFO)
logger.propagate = False

client = anthropic.Anthropic()

@dataclass
class AgentContext:
    session_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    turn: int = 0
    user_id: str = ""

    def log(self, level: str, msg: str, **kwargs):
        extra = {"session_id": self.session_id, "turn": self.turn, "user_id": self.user_id, **kwargs}
        getattr(logger, level)(msg, extra=extra)

def run_structured_agent(user_message: str, user_id: str = "anon") -> str:
    ctx = AgentContext(user_id=user_id)
    ctx.log("info", "session_start", message_preview=user_message[:50])

    tools = [{
        "name": "search",
        "description": "Search for information",
        "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    }]

    messages = [{"role": "user", "content": user_message}]

    while True:
        ctx.turn += 1
        turn_start = time.time()
        ctx.log("info", "turn_start")

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=tools,
            messages=messages,
        )

        ctx.log("info", "llm_response",
                stop_reason=response.stop_reason,
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
                latency_ms=round((time.time() - turn_start) * 1000, 1))

        if response.stop_reason == "end_turn":
            result = next((b.text for b in response.content if hasattr(b, "text")), "")
            ctx.log("info", "session_end", response_len=len(result))
            return result

        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                tool_start = time.time()
                ctx.log("info", "tool_call", tool=block.name, input=block.input)
                result = {"results": [f"Result for {block.input.get('query', '')}"], "count": 1}
                ctx.log("info", "tool_result",
                        tool=block.name,
                        tool_use_id=block.id,
                        latency_ms=round((time.time() - tool_start) * 1000, 1))
                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(result)})

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

result = run_structured_agent("Search for information about async Python.", user_id="user_42")
print(f"\nFinal: {result[:80]}...")

# Expected Token Savings: No token impact; purely observability improvement
# Environment: Any production agent; enables log aggregation in Datadog, CloudWatch, ELK
```

### Option 2: Hierarchical Correlation IDs (Session → Turn → Tool)

```python
import anthropic
import json
import time
import uuid
from dataclasses import dataclass, field

client = anthropic.Anthropic()

@dataclass
class SpanContext:
    """Hierarchical IDs for correlating logs at any level."""
    session_id: str
    turn_id: str = ""
    tool_span_id: str = ""

    @property
    def as_dict(self) -> dict:
        d = {"session_id": self.session_id}
        if self.turn_id:
            d["turn_id"] = self.turn_id
        if self.tool_span_id:
            d["tool_span_id"] = self.tool_span_id
        return d

    def new_turn(self) -> "SpanContext":
        return SpanContext(session_id=self.session_id, turn_id=str(uuid.uuid4())[:8])

    def new_tool_span(self) -> "SpanContext":
        return SpanContext(session_id=self.session_id, turn_id=self.turn_id, tool_span_id=str(uuid.uuid4())[:8])

_log_records: list[dict] = []

def log(level: str, event: str, ctx: SpanContext, **fields):
    record = {
        "ts": time.time(),
        "level": level,
        "event": event,
        **ctx.as_dict,
        **fields,
    }
    _log_records.append(record)
    print(json.dumps(record))

def run_hierarchical_agent(user_message: str) -> str:
    session_ctx = SpanContext(session_id=str(uuid.uuid4())[:8])
    log("INFO", "session.start", session_ctx, user_preview=user_message[:40])

    tools = [{
        "name": "fetch_data",
        "description": "Fetch data from external source",
        "input_schema": {"type": "object", "properties": {"resource": {"type": "string"}}, "required": ["resource"]},
    }]

    messages = [{"role": "user", "content": user_message}]
    turn_num = 0

    while True:
        turn_num += 1
        turn_ctx = session_ctx.new_turn()
        t0 = time.time()
        log("INFO", "turn.start", turn_ctx, turn_num=turn_num)

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=tools,
            messages=messages,
        )

        log("INFO", "turn.llm_done", turn_ctx,
            turn_num=turn_num,
            stop_reason=response.stop_reason,
            in_tok=response.usage.input_tokens,
            out_tok=response.usage.output_tokens,
            ms=round((time.time() - t0) * 1000))

        if response.stop_reason == "end_turn":
            text = next((b.text for b in response.content if hasattr(b, "text")), "")
            log("INFO", "session.complete", session_ctx, turns=turn_num, response_chars=len(text))
            return text

        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                tool_ctx = turn_ctx.new_tool_span()
                t_tool = time.time()
                log("INFO", "tool.start", tool_ctx, tool=block.name, input=block.input)
                result = {"data": f"fetched:{block.input.get('resource', '')}", "status": "ok"}
                log("INFO", "tool.done", tool_ctx,
                    tool=block.name,
                    ms=round((time.time() - t_tool) * 1000))
                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(result)})

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

result = run_hierarchical_agent("Fetch the user profile and then fetch their preferences.")

# Demonstrate log correlation: all tool logs share the same session_id+turn_id
print(f"\n--- Unique session IDs in log ---")
sessions = set(r.get("session_id") for r in _log_records)
print(f"Sessions: {sessions}")
print(f"Total log events: {len(_log_records)}")

# Expected Token Savings: None — pure observability infrastructure
# Environment: Multi-step agents with nested tool calls; enables drill-down from session → turn → tool
```

### Option 3: Async Structured Logger with Log Shipping

```python
import anthropic
import asyncio
import json
import time
import uuid
from collections import deque
from dataclasses import dataclass, field

async_client = anthropic.AsyncAnthropic()

@dataclass
class LogShipper:
    """Buffers log records and flushes in batch to a log sink."""
    buffer: deque = field(default_factory=lambda: deque(maxlen=1000))
    flush_interval: float = 5.0
    _task: asyncio.Task = field(default=None, init=False)

    def emit(self, record: dict):
        self.buffer.append({**record, "ts": time.time()})

    async def flush(self) -> list[dict]:
        """Flush buffered records (simulate shipping to ELK/Datadog)."""
        batch = []
        while self.buffer:
            batch.append(self.buffer.popleft())
        if batch:
            print(f"[SHIPPER] Flushing {len(batch)} log records to sink")
        return batch

    async def start_background_flush(self):
        async def _loop():
            while True:
                await asyncio.sleep(self.flush_interval)
                await self.flush()
        self._task = asyncio.create_task(_loop())

    def stop(self):
        if self._task:
            self._task.cancel()

shipper = LogShipper()

class StructuredLogger:
    def __init__(self, shipper: LogShipper, context: dict | None = None):
        self._shipper = shipper
        self._context = context or {}

    def with_context(self, **kwargs) -> "StructuredLogger":
        return StructuredLogger(self._shipper, {**self._context, **kwargs})

    def _emit(self, level: str, event: str, **fields):
        self._shipper.emit({"level": level, "event": event, **self._context, **fields})

    def info(self, event: str, **fields): self._emit("INFO", event, **fields)
    def warn(self, event: str, **fields): self._emit("WARN", event, **fields)
    def error(self, event: str, **fields): self._emit("ERROR", event, **fields)

async def async_structured_agent(user_message: str, user_id: str = "") -> str:
    session_id = str(uuid.uuid4())[:8]
    log = StructuredLogger(shipper).with_context(session_id=session_id, user_id=user_id)
    log.info("session.start", query_len=len(user_message))

    tools = [{
        "name": "query_db",
        "description": "Query the database",
        "input_schema": {"type": "object", "properties": {"sql": {"type": "string"}}, "required": ["sql"]},
    }]

    messages = [{"role": "user", "content": user_message}]
    turn = 0

    while True:
        turn += 1
        turn_id = str(uuid.uuid4())[:8]
        turn_log = log.with_context(turn_id=turn_id, turn_num=turn)
        t0 = time.time()
        turn_log.info("turn.start")

        try:
            response = await async_client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=512,
                tools=tools,
                messages=messages,
            )
        except Exception as e:
            turn_log.error("turn.llm_error", error=str(e), error_type=type(e).__name__)
            raise

        turn_log.info("turn.llm_done",
                      stop_reason=response.stop_reason,
                      in_tokens=response.usage.input_tokens,
                      out_tokens=response.usage.output_tokens,
                      latency_ms=round((time.time() - t0) * 1000))

        if response.stop_reason == "end_turn":
            text = next((b.text for b in response.content if hasattr(b, "text")), "")
            log.info("session.complete", total_turns=turn, output_len=len(text))
            await shipper.flush()
            return text

        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                span_id = str(uuid.uuid4())[:8]
                tool_log = turn_log.with_context(tool_span_id=span_id)
                t_tool = time.time()
                tool_log.info("tool.invoke", tool=block.name, args=block.input)
                result = {"rows": [{"id": 1, "name": "Alice"}], "count": 1}
                tool_log.info("tool.result", tool=block.name, result_rows=1,
                              latency_ms=round((time.time() - t_tool) * 1000))
                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(result)})

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

async def main():
    result = await async_structured_agent("Query the users table for active accounts.", user_id="u99")
    print(f"\nResult: {result[:80]}...")

asyncio.run(main())

# Expected Token Savings: None — log shipping is async and doesn't affect API calls
# Environment: High-throughput async agents with centralized log aggregation pipelines
```

### Option 4: OpenTelemetry-Compatible Structured Logging

```python
import anthropic
import json
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Generator

client = anthropic.Anthropic()

@dataclass
class OTelSpan:
    """Simplified OpenTelemetry-compatible span."""
    name: str
    trace_id: str
    span_id: str
    parent_span_id: str = ""
    start_time: float = field(default_factory=time.time)
    end_time: float = 0.0
    attributes: dict = field(default_factory=dict)
    events: list[dict] = field(default_factory=list)
    status: str = "OK"  # OK, ERROR

    def set_attribute(self, key: str, value):
        self.attributes[key] = value

    def add_event(self, name: str, **attrs):
        self.events.append({"name": name, "ts": time.time(), **attrs})

    def set_error(self, error: str):
        self.status = "ERROR"
        self.attributes["error.message"] = error

    def end(self):
        self.end_time = time.time()
        duration_ms = round((self.end_time - self.start_time) * 1000, 1)
        record = {
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "name": self.name,
            "duration_ms": duration_ms,
            "status": self.status,
            **self.attributes,
        }
        if self.events:
            record["events"] = self.events
        print(json.dumps(record))

class Tracer:
    def __init__(self, service_name: str):
        self.service = service_name
        self._current_trace: str = ""
        self._span_stack: list[OTelSpan] = []

    @contextmanager
    def start_span(self, name: str, **attrs) -> Generator[OTelSpan, None, None]:
        trace_id = self._current_trace or str(uuid.uuid4()).replace("-", "")
        self._current_trace = trace_id
        parent_id = self._span_stack[-1].span_id if self._span_stack else ""
        span = OTelSpan(
            name=f"{self.service}.{name}",
            trace_id=trace_id,
            span_id=str(uuid.uuid4())[:16],
            parent_span_id=parent_id,
        )
        for k, v in attrs.items():
            span.set_attribute(k, v)
        self._span_stack.append(span)
        try:
            yield span
        except Exception as e:
            span.set_error(str(e))
            raise
        finally:
            span.end()
            self._span_stack.pop()

tracer = Tracer("claude-agent")

def run_otel_agent(user_message: str, user_id: str = "") -> str:
    with tracer.start_span("session", user_id=user_id) as session_span:
        session_span.set_attribute("query.preview", user_message[:50])

        tools = [{
            "name": "lookup",
            "description": "Look up a value",
            "input_schema": {"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"]},
        }]
        messages = [{"role": "user", "content": user_message}]
        turn = 0

        while True:
            turn += 1
            with tracer.start_span("turn", turn_num=turn) as turn_span:
                response = client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=512,
                    tools=tools,
                    messages=messages,
                )
                turn_span.set_attribute("llm.model", "claude-haiku-4-5-20251001")
                turn_span.set_attribute("llm.input_tokens", response.usage.input_tokens)
                turn_span.set_attribute("llm.output_tokens", response.usage.output_tokens)
                turn_span.set_attribute("llm.stop_reason", response.stop_reason)

                if response.stop_reason == "end_turn":
                    text = next((b.text for b in response.content if hasattr(b, "text")), "")
                    session_span.set_attribute("session.total_turns", turn)
                    session_span.set_attribute("session.output_len", len(text))
                    return text

                tool_results = []
                for block in response.content:
                    if block.type == "tool_use":
                        with tracer.start_span("tool_call", tool=block.name, tool_id=block.id) as tool_span:
                            result = {"value": f"result_for_{block.input.get('key', '')}"}
                            tool_span.set_attribute("tool.result_size", len(json.dumps(result)))
                            tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(result)})

            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": tool_results})

result = run_otel_agent("Look up config for 'database' and 'cache'.", user_id="u7")
print(f"\nResult: {result[:80]}...")

# Expected Token Savings: None — observability infrastructure only
# Environment: Teams using Jaeger, Zipkin, or OTLP-compatible backends for distributed tracing
```

### Option 5: Correlation ID Propagation Across Sub-Agents

```python
import anthropic
import json
import time
import uuid
from dataclasses import dataclass, field

client = anthropic.Anthropic()

@dataclass
class PropagatedContext:
    """Correlation context that flows from orchestrator to sub-agents."""
    trace_id: str
    parent_agent: str = "orchestrator"
    agent_name: str = "orchestrator"
    depth: int = 0

    def child(self, agent_name: str) -> "PropagatedContext":
        return PropagatedContext(
            trace_id=self.trace_id,
            parent_agent=self.agent_name,
            agent_name=agent_name,
            depth=self.depth + 1,
        )

_log: list[dict] = []

def emit(ctx: PropagatedContext, event: str, **fields):
    record = {
        "ts": time.time(),
        "trace_id": ctx.trace_id,
        "agent": ctx.agent_name,
        "parent_agent": ctx.parent_agent,
        "depth": ctx.depth,
        "event": event,
        **fields,
    }
    _log.append(record)
    indent = "  " * ctx.depth
    print(f"{indent}[{ctx.agent_name}|{ctx.trace_id[:6]}] {event} {json.dumps(fields)}")

def sub_agent_call(ctx: PropagatedContext, role: str, task: str) -> str:
    """Simulate a sub-agent that inherits the correlation context."""
    child_ctx = ctx.child(agent_name=f"{role}_agent")
    emit(child_ctx, "sub_agent.start", task_preview=task[:40])

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=f"You are a specialized {role} agent.",
        messages=[{"role": "user", "content": task}],
    )

    result = response.content[0].text
    emit(child_ctx, "sub_agent.done",
         in_tokens=response.usage.input_tokens,
         out_tokens=response.usage.output_tokens,
         result_len=len(result))
    return result

def orchestrator(user_request: str) -> dict:
    ctx = PropagatedContext(trace_id=str(uuid.uuid4()).replace("-", "")[:12])
    emit(ctx, "orchestrator.start", request=user_request[:50])

    # Orchestrator dispatches to specialized sub-agents
    research = sub_agent_call(ctx, "research", f"Research: {user_request}")
    analysis = sub_agent_call(ctx, "analysis", f"Analyze this research: {research[:200]}")
    summary = sub_agent_call(ctx, "writer", f"Write a brief summary: {analysis[:200]}")

    emit(ctx, "orchestrator.done", sub_agents_called=3)

    # All log records share the same trace_id
    trace_records = [r for r in _log if r["trace_id"] == ctx.trace_id]
    print(f"\n[CORRELATION] trace_id={ctx.trace_id} captured {len(trace_records)} log events across {3+1} agents")
    return {"summary": summary, "trace_id": ctx.trace_id}

result = orchestrator("Explain the benefits of event-driven architecture")
print(f"\nFinal summary: {result['summary'][:100]}...")

# Expected Token Savings: None — correlation propagation is metadata only
# Environment: Multi-agent orchestration systems; enables end-to-end trace reconstruction
```

### Option 6: Sampling-Based Structured Logging with Rate Limits

```python
import anthropic
import hashlib
import json
import random
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum

client = anthropic.Anthropic()

class SampleRate(Enum):
    ALWAYS = 1.0
    HIGH = 0.5
    LOW = 0.1
    NEVER = 0.0

@dataclass
class SampledLogger:
    """Structured logger with per-event sampling to control log volume."""
    session_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    _sample_config: dict[str, float] = field(default_factory=dict)
    _emitted: int = 0
    _sampled_out: int = 0

    def configure_sampling(self, event: str, rate: float):
        self._sample_config[event] = rate

    def _should_emit(self, event: str) -> bool:
        rate = self._sample_config.get(event, 1.0)
        if rate >= 1.0:
            return True
        if rate <= 0.0:
            return False
        return random.random() < rate

    def log(self, level: str, event: str, **fields):
        if not self._should_emit(event):
            self._sampled_out += 1
            return
        record = {
            "ts": round(time.time(), 3),
            "level": level,
            "event": event,
            "session_id": self.session_id,
            **fields,
        }
        self._emitted += 1
        print(json.dumps(record))

    @property
    def stats(self) -> dict:
        total = self._emitted + self._sampled_out
        return {"emitted": self._emitted, "sampled_out": self._sampled_out, "emit_rate_pct": round(self._emitted / max(total, 1) * 100)}

def run_sampled_logging_agent(user_message: str) -> str:
    log = SampledLogger()

    # Configure per-event sampling rates
    log.configure_sampling("turn.start", SampleRate.ALWAYS.value)
    log.configure_sampling("turn.llm_done", SampleRate.ALWAYS.value)
    log.configure_sampling("tool.invoke", SampleRate.HIGH.value)    # Sample 50% of tool calls
    log.configure_sampling("tool.result", SampleRate.LOW.value)     # Sample 10% of tool results
    log.configure_sampling("chunk.received", SampleRate.NEVER.value)  # Never log streaming chunks

    log.log("INFO", "turn.start", query_len=len(user_message))

    tools = [{
        "name": "process",
        "description": "Process data",
        "input_schema": {"type": "object", "properties": {"data": {"type": "string"}}, "required": ["data"]},
    }]

    messages = [{"role": "user", "content": user_message}]
    turn = 0

    while True:
        turn += 1
        t0 = time.time()
        log.log("INFO", "turn.start", turn=turn)

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=tools,
            messages=messages,
        )

        log.log("INFO", "turn.llm_done",
                turn=turn,
                stop_reason=response.stop_reason,
                in_tok=response.usage.input_tokens,
                out_tok=response.usage.output_tokens,
                ms=round((time.time() - t0) * 1000))

        if response.stop_reason == "end_turn":
            text = next((b.text for b in response.content if hasattr(b, "text")), "")
            log.log("INFO", "session.end", turns=turn)
            print(f"\n[LOG STATS] {json.dumps(log.stats)}")
            return text

        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                log.log("INFO", "tool.invoke", tool=block.name, args=block.input)
                result = {"processed": block.input.get("data", ""), "status": "done"}
                log.log("DEBUG", "tool.result", tool=block.name, result=result)
                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(result)})

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

result = run_sampled_logging_agent("Process these 5 items: alpha, beta, gamma, delta, epsilon.")
print(f"\nResult: {result[:80]}...")

# Expected Token Savings: None — sampling only affects log volume, not API calls
# Environment: High-throughput agents where full logging is too expensive; tune sampling per event type
```

## Comparison

| Option | Correlation Scope | Format | Sampling | OTel-Compatible | Best For |
|--------|-----------------|--------|---------|-----------------|---------|
| 1. Basic Structured | Session+Turn+Tool | JSON | No | No | Quick production upgrade from print() |
| 2. Hierarchical IDs | Session→Turn→Tool | JSON | No | No | Deep nested tool call tracing |
| 3. Async Shipper | Session+Turn+Tool | JSON | No | No | High-throughput with batched log shipping |
| 4. OTel Spans | Trace+Span+Parent | JSON | No | Yes | Teams using Jaeger/Zipkin/OTLP |
| 5. Cross-Agent Propagation | Multi-agent trace | JSON | No | Partial | Orchestrator + sub-agent systems |
| 6. Sampled Logging | Session | JSON | Yes (per-event) | No | High-volume agents with log cost constraints |
