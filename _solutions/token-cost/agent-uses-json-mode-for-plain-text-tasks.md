---
layout: solution
title: "Agent Uses JSON Mode for Plain-Text Tasks"
category: token-cost
description: "Agent requests JSON output for tasks that only need a plain string, tripling the token count with braces, keys, and quotes around a single-sentence answer."
tags: [token-cost, json, output-format, efficiency, production]
---

## Symptom

An agent asks Claude to classify a support ticket as `urgent`, `normal`, or `low`, but requests JSON output: `{"priority": "urgent", "confidence": 0.95, "reasoning": "..."}`. The JSON wrapper, key names, and extra fields consume 3–10× the tokens of the raw answer `urgent`. At scale across thousands of classifications per day this overhead becomes a significant cost line — for a task that only needed one word.

## Root Cause

Developers default to JSON because it is easy to parse programmatically, but they apply it universally without checking whether the task actually needs structure. A single-field response, a yes/no question, a category label, or a short summary gains nothing from JSON wrapping — the format adds tokens without adding information. The fix is to match output format to the actual complexity of the response.

## Fix

### Option 1 — Request raw text for single-value outputs

```python
import anthropic

client = anthropic.Anthropic()

# BAD: JSON wrapper for a single-word answer
def classify_json(ticket: str) -> dict:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{
            "role": "user",
            "content": (
                f"Classify this support ticket as one of: urgent, normal, low. "
                f"Return JSON: {{\"priority\": \"<label>\"}}.\n\nTicket: {ticket}"
            ),
        }],
    )
    import json
    return json.loads(response.content[0].text)

# GOOD: single word, no JSON overhead
def classify_text(ticket: str) -> str:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=8,   # "urgent" / "normal" / "low" — 1–2 tokens
        messages=[{
            "role": "user",
            "content": (
                f"Classify this support ticket. Reply with exactly one word: "
                f"urgent, normal, or low.\n\nTicket: {ticket}"
            ),
        }],
    )
    label = response.content[0].text.strip().lower()
    assert label in ("urgent", "normal", "low"), f"Unexpected label: {label}"
    return label

ticket = "My production database is down and I'm losing $10k/minute."

# Measure token cost difference
r_json = client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=128,
    messages=[{"role": "user", "content": f'Return JSON {{"priority":"<label>"}} for: {ticket}'}],
)
r_text = client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=8,
    messages=[{"role": "user", "content": f"One word (urgent/normal/low): {ticket}"}],
)

print(f"JSON approach:  {r_json.usage.output_tokens} output tokens")
print(f"Text approach:  {r_text.usage.output_tokens} output tokens")
print(f"Savings:        {r_json.usage.output_tokens - r_text.usage.output_tokens} tokens/call")
print(f"Result: {r_text.content[0].text.strip()}")
```

**Expected Token Savings:** Single-label classification uses 1–3 output tokens vs 20–50 for JSON; at 10,000 classifications/day, savings are 170,000–470,000 output tokens daily.
**Environment:** Classification, sentiment analysis, routing, boolean yes/no tasks — any single-value extraction.

---

### Option 2 — Use JSON only when there are two or more fields

```python
import json
import anthropic

client = anthropic.Anthropic()

def route_request(user_message: str) -> str:
    """Single output — plain text is correct here."""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=4,
        messages=[{
            "role": "user",
            "content": (
                "Route this message to: billing, technical, or sales. "
                f"Reply with one word only.\n\nMessage: {user_message}"
            ),
        }],
    )
    return response.content[0].text.strip().lower()

def extract_order_fields(order_text: str) -> dict:
    """Multiple fields — JSON is justified here."""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{
            "role": "user",
            "content": (
                'Extract order fields as JSON: {"product": "", "quantity": 0, "urgency": ""}.\n\n'
                f"Order: {order_text}"
            ),
        }],
    )
    return json.loads(response.content[0].text)

# Single field → text
dept = route_request("My invoice has a wrong charge.")
print(f"[route] → {dept}")  # "billing"

# Multiple fields → JSON (justified)
order = extract_order_fields("Rush order: 50 units of SKU-123, needed by Friday.")
print(f"[extract] → {order}")

# Decision rule: use plain text when fields == 1, JSON when fields >= 2
def choose_format(fields: list[str]) -> str:
    if len(fields) == 1:
        return "plain text"
    elif len(fields) <= 5:
        return "JSON object"
    else:
        return "JSON with schema validation"

for n in [1, 3, 8]:
    fields = [f"field_{i}" for i in range(n)]
    print(f"{n} field(s) → {choose_format(fields)}")
```

**Expected Token Savings:** Enforcing the "plain text for 1 field" rule across a codebase typically reduces output tokens by 30–60% for classification-heavy workloads.
**Environment:** Any agent that mixes classification tasks with multi-field extraction; the rule is cheap to enforce with a code review checklist.

---

### Option 3 — Prefill to force ultra-short structured responses

```python
import anthropic

client = anthropic.Anthropic()

def yes_no(question: str, context: str) -> bool:
    """Force a yes/no answer with assistant prefill — 1 token output."""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=4,
        messages=[
            {"role": "user",    "content": f"Context: {context}\n\nQuestion: {question}\nAnswer yes or no."},
            {"role": "assistant","content": ""},   # prefill — model continues from here
        ],
    )
    text = response.content[0].text.strip().lower()
    return text.startswith("yes")

def score_0_to_10(content: str, criterion: str) -> int:
    """Force a single digit — 1–2 tokens."""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=4,
        messages=[
            {"role": "user",     "content": f"Rate '{criterion}' of this text 0-10 (integer only):\n\n{content}"},
            {"role": "assistant","content": ""},
        ],
    )
    try:
        return int(response.content[0].text.strip().split()[0])
    except (ValueError, IndexError):
        return 5  # default on parse failure

# Usage
is_spam = yes_no(
    "Is this email spam?",
    "CONGRATULATIONS! You've won a $1,000,000 prize. Click here."
)
print(f"[spam] {is_spam}")  # True

quality = score_0_to_10(
    "The report covers all key metrics with clear visualisations.",
    "clarity"
)
print(f"[quality] {quality}/10")

# Token comparison: JSON vs prefill
r_json = client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=64,
    messages=[{"role": "user", "content": 'Return {"is_spam": true/false} for: FREE MONEY NOW'}],
)
r_prefill = client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=4,
    messages=[
        {"role": "user",     "content": "Is this spam? yes or no: FREE MONEY NOW"},
        {"role": "assistant","content": ""},
    ],
)
print(f"JSON: {r_json.usage.output_tokens} tokens | Prefill: {r_prefill.usage.output_tokens} token(s)")
```

**Expected Token Savings:** Prefill-forced single-token outputs (yes/no, 0–9, a/b/c) use 1–2 output tokens vs 15–40 for JSON; 95%+ output token reduction for boolean classifiers.
**Environment:** High-throughput quality scoring, spam filtering, sentiment binary; wherever a human would circle one answer on a form.

---

### Option 4 — CSV for list outputs instead of JSON arrays

```python
import anthropic
import json

client = anthropic.Anthropic()

def extract_tags_json(text: str) -> list[str]:
    """JSON array — verbose."""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{
            "role": "user",
            "content": f'Extract topic tags as JSON array of strings:\n\n{text}',
        }],
    )
    return json.loads(response.content[0].text)

def extract_tags_csv(text: str) -> list[str]:
    """Comma-separated — no brackets, no quotes, no JSON overhead."""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        messages=[{
            "role": "user",
            "content": (
                "Extract topic tags, comma-separated, no extra text:\n\n"
                f"{text}"
            ),
        }],
    )
    raw = response.content[0].text.strip()
    return [t.strip() for t in raw.split(",") if t.strip()]

text = "This article covers Python asyncio, FastAPI, PostgreSQL, and Docker Compose for building async microservices."

# Compare token costs
r_json = client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=128,
    messages=[{"role": "user", "content": f'Return JSON array of tags for:\n{text}'}],
)
r_csv = client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=64,
    messages=[{"role": "user", "content": f'Tags, comma-separated only:\n{text}'}],
)
print(f"JSON: {r_json.usage.output_tokens} output tokens → {r_json.content[0].text[:60]}")
print(f"CSV:  {r_csv.usage.output_tokens} output tokens → {r_csv.content[0].text[:60]}")

tags = extract_tags_csv(text)
print(f"Parsed tags: {tags}")
```

**Expected Token Savings:** A 5-tag JSON array `["python", "asyncio", "fastapi", "postgresql", "docker"]` costs ~20 tokens; CSV `python, asyncio, fastapi, postgresql, docker` costs ~12 tokens — 40% less; scales with list length.
**Environment:** Entity extraction, keyword extraction, tag generation; any output that is a flat list of short strings.

---

### Option 5 — Format selector: choose output format based on field count

```python
import json
import anthropic
from typing import Any

client = anthropic.Anthropic()

def smart_extract(
    text: str,
    fields: list[str],
    model: str = "claude-haiku-4-5-20251001",
) -> Any:
    """
    Automatically choose the most token-efficient output format
    based on the number of requested fields.
    """
    n = len(fields)

    if n == 1:
        # Plain text — no format overhead
        field = fields[0]
        response = client.messages.create(
            model=model,
            max_tokens=32,
            messages=[{
                "role": "user",
                "content": f"Extract the {field} from this text. Reply with just the value:\n\n{text}",
            }],
        )
        return {field: response.content[0].text.strip()}

    elif n <= 4:
        # Pipe-delimited — compact, parseable
        header = "|".join(fields)
        response = client.messages.create(
            model=model,
            max_tokens=128,
            messages=[{
                "role": "user",
                "content": (
                    f"Extract these fields and return them pipe-delimited in this exact order: {header}\n"
                    f"Format: value1|value2|...\n\nText: {text}"
                ),
            }],
        )
        values = response.content[0].text.strip().split("|")
        return dict(zip(fields, [v.strip() for v in values]))

    else:
        # Many fields — JSON is justified; structure helps avoid errors
        schema = "{" + ", ".join(f'"{f}": ""' for f in fields) + "}"
        response = client.messages.create(
            model=model,
            max_tokens=512,
            messages=[{
                "role": "user",
                "content": f"Extract fields as JSON matching this schema: {schema}\n\nText: {text}",
            }],
        )
        return json.loads(response.content[0].text)

# 1 field → plain text
result = smart_extract("The invoice total is $1,234.56.", ["total"])
print(f"1 field: {result}")

# 3 fields → pipe-delimited
result = smart_extract("John Smith, age 34, lives in Austin TX.", ["name", "age", "city"])
print(f"3 fields: {result}")

# 6 fields → JSON
result = smart_extract(
    "Order #4521: 10 units of Widget A, priority high, shipping express, customer ACME Corp, due 2026-04-20, cost $450.",
    ["order_id", "quantity", "product", "priority", "shipping", "due_date"],
)
print(f"6 fields: {result}")
```

**Expected Token Savings:** Pipe-delimited format for 2–4 fields saves 20–40% vs JSON; plain text for 1 field saves 60–90%; the format selector is transparent to callers — same return type (dict) in all cases.
**Environment:** General-purpose extraction agents; useful as a shared utility across many tool implementations.

---

### Option 6 — Audit existing prompts and flag JSON-for-single-field anti-patterns

```python
import re
import anthropic

client = anthropic.Anthropic()

# Registry of existing prompts to audit
PROMPTS_TO_AUDIT = [
    {
        "name":   "sentiment_classifier",
        "prompt": 'Classify sentiment as positive, negative, or neutral. Return {"sentiment": "<label>"}.',
        "expected_fields": 1,
    },
    {
        "name":   "topic_extractor",
        "prompt": 'Extract the main topic and subtopics. Return {"topic": "", "subtopics": []}.',
        "expected_fields": 2,
    },
    {
        "name":   "spam_detector",
        "prompt": 'Is this spam? Return {"is_spam": true/false, "confidence": 0.0-1.0}.',
        "expected_fields": 2,
    },
    {
        "name":   "language_detector",
        "prompt": 'Detect language. Return {"language_code": "en"}.',
        "expected_fields": 1,
    },
]

JSON_PATTERN = re.compile(r'\{[^}]{1,200}\}')

def audit_prompt(entry: dict) -> dict:
    prompt = entry["prompt"]
    has_json_instruction = bool(JSON_PATTERN.search(prompt))
    fields = entry["expected_fields"]
    is_wasteful = has_json_instruction and fields == 1

    suggestion = None
    if is_wasteful:
        # Estimate savings
        json_tokens  = 20   # typical single-field JSON output
        plain_tokens = 3    # typical single-value output
        suggestion = (
            f"Replace JSON with plain text. "
            f"Est. savings: {json_tokens - plain_tokens} output tokens/call "
            f"(~{(1 - plain_tokens/json_tokens)*100:.0f}%)."
        )

    return {
        "name":          entry["name"],
        "uses_json":     has_json_instruction,
        "fields":        fields,
        "wasteful":      is_wasteful,
        "suggestion":    suggestion,
    }

print("=== Prompt Audit Report ===")
for entry in PROMPTS_TO_AUDIT:
    result = audit_prompt(entry)
    status = "⚠ WASTEFUL" if result["wasteful"] else "✓ OK"
    print(f"\n[{status}] {result['name']}")
    if result["suggestion"]:
        print(f"  → {result['suggestion']}")

# Demonstrate the fix for sentiment_classifier
response = client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=4,
    messages=[{
        "role": "user",
        "content": "Classify sentiment (positive/negative/neutral), one word only: 'This product is amazing!'",
    }],
)
print(f"\n[fixed] sentiment: '{response.content[0].text.strip()}' "
      f"({response.usage.output_tokens} output tokens)")
```

**Expected Token Savings:** Auditing an existing codebase often finds 30–50% of prompts using JSON for single-field outputs; fixing them requires one-line prompt changes with no structural refactoring.
**Environment:** Existing codebases; especially useful as a pre-deploy CI check or code review guideline to prevent the anti-pattern from accumulating.

---

## Comparison

| Option | Format | Output Tokens (typical) | Parseable | Multi-field | Best For |
|---|---|---|---|---|---|
| 1. Plain text | Raw string | 1–10 | Yes (strip) | No | Single-label classification |
| 2. JSON when ≥2 fields | JSON or text | Varies | Yes | Yes | Mixed extraction workloads |
| 3. Prefill forcing | 1 token | 1–2 | Yes (starts-with) | No | Boolean, 0–9 score, A/B/C choice |
| 4. CSV for lists | Comma-sep | 30–50% less than JSON | Yes (split) | Lists only | Tag/keyword extraction |
| 5. Smart format selector | Auto | Minimum for field count | Yes | Yes | General-purpose extraction |
| 6. Audit tool | N/A | N/A | N/A | N/A | Fixing existing codebases |
