---
layout: solution
title: "Agent Exposes Internal Errors to Users"
category: general
description: "Agent forwards raw stack traces, database errors, API keys fragments, or internal system details to users when exceptions occur — leaking sensitive information and damaging trust."
tags: [general, error-handling, security, logging, user-experience, observability]
---

## Symptom

User receives:

```
Error: psycopg2.OperationalError: FATAL: password authentication failed
for user "prod_db_admin" at host "db-prod-01.internal.company.com"
Connection string: postgresql://prod_db_admin:s3cr3tP@ss@db-prod-01.internal:5432/orders
```

Or:
```
anthropic.APIStatusError: {"type":"error","error":{"type":"authentication_error",
"message":"invalid x-api-key"},"request_id":"req_01XYZ"}
```

Internal hostnames, credentials, stack traces, and API details become visible to end users.

## Root Cause

Exceptions propagate from tool functions directly into the agent's tool result — which is then summarised and forwarded to the user. The agent has no error sanitisation layer between internal failures and user-facing output.

## Fix

---

### Option 1 — Safe Error Wrapper for All Tool Functions

Wrap every tool function in a decorator that catches exceptions, logs the full details internally, and returns only a sanitised user-safe message.

```python
import json
import uuid
import logging
import traceback
import anthropic
from functools import wraps
from typing import Callable

logger = logging.getLogger("agent.tools")
logging.basicConfig(level=logging.INFO)

def safe_tool(user_message: str = "An error occurred. Please try again later."):
    """
    Decorator: catches all exceptions from tool functions.
    - Logs full details internally (including stack trace)
    - Returns a sanitised message to the agent (and hence the user)
    """
    def decorator(fn: Callable) -> Callable:
        @wraps(fn)
        def wrapper(*args, **kwargs) -> str:
            error_id = str(uuid.uuid4())[:8].upper()
            try:
                result = fn(*args, **kwargs)
                # Ensure result is always a JSON string
                if isinstance(result, str):
                    return result
                return json.dumps(result)
            except Exception as e:
                # Log everything internally
                logger.error(
                    "Tool error [%s] in %s: %s\nArgs: %s\nTrace:\n%s",
                    error_id,
                    fn.__name__,
                    str(e),
                    {"args": args, "kwargs": kwargs},
                    traceback.format_exc(),
                )
                # Return only safe, generic message to agent/user
                return json.dumps({
                    "error": True,
                    "message": user_message,
                    "error_id": error_id,
                    "hint": "If this persists, contact support with error ID: " + error_id,
                })
        return wrapper
    return decorator

# Database tool — connection string stays internal
@safe_tool("Unable to retrieve user data. Please try again.")
def get_user_data(user_id: str) -> dict:
    # Simulate a database error with sensitive connection info
    raise ConnectionError(
        "FATAL: password authentication failed for user 'prod_admin' "
        "at postgresql://prod_admin:s3cr3t@db-01.internal.company.com:5432/users"
    )

@safe_tool("Search is temporarily unavailable.")
def search_products(query: str) -> list:
    # Simulate API key leaking in error
    raise PermissionError(
        "Invalid API key: sk-live-abc123def456. "
        "Please check your SEARCH_API_KEY environment variable."
    )

client = anthropic.Anthropic()

TOOLS = [
    {
        "name": "get_user_data",
        "description": "Retrieve user profile information.",
        "input_schema": {
            "type": "object",
            "properties": {"user_id": {"type": "string"}},
            "required": ["user_id"],
        },
    },
    {
        "name": "search_products",
        "description": "Search the product catalogue.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
]

TOOL_FNS = {"get_user_data": get_user_data, "search_products": search_products}

def run_agent(task: str) -> str:
    messages = [{"role": "user", "content": task}]

    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system="You are a helpful assistant. When tools return errors, relay the error message to the user clearly.",
            tools=TOOLS,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if b.type == "text"), "")

        messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                fn = TOOL_FNS[block.name]
                result = fn(**block.input)
                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})
        messages.append({"role": "user", "content": tool_results})

# Database error — user only sees safe message, not credentials
print(run_agent("Get data for user u-42."))
# Search error — API key stays internal
print(run_agent("Search for widgets."))
```

**Expected Token Savings:** None — security fix; prevents credential leakage
**Environment:** `pip install anthropic`

---

### Option 2 — Error Classification: Retryable vs Fatal vs User Error

Classify exceptions into categories. Retryable errors trigger a retry; user errors return a helpful hint; fatal/internal errors log and return a generic message.

```python
import json
import uuid
import time
import logging
import anthropic

logger = logging.getLogger("agent")

class UserError(Exception):
    """Error caused by bad user input — safe to show details."""
    pass

class RetryableError(Exception):
    """Transient error — retry may succeed."""
    pass

class InternalError(Exception):
    """Internal system error — never show details to users."""
    pass

def classify_exception(e: Exception) -> str:
    """Classify an exception into a category."""
    if isinstance(e, UserError):
        return "user_error"
    if isinstance(e, RetryableError):
        return "retryable"
    if isinstance(e, (ConnectionError, TimeoutError, OSError)):
        return "retryable"
    if isinstance(e, (ValueError, TypeError)) and "invalid" in str(e).lower():
        return "user_error"
    return "internal"

def classified_tool_call(
    fn,
    args: dict,
    max_retries: int = 2,
    base_delay: float = 1.0,
) -> str:
    error_id = str(uuid.uuid4())[:8].upper()

    for attempt in range(max_retries + 1):
        try:
            result = fn(**args)
            return json.dumps(result) if not isinstance(result, str) else result

        except Exception as e:
            category = classify_exception(e)

            if category == "user_error":
                return json.dumps({
                    "error": "user_error",
                    "message": str(e),  # Safe to surface user errors
                })

            if category == "retryable" and attempt < max_retries:
                delay = base_delay * (2 ** attempt)
                logger.warning(f"Retryable error in {fn.__name__}, retry {attempt + 1}: {e}")
                time.sleep(delay)
                continue

            # Internal or exhausted retries — log fully, return generic message
            logger.error(
                f"Tool error [{error_id}] in {fn.__name__} (attempt {attempt + 1}): {e}",
                exc_info=True,
            )
            return json.dumps({
                "error": "internal",
                "message": "Service temporarily unavailable. Please try again.",
                "error_id": error_id,
            })

    return json.dumps({"error": "internal", "message": "Max retries exceeded.", "error_id": error_id})

def lookup_order(order_id: str) -> dict:
    if not order_id.startswith("ORD-"):
        raise UserError(f"Invalid order ID format: {order_id!r}. Expected format: ORD-XXXXX")
    if order_id == "ORD-99999":
        raise ConnectionError("DB connection pool exhausted (internal)")
    return {"order_id": order_id, "status": "shipped", "items": 3}

client = anthropic.Anthropic()

TOOLS = [{
    "name": "lookup_order",
    "description": "Look up an order by ID. Format: ORD-XXXXX",
    "input_schema": {
        "type": "object",
        "properties": {"order_id": {"type": "string"}},
        "required": ["order_id"],
    },
}]

def run_agent(task: str) -> str:
    messages = [{"role": "user", "content": task}]

    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            tools=TOOLS,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if b.type == "text"), "")

        messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                result = classified_tool_call(lookup_order, block.input)
                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})
        messages.append({"role": "user", "content": tool_results})

print(run_agent("Look up order ORD-12345."))         # Success
print(run_agent("Look up order 12345."))              # User error (safe to show)
print(run_agent("Look up order ORD-99999."))          # Internal error (sanitised)
```

**Expected Token Savings:** None — security + reliability fix
**Environment:** `pip install anthropic`

---

### Option 3 — Error Response Template with Escalation Path

Return structured error responses using a template that gives users a clear action (retry, contact support, rephrase) without revealing internals.

```python
import json
import uuid
import anthropic

ERROR_TEMPLATES = {
    "not_found": {
        "message": "The requested {resource} was not found.",
        "action": "Please verify the {resource} ID and try again.",
        "severity": "info",
    },
    "permission_denied": {
        "message": "You don't have permission to access this {resource}.",
        "action": "Contact your administrator to request access.",
        "severity": "warning",
    },
    "service_unavailable": {
        "message": "This service is temporarily unavailable.",
        "action": "Please try again in a few minutes. If the issue persists, contact support with error ID: {error_id}",
        "severity": "error",
    },
    "rate_limited": {
        "message": "Too many requests. Please slow down.",
        "action": "Wait {wait_seconds} seconds before trying again.",
        "severity": "warning",
    },
    "invalid_input": {
        "message": "Invalid input: {detail}",
        "action": "Please correct your input and try again.",
        "severity": "info",
    },
}

def build_error_response(template_key: str, **kwargs) -> str:
    template = ERROR_TEMPLATES.get(template_key, ERROR_TEMPLATES["service_unavailable"])
    error_id = str(uuid.uuid4())[:8].upper()

    message = template["message"].format(**kwargs, error_id=error_id)
    action = template["action"].format(**kwargs, error_id=error_id)

    return json.dumps({
        "error": True,
        "severity": template["severity"],
        "message": message,
        "recommended_action": action,
        "error_id": error_id,
    })

def get_document(doc_id: str, user_id: str) -> str:
    if not doc_id.startswith("DOC-"):
        return build_error_response("invalid_input", detail=f"Doc ID must start with DOC-, got {doc_id!r}")
    if doc_id == "DOC-PRIVATE":
        return build_error_response("permission_denied", resource="document")
    if doc_id == "DOC-MISSING":
        return build_error_response("not_found", resource="document")
    if doc_id == "DOC-ERROR":
        # Log internally
        import logging
        logging.error(f"Internal error fetching {doc_id} for {user_id}: DB connection failed")
        return build_error_response("service_unavailable", error_id="already-set")

    return json.dumps({
        "doc_id": doc_id,
        "title": "Sample Document",
        "content": "This is the document content.",
    })

client = anthropic.Anthropic()

TOOLS = [{
    "name": "get_document",
    "description": "Retrieve a document by ID.",
    "input_schema": {
        "type": "object",
        "properties": {
            "doc_id": {"type": "string"},
            "user_id": {"type": "string"},
        },
        "required": ["doc_id", "user_id"],
    },
}]

def run_agent(task: str) -> str:
    messages = [{"role": "user", "content": task}]

    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            system="Relay error messages and recommended actions clearly to users.",
            tools=TOOLS,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if b.type == "text"), "")

        messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                result = get_document(**block.input)
                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})
        messages.append({"role": "user", "content": tool_results})

for q, doc in [
    ("Get document DOC-123 for user u-1.", "DOC-123"),
    ("Get document DOC-PRIVATE for user u-1.", "DOC-PRIVATE"),
    ("Get document DOC-MISSING for user u-1.", "DOC-MISSING"),
    ("Get document 123 for user u-1.", "123"),
]:
    print(f"\nQ: {q}")
    print(f"A: {run_agent(q)[:120]}...")
```

**Expected Token Savings:** None — UX + security; templated errors are predictable
**Environment:** `pip install anthropic`

---

### Option 4 — Centralized Error Boundary with Sentry-Style Capture

Route all tool errors through a central error boundary. Capture to an error-tracking service (Sentry, Datadog) while returning safe user messages. Include request context for debugging.

```python
import json
import uuid
import time
import traceback
import logging
import anthropic
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("agent.errors")

@dataclass
class ErrorCapture:
    error_id: str
    timestamp: float
    tool_name: str
    tool_args: dict
    exception_type: str
    exception_message: str
    stack_trace: str
    session_id: str
    context: dict = field(default_factory=dict)

# In production: replace with Sentry, Datadog, etc.
_error_log: list[ErrorCapture] = []

def capture_error(
    tool_name: str,
    tool_args: dict,
    exception: Exception,
    session_id: str,
    context: dict | None = None,
) -> str:
    error_id = f"ERR-{str(uuid.uuid4())[:8].upper()}"
    capture = ErrorCapture(
        error_id=error_id,
        timestamp=time.time(),
        tool_name=tool_name,
        tool_args={k: "***REDACTED***" if "key" in k.lower() or "token" in k.lower() or "password" in k.lower() else v
                   for k, v in tool_args.items()},
        exception_type=type(exception).__name__,
        exception_message=str(exception),
        stack_trace=traceback.format_exc(),
        session_id=session_id,
        context=context or {},
    )
    _error_log.append(capture)
    logger.error(f"[{error_id}] {tool_name} failed: {type(exception).__name__}: {exception}")
    # In production: sentry_sdk.capture_exception(exception, extra=capture.__dict__)
    return error_id

def safe_execute(
    tool_name: str,
    fn,
    args: dict,
    session_id: str,
    user_message: str = "Something went wrong. Please try again.",
) -> str:
    try:
        result = fn(**args)
        return json.dumps(result) if not isinstance(result, str) else result
    except Exception as e:
        error_id = capture_error(tool_name, args, e, session_id)
        return json.dumps({
            "error": True,
            "message": user_message,
            "support_reference": error_id,
        })

# Tool implementations
def send_payment(amount: float, account_number: str, routing_number: str) -> dict:
    # Simulate internal error with sensitive payment data
    raise RuntimeError(
        f"ACH transfer failed: account {account_number}, routing {routing_number}, "
        "bank returned: ACCOUNT_FROZEN. Internal error code: ACH-7723"
    )

def get_analytics(metric: str, api_key: str) -> dict:
    raise ConnectionError(f"Failed to connect: api_key={api_key}")

client = anthropic.Anthropic()
SESSION_ID = str(uuid.uuid4())

TOOLS = [
    {
        "name": "send_payment",
        "description": "Process a payment transfer.",
        "input_schema": {
            "type": "object",
            "properties": {
                "amount": {"type": "number"},
                "account_number": {"type": "string"},
                "routing_number": {"type": "string"},
            },
            "required": ["amount", "account_number", "routing_number"],
        },
    },
]

def run_agent(task: str) -> str:
    messages = [{"role": "user", "content": task}]

    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            tools=TOOLS,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if b.type == "text"), "")

        messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                result = safe_execute(block.name, send_payment, block.input, SESSION_ID,
                                      "Payment processing failed. Please contact support.")
                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})
        messages.append({"role": "user", "content": tool_results})

result = run_agent("Send $500 to account 12345678 routing 987654321.")
print(f"User sees: {result}")
print(f"\nInternal error log ({len(_error_log)} captured):")
for e in _error_log:
    print(f"  [{e.error_id}] {e.tool_name}: {e.exception_type}")
    print(f"  Account in log: {e.tool_args}")  # Confirm sensitive data is redacted
```

**Expected Token Savings:** None — security + observability; errors captured with context, users see only safe messages
**Environment:** `pip install anthropic`

---

### Option 5 — Environment-Aware Error Verbosity

Show detailed errors in development, sanitised errors in production. Configuration-driven verbosity level prevents accidentally shipping debug errors to users.

```python
import json
import uuid
import os
import traceback
import anthropic

ENVIRONMENT = os.getenv("ENVIRONMENT", "development")
IS_PRODUCTION = ENVIRONMENT == "production"

def format_error(
    exception: Exception,
    tool_name: str,
    user_facing_message: str = "An error occurred.",
) -> str:
    error_id = str(uuid.uuid4())[:8].upper()

    if not IS_PRODUCTION:
        # Development: full details for debugging
        return json.dumps({
            "error": True,
            "environment": ENVIRONMENT,
            "error_id": error_id,
            "exception_type": type(exception).__name__,
            "exception_message": str(exception),
            "tool": tool_name,
            "stack_trace": traceback.format_exc(),
        })

    # Production: sanitised only
    return json.dumps({
        "error": True,
        "message": user_facing_message,
        "error_id": error_id,
    })

def process_data(data_id: str) -> dict:
    if data_id == "BAD":
        raise ValueError(
            "Failed to parse data from internal-service://data-processor:8080/api/v2/process"
            " — Connection refused (internal hostname)"
        )
    return {"data_id": data_id, "processed": True}

client = anthropic.Anthropic()

TOOLS = [{
    "name": "process_data",
    "description": "Process a data record.",
    "input_schema": {
        "type": "object",
        "properties": {"data_id": {"type": "string"}},
        "required": ["data_id"],
    },
}]

def run_agent(task: str) -> str:
    messages = [{"role": "user", "content": task}]

    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            tools=TOOLS,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if b.type == "text"), "")

        messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                try:
                    result = json.dumps(process_data(**block.input))
                except Exception as e:
                    result = format_error(e, block.name, "Data processing failed. Please try again.")
                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})
        messages.append({"role": "user", "content": tool_results})

print(f"Environment: {ENVIRONMENT}")
result = run_agent("Process record BAD.")
print(f"Response: {result[:200]}")
```

**Expected Token Savings:** None — environment-aware verbosity prevents accidental prod leaks
**Environment:** `pip install anthropic`

---

### Option 6 — System Prompt: Instruct Agent Not to Repeat Internal Errors

Add an explicit instruction in the system prompt so even if a raw error reaches the agent context, it knows not to repeat internal details to the user.

```python
import anthropic

client = anthropic.Anthropic()

SECURE_SYSTEM = """You are a helpful customer service assistant.

ERROR HANDLING INSTRUCTIONS:
- If a tool returns an error message, relay ONLY the user-facing summary to the user.
- NEVER repeat: stack traces, connection strings, database names, internal hostnames,
  API keys, passwords, internal error codes, file paths, or server addresses.
- If a tool error contains sensitive-looking text (URLs with credentials, "password=",
  "api_key=", "secret", internal hostnames ending in ".internal"), do NOT relay it.
- Instead, say: "I encountered an error completing that request. Please try again or
  contact support if the issue persists."
- You may share: error reference IDs (like ERR-ABC123), general descriptions
  ("the payment service is unavailable"), and suggested user actions."""

TOOLS = [{
    "name": "fetch_record",
    "description": "Fetch a database record.",
    "input_schema": {
        "type": "object",
        "properties": {"record_id": {"type": "string"}},
        "required": ["record_id"],
    },
}]

import json

def fetch_record(record_id: str) -> str:
    if record_id == "ERR":
        # Simulate an error that accidentally contains sensitive info
        return json.dumps({
            "error": True,
            "detail": (
                "DatabaseError: Access denied for user 'prod_user'@'db-prod-01.internal' "
                "to database 'orders_prod'. "
                "Connection: mysql://prod_user:Sup3rS3cr3t@db-prod-01.internal:3306/orders_prod"
            ),
        })
    return json.dumps({"record_id": record_id, "data": "some data"})

def run_agent(task: str) -> str:
    messages = [{"role": "user", "content": task}]

    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            system=SECURE_SYSTEM,
            tools=TOOLS,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if b.type == "text"), "")

        messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                result = fetch_record(**block.input)
                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})
        messages.append({"role": "user", "content": tool_results})

# Error with internal credentials — agent should not relay them
result = run_agent("Fetch record ERR.")
print(f"User sees: {result}")
# Should say something like "I encountered an error..." without the DB credentials
```

**Expected Token Savings:** None — defence-in-depth; last-line protection if sanitisation layer is missed
**Environment:** `pip install anthropic`

---

## Comparison

| Option | Protection Layer | Captures Internally | Auto-Retry | Best For |
|--------|----------------|---------------------|------------|----------|
| Safe Tool Decorator | Tool wrapper | Yes (logs) | No | All tools (always apply) |
| Error Classification | Tool wrapper | Yes | Yes (retryable) | Mixed error environments |
| Error Templates | Tool wrapper | No | No | UX-focused error messages |
| Central Error Boundary | Error boundary | Yes (structured) | No | Production observability |
| Environment-Aware | Config-driven | Partial | No | Dev/prod parity |
| System Prompt Guard | Prompt-level | No | No | Defence-in-depth |

**Recommended starting point:** Option 1 (Safe Tool Decorator) on every tool function immediately. Add Option 4 (Central Error Boundary) for production systems that need error tracking.
