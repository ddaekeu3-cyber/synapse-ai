---
layout: solution
title: "Tool Schema Validation Error — Malformed JSON Schema Rejects Tool Definition"
category: tool-failure
description: "Tool definition fails validation with 'invalid schema' or 'tool_use_param_invalid'. The input_schema is not valid JSON Schema. Tool is unavailable to the agent."
tags: [tool-use, json-schema, validation, anthropic, mcp]
---

# Tool Schema Validation Error — Malformed JSON Schema Rejects Tool Definition

## Symptom
- Tool is defined but agent cannot call it
- Error: `invalid_request_error: tools[N].input_schema is not valid JSON Schema`
- Error: `tool_use_param_invalid: input_schema must be an object`
- Tool appears in the list but calls fail with schema errors
- Works in one SDK version but breaks after upgrade

## Root Cause
Anthropic's tool use requires `input_schema` to be a valid JSON Schema object. Common validation failures:

- Missing required `"type": "object"` at root level
- Using `"anyOf"`, `"oneOf"` without correct structure
- Optional parameters listed in `required` array
- `default` values specified (Anthropic doesn't support them in tool schemas)
- Python `None` values serialized as JSON `null` in required fields

## Fix

### Correct minimal tool schema

```python
tools = [
    {
        "name": "search",
        "description": "Search the error database for solutions",
        "input_schema": {
            "type": "object",       # REQUIRED — must be "object" at root
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query"
                },
                "category": {
                    "type": "string",
                    "description": "Optional category filter",
                    "enum": ["auth", "rate-limit", "docker"]  # Optional enum
                }
            },
            "required": ["query"]   # Only list truly required params here
        }
    }
]
```

### Common schema errors and fixes

```python
# BAD — missing type at root
"input_schema": {
    "properties": {"query": {"type": "string"}}
}
# FIX — add type: object
"input_schema": {
    "type": "object",
    "properties": {"query": {"type": "string"}}
}

# BAD — optional param in required list
"properties": {
    "query": {"type": "string"},
    "limit": {"type": "integer", "default": 10}
},
"required": ["query", "limit"]  # limit has a default, shouldn't be required
# FIX
"required": ["query"]  # Remove limit from required

# BAD — default values (not supported by Anthropic)
"limit": {"type": "integer", "default": 10}
# FIX — remove default, put in description instead
"limit": {"type": "integer", "description": "Number of results (default: 10)"}

# BAD — null type for optional
"category": {"type": ["string", "null"]}
# FIX — just omit from required instead
"category": {"type": "string"}
# and don't include it in the required array
```

### Validate schema before using

```python
import jsonschema, json

def validate_tool_schema(tool):
    schema = tool["input_schema"]

    # Must be an object type at root
    assert schema.get("type") == "object", "Root must have type: object"

    # required fields must exist in properties
    required = schema.get("required", [])
    properties = schema.get("properties", {})
    for field in required:
        assert field in properties, f"Required field '{field}' not in properties"

    # Validate it's valid JSON Schema
    jsonschema.Draft7Validator.check_schema(schema)
    print(f"Tool '{tool['name']}' schema valid ✓")

for tool in tools:
    validate_tool_schema(tool)
```

### Minimal valid schema templates

```python
# Minimal (no parameters)
ZERO_PARAM_SCHEMA = {
    "type": "object",
    "properties": {}
}

# One required string param
ONE_STRING_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "..."}
    },
    "required": ["query"]
}

# Mixed required + optional
MIXED_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string"},         # required
        "limit": {"type": "integer"},        # optional
        "format": {
            "type": "string",
            "enum": ["json", "text"]         # optional with enum
        }
    },
    "required": ["query"]
}
```

## Expected Token Savings
Debugging tool unavailability due to schema errors: ~4,000 tokens
Schema validation at definition time: catches error before runtime

## Environment
- Anthropic API tool_use feature
- MCP server tool definitions
- Source: Anthropic tool use documentation, direct experience
