---
layout: solution
title: "Agent Doesn't Implement Per-Turn Latency Breakdown Tracing"
category: observability
description: "Instrument every pipeline stage—queue wait, model call, tool execution, post-processing—to expose where latency is actually spent in each agent turn."
tags: [observability, latency, tracing, performance, instrumentation]
---

# Agent Doesn't Implement Per-Turn Latency Breakdown Tracing

## Problem

Without stage-level timing, all you know is "the turn took 3.2 seconds." You can't tell whether the bottleneck is model latency, tool execution, serialization overhead, or queue wait time — making optimization guesswork.

## Solution Options

### Option 1: Simple Stage Timer Context Manager

```python
import anthropic
import time
from contextlib import contextmanager
from dataclasses import dataclass, field

client = anthropic.Anthropic()

@dataclass
class TurnTrace:
    turn_id: str
    stages: dict[str, float] = field(default_factory=dict)
    _start: float = field(default_factory=time.perf_counter, repr=False)

    @contextmanager
    def stage(self, name: str):
        t0 = time.perf_counter()
        try:
            yield
        finally:
            self.stages[name] = round((time.perf_counter() - t0) * 1000, 2)

    @property
    def total_ms(self) -> float:
        return round((time.perf_counter() - self._start) * 1000, 2)

    def summary(self) -> str:
        parts = [f"{k}={v}ms" for k, v in self.stages.items()]
        return f"[{self.turn_id}] total={self.total_ms}ms | " + " | ".join(parts)

def process_turn(user_msg: str, history: list[dict], turn_id: str) -> tuple[str, TurnTrace]:
    trace = TurnTrace(turn_id=turn_id)

    with trace.stage("history_serialize"):
        messages = history + [{"role": "user", "content": user_msg}]

    with trace.stage("model_call"):
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            messages=messages
        )

    with trace.stage("response_extract"):
        reply = resp.content[0].text
        token_info = f"in={resp.usage.input_tokens} out={resp.usage.output_tokens}"

    with trace.stage("history_update"):
        history.append({"role": "user", "content": user_msg})
        history.append({"role": "assistant", "content": reply})

    trace.stages["tokens"] = token_info  # type: ignore
    return reply, trace

history = []
for i, msg in enumerate(["What is TTFB?", "How does it affect agent UX?", "What's a good p95 target?"]):
    reply, trace = process_turn(msg, history, turn_id=f"turn_{i+1}")
    print(trace.summary())

# Expected Token Savings: zero overhead on model calls; tracing is pure Python timing
# Environment: any agent pipeline; baseline for latency optimization
```

### Option 2: Structured Trace with Tool Execution Breakdown

```python
import anthropic
import time
import json
from dataclasses import dataclass, field
from typing import Any

client = anthropic.Anthropic()

@dataclass
class StageSpan:
    name: str
    start_ms: float
    end_ms: float | None = None
    metadata: dict = field(default_factory=dict)

    @property
    def duration_ms(self) -> float:
        if self.end_ms is None:
            return 0.0
        return round(self.end_ms - self.start_ms, 2)

class TurnTracer:
    def __init__(self, turn_id: str):
        self.turn_id = turn_id
        self.spans: list[StageSpan] = []
        self._epoch = time.perf_counter()

    def _now_ms(self) -> float:
        return round((time.perf_counter() - self._epoch) * 1000, 2)

    def start_span(self, name: str, **meta) -> StageSpan:
        span = StageSpan(name=name, start_ms=self._now_ms(), metadata=meta)
        self.spans.append(span)
        return span

    def end_span(self, span: StageSpan, **meta) -> None:
        span.end_ms = self._now_ms()
        span.metadata.update(meta)

    def report(self) -> dict:
        total = sum(s.duration_ms for s in self.spans)
        return {
            "turn_id": self.turn_id,
            "total_ms": total,
            "stages": [
                {
                    "name": s.name,
                    "duration_ms": s.duration_ms,
                    "pct": round(s.duration_ms / max(total, 1) * 100, 1),
                    **s.metadata
                }
                for s in self.spans
            ]
        }

def simulate_tool_call(tool_name: str, args: dict) -> dict:
    time.sleep(0.05)  # simulate 50ms tool latency
    return {"result": f"{tool_name} executed with {args}"}

def run_traced_turn(user_msg: str, history: list[dict]) -> dict:
    tracer = TurnTracer(turn_id=f"t_{int(time.time()*1000)}")

    span = tracer.start_span("model_call_1", model="claude-haiku-4-5-20251001")
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        tools=[{
            "name": "search_docs",
            "description": "Search documentation",
            "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}
        }],
        messages=history + [{"role": "user", "content": user_msg}]
    )
    tracer.end_span(span, input_tokens=resp.usage.input_tokens, output_tokens=resp.usage.output_tokens, stop_reason=resp.stop_reason)

    tool_results = []
    if resp.stop_reason == "tool_use":
        for block in resp.content:
            if block.type == "tool_use":
                tspan = tracer.start_span("tool_exec", tool=block.name)
                result = simulate_tool_call(block.name, block.input)
                tracer.end_span(tspan)
                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(result)})

        if tool_results:
            span2 = tracer.start_span("model_call_2", model="claude-haiku-4-5-20251001")
            resp2 = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=512,
                messages=history + [
                    {"role": "user", "content": user_msg},
                    {"role": "assistant", "content": resp.content},
                    {"role": "user", "content": tool_results}
                ]
            )
            tracer.end_span(span2, input_tokens=resp2.usage.input_tokens, output_tokens=resp2.usage.output_tokens)

    report = tracer.report()
    print(json.dumps(report, indent=2))
    return report

run_traced_turn("Search for information about rate limiting patterns", [])

# Expected Token Savings: N/A; reveals tool vs model latency ratio (often 60/40)
# Environment: agentic pipelines with tool use, latency SLO monitoring
```

### Option 3: Percentile Aggregator Across Multiple Turns

```python
import anthropic
import time
import statistics
from collections import defaultdict

client = anthropic.Anthropic()

class LatencyAggregator:
    def __init__(self):
        self.stage_samples: dict[str, list[float]] = defaultdict(list)
        self.turn_count = 0

    def record(self, stages: dict[str, float]) -> None:
        self.turn_count += 1
        for stage, ms in stages.items():
            if isinstance(ms, (int, float)):
                self.stage_samples[stage].append(ms)

    def percentile(self, data: list[float], p: float) -> float:
        if not data:
            return 0.0
        sorted_data = sorted(data)
        idx = int(len(sorted_data) * p / 100)
        return round(sorted_data[min(idx, len(sorted_data)-1)], 2)

    def report(self) -> None:
        print(f"\n=== Latency Report ({self.turn_count} turns) ===")
        print(f"{'Stage':<25} {'p50':>8} {'p95':>8} {'p99':>8} {'avg':>8}")
        print("-" * 60)
        for stage, samples in sorted(self.stage_samples.items()):
            p50 = self.percentile(samples, 50)
            p95 = self.percentile(samples, 95)
            p99 = self.percentile(samples, 99)
            avg = round(statistics.mean(samples), 2)
            print(f"{stage:<25} {p50:>7}ms {p95:>7}ms {p99:>7}ms {avg:>7}ms")

aggregator = LatencyAggregator()

PROMPTS = [
    "What is a bloom filter?",
    "Explain consistent hashing.",
    "What is a skip list?",
    "Explain the thundering herd problem.",
    "What is write amplification in LSM trees?"
]

for prompt in PROMPTS:
    stages: dict[str, float] = {}

    t0 = time.perf_counter()
    messages = [{"role": "user", "content": prompt}]
    stages["msg_prepare_ms"] = round((time.perf_counter() - t0) * 1000, 2)

    t1 = time.perf_counter()
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=messages
    )
    stages["model_call_ms"] = round((time.perf_counter() - t1) * 1000, 2)

    t2 = time.perf_counter()
    _ = resp.content[0].text
    stages["extract_ms"] = round((time.perf_counter() - t2) * 1000, 2)
    stages["input_tokens"] = resp.usage.input_tokens
    stages["output_tokens"] = resp.usage.output_tokens

    aggregator.record(stages)
    print(f"Turn: model={stages['model_call_ms']}ms tokens_out={stages['output_tokens']}")

aggregator.report()

# Expected Token Savings: N/A; enables data-driven SLO thresholds (e.g. p95 < 2000ms)
# Environment: production monitoring, capacity planning, SLO dashboards
```

### Option 4: Async Pipeline Tracer with Concurrent Stage Tracking

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass, field

async_client = anthropic.AsyncAnthropic()

@dataclass
class AsyncSpan:
    name: str
    start: float = field(default_factory=time.perf_counter)
    end: float | None = None
    concurrent_with: list[str] = field(default_factory=list)

    @property
    def ms(self) -> float:
        return round(((self.end or time.perf_counter()) - self.start) * 1000, 2)

class AsyncTurnTracer:
    def __init__(self):
        self.spans: list[AsyncSpan] = []

    def span(self, name: str) -> "SpanCtx":
        return SpanCtx(self, name)

    def report(self) -> None:
        total = sum(s.ms for s in self.spans)
        print(f"\n{'Stage':<30} {'Duration':>10} {'%':>6}")
        print("-" * 50)
        for s in self.spans:
            pct = round(s.ms / max(total, 1) * 100, 1)
            print(f"{s.name:<30} {s.ms:>9}ms {pct:>5}%")
        print(f"{'TOTAL':<30} {total:>9}ms")

class SpanCtx:
    def __init__(self, tracer: AsyncTurnTracer, name: str):
        self.tracer = tracer
        self.name = name
        self.span: AsyncSpan | None = None

    async def __aenter__(self):
        self.span = AsyncSpan(name=self.name)
        self.tracer.spans.append(self.span)
        return self.span

    async def __aexit__(self, *_):
        if self.span:
            self.span.end = time.perf_counter()

async def parallel_context_fetch(query: str) -> tuple[str, str]:
    """Simulate fetching context from two sources concurrently."""
    await asyncio.sleep(0.03)  # 30ms each
    return f"ctx_a: {query[:20]}", f"ctx_b: {query[:20]}"

async def run_async_traced_turn(user_msg: str) -> str:
    tracer = AsyncTurnTracer()

    async with tracer.span("context_fetch"):
        ctx_a, ctx_b = await parallel_context_fetch(user_msg)
        augmented_msg = f"{user_msg}\n\nContext: {ctx_a} | {ctx_b}"

    async with tracer.span("model_call"):
        resp = await async_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=[{"role": "user", "content": augmented_msg}]
        )

    async with tracer.span("post_process"):
        reply = resp.content[0].text
        await asyncio.sleep(0.005)  # simulate 5ms post-processing

    tracer.report()
    return reply

asyncio.run(run_async_traced_turn("Explain vector databases and their use in RAG systems"))

# Expected Token Savings: context fetch parallelism saves ~30ms wall time vs sequential
# Environment: async agents with parallel context retrieval, RAG pipelines
```

### Option 5: Trace Export to Structured Log with Request Correlation

```python
import anthropic
import time
import json
import uuid
import logging
from dataclasses import dataclass, asdict

client = anthropic.Anthropic()

logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger("agent.trace")

@dataclass
class TraceEvent:
    trace_id: str
    turn_id: str
    stage: str
    duration_ms: float
    timestamp: float
    metadata: dict

def emit_trace(event: TraceEvent) -> None:
    logger.info(json.dumps(asdict(event)))

class CorrelatedTracer:
    def __init__(self, trace_id: str | None = None):
        self.trace_id = trace_id or str(uuid.uuid4())[:8]
        self.turn_id = str(uuid.uuid4())[:8]
        self._stages: dict[str, float] = {}

    def record(self, stage: str, duration_ms: float, **meta) -> None:
        emit_trace(TraceEvent(
            trace_id=self.trace_id,
            turn_id=self.turn_id,
            stage=stage,
            duration_ms=round(duration_ms, 2),
            timestamp=time.time(),
            metadata=meta
        ))

def timed_call(fn, *args, **kwargs):
    t0 = time.perf_counter()
    result = fn(*args, **kwargs)
    return result, round((time.perf_counter() - t0) * 1000, 2)

def handle_request(user_msg: str, session_trace_id: str) -> str:
    tracer = CorrelatedTracer(trace_id=session_trace_id)

    messages = [{"role": "user", "content": user_msg}]
    tracer.record("msg_prepare", 0.1, msg_len=len(user_msg))

    resp, model_ms = timed_call(
        client.messages.create,
        model="claude-haiku-4-5-20251001",
        max_tokens=384,
        messages=messages
    )
    tracer.record("model_call", model_ms,
        model="claude-haiku-4-5-20251001",
        input_tokens=resp.usage.input_tokens,
        output_tokens=resp.usage.output_tokens,
        stop_reason=resp.stop_reason
    )

    reply = resp.content[0].text
    tracer.record("extract", 0.1, reply_len=len(reply))

    return reply

session_id = str(uuid.uuid4())[:8]
print(f"Session: {session_id}\n")
for msg in ["What is Kafka?", "How does Kafka guarantee ordering?"]:
    handle_request(msg, session_id)

# Expected Token Savings: trace log enables post-hoc filtering by trace_id across long sessions
# Environment: multi-service agents, distributed systems, log aggregation (Datadog, CloudWatch)
```

### Option 6: Real-Time Latency SLO Alerting

```python
import anthropic
import time
from dataclasses import dataclass, field
from collections import deque

client = anthropic.Anthropic()

@dataclass
class SLOConfig:
    stage: str
    warn_ms: float
    critical_ms: float

SLO_RULES = [
    SLOConfig("model_call", warn_ms=2000, critical_ms=5000),
    SLOConfig("tool_exec", warn_ms=500, critical_ms=2000),
    SLOConfig("total_turn", warn_ms=3000, critical_ms=8000),
]

@dataclass
class SLOMonitor:
    rules: list[SLOConfig]
    window: deque = field(default_factory=lambda: deque(maxlen=100))
    breach_count: dict[str, int] = field(default_factory=dict)

    def check(self, stage: str, duration_ms: float) -> str | None:
        for rule in self.rules:
            if rule.stage != stage:
                continue
            if duration_ms >= rule.critical_ms:
                self.breach_count[stage] = self.breach_count.get(stage, 0) + 1
                return f"CRITICAL: {stage}={duration_ms:.0f}ms (limit={rule.critical_ms}ms)"
            elif duration_ms >= rule.warn_ms:
                self.breach_count[stage] = self.breach_count.get(stage, 0) + 1
                return f"WARN: {stage}={duration_ms:.0f}ms (limit={rule.warn_ms}ms)"
        return None

monitor = SLOMonitor(rules=SLO_RULES)

def run_monitored_turn(user_msg: str, turn_num: int) -> str:
    turn_start = time.perf_counter()
    alerts = []

    t0 = time.perf_counter()
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": user_msg}]
    )
    model_ms = (time.perf_counter() - t0) * 1000
    alert = monitor.check("model_call", model_ms)
    if alert:
        alerts.append(alert)

    reply = resp.content[0].text
    total_ms = (time.perf_counter() - turn_start) * 1000
    alert = monitor.check("total_turn", total_ms)
    if alert:
        alerts.append(alert)

    status = f"[turn {turn_num}] model={model_ms:.0f}ms total={total_ms:.0f}ms"
    if alerts:
        status += f" | ALERTS: {'; '.join(alerts)}"
    else:
        status += " | OK"
    print(status)
    return reply

prompts = [
    "List 3 distributed consensus algorithms.",
    "Compare Raft and Paxos.",
    "What is the CAP theorem?",
    "Explain eventual consistency."
]
for i, p in enumerate(prompts):
    run_monitored_turn(p, i + 1)

if monitor.breach_count:
    print(f"\nSLO Breach Summary: {monitor.breach_count}")
else:
    print("\nAll turns within SLO thresholds.")

# Expected Token Savings: N/A; enables proactive alerting before users notice degradation
# Environment: production agents, on-call SLO monitoring, latency regression detection
```

## Comparison

| Option | Granularity | Overhead | Best For |
|--------|-------------|----------|----------|
| 1 | Stage-level | Minimal | Quick instrumentation |
| 2 | Span + tool breakdown | Low | Agentic tool-use pipelines |
| 3 | Percentile aggregation | Low | SLO baseline establishment |
| 4 | Async concurrent stages | Low | Parallel context pipelines |
| 5 | Correlated structured log | Minimal | Distributed log aggregation |
| 6 | Real-time SLO alerting | Minimal | Production on-call monitoring |
