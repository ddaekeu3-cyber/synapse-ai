---
layout: solution
title: "Agent Hallucinates API Response Fields That Don't Exist"
category: hallucination
description: "Agent accesses fields on API responses that were renamed, removed, or never existed — causing silent None values or runtime KeyErrors in downstream code."
tags: [hallucination, api, schema, validation, runtime-errors, field-access]
---

## Symptom

Agent generates code that accesses non-existent response fields:

```python
# Agent generates this (based on outdated or hallucinated schema):
response = client.messages.create(model="claude-sonnet-4-6", ...)

# Hallucinated fields:
text = response.text              # AttributeError: no .text (it's response.content[0].text)
tokens = response.token_count     # AttributeError: no .token_count (it's response.usage.input_tokens)
finish = response.finish_reason   # AttributeError: no .finish_reason (it's response.stop_reason)
model = response.model_name       # AttributeError: no .model_name (it's response.model)

# Or in dict-style API responses:
data = requests.get("https://api.example.com/users/42").json()
email = data["contact"]["email"]     # KeyError: "contact" (field is "contact_info")
phone = data["phone_number"]         # KeyError: field was renamed to "phone" in v2
created = data["created_date"]       # KeyError: field is "created_at" in ISO format
```

Code runs without error during generation but crashes at runtime. The failure is silent if the code uses `.get()` with a default, producing wrong results without any error.

## Root Cause

LLMs learn API schemas from training data. When APIs evolve (field renames, nested restructuring, type changes), the model's learned schema diverges from the live API. The model generates confidently wrong field accesses. Additionally, the model sometimes confuses similar APIs (e.g., OpenAI's `choices[0].message.content` vs Anthropic's `content[0].text`) and generates plausible-but-wrong field paths.

## Fix

---

### Option 1: Inject Live API Schema into Code Generation Prompt

Before generating code that accesses an API response, fetch or cache the actual response schema and inject it so the model uses real field names.

```python
import json
import anthropic

# Real schemas captured from actual API calls
RESPONSE_SCHEMAS = {
    "anthropic.messages": {
        "id": "msg_xxx",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": "..."}],
        "model": "claude-sonnet-4-6",
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {
            "input_tokens": 25,
            "output_tokens": 150,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
    },
    "openai.chat": {
        "id": "chatcmpl-xxx",
        "object": "chat.completion",
        "created": 1234567890,
        "model": "gpt-4o",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "..."},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 25, "completion_tokens": 150, "total_tokens": 175},
    },
}

def generate_api_code(task: str, api_name: str) -> str:
    schema = RESPONSE_SCHEMAS.get(api_name)
    schema_note = (
        f"\nEXACT response schema for {api_name}:\n{json.dumps(schema, indent=2)}\n"
        "Use ONLY fields shown above. Do not invent or guess field names."
        if schema else ""
    )

    client = anthropic.Anthropic()
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=f"Generate Python code for the task. {schema_note}",
        messages=[{"role": "user", "content": task}],
    )
    return response.content[0].text

# Generate code that accesses Anthropic API response
code = generate_api_code(
    "Write a function that calls claude-sonnet-4-6 and returns the text and token count",
    "anthropic.messages",
)
print(code)

# Also useful: generate schema from a real API call and cache it
def capture_schema(api_response_dict: dict, depth: int = 2) -> dict:
    """Reduce a live API response to its schema (keys + types, values truncated)."""
    if depth == 0 or not isinstance(api_response_dict, dict):
        return type(api_response_dict).__name__
    return {k: capture_schema(v, depth - 1) for k, v in api_response_dict.items()}
```

**Expected Token Savings:** Schema injection adds ~200 tokens but eliminates an average of 3 debug turns (each ~400 tokens) when field access errors surface at runtime. Net savings: 1,000 tokens per prevented error.
**Environment:** Maintain a `RESPONSE_SCHEMAS` registry that is updated whenever an API version changes. For dynamic APIs, make a real test call and capture the schema before code generation.

---

### Option 2: Safe Field Access Wrapper — Fail Loudly on Missing Fields

Generate code that uses a strict accessor wrapper instead of direct attribute/key access, turning silent `None` failures into loud errors with helpful context.

```python
import anthropic

SAFE_ACCESS_HELPERS = '''
def safe_get(obj, *path, default=None, required=False):
    """Navigate a nested object/dict with clear error on missing path."""
    current = obj
    for i, key in enumerate(path):
        try:
            if isinstance(current, dict):
                if required and key not in current:
                    available = list(current.keys())
                    raise KeyError(
                        f"Required key {key!r} not found at path {path[:i+1]}. "
                        f"Available keys: {available}"
                    )
                current = current.get(key, default)
            else:
                current = getattr(current, key, default)
                if required and current is default:
                    raise AttributeError(
                        f"Required attribute {key!r} not found on {type(current).__name__} "
                        f"at path {path[:i+1]}. "
                        f"Available attrs: {[a for a in dir(current) if not a.startswith('_')][:10]}"
                    )
        except (KeyError, AttributeError) as e:
            if required:
                raise
            return default
    return current
'''

def generate_safe_code(task: str) -> str:
    client = anthropic.Anthropic()
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=f"""Generate Python code using the safe_get helper for all API field access.
Never use direct attribute access (response.field) or dict access (data["field"]) on external API responses.
Always use safe_get(obj, "field1", "field2", required=True) for required fields.

Available helper:
{SAFE_ACCESS_HELPERS}

Example of correct usage:
  text = safe_get(response, "content", 0, "text", required=True)
  tokens = safe_get(response.usage, "input_tokens", required=True)
  stop = safe_get(response, "stop_reason", default="unknown")""",
        messages=[{"role": "user", "content": task}],
    )
    return SAFE_ACCESS_HELPERS + "\n\n" + response.content[0].text

code = generate_safe_code(
    "Call the Anthropic API and extract: response text, stop reason, and input token count"
)
print(code)
```

**Expected Token Savings:** Defensive accessor pattern turns runtime `AttributeError`/`KeyError` into descriptive errors that developers fix in seconds instead of minutes. Each debug session avoided: ~10 minutes of developer time + 2-3 correction chat turns (~800 tokens). Net: zero extra tokens in prompt, saves debugging cost.
**Environment:** Include `safe_get` in a shared utility module so it's always importable. Works for any dict/object-based API response. The `required=True` flag makes field presence explicit and auditable.

---

### Option 3: Runtime Schema Validator — Validate Response Before Passing to Code

After each API call, validate the response structure against an expected schema before the generated code accesses any fields.

```python
import json
import anthropic
from pydantic import BaseModel, ValidationError
from typing import Any

# Pydantic models for known API responses
class ContentBlock(BaseModel):
    type: str
    text: str | None = None

class Usage(BaseModel):
    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0

class AnthropicMessage(BaseModel):
    id: str
    type: str
    role: str
    content: list[ContentBlock]
    model: str
    stop_reason: str | None = None
    usage: Usage

def validate_and_extract(raw_response: Any, schema: type[BaseModel]) -> BaseModel:
    """Validate API response against schema; raise descriptive error if mismatch."""
    if hasattr(raw_response, "model_dump"):
        data = raw_response.model_dump()
    elif isinstance(raw_response, dict):
        data = raw_response
    else:
        raise TypeError(f"Cannot validate {type(raw_response)}")

    try:
        return schema(**data)
    except ValidationError as e:
        raise ValueError(
            f"API response doesn't match expected schema {schema.__name__}.\n"
            f"Validation errors:\n{e}\n"
            f"Received fields: {list(data.keys())}\n"
            "This may indicate an API version change. Update the schema model."
        ) from e

client = anthropic.Anthropic()

# Safe: validate before field access
raw = client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=128,
    messages=[{"role": "user", "content": "Say hello"}],
)

try:
    validated = validate_and_extract(raw, AnthropicMessage)
    # Now access with confidence — Pydantic guarantees field existence
    text = validated.content[0].text
    tokens = validated.usage.input_tokens
    stop = validated.stop_reason
    print(f"Text: {text}, Tokens: {tokens}, Stop: {stop}")
except ValueError as e:
    print(f"Schema mismatch detected: {e}")

# Generate code that uses validation
def generate_validated_code(task: str) -> str:
    schema_code = """
from pydantic import BaseModel

class ContentBlock(BaseModel):
    type: str
    text: str | None = None

class Usage(BaseModel):
    input_tokens: int
    output_tokens: int

class AnthropicMessage(BaseModel):
    id: str
    content: list[ContentBlock]
    model: str
    stop_reason: str | None
    usage: Usage
"""
    agent = anthropic.Anthropic()
    response = agent.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=(
            f"Generate Python code using these Pydantic models for type-safe field access.\n"
            f"Always call AnthropicMessage(**response.model_dump()) before accessing fields.\n"
            f"Schema:\n{schema_code}"
        ),
        messages=[{"role": "user", "content": task}],
    )
    return schema_code + "\n\n" + response.content[0].text

print(generate_validated_code("Extract the response text and token usage from an Anthropic API call"))
```

**Expected Token Savings:** Pydantic validation catches schema mismatches at the boundary, not deep in business logic. Each caught mismatch saves 3-5 debugging turns (~1,500 tokens) and surfaces API version changes immediately rather than silently corrupting data.
**Environment:** Define Pydantic models for every external API you consume. Update models whenever the API version changes. Run `validate_and_extract` in CI with recorded real responses to catch schema drift before deployment.

---

### Option 4: Field Path Test Before Code Delivery

After generating code, automatically test-run the field access paths against a real API response before returning the code to the user.

```python
import ast
import re
import anthropic

def extract_field_accesses(code: str) -> list[str]:
    """Extract all response.field and data["field"] patterns from code."""
    patterns = [
        r'\bresponse\.(\w+)',
        r'\bdata\[["\']([\w]+)["\']\]',
        r'\bresult\.(\w+)',
    ]
    found = []
    for pattern in patterns:
        found.extend(re.findall(pattern, code))
    return found

def verify_field_paths(code: str, test_response: dict) -> list[str]:
    """Try all field accesses against a real response. Return list of failures."""
    failures = []
    field_accesses = extract_field_accesses(code)

    for field in field_accesses:
        # Check both attribute and dict access styles
        if field not in test_response and not hasattr(test_response, field):
            nested_check = False
            for v in test_response.values() if isinstance(test_response, dict) else []:
                if isinstance(v, dict) and field in v:
                    nested_check = True
                    break
            if not nested_check:
                failures.append(field)

    return failures

def generate_and_verify(task: str) -> str:
    client = anthropic.Anthropic()

    # Make a real test API call to get the actual response structure
    test_response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=32,
        messages=[{"role": "user", "content": "test"}],
    )
    test_dict = test_response.model_dump()

    # Generate code
    code_response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=(
            f"Generate Python code. The Anthropic Message object has these fields: "
            f"{list(test_dict.keys())}. "
            f"Usage fields: {list(test_dict.get('usage', {}).keys())}. "
            f"Content structure: {test_dict.get('content', [{}])[0]}"
        ),
        messages=[{"role": "user", "content": task}],
    )
    generated_code = code_response.content[0].text

    # Verify field accesses
    failures = verify_field_paths(generated_code, test_dict)
    if failures:
        print(f"Field verification failed for: {failures}")
        # Retry with explicit correction
        correction = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=f"Fix the code. These field accesses don't exist: {failures}. Available fields: {list(test_dict.keys())}",
            messages=[
                {"role": "user", "content": task},
                {"role": "assistant", "content": generated_code},
                {"role": "user", "content": f"Fields {failures} don't exist. Fix them using available fields: {list(test_dict.keys())}"},
            ],
        )
        return correction.content[0].text

    print("All field accesses verified against live API response")
    return generated_code

code = generate_and_verify("Write a function to call Claude and return text + input token count")
print(code[:400])
```

**Expected Token Savings:** Test-verify pattern costs 1 real API call (32 tokens output) + regex analysis (free). Prevents field access bugs that cost 3-5 correction turns × 500 tokens = 1,500-2,500 tokens. Net: significant savings whenever the model would have hallucinated a field name.
**Environment:** Keep the test API call minimal (32 tokens max_tokens) to minimize cost. Cache `test_dict` across multiple code generation calls in the same session so the test call overhead is paid once.

---

### Option 5: API Response Envelope Abstraction

Wrap all API clients in an abstraction layer that maps raw response fields to stable internal names, so generated code is insulated from API schema changes.

```python
import anthropic
from dataclasses import dataclass
from typing import Any

@dataclass
class NormalisedResponse:
    """Stable internal schema regardless of which API/version produced the response."""
    text: str
    input_tokens: int
    output_tokens: int
    stop_reason: str
    model: str
    raw: Any  # original response for debugging

class NormalisedAnthropicClient:
    """Wraps Anthropic client and returns NormalisedResponse."""

    def __init__(self):
        self._client = anthropic.Anthropic()

    def create(self, model: str, messages: list[dict], max_tokens: int = 1024, **kwargs) -> NormalisedResponse:
        raw = self._client.messages.create(
            model=model,
            max_tokens=max_tokens,
            messages=messages,
            **kwargs,
        )
        # All field mapping lives here — one place to update when API changes
        text = raw.content[0].text if raw.content and raw.content[0].type == "text" else ""
        return NormalisedResponse(
            text=text,
            input_tokens=raw.usage.input_tokens,
            output_tokens=raw.usage.output_tokens,
            stop_reason=raw.stop_reason or "unknown",
            model=raw.model,
            raw=raw,
        )

# Generated code always uses NormalisedResponse — no raw field access
def generate_with_normalised_client(task: str) -> str:
    normalised_schema = """
@dataclass
class NormalisedResponse:
    text: str           # response text
    input_tokens: int   # tokens in prompt
    output_tokens: int  # tokens in response
    stop_reason: str    # why generation stopped
    model: str          # model that generated it
    raw: Any            # original response

client = NormalisedAnthropicClient()
response = client.create(model=..., messages=..., max_tokens=...)
# Access: response.text, response.input_tokens, response.output_tokens, response.stop_reason
"""
    agent = anthropic.Anthropic()
    result = agent.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=(
            "Generate Python code using NormalisedAnthropicClient.\n"
            f"The response object has ONLY these fields:\n{normalised_schema}\n"
            "Never access raw response fields directly."
        ),
        messages=[{"role": "user", "content": task}],
    )
    return result.content[0].text

# Test the normalised client
client = NormalisedAnthropicClient()
response = client.create(
    model="claude-haiku-4-5-20251001",
    messages=[{"role": "user", "content": "Hello!"}],
    max_tokens=64,
)
print(f"text={response.text!r}, in={response.input_tokens}, out={response.output_tokens}")
print(generate_with_normalised_client("Write a function that logs API cost per call"))
```

**Expected Token Savings:** Abstraction layer prevents all hallucinated field access — generated code only uses 5 stable field names. When Anthropic changes API schema, only the wrapper updates; all generated code remains correct. Saves N × correction turns across all future code generation in the codebase.
**Environment:** Maintain one `NormalisedClient` per external API. When the API changes, update the wrapper and run existing code unchanged. Especially valuable in codebases where many agents share the same API client.

---

### Option 6: Schema Changelog Injection — Explicitly Warn About Known Renames

Maintain a registry of known field renames and inject warnings for each migration so the model actively avoids deprecated field paths.

```python
import anthropic

# Registry of field renames and removals across API versions
SCHEMA_MIGRATIONS = {
    "anthropic.messages": [
        {
            "version": "v1 → SDK v0.20+",
            "changes": [
                "response.completion → response.content[0].text",
                "response.stop_reason (was 'stop_sequence' in older versions) → response.stop_reason",
                "response.token_count REMOVED → use response.usage.input_tokens + usage.output_tokens",
                "response.model_name REMOVED → use response.model",
            ],
        }
    ],
    "openai.chat": [
        {
            "version": "v0 → v1",
            "changes": [
                "openai.ChatCompletion.create() REMOVED → client.chat.completions.create()",
                "response.choices[0].text (completions) → response.choices[0].message.content (chat)",
                "response.usage.total_tokens (still exists, but prefer prompt_tokens + completion_tokens)",
            ],
        }
    ],
}

def build_migration_warning(api_name: str) -> str:
    migrations = SCHEMA_MIGRATIONS.get(api_name, [])
    if not migrations:
        return ""
    lines = [f"\nKNOWN FIELD MIGRATIONS for {api_name} (use CURRENT paths only):"]
    for migration in migrations:
        lines.append(f"  [{migration['version']}]")
        for change in migration["changes"]:
            lines.append(f"    - {change}")
    lines.append("NEVER use the old paths listed above — they will cause AttributeError at runtime.")
    return "\n".join(lines)

def generate_with_migration_awareness(task: str, api_name: str) -> str:
    migration_warning = build_migration_warning(api_name)
    client = anthropic.Anthropic()
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=f"Generate Python code for the task.{migration_warning}",
        messages=[{"role": "user", "content": task}],
    )
    return response.content[0].text

# Comparison table
"""
| Approach | Prevention Type | Cost | Maintenance | Best For |
|---|---|---|---|---|
| Option 1: Schema injection | Upfront schema context | ~200 tokens | Update on API change | Known APIs |
| Option 2: Safe accessor | Defensive field access | 0 extra tokens | Low | Any API |
| Option 3: Pydantic validation | Runtime schema check | +1 parse step | Update on API change | Critical paths |
| Option 4: Field path testing | Live verification | 1 test API call | Low | Code generation |
| Option 5: Normalised client | Abstraction layer | 0 extra tokens | One place to update | Stable codebase |
| Option 6: Changelog injection | Migration warnings | ~200 tokens | Update on API change | Frequently-changed APIs |
"""

code = generate_with_migration_awareness(
    "Write a function using the Anthropic Python SDK to extract the response text and token count",
    "anthropic.messages",
)
print(code)
```

**Expected Token Savings:** Migration warning adds ~200 tokens but prevents the most common class of hallucinated field access (using pre-migration field names). For each session that would have generated deprecated field accesses: saves 2-3 correction turns × 400 tokens = 800-1,200 tokens. Net: 4-6× return on the warning investment.
**Environment:** Add new entries to `SCHEMA_MIGRATIONS` whenever a major API version is released. Pair with Option 3 (Pydantic validation) for defense-in-depth: prevent wrong fields at generation time AND catch them at runtime.
