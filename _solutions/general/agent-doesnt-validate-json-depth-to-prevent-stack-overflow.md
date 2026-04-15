---
layout: solution
title: "Agent Doesn't Validate JSON Depth to Prevent Stack Overflow"
category: general
description: "Agent parses user-supplied or tool-returned JSON without checking nesting depth, allowing deeply nested payloads to exhaust the call stack, crash the process, or trigger ReDoS in regex-based parsers."
tags: [security, reliability, json, validation, stack-overflow]
---

## Symptom

The agent crashes with a recursion error when processing a crafted or unexpectedly deep JSON payload:

```python
RecursionError: maximum recursion depth exceeded in comparison
```

Or a more subtle failure: the Python `json` module hangs for seconds on a deeply nested array before Python's recursion limit terminates it, causing the agent process to crash mid-request.

## Root Cause

The agent passes JSON directly to `json.loads()` without any depth pre-check:

```python
import json
import anthropic

client = anthropic.Anthropic(api_key="sk-live-...")

def process_tool_result(raw_json: str) -> dict:
    # No depth check — crashes on [[[[...10,000 levels deep...]]]]
    data = json.loads(raw_json)
    return data
```

Python's `json` module itself is implemented in C and handles most depth cases without hitting the Python recursion limit, but recursive processors built on top of parsed structures (schema validators, pretty-printers, diff functions) will crash.

More critically: **user-supplied JSON injected into tool arguments** can be crafted to exploit this.

---

## Fix

### Option 1 — Pre-check nesting depth before parsing

Scan the raw string for bracket depth before calling `json.loads()`. Reject payloads that exceed the limit.

```python
import json
import anthropic

client = anthropic.Anthropic(api_key="sk-live-...")

MAX_JSON_DEPTH = 20


def check_json_depth(raw: str, max_depth: int = MAX_JSON_DEPTH) -> int:
    """Count max nesting depth in JSON string without fully parsing it."""
    depth = 0
    max_seen = 0
    in_string = False
    escape_next = False

    for char in raw:
        if escape_next:
            escape_next = False
            continue
        if char == '\\' and in_string:
            escape_next = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if char in ('{', '['):
            depth += 1
            max_seen = max(max_seen, depth)
        elif char in ('}', ']'):
            depth -= 1

    return max_seen


def safe_parse(raw_json: str, max_depth: int = MAX_JSON_DEPTH) -> dict:
    """Parse JSON with depth guard."""
    depth = check_json_depth(raw_json, max_depth)
    if depth > max_depth:
        raise ValueError(
            f"JSON exceeds maximum nesting depth {max_depth} (got {depth}). "
            "Payload rejected for safety."
        )
    return json.loads(raw_json)


# Safe: normal payload
normal = '{"user": {"id": 1, "prefs": {"theme": "dark"}}}'
print(safe_parse(normal))  # {'user': {'id': 1, 'prefs': {'theme': 'dark'}}}

# Rejected: deeply nested attack payload
attack = "[" * 5000 + "1" + "]" * 5000
try:
    safe_parse(attack)
except ValueError as e:
    print(f"Rejected: {e}")

# Expected Token Savings: prevents crash-restart cycles that consume tokens with no output
# Environment: any agent that parses user-supplied or external JSON payloads
```

---

### Option 2 — Pydantic validation with max recursion protection

Use Pydantic to validate the parsed structure. Wrap the model call in a recursion guard so deeply nested inputs raise a `ValidationError` instead of crashing.

```python
import sys
import json
import anthropic
from pydantic import BaseModel, field_validator, model_validator
from typing import Any

client = anthropic.Anthropic(api_key="sk-live-...")

MAX_DEPTH = 15


def measure_depth(obj: Any, current: int = 0) -> int:
    if current > MAX_DEPTH:
        return current  # Early exit — already too deep
    if isinstance(obj, dict):
        if not obj:
            return current
        return max(measure_depth(v, current + 1) for v in obj.values())
    if isinstance(obj, list):
        if not obj:
            return current
        return max(measure_depth(item, current + 1) for item in obj)
    return current


class SafePayload(BaseModel):
    data: Any

    @model_validator(mode="after")
    def check_depth(self) -> "SafePayload":
        depth = measure_depth(self.data)
        if depth > MAX_DEPTH:
            raise ValueError(
                f"Payload nesting depth {depth} exceeds maximum {MAX_DEPTH}"
            )
        return self


def process_payload(raw_json: str) -> Any:
    parsed = json.loads(raw_json)
    # Validate depth via Pydantic
    validated = SafePayload(data=parsed)
    return validated.data


# Normal payload
result = process_payload('{"config": {"timeout": 30, "retries": 3}}')
print("OK:", result)

# Deep payload — rejected cleanly
deep = json.dumps({"a": {"b": {"c": {"d": {"e": {"f": {"g": {"h": {"i": {"j": {"k": {"l": {"m": {"n": {"o": {"p": 1}}}}}}}}}}}}}}}})
try:
    process_payload(deep)
except Exception as e:
    print(f"Rejected: {e}")

# Expected Token Savings: Pydantic error surfaced cleanly vs process crash and restart
# Environment: FastAPI/Pydantic agents where request bodies include arbitrary JSON
```

---

### Option 3 — Iterative JSON depth check (no recursion)

For very large payloads, use an explicit stack instead of recursive Python calls to measure depth — no risk of hitting Python's own recursion limit during the check.

```python
import json
import anthropic
from collections import deque

client = anthropic.Anthropic(api_key="sk-live-...")

MAX_DEPTH = 20


def measure_depth_iterative(obj: object) -> int:
    """Measure JSON depth without recursion using an explicit stack."""
    if not isinstance(obj, (dict, list)):
        return 0

    max_depth = 0
    # Stack stores (node, current_depth)
    stack = deque([(obj, 1)])

    while stack:
        node, depth = stack.pop()
        max_depth = max(max_depth, depth)

        if depth > MAX_DEPTH:
            return depth  # Early exit

        if isinstance(node, dict):
            for v in node.values():
                if isinstance(v, (dict, list)):
                    stack.append((v, depth + 1))
        elif isinstance(node, list):
            for item in node:
                if isinstance(item, (dict, list)):
                    stack.append((item, depth + 1))

    return max_depth


def safe_json_parse(raw: str, max_depth: int = MAX_DEPTH) -> object:
    # Python's json.loads handles malformed JSON and most depth cases safely
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON: {e}")

    depth = measure_depth_iterative(parsed)
    if depth > max_depth:
        raise ValueError(f"JSON depth {depth} exceeds limit {max_depth}")

    return parsed


# Test
simple = json.dumps({"a": [1, 2, {"b": 3}]})
print("Depth:", measure_depth_iterative(json.loads(simple)))  # 3

# Simulate a nested payload
nested = {}
current = nested
for _ in range(25):
    current["child"] = {}
    current = current["child"]

raw = json.dumps(nested)
try:
    safe_json_parse(raw)
except ValueError as e:
    print(f"Rejected: {e}")

# Expected Token Savings: no Python stack exhaustion during depth check
# Environment: agents processing large JSON blobs where recursive checkers would themselves overflow
```

---

### Option 4 — Sanitise tool arguments before injection into prompts

When tool call arguments contain JSON that will be injected into an LLM prompt, cap the depth AND flatten it to prevent both stack overflow and prompt injection.

```python
import json
import anthropic
from typing import Any

client = anthropic.Anthropic(api_key="sk-live-...")

MAX_DEPTH = 10
MAX_STRING_LENGTH = 500


def flatten_deep(obj: Any, depth: int = 0, max_depth: int = MAX_DEPTH) -> Any:
    """Recursively flatten/truncate deep structures."""
    if depth >= max_depth:
        return f"[TRUNCATED depth>{max_depth}]"

    if isinstance(obj, dict):
        return {k: flatten_deep(v, depth + 1, max_depth) for k, v in list(obj.items())[:50]}
    if isinstance(obj, list):
        return [flatten_deep(item, depth + 1, max_depth) for item in obj[:100]]
    if isinstance(obj, str) and len(obj) > MAX_STRING_LENGTH:
        return obj[:MAX_STRING_LENGTH] + "...[TRUNCATED]"
    return obj


def safe_tool_arg(raw_json: str) -> str:
    """Parse, flatten to safe depth, and re-serialise for use in prompts."""
    try:
        parsed = json.loads(raw_json)
    except json.JSONDecodeError:
        return '{"error": "invalid_json"}'

    safe = flatten_deep(parsed)
    return json.dumps(safe, ensure_ascii=False)


# Deeply nested user input that would crash a recursive processor
attack_payload = json.dumps({"user": {"data": [{"nested": {"deeply": {"very": {"extreme": 42}}}}]}})
safe = safe_tool_arg(attack_payload)
print("Safe payload:", safe[:200])

# Use in agent prompt safely
response = client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=100,
    messages=[{"role": "user", "content": f"Summarise this data: {safe}"}]
)
print(response.content[0].text)

# Expected Token Savings: truncated payloads use fewer prompt tokens; no crash recovery needed
# Environment: agents that inject user-supplied structured data into LLM prompts
```

---

### Option 5 — Depth limit enforced at API boundary (FastAPI middleware)

Add middleware to the agent's API that rejects any request body with a JSON depth exceeding the limit before the request reaches the agent logic.

```python
import json
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
import anthropic

app = FastAPI()
client = anthropic.Anthropic(api_key="sk-live-...")

MAX_REQUEST_DEPTH = 10
MAX_BODY_SIZE = 1_000_000  # 1 MB


def json_depth(raw: str) -> int:
    """Fast depth scan without full parse."""
    depth = 0
    max_d = 0
    in_string = False
    prev = ""
    for ch in raw:
        if ch == '"' and prev != '\\':
            in_string = not in_string
        if not in_string:
            if ch in ('{', '['):
                depth += 1
                max_d = max(max_d, depth)
            elif ch in ('}', ']'):
                depth -= 1
        prev = ch
    return max_d


@app.middleware("http")
async def json_depth_guard(request: Request, call_next):
    if request.method in ("POST", "PUT", "PATCH"):
        body = await request.body()

        if len(body) > MAX_BODY_SIZE:
            return JSONResponse(
                {"error": "Request body too large"},
                status_code=413,
            )

        content_type = request.headers.get("content-type", "")
        if "application/json" in content_type:
            depth = json_depth(body.decode("utf-8", errors="replace"))
            if depth > MAX_REQUEST_DEPTH:
                return JSONResponse(
                    {"error": f"JSON nesting depth {depth} exceeds limit {MAX_REQUEST_DEPTH}"},
                    status_code=400,
                )

    return await call_next(request)


@app.post("/agent")
async def agent_endpoint(request: Request):
    body = await request.json()
    prompt = body.get("prompt", "")

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}]
    )
    return {"result": response.content[0].text}

# Expected Token Savings: malicious payloads rejected at gateway before consuming any compute
# Environment: production FastAPI agents exposed to untrusted clients
```

---

### Option 6 — Schema-constrained JSON parsing with depth embedded in schema

Use JSON Schema validation with a `maxProperties` / `maxItems` constraint that indirectly limits depth by limiting the size of each nesting level.

```python
import json
import jsonschema
import anthropic
from typing import Any

client = anthropic.Anthropic(api_key="sk-live-...")

# Schema that limits depth via maxItems and maxProperties at each level
ORDER_SCHEMA = {
    "type": "object",
    "maxProperties": 20,
    "properties": {
        "order_id": {"type": "string", "maxLength": 50},
        "customer": {
            "type": "object",
            "maxProperties": 10,
            "properties": {
                "id": {"type": "string"},
                "name": {"type": "string", "maxLength": 100},
                "address": {
                    "type": "object",
                    "maxProperties": 8,
                    "additionalProperties": {"type": "string", "maxLength": 200}
                }
            },
            "additionalProperties": False
        },
        "items": {
            "type": "array",
            "maxItems": 100,
            "items": {
                "type": "object",
                "maxProperties": 8,
                "properties": {
                    "sku": {"type": "string"},
                    "qty": {"type": "integer", "minimum": 1, "maximum": 9999},
                    "price_cents": {"type": "integer"},
                },
                "additionalProperties": False
            }
        }
    },
    "additionalProperties": False,
    "required": ["order_id", "items"]
}


def parse_order(raw_json: str) -> dict:
    """Parse and validate an order payload against a strict schema."""
    try:
        parsed = json.loads(raw_json)
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON: {e}")

    try:
        jsonschema.validate(parsed, ORDER_SCHEMA)
    except jsonschema.ValidationError as e:
        raise ValueError(f"Schema validation failed: {e.message}")

    return parsed


# Valid order
valid_order = json.dumps({
    "order_id": "ord-123",
    "customer": {"id": "cust-1", "name": "Alice"},
    "items": [{"sku": "SKU-A", "qty": 2, "price_cents": 999}]
})
print("Valid:", parse_order(valid_order))

# Schema prevents arbitrary nesting — additionalProperties: false blocks unknown deep structures
injected = json.dumps({"order_id": "x", "items": [], "extra": {"deeply": {"nested": "data"}}})
try:
    parse_order(injected)
except ValueError as e:
    print(f"Rejected: {e}")

# Expected Token Savings: schema enforcement prevents both depth attacks and malformed data
# Environment: agents with well-defined input schemas; install: pip install jsonschema
```

---

## Comparison

| Option | Depth Check | Recursion-Safe | FastAPI-Ready | Schema-Validated | Complexity |
|--------|-------------|----------------|---------------|------------------|------------|
| 1 | String scan | Yes | No | No | Low |
| 2 | Pydantic | Yes | Yes | Yes | Low |
| 3 | Iterative stack | Yes | No | No | Low |
| 4 | Flatten + truncate | Yes | No | No | Low |
| 5 | Middleware | Yes | Yes | No | Medium |
| 6 | JSON Schema | Yes | No | Yes | Medium |

**Recommended starting point:** Option 1 (string scan) for quick protection on any existing JSON-parsing code — add `check_json_depth()` before `json.loads()` in one line. Option 5 (middleware) for production FastAPI agents where all inputs must be guarded at the API boundary.
