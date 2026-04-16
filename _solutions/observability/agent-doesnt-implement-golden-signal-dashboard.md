---
title: "Agent Doesn't Implement Golden Signal Dashboard"
description: "AI agents lack a unified view of the four golden signals — latency, traffic, errors, and saturation — making it impossible to diagnose incidents quickly or set meaningful SLO alerts."
category: observability
difficulty: intermediate
tags: [golden-signals, dashboard, prometheus, grafana, slo, monitoring, metrics]
---

# Agent Doesn't Implement Golden Signal Dashboard

## Problem

The four golden signals (Google SRE book) are the minimum observable surface for any production service: **latency**, **traffic**, **errors**, and **saturation**. AI agents often have scattered metrics — token counts here, error logs there, no latency histograms — making it impossible to answer "is my agent healthy right now?" in under 30 seconds. A golden signal dashboard provides this at a glance.

## Solution 1: Prometheus Metrics Covering All Four Signals

Instrument all four golden signals with Prometheus; one scrape endpoint, one Grafana dashboard.

```python
import asyncio
import time
from prometheus_client import Counter, Histogram, Gauge, start_http_server

# ── TRAFFIC ──────────────────────────────────────────────────────────────────
agent_requests_total = Counter(
    "agent_requests_total",
    "Total requests handled by the agent",
    ["model", "endpoint", "user_tier"],
)

# ── LATENCY ──────────────────────────────────────────────────────────────────
agent_request_latency = Histogram(
    "agent_request_latency_seconds",
    "End-to-end request latency",
    ["model", "endpoint"],
    buckets=[0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0],
)
agent_ttft_seconds = Histogram(
    "agent_ttft_seconds",
    "Time to first token (streaming)",
    ["model"],
    buckets=[0.05, 0.1, 0.2, 0.5, 1.0, 2.0, 5.0],
)

# ── ERRORS ───────────────────────────────────────────────────────────────────
agent_errors_total = Counter(
    "agent_errors_total",
    "Total errors by category",
    ["error_type", "model", "retriable"],
    # error_type: api_error | validation_error | timeout | rate_limit | schema_error
)

# ── SATURATION ────────────────────────────────────────────────────────────────
agent_active_requests = Gauge(
    "agent_active_requests",
    "Currently in-flight requests",
    ["model"],
)
agent_queue_depth = Gauge(
    "agent_queue_depth",
    "Requests waiting in queue",
)
agent_token_budget_remaining = Gauge(
    "agent_token_budget_remaining_ratio",
    "Fraction of token budget remaining (0-1)",
    ["user_id"],
)

# ── HELPER CONTEXT MANAGER ────────────────────────────────────────────────────
class TrackedRequest:
    def __init__(self, model: str, endpoint: str, user_tier: str = "standard"):
        self._model = model
        self._endpoint = endpoint
        self._tier = user_tier
        self._t0 = 0.0

    async def __aenter__(self):
        self._t0 = time.monotonic()
        agent_active_requests.labels(model=self._model).inc()
        agent_requests_total.labels(
            model=self._model, endpoint=self._endpoint, user_tier=self._tier
        ).inc()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        elapsed = time.monotonic() - self._t0
        agent_request_latency.labels(model=self._model, endpoint=self._endpoint).observe(elapsed)
        agent_active_requests.labels(model=self._model).dec()
        if exc_type is not None:
            error_type = type(exc_val).__name__.lower().replace("error", "_error")
            agent_errors_total.labels(
                error_type=error_type, model=self._model, retriable="unknown"
            ).inc()

# Usage
async def handle_agent_request(prompt: str, model: str = "claude-sonnet-4-6"):
    async with TrackedRequest(model=model, endpoint="/agent/run"):
        from anthropic import AsyncAnthropic
        resp = await AsyncAnthropic().messages.create(
            model=model, max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.content[0].text

# Start Prometheus endpoint
def start_metrics_server(port: int = 9090):
    start_http_server(port)
```

**When to use**: Any agent with a Prometheus/Grafana stack. Copy this module into any FastAPI or aiohttp service.

---

## Solution 2: FastAPI Middleware — Automatic Signal Collection

Collect all four signals automatically in middleware without modifying individual route handlers.

```python
import asyncio
import time
from fastapi import FastAPI, Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from prometheus_client import Counter, Histogram, Gauge

# Signal metrics (reuse from Solution 1 or define fresh)
http_requests = Counter("http_requests_total", "Total HTTP requests", ["method", "path", "status"])
http_latency  = Histogram("http_latency_seconds", "HTTP request latency", ["method", "path"],
                           buckets=[0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 15.0])
http_active   = Gauge("http_active_requests", "Currently active HTTP requests")
http_errors   = Counter("http_errors_total", "HTTP errors", ["method", "path", "status"])

class GoldenSignalMiddleware(BaseHTTPMiddleware):
    SKIP_PATHS = {"/healthz", "/metrics", "/favicon.ico"}

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path
        if path in self.SKIP_PATHS:
            return await call_next(request)

        method = request.method
        t0 = time.monotonic()
        http_active.inc()

        try:
            response = await call_next(request)
            status = response.status_code
        except Exception as exc:
            http_active.dec()
            http_errors.labels(method=method, path=path, status="500").inc()
            http_requests.labels(method=method, path=path, status="500").inc()
            http_latency.labels(method=method, path=path).observe(time.monotonic() - t0)
            raise

        elapsed = time.monotonic() - t0
        http_active.dec()
        http_requests.labels(method=method, path=path, status=str(status)).inc()
        http_latency.labels(method=method, path=path).observe(elapsed)
        if status >= 400:
            http_errors.labels(method=method, path=path, status=str(status)).inc()

        return response

app = FastAPI()
app.add_middleware(GoldenSignalMiddleware)

from prometheus_client import make_asgi_app
app.mount("/metrics", make_asgi_app())
```

**When to use**: FastAPI agents. Zero handler changes needed; middleware captures every request.

---

## Solution 3: LLM-Specific Signals — Token Throughput and Model Saturation

Beyond HTTP signals, track LLM-specific saturation: token throughput, concurrent model calls, and context window utilization.

```python
import asyncio
import time
from prometheus_client import Counter, Histogram, Gauge, Summary

# Token throughput (traffic by token count, not request count)
llm_input_tokens  = Counter("llm_input_tokens_total",  "Total input tokens consumed",  ["model"])
llm_output_tokens = Counter("llm_output_tokens_total", "Total output tokens generated", ["model"])
llm_tokens_per_second = Gauge("llm_tokens_per_second", "Current output token rate",    ["model"])

# LLM-specific latency
llm_call_latency = Histogram(
    "llm_call_latency_seconds",
    "Time for LLM API call to complete",
    ["model", "call_type"],  # call_type: complete | stream | tool_use
    buckets=[0.2, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0],
)

# Context window saturation
context_window_utilization = Histogram(
    "context_window_utilization_ratio",
    "Fraction of context window used (0-1)",
    ["model"],
    buckets=[0.1, 0.25, 0.5, 0.7, 0.85, 0.95, 1.0],
)

# Concurrent LLM calls (saturation indicator)
llm_concurrent_calls = Gauge("llm_concurrent_calls", "Active concurrent LLM API calls", ["model"])

# Rate limit hits
llm_rate_limit_hits = Counter("llm_rate_limit_hits_total", "429 rate limit responses", ["model"])

MODEL_CONTEXT_LIMITS = {
    "claude-sonnet-4-6": 200_000,
    "claude-opus-4-6": 200_000,
    "claude-haiku-4-5-20251001": 200_000,
}

async def instrumented_llm_call(model: str, messages: list[dict], **kwargs):
    from anthropic import AsyncAnthropic
    client = AsyncAnthropic()

    llm_concurrent_calls.labels(model=model).inc()
    t0 = time.monotonic()
    try:
        resp = await client.messages.create(model=model, messages=messages, **kwargs)
    except Exception as e:
        if "429" in str(e) or "rate_limit" in str(e).lower():
            llm_rate_limit_hits.labels(model=model).inc()
        raise
    finally:
        llm_concurrent_calls.labels(model=model).dec()

    elapsed = time.monotonic() - t0
    llm_call_latency.labels(model=model, call_type="complete").observe(elapsed)

    usage = resp.usage
    llm_input_tokens.labels(model=model).inc(usage.input_tokens)
    llm_output_tokens.labels(model=model).inc(usage.output_tokens)

    # Context window saturation
    total_tokens = usage.input_tokens + usage.output_tokens
    limit = MODEL_CONTEXT_LIMITS.get(model, 200_000)
    context_window_utilization.labels(model=model).observe(total_tokens / limit)

    # Rolling token/sec gauge
    tps = usage.output_tokens / max(elapsed, 0.001)
    llm_tokens_per_second.labels(model=model).set(tps)

    return resp
```

**When to use**: Deep LLM observability. Token throughput tells you capacity faster than request count.

---

## Solution 4: Error Rate by Category with SLO Burn Rate

Track error rate by category with a burn rate calculation to detect SLO violations early.

```python
import time
from collections import deque
from dataclasses import dataclass, field
from prometheus_client import Counter, Gauge
import logging

logger = logging.getLogger(__name__)

error_requests = Counter(
    "agent_error_requests_total",
    "Requests ending in error by category",
    ["category", "severity"],
    # category: model_api | tool_timeout | schema_validation | rate_limit | context_overflow | unknown
    # severity: critical | warning | info
)
slo_burn_rate = Gauge(
    "agent_slo_burn_rate",
    "Current error budget burn rate (1.0 = burning at SLO boundary rate)",
)
slo_error_budget_remaining = Gauge(
    "agent_slo_error_budget_remaining_ratio",
    "Fraction of monthly error budget remaining",
)

ERROR_CATEGORY_MAP = {
    "RateLimitError": ("rate_limit", "warning"),
    "APITimeoutError": ("model_api", "critical"),
    "ValidationError": ("schema_validation", "warning"),
    "ContextLengthError": ("context_overflow", "warning"),
    "ToolTimeoutError": ("tool_timeout", "critical"),
}

@dataclass
class BurnRateTracker:
    """Sliding window burn rate calculation (SRE Workbook approach)."""
    slo_target: float = 0.999       # 99.9% success rate
    window_1h: deque = field(default_factory=lambda: deque(maxlen=3600))
    window_5m: deque = field(default_factory=lambda: deque(maxlen=300))

    def record(self, ok: bool):
        entry = (time.monotonic(), ok)
        self.window_1h.append(entry)
        self.window_5m.append(entry)
        self._update_gauges()

    def _error_rate(self, window: deque) -> float:
        if not window:
            return 0.0
        errors = sum(1 for _, ok in window if not ok)
        return errors / len(window)

    def _update_gauges(self):
        error_rate_1h = self._error_rate(self.window_1h)
        allowed_error_rate = 1.0 - self.slo_target
        burn = error_rate_1h / allowed_error_rate if allowed_error_rate > 0 else 0.0
        slo_burn_rate.set(burn)

        # Approximate remaining budget (simplistic — use proper window in prod)
        budget_used = error_rate_1h / allowed_error_rate
        slo_error_budget_remaining.set(max(0.0, 1.0 - budget_used))

        if burn > 14.4:  # fast burn: exhaust 30-day budget in 2 hours
            logger.critical("slo_fast_burn_detected", extra={"burn_rate": round(burn, 2)})
        elif burn > 6.0:
            logger.warning("slo_elevated_burn", extra={"burn_rate": round(burn, 2)})

tracker = BurnRateTracker(slo_target=0.999)

def record_request_outcome(ok: bool, exc: Exception | None = None):
    tracker.record(ok)
    if not ok and exc is not None:
        exc_name = type(exc).__name__
        category, severity = ERROR_CATEGORY_MAP.get(exc_name, ("unknown", "warning"))
        error_requests.labels(category=category, severity=severity).inc()
```

**When to use**: SLO-driven agents. Burn rate fires fast-burn alerts hours before you'd run out of error budget.

---

## Solution 5: Grafana Dashboard JSON for Golden Signals

A ready-to-import Grafana dashboard provisioning config covering all four signals.

```python
# Generate a Grafana dashboard JSON programmatically
import json

def generate_golden_signal_dashboard(datasource: str = "Prometheus") -> dict:
    def panel(title, targets, y_unit="short", panel_type="graph", gridPos=None):
        return {
            "title": title,
            "type": panel_type,
            "datasource": datasource,
            "gridPos": gridPos or {"h": 8, "w": 12, "x": 0, "y": 0},
            "targets": [{"expr": t["expr"], "legendFormat": t.get("legend", ""), "refId": chr(65+i)}
                        for i, t in enumerate(targets)],
            "yaxes": [{"format": y_unit}],
            "options": {},
        }

    panels = [
        # TRAFFIC
        panel("Traffic — Requests/sec", [
            {"expr": "rate(agent_requests_total[1m])", "legend": "{{model}} {{endpoint}}"},
        ], y_unit="reqps", gridPos={"h": 8, "w": 12, "x": 0, "y": 0}),

        # LATENCY
        panel("Latency — p50/p95/p99", [
            {"expr": "histogram_quantile(0.50, rate(agent_request_latency_seconds_bucket[5m]))", "legend": "p50"},
            {"expr": "histogram_quantile(0.95, rate(agent_request_latency_seconds_bucket[5m]))", "legend": "p95"},
            {"expr": "histogram_quantile(0.99, rate(agent_request_latency_seconds_bucket[5m]))", "legend": "p99"},
        ], y_unit="s", gridPos={"h": 8, "w": 12, "x": 12, "y": 0}),

        # ERRORS
        panel("Errors — Error Rate %", [
            {"expr": "100 * rate(agent_errors_total[5m]) / rate(agent_requests_total[5m])", "legend": "Error %"},
        ], y_unit="percent", gridPos={"h": 8, "w": 12, "x": 0, "y": 8}),

        # SATURATION
        panel("Saturation — Active Requests & Queue", [
            {"expr": "agent_active_requests", "legend": "Active"},
            {"expr": "agent_queue_depth", "legend": "Queued"},
        ], y_unit="short", gridPos={"h": 8, "w": 12, "x": 12, "y": 8}),

        # SLO BURN RATE
        panel("SLO Burn Rate (>1 = burning budget)", [
            {"expr": "agent_slo_burn_rate", "legend": "Burn Rate"},
        ], y_unit="short", gridPos={"h": 6, "w": 24, "x": 0, "y": 16}),
    ]

    return {
        "title": "Agent Golden Signals",
        "uid": "agent-golden-signals",
        "schemaVersion": 36,
        "refresh": "30s",
        "panels": panels,
        "annotations": {"list": []},
        "templating": {"list": []},
        "time": {"from": "now-3h", "to": "now"},
    }

# Export to file for Grafana provisioning
if __name__ == "__main__":
    dash = generate_golden_signal_dashboard()
    with open("grafana/dashboards/agent-golden-signals.json", "w") as f:
        json.dump(dash, f, indent=2)
    print("Dashboard JSON written.")
```

**When to use**: Bootstrap Grafana visibility in < 10 minutes. Import the JSON via Grafana UI or provisioning directory.

---

## Solution 6: In-Process Real-Time Signal Reporter (No External Dependencies)

Emit a human-readable golden-signal report to logs every 60 seconds for agents without Prometheus.

```python
import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
import logging
import json

logger = logging.getLogger("golden_signals")

@dataclass
class SignalAccumulator:
    """Accumulate all four signals in-process; flush periodically."""
    window_seconds: float = 60.0
    _requests: deque = field(default_factory=lambda: deque())
    _latencies: deque = field(default_factory=lambda: deque())
    _errors: deque = field(default_factory=lambda: deque())
    _active: int = 0
    _queue: int = 0

    def _prune(self):
        cutoff = time.monotonic() - self.window_seconds
        while self._requests and self._requests[0] < cutoff:
            self._requests.popleft()
        while self._latencies and self._latencies[0][0] < cutoff:
            self._latencies.popleft()
        while self._errors and self._errors[0][0] < cutoff:
            self._errors.popleft()

    def record_request(self, latency_s: float, error: bool = False):
        now = time.monotonic()
        self._requests.append(now)
        self._latencies.append((now, latency_s))
        if error:
            self._errors.append((now, True))

    def set_active(self, n: int): self._active = n
    def set_queue(self, n: int): self._queue = n

    def report(self) -> dict:
        self._prune()
        n = len(self._requests)
        rps = n / self.window_seconds

        lats = sorted(v for _, v in self._latencies)
        p50 = lats[int(len(lats)*0.50)] if lats else 0.0
        p95 = lats[int(len(lats)*0.95)] if lats else 0.0

        error_rate = len(self._errors) / max(n, 1)

        return {
            "window_s": self.window_seconds,
            "traffic": {"rps": round(rps, 2), "total_requests": n},
            "latency": {"p50_ms": round(p50*1000, 1), "p95_ms": round(p95*1000, 1)},
            "errors": {"rate": round(error_rate, 4), "count": len(self._errors)},
            "saturation": {"active": self._active, "queued": self._queue},
        }

signals = SignalAccumulator(window_seconds=60.0)

async def golden_signal_reporter_loop(interval: float = 60.0):
    while True:
        await asyncio.sleep(interval)
        report = signals.report()
        logger.info("golden_signals", extra=report)

        # Flag degraded conditions inline
        if report["errors"]["rate"] > 0.05:
            logger.warning("HIGH_ERROR_RATE", extra={"rate": report["errors"]["rate"]})
        if report["latency"]["p95_ms"] > 5000:
            logger.warning("HIGH_P95_LATENCY", extra={"p95_ms": report["latency"]["p95_ms"]})
        if report["saturation"]["queued"] > 50:
            logger.warning("QUEUE_BACKED_UP", extra={"queued": report["saturation"]["queued"]})

# Start in your agent process
async def start_golden_signal_reporting():
    asyncio.create_task(golden_signal_reporter_loop(interval=60.0))
```

**When to use**: Agents deployed without a Prometheus stack. Structured log output can feed CloudWatch, Datadog, or Loki.

---

## Comparison

| Solution | Signals | External Deps | Dashboard | SLO Alerting | Best For |
|---|---|---|---|---|---|
| Prometheus 4-signal metrics | All 4 | Prometheus | Grafana | Via rules | Standard Prom/Grafana stack |
| FastAPI middleware | All 4 | Prometheus | Grafana | Via rules | FastAPI agents, zero handler change |
| LLM-specific signals | Traffic + Saturation | Prometheus | Grafana | Via rules | Token-cost-aware monitoring |
| Burn rate tracker | Errors | Prometheus | Grafana | Built-in | SLO budget tracking |
| Grafana JSON generator | All 4 | Grafana | Yes (generated) | Via Grafana | Bootstrap dashboards quickly |
| In-process reporter | All 4 | None | Log-based | Built-in warnings | No-infra agents |

**Rule of thumb**: Implement all four signals. Latency without error rate misses silent failures. Error rate without saturation misses capacity problems. Saturation without traffic misses whether load is expected.
