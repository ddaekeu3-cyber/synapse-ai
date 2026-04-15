---
layout: solution
title: "Agent Doesn't Test Error Handling Paths"
category: testing
description: "Unit and integration tests only cover the happy path — rate limits, network errors, API timeouts, and malformed responses are never exercised, so error handlers are untested and broken in production."
tags: [testing, error-handling, reliability, ci, resilience]
---

## Symptom

The test suite passes 100% in CI. On the first production rate-limit spike, the agent crashes with an unhandled `RateLimitError` instead of waiting and retrying. The retry loop that was written but never tested has a bug — it retries the wrong number of times, or re-raises instead of returning a fallback. Error paths are discovered only when real users are affected.

## Root Cause

Error handling code is harder to trigger in tests than happy-path code: you cannot make `client.messages.create()` fail just by passing a particular prompt. Without mocking exceptions via `side_effect`, error handlers are dead code that appears to work but has never been executed. `side_effect` on a mock replaces the return value with an exception, letting you write tests that exercise every error branch.

## Fix

### Option 1 — Mock side_effect to raise SDK exceptions

```python
import pytest
import anthropic
import time
from unittest.mock import MagicMock, patch

# ── agent with error handling ─────────────────────────────────────────────────

def ask_with_retry(prompt: str, client: anthropic.Anthropic, max_retries: int = 3) -> str | None:
    for attempt in range(max_retries):
        try:
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=128,
                messages=[{"role": "user", "content": prompt}],
            )
            return resp.content[0].text
        except anthropic.RateLimitError:
            if attempt < max_retries - 1:
                time.sleep(0.001)  # tiny sleep in tests
                continue
            return None
        except anthropic.APIConnectionError:
            return None
        except anthropic.APIStatusError as e:
            if e.status_code >= 500:
                if attempt < max_retries - 1:
                    continue
            return None
    return None

# ── helpers ────────────────────────────────────────────────────────────────────

def make_error_client(error_class, **kwargs) -> MagicMock:
    client = MagicMock(spec=anthropic.Anthropic)
    client.messages.create.side_effect = error_class(
        message="mock error", request=MagicMock(), response=MagicMock()
    )
    return client

def make_success_client(text: str = "ok") -> MagicMock:
    content = MagicMock(); content.text = text
    resp    = MagicMock(); resp.content = [content]
    client  = MagicMock(spec=anthropic.Anthropic)
    client.messages.create.return_value = resp
    return client

# ── tests ──────────────────────────────────────────────────────────────────────

def test_rate_limit_retries_and_returns_none_on_exhaustion():
    client = make_error_client(anthropic.RateLimitError)
    result = ask_with_retry("test", client, max_retries=3)
    assert result is None
    assert client.messages.create.call_count == 3  # retried all 3 times

def test_connection_error_returns_none_immediately():
    client = make_error_client(anthropic.APIConnectionError)
    result = ask_with_retry("test", client, max_retries=3)
    assert result is None
    assert client.messages.create.call_count == 1  # no retry on connection error

def test_success_after_rate_limit():
    content = MagicMock(); content.text = "answer"
    resp    = MagicMock(); resp.content = [content]
    client  = MagicMock(spec=anthropic.Anthropic)
    client.messages.create.side_effect = [
        anthropic.RateLimitError(message="429", request=MagicMock(), response=MagicMock()),
        resp,
    ]
    result = ask_with_retry("test", client, max_retries=3)
    assert result == "answer"
    assert client.messages.create.call_count == 2

def test_success_returns_text():
    client = make_success_client("hello world")
    result = ask_with_retry("test", client)
    assert result == "hello world"

def test_single_retry_exhausted():
    client = make_error_client(anthropic.RateLimitError)
    result = ask_with_retry("test", client, max_retries=1)
    assert result is None
    assert client.messages.create.call_count == 1
```

**Expected Token Savings:** Catching broken retry logic in CI costs zero tokens; broken retry logic in production causes duplicate API calls and token waste.
**Environment:** Any agent with retry logic; the pattern applies to any `side_effect` exception type.

---

### Option 2 — Test timeout handling and fallback behavior

```python
import pytest
import anthropic
import asyncio
from unittest.mock import MagicMock, AsyncMock

# ── async agent with timeout ───────────────────────────────────────────────────

async def ask_with_timeout(
    prompt: str,
    client: anthropic.AsyncAnthropic,
    timeout_seconds: float = 5.0,
    fallback: str = "Request timed out. Please try again.",
) -> str:
    try:
        resp = await asyncio.wait_for(
            client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=128,
                messages=[{"role": "user", "content": prompt}],
            ),
            timeout=timeout_seconds,
        )
        return resp.content[0].text
    except asyncio.TimeoutError:
        return fallback
    except anthropic.APITimeoutError:
        return fallback
    except Exception as exc:
        return f"Error: {type(exc).__name__}"

# ── helpers ────────────────────────────────────────────────────────────────────

def make_async_timeout_client() -> MagicMock:
    """Client that always raises asyncio.TimeoutError."""
    client = MagicMock(spec=anthropic.AsyncAnthropic)
    client.messages.create = AsyncMock(side_effect=asyncio.TimeoutError())
    return client

def make_async_success_client(text: str) -> MagicMock:
    content = MagicMock(); content.text = text
    resp    = MagicMock(); resp.content = [content]
    client  = MagicMock(spec=anthropic.AsyncAnthropic)
    client.messages.create = AsyncMock(return_value=resp)
    return client

def make_async_sdk_timeout_client() -> MagicMock:
    client = MagicMock(spec=anthropic.AsyncAnthropic)
    client.messages.create = AsyncMock(
        side_effect=anthropic.APITimeoutError(request=MagicMock())
    )
    return client

# ── tests ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_asyncio_timeout_returns_fallback():
    client = make_async_timeout_client()
    result = await ask_with_timeout("test", client, timeout_seconds=1.0)
    assert "timed out" in result.lower()

@pytest.mark.asyncio
async def test_sdk_timeout_returns_fallback():
    client = make_async_sdk_timeout_client()
    result = await ask_with_timeout("test", client)
    assert "timed out" in result.lower()

@pytest.mark.asyncio
async def test_success_does_not_use_fallback():
    client = make_async_success_client("real answer")
    result = await ask_with_timeout("test", client)
    assert result == "real answer"
    assert "timed out" not in result

@pytest.mark.asyncio
async def test_custom_fallback_message():
    client = make_async_timeout_client()
    result = await ask_with_timeout("test", client, fallback="Service unavailable")
    assert result == "Service unavailable"

@pytest.mark.asyncio
async def test_unknown_exception_returns_error_type():
    client = MagicMock(spec=anthropic.AsyncAnthropic)
    client.messages.create = AsyncMock(side_effect=ValueError("unexpected"))
    result = await ask_with_timeout("test", client)
    assert "ValueError" in result
```

**Expected Token Savings:** Timeout fallbacks prevent hung requests from consuming thread/connection slots indefinitely; testing them ensures the fallback actually fires rather than re-raising.
**Environment:** Async agents with timeout budgets; latency-sensitive pipelines where degraded responses are better than blocking.

---

### Option 3 — Parametrized error scenario matrix

```python
import pytest
import anthropic
from unittest.mock import MagicMock

# ── agent under test ───────────────────────────────────────────────────────────

def classify(text: str, client: anthropic.Anthropic) -> dict:
    """Returns dict with label and error fields."""
    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=16,
            messages=[{"role": "user", "content": f"Classify as positive/negative: {text}"}],
        )
        label = resp.content[0].text.strip().lower()
        return {"label": label, "error": None, "retryable": False}
    except anthropic.RateLimitError:
        return {"label": None, "error": "rate_limited", "retryable": True}
    except anthropic.APIConnectionError:
        return {"label": None, "error": "connection_error", "retryable": True}
    except anthropic.AuthenticationError:
        return {"label": None, "error": "auth_error", "retryable": False}
    except anthropic.BadRequestError:
        return {"label": None, "error": "bad_request", "retryable": False}

def make_error(error_class) -> MagicMock:
    client = MagicMock(spec=anthropic.Anthropic)
    err_kwargs = {"message": "test", "request": MagicMock(), "response": MagicMock()}
    if error_class == anthropic.APIConnectionError:
        err_kwargs = {"message": "test", "request": MagicMock()}
    client.messages.create.side_effect = error_class(**err_kwargs)
    return client

# ── parametrized tests ────────────────────────────────────────────────────────

@pytest.mark.parametrize("error_class,expected_error,expected_retryable", [
    (anthropic.RateLimitError,      "rate_limited",      True),
    (anthropic.APIConnectionError,  "connection_error",  True),
    (anthropic.AuthenticationError, "auth_error",        False),
    (anthropic.BadRequestError,     "bad_request",       False),
])
def test_error_classification(error_class, expected_error, expected_retryable):
    client = make_error(error_class)
    result = classify("I love this!", client)
    assert result["label"] is None
    assert result["error"] == expected_error
    assert result["retryable"] == expected_retryable

@pytest.mark.parametrize("error_class", [
    anthropic.RateLimitError,
    anthropic.APIConnectionError,
    anthropic.AuthenticationError,
])
def test_errors_never_raise(error_class):
    """Agent must never propagate exceptions to the caller."""
    client = make_error(error_class)
    try:
        result = classify("test", client)
        assert isinstance(result, dict)
    except Exception as exc:
        pytest.fail(f"Agent propagated {type(exc).__name__} to caller")

def test_success_has_no_error():
    content = MagicMock(); content.text = "positive"
    resp    = MagicMock(); resp.content = [content]
    client  = MagicMock(spec=anthropic.Anthropic)
    client.messages.create.return_value = resp
    result = classify("great product!", client)
    assert result["error"] is None
    assert result["label"] == "positive"
```

**Expected Token Savings:** Parametrize covers all error branches in a single test block; ensures every error type is classified correctly and never propagates unexpectedly.
**Environment:** Agents with comprehensive exception handling; testing that the error taxonomy (retryable vs non-retryable) is correctly implemented.

---

### Option 4 — Transient error sequence: fail N times then succeed

```python
import pytest
import anthropic
from unittest.mock import MagicMock, call

def make_fail_then_succeed(fail_count: int, success_text: str, error_class=anthropic.RateLimitError) -> MagicMock:
    """Returns a client that raises `error_class` N times, then succeeds."""
    content = MagicMock(); content.text = success_text
    resp    = MagicMock(); resp.content = [content]
    errors  = [
        error_class(message="429", request=MagicMock(), response=MagicMock())
        for _ in range(fail_count)
    ]
    client = MagicMock(spec=anthropic.Anthropic)
    client.messages.create.side_effect = [*errors, resp]
    return client

# ── agent under test ───────────────────────────────────────────────────────────

import time

def ask_with_backoff(prompt: str, client: anthropic.Anthropic, max_retries: int = 5) -> str | None:
    for attempt in range(max_retries):
        try:
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=128,
                messages=[{"role": "user", "content": prompt}],
            )
            return resp.content[0].text
        except anthropic.RateLimitError:
            if attempt < max_retries - 1:
                time.sleep(0.001)
            else:
                return None
        except anthropic.APIConnectionError:
            if attempt < max_retries - 1:
                time.sleep(0.001)
            else:
                return None
    return None

# ── tests ──────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("fail_count,max_retries,should_succeed", [
    (0, 3, True),   # no failures
    (1, 3, True),   # 1 failure, recovered
    (2, 3, True),   # 2 failures, recovered
    (3, 3, False),  # 3 failures = exhausted
    (4, 3, False),  # 4 failures = exhausted
])
def test_retry_recovery_matrix(fail_count, max_retries, should_succeed):
    client = make_fail_then_succeed(fail_count, "answer")
    result = ask_with_backoff("test", client, max_retries=max_retries)
    if should_succeed:
        assert result == "answer"
    else:
        assert result is None

def test_exact_call_count_on_failure():
    client = make_fail_then_succeed(5, "answer")  # more failures than retries
    ask_with_backoff("test", client, max_retries=3)
    assert client.messages.create.call_count == 3

def test_exact_call_count_on_recovery():
    client = make_fail_then_succeed(2, "answer")
    ask_with_backoff("test", client, max_retries=5)
    assert client.messages.create.call_count == 3  # 2 fails + 1 success

@pytest.mark.parametrize("error_class", [
    anthropic.RateLimitError,
    anthropic.APIConnectionError,
])
def test_multiple_error_types_retry(error_class):
    client = make_fail_then_succeed(1, "recovered", error_class)
    result = ask_with_backoff("test", client, max_retries=3)
    assert result == "recovered"
```

**Expected Token Savings:** Fail-then-succeed sequences test the entire retry lifecycle including the eventual success; catches off-by-one errors in retry counting that cause premature abandonment.
**Environment:** Agents with exponential backoff; verifying that retry logic neither gives up too early nor retries indefinitely.

---

### Option 5 — Test malformed API response handling

```python
import pytest
import anthropic
from unittest.mock import MagicMock

# ── agent with defensive response handling ─────────────────────────────────────

def safe_extract_text(resp) -> str:
    """Defensively extract text from an API response."""
    if resp is None:
        return ""
    content = getattr(resp, "content", None)
    if not content:
        return ""
    for block in content:
        if getattr(block, "type", None) == "text":
            text = getattr(block, "text", "")
            if isinstance(text, str):
                return text
    return ""

def ask_defensive(prompt: str, client: anthropic.Anthropic) -> str:
    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=128,
            messages=[{"role": "user", "content": prompt}],
        )
        return safe_extract_text(resp)
    except Exception:
        return ""

# ── malformed response factories ───────────────────────────────────────────────

def make_empty_content_client() -> MagicMock:
    resp = MagicMock(); resp.content = []
    client = MagicMock(spec=anthropic.Anthropic)
    client.messages.create.return_value = resp
    return client

def make_none_response_client() -> MagicMock:
    client = MagicMock(spec=anthropic.Anthropic)
    client.messages.create.return_value = None
    return client

def make_no_text_block_client() -> MagicMock:
    block = MagicMock(); block.type = "tool_use"; block.text = None
    resp  = MagicMock(); resp.content = [block]
    client = MagicMock(spec=anthropic.Anthropic)
    client.messages.create.return_value = resp
    return client

def make_none_text_block_client() -> MagicMock:
    block = MagicMock(); block.type = "text"; block.text = None
    resp  = MagicMock(); resp.content = [block]
    client = MagicMock(spec=anthropic.Anthropic)
    client.messages.create.return_value = resp
    return client

# ── tests ──────────────────────────────────────────────────────────────────────

def test_empty_content_returns_empty_string():
    assert ask_defensive("test", make_empty_content_client()) == ""

def test_none_response_returns_empty_string():
    assert ask_defensive("test", make_none_response_client()) == ""

def test_no_text_block_returns_empty_string():
    assert ask_defensive("test", make_no_text_block_client()) == ""

def test_none_text_field_returns_empty_string():
    assert ask_defensive("test", make_none_text_block_client()) == ""

def test_normal_response_returns_text():
    content = MagicMock(); content.type = "text"; content.text = "hello"
    resp    = MagicMock(); resp.content = [content]
    client  = MagicMock(spec=anthropic.Anthropic)
    client.messages.create.return_value = resp
    assert ask_defensive("test", client) == "hello"

@pytest.mark.parametrize("bad_client_factory", [
    make_empty_content_client,
    make_none_response_client,
    make_no_text_block_client,
    make_none_text_block_client,
])
def test_malformed_response_never_raises(bad_client_factory):
    try:
        result = ask_defensive("test", bad_client_factory())
        assert isinstance(result, str)
    except Exception as exc:
        pytest.fail(f"Agent raised {type(exc).__name__} on malformed response")
```

**Expected Token Savings:** Malformed response tests catch `AttributeError` and `IndexError` that would crash the agent silently; these bugs appear when the API changes its response structure or when a tool_use block appears where text is expected.
**Environment:** Agents processing API responses defensively; multi-model agents where response structure may vary.

---

### Option 6 — Error handler coverage report using pytest-cov

```python
import pytest
import anthropic
from unittest.mock import MagicMock

# ── agent with multiple error handlers ────────────────────────────────────────

class AgentError(Exception):
    def __init__(self, code: str, message: str, retryable: bool):
        self.code = code; self.message = message; self.retryable = retryable
        super().__init__(message)

def run_agent(prompt: str, client: anthropic.Anthropic) -> str:
    """Agent that converts SDK errors to domain errors."""
    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=128,
            messages=[{"role": "user", "content": prompt}],
        )
        if not resp or not resp.content:
            raise AgentError("empty_response", "No content in response", retryable=True)
        return resp.content[0].text

    except anthropic.RateLimitError as e:
        raise AgentError("rate_limited", f"Rate limited: {e}", retryable=True) from e
    except anthropic.APIConnectionError as e:
        raise AgentError("connection_error", f"Connection failed: {e}", retryable=True) from e
    except anthropic.AuthenticationError as e:
        raise AgentError("auth_error", f"Auth failed: {e}", retryable=False) from e
    except anthropic.BadRequestError as e:
        raise AgentError("bad_request", f"Bad request: {e}", retryable=False) from e
    except anthropic.InternalServerError as e:
        raise AgentError("server_error", f"Server error: {e}", retryable=True) from e
    except AgentError:
        raise
    except Exception as e:
        raise AgentError("unknown", f"Unexpected: {e}", retryable=False) from e

def make_client_with_error(exc):
    client = MagicMock(spec=anthropic.Anthropic)
    client.messages.create.side_effect = exc
    return client

MOCK_ARGS = {"message": "err", "request": MagicMock(), "response": MagicMock()}
CONN_ARGS = {"message": "err", "request": MagicMock()}

@pytest.mark.parametrize("exc,expected_code,expected_retryable", [
    (anthropic.RateLimitError(**MOCK_ARGS),      "rate_limited",    True),
    (anthropic.APIConnectionError(**CONN_ARGS),  "connection_error", True),
    (anthropic.AuthenticationError(**MOCK_ARGS), "auth_error",       False),
    (anthropic.BadRequestError(**MOCK_ARGS),     "bad_request",      False),
    (anthropic.InternalServerError(**MOCK_ARGS), "server_error",     True),
    (RuntimeError("surprise"),                   "unknown",          False),
])
def test_error_conversion(exc, expected_code, expected_retryable):
    client = make_client_with_error(exc)
    with pytest.raises(AgentError) as exc_info:
        run_agent("test", client)
    assert exc_info.value.code == expected_code
    assert exc_info.value.retryable == expected_retryable

def test_empty_response_raises_agent_error():
    resp   = MagicMock(); resp.content = []
    client = MagicMock(spec=anthropic.Anthropic)
    client.messages.create.return_value = resp
    with pytest.raises(AgentError) as exc_info:
        run_agent("test", client)
    assert exc_info.value.code == "empty_response"
    assert exc_info.value.retryable is True

def test_success_does_not_raise():
    content = MagicMock(); content.text = "ok"
    resp    = MagicMock(); resp.content = [content]
    client  = MagicMock(spec=anthropic.Anthropic)
    client.messages.create.return_value = resp
    assert run_agent("test", client) == "ok"
```

**Expected Token Savings:** Domain error taxonomy (retryable vs non-retryable) is tested exhaustively; callers that check `retryable` before waiting will not burn retry tokens on auth errors that will never succeed.
**Environment:** Production agents with structured error propagation; systems where callers need to distinguish transient from permanent failures.

---

## Comparison

| Option | Error Types Covered | Retry Logic Tested | Async Support | Parametrized | Best For |
|---|---|---|---|---|---|
| 1. side_effect exceptions | Rate limit, connection | Yes | No | No | Baseline error path testing |
| 2. Timeout handling | Timeout (asyncio + SDK) | No | Yes | No | Async agents with timeout budgets |
| 3. Error scenario matrix | All SDK errors | No | No | Yes | Comprehensive error taxonomy |
| 4. Fail-then-succeed | Rate limit, connection | Yes (recovery) | No | Yes | Retry count and recovery verification |
| 5. Malformed responses | None (structural bugs) | No | No | Yes | Defensive response parsing |
| 6. Domain error taxonomy | All SDK + unknown | No | No | Yes | Structured error propagation to callers |
