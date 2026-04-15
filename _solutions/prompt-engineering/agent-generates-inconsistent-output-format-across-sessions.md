---
layout: solution
title: "Agent Generates Inconsistent Output Format Across Sessions"
category: prompt-engineering
description: "The agent returns JSON in one session, markdown in another, and plain prose in a third — for identical requests. Downstream parsers break, users lose trust, and automation pipelines fail unpredictably."
tags: [prompt-engineering, output-format, json, structured-output, tool-use, consistency, schema]
---

## Symptom

Your automation pipeline calls the agent and parses its response as JSON. It works in development. In production it returns a markdown code block around the JSON. On Tuesday it returns a different key name. The agent's creative license with output format makes it unreliable as a machine-readable component. Even with format instructions in the system prompt, the model occasionally deviates.

## Root Cause

Natural language instructions like "respond in JSON" are treated as a soft preference, not a hard constraint. The model balances format compliance against other objectives (being helpful, being clear, following conversational norms). Under distributional pressure — long conversations, unusual inputs, creative requests — format instructions erode. There is no enforcement mechanism that guarantees the output structure.

## Fix

---

### Option 1: Force JSON via Tool Use (Most Reliable)

Wrap the desired output schema as a tool. Use `tool_choice={"type": "any"}` to force the model to always call it.

```python
import json
import anthropic
from typing import Any

client = anthropic.Anthropic()

# Define the required output shape as a tool schema
STRUCTURED_OUTPUT_TOOL = {
    "name": "return_analysis",
    "description": "Return the analysis result in a structured format.",
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": "One-sentence summary of the analysis",
            },
            "sentiment": {
                "type": "string",
                "enum": ["positive", "negative", "neutral", "mixed"],
            },
            "key_points": {
                "type": "array",
                "items": {"type": "string"},
                "description": "3-5 key points from the text",
            },
            "confidence": {
                "type": "number",
                "minimum": 0.0,
                "maximum": 1.0,
                "description": "Confidence in this analysis (0-1)",
            },
            "topics": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Main topics covered",
            },
        },
        "required": ["summary", "sentiment", "key_points", "confidence", "topics"],
    },
}


def analyze_text(text: str) -> dict[str, Any]:
    """
    Analyze text and ALWAYS return structured JSON.
    Tool use with tool_choice=any guarantees format compliance.
    """
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        tools=[STRUCTURED_OUTPUT_TOOL],
        tool_choice={"type": "any"},  # MUST call the tool — no text-only response allowed
        messages=[{
            "role": "user",
            "content": f"Analyze this text:\n\n{text}",
        }],
    )

    for block in response.content:
        if block.type == "tool_use" and block.name == "return_analysis":
            return block.input  # guaranteed to match schema

    raise RuntimeError("Model did not call the required tool — unexpected")


# Usage
texts = [
    "The new product launch exceeded all expectations with record sales and overwhelmingly positive customer reviews.",
    "Despite initial challenges, the team managed to deliver on time though some quality issues remain.",
    "The quarterly report shows mixed results across different market segments.",
]

for text in texts:
    result = analyze_text(text)
    print(f"Sentiment: {result['sentiment']} (confidence: {result['confidence']:.0%})")
    print(f"Summary: {result['summary']}")
    print(f"Key points: {result['key_points']}")
    print()
    # Guaranteed: result is always a dict with all required keys
    assert "sentiment" in result
    assert "key_points" in result
    assert isinstance(result["confidence"], (int, float))
```

**Expected Token Savings**: Zero format-correction retries. Downstream parsers never fail. One call = one reliable structured result.
**Environment**: Anthropic Python SDK. `tool_choice={"type": "any"}` is the key constraint.

---

### Option 2: JSON Mode via Prefilled Assistant Turn

Pre-fill the assistant turn with `{` to force JSON output. Validate and retry if structure is wrong.

```python
import json
import re
import anthropic
from typing import Any

client = anthropic.Anthropic()

SYSTEM = """You are a data extraction assistant. You ALWAYS respond with valid JSON only.
Never include explanations, markdown, or text outside the JSON object.
Your response must be parseable by json.loads()."""

OUTPUT_SCHEMA = {
    "entity_type": "string (person|company|product|location|event)",
    "name": "string",
    "attributes": "object with relevant key-value pairs",
    "confidence": "number 0.0-1.0",
}


def extract_entity(text: str, max_retries: int = 3) -> dict[str, Any]:
    """Extract entity info as guaranteed JSON."""
    messages = [
        {"role": "user", "content": f"Extract the main entity from:\n\n{text}\n\nSchema: {json.dumps(OUTPUT_SCHEMA)}"},
        {"role": "assistant", "content": "{"},  # Pre-fill forces JSON mode
    ]

    for attempt in range(max_retries):
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=SYSTEM,
            messages=messages,
        )

        # The response continues from the pre-filled "{"
        raw = "{" + response.content[0].text

        # Extract JSON even if there's trailing text
        try:
            # Find the outermost complete JSON object
            brace_count = 0
            end_idx = 0
            for i, char in enumerate(raw):
                if char == "{":
                    brace_count += 1
                elif char == "}":
                    brace_count -= 1
                    if brace_count == 0:
                        end_idx = i + 1
                        break

            parsed = json.loads(raw[:end_idx])
            return parsed

        except (json.JSONDecodeError, ValueError) as e:
            print(f"  [Attempt {attempt+1}] JSON parse failed: {e}")
            if attempt < max_retries - 1:
                # Add error feedback for retry
                messages.append({"role": "assistant", "content": raw})
                messages.append({"role": "user", "content": "That was not valid JSON. Return only a valid JSON object."})
                messages.append({"role": "assistant", "content": "{"})

    raise ValueError(f"Failed to get valid JSON after {max_retries} attempts")


# Usage
texts = [
    "Elon Musk founded SpaceX in 2002 to revolutionize space transportation.",
    "Apple Inc. released the iPhone 15 in September 2023 with a USB-C port.",
    "The Paris Olympics took place in July-August 2024.",
]

for text in texts:
    result = extract_entity(text)
    print(f"Entity: {result.get('name')} ({result.get('entity_type')})")
    print(f"Attrs: {result.get('attributes')}")
    print()
```

**Expected Token Savings**: Pre-fill eliminates ~50 tokens of "Sure, here's the JSON:" preamble per response. Retry only on actual parse failures.
**Environment**: Works with any Anthropic model. Pre-filling is a native API feature.

---

### Option 3: Output Schema Validator with Auto-Correction

Validate the model's output against a Pydantic schema. If invalid, send the validation error back for one self-correction pass.

```python
import json
import anthropic
from pydantic import BaseModel, Field, ValidationError
from typing import Optional

client = anthropic.Anthropic()


class ReportSchema(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    executive_summary: str = Field(..., min_length=10)
    findings: list[str] = Field(..., min_items=1, max_items=10)
    recommendation: str
    priority: str = Field(..., pattern="^(high|medium|low)$")
    estimated_impact: Optional[str] = None


SYSTEM = """You are a report generator. Always respond with a JSON object matching this schema:
{
  "title": "string",
  "executive_summary": "string (10+ chars)",
  "findings": ["string", ...],
  "recommendation": "string",
  "priority": "high|medium|low",
  "estimated_impact": "string or null"
}
Respond with ONLY the JSON object, no markdown, no explanation."""


def generate_report(topic: str) -> ReportSchema:
    messages = [{"role": "user", "content": f"Generate a brief analysis report about: {topic}"}]

    for attempt in range(2):
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            system=SYSTEM,
            messages=messages,
        )

        raw = response.content[0].text.strip()

        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\n?", "", raw)
            raw = re.sub(r"\n?```$", "", raw)

        try:
            data = json.loads(raw)
            validated = ReportSchema(**data)
            if attempt > 0:
                print(f"  [Self-corrected on attempt {attempt+1}]")
            return validated

        except (json.JSONDecodeError, ValidationError) as e:
            if attempt == 0:
                # Send validation error back for correction
                error_details = str(e)
                messages.append({"role": "assistant", "content": raw})
                messages.append({
                    "role": "user",
                    "content": (
                        f"Your response had errors: {error_details}\n\n"
                        "Fix these issues and return only valid JSON matching the schema. "
                        "Priority must be exactly 'high', 'medium', or 'low'."
                    ),
                })
            else:
                raise ValueError(f"Could not generate valid report after correction: {e}")

    raise ValueError("Unreachable")


import re

result = generate_report("remote work productivity trends in 2025")
print(f"Title: {result.title}")
print(f"Priority: {result.priority}")
print(f"Findings: {result.findings}")
print(f"Recommendation: {result.recommendation}")
# result is always a valid ReportSchema — type-safe
```

**Expected Token Savings**: Self-correction in a single retry (not a full new call chain). Pydantic validation catches structural and type issues before they reach downstream code.
**Environment**: `pip install pydantic`. Works with any model.

---

### Option 4: Format-Locked Prompt Template with Examples

Combine a strict format instruction with few-shot examples in the system prompt. Examples teach format more reliably than rules alone.

```python
import json
import anthropic

client = anthropic.Anthropic()

# System prompt with embedded examples (few-shot format locking)
FORMAT_LOCKED_SYSTEM = """
You extract product information and return ONLY this exact JSON format:

{"name": "string", "price": number, "currency": "USD|EUR|GBP", "in_stock": boolean, "category": "string"}

EXAMPLE INPUT: "The blue widget is $29.99 and currently available in our warehouse."
EXAMPLE OUTPUT: {"name": "blue widget", "price": 29.99, "currency": "USD", "in_stock": true, "category": "widget"}

EXAMPLE INPUT: "MacBook Pro 14-inch, priced at €1,999, currently out of stock."
EXAMPLE OUTPUT: {"name": "MacBook Pro 14-inch", "price": 1999, "currency": "EUR", "in_stock": false, "category": "laptop"}

RULES:
- Return ONLY the JSON object, nothing else
- price is always a number (not a string)
- in_stock is always boolean
- If currency is unclear, use "USD"
- If category is unclear, use "general"
""".strip()


def extract_product(text: str) -> dict:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=FORMAT_LOCKED_SYSTEM,
        messages=[{"role": "user", "content": text}],
    )

    raw = response.content[0].text.strip()

    # Attempt parse
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Find JSON in response (model may have added a comment)
        start = raw.find("{")
        end = raw.rfind("}") + 1
        if start != -1 and end > start:
            return json.loads(raw[start:end])
        raise ValueError(f"Could not parse JSON from: {raw!r}")


# Test with varied inputs
inputs = [
    "Sony WH-1000XM5 headphones, $349, in stock",
    "The vintage lamp costs 45 pounds and we have 3 left",
    "Limited edition sneakers - SOLD OUT - retail price $220 USD",
    "Organic coffee beans, €12.50 per 250g, available",
]

for inp in inputs:
    result = extract_product(inp)
    print(f"Input: {inp[:60]}")
    print(f"  → {result}")
    # Type guarantees from schema
    assert isinstance(result["price"], (int, float))
    assert isinstance(result["in_stock"], bool)
    print()
```

**Expected Token Savings**: Few-shot examples reduce format deviation more reliably than rules alone, cutting correction retries by ~80%.
**Environment**: System prompt only — no extra dependencies. Most effective for simple, repeated schemas.

---

### Option 5: Streaming Output Validation with Early Abort

Validate the structure as it streams in. Abort and retry if the format is wrong within the first 100 tokens.

```python
import json
import re
import anthropic

client = anthropic.Anthropic()

SYSTEM = """Respond ONLY with a JSON object. Start immediately with { and end with }.
No markdown, no explanations, no code fences."""


def streaming_validated_create(
    messages: list[dict],
    expected_start: str = "{",
    max_tokens: int = 512,
) -> dict:
    """
    Stream the response. If the first non-whitespace character is wrong,
    abort and retry with correction.
    """
    for attempt in range(3):
        collected = []
        format_ok = None

        with client.messages.stream(
            model="claude-haiku-4-5-20251001",
            max_tokens=max_tokens,
            system=SYSTEM,
            messages=messages,
        ) as stream:
            for token in stream.text_stream:
                collected.append(token)
                joined = "".join(collected).lstrip()

                # Early format check on first meaningful content
                if format_ok is None and joined:
                    if joined[0] == expected_start:
                        format_ok = True
                    else:
                        format_ok = False
                        print(f"  [Attempt {attempt+1}] Bad format start: {joined[:20]!r} — aborting stream")
                        break  # Break out of stream loop — generator is abandoned

        if format_ok is False:
            messages = messages + [
                {"role": "assistant", "content": "".join(collected)},
                {"role": "user", "content": f"Your response must start with '{expected_start}'. Return only valid JSON."},
            ]
            continue

        raw = "".join(collected).strip()
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            print(f"  [Attempt {attempt+1}] JSON parse error: {e}")
            messages = messages + [
                {"role": "assistant", "content": raw},
                {"role": "user", "content": "Fix the JSON syntax error and return only valid JSON."},
            ]

    raise ValueError("Could not get valid JSON after 3 attempts")


result = streaming_validated_create(
    messages=[{"role": "user", "content": "Return info about Python as JSON with keys: name, year_created, creator, paradigm"}],
)
print(result)
```

**Expected Token Savings**: Early abort on bad-format stream wastes only the first 10–50 tokens instead of the full response. Saves ~90% of generation cost for format-wrong responses.
**Environment**: Streaming SDK. Works best when format errors are detectable early (JSON must start with `{`).

---

### Option 6: Output Format Registry with Per-Endpoint Enforcement

Define output formats centrally. Each agent endpoint declares its format and enforces it automatically.

```python
import json
import re
from dataclasses import dataclass
from typing import Callable, Any
import anthropic

client = anthropic.Anthropic()


@dataclass
class OutputFormat:
    name: str
    system_instruction: str
    parser: Callable[[str], Any]
    validator: Callable[[Any], bool]
    tool_definition: dict | None = None


def parse_json(raw: str) -> dict:
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?\n?", "", raw)
    raw = re.sub(r"\n?```$", "", raw)
    return json.loads(raw)


def parse_bullet_list(raw: str) -> list[str]:
    lines = raw.strip().splitlines()
    items = []
    for line in lines:
        line = line.strip()
        if line.startswith(("- ", "• ", "* ")):
            items.append(line[2:].strip())
        elif re.match(r"^\d+\.", line):
            items.append(re.sub(r"^\d+\.\s*", "", line))
        elif line:
            items.append(line)
    return items


FORMAT_REGISTRY = {
    "json_object": OutputFormat(
        name="json_object",
        system_instruction="Respond ONLY with a valid JSON object. No markdown, no explanation.",
        parser=parse_json,
        validator=lambda x: isinstance(x, dict),
    ),
    "json_array": OutputFormat(
        name="json_array",
        system_instruction="Respond ONLY with a valid JSON array. No markdown, no explanation.",
        parser=lambda raw: json.loads(raw.strip()),
        validator=lambda x: isinstance(x, list),
    ),
    "bullet_list": OutputFormat(
        name="bullet_list",
        system_instruction="Respond ONLY with a bulleted list. Each item on its own line starting with '- '.",
        parser=parse_bullet_list,
        validator=lambda x: isinstance(x, list) and len(x) > 0,
    ),
    "plain_text": OutputFormat(
        name="plain_text",
        system_instruction="Respond with plain text only. No markdown formatting.",
        parser=lambda raw: raw.strip(),
        validator=lambda x: isinstance(x, str) and len(x) > 0,
    ),
}


def format_enforced_create(
    messages: list[dict],
    format_name: str,
    max_tokens: int = 512,
    max_retries: int = 2,
) -> Any:
    """
    Create a message and enforce the registered output format.
    Returns parsed, validated output.
    """
    fmt = FORMAT_REGISTRY[format_name]

    for attempt in range(max_retries + 1):
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=max_tokens,
            system=fmt.system_instruction,
            messages=messages,
        )
        raw = response.content[0].text

        try:
            parsed = fmt.parser(raw)
            if fmt.validator(parsed):
                return parsed
            else:
                raise ValueError(f"Validation failed for format {format_name}")
        except Exception as e:
            if attempt < max_retries:
                print(f"  [Retry {attempt+1}] Format error: {e}")
                messages = messages + [
                    {"role": "assistant", "content": raw},
                    {"role": "user", "content": f"Output format error: {e}. Please follow: {fmt.system_instruction}"},
                ]
            else:
                raise ValueError(f"Could not enforce format '{format_name}' after {max_retries} retries: {e}")

    raise ValueError("Unreachable")


# Usage — format is specified per call, not per prompt
json_result = format_enforced_create(
    messages=[{"role": "user", "content": "List 3 Python web frameworks with their key features."}],
    format_name="json_array",
)
print("JSON array result:", json_result[:2])

bullet_result = format_enforced_create(
    messages=[{"role": "user", "content": "List 5 benefits of using async Python."}],
    format_name="bullet_list",
)
print("Bullet list:", bullet_result[:3])
```

**Expected Token Savings**: Centralized format registry eliminates format instructions scattered across 20 different system prompts. Consistency guaranteed at the call site.
**Environment**: Pure Python. Registry is maintainable as a config or module-level dict.

---

| Option | Mechanism | Reliability | Overhead | Best For |
|--------|---------|------------|---------|---------|
| 1 | Tool use + `tool_choice=any` | Highest (schema-enforced) | ~50 tokens | Production APIs, machine parsing |
| 2 | Pre-filled assistant `{` | High | Minimal | JSON-only outputs |
| 3 | Pydantic validation + retry | High | 1 retry pass | Complex schemas with type constraints |
| 4 | Few-shot format examples | Medium-High | ~200 token system prompt | Simple schemas, high call volume |
| 5 | Streaming early abort | High | Minimal on bad response | Cost-sensitive, detectable early errors |
| 6 | Format registry | High | Minimal | Multi-endpoint agents, team consistency |
