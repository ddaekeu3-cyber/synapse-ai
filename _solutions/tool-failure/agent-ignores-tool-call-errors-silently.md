---
layout: solution
title: "Agent Ignores Tool Call Errors Silently"
category: tool-failure
description: "Tool returns an error payload but the agent treats it as success, producing confident wrong answers based on failed tool output."
tags: [tool-failure, error-handling, reliability, tool-use, anthropic-sdk]
---

## Symptom

A tool call fails — the database is unreachable, the API returned HTTP 500, or the function threw an exception — but the `tool_result` content is set to an empty string or a generic message. The model receives this silently broken result and continues the conversation, inventing plausible-sounding answers rather than acknowledging the failure. The user sees confident output built on a failed tool call.

## Root Cause

The Anthropic API accepts any string as `tool_result` content. If the tool executor catches exceptions and returns an empty string or a truncated error, the model has no way to distinguish success from failure. The model's training biases it toward completing the task — so it hallucinates rather than stopping to report the error. The fix is to include structured error signals in the tool result and instruct the model to surface errors explicitly.

## Fix

### Option 1 — Return a structured error object on failure

```python
import json
import anthropic

client = anthropic.Anthropic()

def execute_tool(name: str, inputs: dict) -> str:
    """Execute a tool and always return a JSON string with a success/error envelope."""
    try:
        if name == "get_user":
            user_id = inputs.get("user_id", "")
            if not user_id:
                raise ValueError("user_id is required")
            # Simulate DB call
            return json.dumps({"ok": True, "data": {"id": user_id, "name": "Alice", "email": "alice@example.com"}})

        if name == "send_email":
            raise ConnectionError("SMTP server unreachable")

        raise ValueError(f"Unknown tool: {name!r}")

    except Exception as e:
        return json.dumps({
            "ok":    False,
            "error": type(e).__name__,
            "message": str(e),
        })

TOOLS = [
    {
        "name": "get_user",
        "description": "Look up a user by ID. Returns {ok, data} or {ok: false, error, message}.",
        "input_schema": {
            "type": "object",
            "properties": {"user_id": {"type": "string"}},
            "required": ["user_id"],
        },
    },
    {
        "name": "send_email",
        "description": "Send an email. Returns {ok, data} or {ok: false, error, message}.",
        "input_schema": {
            "type": "object",
            "properties": {
                "to":      {"type": "string"},
                "subject": {"type": "string"},
                "body":    {"type": "string"},
            },
            "required": ["to", "subject", "body"],
        },
    },
]

SYSTEM = """When a tool returns {"ok": false, ...}, stop and tell the user exactly what failed.
Do NOT invent or assume what the result would have been."""

def run_agent(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]
    for _ in range(6):
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=SYSTEM,
            tools=TOOLS,
            messages=messages,
        )
        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if b.type == "text"), "")

        messages.append({"role": "assistant", "content": response.content})
        results = []
        for block in response.content:
            if block.type == "tool_use":
                result = execute_tool(block.name, block.input)
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})
        messages.append({"role": "user", "content": results})

    return "max steps reached"

print(run_agent("Get user u42, then send them a welcome email."))
```

**Expected Token Savings:** Eliminates extra turns where the model tries to recover from silently broken tool output; one clear error stops the loop immediately.
**Environment:** Any tool-using agent; the `{ok, data/error}` envelope is a universal pattern.

---

### Option 2 — Mark tool_result with `is_error: true`

```python
import json
import anthropic

client = anthropic.Anthropic()

def safe_tool_call(name: str, inputs: dict) -> tuple[str, bool]:
    """Return (content, is_error)."""
    try:
        if name == "fetch_price":
            ticker = inputs.get("ticker", "")
            # Simulate occasional failure
            import random
            if random.random() < 0.5:
                raise TimeoutError(f"Price feed timeout for {ticker!r}")
            return json.dumps({"ticker": ticker, "price": 142.50, "currency": "USD"}), False

        raise ValueError(f"Unknown tool: {name!r}")

    except Exception as e:
        return f"{type(e).__name__}: {e}", True

TOOLS = [
    {
        "name": "fetch_price",
        "description": "Fetch the current stock price for a ticker symbol.",
        "input_schema": {
            "type": "object",
            "properties": {"ticker": {"type": "string", "description": "Stock ticker, e.g. AAPL"}},
            "required": ["ticker"],
        },
    }
]

def run_agent(query: str) -> str:
    messages = [{"role": "user", "content": query}]
    for _ in range(6):
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=TOOLS,
            messages=messages,
        )
        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if b.type == "text"), "")

        messages.append({"role": "assistant", "content": response.content})
        results = []
        for block in response.content:
            if block.type == "tool_use":
                content, is_error = safe_tool_call(block.name, block.input)
                result = {
                    "type":        "tool_result",
                    "tool_use_id": block.id,
                    "content":     content,
                }
                if is_error:
                    result["is_error"] = True  # SDK-level error flag
                results.append(result)
        messages.append({"role": "user", "content": results})

    return "max steps reached"

print(run_agent("What is the current price of NVDA?"))
```

**Expected Token Savings:** `is_error: true` is natively understood by Claude — it will not hallucinate a price from an error message; saves a retry turn.
**Environment:** Any tool-using agent; `is_error` is the cleanest signal — use this over custom JSON envelopes when possible.

---

### Option 3 — Validate tool output schema before returning to the model

```python
import json
import anthropic

client = anthropic.Anthropic()

# Expected shape for each tool's successful response
RESPONSE_SCHEMAS: dict[str, set[str]] = {
    "search_products":  {"results", "total", "page"},
    "get_order_status": {"order_id", "status", "updated_at"},
    "calculate_tax":    {"subtotal", "tax_rate", "tax_amount", "total"},
}

def validate_tool_response(tool_name: str, response_data: dict) -> str | None:
    """Return an error string if the response is missing required fields, else None."""
    required = RESPONSE_SCHEMAS.get(tool_name)
    if required is None:
        return None  # no schema defined — skip validation
    missing = required - set(response_data.keys())
    if missing:
        return f"Incomplete response: missing fields {sorted(missing)}"
    return None

def execute_tool(name: str, inputs: dict) -> tuple[str, bool]:
    try:
        if name == "search_products":
            # Simulate a partial response (missing "total" and "page")
            raw = {"results": [{"id": "p1", "name": "Widget"}]}
            err = validate_tool_response(name, raw)
            if err:
                return f"Tool validation error: {err}", True
            return json.dumps(raw), False

        if name == "get_order_status":
            raw = {"order_id": inputs.get("order_id"), "status": "shipped", "updated_at": "2025-04-10T12:00:00Z"}
            err = validate_tool_response(name, raw)
            if err:
                return f"Tool validation error: {err}", True
            return json.dumps(raw), False

        raise ValueError(f"Unknown tool: {name!r}")
    except Exception as e:
        return f"{type(e).__name__}: {e}", True

TOOLS = [
    {
        "name": "search_products",
        "description": "Search the product catalogue.",
        "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    },
    {
        "name": "get_order_status",
        "description": "Get the current status of an order.",
        "input_schema": {"type": "object", "properties": {"order_id": {"type": "string"}}, "required": ["order_id"]},
    },
]

def run_agent(query: str) -> str:
    messages = [{"role": "user", "content": query}]
    for _ in range(6):
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=TOOLS,
            messages=messages,
        )
        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if b.type == "text"), "")

        messages.append({"role": "assistant", "content": response.content})
        results = []
        for block in response.content:
            if block.type == "tool_use":
                content, is_error = execute_tool(block.name, block.input)
                r = {"type": "tool_result", "tool_use_id": block.id, "content": content}
                if is_error:
                    r["is_error"] = True
                results.append(r)
        messages.append({"role": "user", "content": results})
    return "max steps reached"

print(run_agent("Search for widgets and then check order ORD-9999."))
```

**Expected Token Savings:** Catches partial tool responses before they mislead the model into a wrong answer that requires a correction turn.
**Environment:** Agents integrating third-party APIs that sometimes return incomplete responses.

---

### Option 4 — Retry the tool call on transient errors, escalate on permanent ones

```python
import time
import json
import random
import anthropic

client = anthropic.Anthropic()

TRANSIENT_ERRORS = {ConnectionError, TimeoutError, OSError}
MAX_TOOL_RETRIES = 3

def call_external_api(query: str) -> dict:
    """Simulated external API — fails transiently 40% of the time."""
    if random.random() < 0.4:
        raise ConnectionError("upstream timeout")
    return {"results": [f"item-{i}" for i in range(3)], "query": query}

def execute_with_retry(name: str, inputs: dict) -> tuple[str, bool]:
    last_err = None
    for attempt in range(MAX_TOOL_RETRIES):
        try:
            if name == "search":
                data = call_external_api(inputs.get("query", ""))
                return json.dumps(data), False
            raise ValueError(f"Unknown tool: {name!r}")

        except tuple(TRANSIENT_ERRORS) as e:
            last_err = e
            wait = 2 ** attempt
            print(f"[tool] transient error on attempt {attempt + 1}: {e}. Retrying in {wait}s")
            time.sleep(wait)
        except Exception as e:
            # Permanent error — don't retry
            return f"Permanent error: {type(e).__name__}: {e}", True

    return f"Failed after {MAX_TOOL_RETRIES} attempts: {last_err}", True

TOOLS = [
    {
        "name": "search",
        "description": "Search the knowledge base.",
        "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    }
]

def run_agent(query: str) -> str:
    messages = [{"role": "user", "content": query}]
    for _ in range(6):
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=TOOLS,
            messages=messages,
        )
        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if b.type == "text"), "")

        messages.append({"role": "assistant", "content": response.content})
        results = []
        for block in response.content:
            if block.type == "tool_use":
                content, is_error = execute_with_retry(block.name, block.input)
                r = {"type": "tool_result", "tool_use_id": block.id, "content": content}
                if is_error:
                    r["is_error"] = True
                results.append(r)
        messages.append({"role": "user", "content": results})
    return "max steps reached"

print(run_agent("Search for Python tutorials."))
```

**Expected Token Savings:** Transient errors resolved at the tool layer don't reach the model, saving the extra conversational turn to handle the error.
**Environment:** Agents calling flaky external APIs (webhooks, scrapers, third-party SaaS); retry at the tool level, not the agent level.

---

### Option 5 — Error taxonomy in the system prompt

```python
import json
import anthropic

client = anthropic.Anthropic()

SYSTEM = """You are a data assistant. When a tool returns an error, follow these rules:

- error_type "not_found"    → Tell the user the item doesn't exist. Do not retry.
- error_type "permission"   → Tell the user they lack access. Do not retry.
- error_type "rate_limit"   → Tell the user to try again in a few seconds. Do not retry now.
- error_type "transient"    → Call the same tool once more. If it fails again, report the error.
- error_type "invalid_input"→ Ask the user to correct their input. Do not retry.

Never fabricate data when a tool fails."""

def execute_tool(name: str, inputs: dict) -> str:
    if name == "get_record":
        record_id = inputs.get("id", "")
        if record_id.startswith("missing"):
            return json.dumps({"ok": False, "error_type": "not_found", "message": f"Record {record_id!r} not found"})
        if record_id.startswith("secret"):
            return json.dumps({"ok": False, "error_type": "permission", "message": "Access denied"})
        return json.dumps({"ok": True, "data": {"id": record_id, "value": 42}})
    return json.dumps({"ok": False, "error_type": "invalid_input", "message": f"Unknown tool: {name!r}"})

TOOLS = [
    {
        "name": "get_record",
        "description": "Retrieve a record by ID.",
        "input_schema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
    }
]

def run_agent(query: str) -> str:
    messages = [{"role": "user", "content": query}]
    for _ in range(6):
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=SYSTEM,
            tools=TOOLS,
            messages=messages,
        )
        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if b.type == "text"), "")

        messages.append({"role": "assistant", "content": response.content})
        results = [
            {"type": "tool_result", "tool_use_id": b.id, "content": execute_tool(b.name, b.input)}
            for b in response.content if b.type == "tool_use"
        ]
        messages.append({"role": "user", "content": results})
    return "max steps reached"

for q in ["Get record rec-123.", "Get record missing-456.", "Get record secret-789."]:
    print(f"\nQuery: {q}")
    print(run_agent(q))
```

**Expected Token Savings:** Error taxonomy lets the model take the correct action on first error, eliminating recovery loops.
**Environment:** Agents with multiple error types needing different recovery strategies; documents expected errors explicitly.

---

### Option 6 — Post-call assertion: verify model didn't use a failed result

```python
import json
import re
import anthropic

client = anthropic.Anthropic()

class ToolResultAuditor:
    """Track which tool_use_ids returned errors and verify the model's reply."""

    def __init__(self):
        self.failed_ids: set[str] = set()

    def register_result(self, tool_use_id: str, content: str) -> None:
        try:
            data = json.loads(content)
            if not data.get("ok", True):
                self.failed_ids.add(tool_use_id)
        except (json.JSONDecodeError, AttributeError):
            pass

    def audit_reply(self, reply: str) -> list[str]:
        """Return warnings if the reply appears to use data from a failed tool."""
        warnings = []
        # Heuristic: look for specific patterns that suggest fabricated success
        suspicious = [r"\$[\d,]+", r"\d+\s+results?", r"successfully", r"found \d+"]
        if self.failed_ids:
            for pattern in suspicious:
                if re.search(pattern, reply, re.IGNORECASE):
                    warnings.append(f"Reply may contain fabricated data from failed tool(s): {self.failed_ids}")
                    break
        return warnings

def execute_tool(name: str, inputs: dict) -> str:
    if name == "get_balance":
        return json.dumps({"ok": False, "error_type": "transient", "message": "Database connection lost"})
    return json.dumps({"ok": False, "error_type": "not_found"})

TOOLS = [
    {
        "name": "get_balance",
        "description": "Get account balance.",
        "input_schema": {"type": "object", "properties": {"account_id": {"type": "string"}}, "required": ["account_id"]},
    }
]

def run_audited_agent(query: str) -> str:
    auditor  = ToolResultAuditor()
    messages = [{"role": "user", "content": query}]

    for _ in range(6):
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=TOOLS,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            reply = next((b.text for b in response.content if b.type == "text"), "")
            warnings = auditor.audit_reply(reply)
            for w in warnings:
                print(f"[AUDIT WARNING] {w}")
            return reply

        messages.append({"role": "assistant", "content": response.content})
        results = []
        for block in response.content:
            if block.type == "tool_use":
                content = execute_tool(block.name, block.input)
                auditor.register_result(block.id, content)
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": content, "is_error": True})
        messages.append({"role": "user", "content": results})

    return "max steps reached"

print(run_audited_agent("What is the balance on account ACC-001?"))
```

**Expected Token Savings:** Auditing catches hallucinated success before it reaches downstream systems, preventing cascading errors that would be expensive to diagnose.
**Environment:** High-stakes agents (finance, medical, legal) where a hallucinated success from a failed tool could cause real harm.

---

## Comparison

| Option | Error Signal | Model Awareness | Retry Logic | Best For |
|---|---|---|---|---|
| 1. Structured envelope | `{ok: false}` in content | Via system prompt | No | Universal baseline |
| 2. `is_error: true` flag | SDK-native field | Native | No | Cleanest signal — prefer this |
| 3. Schema validation | Validation error string | Via content | No | Partial response detection |
| 4. Retry + escalate | Error after retries | Via content | Yes | Transient external API failures |
| 5. Error taxonomy | Typed error in JSON | Via system prompt | Conditional | Multiple error types needing different handling |
| 6. Post-call audit | Pattern detection | External check | No | High-stakes agents, compliance |
