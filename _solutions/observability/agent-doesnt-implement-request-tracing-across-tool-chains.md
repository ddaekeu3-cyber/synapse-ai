---
title: "Agent Doesn't Implement Request Tracing Across Tool Chains"
description: "Agents that log individual tool calls without a shared trace identifier cannot reconstruct the causal chain of a multi-tool session: which LLM turn triggered which tool, which tool result fed into the next LLM call, and where latency accumulated. Implement distributed-style request tracing with trace IDs, span IDs, and parent-child relationships so every tool call in a session is linked into a single queryable trace."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-request-tracing-across-tool-chains
tags: [tracing, distributed-tracing, trace-id, span, tool-chain, observability, latency-attribution]
symptoms:
  - "Logs show individual tool results but cannot be joined into a session timeline"
  - "Impossible to tell which LLM turn caused a slow tool call"
  - "No correlation between the user's original query and downstream tool invocations"
  - "Debugging a failed session requires manually reconstructing order from timestamps"
  - "Cannot measure total session latency broken down by tool contribution"
---

## Why This Happens

Agent frameworks typically instrument individual components in isolation: the LLM client logs its request, the tool runner logs its invocation, the response assembler logs its output. Without a shared trace ID propagated through every hop, these log lines cannot be joined. Distributed tracing solves this by assigning a single trace ID to each user request and a unique span ID to each unit of work within it. Each span records its parent span ID, creating a tree that maps the full execution path, timing, and causality of a multi-tool session.

## Solution 1: Trace and Span Identifiers

```python
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class SpanKind(str, Enum):
    SESSION = "session"       # top-level user request
    LLM_TURN = "llm_turn"     # one LLM completion call
    TOOL_CALL = "tool_call"   # one tool invocation
    INTERNAL = "internal"     # any other internal operation


class SpanStatus(str, Enum):
    OK = "ok"
    ERROR = "error"
    TIMEOUT = "timeout"


@dataclass
class Span:
    trace_id: str
    span_id: str
    parent_span_id: Optional[str]
    name: str
    kind: SpanKind
    started_at: float = field(default_factory=time.time)
    ended_at: Optional[float] = None
    status: SpanStatus = SpanStatus.OK
    attributes: Dict[str, Any] = field(default_factory=dict)
    events: list = field(default_factory=list)

    def duration_ms(self) -> Optional[float]:
        if self.ended_at is None:
            return None
        return round((self.ended_at - self.started_at) * 1000, 2)

    def end(self, status: SpanStatus = SpanStatus.OK) -> None:
        self.ended_at = time.time()
        self.status = status

    def add_event(self, name: str, attributes: Optional[Dict[str, Any]] = None) -> None:
        self.events.append({
            "name": name,
            "timestamp": time.time(),
            "attributes": attributes or {},
        })

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value
```

## Solution 2: Trace Context

```python
import uuid
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class TraceContext:
    """
    Carries the trace_id and current span_id through the call chain.
    Child spans are created by forking the context with a new span_id.
    """
    trace_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    span_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    parent_span_id: Optional[str] = None
    session_id: str = ""
    user_id: str = ""
    baggage: dict = field(default_factory=dict)

    def child(self, name: str, kind: SpanKind) -> "tuple[TraceContext, Span]":
        child_span_id = uuid.uuid4().hex[:16]
        child_ctx = TraceContext(
            trace_id=self.trace_id,
            span_id=child_span_id,
            parent_span_id=self.span_id,
            session_id=self.session_id,
            user_id=self.user_id,
            baggage=dict(self.baggage),
        )
        span = Span(
            trace_id=self.trace_id,
            span_id=child_span_id,
            parent_span_id=self.span_id,
            name=name,
            kind=kind,
        )
        return child_ctx, span

    @classmethod
    def new_session(cls, session_id: str = "", user_id: str = "") -> "tuple[TraceContext, Span]":
        ctx = cls(session_id=session_id, user_id=user_id)
        span = Span(
            trace_id=ctx.trace_id,
            span_id=ctx.span_id,
            parent_span_id=None,
            name="session",
            kind=SpanKind.SESSION,
        )
        return ctx, span
```

## Solution 3: In-Process Span Collector

```python
import time
from collections import defaultdict
from threading import Lock
from typing import Dict, List, Optional


class InProcessSpanCollector:
    """
    Accumulates finished spans in memory, grouped by trace_id.
    Suitable for single-process agents; replace with an OTLP exporter
    (Jaeger, Tempo, Honeycomb) for production multi-instance deployments.
    """

    def __init__(self, max_traces: int = 1000, trace_ttl_seconds: float = 3600.0):
        self._max = max_traces
        self._ttl = trace_ttl_seconds
        self._traces: Dict[str, List[Span]] = defaultdict(list)
        self._trace_started: Dict[str, float] = {}
        self._lock = Lock()

    def record(self, span: Span) -> None:
        with self._lock:
            if span.trace_id not in self._trace_started:
                self._trace_started[span.trace_id] = time.time()
                if len(self._traces) >= self._max:
                    self._evict_oldest()
            self._traces[span.trace_id].append(span)

    def _evict_oldest(self) -> None:
        if not self._trace_started:
            return
        oldest = min(self._trace_started, key=self._trace_started.get)
        del self._traces[oldest]
        del self._trace_started[oldest]

    def get_trace(self, trace_id: str) -> List[Span]:
        with self._lock:
            return list(self._traces.get(trace_id, []))

    def purge_expired(self) -> int:
        cutoff = time.time() - self._ttl
        with self._lock:
            expired = [tid for tid, ts in self._trace_started.items() if ts < cutoff]
            for tid in expired:
                del self._traces[tid]
                del self._trace_started[tid]
            return len(expired)

    def stats(self) -> dict:
        with self._lock:
            return {
                "active_traces": len(self._traces),
                "total_spans": sum(len(v) for v in self._traces.values()),
            }
```

## Solution 4: Traced Tool Executor

```python
import asyncio
import time
from typing import Any, Callable


class TracedToolExecutor:
    """
    Wraps tool calls with span creation and recording.
    Propagates trace context through the tool chain automatically.
    """

    def __init__(self, collector: InProcessSpanCollector):
        self._collector = collector

    async def call(
        self,
        ctx: TraceContext,
        tool_name: str,
        tool_fn: Callable,
        *args: Any,
        **kwargs: Any,
    ) -> "tuple[Any, Span]":
        child_ctx, span = ctx.child(
            name=f"tool:{tool_name}",
            kind=SpanKind.TOOL_CALL,
        )
        span.set_attribute("tool.name", tool_name)
        span.set_attribute("tool.args_keys", list(kwargs.keys()))

        try:
            result = await tool_fn(*args, **kwargs)
            span.end(SpanStatus.OK)
            span.set_attribute("tool.result_type", type(result).__name__)
            return result, span
        except asyncio.TimeoutError:
            span.end(SpanStatus.TIMEOUT)
            span.add_event("timeout")
            raise
        except Exception as exc:
            span.end(SpanStatus.ERROR)
            span.set_attribute("error.message", str(exc)[:200])
            raise
        finally:
            self._collector.record(span)
```

## Solution 5: Trace Timeline Renderer

```python
import time
from typing import List, Optional


class TraceTimelineRenderer:
    """
    Reconstructs a trace into a human-readable timeline showing
    span start times, durations, depths, and status — useful for
    debugging session latency and identifying slow hops.
    """

    def __init__(self, collector: InProcessSpanCollector):
        self._collector = collector

    def render(self, trace_id: str) -> dict:
        spans = self._collector.get_trace(trace_id)
        if not spans:
            return {"trace_id": trace_id, "spans": [], "total_duration_ms": None}

        spans_sorted = sorted(spans, key=lambda s: s.started_at)
        root_start = spans_sorted[0].started_at

        # Build parent -> children map for depth calculation
        parent_map: dict = {}
        for s in spans_sorted:
            parent_map.setdefault(s.parent_span_id, []).append(s.span_id)

        def depth(span_id: str, memo: dict = {}) -> int:
            if span_id in memo:
                return memo[span_id]
            span = next((s for s in spans_sorted if s.span_id == span_id), None)
            if span is None or span.parent_span_id is None:
                memo[span_id] = 0
                return 0
            d = 1 + depth(span.parent_span_id, memo)
            memo[span_id] = d
            return d

        timeline = []
        for s in spans_sorted:
            timeline.append({
                "span_id": s.span_id,
                "parent_span_id": s.parent_span_id,
                "name": s.name,
                "kind": s.kind.value,
                "depth": depth(s.span_id),
                "offset_ms": round((s.started_at - root_start) * 1000, 2),
                "duration_ms": s.duration_ms(),
                "status": s.status.value,
                "attributes": s.attributes,
            })

        total_duration = None
        if spans_sorted[-1].ended_at:
            total_duration = round(
                (spans_sorted[-1].ended_at - root_start) * 1000, 2
            )

        return {
            "trace_id": trace_id,
            "span_count": len(timeline),
            "total_duration_ms": total_duration,
            "spans": timeline,
        }
```

## Solution 6: Trace Anomaly Detector

```python
import time
from typing import List


class TraceAnomalyDetector:
    """
    Scans completed traces for anomalies: unusually long spans,
    error spans, missing expected tool calls, and sessions
    that exceed the latency SLO.
    """

    def __init__(
        self,
        collector: InProcessSpanCollector,
        span_latency_warn_ms: float = 5000.0,
        session_latency_slo_ms: float = 30_000.0,
    ):
        self._collector = collector
        self._span_warn = span_latency_warn_ms
        self._session_slo = session_latency_slo_ms

    def analyze_trace(self, trace_id: str) -> dict:
        spans = self._collector.get_trace(trace_id)
        if not spans:
            return {"trace_id": trace_id, "anomalies": []}

        anomalies = []

        for span in spans:
            dur = span.duration_ms()
            if dur is not None and dur > self._span_warn:
                anomalies.append({
                    "type": "slow_span",
                    "span_id": span.span_id,
                    "name": span.name,
                    "duration_ms": dur,
                    "threshold_ms": self._span_warn,
                })
            if span.status == SpanStatus.ERROR:
                anomalies.append({
                    "type": "error_span",
                    "span_id": span.span_id,
                    "name": span.name,
                    "error": span.attributes.get("error.message", ""),
                })

        session_spans = [s for s in spans if s.kind == SpanKind.SESSION]
        for s in session_spans:
            dur = s.duration_ms()
            if dur is not None and dur > self._session_slo:
                anomalies.append({
                    "type": "slo_breach",
                    "span_id": s.span_id,
                    "session_duration_ms": dur,
                    "slo_ms": self._session_slo,
                })

        return {
            "trace_id": trace_id,
            "span_count": len(spans),
            "anomaly_count": len(anomalies),
            "anomalies": anomalies,
        }
```

## Comparison

| Approach | Trace Propagation | Span Hierarchy | In-Process Storage | Timeline View | Anomaly Detection |
|---|---|---|---|---|---|
| TraceContext | Yes (fork pattern) | Via parent_span_id | No | No | No |
| InProcessSpanCollector | No | No | Yes (TTL + LRU) | No | No |
| TracedToolExecutor | Via context | Via context | Via collector | No | No |
| TraceTimelineRenderer | No | Yes (depth calc) | No | Yes | No |
| TraceAnomalyDetector | No | No | Via collector | No | Yes |

**Best for production**: Create a new `TraceContext` at the session boundary (when the user's message arrives) and pass it through every tool call and LLM turn. Store spans with an OTLP exporter (Jaeger, Grafana Tempo, or Honeycomb) rather than `InProcessSpanCollector` for multi-instance deployments — the in-process collector is useful for development and single-node setups. Set `span_latency_warn_ms` to 2× your P95 tool latency so anomaly alerts fire only for genuine outliers. Emit `trace_id` in every user-facing error response so support engineers can look up the full execution tree for a failing session.
