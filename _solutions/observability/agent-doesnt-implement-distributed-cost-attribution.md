---
title: "Agent Doesn't Implement Distributed Cost Attribution"
description: "Solutions for tracking and attributing LLM token costs across distributed agent workflows, sub-agents, and multi-tenant systems."
tags: [observability, cost, attribution, multi-agent, billing]
difficulty: intermediate
---

## Problem

In distributed agent architectures with sub-agents, parallel workflows, and multi-tenant usage, token costs accumulate invisibly. There's no way to know which workflow, user, team, or feature drove the bill — making optimization, chargeback, and budget enforcement impossible.

---

## Solution 1: Span-Based Cost Tracker with Propagating Trace Context

Attach cost metadata to every API call and propagate a trace context so costs roll up through the call tree.

```python
import anthropic
import uuid
import time
from dataclasses import dataclass, field
from typing import Optional
from contextlib import contextmanager
import threading

client = anthropic.Anthropic()

@dataclass
class CostSpan:
    span_id: str
    parent_id: Optional[str]
    trace_id: str
    name: str
    labels: dict
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    start_time: float = field(default_factory=time.time)
    end_time: Optional[float] = None

    @property
    def cost_usd(self) -> float:
        # claude-sonnet-4-6 pricing
        input_cost = self.input_tokens * 3.00 / 1_000_000
        output_cost = self.output_tokens * 15.00 / 1_000_000
        cache_read_cost = self.cache_read_tokens * 0.30 / 1_000_000
        cache_write_cost = self.cache_write_tokens * 3.75 / 1_000_000
        return input_cost + output_cost + cache_read_cost + cache_write_cost

_local = threading.local()
_spans: list[CostSpan] = []
_spans_lock = threading.Lock()

@contextmanager
def cost_span(name: str, labels: dict = None):
    parent_id = getattr(_local, "current_span_id", None)
    trace_id = getattr(_local, "trace_id", str(uuid.uuid4()))
    span = CostSpan(
        span_id=str(uuid.uuid4()),
        parent_id=parent_id,
        trace_id=trace_id,
        name=name,
        labels=labels or {},
    )
    _local.current_span_id = span.span_id
    _local.trace_id = trace_id
    try:
        yield span
    finally:
        span.end_time = time.time()
        with _spans_lock:
            _spans.append(span)
        _local.current_span_id = parent_id

def tracked_call(messages: list, model="claude-sonnet-4-6", **kwargs) -> anthropic.types.Message:
    span_id = getattr(_local, "current_span_id", None)
    response = client.messages.create(model=model, max_tokens=1024, messages=messages, **kwargs)
    usage = response.usage
    if span_id:
        with _spans_lock:
            for span in _spans:
                if span.span_id == span_id:
                    span.input_tokens += usage.input_tokens
                    span.output_tokens += usage.output_tokens
                    span.cache_read_tokens += getattr(usage, "cache_read_input_tokens", 0)
                    span.cache_write_tokens += getattr(usage, "cache_creation_input_tokens", 0)
                    break
    return response

def print_cost_tree():
    with _spans_lock:
        spans_by_id = {s.span_id: s for s in _spans}
        roots = [s for s in _spans if s.parent_id is None]

    def print_node(span, depth=0):
        indent = "  " * depth
        print(f"{indent}[{span.name}] ${span.cost_usd:.6f} "
              f"(in={span.input_tokens}, out={span.output_tokens})")
        children = [s for s in _spans if s.parent_id == span.span_id]
        for child in children:
            print_node(child, depth + 1)

    for root in roots:
        print_node(root)

# Usage
with cost_span("pipeline", {"team": "search", "feature": "query-expansion"}):
    with cost_span("expand-query"):
        tracked_call([{"role": "user", "content": "Expand: machine learning"}])
    with cost_span("rank-results"):
        tracked_call([{"role": "user", "content": "Rank these results by relevance"}])

print_cost_tree()
```

---

## Solution 2: Label-Based Cost Aggregator with Budget Enforcement

Tag every call with structured labels (user, team, feature) and aggregate for chargeback and budget gating.

```python
import anthropic
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional
import threading

client = anthropic.Anthropic()

# Pricing per million tokens (claude-sonnet-4-6)
PRICING = {
    "input": 3.00,
    "output": 15.00,
    "cache_read": 0.30,
    "cache_write": 3.75,
}

@dataclass
class LabeledUsage:
    labels: dict
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    call_count: int = 0

    @property
    def cost_usd(self) -> float:
        return (
            self.input_tokens * PRICING["input"] / 1_000_000
            + self.output_tokens * PRICING["output"] / 1_000_000
            + self.cache_read_tokens * PRICING["cache_read"] / 1_000_000
            + self.cache_write_tokens * PRICING["cache_write"] / 1_000_000
        )

class CostAttributor:
    def __init__(self):
        self._lock = threading.Lock()
        self._records: list[LabeledUsage] = []
        self._budgets: dict[str, float] = {}  # label_key:value -> max_usd

    def set_budget(self, label_key: str, label_value: str, max_usd: float):
        self._budgets[f"{label_key}:{label_value}"] = max_usd

    def _check_budget(self, labels: dict):
        for k, v in labels.items():
            budget_key = f"{k}:{v}"
            if budget_key in self._budgets:
                spent = self.total_for_label(k, v)
                if spent >= self._budgets[budget_key]:
                    raise RuntimeError(
                        f"Budget exceeded for {budget_key}: "
                        f"${spent:.4f} >= ${self._budgets[budget_key]:.4f}"
                    )

    def call(self, messages: list, labels: dict, model="claude-sonnet-4-6", **kwargs):
        self._check_budget(labels)
        response = client.messages.create(
            model=model, max_tokens=1024, messages=messages, **kwargs
        )
        usage = response.usage
        record = LabeledUsage(
            labels=labels,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0),
            cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0),
            call_count=1,
        )
        with self._lock:
            self._records.append(record)
        return response

    def total_for_label(self, key: str, value: str) -> float:
        with self._lock:
            return sum(
                r.cost_usd for r in self._records if r.labels.get(key) == value
            )

    def report(self) -> dict:
        aggregated = defaultdict(lambda: defaultdict(float))
        with self._lock:
            for r in self._records:
                for k, v in r.labels.items():
                    aggregated[k][v] += r.cost_usd
        return {k: dict(v) for k, v in aggregated.items()}

# Usage
attributor = CostAttributor()
attributor.set_budget("user", "alice", max_usd=0.10)
attributor.set_budget("team", "search", max_usd=1.00)

try:
    attributor.call(
        [{"role": "user", "content": "Summarize this document"}],
        labels={"user": "alice", "team": "search", "feature": "summarize"},
    )
    attributor.call(
        [{"role": "user", "content": "Translate to French"}],
        labels={"user": "bob", "team": "search", "feature": "translate"},
    )
except RuntimeError as e:
    print(f"Budget gate: {e}")

report = attributor.report()
for dimension, breakdown in report.items():
    print(f"\n=== By {dimension} ===")
    for label, cost in sorted(breakdown.items(), key=lambda x: -x[1]):
        print(f"  {label}: ${cost:.6f}")
```

---

## Solution 3: Per-Request Cost Envelope with Upstream Propagation

Wrap every response in a cost envelope and propagate upstream so orchestrators see full subtree costs.

```python
import anthropic
from dataclasses import dataclass, field
from typing import Any

client = anthropic.Anthropic()

PRICING_PER_MTok = {
    "claude-haiku-4-5-20251001":  {"input": 0.80, "output": 4.00},
    "claude-sonnet-4-6":          {"input": 3.00, "output": 15.00},
    "claude-opus-4-6":            {"input": 15.00, "output": 75.00},
}

@dataclass
class CostEnvelope:
    model: str
    input_tokens: int
    output_tokens: int
    children: list["CostEnvelope"] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    @property
    def own_cost(self) -> float:
        pricing = PRICING_PER_MTok.get(self.model, {"input": 3.00, "output": 15.00})
        return (
            self.input_tokens * pricing["input"] / 1_000_000
            + self.output_tokens * pricing["output"] / 1_000_000
        )

    @property
    def total_cost(self) -> float:
        return self.own_cost + sum(c.total_cost for c in self.children)

    def add_child(self, child: "CostEnvelope"):
        self.children.append(child)

    def summary(self, indent=0) -> str:
        lines = [
            "  " * indent
            + f"[{self.model}] own=${self.own_cost:.6f} total=${self.total_cost:.6f} "
            + f"meta={self.metadata}"
        ]
        for c in self.children:
            lines.append(c.summary(indent + 1))
        return "\n".join(lines)

def agent_call(
    messages: list,
    model: str = "claude-sonnet-4-6",
    metadata: dict = None,
    sub_calls: list[CostEnvelope] = None,
) -> tuple[Any, CostEnvelope]:
    response = client.messages.create(model=model, max_tokens=512, messages=messages)
    envelope = CostEnvelope(
        model=model,
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
        metadata=metadata or {},
    )
    for child in (sub_calls or []):
        envelope.add_child(child)
    return response, envelope

# Simulated orchestrator → 2 sub-agents → merge
_, sub1_envelope = agent_call(
    [{"role": "user", "content": "Extract entities from text"}],
    model="claude-haiku-4-5-20251001",
    metadata={"task": "entity-extraction"},
)
_, sub2_envelope = agent_call(
    [{"role": "user", "content": "Classify sentiment"}],
    model="claude-haiku-4-5-20251001",
    metadata={"task": "sentiment"},
)
_, root_envelope = agent_call(
    [{"role": "user", "content": "Merge analysis results into report"}],
    model="claude-sonnet-4-6",
    metadata={"task": "merge", "workflow": "doc-analysis"},
    sub_calls=[sub1_envelope, sub2_envelope],
)

print(root_envelope.summary())
print(f"\nTotal pipeline cost: ${root_envelope.total_cost:.6f}")
```

---

## Solution 4: OpenTelemetry-Compatible Cost Exporter

Emit cost as OTEL spans so cost data lands in your existing observability stack (Jaeger, Honeycomb, Datadog).

```python
import anthropic
import time
import uuid
import json
from dataclasses import dataclass, field
from typing import Optional

client = anthropic.Anthropic()

@dataclass
class OtelSpan:
    trace_id: str
    span_id: str
    parent_span_id: Optional[str]
    name: str
    start_time_ns: int
    end_time_ns: int
    attributes: dict
    events: list = field(default_factory=list)

    def to_otel_dict(self) -> dict:
        return {
            "traceId": self.trace_id,
            "spanId": self.span_id,
            "parentSpanId": self.parent_span_id,
            "name": self.name,
            "startTimeUnixNano": str(self.start_time_ns),
            "endTimeUnixNano": str(self.end_time_ns),
            "attributes": [
                {"key": k, "value": {"stringValue": str(v)}}
                for k, v in self.attributes.items()
            ],
        }

class CostOtelExporter:
    def __init__(self, export_fn=None):
        self._spans: list[OtelSpan] = []
        self._export_fn = export_fn or (lambda spans: print(
            json.dumps([s.to_otel_dict() for s in spans], indent=2)
        ))

    def record_call(
        self,
        model: str,
        messages: list,
        trace_id: str = None,
        parent_span_id: str = None,
        span_name: str = "llm.call",
        extra_attrs: dict = None,
    ):
        trace_id = trace_id or str(uuid.uuid4()).replace("-", "")
        span_id = str(uuid.uuid4()).replace("-", "")[:16]
        t0 = time.time_ns()

        response = client.messages.create(model=model, max_tokens=512, messages=messages)

        t1 = time.time_ns()
        usage = response.usage

        # Pricing
        pricing = {
            "claude-haiku-4-5-20251001": (0.80, 4.00),
            "claude-sonnet-4-6": (3.00, 15.00),
            "claude-opus-4-6": (15.00, 75.00),
        }.get(model, (3.00, 15.00))
        cost = (
            usage.input_tokens * pricing[0] / 1_000_000
            + usage.output_tokens * pricing[1] / 1_000_000
        )

        attrs = {
            "llm.model": model,
            "llm.input_tokens": usage.input_tokens,
            "llm.output_tokens": usage.output_tokens,
            "llm.cost_usd": f"{cost:.8f}",
            "llm.latency_ms": (t1 - t0) // 1_000_000,
        }
        attrs.update(extra_attrs or {})

        span = OtelSpan(
            trace_id=trace_id,
            span_id=span_id,
            parent_span_id=parent_span_id,
            name=span_name,
            start_time_ns=t0,
            end_time_ns=t1,
            attributes=attrs,
        )
        self._spans.append(span)
        return response, span

    def flush(self):
        self._export_fn(self._spans)
        self._spans.clear()

# Usage
exporter = CostOtelExporter()
trace_id = uuid.uuid4().hex

_, root_span = exporter.record_call(
    model="claude-sonnet-4-6",
    messages=[{"role": "user", "content": "Plan a 3-step research workflow"}],
    trace_id=trace_id,
    span_name="orchestrator.plan",
    extra_attrs={"workflow": "research", "user": "alice"},
)

_, _ = exporter.record_call(
    model="claude-haiku-4-5-20251001",
    messages=[{"role": "user", "content": "Fetch and summarize paper 1"}],
    trace_id=trace_id,
    parent_span_id=root_span.span_id,
    span_name="sub-agent.summarize",
    extra_attrs={"step": "1"},
)

exporter.flush()
```

---

## Solution 5: Multi-Tenant Cost Ledger with Quota Enforcement

Maintain per-tenant cost ledgers with rolling quota windows and automatic throttling.

```python
import anthropic
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from threading import Lock
from typing import Optional

client = anthropic.Anthropic()

PRICING = {"input": 3.00, "output": 15.00}  # per million tokens, sonnet-4-6

@dataclass
class UsageRecord:
    tenant_id: str
    cost_usd: float
    timestamp: float

class MultiTenantCostLedger:
    def __init__(self, window_seconds: int = 3600):
        self._window = window_seconds
        self._records: dict[str, deque[UsageRecord]] = defaultdict(deque)
        self._quotas: dict[str, float] = {}  # tenant_id -> max_usd per window
        self._lock = Lock()

    def set_quota(self, tenant_id: str, max_usd_per_window: float):
        self._quotas[tenant_id] = max_usd_per_window

    def _evict_old(self, tenant_id: str):
        cutoff = time.time() - self._window
        q = self._records[tenant_id]
        while q and q[0].timestamp < cutoff:
            q.popleft()

    def _current_spend(self, tenant_id: str) -> float:
        self._evict_old(tenant_id)
        return sum(r.cost_usd for r in self._records[tenant_id])

    def _estimate_cost(self, input_tokens: int, output_tokens: int) -> float:
        return (
            input_tokens * PRICING["input"] / 1_000_000
            + output_tokens * PRICING["output"] / 1_000_000
        )

    def call(
        self,
        tenant_id: str,
        messages: list,
        model: str = "claude-sonnet-4-6",
        **kwargs,
    ):
        # Pre-flight quota check using token count estimate
        estimated_input = client.messages.count_tokens(
            model=model, messages=messages
        ).input_tokens
        estimated_cost = self._estimate_cost(estimated_input, estimated_input // 4)

        with self._lock:
            current = self._current_spend(tenant_id)
            quota = self._quotas.get(tenant_id, float("inf"))
            if current + estimated_cost > quota:
                raise RuntimeError(
                    f"Tenant {tenant_id!r} quota exceeded: "
                    f"${current:.4f} + ~${estimated_cost:.4f} > ${quota:.4f}/hr"
                )

        response = client.messages.create(model=model, max_tokens=1024, messages=messages, **kwargs)
        usage = response.usage
        actual_cost = self._estimate_cost(usage.input_tokens, usage.output_tokens)

        with self._lock:
            self._records[tenant_id].append(
                UsageRecord(tenant_id=tenant_id, cost_usd=actual_cost, timestamp=time.time())
            )
        return response

    def report(self) -> dict:
        result = {}
        with self._lock:
            for tenant_id in self._records:
                spend = self._current_spend(tenant_id)
                quota = self._quotas.get(tenant_id, float("inf"))
                result[tenant_id] = {
                    "spend_usd": round(spend, 6),
                    "quota_usd": quota,
                    "utilization_pct": round(spend / quota * 100, 1) if quota < float("inf") else None,
                }
        return result

# Usage
ledger = MultiTenantCostLedger(window_seconds=3600)
ledger.set_quota("tenant-A", max_usd_per_window=0.50)
ledger.set_quota("tenant-B", max_usd_per_window=2.00)

for tenant in ["tenant-A", "tenant-B", "tenant-A"]:
    try:
        ledger.call(
            tenant_id=tenant,
            messages=[{"role": "user", "content": f"Hello from {tenant}"}],
        )
    except RuntimeError as e:
        print(f"Blocked: {e}")

import json
print(json.dumps(ledger.report(), indent=2))
```

---

## Solution 6: Async Cost Attribution Pipeline with Background Aggregation

Non-blocking cost tracking for high-throughput async agents — fire-and-forget attribution with background aggregation.

```python
import anthropic
import asyncio
import time
import uuid
from dataclasses import dataclass
from typing import Optional
from collections import defaultdict

client = anthropic.AsyncAnthropic()

PRICING = {"claude-haiku-4-5-20251001": (0.80, 4.00), "claude-sonnet-4-6": (3.00, 15.00)}

@dataclass
class CostEvent:
    event_id: str
    model: str
    input_tokens: int
    output_tokens: int
    labels: dict
    timestamp: float

class AsyncCostPipeline:
    def __init__(self, flush_interval: float = 5.0):
        self._queue: asyncio.Queue[Optional[CostEvent]] = asyncio.Queue()
        self._aggregated: dict[str, dict] = defaultdict(lambda: {"cost": 0.0, "calls": 0})
        self._flush_interval = flush_interval
        self._task: Optional[asyncio.Task] = None

    async def start(self):
        self._task = asyncio.create_task(self._aggregate_loop())

    async def stop(self):
        await self._queue.put(None)  # sentinel
        if self._task:
            await self._task

    async def _aggregate_loop(self):
        while True:
            try:
                event = await asyncio.wait_for(self._queue.get(), timeout=self._flush_interval)
            except asyncio.TimeoutError:
                await self._flush()
                continue
            if event is None:
                await self._flush()
                break
            pricing = PRICING.get(event.model, (3.00, 15.00))
            cost = (
                event.input_tokens * pricing[0] / 1_000_000
                + event.output_tokens * pricing[1] / 1_000_000
            )
            for k, v in event.labels.items():
                key = f"{k}:{v}"
                self._aggregated[key]["cost"] += cost
                self._aggregated[key]["calls"] += 1

    async def _flush(self):
        if self._aggregated:
            print(f"\n[CostFlush @ {time.strftime('%H:%M:%S')}]")
            for label, stats in sorted(self._aggregated.items()):
                print(f"  {label}: ${stats['cost']:.6f} ({stats['calls']} calls)")

    async def tracked_call(
        self, messages: list, labels: dict, model: str = "claude-sonnet-4-6", **kwargs
    ):
        response = await client.messages.create(
            model=model, max_tokens=512, messages=messages, **kwargs
        )
        event = CostEvent(
            event_id=str(uuid.uuid4()),
            model=model,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            labels=labels,
            timestamp=time.time(),
        )
        await self._queue.put(event)
        return response

async def main():
    pipeline = AsyncCostPipeline(flush_interval=2.0)
    await pipeline.start()

    # Simulate concurrent agent calls across teams/features
    tasks = [
        pipeline.tracked_call(
            [{"role": "user", "content": f"Task {i}"}],
            labels={"team": f"team-{i % 2}", "feature": f"feature-{i % 3}"},
            model="claude-haiku-4-5-20251001",
        )
        for i in range(6)
    ]
    await asyncio.gather(*tasks)
    await pipeline.stop()

asyncio.run(main())
```

---

## Comparison

| Solution | Attribution Granularity | Budget Enforcement | Async-Safe | OTEL Compatible | Multi-Tenant |
|---|---|---|---|---|---|
| Span-Based Trace Context | Call tree / parent-child | No | Thread-local | Partial | No |
| Label-Based Aggregator | Arbitrary labels | Yes (hard gate) | Thread-safe | No | Via labels |
| Cost Envelope Propagation | Subtree rollup | No | No | No | No |
| OTEL Exporter | Span-level | No | Thread-safe | Yes | Via attributes |
| Multi-Tenant Ledger | Per-tenant rolling window | Yes (pre-flight) | Thread-safe | No | Yes |
| Async Pipeline | Label-based, batched | No | Yes (asyncio) | No | Via labels |

**Recommended starting point:** Solution 2 (Label-Based Aggregator) for most teams — simple, flexible labels, and hard budget enforcement. Add Solution 4 (OTEL Exporter) when you need cost data in your existing observability stack.
