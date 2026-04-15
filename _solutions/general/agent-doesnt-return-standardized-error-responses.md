---
layout: solution
title: "Agent Doesn't Return Standardized Error Responses"
category: general
description: "Agents that return ad-hoc error shapes make client integration brittle. A consistent error schema lets clients handle failures programmatically without parsing freeform strings."
tags: [general, error-handling, api-design, fastapi, rfc7807, pydantic]
---

# Agent Doesn't Return Standardized Error Responses

When an agent fails, it might return `{"error": "something went wrong"}`, `{"message": "Internal Server Error"}`, `{"detail": "rate limit exceeded"}`, or a raw Python traceback — all with HTTP 500. Clients can't distinguish between a rate limit (retry later), an auth failure (fix the key), a bad input (fix the request), and a server crash (report a bug). Standardized error shapes make all of these machine-parseable.

## Why This Happens

FastAPI's default error format works for demos. Teams add error handling case by case and end up with five different shapes. No one writes the standard until it's too late.

---

## Option 1: RFC 7807 Problem Details Format

Implement the IETF RFC 7807 "Problem Details for HTTP APIs" standard — the most widely adopted API error format.

```python
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
import anthropic

app = FastAPI()
client = anthropic.Anthropic()


class ProblemDetail(BaseModel):
    """RFC 7807 Problem Details."""
    type: str       # URI identifying the problem type
    title: str      # Short, human-readable summary
    status: int     # HTTP status code
    detail: str     # Human-readable explanation specific to this occurrence
    instance: str | None = None  # URI identifying the specific occurrence

    # Agent-specific extensions
    error_code: str | None = None
    retry_after: int | None = None


PROBLEM_TYPES = {
    "rate_limited":     "https://agent.example.com/errors/rate-limited",
    "auth_failed":      "https://agent.example.com/errors/auth-failed",
    "invalid_input":    "https://agent.example.com/errors/invalid-input",
    "model_overloaded": "https://agent.example.com/errors/model-overloaded",
    "internal":         "https://agent.example.com/errors/internal",
}


def problem_response(
    problem_type: str,
    title: str,
    status: int,
    detail: str,
    request: Request | None = None,
    retry_after: int | None = None,
) -> JSONResponse:
    problem = ProblemDetail(
        type=PROBLEM_TYPES.get(problem_type, PROBLEM_TYPES["internal"]),
        title=title,
        status=status,
        detail=detail,
        instance=str(request.url) if request else None,
        error_code=problem_type,
        retry_after=retry_after,
    )
    headers = {}
    if retry_after:
        headers["Retry-After"] = str(retry_after)

    return JSONResponse(
        content=problem.model_dump(exclude_none=True),
        status_code=status,
        media_type="application/problem+json",
        headers=headers,
    )


@app.exception_handler(anthropic.RateLimitError)
async def rate_limit_handler(request: Request, exc: anthropic.RateLimitError):
    return problem_response(
        "rate_limited", "Rate Limit Exceeded", 429,
        "The AI service is rate limiting requests. Please retry after the indicated delay.",
        request=request, retry_after=30,
    )


@app.exception_handler(anthropic.AuthenticationError)
async def auth_handler(request: Request, exc: anthropic.AuthenticationError):
    return problem_response(
        "auth_failed", "Authentication Failed", 401,
        "The API key is invalid or expired.",
        request=request,
    )


@app.exception_handler(anthropic.APIStatusError)
async def api_status_handler(request: Request, exc: anthropic.APIStatusError):
    if exc.status_code == 529:
        return problem_response(
            "model_overloaded", "Model Overloaded", 503,
            "The AI model is temporarily overloaded. Retry with exponential backoff.",
            request=request, retry_after=10,
        )
    return problem_response(
        "internal", "Upstream API Error", 502,
        f"The AI service returned an unexpected error: {exc.status_code}",
        request=request,
    )


@app.post("/agent/run")
async def run_agent(request: Request, prompt: str):
    if not prompt or not prompt.strip():
        return problem_response(
            "invalid_input", "Invalid Input", 422,
            "prompt must be a non-empty string",
            request=request,
        )

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    return {"result": response.content[0].text}
```

**Expected Token Savings:** Standardized errors enable client-side retry logic; clients retry rate limits but not auth failures, reducing wasted API calls.

**Environment:** Any FastAPI agent; RFC 7807 is supported by most API clients and gateways.

---

## Option 2: Typed Error Code Enum with Consistent Shape

Define an enum of all possible error codes and a single response model used everywhere.

```python
from enum import Enum
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
import anthropic

app = FastAPI()
client = anthropic.Anthropic()


class ErrorCode(str, Enum):
    VALIDATION_ERROR   = "VALIDATION_ERROR"
    AUTH_ERROR         = "AUTH_ERROR"
    RATE_LIMITED       = "RATE_LIMITED"
    MODEL_ERROR        = "MODEL_ERROR"
    QUOTA_EXCEEDED     = "QUOTA_EXCEEDED"
    INTERNAL_ERROR     = "INTERNAL_ERROR"
    CONTEXT_TOO_LONG   = "CONTEXT_TOO_LONG"
    TOOL_FAILED        = "TOOL_FAILED"


class ErrorResponse(BaseModel):
    code: ErrorCode
    message: str
    details: dict | None = None
    request_id: str | None = None
    retry_after_seconds: int | None = None


def make_error(
    code: ErrorCode,
    message: str,
    status: int,
    details: dict | None = None,
    retry_after: int | None = None,
    request_id: str | None = None,
) -> JSONResponse:
    body = ErrorResponse(
        code=code,
        message=message,
        details=details,
        request_id=request_id,
        retry_after_seconds=retry_after,
    )
    headers = {}
    if retry_after:
        headers["Retry-After"] = str(retry_after)
    return JSONResponse(
        content=body.model_dump(exclude_none=True),
        status_code=status,
        headers=headers,
    )


@app.exception_handler(anthropic.RateLimitError)
async def on_rate_limit(request: Request, exc):
    return make_error(ErrorCode.RATE_LIMITED, "Rate limit hit — retry shortly", 429, retry_after=30)


@app.exception_handler(anthropic.BadRequestError)
async def on_bad_request(request: Request, exc: anthropic.BadRequestError):
    if "context_length" in str(exc).lower():
        return make_error(ErrorCode.CONTEXT_TOO_LONG, "Prompt exceeds model context limit", 422)
    return make_error(ErrorCode.VALIDATION_ERROR, str(exc), 422)


@app.exception_handler(Exception)
async def on_unhandled(request: Request, exc: Exception):
    import uuid
    rid = str(uuid.uuid4())
    print(f"[{rid}] Unhandled: {exc}")
    return make_error(
        ErrorCode.INTERNAL_ERROR,
        "An unexpected error occurred",
        500,
        request_id=rid,
    )


@app.post("/agent/run")
async def run_agent(prompt: str):
    if len(prompt) > 50_000:
        return make_error(
            ErrorCode.VALIDATION_ERROR,
            "Prompt too long",
            422,
            details={"max_chars": 50000, "got": len(prompt)},
        )
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    return {"result": response.content[0].text}
```

**Expected Token Savings:** Clients can programmatically route errors — retry on RATE_LIMITED, surface to user on VALIDATION_ERROR, alert on INTERNAL_ERROR.

**Environment:** Internal APIs with multiple client teams; microservice meshes.

---

## Option 3: Error Registry with Documentation Links

Extend the error response with a `docs_url` field pointing to runbook or troubleshooting documentation.

```python
from dataclasses import dataclass
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import anthropic

app = FastAPI()
client = anthropic.Anthropic()

BASE_DOCS = "https://docs.agent.example.com/errors"


@dataclass
class ErrorDefinition:
    code: str
    title: str
    status: int
    docs_path: str
    retryable: bool
    default_message: str


ERROR_REGISTRY: dict[str, ErrorDefinition] = {
    "rate_limited": ErrorDefinition(
        code="rate_limited", title="Rate Limit Exceeded", status=429,
        docs_path="/rate-limits", retryable=True,
        default_message="Too many requests. Use exponential backoff.",
    ),
    "model_overloaded": ErrorDefinition(
        code="model_overloaded", title="Model Temporarily Unavailable", status=503,
        docs_path="/model-availability", retryable=True,
        default_message="The AI model is temporarily overloaded.",
    ),
    "invalid_prompt": ErrorDefinition(
        code="invalid_prompt", title="Invalid Prompt", status=422,
        docs_path="/prompt-requirements", retryable=False,
        default_message="The prompt does not meet requirements.",
    ),
    "auth_failed": ErrorDefinition(
        code="auth_failed", title="Authentication Failed", status=401,
        docs_path="/authentication", retryable=False,
        default_message="Invalid or missing API key.",
    ),
    "internal": ErrorDefinition(
        code="internal", title="Internal Server Error", status=500,
        docs_path="/internal-errors", retryable=False,
        default_message="An unexpected error occurred.",
    ),
}


def error_response(
    error_code: str,
    message: str | None = None,
    detail: dict | None = None,
    retry_after: int | None = None,
) -> JSONResponse:
    defn = ERROR_REGISTRY.get(error_code, ERROR_REGISTRY["internal"])
    body = {
        "error": {
            "code": defn.code,
            "title": defn.title,
            "message": message or defn.default_message,
            "retryable": defn.retryable,
            "docs_url": f"{BASE_DOCS}{defn.docs_path}",
        }
    }
    if detail:
        body["error"]["detail"] = detail
    if retry_after and defn.retryable:
        body["error"]["retry_after_seconds"] = retry_after

    headers = {"Retry-After": str(retry_after)} if retry_after else {}
    return JSONResponse(content=body, status_code=defn.status, headers=headers)


@app.exception_handler(anthropic.RateLimitError)
async def handle_rate_limit(request: Request, exc):
    return error_response("rate_limited", retry_after=30)


@app.exception_handler(anthropic.AuthenticationError)
async def handle_auth(request: Request, exc):
    return error_response("auth_failed")


@app.post("/agent/run")
async def run_agent(prompt: str):
    if not prompt.strip():
        return error_response("invalid_prompt", message="prompt cannot be blank")
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )
    return {"result": response.content[0].text}
```

**Expected Token Savings:** `retryable: true/false` in the response body lets SDKs auto-retry correctly without custom client logic.

**Environment:** Developer-facing APIs; SDKs that embed error handling logic.

---

## Option 4: Middleware Error Normalizer

Catch all unhandled exceptions at middleware level and normalize them into a consistent shape before they reach the client.

```python
import uuid
import traceback
import anthropic
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

app = FastAPI()
client = anthropic.Anthropic()


def classify_exception(exc: Exception) -> tuple[str, int, bool]:
    """Returns (error_code, http_status, retryable)."""
    if isinstance(exc, anthropic.RateLimitError):
        return "rate_limited", 429, True
    if isinstance(exc, anthropic.AuthenticationError):
        return "auth_failed", 401, False
    if isinstance(exc, anthropic.BadRequestError):
        return "invalid_request", 422, False
    if isinstance(exc, anthropic.APIStatusError) and exc.status_code == 529:
        return "model_overloaded", 503, True
    if isinstance(exc, anthropic.APIStatusError):
        return "upstream_error", 502, True
    if isinstance(exc, ValueError):
        return "invalid_input", 422, False
    if isinstance(exc, PermissionError):
        return "forbidden", 403, False
    return "internal_error", 500, False


class ErrorNormalizerMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        try:
            return await call_next(request)
        except Exception as exc:
            error_code, status, retryable = classify_exception(exc)
            error_id = str(uuid.uuid4())[:8]

            # Log full traceback server-side
            print(f"[{error_id}] {type(exc).__name__}: {exc}")
            if status >= 500:
                traceback.print_exc()

            body = {
                "error": {
                    "code": error_code,
                    "message": str(exc) if status < 500 else "An internal error occurred",
                    "error_id": error_id,
                    "retryable": retryable,
                }
            }
            return JSONResponse(content=body, status_code=status)


app.add_middleware(ErrorNormalizerMiddleware)


@app.post("/agent/run")
async def run_agent(prompt: str):
    # Any exception here is normalized by middleware
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    return {"result": response.content[0].text}
```

**Expected Token Savings:** Zero per-route error handling boilerplate; consistent shape across all endpoints automatically.

**Environment:** Existing FastAPI codebases; retrofit without touching any route handlers.

---

## Option 5: Error Response Client SDK

Package standardized error handling into a client SDK so all consumers get the same retry and error parsing logic.

```python
# synapse_client.py — client-side SDK with standardized error handling
import time
import httpx
from dataclasses import dataclass
from typing import Any


@dataclass
class AgentError(Exception):
    code: str
    message: str
    status: int
    retryable: bool
    retry_after: int | None = None
    docs_url: str | None = None
    error_id: str | None = None

    def __str__(self):
        return f"[{self.code}] {self.message} (status={self.status}, retryable={self.retryable})"


class SynapseAgentClient:
    def __init__(self, base_url: str, api_key: str, max_retries: int = 3):
        self._base = base_url.rstrip("/")
        self._key = api_key
        self._max_retries = max_retries
        self._http = httpx.Client(headers={"Authorization": f"Bearer {api_key}"}, timeout=30)

    def _parse_error(self, resp: httpx.Response) -> AgentError:
        try:
            body = resp.json()
            err = body.get("error", body)
            return AgentError(
                code=err.get("code", "unknown"),
                message=err.get("message", resp.text),
                status=resp.status_code,
                retryable=err.get("retryable", resp.status_code in (429, 503, 502)),
                retry_after=err.get("retry_after_seconds"),
                docs_url=err.get("docs_url"),
                error_id=err.get("error_id"),
            )
        except Exception:
            return AgentError(
                code="parse_error",
                message=resp.text,
                status=resp.status_code,
                retryable=resp.status_code >= 500,
            )

    def run(self, prompt: str, **kwargs) -> str:
        for attempt in range(self._max_retries):
            resp = self._http.post(f"{self._base}/agent/run", params={"prompt": prompt, **kwargs})

            if resp.is_success:
                return resp.json()["result"]

            error = self._parse_error(resp)

            if not error.retryable or attempt == self._max_retries - 1:
                raise error

            # Respect Retry-After or use exponential backoff
            delay = error.retry_after or (2 ** attempt)
            print(f"[SDK] {error.code} — retrying in {delay}s (attempt {attempt + 1}/{self._max_retries})")
            time.sleep(delay)

        raise AgentError("max_retries", "Max retries exceeded", 0, False)


# Usage
if __name__ == "__main__":
    sdk = SynapseAgentClient("http://localhost:8000", api_key="your-key")
    try:
        result = sdk.run("What is 2+2?")
        print(result)
    except AgentError as e:
        print(f"Error: {e}")
        if e.docs_url:
            print(f"Troubleshooting: {e.docs_url}")
```

**Expected Token Savings:** Client auto-retries only retryable errors; avoids hammering on 401s or 422s that will never succeed.

**Environment:** Any project with multiple clients consuming the agent API; internal SDKs.

---

## Option 6: Error Contract Tests

Verify that every known error case returns the correct shape and status code.

```python
import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch
import anthropic

# Import app from option 1 or 2
from fastapi import FastAPI
from fastapi.responses import JSONResponse

# Minimal test app
app = FastAPI()

@app.post("/agent/run")
async def run(prompt: str):
    if not prompt.strip():
        return JSONResponse(
            {"error": {"code": "invalid_input", "message": "prompt required", "retryable": False}},
            status_code=422,
        )
    raise ValueError("Simulated internal error")

@app.exception_handler(ValueError)
async def val_err(req, exc):
    return JSONResponse(
        {"error": {"code": "internal_error", "message": str(exc), "retryable": False}},
        status_code=500,
    )

client = TestClient(app, raise_server_exceptions=False)


class TestErrorContract:
    def test_empty_prompt_returns_422(self):
        resp = client.post("/agent/run", params={"prompt": ""})
        assert resp.status_code == 422

    def test_422_has_error_envelope(self):
        resp = client.post("/agent/run", params={"prompt": ""})
        body = resp.json()
        assert "error" in body
        assert "code" in body["error"]
        assert "message" in body["error"]
        assert "retryable" in body["error"]

    def test_422_not_retryable(self):
        resp = client.post("/agent/run", params={"prompt": ""})
        assert resp.json()["error"]["retryable"] is False

    def test_500_has_error_envelope(self):
        resp = client.post("/agent/run", params={"prompt": "x"})
        assert resp.status_code == 500
        body = resp.json()
        assert "error" in body
        assert body["error"]["code"] == "internal_error"

    def test_error_code_is_string(self):
        resp = client.post("/agent/run", params={"prompt": ""})
        assert isinstance(resp.json()["error"]["code"], str)

    def test_error_message_is_string(self):
        resp = client.post("/agent/run", params={"prompt": ""})
        assert isinstance(resp.json()["error"]["message"], str)

    def test_no_raw_traceback_in_500(self):
        resp = client.post("/agent/run", params={"prompt": "x"})
        body_str = resp.text
        assert "Traceback" not in body_str
        assert "File \"" not in body_str


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
```

**Expected Token Savings:** Contract tests prevent error shape regressions; clients don't break when error format accidentally changes.

**Environment:** CI pipeline; any API with multiple client consumers.

---

## Comparison

| Option | Standard | Machine-Parseable | Retry Signal | Docs Link | Middleware |
|--------|----------|-------------------|--------------|-----------|------------|
| 1. RFC 7807 | Yes (IETF) | Yes | `Retry-After` header | No | Per-handler |
| 2. Typed enum | Custom | Yes | `retry_after_seconds` | No | Per-handler |
| 3. Error registry + docs | Custom | Yes | Yes | Yes | Per-handler |
| 4. Middleware normalizer | Custom | Yes | Yes | No | All routes |
| 5. Client SDK | Any | SDK handles | Auto-retry | Yes | Client-side |
| 6. Contract tests | Any | Verified | Tested | No | N/A |
