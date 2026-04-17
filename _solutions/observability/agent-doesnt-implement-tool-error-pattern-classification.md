---
title: "Agent Doesn't Implement Tool Error Pattern Classification"
description: "Agents that log tool errors as raw exception strings provide no structured signal for on-call engineers: every error is a unique string, making it impossible to aggregate by failure class, distinguish transient network errors from permanent schema errors, or detect when a new error pattern emerges. Implement tool error pattern classification that normalizes raw exceptions into typed failure categories with actionability metadata."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-tool-error-pattern-classification
tags: [error-classification, tool-errors, failure-patterns, error-aggregation, structured-logging, alerting]
symptoms:
  - "Tool error logs are unstructured strings — impossible to GROUP BY failure type in dashboards"
  - "Transient connection errors and permanent schema errors are indistinguishable in metrics"
  - "New error patterns go undetected because they look like noise in a sea of unique strings"
  - "On-call engineers cannot tell whether an error spike is retryable or requires a code fix"
  - "Error rate alerts fire on transient blips that resolve on retry, causing alert fatigue"
---

## Why This Happens

Exceptions carry a type, a message, and a stack trace — but most logging pipelines serialize them as a single string. Once serialized, the structure is lost: a `ConnectionResetError` and a `TimeoutError` look like two different unique strings rather than two instances of the same `NETWORK_TRANSIENT` failure class. Error pattern classification requires intercepting exceptions before serialization, matching them against a typed taxonomy, and emitting structured records that aggregation systems can count, group, and alert on.

## Solution 1: Tool Error Taxonomy

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


class ErrorCategory(str, Enum):
    NETWORK_TRANSIENT = "network_transient"      # retry safe
    NETWORK_PERMANENT = "network_permanent"      # DNS/TLS failure — needs investigation
    AUTH_EXPIRED = "auth_expired"                # token refresh needed
    AUTH_FORBIDDEN = "auth_forbidden"            # permission issue — not retryable
    RATE_LIMITED = "rate_limited"                # back off and retry
    SCHEMA_MISMATCH = "schema_mismatch"          # tool contract broken — needs fix
    TIMEOUT = "timeout"                          # adjustable — may be retryable
    RESOURCE_NOT_FOUND = "resource_not_found"   # permanent — bad input or deleted resource
    QUOTA_EXCEEDED = "quota_exceeded"            # billing/plan limit
    INTERNAL_TOOL = "internal_tool"              # bug in tool implementation
    UNKNOWN = "unknown"                          # unclassified


@dataclass
class ClassifiedToolError:
    tool_name: str
    category: ErrorCategory
    original_type: str           # e.g. "ConnectionResetError"
    original_message: str
    retryable: bool
    actionability: str           # human-readable next step
    http_status: Optional[int] = None
    matched_pattern: str = ""
    metadata: dict = field(default_factory=dict)
```

## Solution 2: Tool Error Classifier

```python
import re
from typing import List, Optional, Tuple


_CLASSIFICATION_RULES: List[Tuple[str, str, ErrorCategory, bool, str]] = [
    # (exc_type_pattern, message_pattern, category, retryable, actionability)
    (r"ConnectionReset|BrokenPipe|ConnectionAborted",
     r".*", ErrorCategory.NETWORK_TRANSIENT, True,
     "Retry with exponential backoff"),
    (r"gaierror|Name or service not known|nodename nor servname",
     r".*", ErrorCategory.NETWORK_PERMANENT, False,
     "Check DNS configuration and network connectivity"),
    (r"SSLError|CertificateError",
     r".*", ErrorCategory.NETWORK_PERMANENT, False,
     "Inspect TLS certificate and CA bundle"),
    (r"TimeoutError|asyncio.TimeoutError|ReadTimeout|ConnectTimeout",
     r".*", ErrorCategory.TIMEOUT, True,
     "Retry; consider increasing timeout if persistent"),
    (r".*", r"401|Unauthorized|token.*expired|invalid.*token",
     ErrorCategory.AUTH_EXPIRED, True,
     "Refresh the access token and retry"),
    (r".*", r"403|Forbidden|permission denied|not authorized",
     ErrorCategory.AUTH_FORBIDDEN, False,
     "Check IAM/RBAC permissions for this tool"),
    (r".*", r"429|Too Many Requests|rate.?limit",
     ErrorCategory.RATE_LIMITED, True,
     "Back off per Retry-After header"),
    (r".*", r"402|quota.*exceeded|billing",
     ErrorCategory.QUOTA_EXCEEDED, False,
     "Upgrade plan or reduce request volume"),
    (r".*", r"404|Not Found|resource.*not.*found|does not exist",
     ErrorCategory.RESOURCE_NOT_FOUND, False,
     "Verify the resource ID/path is correct"),
    (r"ValidationError|SchemaError|KeyError|TypeError|AttributeError",
     r".*", ErrorCategory.SCHEMA_MISMATCH, False,
     "Tool contract changed — update schema or tool implementation"),
]


class ToolErrorClassifier:
    """
    Matches a raw exception against classification rules and returns
    a ClassifiedToolError with category and actionability metadata.
    """

    def classify(
        self,
        tool_name: str,
        exc: Exception,
        http_status: Optional[int] = None,
    ) -> ClassifiedToolError:
        exc_type = type(exc).__name__
        message = str(exc)
        combined = f"{exc_type}: {message}"

        if http_status:
            combined += f" HTTP/{http_status}"

        for exc_pat, msg_pat, category, retryable, action in _CLASSIFICATION_RULES:
            if re.search(exc_pat, exc_type, re.IGNORECASE) and re.search(msg_pat, message, re.IGNORECASE):
                return ClassifiedToolError(
                    tool_name=tool_name,
                    category=category,
                    original_type=exc_type,
                    original_message=message,
                    retryable=retryable,
                    actionability=action,
                    http_status=http_status,
                    matched_pattern=f"{exc_pat} / {msg_pat}",
                )

        return ClassifiedToolError(
            tool_name=tool_name,
            category=ErrorCategory.UNKNOWN,
            original_type=exc_type,
            original_message=message,
            retryable=False,
            actionability="Investigate — no classification rule matched",
            http_status=http_status,
        )
```

## Solution 3: Error Pattern Aggregator

```python
import time
from collections import defaultdict
from threading import Lock
from typing import Dict, List, Optional, Tuple


class ErrorPatternAggregator:
    """
    Accumulates ClassifiedToolErrors and reports counts by tool + category.
    Detects when a new error pattern (tool+category combination) appears
    for the first time, enabling first-occurrence alerts.
    """

    def __init__(self):
        self._lock = Lock()
        # (tool_name, category) -> list of timestamps
        self._events: Dict[Tuple[str, str], List[float]] = defaultdict(list)
        self._first_seen: Dict[Tuple[str, str], float] = {}

    def record(self, error: ClassifiedToolError) -> bool:
        """Record an error. Returns True if this is the first occurrence of this pattern."""
        key = (error.tool_name, error.category.value)
        now = time.time()
        with self._lock:
            is_new = key not in self._first_seen
            if is_new:
                self._first_seen[key] = now
            self._events[key].append(now)
        return is_new

    def counts(
        self,
        window_seconds: float = 3600.0,
    ) -> List[dict]:
        cutoff = time.time() - window_seconds
        with self._lock:
            rows = []
            for (tool, category), timestamps in self._events.items():
                recent = [t for t in timestamps if t >= cutoff]
                if recent:
                    rows.append({
                        "tool_name": tool,
                        "category": category,
                        "count": len(recent),
                        "first_seen": self._first_seen.get((tool, category)),
                    })
        return sorted(rows, key=lambda r: r["count"], reverse=True)

    def top_unknown(self, window_seconds: float = 3600.0, limit: int = 10) -> List[dict]:
        """Returns the most frequent UNKNOWN errors — candidates for new classification rules."""
        return [
            r for r in self.counts(window_seconds)
            if r["category"] == ErrorCategory.UNKNOWN.value
        ][:limit]
```

## Solution 4: Classifying Tool Dispatcher

```python
import time
from typing import Any, Callable, Optional


class ClassifyingToolDispatcher:
    """
    Wraps tool calls, catches exceptions, classifies them, and emits
    structured ClassifiedToolError records to the aggregator and logger.
    Re-raises the original exception so the agent's retry logic still fires.
    """

    def __init__(
        self,
        classifier: ToolErrorClassifier,
        aggregator: ErrorPatternAggregator,
        on_classified: Optional[Callable[[ClassifiedToolError], None]] = None,
    ):
        self._classifier = classifier
        self._aggregator = aggregator
        self._on_classified = on_classified

    async def dispatch(
        self,
        tool_name: str,
        fn: Callable,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        try:
            return await fn(*args, **kwargs)
        except Exception as exc:
            http_status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
            classified = self._classifier.classify(tool_name, exc, http_status)
            is_new = self._aggregator.record(classified)
            classified.metadata["is_new_pattern"] = is_new

            if self._on_classified:
                self._on_classified(classified)

            raise
```

## Solution 5: Error Trend Detector

```python
import time
from typing import List, Optional, Tuple


class ErrorTrendDetector:
    """
    Compares error counts in a recent window against a baseline window
    to detect emerging spikes in specific error categories.
    """

    def __init__(
        self,
        aggregator: ErrorPatternAggregator,
        spike_multiplier: float = 3.0,
        min_recent_count: int = 5,
    ):
        self._aggregator = aggregator
        self._multiplier = spike_multiplier
        self._min_count = min_recent_count

    def detect_spikes(
        self,
        recent_window_seconds: float = 300.0,
        baseline_window_seconds: float = 3600.0,
    ) -> List[dict]:
        recent = {
            (r["tool_name"], r["category"]): r["count"]
            for r in self._aggregator.counts(recent_window_seconds)
        }
        baseline = {
            (r["tool_name"], r["category"]): r["count"]
            for r in self._aggregator.counts(baseline_window_seconds)
        }

        spikes = []
        scale = recent_window_seconds / baseline_window_seconds
        for key, recent_count in recent.items():
            if recent_count < self._min_count:
                continue
            baseline_rate = baseline.get(key, 0) * scale
            if baseline_rate == 0 or recent_count / max(baseline_rate, 1) >= self._multiplier:
                tool, category = key
                spikes.append({
                    "tool_name": tool,
                    "category": category,
                    "recent_count": recent_count,
                    "baseline_rate_normalized": round(baseline_rate, 1),
                    "multiplier": round(recent_count / max(baseline_rate, 0.001), 1),
                })

        return sorted(spikes, key=lambda s: s["multiplier"], reverse=True)
```

## Solution 6: Tool Error Classification Dashboard

```python
import time
from typing import Optional


class ToolErrorClassificationDashboard:
    """
    Combines aggregated error counts, spike detection, and top unknown
    patterns into a single operational report for on-call visibility.
    """

    def __init__(
        self,
        aggregator: ErrorPatternAggregator,
        trend_detector: ErrorTrendDetector,
    ):
        self._aggregator = aggregator
        self._trend_detector = trend_detector

    def render(self, window_seconds: float = 3600.0) -> dict:
        counts = self._aggregator.counts(window_seconds)
        by_category: dict = {}
        for row in counts:
            cat = row["category"]
            by_category[cat] = by_category.get(cat, 0) + row["count"]

        return {
            "generated_at": time.time(),
            "window_seconds": window_seconds,
            "total_classified_errors": sum(r["count"] for r in counts),
            "by_category": by_category,
            "top_patterns": counts[:10],
            "spikes": self._trend_detector.detect_spikes(
                recent_window_seconds=300.0,
                baseline_window_seconds=window_seconds,
            ),
            "top_unknown_patterns": self._aggregator.top_unknown(window_seconds),
        }
```

## Comparison

| Approach | Typed Classification | Retryability Flag | First-Occurrence Alert | Spike Detection | Dashboard |
|---|---|---|---|---|---|
| ToolErrorClassifier | Yes (regex rules) | Yes | No | No | No |
| ErrorPatternAggregator | No | No | Yes | No | No |
| ClassifyingToolDispatcher | Via classifier | Via classifier | Via aggregator | No | No |
| ErrorTrendDetector | No | No | No | Yes (multiplier) | No |
| ToolErrorClassificationDashboard | No | No | No | No | Yes |

**Best for production**: Emit `ClassifiedToolError` as a structured log field (not a string) — this unlocks GROUP BY queries on `category` and `tool_name` in any log aggregation platform. Alert on `is_new_pattern=true` immediately: a brand-new error pattern appearing in production almost always warrants investigation regardless of count. Set the retryable field in retry logic so `NETWORK_TRANSIENT` and `RATE_LIMITED` errors retry automatically while `SCHEMA_MISMATCH` and `AUTH_FORBIDDEN` surface immediately to operators. Review `top_unknown_patterns` weekly and promote recurring unknowns into classification rules — an unknown that appears more than 10 times per hour has earned a named category.
