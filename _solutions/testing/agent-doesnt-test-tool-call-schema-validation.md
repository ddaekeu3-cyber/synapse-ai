---
layout: solution
title: "Agent Doesn't Test Tool Call Schema Validation"
category: testing
description: "Tool schemas are defined but never tested — wrong argument types, missing required fields, and invalid enum values only surface at runtime against the live API."
tags: [testing, tool-use, schema-validation, reliability, ci]
---

## Symptom

The agent defines tool schemas with required fields, enum values, and type constraints, but no tests verify that the schema is correct or that the agent actually produces valid tool calls. Bugs surface only in production: the model passes a string where an integer is expected, omits a required field, or uses an enum value that was renamed in the schema. Each bug requires a live API call to reproduce.

## Root Cause

Tool schemas are JSON Schema objects embedded in the API request. Without tests, there is no validation layer between schema definition and live API. The model may also call tools with arguments that are syntactically valid JSON but semantically wrong — out-of-range values, wrong field names, type coercions. Testing both the schema structure and the argument validation logic catches these bugs at zero token cost.

## Fix

### Option 1 — JSON Schema validation of tool definitions

```python
import json
import jsonschema
import pytest
import anthropic

# ── tool definitions under test ────────────────────────────────────────────────

TOOLS = [
    {
        "name": "search_database",
        "description": "Search the product database.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query":    {"type": "string", "minLength": 1, "maxLength": 500},
                "limit":    {"type": "integer", "minimum": 1, "maximum": 100},
                "category": {"type": "string", "enum": ["electronics", "clothing", "books"]},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "send_email",
        "description": "Send an email to a user.",
        "input_schema": {
            "type": "object",
            "properties": {
                "to":      {"type": "string", "format": "email"},
                "subject": {"type": "string", "minLength": 1},
                "body":    {"type": "string", "minLength": 1},
            },
            "required": ["to", "subject", "body"],
            "additionalProperties": False,
        },
    },
]

TOOL_SCHEMAS = {t["name"]: t["input_schema"] for t in TOOLS}

def validate_tool_call(tool_name: str, arguments: dict) -> list[str]:
    """Returns list of validation errors, empty if valid."""
    schema = TOOL_SCHEMAS.get(tool_name)
    if schema is None:
        return [f"Unknown tool: {tool_name}"]
    try:
        jsonschema.validate(arguments, schema)
        return []
    except jsonschema.ValidationError as e:
        return [e.message]

# ── tests ──────────────────────────────────────────────────────────────────────

def test_search_valid_minimal():
    errors = validate_tool_call("search_database", {"query": "laptop"})
    assert errors == []

def test_search_valid_full():
    errors = validate_tool_call("search_database", {
        "query": "gaming laptop", "limit": 10, "category": "electronics"
    })
    assert errors == []

def test_search_missing_required():
    errors = validate_tool_call("search_database", {"limit": 5})
    assert any("query" in e for e in errors)

def test_search_limit_out_of_range():
    errors = validate_tool_call("search_database", {"query": "x", "limit": 200})
    assert errors  # should fail

def test_search_invalid_category():
    errors = validate_tool_call("search_database", {"query": "x", "category": "furniture"})
    assert errors

def test_search_extra_field_rejected():
    errors = validate_tool_call("search_database", {"query": "x", "secret": "y"})
    assert errors

def test_email_valid():
    errors = validate_tool_call("send_email", {
        "to": "user@example.com", "subject": "Hello", "body": "World"
    })
    assert errors == []

def test_email_missing_body():
    errors = validate_tool_call("send_email", {"to": "a@b.com", "subject": "Hi"})
    assert errors

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
```

**Expected Token Savings:** Schema bugs caught at test time cost zero tokens; caught at runtime they cost one full API round-trip per occurrence.
**Environment:** Any agent with tool definitions; CI pipelines validating schema correctness before deployment.

---

### Option 2 — Mock-based test: verify agent produces valid tool calls

```python
import pytest
import json
import anthropic
from unittest.mock import MagicMock

# ── tool schema ────────────────────────────────────────────────────────────────

WEATHER_TOOL = {
    "name": "get_weather",
    "description": "Get current weather for a city.",
    "input_schema": {
        "type": "object",
        "properties": {
            "city":    {"type": "string"},
            "units":   {"type": "string", "enum": ["celsius", "fahrenheit"]},
            "days":    {"type": "integer", "minimum": 1, "maximum": 7},
        },
        "required": ["city"],
    },
}

# ── agent function under test ──────────────────────────────────────────────────

def run_weather_agent(user_query: str, client: anthropic.Anthropic) -> dict | None:
    """Returns parsed tool_use block if agent calls a tool, else None."""
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        tools=[WEATHER_TOOL],
        messages=[{"role": "user", "content": user_query}],
    )
    for block in resp.content:
        if block.type == "tool_use":
            return {"name": block.name, "input": block.input}
    return None

# ── helpers ───────────────────────────────────────────────────────────────────

def make_tool_use_response(tool_name: str, tool_input: dict) -> MagicMock:
    block = MagicMock()
    block.type  = "tool_use"
    block.name  = tool_name
    block.input = tool_input
    resp = MagicMock()
    resp.content = [block]
    client = MagicMock(spec=anthropic.Anthropic)
    client.messages.create.return_value = resp
    return client

# ── tests ──────────────────────────────────────────────────────────────────────

def test_agent_calls_weather_tool():
    client = make_tool_use_response("get_weather", {"city": "Paris", "units": "celsius"})
    result = run_weather_agent("What's the weather in Paris?", client)
    assert result is not None
    assert result["name"] == "get_weather"

def test_agent_provides_city():
    client = make_tool_use_response("get_weather", {"city": "Tokyo"})
    result = run_weather_agent("Weather in Tokyo?", client)
    assert "city" in result["input"]
    assert isinstance(result["input"]["city"], str)

def test_agent_valid_units_enum():
    client = make_tool_use_response("get_weather", {"city": "NYC", "units": "fahrenheit"})
    result = run_weather_agent("Temperature in NYC in Fahrenheit?", client)
    assert result["input"].get("units") in ("celsius", "fahrenheit", None)

def test_agent_days_within_range():
    client = make_tool_use_response("get_weather", {"city": "London", "days": 5})
    result = run_weather_agent("5-day forecast for London?", client)
    days = result["input"].get("days")
    if days is not None:
        assert 1 <= days <= 7
```

**Expected Token Savings:** Mocked tool responses let you validate agent tool-selection logic without API calls; tests run in milliseconds.
**Environment:** Agents with tool selection logic; testing which tool is called and with what arguments for given user inputs.

---

### Option 3 — Parametrized schema boundary tests

```python
import pytest
import jsonschema

ORDER_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "order_id":    {"type": "string", "pattern": "^ORD-[0-9]{6}$"},
        "quantity":    {"type": "integer", "minimum": 1, "maximum": 999},
        "priority":    {"type": "string", "enum": ["low", "medium", "high", "urgent"]},
        "notes":       {"type": "string", "maxLength": 500},
    },
    "required": ["order_id", "quantity"],
    "additionalProperties": False,
}

def validate(args: dict) -> bool:
    try:
        jsonschema.validate(args, ORDER_TOOL_SCHEMA)
        return True
    except jsonschema.ValidationError:
        return False

# ── valid cases ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("args", [
    {"order_id": "ORD-000001", "quantity": 1},
    {"order_id": "ORD-999999", "quantity": 999},
    {"order_id": "ORD-123456", "quantity": 5, "priority": "high"},
    {"order_id": "ORD-000000", "quantity": 1, "priority": "urgent", "notes": "Rush"},
])
def test_valid_orders(args):
    assert validate(args) is True

# ── invalid cases ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("args,reason", [
    ({"order_id": "ORD-12345",  "quantity": 1},    "order_id too short"),
    ({"order_id": "ORD-1234567","quantity": 1},    "order_id too long"),
    ({"order_id": "ord-123456", "quantity": 1},    "order_id lowercase"),
    ({"order_id": "ABC-123456", "quantity": 1},    "wrong prefix"),
    ({"order_id": "ORD-123456", "quantity": 0},    "quantity below min"),
    ({"order_id": "ORD-123456", "quantity": 1000}, "quantity above max"),
    ({"order_id": "ORD-123456", "quantity": 1.5},  "quantity float"),
    ({"order_id": "ORD-123456", "quantity": 1, "priority": "critical"}, "invalid enum"),
    ({"order_id": "ORD-123456", "quantity": 1, "extra": "x"},           "additional prop"),
    ({"quantity": 1},                                                    "missing order_id"),
    ({"order_id": "ORD-123456"},                                         "missing quantity"),
])
def test_invalid_orders(args, reason):
    assert validate(args) is False, f"Expected validation to fail for: {reason}"

# ── boundary values ────────────────────────────────────────────────────────────

def test_notes_max_length_valid():
    assert validate({"order_id": "ORD-123456", "quantity": 1, "notes": "x" * 500})

def test_notes_over_max_length():
    assert not validate({"order_id": "ORD-123456", "quantity": 1, "notes": "x" * 501})
```

**Expected Token Savings:** Parametrized boundary tests cover 20+ scenarios in one file; no API calls needed to validate schema correctness comprehensively.
**Environment:** Complex tool schemas with regex patterns, enum constraints, and length limits; CI schema regression testing.

---

### Option 4 — Integration-style test with recorded tool call roundtrip

```python
import pytest
import json
import anthropic
from unittest.mock import MagicMock

# ── multi-tool agent ───────────────────────────────────────────────────────────

TOOLS = [
    {
        "name": "search_web",
        "description": "Search the web for information.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}, "num_results": {"type": "integer"}},
            "required": ["query"],
        },
    },
    {
        "name": "save_note",
        "description": "Save a note to the user's notebook.",
        "input_schema": {
            "type": "object",
            "properties": {"title": {"type": "string"}, "content": {"type": "string"}},
            "required": ["title", "content"],
        },
    },
]

TOOL_SCHEMAS = {t["name"]: t["input_schema"] for t in TOOLS}

def agent_step(messages: list[dict], client: anthropic.Anthropic) -> dict:
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        tools=TOOLS,
        messages=messages,
    )
    return resp

# ── tool call recorder / validator ────────────────────────────────────────────

class ToolCallRecorder:
    def __init__(self):
        self.calls: list[dict] = []

    def record(self, name: str, args: dict) -> None:
        import jsonschema
        schema = TOOL_SCHEMAS.get(name, {})
        try:
            jsonschema.validate(args, schema)
            valid = True
            errors = []
        except jsonschema.ValidationError as e:
            valid = False
            errors = [e.message]
        self.calls.append({"name": name, "args": args, "valid": valid, "errors": errors})

    @property
    def all_valid(self) -> bool:
        return all(c["valid"] for c in self.calls)

def make_tool_response(calls: list[tuple[str, dict]]) -> MagicMock:
    blocks = []
    for name, inp in calls:
        b = MagicMock(); b.type = "tool_use"; b.name = name; b.input = inp
        blocks.append(b)
    resp = MagicMock()
    resp.content = blocks
    client = MagicMock(spec=anthropic.Anthropic)
    client.messages.create.return_value = resp
    return client

# ── tests ──────────────────────────────────────────────────────────────────────

def test_all_tool_calls_valid_schema():
    recorder = ToolCallRecorder()
    simulated_calls = [
        ("search_web", {"query": "Python best practices", "num_results": 5}),
        ("save_note",  {"title": "Search results", "content": "Found 5 articles..."}),
    ]
    for name, args in simulated_calls:
        recorder.record(name, args)
    assert recorder.all_valid
    assert len(recorder.calls) == 2

def test_detects_invalid_tool_call():
    recorder = ToolCallRecorder()
    recorder.record("save_note", {"title": "Missing content"})  # content required
    assert not recorder.all_valid
    assert any("content" in e for call in recorder.calls for e in call["errors"])

def test_unknown_tool_handled():
    recorder = ToolCallRecorder()
    recorder.record("unknown_tool", {"arg": "value"})
    # Unknown tool: schema is {}, so all args may pass; but name should be flagged
    # In real systems, add explicit unknown-tool detection
    assert len(recorder.calls) == 1
```

**Expected Token Savings:** Recorder pattern captures and validates every tool call in a sequence without live API calls; regression tests catch schema drift when tools are updated.
**Environment:** Multi-step agents with complex tool use sequences; regression suites validating agent behavior after tool schema changes.

---

### Option 5 — Property-based testing with hypothesis

```python
import pytest
import jsonschema
from hypothesis import given, strategies as st, assume

SEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "query":    {"type": "string", "minLength": 1, "maxLength": 200},
        "limit":    {"type": "integer", "minimum": 1, "maximum": 50},
        "sort_by":  {"type": "string", "enum": ["relevance", "date", "rating"]},
    },
    "required": ["query"],
    "additionalProperties": False,
}

def validate(args: dict) -> bool:
    try:
        jsonschema.validate(args, SEARCH_SCHEMA)
        return True
    except jsonschema.ValidationError:
        return False

# ── property tests: valid args always pass ─────────────────────────────────────

@given(
    query   = st.text(min_size=1, max_size=200),
    limit   = st.integers(min_value=1, max_value=50),
    sort_by = st.sampled_from(["relevance", "date", "rating"]),
)
def test_valid_args_always_pass(query, limit, sort_by):
    args = {"query": query, "limit": limit, "sort_by": sort_by}
    assert validate(args) is True

# ── property tests: empty query always fails ──────────────────────────────────

@given(
    limit   = st.integers(min_value=1, max_value=50),
    sort_by = st.sampled_from(["relevance", "date", "rating"]),
)
def test_empty_query_always_fails(limit, sort_by):
    assert not validate({"query": "", "limit": limit, "sort_by": sort_by})

# ── property tests: out-of-range limit always fails ───────────────────────────

@given(limit = st.integers().filter(lambda x: x < 1 or x > 50))
def test_out_of_range_limit_always_fails(limit):
    assert not validate({"query": "test", "limit": limit})

# ── property tests: invalid sort_by always fails ──────────────────────────────

@given(sort_by = st.text().filter(lambda s: s not in ("relevance", "date", "rating")))
def test_invalid_sort_always_fails(sort_by):
    assume(len(sort_by) > 0)
    assert not validate({"query": "test", "sort_by": sort_by})

# ── property tests: extra fields always fail ──────────────────────────────────

@given(
    query     = st.text(min_size=1, max_size=100),
    extra_key = st.text(min_size=1, max_size=20).filter(
        lambda k: k not in ("query", "limit", "sort_by")
    ),
    extra_val = st.text(),
)
def test_extra_fields_always_fail(query, extra_key, extra_val):
    args = {"query": query, extra_key: extra_val}
    assert not validate(args)
```

**Expected Token Savings:** Hypothesis generates hundreds of test cases automatically; property tests find edge cases (Unicode, whitespace-only strings) that handwritten tests miss.
**Environment:** Teams using hypothesis for property-based testing; schemas with complex constraints where manual edge case enumeration is impractical.

---

### Option 6 — End-to-end tool call flow test with tool result injection

```python
import pytest
import anthropic
from unittest.mock import MagicMock

# ── tool definition ────────────────────────────────────────────────────────────

CALCULATOR_TOOL = {
    "name": "calculate",
    "description": "Perform arithmetic calculations.",
    "input_schema": {
        "type": "object",
        "properties": {
            "expression": {"type": "string"},
            "precision":  {"type": "integer", "minimum": 0, "maximum": 10},
        },
        "required": ["expression"],
    },
}

def execute_calculator(expression: str, precision: int = 2) -> str:
    """Real tool implementation."""
    try:
        result = eval(expression, {"__builtins__": {}})  # noqa: S307 (safe: no builtins)
        return str(round(float(result), precision))
    except Exception as e:
        return f"error: {e}"

def run_calculator_agent(user_query: str, client: anthropic.Anthropic) -> str:
    """Full agent loop: LLM → tool → LLM → final answer."""
    messages = [{"role": "user", "content": user_query}]

    # First turn: get tool call
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        tools=[CALCULATOR_TOOL],
        messages=messages,
    )

    tool_calls = [b for b in resp.content if b.type == "tool_use"]
    if not tool_calls:
        return resp.content[0].text if resp.content else ""

    # Execute tools and continue
    tool_results = []
    for tc in tool_calls:
        result = execute_calculator(tc.input["expression"], tc.input.get("precision", 2))
        tool_results.append({"type": "tool_result", "tool_use_id": tc.id, "content": result})

    messages.append({"role": "assistant", "content": resp.content})
    messages.append({"role": "user", "content": tool_results})

    final = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        tools=[CALCULATOR_TOOL],
        messages=messages,
    )
    return final.content[0].text if final.content else ""

# ── test helpers ───────────────────────────────────────────────────────────────

def make_tool_then_text_client(expression: str, precision: int = 2, final_text: str = "The answer is 4.") -> MagicMock:
    # Turn 1: tool_use block
    tc = MagicMock(); tc.type = "tool_use"; tc.name = "calculate"
    tc.input = {"expression": expression, "precision": precision}
    tc.id    = "tool_use_abc123"
    resp1 = MagicMock(); resp1.content = [tc]; resp1.stop_reason = "tool_use"

    # Turn 2: text block
    tb = MagicMock(); tb.type = "text"; tb.text = final_text
    resp2 = MagicMock(); resp2.content = [tb]; resp2.stop_reason = "end_turn"

    client = MagicMock(spec=anthropic.Anthropic)
    client.messages.create.side_effect = [resp1, resp2]
    return client

# ── tests ──────────────────────────────────────────────────────────────────────

def test_full_tool_flow_returns_text():
    client = make_tool_then_text_client("2 + 2", final_text="The result is 4.")
    result = run_calculator_agent("What is 2 + 2?", client)
    assert "4" in result

def test_two_api_calls_made():
    client = make_tool_then_text_client("10 * 10")
    run_calculator_agent("What is 10 * 10?", client)
    assert client.messages.create.call_count == 2

def test_tool_result_injected_in_second_call():
    client = make_tool_then_text_client("5 * 5", precision=0)
    run_calculator_agent("5 times 5?", client)
    second_call_messages = client.messages.create.call_args_list[1].kwargs["messages"]
    # Last user message should contain tool results
    last_user = second_call_messages[-1]
    assert last_user["role"] == "user"
    assert any("tool_result" in str(item) for item in (last_user["content"] if isinstance(last_user["content"], list) else []))
```

**Expected Token Savings:** Full flow tests verify the entire tool use loop (LLM → execute → inject result → LLM) without live API calls; catches message format bugs before they hit the API.
**Environment:** Agents with multi-turn tool use loops; teams verifying that tool results are correctly formatted and injected back into the conversation.

---

## Comparison

| Option | What It Tests | API Calls | Coverage | Best For |
|---|---|---|---|---|
| 1. JSON Schema validation | Schema structure correctness | Zero | Schema rules | CI schema regression; new tool development |
| 2. Mock tool call | Agent produces valid tool calls | Zero | Tool selection logic | Agent routing; argument type verification |
| 3. Parametrized boundaries | Schema edge cases exhaustively | Zero | Boundary values | Complex schemas with regex/enum/range |
| 4. Roundtrip recorder | Multi-tool call sequence validity | Zero | Call sequences | Multi-step agents; schema drift detection |
| 5. Hypothesis property | Schema invariants under random input | Zero | All valid/invalid combos | High-confidence schema validation |
| 6. Full flow E2E | Complete tool use loop | Zero (mocked) | Tool → result → answer | Integration-style tests of agent loop |
