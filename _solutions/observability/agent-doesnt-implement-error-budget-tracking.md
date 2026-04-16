---
title: "Agent Doesn't Implement Error Budget Tracking"
description: "Solutions for tracking the error budget consumed by an AI agent service — measuring how much reliability has been spent and automatically throttling risk-taking when the budget runs low."
tags: [observability, slo, error-budget, reliability, sre]
difficulty: intermediate
---

## Problem

Agents without error budget tracking keep running at full speed even as their SLO window fills with failures. There's no signal to slow risky operations, no mechanism to freeze deployments when reliability degrades, and no data to have informed conversations with stakeholders about acceptable failure rates vs feature velocity.

---

## Solution 1: Rolling Window Error Budget Calculator

Track success/failure over a rolling time window and compute remaining error budget as a percentage.

```python
import anthropic
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

client = anthropic.Anthropic()

@dataclass
class RequestRecord:
    timestamp: float
    success: bool
    latency_ms: int
    error_type: Optional[str] = None

@dataclass
class ErrorBudget:
    slo_target: float  # e.g., 0.99 = 99% success rate
    window_seconds: int  # rolling window
    _records: deque = field(default_factory=deque)

    def record(self, success: bool, latency_ms: int = 0, error_type: str = None):
        self._records.append(RequestRecord(
            timestamp=time.time(),
            success=success,
            latency_ms=latency_ms,
            error_type=error_type,
        ))

    def _evict_old(self):
        cutoff = time.time() - self.window_seconds
        while self._records and self._records[0].timestamp < cutoff:
            self._records.popleft()

    @property
    def stats(self) -> dict:
        self._evict_old()
        records = list(self._records)
        if not records:
            return {
                "total": 0, "successes": 0, "failures": 0,
                "success_rate": 1.0, "budget_consumed_pct": 0.0,
                "budget_remaining_pct": 100.0, "status": "ok",
            }

        total = len(records)
        successes = sum(1 for r in records if r.success)
        failures = total - successes
        success_rate = successes / total

        # Error budget: (1 - SLO) * total = allowed failures
        allowed_failures = (1 - self.slo_target) * total
        budget_consumed_pct = (failures / max(1, allowed_failures)) * 100
        budget_remaining_pct = max(0, 100 - budget_consumed_pct)

        if budget_remaining_pct <= 0:
            status = "exhausted"
        elif budget_remaining_pct <= 10:
            status = "critical"
        elif budget_remaining_pct <= 25:
            status = "warning"
        else:
            status = "ok"

        return {
            "total": total,
            "successes": successes,
            "failures": failures,
            "success_rate": round(success_rate, 4),
            "slo_target": self.slo_target,
            "allowed_failures": round(allowed_failures, 1),
            "budget_consumed_pct": round(budget_consumed_pct, 1),
            "budget_remaining_pct": round(budget_remaining_pct, 1),
            "status": status,
            "window_seconds": self.window_seconds,
        }

class BudgetAwareAgent:
    def __init__(self, slo_target: float = 0.99, window_hours: int = 1):
        self._budget = ErrorBudget(
            slo_target=slo_target,
            window_seconds=window_hours * 3600,
        )

    def respond(self, message: str) -> dict:
        budget_stats = self._budget.stats
        status = budget_stats.get("status", "ok")

        # Throttle on low budget
        if status == "exhausted":
            return {
                "response": "Service is in error budget protection mode. Only critical requests accepted.",
                "budget": budget_stats,
                "throttled": True,
            }

        model = (
            "claude-haiku-4-5-20251001" if status in ("critical", "warning")
            else "claude-sonnet-4-6"
        )

        t0 = time.time()
        try:
            response = client.messages.create(
                model=model, max_tokens=512,
                messages=[{"role": "user", "content": message}]
            )
            latency_ms = int((time.time() - t0) * 1000)
            self._budget.record(success=True, latency_ms=latency_ms)
            return {
                "response": response.content[0].text,
                "model": model,
                "budget": budget_stats,
                "throttled": False,
            }
        except Exception as e:
            latency_ms = int((time.time() - t0) * 1000)
            self._budget.record(success=False, latency_ms=latency_ms, error_type=type(e).__name__)
            raise

    def budget_report(self) -> dict:
        return self._budget.stats

# Simulate calls with some failures
agent = BudgetAwareAgent(slo_target=0.95, window_hours=1)

# Simulate 20 requests: 18 success, 2 failures
for i in range(18):
    agent._budget.record(success=True, latency_ms=200)
for _ in range(2):
    agent._budget.record(success=False, latency_ms=5000, error_type="TimeoutError")

report = agent.budget_report()
print(f"Error Budget Report:")
print(f"  SLO target: {report['slo_target']:.0%}")
print(f"  Success rate: {report['success_rate']:.2%}")
print(f"  Budget consumed: {report['budget_consumed_pct']:.1f}%")
print(f"  Budget remaining: {report['budget_remaining_pct']:.1f}%")
print(f"  Status: {report['status'].upper()}")

# Try a real request
result = agent.respond("What is machine learning?")
print(f"\nModel used: {result.get('model')} (throttled: {result.get('throttled')})")
```

---

## Solution 2: Multi-SLI Error Budget with Latency and Availability Tracking

Track multiple Service Level Indicators (SLIs): availability (success rate) AND latency (p95 under threshold). Both consume from the same budget.

```python
import anthropic
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

client = anthropic.Anthropic()

@dataclass
class SLIRecord:
    timestamp: float
    available: bool       # Did the request succeed?
    latency_ok: bool      # Was p95 latency under threshold?
    latency_ms: int

@dataclass
class MultiSLIBudget:
    availability_target: float = 0.99   # 99% availability
    latency_target_ms: int = 2000       # p95 < 2000ms
    latency_target_pct: float = 0.95   # 95th percentile requirement
    window_seconds: int = 3600
    _records: deque = field(default_factory=lambda: deque(maxlen=10000))

    def record(self, success: bool, latency_ms: int):
        self._records.append(SLIRecord(
            timestamp=time.time(),
            available=success,
            latency_ok=latency_ms <= self.latency_target_ms,
            latency_ms=latency_ms,
        ))

    def _current_records(self) -> list[SLIRecord]:
        cutoff = time.time() - self.window_seconds
        return [r for r in self._records if r.timestamp >= cutoff]

    def _p95_latency(self, records: list[SLIRecord]) -> float:
        latencies = sorted(r.latency_ms for r in records)
        if not latencies:
            return 0.0
        idx = int(len(latencies) * 0.95)
        return latencies[min(idx, len(latencies) - 1)]

    def budget_state(self) -> dict:
        records = self._current_records()
        if not records:
            return {"status": "no-data", "availability_budget_pct": 100, "latency_budget_pct": 100}

        total = len(records)

        # Availability SLI
        available = sum(1 for r in records if r.available)
        avail_rate = available / total
        avail_failures = total - available
        avail_allowed = (1 - self.availability_target) * total
        avail_budget_pct = max(0, 100 - (avail_failures / max(1, avail_allowed) * 100))

        # Latency SLI
        p95 = self._p95_latency(records)
        latency_violations = sum(1 for r in records if not r.latency_ok)
        latency_allowed = (1 - self.latency_target_pct) * total
        latency_budget_pct = max(0, 100 - (latency_violations / max(1, latency_allowed) * 100))

        # Overall budget = min of both
        overall_budget_pct = min(avail_budget_pct, latency_budget_pct)
        status = (
            "exhausted" if overall_budget_pct <= 0 else
            "critical" if overall_budget_pct <= 10 else
            "warning" if overall_budget_pct <= 25 else
            "ok"
        )

        return {
            "total_requests": total,
            "availability_rate": round(avail_rate, 4),
            "availability_budget_pct": round(avail_budget_pct, 1),
            "p95_latency_ms": round(p95, 0),
            "latency_budget_pct": round(latency_budget_pct, 1),
            "overall_budget_pct": round(overall_budget_pct, 1),
            "status": status,
        }

# Simulate a degraded period
budget = MultiSLIBudget(
    availability_target=0.99,
    latency_target_ms=1000,
    window_seconds=3600,
)

# Normal baseline: 50 requests, fast
for _ in range(50):
    budget.record(success=True, latency_ms=300)

# Degraded period: 10 slow requests + 2 failures
for _ in range(10):
    budget.record(success=True, latency_ms=3500)  # exceeds 1000ms threshold
for _ in range(2):
    budget.record(success=False, latency_ms=5000)

state = budget.budget_state()
print("Multi-SLI Error Budget:")
for k, v in state.items():
    print(f"  {k}: {v}")
```

---

## Solution 3: Error Budget Burn Rate Alerting

Alert when the error budget is burning faster than sustainable — catching incidents before the budget fully depletes.

```python
import anthropic
import time
import math
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Optional

client = anthropic.Anthropic()

ALERT_RULES = [
    # (short_window_hours, long_window_hours, burn_rate_multiplier, severity)
    # Burn 2% of budget in 1h AND 5% in 6h → page
    (1,  6,  14.4, "page"),   # 2%/1h OR 5%/6h above 99% SLO
    (6,  24, 6.0,  "ticket"),  # 10%/6h or 30%/24h
    (24, 72, 3.0,  "warning"),
]

@dataclass
class BurnRateAlert:
    severity: str
    burn_rate: float
    budget_consumed_pct: float
    time_to_exhaustion_hours: Optional[float]
    message: str

@dataclass
class BurnRateTracker:
    slo_target: float = 0.99
    _records: deque = field(default_factory=lambda: deque(maxlen=100000))

    def record(self, success: bool):
        self._records.append((time.time(), success))

    def _failure_rate_in_window(self, window_hours: float) -> float:
        cutoff = time.time() - window_hours * 3600
        recent = [(t, s) for t, s in self._records if t >= cutoff]
        if not recent:
            return 0.0
        failures = sum(1 for _, s in recent if not s)
        return failures / len(recent)

    def _burn_rate(self, window_hours: float) -> float:
        """Burn rate: how fast are we consuming budget relative to sustainable rate?"""
        failure_rate = self._failure_rate_in_window(window_hours)
        error_budget_rate = 1 - self.slo_target  # sustainable failure rate
        if error_budget_rate == 0:
            return float("inf") if failure_rate > 0 else 0
        return failure_rate / error_budget_rate

    def _budget_consumed_pct(self, window_hours: float = 720) -> float:
        """Budget consumed in the rolling window (default 30 days)."""
        failure_rate = self._failure_rate_in_window(window_hours)
        error_budget = 1 - self.slo_target
        return min(100, (failure_rate / max(error_budget, 0.0001)) * 100)

    def check_alerts(self) -> list[BurnRateAlert]:
        alerts = []
        consumed = self._budget_consumed_pct()

        for short_w, long_w, threshold, severity in ALERT_RULES:
            short_rate = self._burn_rate(short_w)
            long_rate = self._burn_rate(long_w)

            if short_rate >= threshold and long_rate >= threshold * 0.5:
                # Time to exhaustion at current rate
                remaining_budget = max(0, 100 - consumed) / 100
                tte = None
                if short_rate > 1:
                    hours_in_window = 30 * 24  # 30-day SLO window
                    tte = remaining_budget * hours_in_window / (short_rate - 1) if short_rate > 1 else None

                alerts.append(BurnRateAlert(
                    severity=severity,
                    burn_rate=round(short_rate, 2),
                    budget_consumed_pct=round(consumed, 1),
                    time_to_exhaustion_hours=round(tte, 1) if tte else None,
                    message=(
                        f"Burn rate {short_rate:.1f}x (threshold {threshold}x). "
                        f"Budget {consumed:.1f}% consumed. "
                        f"Time to exhaustion: {tte:.1f}h" if tte else f"Burn rate {short_rate:.1f}x"
                    ),
                ))
                break  # Report highest severity only

        return alerts

tracker = BurnRateTracker(slo_target=0.99)

# Normal period
for _ in range(100):
    tracker.record(success=True)

# Incident: 30% failure rate
for _ in range(300):
    tracker.record(success=random := True if (id:=0) == 0 else False)

import random
for _ in range(300):
    tracker.record(success=random.random() > 0.30)

alerts = tracker.check_alerts()
if alerts:
    for alert in alerts:
        print(f"[{alert.severity.upper()}] {alert.message}")
        print(f"  Burn rate: {alert.burn_rate}x | Budget consumed: {alert.budget_consumed_pct}%")
else:
    print("No alerts — error budget within healthy range")
```

---

## Solution 4: Error Budget Policy Enforcer — Automated Risk Controls

Automatically apply risk controls (freeze deploys, disable risky features, increase timeouts) when error budget thresholds are crossed.

```python
import anthropic
import time
from dataclasses import dataclass, field
from typing import Callable

client = anthropic.Anthropic()

@dataclass
class RiskControl:
    name: str
    activate_at_pct: float  # Activate when budget consumed >= this %
    deactivate_at_pct: float  # Deactivate when budget improves below this %
    action: str
    active: bool = False

RISK_CONTROLS = [
    RiskControl("use-cheaper-model",      50, 30, "switch_to_haiku"),
    RiskControl("disable-retries",        65, 45, "disable_retries"),
    RiskControl("reduce-max-tokens",      75, 55, "reduce_max_tokens"),
    RiskControl("freeze-new-features",    80, 65, "freeze_features"),
    RiskControl("drop-non-critical",      90, 75, "drop_non_critical"),
    RiskControl("read-only-mode",         95, 85, "read_only"),
]

class BudgetPolicyEnforcer:
    def __init__(self, slo_target: float = 0.99, window_seconds: int = 3600):
        self._slo = slo_target
        self._window = window_seconds
        self._records: list[tuple[float, bool]] = []
        self._controls = list(RISK_CONTROLS)
        self._policy_log: list[dict] = []

    def _consumed_pct(self) -> float:
        cutoff = time.time() - self._window
        recent = [(t, s) for t, s in self._records if t >= cutoff]
        if not recent:
            return 0.0
        failures = sum(1 for _, s in recent if not s)
        allowed = (1 - self._slo) * len(recent)
        return min(100, failures / max(1, allowed) * 100)

    def record(self, success: bool):
        self._records.append((time.time(), success))
        self._enforce_policies()

    def _enforce_policies(self):
        consumed = self._consumed_pct()
        for control in self._controls:
            if not control.active and consumed >= control.activate_at_pct:
                control.active = True
                entry = {
                    "time": time.time(), "control": control.name,
                    "action": "activated", "budget_consumed_pct": consumed,
                }
                self._policy_log.append(entry)
                print(f"[Policy] ACTIVATED: {control.name} (budget={consumed:.1f}%)")
            elif control.active and consumed < control.deactivate_at_pct:
                control.active = False
                entry = {
                    "time": time.time(), "control": control.name,
                    "action": "deactivated", "budget_consumed_pct": consumed,
                }
                self._policy_log.append(entry)
                print(f"[Policy] DEACTIVATED: {control.name} (budget={consumed:.1f}%)")

    def active_controls(self) -> list[str]:
        return [c.name for c in self._controls if c.active]

    def call_params(self, requested_model: str = "claude-sonnet-4-6", max_tokens: int = 1024) -> dict:
        params = {"model": requested_model, "max_tokens": max_tokens}
        active = self.active_controls()

        if "switch_to_haiku" in active:
            params["model"] = "claude-haiku-4-5-20251001"
        if "reduce_max_tokens" in active:
            params["max_tokens"] = min(params["max_tokens"], 256)
        return params

    def is_allowed(self, is_critical: bool = False) -> tuple[bool, str]:
        active = self.active_controls()
        if "read_only" in active:
            return False, "Read-only mode: write operations suspended"
        if "drop_non_critical" in active and not is_critical:
            return False, "Non-critical requests dropped due to budget pressure"
        return True, "OK"

# Simulate degradation
enforcer = BudgetPolicyEnforcer(slo_target=0.99, window_seconds=3600)

# Normal: 50 successes
for _ in range(50):
    enforcer.record(success=True)

print(f"Active controls: {enforcer.active_controls() or 'none'}")

# Degradation: 40% failure
for i in range(100):
    enforcer.record(success=i % 10 < 6)

print(f"\nActive controls after degradation: {enforcer.active_controls()}")

# Show what params would be used
params = enforcer.call_params("claude-sonnet-4-6", 1024)
print(f"Call params under policy: {params}")

allowed, reason = enforcer.is_allowed(is_critical=False)
print(f"Non-critical request allowed: {allowed} — {reason}")

allowed, reason = enforcer.is_allowed(is_critical=True)
print(f"Critical request allowed: {allowed} — {reason}")
```

---

## Solution 5: Per-Feature Error Budget Allocation

Allocate separate error budgets per agent feature or endpoint, preventing one high-traffic feature from consuming the entire budget.

```python
import anthropic
import time
from dataclasses import dataclass, field
from typing import Optional

client = anthropic.Anthropic()

@dataclass
class FeatureBudget:
    feature_name: str
    budget_allocation: float  # fraction of total budget allocated (0-1)
    slo_target: float
    window_seconds: int
    _records: list = field(default_factory=list)

    def record(self, success: bool):
        self._records.append((time.time(), success))

    def _current(self) -> list:
        cutoff = time.time() - self.window_seconds
        return [(t, s) for t, s in self._records if t >= cutoff]

    @property
    def consumed_pct(self) -> float:
        records = self._current()
        if not records:
            return 0.0
        failures = sum(1 for _, s in records if not s)
        allowed = (1 - self.slo_target) * len(records)
        return min(100, failures / max(1, allowed) * 100)

    @property
    def status(self) -> str:
        consumed = self.consumed_pct
        if consumed >= 100:
            return "exhausted"
        if consumed >= 75:
            return "critical"
        if consumed >= 50:
            return "warning"
        return "ok"

class FeatureBudgetRegistry:
    def __init__(self, slo_target: float = 0.99, window_seconds: int = 3600):
        self._slo = slo_target
        self._window = window_seconds
        self._features: dict[str, FeatureBudget] = {}

    def register(self, feature_name: str, budget_allocation: float = 0.25):
        self._features[feature_name] = FeatureBudget(
            feature_name=feature_name,
            budget_allocation=budget_allocation,
            slo_target=self._slo,
            window_seconds=self._window,
        )

    def record(self, feature: str, success: bool):
        if feature not in self._features:
            self.register(feature)
        self._features[feature].record(success)

    def can_proceed(self, feature: str) -> tuple[bool, str]:
        budget = self._features.get(feature)
        if budget is None:
            return True, "Untracked feature — allowed"
        if budget.status == "exhausted":
            return False, f"Feature {feature!r} budget exhausted ({budget.consumed_pct:.1f}%)"
        return True, f"Budget {budget.consumed_pct:.1f}% consumed"

    def dashboard(self) -> list[dict]:
        return [
            {
                "feature": b.feature_name,
                "status": b.status,
                "consumed_pct": round(b.consumed_pct, 1),
                "remaining_pct": round(max(0, 100 - b.consumed_pct), 1),
                "allocation": b.budget_allocation,
            }
            for b in sorted(self._features.values(), key=lambda b: -b.consumed_pct)
        ]

registry = FeatureBudgetRegistry(slo_target=0.99, window_seconds=3600)
registry.register("search",    budget_allocation=0.40)
registry.register("summarize", budget_allocation=0.30)
registry.register("chat",      budget_allocation=0.20)
registry.register("export",    budget_allocation=0.10)

# Simulate: search has failures, others are fine
for _ in range(80):
    registry.record("search", success=True)
for _ in range(20):
    registry.record("search", success=False)  # 20% failure rate

for _ in range(50):
    registry.record("summarize", success=True)
for _ in range(100):
    registry.record("chat", success=True)

print("=== Feature Error Budget Dashboard ===")
for row in registry.dashboard():
    bar = "█" * int(row['consumed_pct'] / 10) + "░" * (10 - int(row['consumed_pct'] / 10))
    print(f"  {row['feature']:12} [{bar}] {row['consumed_pct']:5.1f}% consumed ({row['status']})")

print("\nGate checks:")
for feature in ["search", "summarize", "chat"]:
    allowed, reason = registry.can_proceed(feature)
    print(f"  {feature}: {'✓' if allowed else '✗'} {reason}")
```

---

## Solution 6: Error Budget Report Generator for Stakeholder Communication

Auto-generate weekly error budget reports with trend analysis and recommendations for engineering and product teams.

```python
import anthropic
import json
import time
from dataclasses import dataclass
from typing import Optional

client = anthropic.Anthropic()

@dataclass
class WeeklyMetrics:
    week_label: str
    total_requests: int
    failure_count: int
    p95_latency_ms: float
    budget_consumed_pct: float
    incidents: list[str]
    deployments: int

REPORT_PROMPT = """You are an SRE analyst generating a weekly error budget report for engineering leadership.

SLO Target: {slo_target}% availability
Window: 30-day rolling

Weekly Metrics:
{weekly_data}

Current Budget Status:
- Budget consumed this month: {consumed_pct:.1f}%
- Budget remaining: {remaining_pct:.1f}%
- Burn rate trend: {trend}

Incidents this week: {incidents}
Deployments this week: {deployments}

Write a concise (3-4 paragraph) error budget report that includes:
1. Current budget health summary
2. Key failure patterns and root causes
3. Risk assessment for upcoming week
4. Specific recommendations (freeze features / invest in reliability / safe to deploy)

Use engineering-friendly language. Be direct about risks."""

def generate_error_budget_report(
    slo_target: float,
    weekly_data: list[WeeklyMetrics],
    current_consumed_pct: float,
) -> str:
    if len(weekly_data) >= 2:
        recent = weekly_data[-1].budget_consumed_pct
        prev = weekly_data[-2].budget_consumed_pct
        if recent > prev + 10:
            trend = f"accelerating (↑{recent - prev:.1f}%)"
        elif recent < prev - 10:
            trend = f"improving (↓{prev - recent:.1f}%)"
        else:
            trend = "stable"
    else:
        trend = "insufficient data"

    weekly_summary = "\n".join([
        f"Week {m.week_label}: {m.total_requests} requests, "
        f"{m.failure_count} failures ({m.failure_count/max(1,m.total_requests)*100:.1f}% error rate), "
        f"p95={m.p95_latency_ms:.0f}ms, budget consumed={m.budget_consumed_pct:.1f}%"
        for m in weekly_data
    ])

    latest = weekly_data[-1] if weekly_data else None
    incidents = latest.incidents if latest else []
    deployments = latest.deployments if latest else 0

    prompt = REPORT_PROMPT.format(
        slo_target=slo_target * 100,
        weekly_data=weekly_summary,
        consumed_pct=current_consumed_pct,
        remaining_pct=100 - current_consumed_pct,
        trend=trend,
        incidents=", ".join(incidents) if incidents else "None",
        deployments=deployments,
    )

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=800,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text

# Generate sample report
weekly_data = [
    WeeklyMetrics("W1", 50000, 150, 320.0, 30.0, [], 3),
    WeeklyMetrics("W2", 52000, 200, 340.0, 42.0, ["Search timeout 45min"], 4),
    WeeklyMetrics("W3", 48000, 600, 890.0, 78.0,
                  ["DB connection pool exhausted (2h)", "Model API 429 storm"], 2),
    WeeklyMetrics("W4", 51000, 900, 1200.0, 95.0,
                  ["Cascading timeout during deploy"], 1),
]

report = generate_error_budget_report(
    slo_target=0.99,
    weekly_data=weekly_data,
    current_consumed_pct=95.0,
)
print("=== Weekly Error Budget Report ===\n")
print(report)
```

---

## Comparison

| Solution | Granularity | Alerting | Automated Action | Stakeholder Ready | Best For |
|---|---|---|---|---|---|
| Rolling Window Calculator | Service-level | No | No | Partial | Dashboard metric |
| Multi-SLI Budget | Availability + latency | No | No | Partial | Comprehensive SLO |
| Burn Rate Alerting | Incident detection | Yes | No | No | On-call paging |
| Policy Enforcer | Automated risk gates | Yes | Yes | No | Production safety |
| Per-Feature Allocation | Feature-level | Partial | Partial | No | Multi-feature services |
| Report Generator | Weekly trend | No | No | Yes | Leadership communication |

**Recommended stack:** Deploy Solutions 1 + 3 (rolling window + burn rate alerts) as always-on instrumentation, Solution 4 (policy enforcer) to automate risk controls, and Solution 6 (report generator) for weekly stakeholder communication.
