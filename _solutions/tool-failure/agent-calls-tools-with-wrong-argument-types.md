---
layout: solution
title: "Agent Calls Tools with Wrong Argument Types"
category: tool-failure
description: "Agent passes a string where an integer is required, an object where an array is expected, or omits required nested fields, causing tool calls to fail with type errors."
tags: [tool-failure, tools, type-validation, json-schema, reliability]
---

## Symptom

Tool calls fail with errors like `"expected integer, got string"`, `"field 'limit' must be a number"`, or `"items must be an array"`. The agent passes `"10"` instead of `10`, `{"id": 1}` instead of `[{"id": 1}]`, or leaves required nested fields empty. The failure rate is non-zero even on well-formed user requests — the LLM is generating syntactically valid JSON but semantically wrong types.

## Root Cause

The model generates tool arguments by predicting tokens, not by executing a type-safe constructor. When the JSON schema is complex (deeply nested, mixed array/object types, enums), the model may predict a plausible-looking value that violates the schema. This is especially common for: integer fields the model represents as quoted strings, array fields when the example value was a single object, boolean fields confused with `"true"` strings, and nullable fields passed as `null` when the schema forbids null.

## Fix

### Option 1 — Strict JSON schema with type-coercing validator before tool execution

```python
import json
import anthropic
from typing import Any

client = anthropic.Anthropic()

TOOLS = [
    {
        "name": "search_products",
        "description": "Search the product catalogue.",
        "input_schema": {
            "type": "object",
            "required": ["query", "limit", "in_stock"],
            "properties": {
                "query":    {"type": "string",  "description": "Search terms"},
                "limit":    {"type": "integer", "description": "Max results (1-100)", "minimum": 1, "maximum": 100},
                "in_stock": {"type": "boolean", "description": "Filter to in-stock only"},
                "tags":     {"type": "array", "items": {"type": "string"}, "description": "Filter by tags"},
            },
        },
    }
]

def coerce_to_schema(args: dict, schema: dict) -> dict:
    """Best-effort type coercion to match JSON schema types."""
    props = schema.get("properties", {})
    coerced = dict(args)
    for field, spec in props.items():
        if field not in coerced:
            continue
        val  = coerced[field]
        typ  = spec.get("type")
        if typ == "integer" and isinstance(val, str):
            try:
                coerced[field] = int(val)
                print(f"[coerce] {field}: str→int ({val!r} → {coerced[field]})")
            except ValueError:
                pass
        elif typ == "number" and isinstance(val, str):
            try:
                coerced[field] = float(val)
                print(f"[coerce] {field}: str→float")
            except ValueError:
                pass
        elif typ == "boolean" and isinstance(val, str):
            coerced[field] = val.lower() in {"true", "1", "yes"}
            print(f"[coerce] {field}: str→bool ({val!r} → {coerced[field]})")
        elif typ == "array" and not isinstance(val, list):
            coerced[field] = [val] if val is not None else []
            print(f"[coerce] {field}: scalar→array")
    return coerced

def execute_tool(name: str, raw_args: dict) -> str:
    tool_schema = next(t for t in TOOLS if t["name"] == name)
    args = coerce_to_schema(raw_args, tool_schema["input_schema"])
    print(f"[tool] {name}({args})")
    # Simulated execution
    return json.dumps({"results": [{"id": 1, "name": "Widget Pro", "in_stock": True}], "total": 1})

def run_agent(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]
    for _ in range(6):
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            tools=TOOLS,
            messages=messages,
        )
        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if b.type == "text"), "")
        messages.append({"role": "assistant", "content": response.content})
        results = []
        for b in response.content:
            if b.type == "tool_use":
                result = execute_tool(b.name, b.input)
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": result})
        messages.append({"role": "user", "content": results})
    return "max steps reached"

print(run_agent("Search for widgets, limit to 5, show only in-stock items"))
```

**Expected Token Savings:** Coercion eliminates tool-failure retry turns (~300-500 tokens each); one coerce call costs microseconds with no API tokens.
**Environment:** Any agent using tools with integer, boolean, or array fields; coercion should be a standard pre-execution layer.

---

### Option 2 — Schema-injected examples in tool description

```python
import json
import anthropic

client = anthropic.Anthropic()

def make_tool_with_examples(base_tool: dict, examples: list[dict]) -> dict:
    """Append concrete examples to the tool description to guide the model."""
    tool = dict(base_tool)
    example_lines = []
    for ex in examples:
        example_lines.append(f"Example input: {json.dumps(ex['input'])}")
    tool["description"] = tool["description"].rstrip(".") + ". " + " | ".join(example_lines)
    return tool

BASE_TOOL = {
    "name": "create_order",
    "description": "Create a new customer order.",
    "input_schema": {
        "type": "object",
        "required": ["customer_id", "items", "priority"],
        "properties": {
            "customer_id": {"type": "integer"},
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["sku", "quantity"],
                    "properties": {
                        "sku":      {"type": "string"},
                        "quantity": {"type": "integer"},
                    },
                },
            },
            "priority": {"type": "string", "enum": ["low", "normal", "high"]},
            "rush":     {"type": "boolean"},
        },
    },
}

TOOL_WITH_EXAMPLES = make_tool_with_examples(BASE_TOOL, [
    {"input": {"customer_id": 1042, "items": [{"sku": "WGT-001", "quantity": 3}], "priority": "normal", "rush": False}},
    {"input": {"customer_id": 7, "items": [{"sku": "A-1", "quantity": 1}, {"sku": "B-2", "quantity": 2}], "priority": "high", "rush": True}},
])

def simulated_create_order(args: dict) -> dict:
    # Validate types
    errors = []
    if not isinstance(args.get("customer_id"), int):
        errors.append(f"customer_id must be integer, got {type(args.get('customer_id')).__name__}")
    if not isinstance(args.get("items"), list):
        errors.append(f"items must be array, got {type(args.get('items')).__name__}")
    if errors:
        return {"error": "; ".join(errors)}
    return {"order_id": 9901, "status": "created"}

def run_agent(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]
    for _ in range(6):
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            tools=[TOOL_WITH_EXAMPLES],
            messages=messages,
        )
        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if b.type == "text"), "")
        messages.append({"role": "assistant", "content": response.content})
        results = []
        for b in response.content:
            if b.type == "tool_use":
                result = simulated_create_order(b.input)
                print(f"[tool] input={b.input} → result={result}")
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": json.dumps(result)})
        messages.append({"role": "user", "content": results})
    return "max steps reached"

print(run_agent("Order 2 units of SKU-XJ9 and 1 unit of SKU-AB3 for customer 555, normal priority"))
```

**Expected Token Savings:** Examples add ~100 tokens to tool descriptions but prevent wrong-type calls; each prevented failure saves a full retry turn.
**Environment:** Tools with complex nested schemas or enum fields; few-shot examples in descriptions are the highest-ROI fix.

---

### Option 3 — Pydantic validator as tool call firewall

```python
import json
import anthropic
from pydantic import BaseModel, Field, field_validator, model_config
from typing import Any

client = anthropic.Anthropic()

class SearchArgs(BaseModel):
    model_config = model_config(extra="forbid")

    query:    str
    limit:    int   = Field(default=10, ge=1, le=100)
    in_stock: bool  = False
    tags:     list[str] = Field(default_factory=list)

    @field_validator("limit", mode="before")
    @classmethod
    def coerce_limit(cls, v: Any) -> int:
        if isinstance(v, str):
            return int(v)
        return v

    @field_validator("in_stock", mode="before")
    @classmethod
    def coerce_bool(cls, v: Any) -> bool:
        if isinstance(v, str):
            return v.lower() in {"true", "1", "yes"}
        return v

    @field_validator("tags", mode="before")
    @classmethod
    def coerce_tags(cls, v: Any) -> list[str]:
        if isinstance(v, str):
            return [v]
        return v or []

TOOLS = [
    {
        "name": "search_products",
        "description": "Search the product catalogue by query and optional filters.",
        "input_schema": SearchArgs.model_json_schema(),
    }
]

def safe_execute_tool(name: str, raw_input: dict) -> str:
    validators = {"search_products": SearchArgs}
    validator = validators.get(name)
    if not validator:
        return json.dumps({"error": f"unknown tool: {name}"})

    try:
        args = validator.model_validate(raw_input)
        print(f"[tool] validated args: {args.model_dump()}")
    except Exception as e:
        print(f"[tool] VALIDATION FAILED: {e}")
        return json.dumps({"error": str(e), "hint": "Check argument types and required fields."})

    # Simulated execution with validated args
    return json.dumps({"results": [{"name": "Widget", "in_stock": args.in_stock}], "query": args.query})

def run_agent(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]
    for _ in range(6):
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            tools=TOOLS,
            messages=messages,
        )
        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if b.type == "text"), "")
        messages.append({"role": "assistant", "content": response.content})
        results = []
        for b in response.content:
            if b.type == "tool_use":
                result = safe_execute_tool(b.name, b.input)
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": result})
        messages.append({"role": "user", "content": results})
    return "max steps reached"

print(run_agent("Find 20 in-stock items matching 'gadget'"))
```

**Expected Token Savings:** Pydantic validation catches type errors before execution and returns structured error messages that guide the model to self-correct in one retry.
**Environment:** Production agents; Pydantic validators are the most robust approach and serve as living documentation of the schema.

---

### Option 4 — Tool call diffing: detect and log type mismatches before execution

```python
import json
import anthropic
from typing import Any

client = anthropic.Anthropic()

def check_types(args: dict, schema: dict, path: str = "") -> list[str]:
    """Recursively check argument types against JSON schema. Returns list of errors."""
    errors = []
    props = schema.get("properties", {})
    required = schema.get("required", [])

    for field in required:
        if field not in args:
            errors.append(f"{path}{field}: required but missing")

    for field, val in args.items():
        spec = props.get(field)
        if not spec:
            continue   # unknown field — let the tool handle it
        fpath = f"{path}{field}."
        expected = spec.get("type")

        if expected == "integer" and not isinstance(val, int):
            errors.append(f"{path}{field}: expected integer, got {type(val).__name__} ({val!r})")
        elif expected == "number" and not isinstance(val, (int, float)):
            errors.append(f"{path}{field}: expected number, got {type(val).__name__}")
        elif expected == "boolean" and not isinstance(val, bool):
            errors.append(f"{path}{field}: expected boolean, got {type(val).__name__} ({val!r})")
        elif expected == "array" and not isinstance(val, list):
            errors.append(f"{path}{field}: expected array, got {type(val).__name__}")
        elif expected == "string" and not isinstance(val, str):
            errors.append(f"{path}{field}: expected string, got {type(val).__name__}")
        elif expected == "object" and isinstance(val, dict):
            sub_errors = check_types(val, spec, fpath)
            errors.extend(sub_errors)
        elif expected == "array" and isinstance(val, list):
            item_schema = spec.get("items", {})
            for i, item in enumerate(val):
                if isinstance(item, dict) and item_schema.get("type") == "object":
                    sub_errors = check_types(item, item_schema, f"{path}{field}[{i}].")
                    errors.extend(sub_errors)

    return errors

TOOLS = [
    {
        "name": "book_appointment",
        "description": "Book a calendar appointment.",
        "input_schema": {
            "type": "object",
            "required": ["title", "duration_minutes", "attendees", "send_invite"],
            "properties": {
                "title":            {"type": "string"},
                "duration_minutes": {"type": "integer"},
                "attendees":        {"type": "array", "items": {"type": "string"}},
                "send_invite":      {"type": "boolean"},
                "room_id":          {"type": "integer"},
            },
        },
    }
]

def execute_tool(name: str, raw_args: dict) -> str:
    tool = next(t for t in TOOLS if t["name"] == name)
    errors = check_types(raw_args, tool["input_schema"])
    if errors:
        print(f"[type-check] ERRORS for {name}: {errors}")
        return json.dumps({"error": "Type mismatch in arguments", "details": errors})
    print(f"[type-check] OK for {name}")
    return json.dumps({"booking_id": "BK-2024-001", "status": "confirmed"})

def run_agent(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]
    for _ in range(8):
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            tools=TOOLS,
            messages=messages,
        )
        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if b.type == "text"), "")
        messages.append({"role": "assistant", "content": response.content})
        results = []
        for b in response.content:
            if b.type == "tool_use":
                result = execute_tool(b.name, b.input)
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": result})
        messages.append({"role": "user", "content": results})
    return "max steps reached"

print(run_agent("Book a 30-minute meeting called 'Sprint Review' with alice@co.com and bob@co.com, send invites"))
```

**Expected Token Savings:** Type diffing returns precise error messages that let the model fix one field rather than regenerating the entire tool call.
**Environment:** Development and staging; type diffing also serves as observability — log errors to detect systematic schema mismatches.

---

### Option 5 — Constrained generation with `tool_choice` and schema enforcement

```python
import json
import anthropic

client = anthropic.Anthropic()

TOOLS = [
    {
        "name": "calculate_shipping",
        "description": "Calculate shipping cost for an order.",
        "input_schema": {
            "type": "object",
            "required": ["weight_kg", "distance_km", "express"],
            "properties": {
                "weight_kg":   {"type": "number",  "minimum": 0.01, "maximum": 1000},
                "distance_km": {"type": "integer", "minimum": 1},
                "express":     {"type": "boolean"},
                "insurance":   {"type": "boolean"},
            },
        },
    }
]

def force_tool_call(user_message: str, tool_name: str) -> dict:
    """Force the model to call a specific tool — ensures structured output."""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        tools=TOOLS,
        tool_choice={"type": "tool", "name": tool_name},   # forces specific tool
        messages=[{"role": "user", "content": user_message}],
    )
    for block in response.content:
        if block.type == "tool_use" and block.name == tool_name:
            return block.input
    return {}

def calculate_shipping(weight_kg: float, distance_km: int, express: bool, insurance: bool = False) -> float:
    base = weight_kg * 0.5 + distance_km * 0.02
    if express:
        base *= 1.5
    if insurance:
        base += 5.0
    return round(base, 2)

def answer_shipping_query(user_message: str) -> str:
    raw_args = force_tool_call(user_message, "calculate_shipping")
    print(f"[tool] raw args: {raw_args}")

    # Coerce types
    try:
        weight   = float(raw_args.get("weight_kg", 0))
        distance = int(raw_args.get("distance_km", 0))
        express  = bool(raw_args.get("express", False))
        insurance = bool(raw_args.get("insurance", False))
    except (TypeError, ValueError) as e:
        return f"Could not parse shipping details: {e}"

    cost = calculate_shipping(weight, distance, express, insurance)

    # Final natural language response
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{
            "role": "user",
            "content": f"Shipping cost calculated: ${cost:.2f} for {weight}kg, {distance}km, express={express}. Tell the user.",
        }],
    )
    return response.content[0].text

queries = [
    "How much to ship a 2.5kg package 150km by express?",
    "Shipping cost for 0.5kg, 50km, regular delivery",
]
for q in queries:
    print(f"Q: {q}")
    print(f"A: {answer_shipping_query(q)}\n")
```

**Expected Token Savings:** `tool_choice: {type: "tool"}` guarantees a tool call is made; combining with coercion ensures the output is always usable without retry turns.
**Environment:** Structured data extraction workflows where the model must always return typed arguments.

---

### Option 6 — Schema simplification: flatten complex nested schemas

```python
import json
import anthropic

client = anthropic.Anthropic()

# BEFORE: complex nested schema — high type-error rate
COMPLEX_TOOL = {
    "name": "create_invoice",
    "description": "Create an invoice with line items.",
    "input_schema": {
        "type": "object",
        "required": ["customer", "line_items", "payment"],
        "properties": {
            "customer": {
                "type": "object",
                "required": ["id", "email"],
                "properties": {
                    "id":    {"type": "integer"},
                    "email": {"type": "string"},
                    "name":  {"type": "string"},
                },
            },
            "line_items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["sku", "qty", "unit_price"],
                    "properties": {
                        "sku":        {"type": "string"},
                        "qty":        {"type": "integer"},
                        "unit_price": {"type": "number"},
                    },
                },
            },
            "payment": {
                "type": "object",
                "required": ["method", "due_days"],
                "properties": {
                    "method":   {"type": "string", "enum": ["card", "bank", "crypto"]},
                    "due_days": {"type": "integer"},
                },
            },
        },
    },
}

# AFTER: flattened schema — lower type-error rate, easier for the model
FLAT_TOOL = {
    "name": "create_invoice",
    "description": (
        "Create an invoice. Pass line_items as a JSON string array: "
        "'[{\"sku\":\"A-1\",\"qty\":2,\"unit_price\":9.99}]'. "
        "payment_method must be one of: card, bank, crypto."
    ),
    "input_schema": {
        "type": "object",
        "required": ["customer_id", "customer_email", "line_items_json", "payment_method", "payment_due_days"],
        "properties": {
            "customer_id":       {"type": "integer"},
            "customer_email":    {"type": "string"},
            "customer_name":     {"type": "string"},
            "line_items_json":   {"type": "string", "description": "JSON array of line items"},
            "payment_method":    {"type": "string", "enum": ["card", "bank", "crypto"]},
            "payment_due_days":  {"type": "integer"},
        },
    },
}

def parse_flat_invoice_args(args: dict) -> dict:
    """Reconstruct the nested structure from flat args."""
    line_items = json.loads(args.get("line_items_json", "[]"))
    return {
        "customer":   {"id": args["customer_id"], "email": args["customer_email"], "name": args.get("customer_name", "")},
        "line_items": line_items,
        "payment":    {"method": args["payment_method"], "due_days": args["payment_due_days"]},
    }

def run_agent(user_message: str, use_flat: bool = True) -> str:
    tool = FLAT_TOOL if use_flat else COMPLEX_TOOL
    messages = [{"role": "user", "content": user_message}]
    for _ in range(6):
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            tools=[tool],
            messages=messages,
        )
        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if b.type == "text"), "")
        messages.append({"role": "assistant", "content": response.content})
        results = []
        for b in response.content:
            if b.type == "tool_use":
                if use_flat:
                    structured = parse_flat_invoice_args(b.input)
                else:
                    structured = b.input
                print(f"[tool] structured={json.dumps(structured, indent=2)[:300]}")
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": json.dumps({"invoice_id": "INV-001"})})
        messages.append({"role": "user", "content": results})
    return "max steps reached"

query = "Create invoice for customer ID 42 (alice@example.com), 2x Widget at $15 each, pay by bank in 30 days"
print("Flat schema:")
print(run_agent(query, use_flat=True))
```

**Expected Token Savings:** Flat schemas reduce nesting depth; each removed nesting level reduces type-error probability; simpler schemas also reduce the prompt tokens used to describe the tool.
**Environment:** Complex domain tools (invoicing, orders, bookings); flatten when type errors persist despite coercion.

---

## Comparison

| Option | Fix Layer | Handles Nesting | Auto-Corrects | Best For |
|---|---|---|---|---|
| 1. Type coercion validator | Pre-execution | No | Yes | Quick win — coerce common str→int/bool/array mistakes |
| 2. Schema + examples in description | Prompt | No | N/A | Prevention — reduces error rate at source |
| 3. Pydantic firewall | Pre-execution | Yes | Yes | Production — living schema documentation + validation |
| 4. Type diffing + error return | Pre-execution | Yes | No | Observability — precise errors guide model self-correction |
| 5. `tool_choice` forced call | Prompt | No | Partial | Guaranteed tool invocation for structured extraction |
| 6. Schema simplification | Schema design | N/A | N/A | High-complexity tools with persistent nested type errors |
