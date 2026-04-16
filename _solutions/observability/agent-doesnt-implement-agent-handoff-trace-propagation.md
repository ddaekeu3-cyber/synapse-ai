---
title: "Agent Doesn't Implement Agent Handoff Trace Propagation"
description: "Multi-agent systems where a orchestrator delegates to subagents lose trace continuity at handoff boundaries: each subagent starts a new trace with no reference to the parent, making it impossible to reconstruct the full execution path when a task fails three levels deep. Implement agent handoff trace propagation that passes a trace context through every delegation, links parent and child spans, and enables end-to-end trace reconstruction across agent boundaries."
date: 2026-04-16
difficulty: advanced
category: observability
slug: agent-doesnt-implement-agent-handoff-trace-propagation
tags: [distributed-tracing, agent-handoff, trace-propagation, multi-agent, span-context, observability]
symptoms:
  - "Subagent failures cannot be traced back to the orchestrator task that triggered them"
  - "Each agent creates an independent trace — no parent-child relationship visible in dashboards"
  - "No way to measure total end-to-end latency across an orchestrator-subagent chain"
  - "Debugging a failure requires manually correlating timestamps across disconnected traces"
  - "Trace IDs are not passed when an agent delegates to another via tool call or message"
---

## Why This Happens

Distributed tracing requires explicit propagation: a trace context (trace ID, parent span ID) must be explicitly carried from the caller to the callee. In single-service systems, frameworks handle this automatically via HTTP headers or thread-local storage. In multi-agent systems, handoffs happen through custom channels — tool calls, message queues, direct invocations — that have no automatic propagation. Each agent must explicitly extract the incoming trace context, create a child span under it, and inject the updated context into any outbound delegation. Without this, every agent boundary is a trace discontinuity.

## Solution 1: Trace Context

```python
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, Optional


@dataclass
class TraceContext:
    trace_id: str                        # shared across entire multi-agent chain
    span_id: str                         # unique per agent / per operation
    parent_span_id: Optional[str]        # span_id of the delegating agent
    agent_id: str                        # identifies this agent instance
    depth: int = 0                       # delegation depth (orchestrator=0)
    baggage: Dict[str, str] = field(default_factory=dict)  # propagated metadata

    @classmethod
    def root(cls, agent_id: str) -> "TraceContext":
        return cls(
            trace_id=uuid.uuid4().hex,
            span_id=uuid.uuid4().hex[:16],
            parent_span_id=None,
            agent_id=agent_id,
            depth=0,
        )

    def child(self, child_agent_id: str) -> "TraceContext":
        return TraceContext(
            trace_id=self.trace_id,
            span_id=uuid.uuid4().hex[:16],
            parent_span_id=self.span_id,
            agent_id=child_agent_id,
            depth=self.depth + 1,
            baggage=dict(self.baggage),
        )

    def to_headers(self) -> Dict[str, str]:
        headers = {
            "x-trace-id": self.trace_id,
            "x-span-id": self.span_id,
            "x-agent-id": self.agent_id,
            "x-depth": str(self.depth),
        }
        if self.parent_span_id:
            headers["x-parent-span-id"] = self.parent_span_id
        for k, v in self.baggage.items():
            headers[f"x-baggage-{k}"] = v
        return headers

    @classmethod
    def from_headers(cls, headers: Dict[str, str], agent_id: str) -> "TraceContext":
        return cls(
            trace_id=headers.get("x-trace-id", uuid.uuid4().hex),
            span_id=uuid.uuid4().hex[:16],   # new span for this agent
            parent_span_id=headers.get("x-span-id"),
            agent_id=agent_id,
            depth=int(headers.get("x-depth", "0")) + 1,
            baggage={
                k[len("x-baggage-"):]: v
                for k, v in headers.items()
                if k.startswith("x-baggage-")
            },
        )
```

## Solution 2: Agent Span Recorder

```python
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class SpanStatus(str, Enum):
    RUNNING = "running"
    OK = "ok"
    ERROR = "error"


@dataclass
class AgentSpan:
    trace_id: str
    span_id: str
    parent_span_id: Optional[str]
    agent_id: str
    operation: str
    depth: int
    started_at: float = field(default_factory=time.time)
    ended_at: Optional[float] = None
    status: SpanStatus = SpanStatus.RUNNING
    error: Optional[str] = None
    attributes: Dict[str, Any] = field(default_factory=dict)
    child_span_ids: List[str] = field(default_factory=list)

    @property
    def duration_ms(self) -> Optional[float]:
        if self.ended_at is None:
            return None
        return round((self.ended_at - self.started_at) * 1000, 2)


class AgentSpanRecorder:
    """
    Creates and records spans for agent operations.
    Stores spans indexed by trace_id for end-to-end reconstruction.
    """

    def __init__(self):
        self._spans: Dict[str, List[AgentSpan]] = {}  # trace_id -> [spans]

    def start_span(
        self,
        ctx: TraceContext,
        operation: str,
        attributes: Optional[Dict[str, Any]] = None,
    ) -> AgentSpan:
        span = AgentSpan(
            trace_id=ctx.trace_id,
            span_id=ctx.span_id,
            parent_span_id=ctx.parent_span_id,
            agent_id=ctx.agent_id,
            operation=operation,
            depth=ctx.depth,
            attributes=attributes or {},
        )
        self._spans.setdefault(ctx.trace_id, []).append(span)
        return span

    def end_span(
        self,
        span: AgentSpan,
        status: SpanStatus = SpanStatus.OK,
        error: Optional[str] = None,
    ) -> None:
        span.ended_at = time.time()
        span.status = status
        span.error = error

    def get_trace(self, trace_id: str) -> List[AgentSpan]:
        return self._spans.get(trace_id, [])
```

## Solution 3: Handoff Context Injector

```python
from typing import Any, Dict, Optional


class HandoffContextInjector:
    """
    Injects trace context into agent handoff payloads so that
    receiving agents can extract and continue the trace.
    Supports dict payloads (tool args), HTTP headers, and message envelopes.
    """

    _CONTEXT_KEY = "__trace_context__"

    @classmethod
    def inject_into_args(
        cls, args: Dict[str, Any], ctx: TraceContext
    ) -> Dict[str, Any]:
        """Embeds trace context as a reserved field in tool call args."""
        return {**args, cls._CONTEXT_KEY: ctx.to_headers()}

    @classmethod
    def extract_from_args(
        cls, args: Dict[str, Any], agent_id: str
    ) -> tuple[Dict[str, Any], Optional[TraceContext]]:
        """Extracts and removes trace context from tool call args."""
        ctx_headers = args.get(cls._CONTEXT_KEY)
        clean_args = {k: v for k, v in args.items() if k != cls._CONTEXT_KEY}
        if ctx_headers:
            return clean_args, TraceContext.from_headers(ctx_headers, agent_id)
        return clean_args, None

    @classmethod
    def inject_into_message(
        cls, message: Dict[str, Any], ctx: TraceContext
    ) -> Dict[str, Any]:
        """Embeds trace context in an inter-agent message envelope."""
        return {**message, "trace_context": ctx.to_headers()}

    @classmethod
    def extract_from_message(
        cls, message: Dict[str, Any], agent_id: str
    ) -> Optional[TraceContext]:
        ctx_headers = message.get("trace_context")
        if ctx_headers:
            return TraceContext.from_headers(ctx_headers, agent_id)
        return None
```

## Solution 4: Trace-Aware Agent Dispatcher

```python
import asyncio
from typing import Any, Callable, Dict, Optional


class TraceAwareAgentDispatcher:
    """
    Wraps agent delegation calls with automatic trace context propagation.
    Creates a child context for each delegation and records the handoff span.
    """

    def __init__(
        self,
        recorder: AgentSpanRecorder,
        injector: HandoffContextInjector,
    ):
        self._recorder = recorder
        self._injector = injector

    async def delegate(
        self,
        parent_ctx: TraceContext,
        child_agent_id: str,
        delegation_fn: Callable,       # async fn(args_with_context) -> Any
        args: Dict[str, Any],
        operation: str = "delegation",
    ) -> Any:
        child_ctx = parent_ctx.child(child_agent_id)
        span = self._recorder.start_span(
            child_ctx, operation,
            attributes={"delegated_to": child_agent_id, "args_keys": list(args.keys())},
        )

        injected_args = self._injector.inject_into_args(args, child_ctx)

        try:
            result = await delegation_fn(injected_args)
            self._recorder.end_span(span, SpanStatus.OK)
            return result
        except Exception as exc:
            self._recorder.end_span(span, SpanStatus.ERROR, str(exc))
            raise
```

## Solution 5: End-to-End Trace Reconstructor

```python
from typing import Dict, List, Optional


class EndToEndTraceReconstructor:
    """
    Assembles a complete execution tree from recorded spans
    for a given trace_id, including depth, timing, and error paths.
    """

    def __init__(self, recorder: AgentSpanRecorder):
        self._recorder = recorder

    def reconstruct(self, trace_id: str) -> dict:
        spans = self._recorder.get_trace(trace_id)
        if not spans:
            return {"trace_id": trace_id, "status": "not_found"}

        span_map = {s.span_id: s for s in spans}
        root_spans = [s for s in spans if s.parent_span_id is None]

        def build_node(span: AgentSpan) -> dict:
            children = [
                build_node(span_map[sid])
                for sid in span.child_span_ids
                if sid in span_map
            ]
            # Also find children by parent_span_id reference
            child_spans = [s for s in spans if s.parent_span_id == span.span_id]
            for cs in child_spans:
                if cs.span_id not in span.child_span_ids:
                    children.append(build_node(cs))

            return {
                "span_id": span.span_id,
                "agent_id": span.agent_id,
                "operation": span.operation,
                "depth": span.depth,
                "duration_ms": span.duration_ms,
                "status": span.status.value,
                "error": span.error,
                "children": children,
            }

        total_duration = None
        if spans:
            earliest = min(s.started_at for s in spans)
            latest_ended = [s.ended_at for s in spans if s.ended_at]
            if latest_ended:
                total_duration = round((max(latest_ended) - earliest) * 1000, 2)

        errors = [s for s in spans if s.status == SpanStatus.ERROR]

        return {
            "trace_id": trace_id,
            "total_span_count": len(spans),
            "total_duration_ms": total_duration,
            "max_depth": max((s.depth for s in spans), default=0),
            "error_count": len(errors),
            "failed_agents": [s.agent_id for s in errors],
            "execution_tree": [build_node(s) for s in root_spans],
        }
```

## Solution 6: Handoff Trace Dashboard

```python
import time
from typing import List


class AgentHandoffTraceDashboard:
    """
    Surfaces cross-agent trace statistics: depth distributions,
    failure rates by agent, and average delegation overhead.
    """

    def __init__(self, recorder: AgentSpanRecorder):
        self._recorder = recorder

    def render(self, recent_trace_ids: List[str]) -> dict:
        all_spans = []
        for tid in recent_trace_ids:
            all_spans.extend(self._recorder.get_trace(tid))

        if not all_spans:
            return {"generated_at": time.time(), "traces": 0}

        error_spans = [s for s in all_spans if s.status == SpanStatus.ERROR]
        depth_values = [s.depth for s in all_spans]
        durations = [s.duration_ms for s in all_spans if s.duration_ms is not None]

        by_agent: dict = {}
        for s in all_spans:
            if s.agent_id not in by_agent:
                by_agent[s.agent_id] = {"calls": 0, "errors": 0}
            by_agent[s.agent_id]["calls"] += 1
            if s.status == SpanStatus.ERROR:
                by_agent[s.agent_id]["errors"] += 1

        return {
            "generated_at": time.time(),
            "traces": len(recent_trace_ids),
            "total_spans": len(all_spans),
            "error_rate_pct": round(len(error_spans) / max(len(all_spans), 1) * 100, 2),
            "max_delegation_depth": max(depth_values, default=0),
            "avg_span_duration_ms": round(sum(durations) / len(durations), 2) if durations else None,
            "by_agent": by_agent,
        }
```

## Comparison

| Approach | Context Propagation | Span Recording | Tree Reconstruction | Multi-Agent Dispatch | Dashboard |
|---|---|---|---|---|---|
| TraceContext | Yes (headers + child) | No | No | No | No |
| AgentSpanRecorder | No | Yes | No | No | No |
| HandoffContextInjector | Yes (inject/extract) | No | No | No | No |
| TraceAwareAgentDispatcher | Via injector | Via recorder | No | Yes | No |
| EndToEndTraceReconstructor | No | No | Yes | No | No |
| AgentHandoffTraceDashboard | No | No | No | No | Yes |

**Best for production**: Always generate a root `TraceContext` at the user request entry point and propagate it through every agent delegation — retrofitting tracing later requires touching every delegation boundary simultaneously. Use `x-trace-id` as a standard HTTP header so traces span both agent-to-agent calls and agent-to-service calls, allowing existing APM tooling (Datadog, Jaeger, Honeycomb) to correlate them. Store spans in a centralized backend (not in-process) in production: the `AgentSpanRecorder` here is an in-memory reference — replace `_spans` storage with a write-ahead log or OTLP exporter. Set a max delegation depth (e.g., 10) and reject delegations that would exceed it — `depth` in `TraceContext` makes this check trivial and prevents unbounded recursive agent chains from consuming API budget silently.
