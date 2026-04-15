---
layout: solution
title: "Agent Doesn't Implement Per-Session Cost Attribution"
category: token-cost
description: "Agents accumulate API costs without tracking which session, user, or feature generated them. Without cost attribution, you cannot identify runaway sessions, enforce per-user budgets, or produce accurate billing."
tags: [token-cost, cost-tracking, attribution, billing, budget, multi-tenant]
---

# Agent Doesn't Implement Per-Session Cost Attribution

## Problem

When multiple users, features, or workflows share the same API key, costs are invisible at the session level. You see only a monthly bill with no breakdown. This makes it impossible to identify which user triggered a $200 session, enforce per-tenant spend limits, or charge back costs to business units. Runaway agents go undetected until the invoice arrives.

## Why This Happens

The Anthropic API returns token usage per response (`usage.input_tokens`, `usage.output_tokens`), but nothing aggregates this across a session or assigns it to a logical owner. Without explicit tracking code, every call's cost evaporates after the response is processed.

## Solutions

### Option 1: In-Memory Session Ledger — Track costs per session in a simple dict

```python
import anthropic
from dataclasses import dataclass, field
from datetime import datetime

# Approximate pricing per million tokens (update as needed)
PRICING = {
    "claude-haiku-4-5-20251001":  {"input": 0.80,  "output": 4.00},
    "claude-sonnet-4-6":          {"input": 3.00,  "output": 15.00},
    "claude-opus-4-6":            {"input": 15.00, "output": 75.00},
}

def tokens_to_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    prices = PRICING.get(model, {"input": 3.00, "output": 15.00})
    return (input_tokens * prices["input"] + output_tokens * prices["output"]) / 1_000_000


@dataclass
class SessionLedger:
    session_id: str
    user_id: str
    started_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cost_usd: float = 0.0
    call_count: int = 0

    def record(self, model: str, input_tokens: int, output_tokens: int) -> float:
        cost = tokens_to_usd(model, input_tokens, output_tokens)
        self.total_input_tokens += input_tokens
        self.total_output_tokens += output_tokens
        self.total_cost_usd += cost
        self.call_count += 1
        return cost

    def summary(self) -> dict:
        return {
            "session_id": self.session_id,
            "user_id": self.user_id,
            "started_at": self.started_at,
            "calls": self.call_count,
            "input_tokens": self.total_input_tokens,
            "output_tokens": self.total_output_tokens,
            "total_cost_usd": round(self.total_cost_usd, 6),
        }


class AttributedClient:
    def __init__(self, session_id: str, user_id: str):
        self.client = anthropic.Anthropic()
        self.ledger = SessionLedger(session_id=session_id, user_id=user_id)

    def create(self, model: str, messages: list, **kwargs) -> anthropic.types.Message:
        response = self.client.messages.create(
            model=model,
            messages=messages,
            **kwargs,
        )
        self.ledger.record(
            model=model,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )
        return response

    def cost_summary(self) -> dict:
        return self.ledger.summary()


# Usage
client = AttributedClient(session_id="sess-abc123", user_id="user-42")

response = client.create(
    model="claude-sonnet-4-6",
    max_tokens=1024,
    messages=[{"role": "user", "content": "Summarize the Rust ownership model."}]
)
print(response.content[0].text)
print(client.cost_summary())
# {'session_id': 'sess-abc123', 'user_id': 'user-42', 'calls': 1, 'total_cost_usd': 0.000312, ...}

# Expected Token Savings: No token savings; enables cost visibility to prevent runaway sessions
# Environment: Multi-user applications, SaaS products, any system with more than one user or feature
```

### Option 2: Per-Tenant Budget Enforcer — Hard-stop when session exceeds limit

```python
import anthropic
from dataclasses import dataclass, field
from datetime import datetime

PRICING = {
    "claude-haiku-4-5-20251001":  {"input": 0.80,  "output": 4.00},
    "claude-sonnet-4-6":          {"input": 3.00,  "output": 15.00},
    "claude-opus-4-6":            {"input": 15.00, "output": 75.00},
}

class BudgetExceededError(Exception):
    def __init__(self, spent: float, limit: float, session_id: str):
        self.spent = spent
        self.limit = limit
        self.session_id = session_id
        super().__init__(
            f"Session {session_id} exceeded budget: ${spent:.4f} > ${limit:.4f}"
        )


@dataclass
class TenantBudget:
    tenant_id: str
    limit_usd: float
    warn_at_pct: float = 0.8
    spent_usd: float = 0.0
    warned: bool = False

    def check(self, session_id: str) -> None:
        """Raise if over budget; warn if approaching limit."""
        if self.spent_usd >= self.limit_usd:
            raise BudgetExceededError(self.spent_usd, self.limit_usd, session_id)
        pct = self.spent_usd / self.limit_usd
        if pct >= self.warn_at_pct and not self.warned:
            self.warned = True
            print(f"[WARN] Tenant {self.tenant_id} at {pct*100:.0f}% of budget "
                  f"(${self.spent_usd:.4f}/${self.limit_usd:.2f})")

    def record(self, model: str, input_tokens: int, output_tokens: int) -> float:
        prices = PRICING.get(model, {"input": 3.00, "output": 15.00})
        cost = (input_tokens * prices["input"] + output_tokens * prices["output"]) / 1_000_000
        self.spent_usd += cost
        return cost


class BudgetedClient:
    def __init__(self, tenant_id: str, session_id: str, budget_usd: float):
        self.client = anthropic.Anthropic()
        self.session_id = session_id
        self.budget = TenantBudget(tenant_id=tenant_id, limit_usd=budget_usd)
        self.history: list[dict] = []

    def chat(self, user_message: str, model: str = "claude-sonnet-4-6") -> str:
        # Pre-flight budget check
        self.budget.check(self.session_id)

        self.history.append({"role": "user", "content": user_message})

        response = self.client.messages.create(
            model=model,
            max_tokens=1024,
            messages=self.history,
        )

        text = response.content[0].text
        self.history.append({"role": "assistant", "content": text})

        cost = self.budget.record(model, response.usage.input_tokens, response.usage.output_tokens)
        print(f"[COST] Turn cost: ${cost:.6f} | Session total: ${self.budget.spent_usd:.6f}")

        return text


# Usage
client = BudgetedClient(tenant_id="acme-corp", session_id="sess-001", budget_usd=0.10)

try:
    while True:
        user_input = input("You: ")
        reply = client.chat(user_input)
        print(f"Agent: {reply}")
except BudgetExceededError as e:
    print(f"Session ended: {e}")
except KeyboardInterrupt:
    pass

# Expected Token Savings: Saves up to 100% cost of runaway sessions by hard-stopping at limit
# Environment: Customer-facing chatbots, free-tier products, multi-tenant SaaS with usage limits
```

### Option 3: SQLite Cost Attribution — Persist costs across restarts with per-user rollups

```python
import anthropic
import sqlite3
from datetime import datetime, date
from pathlib import Path

PRICING = {
    "claude-haiku-4-5-20251001":  {"input": 0.80,  "output": 4.00},
    "claude-sonnet-4-6":          {"input": 3.00,  "output": 15.00},
    "claude-opus-4-6":            {"input": 15.00, "output": 75.00},
}

class CostAttributionDB:
    def __init__(self, db_path: str = "/tmp/cost_attribution.db"):
        self.db_path = db_path
        self._init()

    def _init(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS api_calls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    feature TEXT NOT NULL,
                    model TEXT NOT NULL,
                    input_tokens INTEGER NOT NULL,
                    output_tokens INTEGER NOT NULL,
                    cost_usd REAL NOT NULL,
                    timestamp TEXT NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_user ON api_calls(user_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_session ON api_calls(session_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_date ON api_calls(timestamp)")

    def record(self, session_id: str, user_id: str, feature: str,
               model: str, input_tokens: int, output_tokens: int) -> float:
        prices = PRICING.get(model, {"input": 3.00, "output": 15.00})
        cost = (input_tokens * prices["input"] + output_tokens * prices["output"]) / 1_000_000
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO api_calls VALUES (NULL,?,?,?,?,?,?,?,?)",
                (session_id, user_id, feature, model,
                 input_tokens, output_tokens, cost, datetime.utcnow().isoformat())
            )
        return cost

    def user_daily_cost(self, user_id: str, day: str = None) -> float:
        day = day or date.today().isoformat()
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT SUM(cost_usd) FROM api_calls WHERE user_id=? AND timestamp LIKE ?",
                (user_id, f"{day}%")
            ).fetchone()
        return row[0] or 0.0

    def top_spenders(self, limit: int = 10) -> list[dict]:
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute("""
                SELECT user_id, feature,
                       SUM(cost_usd) as total_cost,
                       SUM(input_tokens + output_tokens) as total_tokens,
                       COUNT(*) as calls
                FROM api_calls
                GROUP BY user_id, feature
                ORDER BY total_cost DESC
                LIMIT ?
            """, (limit,)).fetchall()
        return [
            {"user_id": r[0], "feature": r[1],
             "total_cost_usd": round(r[2], 6), "total_tokens": r[3], "calls": r[4]}
            for r in rows
        ]

    def session_breakdown(self, session_id: str) -> dict:
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT model, SUM(input_tokens), SUM(output_tokens), SUM(cost_usd), COUNT(*) "
                "FROM api_calls WHERE session_id=? GROUP BY model",
                (session_id,)
            ).fetchall()
        return {
            "session_id": session_id,
            "models": [
                {"model": r[0], "input_tokens": r[1], "output_tokens": r[2],
                 "cost_usd": round(r[3], 6), "calls": r[4]}
                for r in rows
            ],
            "total_cost_usd": round(sum(r[3] for r in rows), 6),
        }


class AttributedAgentClient:
    def __init__(self, session_id: str, user_id: str, feature: str):
        self.client = anthropic.Anthropic()
        self.db = CostAttributionDB()
        self.session_id = session_id
        self.user_id = user_id
        self.feature = feature

    def create(self, model: str, messages: list, **kwargs) -> anthropic.types.Message:
        response = self.client.messages.create(model=model, messages=messages, **kwargs)
        self.db.record(
            session_id=self.session_id,
            user_id=self.user_id,
            feature=self.feature,
            model=model,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )
        return response


# Usage
client = AttributedAgentClient(
    session_id="sess-abc123",
    user_id="user-42",
    feature="code-review"
)
response = client.create(
    model="claude-sonnet-4-6",
    max_tokens=512,
    messages=[{"role": "user", "content": "Review this Python function for bugs."}]
)

db = CostAttributionDB()
print(db.session_breakdown("sess-abc123"))
print(db.top_spenders())
print(f"user-42 today: ${db.user_daily_cost('user-42'):.6f}")

# Expected Token Savings: No token overhead; prevents bill surprises via daily/session rollup queries
# Environment: Production multi-user systems, internal tooling, any system needing cost accountability
```

### Option 4: Streaming Cost Tracker — Accumulate costs from streamed responses

```python
import anthropic
from dataclasses import dataclass, field
from datetime import datetime
from contextlib import contextmanager

PRICING = {
    "claude-haiku-4-5-20251001":  {"input": 0.80,  "output": 4.00},
    "claude-sonnet-4-6":          {"input": 3.00,  "output": 15.00},
    "claude-opus-4-6":            {"input": 15.00, "output": 75.00},
}

@dataclass
class StreamCostAccumulator:
    session_id: str
    user_id: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    started_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())

    @property
    def cost_usd(self) -> float:
        prices = PRICING.get(self.model, {"input": 3.00, "output": 15.00})
        return (self.input_tokens * prices["input"] + self.output_tokens * prices["output"]) / 1_000_000

    def finalize(self, usage: anthropic.types.Usage) -> None:
        self.input_tokens = usage.input_tokens
        self.output_tokens = usage.output_tokens

    def report(self) -> str:
        return (
            f"Session {self.session_id} | User {self.user_id} | "
            f"Model {self.model} | "
            f"In: {self.input_tokens:,} | Out: {self.output_tokens:,} | "
            f"Cost: ${self.cost_usd:.6f}"
        )


class StreamingAttributedClient:
    def __init__(self, session_id: str, user_id: str):
        self.client = anthropic.Anthropic()
        self.session_id = session_id
        self.user_id = user_id
        self._accumulators: list[StreamCostAccumulator] = []

    @contextmanager
    def stream(self, model: str, messages: list, **kwargs):
        accumulator = StreamCostAccumulator(
            session_id=self.session_id,
            user_id=self.user_id,
            model=model,
        )
        self._accumulators.append(accumulator)

        with self.client.messages.stream(
            model=model,
            messages=messages,
            **kwargs,
        ) as stream:
            yield stream
            # Finalize with actual usage after stream completes
            final_message = stream.get_final_message()
            accumulator.finalize(final_message.usage)
            print(f"[COST] {accumulator.report()}")

    def session_total_cost(self) -> float:
        return sum(a.cost_usd for a in self._accumulators)

    def session_summary(self) -> dict:
        return {
            "session_id": self.session_id,
            "user_id": self.user_id,
            "api_calls": len(self._accumulators),
            "total_input_tokens": sum(a.input_tokens for a in self._accumulators),
            "total_output_tokens": sum(a.output_tokens for a in self._accumulators),
            "total_cost_usd": round(self.session_total_cost(), 6),
        }


# Usage
client = StreamingAttributedClient(session_id="sess-stream-001", user_id="user-99")

with client.stream(
    model="claude-sonnet-4-6",
    max_tokens=1024,
    messages=[{"role": "user", "content": "Explain async/await in Python."}]
) as stream:
    for text in stream.text_stream:
        print(text, end="", flush=True)

print()
print(client.session_summary())

# Expected Token Savings: No token overhead; streaming-compatible attribution with zero latency impact
# Environment: Real-time streaming chatbots, interactive agents, CLI tools with streaming output
```

### Option 5: Feature-Flag Cost Rollup — Tag costs by feature, A/B variant, or experiment

```python
import anthropic
import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

PRICING = {
    "claude-haiku-4-5-20251001":  {"input": 0.80,  "output": 4.00},
    "claude-sonnet-4-6":          {"input": 3.00,  "output": 15.00},
    "claude-opus-4-6":            {"input": 15.00, "output": 75.00},
}

@dataclass
class CostEntry:
    feature: str
    variant: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    timestamp: str


class FeatureCostTracker:
    def __init__(self):
        self.entries: list[CostEntry] = []

    def record(self, feature: str, variant: str, model: str,
               input_tokens: int, output_tokens: int) -> float:
        prices = PRICING.get(model, {"input": 3.00, "output": 15.00})
        cost = (input_tokens * prices["input"] + output_tokens * prices["output"]) / 1_000_000
        self.entries.append(CostEntry(
            feature=feature, variant=variant, model=model,
            input_tokens=input_tokens, output_tokens=output_tokens,
            cost_usd=cost, timestamp=datetime.utcnow().isoformat()
        ))
        return cost

    def rollup_by_feature(self) -> dict:
        rollup: dict = defaultdict(lambda: {"cost_usd": 0.0, "calls": 0, "tokens": 0})
        for e in self.entries:
            key = f"{e.feature}/{e.variant}"
            rollup[key]["cost_usd"] += e.cost_usd
            rollup[key]["calls"] += 1
            rollup[key]["tokens"] += e.input_tokens + e.output_tokens
        return {k: {"cost_usd": round(v["cost_usd"], 6), "calls": v["calls"], "tokens": v["tokens"]}
                for k, v in rollup.items()}

    def compare_variants(self, feature: str) -> dict:
        """Compare A/B variant costs for a given feature."""
        variants: dict = defaultdict(lambda: {"cost_usd": 0.0, "calls": 0})
        for e in self.entries:
            if e.feature == feature:
                variants[e.variant]["cost_usd"] += e.cost_usd
                variants[e.variant]["calls"] += 1
        result = {}
        for v, data in variants.items():
            result[v] = {
                "total_cost_usd": round(data["cost_usd"], 6),
                "calls": data["calls"],
                "avg_cost_per_call": round(data["cost_usd"] / data["calls"], 6) if data["calls"] else 0,
            }
        return result


class FeatureAttributedClient:
    def __init__(self):
        self.client = anthropic.Anthropic()
        self.tracker = FeatureCostTracker()

    def create(self, model: str, messages: list,
               feature: str = "default", variant: str = "control",
               **kwargs) -> anthropic.types.Message:
        response = self.client.messages.create(model=model, messages=messages, **kwargs)
        cost = self.tracker.record(
            feature=feature, variant=variant, model=model,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )
        return response


# Usage — A/B test: does GPT-style prompting save money?
client = FeatureAttributedClient()

# Variant A: verbose system prompt
client.create(
    model="claude-sonnet-4-6", max_tokens=256,
    feature="summarization", variant="verbose-prompt",
    system="You are a highly skilled summarization assistant. Please provide a concise, accurate, and well-structured summary of the following text, capturing all key points while eliminating redundancy.",
    messages=[{"role": "user", "content": "Summarize: The quick brown fox jumped over the lazy dog repeatedly."}]
)

# Variant B: terse system prompt
client.create(
    model="claude-sonnet-4-6", max_tokens=256,
    feature="summarization", variant="terse-prompt",
    system="Summarize concisely.",
    messages=[{"role": "user", "content": "Summarize: The quick brown fox jumped over the lazy dog repeatedly."}]
)

print(json.dumps(client.tracker.compare_variants("summarization"), indent=2))
print(json.dumps(client.tracker.rollup_by_feature(), indent=2))

# Expected Token Savings: Identifies high-cost variants; verbose prompts often 20-40% more expensive
# Environment: A/B testing platforms, prompt optimization, product analytics, growth engineering
```

### Option 6: Async Cost Sink — Non-blocking attribution with background flush to storage

```python
import anthropic
import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

PRICING = {
    "claude-haiku-4-5-20251001":  {"input": 0.80,  "output": 4.00},
    "claude-sonnet-4-6":          {"input": 3.00,  "output": 15.00},
    "claude-opus-4-6":            {"input": 15.00, "output": 75.00},
}

@dataclass
class CostEvent:
    session_id: str
    user_id: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    timestamp: str


class AsyncCostSink:
    """Non-blocking cost attribution: API calls don't wait for cost recording."""

    FLUSH_INTERVAL = 10.0   # seconds
    FLUSH_BATCH_SIZE = 50

    def __init__(self, sink_path: str = "/tmp/cost_sink.jsonl"):
        self.sink_path = Path(sink_path)
        self._queue: asyncio.Queue = asyncio.Queue()
        self._flusher_task: asyncio.Task | None = None

    async def start(self) -> None:
        self._flusher_task = asyncio.create_task(self._flush_loop())

    async def stop(self) -> None:
        if self._flusher_task:
            self._flusher_task.cancel()
            await self._drain()  # Final flush

    async def record(self, session_id: str, user_id: str, model: str,
                     input_tokens: int, output_tokens: int) -> float:
        prices = PRICING.get(model, {"input": 3.00, "output": 15.00})
        cost = (input_tokens * prices["input"] + output_tokens * prices["output"]) / 1_000_000
        event = CostEvent(
            session_id=session_id, user_id=user_id, model=model,
            input_tokens=input_tokens, output_tokens=output_tokens,
            cost_usd=cost, timestamp=datetime.utcnow().isoformat(),
        )
        await self._queue.put(event)
        return cost

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(self.FLUSH_INTERVAL)
            await self._drain()

    async def _drain(self) -> None:
        batch: list[CostEvent] = []
        while not self._queue.empty() and len(batch) < self.FLUSH_BATCH_SIZE:
            batch.append(self._queue.get_nowait())

        if batch:
            with open(self.sink_path, "a") as f:
                for event in batch:
                    f.write(json.dumps({
                        "session_id": event.session_id,
                        "user_id": event.user_id,
                        "model": event.model,
                        "input_tokens": event.input_tokens,
                        "output_tokens": event.output_tokens,
                        "cost_usd": event.cost_usd,
                        "timestamp": event.timestamp,
                    }) + "\n")


class AsyncAttributedClient:
    def __init__(self, session_id: str, user_id: str, cost_sink: AsyncCostSink):
        self.client = anthropic.AsyncAnthropic()
        self.session_id = session_id
        self.user_id = user_id
        self.sink = cost_sink
        self._session_cost: float = 0.0

    async def create(self, model: str, messages: list, **kwargs) -> anthropic.types.Message:
        response = await self.client.messages.create(model=model, messages=messages, **kwargs)
        cost = await self.sink.record(
            session_id=self.session_id,
            user_id=self.user_id,
            model=model,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )
        self._session_cost += cost
        return response

    @property
    def session_cost(self) -> float:
        return self._session_cost


async def main():
    sink = AsyncCostSink()
    await sink.start()

    # Simulate multiple concurrent sessions
    clients = [
        AsyncAttributedClient(f"sess-{i}", f"user-{i}", sink)
        for i in range(3)
    ]

    async def run_session(client: AsyncAttributedClient, question: str) -> None:
        response = await client.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=128,
            messages=[{"role": "user", "content": question}]
        )
        print(f"[{client.session_id}] Cost: ${client.session_cost:.6f}")

    await asyncio.gather(
        run_session(clients[0], "What is Python?"),
        run_session(clients[1], "What is JavaScript?"),
        run_session(clients[2], "What is Rust?"),
    )

    await asyncio.sleep(1)  # Let queue drain
    await sink.stop()
    print(f"Cost events written to {sink.sink_path}")


asyncio.run(main())

# Expected Token Savings: Zero latency overhead on API calls; async flush adds <1ms per request
# Environment: High-throughput async agents, production APIs with strict latency budgets
```

## Comparison

| Option | Storage | Overhead | Budget Enforcement | Best For |
|--------|---------|----------|-------------------|----------|
| In-Memory Ledger | RAM only | Minimal | No | Single-process apps, development |
| Budget Enforcer | RAM only | Minimal | Hard stop | Free tiers, per-user limits |
| SQLite DB | Disk (persistent) | Low | Via queries | Multi-user production systems |
| Streaming Tracker | RAM only | Minimal | No | Streaming-first applications |
| Feature Flag Rollup | RAM only | Minimal | No | A/B testing, prompt optimization |
| Async Cost Sink | Disk (async JSONL) | ~1ms | No | High-throughput, latency-sensitive APIs |
