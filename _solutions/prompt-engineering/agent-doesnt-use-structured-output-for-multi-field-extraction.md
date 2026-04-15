---
layout: solution
title: "Agent Doesn't Use Structured Output for Multi-Field Extraction"
category: prompt-engineering
description: "Agent returns prose or inconsistently formatted text when extracting multiple fields, requiring fragile post-processing and losing data when fields are missing or merged."
tags: [prompt-engineering, structured-output, json, extraction, parsing, reliability]
---

## Symptom

Agent returns free-form text for multi-field extraction tasks:

```
Prompt: "Extract name, email, company, and role from this business card text"
Input: "John Smith | CEO at Acme Corp | john@acme.com | Founded 2019"

Agent returns:
"The person's name is John Smith. He is the CEO of Acme Corp.
His email address is john@acme.com. The company was founded in 2019."

Problems:
- "role" and "title" are mixed into prose — parser must guess which sentence is which field
- "founded" is extracted but wasn't asked for
- "role" is embedded in "He is the CEO" — regex will break on "She is the VP"
- Next input returns fields in different order — downstream pipeline breaks
```

Downstream code then uses fragile regex or string splitting that breaks on format variations, missing fields, or unexpected extra information.

## Root Cause

Without explicit instructions to return structured output, LLMs default to natural language — the dominant pattern in their training data. Multi-field extraction is semantically identical to question-answering, and the model produces a conversational answer. Even when the user says "extract X, Y, Z", the model interprets this as "answer a question about X, Y, Z" rather than "populate a data structure."

## Fix

---

### Option 1: Explicit JSON Schema in System Prompt with Prefill

Specify the exact JSON output schema and use assistant prefill to force the response to start with `{`, preventing prose preamble.

```python
import json
import anthropic

client = anthropic.Anthropic()

EXTRACTION_SCHEMA = {
    "name": "string | null",
    "email": "string | null",
    "company": "string | null",
    "role": "string | null",
    "phone": "string | null",
}

def extract_contact(text: str) -> dict:
    schema_str = json.dumps(EXTRACTION_SCHEMA, indent=2)
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=(
            f"Extract contact information. Return ONLY a JSON object matching this schema:\n"
            f"{schema_str}\n\n"
            "Rules:\n"
            "- Use null for missing fields (never omit a field)\n"
            "- Return raw JSON only — no explanation, no markdown code block\n"
            "- Do not infer or guess values not present in the input"
        ),
        messages=[
            {"role": "user", "content": text},
            # Prefill: forces response to start with JSON, eliminates prose preamble
            {"role": "assistant", "content": "{"},
        ],
    )
    raw = "{" + response.content[0].text
    # Strip accidental trailing text after closing brace
    raw = raw[:raw.rfind("}") + 1]
    return json.loads(raw)

# Test cases
inputs = [
    "John Smith | CEO at Acme Corp | john@acme.com | +1-555-0100",
    "Sarah Chen, Product Manager, sarah.chen@startup.io",
    "Just a name: Bob",
    "contact@example.com",
]

for text in inputs:
    result = extract_contact(text)
    print(f"Input: {text!r}")
    print(f"Output: {result}\n")
```

**Expected Token Savings:** Prefill eliminates preamble text (~30-80 tokens per response). JSON output is more token-efficient than prose for the same information. Saves downstream parsing retries (each ~400 tokens) when prose format is ambiguous.
**Environment:** Works with any Claude model. Prefill is Claude-specific — OpenAI uses `response_format={"type": "json_object"}` instead. Always validate JSON with `json.loads()` and handle `json.JSONDecodeError`.

---

### Option 2: Tool Use as Structured Output — Define Fields as Tool Parameters

Use a tool definition to enforce the output schema. The model fills tool parameters instead of generating free text, and the SDK validates types automatically.

```python
import json
import anthropic

client = anthropic.Anthropic()

EXTRACT_TOOL = {
    "name": "extract_contact",
    "description": "Extract structured contact information from text",
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {
                "type": ["string", "null"],
                "description": "Full name of the person",
            },
            "email": {
                "type": ["string", "null"],
                "description": "Email address",
            },
            "company": {
                "type": ["string", "null"],
                "description": "Company or organisation name",
            },
            "role": {
                "type": ["string", "null"],
                "description": "Job title or role",
            },
            "phone": {
                "type": ["string", "null"],
                "description": "Phone number in original format",
            },
        },
        "required": ["name", "email", "company", "role", "phone"],
    },
}

def extract_with_tool(text: str) -> dict:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        tools=[EXTRACT_TOOL],
        tool_choice={"type": "tool", "name": "extract_contact"},  # force this tool
        messages=[{"role": "user", "content": f"Extract contact info from: {text}"}],
    )
    tool_use = next(b for b in response.content if b.type == "tool_use")
    return tool_use.input  # already a parsed dict, schema-validated by the SDK

# Test
samples = [
    "Dr. Emily Rodriguez, Chief Medical Officer | emily.rodriguez@medtech.com | MedTech Inc | (415) 555-0199",
    "Alex Kim — alex@freelance.dev",
    "Headquarters: contact@bigcorp.com",
]

for sample in samples:
    result = extract_with_tool(sample)
    print(json.dumps(result, indent=2))
    print()
```

**Expected Token Savings:** Tool use eliminates JSON parsing failures entirely — SDK handles schema validation. No need for retry on parse error (~400 tokens each). `tool_choice={"type": "tool"}` forces structured output, preventing the model from returning prose even on unusual inputs.
**Environment:** Slightly higher latency than direct JSON output due to tool use overhead. `tool_choice` with a named tool guarantees the tool is called — don't omit it or the model may return text instead.

---

### Option 3: Pydantic Schema Generation — Derive Prompt from Data Model

Define your data model as a Pydantic class and automatically generate both the extraction prompt and the output parser from the schema.

```python
import json
import anthropic
from pydantic import BaseModel, Field, field_validator
from typing import Any

class ContactInfo(BaseModel):
    name: str | None = Field(None, description="Full name")
    email: str | None = Field(None, description="Email address")
    company: str | None = Field(None, description="Company name")
    role: str | None = Field(None, description="Job title")
    phone: str | None = Field(None, description="Phone number")
    linkedin: str | None = Field(None, description="LinkedIn URL or handle")

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: str | None) -> str | None:
        if v and "@" not in v:
            return None  # reject invalid emails silently
        return v

def schema_to_prompt(model: type[BaseModel]) -> str:
    """Generate extraction instructions from Pydantic model field definitions."""
    schema = model.model_json_schema()
    fields = schema.get("properties", {})
    lines = ["Extract the following fields (use null if not found):"]
    for field_name, field_def in fields.items():
        desc = field_def.get("description", field_name)
        types = field_def.get("type", "string")
        lines.append(f"  - {field_name}: {desc} ({types})")
    lines.append("\nReturn ONLY a JSON object with exactly these fields.")
    return "\n".join(lines)

def extract_structured(text: str, model: type[BaseModel]) -> BaseModel:
    prompt = schema_to_prompt(model)
    client = anthropic.Anthropic()

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=prompt,
        messages=[
            {"role": "user", "content": text},
            {"role": "assistant", "content": "{"},
        ],
    )
    raw = "{" + response.content[0].text
    raw = raw[:raw.rfind("}") + 1]
    data = json.loads(raw)
    return model(**data)

# Extract and validate
contact = extract_structured(
    "Jane Doe, VP Engineering | jane@example.com | ExampleCorp | linkedin.com/in/janedoe",
    ContactInfo,
)
print(contact.model_dump_json(indent=2))

# Extend to any model — zero extra prompt engineering
class ProductInfo(BaseModel):
    name: str | None = Field(None, description="Product name")
    price: float | None = Field(None, description="Price in USD")
    sku: str | None = Field(None, description="SKU or product code")
    in_stock: bool | None = Field(None, description="Whether item is in stock")

product = extract_structured(
    "Widget Pro X200 — SKU: WX-200 — $49.99 — In Stock",
    ProductInfo,
)
print(product.model_dump_json(indent=2))
```

**Expected Token Savings:** Schema-driven prompt generation is reusable across all extraction tasks — no per-task prompt engineering. Pydantic validation catches type mismatches silently (returns None) rather than propagating wrong data. Saves 1-2 correction turns per extraction job.
**Environment:** Works for any structured extraction task. Field descriptions become the extraction instructions — invest in clear descriptions for best results. Combine with Option 2 (tool use) for the most robust output.

---

### Option 4: Multi-Step Extraction — Identify Then Extract

For complex or long documents, split extraction into two steps: first identify all relevant spans, then extract values from those spans. Reduces hallucination on sparse documents.

```python
import json
import anthropic

client = anthropic.Anthropic()

def extract_in_two_steps(document: str, fields: list[str]) -> dict:
    """Step 1: Find relevant spans. Step 2: Extract clean values."""

    # Step 1: Locate spans (cheap model, small output)
    fields_list = ", ".join(fields)
    locate_response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=(
            f"Locate text spans in the document that contain: {fields_list}.\n"
            "Return a JSON object: {field_name: 'exact quoted text span | null'}\n"
            "Quote the EXACT text from the document. Use null if not found."
        ),
        messages=[
            {"role": "user", "content": document},
            {"role": "assistant", "content": "{"},
        ],
    )
    raw_spans = "{" + locate_response.content[0].text
    raw_spans = raw_spans[:raw_spans.rfind("}") + 1]
    spans = json.loads(raw_spans)

    # Step 2: Normalise each span to a clean value (only process found spans)
    results: dict[str, str | None] = {f: None for f in fields}
    found_spans = {k: v for k, v in spans.items() if v is not None}

    if not found_spans:
        return results

    normalise_response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=(
            "Normalise these raw text spans into clean values.\n"
            "Return JSON: {field: clean_value}\n"
            "Rules: emails → lowercase, phones → digits+hyphens, names → Title Case"
        ),
        messages=[
            {"role": "user", "content": json.dumps(found_spans)},
            {"role": "assistant", "content": "{"},
        ],
    )
    raw_clean = "{" + normalise_response.content[0].text
    raw_clean = raw_clean[:raw_clean.rfind("}") + 1]
    clean_values = json.loads(raw_clean)
    results.update(clean_values)
    return results

# Test on messy real-world text
doc = """
From the business card of our new partner:
  Mr. JAMES WILSON  -  james.wilson@BIGCORP.COM
  Senior Vice President, Strategic Partnerships
  BigCorp International LLC
  Mobile: +1 (415) 555.0177
"""
result = extract_in_two_steps(doc, ["name", "email", "company", "role", "phone"])
print(json.dumps(result, indent=2))
```

**Expected Token Savings:** Two-step approach costs ~2× tokens vs single-step but dramatically reduces extraction errors on long/messy documents. Each avoided correction turn: ~600 tokens. Break-even at ~2 prevented errors. For high-volume extraction pipelines, net savings are positive.
**Environment:** Best for documents where relevant information is scattered and interleaved with noise. For short, clean inputs (form submissions, structured text), single-step extraction (Options 1-3) is more efficient.

---

### Option 5: Batch Extraction — Extract Multiple Records in One Call

When processing many records of the same type, batch them into a single API call to amortise the schema instruction overhead.

```python
import json
import anthropic

client = anthropic.Anthropic()

BATCH_SYSTEM = """Extract contact information from each numbered input.
Return a JSON array where each element matches this schema:
{"name": string|null, "email": string|null, "company": string|null, "role": string|null}

Rules:
- Array must have exactly as many elements as there are numbered inputs
- Use null for any field not found in that input
- Return raw JSON array only"""

def batch_extract(texts: list[str]) -> list[dict]:
    # Number each input for tracking
    numbered = "\n".join(f"{i+1}. {t}" for i, t in enumerate(texts))

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        system=BATCH_SYSTEM,
        messages=[
            {"role": "user", "content": numbered},
            {"role": "assistant", "content": "["},
        ],
    )
    raw = "[" + response.content[0].text
    raw = raw[:raw.rfind("]") + 1]
    results = json.loads(raw)

    # Ensure result count matches input count
    if len(results) != len(texts):
        raise ValueError(f"Expected {len(texts)} results, got {len(results)}")
    return results

# 10 inputs in one API call vs 10 separate calls
contacts = [
    "Alice Johnson, CTO, alice@techco.com, TechCo",
    "Bob@sales.io — Account Executive at SalesCo",
    "Dr. Carol White | carol.white@hospital.org | Chief of Medicine",
    "david_lee@freelance.dev",
    "Emma Brown, emma@startup.co, Founder & CEO",
]

results = batch_extract(contacts)
for text, result in zip(contacts, results):
    print(f"Input: {text!r}")
    print(f"Output: {result}\n")
```

**Expected Token Savings:** System prompt overhead (~150 tokens) paid once per batch instead of per item. For 10 items: 10 × 150 = 1,500 tokens separately vs 150 + (10 × 50 per item) = 650 tokens batched. 57% reduction in overhead tokens. API call count also drops from 10 to 1.
**Environment:** Batch size limited by context window. For 100+ items, split into batches of 20-50. Verify result count matches input count — if model produces wrong count, split batch and retry individually.

---

### Option 6: Streaming Structured Output — Validate and Stream Large Extractions

For large documents where extraction may take time, stream the JSON output and validate incrementally so errors are caught early.

```python
import json
import re
import anthropic

client = anthropic.Anthropic()

def stream_structured_extraction(document: str, schema_example: dict) -> dict:
    """Stream JSON extraction and validate on completion."""
    schema_str = json.dumps(schema_example, indent=2)

    full_text = ""
    with client.messages.stream(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=(
            f"Extract data from the document. Return ONLY a JSON object matching:\n{schema_str}\n"
            "Use null for missing fields. Raw JSON only."
        ),
        messages=[
            {"role": "user", "content": document},
            {"role": "assistant", "content": "{"},
        ],
    ) as stream:
        full_text = "{"
        for text in stream.text_stream:
            full_text += text
            # Early termination: stop after closing brace
            if full_text.count("{") <= full_text.count("}"):
                break

    # Clean and validate
    full_text = full_text[:full_text.rfind("}") + 1]

    try:
        result = json.loads(full_text)
    except json.JSONDecodeError:
        # Attempt repair: find last valid JSON object
        matches = list(re.finditer(r'\{[^{}]*\}', full_text))
        if matches:
            result = json.loads(matches[-1].group())
        else:
            raise

    # Ensure all schema keys are present
    for key in schema_example:
        result.setdefault(key, None)

    return result

# Comparison table
"""
| Approach | Reliability | Token Cost | Parse Risk | Best For |
|---|---|---|---|---|
| Option 1: JSON + prefill | High | Low | Low | Simple schemas |
| Option 2: Tool use | Very High | Medium | None | Any schema |
| Option 3: Pydantic auto | High | Low | Low | Reusable models |
| Option 4: Two-step | Very High | Medium | Low | Long/messy docs |
| Option 5: Batch | High | Lowest/item | Low | High-volume |
| Option 6: Streaming | High | Low | Medium | Large documents |
"""

# Test streaming extraction
doc = """
Annual Report 2025 — Submitted by Jane Doe (jane@example.com)
CEO, Example Corp — Founded 2018 — Revenue: $12.4M (+23% YoY)
Contact for investor relations: investors@example.com | +1-800-555-0100
"""

schema = {
    "contact_name": None,
    "contact_email": None,
    "company": None,
    "founded_year": None,
    "revenue_usd_millions": None,
}

result = stream_structured_extraction(doc, schema)
print(json.dumps(result, indent=2))
```

**Expected Token Savings:** Streaming with early termination stops generation as soon as the JSON closes, avoiding trailing prose. For 512 max_tokens but 150-token JSON responses, saves ~360 tokens per call. For 1,000 extractions/day: ~360,000 tokens saved.
**Environment:** Streaming requires `with client.messages.stream()` context manager. Early termination on brace balance works for flat JSON; extend brace counting for nested schemas. Always include `setdefault(key, None)` guard for partial outputs.
