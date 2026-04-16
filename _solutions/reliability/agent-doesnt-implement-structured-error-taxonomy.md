---
title: "Agent Doesn't Implement Structured Error Taxonomy"
description: "Agents that surface raw exceptions to callers mix retriable API errors, user-caused validation failures, and unrecoverable system faults into a single undifferentiated error channel — a structured taxonomy enables correct retry, user-messaging, and alerting decisions."
difficulty: intermediate
category: reliability
tags: [reliability, error-handling, taxonomy, structured-errors, retry, observability]
---

# Agent Doesn't Implement Structured Error Taxonomy

## Problem

When an agent raises a bare `Exception` or `RuntimeError`, callers can't distinguish a transient 429 rate limit (retry in 30s) from a malformed request (fix the payload and retry) from a billing hard stop (alert the team). This causes:
- Automatic retries on non-retriable user errors, wasting quota
- Silent swallowing of system-level faults that need paging
- User-visible stack traces instead of actionable error messages
- No structured error codes for downstream logging/alerting

**Symptoms:**
- Callers catch `Exception` and retry everything blindly
- 400 Bad Request retried 5 times before giving up
- Users see "Internal Server Error" instead of "Your message exceeds the length limit"
- Ops team can't alert on "billing_limit_reached" because it's not a distinct code
- Error rate dashboards can't separate user errors from system errors

---

## Solution 1: Error Code Enum with Retriable/Non-Retriable Classification

Define a flat error code enum and a base exception class that carries retry advice.

```python
import asyncio
from enum import Enum
from dataclasses import dataclass
from typing import Optional
import anthropic


class ErrorCode(str, Enum):
    # --- Retriable transient errors ---
    RATE_LIMITED          = "rate_limited"           # 429 — retry after backoff
    SERVICE_OVERLOADED    = "service_overloaded"     # 503/529 — retry
    NETWORK_TIMEOUT       = "network_timeout"        # Transient network issue
    LLM_INTERNAL_ERROR    = "llm_internal_error"    # 500 from provider

    # --- User errors (not retriable — fix the input) ---
    INPUT_TOO_LONG        = "input_too_long"
    INVALID_TOOL_ARGS     = "invalid_tool_args"
    SESSION_NOT_FOUND     = "session_not_found"
    MESSAGE_POLICY_BLOCKED= "message_policy_blocked"
    UNSUPPORTED_MEDIA_TYPE= "unsupported_media_type"

    # --- System/config errors (alert team) ---
    INVALID_API_KEY       = "invalid_api_key"
    BILLING_LIMIT_REACHED = "billing_limit_reached"
    QUOTA_EXHAUSTED       = "quota_exhausted"
    DEPENDENCY_UNAVAILABLE= "dependency_unavailable"

    # --- Unknown ---
    UNKNOWN               = "unknown"


RETRIABLE_CODES = {
    ErrorCode.RATE_LIMITED,
    ErrorCode.SERVICE_OVERLOADED,
    ErrorCode.NETWORK_TIMEOUT,
    ErrorCode.LLM_INTERNAL_ERROR,
}

ALERT_CODES = {
    ErrorCode.INVALID_API_KEY,
    ErrorCode.BILLING_LIMIT_REACHED,
    ErrorCode.QUOTA_EXHAUSTED,
}


@dataclass
class AgentError(Exception):
    code: ErrorCode
    message: str
    http_status: Optional[int] = None
    retry_after_seconds: Optional[float] = None
    details: Optional[dict] = None

    @property
    def is_retriable(self) -> bool:
        return self.code in RETRIABLE_CODES

    @property
    def needs_alert(self) -> bool:
        return self.code in ALERT_CODES

    def user_message(self) -> str:
        """Human-readable message safe to show end-users."""
        USER_MESSAGES = {
            ErrorCode.RATE_LIMITED: "The service is busy. Please try again in a moment.",
            ErrorCode.INPUT_TOO_LONG: "Your message is too long. Please shorten it and try again.",
            ErrorCode.MESSAGE_POLICY_BLOCKED: "This message was blocked by content policy.",
            ErrorCode.BILLING_LIMIT_REACHED: "Usage limit reached. Please contact support.",
            ErrorCode.SESSION_NOT_FOUND: "Your session has expired. Please start a new conversation.",
        }
        return USER_MESSAGES.get(self.code, "Something went wrong. Please try again.")

    def __str__(self) -> str:
        return f"AgentError({self.code.value}): {self.message}"


def classify_anthropic_error(exc: Exception) -> AgentError:
    """Map Anthropic SDK exceptions to our taxonomy."""
    if isinstance(exc, anthropic.RateLimitError):
        retry_after = None
        if hasattr(exc, "response") and exc.response:
            try:
                retry_after = float(exc.response.headers.get("retry-after", 30))
            except (TypeError, ValueError):
                retry_after = 30.0
        return AgentError(
            code=ErrorCode.RATE_LIMITED,
            message=str(exc),
            http_status=429,
            retry_after_seconds=retry_after or 30.0,
        )
    if isinstance(exc, anthropic.AuthenticationError):
        return AgentError(code=ErrorCode.INVALID_API_KEY, message=str(exc), http_status=401)
    if isinstance(exc, anthropic.APIStatusError):
        if exc.status_code == 400:
            return AgentError(code=ErrorCode.INVALID_TOOL_ARGS, message=str(exc), http_status=400)
        if exc.status_code in (503, 529):
            return AgentError(code=ErrorCode.SERVICE_OVERLOADED, message=str(exc), http_status=exc.status_code)
        if exc.status_code == 500:
            return AgentError(code=ErrorCode.LLM_INTERNAL_ERROR, message=str(exc), http_status=500)
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return AgentError(code=ErrorCode.NETWORK_TIMEOUT, message=str(exc))
    return AgentError(code=ErrorCode.UNKNOWN, message=str(exc))


class TaxonomyAwareAgent:
    def __init__(self, api_key: str, max_retries: int = 4):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)
        self.max_retries = max_retries

    async def complete(self, messages: list[dict]) -> str:
        last_error: Optional[AgentError] = None

        for attempt in range(self.max_retries):
            try:
                response = await self.client.messages.create(
                    model="claude-opus-4-6",
                    max_tokens=512,
                    messages=messages,
                )
                return response.content[0].text

            except Exception as exc:
                err = classify_anthropic_error(exc)
                last_error = err

                if err.needs_alert:
                    print(f"[ALERT] {err.code.value}: {err.message}")
                    raise err  # Don't retry system errors

                if not err.is_retriable:
                    raise err  # Don't retry user errors

                wait = err.retry_after_seconds or (2 ** attempt)
                print(f"[retry] {err.code.value} attempt={attempt+1} wait={wait:.1f}s")
                await asyncio.sleep(wait)

        raise last_error or AgentError(ErrorCode.UNKNOWN, "Max retries exceeded")


async def demo():
    agent = TaxonomyAwareAgent(api_key="sk-...")
    try:
        reply = await agent.complete([{"role": "user", "content": "Hello!"}])
        print(reply[:80])
    except AgentError as err:
        print(f"Error: {err.code.value} — {err.user_message()}")
        print(f"Retriable: {err.is_retriable}, Alert: {err.needs_alert}")

# asyncio.run(demo())
```

---

## Solution 2: Hierarchical Error Class Tree

Use Python's exception hierarchy so callers can catch at varying levels of specificity.

```python
import asyncio
from typing import Optional
import anthropic


class AgentBaseError(Exception):
    """Base for all agent errors."""
    code: str = "agent_error"
    http_status: Optional[int] = None
    retriable: bool = False
    user_facing_message: str = "An error occurred. Please try again."

    def __init__(self, message: str = "", **kwargs):
        super().__init__(message)
        self.message = message
        for k, v in kwargs.items():
            setattr(self, k, v)


# --- Retriable errors ---
class TransientError(AgentBaseError):
    retriable = True


class RateLimitError(TransientError):
    code = "rate_limited"
    http_status = 429
    user_facing_message = "Service is busy. Please wait and try again."

    def __init__(self, message: str = "", retry_after: float = 30.0):
        super().__init__(message)
        self.retry_after = retry_after


class ServiceOverloadedError(TransientError):
    code = "service_overloaded"
    http_status = 503
    user_facing_message = "Service is temporarily overloaded."


class NetworkTimeoutError(TransientError):
    code = "network_timeout"
    user_facing_message = "Request timed out. Please try again."


# --- User errors (not retriable) ---
class UserError(AgentBaseError):
    retriable = False


class InputTooLongError(UserError):
    code = "input_too_long"
    user_facing_message = "Your message is too long. Please shorten it."


class ContentPolicyError(UserError):
    code = "content_policy_blocked"
    user_facing_message = "This content was blocked by our usage policy."


class InvalidRequestError(UserError):
    code = "invalid_request"
    user_facing_message = "The request was invalid. Please check your input."


# --- System errors (alert ops) ---
class SystemError(AgentBaseError):
    retriable = False
    alert_ops: bool = True


class AuthenticationError(SystemError):
    code = "invalid_api_key"
    http_status = 401


class BillingLimitError(SystemError):
    code = "billing_limit_reached"
    http_status = 402


class ProviderInternalError(SystemError):
    code = "provider_internal_error"
    http_status = 500
    retriable = True  # Internal errors are retriable unlike other system errors
    alert_ops = True


def map_exception(exc: Exception) -> AgentBaseError:
    if isinstance(exc, anthropic.RateLimitError):
        retry = 30.0
        if hasattr(exc, "response") and exc.response:
            try:
                retry = float(exc.response.headers.get("retry-after", 30))
            except (TypeError, ValueError):
                pass
        return RateLimitError(str(exc), retry_after=retry)
    if isinstance(exc, anthropic.AuthenticationError):
        return AuthenticationError(str(exc))
    if isinstance(exc, anthropic.APIStatusError):
        if exc.status_code == 429:
            return RateLimitError(str(exc))
        if exc.status_code in (503, 529):
            return ServiceOverloadedError(str(exc))
        if exc.status_code == 500:
            return ProviderInternalError(str(exc))
        if exc.status_code == 400:
            return InvalidRequestError(str(exc))
    if isinstance(exc, asyncio.TimeoutError):
        return NetworkTimeoutError(str(exc))
    return AgentBaseError(str(exc))


class HierarchicalErrorAgent:
    def __init__(self, api_key: str):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)

    async def complete(self, message: str) -> str:
        try:
            response = await self.client.messages.create(
                model="claude-opus-4-6",
                max_tokens=256,
                messages=[{"role": "user", "content": message}],
            )
            return response.content[0].text
        except Exception as exc:
            raise map_exception(exc) from exc


async def demo():
    agent = HierarchicalErrorAgent(api_key="sk-...")
    try:
        reply = await agent.complete("Hello!")
        print(reply)
    except RateLimitError as e:
        print(f"Rate limited — retry in {e.retry_after}s")
    except UserError as e:
        print(f"User error: {e.user_facing_message}")
    except SystemError as e:
        print(f"System error (alert ops): {e.code}")
    except TransientError as e:
        print(f"Transient: {e.code} — will retry")

# asyncio.run(demo())
```

---

## Solution 3: Error Context Propagation with Structured Logging

Attach request context to errors and emit structured JSON logs that downstream alerting can query.

```python
import asyncio
import json
import sys
import time
import traceback
from dataclasses import dataclass, field
from typing import Optional
import anthropic


@dataclass
class ErrorContext:
    session_id: str = ""
    request_id: str = ""
    user_id: str = ""
    turn_index: int = 0
    model: str = ""
    timestamp: float = field(default_factory=time.time)
    extra: dict = field(default_factory=dict)


@dataclass
class StructuredError:
    code: str
    message: str
    retriable: bool
    severity: str  # "info", "warn", "error", "critical"
    context: ErrorContext = field(default_factory=ErrorContext)
    cause: Optional[str] = None  # Formatted exception chain

    def emit_log(self) -> None:
        record = {
            "timestamp": self.context.timestamp,
            "level": self.severity,
            "event": "agent_error",
            "error_code": self.code,
            "error_message": self.message,
            "retriable": self.retriable,
            "session_id": self.context.session_id,
            "request_id": self.context.request_id,
            "user_id": self.context.user_id,
            "model": self.context.model,
            "cause": self.cause,
        }
        print(json.dumps(record), file=sys.stderr)

    def to_api_response(self) -> dict:
        return {
            "error": self.code,
            "message": self._user_safe_message(),
            "request_id": self.context.request_id,
            "retriable": self.retriable,
        }

    def _user_safe_message(self) -> str:
        safe = {
            "rate_limited": "Service is busy. Please try again shortly.",
            "input_too_long": "Your message is too long.",
            "content_blocked": "This content was blocked.",
        }
        return safe.get(self.code, "An error occurred.")


def classify_and_log(exc: Exception, ctx: ErrorContext) -> StructuredError:
    cause = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))

    if isinstance(exc, anthropic.RateLimitError):
        err = StructuredError("rate_limited", str(exc), retriable=True,
                              severity="warn", context=ctx, cause=cause)
    elif isinstance(exc, anthropic.AuthenticationError):
        err = StructuredError("invalid_api_key", str(exc), retriable=False,
                              severity="critical", context=ctx, cause=cause)
    elif isinstance(exc, asyncio.TimeoutError):
        err = StructuredError("network_timeout", str(exc), retriable=True,
                              severity="warn", context=ctx, cause=cause)
    else:
        err = StructuredError("unknown", str(exc), retriable=False,
                              severity="error", context=ctx, cause=cause)

    err.emit_log()
    return err


class ContextualErrorAgent:
    def __init__(self, api_key: str):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)

    async def complete(
        self,
        message: str,
        ctx: ErrorContext,
    ) -> dict:
        try:
            response = await self.client.messages.create(
                model="claude-opus-4-6",
                max_tokens=256,
                messages=[{"role": "user", "content": message}],
            )
            return {"text": response.content[0].text}
        except Exception as exc:
            err = classify_and_log(exc, ctx)
            return err.to_api_response()


async def demo():
    import secrets
    agent = ContextualErrorAgent(api_key="sk-...")
    ctx = ErrorContext(
        session_id="sess_demo",
        request_id=secrets.token_hex(8),
        user_id="usr_42",
        model="claude-opus-4-6",
    )
    result = await agent.complete("Hello!", ctx)
    print(result)

# asyncio.run(demo())
```

---

## Solution 4: Error Budget Tracking per Error Category

Track how many errors of each category occurred per time window; alert when error budget is consumed.

```python
import asyncio
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Optional
import anthropic


@dataclass
class ErrorBudget:
    category: str
    window_seconds: float
    max_allowed: int

    def __post_init__(self):
        self._timestamps: deque = deque()

    def record(self) -> bool:
        """Record an error. Returns True if budget is now exceeded."""
        now = time.time()
        cutoff = now - self.window_seconds
        while self._timestamps and self._timestamps[0] < cutoff:
            self._timestamps.popleft()
        self._timestamps.append(now)
        return len(self._timestamps) > self.max_allowed

    def current_count(self) -> int:
        cutoff = time.time() - self.window_seconds
        return sum(1 for t in self._timestamps if t >= cutoff)


ERROR_BUDGETS = {
    "rate_limited":    ErrorBudget("rate_limited",    window_seconds=60,   max_allowed=10),
    "network_timeout": ErrorBudget("network_timeout", window_seconds=300,  max_allowed=5),
    "invalid_request": ErrorBudget("invalid_request", window_seconds=60,   max_allowed=20),
    "unknown":         ErrorBudget("unknown",         window_seconds=300,  max_allowed=3),
}


def classify_error_category(exc: Exception) -> str:
    if isinstance(exc, anthropic.RateLimitError):
        return "rate_limited"
    if isinstance(exc, asyncio.TimeoutError):
        return "network_timeout"
    if isinstance(exc, anthropic.APIStatusError) and exc.status_code == 400:
        return "invalid_request"
    return "unknown"


class BudgetTrackingAgent:
    def __init__(self, api_key: str):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)

    async def complete(self, message: str) -> str:
        try:
            response = await self.client.messages.create(
                model="claude-opus-4-6",
                max_tokens=256,
                messages=[{"role": "user", "content": message}],
            )
            return response.content[0].text
        except Exception as exc:
            category = classify_error_category(exc)
            budget = ERROR_BUDGETS.get(category)
            if budget:
                exceeded = budget.record()
                count = budget.current_count()
                if exceeded:
                    print(
                        f"[BUDGET] Error budget EXCEEDED: category={category} "
                        f"count={count}/{budget.max_allowed} in {budget.window_seconds}s window"
                    )
                else:
                    print(f"[budget] {category}: {count}/{budget.max_allowed}")
            raise
```

---

## Solution 5: Error Serialization for Cross-Service Transport

Serialize errors as structured payloads so they survive HTTP/queue hops with full context.

```python
import json
import time
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Optional
import anthropic


class ErrorSeverity(str, Enum):
    DEBUG = "debug"
    INFO = "info"
    WARN = "warn"
    ERROR = "error"
    CRITICAL = "critical"


@dataclass
class SerializableError:
    code: str
    message: str
    severity: str
    retriable: bool
    timestamp: float
    http_status: Optional[int] = None
    retry_after_seconds: Optional[float] = None
    context: dict = None  # type: ignore

    def __post_init__(self):
        if self.context is None:
            self.context = {}

    def to_json(self) -> str:
        return json.dumps({
            "code": self.code,
            "message": self.message,
            "severity": self.severity,
            "retriable": self.retriable,
            "timestamp": self.timestamp,
            "http_status": self.http_status,
            "retry_after_seconds": self.retry_after_seconds,
            "context": self.context,
        })

    @classmethod
    def from_json(cls, raw: str) -> "SerializableError":
        data = json.loads(raw)
        return cls(**data)

    @classmethod
    def from_exception(
        cls,
        exc: Exception,
        context: Optional[dict] = None,
    ) -> "SerializableError":
        now = time.time()
        ctx = context or {}

        if isinstance(exc, anthropic.RateLimitError):
            return cls("rate_limited", str(exc), ErrorSeverity.WARN, True, now,
                       http_status=429, retry_after_seconds=30.0, context=ctx)
        if isinstance(exc, anthropic.AuthenticationError):
            return cls("invalid_api_key", str(exc), ErrorSeverity.CRITICAL, False, now,
                       http_status=401, context=ctx)
        if isinstance(exc, anthropic.APIStatusError) and exc.status_code == 400:
            return cls("invalid_request", str(exc), ErrorSeverity.WARN, False, now,
                       http_status=400, context=ctx)
        return cls("unknown", str(exc), ErrorSeverity.ERROR, False, now, context=ctx)


class SerializingErrorAgent:
    def __init__(self, api_key: str):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)

    async def complete(
        self,
        message: str,
        ctx: Optional[dict] = None,
    ) -> dict:
        try:
            response = await self.client.messages.create(
                model="claude-opus-4-6",
                max_tokens=256,
                messages=[{"role": "user", "content": message}],
            )
            return {"ok": True, "text": response.content[0].text}
        except Exception as exc:
            err = SerializableError.from_exception(exc, context=ctx)
            serialized = err.to_json()
            print(f"[error] Serialized: {serialized[:120]}")
            return {"ok": False, "error": json.loads(serialized)}


import asyncio

async def demo():
    agent = SerializingErrorAgent(api_key="sk-...")
    result = await agent.complete("Hello!", ctx={"user_id": "usr_1", "session": "sess_abc"})
    print(result)

# asyncio.run(demo())
```

---

## Solution 6: Dead Letter Queue for Non-Retriable Errors

Route permanently failed requests to a dead letter queue for manual review instead of silently dropping them.

```python
import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Optional
import anthropic


@dataclass
class FailedRequest:
    request_id: str
    message: str
    error_code: str
    error_detail: str
    failed_at: float = field(default_factory=time.time)
    retry_count: int = 0
    session_id: str = ""


class DeadLetterQueue:
    def __init__(self, max_size: int = 1000):
        self._queue: list[FailedRequest] = []
        self._max_size = max_size
        self._lock = asyncio.Lock()

    async def enqueue(self, item: FailedRequest) -> None:
        async with self._lock:
            if len(self._queue) >= self._max_size:
                oldest = self._queue.pop(0)
                print(f"[dlq] Evicted oldest entry: {oldest.request_id}")
            self._queue.append(item)
            print(
                f"[dlq] Enqueued failed request: id={item.request_id} "
                f"code={item.error_code} retries={item.retry_count}"
            )

    async def drain(self) -> list[FailedRequest]:
        async with self._lock:
            items = list(self._queue)
            self._queue.clear()
        return items

    async def size(self) -> int:
        async with self._lock:
            return len(self._queue)


dlq = DeadLetterQueue()

NON_RETRIABLE_STATUS_CODES = {400, 401, 402, 403, 404, 422}


def is_retriable(exc: Exception) -> bool:
    if isinstance(exc, anthropic.RateLimitError):
        return True
    if isinstance(exc, anthropic.APIStatusError):
        return exc.status_code not in NON_RETRIABLE_STATUS_CODES
    if isinstance(exc, asyncio.TimeoutError):
        return True
    return False


def error_code(exc: Exception) -> str:
    if isinstance(exc, anthropic.RateLimitError):
        return "rate_limited"
    if isinstance(exc, anthropic.AuthenticationError):
        return "invalid_api_key"
    if isinstance(exc, anthropic.APIStatusError):
        return f"http_{exc.status_code}"
    return "unknown"


class DLQAgent:
    def __init__(self, api_key: str, max_retries: int = 3):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)
        self.max_retries = max_retries

    async def process(
        self,
        request_id: str,
        message: str,
        session_id: str = "",
    ) -> Optional[str]:
        last_exc: Optional[Exception] = None

        for attempt in range(self.max_retries):
            try:
                response = await self.client.messages.create(
                    model="claude-opus-4-6",
                    max_tokens=256,
                    messages=[{"role": "user", "content": message}],
                )
                return response.content[0].text
            except Exception as exc:
                last_exc = exc
                if not is_retriable(exc):
                    await dlq.enqueue(FailedRequest(
                        request_id=request_id,
                        message=message,
                        error_code=error_code(exc),
                        error_detail=str(exc),
                        retry_count=attempt,
                        session_id=session_id,
                    ))
                    return None
                await asyncio.sleep(2 ** attempt)

        # Retriable error exhausted — also DLQ it
        if last_exc:
            await dlq.enqueue(FailedRequest(
                request_id=request_id,
                message=message,
                error_code=error_code(last_exc),
                error_detail=f"Max retries exceeded: {last_exc}",
                retry_count=self.max_retries,
                session_id=session_id,
            ))
        return None


async def demo():
    agent = DLQAgent(api_key="sk-...")
    result = await agent.process("req_001", "Hello!", session_id="sess_1")
    print(f"Result: {result}")
    print(f"DLQ size: {await dlq.size()}")
    items = await dlq.drain()
    for item in items:
        print(f"DLQ item: {item.request_id} error={item.error_code}")

# asyncio.run(demo())
```

---

## Comparison

| Solution | Error Classification | Structured Logging | Retry Guidance | Alerting | Complexity |
|---|---|---|---|---|---|
| Error code enum | Flat enum | No | Via is_retriable | Via needs_alert | Low |
| Exception hierarchy | Class tree | No | Via retriable attr | Via alert_ops | Low |
| Context propagation + JSON log | Flat + context | Yes | Via retriable | Via severity | Low |
| Error budget tracking | Category-based | Partial | No | Budget exceeded | Medium |
| Serializable error | Flat struct | JSON | Via retriable | No | Low |
| Dead letter queue | Binary retriable | Partial | Via retry_count | Queue size | Medium |

**Recommendation:** Use Solution 2 (exception hierarchy) as your base — it lets callers `except RateLimitError` or `except UserError` without needing to check codes. Add Solution 3 (context propagation + JSON log) for production observability. Add Solution 6 (dead letter queue) for any agent that processes critical operations where silent failure is unacceptable.
