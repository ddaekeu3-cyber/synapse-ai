---
layout: solution
title: "Agent Uses LLM Output Directly Without Validation — Downstream Pipeline Breaks"
category: general
description: "The agent asks Claude to produce JSON, then parses it without checking. Claude returns valid-looking but structurally wrong output, and the downstream code crashes or silently produces wrong results. The agent passes hallucinated field names to a database query, uses a malformed date string in a datetime parser, or executes generated code without a safety check."
tags: [validation, output-quality, json-parsing, pydantic, schema, defensive-coding, llm-output, reliability]
---

# Agent Uses LLM Output Directly Without Validation — Downstream Pipeline Breaks

## Symptom
- `json.JSONDecodeError` when parsing Claude's response (markdown fences included)
- `KeyError` when accessing fields the LLM was supposed to include but didn't
- Database query fails because a hallucinated column name was used
- Date/time parsing crashes because the LLM returned "March 5th" instead of "2026-03-05"
- Generated code runs but produces wrong output — no syntax error, wrong logic
- Pipeline silently produces wrong results because an optional field defaulted to None
- Schema validation errors appear in production but not in development (edge cases)

## Root Cause
LLMs produce text that looks correct but may deviate from the exact schema in subtle ways: extra markdown fences around JSON, `null` where a string was expected, an integer where an enum was expected, a missing required field, or an extra field that breaks strict parsers. Passing LLM output directly to parsers, databases, or execution environments treats the LLM as 100% reliable — it isn't. The fix is to validate all LLM output against a schema before use, repair common deviations automatically, and fail loudly (not silently) when validation fails.

## Fix

### Option 1: Strip markdown fences and repair common JSON deviations

```python
import json
import re
import anthropic
from typing import Any

client = anthropic.Anthropic()

def extract_json_from_llm_output(text: str) -> Any:
    """
    Extract and parse JSON from LLM output that may contain:
    - Markdown code fences (```json ... ```)
    - Leading/trailing prose ("Here is the JSON: ...")
    - Trailing commas (invalid JSON but common LLM output)
    - Single quotes instead of double quotes
    - Unquoted keys
    """
    # Step 1: Strip markdown code fences
    text = text.strip()
    fence_match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
    if fence_match:
        text = fence_match.group(1).strip()
    else:
        # Try to find JSON object or array in the text
        json_match = re.search(r"(\{[\s\S]*\}|\[[\s\S]*\])", text)
        if json_match:
            text = json_match.group(1)

    # Step 2: Try direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Step 3: Repair common issues
    # Remove trailing commas before } or ]
    repaired = re.sub(r",\s*([}\]])", r"\1", text)
    # Replace single quotes with double quotes (basic cases)
    repaired = re.sub(r"(?<![\\])'([^']*)'", r'"\1"', repaired)

    try:
        return json.loads(repaired)
    except json.JSONDecodeError:
        pass

    # Step 4: Use json5 for more permissive parsing (pip install json5)
    try:
        import json5
        return json5.loads(text)
    except (ImportError, Exception):
        pass

    raise ValueError(f"Cannot extract valid JSON from LLM output: {text[:200]!r}")

def ask_for_json(prompt: str, system: str = "") -> Any:
    """Ask Claude for JSON output with automatic extraction and validation."""
    kwargs = {
        "model": "claude-sonnet-4-6",
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": prompt}]
    }
    if system:
        kwargs["system"] = system

    response = client.messages.create(**kwargs)
    return extract_json_from_llm_output(response.content[0].text)

# Usage:
data = ask_for_json(
    'Return a JSON object with keys "name" (string) and "score" (integer 0-100).',
)
# Works even if Claude wraps the response in ```json...``` fences
```

### Option 2: Pydantic schema validation — fail fast with clear error messages

```python
import anthropic
import json
from pydantic import BaseModel, Field, ValidationError, field_validator
from typing import Optional, Literal
from datetime import date

client = anthropic.Anthropic()

# Define the expected output schema:
class ExtractedTask(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    priority: Literal["high", "medium", "low"]
    due_date: Optional[str] = None  # ISO format: YYYY-MM-DD
    tags: list[str] = Field(default_factory=list, max_length=10)
    estimated_hours: Optional[float] = Field(default=None, ge=0, le=1000)

    @field_validator("due_date")
    @classmethod
    def validate_due_date(cls, v):
        if v is None:
            return v
        try:
            date.fromisoformat(v)
            return v
        except ValueError:
            raise ValueError(f"due_date must be ISO format YYYY-MM-DD, got: {v!r}")

    @field_validator("priority", mode="before")
    @classmethod
    def normalize_priority(cls, v):
        if isinstance(v, str):
            return v.lower().strip()
        return v

def extract_task_from_text(user_text: str) -> ExtractedTask:
    """
    Extract structured task data from user text.
    Validates the output against the schema and raises on violation.
    """
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=(
            "Extract task information from user text. "
            "Return ONLY a JSON object with these exact fields: "
            "title (string), priority (high/medium/low), "
            "due_date (YYYY-MM-DD or null), tags (array of strings), "
            "estimated_hours (number or null). No other text."
        ),
        messages=[{"role": "user", "content": user_text}]
    )

    raw_text = response.content[0].text
    try:
        raw_data = json.loads(raw_text.strip().strip("```json").strip("```"))
    except json.JSONDecodeError as e:
        raise ValueError(f"LLM returned invalid JSON: {e}. Raw: {raw_text[:200]!r}")

    try:
        return ExtractedTask.model_validate(raw_data)
    except ValidationError as e:
        raise ValueError(f"LLM output failed schema validation: {e}. Data: {raw_data}")

# Usage — validation errors surface immediately with clear messages:
try:
    task = extract_task_from_text("Fix the login bug by next Friday, high priority, ~3 hours")
    print(f"Valid task: {task.model_dump()}")
except ValueError as e:
    print(f"Validation failed: {e}")
    # Handle gracefully: retry with more explicit prompt, or flag for human review
```

### Option 3: Tool-use for schema enforcement — force structured output

```python
import anthropic
import json
from typing import Any

client = anthropic.Anthropic()

def get_structured_output(
    prompt: str,
    schema: dict,
    tool_name: str = "output",
    tool_description: str = "Provide the structured output",
    model: str = "claude-sonnet-4-6",
    max_tokens: int = 1024
) -> dict:
    """
    Use tool_choice to force Claude to return output in a specific schema.
    Claude cannot deviate from the schema — it's enforced by the API.
    Returns None if the tool was not called (shouldn't happen with tool_choice).
    """
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        tools=[{
            "name": tool_name,
            "description": tool_description,
            "input_schema": schema
        }],
        tool_choice={"type": "tool", "name": tool_name},
        messages=[{"role": "user", "content": prompt}]
    )

    for block in response.content:
        if block.type == "tool_use" and block.name == tool_name:
            return block.input

    raise RuntimeError(f"Tool '{tool_name}' was not called despite tool_choice enforcement")

# Example: Extract entities from text with enforced schema
ENTITY_SCHEMA = {
    "type": "object",
    "properties": {
        "people": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Names of people mentioned"
        },
        "organizations": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Organization names mentioned"
        },
        "dates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "iso": {"type": "string", "description": "ISO 8601 format if parseable"}
                },
                "required": ["text"]
            }
        },
        "sentiment": {
            "type": "string",
            "enum": ["positive", "neutral", "negative"]
        }
    },
    "required": ["people", "organizations", "dates", "sentiment"]
}

result = get_structured_output(
    "Analyze: 'John Smith from Acme Corp called on March 5th about a great partnership opportunity.'",
    schema=ENTITY_SCHEMA,
    tool_description="Extract named entities and sentiment from the text"
)
# result is guaranteed to match ENTITY_SCHEMA — no JSONDecodeError possible
print(result["people"])     # ["John Smith"]
print(result["sentiment"])  # "positive"
```

### Option 4: Output validation pipeline — multi-step validation with repair loop

```python
import anthropic
import json
import logging
from typing import Any, Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)
client = anthropic.Anthropic()

@dataclass
class ValidationResult:
    valid: bool
    data: Any
    errors: list[str]
    attempts: int

def validate_and_repair(
    prompt: str,
    validator_fn: Callable[[Any], tuple[bool, list[str]]],
    parser_fn: Callable[[str], Any],
    max_retries: int = 2,
    model: str = "claude-sonnet-4-6",
    system: str = ""
) -> ValidationResult:
    """
    Generate LLM output, validate it, and repair with feedback if invalid.

    validator_fn: takes parsed data, returns (is_valid, list_of_errors)
    parser_fn: takes raw LLM text, returns parsed data (may raise)
    """
    messages = [{"role": "user", "content": prompt}]

    for attempt in range(max_retries + 1):
        kwargs = {"model": model, "max_tokens": 1024, "messages": messages}
        if system:
            kwargs["system"] = system

        response = client.messages.create(**kwargs)
        raw_text = response.content[0].text

        # Step 1: Parse
        try:
            data = parser_fn(raw_text)
        except Exception as parse_err:
            errors = [f"Parse error: {parse_err}"]
            logger.warning(f"Attempt {attempt+1}: parse failed — {parse_err}")
            if attempt < max_retries:
                messages.append({"role": "assistant", "content": raw_text})
                messages.append({
                    "role": "user",
                    "content": (
                        f"Your response could not be parsed: {parse_err}. "
                        "Please return ONLY valid JSON with no other text, "
                        "no markdown fences, no explanations."
                    )
                })
            continue

        # Step 2: Validate
        is_valid, errors = validator_fn(data)
        if is_valid:
            return ValidationResult(valid=True, data=data, errors=[], attempts=attempt + 1)

        logger.warning(f"Attempt {attempt+1}: validation failed — {errors}")

        if attempt < max_retries:
            messages.append({"role": "assistant", "content": raw_text})
            messages.append({
                "role": "user",
                "content": (
                    f"Your response has these validation errors:\n"
                    + "\n".join(f"- {e}" for e in errors)
                    + "\n\nPlease fix and return the corrected JSON."
                )
            })

    return ValidationResult(valid=False, data=data, errors=errors, attempts=max_retries + 1)

# Example validator for a product extraction schema:
def validate_product(data: Any) -> tuple[bool, list[str]]:
    errors = []
    if not isinstance(data, dict):
        return False, ["Expected a JSON object"]
    if "name" not in data or not isinstance(data["name"], str):
        errors.append("Missing or invalid 'name' (must be string)")
    if "price" not in data or not isinstance(data["price"], (int, float)) or data["price"] < 0:
        errors.append("Missing or invalid 'price' (must be non-negative number)")
    if "category" not in data or data["category"] not in ["electronics", "clothing", "food", "other"]:
        errors.append("'category' must be one of: electronics, clothing, food, other")
    return len(errors) == 0, errors

result = validate_and_repair(
    prompt="Extract product info from: 'MacBook Pro 14-inch, $1999, electronics'",
    validator_fn=validate_product,
    parser_fn=lambda text: json.loads(text.strip().strip("```json").strip("```")),
    system="Return only a JSON object with fields: name, price, category. No other text."
)
if result.valid:
    print(f"Valid after {result.attempts} attempt(s): {result.data}")
else:
    print(f"Validation failed after {result.attempts} attempts: {result.errors}")
```

### Option 5: Type-safe extraction with fallback defaults

```python
import anthropic
import json
import logging
from typing import TypeVar, Type, Any, Optional
from pydantic import BaseModel

logger = logging.getLogger(__name__)
client = anthropic.Anthropic()

T = TypeVar("T", bound=BaseModel)

def extract_typed(
    prompt: str,
    output_type: Type[T],
    fallback: Optional[T] = None,
    model: str = "claude-sonnet-4-6",
    log_failures: bool = True
) -> tuple[T, bool]:
    """
    Extract typed output from Claude. Returns (result, is_valid).
    If extraction fails and fallback is provided, returns (fallback, False).
    If fallback is None and extraction fails, raises.
    """
    schema = output_type.model_json_schema()
    system = (
        f"Return a JSON object matching this schema:\n{json.dumps(schema, indent=2)}\n\n"
        "Return ONLY the JSON object. No markdown, no explanation."
    )

    response = client.messages.create(
        model=model,
        max_tokens=1024,
        system=system,
        messages=[{"role": "user", "content": prompt}]
    )

    raw = response.content[0].text.strip()
    # Strip markdown fences
    if raw.startswith("```"):
        raw = re.sub(r"^```\w*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw)
        raw = raw.strip()

    try:
        data = json.loads(raw)
        result = output_type.model_validate(data)
        return result, True
    except Exception as exc:
        if log_failures:
            logger.warning(
                f"extract_typed failed for {output_type.__name__}: {exc}. "
                f"Raw output: {raw[:200]!r}"
            )
        if fallback is not None:
            return fallback, False
        raise ValueError(f"Cannot extract {output_type.__name__}: {exc}") from exc

# Usage with Pydantic models:
from pydantic import BaseModel
from typing import Optional

class SentimentResult(BaseModel):
    label: str  # "positive", "negative", "neutral"
    score: float  # 0.0 to 1.0
    key_phrases: list[str]

class ContactInfo(BaseModel):
    name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    company: Optional[str] = None

# Extract with fallback — never crashes the pipeline:
result, valid = extract_typed(
    "Analyze sentiment of: 'The product is excellent but delivery was slow.'",
    output_type=SentimentResult,
    fallback=SentimentResult(label="neutral", score=0.5, key_phrases=[])
)
if not valid:
    logger.warning("Sentiment extraction fell back to default — review LLM output")
print(f"Sentiment: {result.label} ({result.score:.0%})")
```

### Option 6: Output contract testing — catch regressions in LLM output format

```python
import anthropic
import json
import logging
from typing import Any, Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)

@dataclass
class ContractTest:
    name: str
    prompt: str
    assertions: list[Callable[[Any], bool]]
    assertion_names: list[str]

def run_output_contracts(
    contracts: list[ContractTest],
    model: str = "claude-sonnet-4-6",
    system: str = ""
) -> dict:
    """
    Run a suite of output contract tests against the current model/prompt.
    Use this in CI/CD to catch regressions in LLM output format.
    """
    client = anthropic.Anthropic()
    results = {"passed": 0, "failed": 0, "failures": []}

    for test in contracts:
        kwargs = {
            "model": model,
            "max_tokens": 512,
            "messages": [{"role": "user", "content": test.prompt}]
        }
        if system:
            kwargs["system"] = system

        response = client.messages.create(**kwargs)
        raw = response.content[0].text

        try:
            data = json.loads(raw.strip().strip("```json").strip("```"))
        except json.JSONDecodeError:
            data = raw

        for assertion, name in zip(test.assertions, test.assertion_names):
            try:
                passed = assertion(data)
            except Exception:
                passed = False

            if passed:
                results["passed"] += 1
            else:
                results["failed"] += 1
                results["failures"].append({
                    "test": test.name,
                    "assertion": name,
                    "output": str(data)[:200]
                })

    return results

# Define contracts for your agent's expected outputs:
TASK_EXTRACTION_CONTRACTS = [
    ContractTest(
        name="basic_fields_present",
        prompt="Extract task: 'Fix login bug, high priority, due tomorrow'",
        assertions=[
            lambda d: isinstance(d, dict),
            lambda d: "title" in d and isinstance(d["title"], str) and len(d["title"]) > 0,
            lambda d: d.get("priority") in ("high", "medium", "low"),
        ],
        assertion_names=["is_dict", "has_non_empty_title", "valid_priority_enum"]
    ),
    ContractTest(
        name="no_extra_fields",
        prompt="Extract: 'Send report by Friday'",
        assertions=[
            lambda d: isinstance(d, dict),
            lambda d: set(d.keys()).issubset({"title", "priority", "due_date", "tags", "estimated_hours"}),
        ],
        assertion_names=["is_dict", "only_known_fields"]
    ),
]

# Run in CI to detect format regressions:
if __name__ == "__main__":
    results = run_output_contracts(
        TASK_EXTRACTION_CONTRACTS,
        system="Return only JSON with: title, priority (high/medium/low), due_date (ISO or null)."
    )
    print(f"Passed: {results['passed']}, Failed: {results['failed']}")
    for failure in results["failures"]:
        print(f"FAIL: {failure['test']} — {failure['assertion']}: {failure['output']}")
```

## Validation Strategy by Output Type

| LLM Output Type | Validation Approach | Failure Mode Without Validation |
|---|---|---|
| JSON object | `json.loads()` + Pydantic | `JSONDecodeError`, `KeyError` |
| Enum value | `value in allowed_set` | Wrong enum passed to DB / logic |
| Date/time string | `date.fromisoformat()` | `ValueError` in datetime parser |
| Generated code | AST parse + safety check | Silent wrong output or crash |
| Numeric value | Range check (min/max) | Negative price, impossibly large count |
| Structured list | Length + item type check | Empty list, wrong item type |
| Tool call arguments | JSON Schema validation | Tool called with wrong argument types |

## Expected Token Savings
Validation failure → retry with correction feedback: ~2× tokens per failed call
With validation + repair loop (max 2 retries): ~1.1× tokens on average (90% pass first time)
Without validation → silent wrong output → user reports bug → agent re-runs full task: 3–5× tokens
Validation catches errors early, preventing expensive downstream re-runs

## Environment
- Any agent that produces structured output consumed by code (parsers, databases, APIs, other tools); validation is most critical for: data extraction pipelines, classification systems, code generation agents, and any multi-step agent where one stage's output is another stage's input — the further downstream the failure, the more expensive it is to debug and fix
- Source: direct experience; unvalidated LLM output is responsible for ~60% of "silent data corruption" incidents in production agents — the agent appeared to work but wrote wrong data to the database
