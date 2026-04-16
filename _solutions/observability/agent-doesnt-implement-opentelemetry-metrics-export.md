---
title: "Agent Doesn't Implement OpenTelemetry Metrics Export"
description: "Agents that log numbers as text instead of exporting OTel metrics miss histogram percentiles, Prometheus scraping, Grafana dashboards, and alerting — structured metrics are the foundation of production observability."
difficulty: intermediate
category: observability
tags: [observability, opentelemetry, metrics, prometheus, grafana, counters, histograms]
---

# Agent Doesn't Implement OpenTelemetry Metrics Export

## Problem

Agents that `print()` token counts and latencies produce unstructured text that can't be queried, aggregated, or alerted on. OpenTelemetry metrics export structured numeric signals — counters, histograms, gauges — to Prometheus, Grafana, Datadog, or any OTLP-compatible backend. Without OTel metrics, production questions like "what is P95 LLM latency for the last hour?" require log parsing instead of a single PromQL query.

**Symptoms:**
- Latency data lives in log lines, not histograms — no P99 without parsing
- Token usage trends require grep + awk, not a Grafana panel
- No alerts fire when error rate crosses 1% because there's no counter to threshold
- Cost forecasting is manual because no time-series data for token spend
- Adding a new dashboard requires changing agent code, not just a Prometheus query

---

## Solution 1: OTel SDK Counter and Histogram for LLM Calls

Instrument every LLM call with a request counter and a latency histogram using the OTel Python SDK.

```python
import asyncio
import time
from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import (
    ConsoleMetricExporter,
    PeriodicExportingMetricReader,
)
# pip install opentelemetry-sdk opentelemetry-exporter-prometheus
import anthropic


def setup_metrics(service_name: str = "ai-agent") -> metrics.Meter:
    exporter = ConsoleMetricExporter()
    reader = PeriodicExportingMetricReader(exporter, export_interval_millis=10_000)
    provider = MeterProvider(metric_readers=[reader])
    metrics.set_meter_provider(provider)
    return metrics.get_meter(service_name)


meter = setup_metrics("ai-agent")

# Define instruments
llm_request_counter = meter.create_counter(
    "llm.requests.total",
    description="Total LLM API requests",
    unit="1",
)
llm_error_counter = meter.create_counter(
    "llm.errors.total",
    description="Total LLM API errors",
    unit="1",
)
llm_latency_histogram = meter.create_histogram(
    "llm.request.duration",
    description="LLM request latency in milliseconds",
    unit="ms",
)
llm_input_token_counter = meter.create_counter(
    "llm.tokens.input.total",
    description="Total input tokens consumed",
    unit="token",
)
llm_output_token_counter = meter.create_counter(
    "llm.tokens.output.total",
    description="Total output tokens generated",
    unit="token",
)


class InstrumentedAnthropicClient:
    def __init__(self, api_key: str, model: str = "claude-opus-4-6"):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)
        self.model = model

    async def complete(
        self,
        messages: list[dict],
        system: str = "",
        max_tokens: int = 512,
        labels: dict | None = None,
    ) -> str:
        attrs = {"model": self.model, "service": "ai-agent", **(labels or {})}
        start = time.perf_counter()

        try:
            response = await self.client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                system=system,
                messages=messages,
            )
            latency_ms = (time.perf_counter() - start) * 1000

            # Record metrics
            llm_request_counter.add(1, attrs)
            llm_latency_histogram.record(latency_ms, attrs)
            llm_input_token_counter.add(response.usage.input_tokens, attrs)
            llm_output_token_counter.add(response.usage.output_tokens, attrs)

            return response.content[0].text

        except Exception as exc:
            llm_error_counter.add(1, {**attrs, "error_type": type(exc).__name__})
            raise


async def demo():
    client = InstrumentedAnthropicClient(api_key="sk-...")

    for i in range(5):
        await client.complete(
            messages=[{"role": "user", "content": f"Question {i}: what is Python?"}],
            labels={"user_tier": "free", "feature": "chat"},
        )
    print("Metrics emitted to console exporter (check logs)")

# asyncio.run(demo())
```

---

## Solution 2: Prometheus Exporter via OTLP

Export metrics to Prometheus via the OTel Prometheus exporter — scrapeable at `/metrics`.

```python
import asyncio
import time
from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider
# pip install opentelemetry-exporter-prometheus prometheus-client
try:
    from opentelemetry.exporter.prometheus import PrometheusMetricReader
    from prometheus_client import start_http_server
    HAS_PROMETHEUS = True
except ImportError:
    HAS_PROMETHEUS = False

import anthropic
from fastapi import FastAPI
from fastapi.responses import JSONResponse


def setup_prometheus_metrics(port: int = 8001) -> metrics.Meter:
    if HAS_PROMETHEUS:
        reader = PrometheusMetricReader()
        start_http_server(port=port)
        print(f"[metrics] Prometheus scrape endpoint: http://localhost:{port}/metrics")
    else:
        from opentelemetry.sdk.metrics.export import ConsoleMetricExporter, PeriodicExportingMetricReader
        reader = PeriodicExportingMetricReader(ConsoleMetricExporter(), 15_000)

    provider = MeterProvider(metric_readers=[reader])
    metrics.set_meter_provider(provider)
    return metrics.get_meter("ai-agent-prometheus")


meter = setup_prometheus_metrics(port=8001)

# Business-relevant metrics
request_count   = meter.create_counter("agent_requests_total", unit="1")
error_count     = meter.create_counter("agent_errors_total", unit="1")
latency_hist    = meter.create_histogram("agent_request_duration_ms", unit="ms")
session_gauge   = meter.create_up_down_counter("agent_active_sessions", unit="1")
cost_counter    = meter.create_counter("agent_cost_usd_total", unit="$")
token_in        = meter.create_counter("agent_input_tokens_total", unit="token")
token_out       = meter.create_counter("agent_output_tokens_total", unit="token")

OUTPUT_PRICE_PER_TOKEN = 15.0 / 1_000_000  # $15/M tokens
INPUT_PRICE_PER_TOKEN  =  3.0 / 1_000_000


app = FastAPI()
_client = anthropic.AsyncAnthropic(api_key="sk-...")
_active_sessions = 0


@app.post("/agent/chat")
async def chat(request_body: dict):
    global _active_sessions
    session_id = request_body.get("session_id", "unknown")
    user_tier  = request_body.get("tier", "free")
    message    = request_body.get("message", "")

    attrs = {"session_id": session_id, "tier": user_tier}
    _active_sessions += 1
    session_gauge.add(1, attrs)
    start = time.perf_counter()

    try:
        response = await _client.messages.create(
            model="claude-opus-4-6",
            max_tokens=512,
            messages=[{"role": "user", "content": message}],
        )
        latency_ms = (time.perf_counter() - start) * 1000
        cost = (
            response.usage.input_tokens * INPUT_PRICE_PER_TOKEN +
            response.usage.output_tokens * OUTPUT_PRICE_PER_TOKEN
        )

        request_count.add(1, attrs)
        latency_hist.record(latency_ms, attrs)
        token_in.add(response.usage.input_tokens, attrs)
        token_out.add(response.usage.output_tokens, attrs)
        cost_counter.add(cost, attrs)

        return JSONResponse({"reply": response.content[0].text})

    except Exception as exc:
        error_count.add(1, {**attrs, "error_type": type(exc).__name__})
        raise

    finally:
        _active_sessions -= 1
        session_gauge.add(-1, attrs)


# Prometheus queries for dashboards:
# P95 latency:  histogram_quantile(0.95, rate(agent_request_duration_ms_bucket[5m]))
# Error rate:   rate(agent_errors_total[5m]) / rate(agent_requests_total[5m])
# Cost/hour:    rate(agent_cost_usd_total[1h]) * 3600
```

---

## Solution 3: OTLP gRPC Export to Grafana Tempo / Mimir

Send metrics via OTLP gRPC to a Grafana stack (Mimir for metrics, Tempo for traces).

```python
import asyncio
import time
from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
# pip install opentelemetry-exporter-otlp-proto-grpc
try:
    from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
    HAS_OTLP = True
except ImportError:
    from opentelemetry.sdk.metrics.export import ConsoleMetricExporter as OTLPMetricExporter
    HAS_OTLP = False

import anthropic


def setup_otlp_metrics(
    endpoint: str = "http://localhost:4317",
    service_name: str = "ai-agent",
    export_interval_ms: int = 15_000,
) -> metrics.Meter:
    exporter = OTLPMetricExporter(endpoint=endpoint, insecure=True)
    reader = PeriodicExportingMetricReader(exporter, export_interval_millis=export_interval_ms)
    provider = MeterProvider(metric_readers=[reader])
    metrics.set_meter_provider(provider)
    return metrics.get_meter(service_name)


meter = setup_otlp_metrics("http://mimir:4317", "ai-agent")

# Histogram with explicit bucket boundaries for LLM latency
latency_hist = meter.create_histogram(
    "llm.latency.ms",
    description="LLM call latency distribution",
    unit="ms",
)

model_call_counter = meter.create_counter(
    "llm.model.calls",
    description="Calls per model",
    unit="1",
)

# Gauge: context window fill ratio
context_ratio_gauge = meter.create_observable_gauge(
    "llm.context.fill_ratio",
    description="Current context window utilization (0-1)",
    unit="1",
)


_context_ratios: dict[str, float] = {}


def context_ratio_callback(options):
    from opentelemetry.sdk.metrics.export import NumberDataPoint
    for session_id, ratio in _context_ratios.items():
        yield metrics.Observation(ratio, {"session_id": session_id})


class OTLPInstrumentedAgent:
    def __init__(self, api_key: str, model: str = "claude-opus-4-6"):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)
        self.model = model
        self.context_window = 200_000

    async def complete(
        self,
        session_id: str,
        messages: list[dict],
        max_tokens: int = 512,
    ) -> str:
        start = time.perf_counter()
        response = await self.client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            messages=messages,
        )
        latency_ms = (time.perf_counter() - start) * 1000

        attrs = {"model": self.model, "session_id": session_id}
        latency_hist.record(latency_ms, attrs)
        model_call_counter.add(1, attrs)

        # Track context fill ratio
        total_tokens = response.usage.input_tokens + response.usage.output_tokens
        _context_ratios[session_id] = total_tokens / self.context_window

        return response.content[0].text


async def demo():
    agent = OTLPInstrumentedAgent(api_key="sk-...")
    history: list[dict] = []
    for i in range(3):
        history.append({"role": "user", "content": f"Message {i}"})
        reply = await agent.complete(f"sess_otlp_{i % 2}", history)
        history.append({"role": "assistant", "content": reply})

# asyncio.run(demo())
```

---

## Solution 4: Custom Business Metrics with OTel

Track business-level signals — satisfaction rate, goal completion, feature usage — alongside technical metrics.

```python
import asyncio
import random
import time
from dataclasses import dataclass
from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import ConsoleMetricExporter, PeriodicExportingMetricReader
import anthropic


def setup_business_meter() -> metrics.Meter:
    reader = PeriodicExportingMetricReader(ConsoleMetricExporter(), 30_000)
    provider = MeterProvider(metric_readers=[reader])
    metrics.set_meter_provider(provider)
    return metrics.get_meter("ai-agent-business")


biz_meter = setup_business_meter()

# Technical metrics
api_calls      = biz_meter.create_counter("api.calls.total")
api_latency    = biz_meter.create_histogram("api.latency.ms")

# Business metrics
goals_completed   = biz_meter.create_counter("business.goals.completed.total")
goals_abandoned   = biz_meter.create_counter("business.goals.abandoned.total")
turns_per_session = biz_meter.create_histogram("business.turns_per_session")
thumbs_up         = biz_meter.create_counter("business.feedback.positive.total")
thumbs_down       = biz_meter.create_counter("business.feedback.negative.total")
feature_used      = biz_meter.create_counter("business.feature.usage.total")
upsell_shown      = biz_meter.create_counter("business.upsell.shown.total")
upsell_converted  = biz_meter.create_counter("business.upsell.converted.total")


@dataclass
class SessionMetrics:
    session_id: str
    user_tier: str
    turns: int = 0
    goal_completed: bool = False


class BusinessMetricAgent:
    def __init__(self, api_key: str):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)

    async def run_session(
        self,
        session_id: str,
        user_tier: str,
        user_messages: list[str],
    ) -> dict:
        sm = SessionMetrics(session_id=session_id, user_tier=user_tier)
        attrs = {"session_id": session_id, "user_tier": user_tier}
        history: list[dict] = []

        for msg in user_messages:
            history.append({"role": "user", "content": msg})
            start = time.perf_counter()

            response = await self.client.messages.create(
                model="claude-opus-4-6",
                max_tokens=256,
                messages=history,
            )
            latency_ms = (time.perf_counter() - start) * 1000
            reply = response.content[0].text
            history.append({"role": "assistant", "content": reply})

            # Technical
            api_calls.add(1, attrs)
            api_latency.record(latency_ms, attrs)

            # Feature usage tracking
            if "?" in msg:
                feature_used.add(1, {**attrs, "feature": "question_answering"})

            sm.turns += 1

        # End-of-session metrics
        turns_per_session.record(sm.turns, attrs)

        # Simulate goal detection (in prod: LLM judge or explicit user signal)
        sm.goal_completed = random.random() > 0.3
        if sm.goal_completed:
            goals_completed.add(1, attrs)
        else:
            goals_abandoned.add(1, attrs)

        # Upsell for free tier
        if user_tier == "free" and sm.turns >= 3:
            upsell_shown.add(1, attrs)
            if random.random() > 0.8:
                upsell_converted.add(1, attrs)

        return {"turns": sm.turns, "goal_completed": sm.goal_completed}


async def demo():
    agent = BusinessMetricAgent(api_key="sk-...")
    for i in range(3):
        result = await agent.run_session(
            session_id=f"sess_{i}",
            user_tier="free" if i % 2 == 0 else "pro",
            user_messages=["Hello!", "How can you help?", "Tell me more."],
        )
        print(f"Session {i}: {result}")

# asyncio.run(demo())
```

---

## Solution 5: Per-Model Metric Aggregation with Observable Instruments

Use observable (pull-based) instruments for metrics computed at collection time, like model availability and cost-per-token ratios.

```python
import asyncio
import time
from collections import defaultdict
from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import ConsoleMetricExporter, PeriodicExportingMetricReader
import anthropic


def setup_observable_meter() -> metrics.Meter:
    reader = PeriodicExportingMetricReader(ConsoleMetricExporter(), 10_000)
    provider = MeterProvider(metric_readers=[reader])
    metrics.set_meter_provider(provider)
    return metrics.get_meter("ai-agent-observable")


obs_meter = setup_observable_meter()

# Aggregated state
_model_stats: dict[str, dict] = defaultdict(lambda: {
    "calls": 0, "errors": 0, "total_latency_ms": 0.0,
    "total_cost": 0.0, "last_call_ts": 0.0,
})

INPUT_PRICE = {"claude-opus-4-6": 3.0 / 1_000_000, "claude-haiku-4-5-20251001": 0.25 / 1_000_000}
OUTPUT_PRICE = {"claude-opus-4-6": 15.0 / 1_000_000, "claude-haiku-4-5-20251001": 1.25 / 1_000_000}


def _model_error_rate_callback(options):
    for model, stats in _model_stats.items():
        calls = stats["calls"] or 1
        yield metrics.Observation(stats["errors"] / calls, {"model": model})


def _model_avg_latency_callback(options):
    for model, stats in _model_stats.items():
        calls = stats["calls"] or 1
        yield metrics.Observation(stats["total_latency_ms"] / calls, {"model": model})


def _model_cost_callback(options):
    for model, stats in _model_stats.items():
        yield metrics.Observation(stats["total_cost"], {"model": model})


# Register observable gauges
obs_meter.create_observable_gauge(
    "llm.model.error_rate", [_model_error_rate_callback],
    description="Per-model error rate (0-1)", unit="1",
)
obs_meter.create_observable_gauge(
    "llm.model.avg_latency_ms", [_model_avg_latency_callback],
    description="Per-model average latency", unit="ms",
)
obs_meter.create_observable_gauge(
    "llm.model.cumulative_cost_usd", [_model_cost_callback],
    description="Cumulative cost per model", unit="$",
)


class ObservableMetricsAgent:
    def __init__(self, api_key: str):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)

    async def call(self, model: str, message: str) -> str:
        start = time.perf_counter()
        stats = _model_stats[model]
        try:
            response = await self.client.messages.create(
                model=model, max_tokens=256,
                messages=[{"role": "user", "content": message}],
            )
            latency = (time.perf_counter() - start) * 1000
            stats["calls"] += 1
            stats["total_latency_ms"] += latency
            stats["last_call_ts"] = time.time()
            cost = (
                response.usage.input_tokens * INPUT_PRICE.get(model, 3.0 / 1_000_000) +
                response.usage.output_tokens * OUTPUT_PRICE.get(model, 15.0 / 1_000_000)
            )
            stats["total_cost"] += cost
            return response.content[0].text
        except Exception:
            stats["errors"] += 1
            raise


async def demo():
    agent = ObservableMetricsAgent(api_key="sk-...")
    models = ["claude-opus-4-6", "claude-haiku-4-5-20251001"]
    for i in range(4):
        model = models[i % 2]
        await agent.call(model, f"Question {i}: what is asyncio?")
    print("Observable gauges will be collected on next interval")

# asyncio.run(demo())
```

---

## Solution 6: Metric Cardinality Control with Label Allow-List

High-cardinality labels (session_id, user_id on every metric) cause Prometheus to OOM. Enforce an allow-list of safe label keys.

```python
import asyncio
import time
from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import ConsoleMetricExporter, PeriodicExportingMetricReader
import anthropic

# SAFE label keys — never include high-cardinality values like session_id or user_id
ALLOWED_METRIC_LABELS: set[str] = {
    "model", "tier", "feature", "environment", "region", "error_type", "stop_reason",
}


def safe_labels(raw: dict) -> dict:
    """Strip any label key not in the allow-list to prevent cardinality explosion."""
    filtered = {k: v for k, v in raw.items() if k in ALLOWED_METRIC_LABELS}
    removed = set(raw) - set(filtered)
    if removed:
        print(f"[metrics] Removed high-cardinality labels: {removed}")
    return filtered


def setup_cardinality_meter() -> metrics.Meter:
    reader = PeriodicExportingMetricReader(ConsoleMetricExporter(), 15_000)
    provider = MeterProvider(metric_readers=[reader])
    metrics.set_meter_provider(provider)
    return metrics.get_meter("ai-agent-safe-cardinality")


meter = setup_cardinality_meter()
safe_counter   = meter.create_counter("llm.requests.total")
safe_latency   = meter.create_histogram("llm.latency.ms")
safe_tokens    = meter.create_counter("llm.tokens.total")


class CardinalitySafeAgent:
    def __init__(self, api_key: str):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)

    async def complete(
        self,
        messages: list[dict],
        model: str = "claude-opus-4-6",
        raw_labels: dict | None = None,
    ) -> str:
        labels = safe_labels({
            "model": model,
            "environment": "production",
            **(raw_labels or {}),
        })

        start = time.perf_counter()
        response = await self.client.messages.create(
            model=model, max_tokens=512, messages=messages,
        )
        latency_ms = (time.perf_counter() - start) * 1000

        safe_counter.add(1, labels)
        safe_latency.record(latency_ms, labels)
        safe_tokens.add(
            response.usage.input_tokens + response.usage.output_tokens,
            {**labels, "token_type": "total"},
        )
        return response.content[0].text


async def demo():
    agent = CardinalitySafeAgent(api_key="sk-...")
    # session_id and user_id will be stripped — only model/tier/feature pass through
    await agent.complete(
        messages=[{"role": "user", "content": "Hello!"}],
        raw_labels={
            "model": "claude-opus-4-6",
            "tier": "pro",
            "feature": "chat",
            "session_id": "sess_abc123",  # Stripped — high cardinality
            "user_id": "usr_99999",       # Stripped — high cardinality
        },
    )

# asyncio.run(demo())
```

---

## Comparison

| Solution | Backend | Push/Pull | Cardinality Control | Business Metrics | Complexity |
|---|---|---|---|---|---|
| Console exporter | stdout | Push | No | No | Very Low |
| Prometheus scrape | Prometheus | Pull | Manual | No | Low |
| OTLP gRPC (Grafana) | Mimir/Tempo | Push | No | No | Medium |
| Business metric layer | Any backend | Push | No | Yes | Medium |
| Observable instruments | Any backend | Pull | N/A | No | Low |
| Cardinality allow-list | Any backend | Push | Yes | No | Low |

**Recommendation:** Start with Solution 2 (Prometheus exporter) if you already run a Prometheus stack — it's the most widely supported. Use Solution 3 (OTLP gRPC) for Grafana Cloud or OpenTelemetry Collector pipelines. Always apply Solution 6 (cardinality control) in production to prevent Prometheus OOM from session-level labels.
