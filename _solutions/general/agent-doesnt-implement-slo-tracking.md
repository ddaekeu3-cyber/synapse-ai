---
layout: solution
title: "Agent Doesn't Implement SLO Tracking"
category: general
description: "Define Service Level Objectives for latency, error rate, and token cost — then measure compliance in real time so violations trigger alerts before they become user-visible outages."
tags: [general, slo, observability, metrics, sqlite, python]
---

# Agent Doesn't Implement SLO Tracking

Without SLOs, teams learn about agent degradation from user complaints. Defining concrete objectives (p95 latency < 5s, error rate < 1%, cost < $0.01/request) and measuring them continuously turns reactive firefighting into proactive alerting.

## Option 1: In-Memory SLO Window with Rolling Compliance

```python
import anthropic
import time
from collections import deque
from dataclasses import dataclass, field

client = anthropic.Anthropic()

@dataclass
class SLOConfig:
    name: str
    p95_latency_s: float     # 95th percentile latency target
    error_rate_pct: float    # max acceptable error rate (%)
    window_requests: int     # rolling window size

@dataclass
class SLOTracker:
    config: SLOConfig
    latencies: deque = field(default_factory=deque)
    errors: deque = field(default_factory=deque)

    def record(self, latency_s: float, is_error: bool):
        self.latencies.append(latency_s)
        self.errors.append(1 if is_error else 0)
        max_w = self.config.window_requests
        while len(self.latencies) > max_w:
            self.latencies.popleft()
            self.errors.popleft()

    def p95_latency(self) -> float:
        if not self.latencies:
            return 0.0
        sorted_lats = sorted(self.latencies)
        idx = int(len(sorted_lats) * 0.95)
        return sorted_lats[min(idx, len(sorted_lats) - 1)]

    def error_rate(self) -> float:
        if not self.errors:
            return 0.0
        return sum(self.errors) / len(self.errors) * 100

    def compliance(self) -> dict:
        p95 = self.p95_latency()
        err = self.error_rate()
        return {
            "slo": self.config.name,
            "p95_latency_s": round(p95, 3),
            "p95_ok": p95 <= self.config.p95_latency_s,
            "error_rate_pct": round(err, 2),
            "error_ok": err <= self.config.error_rate_pct,
            "requests": len(self.latencies),
            "compliant": p95 <= self.config.p95_latency_s and err <= self.config.error_rate_pct,
        }

slo = SLOTracker(SLOConfig(
    name="agent_api",
    p95_latency_s=5.0,
    error_rate_pct=2.0,
    window_requests=100,
))

def call_agent(prompt: str) -> str:
    start = time.monotonic()
    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=64,
            messages=[{"role": "user", "content": prompt}],
        )
        latency = time.monotonic() - start
        slo.record(latency, is_error=False)
        return resp.content[0].text
    except Exception as e:
        latency = time.monotonic() - start
        slo.record(latency, is_error=True)
        raise

prompts = ["What is 2+2?", "Explain TCP", "What is Python?", "What is the GIL?"]
for p in prompts:
    try:
        result = call_agent(p)
        print(f"OK: {result[:40]}")
    except Exception as e:
        print(f"ERR: {e}")

report = slo.compliance()
status = "✓ COMPLIANT" if report["compliant"] else "✗ VIOLATION"
print(f"\nSLO Report [{status}]:")
for k, v in report.items():
    print(f"  {k}: {v}")

# Expected Token Savings: N/A; SLO tracking enables cost-based alerts when spend exceeds target
# Environment: pure Python; extend window_requests for longer evaluation periods
```

## Option 2: SQLite-Backed SLO with Time-Window Compliance

```python
import anthropic
import sqlite3
import time
import statistics

client = anthropic.Anthropic()
DB = "slo_metrics.db"

def init_db():
    con = sqlite3.connect(DB)
    con.execute("""
        CREATE TABLE IF NOT EXISTS requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            endpoint TEXT, ts REAL,
            latency_s REAL, is_error INTEGER,
            input_tokens INTEGER, output_tokens INTEGER,
            cost_usd REAL
        )
    """)
    con.commit(); con.close()

# USD per million tokens (approximate)
MODEL_COST = {"input": 0.8, "output": 4.0}

def estimate_cost(inp: int, out: int) -> float:
    return (inp * MODEL_COST["input"] + out * MODEL_COST["output"]) / 1_000_000

def record_request(endpoint: str, latency_s: float, is_error: bool,
                   inp: int = 0, out: int = 0):
    cost = estimate_cost(inp, out)
    con = sqlite3.connect(DB)
    con.execute("INSERT INTO requests VALUES (NULL,?,?,?,?,?,?,?)",
                (endpoint, time.time(), latency_s, int(is_error), inp, out, cost))
    con.commit(); con.close()

def slo_report(endpoint: str, window_s: float = 3600) -> dict:
    cutoff = time.time() - window_s
    con = sqlite3.connect(DB)
    rows = con.execute(
        "SELECT latency_s, is_error, cost_usd FROM requests "
        "WHERE endpoint=? AND ts>? ORDER BY latency_s",
        (endpoint, cutoff)
    ).fetchall()
    con.close()

    if not rows:
        return {"endpoint": endpoint, "requests": 0, "compliant": True}

    latencies = [r[0] for r in rows]
    errors = [r[1] for r in rows]
    costs = [r[2] for r in rows]
    n = len(rows)
    idx_p95 = int(n * 0.95)
    p95 = latencies[min(idx_p95, n - 1)]
    p50 = latencies[n // 2]
    err_rate = sum(errors) / n * 100
    total_cost = sum(costs)
    avg_cost = total_cost / n if n else 0

    # SLO targets
    P95_TARGET = 5.0
    ERR_TARGET  = 2.0
    COST_TARGET = 0.01  # per request

    return {
        "endpoint": endpoint,
        "window_h": window_s / 3600,
        "requests": n,
        "p50_latency_s": round(p50, 3),
        "p95_latency_s": round(p95, 3),
        "p95_ok": p95 <= P95_TARGET,
        "error_rate_pct": round(err_rate, 2),
        "error_ok": err_rate <= ERR_TARGET,
        "avg_cost_usd": round(avg_cost, 6),
        "cost_ok": avg_cost <= COST_TARGET,
        "total_cost_usd": round(total_cost, 4),
        "compliant": p95 <= P95_TARGET and err_rate <= ERR_TARGET and avg_cost <= COST_TARGET,
    }

init_db()

def traced_call(prompt: str) -> str:
    start = time.monotonic()
    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=64,
            messages=[{"role": "user", "content": prompt}],
        )
        latency = time.monotonic() - start
        record_request("chat", latency, False,
                       resp.usage.input_tokens, resp.usage.output_tokens)
        return resp.content[0].text
    except Exception as e:
        latency = time.monotonic() - start
        record_request("chat", latency, True)
        raise

for p in ["What is DNS?", "Explain HTTP/2", "What is TLS?", "What is a CDN?"]:
    print(f"OK: {traced_call(p)[:50]}")

report = slo_report("chat", window_s=3600)
status = "✓ COMPLIANT" if report["compliant"] else "✗ VIOLATION"
print(f"\nSLO [{status}]:")
for k, v in report.items():
    print(f"  {k}: {v}")

# Expected Token Savings: Cost SLO fires before overspend; historical data enables trend analysis
# Environment: SQLite; extend with alert webhook on violation
```

## Option 3: Multi-Endpoint SLO Dashboard with Budget Burn Rate

```python
import anthropic
import sqlite3
import time
from dataclasses import dataclass

client = anthropic.Anthropic()
DB = "multi_slo.db"

@dataclass
class SLO:
    endpoint: str
    p95_latency_s: float = 5.0
    error_rate_pct: float = 1.0
    daily_cost_usd: float = 10.0
    monthly_requests: int = 100_000

def init_db():
    con = sqlite3.connect(DB)
    con.execute("""
        CREATE TABLE IF NOT EXISTS metrics (
            endpoint TEXT, ts REAL,
            latency_s REAL, is_error INTEGER,
            cost_usd REAL
        )
    """)
    con.commit(); con.close()

def log(endpoint: str, latency_s: float, is_error: bool, cost_usd: float = 0):
    con = sqlite3.connect(DB)
    con.execute("INSERT INTO metrics VALUES (?,?,?,?,?)",
                (endpoint, time.time(), latency_s, int(is_error), cost_usd))
    con.commit(); con.close()

def burn_rate(endpoint: str, window_h: float = 1.0) -> dict:
    """Calculate error burn rate relative to monthly budget."""
    cutoff = time.time() - window_h * 3600
    con = sqlite3.connect(DB)
    rows = con.execute(
        "SELECT latency_s, is_error, cost_usd FROM metrics WHERE endpoint=? AND ts>?",
        (endpoint, cutoff)
    ).fetchall()
    # Monthly total for burn rate calculation
    month_start = time.time() - 30 * 86400
    month_total = con.execute(
        "SELECT COUNT(*), SUM(cost_usd) FROM metrics WHERE endpoint=? AND ts>?",
        (endpoint, month_start)
    ).fetchone()
    con.close()

    if not rows:
        return {}
    latencies = sorted(r[0] for r in rows)
    errors = [r[1] for r in rows]
    costs = [r[2] for r in rows]
    n = len(rows)
    p95 = latencies[min(int(n * 0.95), n - 1)]
    err_pct = sum(errors) / n * 100
    hourly_cost = sum(costs)
    daily_cost = hourly_cost * (24 / window_h)

    return {
        "endpoint": endpoint,
        "window_h": window_h,
        "requests": n,
        "p95_s": round(p95, 3),
        "error_pct": round(err_pct, 2),
        "hourly_cost_usd": round(hourly_cost, 4),
        "daily_cost_projected_usd": round(daily_cost, 4),
        "monthly_requests": month_total[0],
        "monthly_cost_usd": round(month_total[1] or 0, 4),
    }

def check_slo(slo: SLO) -> dict:
    report = burn_rate(slo.endpoint)
    if not report:
        return {"endpoint": slo.endpoint, "status": "NO_DATA"}
    violations = []
    if report["p95_s"] > slo.p95_latency_s:
        violations.append(f"p95={report['p95_s']}s > {slo.p95_latency_s}s")
    if report["error_pct"] > slo.error_rate_pct:
        violations.append(f"errors={report['error_pct']}% > {slo.error_rate_pct}%")
    if report["daily_cost_projected_usd"] > slo.daily_cost_usd:
        violations.append(f"cost=${report['daily_cost_projected_usd']:.4f} > ${slo.daily_cost_usd}")
    return {
        **report,
        "violations": violations,
        "compliant": len(violations) == 0,
    }

init_db()
SLOS = {
    "chat": SLO("chat"),
    "summarize": SLO("summarize", p95_latency_s=8.0, daily_cost_usd=5.0),
}

def call(endpoint: str, prompt: str) -> str:
    start = time.monotonic()
    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=64,
            messages=[{"role": "user", "content": prompt}],
        )
        latency = time.monotonic() - start
        cost = (resp.usage.input_tokens * 0.8 + resp.usage.output_tokens * 4.0) / 1_000_000
        log(endpoint, latency, False, cost)
        return resp.content[0].text
    except Exception as e:
        log(endpoint, time.monotonic() - start, True, 0)
        raise

for p in ["Explain REST APIs", "What is GraphQL?"]:
    call("chat", p)
for p in ["Summarize: The quick brown fox.", "Summarize: AI is transforming software."]:
    call("summarize", p)

print("\nSLO Dashboard:")
for name, slo in SLOS.items():
    report = check_slo(slo)
    status = "✓" if report.get("compliant", True) else "✗ VIOLATION"
    print(f"\n  {name} [{status}]")
    for k in ["requests", "p95_s", "error_pct", "daily_cost_projected_usd", "violations"]:
        print(f"    {k}: {report.get(k, 'N/A')}")

# Expected Token Savings: Daily cost projection triggers alert before monthly budget exceeded
# Environment: SQLite; multiple endpoints tracked independently; add alerting on violation
```

## Option 4: Error Budget Tracking with Burn Alert

```python
import anthropic
import sqlite3
import time

client = anthropic.Anthropic()
DB = "error_budget.db"

def init_db():
    con = sqlite3.connect(DB)
    con.execute("""
        CREATE TABLE IF NOT EXISTS calls (
            ts REAL, is_error INTEGER, latency_s REAL
        )
    """)
    con.commit(); con.close()

def log_call(latency_s: float, is_error: bool):
    con = sqlite3.connect(DB)
    con.execute("INSERT INTO calls VALUES (?,?,?)", (time.time(), int(is_error), latency_s))
    con.commit(); con.close()

def error_budget_report(
    slo_availability: float = 0.99,   # 99% availability target
    window_days: int = 30,
) -> dict:
    cutoff = time.time() - window_days * 86400
    con = sqlite3.connect(DB)
    total = con.execute("SELECT COUNT(*) FROM calls WHERE ts>?", (cutoff,)).fetchone()[0]
    errors = con.execute(
        "SELECT COUNT(*) FROM calls WHERE ts>? AND is_error=1", (cutoff,)
    ).fetchone()[0]
    con.close()

    if total == 0:
        return {"status": "NO_DATA"}

    allowed_errors = int(total * (1 - slo_availability))
    remaining_budget = max(0, allowed_errors - errors)
    budget_consumed_pct = (errors / max(allowed_errors, 1)) * 100

    # Burn rate: how fast are we consuming the budget?
    # Check last 1h vs full window
    cutoff_1h = time.time() - 3600
    con = sqlite3.connect(DB)
    errors_1h = con.execute(
        "SELECT COUNT(*) FROM calls WHERE ts>? AND is_error=1", (cutoff_1h,)
    ).fetchone()[0]
    total_1h = con.execute(
        "SELECT COUNT(*) FROM calls WHERE ts>?", (cutoff_1h,)
    ).fetchone()[0]
    con.close()

    hourly_error_rate = errors_1h / max(total_1h, 1)
    hours_to_budget_exhaustion = (
        remaining_budget / max(errors_1h / 1, 0.0001)
        if errors_1h > 0 else float("inf")
    )

    # Alert if burn rate will exhaust budget within 24h
    alert = hours_to_budget_exhaustion < 24

    return {
        "window_days": window_days,
        "total_requests": total,
        "error_count": errors,
        "allowed_errors": allowed_errors,
        "remaining_budget": remaining_budget,
        "budget_consumed_pct": round(budget_consumed_pct, 1),
        "hourly_burn_rate": round(errors_1h, 0),
        "hours_to_exhaustion": round(hours_to_budget_exhaustion, 1),
        "alert": alert,
        "alert_message": "ERROR BUDGET WILL BE EXHAUSTED IN <24H" if alert else None,
    }

init_db()

def call(prompt: str) -> str:
    start = time.monotonic()
    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=64,
            messages=[{"role": "user", "content": prompt}],
        )
        log_call(time.monotonic() - start, False)
        return resp.content[0].text
    except Exception as e:
        log_call(time.monotonic() - start, True)
        raise

for p in ["What is TCP?", "Explain UDP", "What is DNS?", "What is HTTP/3?"]:
    print(f"OK: {call(p)[:50]}")

report = error_budget_report(slo_availability=0.99, window_days=30)
print("\nError Budget Report:")
for k, v in report.items():
    if v is not None:
        print(f"  {k}: {v}")

# Expected Token Savings: Error budget tracking quantifies how much degradation is acceptable before SLO breach
# Environment: SQLite 30-day rolling window; alert threshold configurable per SLO tier
```

## Option 5: Latency Histogram SLO with Percentile Tracking

```python
import anthropic
import time
import math
from collections import defaultdict

client = anthropic.Anthropic()

class LatencyHistogram:
    """Exponential bucket histogram for memory-efficient percentile tracking."""

    def __init__(self, num_buckets: int = 30, max_s: float = 60.0):
        self.buckets = [0] * num_buckets
        self.num_buckets = num_buckets
        self.max_s = max_s
        self.total = 0
        self.sum_s = 0.0

    def _bucket_index(self, latency_s: float) -> int:
        if latency_s <= 0:
            return 0
        idx = int(math.log1p(latency_s / self.max_s * 100) / math.log1p(100) * self.num_buckets)
        return min(idx, self.num_buckets - 1)

    def record(self, latency_s: float):
        self.buckets[self._bucket_index(latency_s)] += 1
        self.total += 1
        self.sum_s += latency_s

    def percentile(self, p: float) -> float:
        if self.total == 0:
            return 0.0
        target = self.total * p
        cumulative = 0
        for i, count in enumerate(self.buckets):
            cumulative += count
            if cumulative >= target:
                # Interpolate bucket upper bound
                frac = i / self.num_buckets
                return self.max_s * (math.expm1(frac * math.log1p(100)) / 100)
        return self.max_s

    @property
    def mean(self) -> float:
        return self.sum_s / self.total if self.total else 0.0

# Per-model histogram
histograms: dict[str, LatencyHistogram] = defaultdict(LatencyHistogram)

SLO_TARGETS = {
    "p50": 1.0,
    "p95": 5.0,
    "p99": 15.0,
}

def call_and_track(prompt: str, model: str = "claude-haiku-4-5-20251001") -> str:
    start = time.monotonic()
    resp = client.messages.create(
        model=model, max_tokens=64,
        messages=[{"role": "user", "content": prompt}],
    )
    latency = time.monotonic() - start
    histograms[model].record(latency)
    return resp.content[0].text

def slo_status(model: str) -> dict:
    h = histograms[model]
    if h.total == 0:
        return {}
    results = {"model": model, "requests": h.total, "mean_s": round(h.mean, 3)}
    violations = []
    for pct_name, target in SLO_TARGETS.items():
        p = float(pct_name[1:]) / 100
        value = h.percentile(p)
        results[f"{pct_name}_s"] = round(value, 3)
        results[f"{pct_name}_ok"] = value <= target
        if value > target:
            violations.append(f"{pct_name}={value:.2f}s > {target}s")
    results["violations"] = violations
    results["compliant"] = len(violations) == 0
    return results

for p in ["What is async?", "Explain generators", "What is a closure?", "What is recursion?"]:
    call_and_track(p)

status = slo_status("claude-haiku-4-5-20251001")
print("Latency SLO Status:")
for k, v in status.items():
    print(f"  {k}: {v}")

# Expected Token Savings: Histogram uses O(buckets) memory vs O(requests); tracks millions of requests cheaply
# Environment: pure Python; swap exponential buckets with linear for narrow latency ranges
```

## Option 6: SLO Alert Webhook with Cooldown

```python
import anthropic
import sqlite3
import time
import json
import urllib.request
from dataclasses import dataclass

client = anthropic.Anthropic()
DB = "slo_alerts.db"

@dataclass
class AlertConfig:
    webhook_url: str | None = None   # set to your Slack/Discord webhook
    cooldown_s: float = 300          # 5 min between same-type alerts
    p95_threshold_s: float = 5.0
    error_threshold_pct: float = 2.0

def init_db():
    con = sqlite3.connect(DB)
    con.execute("CREATE TABLE IF NOT EXISTS calls (ts REAL, latency_s REAL, is_error INTEGER)")
    con.execute("CREATE TABLE IF NOT EXISTS alerts (alert_type TEXT PRIMARY KEY, last_fired REAL)")
    con.commit(); con.close()

def log_call(latency_s: float, is_error: bool):
    con = sqlite3.connect(DB)
    con.execute("INSERT INTO calls VALUES (?,?,?)", (time.time(), latency_s, int(is_error)))
    con.commit(); con.close()

def should_alert(alert_type: str, cooldown_s: float) -> bool:
    con = sqlite3.connect(DB)
    row = con.execute("SELECT last_fired FROM alerts WHERE alert_type=?", (alert_type,)).fetchone()
    con.close()
    if row and time.time() - row[0] < cooldown_s:
        return False
    return True

def record_alert(alert_type: str):
    con = sqlite3.connect(DB)
    con.execute("INSERT OR REPLACE INTO alerts VALUES (?,?)", (alert_type, time.time()))
    con.commit(); con.close()

def send_alert(cfg: AlertConfig, alert_type: str, message: str):
    if not should_alert(alert_type, cfg.cooldown_s):
        print(f"[ALERT SUPPRESSED] {alert_type} (cooldown active)")
        return
    record_alert(alert_type)
    print(f"[ALERT] {alert_type}: {message}")
    if cfg.webhook_url:
        try:
            payload = json.dumps({"text": f"🚨 SLO VIOLATION: {message}"}).encode()
            req = urllib.request.Request(cfg.webhook_url, data=payload,
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=5)
        except Exception as e:
            print(f"  Webhook failed: {e}")

def evaluate_slos(cfg: AlertConfig, window_s: float = 300):
    cutoff = time.time() - window_s
    con = sqlite3.connect(DB)
    rows = con.execute(
        "SELECT latency_s, is_error FROM calls WHERE ts>? ORDER BY latency_s",
        (cutoff,)
    ).fetchall()
    con.close()
    if len(rows) < 5:
        return  # Not enough data

    latencies = [r[0] for r in rows]
    errors = [r[1] for r in rows]
    p95 = latencies[int(len(latencies) * 0.95)]
    err_pct = sum(errors) / len(errors) * 100

    if p95 > cfg.p95_threshold_s:
        send_alert(cfg, "high_latency",
                   f"p95={p95:.2f}s exceeds {cfg.p95_threshold_s}s over {window_s//60}min window")
    if err_pct > cfg.error_threshold_pct:
        send_alert(cfg, "high_error_rate",
                   f"error_rate={err_pct:.1f}% exceeds {cfg.error_threshold_pct}%")

init_db()
cfg = AlertConfig(webhook_url=None, cooldown_s=60)

def call(prompt: str) -> str:
    start = time.monotonic()
    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=64,
            messages=[{"role": "user", "content": prompt}],
        )
        log_call(time.monotonic() - start, False)
        return resp.content[0].text
    except Exception as e:
        log_call(time.monotonic() - start, True)
        raise

for p in ["Explain REST", "What is gRPC?", "What is SOAP?", "Explain WebSockets"]:
    call(p)

evaluate_slos(cfg, window_s=300)

# Expected Token Savings: Alert suppression (cooldown) prevents notification storms on sustained issues
# Environment: set webhook_url to Slack/PagerDuty; cooldown_s prevents alert fatigue
```

## Comparison

| Option | Storage | Percentiles | Alert Mechanism |
|--------|---------|------------|----------------|
| 1 — In-Memory Rolling | deque | Sorted slice | Print only |
| 2 — SQLite Time-Window | SQLite | ORDER BY | Print + extensible |
| 3 — Multi-Endpoint Dashboard | SQLite | Sorted slice | Per-endpoint violations |
| 4 — Error Budget | SQLite | N/A | Burn rate alert |
| 5 — Histogram | In-memory | Exponential buckets | Print violations |
| 6 — Alert Webhook | SQLite | Sorted slice | Webhook + cooldown |
