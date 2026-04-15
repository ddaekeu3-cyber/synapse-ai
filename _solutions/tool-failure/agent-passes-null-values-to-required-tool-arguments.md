---
layout: solution
title: "Agent Passes Null Values to Required Tool Arguments"
category: tool-failure
description: "Agent constructs tool calls where required arguments are None, empty string, or missing — because upstream data was absent, a template variable was unset, or extraction failed. The API returns a 400 error or the tool silently receives garbage input and produces wrong results."
tags: [tool-failure, validation, null, required-arguments, schema, tool-use, error-handling]
---

## Symptom

Agent extracts a customer ID from user input, then calls `get_customer_profile(customer_id=None)` because the extraction step returned nothing. The tool receives `None` as a required string argument. Some tools raise a `TypeError`; others silently convert it to the string `"None"` and query the wrong record. The agent never detects the problem and returns a confident but wrong answer.

Null argument errors in production (without guards): **8–15% of tool calls** involving extracted/templated values

## Root Cause

Tool arguments are assembled from extracted or computed values without checking for None/empty before the call. The model constructs tool calls from its context — if the context contained `None` or an empty value, those propagate directly into the `input` dict of the tool_use block. No validation layer exists between extraction and execution.

## Fix

---

### Option 1 — Pre-Call Argument Validator

Validate all tool arguments against the tool's required fields before executing. Return a structured error to the agent on null/empty required arguments.

```python
import json
import anthropic
from typing import Any

client = anthropic.Anthropic()

# Required fields per tool — mirrors tool input_schema required arrays
REQUIRED_FIELDS: dict[str, list[str]] = {
    "get_customer_profile": ["customer_id"],
    "update_order_status":  ["order_id", "new_status"],
    "send_notification":    ["user_id", "message", "channel"],
    "calculate_invoice":    ["customer_id", "line_items"],
}

NULL_LIKE = {None, "", "null", "none", "undefined", "n/a"}

def is_null_like(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and value.strip().lower() in NULL_LIKE:
        return True
    if isinstance(value, (list, dict)) and len(value) == 0:
        return True
    return False

def validate_tool_args(tool_name: str, args: dict) -> list[str]:
    """Return list of missing/null required arguments. Empty list = valid."""
    required = REQUIRED_FIELDS.get(tool_name, [])
    missing = []
    for field in required:
        if field not in args or is_null_like(args.get(field)):
            missing.append(field)
    return missing

def safe_execute_tool(tool_name: str, args: dict, fn) -> str:
    """Validate args before calling tool. Return error payload on null args."""
    missing = validate_tool_args(tool_name, args)
    if missing:
        error_payload = {
            "error": "missing_required_arguments",
            "tool": tool_name,
            "missing_fields": missing,
            "received_args": {k: repr(v) for k, v in args.items()},
            "agent_instruction": (
                f"Tool '{tool_name}' requires {missing} but received null/empty values. "
                "Ask the user to provide the missing information before retrying."
            ),
        }
        print(f"[Validator] Blocked {tool_name}: missing {missing}")
        return json.dumps(error_payload)

    try:
        return fn(**args)
    except Exception as e:
        return json.dumps({"error": str(e), "tool": tool_name})

# Tool implementations
def get_customer_profile(customer_id: str) -> str:
    return json.dumps({"customer_id": customer_id, "name": "Alice Chen", "plan": "pro"})

def update_order_status(order_id: str, new_status: str) -> str:
    return json.dumps({"order_id": order_id, "status": new_status, "updated": True})

def send_notification(user_id: str, message: str, channel: str) -> str:
    return json.dumps({"sent": True, "user_id": user_id, "channel": channel})

def calculate_invoice(customer_id: str, line_items: list) -> str:
    total = sum(item.get("amount", 0) for item in line_items)
    return json.dumps({"customer_id": customer_id, "total": total})

TOOL_MAP = {
    "get_customer_profile": get_customer_profile,
    "update_order_status":  update_order_status,
    "send_notification":    send_notification,
    "calculate_invoice":    calculate_invoice,
}

TOOLS = [
    {"name": "get_customer_profile", "description": "Get profile for a customer by ID.",
     "input_schema": {"type": "object",
                      "properties": {"customer_id": {"type": "string", "description": "Required — must not be empty"}},
                      "required": ["customer_id"]}},
    {"name": "update_order_status", "description": "Update the status of an order.",
     "input_schema": {"type": "object",
                      "properties": {"order_id": {"type": "string"}, "new_status": {"type": "string"}},
                      "required": ["order_id", "new_status"]}},
]

SYSTEM = """You are a customer service assistant.
If a tool returns 'missing_required_arguments', ask the user for the missing information.
Never guess or fabricate argument values."""

def run_agent(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]
    while True:
        response = client.messages.create(
            model="claude-sonnet-4-6", max_tokens=512, system=SYSTEM, tools=TOOLS, messages=messages
        )
        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if hasattr(b, "text")), "")
        messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                fn = TOOL_MAP.get(block.name)
                result = safe_execute_tool(block.name, block.input, fn) if fn else json.dumps({"error": "unknown"})
                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})
        messages.append({"role": "user", "content": tool_results})

# Normal call — ID provided
print("=== Valid call ===")
print(run_agent("Get the profile for customer ID CUS-4821."))

# Ambiguous call — model may extract None for missing ID
print("\n=== Ambiguous call (no ID) ===")
print(run_agent("Can you look up that customer's profile?"))
```

**Expected Token Savings:** 10–20% — prevents failed tool calls that require extra correction turns
**Environment:** `pip install anthropic`

---

### Option 2 — Null-Coalescing Argument Defaults with Sentinel Detection

Replace null arguments with safe sentinel values and encode them in the tool result so the agent knows to prompt the user — rather than failing with a 400 error.

```python
import json
import anthropic
from typing import Any

client = anthropic.Anthropic()

SENTINEL = "__MISSING__"

def coalesce(*values: Any, default: Any = SENTINEL) -> Any:
    """Return the first non-null-like value, or default."""
    null_set = {None, "", "null", "none", "undefined"}
    for v in values:
        if v is not None and str(v).strip().lower() not in null_set:
            return v
    return default

def normalise_args(tool_name: str, raw_args: dict, defaults: dict = None) -> dict[str, Any]:
    """
    Normalise tool arguments:
    - Replace null-like values with SENTINEL
    - Apply defaults for optional fields
    - Return normalised dict with a _null_fields list
    """
    defaults = defaults or {}
    normalised = {}
    null_fields = []

    for key, value in raw_args.items():
        resolved = coalesce(value, defaults.get(key))
        if resolved is SENTINEL:
            null_fields.append(key)
            normalised[key] = SENTINEL
        else:
            normalised[key] = resolved

    normalised["_null_fields"] = null_fields
    return normalised

def execute_with_null_guard(tool_name: str, raw_args: dict, fn, defaults: dict = None) -> str:
    normalised = normalise_args(tool_name, raw_args, defaults)
    null_fields = normalised.pop("_null_fields", [])

    if null_fields:
        return json.dumps({
            "status": "incomplete",
            "tool": tool_name,
            "null_arguments": null_fields,
            "agent_instruction": f"Cannot call {tool_name} — {null_fields} are null. Ask user to provide them.",
        })

    return fn(**normalised)

# Tools
def book_appointment(doctor_id: str, patient_id: str, date: str, time_slot: str = "09:00") -> str:
    return json.dumps({"booked": True, "doctor": doctor_id, "patient": patient_id,
                       "date": date, "time": time_slot})

def cancel_appointment(appointment_id: str, reason: str = "user_request") -> str:
    return json.dumps({"cancelled": True, "appointment_id": appointment_id, "reason": reason})

TOOLS = [
    {"name": "book_appointment",
     "description": "Book a medical appointment. doctor_id, patient_id, and date are required.",
     "input_schema": {
         "type": "object",
         "properties": {
             "doctor_id":   {"type": "string", "description": "Required — doctor identifier"},
             "patient_id":  {"type": "string", "description": "Required — patient identifier"},
             "date":        {"type": "string", "description": "Required — appointment date YYYY-MM-DD"},
             "time_slot":   {"type": "string", "description": "Optional — defaults to 09:00"},
         },
         "required": ["doctor_id", "patient_id", "date"],
     }},
    {"name": "cancel_appointment",
     "description": "Cancel an existing appointment by ID.",
     "input_schema": {
         "type": "object",
         "properties": {
             "appointment_id": {"type": "string", "description": "Required"},
             "reason":         {"type": "string", "description": "Optional"},
         },
         "required": ["appointment_id"],
     }},
]

TOOL_MAP = {
    "book_appointment":  (book_appointment,  {"time_slot": "09:00"}),
    "cancel_appointment":(cancel_appointment, {"reason": "user_request"}),
}

def run_agent(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]
    while True:
        response = client.messages.create(
            model="claude-sonnet-4-6", max_tokens=512, tools=TOOLS, messages=messages
        )
        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if hasattr(b, "text")), "")
        messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                fn_info = TOOL_MAP.get(block.name)
                if fn_info:
                    fn, defaults = fn_info
                    result = execute_with_null_guard(block.name, block.input, fn, defaults)
                else:
                    result = json.dumps({"error": "unknown tool"})
                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})
        messages.append({"role": "user", "content": tool_results})

print(run_agent("Book an appointment with doctor D-101 for patient P-555 on 2025-05-01."))
print()
print(run_agent("Book an appointment with the doctor for next Tuesday."))  # Missing IDs
```

**Expected Token Savings:** None — same tokens; prevents silent wrong-record errors
**Environment:** `pip install anthropic`

---

### Option 3 — Pydantic Tool Input Validation Before Execution

Model every tool's input as a Pydantic schema. Validate the raw `block.input` dict before calling the tool — catch type errors and null fields at the Pydantic layer.

```python
import json
import anthropic
from pydantic import BaseModel, field_validator, ValidationError
from typing import Optional

client = anthropic.Anthropic()

# Pydantic models mirror each tool's input_schema
class SearchOrdersInput(BaseModel):
    customer_email: str
    status_filter: Optional[str] = None
    limit: int = 10

    @field_validator("customer_email")
    @classmethod
    def email_not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("customer_email must not be empty")
        if "@" not in v:
            raise ValueError(f"customer_email does not look like an email: {v!r}")
        return v.strip().lower()

    @field_validator("limit")
    @classmethod
    def limit_in_range(cls, v: int) -> int:
        if v < 1 or v > 100:
            raise ValueError(f"limit must be 1–100, got {v}")
        return v

class RefundOrderInput(BaseModel):
    order_id: str
    amount_cents: int
    reason: str

    @field_validator("order_id")
    @classmethod
    def order_id_not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("order_id must not be empty")
        return v.strip()

    @field_validator("amount_cents")
    @classmethod
    def amount_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError(f"amount_cents must be positive, got {v}")
        return v

    @field_validator("reason")
    @classmethod
    def reason_not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("reason must not be empty")
        return v.strip()

# Map tool name → (Pydantic model, implementation function)
def _search_orders(customer_email: str, status_filter: Optional[str] = None, limit: int = 10) -> str:
    orders = [{"id": f"ORD-{i:04d}", "status": "shipped", "total": 49.99} for i in range(min(limit, 3))]
    return json.dumps({"orders": orders, "customer": customer_email, "count": len(orders)})

def _refund_order(order_id: str, amount_cents: int, reason: str) -> str:
    return json.dumps({"refunded": True, "order_id": order_id,
                       "amount": amount_cents / 100, "reason": reason})

TOOL_REGISTRY = {
    "search_orders": (SearchOrdersInput, _search_orders),
    "refund_order":  (RefundOrderInput,  _refund_order),
}

def validated_tool_call(tool_name: str, raw_args: dict) -> str:
    """Validate args with Pydantic, then call the tool. Return error on validation failure."""
    entry = TOOL_REGISTRY.get(tool_name)
    if not entry:
        return json.dumps({"error": f"Unknown tool: {tool_name}"})

    InputModel, fn = entry

    try:
        validated = InputModel(**raw_args)
        return fn(**validated.model_dump())
    except ValidationError as e:
        errors = [{"field": err["loc"][0], "message": err["msg"]} for err in e.errors()]
        return json.dumps({
            "validation_error": True,
            "tool": tool_name,
            "errors": errors,
            "agent_instruction": "Fix the validation errors and ask the user for correct values if needed.",
        })
    except Exception as e:
        return json.dumps({"error": str(e), "tool": tool_name})

TOOLS = [
    {"name": "search_orders",
     "description": "Search orders for a customer by email address.",
     "input_schema": {
         "type": "object",
         "properties": {
             "customer_email": {"type": "string", "description": "Required — customer email address"},
             "status_filter":  {"type": "string", "description": "Optional — filter by order status"},
             "limit":          {"type": "integer", "description": "Max results, 1-100 (default: 10)"},
         },
         "required": ["customer_email"],
     }},
    {"name": "refund_order",
     "description": "Issue a refund for an order.",
     "input_schema": {
         "type": "object",
         "properties": {
             "order_id":      {"type": "string", "description": "Required — order identifier"},
             "amount_cents":  {"type": "integer", "description": "Required — refund amount in cents (positive)"},
             "reason":        {"type": "string", "description": "Required — reason for refund"},
         },
         "required": ["order_id", "amount_cents", "reason"],
     }},
]

def run_agent(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]
    while True:
        response = client.messages.create(
            model="claude-sonnet-4-6", max_tokens=512, tools=TOOLS, messages=messages
        )
        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if hasattr(b, "text")), "")
        messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                result = validated_tool_call(block.name, block.input)
                print(f"[Tool] {block.name} → {json.loads(result).get('error') or json.loads(result).get('validation_error') or 'ok'}")
                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})
        messages.append({"role": "user", "content": tool_results})

print(run_agent("Search orders for alice@example.com."))
print()
print(run_agent("Refund order ORD-0042 for the customer."))  # Missing amount and reason
```

**Expected Token Savings:** None — same tokens; prevents silent type errors and null propagation
**Environment:** `pip install anthropic pydantic`

---

### Option 4 — Tool Argument Extractor with Explicit Null Handling in System Prompt

Add explicit system prompt instructions for how the model must handle missing information — never invent values, always surface gaps before calling tools.

```python
import json
import anthropic

client = anthropic.Anthropic()

SYSTEM = """You are a data operations assistant.

CRITICAL RULES FOR TOOL CALLS:
1. Before calling any tool, verify you have actual values for ALL required arguments.
2. If any required argument is unknown, missing, or ambiguous — DO NOT call the tool.
3. Instead, ask the user for the specific missing information.
4. Never use placeholder values like 'unknown', 'N/A', 'null', or made-up IDs.
5. If the user's message implies a value but doesn't state it explicitly, ask for confirmation.

Example:
User: "Look up the order."
Wrong: call get_order(order_id="unknown")
Right: "I'd be happy to look up the order. Could you provide the order ID?"
"""

def get_order(order_id: str) -> str:
    if not order_id or order_id.lower() in ("unknown", "null", "n/a", ""):
        return json.dumps({"error": f"Invalid order_id: {order_id!r}"})
    return json.dumps({"order_id": order_id, "status": "shipped", "total": 89.99,
                       "items": [{"sku": "SKU-101", "qty": 2}]})

def update_inventory(sku: str, quantity: int, warehouse: str) -> str:
    return json.dumps({"updated": True, "sku": sku, "quantity": quantity, "warehouse": warehouse})

def generate_report(report_type: str, start_date: str, end_date: str) -> str:
    return json.dumps({"report": report_type, "period": f"{start_date} to {end_date}",
                       "records": 1240, "generated": True})

TOOLS = [
    {"name": "get_order",
     "description": "Get order details. Requires explicit order_id — never call with unknown/placeholder.",
     "input_schema": {
         "type": "object",
         "properties": {"order_id": {"type": "string", "description": "Exact order ID — e.g. ORD-12345"}},
         "required": ["order_id"],
     }},
    {"name": "update_inventory",
     "description": "Update inventory count. All three arguments are required.",
     "input_schema": {
         "type": "object",
         "properties": {
             "sku":       {"type": "string"},
             "quantity":  {"type": "integer"},
             "warehouse": {"type": "string"},
         },
         "required": ["sku", "quantity", "warehouse"],
     }},
    {"name": "generate_report",
     "description": "Generate a report for a date range. Both dates required in YYYY-MM-DD format.",
     "input_schema": {
         "type": "object",
         "properties": {
             "report_type": {"type": "string", "enum": ["sales", "inventory", "returns"]},
             "start_date":  {"type": "string"},
             "end_date":    {"type": "string"},
         },
         "required": ["report_type", "start_date", "end_date"],
     }},
]

TOOL_MAP = {"get_order": get_order, "update_inventory": update_inventory, "generate_report": generate_report}

def run_agent(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]
    while True:
        response = client.messages.create(
            model="claude-sonnet-4-6", max_tokens=512, system=SYSTEM, tools=TOOLS, messages=messages
        )
        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if hasattr(b, "text")), "")
        messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                fn = TOOL_MAP.get(block.name)
                result = fn(**block.input) if fn else json.dumps({"error": "unknown"})
                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})
        messages.append({"role": "user", "content": tool_results})

scenarios = [
    "Get order ORD-88421.",                           # Valid — ID provided
    "Get the order.",                                  # Missing ID — should ask
    "Generate a sales report.",                        # Missing dates — should ask
    "Update inventory for SKU-X44 to 100 in WH-East", # All args present
]
for s in scenarios:
    print(f"\nUser: {s}")
    print(f"Agent: {run_agent(s)[:120]}...")
```

**Expected Token Savings:** 5–15% — prevents wrong-tool calls from requiring additional turns
**Environment:** `pip install anthropic`

---

### Option 5 — Tool Call Inspector with Argument Tracing

Log every tool call with full argument inspection. When null arguments are detected in the log, flag them without blocking — useful for auditing and gradual improvement.

```python
import json
import logging
import anthropic
from typing import Any
from dataclasses import dataclass

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("tool_inspector")

client = anthropic.Anthropic()

NULL_LIKE_STRINGS = {"null", "none", "undefined", "n/a", "unknown", "", "nil"}

@dataclass
class ArgumentReport:
    tool_name: str
    null_fields: list[str]
    suspicious_fields: list[str]   # Non-null but potentially wrong (too short, wrong format)
    all_args: dict[str, Any]

    @property
    def has_issues(self) -> bool:
        return bool(self.null_fields or self.suspicious_fields)

def inspect_tool_args(tool_name: str, args: dict) -> ArgumentReport:
    null_fields = []
    suspicious = []

    for key, value in args.items():
        if value is None:
            null_fields.append(key)
        elif isinstance(value, str):
            if value.strip().lower() in NULL_LIKE_STRINGS:
                null_fields.append(key)
            elif len(value.strip()) < 2 and key.endswith(("_id", "_code", "_key")):
                suspicious.append(f"{key}={value!r} (suspiciously short ID)")
        elif isinstance(value, (list, dict)) and len(value) == 0:
            suspicious.append(f"{key}=[] (empty collection for required field)")

    return ArgumentReport(tool_name, null_fields, suspicious, args)

def inspected_execute(tool_name: str, args: dict, fn, block_on_null: bool = True) -> str:
    report = inspect_tool_args(tool_name, args)

    if report.null_fields:
        log_data = {
            "tool": tool_name,
            "null_fields": report.null_fields,
            "args": {k: repr(v) for k, v in args.items()},
        }
        logger.error(f"NULL_ARGS {json.dumps(log_data)}")
        if block_on_null:
            return json.dumps({
                "error": "null_required_arguments",
                "null_fields": report.null_fields,
                "agent_instruction": f"Missing values for {report.null_fields}. Ask the user to provide them.",
            })

    if report.suspicious_fields:
        logger.warning(f"SUSPICIOUS_ARGS tool={tool_name} issues={report.suspicious_fields}")

    try:
        return fn(**{k: v for k, v in args.items() if v is not None})
    except Exception as e:
        logger.error(f"TOOL_ERROR tool={tool_name} error={e}")
        return json.dumps({"error": str(e)})

# Tool implementations
def fetch_user(user_id: str, include_history: bool = False) -> str:
    return json.dumps({"user_id": user_id, "name": "Bob", "email": "bob@example.com"})

def post_message(channel_id: str, content: str, thread_id: str = None) -> str:
    return json.dumps({"posted": True, "channel": channel_id,
                       "thread": thread_id, "chars": len(content)})

TOOLS = [
    {"name": "fetch_user", "description": "Fetch user by ID.",
     "input_schema": {"type": "object",
                      "properties": {"user_id": {"type": "string"}, "include_history": {"type": "boolean"}},
                      "required": ["user_id"]}},
    {"name": "post_message", "description": "Post a message to a channel.",
     "input_schema": {"type": "object",
                      "properties": {"channel_id": {"type": "string"}, "content": {"type": "string"},
                                     "thread_id": {"type": "string"}},
                      "required": ["channel_id", "content"]}},
]
TOOL_MAP = {"fetch_user": fetch_user, "post_message": post_message}

def run_agent(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]
    while True:
        response = client.messages.create(
            model="claude-sonnet-4-6", max_tokens=512, tools=TOOLS, messages=messages
        )
        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if hasattr(b, "text")), "")
        messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                fn = TOOL_MAP.get(block.name)
                result = inspected_execute(block.name, block.input, fn) if fn else json.dumps({"error": "unknown"})
                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})
        messages.append({"role": "user", "content": tool_results})

print(run_agent("Fetch user USR-001 and post 'Hello world' to channel CH-42."))
print()
print(run_agent("Look up the user and send a message."))  # Missing values
```

**Expected Token Savings:** None — logging only; enables data-driven improvement of argument handling
**Environment:** `pip install anthropic`

---

### Option 6 — Extraction-Validation Pipeline Before Tool Dispatch

For agents that extract arguments from user text, add a dedicated extraction-validation step. Only dispatch the tool if extraction produced valid, non-null values.

```python
import json
import anthropic
from dataclasses import dataclass
from typing import Any, Optional

client = anthropic.Anthropic()

@dataclass
class ExtractionResult:
    extracted: dict[str, Any]
    missing: list[str]
    valid: bool

    def to_prompt_context(self) -> str:
        if self.valid:
            return f"Extracted values: {json.dumps(self.extracted)}"
        return (
            f"Extracted values: {json.dumps(self.extracted)}\n"
            f"Missing required values: {self.missing}\n"
            f"Ask the user for: {', '.join(self.missing)}"
        )

def extract_structured(
    user_text: str,
    required_fields: list[str],
    optional_fields: list[str] = None,
) -> ExtractionResult:
    """Use Haiku to extract structured values from user text."""
    optional_fields = optional_fields or []
    all_fields = required_fields + optional_fields

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=f"""Extract these fields from the user's message.
Return JSON with exactly these keys: {all_fields}.
Use null for any field not explicitly mentioned.
Do not invent or guess values.""",
        messages=[{"role": "user", "content": user_text}],
    )

    try:
        raw = response.content[0].text.strip()
        if "```" in raw:
            raw = raw.split("```")[1].lstrip("json").strip()
        extracted = json.loads(raw)
    except json.JSONDecodeError:
        extracted = {f: None for f in all_fields}

    missing = [f for f in required_fields if not extracted.get(f)]
    return ExtractionResult(
        extracted={k: v for k, v in extracted.items() if v is not None},
        missing=missing,
        valid=len(missing) == 0,
    )

# Tool implementations
def schedule_meeting(
    attendee_email: str,
    date: str,
    time: str,
    duration_minutes: int = 60,
    topic: str = "Meeting",
) -> str:
    return json.dumps({
        "scheduled": True,
        "attendee": attendee_email,
        "datetime": f"{date} {time}",
        "duration_min": duration_minutes,
        "topic": topic,
    })

TOOLS = [
    {"name": "schedule_meeting",
     "description": "Schedule a meeting. Requires attendee email, date, and time.",
     "input_schema": {
         "type": "object",
         "properties": {
             "attendee_email":    {"type": "string"},
             "date":              {"type": "string", "description": "YYYY-MM-DD"},
             "time":              {"type": "string", "description": "HH:MM"},
             "duration_minutes":  {"type": "integer"},
             "topic":             {"type": "string"},
         },
         "required": ["attendee_email", "date", "time"],
     }},
]

def run_agent_with_extraction(user_message: str) -> str:
    # Step 1: Extract and validate before handing to agent
    extraction = extract_structured(
        user_text=user_message,
        required_fields=["attendee_email", "date", "time"],
        optional_fields=["duration_minutes", "topic"],
    )

    print(f"[Extract] valid={extraction.valid}, extracted={extraction.extracted}, missing={extraction.missing}")

    if not extraction.valid:
        # Don't call agent with incomplete args — ask user directly
        return f"To schedule the meeting, I need: {', '.join(extraction.missing)}. Could you provide those?"

    # Step 2: Pass verified args to the agent
    augmented_message = f"{user_message}\n\n[Verified extracted values: {json.dumps(extraction.extracted)}]"
    messages = [{"role": "user", "content": augmented_message}]

    while True:
        response = client.messages.create(
            model="claude-sonnet-4-6", max_tokens=512, tools=TOOLS, messages=messages
        )
        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if hasattr(b, "text")), "")
        messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                result = schedule_meeting(**block.input)
                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})
        messages.append({"role": "user", "content": tool_results})

test_messages = [
    "Schedule a meeting with sarah@company.com on 2025-05-15 at 14:00 for 45 minutes about the Q2 roadmap.",
    "Set up a call with the team tomorrow afternoon.",   # Missing email, vague date/time
    "Book a meeting with bob@example.com.",              # Missing date and time
]
for msg in test_messages:
    print(f"\nUser: {msg}")
    print(f"Agent: {run_agent_with_extraction(msg)[:100]}...")
```

**Expected Token Savings:** 20–30% — invalid-arg requests handled before reaching Sonnet
**Environment:** `pip install anthropic`

---

## Comparison

| Option | Detection Point | Blocking | Best For |
|--------|----------------|---------|----------|
| Pre-Call Validator | Before tool execution | Yes | Quick retrofit to any agent |
| Null-Coalescing Defaults | At arg assembly | Optional | Tools with safe fallback defaults |
| Pydantic Schema | At validation time | Yes | Type-safe codebases |
| System Prompt Instructions | Model-level | Soft (model-enforced) | Fast fix — no code changes |
| Argument Inspector | At execution + logging | Optional | Monitoring and gradual improvement |
| Extraction-Validation Pipeline | Before agent sees args | Yes | High-stakes tools, complex extraction |

**Recommended starting point:** Option 4 (System Prompt Instructions) — add the null-argument policy to your system prompt today. Works immediately with no code changes and prevents the model from attempting calls with missing data. Add Option 1 or 3 as a hard guard for critical tools.
