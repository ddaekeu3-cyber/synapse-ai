---
layout: solution
title: "Agent Doesn't Implement SLA Enforcement with Auto-Escalation"
category: reliability
description: "Enforce response time and quality SLAs for agent operations, automatically escalating to faster models, human review, or fallback responses when SLA thresholds are at risk."
tags: [reliability, sla, escalation, latency, quality, monitoring]
---

Agents that operate without SLA awareness silently breach latency and quality commitments. A slow tool call or overloaded model pushes the total response time past the agreed limit — and no one knows until the user complains. SLA enforcement monitors in-flight operations against time budgets and quality thresholds, escalating automatically (faster model, cached response, human handoff) before the deadline is missed.

## Option 1: Deadline-Propagating Request Context

Attach an absolute deadline to every request at ingestion. Pass the deadline through each processing step. Before each model call or tool call, check remaining time — if insufficient, escalate to a faster alternative or return a partial result.

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass

@dataclass
class SLAContext:
    request_id: str
    deadline: float           # absolute epoch time
    sla_ms: float             # original SLA in milliseconds
    escalation_threshold: float = 0.8  # escalate when this fraction of SLA is used

    @property
    def remaining_ms(self) -> float:
        return (self.deadline - time.monotonic()) * 1000

    @property
    def elapsed_ms(self) -> float:
        return self.sla_ms - self.remaining_ms

    @property
    def should_escalate(self) -> bool:
        return self.elapsed_ms >= self.sla_ms * self.escalation_threshold

    @property
    def is_breached(self) -> bool:
        return time.monotonic() > self.deadline

def create_sla_context(request_id: str, sla_ms: float = 5000) -> SLAContext:
    return SLAContext(
        request_id=request_id,
        deadline=time.monotonic() + sla_ms / 1000,
        sla_ms=sla_ms,
    )

def select_model(sla: SLAContext) -> str:
    remaining = sla.remaining_ms
    if remaining > 3000:
        return "claude-sonnet-4-6"
    elif remaining > 1500:
        return "claude-haiku-4-5-20251001"
    else:
        return "claude-haiku-4-5-20251001"  # fastest available

def call_with_sla(sla: SLAContext, messages: list[dict], system: str = "") -> str:
    if sla.is_breached:
        return f"[SLA BREACHED] Request {sla.request_id} exceeded {sla.sla_ms:.0f}ms SLA"

    model = select_model(sla)
    if sla.should_escalate:
        print(f"[SLA] {sla.elapsed_ms:.0f}/{sla.sla_ms:.0f}ms used — escalating to {model}")

    client = anthropic.Anthropic()
    kwargs = {
        "model": model,
        "max_tokens": 512 if sla.remaining_ms > 2000 else 128,
        "messages": messages,
    }
    if system:
        kwargs["system"] = system

    response = client.messages.create(**kwargs)
    elapsed = sla.elapsed_ms
    status = "OK" if elapsed < sla.sla_ms else "BREACHED"
    print(f"[SLA] {status}: {elapsed:.0f}/{sla.sla_ms:.0f}ms, model={model}")
    return response.content[0].text

def run_pipeline_with_sla(user_query: str, sla_ms: float = 5000) -> str:
    import uuid
    sla = create_sla_context(str(uuid.uuid4())[:8], sla_ms)

    # Step 1: Classification
    if sla.is_breached:
        return "[SLA BREACHED] Before classification"
    classification = call_with_sla(
        sla,
        [{"role": "user", "content": f"Classify in one word (question/task/chat): {user_query}"}],
    )

    # Step 2: Main response
    if sla.is_breached:
        return f"[PARTIAL] Classification: {classification}. SLA breached before main response."
    result = call_with_sla(
        sla,
        [{"role": "user", "content": user_query}],
        system=f"You are handling a {classification.strip()} request. Be concise.",
    )
    return result

if __name__ == "__main__":
    print(run_pipeline_with_sla("What is the difference between asyncio and threading in Python?", sla_ms=8000))

# Expected Token Savings: Reduces max_tokens on escalated calls; prevents expensive slow model calls near deadline
# Environment: pip install anthropic
```

## Option 2: Async SLA Monitor with Parallel Fallback

Run the primary model call and an SLA watchdog concurrently. When the watchdog detects the deadline approaching, it pre-emptively launches a fallback call. Whichever completes first (primary within SLA, or fallback) wins. The loser is cancelled.

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass

@dataclass
class SLAConfig:
    target_ms: float = 3000
    warn_at_ms: float = 2000    # start fallback at this elapsed time
    hard_limit_ms: float = 4000 # cancel and return error after this

async def primary_call(
    client: anthropic.AsyncAnthropic,
    messages: list[dict],
    system: str,
) -> str:
    response = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=system,
        messages=messages,
    )
    return response.content[0].text

async def fallback_call(
    client: anthropic.AsyncAnthropic,
    messages: list[dict],
) -> str:
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=messages,
    )
    return f"[FALLBACK] {response.content[0].text}"

async def call_with_sla_monitor(
    messages: list[dict],
    system: str = "",
    config: SLAConfig = None,
) -> tuple[str, str]:  # (result, status)
    config = config or SLAConfig()
    client = anthropic.AsyncAnthropic()
    start = time.monotonic()

    primary_task = asyncio.create_task(primary_call(client, messages, system))
    fallback_task: asyncio.Task | None = None
    result_source = "primary"

    async def watchdog():
        nonlocal fallback_task, result_source
        await asyncio.sleep(config.warn_at_ms / 1000)
        if not primary_task.done():
            elapsed = (time.monotonic() - start) * 1000
            print(f"[SLA-Monitor] {elapsed:.0f}ms elapsed — launching fallback")
            fallback_task = asyncio.create_task(fallback_call(client, messages))

    watchdog_task = asyncio.create_task(watchdog())

    try:
        done, pending = await asyncio.wait(
            {primary_task},
            timeout=config.hard_limit_ms / 1000,
        )
        if primary_task in done and not primary_task.exception():
            elapsed_ms = (time.monotonic() - start) * 1000
            watchdog_task.cancel()
            if fallback_task:
                fallback_task.cancel()
            status = "ON_TIME" if elapsed_ms <= config.target_ms else "SLA_WARNING"
            return primary_task.result(), status

        # Primary timed out — wait for fallback
        if fallback_task:
            try:
                result = await asyncio.wait_for(fallback_task, timeout=2.0)
                elapsed_ms = (time.monotonic() - start) * 1000
                print(f"[SLA-Monitor] Primary timed out at {elapsed_ms:.0f}ms, using fallback")
                return result, "SLA_FALLBACK"
            except asyncio.TimeoutError:
                pass

        return "[SLA BREACHED] No response within hard limit", "SLA_BREACHED"

    finally:
        watchdog_task.cancel()
        primary_task.cancel()

async def main():
    messages = [{"role": "user", "content": "Explain the CAP theorem in distributed systems."}]
    result, status = await call_with_sla_monitor(
        messages,
        system="You are a distributed systems expert. Be concise.",
        config=SLAConfig(target_ms=3000, warn_at_ms=1500, hard_limit_ms=4000),
    )
    print(f"Status: {status}\nResult: {result[:200]}")

if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: Fallback uses 75% fewer tokens than primary; activates only when needed
# Environment: pip install anthropic
```

## Option 3: Quality-Based SLA with Score Gating

SLAs cover not just latency but quality. After generating a response, score it against quality criteria (completeness, relevance, format compliance). If below threshold and time budget allows, regenerate with refined instructions. Track both latency and quality SLA compliance.

```python
import anthropic
import time
from dataclasses import dataclass

@dataclass
class QualitySLA:
    latency_sla_ms: float = 5000
    min_quality_score: float = 0.7
    max_regenerations: int = 2

@dataclass
class SLAResult:
    response: str
    latency_ms: float
    quality_score: float
    regenerations: int
    latency_met: bool
    quality_met: bool

def score_response(client: anthropic.Anthropic, query: str, response: str) -> float:
    """Use Haiku to score response quality 0.0-1.0."""
    scoring_response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=50,
        messages=[{
            "role": "user",
            "content": f"Score this response to '{query}' from 0.0 to 1.0 for relevance, completeness, and clarity. Reply with ONLY the decimal number.\n\nResponse: {response[:500]}",
        }],
    )
    try:
        return float(scoring_response.content[0].text.strip())
    except ValueError:
        return 0.5

def generate_response(client: anthropic.Anthropic, messages: list[dict], system: str, attempt: int) -> str:
    refinements = ["", " Be more complete and specific.", " Focus on practical examples and key points."]
    refined_system = system + refinements[min(attempt, len(refinements)-1)]
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=refined_system,
        messages=messages,
    )
    return response.content[0].text

def call_with_quality_sla(
    messages: list[dict],
    system: str = "",
    sla: QualitySLA = None,
) -> SLAResult:
    sla = sla or QualitySLA()
    client = anthropic.Anthropic()
    start = time.monotonic()
    query = messages[-1]["content"] if messages else ""
    best_response = ""
    best_score = 0.0
    regenerations = 0

    for attempt in range(sla.max_regenerations + 1):
        elapsed_ms = (time.monotonic() - start) * 1000
        remaining_ms = sla.latency_sla_ms - elapsed_ms

        if remaining_ms < 500:  # not enough time for another attempt
            print(f"[QualitySLA] Time budget exhausted ({elapsed_ms:.0f}ms), using best response")
            break

        response = generate_response(client, messages, system, attempt)
        if attempt > 0:
            regenerations += 1

        score = score_response(client, query, response)
        print(f"[QualitySLA] Attempt {attempt+1}: quality={score:.2f}, elapsed={elapsed_ms:.0f}ms")

        if score > best_score:
            best_score = score
            best_response = response

        if score >= sla.min_quality_score:
            break

    final_elapsed = (time.monotonic() - start) * 1000
    return SLAResult(
        response=best_response,
        latency_ms=final_elapsed,
        quality_score=best_score,
        regenerations=regenerations,
        latency_met=final_elapsed <= sla.latency_sla_ms,
        quality_met=best_score >= sla.min_quality_score,
    )

if __name__ == "__main__":
    result = call_with_quality_sla(
        messages=[{"role": "user", "content": "Explain database indexing and when to use composite indexes."}],
        system="You are a database expert.",
        sla=QualitySLA(latency_sla_ms=10000, min_quality_score=0.75, max_regenerations=2),
    )
    print(f"Quality: {result.quality_score:.2f} | Latency: {result.latency_ms:.0f}ms | Regen: {result.regenerations}")
    print(f"SLA Met: latency={result.latency_met}, quality={result.quality_met}")
    print(f"\n{result.response[:300]}")

# Expected Token Savings: Avoids expensive final-model calls when Haiku quality is sufficient
# Environment: pip install anthropic
```

## Option 4: SLA Tracking Registry with Dashboard Reporting

Maintain a registry of SLA metrics per operation type. Record every request's actual latency, quality, and escalation events. Generate periodic compliance reports showing which operations breach SLAs most frequently.

```python
import anthropic
import time
import json
from dataclasses import dataclass, field, asdict
from collections import defaultdict
from statistics import median, mean

@dataclass
class SLADefinition:
    operation: str
    target_p50_ms: float
    target_p95_ms: float
    target_p99_ms: float

@dataclass
class SLARecord:
    operation: str
    request_id: str
    latency_ms: float
    model_used: str
    escalated: bool
    sla_breached: bool
    timestamp: float = field(default_factory=time.time)

class SLARegistry:
    def __init__(self, definitions: list[SLADefinition]):
        self.definitions = {d.operation: d for d in definitions}
        self._records: dict[str, list[SLARecord]] = defaultdict(list)

    def record(self, record: SLARecord) -> None:
        self._records[record.operation].append(record)

    def compliance_report(self, operation: str) -> dict:
        records = self._records.get(operation, [])
        if not records:
            return {"operation": operation, "samples": 0}

        defn = self.definitions.get(operation)
        latencies = sorted(r.latency_ms for r in records)
        n = len(latencies)

        p50 = latencies[int(n * 0.5)]
        p95 = latencies[int(n * 0.95)] if n >= 20 else max(latencies)
        p99 = latencies[int(n * 0.99)] if n >= 100 else max(latencies)

        return {
            "operation": operation,
            "samples": n,
            "p50_ms": round(p50, 1),
            "p95_ms": round(p95, 1),
            "p99_ms": round(p99, 1),
            "mean_ms": round(mean(latencies), 1),
            "escalation_rate": f"{sum(1 for r in records if r.escalated)/n*100:.1f}%",
            "breach_rate": f"{sum(1 for r in records if r.sla_breached)/n*100:.1f}%",
            "sla_p50_ok": defn and p50 <= defn.target_p50_ms,
            "sla_p95_ok": defn and p95 <= defn.target_p95_ms,
        }

    def full_report(self) -> list[dict]:
        return [self.compliance_report(op) for op in self._records]

registry = SLARegistry([
    SLADefinition("chat_response", target_p50_ms=1500, target_p95_ms=3000, target_p99_ms=5000),
    SLADefinition("tool_call", target_p50_ms=800, target_p95_ms=2000, target_p99_ms=4000),
    SLADefinition("summarization", target_p50_ms=2000, target_p95_ms=4000, target_p99_ms=6000),
])

def tracked_call(
    operation: str,
    messages: list[dict],
    system: str = "",
    sla_ms: float = 3000,
    request_id: str = "req",
) -> str:
    import uuid
    rid = f"{request_id}_{uuid.uuid4().hex[:6]}"
    client = anthropic.Anthropic()
    start = time.monotonic()
    escalated = False

    elapsed = (time.monotonic() - start) * 1000
    model = "claude-haiku-4-5-20251001" if elapsed < sla_ms * 0.5 else "claude-haiku-4-5-20251001"
    if elapsed > sla_ms * 0.7:
        escalated = True
        model = "claude-haiku-4-5-20251001"

    kwargs = {"model": model, "max_tokens": 256, "messages": messages}
    if system:
        kwargs["system"] = system
    response = client.messages.create(**kwargs)

    latency_ms = (time.monotonic() - start) * 1000
    registry.record(SLARecord(
        operation=operation,
        request_id=rid,
        latency_ms=latency_ms,
        model_used=model,
        escalated=escalated,
        sla_breached=latency_ms > sla_ms,
    ))
    return response.content[0].text

if __name__ == "__main__":
    queries = [
        "What is asyncio?",
        "Explain the GIL",
        "How does garbage collection work in Python?",
        "What are decorators?",
        "Explain metaclasses",
    ]
    for q in queries:
        tracked_call("chat_response", [{"role": "user", "content": q}], sla_ms=5000)
        tracked_call("summarization", [{"role": "user", "content": f"Summarize in one sentence: {q}"}], sla_ms=3000)

    print("\n=== SLA Compliance Report ===")
    for report in registry.full_report():
        print(json.dumps(report, indent=2))

# Expected Token Savings: Identifies breach patterns enabling targeted optimization
# Environment: pip install anthropic
```

## Option 5: Tiered Escalation with Human Handoff

Define escalation tiers: (1) faster model, (2) cached response, (3) human review queue. Progress through tiers automatically based on latency consumption and quality score. Log escalations for SLA auditing.

```python
import anthropic
import time
import json
from dataclasses import dataclass
from enum import Enum

class EscalationTier(Enum):
    PRIMARY = "primary"         # full quality primary model
    FAST_MODEL = "fast_model"   # smaller model, faster
    CACHED = "cached"           # return best cached response
    HUMAN = "human_queue"       # queue for human review

@dataclass
class EscalationEvent:
    tier: EscalationTier
    reason: str
    elapsed_ms: float

_response_cache: dict[str, str] = {}

def cache_key(messages: list[dict]) -> str:
    import hashlib
    content = json.dumps([m["content"] for m in messages[-2:]])
    return hashlib.md5(content.encode()).hexdigest()[:16]

def get_cached(messages: list[dict]) -> str | None:
    return _response_cache.get(cache_key(messages))

def store_cached(messages: list[dict], response: str) -> None:
    _response_cache[cache_key(messages)] = response

def queue_for_human(request_id: str, messages: list[dict], reason: str) -> str:
    # In production: push to ticketing system, Slack, PagerDuty, etc.
    print(f"[HumanQueue] Request {request_id} queued: {reason}")
    return f"Your request is being reviewed by our team. Reference: {request_id}"

def call_with_tiered_escalation(
    request_id: str,
    messages: list[dict],
    system: str = "",
    sla_ms: float = 5000,
) -> tuple[str, list[EscalationEvent]]:
    client = anthropic.Anthropic()
    start = time.monotonic()
    events: list[EscalationEvent] = []

    def elapsed() -> float:
        return (time.monotonic() - start) * 1000

    def remaining() -> float:
        return sla_ms - elapsed()

    def try_model(model: str, max_tokens: int) -> str | None:
        if remaining() < 300:
            return None
        try:
            kwargs = {"model": model, "max_tokens": max_tokens, "messages": messages}
            if system:
                kwargs["system"] = system
            r = client.messages.create(**kwargs)
            return r.content[0].text
        except Exception as e:
            print(f"[Escalation] {model} failed: {e}")
            return None

    # Tier 1: Primary
    if remaining() > sla_ms * 0.6:
        result = try_model("claude-sonnet-4-6", 512)
        if result:
            store_cached(messages, result)
            return result, events

    # Tier 2: Fast model
    events.append(EscalationEvent(EscalationTier.FAST_MODEL, f"primary slow ({elapsed():.0f}ms)", elapsed()))
    print(f"[Escalation] → fast_model at {elapsed():.0f}ms")
    if remaining() > 400:
        result = try_model("claude-haiku-4-5-20251001", 128)
        if result:
            store_cached(messages, result)
            return result, events

    # Tier 3: Cache
    events.append(EscalationEvent(EscalationTier.CACHED, f"all models slow ({elapsed():.0f}ms)", elapsed()))
    print(f"[Escalation] → cache at {elapsed():.0f}ms")
    cached = get_cached(messages)
    if cached:
        return f"[CACHED] {cached}", events

    # Tier 4: Human queue
    events.append(EscalationEvent(EscalationTier.HUMAN, f"SLA breached ({elapsed():.0f}ms)", elapsed()))
    return queue_for_human(request_id, messages, f"All tiers failed after {elapsed():.0f}ms"), events

if __name__ == "__main__":
    import uuid
    messages = [{"role": "user", "content": "Explain distributed consensus algorithms."}]
    result, escalations = call_with_tiered_escalation(
        str(uuid.uuid4())[:8], messages, system="You are a distributed systems expert.", sla_ms=8000
    )
    print(f"\nResult: {result[:200]}")
    print(f"Escalation path: {' → '.join(e.tier.value for e in escalations) or 'primary (no escalation)'}")

# Expected Token Savings: Caching eliminates repeat calls; fast model saves 60% on escalated requests
# Environment: pip install anthropic
```

## Option 6: Prometheus-Compatible SLA Metrics with Alerting

Emit SLA metrics in Prometheus format. Track p50/p95/p99 latency histograms, breach counters, and escalation rates per operation. Fire alerts when breach rate exceeds threshold over a rolling window.

```python
import anthropic
import time
import math
from dataclasses import dataclass, field
from collections import deque

@dataclass
class LatencyHistogram:
    buckets_ms: list[float] = field(default_factory=lambda: [100, 250, 500, 1000, 2000, 5000, 10000])
    _counts: list[int] = field(default_factory=list, init=False, repr=False)
    _sum_ms: float = field(default=0.0, init=False, repr=False)
    _total: int = field(default=0, init=False, repr=False)

    def __post_init__(self):
        self._counts = [0] * (len(self.buckets_ms) + 1)

    def observe(self, latency_ms: float) -> None:
        self._sum_ms += latency_ms
        self._total += 1
        for i, bucket in enumerate(self.buckets_ms):
            if latency_ms <= bucket:
                for j in range(i, len(self._counts)):
                    self._counts[j] += 1
                return
        self._counts[-1] += 1

    def percentile(self, p: float) -> float:
        """Estimate percentile from histogram buckets."""
        if self._total == 0:
            return 0.0
        target = math.ceil(self._total * p)
        prev_count = 0
        prev_bound = 0.0
        for i, bucket in enumerate(self.buckets_ms):
            cumulative = self._counts[i]
            if cumulative >= target:
                fraction = (target - prev_count) / max(1, cumulative - prev_count)
                return prev_bound + fraction * (bucket - prev_bound)
            prev_count = cumulative
            prev_bound = bucket
        return self.buckets_ms[-1]

    def prometheus_lines(self, metric_name: str, labels: str) -> list[str]:
        lines = []
        for i, bucket in enumerate(self.buckets_ms):
            lines.append(f'{metric_name}_bucket{{{labels},le="{bucket}"}} {self._counts[i]}')
        lines.append(f'{metric_name}_bucket{{{labels},le="+Inf"}} {self._total}')
        lines.append(f'{metric_name}_sum{{{labels}}} {self._sum_ms:.1f}')
        lines.append(f'{metric_name}_count{{{labels}}} {self._total}')
        return lines

class SLAMetricsCollector:
    def __init__(self, operation: str, sla_ms: float, breach_alert_pct: float = 5.0):
        self.operation = operation
        self.sla_ms = sla_ms
        self.breach_alert_pct = breach_alert_pct
        self.histogram = LatencyHistogram()
        self._breach_count = 0
        self._escalation_count = 0
        self._total = 0
        self._recent_window: deque = deque(maxlen=100)  # rolling window

    def record(self, latency_ms: float, escalated: bool = False) -> None:
        self.histogram.observe(latency_ms)
        breached = latency_ms > self.sla_ms
        self._total += 1
        if breached:
            self._breach_count += 1
        if escalated:
            self._escalation_count += 1
        self._recent_window.append(breached)
        self._check_alert()

    def _check_alert(self) -> None:
        if len(self._recent_window) < 10:
            return
        recent_breach_pct = sum(self._recent_window) / len(self._recent_window) * 100
        if recent_breach_pct > self.breach_alert_pct:
            print(f"🚨 [SLA-ALERT] {self.operation}: {recent_breach_pct:.1f}% breach rate in last {len(self._recent_window)} requests (threshold: {self.breach_alert_pct}%)")

    def prometheus_output(self) -> str:
        labels = f'operation="{self.operation}"'
        lines = [
            f"# HELP agent_sla_latency_ms Latency histogram for {self.operation}",
            f"# TYPE agent_sla_latency_ms histogram",
        ] + self.histogram.prometheus_lines("agent_sla_latency_ms", labels) + [
            f'agent_sla_breaches_total{{{labels}}} {self._breach_count}',
            f'agent_sla_escalations_total{{{labels}}} {self._escalation_count}',
            f'agent_sla_p50_ms{{{labels}}} {self.histogram.percentile(0.5):.1f}',
            f'agent_sla_p95_ms{{{labels}}} {self.histogram.percentile(0.95):.1f}',
            f'agent_sla_p99_ms{{{labels}}} {self.histogram.percentile(0.99):.1f}',
            f'agent_sla_target_ms{{{labels}}} {self.sla_ms}',
        ]
        return "\n".join(lines)

_metrics = SLAMetricsCollector("chat_response", sla_ms=3000, breach_alert_pct=10.0)

def instrumented_call(messages: list[dict], system: str = "") -> str:
    client = anthropic.Anthropic()
    start = time.monotonic()
    kwargs = {"model": "claude-haiku-4-5-20251001", "max_tokens": 256, "messages": messages}
    if system:
        kwargs["system"] = system
    response = client.messages.create(**kwargs)
    latency_ms = (time.monotonic() - start) * 1000
    _metrics.record(latency_ms)
    return response.content[0].text

if __name__ == "__main__":
    queries = ["What is Python?", "Explain async/await", "How does GC work?", "What are generators?", "Explain decorators"]
    for q in queries:
        instrumented_call([{"role": "user", "content": q}])

    print("\n=== Prometheus Metrics ===")
    print(_metrics.prometheus_output())

# Expected Token Savings: Metrics-driven optimization targets the 20% of operations causing 80% of breaches
# Environment: pip install anthropic
```

## Comparison

| Option | Latency SLA | Quality SLA | Escalation | Reporting | Best For |
|--------|------------|------------|-----------|-----------|----------|
| 1. Deadline Context | Yes | No | Model downgrade | Console | Simple pipelines |
| 2. Async Monitor | Yes | No | Parallel fallback | Console | Latency-sensitive async |
| 3. Quality Gating | Yes | Yes | Regeneration | Console | Quality-critical responses |
| 4. SLA Registry | Yes | No | No | Dashboard | Multi-operation tracking |
| 5. Tiered Escalation | Yes | No | Model→Cache→Human | Console | Production with human backup |
| 6. Prometheus Metrics | Yes | No | Alerting | Prometheus | Ops/monitoring integration |
