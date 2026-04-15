---
layout: solution
title: "Agent Doesn't Handle Tool Call Schema Validation Errors"
category: general
description: "Agent crashes or enters an error loop when Claude returns a tool_use block with inputs that don't match the expected schema — missing required fields, wrong types, or extra unexpected keys."
tags: [tool-use, validation, error-handling, schema, reliability]
---

## Symptom

The agent crashes with `KeyError`, `TypeError`, or `ValidationError` when executing a tool call. Claude occasionally omits a required field, passes a string where an integer is expected, or nests an object one level deeper than the schema specifies. The error propagates uncaught and the entire task fails. Alternatively, the agent silently executes with wrong data, producing incorrect results.

## Root Cause

The Anthropic API does not guarantee that the model's `tool_use` block inputs strictly match the declared JSON schema. Claude attempts to follow the schema but can deviate — especially for complex schemas, ambiguous user input, or when the model is unsure of a value. Agents that directly unpack `block.input["required_field"]` without validation assume perfect schema compliance, making them brittle to the natural variance in model outputs.

## Fix

### Option 1: Validate tool inputs with Pydantic before executing

```python
import anthropic
from pydantic import BaseModel, Field, ValidationError

client = anthropic.Anthropic()


# Define expected tool inputs as Pydantic models
class SearchInput(BaseModel):
    query: str = Field(..., min_length=1)
    max_results: int = Field(5, ge=1, le=20)
    category: str | None = None


class CreateRecordInput(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    priority: str = Field(..., pattern="^(low|medium|high|critical)$")
    tags: list[str] = Field(default_factory=list)


TOOL_MODELS = {
    "search": SearchInput,
    "create_record": CreateRecordInput,
}

TOOLS = [
    {
        "name": "search",
        "description": "Search for records",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "max_results": {"type": "integer", "default": 5},
                "category": {"type": "string"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "create_record",
        "description": "Create a new record",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "priority": {"type": "string", "enum": ["low", "medium", "high", "critical"]},
                "tags": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["title", "priority"],
        },
    },
]


def execute_tool(name: str, raw_input: dict) -> str:
    model_class = TOOL_MODELS.get(name)
    if not model_class:
        return f"ERROR: Unknown tool '{name}'"

    try:
        validated = model_class(**raw_input)
    except ValidationError as e:
        # Return structured error so Claude can self-correct
        errors = [f"{err['loc'][0]}: {err['msg']}" for err in e.errors()]
        return (
            f"SCHEMA_VALIDATION_ERROR: Tool '{name}' received invalid inputs.\n"
            f"Errors: {'; '.join(errors)}\n"
            f"Please retry with corrected inputs matching the schema."
        )

    # Safe to use validated inputs
    if name == "search":
        return f"Found 3 results for '{validated.query}' (max={validated.max_results})"
    if name == "create_record":
        return f"Created record: title='{validated.title}', priority={validated.priority}, tags={validated.tags}"
    return "Tool executed"


messages = [{"role": "user", "content": "Search for Python tutorials and create a record for the best one"}]

while True:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        tools=TOOLS,
        messages=messages,
    )
    messages.append({"role": "assistant", "content": response.content})

    if response.stop_reason == "end_turn":
        print(next(b.text for b in response.content if b.type == "text"))
        break

    results = []
    for block in response.content:
        if block.type == "tool_use":
            result = execute_tool(block.name, block.input)
            print(f"[{block.name}] → {result[:120]}")
            results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})

    messages.append({"role": "user", "content": results})
```

**Expected Token Savings:** Validation errors returned as tool_result content let Claude self-correct in one turn vs. a crash that requires the user to restart the entire session.
**Environment:** Python 3.10+; requires `pydantic>=2.0`; validation adds <1ms per tool call.

---

### Option 2: Lenient input coercion with fallback defaults

```python
import anthropic
from typing import Any

client = anthropic.Anthropic()


def coerce_field(value: Any, expected_type: str, default: Any = None) -> tuple[Any, str | None]:
    """
    Attempt to coerce a value to the expected type.
    Returns (coerced_value, error_message_or_None).
    """
    if value is None:
        return default, None

    if expected_type == "string":
        return str(value), None

    if expected_type == "integer":
        try:
            return int(value), None
        except (ValueError, TypeError):
            if default is not None:
                return default, f"Could not convert {value!r} to integer, using default {default}"
            return None, f"Expected integer, got {type(value).__name__}: {value!r}"

    if expected_type == "boolean":
        if isinstance(value, bool):
            return value, None
        if isinstance(value, str):
            return value.lower() in ("true", "1", "yes"), None
        return bool(value), None

    if expected_type == "array":
        if isinstance(value, list):
            return value, None
        if isinstance(value, str):
            return [value], None  # Wrap single string in list
        return [value], f"Wrapped non-list value in array"

    return value, None


def safe_execute_tool(name: str, raw_input: dict) -> str:
    warnings = []

    if name == "send_email":
        # Required: recipient (string), subject (string)
        # Optional: cc (array), priority (string, enum)
        recipient, err = coerce_field(raw_input.get("recipient"), "string")
        if err:
            warnings.append(err)

        subject, err = coerce_field(raw_input.get("subject"), "string", default="(no subject)")
        if err:
            warnings.append(err)

        cc, err = coerce_field(raw_input.get("cc"), "array", default=[])
        if err:
            warnings.append(err)

        priority_raw = raw_input.get("priority", "normal")
        valid_priorities = {"low", "normal", "high", "urgent"}
        priority = priority_raw if priority_raw in valid_priorities else "normal"
        if priority != priority_raw:
            warnings.append(f"Invalid priority '{priority_raw}', defaulted to 'normal'")

        if not recipient:
            return "ERROR: 'recipient' is required and could not be coerced"

        result = f"Email sent to {recipient}: '{subject}' (priority={priority}, cc={cc})"
        if warnings:
            result += f"\n[Coercion warnings: {'; '.join(warnings)}]"
        return result

    return f"ERROR: Unknown tool '{name}'"


TOOLS = [
    {
        "name": "send_email",
        "description": "Send an email",
        "input_schema": {
            "type": "object",
            "properties": {
                "recipient": {"type": "string"},
                "subject": {"type": "string"},
                "cc": {"type": "array", "items": {"type": "string"}},
                "priority": {"type": "string", "enum": ["low", "normal", "high", "urgent"]},
            },
            "required": ["recipient", "subject"],
        },
    }
]

messages = [{"role": "user", "content": "Send an urgent email to alice@example.com about the meeting"}]

while True:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=256,
        tools=TOOLS,
        messages=messages,
    )
    messages.append({"role": "assistant", "content": response.content})

    if response.stop_reason == "end_turn":
        print(next(b.text for b in response.content if b.type == "text"))
        break

    results = []
    for block in response.content:
        if block.type == "tool_use":
            result = safe_execute_tool(block.name, block.input)
            print(f"Tool result: {result}")
            results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})

    messages.append({"role": "user", "content": results})
```

**Expected Token Savings:** Coercion prevents needless validation-error retry turns; defaults let the task complete even with imperfect model inputs.
**Environment:** Python 3.10+; coercion is tool-specific — write coerce logic once per tool, reuse indefinitely.

---

### Option 3: JSON schema validator with detailed error feedback

```python
import json
import anthropic

try:
    import jsonschema
    HAS_JSONSCHEMA = True
except ImportError:
    HAS_JSONSCHEMA = False

client = anthropic.Anthropic()

TOOL_SCHEMAS = {
    "analyze_text": {
        "type": "object",
        "properties": {
            "text": {"type": "string", "minLength": 1},
            "language": {"type": "string", "enum": ["en", "fr", "de", "es", "ja"]},
            "tasks": {
                "type": "array",
                "items": {"type": "string", "enum": ["sentiment", "entities", "summary", "keywords"]},
                "minItems": 1,
            },
            "max_tokens": {"type": "integer", "minimum": 10, "maximum": 2000},
        },
        "required": ["text", "tasks"],
        "additionalProperties": False,
    }
}

TOOLS = [
    {
        "name": "analyze_text",
        "description": "Analyze text with specified NLP tasks",
        "input_schema": TOOL_SCHEMAS["analyze_text"],
    }
]


def validate_tool_input(tool_name: str, raw_input: dict) -> tuple[bool, str]:
    """
    Validate tool input against its JSON schema.
    Returns (is_valid, error_message).
    """
    schema = TOOL_SCHEMAS.get(tool_name)
    if not schema:
        return True, ""  # No schema registered — allow through

    if HAS_JSONSCHEMA:
        try:
            jsonschema.validate(raw_input, schema)
            return True, ""
        except jsonschema.ValidationError as e:
            # Provide actionable error message
            path = " → ".join(str(p) for p in e.absolute_path) or "root"
            return False, (
                f"Validation failed at '{path}': {e.message}\n"
                f"Schema expects: {json.dumps(e.schema, indent=2)[:200]}\n"
                f"Got: {json.dumps(e.instance)[:100]}\n"
                f"Please correct and retry."
            )
    else:
        # Minimal fallback without jsonschema library
        required = schema.get("required", [])
        missing = [f for f in required if f not in raw_input]
        if missing:
            return False, f"Missing required fields: {', '.join(missing)}"
        return True, ""


def run_analyze_text(inputs: dict) -> str:
    """Execute the tool after validation passes."""
    return (
        f"Analysis complete: text={inputs['text'][:30]}..., "
        f"tasks={inputs['tasks']}, language={inputs.get('language', 'en')}"
    )


messages = [{"role": "user", "content": "Analyze this text for sentiment and keywords: 'Python is a great language for AI development'"}]

while True:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        tools=TOOLS,
        messages=messages,
    )
    messages.append({"role": "assistant", "content": response.content})

    if response.stop_reason == "end_turn":
        print(next(b.text for b in response.content if b.type == "text"))
        break

    results = []
    for block in response.content:
        if block.type == "tool_use":
            is_valid, error = validate_tool_input(block.name, block.input)
            if is_valid:
                result = run_analyze_text(block.input)
            else:
                result = f"SCHEMA_ERROR: {error}"
            print(f"[{block.name}] valid={is_valid} → {result[:100]}")
            results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})

    messages.append({"role": "user", "content": results})
```

**Expected Token Savings:** Detailed validation errors enable single-turn self-correction by Claude vs. opaque crashes requiring session restart.
**Environment:** Python 3.9+; `jsonschema` is optional but recommended (`pip install jsonschema`); fallback validation handles the common case.

---

### Option 4: Tool execution sandbox with exception capture

```python
import traceback
import anthropic
from typing import Callable, Any

client = anthropic.Anthropic()


def sandboxed_tool_call(
    tool_name: str,
    tool_input: dict,
    handler: Callable[[dict], str],
) -> str:
    """
    Execute a tool handler in a sandbox that captures all exceptions.
    Returns a structured error string instead of raising, so the agent loop
    can continue and Claude can self-correct.
    """
    try:
        result = handler(tool_input)
        return str(result)
    except KeyError as e:
        return (
            f"TOOL_ERROR [KeyError]: Missing field {e} in tool inputs.\n"
            f"Received inputs: {list(tool_input.keys())}\n"
            f"Retry with the missing field included."
        )
    except TypeError as e:
        return (
            f"TOOL_ERROR [TypeError]: Wrong type for a field — {e}.\n"
            f"Received: {tool_input}\n"
            f"Check that numeric fields are numbers, not strings."
        )
    except ValueError as e:
        return (
            f"TOOL_ERROR [ValueError]: Invalid value — {e}.\n"
            f"Check that enum values match exactly."
        )
    except Exception as e:
        # Capture full traceback for debugging, but don't expose to Claude
        tb = traceback.format_exc()
        print(f"[INTERNAL ERROR in {tool_name}]:\n{tb}")
        return (
            f"TOOL_ERROR [Internal]: The tool '{tool_name}' encountered an unexpected error.\n"
            f"Error type: {type(e).__name__}\n"
            f"This may be a bug in the tool implementation. "
            f"Try rephrasing your request or contact support."
        )


# Tool handlers — deliberately imperfect to test sandbox
def handle_calculate(inputs: dict) -> str:
    a = inputs["a"]       # May raise KeyError
    b = inputs["b"]       # May raise KeyError
    op = inputs["operation"]
    if op == "divide" and b == 0:
        raise ValueError("Cannot divide by zero")
    ops = {"add": a + b, "subtract": a - b, "multiply": a * b, "divide": a / b}
    return str(ops[op])


def handle_format_data(inputs: dict) -> str:
    records = inputs["records"]       # Expected: list
    field = inputs["display_field"]   # Expected: string
    return "\n".join(str(r[field]) for r in records)   # May raise KeyError on record


TOOLS = [
    {
        "name": "calculate",
        "description": "Perform arithmetic: a op b",
        "input_schema": {
            "type": "object",
            "properties": {
                "a": {"type": "number"},
                "b": {"type": "number"},
                "operation": {"type": "string", "enum": ["add", "subtract", "multiply", "divide"]},
            },
            "required": ["a", "b", "operation"],
        },
    },
    {
        "name": "format_data",
        "description": "Format records by extracting a field",
        "input_schema": {
            "type": "object",
            "properties": {
                "records": {"type": "array"},
                "display_field": {"type": "string"},
            },
            "required": ["records", "display_field"],
        },
    },
]

HANDLERS = {"calculate": handle_calculate, "format_data": handle_format_data}

messages = [{"role": "user", "content": "Calculate 100 divided by 4, then multiply the result by 3"}]

while True:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        tools=TOOLS,
        messages=messages,
    )
    messages.append({"role": "assistant", "content": response.content})

    if response.stop_reason == "end_turn":
        print(next(b.text for b in response.content if b.type == "text"))
        break

    results = []
    for block in response.content:
        if block.type == "tool_use":
            handler = HANDLERS.get(block.name, lambda _: f"Unknown tool: {block.name}")
            result = sandboxed_tool_call(block.name, block.input, handler)
            print(f"[{block.name}] → {result[:100]}")
            results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})

    messages.append({"role": "user", "content": results})
```

**Expected Token Savings:** Sandbox captures all exceptions as tool_result errors — no crashes, no session restarts; each error costs one turn to recover vs. full task restart.
**Environment:** Python 3.9+; no dependencies; handler pattern separates validation from business logic.

---

### Option 5: Retry with corrected schema hint injected into tool_result

```python
import anthropic
from pydantic import BaseModel, Field, ValidationError

client = anthropic.Anthropic()


class QueryDBInput(BaseModel):
    table: str = Field(..., description="Table name")
    filters: dict[str, str] = Field(default_factory=dict)
    limit: int = Field(10, ge=1, le=1000)
    order_by: str | None = None
    descending: bool = False


TOOLS = [
    {
        "name": "query_db",
        "description": "Query a database table",
        "input_schema": {
            "type": "object",
            "properties": {
                "table": {"type": "string"},
                "filters": {"type": "object", "additionalProperties": {"type": "string"}},
                "limit": {"type": "integer", "default": 10},
                "order_by": {"type": "string"},
                "descending": {"type": "boolean", "default": False},
            },
            "required": ["table"],
        },
    }
]


def build_correction_hint(validation_error: ValidationError, original_input: dict) -> str:
    """
    Build a targeted correction message from Pydantic validation errors.
    Includes the original input and specific field corrections needed.
    """
    lines = ["VALIDATION_FAILED — please correct these fields and retry:\n"]

    for err in validation_error.errors():
        field = ".".join(str(loc) for loc in err["loc"])
        msg = err["msg"]
        got = err.get("input")
        lines.append(f"  Field '{field}': {msg} (received: {got!r})")

    lines.append(f"\nOriginal input: {original_input}")
    lines.append("\nExpected schema:")
    lines.append("  table: string (required)")
    lines.append("  filters: object with string values (optional)")
    lines.append("  limit: integer 1–1000 (default: 10)")
    lines.append("  order_by: string column name (optional)")
    lines.append("  descending: boolean (default: false)")

    return "\n".join(lines)


def execute_query_db(raw_input: dict) -> str:
    try:
        validated = QueryDBInput(**raw_input)
    except ValidationError as e:
        return build_correction_hint(e, raw_input)

    # Safe execution
    filter_str = " AND ".join(f"{k}='{v}'" for k, v in validated.filters.items())
    order_str = f" ORDER BY {validated.order_by} {'DESC' if validated.descending else 'ASC'}" if validated.order_by else ""
    sql = f"SELECT * FROM {validated.table}" + (f" WHERE {filter_str}" if filter_str else "") + order_str + f" LIMIT {validated.limit}"
    return f"Query executed: {sql}\nReturned: 5 rows (simulated)"


messages = [{"role": "user", "content": "Query the orders table for status='pending', limit 25, newest first"}]

while True:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        tools=TOOLS,
        messages=messages,
    )
    messages.append({"role": "assistant", "content": response.content})

    if response.stop_reason == "end_turn":
        print(next(b.text for b in response.content if b.type == "text"))
        break

    results = []
    for block in response.content:
        if block.type == "tool_use":
            result = execute_query_db(block.input)
            print(f"[query_db] → {result[:150]}")
            results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})

    messages.append({"role": "user", "content": results})
```

**Expected Token Savings:** Targeted correction hints enable single-turn recovery from schema errors; generic error messages often require 2–3 additional turns.
**Environment:** Python 3.10+; Pydantic validation errors map directly to schema fields; hint generation is instant.

---

### Option 6: Schema validation middleware applied to all tools

```python
import json
import anthropic
from typing import Callable

client = anthropic.Anthropic()


class ToolRegistry:
    """
    Central registry that applies schema validation middleware to all tools.
    Handlers only run when inputs pass validation.
    """

    def __init__(self):
        self._tools: list[dict] = []
        self._handlers: dict[str, Callable] = {}
        self._schemas: dict[str, dict] = {}

    def register(
        self,
        name: str,
        description: str,
        input_schema: dict,
        handler: Callable[[dict], str],
    ) -> None:
        self._tools.append({
            "name": name,
            "description": description,
            "input_schema": input_schema,
        })
        self._handlers[name] = handler
        self._schemas[name] = input_schema

    @property
    def tools(self) -> list[dict]:
        return self._tools

    def _validate_basic(self, schema: dict, inputs: dict) -> list[str]:
        """Basic schema validation without external libraries."""
        errors = []
        required = schema.get("required", [])
        properties = schema.get("properties", {})

        for field in required:
            if field not in inputs or inputs[field] is None:
                errors.append(f"Missing required field: '{field}'")

        for field, value in inputs.items():
            if field not in properties:
                continue  # Allow extra fields unless additionalProperties: false
            prop = properties[field]
            expected_type = prop.get("type")
            enum_values = prop.get("enum")

            if expected_type == "integer" and not isinstance(value, int):
                errors.append(f"Field '{field}': expected integer, got {type(value).__name__}")
            elif expected_type == "string" and not isinstance(value, str):
                errors.append(f"Field '{field}': expected string, got {type(value).__name__}")
            elif expected_type == "boolean" and not isinstance(value, bool):
                errors.append(f"Field '{field}': expected boolean, got {type(value).__name__}")
            elif expected_type == "array" and not isinstance(value, list):
                errors.append(f"Field '{field}': expected array, got {type(value).__name__}")

            if enum_values and value not in enum_values:
                errors.append(f"Field '{field}': '{value}' not in allowed values: {enum_values}")

        return errors

    def execute(self, tool_name: str, raw_input: dict) -> str:
        handler = self._handlers.get(tool_name)
        if not handler:
            return f"ERROR: No handler registered for tool '{tool_name}'"

        schema = self._schemas.get(tool_name, {})
        errors = self._validate_basic(schema, raw_input)

        if errors:
            return (
                f"SCHEMA_VALIDATION_FAILED for '{tool_name}':\n"
                + "\n".join(f"  • {e}" for e in errors)
                + f"\n\nReceived: {json.dumps(raw_input, indent=2)}"
                + "\nPlease retry with corrected inputs."
            )

        try:
            return handler(raw_input)
        except Exception as e:
            return f"EXECUTION_ERROR in '{tool_name}': {type(e).__name__}: {e}"


# Build registry
registry = ToolRegistry()

registry.register(
    name="resize_image",
    description="Resize an image to specified dimensions",
    input_schema={
        "type": "object",
        "properties": {
            "image_path": {"type": "string"},
            "width": {"type": "integer"},
            "height": {"type": "integer"},
            "format": {"type": "string", "enum": ["jpeg", "png", "webp"]},
        },
        "required": ["image_path", "width", "height"],
    },
    handler=lambda inp: f"Resized {inp['image_path']} to {inp['width']}x{inp['height']} ({inp.get('format', 'jpeg')})",
)

registry.register(
    name="send_slack",
    description="Send a Slack message",
    input_schema={
        "type": "object",
        "properties": {
            "channel": {"type": "string"},
            "message": {"type": "string"},
            "urgent": {"type": "boolean"},
        },
        "required": ["channel", "message"],
    },
    handler=lambda inp: f"Slack sent to #{inp['channel']}: '{inp['message'][:50]}' (urgent={inp.get('urgent', False)})",
)

messages = [{"role": "user", "content": "Resize the image at /tmp/photo.jpg to 800x600 as PNG, then notify #design on Slack"}]

while True:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        tools=registry.tools,
        messages=messages,
    )
    messages.append({"role": "assistant", "content": response.content})

    if response.stop_reason == "end_turn":
        print(next(b.text for b in response.content if b.type == "text"))
        break

    results = []
    for block in response.content:
        if block.type == "tool_use":
            result = registry.execute(block.name, block.input)
            print(f"[{block.name}] → {result[:120]}")
            results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})

    messages.append({"role": "user", "content": results})
```

**Expected Token Savings:** Centralized middleware catches all schema violations consistently; handlers never receive invalid inputs; eliminates entire class of runtime crashes.
**Environment:** Python 3.10+; zero dependencies; registry scales to 50+ tools without additional code.

---

| Option | Approach | Validation Depth | Best For |
|--------|----------|-----------------|----------|
| 1 | Pydantic model validation | Full type + constraint | Typed tool libraries |
| 2 | Lenient coercion with defaults | Best-effort correction | Forgiving agents |
| 3 | JSON schema validator | Schema-spec compliant | Complex nested schemas |
| 4 | Exception sandbox | Runtime error capture | Legacy handlers |
| 5 | Pydantic + correction hints | Error + fix guidance | Self-correcting agents |
| 6 | Registry middleware | All-tools coverage | Large tool libraries |
