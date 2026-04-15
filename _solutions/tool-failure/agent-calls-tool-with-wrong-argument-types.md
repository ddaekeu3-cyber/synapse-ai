---
layout: solution
title: "Agent Calls Tool with Wrong Argument Types — Silent Failure or Type Error"
category: tool-failure
description: "Agent passes a string where the tool expects an integer, or a list where a string is expected. Tool raises TypeError or silently coerces the value incorrectly. Agent retries with the same wrong types."
tags: [tool-call, type-error, argument, validation, schema, pydantic, coercion]
---

# Agent Calls Tool with Wrong Argument Types — Silent Failure or Type Error

## Symptom
- `TypeError: expected int, got str` when agent calls a tool
- Tool receives `"10"` (string) instead of `10` (integer) — coercion silently changes behavior
- Agent passes `None` for a required parameter — tool raises `AttributeError`
- Agent serializes a list as a JSON string: `"[1, 2, 3]"` instead of `[1, 2, 3]`
- Tool schema says `limit: integer` but agent sends `limit: "20"` — query returns wrong results

## Root Cause
LLMs generate tool call arguments as JSON strings. Without strict validation, type mismatches slip through — especially for numeric strings, nested objects serialized as strings, and `None` vs. missing keys. Many tools accept the wrong type silently (Python's implicit coercion) and produce subtly wrong results instead of raising immediately.

## Fix

### Option 1: Pydantic validation on every tool call

```python
from pydantic import BaseModel, Field, validator
from typing import Optional

class SearchArgs(BaseModel):
    query: str = Field(..., min_length=1, max_length=500)
    limit: int = Field(default=10, ge=1, le=100)
    offset: int = Field(default=0, ge=0)
    include_metadata: bool = False
    tags: list[str] = Field(default_factory=list)

    @validator("limit", "offset", pre=True)
    def coerce_int(cls, v):
        """Allow '10' → 10 but reject 'abc'"""
        if isinstance(v, str):
            try:
                return int(v)
            except ValueError:
                raise ValueError(f"Expected integer, got '{v}'")
        return v

def search_tool(raw_args: dict) -> dict:
    """Tool with validated arguments"""
    try:
        args = SearchArgs(**raw_args)
    except Exception as e:
        # Return structured error the agent can understand and fix
        return {
            "error": "invalid_arguments",
            "message": str(e),
            "received": raw_args,
            "schema": SearchArgs.schema()
        }

    return execute_search(args.query, args.limit, args.offset)
```

### Option 2: Type coercion layer between agent and tool

```python
from typing import get_type_hints, get_origin, get_args
import inspect

def coerce_tool_args(fn, raw_args: dict) -> dict:
    """
    Coerce agent-provided arguments to the types expected by the tool function.
    Handles common conversions: str→int, str→float, str→bool, str→list.
    """
    hints = get_type_hints(fn)
    coerced = {}

    for param_name, expected_type in hints.items():
        if param_name == "return":
            continue
        if param_name not in raw_args:
            continue

        raw_value = raw_args[param_name]
        origin = get_origin(expected_type)

        try:
            if expected_type == int or expected_type == Optional[int]:
                coerced[param_name] = int(raw_value) if raw_value is not None else None
            elif expected_type == float:
                coerced[param_name] = float(raw_value)
            elif expected_type == bool:
                if isinstance(raw_value, str):
                    coerced[param_name] = raw_value.lower() in ("true", "1", "yes")
                else:
                    coerced[param_name] = bool(raw_value)
            elif expected_type == list or origin == list:
                if isinstance(raw_value, str):
                    import json
                    coerced[param_name] = json.loads(raw_value)
                else:
                    coerced[param_name] = list(raw_value)
            else:
                coerced[param_name] = raw_value
        except (ValueError, TypeError) as e:
            raise TypeError(
                f"Cannot coerce argument '{param_name}': "
                f"expected {expected_type.__name__}, got {type(raw_value).__name__} '{raw_value}': {e}"
            )

    return coerced

# Usage:
def my_tool(query: str, limit: int, tags: list) -> dict:
    return search(query, limit, tags)

safe_args = coerce_tool_args(my_tool, {"query": "hello", "limit": "20", "tags": "[\"a\", \"b\"]"})
result = my_tool(**safe_args)
```

### Option 3: Strict JSON schema for tool definitions

```python
# Provide strict schemas to the model so it generates correct types
TOOL_DEFINITIONS = [
    {
        "name": "search_database",
        "description": "Search the database for records matching the query",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query text",
                    "minLength": 1
                },
                "limit": {
                    "type": "integer",      # NOT "number" — forces integer
                    "description": "Maximum results to return",
                    "minimum": 1,
                    "maximum": 100,
                    "default": 10
                },
                "active_only": {
                    "type": "boolean",      # NOT string "true"/"false"
                    "description": "If true, only return active records",
                    "default": True
                },
                "tags": {
                    "type": "array",        # NOT "string" — forces actual array
                    "items": {"type": "string"},
                    "description": "Filter by these tags"
                }
            },
            "required": ["query"],
            "additionalProperties": False   # Reject unknown args
        }
    }
]

# Validate agent output against schema before dispatching
import jsonschema

def validate_tool_call(tool_name: str, args: dict) -> dict:
    schema = next(
        (t["input_schema"] for t in TOOL_DEFINITIONS if t["name"] == tool_name),
        None
    )
    if not schema:
        raise ValueError(f"Unknown tool: {tool_name}")

    try:
        jsonschema.validate(args, schema)
        return args
    except jsonschema.ValidationError as e:
        raise ValueError(f"Tool '{tool_name}' argument error: {e.message} (path: {list(e.absolute_path)})")
```

### Option 4: Log and catch type errors with agent-readable feedback

```python
import functools
import traceback

def typed_tool(fn):
    """
    Decorator that catches TypeErrors and returns structured error
    that the agent can read and correct.
    """
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except TypeError as e:
            import inspect
            sig = inspect.signature(fn)
            hints = {k: str(v) for k, v in get_type_hints(fn).items() if k != "return"}
            return {
                "error": "type_error",
                "message": str(e),
                "expected_signature": str(sig),
                "expected_types": hints,
                "received_args": {k: f"{type(v).__name__}({v!r})" for k, v in kwargs.items()},
                "fix": "Check argument types match the expected signature above"
            }
        except ValueError as e:
            return {
                "error": "value_error",
                "message": str(e),
                "fix": "Check argument values are within acceptable ranges"
            }

    return wrapper

@typed_tool
def calculate_discount(price: float, percent: int, coupon_code: str) -> float:
    return price * (1 - percent / 100)

# Agent passes {"price": "19.99", "percent": "10", "coupon_code": "SAVE10"}
result = calculate_discount(price="19.99", percent="10", coupon_code="SAVE10")
# → {"error": "type_error", "message": "...", "expected_types": {...}}
# Agent reads error and corrects the types
```

### Option 5: System prompt with type examples

```
System prompt:
"Tool call argument rules:

1. Integers: pass as JSON numbers, NOT strings
   ✓ correct: {"limit": 10}
   ✗ wrong:   {"limit": "10"}

2. Booleans: pass as JSON true/false, NOT strings
   ✓ correct: {"active": true}
   ✗ wrong:   {"active": "true"}

3. Arrays: pass as JSON arrays, NOT serialized strings
   ✓ correct: {"tags": ["a", "b"]}
   ✗ wrong:   {"tags": "[\"a\", \"b\"]"}

4. Optional parameters: omit entirely if not needed — do NOT pass null
   ✓ correct: {"query": "hello"}
   ✗ wrong:   {"query": "hello", "filter": null}

5. If a tool returns {\"error\": \"type_error\"}, read the \"expected_types\"
   field and correct your argument types before retrying."
```

### Option 6: Pre-dispatch argument inspector

```python
def inspect_tool_args(tool_name: str, args: dict, schema: dict) -> list[str]:
    """
    Check for common type mistakes before dispatching.
    Returns list of warnings/errors.
    """
    issues = []
    props = schema.get("properties", {})

    for key, value in args.items():
        if key not in props:
            issues.append(f"Unknown argument '{key}' — not in schema")
            continue

        expected_type = props[key].get("type")
        actual_type = type(value).__name__

        # String where number expected
        if expected_type == "integer" and isinstance(value, str):
            issues.append(f"'{key}': expected integer, got string '{value}' — pass {int(value) if value.isdigit() else '?'}")

        # String "true"/"false" where boolean expected
        if expected_type == "boolean" and isinstance(value, str):
            issues.append(f"'{key}': expected boolean, got string '{value}' — pass true or false (no quotes)")

        # String where array expected
        if expected_type == "array" and isinstance(value, str):
            issues.append(f"'{key}': expected array, got string — pass a JSON array [...] not a string")

        # None for required field
        if key in schema.get("required", []) and value is None:
            issues.append(f"'{key}': required but received null")

    return issues
```

## Common Type Mistakes by Argument Type

| Expected | Wrong (agent sends) | Correct |
|---|---|---|
| `integer` | `"10"` (string) | `10` |
| `boolean` | `"true"` (string) | `true` |
| `array` | `"[1,2,3]"` (string) | `[1, 2, 3]` |
| `object` | `"{\"key\": 1}"` (string) | `{"key": 1}` |
| `float` | `"3.14"` (string) | `3.14` |
| `null` (optional) | `"null"` (string) | omit the key entirely |

## Expected Token Savings
Type error + 3 retry attempts with same wrong types: ~6,000 tokens
Pydantic validation + structured error on first call: ~500 tokens

## Environment
- Any agent using tool calling; most common with numeric and boolean parameters
- Source: direct experience; string-vs-integer is the most frequent tool argument mistake
