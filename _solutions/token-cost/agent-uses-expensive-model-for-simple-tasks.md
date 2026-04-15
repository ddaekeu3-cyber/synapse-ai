---
layout: solution
title: "Agent Uses Expensive Model for Simple Tasks"
category: token-cost
description: "Every request goes to claude-opus-4-6 regardless of complexity, spending 15–75x more than necessary on classification, extraction, and routing tasks."
tags: [token-cost, model-routing, performance, cost-optimization, anthropic-sdk]
---

## Symptom

Your API bill is dominated by input/output token costs but the tasks being performed are simple: classifying an intent, extracting a field from a form, deciding which tool to call, or routing to a specialist. These tasks don't need a frontier model — but the agent sends every request to the most capable (and most expensive) model in the family.

## Root Cause

The default pattern in most agent tutorials uses one model for everything. Frontier models like `claude-opus-4-6` cost 15–75x more per token than `claude-haiku-4-5-20251001` but produce identical results on structured tasks with clear instructions. Without an explicit routing layer, token cost scales linearly with volume even when the workload is dominated by cheap tasks.

## Fix

### Option 1 — Static task-type routing table

```python
import anthropic

client = anthropic.Anthropic()

# Route by task type — never use Opus for tasks Haiku can handle
MODEL_ROUTES = {
    "classify":   "claude-haiku-4-5-20251001",   # intent classification
    "extract":    "claude-haiku-4-5-20251001",   # field extraction
    "route":      "claude-haiku-4-5-20251001",   # tool/agent routing
    "summarise":  "claude-haiku-4-5-20251001",   # short summarization
    "draft":      "claude-sonnet-4-6",           # writing assistance
    "reason":     "claude-sonnet-4-6",           # multi-step reasoning
    "analyse":    "claude-sonnet-4-6",           # data analysis
    "architect":  "claude-opus-4-6",             # system design
    "research":   "claude-opus-4-6",             # deep open-ended research
}

def model_for(task_type: str) -> str:
    return MODEL_ROUTES.get(task_type, "claude-sonnet-4-6")  # safe default

def run(task_type: str, prompt: str, max_tokens: int = 512) -> str:
    model = model_for(task_type)
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    cost_tier = {
        "claude-haiku-4-5-20251001": "cheap",
        "claude-sonnet-4-6":         "medium",
        "claude-opus-4-6":           "expensive",
    }.get(model, "unknown")
    print(f"[model] task={task_type!r} → {model} ({cost_tier})")
    return response.content[0].text

# Low-cost tasks
print(run("classify",  "Is this message a complaint, a question, or a compliment? 'Where is my order?'"))
print(run("extract",   "Extract the invoice number from: 'Your invoice #INV-4521 is attached.'"))
print(run("summarise", "Summarise in one sentence: 'The quick brown fox jumps over the lazy dog repeatedly.'"))

# Higher-cost tasks
print(run("reason",    "A train leaves at 9am at 60mph. Another leaves the same station at 10am at 80mph. When do they meet?"))
print(run("architect", "Design a fault-tolerant event-sourcing system for a banking application."))
```

**Expected Token Savings:** 87–93% cost reduction on classify/extract/route tasks vs. Opus; 50–75% vs. Sonnet on those same tasks.
**Environment:** Any multi-task agent pipeline; define routes once and apply everywhere.

---

### Option 2 — Haiku-based meta-router that selects the model

```python
import anthropic

client = anthropic.Anthropic()

ROUTER_SYSTEM = """Classify the complexity of the user request and return ONLY one of:
haiku   — simple extraction, yes/no, classification, short lookup, single-field format
sonnet  — multi-step reasoning, moderate writing, code with explanation, analysis
opus    — deep research, architecture design, long-form creative, complex strategy

Return exactly one word: haiku, sonnet, or opus."""

MODEL_MAP = {
    "haiku":  "claude-haiku-4-5-20251001",
    "sonnet": "claude-sonnet-4-6",
    "opus":   "claude-opus-4-6",
}

def route(user_request: str) -> str:
    """Use Haiku to classify complexity — costs almost nothing."""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=4,
        system=ROUTER_SYSTEM,
        messages=[{"role": "user", "content": user_request}],
    )
    tier = response.content[0].text.strip().lower()
    return MODEL_MAP.get(tier, "claude-sonnet-4-6")

def smart_ask(request: str, max_tokens: int = 1024) -> str:
    model = route(request)
    print(f"[router] → {model}")
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": request}],
    )
    return response.content[0].text

requests = [
    "What is 2 + 2?",
    "Write a Python function to binary search a sorted list.",
    "Design a distributed rate-limiting system for a global API gateway.",
    "Extract the city from: 'Ship to 123 Main St, Springfield, IL 62701'",
    "Analyse the trade-offs between event sourcing and CQRS for a fintech startup.",
]
for req in requests:
    print(f"\nQ: {req!r}")
    print(f"A: {smart_ask(req)[:120]}")
```

**Expected Token Savings:** Router call costs ~20 tokens (Haiku); saves thousands of tokens on every request that gets downgraded from Opus/Sonnet to Haiku.
**Environment:** General-purpose agents where task complexity varies widely; the router adds one cheap round-trip.

---

### Option 3 — Prompt-length heuristic: cheap model for short prompts

```python
import anthropic

client = anthropic.Anthropic()

# Thresholds tuned empirically: short prompts are usually simple
SHORT_PROMPT_TOKENS = 200    # use Haiku
MEDIUM_PROMPT_TOKENS = 1000  # use Sonnet

def estimate_tokens(text: str) -> int:
    return len(text) // 4  # rough: 4 chars ≈ 1 token

def select_model(prompt: str, force: str | None = None) -> str:
    if force:
        return force
    n = estimate_tokens(prompt)
    if n < SHORT_PROMPT_TOKENS:
        return "claude-haiku-4-5-20251001"
    if n < MEDIUM_PROMPT_TOKENS:
        return "claude-sonnet-4-6"
    return "claude-opus-4-6"

def ask(prompt: str, max_tokens: int = 512, force_model: str | None = None) -> str:
    model = select_model(prompt, force_model)
    tokens_est = estimate_tokens(prompt)
    print(f"[model] prompt≈{tokens_est}tok → {model}")
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text

# Short → Haiku
print(ask("What is the capital of France?"))

# Medium → Sonnet
print(ask("Explain the differences between REST and GraphQL APIs, with examples."))

# Force Opus when you know the task is hard
long_context = "Here is a 5000-token legal document. " + "clause " * 1200
print(ask(long_context + "\nSummarise the key obligations.", force_model="claude-opus-4-6"))
```

**Expected Token Savings:** Zero LLM overhead for routing; prompt length correlates well with task complexity for most workloads.
**Environment:** Simple pipelines without a separate routing call; works as a quick-win cost reduction.

---

### Option 4 — Per-pipeline model configuration

```python
import anthropic
from dataclasses import dataclass

client = anthropic.Anthropic()

@dataclass
class Pipeline:
    name:       str
    model:      str
    max_tokens: int
    system:     str = ""

# Define each pipeline with the minimum model it needs
PIPELINES = {
    "intent_classifier": Pipeline(
        name="intent_classifier",
        model="claude-haiku-4-5-20251001",
        max_tokens=32,
        system="Return ONLY one word: question, complaint, compliment, request, or other.",
    ),
    "entity_extractor": Pipeline(
        name="entity_extractor",
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        system="Extract entities as JSON: {people: [], places: [], dates: [], amounts: []}. Return ONLY JSON.",
    ),
    "email_drafter": Pipeline(
        name="email_drafter",
        model="claude-sonnet-4-6",
        max_tokens=512,
        system="Draft a professional email. Be concise and warm.",
    ),
    "strategy_advisor": Pipeline(
        name="strategy_advisor",
        model="claude-opus-4-6",
        max_tokens=2048,
        system="You are a senior strategy consultant. Provide thorough analysis.",
    ),
}

def run_pipeline(pipeline_name: str, user_input: str) -> str:
    p = PIPELINES[pipeline_name]
    kwargs = dict(model=p.model, max_tokens=p.max_tokens, messages=[{"role": "user", "content": user_input}])
    if p.system:
        kwargs["system"] = p.system
    response = client.messages.create(**kwargs)
    print(f"[pipeline:{p.name}] model={p.model}")
    return response.content[0].text

# Intent classification — Haiku
intent = run_pipeline("intent_classifier", "Where is my delivery?")
print(f"Intent: {intent}")

# Entity extraction — Haiku
entities = run_pipeline("entity_extractor", "John Smith flew from New York to London on March 5th for $850.")
print(f"Entities: {entities}")

# Email drafting — Sonnet
email = run_pipeline("email_drafter", "Write an apology email for a delayed shipment.")
print(f"Email: {email[:200]}")
```

**Expected Token Savings:** Each pipeline uses the minimum viable model; savings are 87% on Haiku pipelines vs. routing everything to Opus.
**Environment:** Multi-stage agents (classify → extract → draft → review); configure model per stage once and never revisit.

---

### Option 5 — Output-complexity routing: cheap model with Sonnet fallback

```python
import anthropic

client = anthropic.Anthropic()

def ask_with_fallback(prompt: str, max_tokens: int = 512) -> str:
    """
    Try Haiku first. If the response hits the token limit or contains
    uncertainty signals, escalate to Sonnet.
    """
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    text = response.content[0].text

    # Escalation signals
    hit_limit  = response.stop_reason == "max_tokens"
    uncertain  = any(phrase in text.lower() for phrase in
                     ["i'm not sure", "i don't know", "unclear", "complex", "beyond my"])

    if hit_limit or uncertain:
        print(f"[fallback] escalating to Sonnet (hit_limit={hit_limit}, uncertain={uncertain})")
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=max_tokens * 2,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text

    print("[model] Haiku sufficient")
    return text

queries = [
    "What is HTTP?",
    "Explain the CAP theorem and its implications for distributed database design.",
    "What colour is the sky?",
    "Compare and contrast microservices, SOA, and monolithic architectures across 10 dimensions.",
]
for q in queries:
    print(f"\nQ: {q!r[:80]}")
    print(f"A: {ask_with_fallback(q)[:150]}")
```

**Expected Token Savings:** Most simple queries are answered by Haiku at 1/15 the cost of Sonnet; escalation adds one extra call only when needed.
**Environment:** Unknown-complexity workloads where query difficulty isn't predictable upfront.

---

### Option 6 — Cost tracker that alerts when per-task cost exceeds budget

```python
import anthropic
from decimal import Decimal
from dataclasses import dataclass, field

client = anthropic.Anthropic()

# Pricing per million tokens (as of 2025)
PRICING = {
    "claude-haiku-4-5-20251001": {"input": Decimal("0.80"),  "output": Decimal("4.00")},
    "claude-sonnet-4-6":         {"input": Decimal("3.00"),  "output": Decimal("15.00")},
    "claude-opus-4-6":           {"input": Decimal("15.00"), "output": Decimal("75.00")},
}

@dataclass
class CostTracker:
    budget_per_task_usd: Decimal = Decimal("0.005")  # $0.005 per task
    total_spent:         Decimal = field(default_factory=Decimal)
    over_budget_count:   int     = 0

    def record(self, model: str, usage) -> Decimal:
        prices  = PRICING[model]
        m       = Decimal("1_000_000")
        cost    = (Decimal(usage.input_tokens) / m * prices["input"]
                   + Decimal(usage.output_tokens) / m * prices["output"])
        self.total_spent += cost
        if cost > self.budget_per_task_usd:
            self.over_budget_count += 1
            print(f"[cost] OVER BUDGET: ${cost:.6f} > ${self.budget_per_task_usd} "
                  f"on model={model}")
        else:
            print(f"[cost] ${cost:.6f} ({model})")
        return cost

tracker = CostTracker(budget_per_task_usd=Decimal("0.002"))

def ask_tracked(prompt: str, model: str = "claude-haiku-4-5-20251001", max_tokens: int = 256) -> str:
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    tracker.record(model, response.usage)
    return response.content[0].text

# These should all be within budget on Haiku
ask_tracked("What is 10 + 10?")
ask_tracked("Name the capital of Japan.")
ask_tracked("Is 'hello world' a valid Python string? Answer yes or no.")

# This will likely exceed budget if sent to Opus
ask_tracked("Explain deep learning.", model="claude-opus-4-6", max_tokens=512)

print(f"\nTotal spent: ${tracker.total_spent:.4f}")
print(f"Over-budget calls: {tracker.over_budget_count}")
```

**Expected Token Savings:** Budget alerts surface which tasks are being over-served by expensive models; provides data to justify a routing change.
**Environment:** Production agents — run for a week, review over-budget alerts, then add explicit model routing for flagged task types.

---

## Comparison

| Option | Routing Mechanism | Overhead | Adaptability | Best For |
|---|---|---|---|---|
| 1. Static table | Hardcoded dict | None | Low | Well-defined task types |
| 2. Haiku meta-router | 1 LLM call | ~20 tokens | High | Variable-complexity workloads |
| 3. Prompt length | Token estimate | None | Medium | Quick-win, no extra calls |
| 4. Per-pipeline config | Dataclass config | None | Low | Multi-stage pipelines |
| 5. Output fallback | Post-hoc escalation | +1 call on escalate | High | Unknown-complexity queries |
| 6. Cost tracker | Budget alerting | None | N/A | Audit and justify routing changes |
