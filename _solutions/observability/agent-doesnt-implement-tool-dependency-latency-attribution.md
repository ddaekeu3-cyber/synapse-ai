---
title: "Agent Doesn't Implement Tool Dependency Latency Attribution"
description: "Agents that report only total request latency cannot identify which tool dependency is responsible for slow responses — a P99 of 8 seconds could be caused by a slow embedding call, a database lookup, or an external API, but without per-dependency latency attribution, root cause analysis is guesswork. Implement tool dependency latency attribution that measures, labels, and aggregates latency per named dependency, computing the contribution of each to overall request latency."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-tool-dependency-latency-attribution
tags: [latency-attribution, dependency-latency, tool-performance, root-cause, p99-breakdown, service-dependencies]
symptoms:
  - "P99 request latency is 8s but no breakdown by which dependency is slow"
  - "Cannot determine whether high latency is caused by the LLM call or an external API"
  - "All tool calls reported under a single 'tool_execution' metric with no name label"
  - "Slow database query hidden inside an aggregate latency measurement"
  - "On-call engineer must add temporary logging to identify which dependency is degraded"
---

## Why This Happens

Request latency is a sum of its parts. Without labeling each part, a global P99 spike is unactionable — the on-call engineer must either guess or add ad-hoc instrumentation. Tool dependency latency attribution requires naming every latency-contributing operation, measuring it with a timer, and grouping measurements by dependency name so that per-dependency percentiles can be computed. When a dependency's P99 diverges from its baseline, that dependency is the root cause of the overall latency regression — and you know it immediately without any additional investigation.

## Solution 1: Dependency Latency Sample

```python
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class DependencyLatencySample:
    dependency_name: str      # e.g. "anthropic_llm", "pinecone_search", "postgres_read"
    operation: str            # e.g. "embed", "query", "insert"
    latency_ms: float
    success: bool
    error_type: Optional[str] = None
    request_id: str = ""
    sampled_at: float = field(default_factory=time.time)
    metadata: dict = field(default_factory=dict)
```

## Solution 2: Dependency Latency Store

```python
import time
from collections import defaultdict, deque
from threading import Lock
from typing import Deque, Dict, List, Optional, Tuple


class DependencyLatencyStore:
    """
    Accumulates latency samples per dependency name and operation.
    Supports sliding-window percentile queries for SLO tracking.
    """

    def __init__(
        self,
        max_samples_per_key: int = 5000,
        window_seconds: float = 3600.0,
    ):
        self._max = max_samples_per_key
        self._window = window_seconds
        # key: (dependency_name, operation) -> deque of (timestamp, latency_ms, success)
        self._data: Dict[Tuple[str, str], Deque] = defaultdict(deque)
        self._lock = Lock()

    def record(self, sample: DependencyLatencySample) -> None:
        key = (sample.dependency_name, sample.operation)
        with self._lock:
            bucket = self._data[key]
            bucket.append((sample.sampled_at, sample.latency_ms, sample.success))
            if len(bucket) > self._max:
                bucket.popleft()

    def percentile(
        self,
        dependency_name: str,
        operation: str,
        pct: float,
        window_seconds: Optional[float] = None,
    ) -> Optional[float]:
        window = window_seconds or self._window
        cutoff = time.time() - window
        key = (dependency_name, operation)
        with self._lock:
            bucket = self._data.get(key, deque())
            values = sorted(v for ts, v, _ in bucket if ts >= cutoff)
        if not values:
            return None
        idx = min(int(len(values) * pct / 100.0), len(values) - 1)
        return round(values[idx], 2)

    def dependency_summary(
        self,
        dependency_name: str,
        window_seconds: Optional[float] = None,
    ) -> dict:
        window = window_seconds or self._window
        cutoff = time.time() - window
        summary = {}
        with self._lock:
            for (dep, op), bucket in self._data.items():
                if dep != dependency_name:
                    continue
                recent = [(ts, v, s) for ts, v, s in bucket if ts >= cutoff]
                if not recent:
                    continue
                values = sorted(v for _, v, _ in recent)
                error_count = sum(1 for _, _, s in recent if not s)
                summary[op] = {
                    "sample_count": len(recent),
                    "p50_ms": self._pct(values, 50),
                    "p95_ms": self._pct(values, 95),
                    "p99_ms": self._pct(values, 99),
                    "mean_ms": round(sum(values) / len(values), 2),
                    "error_rate": round(error_count / len(recent), 4),
                }
        return summary

    def all_dependencies(self) -> List[str]:
        with self._lock:
            return list({dep for dep, _ in self._data.keys()})

    @staticmethod
    def _pct(sorted_vals: List[float], pct: float) -> float:
        if not sorted_vals:
            return 0.0
        idx = min(int(len(sorted_vals) * pct / 100.0), len(sorted_vals) - 1)
        return round(sorted_vals[idx], 2)
```

## Solution 3: Dependency Timer Context Manager

```python
import asyncio
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional


class DependencyLatencyTimer:
    """
    Async context manager that measures operation latency and records
    it to the dependency latency store automatically.
    """

    def __init__(self, store: DependencyLatencyStore):
        self._store = store

    @asynccontextmanager
    async def measure(
        self,
        dependency_name: str,
        operation: str,
        request_id: str = "",
    ) -> AsyncIterator[dict]:
        context = {"success": True, "error_type": None}
        start = time.time()
        try:
            yield context
        except Exception as exc:
            context["success"] = False
            context["error_type"] = type(exc).__name__
            raise
        finally:
            latency_ms = (time.time() - start) * 1000
            sample = DependencyLatencySample(
                dependency_name=dependency_name,
                operation=operation,
                latency_ms=round(latency_ms, 2),
                success=context["success"],
                error_type=context.get("error_type"),
                request_id=request_id,
            )
            self._store.record(sample)
```

## Solution 4: Request Latency Decomposer

```python
import time
from typing import Dict, List, Optional


class RequestLatencyDecomposer:
    """
    Tracks all dependency spans within a single request and computes
    each dependency's contribution to total request latency.
    """

    def __init__(self):
        self._spans: List[dict] = []
        self._request_start: float = time.time()

    def record_span(
        self,
        dependency_name: str,
        operation: str,
        latency_ms: float,
        success: bool,
    ) -> None:
        self._spans.append({
            "dependency": dependency_name,
            "operation": operation,
            "latency_ms": latency_ms,
            "success": success,
            "recorded_at": time.time(),
        })

    def decompose(self, total_request_latency_ms: Optional[float] = None) -> dict:
        total_tool_ms = sum(s["latency_ms"] for s in self._spans)
        total_request_ms = total_request_latency_ms or (
            (time.time() - self._request_start) * 1000
        )
        overhead_ms = max(total_request_ms - total_tool_ms, 0)

        by_dependency: Dict[str, float] = {}
        for span in self._spans:
            dep = span["dependency"]
            by_dependency[dep] = by_dependency.get(dep, 0) + span["latency_ms"]

        return {
            "total_request_ms": round(total_request_ms, 2),
            "total_tool_ms": round(total_tool_ms, 2),
            "overhead_ms": round(overhead_ms, 2),
            "tool_fraction": round(total_tool_ms / max(total_request_ms, 1), 3),
            "by_dependency": {
                dep: {
                    "total_ms": round(ms, 2),
                    "fraction": round(ms / max(total_request_ms, 1), 3),
                }
                for dep, ms in sorted(by_dependency.items(), key=lambda x: x[1], reverse=True)
            },
            "spans": self._spans,
        }
```

## Solution 5: Dependency SLO Tracker

```python
import time
from typing import Dict, Optional


class DependencySLOTracker:
    """
    Tracks SLO compliance per dependency based on P99 thresholds.
    Reports which dependencies are currently violating their SLOs.
    """

    def __init__(
        self,
        store: DependencyLatencyStore,
        slo_thresholds_ms: Dict[str, float],   # dependency_name -> P99 threshold ms
    ):
        self._store = store
        self._thresholds = slo_thresholds_ms

    def evaluate(self, window_seconds: float = 3600.0) -> dict:
        results = {}
        for dep_name, threshold_ms in self._thresholds.items():
            summary = self._store.dependency_summary(dep_name, window_seconds)
            if not summary:
                results[dep_name] = {"status": "no_data", "threshold_ms": threshold_ms}
                continue

            # Aggregate P99 across operations
            p99_values = [op.get("p99_ms", 0) for op in summary.values()]
            max_p99 = max(p99_values) if p99_values else 0

            results[dep_name] = {
                "status": "violation" if max_p99 > threshold_ms else "ok",
                "max_p99_ms": max_p99,
                "threshold_ms": threshold_ms,
                "operations": summary,
            }

        violating = [dep for dep, r in results.items() if r.get("status") == "violation"]
        return {
            "evaluated_at": time.time(),
            "window_seconds": window_seconds,
            "dependencies": results,
            "violating": violating,
            "all_ok": len(violating) == 0,
        }
```

## Solution 6: Dependency Latency Dashboard

```python
import time


class DependencyLatencyDashboard:
    """
    Combines per-dependency summaries and SLO compliance
    into a single operational view.
    """

    def __init__(
        self,
        store: DependencyLatencyStore,
        slo_tracker: DependencySLOTracker,
    ):
        self._store = store
        self._slo = slo_tracker

    def render(self, window_seconds: float = 3600.0) -> dict:
        dependencies = self._store.all_dependencies()
        summaries = {
            dep: self._store.dependency_summary(dep, window_seconds)
            for dep in dependencies
        }
        slo_report = self._slo.evaluate(window_seconds)

        return {
            "generated_at": time.time(),
            "window_seconds": window_seconds,
            "dependency_summaries": summaries,
            "slo_compliance": slo_report,
            "violating_dependencies": slo_report.get("violating", []),
        }
```

## Comparison

| Approach | Per-Dep Measurement | Percentile Queries | Request Decomposition | SLO Tracking | Dashboard |
|---|---|---|---|---|---|
| DependencyLatencyStore | Yes (sliding window) | Yes (P50/P95/P99) | No | No | No |
| DependencyLatencyTimer | Via store | No | No | No | No |
| RequestLatencyDecomposer | Yes (per-request) | No | Yes (fraction) | No | No |
| DependencySLOTracker | Via store | Via store | No | Yes | No |
| DependencyLatencyDashboard | Via store | Via store | No | Via SLO | Yes |

**Best for production**: Name dependencies consistently — use `anthropic_llm`, `openai_embed`, `pinecone_search`, `postgres_read` rather than generic names like `llm` or `db`. This allows the dashboard to immediately identify which vendor or service is degraded. Set SLO thresholds per dependency based on their historical P99 baselines plus a 50% buffer — this distinguishes normal variance from genuine degradation. Use `RequestLatencyDecomposer` to emit a latency breakdown structured log on every request; a dashboard that shows `tool_fraction` trending toward 1.0 means tools are consuming nearly all request time and the LLM inference overhead is negligible — useful for prioritizing where to optimize.
