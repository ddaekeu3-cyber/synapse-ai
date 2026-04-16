---
title: "Agent Doesn't Implement Tool Call Retry with Modified Arguments on Validation Error"
description: "Agents that retry failed tool calls with identical arguments loop indefinitely when the failure is caused by invalid input rather than a transient network issue. A date format mismatch, an out-of-range value, or an unknown enum variant will fail on every attempt. Implement argument-correcting retry that classifies validation errors, applies targeted argument fixes, and retries with the corrected arguments rather than the original ones."
date: 2026-04-16
difficulty: intermediate
category: reliability
slug: agent-doesnt-implement-tool-call-retry-with-modified-arguments-on-validation-error
tags: [retry, argument-correction, validation-error, argument-fixing, smart-retry, error-recovery]
symptoms:
  - "Tool call fails with 'invalid date format' and retries identically 3 times before giving up"
  - "Enum validation error causes infinite retry loop because args are never modified"
  - "Agent does not distinguish transient errors (retry same args) from validation errors (fix args)"
  - "No mechanism to extract the corrected value from a validation error response"
  - "Date, format, and range errors always result in final failure rather than automatic correction"
---

## Why This Happens

Retry logic is typically implemented at the transport level: if a request fails, wait and retry. This works for network errors but is counterproductive for semantic validation errors. A validation error means the request was received and rejected — retrying with the same arguments guarantees the same rejection. Argument-correcting retry requires classifying errors by type, extracting correction hints from error messages (e.g., "expected ISO 8601 format"), applying targeted fixes to the relevant arguments, and retrying only when a valid fix can be constructed.

## Solution 1: Error Classifier

```python
import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class ErrorCategory(str, Enum):
    TRANSIENT = "transient"           # network, timeout, 5xx — retry same args
    VALIDATION = "validation"         # invalid args — fix then retry
    AUTH = "auth"                     # credentials invalid — do not retry
    NOT_FOUND = "not_found"           # resource missing — do not retry
    RATE_LIMIT = "rate_limit"         # quota exceeded — retry with backoff
    UNKNOWN = "unknown"               # default — retry same args once


@dataclass
class ClassifiedError:
    category: ErrorCategory
    original_error: Exception
    error_message: str
    validation_hint: Optional[str] = None   # extracted expected format/value


class ToolErrorClassifier:
    """
    Classifies tool call exceptions into error categories.
    Extracts validation hints from error messages for argument correction.
    """

    _TRANSIENT_PATTERNS = [
        re.compile(r"connection (refused|reset|timed? ?out)", re.I),
        re.compile(r"(timeout|503|502|504|service unavailable)", re.I),
        re.compile(r"(network|socket) error", re.I),
    ]
    _VALIDATION_PATTERNS = [
        re.compile(r"(invalid|malformed|unexpected|unrecognized|unknown) (format|value|field|parameter|type|enum|date)", re.I),
        re.compile(r"(must be|expected|should be) (iso ?8601|yyyy|date|integer|string|one of)", re.I),
        re.compile(r"(out of range|exceeds maximum|below minimum|not in \[)", re.I),
        re.compile(r"(required field|missing (required |field ))", re.I),
        re.compile(r"validation (error|failed)", re.I),
    ]
    _RATE_LIMIT_PATTERNS = [
        re.compile(r"(rate limit|too many requests|429|quota exceeded)", re.I),
    ]
    _AUTH_PATTERNS = [
        re.compile(r"(unauthorized|forbidden|401|403|invalid (api |)key|authentication failed)", re.I),
    ]

    def classify(self, exc: Exception) -> ClassifiedError:
        msg = str(exc)

        for pattern in self._AUTH_PATTERNS:
            if pattern.search(msg):
                return ClassifiedError(ErrorCategory.AUTH, exc, msg)

        for pattern in self._RATE_LIMIT_PATTERNS:
            if pattern.search(msg):
                return ClassifiedError(ErrorCategory.RATE_LIMIT, exc, msg)

        for pattern in self._VALIDATION_PATTERNS:
            if pattern.search(msg):
                hint = self._extract_hint(msg)
                return ClassifiedError(ErrorCategory.VALIDATION, exc, msg, hint)

        for pattern in self._TRANSIENT_PATTERNS:
            if pattern.search(msg):
                return ClassifiedError(ErrorCategory.TRANSIENT, exc, msg)

        return ClassifiedError(ErrorCategory.UNKNOWN, exc, msg)

    @staticmethod
    def _extract_hint(message: str) -> Optional[str]:
        """Extract expected format or value from error message."""
        patterns = [
            re.compile(r"expected[:\s]+([^\.,;]+)", re.I),
            re.compile(r"must be[:\s]+([^\.,;]+)", re.I),
            re.compile(r"one of[:\s]+\[([^\]]+)\]", re.I),
            re.compile(r"format[:\s]+([^\.,;]+)", re.I),
        ]
        for p in patterns:
            m = p.search(message)
            if m:
                return m.group(1).strip()[:100]
        return None
```

## Solution 2: Argument Corrector

```python
import re
from datetime import datetime, timezone
from typing import Any, Dict, Optional


class ToolArgumentCorrector:
    """
    Applies targeted corrections to tool arguments based on
    the classified validation error and extracted hint.
    Returns None if no correction can be applied.
    """

    def correct(
        self,
        args: Dict[str, Any],
        error: ClassifiedError,
    ) -> Optional[Dict[str, Any]]:
        if error.category != ErrorCategory.VALIDATION:
            return None

        msg = error.error_message.lower()
        hint = (error.validation_hint or "").lower()
        corrected = dict(args)
        applied = False

        # Date format corrections
        if "date" in msg or "iso" in msg or "8601" in msg:
            for key, value in corrected.items():
                if "date" in key.lower() and isinstance(value, str):
                    fixed = self._fix_date(value)
                    if fixed and fixed != value:
                        corrected[key] = fixed
                        applied = True

        # Integer conversion
        if "integer" in msg or "int" in msg:
            for key, value in corrected.items():
                if isinstance(value, str) and value.isdigit():
                    corrected[key] = int(value)
                    applied = True

        # Enum correction (pick first valid option from hint)
        if "one of" in hint or "must be" in hint:
            options = re.findall(r"['\"]?([a-zA-Z_][a-zA-Z0-9_]*)['\"]?", hint)
            for key, value in corrected.items():
                if isinstance(value, str) and options and value not in options:
                    # Use the first option as fallback
                    corrected[key] = options[0]
                    applied = True
                    break

        return corrected if applied else None

    @staticmethod
    def _fix_date(value: str) -> Optional[str]:
        """Attempt to parse and reformat a date string to ISO 8601."""
        formats = [
            "%m/%d/%Y", "%d/%m/%Y", "%Y%m%d",
            "%d-%m-%Y", "%m-%d-%Y", "%B %d, %Y",
            "%b %d, %Y", "%d %B %Y",
        ]
        for fmt in formats:
            try:
                dt = datetime.strptime(value.strip(), fmt)
                return dt.date().isoformat()
            except ValueError:
                continue
        return None
```

## Solution 3: Argument-Correcting Retry Executor

```python
import asyncio
import time
from typing import Any, Callable, Dict, Optional


class ArgumentCorrectingRetryExecutor:
    """
    Retries failed tool calls with argument corrections for validation errors
    and standard exponential backoff for transient errors.
    """

    def __init__(
        self,
        classifier: ToolErrorClassifier,
        corrector: ToolArgumentCorrector,
        max_retries: int = 3,
        base_delay_seconds: float = 1.0,
        backoff_multiplier: float = 2.0,
    ):
        self._classifier = classifier
        self._corrector = corrector
        self._max_retries = max_retries
        self._base_delay = base_delay_seconds
        self._backoff = backoff_multiplier

    async def execute(
        self,
        tool_name: str,
        args: Dict[str, Any],
        dispatch_fn: Callable[[str, Dict[str, Any]], Any],
    ) -> Any:
        current_args = dict(args)
        last_error: Optional[Exception] = None

        for attempt in range(1, self._max_retries + 2):
            try:
                return await dispatch_fn(tool_name, current_args)
            except Exception as exc:
                classified = self._classifier.classify(exc)
                last_error = exc

                if classified.category in (ErrorCategory.AUTH, ErrorCategory.NOT_FOUND):
                    raise  # No retry for these

                if attempt > self._max_retries:
                    break

                if classified.category == ErrorCategory.VALIDATION:
                    corrected = self._corrector.correct(current_args, classified)
                    if corrected:
                        current_args = corrected
                        continue  # No delay for argument correction — retry immediately
                    else:
                        raise  # Can't fix — don't retry

                if classified.category == ErrorCategory.RATE_LIMIT:
                    delay = self._base_delay * (self._backoff ** attempt) * 5
                else:
                    delay = self._base_delay * (self._backoff ** (attempt - 1))

                await asyncio.sleep(delay)

        raise last_error
```

## Solution 4: Retry Outcome Tracker

```python
import time
from collections import deque
from threading import Lock
from typing import Deque


class RetryOutcomeTracker:
    """
    Records retry attempts and argument corrections for analysis.
    Identifies which tools most frequently require argument corrections.
    """

    def __init__(self, max_records: int = 10_000):
        self._records: Deque[dict] = deque(maxlen=max_records)
        self._lock = Lock()

    def record(
        self,
        tool_name: str,
        attempt: int,
        succeeded: bool,
        error_category: Optional[str],
        args_corrected: bool,
    ) -> None:
        with self._lock:
            self._records.append({
                "ts": time.time(),
                "tool_name": tool_name,
                "attempt": attempt,
                "succeeded": succeeded,
                "error_category": error_category,
                "args_corrected": args_corrected,
            })

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        with self._lock:
            recent = [r for r in self._records if r["ts"] >= cutoff]
        if not recent:
            return {"attempts": 0}

        corrections = [r for r in recent if r["args_corrected"]]
        by_tool: dict = {}
        for r in recent:
            t = r["tool_name"]
            if t not in by_tool:
                by_tool[t] = {"retries": 0, "corrections": 0}
            if r["attempt"] > 1:
                by_tool[t]["retries"] += 1
            if r["args_corrected"]:
                by_tool[t]["corrections"] += 1

        return {
            "window_seconds": window_seconds,
            "total_attempts": len(recent),
            "argument_corrections": len(corrections),
            "correction_rate": round(len(corrections) / len(recent), 4),
            "by_tool": by_tool,
        }
```

## Solution 5: Correcting Retry Dashboard

```python
import time


class ArgumentCorrectingRetryDashboard:
    """
    Renders retry outcomes, correction rates, and tool-level
    failure patterns for reliability tuning.
    """

    def __init__(
        self,
        tracker: RetryOutcomeTracker,
        classifier: ToolErrorClassifier,
    ):
        self._tracker = tracker
        self._classifier = classifier

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "retry_summary_1h": self._tracker.summary(3600.0),
            "correctable_error_patterns": [
                p.pattern for p in classifier._VALIDATION_PATTERNS
                for classifier in [self._classifier]
            ],
        }
```

## Solution 6: Schema-Guided Argument Fixer

```python
from typing import Any, Dict, Optional


class SchemaGuidedArgumentFixer:
    """
    Uses tool schema metadata to apply type coercions and format corrections
    without relying on error message parsing.
    """

    def fix(
        self,
        args: Dict[str, Any],
        schema: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        schema: JSON Schema properties dict for the tool's arguments.
        Returns a copy of args with type coercions applied.
        """
        fixed = dict(args)
        properties = schema.get("properties", {})

        for field, field_schema in properties.items():
            if field not in fixed:
                continue
            value = fixed[field]
            expected_type = field_schema.get("type")
            fmt = field_schema.get("format", "")

            if expected_type == "integer" and isinstance(value, str):
                try:
                    fixed[field] = int(value)
                except ValueError:
                    pass
            elif expected_type == "number" and isinstance(value, str):
                try:
                    fixed[field] = float(value)
                except ValueError:
                    pass
            elif expected_type == "string" and not isinstance(value, str):
                fixed[field] = str(value)
            elif fmt == "date" and isinstance(value, str) and "T" in value:
                # Strip time component from datetime if date expected
                fixed[field] = value.split("T")[0]

        return fixed
```

## Comparison

| Approach | Error Classification | Argument Correction | Retry Logic | Schema-Guided Fix | Outcome Tracking |
|---|---|---|---|---|---|
| ToolErrorClassifier | Yes (5 categories) | No | No | No | No |
| ToolArgumentCorrector | Via error hint | Yes (date/int/enum) | No | No | No |
| ArgumentCorrectingRetryExecutor | Via classifier | Via corrector | Yes | No | No |
| SchemaGuidedArgumentFixer | No | Yes (type coercion) | No | Yes | No |
| RetryOutcomeTracker | No | No | No | No | Yes |

**Best for production**: Classify errors before retrying — never retry AUTH or NOT_FOUND errors. Apply `SchemaGuidedArgumentFixer` before trying `ToolArgumentCorrector`: schema-based fixes are deterministic while message-parsing fixes are heuristic. Set `max_retries=2` for validation errors with correction (one attempt at the fix is usually sufficient); use `max_retries=3` with exponential backoff for transient errors. Monitor `correction_rate` via `RetryOutcomeTracker` — high rates for a specific tool indicate the LLM is consistently generating incorrect argument formats for that tool and the tool schema or system prompt should be updated.
