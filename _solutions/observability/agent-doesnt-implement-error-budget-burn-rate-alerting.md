---
title: "Agent Doesn't Implement Error Budget Burn Rate Alerting"
description: "Agents that only alert on instantaneous error rate miss slow-burn reliability degradation: a 2% error rate sustained for 12 hours exhausts a monthly error budget before anyone notices. Implement error budget burn rate alerting that tracks budget consumption velocity, fires multi-window alerts when burn rate predicts budget exhaustion, and reports time-to-exhaustion for on-call triage."
date: 2026-04-16
difficulty: advanced
category: observability
slug: agent-doesnt-implement-error-budget-burn-rate-alerting
tags: [error-budget, burn-rate, slo-alerting, multi-window-alert, budget-exhaustion, sre-practices]
symptoms:
  - "Alert fires only when error rate exceeds a fixed threshold — misses slow burns"
  - "Error budget exhausted by end of month with no prior warning"
  - "No metric showing how fast the error budget is being consumed"
  - "On-call receives no alert for sustained 2% error rate that burns budget over 12 hours"
  - "Cannot tell how many hours remain before the SLO period's budget is gone"
---

## Why This Happens

Traditional threshold alerts fire when the current error rate exceeds a fixed percentage (e.g., >5%). This misses two failure modes: (1) a sustained low error rate that is below the threshold but consumes the budget over hours; (2) a short spike that would exhaust the remaining budget even if the current rate is now low. Error budget burn rate alerting compares the actual budget consumption rate to the rate that would exhaust the budget in a given time window, and fires when consumption velocity is dangerously high — not just when the instantaneous rate is high.

## Solution 1: Error Budget Definition

```python
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class SLODefinition:
    name: str
    target_availability: float      # e.g., 0.999 for 99.9%
    window_days: int = 30
    total_requests_estimate: Optional[int] = None  # for capacity planning

    @property
    def error_budget_fraction(self) -> float:
        return 1.0 - self.target_availability

    @property
    def window_seconds(self) -> float:
        return self.window_days * 86400.0

    @property
    def budget_minutes(self) -> float:
        """Minutes of downtime allowed in the window."""
        return self.window_seconds * self.error_budget_fraction / 60.0


@dataclass
class BurnRateThreshold:
    """
    Defines a burn rate alert. If burn_rate_multiplier = 14.4 over
    a 1-hour window, the budget will be exhausted in 30/14.4 ≈ 2 days.
    """
    name: str
    burn_rate_multiplier: float     # how many times faster than allowed burn rate
    short_window_seconds: float     # short window for sensitivity
    long_window_seconds: float      # long window for specificity
    severity: str = "warning"       # "warning" | "critical"


DEFAULT_BURN_RATE_THRESHOLDS = [
    BurnRateThreshold(
        name="critical_fast_burn",
        burn_rate_multiplier=14.4,  # exhausts 30-day budget in 2 days
        short_window_seconds=3600,   # 1h
        long_window_seconds=300,     # 5min
        severity="critical",
    ),
    BurnRateThreshold(
        name="warning_slow_burn",
        burn_rate_multiplier=6.0,   # exhausts 30-day budget in 5 days
        short_window_seconds=21600,  # 6h
        long_window_seconds=3600,    # 1h
        severity="warning",
    ),
]
```

## Solution 2: Error Budget Tracker

```python
import time
from collections import deque
from threading import Lock
from typing import Deque, Optional, Tuple


class ErrorBudgetTracker:
    """
    Tracks request outcomes (success/failure) in a sliding window and
    computes error rate, budget consumed, and burn rate.
    """

    def __init__(self, slo: SLODefinition, max_samples: int = 100_000):
        self._slo = slo
        self._samples: Deque[Tuple[float, bool]] = deque(maxlen=max_samples)
        # (timestamp, is_error)
        self._lock = Lock()

    def record(self, is_error: bool) -> None:
        with self._lock:
            self._samples.append((time.time(), is_error))

    def _window_stats(self, window_seconds: float) -> Tuple[int, int]:
        """Returns (total_requests, error_count) within window."""
        cutoff = time.time() - window_seconds
        with self._lock:
            recent = [(ts, err) for ts, err in self._samples if ts >= cutoff]
        total = len(recent)
        errors = sum(1 for _, err in recent if err)
        return total, errors

    def error_rate(self, window_seconds: float) -> float:
        total, errors = self._window_stats(window_seconds)
        if total == 0:
            return 0.0
        return errors / total

    def burn_rate(self, window_seconds: float) -> float:
        """
        Burn rate = actual_error_rate / allowed_error_rate.
        A burn rate of 1.0 means consuming budget at exactly the SLO-allowed rate.
        A burn rate of 14.4 means consuming 14.4× faster.
        """
        actual = self.error_rate(window_seconds)
        allowed = self._slo.error_budget_fraction
        if allowed == 0:
            return float("inf")
        return actual / allowed

    def budget_consumed_fraction(self) -> float:
        """How much of the SLO window's budget has been used so far."""
        total, errors = self._window_stats(self._slo.window_seconds)
        if total == 0:
            return 0.0
        budget_errors = total * self._slo.error_budget_fraction
        return min(1.0, errors / max(budget_errors, 1))

    def time_to_exhaustion_hours(self) -> Optional[float]:
        """Projected hours until budget is fully consumed at current burn rate."""
        burn = self.burn_rate(3600.0)
        if burn <= 0:
            return None
        remaining_fraction = 1.0 - self.budget_consumed_fraction()
        if remaining_fraction <= 0:
            return 0.0
        # hours_until_exhaustion = (remaining_budget / burn_rate) * window_hours
        window_hours = self._slo.window_seconds / 3600.0
        return round((remaining_fraction / burn) * window_hours, 1)
```

## Solution 3: Multi-Window Burn Rate Alert Evaluator

```python
import time
from typing import List, Optional


class BurnRateAlert:
    def __init__(
        self,
        slo_name: str,
        threshold: BurnRateThreshold,
        short_burn_rate: float,
        long_burn_rate: float,
        time_to_exhaustion_hours: Optional[float],
    ):
        self.slo_name = slo_name
        self.threshold = threshold
        self.short_burn_rate = short_burn_rate
        self.long_burn_rate = long_burn_rate
        self.time_to_exhaustion_hours = time_to_exhaustion_hours
        self.timestamp = time.time()

    @property
    def message(self) -> str:
        eta = (
            f"{self.time_to_exhaustion_hours:.1f}h"
            if self.time_to_exhaustion_hours is not None
            else "unknown"
        )
        return (
            f"[{self.threshold.severity.upper()}] SLO '{self.slo_name}': "
            f"burn rate {self.short_burn_rate:.1f}× (threshold {self.threshold.burn_rate_multiplier}×). "
            f"Budget exhaustion in ~{eta}."
        )


class MultiWindowBurnRateEvaluator:
    """
    Evaluates burn rate thresholds using multi-window confirmation:
    both the short AND long window must exceed the threshold to fire.
    This reduces false positives from transient spikes.
    """

    def __init__(self, tracker: ErrorBudgetTracker, slo: SLODefinition):
        self._tracker = tracker
        self._slo = slo

    def evaluate(
        self,
        thresholds: List[BurnRateThreshold],
    ) -> List[BurnRateAlert]:
        alerts = []
        eta = self._tracker.time_to_exhaustion_hours()

        for threshold in thresholds:
            short_rate = self._tracker.burn_rate(threshold.short_window_seconds)
            long_rate = self._tracker.burn_rate(threshold.long_window_seconds)

            # Both windows must exceed threshold (multi-window confirmation)
            if (
                short_rate >= threshold.burn_rate_multiplier
                and long_rate >= threshold.burn_rate_multiplier
            ):
                alerts.append(BurnRateAlert(
                    slo_name=self._slo.name,
                    threshold=threshold,
                    short_burn_rate=round(short_rate, 2),
                    long_burn_rate=round(long_rate, 2),
                    time_to_exhaustion_hours=eta,
                ))

        return alerts
```

## Solution 4: Alert Suppression Manager

```python
import time
from typing import Dict, List, Optional


class BurnRateAlertSuppressionManager:
    """
    Prevents alert storms by suppressing re-alerts for the same threshold
    within a cooldown window. Tracks alert history for trend analysis.
    """

    def __init__(self, cooldown_seconds: float = 3600.0):
        self._cooldown = cooldown_seconds
        self._last_fired: Dict[str, float] = {}
        self._history: List[BurnRateAlert] = []
        self._alert_fn = None

    def set_alert_fn(self, fn) -> None:
        self._alert_fn = fn

    def process(self, alerts: List[BurnRateAlert]) -> List[BurnRateAlert]:
        fired = []
        now = time.time()
        for alert in alerts:
            key = f"{alert.slo_name}:{alert.threshold.name}"
            last = self._last_fired.get(key, 0)
            if now - last >= self._cooldown:
                self._last_fired[key] = now
                self._history.append(alert)
                fired.append(alert)
                if self._alert_fn:
                    self._alert_fn(alert)
        return fired

    def recent_alerts(self, window_seconds: float = 86400.0) -> List[BurnRateAlert]:
        cutoff = time.time() - window_seconds
        return [a for a in self._history if a.timestamp >= cutoff]
```

## Solution 5: Budget Status Reporter

```python
import time
from typing import List


class ErrorBudgetStatusReporter:
    """
    Generates a human-readable error budget status report
    suitable for on-call dashboards and weekly reviews.
    """

    def __init__(self, tracker: ErrorBudgetTracker, slo: SLODefinition):
        self._tracker = tracker
        self._slo = slo

    def report(self) -> dict:
        consumed = self._tracker.budget_consumed_fraction()
        remaining = 1.0 - consumed
        burn_1h = self._tracker.burn_rate(3600.0)
        burn_6h = self._tracker.burn_rate(21600.0)
        burn_24h = self._tracker.burn_rate(86400.0)
        eta = self._tracker.time_to_exhaustion_hours()

        return {
            "slo_name": self._slo.name,
            "target": f"{self._slo.target_availability * 100:.3f}%",
            "window_days": self._slo.window_days,
            "budget_consumed_pct": round(consumed * 100, 2),
            "budget_remaining_pct": round(remaining * 100, 2),
            "burn_rate_1h": round(burn_1h, 2),
            "burn_rate_6h": round(burn_6h, 2),
            "burn_rate_24h": round(burn_24h, 2),
            "time_to_exhaustion_hours": eta,
            "status": (
                "critical" if consumed > 0.90
                else "warning" if consumed > 0.50
                else "healthy"
            ),
        }
```

## Solution 6: Error Budget Burn Rate Dashboard

```python
import time
from typing import List


class ErrorBudgetBurnRateDashboard:
    """
    Combines budget status, current burn rates, and alert history
    into a single operational view for on-call engineers.
    """

    def __init__(
        self,
        reporter: ErrorBudgetStatusReporter,
        evaluator: MultiWindowBurnRateEvaluator,
        suppression_manager: BurnRateAlertSuppressionManager,
        thresholds: List[BurnRateThreshold],
    ):
        self._reporter = reporter
        self._evaluator = evaluator
        self._suppressor = suppression_manager
        self._thresholds = thresholds

    def render(self) -> dict:
        status = self._reporter.report()
        active_alerts = self._evaluator.evaluate(self._thresholds)
        recent = self._suppressor.recent_alerts(86400.0)

        return {
            "generated_at": time.time(),
            "budget_status": status,
            "active_alerts": [
                {"severity": a.threshold.severity, "message": a.message}
                for a in active_alerts
            ],
            "alerts_last_24h": len(recent),
            "last_alert_message": recent[-1].message if recent else None,
        }
```

## Comparison

| Approach | Budget Tracking | Burn Rate Calculation | Multi-Window Confirmation | Alert Suppression | Dashboard |
|---|---|---|---|---|---|
| ErrorBudgetTracker | Yes | Yes (per window) | No | No | No |
| MultiWindowBurnRateEvaluator | Via tracker | Via tracker | Yes | No | No |
| BurnRateAlertSuppressionManager | No | No | No | Yes (cooldown) | No |
| ErrorBudgetStatusReporter | Via tracker | Via tracker | No | No | No |
| ErrorBudgetBurnRateDashboard | No | No | No | No | Yes |

**Best for production**: Implement both the 14.4× fast-burn (1h window) and 6× slow-burn (6h window) thresholds from Google's SRE workbook — these catch both incident-level spikes and insidious slow burns. Use a 1-hour cooldown on alert suppression to prevent storm suppression from hiding a sustained incident. Compute `time_to_exhaustion_hours` at alert time and include it in the PagerDuty/Slack message — "budget exhausted in 4 hours" is far more actionable than "burn rate 14×". Reset `_last_fired` for a threshold when the burn rate drops back below 1× so the next incident fires immediately rather than waiting for the cooldown.
