---
layout: solution
title: "Agent Ignores Negative Constraints in the Prompt"
category: prompt-engineering
description: "Instructions like 'do not mention competitors' or 'never output code' are ignored because negations are less salient than affirmative instructions."
tags: [prompt-engineering, constraints, reliability, output-format, system-prompt]
---

## Symptom

The system prompt says "do not recommend competitor products" — but the agent does anyway when a user mentions them. It says "never output raw SQL" — but the model includes SQL in its response. Negative constraints ("do not", "never", "avoid") are followed inconsistently, especially when the user's message creates a strong pull toward the forbidden output.

## Root Cause

Language models process instructions probabilistically. Negations require the model to first activate the concept and then suppress it — this is harder than simply activating a concept. Strong contextual pull (a user asking directly about a competitor) can override a weakly-stated negative constraint. Constraints buried in long system prompts are especially vulnerable because attention dilutes over distance.

## Fix

### Option 1 — Reframe negatives as affirmatives

```python
import anthropic

client = anthropic.Anthropic()

# WEAK: negative framing
WEAK_SYSTEM = """You are a customer support agent for AcmeSoft.
Do not mention competitor products.
Don't recommend users try other software.
Never say anything negative about our product."""

# STRONG: affirmative framing
STRONG_SYSTEM = """You are a customer support agent for AcmeSoft.

When users ask about other software:
- Focus entirely on AcmeSoft's capabilities
- Explain how AcmeSoft solves their specific need
- If AcmeSoft cannot do something, say "That's on our roadmap" or "Let me check with our team"

Respond only about AcmeSoft features, pricing, and support."""

def ask(system: str, question: str) -> str:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=system,
        messages=[{"role": "user", "content": question}],
    )
    return response.content[0].text

question = "Is AcmeSoft better than Salesforce? Should I just use Salesforce instead?"
print("=== WEAK (negative framing) ===")
print(ask(WEAK_SYSTEM, question))
print("\n=== STRONG (affirmative framing) ===")
print(ask(STRONG_SYSTEM, question))
```

**Expected Token Savings:** Affirmative constraints are followed more consistently, reducing correction turns and re-runs.
**Environment:** Any agent with behavioural guardrails; reframing is the highest-leverage first step.

---

### Option 2 — Post-generation constraint checker with auto-retry

```python
import re
import anthropic

client = anthropic.Anthropic()

# Define constraints as (pattern, violation_message) pairs
CONSTRAINTS = [
    (re.compile(r"\b(Salesforce|HubSpot|Zendesk|Intercom)\b", re.IGNORECASE),
     "Response mentions a competitor brand"),
    (re.compile(r"```sql|SELECT\s+\w+\s+FROM", re.IGNORECASE),
     "Response contains raw SQL"),
    (re.compile(r"\b(sorry|apologise|apologize)\b", re.IGNORECASE),
     "Response contains apology language (use solution-focused language instead)"),
]

SYSTEM = """You are AcmeSoft support. Focus only on AcmeSoft. Speak with confidence."""

def check_constraints(text: str) -> list[str]:
    return [msg for pattern, msg in CONSTRAINTS if pattern.search(text)]

def ask_constrained(user_message: str, max_attempts: int = 3) -> str:
    messages = [{"role": "user", "content": user_message}]

    for attempt in range(max_attempts):
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            system=SYSTEM,
            messages=messages,
        )
        reply      = response.content[0].text
        violations = check_constraints(reply)

        if not violations:
            return reply

        print(f"[constraint] attempt {attempt + 1} violations: {violations}")
        # Append the violation feedback and retry
        messages.append({"role": "assistant", "content": reply})
        messages.append({
            "role": "user",
            "content": (
                f"Your response violated these rules: {'; '.join(violations)}. "
                "Please rewrite your response without violating these rules."
            ),
        })

    # Return last attempt even if it still violates (better than nothing)
    return reply

test_messages = [
    "How does AcmeSoft compare to Salesforce?",
    "Can you write a SQL query to export my data?",
    "I'm sorry but I'm not happy with the product.",
]
for msg in test_messages:
    print(f"\nUser: {msg!r}")
    print(f"Agent: {ask_constrained(msg)[:200]}")
```

**Expected Token Savings:** Catches violations before they reach the user; a single retry is cheaper than a customer escalation or a downstream parsing failure.
**Environment:** Production agents with hard compliance requirements (legal, brand, security).

---

### Option 3 — Constraint priority ladder: critical rules at the top, short, emphatic

```python
import anthropic

client = anthropic.Anthropic()

# Rules ordered: most critical first, stated positively, short sentences
SYSTEM = """ABSOLUTE RULES (always apply, no exceptions):
1. Output JSON only. No prose. No markdown.
2. Every response must include the field "confidence" (0.0–1.0).
3. If data is missing, set the field to null — never omit it.
4. The "category" field must be one of: electronics, clothing, food, home, other.

You extract product metadata from text. Return a JSON object with:
name, price_usd, category, in_stock, confidence."""

def extract(text: str) -> str:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=SYSTEM,
        messages=[{"role": "user", "content": text}],
    )
    return response.content[0].text

import json

samples = [
    "iPhone 15 Pro, $999, in stock.",
    "Blue jeans, unavailable.",
    "Organic oats, $5.99, available.",
    "Garden hose.",  # missing category match, price missing
]
for s in samples:
    raw = extract(s)
    print(f"Input:  {s!r}")
    try:
        data = json.loads(raw)
        print(f"Output: {data}")
    except json.JSONDecodeError:
        print(f"RAW (parse failed): {raw[:100]}")
    print()
```

**Expected Token Savings:** Top-of-prompt critical rules are attended to most strongly; eliminates category of output-format errors entirely.
**Environment:** Structured extraction agents; format constraints are the most common victim of constraint drift.

---

### Option 4 — Constitutional checker: second model enforces rules

```python
import json
import anthropic

client = anthropic.Anthropic()

AGENT_SYSTEM = "You are a helpful coding assistant."

CONSTITUTION = """You are a compliance checker. Review the response and check these rules:
1. No external library imports (only stdlib allowed)
2. No network calls (no requests, httpx, urllib)
3. All functions must have type annotations
4. No global mutable state (no global variables that change)

Return JSON: {"pass": true/false, "violations": ["..."]}
If pass is true, violations must be empty."""

def generate_code(request: str) -> str:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=AGENT_SYSTEM,
        messages=[{"role": "user", "content": request}],
    )
    return response.content[0].text

def check_compliance(code: str) -> dict:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=CONSTITUTION,
        messages=[{"role": "user", "content": f"Review this code:\n\n{code}"}],
    )
    try:
        return json.loads(response.content[0].text.strip().lstrip("```json").rstrip("```").strip())
    except json.JSONDecodeError:
        return {"pass": False, "violations": ["compliance check parse error"]}

def safe_generate(request: str, max_attempts: int = 3) -> str:
    for attempt in range(max_attempts):
        code       = generate_code(request)
        compliance = check_compliance(code)

        if compliance.get("pass"):
            print(f"[compliance] passed on attempt {attempt + 1}")
            return code

        violations = compliance.get("violations", [])
        print(f"[compliance] attempt {attempt + 1} failed: {violations}")
        request = (
            f"{request}\n\n"
            f"Previous attempt violated these rules: {'; '.join(violations)}. "
            "Fix all violations."
        )

    return code  # return best attempt

print(safe_generate("Write a function to check if a string is a palindrome."))
```

**Expected Token Savings:** Constitutional checker is a cheap Haiku call; prevents compliance violations that would require human review or a full pipeline restart.
**Environment:** Code generation agents with strict output requirements (stdlib-only, no network, specific style rules).

---

### Option 5 — Constraint injection via tool_choice: make forbidden actions impossible

```python
import json
import anthropic

client = anthropic.Anthropic()

# Instead of telling the model "don't recommend competitors" in prose,
# make competitor recommendations structurally impossible by controlling tool schema

TOOLS = [
    {
        "name": "recommend_product",
        "description": "Recommend an AcmeSoft product to the user.",
        "input_schema": {
            "type": "object",
            "required": ["product_name", "reason", "price_usd"],
            "properties": {
                "product_name": {
                    "type": "string",
                    "enum": ["AcmeSoft Basic", "AcmeSoft Pro", "AcmeSoft Enterprise"],
                    "description": "Only AcmeSoft products may be recommended.",
                },
                "reason":    {"type": "string"},
                "price_usd": {"type": "number"},
            },
        },
    },
    {
        "name": "escalate_to_human",
        "description": "Escalate to a human agent when you cannot help.",
        "input_schema": {
            "type": "object",
            "required": ["reason"],
            "properties": {"reason": {"type": "string"}},
        },
    },
]

PRICES = {"AcmeSoft Basic": 29.0, "AcmeSoft Pro": 79.0, "AcmeSoft Enterprise": 299.0}

def handle_tool(name: str, inputs: dict) -> str:
    if name == "recommend_product":
        return json.dumps({
            "recommended": inputs["product_name"],
            "price":       PRICES.get(inputs["product_name"], 0),
            "reason":      inputs["reason"],
        })
    return json.dumps({"escalated": True, "reason": inputs.get("reason")})

def run_agent(query: str) -> str:
    messages = [{"role": "user", "content": query}]
    for _ in range(4):
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            system="You are AcmeSoft support. Help users find the right AcmeSoft product.",
            tools=TOOLS,
            messages=messages,
        )
        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if b.type == "text"), "Done.")
        messages.append({"role": "assistant", "content": response.content})
        results = [
            {"type": "tool_result", "tool_use_id": b.id, "content": handle_tool(b.name, b.input)}
            for b in response.content if b.type == "tool_use"
        ]
        messages.append({"role": "user", "content": results})
    return "max steps reached"

# Competitor mention — model can only recommend AcmeSoft products via the tool schema
print(run_agent("Should I use AcmeSoft or Salesforce? I need CRM for 50 users."))
```

**Expected Token Savings:** Structural constraint via tool schema is 100% reliable — the model literally cannot output a competitor name via the tool; no retry needed.
**Environment:** Sales agents, product recommendation bots; the most reliable approach when you can model the output as tool calls.

---

### Option 6 — Output filter layer: redact/replace constraint violations post-generation

```python
import re
import anthropic

client = anthropic.Anthropic()

# Redaction rules: (pattern, replacement)
REDACTION_RULES = [
    # Competitor names → neutral
    (re.compile(r"\b(Salesforce|HubSpot|Zendesk)\b", re.IGNORECASE), "[competitor]"),
    # PII patterns
    (re.compile(r"\b\d{3}[-.\s]?\d{3}[-.\s]?\d{4}\b"), "[phone-redacted]"),
    (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b"), "[email-redacted]"),
    # Internal endpoints
    (re.compile(r"https?://internal\.[a-z]+\.[a-z]+/\S*"), "[internal-url-redacted]"),
]

def apply_filters(text: str) -> tuple[str, list[str]]:
    applied = []
    for pattern, replacement in REDACTION_RULES:
        matches = pattern.findall(text)
        if matches:
            applied.append(f"redacted {len(matches)} match(es) of {pattern.pattern!r}")
            text = pattern.sub(replacement, text)
    return text, applied

SYSTEM = "You are a helpful assistant. Answer the user's question."

def filtered_ask(user_message: str) -> str:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=SYSTEM,
        messages=[{"role": "user", "content": user_message}],
    )
    raw_reply    = response.content[0].text
    clean_reply, actions = apply_filters(raw_reply)

    if actions:
        print(f"[filter] {'; '.join(actions)}")

    return clean_reply

test_inputs = [
    "What CRM is better, Salesforce or HubSpot?",
    "My email is john.doe@example.com, can you help me?",
    "The internal API is at https://internal.api.acmesoft.com/v1/users",
]
for msg in test_inputs:
    print(f"\nUser: {msg!r}")
    print(f"Agent: {filtered_ask(msg)[:200]}")
```

**Expected Token Savings:** Filter layer adds zero tokens; it's a post-processing step that guarantees constraint compliance regardless of model behaviour.
**Environment:** Last line of defence for any constraint that must be enforced 100% of the time; combine with other options for defence in depth.

---

## Comparison

| Option | Mechanism | Reliability | Latency Added | Best For |
|---|---|---|---|---|
| 1. Affirmative framing | Prompt rewrite | High | None | First step for all negative constraints |
| 2. Post-gen checker | Regex + retry | Medium-High | +1 call on violation | Compliance validation with audit trail |
| 3. Priority ladder | Structural prompt | High | None | Format and output-type constraints |
| 4. Constitutional AI | Second model review | High | +1 call always | Code generation with hard rules |
| 5. Tool schema | Structural impossibility | 100% | None | Brand/product constraints (preferred) |
| 6. Output filter | Post-processing redaction | 100% | Negligible | PII, competitor names, URL redaction |
