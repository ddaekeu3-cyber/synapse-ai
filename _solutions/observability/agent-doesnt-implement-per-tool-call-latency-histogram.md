---
title: "Agent doesn't implement per-tool-call latency histogram"
description: "The agent tracks only overall request latency. Individual tool call durations are invisible, so slow tools, P99 outliers, and latency regressions after tool updates go undetected."
difficulty: intermediate
category: observability
tags: [latency, histogram, p99, prometheus, tracing, tool-monitoring]
---

## Problem

When an agent request is slow, the culprit is almost always a specific tool call — a database lookup that started returning large payloads, an external API that added a new authentication step, or a search tool that now fetches five pages instead of one. Without per-tool-call latency histograms, you know the request is slow but not which tool caused it, making optimization impossible.

A latency histogram records the distribution of durations per tool, enabling P50/P95/P99 computation, trend tracking, and SLO alerting on individual tool calls.

```python
# BAD: no per-tool latency — only total request time is visible
async def run_agent(messages):
    start = time.monotonic()
    result = await client.messages.create(...)
    elapsed = time.monotonic() - start
    print(f"Total: {elapsed:.2f}s")  # which tool was slow? unknown
```

## Solution 1: Simple in-process histogram with percentile computation

Record call durations in a circular buffer per tool name. Compute P50/P95/P99 on demand.

```python
import time
import statistics
from collections import defaultdict
from typing import Callable, Awaitable, Any
from contextlib import asynccontextmanager
import asyncio


class LatencyHistogram:
    def __init__(self, max_samples: int = 1000):
        self._buckets: dict[str, list[float]] = defaultdict(list)
        self._max = max_samples

    def record(self, tool: str, elapsed_ms: float):
        bucket = self._buckets[tool]
        bucket.append(elapsed_ms)
        if len(bucket) > self._max:
            bucket.pop(0)

    def percentile(self, tool: str, p: float) -> float | None:
        """Return the p-th percentile latency in ms (p=0.99 for P99)."""
        data = sorted(self._buckets.get(tool, []))
        if not data:
            return None
        idx = max(0, int(len(data) * p) - 1)
        return data[idx]

    def summary(self, tool: str) -> dict:
        data = sorted(self._buckets.get(tool, []))
        if not data:
            return {"tool": tool, "samples": 0}
        return {
            "tool": tool,
            "samples": len(data),
            "p50_ms": round(data[int(len(data) * 0.50)], 1),
            "p95_ms": round(data[int(len(data) * 0.95)], 1),
            "p99_ms": round(data[min(int(len(data) * 0.99), len(data) - 1)], 1),
            "max_ms": round(data[-1], 1),
            "mean_ms": round(statistics.mean(data), 1),
        }

    def all_summaries(self) -> list[dict]:
        return [self.summary(tool) for tool in self._buckets]


histogram = LatencyHistogram()


@asynccontextmanager
async def timed_tool(name: str):
    start = time.monotonic()
    try:
        yield
    finally:
        elapsed_ms = (time.monotonic() - start) * 1000
        histogram.record(name, elapsed_ms)


# ── Usage ────────────────────────────────────────────────────────────
async def call_search(query: str) -> list[str]:
    async with timed_tool("web_search"):
        await asyncio.sleep(0.15)  # simulated latency
        return [f"result for {query}"]


async def call_database(key: str) -> dict:
    async with timed_tool("database_lookup"):
        await asyncio.sleep(0.04)
        return {"key": key, "value": "data"}


async def main():
    for _ in range(20):
        await call_search("AI agents")
        await call_database("user:42")

    for summary in histogram.all_summaries():
        print(summary)


asyncio.run(main())
```

## Solution 2: Prometheus histogram with HDR bucketing

Export latency histograms to Prometheus using standard HDR buckets (0.01s → 10s). Enables Grafana dashboards and alerting rules on P99 breach.

```python
import asyncio
import time
from contextlib import asynccontextmanager
from prometheus_client import Histogram, Counter, start_http_server

# Standard latency buckets: 10ms, 25ms, 50ms, 100ms, 250ms, 500ms, 1s, 2.5s, 5s, 10s
LATENCY_BUCKETS = (0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)

tool_latency = Histogram(
    "agent_tool_call_duration_seconds",
    "Latency of individual agent tool calls",
    labelnames=["tool_name", "status"],
    buckets=LATENCY_BUCKETS,
)

tool_calls_total = Counter(
    "agent_tool_calls_total",
    "Total tool calls by name and status",
    labelnames=["tool_name", "status"],
)


@asynccontextmanager
async def prometheus_timed_tool(name: str):
    start = time.monotonic()
    status = "success"
    try:
        yield
    except Exception:
        status = "error"
        raise
    finally:
        elapsed = time.monotonic() - start
        tool_latency.labels(tool_name=name, status=status).observe(elapsed)
        tool_calls_total.labels(tool_name=name, status=status).inc()


# ── Grafana alert rule (example — place in alerts.yaml) ──────────────
GRAFANA_ALERT_RULE = """
groups:
  - name: agent-tool-slo
    rules:
      - alert: ToolP99LatencyHigh
        expr: |
          histogram_quantile(
            0.99,
            rate(agent_tool_call_duration_seconds_bucket[5m])
          ) > 2.0
        for: 2m
        labels:
          severity: warning
        annotations:
          summary: "Tool {{ $labels.tool_name }} P99 latency > 2s"
"""


# ── Usage ────────────────────────────────────────────────────────────
async def search_tool(query: str) -> list:
    async with prometheus_timed_tool("web_search"):
        await asyncio.sleep(0.12)
        return []


async def main():
    start_http_server(8000)
    print("Prometheus metrics on :8000/metrics")
    for _ in range(50):
        await search_tool("test query")
    await asyncio.sleep(1)


asyncio.run(main())
```

## Solution 3: Decorator-based automatic instrumentation for all tool functions

Wrap every tool call automatically using a decorator. Register tools by name; the decorator records latency, error rate, and call count without touching each function individually.

```python
import asyncio
import time
import functools
from collections import defaultdict
from typing import Callable, Any
from dataclasses import dataclass, field


@dataclass
class ToolMetrics:
    name: str
    call_count: int = 0
    error_count: int = 0
    latencies_ms: list[float] = field(default_factory=list)
    MAX_SAMPLES: int = 500

    def record(self, elapsed_ms: float, error: bool):
        self.call_count += 1
        if error:
            self.error_count += 1
        self.latencies_ms.append(elapsed_ms)
        if len(self.latencies_ms) > self.MAX_SAMPLES:
            self.latencies_ms.pop(0)

    def percentile(self, p: float) -> float:
        if not self.latencies_ms:
            return 0.0
        data = sorted(self.latencies_ms)
        return data[int(len(data) * p)]

    def error_rate(self) -> float:
        return self.error_count / max(self.call_count, 1)


class ToolRegistry:
    def __init__(self):
        self._metrics: dict[str, ToolMetrics] = {}

    def instrument(self, name: str | None = None):
        """Decorator: wrap an async tool function with latency tracking."""
        def decorator(fn: Callable) -> Callable:
            tool_name = name or fn.__name__

            @functools.wraps(fn)
            async def wrapper(*args, **kwargs) -> Any:
                metrics = self._metrics.setdefault(
                    tool_name, ToolMetrics(name=tool_name)
                )
                start = time.monotonic()
                error = False
                try:
                    return await fn(*args, **kwargs)
                except Exception:
                    error = True
                    raise
                finally:
                    elapsed_ms = (time.monotonic() - start) * 1000
                    metrics.record(elapsed_ms, error)

            return wrapper
        return decorator

    def report(self) -> list[dict]:
        rows = []
        for m in self._metrics.values():
            rows.append({
                "tool": m.name,
                "calls": m.call_count,
                "error_rate": f"{m.error_rate():.1%}",
                "p50_ms": round(m.percentile(0.50), 1),
                "p95_ms": round(m.percentile(0.95), 1),
                "p99_ms": round(m.percentile(0.99), 1),
            })
        return sorted(rows, key=lambda r: r["p99_ms"], reverse=True)


# ── Global registry ───────────────────────────────────────────────────
registry = ToolRegistry()


@registry.instrument("web_search")
async def search(query: str) -> list[str]:
    await asyncio.sleep(0.1 + hash(query) % 50 / 1000)
    return [f"result:{query}"]


@registry.instrument("db_lookup")
async def lookup(key: str) -> dict:
    await asyncio.sleep(0.03)
    return {"key": key}


async def main():
    for i in range(30):
        await search(f"query-{i}")
        await lookup(f"user:{i}")

    import json
    print(json.dumps(registry.report(), indent=2))


asyncio.run(main())
```

## Solution 4: OTEL span-based per-tool latency with trace context propagation

Use OpenTelemetry spans so each tool call appears as a child span in distributed traces. Works with Jaeger, Tempo, and Datadog.

```python
import asyncio
import time
from contextlib import asynccontextmanager
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

# ── Setup (replace exporter with OTLP/Jaeger in production) ──────────
exporter = InMemorySpanExporter()
provider = TracerProvider()
provider.add_span_processor(SimpleSpanProcessor(exporter))
trace.set_tracer_provider(provider)
tracer = trace.get_tracer("agent.tools")


@asynccontextmanager
async def otel_tool_span(tool_name: str, **attributes):
    with tracer.start_as_current_span(
        f"tool.{tool_name}",
        kind=trace.SpanKind.CLIENT,
    ) as span:
        span.set_attribute("tool.name", tool_name)
        for k, v in attributes.items():
            span.set_attribute(k, str(v))
        try:
            yield span
        except Exception as e:
            span.record_exception(e)
            span.set_status(trace.StatusCode.ERROR, str(e))
            raise
        else:
            span.set_status(trace.StatusCode.OK)


# ── Instrument tools ──────────────────────────────────────────────────
async def otel_search(query: str) -> list[str]:
    async with otel_tool_span("web_search", query=query[:50]) as span:
        await asyncio.sleep(0.08)
        results = [f"result:{query}"]
        span.set_attribute("result.count", len(results))
        return results


async def otel_embed(text: str) -> list[float]:
    async with otel_tool_span("embedding", text_length=len(text)) as span:
        await asyncio.sleep(0.05)
        vec = [0.1] * 128
        span.set_attribute("embedding.dim", len(vec))
        return vec


# ── Extract latencies from spans ─────────────────────────────────────
def span_latency_report() -> list[dict]:
    spans = exporter.get_finished_spans()
    by_tool: dict[str, list[float]] = {}
    for span in spans:
        name = span.attributes.get("tool.name")
        if name:
            duration_ms = (span.end_time - span.start_time) / 1e6
            by_tool.setdefault(name, []).append(duration_ms)

    report = []
    for tool, latencies in by_tool.items():
        data = sorted(latencies)
        report.append({
            "tool": tool,
            "samples": len(data),
            "p50_ms": round(data[int(len(data) * 0.50)], 1),
            "p99_ms": round(data[min(int(len(data) * 0.99), len(data) - 1)], 1),
        })
    return report


async def main():
    for _ in range(20):
        await otel_search("AI agent latency")
        await otel_embed("some text")

    import json
    print(json.dumps(span_latency_report(), indent=2))


asyncio.run(main())
```

## Solution 5: Real-time latency budget enforcer with P99 auto-throttle

Track P99 latency per tool in a rolling window. If a tool's P99 exceeds its SLO budget, automatically throttle its concurrency to give the system time to recover.

```python
import asyncio
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class ToolSLO:
    name: str
    p99_budget_ms: float         # target P99 in ms
    window_seconds: float = 60.0
    throttle_factor: float = 0.5  # reduce concurrency to this fraction on breach


class LatencyBudgetEnforcer:
    def __init__(self):
        self._samples: dict[str, deque] = {}
        self._slos: dict[str, ToolSLO] = {}
        self._concurrency: dict[str, asyncio.Semaphore] = {}
        self._base_concurrency: dict[str, int] = {}
        self._throttled: dict[str, bool] = {}

    def register(self, slo: ToolSLO, max_concurrency: int = 10):
        self._slos[slo.name] = slo
        self._samples[slo.name] = deque()
        self._concurrency[slo.name] = asyncio.Semaphore(max_concurrency)
        self._base_concurrency[slo.name] = max_concurrency
        self._throttled[slo.name] = False

    def _p99(self, tool: str) -> float | None:
        now = time.monotonic()
        slo = self._slos[tool]
        samples = self._samples[tool]
        # Evict old samples
        while samples and now - samples[0][0] > slo.window_seconds:
            samples.popleft()
        if len(samples) < 5:
            return None
        data = sorted(s[1] for s in samples)
        return data[int(len(data) * 0.99)]

    def _maybe_throttle(self, tool: str, p99: float):
        slo = self._slos[tool]
        currently_throttled = self._throttled[tool]

        if p99 > slo.p99_budget_ms and not currently_throttled:
            new_limit = max(1, int(self._base_concurrency[tool] * slo.throttle_factor))
            self._concurrency[tool] = asyncio.Semaphore(new_limit)
            self._throttled[tool] = True
            print(f"[{tool}] P99={p99:.0f}ms > {slo.p99_budget_ms:.0f}ms — throttled to {new_limit} concurrent")
        elif p99 <= slo.p99_budget_ms and currently_throttled:
            self._concurrency[tool] = asyncio.Semaphore(self._base_concurrency[tool])
            self._throttled[tool] = False
            print(f"[{tool}] P99={p99:.0f}ms recovered — restored concurrency")

    async def call(self, tool: str, handler: Callable, *args, **kwargs) -> Any:
        sem = self._concurrency.get(tool, asyncio.Semaphore(10))
        async with sem:
            start = time.monotonic()
            try:
                return await handler(*args, **kwargs)
            finally:
                elapsed_ms = (time.monotonic() - start) * 1000
                self._samples[tool].append((time.monotonic(), elapsed_ms))
                p99 = self._p99(tool)
                if p99 is not None:
                    self._maybe_throttle(tool, p99)


# ── Usage ────────────────────────────────────────────────────────────
enforcer = LatencyBudgetEnforcer()
enforcer.register(ToolSLO("web_search", p99_budget_ms=200.0), max_concurrency=8)


async def slow_search(query: str) -> list:
    import random
    await asyncio.sleep(random.uniform(0.05, 0.4))  # simulate variable latency
    return []


async def main():
    tasks = [
        enforcer.call("web_search", slow_search, f"query-{i}")
        for i in range(50)
    ]
    await asyncio.gather(*tasks)


asyncio.run(main())
```

## Solution 6: CI latency regression gate — fail the build if P99 regresses

Run a latency benchmark in CI. Compare P99 per tool against the stored baseline; fail the build if any tool regresses by more than the allowed threshold.

```python
import asyncio
import json
import os
import sys
import time
from statistics import quantiles
from typing import Callable


BASELINE_FILE = ".latency_baselines.json"
REGRESSION_THRESHOLD = 0.20   # fail if P99 regresses by more than 20%
BENCHMARK_ITERATIONS = 100


async def benchmark_tool(name: str, fn: Callable, iterations: int) -> dict:
    latencies_ms = []
    for _ in range(iterations):
        start = time.monotonic()
        await fn()
        latencies_ms.append((time.monotonic() - start) * 1000)

    data = sorted(latencies_ms)
    n = len(data)
    return {
        "tool": name,
        "samples": n,
        "p50_ms": round(data[n // 2], 1),
        "p95_ms": round(data[int(n * 0.95)], 1),
        "p99_ms": round(data[int(n * 0.99)], 1),
    }


def load_baselines() -> dict[str, dict]:
    if not os.path.exists(BASELINE_FILE):
        return {}
    with open(BASELINE_FILE) as f:
        return json.load(f)


def save_baselines(results: list[dict]):
    baselines = {r["tool"]: r for r in results}
    with open(BASELINE_FILE, "w") as f:
        json.dump(baselines, f, indent=2)
    print(f"Baselines saved to {BASELINE_FILE}")


def check_regressions(current: list[dict], baselines: dict) -> list[str]:
    failures = []
    for result in current:
        tool = result["tool"]
        if tool not in baselines:
            continue
        baseline_p99 = baselines[tool]["p99_ms"]
        current_p99 = result["p99_ms"]
        regression = (current_p99 - baseline_p99) / max(baseline_p99, 1)
        if regression > REGRESSION_THRESHOLD:
            failures.append(
                f"{tool}: P99 regressed {regression:.0%} "
                f"({baseline_p99:.0f}ms → {current_p99:.0f}ms)"
            )
        else:
            print(f"  {tool}: P99={current_p99:.0f}ms (baseline={baseline_p99:.0f}ms) ✓")
    return failures


async def run_ci_latency_gate():
    # Define tools to benchmark
    async def search(): await asyncio.sleep(0.08)
    async def embed(): await asyncio.sleep(0.04)
    async def db(): await asyncio.sleep(0.02)

    tools = [
        ("web_search", search),
        ("embedding", embed),
        ("database", db),
    ]

    print(f"Benchmarking {BENCHMARK_ITERATIONS} iterations per tool...")
    results = []
    for name, fn in tools:
        result = await benchmark_tool(name, fn, BENCHMARK_ITERATIONS)
        results.append(result)
        print(f"  {name}: P99={result['p99_ms']}ms")

    baselines = load_baselines()

    if not baselines:
        print("No baselines found — saving current results as baseline")
        save_baselines(results)
        return

    failures = check_regressions(results, baselines)
    if failures:
        print("\nLATENCY REGRESSIONS DETECTED:")
        for f in failures:
            print(f"  ❌ {f}")
        sys.exit(1)
    else:
        print("\nAll tools within latency budget ✓")
        save_baselines(results)  # update baselines on success


asyncio.run(run_ci_latency_gate())
```

## Comparison

| Approach | Storage | P99 support | Real-time alerts | Distributed tracing | CI gate |
|---|---|---|---|---|---|
| In-process histogram | Memory | Yes | No | No | No |
| Prometheus histogram | Time-series DB | Yes (PromQL) | Yes (Alertmanager) | No | No |
| Decorator auto-instrumentation | Memory | Yes | No | No | No |
| OTEL spans | Trace backend | Yes | Yes (via backend) | Yes | No |
| P99 budget enforcer | Memory | Yes | Yes (auto-throttle) | No | No |
| CI latency regression gate | File | Yes | No | No | Yes |

**Recommendation**: Use the **decorator auto-instrumentation** (Solution 3) for zero-friction coverage of all tools. Export to **Prometheus** (Solution 2) for production dashboards and alerting. Add the **CI latency gate** (Solution 6) to catch regressions before deployment. Use **OTEL spans** (Solution 4) when you need to correlate tool latency with the full distributed trace.
