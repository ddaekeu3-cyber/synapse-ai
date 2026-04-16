---
title: "Agent Doesn't Implement Composite Agent Health Score"
description: "Individual metrics like error rate or p99 latency tell you something is wrong but not how wrong or which dimension is degrading. A composite health score aggregates multiple signals into a single number that drives alerting, dashboards, and auto-remediation."
difficulty: intermediate
category: observability
tags: [health-score, composite-metric, monitoring, alerting, SLO, observability, prometheus]
---

## Problem

An agent has dozens of observable signals: error rate, latency percentiles, token usage, tool call success rate, queue depth, cache hit rate, model response quality. Operations teams monitor each independently and must mentally combine them to decide if the agent is healthy. There is no single number to put on a dashboard, set an alert threshold on, or expose to an auto-scaler.

```python
# Broken: individual metrics in isolation — no synthesized health signal
class AgentMetrics:
    error_rate: float        # 0.05 — is this bad?
    p99_latency_ms: float    # 4200 — bad or acceptable?
    cache_hit_rate: float    # 0.65 — OK?
    tool_success_rate: float # 0.88 — concerning?
    # No way to answer: "Is the agent healthy right now?"
```

---

## Solution 1: Weighted Linear Health Score

```python
import time
from dataclasses import dataclass, field
from typing import Callable

@dataclass
class HealthDimension:
    name: str
    weight: float          # contribution to total score (weights sum to 1.0)
    measure_fn: Callable[[], float]  # returns value in natural units
    normalize_fn: Callable[[float], float]  # maps value → [0.0, 1.0] score
    critical_threshold: float = 0.3  # score below this → critical

def clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))

class CompositeHealthScore:
    """
    Computes a [0.0, 1.0] health score from weighted dimensions.
    1.0 = fully healthy, 0.0 = completely degraded.
    """

    def __init__(self, dimensions: list[HealthDimension]):
        total_weight = sum(d.weight for d in dimensions)
        assert abs(total_weight - 1.0) < 0.001, \
            f"Weights must sum to 1.0, got {total_weight}"
        self._dims = dimensions

    def compute(self) -> dict:
        dimension_scores: dict[str, float] = {}
        weighted_sum = 0.0
        critical_dims: list[str] = []

        for dim in self._dims:
            try:
                raw = dim.measure_fn()
                score = clamp(dim.normalize_fn(raw))
            except Exception as e:
                print(f"[Health] Failed to measure {dim.name}: {e}")
                score = 0.0
                raw = None

            dimension_scores[dim.name] = score
            weighted_sum += score * dim.weight
            if score < dim.critical_threshold:
                critical_dims.append(dim.name)

        overall = clamp(weighted_sum)
        return {
            "score": round(overall, 3),
            "status": _classify(overall, critical_dims),
            "dimensions": {k: round(v, 3) for k, v in dimension_scores.items()},
            "critical_dimensions": critical_dims,
            "timestamp": time.time(),
        }

def _classify(score: float, critical_dims: list[str]) -> str:
    if critical_dims:
        return "critical"
    if score >= 0.9:
        return "healthy"
    if score >= 0.7:
        return "degraded"
    if score >= 0.4:
        return "unhealthy"
    return "critical"

# Example: build a health score for a typical agent
def build_agent_health_score(metrics_store: "MetricsStore") -> CompositeHealthScore:
    return CompositeHealthScore([
        HealthDimension(
            name="error_rate",
            weight=0.30,
            measure_fn=lambda: metrics_store.error_rate_1m(),
            # 0% error → score 1.0; 20% error → score 0.0
            normalize_fn=lambda r: clamp(1.0 - r / 0.20),
            critical_threshold=0.3,
        ),
        HealthDimension(
            name="p99_latency",
            weight=0.25,
            measure_fn=lambda: metrics_store.p99_latency_ms(),
            # <1000ms → 1.0; >10000ms → 0.0 (linear interpolation)
            normalize_fn=lambda ms: clamp(1.0 - (ms - 1000) / 9000),
            critical_threshold=0.2,
        ),
        HealthDimension(
            name="tool_success_rate",
            weight=0.25,
            measure_fn=lambda: metrics_store.tool_success_rate(),
            # >95% → 1.0; <70% → 0.0
            normalize_fn=lambda r: clamp((r - 0.70) / 0.25),
            critical_threshold=0.3,
        ),
        HealthDimension(
            name="queue_depth",
            weight=0.10,
            measure_fn=lambda: metrics_store.queue_depth(),
            # <10 → 1.0; >1000 → 0.0 (log scale)
            normalize_fn=lambda d: clamp(1.0 - max(0, (d - 10)) / 990),
            critical_threshold=0.2,
        ),
        HealthDimension(
            name="cache_hit_rate",
            weight=0.10,
            measure_fn=lambda: metrics_store.cache_hit_rate(),
            # >80% → 1.0; <20% → 0.0
            normalize_fn=lambda r: clamp((r - 0.20) / 0.60),
            critical_threshold=0.1,
        ),
    ])
```

---

## Solution 2: Time-Weighted Exponential Health Score

```python
import asyncio
import math
import time
from collections import deque
from dataclasses import dataclass, field

@dataclass
class HealthSample:
    score: float
    timestamp: float = field(default_factory=time.monotonic)

class ExponentialHealthScore:
    """
    Computes health as an exponential moving average of samples.
    Recent samples have much higher weight than old ones.
    Fast to degrade on sustained problems; slow to recover (avoid flapping).
    """

    def __init__(self, decay_halflife_seconds: float = 60.0,
                 min_samples: int = 3):
        self._decay = math.log(2) / decay_halflife_seconds
        self._samples: deque[HealthSample] = deque(maxlen=1000)
        self._min_samples = min_samples

    def record(self, score: float):
        """Record a health sample (0.0 = bad, 1.0 = good)."""
        self._samples.append(HealthSample(score=clamp(score)))

    def current_score(self) -> float | None:
        """Returns EMA score or None if insufficient samples."""
        if len(self._samples) < self._min_samples:
            return None
        now = time.monotonic()
        total_weight = 0.0
        weighted_sum = 0.0
        for sample in self._samples:
            age = now - sample.timestamp
            weight = math.exp(-self._decay * age)
            weighted_sum += sample.score * weight
            total_weight += weight
        if total_weight < 1e-9:
            return None
        return clamp(weighted_sum / total_weight)

    def trend(self) -> str:
        """Compares recent half vs older half of samples."""
        if len(self._samples) < 6:
            return "insufficient_data"
        mid = len(self._samples) // 2
        samples_list = list(self._samples)
        older_avg = sum(s.score for s in samples_list[:mid]) / mid
        newer_avg = sum(s.score for s in samples_list[mid:]) / (len(samples_list) - mid)
        diff = newer_avg - older_avg
        if diff > 0.05:
            return "improving"
        if diff < -0.05:
            return "degrading"
        return "stable"

def clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))

class SampleCollector:
    """Periodically samples raw metrics and feeds into EMA health score."""

    def __init__(self, health: ExponentialHealthScore,
                 compute_fn: "Callable[[], float]",
                 interval: float = 15.0):
        self._health = health
        self._compute_fn = compute_fn
        self._interval = interval

    async def run(self):
        while True:
            try:
                score = self._compute_fn()
                self._health.record(score)
            except Exception as e:
                print(f"[HealthSampler] Error: {e}")
                self._health.record(0.0)  # failure → mark as unhealthy
            await asyncio.sleep(self._interval)
```

---

## Solution 3: Multi-Level Health Score with Severity Classification

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

class Severity(str, Enum):
    OK       = "ok"
    WARN     = "warn"
    CRITICAL = "critical"
    UNKNOWN  = "unknown"

@dataclass
class DimensionResult:
    name: str
    score: float
    severity: Severity
    value: Any
    message: str = ""

@dataclass
class HealthReport:
    overall_score: float
    overall_severity: Severity
    dimensions: list[DimensionResult]
    summary: str
    timestamp: float = field(default_factory=__import__("time").time)

    def worst_dimensions(self, n: int = 3) -> list[DimensionResult]:
        return sorted(self.dimensions, key=lambda d: d.score)[:n]

    def critical_count(self) -> int:
        return sum(1 for d in self.dimensions if d.severity == Severity.CRITICAL)

class ThresholdHealthDimension:
    """Dimension with explicit warn/critical thresholds."""

    def __init__(self, name: str, weight: float,
                 warn_threshold: float, critical_threshold: float,
                 higher_is_better: bool = False):
        self.name = name
        self.weight = weight
        self.warn = warn_threshold
        self.critical = critical_threshold
        self.higher_is_better = higher_is_better

    def evaluate(self, value: float) -> DimensionResult:
        if self.higher_is_better:
            if value >= self.warn:
                score, sev = 1.0, Severity.OK
                msg = f"{value:.2f} ≥ {self.warn} (OK)"
            elif value >= self.critical:
                ratio = (value - self.critical) / (self.warn - self.critical)
                score, sev = ratio * 0.5 + 0.3, Severity.WARN
                msg = f"{value:.2f} between critical/warn thresholds"
            else:
                score, sev = 0.0, Severity.CRITICAL
                msg = f"{value:.2f} < {self.critical} (CRITICAL)"
        else:
            if value <= self.warn:
                score, sev = 1.0, Severity.OK
                msg = f"{value:.2f} ≤ {self.warn} (OK)"
            elif value <= self.critical:
                ratio = 1.0 - (value - self.warn) / (self.critical - self.warn)
                score, sev = ratio * 0.5 + 0.3, Severity.WARN
                msg = f"{value:.2f} between warn/critical thresholds"
            else:
                score, sev = 0.0, Severity.CRITICAL
                msg = f"{value:.2f} > {self.critical} (CRITICAL)"

        return DimensionResult(name=self.name, score=score,
                               severity=sev, value=value, message=msg)

class MultiLevelHealthScorer:
    def __init__(self, dimensions: list[ThresholdHealthDimension]):
        self._dims = dimensions

    def evaluate(self, values: dict[str, float]) -> HealthReport:
        results: list[DimensionResult] = []
        weighted_score = 0.0
        total_weight = sum(d.weight for d in self._dims)

        for dim in self._dims:
            value = values.get(dim.name)
            if value is None:
                result = DimensionResult(dim.name, 0.5, Severity.UNKNOWN,
                                         None, "No data")
            else:
                result = dim.evaluate(value)
            results.append(result)
            weighted_score += result.score * (dim.weight / total_weight)

        overall = clamp(weighted_score)
        critical_dims = [r for r in results if r.severity == Severity.CRITICAL]

        if critical_dims:
            sev = Severity.CRITICAL
            summary = f"CRITICAL: {', '.join(r.name for r in critical_dims)}"
        elif any(r.severity == Severity.WARN for r in results):
            sev = Severity.WARN
            summary = "Degraded performance in some dimensions"
        else:
            sev = Severity.OK
            summary = "All dimensions healthy"

        return HealthReport(overall_score=round(overall, 3),
                            overall_severity=sev,
                            dimensions=results,
                            summary=summary)
```

---

## Solution 4: SLO-Based Health Score

```python
import time
from dataclasses import dataclass, field

@dataclass
class SLOTarget:
    name: str
    target: float        # e.g., 0.999 = 99.9% availability
    window_seconds: float = 3600.0  # rolling window

@dataclass
class SLOBurn:
    slo: SLOTarget
    error_events: list[float] = field(default_factory=list)   # timestamps
    total_events: list[float] = field(default_factory=list)   # timestamps

    def _in_window(self, events: list[float]) -> int:
        cutoff = time.monotonic() - self.slo.window_seconds
        return sum(1 for t in events if t >= cutoff)

    def current_slo(self) -> float:
        total = self._in_window(self.total_events)
        if total == 0:
            return 1.0
        errors = self._in_window(self.error_events)
        return 1.0 - (errors / total)

    def error_budget_remaining(self) -> float:
        """Returns fraction of error budget remaining (1.0 = full, 0.0 = exhausted)."""
        allowed_error_rate = 1.0 - self.slo.target
        actual_error_rate = 1.0 - self.current_slo()
        if allowed_error_rate <= 0:
            return 0.0 if actual_error_rate > 0 else 1.0
        return clamp(1.0 - actual_error_rate / allowed_error_rate)

    def burn_rate(self) -> float:
        """How fast we're burning error budget (1.0 = exactly at SLO limit)."""
        budget_remaining = self.error_budget_remaining()
        # Full burn rate = consuming all budget in one window
        return clamp(1.0 - budget_remaining)

class SLOHealthScorer:
    """
    Health score derived directly from SLO compliance and error budget burn.
    """

    def __init__(self, slo_trackers: list[SLOBurn]):
        self._trackers = slo_trackers

    def record_event(self, slo_name: str, is_error: bool = False):
        for tracker in self._trackers:
            if tracker.slo.name == slo_name:
                tracker.total_events.append(time.monotonic())
                if is_error:
                    tracker.error_events.append(time.monotonic())
                break

    def compute(self) -> dict:
        results = []
        overall_score = 1.0

        for tracker in self._trackers:
            slo_met = tracker.current_slo()
            budget_remaining = tracker.error_budget_remaining()
            burn_rate = tracker.burn_rate()

            # Score: weighted between SLO compliance and budget remaining
            dim_score = 0.6 * clamp(slo_met / tracker.slo.target) + \
                        0.4 * budget_remaining

            results.append({
                "slo": tracker.slo.name,
                "target": tracker.slo.target,
                "current": round(slo_met, 5),
                "budget_remaining_pct": round(budget_remaining * 100, 1),
                "burn_rate": round(burn_rate, 3),
                "score": round(clamp(dim_score), 3),
            })
            overall_score = min(overall_score, clamp(dim_score))

        return {
            "overall_score": round(overall_score, 3),
            "slos": results,
            "alert": overall_score < 0.5,
        }

def clamp(v, lo=0.0, hi=1.0):
    return max(lo, min(hi, v))
```

---

## Solution 5: Prometheus-Integrated Health Score Exporter

```python
import asyncio
import time
from typing import Callable

# Requires: pip install prometheus-client
try:
    from prometheus_client import Gauge, Counter, Histogram, start_http_server
    HAS_PROMETHEUS = True
except ImportError:
    HAS_PROMETHEUS = False

class PrometheusHealthExporter:
    """
    Exports the composite health score and per-dimension scores as
    Prometheus gauges, enabling Grafana dashboards and alerting rules.
    """

    def __init__(self, scorer: "CompositeHealthScore",
                 agent_id: str = "default"):
        self._scorer = scorer
        self._agent_id = agent_id
        if HAS_PROMETHEUS:
            labels = ["agent_id"]
            self._overall = Gauge(
                "agent_health_score", "Overall agent health score [0-1]", labels
            )
            self._dimension = Gauge(
                "agent_health_dimension_score",
                "Per-dimension health score [0-1]",
                labels + ["dimension"]
            )
            self._status = Gauge(
                "agent_health_status",
                "Agent health status (1=healthy, 0.5=degraded, 0=critical)",
                labels
            )
            self._compute_errors = Counter(
                "agent_health_compute_errors_total",
                "Health score computation errors",
                labels
            )

    STATUS_VALUES = {"healthy": 1.0, "degraded": 0.5,
                     "unhealthy": 0.25, "critical": 0.0}

    async def export_loop(self, interval: float = 15.0):
        """Periodically compute and export health scores."""
        while True:
            try:
                report = self._scorer.compute()
                if HAS_PROMETHEUS:
                    self._overall.labels(self._agent_id).set(report["score"])
                    self._status.labels(self._agent_id).set(
                        self.STATUS_VALUES.get(report["status"], 0.0)
                    )
                    for dim_name, dim_score in report["dimensions"].items():
                        self._dimension.labels(self._agent_id, dim_name).set(dim_score)
                print(f"[Health] score={report['score']:.3f} "
                      f"status={report['status']} "
                      f"critical={report['critical_dimensions']}")
            except Exception as e:
                print(f"[Health] Computation error: {e}")
                if HAS_PROMETHEUS:
                    self._compute_errors.labels(self._agent_id).inc()
            await asyncio.sleep(interval)

# Alertmanager rule (YAML — place in your Prometheus rules):
ALERTMANAGER_RULE = """
groups:
  - name: agent_health
    rules:
      - alert: AgentHealthCritical
        expr: agent_health_score < 0.4
        for: 2m
        labels:
          severity: critical
        annotations:
          summary: "Agent health score critical: {{ $value }}"
      - alert: AgentDimensionDegraded
        expr: agent_health_dimension_score < 0.5
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "Agent dimension {{ $labels.dimension }} degraded: {{ $value }}"
"""
```

---

## Solution 6: Self-Healing Trigger Based on Health Score

```python
import asyncio
from dataclasses import dataclass
from typing import Callable, Awaitable

@dataclass
class RemediationAction:
    name: str
    score_threshold: float   # trigger when score drops below this
    action: Callable[[], Awaitable[None]]
    cooldown_seconds: float = 300.0
    _last_triggered: float = 0.0

    def can_trigger(self) -> bool:
        import time
        return time.time() - self._last_triggered >= self.cooldown_seconds

    def mark_triggered(self):
        import time
        self._last_triggered = time.time()

class SelfHealingHealthMonitor:
    """
    Monitors composite health score and triggers remediation actions
    when thresholds are crossed, with cooldown to prevent thrashing.
    """

    def __init__(self, scorer: "CompositeHealthScore",
                 actions: list[RemediationAction],
                 check_interval: float = 30.0):
        self._scorer = scorer
        self._actions = sorted(actions, key=lambda a: a.score_threshold)
        self._check_interval = check_interval
        self._last_status: str = "unknown"

    async def run(self):
        while True:
            await asyncio.sleep(self._check_interval)
            try:
                report = self._scorer.compute()
                score = report["score"]
                status = report["status"]

                if status != self._last_status:
                    print(f"[SelfHeal] Status change: {self._last_status} → {status} "
                          f"(score={score:.3f})")
                    self._last_status = status

                for action in self._actions:
                    if score < action.score_threshold and action.can_trigger():
                        print(f"[SelfHeal] Triggering '{action.name}' "
                              f"(score={score:.3f} < {action.score_threshold})")
                        try:
                            await action.action()
                            action.mark_triggered()
                        except Exception as e:
                            print(f"[SelfHeal] Action '{action.name}' failed: {e}")

            except Exception as e:
                print(f"[SelfHeal] Monitor error: {e}")

# Example remediation actions
async def clear_request_queue(): print("[Action] Queue cleared")
async def restart_tool_pool(): print("[Action] Tool pool restarted")
async def scale_up_workers(): print("[Action] Worker count increased")
async def alert_on_call(): print("[Action] On-call paged")

def build_self_healing_monitor(scorer) -> SelfHealingHealthMonitor:
    return SelfHealingHealthMonitor(
        scorer=scorer,
        actions=[
            RemediationAction("clear_queue", 0.7, clear_request_queue, 60.0),
            RemediationAction("restart_tools", 0.5, restart_tool_pool, 300.0),
            RemediationAction("scale_up", 0.4, scale_up_workers, 600.0),
            RemediationAction("alert_oncall", 0.3, alert_on_call, 1800.0),
        ]
    )
```

---

## Comparison

| Solution | Aggregation | Trend | SLO-Aware | Auto-Remediate | Export | Best For |
|---|---|---|---|---|---|---|
| 1. Weighted linear | Weighted avg | No | No | No | No | Simple dashboards |
| 2. EMA time-weighted | Exponential MA | Yes | No | No | No | Flap-resistant alerting |
| 3. Multi-level threshold | Weighted + severity | No | No | No | No | Ops with defined thresholds |
| 4. SLO-based | Error budget burn | No | Yes | No | No | SRE teams with SLO contracts |
| 5. Prometheus exporter | Any scorer | No | No | No | Prometheus | Grafana dashboards + alerts |
| 6. Self-healing monitor | Any scorer | No | No | Yes | No | Autonomous recovery |

**Key principle**: the composite health score is not a replacement for individual metrics — it's an index that answers "should I be worried right now?" It should feed alerting (one alert, not ten), dashboards (one panel on the top), and auto-remediation triggers. Keep the underlying dimensions accessible for drill-down. Weight error rate and latency highest: users notice these first.
