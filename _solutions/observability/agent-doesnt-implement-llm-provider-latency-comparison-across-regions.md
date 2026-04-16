---
title: "Agent Doesn't Implement LLM Provider Latency Comparison Across Regions"
description: "Agents that route all LLM requests to a single endpoint cannot detect when a regional provider endpoint degrades while another region stays healthy, nor can they demonstrate that routing decisions are latency-optimal. Implement multi-region provider latency tracking that records per-endpoint P50/P95 metrics, detects regional degradation, and surfaces routing recommendations."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-llm-provider-latency-comparison-across-regions
tags: [multi-region, provider-latency, routing-optimization, regional-degradation, llm-endpoints, geographic-routing]
symptoms:
  - "us-east-1 endpoint degrades for 20 minutes but agent keeps routing to it"
  - "No comparison of latency across eu-west-1, us-east-1, and ap-southeast-1 endpoints"
  - "Cannot demonstrate which region provides lowest P95 latency for a given time of day"
  - "Regional provider incidents go undetected until users in that geography report slowness"
  - "Routing is hardcoded to one region with no dynamic optimization capability"
---

## Why This Happens

LLM provider APIs are deployed across multiple regions, each with independent availability and latency characteristics. Diurnal traffic patterns cause regional load to vary: us-east-1 may be fastest in early UTC hours when US traffic is low, while eu-west-1 leads in EU business hours. Without measuring per-endpoint latency independently, routing decisions cannot be data-driven. Latency comparison requires tagging each LLM call with its endpoint region, maintaining per-region sliding windows, and periodically computing which endpoint currently offers the best P95 latency.

## Solution 1: Provider Endpoint Record

```python
import os
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ProviderEndpointRecord:
    endpoint_id: str
    region: str
    provider: str           # "openai" | "anthropic" | "azure" | etc.
    base_url: str
    model: str
    priority: int = 5       # lower = preferred when latencies are equal
    enabled: bool = True
    metadata: dict = field(default_factory=dict)


@dataclass
class LLMCallSample:
    endpoint_id: str
    region: str
    provider: str
    model: str
    latency_ms: float
    success: bool
    tokens_used: Optional[int] = None
    error_type: Optional[str] = None
    recorded_at: float = field(default_factory=time.time)
```

## Solution 2: Per-Region Latency Tracker

```python
import time
from collections import deque
from threading import Lock
from typing import Deque, Dict, List, Optional, Tuple


class PerRegionLatencyTracker:
    """
    Maintains a sliding window of latency samples per endpoint region.
    Provides P50/P95/P99 percentiles and success rates per endpoint.
    """

    def __init__(self, window_seconds: float = 300.0, max_samples: int = 2000):
        self._window = window_seconds
        self._max = max_samples
        self._samples: Dict[str, Deque[LLMCallSample]] = {}
        self._lock = Lock()

    def record(self, sample: LLMCallSample) -> None:
        with self._lock:
            if sample.endpoint_id not in self._samples:
                self._samples[sample.endpoint_id] = deque()
            self._samples[sample.endpoint_id].append(sample)
            if len(self._samples[sample.endpoint_id]) > self._max:
                self._samples[sample.endpoint_id].popleft()

    def percentile(
        self,
        endpoint_id: str,
        pct: float,
        window_seconds: Optional[float] = None,
    ) -> Optional[float]:
        ws = window_seconds or self._window
        cutoff = time.time() - ws
        with self._lock:
            samples = [
                s.latency_ms for s in self._samples.get(endpoint_id, [])
                if s.recorded_at >= cutoff and s.success
            ]
        if not samples:
            return None
        samples.sort()
        idx = min(int(len(samples) * pct / 100.0), len(samples) - 1)
        return round(samples[idx], 2)

    def success_rate(
        self,
        endpoint_id: str,
        window_seconds: Optional[float] = None,
    ) -> Optional[float]:
        ws = window_seconds or self._window
        cutoff = time.time() - ws
        with self._lock:
            samples = [
                s for s in self._samples.get(endpoint_id, [])
                if s.recorded_at >= cutoff
            ]
        if not samples:
            return None
        return round(sum(1 for s in samples if s.success) / len(samples), 4)

    def endpoint_summary(
        self,
        endpoint_id: str,
        window_seconds: Optional[float] = None,
    ) -> dict:
        ws = window_seconds or self._window
        cutoff = time.time() - ws
        with self._lock:
            samples = [s for s in self._samples.get(endpoint_id, []) if s.recorded_at >= cutoff]
        if not samples:
            return {"endpoint_id": endpoint_id, "samples": 0}
        latencies = sorted(s.latency_ms for s in samples if s.success)
        return {
            "endpoint_id": endpoint_id,
            "region": samples[-1].region,
            "samples": len(samples),
            "success_rate": round(sum(1 for s in samples if s.success) / len(samples), 4),
            "p50_ms": latencies[len(latencies) // 2] if latencies else None,
            "p95_ms": latencies[min(int(len(latencies) * 0.95), len(latencies) - 1)] if latencies else None,
            "p99_ms": latencies[min(int(len(latencies) * 0.99), len(latencies) - 1)] if latencies else None,
        }

    def all_endpoints(self) -> List[str]:
        with self._lock:
            return list(self._samples.keys())
```

## Solution 3: Regional Degradation Detector

```python
from typing import List, Optional


class RegionalDegradationDetector:
    """
    Compares recent P95 latency for each endpoint against its baseline.
    Declares a region degraded when recent P95 exceeds baseline by
    the configured threshold percentage.
    """

    def __init__(
        self,
        tracker: PerRegionLatencyTracker,
        degradation_threshold_pct: float = 50.0,
        min_samples: int = 10,
        baseline_window_seconds: float = 3600.0,
        recent_window_seconds: float = 300.0,
    ):
        self._tracker = tracker
        self._threshold = degradation_threshold_pct / 100.0
        self._min_samples = min_samples
        self._baseline_window = baseline_window_seconds
        self._recent_window = recent_window_seconds

    def detect(self, endpoint_id: str) -> dict:
        baseline = self._tracker.percentile(endpoint_id, 95, self._baseline_window)
        recent = self._tracker.percentile(endpoint_id, 95, self._recent_window)
        success = self._tracker.success_rate(endpoint_id, self._recent_window)

        if baseline is None or recent is None:
            return {
                "endpoint_id": endpoint_id,
                "status": "insufficient_data",
            }

        change = (recent - baseline) / max(baseline, 1)
        degraded = change > self._threshold or (success is not None and success < 0.90)

        return {
            "endpoint_id": endpoint_id,
            "status": "degraded" if degraded else "healthy",
            "baseline_p95_ms": baseline,
            "recent_p95_ms": recent,
            "change_pct": round(change * 100, 1),
            "recent_success_rate": success,
        }

    def scan_all(self) -> List[dict]:
        return [
            self.detect(eid)
            for eid in self._tracker.all_endpoints()
        ]
```

## Solution 4: Routing Recommendation Engine

```python
from typing import List, Optional


class RoutingRecommendationEngine:
    """
    Selects the best endpoint based on current P95 latency and
    success rate, filtering out degraded regions.
    """

    def __init__(
        self,
        tracker: PerRegionLatencyTracker,
        detector: RegionalDegradationDetector,
        endpoints: List[ProviderEndpointRecord],
    ):
        self._tracker = tracker
        self._detector = detector
        self._endpoints = {e.endpoint_id: e for e in endpoints}

    def recommend(
        self,
        exclude_degraded: bool = True,
        window_seconds: float = 300.0,
    ) -> Optional[dict]:
        candidates = []
        for eid, endpoint in self._endpoints.items():
            if not endpoint.enabled:
                continue
            if exclude_degraded:
                status = self._detector.detect(eid)
                if status.get("status") == "degraded":
                    continue
            p95 = self._tracker.percentile(eid, 95, window_seconds)
            sr = self._tracker.success_rate(eid, window_seconds)
            if p95 is not None and sr is not None:
                candidates.append({
                    "endpoint_id": eid,
                    "region": endpoint.region,
                    "p95_ms": p95,
                    "success_rate": sr,
                    "priority": endpoint.priority,
                })

        if not candidates:
            return None

        # Sort by P95 ascending, then priority ascending
        best = sorted(candidates, key=lambda c: (c["p95_ms"], c["priority"]))[0]
        return best
```

## Solution 5: Diurnal Pattern Recorder

```python
import time
from collections import defaultdict
from typing import Dict, List


class DiurnalPatternRecorder:
    """
    Buckets latency observations by hour-of-day to reveal whether
    a region is systematically slower during certain UTC hours.
    """

    def __init__(self):
        self._hourly: Dict[str, Dict[int, List[float]]] = defaultdict(lambda: defaultdict(list))

    def record(self, sample: LLMCallSample) -> None:
        if not sample.success:
            return
        hour = int(time.gmtime(sample.recorded_at).tm_hour)
        self._hourly[sample.endpoint_id][hour].append(sample.latency_ms)

    def hourly_p50(self, endpoint_id: str) -> Dict[int, Optional[float]]:
        result = {}
        for hour in range(24):
            vals = sorted(self._hourly.get(endpoint_id, {}).get(hour, []))
            result[hour] = vals[len(vals) // 2] if vals else None
        return result

    def best_hours(self, endpoint_id: str, top_n: int = 6) -> List[dict]:
        hourly = self.hourly_p50(endpoint_id)
        ranked = [
            {"hour_utc": h, "p50_ms": ms}
            for h, ms in hourly.items()
            if ms is not None
        ]
        return sorted(ranked, key=lambda x: x["p50_ms"])[:top_n]
```

## Solution 6: Multi-Region Provider Dashboard

```python
import time


class MultiRegionProviderLatencyDashboard:
    """
    Combines per-endpoint summaries, degradation status, routing
    recommendation, and diurnal patterns into a provider health report.
    """

    def __init__(
        self,
        tracker: PerRegionLatencyTracker,
        detector: RegionalDegradationDetector,
        router: RoutingRecommendationEngine,
        diurnal: DiurnalPatternRecorder,
    ):
        self._tracker = tracker
        self._detector = detector
        self._router = router
        self._diurnal = diurnal

    def render(self) -> dict:
        endpoints = self._tracker.all_endpoints()
        return {
            "generated_at": time.time(),
            "endpoint_summaries": {
                eid: self._tracker.endpoint_summary(eid)
                for eid in endpoints
            },
            "degradation_status": self._detector.scan_all(),
            "recommended_endpoint": self._router.recommend(),
            "diurnal_best_hours": {
                eid: self._diurnal.best_hours(eid)
                for eid in endpoints
            },
        }
```

## Comparison

| Approach | Per-Region Tracking | Degradation Detection | Routing Recommendation | Diurnal Patterns | Dashboard |
|---|---|---|---|---|---|
| PerRegionLatencyTracker | Yes (P50/P95/P99) | No | No | No | No |
| RegionalDegradationDetector | Via tracker | Yes (threshold) | No | No | No |
| RoutingRecommendationEngine | Via tracker | Via detector | Yes (best P95) | No | No |
| DiurnalPatternRecorder | No | No | No | Yes (hourly) | No |
| MultiRegionProviderLatencyDashboard | No | No | No | No | Yes |

**Best for production**: Use a `recent_window_seconds=300` (5 min) for degradation detection — 5 minutes is enough to confirm a regional incident without false-positiving on a single slow request. Tag every LLM metric with `endpoint_id` and `region` so dashboards can show per-region latency heatmaps over time. Run `RoutingRecommendationEngine.recommend()` every 60 seconds and update the active endpoint — this provides dynamic routing without constantly re-evaluating on every request. Use `DiurnalPatternRecorder` to pre-configure region priority by time of day rather than relying on runtime detection: if eu-west-1 is consistently best from 06:00–14:00 UTC, set it as priority 1 during that window.
