---
layout: solution
title: "Agent Exposes Internal Error Details to Users — Stack Traces and System Internals Leak"
category: general
description: "When a tool call fails, database query errors, or an exception is raised, the agent forwards the raw error message to the user verbatim — including stack traces, SQL queries, internal API URLs, environment variable names, file paths, and service names. This leaks system architecture, aids attackers, and looks unprofessional. The fix is to intercept exceptions at every boundary and return sanitized user-facing messages."
tags: [security, error-handling, information-disclosure, stack-trace, logging, user-facing-errors, pii]
---

# Agent Exposes Internal Error Details to Users — Stack Traces and System Internals Leak

## Symptom
- User sees `psycopg2.errors.UndefinedTable: relation "user_orders_v2" does not exist`
- Tool error includes full Python traceback with file paths and line numbers
- API error message reveals internal service URL: `Failed to connect to internal-api.prod.acmecorp.internal:8432`
- Database error exposes schema: `Column "ssn" of type VARCHAR(9) violates constraint...`
- Error message includes environment name: `PROD_DB_PASSWORD not set in environment`
- Agent forwards raw `openai.APIConnectionError` or `anthropic.RateLimitError` to chat

## Root Cause
Tool implementations and agent orchestration code propagate exceptions without sanitizing them. The raw exception object, including its message, traceback, and any embedded context (SQL, URLs, variable names), gets serialized into the tool result and returned to the LLM, which then incorporates it into the user-visible response. The fix is to catch exceptions at tool boundaries, log the full details internally, and return only a sanitized message to the agent (and by extension, the user).

## Fix

### Option 1: Tool boundary error wrapper — catch and sanitize at every tool call

```python
import anthropic
import logging
import traceback
import uuid
from functools import wraps
from typing import Callable

logger = logging.getLogger(__name__)
client = anthropic.Anthropic()

def sanitize_tool_error(fn: Callable) -> Callable:
    """
    Decorator that catches all exceptions from a tool implementation,
    logs full details internally, and returns a sanitized error to the agent.
    """
    @wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            # Generate a correlation ID so support can find the full error:
            error_id = str(uuid.uuid4())[:8].upper()
            # Log full details internally — never sent to user:
            logger.error(
                f"Tool error [{error_id}]: {fn.__name__} "
                f"args={args!r} kwargs={kwargs!r}\n"
                f"{traceback.format_exc()}"
            )
            # Return only a safe, generic message to the agent:
            return {
                "error": True,
                "message": f"The operation could not be completed (ref: {error_id}). "
                           f"Please try again or contact support with reference {error_id}.",
                "user_facing": True
            }
    return wrapper


@sanitize_tool_error
def get_order(order_id: str) -> dict:
    """Fetch order from database — errors are sanitized before reaching agent."""
    import psycopg2  # type: ignore
    conn = psycopg2.connect("postgresql://prod-db:5432/orders")
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM orders WHERE id = %s", (order_id,))
    row = cursor.fetchone()
    if row is None:
        return {"found": False, "order_id": order_id}
    return {"found": True, "order": dict(zip([d[0] for d in cursor.description], row))}


@sanitize_tool_error
def send_notification(user_id: str, message: str) -> dict:
    """Send notification — connection errors won't reach the user."""
    import httpx
    response = httpx.post(
        "http://notification-service.internal:8080/send",
        json={"user_id": user_id, "message": message},
        timeout=5.0
    )
    response.raise_for_status()
    return {"sent": True}


TOOLS = [
    {
        "name": "get_order",
        "description": "Get order details by ID",
        "input_schema": {
            "type": "object",
            "properties": {"order_id": {"type": "string"}},
            "required": ["order_id"]
        }
    }
]


def run_agent(user_message: str) -> str:
    import json
    messages = [{"role": "user", "content": user_message}]

    while True:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            tools=TOOLS,
            messages=messages
        )

        if response.stop_reason == "end_turn":
            return response.content[0].text

        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                if block.name == "get_order":
                    result = get_order(block.input["order_id"])
                else:
                    result = {"error": True, "message": "Unknown tool"}
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result)
                })

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})
```

### Option 2: Error category classification — different sanitization by error type

```python
import anthropic
import logging
import traceback
import uuid
import re

logger = logging.getLogger(__name__)
client = anthropic.Anthropic()

# User-safe messages for each category of internal error:
ERROR_MESSAGES = {
    "database": "There was a problem retrieving data. Please try again.",
    "network": "The service is temporarily unavailable. Please try again in a moment.",
    "auth": "Authentication failed. Please check your credentials and try again.",
    "not_found": "The requested resource was not found.",
    "rate_limit": "Too many requests. Please wait a moment before trying again.",
    "timeout": "The operation timed out. Please try again.",
    "validation": "The request contained invalid data. Please check your input.",
    "permission": "You don't have permission to perform this action.",
    "default": "An unexpected error occurred. Please try again."
}

# Patterns that reveal internal details — must never reach users:
SENSITIVE_PATTERNS = [
    r"password",
    r"secret",
    r"token",
    r"api[_-]?key",
    r"\.internal",
    r"127\.0\.0\.",
    r"192\.168\.",
    r"10\.\d+\.\d+\.",
    r"stack\s*trace",
    r"traceback",
    r"file\s+\".+\.py\"",
    r"line\s+\d+",
    r"psycopg2",
    r"sqlalchemy",
    r"connectionstring",
    r"dsn=",
]


def classify_error(exception: Exception) -> str:
    """Classify an exception into a safe category."""
    exc_type = type(exception).__name__.lower()
    exc_msg = str(exception).lower()

    if any(kw in exc_type or kw in exc_msg for kw in ["connection", "timeout", "network", "socket"]):
        return "network"
    if any(kw in exc_type or kw in exc_msg for kw in ["auth", "unauthorized", "forbidden", "403", "401"]):
        return "auth"
    if any(kw in exc_type or kw in exc_msg for kw in ["notfound", "404", "does not exist", "no such"]):
        return "not_found"
    if any(kw in exc_type or kw in exc_msg for kw in ["ratelimit", "429", "too many"]):
        return "rate_limit"
    if any(kw in exc_type or kw in exc_msg for kw in ["timeout", "timedout", "timed out"]):
        return "timeout"
    if any(kw in exc_type or kw in exc_msg for kw in ["validation", "invalid", "malformed"]):
        return "validation"
    if any(kw in exc_type or kw in exc_msg for kw in ["permission", "access denied", "acl"]):
        return "permission"
    if any(kw in exc_type or kw in exc_msg for kw in ["psycopg", "sqlalchemy", "mysql", "sqlite", "db"]):
        return "database"
    return "default"


def is_safe_message(message: str) -> bool:
    """Check if an error message is safe to show to users."""
    msg_lower = message.lower()
    return not any(re.search(p, msg_lower) for p in SENSITIVE_PATTERNS)


def safe_tool_error(exception: Exception, tool_name: str) -> dict:
    """Convert an exception into a safe tool result."""
    error_id = str(uuid.uuid4())[:8].upper()
    category = classify_error(exception)
    user_message = ERROR_MESSAGES[category]

    logger.error(
        f"[{error_id}] Tool '{tool_name}' failed (category={category}):\n"
        f"{traceback.format_exc()}"
    )

    return {
        "error": True,
        "category": category,
        "message": f"{user_message} (ref: {error_id})",
        "error_id": error_id
    }


def call_tool_safely(tool_name: str, tool_fn, *args, **kwargs) -> dict:
    try:
        result = tool_fn(*args, **kwargs)
        # Even on success, verify the result doesn't contain sensitive data:
        result_str = str(result)
        for pattern in SENSITIVE_PATTERNS:
            if re.search(pattern, result_str, re.IGNORECASE):
                logger.warning(f"Sensitive pattern '{pattern}' found in tool result from '{tool_name}'")
                # Sanitize by returning a limited version
                return {"result": "Data retrieved successfully", "sanitized": True}
        return result
    except Exception as e:
        return safe_tool_error(e, tool_name)
```

### Option 3: Anthropic SDK error handling — sanitize SDK-level errors

```python
import anthropic
import logging
import uuid

logger = logging.getLogger(__name__)

def create_safe_client() -> anthropic.Anthropic:
    return anthropic.Anthropic(max_retries=2)

client = create_safe_client()


def safe_api_call(messages: list[dict], system: str = "", **kwargs) -> str:
    """
    Wrapper around client.messages.create that catches SDK errors and
    returns user-safe messages instead of raw SDK exception details.
    """
    error_id = str(uuid.uuid4())[:8].upper()
    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=system,
            messages=messages,
            **kwargs
        )
        return response.content[0].text

    except anthropic.RateLimitError as e:
        logger.warning(f"[{error_id}] Rate limit hit: {e}")
        return "I'm receiving too many requests right now. Please try again in a moment."

    except anthropic.APIConnectionError as e:
        logger.error(f"[{error_id}] Connection error: {e}")
        return "I'm having trouble connecting to my services. Please try again shortly."

    except anthropic.AuthenticationError as e:
        logger.critical(f"[{error_id}] Auth error (check API key): {e}")
        return "There's a configuration issue. Please contact support."

    except anthropic.BadRequestError as e:
        logger.error(f"[{error_id}] Bad request: {e}")
        return "I wasn't able to process that request. Could you rephrase it?"

    except anthropic.APITimeoutError as e:
        logger.error(f"[{error_id}] Timeout: {e}")
        return "The request took too long to complete. Please try again."

    except anthropic.APIError as e:
        # Catch-all for any other Anthropic API error:
        logger.error(f"[{error_id}] Unexpected API error (status={e.status_code}): {e}")
        return f"An unexpected error occurred (ref: {error_id}). Please try again."

    except Exception as e:
        logger.error(f"[{error_id}] Unhandled exception: {e}", exc_info=True)
        return f"Something went wrong (ref: {error_id}). Please contact support."
```

### Option 4: Error response audit — scan agent outputs for leaked internals

```python
import anthropic
import re
import logging

logger = logging.getLogger(__name__)
client = anthropic.Anthropic()

# Patterns in agent responses that indicate internal error leakage:
LEAK_PATTERNS = [
    (r"Traceback \(most recent call last\)", "python_traceback"),
    (r"File \".*\.py\", line \d+", "file_path"),
    (r"raise \w+Error", "exception_raise"),
    (r"psycopg2\.", "db_driver"),
    (r"sqlalchemy\.", "orm_name"),
    (r"\b(localhost|127\.0\.0\.1|0\.0\.0\.0)\b", "localhost_ref"),
    (r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}:\d{4,5}", "internal_ip_port"),
    (r"\.internal\b", "internal_domain"),
    (r"(DB_PASSWORD|API_KEY|SECRET_KEY|DATABASE_URL)\s*=", "env_var_value"),
    (r"password[\s:=]+\S+", "password_value"),
    (r"Bearer [A-Za-z0-9\-_\.]+", "bearer_token"),
    (r"[A-Za-z0-9]{32,}", "possible_secret"),   # long alphanumeric strings
]

SAFE_FALLBACK = (
    "I encountered an issue processing your request. "
    "Please try again or contact support if the problem persists."
)


def audit_response(response_text: str) -> tuple[str, list[str]]:
    """
    Scan a response for patterns that indicate internal error leakage.
    Returns (safe_response, list_of_detected_leaks).
    """
    detected = []
    for pattern, category in LEAK_PATTERNS:
        if re.search(pattern, response_text, re.IGNORECASE):
            detected.append(category)

    if detected:
        logger.warning(f"Internal details leaked in response: {detected}")
        return SAFE_FALLBACK, detected

    return response_text, []


def agent_with_output_audit(user_message: str) -> str:
    """Agent that audits its own output before returning to the user."""
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=[{"role": "user", "content": user_message}]
    )
    raw_response = response.content[0].text
    safe_response, leaks = audit_response(raw_response)

    if leaks:
        logger.error(
            f"LEAK DETECTED in agent response: categories={leaks}\n"
            f"Original response (NOT shown to user):\n{raw_response}"
        )

    return safe_response
```

### Option 5: Structured error response standard — consistent error shape

```python
import anthropic
import uuid
import time
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)
client = anthropic.Anthropic()

@dataclass
class SafeError:
    """
    A structured error that is safe to return to users.
    Contains only what users need — nothing internal.
    """
    code: str           # machine-readable, for client handling
    message: str        # human-readable, safe for display
    error_id: str       # correlation ID for support lookups
    retryable: bool     # whether the user can retry

    def to_dict(self) -> dict:
        return {
            "error": True,
            "code": self.code,
            "message": self.message,
            "error_id": self.error_id,
            "retryable": self.retryable
        }

    @classmethod
    def transient(cls, context: str = "") -> "SafeError":
        return cls(
            code="SERVICE_UNAVAILABLE",
            message=f"Service temporarily unavailable{f' ({context})' if context else ''}. Please try again.",
            error_id=str(uuid.uuid4())[:8].upper(),
            retryable=True
        )

    @classmethod
    def not_found(cls, resource: str = "resource") -> "SafeError":
        return cls(
            code="NOT_FOUND",
            message=f"The requested {resource} was not found.",
            error_id=str(uuid.uuid4())[:8].upper(),
            retryable=False
        )

    @classmethod
    def invalid_input(cls, hint: str = "") -> "SafeError":
        return cls(
            code="INVALID_INPUT",
            message=f"Invalid input{f': {hint}' if hint else ''}. Please check and try again.",
            error_id=str(uuid.uuid4())[:8].upper(),
            retryable=False
        )

    @classmethod
    def unexpected(cls) -> "SafeError":
        eid = str(uuid.uuid4())[:8].upper()
        return cls(
            code="INTERNAL_ERROR",
            message=f"An unexpected error occurred (ref: {eid}). Please contact support.",
            error_id=eid,
            retryable=True
        )


# All tool implementations return SafeError on failure:
def lookup_customer(customer_id: str) -> dict:
    if not customer_id.startswith("CUS"):
        return SafeError.invalid_input("Customer ID must start with 'CUS'").to_dict()
    try:
        # ... database call ...
        raise ConnectionError("db.prod.internal:5432 refused connection")
    except ConnectionError as e:
        logger.error(f"DB connection failed for customer {customer_id!r}: {e}")
        return SafeError.transient("customer lookup").to_dict()
    except Exception as e:
        logger.error(f"Unexpected error in lookup_customer: {e}", exc_info=True)
        return SafeError.unexpected().to_dict()
```

### Option 6: Global exception handler for web framework integration

```python
import anthropic
import logging
import uuid
import traceback
from typing import Any

logger = logging.getLogger(__name__)
client = anthropic.Anthropic()

# FastAPI / Flask global exception handler approach.
# Catches all unhandled exceptions at the HTTP boundary.

# --- FastAPI version ---
# from fastapi import FastAPI, Request
# from fastapi.responses import JSONResponse
#
# app = FastAPI()
#
# @app.exception_handler(Exception)
# async def global_exception_handler(request: Request, exc: Exception):
#     error_id = str(uuid.uuid4())[:8].upper()
#     logger.error(
#         f"[{error_id}] Unhandled exception for {request.method} {request.url}\n"
#         f"{traceback.format_exc()}"
#     )
#     return JSONResponse(
#         status_code=500,
#         content={
#             "error": "An unexpected error occurred.",
#             "error_id": error_id,
#             "support": "Include this ID when contacting support."
#         }
#     )

# --- Standalone agent error boundary ---
class AgentErrorBoundary:
    """
    Wraps agent execution with a global error boundary.
    Any unhandled exception returns a safe user message.
    """
    def __init__(self, agent_fn):
        self._agent_fn = agent_fn

    def __call__(self, *args, **kwargs) -> Any:
        error_id = str(uuid.uuid4())[:8].upper()
        try:
            return self._agent_fn(*args, **kwargs)
        except Exception:
            logger.error(
                f"[{error_id}] Unhandled exception in agent:\n"
                f"{traceback.format_exc()}"
            )
            return (
                f"I encountered an unexpected problem and couldn't complete your request. "
                f"Please try again. If the problem persists, contact support (ref: {error_id})."
            )


def my_raw_agent(user_message: str) -> str:
    """Agent that might raise exceptions."""
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=[{"role": "user", "content": user_message}]
    )
    return response.content[0].text


# Wrap with error boundary — never exposes internals:
safe_agent = AgentErrorBoundary(my_raw_agent)
result = safe_agent("Hello, how are you?")
```

## Error Information Exposure Risk Matrix

| Data Leaked | Risk | Example |
|---|---|---|
| Stack trace with file paths | High — reveals code structure | `File "/app/src/db/orders.py", line 47` |
| Database errors | High — reveals schema, DB type | `relation "user_pii" does not exist` |
| Internal hostnames/IPs | High — network topology | `db.prod.internal:5432` |
| Environment variable names | High — reveals config keys | `DATABASE_URL not set` |
| API keys in errors | Critical — direct credential leak | `invalid_api_key: sk-...` |
| Service names | Medium — architecture disclosure | `Failed to connect to auth-service` |
| Generic error codes | Low — acceptable | `Service unavailable (ref: A1B2C3)` |

## Expected Token Savings
No direct token impact. However, leaked stack traces can add 200–2000 tokens of internal detail to the conversation context — wasting context window and potentially influencing subsequent model behavior.

## Environment
- All production agents; any agent where tool results or LLM responses may contain exception information; mandatory for customer-facing agents; the decorator approach (Option 1) is the lowest-friction fix for existing tools; the audit approach (Option 4) is a safety net that catches leaks that got through the primary defenses; combine Options 1 + 3 + 4 for defense-in-depth
