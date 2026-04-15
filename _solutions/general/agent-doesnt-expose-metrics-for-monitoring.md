---
layout: solution
title: "Agent Doesn't Expose Metrics for Monitoring"
category: general
description: "The agent produces no runtime metrics — no request counts, latency histograms, token usage, or error rates — so operators cannot detect degradation, cost spikes, or outages."
tags: [general, observability, metrics, monitoring, production]
---

## Symptom

The agent runs successfully in development but in production there is no way to answer: How many requests per minute is it handling? What is the p95 latency? How many tokens did it consume today? How often are tools failing? When things go wrong, the on-call engineer has no dashboard to consult and must dig through raw logs to understand what happened.

## Root Cause

Metrics are not emitted by default from the Anthropic Python SDK. Usage data (`response.usage`) is available in every API response but is discarded unless explicitly captured. Without a metrics layer — counters, histograms, and gauges written to a time-series backend — the agent is a black box. Operators rely on guesswork rather than data to detect problems and capacity-plan.

## Fix

### Option 1 — In-process counters with periodic log flush

```python
import anthropic
import time
import threading
from collections import defaultdict

client = anthropic.Anthropic()

# Thread-safe counters
_lock     = threading.Lock()
_counters: dict[str, int | float] = defaultdict(float)

def inc(name: str, value: float = 1.0) -> None:
    with _lock:
        _counters[name] += value

def snapshot() -> dict[str, float]:
    with _lock:
        return dict(_counters)

def flush_metrics() -> None:
    """Periodically log all counters as structured JSON."""
    import json, logging
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger("metrics")
    while True:
        time.sleep(60)
        data = snapshot()
        logger.info(json.dumps({"event": "metrics_flush", **data}))

# Background flush thread
threading.Thread(target=flush_metrics, daemon=True).start()

def ask(prompt: str) -> str:
    inc("requests_total")
    start = time.monotonic()
    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=[{"role": "user", "content": prompt}],
        )
        latency = time.monotonic() - start
        inc("requests_success")
        inc("latency_seconds_total", latency)
        inc("tokens_input_total",  resp.usage.input_tokens)
        inc("tokens_output_total", resp.usage.output_tokens)
        return resp.content[0].text
    except Exception as exc:
        inc("requests_error")
        inc(f"errors_{type(exc).__name__}")
        raise

for i in range(5):
    print(ask(f"What is {i} squared?"))

print("Current metrics:", snapshot())
```

**Expected Token Savings:** Token counters make daily spend visible; catching unexpected spikes early prevents runaway costs.
**Environment:** Simple single-process agents; quick observability baseline with zero external dependencies.

---

### Option 2 — Prometheus metrics with `/metrics` HTTP endpoint

```python
import anthropic
import time
import threading
from prometheus_client import Counter, Histogram, Gauge, start_http_server

client = anthropic.Anthropic()

# Prometheus metrics
REQUEST_COUNT   = Counter("agent_requests_total",        "Total API requests",          ["status"])
REQUEST_LATENCY = Histogram("agent_request_latency_seconds", "Request latency in seconds",
                            buckets=[0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0])
TOKEN_COUNT     = Counter("agent_tokens_total",          "Total tokens consumed",       ["direction"])
ACTIVE_REQUESTS = Gauge("agent_active_requests",         "In-flight requests")

# Expose /metrics on port 8000 for Prometheus scraping
start_http_server(8000)
print("[metrics] Prometheus endpoint: http://localhost:8000/metrics")

def ask(prompt: str) -> str:
    ACTIVE_REQUESTS.inc()
    start = time.monotonic()
    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=[{"role": "user", "content": prompt}],
        )
        REQUEST_COUNT.labels(status="success").inc()
        TOKEN_COUNT.labels(direction="input").inc(resp.usage.input_tokens)
        TOKEN_COUNT.labels(direction="output").inc(resp.usage.output_tokens)
        return resp.content[0].text
    except Exception:
        REQUEST_COUNT.labels(status="error").inc()
        raise
    finally:
        REQUEST_LATENCY.observe(time.monotonic() - start)
        ACTIVE_REQUESTS.dec()

for prompt in ["Explain gravity.", "What is entropy?", "Define recursion."]:
    print(ask(prompt))

# Keep the server alive for scraping
time.sleep(5)
```

**Expected Token Savings:** Prometheus histograms expose p50/p95/p99 latency; token counters drive cost alerts before the bill arrives.
**Environment:** Kubernetes-hosted agents; Grafana/Prometheus stacks; any environment with a Prometheus scraper.

---

### Option 3 — OpenTelemetry metrics exported to OTLP collector

```python
import anthropic
import time
from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import (
    ConsoleMetricExporter,
    PeriodicExportingMetricReader,
)

# Configure OTLP exporter (replace ConsoleMetricExporter with OTLPMetricExporter for production)
reader   = PeriodicExportingMetricReader(ConsoleMetricExporter(), export_interval_millis=30_000)
provider = MeterProvider(metric_readers=[reader])
metrics.set_meter_provider(provider)
meter = metrics.get_meter("agent.metrics", version="1.0.0")

# Instruments
request_counter   = meter.create_counter("agent.requests",       unit="1",   description="Total requests")
error_counter     = meter.create_counter("agent.errors",         unit="1",   description="Total errors")
latency_histogram = meter.create_histogram("agent.latency",      unit="s",   description="Request latency")
token_counter     = meter.create_counter("agent.tokens",         unit="token", description="Tokens consumed")

client = anthropic.Anthropic()

def ask(prompt: str, task_type: str = "general") -> str:
    attrs = {"task_type": task_type, "model": "claude-haiku-4-5-20251001"}
    start = time.monotonic()
    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=[{"role": "user", "content": prompt}],
        )
        request_counter.add(1, {**attrs, "status": "success"})
        token_counter.add(resp.usage.input_tokens,  {**attrs, "direction": "input"})
        token_counter.add(resp.usage.output_tokens, {**attrs, "direction": "output"})
        return resp.content[0].text
    except Exception as exc:
        request_counter.add(1, {**attrs, "status": "error"})
        error_counter.add(1, {**attrs, "error_type": type(exc).__name__})
        raise
    finally:
        latency_histogram.record(time.monotonic() - start, attrs)

print(ask("Summarise the water cycle.", task_type="summary"))
print(ask("Write a haiku about autumn.", task_type="creative"))
```

**Expected Token Savings:** OTLP attributes (task_type, model) allow per-task cost breakdown, identifying which task types are most expensive.
**Environment:** Cloud-native agents; DataDog, Honeycomb, Jaeger, or any OTLP-compatible backend.

---

### Option 4 — StatsD metrics pushed to Graphite/DataDog

```python
import anthropic
import time
import socket
from contextlib import contextmanager

client = anthropic.Anthropic()

STATSD_HOST = "localhost"
STATSD_PORT = 8125

def _send_statsd(metric: str) -> None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.sendto(metric.encode(), (STATSD_HOST, STATSD_PORT))
    except OSError:
        pass  # non-critical — never let metrics break the agent

def increment(name: str, value: int = 1, tags: dict | None = None) -> None:
    tag_str = ",".join(f"{k}:{v}" for k, v in (tags or {}).items())
    payload = f"{name}:{value}|c" + (f"|#{tag_str}" if tag_str else "")
    _send_statsd(payload)

def timing(name: str, ms: float, tags: dict | None = None) -> None:
    tag_str = ",".join(f"{k}:{v}" for k, v in (tags or {}).items())
    payload = f"{name}:{ms:.2f}|ms" + (f"|#{tag_str}" if tag_str else "")
    _send_statsd(payload)

@contextmanager
def timed(name: str, tags: dict | None = None):
    start = time.monotonic()
    try:
        yield
    finally:
        timing(name, (time.monotonic() - start) * 1000, tags)

def ask(prompt: str, model: str = "claude-haiku-4-5-20251001") -> str:
    tags = {"model": model}
    increment("agent.requests", tags=tags)
    with timed("agent.latency_ms", tags=tags):
        try:
            resp = client.messages.create(
                model=model,
                max_tokens=256,
                messages=[{"role": "user", "content": prompt}],
            )
            increment("agent.tokens.input",  resp.usage.input_tokens,  tags=tags)
            increment("agent.tokens.output", resp.usage.output_tokens, tags=tags)
            increment("agent.requests.success", tags=tags)
            return resp.content[0].text
        except Exception as exc:
            increment("agent.requests.error", tags={**tags, "error": type(exc).__name__})
            raise

for prompt in ["What is a quasar?", "Explain DNA replication.", "Define osmosis."]:
    print(ask(prompt))
```

**Expected Token Savings:** StatsD gauges for token counts flow directly into DataDog cost monitors; zero-overhead UDP datagrams never block the agent.
**Environment:** DataDog APM users; legacy stacks with existing Graphite/Graphite Whisper backends.

---

### Option 5 — Async metrics with asyncio and aiohttp health endpoint

```python
import asyncio
import time
from collections import defaultdict
from aiohttp import web
import anthropic

client = anthropic.AsyncAnthropic()

_metrics: dict[str, float] = defaultdict(float)
_lock = asyncio.Lock()

async def inc(key: str, value: float = 1.0) -> None:
    async with _lock:
        _metrics[key] += value

async def ask(prompt: str) -> str:
    await inc("requests_total")
    start = time.monotonic()
    try:
        resp = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=[{"role": "user", "content": prompt}],
        )
        latency = time.monotonic() - start
        await inc("requests_success")
        await inc("latency_sum", latency)
        await inc("tokens_input",  resp.usage.input_tokens)
        await inc("tokens_output", resp.usage.output_tokens)
        return resp.content[0].text
    except Exception:
        await inc("requests_error")
        raise

async def metrics_handler(request: web.Request) -> web.Response:
    async with _lock:
        data = dict(_metrics)
    lines = [f"# agent metrics\n"]
    for k, v in sorted(data.items()):
        lines.append(f"{k} {v:.2f}\n")
    return web.Response(text="".join(lines), content_type="text/plain")

async def main():
    app = web.Application()
    app.router.add_get("/metrics", metrics_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "localhost", 8080)
    await site.start()
    print("[metrics] http://localhost:8080/metrics")

    prompts = ["Explain photosynthesis.", "What is tectonic drift?", "Define symbiosis."]
    results = await asyncio.gather(*[ask(p) for p in prompts])
    for r in results:
        print(r[:80])

    await asyncio.sleep(2)  # let metrics endpoint serve

asyncio.run(main())
```

**Expected Token Savings:** Async metrics collection adds zero blocking overhead; concurrent requests all update shared counters safely via async lock.
**Environment:** Async agents using aiohttp or FastAPI; services that need a lightweight health/metrics endpoint without external agents.

---

### Option 6 — Decorator-based metrics for tool-instrumented agents

```python
import anthropic
import time
import functools
from typing import Callable, Any
from dataclasses import dataclass, field

client = anthropic.Anthropic()

@dataclass
class MetricsStore:
    calls:   dict[str, int]   = field(default_factory=dict)
    errors:  dict[str, int]   = field(default_factory=dict)
    latency: dict[str, float] = field(default_factory=dict)
    tokens:  dict[str, int]   = field(default_factory=dict)

    def report(self) -> None:
        print("\n=== Agent Metrics ===")
        for name in self.calls:
            avg = self.latency.get(name, 0) / max(self.calls[name], 1)
            print(
                f"  {name}: calls={self.calls[name]}, errors={self.errors.get(name,0)}, "
                f"avg_latency={avg:.2f}s, tokens={self.tokens.get(name,0)}"
            )

store = MetricsStore()

def tracked(name: str):
    """Decorator that records call count, latency, errors, and token usage."""
    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs) -> Any:
            store.calls[name] = store.calls.get(name, 0) + 1
            start = time.monotonic()
            try:
                result = fn(*args, **kwargs)
                store.latency[name] = store.latency.get(name, 0) + (time.monotonic() - start)
                # If result has usage (Anthropic response), capture tokens
                if hasattr(result, "usage"):
                    store.tokens[name] = (
                        store.tokens.get(name, 0)
                        + result.usage.input_tokens
                        + result.usage.output_tokens
                    )
                return result
            except Exception:
                store.errors[name] = store.errors.get(name, 0) + 1
                raise
        return wrapper
    return decorator

@tracked("summarise")
def summarise(text: str):
    return client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{"role": "user", "content": f"Summarise: {text}"}],
    )

@tracked("classify")
def classify(text: str):
    return client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=16,
        messages=[{"role": "user", "content": f"Classify as positive/negative: {text}"}],
    )

for _ in range(3):
    summarise("The quarterly report showed strong growth across all regions.")
    classify("I love this product, it works perfectly!")

store.report()
```

**Expected Token Savings:** Per-function token tracking reveals which agent tools are consuming the most tokens, enabling targeted optimisation.
**Environment:** Tool-using agents; any agent where individual operations need separate metrics rather than aggregate totals.

---

## Comparison

| Option | Backend | Push/Pull | Async Safe | Overhead | Best For |
|---|---|---|---|---|---|
| 1. In-process counters | Log file / stdout | Push (periodic) | No | Minimal | Quick baseline; no external deps |
| 2. Prometheus client | Prometheus/Grafana | Pull (scrape) | No | Low | Kubernetes; Grafana dashboards |
| 3. OpenTelemetry | Any OTLP backend | Push | No | Medium | Vendor-agnostic; cloud-native stacks |
| 4. StatsD/DataDog | Graphite / DataDog | Push (UDP) | No | Negligible | DataDog APM; legacy stacks |
| 5. Async + aiohttp | Custom HTTP endpoint | Pull | Yes | Low | Async agents; lightweight health endpoint |
| 6. Decorator tracking | In-process report | On-demand | No | Minimal | Per-tool breakdowns; dev/staging |
