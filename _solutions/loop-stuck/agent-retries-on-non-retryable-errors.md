---
layout: solution
title: "Agent Retries on Non-Retryable Errors"
category: loop-stuck
description: "Agent enters an infinite retry loop on errors like 400 Bad Request, 401 Unauthorized, or 404 Not Found that will never succeed regardless of how many times they are retried."
tags: [retry, error-handling, loop-stuck, 400, 404, reliability]
---

## Symptom

The agent burns through its retry budget — or loops indefinitely — on a request that will never succeed. Logs show the same 400 or 404 error repeated 5–10 times before the agent gives up or crashes. Token cost for the failed task is 5–10x what a single attempt would cost. In some cases the agent slightly modifies its request each retry (different wording, different parameters) but the fundamental error remains.

## Root Cause

The agent's retry logic treats all errors uniformly — it catches `Exception` or `httpx.HTTPError` and retries without inspecting the status code. HTTP errors divide into two categories: **transient** (5xx, 429, network timeouts — might succeed if retried) and **permanent** (4xx — will never succeed with the same request). Retrying a 400 Bad Request means the request is malformed; retrying a 401 means the credentials are wrong; retrying a 404 means the resource doesn't exist. None of these change on their own.

## Fix

### Option 1: Status-code-aware retry decorator

```python
import time
import anthropic
import httpx

# Errors that are worth retrying (transient)
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

# Errors that will never succeed — fail immediately
NON_RETRYABLE_STATUS_CODES = {400, 401, 403, 404, 405, 409, 410, 422, 451}


def retry_transient(max_attempts: int = 3, base_delay: float = 1.0):
    """
    Decorator that retries only on transient errors.
    Immediately raises on permanent (4xx) errors.
    """
    def decorator(func):
        def wrapper(*args, **kwargs):
            last_error = None
            for attempt in range(max_attempts):
                try:
                    return func(*args, **kwargs)
                except anthropic.RateLimitError as e:
                    # 429 — transient, retry with backoff
                    delay = base_delay * (2 ** attempt)
                    print(f"Rate limited (attempt {attempt+1}/{max_attempts}), retrying in {delay:.1f}s")
                    time.sleep(delay)
                    last_error = e
                except anthropic.APIStatusError as e:
                    status = e.status_code
                    if status in NON_RETRYABLE_STATUS_CODES:
                        # Permanent error — do NOT retry
                        raise RuntimeError(
                            f"Non-retryable error {status}: {e.message}. "
                            f"Fix the request before retrying."
                        ) from e
                    elif status in RETRYABLE_STATUS_CODES:
                        delay = base_delay * (2 ** attempt)
                        print(f"Server error {status} (attempt {attempt+1}/{max_attempts}), retrying in {delay:.1f}s")
                        time.sleep(delay)
                        last_error = e
                    else:
                        raise  # Unknown status — propagate
                except anthropic.APIConnectionError as e:
                    # Network error — transient, retry
                    delay = base_delay * (2 ** attempt)
                    print(f"Connection error (attempt {attempt+1}/{max_attempts}), retrying in {delay:.1f}s")
                    time.sleep(delay)
                    last_error = e

            raise RuntimeError(f"Max retries ({max_attempts}) exhausted") from last_error

        return wrapper
    return decorator


client = anthropic.Anthropic()


@retry_transient(max_attempts=3, base_delay=1.0)
def call_claude(messages: list[dict]) -> str:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=messages,
    )
    return response.content[0].text


try:
    result = call_claude([{"role": "user", "content": "Hello"}])
    print(result)
except RuntimeError as e:
    print(f"Failed: {e}")
```

**Expected Token Savings:** Eliminates 2–9 wasted retry calls per permanent error — each retry is a full API round-trip.
**Environment:** Python 3.9+; works with the Anthropic SDK's built-in exception hierarchy.

---

### Option 2: Classify tool call errors before retrying

```python
import anthropic

client = anthropic.Anthropic()

# Classify HTTP-like errors from tool results
def classify_tool_error(error_message: str) -> str:
    """
    Determine if a tool error is retryable.
    Returns: "retryable", "non_retryable", or "unknown"
    """
    error_lower = error_message.lower()

    # Non-retryable patterns — permanent failures
    non_retryable_patterns = [
        "404", "not found",
        "400", "bad request", "invalid", "malformed",
        "401", "unauthorized", "authentication failed", "invalid api key",
        "403", "forbidden", "permission denied", "access denied",
        "405", "method not allowed",
        "409", "conflict", "already exists",
        "410", "gone", "permanently deleted",
        "422", "unprocessable", "validation error",
    ]

    # Retryable patterns — transient failures
    retryable_patterns = [
        "429", "rate limit", "too many requests",
        "500", "internal server error",
        "502", "bad gateway",
        "503", "service unavailable", "temporarily unavailable",
        "504", "gateway timeout",
        "timeout", "connection refused", "network error",
    ]

    for pattern in non_retryable_patterns:
        if pattern in error_lower:
            return "non_retryable"

    for pattern in retryable_patterns:
        if pattern in error_lower:
            return "retryable"

    return "unknown"


TOOLS = [
    {
        "name": "fetch_record",
        "description": "Fetch a record from the database by ID",
        "input_schema": {
            "type": "object",
            "properties": {
                "record_id": {"type": "string"},
                "record_type": {"type": "string", "enum": ["user", "order", "product"]},
            },
            "required": ["record_id", "record_type"],
        },
    }
]

SYSTEM = """You are a data retrieval agent.

<instructions>
- If a tool call fails, read the error message carefully.
- If the error indicates the record does not exist (404/not found), do NOT retry — report to the user.
- If the error indicates a temporary service issue (500/503/timeout), retry once.
- Never retry authentication errors (401/403).
</instructions>"""


def simulate_tool_call(record_id: str, record_type: str) -> str:
    """Simulate tool responses with different error types."""
    if record_id == "missing-id":
        return "ERROR 404: Record not found"
    if record_id == "bad-format":
        return "ERROR 400: Invalid record ID format — must be UUID"
    if record_id == "retry-me":
        return "ERROR 503: Service temporarily unavailable"
    return f'{{"id": "{record_id}", "type": "{record_type}", "data": "example data"}}'


def agent_loop_with_error_classification(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]
    retry_counts: dict[str, int] = {}
    MAX_RETRIES_PER_TOOL = 2

    while True:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            system=SYSTEM,
            tools=TOOLS,
            messages=messages,
        )

        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            return next(b.text for b in response.content if b.type == "text")

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue

            result = simulate_tool_call(
                block.input.get("record_id", ""),
                block.input.get("record_type", "user"),
            )

            if result.startswith("ERROR"):
                error_class = classify_tool_error(result)
                tool_key = f"{block.name}:{block.input.get('record_id')}"
                retry_counts[tool_key] = retry_counts.get(tool_key, 0) + 1

                if error_class == "non_retryable":
                    # Tell Claude this is permanent — don't let it retry
                    result = f"{result}\n[PERMANENT ERROR: Do not retry this request. Report the issue to the user.]"
                    print(f"Non-retryable error for {tool_key} — blocking retry")
                elif error_class == "retryable" and retry_counts[tool_key] > MAX_RETRIES_PER_TOOL:
                    result = f"{result}\n[MAX RETRIES REACHED: Stop retrying and report the issue.]"

            tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})

        messages.append({"role": "user", "content": tool_results})


print(agent_loop_with_error_classification("Fetch user record with ID 'missing-id'"))
```

**Expected Token Savings:** Each blocked retry saves one full agent turn (input + output tokens); prevents retry loops that can cost 5–20x the single-attempt price.
**Environment:** Python 3.9+; pattern-based classifier works without additional API calls.

---

### Option 3: Tenacity with retry predicate on status codes

```python
import anthropic
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
    RetryError,
    before_sleep_log,
)
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

client = anthropic.Anthropic()


def is_retryable_anthropic_error(exc: BaseException) -> bool:
    """
    Tenacity retry predicate — return True only for transient errors.
    Permanent errors (4xx except 429) return False, suppressing retry.
    """
    if isinstance(exc, anthropic.RateLimitError):
        return True  # 429 — retryable
    if isinstance(exc, anthropic.APIStatusError):
        # Only retry server-side errors
        return exc.status_code >= 500
    if isinstance(exc, anthropic.APIConnectionError):
        return True  # Network error — retryable
    if isinstance(exc, anthropic.APITimeoutError):
        return True  # Timeout — retryable
    return False  # All other errors (including 4xx) — not retryable


@retry(
    retry=retry_if_exception(is_retryable_anthropic_error),
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=1, min=1, max=30),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)
def call_claude_with_smart_retry(
    messages: list[dict],
    model: str = "claude-sonnet-4-6",
    max_tokens: int = 512,
) -> str:
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=messages,
    )
    return response.content[0].text


def safe_call(messages: list[dict]) -> str:
    try:
        return call_claude_with_smart_retry(messages)
    except anthropic.AuthenticationError as e:
        return f"Authentication failed — check your API key: {e}"
    except anthropic.BadRequestError as e:
        return f"Bad request — fix the message format: {e}"
    except anthropic.NotFoundError as e:
        return f"Resource not found: {e}"
    except RetryError as e:
        return f"Failed after retries (transient error): {e.last_attempt.exception()}"
    except anthropic.APIStatusError as e:
        return f"API error {e.status_code}: {e.message}"


# Test: permanent error — should NOT retry
print("Testing auth error (should fail immediately):")
result = safe_call([{"role": "user", "content": "Hello"}])
print(result[:200])

# Test: valid call — should succeed
print("\nTesting valid call:")
result = safe_call([{"role": "user", "content": "Say 'hello' in one word"}])
print(result)
```

**Expected Token Savings:** `tenacity` with a precise predicate retries only transient failures; bad requests fail on first attempt.
**Environment:** Python 3.9+; requires `tenacity` (`pip install tenacity`); composable with any Anthropic SDK call.

---

### Option 4: Error budget with non-retryable short-circuit

```python
import time
from dataclasses import dataclass, field
from enum import Enum, auto

import anthropic

client = anthropic.Anthropic()


class ErrorCategory(Enum):
    PERMANENT = auto()       # 4xx (not 429) — never retry
    RATE_LIMITED = auto()    # 429 — retry with backoff
    SERVER_ERROR = auto()    # 5xx — retry with backoff
    NETWORK = auto()         # Connection/timeout — retry
    UNKNOWN = auto()


@dataclass
class RetryBudget:
    """Tracks remaining retries by error category."""
    rate_limit_retries: int = 5
    server_error_retries: int = 3
    network_retries: int = 3
    _history: list[str] = field(default_factory=list)

    def can_retry(self, category: ErrorCategory) -> bool:
        if category == ErrorCategory.PERMANENT:
            return False  # Never
        if category == ErrorCategory.RATE_LIMITED:
            return self.rate_limit_retries > 0
        if category == ErrorCategory.SERVER_ERROR:
            return self.server_error_retries > 0
        if category == ErrorCategory.NETWORK:
            return self.network_retries > 0
        return False

    def consume(self, category: ErrorCategory) -> None:
        if category == ErrorCategory.RATE_LIMITED:
            self.rate_limit_retries -= 1
        elif category == ErrorCategory.SERVER_ERROR:
            self.server_error_retries -= 1
        elif category == ErrorCategory.NETWORK:
            self.network_retries -= 1
        self._history.append(category.name)


def categorize_error(exc: Exception) -> ErrorCategory:
    if isinstance(exc, anthropic.RateLimitError):
        return ErrorCategory.RATE_LIMITED
    if isinstance(exc, anthropic.APIStatusError):
        if 400 <= exc.status_code < 500:
            return ErrorCategory.PERMANENT
        if exc.status_code >= 500:
            return ErrorCategory.SERVER_ERROR
    if isinstance(exc, (anthropic.APIConnectionError, anthropic.APITimeoutError)):
        return ErrorCategory.NETWORK
    return ErrorCategory.UNKNOWN


def call_with_budget(messages: list[dict], budget: RetryBudget | None = None) -> str:
    if budget is None:
        budget = RetryBudget()

    attempt = 0
    while True:
        attempt += 1
        try:
            response = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=512,
                messages=messages,
            )
            return response.content[0].text

        except Exception as exc:
            category = categorize_error(exc)

            if not budget.can_retry(category):
                if category == ErrorCategory.PERMANENT:
                    raise RuntimeError(
                        f"Permanent error — will not retry: {exc}"
                    ) from exc
                raise RuntimeError(
                    f"Retry budget exhausted for {category.name} errors "
                    f"(history: {budget._history})"
                ) from exc

            budget.consume(category)

            # Backoff based on error type
            if category == ErrorCategory.RATE_LIMITED:
                delay = min(60, 2 ** attempt)
            elif category == ErrorCategory.SERVER_ERROR:
                delay = min(30, 2 ** attempt)
            else:
                delay = min(10, 2 ** attempt)

            print(f"[{category.name}] attempt {attempt} failed, retrying in {delay}s "
                  f"(budget: RL={budget.rate_limit_retries}, SE={budget.server_error_retries})")
            time.sleep(delay)


# Usage
budget = RetryBudget(rate_limit_retries=3, server_error_retries=2, network_retries=2)
try:
    result = call_with_budget([{"role": "user", "content": "Hello"}], budget)
    print(result)
except RuntimeError as e:
    print(f"Failed: {e}")
```

**Expected Token Savings:** Budget-aware retries prevent over-spending on transient errors while instantly failing on permanent ones.
**Environment:** Python 3.9+; budget object can be shared across tool calls in a single agent turn.

---

### Option 5: Per-tool non-retry registry for agent tool loops

```python
import anthropic

client = anthropic.Anthropic()

# Track which tool call inputs caused permanent errors
# Key: (tool_name, frozenset of input items) → error message
_permanent_failures: dict[tuple, str] = {}


def make_tool_key(tool_name: str, tool_input: dict) -> tuple:
    """Create a hashable key from tool name and inputs."""
    return (tool_name, frozenset(str(tool_input).split()))


def record_permanent_failure(tool_name: str, tool_input: dict, error: str) -> None:
    key = make_tool_key(tool_name, tool_input)
    _permanent_failures[key] = error
    print(f"[NON-RETRYABLE] Recorded permanent failure for {tool_name}: {error}")


def is_permanent_failure(tool_name: str, tool_input: dict) -> str | None:
    """Returns the error message if this tool+input previously caused a permanent failure."""
    key = make_tool_key(tool_name, tool_input)
    return _permanent_failures.get(key)


NON_RETRYABLE_ERROR_SIGNALS = ["not found", "404", "invalid", "400", "unauthorized", "401", "forbidden", "403"]


def classify_result(result: str) -> str:
    """Classify a tool result as ok, retryable_error, or permanent_error."""
    result_lower = result.lower()
    if "error" not in result_lower and "failed" not in result_lower:
        return "ok"
    for signal in NON_RETRYABLE_ERROR_SIGNALS:
        if signal in result_lower:
            return "permanent_error"
    return "retryable_error"


TOOLS = [
    {
        "name": "lookup_customer",
        "description": "Look up a customer by ID or email",
        "input_schema": {
            "type": "object",
            "properties": {
                "identifier": {"type": "string", "description": "Customer ID or email address"},
            },
            "required": ["identifier"],
        },
    },
    {
        "name": "get_subscription",
        "description": "Get subscription details for a customer",
        "input_schema": {
            "type": "object",
            "properties": {
                "customer_id": {"type": "string"},
            },
            "required": ["customer_id"],
        },
    },
]


def simulate_tool(name: str, inputs: dict) -> str:
    if name == "lookup_customer":
        id_ = inputs.get("identifier", "")
        if "@" in id_ or id_.startswith("cus_"):
            return f'{{"customer_id": "cus_123", "name": "Alice", "email": "{id_}"}}'
        return "ERROR 404: Customer not found"
    if name == "get_subscription":
        return f'{{"plan": "pro", "status": "active", "customer_id": "{inputs.get("customer_id")}"}}'
    return "ERROR 400: Unknown tool"


def agent_loop(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]

    for _ in range(10):  # Hard limit on turns
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            tools=TOOLS,
            system=(
                "You are a customer support agent. "
                "If a tool returns a PERMANENT ERROR, stop retrying that call and explain the issue to the user."
            ),
            messages=messages,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            return next(b.text for b in response.content if b.type == "text")

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue

            # Check if we've seen this exact call fail permanently before
            prior_failure = is_permanent_failure(block.name, block.input)
            if prior_failure:
                result = f"[BLOCKED] This call previously failed permanently: {prior_failure}. Do not retry."
            else:
                result = simulate_tool(block.name, block.input)
                classification = classify_result(result)

                if classification == "permanent_error":
                    record_permanent_failure(block.name, block.input, result)
                    result = f"{result}\n[PERMANENT ERROR — do not retry this exact call]"

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": result,
            })

        messages.append({"role": "user", "content": tool_results})

    return "Agent reached turn limit without resolving the task."


print(agent_loop("Look up customer with ID 'unknown-123' and get their subscription"))
```

**Expected Token Savings:** Per-call failure registry prevents the agent from retrying the same permanent failure across multiple turns — common in agentic loops.
**Environment:** Python 3.9+; in-process registry; suits single-session agents.

---

### Option 6: Structured error response with retry instruction

```python
import json
import anthropic

client = anthropic.Anthropic()


def format_tool_error(
    error_type: str,
    status_code: int | None,
    message: str,
    retry_allowed: bool,
    suggestion: str | None = None,
) -> str:
    """
    Return a structured error string that Claude can parse to decide on retry behavior.
    """
    payload = {
        "error": True,
        "error_type": error_type,
        "status_code": status_code,
        "message": message,
        "retry_allowed": retry_allowed,
    }
    if suggestion:
        payload["suggestion"] = suggestion

    # Clear text summary before JSON for Claude's natural language understanding
    retry_instruction = (
        "RETRY_ALLOWED: You may retry with different parameters."
        if retry_allowed
        else "DO_NOT_RETRY: This error is permanent. Explain the issue to the user."
    )

    return f"{retry_instruction}\n{json.dumps(payload, indent=2)}"


# Mapping of HTTP status codes to structured error responses
STATUS_CODE_RESPONSES = {
    400: lambda msg: format_tool_error("bad_request", 400, msg, retry_allowed=False,
                                        suggestion="Check that all required parameters are present and correctly formatted."),
    401: lambda msg: format_tool_error("unauthorized", 401, msg, retry_allowed=False,
                                        suggestion="The API key or token is invalid. Do not retry — ask the user to check credentials."),
    403: lambda msg: format_tool_error("forbidden", 403, msg, retry_allowed=False,
                                        suggestion="The operation is not permitted for this account."),
    404: lambda msg: format_tool_error("not_found", 404, msg, retry_allowed=False,
                                        suggestion="The requested resource does not exist. Verify the ID or path."),
    409: lambda msg: format_tool_error("conflict", 409, msg, retry_allowed=False,
                                        suggestion="The resource already exists or there is a state conflict."),
    422: lambda msg: format_tool_error("validation_error", 422, msg, retry_allowed=False,
                                        suggestion="The request data failed validation. Fix the input values."),
    429: lambda msg: format_tool_error("rate_limited", 429, msg, retry_allowed=True,
                                        suggestion="Wait before retrying."),
    500: lambda msg: format_tool_error("server_error", 500, msg, retry_allowed=True),
    503: lambda msg: format_tool_error("service_unavailable", 503, msg, retry_allowed=True),
}


def call_external_api(endpoint: str, params: dict) -> str:
    """Simulate API call with structured error responses."""
    if "missing" in endpoint:
        return STATUS_CODE_RESPONSES[404]("Resource not found")
    if "bad" in str(params):
        return STATUS_CODE_RESPONSES[400]("Invalid parameter: 'bad' is not a valid value")
    if "overload" in endpoint:
        return STATUS_CODE_RESPONSES[503]("Service temporarily unavailable")
    return json.dumps({"result": "success", "data": {"endpoint": endpoint, "params": params}})


TOOLS = [
    {
        "name": "call_api",
        "description": "Call an external API endpoint",
        "input_schema": {
            "type": "object",
            "properties": {
                "endpoint": {"type": "string"},
                "params": {"type": "object"},
            },
            "required": ["endpoint"],
        },
    }
]

SYSTEM = """You are an API integration agent.

<instructions>
- When a tool result contains DO_NOT_RETRY: stop immediately and explain the permanent error to the user.
- When a tool result contains RETRY_ALLOWED: you may try once more, then stop.
- Never retry more than once, even for retryable errors.
</instructions>"""

messages = [{"role": "user", "content": "Fetch data from the /missing-resource endpoint"}]

while True:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=SYSTEM,
        tools=TOOLS,
        messages=messages,
    )

    messages.append({"role": "assistant", "content": response.content})

    if response.stop_reason == "end_turn":
        print(next(b.text for b in response.content if b.type == "text"))
        break

    tool_results = []
    for block in response.content:
        if block.type == "tool_use":
            result = call_external_api(block.input.get("endpoint", ""), block.input.get("params", {}))
            print(f"Tool result: {result[:200]}")
            tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})

    messages.append({"role": "user", "content": tool_results})
```

**Expected Token Savings:** Structured `DO_NOT_RETRY` signal in tool results cuts retry loops at source, saving 3–10 tool round-trips per permanent error.
**Environment:** Python 3.9+; works for any tool that calls HTTP APIs; structured error payload is machine-readable by both Claude and your monitoring system.

---

| Option | Approach | Prevention Mechanism | Best For |
|--------|----------|---------------------|----------|
| 1 | Status-code-aware decorator | Exception type filtering | SDK-level retries |
| 2 | Error classifier in agent loop | Pattern matching on tool output | Tool-using agents |
| 3 | Tenacity with predicate | `retry_if_exception` | Library-grade retry |
| 4 | Error budget by category | Category-specific budgets | Complex retry policies |
| 5 | Per-call permanent failure registry | Memoized failure keys | Multi-turn agent loops |
| 6 | Structured DO_NOT_RETRY signal | Explicit retry instruction | HTTP API tool wrappers |
