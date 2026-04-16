---
title: "Agent Doesn't Implement Multi-Agent Conversation Flow Tracing"
description: "When a user request spawns a chain of sub-agents, each with their own conversation turns and tool calls, there is no way to reconstruct the full causal flow, diagnose where quality degraded, or measure end-to-end latency across the pipeline."
difficulty: intermediate
category: observability
tags: [tracing, multi-agent, conversation, opentelemetry, flow, distributed, observability]
---

## Problem

A multi-agent system routes requests through orchestrators, planners, executors, and reviewers. Each agent has its own conversation history, tool calls, and LLM responses. Without end-to-end tracing, debugging a bad final output requires manually correlating logs across agents using timestamps and guesswork. There is no causal link between the user's question and the sub-agent that produced a flawed intermediate result.

```python
# Broken: each agent logs independently with no shared context
async def orchestrator(question: str) -> str:
    planner_result = await planner_agent(question)   # no trace context passed
    executor_result = await executor_agent(planner_result)
    return executor_result
# Logs show three independent sets of spans with no parent-child relationship
```

---

## Solution 1: Conversation Context Propagation via Shared Trace ID

```python
import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

@dataclass
class ConversationContext:
    """Propagated through the entire multi-agent pipeline."""
    trace_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    parent_span_id: str | None = None
    root_agent: str = "orchestrator"
    user_id: str = ""
    session_id: str = ""
    depth: int = 0  # nesting depth: 0=orchestrator, 1=sub-agent, 2=sub-sub-agent

    def child(self, span_id: str) -> "ConversationContext":
        """Create a child context for a sub-agent call."""
        return ConversationContext(
            trace_id=self.trace_id,       # same trace ID
            parent_span_id=span_id,       # link to caller's span
            root_agent=self.root_agent,
            user_id=self.user_id,
            session_id=self.session_id,
            depth=self.depth + 1,
        )

    def to_headers(self) -> dict[str, str]:
        """Serialize for HTTP propagation."""
        h = {
            "X-Trace-Id": self.trace_id,
            "X-Root-Agent": self.root_agent,
        }
        if self.parent_span_id:
            h["X-Parent-Span-Id"] = self.parent_span_id
        if self.user_id:
            h["X-User-Id"] = self.user_id
        return h

    @classmethod
    def from_headers(cls, headers: dict[str, str]) -> "ConversationContext":
        return cls(
            trace_id=headers.get("X-Trace-Id", str(uuid.uuid4())),
            parent_span_id=headers.get("X-Parent-Span-Id"),
            root_agent=headers.get("X-Root-Agent", "unknown"),
            user_id=headers.get("X-User-Id", ""),
        )

@dataclass
class AgentSpan:
    span_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    trace_id: str = ""
    parent_span_id: str | None = None
    agent_name: str = ""
    operation: str = ""
    started_at: float = field(default_factory=time.monotonic)
    ended_at: float | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    events: list[dict] = field(default_factory=list)
    status: str = "ok"

    def end(self, status: str = "ok"):
        self.ended_at = time.monotonic()
        self.status = status

    @property
    def duration_ms(self) -> float | None:
        if self.ended_at is None:
            return None
        return (self.ended_at - self.started_at) * 1000

    def add_event(self, name: str, attrs: dict | None = None):
        self.events.append({
            "name": name,
            "timestamp": time.monotonic(),
            "attributes": attrs or {}
        })

class AgentTracer:
    def __init__(self, agent_name: str):
        self.agent_name = agent_name
        self._spans: list[AgentSpan] = []

    def start_span(self, operation: str, ctx: ConversationContext,
                   attributes: dict | None = None) -> AgentSpan:
        span = AgentSpan(
            trace_id=ctx.trace_id,
            parent_span_id=ctx.parent_span_id,
            agent_name=self.agent_name,
            operation=operation,
            attributes=attributes or {},
        )
        self._spans.append(span)
        return span

    def export(self) -> list[dict]:
        return [
            {
                "span_id": s.span_id,
                "trace_id": s.trace_id,
                "parent_span_id": s.parent_span_id,
                "agent": s.agent_name,
                "operation": s.operation,
                "duration_ms": s.duration_ms,
                "status": s.status,
                "attributes": s.attributes,
                "events": s.events,
            }
            for s in self._spans
        ]
```

---

## Solution 2: Structured Conversation Turn Tracing

```python
import asyncio
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import AsyncIterator, Any

@dataclass
class TurnTrace:
    """Records one conversation turn (user → agent → response)."""
    turn_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    trace_id: str = ""
    agent_name: str = ""
    turn_index: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    started_at: float = field(default_factory=time.monotonic)
    ended_at: float | None = None
    error: str | None = None

    def record_tool_call(self, tool_name: str, args: dict,
                          result: Any, duration_ms: float):
        self.tool_calls.append({
            "tool": tool_name,
            "args": args,
            "result_type": type(result).__name__,
            "duration_ms": duration_ms,
        })

    def finish(self, input_tokens: int, output_tokens: int, model: str):
        self.ended_at = time.monotonic()
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.model = model

    @property
    def latency_ms(self) -> float:
        end = self.ended_at or time.monotonic()
        return (end - self.started_at) * 1000

class ConversationFlowTracer:
    """Tracks the full conversation flow across all agents in a pipeline."""

    def __init__(self, trace_id: str | None = None):
        self.trace_id = trace_id or str(uuid.uuid4())
        self._turns: list[TurnTrace] = []
        self._agent_sequence: list[str] = []
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def trace_turn(self, agent_name: str) -> AsyncIterator[TurnTrace]:
        async with self._lock:
            turn_index = len(self._turns)
            if not self._agent_sequence or self._agent_sequence[-1] != agent_name:
                self._agent_sequence.append(agent_name)
        turn = TurnTrace(
            trace_id=self.trace_id,
            agent_name=agent_name,
            turn_index=turn_index,
        )
        async with self._lock:
            self._turns.append(turn)
        try:
            yield turn
        except Exception as e:
            turn.error = str(e)
            raise
        finally:
            if turn.ended_at is None:
                turn.ended_at = time.monotonic()

    def agent_flow(self) -> list[str]:
        return list(self._agent_sequence)

    def total_tokens(self) -> dict[str, int]:
        return {
            "input": sum(t.input_tokens for t in self._turns),
            "output": sum(t.output_tokens for t in self._turns),
        }

    def end_to_end_latency_ms(self) -> float:
        if not self._turns:
            return 0.0
        start = min(t.started_at for t in self._turns)
        end = max(t.ended_at or time.monotonic() for t in self._turns)
        return (end - start) * 1000

    def summary(self) -> dict:
        return {
            "trace_id": self.trace_id,
            "agent_flow": self.agent_flow(),
            "total_turns": len(self._turns),
            "total_tokens": self.total_tokens(),
            "end_to_end_latency_ms": self.end_to_end_latency_ms(),
            "errors": [t.error for t in self._turns if t.error],
        }

    def waterfall(self) -> list[dict]:
        """Returns spans sorted by start time for Gantt/waterfall visualization."""
        return sorted([
            {
                "agent": t.agent_name,
                "turn": t.turn_index,
                "started_at": t.started_at,
                "latency_ms": t.latency_ms,
                "tokens": t.input_tokens + t.output_tokens,
                "tool_calls": len(t.tool_calls),
                "error": t.error,
            }
            for t in self._turns
        ], key=lambda x: x["started_at"])
```

---

## Solution 3: OpenTelemetry Trace Propagation Across Agents

```python
from opentelemetry import trace
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor
from opentelemetry.context import attach, detach, get_current
import asyncio

# Setup (once at startup)
def setup_otel(service_name: str) -> trace.Tracer:
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
    trace.set_tracer_provider(provider)
    return trace.get_tracer(service_name)

propagator = TraceContextTextMapPropagator()

def inject_trace_context(carrier: dict) -> dict:
    """Inject current trace context into a carrier dict (e.g., HTTP headers)."""
    propagator.inject(carrier)
    return carrier

def extract_trace_context(carrier: dict):
    """Extract trace context from a carrier dict."""
    return propagator.extract(carrier)

async def orchestrator_agent(question: str, tracer: trace.Tracer) -> str:
    with tracer.start_as_current_span("orchestrator.handle") as span:
        span.set_attribute("question.length", len(question))
        span.set_attribute("agent.role", "orchestrator")

        # Propagate context to sub-agent via carrier dict
        carrier: dict[str, str] = {}
        inject_trace_context(carrier)

        plan = await planner_agent(question, carrier, tracer)
        result = await executor_agent(plan, carrier, tracer)

        span.set_attribute("result.length", len(result))
        return result

async def planner_agent(question: str, carrier: dict,
                         tracer: trace.Tracer) -> str:
    ctx = extract_trace_context(carrier)
    token = attach(ctx)
    try:
        with tracer.start_as_current_span("planner.plan") as span:
            span.set_attribute("agent.role", "planner")
            span.set_attribute("input.length", len(question))
            # ... LLM call ...
            plan = f"Plan for: {question[:50]}"
            span.set_attribute("plan.steps", 3)
            return plan
    finally:
        detach(token)

async def executor_agent(plan: str, carrier: dict,
                          tracer: trace.Tracer) -> str:
    ctx = extract_trace_context(carrier)
    token = attach(ctx)
    try:
        with tracer.start_as_current_span("executor.execute") as span:
            span.set_attribute("agent.role", "executor")
            span.set_attribute("plan.length", len(plan))
            # ... execution ...
            result = f"Result from: {plan[:50]}"
            span.add_event("tool_called", {"tool": "web_search", "duration_ms": 120})
            return result
    finally:
        detach(token)
```

---

## Solution 4: Agent DAG Recorder — Causal Dependency Graph

```python
import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

@dataclass
class AgentNode:
    """One agent execution in the pipeline DAG."""
    node_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    agent_name: str = ""
    parent_ids: list[str] = field(default_factory=list)
    started_at: float = field(default_factory=time.monotonic)
    ended_at: float | None = None
    input_summary: str = ""
    output_summary: str = ""
    token_cost: int = 0
    tool_calls: int = 0
    status: str = "running"
    error: str | None = None

    @property
    def duration_ms(self) -> float:
        end = self.ended_at or time.monotonic()
        return (end - self.started_at) * 1000

class AgentDAGRecorder:
    """
    Records the directed acyclic graph of agent executions.
    Enables causal attribution: given a bad output, trace back
    through the DAG to find which agent produced the flawed input.
    """

    def __init__(self, trace_id: str | None = None):
        self.trace_id = trace_id or str(uuid.uuid4())
        self._nodes: dict[str, AgentNode] = {}
        self._lock = asyncio.Lock()

    async def start_agent(self, agent_name: str,
                           parent_ids: list[str] | None = None,
                           input_summary: str = "") -> AgentNode:
        node = AgentNode(
            agent_name=agent_name,
            parent_ids=parent_ids or [],
            input_summary=input_summary[:200],
        )
        async with self._lock:
            self._nodes[node.node_id] = node
        return node

    async def finish_agent(self, node: AgentNode,
                            output_summary: str = "",
                            token_cost: int = 0,
                            tool_calls: int = 0,
                            error: str | None = None):
        node.ended_at = time.monotonic()
        node.output_summary = output_summary[:200]
        node.token_cost = token_cost
        node.tool_calls = tool_calls
        node.status = "error" if error else "ok"
        node.error = error

    def critical_path(self) -> list[AgentNode]:
        """
        Find the longest-latency path through the DAG.
        This is the path that determined end-to-end latency.
        """
        nodes = list(self._nodes.values())
        if not nodes:
            return []

        # Build children map
        children: dict[str, list[str]] = {n.node_id: [] for n in nodes}
        for n in nodes:
            for parent_id in n.parent_ids:
                if parent_id in children:
                    children[parent_id].append(n.node_id)

        # DP: max cumulative latency to reach each node
        dp: dict[str, float] = {}
        prev: dict[str, str | None] = {}

        def compute(node_id: str) -> float:
            if node_id in dp:
                return dp[node_id]
            node = self._nodes[node_id]
            parent_max = max(
                (compute(pid) for pid in node.parent_ids if pid in self._nodes),
                default=0.0
            )
            dp[node_id] = parent_max + node.duration_ms
            prev[node_id] = max(
                node.parent_ids, key=lambda pid: dp.get(pid, 0.0), default=None
            ) if node.parent_ids else None
            return dp[node_id]

        for n in nodes:
            compute(n.node_id)

        # Trace back from the node with maximum cumulative latency
        end_node_id = max(dp, key=dp.get)
        path: list[AgentNode] = []
        nid: str | None = end_node_id
        while nid:
            path.append(self._nodes[nid])
            nid = prev.get(nid)
        return list(reversed(path))

    def to_mermaid(self) -> str:
        """Generate a Mermaid flowchart of the agent execution DAG."""
        lines = ["flowchart TD"]
        for node in self._nodes.values():
            label = (f"{node.agent_name}\\n"
                     f"{node.duration_ms:.0f}ms | {node.token_cost}tok")
            color = '#ffcccc' if node.error else '#ccffcc'
            lines.append(f'    {node.node_id}["{label}"]')
            lines.append(f'    style {node.node_id} fill:{color}')
        for node in self._nodes.values():
            for parent_id in node.parent_ids:
                lines.append(f"    {parent_id} --> {node.node_id}")
        return "\n".join(lines)

    def total_cost(self) -> dict:
        nodes = list(self._nodes.values())
        return {
            "total_tokens": sum(n.token_cost for n in nodes),
            "total_tool_calls": sum(n.tool_calls for n in nodes),
            "agent_count": len(nodes),
            "error_count": sum(1 for n in nodes if n.error),
        }
```

---

## Solution 5: Conversation Quality Signal Propagation

```python
import asyncio
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable

@dataclass
class QualitySignal:
    """Quality annotation attached to any agent output in the pipeline."""
    agent_name: str
    turn_id: str
    score: float | None = None        # 0.0–1.0, None if not scored
    flags: list[str] = field(default_factory=list)  # e.g. ["hallucination", "refusal"]
    confidence: float = 1.0

QUALITY_FLAGS = {
    "hallucination": "Response contains unverified claims",
    "refusal":       "Agent refused to complete the task",
    "truncated":     "Response was cut off before completion",
    "off_topic":     "Response deviates from the question",
    "low_confidence": "Agent expressed uncertainty about output",
}

class QualityAwareFlowTracer:
    """
    Extends ConversationFlowTracer with quality signal propagation.
    When a sub-agent output is flagged, the flag is inherited by
    all downstream agents, enabling end-to-end quality attribution.
    """

    def __init__(self, trace_id: str | None = None):
        self.flow_tracer = ConversationFlowTracer(trace_id)
        self._quality: list[QualitySignal] = []
        self._propagated_flags: set[str] = set()
        self._lock = asyncio.Lock()

    def record_quality(self, agent_name: str, turn_id: str,
                       score: float | None = None,
                       flags: list[str] | None = None):
        signal = QualitySignal(
            agent_name=agent_name,
            turn_id=turn_id,
            score=score,
            flags=flags or [],
        )
        self._quality.append(signal)
        if flags:
            self._propagated_flags.update(flags)

    def active_quality_flags(self) -> set[str]:
        """Flags that are currently propagated through the pipeline."""
        return set(self._propagated_flags)

    def quality_summary(self) -> dict:
        if not self._quality:
            return {"scored_turns": 0, "avg_score": None, "flags": []}
        scored = [s for s in self._quality if s.score is not None]
        return {
            "scored_turns": len(scored),
            "avg_score": sum(s.score for s in scored) / len(scored) if scored else None,
            "flags": list(self._propagated_flags),
            "flagged_agents": list({s.agent_name for s in self._quality if s.flags}),
        }

    async def auto_score_with_llm(self, agent_name: str, turn_id: str,
                                    output: str, question: str,
                                    score_fn: Callable[[str, str], Awaitable[float]]):
        """Automatically score output quality using an LLM judge."""
        score = await score_fn(question, output)
        flags = []
        if score < 0.4:
            flags.append("low_confidence")
        self.record_quality(agent_name, turn_id, score=score, flags=flags)
        return score
```

---

## Solution 6: Trace Export and Visualization

```python
import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any

class TraceExporter:
    """
    Exports multi-agent conversation traces to multiple backends:
    - JSON file (for offline analysis)
    - Jaeger-compatible format (for distributed tracing UI)
    - Console (for development)
    """

    def export_json(self, tracer: "ConversationFlowTracer",
                    path: str | None = None) -> str:
        data = {
            "trace_id": tracer.trace_id,
            "summary": tracer.summary(),
            "waterfall": tracer.waterfall(),
            "exported_at": time.time(),
        }
        payload = json.dumps(data, indent=2)
        if path:
            with open(path, "w") as f:
                f.write(payload)
        return payload

    def to_jaeger_spans(self, dag: "AgentDAGRecorder") -> list[dict]:
        """Convert AgentDAGRecorder nodes to Jaeger-compatible span format."""
        spans = []
        for node in dag._nodes.values():
            span = {
                "traceID": dag.trace_id.replace("-", ""),
                "spanID": node.node_id,
                "operationName": node.agent_name,
                "references": [
                    {"refType": "CHILD_OF",
                     "traceID": dag.trace_id.replace("-", ""),
                     "spanID": pid}
                    for pid in node.parent_ids
                ],
                "startTime": int(node.started_at * 1_000_000),  # microseconds
                "duration": int(node.duration_ms * 1000),       # microseconds
                "tags": [
                    {"key": "agent.name", "type": "string", "value": node.agent_name},
                    {"key": "token.cost", "type": "int64", "value": node.token_cost},
                    {"key": "tool.calls", "type": "int64", "value": node.tool_calls},
                    {"key": "status", "type": "string", "value": node.status},
                ],
                "logs": [
                    {"timestamp": int(node.started_at * 1_000_000),
                     "fields": [{"key": "input", "type": "string",
                                  "value": node.input_summary}]},
                ] if node.input_summary else [],
            }
            if node.error:
                span["tags"].append({"key": "error", "type": "bool", "value": True})
                span["tags"].append({"key": "error.msg", "type": "string",
                                      "value": node.error})
            spans.append(span)
        return spans

    def print_waterfall(self, tracer: "ConversationFlowTracer"):
        """ASCII waterfall chart for CLI debugging."""
        waterfall = tracer.waterfall()
        if not waterfall:
            print("No turns recorded.")
            return

        start = waterfall[0]["started_at"]
        total_ms = tracer.end_to_end_latency_ms()
        width = 60

        print(f"\nTrace {tracer.trace_id[:8]}... — Total: {total_ms:.0f}ms")
        print("=" * 80)
        print(f"{'Agent':<20} {'ms':>6}  {'Bar':<{width}}")
        print("-" * 80)
        for turn in waterfall:
            offset = (turn["started_at"] - start) * 1000
            bar_start = int(offset / total_ms * width)
            bar_len = max(1, int(turn["latency_ms"] / total_ms * width))
            bar = " " * bar_start + "█" * bar_len
            flag = " !" if turn["error"] else ""
            print(f"{turn['agent']:<20} {turn['latency_ms']:>6.0f}  {bar}{flag}")
        print("=" * 80)
        print(f"Flow: {' → '.join(tracer.agent_flow())}")
        print(f"Tokens: {tracer.total_tokens()}")
```

---

## Comparison

| Solution | Cross-Process | Causal Graph | Quality | Visualization | Complexity | Best For |
|---|---|---|---|---|---|---|
| 1. Context propagation | Yes (headers) | Partial | No | No | Low | HTTP-based agent chains |
| 2. Turn tracing | No (shared object) | No | No | Waterfall | Low | In-process pipelines |
| 3. OpenTelemetry | Yes (W3C headers) | Yes | No | Jaeger/Zipkin | Med | Standard OTEL stack |
| 4. Agent DAG recorder | No | Yes (full DAG) | No | Mermaid/DOT | Med | Complex branching pipelines |
| 5. Quality signal propagation | No | Partial | Yes | No | Med | Output quality debugging |
| 6. Trace exporter | Yes (JSON/Jaeger) | Yes | No | Console + Jaeger | Med | Production export |

**Key principle**: the trace ID is the single thread connecting the user's request to every sub-agent's decision. Propagate it as an HTTP header, function argument, or context variable — never let it reset at an agent boundary. The causal graph (which agent called which, with what output) is what turns a bag of logs into a debuggable execution history.
