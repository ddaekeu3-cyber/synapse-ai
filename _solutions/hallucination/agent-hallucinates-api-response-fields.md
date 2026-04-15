---
layout: solution
title: "Agent hallucinates API response fields"
category: hallucination
description: "Agent assumes API responses contain fields that don't exist, inventing values for keys never returned by the actual API."
tags: [hallucination, tool-failure, validation, schema, api]
---

## Symptom

The agent reads a tool result and then references fields that were never present in the response. It confidently extracts `response["user_id"]` from a payload that only contains `response["id"]`, or uses `data["created_at"]` when the API returns `data["timestamp"]`. Downstream steps silently receive `None` or raise `KeyError`, and the agent either fabricates a replacement value or crashes.

```
Tool result: {"id": 42, "status": "active", "timestamp": 1712000000}
Agent reads:  user_id=None, created_at=None   ← hallucinated field names
Agent output: "User 42 was created at None"   ← fabricated narrative
```

## Root Cause

The model was trained on many APIs with similar-sounding field names (`user_id` vs `id`, `created_at` vs `timestamp`, `name` vs `display_name`). When the actual response structure is ambiguous or the model has not seen the exact schema before, it guesses field names from prior training knowledge instead of reading the actual keys returned in the tool result.

## Fix

Validate every tool result against its expected schema before the agent processes it. Surface mismatches as explicit errors so the model is forced to adapt rather than hallucinate.

---

### Option 1 — Schema validation with Pydantic before passing to agent

```python
import anthropic
import json
from pydantic import BaseModel, ValidationError

client = anthropic.Anthropic()

# --- Define expected response schemas ---
class UserResponse(BaseModel):
    id: int
    status: str
    timestamp: int
    email: str | None = None

class OrderResponse(BaseModel):
    order_id: str
    total_cents: int
    currency: str

SCHEMA_MAP: dict[str, type[BaseModel]] = {
    "get_user": UserResponse,
    "get_order": OrderResponse,
}

def validate_tool_result(tool_name: str, raw: dict) -> tuple[bool, str]:
    """Returns (is_valid, normalized_json_or_error_message)."""
    schema = SCHEMA_MAP.get(tool_name)
    if schema is None:
        return True, json.dumps(raw)  # no schema registered — pass through
    try:
        parsed = schema(**raw)
        return True, parsed.model_dump_json()
    except ValidationError as e:
        error_summary = "; ".join(
            f"field '{err['loc'][0]}': {err['msg']}" for err in e.errors()
        )
        return False, (
            f"[SCHEMA ERROR] Tool '{tool_name}' returned unexpected structure. "
            f"Errors: {error_summary}. "
            f"Actual keys returned: {list(raw.keys())}. "
            "Use only the keys listed above — do NOT invent missing fields."
        )

def fake_api_call(tool_name: str, inputs: dict) -> dict:
    """Simulate real API returning its actual field names."""
    if tool_name == "get_user":
        return {"id": 99, "status": "active", "timestamp": 1712000000}
    if tool_name == "get_order":
        return {"order_id": "ORD-001", "total_cents": 4999, "currency": "USD"}
    return {}

TOOLS = [
    {
        "name": "get_user",
        "description": "Fetch user by ID.",
        "input_schema": {
            "type": "object",
            "properties": {"user_id": {"type": "integer"}},
            "required": ["user_id"],
        },
    },
    {
        "name": "get_order",
        "description": "Fetch an order by ID.",
        "input_schema": {
            "type": "object",
            "properties": {"order_id": {"type": "string"}},
            "required": ["order_id"],
        },
    },
]

def run_agent(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]

    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            tools=TOOLS,
            messages=messages,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            break

        tool_results = []
        for b in response.content:
            if b.type != "tool_use":
                continue
            raw = fake_api_call(b.name, b.input)
            is_valid, content = validate_tool_result(b.name, raw)
            print(f"[VALID={is_valid}] {b.name} → {content[:80]}")
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": b.id,
                "content": content,
                "is_error": not is_valid,
            })

        messages.append({"role": "user", "content": tool_results})

    return next(b.text for b in response.content if hasattr(b, "text"))

print(run_agent("Get user 99 and order ORD-001, then summarize their details."))
```

**Expected Token Savings:** Prevents hallucinated field usage which causes retry loops; reduces cascading errors by 60–80% in tool-heavy agents.

**Environment:** Any synchronous agent using Pydantic; add schemas incrementally as new tools are integrated.

---

### Option 2 — Key-allow-list injection: tell the model exactly which keys exist

```python
import anthropic
import json

client = anthropic.Anthropic()

# Authoritative field lists per tool — injected into tool result content
ALLOWED_FIELDS: dict[str, list[str]] = {
    "get_user":  ["id", "status", "timestamp", "email"],
    "get_order": ["order_id", "total_cents", "currency", "created_epoch"],
}

def wrap_result_with_field_guide(tool_name: str, raw: dict) -> str:
    allowed = ALLOWED_FIELDS.get(tool_name)
    if not allowed:
        return json.dumps(raw)

    unknown = [k for k in raw if k not in allowed]
    missing  = [k for k in allowed if k not in raw]

    lines = [
        f"TOOL RESULT for '{tool_name}':",
        json.dumps(raw),
        "",
        f"VALID FIELDS: {allowed}",
    ]
    if unknown:
        lines.append(f"WARNING — unexpected keys returned (ignore): {unknown}")
    if missing:
        lines.append(f"NOTE — these fields were absent (treat as null): {missing}")
    lines.append("Only reference the keys shown in TOOL RESULT above.")
    return "\n".join(lines)

def fake_api(name: str) -> dict:
    if name == "get_user":
        return {"id": 7, "status": "suspended", "timestamp": 1700000000}
    if name == "get_order":
        return {"order_id": "X-200", "total_cents": 1099, "currency": "EUR", "created_epoch": 1711000000}
    return {}

TOOLS = [
    {
        "name": "get_user",
        "description": "Fetch user record. Returns: id, status, timestamp, email.",
        "input_schema": {
            "type": "object",
            "properties": {"user_id": {"type": "integer"}},
            "required": ["user_id"],
        },
    },
    {
        "name": "get_order",
        "description": "Fetch order record. Returns: order_id, total_cents, currency, created_epoch.",
        "input_schema": {
            "type": "object",
            "properties": {"order_id": {"type": "string"}},
            "required": ["order_id"],
        },
    },
]

def run(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]
    while True:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            tools=TOOLS,
            messages=messages,
        )
        messages.append({"role": "assistant", "content": resp.content})
        if resp.stop_reason != "tool_use":
            break
        messages.append({
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": b.id,
                    "content": wrap_result_with_field_guide(b.name, fake_api(b.name)),
                }
                for b in resp.content if b.type == "tool_use"
            ],
        })
    return next(b.text for b in resp.content if hasattr(b, "text"))

print(run("Fetch user 7 and order X-200. What is the user's creation date and email?"))
```

**Expected Token Savings:** Eliminates field-name guessing errors; slightly increases tool result tokens (+50–100 chars per call) but prevents costly error-recovery turns.

**Environment:** No external dependencies; works with any agent orchestrator.

---

### Option 3 — Response normalizer that maps common alias fields

```python
import anthropic
import json

client = anthropic.Anthropic()

# Map of (tool_name → {alias: canonical}) for field name normalization
FIELD_ALIASES: dict[str, dict[str, str]] = {
    "get_user": {
        "user_id":    "id",
        "userId":     "id",
        "created_at": "timestamp",
        "createdAt":  "timestamp",
        "mail":       "email",
    },
    "get_account": {
        "account_id": "id",
        "accountId":  "id",
        "balance_cents": "balance",
    },
}

def normalize_response(tool_name: str, raw: dict) -> dict:
    """Rename aliased keys to canonical names so the model sees consistent fields."""
    aliases = FIELD_ALIASES.get(tool_name, {})
    normalized = {}
    for k, v in raw.items():
        canonical = aliases.get(k, k)
        if canonical in normalized:
            print(f"  [NORMALIZE] collision: '{k}' → '{canonical}' already set, skipping")
        else:
            if k != canonical:
                print(f"  [NORMALIZE] '{k}' → '{canonical}'")
            normalized[canonical] = v
    return normalized

def fake_api(name: str) -> dict:
    # Simulates an API that uses inconsistent naming
    if name == "get_user":
        return {"userId": 55, "status": "active", "created_at": 1710000000, "mail": "alice@example.com"}
    if name == "get_account":
        return {"accountId": "ACC-9", "balance_cents": 25000, "currency": "USD"}
    return {}

TOOLS = [
    {
        "name": "get_user",
        "description": "Fetch user. Canonical response fields: id, status, timestamp, email.",
        "input_schema": {
            "type": "object",
            "properties": {"user_id": {"type": "integer"}},
            "required": ["user_id"],
        },
    },
    {
        "name": "get_account",
        "description": "Fetch account. Canonical response fields: id, balance, currency.",
        "input_schema": {
            "type": "object",
            "properties": {"account_id": {"type": "string"}},
            "required": ["account_id"],
        },
    },
]

def run(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]
    while True:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            tools=TOOLS,
            messages=messages,
        )
        messages.append({"role": "assistant", "content": resp.content})
        if resp.stop_reason != "tool_use":
            break
        messages.append({
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": b.id,
                    "content": json.dumps(normalize_response(b.name, fake_api(b.name))),
                }
                for b in resp.content if b.type == "tool_use"
            ],
        })
    return next(b.text for b in resp.content if hasattr(b, "text"))

print(run("Get user 55 and account ACC-9. Summarize when the user was created and their balance."))
```

**Expected Token Savings:** Avoids retry cycles caused by the model using wrong field names; normalization runs in microseconds with no token cost.

**Environment:** Ideal for agents integrating multiple APIs with inconsistent naming conventions.

---

### Option 4 — Field-access guard that returns explicit error on missing key

```python
import anthropic
import json

client = anthropic.Anthropic()

KNOWN_FIELDS: dict[str, set[str]] = {
    "get_product": {"product_id", "name", "price_cents", "in_stock", "sku"},
    "get_review":  {"review_id", "rating", "body", "author", "created_epoch"},
}

def guarded_result(tool_name: str, raw: dict) -> str:
    known = KNOWN_FIELDS.get(tool_name)
    if known is None:
        return json.dumps(raw)

    hallucination_risk = {k: v for k, v in raw.items() if k not in known}
    safe = {k: v for k, v in raw.items() if k in known}

    parts = {"data": safe}
    if hallucination_risk:
        parts["_unknown_fields_do_not_use"] = list(hallucination_risk.keys())

    missing = known - set(raw.keys())
    if missing:
        parts["_absent_fields"] = {k: None for k in missing}
        parts["_instruction"] = (
            "Fields listed in _absent_fields were NOT returned by the API. "
            "Do not fabricate values for them. State they are unavailable."
        )

    return json.dumps(parts)

def fake_api(name: str) -> dict:
    if name == "get_product":
        # API returns 'productId' and 'priceUSD' instead of canonical names
        return {"product_id": "P-10", "name": "Widget", "price_cents": 999, "sku": "WGT-BLU"}
        # 'in_stock' is missing from this response
    if name == "get_review":
        return {"review_id": "R-55", "rating": 4, "body": "Great product!", "author": "bob", "created_epoch": 1700000000}
    return {}

TOOLS = [
    {
        "name": "get_product",
        "description": "Fetch product. Fields: product_id, name, price_cents, in_stock, sku.",
        "input_schema": {
            "type": "object",
            "properties": {"product_id": {"type": "string"}},
            "required": ["product_id"],
        },
    },
    {
        "name": "get_review",
        "description": "Fetch a review. Fields: review_id, rating, body, author, created_epoch.",
        "input_schema": {
            "type": "object",
            "properties": {"review_id": {"type": "string"}},
            "required": ["review_id"],
        },
    },
]

def run(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]
    while True:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            tools=TOOLS,
            messages=messages,
        )
        messages.append({"role": "assistant", "content": resp.content})
        if resp.stop_reason != "tool_use":
            break
        messages.append({
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": b.id,
                    "content": guarded_result(b.name, fake_api(b.name)),
                }
                for b in resp.content if b.type == "tool_use"
            ],
        })
    return next(b.text for b in resp.content if hasattr(b, "text"))

print(run("Get product P-10 and review R-55. Is the product in stock? What rating did it get?"))
```

**Expected Token Savings:** Forces honest "unavailable" answers instead of hallucinated values; eliminates silent data corruption in downstream logic.

**Environment:** Drop-in middleware; annotate tool schemas with `Fields:` lists in the description to reinforce field names at the model level.

---

### Option 5 — Post-generation field auditor (verify before returning to user)

```python
import anthropic
import json
import re

client = anthropic.Anthropic()

# Ground truth: what fields were actually returned per tool call in this session
_tool_result_registry: dict[str, set[str]] = {}

def register_tool_result(tool_use_id: str, tool_name: str, raw: dict) -> str:
    all_keys = set(raw.keys())
    _tool_result_registry[tool_use_id] = all_keys
    return json.dumps(raw)

def audit_agent_response(response_text: str) -> list[str]:
    """
    Heuristic scan: look for field-access patterns like ['field'] or .field
    that weren't in any returned tool result.
    """
    all_returned_keys: set[str] = set()
    for keys in _tool_result_registry.values():
        all_returned_keys |= keys

    # Find quoted key references in the response
    bracket_refs = set(re.findall(r"\[[\'\"](\w+)[\'\"]\]", response_text))
    dot_refs     = set(re.findall(r"(?<!\w)\.(\w+)(?!\w*\()", response_text))
    referenced   = bracket_refs | dot_refs

    suspicious = referenced - all_returned_keys - {"text", "type", "id"}
    return list(suspicious)

def fake_api(name: str, inputs: dict) -> dict:
    if name == "get_customer":
        return {"id": inputs.get("customer_id", 1), "plan": "pro", "join_epoch": 1680000000}
    return {}

TOOLS = [
    {
        "name": "get_customer",
        "description": "Fetch customer. Returns: id, plan, join_epoch.",
        "input_schema": {
            "type": "object",
            "properties": {"customer_id": {"type": "integer"}},
            "required": ["customer_id"],
        },
    },
]

def run(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]
    while True:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            tools=TOOLS,
            messages=messages,
        )
        messages.append({"role": "assistant", "content": resp.content})

        if resp.stop_reason != "tool_use":
            # Final text — audit it
            text = next((b.text for b in resp.content if hasattr(b, "text")), "")
            suspicious = audit_agent_response(text)
            if suspicious:
                print(f"\n[AUDIT WARNING] Response may reference non-existent fields: {suspicious}")
            return text

        messages.append({
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": b.id,
                    "content": register_tool_result(b.id, b.name, fake_api(b.name, b.input)),
                }
                for b in resp.content if b.type == "tool_use"
            ],
        })

print(run("Get customer 3. What is their email and subscription start date?"))
```

**Expected Token Savings:** Post-generation audit catches field hallucinations before they reach users; combine with Option 1 for defense-in-depth.

**Environment:** Logging and observability pipelines; useful for detecting model drift when API schemas evolve.

---

### Option 6 — Schema-injected system prompt with field contracts

```python
import anthropic
import json

client = anthropic.Anthropic()

TOOL_SCHEMAS = {
    "get_invoice": {
        "fields": {
            "invoice_id": "string — unique invoice identifier",
            "amount_cents": "integer — total in smallest currency unit",
            "currency": "string — ISO 4217 code (e.g. 'USD')",
            "due_epoch": "integer — Unix timestamp of due date",
            "paid": "boolean — whether the invoice has been paid",
        },
        "absent_behavior": "If a field is missing from the response, state it is unavailable. Never invent a value.",
    },
    "get_subscription": {
        "fields": {
            "sub_id": "string — subscription identifier",
            "plan": "string — plan name (e.g. 'starter', 'pro', 'enterprise')",
            "renews_epoch": "integer — Unix timestamp of next renewal",
            "seat_count": "integer — number of licensed seats",
        },
        "absent_behavior": "If a field is missing, say it is not available rather than guessing.",
    },
}

def build_schema_system_prompt() -> str:
    lines = [
        "You are a billing assistant. When you call tools, the results will contain ONLY the fields listed below.",
        "Do NOT reference, guess, or invent field names that are not in these contracts.",
        "",
        "## Tool Response Contracts",
    ]
    for tool_name, schema in TOOL_SCHEMAS.items():
        lines.append(f"\n### {tool_name}")
        lines.append("Fields:")
        for field, desc in schema["fields"].items():
            lines.append(f"  - `{field}`: {desc}")
        lines.append(f"Missing field rule: {schema['absent_behavior']}")
    return "\n".join(lines)

SYSTEM_PROMPT = build_schema_system_prompt()

def fake_api(name: str) -> dict:
    if name == "get_invoice":
        return {"invoice_id": "INV-42", "amount_cents": 9900, "currency": "USD", "due_epoch": 1715000000, "paid": False}
    if name == "get_subscription":
        return {"sub_id": "SUB-7", "plan": "pro", "renews_epoch": 1720000000}
        # seat_count is missing
    return {}

TOOLS = [
    {
        "name": "get_invoice",
        "description": "Retrieve an invoice by ID.",
        "input_schema": {
            "type": "object",
            "properties": {"invoice_id": {"type": "string"}},
            "required": ["invoice_id"],
        },
    },
    {
        "name": "get_subscription",
        "description": "Retrieve a subscription by ID.",
        "input_schema": {
            "type": "object",
            "properties": {"sub_id": {"type": "string"}},
            "required": ["sub_id"],
        },
    },
]

def run(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]
    while True:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages,
        )
        messages.append({"role": "assistant", "content": resp.content})
        if resp.stop_reason != "tool_use":
            break
        messages.append({
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": b.id, "content": json.dumps(fake_api(b.name))}
                for b in resp.content if b.type == "tool_use"
            ],
        })
    return next(b.text for b in resp.content if hasattr(b, "text"))

print(run("Get invoice INV-42 and subscription SUB-7. How many seats and when is payment due?"))
```

**Expected Token Savings:** System-prompt field contracts reduce hallucinated-field errors by 70–90%; slight system-prompt overhead (~200 tokens) is far cheaper than multi-turn error recovery.

**Environment:** Most effective when combined with prompt caching — the schema block is static and will be cache-hit on every subsequent turn.

---

## Comparison

| Option | Mechanism | Catches at | Extra Latency | Schema Source |
|--------|-----------|------------|--------------|--------------|
| 1 — Pydantic validation | Parse + validate | Tool execution | Negligible | Pydantic models |
| 2 — Key-allow-list injection | Text annotation in result | Tool execution | Negligible | Config dict |
| 3 — Response normalizer | Rename aliases to canonical | Tool execution | Negligible | Alias map |
| 4 — Field-access guard | Partition known/unknown/absent | Tool execution | Negligible | Known-field sets |
| 5 — Post-gen auditor | Regex scan of final text | After generation | ~1ms | Registry of returned keys |
| 6 — Schema system prompt | Upfront field contracts | Model inference | 0 | System prompt |

**Recommended default:** Option 1 (Pydantic) for strong correctness guarantees, combined with Option 6 (schema system prompt) to steer model behavior before any tool calls are made.
