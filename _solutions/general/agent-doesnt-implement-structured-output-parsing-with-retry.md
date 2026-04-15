---
layout: solution
title: "Agent Doesn't Implement Structured Output Parsing with Retry"
category: general
description: "Agents that parse Claude's JSON or structured output without retry logic crash on malformed responses, markdown-wrapped JSON, partial completions, or schema mismatches. Robust parsing with targeted retry keeps agents resilient to the ~5% of responses that need repair."
tags: [general, structured-output, json-parsing, retry, pydantic, schema-validation, error-handling]
---

## Problem

Claude reliably produces structured output, but edge cases break naive parsers: JSON wrapped in markdown fences, trailing commas, truncated responses when max_tokens is too low, schema fields with wrong types, or rare hallucinated field names. Agents that call `json.loads()` without a fallback raise exceptions that halt the entire task. Retry-with-repair loops — asking Claude to fix its own output — recover from these cases without losing the work done so far.

## Solutions

### Option 1: Strip-and-Parse with One Repair Retry

```python
import anthropic
import json
import re

client = anthropic.Anthropic()

def strip_markdown_json(text: str) -> str:
    """Remove ```json ... ``` fences and leading/trailing whitespace."""
    text = text.strip()
    # Remove ```json or ``` fences
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()

def parse_with_retry(
    prompt: str,
    system: str,
    schema_hint: str = "",
    max_tokens: int = 512,
    max_retries: int = 1,
) -> dict:
    """
    Ask Claude for JSON output. If parsing fails, send the raw output
    back and ask it to repair and return valid JSON only.
    """
    messages = [{"role": "user", "content": prompt}]
    last_raw = ""

    for attempt in range(max_retries + 1):
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=max_tokens,
            system=system,
            messages=messages,
        )
        raw = resp.content[0].text
        last_raw = raw
        cleaned = strip_markdown_json(raw)
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError as e:
            if attempt >= max_retries:
                raise ValueError(f"JSON parse failed after {attempt+1} attempts: {e}\nRaw: {raw[:200]}")
            # Repair retry: show Claude its broken output
            repair_prompt = (
                f"Your previous response was not valid JSON:\n\n{raw}\n\n"
                f"Error: {e}\n\n"
                f"Please return ONLY valid JSON{' matching this schema: ' + schema_hint if schema_hint else ''}. "
                "No markdown, no explanation."
            )
            messages = [{"role": "user", "content": repair_prompt}]

    raise ValueError("Unreachable")

if __name__ == "__main__":
    result = parse_with_retry(
        prompt='Return a JSON object with keys "name" (string) and "score" (integer 0-100) for a fictional student.',
        system="You are a data generator. Always respond with raw JSON only, no markdown.",
    )
    print("Parsed:", result)
    assert isinstance(result.get("score"), int), "score must be int"
    print("PASS: structured output parsed and validated")

# Expected Token Savings: repair retry only triggers ~5% of the time; haiku keeps repair cost minimal
# Environment: any agent needing JSON output; handles markdown fences and minor formatting errors
```

### Option 2: Pydantic Schema Validation with Targeted Repair

```python
import anthropic
import json
import re
from pydantic import BaseModel, ValidationError, field_validator
from typing import Any

client = anthropic.Anthropic()

class TaskPlan(BaseModel):
    title: str
    steps: list[str]
    estimated_minutes: int
    priority: str  # "low" | "medium" | "high"

    @field_validator("priority")
    @classmethod
    def validate_priority(cls, v: str) -> str:
        allowed = {"low", "medium", "high"}
        if v.lower() not in allowed:
            raise ValueError(f"priority must be one of {allowed}, got '{v}'")
        return v.lower()

    @field_validator("steps")
    @classmethod
    def validate_steps(cls, v: list) -> list:
        if not v:
            raise ValueError("steps must not be empty")
        return v

def extract_json(text: str) -> Any:
    text = text.strip()
    # Strip markdown fences
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE).strip()
    # Try direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Try to find JSON object within text
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            return json.loads(match.group())
        raise

def generate_task_plan(task: str, max_retries: int = 2) -> TaskPlan:
    SYSTEM = (
        "Return a JSON object with exactly these fields: "
        "title (string), steps (list of strings), "
        "estimated_minutes (integer), priority ('low'|'medium'|'high'). "
        "No markdown, no extra text."
    )
    messages = [{"role": "user", "content": f"Create a task plan for: {task}"}]
    last_error = ""

    for attempt in range(max_retries + 1):
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=SYSTEM,
            messages=messages,
        )
        raw = resp.content[0].text
        try:
            data = extract_json(raw)
            return TaskPlan(**data)
        except (json.JSONDecodeError, ValidationError, KeyError) as e:
            last_error = str(e)
            if attempt >= max_retries:
                break
            messages = [
                {"role": "user", "content": (
                    f"Your response had an error: {last_error}\n"
                    f"Your response was: {raw}\n"
                    "Fix it and return ONLY valid JSON with all required fields."
                )},
            ]

    raise ValueError(f"Could not parse TaskPlan after {max_retries+1} attempts. Last error: {last_error}")

if __name__ == "__main__":
    plan = generate_task_plan("Set up a Python development environment")
    print(f"Title: {plan.title}")
    print(f"Steps: {len(plan.steps)} steps")
    print(f"Priority: {plan.priority}")
    print(f"Est. minutes: {plan.estimated_minutes}")

# Expected Token Savings: Pydantic catches type errors without an extra LLM call; repair only if schema fails
# Environment: agents with strict output contracts; Pydantic validators surface exact field-level errors
```

### Option 3: Streaming Parse with Fallback for Truncated Responses

```python
import anthropic
import json
import re

client = anthropic.Anthropic()

def try_complete_json(partial: str) -> dict | None:
    """Attempt to close an unclosed JSON object by appending missing braces."""
    text = partial.strip()
    # Count unclosed braces
    depth = 0
    in_string = False
    escape = False
    for ch in text:
        if escape:
            escape = False
            continue
        if ch == "\\" and in_string:
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
        elif not in_string:
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1

    if depth <= 0:
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return None

    # Append missing closing braces
    completed = text + "}" * depth
    try:
        return json.loads(completed)
    except json.JSONDecodeError:
        return None

def parse_streaming_json(prompt: str, system: str, max_tokens: int = 512) -> dict:
    """
    Stream Claude's response, accumulate it, then parse.
    If truncated (stop_reason='max_tokens'), attempt JSON completion repair.
    """
    accumulated = ""
    stop_reason = "end_turn"

    with client.messages.stream(
        model="claude-haiku-4-5-20251001",
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        for text in stream.text_stream:
            accumulated += text
        stop_reason = stream.get_final_message().stop_reason

    # Clean markdown fences
    clean = re.sub(r"^```(?:json)?\s*|\s*```$", "", accumulated.strip(), flags=re.IGNORECASE).strip()

    # Try direct parse
    try:
        return json.loads(clean)
    except json.JSONDecodeError:
        pass

    if stop_reason == "max_tokens":
        # Attempt to complete truncated JSON
        repaired = try_complete_json(clean)
        if repaired:
            print(f"  [repaired truncated JSON: added closing braces]")
            return repaired
        # Re-run with higher token limit
        print(f"  [truncated: retrying with 2x tokens]")
        return parse_streaming_json(prompt, system, max_tokens=max_tokens * 2)

    raise ValueError(f"JSON parse failed (stop={stop_reason}): {accumulated[:200]}")

if __name__ == "__main__":
    result = parse_streaming_json(
        prompt='Return a JSON object: {"items": [1,2,3], "total": 3, "status": "ok"}',
        system="Return raw JSON only. No explanation, no markdown.",
        max_tokens=256,
    )
    print("Parsed:", result)

# Expected Token Savings: streaming parse catches truncation before extra round-trip; brace completion is free
# Environment: agents with max_tokens limits; streaming allows detecting truncation before full parse attempt
```

### Option 4: Exponential Backoff Retry with Schema Narrowing

```python
import anthropic
import json
import re
import time
from dataclasses import dataclass

client = anthropic.Anthropic()

@dataclass
class RetryConfig:
    max_attempts: int = 3
    base_delay: float = 1.0
    max_delay: float = 8.0
    backoff_factor: float = 2.0

def build_repair_system(original_system: str, errors: list[str]) -> str:
    """Narrow the schema instructions based on observed errors."""
    error_hints = []
    for e in errors:
        if "Expecting" in e:
            error_hints.append("Ensure the JSON is syntactically valid.")
        if "not of type" in e or "is not a" in e:
            error_hints.append("Check that numeric fields are numbers (not strings).")
        if "required" in e.lower():
            error_hints.append("Include ALL required fields.")
    return original_system + "\n\nIMPORTANT: Previous attempts failed. " + " ".join(set(error_hints))

def robust_json_call(
    prompt: str,
    system: str,
    required_keys: list[str] | None = None,
    config: RetryConfig | None = None,
) -> dict:
    config = config or RetryConfig()
    errors: list[str] = []
    current_system = system

    for attempt in range(config.max_attempts):
        try:
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=512,
                system=current_system,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = resp.content[0].text
            clean = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.IGNORECASE).strip()
            data = json.loads(clean)

            # Validate required keys
            if required_keys:
                missing = [k for k in required_keys if k not in data]
                if missing:
                    raise ValueError(f"Missing required keys: {missing}")

            return data

        except (json.JSONDecodeError, ValueError) as e:
            errors.append(str(e))
            if attempt + 1 < config.max_attempts:
                delay = min(config.base_delay * (config.backoff_factor ** attempt), config.max_delay)
                print(f"  [attempt {attempt+1} failed: {e}] retrying in {delay:.1f}s")
                time.sleep(delay)
                # Tighten instructions on each retry
                current_system = build_repair_system(system, errors)
            else:
                raise ValueError(
                    f"Failed after {config.max_attempts} attempts.\nErrors: {errors}"
                )

if __name__ == "__main__":
    result = robust_json_call(
        prompt="Generate a product listing with name, price, and in_stock status.",
        system='Respond with JSON only. Example: {"name": "Widget", "price": 9.99, "in_stock": true}',
        required_keys=["name", "price", "in_stock"],
        config=RetryConfig(max_attempts=3, base_delay=0.5),
    )
    print("Result:", result)
    assert isinstance(result["price"], (int, float)), "price must be numeric"
    assert isinstance(result["in_stock"], bool), "in_stock must be bool"
    print("PASS: all types validated")

# Expected Token Savings: schema narrowing on retry guides Claude more precisely; reduces 3rd-attempt rate
# Environment: production agents; backoff prevents hammering the API on repeated failures
```

### Option 5: Prefill-Guided JSON Extraction

```python
import anthropic
import json

client = anthropic.Anthropic()

def extract_with_prefill(
    prompt: str,
    system: str,
    opening_brace: str = '{"',
    max_tokens: int = 512,
) -> dict:
    """
    Use assistant prefill to force Claude to begin with '{',
    eliminating markdown fences and preamble entirely.
    """
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=max_tokens,
        system=system,
        messages=[
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": opening_brace},  # prefill
        ],
    )
    # Prepend the prefill since Claude continues from it
    raw = opening_brace + resp.content[0].text

    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        # Fallback: try extracting the first complete JSON object
        depth = 0
        start = raw.index("{")
        in_str = False
        for i, ch in enumerate(raw[start:], start):
            if ch == '"' and (i == 0 or raw[i-1] != "\\"):
                in_str = not in_str
            if not in_str:
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        candidate = raw[start:i+1]
                        return json.loads(candidate)
        raise ValueError(f"Could not extract JSON from prefill response: {raw[:200]}")

def batch_extract(prompts: list[str], schema: str) -> list[dict]:
    SYSTEM = f"Output JSON matching this schema: {schema}. No extra text."
    results = []
    for p in prompts:
        try:
            result = extract_with_prefill(p, SYSTEM)
            results.append(result)
            print(f"  OK: {list(result.keys())}")
        except (ValueError, json.JSONDecodeError) as e:
            print(f"  FAIL: {e}")
            results.append({"error": str(e)})
    return results

if __name__ == "__main__":
    schema = '{"city": string, "country": string, "population_millions": number}'
    prompts = [
        "Capital of France",
        "Capital of Japan",
        "Capital of Brazil",
    ]
    results = batch_extract(prompts, schema)
    for r in results:
        if "error" not in r:
            print(f"  → {r['city']}, {r['country']}: {r['population_millions']}M")

# Expected Token Savings: prefill eliminates preamble tokens entirely; no retry needed in most cases
# Environment: high-volume extraction pipelines; prefill is the most reliable way to enforce JSON-only output
```

### Option 6: Multi-Field Validation with Self-Correction Chain

```python
import anthropic
import json
import re
from typing import Any

client = anthropic.Anthropic()

VALIDATORS = {
    "age": lambda v: isinstance(v, int) and 0 <= v <= 150,
    "email": lambda v: isinstance(v, str) and "@" in v and "." in v.split("@")[-1],
    "score": lambda v: isinstance(v, (int, float)) and 0.0 <= v <= 1.0,
    "tags": lambda v: isinstance(v, list) and all(isinstance(t, str) for t in v),
}

def validate_fields(data: dict, required: dict[str, str]) -> list[str]:
    """
    required: {field_name: validator_key}
    Returns list of error messages.
    """
    errors = []
    for field, validator_key in required.items():
        if field not in data:
            errors.append(f"Missing field: '{field}'")
            continue
        validator = VALIDATORS.get(validator_key)
        if validator and not validator(data[field]):
            errors.append(f"Field '{field}' failed {validator_key} validation (got {data[field]!r})")
    return errors

def generate_with_validation(
    prompt: str,
    system: str,
    required_fields: dict[str, str],
    max_rounds: int = 3,
) -> dict:
    messages = [{"role": "user", "content": prompt}]

    for round_num in range(max_rounds):
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=system,
            messages=messages,
        )
        raw = resp.content[0].text
        clean = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.IGNORECASE).strip()

        try:
            data = json.loads(clean)
        except json.JSONDecodeError as e:
            errors = [f"Invalid JSON: {e}"]
            data = {}
        else:
            errors = validate_fields(data, required_fields)

        if not errors:
            print(f"  Validated in {round_num+1} round(s)")
            return data

        # Self-correction: show Claude exactly what failed
        correction = (
            f"Your response had {len(errors)} validation error(s):\n"
            + "\n".join(f"- {e}" for e in errors)
            + "\n\nYour response was:\n" + raw
            + "\n\nFix ALL errors and return ONLY valid JSON."
        )
        messages = [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": raw},
            {"role": "user", "content": correction},
        ]

    raise ValueError(f"Validation failed after {max_rounds} rounds")

if __name__ == "__main__":
    result = generate_with_validation(
        prompt="Generate a user profile with age, email, score, and tags.",
        system='Return JSON only: {"age": int, "email": str, "score": float 0-1, "tags": [str]}',
        required_fields={"age": "age", "email": "email", "score": "score", "tags": "tags"},
    )
    print("Valid result:", result)

# Expected Token Savings: field-specific error messages guide repair precisely; typically 1-2 rounds total
# Environment: agents with strict data contracts; multi-turn correction avoids full re-generation
```

## Comparison

| Option | Repair Strategy | Schema Enforcement | Truncation Handling | Best For |
|--------|----------------|-------------------|---------------------|---------|
| 1 — Strip + one retry | Send raw back + ask to fix | None | No | Simple JSON extraction |
| 2 — Pydantic validation | Send validation error back | Pydantic model | No | Typed output with constraints |
| 3 — Streaming + completion | Brace completion or 2x tokens | None | Yes | Token-limited streaming responses |
| 4 — Exponential backoff | Tighten instructions per retry | Required key list | No | Production agents with rate limits |
| 5 — Prefill | Eliminates need for repair | None | No | High-volume extraction (lowest retry rate) |
| 6 — Multi-field self-correction | Show exact field errors | Custom validators | No | Complex schemas with type constraints |
