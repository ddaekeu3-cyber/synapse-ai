---
title: "Agent Doesn't Implement Tool Error Classification and Categorization"
description: "Agents that log all tool errors as generic failures cannot distinguish actionable errors from noise: a 429 rate limit error, a 500 server error, a connection timeout, and a validation error all appear identical in dashboards. Implement tool error classification that categorizes errors by type, cause, and actionability, enabling targeted alerting and automated remediation routing."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-tool-error-classification-and-categorization
tags: [error-classification, error-categorization, tool-errors, actionable-alerts, error-taxonomy, incident-routing]
symptoms:
  - "All tool errors appear as 'error' in dashboards with no breakdown by type"
  - "On-call engineers cannot determine if an error spike is rate limiting, server errors, or timeouts"
  - "No distinction between transient errors (retry) and permanent errors (fix required)"
  - "Validation errors from bad LLM-generated arguments inflate the error rate metric"
  - "Error alerting fires on every error type equally, including expected validation failures"
---

## Why This Happens

Error logging is often implemented as a catch-all: catch Exception, log the message, increment an error counter. This produces a homogeneous error signal that conflates fundamentally different problems. A 429 error means "slow down" — it is expected and self-resolving. A 500 error means "the server is broken" — it needs investigation. A validation error means "the LLM generated bad arguments" — it is an agent quality issue, not an infrastructure issue. Without classification, all three trigger the same alert and require the same manual investigation.

## Solution 1: Error Taxonomy

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class ErrorCategory(str, Enum):
    RATE_LIMIT = "rate_limit"           # 429, quota exceeded
    SERVER_ERROR = "server_error"       # 5xx from external service
    TIMEOUT = "timeout"                 # connection or read timeout
    VALIDATION = "validation"           # bad arguments, schema mismatch
    AUTHENTICATION = "authentication"   # 401, 403, expired tokens
    NOT_FOUND = "not_found"            # 404, resource does not exist
    NETWORK = "network"                 # DNS failure, connection refused
    CIRCUIT_OPEN = "circuit_open"      # circuit breaker blocked the call
    QUOTA_EXHAUSTED = "quota_exhausted" # billing quota exceeded
    UNKNOWN = "unknown"


class ErrorActionability(str, Enum):
    RETRY = "retry"             # transient, retry will likely succeed
    BACKOFF = "backoff"         # retry after delay
    FIX_AGENT = "fix_agent"    # agent generated bad input, fix prompt
    FIX_CONFIG = "fix_config"  # wrong credentials or config
    ESCALATE = "escalate"      # human intervention needed
    IGNORE = "ignore"           # expected in normal operation


@dataclass
class ClassifiedError:
    tool_name: str
    category: ErrorCategory
    actionability: ErrorActionability
    http_status: Optional[int]
    error_message: str
    raw_error_type: str
    conversation_id: str = ""
    attempt_number: int = 1
    metadata: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=lambda: __import__("time").time())
    retryable: bool = False
```

## Solution 2: Error Classifier

```python
import re
from typing import Optional


class ToolErrorClassifier:
    """
    Classifies exceptions and HTTP errors from tool calls into
    the error taxonomy with actionability guidance.
    """

    HTTP_CATEGORY_MAP = {
        429: (ErrorCategory.RATE_LIMIT, ErrorActionability.BACKOFF, True),
        500: (ErrorCategory.SERVER_ERROR, ErrorActionability.RETRY, True),
        502: (ErrorCategory.SERVER_ERROR, ErrorActionability.RETRY, True),
        503: (ErrorCategory.SERVER_ERROR, ErrorActionability.RETRY, True),
        504: (ErrorCategory.SERVER_ERROR, ErrorActionability.RETRY, True),
        401: (ErrorCategory.AUTHENTICATION, ErrorActionability.FIX_CONFIG, False),
        403: (ErrorCategory.AUTHENTICATION, ErrorActionability.FIX_CONFIG, False),
        404: (ErrorCategory.NOT_FOUND, ErrorActionability.FIX_AGENT, False),
        400: (ErrorCategory.VALIDATION, ErrorActionability.FIX_AGENT, False),
        422: (ErrorCategory.VALIDATION, ErrorActionability.FIX_AGENT, False),
    }

    EXCEPTION_PATTERNS = [
        (r"timeout|timed out", ErrorCategory.TIMEOUT, ErrorActionability.RETRY, True),
        (r"connection refused|connection reset|econnrefused", ErrorCategory.NETWORK, ErrorActionability.RETRY, True),
        (r"rate.?limit|quota|too many requests", ErrorCategory.RATE_LIMIT, ErrorActionability.BACKOFF, True),
        (r"circuit.*open|circuit.*breaker", ErrorCategory.CIRCUIT_OPEN, ErrorActionability.BACKOFF, True),
        (r"auth|unauthorized|forbidden|token", ErrorCategory.AUTHENTICATION, ErrorActionability.FIX_CONFIG, False),
        (r"not found|404|no such", ErrorCategory.NOT_FOUND, ErrorActionability.FIX_AGENT, False),
        (r"validation|invalid|schema|required field", ErrorCategory.VALIDATION, ErrorActionability.FIX_AGENT, False),
        (r"dns|resolve|name.*not.*known", ErrorCategory.NETWORK, ErrorActionability.RETRY, True),
        (r"billing|quota.*exhausted|credit", ErrorCategory.QUOTA_EXHAUSTED, ErrorActionability.ESCALATE, False),
    ]

    def classify(
        self,
        exception: Exception,
        tool_name: str,
        conversation_id: str = "",
        http_status: Optional[int] = None,
        attempt_number: int = 1,
    ) -> ClassifiedError:
        error_msg = str(exception).lower()
        error_type = type(exception).__name__

        # HTTP status code classification
        if http_status and http_status in self.HTTP_CATEGORY_MAP:
            category, actionability, retryable = self.HTTP_CATEGORY_MAP[http_status]
            return ClassifiedError(
                tool_name=tool_name,
                category=category,
                actionability=actionability,
                http_status=http_status,
                error_message=str(exception),
                raw_error_type=error_type,
                conversation_id=conversation_id,
                attempt_number=attempt_number,
                retryable=retryable,
            )

        # Pattern-based classification
        for pattern, category, actionability, retryable in self.EXCEPTION_PATTERNS:
            if re.search(pattern, error_msg):
                return ClassifiedError(
                    tool_name=tool_name,
                    category=category,
                    actionability=actionability,
                    http_status=http_status,
                    error_message=str(exception),
                    raw_error_type=error_type,
                    conversation_id=conversation_id,
                    attempt_number=attempt_number,
                    retryable=retryable,
                )

        return ClassifiedError(
            tool_name=tool_name,
            category=ErrorCategory.UNKNOWN,
            actionability=ErrorActionability.ESCALATE,
            http_status=http_status,
            error_message=str(exception),
            raw_error_type=error_type,
            conversation_id=conversation_id,
            attempt_number=attempt_number,
            retryable=False,
        )
```

## Solution 3: Classified Error Store

```python
import time
from collections import deque
from threading import Lock
from typing import Deque, Dict, List, Optional, Tuple


class ClassifiedErrorStore:
    """
    Accumulates classified errors and provides queries by
    category, tool, actionability, and time window.
    """

    def __init__(self, max_errors: int = 100_000):
        self._errors: Deque[ClassifiedError] = deque()
        self._max = max_errors
        self._lock = Lock()

    def record(self, error: ClassifiedError) -> None:
        with self._lock:
            self._errors.append(error)
            if len(self._errors) > self._max:
                self._errors.popleft()

    def _recent(self, window_seconds: float) -> List[ClassifiedError]:
        cutoff = time.time() - window_seconds
        with self._lock:
            return [e for e in self._errors if e.timestamp >= cutoff]

    def by_category(self, window_seconds: float = 3600.0) -> Dict[str, int]:
        errors = self._recent(window_seconds)
        result: dict = {}
        for e in errors:
            result[e.category.value] = result.get(e.category.value, 0) + 1
        return dict(sorted(result.items(), key=lambda x: x[1], reverse=True))

    def by_actionability(self, window_seconds: float = 3600.0) -> Dict[str, int]:
        errors = self._recent(window_seconds)
        result: dict = {}
        for e in errors:
            result[e.actionability.value] = result.get(e.actionability.value, 0) + 1
        return result

    def by_tool(self, window_seconds: float = 3600.0) -> Dict[str, int]:
        errors = self._recent(window_seconds)
        result: dict = {}
        for e in errors:
            result[e.tool_name] = result.get(e.tool_name, 0) + 1
        return dict(sorted(result.items(), key=lambda x: x[1], reverse=True))

    def escalation_required(self, window_seconds: float = 3600.0) -> List[ClassifiedError]:
        return [
            e for e in self._recent(window_seconds)
            if e.actionability == ErrorActionability.ESCALATE
        ]

    def summary(self, window_seconds: float = 3600.0) -> dict:
        errors = self._recent(window_seconds)
        return {
            "window_seconds": window_seconds,
            "total_errors": len(errors),
            "by_category": self.by_category(window_seconds),
            "by_actionability": self.by_actionability(window_seconds),
            "by_tool": self.by_tool(window_seconds),
            "escalation_count": len(self.escalation_required(window_seconds)),
        }
```

## Solution 4: Classified Tool Executor

```python
import time
from typing import Any, Callable, Dict, Optional


class ClassifiedErrorToolExecutor:
    """
    Wraps tool execution to classify any exception that occurs.
    Records classified errors and exposes them for monitoring.
    """

    def __init__(
        self,
        classifier: ToolErrorClassifier,
        store: ClassifiedErrorStore,
    ):
        self._classifier = classifier
        self._store = store
        self._call_count = 0

    async def execute(
        self,
        tool_name: str,
        fn: Callable,
        args: Dict[str, Any],
        conversation_id: str = "",
        http_status_extractor: Optional[Callable] = None,
    ) -> Any:
        self._call_count += 1
        try:
            return await fn(**args)
        except Exception as exc:
            http_status = None
            if http_status_extractor:
                try:
                    http_status = http_status_extractor(exc)
                except Exception:
                    pass
            elif hasattr(exc, "status_code"):
                http_status = exc.status_code
            elif hasattr(exc, "response") and hasattr(exc.response, "status_code"):
                http_status = exc.response.status_code

            classified = self._classifier.classify(
                exception=exc,
                tool_name=tool_name,
                conversation_id=conversation_id,
                http_status=http_status,
            )
            self._store.record(classified)
            raise

    def error_rate(self, store: ClassifiedErrorStore, window_seconds: float = 3600.0) -> float:
        errors = len(store._recent(window_seconds))
        return errors / max(self._call_count, 1)
```

## Solution 5: Actionability-Based Alert Router

```python
import time
from typing import Callable, Dict, List, Optional


class ActionabilityAlertRouter:
    """
    Routes classified errors to appropriate handlers based on actionability.
    Prevents noisy alerts for expected error types (validation, rate limits).
    """

    def __init__(self, store: ClassifiedErrorStore):
        self._store = store
        self._handlers: Dict[ErrorActionability, Callable] = {}
        self._routed_count = 0

    def register_handler(self, actionability: ErrorActionability, handler: Callable) -> None:
        self._handlers[actionability] = handler

    async def route_pending(self, window_seconds: float = 60.0) -> int:
        recent = self._store._recent(window_seconds)
        routed = 0

        # Only escalate truly actionable errors
        for error in recent:
            if error.actionability == ErrorActionability.ESCALATE:
                handler = self._handlers.get(ErrorActionability.ESCALATE)
                if handler:
                    await handler(error)
                    routed += 1

        self._routed_count += routed
        return routed

    def suppressed_categories(self) -> List[str]:
        """Categories that are noisy but not escalated."""
        return [
            ErrorCategory.VALIDATION.value,
            ErrorCategory.RATE_LIMIT.value,
        ]
```

## Solution 6: Error Classification Dashboard

```python
import time


class ErrorClassificationDashboard:
    """
    Combines error store stats, escalation tracking, and per-tool
    breakdown into a single incident-readiness view.
    """

    def __init__(
        self,
        store: ClassifiedErrorStore,
        executor: ClassifiedErrorToolExecutor,
    ):
        self._store = store
        self._executor = executor

    def render(self) -> dict:
        summary_1h = self._store.summary(window_seconds=3600.0)
        summary_5m = self._store.summary(window_seconds=300.0)
        escalations = self._store.escalation_required(window_seconds=3600.0)

        return {
            "generated_at": time.time(),
            "total_calls": self._executor._call_count,
            "last_5m": summary_5m,
            "last_1h": summary_1h,
            "escalations_1h": len(escalations),
            "top_tool_errors": dict(list(summary_1h["by_tool"].items())[:5]),
            "alert": len(escalations) > 0 or summary_5m["total_errors"] > 50,
        }
```

## Comparison

| Approach | HTTP Classification | Pattern Classification | Store/Query | Alert Routing | Dashboard |
|---|---|---|---|---|---|
| ToolErrorClassifier | Yes (status map) | Yes (regex) | No | No | No |
| ClassifiedErrorStore | No | No | Yes | No | No |
| ClassifiedErrorToolExecutor | Via classifier | Via classifier | Via store | No | No |
| ActionabilityAlertRouter | No | No | Via store | Yes | No |
| ErrorClassificationDashboard | No | No | Via store | No | Yes |

**Best for production**: Never alert on `VALIDATION` or `RATE_LIMIT` errors in isolation — they are expected in normal operation and alerting on them creates alert fatigue. Alert only on `ESCALATE` actionability: unknown errors, quota exhaustion, and sustained authentication failures that require human intervention. Track `by_tool` weekly to identify which tools produce the most `FIX_AGENT` errors — a tool with many validation errors means the LLM is generating bad arguments and the prompt or schema needs improvement. Set up separate dashboards for infrastructure errors (`SERVER_ERROR`, `TIMEOUT`, `NETWORK`) and application errors (`VALIDATION`, `AUTHENTICATION`) — they have different owners and response procedures.
