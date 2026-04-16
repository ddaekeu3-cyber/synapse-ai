---
layout: solution
title: "Agent Doesn't Implement Response Envelope with Metadata"
category: general
description: "Wrap every agent response in a structured envelope containing request ID, model info, token usage, latency, and version metadata—enabling traceability, cost attribution, and downstream processing without parsing the payload."
tags: [response-envelope, metadata, traceability, structured-output, observability]
---

# Agent Doesn't Implement Response Envelope with Metadata

## Problem

Returning raw text responses with no metadata makes it impossible to trace which request produced which response, calculate costs, debug latency issues, or route responses based on model/version in production systems.

## Solution Options

### Option 1: Minimal Response Envelope

```python
import anthropic
import time
import uuid
from dataclasses import dataclass, asdict

client = anthropic.Anthropic()

@dataclass
class AgentResponse:
    request_id: str
    content: str
    model: str
    input_tokens: int
    output_tokens: int
    latency_ms: float
    timestamp: float

def ask(prompt: str, model: str = "claude-haiku-4-5-20251001") -> AgentResponse:
    request_id = str(uuid.uuid4())[:12]
    t0 = time.perf_counter()

    resp = client.messages.create(
        model=model,
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}]
    )

    return AgentResponse(
        request_id=request_id,
        content=resp.content[0].text,
        model=resp.model,
        input_tokens=resp.usage.input_tokens,
        output_tokens=resp.usage.output_tokens,
        latency_ms=round((time.perf_counter() - t0) * 1000, 2),
        timestamp=time.time()
    )

for prompt in ["What is Redis?", "Explain CAP theorem.", "What is a bloom filter?"]:
    response = ask(prompt)
    print(f"[{response.request_id}] {response.latency_ms}ms | "
          f"in={response.input_tokens} out={response.output_tokens} | "
          f"{response.content[:60]}...")

# Expected Token Savings: N/A; enables cost tracking without extra API calls
# Environment: any production agent; baseline for observability infrastructure
```

### Option 2: Rich Envelope with Cost Estimation and Session Context

```python
import anthropic
import time
import uuid
import json
from dataclasses import dataclass, field, asdict

client = anthropic.Anthropic()

# Approximate cost per million tokens (USD)
MODEL_COSTS = {
    "claude-haiku-4-5-20251001": {"input": 0.80, "output": 4.00},
    "claude-sonnet-4-6":          {"input": 3.00, "output": 15.00},
    "claude-opus-4-6":            {"input": 15.00, "output": 75.00},
}

@dataclass
class CostEstimate:
    input_cost_usd: float
    output_cost_usd: float
    total_cost_usd: float

@dataclass
class ResponseEnvelope:
    request_id: str
    session_id: str
    turn_number: int
    content: str
    model: str
    stop_reason: str
    input_tokens: int
    output_tokens: int
    latency_ms: float
    cost: CostEstimate
    tags: dict = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)

    @property
    def cost_summary(self) -> str:
        return f"${self.cost.total_cost_usd:.6f}"

class SessionClient:
    def __init__(self, session_id: str | None = None, model: str = "claude-haiku-4-5-20251001"):
        self.session_id = session_id or str(uuid.uuid4())[:8]
        self.model = model
        self.turn_number = 0
        self.total_cost = 0.0

    def ask(self, prompt: str, **tags) -> ResponseEnvelope:
        self.turn_number += 1
        request_id = f"{self.session_id}_{self.turn_number:03d}"
        t0 = time.perf_counter()

        resp = client.messages.create(
            model=self.model,
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}]
        )

        costs = MODEL_COSTS.get(self.model, {"input": 0, "output": 0})
        cost = CostEstimate(
            input_cost_usd=round(resp.usage.input_tokens / 1_000_000 * costs["input"], 8),
            output_cost_usd=round(resp.usage.output_tokens / 1_000_000 * costs["output"], 8),
            total_cost_usd=round(
                (resp.usage.input_tokens * costs["input"] + resp.usage.output_tokens * costs["output"]) / 1_000_000, 8
            )
        )
        self.total_cost += cost.total_cost_usd

        return ResponseEnvelope(
            request_id=request_id,
            session_id=self.session_id,
            turn_number=self.turn_number,
            content=resp.content[0].text,
            model=resp.model,
            stop_reason=resp.stop_reason,
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens,
            latency_ms=round((time.perf_counter() - t0) * 1000, 2),
            cost=cost,
            tags=tags
        )

session = SessionClient(model="claude-haiku-4-5-20251001")
for prompt, tag in [("What is Kafka?", "intro"), ("Explain partitioning.", "deep-dive"), ("What are consumer groups?", "deep-dive")]:
    env = session.ask(prompt, topic=tag)
    print(f"[{env.request_id}] {env.latency_ms}ms | {env.cost_summary} | {env.content[:60]}...")

print(f"\nSession total cost: ${session.total_cost:.6f}")

# Expected Token Savings: N/A; enables per-session cost attribution and budget enforcement
# Environment: multi-tenant SaaS, cost-center billing, session-level budget caps
```

### Option 3: Envelope with Routing Metadata for Multi-Model Pipelines

```python
import anthropic
import time
import uuid
from dataclasses import dataclass
from enum import Enum

client = anthropic.Anthropic()

class RoutingTier(Enum):
    FAST = "fast"       # haiku
    BALANCED = "balanced"  # sonnet
    POWERFUL = "powerful"  # opus

MODEL_MAP = {
    RoutingTier.FAST: "claude-haiku-4-5-20251001",
    RoutingTier.BALANCED: "claude-sonnet-4-6",
    RoutingTier.POWERFUL: "claude-opus-4-6",
}

@dataclass
class RoutingMetadata:
    requested_tier: str
    actual_model: str
    routing_reason: str
    was_upgraded: bool
    was_downgraded: bool

@dataclass
class PipelineEnvelope:
    request_id: str
    stage: str
    content: str
    routing: RoutingMetadata
    input_tokens: int
    output_tokens: int
    latency_ms: float
    downstream_hint: str  # hint for next stage

def classify_complexity(prompt: str) -> RoutingTier:
    """Simple heuristic — replace with ML classifier in production."""
    length = len(prompt)
    has_complex_keywords = any(kw in prompt.lower() for kw in
        ["architecture", "design", "tradeoff", "analyze", "compare", "security", "production"])
    if length > 500 or has_complex_keywords:
        return RoutingTier.BALANCED
    return RoutingTier.FAST

def pipeline_call(prompt: str, stage: str, preferred_tier: RoutingTier | None = None) -> PipelineEnvelope:
    auto_tier = classify_complexity(prompt)
    actual_tier = preferred_tier or auto_tier
    model = MODEL_MAP[actual_tier]
    request_id = str(uuid.uuid4())[:10]

    t0 = time.perf_counter()
    resp = client.messages.create(
        model=model,
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}]
    )
    latency = round((time.perf_counter() - t0) * 1000, 2)

    # Determine downstream hint based on output characteristics
    output_len = resp.usage.output_tokens
    downstream_hint = "summarize" if output_len > 400 else ("expand" if output_len < 50 else "passthrough")

    return PipelineEnvelope(
        request_id=request_id,
        stage=stage,
        content=resp.content[0].text,
        routing=RoutingMetadata(
            requested_tier=preferred_tier.value if preferred_tier else "auto",
            actual_model=model,
            routing_reason="explicit" if preferred_tier else f"auto-classified-as-{auto_tier.value}",
            was_upgraded=(preferred_tier and actual_tier.value > preferred_tier.value) or False,
            was_downgraded=(preferred_tier and actual_tier.value < preferred_tier.value) or False
        ),
        input_tokens=resp.usage.input_tokens,
        output_tokens=resp.usage.output_tokens,
        latency_ms=latency,
        downstream_hint=downstream_hint
    )

# Simulate multi-stage pipeline
stage1 = pipeline_call("What is eventual consistency?", stage="intake")
print(f"Stage 1 [{stage1.routing.actual_model}] -> hint: {stage1.downstream_hint}")

if stage1.downstream_hint == "summarize":
    stage2 = pipeline_call(f"Summarize in 2 sentences: {stage1.content}", stage="compress")
else:
    stage2 = pipeline_call(f"Expand with examples: {stage1.content}", stage="expand")

print(f"Stage 2 [{stage2.routing.actual_model}] {stage2.latency_ms}ms")
print(f"\nFinal: {stage2.content[:200]}")

# Expected Token Savings: auto-routing to haiku saves ~70% vs always using sonnet
# Environment: multi-stage pipelines, intelligent routing systems, cost-optimized architectures
```

### Option 4: Envelope with Schema Version and Backward Compatibility

```python
import anthropic
import time
import uuid
import json
from dataclasses import dataclass, field

client = anthropic.Anthropic()

ENVELOPE_VERSION = "2.1.0"

@dataclass
class EnvelopeV2:
    """Versioned envelope — consumers can check version before parsing."""
    envelope_version: str
    request_id: str
    content: str
    model: str
    input_tokens: int
    output_tokens: int
    latency_ms: float
    # v2.0 additions
    stop_reason: str = "end_turn"
    # v2.1 additions
    content_type: str = "text"
    warnings: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(self.__dict__, indent=2)

    @classmethod
    def from_json(cls, data: str | dict) -> "EnvelopeV2":
        """Backward-compatible deserialization."""
        if isinstance(data, str):
            data = json.loads(data)
        version = data.get("envelope_version", "1.0.0")

        # Handle v1.x envelopes missing new fields
        if version.startswith("1."):
            data.setdefault("stop_reason", "end_turn")
            data.setdefault("content_type", "text")
            data.setdefault("warnings", [])
            data["warnings"].append(f"Upcast from envelope v{version}")

        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

def create_envelope(prompt: str, model: str = "claude-haiku-4-5-20251001") -> EnvelopeV2:
    warnings = []
    t0 = time.perf_counter()

    resp = client.messages.create(
        model=model,
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}]
    )
    latency = round((time.perf_counter() - t0) * 1000, 2)

    if resp.usage.output_tokens > 450:
        warnings.append("output_near_max_tokens")
    if latency > 3000:
        warnings.append("high_latency")

    return EnvelopeV2(
        envelope_version=ENVELOPE_VERSION,
        request_id=str(uuid.uuid4())[:10],
        content=resp.content[0].text,
        model=resp.model,
        input_tokens=resp.usage.input_tokens,
        output_tokens=resp.usage.output_tokens,
        latency_ms=latency,
        stop_reason=resp.stop_reason,
        content_type="text",
        warnings=warnings
    )

# Create and serialize
env = create_envelope("Explain database sharding strategies.")
serialized = env.to_json()
print(f"Envelope v{env.envelope_version} ({len(serialized)} bytes)")
print(f"  request_id={env.request_id} tokens={env.input_tokens}+{env.output_tokens} latency={env.latency_ms}ms")
if env.warnings:
    print(f"  warnings={env.warnings}")

# Simulate deserializing an old v1 envelope
old_envelope_json = json.dumps({
    "envelope_version": "1.0.0",
    "request_id": "abc123",
    "content": "old response",
    "model": "claude-haiku-4-5-20251001",
    "input_tokens": 10,
    "output_tokens": 20,
    "latency_ms": 500.0
})
old = EnvelopeV2.from_json(old_envelope_json)
print(f"\nUpcast old envelope: warnings={old.warnings}")

# Expected Token Savings: versioning prevents deserialization failures after upgrades
# Environment: long-lived systems, microservice consumers, stored response archives
```

### Option 5: Streaming Envelope with Progressive Metadata

```python
import anthropic
import time
import uuid
from dataclasses import dataclass, field

client = anthropic.Anthropic()

@dataclass
class StreamingEnvelope:
    request_id: str
    model: str
    started_at: float = field(default_factory=time.time)
    first_token_ms: float | None = None
    total_latency_ms: float | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    content: str = ""
    is_complete: bool = False

    @property
    def ttft_ms(self) -> float | None:
        """Time to first token."""
        return self.first_token_ms

    def finalize(self, input_tokens: int, output_tokens: int) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.total_latency_ms = round((time.time() - self.started_at) * 1000, 2)
        self.is_complete = True

    def summary(self) -> str:
        return (
            f"request_id={self.request_id} "
            f"ttft={self.ttft_ms:.0f}ms "
            f"total={self.total_latency_ms}ms "
            f"in={self.input_tokens} out={self.output_tokens}"
        )

def stream_with_envelope(prompt: str, model: str = "claude-haiku-4-5-20251001") -> StreamingEnvelope:
    envelope = StreamingEnvelope(
        request_id=str(uuid.uuid4())[:10],
        model=model
    )
    t0 = time.perf_counter()

    with client.messages.stream(
        model=model,
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}]
    ) as stream:
        for i, chunk in enumerate(stream.text_stream):
            if i == 0:
                envelope.first_token_ms = round((time.perf_counter() - t0) * 1000, 2)
            envelope.content += chunk

        final = stream.get_final_message()
        envelope.finalize(
            input_tokens=final.usage.input_tokens,
            output_tokens=final.usage.output_tokens
        )

    return envelope

prompts = [
    "What is the difference between TCP and UDP?",
    "Explain consensus algorithms in 2 sentences.",
    "What is a merkle tree?"
]

for prompt in prompts:
    env = stream_with_envelope(prompt)
    print(f"[{env.request_id}] {env.summary()}")

# Expected Token Savings: TTFT tracking enables streaming-specific latency optimization
# Environment: streaming APIs, UX latency measurement, real-time chat interfaces
```

### Option 6: Envelope Registry with Aggregated Analytics

```python
import anthropic
import time
import uuid
import sqlite3
import json
from dataclasses import dataclass
from statistics import mean, median

client = anthropic.Anthropic()

@dataclass
class EnvelopeRecord:
    request_id: str
    prompt_hash: str
    model: str
    input_tokens: int
    output_tokens: int
    latency_ms: float
    cost_usd: float
    timestamp: float
    tag: str

MODEL_COSTS = {"claude-haiku-4-5-20251001": (0.80, 4.00), "claude-sonnet-4-6": (3.00, 15.00)}

def init_registry(path: str = "/tmp/envelope_registry.db") -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS envelopes (
            request_id TEXT PRIMARY KEY,
            prompt_hash TEXT,
            model TEXT,
            input_tokens INTEGER,
            output_tokens INTEGER,
            latency_ms REAL,
            cost_usd REAL,
            timestamp REAL,
            tag TEXT
        )
    """)
    conn.commit()
    return conn

def ask_with_registry(prompt: str, tag: str, conn: sqlite3.Connection,
                       model: str = "claude-haiku-4-5-20251001") -> tuple[str, EnvelopeRecord]:
    import hashlib
    prompt_hash = hashlib.md5(prompt.encode()).hexdigest()[:8]
    request_id = str(uuid.uuid4())[:10]
    costs = MODEL_COSTS.get(model, (0, 0))

    t0 = time.perf_counter()
    resp = client.messages.create(
        model=model,
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}]
    )
    latency = round((time.perf_counter() - t0) * 1000, 2)

    cost = round((resp.usage.input_tokens * costs[0] + resp.usage.output_tokens * costs[1]) / 1_000_000, 8)
    record = EnvelopeRecord(
        request_id=request_id,
        prompt_hash=prompt_hash,
        model=model,
        input_tokens=resp.usage.input_tokens,
        output_tokens=resp.usage.output_tokens,
        latency_ms=latency,
        cost_usd=cost,
        timestamp=time.time(),
        tag=tag
    )
    conn.execute(
        "INSERT INTO envelopes VALUES (?,?,?,?,?,?,?,?,?)",
        (record.request_id, record.prompt_hash, record.model, record.input_tokens,
         record.output_tokens, record.latency_ms, record.cost_usd, record.timestamp, record.tag)
    )
    conn.commit()
    return resp.content[0].text, record

def analytics_report(conn: sqlite3.Connection) -> None:
    rows = conn.execute("SELECT model, input_tokens, output_tokens, latency_ms, cost_usd, tag FROM envelopes").fetchall()
    if not rows:
        return
    latencies = [r[3] for r in rows]
    costs = [r[4] for r in rows]
    total_tokens = sum(r[1] + r[2] for r in rows)
    print(f"\n=== Analytics Report ({len(rows)} requests) ===")
    print(f"  Latency: p50={median(latencies):.0f}ms avg={mean(latencies):.0f}ms")
    print(f"  Cost:    total=${sum(costs):.6f} avg=${mean(costs):.6f}")
    print(f"  Tokens:  total={total_tokens}")
    tags = set(r[5] for r in rows)
    for tag in tags:
        tag_rows = [r for r in rows if r[5] == tag]
        tag_cost = sum(r[4] for r in tag_rows)
        print(f"  [{tag}] {len(tag_rows)} requests, ${tag_cost:.6f}")

conn = init_registry()
queries = [
    ("What is sharding?", "education"),
    ("Explain replication lag.", "education"),
    ("List 3 caching strategies.", "reference"),
    ("What is write-ahead logging?", "reference"),
]
for prompt, tag in queries:
    _, record = ask_with_registry(prompt, tag, conn)
    print(f"[{record.request_id}] {record.latency_ms}ms ${record.cost_usd:.6f} [{tag}]")

analytics_report(conn)
conn.close()

# Expected Token Savings: N/A; enables query-level cost attribution and tag-based optimization
# Environment: multi-tenant billing, usage analytics dashboards, cost optimization workflows
```

## Comparison

| Option | Key Metadata | Persistence | Best For |
|--------|-------------|-------------|----------|
| 1 | ID, tokens, latency | None | Quick instrumentation |
| 2 | Cost estimate, session tracking | In-memory | SaaS billing, budget caps |
| 3 | Routing tier, downstream hints | None | Multi-model pipelines |
| 4 | Schema version, warnings | Serializable | Long-lived systems, archives |
| 5 | TTFT, streaming-specific metrics | None | Streaming UX optimization |
| 6 | Full analytics registry | SQLite | Cost dashboards, billing |
