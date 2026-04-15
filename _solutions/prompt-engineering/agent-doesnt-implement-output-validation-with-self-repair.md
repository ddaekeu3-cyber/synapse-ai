---
layout: solution
title: "Agent Doesn't Validate Output and Self-Repair on Failure"
category: prompt-engineering
description: "Agents that accept any LLM response as valid pass malformed JSON, wrong formats, or incomplete answers to downstream systems. Output validation with self-repair re-prompts the model to fix its own mistakes."
tags: [prompt-engineering, validation, self-repair, retry, pydantic, structured-output]
---

# Agent Doesn't Validate Output and Self-Repair on Failure

LLMs occasionally generate output that doesn't conform to the requested format: JSON with a syntax error, a required field missing, markdown instead of plain text, or a list that stops mid-way. Agents that pass this output directly to parsers or downstream services fail with cryptic errors. The fix is to validate output immediately after generation and re-prompt with the specific error if validation fails.

## Why This Happens

Developers test with prompts that always produce correct output and assume the format instruction is reliable. In production, edge cases in user input, context length pressure, or model variance produce malformed responses.

---

## Option 1: JSON Schema Validation with Re-Prompt

Validate the response as JSON and re-prompt with the specific parse error if it fails.

```python
import json
import anthropic
from pydantic import BaseModel, ValidationError

client = anthropic.Anthropic()


class AnalysisResult(BaseModel):
    sentiment: str
    confidence: float
    key_points: list[str]
    summary: str


SYSTEM_PROMPT = """You are a text analysis assistant. Always respond with valid JSON matching this schema:
{
  "sentiment": "positive" | "negative" | "neutral",
  "confidence": <float 0.0-1.0>,
  "key_points": [<string>, ...],
  "summary": <string>
}
Respond with JSON only. No markdown, no explanation."""


def analyze_with_repair(text: str, max_attempts: int = 3) -> AnalysisResult:
    messages = [{"role": "user", "content": f"Analyze this text:\n\n{text}"}]
    last_error = ""

    for attempt in range(max_attempts):
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=SYSTEM_PROMPT,
            messages=messages,
        )
        raw = response.content[0].text.strip()

        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()

        try:
            data = json.loads(raw)
            return AnalysisResult(**data)
        except json.JSONDecodeError as e:
            last_error = f"JSON parse error: {e}"
        except ValidationError as e:
            last_error = f"Schema validation error: {e.errors()}"

        if attempt < max_attempts - 1:
            # Add the bad response and error to the conversation
            messages.append({"role": "assistant", "content": raw})
            messages.append({
                "role": "user",
                "content": (
                    f"Your response had an error: {last_error}\n\n"
                    f"Please fix it and return valid JSON only, matching the required schema."
                ),
            })

    raise ValueError(f"Failed to get valid output after {max_attempts} attempts: {last_error}")


if __name__ == "__main__":
    result = analyze_with_repair(
        "The new product launch exceeded all expectations with record sales and rave reviews."
    )
    print(result.model_dump_json(indent=2))
```

**Expected Token Savings:** Self-repair in 1 extra turn is far cheaper than pipeline failures that require full restarts.

**Environment:** Any structured output use case; JSON extraction, classification, data parsing.

---

## Option 2: Pydantic-Guided Re-Prompt with Field-Level Errors

Extract specific field errors from Pydantic and tell the model exactly which fields to fix.

```python
import json
import anthropic
from pydantic import BaseModel, Field, ValidationError, field_validator
from typing import Literal

client = anthropic.Anthropic()


class TaskPlan(BaseModel):
    task_name: str = Field(min_length=1, max_length=100)
    steps: list[str] = Field(min_length=1, max_length=10)
    estimated_minutes: int = Field(ge=1, le=480)
    priority: Literal["low", "medium", "high", "critical"]
    dependencies: list[str] = Field(default_factory=list)

    @field_validator("steps")
    @classmethod
    def steps_nonempty(cls, v: list[str]) -> list[str]:
        if any(not s.strip() for s in v):
            raise ValueError("Steps cannot contain empty strings")
        return v


def format_validation_errors(exc: ValidationError) -> str:
    """Convert Pydantic errors to a human-readable repair prompt."""
    lines = ["Fix these validation errors:"]
    for error in exc.errors():
        field = " -> ".join(str(loc) for loc in error["loc"])
        lines.append(f"  - Field '{field}': {error['msg']} (got: {error.get('input', 'N/A')!r})")
    return "\n".join(lines)


def plan_task_with_repair(task_description: str, max_attempts: int = 3) -> TaskPlan:
    system = """Return a task plan as JSON with these exact fields:
{
  "task_name": string (1-100 chars),
  "steps": array of strings (1-10 items, none empty),
  "estimated_minutes": integer (1-480),
  "priority": "low" | "medium" | "high" | "critical",
  "dependencies": array of strings (can be empty)
}
JSON only, no markdown."""

    messages = [{"role": "user", "content": f"Plan this task: {task_description}"}]

    for attempt in range(max_attempts):
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=system,
            messages=messages,
        )
        raw = response.content[0].text.strip()
        messages.append({"role": "assistant", "content": raw})

        try:
            data = json.loads(raw)
            return TaskPlan(**data)
        except (json.JSONDecodeError, KeyError) as e:
            error_msg = f"Invalid JSON: {e}"
        except ValidationError as e:
            error_msg = format_validation_errors(e)

        if attempt < max_attempts - 1:
            messages.append({
                "role": "user",
                "content": f"{error_msg}\n\nReturn corrected JSON only.",
            })

    raise ValueError(f"Could not produce valid TaskPlan after {max_attempts} attempts")


if __name__ == "__main__":
    plan = plan_task_with_repair("Deploy the new authentication service to production")
    print(plan.model_dump_json(indent=2))
```

**Expected Token Savings:** Field-specific errors guide the model to fix exactly what's wrong; typically repairs in 1 extra turn.

**Environment:** Complex structured output with business-rule validation; task planning, form extraction.

---

## Option 3: Format Validation with Fallback Extraction

If structured output fails after retries, fall back to extracting the required fields from freeform text.

```python
import re
import json
import anthropic
from pydantic import BaseModel

client = anthropic.Anthropic()


class ExtractedContact(BaseModel):
    name: str
    email: str
    phone: str | None = None
    company: str | None = None


def extract_from_freeform(text: str, target_model: type[BaseModel]) -> dict:
    """Last-resort: use regex to extract fields from unstructured text."""
    extracted = {}

    email_match = re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", text)
    if email_match:
        extracted["email"] = email_match.group()

    phone_match = re.search(r"[\+\d][\d\s\-\(\)]{7,}", text)
    if phone_match:
        extracted["phone"] = phone_match.group().strip()

    # Name: look for "Name: ..." pattern
    name_match = re.search(r"(?:name|contact)[\s:]+([A-Z][a-z]+ [A-Z][a-z]+)", text, re.I)
    if name_match:
        extracted["name"] = name_match.group(1)

    return extracted


def extract_contact(raw_text: str, max_attempts: int = 3) -> ExtractedContact:
    system = """Extract contact information from text. Return JSON only:
{"name": string, "email": string, "phone": string|null, "company": string|null}"""

    messages = [{"role": "user", "content": f"Extract contact info from:\n\n{raw_text}"}]

    for attempt in range(max_attempts):
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            system=system,
            messages=messages,
        )
        raw = response.content[0].text.strip()
        # Strip markdown
        if "```" in raw:
            raw = re.sub(r"```(?:json)?", "", raw).strip()

        try:
            data = json.loads(raw)
            return ExtractedContact(**data)
        except Exception as e:
            if attempt < max_attempts - 1:
                messages.append({"role": "assistant", "content": raw})
                messages.append({
                    "role": "user",
                    "content": f"Error: {e}. Return valid JSON only.",
                })

    # Fallback: regex extraction
    print("[WARNING] Falling back to regex extraction")
    fallback_data = extract_from_freeform(raw_text, ExtractedContact)
    if "name" not in fallback_data:
        fallback_data["name"] = "Unknown"
    if "email" not in fallback_data:
        fallback_data["email"] = "unknown@unknown.com"
    return ExtractedContact(**fallback_data)


if __name__ == "__main__":
    text = """
    Hi, I'm Sarah Johnson from Acme Corp. You can reach me at sarah.johnson@acme.com
    or call me at +1-555-234-5678 anytime.
    """
    contact = extract_contact(text)
    print(contact.model_dump_json(indent=2))
```

**Expected Token Savings:** Regex fallback eliminates failure for structured extraction even when LLM output is malformed.

**Environment:** Data extraction pipelines; ETL workflows where partial results beat total failures.

---

## Option 4: Async Self-Repair with Exponential Backoff

Combine self-repair with async execution and brief delays between attempts to handle model instability.

```python
import asyncio
import json
import time
import anthropic
from pydantic import BaseModel, ValidationError

client = anthropic.AsyncAnthropic()


class ReportSection(BaseModel):
    title: str
    content: str
    word_count: int
    confidence: float


async def generate_with_repair(
    prompt: str,
    output_model: type[BaseModel],
    schema_description: str,
    max_attempts: int = 4,
    base_delay: float = 1.0,
) -> BaseModel:
    messages = [{"role": "user", "content": prompt}]
    system = f"Return valid JSON matching this schema:\n{schema_description}\nJSON only."

    for attempt in range(max_attempts):
        if attempt > 0:
            delay = base_delay * (2 ** (attempt - 1))
            await asyncio.sleep(delay)

        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=system,
            messages=messages,
        )
        raw = response.content[0].text.strip()

        try:
            # Handle ```json blocks
            if raw.startswith("```"):
                parts = raw.split("```")
                raw = parts[1].lstrip("json").strip()

            data = json.loads(raw)
            result = output_model(**data)
            if attempt > 0:
                print(f"[Repair] Succeeded on attempt {attempt + 1}")
            return result

        except (json.JSONDecodeError, ValidationError, KeyError) as e:
            error_detail = str(e)[:200]
            print(f"[Repair] Attempt {attempt + 1}/{max_attempts} failed: {error_detail}")

            if attempt < max_attempts - 1:
                messages.append({"role": "assistant", "content": raw})
                messages.append({
                    "role": "user",
                    "content": (
                        f"Your response had this error: {error_detail}\n\n"
                        f"Please return ONLY valid JSON matching the schema. "
                        f"No explanation, no markdown."
                    ),
                })

    raise ValueError(f"All {max_attempts} repair attempts failed")


async def main():
    section = await generate_with_repair(
        prompt="Write a brief report section about Python async programming.",
        output_model=ReportSection,
        schema_description="""{
  "title": "string",
  "content": "string",
  "word_count": integer,
  "confidence": float (0.0-1.0)
}""",
    )
    print(section.model_dump_json(indent=2))


if __name__ == "__main__":
    asyncio.run(main())
```

**Expected Token Savings:** Exponential backoff prevents thundering herd on model instability; repair turns cost far less than pipeline restarts.

**Environment:** Async pipelines; content generation with strict output requirements.

---

## Option 5: Multi-Stage Validation — Schema then Business Rules

First validate JSON schema, then apply domain-specific business rule validation in a second pass.

```python
import json
import anthropic
from pydantic import BaseModel, Field, field_validator
from datetime import date

client = anthropic.Anthropic()


class EventDetails(BaseModel):
    name: str = Field(min_length=1)
    date: str  # ISO format: YYYY-MM-DD
    capacity: int = Field(ge=1, le=10000)
    ticket_price_usd: float = Field(ge=0)
    is_online: bool
    venue: str | None = None

    @field_validator("date")
    @classmethod
    def date_must_be_future(cls, v: str) -> str:
        try:
            event_date = date.fromisoformat(v)
        except ValueError:
            raise ValueError(f"Invalid date format: {v}. Use YYYY-MM-DD.")
        if event_date <= date.today():
            raise ValueError(f"Event date {v} must be in the future")
        return v

    @field_validator("venue")
    @classmethod
    def venue_required_if_not_online(cls, v, info) -> str | None:
        if info.data.get("is_online") is False and not v:
            raise ValueError("venue is required for in-person events")
        return v


def apply_business_rules(event: EventDetails) -> list[str]:
    """Return list of business rule violations (not schema errors)."""
    violations = []
    if event.ticket_price_usd == 0 and event.capacity > 1000:
        violations.append("Free events with capacity > 1000 require approval")
    if not event.is_online and event.capacity > 5000:
        violations.append("In-person events > 5000 capacity require venue certification")
    return violations


def create_event(description: str, max_attempts: int = 3) -> EventDetails:
    system = """Extract event details as JSON:
{
  "name": string,
  "date": "YYYY-MM-DD",
  "capacity": integer,
  "ticket_price_usd": float,
  "is_online": boolean,
  "venue": string | null
}
JSON only."""

    messages = [{"role": "user", "content": description}]

    for attempt in range(max_attempts):
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            system=system,
            messages=messages,
        )
        raw = response.content[0].text.strip()
        messages.append({"role": "assistant", "content": raw})

        try:
            data = json.loads(raw)
            event = EventDetails(**data)

            # Stage 2: Business rule validation
            violations = apply_business_rules(event)
            if violations:
                messages.append({
                    "role": "user",
                    "content": (
                        f"The event has these policy violations:\n"
                        + "\n".join(f"- {v}" for v in violations)
                        + "\n\nUpdate the event details to comply with these rules and return corrected JSON."
                    ),
                })
                continue

            return event

        except (json.JSONDecodeError, Exception) as e:
            if attempt < max_attempts - 1:
                messages.append({
                    "role": "user",
                    "content": f"Error: {e}. Please fix and return valid JSON.",
                })

    raise ValueError("Failed to produce valid event after all attempts")


if __name__ == "__main__":
    event = create_event(
        "Tech conference on June 15, 2027 in San Francisco, 500 people, $299 tickets, in-person at Moscone Center"
    )
    print(event.model_dump_json(indent=2))
```

**Expected Token Savings:** Separating schema and business rule validation reduces ambiguous error messages to the model; repairs succeed in fewer turns.

**Environment:** Domain-specific extraction (events, orders, contracts); any structured data with business constraints.

---

## Option 6: Output Validation Test Suite

Automated tests that verify self-repair logic works correctly across known failure cases.

```python
import json
import pytest
from unittest.mock import MagicMock, patch
from pydantic import BaseModel


class SimpleResult(BaseModel):
    answer: str
    confidence: float


def validate_and_repair(raw: str, target: type[BaseModel]) -> BaseModel:
    """Parse and validate; raise ValueError with details on failure."""
    raw = raw.strip()
    if raw.startswith("```"):
        import re
        raw = re.sub(r"```(?:json)?", "", raw).strip()
    try:
        data = json.loads(raw)
        return target(**data)
    except json.JSONDecodeError as e:
        raise ValueError(f"JSON error: {e}")
    except Exception as e:
        raise ValueError(f"Validation error: {e}")


class TestOutputValidation:
    def test_valid_json_passes(self):
        raw = '{"answer": "Paris", "confidence": 0.95}'
        result = validate_and_repair(raw, SimpleResult)
        assert result.answer == "Paris"
        assert result.confidence == 0.95

    def test_markdown_fences_stripped(self):
        raw = '```json\n{"answer": "Berlin", "confidence": 0.8}\n```'
        result = validate_and_repair(raw, SimpleResult)
        assert result.answer == "Berlin"

    def test_invalid_json_raises_value_error(self):
        with pytest.raises(ValueError, match="JSON error"):
            validate_and_repair('{"answer": "Paris"  BROKEN', SimpleResult)

    def test_missing_required_field_raises_value_error(self):
        with pytest.raises(ValueError, match="Validation error"):
            validate_and_repair('{"answer": "Paris"}', SimpleResult)  # missing confidence

    def test_wrong_type_raises_value_error(self):
        with pytest.raises(ValueError, match="Validation error"):
            validate_and_repair('{"answer": "Paris", "confidence": "high"}', SimpleResult)

    def test_empty_response_raises_value_error(self):
        with pytest.raises(ValueError):
            validate_and_repair("", SimpleResult)

    def test_free_text_response_raises_value_error(self):
        with pytest.raises(ValueError):
            validate_and_repair(
                "The answer is Paris with high confidence.", SimpleResult
            )

    def test_extra_fields_accepted(self):
        """Pydantic v2 ignores extra fields by default."""
        raw = '{"answer": "Madrid", "confidence": 0.7, "extra_field": "ignored"}'
        result = validate_and_repair(raw, SimpleResult)
        assert result.answer == "Madrid"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
```

**Expected Token Savings:** Test suite catches regressions in validation logic before deployment; prevents silent failures that burn tokens on undetected bad output.

**Environment:** CI pipeline; any project with structured LLM output parsing.

---

## Comparison

| Option | Validation Type | Repair Strategy | Async | Fallback |
|--------|----------------|-----------------|-------|----------|
| 1. JSON + Pydantic | Schema | Re-prompt with error | No | No |
| 2. Pydantic field errors | Field-level | Specific field repair | No | No |
| 3. Format + regex fallback | Schema | Re-prompt + regex | No | Yes |
| 4. Async with backoff | Schema | Re-prompt + delay | Yes | No |
| 5. Schema + business rules | Schema + domain | Two-stage repair | No | No |
| 6. Test suite | All above | N/A | No | N/A |
