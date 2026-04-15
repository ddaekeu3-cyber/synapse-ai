---
layout: solution
title: "Agent Doesn't Validate Tool Output Schema Before Parsing"
category: general
description: "Agent blindly accesses fields on tool results assuming they match the documented schema — a field rename, API version bump, or error response causes a KeyError or AttributeError that crashes the agent mid-task."
tags: [reliability, schema-validation, tool-use, defensive-coding, api]
---

## Symptom

An API tool was updated: the field `user_name` was renamed to `username`. The agent crashes:

```python
result = json.loads(tool_output)
name = result["user_name"]  # KeyError: 'user_name'
# Agent raises exception, task fails
```

Or the tool returns an error object instead of the expected data shape:

```json
{"error": "rate_limit_exceeded", "retry_after": 30}
```

The agent tries to access `result["data"]["users"]` and crashes instead of handling the error.

## Root Cause

Tool results are treated as trusted, schema-stable data — parsed without any validation:

```python
import anthropic
import json

client = anthropic.Anthropic(api_key="sk-live-...")

# Anti-pattern: blind field access on tool results
def handle_tool_result(raw: str) -> dict:
    data = json.loads(raw)
    return {
        "id": data["user"]["id"],         # KeyError if schema changed
        "name": data["user"]["user_name"], # AttributeError after rename
        "email": data["user"]["email"],
    }
```

---

## Fix

### Option 1 — Safe field access with explicit defaults and error detection

Use `.get()` everywhere and check for error fields before accessing data fields.

```python
import anthropic
import json

client = anthropic.Anthropic(api_key="sk-live-...")


def safe_parse_user(raw: str) -> dict:
    """Parse user tool result defensively — never crashes on unexpected schema."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        return {"error": f"Invalid JSON: {e}", "raw_preview": raw[:100]}

    # Check for error response first
    if "error" in data:
        return {
            "error": data["error"],
            "retry_after": data.get("retry_after"),
            "message": data.get("message", "Unknown error")
        }

    # Extract user — try multiple field name variants (API versioning)
    user = data.get("user") or data.get("data", {})
    if not user:
        return {"error": "Missing user/data field in response", "fields_found": list(data.keys())}

    return {
        # Try both old and new field names
        "id": user.get("id") or user.get("user_id"),
        "name": user.get("username") or user.get("user_name") or user.get("name"),
        "email": user.get("email") or user.get("email_address"),
        "_schema_version": data.get("schema_version", "unknown"),
        "_warnings": [
            f"Field '{f}' missing" for f in ["id", "email"]
            if not user.get(f)
        ]
    }


# Test with various response shapes
test_cases = [
    '{"user": {"id": 1, "username": "alice", "email": "alice@example.com"}}',
    '{"user": {"id": 2, "user_name": "bob", "email": "bob@example.com"}}',  # Old API
    '{"error": "rate_limit_exceeded", "retry_after": 30}',
    '{"data": {"id": 3, "username": "carol"}}',  # Different wrapper
    'not json at all',
]

for raw in test_cases:
    result = safe_parse_user(raw)
    print(f"Input: {raw[:50]!r}")
    print(f"Output: {result}\n")

# Expected Token Savings: graceful parse prevents crash → no error recovery turn needed
# Environment: any agent consuming external API tool results that may change schema between versions
```

---

### Option 2 — Pydantic schema validation with version negotiation

Define the expected schema as a Pydantic model. Parse with `model_validate()` and catch `ValidationError` to handle schema mismatches gracefully.

```python
import anthropic
import json
from typing import Any

client = anthropic.Anthropic(api_key="sk-live-...")

try:
    from pydantic import BaseModel, ValidationError, field_validator
    HAS_PYDANTIC = True
except ImportError:
    HAS_PYDANTIC = False
    print("Install: pip install pydantic")


if HAS_PYDANTIC:
    class UserV2(BaseModel):
        """Current API schema (v2)."""
        id: int
        username: str
        email: str | None = None
        role: str = "user"

        @field_validator("username", mode="before")
        @classmethod
        def coerce_username(cls, v: Any) -> str:
            """Accept both 'username' and legacy 'user_name'."""
            return str(v)

    class UserResponse(BaseModel):
        """Wrapper supporting both v1 and v2 response shapes."""
        user: UserV2 | None = None
        data: UserV2 | None = None
        error: str | None = None
        schema_version: str = "1"

        def get_user(self) -> UserV2 | None:
            return self.user or self.data


def parse_user_response(raw: str) -> dict:
    """Validate tool result against schema; return structured result."""
    if not HAS_PYDANTIC:
        return json.loads(raw)

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        return {"error": f"Invalid JSON: {e}"}

    # Handle renamed fields (v1 compat)
    if "user" in data and "user_name" in data.get("user", {}):
        data["user"]["username"] = data["user"].pop("user_name")

    try:
        response = UserResponse.model_validate(data)
    except ValidationError as e:
        # Schema mismatch — return structured error with what was found
        errors = [{"field": ".".join(str(x) for x in err["loc"]), "msg": err["msg"]} for err in e.errors()]
        return {
            "error": "Schema validation failed",
            "validation_errors": errors,
            "fields_received": list(data.keys())
        }

    if response.error:
        return {"error": response.error}

    user = response.get_user()
    if not user:
        return {"error": "No user data in response"}

    return user.model_dump()


cases = [
    '{"user": {"id": 1, "username": "alice", "email": "alice@example.com"}}',
    '{"user": {"id": 2, "user_name": "bob", "email": null}}',
    '{"error": "not_found"}',
    '{"user": {"id": "not-an-int", "username": "dave"}}',
]

for raw in cases:
    result = parse_user_response(raw)
    print(f"{raw[:55]!r}")
    print(f"→ {result}\n")

# Expected Token Savings: Pydantic catches schema drift before model parses it; prevents mid-task crash
# Environment: production agents consuming versioned external APIs; enterprise tool integrations
```

---

### Option 3 — Runtime schema inference: detect and adapt to actual response shape

Infer the schema from the actual response at runtime and map it to the expected interface using heuristics.

```python
import anthropic
import json
import re

client = anthropic.Anthropic(api_key="sk-live-...")

# Field name aliases — map alternate names to canonical names
FIELD_ALIASES = {
    "id": ["id", "user_id", "userId", "uid", "account_id"],
    "name": ["name", "username", "user_name", "userName", "display_name", "full_name"],
    "email": ["email", "email_address", "emailAddress", "contact_email"],
    "created_at": ["created_at", "createdAt", "created", "timestamp", "date_created"],
}


def find_field(data: dict, canonical: str) -> tuple[str | None, Any]:
    """Find a field value trying all known aliases."""
    for alias in FIELD_ALIASES.get(canonical, [canonical]):
        if alias in data:
            return alias, data[alias]
    return None, None


def infer_and_map(raw: str, required_fields: list[str]) -> dict:
    """
    Parse tool result and map to required fields using alias inference.
    Returns mapped data or structured error.
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        return {"error": f"JSON parse error: {e}"}

    if not isinstance(data, dict):
        return {"error": f"Expected object, got {type(data).__name__}"}

    # Check for error response
    if "error" in data and len(data) <= 3:
        return {"error": data.get("error"), "details": data.get("message") or data.get("detail")}

    # Unwrap common wrapper fields
    unwrapped = data
    for wrapper in ["user", "data", "result", "payload", "response"]:
        if wrapper in data and isinstance(data[wrapper], dict):
            unwrapped = data[wrapper]
            break

    # Map required fields using aliases
    result = {}
    missing = []
    for field in required_fields:
        alias_used, value = find_field(unwrapped, field)
        if alias_used is not None:
            result[field] = value
            if alias_used != field:
                result[f"_alias_{field}"] = alias_used  # Track what was renamed
        else:
            missing.append(field)

    if missing:
        result["_warnings"] = [f"Could not find: {m} (tried: {FIELD_ALIASES.get(m, [m])})" for m in missing]
        result["_available_fields"] = list(unwrapped.keys())

    return result


required = ["id", "name", "email"]

test_inputs = [
    '{"userId": 10, "userName": "alice", "emailAddress": "alice@ex.com"}',
    '{"data": {"id": 11, "full_name": "Bob Smith", "contact_email": "bob@ex.com"}}',
    '{"error": "unauthorized", "message": "Token expired"}',
    '{"id": 12, "display_name": "Carol", "phone": "555-1234"}',  # Missing email
]

for raw in test_inputs:
    result = infer_and_map(raw, required)
    print(f"Input: {raw[:60]!r}")
    print(f"Mapped: {result}\n")

# Expected Token Savings: alias mapping handles schema drift without agent needing extra turns
# Environment: agents integrating with legacy APIs or systems with inconsistent naming conventions
```

---

### Option 4 — Tool result middleware: validate before returning to model

Wrap the tool execution layer so all results are validated before the model sees them. Invalid results get enriched with diagnostic info.

```python
import anthropic
import json
from typing import Callable

client = anthropic.Anthropic(api_key="sk-live-...")


def make_validator(required_fields: list[str], optional_fields: list[str] | None = None):
    """Factory: create a validator for a specific expected schema."""
    optional_fields = optional_fields or []

    def validate(result: str) -> tuple[bool, str]:
        """Returns (is_valid, enriched_result)."""
        try:
            data = json.loads(result)
        except json.JSONDecodeError:
            enriched = json.dumps({
                "validation_error": "Result is not valid JSON",
                "raw_result": result[:200],
                "model_guidance": "The tool returned non-JSON output. Do not attempt to parse it as structured data."
            })
            return False, enriched

        if not isinstance(data, dict):
            enriched = json.dumps({
                "validation_error": f"Expected object, got {type(data).__name__}",
                "result": data,
                "model_guidance": "Unexpected result type. Extract what information you can."
            })
            return False, enriched

        if "error" in data:
            return True, result  # Error responses are valid by definition

        missing = [f for f in required_fields if f not in data]
        if missing:
            enriched = json.dumps({
                **data,
                "_validation_warning": f"Missing required fields: {missing}",
                "_fields_present": list(data.keys()),
                "_model_guidance": f"Fields {missing} are expected but absent. Work with available data."
            })
            return False, enriched

        return True, result
    return validate


# Tool validators by tool name
VALIDATORS: dict[str, Callable] = {
    "get_user": make_validator(["id", "email"], ["username", "role"]),
    "get_order": make_validator(["order_id", "status", "total"], ["items"]),
}

TOOL_REGISTRY = {
    "get_user": lambda input_data: json.dumps({"id": 1, "email": "alice@ex.com"}),
    "get_order": lambda input_data: json.dumps({"status": "shipped"}),  # Missing order_id, total
}


def execute_validated_tool(name: str, input_data: dict) -> str:
    """Execute tool and validate result before returning to agent."""
    executor = TOOL_REGISTRY.get(name)
    if not executor:
        return json.dumps({"error": f"Unknown tool: {name}"})

    raw_result = executor(input_data)

    validator = VALIDATORS.get(name)
    if validator:
        is_valid, enriched = validator(raw_result)
        if not is_valid:
            print(f"[validator] '{name}' result failed validation")
        return enriched

    return raw_result


tools_spec = [
    {
        "name": "get_user",
        "description": "Fetch a user by ID",
        "input_schema": {"type": "object", "properties": {"user_id": {"type": "integer"}}, "required": ["user_id"]}
    },
    {
        "name": "get_order",
        "description": "Fetch an order by ID",
        "input_schema": {"type": "object", "properties": {"order_id": {"type": "string"}}, "required": ["order_id"]}
    }
]


def run_validated_agent(question: str) -> str:
    messages = [{"role": "user", "content": question}]

    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            tools=tools_spec,
            messages=messages
        )

        tool_uses = [b for b in response.content if b.type == "tool_use"]
        if not tool_uses:
            return next(b.text for b in response.content if b.type == "text")

        tool_results = []
        for tu in tool_uses:
            result = execute_validated_tool(tu.name, tu.input)
            tool_results.append({"type": "tool_result", "tool_use_id": tu.id, "content": result})

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})


print(run_validated_agent("Get user 1 and their latest order"))

# Expected Token Savings: validation warnings guide model to correct approach without extra turns
# Environment: agents calling external APIs where schema stability is not guaranteed
```

---

### Option 5 — Schema versioning: include version in tool result, adapt parser

Require all tools to include a `schema_version` field. The parsing layer selects the right schema based on version.

```python
import anthropic
import json

client = anthropic.Anthropic(api_key="sk-live-...")


def parse_v1(data: dict) -> dict:
    """Parse API v1 response format."""
    return {
        "id": data.get("user_id"),
        "name": data.get("user_name"),
        "email": data.get("email"),
        "_parsed_version": "v1"
    }


def parse_v2(data: dict) -> dict:
    """Parse API v2 response format."""
    return {
        "id": data.get("id"),
        "name": data.get("username"),
        "email": data.get("email"),
        "role": data.get("role", "user"),
        "_parsed_version": "v2"
    }


SCHEMA_PARSERS = {
    "1": parse_v1,
    "1.0": parse_v1,
    "2": parse_v2,
    "2.0": parse_v2,
}
DEFAULT_PARSER = parse_v2


def version_aware_parse(raw: str) -> dict:
    """Parse tool result using the embedded schema_version to select the right parser."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        return {"error": f"Invalid JSON: {e}"}

    if "error" in data:
        return {"error": data["error"], "details": data.get("message")}

    version = str(data.pop("schema_version", "2"))
    parser = SCHEMA_PARSERS.get(version, DEFAULT_PARSER)

    if parser == DEFAULT_PARSER and version not in SCHEMA_PARSERS:
        print(f"[schema] Unknown version '{version}' — using default parser")

    return parser(data)


# Test with different schema versions
v1_response = '{"schema_version": "1", "user_id": 1, "user_name": "alice", "email": "alice@ex.com"}'
v2_response = '{"schema_version": "2", "id": 2, "username": "bob", "email": "bob@ex.com", "role": "admin"}'
unknown_version = '{"schema_version": "3", "id": 3, "username": "carol"}'

for raw in [v1_response, v2_response, unknown_version]:
    result = version_aware_parse(raw)
    print(f"v{json.loads(raw).get('schema_version')}: {result}")

# Expected Token Savings: version-aware parsing is always correct → no schema mismatch errors in agent
# Environment: long-lived agents where API versions evolve; multi-tenant deployments with mixed API versions
```

---

### Option 6 — Graceful degradation: extract what's available, skip what's missing

When schema validation fails, extract whatever valid data is present and clearly mark what's missing for the model to handle.

```python
import anthropic
import json

client = anthropic.Anthropic(api_key="sk-live-...")


def extract_gracefully(raw: str, expected_schema: dict) -> dict:
    """
    Extract fields from raw JSON using expected schema as a guide.
    Never raises — always returns something the model can work with.

    expected_schema: {field_name: {"type": "str|int|float|bool|list|dict", "required": bool}}
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {
            "_parse_error": "Not valid JSON",
            "_raw_preview": raw[:100],
            "_available": {},
            "_missing": list(expected_schema.keys()),
            "_model_note": "Tool returned non-JSON. Report error to user."
        }

    if isinstance(data, str):
        data = {"message": data}

    extracted = {}
    missing = []
    type_mismatches = []

    TYPE_MAP = {
        "str": str, "int": int, "float": (int, float),
        "bool": bool, "list": list, "dict": dict
    }

    for field, spec in expected_schema.items():
        value = data.get(field)

        if value is None:
            if spec.get("required", False):
                missing.append(field)
            continue

        expected_type = TYPE_MAP.get(spec.get("type", "str"))
        if expected_type and not isinstance(value, expected_type):
            # Try coercion
            try:
                if spec.get("type") == "int":
                    value = int(value)
                elif spec.get("type") == "str":
                    value = str(value)
            except (ValueError, TypeError):
                type_mismatches.append(f"{field}: expected {spec['type']}, got {type(value).__name__}")
                continue

        extracted[field] = value

    result = dict(extracted)
    if missing:
        result["_missing_required"] = missing
    if type_mismatches:
        result["_type_mismatches"] = type_mismatches
    if missing or type_mismatches:
        result["_model_note"] = (
            f"Partial data available. Missing required: {missing}. "
            "Use available fields and inform user of incomplete data."
        )

    return result


PAYMENT_SCHEMA = {
    "transaction_id": {"type": "str", "required": True},
    "amount": {"type": "float", "required": True},
    "status": {"type": "str", "required": True},
    "currency": {"type": "str", "required": False},
    "processed_at": {"type": "str", "required": False},
}

test_payloads = [
    '{"transaction_id": "txn_001", "amount": 99.99, "status": "completed", "currency": "USD"}',
    '{"transaction_id": "txn_002", "amount": "invalid", "status": "pending"}',
    '{"status": "failed"}',  # Missing required fields
    '{"transaction_id": "txn_003", "amount": 50, "status": "completed", "extra_field": "ignored"}',
]

for raw in test_payloads:
    result = extract_gracefully(raw, PAYMENT_SCHEMA)
    print(f"Input:  {raw[:70]}")
    print(f"Result: {result}\n")

# Expected Token Savings: partial extraction beats crash → model continues with available data
# Environment: financial, payment, and e-commerce agents where partial data is better than failure
```

---

## Comparison

| Option | Crash Prevention | Schema Mismatch Info | Versioning | Complexity |
|--------|-----------------|---------------------|------------|------------|
| 1 | Yes (.get defaults) | Partial | No | Low |
| 2 | Yes (Pydantic) | Full (ValidationError) | No | Low |
| 3 | Yes (alias inference) | Yes (field mapping) | No | Medium |
| 4 | Yes (middleware) | Yes (enriched result) | No | Medium |
| 5 | Yes (versioned parser) | No | Yes | Low |
| 6 | Yes (graceful extract) | Full | No | Low |

**Recommended starting point:** Option 1 (`.get()` with defaults) for immediate crash prevention — replace all `data["field"]` with `data.get("field")` in tool result parsers and add an upfront error key check. Takes 10 minutes. Add Option 2 (Pydantic) for APIs where schema correctness is critical and you want structured validation errors.
