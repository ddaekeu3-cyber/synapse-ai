---
layout: solution
title: "Agent Doesn't Log Tool Calls for Debugging"
category: general
description: "Agent makes tool calls with no logging — inputs, outputs, latency, and errors are invisible. When a production run fails, there is no trace of which tool was called, what arguments were passed, or what it returned. Debugging requires reproducing the failure from scratch."
tags: [general, observability, logging, debugging, monitoring, tracing, production]
---

## Symptom

A production agent run fails. The error log shows "Agent returned unexpected result" with no context. Engineers spend hours trying to reproduce the failure because they have no record of: which tools were called, in what order, with what arguments, what each tool returned, or how long each call took. The next run succeeds and the failure is never explained.

Mean time to diagnose agent failures without logging: **4–8 hours**
With structured tool call logging: **5–15 minutes**

## Root Cause

The agent loop calls tools directly without any instrumentation. There is no structured log of inputs, outputs, timing, or errors at the tool boundary. `print()` statements exist in some places but are inconsistent and not machine-readable. In production, stdout is discarded.

## Fix

---

### Option 1 — Structured Tool Call Interceptor

Wrap every tool call in a logging interceptor that records inputs, outputs, latency, and errors in structured JSON. Zero changes to tool implementations required.

```python
import json
import logging
import time
import traceback
import uuid
from typing import Any, Callable
import anthropic

# Configure structured JSON logging
logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("agent.tools")

client = anthropic.Anthropic()

def log_tool_call(
    tool_name: str,
    tool_use_id: str,
    inputs: dict,
    fn: Callable,
    run_id: str,
    turn: int,
) -> str:
    """Execute a tool and log the full call record as structured JSON."""
    call_id = str(uuid.uuid4())[:8]
    start = time.monotonic()

    record = {
        "event": "tool_call",
        "run_id": run_id,
        "call_id": call_id,
        "tool_use_id": tool_use_id,
        "turn": turn,
        "tool": tool_name,
        "inputs": inputs,
    }

    try:
        result = fn(**inputs)
        elapsed_ms = (time.monotonic() - start) * 1000

        # Parse result for logging if it's JSON
        try:
            parsed = json.loads(result)
            log_result = parsed if isinstance(parsed, dict) else {"raw": result[:200]}
        except (json.JSONDecodeError, TypeError):
            log_result = {"raw": str(result)[:200]}

        record.update({
            "status": "ok",
            "latency_ms": round(elapsed_ms, 1),
            "output_size_bytes": len(result) if isinstance(result, str) else 0,
            "output_preview": log_result,
        })
        logger.info(json.dumps(record))
        return result

    except Exception as e:
        elapsed_ms = (time.monotonic() - start) * 1000
        record.update({
            "status": "error",
            "latency_ms": round(elapsed_ms, 1),
            "error_type": type(e).__name__,
            "error_message": str(e),
            "traceback": traceback.format_exc().strip(),
        })
        logger.error(json.dumps(record))
        return json.dumps({"error": str(e), "tool": tool_name})

# Tool implementations (unchanged — no logging needed inside)
def search_documents(query: str) -> str:
    return json.dumps({"results": [{"id": 1, "title": "Doc A", "score": 0.92}], "total": 1})

def get_weather(city: str) -> str:
    if city == "Atlantis":
        raise ValueError(f"Unknown city: {city}")
    return json.dumps({"city": city, "temp": 22, "condition": "sunny"})

def calculate(expression: str) -> str:
    result = eval(expression, {"__builtins__": {}})  # noqa: S307 — demo only
    return json.dumps({"expression": expression, "result": result})

TOOL_REGISTRY = {
    "search_documents": search_documents,
    "get_weather": get_weather,
    "calculate": calculate,
}

TOOLS = [
    {"name": "search_documents", "description": "Search documents.",
     "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"name": "get_weather", "description": "Get weather for a city.",
     "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}},
    {"name": "calculate", "description": "Evaluate a math expression.",
     "input_schema": {"type": "object", "properties": {"expression": {"type": "string"}}, "required": ["expression"]}},
]

def run_agent(user_message: str) -> str:
    run_id = str(uuid.uuid4())[:8]
    logger.info(json.dumps({"event": "run_start", "run_id": run_id, "user_message": user_message[:100]}))

    messages = [{"role": "user", "content": user_message}]
    turn = 0

    while True:
        turn += 1
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            tools=TOOLS,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            reply = next((b.text for b in response.content if hasattr(b, "text")), "")
            logger.info(json.dumps({"event": "run_complete", "run_id": run_id, "turns": turn}))
            return reply

        messages.append({"role": "assistant", "content": response.content})
        tool_results = []

        for block in response.content:
            if block.type == "tool_use":
                fn = TOOL_REGISTRY.get(block.name, lambda **k: json.dumps({"error": "unknown tool"}))
                result = log_tool_call(
                    tool_name=block.name,
                    tool_use_id=block.id,
                    inputs=block.input,
                    fn=fn,
                    run_id=run_id,
                    turn=turn,
                )
                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})

        messages.append({"role": "user", "content": tool_results})

result = run_agent("What's the weather in Paris? Also calculate 47 * 23.")
print(f"\nFinal: {result[:100]}")
```

**Expected Token Savings:** None — same tokens; reduces debugging time from hours to minutes
**Environment:** `pip install anthropic`

---

### Option 2 — Decorator-Based Tool Logging with Metrics Collection

Decorate tool functions with `@logged_tool` at definition time. Metrics (call counts, error rates, latency p95) are collected automatically.

```python
import json
import logging
import time
import functools
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable
import anthropic

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger("agent")
client = anthropic.Anthropic()

@dataclass
class ToolMetrics:
    call_count: int = 0
    error_count: int = 0
    total_latency_ms: float = 0.0
    latencies: list[float] = field(default_factory=list)

    @property
    def error_rate(self) -> float:
        return self.error_count / self.call_count if self.call_count else 0.0

    @property
    def p95_latency(self) -> float:
        if not self.latencies:
            return 0.0
        sorted_lat = sorted(self.latencies)
        idx = int(len(sorted_lat) * 0.95)
        return sorted_lat[min(idx, len(sorted_lat) - 1)]

_metrics: dict[str, ToolMetrics] = defaultdict(ToolMetrics)

def logged_tool(fn: Callable) -> Callable:
    """Decorator: logs every call with inputs, outputs, latency, and errors."""
    tool_name = fn.__name__

    @functools.wraps(fn)
    def wrapper(*args, **kwargs) -> str:
        m = _metrics[tool_name]
        m.call_count += 1
        start = time.monotonic()

        log_entry: dict[str, Any] = {
            "tool": tool_name,
            "call_no": m.call_count,
            "inputs": kwargs,
        }

        try:
            result = fn(*args, **kwargs)
            elapsed = (time.monotonic() - start) * 1000
            m.total_latency_ms += elapsed
            m.latencies.append(elapsed)

            log_entry.update({"status": "ok", "latency_ms": round(elapsed, 1)})
            logger.info(json.dumps(log_entry))
            return result

        except Exception as e:
            elapsed = (time.monotonic() - start) * 1000
            m.error_count += 1
            m.latencies.append(elapsed)

            log_entry.update({
                "status": "error",
                "latency_ms": round(elapsed, 1),
                "error": str(e),
            })
            logger.error(json.dumps(log_entry))
            return json.dumps({"error": str(e)})

    return wrapper

def print_metrics():
    print("\n=== Tool Metrics ===")
    for name, m in _metrics.items():
        print(f"{name:25s} calls={m.call_count} errors={m.error_count} "
              f"err_rate={m.error_rate:.0%} p95={m.p95_latency:.0f}ms")

# Decorate tools at definition
@logged_tool
def search_web(query: str) -> str:
    return json.dumps({"results": [{"title": f"Result for {query}", "url": "https://example.com"}]})

@logged_tool
def read_file(path: str) -> str:
    if "secret" in path:
        raise PermissionError(f"Access denied: {path}")
    return json.dumps({"path": path, "content": "File content here...", "size_bytes": 1024})

@logged_tool
def write_summary(content: str, output_path: str) -> str:
    return json.dumps({"written": True, "path": output_path, "bytes": len(content)})

TOOL_MAP = {
    "search_web": search_web,
    "read_file": read_file,
    "write_summary": write_summary,
}

TOOLS = [
    {"name": "search_web", "description": "Search the web.",
     "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"name": "read_file", "description": "Read a file by path.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "write_summary", "description": "Write a summary to a file.",
     "input_schema": {"type": "object",
                      "properties": {"content": {"type": "string"}, "output_path": {"type": "string"}},
                      "required": ["content", "output_path"]}},
]

def run_agent(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]
    while True:
        response = client.messages.create(
            model="claude-sonnet-4-6", max_tokens=1024, tools=TOOLS, messages=messages
        )
        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if hasattr(b, "text")), "")
        messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                fn = TOOL_MAP.get(block.name, lambda **k: json.dumps({"error": "unknown"}))
                result = fn(**block.input)
                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})
        messages.append({"role": "user", "content": tool_results})

run_agent("Search for Python best practices, read the guide at docs/guide.txt, then write a summary to output/summary.txt.")
print_metrics()
```

**Expected Token Savings:** None — same tokens; enables SLA monitoring and error rate alerting
**Environment:** `pip install anthropic`

---

### Option 3 — Distributed Trace with Span Hierarchy

Model the agent run as a root span with child spans per tool call. Compatible with OpenTelemetry exporters — works with Jaeger, Datadog, Honeycomb.

```python
import json
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Generator, Optional
import anthropic

client = anthropic.Anthropic()

@dataclass
class Span:
    span_id: str
    parent_id: Optional[str]
    operation: str
    start_time: float
    attributes: dict = field(default_factory=dict)
    end_time: Optional[float] = None
    status: str = "ok"
    error: Optional[str] = None

    def finish(self, status: str = "ok", error: str = None):
        self.end_time = time.monotonic()
        self.status = status
        self.error = error

    @property
    def duration_ms(self) -> float:
        if self.end_time:
            return (self.end_time - self.start_time) * 1000
        return 0.0

    def to_dict(self) -> dict:
        return {
            "span_id": self.span_id,
            "parent_id": self.parent_id,
            "operation": self.operation,
            "duration_ms": round(self.duration_ms, 1),
            "status": self.status,
            "error": self.error,
            "attributes": self.attributes,
        }

class Tracer:
    def __init__(self, service_name: str):
        self.service = service_name
        self._spans: list[Span] = []
        self._active: Optional[Span] = None

    @contextmanager
    def span(self, operation: str, **attributes) -> Generator[Span, None, None]:
        s = Span(
            span_id=str(uuid.uuid4())[:8],
            parent_id=self._active.span_id if self._active else None,
            operation=operation,
            start_time=time.monotonic(),
            attributes=attributes,
        )
        prev = self._active
        self._active = s
        try:
            yield s
            s.finish("ok")
        except Exception as e:
            s.finish("error", str(e))
            raise
        finally:
            self._active = prev
            self._spans.append(s)

    def print_trace(self):
        print(f"\n=== Trace [{self.service}] ===")
        spans_by_parent: dict[Optional[str], list[Span]] = {}
        for s in self._spans:
            spans_by_parent.setdefault(s.parent_id, []).append(s)

        def print_span(span: Span, depth: int = 0):
            indent = "  " * depth
            status_icon = "✓" if span.status == "ok" else "✗"
            print(f"{indent}{status_icon} [{span.duration_ms:.0f}ms] {span.operation}")
            if span.attributes:
                for k, v in span.attributes.items():
                    print(f"{indent}   {k}: {str(v)[:60]}")
            if span.error:
                print(f"{indent}   ERROR: {span.error}")
            for child in spans_by_parent.get(span.span_id, []):
                print_span(child, depth + 1)

        root_spans = spans_by_parent.get(None, [])
        for root in root_spans:
            print_span(root)

tracer = Tracer("agent")

def make_traced_tool(name: str, fn) -> callable:
    def wrapper(**kwargs) -> str:
        with tracer.span(f"tool.{name}", **kwargs) as span:
            result = fn(**kwargs)
            try:
                parsed = json.loads(result)
                span.attributes["result_keys"] = list(parsed.keys()) if isinstance(parsed, dict) else "list"
            except Exception:
                span.attributes["result_size"] = len(result)
            return result
    return wrapper

# Tool implementations
def _fetch_data(source: str) -> str:
    time.sleep(0.05)  # Simulated latency
    return json.dumps({"source": source, "records": 42, "status": "ok"})

def _process_data(data_source: str, operation: str) -> str:
    time.sleep(0.03)
    return json.dumps({"processed": True, "operation": operation, "rows_affected": 42})

def _send_report(destination: str, format: str = "json") -> str:
    time.sleep(0.02)
    return json.dumps({"sent": True, "destination": destination, "format": format})

fetch_data = make_traced_tool("fetch_data", _fetch_data)
process_data = make_traced_tool("process_data", _process_data)
send_report = make_traced_tool("send_report", _send_report)

TOOL_MAP = {"fetch_data": fetch_data, "process_data": process_data, "send_report": send_report}

TOOLS = [
    {"name": "fetch_data", "description": "Fetch data from a source.",
     "input_schema": {"type": "object", "properties": {"source": {"type": "string"}}, "required": ["source"]}},
    {"name": "process_data", "description": "Process fetched data.",
     "input_schema": {"type": "object",
                      "properties": {"data_source": {"type": "string"}, "operation": {"type": "string"}},
                      "required": ["data_source", "operation"]}},
    {"name": "send_report", "description": "Send a report to a destination.",
     "input_schema": {"type": "object", "properties": {"destination": {"type": "string"}}, "required": ["destination"]}},
]

def run_agent(user_message: str) -> str:
    with tracer.span("agent.run", user_message=user_message[:60]) as root:
        messages = [{"role": "user", "content": user_message}]
        turn = 0

        while True:
            turn += 1
            with tracer.span("agent.llm_call", turn=turn, model="claude-sonnet-4-6"):
                response = client.messages.create(
                    model="claude-sonnet-4-6", max_tokens=1024, tools=TOOLS, messages=messages
                )

            if response.stop_reason == "end_turn":
                root.attributes["turns"] = turn
                return next((b.text for b in response.content if hasattr(b, "text")), "")

            messages.append({"role": "assistant", "content": response.content})
            tool_results = []

            for block in response.content:
                if block.type == "tool_use":
                    fn = TOOL_MAP.get(block.name, lambda **k: json.dumps({"error": "unknown"}))
                    result = fn(**block.input)
                    tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})

            messages.append({"role": "user", "content": tool_results})

result = run_agent("Fetch data from sales_db, process it with aggregation, then send a report to dashboard.")
tracer.print_trace()
```

**Expected Token Savings:** None — observability investment; enables production diagnosis
**Environment:** `pip install anthropic`

---

### Option 4 — Replay Log for Deterministic Debugging

Record every tool call input/output to a replay log. When a run fails, replay the recorded tool responses without hitting real APIs — reproduce the exact failure instantly.

```python
import json
import os
import time
import anthropic
from dataclasses import dataclass, asdict
from enum import Enum
from typing import Callable, Optional

client = anthropic.Anthropic()

class LogMode(Enum):
    RECORD = "record"   # Live run — record all tool I/O
    REPLAY = "replay"   # Debug run — use recorded tool I/O

@dataclass
class ToolCallRecord:
    sequence: int
    tool_name: str
    inputs: dict
    output: str
    latency_ms: float
    error: Optional[str] = None

class ReplayLogger:
    def __init__(self, log_path: str, mode: LogMode = LogMode.RECORD):
        self.log_path = log_path
        self.mode = mode
        self._records: list[ToolCallRecord] = []
        self._sequence = 0

        if mode == LogMode.REPLAY:
            self._load()

    def _load(self):
        if os.path.exists(self.log_path):
            with open(self.log_path) as f:
                data = json.load(f)
            self._records = [ToolCallRecord(**r) for r in data]
            print(f"[Replay] Loaded {len(self._records)} recorded tool calls from {self.log_path}")

    def save(self):
        with open(self.log_path, "w") as f:
            json.dump([asdict(r) for r in self._records], f, indent=2)
        print(f"[Record] Saved {len(self._records)} tool calls to {self.log_path}")

    def execute(self, tool_name: str, inputs: dict, fn: Callable) -> str:
        self._sequence += 1

        if self.mode == LogMode.REPLAY:
            # Find the matching recorded call
            matching = [
                r for r in self._records
                if r.sequence == self._sequence and r.tool_name == tool_name
            ]
            if matching:
                rec = matching[0]
                print(f"[Replay] #{self._sequence} {tool_name}({list(inputs.keys())}) → replayed ({rec.latency_ms:.0f}ms)")
                if rec.error:
                    raise RuntimeError(rec.error)
                return rec.output
            else:
                raise RuntimeError(f"No recorded call #{self._sequence} for tool '{tool_name}'")

        # RECORD mode — call real tool and log
        start = time.monotonic()
        error = None
        try:
            output = fn(**inputs)
        except Exception as e:
            output = json.dumps({"error": str(e)})
            error = str(e)
        finally:
            latency = (time.monotonic() - start) * 1000

        rec = ToolCallRecord(
            sequence=self._sequence,
            tool_name=tool_name,
            inputs=inputs,
            output=output,
            latency_ms=round(latency, 1),
            error=error,
        )
        self._records.append(rec)
        print(f"[Record] #{self._sequence} {tool_name}({list(inputs.keys())}) → {latency:.0f}ms {'ERR' if error else 'OK'}")
        return output

def get_stock_price(symbol: str) -> str:
    return json.dumps({"symbol": symbol, "price": 182.45, "change": +1.2})

def get_news(topic: str) -> str:
    return json.dumps({"topic": topic, "headlines": [f"Breaking: {topic} update", f"Analysis: {topic} trends"]})

TOOL_MAP = {"get_stock_price": get_stock_price, "get_news": get_news}
TOOLS = [
    {"name": "get_stock_price", "description": "Get stock price.",
     "input_schema": {"type": "object", "properties": {"symbol": {"type": "string"}}, "required": ["symbol"]}},
    {"name": "get_news", "description": "Get news headlines.",
     "input_schema": {"type": "object", "properties": {"topic": {"type": "string"}}, "required": ["topic"]}},
]

def run_agent(user_message: str, replay_log: ReplayLogger) -> str:
    messages = [{"role": "user", "content": user_message}]
    while True:
        response = client.messages.create(
            model="claude-sonnet-4-6", max_tokens=1024, tools=TOOLS, messages=messages
        )
        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if hasattr(b, "text")), "")
        messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                fn = TOOL_MAP.get(block.name, lambda **k: json.dumps({"error": "unknown"}))
                result = replay_log.execute(block.name, block.input, fn)
                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})
        messages.append({"role": "user", "content": tool_results})

LOG_PATH = "/tmp/agent_replay_demo.json"

# First run: RECORD mode
print("=== RECORD RUN ===")
recorder = ReplayLogger(LOG_PATH, mode=LogMode.RECORD)
result = run_agent("Get the stock price of AAPL and find recent news about it.", recorder)
recorder.save()
print(f"Result: {result[:80]}...")

# Second run: REPLAY mode — no real API calls for tools
print("\n=== REPLAY RUN (exact same tool I/O) ===")
replayer = ReplayLogger(LOG_PATH, mode=LogMode.REPLAY)
result = run_agent("Get the stock price of AAPL and find recent news about it.", replayer)
print(f"Result: {result[:80]}...")
```

**Expected Token Savings:** 100% of tool call costs in replay — all tool I/O is synthetic
**Environment:** `pip install anthropic`

---

### Option 5 — Error-Only Alert Logger with Sampling

In high-throughput production, log every tool error and a 1% sample of successes. Keeps log volume manageable while capturing all failures.

```python
import json
import logging
import random
import time
from typing import Callable
import anthropic

client = anthropic.Anthropic()

# Two loggers: one for errors (always), one for sampled successes
error_logger = logging.getLogger("agent.tool.errors")
sample_logger = logging.getLogger("agent.tool.sample")

logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(name)s %(message)s")

SUCCESS_SAMPLE_RATE = 0.01   # Log 1% of successes for baseline metrics
SLOW_THRESHOLD_MS = 500      # Always log if slower than this

def production_log_tool(
    tool_name: str,
    inputs: dict,
    fn: Callable,
    context: dict = None,
) -> str:
    start = time.monotonic()
    context = context or {}

    try:
        result = fn(**inputs)
        elapsed_ms = (time.monotonic() - start) * 1000

        should_sample = random.random() < SUCCESS_SAMPLE_RATE
        is_slow = elapsed_ms > SLOW_THRESHOLD_MS

        if should_sample or is_slow:
            sample_logger.info(json.dumps({
                "tool": tool_name,
                "status": "ok",
                "latency_ms": round(elapsed_ms, 1),
                "sampled": should_sample,
                "slow": is_slow,
                **context,
            }))
        return result

    except Exception as e:
        elapsed_ms = (time.monotonic() - start) * 1000
        # Always log errors — never sampled
        error_logger.error(json.dumps({
            "tool": tool_name,
            "status": "error",
            "latency_ms": round(elapsed_ms, 1),
            "error_type": type(e).__name__,
            "error_message": str(e),
            "inputs": {k: str(v)[:100] for k, v in inputs.items()},
            **context,
        }))
        return json.dumps({"error": str(e), "tool": tool_name})

def simulate_tool(name: str, fail_rate: float = 0.1, slow_rate: float = 0.05) -> Callable:
    def tool(**kwargs) -> str:
        if random.random() < fail_rate:
            raise ConnectionError(f"{name} service temporarily unavailable")
        if random.random() < slow_rate:
            time.sleep(0.6)  # Slow call
        return json.dumps({"tool": name, "result": "ok", **kwargs})
    tool.__name__ = name
    return tool

TOOLS_IMPL = {
    "tool_a": simulate_tool("tool_a", fail_rate=0.15),
    "tool_b": simulate_tool("tool_b", fail_rate=0.05),
    "tool_c": simulate_tool("tool_c", fail_rate=0.0, slow_rate=0.3),
}

TOOLS = [
    {"name": name, "description": f"Tool {name}.",
     "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}
    for name in TOOLS_IMPL
]

def run_agent_batch(n_runs: int = 20):
    """Simulate multiple agent runs to see sampling and error logging."""
    results = {"ok": 0, "error": 0}
    for run_idx in range(n_runs):
        messages = [{"role": "user", "content": f"Run {run_idx}: use tool_a, tool_b, and tool_c."}]
        run_ctx = {"run_id": f"run_{run_idx:04d}"}

        for _ in range(3):  # Simulate 3 tool calls per run
            tool_name = random.choice(list(TOOLS_IMPL.keys()))
            result_str = production_log_tool(
                tool_name=tool_name,
                inputs={"query": f"query_{run_idx}"},
                fn=TOOLS_IMPL[tool_name],
                context=run_ctx,
            )
            parsed = json.loads(result_str)
            if "error" in parsed:
                results["error"] += 1
            else:
                results["ok"] += 1

    print(f"\n=== Batch Summary: {n_runs} runs ===")
    print(f"  Tool calls: {results['ok']} ok, {results['error']} errors")
    print(f"  Error rate: {results['error'] / (results['ok'] + results['error']):.1%}")

run_agent_batch(20)
```

**Expected Token Savings:** None — observability layer; enables production SLO monitoring
**Environment:** `pip install anthropic`

---

### Option 6 — Async Structured Logger with Log Aggregation Export

Non-blocking async logger that batches log entries and exports to a log aggregation service (e.g., CloudWatch, Loki) without blocking the agent loop.

```python
import asyncio
import json
import time
import uuid
from collections import deque
from dataclasses import dataclass, asdict
from typing import Optional
import anthropic

async_client = anthropic.AsyncAnthropic()

@dataclass
class LogEntry:
    ts: float
    level: str
    event: str
    run_id: str
    data: dict

class AsyncBatchLogger:
    FLUSH_INTERVAL = 2.0    # Flush every 2 seconds
    MAX_BATCH = 50          # Or when 50 entries accumulate

    def __init__(self, export_fn=None):
        self._queue: deque[LogEntry] = deque()
        self._export_fn = export_fn or self._default_export
        self._task: Optional[asyncio.Task] = None

    async def start(self):
        self._task = asyncio.create_task(self._flush_loop())

    async def stop(self):
        if self._task:
            self._task.cancel()
            await self._flush()  # Final flush

    def log(self, level: str, event: str, run_id: str, **data):
        entry = LogEntry(ts=time.time(), level=level, event=event, run_id=run_id, data=data)
        self._queue.append(entry)
        if len(self._queue) >= self.MAX_BATCH:
            asyncio.create_task(self._flush())

    async def _flush_loop(self):
        while True:
            await asyncio.sleep(self.FLUSH_INTERVAL)
            await self._flush()

    async def _flush(self):
        if not self._queue:
            return
        batch = []
        while self._queue:
            batch.append(self._queue.popleft())
        await self._export_fn(batch)

    @staticmethod
    async def _default_export(batch: list[LogEntry]):
        """Default: print as newline-delimited JSON (compatible with most log aggregators)."""
        for entry in batch:
            print(json.dumps(asdict(entry)))

batch_logger = AsyncBatchLogger()

async def traced_tool_call(
    tool_name: str,
    inputs: dict,
    fn,
    run_id: str,
) -> str:
    span_id = str(uuid.uuid4())[:8]
    batch_logger.log("INFO", "tool.start", run_id, tool=tool_name, inputs=inputs, span_id=span_id)
    start = time.monotonic()

    try:
        result = await fn(**inputs)
        elapsed_ms = (time.monotonic() - start) * 1000
        batch_logger.log("INFO", "tool.end", run_id,
                         tool=tool_name, status="ok", latency_ms=round(elapsed_ms, 1), span_id=span_id)
        return result
    except Exception as e:
        elapsed_ms = (time.monotonic() - start) * 1000
        batch_logger.log("ERROR", "tool.error", run_id,
                         tool=tool_name, error=str(e), latency_ms=round(elapsed_ms, 1), span_id=span_id)
        return json.dumps({"error": str(e)})

async def fetch_orders(customer_id: str) -> str:
    await asyncio.sleep(0.03)
    return json.dumps({"customer_id": customer_id, "orders": [{"id": "ORD-1", "total": 99.0}]})

async def check_inventory(sku: str) -> str:
    await asyncio.sleep(0.02)
    return json.dumps({"sku": sku, "in_stock": True, "qty": 42})

TOOL_MAP = {"fetch_orders": fetch_orders, "check_inventory": check_inventory}
TOOLS = [
    {"name": "fetch_orders", "description": "Fetch orders for a customer.",
     "input_schema": {"type": "object", "properties": {"customer_id": {"type": "string"}}, "required": ["customer_id"]}},
    {"name": "check_inventory", "description": "Check inventory for a SKU.",
     "input_schema": {"type": "object", "properties": {"sku": {"type": "string"}}, "required": ["sku"]}},
]

async def run_agent(user_message: str) -> str:
    run_id = str(uuid.uuid4())[:8]
    batch_logger.log("INFO", "agent.start", run_id, message=user_message[:80])
    messages = [{"role": "user", "content": user_message}]

    while True:
        response = await async_client.messages.create(
            model="claude-sonnet-4-6", max_tokens=1024, tools=TOOLS, messages=messages
        )
        if response.stop_reason == "end_turn":
            batch_logger.log("INFO", "agent.complete", run_id)
            return next((b.text for b in response.content if hasattr(b, "text")), "")

        messages.append({"role": "assistant", "content": response.content})
        tool_results = []

        for block in response.content:
            if block.type == "tool_use":
                fn = TOOL_MAP.get(block.name, lambda **k: json.dumps({"error": "unknown"}))
                result = await traced_tool_call(block.name, block.input, fn, run_id)
                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})

        messages.append({"role": "user", "content": tool_results})

async def main():
    await batch_logger.start()
    result = await run_agent("Fetch orders for customer C-101 and check inventory for SKU-ABC.")
    print(f"\nAgent result: {result[:80]}...")
    await batch_logger.stop()

asyncio.run(main())
```

**Expected Token Savings:** None — async logging adds <1ms overhead; enables production observability
**Environment:** `pip install anthropic`

---

## Comparison

| Option | Log Format | Overhead | Best For |
|--------|-----------|---------|----------|
| Structured Interceptor | JSON per call | Minimal | Retrofit into any existing agent |
| Decorator | JSON + metrics | Minimal | New agents — enforce at definition |
| Distributed Trace | Span hierarchy | Low | Multi-service systems, Jaeger/OTEL |
| Replay Log | JSON fixture | Low record, zero replay | Reproducing production failures |
| Error-Only + Sampling | JSON | Near-zero | High-throughput production |
| Async Batch Export | JSON NDJL | <1ms | Cloud log aggregation (CW, Loki) |

**Recommended starting point:** Option 1 (Structured Interceptor) — one `log_tool_call()` wrapper in your tool dispatch loop. Implement in 20 minutes; provides full input/output/latency/error logging immediately. Add Option 4 (Replay Log) when you need deterministic debugging.
