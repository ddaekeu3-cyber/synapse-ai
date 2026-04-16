---
title: "Agent Doesn't Implement LLM Output Schema Enforcement"
description: "AI agents trust raw LLM text output and pass it downstream without schema validation, leading to injection, type confusion, and silent data corruption when the model deviates from the expected format."
category: security
difficulty: intermediate
tags: [schema, validation, pydantic, json, structured-output, safety, injection]
---

# Agent Doesn't Implement LLM Output Schema Enforcement

## Problem

LLMs are probabilistic — they can return malformed JSON, unexpected field types, extra keys with injected content, or entirely wrong structures. Without strict schema enforcement, downstream code that trusts LLM output is vulnerable to type confusion errors, silent data corruption, and prompt injection via crafted field values that propagate into tool calls or database writes.

## Solution 1: Pydantic Validation with Retry on Parse Failure

Define an exact output schema; parse and validate, retry with a correction prompt on failure.

```python
import json
import asyncio
from pydantic import BaseModel, Field, ValidationError, field_validator
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

class AgentAction(BaseModel):
    action: str = Field(..., pattern=r"^(search|write|read|done)$")
    target: str = Field(..., min_length=1, max_length=500)
    parameters: dict[str, str] = Field(default_factory=dict)
    confidence: float = Field(..., ge=0.0, le=1.0)

    @field_validator("target")
    @classmethod
    def no_injection(cls, v: str) -> str:
        # Reject values containing prompt-injection markers
        forbidden = ["<tool_use>", "SYSTEM:", "Human:", "Assistant:", "{{", "}}"]
        for marker in forbidden:
            if marker.lower() in v.lower():
                raise ValueError(f"Forbidden content in target: {marker}")
        return v

    @field_validator("parameters")
    @classmethod
    def sanitize_params(cls, v: dict) -> dict:
        # Ensure all values are strings and not excessively long
        return {
            k[:64]: str(val)[:1024]
            for k, val in v.items()
            if isinstance(k, str)
        }

SCHEMA_PROMPT = """
Respond ONLY with valid JSON matching this schema (no markdown, no explanation):
{
  "action": "search" | "write" | "read" | "done",
  "target": "<string>",
  "parameters": {"key": "value", ...},
  "confidence": 0.0-1.0
}
"""

async def get_validated_action(user_request: str, max_retries: int = 2) -> AgentAction:
    messages = [
        {"role": "user", "content": f"{SCHEMA_PROMPT}\n\nRequest: {user_request}"},
    ]
    for attempt in range(max_retries + 1):
        resp = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            messages=messages,
        )
        raw = resp.content[0].text.strip()
        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]

        try:
            data = json.loads(raw)
            return AgentAction(**data)
        except (json.JSONDecodeError, ValidationError) as e:
            if attempt == max_retries:
                raise ValueError(f"LLM output failed schema validation after {max_retries+1} attempts: {e}")
            # Add correction message and retry
            messages.append({"role": "assistant", "content": raw})
            messages.append({
                "role": "user",
                "content": f"Your response failed validation: {e}. Please fix and return ONLY valid JSON.",
            })
```

**When to use**: Any agent that routes actions based on LLM output. Pydantic catches type, range, and injection issues simultaneously.

---

## Solution 2: Anthropic Tool-Use as Schema Enforcement

Use Anthropic's tool-use feature — the model is constrained to return JSON matching the tool's input schema exactly.

```python
import asyncio
import json
from pydantic import BaseModel
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

# Define tool schema — model MUST return JSON matching this
DECISION_TOOL = {
    "name": "agent_decision",
    "description": "Record the agent's decision for this turn",
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["search", "write", "read", "summarize", "done"],
                "description": "The action to take",
            },
            "reasoning": {
                "type": "string",
                "maxLength": 300,
                "description": "Brief reasoning for this action",
            },
            "target": {
                "type": "string",
                "minLength": 1,
                "maxLength": 200,
            },
            "priority": {
                "type": "integer",
                "minimum": 1,
                "maximum": 5,
            },
        },
        "required": ["action", "reasoning", "target", "priority"],
        "additionalProperties": False,  # reject extra keys
    },
}

class AgentDecision(BaseModel):
    action: str
    reasoning: str
    target: str
    priority: int

async def get_schema_enforced_decision(context: str) -> AgentDecision:
    resp = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        tools=[DECISION_TOOL],
        tool_choice={"type": "any"},  # force tool use
        messages=[{"role": "user", "content": context}],
    )

    tool_blocks = [b for b in resp.content if b.type == "tool_use"]
    if not tool_blocks:
        raise ValueError("Model did not use the required tool")

    # Tool input is pre-validated by Anthropic's API against the schema
    data = tool_blocks[0].input
    return AgentDecision(**data)

async def main():
    decision = await get_schema_enforced_decision(
        "Analyze this codebase and decide what to do next: the tests are failing."
    )
    print(f"Action: {decision.action}, Priority: {decision.priority}")
```

**When to use**: Best-in-class schema enforcement. The API rejects malformed tool inputs before they reach your code.

---

## Solution 3: JSON Schema Validator with Strict Mode and Extra-Key Rejection

Validate LLM output against a JSON Schema with `additionalProperties: false` to prevent field injection.

```python
import json
import jsonschema
from jsonschema import Draft202012Validator, ValidationError
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

STRICT_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12",
    "type": "object",
    "properties": {
        "summary": {"type": "string", "maxLength": 500},
        "sentiment": {"type": "string", "enum": ["positive", "neutral", "negative"]},
        "topics": {
            "type": "array",
            "items": {"type": "string", "maxLength": 50},
            "maxItems": 10,
        },
        "score": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": ["summary", "sentiment", "topics", "score"],
    "additionalProperties": False,  # CRITICAL: blocks injected extra fields
}

validator = Draft202012Validator(STRICT_SCHEMA)

def extract_json(text: str) -> str:
    """Extract JSON from text that may contain markdown fences or preamble."""
    text = text.strip()
    if "```" in text:
        parts = text.split("```")
        for part in parts:
            part = part.strip().lstrip("json").strip()
            if part.startswith("{"):
                return part
    # Find first { ... } block
    start = text.find("{")
    end = text.rfind("}") + 1
    if start >= 0 and end > start:
        return text[start:end]
    return text

async def analyze_text(text: str) -> dict:
    resp = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=(
            "Respond ONLY with JSON. No markdown. No extra text. "
            "Fields: summary (str), sentiment (positive/neutral/negative), topics (array of str), score (0-1 float)."
        ),
        messages=[{"role": "user", "content": f"Analyze: {text}"}],
    )
    raw = extract_json(resp.content[0].text)

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"LLM returned invalid JSON: {e}\nRaw: {raw[:200]}")

    errors = list(validator.iter_errors(data))
    if errors:
        msgs = "; ".join(str(e.message) for e in errors[:3])
        raise ValueError(f"Schema validation failed: {msgs}")

    return data
```

**When to use**: Complex schemas with nested structures where Pydantic alone is insufficient.

---

## Solution 4: Output Allowlist Sanitizer for Safe Downstream Injection

After schema validation, sanitize each field to ensure no value can escape its context (SQL, shell, HTML).

```python
import re
import html
import json
from typing import Any

# Field-level sanitization rules
SANITIZERS: dict[str, list] = {
    "sql_safe": [
        lambda v: re.sub(r"['\";\\]", "", v),        # strip SQL metacharacters
        lambda v: re.sub(r"--.*$", "", v, flags=re.M),  # strip SQL comments
        lambda v: v[:512],                              # hard length cap
    ],
    "shell_safe": [
        lambda v: re.sub(r"[;&|`$\\!]", "", v),      # strip shell metacharacters
        lambda v: re.sub(r"\.\./", "", v),             # strip path traversal
        lambda v: v[:256],
    ],
    "html_safe": [
        html.escape,
        lambda v: v[:2000],
    ],
    "path_safe": [
        lambda v: re.sub(r"[^a-zA-Z0-9._\-/]", "_", v),  # allowlist chars only
        lambda v: re.sub(r"\.{2,}", ".", v),               # collapse ..
        lambda v: v.lstrip("/"),                            # no absolute paths
        lambda v: v[:256],
    ],
    "identifier_safe": [
        lambda v: re.sub(r"[^a-zA-Z0-9_]", "", v),   # alphanumeric + underscore only
        lambda v: v[:64],
    ],
}

FIELD_POLICIES: dict[str, str] = {
    "table_name": "identifier_safe",
    "file_path": "path_safe",
    "query": "sql_safe",
    "shell_cmd": "shell_safe",
    "display_text": "html_safe",
}

def sanitize_value(value: Any, policy_name: str) -> str:
    if not isinstance(value, str):
        value = str(value)
    for fn in SANITIZERS[policy_name]:
        value = fn(value)
    return value

def sanitize_output(data: dict, policies: dict[str, str] | None = None) -> dict:
    """Apply field-level sanitization to validated LLM output."""
    if policies is None:
        policies = FIELD_POLICIES
    result = {}
    for key, value in data.items():
        if key in policies:
            result[key] = sanitize_value(value, policies[key])
        else:
            result[key] = value
    return result

# Combined pipeline: validate then sanitize
def safe_parse_output(raw_json: str, pydantic_model, field_policies: dict) -> dict:
    data = json.loads(raw_json)
    validated = pydantic_model(**data).model_dump()
    return sanitize_output(validated, field_policies)
```

**When to use**: When validated LLM output flows into SQL queries, shell commands, file paths, or HTML rendering.

---

## Solution 5: Schema Version Registry for Evolving Outputs

Track output schemas by version; validate against the correct schema and detect breaking changes.

```python
import json
from typing import Any
from pydantic import BaseModel

class SchemaRegistry:
    """Manages versioned output schemas with validation and migration."""

    def __init__(self):
        self._schemas: dict[str, dict[str, type[BaseModel]]] = {}
        self._current: dict[str, str] = {}  # schema_name → current version

    def register(self, name: str, version: str, model: type[BaseModel]):
        self._schemas.setdefault(name, {})[version] = model
        self._current[name] = version  # last registered = current

    def validate(self, name: str, data: dict, version: str | None = None) -> BaseModel:
        version = version or self._current[name]
        schema_versions = self._schemas.get(name)
        if not schema_versions:
            raise KeyError(f"Unknown schema: {name}")
        model = schema_versions.get(version)
        if not model:
            raise KeyError(f"Unknown version {version} for schema {name}")
        return model(**data)

    def list_versions(self, name: str) -> list[str]:
        return list(self._schemas.get(name, {}).keys())

# Define versioned schemas
class AgentOutputV1(BaseModel):
    result: str
    confidence: float

class AgentOutputV2(BaseModel):
    result: str
    confidence: float
    sources: list[str] = []      # new field in v2
    reasoning: str = ""          # new field in v2

registry = SchemaRegistry()
registry.register("agent_output", "v1", AgentOutputV1)
registry.register("agent_output", "v2", AgentOutputV2)

def parse_with_version(raw: str, schema_name: str = "agent_output") -> BaseModel:
    data = json.loads(raw)
    # Detect version from output or default to latest
    version = data.pop("schema_version", None) or registry._current[schema_name]
    return registry.validate(schema_name, data, version)

# Usage
output_v1 = '{"result": "Paris", "confidence": 0.97}'
output_v2 = '{"result": "Paris", "confidence": 0.97, "sources": ["wiki"], "reasoning": "Capital city."}'

parsed_v1 = parse_with_version(output_v1)   # → AgentOutputV1
parsed_v2 = parse_with_version(output_v2)   # → AgentOutputV2 (latest)
```

**When to use**: Long-lived agents where schema evolution must be backward compatible.

---

## Solution 6: Output Constraint via Structured Output Mode + Post-Validation

Use Claude's JSON mode (via system prompt + temperature 0) combined with post-validation for defense-in-depth.

```python
import asyncio
import json
import logging
from pydantic import BaseModel, Field
from anthropic import AsyncAnthropic

logger = logging.getLogger(__name__)
client = AsyncAnthropic()

class SearchResult(BaseModel):
    query: str = Field(min_length=1, max_length=200)
    results: list[str] = Field(max_length=10)
    total_found: int = Field(ge=0, le=10000)
    truncated: bool

CONSTRAINED_SYSTEM = """\
You are a JSON API. You MUST respond with valid JSON only.
Never include markdown formatting, code fences, or explanatory text.
Never include fields not in the schema.
Schema: {"query": str, "results": [str, ...max 10], "total_found": int, "truncated": bool}"""

async def constrained_search(user_query: str) -> SearchResult:
    resp = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=CONSTRAINED_SYSTEM,
        messages=[{"role": "user", "content": f"Search for: {user_query}"}],
        temperature=0,  # minimize randomness in format
    )

    raw = resp.content[0].text.strip()

    # Log raw output for audit
    logger.debug("llm_raw_output", extra={"raw": raw[:500], "query": user_query})

    # Parse and validate
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.error("json_parse_failed", extra={"raw": raw[:200], "error": str(e)})
        raise

    try:
        result = SearchResult(**data)
    except Exception as e:
        logger.error("schema_validation_failed", extra={"data": data, "error": str(e)})
        raise ValueError(f"LLM output failed schema enforcement: {e}") from e

    # Post-validation: ensure no results contain injection payloads
    for item in result.results:
        if any(marker in item for marker in ["<script", "javascript:", "data:", "\x00"]):
            raise ValueError(f"Potential injection detected in result: {item[:50]}")

    return result
```

**When to use**: Search agents, data pipelines, any agent whose output is rendered in a browser or stored in a database.

---

## Comparison

| Solution | Schema Source | Injection Defense | Retry Logic | Versioning | Best For |
|---|---|---|---|---|---|
| Pydantic + retry | Python model | Field validators | Yes | No | Most agent actions |
| Tool-use enforcement | API-enforced | API-level | No (handled by API) | No | Strongest enforcement |
| JSON Schema strict | JSON Schema | `additionalProperties: false` | No | No | Complex nested schemas |
| Output sanitizer | Field policies | Context-aware (SQL/shell/HTML) | No | No | Downstream injection prevention |
| Schema version registry | Version-aware | Pydantic | No | Yes | Evolving long-lived agents |
| Constrained system + post-validate | System prompt + Pydantic | Custom checks | No | No | Defense-in-depth |

**Rule of thumb**: Use tool-use schema enforcement as the primary mechanism. Always add a Pydantic post-validation layer. Apply context-aware sanitization before SQL/shell/HTML injection points.
