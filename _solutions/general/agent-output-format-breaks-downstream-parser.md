---
layout: solution
title: "Agent Output Format Breaks Downstream Parser — Markdown Instead of JSON"
category: general
description: "Downstream system expects JSON. Agent returns a markdown code block with JSON inside it. Parser sees triple backticks and crashes. Other times agent adds an explanation before the JSON. Sometimes it returns valid JSON, sometimes not. Output format is unpredictable."
tags: [general, output-format, json, structured-output, parser, markdown, reliability, schema]
---

# Agent Output Format Breaks Downstream Parser — Markdown Instead of JSON

## Symptom
- `json.loads()` raises `JSONDecodeError` on agent output that contains markdown fences
- Parser receives `{"result": ...}` sometimes and ` ```json\n{"result":...}\n``` ` other times
- Agent prefixes JSON with "Here is the result:" — parser chokes on the prefix text
- Integration test passes 80% of the time but fails 20% — non-deterministic output format
- Agent adds trailing comments after the closing `}` — breaks strict JSON parsers
- Downstream system works in dev but fails in prod because the model version changed formatting behavior

## Root Cause
Language models produce natural language by default. Without explicit format constraints, the model chooses how to format its output based on context — and that choice is non-deterministic. The model may wrap JSON in markdown fences because that's how JSON is displayed in documentation, add helpful text before/after the JSON, or format numbers differently. Any downstream system that parses the raw output will break when the format varies.

## Fix

### Option 1: Tool use / forced structured output

```python
import anthropic
import json

client = anthropic.Anthropic()

def get_structured_output(prompt: str, output_schema: dict) -> dict:
    """
    Use tool_choice="tool" to force the model to return structured JSON.
    The response is guaranteed to match the schema — no parsing guesswork.
    """
    response = client.messages.create(
        model="claude-sonnet-4-6",
        messages=[{"role": "user", "content": prompt}],
        tools=[{
            "name": "submit_result",
            "description": "Submit the structured result",
            "input_schema": output_schema
        }],
        tool_choice={"type": "tool", "name": "submit_result"},  # Force this tool
        max_tokens=2048
    )

    # The model MUST call submit_result — no free-form text possible
    tool_call = next(b for b in response.content if b.type == "tool_use")
    return tool_call.input  # Already a dict — no JSON parsing needed

# Define schema once, reuse everywhere:
SENTIMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "sentiment": {"type": "string", "enum": ["positive", "negative", "neutral"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "explanation": {"type": "string"},
        "keywords": {"type": "array", "items": {"type": "string"}}
    },
    "required": ["sentiment", "confidence", "explanation"]
}

result = get_structured_output(
    "Analyze sentiment: 'The product works but the support was slow'",
    SENTIMENT_SCHEMA
)
# result is always a dict with exactly the specified fields
# Never markdown, never prefixes, never trailing text
print(result["sentiment"])      # → "neutral"
print(result["confidence"])     # → 0.72
```

### Option 2: Robust JSON extraction from free-form output

```python
import json
import re

def extract_json(text: str) -> dict | list | None:
    """
    Extract JSON from model output that may contain markdown fences,
    prefixes, suffixes, or other non-JSON content.
    Tries multiple extraction strategies in order of reliability.
    """
    if not text or not text.strip():
        return None

    # Strategy 1: Try raw parse (model returned clean JSON)
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        pass

    # Strategy 2: Extract from markdown code block ```json ... ```
    fence_pattern = re.compile(
        r'```(?:json)?\s*\n?([\s\S]*?)\n?```',
        re.IGNORECASE
    )
    for match in fence_pattern.finditer(text):
        try:
            return json.loads(match.group(1).strip())
        except json.JSONDecodeError:
            continue

    # Strategy 3: Find first { ... } or [ ... ] balanced block
    for start_char, end_char in [('{', '}'), ('[', ']')]:
        start_idx = text.find(start_char)
        if start_idx == -1:
            continue

        depth = 0
        in_string = False
        escape_next = False

        for i, char in enumerate(text[start_idx:], start=start_idx):
            if escape_next:
                escape_next = False
                continue
            if char == '\\' and in_string:
                escape_next = True
                continue
            if char == '"' and not escape_next:
                in_string = not in_string
                continue
            if not in_string:
                if char == start_char:
                    depth += 1
                elif char == end_char:
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(text[start_idx:i+1])
                        except json.JSONDecodeError:
                            break

    # Strategy 4: jsonc — strip comments and retry
    try:
        clean = re.sub(r'//[^\n]*', '', text)  # Remove // comments
        clean = re.sub(r'/\*.*?\*/', '', clean, flags=re.DOTALL)  # Remove /* */ comments
        return json.loads(clean.strip())
    except Exception:
        pass

    return None  # All strategies failed

# Usage:
raw_outputs = [
    '{"result": "ok"}',                               # Clean JSON
    '```json\n{"result": "ok"}\n```',                 # Markdown fence
    'Here is the result:\n{"result": "ok"}',          # Prefix text
    'The answer is:\n```\n{"result": "ok"}\n```\n\nLet me know if you need anything else.',
]

for output in raw_outputs:
    parsed = extract_json(output)
    print(f"Extracted: {parsed}")  # All → {"result": "ok"}
```

### Option 3: System prompt with strict format instructions

```python
FORMAT_ENFORCEMENT_PROMPT = """You are a data extraction API.

OUTPUT FORMAT — MANDATORY:
- Respond with ONLY valid JSON
- No markdown code fences (no backticks)
- No explanatory text before or after the JSON
- No comments inside the JSON
- The response must start with {{ or [
- The response must end with }} or ]

If you cannot extract the requested data, return:
{{"error": "reason why extraction failed", "data": null}}

WRONG response format:
"Here is the extracted data:
```json
{{"name": "Alice"}}
```"

CORRECT response format:
{{"name": "Alice"}}"""

def extract_with_format_prompt(text: str, extraction_goal: str) -> dict:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        system=FORMAT_ENFORCEMENT_PROMPT,
        messages=[{"role": "user", "content": f"{extraction_goal}\n\nText: {text}"}],
        max_tokens=1024
    )

    raw = response.content[0].text
    result = extract_json(raw)

    if result is None:
        raise ValueError(
            f"Model returned non-JSON output despite format instructions: {raw[:200]}"
        )

    return result
```

### Option 4: Pydantic validation — enforce schema on parsed output

```python
from pydantic import BaseModel, ValidationError, Field
from typing import Literal
import anthropic
import json

class ExtractionResult(BaseModel):
    """Validated output schema — pydantic rejects wrong types/values"""
    entity_type: Literal["person", "company", "location", "product"]
    name: str = Field(min_length=1, max_length=200)
    confidence: float = Field(ge=0.0, le=1.0)
    context: str | None = None
    metadata: dict = Field(default_factory=dict)

def extract_entity(text: str) -> ExtractionResult:
    """
    Extract entity with schema validation.
    Retries once if output doesn't match schema.
    """
    client = anthropic.Anthropic()

    for attempt in range(2):
        stronger_instruction = "" if attempt == 0 else (
            "\n\nPrevious attempt failed validation. Return ONLY the JSON object. "
            "Ensure all required fields are present with correct types."
        )

        response = client.messages.create(
            model="claude-sonnet-4-6",
            system=f"""Return a JSON object with these exact fields:
- entity_type: one of "person", "company", "location", "product"
- name: string (the entity name)
- confidence: float 0.0-1.0
- context: string or null (surrounding context)
- metadata: object (any additional fields){stronger_instruction}""",
            messages=[{"role": "user", "content": f"Extract the main entity from: {text}"}],
            max_tokens=512
        )

        raw = response.content[0].text
        parsed = extract_json(raw)

        if parsed is None:
            if attempt == 0:
                continue
            raise ValueError(f"Failed to parse JSON after 2 attempts: {raw[:200]}")

        try:
            return ExtractionResult(**parsed)
        except ValidationError as e:
            if attempt == 0:
                print(f"Validation failed (attempt 1): {e.errors()}")
                continue
            raise ValueError(f"Schema validation failed after 2 attempts: {e}") from e

# Usage:
result = extract_entity("Apple Inc. reported $124B in quarterly revenue")
print(result.entity_type)   # → "company"
print(result.name)          # → "Apple Inc."
print(result.confidence)    # → 0.95 (or similar)
```

### Option 5: Output format testing in CI

```python
import pytest
import anthropic

client = anthropic.Anthropic()

@pytest.mark.parametrize("prompt,expected_keys", [
    ("Extract person name from: 'Alice Smith submitted the report'",
     ["name", "confidence"]),
    ("Classify sentiment of: 'Great product, terrible support'",
     ["sentiment", "confidence", "explanation"]),
])
def test_output_is_valid_json(prompt, expected_keys):
    """
    CI test: verify model always returns parseable JSON with required keys.
    Run this on every prompt template change.
    """
    # Run multiple times to catch non-determinism
    for trial in range(5):
        response = client.messages.create(
            model="claude-sonnet-4-6",
            system="Respond with JSON only. No markdown. No extra text.",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=512
        )

        raw = response.content[0].text
        parsed = extract_json(raw)

        assert parsed is not None, (
            f"Trial {trial}: Model returned non-JSON: {raw[:200]}"
        )
        assert isinstance(parsed, dict), (
            f"Trial {trial}: Expected dict, got {type(parsed)}: {raw[:100]}"
        )
        for key in expected_keys:
            assert key in parsed, (
                f"Trial {trial}: Missing required key '{key}'. Got: {list(parsed.keys())}"
            )

def test_output_format_stability():
    """Test that output format is consistent across 10 runs"""
    formats_seen = set()
    prompt = "List 3 colors as JSON array"

    for _ in range(10):
        response = client.messages.create(
            model="claude-sonnet-4-6",
            system="Return JSON only.",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=200
        )
        raw = response.content[0].text.strip()

        # Classify format
        if raw.startswith('[') or raw.startswith('{'):
            formats_seen.add("clean_json")
        elif raw.startswith('```'):
            formats_seen.add("markdown_fence")
        else:
            formats_seen.add("text_prefix")

    assert formats_seen == {"clean_json"}, (
        f"Format instability detected — saw formats: {formats_seen}"
    )
```

### Option 6: Output normalization pipeline

```python
from typing import Callable
import json

class OutputNormalizer:
    """
    Multi-stage pipeline to normalize agent output to a target format.
    Apply in order — first success wins.
    """

    def __init__(self, target_type: type = dict):
        self.target_type = target_type
        self._extractors: list[Callable] = [
            self._try_direct_parse,
            self._try_strip_markdown,
            self._try_find_json_block,
            self._try_regex_extract,
        ]

    def normalize(self, raw: str) -> dict | list:
        for extractor in self._extractors:
            result = extractor(raw)
            if result is not None and isinstance(result, self.target_type):
                return result

        raise ValueError(
            f"Cannot normalize output to {self.target_type.__name__}. "
            f"Raw output: {raw[:300]}"
        )

    def _try_direct_parse(self, text: str):
        try:
            return json.loads(text.strip())
        except Exception:
            return None

    def _try_strip_markdown(self, text: str):
        stripped = re.sub(r'^```(?:json)?\s*\n?', '', text.strip())
        stripped = re.sub(r'\n?```\s*$', '', stripped)
        try:
            return json.loads(stripped.strip())
        except Exception:
            return None

    def _try_find_json_block(self, text: str):
        return extract_json(text)

    def _try_regex_extract(self, text: str):
        # Last resort: find anything that looks like a JSON value
        patterns = [
            r'\{[^{}]*\}',          # Simple flat object
            r'\[[^\[\]]*\]',         # Simple flat array
        ]
        for pattern in patterns:
            for match in re.finditer(pattern, text, re.DOTALL):
                try:
                    result = json.loads(match.group(0))
                    if isinstance(result, self.target_type):
                        return result
                except Exception:
                    continue
        return None

normalizer = OutputNormalizer(target_type=dict)

# Works on any format the model might return:
outputs = [
    '{"status": "ok"}',
    '```json\n{"status": "ok"}\n```',
    'Result:\n```\n{"status": "ok"}\n```\nDone.',
    'The status is {"status": "ok"} as requested.',
]

for output in outputs:
    parsed = normalizer.normalize(output)
    assert parsed == {"status": "ok"}
```

## Output Format Reliability by Method

| Method | Reliability | Overhead | Best For |
|---|---|---|---|
| Tool use / forced schema | ~100% | Moderate | Production pipelines |
| Pydantic validation + retry | ~99% | Low | Structured extraction |
| System prompt (format only) | ~85% | None | Simple cases |
| Post-processing extraction | ~95% | Low | Handling legacy prompts |
| Output format testing in CI | Catches regressions | Testing time | Long-term stability |
| Temperature=0 | Reduces variance | None | Determinism aid |

## Expected Token Savings
Parse failure → error handling → retry with clarification → re-parse: ~6,000 tokens per failure
Forced structured output → parse succeeds on first attempt: 0 parsing overhead

## Environment
- Any agent whose output is consumed by code rather than displayed to a human; critical for pipelines, integrations, and API wrappers that depend on predictable output format
- Source: direct experience; format instability is the most common cause of brittle agent integrations that break silently after model updates
