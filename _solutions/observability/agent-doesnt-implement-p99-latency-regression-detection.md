---
title: "Agent Doesn't Implement P99 Latency Regression Detection"
description: "Agents that compare only average latency miss tail latency regressions: a code change that slows 1% of requests by 10× shows no change in P50 but doubles P99, degrading the experience for hundreds of users. Implement P99 latency regression detection that compares latency percentiles between a baseline window and the current window, and fires alerts when tail latency worsens beyond a configured threshold."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-p99-latency-regression-detection
tags: [p99-latency, latency-regression, tail-latency, percentile-tracking, slo-regression, performance-monitoring]
symptoms:
  - "Deployments that worsen P99 by 200% go undetected because P50 is unchanged"
  - "No percentile comparison between baseline and current window"
  - "Latency alerts fire only on sustained high P50 — tail regressions are invisible"
  - "Cannot determine whether a deployment caused a latency regression"
  - "On-call receives user complaints about slowness but metrics show average latency is fine"
---

## Why This Happens

Average and P50 latency are insensitive to tail regressions because outliers represent a small fraction of requests. A deployment that introduces a slow path executed 1% of the time doubles P99 without touching P50. Without explicit percentile tracking and baseline comparison, tail regressions accumulate silently until they affect enough users to appear in user-reported incidents. P99 regression detection requires maintaining a sliding window of latency samples, computing percentiles, comparing against a baseline window, and alerting when the relative change exceeds a threshold.

## Solution 1: Latency Sample Store

```python
import time
from collections import deque
from threading import Lock
from typing import Deque, List, Optional, Tuple


class LatencySampleStore:
    """
    Stores latency observations with timestamps in a ring buffer.
    Supports percentile queries over arbitrary time windows.
    """

    def __init__(self, max_samples: int = 100_000):
        self._samples: Deque[Tuple[float, float]] = deque(maxlen=max_samples)
        # (timestamp, latency_ms)
        self._lock = Lock()

    def record(self, latency_ms: float, timestamp: Optional[float] = None) -> None:
        ts = timestamp or time.time()
        with self._lock:
            self._samples.append((ts, latency_ms))

    def window_samples(self, window_seconds: float, end_time: Optional[float] = None) -> List[float]:
        end = end_time or time.time()
        start = end - window_seconds
        with self._lock:
            return [ms for ts, ms in self._samples if start <= ts <= end]

    def percentile(
        self,
        pct: float,
        window_seconds: float,
        end_time: Optional[float] = None,
    ) -> Optional[float]:
        samples = self.window_samples(window_seconds, end_time)
        if not samples:
            return None
        sorted_samples = sorted(samples)
        idx = min(int(len(sorted_samples) * pct / 100.0), len(sorted_samples) - 1)
        return round(sorted_samples[idx], 2)

    def percentiles(
        self,
        pcts: List[float],
        window_seconds: float,
        end_time: Optional[float] = None,
    ) -> dict:
        samples = self.window_samples(window_seconds, end_time)
        if not samples:
            return {p: None for p in pcts}
        sorted_samples = sorted(samples)
        n = len(sorted_samples)
        result = {}
        for pct in pcts:
            idx = min(int(n * pct / 100.0), n - 1)
            result[pct] = round(sorted_samples[idx], 2)
        return result

    def sample_count(self, window_seconds: float) -> int:
        return len(self.window_samples(window_seconds))
```

## Solution 2: Baseline Window Manager

```python
import time
from dataclasses import dataclass, field
from typing import Dict, Optional


@dataclass
class LatencyBaseline:
    captured_at: float
    window_seconds: float
    sample_count: int
    percentiles: Dict[float, Optional[float]]  # {50.0: ms, 95.0: ms, 99.0: ms}

    def p(self, pct: float) -> Optional[float]:
        return self.percentiles.get(pct)

    def age_seconds(self) -> float:
        return time.time() - self.captured_at


class BaselineWindowManager:
    """
    Captures and manages a baseline latency snapshot.
    The baseline represents expected performance (e.g., pre-deployment state).
    """

    def __init__(
        self,
        store: LatencySampleStore,
        baseline_window_seconds: float = 3600.0,
        percentiles_to_track: list = None,
    ):
        self._store = store
        self._baseline_window = baseline_window_seconds
        self._percentiles = percentiles_to_track or [50.0, 90.0, 95.0, 99.0, 99.9]
        self._baseline: Optional[LatencyBaseline] = None

    def capture_baseline(self, at_time: Optional[float] = None) -> LatencyBaseline:
        end = at_time or time.time()
        pcts = self._store.percentiles(self._percentiles, self._baseline_window, end)
        count = self._store.sample_count(self._baseline_window)
        baseline = LatencyBaseline(
            captured_at=end,
            window_seconds=self._baseline_window,
            sample_count=count,
            percentiles=pcts,
        )
        self._baseline = baseline
        return baseline

    def current_baseline(self) -> Optional[LatencyBaseline]:
        return self._baseline

    def reset_baseline(self) -> None:
        self._baseline = None
```

## Solution 3: P99 Regression Detector

```python
import time
from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass
class LatencyRegressionResult:
    detected: bool
    percentile: float
    baseline_ms: Optional[float]
    current_ms: Optional[float]
    change_pct: Optional[float]
    threshold_pct: float
    baseline_age_seconds: float
    current_sample_count: int

    @property
    def message(self) -> str:
        if not self.detected:
            return f"P{self.percentile:.0f} latency OK"
        return (
            f"P{self.percentile:.0f} latency regression: "
            f"{self.baseline_ms:.0f}ms → {self.current_ms:.0f}ms "
            f"(+{self.change_pct:.1f}%, threshold {self.threshold_pct:.0f}%)"
        )


class P99LatencyRegressionDetector:
    """
    Compares current latency percentiles against a captured baseline.
    Fires regression alerts when the relative increase exceeds the threshold.
    """

    def __init__(
        self,
        store: LatencySampleStore,
        baseline_manager: BaselineWindowManager,
        current_window_seconds: float = 300.0,
        regression_threshold_pct: float = 25.0,
        min_current_samples: int = 50,
        percentiles_to_check: list = None,
    ):
        self._store = store
        self._baseline_manager = baseline_manager
        self._current_window = current_window_seconds
        self._threshold = regression_threshold_pct
        self._min_samples = min_current_samples
        self._check_percentiles = percentiles_to_check or [95.0, 99.0, 99.9]

    def check(self) -> List[LatencyRegressionResult]:
        baseline = self._baseline_manager.current_baseline()
        results = []

        for pct in self._check_percentiles:
            current_ms = self._store.percentile(pct, self._current_window)
            current_count = self._store.sample_count(self._current_window)
            baseline_ms = baseline.p(pct) if baseline else None
            baseline_age = baseline.age_seconds() if baseline else 0.0

            if baseline_ms is None or current_ms is None or current_count < self._min_samples:
                results.append(LatencyRegressionResult(
                    detected=False,
                    percentile=pct,
                    baseline_ms=baseline_ms,
                    current_ms=current_ms,
                    change_pct=None,
                    threshold_pct=self._threshold,
                    baseline_age_seconds=baseline_age,
                    current_sample_count=current_count,
                ))
                continue

            change_pct = (current_ms - baseline_ms) / max(baseline_ms, 1) * 100
            detected = change_pct > self._threshold

            results.append(LatencyRegressionResult(
                detected=detected,
                percentile=pct,
                baseline_ms=baseline_ms,
                current_ms=current_ms,
                change_pct=round(change_pct, 1),
                threshold_pct=self._threshold,
                baseline_age_seconds=round(baseline_age, 0),
                current_sample_count=current_count,
            ))

        return results
```

## Solution 4: Regression Alert Manager

```python
import time
from typing import Callable, List, Optional


class LatencyRegressionAlertManager:
    """
    Fires alerts when regressions are detected, with cooldown suppression
    to prevent alert storms during sustained regressions.
    """

    def __init__(
        self,
        alert_fn: Optional[Callable[[dict], None]] = None,
        cooldown_seconds: float = 900.0,
    ):
        self._alert_fn = alert_fn or (lambda e: None)
        self._cooldown = cooldown_seconds
        self._last_fired: dict = {}
        self._fired_count = 0

    def process(self, results: List[LatencyRegressionResult]) -> List[LatencyRegressionResult]:
        fired = []
        now = time.time()

        for result in results:
            if not result.detected:
                continue
            key = f"p{result.percentile:.0f}"
            if now - self._last_fired.get(key, 0) >= self._cooldown:
                self._last_fired[key] = now
                self._fired_count += 1
                fired.append(result)
                self._alert_fn({
                    "ts": now,
                    "percentile": result.percentile,
                    "baseline_ms": result.baseline_ms,
                    "current_ms": result.current_ms,
                    "change_pct": result.change_pct,
                    "message": result.message,
                })

        return fired
```

## Solution 5: Deployment-Triggered Baseline Recapture

```python
import time
from typing import Optional


class DeploymentTriggeredBaselineRecapture:
    """
    Automatically captures a new baseline after a deployment or
    at a scheduled interval, ensuring comparisons are against
    recent known-good performance rather than stale historical data.
    """

    def __init__(
        self,
        baseline_manager: BaselineWindowManager,
        max_baseline_age_seconds: float = 86400.0,
        settle_seconds: float = 300.0,
    ):
        self._manager = baseline_manager
        self._max_age = max_baseline_age_seconds
        self._settle = settle_seconds
        self._deployment_at: Optional[float] = None

    def on_deployment(self) -> None:
        """Call this from your deployment pipeline."""
        self._deployment_at = time.time()

    def maybe_recapture(self) -> bool:
        baseline = self._manager.current_baseline()

        # Recapture if baseline is too old
        if baseline and baseline.age_seconds() > self._max_age:
            self._manager.capture_baseline()
            return True

        # Recapture if no baseline exists
        if baseline is None:
            self._manager.capture_baseline()
            return True

        # Recapture after deployment settle time
        if (
            self._deployment_at is not None
            and time.time() - self._deployment_at >= self._settle
        ):
            self._manager.capture_baseline()
            self._deployment_at = None
            return True

        return False
```

## Solution 6: P99 Latency Regression Dashboard

```python
import time
from typing import List


class P99LatencyRegressionDashboard:
    """
    Combines current latency percentiles, baseline comparison, and
    regression detection results into a single operational view.
    """

    def __init__(
        self,
        store: LatencySampleStore,
        detector: P99LatencyRegressionDetector,
        alert_manager: LatencyRegressionAlertManager,
        baseline_manager: BaselineWindowManager,
    ):
        self._store = store
        self._detector = detector
        self._alerts = alert_manager
        self._baseline_manager = baseline_manager

    def render(self) -> dict:
        results = self._detector.check()
        baseline = self._baseline_manager.current_baseline()
        current_pcts = self._store.percentiles(
            [50.0, 95.0, 99.0, 99.9], window_seconds=300.0
        )

        return {
            "generated_at": time.time(),
            "current_latency_ms": {
                f"p{int(k)}": v for k, v in current_pcts.items()
            },
            "baseline_captured_at": baseline.captured_at if baseline else None,
            "baseline_age_seconds": baseline.age_seconds() if baseline else None,
            "regression_checks": [
                {
                    "percentile": r.percentile,
                    "detected": r.detected,
                    "change_pct": r.change_pct,
                    "message": r.message,
                }
                for r in results
            ],
            "regressions_detected": sum(1 for r in results if r.detected),
            "total_alerts_fired": self._alerts._fired_count,
        }
```

## Comparison

| Approach | Percentile Tracking | Baseline Comparison | Regression Detection | Deployment Integration | Dashboard |
|---|---|---|---|---|---|
| LatencySampleStore | Yes (any percentile) | No | No | No | No |
| BaselineWindowManager | Via store | Yes (snapshot) | No | No | No |
| P99LatencyRegressionDetector | Via store | Via baseline | Yes (threshold) | No | No |
| LatencyRegressionAlertManager | No | No | Via detector | No | No |
| DeploymentTriggeredBaselineRecapture | No | Via manager | No | Yes | No |
| P99LatencyRegressionDashboard | No | No | No | No | Yes |

**Best for production**: Capture a new baseline immediately after every deployment succeeds — use `DeploymentTriggeredBaselineRecapture.on_deployment()` from your CI/CD pipeline. Wait 5 minutes (`settle_seconds=300`) before recapturing to allow initial traffic to normalize. Set `regression_threshold_pct=25` for P99 and `50` for P99.9 — tail latency is inherently noisier, so a higher threshold prevents false positives from statistical noise. Check P95 as a leading indicator: a P95 regression almost always precedes a P99 regression, giving earlier warning. Set `min_current_samples=100` to prevent triggering on the first few requests after deployment when the sample size is too small for reliable percentile estimates.
