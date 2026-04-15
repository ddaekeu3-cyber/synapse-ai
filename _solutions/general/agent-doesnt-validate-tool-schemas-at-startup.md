---
layout: solution
title: "Agent Doesn't Validate Tool Schemas at Startup"
category: general
description: "Agent sends malformed or incomplete tool schemas to the API without validating them first, causing cryptic 400 errors at runtime that are hard to diagnose — instead of failing fast at deployment time."
tags: [general, tool-use, validation, reliability, startup, schema, debugging]
---

## Symptom

The agent deploys successfully, but the first time a user triggers a tool call, the API returns `{"error": {"type": "invalid_request_error", "message": "tools.2.input_schema: 'required' must be an array"}}`. The error is cryptic, the stack trace points nowhere useful, and the developer spends an hour realising that tool index 2 has `"required": "path"` (a string) instead of `"required": ["path"]` (an array). If schema validation ran at startup, the error would appear immediately with a clear message.

## Root Cause

Tool schemas are Python dicts written by hand. They are easy to get wrong: `required` as a string instead of an array, `properties` missing when `required` is set, `type` values misspelled ("String" instead of "string"), or a `description` field missing on a required parameter. The Anthropic API validates schemas strictly but only at request time. Without upfront validation, every schema error surfaces in production under real user load rather than during development or deployment.

## Fix

### Option 1 — JSON Schema validator at module load time

```python
import jsonschema
import anthropic

# The meta-schema for Anthropic tool input_schema — a subset of JSON Schema draft-07
TOOL_META_SCHEMA = {
    "type": "object",
    "required": ["name", "description", "input_schema"],
    "properties": {
        "name":        {"type": "string", "minLength": 1, "maxLength": 64, "pattern": "^[a-zA-Z_][a-zA-Z0-9_]*$"},
        "description": {"type": "string", "minLength": 1},
        "input_schema": {
            "type": "object",
            "required": ["type", "properties"],
            "properties": {
                "type":       {"type": "string", "enum": ["object"]},
                "properties": {"type": "object"},
                "required":   {"type": "array", "items": {"type": "string"}},
            },
        },
    },
}

def validate_tools(tools: list[dict]) -> None:
    """Validate all tool schemas at startup. Raises ValueError with a clear message on failure."""
    for i, tool in enumerate(tools):
        try:
            jsonschema.validate(tool, TOOL_META_SCHEMA)
        except jsonschema.ValidationError as e:
            raise ValueError(
                f"Tool schema error at index {i} (name={tool.get('name', '<unnamed>')!r}): "
                f"{e.message} at path {list(e.absolute_path)}"
            ) from e
        # Additional semantic checks
        schema    = tool["input_schema"]
        required  = schema.get("required", [])
        props     = schema.get("properties", {})
        missing   = [r for r in required if r not in props]
        if missing:
            raise ValueError(
                f"Tool {tool['name']!r}: required fields {missing} not in properties"
            )
    print(f"[startup] {len(tools)} tool schemas validated OK")

# CORRECT schemas
GOOD_TOOLS = [
    {
        "name":        "web_search",
        "description": "Search the web for current information.",
        "input_schema": {
            "type":       "object",
            "required":   ["query"],
            "properties": {"query": {"type": "string", "description": "Search query"}},
        },
    },
    {
        "name":        "read_file",
        "description": "Read a file from the filesystem.",
        "input_schema": {
            "type":       "object",
            "required":   ["path"],
            "properties": {"path": {"type": "string", "description": "File path"}},
        },
    },
]

# BROKEN schema — required is a string, not array
BAD_TOOLS = [
    {
        "name":        "broken_tool",
        "description": "This tool has a schema error.",
        "input_schema": {
            "type":       "object",
            "required":   "path",           # BUG: should be ["path"]
            "properties": {"path": {"type": "string"}},
        },
    },
]

# Validate at module load — catches errors before any user request
validate_tools(GOOD_TOOLS)

try:
    validate_tools(BAD_TOOLS)
except ValueError as e:
    print(f"[startup] schema error caught: {e}")

# Only after validation passes, create the client and agent
client = anthropic.Anthropic()

def ask(question: str) -> str:
    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        tools=GOOD_TOOLS,
        messages=[{"role": "user", "content": question}],
    )
    return next((b.text for b in r.content if b.type == "text"), r.content[0].type)

print(f"\nA: {ask('Search for Python news.')[:150]}")
```

**Expected Token Savings:** Schema validation at startup costs 0 tokens — it runs before any API call; catching a schema error that would otherwise cause a 400 on every tool call saves N × (full_request_tokens) in failed calls before the bug is noticed.
**Environment:** All agents; tool schema validation is a mandatory startup check that takes <1ms and prevents runtime 400 errors that are hard to diagnose without reading the raw API error response.

---

### Option 2 — Dry-run validation: send schemas to the API with a test message

```python
import anthropic

client = anthropic.Anthropic()

def dry_run_validate_tools(tools: list[dict], model: str = "claude-haiku-4-5-20251001") -> None:
    """
    Send the tool schemas to the API with a minimal test message.
    If the API accepts the schemas, they are valid.
    If not, the API returns a clear error message before any real user traffic.
    """
    try:
        client.messages.create(
            model=model,
            max_tokens=1,
            tools=tools,
            messages=[{"role": "user", "content": "__schema_validation_test__"}],
        )
        print(f"[startup] dry-run validation passed for {len(tools)} tools")
    except anthropic.BadRequestError as e:
        raise ValueError(f"Tool schema rejected by API: {e}") from e
    except anthropic.AuthenticationError:
        print("[startup] skipping dry-run validation (no API key in test environment)")

TOOLS = [
    {
        "name":        "calculate",
        "description": "Evaluate a mathematical expression.",
        "input_schema": {
            "type":       "object",
            "required":   ["expression"],
            "properties": {"expression": {"type": "string", "description": "Math expression"}},
        },
    },
    {
        "name":        "get_time",
        "description": "Get the current date and time.",
        "input_schema": {
            "type":       "object",
            "properties": {},
        },
    },
]

# Run before accepting traffic
dry_run_validate_tools(TOOLS)

def ask(question: str) -> str:
    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        tools=TOOLS,
        messages=[{"role": "user", "content": question}],
    )
    return next((b.text for b in r.content if b.type == "text"), "")

questions = ["What is 144 / 12?", "What time is it?"]
for q in questions:
    print(f"Q: {q}\nA: {ask(q)[:100]}\n")
```

**Expected Token Savings:** Dry-run validation costs 1 API call (~50 tokens) at startup but guarantees the API accepts the schemas; the API's error message on rejection is far clearer than the runtime error a user would see during a real call.
**Environment:** Agents in CI/CD pipelines; dry-run validation is ideal as a deployment gate — if the validation call fails, the deployment is rejected before traffic is shifted.

---

### Option 3 — Schema builder with typed helpers to prevent mistakes

```python
import anthropic

def string_param(description: str, enum: list[str] | None = None) -> dict:
    p: dict = {"type": "string", "description": description}
    if enum:
        p["enum"] = enum
    return p

def integer_param(description: str, minimum: int | None = None, maximum: int | None = None) -> dict:
    p: dict = {"type": "integer", "description": description}
    if minimum is not None:
        p["minimum"] = minimum
    if maximum is not None:
        p["maximum"] = maximum
    return p

def boolean_param(description: str) -> dict:
    return {"type": "boolean", "description": description}

def build_tool(name: str, description: str,
               required_params: dict[str, dict],
               optional_params: dict[str, dict] | None = None) -> dict:
    """
    Construct a tool schema programmatically — impossible to get required/properties out of sync.
    required_params: always included in 'required'
    optional_params: included in 'properties' but not 'required'
    """
    properties = {**required_params, **(optional_params or {})}
    return {
        "name":        name,
        "description": description,
        "input_schema": {
            "type":       "object",
            "required":   list(required_params.keys()),   # always a list
            "properties": properties,
        },
    }

# Schemas built via helpers — structurally correct by construction
TOOLS = [
    build_tool(
        name="web_search",
        description="Search the web for current information.",
        required_params={"query": string_param("The search query to submit.")},
        optional_params={"max_results": integer_param("Max results to return", minimum=1, maximum=20)},
    ),
    build_tool(
        name="send_email",
        description="Send an email to a recipient.",
        required_params={
            "to":      string_param("Recipient email address."),
            "subject": string_param("Email subject line."),
            "body":    string_param("Email body text."),
        },
        optional_params={"cc": string_param("CC recipient email address.")},
    ),
    build_tool(
        name="classify_sentiment",
        description="Classify the sentiment of a text.",
        required_params={"text": string_param("Text to classify.")},
        optional_params={"label": string_param("Expected label.", enum=["positive", "negative", "neutral"])},
    ),
]

import json
print("Built tool schemas:")
for t in TOOLS:
    print(f"  {t['name']}: required={t['input_schema']['required']}")

client = anthropic.Anthropic()
r = client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=128,
    tools=TOOLS,
    messages=[{"role": "user", "content": "Search for Python 3.13 release notes."}],
)
print(f"\nA: {next((b.text for b in r.content if b.type == 'text'), r.content[0].type if r.content else '')[:150]}")
```

**Expected Token Savings:** Schema builder helpers make the `required` field impossible to set incorrectly — it is always derived from the `required_params` dict keys; eliminating the entire class of `required`-as-string errors saves the debugging time those errors would cost.
**Environment:** Teams maintaining large tool libraries; schema builders are most valuable when multiple developers contribute tool definitions and manual reviews don't catch structural errors.

---

### Option 4 — Schema diff validator: detect breaking changes between versions

```python
import json
import anthropic

client = anthropic.Anthropic()

def schema_fingerprint(tools: list[dict]) -> dict[str, dict]:
    """Extract a stable fingerprint of tool schemas for comparison."""
    return {
        t["name"]: {
            "required":   sorted(t["input_schema"].get("required", [])),
            "properties": sorted(t["input_schema"].get("properties", {}).keys()),
            "description_hash": hash(t.get("description", "")),
        }
        for t in tools
    }

def check_schema_compatibility(old_tools: list[dict], new_tools: list[dict]) -> list[str]:
    """
    Detect breaking schema changes:
    - Removed tools
    - New required parameters added (breaks existing callers)
    - Removed parameters (breaks existing callers)
    """
    old_fp = schema_fingerprint(old_tools)
    new_fp = schema_fingerprint(new_tools)
    issues = []

    for name in old_fp:
        if name not in new_fp:
            issues.append(f"BREAKING: tool '{name}' was removed")
            continue
        old_req  = set(old_fp[name]["required"])
        new_req  = set(new_fp[name]["required"])
        added    = new_req - old_req
        removed  = old_req - new_req
        old_props = set(old_fp[name]["properties"])
        new_props = set(new_fp[name]["properties"])
        dropped   = old_props - new_props
        if added:
            issues.append(f"BREAKING: tool '{name}' added required params {added}")
        if removed:
            issues.append(f"WARNING:  tool '{name}' removed required params {removed}")
        if dropped:
            issues.append(f"BREAKING: tool '{name}' removed properties {dropped}")

    for name in new_fp:
        if name not in old_fp:
            issues.append(f"INFO:     tool '{name}' is new")

    return issues

# Simulate v1 and v2 schemas
V1_TOOLS = [
    {"name": "web_search", "description": "Search.", "input_schema": {"type": "object", "required": ["query"], "properties": {"query": {"type": "string"}}}},
    {"name": "read_file",  "description": "Read.",   "input_schema": {"type": "object", "required": ["path"],  "properties": {"path":  {"type": "string"}}}},
]

V2_TOOLS = [
    # web_search gained a new required field — breaking!
    {"name": "web_search", "description": "Search.", "input_schema": {"type": "object", "required": ["query", "api_key"], "properties": {"query": {"type": "string"}, "api_key": {"type": "string"}}}},
    # read_file removed — breaking!
    # send_email added — safe
    {"name": "send_email", "description": "Send.",   "input_schema": {"type": "object", "required": ["to", "body"],       "properties": {"to":    {"type": "string"}, "body":    {"type": "string"}}}},
]

print("Schema compatibility check V1 → V2:")
issues = check_schema_compatibility(V1_TOOLS, V2_TOOLS)
for issue in issues:
    print(f"  {issue}")

if any(issue.startswith("BREAKING") for issue in issues):
    print("\n[deploy] BLOCKED — breaking schema changes detected")
else:
    print("\n[deploy] OK — no breaking changes")
```

**Expected Token Savings:** Schema diff validation in CI catches breaking changes before deployment; a breaking schema change that reaches production can cause 100% tool call failure until rolled back — preventing one such incident saves the cost of an emergency rollback + incident response.
**Environment:** Teams with frequent tool schema updates; diff validation belongs in the CI pipeline and runs as a pre-deploy gate after every schema change.

---

### Option 5 — Test harness: unit-test every tool schema

```python
import pytest
import jsonschema
import anthropic

# ── Tool definitions under test ──────────────────────────────────────────────

TOOLS = [
    {
        "name":        "web_search",
        "description": "Search the web for current information.",
        "input_schema": {
            "type":       "object",
            "required":   ["query"],
            "properties": {
                "query":       {"type": "string",  "description": "Search query"},
                "max_results": {"type": "integer", "description": "Max results", "minimum": 1},
            },
        },
    },
    {
        "name":        "send_email",
        "description": "Send an email.",
        "input_schema": {
            "type":       "object",
            "required":   ["to", "subject", "body"],
            "properties": {
                "to":      {"type": "string", "description": "Recipient"},
                "subject": {"type": "string", "description": "Subject"},
                "body":    {"type": "string", "description": "Body"},
            },
        },
    },
]

TOOL_META_SCHEMA = {
    "type": "object",
    "required": ["name", "description", "input_schema"],
    "properties": {
        "name":        {"type": "string", "pattern": "^[a-zA-Z_][a-zA-Z0-9_]*$"},
        "description": {"type": "string", "minLength": 1},
        "input_schema": {
            "type": "object",
            "required": ["type", "properties"],
            "properties": {
                "type":       {"type": "string", "enum": ["object"]},
                "properties": {"type": "object"},
                "required":   {"type": "array", "items": {"type": "string"}},
            },
        },
    },
}

# ── Tests ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("tool", TOOLS)
def test_tool_name_is_valid_identifier(tool):
    import re
    assert re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", tool["name"]), \
        f"Tool name {tool['name']!r} is not a valid identifier"

@pytest.mark.parametrize("tool", TOOLS)
def test_tool_description_not_empty(tool):
    assert tool.get("description", "").strip(), \
        f"Tool {tool['name']!r} has no description"

@pytest.mark.parametrize("tool", TOOLS)
def test_tool_schema_is_valid_json_schema(tool):
    jsonschema.validate(tool, TOOL_META_SCHEMA)

@pytest.mark.parametrize("tool", TOOLS)
def test_required_fields_exist_in_properties(tool):
    schema   = tool["input_schema"]
    required = schema.get("required", [])
    props    = schema.get("properties", {})
    missing  = [r for r in required if r not in props]
    assert not missing, \
        f"Tool {tool['name']!r}: required fields {missing} not in properties"

@pytest.mark.parametrize("tool", TOOLS)
def test_required_is_list_not_string(tool):
    required = tool["input_schema"].get("required")
    if required is not None:
        assert isinstance(required, list), \
            f"Tool {tool['name']!r}: 'required' must be a list, got {type(required).__name__}"

@pytest.mark.parametrize("tool", TOOLS)
def test_property_types_are_valid(tool):
    valid_types = {"string", "integer", "number", "boolean", "array", "object", "null"}
    for prop_name, prop_def in tool["input_schema"].get("properties", {}).items():
        if "type" in prop_def:
            assert prop_def["type"] in valid_types, \
                f"Tool {tool['name']!r}.{prop_name}: invalid type {prop_def['type']!r}"

# Run with: pytest solution.py -v
# These tests run in <50ms and catch all common schema mistakes.

if __name__ == "__main__":
    # Quick standalone validation outside pytest
    for tool in TOOLS:
        jsonschema.validate(tool, TOOL_META_SCHEMA)
        print(f"  ✓ {tool['name']}")
    print("All schemas valid.")
```

**Expected Token Savings:** Unit tests run in CI in <50ms with 0 API calls; they catch every common schema mistake (wrong type for `required`, missing description, invalid property types) before a single token is spent on a broken deployment.
**Environment:** Teams using pytest in CI; parameterised tool schema tests are easy to add to an existing test suite and provide a permanent safety net against schema regressions.

---

### Option 6 — Runtime schema error handler with automatic fix suggestions

```python
import re
import anthropic

client = anthropic.Anthropic()

SCHEMA_ERROR_PATTERNS = [
    (re.compile(r"'required' must be an array"),
     "Set 'required' to a list: [\"field\"] instead of \"field\""),
    (re.compile(r"'type' must be one of"),
     "Use lowercase type names: 'string', 'integer', 'boolean', 'object', 'array'"),
    (re.compile(r"'properties' is required"),
     "Add 'properties': {} to the input_schema even if empty"),
    (re.compile(r"'input_schema' is required"),
     "Add 'input_schema': {\"type\": \"object\", \"properties\": {}} to the tool"),
    (re.compile(r"tool name.*invalid"),
     "Tool names must match [a-zA-Z_][a-zA-Z0-9_]* — no spaces or special chars"),
]

def diagnose_schema_error(error_message: str) -> str:
    for pattern, suggestion in SCHEMA_ERROR_PATTERNS:
        if pattern.search(error_message):
            return suggestion
    return "Check the Anthropic tool schema documentation for the correct format."

def safe_create(tools: list[dict], question: str, **kwargs) -> str:
    """Wrapper around messages.create that diagnoses tool schema errors."""
    try:
        r = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=128,
            tools=tools,
            messages=[{"role": "user", "content": question}],
            **kwargs,
        )
        return next((b.text for b in r.content if b.type == "text"), "")
    except anthropic.BadRequestError as e:
        diagnosis = diagnose_schema_error(str(e))
        raise ValueError(
            f"Tool schema error: {e}\n"
            f"Suggestion: {diagnosis}\n"
            f"Tool schemas sent:\n" +
            "\n".join(f"  {t.get('name')}: required={t.get('input_schema', {}).get('required')}" for t in tools)
        ) from e

# Test with good schemas
GOOD = [{"name": "calculate", "description": "Do math.", "input_schema": {"type": "object", "required": ["expression"], "properties": {"expression": {"type": "string"}}}}]
print(f"Good schema: {safe_create(GOOD, 'What is 6 * 7?')[:80]}")

# Test with bad schema — get a helpful error
BAD = [{"name": "broken", "description": "Bad.", "input_schema": {"type": "object", "required": "field", "properties": {"field": {"type": "string"}}}}]
try:
    safe_create(BAD, "Test.")
except ValueError as e:
    print(f"\nCaught schema error with diagnosis:\n{str(e)[:400]}")
```

**Expected Token Savings:** Runtime error handler adds zero tokens to successful calls; it converts cryptic API error messages into actionable fix suggestions — reducing schema debugging time from ~30 minutes (reading API docs) to <5 minutes (reading the suggestion).
**Environment:** Development environments and staging; the error handler is most useful during the schema-authoring phase and can be replaced by the static validators (Options 1, 5) once schemas are stable.

---

## Comparison

| Option | When It Runs | API Calls | Catches All Errors | Best For |
|---|---|---|---|---|
| 1. JSON Schema validator | Module import | 0 | Most structural errors | All agents — baseline fix |
| 2. API dry-run | Startup | 1 (cheap) | All API-rejected errors | CI/CD deployment gates |
| 3. Schema builder helpers | N/A (by construction) | 0 | required/properties sync | Teams with multiple schema authors |
| 4. Diff validator | CI pipeline | 0 | Breaking changes | Frequent schema updates |
| 5. Pytest unit tests | CI pipeline | 0 | All structural + semantic | Teams with existing test suites |
| 6. Runtime error handler | Per-call (on error only) | 0 | Runtime errors only | Development / authoring phase |
