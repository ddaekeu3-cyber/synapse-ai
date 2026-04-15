---
layout: solution
title: "Agent Doesn't Validate Output Schema Before Returning"
category: general
description: "Agent returns whatever the model generates, allowing malformed JSON, missing fields, and wrong types to reach downstream systems."
tags: [general, validation, pydantic, output-format, reliability]
---

## Symptom

Downstream code that parses the agent's output throws `KeyError`, `TypeError`, or `json.JSONDecodeError` intermittently. The agent returns valid JSON most of the time, but occasionally wraps it in a markdown fence, omits a required field, returns `null` where an array was expected, or produces a string where a number was required. These errors are hard to reproduce because they depend on sampling temperature.

## Root Cause

Language models don't guarantee schema compliance — they approximate it based on instructions and few-shot examples. Without a validation step, any deviation in the model's output is silently passed to the caller. The failure surfaces in downstream code rather than at the agent boundary, making debugging difficult and the error message misleading.

## Fix

### Option 1 — JSON parse + key presence check

```python
import json
import anthropic

client = anthropic.Anthropic()

REQUIRED_FIELDS = {"name", "price_usd", "category", "in_stock"}

SYSTEM = """Extract product information and return ONLY a JSON object with these fields:
- name (string)
- price_usd (number)
- category (string)
- in_stock (boolean)

Return no markdown, no prose — only the JSON object."""

def extract_product(text: str, max_attempts: int = 3) -> dict:
    for attempt in range(max_attempts):
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            system=SYSTEM,
            messages=[{"role": "user", "content": text}],
        )
        raw = response.content[0].text.strip()

        # Strip markdown fences if present
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            print(f"[validate] attempt {attempt + 1}: JSON parse error: {e}")
            continue

        missing = REQUIRED_FIELDS - set(data.keys())
        if missing:
            print(f"[validate] attempt {attempt + 1}: missing fields: {missing}")
            continue

        return data

    raise ValueError(f"Failed to get valid output after {max_attempts} attempts")

result = extract_product("Blue wireless headphones by Sony, $149.99, in stock.")
print(result)
```

**Expected Token Savings:** Catches malformed output early; at most 3 attempts per call vs. cascading failures that require re-running the entire pipeline.
**Environment:** Simple extraction tasks; good first step before adding Pydantic.

---

### Option 2 — Pydantic model validation with auto-retry

```python
import json
from pydantic import BaseModel, field_validator, ValidationError
import anthropic

client = anthropic.Anthropic()

class ProductExtraction(BaseModel):
    model_config = {"extra": "forbid"}

    name:       str
    price_usd:  float
    category:   str
    in_stock:   bool
    tags:       list[str] = []

    @field_validator("price_usd")
    @classmethod
    def price_must_be_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError(f"price_usd must be positive, got {v}")
        return round(v, 2)

    @field_validator("name", "category")
    @classmethod
    def must_not_be_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Field must not be empty")
        return v.strip()

SYSTEM = """Extract product data as JSON with fields: name, price_usd, category, in_stock, tags.
Return ONLY valid JSON. No markdown."""

def extract_validated(text: str, max_attempts: int = 3) -> ProductExtraction:
    last_error: Exception | None = None

    for attempt in range(max_attempts):
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            system=SYSTEM,
            messages=[{"role": "user", "content": text}],
        )
        raw = response.content[0].text.strip().lstrip("```json").rstrip("```").strip()

        try:
            data = json.loads(raw)
            return ProductExtraction(**data)
        except json.JSONDecodeError as e:
            last_error = e
            print(f"[validate] attempt {attempt + 1}: JSON error: {e}")
        except ValidationError as e:
            last_error = e
            print(f"[validate] attempt {attempt + 1}: schema error: {e.errors()}")

    raise ValueError(f"Validation failed after {max_attempts} attempts: {last_error}")

inputs = [
    "Red running shoes by Nike, $89.95, available, tags: sports, footwear.",
    "Blue denim jacket, $0 — free gift, category: clothing.",  # price validation
    "Wireless mouse, $29.99, out of stock.",
]
for text in inputs:
    try:
        product = extract_validated(text)
        print(f"OK: {product.model_dump()}")
    except ValueError as e:
        print(f"FAILED: {e}")
```

**Expected Token Savings:** Pydantic catches type coercion issues (string "29.99" vs. float) and constraint violations that JSON parsing misses; reduces silent downstream failures.
**Environment:** Any extraction pipeline; Pydantic is the standard for production Python data validation.

---

### Option 3 — JSON Schema validation without Pydantic

```python
import json
import jsonschema
import anthropic

client = anthropic.Anthropic()

ORDER_SCHEMA = {
    "type": "object",
    "required": ["order_id", "status", "items", "total_usd"],
    "additionalProperties": False,
    "properties": {
        "order_id":  {"type": "string",  "pattern": "^ORD-[0-9]+$"},
        "status":    {"type": "string",  "enum": ["pending", "processing", "shipped", "delivered", "cancelled"]},
        "items":     {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["sku", "qty", "unit_price"],
                "properties": {
                    "sku":        {"type": "string"},
                    "qty":        {"type": "integer", "minimum": 1},
                    "unit_price": {"type": "number",  "minimum": 0},
                },
            },
        },
        "total_usd": {"type": "number", "minimum": 0},
    },
}

SYSTEM = f"""Parse order information into JSON matching this schema:
{json.dumps(ORDER_SCHEMA, indent=2)}
Return ONLY valid JSON."""

def parse_order(text: str, max_attempts: int = 3) -> dict:
    for attempt in range(max_attempts):
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=SYSTEM,
            messages=[{"role": "user", "content": text}],
        )
        raw = response.content[0].text.strip().lstrip("```json").rstrip("```").strip()

        try:
            data = json.loads(raw)
            jsonschema.validate(data, ORDER_SCHEMA)
            return data
        except json.JSONDecodeError as e:
            print(f"[validate] attempt {attempt + 1}: JSON error: {e}")
        except jsonschema.ValidationError as e:
            print(f"[validate] attempt {attempt + 1}: schema error: {e.message}")

    raise ValueError(f"Could not parse valid order after {max_attempts} attempts")

order_text = "Order ORD-1042 for 2x SKU-ABC at $19.99 each and 1x SKU-XYZ at $5.00. Status: shipped."
result = parse_order(order_text)
print(json.dumps(result, indent=2))
```

**Expected Token Savings:** JSON Schema validates structure, enum values, patterns, and numeric bounds — catching errors that require a downstream database write to discover otherwise.
**Environment:** Teams that prefer JSON Schema over Pydantic; useful when sharing the schema with non-Python services.

---

### Option 4 — Type coercion layer: fix common model mistakes automatically

```python
import json
import re
import anthropic

client = anthropic.Anthropic()

def coerce_output(raw: str, expected_types: dict[str, type]) -> dict:
    """
    Parse JSON and attempt safe type coercion for common model mistakes
    before raising a validation error.
    """
    # Strip markdown fences
    raw = re.sub(r"^```[a-z]*\n?", "", raw.strip())
    raw = re.sub(r"\n?```$", "", raw)

    data = json.loads(raw)

    for field, target_type in expected_types.items():
        if field not in data:
            continue
        value = data[field]
        if not isinstance(value, target_type):
            try:
                if target_type is bool:
                    # "true"/"yes"/"1" → True
                    data[field] = str(value).lower() in {"true", "yes", "1"}
                elif target_type is float:
                    # "$29.99" → 29.99
                    data[field] = float(re.sub(r"[^\d.]", "", str(value)))
                elif target_type is int:
                    data[field] = int(float(str(value)))
                elif target_type is list and isinstance(value, str):
                    # "a, b, c" → ["a", "b", "c"]
                    data[field] = [s.strip() for s in value.split(",")]
                else:
                    data[field] = target_type(value)
                print(f"[coerce] {field}: {value!r} → {data[field]!r}")
            except (ValueError, TypeError) as e:
                raise ValueError(f"Cannot coerce {field}={value!r} to {target_type.__name__}: {e}")
    return data

EXPECTED = {"name": str, "price_usd": float, "in_stock": bool, "rating": float, "tags": list}
SYSTEM    = "Extract product info as JSON: name, price_usd, in_stock, rating (0-5), tags."

def extract(text: str) -> dict:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=SYSTEM,
        messages=[{"role": "user", "content": text}],
    )
    return coerce_output(response.content[0].text, EXPECTED)

# Model might return price as "$45.00" or in_stock as "yes" — coercion handles it
result = extract("Premium coffee grinder, price $45.00, available, rating 4.7, tags: kitchen, appliance")
print(result)
```

**Expected Token Savings:** Coercion fixes common model formatting habits without an extra API call; saves 1–2 retry turns per affected request.
**Environment:** Extraction pipelines where retrying is expensive; accept minor format variations rather than failing.

---

### Option 5 — Structured output via tool_use to force schema compliance

```python
import json
import anthropic

client = anthropic.Anthropic()

# Define output schema as a tool — the model must call it to respond
OUTPUT_TOOL = {
    "name": "return_analysis",
    "description": "Return the structured sentiment analysis result.",
    "input_schema": {
        "type": "object",
        "required": ["sentiment", "confidence", "key_phrases", "action_required"],
        "properties": {
            "sentiment":        {"type": "string", "enum": ["positive", "neutral", "negative"]},
            "confidence":       {"type": "number",  "minimum": 0.0, "maximum": 1.0},
            "key_phrases":      {"type": "array",   "items": {"type": "string"}, "maxItems": 5},
            "action_required":  {"type": "boolean"},
        },
    },
}

def analyse_sentiment(text: str) -> dict:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        tools=[OUTPUT_TOOL],
        tool_choice={"type": "any"},   # force tool use
        messages=[
            {"role": "user", "content": f"Analyse the sentiment of this text:\n\n{text}"}
        ],
    )
    tool_use = next((b for b in response.content if b.type == "tool_use"), None)
    if tool_use is None:
        raise ValueError("Model did not call the output tool")
    return tool_use.input   # already validated against the JSON Schema by the SDK

texts = [
    "I absolutely love this product! Best purchase I've made all year.",
    "The package arrived late and the item was damaged. Very disappointed.",
    "It works as described. Delivery was on time.",
    "URGENT: System is down and customers cannot log in!!!",
]
for text in texts:
    result = analyse_sentiment(text)
    print(f"Text: {text[:60]!r}")
    print(f"Result: {result}\n")
```

**Expected Token Savings:** Tool-use forces schema compliance at the API level — no retry needed for format errors; saves 300–800 tokens per malformed response.
**Environment:** The preferred pattern for any structured output task; tool_use JSON Schema validation is more reliable than prose instructions.

---

### Option 6 — Schema validation with structured error feedback for self-correction

```python
import json
from pydantic import BaseModel, ValidationError
import anthropic

client = anthropic.Anthropic()

class ReportSchema(BaseModel):
    title:       str
    summary:     str
    findings:    list[str]
    risk_level:  str   # "low" | "medium" | "high"
    recommended_action: str

GENERATE_SYSTEM = "Generate a security audit report as a JSON object. Return ONLY valid JSON."

CORRECT_SYSTEM = """The previous JSON output failed schema validation. Fix ONLY the fields listed in the error.
Return the corrected complete JSON object. No markdown, no prose."""

def generate_with_self_correction(prompt: str, max_attempts: int = 3) -> ReportSchema:
    messages = [{"role": "user", "content": prompt}]
    last_error_str = ""

    for attempt in range(max_attempts):
        system = GENERATE_SYSTEM if attempt == 0 else CORRECT_SYSTEM

        if attempt > 0 and last_error_str:
            # Append error context for self-correction
            messages.append({"role": "user", "content": f"Validation error: {last_error_str}\n\nPlease fix and return the complete corrected JSON."})

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=system,
            messages=messages,
        )
        raw  = response.content[0].text.strip().lstrip("```json").rstrip("```").strip()
        reply_text = response.content[0].text

        try:
            data = json.loads(raw)
            return ReportSchema(**data)
        except json.JSONDecodeError as e:
            last_error_str = f"Invalid JSON: {e}"
            print(f"[validate] attempt {attempt + 1}: {last_error_str}")
        except ValidationError as e:
            last_error_str = "; ".join(f"{err['loc'][0]}: {err['msg']}" for err in e.errors())
            print(f"[validate] attempt {attempt + 1}: {last_error_str}")

        messages.append({"role": "assistant", "content": reply_text})

    raise ValueError(f"Could not produce valid output after {max_attempts} attempts")

report = generate_with_self_correction(
    "Audit the authentication system of a web application that stores passwords in plain text."
)
print(f"Title:    {report.title}")
print(f"Risk:     {report.risk_level}")
print(f"Summary:  {report.summary[:100]}")
print(f"Findings: {report.findings[:2]}")
```

**Expected Token Savings:** Self-correction uses the existing context window and costs one extra Haiku call (~200 tokens) rather than restarting the full pipeline.
**Environment:** Complex generation tasks where output validity is critical and retry cost must be minimised.

---

## Comparison

| Option | Validation Method | Auto-Retry | Type Coercion | Best For |
|---|---|---|---|---|
| 1. Key presence check | Manual field check | Yes | No | Simple extraction, quick start |
| 2. Pydantic | Data model + validators | Yes | Partial | Python-native, production pipelines |
| 3. JSON Schema | jsonschema library | Yes | No | Language-agnostic, shared schemas |
| 4. Coercion layer | Custom type casting | No | Yes | Tolerating minor model format habits |
| 5. Tool-use schema | API-enforced | No | No | Most reliable — preferred for new agents |
| 6. Self-correction | Pydantic + feedback | Yes | No | Complex generation with critical validity |
