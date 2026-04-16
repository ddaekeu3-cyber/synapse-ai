---
title: "Agent Doesn't Implement Error Budget Tracking for SLO Compliance"
description: "Agents that track only raw error rates miss the operational model that makes SLOs actionable: an error budget that quantifies how much unreliability is permitted before the SLO is breached, and burns down in real time as errors occur. Without error budget tracking, on-call engineers cannot answer 'how close are we to breaching?' or 'at this burn rate, when do we run out of budget?' Implement error budget calculation, burn rate alerting, and budget exhaustion forecasting."
date: 2026-04-16
difficulty: advanced
category: observability
slug: agent-doesnt-implement-error-budget-tracking-for-slo-compliance
tags: [slo, error-budget, burn-rate, reliability-engineering, sre, alerting]
symptoms:
  - "Team knows the error rate but cannot say how much budget remains for the month"
  - "Alerts fire on raw error rate thresholds that don't account for the SLO window"
  - "No early warning when burn rate is elevated — only notified after SLO is already breached"
  - "Error budget resets are missed — the monthly window rolled over but counters didn't"
  - "Cannot compare error budget consumption across different SLO dimensions (latency vs. errors)"
---

## Why This Happens

An SLO defines a target reliability level over a rolling window (e.g., 99.9% success rate over 30 days). The error budget is the complement: 0.1% of requests may fail before the SLO is breached. Without tracking events against this budget in real time, teams react to individual errors rather than to budget burn rate — the rate at which the budget is being consumed relative to the rate that would exhaust it by the window's end. A burn rate of 1× means exactly on track to use all the budget; a burn rate of 10× means the budget will be exhausted in 1/10 of the window. Multi-window burn rate alerting (short window for fast detection, long window to avoid false positives) is the standard pattern from Google SRE practice.

## Solution 1: SLO Definition

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class SLOType(str, Enum):
    AVAILABILITY = "availability"   # success / total
    LATENCY = "latency"             # requests under threshold / total
    THROUGHPUT = "throughput"       # requests processed / requests received


@dataclass
class SLODefinition:
    name: str
    slo_type: SLOType
    target_ratio: float             # e.g., 0.999 for 99.9%
    window_seconds: float = 2592000.0  # 30 days
    latency_threshold_ms: Optional[float] = None  # only for LATENCY type

    @property
    def error_budget_ratio(self) -> float:
        return round(1.0 - self.target_ratio, 6)

    def allowed_bad_events(self, total_events: int) -> float:
        return total_events * self.error_budget_ratio

    def __post_init__(self) -> None:
        if not 0 < self.target_ratio < 1:
            raise ValueError(f"target_ratio must be between 0 and 1, got {self.target_ratio}")
```

## Solution 2: Event Counter

```python
import time
from collections import deque
from threading import Lock
from typing import Deque, Tuple


class SLOEventCounter:
    """
    Thread-safe sliding window counter for SLO good/bad events.
    Supports arbitrary window sizes and sub-window queries.
    """

    def __init__(self, window_seconds: float):
        self._window = window_seconds
        self._good: Deque[float] = deque()
        self._bad: Deque[float] = deque()
        self._lock = Lock()

    def record_good(self, count: int = 1) -> None:
        now = time.time()
        with self._lock:
            for _ in range(count):
                self._good.append(now)
            self._evict(now)

    def record_bad(self, count: int = 1) -> None:
        now = time.time()
        with self._lock:
            for _ in range(count):
                self._bad.append(now)
            self._evict(now)

    def _evict(self, now: float) -> None:
        cutoff = now - self._window
        while self._good and self._good[0] < cutoff:
            self._good.popleft()
        while self._bad and self._bad[0] < cutoff:
            self._bad.popleft()

    def counts(self, sub_window_seconds: Optional[float] = None) -> Tuple[int, int]:
        """Returns (good_count, bad_count) within sub_window or full window."""
        now = time.time()
        cutoff = now - (sub_window_seconds or self._window)
        with self._lock:
            good = sum(1 for ts in self._good if ts >= cutoff)
            bad = sum(1 for ts in self._bad if ts >= cutoff)
            return good, bad

    def current_ratio(self, sub_window_seconds: Optional[float] = None) -> Optional[float]:
        good, bad = self.counts(sub_window_seconds)
        total = good + bad
        if total == 0:
            return None
        return round(good / total, 6)
```

## Solution 3: Error Budget Calculator

```python
import time
from typing import Optional


class ErrorBudgetCalculator:
    """
    Computes error budget remaining, burn rate, and exhaustion forecast
    from an SLO definition and a live event counter.
    """

    def __init__(self, slo: SLODefinition, counter: SLOEventCounter):
        self._slo = slo
        self._counter = counter

    def budget_remaining_ratio(self) -> Optional[float]:
        """Fraction of error budget remaining (0=exhausted, 1=fully intact)."""
        good, bad = self._counter.counts()
        total = good + bad
        if total == 0:
            return 1.0
        allowed_bad = total * self._slo.error_budget_ratio
        if allowed_bad <= 0:
            return 0.0
        remaining = max(0.0, allowed_bad - bad) / allowed_bad
        return round(remaining, 4)

    def burn_rate(self, window_seconds: float) -> Optional[float]:
        """
        Burn rate = actual error ratio / SLO error budget ratio.
        A burn rate of 1.0 means consuming budget at exactly the sustainable rate.
        A burn rate > 1.0 means budget will be exhausted before the window ends.
        """
        actual_ratio = self._counter.current_ratio(window_seconds)
        if actual_ratio is None:
            return None
        actual_error_ratio = 1.0 - actual_ratio
        if self._slo.error_budget_ratio == 0:
            return None
        return round(actual_error_ratio / self._slo.error_budget_ratio, 3)

    def exhaustion_forecast_seconds(self) -> Optional[float]:
        """
        Estimates seconds until error budget exhaustion at the current burn rate.
        Returns None if burn rate is <= 1 (not on track to exhaust).
        """
        br = self.burn_rate(self._slo.window_seconds)
        if br is None or br <= 1.0:
            return None
        remaining = self.budget_remaining_ratio()
        if remaining is None or remaining <= 0:
            return 0.0
        # At burn rate BR, budget exhausts in window * remaining / BR
        seconds = self._slo.window_seconds * remaining / br
        return round(seconds, 1)

    def snapshot(self) -> dict:
        good, bad = self._counter.counts()
        total = good + bad
        return {
            "slo_name": self._slo.name,
            "target_ratio": self._slo.target_ratio,
            "total_events": total,
            "bad_events": bad,
            "current_ratio": self._counter.current_ratio(),
            "budget_remaining_ratio": self.budget_remaining_ratio(),
            "burn_rate_1h": self.burn_rate(3600),
            "burn_rate_6h": self.burn_rate(21600),
            "exhaustion_forecast_seconds": self.exhaustion_forecast_seconds(),
        }
```

## Solution 4: Multi-Window Burn Rate Alerter

```python
import time
from dataclasses import dataclass
from typing import List, Optional


@dataclass
class BurnRateAlert:
    slo_name: str
    alert_name: str
    short_window_seconds: float
    long_window_seconds: float
    burn_rate_threshold: float
    triggered: bool = False
    short_burn_rate: Optional[float] = None
    long_burn_rate: Optional[float] = None
    triggered_at: Optional[float] = None


class MultiWindowBurnRateAlerter:
    """
    Implements the two-window burn rate alerting pattern from Google SRE.
    An alert fires when BOTH the short and long window burn rates exceed
    the threshold — short window ensures fast detection, long window
    suppresses false positives from brief spikes.
    """

    # Standard Google SRE alert tiers
    DEFAULT_ALERTS = [
        # (alert_name, short_window_h, long_window_h, burn_rate_threshold)
        ("page_critical", 1, 6, 14.4),    # exhausts 30d budget in 2h
        ("page_high", 6, 24, 6.0),        # exhausts 30d budget in 5d
        ("ticket_medium", 24, 72, 3.0),   # exhausts 30d budget in 10d
        ("ticket_low", 72, 168, 1.0),     # consuming at sustainable rate
    ]

    def __init__(self, calculator: ErrorBudgetCalculator):
        self._calc = calculator
        self._slo_name = calculator._slo.name
        self._alert_history: List[dict] = []

    def evaluate(self) -> List[BurnRateAlert]:
        alerts = []
        for alert_name, short_h, long_h, threshold in self.DEFAULT_ALERTS:
            short_br = self._calc.burn_rate(short_h * 3600)
            long_br = self._calc.burn_rate(long_h * 3600)
            triggered = (
                short_br is not None and long_br is not None
                and short_br >= threshold and long_br >= threshold
            )
            alert = BurnRateAlert(
                slo_name=self._slo_name,
                alert_name=alert_name,
                short_window_seconds=short_h * 3600,
                long_window_seconds=long_h * 3600,
                burn_rate_threshold=threshold,
                triggered=triggered,
                short_burn_rate=short_br,
                long_burn_rate=long_br,
                triggered_at=time.time() if triggered else None,
            )
            alerts.append(alert)
            if triggered:
                self._alert_history.append({
                    "ts": time.time(),
                    "alert_name": alert_name,
                    "short_burn_rate": short_br,
                    "long_burn_rate": long_br,
                })
        return alerts
```

## Solution 5: Error Budget Report

```python
import time
from typing import List


class ErrorBudgetReporter:
    """
    Produces structured error budget status reports for multiple SLOs.
    """

    def __init__(
        self,
        calculators: List[ErrorBudgetCalculator],
        alerter_map: Optional[dict] = None,
    ):
        self._calcs = calculators
        self._alerters = alerter_map or {}

    def report(self) -> dict:
        slo_reports = []
        for calc in self._calcs:
            snap = calc.snapshot()
            alerter = self._alerters.get(calc._slo.name)
            active_alerts = []
            if alerter:
                active_alerts = [
                    {"name": a.alert_name, "short_br": a.short_burn_rate, "long_br": a.long_burn_rate}
                    for a in alerter.evaluate()
                    if a.triggered
                ]
            slo_reports.append({**snap, "active_alerts": active_alerts})

        overall_healthy = all(
            (r["budget_remaining_ratio"] or 1.0) > 0.10
            for r in slo_reports
        )
        return {
            "generated_at": time.time(),
            "overall_healthy": overall_healthy,
            "slos": slo_reports,
        }
```

## Solution 6: Error Budget Dashboard

```python
import time


class ErrorBudgetDashboard:
    """
    Single-call render of error budget status with traffic-light summary.
    """

    def __init__(self, reporter: ErrorBudgetReporter):
        self._reporter = reporter

    def render(self) -> dict:
        report = self._reporter.report()
        slos = report["slos"]

        def traffic_light(remaining: Optional[float]) -> str:
            if remaining is None:
                return "unknown"
            if remaining > 0.50:
                return "green"
            if remaining > 0.10:
                return "yellow"
            return "red"

        return {
            "generated_at": time.time(),
            "overall_status": "healthy" if report["overall_healthy"] else "at_risk",
            "slo_summary": [
                {
                    "name": s["slo_name"],
                    "status": traffic_light(s.get("budget_remaining_ratio")),
                    "budget_remaining_pct": round((s.get("budget_remaining_ratio") or 0) * 100, 1),
                    "burn_rate_1h": s.get("burn_rate_1h"),
                    "active_alert_count": len(s.get("active_alerts", [])),
                }
                for s in slos
            ],
        }
```

## Comparison

| Approach | SLO Definition | Event Counting | Budget Math | Burn Rate Alerts | Dashboard |
|---|---|---|---|---|---|
| SLODefinition | Yes | No | No | No | No |
| SLOEventCounter | No | Yes (sliding window) | No | No | No |
| ErrorBudgetCalculator | Via SLO | Via counter | Yes | No | No |
| MultiWindowBurnRateAlerter | Via calculator | Via calculator | Via calculator | Yes (2-window) | No |
| ErrorBudgetReporter | Via calculators | Via calculators | Via calculators | Via alerters | No |
| ErrorBudgetDashboard | No | No | No | No | Yes |

**Best for production**: Define SLOs at the service boundary — one for availability (success/total), one for P99 latency (requests under threshold/total). Set the `page_critical` alert threshold to 14.4× burn rate (exhausts 30-day budget in 2 hours) and route it to PagerDuty. Set `ticket_medium` at 3× and route to a Slack channel for async review. Check `budget_remaining_ratio` in your deployment gate — if less than 10% of budget remains, block non-emergency deploys until the window resets. Monitor `exhaustion_forecast_seconds` in the dashboard: if it shows less than 48 hours, freeze feature work and investigate root cause immediately.
