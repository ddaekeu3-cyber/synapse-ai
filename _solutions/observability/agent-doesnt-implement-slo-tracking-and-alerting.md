---
layout: solution
title: "Agent Doesn't Implement SLO Tracking and Alerting"
category: observability
description: "Agent has no defined service level objectives for latency, error rate, or success rate, so reliability degradation goes undetected until users complain."
tags: [observability, slo, alerting, reliability, monitoring]
---

# Agent Doesn't Implement SLO Tracking and Alerting

## Problem

Without Service Level Objectives (SLOs), there is no objective definition of "working correctly." An agent that takes 30 seconds to respond 20% of the time is technically functioning but failing its users — and no one knows. SLO tracking measures p50/p95/p99 latency, error rate, and task success rate against defined targets, and fires alerts when those targets are breached. This turns vague reliability intuitions into measurable engineering commitments.

## Solution Options

### Option 1: Latency SLO with Percentile Tracking

```python
import anthropic
import json
import statistics
import time
from collections import deque
from dataclasses import dataclass, field

client = anthropic.Anthropic()

@dataclass
class LatencySLO:
    """Tracks response latency against p50/p95/p99 SLO targets."""
    p50_target_ms: float
    p95_target_ms: float
    p99_target_ms: float
    window_size: int = 100  # Rolling window of N observations

    _samples: deque = field(default_factory=deque)
    _violations: list[dict] = field(default_factory=list)

    def record(self, latency_ms: float, request_id: str = ""):
        if len(self._samples) >= self.window_size:
            self._samples.popleft()
        self._samples.append(latency_ms)
        self._check_slo(latency_ms, request_id)

    def _check_slo(self, latency_ms: float, request_id: str):
        if latency_ms > self.p99_target_ms:
            self._violations.append({
                "ts": time.time(), "request_id": request_id,
                "latency_ms": latency_ms, "violated": "p99",
                "target_ms": self.p99_target_ms,
            })
            print(f"[SLO VIOLATION] p99 breach: {latency_ms:.0f}ms > {self.p99_target_ms}ms")

    def percentile(self, pct: float) -> float:
        if not self._samples:
            return 0.0
        sorted_samples = sorted(self._samples)
        idx = int(len(sorted_samples) * pct / 100)
        return sorted_samples[min(idx, len(sorted_samples) - 1)]

    def report(self) -> dict:
        if not self._samples:
            return {"status": "no_data"}

        p50 = self.percentile(50)
        p95 = self.percentile(95)
        p99 = self.percentile(99)

        return {
            "samples": len(self._samples),
            "p50_ms": round(p50, 1),
            "p95_ms": round(p95, 1),
            "p99_ms": round(p99, 1),
            "p50_ok": p50 <= self.p50_target_ms,
            "p95_ok": p95 <= self.p95_target_ms,
            "p99_ok": p99 <= self.p99_target_ms,
            "violations": len(self._violations),
            "slo_status": "GREEN" if p95 <= self.p95_target_ms else "RED",
        }

# SLO targets: p50 < 2s, p95 < 4s, p99 < 8s
slo = LatencySLO(p50_target_ms=2000, p95_target_ms=4000, p99_target_ms=8000)

def slo_tracked_call(user_message: str, req_id: str) -> str:
    t0 = time.monotonic()
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{"role": "user", "content": user_message}],
    )
    latency_ms = (time.monotonic() - t0) * 1000
    slo.record(latency_ms, req_id)
    print(f"[{req_id}] {latency_ms:.0f}ms")
    return response.content[0].text

# Simulate a series of requests
for i in range(8):
    slo_tracked_call(f"Brief answer to question {i}.", f"req_{i:03d}")

report = slo.report()
print(f"\n=== Latency SLO Report ===")
print(json.dumps(report, indent=2))

# Expected Token Savings: None — SLO tracking is observability-only
# Environment: Production agents with latency SLAs to customers or internal SLOs
```

### Option 2: Error Rate SLO with Burn Rate Alerting

```python
import anthropic
import time
from collections import deque
from dataclasses import dataclass, field

client = anthropic.Anthropic()

@dataclass
class ErrorRateSLO:
    """
    Tracks error rate against an SLO target.
    Implements burn rate alerting: fires when error budget is consumed too fast.
    """
    target_availability: float  # e.g., 0.99 = 99% success rate
    window_minutes: float = 60.0
    error_budget_burn_threshold: float = 2.0  # Alert if burning budget 2x faster than allowed

    _events: deque = field(default_factory=deque)  # (ts, is_error)
    _alerts_fired: list[dict] = field(default_factory=list)

    @property
    def allowed_error_rate(self) -> float:
        return 1.0 - self.target_availability

    def record(self, success: bool, context: str = ""):
        ts = time.time()
        self._events.append((ts, not success))
        # Evict old events outside window
        cutoff = ts - self.window_minutes * 60
        while self._events and self._events[0][0] < cutoff:
            self._events.popleft()
        self._check_burn_rate(context)

    def _check_burn_rate(self, context: str):
        if len(self._events) < 10:
            return  # Not enough data
        errors = sum(1 for _, is_err in self._events if is_err)
        error_rate = errors / len(self._events)
        burn_rate = error_rate / max(self.allowed_error_rate, 1e-9)

        if burn_rate > self.error_budget_burn_threshold:
            alert = {
                "ts": time.time(),
                "error_rate": round(error_rate * 100, 2),
                "burn_rate": round(burn_rate, 2),
                "context": context,
            }
            self._alerts_fired.append(alert)
            print(f"[SLO ALERT] Error rate {alert['error_rate']}% is burning budget at {alert['burn_rate']:.1f}x rate!")

    def report(self) -> dict:
        if not self._events:
            return {"status": "no_data"}
        total = len(self._events)
        errors = sum(1 for _, is_err in self._events if is_err)
        error_rate = errors / total
        availability = 1 - error_rate
        return {
            "total_requests": total,
            "errors": errors,
            "availability_pct": round(availability * 100, 3),
            "target_pct": round(self.target_availability * 100, 3),
            "slo_met": availability >= self.target_availability,
            "alerts_fired": len(self._alerts_fired),
            "slo_status": "GREEN" if availability >= self.target_availability else "RED",
        }

slo = ErrorRateSLO(target_availability=0.99, window_minutes=5, error_budget_burn_threshold=3.0)

def slo_tracked_request(user_message: str, simulate_error: bool = False) -> str:
    if simulate_error:
        slo.record(success=False, context=user_message[:30])
        return "[SIMULATED ERROR]"

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=64,
            messages=[{"role": "user", "content": user_message}],
        )
        slo.record(success=True)
        return response.content[0].text
    except Exception as e:
        slo.record(success=False, context=str(e))
        return f"[ERROR: {e}]"

# Simulate requests with some errors
test_cases = [
    ("What is Python?", False),
    ("Explain async/await", False),
    ("fail", True),
    ("What is REST?", False),
    ("fail", True),
    ("fail", True),
    ("Explain Docker", False),
    ("fail", True),
    ("fail", True),
    ("What is Kubernetes?", False),
]

for msg, err in test_cases:
    slo_tracked_request(msg, simulate_error=err)

print(f"\n=== Error Rate SLO Report ===")
import json
print(json.dumps(slo.report(), indent=2))

# Expected Token Savings: None — SLO monitoring only
# Environment: APIs with availability SLAs where error budget burn rate matters
```

### Option 3: Multi-Metric SLO Dashboard

```python
import anthropic
import json
import time
from dataclasses import dataclass, field
from collections import deque
from typing import Callable

client = anthropic.Anthropic()

@dataclass
class SLOMetric:
    name: str
    target: float
    comparator: str  # "lte" (lower is better) or "gte" (higher is better)
    unit: str
    _values: deque = field(default_factory=lambda: deque(maxlen=200))

    def record(self, value: float):
        self._values.append(value)

    def current(self) -> float:
        if not self._values:
            return 0.0
        return sum(self._values) / len(self._values)

    def is_met(self) -> bool:
        current = self.current()
        if self.comparator == "lte":
            return current <= self.target
        return current >= self.target

    def status(self) -> str:
        return "GREEN" if self.is_met() else "RED"

class AgentSLODashboard:
    def __init__(self):
        self.metrics: dict[str, SLOMetric] = {
            "p95_latency_ms": SLOMetric("p95_latency_ms", target=3000, comparator="lte", unit="ms"),
            "error_rate_pct": SLOMetric("error_rate_pct", target=1.0, comparator="lte", unit="%"),
            "tool_success_rate_pct": SLOMetric("tool_success_rate_pct", target=95.0, comparator="gte", unit="%"),
            "avg_turns_to_completion": SLOMetric("avg_turns_to_completion", target=3.0, comparator="lte", unit="turns"),
        }
        self._request_count = 0
        self._error_count = 0

    def record_request(self, latency_ms: float, error: bool, tool_errors: int, total_tools: int, turns: int):
        self._request_count += 1
        if error:
            self._error_count += 1

        self.metrics["p95_latency_ms"].record(latency_ms)
        self.metrics["error_rate_pct"].record(self._error_count / self._request_count * 100)
        if total_tools > 0:
            self.metrics["tool_success_rate_pct"].record((total_tools - tool_errors) / total_tools * 100)
        self.metrics["avg_turns_to_completion"].record(turns)

        self._check_alerts()

    def _check_alerts(self):
        for name, metric in self.metrics.items():
            if not metric.is_met() and len(metric._values) >= 5:
                print(f"[SLO ALERT] {name}: {metric.current():.2f}{metric.unit} violates target {metric.target}{metric.unit}")

    def render(self) -> dict:
        return {
            "overall_status": "GREEN" if all(m.is_met() for m in self.metrics.values()) else "RED",
            "metrics": {
                name: {
                    "current": round(metric.current(), 2),
                    "target": metric.target,
                    "unit": metric.unit,
                    "status": metric.status(),
                }
                for name, metric in self.metrics.items()
            },
            "total_requests": self._request_count,
        }

dashboard = AgentSLODashboard()

def instrumented_agent(user_message: str, simulate_tool_error: bool = False) -> str:
    t0 = time.monotonic()
    turns = 0
    tool_calls = 0
    tool_errors = 0
    had_error = False

    tools = [{
        "name": "lookup",
        "description": "Look up data",
        "input_schema": {"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"]},
    }]
    messages = [{"role": "user", "content": user_message}]

    while turns < 5:
        turns += 1
        try:
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=128,
                tools=tools,
                messages=messages,
            )
        except Exception as e:
            had_error = True
            break

        if response.stop_reason == "end_turn":
            break

        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                tool_calls += 1
                if simulate_tool_error:
                    tool_errors += 1
                    tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": "Error", "is_error": True})
                else:
                    tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps({"value": "ok"})})

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

    latency_ms = (time.monotonic() - t0) * 1000
    dashboard.record_request(latency_ms, had_error, tool_errors, tool_calls, turns)
    return "done"

# Run various scenarios
for i in range(6):
    instrumented_agent(f"Look up item {i}", simulate_tool_error=(i % 3 == 2))

print(f"\n=== Multi-Metric SLO Dashboard ===")
print(json.dumps(dashboard.render(), indent=2))

# Expected Token Savings: None — multi-metric observability infrastructure
# Environment: Production agents requiring comprehensive SLO reporting for stakeholders
```

### Option 4: SLO with Error Budget Tracking

```python
import anthropic
import json
import time
from dataclasses import dataclass, field

client = anthropic.Anthropic()

@dataclass
class ErrorBudget:
    """
    Tracks 30-day error budget consumption.
    Error budget = allowed failures in 30 days given SLO target.
    """
    slo_target: float  # e.g., 0.999 = 99.9% availability
    period_days: float = 30.0

    _total_requests: int = 0
    _total_errors: int = 0
    _period_start: float = field(default_factory=time.time)

    @property
    def budget_minutes(self) -> float:
        """Total allowed downtime minutes in the period."""
        downtime_fraction = 1.0 - self.slo_target
        return downtime_fraction * self.period_days * 24 * 60

    @property
    def budget_requests(self) -> int:
        """Total allowed failed requests given current volume."""
        if self._total_requests == 0:
            return 0
        failure_rate = 1.0 - self.slo_target
        return int(self._total_requests * failure_rate)

    @property
    def budget_remaining_pct(self) -> float:
        allowed = self.budget_requests
        if allowed == 0:
            return 100.0 if self._total_errors == 0 else 0.0
        remaining = max(0, allowed - self._total_errors)
        return remaining / allowed * 100

    @property
    def burn_rate(self) -> float:
        """How fast we're burning through error budget (1.0 = exactly on pace)."""
        elapsed_days = (time.time() - self._period_start) / 86400
        if elapsed_days == 0 or self._total_requests == 0:
            return 0.0
        actual_error_rate = self._total_errors / self._total_requests
        expected_error_rate = 1.0 - self.slo_target
        return actual_error_rate / max(expected_error_rate, 1e-9)

    def record(self, success: bool):
        self._total_requests += 1
        if not success:
            self._total_errors += 1

    def report(self) -> dict:
        elapsed_days = (time.time() - self._period_start) / 86400
        return {
            "slo_target_pct": self.slo_target * 100,
            "total_requests": self._total_requests,
            "total_errors": self._total_errors,
            "actual_availability_pct": round((1 - self._total_errors / max(self._total_requests, 1)) * 100, 3),
            "error_budget_allowed_requests": self.budget_requests,
            "error_budget_remaining_pct": round(self.budget_remaining_pct, 1),
            "burn_rate": round(self.burn_rate, 2),
            "burn_rate_status": "CRITICAL" if self.burn_rate > 14.4 else "HIGH" if self.burn_rate > 6 else "OK",
            "elapsed_days": round(elapsed_days, 4),
        }

budget = ErrorBudget(slo_target=0.999)  # 99.9% availability

def budget_tracked_call(user_message: str, simulate_error: bool = False) -> str:
    if simulate_error:
        budget.record(success=False)
        return "[ERROR]"
    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=64,
            messages=[{"role": "user", "content": user_message}],
        )
        budget.record(success=True)
        return response.content[0].text
    except Exception:
        budget.record(success=False)
        return "[ERROR]"

# Simulate 20 requests with some failures
import random
random.seed(42)
for i in range(20):
    budget_tracked_call(f"Question {i}", simulate_error=(random.random() < 0.1))

report = budget.report()
print("=== Error Budget Report ===")
print(json.dumps(report, indent=2))

status = report["burn_rate_status"]
if status != "OK":
    pct_remaining = report["error_budget_remaining_pct"]
    print(f"\n[ALERT] Burn rate status: {status} | {pct_remaining:.1f}% budget remaining")

# Expected Token Savings: None — error budget tracking
# Environment: Teams practicing SRE with 30-day error budget windows and on-call rotation
```

### Option 5: SLO Alerting with Multiple Channels

```python
import anthropic
import json
import time
from dataclasses import dataclass, field
from typing import Callable

client = anthropic.Anthropic()

AlertHandler = Callable[[str, dict], None]

@dataclass
class SLOAlert:
    severity: str  # "warning", "critical"
    metric: str
    current_value: float
    threshold: float
    message: str
    ts: float = field(default_factory=time.time)

class SLOAlerter:
    def __init__(self):
        self._handlers: dict[str, list[AlertHandler]] = {"warning": [], "critical": []}
        self._alerts: list[SLOAlert] = []
        self._suppressed: dict[str, float] = {}  # metric -> last alert ts (for cooldown)
        self.cooldown_seconds: float = 60.0

    def register_handler(self, severity: str, handler: AlertHandler):
        self._handlers[severity].append(handler)

    def _should_suppress(self, metric: str) -> bool:
        last = self._suppressed.get(metric, 0)
        return time.time() - last < self.cooldown_seconds

    def fire(self, alert: SLOAlert):
        if self._should_suppress(alert.metric):
            return
        self._alerts.append(alert)
        self._suppressed[alert.metric] = time.time()
        for handler in self._handlers.get(alert.severity, []):
            handler(alert.severity, {
                "metric": alert.metric,
                "current": alert.current_value,
                "threshold": alert.threshold,
                "message": alert.message,
            })

# Alert handlers (in production: Slack, PagerDuty, email)
def log_handler(severity: str, data: dict):
    print(f"[{severity.upper()} ALERT] {data['metric']}: {data['current']:.2f} > {data['threshold']:.2f} — {data['message']}")

def metrics_handler(severity: str, data: dict):
    # In production: POST to Datadog/Prometheus alertmanager
    pass

alerter = SLOAlerter()
alerter.cooldown_seconds = 1.0  # Short cooldown for demo
alerter.register_handler("warning", log_handler)
alerter.register_handler("critical", log_handler)
alerter.register_handler("critical", metrics_handler)

# SLO thresholds
LATENCY_WARNING_MS = 2000
LATENCY_CRITICAL_MS = 5000
ERROR_RATE_WARNING_PCT = 1.0
ERROR_RATE_CRITICAL_PCT = 5.0

_latencies: list[float] = []
_errors: list[bool] = []

def check_and_alert():
    if len(_latencies) < 5:
        return
    p95 = sorted(_latencies)[int(len(_latencies) * 0.95)]
    error_rate = sum(_errors) / len(_errors) * 100

    if p95 > LATENCY_CRITICAL_MS:
        alerter.fire(SLOAlert("critical", "p95_latency_ms", p95, LATENCY_CRITICAL_MS,
                              f"p95 latency {p95:.0f}ms exceeds critical threshold"))
    elif p95 > LATENCY_WARNING_MS:
        alerter.fire(SLOAlert("warning", "p95_latency_ms", p95, LATENCY_WARNING_MS,
                              f"p95 latency {p95:.0f}ms exceeds warning threshold"))

    if error_rate > ERROR_RATE_CRITICAL_PCT:
        alerter.fire(SLOAlert("critical", "error_rate_pct", error_rate, ERROR_RATE_CRITICAL_PCT,
                              f"Error rate {error_rate:.1f}% exceeds critical threshold"))
    elif error_rate > ERROR_RATE_WARNING_PCT:
        alerter.fire(SLOAlert("warning", "error_rate_pct", error_rate, ERROR_RATE_WARNING_PCT,
                              f"Error rate {error_rate:.1f}% exceeds warning threshold"))

def monitored_call(user_message: str, simulate_slow: bool = False, simulate_error: bool = False) -> str:
    t0 = time.monotonic()

    if simulate_error:
        _errors.append(True)
        _latencies.append(100)
        check_and_alert()
        return "[ERROR]"

    if simulate_slow:
        time.sleep(0.05)  # Simulate slow response (scaled down for demo)

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        messages=[{"role": "user", "content": user_message}],
    )
    latency_ms = (time.monotonic() - t0) * 1000
    _errors.append(False)
    _latencies.append(latency_ms)
    check_and_alert()
    return response.content[0].text

for i in range(10):
    monitored_call(f"Brief answer {i}", simulate_error=(i >= 7))

print(f"\nAlerts fired: {len(alerter._alerts)}")

# Expected Token Savings: None — alerting infrastructure
# Environment: On-call teams needing automated alerts to Slack, PagerDuty, or OpsGenie
```

### Option 6: SLO Reporting with SQLite Persistence

```python
import anthropic
import json
import sqlite3
import time
from pathlib import Path

client = anthropic.Anthropic()
DB_PATH = Path("/tmp/slo_metrics.db")

def init_slo_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS slo_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                request_id TEXT,
                latency_ms REAL,
                success INTEGER NOT NULL,
                tool_calls INTEGER DEFAULT 0,
                tool_errors INTEGER DEFAULT 0,
                turns INTEGER DEFAULT 1,
                model TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ts ON slo_events(ts)")
        conn.commit()

init_slo_db()

def record_event(request_id: str, latency_ms: float, success: bool,
                 tool_calls: int = 0, tool_errors: int = 0, turns: int = 1,
                 model: str = "claude-haiku-4-5-20251001"):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO slo_events (ts, request_id, latency_ms, success, tool_calls, tool_errors, turns, model) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (time.time(), request_id, latency_ms, int(success), tool_calls, tool_errors, turns, model),
        )
        conn.commit()

def query_slo_report(window_hours: float = 1.0) -> dict:
    cutoff = time.time() - window_hours * 3600
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT latency_ms, success, tool_calls, tool_errors, turns FROM slo_events WHERE ts > ? ORDER BY ts",
            (cutoff,),
        ).fetchall()

    if not rows:
        return {"status": "no_data", "window_hours": window_hours}

    latencies = sorted(r["latency_ms"] for r in rows if r["latency_ms"] is not None)
    total = len(rows)
    errors = sum(1 for r in rows if not r["success"])
    tool_total = sum(r["tool_calls"] for r in rows)
    tool_err = sum(r["tool_errors"] for r in rows)

    def percentile(data: list, pct: float) -> float:
        if not data:
            return 0.0
        idx = int(len(data) * pct / 100)
        return data[min(idx, len(data) - 1)]

    p50 = percentile(latencies, 50)
    p95 = percentile(latencies, 95)
    p99 = percentile(latencies, 99)
    availability = (total - errors) / total * 100

    # SLO targets
    SLO_P95_MS = 3000
    SLO_AVAILABILITY_PCT = 99.0

    return {
        "window_hours": window_hours,
        "total_requests": total,
        "errors": errors,
        "availability_pct": round(availability, 3),
        "availability_slo_met": availability >= SLO_AVAILABILITY_PCT,
        "p50_ms": round(p50, 1),
        "p95_ms": round(p95, 1),
        "p99_ms": round(p99, 1),
        "p95_slo_met": p95 <= SLO_P95_MS,
        "tool_success_rate_pct": round((tool_total - tool_err) / max(tool_total, 1) * 100, 1),
        "avg_turns": round(sum(r["turns"] for r in rows) / total, 2),
        "overall_slo_status": "GREEN" if (availability >= SLO_AVAILABILITY_PCT and p95 <= SLO_P95_MS) else "RED",
    }

import uuid

def persistent_slo_call(user_message: str, simulate_error: bool = False) -> str:
    req_id = str(uuid.uuid4())[:8]
    t0 = time.monotonic()

    if simulate_error:
        record_event(req_id, 50, success=False)
        return "[ERROR]"

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        messages=[{"role": "user", "content": user_message}],
    )
    latency_ms = (time.monotonic() - t0) * 1000
    record_event(req_id, latency_ms, success=True)
    return response.content[0].text

for i in range(8):
    persistent_slo_call(f"Answer question {i}", simulate_error=(i == 5))

report = query_slo_report(window_hours=1.0)
print("=== Persistent SLO Report (last 1h) ===")
print(json.dumps(report, indent=2))

# Expected Token Savings: None — SQLite persistence enables long-term SLO trend analysis
# Environment: Production services needing SLO reporting across restarts and multiple deployments
```

## Comparison

| Option | Metrics Tracked | Alerting | Persistence | Error Budget | Best For |
|--------|----------------|---------|------------|-------------|---------|
| 1. Latency Percentiles | p50/p95/p99 | Threshold | Memory | No | Latency SLA agents |
| 2. Error Rate + Burn Rate | Error rate, burn rate | Burn rate | Memory | Partial | Availability SLOs |
| 3. Multi-Metric Dashboard | Latency+Error+Tools+Turns | In-flight | Memory | No | Comprehensive SLO dashboards |
| 4. Error Budget | Availability, burn | Budget depletion | Memory | Yes | SRE error budget practice |
| 5. Multi-Channel Alerting | Latency+Error | Warning+Critical | Memory | No | On-call alert routing |
| 6. SQLite Persistent | All metrics | Threshold | SQLite | No | Long-term SLO trend analysis |
