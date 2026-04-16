---
title: "Agent Doesn't Implement Circuit Breaker for External API Calls"
description: "Agents that retry every failed external API call without a circuit breaker accumulate slow requests: each call waits for a full timeout before failing, and the backlog grows faster than the retries drain it. Implement a circuit breaker that transitions between CLOSED, OPEN, and HALF-OPEN states based on failure rates, immediately fails fast when a dependency is known to be down, and probes for recovery before re-enabling traffic."
date: 2026-04-16
difficulty: intermediate
category: reliability
slug: agent-doesnt-implement-circuit-breaker-for-external-api-calls
tags: [circuit-breaker, fail-fast, external-api, resilience, half-open, failure-detection]
symptoms:
  - "All concurrent sessions block for 30s timeout when a downstream API goes down"
  - "Session queue depth grows during an outage because each retry takes the full timeout duration"
  - "No mechanism to stop calling a known-broken dependency until it recovers"
  - "Manual intervention required to stop flooding a degraded API with retries"
  - "Recovery after an outage causes a thundering-herd of queued requests hitting the API at once"
---

## Why This Happens

Without a circuit breaker, every request to a failed API goes through the full retry cycle — multiple timeouts per call — before failing. With 100 concurrent sessions each waiting 30 seconds, the agent is effectively blocked for the duration of the outage. A circuit breaker short-circuits failed calls immediately once the failure rate crosses a threshold (OPEN state), periodically probes for recovery (HALF-OPEN), and resumes normal operation once the probe succeeds (CLOSED). This limits blast radius to the first few failures rather than the entire session backlog.

## Solution 1: Circuit Breaker State

```python
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class CircuitState(str, Enum):
    CLOSED = "closed"       # normal — requests pass through
    OPEN = "open"           # tripped — requests fail fast
    HALF_OPEN = "half_open" # probe — one request allowed through


@dataclass
class CircuitBreakerConfig:
    failure_threshold: int = 5         # failures within window before OPEN
    failure_window_seconds: float = 60.0
    success_threshold: int = 2         # successes in HALF-OPEN before CLOSED
    open_duration_seconds: float = 30.0
    half_open_max_calls: int = 1       # concurrent probe calls in HALF-OPEN


@dataclass
class CircuitBreakerState:
    name: str
    config: CircuitBreakerConfig
    state: CircuitState = CircuitState.CLOSED
    failure_count: int = 0
    success_count: int = 0
    half_open_calls: int = 0
    opened_at: Optional[float] = None
    last_failure_at: Optional[float] = None
    last_success_at: Optional[float] = None

    def should_attempt(self) -> bool:
        if self.state == CircuitState.CLOSED:
            return True
        if self.state == CircuitState.OPEN:
            if (time.time() - (self.opened_at or 0)) >= self.config.open_duration_seconds:
                return True   # will transition to HALF_OPEN on actual attempt
            return False
        # HALF_OPEN
        return self.half_open_calls < self.config.half_open_max_calls
```

## Solution 2: Circuit Breaker

```python
import asyncio
import time
from typing import Any, Callable, Optional


class CircuitBreaker:
    """
    Implements the circuit breaker pattern for async callables.
    States: CLOSED -> OPEN (on failure threshold) -> HALF_OPEN (after open_duration) -> CLOSED.
    Thread-safe via asyncio.Lock.
    """

    def __init__(self, state: CircuitBreakerState):
        self._s = state
        self._lock = asyncio.Lock()
        self._failure_timestamps: list = []

    async def call(self, fn: Callable, *args: Any, **kwargs: Any) -> Any:
        async with self._lock:
            allowed = await self._pre_call()

        if not allowed:
            raise RuntimeError(
                f"Circuit breaker '{self._s.name}' is OPEN — failing fast"
            )

        try:
            result = await fn(*args, **kwargs)
            async with self._lock:
                await self._on_success()
            return result
        except Exception as exc:
            async with self._lock:
                await self._on_failure()
            raise

    async def _pre_call(self) -> bool:
        s = self._s
        now = time.time()

        if s.state == CircuitState.OPEN:
            if (now - (s.opened_at or 0)) >= s.config.open_duration_seconds:
                s.state = CircuitState.HALF_OPEN
                s.half_open_calls = 0
                s.success_count = 0
            else:
                return False

        if s.state == CircuitState.HALF_OPEN:
            if s.half_open_calls >= s.config.half_open_max_calls:
                return False
            s.half_open_calls += 1

        return True

    async def _on_success(self) -> None:
        s = self._s
        s.last_success_at = time.time()
        if s.state == CircuitState.HALF_OPEN:
            s.success_count += 1
            if s.success_count >= s.config.success_threshold:
                s.state = CircuitState.CLOSED
                s.failure_count = 0
                self._failure_timestamps.clear()
        elif s.state == CircuitState.CLOSED:
            s.failure_count = max(0, s.failure_count - 1)

    async def _on_failure(self) -> None:
        s = self._s
        now = time.time()
        s.last_failure_at = now
        self._failure_timestamps.append(now)

        # Trim outside window
        cutoff = now - s.config.failure_window_seconds
        self._failure_timestamps = [t for t in self._failure_timestamps if t >= cutoff]
        s.failure_count = len(self._failure_timestamps)

        if s.state == CircuitState.HALF_OPEN:
            s.state = CircuitState.OPEN
            s.opened_at = now
        elif s.state == CircuitState.CLOSED:
            if s.failure_count >= s.config.failure_threshold:
                s.state = CircuitState.OPEN
                s.opened_at = now

    def status(self) -> dict:
        s = self._s
        return {
            "name": s.name,
            "state": s.state.value,
            "failure_count": s.failure_count,
            "opened_at": s.opened_at,
            "last_failure_at": s.last_failure_at,
            "last_success_at": s.last_success_at,
        }
```

## Solution 3: Circuit Breaker Registry

```python
from typing import Dict, List, Optional


class CircuitBreakerRegistry:
    """
    Manages named circuit breakers for different external endpoints.
    """

    def __init__(self):
        self._breakers: Dict[str, CircuitBreaker] = {}

    def register(
        self,
        name: str,
        config: Optional[CircuitBreakerConfig] = None,
    ) -> CircuitBreaker:
        state = CircuitBreakerState(
            name=name,
            config=config or CircuitBreakerConfig(),
        )
        breaker = CircuitBreaker(state)
        self._breakers[name] = breaker
        return breaker

    def get(self, name: str) -> CircuitBreaker:
        if name not in self._breakers:
            raise KeyError(f"No circuit breaker registered for '{name}'")
        return self._breakers[name]

    def all_statuses(self) -> List[dict]:
        return [b.status() for b in self._breakers.values()]

    def open_circuits(self) -> List[str]:
        return [
            name for name, b in self._breakers.items()
            if b._s.state == CircuitState.OPEN
        ]
```

## Solution 4: Circuit-Breaker-Protected Tool Caller

```python
from typing import Any, Callable, Dict


class CircuitBreakerProtectedToolCaller:
    """
    Wraps tool invocations with circuit breaker protection.
    Each tool_name maps to a named circuit breaker.
    Falls back to fallback_value if the circuit is open and a fallback is provided.
    """

    def __init__(self, registry: CircuitBreakerRegistry):
        self._registry = registry
        self._fallbacks: Dict[str, Any] = {}

    def register_fallback(self, tool_name: str, fallback_value: Any) -> None:
        self._fallbacks[tool_name] = fallback_value

    async def call(
        self,
        tool_name: str,
        tool_fn: Callable,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        try:
            breaker = self._registry.get(tool_name)
        except KeyError:
            # No circuit breaker registered — call directly
            return await tool_fn(*args, **kwargs)

        try:
            return await breaker.call(tool_fn, *args, **kwargs)
        except RuntimeError as exc:
            if "OPEN" in str(exc) and tool_name in self._fallbacks:
                return self._fallbacks[tool_name]
            raise
```

## Solution 5: Circuit State Change Notifier

```python
import asyncio
import time
from typing import Callable, Dict, List, Optional


class CircuitStateChangeNotifier:
    """
    Polls circuit breakers for state changes and fires callbacks.
    Useful for alerting when a circuit trips or recovers.
    """

    def __init__(
        self,
        registry: CircuitBreakerRegistry,
        poll_interval_seconds: float = 5.0,
    ):
        self._registry = registry
        self._interval = poll_interval_seconds
        self._last_states: Dict[str, str] = {}
        self._on_open: List[Callable] = []
        self._on_closed: List[Callable] = []
        self._task: Optional[asyncio.Task] = None

    def on_open(self, fn: Callable) -> None:
        self._on_open.append(fn)

    def on_closed(self, fn: Callable) -> None:
        self._on_closed.append(fn)

    def start(self) -> None:
        self._task = asyncio.create_task(self._poll_loop())

    def stop(self) -> None:
        if self._task:
            self._task.cancel()

    async def _poll_loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            for status in self._registry.all_statuses():
                name = status["name"]
                current = status["state"]
                prev = self._last_states.get(name, CircuitState.CLOSED.value)
                if current != prev:
                    if current == CircuitState.OPEN.value:
                        for fn in self._on_open:
                            try:
                                fn(name, status)
                            except Exception:
                                pass
                    elif current == CircuitState.CLOSED.value and prev != CircuitState.CLOSED.value:
                        for fn in self._on_closed:
                            try:
                                fn(name, status)
                            except Exception:
                                pass
                self._last_states[name] = current
```

## Solution 6: Circuit Breaker Dashboard

```python
import time


class CircuitBreakerDashboard:
    """Summarizes all circuit breaker states and recent trips."""

    def __init__(self, registry: CircuitBreakerRegistry):
        self._registry = registry

    def render(self) -> dict:
        statuses = self._registry.all_statuses()
        open_circuits = [s for s in statuses if s["state"] == "open"]
        half_open = [s for s in statuses if s["state"] == "half_open"]

        return {
            "generated_at": time.time(),
            "total_circuits": len(statuses),
            "open_count": len(open_circuits),
            "half_open_count": len(half_open),
            "healthy": len(open_circuits) == 0,
            "circuits": statuses,
            "open_circuits": [c["name"] for c in open_circuits],
        }
```

## Comparison

| Approach | Fail Fast | HALF-OPEN Probe | Fallback Values | State Notifications | Dashboard |
|---|---|---|---|---|---|
| CircuitBreaker | Yes | Yes | No | No | No |
| CircuitBreakerRegistry | Via breakers | Via breakers | No | No | No |
| CircuitBreakerProtectedToolCaller | Via registry | Via registry | Yes | No | No |
| CircuitStateChangeNotifier | No | No | No | Yes (poll-based) | No |
| CircuitBreakerDashboard | No | No | No | No | Yes |

**Best for production**: Set `failure_threshold=5` within a 60-second window — this trips the breaker only on sustained failure, not on isolated glitches. Set `open_duration_seconds=30` for fast recovery probing; most transient API outages resolve within 30 seconds. Register fallback values for non-critical tools (search enrichment, trending topics) so the circuit opening is invisible to users. Wire `CircuitStateChangeNotifier.on_open()` to your alerting system — a circuit tripping is a significant event that warrants immediate investigation even if users are not yet affected.
