---
layout: solution
title: "Agent Passes Wrong Argument Types to Tools"
category: tool-failure
description: "The agent calls tools with strings where integers are expected, None where lists are required, or nested objects instead of flat IDs. Tool execution fails, the agent retries endlessly, or silently produces wrong results."
tags: [tool-use, type-validation, pydantic, schema, argument-coercion, tool-failure]
---

## Symptom

A tool call arrives with `{"count": "5"}` instead of `{"count": 5}`, or `{"user_id": {"id": 42}}` instead of `{"user_id": 42}`. The tool raises a `TypeError`, the agent sees a tool error response, and either retries the same bad call or propagates the error to the user. Alternatively, the type is silently coerced in a language like Python and the tool runs with subtly wrong values (e.g., `"5" * 3 = "555"` instead of `15`).

## Root Cause

The Anthropic API returns tool `input` as a raw JSON-deserialized Python dict. The agent's language model extracts values from context and formats them as it sees fit — often wrapping IDs in objects, passing numeric strings instead of integers, or omitting optional-but-required fields. Without explicit validation at the tool call boundary, bad arguments reach tool implementations.

```python
# Anti-pattern: no validation
def execute_tool(name: str, arguments: dict) -> str:
    if name == "get_user":
        return db.get_user(arguments["user_id"])  # may be str, dict, or None
```

## Fix

---

### Option 1: Pydantic Argument Validation at Tool Boundary

Define Pydantic models for each tool's arguments. Validate and coerce before execution.

```python
import json
import anthropic
from pydantic import BaseModel, Field, validator, ValidationError
from typing import Optional

client = anthropic.Anthropic()


# Tool argument schemas
class SearchArgs(BaseModel):
    query: str = Field(..., min_length=1, max_length=500)
    max_results: int = Field(default=10, ge=1, le=100)
    include_metadata: bool = Field(default=False)

    @validator("max_results", pre=True)
    def coerce_to_int(cls, v):
        """Accept string integers from LLM output."""
        try:
            return int(v)
        except (TypeError, ValueError):
            return 10  # safe default


class CreateNoteArgs(BaseModel):
    title: str = Field(..., min_length=1)
    content: str = Field(..., min_length=1)
    tags: list[str] = Field(default_factory=list)
    priority: Optional[int] = Field(default=None, ge=1, le=5)

    @validator("tags", pre=True)
    def coerce_tags(cls, v):
        """Accept comma-separated string or list."""
        if isinstance(v, str):
            return [t.strip() for t in v.split(",") if t.strip()]
        return v or []


# Tool registry with schema mapping
TOOL_SCHEMAS = {
    "search_documents": SearchArgs,
    "create_note":      CreateNoteArgs,
}

TOOLS = [
    {
        "name": "search_documents",
        "description": "Search the document store.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query":            {"type": "string"},
                "max_results":      {"type": "integer", "default": 10},
                "include_metadata": {"type": "boolean", "default": False},
            },
            "required": ["query"],
        },
    },
    {
        "name": "create_note",
        "description": "Create a new note.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title":    {"type": "string"},
                "content":  {"type": "string"},
                "tags":     {"type": "array", "items": {"type": "string"}},
                "priority": {"type": "integer", "minimum": 1, "maximum": 5},
            },
            "required": ["title", "content"],
        },
    },
]


def validate_tool_args(tool_name: str, raw_args: dict) -> tuple[BaseModel | None, str]:
    """
    Validate and coerce tool arguments.
    Returns (validated_model, error_message).
    """
    schema_class = TOOL_SCHEMAS.get(tool_name)
    if not schema_class:
        return None, f"Unknown tool: {tool_name}"

    try:
        validated = schema_class(**raw_args)
        return validated, ""
    except ValidationError as e:
        errors = [f"{err['loc'][0]}: {err['msg']}" for err in e.errors()]
        return None, f"Argument validation failed: {'; '.join(errors)}"


def execute_tool(tool_name: str, raw_args: dict) -> str:
    validated, error = validate_tool_args(tool_name, raw_args)
    if error:
        return json.dumps({"error": error, "received_args": raw_args})

    if tool_name == "search_documents":
        # validated.query is guaranteed str, validated.max_results is guaranteed int
        return json.dumps({
            "results": [f"Doc {i}: result for '{validated.query}'" for i in range(validated.max_results)],
            "count": validated.max_results,
        })
    elif tool_name == "create_note":
        return json.dumps({
            "id": "note_123",
            "title": validated.title,
            "tags": validated.tags,  # guaranteed list
        })
    return json.dumps({"error": "unhandled tool"})


def run_agent(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]

    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            tools=TOOLS,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            return next(b.text for b in response.content if b.type == "text")

        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                result = execute_tool(block.name, block.input)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})


print(run_agent('Search for "python async" with max_results="5" and create a note titled "Results".'))
```

**Expected Token Savings**: Validation errors caught before execution prevent retry loops (2–5 extra turns saved per bad call).
**Environment**: Pydantic v2. Install: `pip install pydantic`.

---

### Option 2: JSON Schema Coercion with jsonschema + Type Casting

Validate against the tool's own JSON schema and auto-cast common type mismatches.

```python
import json
import anthropic
from jsonschema import validate, ValidationError, Draft7Validator

client = anthropic.Anthropic()

TOOLS = [
    {
        "name": "calculate",
        "description": "Perform arithmetic calculations.",
        "input_schema": {
            "type": "object",
            "properties": {
                "a":         {"type": "number"},
                "b":         {"type": "number"},
                "operation": {"type": "string", "enum": ["add", "subtract", "multiply", "divide"]},
            },
            "required": ["a", "b", "operation"],
        },
    },
]

TYPE_COERCERS = {
    "number":  lambda v: float(v) if isinstance(v, str) else v,
    "integer": lambda v: int(v)   if isinstance(v, str) else v,
    "boolean": lambda v: v.lower() in ("true", "1", "yes") if isinstance(v, str) else v,
    "string":  lambda v: str(v)   if not isinstance(v, str) else v,
    "array":   lambda v: [v]      if not isinstance(v, list) else v,
}


def coerce_args(raw_args: dict, schema: dict) -> tuple[dict, list[str]]:
    """
    Attempt to coerce argument types to match schema.
    Returns (coerced_args, warnings).
    """
    coerced = dict(raw_args)
    warnings = []
    properties = schema.get("properties", {})

    for field, field_schema in properties.items():
        if field not in coerced:
            continue
        expected_type = field_schema.get("type")
        coercer = TYPE_COERCERS.get(expected_type)
        if coercer and not isinstance(coerced[field], type(coerced[field])):
            try:
                original = coerced[field]
                coerced[field] = coercer(coerced[field])
                warnings.append(f"Coerced {field}: {type(original).__name__} → {type(coerced[field]).__name__}")
            except (ValueError, TypeError):
                pass  # leave as-is; let validation catch it

    return coerced, warnings


def validated_execute(tool_name: str, raw_args: dict, tools: list[dict]) -> str:
    tool_def = next((t for t in tools if t["name"] == tool_name), None)
    if not tool_def:
        return json.dumps({"error": f"Unknown tool: {tool_name}"})

    schema = tool_def["input_schema"]

    # Coerce types first
    coerced, warnings = coerce_args(raw_args, schema)
    if warnings:
        print(f"  [Type coercion] {'; '.join(warnings)}")

    # Validate coerced args
    validator = Draft7Validator(schema)
    errors = list(validator.iter_errors(coerced))
    if errors:
        return json.dumps({
            "error": "Validation failed after coercion",
            "details": [e.message for e in errors],
        })

    # Execute
    if tool_name == "calculate":
        a, b = coerced["a"], coerced["b"]
        op = coerced["operation"]
        if op == "add":       result = a + b
        elif op == "subtract": result = a - b
        elif op == "multiply": result = a * b
        elif op == "divide":
            if b == 0:
                return json.dumps({"error": "Division by zero"})
            result = a / b
        return json.dumps({"result": result})

    return json.dumps({"error": "unhandled"})


def run_agent(message: str) -> str:
    messages = [{"role": "user", "content": message}]
    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=TOOLS,
            messages=messages,
        )
        if response.stop_reason == "end_turn":
            return next(b.text for b in response.content if b.type == "text")

        results = []
        for block in response.content:
            if block.type == "tool_use":
                print(f"  Tool call: {block.name}({block.input})")
                result = validated_execute(block.name, block.input, TOOLS)
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})

        messages += [
            {"role": "assistant", "content": response.content},
            {"role": "user", "content": results},
        ]


print(run_agent('Calculate: a="10", b="3", operation="multiply"'))
```

**Expected Token Savings**: Catches string-numeric mismatches before tool execution; prevents 1–3 retry turns per bad call.
**Environment**: `pip install jsonschema`. Works with any tool schema.

---

### Option 3: Tool Call Interceptor with Argument Repair via LLM

When validation fails, use a cheap Haiku call to repair the arguments before rejecting.

```python
import json
import anthropic
from pydantic import BaseModel, Field, ValidationError

client = anthropic.Anthropic()

TOOLS = [{
    "name": "send_email",
    "description": "Send an email to a recipient.",
    "input_schema": {
        "type": "object",
        "properties": {
            "to":      {"type": "string", "format": "email"},
            "subject": {"type": "string"},
            "body":    {"type": "string"},
            "cc":      {"type": "array", "items": {"type": "string"}},
        },
        "required": ["to", "subject", "body"],
    },
}]


class EmailArgs(BaseModel):
    to: str = Field(..., description="Recipient email address")
    subject: str
    body: str
    cc: list[str] = Field(default_factory=list)


def repair_arguments(tool_name: str, bad_args: dict, error: str) -> dict | None:
    """Use Haiku to repair malformed tool arguments."""
    repair_prompt = f"""Tool "{tool_name}" was called with invalid arguments.

Error: {error}

Invalid arguments:
{json.dumps(bad_args, indent=2)}

Return ONLY a valid JSON object with corrected arguments. Fix type mismatches and missing fields.
Do not include any explanation."""

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": repair_prompt}],
    )
    text = response.content[0].text.strip()
    try:
        start = text.find("{")
        end = text.rfind("}") + 1
        return json.loads(text[start:end])
    except (json.JSONDecodeError, ValueError):
        return None


def safe_execute_tool(tool_name: str, raw_args: dict) -> str:
    if tool_name == "send_email":
        try:
            validated = EmailArgs(**raw_args)
        except ValidationError as e:
            error_msg = "; ".join(f"{err['loc'][0]}: {err['msg']}" for err in e.errors())
            print(f"  [Validation error] {error_msg}")
            print(f"  [Attempting argument repair...]")

            repaired = repair_arguments(tool_name, raw_args, error_msg)
            if repaired:
                try:
                    validated = EmailArgs(**repaired)
                    print(f"  [Repair successful] Using: {repaired}")
                except ValidationError:
                    return json.dumps({"error": f"Could not repair arguments: {error_msg}"})
            else:
                return json.dumps({"error": f"Validation failed and repair failed: {error_msg}"})

        # Simulate sending
        return json.dumps({
            "status": "sent",
            "to": validated.to,
            "subject": validated.subject,
            "cc_count": len(validated.cc),
        })

    return json.dumps({"error": f"Unknown tool: {tool_name}"})


def run_agent(message: str) -> str:
    messages = [{"role": "user", "content": message}]
    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=TOOLS,
            messages=messages,
        )
        if response.stop_reason == "end_turn":
            return next(b.text for b in response.content if b.type == "text")

        results = []
        for block in response.content:
            if block.type == "tool_use":
                result = safe_execute_tool(block.name, block.input)
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})

        messages += [
            {"role": "assistant", "content": response.content},
            {"role": "user", "content": results},
        ]


# Test with intentionally malformed call
print(run_agent('Email alice@example.com about "meeting tomorrow" — say "See you at 3pm". CC: "bob@example.com"'))
```

**Expected Token Savings**: Self-repairing saves 2–4 retry turns at ~200 tokens each. Repair call costs ~100 Haiku tokens.
**Environment**: Pydantic + Haiku repair. Adds ~50ms latency on repair path.

---

### Option 4: Strict Tool Schema Enforcement with examples in Description

Use rich descriptions and examples in the schema to guide the LLM toward correct argument types.

```python
import json
import anthropic

client = anthropic.Anthropic()

# Richly documented tools reduce type errors at the source
TOOLS_WITH_EXAMPLES = [
    {
        "name": "filter_records",
        "description": (
            "Filter database records by criteria. "
            "IMPORTANT: 'limit' must be an INTEGER (not a string), "
            "'active' must be a BOOLEAN (true/false, not 'true'/'false'), "
            "'ids' must be an ARRAY of integers (e.g., [1, 2, 3], not '1,2,3'). "
            "Example call: {\"status\": \"active\", \"limit\": 25, \"active\": true, \"ids\": [10, 20]}"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["active", "inactive", "pending"],
                    "description": "Filter by status. Must be exactly one of: active, inactive, pending",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 1000,
                    "description": "Max records to return. INTEGER only, e.g. 10 not '10'",
                },
                "active": {
                    "type": "boolean",
                    "description": "Filter active records. BOOLEAN: true or false (not strings)",
                },
                "ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "Specific record IDs. ARRAY of integers: [1, 2, 3]",
                },
            },
            "additionalProperties": False,
        },
    },
]


STRICT_SYSTEM = """You are a data assistant. When calling tools:
- 'limit' is always an INTEGER: use 10, not "10"
- Boolean fields use true/false: never "true" or "false" (strings)
- Array fields need brackets: [1, 2, 3] not "1,2,3"
- Never wrap IDs in objects: use 42 not {"id": 42}
"""


def execute_filter(args: dict) -> str:
    # Strict type checks — these would fail without proper schema guidance
    assert isinstance(args.get("limit", 10), int), f"limit must be int, got {type(args.get('limit'))}"
    assert isinstance(args.get("active", True), bool), f"active must be bool"
    assert isinstance(args.get("ids", []), list), f"ids must be list"

    return json.dumps({
        "records": [{"id": i, "status": args.get("status")} for i in range(args.get("limit", 5))],
        "total": args.get("limit", 5),
    })


def run_agent(message: str) -> str:
    messages = [{"role": "user", "content": message}]
    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=STRICT_SYSTEM,
            tools=TOOLS_WITH_EXAMPLES,
            messages=messages,
        )
        if response.stop_reason == "end_turn":
            return next(b.text for b in response.content if b.type == "text")

        results = []
        for block in response.content:
            if block.type == "tool_use":
                print(f"  Tool args: {block.input}")
                try:
                    result = execute_filter(block.input)
                except AssertionError as e:
                    result = json.dumps({"error": str(e)})
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})

        messages += [
            {"role": "assistant", "content": response.content},
            {"role": "user", "content": results},
        ]


print(run_agent("Filter active records with limit 20 and IDs 1, 5, 10."))
```

**Expected Token Savings**: Prevents type errors before they happen — zero repair calls needed. Rich descriptions cost ~50 extra input tokens but save 2–5× that in retry prevention.
**Environment**: No extra dependencies. Schema-side fix, works with all models.

---

### Option 5: Runtime Type Coercion Registry

A centralized coercion registry that maps (tool_name, field_name) → coercion function.

```python
import json
from typing import Any, Callable
import anthropic

client = anthropic.Anthropic()

# Coercion registry: maps (tool, field) to a coercion function
CoerceFn = Callable[[Any], Any]

COERCE_REGISTRY: dict[tuple[str, str], CoerceFn] = {
    ("book_appointment", "date"):         lambda v: v if isinstance(v, str) else str(v),
    ("book_appointment", "duration_mins"): lambda v: int(float(str(v))),
    ("book_appointment", "notify_sms"):   lambda v: bool(v) if isinstance(v, bool) else str(v).lower() in ("true","1","yes"),
    ("book_appointment", "attendee_ids"): lambda v: [int(x) for x in (v if isinstance(v, list) else str(v).split(","))],
}

TOOLS = [{
    "name": "book_appointment",
    "description": "Book a calendar appointment.",
    "input_schema": {
        "type": "object",
        "properties": {
            "date":          {"type": "string", "description": "ISO date: 2025-04-15"},
            "duration_mins": {"type": "integer"},
            "notify_sms":    {"type": "boolean"},
            "attendee_ids":  {"type": "array", "items": {"type": "integer"}},
        },
        "required": ["date", "duration_mins"],
    },
}]


def coerce_tool_args(tool_name: str, raw_args: dict) -> tuple[dict, list[str]]:
    coerced = {}
    log = []
    for field, value in raw_args.items():
        fn = COERCE_REGISTRY.get((tool_name, field))
        if fn:
            try:
                new_value = fn(value)
                if new_value != value:
                    log.append(f"{field}: {repr(value)} → {repr(new_value)}")
                coerced[field] = new_value
            except Exception as e:
                log.append(f"{field}: coercion failed ({e}), keeping original")
                coerced[field] = value
        else:
            coerced[field] = value
    return coerced, log


def execute_tool(name: str, raw_args: dict) -> str:
    coerced, log = coerce_tool_args(name, raw_args)
    if log:
        print(f"  [Coerced] {'; '.join(log)}")

    if name == "book_appointment":
        mins = coerced["duration_mins"]
        if not isinstance(mins, int):
            return json.dumps({"error": f"duration_mins must be int after coercion, got {type(mins)}"})
        return json.dumps({
            "appointment_id": "apt_42",
            "date": coerced["date"],
            "duration_mins": mins,
            "attendees": coerced.get("attendee_ids", []),
        })
    return json.dumps({"error": "unknown tool"})


def run_agent(message: str) -> str:
    messages = [{"role": "user", "content": message}]
    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=TOOLS,
            messages=messages,
        )
        if response.stop_reason == "end_turn":
            return next(b.text for b in response.content if b.type == "text")

        results = []
        for block in response.content:
            if block.type == "tool_use":
                result = execute_tool(block.name, block.input)
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})

        messages += [
            {"role": "assistant", "content": response.content},
            {"role": "user", "content": results},
        ]


print(run_agent('Book appointment on 2025-06-01 for "90" minutes, notify_sms="true", attendees: "3,7,12"'))
```

**Expected Token Savings**: Registry centralizes coercion logic — no per-tool boilerplate, easy to audit and extend.
**Environment**: No extra dependencies. Registry is maintained in a single dict.

---

### Option 6: Tool Call Audit Log with Type Error Tracking

Log all tool calls with type information. Build a feedback loop that improves prompts based on observed type errors.

```python
import json
import sqlite3
import time
from collections import Counter
import anthropic

client = anthropic.Anthropic()
audit_conn = sqlite3.connect("tool_audit.db")
audit_conn.execute("""
    CREATE TABLE IF NOT EXISTS tool_calls (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        ts         REAL,
        tool_name  TEXT,
        field      TEXT,
        expected   TEXT,
        received   TEXT,
        had_error  INTEGER
    )
""")
audit_conn.commit()

TOOLS = [{
    "name": "resize_image",
    "description": "Resize an image to given dimensions.",
    "input_schema": {
        "type": "object",
        "properties": {
            "width":   {"type": "integer", "minimum": 1},
            "height":  {"type": "integer", "minimum": 1},
            "quality": {"type": "number",  "minimum": 0.0, "maximum": 1.0},
            "format":  {"type": "string",  "enum": ["jpeg", "png", "webp"]},
        },
        "required": ["width", "height"],
    },
}]

EXPECTED_TYPES = {
    "resize_image": {
        "width":   int,
        "height":  int,
        "quality": float,
        "format":  str,
    },
}


def audit_and_validate(tool_name: str, raw_args: dict) -> tuple[dict, bool]:
    expected = EXPECTED_TYPES.get(tool_name, {})
    coerced = {}
    had_error = False
    ts = time.time()

    for field, value in raw_args.items():
        exp_type = expected.get(field)
        received_type = type(value).__name__

        if exp_type and not isinstance(value, exp_type):
            had_error = True
            # Log type mismatch
            audit_conn.execute(
                "INSERT INTO tool_calls (ts, tool_name, field, expected, received, had_error) VALUES (?,?,?,?,?,?)",
                (ts, tool_name, field, exp_type.__name__, received_type, 1),
            )
            audit_conn.commit()

            # Attempt coercion
            try:
                coerced[field] = exp_type(value)
                print(f"  [Type fix] {field}: {received_type}({repr(value)}) → {exp_type.__name__}({repr(coerced[field])})")
            except (TypeError, ValueError):
                coerced[field] = value
                print(f"  [Type error, keeping raw] {field}: {repr(value)}")
        else:
            coerced[field] = value

    return coerced, had_error


def get_type_error_report() -> str:
    rows = audit_conn.execute(
        "SELECT tool_name, field, expected, received, COUNT(*) as count "
        "FROM tool_calls WHERE had_error=1 "
        "GROUP BY tool_name, field, expected, received "
        "ORDER BY count DESC"
    ).fetchall()
    if not rows:
        return "No type errors recorded."
    lines = ["Type Error Report:"]
    for tool, field, exp, rec, count in rows:
        lines.append(f"  {tool}.{field}: expected={exp}, received={rec} ({count}x)")
    return "\n".join(lines)


def execute_tool(name: str, raw_args: dict) -> str:
    coerced, _ = audit_and_validate(name, raw_args)

    if name == "resize_image":
        w = coerced.get("width")
        h = coerced.get("height")
        if not isinstance(w, int) or not isinstance(h, int):
            return json.dumps({"error": "width and height must be integers"})
        return json.dumps({
            "resized": True,
            "dimensions": f"{w}x{h}",
            "format": coerced.get("format", "jpeg"),
        })
    return json.dumps({"error": "unknown tool"})


def run_agent(message: str) -> str:
    messages = [{"role": "user", "content": message}]
    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=TOOLS,
            messages=messages,
        )
        if response.stop_reason == "end_turn":
            return next(b.text for b in response.content if b.type == "text")

        results = []
        for block in response.content:
            if block.type == "tool_use":
                result = execute_tool(block.name, block.input)
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})

        messages += [
            {"role": "assistant", "content": response.content},
            {"role": "user", "content": results},
        ]


run_agent('Resize image to width="800", height="600", quality=0.85, format="jpeg"')
print("\n" + get_type_error_report())
```

**Expected Token Savings**: Audit log identifies recurring type errors, enabling targeted prompt fixes that prevent entire classes of bad calls.
**Environment**: SQLite audit log. Review report weekly to improve tool descriptions.

---

| Option | Approach | Auto-Repair | External Deps | Best For |
|--------|---------|------------|--------------|----------|
| 1 | Pydantic models | Coercion via validators | pydantic | Type-safe tool execution |
| 2 | JSON schema + coerce | Type casting | jsonschema | Schema-first validation |
| 3 | LLM repair on failure | Full LLM repair | pydantic | Graceful degradation |
| 4 | Rich schema descriptions | Prevention at source | None | Reducing errors before they occur |
| 5 | Coercion registry | Per-field functions | None | Centralized, auditable coercion |
| 6 | Audit log + tracking | Auto-cast + log | sqlite3 | Continuous improvement loop |
