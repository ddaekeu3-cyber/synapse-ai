---
title: "Agent Doesn't Implement Exponential Backoff with Jitter for API Retries"
description: "Agents that retry failed API calls with a fixed delay or no delay at all cause retry storms: when a provider experiences a brief outage, hundreds of agents simultaneously retry at the same interval, generating a synchronized traffic spike that extends the outage. Implement exponential backoff with full jitter so retries are spread across time, reducing load on recovering services and avoiding correlated retry spikes."
date: 2026-04-16
difficulty: intermediate
category: reliability
slug: agent-doesnt-implement-exponential-backoff-with-jitter-for-api-retries
tags: [exponential-backoff, jitter, retry-storm, rate-limiting, api-retry, fault-tolerance]
symptoms:
  - "All agent instances retry a failed API call at the same time, extending the outage"
  - "Fixed 1-second retry delay causes synchronized load spikes on recovering services"
  - "No respect for Retry-After headers — agent retries immediately after a 429"
  - "Retry count is unlimited — a permanent failure causes an infinite retry loop"
  - "No distinction between retryable errors (429, 503) and non-retryable errors (400, 401)"
---

## Why This Happens

When multiple agents share an upstream dependency and that dependency returns errors, all agents attempt retries at approximately the same time — especially if they all started processing a batch simultaneously. Without jitter, exponential backoff still produces synchronized retries: all agents at `base * 2^attempt` seconds produce correlated spikes at fixed intervals. Full jitter (`random(0, base * 2^attempt)`) spreads retries uniformly across the backoff window, smoothing the load profile on the recovering service. Retry-After header respect is a contractual requirement for 429 responses — ignoring it causes the provider to ban the client.

## Solution 1: Retry Policy

```python
import random
import time
from dataclasses import dataclass, field
from typing import FrozenSet, Optional, Set, Type


@dataclass
class RetryPolicy:
    max_attempts: int = 4
    base_delay_seconds: float = 1.0
    max_delay_seconds: float = 60.0
    jitter_strategy: str = "full"       # "full", "equal", "decorrelated", "none"
    retryable_status_codes: FrozenSet[int] = field(
        default_factory=lambda: frozenset({429, 500, 502, 503, 504})
    )
    non_retryable_status_codes: FrozenSet[int] = field(
        default_factory=lambda: frozenset({400, 401, 403, 404, 422})
    )
    respect_retry_after: bool = True
    retry_after_cap_seconds: float = 300.0

    def is_retryable_status(self, status_code: int) -> bool:
        if status_code in self.non_retryable_status_codes:
            return False
        return status_code in self.retryable_status_codes

    def delay_for_attempt(self, attempt: int, retry_after: Optional[float] = None) -> float:
        if retry_after is not None and self.respect_retry_after:
            return min(retry_after, self.retry_after_cap_seconds)

        base = self.base_delay_seconds * (2 ** attempt)
        cap = min(base, self.max_delay_seconds)

        if self.jitter_strategy == "full":
            return random.uniform(0, cap)
        if self.jitter_strategy == "equal":
            return cap / 2 + random.uniform(0, cap / 2)
        if self.jitter_strategy == "decorrelated":
            # AWS decorrelated jitter
            prev = self.base_delay_seconds * (2 ** max(attempt - 1, 0))
            return min(self.max_delay_seconds, random.uniform(self.base_delay_seconds, prev * 3))
        return cap  # "none"
```

## Solution 2: Retry-After Header Parser

```python
import time
from typing import Optional


class RetryAfterParser:
    """
    Parses Retry-After headers from HTTP responses.
    Supports both delay-seconds and HTTP-date formats.
    """

    def parse(self, header_value: Optional[str]) -> Optional[float]:
        if not header_value:
            return None
        header_value = header_value.strip()
        # Try integer seconds first
        try:
            delay = float(header_value)
            return max(0.0, delay)
        except ValueError:
            pass
        # Try HTTP-date format
        try:
            import email.utils
            parsed = email.utils.parsedate_to_datetime(header_value)
            delay = (parsed.timestamp() - time.time())
            return max(0.0, delay)
        except Exception:
            pass
        return None
```

## Solution 3: Backoff Retry Executor

```python
import asyncio
import time
from typing import Any, Callable, Optional, Type


class RetryableError(Exception):
    def __init__(self, status_code: int, message: str, retry_after: Optional[float] = None):
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


class NonRetryableError(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


class BackoffRetryExecutor:
    """
    Executes an async callable with exponential backoff and jitter retries.
    Distinguishes retryable from non-retryable errors.
    Records retry statistics for observability.
    """

    def __init__(self, policy: RetryPolicy):
        self._policy = policy
        self._total_attempts = 0
        self._total_retries = 0
        self._total_exhausted = 0
        self._delay_history: list = []

    async def execute(
        self,
        fn: Callable,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        last_exc = None
        for attempt in range(self._policy.max_attempts):
            self._total_attempts += 1
            try:
                return await fn(*args, **kwargs)
            except NonRetryableError:
                raise
            except RetryableError as exc:
                last_exc = exc
                if attempt + 1 >= self._policy.max_attempts:
                    self._total_exhausted += 1
                    raise RetryBudgetExhausted(attempts=attempt + 1) from exc
                delay = self._policy.delay_for_attempt(attempt, exc.retry_after)
                self._delay_history.append(delay)
                if len(self._delay_history) > 1000:
                    self._delay_history.pop(0)
                self._total_retries += 1
                await asyncio.sleep(delay)
            except Exception as exc:
                # Unknown exception — treat as retryable by default
                last_exc = exc
                if attempt + 1 >= self._policy.max_attempts:
                    self._total_exhausted += 1
                    raise
                delay = self._policy.delay_for_attempt(attempt)
                self._delay_history.append(delay)
                self._total_retries += 1
                await asyncio.sleep(delay)

        raise last_exc or RuntimeError("retry loop exited without result")

    def stats(self) -> dict:
        avg_delay = sum(self._delay_history) / max(len(self._delay_history), 1)
        return {
            "total_attempts": self._total_attempts,
            "total_retries": self._total_retries,
            "total_exhausted": self._total_exhausted,
            "avg_retry_delay_seconds": round(avg_delay, 3),
            "retry_rate": round(self._total_retries / max(self._total_attempts, 1), 4),
        }


class RetryBudgetExhausted(Exception):
    def __init__(self, attempts: int):
        super().__init__(f"retry budget exhausted after {attempts} attempts")
        self.attempts = attempts
```

## Solution 4: Per-Provider Retry Registry

```python
from typing import Dict, Optional


class ProviderRetryRegistry:
    """
    Maintains per-provider retry policies so different upstreams
    can have different retry configurations.
    """

    DEFAULT_POLICIES = {
        "anthropic": RetryPolicy(
            max_attempts=4,
            base_delay_seconds=1.0,
            max_delay_seconds=30.0,
            jitter_strategy="full",
        ),
        "openai": RetryPolicy(
            max_attempts=4,
            base_delay_seconds=1.0,
            max_delay_seconds=60.0,
            jitter_strategy="full",
        ),
        "cohere": RetryPolicy(
            max_attempts=3,
            base_delay_seconds=2.0,
            max_delay_seconds=30.0,
            jitter_strategy="equal",
        ),
    }

    def __init__(self):
        self._policies: Dict[str, RetryPolicy] = dict(self.DEFAULT_POLICIES)
        self._executors: Dict[str, BackoffRetryExecutor] = {}

    def register(self, provider: str, policy: RetryPolicy) -> None:
        self._policies[provider] = policy
        self._executors.pop(provider, None)

    def get_executor(self, provider: str) -> BackoffRetryExecutor:
        if provider not in self._executors:
            policy = self._policies.get(provider, RetryPolicy())
            self._executors[provider] = BackoffRetryExecutor(policy)
        return self._executors[provider]

    def all_stats(self) -> Dict[str, dict]:
        return {
            provider: executor.stats()
            for provider, executor in self._executors.items()
        }
```

## Solution 5: Retry Storm Detector

```python
import time
from collections import deque
from threading import Lock
from typing import Deque


class RetryStormDetector:
    """
    Detects correlated retry bursts by monitoring the rate of retry events
    across all executor instances. A spike in retry rate within a short window
    indicates a retry storm caused by a shared upstream failure.
    """

    def __init__(
        self,
        window_seconds: float = 60.0,
        storm_threshold_per_minute: int = 50,
    ):
        self._window = window_seconds
        self._threshold = storm_threshold_per_minute
        self._events: Deque[float] = deque()
        self._lock = Lock()

    def record_retry(self) -> bool:
        """Record a retry event. Returns True if a storm is detected."""
        now = time.time()
        with self._lock:
            self._events.append(now)
            cutoff = now - self._window
            while self._events and self._events[0] < cutoff:
                self._events.popleft()
            count = len(self._events)
        rate_per_minute = count / (self._window / 60.0)
        return rate_per_minute >= self._threshold

    def current_rate_per_minute(self) -> float:
        now = time.time()
        cutoff = now - self._window
        with self._lock:
            count = sum(1 for ts in self._events if ts >= cutoff)
        return round(count / (self._window / 60.0), 2)
```

## Solution 6: Backoff Retry Dashboard

```python
import time


class BackoffRetryDashboard:
    """
    Combines per-provider retry stats and storm detection.
    """

    def __init__(
        self,
        registry: ProviderRetryRegistry,
        storm_detector: RetryStormDetector,
    ):
        self._registry = registry
        self._detector = storm_detector

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "provider_stats": self._registry.all_stats(),
            "retry_rate_per_minute": self._detector.current_rate_per_minute(),
            "storm_detected": self._detector.current_rate_per_minute() >= self._detector._threshold,
        }
```

## Comparison

| Approach | Jitter Strategies | Retry-After Header | Per-Provider Config | Storm Detection | Dashboard |
|---|---|---|---|---|---|
| RetryPolicy | Yes (4 strategies) | Yes | No | No | No |
| BackoffRetryExecutor | Via policy | Via policy | No | No | No |
| ProviderRetryRegistry | Via policies | Via policies | Yes | No | No |
| RetryStormDetector | No | No | No | Yes | No |
| BackoffRetryDashboard | No | No | No | No | Yes |

**Best for production**: Use `jitter_strategy="full"` (uniform random from 0 to cap) as the default — it is the most effective at spreading retry load. Always respect `Retry-After` headers from 429 responses — providers that rate-limit expect clients to honor the header, and repeated violations can result in IP bans. Set `max_attempts=4` with `base_delay_seconds=1.0` and `max_delay_seconds=60.0` — this gives retry windows of approximately 0-1s, 0-2s, 0-4s for the first three retries, covering most transient outages without excessive total wait time. Monitor `RetryStormDetector.current_rate_per_minute()` in a background task and alert when it exceeds the threshold — a storm detected while the provider's status page shows an incident means your jitter is insufficient or your fleet size exceeds the provider's rate limit.
