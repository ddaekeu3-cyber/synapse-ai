---
layout: solution
title: "Agent Doesn't Use Few-Shot Examples"
category: prompt-engineering
description: "Agent output format is inconsistent across calls because the prompt describes the format in prose instead of demonstrating it with examples."
tags: [prompt-engineering, few-shot, output-format, consistency, reliability]
---

## Symptom

The agent's instructions say "return a JSON object with fields X, Y, Z" — but the model sometimes wraps the JSON in markdown fences, sometimes adds explanatory prose before it, sometimes uses different field names, and sometimes returns a completely different structure. Every caller has to defensively parse multiple possible output shapes.

## Root Cause

Prose instructions describe what to do; few-shot examples show it. Language models generalise format from patterns far more reliably than from descriptions. A single demonstration anchors field names, casing, nesting depth, array vs. object choice, and edge-case handling in a way that paragraphs of instructions cannot. Without examples, the model fills ambiguity with its own defaults — which vary by phrasing, token sampling, and model version.

## Fix

### Option 1 — Static few-shot examples in the system prompt

```python
import json
import anthropic

client = anthropic.Anthropic()

SYSTEM = """Extract product information from text and return ONLY a JSON object.
No markdown fences. No prose. Only the JSON object.

Examples:

Input: "The Nike Air Max 270 in black costs $150 and is available in sizes 8–13."
Output: {"name": "Nike Air Max 270", "color": "black", "price_usd": 150, "sizes": [8, 9, 10, 11, 12, 13]}

Input: "Blue denim jacket by Levi's, $89.99, one size fits all."
Output: {"name": "Levi's Denim Jacket", "color": "blue", "price_usd": 89.99, "sizes": null}

Input: "Red running shorts, $35."
Output: {"name": "Red Running Shorts", "color": "red", "price_usd": 35, "sizes": null}
"""

def extract_product(text: str) -> dict:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=SYSTEM,
        messages=[{"role": "user", "content": text}],
    )
    raw = response.content[0].text.strip()
    return json.loads(raw)

samples = [
    "Adidas Ultraboost 22 in white, $180, available sizes 7 to 12.",
    "Green hoodie by Champion, $55, sizes S, M, L, XL.",
    "Yellow rain jacket, waterproof, $120.",
]
for s in samples:
    result = extract_product(s)
    print(json.dumps(result, indent=2))
```

**Expected Token Savings:** Eliminates retry loops caused by malformed output; consistent format means no fallback parsing code.
**Environment:** Any extraction or classification task where output format must be machine-readable.

---

### Option 2 — Dynamic few-shot selection based on input similarity

```python
import anthropic

client = anthropic.Anthropic()

# Example bank: (input_keywords, input_text, expected_output)
EXAMPLE_BANK = [
    ({"invoice", "total", "vendor"},
     "Invoice #4521 from Acme Corp. Total due: $2,340.00.",
     '{"document_type": "invoice", "vendor": "Acme Corp", "amount_usd": 2340.00, "reference": "4521"}'),

    ({"receipt", "paid", "store"},
     "Receipt from Target. Items: $45.32. Tax: $3.62. Total paid: $48.94.",
     '{"document_type": "receipt", "vendor": "Target", "amount_usd": 48.94, "reference": null}'),

    ({"contract", "agreement", "parties"},
     "Service agreement between ClientCo and VendorInc dated 2024-03-01.",
     '{"document_type": "contract", "vendor": "VendorInc", "amount_usd": null, "reference": null}'),

    ({"refund", "credit", "return"},
     "Refund of $89.00 issued by Amazon for order #112-3456789.",
     '{"document_type": "refund", "vendor": "Amazon", "amount_usd": -89.00, "reference": "112-3456789"}'),
]

def select_examples(text: str, n: int = 2) -> list[tuple[str, str]]:
    words = set(text.lower().split())
    scored = sorted(
        EXAMPLE_BANK,
        key=lambda ex: len(ex[0] & words),
        reverse=True,
    )
    return [(ex[1], ex[2]) for ex in scored[:n]]

def extract_document(text: str) -> dict:
    examples = select_examples(text)
    example_str = "\n\n".join(f"Input: {inp}\nOutput: {out}" for inp, out in examples)

    system = (
        "Extract document metadata. Return ONLY a JSON object with keys: "
        "document_type, vendor, amount_usd, reference.\n\n"
        f"Examples:\n\n{example_str}"
    )
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=system,
        messages=[{"role": "user", "content": text}],
    )
    import json
    return json.loads(response.content[0].text.strip())

texts = [
    "Invoice #7890 from SupplierX. Amount owed: $5,600.",
    "Store receipt from Walmart, total paid $23.50.",
    "Credit memo: $150 refund from Shopify for order #abc-999.",
]
for t in texts:
    import json
    print(json.dumps(extract_document(t), indent=2))
```

**Expected Token Savings:** Dynamic selection keeps system prompt shorter than including all examples; matched examples improve accuracy, reducing re-runs.
**Environment:** Diverse extraction tasks with an example bank; pairs well with prompt caching on the static system prefix.

---

### Option 3 — Few-shot via the messages array (user/assistant turns)

```python
import json
import anthropic

client = anthropic.Anthropic()

SYSTEM = "Classify customer support tickets. Respond ONLY with a JSON object."

FEW_SHOT: list[dict] = [
    {"role": "user",      "content": "My order hasn't arrived after 3 weeks!"},
    {"role": "assistant", "content": '{"category": "shipping", "priority": "high", "sentiment": "angry"}'},
    {"role": "user",      "content": "How do I reset my password?"},
    {"role": "assistant", "content": '{"category": "account", "priority": "low", "sentiment": "neutral"}'},
    {"role": "user",      "content": "The app keeps crashing when I open settings."},
    {"role": "assistant", "content": '{"category": "bug", "priority": "high", "sentiment": "frustrated"}'},
    {"role": "user",      "content": "Love your product! Just wanted to say thanks."},
    {"role": "assistant", "content": '{"category": "feedback", "priority": "low", "sentiment": "positive"}'},
]

def classify_ticket(ticket: str) -> dict:
    messages = FEW_SHOT + [{"role": "user", "content": ticket}]
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        system=SYSTEM,
        messages=messages,
    )
    return json.loads(response.content[0].text.strip())

tickets = [
    "I was charged twice for my subscription this month.",
    "Can you add dark mode to the mobile app?",
    "Error 500 when submitting the checkout form.",
    "Your support team was incredibly helpful today!",
]
for t in tickets:
    result = classify_ticket(t)
    print(f"Ticket: {t!r}")
    print(f"Result: {result}\n")
```

**Expected Token Savings:** In-messages examples are reusable across calls; combine with prompt caching on the few-shot prefix for ~85% cost reduction on the examples.
**Environment:** Classification tasks (intent, sentiment, priority, routing) where the output schema is fixed.

---

### Option 4 — Chain-of-thought few-shot for reasoning tasks

```python
import anthropic

client = anthropic.Anthropic()

SYSTEM = """Analyse SQL queries for performance issues. Show your reasoning, then give a final verdict.

Example 1:
Query: SELECT * FROM orders WHERE customer_id = 123;
Analysis: Checking for index on customer_id... customer_id is a foreign key and should be indexed.
SELECT * retrieves all columns — inefficient if only a few are needed.
Verdict: {"issue": "missing_projection", "severity": "medium", "suggestion": "Select only needed columns; verify index on customer_id."}

Example 2:
Query: SELECT COUNT(*) FROM users;
Analysis: Full table scan required for COUNT(*) with no WHERE clause. This is expected for total count.
No problematic joins or subqueries.
Verdict: {"issue": "none", "severity": "low", "suggestion": "Use COUNT(1) for marginal speed gain; add caching if called frequently."}

Example 3:
Query: SELECT * FROM products p JOIN inventory i ON p.id = i.product_id WHERE i.quantity > 0 ORDER BY p.name;
Analysis: JOIN on product_id — check index on inventory.product_id. ORDER BY p.name requires sort; index on name would help.
SELECT * is broad.
Verdict: {"issue": "unindexed_join_and_sort", "severity": "high", "suggestion": "Add index on inventory.product_id and products.name; project only needed columns."}
"""

def analyse_query(sql: str) -> str:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=SYSTEM,
        messages=[{"role": "user", "content": f"Query: {sql}"}],
    )
    return response.content[0].text

queries = [
    "SELECT * FROM logs WHERE created_at > '2024-01-01';",
    "SELECT u.name, o.total FROM users u, orders o WHERE u.id = o.user_id AND o.total > 1000;",
]
for q in queries:
    print(f"SQL: {q}")
    print(analyse_query(q))
    print()
```

**Expected Token Savings:** Chain-of-thought examples reduce wrong-answer retries on complex reasoning; each saved retry is 500–2 000 tokens.
**Environment:** Complex analysis tasks (code review, SQL analysis, security audit) where reasoning quality matters more than brevity.

---

### Option 5 — Negative examples to prevent common mistakes

```python
import json
import anthropic

client = anthropic.Anthropic()

SYSTEM = """Convert dates to ISO 8601 format (YYYY-MM-DD). Return ONLY a JSON object: {"date": "YYYY-MM-DD"}.

Correct examples:
Input: "March 5th, 2024"          → {"date": "2024-03-05"}
Input: "5/3/2024" (US format)     → {"date": "2024-05-03"}
Input: "3rd of December, 2023"    → {"date": "2023-12-03"}

WRONG (do not do this):
Input: "January 1, 2024"   WRONG: {"date": "01-01-2024"}   ← wrong order
Input: "2024/06/15"        WRONG: {"date": "15/06/2024"}   ← wrong separator
Input: "Today"             WRONG: {"date": "today"}         ← not a real date; use null instead

Correct for ambiguous:
Input: "Today"             → {"date": null, "error": "relative date cannot be resolved"}
"""

def parse_date(date_string: str) -> dict:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        system=SYSTEM,
        messages=[{"role": "user", "content": date_string}],
    )
    return json.loads(response.content[0].text.strip())

test_dates = [
    "February 28th, 2025",
    "12/25/2024",
    "the 4th of July, 2026",
    "next Monday",
    "2025-11-11",
]
for d in test_dates:
    result = parse_date(d)
    print(f"{d!r:30s} → {result}")
```

**Expected Token Savings:** Negative examples cut common error modes without needing a validation retry loop.
**Environment:** Extraction tasks with tricky edge cases (date formats, currency, unit conversion) where hallucinated formats are common.

---

### Option 6 — Auto-generate few-shot examples from validated historical outputs

```python
import json
import os
import anthropic

client = anthropic.Anthropic()

EXAMPLE_STORE_PATH = "/tmp/few_shot_examples.json"

def load_examples() -> list[dict]:
    if os.path.exists(EXAMPLE_STORE_PATH):
        with open(EXAMPLE_STORE_PATH) as f:
            return json.load(f)
    return []

def save_example(input_text: str, output: dict, max_stored: int = 20) -> None:
    examples = load_examples()
    examples.append({"input": input_text, "output": json.dumps(output)})
    if len(examples) > max_stored:
        examples = examples[-max_stored:]  # keep the most recent
    tmp = EXAMPLE_STORE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(examples, f, indent=2)
    os.replace(tmp, EXAMPLE_STORE_PATH)

def build_system_with_examples(base_instruction: str, n: int = 3) -> str:
    examples = load_examples()[-n:]
    if not examples:
        return base_instruction
    example_str = "\n\n".join(f"Input: {ex['input']}\nOutput: {ex['output']}" for ex in examples)
    return f"{base_instruction}\n\nExamples from previous validated calls:\n\n{example_str}"

BASE = "Extract the company name and revenue from the text. Return ONLY JSON: {\"company\": ..., \"revenue_usd\": ...}"

def extract_financials(text: str, validated: bool = False) -> dict:
    system = build_system_with_examples(BASE)
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        system=system,
        messages=[{"role": "user", "content": text}],
    )
    result = json.loads(response.content[0].text.strip())

    # Only save validated outputs as future examples
    if validated:
        save_example(text, result)

    return result

texts = [
    ("Acme Corp reported annual revenue of $4.2 billion.", True),
    ("GlobalTech's Q3 earnings were $890 million.",         True),
    ("DataSoft earned $2.1B last fiscal year.",             True),
    ("NovaCorp: $550M revenue in 2024.",                    False),  # not validated yet
]
for text, is_validated in texts:
    r = extract_financials(text, validated=is_validated)
    print(f"{text!r}")
    print(f"  → {r} ({'saved' if is_validated else 'not saved'})\n")
```

**Expected Token Savings:** Examples grow automatically from production traffic; no manual curation needed; improves consistency over time.
**Environment:** Production extraction pipelines where human-validated outputs can be recycled as few-shot examples.

---

## Comparison

| Option | Example Source | Dynamic? | Handles Edge Cases | Best For |
|---|---|---|---|---|
| 1. Static system prompt | Handcrafted | No | Partial | Fixed schema extraction |
| 2. Dynamic selection | Example bank | Yes | Medium | Diverse input types |
| 3. Messages array | Handcrafted | No | Medium | Classification with caching |
| 4. Chain-of-thought | Handcrafted | No | High | Complex reasoning tasks |
| 5. Negative examples | Handcrafted | No | High | Known error-prone formats |
| 6. Auto-generated | Production data | Yes | Grows over time | High-volume pipelines |
