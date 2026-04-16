---
layout: solution
title: "Agent Doesn't Implement Token Usage Cost Dashboard"
category: observability
description: "Track, aggregate, and visualize token consumption and estimated costs per model, session, user, and time window—enabling cost attribution, budget enforcement, and spend optimization."
tags: [observability, cost, token-usage, dashboard, attribution]
---

# Agent Doesn't Implement Token Usage Cost Dashboard

## Problem

Without cost tracking, teams discover unexpectedly large API bills at month-end with no ability to attribute spend to specific users, features, or agent behaviors. Optimization is impossible without knowing where tokens are spent.

## Solution Options

### Option 1: Per-Request Cost Meter with Running Totals

```python
import anthropic
import time
from dataclasses import dataclass, field
from collections import defaultdict

client = anthropic.Anthropic()

# Prices per million tokens (USD)
PRICING = {
    "claude-haiku-4-5-20251001": {"input": 0.80,  "output": 4.00},
    "claude-sonnet-4-6":          {"input": 3.00,  "output": 15.00},
    "claude-opus-4-6":            {"input": 15.00, "output": 75.00},
}

@dataclass
class CostMeter:
    totals: dict = field(default_factory=lambda: defaultdict(lambda: {"input": 0, "output": 0, "cost_usd": 0.0, "calls": 0}))

    def record(self, model: str, input_tokens: int, output_tokens: int) -> float:
        prices = PRICING.get(model, {"input": 3.0, "output": 15.0})
        cost = (input_tokens * prices["input"] + output_tokens * prices["output"]) / 1_000_000
        self.totals[model]["input"] += input_tokens
        self.totals[model]["output"] += output_tokens
        self.totals[model]["cost_usd"] += cost
        self.totals[model]["calls"] += 1
        return cost

    def print_dashboard(self) -> None:
        print(f"\n{'Model':<30} {'Calls':>6} {'Input':>8} {'Output':>8} {'Cost USD':>10}")
        print("-" * 68)
        grand_total_cost = 0.0
        for model, stats in sorted(self.totals.items()):
            print(f"{model:<30} {stats['calls']:>6} {stats['input']:>8} {stats['output']:>8} ${stats['cost_usd']:>9.6f}")
            grand_total_cost += stats['cost_usd']
        print("-" * 68)
        print(f"{'TOTAL':<30} {'':>6} {'':>8} {'':>8} ${grand_total_cost:>9.6f}")

meter = CostMeter()

def tracked_call(prompt: str, model: str = "claude-haiku-4-5-20251001") -> str:
    resp = client.messages.create(
        model=model,
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}]
    )
    cost = meter.record(model, resp.usage.input_tokens, resp.usage.output_tokens)
    print(f"  [{model.split('-')[1]}] {resp.usage.input_tokens}+{resp.usage.output_tokens}t = ${cost:.6f}")
    return resp.content[0].text

for prompt in ["What is Redis?", "Explain Kafka in one paragraph.", "What is a Bloom filter?"]:
    tracked_call(prompt, "claude-haiku-4-5-20251001")

tracked_call("Compare Redis and Memcached in depth.", "claude-sonnet-4-6")

meter.print_dashboard()

# Expected Token Savings: cost visibility drives model tier selection; typically 60-80% savings switching to haiku
# Environment: any production agent; prerequisite for cost optimization
```

### Option 2: Session-Scoped Cost Attribution

```python
import anthropic
import time
import uuid
from dataclasses import dataclass, field
from collections import defaultdict

client = anthropic.Anthropic()

PRICING = {
    "claude-haiku-4-5-20251001": (0.80, 4.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-opus-4-6": (15.00, 75.00),
}

@dataclass
class SessionStats:
    session_id: str
    user_id: str
    feature: str
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    started_at: float = field(default_factory=time.time)

    def record(self, model: str, inp: int, out: int) -> None:
        prices = PRICING.get(model, (3.0, 15.0))
        self.calls += 1
        self.input_tokens += inp
        self.output_tokens += out
        self.cost_usd += (inp * prices[0] + out * prices[1]) / 1_000_000

class CostDashboard:
    def __init__(self):
        self.sessions: dict[str, SessionStats] = {}
        self.by_user: defaultdict = defaultdict(float)
        self.by_feature: defaultdict = defaultdict(float)

    def create_session(self, user_id: str, feature: str) -> str:
        session_id = str(uuid.uuid4())[:8]
        self.sessions[session_id] = SessionStats(session_id=session_id, user_id=user_id, feature=feature)
        return session_id

    def record(self, session_id: str, model: str, inp: int, out: int) -> float:
        session = self.sessions.get(session_id)
        if not session:
            return 0.0
        session.record(model, inp, out)
        self.by_user[session.user_id] = sum(
            s.cost_usd for s in self.sessions.values() if s.user_id == session.user_id
        )
        self.by_feature[session.feature] = sum(
            s.cost_usd for s in self.sessions.values() if s.feature == session.feature
        )
        return session.cost_usd

    def print_report(self) -> None:
        print("\n=== Cost by User ===")
        for user, cost in sorted(self.by_user.items(), key=lambda x: -x[1]):
            print(f"  {user:<15} ${cost:.6f}")
        print("\n=== Cost by Feature ===")
        for feat, cost in sorted(self.by_feature.items(), key=lambda x: -x[1]):
            print(f"  {feat:<20} ${cost:.6f}")
        print("\n=== Active Sessions ===")
        for s in self.sessions.values():
            duration = round(time.time() - s.started_at, 1)
            print(f"  [{s.session_id}] user={s.user_id} feature={s.feature} calls={s.calls} ${s.cost_usd:.6f} ({duration}s)")

dashboard = CostDashboard()

# Simulate multiple users using different features
scenarios = [
    ("user_alice", "chat"),
    ("user_alice", "search"),
    ("user_bob", "chat"),
    ("user_charlie", "analysis"),
]

for user_id, feature in scenarios:
    sid = dashboard.create_session(user_id, feature)
    prompts = ["What is caching?", "Explain LRU."] if feature != "analysis" else ["Analyze distributed systems tradeoffs."]
    model = "claude-haiku-4-5-20251001" if feature != "analysis" else "claude-sonnet-4-6"

    for prompt in prompts:
        resp = client.messages.create(
            model=model,
            max_tokens=128,
            messages=[{"role": "user", "content": prompt}]
        )
        session_cost = dashboard.record(sid, model, resp.usage.input_tokens, resp.usage.output_tokens)

dashboard.print_report()

# Expected Token Savings: per-user attribution enables quota enforcement; per-feature reveals cost drivers
# Environment: SaaS platforms, multi-tenant agents, feature cost analysis
```

### Option 3: Time-Window Cost Aggregation with Budget Alerts

```python
import anthropic
import time
from collections import deque
from dataclasses import dataclass, field

client = anthropic.Anthropic()

PRICING = {"claude-haiku-4-5-20251001": (0.80, 4.00), "claude-sonnet-4-6": (3.00, 15.00)}

@dataclass
class CostEvent:
    timestamp: float
    model: str
    cost_usd: float
    input_tokens: int
    output_tokens: int

@dataclass
class BudgetAlert:
    window_seconds: int
    limit_usd: float
    fired: bool = False

    def check(self, window_cost: float) -> bool:
        should_fire = window_cost > self.limit_usd
        if should_fire and not self.fired:
            self.fired = True
            print(f"  [BUDGET ALERT] ${window_cost:.4f} > ${self.limit_usd:.4f} in {self.window_seconds}s window")
        elif not should_fire:
            self.fired = False
        return should_fire

class WindowedCostTracker:
    def __init__(self, windows: list[int], alerts: list[BudgetAlert] | None = None):
        self.windows = windows  # seconds
        self.events: deque[CostEvent] = deque()
        self.alerts = alerts or []

    def record(self, model: str, inp: int, out: int) -> float:
        prices = PRICING.get(model, (3.0, 15.0))
        cost = (inp * prices[0] + out * prices[1]) / 1_000_000
        self.events.append(CostEvent(time.time(), model, cost, inp, out))
        # Check alerts against shortest window
        min_window = min(self.windows)
        window_cost = self._window_cost(min_window)
        for alert in self.alerts:
            if alert.window_seconds == min_window:
                alert.check(window_cost)
        return cost

    def _window_cost(self, window_seconds: int) -> float:
        cutoff = time.time() - window_seconds
        return sum(e.cost_usd for e in self.events if e.timestamp > cutoff)

    def print_windows(self) -> None:
        print("\n=== Cost Windows ===")
        for w in self.windows:
            cost = self._window_cost(w)
            label = f"{w}s" if w < 60 else f"{w//60}m"
            print(f"  Last {label:<6}: ${cost:.6f}")
        total = sum(e.cost_usd for e in self.events)
        print(f"  All time    : ${total:.6f}")

tracker = WindowedCostTracker(
    windows=[60, 300, 3600],
    alerts=[BudgetAlert(window_seconds=60, limit_usd=0.001)]
)

for i, prompt in enumerate([
    "What is database indexing?",
    "Explain B-tree vs hash index.",
    "What is a covering index?",
    "When should I use partial indexes?",
    "What is index bloat?"
]):
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{"role": "user", "content": prompt}]
    )
    cost = tracker.record("claude-haiku-4-5-20251001", resp.usage.input_tokens, resp.usage.output_tokens)
    print(f"  Call {i+1}: ${cost:.6f}")

tracker.print_windows()

# Expected Token Savings: window alerts enable proactive throttling before budget exhaustion
# Environment: budget-capped deployments, on-demand cost alerts, rate-limited API consumers
```

### Option 4: SQLite Cost Log with Query API

```python
import anthropic
import sqlite3
import time
import uuid
from contextlib import contextmanager

client = anthropic.Anthropic()

PRICING = {
    "claude-haiku-4-5-20251001": (0.80, 4.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-opus-4-6": (15.00, 75.00),
}

@contextmanager
def get_db(path: str = "/tmp/agent_costs.db"):
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS usage (
            id TEXT PRIMARY KEY,
            ts REAL,
            model TEXT,
            user_id TEXT,
            feature TEXT,
            input_tokens INTEGER,
            output_tokens INTEGER,
            cost_usd REAL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ts ON usage(ts)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_user ON usage(user_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_feature ON usage(feature)")
    conn.commit()
    try:
        yield conn
    finally:
        conn.close()

def log_usage(conn: sqlite3.Connection, model: str, inp: int, out: int,
               user_id: str = "anonymous", feature: str = "default") -> float:
    prices = PRICING.get(model, (3.0, 15.0))
    cost = (inp * prices[0] + out * prices[1]) / 1_000_000
    conn.execute(
        "INSERT INTO usage VALUES (?,?,?,?,?,?,?,?)",
        (str(uuid.uuid4())[:8], time.time(), model, user_id, feature, inp, out, round(cost, 8))
    )
    conn.commit()
    return cost

def query_cost_report(conn: sqlite3.Connection, since_seconds: int = 3600) -> None:
    since = time.time() - since_seconds
    rows = conn.execute(
        "SELECT model, user_id, feature, SUM(input_tokens), SUM(output_tokens), SUM(cost_usd), COUNT(*) "
        "FROM usage WHERE ts > ? GROUP BY model, user_id, feature ORDER BY SUM(cost_usd) DESC",
        (since,)
    ).fetchall()
    print(f"\n=== Usage Report (last {since_seconds//60}min) ===")
    print(f"{'Model':<25} {'User':<12} {'Feature':<12} {'Calls':>5} {'Tokens':>8} {'Cost':>10}")
    print("-" * 80)
    for model, user, feat, inp, out, cost, calls in rows:
        print(f"{model:<25} {user:<12} {feat:<12} {calls:>5} {inp+out:>8} ${cost:>9.6f}")
    total = sum(r[5] for r in rows)
    print(f"\nTotal: ${total:.6f}")

with get_db() as conn:
    calls = [
        ("What is Redis?",        "user_a", "chat",     "claude-haiku-4-5-20251001"),
        ("Explain Kafka.",         "user_a", "chat",     "claude-haiku-4-5-20251001"),
        ("Search: caching",        "user_b", "search",   "claude-haiku-4-5-20251001"),
        ("Analyze architecture.",  "user_b", "analysis", "claude-sonnet-4-6"),
        ("What is B-tree?",        "user_c", "chat",     "claude-haiku-4-5-20251001"),
    ]
    for prompt, user, feature, model in calls:
        resp = client.messages.create(
            model=model,
            max_tokens=128,
            messages=[{"role": "user", "content": prompt}]
        )
        cost = log_usage(conn, model, resp.usage.input_tokens, resp.usage.output_tokens, user, feature)
        print(f"  [{user}/{feature}] ${cost:.6f}")

    query_cost_report(conn, since_seconds=3600)

# Expected Token Savings: SQL queries enable cost drill-down without re-processing all events
# Environment: billing systems, team cost dashboards, feature-level cost optimization
```

### Option 5: Cost Projection and Budget Forecasting

```python
import anthropic
import time
import statistics
from dataclasses import dataclass, field
from collections import deque

client = anthropic.Anthropic()
PRICING = {"claude-haiku-4-5-20251001": (0.80, 4.00), "claude-sonnet-4-6": (3.00, 15.00)}

@dataclass
class CostForecaster:
    monthly_budget_usd: float
    hourly_costs: deque = field(default_factory=lambda: deque(maxlen=24))
    session_start: float = field(default_factory=time.time)
    total_cost: float = 0.0
    call_count: int = 0

    def record(self, model: str, inp: int, out: int) -> float:
        prices = PRICING.get(model, (3.0, 15.0))
        cost = (inp * prices[0] + out * prices[1]) / 1_000_000
        self.total_cost += cost
        self.call_count += 1
        return cost

    def flush_hour_bucket(self) -> None:
        elapsed_hours = (time.time() - self.session_start) / 3600
        if elapsed_hours > 0:
            hourly_rate = self.total_cost / elapsed_hours
            self.hourly_costs.append(hourly_rate)

    def project_monthly_cost(self) -> dict:
        elapsed_hours = max((time.time() - self.session_start) / 3600, 0.0001)
        current_hourly_rate = self.total_cost / elapsed_hours
        hours_in_month = 730.0

        # Simple linear projection
        projected = current_hourly_rate * hours_in_month

        # Trend-adjusted projection using recent samples
        if len(self.hourly_costs) >= 2:
            trend_rate = statistics.mean(list(self.hourly_costs)[-3:])
            trend_projected = trend_rate * hours_in_month
        else:
            trend_projected = projected

        budget_remaining = self.monthly_budget_usd - projected
        days_until_exhausted = (self.monthly_budget_usd - self.total_cost) / max(current_hourly_rate * 24, 0.0001)

        return {
            "current_spend_usd": round(self.total_cost, 6),
            "hourly_rate_usd": round(current_hourly_rate, 6),
            "projected_monthly_usd": round(projected, 4),
            "trend_projected_monthly_usd": round(trend_projected, 4),
            "budget_remaining_usd": round(budget_remaining, 4),
            "days_until_budget_exhausted": round(days_until_exhausted, 1),
            "on_track": projected <= self.monthly_budget_usd,
            "calls_per_hour": round(self.call_count / elapsed_hours, 1)
        }

forecaster = CostForecaster(monthly_budget_usd=10.00)

for i, prompt in enumerate([
    "What is Redis?", "Explain Kafka.", "What is sharding?",
    "Explain MVCC.", "What is WAL?", "What is a B-tree?"
]):
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{"role": "user", "content": prompt}]
    )
    cost = forecaster.record("claude-haiku-4-5-20251001", resp.usage.input_tokens, resp.usage.output_tokens)

import json
forecast = forecaster.project_monthly_cost()
print("\n=== Cost Forecast ===")
for k, v in forecast.items():
    print(f"  {k:<35}: {v}")

# Expected Token Savings: forecast drives model tier decisions before budget exhaustion
# Environment: production cost governance, budget planning, automated tier downgrade triggers
```

### Option 6: Cost-Per-Feature Heatmap with Optimization Recommendations

```python
import anthropic
import time
from collections import defaultdict
from dataclasses import dataclass, field

client = anthropic.Anthropic()
PRICING = {"claude-haiku-4-5-20251001": (0.80, 4.00), "claude-sonnet-4-6": (3.00, 15.00)}

@dataclass
class FeatureCostProfile:
    feature: str
    calls: int = 0
    total_cost: float = 0.0
    avg_input_tokens: float = 0.0
    avg_output_tokens: float = 0.0
    models_used: dict = field(default_factory=lambda: defaultdict(int))
    _total_input: int = 0
    _total_output: int = 0

    def record(self, model: str, inp: int, out: int) -> None:
        prices = PRICING.get(model, (3.0, 15.0))
        cost = (inp * prices[0] + out * prices[1]) / 1_000_000
        self.calls += 1
        self.total_cost += cost
        self._total_input += inp
        self._total_output += out
        self.avg_input_tokens = self._total_input / self.calls
        self.avg_output_tokens = self._total_output / self.calls
        self.models_used[model] += 1

    def haiku_savings(self) -> float:
        """Estimated savings if all calls used haiku."""
        haiku_cost = (self._total_input * 0.80 + self._total_output * 4.00) / 1_000_000
        return max(0, self.total_cost - haiku_cost)

    def recommend(self) -> str:
        if self.avg_output_tokens < 100 and self.haiku_savings() > 0.0001:
            return f"DOWNGRADE to haiku (save ~${self.haiku_savings():.4f})"
        elif self.avg_input_tokens > 2000:
            return "OPTIMIZE: high input tokens — consider context compression"
        elif self.calls > 100:
            return "CACHE: high call volume — consider response caching"
        return "OK"

class FeatureHeatmap:
    def __init__(self):
        self.profiles: dict[str, FeatureCostProfile] = {}

    def record(self, feature: str, model: str, inp: int, out: int) -> None:
        if feature not in self.profiles:
            self.profiles[feature] = FeatureCostProfile(feature=feature)
        self.profiles[feature].record(model, inp, out)

    def print_heatmap(self) -> None:
        sorted_features = sorted(self.profiles.values(), key=lambda p: p.total_cost, reverse=True)
        total_all = sum(p.total_cost for p in sorted_features)
        print(f"\n{'Feature':<20} {'Calls':>6} {'Avg In':>7} {'Avg Out':>7} {'Cost':>10} {'% Total':>8} {'Recommendation'}")
        print("-" * 100)
        for p in sorted_features:
            pct = p.total_cost / max(total_all, 0.000001) * 100
            bar = "█" * int(pct / 5)
            print(f"{p.feature:<20} {p.calls:>6} {p.avg_input_tokens:>7.0f} {p.avg_output_tokens:>7.0f} ${p.total_cost:>9.6f} {pct:>7.1f}% {p.recommend()}")
        print(f"\n{'TOTAL':<20} {'':>6} {'':>7} {'':>7} ${total_all:>9.6f}")

heatmap = FeatureHeatmap()

# Simulate different features with different usage patterns
scenarios = [
    ("chat",        "What is Redis?",                      "claude-haiku-4-5-20251001", 128),
    ("chat",        "Explain Kafka briefly.",              "claude-haiku-4-5-20251001", 128),
    ("search",      "Summarize: " + "x" * 300,           "claude-haiku-4-5-20251001", 64),
    ("search",      "Summarize: " + "y" * 300,           "claude-haiku-4-5-20251001", 64),
    ("analysis",    "Analyze tradeoffs of microservices.", "claude-sonnet-4-6",          512),
    ("code-review", "Review this Python function.",       "claude-sonnet-4-6",          384),
    ("chat",        "What is a bloom filter?",            "claude-haiku-4-5-20251001", 128),
]

for feature, prompt, model, max_tokens in scenarios:
    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}]
    )
    heatmap.record(feature, model, resp.usage.input_tokens, resp.usage.output_tokens)

heatmap.print_heatmap()

# Expected Token Savings: heatmap surfaces top cost drivers; recommendations typically identify 30-60% savings
# Environment: product teams optimizing AI spend, cost review meetings, quarterly efficiency initiatives
```

## Comparison

| Option | Attribution Level | Storage | Forecasting | Best For |
|--------|------------------|---------|-------------|----------|
| 1 | Per-model totals | In-memory | No | Quick cost audit |
| 2 | Per-user, per-feature | In-memory | No | SaaS cost attribution |
| 3 | Time windows + alerts | In-memory deque | No | Real-time budget alerts |
| 4 | Full audit log | SQLite | No | Billing and compliance |
| 5 | Session-level projection | In-memory | Yes | Budget forecasting |
| 6 | Feature heatmap + advice | In-memory | No | Cost optimization roadmap |
