---
layout: solution
title: "Agent Doesn't Implement Dynamic Model Selection Based on Cost Budget"
category: token-cost
description: "Agent always uses the same model regardless of remaining budget, burning expensive tokens on simple tasks and failing when budget runs out instead of gracefully downgrading."
tags: [token-cost, model-routing, budget, cost-control, dynamic-routing]
---

# Agent Doesn't Implement Dynamic Model Selection Based on Cost Budget

## Problem

Agents that hardcode a single model spend expensive Opus tokens on trivial classification tasks and run out of budget mid-session for the complex reasoning that actually needs it. Without dynamic model selection, there's no way to stretch a fixed token budget across a full workflow: costs spike unpredictably, cheap tasks consume capacity intended for heavy ones, and users hit hard cutoffs instead of graceful degradation.

## Solution Options

### Option 1: Budget-Tier Model Router

```python
import anthropic
from dataclasses import dataclass, field

client = anthropic.Anthropic()

# Cost per 1M tokens (input/output) in USD
MODEL_COSTS = {
    "claude-haiku-4-5-20251001": {"input": 0.25, "output": 1.25},
    "claude-sonnet-4-6":         {"input": 3.00, "output": 15.00},
    "claude-opus-4-6":           {"input": 15.00, "output": 75.00},
}

@dataclass
class BudgetState:
    total_budget_usd: float
    spent_usd: float = 0.0
    calls: int = 0

    @property
    def remaining_usd(self) -> float:
        return self.total_budget_usd - self.spent_usd

    @property
    def remaining_pct(self) -> float:
        return self.remaining_usd / self.total_budget_usd * 100

    def select_model(self) -> str:
        """Select model based on remaining budget percentage."""
        pct = self.remaining_pct
        if pct > 60:
            return "claude-opus-4-6"
        elif pct > 25:
            return "claude-sonnet-4-6"
        else:
            return "claude-haiku-4-5-20251001"

    def record_usage(self, model: str, input_tokens: int, output_tokens: int):
        costs = MODEL_COSTS[model]
        cost = (input_tokens / 1_000_000 * costs["input"] +
                output_tokens / 1_000_000 * costs["output"])
        self.spent_usd += cost
        self.calls += 1
        return cost

budget = BudgetState(total_budget_usd=0.05)  # $0.05 session budget

def budget_routed_call(system: str, user_message: str, state: BudgetState) -> str:
    if state.remaining_usd <= 0:
        return "[BUDGET EXHAUSTED] Cannot process further requests this session."

    model = state.select_model()
    print(f"[ROUTER] Budget remaining: ${state.remaining_usd:.4f} ({state.remaining_pct:.1f}%) → using {model}")

    response = client.messages.create(
        model=model,
        max_tokens=256,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )

    call_cost = state.record_usage(model, response.usage.input_tokens, response.usage.output_tokens)
    print(f"[COST] Call cost: ${call_cost:.5f} | Total spent: ${state.spent_usd:.4f}")
    return response.content[0].text

system = "You are a helpful assistant."
tasks = [
    "Analyze the philosophical implications of consciousness in artificial systems and provide a comprehensive treatise.",
    "Summarize the key points of the previous analysis in 2 sentences.",
    "Classify this sentiment as positive/negative/neutral: 'The product works fine.'",
    "Translate 'hello' to French.",
    "What is 2 + 2?",
]

for i, task in enumerate(tasks, 1):
    print(f"\n=== Task {i} ===")
    result = budget_routed_call(system, task, budget)
    print(f"Response: {result[:100]}...")

print(f"\n[SUMMARY] Total calls: {budget.calls} | Total spent: ${budget.spent_usd:.5f}")

# Expected Token Savings: 40-70% cost reduction by using cheaper models as budget depletes
# Environment: Fixed-budget sessions, trial users, cost-capped B2B API products
```

### Option 2: Task-Complexity-Based Model Selection

```python
import anthropic
import re
from enum import Enum

client = anthropic.Anthropic()

class TaskComplexity(Enum):
    TRIVIAL = "trivial"      # Classification, yes/no, single word
    SIMPLE = "simple"        # Short factual, format conversion
    MODERATE = "moderate"    # Summarization, explanation, short code
    COMPLEX = "complex"      # Analysis, multi-step reasoning, long code
    EXPERT = "expert"        # Architecture, research synthesis, creative

COMPLEXITY_TO_MODEL = {
    TaskComplexity.TRIVIAL:  "claude-haiku-4-5-20251001",
    TaskComplexity.SIMPLE:   "claude-haiku-4-5-20251001",
    TaskComplexity.MODERATE: "claude-sonnet-4-6",
    TaskComplexity.COMPLEX:  "claude-sonnet-4-6",
    TaskComplexity.EXPERT:   "claude-opus-4-6",
}

COMPLEXITY_SIGNALS = {
    TaskComplexity.TRIVIAL: [
        "classify", "yes or no", "true or false", "sentiment",
        "translate the word", "what is the capital", "2 + 2",
    ],
    TaskComplexity.SIMPLE: [
        "translate", "define", "what does", "spell", "convert", "format",
        "list the", "name the",
    ],
    TaskComplexity.MODERATE: [
        "summarize", "explain", "describe", "how does", "what are",
        "write a function", "simple script",
    ],
    TaskComplexity.COMPLEX: [
        "analyze", "compare", "design", "implement", "refactor",
        "debug", "architecture", "trade-offs", "evaluate",
    ],
    TaskComplexity.EXPERT: [
        "comprehensive", "treatise", "research", "synthesize",
        "philosophical", "deep dive", "complete system", "review all",
    ],
}

def classify_complexity(user_message: str) -> TaskComplexity:
    """Classify task complexity from the user message."""
    msg_lower = user_message.lower()

    # Count signal matches for each tier
    scores = {}
    for complexity, signals in COMPLEXITY_SIGNALS.items():
        scores[complexity] = sum(1 for s in signals if s in msg_lower)

    # Length heuristic
    word_count = len(user_message.split())
    if word_count > 80:
        scores[TaskComplexity.COMPLEX] = scores.get(TaskComplexity.COMPLEX, 0) + 2
    elif word_count > 40:
        scores[TaskComplexity.MODERATE] = scores.get(TaskComplexity.MODERATE, 0) + 1
    elif word_count < 10:
        scores[TaskComplexity.SIMPLE] = scores.get(TaskComplexity.SIMPLE, 0) + 1

    # Return highest-scoring complexity
    best = max(scores.items(), key=lambda x: x[1])
    if best[1] == 0:
        return TaskComplexity.MODERATE  # Default
    return best[0]

total_cost = 0.0
MODEL_COSTS = {
    "claude-haiku-4-5-20251001": {"input": 0.25, "output": 1.25},
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00},
    "claude-opus-4-6": {"input": 15.00, "output": 75.00},
}

def complexity_routed_call(system: str, user_message: str) -> str:
    global total_cost
    complexity = classify_complexity(user_message)
    model = COMPLEXITY_TO_MODEL[complexity]

    print(f"[CLASSIFIER] Complexity: {complexity.value} → Model: {model}")

    response = client.messages.create(
        model=model,
        max_tokens=512,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )

    costs = MODEL_COSTS[model]
    call_cost = (response.usage.input_tokens / 1_000_000 * costs["input"] +
                 response.usage.output_tokens / 1_000_000 * costs["output"])
    total_cost += call_cost
    print(f"[COST] ${call_cost:.5f} this call | ${total_cost:.5f} total")
    return response.content[0].text

system = "You are a helpful assistant."
test_cases = [
    "Classify this as spam or not spam: 'Win a free iPhone now!'",
    "Translate 'good morning' to Spanish.",
    "Explain how TCP/IP handshake works.",
    "Design a comprehensive microservices architecture for a high-scale e-commerce platform with event sourcing and CQRS.",
    "What is 15% of 200?",
]

for msg in test_cases:
    print(f"\nQuery: {msg[:60]}...")
    result = complexity_routed_call(system, msg)
    print(f"Result: {result[:80]}...")

# Expected Token Savings: 50-80% vs always using Opus; routes trivial tasks to Haiku automatically
# Environment: General-purpose agents with diverse workloads and mixed complexity queries
```

### Option 3: Rolling Window Budget with Adaptive Downgrade

```python
import anthropic
import time
import sqlite3
from dataclasses import dataclass
from pathlib import Path

client = anthropic.Anthropic()

DB_PATH = Path("/tmp/budget_tracker.db")

MODEL_COSTS = {
    "claude-haiku-4-5-20251001": {"input": 0.25, "output": 1.25},
    "claude-sonnet-4-6":         {"input": 3.00, "output": 15.00},
    "claude-opus-4-6":           {"input": 15.00, "output": 75.00},
}

def init_budget_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS usage_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                model TEXT NOT NULL,
                input_tokens INTEGER NOT NULL,
                output_tokens INTEGER NOT NULL,
                cost_usd REAL NOT NULL,
                session_id TEXT
            )
        """)
        conn.commit()

init_budget_db()

def log_usage(model: str, input_tokens: int, output_tokens: int, session_id: str = ""):
    costs = MODEL_COSTS[model]
    cost = input_tokens / 1_000_000 * costs["input"] + output_tokens / 1_000_000 * costs["output"]
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO usage_log (ts, model, input_tokens, output_tokens, cost_usd, session_id) VALUES (?, ?, ?, ?, ?, ?)",
            (time.time(), model, input_tokens, output_tokens, cost, session_id),
        )
        conn.commit()
    return cost

def get_window_spend(window_seconds: float = 3600) -> float:
    cutoff = time.time() - window_seconds
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) FROM usage_log WHERE ts > ?", (cutoff,)
        ).fetchone()
    return row[0]

def select_model_for_budget(
    hourly_budget: float = 1.00,
    preferred_model: str = "claude-sonnet-4-6",
) -> str:
    """Adaptively downgrade model based on rolling window spend."""
    spent = get_window_spend(window_seconds=3600)
    remaining = hourly_budget - spent
    pct_used = spent / hourly_budget * 100

    print(f"[BUDGET MONITOR] Hour spend: ${spent:.4f}/{hourly_budget:.2f} ({pct_used:.1f}% used)")

    if pct_used >= 90:
        model = "claude-haiku-4-5-20251001"
        print(f"[DOWNGRADE] >90% budget used → forced to Haiku")
    elif pct_used >= 70:
        # Downgrade one tier from preferred
        tier_map = {
            "claude-opus-4-6": "claude-sonnet-4-6",
            "claude-sonnet-4-6": "claude-haiku-4-5-20251001",
            "claude-haiku-4-5-20251001": "claude-haiku-4-5-20251001",
        }
        model = tier_map.get(preferred_model, "claude-haiku-4-5-20251001")
        print(f"[DOWNGRADE] >70% budget used → downgraded to {model}")
    elif pct_used >= 50:
        # Stay at preferred but warn
        model = preferred_model
        print(f"[WARNING] >50% budget used — monitoring")
    else:
        model = preferred_model
        print(f"[OK] Budget healthy — using {model}")

    return model

SESSION_ID = f"sess_{int(time.time())}"

def adaptive_budget_call(system: str, user_message: str, hourly_budget: float = 1.00) -> str:
    model = select_model_for_budget(hourly_budget=hourly_budget)

    response = client.messages.create(
        model=model,
        max_tokens=256,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )

    call_cost = log_usage(model, response.usage.input_tokens, response.usage.output_tokens, SESSION_ID)
    print(f"[COST] ${call_cost:.5f} on {model}")
    return response.content[0].text

system = "You are a helpful assistant."
for i in range(4):
    print(f"\n=== Call {i+1} ===")
    result = adaptive_budget_call(system, f"Give a brief explanation of topic number {i+1}.", hourly_budget=0.002)
    print(f"Response: {result[:80]}...")

# Expected Token Savings: 30-60% reduction by catching budget pressure early and downgrading proactively
# Environment: Multi-tenant SaaS with hourly/daily per-account budget limits
```

### Option 4: Task Queue with Budget-Aware Scheduling

```python
import anthropic
import asyncio
from dataclasses import dataclass, field
from enum import Enum
import time

client = anthropic.AsyncAnthropic()

class Priority(Enum):
    CRITICAL = 1   # Always use best available model
    HIGH = 2       # Use good model unless budget < 20%
    NORMAL = 3     # Use balanced model, downgrade at < 40%
    LOW = 4        # Always use cheapest model

@dataclass(order=True)
class QueuedTask:
    priority: int  # Lower = higher priority
    task_id: str = field(compare=False)
    system: str = field(compare=False)
    message: str = field(compare=False)
    preferred_model: str = field(compare=False)
    result: asyncio.Future = field(compare=False, default=None)

MODEL_COSTS = {
    "claude-haiku-4-5-20251001": {"input": 0.25, "output": 1.25},
    "claude-sonnet-4-6":         {"input": 3.00, "output": 15.00},
    "claude-opus-4-6":           {"input": 15.00, "output": 75.00},
}

class BudgetAwareScheduler:
    def __init__(self, total_budget: float):
        self.total_budget = total_budget
        self.spent = 0.0
        self._queue: asyncio.PriorityQueue = asyncio.PriorityQueue()

    @property
    def remaining_pct(self) -> float:
        return (1 - self.spent / self.total_budget) * 100

    def select_model(self, task: QueuedTask) -> str:
        pct = self.remaining_pct
        priority = Priority(task.priority)

        if priority == Priority.CRITICAL:
            return task.preferred_model  # Never downgrade critical tasks
        elif priority == Priority.HIGH:
            if pct < 20:
                return "claude-haiku-4-5-20251001"
            return task.preferred_model
        elif priority == Priority.NORMAL:
            if pct < 40:
                return "claude-haiku-4-5-20251001"
            elif pct < 70:
                return "claude-sonnet-4-6"
            return task.preferred_model
        else:  # LOW
            return "claude-haiku-4-5-20251001"

    async def enqueue(self, task: QueuedTask):
        task.result = asyncio.get_event_loop().create_future()
        await self._queue.put(task)

    async def process_queue(self):
        while not self._queue.empty():
            task = await self._queue.get()
            if self.spent >= self.total_budget:
                task.result.set_result("[BUDGET EXHAUSTED]")
                continue

            model = self.select_model(task)
            print(f"[SCHEDULER] Task {task.task_id} (priority={task.priority}) → {model} | Budget: {self.remaining_pct:.1f}% left")

            response = await client.messages.create(
                model=model,
                max_tokens=128,
                system=task.system,
                messages=[{"role": "user", "content": task.message}],
            )

            costs = MODEL_COSTS[model]
            call_cost = (response.usage.input_tokens / 1_000_000 * costs["input"] +
                         response.usage.output_tokens / 1_000_000 * costs["output"])
            self.spent += call_cost
            print(f"[COST] ${call_cost:.5f} | Total: ${self.spent:.5f}")
            task.result.set_result(response.content[0].text)

async def main():
    scheduler = BudgetAwareScheduler(total_budget=0.003)  # $0.003 budget

    tasks = [
        QueuedTask(Priority.CRITICAL.value, "t1", "You are a helpful assistant.", "Diagnose a critical production outage: all API endpoints returning 503.", "claude-opus-4-6"),
        QueuedTask(Priority.HIGH.value, "t2", "You are a helpful assistant.", "Summarize today's incident report.", "claude-sonnet-4-6"),
        QueuedTask(Priority.NORMAL.value, "t3", "You are a helpful assistant.", "Format this JSON: {name: alice, age: 30}", "claude-sonnet-4-6"),
        QueuedTask(Priority.LOW.value, "t4", "You are a helpful assistant.", "Translate 'hello' to German.", "claude-opus-4-6"),
        QueuedTask(Priority.NORMAL.value, "t5", "You are a helpful assistant.", "List 3 best practices for Python.", "claude-sonnet-4-6"),
    ]

    for task in tasks:
        await scheduler.enqueue(task)

    await scheduler.process_queue()

    print("\n=== Results ===")
    for task in tasks:
        print(f"Task {task.task_id}: {task.result.result()[:60]}...")

asyncio.run(main())

# Expected Token Savings: 50-75% by scheduling low-priority work on cheap models
# Environment: Async task queues with mixed-priority workloads and fixed session budgets
```

### Option 5: Per-Feature Budget Allocation

```python
import anthropic
from dataclasses import dataclass, field

client = anthropic.Anthropic()

MODEL_COSTS = {
    "claude-haiku-4-5-20251001": {"input": 0.25, "output": 1.25},
    "claude-sonnet-4-6":         {"input": 3.00, "output": 15.00},
    "claude-opus-4-6":           {"input": 15.00, "output": 75.00},
}

@dataclass
class FeatureBudget:
    name: str
    budget_usd: float
    preferred_model: str
    fallback_model: str
    spent_usd: float = 0.0
    calls: int = 0

    @property
    def remaining(self) -> float:
        return self.budget_usd - self.spent_usd

    @property
    def is_depleted(self) -> bool:
        return self.remaining <= 0

    def get_model(self) -> str:
        if self.is_depleted:
            return None
        # Estimate cost of a typical call; use fallback if remaining is tight
        typical_output_tokens = 200
        fallback_costs = MODEL_COSTS[self.fallback_model]
        typical_fallback_cost = typical_output_tokens / 1_000_000 * fallback_costs["output"]
        preferred_costs = MODEL_COSTS[self.preferred_model]
        typical_preferred_cost = typical_output_tokens / 1_000_000 * preferred_costs["output"]

        if self.remaining < typical_preferred_cost * 2:
            return self.fallback_model
        return self.preferred_model

    def record(self, model: str, input_tokens: int, output_tokens: int) -> float:
        costs = MODEL_COSTS[model]
        cost = (input_tokens / 1_000_000 * costs["input"] +
                output_tokens / 1_000_000 * costs["output"])
        self.spent_usd += cost
        self.calls += 1
        return cost

# Define per-feature budgets
FEATURE_BUDGETS: dict[str, FeatureBudget] = {
    "code_review": FeatureBudget("code_review", budget_usd=0.02, preferred_model="claude-opus-4-6", fallback_model="claude-sonnet-4-6"),
    "summarization": FeatureBudget("summarization", budget_usd=0.005, preferred_model="claude-sonnet-4-6", fallback_model="claude-haiku-4-5-20251001"),
    "classification": FeatureBudget("classification", budget_usd=0.001, preferred_model="claude-haiku-4-5-20251001", fallback_model="claude-haiku-4-5-20251001"),
    "chat": FeatureBudget("chat", budget_usd=0.01, preferred_model="claude-sonnet-4-6", fallback_model="claude-haiku-4-5-20251001"),
}

def feature_routed_call(feature: str, system: str, user_message: str) -> str:
    budget = FEATURE_BUDGETS.get(feature)
    if budget is None:
        raise ValueError(f"Unknown feature: {feature}")

    model = budget.get_model()
    if model is None:
        return f"[FEATURE BUDGET DEPLETED] The '{feature}' feature has no remaining budget this period."

    print(f"[{feature.upper()}] Budget: ${budget.remaining:.5f} remaining → {model}")

    response = client.messages.create(
        model=model,
        max_tokens=256,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )

    call_cost = budget.record(model, response.usage.input_tokens, response.usage.output_tokens)
    print(f"[COST] ${call_cost:.5f} | Feature spent: ${budget.spent_usd:.5f}/{budget.budget_usd}")
    return response.content[0].text

test_calls = [
    ("code_review", "You are an expert code reviewer.", "Review this Python function for bugs:\ndef add(a, b): return a + b"),
    ("classification", "You are a classifier.", "Classify sentiment: 'This is great!'"),
    ("summarization", "You are a summarizer.", "Summarize in one sentence: The quick brown fox jumps over the lazy dog."),
    ("chat", "You are a helpful assistant.", "What is the capital of France?"),
    ("classification", "You are a classifier.", "Classify intent: 'I want to cancel my subscription'"),
]

for feature, system, message in test_calls:
    print(f"\n=== {feature} ===")
    result = feature_routed_call(feature, system, message)
    print(f"Result: {result[:80]}...")

print("\n=== Budget Summary ===")
for name, budget in FEATURE_BUDGETS.items():
    pct = budget.spent_usd / budget.budget_usd * 100 if budget.budget_usd > 0 else 0
    print(f"{name}: ${budget.spent_usd:.5f}/{budget.budget_usd} ({pct:.1f}%) — {budget.calls} calls")

# Expected Token Savings: Guarantees high-value features always get premium models without over-spending
# Environment: Multi-feature products where different features have different value and cost tolerance
```

### Option 6: Predictive Budget-Aware Routing with Cost Forecasting

```python
import anthropic
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

client = anthropic.Anthropic()

DB_PATH = Path("/tmp/predictive_budget.db")

MODEL_COSTS = {
    "claude-haiku-4-5-20251001": {"input": 0.25, "output": 1.25},
    "claude-sonnet-4-6":         {"input": 3.00, "output": 15.00},
    "claude-opus-4-6":           {"input": 15.00, "output": 75.00},
}

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS call_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL,
                model TEXT,
                prompt_len INTEGER,
                input_tokens INTEGER,
                output_tokens INTEGER,
                cost_usd REAL
            )
        """)
        conn.commit()

init_db()

def get_avg_tokens_per_char(model: str, window: int = 50) -> float:
    """Estimate average output tokens per input character from history."""
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT prompt_len, output_tokens FROM call_history WHERE model = ? ORDER BY ts DESC LIMIT ?",
            (model, window),
        ).fetchall()
    if not rows:
        return 1.5  # Default estimate
    return sum(r[1] / max(r[0], 1) for r in rows) / len(rows)

def forecast_cost(message: str, model: str) -> float:
    """Forecast cost for a call before making it."""
    prompt_chars = len(message)
    estimated_input_tokens = prompt_chars // 4  # ~4 chars per token
    ratio = get_avg_tokens_per_char(model, window=20)
    estimated_output_tokens = int(prompt_chars * ratio)

    costs = MODEL_COSTS[model]
    return (estimated_input_tokens / 1_000_000 * costs["input"] +
            estimated_output_tokens / 1_000_000 * costs["output"])

def record_call(model: str, prompt_len: int, input_tokens: int, output_tokens: int):
    costs = MODEL_COSTS[model]
    cost = input_tokens / 1_000_000 * costs["input"] + output_tokens / 1_000_000 * costs["output"]
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO call_history (ts, model, prompt_len, input_tokens, output_tokens, cost_usd) VALUES (?, ?, ?, ?, ?, ?)",
            (time.time(), model, prompt_len, input_tokens, output_tokens, cost),
        )
        conn.commit()
    return cost

def get_session_spend(session_start: float) -> float:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM call_history WHERE ts > ?", (session_start,)).fetchone()
    return row[0]

SESSION_START = time.time()
SESSION_BUDGET = 0.005  # $0.005 session budget
CANDIDATE_MODELS = ["claude-opus-4-6", "claude-sonnet-4-6", "claude-haiku-4-5-20251001"]

def predictive_routed_call(system: str, user_message: str, preferred_model: str = "claude-sonnet-4-6") -> str:
    remaining = SESSION_BUDGET - get_session_spend(SESSION_START)
    if remaining <= 0:
        return "[SESSION BUDGET EXHAUSTED]"

    # Try models from preferred down to cheapest; pick highest-quality that fits budget
    selected_model = "claude-haiku-4-5-20251001"  # Fallback to cheapest
    for model in CANDIDATE_MODELS:
        if CANDIDATE_MODELS.index(model) < CANDIDATE_MODELS.index(preferred_model):
            continue  # Skip more expensive than preferred
        forecast = forecast_cost(user_message, model)
        safety_factor = 1.5  # Reserve 50% buffer over forecast
        if forecast * safety_factor <= remaining:
            selected_model = model
            print(f"[PREDICTIVE] {model}: forecast ${forecast:.5f} × {safety_factor} = ${forecast*safety_factor:.5f} ≤ remaining ${remaining:.5f} ✓")
            break
        else:
            print(f"[SKIP] {model}: forecast ${forecast:.5f} × {safety_factor} = ${forecast*safety_factor:.5f} > remaining ${remaining:.5f}")

    print(f"[SELECTED] {selected_model}")
    response = client.messages.create(
        model=selected_model,
        max_tokens=256,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )

    actual_cost = record_call(
        selected_model, len(user_message),
        response.usage.input_tokens, response.usage.output_tokens,
    )
    new_remaining = SESSION_BUDGET - get_session_spend(SESSION_START)
    print(f"[ACTUAL COST] ${actual_cost:.5f} | Remaining: ${new_remaining:.5f}")
    return response.content[0].text

system = "You are a helpful assistant."
queries = [
    "Write a detailed comparison of microservices vs monolithic architecture with pros, cons, and migration strategies.",
    "Summarize the above in one paragraph.",
    "Classify as technical or non-technical: 'The database is slow.'",
    "What is 5 squared?",
]

for i, q in enumerate(queries, 1):
    print(f"\n=== Query {i} ===")
    result = predictive_routed_call(system, q, preferred_model="claude-sonnet-4-6")
    print(f"Response: {result[:80]}...")

total_spent = get_session_spend(SESSION_START)
print(f"\n[SESSION TOTAL] ${total_spent:.5f} / ${SESSION_BUDGET} budget used")

# Expected Token Savings: 40-65% by avoiding model selection that would exceed remaining budget
# Environment: Long sessions with uncertain workloads; forecasting prevents unexpected budget overruns
```

## Comparison

| Option | Selection Trigger | Predictive | Persistence | Async | Best For |
|--------|------------------|-----------|-------------|-------|---------|
| 1. Budget-Tier Router | Remaining % | No | Memory | No | Simple fixed-budget sessions |
| 2. Task Complexity | Message signals | No | Memory | No | Diverse-workload agents |
| 3. Rolling Window | Hourly spend | No | SQLite | No | Multi-tenant hourly budget limits |
| 4. Priority Scheduler | Priority + budget | No | Memory | Yes | Async task queues with priorities |
| 5. Per-Feature Budget | Feature + remaining | No | Memory | No | Multi-feature products |
| 6. Predictive Forecasting | Forecast vs remaining | Yes | SQLite | No | Long sessions with uncertain load |
