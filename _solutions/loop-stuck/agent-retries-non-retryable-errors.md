---
layout: solution
title: "Agent Retries Non-Retryable Errors — Wastes Tokens on Guaranteed Failures"
category: loop-stuck
description: "Agent gets a 400 Bad Request with 'invalid parameter: model must be one of'. It retries the same request 5 times. Each retry fails identically. Agent exhausts its retry budget on an error that can never succeed without changing the request."
tags: [loop-stuck, retry, error-handling, 400, 422, non-retryable, retry-budget, error-classification]
---

# Agent Retries Non-Retryable Errors — Wastes Tokens on Guaranteed Failures

## Symptom
- Agent retries a request that fails with 400 Bad Request 5 times before giving up
- Each retry is identical to the previous — same request, same error, same failure
- `422 Unprocessable Entity` response has clear validation error — agent ignores it and retries
- `"error": "model_not_found"` retried 3 times — model name is wrong and will never exist
- Agent logs show: attempt 1/5 failed, attempt 2/5 failed... (all with same error code)
- Token cost of 5 failed attempts = 5× the cost of failing once and escalating

## Root Cause
Retry logic treats all errors as transient. A 503 Service Unavailable is transient — the server might recover. A 400 Bad Request means the request itself is malformed — retrying the identical request will always get the same result. Mixing retryable and non-retryable errors in a single retry block wastes the retry budget and delays the agent from recognizing that it needs to change its approach, ask for help, or escalate.

## Fix

### Option 1: Classify errors before retrying

```python
import httpx
from enum import Enum

class ErrorClass(Enum):
    RETRYABLE = "retryable"         # Transient — retry with backoff
    NON_RETRYABLE = "non_retryable" # Permanent — don't retry, fix the request
    RATE_LIMITED = "rate_limited"   # Quota — wait then retry
    AUTH_FAILURE = "auth_failure"   # Token issue — refresh then retry

def classify_http_error(response: httpx.Response) -> ErrorClass:
    """
    Classify HTTP error to determine retry strategy.
    """
    status = response.status_code

    # Non-retryable: request is wrong, retrying won't help
    if status in (400, 404, 405, 406, 409, 410, 413, 422, 451):
        return ErrorClass.NON_RETRYABLE

    # Auth: token may be expired — refresh and retry
    if status == 401:
        return ErrorClass.AUTH_FAILURE

    # Permissions: not a token issue — don't retry
    if status == 403:
        return ErrorClass.NON_RETRYABLE

    # Rate limit: wait for Retry-After then retry
    if status == 429:
        return ErrorClass.RATE_LIMITED

    # Retryable: server-side transient errors
    if status in (500, 502, 503, 504, 507, 529):
        return ErrorClass.RETRYABLE

    # Unknown: assume retryable with low confidence
    return ErrorClass.RETRYABLE

def classify_api_error(error_body: dict) -> ErrorClass:
    """
    Classify by API error code — more precise than HTTP status.
    """
    error = error_body.get("error", {})
    code = error.get("code", "") if isinstance(error, dict) else str(error)
    message = error.get("message", "") if isinstance(error, dict) else ""

    # Non-retryable error codes (fix the request):
    NON_RETRYABLE_CODES = {
        "invalid_request_error", "validation_error", "invalid_parameter",
        "model_not_found", "invalid_model", "context_length_exceeded",
        "content_policy_violation", "unsupported_media_type",
        "missing_required_parameter", "invalid_api_key"
    }

    if code in NON_RETRYABLE_CODES:
        return ErrorClass.NON_RETRYABLE

    # Retryable error codes:
    RETRYABLE_CODES = {
        "overloaded_error", "api_error", "server_error",
        "rate_limit_error", "timeout_error"
    }

    if code in RETRYABLE_CODES:
        return ErrorClass.RETRYABLE if "rate" not in code else ErrorClass.RATE_LIMITED

    return ErrorClass.RETRYABLE  # Default: assume retryable
```

### Option 2: Smart retry wrapper with error classification

```python
import asyncio
import random
import httpx
from dataclasses import dataclass

@dataclass
class RetryConfig:
    max_retries: int = 3
    base_delay: float = 1.0
    max_delay: float = 60.0
    jitter: bool = True

class NonRetryableError(Exception):
    """Raised when the error cannot be resolved by retrying"""
    def __init__(self, status: int, error_code: str, message: str):
        self.status = status
        self.error_code = error_code
        super().__init__(
            f"Non-retryable error {status} ({error_code}): {message}. "
            f"Fix the request before retrying."
        )

async def smart_retry(
    fn,
    config: RetryConfig = RetryConfig(),
    token_refresh_fn = None
) -> any:
    """
    Retry with error classification.
    - Non-retryable errors: raise immediately (don't waste retries)
    - Rate limit: wait for Retry-After then retry
    - Auth failure: refresh token then retry
    - Transient: exponential backoff with jitter
    """
    last_error = None

    for attempt in range(config.max_retries + 1):
        try:
            return await fn()

        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            error_class = classify_http_error(e.response)

            try:
                body = e.response.json()
            except Exception:
                body = {}

            error_code = body.get("error", {}).get("code", str(status)) if isinstance(body.get("error"), dict) else str(status)
            message = body.get("error", {}).get("message", e.response.text[:200]) if isinstance(body.get("error"), dict) else e.response.text[:200]

            if error_class == ErrorClass.NON_RETRYABLE:
                # Don't retry — raise immediately with clear message
                raise NonRetryableError(status, error_code, message)

            if error_class == ErrorClass.AUTH_FAILURE and token_refresh_fn:
                print(f"Auth failure — refreshing token (attempt {attempt + 1})")
                await token_refresh_fn()
                # Retry with refreshed token (no delay needed)
                last_error = e
                continue

            if error_class == ErrorClass.RATE_LIMITED:
                retry_after = float(e.response.headers.get("retry-after", "60"))
                print(f"Rate limited — waiting {retry_after}s")
                await asyncio.sleep(retry_after)
                last_error = e
                continue

            # Transient — exponential backoff
            if attempt < config.max_retries:
                delay = min(config.base_delay * (2 ** attempt), config.max_delay)
                if config.jitter:
                    delay *= (0.5 + random.random() * 0.5)
                print(f"Transient error {status} — retrying in {delay:.1f}s (attempt {attempt + 1}/{config.max_retries})")
                await asyncio.sleep(delay)
                last_error = e
            else:
                raise

    raise last_error

# Usage:
try:
    result = await smart_retry(
        fn=lambda: call_api(bad_payload),
        config=RetryConfig(max_retries=3)
    )
except NonRetryableError as e:
    # Request is wrong — no point retrying
    print(f"Request error: {e}")
    # Fix the request or escalate to user
```

### Option 3: Per-error-type retry policies

```python
from dataclasses import dataclass
from typing import Callable

@dataclass
class RetryPolicy:
    should_retry: bool
    max_attempts: int
    delay_fn: Callable[[int], float]  # attempt number → delay seconds
    action_before_retry: Callable | None = None  # e.g., token refresh
    message: str = ""

RETRY_POLICIES: dict[str, RetryPolicy] = {
    # Non-retryable — fail immediately
    "400": RetryPolicy(
        should_retry=False, max_attempts=0,
        delay_fn=lambda _: 0,
        message="Bad request — fix parameters before retrying"
    ),
    "404": RetryPolicy(
        should_retry=False, max_attempts=0,
        delay_fn=lambda _: 0,
        message="Resource not found — verify the ID or URL"
    ),
    "422": RetryPolicy(
        should_retry=False, max_attempts=0,
        delay_fn=lambda _: 0,
        message="Validation failed — check request body against API schema"
    ),

    # Auth — refresh and retry once
    "401": RetryPolicy(
        should_retry=True, max_attempts=2,
        delay_fn=lambda _: 0,
        action_before_retry=refresh_token,
        message="Token expired — refreshing"
    ),

    # Rate limit — wait then retry
    "429": RetryPolicy(
        should_retry=True, max_attempts=5,
        delay_fn=lambda attempt: 60.0,  # Wait for quota reset
        message="Rate limited — waiting for quota reset"
    ),

    # Transient — exponential backoff
    "500": RetryPolicy(
        should_retry=True, max_attempts=3,
        delay_fn=lambda attempt: min(2 ** attempt, 30),
        message="Server error — retrying with backoff"
    ),
    "503": RetryPolicy(
        should_retry=True, max_attempts=4,
        delay_fn=lambda attempt: min(2 ** attempt * 2, 60),
        message="Service unavailable — retrying with backoff"
    ),
}

async def execute_with_policy(fn, status_code_fn) -> any:
    """Execute fn, applying the correct retry policy based on error code"""
    policy = None
    attempt = 0

    while True:
        try:
            return await fn()
        except Exception as e:
            code = str(status_code_fn(e))
            policy = RETRY_POLICIES.get(code, RETRY_POLICIES.get("500"))

            if not policy.should_retry or attempt >= policy.max_attempts - 1:
                print(f"Not retrying ({code}): {policy.message}")
                raise

            print(f"Retry policy for {code}: {policy.message} (attempt {attempt + 1}/{policy.max_attempts})")

            if policy.action_before_retry:
                await policy.action_before_retry()

            delay = policy.delay_fn(attempt)
            if delay > 0:
                await asyncio.sleep(delay)

            attempt += 1
```

### Option 4: Log non-retryable errors for diagnosis

```python
import logging
import json

logger = logging.getLogger(__name__)

NON_RETRYABLE_STATUSES = {400, 404, 405, 406, 409, 410, 413, 422, 451}

async def logged_api_call(endpoint: str, payload: dict, client: httpx.AsyncClient) -> dict:
    """
    API call with structured logging for non-retryable errors.
    Makes it easy to identify requests that need fixing (not retrying).
    """
    try:
        response = await client.post(endpoint, json=payload, timeout=30.0)
        response.raise_for_status()
        return response.json()

    except httpx.HTTPStatusError as e:
        status = e.response.status_code

        try:
            error_body = e.response.json()
        except Exception:
            error_body = {"raw": e.response.text[:500]}

        if status in NON_RETRYABLE_STATUSES:
            logger.error(
                "Non-retryable API error — fix request, do not retry",
                extra={
                    "event": "non_retryable_error",
                    "endpoint": endpoint,
                    "status": status,
                    "error": error_body,
                    "request_payload": {k: v for k, v in payload.items()
                                       if k not in ("api_key", "secret")},
                    "recommendation": "Inspect error.message and fix the request payload"
                }
            )
            raise NonRetryableError(
                status=status,
                error_code=error_body.get("error", {}).get("code", str(status)),
                message=json.dumps(error_body)[:200]
            )
        else:
            logger.warning(
                "Transient API error — will retry",
                extra={
                    "event": "retryable_error",
                    "endpoint": endpoint,
                    "status": status,
                    "error": error_body
                }
            )
            raise
```

### Option 5: Agent system prompt — teach error triage

```
System prompt:
"Error handling rules — read before retrying any failed operation:

NEVER retry these errors — they require fixing the request, not repeating it:
- 400 Bad Request: invalid parameters — inspect error.message, fix the payload
- 404 Not Found: wrong ID or URL — verify the resource exists
- 422 Unprocessable Entity: validation failed — check field types and required fields
- 403 Forbidden: insufficient permissions — cannot be resolved by retrying
- 409 Conflict: resource already exists or state conflict — handle the conflict

DO retry these errors — they are transient:
- 429 Too Many Requests: wait for Retry-After header duration, then retry once
- 500 Internal Server Error: wait 2s, 4s, 8s between attempts (max 3 retries)
- 503 Service Unavailable: wait 5s, 10s, 20s between attempts (max 3 retries)

When you receive a non-retryable error:
1. Read the full error message
2. Identify what is wrong with the request
3. Either fix the request OR report to the user that the operation cannot complete
4. Do NOT retry the same failing request — it will always fail the same way"
```

### Option 6: Retry budget tracker — stop when budget is exhausted

```python
from dataclasses import dataclass, field
import time

@dataclass
class RetryBudget:
    """
    Global retry budget across all operations in a task.
    Prevents runaway retry loops from consuming all tokens.
    """
    max_total_retries: int = 10
    max_per_endpoint: int = 3
    window_seconds: float = 300.0

    _total_retries: int = field(default=0, init=False)
    _per_endpoint: dict[str, int] = field(default_factory=dict, init=False)
    _window_start: float = field(default_factory=time.monotonic, init=False)

    def can_retry(self, endpoint: str, error_class: ErrorClass) -> bool:
        """Check if retry is allowed under current budget"""
        # Reset window if expired
        if time.monotonic() - self._window_start > self.window_seconds:
            self._total_retries = 0
            self._per_endpoint.clear()
            self._window_start = time.monotonic()

        # Non-retryable: never allow
        if error_class == ErrorClass.NON_RETRYABLE:
            return False

        # Budget checks
        if self._total_retries >= self.max_total_retries:
            print(
                f"Retry budget exhausted ({self._total_retries}/{self.max_total_retries} total). "
                f"Task may need human intervention."
            )
            return False

        endpoint_retries = self._per_endpoint.get(endpoint, 0)
        if endpoint_retries >= self.max_per_endpoint:
            print(
                f"Per-endpoint retry limit reached for {endpoint} "
                f"({endpoint_retries}/{self.max_per_endpoint}). "
                f"Endpoint may be persistently failing."
            )
            return False

        return True

    def record_retry(self, endpoint: str):
        self._total_retries += 1
        self._per_endpoint[endpoint] = self._per_endpoint.get(endpoint, 0) + 1

    @property
    def status(self) -> dict:
        return {
            "total_retries": self._total_retries,
            "max_total": self.max_total_retries,
            "remaining": self.max_total_retries - self._total_retries,
            "per_endpoint": dict(self._per_endpoint)
        }

budget = RetryBudget(max_total_retries=10, max_per_endpoint=3)
```

## Retryable vs Non-Retryable Error Reference

| HTTP Status | Error Class | Action |
|---|---|---|
| 400 Bad Request | Non-retryable | Fix request payload |
| 401 Unauthorized | Auth | Refresh token, retry once |
| 403 Forbidden | Non-retryable | Check permissions — not a token issue |
| 404 Not Found | Non-retryable | Verify resource ID/URL |
| 409 Conflict | Non-retryable | Resolve conflict first |
| 413 Payload Too Large | Non-retryable | Reduce request size |
| 422 Unprocessable | Non-retryable | Fix validation errors |
| 429 Too Many Requests | Rate-limited | Wait Retry-After, then retry |
| 500 Internal Error | Retryable | Exponential backoff, max 3 |
| 502 Bad Gateway | Retryable | Retry after 5s |
| 503 Unavailable | Retryable | Retry after 10s, max 4 |
| 504 Gateway Timeout | Retryable | Retry after 10s |

## Expected Token Savings
5 retries × non-retryable error × 1,000 tokens each: 5,000 wasted tokens per failed operation
Fail fast on non-retryable: 0 wasted retries, immediate escalation

## Environment
- Any agent with retry logic calling external APIs; critical for agents calling LLM APIs, REST services, or databases where request errors must be distinguished from service errors
- Source: direct experience; retrying 400/422 errors is one of the most common sources of wasted token budget in production agents
