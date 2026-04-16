---
title: "Agent Doesn't Implement LLM Response Time Histogram Tracking"
description: "Agents that only log average LLM latency miss the tail distribution: a P99 of 12s and a P50 of 1.2s both average to ~1.5s with normal traffic, but the user experience is dominated by the tail. Implement a histogram-based latency tracker that records per-request durations into configurable buckets, computes P50/P90/P99 percentiles, and alerts when the tail exceeds SLO thresholds."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-llm-response-time-histogram-tracking
tags: [latency-histogram, p99, response-time, slo-monitoring, tail-latency, percentile-tracking]
symptoms:
  - "Average LLM latency looks healthy at 1.5s but users complain about slow responses"
  - "No P90 or P99 metrics — impossible to set or monitor latency SLOs"
  - "Latency is logged as a single value per request with no distribution tracking"
  - "Cannot tell if slow responses are isolated spikes or a sustained trend"
  - "Alert fires on average latency only after P99 has been degraded for hours"
---

## Why This Happens

Average latency is a misleading metric for LLM calls because the distribution is right-skewed: most responses are fast but a small fraction are very slow. Averaging these hides the tail. Histogram-based tracking bins durations into configurable buckets (e.g., 0–500ms, 500ms–1s, 1–2s, 2–5s, 5–10s, 10s+), counts how many requests fall into each bucket, and computes exact percentiles from the bucket counts without storing every individual measurement. This gives accurate P50/P90/P99 at O(buckets) memory rather than O(requests).

## Solution 1: Latency Histogram

```python
import bisect
import threading
import time
from typing import Dict, List, Optional, Tuple


class LatencyHistogram:
    """
    Fixed-bucket histogram for latency measurements (in milliseconds).
    Bucket boundaries are configurable; the last bucket captures all values
    exceeding the highest boundary (open upper bound).
    """

    DEFAULT_BUCKETS_MS = [50, 100, 200, 500, 1000, 2000, 5000, 10000, 30000]

    def __init__(self, buckets_ms: Optional[List[float]] = None, label: str = ""):
        self._buckets = sorted(buckets_ms or self.DEFAULT_BUCKETS_MS)
        self._counts: List[int] = [0] * (len(self._buckets) + 1)
        self._total_count: int = 0
        self._sum_ms: float = 0.0
        self._min_ms: Optional[float] = None
        self._max_ms: Optional[float] = None
        self._label = label
        self._lock = threading.Lock()

    def record(self, duration_ms: float) -> None:
        with self._lock:
            idx = bisect.bisect_right(self._buckets, duration_ms)
            self._counts[idx] += 1
            self._total_count += 1
            self._sum_ms += duration_ms
            if self._min_ms is None or duration_ms < self._min_ms:
                self._min_ms = duration_ms
            if self._max_ms is None or duration_ms > self._max_ms:
                self._max_ms = duration_ms

    def percentile(self, pct: float) -> Optional[float]:
        """
        Returns the approximate percentile value using linear interpolation
        within the bucket that contains the target rank.
        """
        with self._lock:
            if self._total_count == 0:
                return None
            target_rank = pct / 100.0 * self._total_count
            cumulative = 0
            for i, count in enumerate(self._counts):
                cumulative += count
                if cumulative >= target_rank:
                    lower = self._buckets[i - 1] if i > 0 else 0.0
                    upper = self._buckets[i] if i < len(self._buckets) else self._buckets[-1] * 2
                    # Linear interpolation within bucket
                    bucket_start_rank = cumulative - count
                    if count == 0:
                        return lower
                    fraction = (target_rank - bucket_start_rank) / count
                    return round(lower + fraction * (upper - lower), 2)
        return None

    def snapshot(self) -> dict:
        with self._lock:
            avg = round(self._sum_ms / max(self._total_count, 1), 2)
            bucket_dist = {}
            for i, boundary in enumerate(self._buckets):
                label = f"le_{int(boundary)}ms"
                bucket_dist[label] = self._counts[i]
            bucket_dist["le_inf"] = self._counts[-1]
            return {
                "label": self._label,
                "count": self._total_count,
                "sum_ms": round(self._sum_ms, 2),
                "avg_ms": avg,
                "min_ms": self._min_ms,
                "max_ms": self._max_ms,
                "p50_ms": self.percentile(50),
                "p90_ms": self.percentile(90),
                "p99_ms": self.percentile(99),
                "buckets": bucket_dist,
            }

    def reset(self) -> None:
        with self._lock:
            self._counts = [0] * (len(self._buckets) + 1)
            self._total_count = 0
            self._sum_ms = 0.0
            self._min_ms = None
            self._max_ms = None
```

## Solution 2: Per-Model Histogram Registry

```python
from typing import Dict, List, Optional


class PerModelHistogramRegistry:
    """
    Maintains one histogram per (model, operation) combination.
    Auto-creates histograms on first use.
    """

    def __init__(self, buckets_ms: Optional[List[float]] = None):
        self._buckets = buckets_ms or LatencyHistogram.DEFAULT_BUCKETS_MS
        self._histograms: Dict[str, LatencyHistogram] = {}
        self._lock = __import__("threading").Lock()

    def _key(self, model: str, operation: str) -> str:
        return f"{model}:{operation}"

    def record(self, model: str, operation: str, duration_ms: float) -> None:
        key = self._key(model, operation)
        with self._lock:
            if key not in self._histograms:
                self._histograms[key] = LatencyHistogram(
                    buckets_ms=self._buckets,
                    label=key,
                )
        self._histograms[key].record(duration_ms)

    def get(self, model: str, operation: str) -> Optional[LatencyHistogram]:
        return self._histograms.get(self._key(model, operation))

    def all_snapshots(self) -> List[dict]:
        return [h.snapshot() for h in self._histograms.values()]
```

## Solution 3: Timed LLM Call Wrapper

```python
import asyncio
import time
from typing import Any, Callable, Dict, Optional


class TimedLLMCallWrapper:
    """
    Wraps LLM API calls to automatically record duration into the histogram.
    Records separately for first-token latency (TTFT) and total latency.
    """

    def __init__(self, registry: PerModelHistogramRegistry):
        self._registry = registry

    async def call(
        self,
        model: str,
        llm_fn: Callable,
        operation: str = "completion",
        **kwargs: Any,
    ) -> Any:
        start = time.time()
        try:
            result = await llm_fn(**kwargs)
            duration_ms = (time.time() - start) * 1000
            self._registry.record(model, operation, duration_ms)
            return result
        except Exception:
            duration_ms = (time.time() - start) * 1000
            self._registry.record(model, f"{operation}:error", duration_ms)
            raise

    async def call_streaming(
        self,
        model: str,
        llm_fn: Callable,
        on_first_token: Optional[Callable[[float], None]] = None,
        **kwargs: Any,
    ):
        """
        Async generator wrapper for streaming calls.
        Records TTFT when the first token arrives.
        """
        start = time.time()
        first_token_recorded = False
        async for chunk in llm_fn(**kwargs):
            if not first_token_recorded:
                ttft_ms = (time.time() - start) * 1000
                self._registry.record(model, "ttft", ttft_ms)
                first_token_recorded = True
                if on_first_token:
                    on_first_token(ttft_ms)
            yield chunk
        total_ms = (time.time() - start) * 1000
        self._registry.record(model, "streaming_total", total_ms)
```

## Solution 4: SLO Evaluator

```python
from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass
class LatencySLO:
    model: str
    operation: str
    percentile: float       # e.g. 99.0 for P99
    threshold_ms: float     # SLO target in milliseconds
    label: str = ""


class SLOEvaluator:
    """
    Evaluates defined SLOs against current histogram percentiles.
    Returns pass/fail status and the measured value for each SLO.
    """

    def __init__(self, registry: PerModelHistogramRegistry):
        self._registry = registry
        self._slos: List[LatencySLO] = []

    def register_slo(self, slo: LatencySLO) -> None:
        self._slos.append(slo)

    def evaluate(self) -> List[dict]:
        results = []
        for slo in self._slos:
            hist = self._registry.get(slo.model, slo.operation)
            if hist is None:
                results.append({
                    "model": slo.model,
                    "operation": slo.operation,
                    "percentile": slo.percentile,
                    "threshold_ms": slo.threshold_ms,
                    "measured_ms": None,
                    "status": "no_data",
                    "label": slo.label,
                })
                continue
            measured = hist.percentile(slo.percentile)
            passing = measured is not None and measured <= slo.threshold_ms
            results.append({
                "model": slo.model,
                "operation": slo.operation,
                "percentile": slo.percentile,
                "threshold_ms": slo.threshold_ms,
                "measured_ms": measured,
                "status": "pass" if passing else "fail",
                "breach_ms": round(measured - slo.threshold_ms, 2) if measured and not passing else None,
                "label": slo.label,
            })
        return results
```

## Solution 5: Sliding Window Histogram

```python
import threading
import time
from collections import deque
from typing import Deque, List, Optional


class SlidingWindowLatencyHistogram:
    """
    Maintains a rolling histogram over the last N seconds.
    Implemented as a deque of (timestamp, duration_ms) pairs.
    Less memory-efficient than a fixed histogram but supports
    exact windowed percentiles without bucket approximation.
    Uses reservoir sampling when the window grows too large.
    """

    def __init__(self, window_seconds: float = 300.0, max_samples: int = 10_000):
        self._window = window_seconds
        self._max = max_samples
        self._samples: Deque[tuple] = deque()
        self._lock = threading.Lock()

    def record(self, duration_ms: float) -> None:
        with self._lock:
            now = time.time()
            self._samples.append((now, duration_ms))
            self._trim(now)

    def _trim(self, now: float) -> None:
        cutoff = now - self._window
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()
        if len(self._samples) > self._max:
            # Keep every other sample to stay within limit
            as_list = list(self._samples)
            self._samples = deque(as_list[::2])

    def percentile(self, pct: float) -> Optional[float]:
        with self._lock:
            self._trim(time.time())
            if not self._samples:
                return None
            values = sorted(s[1] for s in self._samples)
            idx = int(pct / 100 * len(values))
            idx = min(idx, len(values) - 1)
            return round(values[idx], 2)

    def snapshot(self) -> dict:
        with self._lock:
            self._trim(time.time())
            if not self._samples:
                return {"count": 0, "window_seconds": self._window}
            values = [s[1] for s in self._samples]
            return {
                "count": len(values),
                "window_seconds": self._window,
                "p50_ms": self.percentile(50),
                "p90_ms": self.percentile(90),
                "p99_ms": self.percentile(99),
                "avg_ms": round(sum(values) / len(values), 2),
                "max_ms": round(max(values), 2),
            }
```

## Solution 6: Latency SLO Dashboard

```python
import time


class LatencySLODashboard:
    """
    Combines per-model histogram snapshots and SLO evaluation
    into a single observability report.
    """

    def __init__(
        self,
        registry: PerModelHistogramRegistry,
        slo_evaluator: SLOEvaluator,
        sliding_window: Optional[SlidingWindowLatencyHistogram] = None,
    ):
        self._registry = registry
        self._slo = slo_evaluator
        self._sliding = sliding_window

    def render(self) -> dict:
        slo_results = self._slo.evaluate()
        failing_slos = [r for r in slo_results if r["status"] == "fail"]

        report = {
            "generated_at": time.time(),
            "histograms": self._registry.all_snapshots(),
            "slo_results": slo_results,
            "failing_slos": failing_slos,
            "healthy": len(failing_slos) == 0,
        }
        if self._sliding:
            report["rolling_5min"] = self._sliding.snapshot()
        return report
```

## Comparison

| Approach | Bucket Histogram | Percentile | Per-Model | SLO Evaluation | Sliding Window |
|---|---|---|---|---|---|
| LatencyHistogram | Yes | Yes (approx) | No | No | No |
| PerModelHistogramRegistry | Via histogram | Via histogram | Yes | No | No |
| TimedLLMCallWrapper | No | No | Via registry | No | No |
| SLOEvaluator | No | No | No | Yes | No |
| SlidingWindowLatencyHistogram | No | Yes (exact, windowed) | No | No | Yes |
| LatencySLODashboard | No | No | No | Yes | Optional |

**Best for production**: Use `PerModelHistogramRegistry` with the default buckets for all-time percentiles, and pair it with `SlidingWindowLatencyHistogram` (5-minute window) for real-time alerting. The fixed histogram is memory-efficient and suitable for long-running processes; the sliding window is exact and ideal for short-window SLO burn-rate calculations. Register SLOs at P99: for interactive agents set the P99 threshold at 5s; for batch processing set it at 30s. Wire `SLOEvaluator` results to your alerting system — a P99 breach should page on-call within 5 minutes, while a P90 breach warrants a warning notification.
