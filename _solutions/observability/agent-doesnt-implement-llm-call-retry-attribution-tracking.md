---
title: "Agent Doesn't Implement LLM Call Retry Attribution Tracking"
description: "Agents that retry failed LLM calls log each attempt as an independent event, making it impossible to attribute retried calls to their original request, measure true first-attempt success rates, or identify which failure types are driving retry volume. Implement retry attribution tracking that links all attempts of a logical LLM call under a single root ID, records the cause of each failure, and surfaces retry-driven latency separately from base latency."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-llm-call-retry-attribution-tracking
tags: [retry-attribution, llm-call-tracking, failure-classification, retry-metrics, observability, call-lineage]
symptoms:
  - "LLM call success rate appears high because retried successful calls mask first-attempt failures"
  - "Retry attempts are logged as independent calls — no way to group them under one request"
  - "Cannot determine what fraction of LLM call latency is retry overhead vs. first-attempt latency"
  - "No record of which error type (rate limit, timeout, context-length) caused each retry"
  - "Retry volume spikes are invisible — only total call volume is tracked"
---

## Why This Happens

When a retry loop calls the LLM API multiple times for one logical request, each attempt typically creates its own log event with a new ID. Downstream metrics treat these as independent calls. This inflates call volume, hides first-attempt failure rates, and makes it impossible to answer "how much of our P95 latency is retry overhead?" Retry attribution requires assigning a stable root call ID to the original attempt and linking all retries to it with an attempt sequence number and a failure reason for each non-final attempt.

## Solution 1: LLM Call Attempt Record

```python
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class LLMCallFailureType(str, Enum):
    RATE_LIMIT = "rate_limit"
    TIMEOUT = "timeout"
    CONTEXT_LENGTH = "context_length"
    SERVER_ERROR = "server_error"
    INVALID_RESPONSE = "invalid_response"
    NETWORK_ERROR = "network_error"
    UNKNOWN = "unknown"


@dataclass
class LLMCallAttempt:
    root_call_id: str
    attempt_number: int          # 1-indexed; attempt 1 is the first try
    model: str
    started_at: float = field(default_factory=time.time)
    ended_at: Optional[float] = None
    success: bool = False
    failure_type: Optional[LLMCallFailureType] = None
    failure_message: str = ""
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    session_id: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def latency_ms(self) -> Optional[float]:
        if self.ended_at is None:
            return None
        return round((self.ended_at - self.started_at) * 1000, 2)

    @property
    def is_retry(self) -> bool:
        return self.attempt_number > 1
```

## Solution 2: Failure Type Classifier

```python
from typing import Optional


class LLMFailureTypeClassifier:
    """
    Maps exception types and HTTP status codes from LLM API clients
    to the LLMCallFailureType enum for consistent attribution.
    """

    def classify(self, exc: Exception) -> LLMCallFailureType:
        exc_name = type(exc).__name__
        status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
        message = str(exc).lower()

        if status == 429 or "rate limit" in message or "rate_limit" in message:
            return LLMCallFailureType.RATE_LIMIT

        if status in (500, 502, 503, 504) or "server error" in message:
            return LLMCallFailureType.SERVER_ERROR

        if "timeout" in exc_name.lower() or "timeout" in message:
            return LLMCallFailureType.TIMEOUT

        if "context" in message and ("length" in message or "too long" in message or "max_tokens" in message):
            return LLMCallFailureType.CONTEXT_LENGTH

        if "connection" in exc_name.lower() or "network" in message:
            return LLMCallFailureType.NETWORK_ERROR

        if "invalid" in message and "response" in message:
            return LLMCallFailureType.INVALID_RESPONSE

        return LLMCallFailureType.UNKNOWN
```

## Solution 3: LLM Call Attempt Store

```python
import time
from collections import defaultdict
from threading import Lock
from typing import Dict, List, Optional


class LLMCallAttemptStore:
    """
    Accumulates all attempts for each root call ID and supports
    queries for retry analysis: first-attempt success rate,
    average retry count, failure type distribution.
    """

    def __init__(self, max_root_calls: int = 10000):
        self._max = max_root_calls
        self._attempts: Dict[str, List[LLMCallAttempt]] = defaultdict(list)
        self._insertion_order: List[str] = []
        self._lock = Lock()

    def record(self, attempt: LLMCallAttempt) -> None:
        with self._lock:
            if attempt.root_call_id not in self._attempts:
                self._insertion_order.append(attempt.root_call_id)
                if len(self._insertion_order) > self._max:
                    oldest = self._insertion_order.pop(0)
                    del self._attempts[oldest]
            self._attempts[attempt.root_call_id].append(attempt)

    def attempts_for(self, root_call_id: str) -> List[LLMCallAttempt]:
        with self._lock:
            return list(self._attempts.get(root_call_id, []))

    def recent_roots(self, window_seconds: float = 3600.0) -> List[str]:
        cutoff = time.time() - window_seconds
        with self._lock:
            return [
                rid for rid, attempts in self._attempts.items()
                if any(a.started_at >= cutoff for a in attempts)
            ]

    def summary(self, window_seconds: float = 3600.0) -> dict:
        roots = self.recent_roots(window_seconds)
        total_roots = len(roots)
        first_attempt_successes = 0
        total_retries = 0
        failure_types: Dict[str, int] = defaultdict(int)
        retry_latencies: List[float] = []

        for rid in roots:
            attempts = self._attempts[rid]
            sorted_attempts = sorted(attempts, key=lambda a: a.attempt_number)
            if sorted_attempts and sorted_attempts[0].success:
                first_attempt_successes += 1
            retries = [a for a in sorted_attempts if a.is_retry]
            total_retries += len(retries)
            for a in sorted_attempts[:-1]:
                if a.failure_type:
                    failure_types[a.failure_type.value] += 1
                if a.latency_ms:
                    retry_latencies.append(a.latency_ms)

        return {
            "window_seconds": window_seconds,
            "total_logical_calls": total_roots,
            "first_attempt_success_rate": round(first_attempt_successes / max(total_roots, 1), 4),
            "total_retries": total_retries,
            "avg_retries_per_call": round(total_retries / max(total_roots, 1), 2),
            "failure_type_distribution": dict(failure_types),
            "avg_retry_latency_ms": round(
                sum(retry_latencies) / max(len(retry_latencies), 1), 2
            ) if retry_latencies else None,
        }
```

## Solution 4: Attributed LLM Call Executor

```python
import asyncio
import time
import uuid
from typing import Any, Callable, Optional


class AttributedLLMCallExecutor:
    """
    Executes LLM API calls with retry attribution. Each logical call
    gets a root_call_id; each attempt is recorded with its outcome
    and failure type so retries are linked to their original request.
    """

    def __init__(
        self,
        store: LLMCallAttemptStore,
        classifier: LLMFailureTypeClassifier,
        max_attempts: int = 3,
        base_delay_seconds: float = 1.0,
    ):
        self._store = store
        self._classifier = classifier
        self._max_attempts = max_attempts
        self._base_delay = base_delay_seconds

    async def call(
        self,
        llm_fn: Callable,
        model: str,
        session_id: str = "",
        **kwargs: Any,
    ) -> Any:
        root_call_id = str(uuid.uuid4())[:16]
        last_exc: Optional[Exception] = None

        for attempt_num in range(1, self._max_attempts + 1):
            attempt = LLMCallAttempt(
                root_call_id=root_call_id,
                attempt_number=attempt_num,
                model=model,
                session_id=session_id,
            )
            try:
                result = await llm_fn(**kwargs)
                attempt.success = True
                attempt.ended_at = time.time()
                usage = getattr(result, "usage", None)
                if usage:
                    attempt.input_tokens = getattr(usage, "input_tokens", None)
                    attempt.output_tokens = getattr(usage, "output_tokens", None)
                self._store.record(attempt)
                return result
            except Exception as exc:
                last_exc = exc
                attempt.success = False
                attempt.ended_at = time.time()
                attempt.failure_type = self._classifier.classify(exc)
                attempt.failure_message = str(exc)[:200]
                self._store.record(attempt)
                if attempt_num < self._max_attempts:
                    await asyncio.sleep(self._base_delay * (2 ** (attempt_num - 1)))

        raise last_exc
```

## Solution 5: Retry Overhead Calculator

```python
from typing import List, Optional


class RetryOverheadCalculator:
    """
    Computes retry-driven latency overhead for a set of root call IDs.
    Separates base first-attempt latency from retry overhead so the
    cost of retry behavior is visible in dashboards.
    """

    def __init__(self, store: LLMCallAttemptStore):
        self._store = store

    def overhead_for(self, root_call_id: str) -> dict:
        attempts = sorted(
            self._store.attempts_for(root_call_id),
            key=lambda a: a.attempt_number,
        )
        if not attempts:
            return {"root_call_id": root_call_id, "status": "not_found"}

        first = attempts[0]
        retries = attempts[1:]
        retry_ms = sum(a.latency_ms or 0 for a in retries)
        total_ms = sum(a.latency_ms or 0 for a in attempts)

        return {
            "root_call_id": root_call_id,
            "attempt_count": len(attempts),
            "success": any(a.success for a in attempts),
            "first_attempt_ms": first.latency_ms,
            "retry_overhead_ms": round(retry_ms, 2),
            "total_ms": round(total_ms, 2),
            "retry_overhead_pct": round(retry_ms / max(total_ms, 1) * 100, 1),
        }

    def aggregate_overhead(self, window_seconds: float = 3600.0) -> dict:
        roots = self._store.recent_roots(window_seconds)
        overheads = [self.overhead_for(r) for r in roots]
        retry_ms_values = [o["retry_overhead_ms"] for o in overheads if o.get("attempt_count", 1) > 1]
        return {
            "calls_with_retries": len(retry_ms_values),
            "total_calls": len(roots),
            "avg_retry_overhead_ms": round(
                sum(retry_ms_values) / max(len(retry_ms_values), 1), 2
            ) if retry_ms_values else 0,
        }
```

## Solution 6: Retry Attribution Dashboard

```python
import time


class LLMCallRetryAttributionDashboard:
    """
    Combines store summary, retry overhead analysis, and failure type
    distribution into a single operational report.
    """

    def __init__(
        self,
        store: LLMCallAttemptStore,
        calculator: RetryOverheadCalculator,
    ):
        self._store = store
        self._calculator = calculator

    def render(self, window_seconds: float = 3600.0) -> dict:
        summary = self._store.summary(window_seconds)
        overhead = self._calculator.aggregate_overhead(window_seconds)
        return {
            "generated_at": time.time(),
            "window_seconds": window_seconds,
            "call_summary": summary,
            "retry_overhead": overhead,
            "health": {
                "first_attempt_success_rate": summary["first_attempt_success_rate"],
                "dominant_failure_type": max(
                    summary["failure_type_distribution"],
                    key=lambda k: summary["failure_type_distribution"][k],
                    default=None,
                ),
            },
        }
```

## Comparison

| Approach | Root Call Grouping | Failure Classification | First-Attempt Rate | Retry Overhead Ms | Dashboard |
|---|---|---|---|---|---|
| LLMCallAttemptStore | Yes (root_call_id) | No | Yes | No | No |
| LLMFailureTypeClassifier | No | Yes | No | No | No |
| AttributedLLMCallExecutor | Yes | Via classifier | Via store | No | No |
| RetryOverheadCalculator | Via store | No | No | Yes | No |
| LLMCallRetryAttributionDashboard | No | No | No | No | Yes |

**Best for production**: Emit `root_call_id` as a structured log field on every attempt so log search can group all retries under one request. Track `first_attempt_success_rate` as a primary SLO metric — it reflects true API reliability without masking failures behind successful retries. When `dominant_failure_type` is `rate_limit` for more than 30% of retried calls, the retry strategy is correct but the upstream quota needs increasing; when it is `timeout`, investigate network path or model endpoint latency. Set `max_attempts=3` — beyond three attempts, retrying typically indicates a sustained outage that requires circuit-breaker intervention rather than more retries.
