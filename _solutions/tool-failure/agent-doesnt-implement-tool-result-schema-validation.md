---
layout: solution
title: "Agent Doesn't Implement Tool Result Schema Validation"
category: tool-failure
description: "Agent blindly trusts tool output structure, causing downstream KeyError crashes, silent misinterpretations, or hallucinated values when external APIs change their response shape."
tags: [tool-failure, schema-validation, pydantic, robustness, type-safety]
---

# Agent Doesn't Implement Tool Result Schema Validation

## Problem

When an agent calls a tool and receives a result, it typically passes the raw output directly into its context or processing logic. If the tool's API changes a field name, returns a partial object on error, or sends an unexpected type, the agent either crashes with a `KeyError`/`TypeError`, silently uses a wrong value, or hallucinates a plausible substitute—leading to corrupted downstream reasoning.

**Root cause:** No validation layer between raw tool output and agent consumption.

**Symptoms:**
- `KeyError: 'data'` mid-task when API renames a field
- Agent confidently uses `None` as a numeric value
- Hallucinated substitutions when expected keys are missing
- Silent wrong behavior that only surfaces much later

---

## Option 1: Pydantic Schema with Strict Validation

Define expected tool output shapes with Pydantic and reject anything that doesn't conform.

```python
import anthropic
import json
from pydantic import BaseModel, ValidationError, Field
from typing import Optional

client = anthropic.Anthropic()

# Define expected tool output schemas
class WeatherResult(BaseModel):
    temperature: float = Field(..., ge=-100, le=100)
    condition: str
    humidity: Optional[float] = Field(None, ge=0, le=100)
    city: str

class SearchResult(BaseModel):
    results: list[dict]
    total_count: int = Field(..., ge=0)
    query: str

# Schema registry keyed by tool name
TOOL_SCHEMAS: dict[str, type[BaseModel]] = {
    "get_weather": WeatherResult,
    "search_web": SearchResult,
}

def validate_tool_result(tool_name: str, raw_result: dict) -> dict:
    """Validate tool result against registered schema."""
    schema_class = TOOL_SCHEMAS.get(tool_name)
    if schema_class is None:
        # No schema registered — return as-is with a warning
        return {"validated": False, "data": raw_result, "warning": f"No schema for {tool_name}"}

    try:
        validated = schema_class(**raw_result)
        return {"validated": True, "data": validated.model_dump()}
    except ValidationError as e:
        errors = e.errors()
        return {
            "validated": False,
            "data": None,
            "error": f"Schema validation failed for {tool_name}",
            "fields": [f"{err['loc']}: {err['msg']}" for err in errors]
        }

def mock_tool_call(tool_name: str, tool_input: dict) -> dict:
    """Simulate tool calls — some return malformed data."""
    if tool_name == "get_weather":
        # Simulate API returning wrong field name ("temp" instead of "temperature")
        return {"temp": 22.5, "condition": "sunny", "city": "Seoul"}
    elif tool_name == "search_web":
        return {"results": [{"title": "Result 1"}], "total_count": 1, "query": tool_input.get("q", "")}
    return {}

tools = [
    {
        "name": "get_weather",
        "description": "Get current weather for a city",
        "input_schema": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"]
        }
    },
    {
        "name": "search_web",
        "description": "Search the web",
        "input_schema": {
            "type": "object",
            "properties": {"q": {"type": "string"}},
            "required": ["q"]
        }
    }
]

def run_agent_with_schema_validation(user_query: str) -> str:
    messages = [{"role": "user", "content": user_query}]

    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            tools=tools,
            messages=messages
        )

        if response.stop_reason == "end_turn":
            return next(b.text for b in response.content if hasattr(b, "text"))

        if response.stop_reason != "tool_use":
            break

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue

            raw = mock_tool_call(block.name, block.input)
            validated = validate_tool_result(block.name, raw)

            if validated["validated"]:
                result_content = json.dumps(validated["data"])
            else:
                # Return error to agent so it can reason about it
                result_content = json.dumps({
                    "error": validated.get("error", "Validation failed"),
                    "details": validated.get("fields", []),
                    "raw_available_keys": list(raw.keys()) if raw else []
                })

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": result_content
            })

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

    return "Agent stopped unexpectedly"

result = run_agent_with_schema_validation("What's the weather in Seoul?")
print(result)

# Expected Token Savings: ~0% (validation overhead is local; prevents costly retry loops caused by corrupted state)
# Environment: Any production agent using external APIs with evolving schemas
```

---

## Option 2: JSON Schema Validation with `jsonschema`

Use JSON Schema draft-7 for lightweight validation without Pydantic dependency.

```python
import anthropic
import json
import jsonschema
from jsonschema import validate, ValidationError as JsonValidationError

client = anthropic.Anthropic()

TOOL_OUTPUT_SCHEMAS = {
    "get_stock_price": {
        "type": "object",
        "required": ["symbol", "price", "currency"],
        "properties": {
            "symbol": {"type": "string", "minLength": 1},
            "price": {"type": "number", "minimum": 0},
            "currency": {"type": "string", "enum": ["USD", "EUR", "KRW", "JPY"]},
            "change_percent": {"type": "number"}
        },
        "additionalProperties": True
    },
    "get_user_profile": {
        "type": "object",
        "required": ["user_id", "email"],
        "properties": {
            "user_id": {"type": "integer"},
            "email": {"type": "string", "format": "email"},
            "name": {"type": "string"},
            "role": {"type": "string", "enum": ["admin", "user", "viewer"]}
        }
    }
}

class ToolResultValidator:
    def __init__(self, schemas: dict):
        self.schemas = schemas
        self.validation_log = []

    def validate(self, tool_name: str, result: dict) -> tuple[bool, dict, str | None]:
        """Returns (is_valid, cleaned_result, error_message)."""
        schema = self.schemas.get(tool_name)
        if not schema:
            return True, result, None  # No schema = pass through

        try:
            validate(instance=result, schema=schema)
            self.validation_log.append({"tool": tool_name, "status": "pass"})
            return True, result, None
        except JsonValidationError as e:
            error_msg = f"Field '{'.'.join(str(p) for p in e.absolute_path)}': {e.message}" if e.absolute_path else e.message
            self.validation_log.append({"tool": tool_name, "status": "fail", "error": error_msg})
            return False, {}, error_msg

validator = ToolResultValidator(TOOL_OUTPUT_SCHEMAS)

def mock_stock_tool(symbol: str) -> dict:
    # Simulate a bug: price returned as string instead of number
    return {"symbol": symbol, "price": "150.25", "currency": "USD"}

tools = [
    {
        "name": "get_stock_price",
        "description": "Get current stock price",
        "input_schema": {
            "type": "object",
            "properties": {"symbol": {"type": "string"}},
            "required": ["symbol"]
        }
    }
]

def run_with_jsonschema_validation(query: str) -> str:
    messages = [{"role": "user", "content": query}]

    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=tools,
            messages=messages
        )

        if response.stop_reason == "end_turn":
            return next(b.text for b in response.content if hasattr(b, "text"))

        if response.stop_reason != "tool_use":
            break

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue

            raw = mock_stock_tool(block.input.get("symbol", ""))
            is_valid, clean, error = validator.validate(block.name, raw)

            if is_valid:
                content = json.dumps(clean)
            else:
                # Attempt auto-coercion for common type mismatches
                coerced = dict(raw)
                if "price" in coerced and isinstance(coerced["price"], str):
                    try:
                        coerced["price"] = float(coerced["price"])
                        is_valid2, clean2, error2 = validator.validate(block.name, coerced)
                        content = json.dumps(clean2) if is_valid2 else json.dumps({"validation_error": error, "raw": raw})
                    except ValueError:
                        content = json.dumps({"validation_error": error, "raw": raw})
                else:
                    content = json.dumps({"validation_error": error, "raw": raw})

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": content
            })

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

    return "Done"

print(run_with_jsonschema_validation("What is Apple's stock price?"))
print("Validation log:", validator.validation_log)

# Expected Token Savings: ~5-15% (prevents retry turns caused by bad data interpretation)
# Environment: Agents with strict API contracts; CI environments validating tool integrations
```

---

## Option 3: Typed Dataclass Validation with Coercion

Use Python dataclasses with post-init validation and automatic type coercion.

```python
import anthropic
import json
from dataclasses import dataclass, field, fields
from typing import Any, get_type_hints

client = anthropic.Anthropic()

@dataclass
class ValidatedToolResult:
    tool_name: str
    raw_data: dict
    is_valid: bool
    errors: list[str] = field(default_factory=list)
    coerced_data: dict = field(default_factory=dict)

def coerce_and_validate(tool_name: str, raw: dict, expected_types: dict[str, type]) -> ValidatedToolResult:
    """Coerce values to expected types where possible, collect errors for remainder."""
    result = ValidatedToolResult(tool_name=tool_name, raw_data=raw, is_valid=True)
    coerced = {}

    for field_name, expected_type in expected_types.items():
        if field_name not in raw:
            result.errors.append(f"Missing required field: {field_name}")
            result.is_valid = False
            continue

        value = raw[field_name]

        if isinstance(value, expected_type):
            coerced[field_name] = value
        else:
            # Attempt coercion
            try:
                coerced[field_name] = expected_type(value)
            except (ValueError, TypeError) as e:
                result.errors.append(f"Cannot coerce {field_name}={value!r} to {expected_type.__name__}: {e}")
                result.is_valid = False

    # Pass through extra fields
    for k, v in raw.items():
        if k not in coerced:
            coerced[k] = v

    result.coerced_data = coerced
    return result

FIELD_TYPES = {
    "get_order": {"order_id": str, "total": float, "item_count": int, "status": str},
    "get_inventory": {"item_id": str, "quantity": int, "price": float},
}

def mock_order_tool(order_id: str) -> dict:
    # total returned as int instead of float, item_count as string
    return {"order_id": order_id, "total": 99, "item_count": "3", "status": "shipped"}

tools = [
    {
        "name": "get_order",
        "description": "Retrieve order details",
        "input_schema": {
            "type": "object",
            "properties": {"order_id": {"type": "string"}},
            "required": ["order_id"]
        }
    }
]

def run_with_coercion_validation(query: str) -> str:
    messages = [{"role": "user", "content": query}]

    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=tools,
            messages=messages
        )

        if response.stop_reason == "end_turn":
            return next(b.text for b in response.content if hasattr(b, "text"))

        if response.stop_reason != "tool_use":
            break

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue

            raw = mock_order_tool(block.input.get("order_id", ""))
            expected = FIELD_TYPES.get(block.name, {})
            validated = coerce_and_validate(block.name, raw, expected)

            if validated.is_valid:
                content = json.dumps({
                    "status": "ok",
                    "data": validated.coerced_data
                })
            else:
                content = json.dumps({
                    "status": "partial",
                    "data": validated.coerced_data,
                    "warnings": validated.errors
                })

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": content
            })

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

    return "Done"

print(run_with_coercion_validation("Get details for order ORD-12345"))

# Expected Token Savings: ~10% (coercion prevents re-asking the user for clarification)
# Environment: E-commerce agents, order management systems, inventory tools
```

---

## Option 4: Schema Registry with Versioning

Support multiple API versions with a versioned schema registry.

```python
import anthropic
import json
from typing import Callable

client = anthropic.Anthropic()

# Versioned schema registry
SchemaValidator = Callable[[dict], tuple[bool, dict, str]]

def make_v1_weather_validator() -> SchemaValidator:
    def validate(data: dict) -> tuple[bool, dict, str]:
        required = {"temperature", "conditions", "location"}
        missing = required - set(data.keys())
        if missing:
            return False, {}, f"Missing fields: {missing}"
        if not isinstance(data["temperature"], (int, float)):
            return False, {}, "temperature must be numeric"
        return True, data, ""
    return validate

def make_v2_weather_validator() -> SchemaValidator:
    """V2 API renamed 'conditions' to 'condition' and added 'feels_like'."""
    def validate(data: dict) -> tuple[bool, dict, str]:
        # Handle both v1 and v2 field names
        normalized = dict(data)
        if "conditions" in normalized and "condition" not in normalized:
            normalized["condition"] = normalized.pop("conditions")

        required = {"temperature", "condition", "location"}
        missing = required - set(normalized.keys())
        if missing:
            return False, {}, f"Missing fields in v2 schema: {missing}"
        return True, normalized, ""
    return validate

class VersionedSchemaRegistry:
    def __init__(self):
        self.registry: dict[str, dict[str, SchemaValidator]] = {}
        self.current_versions: dict[str, str] = {}

    def register(self, tool_name: str, version: str, validator: SchemaValidator, is_current: bool = False):
        self.registry.setdefault(tool_name, {})[version] = validator
        if is_current:
            self.current_versions[tool_name] = version

    def validate(self, tool_name: str, data: dict, preferred_version: str | None = None) -> tuple[bool, dict, str]:
        versions = self.registry.get(tool_name, {})
        if not versions:
            return True, data, ""  # No schema registered

        # Try preferred version, then current, then all in reverse order
        try_order = []
        if preferred_version and preferred_version in versions:
            try_order.append(preferred_version)
        current = self.current_versions.get(tool_name)
        if current and current not in try_order:
            try_order.append(current)
        for v in sorted(versions.keys(), reverse=True):
            if v not in try_order:
                try_order.append(v)

        last_error = ""
        for version in try_order:
            validator = versions[version]
            is_valid, cleaned, error = validator(data)
            if is_valid:
                return True, {**cleaned, "_schema_version": version}, ""
            last_error = f"v{version}: {error}"

        return False, {}, f"Failed all schema versions. Last: {last_error}"

registry = VersionedSchemaRegistry()
registry.register("get_weather", "v1", make_v1_weather_validator())
registry.register("get_weather", "v2", make_v2_weather_validator(), is_current=True)

def mock_weather_v1_response(city: str) -> dict:
    # Simulate old API still running v1 schema
    return {"temperature": 20.5, "conditions": "cloudy", "location": city}

tools = [
    {
        "name": "get_weather",
        "description": "Get weather data",
        "input_schema": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"]
        }
    }
]

def run_with_versioned_schema(query: str) -> str:
    messages = [{"role": "user", "content": query}]

    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=tools,
            messages=messages
        )

        if response.stop_reason == "end_turn":
            return next(b.text for b in response.content if hasattr(b, "text"))

        if response.stop_reason != "tool_use":
            break

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue

            raw = mock_weather_v1_response(block.input.get("city", ""))
            is_valid, clean, error = registry.validate(block.name, raw)

            content = json.dumps(clean if is_valid else {"error": error, "raw": raw})
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": content
            })

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

    return "Done"

print(run_with_versioned_schema("What's the weather in Tokyo?"))

# Expected Token Savings: ~0% (prevents crashes; net cost savings from avoided error recovery loops)
# Environment: Long-lived agents operating across API migration periods
```

---

## Option 5: Streaming Validation with Early Abort

Validate tool results in streaming fashion; abort and report the moment an invalid field is detected.

```python
import anthropic
import json
from collections.abc import Iterator

client = anthropic.Anthropic()

class StreamingValidator:
    """Validate fields as they are iterated — abort early on first violation."""

    def __init__(self, rules: dict[str, Callable]):
        self.rules = rules  # field_name -> validator function

    def validate_stream(self, data: dict) -> Iterator[tuple[str, Any, bool, str]]:
        """Yields (field, value, is_valid, error) for each field."""
        for field_name, validator_fn in self.rules.items():
            value = data.get(field_name)
            try:
                is_valid = validator_fn(value)
                yield field_name, value, is_valid, "" if is_valid else f"Validation failed for {field_name}={value!r}"
            except Exception as e:
                yield field_name, value, False, str(e)

from typing import Any, Callable

def is_positive_float(v: Any) -> bool:
    return isinstance(v, (int, float)) and v > 0

def is_nonempty_string(v: Any) -> bool:
    return isinstance(v, str) and len(v.strip()) > 0

def is_valid_status(v: Any) -> bool:
    return v in {"active", "inactive", "pending", "closed"}

product_validator = StreamingValidator({
    "product_id": is_nonempty_string,
    "price": is_positive_float,
    "name": is_nonempty_string,
    "status": is_valid_status,
})

def mock_product_tool(product_id: str) -> dict:
    # status has a typo: "Activ" instead of "active"
    return {"product_id": product_id, "price": 29.99, "name": "Widget Pro", "status": "Activ"}

tools = [
    {
        "name": "get_product",
        "description": "Fetch product details",
        "input_schema": {
            "type": "object",
            "properties": {"product_id": {"type": "string"}},
            "required": ["product_id"]
        }
    }
]

def run_with_streaming_validation(query: str) -> str:
    messages = [{"role": "user", "content": query}]

    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=tools,
            messages=messages
        )

        if response.stop_reason == "end_turn":
            return next(b.text for b in response.content if hasattr(b, "text"))

        if response.stop_reason != "tool_use":
            break

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue

            raw = mock_product_tool(block.input.get("product_id", ""))

            violations = []
            for field_name, value, is_valid, error in product_validator.validate_stream(raw):
                if not is_valid:
                    violations.append({"field": field_name, "value": value, "error": error})

            if not violations:
                content = json.dumps({"status": "valid", "data": raw})
            else:
                # Return both the data and the violations so agent can make informed decisions
                content = json.dumps({
                    "status": "invalid",
                    "data": raw,
                    "violations": violations,
                    "note": "Data may be partially usable; check violations before trusting flagged fields"
                })

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": content
            })

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

    return "Done"

print(run_with_streaming_validation("Get details for product P-999"))

# Expected Token Savings: ~5% (field-by-field reporting allows agent to use valid fields without re-fetching)
# Environment: Agents processing product catalogs, CRM data, or any high-field-count API
```

---

## Option 6: LLM-Based Schema Inference and Validation

When no schema exists, use a cheap model to infer the expected structure and validate future calls against it.

```python
import anthropic
import json
import hashlib
import sqlite3
from pathlib import Path

client = anthropic.Anthropic()

DB_PATH = Path("/tmp/schema_inference_cache.db")

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS inferred_schemas (
            tool_name TEXT PRIMARY KEY,
            schema_json TEXT NOT NULL,
            sample_count INTEGER DEFAULT 1,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit()
    return conn

def infer_schema_from_sample(tool_name: str, sample: dict) -> dict:
    """Use LLM to infer expected schema from a sample result."""
    prompt = f"""Given this tool result sample for tool '{tool_name}':
{json.dumps(sample, indent=2)}

Generate a concise JSON schema describing what fields are required, their types, and any obvious constraints (e.g., enums, ranges).
Return ONLY a valid JSON object with this structure:
{{"required": ["field1", "field2"], "field_types": {{"field1": "string", "field2": "number"}}, "constraints": {{"field2": "positive"}}}}"""

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}]
    )

    try:
        return json.loads(response.content[0].text)
    except (json.JSONDecodeError, IndexError, AttributeError):
        return {"required": list(sample.keys()), "field_types": {k: type(v).__name__ for k, v in sample.items()}}

def validate_against_inferred_schema(data: dict, schema: dict) -> tuple[bool, list[str]]:
    errors = []
    for field in schema.get("required", []):
        if field not in data:
            errors.append(f"Missing required field: {field}")

    for field, expected_type in schema.get("field_types", {}).items():
        if field not in data:
            continue
        value = data[field]
        type_map = {"string": str, "number": (int, float), "integer": int, "boolean": bool, "array": list, "object": dict}
        expected = type_map.get(expected_type)
        if expected and not isinstance(value, expected):
            errors.append(f"Field {field}: expected {expected_type}, got {type(value).__name__}")

    return len(errors) == 0, errors

class LLMSchemaInferenceValidator:
    def __init__(self, db_path: Path):
        self.conn = init_db()

    def validate(self, tool_name: str, data: dict) -> tuple[bool, dict, str]:
        # Check if we have an inferred schema
        row = self.conn.execute(
            "SELECT schema_json FROM inferred_schemas WHERE tool_name = ?", (tool_name,)
        ).fetchone()

        if row is None:
            # First call: infer schema from this sample
            schema = infer_schema_from_sample(tool_name, data)
            self.conn.execute(
                "INSERT INTO inferred_schemas (tool_name, schema_json) VALUES (?, ?)",
                (tool_name, json.dumps(schema))
            )
            self.conn.commit()
            # First call always passes (it's the baseline)
            return True, data, ""

        schema = json.loads(row[0])
        is_valid, errors = validate_against_inferred_schema(data, schema)

        if not is_valid:
            return False, {}, "; ".join(errors)

        # Update sample count
        self.conn.execute(
            "UPDATE inferred_schemas SET sample_count = sample_count + 1 WHERE tool_name = ?",
            (tool_name,)
        )
        self.conn.commit()
        return True, data, ""

llm_validator = LLMSchemaInferenceValidator(DB_PATH)

def mock_analytics_tool_v1(event: str) -> dict:
    return {"event": event, "count": 150, "date": "2026-04-16", "conversion_rate": 0.12}

def mock_analytics_tool_v2(event: str) -> dict:
    # API changed: count is now None and conversion_rate is missing
    return {"event": event, "count": None, "date": "2026-04-16"}

tools = [
    {
        "name": "get_analytics",
        "description": "Fetch analytics data for an event",
        "input_schema": {
            "type": "object",
            "properties": {"event": {"type": "string"}},
            "required": ["event"]
        }
    }
]

def run_with_llm_schema_inference(query: str, use_v2: bool = False) -> str:
    messages = [{"role": "user", "content": query}]

    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=tools,
            messages=messages
        )

        if response.stop_reason == "end_turn":
            return next(b.text for b in response.content if hasattr(b, "text"))

        if response.stop_reason != "tool_use":
            break

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue

            raw = mock_analytics_tool_v2(block.input["event"]) if use_v2 else mock_analytics_tool_v1(block.input["event"])
            is_valid, clean, error = llm_validator.validate(block.name, raw)

            content = json.dumps(clean if is_valid else {"schema_drift_detected": True, "error": error, "raw": raw})
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": content
            })

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

    return "Done"

# First call establishes baseline schema
print("First call:", run_with_llm_schema_inference("Get analytics for 'checkout'"))
# Second call detects drift when API changes
print("After API change:", run_with_llm_schema_inference("Get analytics for 'checkout'", use_v2=True))

# Expected Token Savings: ~15% (schema inference uses haiku; prevents costly agent confusion from silent API drift)
# Environment: Agents using third-party APIs without published schemas; long-running agents spanning API versions
```

---

## Comparison

| Option | Approach | Coercion | Versioning | Best For |
|--------|----------|----------|------------|----------|
| 1. Pydantic | Strict typed models | No | No | Teams already using Pydantic |
| 2. JSON Schema | Standard draft-7 | Yes (manual) | No | Language-agnostic validation |
| 3. Dataclass + Coercion | Field-by-field with auto-cast | Yes (automatic) | No | Forgiving APIs with type drift |
| 4. Versioned Registry | Per-version validators | Yes | Yes | APIs undergoing migration |
| 5. Streaming Validation | Early-exit field iteration | No | No | High-field-count APIs |
| 6. LLM Schema Inference | Auto-infer schema from samples | No | No | Unknown/undocumented APIs |
