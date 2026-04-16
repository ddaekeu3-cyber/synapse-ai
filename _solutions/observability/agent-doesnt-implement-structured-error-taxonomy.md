---
title: "Agent Doesn't Implement Structured Error Taxonomy"
description: "Agents that log raw exception messages produce unstructured error noise — stack traces, HTTP error strings, and timeout messages that cannot be aggregated, alerted on by type, or used to identify the most impactful failure categories. Implement a structured error taxonomy that classifies every exception into a typed hierarchy, attaches operational context, and enables error rate dashboards broken down by category and severity."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-structured-error-taxonomy
tags: [error-taxonomy, structured-errors, error-classification, observability, error-rates, incident-response]
symptoms:
  - "Error logs contain raw exception strings with no consistent classification"
  - "Cannot answer 'what percentage of errors are transient vs permanent?' from logs"
  - "Alert rules match on exception class names that change across library versions"
  - "No way to distinguish a tool timeout from an LLM rate limit from a validation error"
  - "Error dashboards show a single 'errors' counter with no category breakdown"
---

## Why This Happens

Python exceptions carry a class name and message, but no operational category, retriability signal, or user-impact level. When these raw exceptions propagate to logging, every error looks the same — a stack trace with a string. Structured error taxonomy requires a classification layer that maps exception types and context to a defined set of error codes, each with a category (infra/tool/llm/validation), a severity, and a retriability flag. This turns an unstructured noise floor into actionable signal.

## Solution 1: Error Taxonomy Definition

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class ErrorCategory(str, Enum):
    LLM_API = "llm_api"           # errors from LLM provider calls
    TOOL_EXECUTION = "tool_exec"  # errors from tool invocations
    VALIDATION = "validation"     # input/output schema violations
    INFRASTRUCTURE = "infra"      # network, timeout, connection
    AUTHENTICATION = "auth"       # credential and permission failures
    RATE_LIMIT = "rate_limit"     # quota and throttling
    INTERNAL = "internal"         # agent logic / programming errors
    UNKNOWN = "unknown"


class ErrorSeverity(str, Enum):
    CRITICAL = "critical"   # session cannot continue
    HIGH = "high"           # significant quality loss
    MEDIUM = "medium"       # degraded but recoverable
    LOW = "low"             # minor, likely self-healing


@dataclass
class ErrorCode:
    code: str                        # e.g. "LLM_CONTEXT_OVERFLOW"
    category: ErrorCategory
    severity: ErrorSeverity
    retriable: bool
    description: str
    suggested_action: str = ""
```

## Solution 2: Error Code Registry

```python
from typing import Dict, List


class ErrorCodeRegistry:
    """
    Central registry of all named error codes in the agent system.
    Provides lookup by code string and category filtering.
    """

    def __init__(self) -> None:
        self._codes: Dict[str, ErrorCode] = {}

    def register(self, code: ErrorCode) -> None:
        self._codes[code.code] = code

    def get(self, code_str: str) -> Optional[ErrorCode]:
        return self._codes.get(code_str)

    def by_category(self, category: ErrorCategory) -> List[ErrorCode]:
        return [c for c in self._codes.values() if c.category == category]

    def all_codes(self) -> List[ErrorCode]:
        return list(self._codes.values())


def build_default_registry() -> ErrorCodeRegistry:
    reg = ErrorCodeRegistry()
    definitions = [
        ErrorCode("LLM_CONTEXT_OVERFLOW", ErrorCategory.LLM_API, ErrorSeverity.HIGH,
                  False, "Prompt exceeds model context window",
                  "Truncate conversation history before retrying"),
        ErrorCode("LLM_RATE_LIMITED", ErrorCategory.RATE_LIMIT, ErrorSeverity.MEDIUM,
                  True, "LLM provider returned 429",
                  "Apply exponential backoff with jitter"),
        ErrorCode("LLM_SERVER_ERROR", ErrorCategory.LLM_API, ErrorSeverity.HIGH,
                  True, "LLM provider returned 5xx",
                  "Retry with backoff; failover to secondary provider"),
        ErrorCode("LLM_AUTH_FAILED", ErrorCategory.AUTHENTICATION, ErrorSeverity.CRITICAL,
                  False, "LLM API key invalid or expired",
                  "Rotate API key and restart affected workers"),
        ErrorCode("TOOL_TIMEOUT", ErrorCategory.TOOL_EXECUTION, ErrorSeverity.MEDIUM,
                  True, "Tool call exceeded deadline",
                  "Check tool health; increase timeout if target is slow"),
        ErrorCode("TOOL_SCHEMA_MISMATCH", ErrorCategory.VALIDATION, ErrorSeverity.HIGH,
                  False, "Tool returned unexpected response shape",
                  "Update tool response schema or sanitizer"),
        ErrorCode("TOOL_DEPENDENCY_UNAVAILABLE", ErrorCategory.INFRASTRUCTURE, ErrorSeverity.HIGH,
                  True, "Tool's downstream dependency is unreachable",
                  "Check dependency health; apply graceful degradation"),
        ErrorCode("INPUT_VALIDATION_FAILED", ErrorCategory.VALIDATION, ErrorSeverity.LOW,
                  False, "User input failed validation rules",
                  "Return structured validation error to caller"),
        ErrorCode("OUTPUT_SANITIZATION_BLOCKED", ErrorCategory.VALIDATION, ErrorSeverity.MEDIUM,
                  False, "LLM output blocked by sanitization rules",
                  "Review output policy; may indicate prompt injection attempt"),
        ErrorCode("SESSION_EXPIRED", ErrorCategory.AUTHENTICATION, ErrorSeverity.LOW,
                  False, "Session token is expired",
                  "Re-authenticate and issue new session"),
        ErrorCode("NETWORK_TIMEOUT", ErrorCategory.INFRASTRUCTURE, ErrorSeverity.MEDIUM,
                  True, "TCP connection timed out",
                  "Retry; check network path to provider"),
        ErrorCode("INTERNAL_ASSERTION", ErrorCategory.INTERNAL, ErrorSeverity.CRITICAL,
                  False, "Agent logic reached unexpected state",
                  "File bug report with session context"),
        ErrorCode("UNKNOWN_ERROR", ErrorCategory.UNKNOWN, ErrorSeverity.HIGH,
                  False, "Unclassified error",
                  "Investigate; add classification rule"),
    ]
    for d in definitions:
        reg.register(d)
    return reg
```

## Solution 3: Error Classifier

```python
import re
from typing import Optional, Tuple


CLASSIFICATION_RULES: List[Tuple[str, str]] = [
    # (regex on exception string, error_code)
    (r"context.*(length|window|overflow)|too many tokens", "LLM_CONTEXT_OVERFLOW"),
    (r"rate.?limit|429|too many requests", "LLM_RATE_LIMITED"),
    (r"50[0-9]|server error|internal server", "LLM_SERVER_ERROR"),
    (r"401|403|authentication|invalid.*key|api.?key", "LLM_AUTH_FAILED"),
    (r"timeout|timed.?out|deadline", "TOOL_TIMEOUT"),
    (r"validation.*fail|schema.*mismatch|unexpected.*field", "TOOL_SCHEMA_MISMATCH"),
    (r"connection.*refused|unreachable|service.*unavailable|503", "TOOL_DEPENDENCY_UNAVAILABLE"),
    (r"network|socket|dns|name.*resolution", "NETWORK_TIMEOUT"),
    (r"session.*expir|token.*expir", "SESSION_EXPIRED"),
    (r"assertion|unexpected state|invariant", "INTERNAL_ASSERTION"),
]

_COMPILED = [(re.compile(p, re.IGNORECASE), code) for p, code in CLASSIFICATION_RULES]


class ErrorClassifier:
    """
    Maps Python exceptions to ErrorCode entries in the registry.
    Uses exception type name, message, and optional HTTP status code.
    """

    def __init__(self, registry: ErrorCodeRegistry) -> None:
        self._registry = registry

    def classify(
        self,
        exc: Exception,
        http_status: Optional[int] = None,
    ) -> ErrorCode:
        combined = f"{type(exc).__name__}: {str(exc)}"
        if http_status:
            combined = f"HTTP {http_status} {combined}"

        for pattern, code_str in _COMPILED:
            if pattern.search(combined):
                code = self._registry.get(code_str)
                if code:
                    return code

        return self._registry.get("UNKNOWN_ERROR") or ErrorCode(
            "UNKNOWN_ERROR", ErrorCategory.UNKNOWN, ErrorSeverity.HIGH,
            False, "Unclassified error"
        )
```

## Solution 4: Structured Error Event

```python
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class StructuredErrorEvent:
    error_code: str
    category: str
    severity: str
    retriable: bool
    message: str
    exception_type: str
    session_id: Optional[str] = None
    tool_name: Optional[str] = None
    model: Optional[str] = None
    http_status: Optional[int] = None
    stack_trace: Optional[str] = None
    context: Dict[str, Any] = field(default_factory=dict)
    occurred_at: float = field(default_factory=time.time)

    def to_log_dict(self) -> dict:
        return {
            "level": "ERROR",
            "error_code": self.error_code,
            "category": self.category,
            "severity": self.severity,
            "retriable": self.retriable,
            "message": self.message,
            "exception_type": self.exception_type,
            "session_id": self.session_id,
            "tool_name": self.tool_name,
            "model": self.model,
            "http_status": self.http_status,
            "context": self.context,
            "occurred_at": self.occurred_at,
        }


class StructuredErrorRecorder:
    """
    Converts raw exceptions into StructuredErrorEvents using the classifier,
    then emits them to registered sinks (logger, metrics, alerting).
    """

    def __init__(
        self,
        classifier: ErrorClassifier,
        include_stack_trace: bool = True,
    ) -> None:
        self._classifier = classifier
        self._include_stack = include_stack_trace
        self._events: List[StructuredErrorEvent] = []

    def record(
        self,
        exc: Exception,
        session_id: Optional[str] = None,
        tool_name: Optional[str] = None,
        model: Optional[str] = None,
        http_status: Optional[int] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> StructuredErrorEvent:
        code = self._classifier.classify(exc, http_status)
        event = StructuredErrorEvent(
            error_code=code.code,
            category=code.category.value,
            severity=code.severity.value,
            retriable=code.retriable,
            message=str(exc)[:500],
            exception_type=type(exc).__name__,
            session_id=session_id,
            tool_name=tool_name,
            model=model,
            http_status=http_status,
            stack_trace=traceback.format_exc()[:2000] if self._include_stack else None,
            context=context or {},
        )
        self._events.append(event)
        return event

    def recent(self, window_seconds: float = 3600.0) -> List[StructuredErrorEvent]:
        cutoff = time.time() - window_seconds
        return [e for e in self._events if e.occurred_at >= cutoff]
```

## Solution 5: Error Rate Aggregator

```python
import time
from collections import defaultdict
from typing import Dict, List


class ErrorRateAggregator:
    """
    Computes per-category and per-code error rates over a sliding window.
    Alerts when any category exceeds its threshold.
    """

    def __init__(
        self,
        recorder: StructuredErrorRecorder,
        window_seconds: float = 300.0,
        category_alert_thresholds: Optional[Dict[str, int]] = None,
    ) -> None:
        self._recorder = recorder
        self._window = window_seconds
        self._thresholds = category_alert_thresholds or {
            ErrorCategory.AUTHENTICATION.value: 3,
            ErrorCategory.INTERNAL.value: 2,
            ErrorCategory.LLM_API.value: 10,
        }

    def aggregate(self) -> dict:
        events = self._recorder.recent(self._window)
        by_category: Dict[str, int] = defaultdict(int)
        by_code: Dict[str, int] = defaultdict(int)
        critical = [e for e in events if e.severity == ErrorSeverity.CRITICAL.value]

        for e in events:
            by_category[e.category] += 1
            by_code[e.error_code] += 1

        alerts = []
        for cat, threshold in self._thresholds.items():
            count = by_category.get(cat, 0)
            if count >= threshold:
                alerts.append({
                    "type": "category_threshold_exceeded",
                    "category": cat,
                    "count": count,
                    "threshold": threshold,
                    "severity": "warning",
                })
        if critical:
            alerts.append({
                "type": "critical_errors_present",
                "count": len(critical),
                "codes": list({e.error_code for e in critical}),
                "severity": "critical",
            })

        return {
            "window_seconds": self._window,
            "total_errors": len(events),
            "by_category": dict(sorted(by_category.items(), key=lambda x: -x[1])),
            "by_code": dict(sorted(by_code.items(), key=lambda x: -x[1])),
            "alerts": alerts,
        }
```

## Solution 6: Error Taxonomy Dashboard

```python
import time


class ErrorTaxonomyDashboard:
    """
    Combines error rate aggregation, retriability distribution,
    and top error codes into a single observability report.
    """

    def __init__(
        self,
        recorder: StructuredErrorRecorder,
        aggregator: ErrorRateAggregator,
    ) -> None:
        self._recorder = recorder
        self._aggregator = aggregator

    def render(self, window_seconds: float = 300.0) -> dict:
        events = self._recorder.recent(window_seconds)
        aggregation = self._aggregator.aggregate()

        retriable = sum(1 for e in events if e.retriable)
        non_retriable = len(events) - retriable

        top_codes = list(aggregation["by_code"].items())[:5]

        return {
            "generated_at": time.time(),
            "window_seconds": window_seconds,
            "summary": {
                "total_errors": len(events),
                "retriable": retriable,
                "non_retriable": non_retriable,
                "retriability_rate": round(retriable / max(len(events), 1), 4),
                "critical_count": sum(1 for e in events if e.severity == "critical"),
            },
            "by_category": aggregation["by_category"],
            "top_error_codes": [{"code": c, "count": n} for c, n in top_codes],
            "active_alerts": aggregation["alerts"],
        }
```

## Comparison

| Approach | Classification | Structured Events | Rate Tracking | Alerting | Dashboard |
|---|---|---|---|---|---|
| ErrorClassifier | Yes (regex rules) | No | No | No | No |
| StructuredErrorRecorder | Via classifier | Yes | No | No | No |
| ErrorRateAggregator | No | No | Yes (sliding window) | Yes | No |
| ErrorTaxonomyDashboard | No | No | Via aggregator | Via aggregator | Yes |

**Best for production**: Wrap every `except` block in the tool-call loop with `StructuredErrorRecorder.record()` — pass `session_id`, `tool_name`, and `http_status` as context. Set category alert thresholds conservatively: 3 `auth` errors in 5 minutes warrants immediate on-call notification; 10 `llm_api` errors is normal during a provider incident and should route to a Slack channel, not PagerDuty. Emit `ErrorTaxonomyDashboard.render()` to your metrics system every minute so you can plot error rates by category and immediately distinguish a provider incident (spike in `llm_api`) from a deployment regression (spike in `internal`).
