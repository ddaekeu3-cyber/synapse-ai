---
layout: solution
title: "Agent Doesn't Implement Circuit Breaker per Downstream Dependency"
category: reliability
description: "Give each downstream dependency (database, search API, tool endpoint) its own independent circuit breaker so a failure in one service doesn't cascade to unrelated services or block the entire agent."
tags: [reliability, circuit-breaker, resilience, dependencies, fault-isolation, cascading-failures, async]
---

# Agent Doesn't Implement Circuit Breaker per Downstream Dependency

## Problem

An agent calls multiple downstream services: a vector database for retrieval, a web search API, a SQL database, and several tool endpoints. When the search API starts returning errors, a single shared circuit breaker opens and blocks ALL downstream calls — including the healthy database. A per-dependency circuit breaker isolates failures: the search breaker opens while the database breaker stays closed, and the agent degrades gracefully instead of failing completely.

## Solution Options

### Option 1: Per-Dependency Circuit Breaker Registry

```python
import anthropic
import time
from dataclasses import dataclass, field
from enum import Enum


class BreakerState(Enum):
    CLOSED   = "closed"    # normal operation
    OPEN     = "open"      # failing — reject calls
    HALF_OPEN = "half_open"  # testing recovery


@dataclass
class CircuitBreaker:
    name: str
    failure_threshold: int = 3
    recovery_timeout: float = 30.0

    _failures: int = field(default=0, init=False)
    _state: BreakerState = field(default=BreakerState.CLOSED, init=False)
    _opened_at: float = field(default=0.0, init=False)

    def call(self, fn, *args, **kwargs):
        if self._state == BreakerState.OPEN:
            if time.monotonic() - self._opened_at > self.recovery_timeout:
                self._state = BreakerState.HALF_OPEN
            else:
                raise RuntimeError(f"[{self.name}] Circuit OPEN — service unavailable")

        try:
            result = fn(*args, **kwargs)
            self._on_success()
            return result
        except Exception as e:
            self._on_failure()
            raise

    def _on_success(self) -> None:
        self._failures = 0
        self._state = BreakerState.CLOSED

    def _on_failure(self) -> None:
        self._failures += 1
        if self._failures >= self.failure_threshold:
            self._state = BreakerState.OPEN
            self._opened_at = time.monotonic()
            print(f"[circuit] {self.name} OPENED after {self._failures} failures")

    @property
    def state(self) -> BreakerState:
        return self._state


class DependencyRegistry:
    """Registry of per-dependency circuit breakers."""

    def __init__(self) -> None:
        self._breakers: dict[str, CircuitBreaker] = {}

    def breaker(self, name: str, failure_threshold: int = 3, recovery_timeout: float = 30.0) -> CircuitBreaker:
        if name not in self._breakers:
            self._breakers[name] = CircuitBreaker(name, failure_threshold, recovery_timeout)
        return self._breakers[name]

    def status(self) -> dict[str, str]:
        return {name: b.state.value for name, b in self._breakers.items()}


registry = DependencyRegistry()


def search_api(query: str) -> list[str]:
    """Simulated search API that fails on certain queries."""
    if "fail" in query.lower():
        raise ConnectionError("Search API timeout")
    return [f"Result for: {query}"]


def vector_db(query: str) -> list[str]:
    """Simulated vector DB — always healthy."""
    return [f"Vector result: {query}"]


def protected_search(query: str) -> list[str]:
    breaker = registry.breaker("search_api", failure_threshold=2)
    return breaker.call(search_api, query)


def protected_vector(query: str) -> list[str]:
    breaker = registry.breaker("vector_db", failure_threshold=2)
    return breaker.call(vector_db, query)


def agent_handler(user_query: str) -> str:
    client = anthropic.Anthropic()
    context_parts = []

    # Try search (may fail)
    try:
        results = protected_search(user_query)
        context_parts.append(f"Search: {results[0]}")
    except RuntimeError as e:
        context_parts.append(f"Search unavailable: {e}")

    # Try vector DB (independent breaker — unaffected by search failures)
    try:
        vectors = protected_vector(user_query)
        context_parts.append(f"Vector: {vectors[0]}")
    except RuntimeError as e:
        context_parts.append(f"Vector unavailable: {e}")

    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        system=f"Context:\n{chr(10).join(context_parts)}",
        messages=[{"role": "user", "content": user_query}],
    )
    return resp.content[0].text


if __name__ == "__main__":
    queries = ["What is AI?", "fail this query", "fail again", "What is ML?"]
    for q in queries:
        result = agent_handler(q)
        print(f"Q: {q[:30]:<30} Status: {registry.status()}")

# Expected Token Savings: No extra tokens; isolated breakers prevent healthy services from being blocked
# Environment: Agents with 2+ independent downstream dependencies of varying reliability
```

---

### Option 2: Async Per-Dependency Breaker with Fallback

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Awaitable


class State(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class AsyncBreaker:
    name: str
    threshold: int = 3
    timeout: float = 20.0
    fallback: Any = None  # default value when open

    _failures: int = field(default=0, init=False)
    _state: State = field(default=State.CLOSED, init=False)
    _opened_at: float = field(default=0.0, init=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)

    async def call(self, fn: Callable[..., Awaitable], *args, **kwargs) -> Any:
        async with self._lock:
            if self._state == State.OPEN:
                if time.monotonic() - self._opened_at > self.timeout:
                    self._state = State.HALF_OPEN
                else:
                    print(f"[{self.name}] OPEN — returning fallback")
                    return self.fallback

        try:
            result = await fn(*args, **kwargs)
            async with self._lock:
                self._failures = 0
                if self._state == State.HALF_OPEN:
                    self._state = State.CLOSED
                    print(f"[{self.name}] Recovered → CLOSED")
            return result
        except Exception as e:
            async with self._lock:
                self._failures += 1
                if self._failures >= self.threshold:
                    self._state = State.OPEN
                    self._opened_at = time.monotonic()
                    print(f"[{self.name}] OPENED (failure #{self._failures}): {e}")
            if self.fallback is not None:
                return self.fallback
            raise


# Dependency simulators
_search_fail_count = 0

async def search_service(query: str) -> list[str]:
    global _search_fail_count
    _search_fail_count += 1
    if _search_fail_count <= 4:
        raise ConnectionError("Search down")
    return [f"search: {query}"]


async def db_service(query: str) -> dict:
    return {"db_result": f"data for {query}"}


async def llm_tool(query: str) -> str:
    return f"tool: processed {query}"


# Per-dependency breakers with fallbacks
BREAKERS = {
    "search": AsyncBreaker("search", threshold=2, fallback=[]),
    "database": AsyncBreaker("database", threshold=3, fallback={}),
    "llm_tool": AsyncBreaker("llm_tool", threshold=2, fallback="tool unavailable"),
}


async def resilient_agent(query: str) -> str:
    search_results, db_result, tool_result = await asyncio.gather(
        BREAKERS["search"].call(search_service, query),
        BREAKERS["database"].call(db_service, query),
        BREAKERS["llm_tool"].call(llm_tool, query),
        return_exceptions=False,
    )
    client = anthropic.AsyncAnthropic()
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251011" if False else "claude-haiku-4-5-20251001",
        max_tokens=64,
        system=f"search={search_results}, db={db_result}, tool={tool_result}",
        messages=[{"role": "user", "content": query}],
    )
    await client.close()
    return resp.content[0].text


async def main() -> None:
    for i in range(8):
        result = await resilient_agent(f"Query {i}")
        states = {k: b._state.value for k, b in BREAKERS.items()}
        print(f"[{i}] {states} → {result[:40]}")


if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: No extra tokens; fallback values allow partial response even when services fail
# Environment: Async agents with multiple concurrent service calls and varying SLAs per dependency
```

---

### Option 3: Circuit Breaker with Success Rate Window

```python
import anthropic
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum


class State(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class WindowedBreaker:
    """
    Circuit breaker based on error rate over a rolling time window,
    rather than a fixed failure count. More resilient to transient errors.
    """

    def __init__(
        self,
        name: str,
        window_seconds: float = 60.0,
        error_rate_threshold: float = 0.5,  # open if >50% errors
        min_requests: int = 5,              # require minimum sample size
        recovery_timeout: float = 30.0,
    ) -> None:
        self.name = name
        self._window = window_seconds
        self._error_threshold = error_rate_threshold
        self._min_requests = min_requests
        self._recovery_timeout = recovery_timeout
        self._events: deque[tuple[float, bool]] = deque()  # (ts, is_failure)
        self._state = State.CLOSED
        self._opened_at = 0.0

    def _prune(self) -> None:
        cutoff = time.monotonic() - self._window
        while self._events and self._events[0][0] < cutoff:
            self._events.popleft()

    def _error_rate(self) -> float:
        self._prune()
        if not self._events:
            return 0.0
        failures = sum(1 for _, is_fail in self._events if is_fail)
        return failures / len(self._events)

    def call(self, fn, *args, **kwargs):
        if self._state == State.OPEN:
            if time.monotonic() - self._opened_at > self._recovery_timeout:
                self._state = State.HALF_OPEN
            else:
                raise RuntimeError(f"[{self.name}] OPEN (error_rate={self._error_rate():.0%})")

        try:
            result = fn(*args, **kwargs)
            self._events.append((time.monotonic(), False))
            if self._state == State.HALF_OPEN:
                self._state = State.CLOSED
                print(f"[{self.name}] Probe success → CLOSED")
            return result
        except Exception as e:
            self._events.append((time.monotonic(), True))
            rate = self._error_rate()
            n = len(self._events)
            if n >= self._min_requests and rate >= self._error_threshold:
                self._state = State.OPEN
                self._opened_at = time.monotonic()
                print(f"[{self.name}] OPEN (error_rate={rate:.0%}, n={n})")
            raise

    @property
    def stats(self) -> dict:
        self._prune()
        return {"state": self._state.value, "error_rate": f"{self._error_rate():.0%}", "samples": len(self._events)}


# Service simulators
_sql_failures = 0

def sql_db(query: str) -> str:
    global _sql_failures
    _sql_failures += 1
    if 3 <= _sql_failures <= 8:
        raise TimeoutError("SQL slow query timeout")
    return f"SQL: {query}"


def cache_service(key: str) -> str:
    return f"CACHED: {key}"


BREAKERS = {
    "sql_db": WindowedBreaker("sql_db", window_seconds=30, error_rate_threshold=0.4, min_requests=3),
    "cache":  WindowedBreaker("cache", window_seconds=30, error_rate_threshold=0.5, min_requests=3),
}


def agent_call(query: str) -> str:
    client = anthropic.Anthropic()
    context_parts = []

    for name, fn, args in [("sql_db", sql_db, query), ("cache", cache_service, query)]:
        try:
            result = BREAKERS[name].call(fn, args)
            context_parts.append(f"{name}: {result}")
        except RuntimeError as e:
            context_parts.append(f"{name}: unavailable")
        except Exception:
            context_parts.append(f"{name}: error")

    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        system="\n".join(context_parts),
        messages=[{"role": "user", "content": query}],
    )
    return resp.content[0].text


if __name__ == "__main__":
    for i in range(12):
        result = agent_call(f"query_{i}")
        stats = {k: b.stats for k, b in BREAKERS.items()}
        print(f"[{i:02d}] {stats['sql_db']} | {result[:30]}")

# Expected Token Savings: No extra tokens; rate-based breaker avoids false-opens from isolated blips
# Environment: Agents with bursty traffic patterns where fixed-count breakers open too aggressively
```

---

### Option 4: Dependency Health Dashboard with Auto-Healing

```python
import anthropic
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable


class Health(Enum):
    HEALTHY   = "healthy"
    DEGRADED  = "degraded"
    UNHEALTHY = "unhealthy"


@dataclass
class DependencyHealth:
    name: str
    total_calls: int = 0
    failures: int = 0
    consecutive_failures: int = 0
    consecutive_successes: int = 0
    last_check: float = field(default_factory=time.monotonic)
    state_history: list[str] = field(default_factory=list)

    @property
    def health(self) -> Health:
        if self.total_calls == 0:
            return Health.HEALTHY
        failure_rate = self.failures / self.total_calls
        if self.consecutive_failures >= 5 or failure_rate > 0.7:
            return Health.UNHEALTHY
        if self.consecutive_failures >= 2 or failure_rate > 0.3:
            return Health.DEGRADED
        return Health.HEALTHY

    def record_success(self) -> None:
        self.total_calls += 1
        self.consecutive_failures = 0
        self.consecutive_successes += 1

    def record_failure(self) -> None:
        self.total_calls += 1
        self.failures += 1
        self.consecutive_failures += 1
        self.consecutive_successes = 0


class DependencyHealthDashboard:
    """
    Tracks health of each dependency and routes calls based on health status.
    Provides a unified health dashboard for operations visibility.
    """

    def __init__(self) -> None:
        self._deps: dict[str, DependencyHealth] = {}
        self._breaker_open: dict[str, bool] = {}

    def register(self, name: str) -> None:
        self._deps[name] = DependencyHealth(name=name)
        self._breaker_open[name] = False

    def call(self, name: str, fn: Callable, *args, **kwargs):
        if name not in self._deps:
            self.register(name)

        dep = self._deps[name]

        if self._breaker_open.get(name) and dep.consecutive_failures < 2:
            self._breaker_open[name] = False  # attempt recovery

        if self._breaker_open.get(name):
            raise RuntimeError(f"[{name}] breaker open")

        try:
            result = fn(*args, **kwargs)
            dep.record_success()
            if dep.health == Health.HEALTHY and dep.state_history[-1:] != ["healthy"]:
                dep.state_history.append("healthy")
                print(f"[health] {name} → HEALTHY")
            return result
        except Exception as e:
            dep.record_failure()
            h = dep.health
            if h != Health.HEALTHY:
                if not dep.state_history or dep.state_history[-1] != h.value:
                    dep.state_history.append(h.value)
                    print(f"[health] {name} → {h.value.upper()}")
            if h == Health.UNHEALTHY:
                self._breaker_open[name] = True
            raise

    def dashboard(self) -> str:
        lines = ["=== Dependency Health ==="]
        for name, dep in self._deps.items():
            h = dep.health
            rate = dep.failures / max(dep.total_calls, 1) * 100
            breaker = " [OPEN]" if self._breaker_open.get(name) else ""
            lines.append(
                f"  {name:<20} {h.value:<10} calls={dep.total_calls} "
                f"failures={dep.failures} ({rate:.0f}%){breaker}"
            )
        return "\n".join(lines)


dashboard = DependencyHealthDashboard()

_embed_fail_n = 0
def embedding_service(text: str) -> list[float]:
    global _embed_fail_n
    _embed_fail_n += 1
    if 2 <= _embed_fail_n <= 7:
        raise ConnectionError("Embedding service unavailable")
    return [0.1, 0.2, 0.3]

def sql_service(query: str) -> str:
    return f"SQL result: {query}"


def agent_with_dashboard(query: str) -> str:
    client = anthropic.Anthropic()
    context = []

    for dep_name, fn, arg in [("embeddings", embedding_service, query), ("sql", sql_service, query)]:
        try:
            result = dashboard.call(dep_name, fn, arg)
            context.append(f"{dep_name}: ok ({str(result)[:30]})")
        except RuntimeError:
            context.append(f"{dep_name}: breaker open")
        except Exception:
            context.append(f"{dep_name}: error")

    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        system="\n".join(context),
        messages=[{"role": "user", "content": query}],
    )
    return resp.content[0].text


if __name__ == "__main__":
    for i in range(12):
        agent_with_dashboard(f"query {i}")

    print(dashboard.dashboard())

# Expected Token Savings: No extra tokens; health dashboard surfaces degraded dependencies before they cascade
# Environment: Production agents with ops teams monitoring per-dependency health via dashboards
```

---

### Option 5: Bulkhead + Circuit Breaker Combination

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum


class State(Enum):
    CLOSED = "closed"
    OPEN = "open"


@dataclass
class BulkheadBreaker:
    """
    Combines bulkhead (max concurrent calls) with circuit breaker.
    Prevents a slow dependency from consuming all available workers.
    """

    name: str
    max_concurrent: int = 3      # bulkhead limit
    failure_threshold: int = 3
    recovery_timeout: float = 20.0

    _semaphore: asyncio.Semaphore = field(init=False)
    _failures: int = field(default=0, init=False)
    _state: State = field(default=State.CLOSED, init=False)
    _opened_at: float = field(default=0.0, init=False)
    _concurrent: int = field(default=0, init=False)
    _rejected: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        self._semaphore = asyncio.Semaphore(self.max_concurrent)

    async def call(self, fn, *args, **kwargs):
        # Circuit breaker check
        if self._state == State.OPEN:
            if time.monotonic() - self._opened_at > self.recovery_timeout:
                self._state = State.CLOSED
                self._failures = 0
                print(f"[{self.name}] Attempting recovery")
            else:
                raise RuntimeError(f"[{self.name}] Circuit OPEN")

        # Bulkhead check
        acquired = self._semaphore._value > 0
        if not acquired:
            self._rejected += 1
            raise RuntimeError(f"[{self.name}] Bulkhead full ({self.max_concurrent} concurrent)")

        async with self._semaphore:
            self._concurrent += 1
            try:
                result = await fn(*args, **kwargs)
                self._failures = 0
                return result
            except Exception as e:
                self._failures += 1
                if self._failures >= self.failure_threshold:
                    self._state = State.OPEN
                    self._opened_at = time.monotonic()
                    print(f"[{self.name}] OPENED: {e}")
                raise
            finally:
                self._concurrent -= 1

    def stats(self) -> dict:
        return {
            "name": self.name,
            "state": self._state.value,
            "concurrent": self._concurrent,
            "failures": self._failures,
            "rejected": self._rejected,
        }


async def slow_service(name: str, delay: float, fail: bool = False) -> str:
    await asyncio.sleep(delay)
    if fail:
        raise TimeoutError(f"{name} timed out")
    return f"{name}: ok"


BULKHEADS = {
    "search": BulkheadBreaker("search", max_concurrent=2, failure_threshold=2),
    "db":     BulkheadBreaker("db", max_concurrent=4, failure_threshold=3),
}

_call_count = 0

async def call_services(query: str) -> dict:
    global _call_count
    _call_count += 1
    fail = _call_count in (3, 4, 5)

    results = {}
    for dep, breaker in BULKHEADS.items():
        try:
            results[dep] = await breaker.call(
                slow_service, dep, delay=0.1, fail=(fail and dep == "search")
            )
        except RuntimeError as e:
            results[dep] = f"blocked: {e}"
        except Exception as e:
            results[dep] = f"error: {e}"
    return results


async def main() -> None:
    client = anthropic.AsyncAnthropic()

    # Run 10 concurrent requests
    tasks = [call_services(f"query_{i}") for i in range(10)]
    all_results = await asyncio.gather(*tasks)

    for i, res in enumerate(all_results):
        print(f"[{i:02d}] {res}")

    print("\nBulkhead stats:")
    for b in BULKHEADS.values():
        print(f"  {b.stats()}")

    await client.close()


if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: No extra tokens; bulkhead prevents slow dependency from starving others
# Environment: High-concurrency async agents where one slow dependency can exhaust thread/task pools
```

---

### Option 6: Adaptive Circuit Breaker with Exponential Backoff Recovery

```python
import anthropic
import time
import random
from dataclasses import dataclass, field
from enum import Enum


class State(Enum):
    CLOSED    = "closed"
    OPEN      = "open"
    HALF_OPEN = "half_open"


@dataclass
class AdaptiveBreaker:
    """
    Exponential backoff on recovery: first probe after 5s, then 10s, 20s, 40s...
    Prevents repeated failed probes from hammering a recovering service.
    """

    name: str
    threshold: int = 3
    base_timeout: float = 5.0
    max_timeout: float = 300.0
    jitter: float = 0.2

    _failures: int = field(default=0, init=False)
    _state: State = field(default=State.CLOSED, init=False)
    _opened_at: float = field(default=0.0, init=False)
    _recovery_attempt: int = field(default=0, init=False)
    _success_history: list[bool] = field(default_factory=list, init=False)

    def _current_timeout(self) -> float:
        raw = min(self.base_timeout * (2 ** self._recovery_attempt), self.max_timeout)
        jitter_factor = 1 + random.uniform(-self.jitter, self.jitter)
        return raw * jitter_factor

    def call(self, fn, *args, **kwargs):
        if self._state == State.OPEN:
            elapsed = time.monotonic() - self._opened_at
            timeout = self._current_timeout()
            if elapsed >= timeout:
                self._state = State.HALF_OPEN
                print(f"[{self.name}] Probe attempt #{self._recovery_attempt + 1} (waited {elapsed:.1f}s)")
            else:
                raise RuntimeError(
                    f"[{self.name}] OPEN — retry in {timeout - elapsed:.1f}s"
                )

        try:
            result = fn(*args, **kwargs)
            self._success_history.append(True)
            if self._state == State.HALF_OPEN:
                self._state = State.CLOSED
                self._failures = 0
                self._recovery_attempt = 0
                print(f"[{self.name}] Recovered → CLOSED after {self._recovery_attempt} attempts")
            return result
        except Exception as e:
            self._success_history.append(False)
            self._failures += 1
            if self._state == State.HALF_OPEN:
                # Probe failed — back to OPEN with increased timeout
                self._recovery_attempt += 1
                self._state = State.OPEN
                self._opened_at = time.monotonic()
                print(f"[{self.name}] Probe failed — backing off to {self._current_timeout():.1f}s")
            elif self._failures >= self.threshold:
                self._state = State.OPEN
                self._opened_at = time.monotonic()
                self._recovery_attempt = 0
                print(f"[{self.name}] OPENED after {self._failures} failures")
            raise

    def status(self) -> dict:
        recent_failures = self._success_history[-10:].count(False) if self._success_history else 0
        return {
            "name": self.name,
            "state": self._state.value,
            "failures": self._failures,
            "recovery_attempt": self._recovery_attempt,
            "recent_errors_10": recent_failures,
        }


# Registry of per-dependency adaptive breakers
_breakers: dict[str, AdaptiveBreaker] = {
    "search_api":   AdaptiveBreaker("search_api",   threshold=2, base_timeout=5),
    "vector_store": AdaptiveBreaker("vector_store", threshold=3, base_timeout=10),
    "sql_db":       AdaptiveBreaker("sql_db",       threshold=3, base_timeout=5),
}

_fail_counts: dict[str, int] = {"search_api": 0, "vector_store": 0, "sql_db": 0}

def call_dep(name: str, query: str) -> str:
    _fail_counts[name] += 1
    # search_api fails on calls 2–5
    if name == "search_api" and 2 <= _fail_counts[name] <= 5:
        raise ConnectionError(f"{name} unavailable")
    return f"{name}: result for {query}"


def resilient_agent(query: str) -> str:
    client = anthropic.Anthropic()
    ctx = {}
    for dep in ["search_api", "vector_store", "sql_db"]:
        try:
            ctx[dep] = _breakers[dep].call(call_dep, dep, query)
        except RuntimeError as e:
            ctx[dep] = f"OPEN: {e}"
        except Exception as e:
            ctx[dep] = f"ERROR: {e}"

    context_str = "\n".join(f"{k}: {v}" for k, v in ctx.items())
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        system=context_str,
        messages=[{"role": "user", "content": query}],
    )
    return resp.content[0].text


if __name__ == "__main__":
    for i in range(8):
        result = resilient_agent(f"query_{i}")
        print(f"[{i}] {_breakers['search_api'].status()['state']:<10} {result[:40]}")

    print("\nFinal statuses:")
    for b in _breakers.values():
        print(f"  {b.status()}")

# Expected Token Savings: No extra tokens; exponential backoff prevents probe storms on recovering services
# Environment: Production agents where services need minutes to recover and aggressive retries worsen recovery
```

---

## Comparison

| Option | Trigger | Per-Dependency | Fallback | Recovery |
|--------|---------|---------------|----------|----------|
| 1 | Fixed failure count | Yes, registry-based | None (raises) | Fixed timeout |
| 2 | Fixed count + async | Yes, per-breaker fallback | Configurable default value | Fixed timeout |
| 3 | Rolling error rate window | Yes | None (raises) | Fixed timeout |
| 4 | Health tiers (healthy/degraded/unhealthy) | Yes, with dashboard | None (raises) | Consecutive successes |
| 5 | Count + bulkhead concurrency limit | Yes | None (raises) | Fixed timeout |
| 6 | Count + exponential backoff | Yes | None (raises) | Exponential backoff |
