---
title: "Agent Doesn't Implement Exponential Backoff with Jitter for API Retries"
description: "Agents that retry failed API calls immediately or at fixed intervals cause thundering-herd problems: all instances retry simultaneously after a service blip, overwhelming the recovering API. Implement exponential backoff with full jitter that randomizes retry intervals across the exponential envelope, respects Retry-After headers from 429 responses, and caps total retry duration to prevent session hangs."
date: 2026-04-16
difficulty: intermediate
category: reliability
slug: agent-doesnt-implement-exponential-backoff-with-jitter-for-api-retries
tags: [exponential-backoff, jitter, retry-strategy, rate-limiting, thundering-herd, resilience]
symptoms:
  - "All agent instances retry simultaneously after a transient API failure, re-triggering the outage"
  - "Fixed 1-second retry interval causes 429 cascade when hundreds of sessions retry at once"
  - "Agent ignores Retry-After header from 429 responses and retries too early"
  - "Retry loop runs indefinitely — a permanent API error hangs the session for minutes"
  - "No distinction between retryable errors (429, 503) and non-retryable ones (400, 401)"
---

## Why This Happens

The simplest retry is `await asyncio.sleep(1); await api_call()` in a loop. This creates a thundering herd: every instance that received the same 503 at the same time retries at t+1s, t+2s, t+3s — all in synchrony. Exponential backoff spreads retries out exponentially (1s, 2s, 4s, 8s…), and full jitter randomizes each delay within the exponential envelope (0–1s, 0–2s, 0–4s…), ensuring no two instances retry at exactly the same moment. Retry-After header compliance ensures the agent does not retry before the server has explicitly said it is ready.

## Solution 1: Retry Policy

```python
from dataclasses import dataclass, field
from typing import FrozenSet, Tuple, Type


@dataclass
class RetryPolicy:
    max_attempts: int = 4
    base_delay_seconds: float = 1.0
    max_delay_seconds: float = 60.0
    multiplier: float = 2.0
    jitter: str = "full"          # "full" | "equal" | "none"
    max_total_seconds: float = 120.0
    retryable_status_codes: FrozenSet[int] = field(
        default_factory=lambda: frozenset({429, 500, 502, 503, 504})
    )
    retryable_exceptions: Tuple[Type[Exception], ...] = field(
        default_factory=lambda: (TimeoutError, ConnectionError, OSError)
    )

    def is_retryable_status(self, status_code: int) -> bool:
        return status_code in self.retryable_status_codes

    def is_retryable_exception(self, exc: Exception) -> bool:
        return isinstance(exc, self.retryable_exceptions)
```

## Solution 2: Jittered Delay Calculator

```python
import random
from typing import Optional


class JitteredDelayCalculator:
    """
    Computes the retry delay for a given attempt number using
    exponential backoff with configurable jitter strategy.

    full jitter:  uniform(0, min(cap, base * mult^n))
    equal jitter: half-fixed + half-random: cap/2 + uniform(0, cap/2)
    none:         min(cap, base * mult^n)
    """

    def __init__(self, policy: RetryPolicy):
        self._policy = policy

    def delay(self, attempt: int, retry_after: Optional[float] = None) -> float:
        """
        attempt: 0-indexed attempt number (0 = first retry)
        retry_after: value from Retry-After header in seconds, if present
        """
        if retry_after is not None and retry_after > 0:
            return retry_after + random.uniform(0, 0.5)

        p = self._policy
        expo = p.base_delay_seconds * (p.multiplier ** attempt)
        cap = min(expo, p.max_delay_seconds)

        if p.jitter == "full":
            delay = random.uniform(0, cap)
        elif p.jitter == "equal":
            delay = cap / 2.0 + random.uniform(0, cap / 2.0)
        else:
            delay = cap

        return round(delay, 3)
```

## Solution 3: Retry-After Header Parser

```python
import email.utils
import re
import time
from typing import Optional


class RetryAfterParser:
    """
    Parses the Retry-After header from HTTP responses.
    Supports both integer seconds and HTTP-date formats.
    """

    @staticmethod
    def parse(header_value: Optional[str]) -> Optional[float]:
        if not header_value:
            return None
        header_value = header_value.strip()

        if re.fullmatch(r"\d+", header_value):
            return float(header_value)

        try:
            retry_at = email.utils.parsedate_to_datetime(header_value).timestamp()
            delay = retry_at - time.time()
            return max(0.0, delay)
        except Exception:
            return None
```

## Solution 4: Exponential Backoff Retry Executor

```python
import asyncio
import time
from typing import Any, Callable, Optional


class ExponentialBackoffRetryExecutor:
    """
    Executes an async callable with exponential backoff + full jitter retries.
    Respects Retry-After headers returned via exception attributes.
    Aborts if total elapsed time exceeds policy.max_total_seconds.
    """

    def __init__(
        self,
        policy: RetryPolicy,
        calculator: JitteredDelayCalculator,
        parser: RetryAfterParser,
    ):
        self._policy = policy
        self._calc = calculator
        self._parser = parser
        self._attempts_made = 0
        self._total_delay_seconds = 0.0

    async def execute(self, fn: Callable, *args: Any, **kwargs: Any) -> Any:
        start = time.time()
        last_exc: Optional[Exception] = None

        for attempt in range(self._policy.max_attempts):
            self._attempts_made += 1
            if time.time() - start >= self._policy.max_total_seconds:
                raise TimeoutError(
                    f"Retry budget exhausted after {time.time()-start:.1f}s"
                ) from last_exc

            try:
                return await fn(*args, **kwargs)
            except Exception as exc:
                last_exc = exc
                retry_after = self._extract_retry_after(exc)
                retryable = (
                    self._policy.is_retryable_exception(exc)
                    or self._is_retryable_http_error(exc)
                )
                if not retryable:
                    raise
                if attempt == self._policy.max_attempts - 1:
                    break

                delay = self._calc.delay(attempt, retry_after)
                remaining = self._policy.max_total_seconds - (time.time() - start)
                delay = min(delay, max(0, remaining - 0.1))
                self._total_delay_seconds += delay
                await asyncio.sleep(delay)

        raise last_exc

    def _extract_retry_after(self, exc: Exception) -> Optional[float]:
        raw = getattr(exc, "retry_after_seconds", None)
        if raw is not None:
            return float(raw)
        headers = getattr(exc, "headers", None) or {}
        return self._parser.parse(headers.get("Retry-After"))

    def _is_retryable_http_error(self, exc: Exception) -> bool:
        status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
        if status is not None:
            return self._policy.is_retryable_status(int(status))
        return False

    def stats(self) -> dict:
        return {
            "attempts_made": self._attempts_made,
            "total_delay_seconds": round(self._total_delay_seconds, 3),
        }
```

## Solution 5: Per-Endpoint Retry Manager

```python
from typing import Any, Callable, Dict


class PerEndpointRetryManager:
    """
    Maintains separate retry policies for each registered endpoint.
    Allows different APIs to have different backoff profiles.
    """

    def __init__(self):
        self._policies: Dict[str, RetryPolicy] = {}
        self._parser = RetryAfterParser()

    def register(self, endpoint: str, policy: RetryPolicy) -> None:
        self._policies[endpoint] = policy

    def get_policy(self, endpoint: str) -> RetryPolicy:
        return self._policies.get(endpoint, RetryPolicy())

    def _make_executor(self, policy: RetryPolicy) -> ExponentialBackoffRetryExecutor:
        return ExponentialBackoffRetryExecutor(
            policy, JitteredDelayCalculator(policy), self._parser
        )

    async def call(
        self,
        endpoint: str,
        fn: Callable,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        executor = self._make_executor(self.get_policy(endpoint))
        return await executor.execute(fn, *args, **kwargs)
```

## Solution 6: Retry Metrics Collector

```python
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List


@dataclass
class RetryMetricEvent:
    endpoint: str
    attempts: int
    succeeded: bool
    total_delay_seconds: float
    timestamp: float = field(default_factory=time.time)


class RetryMetricsCollector:
    """
    Accumulates retry events and computes per-endpoint retry statistics.
    High avg_attempts on a specific endpoint indicates a chronic reliability issue.
    """

    def __init__(self, window_seconds: float = 3600.0):
        self._events: List[RetryMetricEvent] = []
        self._window = window_seconds

    def record(
        self,
        endpoint: str,
        attempts: int,
        succeeded: bool,
        total_delay_seconds: float,
    ) -> None:
        self._events.append(RetryMetricEvent(
            endpoint=endpoint,
            attempts=attempts,
            succeeded=succeeded,
            total_delay_seconds=total_delay_seconds,
        ))

    def _trim(self) -> None:
        cutoff = time.time() - self._window
        self._events = [e for e in self._events if e.timestamp >= cutoff]

    def summary(self) -> dict:
        self._trim()
        by_endpoint: Dict[str, list] = defaultdict(list)
        for e in self._events:
            by_endpoint[e.endpoint].append(e)

        result = {}
        for endpoint, events in by_endpoint.items():
            success = [e for e in events if e.succeeded]
            result[endpoint] = {
                "total_calls": len(events),
                "success_rate": round(len(success) / max(len(events), 1), 4),
                "avg_attempts": round(
                    sum(e.attempts for e in events) / max(len(events), 1), 2
                ),
                "avg_delay_seconds": round(
                    sum(e.total_delay_seconds for e in events) / max(len(events), 1), 3
                ),
                "max_attempts": max(e.attempts for e in events),
            }
        return result
```

## Comparison

| Approach | Jitter Strategy | Retry-After Support | Per-Endpoint Policy | Total Time Cap | Metrics |
|---|---|---|---|---|---|
| JitteredDelayCalculator | full / equal / none | Via parameter | No | No | No |
| ExponentialBackoffRetryExecutor | Via calculator | Yes (auto-parsed) | No | Yes | Yes (per call) |
| PerEndpointRetryManager | Via policy | Via executor | Yes | Via policy | No |
| RetryMetricsCollector | No | No | No | No | Yes (fleet-level) |

**Best for production**: Use `jitter="full"` — it provides the best thundering-herd prevention because the entire delay range is randomized. Set `max_total_seconds=120` for LLM API calls and `max_total_seconds=10` for cache lookups (fast-fail and degrade). Always honour `Retry-After` headers from 429 responses — ignoring them is the primary cause of repeat rate-limit violations. Register each downstream API with `PerEndpointRetryManager` so LLM, search, and database calls can each have appropriate `max_attempts` and `max_delay_seconds`. Monitor `RetryMetricsCollector.summary()`: avg_attempts above 1.5 on any endpoint signals a chronic reliability problem that warrants investigation beyond retry tuning.
