---
title: "Agent Doesn't Implement Tool Latency Percentile Tracking Per Environment"
description: "Agents that average tool latency across all environments mask critical signals: a tool running at P99=200ms in staging but P99=4000ms in production indicates an infrastructure gap, not a tool bug. Implement per-environment tool latency percentile tracking that maintains separate histograms per tool per environment, computes P50/P95/P99, and alerts when production latency diverges significantly from staging baselines."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-tool-latency-percentile-tracking-per-environment
tags: [latency-percentiles, per-environment, tool-latency, p99, histogram, environment-comparison]
symptoms:
  - "P99 latency is averaged across staging and production, hiding environment-specific regressions"
  - "A tool performs well in staging but is 10× slower in production with no alert"
  - "No per-environment breakdown — cannot isolate whether slowness is config or infra"
  - "Latency dashboards show mean only — P95/P99 tail latency is invisible"
  - "Environment promotion decisions are made without comparing latency profiles"
---

## Why This Happens

Latency averages are dominated by the median and cannot represent tail behavior. P99 latency — the slowest 1% of requests — is often 5–10× the median and is what users actually experience during peak load. Mixing environments in the same histogram makes it impossible to answer "is production slower than staging?" — a question that catches misconfigured connection pools, missing CDN caches, and cold-start penalties that only appear in production.

## Solution 1: Environment-Tagged Latency Sample

```python
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class LatencySample:
    tool_name: str
    environment: str          # "production" | "staging" | "development" | custom
    latency_ms: float
    success: bool
    recorded_at: float = field(default_factory=time.time)
    model: Optional[str] = None
    session_id: Optional[str] = None

    def __post_init__(self) -> None:
        if self.latency_ms < 0:
            raise ValueError("latency_ms must be non-negative")
```

## Solution 2: Percentile Histogram Per Tool Per Environment

```python
import bisect
import time
from typing import Dict, List, Optional, Tuple


class ToolEnvironmentHistogram:
    """
    Maintains a sorted sample buffer per (tool, environment) pair.
    Computes exact percentiles via interpolation over the sample buffer.
    Trims to max_samples when full to bound memory usage.
    """

    def __init__(self, max_samples: int = 2000) -> None:
        self._max = max_samples
        self._samples: List[float] = []   # kept sorted via bisect
        self._count = 0
        self._error_count = 0
        self._sum = 0.0
        self._first_at: Optional[float] = None
        self._last_at: Optional[float] = None

    def record(self, latency_ms: float, success: bool = True) -> None:
        now = time.time()
        if self._first_at is None:
            self._first_at = now
        self._last_at = now
        self._count += 1
        self._sum += latency_ms
        if not success:
            self._error_count += 1
            return   # don't skew latency histogram with error latencies

        bisect.insort(self._samples, latency_ms)
        if len(self._samples) > self._max:
            # Evict oldest approximation: remove from middle to preserve tails
            self._samples.pop(self._max // 2)

    def percentile(self, p: float) -> Optional[float]:
        """p in [0, 100]. Returns None if fewer than 5 samples."""
        if len(self._samples) < 5:
            return None
        idx = (p / 100.0) * (len(self._samples) - 1)
        lo, hi = int(idx), min(int(idx) + 1, len(self._samples) - 1)
        frac = idx - lo
        value = self._samples[lo] * (1 - frac) + self._samples[hi] * frac
        return round(value, 2)

    def mean(self) -> Optional[float]:
        success_count = self._count - self._error_count
        if success_count == 0:
            return None
        return round(self._sum / success_count, 2)

    def summary(self) -> dict:
        return {
            "count": self._count,
            "error_count": self._error_count,
            "error_rate": round(self._error_count / max(self._count, 1), 4),
            "mean_ms": self.mean(),
            "p50_ms": self.percentile(50),
            "p75_ms": self.percentile(75),
            "p95_ms": self.percentile(95),
            "p99_ms": self.percentile(99),
            "p999_ms": self.percentile(99.9),
            "min_ms": self._samples[0] if self._samples else None,
            "max_ms": self._samples[-1] if self._samples else None,
        }
```

## Solution 3: Per-Environment Latency Registry

```python
from typing import Dict, List, Tuple


class PerEnvironmentLatencyRegistry:
    """
    Maintains separate histograms per (tool_name, environment) pair.
    Ingests LatencySamples and routes them to the correct histogram.
    """

    def __init__(self, max_samples_per_bucket: int = 2000) -> None:
        self._max_samples = max_samples_per_bucket
        self._histograms: Dict[Tuple[str, str], ToolEnvironmentHistogram] = {}

    def _key(self, tool_name: str, environment: str) -> Tuple[str, str]:
        return (tool_name, environment)

    def record(self, sample: LatencySample) -> None:
        key = self._key(sample.tool_name, sample.environment)
        if key not in self._histograms:
            self._histograms[key] = ToolEnvironmentHistogram(self._max_samples)
        self._histograms[key].record(sample.latency_ms, sample.success)

    def get(
        self,
        tool_name: str,
        environment: str,
    ) -> Optional[ToolEnvironmentHistogram]:
        return self._histograms.get(self._key(tool_name, environment))

    def tools(self) -> List[str]:
        return list({k[0] for k in self._histograms})

    def environments(self) -> List[str]:
        return list({k[1] for k in self._histograms})

    def all_summaries(self) -> Dict[str, dict]:
        return {
            f"{tool}:{env}": hist.summary()
            for (tool, env), hist in self._histograms.items()
        }
```

## Solution 4: Environment Comparison Analyzer

```python
from typing import Dict, List, Optional


@dataclass
class EnvironmentLatencyComparison:
    tool_name: str
    baseline_env: str
    target_env: str
    percentile: float
    baseline_ms: Optional[float]
    target_ms: Optional[float]
    ratio: Optional[float]   # target / baseline
    is_regression: bool
    severity: str   # "none" | "warning" | "critical"


class EnvironmentComparisonAnalyzer:
    """
    Compares tool latency percentiles between environments.
    Flags regressions where production is significantly slower than staging.
    """

    def __init__(
        self,
        registry: PerEnvironmentLatencyRegistry,
        regression_warning_ratio: float = 2.0,   # target is 2× baseline
        regression_critical_ratio: float = 5.0,
        compare_percentile: float = 95.0,
    ) -> None:
        self._registry = registry
        self._warning = regression_warning_ratio
        self._critical = regression_critical_ratio
        self._percentile = compare_percentile

    def compare(
        self,
        tool_name: str,
        baseline_env: str = "staging",
        target_env: str = "production",
    ) -> EnvironmentLatencyComparison:
        baseline_hist = self._registry.get(tool_name, baseline_env)
        target_hist = self._registry.get(tool_name, target_env)

        baseline_ms = baseline_hist.percentile(self._percentile) if baseline_hist else None
        target_ms = target_hist.percentile(self._percentile) if target_hist else None

        ratio = None
        is_regression = False
        severity = "none"

        if baseline_ms and target_ms and baseline_ms > 0:
            ratio = round(target_ms / baseline_ms, 2)
            if ratio >= self._critical:
                is_regression = True
                severity = "critical"
            elif ratio >= self._warning:
                is_regression = True
                severity = "warning"

        return EnvironmentLatencyComparison(
            tool_name=tool_name,
            baseline_env=baseline_env,
            target_env=target_env,
            percentile=self._percentile,
            baseline_ms=baseline_ms,
            target_ms=target_ms,
            ratio=ratio,
            is_regression=is_regression,
            severity=severity,
        )

    def compare_all(
        self,
        baseline_env: str = "staging",
        target_env: str = "production",
    ) -> List[EnvironmentLatencyComparison]:
        return [
            self.compare(tool, baseline_env, target_env)
            for tool in self._registry.tools()
        ]
```

## Solution 5: Latency Regression Alert Manager

```python
import time
from typing import Callable, List, Optional


class LatencyRegressionAlertManager:
    """
    Fires alerts when environment comparison analysis detects latency regressions.
    Uses per-tool cooldowns to prevent alert storms during sustained slowness.
    """

    def __init__(
        self,
        analyzer: EnvironmentComparisonAnalyzer,
        alert_handler: Optional[Callable[[dict], None]] = None,
        cooldown_seconds: float = 600.0,
    ) -> None:
        self._analyzer = analyzer
        self._handler = alert_handler
        self._cooldown = cooldown_seconds
        self._last_fired: Dict[str, float] = {}

    def _can_fire(self, key: str) -> bool:
        last = self._last_fired.get(key, 0.0)
        if time.time() - last >= self._cooldown:
            self._last_fired[key] = time.time()
            return True
        return False

    def check(
        self,
        baseline_env: str = "staging",
        target_env: str = "production",
    ) -> List[dict]:
        comparisons = self._analyzer.compare_all(baseline_env, target_env)
        alerts = []

        for comp in comparisons:
            if not comp.is_regression:
                continue
            key = f"{comp.tool_name}:{comp.target_env}:{comp.severity}"
            if not self._can_fire(key):
                continue

            alert = {
                "type": "latency_regression",
                "tool": comp.tool_name,
                "baseline_env": comp.baseline_env,
                "target_env": comp.target_env,
                "percentile": comp.percentile,
                "baseline_ms": comp.baseline_ms,
                "target_ms": comp.target_ms,
                "ratio": comp.ratio,
                "severity": comp.severity,
                "message": (
                    f"Tool '{comp.tool_name}' P{comp.percentile:.0f} in {comp.target_env} "
                    f"is {comp.ratio}× slower than {comp.baseline_env} "
                    f"({comp.target_ms}ms vs {comp.baseline_ms}ms)"
                ),
            }
            alerts.append(alert)
            if self._handler:
                try:
                    self._handler(alert)
                except Exception:
                    pass

        return alerts
```

## Solution 6: Per-Environment Latency Dashboard

```python
import time


class PerEnvironmentLatencyDashboard:
    """
    Combines per-environment histograms, cross-environment comparisons,
    and regression alerts into a single operational report.
    """

    def __init__(
        self,
        registry: PerEnvironmentLatencyRegistry,
        analyzer: EnvironmentComparisonAnalyzer,
        alert_manager: LatencyRegressionAlertManager,
    ) -> None:
        self._registry = registry
        self._analyzer = analyzer
        self._alerts = alert_manager

    def render(
        self,
        baseline_env: str = "staging",
        target_env: str = "production",
    ) -> dict:
        summaries = self._registry.all_summaries()
        comparisons = self._analyzer.compare_all(baseline_env, target_env)
        alerts = self._alert_manager.check(baseline_env, target_env)

        regressions = [c for c in comparisons if c.is_regression]

        return {
            "generated_at": time.time(),
            "environments": self._registry.environments(),
            "tools": self._registry.tools(),
            "histograms": summaries,
            "regressions": [
                {
                    "tool": c.tool_name,
                    "ratio": c.ratio,
                    "severity": c.severity,
                    f"p{c.percentile:.0f}_{baseline_env}_ms": c.baseline_ms,
                    f"p{c.percentile:.0f}_{target_env}_ms": c.target_ms,
                }
                for c in regressions
            ],
            "active_alerts": alerts,
        }
```

## Comparison

| Approach | Per-Environment Separation | Percentile Calculation | Cross-Env Comparison | Regression Alerts | Dashboard |
|---|---|---|---|---|---|
| ToolEnvironmentHistogram | No (single bucket) | Yes (P50–P99.9) | No | No | No |
| PerEnvironmentLatencyRegistry | Yes | Via histogram | No | No | No |
| EnvironmentComparisonAnalyzer | No | Via registry | Yes | No | No |
| LatencyRegressionAlertManager | No | No | Via analyzer | Yes (with cooldown) | No |
| PerEnvironmentLatencyDashboard | No | No | No | Via manager | Yes |

**Best for production**: Tag every tool call with the current environment (`DEPLOY_ENV` or equivalent) at the call site — never infer it. Use P95 for regression comparison rather than P99 to avoid false positives from single-sample outliers. Set `regression_warning_ratio=2.0` and `regression_critical_ratio=5.0`: 2× production-to-staging ratio is worth investigating; 5× warrants an immediate rollback review. Run the comparison before every environment promotion as a deployment gate — if any tool's P95 regresses beyond the warning threshold, block the promotion and require a manual sign-off.
