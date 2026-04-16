---
title: "Agent Doesn't Implement Cost-Per-Conversation Tracking"
slug: agent-doesnt-implement-cost-per-conversation-tracking
category: observability
tags: [cost, tracking, billing, token-usage, analytics, anthropic-sdk]
description: >
  The agent processes conversations without tracking token consumption or API
  costs at the conversation level, making it impossible to attribute spend to
  individual users, sessions, or features, or to enforce per-conversation budgets.
symptoms:
  - Monthly API bills arrive with no breakdown by user or feature
  - High-cost conversations are invisible until the invoice arrives
  - No ability to cap spend per user, session, or workflow
  - Finance and product teams cannot correlate AI spend with business value
related_solutions:
  - agent-doesnt-implement-model-tiering-by-task-complexity
  - agent-doesnt-implement-prompt-token-budget-enforcement-per-request
  - agent-doesnt-implement-request-batching-for-bulk-inference
---

## Problem

Most agents count tokens at the request level but never aggregate those counts
into a coherent per-conversation ledger. Without conversation-scoped cost
tracking you cannot answer basic questions: *Which user spent the most this
week? Which feature drives 80 % of our bill? Did that prompt refactor actually
save money?* The absence of this data also makes it impossible to enforce
hard spending caps before costs run away.

---

## Solution 1 — Inline Usage Accumulator (simplest)

Attach a running `UsageLedger` to every conversation object and update it after
each API call using the `usage` field returned in every `Message` response.

```python
import anthropic
from dataclasses import dataclass, field

COST_PER_MILLION = {
    "claude-haiku-4-5-20251001":  {"input": 0.80,  "output": 4.00},
    "claude-sonnet-4-6":          {"input": 3.00,  "output": 15.00},
    "claude-opus-4-6":            {"input": 15.00, "output": 75.00},
}

@dataclass
class UsageLedger:
    conversation_id: str
    input_tokens:  int   = 0
    output_tokens: int   = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    turns: int = 0

    def record(self, usage: anthropic.types.Usage, model: str) -> float:
        self.input_tokens  += usage.input_tokens
        self.output_tokens += usage.output_tokens
        self.cache_read_tokens  += getattr(usage, "cache_read_input_tokens",  0)
        self.cache_write_tokens += getattr(usage, "cache_creation_input_tokens", 0)
        self.turns += 1
        rates = COST_PER_MILLION.get(model, {"input": 3.00, "output": 15.00})
        return (usage.input_tokens * rates["input"] +
                usage.output_tokens * rates["output"]) / 1_000_000

    @property
    def total_cost_usd(self) -> float:
        costs = {}
        for model, rates in COST_PER_MILLION.items():
            # Simplified: allocate all tokens to conversation totals
            pass
        # Use accumulated per-call costs instead
        return self._accumulated_cost

    def summary(self) -> dict:
        return {
            "conversation_id": self.conversation_id,
            "turns": self.turns,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
        }


@dataclass
class CostTrackedConversation:
    conversation_id: str
    model: str = "claude-sonnet-4-6"
    messages: list = field(default_factory=list)
    ledger: UsageLedger = field(init=False)
    _accumulated_cost: float = field(default=0.0, init=False)

    def __post_init__(self):
        self.ledger = UsageLedger(self.conversation_id)

    def chat(self, user_message: str) -> str:
        client = anthropic.Anthropic()
        self.messages.append({"role": "user", "content": user_message})
        response = client.messages.create(
            model=self.model,
            max_tokens=1024,
            messages=self.messages,
        )
        turn_cost = self.ledger.record(response.usage, self.model)
        self._accumulated_cost += turn_cost
        assistant_text = response.content[0].text
        self.messages.append({"role": "assistant", "content": assistant_text})
        print(f"[cost] turn={turn_cost:.6f} USD  total={self._accumulated_cost:.4f} USD")
        return assistant_text

    def cost_summary(self) -> dict:
        return {**self.ledger.summary(), "total_cost_usd": round(self._accumulated_cost, 6)}


# Usage
conv = CostTrackedConversation(conversation_id="conv-001")
conv.chat("Explain transformer attention in one paragraph.")
conv.chat("Now give a Python example of multi-head attention.")
print(conv.cost_summary())
```

---

## Solution 2 — Context-Manager Scoped Cost Tracker

Wrap any block of API calls in a `ConversationCostScope` context manager that
automatically collects usage and emits a structured cost record on exit.

```python
import anthropic
import json
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, asdict
from typing import AsyncIterator


PRICING = {
    "claude-haiku-4-5-20251001":  (0.80,  4.00),
    "claude-sonnet-4-6":          (3.00,  15.00),
    "claude-opus-4-6":            (15.00, 75.00),
}


@dataclass
class CostRecord:
    conversation_id: str
    user_id: str
    feature: str
    started_at: float
    ended_at: float = 0.0
    turns: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cost_usd: float = 0.0
    model_breakdown: dict = field(default_factory=dict)

    def add_turn(self, model: str, usage: anthropic.types.Usage) -> None:
        inp_rate, out_rate = PRICING.get(model, (3.00, 15.00))
        inp = usage.input_tokens
        out = usage.output_tokens
        cost = (inp * inp_rate + out * out_rate) / 1_000_000
        self.turns += 1
        self.total_input_tokens += inp
        self.total_output_tokens += out
        self.total_cost_usd += cost
        mb = self.model_breakdown.setdefault(model, {"input": 0, "output": 0, "cost": 0.0})
        mb["input"] += inp
        mb["output"] += out
        mb["cost"] += cost

    def finalize(self) -> None:
        self.ended_at = time.time()
        self.total_cost_usd = round(self.total_cost_usd, 8)


class ConversationCostScope:
    def __init__(self, conversation_id: str, user_id: str, feature: str):
        self.record = CostRecord(
            conversation_id=conversation_id,
            user_id=user_id,
            feature=feature,
            started_at=time.time(),
        )
        self.client = anthropic.Anthropic()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.record.finalize()
        self._emit(self.record)

    def _emit(self, record: CostRecord) -> None:
        # In production: write to Postgres, BigQuery, ClickHouse, etc.
        print(json.dumps(asdict(record), indent=2))

    def create_message(self, model: str, messages: list, **kwargs):
        resp = self.client.messages.create(model=model, messages=messages, **kwargs)
        self.record.add_turn(model, resp.usage)
        return resp


# Usage
with ConversationCostScope("conv-042", user_id="u-99", feature="code-review") as scope:
    r1 = scope.create_message(
        model="claude-sonnet-4-6",
        messages=[{"role": "user", "content": "Review this PR diff..."}],
        max_tokens=512,
    )
    r2 = scope.create_message(
        model="claude-sonnet-4-6",
        messages=[
            {"role": "user", "content": "Review this PR diff..."},
            {"role": "assistant", "content": r1.content[0].text},
            {"role": "user", "content": "Summarise the issues as bullet points."},
        ],
        max_tokens=256,
    )
# CostRecord JSON emitted automatically on exit
```

---

## Solution 3 — Redis-Backed Real-Time Cost Ledger

Store per-conversation cost in Redis so every service instance can query live
spend, enforce caps, and expose a real-time dashboard without a separate DB
write per call.

```python
import anthropic
import json
import time
import redis.asyncio as aioredis


PRICING = {
    "claude-haiku-4-5-20251001":  (0.80,  4.00),
    "claude-sonnet-4-6":          (3.00,  15.00),
    "claude-opus-4-6":            (15.00, 75.00),
}
CONV_TTL = 86_400  # 24 h


class RedisCostLedger:
    def __init__(self, redis_url: str = "redis://localhost:6379"):
        self.redis = aioredis.from_url(redis_url, decode_responses=True)
        self.client = anthropic.AsyncAnthropic()

    def _key(self, conversation_id: str) -> str:
        return f"cost:conv:{conversation_id}"

    async def record_turn(
        self,
        conversation_id: str,
        model: str,
        usage: anthropic.types.Usage,
    ) -> dict:
        inp_rate, out_rate = PRICING.get(model, (3.00, 15.00))
        turn_cost = (usage.input_tokens * inp_rate +
                     usage.output_tokens * out_rate) / 1_000_000
        key = self._key(conversation_id)
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.hincrbyfloat(key, "total_cost_usd", turn_cost)
            pipe.hincrby(key, "input_tokens", usage.input_tokens)
            pipe.hincrby(key, "output_tokens", usage.output_tokens)
            pipe.hincrby(key, "turns", 1)
            pipe.hsetnx(key, "started_at", str(time.time()))
            pipe.expire(key, CONV_TTL)
            await pipe.execute()
        return {"turn_cost_usd": round(turn_cost, 8)}

    async def get_cost(self, conversation_id: str) -> dict:
        data = await self.redis.hgetall(self._key(conversation_id))
        if not data:
            return {"conversation_id": conversation_id, "total_cost_usd": 0.0}
        return {
            "conversation_id": conversation_id,
            "total_cost_usd": float(data.get("total_cost_usd", 0)),
            "input_tokens": int(data.get("input_tokens", 0)),
            "output_tokens": int(data.get("output_tokens", 0)),
            "turns": int(data.get("turns", 0)),
        }

    async def check_budget(self, conversation_id: str, cap_usd: float) -> bool:
        """Returns False if conversation has exceeded the cap."""
        data = await self.redis.hgetall(self._key(conversation_id))
        spent = float(data.get("total_cost_usd", 0))
        return spent < cap_usd

    async def chat_with_cap(
        self,
        conversation_id: str,
        messages: list,
        model: str = "claude-sonnet-4-6",
        cap_usd: float = 0.10,
    ) -> str:
        if not await self.check_budget(conversation_id, cap_usd):
            raise ValueError(f"Conversation {conversation_id} exceeded ${cap_usd:.2f} cap")
        response = await self.client.messages.create(
            model=model,
            max_tokens=1024,
            messages=messages,
        )
        await self.record_turn(conversation_id, model, response.usage)
        return response.content[0].text


import asyncio

async def main():
    ledger = RedisCostLedger()
    conv_id = "conv-redis-001"
    messages = [{"role": "user", "content": "What is retrieval-augmented generation?"}]
    reply = await ledger.chat_with_cap(conv_id, messages, cap_usd=0.05)
    print(reply[:120])
    cost = await ledger.get_cost(conv_id)
    print(cost)

asyncio.run(main())
```

---

## Solution 4 — SQLite Persistent Cost Journal with Daily Rollup

Write per-turn cost rows to a local SQLite database and aggregate them into
daily/user/feature rollups for billing exports and trend analysis.

```python
import anthropic
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass


PRICING = {
    "claude-haiku-4-5-20251001":  (0.80,  4.00),
    "claude-sonnet-4-6":          (3.00,  15.00),
    "claude-opus-4-6":            (15.00, 75.00),
}

DDL = """
CREATE TABLE IF NOT EXISTS cost_turns (
    id              TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    user_id         TEXT NOT NULL,
    feature         TEXT NOT NULL,
    model           TEXT NOT NULL,
    input_tokens    INTEGER NOT NULL,
    output_tokens   INTEGER NOT NULL,
    cost_usd        REAL NOT NULL,
    ts              REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_conv  ON cost_turns(conversation_id);
CREATE INDEX IF NOT EXISTS idx_user  ON cost_turns(user_id);
CREATE INDEX IF NOT EXISTS idx_ts    ON cost_turns(ts);
"""


class SQLiteCostJournal:
    def __init__(self, db_path: str = "agent_costs.db"):
        self.db_path = db_path
        self.client = anthropic.Anthropic()
        with sqlite3.connect(db_path) as conn:
            conn.executescript(DDL)

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def record(
        self,
        conversation_id: str,
        user_id: str,
        feature: str,
        model: str,
        usage: anthropic.types.Usage,
    ) -> float:
        inp_rate, out_rate = PRICING.get(model, (3.00, 15.00))
        cost = (usage.input_tokens * inp_rate + usage.output_tokens * out_rate) / 1_000_000
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO cost_turns VALUES (?,?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), conversation_id, user_id, feature,
                 model, usage.input_tokens, usage.output_tokens, cost, time.time()),
            )
        return cost

    def conversation_total(self, conversation_id: str) -> dict:
        with self._conn() as conn:
            row = conn.execute(
                """SELECT COUNT(*), SUM(input_tokens), SUM(output_tokens), SUM(cost_usd)
                   FROM cost_turns WHERE conversation_id=?""",
                (conversation_id,),
            ).fetchone()
        return {"turns": row[0], "input_tokens": row[1],
                "output_tokens": row[2], "total_cost_usd": round(row[3] or 0, 8)}

    def daily_rollup(self, days: int = 7) -> list[dict]:
        cutoff = time.time() - days * 86_400
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT date(ts,'unixepoch') as day, user_id, feature,
                          SUM(cost_usd) as cost, SUM(input_tokens+output_tokens) as tokens
                   FROM cost_turns WHERE ts>=?
                   GROUP BY day, user_id, feature
                   ORDER BY day DESC, cost DESC""",
                (cutoff,),
            ).fetchall()
        return [{"day": r[0], "user_id": r[1], "feature": r[2],
                 "cost_usd": round(r[3], 6), "tokens": r[4]} for r in rows]

    def chat(
        self,
        conversation_id: str,
        user_id: str,
        feature: str,
        messages: list,
        model: str = "claude-sonnet-4-6",
    ) -> str:
        resp = self.client.messages.create(model=model, max_tokens=1024, messages=messages)
        cost = self.record(conversation_id, user_id, feature, model, resp.usage)
        print(f"[journal] turn cost=${cost:.6f}")
        return resp.content[0].text


journal = SQLiteCostJournal()
conv_id = f"conv-{uuid.uuid4().hex[:8]}"
journal.chat(conv_id, "u-42", "chat", [{"role": "user", "content": "Hello!"}])
print(journal.conversation_total(conv_id))
print(journal.daily_rollup(days=1))
```

---

## Solution 5 — Streaming Cost Tracker with Server-Sent Events

Track cost in real time during streaming responses and push incremental cost
events to the client over SSE, so the UI can show a live "current cost" meter.

```python
import anthropic
import asyncio
import json
import time
from dataclasses import dataclass, field


PRICING = {
    "claude-haiku-4-5-20251001":  (0.80,  4.00),
    "claude-sonnet-4-6":          (3.00,  15.00),
    "claude-opus-4-6":            (15.00, 75.00),
}


@dataclass
class StreamCostState:
    conversation_id: str
    model: str
    accumulated_cost_usd: float = 0.0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    turns: int = 0
    events: list = field(default_factory=list)

    def record_turn(self, usage: anthropic.types.Usage) -> dict:
        inp_rate, out_rate = PRICING.get(self.model, (3.00, 15.00))
        turn_cost = (usage.input_tokens * inp_rate +
                     usage.output_tokens * out_rate) / 1_000_000
        self.accumulated_cost_usd += turn_cost
        self.total_input_tokens += usage.input_tokens
        self.total_output_tokens += usage.output_tokens
        self.turns += 1
        event = {
            "type": "cost_update",
            "conversation_id": self.conversation_id,
            "turn": self.turns,
            "turn_cost_usd": round(turn_cost, 8),
            "accumulated_cost_usd": round(self.accumulated_cost_usd, 8),
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "ts": time.time(),
        }
        self.events.append(event)
        return event


async def streaming_chat_with_cost(
    conversation_id: str,
    messages: list,
    model: str = "claude-sonnet-4-6",
    state: StreamCostState | None = None,
):
    """
    Async generator yielding (chunk_text, cost_event_or_None).
    cost_event is emitted once at end of each turn with final usage counts.
    """
    if state is None:
        state = StreamCostState(conversation_id=conversation_id, model=model)

    client = anthropic.AsyncAnthropic()
    full_text = []

    async with client.messages.stream(
        model=model,
        max_tokens=1024,
        messages=messages,
    ) as stream:
        async for text in stream.text_stream:
            full_text.append(text)
            yield text, None  # text chunk, no cost event yet

    # Final message has usage counts
    final_message = await stream.get_final_message()
    cost_event = state.record_turn(final_message.usage)
    yield "", cost_event  # empty text, cost event


async def demo_streaming_cost():
    conv_id = "conv-stream-001"
    state = StreamCostState(conversation_id=conv_id, model="claude-sonnet-4-6")
    messages = [{"role": "user", "content": "Explain cosine similarity in 3 sentences."}]

    print("Assistant: ", end="", flush=True)
    async for text, cost_event in streaming_chat_with_cost(conv_id, messages, state=state):
        if text:
            print(text, end="", flush=True)
        elif cost_event:
            print(f"\n\n[SSE] {json.dumps(cost_event, indent=2)}")

    # Second turn
    messages += [
        {"role": "assistant", "content": "".join(
            [t async for t, _ in streaming_chat_with_cost(conv_id, messages, state=state)])},
        {"role": "user", "content": "Now give a NumPy code example."},
    ]


asyncio.run(demo_streaming_cost())
```

---

## Solution 6 — Multi-Tenant Cost Aggregator with Budget Alerts

Aggregate costs across multiple tenants with per-tenant daily/monthly budgets,
percentage-threshold alerts, and automatic model downgrade when limits approach.

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass, field
from collections import defaultdict
from typing import Callable, Awaitable


PRICING = {
    "claude-haiku-4-5-20251001":  (0.80,  4.00),
    "claude-sonnet-4-6":          (3.00,  15.00),
    "claude-opus-4-6":            (15.00, 75.00),
}

FALLBACK_MODEL = {
    "claude-opus-4-6":   "claude-sonnet-4-6",
    "claude-sonnet-4-6": "claude-haiku-4-5-20251001",
    "claude-haiku-4-5-20251001": "claude-haiku-4-5-20251001",
}

AlertCallback = Callable[[str, float, float, str], Awaitable[None]]


@dataclass
class TenantBudget:
    tenant_id: str
    daily_cap_usd: float = 10.00
    monthly_cap_usd: float = 200.00
    alert_threshold: float = 0.80   # alert at 80% of cap
    daily_spent: float = 0.0
    monthly_spent: float = 0.0
    day_reset: float = field(default_factory=time.time)
    month_reset: float = field(default_factory=time.time)
    alerted_daily: bool = False
    alerted_monthly: bool = False

    def _reset_if_needed(self):
        now = time.time()
        if now - self.day_reset >= 86_400:
            self.daily_spent = 0.0
            self.day_reset = now
            self.alerted_daily = False
        if now - self.month_reset >= 30 * 86_400:
            self.monthly_spent = 0.0
            self.month_reset = now
            self.alerted_monthly = False

    def add(self, cost: float) -> tuple[bool, bool]:
        """Returns (daily_ok, monthly_ok)."""
        self._reset_if_needed()
        self.daily_spent += cost
        self.monthly_spent += cost
        return (self.daily_spent < self.daily_cap_usd,
                self.monthly_spent < self.monthly_cap_usd)

    def utilization(self) -> dict:
        self._reset_if_needed()
        return {
            "daily_pct":   self.daily_spent   / self.daily_cap_usd,
            "monthly_pct": self.monthly_spent / self.monthly_cap_usd,
        }


class MultiTenantCostAggregator:
    def __init__(self, alert_callback: AlertCallback | None = None):
        self.client = anthropic.AsyncAnthropic()
        self.budgets: dict[str, TenantBudget] = {}
        self.conv_costs: dict[str, float] = defaultdict(float)
        self.alert_callback = alert_callback or self._default_alert

    def register_tenant(self, tenant_id: str, **budget_kwargs) -> None:
        self.budgets[tenant_id] = TenantBudget(tenant_id=tenant_id, **budget_kwargs)

    async def _default_alert(self, tenant_id: str, spent: float, cap: float, period: str):
        print(f"[ALERT] tenant={tenant_id} {period} spent=${spent:.4f}/{cap:.2f} ({spent/cap:.0%})")

    def _select_model(self, tenant_id: str, requested_model: str) -> str:
        budget = self.budgets.get(tenant_id)
        if not budget:
            return requested_model
        util = budget.utilization()
        # Downgrade if approaching daily or monthly cap
        if util["daily_pct"] >= 0.90 or util["monthly_pct"] >= 0.90:
            return "claude-haiku-4-5-20251001"
        if util["daily_pct"] >= 0.75 or util["monthly_pct"] >= 0.75:
            return FALLBACK_MODEL.get(requested_model, requested_model)
        return requested_model

    async def chat(
        self,
        tenant_id: str,
        conversation_id: str,
        messages: list,
        model: str = "claude-sonnet-4-6",
    ) -> str:
        budget = self.budgets.get(tenant_id)
        if budget:
            util = budget.utilization()
            if util["daily_pct"] >= 1.0:
                raise RuntimeError(f"Tenant {tenant_id} exceeded daily budget")
            if util["monthly_pct"] >= 1.0:
                raise RuntimeError(f"Tenant {tenant_id} exceeded monthly budget")

        effective_model = self._select_model(tenant_id, model)
        if effective_model != model:
            print(f"[downgrade] {tenant_id}: {model} -> {effective_model}")

        resp = await self.client.messages.create(
            model=effective_model, max_tokens=1024, messages=messages
        )
        inp_rate, out_rate = PRICING.get(effective_model, (3.00, 15.00))
        cost = (resp.usage.input_tokens * inp_rate +
                resp.usage.output_tokens * out_rate) / 1_000_000
        self.conv_costs[conversation_id] += cost

        if budget:
            daily_ok, monthly_ok = budget.add(cost)
            util = budget.utilization()
            if not budget.alerted_daily and util["daily_pct"] >= budget.alert_threshold:
                budget.alerted_daily = True
                await self.alert_callback(tenant_id, budget.daily_spent,
                                          budget.daily_cap_usd, "daily")
            if not budget.alerted_monthly and util["monthly_pct"] >= budget.alert_threshold:
                budget.alerted_monthly = True
                await self.alert_callback(tenant_id, budget.monthly_spent,
                                          budget.monthly_cap_usd, "monthly")

        return resp.content[0].text

    def conversation_cost(self, conversation_id: str) -> float:
        return round(self.conv_costs[conversation_id], 8)

    def tenant_summary(self, tenant_id: str) -> dict:
        b = self.budgets.get(tenant_id)
        if not b:
            return {}
        return {
            "tenant_id": tenant_id,
            "daily_spent_usd": round(b.daily_spent, 6),
            "monthly_spent_usd": round(b.monthly_spent, 6),
            **b.utilization(),
        }


async def main():
    aggregator = MultiTenantCostAggregator()
    aggregator.register_tenant("acme", daily_cap_usd=5.0, monthly_cap_usd=50.0)
    aggregator.register_tenant("globex", daily_cap_usd=2.0, monthly_cap_usd=20.0)

    reply = await aggregator.chat(
        "acme", "conv-acme-1",
        [{"role": "user", "content": "Summarise the CAP theorem."}],
    )
    print(reply[:80])
    print(aggregator.tenant_summary("acme"))
    print("conv cost:", aggregator.conversation_cost("conv-acme-1"))


asyncio.run(main())
```

---

## Comparison

| Approach | Persistence | Real-time | Multi-tenant | Budget enforcement | Complexity |
|---|---|---|---|---|---|
| Inline accumulator | In-memory only | Yes (per turn) | No | Manual | Very low |
| Context-manager scope | Emitted on exit | On exit | No | No | Low |
| Redis ledger | Redis (TTL) | Yes (per turn) | Yes | Hard cap per conv | Medium |
| SQLite journal | Disk (permanent) | After each turn | Yes | Query-based | Medium |
| SSE streaming tracker | In-memory + push | Yes (live chunks) | No | No | Medium-high |
| Multi-tenant aggregator | In-memory | Yes (per turn) | Yes | Auto-downgrade + alerts | High |

**Rule of thumb:**
- Single-service prototype → inline accumulator or context-manager scope
- Production single-service → SQLite journal for auditable records
- High-traffic distributed → Redis ledger for atomic cross-instance aggregation
- SaaS with multiple customers → multi-tenant aggregator with alert callbacks
- Real-time UI meters → SSE streaming tracker layered on any backend store
