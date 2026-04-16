---
layout: solution
title: "Agent Doesn't Implement Span-Based Distributed Tracing"
category: observability
description: "Agents without distributed tracing produce opaque logs where slow steps, cascading failures, and cross-agent latency are invisible. These patterns show how to instrument agent calls with spans so every step is traceable end-to-end."
tags: [observability, tracing, spans, distributed, debugging, anthropic]
---

## Problem

A multi-step agent pipeline fails after 12 seconds. The logs say "task completed" or "error". Without span-based tracing, you cannot tell which tool call was slow, which sub-agent failed, or where in the pipeline a retry was triggered. Span-based tracing adds a structured trace ID and timing hierarchy to every LLM call, tool execution, and sub-agent invocation.

---

### Option 1: Lightweight Manual Span Instrumentation

Implement spans as simple context managers with nested timing, no external dependencies.

```python
import time
import json
import uuid
import asyncio
from contextlib import contextmanager
from dataclasses import dataclass, field, asdict
from typing import Optional
import anthropic

client = anthropic.Anthropic()

@dataclass
class Span:
    trace_id: str
    span_id: str
    parent_id: Optional[str]
    name: str
    start_time: float
    end_time: Optional[float] = None
    status: str = "ok"
    attrs: dict = field(default_factory=dict)
    events: list[dict] = field(default_factory=list)

    @property
    def duration_ms(self) -> float:
        if self.end_time is None:
            return 0.0
        return (self.end_time - self.start_time) * 1000

    def add_event(self, name: str, attrs: dict = None):
        self.events.append({"name": name, "time_ms": (time.monotonic() - self.start_time) * 1000, "attrs": attrs or {}})

    def finish(self, status: str = "ok", **attrs):
        self.end_time = time.monotonic()
        self.status = status
        self.attrs.update(attrs)

class Tracer:
    def __init__(self):
        self._spans: list[Span] = []
        self._current: list[Span] = []

    @contextmanager
    def span(self, name: str, **attrs):
        trace_id = self._current[0].trace_id if self._current else str(uuid.uuid4())[:8]
        parent_id = self._current[-1].span_id if self._current else None
        s = Span(trace_id=trace_id, span_id=str(uuid.uuid4())[:8],
                 parent_id=parent_id, name=name,
                 start_time=time.monotonic(), attrs=dict(attrs))
        self._current.append(s)
        try:
            yield s
            s.finish("ok")
        except Exception as e:
            s.finish("error", error=str(e))
            raise
        finally:
            self._current.pop()
            self._spans.append(s)

    def print_trace(self):
        if not self._spans:
            return
        root_trace = self._spans[0].trace_id
        spans = [s for s in self._spans if s.trace_id == root_trace]
        # Sort by start time
        spans.sort(key=lambda s: s.start_time)
        print(f"\n=== Trace {root_trace} ===")
        for s in spans:
            indent = "  " if s.parent_id else ""
            status_icon = "✓" if s.status == "ok" else "✗"
            print(f"  {indent}{status_icon} [{s.name}] {s.duration_ms:.1f}ms  {s.attrs}")
            for ev in s.events:
                print(f"  {indent}   ↳ {ev['name']} +{ev['time_ms']:.1f}ms")

tracer = Tracer()

def agent_pipeline(user_query: str) -> str:
    with tracer.span("pipeline", query=user_query[:50]) as root:
        # Step 1: Classify intent
        with tracer.span("classify_intent") as classify_span:
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=50,
                messages=[{"role": "user", "content": f"Classify in one word: {user_query}"}],
            )
            intent = response.content[0].text.strip()
            classify_span.add_event("classified", {"intent": intent})
            classify_span.attrs["tokens_in"] = response.usage.input_tokens

        # Step 2: Generate response
        with tracer.span("generate_response", intent=intent) as gen_span:
            response = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=512,
                messages=[{"role": "user", "content": user_query}],
            )
            result = response.content[0].text
            gen_span.attrs.update({
                "tokens_in": response.usage.input_tokens,
                "tokens_out": response.usage.output_tokens,
            })

        root.add_event("pipeline_complete", {"intent": intent})
    return result

if __name__ == "__main__":
    result = agent_pipeline("Explain the difference between TCP and UDP protocols.")
    print(result[:300])
    tracer.print_trace()

# Expected Token Savings: Tracing is zero-token overhead; finds slow steps without token-burning retries
# Environment: ANTHROPIC_API_KEY
```

---

### Option 2: OpenTelemetry-Compatible Span Export

Emit spans in OTLP-compatible JSON so they can be ingested by Jaeger, Tempo, or any OTel collector.

```python
import time
import json
import uuid
import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Optional
import anthropic

client = anthropic.AsyncAnthropic()

@dataclass
class OTelSpan:
    trace_id: str
    span_id: str
    parent_span_id: Optional[str]
    name: str
    start_time_unix_nano: int
    end_time_unix_nano: int = 0
    status_code: int = 0    # 0=unset, 1=ok, 2=error
    attributes: list[dict] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)

    def to_otlp(self) -> dict:
        return {
            "traceId": self.trace_id,
            "spanId": self.span_id,
            "parentSpanId": self.parent_span_id or "",
            "name": self.name,
            "startTimeUnixNano": self.start_time_unix_nano,
            "endTimeUnixNano": self.end_time_unix_nano,
            "status": {"code": self.status_code},
            "attributes": self.attributes,
            "events": self.events,
        }

def attr(key: str, value) -> dict:
    if isinstance(value, str):
        return {"key": key, "value": {"stringValue": value}}
    elif isinstance(value, int):
        return {"key": key, "value": {"intValue": value}}
    elif isinstance(value, float):
        return {"key": key, "value": {"doubleValue": value}}
    return {"key": key, "value": {"stringValue": str(value)}}

class OTelTracer:
    def __init__(self, service_name: str):
        self.service_name = service_name
        self._spans: list[OTelSpan] = []
        self._stack: list[OTelSpan] = []
        self._trace_id = uuid.uuid4().hex

    @asynccontextmanager
    async def span(self, name: str, **attrs):
        parent_id = self._stack[-1].span_id if self._stack else None
        s = OTelSpan(
            trace_id=self._trace_id,
            span_id=uuid.uuid4().hex[:16],
            parent_span_id=parent_id,
            name=name,
            start_time_unix_nano=int(time.time_ns()),
            attributes=[attr(k, v) for k, v in attrs.items()],
        )
        self._stack.append(s)
        try:
            yield s
            s.status_code = 1
        except Exception as e:
            s.status_code = 2
            s.attributes.append(attr("error.message", str(e)))
            raise
        finally:
            s.end_time_unix_nano = int(time.time_ns())
            self._stack.pop()
            self._spans.append(s)

    def export_otlp_json(self) -> str:
        spans_data = [s.to_otlp() for s in self._spans]
        payload = {
            "resourceSpans": [{
                "resource": {"attributes": [attr("service.name", self.service_name)]},
                "scopeSpans": [{"spans": spans_data}],
            }]
        }
        return json.dumps(payload, indent=2)

    def print_summary(self):
        for s in sorted(self._spans, key=lambda x: x.start_time_unix_nano):
            dur_ms = (s.end_time_unix_nano - s.start_time_unix_nano) / 1_000_000
            icon = "✓" if s.status_code == 1 else "✗"
            indent = "    " if s.parent_span_id else ""
            print(f"  {indent}{icon} {s.name}: {dur_ms:.1f}ms")

otel = OTelTracer("synapse-agent")

async def traced_agent_workflow(user_task: str) -> str:
    async with otel.span("workflow", task=user_task[:60]) as wf:
        # Sub-step: tool selection
        async with otel.span("select_tools", task_type="tool_selection"):
            resp = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=80,
                messages=[{"role": "user", "content": f"List 2-3 tools needed for: {user_task}"}],
            )
            tools_text = resp.content[0].text

        # Sub-step: main generation
        async with otel.span("llm_generate",
                              model="claude-sonnet-4-6",
                              input_tokens=0) as gen:
            resp2 = await client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=1024,
                messages=[{"role": "user", "content": user_task}],
            )
            gen.attributes.append(attr("input_tokens", resp2.usage.input_tokens))
            gen.attributes.append(attr("output_tokens", resp2.usage.output_tokens))
            result = resp2.content[0].text

        wf.attributes.append(attr("total_output_tokens", resp2.usage.output_tokens))
    return result

if __name__ == "__main__":
    async def main():
        result = await traced_agent_workflow("Design a rate limiter for a REST API.")
        print(result[:300])
        print("\n=== Trace Summary ===")
        otel.print_summary()
        # Uncomment to see full OTLP JSON for ingestion:
        # print(otel.export_otlp_json())
    asyncio.run(main())

# Expected Token Savings: OTel export is zero-token; enables latency diagnosis without blind retries
# Environment: ANTHROPIC_API_KEY
```

---

### Option 3: Async Context-Propagated Trace with Sub-Agent Fan-Out

Propagate trace context across parallel sub-agent calls using asyncio context variables.

```python
import time
import uuid
import asyncio
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Optional
import anthropic

client = anthropic.AsyncAnthropic()

current_trace_id: ContextVar[str] = ContextVar("trace_id", default="")
current_span_id: ContextVar[str] = ContextVar("span_id", default="")

@dataclass
class TraceSpan:
    trace_id: str
    span_id: str
    parent_id: str
    name: str
    start: float
    end: Optional[float] = None
    meta: dict = field(default_factory=dict)

_span_log: list[TraceSpan] = []

class PropagatedSpan:
    def __init__(self, name: str, **meta):
        self.name = name
        self.meta = meta
        self._span: Optional[TraceSpan] = None
        self._trace_token = None
        self._span_token = None

    async def __aenter__(self):
        trace_id = current_trace_id.get() or str(uuid.uuid4())[:8]
        parent_id = current_span_id.get()
        span_id = str(uuid.uuid4())[:8]

        self._trace_token = current_trace_id.set(trace_id)
        self._span_token = current_span_id.set(span_id)

        self._span = TraceSpan(trace_id=trace_id, span_id=span_id,
                               parent_id=parent_id, name=self.name,
                               start=time.monotonic(), meta=self.meta)
        return self._span

    async def __aexit__(self, exc_type, exc, tb):
        if self._span:
            self._span.end = time.monotonic()
            if exc_type:
                self._span.meta["error"] = str(exc)
            _span_log.append(self._span)
        if self._trace_token:
            current_trace_id.reset(self._trace_token)
        if self._span_token:
            current_span_id.reset(self._span_token)

async def sub_agent_task(task_name: str, prompt: str) -> str:
    async with PropagatedSpan(f"sub_agent:{task_name}", task=task_name) as span:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=[{"role": "user", "content": prompt}],
        )
        result = response.content[0].text
        span.meta["tokens"] = response.usage.output_tokens
        return result

async def orchestrated_pipeline(user_query: str) -> dict:
    async with PropagatedSpan("orchestrator", query=user_query[:50]):
        # Fan out to parallel sub-agents — each inherits trace context
        async with PropagatedSpan("parallel_research"):
            results = await asyncio.gather(
                sub_agent_task("technical", f"Technical aspects of: {user_query}"),
                sub_agent_task("practical", f"Practical implementation of: {user_query}"),
                sub_agent_task("tradeoffs", f"Tradeoffs and risks of: {user_query}"),
            )

        # Synthesize
        async with PropagatedSpan("synthesis", sources=3):
            synthesis_prompt = f"Synthesize these perspectives:\n" + "\n---\n".join(results)
            response = await client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=1024,
                messages=[{"role": "user", "content": synthesis_prompt}],
            )
            final = response.content[0].text

    return {
        "result": final,
        "sub_results": results,
    }

def print_trace_tree():
    by_parent: dict[str, list[TraceSpan]] = {}
    for s in _span_log:
        by_parent.setdefault(s.parent_id, []).append(s)

    def print_node(span_id: str, depth: int = 0):
        for s in by_parent.get(span_id, []):
            dur = ((s.end or time.monotonic()) - s.start) * 1000
            indent = "  " * depth
            print(f"{indent}[{s.name}] {dur:.1f}ms  {s.meta}")
            print_node(s.span_id, depth + 1)

    roots = [s for s in _span_log if not s.parent_id]
    for r in roots:
        dur = ((r.end or time.monotonic()) - r.start) * 1000
        print(f"[{r.name}] {dur:.1f}ms  trace={r.trace_id}")
        print_node(r.span_id, 1)

if __name__ == "__main__":
    async def main():
        output = await orchestrated_pipeline("How should I implement a distributed cache with TTL?")
        print(output["result"][:400])
        print("\n=== Trace Tree ===")
        print_trace_tree()
    asyncio.run(main())

# Expected Token Savings: Context propagation is zero-token; trace IDs enable precise retries of failed sub-agents only
# Environment: ANTHROPIC_API_KEY
```

---

### Option 4: Token-Level Span Metrics with Cost Attribution

Track token usage per span and roll up cost attribution across the trace.

```python
import time
import uuid
import asyncio
from dataclasses import dataclass, field
from contextlib import asynccontextmanager
import anthropic

client = anthropic.AsyncAnthropic()

# Token cost per 1M tokens (approximate)
MODEL_COSTS = {
    "claude-haiku-4-5-20251001": {"input": 0.80, "output": 4.00},
    "claude-sonnet-4-6":         {"input": 3.00, "output": 15.00},
    "claude-opus-4-6":           {"input": 15.00, "output": 75.00},
}

@dataclass
class TokenSpan:
    name: str
    span_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    parent_id: str = ""
    start: float = field(default_factory=time.monotonic)
    end: float = 0.0
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    children: list["TokenSpan"] = field(default_factory=list)

    @property
    def duration_ms(self) -> float:
        return (self.end - self.start) * 1000

    @property
    def cost_usd(self) -> float:
        if not self.model or self.model not in MODEL_COSTS:
            return sum(c.cost_usd for c in self.children)
        rates = MODEL_COSTS[self.model]
        return (self.input_tokens * rates["input"] +
                self.output_tokens * rates["output"]) / 1_000_000

    @property
    def total_cost_usd(self) -> float:
        return self.cost_usd + sum(c.total_cost_usd for c in self.children)

    @property
    def total_tokens(self) -> int:
        own = self.input_tokens + self.output_tokens
        return own + sum(c.total_tokens for c in self.children)

class CostTracer:
    def __init__(self):
        self._root: TokenSpan | None = None
        self._stack: list[TokenSpan] = []

    @asynccontextmanager
    async def span(self, name: str):
        s = TokenSpan(name=name, parent_id=self._stack[-1].span_id if self._stack else "")
        if self._stack:
            self._stack[-1].children.append(s)
        else:
            self._root = s
        self._stack.append(s)
        try:
            yield s
        finally:
            s.end = time.monotonic()
            self._stack.pop()

    def print_cost_tree(self, span: TokenSpan = None, depth: int = 0):
        s = span or self._root
        if not s:
            return
        indent = "  " * depth
        own_cost = f"${s.cost_usd:.6f}" if s.model else ""
        total = f"${s.total_cost_usd:.6f}" if s.total_cost_usd > 0 else ""
        print(f"{indent}[{s.name}] {s.duration_ms:.0f}ms | tokens={s.total_tokens} | {own_cost or total}")
        for child in s.children:
            self.print_cost_tree(child, depth + 1)

cost_tracer = CostTracer()

async def traced_llm_call(span_name: str, model: str, prompt: str, max_tokens: int = 512) -> str:
    async with cost_tracer.span(span_name) as s:
        resp = await client.messages.create(
            model=model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        s.model = model
        s.input_tokens = resp.usage.input_tokens
        s.output_tokens = resp.usage.output_tokens
        s.cache_read = getattr(resp.usage, "cache_read_input_tokens", 0)
        return resp.content[0].text

async def multi_model_pipeline(task: str) -> str:
    async with cost_tracer.span("pipeline"):
        # Cheap routing decision
        route = await traced_llm_call("route_decision", "claude-haiku-4-5-20251001",
                                       f"Is this simple or complex? Answer one word: {task}", 20)

        if "simple" in route.lower():
            async with cost_tracer.span("simple_path"):
                result = await traced_llm_call("generate", "claude-haiku-4-5-20251001", task, 512)
        else:
            async with cost_tracer.span("complex_path"):
                # Fan-out to parallel analyses
                r1, r2 = await asyncio.gather(
                    traced_llm_call("analysis_1", "claude-sonnet-4-6", f"Technical analysis: {task}", 512),
                    traced_llm_call("analysis_2", "claude-haiku-4-5-20251001", f"Quick summary: {task}", 256),
                )
                result = await traced_llm_call("synthesis", "claude-sonnet-4-6",
                                                f"Synthesize: {r1[:200]}\n\n{r2[:200]}", 1024)
    return result

if __name__ == "__main__":
    async def main():
        result = await multi_model_pipeline("Design a distributed rate limiter with Redis.")
        print(result[:300])
        print("\n=== Cost Trace ===")
        cost_tracer.print_cost_tree()
    asyncio.run(main())

# Expected Token Savings: Cost tracing reveals which spans over-spend; enables targeted token reduction
# Environment: ANTHROPIC_API_KEY
```

---

### Option 5: SQLite-Backed Persistent Trace Store

Persist spans to SQLite so traces survive process restarts and can be queried across sessions.

```python
import time
import uuid
import sqlite3
import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
import anthropic

client = anthropic.AsyncAnthropic()
DB_PATH = Path("/tmp/agent_traces.db")

def init_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS spans (
            span_id TEXT PRIMARY KEY,
            trace_id TEXT NOT NULL,
            parent_id TEXT,
            name TEXT NOT NULL,
            start_ns INTEGER NOT NULL,
            end_ns INTEGER,
            status TEXT DEFAULT 'ok',
            model TEXT,
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            attrs TEXT DEFAULT '{}'
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_trace ON spans(trace_id)")
    conn.commit()
    return conn

conn = init_db(DB_PATH)

@dataclass
class PersistentSpan:
    span_id: str
    trace_id: str
    parent_id: str
    name: str
    start_ns: int
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0

    def save(self, end_ns: int, status: str = "ok", attrs: dict = None):
        import json
        conn.execute("""
            INSERT OR REPLACE INTO spans
            (span_id, trace_id, parent_id, name, start_ns, end_ns, status, model, input_tokens, output_tokens, attrs)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (self.span_id, self.trace_id, self.parent_id, self.name,
              self.start_ns, end_ns, status, self.model,
              self.input_tokens, self.output_tokens, json.dumps(attrs or {})))
        conn.commit()

_active_trace: list[PersistentSpan] = []

@asynccontextmanager
async def persistent_span(name: str, trace_id: str = None):
    tid = trace_id or (_active_trace[0].trace_id if _active_trace else str(uuid.uuid4())[:8])
    parent_id = _active_trace[-1].span_id if _active_trace else ""
    s = PersistentSpan(span_id=str(uuid.uuid4())[:8], trace_id=tid,
                       parent_id=parent_id, name=name, start_ns=time.time_ns())
    _active_trace.append(s)
    try:
        yield s
        s.save(time.time_ns(), "ok")
    except Exception as e:
        s.save(time.time_ns(), "error", {"error": str(e)})
        raise
    finally:
        _active_trace.pop()

async def run_traced_task(prompt: str) -> str:
    async with persistent_span("task") as root:
        async with persistent_span("classify") as cls_span:
            r = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=30,
                messages=[{"role": "user", "content": f"One word topic: {prompt}"}],
            )
            cls_span.model = "claude-haiku-4-5-20251001"
            cls_span.input_tokens = r.usage.input_tokens
            cls_span.output_tokens = r.usage.output_tokens

        async with persistent_span("respond") as resp_span:
            r2 = await client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=512,
                messages=[{"role": "user", "content": prompt}],
            )
            resp_span.model = "claude-sonnet-4-6"
            resp_span.input_tokens = r2.usage.input_tokens
            resp_span.output_tokens = r2.usage.output_tokens
            return r2.content[0].text

def query_trace(trace_id: str):
    rows = conn.execute(
        "SELECT name, (end_ns-start_ns)/1e6 as dur_ms, status, model, input_tokens, output_tokens "
        "FROM spans WHERE trace_id=? ORDER BY start_ns",
        (trace_id,)
    ).fetchall()
    print(f"\n=== Trace {trace_id} ===")
    for row in rows:
        print(f"  [{row[0]}] {row[1]:.1f}ms status={row[2]} model={row[3]} in={row[4]} out={row[5]}")

if __name__ == "__main__":
    async def main():
        result = await run_traced_task("Explain eventual consistency in distributed systems.")
        print(result[:300])
        trace_id = _active_trace[0].trace_id if _active_trace else conn.execute(
            "SELECT trace_id FROM spans ORDER BY start_ns DESC LIMIT 1").fetchone()[0]
        query_trace(trace_id)
    asyncio.run(main())

# Expected Token Savings: Persistent traces enable historical analysis without re-running expensive workflows
# Environment: ANTHROPIC_API_KEY
```

---

### Option 6: Trace-Aware Retry with Span Linkage

Link retry spans to their parent failed spans so retry storms are visible in the trace.

```python
import time
import uuid
import asyncio
from dataclasses import dataclass, field
from typing import Optional, Callable, Awaitable
import anthropic

client = anthropic.AsyncAnthropic()

@dataclass
class RetrySpan:
    name: str
    span_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    trace_id: str = ""
    parent_id: str = ""
    attempt: int = 0
    linked_spans: list[str] = field(default_factory=list)  # IDs of prior failed attempts
    start: float = field(default_factory=time.monotonic)
    end: float = 0.0
    status: str = "pending"
    error: str = ""
    meta: dict = field(default_factory=dict)

    def finish(self, status: str, **meta):
        self.end = time.monotonic()
        self.status = status
        self.meta.update(meta)

    @property
    def duration_ms(self) -> float:
        return (self.end - self.start) * 1000

_trace_store: list[RetrySpan] = []
_current_trace_id: str = str(uuid.uuid4())[:8]
_current_parent: list[str] = []

async def retry_with_tracing(
    name: str,
    fn: Callable[[], Awaitable],
    max_attempts: int = 3,
    base_delay: float = 0.5,
) -> tuple[any, list[RetrySpan]]:
    failed_spans = []
    last_error = None

    for attempt in range(1, max_attempts + 1):
        span = RetrySpan(
            name=f"{name}:attempt_{attempt}",
            trace_id=_current_trace_id,
            parent_id=_current_parent[-1] if _current_parent else "",
            attempt=attempt,
            linked_spans=[s.span_id for s in failed_spans],
        )
        _current_parent.append(span.span_id)
        try:
            result = await fn()
            span.finish("ok", attempt=attempt)
            _trace_store.append(span)
            _current_parent.pop()
            return result, failed_spans
        except Exception as e:
            last_error = e
            span.error = str(e)
            span.finish("error", attempt=attempt, error=str(e))
            failed_spans.append(span)
            _trace_store.append(span)
            _current_parent.pop()
            if attempt < max_attempts:
                delay = base_delay * (2 ** (attempt - 1))
                print(f"  [retry {name}: attempt {attempt} failed ({e}), retrying in {delay:.1f}s]")
                await asyncio.sleep(delay)

    raise RuntimeError(f"{name} failed after {max_attempts} attempts: {last_error}")

# Simulated flaky API
_call_count = 0
async def flaky_llm_call(prompt: str) -> str:
    global _call_count
    _call_count += 1
    if _call_count <= 2:
        raise TimeoutError(f"LLM timeout (simulated, call #{_call_count})")
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text

def print_retry_trace():
    print(f"\n=== Retry Trace {_current_trace_id} ===")
    for s in _trace_store:
        icon = "✓" if s.status == "ok" else "✗"
        links = f" [links: {s.linked_spans}]" if s.linked_spans else ""
        print(f"  {icon} [{s.name}] {s.duration_ms:.0f}ms{links}")
        if s.error:
            print(f"      error: {s.error}")

if __name__ == "__main__":
    async def main():
        try:
            result, retries = await retry_with_tracing(
                "llm_generate",
                lambda: flaky_llm_call("Explain distributed tracing in 2 sentences."),
                max_attempts=4,
            )
            print(f"Result: {result[:200]}")
            print(f"Succeeded after {len(retries)+1} attempts, {len(retries)} linked failure spans")
        except RuntimeError as e:
            print(f"All attempts failed: {e}")
        print_retry_trace()
    asyncio.run(main())

# Expected Token Savings: Linked retry spans reveal retry storms without adding any tokens to LLM calls
# Environment: ANTHROPIC_API_KEY
```

---

## Comparison

| Option | Approach | Persistence | External Deps | Best For |
|--------|----------|-------------|---------------|----------|
| 1 | Manual span context manager | In-memory | None | Quick instrumentation, no infrastructure |
| 2 | OTel-compatible OTLP export | In-memory | None (export ready) | Jaeger/Tempo/Grafana ingestion |
| 3 | Async context-propagated spans | In-memory | None | Multi-agent fan-out, parallel workflows |
| 4 | Token-level cost attribution | In-memory | None | Cost optimization, model routing |
| 5 | SQLite persistent trace store | Disk (SQLite) | None | Cross-session debugging, auditing |
| 6 | Trace-aware retry with span linkage | In-memory | None | Retry storm detection, failure analysis |
