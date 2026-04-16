---
title: "Agent Doesn't Implement Throttle Shedding for Overloaded LLM Providers"
description: "Agents that blindly retry on 429 rate-limit errors compound provider overload: every retry adds traffic to an already-saturated API, making recovery slower for all callers. Implement throttle shedding that detects rate-limit signals early, immediately sheds low-priority requests, queues high-priority requests with exponential backoff, and switches to a fallback provider when the primary is consistently throttled."
date: 2026-04-16
difficulty: intermediate
category: reliability
slug: agent-doesnt-implement-throttle-shedding-for-overloaded-llm-providers
tags: [throttle-shedding, rate-limiting, 429, load-shedding, provider-failover, backpressure]
symptoms:
  - "429 errors cause immediate retry — adding more load to an already-throttled API"
  - "All requests treated equally during throttling — no priority-based shedding"
  - "No fallback provider when the primary is rate-limited for extended periods"
  - "Retry storms during API incidents consume quota faster than the provider can recover"
  - "No measurement of throttle rate — 429s are logged but not aggregated"
---

## Why This Happens

Rate limiting is a cooperative protocol: the provider signals overload, and well-behaved clients back off. Agents that retry immediately on 429 violate this protocol and make the overload worse. Throttle shedding inverts the response: when rate limits are detected, the agent proactively sheds work rather than adding more. Low-priority requests are dropped with a clear error; high-priority requests wait in a bounded queue with exponential backoff. If throttling persists for more than a configured window, traffic shifts to a secondary provider.

## Solution 1: Throttle Signal Detector

```python
import time
from dataclasses import dataclass, field
from enum import Enum
from threading import Lock
from typing import Optional


class ThrottleSignal(str, Enum):
    NONE = "none"
    SOFT = "soft"       # occasional 429s — back off gently
    HARD = "hard"       # sustained 429s — shed aggressively
    CRITICAL = "critical"  # provider effectively unavailable


@dataclass
class ThrottleState:
    signal: ThrottleSignal = ThrottleSignal.NONE
    consecutive_429s: int = 0
    total_429s_in_window: int = 0
    last_429_at: Optional[float] = None
    window_start: float = field(default_factory=time.time)
    shed_count: int = 0


class ThrottleSignalDetector:
    def __init__(
        self,
        soft_threshold: int = 3,
        hard_threshold: int = 10,
        critical_threshold: int = 30,
        window_seconds: float = 60.0,
    ):
        self._soft = soft_threshold
        self._hard = hard_threshold
        self._critical = critical_threshold
        self._window = window_seconds
        self._state = ThrottleState()
        self._lock = Lock()

    def record_429(self) -> ThrottleSignal:
        now = time.time()
        with self._lock:
            s = self._state
            if now - s.window_start >= self._window:
                s.total_429s_in_window = 0
                s.window_start = now

            s.consecutive_429s += 1
            s.total_429s_in_window += 1
            s.last_429_at = now

            if s.total_429s_in_window >= self._critical:
                s.signal = ThrottleSignal.CRITICAL
            elif s.total_429s_in_window >= self._hard:
                s.signal = ThrottleSignal.HARD
            elif s.total_429s_in_window >= self._soft:
                s.signal = ThrottleSignal.SOFT
            return s.signal

    def record_success(self) -> None:
        with self._lock:
            self._state.consecutive_429s = 0
            if self._state.signal != ThrottleSignal.NONE:
                if self._state.consecutive_429s == 0:
                    self._state.signal = ThrottleSignal.NONE

    def current_signal(self) -> ThrottleSignal:
        with self._lock:
            return self._state.signal

    def stats(self) -> dict:
        with self._lock:
            s = self._state
            return {
                "signal": s.signal.value,
                "consecutive_429s": s.consecutive_429s,
                "window_429s": s.total_429s_in_window,
                "shed_count": s.shed_count,
            }
```

## Solution 2: Request Priority Classifier

```python
from enum import Enum
from typing import Any, Dict


class RequestPriority(str, Enum):
    CRITICAL = "critical"   # always attempt — user-facing blocking
    HIGH = "high"           # queue and retry
    MEDIUM = "medium"       # shed on HARD throttle
    LOW = "low"             # shed on SOFT throttle
    BACKGROUND = "background"  # shed immediately on any throttle


def classify_request_priority(
    request_metadata: Dict[str, Any],
) -> RequestPriority:
    """
    Classifies request priority from metadata.
    Customize this logic for your application's priority model.
    """
    if request_metadata.get("is_user_blocking"):
        return RequestPriority.CRITICAL
    if request_metadata.get("is_realtime"):
        return RequestPriority.HIGH
    if request_metadata.get("is_batch"):
        return RequestPriority.LOW
    if request_metadata.get("is_background"):
        return RequestPriority.BACKGROUND
    return RequestPriority.MEDIUM
```

## Solution 3: Throttle-Aware Request Dispatcher

```python
import asyncio
import time
from typing import Any, Callable, Dict, Optional


class ThrottledRequestError(Exception):
    def __init__(self, priority: RequestPriority, signal: ThrottleSignal):
        self.priority = priority
        self.signal = signal
        super().__init__(f"request shed: priority={priority.value}, throttle={signal.value}")


class ThrottleAwareRequestDispatcher:
    """
    Dispatches LLM requests with priority-based shedding during throttle events.
    Implements exponential backoff for queued requests and emits shed events.
    """

    SHED_MATRIX = {
        ThrottleSignal.NONE: set(),
        ThrottleSignal.SOFT: {RequestPriority.BACKGROUND},
        ThrottleSignal.HARD: {RequestPriority.BACKGROUND, RequestPriority.LOW},
        ThrottleSignal.CRITICAL: {
            RequestPriority.BACKGROUND, RequestPriority.LOW, RequestPriority.MEDIUM
        },
    }

    def __init__(
        self,
        detector: ThrottleSignalDetector,
        max_retries: int = 4,
        base_backoff_seconds: float = 1.0,
        max_backoff_seconds: float = 30.0,
    ):
        self._detector = detector
        self._max_retries = max_retries
        self._base_backoff = base_backoff_seconds
        self._max_backoff = max_backoff_seconds

    def _should_shed(self, priority: RequestPriority) -> bool:
        signal = self._detector.current_signal()
        return priority in self.SHED_MATRIX.get(signal, set())

    async def dispatch(
        self,
        request_fn: Callable,
        priority: RequestPriority = RequestPriority.MEDIUM,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        if self._should_shed(priority):
            self._detector._state.shed_count += 1
            raise ThrottledRequestError(priority, self._detector.current_signal())

        last_exc = None
        for attempt in range(self._max_retries + 1):
            try:
                result = await request_fn(*args, **kwargs)
                self._detector.record_success()
                return result
            except Exception as exc:
                error_str = str(exc)
                is_429 = "429" in error_str or "rate_limit" in error_str.lower()
                if is_429:
                    signal = self._detector.record_429()
                    if self._should_shed(priority):
                        raise ThrottledRequestError(priority, signal)
                    if attempt < self._max_retries:
                        delay = min(
                            self._base_backoff * (2 ** attempt),
                            self._max_backoff,
                        )
                        await asyncio.sleep(delay)
                        continue
                last_exc = exc
                if not is_429:
                    raise
        raise last_exc
```

## Solution 4: Provider Failover Manager

```python
import asyncio
import time
from typing import Any, Callable, Dict, List, Optional


class ProviderFailoverManager:
    """
    Monitors throttle state per provider and routes requests to a fallback
    provider when the primary has been critically throttled for too long.
    """

    def __init__(
        self,
        providers: Dict[str, Callable],     # name -> async request factory
        detectors: Dict[str, ThrottleSignalDetector],
        failover_after_seconds: float = 30.0,
    ):
        self._providers = providers
        self._detectors = detectors
        self._failover_after = failover_after_seconds
        self._critical_since: Dict[str, Optional[float]] = {p: None for p in providers}

    def _select_provider(self) -> str:
        now = time.time()
        for name, detector in self._detectors.items():
            signal = detector.current_signal()
            if signal == ThrottleSignal.CRITICAL:
                if self._critical_since[name] is None:
                    self._critical_since[name] = now
            else:
                self._critical_since[name] = None

        for name in self._providers:
            critical_start = self._critical_since.get(name)
            if critical_start is None or (now - critical_start) < self._failover_after:
                return name

        # All providers critical — use the one least recently critical
        return min(
            self._critical_since,
            key=lambda n: self._critical_since[n] or 0,
        )

    async def call(self, request_fn_key: str, *args: Any, **kwargs: Any) -> Any:
        provider_name = self._select_provider()
        provider_factory = self._providers[provider_name]
        client = provider_factory()
        method = getattr(client, request_fn_key)
        return await method(*args, **kwargs)
```

## Solution 5: Throttle Metrics Dashboard

```python
import time


class ThrottleDashboard:
    def __init__(
        self,
        detectors: Dict[str, ThrottleSignalDetector],
        dispatcher: ThrottleAwareRequestDispatcher,
    ):
        self._detectors = detectors
        self._dispatcher = dispatcher

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "providers": {
                name: detector.stats()
                for name, detector in self._detectors.items()
            },
        }
```

## Solution 6: Retry Budget Integration

```python
from typing import Optional


class ThrottleRetryBudgetBridge:
    """
    Connects throttle shedding with a retry budget to prevent
    throttle-induced retry storms from consuming the retry budget
    of other operations.
    """

    def __init__(
        self,
        detector: ThrottleSignalDetector,
        max_throttle_retries_per_minute: int = 10,
    ):
        self._detector = detector
        self._max = max_throttle_retries_per_minute
        self._retry_timestamps: list = []

    def can_retry_throttled_request(self) -> bool:
        now = time.time()
        cutoff = now - 60.0
        self._retry_timestamps = [t for t in self._retry_timestamps if t >= cutoff]
        if len(self._retry_timestamps) >= self._max:
            return False
        self._retry_timestamps.append(now)
        return True

    def stats(self) -> dict:
        return {
            "throttle_retries_last_minute": len(self._retry_timestamps),
            "max_per_minute": self._max,
        }
```

## Comparison

| Approach | 429 Detection | Priority Shedding | Backoff | Provider Failover | Dashboard |
|---|---|---|---|---|---|
| ThrottleSignalDetector | Yes (windowed) | No | No | No | No |
| ThrottleAwareRequestDispatcher | Via detector | Yes (matrix) | Yes (exponential) | No | No |
| ProviderFailoverManager | Via detectors | No | No | Yes | No |
| ThrottleRetryBudgetBridge | Via detector | No | No | No | No |
| ThrottleDashboard | No | No | No | No | Yes |

**Best for production**: Set `soft_threshold=3` and `hard_threshold=10` per 60-second window — these trigger shedding before the provider's own rate limiter cuts off all traffic. Always shed BACKGROUND jobs first (analytics, cache warming, batch summarization) to preserve capacity for user-facing requests. Configure `failover_after_seconds=30`: switching providers too quickly causes unnecessary fragmentation; too slowly means users wait while the primary recovers. Monitor `shed_count` — a consistently non-zero shed count means provisioned quota is insufficient for the load and should be increased rather than relying on shedding indefinitely.
