---
title: "Agent Doesn't Implement Retry Budget to Prevent Retry Storms"
description: "Agents with unbounded per-request retry logic amplify failures into retry storms: when a dependency degrades, every in-flight request retries simultaneously, producing 3–5× the original request volume and preventing recovery. Implement a retry budget that caps the total number of active retries across all requests, enforces a per-caller retry share, and sheds retries gracefully when the budget is exhausted rather than amplifying load."
date: 2026-04-16
difficulty: advanced
category: reliability
slug: agent-doesnt-implement-retry-budget-to-prevent-retry-storms
tags: [retry-budget, retry-storm, retry-amplification, backpressure, circuit-breaker, overload-protection]
symptoms:
  - "A 30-second LLM provider outage causes 5 minutes of elevated error rates from retry amplification"
  - "Every failing request retries 3 times simultaneously, tripling load on an already degraded provider"
  - "No global retry limit — 100 concurrent sessions each retry independently"
  - "Retry count is per-request with no awareness of fleet-wide retry pressure"
  - "Recovery time after outage is longer than the outage itself due to retry queues draining"
---

## Why This Happens

Per-request retry logic is designed for isolated failures, not correlated failures. When a provider experiences latency, all concurrent requests begin retrying simultaneously. With 3 retries per request and 100 concurrent sessions, a 10-second outage generates 300 retry attempts that all fire at the same time, producing a request spike that extends the outage. A retry budget is a shared token pool: each retry attempt consumes a token; when the pool is empty, additional retries are rejected with a fast failure rather than queued. This transforms unbounded amplification into bounded, controlled degradation.

## Solution 1: Retry Budget Token Pool

```python
import asyncio
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class RetryBudgetConfig:
    total_budget: int = 100          # max concurrent active retries fleet-wide
    per_caller_share: float = 0.10   # max fraction any single caller can hold
    refill_rate_per_second: float = 5.0  # tokens refilled per second
    budget_exhausted_action: str = "fail_fast"  # "fail_fast" | "shed_oldest"
    min_tokens_for_retry: int = 1


class RetryBudgetTokenPool:
    """
    Leaky-bucket token pool for retry budgets.
    Tokens are consumed on retry attempt and refilled at a steady rate.
    Per-caller limits prevent a single caller from exhausting the pool.
    """

    def __init__(self, config: RetryBudgetConfig) -> None:
        self._config = config
        self._tokens = float(config.total_budget)
        self._last_refill = time.time()
        self._per_caller_tokens: dict = {}
        self._lock = asyncio.Lock()
        self._total_granted = 0
        self._total_rejected = 0

    def _refill(self) -> None:
        now = time.time()
        elapsed = now - self._last_refill
        self._tokens = min(
            float(self._config.total_budget),
            self._tokens + elapsed * self._config.refill_rate_per_second,
        )
        self._last_refill = now

    def _caller_limit(self) -> int:
        return max(1, int(self._config.total_budget * self._config.per_caller_share))

    async def acquire(self, caller_id: str, tokens: int = 1) -> bool:
        async with self._lock:
            self._refill()

            caller_used = self._per_caller_tokens.get(caller_id, 0)
            if caller_used + tokens > self._caller_limit():
                self._total_rejected += 1
                return False

            if self._tokens < tokens:
                self._total_rejected += 1
                return False

            self._tokens -= tokens
            self._per_caller_tokens[caller_id] = caller_used + tokens
            self._total_granted += 1
            return True

    async def release(self, caller_id: str, tokens: int = 1) -> None:
        async with self._lock:
            used = self._per_caller_tokens.get(caller_id, 0)
            self._per_caller_tokens[caller_id] = max(0, used - tokens)

    def stats(self) -> dict:
        total = self._total_granted + self._total_rejected
        return {
            "available_tokens": round(self._tokens, 1),
            "total_budget": self._config.total_budget,
            "utilization_pct": round(
                (self._config.total_budget - self._tokens) / max(self._config.total_budget, 1) * 100, 1
            ),
            "total_granted": self._total_granted,
            "total_rejected": self._total_rejected,
            "rejection_rate": round(self._total_rejected / max(total, 1), 4),
        }
```

## Solution 2: Budget-Aware Retry Policy

```python
from dataclasses import dataclass
from typing import Optional


@dataclass
class BudgetAwareRetryPolicy:
    max_attempts: int = 3
    base_delay_seconds: float = 0.5
    max_delay_seconds: float = 30.0
    jitter_factor: float = 0.25        # random jitter as fraction of delay
    retryable_status_codes: frozenset = frozenset({429, 500, 502, 503, 504})
    retryable_exception_types: tuple = ()
    require_budget: bool = True        # if False, skip budget check

    def is_retryable(self, exc: Exception, http_status: Optional[int] = None) -> bool:
        if http_status in self.retryable_status_codes:
            return True
        if self.retryable_exception_types and isinstance(exc, self.retryable_exception_types):
            return True
        error_str = str(exc).lower()
        return any(kw in error_str for kw in ("timeout", "connection", "temporarily"))

    def delay_seconds(self, attempt: int) -> float:
        import random
        base = min(
            self.base_delay_seconds * (2 ** (attempt - 1)),
            self.max_delay_seconds,
        )
        jitter = base * self.jitter_factor * random.random()
        return base + jitter
```

## Solution 3: Budget-Controlled Retry Executor

```python
import asyncio
from typing import Any, Callable, Optional


class BudgetControlledRetryExecutor:
    """
    Wraps async calls with retry logic gated by the global retry budget.
    When the budget is exhausted, raises immediately rather than retrying.
    Releases budget tokens when retries complete (success or final failure).
    """

    def __init__(
        self,
        budget_pool: RetryBudgetTokenPool,
        policy: BudgetAwareRetryPolicy,
    ) -> None:
        self._pool = budget_pool
        self._policy = policy

    async def execute(
        self,
        caller_id: str,
        fn: Callable,
        *args: Any,
        http_status_fn: Optional[Callable[[Exception], Optional[int]]] = None,
        **kwargs: Any,
    ) -> Any:
        """
        Executes fn with budget-gated retry.
        http_status_fn: optional callable that extracts HTTP status from an exception.
        """
        last_exc = None

        for attempt in range(1, self._policy.max_attempts + 1):
            try:
                return await fn(*args, **kwargs)
            except Exception as exc:
                last_exc = exc
                http_status = http_status_fn(exc) if http_status_fn else None

                if attempt >= self._policy.max_attempts:
                    break

                if not self._policy.is_retryable(exc, http_status):
                    break

                if self._policy.require_budget:
                    granted = await self._pool.acquire(caller_id)
                    if not granted:
                        # Budget exhausted — fail fast instead of retrying
                        raise RuntimeError(
                            f"Retry budget exhausted for caller '{caller_id}' — "
                            f"fast-failing instead of retrying"
                        ) from exc

                delay = self._policy.delay_seconds(attempt)
                try:
                    await asyncio.sleep(delay)
                finally:
                    if self._policy.require_budget:
                        await self._pool.release(caller_id)

        raise last_exc
```

## Solution 4: Retry Pressure Monitor

```python
import time
from typing import List


class RetryPressureMonitor:
    """
    Tracks retry pressure signals — rejection rate, pool utilization,
    and per-caller saturation — and fires alerts when pressure is high.
    """

    def __init__(
        self,
        pool: RetryBudgetTokenPool,
        rejection_rate_alert: float = 0.10,
        utilization_alert_pct: float = 80.0,
    ) -> None:
        self._pool = pool
        self._rejection_threshold = rejection_rate_alert
        self._utilization_threshold = utilization_alert_pct

    def check(self) -> List[dict]:
        stats = self._pool.stats()
        alerts = []

        if stats["rejection_rate"] >= self._rejection_threshold:
            alerts.append({
                "type": "high_retry_rejection_rate",
                "rejection_rate": stats["rejection_rate"],
                "threshold": self._rejection_threshold,
                "severity": "warning",
                "message": (
                    f"Retry budget rejecting {stats['rejection_rate']*100:.1f}% of retry attempts — "
                    "system under retry pressure. Consider increasing budget or reducing call volume."
                ),
            })

        if stats["utilization_pct"] >= self._utilization_threshold:
            alerts.append({
                "type": "retry_budget_saturated",
                "utilization_pct": stats["utilization_pct"],
                "threshold": self._utilization_threshold,
                "severity": "critical" if stats["utilization_pct"] >= 95 else "warning",
                "message": f"Retry budget at {stats['utilization_pct']:.1f}% capacity",
            })

        return alerts

    def report(self) -> dict:
        return {
            "generated_at": time.time(),
            "stats": self._pool.stats(),
            "alerts": self.check(),
        }
```

## Solution 5: Retry Storm Circuit Breaker

```python
import time
from enum import Enum
from typing import Optional


class StormCircuitState(str, Enum):
    CLOSED = "closed"     # normal operation
    OPEN = "open"         # all retries blocked
    HALF_OPEN = "half_open"  # test probe allowed


class RetryStormCircuitBreaker:
    """
    Opens when the retry rejection rate exceeds a threshold,
    blocking all retries until pressure drops and a recovery
    probe succeeds. Prevents the pool from being constantly hammered.
    """

    def __init__(
        self,
        monitor: RetryPressureMonitor,
        open_threshold_rejection_rate: float = 0.20,
        recovery_window_seconds: float = 30.0,
        probe_success_threshold: int = 3,
    ) -> None:
        self._monitor = monitor
        self._open_threshold = open_threshold_rejection_rate
        self._recovery_window = recovery_window_seconds
        self._probe_threshold = probe_success_threshold
        self._state = StormCircuitState.CLOSED
        self._opened_at: Optional[float] = None
        self._probe_successes = 0

    def is_open(self) -> bool:
        if self._state == StormCircuitState.CLOSED:
            return False
        if self._state == StormCircuitState.OPEN:
            if time.time() - (self._opened_at or 0) > self._recovery_window:
                self._state = StormCircuitState.HALF_OPEN
                self._probe_successes = 0
            return True
        return False   # HALF_OPEN allows probes

    def record_outcome(self, succeeded: bool) -> None:
        if self._state == StormCircuitState.HALF_OPEN:
            if succeeded:
                self._probe_successes += 1
                if self._probe_successes >= self._probe_threshold:
                    self._state = StormCircuitState.CLOSED
            else:
                self._state = StormCircuitState.OPEN
                self._opened_at = time.time()

    def evaluate(self) -> None:
        stats = self._monitor._pool.stats()
        if (self._state == StormCircuitState.CLOSED
                and stats["rejection_rate"] >= self._open_threshold):
            self._state = StormCircuitState.OPEN
            self._opened_at = time.time()

    def state(self) -> StormCircuitState:
        return self._state
```

## Solution 6: Retry Budget Dashboard

```python
import time


class RetryBudgetDashboard:
    """
    Combines pool stats, pressure monitor, and circuit breaker state
    into a single retry infrastructure operational view.
    """

    def __init__(
        self,
        pool: RetryBudgetTokenPool,
        monitor: RetryPressureMonitor,
        circuit_breaker: RetryStormCircuitBreaker,
    ) -> None:
        self._pool = pool
        self._monitor = monitor
        self._circuit_breaker = circuit_breaker

    def render(self) -> dict:
        self._circuit_breaker.evaluate()
        stats = self._pool.stats()
        alerts = self._monitor.check()

        return {
            "generated_at": time.time(),
            "retry_budget": stats,
            "circuit_breaker_state": self._circuit_breaker.state().value,
            "active_alerts": alerts,
        }
```

## Comparison

| Approach | Token Pool | Per-Caller Limit | Retry Gating | Storm Detection | Circuit Breaker |
|---|---|---|---|---|---|
| RetryBudgetTokenPool | Yes (leaky bucket) | Yes | No | No | No |
| BudgetControlledRetryExecutor | Via pool | Via pool | Yes | No | No |
| RetryPressureMonitor | No | No | No | Yes | No |
| RetryStormCircuitBreaker | No | No | No | Via monitor | Yes |
| RetryBudgetDashboard | No | No | No | No | Via breaker |

**Best for production**: Set `total_budget` to 10–20% of your peak concurrent request volume — if you handle 500 concurrent requests at peak, a budget of 50–100 retry tokens is appropriate. Use `per_caller_share=0.10` to prevent any single session from consuming more than 10% of the pool. Set `refill_rate_per_second` to match your provider's recovery rate after incidents — if your provider recovers in 30 seconds, refilling 5 tokens/second ensures the budget is full again within that window. Open the circuit breaker at 20% rejection rate — by that point, retries are making things worse rather than helping recovery.
