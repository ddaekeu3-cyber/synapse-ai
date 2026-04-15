---
layout: solution
title: "Agent doesn't implement circuit breaker"
category: concurrency
description: "Agent keeps hammering a failing downstream service with no circuit breaker, amplifying outages and exhausting retries on requests that cannot succeed."
tags: [concurrency, circuit-breaker, reliability, retry, resilience]
---

## Symptom

When a downstream service (database, external API, internal microservice) goes down, the agent continues sending requests. Each request waits for the full timeout, burns retry budget, and delays the user. All workers pile up waiting for a service that won't recover for minutes. The agent has no mechanism to detect the outage, open a circuit, and fail fast until the service recovers.

```
t=0   Service goes down
t=5   Request 1: timeout after 30s  → retry → timeout again → fail (60s wasted)
t=65  Request 2: timeout after 30s  → retry → timeout → fail  (60s more)
t=125 Request 3: same pattern       → 20 workers stuck, memory climbing
...
t=600 Service recovers — but agent queue is saturated with timed-out retries
```

## Root Cause

The retry loop treats every failure as transient and retryable. Without tracking the failure rate over a time window, the agent cannot distinguish a single flaky error from a systemic outage. A circuit breaker tracks error rate and opens (stops sending) when the rate exceeds a threshold, allowing the service time to recover without being pummeled.

## Fix

Implement a circuit breaker with three states: Closed (normal), Open (fast-fail), and Half-Open (probe to test recovery). Wrap every external call through the breaker.

---

### Option 1 — Simple three-state circuit breaker

```python
import anthropic
import asyncio
import time
from enum import Enum, auto

async_client = anthropic.AsyncAnthropic()

class State(Enum):
    CLOSED    = auto()   # normal — requests pass through
    OPEN      = auto()   # failing — reject immediately
    HALF_OPEN = auto()   # probing — allow one request to test recovery

class CircuitBreaker:
    def __init__(
        self,
        failure_threshold: int   = 5,
        recovery_timeout: float  = 30.0,
        probe_success_threshold: int = 2,
    ) -> None:
        self._failure_threshold       = failure_threshold
        self._recovery_timeout        = recovery_timeout
        self._probe_success_threshold = probe_success_threshold

        self._state          = State.CLOSED
        self._failure_count  = 0
        self._success_count  = 0
        self._opened_at: float | None = None

    @property
    def state(self) -> State:
        if self._state == State.OPEN:
            if time.monotonic() - (self._opened_at or 0) >= self._recovery_timeout:
                self._state = State.HALF_OPEN
                self._success_count = 0
                print("[CIRCUIT] OPEN → HALF_OPEN (probing)")
        return self._state

    def record_success(self) -> None:
        if self.state == State.HALF_OPEN:
            self._success_count += 1
            if self._success_count >= self._probe_success_threshold:
                self._state = State.CLOSED
                self._failure_count = 0
                print("[CIRCUIT] HALF_OPEN → CLOSED (recovered)")
        elif self.state == State.CLOSED:
            self._failure_count = max(0, self._failure_count - 1)

    def record_failure(self) -> None:
        self._failure_count += 1
        if self.state == State.HALF_OPEN:
            self._state = State.OPEN
            self._opened_at = time.monotonic()
            print("[CIRCUIT] HALF_OPEN → OPEN (probe failed)")
        elif self._failure_count >= self._failure_threshold:
            self._state = State.OPEN
            self._opened_at = time.monotonic()
            print(f"[CIRCUIT] CLOSED → OPEN after {self._failure_count} failures")

    async def call(self, coro) -> any:
        if self.state == State.OPEN:
            raise RuntimeError("Circuit OPEN — service unavailable, failing fast")

        try:
            result = await coro
            self.record_success()
            return result
        except Exception as e:
            self.record_failure()
            raise

# Shared breaker for the external service
breaker = CircuitBreaker(failure_threshold=3, recovery_timeout=10.0)

# Simulate a flaky service: fails first N calls
_call_count = 0
_fail_until = 6   # first 6 calls fail

async def flaky_tool_call(request_id: int) -> str:
    global _call_count
    _call_count += 1
    await asyncio.sleep(0.1)
    if _call_count <= _fail_until:
        raise ConnectionError(f"Service unavailable (call {_call_count})")
    return f"OK: result for request {request_id}"

async def agent_request(request_id: int) -> str:
    try:
        result = await breaker.call(flaky_tool_call(request_id))
        return f"[{request_id}] SUCCESS: {result}"
    except RuntimeError as e:
        return f"[{request_id}] CIRCUIT: {e}"
    except Exception as e:
        return f"[{request_id}] ERROR: {e}"

async def main() -> None:
    # Send 12 requests with 1s spacing
    for i in range(1, 13):
        result = await agent_request(i)
        print(result)
        await asyncio.sleep(1.0)

asyncio.run(main())
```

**Expected Token Savings:** Eliminates wasted API calls during outages; fail-fast responses return in microseconds instead of waiting 30s per timeout; reduces error-recovery token burn by 80–95%.

**Environment:** Any async agent calling external services; single-process use; share the breaker instance across all coroutines calling the same service.

---

### Option 2 — Sliding window failure rate breaker

```python
import anthropic
import asyncio
import time
from collections import deque

async_client = anthropic.AsyncAnthropic()

class SlidingWindowBreaker:
    """
    Opens when the error rate in a sliding time window exceeds the threshold.
    More accurate than a simple counter under variable load.
    """

    def __init__(
        self,
        window_seconds: float = 60.0,
        error_rate_threshold: float = 0.5,   # 50% errors → open
        min_calls_in_window: int = 5,         # need at least N calls to evaluate
        recovery_timeout: float = 20.0,
    ) -> None:
        self._window   = window_seconds
        self._threshold = error_rate_threshold
        self._min_calls = min_calls_in_window
        self._recovery  = recovery_timeout
        self._events: deque[tuple[float, bool]] = deque()  # (timestamp, is_error)
        self._open_at: float | None = None

    def _prune(self) -> None:
        cutoff = time.monotonic() - self._window
        while self._events and self._events[0][0] < cutoff:
            self._events.popleft()

    @property
    def is_open(self) -> bool:
        if self._open_at is not None:
            if time.monotonic() - self._open_at >= self._recovery:
                print("[BREAKER] Attempting recovery (half-open probe)")
                self._open_at = None   # allow one probe
            else:
                return True
        return False

    def _evaluate(self) -> None:
        self._prune()
        if len(self._events) < self._min_calls:
            return
        errors = sum(1 for _, is_err in self._events if is_err)
        rate = errors / len(self._events)
        if rate >= self._threshold:
            self._open_at = time.monotonic()
            print(f"[BREAKER] OPENED: error_rate={rate:.0%} ({errors}/{len(self._events)} in window)")

    def record(self, is_error: bool) -> None:
        self._events.append((time.monotonic(), is_error))
        if is_error:
            self._evaluate()
        elif self._open_at is None:
            self._prune()

    async def call(self, coro) -> any:
        if self.is_open:
            raise RuntimeError(f"Circuit open — error rate exceeded {self._threshold:.0%} threshold")
        try:
            result = await coro
            self.record(is_error=False)
            return result
        except Exception:
            self.record(is_error=True)
            raise

sw_breaker = SlidingWindowBreaker(
    window_seconds=30.0,
    error_rate_threshold=0.6,
    min_calls_in_window=4,
    recovery_timeout=8.0,
)

_request_num = 0

async def external_api(req_id: int) -> str:
    global _request_num
    _request_num += 1
    await asyncio.sleep(0.05)
    # Fail 70% of requests 5–15, then recover
    if 5 <= _request_num <= 15 and (_request_num % 10) < 7:
        raise RuntimeError(f"External API error (call {_request_num})")
    return f"data_for_{req_id}"

async def main() -> None:
    for i in range(25):
        try:
            result = await sw_breaker.call(external_api(i))
            print(f"[{i:02d}] OK: {result}")
        except RuntimeError as e:
            print(f"[{i:02d}] BREAKER: {e}")
        except Exception as e:
            print(f"[{i:02d}] ERROR: {e}")
        await asyncio.sleep(0.3)

asyncio.run(main())
```

**Expected Token Savings:** Sliding window prevents both premature opening (single error) and staying closed too long under sustained failure; accurately tracks real error rate across varying load.

**Environment:** Production agents with variable request rates; sliding window is more accurate than a fixed counter for bursty traffic patterns.

---

### Option 3 — Per-service breaker registry for multi-dependency agents

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass, field

async_client = anthropic.AsyncAnthropic()

@dataclass
class BreakerConfig:
    name: str
    failure_threshold: int   = 5
    recovery_timeout: float  = 30.0

@dataclass
class BreakerState:
    config: BreakerConfig
    failures: int       = 0
    opened_at: float    = 0.0
    is_open: bool       = False
    total_opens: int    = 0
    total_fast_fails: int = 0

class BreakerRegistry:
    """Central registry for all circuit breakers in an agent."""

    def __init__(self) -> None:
        self._breakers: dict[str, BreakerState] = {}

    def register(self, config: BreakerConfig) -> None:
        self._breakers[config.name] = BreakerState(config)

    def _get(self, name: str) -> BreakerState:
        if name not in self._breakers:
            # Auto-register with defaults
            self.register(BreakerConfig(name=name))
        return self._breakers[name]

    def is_available(self, name: str) -> bool:
        b = self._get(name)
        if b.is_open:
            if time.monotonic() - b.opened_at >= b.config.recovery_timeout:
                b.is_open = False
                b.failures = 0
                print(f"[REGISTRY] '{name}' → HALF_OPEN")
            else:
                b.total_fast_fails += 1
                return False
        return True

    def record_success(self, name: str) -> None:
        b = self._get(name)
        b.failures = max(0, b.failures - 1)

    def record_failure(self, name: str) -> None:
        b = self._get(name)
        b.failures += 1
        if b.failures >= b.config.failure_threshold:
            b.is_open = True
            b.opened_at = time.monotonic()
            b.total_opens += 1
            print(f"[REGISTRY] '{name}' OPENED (failure #{b.failures})")

    async def call(self, service_name: str, coro) -> any:
        if not self.is_available(service_name):
            raise RuntimeError(f"Service '{service_name}' circuit is OPEN")
        try:
            result = await coro
            self.record_success(service_name)
            return result
        except Exception:
            self.record_failure(service_name)
            raise

    def status(self) -> dict:
        return {
            name: {
                "open": b.is_open,
                "failures": b.failures,
                "total_opens": b.total_opens,
                "fast_fails": b.total_fast_fails,
            }
            for name, b in self._breakers.items()
        }

registry = BreakerRegistry()
registry.register(BreakerConfig("anthropic-api",  failure_threshold=3, recovery_timeout=15.0))
registry.register(BreakerConfig("database",       failure_threshold=5, recovery_timeout=30.0))
registry.register(BreakerConfig("search-service", failure_threshold=4, recovery_timeout=20.0))

_db_fail_count = 0

async def call_database(query: str) -> str:
    global _db_fail_count
    _db_fail_count += 1
    await asyncio.sleep(0.05)
    if _db_fail_count <= 6:
        raise ConnectionError("Database unreachable")
    return f"DB result for: {query}"

async def call_search(query: str) -> str:
    await asyncio.sleep(0.02)
    return f"Search results for: {query}"

async def agent_task(task_id: int) -> str:
    results = []
    try:
        db_result = await registry.call("database", call_database(f"query_{task_id}"))
        results.append(f"DB={db_result}")
    except Exception as e:
        results.append(f"DB=FAIL({e})")

    try:
        search_result = await registry.call("search-service", call_search(f"search_{task_id}"))
        results.append(f"SEARCH={search_result}")
    except Exception as e:
        results.append(f"SEARCH=FAIL({e})")

    return f"[task {task_id:02d}] " + " | ".join(results)

async def main() -> None:
    tasks = [agent_task(i) for i in range(15)]
    for coro in tasks:
        result = await coro
        print(result)
        await asyncio.sleep(0.2)

    import json
    print("\nRegistry status:", json.dumps(registry.status(), indent=2))

asyncio.run(main())
```

**Expected Token Savings:** Isolated per-service breakers prevent one failing dependency from degrading unrelated services; registry provides observability into which services are failing.

**Environment:** Agents with multiple downstream dependencies (database, search, external APIs); each service gets an independently tunable breaker.

---

### Option 4 — Breaker with fallback response on open state

```python
import anthropic
import asyncio
import time

async_client = anthropic.AsyncAnthropic()

class BreakerWithFallback:
    """
    Circuit breaker that returns a fallback value when open,
    allowing the agent to continue with degraded functionality
    rather than failing outright.
    """

    def __init__(self, failure_threshold: int = 4, recovery_timeout: float = 15.0) -> None:
        self._threshold = failure_threshold
        self._recovery  = recovery_timeout
        self._failures  = 0
        self._opened_at: float | None = None

    @property
    def is_open(self) -> bool:
        if self._opened_at and time.monotonic() - self._opened_at >= self._recovery:
            self._opened_at = None
            self._failures  = 0
        return self._opened_at is not None

    def _open(self) -> None:
        self._opened_at = time.monotonic()
        print(f"[BREAKER] Circuit OPENED")

    async def call(self, coro, fallback=None):
        if self.is_open:
            print(f"[BREAKER] Fast-failing, using fallback: {fallback!r}")
            return fallback

        try:
            result = await coro
            self._failures = max(0, self._failures - 1)
            return result
        except Exception as e:
            self._failures += 1
            if self._failures >= self._threshold:
                self._open()
            print(f"[BREAKER] Failure #{self._failures}: {e}")
            return fallback

# Example: product search with fallback to cached results
CACHE = {"laptop": ["Dell XPS", "MacBook Pro"], "phone": ["iPhone 15", "Pixel 8"]}

_search_fail = True   # simulate search service being down

async def search_products(query: str) -> list[str]:
    await asyncio.sleep(0.1)
    if _search_fail:
        raise ConnectionError("Search service down")
    return [f"Result for {query}"]

search_breaker = BreakerWithFallback(failure_threshold=3, recovery_timeout=10.0)

async def agent_search(query: str) -> dict:
    fallback = CACHE.get(query, [f"Cached: no results for {query}"])
    results = await search_breaker.call(
        search_products(query),
        fallback=fallback,
    )
    source = "live" if not search_breaker.is_open else "fallback"
    return {"query": query, "results": results, "source": source}

async def main() -> None:
    queries = ["laptop", "phone", "tablet", "laptop", "phone"]
    for q in queries:
        result = await agent_search(q)
        print(f"[{result['source'].upper():8}] {q}: {result['results']}")
        await asyncio.sleep(0.5)

asyncio.run(main())
```

**Expected Token Savings:** Fallback responses allow the agent to continue producing output during outages; avoids burning tokens on LLM error-handling logic when cached results are sufficient.

**Environment:** Agents with cacheable or approximable responses; product search, recommendation, and lookup agents all benefit from graceful fallbacks.

---

### Option 5 — Async breaker with health probe coroutine

```python
import anthropic
import asyncio
import time

async_client = anthropic.AsyncAnthropic()

class ProbingBreaker:
    """
    Circuit breaker that actively probes the service in the background
    rather than waiting for a real request to test recovery.
    """

    def __init__(
        self,
        failure_threshold: int    = 4,
        probe_interval: float     = 5.0,
        probe_timeout: float      = 2.0,
    ) -> None:
        self._threshold     = failure_threshold
        self._probe_interval = probe_interval
        self._probe_timeout  = probe_timeout
        self._failures       = 0
        self._is_open        = False
        self._probe_task: asyncio.Task | None = None

    async def start(self, health_check_coro_fn) -> None:
        """Start background health probe loop."""
        self._health_fn = health_check_coro_fn
        self._probe_task = asyncio.create_task(self._probe_loop())

    async def stop(self) -> None:
        if self._probe_task:
            self._probe_task.cancel()

    async def _probe_loop(self) -> None:
        while True:
            await asyncio.sleep(self._probe_interval)
            if not self._is_open:
                continue
            try:
                await asyncio.wait_for(self._health_fn(), timeout=self._probe_timeout)
                self._is_open  = False
                self._failures = 0
                print("[PROBE] Service recovered → CLOSED")
            except Exception:
                print("[PROBE] Service still down → staying OPEN")

    async def call(self, coro) -> any:
        if self._is_open:
            raise RuntimeError("Circuit OPEN — service unavailable")
        try:
            result = await coro
            self._failures = max(0, self._failures - 1)
            return result
        except Exception:
            self._failures += 1
            if self._failures >= self._threshold:
                self._is_open = True
                print(f"[BREAKER] OPENED after {self._failures} failures")
            raise

# Simulated service: down for 10s, then recovers
_service_start = time.monotonic()
_down_duration = 10.0

async def external_service(req_id: int) -> str:
    await asyncio.sleep(0.05)
    if time.monotonic() - _service_start < _down_duration:
        raise ConnectionError("Service down")
    return f"response_{req_id}"

async def health_check() -> None:
    await external_service(-1)  # raises if service is down

async def main() -> None:
    breaker = ProbingBreaker(failure_threshold=3, probe_interval=3.0)
    await breaker.start(health_check)

    for i in range(30):
        try:
            result = await breaker.call(external_service(i))
            print(f"[{i:02d}] OK: {result}")
        except RuntimeError as e:
            print(f"[{i:02d}] OPEN: {e}")
        except Exception as e:
            print(f"[{i:02d}] ERR: {e}")
        await asyncio.sleep(0.8)

    await breaker.stop()

asyncio.run(main())
```

**Expected Token Savings:** Active health probing means recovery is detected within `probe_interval` seconds rather than waiting for the next real request; reduces false-open duration by 50–80%.

**Environment:** Async agents in long-running daemon processes; background probe is low-cost and eliminates recovery latency.

---

### Option 6 — Breaker integrated with Anthropic API calls

```python
import anthropic
import asyncio
import time

async_client = anthropic.AsyncAnthropic()

class AnthropicCircuitBreaker:
    """
    Circuit breaker specifically tuned for Anthropic API failures.
    Distinguishes retriable errors (529 overload, 500) from non-retriable ones
    (401 auth, 400 bad request) — only retriable errors trip the circuit.
    """

    RETRIABLE_STATUS = {500, 502, 503, 529}   # server-side / overload errors
    NON_RETRIABLE    = {400, 401, 403, 404}   # client errors — don't trip circuit

    def __init__(self, failure_threshold: int = 5, recovery_timeout: float = 60.0) -> None:
        self._threshold = failure_threshold
        self._recovery  = recovery_timeout
        self._failures  = 0
        self._opened_at: float | None = None

    @property
    def is_open(self) -> bool:
        if self._opened_at and time.monotonic() - self._opened_at >= self._recovery:
            self._opened_at = None
            self._failures  = 0
            print("[ANTHROPIC BREAKER] Attempting recovery")
        return self._opened_at is not None

    def _is_retriable(self, exc: Exception) -> bool:
        if isinstance(exc, anthropic.APIStatusError):
            return exc.status_code in self.RETRIABLE_STATUS
        if isinstance(exc, (anthropic.APIConnectionError, anthropic.APITimeoutError)):
            return True
        return False

    async def create_message(self, **kwargs) -> anthropic.types.Message:
        if self.is_open:
            raise RuntimeError(
                "Anthropic API circuit OPEN — overload/error threshold exceeded. "
                "Retry after backoff."
            )

        try:
            msg = await async_client.messages.create(**kwargs)
            self._failures = max(0, self._failures - 1)
            return msg
        except Exception as e:
            if self._is_retriable(e):
                self._failures += 1
                if self._failures >= self._threshold:
                    self._opened_at = time.monotonic()
                    print(f"[ANTHROPIC BREAKER] OPENED after {self._failures} retriable errors")
            raise

api_breaker = AnthropicCircuitBreaker(failure_threshold=3, recovery_timeout=30.0)

async def agent_call(user_message: str, request_id: int) -> str:
    try:
        response = await api_breaker.create_message(
            model="claude-haiku-4-5-20251001",
            max_tokens=64,
            messages=[{"role": "user", "content": user_message}],
        )
        return f"[{request_id}] {response.content[0].text.strip()}"
    except RuntimeError as e:
        return f"[{request_id}] CIRCUIT OPEN: {e}"
    except anthropic.APIError as e:
        return f"[{request_id}] API ERROR: {e}"

async def main() -> None:
    messages = [
        "What is 1+1?",
        "Name a planet.",
        "What color is the sky?",
        "What is the capital of France?",
        "Name a programming language.",
    ]
    tasks = [agent_call(msg, i) for i, msg in enumerate(messages)]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for r in results:
        print(r)

asyncio.run(main())
```

**Expected Token Savings:** Non-retriable errors (400, 401) don't trip the circuit — only server-side failures do; prevents the breaker from opening on client bugs while still protecting against API overload.

**Environment:** Any agent using the Anthropic API in production; tune `recovery_timeout` to match Anthropic's typical recovery time for 529 overload responses (usually 30–120s).

---

## Comparison

| Option | Failure Detection | Recovery | Fallback | Best For |
|--------|-----------------|---------|---------|---------|
| 1 — Three-state | Fixed counter | Probe via real request | None | General use |
| 2 — Sliding window | Error rate % | Probe via real request | None | Variable load |
| 3 — Registry | Per-service counter | Per-service timeout | None | Multi-dependency |
| 4 — With fallback | Fixed counter | Probe via real request | Cached value | Degradable features |
| 5 — Active probe | Fixed counter | Background health check | None | Long-running daemons |
| 6 — Anthropic-aware | Retriable error filter | Probe via real request | None | Anthropic API calls |

**Recommended default:** Option 1 (three-state) for most agents — minimal code, correct behavior, zero dependencies. Add Option 4 (fallback) for features where degraded-mode output is acceptable. Use Option 6 for Anthropic API calls to avoid opening the circuit on client errors.
