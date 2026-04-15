---
layout: solution
title: "Agent Doesn't Implement Dynamic Model Selection by Load"
category: config
description: "Route requests to cheaper/faster models when the system is under load, quota is constrained, or latency budgets are tight — preserving throughput without manual intervention."
tags: [config, model-selection, load-balancing, cost-optimization, quota-management, python]
---

# Agent Doesn't Implement Dynamic Model Selection by Load

When all requests always go to the same model, a traffic spike or quota exhaustion causes uniform degradation. Dynamic model selection routes each request to the best available model given current conditions — balancing quality, cost, and latency in real time.

## Option 1: Latency-Budget-Based Selection

```python
import anthropic
import time

client = anthropic.Anthropic()

# Tier: latency budget (seconds) -> model
MODEL_TIERS = [
    (2.0, "claude-haiku-4-5-20251001"),    # very tight budget
    (8.0, "claude-sonnet-4-6"),             # moderate budget
    (float("inf"), "claude-opus-4-6"),      # no constraint
]

def select_model_by_budget(latency_budget_s: float) -> str:
    for threshold, model in MODEL_TIERS:
        if latency_budget_s <= threshold:
            return model
    return "claude-opus-4-6"

def call_with_budget(prompt: str, latency_budget_s: float) -> str:
    model = select_model_by_budget(latency_budget_s)
    print(f"Budget={latency_budget_s}s -> model={model}")

    start = time.perf_counter()
    resp = client.messages.create(
        model=model,
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )
    elapsed = time.perf_counter() - start
    print(f"Completed in {elapsed:.2f}s using {model}")
    return resp.content[0].text

# Tight deadline: use Haiku
result = call_with_budget("Summarize in one sentence: async vs sync Python.", latency_budget_s=1.5)
print(result)

# Relaxed deadline: use Opus
result = call_with_budget("Explain the CAP theorem with examples.", latency_budget_s=30.0)
print(result)

# Expected Token Savings: 60-80% reduction when Haiku handles high-volume/tight-budget requests
# Environment: any; no external dependencies beyond anthropic SDK
```

## Option 2: Error-Rate-Triggered Downgrade

```python
import anthropic
import time
import threading
from collections import deque
from dataclasses import dataclass, field

client = anthropic.Anthropic()

@dataclass
class ModelHealth:
    model: str
    errors: deque = field(default_factory=lambda: deque(maxlen=20))
    lock: threading.Lock = field(default_factory=threading.Lock)

    def record(self, success: bool):
        with self.lock:
            self.errors.append(0 if success else 1)

    @property
    def error_rate(self) -> float:
        with self.lock:
            if not self.errors:
                return 0.0
            return sum(self.errors) / len(self.errors)

MODELS = [
    ModelHealth("claude-opus-4-6"),
    ModelHealth("claude-sonnet-4-6"),
    ModelHealth("claude-haiku-4-5-20251001"),
]

def select_healthy_model(max_error_rate: float = 0.3) -> ModelHealth:
    for mh in MODELS:
        if mh.error_rate < max_error_rate:
            return mh
    # All degraded — fall back to fastest
    return MODELS[-1]

def call_with_fallback(prompt: str) -> str:
    mh = select_healthy_model()
    print(f"Using {mh.model} (error_rate={mh.error_rate:.0%})")
    try:
        resp = client.messages.create(
            model=mh.model,
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )
        mh.record(True)
        return resp.content[0].text
    except Exception as e:
        mh.record(False)
        print(f"Error on {mh.model}: {e}. Retrying with next tier.")
        # Try next model in chain
        for fallback in MODELS:
            if fallback.model != mh.model:
                try:
                    resp = client.messages.create(
                        model=fallback.model,
                        max_tokens=512,
                        messages=[{"role": "user", "content": prompt}],
                    )
                    fallback.record(True)
                    return resp.content[0].text
                except Exception:
                    fallback.record(False)
        raise RuntimeError("All models failed")

result = call_with_fallback("What is the difference between TCP and UDP?")
print(result)

# Expected Token Savings: Automatic cost reduction during error conditions; preserves availability
# Environment: multi-threaded servers; thread-safe via per-model locks
```

## Option 3: Queue-Depth-Based Selection

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass

client = anthropic.AsyncAnthropic()

@dataclass
class ModelPool:
    model: str
    semaphore: asyncio.Semaphore
    capacity: int

    @property
    def queue_depth(self) -> int:
        return self.capacity - self.semaphore._value  # type: ignore[attr-defined]

    @property
    def load_fraction(self) -> float:
        return self.queue_depth / self.capacity

POOLS = None  # Initialized in async context

def build_pools() -> list[ModelPool]:
    return [
        ModelPool("claude-haiku-4-5-20251001", asyncio.Semaphore(50), 50),
        ModelPool("claude-sonnet-4-6",          asyncio.Semaphore(20), 20),
        ModelPool("claude-opus-4-6",            asyncio.Semaphore(5),  5),
    ]

def select_pool_by_load(pools: list[ModelPool], task_priority: str) -> ModelPool:
    if task_priority == "high":
        # Prefer Opus if capacity available
        for pool in reversed(pools):
            if pool.load_fraction < 0.8:
                return pool
    # Default: use least-loaded pool that isn't overloaded
    available = [p for p in pools if pool.load_fraction < 0.9]
    if available:
        return min(available, key=lambda p: p.load_fraction)
    return pools[0]  # Haiku as last resort

async def call_with_load_balancing(
    prompt: str,
    pools: list[ModelPool],
    priority: str = "normal",
) -> str:
    pool = select_pool_by_load(pools, priority)
    print(f"[{priority}] -> {pool.model} (load={pool.load_fraction:.0%})")
    async with pool.semaphore:
        resp = await client.messages.create(
            model=pool.model,
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )
    return resp.content[0].text

async def main():
    pools = build_pools()
    prompts = [
        ("Summarize: REST vs GraphQL", "normal"),
        ("Explain quantum entanglement in depth", "high"),
        ("What is 2+2?", "low"),
    ]
    results = await asyncio.gather(*[
        call_with_load_balancing(p, pools, prio) for p, prio in prompts
    ])
    for r in results:
        print(r[:80])

asyncio.run(main())

# Expected Token Savings: Routes bulk/low-priority to Haiku; high-priority gets Opus when idle
# Environment: async servers (FastAPI, aiohttp); semaphore tracks in-flight capacity
```

## Option 4: Token-Quota-Aware Selection

```python
import anthropic
import sqlite3
import time
from datetime import datetime, timedelta

client = anthropic.Anthropic()
DB = "quota.db"

def init_db():
    con = sqlite3.connect(DB)
    con.execute("""
        CREATE TABLE IF NOT EXISTS usage (
            model TEXT,
            ts    REAL,
            input_tokens  INTEGER,
            output_tokens INTEGER
        )
    """)
    con.commit()
    con.close()

def record_usage(model: str, input_tok: int, output_tok: int):
    con = sqlite3.connect(DB)
    con.execute("INSERT INTO usage VALUES (?,?,?,?)",
                (model, time.time(), input_tok, output_tok))
    con.commit()
    con.close()

def tokens_used_last_minute(model: str) -> int:
    cutoff = time.time() - 60
    con = sqlite3.connect(DB)
    row = con.execute(
        "SELECT COALESCE(SUM(input_tokens+output_tokens),0) FROM usage WHERE model=? AND ts>?",
        (model, cutoff)
    ).fetchone()
    con.close()
    return row[0]

# Approximate per-minute token limits (adjust to your tier)
MODEL_LIMITS = {
    "claude-opus-4-6":           20_000,
    "claude-sonnet-4-6":         40_000,
    "claude-haiku-4-5-20251001": 100_000,
}

MODEL_PREFERENCE = ["claude-opus-4-6", "claude-sonnet-4-6", "claude-haiku-4-5-20251001"]

def select_model_by_quota(estimated_tokens: int = 500) -> str:
    for model in MODEL_PREFERENCE:
        used = tokens_used_last_minute(model)
        limit = MODEL_LIMITS[model]
        remaining = limit - used
        if remaining >= estimated_tokens * 1.2:  # 20% headroom
            print(f"Selected {model} (used={used}/{limit})")
            return model
    print("All models near quota; using Haiku as last resort")
    return "claude-haiku-4-5-20251001"

def call_with_quota_awareness(prompt: str) -> str:
    model = select_model_by_quota(estimated_tokens=300)
    resp = client.messages.create(
        model=model,
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )
    record_usage(model, resp.usage.input_tokens, resp.usage.output_tokens)
    return resp.content[0].text

init_db()
result = call_with_quota_awareness("Explain eventual consistency.")
print(result)

# Expected Token Savings: Prevents quota exhaustion on premium models; shifts load to cheaper tier
# Environment: single-process; SQLite persists across restarts; adjust MODEL_LIMITS to your plan
```

## Option 5: Complexity-Scored Routing

```python
import anthropic
import re

client = anthropic.Anthropic()

def score_complexity(prompt: str) -> float:
    """Return a 0.0–1.0 complexity score for the prompt."""
    score = 0.0
    words = prompt.split()
    # Length signal
    score += min(len(words) / 200, 0.3)
    # Technical terms
    technical = ["algorithm", "architecture", "distributed", "optimize",
                 "tradeoff", "concurrency", "theorem", "formal", "proof"]
    matches = sum(1 for w in words if w.lower() in technical)
    score += min(matches * 0.05, 0.3)
    # Multi-part questions
    if re.search(r"\b(and|also|additionally|furthermore|compare|contrast)\b", prompt, re.I):
        score += 0.2
    # Code present
    if "```" in prompt or "def " in prompt or "class " in prompt:
        score += 0.2
    return min(score, 1.0)

def select_model_by_complexity(complexity: float) -> str:
    if complexity < 0.3:
        return "claude-haiku-4-5-20251001"
    elif complexity < 0.65:
        return "claude-sonnet-4-6"
    else:
        return "claude-opus-4-6"

def call_with_complexity_routing(prompt: str) -> str:
    complexity = score_complexity(prompt)
    model = select_model_by_complexity(complexity)
    print(f"Complexity={complexity:.2f} -> {model}")
    resp = client.messages.create(
        model=model,
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text

# Simple query -> Haiku
print(call_with_complexity_routing("What does HTTP stand for?"))

# Complex query -> Opus
print(call_with_complexity_routing(
    "Compare and contrast the CAP theorem tradeoffs in distributed systems "
    "with formal proofs and real-world architecture examples."
))

# Expected Token Savings: 70% cost reduction by routing simple queries to Haiku automatically
# Environment: any; pure Python scoring, no external dependencies
```

## Option 6: Time-of-Day + Cost-Ceiling Selection

```python
import anthropic
import time
import sqlite3
from datetime import datetime

client = anthropic.Anthropic()
DB = "cost_ceil.db"

# USD cost per 1M tokens (approximate)
MODEL_COST = {
    "claude-opus-4-6":           {"input": 15.0, "output": 75.0},
    "claude-sonnet-4-6":         {"input": 3.0,  "output": 15.0},
    "claude-haiku-4-5-20251001": {"input": 0.8,  "output": 4.0},
}

DAILY_BUDGET_USD = 5.0  # Stop using expensive models after this

def init_db():
    con = sqlite3.connect(DB)
    con.execute("""
        CREATE TABLE IF NOT EXISTS spend (
            date TEXT, model TEXT,
            input_tokens INTEGER, output_tokens INTEGER
        )
    """)
    con.commit(); con.close()

def today_spend_usd() -> float:
    today = datetime.utcnow().date().isoformat()
    con = sqlite3.connect(DB)
    rows = con.execute(
        "SELECT model, SUM(input_tokens), SUM(output_tokens) FROM spend WHERE date=? GROUP BY model",
        (today,)
    ).fetchall()
    con.close()
    total = 0.0
    for model, inp, out in rows:
        c = MODEL_COST.get(model, {"input": 3.0, "output": 15.0})
        total += (inp * c["input"] + out * c["output"]) / 1_000_000
    return total

def record_spend(model: str, inp: int, out: int):
    today = datetime.utcnow().date().isoformat()
    con = sqlite3.connect(DB)
    con.execute("INSERT INTO spend VALUES (?,?,?,?)", (today, model, inp, out))
    con.commit(); con.close()

def hour_of_day() -> int:
    return datetime.utcnow().hour

def select_model_with_ceiling(task_importance: str = "normal") -> str:
    spent = today_spend_usd()
    hour = hour_of_day()
    budget_pct = spent / DAILY_BUDGET_USD

    print(f"Spent ${spent:.4f}/{DAILY_BUDGET_USD} ({budget_pct:.0%}), hour={hour}UTC")

    if budget_pct > 0.8 or (hour >= 18 and task_importance != "critical"):
        return "claude-haiku-4-5-20251001"  # Late-day or near budget: save cost
    elif budget_pct > 0.5 or task_importance == "normal":
        return "claude-sonnet-4-6"
    else:
        return "claude-opus-4-6"

def call_with_ceiling(prompt: str, importance: str = "normal") -> str:
    model = select_model_with_ceiling(importance)
    resp = client.messages.create(
        model=model,
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )
    record_spend(model, resp.usage.input_tokens, resp.usage.output_tokens)
    return resp.content[0].text

init_db()
result = call_with_ceiling("Explain microservices vs monolith.", importance="normal")
print(result)

# Expected Token Savings: Enforces daily budget ceiling; off-peak/low-importance traffic shifted to Haiku
# Environment: single-process; SQLite persists daily spend; adjust DAILY_BUDGET_USD to your limit
```

## Comparison

| Option | Routing Signal | Strengths | Weaknesses |
|--------|---------------|-----------|------------|
| 1 — Latency Budget | Caller-provided deadline | Zero overhead, deterministic | Requires callers to know their budget |
| 2 — Error Rate | Recent failure rate per model | Auto-heals on outages | Short window may cause flapping |
| 3 — Queue Depth | In-flight semaphore count | Prevents overload per pool | In-memory; resets on restart |
| 4 — Token Quota | Per-minute token usage | Respects rate limits proactively | Requires accurate limit constants |
| 5 — Complexity Score | Prompt heuristics | No infra needed; zero latency | Heuristic may misclassify edge cases |
| 6 — Cost Ceiling | Daily USD spend + time | Hard budget enforcement | Coarse granularity; UTC-only |
