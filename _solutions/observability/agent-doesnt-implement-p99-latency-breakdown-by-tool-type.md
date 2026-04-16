---
title: "Agent Doesn't Implement P99 Latency Breakdown by Tool Type"
description: "Agents that report only overall turn latency cannot identify which tool type is responsible for P99 tail latency — whether slow turns are caused by an HTTP API endpoint, a database query, an embedding call, or the LLM itself. Implement P99 latency breakdown by tool type that maintains separate latency histograms per tool category, enabling targeted optimization of the slowest contributors."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-p99-latency-breakdown-by-tool-type
tags: [p99-latency, latency-breakdown, histogram, tail-latency, tool-type, performance-profiling]
symptoms:
  - "P99 turn latency is 8 seconds but it's unknown which tool causes the tail"
  - "All tool latencies are averaged into a single metric losing per-type signal"
  - "Cannot tell whether slow P99 is consistent (slow tool) or bursty (occasional timeout)"
  - "SLO negotiations with downstream teams require per-tool latency data that doesn't exist"
  - "Latency regressions after dependency updates are invisible without pre/post breakdown"
---

## Why This Happens

A single `tool_latency_ms` metric averaged across all tool types obscures which category is slow. An HTTP tool with P99=6s and a database tool with P99=50ms both contribute to the same metric — the average looks fine while the HTTP tail is unacceptable. Per-type histograms require categorizing each tool call, maintaining a sorted sample per category, and computing percentiles on demand. This lets engineers identify exactly which tool type needs optimization.

## Solution 1: Latency Sample Store

```python
import time
from collections import deque
from threading import Lock
from typing import Deque, Dict, List, Optional, Tuple


class LatencySampleStore:
    """
    Maintains a bounded sliding-window deque of latency samples
    per tool type key. Supports O(n log n) percentile queries.
    """

    def __init__(
        self,
        window_seconds: float = 3600.0,
        max_samples_per_key: int = 5000,
    ):
        self._window = window_seconds
        self._max = max_samples_per_key
        # key -> deque of (recorded_at, latency_ms)
        self._samples: Dict[str, Deque[Tuple[float, float]]] = {}
        self._lock = Lock()

    def record(self, key: str, latency_ms: float) -> None:
        now = time.time()
        with self._lock:
            if key not in self._samples:
                self._samples[key] = deque()
            dq = self._samples[key]
            dq.append((now, latency_ms))
            if len(dq) > self._max:
                dq.popleft()

    def percentile(self, key: str, pct: float) -> Optional[float]:
        cutoff = time.time() - self._window
        with self._lock:
            dq = self._samples.get(key)
            if not dq:
                return None
            values = sorted(ms for ts, ms in dq if ts >= cutoff)
        if not values:
            return None
        idx = min(int(len(values) * pct / 100.0), len(values) - 1)
        return round(values[idx], 2)

    def count(self, key: str) -> int:
        cutoff = time.time() - self._window
        with self._lock:
            dq = self._samples.get(key)
            if not dq:
                return 0
            return sum(1 for ts, _ in dq if ts >= cutoff)

    def all_keys(self) -> List[str]:
        with self._lock:
            return list(self._samples.keys())
```

## Solution 2: Tool Type Latency Recorder

```python
import time
from typing import Optional


class ToolTypeLatencyRecorder:
    """
    Records per-call latency keyed by both tool name and tool category.
    Supports querying P50/P95/P99 for any key.
    """

    def __init__(
        self,
        sample_store: LatencySampleStore,
        category_registry: Optional[object] = None,  # ToolCategoryRegistry
    ):
        self._store = sample_store
        self._registry = category_registry

    def record(self, tool_name: str, latency_ms: float, success: bool = True) -> None:
        self._store.record(f"tool:{tool_name}", latency_ms)
        self._store.record("all_tools", latency_ms)
        if self._registry:
            category = self._registry.category_of(tool_name).value
            self._store.record(f"category:{category}", latency_ms)
        status = "success" if success else "error"
        self._store.record(f"status:{status}", latency_ms)

    def p99(self, key: str) -> Optional[float]:
        return self._store.percentile(key, 99)

    def p95(self, key: str) -> Optional[float]:
        return self._store.percentile(key, 95)

    def p50(self, key: str) -> Optional[float]:
        return self._store.percentile(key, 50)

    def full_percentiles(self, key: str) -> dict:
        return {
            "p50_ms": self.p50(key),
            "p95_ms": self.p95(key),
            "p99_ms": self.p99(key),
            "count": self._store.count(key),
        }
```

## Solution 3: Latency SLO Checker

```python
from typing import Dict, List, Optional


class LatencySLOChecker:
    """
    Evaluates per-tool-type latency against configured SLO thresholds.
    Returns a list of violations for alerting or dashboard display.
    """

    def __init__(
        self,
        recorder: ToolTypeLatencyRecorder,
        slo_thresholds: Dict[str, float],   # key -> P99 threshold ms
        min_sample_count: int = 20,
    ):
        self._recorder = recorder
        self._thresholds = slo_thresholds
        self._min_samples = min_sample_count

    def evaluate(self) -> List[dict]:
        violations = []
        for key, threshold_ms in self._thresholds.items():
            count = self._recorder._store.count(key)
            if count < self._min_samples:
                continue
            p99 = self._recorder.p99(key)
            if p99 is not None and p99 > threshold_ms:
                violations.append({
                    "key": key,
                    "p99_ms": p99,
                    "threshold_ms": threshold_ms,
                    "excess_ms": round(p99 - threshold_ms, 2),
                    "excess_pct": round((p99 - threshold_ms) / threshold_ms * 100, 1),
                    "sample_count": count,
                })
        return sorted(violations, key=lambda v: -v["excess_pct"])
```

## Solution 4: Latency Anomaly Detector

```python
from typing import Optional


class LatencyAnomalyDetector:
    """
    Detects sudden P99 spikes by comparing the current hour's P99
    to the prior hour's P99 for each tool type.
    """

    def __init__(
        self,
        sample_store: LatencySampleStore,
        spike_threshold_multiplier: float = 2.0,
    ):
        self._store = sample_store
        self._threshold = spike_threshold_multiplier

    def _percentile_window(
        self, key: str, window_start_offset: float, window_seconds: float, pct: float
    ) -> Optional[float]:
        import time
        cutoff_end = time.time() - window_start_offset
        cutoff_start = cutoff_end - window_seconds
        with self._store._lock:
            dq = self._store._samples.get(key)
            if not dq:
                return None
            values = sorted(
                ms for ts, ms in dq
                if cutoff_start <= ts <= cutoff_end
            )
        if not values:
            return None
        idx = min(int(len(values) * pct / 100.0), len(values) - 1)
        return values[idx]

    def detect_spikes(self) -> list:
        spikes = []
        for key in self._store.all_keys():
            current_p99 = self._percentile_window(key, 0, 3600, 99)
            prior_p99 = self._percentile_window(key, 3600, 3600, 99)
            if current_p99 and prior_p99 and prior_p99 > 0:
                if current_p99 > prior_p99 * self._threshold:
                    spikes.append({
                        "key": key,
                        "current_p99_ms": current_p99,
                        "prior_p99_ms": prior_p99,
                        "spike_factor": round(current_p99 / prior_p99, 2),
                    })
        return sorted(spikes, key=lambda s: -s["spike_factor"])
```

## Solution 5: Instrumented Tool Call Wrapper

```python
import time
from typing import Any, Callable


class P99InstrumentedToolWrapper:
    """
    Wraps every tool call to record latency automatically.
    Drop-in wrapper that requires no changes to tool implementations.
    """

    def __init__(self, recorder: ToolTypeLatencyRecorder):
        self._recorder = recorder

    async def wrap(
        self,
        tool_name: str,
        fn: Callable,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        start = time.time()
        success = True
        try:
            result = await fn(*args, **kwargs)
            return result
        except Exception:
            success = False
            raise
        finally:
            latency_ms = round((time.time() - start) * 1000, 2)
            self._recorder.record(tool_name, latency_ms, success)
```

## Solution 6: P99 Latency Breakdown Dashboard

```python
import time
from typing import List


class P99LatencyBreakdownDashboard:
    """
    Renders a full per-tool-type latency breakdown with SLO status,
    anomaly detection, and worst-offender identification.
    """

    def __init__(
        self,
        recorder: ToolTypeLatencyRecorder,
        slo_checker: LatencySLOChecker,
        anomaly_detector: LatencyAnomalyDetector,
    ):
        self._recorder = recorder
        self._slo = slo_checker
        self._anomaly = anomaly_detector

    def render(self) -> dict:
        keys = self._recorder._store.all_keys()
        per_key = {}
        for key in keys:
            per_key[key] = self._recorder.full_percentiles(key)

        violations = self._slo.evaluate()
        spikes = self._anomaly.detect_spikes()

        worst = sorted(
            [(k, v["p99_ms"]) for k, v in per_key.items() if v["p99_ms"] is not None],
            key=lambda x: -(x[1] or 0),
        )[:5]

        return {
            "generated_at": time.time(),
            "per_tool_type": per_key,
            "slo_violations": violations,
            "latency_spikes": spikes,
            "worst_p99": [{"key": k, "p99_ms": v} for k, v in worst],
        }
```

## Comparison

| Approach | Per-Type Recording | Percentile Queries | SLO Checking | Spike Detection | Dashboard |
|---|---|---|---|---|---|
| LatencySampleStore | Yes (keyed) | Yes (sorted) | No | No | No |
| ToolTypeLatencyRecorder | Yes (name+category) | Yes (P50/95/99) | No | No | No |
| LatencySLOChecker | Via recorder | Via recorder | Yes | No | No |
| LatencyAnomalyDetector | Via store | Via store | No | Yes (2-window) | No |
| P99LatencyBreakdownDashboard | No | No | Via checker | Via detector | Yes |

**Best for production**: Maintain separate keys for tool name (`tool:search_kb`) and category (`category:http`) — tool-level keys identify the specific offender while category-level keys drive SLO negotiations with providers. Set `window_seconds=3600` for real-time alerting and use a second store with `window_seconds=86400` for daily trend reports — percentiles require a meaningful sample size and short windows on low-traffic tools can be misleading. Alert on `LatencyAnomalyDetector` spikes with `spike_factor > 3×` as a P0 signal: a 3× P99 increase usually indicates an infrastructure change (deployment, config update) rather than load-driven degradation.
