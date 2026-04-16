---
title: "Agent Doesn't Implement Error Rate Anomaly Detection Across Tool Categories"
description: "Agents that track only aggregate error rates miss category-level degradation: a search tool failing at 80% while all other tools succeed keeps the overall error rate below alerting thresholds. Implement error rate anomaly detection that tracks error rates per tool category, compares each category against its historical baseline, and fires alerts when any category's error rate spikes above expected variance — regardless of whether the aggregate rate looks healthy."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-error-rate-anomaly-detection-across-tool-categories
tags: [error-rate, anomaly-detection, tool-categories, baseline-comparison, alert, degradation-detection]
symptoms:
  - "Single-tool failures masked by healthy aggregate error rate"
  - "Category-level degradation (all search tools failing) goes undetected for minutes"
  - "No baseline error rate per tool category — cannot distinguish normal from anomalous"
  - "Alerts fire only at 100% failure — partial degradation never surfaces"
  - "On-call engineers discover tool category failures from user complaints, not metrics"
---

## Why This Happens

Aggregate error rate metrics average failures across all tools. If 90% of calls are to healthy tools and 10% are to a failing tool, the aggregate error rate reflects a weighted average that may never cross an alert threshold even if every call to the failing category fails. Category-level tracking exposes this: each tool or category maintains its own error rate history, and anomalies are detected by comparing the current rate against that category's specific baseline rather than a global threshold. This catches the "one bad apple" failure mode that aggregate metrics consistently miss.

## Solution 1: Tool Category Registry

```python
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set


@dataclass
class ToolCategory:
    name: str
    tools: Set[str]
    baseline_error_rate: float = 0.0     # historical normal error rate
    alert_threshold_pct: float = 20.0    # alert when error rate exceeds this
    min_calls_for_alert: int = 10        # don't alert on tiny sample sizes
    description: str = ""


class ToolCategoryRegistry:
    """
    Maps individual tool names to their category.
    Categories group tools with similar failure modes (e.g., all search tools,
    all database tools) so category-level degradation is detectable.
    """

    def __init__(self):
        self._categories: Dict[str, ToolCategory] = {}
        self._tool_to_category: Dict[str, str] = {}

    def register(self, category: ToolCategory) -> None:
        self._categories[category.name] = category
        for tool in category.tools:
            self._tool_to_category[tool] = category.name

    def get_category(self, tool_name: str) -> Optional[ToolCategory]:
        cat_name = self._tool_to_category.get(tool_name)
        if cat_name:
            return self._categories.get(cat_name)
        return None

    def get_category_name(self, tool_name: str) -> str:
        return self._tool_to_category.get(tool_name, "uncategorized")

    def all_categories(self) -> List[ToolCategory]:
        return list(self._categories.values())
```

## Solution 2: Per-Category Error Rate Tracker

```python
import time
from collections import deque
from threading import Lock
from typing import Deque, Dict, Optional, Tuple


class PerCategoryErrorRateTracker:
    """
    Maintains a sliding window of call outcomes per tool category.
    Computes real-time error rates for each category independently.
    """

    def __init__(
        self,
        registry: ToolCategoryRegistry,
        window_seconds: float = 300.0,   # 5-minute sliding window
        max_events_per_category: int = 10000,
    ):
        self._registry = registry
        self._window = window_seconds
        self._events: Dict[str, Deque[Tuple[float, bool]]] = {}
        # category_name -> deque of (timestamp, is_error)
        self._lock = Lock()

    def record(self, tool_name: str, is_error: bool) -> None:
        category_name = self._registry.get_category_name(tool_name)
        now = time.time()
        with self._lock:
            if category_name not in self._events:
                self._events[category_name] = deque(maxlen=10000)
            self._events[category_name].append((now, is_error))

    def error_rate(self, category_name: str) -> Optional[Tuple[float, int]]:
        """Returns (error_rate_pct, total_calls) for the sliding window."""
        cutoff = time.time() - self._window
        with self._lock:
            events = self._events.get(category_name, deque())
            recent = [(ts, err) for ts, err in events if ts >= cutoff]
        if not recent:
            return None
        total = len(recent)
        errors = sum(1 for _, err in recent if err)
        return round(errors / total * 100, 3), total

    def all_rates(self) -> Dict[str, dict]:
        result = {}
        for cat_name in list(self._events.keys()):
            rate_data = self.error_rate(cat_name)
            if rate_data:
                error_rate, total = rate_data
                result[cat_name] = {
                    "error_rate_pct": error_rate,
                    "total_calls": total,
                    "window_seconds": self._window,
                }
        return result
```

## Solution 3: Baseline Comparator

```python
import time
from collections import deque
from threading import Lock
from typing import Deque, Dict, List, Optional, Tuple


class CategoryBaselineComparator:
    """
    Maintains a rolling historical baseline of error rates per category.
    Compares current error rate against baseline to detect anomalies.
    Uses standard deviation to determine anomaly threshold dynamically.
    """

    def __init__(
        self,
        baseline_window_hours: float = 24.0,
        sample_interval_seconds: float = 60.0,
        min_baseline_samples: int = 30,
    ):
        self._baseline_window = baseline_window_hours * 3600
        self._sample_interval = sample_interval_seconds
        self._min_samples = min_baseline_samples
        self._baseline: Dict[str, Deque[Tuple[float, float]]] = {}
        # category -> deque of (timestamp, error_rate_pct)
        self._lock = Lock()

    def record_sample(self, category_name: str, error_rate_pct: float) -> None:
        with self._lock:
            if category_name not in self._baseline:
                self._baseline[category_name] = deque()
            self._baseline[category_name].append((time.time(), error_rate_pct))
            # Evict samples outside the baseline window
            cutoff = time.time() - self._baseline_window
            while (self._baseline[category_name] and
                   self._baseline[category_name][0][0] < cutoff):
                self._baseline[category_name].popleft()

    def is_anomalous(
        self,
        category_name: str,
        current_rate_pct: float,
        z_score_threshold: float = 3.0,
        min_absolute_increase_pct: float = 10.0,
    ) -> Tuple[bool, dict]:
        with self._lock:
            samples = list(self._baseline.get(category_name, []))

        rates = [r for _, r in samples]
        if len(rates) < self._min_samples:
            return False, {"status": "insufficient_baseline", "samples": len(rates)}

        mean = sum(rates) / len(rates)
        variance = sum((r - mean) ** 2 for r in rates) / len(rates)
        stddev = variance ** 0.5

        z_score = (current_rate_pct - mean) / max(stddev, 0.1)
        absolute_increase = current_rate_pct - mean

        anomalous = (
            z_score >= z_score_threshold
            and absolute_increase >= min_absolute_increase_pct
        )

        return anomalous, {
            "status": "anomaly" if anomalous else "normal",
            "current_rate_pct": round(current_rate_pct, 3),
            "baseline_mean_pct": round(mean, 3),
            "baseline_stddev_pct": round(stddev, 3),
            "z_score": round(z_score, 2),
            "absolute_increase_pct": round(absolute_increase, 3),
            "baseline_samples": len(rates),
        }
```

## Solution 4: Category Anomaly Alert Manager

```python
import time
from typing import Callable, Dict, List, Optional, Set


class CategoryAnomalyAlertManager:
    """
    Coordinates per-category anomaly detection and fires alerts
    when a category's error rate exceeds its baseline. Prevents
    alert storms by enforcing a cooldown between repeated alerts.
    """

    def __init__(
        self,
        tracker: PerCategoryErrorRateTracker,
        comparator: CategoryBaselineComparator,
        alert_fn: Optional[Callable[[dict], None]] = None,
        alert_cooldown_seconds: float = 300.0,
    ):
        self._tracker = tracker
        self._comparator = comparator
        self._alert_fn = alert_fn or (lambda a: print(a))
        self._cooldown = alert_cooldown_seconds
        self._last_alert: Dict[str, float] = {}
        self._alert_history: List[dict] = []

    def check_all(self, registry: "ToolCategoryRegistry") -> List[dict]:
        fired = []
        for category in registry.all_categories():
            rate_data = self._tracker.error_rate(category.name)
            if rate_data is None:
                continue

            error_rate_pct, total_calls = rate_data
            if total_calls < category.min_calls_for_alert:
                continue

            # Record baseline sample
            self._comparator.record_sample(category.name, error_rate_pct)

            # Threshold check
            if error_rate_pct < category.alert_threshold_pct:
                continue

            # Anomaly check
            anomalous, detail = self._comparator.is_anomalous(
                category.name, error_rate_pct
            )
            if not anomalous and error_rate_pct < category.alert_threshold_pct * 1.5:
                continue

            # Cooldown check
            last = self._last_alert.get(category.name, 0)
            if time.time() - last < self._cooldown:
                continue

            alert = {
                "ts": time.time(),
                "category": category.name,
                "error_rate_pct": error_rate_pct,
                "total_calls": total_calls,
                "anomaly_detail": detail,
                "severity": "critical" if error_rate_pct >= 50 else "warning",
            }
            self._alert_fn(alert)
            self._alert_history.append(alert)
            self._last_alert[category.name] = time.time()
            fired.append(alert)

        return fired
```

## Solution 5: Cross-Category Correlation Detector

```python
from typing import Dict, List


class CrossCategoryCorrelationDetector:
    """
    Detects when multiple tool categories degrade simultaneously,
    suggesting a shared infrastructure failure (network, auth service,
    cloud region) rather than an isolated tool bug.
    """

    def __init__(
        self,
        tracker: PerCategoryErrorRateTracker,
        simultaneous_threshold_pct: float = 25.0,
    ):
        self._tracker = tracker
        self._threshold = simultaneous_threshold_pct

    def detect_shared_failure(self) -> dict:
        rates = self._tracker.all_rates()
        degraded = {
            cat: data for cat, data in rates.items()
            if data["error_rate_pct"] >= self._threshold
        }

        shared_failure = len(degraded) >= 2

        return {
            "degraded_categories": list(degraded.keys()),
            "degraded_count": len(degraded),
            "shared_infrastructure_failure": shared_failure,
            "recommendation": (
                "Check shared infrastructure (network, auth, cloud region)"
                if shared_failure else "Investigate individual tool category"
            ),
        }
```

## Solution 6: Error Rate Anomaly Dashboard

```python
import time


class ErrorRateAnomalyDashboard:
    """
    Combines per-category error rates, anomaly detection results,
    alert history, and cross-category correlation into a single view.
    """

    def __init__(
        self,
        tracker: PerCategoryErrorRateTracker,
        alert_manager: CategoryAnomalyAlertManager,
        correlation_detector: CrossCategoryCorrelationDetector,
        registry: ToolCategoryRegistry,
    ):
        self._tracker = tracker
        self._alerts = alert_manager
        self._correlation = correlation_detector
        self._registry = registry

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "per_category_rates": self._tracker.all_rates(),
            "correlation": self._correlation.detect_shared_failure(),
            "recent_alerts": self._alerts._alert_history[-10:],
            "alert_count_total": len(self._alerts._alert_history),
        }
```

## Comparison

| Approach | Per-Category Tracking | Baseline Comparison | Z-Score Anomaly | Correlation | Alert Cooldown |
|---|---|---|---|---|---|
| PerCategoryErrorRateTracker | Yes (sliding window) | No | No | No | No |
| CategoryBaselineComparator | No | Yes | Yes | No | No |
| CategoryAnomalyAlertManager | Via tracker | Via comparator | Via comparator | No | Yes |
| CrossCategoryCorrelationDetector | Via tracker | No | No | Yes | No |
| ErrorRateAnomalyDashboard | No | No | No | No | No |

**Best for production**: Define categories that map to failure domains rather than functional domains — group tools that share the same upstream dependency (same API endpoint, same database cluster, same auth service) so that correlated failures appear as a single category spike rather than multiple independent tool failures. Set `z_score_threshold=3.0` combined with `min_absolute_increase_pct=10.0` to avoid alerting on statistically significant but operationally irrelevant increases: a category that normally runs at 0.1% error rate spiking to 0.5% is a 5-sigma event but still operationally fine, while the absolute increase requirement ensures only meaningful degradation fires. Use `CrossCategoryCorrelationDetector` as the first check when an alert fires: simultaneous degradation across search, database, and external API categories almost always indicates a network partition or cloud provider incident rather than application bugs.
