---
title: "Agent Doesn't Implement Latency Percentile Tracking Per Model"
description: "Average latency is misleading — a model averaging 500ms might have p99 of 8 seconds. Per-model latency percentile tracking (p50/p95/p99) reveals tail latency problems, enables informed model routing, and catches SLA violations before users notice."
difficulty: intermediate
category: observability
tags: [observability, latency, percentiles, p99, per-model, metrics, performance, SLA]
---

## Problem

An agent logs average response time across all model calls. The average looks fine at 600ms, but p99 is 12 seconds — 1% of requests are painfully slow. Without percentile tracking, these outliers are invisible until users complain. With per-model percentile data, you can see that Haiku's p99 is 2s but Sonnet's p99 is 12s, enabling intelligent model routing and alerting on actual SLA violations.

```python
# BAD: only tracking average — hides tail latency
total_time = 0
call_count = 0

async def call_model(prompt: str) -> str:
    start = time.time()
    result = await client.messages.create(...)
    total_time += time.time() - start
    call_count += 1
    print(f"Avg latency: {total_time/call_count:.2f}s")  # misleading
    return result
```

## Solution 1: In-Memory Reservoir Sampling with Percentiles

Use reservoir sampling to maintain a fixed-size sample of latencies, then compute percentiles.

```python
import asyncio
import random
import time
import math
from anthropic import AsyncAnthropic
from dataclasses import dataclass, field
from collections import defaultdict

client = AsyncAnthropic()

class ReservoirSampler:
    """
    Reservoir sampling maintains a statistically representative sample
    of a stream without storing all values.
    """
    def __init__(self, reservoir_size: int = 1000):
        self._reservoir: list[float] = []
        self._count = 0
        self._size = reservoir_size

    def add(self, value: float):
        self._count += 1
        if len(self._reservoir) < self._size:
            self._reservoir.append(value)
        else:
            # Replace a random element with decreasing probability
            j = random.randint(0, self._count - 1)
            if j < self._size:
                self._reservoir[j] = value

    def percentile(self, p: float) -> float:
        if not self._reservoir:
            return 0.0
        sorted_vals = sorted(self._reservoir)
        idx = int(math.ceil(p / 100 * len(sorted_vals))) - 1
        return sorted_vals[max(0, min(idx, len(sorted_vals) - 1))]

    def summary(self) -> dict:
        if not self._reservoir:
            return {}
        return {
            "count": self._count,
            "p50_ms": round(self.percentile(50) * 1000, 1),
            "p90_ms": round(self.percentile(90) * 1000, 1),
            "p95_ms": round(self.percentile(95) * 1000, 1),
            "p99_ms": round(self.percentile(99) * 1000, 1),
            "min_ms": round(min(self._reservoir) * 1000, 1),
            "max_ms": round(max(self._reservoir) * 1000, 1),
        }

class PerModelLatencyTracker:
    def __init__(self, reservoir_size: int = 1000):
        self._samplers: dict[str, ReservoirSampler] = defaultdict(
            lambda: ReservoirSampler(reservoir_size)
        )

    def record(self, model: str, latency_seconds: float):
        self._samplers[model].add(latency_seconds)

    def get_summary(self, model: str) -> dict:
        return {"model": model, **self._samplers[model].summary()}

    def all_summaries(self) -> list[dict]:
        return [self.get_summary(m) for m in sorted(self._samplers.keys())]

    def check_sla(self, model: str, p99_sla_ms: float) -> bool:
        summary = self._samplers[model].summary()
        return summary.get("p99_ms", 0) <= p99_sla_ms

# Global tracker
tracker = PerModelLatencyTracker()

async def tracked_call(
    prompt: str,
    model: str = "claude-haiku-4-5-20251001"
) -> str:
    start = time.perf_counter()
    try:
        response = await client.messages.create(
            model=model,
            max_tokens=256,
            messages=[{"role": "user", "content": prompt}]
        )
        return response.content[0].text if response.content else ""
    finally:
        latency = time.perf_counter() - start
        tracker.record(model, latency)

async def main():
    # Simulate calls to different models
    models = ["claude-haiku-4-5-20251001"] * 8 + ["claude-haiku-4-5-20251001"] * 2
    prompts = [f"Brief answer: what is concept {i}?" for i in range(10)]

    await asyncio.gather(*[
        tracked_call(p, m) for p, m in zip(prompts, models)
    ])

    print("Per-model latency percentiles:")
    for summary in tracker.all_summaries():
        print(f"\n  Model: {summary['model']}")
        for k, v in summary.items():
            if k != "model":
                print(f"    {k}: {v}")

    # SLA check
    model = "claude-haiku-4-5-20251001"
    sla_ok = tracker.check_sla(model, p99_sla_ms=10000)
    print(f"\nSLA check ({model} p99 ≤ 10s): {'✓ PASS' if sla_ok else '✗ FAIL'}")

asyncio.run(main())
```

## Solution 2: Rolling Window Percentiles with Time Decay

Track latency percentiles over rolling time windows to detect regressions.

```python
import asyncio
import time
from collections import deque
from anthropic import AsyncAnthropic
from dataclasses import dataclass, field

client = AsyncAnthropic()

@dataclass
class TimedSample:
    value: float
    timestamp: float = field(default_factory=time.time)

class RollingWindowPercentile:
    def __init__(self, window_seconds: float = 300.0):  # 5-minute window
        self._window = window_seconds
        self._samples: deque[TimedSample] = deque()

    def add(self, value: float):
        self._samples.append(TimedSample(value))
        self._evict_stale()

    def _evict_stale(self):
        cutoff = time.time() - self._window
        while self._samples and self._samples[0].timestamp < cutoff:
            self._samples.popleft()

    def percentile(self, p: float) -> float | None:
        self._evict_stale()
        if not self._samples:
            return None
        sorted_vals = sorted(s.value for s in self._samples)
        import math
        idx = int(math.ceil(p / 100 * len(sorted_vals))) - 1
        return sorted_vals[max(0, min(idx, len(sorted_vals) - 1))]

    def summary(self) -> dict:
        self._evict_stale()
        if not self._samples:
            return {"count": 0}
        vals = [s.value for s in self._samples]
        return {
            "count": len(vals),
            "window_seconds": self._window,
            "p50_ms": round((self.percentile(50) or 0) * 1000, 1),
            "p95_ms": round((self.percentile(95) or 0) * 1000, 1),
            "p99_ms": round((self.percentile(99) or 0) * 1000, 1),
            "oldest_sample_age_s": round(time.time() - self._samples[0].timestamp, 1) if self._samples else 0,
        }

class RollingPerModelTracker:
    WINDOWS = {"1m": 60.0, "5m": 300.0, "15m": 900.0}

    def __init__(self):
        self._trackers: dict[str, dict[str, RollingWindowPercentile]] = {}

    def _get_or_create(self, model: str) -> dict[str, RollingWindowPercentile]:
        if model not in self._trackers:
            self._trackers[model] = {
                name: RollingWindowPercentile(window_secs)
                for name, window_secs in self.WINDOWS.items()
            }
        return self._trackers[model]

    def record(self, model: str, latency_s: float):
        for window_tracker in self._get_or_create(model).values():
            window_tracker.add(latency_s)

    def report(self, model: str) -> dict:
        windows = self._get_or_create(model)
        return {
            "model": model,
            **{f"{name}_{k}": v
               for name, tracker in windows.items()
               for k, v in tracker.summary().items()
               if k != "window_seconds"}
        }

rolling_tracker = RollingPerModelTracker()

async def rolling_tracked_call(prompt: str, model: str = "claude-haiku-4-5-20251001") -> str:
    start = time.perf_counter()
    try:
        response = await client.messages.create(
            model=model, max_tokens=128,
            messages=[{"role": "user", "content": prompt}]
        )
        return response.content[0].text if response.content else ""
    finally:
        rolling_tracker.record(model, time.perf_counter() - start)

async def main():
    prompts = [f"One-word answer: color {i}" for i in range(5)]
    await asyncio.gather(*[rolling_tracked_call(p) for p in prompts])

    report = rolling_tracker.report("claude-haiku-4-5-20251001")
    print("Rolling window percentiles:")
    for k, v in report.items():
        if k != "model":
            print(f"  {k}: {v}")

asyncio.run(main())
```

## Solution 3: Histogram-Based Percentile Approximation (HDR-style)

Use bucketed histograms for memory-efficient percentile tracking at high throughput.

```python
import asyncio
import time
import math
from anthropic import AsyncAnthropic
from dataclasses import dataclass, field

client = AsyncAnthropic()

class HistogramPercentile:
    """
    Log-scale histogram for efficient percentile approximation.
    Bucket boundaries: 0-10ms, 10-25ms, 25-50ms, 50-100ms, ... up to 60s
    """
    BUCKET_BOUNDARIES_MS = [0, 10, 25, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 30000, 60000]

    def __init__(self):
        self._buckets = [0] * (len(self.BUCKET_BOUNDARIES_MS))
        self._total = 0
        self._sum = 0.0
        self._min = float("inf")
        self._max = 0.0

    def add(self, latency_seconds: float):
        ms = latency_seconds * 1000
        self._total += 1
        self._sum += ms
        self._min = min(self._min, ms)
        self._max = max(self._max, ms)

        for i, boundary in enumerate(self.BUCKET_BOUNDARIES_MS):
            if ms <= boundary:
                self._buckets[i] += 1
                return
        self._buckets[-1] += 1

    def percentile(self, p: float) -> float:
        if self._total == 0:
            return 0.0
        target = math.ceil(p / 100 * self._total)
        cumulative = 0
        for i, count in enumerate(self._buckets):
            cumulative += count
            if cumulative >= target:
                lo = self.BUCKET_BOUNDARIES_MS[i - 1] if i > 0 else 0
                hi = self.BUCKET_BOUNDARIES_MS[i]
                return (lo + hi) / 2  # midpoint of bucket
        return self.BUCKET_BOUNDARIES_MS[-1]

    def summary(self) -> dict:
        if self._total == 0:
            return {"count": 0}
        return {
            "count": self._total,
            "mean_ms": round(self._sum / self._total, 1),
            "min_ms": round(self._min, 1),
            "max_ms": round(self._max, 1),
            "p50_ms": round(self.percentile(50), 1),
            "p90_ms": round(self.percentile(90), 1),
            "p95_ms": round(self.percentile(95), 1),
            "p99_ms": round(self.percentile(99), 1),
        }

class HistogramTracker:
    def __init__(self):
        self._histograms: dict[str, HistogramPercentile] = {}

    def record(self, model: str, latency_s: float):
        if model not in self._histograms:
            self._histograms[model] = HistogramPercentile()
        self._histograms[model].add(latency_s)

    def compare_models(self) -> list[dict]:
        results = []
        for model in sorted(self._histograms):
            results.append({"model": model, **self._histograms[model].summary()})
        return results

hist_tracker = HistogramTracker()

async def histogram_tracked_call(prompt: str, model: str) -> str:
    start = time.perf_counter()
    try:
        response = await client.messages.create(
            model=model, max_tokens=128,
            messages=[{"role": "user", "content": prompt}]
        )
        return response.content[0].text if response.content else ""
    finally:
        hist_tracker.record(model, time.perf_counter() - start)

async def main():
    # Run calls to two configurations
    tasks = [
        histogram_tracked_call(f"Question {i}", "claude-haiku-4-5-20251001")
        for i in range(6)
    ]
    await asyncio.gather(*tasks)

    print(f"{'Model':<35} {'p50':>8} {'p95':>8} {'p99':>8} {'max':>8}")
    print("-" * 65)
    for r in hist_tracker.compare_models():
        print(f"{r['model']:<35} {r.get('p50_ms',0):>7.0f}ms {r.get('p95_ms',0):>7.0f}ms {r.get('p99_ms',0):>7.0f}ms {r.get('max_ms',0):>7.0f}ms")

asyncio.run(main())
```

## Solution 4: First-Token vs. Total Latency Tracking

Separately track time-to-first-token (TTFT) and total completion time for streaming responses.

```python
import asyncio
import time
from anthropic import AsyncAnthropic
from dataclasses import dataclass, field
from collections import defaultdict

client = AsyncAnthropic()

@dataclass
class StreamingLatencyRecord:
    model: str
    ttft_ms: float       # time to first token
    total_ms: float      # total completion time
    output_tokens: int
    tokens_per_second: float

class StreamingLatencyTracker:
    def __init__(self):
        self._records: dict[str, list[StreamingLatencyRecord]] = defaultdict(list)

    def record(self, rec: StreamingLatencyRecord):
        self._records[rec.model].append(rec)

    def _pct(self, values: list[float], p: float) -> float:
        if not values:
            return 0.0
        sorted_v = sorted(values)
        import math
        idx = int(math.ceil(p / 100 * len(sorted_v))) - 1
        return sorted_v[max(0, min(idx, len(sorted_v) - 1))]

    def summary(self, model: str) -> dict:
        recs = self._records.get(model, [])
        if not recs:
            return {}
        ttfts = [r.ttft_ms for r in recs]
        totals = [r.total_ms for r in recs]
        tps = [r.tokens_per_second for r in recs]
        return {
            "count": len(recs),
            "ttft_p50_ms": round(self._pct(ttfts, 50), 1),
            "ttft_p95_ms": round(self._pct(ttfts, 95), 1),
            "ttft_p99_ms": round(self._pct(ttfts, 99), 1),
            "total_p50_ms": round(self._pct(totals, 50), 1),
            "total_p95_ms": round(self._pct(totals, 95), 1),
            "total_p99_ms": round(self._pct(totals, 99), 1),
            "avg_tokens_per_sec": round(sum(tps) / len(tps), 1),
        }

stream_tracker = StreamingLatencyTracker()

async def streaming_latency_call(prompt: str, model: str = "claude-haiku-4-5-20251001") -> str:
    request_start = time.perf_counter()
    first_token_time: float | None = None
    output_text = ""

    async with client.messages.stream(
        model=model,
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}]
    ) as stream:
        async for text in stream.text_stream:
            if first_token_time is None:
                first_token_time = time.perf_counter()
            output_text += text

        final = await stream.get_final_message()

    total_s = time.perf_counter() - request_start
    ttft_s = (first_token_time - request_start) if first_token_time else total_s
    output_tokens = final.usage.output_tokens
    tps = output_tokens / max(total_s - ttft_s, 0.001)

    stream_tracker.record(StreamingLatencyRecord(
        model=model,
        ttft_ms=ttft_s * 1000,
        total_ms=total_s * 1000,
        output_tokens=output_tokens,
        tokens_per_second=tps,
    ))
    return output_text

async def main():
    prompts = [f"Explain briefly: topic {i}" for i in range(5)]
    await asyncio.gather(*[streaming_latency_call(p) for p in prompts])

    summary = stream_tracker.summary("claude-haiku-4-5-20251001")
    print("Streaming latency breakdown:")
    print(f"  TTFT p50/p95/p99: {summary.get('ttft_p50_ms')}ms / {summary.get('ttft_p95_ms')}ms / {summary.get('ttft_p99_ms')}ms")
    print(f"  Total p50/p95/p99: {summary.get('total_p50_ms')}ms / {summary.get('total_p95_ms')}ms / {summary.get('total_p99_ms')}ms")
    print(f"  Avg tokens/sec: {summary.get('avg_tokens_per_sec')}")

asyncio.run(main())
```

## Solution 5: SLA Alerting with Percentile Thresholds

Fire alerts when percentile thresholds are breached, with configurable per-model SLAs.

```python
import asyncio
import time
from anthropic import AsyncAnthropic
from dataclasses import dataclass
from typing import Callable

client = AsyncAnthropic()

@dataclass
class SLAConfig:
    model: str
    p95_threshold_ms: float
    p99_threshold_ms: float
    min_samples: int = 10  # don't alert with too few samples

@dataclass
class SLAViolation:
    model: str
    percentile: str
    threshold_ms: float
    actual_ms: float
    sample_count: int

class SLAMonitor:
    def __init__(self, sla_configs: list[SLAConfig], alert_fn: Callable | None = None):
        self._slas = {c.model: c for c in sla_configs}
        self._samples: dict[str, list[float]] = {}
        self._alert_fn = alert_fn or self._default_alert
        self._violations: list[SLAViolation] = []

    def _default_alert(self, violation: SLAViolation):
        print(
            f"[SLA VIOLATION] {violation.model}: "
            f"{violation.percentile} = {violation.actual_ms:.0f}ms "
            f"(threshold: {violation.threshold_ms:.0f}ms, "
            f"samples: {violation.sample_count})"
        )

    def record(self, model: str, latency_s: float):
        if model not in self._samples:
            self._samples[model] = []
        self._samples[model].append(latency_s * 1000)
        self._check_sla(model)

    def _pct(self, values: list[float], p: float) -> float:
        import math
        sorted_v = sorted(values)
        idx = int(math.ceil(p / 100 * len(sorted_v))) - 1
        return sorted_v[max(0, min(idx, len(sorted_v) - 1))]

    def _check_sla(self, model: str):
        sla = self._slas.get(model)
        if not sla:
            return
        samples = self._samples.get(model, [])
        if len(samples) < sla.min_samples:
            return

        for pct, threshold in [("p95", sla.p95_threshold_ms), ("p99", sla.p99_threshold_ms)]:
            p_val = 95.0 if pct == "p95" else 99.0
            actual = self._pct(samples, p_val)
            if actual > threshold:
                violation = SLAViolation(model, pct, threshold, actual, len(samples))
                self._violations.append(violation)
                self._alert_fn(violation)

sla_monitor = SLAMonitor([
    SLAConfig("claude-haiku-4-5-20251001", p95_threshold_ms=5000, p99_threshold_ms=10000, min_samples=3),
    SLAConfig("claude-sonnet-4-6", p95_threshold_ms=8000, p99_threshold_ms=15000, min_samples=3),
])

async def sla_monitored_call(prompt: str, model: str = "claude-haiku-4-5-20251001") -> str:
    start = time.perf_counter()
    try:
        response = await client.messages.create(
            model=model, max_tokens=256,
            messages=[{"role": "user", "content": prompt}]
        )
        return response.content[0].text if response.content else ""
    finally:
        sla_monitor.record(model, time.perf_counter() - start)

async def main():
    prompts = [f"Brief answer on topic {i}" for i in range(5)]
    results = await asyncio.gather(*[sla_monitored_call(p) for p in prompts])
    print(f"Completed {len(results)} calls")
    print(f"SLA violations detected: {len(sla_monitor._violations)}")

asyncio.run(main())
```

## Solution 6: Prometheus-Compatible Metrics Export

Export percentile metrics in Prometheus format for integration with monitoring stacks.

```python
import asyncio
import time
import math
from anthropic import AsyncAnthropic
from dataclasses import dataclass, field
from collections import defaultdict

client = AsyncAnthropic()

@dataclass
class QuantileSummary:
    """Simplified streaming quantile approximation (t-digest-inspired)."""
    samples: list[float] = field(default_factory=list)
    max_samples: int = 500

    def add(self, value: float):
        self.samples.append(value)
        if len(self.samples) > self.max_samples:
            # Downsample: keep evenly spaced samples
            step = len(self.samples) // (self.max_samples // 2)
            self.samples = self.samples[::step]

    def quantile(self, q: float) -> float:
        if not self.samples:
            return 0.0
        sorted_s = sorted(self.samples)
        idx = int(math.ceil(q * len(sorted_s))) - 1
        return sorted_s[max(0, min(idx, len(sorted_s) - 1))]

    def count(self) -> int:
        return len(self.samples)

    def sum(self) -> float:
        return sum(self.samples)

class PrometheusLatencyRegistry:
    QUANTILES = [0.5, 0.9, 0.95, 0.99]

    def __init__(self, metric_name: str = "agent_llm_request_duration_seconds"):
        self._metric_name = metric_name
        self._summaries: dict[str, QuantileSummary] = defaultdict(QuantileSummary)

    def record(self, model: str, latency_s: float):
        self._summaries[model].add(latency_s)

    def emit_prometheus(self) -> str:
        lines = [
            f"# HELP {self._metric_name} LLM request duration in seconds",
            f"# TYPE {self._metric_name} summary",
        ]
        for model, summary in sorted(self._summaries.items()):
            safe_model = model.replace("-", "_").replace(".", "_")
            for q in self.QUANTILES:
                val = summary.quantile(q)
                lines.append(
                    f'{self._metric_name}{{model="{model}",quantile="{q}"}} {val:.6f}'
                )
            lines.append(
                f'{self._metric_name}_count{{model="{model}"}} {summary.count()}'
            )
            lines.append(
                f'{self._metric_name}_sum{{model="{model}"}} {summary.sum():.6f}'
            )
        return "\n".join(lines)

prom_registry = PrometheusLatencyRegistry()

async def prom_tracked_call(prompt: str, model: str = "claude-haiku-4-5-20251001") -> str:
    start = time.perf_counter()
    try:
        response = await client.messages.create(
            model=model, max_tokens=128,
            messages=[{"role": "user", "content": prompt}]
        )
        return response.content[0].text if response.content else ""
    finally:
        prom_registry.record(model, time.perf_counter() - start)

async def main():
    await asyncio.gather(*[
        prom_tracked_call(f"Brief: topic {i}") for i in range(5)
    ])

    metrics = prom_registry.emit_prometheus()
    print("Prometheus metrics output:")
    print(metrics)

asyncio.run(main())
```

## Comparison

| Approach | Memory | Accuracy | Query Speed | Best For |
|---|---|---|---|---|
| Reservoir Sampling | Fixed (N samples) | ~1% error | O(N log N) | General purpose, low memory |
| Rolling Window | O(N×windows) | Exact | O(N log N) | Trend detection, recent focus |
| Histogram Buckets | Fixed (bucket count) | Bucket-width error | O(buckets) | High throughput, real-time |
| TTFT + Total | Per-model O(N) | Exact | O(N log N) | Streaming latency decomposition |
| SLA Alerting | O(N) | Exact | O(N log N) | SLA enforcement, alerting |
| Prometheus Export | O(N) | ~2% error | O(N log N) | Existing monitoring stacks |

**Rule of thumb**: Start with reservoir sampling (low memory, accurate enough). Add rolling windows when you need to catch regressions over time. Integrate Prometheus export when you have an existing monitoring stack — it takes 10 lines and unlocks Grafana dashboards for free.
