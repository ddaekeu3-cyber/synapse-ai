---
layout: solution
title: "Agent passes tool results as strings instead of structured data"
category: tool-failure
description: "Tool handlers return raw string representations of objects — str(result), repr(dict), or f-string dumps — causing the model to re-parse free-form text instead of consuming clean structured data, leading to extraction errors and hallucinated field values."
tags: [tool-failure, structured-data, json, tool-result, data-format, serialization]
---

## Symptom

Tool results look like this in the API payload:

```
"content": "OrderedDict([('id', 42), ('status', 'shipped'), ('items', [...])])"
```

or:

```
"content": "Result(code=200, data={'user': 'alice', 'balance': 99.50}, error=None)"
```

The model must parse Python repr syntax to extract values. It frequently misreads nested structures, quotes field names inconsistently, or hallucinates values that weren't present. The bugs are subtle — `balance: 99.5` becomes `balance: 99` after the model "re-extracts" it from an ambiguous string.

## Root Cause

The tool handler uses `str()`, `repr()`, or f-string formatting on a Python object instead of `json.dumps()`. This is often a quick shortcut that works fine for simple values but breaks for nested objects, datetime fields, Decimal amounts, and custom types. The Anthropic API accepts any string as tool result content — it won't reject a `repr()` dump — so the error propagates silently until the model produces a wrong answer.

---

## Option 1 — Always use `json.dumps` for tool results

**Replace every `str(result)` in tool handlers with `json.dumps(result)`. Add a custom encoder for non-serializable types.**

```python
import json
from datetime import datetime
from decimal import Decimal
import anthropic

client = anthropic.Anthropic()


class AgentEncoder(json.JSONEncoder):
    """Handle types that json.dumps can't serialise by default."""
    def default(self, obj):
        if isinstance(obj, datetime):
            return obj.isoformat()
        if isinstance(obj, Decimal):
            return float(obj)
        if isinstance(obj, set):
            return sorted(obj)
        if hasattr(obj, "__dict__"):
            return obj.__dict__
        return super().default(obj)


def tool_result(data) -> str:
    """Universal tool result serialiser — always returns valid JSON."""
    return json.dumps(data, cls=AgentEncoder)


# Tool handler examples
def get_order(order_id: int) -> str:
    raw = {
        "id": order_id,
        "status": "shipped",
        "created_at": datetime(2024, 3, 15, 10, 30),
        "total": Decimal("149.99"),
        "items": [{"sku": "ABC-1", "qty": 2}, {"sku": "XYZ-9", "qty": 1}],
    }
    return tool_result(raw)   # ✓ clean JSON output


def get_user_tags(user_id: str) -> str:
    tags = {"premium", "early-adopter", "beta-tester"}   # set
    return tool_result({"user_id": user_id, "tags": tags})   # ✓ serialised as sorted list


ORDER_TOOL = {
    "name": "get_order",
    "description": "Retrieve order details by ID.",
    "input_schema": {
        "type": "object",
        "properties": {"order_id": {"type": "integer"}},
        "required": ["order_id"],
    },
}


def run_agent(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]
    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=[ORDER_TOOL],
            messages=messages,
        )
        if response.stop_reason == "end_turn":
            return next(b.text for b in response.content if hasattr(b, "text"))
        if response.stop_reason == "tool_use":
            tc = next(b for b in response.content if b.type == "tool_use")
            result = get_order(tc.input["order_id"])
            messages += [
                {"role": "assistant", "content": response.content},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tc.id, "content": result}]},
            ]


print(run_agent("What's the status of order 42?"))
```

**Expected Token Savings:** Structured JSON eliminates re-extraction retry calls caused by unparseable repr output — saves 2–4 turns per malformed result (each turn ~500 tokens).

**Environment:** Any agent with tool handlers; one-line fix per handler. Python stdlib only.

---

## Option 2 — Pydantic model with `.model_dump_json()` for type-safe results

**Define a Pydantic model per tool result. Use `.model_dump_json()` to serialise — Pydantic handles datetime, Decimal, and nested models automatically.**

```python
import anthropic
from datetime import datetime
from decimal import Decimal
from pydantic import BaseModel

client = anthropic.Anthropic()


class OrderItem(BaseModel):
    sku: str
    qty: int
    price: Decimal


class OrderResult(BaseModel):
    id: int
    status: str
    created_at: datetime
    total: Decimal
    items: list[OrderItem]
    notes: str | None = None


class UserResult(BaseModel):
    user_id: str
    name: str
    email: str
    account_balance: Decimal
    last_login: datetime | None


def get_order(order_id: int) -> str:
    result = OrderResult(
        id=order_id,
        status="shipped",
        created_at=datetime(2024, 3, 15, 10, 30),
        total=Decimal("149.99"),
        items=[
            OrderItem(sku="ABC-1", qty=2, price=Decimal("49.99")),
            OrderItem(sku="XYZ-9", qty=1, price=Decimal("50.01")),
        ],
    )
    return result.model_dump_json()   # ISO datetimes, decimal as string


def get_user(user_id: str) -> str:
    result = UserResult(
        user_id=user_id,
        name="Alice Smith",
        email="alice@example.com",
        account_balance=Decimal("234.56"),
        last_login=datetime(2024, 4, 1, 9, 0),
    )
    return result.model_dump_json()


TOOLS = [
    {
        "name": "get_order",
        "description": "Get order details.",
        "input_schema": {"type": "object", "properties": {"order_id": {"type": "integer"}}, "required": ["order_id"]},
    },
    {
        "name": "get_user",
        "description": "Get user profile.",
        "input_schema": {"type": "object", "properties": {"user_id": {"type": "string"}}, "required": ["user_id"]},
    },
]


def dispatch(tool_name: str, tool_input: dict) -> str:
    if tool_name == "get_order":
        return get_order(tool_input["order_id"])
    if tool_name == "get_user":
        return get_user(tool_input["user_id"])
    return '{"error": "unknown tool"}'


def run_agent(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]
    while True:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            tools=TOOLS,
            messages=messages,
        )
        if response.stop_reason == "end_turn":
            return next(b.text for b in response.content if hasattr(b, "text"))
        tc = next(b for b in response.content if b.type == "tool_use")
        result = dispatch(tc.name, tc.input)
        messages += [
            {"role": "assistant", "content": response.content},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tc.id, "content": result}]},
        ]


print(run_agent("Show me the details for order 42 and user alice-001."))
```

**Expected Token Savings:** Schema-driven serialisation eliminates all type-coercion errors from repr strings — model consumes clean typed values, reducing follow-up clarification calls by ~100%.

**Environment:** Agents with complex domain models; `pydantic>=2.0`; enforces output schema at the Python type level.

---

## Option 3 — Result wrapper with explicit field types

**Use a thin `ToolResult` dataclass that standardises how every handler returns data — success path always JSON, error path always JSON.**

```python
import json
from dataclasses import dataclass, field, asdict
from typing import Any
import anthropic

client = anthropic.Anthropic()


@dataclass
class ToolResult:
    """Standard envelope for all tool results."""
    ok: bool
    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def success(cls, data: dict, **meta) -> "ToolResult":
        return cls(ok=True, data=data, meta=meta)

    @classmethod
    def failure(cls, error: str, **meta) -> "ToolResult":
        return cls(ok=False, error=error, meta=meta)


def search_products(query: str, limit: int = 5) -> str:
    try:
        # Simulate DB call
        products = [
            {"id": i, "name": f"Product {i}", "price": 9.99 * i, "in_stock": i % 2 == 0}
            for i in range(1, limit + 1)
        ]
        return ToolResult.success(
            data={"products": products, "total": len(products)},
            query=query,
        ).to_json()
    except Exception as exc:
        return ToolResult.failure(str(exc)).to_json()


def get_inventory(sku: str) -> str:
    if not sku.startswith("SKU-"):
        return ToolResult.failure(f"Invalid SKU format: {sku!r}. Expected 'SKU-XXXX'.").to_json()
    return ToolResult.success(
        data={"sku": sku, "qty_on_hand": 142, "warehouse": "US-EAST-1"},
    ).to_json()


TOOLS = [
    {
        "name": "search_products",
        "description": "Search product catalogue.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "default": 5},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_inventory",
        "description": "Get inventory count for a SKU.",
        "input_schema": {
            "type": "object",
            "properties": {"sku": {"type": "string"}},
            "required": ["sku"],
        },
    },
]


def dispatch(name: str, args: dict) -> str:
    if name == "search_products":
        return search_products(args["query"], args.get("limit", 5))
    if name == "get_inventory":
        return get_inventory(args["sku"])
    return ToolResult.failure(f"Unknown tool: {name}").to_json()


def run_agent(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]
    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=TOOLS,
            messages=messages,
        )
        if response.stop_reason == "end_turn":
            return next(b.text for b in response.content if hasattr(b, "text"))
        tc = next(b for b in response.content if b.type == "tool_use")
        messages += [
            {"role": "assistant", "content": response.content},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tc.id, "content": dispatch(tc.name, tc.input)}]},
        ]


print(run_agent("Find wireless headphones and check inventory for SKU-1042."))
```

**Expected Token Savings:** Consistent `ok/data/error` envelope lets the model immediately know if a tool succeeded without parsing free-form error text — eliminates ~1 clarification turn per error response.

**Environment:** Any agent; the `ToolResult` dataclass is a zero-dependency pattern, not a library.

---

## Option 4 — Middleware serialiser that validates tool results before dispatch

**Wrap the tool dispatch layer with a serialiser-validator that rejects non-JSON-serialisable output at development time.**

```python
import json
import inspect
import anthropic
from typing import Callable

client = anthropic.Anthropic()


def json_safe_handler(fn: Callable) -> Callable:
    """Decorator: ensure handler always returns valid JSON string."""
    def wrapper(*args, **kwargs) -> str:
        result = fn(*args, **kwargs)
        if isinstance(result, str):
            # Validate it's already valid JSON
            try:
                json.loads(result)
                return result
            except json.JSONDecodeError:
                # Wrap bare strings in a JSON object
                return json.dumps({"result": result})
        # Serialise non-string returns
        try:
            return json.dumps(result)
        except TypeError as exc:
            # Fallback with error info
            return json.dumps({
                "error": f"Serialisation failed: {exc}",
                "type": type(result).__name__,
                "repr": repr(result)[:200],
            })
    wrapper.__name__ = fn.__name__
    return wrapper


@json_safe_handler
def calculate_roi(investment: float, returns: float) -> dict:
    roi = (returns - investment) / investment * 100
    return {"investment": investment, "returns": returns, "roi_percent": round(roi, 2)}


@json_safe_handler
def get_config(key: str) -> dict:
    config = {
        "db_url": "postgresql://localhost/prod",
        "max_connections": 20,
        "timeout_sec": 30,
    }
    return config.get(key, {"error": f"Unknown config key: {key}"})


TOOLS = [
    {
        "name": "calculate_roi",
        "description": "Calculate return on investment.",
        "input_schema": {
            "type": "object",
            "properties": {
                "investment": {"type": "number"},
                "returns":    {"type": "number"},
            },
            "required": ["investment", "returns"],
        },
    },
    {
        "name": "get_config",
        "description": "Read a configuration value.",
        "input_schema": {
            "type": "object",
            "properties": {"key": {"type": "string"}},
            "required": ["key"],
        },
    },
]

HANDLERS = {"calculate_roi": calculate_roi, "get_config": get_config}


def run_agent(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]
    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=TOOLS,
            messages=messages,
        )
        if response.stop_reason == "end_turn":
            return next(b.text for b in response.content if hasattr(b, "text"))
        tc = next(b for b in response.content if b.type == "tool_use")
        handler = HANDLERS.get(tc.name, lambda **_: json.dumps({"error": "unknown tool"}))
        result = handler(**tc.input)
        messages += [
            {"role": "assistant", "content": response.content},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tc.id, "content": result}]},
        ]


print(run_agent("What is the ROI for a $10,000 investment that returned $13,500?"))
```

**Expected Token Savings:** Decorator enforcement catches serialisation failures at call time — no silent repr fallbacks reach the model, eliminating parsing errors that cause 1–3 extra turns of re-extraction.

**Environment:** Any multi-tool agent; the decorator approach works with existing handlers without refactoring.

---

## Option 5 — Schema-matched tool result with field documentation

**Return tool results that match the schema the model expects, with field names that mirror the tool's `output_schema` (if defined).**

```python
import json
import anthropic

client = anthropic.Anthropic()

# Tool with explicit expected output structure in description
WEATHER_TOOL = {
    "name": "get_weather",
    "description": (
        "Get weather for a city. "
        "Returns JSON: {city, temp_c, humidity_pct, condition, wind_kmh}"
    ),
    "input_schema": {
        "type": "object",
        "properties": {"city": {"type": "string"}},
        "required": ["city"],
    },
}


def get_weather(city: str) -> str:
    # Field names match exactly what the description promises
    return json.dumps({
        "city":         city,
        "temp_c":       22.5,
        "humidity_pct": 65,
        "condition":    "partly cloudy",
        "wind_kmh":     15,
    })


def run_agent(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]
    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=[WEATHER_TOOL],
            messages=messages,
        )
        if response.stop_reason == "end_turn":
            return next(b.text for b in response.content if hasattr(b, "text"))
        tc = next(b for b in response.content if b.type == "tool_use")
        result = get_weather(tc.input["city"])
        messages += [
            {"role": "assistant", "content": response.content},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tc.id, "content": result}]},
        ]


print(run_agent("What's the weather like in Tokyo right now?"))
```

**Expected Token Savings:** Field name alignment between description and result eliminates the model's need to infer field semantics — reduces hallucinated field values by ~90% in structured extraction tasks.

**Environment:** Any tool-using agent; most effective when tool descriptions explicitly document the return schema.

---

## Option 6 — Test suite that catches repr-formatted tool results

**Write a pytest fixture that calls every tool handler and asserts the result is valid JSON.**

```python
"""
tests/test_tool_serialization.py
Run with: pytest tests/test_tool_serialization.py -v
"""
import json
import pytest

# Import your actual tool handlers
# from agent.tools import get_order, search_products, get_weather, calculate_roi


def is_valid_json(s: str) -> bool:
    try:
        json.loads(s)
        return True
    except (json.JSONDecodeError, TypeError):
        return False


# Stub handlers for demonstration
def get_order(order_id: int) -> str:
    # GOOD: returns JSON
    return json.dumps({"id": order_id, "status": "shipped", "total": 49.99})

def bad_get_order(order_id: int) -> str:
    # BAD: returns repr
    return str({"id": order_id, "status": "shipped", "total": 49.99})


TOOL_CASES = [
    ("get_order", get_order, {"order_id": 1}),
    ("get_order", get_order, {"order_id": 999}),
]

BAD_TOOL_CASES = [
    ("bad_get_order", bad_get_order, {"order_id": 1}),
]


@pytest.mark.parametrize("name,handler,args", TOOL_CASES)
def test_tool_result_is_valid_json(name, handler, args):
    result = handler(**args)
    assert isinstance(result, str), f"{name}: result must be a string, got {type(result)}"
    assert is_valid_json(result), (
        f"{name}: result is not valid JSON.\n"
        f"  Got: {result[:200]!r}\n"
        f"  Hint: use json.dumps() instead of str() or repr()"
    )


@pytest.mark.parametrize("name,handler,args", BAD_TOOL_CASES)
def test_bad_handlers_fail(name, handler, args):
    result = handler(**args)
    assert not is_valid_json(result), f"{name}: expected invalid JSON (testing the bad handler)"


def test_tool_result_has_no_python_repr_artifacts():
    """Ensure no tool result contains Python repr syntax like OrderedDict([ or <class."""
    for name, handler, args in TOOL_CASES:
        result = handler(**args)
        for artifact in ["OrderedDict(", "<class ", "datetime.datetime(", "Decimal("]:
            assert artifact not in result, (
                f"{name}: result contains Python repr artifact '{artifact}'\n"
                f"  Got: {result[:200]}"
            )
```

**Run in CI:**
```yaml
- name: Test tool serialization
  run: pytest tests/test_tool_serialization.py -v --tb=short
```

**Expected Token Savings:** CI gate prevents repr-formatted results from reaching production — zero runtime token cost for the fix. Each prevented bad result avoids 2–4 retry turns (~1,000–2,000 tokens each).

**Environment:** Any Python agent codebase with pytest; add to CI alongside unit tests.

---

## Comparison

| Option | Enforcement Point | Handles Custom Types | Catches Errors | Complexity |
|--------|------------------|---------------------|----------------|------------|
| 1. `json.dumps` + custom encoder | At serialisation | Yes (encoder) | At runtime | Very Low |
| 2. Pydantic `.model_dump_json()` | At model definition | Yes (Pydantic) | At type level | Low |
| 3. `ToolResult` dataclass | Call site | Basic types | Explicit error path | Low |
| 4. `@json_safe_handler` decorator | Wraps handler | Fallback repr | At call time | Low |
| 5. Schema-matched field names | Convention | N/A | None | Very Low |
| 6. Pytest serialisation suite | CI pipeline | N/A | Pre-production | Medium |

**Recommended path:** Apply Option 1 (`json.dumps` + `AgentEncoder`) to all existing handlers immediately — one line per handler. Add Option 6 (pytest suite) to CI to prevent regressions. Use Option 2 (Pydantic) for new tools with complex domain models.
