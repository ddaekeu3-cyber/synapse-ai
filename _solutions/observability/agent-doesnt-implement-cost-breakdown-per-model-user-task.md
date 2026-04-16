---
layout: solution
title: "Agent Doesn't Implement Cost Breakdown per Model, User, and Task"
category: observability
description: "Track and report token costs attributed to specific models, users, and task types so teams can identify the most expensive operations and allocate costs accurately."
tags: [observability, cost, attribution, billing, token-tracking, analytics, multi-tenant]
---

# Agent Doesn't Implement Cost Breakdown per Model, User, and Task

## Problem

An agent accumulates API costs across dozens of users and task types but reports only a single monthly total. Engineering teams cannot identify which feature is most expensive, which user group drives 80% of token spend, or whether cost is dominated by input or output tokens. Without per-dimension cost attribution, optimization is guesswork and chargeback to business units is impossible.

## Solution Options

### Option 1: Simple Per-Call Cost Tracker

```python
import anthropic
from dataclasses import dataclass, field
from collections import defaultdict


# Pricing per 1K tokens (USD) — update from Anthropic pricing page
MODEL_PRICING = {
    "claude-haiku-4-5-20251001": {"input": 0.00025, "output": 0.00125},
    "claude-sonnet-4-6":         {"input": 0.003,   "output": 0.015},
    "claude-opus-4-6":           {"input": 0.015,   "output": 0.075},
}


@dataclass
class CostRecord:
    model: str
    user_id: str
    task_type: str
    input_tokens: int
    output_tokens: int

    @property
    def cost_usd(self) -> float:
        pricing = MODEL_PRICING.get(self.model, {"input": 0.003, "output": 0.015})
        return (
            self.input_tokens * pricing["input"] / 1000
            + self.output_tokens * pricing["output"] / 1000
        )


class CostTracker:
    def __init__(self) -> None:
        self._records: list[CostRecord] = []

    def record(self, record: CostRecord) -> None:
        self._records.append(record)

    def by_model(self) -> dict[str, float]:
        totals: dict[str, float] = defaultdict(float)
        for r in self._records:
            totals[r.model] += r.cost_usd
        return dict(totals)

    def by_user(self) -> dict[str, float]:
        totals: dict[str, float] = defaultdict(float)
        for r in self._records:
            totals[r.user_id] += r.cost_usd
        return dict(totals)

    def by_task(self) -> dict[str, float]:
        totals: dict[str, float] = defaultdict(float)
        for r in self._records:
            totals[r.task_type] += r.cost_usd
        return dict(totals)

    def total(self) -> float:
        return sum(r.cost_usd for r in self._records)

    def report(self) -> str:
        lines = [f"Total cost: ${self.total():.5f}"]
        lines.append("\nBy model:")
        for model, cost in sorted(self.by_model().items(), key=lambda x: -x[1]):
            lines.append(f"  {model:<35} ${cost:.5f}")
        lines.append("\nBy user:")
        for user, cost in sorted(self.by_user().items(), key=lambda x: -x[1]):
            lines.append(f"  {user:<20} ${cost:.5f}")
        lines.append("\nBy task:")
        for task, cost in sorted(self.by_task().items(), key=lambda x: -x[1]):
            lines.append(f"  {task:<25} ${cost:.5f}")
        return "\n".join(lines)


tracker = CostTracker()


def tracked_call(
    user_id: str,
    task_type: str,
    message: str,
    model: str = "claude-haiku-4-5-20251001",
    max_tokens: int = 256,
) -> str:
    client = anthropic.Anthropic()
    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": message}],
    )
    tracker.record(CostRecord(
        model=model,
        user_id=user_id,
        task_type=task_type,
        input_tokens=resp.usage.input_tokens,
        output_tokens=resp.usage.output_tokens,
    ))
    return resp.content[0].text


if __name__ == "__main__":
    tracked_call("alice", "summarization", "Summarize quantum computing in 2 sentences")
    tracked_call("bob",   "qa",            "What is the capital of Japan?")
    tracked_call("alice", "qa",            "Define machine learning")
    tracked_call("carol", "summarization", "Summarize the French Revolution briefly")

    print(tracker.report())

# Expected Token Savings: No extra tokens; cost attribution guides model downgrade decisions
# Environment: Any multi-user agent where per-user or per-feature cost visibility is needed
```

---

### Option 2: SQLite Cost Store with Aggregation Queries

```python
import anthropic
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass


MODEL_PRICING = {
    "claude-haiku-4-5-20251001": (0.00025, 0.00125),
    "claude-sonnet-4-6":         (0.003,   0.015),
    "claude-opus-4-6":           (0.015,   0.075),
}


@contextmanager
def get_db(path: str):
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


class SQLiteCostStore:
    def __init__(self, db_path: str = ":memory:") -> None:
        self.db_path = db_path
        with get_db(db_path) as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS cost_events (
                    id TEXT PRIMARY KEY,
                    ts REAL,
                    user_id TEXT,
                    task_type TEXT,
                    model TEXT,
                    input_tokens INTEGER,
                    output_tokens INTEGER,
                    cost_usd REAL
                )
            """)
            db.execute("CREATE INDEX IF NOT EXISTS idx_user ON cost_events(user_id)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_task ON cost_events(task_type)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_model ON cost_events(model)")

    def insert(self, user_id: str, task_type: str, model: str, input_tok: int, output_tok: int) -> None:
        pricing = MODEL_PRICING.get(model, (0.003, 0.015))
        cost = input_tok * pricing[0] / 1000 + output_tok * pricing[1] / 1000
        with get_db(self.db_path) as db:
            db.execute(
                "INSERT INTO cost_events VALUES (?,?,?,?,?,?,?,?)",
                (uuid.uuid4().hex, time.time(), user_id, task_type, model, input_tok, output_tok, cost),
            )

    def breakdown(self, group_by: str = "model") -> list[dict]:
        valid = {"model", "user_id", "task_type"}
        if group_by not in valid:
            raise ValueError(f"group_by must be one of {valid}")
        with get_db(self.db_path) as db:
            rows = db.execute(f"""
                SELECT {group_by} as dimension,
                       COUNT(*) as calls,
                       SUM(input_tokens) as total_input,
                       SUM(output_tokens) as total_output,
                       SUM(cost_usd) as total_cost
                FROM cost_events
                GROUP BY {group_by}
                ORDER BY total_cost DESC
            """).fetchall()
        return [dict(r) for r in rows]

    def top_users_by_cost(self, n: int = 5) -> list[dict]:
        with get_db(self.db_path) as db:
            rows = db.execute("""
                SELECT user_id, SUM(cost_usd) as total_cost, COUNT(*) as calls
                FROM cost_events GROUP BY user_id ORDER BY total_cost DESC LIMIT ?
            """, (n,)).fetchall()
        return [dict(r) for r in rows]

    def daily_spend(self) -> list[dict]:
        with get_db(self.db_path) as db:
            rows = db.execute("""
                SELECT DATE(ts, 'unixepoch') as day,
                       SUM(cost_usd) as spend,
                       COUNT(*) as calls
                FROM cost_events GROUP BY day ORDER BY day DESC LIMIT 30
            """).fetchall()
        return [dict(r) for r in rows]


store = SQLiteCostStore()


def tracked(user_id: str, task_type: str, prompt: str, model: str = "claude-haiku-4-5-20251001") -> str:
    client = anthropic.Anthropic()
    resp = client.messages.create(model=model, max_tokens=128, messages=[{"role": "user", "content": prompt}])
    store.insert(user_id, task_type, model, resp.usage.input_tokens, resp.usage.output_tokens)
    return resp.content[0].text


if __name__ == "__main__":
    calls = [
        ("alice", "qa",       "What is gravity?"),
        ("alice", "summarize","Summarize the solar system"),
        ("bob",   "qa",       "What is Python?"),
        ("carol", "generate", "Write a haiku about rain"),
        ("bob",   "qa",       "Define recursion"),
    ]
    for user, task, prompt in calls:
        tracked(user, task, prompt)

    print("=== By Model ===")
    for row in store.breakdown("model"):
        print(f"  {row['dimension']:<35} calls={row['calls']} cost=${row['total_cost']:.5f}")

    print("\n=== By User ===")
    for row in store.breakdown("user_id"):
        print(f"  {row['dimension']:<15} calls={row['calls']} cost=${row['total_cost']:.5f}")

    print("\n=== By Task ===")
    for row in store.breakdown("task_type"):
        print(f"  {row['dimension']:<15} calls={row['calls']} cost=${row['total_cost']:.5f}")

# Expected Token Savings: No extra tokens; SQL aggregation enables BI tool integration
# Environment: Production agents with chargeback requirements or per-feature cost tracking
```

---

### Option 3: Async Cost Dashboard with Real-Time Totals

```python
import anthropic
import asyncio
import time
from collections import defaultdict
from dataclasses import dataclass, field


MODEL_PRICING = {
    "claude-haiku-4-5-20251001": (0.00025, 0.00125),
    "claude-sonnet-4-6":         (0.003,   0.015),
    "claude-opus-4-6":           (0.015,   0.075),
}


@dataclass
class DimensionBucket:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    last_call: float = field(default_factory=time.time)

    def add(self, input_tok: int, output_tok: int, cost: float) -> None:
        self.calls += 1
        self.input_tokens += input_tok
        self.output_tokens += output_tok
        self.cost_usd += cost
        self.last_call = time.time()


class AsyncCostDashboard:
    def __init__(self) -> None:
        self._by_model: dict[str, DimensionBucket] = defaultdict(DimensionBucket)
        self._by_user: dict[str, DimensionBucket] = defaultdict(DimensionBucket)
        self._by_task: dict[str, DimensionBucket] = defaultdict(DimensionBucket)
        self._lock = asyncio.Lock()
        self._total_cost = 0.0
        self._total_calls = 0

    async def record(self, model: str, user_id: str, task_type: str, input_tok: int, output_tok: int) -> float:
        pricing = MODEL_PRICING.get(model, (0.003, 0.015))
        cost = input_tok * pricing[0] / 1000 + output_tok * pricing[1] / 1000
        async with self._lock:
            self._by_model[model].add(input_tok, output_tok, cost)
            self._by_user[user_id].add(input_tok, output_tok, cost)
            self._by_task[task_type].add(input_tok, output_tok, cost)
            self._total_cost += cost
            self._total_calls += 1
        return cost

    def snapshot(self) -> dict:
        return {
            "total_calls": self._total_calls,
            "total_cost_usd": round(self._total_cost, 5),
            "by_model": {k: {"calls": v.calls, "cost": round(v.cost_usd, 5)} for k, v in self._by_model.items()},
            "by_user":  {k: {"calls": v.calls, "cost": round(v.cost_usd, 5)} for k, v in self._by_user.items()},
            "by_task":  {k: {"calls": v.calls, "cost": round(v.cost_usd, 5)} for k, v in self._by_task.items()},
        }

    def top_cost_users(self, n: int = 3) -> list[tuple[str, float]]:
        return sorted(
            [(user, b.cost_usd) for user, b in self._by_user.items()],
            key=lambda x: -x[1],
        )[:n]


dashboard = AsyncCostDashboard()


async def async_tracked(user_id: str, task_type: str, prompt: str, model: str = "claude-haiku-4-5-20251001") -> str:
    client = anthropic.AsyncAnthropic()
    resp = await client.messages.create(model=model, max_tokens=128, messages=[{"role": "user", "content": prompt}])
    cost = await dashboard.record(model, user_id, task_type, resp.usage.input_tokens, resp.usage.output_tokens)
    await client.close()
    return resp.content[0].text


async def main() -> None:
    calls = [
        ("alice", "qa",        "What is the speed of light?"),
        ("alice", "summarize", "Summarize the water cycle"),
        ("bob",   "generate",  "Write a short poem about autumn"),
        ("carol", "qa",        "What is DNA?"),
        ("bob",   "qa",        "Define a binary tree"),
        ("carol", "summarize", "Summarize photosynthesis"),
    ]
    await asyncio.gather(*[async_tracked(u, t, p) for u, t, p in calls])

    snap = dashboard.snapshot()
    print(f"Total: {snap['total_calls']} calls, ${snap['total_cost_usd']:.5f}")
    print("\nBy model:", snap["by_model"])
    print("By user:", snap["by_user"])
    print("By task:", snap["by_task"])
    print("\nTop cost users:", dashboard.top_cost_users())


if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: No extra tokens; real-time dashboard enables live cost monitoring
# Environment: Async agents with concurrent users needing live cost visibility
```

---

### Option 4: Budget Enforcement with Per-User Cost Caps

```python
import anthropic
from dataclasses import dataclass, field
from collections import defaultdict


MODEL_PRICING = {
    "claude-haiku-4-5-20251001": (0.00025, 0.00125),
    "claude-sonnet-4-6":         (0.003,   0.015),
    "claude-opus-4-6":           (0.015,   0.075),
}


@dataclass
class UserBudget:
    user_id: str
    daily_limit_usd: float
    spent_today: float = 0.0
    calls_today: int = 0
    blocked_calls: int = 0

    @property
    def remaining(self) -> float:
        return max(0.0, self.daily_limit_usd - self.spent_today)

    @property
    def is_over_budget(self) -> bool:
        return self.spent_today >= self.daily_limit_usd


class BudgetEnforcingTracker:
    """
    Tracks cost per user and enforces daily spending caps.
    Over-budget users receive a degraded model or rejection.
    """

    def __init__(self) -> None:
        self._budgets: dict[str, UserBudget] = {}
        self._untracked_cost: float = 0.0

    def set_budget(self, user_id: str, daily_limit_usd: float) -> None:
        self._budgets[user_id] = UserBudget(user_id=user_id, daily_limit_usd=daily_limit_usd)

    def _get_budget(self, user_id: str) -> UserBudget:
        if user_id not in self._budgets:
            self._budgets[user_id] = UserBudget(user_id=user_id, daily_limit_usd=0.10)
        return self._budgets[user_id]

    def _compute_cost(self, model: str, input_tok: int, output_tok: int) -> float:
        pricing = MODEL_PRICING.get(model, (0.003, 0.015))
        return input_tok * pricing[0] / 1000 + output_tok * pricing[1] / 1000

    def call(self, user_id: str, task_type: str, prompt: str, preferred_model: str = "claude-sonnet-4-6") -> dict:
        client = anthropic.Anthropic()
        budget = self._get_budget(user_id)

        # Select model based on remaining budget
        if budget.is_over_budget:
            budget.blocked_calls += 1
            return {"status": "blocked", "reason": f"Daily budget ${budget.daily_limit_usd:.3f} exhausted"}

        # Downgrade model if budget is low
        model = preferred_model
        if budget.remaining < 0.01 and preferred_model != "claude-haiku-4-5-20251001":
            model = "claude-haiku-4-5-20251001"
            print(f"[budget] {user_id} downgraded to haiku (remaining=${budget.remaining:.4f})")

        resp = client.messages.create(model=model, max_tokens=128, messages=[{"role": "user", "content": prompt}])
        cost = self._compute_cost(model, resp.usage.input_tokens, resp.usage.output_tokens)
        budget.spent_today += cost
        budget.calls_today += 1

        return {
            "status": "ok",
            "model": model,
            "cost": round(cost, 5),
            "remaining": round(budget.remaining, 5),
            "response": resp.content[0].text[:60],
        }

    def report(self) -> str:
        lines = ["=== Budget Report ==="]
        for uid, b in sorted(self._budgets.items()):
            pct = b.spent_today / b.daily_limit_usd * 100 if b.daily_limit_usd > 0 else 0
            lines.append(
                f"  {uid:<12} spent=${b.spent_today:.4f}/{b.daily_limit_usd:.3f} "
                f"({pct:.0f}%) calls={b.calls_today} blocked={b.blocked_calls}"
            )
        return "\n".join(lines)


if __name__ == "__main__":
    tracker = BudgetEnforcingTracker()
    tracker.set_budget("alice", daily_limit_usd=0.05)
    tracker.set_budget("bob",   daily_limit_usd=0.001)  # very tight

    for user, prompt in [
        ("alice", "What is quantum computing?"),
        ("alice", "Explain neural networks"),
        ("bob",   "What is AI?"),
        ("bob",   "Define recursion"),   # likely blocked
        ("alice", "What is blockchain?"),
    ]:
        result = tracker.call(user, "qa", prompt)
        status = result["status"]
        cost = result.get("cost", 0)
        print(f"[{user}] {status} cost=${cost:.5f}: {result.get('response', result.get('reason', ''))[:50]}")

    print("\n" + tracker.report())

# Expected Token Savings: Over-budget users auto-downgraded or blocked; prevents runaway per-user spend
# Environment: SaaS agents with free/paid tiers enforcing per-user daily spending limits
```

---

### Option 5: Cost Breakdown by Task Pipeline Stage

```python
import anthropic
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from collections import defaultdict


MODEL_PRICING = {
    "claude-haiku-4-5-20251001": (0.00025, 0.00125),
    "claude-sonnet-4-6":         (0.003,   0.015),
    "claude-opus-4-6":           (0.015,   0.075),
}


@dataclass
class StageMetrics:
    stage: str
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    total_ms: float = 0.0

    @property
    def avg_ms(self) -> float:
        return self.total_ms / max(self.calls, 1)

    @property
    def cost_per_call(self) -> float:
        return self.cost_usd / max(self.calls, 1)


class PipelineCostProfiler:
    """
    Tracks cost and latency broken down by pipeline stage.
    Reveals whether cost is dominated by retrieval, generation, or verification.
    """

    def __init__(self) -> None:
        self._stages: dict[str, StageMetrics] = defaultdict(lambda: StageMetrics(""))
        self._client = anthropic.Anthropic()

    @contextmanager
    def stage(self, stage_name: str, model: str = "claude-haiku-4-5-20251001"):
        """Context manager that tracks cost for a named pipeline stage."""
        metrics = self._stages[stage_name]
        metrics.stage = stage_name
        start = time.perf_counter()
        ctx = {"model": model}
        try:
            yield ctx
        finally:
            elapsed_ms = (time.perf_counter() - start) * 1000
            input_tok = ctx.get("input_tokens", 0)
            output_tok = ctx.get("output_tokens", 0)
            pricing = MODEL_PRICING.get(model, (0.003, 0.015))
            cost = input_tok * pricing[0] / 1000 + output_tok * pricing[1] / 1000
            metrics.calls += 1
            metrics.input_tokens += input_tok
            metrics.output_tokens += output_tok
            metrics.cost_usd += cost
            metrics.total_ms += elapsed_ms

    def run_stage(self, stage_name: str, prompt: str, model: str = "claude-haiku-4-5-20251001", max_tokens: int = 128) -> str:
        with self.stage(stage_name, model) as ctx:
            resp = self._client.messages.create(model=model, max_tokens=max_tokens, messages=[{"role": "user", "content": prompt}])
            ctx["input_tokens"] = resp.usage.input_tokens
            ctx["output_tokens"] = resp.usage.output_tokens
            return resp.content[0].text.strip()

    def report(self) -> str:
        total = sum(m.cost_usd for m in self._stages.values())
        lines = [
            f"{'Stage':<25} {'Calls':>6} {'In':>7} {'Out':>7} {'Cost':>9} {'%Total':>7} {'AvgMs':>8}",
            "-" * 72,
        ]
        for name, m in sorted(self._stages.items(), key=lambda x: -x[1].cost_usd):
            pct = m.cost_usd / total * 100 if total > 0 else 0
            lines.append(
                f"{name:<25} {m.calls:>6} {m.input_tokens:>7} {m.output_tokens:>7} "
                f"${m.cost_usd:>8.5f} {pct:>6.1f}% {m.avg_ms:>7.0f}ms"
            )
        lines.append(f"\nTotal: ${total:.5f}")
        return "\n".join(lines)


def run_rag_pipeline(query: str, profiler: PipelineCostProfiler) -> str:
    # Stage 1: Intent classification
    intent = profiler.run_stage("intent_classification", f"Classify this query in 2 words: {query}", max_tokens=10)

    # Stage 2: Query expansion
    expanded = profiler.run_stage("query_expansion", f"Expand this query for search: {query}", max_tokens=30)

    # Stage 3: Answer generation
    answer = profiler.run_stage("answer_generation", f"Answer: {expanded}", max_tokens=128)

    # Stage 4: Answer verification
    profiler.run_stage("answer_verification", f"Is this answer complete? '{answer[:80]}'", max_tokens=20)

    return answer


if __name__ == "__main__":
    profiler = PipelineCostProfiler()
    for query in ["What is machine learning?", "Explain gradient descent", "What is a neural network?"]:
        run_rag_pipeline(query, profiler)

    print(profiler.report())

# Expected Token Savings: No extra tokens; stage-level cost reveals which step to optimize first
# Environment: Multi-stage RAG or agentic pipelines where cost optimization requires stage attribution
```

---

### Option 6: Cost Exporter for Prometheus / Grafana

```python
import anthropic
import time
from collections import defaultdict
from dataclasses import dataclass, field


MODEL_PRICING = {
    "claude-haiku-4-5-20251001": (0.00025, 0.00125),
    "claude-sonnet-4-6":         (0.003,   0.015),
    "claude-opus-4-6":           (0.015,   0.075),
}


@dataclass
class MetricSeries:
    labels: dict[str, str]
    value: float


class PrometheusExporter:
    """
    Collects cost metrics and exports them in Prometheus text format.
    Designed to integrate with existing Grafana dashboards.
    """

    def __init__(self) -> None:
        self._counters: dict[str, float] = defaultdict(float)
        self._call_counts: dict[str, int] = defaultdict(int)

    def _key(self, **labels) -> str:
        return ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()))

    def record(self, model: str, user_id: str, task_type: str, input_tok: int, output_tok: int) -> float:
        pricing = MODEL_PRICING.get(model, (0.003, 0.015))
        cost = input_tok * pricing[0] / 1000 + output_tok * pricing[1] / 1000

        # Aggregate counters
        for key_kwargs, increment in [
            ({"dimension": "model",     "value": model},     cost),
            ({"dimension": "user",      "value": user_id},   cost),
            ({"dimension": "task_type", "value": task_type}, cost),
        ]:
            k = self._key(**key_kwargs)
            self._counters[k] += increment
            self._call_counts[k] += 1

        # Token counters
        self._counters[self._key(model=model, token_type="input")]  += input_tok
        self._counters[self._key(model=model, token_type="output")] += output_tok
        return cost

    def export_prometheus(self) -> str:
        lines = []
        ts = int(time.time() * 1000)

        lines.append("# HELP agent_cost_usd_total Total USD cost attributed per dimension")
        lines.append("# TYPE agent_cost_usd_total counter")
        for key, value in sorted(self._counters.items()):
            if "token_type" not in key:
                lines.append(f"agent_cost_usd_total{{{key}}} {value:.6f} {ts}")

        lines.append("\n# HELP agent_api_calls_total Total API calls per dimension")
        lines.append("# TYPE agent_api_calls_total counter")
        for key, count in sorted(self._call_counts.items()):
            lines.append(f"agent_api_calls_total{{{key}}} {count} {ts}")

        lines.append("\n# HELP agent_tokens_total Total tokens consumed by model and type")
        lines.append("# TYPE agent_tokens_total counter")
        for key, value in sorted(self._counters.items()):
            if "token_type" in key:
                lines.append(f"agent_tokens_total{{{key}}} {int(value)} {ts}")

        return "\n".join(lines)


exporter = PrometheusExporter()


def tracked_call(user_id: str, task_type: str, prompt: str, model: str = "claude-haiku-4-5-20251001") -> str:
    client = anthropic.Anthropic()
    resp = client.messages.create(model=model, max_tokens=128, messages=[{"role": "user", "content": prompt}])
    exporter.record(model, user_id, task_type, resp.usage.input_tokens, resp.usage.output_tokens)
    return resp.content[0].text


if __name__ == "__main__":
    calls = [
        ("alice", "qa",       "What is recursion?"),
        ("bob",   "summarize","Summarize blockchain"),
        ("alice", "generate", "Write a haiku"),
        ("carol", "qa",       "Define entropy"),
        ("bob",   "qa",       "What is REST?"),
    ]
    for user, task, prompt in calls:
        tracked_call(user, task, prompt)

    print(exporter.export_prometheus())

# Expected Token Savings: No extra tokens; Prometheus format feeds existing Grafana dashboards directly
# Environment: Production agents with existing Prometheus/Grafana observability infrastructure
```

---

## Comparison

| Option | Approach | Best For | Storage | Integration |
|--------|----------|----------|---------|-------------|
| 1 | In-memory counters by model/user/task | Quick cost visibility in dev | RAM | None |
| 2 | SQLite store with SQL aggregation | Persistent cost history + BI tools | SQLite | SQL queries |
| 3 | Async real-time dashboard | Live concurrent cost monitoring | RAM | None |
| 4 | Per-user budget enforcement | Tiered SaaS with spending caps | RAM | None |
| 5 | Pipeline stage cost profiler | Stage-level optimization targeting | RAM | None |
| 6 | Prometheus text format exporter | Grafana dashboard integration | RAM | Prometheus/Grafana |
