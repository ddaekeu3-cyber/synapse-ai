---
title: "Agent Doesn't Implement Retry Budget to Prevent Retry Storms"
description: "Agents that retry every failed request with unlimited attempts amplify failures into storms: when a downstream service degrades, all concurrent agent instances begin retrying simultaneously, generating 3–10× the original request volume against a service that is already struggling. Implement a retry budget that limits the total number of retries per service per time window, enforces a global retry rate cap, and sheds retries gracefully when the budget is exhausted."
date: 2026-04-16
difficulty: advanced
category: reliability
slug: agent-doesnt-implement-retry-budget-to-prevent-retry-storms
tags: [retry-budget, retry-storm, overload-protection, retry-rate-limiting, fault-amplification, backoff]
symptoms:
  - "A degraded downstream service receives 5× normal request volume due to agent retries"
  - "Multiple agent instances all retry simultaneously, amplifying load on a struggling service"
  - "Retry logic has no upper bound — a stuck service causes infinite retry loops"
  - "No visibility into how many retries are in flight at any given moment"
  - "Retry backoff is per-instance — global retry volume is never considered"
---

## Why This Happens

Per-request retry logic looks correct in isolation: each failed request retries up to N times with exponential backoff. But in a fleet of agent instances, every instance applies this logic independently. When service latency spikes, all instances simultaneously experience timeouts, simultaneously trigger retries, and simultaneously hit the service again with 3–5× the original volume. The service, already degraded, receives this amplified load and degrades further. A retry budget adds a coordination layer: a shared counter of retries consumed against each service, a rate limit on retry throughput, and a shed mechanism that converts budget-exhausted retries into immediate failures rather than queuing more load.

## Solution 1: Retry Budget State

```python
import time
import threading
from dataclasses import dataclass, field
from typing import Dict, Optional


@dataclass
class RetryBudgetConfig:
    service_name: str
    max_retries_per_window: int = 100    # total retries allowed per window
    window_seconds: float = 60.0         # rolling window duration
    max_retry_rate_per_second: float = 5.0  # token bucket rate
    burst_capacity: int = 10             # token bucket burst
    shed_strategy: str = "fail_fast"     # "fail_fast" | "queue"


class RetryBudgetState:
    """
    Thread-safe sliding window counter + token bucket for retry rate limiting.
    """

    def __init__(self, config: RetryBudgetConfig):
        self._config = config
        self._window_events: list = []    # timestamps of retries in current window
        self._tokens: float = float(config.burst_capacity)
        self._last_refill: float = time.time()
        self._lock = threading.Lock()
        self._total_allowed = 0
        self._total_shed = 0

    def _refill_tokens(self, now: float) -> None:
        elapsed = now - self._last_refill
        self._tokens = min(
            self._config.burst_capacity,
            self._tokens + elapsed * self._config.max_retry_rate_per_second,
        )
        self._last_refill = now

    def _evict_old(self, now: float) -> None:
        cutoff = now - self._config.window_seconds
        self._window_events = [ts for ts in self._window_events if ts >= cutoff]

    def request_retry(self) -> bool:
        """Returns True if retry is allowed, False if shed."""
        now = time.time()
        with self._lock:
            self._refill_tokens(now)
            self._evict_old(now)

            window_count = len(self._window_events)
            if window_count >= self._config.max_retries_per_window:
                self._total_shed += 1
                return False

            if self._tokens < 1.0:
                self._total_shed += 1
                return False

            self._tokens -= 1.0
            self._window_events.append(now)
            self._total_allowed += 1
            return True

    def stats(self) -> dict:
        now = time.time()
        with self._lock:
            self._evict_old(now)
            return {
                "service_name": self._config.service_name,
                "window_retries": len(self._window_events),
                "window_budget": self._config.max_retries_per_window,
                "tokens_available": round(self._tokens, 2),
                "total_allowed": self._total_allowed,
                "total_shed": self._total_shed,
                "shed_rate": round(
                    self._total_shed / max(self._total_allowed + self._total_shed, 1),
                    4,
                ),
            }
```

## Solution 2: Retry Budget Registry

```python
import threading
from typing import Dict, Optional


class RetryBudgetRegistry:
    """
    Manages RetryBudgetState instances per service.
    Provides a single entry point for all retry budget checks.
    """

    def __init__(self, default_config: Optional[RetryBudgetConfig] = None):
        self._budgets: Dict[str, RetryBudgetState] = {}
        self._configs: Dict[str, RetryBudgetConfig] = {}
        self._default = default_config
        self._lock = threading.Lock()

    def register(self, config: RetryBudgetConfig) -> None:
        with self._lock:
            self._configs[config.service_name] = config
            self._budgets[config.service_name] = RetryBudgetState(config)

    def request_retry(self, service_name: str) -> bool:
        with self._lock:
            if service_name not in self._budgets:
                if self._default:
                    cfg = RetryBudgetConfig(
                        service_name=service_name,
                        max_retries_per_window=self._default.max_retries_per_window,
                        window_seconds=self._default.window_seconds,
                        max_retry_rate_per_second=self._default.max_retry_rate_per_second,
                        burst_capacity=self._default.burst_capacity,
                    )
                    self._budgets[service_name] = RetryBudgetState(cfg)
                else:
                    return True   # no budget configured — allow
            budget = self._budgets[service_name]
        return budget.request_retry()

    def all_stats(self) -> Dict[str, dict]:
        with self._lock:
            return {name: b.stats() for name, b in self._budgets.items()}
```

## Solution 3: Budget-Aware Retry Executor

```python
import asyncio
import time
from typing import Any, Callable, Optional


class RetryBudgetExhaustedError(Exception):
    def __init__(self, service_name: str):
        super().__init__(
            f"retry budget exhausted for service '{service_name}' — request shed"
        )
        self.service_name = service_name


class BudgetAwareRetryExecutor:
    """
    Wraps async calls with budget-checked retry logic.
    Uses jittered exponential backoff to desynchronize concurrent retries.
    """

    RETRYABLE_STATUS = {429, 500, 502, 503, 504}

    def __init__(
        self,
        registry: RetryBudgetRegistry,
        max_attempts: int = 4,
        base_delay_seconds: float = 1.0,
        max_delay_seconds: float = 30.0,
        jitter_factor: float = 0.3,
    ):
        self._registry = registry
        self._max_attempts = max_attempts
        self._base = base_delay_seconds
        self._max_delay = max_delay_seconds
        self._jitter = jitter_factor

    def _jittered_delay(self, attempt: int) -> float:
        import random
        delay = min(self._base * (2 ** (attempt - 1)), self._max_delay)
        jitter = delay * self._jitter * random.uniform(-1, 1)
        return max(0.0, delay + jitter)

    async def execute(
        self,
        service_name: str,
        call_fn: Callable,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        last_error = None

        for attempt in range(1, self._max_attempts + 1):
            try:
                return await call_fn(*args, **kwargs)
            except Exception as exc:
                last_error = exc
                if attempt >= self._max_attempts:
                    break

                if not self._registry.request_retry(service_name):
                    raise RetryBudgetExhaustedError(service_name)

                delay = self._jittered_delay(attempt)
                await asyncio.sleep(delay)

        raise last_error
```

## Solution 4: Retry Storm Detector

```python
import time
import threading
from typing import Dict, List


class RetryStormDetector:
    """
    Detects retry storms by comparing the retry rate against the
    baseline request rate. A ratio > 2× for more than 30 seconds signals a storm.
    """

    def __init__(
        self,
        registry: RetryBudgetRegistry,
        storm_ratio_threshold: float = 2.0,
        storm_duration_seconds: float = 30.0,
    ):
        self._registry = registry
        self._ratio_threshold = storm_ratio_threshold
        self._duration = storm_duration_seconds
        self._storm_start: Dict[str, float] = {}
        self._lock = threading.Lock()

    def check(self) -> List[dict]:
        storms = []
        all_stats = self._registry.all_stats()
        now = time.time()

        for service_name, stats in all_stats.items():
            window_retries = stats["window_retries"]
            window_budget = stats["window_budget"]
            usage_rate = window_retries / max(window_budget, 1)

            with self._lock:
                if usage_rate >= self._ratio_threshold / 2:
                    if service_name not in self._storm_start:
                        self._storm_start[service_name] = now
                    elif now - self._storm_start[service_name] >= self._duration:
                        storms.append({
                            "service_name": service_name,
                            "window_retries": window_retries,
                            "shed_rate": stats["shed_rate"],
                            "storm_duration_seconds": round(now - self._storm_start[service_name], 1),
                        })
                else:
                    self._storm_start.pop(service_name, None)

        return storms
```

## Solution 5: Adaptive Retry Budget Adjuster

```python
import time
import threading
from typing import Dict


class AdaptiveRetryBudgetAdjuster:
    """
    Reduces retry budgets dynamically when shed rates are high,
    preventing runaway retry amplification during sustained outages.
    """

    def __init__(
        self,
        registry: RetryBudgetRegistry,
        shed_rate_threshold: float = 0.30,
        reduction_factor: float = 0.5,
        check_interval_seconds: float = 60.0,
    ):
        self._registry = registry
        self._shed_threshold = shed_rate_threshold
        self._reduction = reduction_factor
        self._interval = check_interval_seconds
        self._adjustments: Dict[str, int] = {}

    def check_and_adjust(self) -> List[dict]:
        adjustments = []
        all_stats = self._registry.all_stats()

        for service_name, stats in all_stats.items():
            if stats["shed_rate"] >= self._shed_threshold:
                budget = self._registry._budgets.get(service_name)
                if budget:
                    old_max = budget._config.max_retries_per_window
                    new_max = max(10, int(old_max * self._reduction))
                    budget._config.max_retries_per_window = new_max
                    self._adjustments[service_name] = self._adjustments.get(service_name, 0) + 1
                    adjustments.append({
                        "service_name": service_name,
                        "old_max": old_max,
                        "new_max": new_max,
                        "reason": f"shed_rate={stats['shed_rate']:.2f} exceeded threshold",
                    })

        return adjustments
```

## Solution 6: Retry Budget Dashboard

```python
import time


class RetryBudgetDashboard:
    """
    Combines budget statistics, storm detection, and adjustment history
    into a single reliability operations view.
    """

    def __init__(
        self,
        registry: RetryBudgetRegistry,
        storm_detector: RetryStormDetector,
        adjuster: AdaptiveRetryBudgetAdjuster,
    ):
        self._registry = registry
        self._detector = storm_detector
        self._adjuster = adjuster

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "service_budgets": self._registry.all_stats(),
            "active_storms": self._detector.check(),
            "budget_adjustments": dict(self._adjuster._adjustments),
        }
```

## Comparison

| Approach | Window Rate Limit | Token Bucket | Storm Detection | Adaptive Reduction | Dashboard |
|---|---|---|---|---|---|
| RetryBudgetState | Yes (sliding) | Yes (burst) | No | No | No |
| RetryBudgetRegistry | Via state | Via state | No | No | No |
| BudgetAwareRetryExecutor | Via registry | Via registry | No | No | No |
| RetryStormDetector | No | No | Yes (ratio+duration) | No | No |
| AdaptiveRetryBudgetAdjuster | No | No | No | Yes | No |
| RetryBudgetDashboard | No | No | No | No | Yes |

**Best for production**: Set `max_retry_rate_per_second=5` and `burst_capacity=10` as fleet-wide defaults — these values allow brief bursts without sustaining storm-level retry rates. Apply jitter with `jitter_factor=0.3` to desynchronize retries across agent instances: synchronized retries are the root cause of storms even when individual instance retry counts are low. Alert when `shed_rate` exceeds 30% for any service: this means the budget is being actively used as a load limiter rather than as a safety net, indicating the service is under sustained pressure that requires investigation rather than retries.
