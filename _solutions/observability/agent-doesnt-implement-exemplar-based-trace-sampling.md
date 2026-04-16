---
title: "Agent Doesn't Implement Exemplar-Based Trace Sampling"
description: "AI agents that emit metrics without linking them to representative traces make it impossible to jump from a latency histogram to the actual slow request. Exemplar-based sampling attaches a trace ID to each metric data point, creating a navigable bridge between dashboards and distributed traces."
date: 2025-02-01
difficulty: advanced
category: observability
slug: agent-doesnt-implement-exemplar-based-trace-sampling
tags:
  - exemplars
  - tracing
  - sampling
  - opentelemetry
  - prometheus
  - observability
  - metrics
symptoms:
  - "Latency dashboards show p99 spikes but there is no way to find the actual slow request"
  - "Error rate jumps appear in metrics but the corresponding traces have already been sampled away"
  - "High-cardinality breakdowns (per-user, per-tool) are lost when traces are head-sampled"
  - "On-call engineers spend 10+ minutes correlating a metric anomaly to a trace ID"
  - "Tail-latency outliers are systematically excluded by uniform sampling strategies"
---

## Problem

Standard distributed tracing uses head-based sampling: a small percentage of requests are traced, and the decision is made at the start of each request. This works well for average latency but systematically discards outliers — the exact requests you need to debug.

Exemplars solve the correlation gap between metrics and traces. An exemplar is a specific, sampled data point (a trace ID + labels) attached to a metric observation. When a Prometheus histogram records a latency bucket, it can also record: "the request that contributed to this bucket had trace_id=abc123, and here is a link to it in Jaeger/Tempo."

OpenTelemetry natively supports exemplars. Prometheus 2.43+ stores them. Grafana can render them as clickable dots on histogram charts.

---

## Solution 1: Manual Exemplar Recorder

Record exemplars alongside Prometheus histograms. Each histogram observation optionally carries a trace ID so operators can jump from a metric spike to the exact trace.

```python
import time
import random
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


@dataclass
class Exemplar:
    value: float
    trace_id: str
    span_id: str
    labels: Dict[str, str]
    timestamp: float = field(default_factory=time.time)


class ExemplarHistogram:
    """
    Histogram that stores one exemplar per bucket.
    Compatible with Prometheus exposition format exemplars.

    Usage:
        hist = ExemplarHistogram(
            name="agent_tool_latency_seconds",
            buckets=[0.01, 0.05, 0.1, 0.5, 1.0, 5.0],
        )
        with trace_context() as ctx:
            t0 = time.monotonic()
            await call_tool()
            hist.observe(time.monotonic() - t0, ctx.trace_id, ctx.span_id,
                         labels={"tool": "web_search"})

        print(hist.prometheus_text())
    """

    def __init__(self, name: str, buckets: List[float],
                 labels: Optional[Dict[str, str]] = None):
        self.name = name
        self.buckets = sorted(buckets)
        self._label_defaults = labels or {}
        self._counts = [0] * (len(buckets) + 1)   # +inf bucket
        self._sum = 0.0
        self._total = 0
        self._exemplars: Dict[int, Exemplar] = {}  # bucket_idx -> Exemplar

    def observe(self, value: float,
                trace_id: Optional[str] = None,
                span_id: Optional[str] = None,
                labels: Optional[Dict[str, str]] = None):
        self._sum += value
        self._total += 1
        bucket_idx = len(self.buckets)  # default: +inf bucket
        for i, b in enumerate(self.buckets):
            if value <= b:
                bucket_idx = i
                break
        for i in range(bucket_idx, len(self.buckets) + 1):
            self._counts[i] += 1

        if trace_id and span_id:
            # Replace exemplar with probability proportional to recency
            self._exemplars[bucket_idx] = Exemplar(
                value=value,
                trace_id=trace_id,
                span_id=span_id,
                labels={**self._label_defaults, **(labels or {})},
            )

    def prometheus_text(self) -> str:
        lines = [f"# HELP {self.name} Latency histogram with exemplars"]
        lines.append(f"# TYPE {self.name} histogram")
        for i, b in enumerate(self.buckets):
            exemplar_str = ""
            if i in self._exemplars:
                ex = self._exemplars[i]
                exemplar_str = (
                    f" # {{trace_id=\"{ex.trace_id}\","
                    f"span_id=\"{ex.span_id}\"}} "
                    f"{ex.value:.6f} {ex.timestamp:.3f}"
                )
                for k, v in ex.labels.items():
                    exemplar_str = exemplar_str.replace(
                        f"span_id=\"{ex.span_id}\"}}",
                        f"span_id=\"{ex.span_id}\",{k}=\"{v}\"}}"
                    )
            lines.append(f'{self.name}_bucket{{le="{b}"}} {self._counts[i]}{exemplar_str}')
        lines.append(f'{self.name}_bucket{{le="+Inf"}} {self._counts[-1]}')
        lines.append(f'{self.name}_sum {self._sum:.6f}')
        lines.append(f'{self.name}_count {self._total}')
        return "\n".join(lines)
```

---

## Solution 2: Tail-Latency Exemplar Sampler

Standard head-based sampling discards slow requests. This sampler uses reservoir sampling weighted by latency — slow requests are exponentially more likely to be retained as exemplars.

```python
import math
import random
import time
from dataclasses import dataclass
from typing import List, Optional, Tuple


@dataclass
class TraceExemplar:
    trace_id: str
    span_id: str
    latency_ms: float
    error: bool
    labels: dict
    sampled_at: float = 0.0

    def __post_init__(self):
        if not self.sampled_at:
            self.sampled_at = time.time()


class TailLatencyExemplarSampler:
    """
    Keeps a reservoir of exemplars biased toward high-latency requests.
    Each incoming trace competes to replace reservoir entries based on
    a latency-weighted random score.

    Usage:
        sampler = TailLatencyExemplarSampler(reservoir_size=50)
        sampler.observe(TraceExemplar("abc", "def", latency_ms=850, error=False, labels={}))
        top_slow = sampler.top_by_latency(10)
    """

    def __init__(self, reservoir_size: int = 50):
        self._size = reservoir_size
        self._reservoir: List[Tuple[float, TraceExemplar]] = []
        self._total_seen = 0

    def observe(self, exemplar: TraceExemplar):
        self._total_seen += 1
        # Weight: log(latency) so slow requests have much higher weight
        weight = math.log1p(exemplar.latency_ms) + (10.0 if exemplar.error else 0.0)
        score = weight * random.random()

        if len(self._reservoir) < self._size:
            self._reservoir.append((score, exemplar))
            self._reservoir.sort(key=lambda x: x[0])
        elif score > self._reservoir[0][0]:
            self._reservoir[0] = (score, exemplar)
            self._reservoir.sort(key=lambda x: x[0])

    def top_by_latency(self, n: int = 10) -> List[TraceExemplar]:
        sorted_res = sorted(self._reservoir, key=lambda x: x[1].latency_ms, reverse=True)
        return [ex for _, ex in sorted_res[:n]]

    def error_exemplars(self) -> List[TraceExemplar]:
        return [ex for _, ex in self._reservoir if ex.error]

    def p99_trace_id(self) -> Optional[str]:
        """Return the trace ID closest to the p99 observed so far."""
        if not self._reservoir:
            return None
        by_latency = sorted(self._reservoir, key=lambda x: x[1].latency_ms)
        idx = int(len(by_latency) * 0.99)
        return by_latency[min(idx, len(by_latency) - 1)][1].trace_id
```

---

## Solution 3: OpenTelemetry Exemplar Context Propagator

Integrates with OpenTelemetry spans. Every metric observation automatically picks up the current span's trace context as its exemplar, with no manual trace ID passing.

```python
import time
from contextlib import contextmanager
from typing import Optional

try:
    from opentelemetry import trace
    from opentelemetry.trace import Span, SpanContext
    HAS_OTEL = True
except ImportError:
    HAS_OTEL = False


class OTelExemplarContext:
    """
    Reads the current OTel span context and returns it as an exemplar dict.
    Use this in metric observation callbacks to auto-attach trace IDs.

    Usage:
        ctx = OTelExemplarContext.current()
        if ctx:
            histogram.observe(latency, trace_id=ctx["trace_id"],
                              span_id=ctx["span_id"])
    """

    @staticmethod
    def current() -> Optional[dict]:
        if not HAS_OTEL:
            return None
        span = trace.get_current_span()
        if span is trace.INVALID_SPAN:
            return None
        ctx: SpanContext = span.get_span_context()
        if not ctx.is_valid:
            return None
        return {
            "trace_id": format(ctx.trace_id, "032x"),
            "span_id": format(ctx.span_id, "016x"),
            "trace_flags": ctx.trace_flags,
        }


class OTelExemplarHistogram:
    """
    Histogram that automatically captures the current OTel span as an exemplar.

    Usage:
        hist = OTelExemplarHistogram("agent_latency_seconds")

        with tracer.start_as_current_span("tool_call"):
            t0 = time.monotonic()
            await call_tool()
            hist.observe(time.monotonic() - t0, labels={"tool": "search"})
    """

    def __init__(self, name: str,
                 buckets: Optional[list] = None):
        self._inner = ExemplarHistogram(
            name,
            buckets or [0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10],
        )

    def observe(self, value: float, labels: Optional[dict] = None):
        ctx = OTelExemplarContext.current()
        if ctx:
            self._inner.observe(
                value,
                trace_id=ctx["trace_id"],
                span_id=ctx["span_id"],
                labels=labels,
            )
        else:
            self._inner.observe(value, labels=labels)

    def prometheus_text(self) -> str:
        return self._inner.prometheus_text()
```

---

## Solution 4: Exemplar-Enriched Error Rate Tracker

For each error category, retain the most recent trace ID and the worst-latency trace ID. Instantly provides a "show me an example" link from the error-rate panel.

```python
import time
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple


@dataclass
class ErrorExemplar:
    trace_id: str
    span_id: str
    error_type: str
    message: str
    latency_ms: float
    timestamp: float = field(default_factory=time.time)


class ExemplarErrorRateTracker:
    """
    Tracks error rates with per-category exemplars.

    Usage:
        tracker = ExemplarErrorRateTracker()
        # On each request:
        tracker.record_success("web_search", latency_ms=45.0, trace_id="abc", span_id="def")
        tracker.record_error("web_search", "TimeoutError", "upstream timed out",
                             latency_ms=5000.0, trace_id="xyz", span_id="uvw")

        report = tracker.report()
        # report["web_search"]["error_exemplar"]["trace_id"] -> "xyz"
    """

    def __init__(self):
        self._success: Dict[str, int] = {}
        self._errors: Dict[str, int] = {}
        self._latest_error: Dict[str, ErrorExemplar] = {}
        self._worst_error: Dict[str, ErrorExemplar] = {}

    def record_success(self, operation: str, latency_ms: float,
                       trace_id: str = "", span_id: str = ""):
        self._success[operation] = self._success.get(operation, 0) + 1

    def record_error(self, operation: str, error_type: str, message: str,
                     latency_ms: float, trace_id: str = "", span_id: str = ""):
        self._errors[operation] = self._errors.get(operation, 0) + 1
        exemplar = ErrorExemplar(
            trace_id=trace_id, span_id=span_id,
            error_type=error_type, message=message, latency_ms=latency_ms,
        )
        self._latest_error[operation] = exemplar
        prev_worst = self._worst_error.get(operation)
        if prev_worst is None or latency_ms > prev_worst.latency_ms:
            self._worst_error[operation] = exemplar

    def report(self) -> Dict[str, dict]:
        ops = set(list(self._success.keys()) + list(self._errors.keys()))
        result = {}
        for op in ops:
            s = self._success.get(op, 0)
            e = self._errors.get(op, 0)
            total = s + e
            result[op] = {
                "total": total,
                "errors": e,
                "error_rate": round(e / max(1, total), 4),
                "latest_error_exemplar": (
                    self._format(self._latest_error.get(op))
                ),
                "worst_latency_exemplar": (
                    self._format(self._worst_error.get(op))
                ),
            }
        return result

    def _format(self, ex: Optional[ErrorExemplar]) -> Optional[dict]:
        if ex is None:
            return None
        return {
            "trace_id": ex.trace_id,
            "span_id": ex.span_id,
            "error_type": ex.error_type,
            "message": ex.message[:100],
            "latency_ms": ex.latency_ms,
            "timestamp": ex.timestamp,
        }
```

---

## Solution 5: Exemplar-Linked SLO Budget Tracker

Links SLO burn-rate anomalies to specific traces so engineers can investigate the cause of a budget burn within seconds.

```python
import time
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class SLOBurnExemplar:
    window: str        # "1h" | "6h" | "24h"
    burn_rate: float
    threshold: float
    trace_id: str
    span_id: str
    latency_ms: float
    timestamp: float = field(default_factory=time.time)


class ExemplarLinkedSLOTracker:
    """
    Tracks SLO error budget burn rate and records exemplars when burn exceeds threshold.

    Usage:
        slo = ExemplarLinkedSLOTracker(slo_target=0.999, burn_threshold=2.0)
        slo.record(success=False, latency_ms=6000, trace_id="abc", span_id="xyz")
        slo.record(success=True, latency_ms=50, trace_id="def", span_id="ghi")

        if slo.is_burning_fast("1h"):
            alert(slo.burn_exemplars("1h"))
    """

    WINDOWS = {"1h": 3600, "6h": 21600, "24h": 86400}

    def __init__(self, slo_target: float = 0.999,
                 burn_threshold: float = 2.0,
                 latency_slo_ms: float = 1000.0):
        self._target = slo_target
        self._burn_threshold = burn_threshold
        self._latency_slo = latency_slo_ms
        self._events: List[dict] = []
        self._exemplars: List[SLOBurnExemplar] = []

    def record(self, success: bool, latency_ms: float,
               trace_id: str = "", span_id: str = ""):
        self._events.append({
            "ts": time.time(),
            "ok": success and latency_ms <= self._latency_slo,
            "latency_ms": latency_ms,
            "trace_id": trace_id,
            "span_id": span_id,
        })

    def _error_rate(self, window_seconds: int) -> float:
        cutoff = time.time() - window_seconds
        recent = [e for e in self._events if e["ts"] >= cutoff]
        if not recent:
            return 0.0
        bad = sum(1 for e in recent if not e["ok"])
        return bad / len(recent)

    def burn_rate(self, window: str = "1h") -> float:
        seconds = self.WINDOWS[window]
        error_rate = self._error_rate(seconds)
        allowed_error_rate = 1.0 - self._target
        return error_rate / max(1e-9, allowed_error_rate)

    def is_burning_fast(self, window: str = "1h") -> bool:
        return self.burn_rate(window) > self._burn_threshold

    def record_burn_exemplar(self, window: str, trace_id: str,
                              span_id: str, latency_ms: float):
        br = self.burn_rate(window)
        if br > self._burn_threshold:
            self._exemplars.append(SLOBurnExemplar(
                window=window,
                burn_rate=round(br, 2),
                threshold=self._burn_threshold,
                trace_id=trace_id,
                span_id=span_id,
                latency_ms=latency_ms,
            ))

    def burn_exemplars(self, window: Optional[str] = None) -> List[dict]:
        exs = [e for e in self._exemplars if window is None or e.window == window]
        return [
            {
                "window": e.window, "burn_rate": e.burn_rate,
                "trace_id": e.trace_id, "latency_ms": e.latency_ms,
                "timestamp": e.timestamp,
            }
            for e in sorted(exs, key=lambda x: x.burn_rate, reverse=True)[:10]
        ]
```

---

## Solution 6: Unified Exemplar Telemetry Middleware

Drop-in async middleware for agent tool calls. Every invocation records latency and result to the exemplar histogram, error tracker, and SLO tracker automatically.

```python
import asyncio
import time
import uuid
from typing import Any, Callable, Optional


class ExemplarTelemetryMiddleware:
    """
    Wraps agent tool calls with full exemplar telemetry:
    - Latency histogram with trace exemplars
    - Error rate tracker with error exemplars
    - SLO burn tracker with burn exemplars

    Usage:
        mw = ExemplarTelemetryMiddleware(slo_latency_ms=500)
        result = await mw.call("web_search", web_search_fn, query="...")
        print(mw.report())
    """

    def __init__(self, slo_latency_ms: float = 500.0,
                 slo_target: float = 0.999):
        self._hist = OTelExemplarHistogram("agent_tool_latency_seconds")
        self._errors = ExemplarErrorRateTracker()
        self._slo = ExemplarLinkedSLOTracker(
            slo_target=slo_target, latency_slo_ms=slo_latency_ms
        )

    async def call(self, operation: str, fn: Callable,
                   trace_id: Optional[str] = None,
                   span_id: Optional[str] = None, **kwargs) -> Any:
        trace_id = trace_id or uuid.uuid4().hex
        span_id = span_id or uuid.uuid4().hex[:16]
        t0 = time.monotonic()
        try:
            result = await fn(**kwargs)
            latency_ms = (time.monotonic() - t0) * 1000
            self._hist.observe(latency_ms / 1000)
            self._errors.record_success(operation, latency_ms, trace_id, span_id)
            self._slo.record(True, latency_ms, trace_id, span_id)
            self._slo.record_burn_exemplar("1h", trace_id, span_id, latency_ms)
            return result
        except Exception as exc:
            latency_ms = (time.monotonic() - t0) * 1000
            self._hist.observe(latency_ms / 1000)
            self._errors.record_error(
                operation, type(exc).__name__, str(exc),
                latency_ms, trace_id, span_id
            )
            self._slo.record(False, latency_ms, trace_id, span_id)
            self._slo.record_burn_exemplar("1h", trace_id, span_id, latency_ms)
            raise

    def report(self) -> dict:
        return {
            "error_rates": self._errors.report(),
            "slo_burn_1h": self._slo.burn_rate("1h"),
            "burn_exemplars": self._slo.burn_exemplars("1h"),
            "prometheus_metrics": self._hist.prometheus_text(),
        }
```

---

## Comparison

| Approach | Links Metrics → Traces | Sampling Bias | Prometheus Compatible |
|---|---|---|---|
| **Manual Exemplar Recorder** | Yes (per bucket) | None (last write wins) | Yes |
| **Tail Latency Sampler** | Yes (reservoir) | Biased toward slow requests | Via export |
| **OTel Context Propagator** | Yes (auto from span) | Inherits OTel sampling | Yes |
| **Error Rate Exemplar Tracker** | Yes (latest + worst) | Biased toward errors | Via export |
| **SLO Burn Exemplar Tracker** | Yes (burn events) | Biased toward budget burns | Via export |
| **Unified Middleware** | Yes (all of above) | Composite | Yes |

**Key insight**: exemplars cost almost nothing to record (one dict per histogram bucket) but cut mean-time-to-trace from minutes to seconds. Enable them from day one and configure Grafana to render them as clickable points on latency histograms.
