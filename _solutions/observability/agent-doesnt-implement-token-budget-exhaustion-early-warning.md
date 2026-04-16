---
title: "Agent Doesn't Implement Token Budget Exhaustion Early Warning"
description: "Agents operating under a fixed token budget — a monthly API spend limit, a per-request token cap, or a session context window — have no mechanism to warn operators before the budget is exhausted. The first signal is a hard failure: the API returns a quota error or a context overflow. Implement token budget exhaustion early warning that tracks consumption rate, projects time-to-exhaustion, and emits graduated alerts at configurable threshold percentages."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-token-budget-exhaustion-early-warning
tags: [token-budget, exhaustion-warning, quota-monitoring, rate-projection, early-warning, spend-alert]
symptoms:
  - "Monthly API token quota exhausted with no warning — agent goes dark at end of billing cycle"
  - "Context window overflow discovered at call time — no preemptive alert fired"
  - "No visibility into current token consumption rate or projected exhaustion time"
  - "Budget alerts configured only for 100% — no graduated warning thresholds"
  - "Cannot distinguish between normal consumption rate and an anomalous spike"
---

## Why This Happens

Token budgets are consumed continuously but measured only at billing boundaries or request failures. Without a consumption-rate tracker, the agent has no basis for projecting when a budget will run out. A budget that refills monthly but is consumed in the first week due to a traffic spike or a runaway loop goes undetected until the quota error arrives. Early warning requires three components: a consumption accumulator (how many tokens have been used), a rate estimator (how fast they are being consumed), and a projector (when will the budget run out at this rate) — with alerts at graduated thresholds before the hard limit is reached.

## Solution 1: Budget Scope Definition

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
import time


class BudgetPeriod(str, Enum):
    SESSION = "session"        # per user session
    DAILY = "daily"            # resets at midnight UTC
    MONTHLY = "monthly"        # resets at start of billing month
    TOTAL = "total"            # does not reset — lifetime cap


@dataclass
class TokenBudget:
    budget_id: str
    total_tokens: int                   # hard limit
    period: BudgetPeriod
    period_start_ts: float = field(default_factory=time.time)
    warn_thresholds_pct: list = field(default_factory=lambda: [50.0, 75.0, 90.0, 95.0])
    # Alert fires when consumption crosses each threshold

    @property
    def period_end_ts(self) -> Optional[float]:
        if self.period == BudgetPeriod.DAILY:
            import datetime
            start = datetime.datetime.utcfromtimestamp(self.period_start_ts)
            end = (start + datetime.timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            return end.timestamp()
        if self.period == BudgetPeriod.MONTHLY:
            import datetime
            start = datetime.datetime.utcfromtimestamp(self.period_start_ts)
            if start.month == 12:
                end = start.replace(year=start.year + 1, month=1, day=1,
                                    hour=0, minute=0, second=0, microsecond=0)
            else:
                end = start.replace(month=start.month + 1, day=1,
                                    hour=0, minute=0, second=0, microsecond=0)
            return end.timestamp()
        return None
```

## Solution 2: Token Budget Accumulator

```python
import time
from collections import deque
from threading import Lock
from typing import Deque, Optional, Tuple


class TokenBudgetAccumulator:
    """
    Tracks cumulative token consumption against a budget.
    Maintains a sliding window of recent usage for rate estimation.
    """

    def __init__(
        self,
        budget: TokenBudget,
        rate_window_seconds: float = 300.0,   # 5-minute window for rate calc
    ):
        self._budget = budget
        self._rate_window = rate_window_seconds
        self._consumed: int = 0
        self._usage_events: Deque[Tuple[float, int]] = deque()  # (ts, tokens)
        self._lock = Lock()

    def record(self, tokens: int) -> None:
        with self._lock:
            now = time.time()
            self._consumed += tokens
            self._usage_events.append((now, tokens))
            # Evict events outside the rate window
            cutoff = now - self._rate_window
            while self._usage_events and self._usage_events[0][0] < cutoff:
                self._usage_events.popleft()

    def consumed(self) -> int:
        with self._lock:
            return self._consumed

    def remaining(self) -> int:
        return max(0, self._budget.total_tokens - self.consumed())

    def utilization_pct(self) -> float:
        return round(self.consumed() / max(self._budget.total_tokens, 1) * 100, 3)

    def rate_tokens_per_second(self) -> float:
        with self._lock:
            if len(self._usage_events) < 2:
                return 0.0
            window_tokens = sum(t for _, t in self._usage_events)
            window_span = self._usage_events[-1][0] - self._usage_events[0][0]
            if window_span <= 0:
                return 0.0
            return round(window_tokens / window_span, 4)
```

## Solution 3: Exhaustion Projector

```python
import time
from typing import Optional


class ExhaustionProjector:
    """
    Projects when the token budget will be exhausted based on
    current consumption rate. Also accounts for period resets.
    """

    def __init__(self, accumulator: TokenBudgetAccumulator):
        self._acc = accumulator

    def project(self) -> dict:
        rate = self._acc.rate_tokens_per_second()
        remaining = self._acc.remaining()
        budget = self._acc._budget
        now = time.time()

        if rate <= 0 or remaining <= 0:
            return {
                "remaining_tokens": remaining,
                "rate_tokens_per_second": rate,
                "seconds_to_exhaustion": None,
                "exhaustion_ts": None,
                "exhausted": remaining <= 0,
            }

        seconds_to_exhaustion = remaining / rate
        exhaustion_ts = now + seconds_to_exhaustion

        # If the budget resets before exhaustion, we're fine
        period_end = budget.period_end_ts
        exhausted_before_reset = (
            period_end is None or exhaustion_ts < period_end
        )

        return {
            "remaining_tokens": remaining,
            "rate_tokens_per_second": round(rate, 2),
            "seconds_to_exhaustion": round(seconds_to_exhaustion, 1),
            "exhaustion_ts": round(exhaustion_ts, 1),
            "exhausted_before_reset": exhausted_before_reset,
            "period_end_ts": period_end,
        }
```

## Solution 4: Graduated Threshold Alerter

```python
import time
from typing import Callable, List, Optional, Set


class GraduatedThresholdAlerter:
    """
    Fires alerts when token budget utilization crosses configured thresholds.
    Each threshold fires at most once per budget period to avoid alert storms.
    """

    def __init__(
        self,
        accumulator: TokenBudgetAccumulator,
        projector: ExhaustionProjector,
        alert_fn: Optional[Callable[[dict], None]] = None,
    ):
        self._acc = accumulator
        self._proj = projector
        self._alert_fn = alert_fn or self._default_alert
        self._fired_thresholds: Set[float] = set()
        self._alert_history: List[dict] = []

    @staticmethod
    def _default_alert(alert: dict) -> None:
        import json
        print(json.dumps(alert))

    def check_and_alert(self) -> List[dict]:
        utilization = self._acc.utilization_pct()
        budget = self._acc._budget
        new_alerts = []

        for threshold in sorted(budget.warn_thresholds_pct):
            if utilization >= threshold and threshold not in self._fired_thresholds:
                self._fired_thresholds.add(threshold)
                projection = self._proj.project()
                alert = {
                    "ts": time.time(),
                    "alert_type": "token_budget_threshold",
                    "budget_id": budget.budget_id,
                    "threshold_pct": threshold,
                    "current_utilization_pct": utilization,
                    "remaining_tokens": self._acc.remaining(),
                    "seconds_to_exhaustion": projection.get("seconds_to_exhaustion"),
                    "severity": "critical" if threshold >= 90 else "warning",
                }
                self._alert_fn(alert)
                self._alert_history.append(alert)
                new_alerts.append(alert)

        return new_alerts

    def reset_for_new_period(self) -> None:
        """Call when the budget period resets."""
        self._fired_thresholds.clear()
```

## Solution 5: Budget Consumption Rate Monitor

```python
import time
from typing import List


class BudgetConsumptionRateMonitor:
    """
    Detects anomalous consumption rate spikes by comparing the current
    rate against a rolling baseline. Alerts when the rate is N× the baseline.
    """

    def __init__(
        self,
        accumulator: TokenBudgetAccumulator,
        spike_multiplier: float = 5.0,
        baseline_samples: int = 12,          # number of rate samples for baseline
    ):
        self._acc = accumulator
        self._spike_mult = spike_multiplier
        self._baseline_samples = baseline_samples
        self._rate_history: List[float] = []

    def sample_and_check(self) -> dict:
        current_rate = self._acc.rate_tokens_per_second()
        self._rate_history.append(current_rate)
        if len(self._rate_history) > self._baseline_samples:
            self._rate_history.pop(0)

        if len(self._rate_history) < 3:
            return {"status": "collecting_baseline", "current_rate": current_rate}

        baseline = sum(self._rate_history[:-1]) / len(self._rate_history[:-1])
        is_spike = baseline > 0 and current_rate > baseline * self._spike_mult

        return {
            "status": "spike" if is_spike else "normal",
            "current_rate_tps": round(current_rate, 3),
            "baseline_rate_tps": round(baseline, 3),
            "spike_multiplier": self._spike_mult,
            "is_spike": is_spike,
        }
```

## Solution 6: Token Budget Dashboard

```python
import time


class TokenBudgetExhaustionDashboard:
    """
    Combines utilization, projection, alert history, and rate anomaly
    detection into a single budget health view.
    """

    def __init__(
        self,
        accumulator: TokenBudgetAccumulator,
        projector: ExhaustionProjector,
        alerter: GraduatedThresholdAlerter,
        rate_monitor: BudgetConsumptionRateMonitor,
    ):
        self._acc = accumulator
        self._proj = projector
        self._alerter = alerter
        self._monitor = rate_monitor

    def render(self) -> dict:
        budget = self._acc._budget
        return {
            "generated_at": time.time(),
            "budget": {
                "id": budget.budget_id,
                "total_tokens": budget.total_tokens,
                "period": budget.period.value,
                "period_end_ts": budget.period_end_ts,
            },
            "consumption": {
                "consumed_tokens": self._acc.consumed(),
                "remaining_tokens": self._acc.remaining(),
                "utilization_pct": self._acc.utilization_pct(),
            },
            "projection": self._proj.project(),
            "rate_anomaly": self._monitor.sample_and_check(),
            "alerts_fired": len(self._alerter._alert_history),
            "thresholds_fired": sorted(self._alerter._fired_thresholds),
        }
```

## Comparison

| Approach | Consumption Tracking | Rate Estimation | Exhaustion Projection | Graduated Alerts | Spike Detection |
|---|---|---|---|---|---|
| TokenBudgetAccumulator | Yes | Yes (sliding window) | No | No | No |
| ExhaustionProjector | No | Via accumulator | Yes | No | No |
| GraduatedThresholdAlerter | Via accumulator | No | Via projector | Yes | No |
| BudgetConsumptionRateMonitor | No | Via accumulator | No | No | Yes |
| TokenBudgetExhaustionDashboard | No | No | No | No | No |

**Best for production**: Configure `warn_thresholds_pct=[50, 75, 90, 95]` — the 50% alert is informational (normal operations), 75% is a planning signal (consider throttling), 90% is an operational alert (reduce traffic), and 95% is critical (imminent exhaustion). Set `rate_window_seconds=300` for the accumulator to smooth out short bursts while still detecting sustained anomalies. Wire `alert_fn` to your on-call notification system rather than stdout — token budget exhaustion that goes unnoticed until 100% means the agent has been completely dark for some period before anyone responds. Call `reset_for_new_period()` at period boundaries to allow thresholds to re-fire in the new period; without this, a 90% alert that fired last month will never fire again even if this month's consumption is equally alarming.
