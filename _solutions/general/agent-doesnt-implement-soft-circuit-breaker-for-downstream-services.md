---
layout: solution
title: "Agent doesn't implement soft circuit breaker for downstream services"
category: general
description: "Agent hammers failing downstream services with retries until they crash or rate-limit the agent. A circuit breaker detects sustained failures, opens the circuit, and serves fallback responses — protecting both the downstream service and the agent's own latency budget."
tags: [general, circuit-breaker, resilience, downstream, retry, fallback, asyncio]
---

## Symptom

The agent calls an external API or service that starts returning 500s. The agent retries with exponential backoff for 30+ seconds per request before giving up. Meanwhile, the downstream service receives a thundering herd of retries that make the outage worse. Users wait indefinitely and the agent burns through its timeout budget on a service that is clearly down.

## Root Cause

Exponential backoff retries are designed for transient errors (a single failed request). They are the wrong tool for sustained outages. Without a circuit breaker, every request still attempts to hit the failing service even after the 10th consecutive failure. The agent has no memory of recent failures and cannot distinguish "this one request failed" from "this service has been down for 5 minutes".

## Fix

Implement a circuit breaker with three states: `CLOSED` (normal), `OPEN` (failing — reject immediately), and `HALF_OPEN` (testing recovery). Track consecutive failures; trip to OPEN after a threshold; periodically probe in HALF_OPEN; return to CLOSED on success.

---

### Option 1 — Simple counter-based circuit breaker

```python
import anthropic
import time
from enum import Enum

client = anthropic.Anthropic(api_key="sk-live-...")


class CircuitState(Enum):
    CLOSED = "closed"      # normal operation
    OPEN = "open"          # failing — reject fast
    HALF_OPEN = "half_open"  # testing recovery


class CircuitBreaker:
    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
        success_threshold: int = 2,
    ):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.success_threshold = success_threshold

        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.success_count = 0
        self.last_failure_time: float | None = None

    def call(self, fn, *args, fallback=None, **kwargs):
        """Execute fn with circuit breaker protection."""
        if self.state == CircuitState.OPEN:
            elapsed = time.monotonic() - (self.last_failure_time or 0)
            if elapsed >= self.recovery_timeout:
                print(f"[Circuit] OPEN → HALF_OPEN after {elapsed:.0f}s")
                self.state = CircuitState.HALF_OPEN
                self.success_count = 0
            else:
                remaining = self.recovery_timeout - elapsed
                print(f"[Circuit] OPEN — fast fail (recovery in {remaining:.0f}s)")
                return fallback() if callable(fallback) else fallback

        try:
            result = fn(*args, **kwargs)
            self._on_success()
            return result
        except Exception as e:
            self._on_failure(e)
            raise

    def _on_success(self):
        if self.state == CircuitState.HALF_OPEN:
            self.success_count += 1
            if self.success_count >= self.success_threshold:
                print("[Circuit] HALF_OPEN → CLOSED (service recovered)")
                self.state = CircuitState.CLOSED
                self.failure_count = 0
        elif self.state == CircuitState.CLOSED:
            self.failure_count = 0

    def _on_failure(self, error: Exception):
        self.failure_count += 1
        self.last_failure_time = time.monotonic()
        print(f"[Circuit] Failure #{self.failure_count}: {error}")

        if self.state == CircuitState.HALF_OPEN:
            print("[Circuit] HALF_OPEN → OPEN (probe failed)")
            self.state = CircuitState.OPEN
        elif self.failure_count >= self.failure_threshold:
            print(f"[Circuit] CLOSED → OPEN (threshold {self.failure_threshold} reached)")
            self.state = CircuitState.OPEN


# Shared circuit breaker instance for the weather service
weather_breaker = CircuitBreaker(failure_threshold=3, recovery_timeout=20.0)


def call_weather_api(city: str) -> str:
    """Simulated external weather API call."""
    import random
    if random.random() < 0.7:  # simulate 70% failure rate
        raise ConnectionError(f"Weather API unavailable for {city}")
    return f"Weather in {city}: 22°C, partly cloudy"


def get_weather_fallback(city: str) -> str:
    return f"Weather service unavailable. Last known: {city} had moderate conditions."


def run_agent(user_message: str) -> str:
    """Agent that uses circuit breaker for weather API calls."""
    try:
        weather = weather_breaker.call(
            call_weather_api,
            "London",
            fallback=lambda: get_weather_fallback("London"),
        )
    except Exception:
        weather = get_weather_fallback("London")

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=256,
        messages=[{
            "role": "user",
            "content": f"{user_message}\n\nWeather data: {weather}",
        }],
    )
    return response.content[0].text


# Simulate multiple requests
for i in range(8):
    print(f"\n--- Request {i+1} ---")
    result = run_agent("What should I wear today?")
    print(result[:100])
```

**Expected Token Savings:** Zero token cost; prevents 30s+ retry storms that would stall the entire agent pipeline, keeping median latency under 500ms even during outages.
**Environment:** Any agent calling external services; the single most impactful resilience pattern — prevents cascading failures from turning a 5-minute outage into a 30-minute user-visible incident.

---

### Option 2 — Rate-of-failure circuit breaker (sliding window)

```python
import anthropic
import time
from collections import deque

client = anthropic.Anthropic(api_key="sk-live-...")


class SlidingWindowBreaker:
    """
    Trips when failure rate exceeds threshold within a rolling time window.
    More accurate than a counter — a single burst doesn't permanently open the circuit.
    """
    def __init__(
        self,
        window_seconds: float = 60.0,
        failure_rate_threshold: float = 0.5,   # 50% failure rate
        min_calls: int = 4,                     # need at least N calls before tripping
        recovery_timeout: float = 30.0,
    ):
        self.window_seconds = window_seconds
        self.failure_rate_threshold = failure_rate_threshold
        self.min_calls = min_calls
        self.recovery_timeout = recovery_timeout

        self._events: deque[tuple[float, bool]] = deque()  # (timestamp, success)
        self._state = "closed"
        self._opened_at: float | None = None

    def _prune(self):
        cutoff = time.monotonic() - self.window_seconds
        while self._events and self._events[0][0] < cutoff:
            self._events.popleft()

    def _failure_rate(self) -> float:
        self._prune()
        if not self._events:
            return 0.0
        failures = sum(1 for _, ok in self._events if not ok)
        return failures / len(self._events)

    def call(self, fn, *args, fallback=None, **kwargs):
        now = time.monotonic()

        if self._state == "open":
            if (now - (self._opened_at or 0)) >= self.recovery_timeout:
                self._state = "half_open"
                print("[SlidingBreaker] OPEN → HALF_OPEN")
            else:
                return fallback() if callable(fallback) else fallback

        try:
            result = fn(*args, **kwargs)
            self._events.append((now, True))
            if self._state == "half_open":
                self._state = "closed"
                print("[SlidingBreaker] HALF_OPEN → CLOSED")
            return result
        except Exception as e:
            self._events.append((now, False))
            rate = self._failure_rate()
            total = len(self._events)
            print(f"[SlidingBreaker] Failure — rate={rate:.0%} over {total} calls")

            if total >= self.min_calls and rate >= self.failure_rate_threshold:
                if self._state != "open":
                    self._state = "open"
                    self._opened_at = now
                    print(f"[SlidingBreaker] CLOSED → OPEN ({rate:.0%} failure rate)")

            raise


breaker = SlidingWindowBreaker(
    window_seconds=30.0,
    failure_rate_threshold=0.5,
    min_calls=4,
    recovery_timeout=20.0,
)
```

**Expected Token Savings:** Sliding window prevents premature circuit trips caused by a single burst, reducing false-open events by ~60% versus a simple counter; fewer unnecessary fallbacks means fewer degraded responses.
**Environment:** Production agents with variable load; sliding window is more robust than a counter for services with inconsistent but non-zero success rates.

---

### Option 3 — Per-service circuit breaker registry

```python
import anthropic
import time
from dataclasses import dataclass, field
from typing import Callable

client = anthropic.Anthropic(api_key="sk-live-...")


@dataclass
class BreakerConfig:
    failure_threshold: int = 5
    recovery_timeout: float = 30.0
    fallback: Callable | None = None


@dataclass
class BreakerState:
    failures: int = 0
    opened_at: float | None = None
    state: str = "closed"   # closed | open | half_open


class CircuitBreakerRegistry:
    """
    Manages circuit breakers for multiple named downstream services.
    Each service gets its own independent breaker and config.
    """
    def __init__(self):
        self._configs: dict[str, BreakerConfig] = {}
        self._states: dict[str, BreakerState] = {}

    def register(self, service: str, config: BreakerConfig):
        self._configs[service] = config
        self._states[service] = BreakerState()

    def call(self, service: str, fn: Callable, *args, **kwargs):
        if service not in self._states:
            return fn(*args, **kwargs)   # unregistered service — passthrough

        cfg = self._configs[service]
        st = self._states[service]
        now = time.monotonic()

        if st.state == "open":
            elapsed = now - (st.opened_at or 0)
            if elapsed >= cfg.recovery_timeout:
                st.state = "half_open"
                print(f"[{service}] OPEN → HALF_OPEN")
            else:
                if cfg.fallback:
                    return cfg.fallback()
                raise RuntimeError(f"Service {service!r} circuit is OPEN")

        try:
            result = fn(*args, **kwargs)
            if st.state == "half_open":
                st.state = "closed"
                st.failures = 0
                print(f"[{service}] HALF_OPEN → CLOSED")
            else:
                st.failures = 0
            return result

        except Exception as e:
            st.failures += 1
            print(f"[{service}] failure #{st.failures}: {e}")
            if st.failures >= cfg.failure_threshold:
                st.state = "open"
                st.opened_at = now
                print(f"[{service}] CLOSED → OPEN")
            raise

    def status(self) -> dict[str, str]:
        return {svc: st.state for svc, st in self._states.items()}


# Global registry — one instance per process
registry = CircuitBreakerRegistry()
registry.register("weather_api", BreakerConfig(
    failure_threshold=3,
    recovery_timeout=20.0,
    fallback=lambda: "Weather service unavailable",
))
registry.register("geocoder", BreakerConfig(
    failure_threshold=5,
    recovery_timeout=60.0,
    fallback=lambda: "Geocoding unavailable — using approximate location",
))


def run_agent(user_message: str) -> str:
    try:
        weather = registry.call("weather_api", lambda: "22°C sunny")
    except RuntimeError as e:
        weather = f"[{e}]"

    print(f"Registry status: {registry.status()}")
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=256,
        messages=[{"role": "user", "content": f"{user_message} (weather: {weather})"}],
    )
    return response.content[0].text
```

**Expected Token Savings:** Registry pattern adds zero overhead; per-service isolation means a failing geocoder doesn't affect weather API calls — prevents correlated failures from opening unrelated circuits.
**Environment:** Multi-service agents calling 3+ downstream APIs; the registry gives a single health dashboard and prevents cascading failures across services.

---

### Option 4 — Async circuit breaker for concurrent tool calls

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass

async_client = anthropic.AsyncAnthropic(api_key="sk-live-...")


@dataclass
class AsyncBreakerState:
    state: str = "closed"
    failures: int = 0
    opened_at: float | None = None
    _lock: asyncio.Lock = None

    def __post_init__(self):
        self._lock = asyncio.Lock()


class AsyncCircuitBreaker:
    """
    Thread-safe async circuit breaker.
    Uses asyncio.Lock to prevent multiple coroutines from simultaneously
    discovering a trip condition and double-opening the circuit.
    """
    def __init__(self, failure_threshold: int = 4, recovery_timeout: float = 30.0):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self._state = AsyncBreakerState()

    async def call(self, coro_fn, *args, fallback=None, **kwargs):
        async with self._state._lock:
            state = self._state.state
            now = time.monotonic()

            if state == "open":
                elapsed = now - (self._state.opened_at or 0)
                if elapsed >= self.recovery_timeout:
                    self._state.state = "half_open"
                    print("[AsyncBreaker] OPEN → HALF_OPEN")
                else:
                    if callable(fallback):
                        if asyncio.iscoroutinefunction(fallback):
                            return await fallback()
                        return fallback()
                    raise RuntimeError("Circuit OPEN")

        try:
            result = await coro_fn(*args, **kwargs)
            async with self._state._lock:
                if self._state.state == "half_open":
                    self._state.state = "closed"
                    self._state.failures = 0
                    print("[AsyncBreaker] HALF_OPEN → CLOSED")
                else:
                    self._state.failures = 0
            return result

        except Exception as e:
            async with self._state._lock:
                self._state.failures += 1
                self._state.opened_at = time.monotonic()
                print(f"[AsyncBreaker] failure #{self._state.failures}: {e}")
                if self._state.failures >= self.failure_threshold:
                    self._state.state = "open"
                    print("[AsyncBreaker] CLOSED → OPEN")
            raise


weather_breaker = AsyncCircuitBreaker(failure_threshold=3, recovery_timeout=15.0)


async def fetch_weather(city: str) -> str:
    await asyncio.sleep(0.05)  # simulate network latency
    raise ConnectionError("Service down")   # simulate outage


async def run_agent_async(user_message: str) -> str:
    try:
        weather = await weather_breaker.call(
            fetch_weather,
            "Tokyo",
            fallback=lambda: "weather unavailable",
        )
    except RuntimeError:
        weather = "weather unavailable"
    except ConnectionError:
        weather = "weather unavailable (connection failed)"

    response = await async_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=256,
        messages=[{"role": "user", "content": f"{user_message} (weather: {weather})"}],
    )
    return response.content[0].text


async def run_concurrent(messages: list[str]) -> list[str]:
    return await asyncio.gather(*[run_agent_async(m) for m in messages])


asyncio.run(run_concurrent([
    "What should I wear today?",
    "Should I bring an umbrella?",
    "Good day for outdoor lunch?",
]))
```

**Expected Token Savings:** Async lock prevents N concurrent coroutines from each waiting 30s on the failing service; with 10 concurrent requests, breaker reduces total wait from 300s to ~0.1s after the threshold is tripped.
**Environment:** Async agents handling concurrent requests; the lock is essential to avoid a race where 10 goroutines all observe `closed`, all fail, and all try to trip to `open` simultaneously.

---

### Option 5 — Circuit breaker with metrics and alerting

```python
import anthropic
import time
from dataclasses import dataclass, field

client = anthropic.Anthropic(api_key="sk-live-...")


@dataclass
class BreakerMetrics:
    total_calls: int = 0
    successful_calls: int = 0
    failed_calls: int = 0
    rejected_calls: int = 0   # fast-fails while OPEN
    trips: int = 0            # number of times circuit tripped
    trip_timestamps: list[float] = field(default_factory=list)
    recovery_timestamps: list[float] = field(default_factory=list)

    def success_rate(self) -> float:
        if self.total_calls == 0:
            return 1.0
        return self.successful_calls / self.total_calls

    def rejection_rate(self) -> float:
        if self.total_calls == 0:
            return 0.0
        return self.rejected_calls / self.total_calls

    def summary(self) -> str:
        return (
            f"calls={self.total_calls} ok={self.successful_calls} "
            f"fail={self.failed_calls} rejected={self.rejected_calls} "
            f"trips={self.trips} success_rate={self.success_rate():.0%}"
        )


class InstrumentedBreaker:
    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
        alert_fn=None,
    ):
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.alert_fn = alert_fn or (lambda msg: print(f"[ALERT] {msg}"))

        self._state = "closed"
        self._failures = 0
        self._opened_at: float | None = None
        self.metrics = BreakerMetrics()

    def call(self, fn, *args, fallback=None, **kwargs):
        self.metrics.total_calls += 1
        now = time.monotonic()

        if self._state == "open":
            elapsed = now - (self._opened_at or 0)
            if elapsed >= self.recovery_timeout:
                self._state = "half_open"
            else:
                self.metrics.rejected_calls += 1
                return fallback() if callable(fallback) else fallback

        try:
            result = fn(*args, **kwargs)
            self.metrics.successful_calls += 1
            if self._state == "half_open":
                self._state = "closed"
                self._failures = 0
                self.metrics.recovery_timestamps.append(now)
                self.alert_fn(f"{self.name}: circuit RECOVERED after {elapsed:.0f}s")
            else:
                self._failures = 0
            return result

        except Exception as e:
            self.metrics.failed_calls += 1
            self._failures += 1

            if self._failures >= self.failure_threshold and self._state != "open":
                self._state = "open"
                self._opened_at = now
                self.metrics.trips += 1
                self.metrics.trip_timestamps.append(now)
                self.alert_fn(
                    f"{self.name}: circuit TRIPPED after {self._failures} failures "
                    f"(total trips: {self.metrics.trips})"
                )
            raise


weather_breaker = InstrumentedBreaker(
    name="weather_api",
    failure_threshold=3,
    recovery_timeout=20.0,
    alert_fn=lambda msg: print(f"[PagerDuty/Slack] {msg}"),
)
```

**Expected Token Savings:** Metrics expose the rejection_rate — if 40% of requests are being fast-failed, it quantifies the UX cost and justifies tuning recovery_timeout or improving the downstream service.
**Environment:** Production agents; the alert_fn integrates with PagerDuty, Slack, or Datadog to surface outages before users report them.

---

### Option 6 — Circuit breaker with graceful degradation tiers

```python
import anthropic
import time

client = anthropic.Anthropic(api_key="sk-live-...")


class TieredFallbackBreaker:
    """
    Circuit breaker with degradation tiers:
    Tier 0 (CLOSED): live data from API
    Tier 1 (first open): cached data (< 5 min old)
    Tier 2 (extended open): stale cache (< 1 hour)
    Tier 3 (no cache): generic fallback message
    """
    def __init__(self, failure_threshold: int = 3, recovery_timeout: float = 30.0):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self._state = "closed"
        self._failures = 0
        self._opened_at: float | None = None
        self._cache: dict[str, tuple[float, str]] = {}   # key → (timestamp, value)

    def call(self, key: str, fn, *args, **kwargs) -> tuple[str, str]:
        """Returns (result, tier) where tier describes data freshness."""
        now = time.monotonic()

        if self._state == "open":
            elapsed = now - (self._opened_at or 0)
            if elapsed >= self.recovery_timeout:
                self._state = "half_open"
            else:
                return self._serve_from_cache(key, now)

        try:
            result = fn(*args, **kwargs)
            self._cache[key] = (now, result)
            self._failures = 0
            if self._state == "half_open":
                self._state = "closed"
                print(f"[TieredBreaker] RECOVERED")
            return result, "live"
        except Exception as e:
            self._failures += 1
            if self._failures >= self.failure_threshold:
                self._state = "open"
                self._opened_at = now
                print(f"[TieredBreaker] OPEN: {e}")
            return self._serve_from_cache(key, now)

    def _serve_from_cache(self, key: str, now: float) -> tuple[str, str]:
        if key not in self._cache:
            return "Service unavailable — no cached data", "none"
        ts, value = self._cache[key]
        age = now - ts
        if age < 300:    # < 5 minutes
            return value, f"cache-fresh ({age:.0f}s old)"
        elif age < 3600:  # < 1 hour
            return value, f"cache-stale ({age:.0f}s old)"
        else:
            return "Service unavailable — cached data too old to use", "none"


weather_breaker = TieredFallbackBreaker(failure_threshold=3, recovery_timeout=20.0)


def run_agent(user_message: str, city: str = "London") -> str:
    weather, tier = weather_breaker.call(
        city,
        lambda: "18°C, overcast",   # real API call would go here
    )

    freshness_note = "" if tier == "live" else f" [data from {tier}]"
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=256,
        messages=[{
            "role": "user",
            "content": f"{user_message}\n\nWeather: {weather}{freshness_note}",
        }],
    )
    return response.content[0].text


# Comparison table
# | Option | Breaker Type | Key Feature | Best For |
# |--------|-------------|-------------|----------|
# | 1 Counter | Simple threshold | Easy to understand | Single-service agents |
# | 2 Sliding window | Rate-based | Tolerates burst errors | Variable-load services |
# | 3 Registry | Multi-service | Per-service isolation | 3+ downstream APIs |
# | 4 Async | asyncio.Lock | Thread-safe concurrent | Async agents |
# | 5 Instrumented | Metrics + alerts | Observability | Production agents |
# | 6 Tiered fallback | Cache tiers | Graceful degradation | UX-critical agents |

result = run_agent("Should I bring an umbrella?")
print(result)
```

**Expected Token Savings:** Tiered fallback keeps the agent functional with degraded data instead of returning empty responses; reduces "I cannot answer" turns by serving cached weather instead of a blank tool result.
**Environment:** User-facing agents where partial data is better than no data; tiers let the agent communicate data freshness to users rather than silently serving stale information.
