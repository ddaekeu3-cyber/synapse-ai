---
title: "Agent Doesn't Implement Capacity Planning Metrics"
description: "Agents that only monitor current resource utilization have no warning before saturation — memory fills, CPU maxes, queue depth hits zero headroom, and the system degrades without notice. Implement capacity planning metrics that project resource utilization trends, estimate time-to-saturation, and trigger scaling decisions before limits are reached."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-capacity-planning-metrics
tags: [capacity-planning, resource-utilization, forecasting, scaling, observability, trend-analysis]
symptoms:
  - "Memory OOM happens without warning because no trend tracking shows growth rate"
  - "Queue depth hits max before auto-scaling triggers, causing request drops"
  - "No projection of when current token budget will be exhausted at current growth rate"
  - "CPU saturation at peak load is only discovered post-incident, not predicted"
  - "Engineering team manually computes headroom from dashboards during quarterly planning"
---

## Why This Happens

Point-in-time metrics (current CPU: 72%) are operationally useful but strategically blind. Capacity planning requires trend metrics: how fast is this resource growing, and when will it hit its limit at that rate? Linear regression over sliding windows provides time-to-saturation estimates that drive proactive scaling decisions — days before a hard limit is reached, not seconds after. Without this, systems run to failure.

## Solution 1: Resource Trend Tracker

```python
import math
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, Optional, Tuple

@dataclass
class ResourceSample:
    value: float
    capacity: float    # maximum possible value (e.g., 100 for %, total bytes for memory)
    timestamp: float = field(default_factory=time.time)

    @property
    def utilization(self) -> float:
        return self.value / max(self.capacity, 1e-9)

@dataclass
class TrendResult:
    resource_name: str
    current_value: float
    capacity: float
    utilization_pct: float
    slope_per_hour: float          # units per hour (positive = growing)
    r_squared: float               # goodness of fit, 0–1
    estimated_hours_to_saturation: Optional[float]   # None if not trending toward saturation
    headroom_pct: float

class ResourceTrendTracker:
    """
    Tracks resource utilization over time and fits a linear trend
    to estimate time-to-saturation. One tracker instance per resource.
    """

    def __init__(self, window_hours: float = 24.0, sample_interval_minutes: float = 5.0):
        max_samples = int(window_hours * 60 / sample_interval_minutes)
        self._samples: Deque[ResourceSample] = deque(maxlen=max_samples)

    def record(self, value: float, capacity: float) -> None:
        self._samples.append(ResourceSample(value=value, capacity=capacity))

    def _linear_regression(
        self, xs: list, ys: list
    ) -> Tuple[float, float, float]:
        """Returns (slope, intercept, r_squared)."""
        n = len(xs)
        if n < 2:
            return 0.0, ys[0] if ys else 0.0, 0.0
        x_mean = sum(xs) / n
        y_mean = sum(ys) / n
        ss_xy = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys))
        ss_xx = sum((x - x_mean) ** 2 for x in xs)
        if ss_xx < 1e-12:
            return 0.0, y_mean, 0.0
        slope = ss_xy / ss_xx
        intercept = y_mean - slope * x_mean
        y_pred = [slope * x + intercept for x in xs]
        ss_res = sum((y - yp) ** 2 for y, yp in zip(ys, y_pred))
        ss_tot = sum((y - y_mean) ** 2 for y in ys)
        r_squared = 1 - ss_res / max(ss_tot, 1e-12)
        return slope, intercept, r_squared

    def analyze(self, resource_name: str) -> Optional[TrendResult]:
        samples = list(self._samples)
        if len(samples) < 3:
            return None

        now = time.time()
        # Convert timestamps to hours relative to now
        xs = [(s.timestamp - now) / 3600 for s in samples]
        ys = [s.value for s in samples]
        capacity = samples[-1].capacity

        slope, intercept, r_squared = self._linear_regression(xs, ys)
        current = samples[-1].value
        headroom = capacity - current

        # Time to saturation: solve (slope * t + current_value = capacity)
        hours_to_sat = None
        if slope > 0 and headroom > 0:
            hours_to_sat = headroom / slope

        return TrendResult(
            resource_name=resource_name,
            current_value=round(current, 2),
            capacity=capacity,
            utilization_pct=round(100 * current / max(capacity, 1e-9), 1),
            slope_per_hour=round(slope, 4),
            r_squared=round(r_squared, 3),
            estimated_hours_to_saturation=round(hours_to_sat, 1) if hours_to_sat else None,
            headroom_pct=round(100 * headroom / max(capacity, 1e-9), 1),
        )
```

## Solution 2: Multi-Resource Capacity Monitor

```python
import asyncio
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

@dataclass
class CapacityAlert:
    resource_name: str
    severity: str       # "warning" | "critical"
    message: str
    hours_to_saturation: Optional[float]
    utilization_pct: float
    timestamp: float

class MultiResourceCapacityMonitor:
    """
    Monitors multiple resources (memory, queue depth, token budget, disk)
    and emits alerts when trends project saturation within alert thresholds.
    """

    WARNING_HOURS = 48.0    # alert if saturation within 48h
    CRITICAL_HOURS = 12.0   # critical if saturation within 12h
    HIGH_UTIL_WARNING = 80.0
    HIGH_UTIL_CRITICAL = 90.0

    def __init__(self, window_hours: float = 24.0):
        self._trackers: Dict[str, ResourceTrendTracker] = {}
        self._window = window_hours
        self._alerts: List[CapacityAlert] = []

    def register_resource(self, name: str) -> None:
        self._trackers[name] = ResourceTrendTracker(window_hours=self._window)

    def record(self, resource_name: str, value: float, capacity: float) -> None:
        if resource_name not in self._trackers:
            self.register_resource(resource_name)
        self._trackers[resource_name].record(value, capacity)

    def analyze_all(self) -> List[CapacityAlert]:
        alerts = []
        for name, tracker in self._trackers.items():
            result = tracker.analyze(name)
            if not result:
                continue

            # High utilization alert
            if result.utilization_pct >= self.HIGH_UTIL_CRITICAL:
                alerts.append(CapacityAlert(
                    resource_name=name,
                    severity="critical",
                    message=f"{name} at {result.utilization_pct:.1f}% utilization",
                    hours_to_saturation=result.estimated_hours_to_saturation,
                    utilization_pct=result.utilization_pct,
                    timestamp=time.time(),
                ))
            elif result.utilization_pct >= self.HIGH_UTIL_WARNING:
                alerts.append(CapacityAlert(
                    resource_name=name,
                    severity="warning",
                    message=f"{name} at {result.utilization_pct:.1f}% utilization",
                    hours_to_saturation=result.estimated_hours_to_saturation,
                    utilization_pct=result.utilization_pct,
                    timestamp=time.time(),
                ))

            # Trend-based projection alert
            h = result.estimated_hours_to_saturation
            if h is not None and result.r_squared > 0.6:
                if h <= self.CRITICAL_HOURS:
                    alerts.append(CapacityAlert(
                        resource_name=name,
                        severity="critical",
                        message=(
                            f"{name} projected to saturate in {h:.1f}h "
                            f"(slope={result.slope_per_hour:.3f}/h, r²={result.r_squared:.2f})"
                        ),
                        hours_to_saturation=h,
                        utilization_pct=result.utilization_pct,
                        timestamp=time.time(),
                    ))
                elif h <= self.WARNING_HOURS:
                    alerts.append(CapacityAlert(
                        resource_name=name,
                        severity="warning",
                        message=(
                            f"{name} projected to saturate in {h:.1f}h"
                        ),
                        hours_to_saturation=h,
                        utilization_pct=result.utilization_pct,
                        timestamp=time.time(),
                    ))

        self._alerts.extend(alerts)
        return alerts
```

## Solution 3: Token Budget Capacity Tracker

```python
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

@dataclass
class TokenBudgetSnapshot:
    period: str              # "daily" | "monthly"
    budget_tokens: int
    consumed_tokens: int
    period_start: float
    period_end: float

    @property
    def remaining_tokens(self) -> int:
        return self.budget_tokens - self.consumed_tokens

    @property
    def elapsed_fraction(self) -> float:
        now = time.time()
        total = self.period_end - self.period_start
        elapsed = now - self.period_start
        return min(elapsed / max(total, 1), 1.0)

    @property
    def consumption_rate(self) -> float:
        """Tokens consumed per second."""
        elapsed = time.time() - self.period_start
        return self.consumed_tokens / max(elapsed, 1)

    @property
    def projected_total(self) -> float:
        """Projected total consumption at current rate for the full period."""
        total_seconds = self.period_end - self.period_start
        return self.consumption_rate * total_seconds

    @property
    def will_exceed_budget(self) -> bool:
        return self.projected_total > self.budget_tokens

class TokenBudgetCapacityTracker:
    """
    Projects token consumption against billing period budgets.
    Alerts when projected consumption will exceed budget before period end.
    """

    def __init__(self):
        self._snapshots: Dict[str, TokenBudgetSnapshot] = {}

    def update(self, snapshot: TokenBudgetSnapshot) -> None:
        self._snapshots[snapshot.period] = snapshot

    def analyze(self) -> List[dict]:
        results = []
        for period, snap in self._snapshots.items():
            utilization = snap.consumed_tokens / max(snap.budget_tokens, 1)
            projected = snap.projected_total
            projected_utilization = projected / max(snap.budget_tokens, 1)
            time_remaining_hours = (snap.period_end - time.time()) / 3600

            results.append({
                "period": period,
                "consumed_tokens": snap.consumed_tokens,
                "budget_tokens": snap.budget_tokens,
                "current_utilization_pct": round(utilization * 100, 1),
                "projected_utilization_pct": round(projected_utilization * 100, 1),
                "will_exceed": snap.will_exceed_budget,
                "time_remaining_hours": round(time_remaining_hours, 1),
                "consumption_rate_per_hour": round(snap.consumption_rate * 3600, 0),
                "severity": (
                    "critical" if snap.will_exceed_budget and time_remaining_hours < 24
                    else "warning" if snap.will_exceed_budget
                    else "ok"
                ),
            })
        return results
```

## Solution 4: Growth Rate Comparator

```python
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

@dataclass
class GrowthComparison:
    resource_name: str
    current_week_slope: float
    previous_week_slope: float
    growth_acceleration: float   # positive = accelerating growth
    is_accelerating: bool

class GrowthRateComparator:
    """
    Compares growth rates between time windows to detect acceleration.
    Accelerating growth (increasing slope) requires more urgent capacity response
    than steady linear growth.
    """

    def __init__(self, tracker: ResourceTrendTracker):
        self._tracker = tracker

    def compare_windows(
        self,
        resource_name: str,
        recent_hours: float = 24.0,
        previous_hours: float = 48.0,
    ) -> Optional[GrowthComparison]:
        samples = list(self._tracker._samples)
        if len(samples) < 10:
            return None

        now = time.time()
        recent_cutoff = now - recent_hours * 3600
        prev_cutoff = now - previous_hours * 3600

        recent = [(s.timestamp, s.value) for s in samples if s.timestamp >= recent_cutoff]
        previous = [(s.timestamp, s.value) for s in samples
                    if prev_cutoff <= s.timestamp < recent_cutoff]

        if len(recent) < 3 or len(previous) < 3:
            return None

        def slope(points):
            xs = [t for t, _ in points]
            ys = [v for _, v in points]
            x_mean = sum(xs) / len(xs)
            y_mean = sum(ys) / len(ys)
            num = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys))
            den = sum((x - x_mean) ** 2 for x in xs)
            return num / max(den, 1e-12)

        recent_slope = slope(recent) * 3600   # per hour
        prev_slope = slope(previous) * 3600

        acceleration = recent_slope - prev_slope

        return GrowthComparison(
            resource_name=resource_name,
            current_week_slope=round(recent_slope, 4),
            previous_week_slope=round(prev_slope, 4),
            growth_acceleration=round(acceleration, 4),
            is_accelerating=acceleration > abs(prev_slope) * 0.2,
        )
```

## Solution 5: Capacity Planning Report Generator

```python
import time
from typing import Dict, List

class CapacityPlanningReportGenerator:
    """
    Generates human-readable capacity planning reports from trend analysis.
    Outputs structured data suitable for weekly planning emails or runbooks.
    """

    def __init__(self, monitor: MultiResourceCapacityMonitor):
        self._monitor = monitor

    def generate(self) -> dict:
        alerts = self._monitor.analyze_all()
        critical = [a for a in alerts if a.severity == "critical"]
        warnings = [a for a in alerts if a.severity == "warning"]

        resource_summaries = []
        for name, tracker in self._monitor._trackers.items():
            result = tracker.analyze(name)
            if not result:
                continue
            resource_summaries.append({
                "resource": name,
                "utilization_pct": result.utilization_pct,
                "headroom_pct": result.headroom_pct,
                "slope_per_hour": result.slope_per_hour,
                "hours_to_saturation": result.estimated_hours_to_saturation,
                "trend_fit_r2": result.r_squared,
                "status": (
                    "critical" if result.utilization_pct >= 90 or
                    (result.estimated_hours_to_saturation and result.estimated_hours_to_saturation <= 12)
                    else "warning" if result.utilization_pct >= 80 or
                    (result.estimated_hours_to_saturation and result.estimated_hours_to_saturation <= 48)
                    else "healthy"
                ),
            })

        return {
            "generated_at": time.time(),
            "summary": {
                "critical_resources": len(critical),
                "warning_resources": len(warnings),
                "healthy_resources": len(resource_summaries) - len(critical) - len(warnings),
            },
            "active_alerts": [
                {"severity": a.severity, "resource": a.resource_name, "message": a.message}
                for a in critical + warnings
            ],
            "resource_details": sorted(
                resource_summaries,
                key=lambda x: x["utilization_pct"],
                reverse=True,
            ),
        }
```

## Solution 6: Capacity Metrics Exporter

```python
import time
from typing import Callable, Dict, List, Optional

class CapacityMetricsExporter:
    """
    Exports capacity planning metrics in Prometheus exposition format.
    Enables alerting rules like: alert when time_to_saturation_hours < 24.
    """

    def __init__(self, monitor: MultiResourceCapacityMonitor):
        self._monitor = monitor

    def prometheus_metrics(self) -> str:
        lines = [
            "# HELP agent_resource_utilization_pct Current resource utilization percent",
            "# TYPE agent_resource_utilization_pct gauge",
            "# HELP agent_resource_hours_to_saturation Projected hours until resource saturates",
            "# TYPE agent_resource_hours_to_saturation gauge",
            "# HELP agent_resource_slope_per_hour Resource growth rate per hour",
            "# TYPE agent_resource_slope_per_hour gauge",
        ]
        for name, tracker in self._monitor._trackers.items():
            result = tracker.analyze(name)
            if not result:
                continue
            label = f'resource="{name}"'
            lines.append(f'agent_resource_utilization_pct{{{label}}} {result.utilization_pct}')
            if result.estimated_hours_to_saturation is not None:
                lines.append(
                    f'agent_resource_hours_to_saturation{{{label}}} '
                    f'{result.estimated_hours_to_saturation}'
                )
            else:
                lines.append(f'agent_resource_hours_to_saturation{{{label}}} +Inf')
            lines.append(f'agent_resource_slope_per_hour{{{label}}} {result.slope_per_hour}')

        return "\n".join(lines) + "\n"
```

## Comparison

| Approach | Detection Method | Forecasting | Alerting | Export Format |
|---|---|---|---|---|
| ResourceTrendTracker | Linear regression | Yes (hours-to-sat) | No | Structured dict |
| MultiResourceCapacityMonitor | Trend + utilization | Via tracker | Yes | Alert list |
| TokenBudgetCapacityTracker | Rate projection | Yes (period end) | Via severity | Structured dict |
| GrowthRateComparator | Window comparison | Acceleration only | No | Structured dict |
| CapacityPlanningReportGenerator | Combined | Via monitor | Via alerts | Human-readable |
| CapacityMetricsExporter | Via monitor | Via tracker | Via Prometheus rules | Prometheus text |

**Best for production**: Deploy `MultiResourceCapacityMonitor` tracking memory, queue depth, disk, token budgets, and connection pool utilization. Expose via `CapacityMetricsExporter` to Prometheus and create alerting rules on `agent_resource_hours_to_saturation < 48`. Use `GrowthRateComparator` to detect acceleration events — a resource growing 3x faster than last week needs attention even if current utilization looks safe. Generate weekly `CapacityPlanningReportGenerator` reports for infrastructure planning decisions.
