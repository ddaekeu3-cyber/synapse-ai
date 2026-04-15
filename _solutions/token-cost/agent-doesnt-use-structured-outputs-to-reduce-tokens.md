---
layout: solution
title: "Agent doesn't use structured outputs to reduce tokens"
category: token-cost
description: "Agent asks for free-form prose responses when structured JSON would convey the same information in far fewer tokens."
tags: [token-cost, structured-output, json, prompt-engineering, efficiency]
---

## Symptom

The agent requests natural-language answers for tasks that only need data — classifiers, extractors, routers, scorers. The model produces polite prose ("Certainly! Based on my analysis, the sentiment of this text is positive, with a confidence level of...") when a two-token JSON blob `{"sentiment":"positive","score":0.9}` would suffice. Token usage per call is 3–10x higher than necessary.

```
Prose response:   "The sentiment of the provided text is clearly positive.
                   The author expresses enthusiasm and satisfaction..."
Token count:      87 tokens

JSON response:    {"sentiment": "positive", "score": 0.92}
Token count:      14 tokens
```

## Root Cause

The default model behavior is to be helpful and conversational. Without explicit constraints, the model wraps every answer in natural language. Agents that never instruct the model to produce structured output pay a constant prose overhead on every call, even for entirely mechanical tasks like classification, extraction, and routing.

## Fix

Instruct the model to respond with only JSON (or another compact structure), parse the result, and skip the prose wrapper entirely. For even tighter control, use tool use to force a typed schema.

---

### Option 1 — JSON-only system prompt for classification tasks

```python
import anthropic
import json

client = anthropic.Anthropic()

CLASSIFY_SYSTEM = """
You are a sentiment classifier. Respond with ONLY a JSON object — no prose, no markdown,
no explanation. The object must have exactly these keys:

{
  "sentiment": "positive" | "negative" | "neutral",
  "score": <float 0.0–1.0>,
  "dominant_emotion": <string>
}
""".strip()

def classify_sentiment(text: str) -> dict:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,          # short: JSON fits in ~20 tokens
        system=CLASSIFY_SYSTEM,
        messages=[{"role": "user", "content": text}],
    )
    raw = response.content[0].text.strip()
    result = json.loads(raw)
    print(f"Tokens used: {response.usage.input_tokens} in / {response.usage.output_tokens} out")
    return result

texts = [
    "I absolutely love this product — best purchase I've made all year!",
    "The service was mediocre, nothing to write home about.",
    "This is a complete disaster. I want a refund immediately.",
]

for text in texts:
    result = classify_sentiment(text)
    print(f"Text: {text[:50]}...")
    print(f"Result: {result}\n")
```

**Expected Token Savings:** 60–85% output token reduction versus free-form prose; `max_tokens=64` enforces the budget constraint.

**Environment:** Any classification, scoring, or extraction task; works with all Claude models.

---

### Option 2 — Tool use to enforce a typed output schema

```python
import anthropic
import json

client = anthropic.Anthropic()

# Define the output schema as a tool — the model is forced to call it
EXTRACTION_TOOL = {
    "name": "submit_extraction",
    "description": "Submit the extracted entities from the input text.",
    "input_schema": {
        "type": "object",
        "properties": {
            "company_name":  {"type": "string",  "description": "Primary company name mentioned"},
            "ticker":        {"type": "string",  "description": "Stock ticker symbol if present, else null"},
            "revenue_usd":   {"type": "number",  "description": "Revenue figure in USD, else null"},
            "yoy_growth_pct":{"type": "number",  "description": "Year-over-year growth percentage, else null"},
            "fiscal_quarter":{"type": "string",  "description": "Fiscal quarter e.g. Q1 2025, else null"},
        },
        "required": ["company_name"],
    },
}

def extract_financials(text: str) -> dict:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        tools=[EXTRACTION_TOOL],
        tool_choice={"type": "tool", "name": "submit_extraction"},  # force the tool
        messages=[{"role": "user", "content": f"Extract financial entities:\n\n{text}"}],
    )

    tool_block = next(b for b in response.content if b.type == "tool_use")
    print(f"Tokens: {response.usage.input_tokens} in / {response.usage.output_tokens} out")
    return tool_block.input

sample = (
    "Acme Corp (ACME) reported Q2 2025 revenue of $4.2B, "
    "up 18% year-over-year, beating analyst expectations."
)

result = extract_financials(sample)
print(json.dumps(result, indent=2))
```

**Expected Token Savings:** 70–90% output token reduction; tool use produces only the schema fields, eliminating all prose; model cannot deviate from the schema.

**Environment:** Entity extraction, data normalization, form-filling agents; `tool_choice: {"type": "tool", "name": "..."}` is the strongest constraint available.

---

### Option 3 — Prefill the assistant turn to anchor JSON output

```python
import anthropic
import json

client = anthropic.Anthropic()

def route_intent(user_message: str) -> dict:
    """
    Use assistant turn prefill to force the model to complete a JSON object
    rather than start with conversational prose.
    """
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=80,
        system=(
            "You are an intent router. Classify the user's request into one of: "
            "['search', 'create', 'update', 'delete', 'summarize', 'unknown']. "
            "Output ONLY the continuation of the JSON started in your turn."
        ),
        messages=[
            {"role": "user", "content": user_message},
            # Prefill: the model must complete this JSON, not start fresh with prose
            {"role": "assistant", "content": '{"intent":'},
        ],
    )

    # The model's output is the completion of '{"intent":'
    completion = response.content[0].text.strip()
    full_json = '{"intent":' + completion

    # Handle trailing characters if model adds explanation after }
    end = full_json.rfind("}") + 1
    result = json.loads(full_json[:end])
    print(f"Tokens: {response.usage.input_tokens} in / {response.usage.output_tokens} out")
    return result

messages = [
    "Can you find me all orders from last week?",
    "Please delete the draft named 'Q3 report'.",
    "Write a summary of the attached document.",
    "Update the user's email to alice@example.com",
]

for msg in messages:
    result = route_intent(msg)
    print(f"'{msg[:45]}...' → {result}")
```

**Expected Token Savings:** 50–75% output token reduction; prefill forces the model into completion mode with no preamble tokens wasted on acknowledgment phrases.

**Environment:** Routing, classification, and labeling agents; prefill is supported by the Anthropic API — pass an `assistant` turn before the final response.

---

### Option 4 — Batch structured extraction with async fan-out

```python
import anthropic
import asyncio
import json

async_client = anthropic.AsyncAnthropic()

EXTRACT_SYSTEM = """
Respond ONLY with a JSON object with these fields:
{
  "category": <string>,
  "priority": "high" | "medium" | "low",
  "action_required": <boolean>,
  "summary": <string max 10 words>
}
No prose. No markdown. Only valid JSON.
""".strip()

async def classify_ticket(ticket: str, ticket_id: int) -> dict:
    response = await async_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=80,
        system=EXTRACT_SYSTEM,
        messages=[{"role": "user", "content": ticket}],
    )
    raw = response.content[0].text.strip()
    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        # Fallback: extract JSON from any surrounding text
        start = raw.find("{")
        end   = raw.rfind("}") + 1
        result = json.loads(raw[start:end]) if start >= 0 else {"error": "parse_failed", "raw": raw}

    result["ticket_id"] = ticket_id
    result["tokens_out"] = response.usage.output_tokens
    return result

async def batch_classify(tickets: list[str]) -> list[dict]:
    tasks = [classify_ticket(t, i) for i, t in enumerate(tickets)]
    results = await asyncio.gather(*tasks)
    total_out = sum(r.get("tokens_out", 0) for r in results)
    print(f"\nTotal output tokens: {total_out} for {len(tickets)} tickets "
          f"(avg {total_out/len(tickets):.1f}/ticket)")
    return list(results)

tickets = [
    "Login page returns 500 error for all users since 14:00 UTC.",
    "Can you add dark mode to the dashboard? Would be nice.",
    "Payment processing is broken — customers cannot check out.",
    "The font on the about page looks slightly off on mobile.",
    "Database connection pool exhausted — production is down.",
]

results = asyncio.run(batch_classify(tickets))
for r in sorted(results, key=lambda x: ["high","medium","low"].index(x.get("priority","low"))):
    print(f"[{r['priority'].upper():6}] #{r['ticket_id']} {r.get('summary','')}")
```

**Expected Token Savings:** 65–80% output token reduction per ticket; async fan-out means all tickets are classified in parallel at the latency of one request.

**Environment:** Support ticket triage, log classification, document tagging pipelines.

---

### Option 5 — Schema registry: reuse schemas with prompt caching

```python
import anthropic
import json

client = anthropic.Anthropic()

# Static schema instruction — suitable for prompt caching
SCHEMA_BLOCK = """
OUTPUT FORMAT (strict JSON only — no prose, no markdown fences):

For product reviews:
{
  "overall_rating": <integer 1–5>,
  "pros": [<string>, ...],           // max 3 items
  "cons": [<string>, ...],           // max 3 items
  "verified_purchase": <boolean>,
  "recommend": <boolean>
}
""".strip()

def analyze_review(review_text: str) -> dict:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        system=[
            {
                "type": "text",
                "text": SCHEMA_BLOCK,
                "cache_control": {"type": "ephemeral"},  # cache the static schema
            }
        ],
        extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
        messages=[{"role": "user", "content": review_text}],
    )

    raw = response.content[0].text.strip()
    result = json.loads(raw)

    cache_read = getattr(response.usage, "cache_read_input_tokens", 0)
    cache_write = getattr(response.usage, "cache_creation_input_tokens", 0)
    print(f"Tokens: {response.usage.input_tokens} in / {response.usage.output_tokens} out | "
          f"cache read={cache_read} write={cache_write}")
    return result

reviews = [
    "Fantastic product! It works exactly as described, arrived fast, and the build quality is excellent. "
    "Only downside is the manual could be clearer. Highly recommend!",

    "Garbage. Broke after two days. Customer service didn't help. Avoid.",

    "Pretty good value for the price. Not perfect — the app is a bit clunky — but it does the job.",
]

for review in reviews:
    result = analyze_review(review)
    print(json.dumps(result, indent=2), "\n")
```

**Expected Token Savings:** 65–80% output reduction from structured output + 60–80% input reduction on repeated calls from prompt caching the schema block; double-stacked savings.

**Environment:** High-volume review analysis, content moderation, or any task where the same schema is reused across many calls.

---

### Option 6 — Output validator with automatic re-prompt on parse failure

```python
import anthropic
import json
import re

client = anthropic.Anthropic()

SYSTEM = """
Respond with ONLY a JSON object. No markdown, no prose.
Schema:
{
  "language": <ISO 639-1 code>,
  "confidence": <float 0.0–1.0>,
  "script": "latin" | "cyrillic" | "cjk" | "arabic" | "other"
}
""".strip()

def detect_language(text: str, max_retries: int = 2) -> dict:
    messages = [{"role": "user", "content": f"Detect the language of:\n\n{text}"}]
    total_tokens = 0

    for attempt in range(max_retries + 1):
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=64,
            system=SYSTEM,
            messages=messages,
        )
        total_tokens += response.usage.output_tokens
        raw = response.content[0].text.strip()

        # Try direct parse
        try:
            result = json.loads(raw)
            print(f"Parsed on attempt {attempt+1} | total_out_tokens={total_tokens}")
            return result
        except json.JSONDecodeError:
            pass

        # Try extracting JSON from the text
        match = re.search(r"\{.*?\}", raw, re.DOTALL)
        if match:
            try:
                result = json.loads(match.group())
                print(f"Regex-extracted on attempt {attempt+1} | total_out_tokens={total_tokens}")
                return result
            except json.JSONDecodeError:
                pass

        if attempt < max_retries:
            # Append the bad response and a correction prompt
            messages.append({"role": "assistant", "content": raw})
            messages.append({
                "role": "user",
                "content": (
                    "That was not valid JSON. Reply with ONLY a JSON object matching the schema. "
                    "No other text."
                ),
            })
            print(f"[RETRY {attempt+1}] Invalid JSON: {raw[:60]!r}")

    return {"error": "parse_failed", "raw": raw}

samples = [
    "Le chat est sur le tapis.",
    "Привет, как дела?",
    "今日は良い天気ですね。",
    "The quick brown fox jumps over the lazy dog.",
    "مرحباً بك في عالم البرمجة.",
]

for sample in samples:
    result = detect_language(sample)
    print(f"'{sample[:35]}' → {result}\n")
```

**Expected Token Savings:** 70–85% output token reduction; retry mechanism ensures reliability without manual intervention; most calls parse on the first attempt.

**Environment:** Any extraction or classification agent where parse failure is possible; the validator prevents silent data corruption without needing an external validation library.

---

## Comparison

| Option | Enforcement | Parse Safety | Caching | Best For |
|--------|------------|-------------|---------|---------|
| 1 — JSON system prompt | Instruction | Try/except | Yes | Simple classification |
| 2 — Tool use schema | API-enforced | Guaranteed | Partial | Entity extraction |
| 3 — Assistant prefill | Context anchor | Try/except | Yes | Routing, labeling |
| 4 — Async batch | Instruction | Try/except | Yes | High-volume pipelines |
| 5 — Cached schema registry | Instruction + cache | Try/except | Full | Repeated-schema tasks |
| 6 — Validator + re-prompt | Instruction + retry | Retry recovery | Yes | Reliability-critical tasks |

**Recommended default:** Option 2 (tool use) for the strongest schema enforcement with zero parse failures. Use Option 5 (cached schema + instruction) for the best token economy when the same schema is reused thousands of times.
