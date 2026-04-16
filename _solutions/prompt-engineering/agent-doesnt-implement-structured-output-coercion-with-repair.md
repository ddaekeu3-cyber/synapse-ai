---
title: "Agent Doesn't Implement Structured Output Coercion with Repair"
description: "How to coerce and repair malformed structured outputs—JSON, YAML, numbered lists—when the model doesn't conform to the expected format on the first attempt."
categories: [prompt-engineering]
difficulty: intermediate
---

Models don't always produce perfectly formatted JSON or YAML on the first try. They add preambles, omit closing braces, or include comments. Rather than failing hard or returning raw text, coerce and repair the output into the expected structure automatically.

## Solution 1: JSON Extract-and-Repair Pipeline

Try strict parsing first, then progressively apply repair strategies until the output is valid JSON.

```python
import asyncio
import json
import re
import anthropic

client = anthropic.AsyncAnthropic()
MODEL = "claude-haiku-4-5-20251001"


def extract_json_block(text: str) -> str:
    """Extract JSON from a markdown code block or raw text."""
    # Try fenced code block
    match = re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", text)
    if match:
        return match.group(1).strip()
    # Try first { or [ to last } or ]
    start = min(
        (text.find("{") if "{" in text else len(text)),
        (text.find("[") if "[" in text else len(text)),
    )
    if start < len(text):
        # Find matching close
        end = max(text.rfind("}"), text.rfind("]"))
        if end > start:
            return text[start:end + 1]
    return text.strip()


def repair_json(raw: str) -> str:
    """Apply lightweight repairs to malformed JSON."""
    # Remove trailing commas before } or ]
    raw = re.sub(r",\s*([}\]])", r"\1", raw)
    # Remove single-line comments
    raw = re.sub(r"//[^\n]*", "", raw)
    # Remove block comments
    raw = re.sub(r"/\*.*?\*/", "", raw, flags=re.DOTALL)
    # Replace single quotes with double quotes (simple cases)
    raw = re.sub(r"'([^']*)':", r'"\1":', raw)
    return raw


async def llm_repair_json(malformed: str) -> str:
    """Use a cheap model to fix structurally broken JSON."""
    resp = await client.messages.create(
        model=MODEL,
        max_tokens=500,
        messages=[
            {
                "role": "user",
                "content": (
                    "Fix this malformed JSON and return ONLY the corrected JSON, "
                    "nothing else:\n\n" + malformed
                ),
            }
        ],
    )
    return resp.content[0].text.strip()


async def coerce_json(text: str, schema: dict | None = None) -> dict | list:
    """
    Coerce raw LLM text into valid JSON using a repair pipeline.
    Raises ValueError if all repair strategies fail.
    """
    raw = extract_json_block(text)

    # Strategy 1: Direct parse
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # Strategy 2: Lightweight text repair
    repaired = repair_json(raw)
    try:
        return json.loads(repaired)
    except json.JSONDecodeError:
        pass

    # Strategy 3: LLM repair
    llm_fixed = await llm_repair_json(raw)
    try:
        return json.loads(llm_fixed)
    except json.JSONDecodeError as e:
        raise ValueError(f"All JSON repair strategies failed: {e}") from e


async def generate_structured(prompt: str) -> dict | list:
    resp = await client.messages.create(
        model=MODEL,
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )
    return await coerce_json(resp.content[0].text)


async def main():
    prompts = [
        'Return a JSON object with "name" (string) and "score" (number) fields. Name: "Alice", Score: 95.',
        'Return a JSON array of 3 colors.',
    ]
    results = await asyncio.gather(*[generate_structured(p) for p in prompts])
    for p, r in zip(prompts, results):
        print(f"Prompt: {p[:60]}")
        print(f"Result: {json.dumps(r)}\n")


asyncio.run(main())
```

## Solution 2: Pydantic Schema Enforcement with Re-Prompt

Parse into a Pydantic model; on validation failure, re-prompt the model with the specific error.

```python
import asyncio
import json
from typing import Any
import anthropic

client = anthropic.AsyncAnthropic()
MODEL = "claude-haiku-4-5-20251001"

try:
    from pydantic import BaseModel, ValidationError, field_validator

    class ProductReview(BaseModel):
        product_name: str
        rating: int
        pros: list[str]
        cons: list[str]
        summary: str

        @field_validator("rating")
        @classmethod
        def rating_in_range(cls, v: int) -> int:
            if not 1 <= v <= 5:
                raise ValueError("rating must be between 1 and 5")
            return v

    HAS_PYDANTIC = True
except ImportError:
    HAS_PYDANTIC = False


async def generate_with_schema_repair(
    prompt: str,
    schema_class,
    max_attempts: int = 3,
) -> Any:
    messages = [{"role": "user", "content": prompt}]

    for attempt in range(max_attempts):
        resp = await client.messages.create(
            model=MODEL,
            max_tokens=512,
            messages=messages,
        )
        raw_text = resp.content[0].text

        # Extract JSON
        import re
        match = re.search(r"\{[\s\S]+\}", raw_text)
        raw_json = match.group(0) if match else raw_text

        try:
            data = json.loads(raw_json)
            return schema_class(**data)
        except (json.JSONDecodeError, Exception) as e:
            if attempt == max_attempts - 1:
                raise ValueError(f"Failed after {max_attempts} attempts: {e}")

            # Re-prompt with the error
            messages.append({"role": "assistant", "content": raw_text})
            messages.append({
                "role": "user",
                "content": (
                    f"Your response caused this error: {e}\n\n"
                    f"Please fix it and return ONLY valid JSON matching the schema. "
                    f"No preamble, no explanation."
                ),
            })

    raise ValueError("Max attempts exceeded")


async def main():
    prompt = (
        "Generate a product review for a mechanical keyboard. "
        "Return JSON with: product_name (str), rating (1-5 int), "
        "pros (list of str), cons (list of str), summary (str)."
    )

    try:
        if HAS_PYDANTIC:
            review = await generate_with_schema_repair(prompt, ProductReview)
            print(f"Validated review: {review.model_dump()}")
        else:
            print("Pydantic not installed — showing raw generation")
            resp = await client.messages.create(
                model=MODEL, max_tokens=512,
                messages=[{"role": "user", "content": prompt}]
            )
            print(resp.content[0].text)
    except ValueError as e:
        print(f"Failed: {e}")


asyncio.run(main())
```

## Solution 3: Format-Aware Coercer with Multiple Output Types

Handle JSON, YAML, numbered lists, and key-value pairs through a unified coercion interface.

```python
import asyncio
import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any
import anthropic

client = anthropic.AsyncAnthropic()
MODEL = "claude-haiku-4-5-20251001"


class OutputFormat(Enum):
    JSON = "json"
    NUMBERED_LIST = "numbered_list"
    KEY_VALUE = "key_value"
    CSV_ROW = "csv_row"


def coerce_numbered_list(text: str) -> list[str]:
    items = re.findall(r"^\s*\d+[\.\)]\s+(.+)", text, re.MULTILINE)
    if items:
        return [item.strip() for item in items]
    # Fallback: bullet points
    items = re.findall(r"^\s*[-•*]\s+(.+)", text, re.MULTILINE)
    return [item.strip() for item in items]


def coerce_key_value(text: str) -> dict[str, str]:
    result = {}
    for line in text.splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            result[key.strip().lower().replace(" ", "_")] = value.strip()
    return result


def coerce_csv_row(text: str) -> list[str]:
    # Find the first line that looks like CSV
    for line in text.splitlines():
        if "," in line and len(line) > 3:
            return [cell.strip().strip('"') for cell in line.split(",")]
    return []


def coerce_json_value(text: str) -> Any:
    match = re.search(r"(\{[\s\S]*\}|\[[\s\S]*\])", text)
    raw = match.group(1) if match else text.strip()
    raw = re.sub(r",\s*([}\]])", r"\1", raw)
    return json.loads(raw)


def coerce_output(text: str, fmt: OutputFormat) -> Any:
    if fmt == OutputFormat.JSON:
        return coerce_json_value(text)
    elif fmt == OutputFormat.NUMBERED_LIST:
        return coerce_numbered_list(text)
    elif fmt == OutputFormat.KEY_VALUE:
        return coerce_key_value(text)
    elif fmt == OutputFormat.CSV_ROW:
        return coerce_csv_row(text)
    raise ValueError(f"Unknown format: {fmt}")


FORMAT_INSTRUCTIONS = {
    OutputFormat.JSON: "Return ONLY valid JSON, no preamble.",
    OutputFormat.NUMBERED_LIST: "Return ONLY a numbered list (1. item, 2. item, ...).",
    OutputFormat.KEY_VALUE: "Return ONLY key: value pairs, one per line.",
    OutputFormat.CSV_ROW: "Return ONLY a single CSV row with comma-separated values.",
}


async def structured_generate(
    prompt: str,
    fmt: OutputFormat,
    max_retries: int = 2,
) -> Any:
    full_prompt = f"{prompt}\n\n{FORMAT_INSTRUCTIONS[fmt]}"
    messages = [{"role": "user", "content": full_prompt}]

    for attempt in range(max_retries + 1):
        resp = await client.messages.create(
            model=MODEL,
            max_tokens=512,
            messages=messages,
        )
        raw = resp.content[0].text
        try:
            return coerce_output(raw, fmt)
        except Exception as e:
            if attempt == max_retries:
                raise ValueError(f"Coercion failed after {max_retries + 1} attempts: {e}") from e
            messages.append({"role": "assistant", "content": raw})
            messages.append({
                "role": "user",
                "content": f"Error: {e}. {FORMAT_INSTRUCTIONS[fmt]} Try again."
            })


async def main():
    tests = [
        ("List 5 Python built-in functions.", OutputFormat.NUMBERED_LIST),
        ("Summarize Python in: name, type, year.", OutputFormat.KEY_VALUE),
        ('Return JSON: {{"lang": "Python", "year": 1991}}', OutputFormat.JSON),
    ]
    for prompt, fmt in tests:
        result = await structured_generate(prompt, fmt)
        print(f"[{fmt.value}] {prompt[:50]}")
        print(f"  → {result}\n")


asyncio.run(main())
```

## Solution 4: Speculative Parsing with Fallback Chain

Try parsing with decreasing strictness: exact schema → relaxed schema → free-form extraction.

```python
import asyncio
import json
import re
from typing import Any
import anthropic

client = anthropic.AsyncAnthropic()
MODEL = "claude-haiku-4-5-20251001"


def strict_parse(text: str, required_keys: list[str]) -> dict | None:
    """Parse JSON and verify all required keys are present."""
    try:
        match = re.search(r"\{[\s\S]+\}", text)
        data = json.loads(match.group(0) if match else text)
        if all(k in data for k in required_keys):
            return data
    except Exception:
        pass
    return None


def relaxed_parse(text: str, required_keys: list[str]) -> dict | None:
    """Extract values using key: value pattern matching."""
    result = {}
    for key in required_keys:
        pattern = rf'["\']?{re.escape(key)}["\']?\s*[:=]\s*["\']?([^"\'\\n,\}}]+)["\']?'
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            result[key] = match.group(1).strip().rstrip('",}')
    return result if len(result) == len(required_keys) else None


async def llm_extract(text: str, required_keys: list[str]) -> dict | None:
    """Ask the model to extract specific keys from its own output."""
    keys_str = ", ".join(f'"{k}"' for k in required_keys)
    resp = await client.messages.create(
        model=MODEL,
        max_tokens=200,
        messages=[
            {
                "role": "user",
                "content": (
                    f"Extract these fields from the text and return ONLY valid JSON: {keys_str}\n\n"
                    f"Text:\n{text}"
                ),
            }
        ],
    )
    try:
        match = re.search(r"\{[\s\S]+\}", resp.content[0].text)
        data = json.loads(match.group(0) if match else resp.content[0].text)
        if all(k in data for k in required_keys):
            return data
    except Exception:
        pass
    return None


async def speculative_parse(text: str, required_keys: list[str]) -> tuple[dict, str]:
    """
    Returns (result, strategy_used) using a fallback chain.
    Raises ValueError if all strategies fail.
    """
    result = strict_parse(text, required_keys)
    if result:
        return result, "strict"

    result = relaxed_parse(text, required_keys)
    if result:
        return result, "relaxed"

    result = await llm_extract(text, required_keys)
    if result:
        return result, "llm_extract"

    raise ValueError(f"All parse strategies failed for keys: {required_keys}")


async def main():
    # Simulate various LLM responses that need parsing
    responses = [
        '{"name": "Alice", "score": 92, "grade": "A"}',
        'The student name is Alice, score: 92, grade: A.',
        'Based on the assessment, Alice achieved a score of ninety-two points, earning grade A.',
    ]

    for resp in responses:
        try:
            result, strategy = await speculative_parse(resp, ["name", "score", "grade"])
            print(f"[{strategy}] {resp[:60]!r}")
            print(f"  → {result}\n")
        except ValueError as e:
            print(f"[FAILED] {e}")


asyncio.run(main())
```

## Solution 5: Output Template Injection

Inject a partial output template into the assistant turn to force the model to complete it in the right format.

```python
import asyncio
import json
import re
import anthropic

client = anthropic.AsyncAnthropic()
MODEL = "claude-haiku-4-5-20251001"


async def generate_with_template(
    prompt: str,
    template_start: str,
    model: str = MODEL,
) -> str:
    """
    Use assistant-turn prefilling to force structured output.
    The model will complete the template_start text.
    """
    resp = await client.messages.create(
        model=model,
        max_tokens=512,
        messages=[
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": template_start},
        ],
    )
    # Combine the injected start with the model's completion
    return template_start + resp.content[0].text


async def generate_json_object(prompt: str, keys: list[str]) -> dict:
    """Force JSON object output by injecting opening brace."""
    fields_hint = ", ".join(f'"{k}": ...' for k in keys)
    raw = await generate_with_template(
        prompt=f"{prompt}\nRespond with JSON only.",
        template_start="{",
    )
    # Ensure it's a complete JSON object
    if not raw.rstrip().endswith("}"):
        raw = raw.rstrip().rstrip(",") + "\n}"

    raw = re.sub(r",\s*([}\]])", r"\1", raw)  # Remove trailing commas

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Try adding closing brace
        try:
            return json.loads(raw + "}")
        except json.JSONDecodeError as e:
            raise ValueError(f"Template injection parse failed: {e}\nRaw: {raw}") from e


async def generate_json_array(prompt: str, expected_items: int) -> list:
    """Force JSON array output by injecting opening bracket."""
    raw = await generate_with_template(
        prompt=f"{prompt}\nRespond with a JSON array only.",
        template_start="[",
    )
    if not raw.rstrip().endswith("]"):
        raw = raw.rstrip().rstrip(",") + "\n]"
    raw = re.sub(r",\s*([}\]])", r"\1", raw)
    return json.loads(raw)


async def main():
    obj = await generate_json_object(
        "Describe the programming language Python with: name, creator, year_created, paradigm.",
        keys=["name", "creator", "year_created", "paradigm"],
    )
    print(f"JSON object: {json.dumps(obj, indent=2)}\n")

    arr = await generate_json_array(
        "List 3 popular Python web frameworks as strings.",
        expected_items=3,
    )
    print(f"JSON array: {arr}")


asyncio.run(main())
```

## Solution 6: Schema-Driven Generation with Validation Loop

Generate output guided by a JSON Schema definition, validate against it, and loop until valid.

```python
import asyncio
import json
import re
from typing import Any
import anthropic

client = anthropic.AsyncAnthropic()
MODEL = "claude-haiku-4-5-20251001"


def validate_against_schema(data: Any, schema: dict) -> list[str]:
    """Minimal JSON Schema validation (type + required + properties)."""
    errors = []
    schema_type = schema.get("type")

    if schema_type == "object":
        if not isinstance(data, dict):
            return [f"Expected object, got {type(data).__name__}"]
        for req in schema.get("required", []):
            if req not in data:
                errors.append(f"Missing required field: '{req}'")
        for prop, prop_schema in schema.get("properties", {}).items():
            if prop in data:
                sub_errors = validate_against_schema(data[prop], prop_schema)
                errors.extend(f"{prop}: {e}" for e in sub_errors)

    elif schema_type == "array":
        if not isinstance(data, list):
            return [f"Expected array, got {type(data).__name__}"]
        min_items = schema.get("minItems", 0)
        if len(data) < min_items:
            errors.append(f"Array has {len(data)} items, minimum is {min_items}")

    elif schema_type == "string":
        if not isinstance(data, str):
            errors.append(f"Expected string, got {type(data).__name__}")

    elif schema_type == "integer":
        if not isinstance(data, int) or isinstance(data, bool):
            errors.append(f"Expected integer, got {type(data).__name__}")
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if minimum is not None and isinstance(data, int) and data < minimum:
            errors.append(f"Value {data} < minimum {minimum}")
        if maximum is not None and isinstance(data, int) and data > maximum:
            errors.append(f"Value {data} > maximum {maximum}")

    return errors


async def schema_guided_generate(
    prompt: str,
    schema: dict,
    max_attempts: int = 3,
) -> Any:
    schema_str = json.dumps(schema, indent=2)
    messages = [
        {
            "role": "user",
            "content": (
                f"{prompt}\n\nReturn ONLY valid JSON conforming to this schema:\n{schema_str}"
            ),
        }
    ]

    for attempt in range(max_attempts):
        resp = await client.messages.create(
            model=MODEL, max_tokens=512, messages=messages
        )
        raw = resp.content[0].text
        match = re.search(r"(\{[\s\S]*\}|\[[\s\S]*\])", raw)
        raw_json = match.group(1) if match else raw

        try:
            data = json.loads(re.sub(r",\s*([}\]])", r"\1", raw_json))
        except json.JSONDecodeError as e:
            if attempt == max_attempts - 1:
                raise ValueError(f"JSON parse failed: {e}")
            messages.extend([
                {"role": "assistant", "content": raw},
                {"role": "user", "content": f"JSON parse error: {e}. Fix and return valid JSON only."},
            ])
            continue

        errors = validate_against_schema(data, schema)
        if not errors:
            return data

        if attempt == max_attempts - 1:
            raise ValueError(f"Schema validation failed: {errors}")

        messages.extend([
            {"role": "assistant", "content": raw},
            {"role": "user", "content": f"Validation errors: {errors}. Fix and return valid JSON only."},
        ])

    raise ValueError("Max attempts exceeded")


async def main():
    schema = {
        "type": "object",
        "required": ["name", "rating", "tags"],
        "properties": {
            "name": {"type": "string"},
            "rating": {"type": "integer", "minimum": 1, "maximum": 5},
            "tags": {"type": "array", "minItems": 2},
        },
    }

    result = await schema_guided_generate(
        "Create a review entry for the Python programming language.",
        schema,
    )
    print(f"Schema-validated result: {json.dumps(result, indent=2)}")
    print(f"Validation errors: {validate_against_schema(result, schema)}")


asyncio.run(main())
```

## Comparison

| Solution | Repair strategy | LLM repair calls | Strictness | Best for |
|---|---|---|---|---|
| **JSON extract-and-repair** | Text repair + LLM fallback | 0-1 | Medium | General JSON outputs |
| **Pydantic re-prompt** | Re-prompt with error | 0-N | High | Typed business objects |
| **Format-aware coercer** | Format-specific heuristics | 0-1 | Medium | Mixed output formats |
| **Speculative parsing** | Degrading strictness | 0-1 | Adaptive | Unpredictable output formats |
| **Template injection** | Prefill forcing | 0 | Medium | Predictable structure start |
| **Schema-driven loop** | Validation + re-prompt | 0-N | Highest | Schema-critical integrations |

Start with **JSON extract-and-repair** (Solution 1) — it handles 90% of cases with no extra API calls. Add **Pydantic re-prompt** (Solution 2) when you need type safety. Use **schema-driven loop** (Solution 6) for integrations where schema compliance is non-negotiable.
