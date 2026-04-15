---
layout: solution
title: "Agent Doesn't Implement Streaming Cost Budget Enforcement"
category: streaming
description: "How to halt or constrain streaming responses mid-generation when token cost exceeds a configured budget threshold."
tags: [streaming, cost, budget, token-counting, rate-limiting, asyncio]
---

# Agent Doesn't Implement Streaming Cost Budget Enforcement

Streaming responses bill per output token as they arrive. Without budget enforcement, a single runaway response can exhaust daily cost limits. You need a layer that tracks cumulative cost mid-stream and halts generation cleanly when a threshold is crossed.

## Option 1: Token-Counting Stream Interceptor with Hard Stop

Count tokens as chunks arrive; close the stream and append a truncation notice when the budget is hit.

```python
import anthropic
import time

# Approximate token cost (USD per token)
OUTPUT_TOKEN_COST = {
    "claude-haiku-4-5-20251001": 0.00000125,
    "claude-sonnet-4-6": 0.000015,
    "claude-opus-4-6": 0.000075,
}

def stream_with_hard_budget(
    prompt: str,
    model: str = "claude-sonnet-4-6",
    budget_usd: float = 0.01,
    system: str = "You are a helpful assistant.",
) -> str:
    client = anthropic.Anthropic()
    cost_per_token = OUTPUT_TOKEN_COST.get(model, 0.000015)
    max_tokens_allowed = int(budget_usd / cost_per_token)

    print(f"Budget: ${budget_usd:.4f} → max {max_tokens_allowed} output tokens")

    collected = []
    tokens_used = 0
    budget_exceeded = False

    with client.messages.stream(
        model=model,
        max_tokens=min(max_tokens_allowed + 50, 4096),  # give slight headroom
        system=system,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        for text in stream.text_stream:
            # Rough token estimate: 4 chars ≈ 1 token
            chunk_tokens = max(1, len(text) // 4)
            tokens_used += chunk_tokens

            if tokens_used > max_tokens_allowed:
                budget_exceeded = True
                break

            collected.append(text)
            print(text, end="", flush=True)

    if budget_exceeded:
        truncation_msg = f"\n\n[Response halted: ${budget_usd:.4f} streaming budget reached after ~{tokens_used} tokens]"
        collected.append(truncation_msg)
        print(truncation_msg)

    return "".join(collected)


if __name__ == "__main__":
    result = stream_with_hard_budget(
        prompt="Write a 2000-word essay on the history of computing.",
        model="claude-sonnet-4-6",
        budget_usd=0.002,  # very tight budget to force truncation
    )
    print(f"\n\nFinal length: {len(result)} chars")

# Expected Token Savings: 60-90% cost reduction for budget-constrained tasks
# Environment: Any application where per-request or per-session spending limits are required
```

## Option 2: Per-Session Streaming Budget Tracker

Maintain a session-level budget that accumulates across multiple streaming calls and blocks new streams when exhausted.

```python
import anthropic
import time
import threading
from dataclasses import dataclass, field
from typing import Optional

@dataclass
class SessionBudget:
    budget_usd: float
    spent_usd: float = 0.0
    request_count: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def can_afford(self, estimated_cost: float) -> bool:
        with self._lock:
            return (self.spent_usd + estimated_cost) <= self.budget_usd

    def record_spend(self, tokens: int, model: str) -> float:
        cost_per_token = {
            "claude-haiku-4-5-20251001": 0.00000125,
            "claude-sonnet-4-6": 0.000015,
            "claude-opus-4-6": 0.000075,
        }.get(model, 0.000015)
        cost = tokens * cost_per_token
        with self._lock:
            self.spent_usd += cost
            self.request_count += 1
        return cost

    @property
    def remaining(self) -> float:
        with self._lock:
            return max(0.0, self.budget_usd - self.spent_usd)


class BudgetedStreamingSession:
    def __init__(self, session_id: str, budget_usd: float):
        self.session_id = session_id
        self.budget = SessionBudget(budget_usd=budget_usd)
        self.client = anthropic.Anthropic()

    def stream_message(
        self,
        prompt: str,
        model: str = "claude-sonnet-4-6",
        estimated_tokens: int = 500,
    ) -> Optional[str]:
        cost_per_token = {
            "claude-haiku-4-5-20251001": 0.00000125,
            "claude-sonnet-4-6": 0.000015,
            "claude-opus-4-6": 0.000075,
        }.get(model, 0.000015)
        estimated_cost = estimated_tokens * cost_per_token

        if not self.budget.can_afford(estimated_cost):
            print(
                f"[Session {self.session_id}] Budget exhausted. "
                f"Remaining: ${self.budget.remaining:.5f}, "
                f"Needed: ${estimated_cost:.5f}"
            )
            return None

        print(
            f"[Session {self.session_id}] Starting stream. "
            f"Budget remaining: ${self.budget.remaining:.5f}"
        )

        collected = []
        actual_tokens = 0

        with self.client.messages.stream(
            model=model,
            max_tokens=estimated_tokens,
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            for text in stream.text_stream:
                actual_tokens += max(1, len(text) // 4)

                # Mid-stream budget check
                mid_cost = actual_tokens * cost_per_token
                if mid_cost > self.budget.remaining:
                    print(f"\n[Budget limit reached mid-stream at {actual_tokens} tokens]")
                    break

                collected.append(text)
                print(text, end="", flush=True)

        spent = self.budget.record_spend(actual_tokens, model)
        print(f"\n[Spent: ${spent:.5f} | Total: ${self.budget.spent_usd:.5f}]")
        return "".join(collected)


if __name__ == "__main__":
    session = BudgetedStreamingSession(session_id="user-42", budget_usd=0.005)

    questions = [
        "What is machine learning?",
        "Explain neural networks in detail.",
        "Describe the full history of AI research from 1950 to today.",
    ]

    for q in questions:
        print(f"\n{'='*60}")
        print(f"Q: {q}")
        result = session.stream_message(q, model="claude-sonnet-4-6", estimated_tokens=400)
        if result is None:
            print("Skipped: no budget remaining")

# Expected Token Savings: 40-70% by enforcing session-level spending caps across multi-turn conversations
# Environment: Chatbot sessions, customer-facing applications with per-user spending limits
```

## Option 3: Streaming Cost Accumulator with Configurable Thresholds

Use a callback-based accumulator that fires warning and halt callbacks at configurable cost thresholds.

```python
import anthropic
from dataclasses import dataclass
from typing import Callable, Optional

@dataclass
class CostThreshold:
    warn_usd: float
    halt_usd: float
    on_warn: Optional[Callable[[float], None]] = None
    on_halt: Optional[Callable[[float], None]] = None


class StreamCostAccumulator:
    COST_PER_TOKEN = {
        "claude-haiku-4-5-20251001": 0.00000125,
        "claude-sonnet-4-6": 0.000015,
        "claude-opus-4-6": 0.000075,
    }

    def __init__(self, model: str, threshold: CostThreshold):
        self.model = model
        self.threshold = threshold
        self.total_tokens = 0
        self.total_cost = 0.0
        self._warned = False
        self._halted = False

    def _cost_per_token(self) -> float:
        return self.COST_PER_TOKEN.get(self.model, 0.000015)

    def feed(self, chunk: str) -> bool:
        """Returns False when streaming should halt."""
        if self._halted:
            return False

        chunk_tokens = max(1, len(chunk) // 4)
        self.total_tokens += chunk_tokens
        self.total_cost += chunk_tokens * self._cost_per_token()

        if not self._warned and self.total_cost >= self.threshold.warn_usd:
            self._warned = True
            if self.threshold.on_warn:
                self.threshold.on_warn(self.total_cost)

        if self.total_cost >= self.threshold.halt_usd:
            self._halted = True
            if self.threshold.on_halt:
                self.threshold.on_halt(self.total_cost)
            return False

        return True


def stream_with_thresholds(
    prompt: str,
    model: str = "claude-sonnet-4-6",
    warn_at_usd: float = 0.005,
    halt_at_usd: float = 0.010,
) -> str:
    client = anthropic.Anthropic()

    def on_warn(cost: float):
        print(f"\n[WARNING: Streaming cost ${cost:.5f} approaching limit ${halt_at_usd:.4f}]")

    def on_halt(cost: float):
        print(f"\n[HALTED: Streaming cost ${cost:.5f} exceeded limit ${halt_at_usd:.4f}]")

    threshold = CostThreshold(
        warn_usd=warn_at_usd,
        halt_usd=halt_at_usd,
        on_warn=on_warn,
        on_halt=on_halt,
    )
    accumulator = StreamCostAccumulator(model=model, threshold=threshold)

    collected = []

    with client.messages.stream(
        model=model,
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        for text in stream.text_stream:
            if not accumulator.feed(text):
                collected.append("\n[Stream truncated by cost budget]")
                break
            collected.append(text)
            print(text, end="", flush=True)

    print(f"\nTotal cost: ${accumulator.total_cost:.5f} over {accumulator.total_tokens} tokens")
    return "".join(collected)


if __name__ == "__main__":
    result = stream_with_thresholds(
        prompt="Write the most comprehensive guide to Python programming ever written.",
        model="claude-sonnet-4-6",
        warn_at_usd=0.001,
        halt_at_usd=0.003,
    )
    print(f"\nResult length: {len(result)} chars")

# Expected Token Savings: 50-80% on open-ended generation tasks with configurable spend tiers
# Environment: Developer tools, content generation pipelines with tiered cost controls
```

## Option 4: Async Stream with asyncio.wait_for Budget Timeout

Translate cost budget into a time estimate and use `asyncio.wait_for` to cancel the coroutine when it runs too long.

```python
import anthropic
import asyncio
import time
from typing import AsyncGenerator


async def budget_to_timeout(budget_usd: float, model: str, chars_per_second: float = 150) -> float:
    """Convert cost budget to approximate wall-clock timeout."""
    cost_per_token = {
        "claude-haiku-4-5-20251001": 0.00000125,
        "claude-sonnet-4-6": 0.000015,
        "claude-opus-4-6": 0.000075,
    }.get(model, 0.000015)
    max_tokens = budget_usd / cost_per_token
    # Assume ~4 chars per token at given chars/sec streaming speed
    timeout_seconds = (max_tokens * 4) / chars_per_second
    return max(5.0, timeout_seconds)  # minimum 5 seconds


async def stream_with_timeout_budget(
    prompt: str,
    model: str = "claude-sonnet-4-6",
    budget_usd: float = 0.005,
) -> str:
    client = anthropic.AsyncAnthropic()
    timeout = await budget_to_timeout(budget_usd, model)
    print(f"Budget ${budget_usd:.4f} → timeout {timeout:.1f}s")

    async def _stream() -> str:
        collected = []
        start = time.monotonic()

        async with client.messages.stream(
            model=model,
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            async for text in stream.text_stream:
                collected.append(text)
                print(text, end="", flush=True)

        elapsed = time.monotonic() - start
        print(f"\nCompleted in {elapsed:.2f}s")
        return "".join(collected)

    try:
        result = await asyncio.wait_for(_stream(), timeout=timeout)
        return result
    except asyncio.TimeoutError:
        msg = f"\n[Stream cancelled: exceeded {timeout:.1f}s budget window (${budget_usd:.4f})]"
        print(msg)
        return msg


async def main():
    result = await stream_with_timeout_budget(
        prompt="Explain the complete history of human civilization from 10000 BC to today.",
        model="claude-sonnet-4-6",
        budget_usd=0.003,
    )
    print(f"\nFinal length: {len(result)} chars")


if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: 55-75% by cutting streams based on wall-clock budget windows
# Environment: Latency-sensitive services where time and cost budgets are tightly coupled
```

## Option 5: Budget-Gated Model Tier Downgrade

Track cumulative streaming costs; automatically downgrade to a cheaper model when the primary model's budget is exhausted.

```python
import anthropic
from dataclasses import dataclass, field
from typing import Iterator

@dataclass
class TieredBudget:
    primary_model: str
    fallback_model: str
    primary_budget_usd: float
    fallback_budget_usd: float
    primary_spent: float = 0.0
    fallback_spent: float = 0.0

    COST_PER_TOKEN = {
        "claude-haiku-4-5-20251001": 0.00000125,
        "claude-sonnet-4-6": 0.000015,
        "claude-opus-4-6": 0.000075,
    }

    def active_model(self) -> str:
        if self.primary_spent < self.primary_budget_usd:
            return self.primary_model
        elif self.fallback_spent < self.fallback_budget_usd:
            return self.fallback_model
        return ""  # all budgets exhausted

    def record(self, model: str, tokens: int) -> float:
        cost = tokens * self.COST_PER_TOKEN.get(model, 0.000015)
        if model == self.primary_model:
            self.primary_spent += cost
        else:
            self.fallback_spent += cost
        return cost

    def remaining(self, model: str) -> float:
        if model == self.primary_model:
            return max(0.0, self.primary_budget_usd - self.primary_spent)
        return max(0.0, self.fallback_budget_usd - self.fallback_spent)


def stream_with_tier_downgrade(
    prompts: list[str],
    budget: TieredBudget,
) -> list[str]:
    client = anthropic.Anthropic()
    results = []

    for prompt in prompts:
        model = budget.active_model()
        if not model:
            print(f"All budgets exhausted. Skipping: {prompt[:40]}...")
            results.append("[SKIPPED: budget exhausted]")
            continue

        print(f"\nUsing {model} (remaining: ${budget.remaining(model):.5f})")
        collected = []
        tokens = 0

        with client.messages.stream(
            model=model,
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            for text in stream.text_stream:
                chunk_tokens = max(1, len(text) // 4)
                tokens += chunk_tokens

                # Bail if this model's budget is now exhausted mid-stream
                if budget.remaining(model) <= tokens * budget.COST_PER_TOKEN.get(model, 0.000015):
                    collected.append(" [truncated: switching models]")
                    break

                collected.append(text)
                print(text, end="", flush=True)

        cost = budget.record(model, tokens)
        print(f"\n[Cost: ${cost:.5f}]")
        results.append("".join(collected))

    return results


if __name__ == "__main__":
    budget = TieredBudget(
        primary_model="claude-sonnet-4-6",
        fallback_model="claude-haiku-4-5-20251001",
        primary_budget_usd=0.003,
        fallback_budget_usd=0.005,
    )

    questions = [
        "Explain transformer architecture in detail.",
        "What is the attention mechanism?",
        "Describe RLHF training for language models.",
        "What are the limitations of current LLMs?",
        "How does RAG improve LLM accuracy?",
    ]

    results = stream_with_tier_downgrade(questions, budget)
    print(f"\n\nProcessed {len([r for r in results if 'SKIPPED' not in r])} of {len(questions)} prompts")

# Expected Token Savings: 70-85% by routing overflow traffic to 10x cheaper Haiku model
# Environment: Production applications with tiered service levels and dynamic cost routing
```

## Option 6: Multi-Tenant Streaming Budget Allocation

Allocate streaming budgets per tenant/user with isolation so one tenant's overflow doesn't affect others.

```python
import anthropic
import asyncio
import sqlite3
import time
from dataclasses import dataclass
from typing import Optional

@dataclass
class TenantStreamBudget:
    tenant_id: str
    daily_budget_usd: float
    burst_limit_usd: float  # max single-request cost


class MultiTenantBudgetManager:
    COST_PER_TOKEN = {
        "claude-haiku-4-5-20251001": 0.00000125,
        "claude-sonnet-4-6": 0.000015,
        "claude-opus-4-6": 0.000075,
    }

    def __init__(self, db_path: str = "tenant_budgets.db"):
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self._init_db()
        self.client = anthropic.AsyncAnthropic()

    def _init_db(self):
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS tenant_spend (
                tenant_id TEXT NOT NULL,
                date TEXT NOT NULL,
                spent_usd REAL DEFAULT 0.0,
                request_count INTEGER DEFAULT 0,
                PRIMARY KEY (tenant_id, date)
            )
        """)
        self.db.commit()

    def get_daily_spent(self, tenant_id: str) -> float:
        today = time.strftime("%Y-%m-%d")
        row = self.db.execute(
            "SELECT spent_usd FROM tenant_spend WHERE tenant_id=? AND date=?",
            (tenant_id, today),
        ).fetchone()
        return row[0] if row else 0.0

    def record_spend(self, tenant_id: str, cost: float):
        today = time.strftime("%Y-%m-%d")
        self.db.execute("""
            INSERT INTO tenant_spend (tenant_id, date, spent_usd, request_count)
            VALUES (?, ?, ?, 1)
            ON CONFLICT(tenant_id, date) DO UPDATE SET
                spent_usd = spent_usd + excluded.spent_usd,
                request_count = request_count + 1
        """, (tenant_id, today, cost))
        self.db.commit()

    async def stream_for_tenant(
        self,
        tenant_id: str,
        budget: TenantStreamBudget,
        prompt: str,
        model: str = "claude-sonnet-4-6",
    ) -> Optional[str]:
        daily_spent = self.get_daily_spent(tenant_id)
        remaining_daily = budget.daily_budget_usd - daily_spent

        if remaining_daily <= 0:
            return f"[{tenant_id}] Daily budget exhausted (${daily_spent:.4f} / ${budget.daily_budget_usd:.4f})"

        cost_per_token = self.COST_PER_TOKEN.get(model, 0.000015)
        max_tokens = int(min(remaining_daily, budget.burst_limit_usd) / cost_per_token)
        max_tokens = min(max_tokens, 2048)

        print(f"[{tenant_id}] Streaming up to {max_tokens} tokens (${remaining_daily:.5f} remaining)")

        collected = []
        tokens_used = 0

        async with self.client.messages.stream(
            model=model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            async for text in stream.text_stream:
                chunk_tokens = max(1, len(text) // 4)
                tokens_used += chunk_tokens
                collected.append(text)

        cost = tokens_used * cost_per_token
        self.record_spend(tenant_id, cost)

        print(f"[{tenant_id}] Spent ${cost:.5f} | Daily total: ${daily_spent + cost:.5f}")
        return "".join(collected)


async def main():
    manager = MultiTenantBudgetManager(db_path=":memory:")

    tenants = [
        TenantStreamBudget("enterprise-A", daily_budget_usd=1.0, burst_limit_usd=0.05),
        TenantStreamBudget("startup-B", daily_budget_usd=0.10, burst_limit_usd=0.01),
        TenantStreamBudget("free-tier-C", daily_budget_usd=0.02, burst_limit_usd=0.005),
    ]

    budget_map = {t.tenant_id: t for t in tenants}

    requests = [
        ("enterprise-A", "Write a detailed analysis of microservices architecture."),
        ("startup-B", "What is Kubernetes?"),
        ("free-tier-C", "Hello!"),
        ("free-tier-C", "Write a 1000-word article about AI."),  # will be budget-constrained
    ]

    tasks = [
        manager.stream_for_tenant(tid, budget_map[tid], prompt)
        for tid, prompt in requests
    ]

    results = await asyncio.gather(*tasks)
    for (tenant_id, _), result in zip(requests, results):
        print(f"\n[{tenant_id}] Result: {result[:100]}...")


if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: 65-90% by isolating and capping per-tenant streaming consumption
# Environment: SaaS platforms with multi-tenant billing, usage-based pricing tiers
```

## Comparison

| Option | Mechanism | Granularity | Async | Best For |
|--------|-----------|-------------|-------|----------|
| 1 Hard Stop | Token counting per chunk | Per request | No | Simple single-request budget cap |
| 2 Session Tracker | Cumulative per session | Per session | No | Multi-turn conversations |
| 3 Threshold Callbacks | Warn + halt callbacks | Per request | No | Tiered alerts with custom actions |
| 4 Timeout Budget | Cost → time conversion | Per request | Yes | Latency+cost coupled environments |
| 5 Model Downgrade | Tier switching on exhaustion | Per model tier | No | Graceful degradation under budget |
| 6 Multi-Tenant SQLite | Per-tenant daily + burst | Per tenant/day | Yes | SaaS platforms with usage billing |
