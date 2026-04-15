---
layout: solution
title: "Agent Invents API Parameters That Don't Exist"
category: hallucination
description: "Agent generates tool calls with parameter names or values that are not part of the actual tool schema — causing validation errors, silent data corruption, or unexpected API behaviour."
tags: [hallucination, tool-use, schema-validation, api, pydantic, guardrails]
---

## Symptom

The agent calls a tool with invented parameters:

```python
# Actual schema: get_user(user_id: str)
# Agent calls:
get_user(user_id="alice", include_metadata=True, format="json")
#                          ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
#                          These parameters do not exist
```

The extra parameters are silently ignored, or worse, passed through to a real API that rejects them with a cryptic error. The agent never learns the call failed.

## Root Cause

The model draws on training data about similar APIs when generating tool inputs. If tool descriptions are vague or the model has seen similar-looking APIs with different parameter sets, it confabulates plausible-sounding parameters. Without strict schema enforcement at the call boundary, invalid calls proceed.

## Fix

---

### Option 1 — Strict Input Validation with Pydantic Before Execution

Parse every tool call input through a Pydantic model before executing it. Reject calls with unknown fields and return a schema error back to the agent.

```python
import json
import anthropic
from pydantic import BaseModel, ValidationError, model_validator

client = anthropic.Anthropic()

# Strict Pydantic models for each tool
class GetUserInput(BaseModel):
    model_config = {"extra": "forbid"}  # Reject unknown fields

    user_id: str

class SearchProductsInput(BaseModel):
    model_config = {"extra": "forbid"}

    query: str
    max_results: int = 10

class CreateOrderInput(BaseModel):
    model_config = {"extra": "forbid"}

    user_id: str
    product_ids: list[str]
    quantity: int = 1

TOOL_MODELS = {
    "get_user": GetUserInput,
    "search_products": SearchProductsInput,
    "create_order": CreateOrderInput,
}

TOOLS = [
    {
        "name": "get_user",
        "description": "Retrieve a user by their ID.",
        "input_schema": {
            "type": "object",
            "properties": {
                "user_id": {"type": "string", "description": "The user's unique identifier"},
            },
            "required": ["user_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "search_products",
        "description": "Search the product catalogue.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "max_results": {"type": "integer", "default": 10},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
]

def validated_tool_call(name: str, raw_input: dict) -> str:
    model_class = TOOL_MODELS.get(name)
    if not model_class:
        return json.dumps({"error": f"Unknown tool: {name}"})

    try:
        validated = model_class(**raw_input)
    except ValidationError as e:
        error_details = []
        for err in e.errors():
            loc = ".".join(str(x) for x in err["loc"])
            error_details.append(f"{loc}: {err['msg']}")

        return json.dumps({
            "error": "Invalid tool parameters",
            "details": error_details,
            "hint": (
                f"Tool '{name}' only accepts these parameters: "
                f"{list(model_class.model_fields.keys())}. "
                "Remove any extra parameters and retry."
            ),
        })

    # Execute with validated data only
    data = validated.model_dump()
    print(f"[VALID] {name}({data})")

    # Simulate execution
    if name == "get_user":
        return json.dumps({"id": data["user_id"], "name": "Alice", "email": "alice@example.com"})
    if name == "search_products":
        return json.dumps({"results": [{"name": "Widget", "price": 9.99}], "count": 1})
    return json.dumps({"status": "ok"})

def run_agent(task: str) -> str:
    messages = [{"role": "user", "content": task}]

    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=TOOLS,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if b.type == "text"), "")

        messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                result = validated_tool_call(block.name, block.input)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })
        messages.append({"role": "user", "content": tool_results})

print(run_agent("Look up the user with ID user-42."))
```

**Expected Token Savings:** None — correctness fix; prevents silent invalid API calls
**Environment:** `pip install anthropic pydantic`

---

### Option 2 — Schema Diff Alerting: Detect Parameters Not in Schema

Compare the agent's tool call inputs against the declared schema at runtime. Log any extra keys as hallucinated parameters before the call executes.

```python
import json
import anthropic
from typing import Any

def extract_allowed_keys(schema: dict, path: str = "") -> set[str]:
    """Recursively extract all property names from a JSON schema."""
    allowed = set()
    props = schema.get("properties", {})
    for key in props:
        allowed.add(key)
        nested = props[key]
        if nested.get("type") == "object":
            for sub in extract_allowed_keys(nested, f"{path}.{key}"):
                allowed.add(sub)
    return allowed

def check_for_hallucinated_params(
    tool_name: str,
    tool_input: dict,
    tool_schema: dict,
) -> list[str]:
    allowed = extract_allowed_keys(tool_schema.get("input_schema", {}))
    extra = [k for k in tool_input if k not in allowed]
    return extra

TOOLS = [
    {
        "name": "send_email",
        "description": "Send an email to a recipient.",
        "input_schema": {
            "type": "object",
            "properties": {
                "to": {"type": "string"},
                "subject": {"type": "string"},
                "body": {"type": "string"},
            },
            "required": ["to", "subject", "body"],
        },
    },
    {
        "name": "get_weather",
        "description": "Get current weather for a city.",
        "input_schema": {
            "type": "object",
            "properties": {
                "city": {"type": "string"},
                "units": {"type": "string", "enum": ["celsius", "fahrenheit"]},
            },
            "required": ["city"],
        },
    },
]

TOOL_SCHEMA_MAP = {t["name"]: t for t in TOOLS}

def audited_tool_call(name: str, tool_input: dict) -> str:
    schema = TOOL_SCHEMA_MAP.get(name)
    if not schema:
        return json.dumps({"error": f"Unknown tool: {name}"})

    hallucinated = check_for_hallucinated_params(name, tool_input, schema)
    if hallucinated:
        print(f"[HALLUCINATION ALERT] Tool '{name}' received unknown params: {hallucinated}")
        # Strip hallucinated params and proceed with valid ones
        allowed_keys = extract_allowed_keys(schema["input_schema"])
        cleaned_input = {k: v for k, v in tool_input.items() if k in allowed_keys}
        print(f"[CLEANED INPUT] {cleaned_input}")
        tool_input = cleaned_input

    # Simulate execution
    print(f"[EXEC] {name}({tool_input})")
    if name == "send_email":
        return json.dumps({"status": "sent", "to": tool_input.get("to")})
    if name == "get_weather":
        return json.dumps({"city": tool_input.get("city"), "temp": 22, "condition": "sunny"})
    return json.dumps({"status": "ok"})

client = anthropic.Anthropic()

def run_audited_agent(task: str) -> str:
    messages = [{"role": "user", "content": task}]

    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=TOOLS,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if b.type == "text"), "")

        messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                result = audited_tool_call(block.name, block.input)
                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})
        messages.append({"role": "user", "content": tool_results})

print(run_audited_agent("Send a welcome email to alice@example.com about our new product."))
```

**Expected Token Savings:** None — observability fix; hallucinated params are caught and stripped
**Environment:** `pip install anthropic`

---

### Option 3 — Canonical Example Values in Tool Descriptions

Embed explicit parameter examples in tool descriptions. When the model sees exact valid parameter names in the description, it is less likely to invent alternatives.

```python
import json
import anthropic

client = anthropic.Anthropic()

# Vague description (bad) — model may invent extra params
VAGUE_TOOLS = [{
    "name": "filter_users",
    "description": "Filter users by various criteria.",
    "input_schema": {
        "type": "object",
        "properties": {
            "status": {"type": "string"},
            "limit": {"type": "integer"},
        },
    },
}]

# Explicit description (good) — exact params listed with examples
EXPLICIT_TOOLS = [{
    "name": "filter_users",
    "description": (
        "Filter users from the database. "
        "EXACT parameters:\n"
        '  status (string): must be exactly "active", "inactive", or "pending"\n'
        "  limit (integer): max results to return, default 20, max 100\n\n"
        "Valid call example: filter_users(status='active', limit=50)\n"
        "Do NOT add any other parameters — there are no others."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": ["active", "inactive", "pending"],
                "description": "User status filter. Must be one of: active, inactive, pending",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 100,
                "default": 20,
                "description": "Maximum number of results (1-100)",
            },
        },
        "additionalProperties": False,
    },
}]

def simulate_filter_users(status: str, limit: int = 20) -> list[dict]:
    return [{"id": f"user-{i}", "status": status} for i in range(min(limit, 3))]

def run_comparison(task: str, tools: list, label: str) -> None:
    print(f"\n=== {label} ===")
    messages = [{"role": "user", "content": task}]

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        tools=tools,
        messages=messages,
    )

    for block in response.content:
        if block.type == "tool_use":
            print(f"Tool called: {block.name}")
            print(f"Parameters: {json.dumps(block.input, indent=2)}")
            allowed = {"status", "limit"}
            hallucinated = [k for k in block.input if k not in allowed]
            if hallucinated:
                print(f"HALLUCINATED params: {hallucinated}")
            else:
                print("All parameters valid.")

task = "Get me the list of active users. Show up to 10 results sorted by name."

run_comparison(task, VAGUE_TOOLS, "Vague description")
run_comparison(task, EXPLICIT_TOOLS, "Explicit description")
```

**Expected Token Savings:** ~10% fewer correction turns due to cleaner first calls
**Environment:** `pip install anthropic`

---

### Option 4 — JSON Schema `additionalProperties: false` Enforcement

Set `additionalProperties: false` in every tool schema. This signals to the model — and enforces at validation — that extra parameters are invalid.

```python
import json
import jsonschema
import anthropic

client = anthropic.Anthropic()

# Tools with strict additionalProperties: false
STRICT_TOOLS = [
    {
        "name": "create_ticket",
        "description": "Create a support ticket. Only the listed parameters are accepted.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Ticket title (max 100 chars)"},
                "priority": {
                    "type": "string",
                    "enum": ["low", "medium", "high", "critical"],
                    "description": "Ticket priority",
                },
                "description": {"type": "string", "description": "Detailed description"},
            },
            "required": ["title", "priority"],
            "additionalProperties": False,
        },
    },
    {
        "name": "assign_ticket",
        "description": "Assign a ticket to a team member.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticket_id": {"type": "string"},
                "assignee_id": {"type": "string"},
            },
            "required": ["ticket_id", "assignee_id"],
            "additionalProperties": False,
        },
    },
]

TOOL_SCHEMAS = {t["name"]: t["input_schema"] for t in STRICT_TOOLS}

def validate_and_execute(name: str, tool_input: dict) -> str:
    schema = TOOL_SCHEMAS.get(name)
    if not schema:
        return json.dumps({"error": f"Unknown tool: {name}"})

    try:
        jsonschema.validate(instance=tool_input, schema=schema)
    except jsonschema.ValidationError as e:
        return json.dumps({
            "error": "Schema validation failed",
            "message": e.message,
            "path": list(e.absolute_path),
            "hint": f"Allowed parameters for '{name}': {list(schema.get('properties', {}).keys())}",
        })

    # Valid — execute
    print(f"[VALIDATED] {name}({tool_input})")
    if name == "create_ticket":
        return json.dumps({"ticket_id": "TKT-001", "status": "created", **tool_input})
    if name == "assign_ticket":
        return json.dumps({"ticket_id": tool_input["ticket_id"], "status": "assigned"})
    return json.dumps({"status": "ok"})

def run_strict_agent(task: str) -> str:
    messages = [{"role": "user", "content": task}]

    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=STRICT_TOOLS,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if b.type == "text"), "")

        messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                result = validate_and_execute(block.name, block.input)
                result_data = json.loads(result)
                if "error" in result_data:
                    print(f"[SCHEMA ERROR] {result_data}")
                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})
        messages.append({"role": "user", "content": tool_results})

print(run_strict_agent("Create a critical ticket for the login bug and assign it to engineer-7."))
```

**Expected Token Savings:** None — schema enforcement; `additionalProperties: false` also signals to the model
**Environment:** `pip install anthropic jsonschema`

---

### Option 5 — Tool Call Mirror: Echo Schema Back on Invalid Call

When an invalid tool call is detected, return the correct schema as the tool result. The agent sees exactly what it should have passed and self-corrects on the next turn.

```python
import json
import anthropic

client = anthropic.Anthropic()

TOOLS = [
    {
        "name": "run_query",
        "description": "Run a read-only SQL query against the analytics database.",
        "input_schema": {
            "type": "object",
            "properties": {
                "sql": {"type": "string", "description": "The SQL SELECT statement to run"},
                "timeout_seconds": {"type": "integer", "default": 30, "description": "Query timeout"},
            },
            "required": ["sql"],
            "additionalProperties": False,
        },
    },
]

ALLOWED_PARAMS = {t["name"]: set(t["input_schema"]["properties"].keys()) for t in TOOLS}

def mirror_schema_on_error(name: str, tool_input: dict) -> str | None:
    """Return corrective schema message if input has unknown params, else None."""
    allowed = ALLOWED_PARAMS.get(name, set())
    extra = [k for k in tool_input if k not in allowed]
    if not extra:
        return None

    tool_schema = next(t for t in TOOLS if t["name"] == name)
    return json.dumps({
        "error": f"Unknown parameters: {extra}",
        "correct_schema": {
            "name": name,
            "parameters": tool_schema["input_schema"]["properties"],
            "required": tool_schema["input_schema"].get("required", []),
        },
        "instruction": (
            f"Remove the unknown parameters {extra} and retry. "
            f"Only these parameters are accepted: {list(allowed)}"
        ),
    })

def execute_query(sql: str, timeout_seconds: int = 30) -> str:
    print(f"[SQL] {sql} (timeout={timeout_seconds}s)")
    return json.dumps({"rows": [{"count": 42}], "duration_ms": 85})

def run_agent_with_mirror(task: str) -> str:
    messages = [{"role": "user", "content": task}]

    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=TOOLS,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if b.type == "text"), "")

        messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                error_result = mirror_schema_on_error(block.name, block.input)
                if error_result:
                    result = error_result
                    print(f"[SCHEMA MIRROR] Corrective schema returned to agent")
                else:
                    result = execute_query(**{
                        k: v for k, v in block.input.items()
                        if k in ALLOWED_PARAMS.get(block.name, set())
                    })
                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})
        messages.append({"role": "user", "content": tool_results})

print(run_agent_with_mirror("Count all active users in the database."))
```

**Expected Token Savings:** None — self-correction reduces multi-turn error recovery loops
**Environment:** `pip install anthropic`

---

### Option 6 — Regression Test Suite for Tool Call Schemas

Maintain a test suite that runs the agent against known prompts and asserts that only valid parameters appear in tool calls. Catches hallucination regressions before deployment.

```python
import json
import anthropic
from dataclasses import dataclass

@dataclass
class ToolCallAssertion:
    expected_tool: str
    allowed_params: set[str]
    required_params: set[str]
    forbidden_params: set[str] = None

    def __post_init__(self):
        if self.forbidden_params is None:
            self.forbidden_params = set()

@dataclass
class TestCase:
    prompt: str
    assertion: ToolCallAssertion

TOOL_SCHEMAS = [
    {
        "name": "get_order",
        "description": "Retrieve an order by its ID.",
        "input_schema": {
            "type": "object",
            "properties": {"order_id": {"type": "string"}},
            "required": ["order_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_orders",
        "description": "List orders for a user.",
        "input_schema": {
            "type": "object",
            "properties": {
                "user_id": {"type": "string"},
                "status": {"type": "string", "enum": ["pending", "shipped", "delivered"]},
                "limit": {"type": "integer"},
            },
            "required": ["user_id"],
            "additionalProperties": False,
        },
    },
]

TEST_CASES = [
    TestCase(
        prompt="Get order ORD-123.",
        assertion=ToolCallAssertion(
            expected_tool="get_order",
            allowed_params={"order_id"},
            required_params={"order_id"},
            forbidden_params={"format", "include_items", "expand", "fields"},
        ),
    ),
    TestCase(
        prompt="List all shipped orders for user user-42.",
        assertion=ToolCallAssertion(
            expected_tool="list_orders",
            allowed_params={"user_id", "status", "limit"},
            required_params={"user_id"},
            forbidden_params={"sort", "order_by", "page", "offset", "filter"},
        ),
    ),
]

def run_schema_regression_tests() -> dict:
    client = anthropic.Anthropic()
    passed = 0
    failed = 0
    failures = []

    for tc in TEST_CASES:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            tools=TOOL_SCHEMAS,
            messages=[{"role": "user", "content": tc.prompt}],
        )

        tool_calls = [b for b in response.content if b.type == "tool_use"]

        if not tool_calls:
            failures.append({"prompt": tc.prompt, "error": "No tool call made"})
            failed += 1
            continue

        call = tool_calls[0]
        assertion = tc.assertion
        errors = []

        if call.name != assertion.expected_tool:
            errors.append(f"Wrong tool: got '{call.name}', expected '{assertion.expected_tool}'")

        extra_params = set(call.input.keys()) - assertion.allowed_params
        if extra_params:
            errors.append(f"Hallucinated params: {extra_params}")

        missing = assertion.required_params - set(call.input.keys())
        if missing:
            errors.append(f"Missing required params: {missing}")

        forbidden_used = assertion.forbidden_params & set(call.input.keys())
        if forbidden_used:
            errors.append(f"Forbidden params used: {forbidden_used}")

        if errors:
            failures.append({"prompt": tc.prompt, "errors": errors, "actual_input": call.input})
            failed += 1
        else:
            passed += 1
            print(f"PASS: {tc.prompt[:60]}")

    for f in failures:
        print(f"FAIL: {f['prompt'][:60]}")
        for err in f.get("errors", [f.get("error", "")]):
            print(f"  - {err}")

    return {"passed": passed, "failed": failed, "total": len(TEST_CASES)}

results = run_schema_regression_tests()
print(f"\nResults: {results['passed']}/{results['total']} passed")
```

**Expected Token Savings:** None — CI/CD guardrail; catches hallucination regressions before they reach production
**Environment:** `pip install anthropic`

---

## Comparison

| Option | Prevention Layer | Auto-Correct | Blocks Execution | Best For |
|--------|----------------|--------------|-----------------|----------|
| Pydantic Validation | Runtime | No (rejects) | Yes | All agents |
| Schema Diff Alerting | Runtime | Yes (strips) | No | Monitoring |
| Explicit Description | Prompt level | N/A | No | Reducing hallucination rate |
| additionalProperties: false | Schema level | No | Yes | Schema-first enforcement |
| Schema Mirror | Runtime | Self-corrects | No | Agent self-correction |
| Regression Tests | CI/CD | N/A | Pre-deploy | Quality assurance |

**Recommended starting point:** Option 1 (Pydantic Validation) for runtime enforcement; Option 3 (Explicit Descriptions) for upstream prevention. Apply both together.
