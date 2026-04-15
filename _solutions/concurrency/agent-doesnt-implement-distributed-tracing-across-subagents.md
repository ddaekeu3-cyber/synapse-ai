---
layout: solution
title: "Agent Doesn't Implement Distributed Tracing Across Subagents"
category: concurrency
description: "How to propagate trace context across parallel and sequential subagent calls so you can reconstruct end-to-end execution timelines, identify bottlenecks, and debug failures in multi-agent systems."
tags: [concurrency, tracing, observability, subagents, distributed, debugging]
---

# Agent Doesn't Implement Distributed Tracing Across Subagents

When an orchestrator spawns parallel subagents, each subagent's logs appear in isolation — there's no way to correlate which calls belong to the same root request, measure cross-agent latency, or identify which subagent caused a failure. Distributed tracing propagates a trace ID and span hierarchy across every agent invocation so the full execution tree is reconstructable from logs.

## Option 1: Simple Trace ID Propagation via System Prompt

Inject a trace ID into each subagent's system prompt. Subagents include it in their responses so the orchestrator can correlate results.

```python
import anthropic
import asyncio
import uuid
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class TraceSpan:
    trace_id: str
    span_id: str
    parent_span_id: Optional[str]
    agent_name: str
    start_time: float
    end_time: Optional[float] = None
    status: str = "running"    # "running", "success", "error"
    token_usage: dict = field(default_factory=dict)

    @property
    def duration_ms(self) -> float:
        if self.end_time:
            return (self.end_time - self.start_time) * 1000
        return 0.0


class TraceContext:
    def __init__(self, trace_id: Optional[str] = None):
        self.trace_id = trace_id or str(uuid.uuid4())[:8]
        self.spans: list[TraceSpan] = []

    def start_span(self, agent_name: str, parent_span_id: Optional[str] = None) -> TraceSpan:
        span = TraceSpan(
            trace_id=self.trace_id,
            span_id=str(uuid.uuid4())[:8],
            parent_span_id=parent_span_id,
            agent_name=agent_name,
            start_time=time.monotonic(),
        )
        self.spans.append(span)
        return span

    def finish_span(self, span: TraceSpan, status: str = "success", usage: dict = None):
        span.end_time = time.monotonic()
        span.status = status
        span.token_usage = usage or {}

    def print_trace(self):
        print(f"\n[TRACE {self.trace_id}] Execution tree:")
        root_spans = [s for s in self.spans if s.parent_span_id is None]
        self._print_span_tree(root_spans, "", 0)

    def _print_span_tree(self, spans: list, prefix: str, depth: int):
        for span in spans:
            tokens = span.token_usage.get("total", 0)
            print(f"{prefix}{'└─' if depth else '●'} [{span.agent_name}] "
                  f"span={span.span_id} {span.duration_ms:.0f}ms {span.status} {tokens}tok")
            children = [s for s in self.spans if s.parent_span_id == span.span_id]
            self._print_span_tree(children, prefix + "  ", depth + 1)


def build_traced_system_prompt(base_prompt: str, span: TraceSpan) -> str:
    return (
        f"{base_prompt}\n\n"
        f"[TRACE] trace_id={span.trace_id} span_id={span.span_id} agent={span.agent_name}"
    )


async def run_subagent(
    client: anthropic.AsyncAnthropic,
    agent_name: str,
    prompt: str,
    trace: TraceContext,
    parent_span_id: Optional[str] = None,
) -> tuple[str, TraceSpan]:
    span = trace.start_span(agent_name, parent_span_id)
    system = build_traced_system_prompt(f"You are the {agent_name} agent.", span)

    try:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        output = response.content[0].text
        trace.finish_span(span, "success", {
            "input": response.usage.input_tokens,
            "output": response.usage.output_tokens,
            "total": response.usage.input_tokens + response.usage.output_tokens,
        })
        return output, span
    except Exception as e:
        trace.finish_span(span, "error")
        raise


async def orchestrated_pipeline(question: str) -> str:
    client = anthropic.AsyncAnthropic()
    trace = TraceContext()

    # Root orchestrator span
    root_span = trace.start_span("orchestrator")

    # Parallel subagents
    results = await asyncio.gather(
        run_subagent(client, "researcher", f"Research: {question}", trace, root_span.span_id),
        run_subagent(client, "critic",     f"Identify gaps in answering: {question}", trace, root_span.span_id),
        run_subagent(client, "summarizer", f"Plan a concise answer to: {question}", trace, root_span.span_id),
    )

    research, research_span = results[0]
    critique, _ = results[1]
    summary_plan, _ = results[2]

    # Sequential synthesis
    synthesis_result, _ = await run_subagent(
        client, "synthesizer",
        f"Combine: Research='{research[:100]}' Critique='{critique[:100]}' Plan='{summary_plan[:100]}'",
        trace, root_span.span_id,
    )

    trace.finish_span(root_span, "success")
    trace.print_trace()
    return synthesis_result


if __name__ == "__main__":
    result = asyncio.run(orchestrated_pipeline("What are the key benefits of async programming?"))
    print(f"\nFinal answer: {result[:200]}")

# Expected Token Savings: Tracing reveals redundant subagent calls; bottleneck visibility enables targeted optimization
# Environment: Multi-agent orchestrators, parallel research pipelines, any system with >2 coordinated agents
```

## Option 2: Structured Span Context with SQLite Persistence

Persist all spans to SQLite so traces survive process restarts and can be queried after the fact.

```python
import anthropic
import asyncio
import sqlite3
import uuid
import time
import json
from dataclasses import dataclass, field
from typing import Optional, ContextManager
from contextlib import contextmanager


DB_PATH = "agent_traces.db"


def init_trace_db(db_path: str = DB_PATH) -> sqlite3.Connection:
    db = sqlite3.connect(db_path)
    db.execute("""
        CREATE TABLE IF NOT EXISTS spans (
            span_id TEXT PRIMARY KEY,
            trace_id TEXT NOT NULL,
            parent_span_id TEXT,
            agent_name TEXT NOT NULL,
            operation TEXT,
            start_time REAL NOT NULL,
            end_time REAL,
            status TEXT DEFAULT 'running',
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            metadata TEXT DEFAULT '{}'
        )
    """)
    db.execute("CREATE INDEX IF NOT EXISTS idx_trace ON spans(trace_id)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_parent ON spans(parent_span_id)")
    db.commit()
    return db


@dataclass
class PersistentSpan:
    span_id: str
    trace_id: str
    agent_name: str
    db: sqlite3.Connection
    parent_span_id: Optional[str] = None
    operation: str = "llm_call"

    def __enter__(self):
        self.db.execute("""
            INSERT INTO spans (span_id, trace_id, parent_span_id, agent_name, operation, start_time)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (self.span_id, self.trace_id, self.parent_span_id, self.agent_name, self.operation, time.time()))
        self.db.commit()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        status = "error" if exc_type else "success"
        self.db.execute("""
            UPDATE spans SET end_time=?, status=? WHERE span_id=?
        """, (time.time(), status, self.span_id))
        self.db.commit()

    def record_usage(self, input_tokens: int, output_tokens: int, metadata: dict = None):
        self.db.execute("""
            UPDATE spans SET input_tokens=?, output_tokens=?, metadata=? WHERE span_id=?
        """, (input_tokens, output_tokens, json.dumps(metadata or {}), self.span_id))
        self.db.commit()


def new_span(db: sqlite3.Connection, trace_id: str, agent_name: str,
             parent_span_id: Optional[str] = None, operation: str = "llm_call") -> PersistentSpan:
    return PersistentSpan(
        span_id=str(uuid.uuid4())[:12],
        trace_id=trace_id,
        agent_name=agent_name,
        db=db,
        parent_span_id=parent_span_id,
        operation=operation,
    )


def print_trace_from_db(db: sqlite3.Connection, trace_id: str):
    spans = db.execute("""
        SELECT span_id, parent_span_id, agent_name, status,
               ROUND((end_time - start_time) * 1000) as duration_ms,
               input_tokens + output_tokens as total_tokens
        FROM spans WHERE trace_id=? ORDER BY start_time
    """, (trace_id,)).fetchall()

    print(f"\n[TRACE {trace_id}]")
    span_map = {row[0]: row for row in spans}
    roots = [r for r in spans if r[1] is None]

    def print_tree(rows, prefix="", depth=0):
        for row in rows:
            span_id, _, agent, status, dur, tokens = row
            dur_str = f"{dur:.0f}ms" if dur else "running"
            print(f"{prefix}{'└─' if depth else '●'} {agent} [{status}] {dur_str} {tokens or 0}tok")
            children = [r for r in spans if r[1] == span_id]
            print_tree(children, prefix + "  ", depth + 1)

    print_tree(roots)


async def traced_subagent(
    client: anthropic.AsyncAnthropic,
    db: sqlite3.Connection,
    trace_id: str,
    parent_span_id: Optional[str],
    agent_name: str,
    prompt: str,
) -> tuple[str, str]:
    """Returns (output, span_id)."""
    with new_span(db, trace_id, agent_name, parent_span_id) as span:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=150,
            messages=[{"role": "user", "content": prompt}],
        )
        output = response.content[0].text
        span.record_usage(
            response.usage.input_tokens,
            response.usage.output_tokens,
            {"prompt_preview": prompt[:50]},
        )
        return output, span.span_id


async def multi_agent_with_db_tracing(goal: str) -> str:
    client = anthropic.AsyncAnthropic()
    db = init_trace_db(":memory:")
    trace_id = str(uuid.uuid4())[:8]

    with new_span(db, trace_id, "orchestrator") as root:
        # Fan-out to 3 parallel subagents
        outputs = await asyncio.gather(
            traced_subagent(client, db, trace_id, root.span_id, "agent-A", f"Perspective A on: {goal}"),
            traced_subagent(client, db, trace_id, root.span_id, "agent-B", f"Perspective B on: {goal}"),
            traced_subagent(client, db, trace_id, root.span_id, "agent-C", f"Perspective C on: {goal}"),
        )

        perspectives = "\n".join(out for out, _ in outputs)

        # Synthesize
        synth_out, _ = await traced_subagent(
            client, db, trace_id, root.span_id, "synthesizer",
            f"Synthesize into 2 sentences:\n{perspectives[:300]}",
        )

    print_trace_from_db(db, trace_id)
    return synth_out


if __name__ == "__main__":
    result = asyncio.run(multi_agent_with_db_tracing("What makes a good API design?"))
    print(f"\nFinal: {result[:200]}")

# Expected Token Savings: Post-hoc trace analysis identifies redundant agents that can be removed
# Environment: Production multi-agent systems needing audit trails, post-mortem debugging
```

## Option 3: OpenTelemetry-Compatible Span Export

Emit spans in OTLP-compatible format for ingestion by Jaeger, Tempo, or any OpenTelemetry backend.

```python
import anthropic
import asyncio
import uuid
import time
import json
from dataclasses import dataclass, field
from typing import Optional
from collections import defaultdict


@dataclass
class OTelSpan:
    """OpenTelemetry-compatible span representation."""
    trace_id: str
    span_id: str
    parent_span_id: Optional[str]
    name: str
    start_time_unix_nano: int
    end_time_unix_nano: Optional[int] = None
    status_code: str = "UNSET"   # "OK", "ERROR", "UNSET"
    attributes: dict = field(default_factory=dict)
    events: list = field(default_factory=list)

    def to_otel_dict(self) -> dict:
        return {
            "traceId": self.trace_id,
            "spanId": self.span_id,
            "parentSpanId": self.parent_span_id or "",
            "name": self.name,
            "startTimeUnixNano": str(self.start_time_unix_nano),
            "endTimeUnixNano": str(self.end_time_unix_nano or 0),
            "status": {"code": self.status_code},
            "attributes": [
                {"key": k, "value": {"stringValue": str(v)}}
                for k, v in self.attributes.items()
            ],
            "events": self.events,
        }


class OTelTracer:
    def __init__(self, service_name: str = "agent-service"):
        self.service_name = service_name
        self.spans: list[OTelSpan] = []
        self._active: dict[str, OTelSpan] = {}

    def _now_ns(self) -> int:
        return int(time.time() * 1e9)

    def start_span(
        self,
        name: str,
        trace_id: Optional[str] = None,
        parent_span_id: Optional[str] = None,
        attributes: dict = None,
    ) -> OTelSpan:
        span = OTelSpan(
            trace_id=trace_id or uuid.uuid4().hex[:16],
            span_id=uuid.uuid4().hex[:16],
            parent_span_id=parent_span_id,
            name=name,
            start_time_unix_nano=self._now_ns(),
            attributes={
                "service.name": self.service_name,
                **(attributes or {}),
            },
        )
        self.spans.append(span)
        self._active[span.span_id] = span
        return span

    def end_span(
        self,
        span: OTelSpan,
        status: str = "OK",
        extra_attrs: dict = None,
    ):
        span.end_time_unix_nano = self._now_ns()
        span.status_code = status
        if extra_attrs:
            span.attributes.update(extra_attrs)
        self._active.pop(span.span_id, None)

    def add_event(self, span: OTelSpan, name: str, attributes: dict = None):
        span.events.append({
            "name": name,
            "timeUnixNano": str(self._now_ns()),
            "attributes": attributes or {},
        })

    def export_json(self) -> str:
        """Export all spans as OTLP JSON."""
        resource_spans = {
            "resourceSpans": [{
                "resource": {"attributes": [
                    {"key": "service.name", "value": {"stringValue": self.service_name}}
                ]},
                "scopeSpans": [{
                    "scope": {"name": "agent-tracer"},
                    "spans": [s.to_otel_dict() for s in self.spans],
                }],
            }]
        }
        return json.dumps(resource_spans, indent=2)

    def print_summary(self):
        total_ms = sum(
            (s.end_time_unix_nano - s.start_time_unix_nano) / 1e6
            for s in self.spans if s.end_time_unix_nano
        )
        by_name: dict = defaultdict(list)
        for s in self.spans:
            if s.end_time_unix_nano:
                dur = (s.end_time_unix_nano - s.start_time_unix_nano) / 1e6
                by_name[s.name].append(dur)

        print(f"\n[OTEL TRACE SUMMARY] Service: {self.service_name}")
        print(f"  Total spans: {len(self.spans)}")
        for name, durs in sorted(by_name.items()):
            avg = sum(durs) / len(durs)
            print(f"  {name}: {len(durs)}x avg={avg:.0f}ms")


tracer = OTelTracer("research-agent")


async def otel_traced_call(
    client: anthropic.AsyncAnthropic,
    span_name: str,
    prompt: str,
    trace_id: str,
    parent_span_id: Optional[str],
    attributes: dict = None,
) -> tuple[str, OTelSpan]:
    span = tracer.start_span(
        name=span_name,
        trace_id=trace_id,
        parent_span_id=parent_span_id,
        attributes={"llm.model": "claude-haiku-4-5-20251001", **(attributes or {})},
    )

    try:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=150,
            messages=[{"role": "user", "content": prompt}],
        )
        output = response.content[0].text
        tracer.add_event(span, "llm.response", {
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
        })
        tracer.end_span(span, "OK", {
            "llm.input_tokens": str(response.usage.input_tokens),
            "llm.output_tokens": str(response.usage.output_tokens),
        })
        return output, span
    except Exception as e:
        tracer.end_span(span, "ERROR", {"error.message": str(e)})
        raise


async def otel_multi_agent(question: str) -> str:
    client = anthropic.AsyncAnthropic()
    trace_id = uuid.uuid4().hex[:16]

    root = tracer.start_span("orchestrator.run", trace_id=trace_id, attributes={"query": question[:80]})

    results = await asyncio.gather(
        otel_traced_call(client, "agent.search", f"Find facts about: {question}", trace_id, root.span_id),
        otel_traced_call(client, "agent.reason", f"Reason about: {question}", trace_id, root.span_id),
    )

    facts, facts_span = results[0]
    reasoning, _ = results[1]

    final, _ = await otel_traced_call(
        client, "agent.synthesize",
        f"Combine facts='{facts[:80]}' and reasoning='{reasoning[:80]}'",
        trace_id, root.span_id,
    )

    tracer.end_span(root, "OK")
    tracer.print_summary()

    # Could POST to OTLP endpoint: requests.post("http://otel-collector:4318/v1/traces", data=tracer.export_json())
    return final


if __name__ == "__main__":
    result = asyncio.run(otel_multi_agent("How does consensus work in distributed systems?"))
    print(f"\nAnswer: {result[:200]}")

# Expected Token Savings: Trace data enables latency optimization — cutting slow agents saves their token cost entirely
# Environment: Production systems with OpenTelemetry infrastructure (Jaeger, Grafana Tempo, Datadog)
```

## Option 4: Causal Chain Logging — Reconstruct Why Each Subagent Was Spawned

Log not just timing but the causal chain — which agent's output triggered which downstream agent invocation.

```python
import anthropic
import asyncio
import uuid
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class CausalLink:
    from_agent: str
    from_output_preview: str
    to_agent: str
    reason: str
    spawned_at: float


@dataclass
class AgentNode:
    agent_id: str
    agent_name: str
    prompt: str
    output: str = ""
    parent_id: Optional[str] = None
    spawn_reason: str = "root"
    start_time: float = field(default_factory=time.monotonic)
    end_time: Optional[float] = None
    children: list = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0


class CausalTraceTree:
    def __init__(self):
        self.nodes: dict[str, AgentNode] = {}
        self.causal_links: list[CausalLink] = []
        self.root_id: Optional[str] = None

    def register_agent(
        self,
        agent_name: str,
        prompt: str,
        parent_id: Optional[str] = None,
        spawn_reason: str = "root",
    ) -> AgentNode:
        node = AgentNode(
            agent_id=str(uuid.uuid4())[:8],
            agent_name=agent_name,
            prompt=prompt,
            parent_id=parent_id,
            spawn_reason=spawn_reason,
        )
        self.nodes[node.agent_id] = node

        if parent_id:
            parent = self.nodes.get(parent_id)
            if parent:
                parent.children.append(node.agent_id)
                self.causal_links.append(CausalLink(
                    from_agent=parent.agent_name,
                    from_output_preview=parent.output[:60],
                    to_agent=agent_name,
                    reason=spawn_reason,
                    spawned_at=time.monotonic(),
                ))
        else:
            self.root_id = node.agent_id

        return node

    def complete_agent(self, node: AgentNode, output: str, input_tokens: int, output_tokens: int):
        node.output = output
        node.end_time = time.monotonic()
        node.input_tokens = input_tokens
        node.output_tokens = output_tokens

    def print_causal_tree(self):
        if not self.root_id:
            return
        print("\n[CAUSAL TRACE]")
        self._print_node(self.root_id, "", 0)
        print("\n[CAUSAL LINKS]")
        for link in self.causal_links:
            print(f"  {link.from_agent} → {link.to_agent}: '{link.reason}'")

    def _print_node(self, node_id: str, prefix: str, depth: int):
        node = self.nodes[node_id]
        dur = f"{(node.end_time - node.start_time)*1000:.0f}ms" if node.end_time else "running"
        tokens = node.input_tokens + node.output_tokens
        print(f"{prefix}{'└─' if depth else '●'} [{node.agent_name}] {dur} {tokens}tok")
        print(f"{prefix}  reason: {node.spawn_reason}")
        print(f"{prefix}  output: {node.output[:60]}...")
        for child_id in node.children:
            self._print_node(child_id, prefix + "  ", depth + 1)


causal_tree = CausalTraceTree()


async def run_causal_agent(
    client: anthropic.AsyncAnthropic,
    agent_name: str,
    prompt: str,
    parent_id: Optional[str],
    spawn_reason: str,
) -> AgentNode:
    node = causal_tree.register_agent(agent_name, prompt, parent_id, spawn_reason)

    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=150,
        messages=[{"role": "user", "content": prompt}],
    )
    output = response.content[0].text
    causal_tree.complete_agent(node, output, response.usage.input_tokens, response.usage.output_tokens)
    return node


async def causal_orchestration(question: str) -> str:
    client = anthropic.AsyncAnthropic()

    # Root: planner decides what subagents to spawn
    planner = await run_causal_agent(
        client, "planner",
        f"List 2 specific research directions to answer: {question}",
        parent_id=None,
        spawn_reason="root",
    )

    # Spawn researchers based on planner output
    research_tasks = await asyncio.gather(
        run_causal_agent(
            client, "researcher-1",
            f"Research direction 1 for: {question}\nContext: {planner.output[:100]}",
            parent_id=planner.agent_id,
            spawn_reason=f"planner identified first research direction",
        ),
        run_causal_agent(
            client, "researcher-2",
            f"Research direction 2 for: {question}\nContext: {planner.output[:100]}",
            parent_id=planner.agent_id,
            spawn_reason=f"planner identified second research direction",
        ),
    )

    # Synthesizer spawned because both researchers completed
    combined = "\n".join(r.output[:100] for r in research_tasks)
    synthesizer = await run_causal_agent(
        client, "synthesizer",
        f"Combine findings into final answer for: {question}\n\nFindings:\n{combined}",
        parent_id=planner.agent_id,
        spawn_reason="both researchers completed — combining results",
    )

    causal_tree.print_causal_tree()
    return synthesizer.output


if __name__ == "__main__":
    result = asyncio.run(causal_orchestration("What are the tradeoffs of microservices vs monoliths?"))
    print(f"\nFinal: {result[:200]}")

# Expected Token Savings: Causal visibility reveals spawn decisions that create unnecessary agents, eliminating them
# Environment: Complex orchestrators where understanding why agents were spawned is critical for debugging
```

## Option 5: Sampling-Based Trace Capture — Only Record Slow or Failing Traces

For high-throughput systems, only capture full traces for requests that are slow, fail, or exceed token budgets.

```python
import anthropic
import asyncio
import uuid
import time
import random
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class SamplingConfig:
    base_sample_rate: float = 0.05      # 5% random sampling
    slow_threshold_ms: float = 3000.0   # Always trace if > 3s
    error_sample_rate: float = 1.0      # Always trace errors
    high_token_threshold: int = 2000    # Always trace high-token requests


@dataclass
class SampledTrace:
    trace_id: str
    sampled: bool
    reason: str     # "random", "slow", "error", "high_tokens", "not_sampled"
    spans: list = field(default_factory=list)
    total_duration_ms: float = 0.0
    total_tokens: int = 0
    had_error: bool = False


class SamplingTracer:
    def __init__(self, config: SamplingConfig):
        self.config = config
        self.traces: list[SampledTrace] = []
        self._pending: dict[str, SampledTrace] = {}

    def start_trace(self) -> SampledTrace:
        trace = SampledTrace(
            trace_id=str(uuid.uuid4())[:8],
            sampled=False,  # Determined at end
            reason="pending",
        )
        self._pending[trace.trace_id] = trace
        return trace

    def record_span(self, trace: SampledTrace, name: str, duration_ms: float, tokens: int, error: bool = False):
        trace.spans.append({"name": name, "duration_ms": duration_ms, "tokens": tokens, "error": error})
        trace.total_duration_ms += duration_ms
        trace.total_tokens += tokens
        if error:
            trace.had_error = True

    def finalize_trace(self, trace: SampledTrace):
        cfg = self.config
        reason = "not_sampled"
        sampled = False

        if trace.had_error and random.random() < cfg.error_sample_rate:
            reason, sampled = "error", True
        elif trace.total_duration_ms > cfg.slow_threshold_ms:
            reason, sampled = "slow", True
        elif trace.total_tokens > cfg.high_token_threshold:
            reason, sampled = "high_tokens", True
        elif random.random() < cfg.base_sample_rate:
            reason, sampled = "random", True

        trace.sampled = sampled
        trace.reason = reason
        self._pending.pop(trace.trace_id, None)

        if sampled:
            self.traces.append(trace)
            print(f"[SAMPLED] trace={trace.trace_id} reason={reason} "
                  f"dur={trace.total_duration_ms:.0f}ms tokens={trace.total_tokens}")
        return sampled

    def stats(self) -> dict:
        if not self.traces:
            return {}
        by_reason = {}
        for t in self.traces:
            by_reason[t.reason] = by_reason.get(t.reason, 0) + 1
        return {"sampled_traces": len(self.traces), "by_reason": by_reason}


sampler = SamplingTracer(SamplingConfig(base_sample_rate=0.1, slow_threshold_ms=500))


async def sampled_agent_call(
    client: anthropic.AsyncAnthropic,
    trace: SampledTrace,
    agent_name: str,
    prompt: str,
) -> str:
    start = time.monotonic()
    try:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=100,
            messages=[{"role": "user", "content": prompt}],
        )
        duration = (time.monotonic() - start) * 1000
        tokens = response.usage.input_tokens + response.usage.output_tokens
        sampler.record_span(trace, agent_name, duration, tokens)
        return response.content[0].text
    except Exception as e:
        duration = (time.monotonic() - start) * 1000
        sampler.record_span(trace, agent_name, duration, 0, error=True)
        raise


async def process_request(question: str) -> str:
    client = anthropic.AsyncAnthropic()
    trace = sampler.start_trace()

    outputs = await asyncio.gather(
        sampled_agent_call(client, trace, "agent-A", f"Answer briefly: {question}"),
        sampled_agent_call(client, trace, "agent-B", f"Add one detail: {question}"),
    )

    combined = " ".join(outputs)
    sampler.finalize_trace(trace)
    return combined


async def main():
    questions = [f"What is {topic}?" for topic in
                 ["async programming", "REST", "GraphQL", "WebSockets", "gRPC",
                  "microservices", "Docker", "Kubernetes", "Redis", "PostgreSQL"]]

    for q in questions:
        result = await process_request(q)
        print(f"Q: {q[:40]} → {result[:50]}")

    print(f"\nSampling stats: {sampler.stats()}")


if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: 90-95% reduction in tracing overhead vs. always-on; captures the traces that actually matter
# Environment: High-throughput production systems where always-on tracing is too expensive
```

## Option 6: Trace Aggregation Dashboard — Cross-Agent Token and Latency Report

Aggregate spans from all agents into a performance dashboard showing per-agent token costs and latency percentiles.

```python
import anthropic
import asyncio
import uuid
import time
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class SpanRecord:
    agent_name: str
    trace_id: str
    duration_ms: float
    input_tokens: int
    output_tokens: int
    success: bool


class AgentPerformanceDashboard:
    def __init__(self):
        self.records: list[SpanRecord] = []

    def record(self, agent_name: str, trace_id: str, duration_ms: float,
               input_tokens: int, output_tokens: int, success: bool = True):
        self.records.append(SpanRecord(
            agent_name=agent_name,
            trace_id=trace_id,
            duration_ms=duration_ms,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            success=success,
        ))

    def report(self):
        by_agent: dict = defaultdict(list)
        for r in self.records:
            by_agent[r.agent_name].append(r)

        total_cost = 0.0
        print("\n[PERFORMANCE DASHBOARD]")
        print(f"{'Agent':<20} {'Calls':>6} {'P50ms':>8} {'P95ms':>8} {'AvgTok':>8} {'Cost$':>10} {'ErrRate':>8}")
        print("-" * 72)

        for agent_name, records in sorted(by_agent.items()):
            durations = sorted(r.duration_ms for r in records)
            p50 = statistics.median(durations)
            p95 = durations[int(len(durations) * 0.95)] if len(durations) > 1 else durations[0]
            avg_tokens = statistics.mean(r.input_tokens + r.output_tokens for r in records)
            # Blended Sonnet rate
            cost = sum((r.input_tokens * 0.000003 + r.output_tokens * 0.000015) for r in records)
            total_cost += cost
            error_rate = sum(1 for r in records if not r.success) / len(records)

            print(f"{agent_name:<20} {len(records):>6} {p50:>8.0f} {p95:>8.0f} {avg_tokens:>8.0f} {cost:>10.5f} {error_rate:>8.1%}")

        print("-" * 72)
        print(f"{'TOTAL':<20} {len(self.records):>6} {'':>8} {'':>8} {'':>8} {total_cost:>10.5f}")


dashboard = AgentPerformanceDashboard()


async def instrumented_agent(
    client: anthropic.AsyncAnthropic,
    agent_name: str,
    prompt: str,
    trace_id: str,
) -> str:
    start = time.monotonic()
    success = True
    try:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=120,
            messages=[{"role": "user", "content": prompt}],
        )
        output = response.content[0].text
        duration = (time.monotonic() - start) * 1000
        dashboard.record(agent_name, trace_id, duration,
                         response.usage.input_tokens, response.usage.output_tokens, True)
        return output
    except Exception as e:
        duration = (time.monotonic() - start) * 1000
        dashboard.record(agent_name, trace_id, duration, 0, 0, False)
        raise


async def benchmark_multi_agent(n_requests: int = 5):
    client = anthropic.AsyncAnthropic()
    topics = ["REST APIs", "SQL joins", "async/await", "Docker networking", "Redis pub/sub"]

    async def single_request(i: int):
        trace_id = uuid.uuid4().hex[:8]
        topic = topics[i % len(topics)]

        results = await asyncio.gather(
            instrumented_agent(client, "researcher", f"One fact about {topic}", trace_id),
            instrumented_agent(client, "simplifier", f"ELI5: {topic}", trace_id),
        )

        combined = " | ".join(results)
        final = await instrumented_agent(
            client, "formatter",
            f"Format as one sentence: {combined[:150]}",
            trace_id,
        )
        return final

    tasks = [single_request(i) for i in range(n_requests)]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    successful = sum(1 for r in results if not isinstance(r, Exception))
    print(f"Completed {successful}/{n_requests} requests")

    dashboard.report()


if __name__ == "__main__":
    asyncio.run(benchmark_multi_agent(n_requests=5))

# Expected Token Savings: Dashboard reveals high-cost agents for optimization; P95 latency identifies bottlenecks
# Environment: Production multi-agent systems, regular performance review of agent architectures
```

## Comparison

| Option | Storage | Overhead | Retention | Best For |
|--------|---------|----------|-----------|----------|
| 1 System Prompt Propagation | In-memory | Minimal (+trace tokens) | Session | Simple orchestrators needing basic correlation |
| 2 SQLite Persistence | SQLite | Low | Persistent | Post-mortem debugging, audit trails |
| 3 OTel-Compatible Export | OTLP JSON | Low | Configurable | Teams with Jaeger/Grafana Tempo infrastructure |
| 4 Causal Chain Logging | In-memory | Low | Session | Debugging why agents were spawned |
| 5 Sampling-Based Capture | In-memory | Very low | Sampled | High-throughput production (>100 req/s) |
| 6 Performance Dashboard | In-memory | Low | Session | Regular performance benchmarking |
