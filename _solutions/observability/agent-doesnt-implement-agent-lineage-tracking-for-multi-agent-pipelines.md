---
layout: solution
title: "Agent Doesn't Implement Agent Lineage Tracking for Multi-Agent Pipelines"
category: observability
description: "Tracking the full ancestry chain of agent invocations — which agent spawned which, with what inputs, producing what outputs — enables root-cause debugging, cost attribution, and compliance auditing in complex multi-agent systems."
tags: [observability, lineage, multi-agent, tracing, audit, debugging]
---

## Problem

In multi-agent pipelines, an orchestrator spawns sub-agents, which may spawn further sub-agents. Without lineage tracking, when something goes wrong deep in the chain, you cannot reconstruct: who called whom, with what context, at what time, producing what output. This makes debugging, cost attribution, and compliance auditing impossible.

## Solutions

### Option 1: Simple Parent-Child Span Tree

```python
import anthropic
import uuid
import time
from dataclasses import dataclass, field
from typing import Optional

client = anthropic.Anthropic()

@dataclass
class AgentSpan:
    span_id: str
    parent_id: Optional[str]
    agent_name: str
    task: str
    start_time: float
    end_time: Optional[float] = None
    output: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    children: list["AgentSpan"] = field(default_factory=list)

    @property
    def duration_ms(self) -> float:
        if self.end_time:
            return (self.end_time - self.start_time) * 1000
        return 0.0

# Global registry
_spans: dict[str, AgentSpan] = {}
_root_spans: list[AgentSpan] = []

def start_span(agent_name: str, task: str, parent_id: Optional[str] = None) -> str:
    span_id = str(uuid.uuid4())[:8]
    span = AgentSpan(
        span_id=span_id,
        parent_id=parent_id,
        agent_name=agent_name,
        task=task,
        start_time=time.time()
    )
    _spans[span_id] = span

    if parent_id and parent_id in _spans:
        _spans[parent_id].children.append(span)
    else:
        _root_spans.append(span)

    return span_id

def end_span(span_id: str, output: str, usage: dict):
    span = _spans[span_id]
    span.end_time = time.time()
    span.output = output[:200]
    span.input_tokens = usage.get("input_tokens", 0)
    span.output_tokens = usage.get("output_tokens", 0)

def run_agent(agent_name: str, task: str, parent_span_id: Optional[str] = None) -> tuple[str, str]:
    """Run an agent and track its lineage."""
    span_id = start_span(agent_name, task, parent_span_id)

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        system=f"You are {agent_name}. Be concise.",
        messages=[{"role": "user", "content": task}]
    )
    output = response.content[0].text
    end_span(span_id, output, {
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens
    })

    return output, span_id

def print_lineage(span: AgentSpan, depth: int = 0):
    indent = "  " * depth
    print(f"{indent}[{span.span_id}] {span.agent_name} ({span.duration_ms:.0f}ms)")
    print(f"{indent}  Task: {span.task[:60]}")
    print(f"{indent}  Tokens: {span.input_tokens}in/{span.output_tokens}out")
    for child in span.children:
        print_lineage(child, depth + 1)

# Usage: orchestrator spawns sub-agents
output, root_id = run_agent("Orchestrator", "Plan a research report on AI safety")
output1, id1 = run_agent("Researcher", "Summarize key AI safety concerns", root_id)
output2, id2 = run_agent("Writer", f"Write an intro based on: {output1[:100]}", root_id)
output3, _ = run_agent("FactChecker", f"Verify: {output2[:100]}", id2)

print("\n=== AGENT LINEAGE TREE ===")
for root in _root_spans:
    print_lineage(root)

# Expected Token Savings: No savings — pure observability overhead is minimal
# Environment: ANTHROPIC_API_KEY required
```

### Option 2: Distributed Trace Context Propagation

```python
import anthropic
import uuid
import time
import json
from dataclasses import dataclass, field, asdict
from typing import Optional

client = anthropic.Anthropic()

@dataclass
class TraceContext:
    """Propagated through agent calls like HTTP trace headers."""
    trace_id: str           # Unique to the root request
    span_id: str            # Current span
    parent_span_id: Optional[str]
    depth: int
    root_agent: str

    def child(self, new_span_id: str) -> "TraceContext":
        return TraceContext(
            trace_id=self.trace_id,
            span_id=new_span_id,
            parent_span_id=self.span_id,
            depth=self.depth + 1,
            root_agent=self.root_agent
        )

    def to_header(self) -> str:
        """Serialize for injection into agent system prompts."""
        return json.dumps(asdict(self))

    @classmethod
    def from_header(cls, header: str) -> "TraceContext":
        return cls(**json.loads(header))

@dataclass
class SpanRecord:
    trace_id: str
    span_id: str
    parent_span_id: Optional[str]
    agent_name: str
    depth: int
    task: str
    output: str
    start_ts: float
    end_ts: float
    tokens_in: int
    tokens_out: int

# Append-only span store (would be a time-series DB in production)
SPAN_STORE: list[SpanRecord] = []

def new_trace(root_agent: str) -> TraceContext:
    return TraceContext(
        trace_id=str(uuid.uuid4()),
        span_id=str(uuid.uuid4())[:8],
        parent_span_id=None,
        depth=0,
        root_agent=root_agent
    )

def run_agent_with_context(
    agent_name: str,
    task: str,
    ctx: TraceContext,
    system_extra: str = ""
) -> tuple[str, TraceContext]:
    """Run agent with trace context propagated in system prompt."""
    child_span_id = str(uuid.uuid4())[:8]
    child_ctx = ctx.child(child_span_id)

    system = f"""You are {agent_name}.
[TRACE] trace_id={child_ctx.trace_id} span={child_ctx.span_id} depth={child_ctx.depth}
{system_extra}"""

    t0 = time.time()
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        system=system,
        messages=[{"role": "user", "content": task}]
    )
    t1 = time.time()
    output = response.content[0].text

    SPAN_STORE.append(SpanRecord(
        trace_id=child_ctx.trace_id,
        span_id=child_ctx.span_id,
        parent_span_id=child_ctx.parent_span_id,
        agent_name=agent_name,
        depth=child_ctx.depth,
        task=task[:150],
        output=output[:150],
        start_ts=t0,
        end_ts=t1,
        tokens_in=response.usage.input_tokens,
        tokens_out=response.usage.output_tokens
    ))

    return output, child_ctx

def print_trace(trace_id: str):
    spans = [s for s in SPAN_STORE if s.trace_id == trace_id]
    spans.sort(key=lambda s: s.start_ts)

    total_cost = sum(s.tokens_in + s.tokens_out for s in spans)
    print(f"\n=== TRACE {trace_id[:8]} === ({len(spans)} spans, {total_cost} total tokens)")

    for span in spans:
        indent = "  " * span.depth
        parent = span.parent_span_id or "ROOT"
        duration = (span.end_ts - span.start_ts) * 1000
        print(f"{indent}[{span.span_id}] ← {parent}")
        print(f"{indent}  Agent: {span.agent_name}, {duration:.0f}ms, {span.tokens_in}+{span.tokens_out}tok")

# Usage
root_ctx = new_trace("Orchestrator")

plan, ctx1 = run_agent_with_context("Orchestrator", "Break down: Write a market analysis report", root_ctx)
research, ctx2 = run_agent_with_context("Researcher", "Research market trends in AI 2024", ctx1)
analysis, ctx3 = run_agent_with_context("Analyst", f"Analyze trends: {research[:100]}", ctx1)
_, _ = run_agent_with_context("QA", f"Verify analysis: {analysis[:100]}", ctx3)

print_trace(root_ctx.trace_id)

# Expected Token Savings: Minimal overhead (~50 tokens/span for trace injection)
# Environment: ANTHROPIC_API_KEY required
```

### Option 3: SQLite-Persisted Lineage with Replay

```python
import anthropic
import sqlite3
import uuid
import time
import json
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Optional, Generator

client = anthropic.Anthropic()

DB_PATH = "/tmp/agent_lineage.db"

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS spans (
                span_id TEXT PRIMARY KEY,
                trace_id TEXT NOT NULL,
                parent_span_id TEXT,
                agent_name TEXT NOT NULL,
                task TEXT,
                output TEXT,
                model TEXT,
                tokens_in INTEGER DEFAULT 0,
                tokens_out INTEGER DEFAULT 0,
                start_ts REAL,
                end_ts REAL,
                status TEXT DEFAULT 'running',
                metadata TEXT DEFAULT '{}'
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_trace ON spans(trace_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_parent ON spans(parent_span_id)")

init_db()

@contextmanager
def agent_span(
    trace_id: str,
    agent_name: str,
    task: str,
    parent_span_id: Optional[str] = None,
    model: str = "claude-haiku-4-5-20251001",
    metadata: dict = None
) -> Generator[str, None, None]:
    """Context manager that records span lifecycle to SQLite."""
    span_id = str(uuid.uuid4())
    start_ts = time.time()

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO spans VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (span_id, trace_id, parent_span_id, agent_name, task[:500], None,
             model, 0, 0, start_ts, None, "running", json.dumps(metadata or {}))
        )

    try:
        yield span_id
    except Exception as e:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "UPDATE spans SET status=?, end_ts=? WHERE span_id=?",
                ("failed", time.time(), span_id)
            )
        raise
    finally:
        pass  # end_span called explicitly for output capture

def complete_span(span_id: str, output: str, tokens_in: int, tokens_out: int):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE spans SET output=?, tokens_in=?, tokens_out=?, end_ts=?, status=? WHERE span_id=?",
            (output[:1000], tokens_in, tokens_out, time.time(), "completed", span_id)
        )

def run_agent(
    trace_id: str,
    agent_name: str,
    task: str,
    parent_span_id: Optional[str] = None
) -> tuple[str, str]:
    with agent_span(trace_id, agent_name, task, parent_span_id) as span_id:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            system=f"You are {agent_name}. Be concise.",
            messages=[{"role": "user", "content": task}]
        )
        output = response.content[0].text
        complete_span(span_id, output, response.usage.input_tokens, response.usage.output_tokens)

    return output, span_id

def get_lineage(trace_id: str) -> list[dict]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM spans WHERE trace_id=? ORDER BY start_ts",
            (trace_id,)
        ).fetchall()
    return [dict(r) for r in rows]

def print_lineage_tree(trace_id: str):
    spans = get_lineage(trace_id)
    span_map = {s["span_id"]: s for s in spans}
    children_map: dict[str, list] = {}
    roots = []

    for s in spans:
        pid = s["parent_span_id"]
        if pid:
            children_map.setdefault(pid, []).append(s)
        else:
            roots.append(s)

    def print_node(span: dict, depth: int = 0):
        indent = "  " * depth
        dur = (span["end_ts"] - span["start_ts"]) * 1000 if span["end_ts"] else 0
        print(f"{indent}[{span['span_id'][:8]}] {span['agent_name']} — {dur:.0f}ms [{span['status']}]")
        print(f"{indent}  {span['tokens_in']}+{span['tokens_out']} tokens")
        for child in children_map.get(span["span_id"], []):
            print_node(child, depth + 1)

    print(f"\n=== LINEAGE: {trace_id[:8]} ===")
    for root in roots:
        print_node(root)

    total_tok = sum(s["tokens_in"] + s["tokens_out"] for s in spans)
    print(f"\nTotal spans: {len(spans)}, Total tokens: {total_tok}")

# Usage
trace_id = str(uuid.uuid4())

_, root_span = run_agent(trace_id, "Orchestrator", "Create a product launch plan")
_, span1 = run_agent(trace_id, "MarketResearcher", "Identify target market segments", root_span)
_, span2 = run_agent(trace_id, "PricingAnalyst", "Suggest pricing strategy", root_span)
run_agent(trace_id, "Copywriter", "Draft launch email", span1)
run_agent(trace_id, "QAReviewer", "Review pricing assumptions", span2)

print_lineage_tree(trace_id)

# Expected Token Savings: None — pure observability, SQLite enables persistent post-mortem debugging
# Environment: ANTHROPIC_API_KEY required, writes to /tmp/agent_lineage.db
```

### Option 4: Async Lineage with Concurrent Sub-Agents

```python
import anthropic
import asyncio
import uuid
import time
from dataclasses import dataclass, field
from typing import Optional

client = anthropic.AsyncAnthropic()

@dataclass
class LineageNode:
    span_id: str
    parent_id: Optional[str]
    agent_name: str
    task: str
    start_time: float
    end_time: float = 0.0
    output: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    children: list["LineageNode"] = field(default_factory=list)
    error: Optional[str] = None

class LineageTracker:
    def __init__(self, trace_id: str):
        self.trace_id = trace_id
        self.nodes: dict[str, LineageNode] = {}
        self._lock = asyncio.Lock()

    async def create_node(self, agent_name: str, task: str, parent_id: Optional[str] = None) -> str:
        span_id = str(uuid.uuid4())[:8]
        node = LineageNode(
            span_id=span_id,
            parent_id=parent_id,
            agent_name=agent_name,
            task=task[:200],
            start_time=time.time()
        )
        async with self._lock:
            self.nodes[span_id] = node
            if parent_id and parent_id in self.nodes:
                self.nodes[parent_id].children.append(node)
        return span_id

    async def complete_node(self, span_id: str, output: str, tokens_in: int, tokens_out: int):
        async with self._lock:
            node = self.nodes[span_id]
            node.end_time = time.time()
            node.output = output[:200]
            node.tokens_in = tokens_in
            node.tokens_out = tokens_out

    async def fail_node(self, span_id: str, error: str):
        async with self._lock:
            node = self.nodes[span_id]
            node.end_time = time.time()
            node.error = error

    def roots(self) -> list[LineageNode]:
        parent_ids = {n.parent_id for n in self.nodes.values() if n.parent_id}
        return [n for n in self.nodes.values() if n.span_id not in parent_ids or n.parent_id is None]

    def total_tokens(self) -> int:
        return sum(n.tokens_in + n.tokens_out for n in self.nodes.values())

    def critical_path_ms(self) -> float:
        """Find the longest sequential path (wall clock critical path)."""
        roots = [n for n in self.nodes.values() if n.parent_id is None]
        def depth_duration(node: LineageNode) -> float:
            dur = (node.end_time - node.start_time) * 1000
            if not node.children:
                return dur
            return dur + max(depth_duration(c) for c in node.children)
        return max((depth_duration(r) for r in roots), default=0.0)

async def run_agent(
    tracker: LineageTracker,
    agent_name: str,
    task: str,
    parent_id: Optional[str] = None
) -> tuple[str, str]:
    span_id = await tracker.create_node(agent_name, task, parent_id)
    try:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=150,
            system=f"You are {agent_name}. Respond concisely.",
            messages=[{"role": "user", "content": task}]
        )
        output = response.content[0].text
        await tracker.complete_node(span_id, output, response.usage.input_tokens, response.usage.output_tokens)
        return output, span_id
    except Exception as e:
        await tracker.fail_node(span_id, str(e))
        raise

def print_tree(node: LineageNode, depth: int = 0):
    indent = "  " * depth
    dur = (node.end_time - node.start_time) * 1000
    status = "ERR" if node.error else "OK"
    print(f"{indent}[{node.span_id}][{status}] {node.agent_name} — {dur:.0f}ms | {node.tokens_in}+{node.tokens_out}tok")
    for child in node.children:
        print_tree(child, depth + 1)

async def main():
    tracker = LineageTracker(trace_id=str(uuid.uuid4()))

    # Sequential: orchestrator first
    plan, orch_id = await run_agent(tracker, "Orchestrator", "Plan a competitive analysis for a new SaaS product")

    # Parallel: multiple sub-agents spawn concurrently
    results = await asyncio.gather(
        run_agent(tracker, "CompetitorResearcher", "List top 5 SaaS competitors", orch_id),
        run_agent(tracker, "PricingAnalyst", "Research SaaS pricing models", orch_id),
        run_agent(tracker, "FeatureAnalyst", "Identify key differentiating features", orch_id),
    )

    # Third level: aggregator depends on all three
    combined = " | ".join(r[0][:80] for r in results)
    parent_ids = [r[1] for r in results]
    # Attach to orchestrator for simplicity
    await run_agent(tracker, "Synthesizer", f"Synthesize findings: {combined[:200]}", orch_id)

    # Print lineage
    print(f"\n=== ASYNC LINEAGE (trace: {tracker.trace_id[:8]}) ===")
    roots = tracker.roots()
    for root in roots:
        print_tree(root)

    print(f"\nTotal tokens: {tracker.total_tokens()}")
    print(f"Critical path: {tracker.critical_path_ms():.0f}ms")

asyncio.run(main())

# Expected Token Savings: Parallel execution reduces latency ~3x vs sequential
# Environment: ANTHROPIC_API_KEY required, uses asyncio
```

### Option 5: OpenTelemetry-Compatible Lineage Export

```python
import anthropic
import uuid
import time
import json
from dataclasses import dataclass, field, asdict
from typing import Optional
from enum import Enum

client = anthropic.Anthropic()

class SpanStatus(str, Enum):
    UNSET = "UNSET"
    OK = "OK"
    ERROR = "ERROR"

@dataclass
class OtelSpan:
    """OpenTelemetry-compatible span structure."""
    trace_id: str
    span_id: str
    parent_span_id: Optional[str]
    name: str                          # agent_name
    kind: str = "INTERNAL"
    start_time_unix_nano: int = 0
    end_time_unix_nano: int = 0
    attributes: dict = field(default_factory=dict)
    events: list[dict] = field(default_factory=list)
    status: SpanStatus = SpanStatus.UNSET
    status_message: str = ""

    def add_event(self, name: str, attrs: dict = None):
        self.events.append({
            "name": name,
            "time_unix_nano": time.time_ns(),
            "attributes": attrs or {}
        })

    def to_otel_json(self) -> dict:
        return {
            "traceId": self.trace_id.replace("-", ""),
            "spanId": self.span_id,
            "parentSpanId": self.parent_span_id or "",
            "name": self.name,
            "kind": self.kind,
            "startTimeUnixNano": str(self.start_time_unix_nano),
            "endTimeUnixNano": str(self.end_time_unix_nano),
            "attributes": [{"key": k, "value": {"stringValue": str(v)}} for k, v in self.attributes.items()],
            "events": self.events,
            "status": {"code": self.status.value, "message": self.status_message}
        }

class OtelLineageExporter:
    def __init__(self):
        self.spans: list[OtelSpan] = []

    def start_span(self, name: str, trace_id: str, parent_id: Optional[str] = None, attrs: dict = None) -> OtelSpan:
        span = OtelSpan(
            trace_id=trace_id,
            span_id=str(uuid.uuid4()).replace("-", "")[:16],
            parent_span_id=parent_id,
            name=name,
            start_time_unix_nano=time.time_ns(),
            attributes=attrs or {}
        )
        self.spans.append(span)
        return span

    def end_span(self, span: OtelSpan, status: SpanStatus = SpanStatus.OK, message: str = ""):
        span.end_time_unix_nano = time.time_ns()
        span.status = status
        span.status_message = message

    def export_json(self) -> str:
        """Export in OTLP JSON format."""
        resource_spans = {
            "resourceSpans": [{
                "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "multi-agent-pipeline"}}]},
                "scopeSpans": [{
                    "scope": {"name": "anthropic-agent-lineage"},
                    "spans": [s.to_otel_json() for s in self.spans]
                }]
            }]
        }
        return json.dumps(resource_spans, indent=2)

    def print_summary(self):
        print(f"\n=== OTEL LINEAGE EXPORT ({len(self.spans)} spans) ===")
        for s in sorted(self.spans, key=lambda x: x.start_time_unix_nano):
            dur_ms = (s.end_time_unix_nano - s.start_time_unix_nano) / 1e6
            parent = s.parent_span_id[:8] if s.parent_span_id else "ROOT"
            tokens = s.attributes.get("llm.tokens.total", "?")
            print(f"  [{s.span_id[:8]}] ← {parent} | {s.name} | {dur_ms:.0f}ms | {tokens}tok [{s.status.value}]")

exporter = OtelLineageExporter()

def run_agent(agent_name: str, task: str, trace_id: str, parent_span_id: Optional[str] = None) -> tuple[str, str]:
    span = exporter.start_span(
        name=agent_name,
        trace_id=trace_id,
        parent_id=parent_span_id,
        attrs={
            "agent.name": agent_name,
            "agent.task": task[:100],
            "llm.model": "claude-haiku-4-5-20251001"
        }
    )
    span.add_event("agent.start", {"task.length": len(task)})

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=150,
            system=f"You are {agent_name}.",
            messages=[{"role": "user", "content": task}]
        )
        output = response.content[0].text
        span.attributes["llm.tokens.input"] = response.usage.input_tokens
        span.attributes["llm.tokens.output"] = response.usage.output_tokens
        span.attributes["llm.tokens.total"] = response.usage.input_tokens + response.usage.output_tokens
        span.add_event("agent.complete", {"output.length": len(output)})
        exporter.end_span(span, SpanStatus.OK)
        return output, span.span_id

    except Exception as e:
        span.add_event("agent.error", {"error": str(e)})
        exporter.end_span(span, SpanStatus.ERROR, str(e))
        raise

# Usage
trace_id = str(uuid.uuid4()).replace("-", "")
out1, id1 = run_agent("PlannerAgent", "Outline a customer onboarding workflow", trace_id)
out2, id2 = run_agent("DocumentAgent", f"Write step 1 docs: {out1[:80]}", trace_id, id1)
out3, id3 = run_agent("ReviewAgent", f"Review: {out2[:80]}", trace_id, id2)
run_agent("PublisherAgent", f"Publish docs: {out3[:80]}", trace_id, id1)

exporter.print_summary()
# Uncomment to write OTLP JSON (importable into Jaeger, Tempo, etc.)
# with open("/tmp/traces.json", "w") as f: f.write(exporter.export_json())

# Expected Token Savings: None — OTEL-compatible export enables integration with Jaeger/Grafana Tempo
# Environment: ANTHROPIC_API_KEY required; export JSON compatible with OTLP receivers
```

### Option 6: Lineage with Cost Attribution and Budget Enforcement

```python
import anthropic
import uuid
import time
from dataclasses import dataclass, field
from typing import Optional

client = anthropic.Anthropic()

# Pricing per 1M tokens (approximate)
MODEL_COSTS = {
    "claude-haiku-4-5-20251001": {"input": 0.25, "output": 1.25},
    "claude-sonnet-4-6":         {"input": 3.00, "output": 15.00},
    "claude-opus-4-6":           {"input": 15.00, "output": 75.00},
}

def estimate_cost(model: str, tokens_in: int, tokens_out: int) -> float:
    prices = MODEL_COSTS.get(model, {"input": 3.0, "output": 15.0})
    return (tokens_in * prices["input"] + tokens_out * prices["output"]) / 1_000_000

@dataclass
class CostNode:
    span_id: str
    parent_id: Optional[str]
    agent_name: str
    model: str
    task: str
    tokens_in: int = 0
    tokens_out: int = 0
    direct_cost_usd: float = 0.0
    subtree_cost_usd: float = 0.0
    children: list["CostNode"] = field(default_factory=list)
    duration_ms: float = 0.0
    output: str = ""

class CostAttributionTracker:
    def __init__(self, trace_id: str, budget_usd: float = 0.10):
        self.trace_id = trace_id
        self.budget_usd = budget_usd
        self.nodes: dict[str, CostNode] = {}
        self.total_cost = 0.0

    def check_budget(self):
        if self.total_cost > self.budget_usd:
            raise RuntimeError(
                f"Budget exceeded: ${self.total_cost:.4f} > ${self.budget_usd:.4f}. "
                f"Halt agent pipeline."
            )

    def run_agent(
        self,
        agent_name: str,
        task: str,
        parent_id: Optional[str] = None,
        model: str = "claude-haiku-4-5-20251001"
    ) -> tuple[str, str]:
        self.check_budget()

        span_id = str(uuid.uuid4())[:8]
        t0 = time.time()

        response = client.messages.create(
            model=model,
            max_tokens=200,
            system=f"You are {agent_name}. Be concise.",
            messages=[{"role": "user", "content": task}]
        )
        output = response.content[0].text
        t1 = time.time()

        cost = estimate_cost(model, response.usage.input_tokens, response.usage.output_tokens)
        self.total_cost += cost

        node = CostNode(
            span_id=span_id,
            parent_id=parent_id,
            agent_name=agent_name,
            model=model,
            task=task[:100],
            tokens_in=response.usage.input_tokens,
            tokens_out=response.usage.output_tokens,
            direct_cost_usd=cost,
            duration_ms=(t1 - t0) * 1000,
            output=output[:100]
        )
        self.nodes[span_id] = node

        if parent_id and parent_id in self.nodes:
            self.nodes[parent_id].children.append(node)

        return output, span_id

    def compute_subtree_costs(self):
        """Propagate child costs up to parents."""
        def _compute(node: CostNode) -> float:
            child_cost = sum(_compute(c) for c in node.children)
            node.subtree_cost_usd = node.direct_cost_usd + child_cost
            return node.subtree_cost_usd

        roots = [n for n in self.nodes.values() if n.parent_id is None]
        for root in roots:
            _compute(root)

    def print_cost_report(self):
        self.compute_subtree_costs()
        roots = [n for n in self.nodes.values() if n.parent_id is None]

        def print_node(node: CostNode, depth: int = 0):
            indent = "  " * depth
            budget_pct = node.subtree_cost_usd / self.budget_usd * 100
            print(f"{indent}[{node.span_id}] {node.agent_name} ({node.model.split('-')[1]})")
            print(f"{indent}  Direct: ${node.direct_cost_usd:.5f} | Subtree: ${node.subtree_cost_usd:.5f} ({budget_pct:.1f}% of budget)")
            print(f"{indent}  Tokens: {node.tokens_in}in/{node.tokens_out}out | {node.duration_ms:.0f}ms")
            for child in node.children:
                print_node(child, depth + 1)

        print(f"\n=== COST ATTRIBUTION (budget: ${self.budget_usd:.3f}) ===")
        for root in roots:
            print_node(root)
        budget_used = self.total_cost / self.budget_usd * 100
        print(f"\nTotal: ${self.total_cost:.5f} ({budget_used:.1f}% of budget)")

# Usage
tracker = CostAttributionTracker(trace_id=str(uuid.uuid4()), budget_usd=0.05)

try:
    out1, id1 = tracker.run_agent("Orchestrator", "Plan a 3-step data pipeline", model="claude-haiku-4-5-20251001")
    out2, id2 = tracker.run_agent("DataEngineer", "Design ingestion step", id1, model="claude-haiku-4-5-20251001")
    out3, id3 = tracker.run_agent("DataEngineer", "Design transform step", id1, model="claude-haiku-4-5-20251001")
    out4, id4 = tracker.run_agent("Reviewer", f"Review pipeline design: {out2[:80]}", id1, model="claude-sonnet-4-6")
except RuntimeError as e:
    print(f"BUDGET HALT: {e}")

tracker.print_cost_report()

# Expected Token Savings: Budget enforcement stops runaway pipelines; cost attribution guides model selection
# Environment: ANTHROPIC_API_KEY required; costs are estimates based on public pricing
```

## Comparison

| Option | Persistence | Export Format | Async | Cost Tracking | Best Use Case |
|--------|------------|---------------|-------|--------------|---------------|
| Simple Parent-Child Tree | In-memory | Console | No | No | Development debugging |
| Distributed Trace Context | In-memory | Console | No | No | Context propagation pattern |
| SQLite-Persisted | SQLite | SQL queryable | No | No | Production post-mortem replay |
| Async with Concurrent Agents | In-memory | Console | Yes | No | Parallel agent pipelines |
| OTel-Compatible Export | In-memory | OTLP JSON | No | No | Jaeger/Grafana integration |
| Cost Attribution + Budget | In-memory | Console | No | Yes | Budget-constrained pipelines |
