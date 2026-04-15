---
layout: solution
title: "Agent Invents API Parameters That Don't Exist — API Call Fails with Unknown Field"
category: hallucination
description: "Agent calls an API with parameters like 'temperature=0.7' or 'max_length=100' that don't exist in that API. API returns 400 or silently ignores the parameter. Agent is hallucinating the API schema."
tags: [hallucination, api, parameters, schema, 400-error, documentation]
---

# Agent Invents API Parameters That Don't Exist — API Call Fails with Unknown Field

## Symptom
- API returns `400 Bad Request: Unknown field 'max_length'`
- Agent uses `model.generate(temperature=0.9)` but the method doesn't have that parameter
- Agent calls Anthropic API with `top_k=40` on a model that doesn't support it
- API silently ignores unknown parameters — call succeeds but behavior is unexpected
- Agent mixes up parameter names from different SDKs (e.g., OpenAI vs Anthropic)

## Root Cause
The model's training data contains API documentation from many versions and providers. Parameters from one API leak into calls for another (e.g., OpenAI's `max_tokens` vs Anthropic's `max_tokens`, or confusing `temperature` ranges). When the agent doesn't have the current API schema in context, it confabulates plausible-sounding parameters.

## Fix

### Option 1: Include the exact API signature in the system prompt

```
System prompt:
"You are calling the Anthropic Messages API. The EXACT valid parameters are:

client.messages.create(
    model: str,          # Required: e.g. 'claude-sonnet-4-6'
    max_tokens: int,     # Required: maximum output tokens
    messages: list,      # Required: list of {role, content} dicts
    system: str,         # Optional: system prompt
    temperature: float,  # Optional: 0.0-1.0
    top_p: float,        # Optional: 0.0-1.0
    top_k: int,          # Optional: integer
    stream: bool,        # Optional: enable streaming
    stop_sequences: list, # Optional: list of stop strings
    tools: list,         # Optional: tool definitions
    tool_choice: dict,   # Optional: tool selection mode
)

DO NOT use any parameter not listed here. Parameters not listed do not exist."
```

### Option 2: Validate parameters against schema before calling

```python
from typing import Any

ANTHROPIC_MESSAGES_SCHEMA = {
    "required": {"model", "max_tokens", "messages"},
    "optional": {
        "system", "temperature", "top_p", "top_k",
        "stream", "stop_sequences", "tools", "tool_choice",
        "metadata"
    }
}

def validate_api_call(params: dict, schema: dict) -> list[str]:
    """Check for unknown parameters before making API call"""
    allowed = schema["required"] | schema["optional"]
    unknown = set(params.keys()) - allowed
    missing = schema["required"] - set(params.keys())

    errors = []
    if unknown:
        errors.append(f"Unknown parameters: {unknown}. Allowed: {allowed}")
    if missing:
        errors.append(f"Missing required parameters: {missing}")
    return errors

# Before calling API
params = {
    "model": "claude-sonnet-4-6",
    "max_tokens": 1024,
    "messages": [...],
    "max_length": 100,  # hallucinated parameter!
}

errors = validate_api_call(params, ANTHROPIC_MESSAGES_SCHEMA)
if errors:
    raise ValueError(f"Invalid API parameters: {errors}")
```

### Option 3: Use typed dataclasses / pydantic models for API calls

```python
from pydantic import BaseModel, Field, model_validator
from typing import Optional

class AnthropicMessageRequest(BaseModel):
    model: str
    max_tokens: int
    messages: list[dict]
    system: Optional[str] = None
    temperature: Optional[float] = Field(None, ge=0.0, le=1.0)
    top_p: Optional[float] = Field(None, ge=0.0, le=1.0)
    top_k: Optional[int] = Field(None, ge=1)
    stream: Optional[bool] = None
    stop_sequences: Optional[list[str]] = None

    class Config:
        extra = "forbid"  # Raise error on unknown fields

# Agent generates params as dict — validate before calling
try:
    request = AnthropicMessageRequest(**agent_generated_params)
    response = client.messages.create(**request.model_dump(exclude_none=True))
except ValidationError as e:
    print(f"Agent hallucinated invalid params: {e}")
    # Re-prompt agent with correct schema
```

### Option 4: Fetch live API schema from documentation

```python
# For OpenAPI-compatible APIs, fetch the actual schema
import httpx, yaml

def get_api_schema(openapi_url: str) -> dict:
    """Fetch live API schema to ground agent in current spec"""
    response = httpx.get(openapi_url)
    return yaml.safe_load(response.text)

def extract_endpoint_params(schema: dict, endpoint: str, method: str = "post") -> list[str]:
    """Extract valid parameter names for a specific endpoint"""
    path = schema.get("paths", {}).get(endpoint, {})
    operation = path.get(method, {})
    params = []
    for param in operation.get("parameters", []):
        params.append(param["name"])
    body_schema = operation.get("requestBody", {}).get("content", {}).get("application/json", {}).get("schema", {})
    params.extend(body_schema.get("properties", {}).keys())
    return params
```

### Option 5: Parameter cross-reference table in prompt

```
System prompt:
"IMPORTANT — API parameter name differences:

| Concept | OpenAI | Anthropic |
|---------|--------|-----------|
| Max output tokens | max_tokens | max_tokens |
| Sampling temperature | temperature | temperature |
| Nucleus sampling | top_p | top_p |
| Model name | model | model |
| System prompt | messages[system] | system (top-level) |
| Stop words | stop | stop_sequences |
| Streaming | stream | stream |
| N completions | n | NOT SUPPORTED |
| Presence penalty | presence_penalty | NOT SUPPORTED |
| Frequency penalty | frequency_penalty | NOT SUPPORTED |
| Logprobs | logprobs | NOT SUPPORTED |

Never use OpenAI-only parameters with the Anthropic API."
```

## Common Hallucinated Parameters for Anthropic API

| Hallucinated param | Actually exists? | What to use instead |
|---|---|---|
| `max_length` | No | `max_tokens` |
| `presence_penalty` | No | Not available |
| `frequency_penalty` | No | Not available |
| `n` | No | Make N separate calls |
| `logprobs` | No | Not available |
| `best_of` | No | Not available |
| `response_format` | No (as of 2025) | Prompt for JSON |
| `functions` | No | `tools` |

## Expected Token Savings
Debugging hallucinated parameter errors: ~3,000 tokens
Schema in system prompt prevents all: 0 wasted

## Environment
- Any agent making API calls, especially when switching between providers
- Source: direct experience; extremely common when agents use multiple API providers
