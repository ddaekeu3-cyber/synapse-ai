---
title: "Agent Doesn't Implement Multi-Region Latency Comparison"
description: "Agents deployed across multiple regions with no cross-region latency comparison cannot determine whether high latency in one region is a regional infrastructure problem or a global application regression. Implement multi-region latency comparison that collects per-region latency samples, computes regional percentiles, detects outlier regions using inter-region Z-score comparison, and surfaces the comparison in a unified dashboard."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-multi-region-latency-comparison
tags: [multi-region, latency-comparison, regional-observability, p99-latency, outlier-detection, geo-distribution]
symptoms:
  - "P99 spike reported in us-west-2 — unclear whether it is regional or global"
  - "No way to compare ap-southeast-1 latency against eu-west-1 to isolate the cause"
  - "Regional deployment differences invisible — all metrics aggregated into a single global histogram"
  - "On-call engineer cannot determine whether to roll back globally or only in one region"
  - "Cross-region latency divergence detected only after user complaints, not proactively"
---

## Why This Happens

Global latency aggregation hides regional anomalies. When latency samples from all regions are merged into a single histogram, a degraded region (e.g., us-west-2 at P99=8s) is masked by healthy regions (e.g., eu-west-1 at P99=400ms), producing a global P99 that looks alarming but not catastrophic. Per-region percentile tracking with cross-region comparison exposes the outlier immediately: if one region's P99 is more than two standard deviations above the inter-regional mean, it is the source of the problem. This requires each agent instance to tag its latency samples with a region identifier and report them to a shared collector.

## Solution 1: Regional Latency Sample

```python
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class RegionalLatencySample:
    region: str
    latency_ms: float
    operation: str          # e.g. "llm_call", "tool_execution", "embedding"
    sampled_at: float = field(default_factory=time.time)
    instance_id: str = ""
    success: bool = True
    error_type: Optional[str] = None
```

## Solution 2: Per-Region Latency Store

```python
import math
import time
from collections import defaultdict, deque
from threading import Lock
from typing import Deque, Dict, List, Optional, Tuple


class PerRegionLatencyStore:
    """
    Collects latency samples per region and operation.
    Supports sliding-window percentile queries per region.
    """

    def __init__(
        self,
        max_samples_per_region: int = 5000,
        window_seconds: float = 3600.0,
    ):
        self._max = max_samples_per_region
        self._window = window_seconds
        self._samples: Dict[str, Dict[str, Deque[Tuple[float, float]]]] = defaultdict(
            lambda: defaultdict(deque)
        )
        self._lock = Lock()

    def record(self, sample: RegionalLatencySample) -> None:
        with self._lock:
            bucket = self._samples[sample.region][sample.operation]
            bucket.append((sample.sampled_at, sample.latency_ms))
            if len(bucket) > self._max:
                bucket.popleft()

    def percentile(
        self,
        region: str,
        operation: str,
        pct: float,
        window_seconds: Optional[float] = None,
    ) -> Optional[float]:
        window = window_seconds or self._window
        cutoff = time.time() - window
        with self._lock:
            bucket = self._samples.get(region, {}).get(operation, deque())
            values = sorted(v for ts, v in bucket if ts >= cutoff)
        if not values:
            return None
        idx = min(int(len(values) * pct / 100.0), len(values) - 1)
        return round(values[idx], 2)

    def region_summary(
        self,
        operation: str,
        window_seconds: Optional[float] = None,
    ) -> Dict[str, dict]:
        window = window_seconds or self._window
        cutoff = time.time() - window
        result = {}
        with self._lock:
            for region, ops in self._samples.items():
                bucket = ops.get(operation, deque())
                values = sorted(v for ts, v in bucket if ts >= cutoff)
                if not values:
                    continue
                mean = sum(values) / len(values)
                result[region] = {
                    "sample_count": len(values),
                    "mean_ms": round(mean, 2),
                    "p50_ms": self._pct(values, 50),
                    "p95_ms": self._pct(values, 95),
                    "p99_ms": self._pct(values, 99),
                }
        return result

    @staticmethod
    def _pct(sorted_values: List[float], pct: float) -> float:
        if not sorted_values:
            return 0.0
        idx = min(int(len(sorted_values) * pct / 100.0), len(sorted_values) - 1)
        return round(sorted_values[idx], 2)

    def known_regions(self) -> List[str]:
        with self._lock:
            return list(self._samples.keys())
```

## Solution 3: Cross-Region Outlier Detector

```python
import math
from typing import Dict, List, Optional


class CrossRegionOutlierDetector:
    """
    Identifies outlier regions by comparing each region's P99 against
    the inter-regional mean and standard deviation using Z-scores.
    """

    def __init__(self, z_threshold: float = 2.0):
        self._z_threshold = z_threshold

    def detect(
        self,
        region_summaries: Dict[str, dict],
        metric: str = "p99_ms",
    ) -> List[dict]:
        values = {
            region: data[metric]
            for region, data in region_summaries.items()
            if metric in data and data[metric] > 0
        }
        if len(values) < 2:
            return []

        vals = list(values.values())
        mean = sum(vals) / len(vals)
        variance = sum((v - mean) ** 2 for v in vals) / max(len(vals) - 1, 1)
        std = math.sqrt(variance)

        outliers = []
        for region, value in values.items():
            z = (value - mean) / std if std > 0 else 0.0
            if abs(z) >= self._z_threshold:
                outliers.append({
                    "region": region,
                    metric: value,
                    "inter_region_mean": round(mean, 2),
                    "z_score": round(z, 3),
                    "direction": "high" if z > 0 else "low",
                    "is_outlier": True,
                })
        return sorted(outliers, key=lambda x: abs(x["z_score"]), reverse=True)
```

## Solution 4: Regional Latency Alert Manager

```python
import time
from typing import Dict, List, Optional


class RegionalLatencyAlertManager:
    """
    Fires alerts when a region is detected as a latency outlier.
    Suppresses repeat alerts within a cooldown window.
    """

    def __init__(
        self,
        cooldown_seconds: float = 300.0,
        absolute_p99_threshold_ms: Optional[float] = None,
    ):
        self._cooldown = cooldown_seconds
        self._absolute_threshold = absolute_p99_threshold_ms
        self._last_alerted: Dict[str, float] = {}
        self._alert_count = 0

    def evaluate(
        self,
        outliers: List[dict],
        region_summaries: Dict[str, dict],
    ) -> List[dict]:
        now = time.time()
        alerts = []

        for outlier in outliers:
            region = outlier["region"]
            if now - self._last_alerted.get(region, 0) < self._cooldown:
                continue
            self._last_alerted[region] = now
            self._alert_count += 1
            alerts.append({
                "alert_type": "regional_latency_outlier",
                "region": region,
                "p99_ms": outlier.get("p99_ms"),
                "z_score": outlier.get("z_score"),
                "direction": outlier.get("direction"),
                "inter_region_mean_ms": outlier.get("inter_region_mean"),
                "ts": now,
            })

        # Absolute threshold check
        if self._absolute_threshold:
            for region, data in region_summaries.items():
                p99 = data.get("p99_ms", 0)
                if p99 >= self._absolute_threshold:
                    if now - self._last_alerted.get(f"{region}:abs", 0) >= self._cooldown:
                        self._last_alerted[f"{region}:abs"] = now
                        self._alert_count += 1
                        alerts.append({
                            "alert_type": "regional_p99_absolute_threshold",
                            "region": region,
                            "p99_ms": p99,
                            "threshold_ms": self._absolute_threshold,
                            "ts": now,
                        })
        return alerts
```

## Solution 5: Multi-Region Latency Collector

```python
import os
import time
from typing import Any, Callable


class MultiRegionLatencyCollector:
    """
    Wraps agent operations with per-region latency recording.
    Automatically tags samples with the region from environment.
    """

    def __init__(
        self,
        store: PerRegionLatencyStore,
        region: str = "",
        instance_id: str = "",
    ):
        self._store = store
        self._region = region or os.getenv("AWS_REGION", os.getenv("REGION", "unknown"))
        self._instance_id = instance_id or os.getenv("INSTANCE_ID", "")

    async def measure(
        self,
        operation: str,
        fn: Callable,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        start = time.time()
        error_type = None
        try:
            result = await fn(*args, **kwargs)
            return result
        except Exception as exc:
            error_type = type(exc).__name__
            raise
        finally:
            latency_ms = (time.time() - start) * 1000
            sample = RegionalLatencySample(
                region=self._region,
                latency_ms=round(latency_ms, 2),
                operation=operation,
                instance_id=self._instance_id,
                success=error_type is None,
                error_type=error_type,
            )
            self._store.record(sample)
```

## Solution 6: Multi-Region Latency Dashboard

```python
import time
from typing import List, Optional


class MultiRegionLatencyDashboard:
    """
    Combines per-region summaries, outlier detection, and alert history
    into a single report for on-call visibility.
    """

    def __init__(
        self,
        store: PerRegionLatencyStore,
        outlier_detector: CrossRegionOutlierDetector,
        alert_manager: RegionalLatencyAlertManager,
        tracked_operations: Optional[List[str]] = None,
    ):
        self._store = store
        self._detector = outlier_detector
        self._alert_manager = alert_manager
        self._operations = tracked_operations or ["llm_call", "tool_execution", "embedding"]

    def render(self, window_seconds: float = 3600.0) -> dict:
        report = {
            "generated_at": time.time(),
            "window_seconds": window_seconds,
            "operations": {},
        }

        for operation in self._operations:
            summaries = self._store.region_summary(operation, window_seconds)
            outliers = self._detector.detect(summaries, metric="p99_ms")
            alerts = self._alert_manager.evaluate(outliers, summaries)

            report["operations"][operation] = {
                "regions": summaries,
                "outlier_regions": [o["region"] for o in outliers],
                "alerts": alerts,
            }

        report["known_regions"] = self._store.known_regions()
        report["total_alerts_fired"] = self._alert_manager._alert_count
        return report
```

## Comparison

| Approach | Per-Region Storage | Percentile Queries | Outlier Detection | Alerting | Full Dashboard |
|---|---|---|---|---|---|
| PerRegionLatencyStore | Yes (sliding window) | Yes (P50/P95/P99) | No | No | No |
| CrossRegionOutlierDetector | No | No | Yes (Z-score) | No | No |
| RegionalLatencyAlertManager | No | No | No | Yes (cooldown) | No |
| MultiRegionLatencyCollector | Via store | No | No | No | No |
| MultiRegionLatencyDashboard | Via store | Via store | Via detector | Via alert manager | Yes |

**Best for production**: Tag every latency sample with region from `AWS_REGION` or `REGION` environment variable at the collection point — not inferred later. Use a Z-score threshold of 2.0 for outlier detection (flags regions that are statistically unusual) combined with an absolute P99 threshold (e.g., 5000ms) to catch cases where all regions are degraded but one is dramatically worse. Set `cooldown_seconds=300` on alerts to prevent alert storms during prolonged regional incidents. Emit `render()` output as a structured log event every 5 minutes — this gives SREs a time-series of regional divergence that is invaluable for post-incident analysis when the dashboards did not capture the incident in real time.
