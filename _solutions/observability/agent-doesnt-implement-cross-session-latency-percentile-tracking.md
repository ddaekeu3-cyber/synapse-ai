---
title: "Agent Doesn't Implement Cross-Session Latency Percentile Tracking"
description: "Agents that measure latency per-session in memory lose all percentile history on restart and cannot answer 'what was our P95 tool call latency over the past 24 hours across all sessions?' Without cross-session latency aggregation, SLO reporting is based on anecdotal observation rather than statistical evidence. Implement cross-session latency percentile tracking that accumulates tool call durations across sessions and restarts and supports P50/P95/P99 queries over sliding time windows."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-cross-session-latency-percentile-tracking
tags: [latency-percentiles, cross-session, p99-tracking, slo-monitoring, sliding-window, latency-aggregation]
symptoms:
  - "No answer to 'what was P95 latency for tool X over the last hour?' across all sessions"
  - "Latency data disappears on restart — no persistent percentile history"
  - "SLO compliance is checked against per-session averages, not population percentiles"
  - "Cannot detect that P99 for one tool has been degrading over the past week"
  - "Dashboards show only current-session latency with no cross-session aggregation"
---

## Why This Happens

Latency measurements collected inside a session object are session-scoped. When the session ends or the process restarts, the data is gone. Percentile computation requires a population of observations — a single session's 20 calls cannot produce a meaningful P99. Cross-session tracking requires a shared, persistent store that accumulates observations from all sessions and supports efficient percentile queries over time windows. The most practical approach for single-instance deployments is a time-bucketed ring buffer that can be serialized to disk; for multi-instance deployments, a shared time-series store (Redis Sorted Set, InfluxDB, or Prometheus histogram) is required.

## Solution 1: Latency Observation

```python
import time
from dataclasses import dataclass
from typing import Optional


@dataclass
class LatencyObservation:
    tool_name: str
    latency_ms: float
    recorded_at: float
    session_id: str = ""
    success: bool = True
    error_type: Optional[str] = None

    @staticmethod
    def now(tool_name: str, latency_ms: float, **kwargs) -> "LatencyObservation":
        return LatencyObservation(
            tool_name=tool_name,
            latency_ms=latency_ms,
            recorded_at=time.time(),
            **kwargs,
        )
```

## Solution 2: Time-Bucketed Latency Store

```python
import json
import time
from collections import defaultdict
from pathlib import Path
from threading import Lock
from typing import Dict, List, Optional, Tuple


class TimeBucketedLatencyStore:
    """
    Accumulates latency observations in fixed-width time buckets.
    Buckets older than retention_seconds are evicted. The store
    can be serialized to a JSON file for persistence across restarts.
    """

    def __init__(
        self,
        bucket_width_seconds: float = 60.0,
        retention_seconds: float = 86400.0,
        persist_path: Optional[str] = None,
    ):
        self._bucket_width = bucket_width_seconds
        self._retention = retention_seconds
        self._path = Path(persist_path) if persist_path else None
        self._lock = Lock()
        # {tool_name: {bucket_ts: [latency_ms, ...]}}
        self._buckets: Dict[str, Dict[float, List[float]]] = defaultdict(lambda: defaultdict(list))
        if self._path and self._path.exists():
            self._load()

    def _bucket_ts(self, ts: float) -> float:
        return (ts // self._bucket_width) * self._bucket_width

    def record(self, obs: LatencyObservation) -> None:
        bucket = self._bucket_ts(obs.recorded_at)
        with self._lock:
            self._buckets[obs.tool_name][bucket].append(obs.latency_ms)
            self._evict()

    def _evict(self) -> None:
        cutoff = self._bucket_ts(time.time() - self._retention)
        for tool in list(self._buckets):
            for bucket in list(self._buckets[tool]):
                if bucket < cutoff:
                    del self._buckets[tool][bucket]

    def observations(self, tool_name: str, window_seconds: float) -> List[float]:
        cutoff = time.time() - window_seconds
        with self._lock:
            result = []
            for bucket_ts, values in self._buckets.get(tool_name, {}).items():
                if bucket_ts >= self._bucket_ts(cutoff):
                    result.extend(values)
            return result

    def all_tool_names(self) -> List[str]:
        with self._lock:
            return list(self._buckets.keys())

    def persist(self) -> None:
        if not self._path:
            return
        with self._lock:
            data = {
                tool: {str(ts): vals for ts, vals in buckets.items()}
                for tool, buckets in self._buckets.items()
            }
        self._path.write_text(json.dumps(data))

    def _load(self) -> None:
        try:
            data = json.loads(self._path.read_text())
            for tool, buckets in data.items():
                for ts_str, vals in buckets.items():
                    self._buckets[tool][float(ts_str)].extend(vals)
            self._evict()
        except (json.JSONDecodeError, OSError):
            pass
```

## Solution 3: Percentile Calculator

```python
from typing import List, Optional


class PercentileCalculator:
    """
    Computes percentiles from a list of latency observations.
    Uses nearest-rank method for correctness with small populations.
    """

    @staticmethod
    def percentile(values: List[float], pct: float) -> Optional[float]:
        if not values:
            return None
        sorted_vals = sorted(values)
        idx = min(int(len(sorted_vals) * pct / 100.0), len(sorted_vals) - 1)
        return round(sorted_vals[idx], 2)

    @staticmethod
    def summary(values: List[float]) -> dict:
        if not values:
            return {"count": 0}
        calc = PercentileCalculator
        return {
            "count": len(values),
            "p50_ms": calc.percentile(values, 50),
            "p75_ms": calc.percentile(values, 75),
            "p95_ms": calc.percentile(values, 95),
            "p99_ms": calc.percentile(values, 99),
            "mean_ms": round(sum(values) / len(values), 2),
            "max_ms": round(max(values), 2),
            "min_ms": round(min(values), 2),
        }
```

## Solution 4: Cross-Session Latency Tracker

```python
import asyncio
import time
from typing import Any, Callable, Dict, Optional


class CrossSessionLatencyTracker:
    """
    Instruments tool calls and records observations into the shared
    time-bucketed store. Provides percentile queries across all sessions.
    """

    def __init__(
        self,
        store: TimeBucketedLatencyStore,
        calculator: PercentileCalculator,
    ):
        self._store = store
        self._calc = calculator
        self._total_recorded = 0

    async def track(
        self,
        tool_name: str,
        tool_fn: Callable,
        *args: Any,
        session_id: str = "",
        **kwargs: Any,
    ) -> Any:
        start = time.time()
        error_type: Optional[str] = None
        try:
            result = await tool_fn(*args, **kwargs)
            return result
        except Exception as exc:
            error_type = type(exc).__name__
            raise
        finally:
            latency_ms = round((time.time() - start) * 1000, 2)
            obs = LatencyObservation.now(
                tool_name=tool_name,
                latency_ms=latency_ms,
                session_id=session_id,
                success=error_type is None,
                error_type=error_type,
            )
            self._store.record(obs)
            self._total_recorded += 1

    def percentiles(self, tool_name: str, window_seconds: float = 3600.0) -> dict:
        values = self._store.observations(tool_name, window_seconds)
        return {
            "tool_name": tool_name,
            "window_seconds": window_seconds,
            **self._calc.summary(values),
        }

    def all_tools_summary(self, window_seconds: float = 3600.0) -> Dict[str, dict]:
        return {
            tool: self.percentiles(tool, window_seconds)
            for tool in self._store.all_tool_names()
        }
```

## Solution 5: SLO Compliance Checker

```python
import time
from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass
class LatencySLO:
    tool_name: str
    percentile: float     # e.g. 95.0 for P95
    threshold_ms: float   # SLO target
    window_seconds: float = 3600.0


class LatencySLOComplianceChecker:
    """
    Evaluates whether each tool meets its defined P-latency SLOs.
    Reports compliance status and breach magnitude.
    """

    def __init__(
        self,
        tracker: CrossSessionLatencyTracker,
        slos: List[LatencySLO],
    ):
        self._tracker = tracker
        self._slos = slos

    def check(self) -> List[dict]:
        results = []
        for slo in self._slos:
            values = self._tracker._store.observations(slo.tool_name, slo.window_seconds)
            actual = PercentileCalculator.percentile(values, slo.percentile)
            if actual is None:
                results.append({
                    "tool_name": slo.tool_name,
                    "status": "no_data",
                    "slo_percentile": slo.percentile,
                    "threshold_ms": slo.threshold_ms,
                })
                continue
            compliant = actual <= slo.threshold_ms
            results.append({
                "tool_name": slo.tool_name,
                "status": "ok" if compliant else "breach",
                "slo_percentile": slo.percentile,
                "threshold_ms": slo.threshold_ms,
                "actual_ms": actual,
                "breach_ms": round(actual - slo.threshold_ms, 2) if not compliant else 0.0,
                "sample_count": len(values),
            })
        return results
```

## Solution 6: Cross-Session Latency Dashboard

```python
import time


class CrossSessionLatencyDashboard:
    """
    Combines percentile summaries across all tools, SLO compliance
    status, and store health into a single operational report.
    """

    def __init__(
        self,
        tracker: CrossSessionLatencyTracker,
        slo_checker: LatencySLOComplianceChecker,
        store: TimeBucketedLatencyStore,
    ):
        self._tracker = tracker
        self._slo_checker = slo_checker
        self._store = store

    def render(self, window_seconds: float = 3600.0) -> dict:
        slo_results = self._slo_checker.check()
        breaches = [r for r in slo_results if r.get("status") == "breach"]
        return {
            "generated_at": time.time(),
            "window_seconds": window_seconds,
            "per_tool_percentiles": self._tracker.all_tools_summary(window_seconds),
            "slo_compliance": {
                "total_slos": len(slo_results),
                "breaches": len(breaches),
                "details": slo_results,
            },
            "store_health": {
                "tracked_tools": len(self._store.all_tool_names()),
                "total_recorded": self._tracker._total_recorded,
            },
        }
```

## Comparison

| Approach | Time-Bucketed Storage | Percentile Query | Cross-Restart Persistence | SLO Compliance | Dashboard |
|---|---|---|---|---|---|
| TimeBucketedLatencyStore | Yes | No | Yes (JSON) | No | No |
| PercentileCalculator | No | Yes (P50/P75/P95/P99) | No | No | No |
| CrossSessionLatencyTracker | Via store | Via calculator | Via store | No | No |
| LatencySLOComplianceChecker | No | Via tracker | No | Yes | No |
| CrossSessionLatencyDashboard | No | No | No | No | Yes |

**Best for production**: Set `bucket_width_seconds=60` and `retention_seconds=86400` to retain 24 hours of one-minute buckets — this gives accurate P95/P99 over any rolling window from 1 minute to 24 hours. Call `store.persist()` on a 60-second interval so a crash loses at most one minute of observations. Define SLOs per tool based on user-facing impact: internal enrichment tools can tolerate P95 < 2000 ms, while tools on the critical rendering path should be P95 < 200 ms. Alert when `LatencySLOComplianceChecker` reports a breach that persists for two consecutive check intervals — single-point spikes are noise; sustained breaches are incidents.
