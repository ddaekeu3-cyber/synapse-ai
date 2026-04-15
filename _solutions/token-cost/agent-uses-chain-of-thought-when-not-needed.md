---
layout: solution
title: "Agent Uses Chain-of-Thought When Not Needed"
category: token-cost
description: "Agent applies verbose step-by-step reasoning to simple factual lookups, classifications, and single-turn answers, generating hundreds of unnecessary tokens before reaching the answer."
tags: [token-cost, chain-of-thought, reasoning, efficiency, prompt-engineering]
---

## Symptom

A user asks "Is this email address valid?" and the agent generates 300 tokens of reasoning ("Let me think step by step. First, I'll identify the components of an email address. The format should be local-part@domain...") before outputting a single-word answer: "Yes." A yes/no classification that should cost 50 tokens costs 350. Multiply by 100,000 daily requests and the wasted spend is significant.

## Root Cause

Chain-of-thought (CoT) prompting ("think step by step", "let's reason through this") improves accuracy on complex multi-step tasks: math, code generation, logical deduction. For simple tasks — classification, extraction, factual lookup, format conversion — CoT adds tokens without improving accuracy. Developers apply CoT globally because it's a safe default, but it is unnecessarily expensive for tasks where the answer is direct.

## Fix

### Option 1 — Task router: apply CoT only to complex queries

```python
import json
import anthropic

client = anthropic.Anthropic()

COMPLEXITY_SYSTEM = """Classify the complexity of this task.
Simple: factual lookup, yes/no, extraction, single-word answer, format conversion.
Complex: multi-step reasoning, code generation, analysis, comparison of multiple options.
Return JSON: {"complexity": "simple" | "complex", "reason": "..."}"""

def classify_complexity(question: str) -> str:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=48,
        system=COMPLEXITY_SYSTEM,
        messages=[{"role": "user", "content": question}],
    )
    raw = response.content[0].text.strip().lstrip("```json").rstrip("```").strip()
    try:
        return json.loads(raw).get("complexity", "simple")
    except json.JSONDecodeError:
        return "simple"   # default to simple (cheaper)

SIMPLE_SYSTEM  = "Answer concisely. One sentence maximum."
COMPLEX_SYSTEM = "Think step by step. Show your reasoning, then give a clear answer."

def ask(question: str) -> str:
    complexity = classify_complexity(question)
    system     = COMPLEX_SYSTEM if complexity == "complex" else SIMPLE_SYSTEM
    print(f"  [router] complexity={complexity!r}")

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512 if complexity == "complex" else 64,
        system=system,
        messages=[{"role": "user", "content": question}],
    )
    tokens = response.usage.input_tokens + response.usage.output_tokens
    print(f"  [tokens] {tokens} total")
    return response.content[0].text.strip()

questions = [
    "Is 'user@example.com' a valid email format?",          # simple
    "What is the capital of Japan?",                        # simple
    "Design a rate limiting system for 1M req/day.",        # complex
    "Should I use PostgreSQL or MongoDB for time-series?",  # complex
    "Convert '2024-01-15' to Unix timestamp.",              # simple
]
for q in questions:
    print(f"Q: {q}")
    print(f"A: {ask(q)[:200]}\n")
```

**Expected Token Savings:** Simple questions use 50-100 tokens vs 300-500 with CoT; for a 70/30 simple/complex split, routing saves ~60% of output tokens.
**Environment:** General-purpose agents handling mixed workloads; routing prevents CoT overhead on the majority of simple queries.

---

### Option 2 — Explicit answer-first instruction for direct questions

```python
import anthropic

client = anthropic.Anthropic()

# Direct answer instruction — no CoT preamble
DIRECT_SYSTEM = """You give direct, concise answers.
Rules:
- Answer the question first. No preamble ("Sure!", "Of course!").
- Do not narrate your reasoning process.
- For yes/no questions: start with Yes or No.
- For factual questions: state the fact immediately.
- Add brief context only if it changes the answer."""

# Comparison: default (may CoT) vs direct
DEFAULT_SYSTEM = ""   # no system prompt — model default

questions = [
    "Is Python dynamically typed?",
    "What does HTTP stand for?",
    "Is 127.0.0.1 a valid IP address?",
    "Does asyncio.gather run tasks in parallel?",
]

import time

print("Default (no system prompt):")
for q in questions:
    t0 = time.perf_counter()
    r  = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        messages=[{"role": "user", "content": q}],
    )
    tokens  = r.usage.output_tokens
    elapsed = time.perf_counter() - t0
    print(f"  [{tokens:3d} tok, {elapsed:.1f}s] Q: {q[:40]}")
    print(f"                        A: {r.content[0].text[:80]}\n")

print("With direct-answer system prompt:")
for q in questions:
    t0 = time.perf_counter()
    r  = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        system=DIRECT_SYSTEM,
        messages=[{"role": "user", "content": q}],
    )
    tokens  = r.usage.output_tokens
    elapsed = time.perf_counter() - t0
    print(f"  [{tokens:3d} tok, {elapsed:.1f}s] Q: {q[:40]}")
    print(f"                        A: {r.content[0].text[:80]}\n")
```

**Expected Token Savings:** Direct-answer instruction reduces output tokens by 50-80% for factual questions; combined with lower `max_tokens`, also reduces latency proportionally.
**Environment:** FAQ bots, classification agents, and any agent where the majority of queries have short, direct answers.

---

### Option 3 — `max_tokens` cap by task type to enforce conciseness

```python
import anthropic

client = anthropic.Anthropic()

# Task type → max_tokens budget
MAX_TOKENS_BY_TASK = {
    "classification":    16,    # POSITIVE/NEGATIVE/NEUTRAL
    "yes_no":            8,     # Yes / No
    "extraction":        64,    # extracted field values
    "one_liner":         48,    # single sentence
    "short_answer":      128,   # factual answer with brief context
    "explanation":       256,   # multi-sentence explanation
    "code_generation":   512,   # code snippet
    "analysis":         1024,   # in-depth analysis
}

TASK_SYSTEMS = {
    "classification":   "Reply with exactly one word: POSITIVE, NEGATIVE, or NEUTRAL.",
    "yes_no":           "Reply with exactly one word: Yes or No.",
    "extraction":       "Extract the requested information. Reply with the value only, no explanation.",
    "one_liner":        "Answer in exactly one sentence.",
    "short_answer":     "Answer concisely in 1-3 sentences.",
    "explanation":      "Explain clearly and concisely.",
    "code_generation":  "Provide clean, working code. Include a brief comment if needed.",
    "analysis":         "Think step by step. Provide a thorough analysis.",
}

def ask(question: str, task_type: str) -> str:
    max_tok = MAX_TOKENS_BY_TASK[task_type]
    system  = TASK_SYSTEMS[task_type]

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=max_tok,
        system=system,
        messages=[{"role": "user", "content": question}],
    )
    tokens = response.usage.output_tokens
    print(f"  [{task_type}, {tokens}/{max_tok} tok]")
    return response.content[0].text.strip()

# Test each task type
tasks = [
    ("Is 'Great product!' positive?",                       "classification"),
    ("Does Python have a GIL?",                             "yes_no"),
    ("Extract the email from: Contact alice@example.com",   "extraction"),
    ("What is a decorator?",                                "one_liner"),
    ("What is the difference between list and tuple?",      "short_answer"),
    ("Explain what asyncio is.",                            "explanation"),
    ("Write a Python function to reverse a string.",        "code_generation"),
    ("Should I use REST or GraphQL for a mobile app?",      "analysis"),
]
for q, task in tasks:
    print(f"Q [{task}]: {q}")
    print(f"A: {ask(q, task)[:150]}\n")
```

**Expected Token Savings:** Hard `max_tokens` caps prevent the model from over-generating; a classification call capped at 16 tokens costs 95% less than one with `max_tokens=1024`.
**Environment:** All structured task agents; matching `max_tokens` to the minimum necessary for each task type is the single most impactful token-reduction change.

---

### Option 4 — Prompt prefix to disable reasoning preamble

```python
import anthropic

client = anthropic.Anthropic()

# Prefill assistant turn to skip preamble and go straight to answer
def ask_with_prefill(question: str, prefill: str = "") -> str:
    """Use assistant prefill to skip CoT preamble and go straight to the answer."""
    messages = [{"role": "user", "content": question}]
    if prefill:
        messages.append({"role": "assistant", "content": prefill})

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=messages,
    )
    # Concatenate prefill + generated continuation
    return (prefill + response.content[0].text).strip()

# Without prefill — model may start with "Let me think..."
print("Without prefill:")
for q in [
    "Is 192.168.1.1 a private IP address?",
    "What language is Django written in?",
]:
    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{"role": "user", "content": q}],
    )
    print(f"  Q: {q}")
    print(f"  A: {r.content[0].text[:120]}\n")

# With prefill — forces answer-first
print("With prefill (forces Yes/No start):")
for q, prefill in [
    ("Is 192.168.1.1 a private IP address?", "Yes"),
    ("What language is Django written in?",   "Python"),
]:
    reply = ask_with_prefill(q, prefill)
    print(f"  Q: {q}")
    print(f"  A: {reply[:120]}\n")

# For extraction tasks, prefill with JSON open brace
print("JSON extraction with prefill:")
q = 'Extract name and age from: "Alice is 30 years old"'
reply = ask_with_prefill(q, '{"name": "')
print(f"  Q: {q}")
print(f"  A: {reply[:120]}")
```

**Expected Token Savings:** Assistant prefill skips all preamble tokens; for a 50-token preamble on 10,000 daily calls, prefill saves 500,000 tokens/day at no quality cost for simple answers.
**Environment:** Classification, extraction, and yes/no agents; prefill is most effective when the answer format is predictable.

---

### Option 5 — Two-model strategy: Haiku for simple, Sonnet for complex

```python
import json
import anthropic

client = anthropic.Anthropic()

ROUTER_SYSTEM = """Determine whether this question requires deep reasoning or just a direct answer.
Simple: yes/no, factual lookup, extraction, conversion, single-step calculation.
Complex: design decisions, multi-step code, comparing tradeoffs, open-ended analysis.
Return JSON: {"route": "simple" | "complex"}"""

def route_question(question: str) -> str:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",   # cheapest model for routing
        max_tokens=16,
        system=ROUTER_SYSTEM,
        messages=[{"role": "user", "content": question}],
    )
    raw = response.content[0].text.strip().lstrip("```json").rstrip("```").strip()
    try:
        return json.loads(raw).get("route", "simple")
    except json.JSONDecodeError:
        return "simple"

def ask(question: str) -> tuple[str, str]:
    route = route_question(question)

    if route == "simple":
        model      = "claude-haiku-4-5-20251001"
        max_tokens = 64
        system     = "Answer directly in 1 sentence."
    else:
        model      = "claude-haiku-4-5-20251001"   # use haiku for both in example; in prod: claude-sonnet-4-6
        max_tokens = 512
        system     = "Think through this carefully. Show reasoning then give a recommendation."

    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": question}],
    )
    tokens = response.usage.input_tokens + response.usage.output_tokens
    print(f"  [{route}, {model.split('-')[1]}, {tokens} tok]")
    return response.content[0].text.strip(), route

questions = [
    "What port does HTTPS use?",
    "Is asyncio thread-safe?",
    "Design a caching strategy for a high-traffic API with read-heavy workloads.",
    "What are the trade-offs between monolith and microservices for a 5-person startup?",
    "Convert 100 Fahrenheit to Celsius.",
]
for q in questions:
    reply, route = ask(q)
    print(f"Q: {q}")
    print(f"A: {reply[:200]}\n")
```

**Expected Token Savings:** Simple questions routed to Haiku cost ~5× less than Sonnet; the router itself costs ~10 tokens (Haiku); for 80% simple queries, routing saves ~(0.8 × Sonnet_cost) - (router_cost) per call.
**Environment:** Mixed-complexity agents handling both trivial and deep questions; model routing is the highest-impact token-cost reduction for heterogeneous workloads.

---

### Option 6 — Structured output to eliminate prose wrapping

```python
import json
import anthropic

client = anthropic.Anthropic()

# BEFORE: prose answer — model generates reasoning + answer wrapped in text
PROSE_SYSTEM = "Answer the question helpfully."

# AFTER: structured output — answer only, no prose
STRUCTURED_SYSTEM = """Return your answer as JSON only. No prose, no explanation.
Use these schemas:
- Yes/No: {"answer": "yes" | "no"}
- Factual: {"value": "<answer>", "unit": "<unit or null>"}
- Classification: {"label": "<class>", "confidence": 0.0-1.0}
No markdown, no code fences — raw JSON only."""

def ask_structured(question: str) -> dict:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=32,
        system=STRUCTURED_SYSTEM,
        messages=[{"role": "user", "content": question}],
    )
    raw = response.content[0].text.strip()
    tokens = response.usage.output_tokens
    print(f"  [{tokens} output tokens] {raw}")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"raw": raw}

def ask_prose(question: str) -> str:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=PROSE_SYSTEM,
        messages=[{"role": "user", "content": question}],
    )
    tokens = response.usage.output_tokens
    print(f"  [{tokens} output tokens]")
    return response.content[0].text.strip()

questions = [
    ("Is TCP connection-oriented?",        "yes_no"),
    ("What is the boiling point of water?","factual"),
    ("Classify: 'I love this product!'",   "classification"),
]

print("Prose answers:")
for q, _ in questions:
    ask_prose(q)

print("\nStructured JSON answers:")
for q, _ in questions:
    result = ask_structured(q)
    print(f"  Parsed: {result}")
```

**Expected Token Savings:** Structured JSON answers use 5-30 tokens vs 50-200 for prose equivalents; 80-90% output token reduction for simple classification and extraction tasks.
**Environment:** Backend pipelines where agent outputs are parsed programmatically; structured output eliminates all prose overhead and eliminates the need to parse natural language responses.

---

## Comparison

| Option | Implementation Effort | Works for Mixed Queries | Token Savings | Best For |
|---|---|---|---|---|
| 1. Complexity router | Medium | Yes | 40-60% | General-purpose agents |
| 2. Direct-answer instruction | None | Partial | 30-50% | FAQ and support bots |
| 3. `max_tokens` by task type | Low | Yes | 50-80% | Structured task pipelines |
| 4. Assistant prefill | Low | No | 20-40% | Predictable-format answers |
| 5. Two-model routing | Medium | Yes | 60-80% | High-volume mixed workloads |
| 6. Structured JSON output | Low | No | 70-90% | Machine-processed answers |
