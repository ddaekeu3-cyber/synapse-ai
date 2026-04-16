---
title: "Agent Doesn't Implement Circuit Breaker-Aware Caching"
description: "AI agents with circuit breakers that open on downstream failures return hard errors to users during the open state, even when a cached version of the response exists. Circuit breaker-aware caching integrates the two patterns: when the circuit opens, the agent automatically serves stale cached responses instead of failing, degrading gracefully rather than returning an error page."
date: 2025-02-22
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-circuit-breaker-aware-caching
tags:
  - circuit-breaker
  - caching
  - stale-cache
  - graceful-degradation
  - resilience
  - performance
  - fallback
symptoms:
  - "Agent returns 503 errors during downstream outages even though results from 5 minutes ago would be acceptable"
  - "Circuit breaker opens but there is no fallback — users see error instead of cached data"
  - "Cache and circuit breaker implemented separately — neither knows about the other"
  - "During a 10-minute API outage, all in-flight requests fail rather than serving the last known good response"
  - "Retry storms occur because circuit breaker opens but no cache prevents repeated attempts"
---

## Problem

Circuit breakers and caches are usually implemented independently: the circuit breaker tracks error rates and stops requests when a downstream is unhealthy; the cache stores successful responses for performance. When the circuit opens, the agent fails fast—but it has no access to recent cached results that would be acceptable substitutes. Integrating the two means: on cache miss with circuit closed, fetch from backend and populate cache; on circuit open, serve stale cache if available; on cache miss with circuit open, return a controlled degraded response. This turns a hard failure into a graceful degradation with measurable staleness.

---

## Solution 1: CircuitBreakerCache — Integrated CB + Cache

```python
import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


class CBState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class CacheEntry:
    value: Any
    stored_at: float
    ttl: float

    @property
    def expired(self) -> bool:
        return time.monotonic() > self.stored_at + self.ttl

    @property
    def age_seconds(self) -> float:
        return time.monotonic() - self.stored_at


class CircuitBreakerCache:
    """
    Combines a TTL cache with a circuit breaker. On circuit open, serves
    stale cache entries (even past their TTL) up to `stale_max_age` seconds
    before giving up. Transitions through CLOSED -> OPEN -> HALF_OPEN states.

    Usage:
        cbc = CircuitBreakerCache(
            fetch_fn=call_external_api,
            ttl=300,
            stale_max_age=1800,       # serve up to 30min stale on outage
            failure_threshold=5,
            recovery_timeout=60,
        )
        result = await cbc.get("search:ai-safety")
    """

    def __init__(
        self,
        fetch_fn: Callable,
        ttl: float = 300.0,
        stale_max_age: float = 1800.0,
        failure_threshold: int = 5,
        success_threshold: int = 2,
        recovery_timeout: float = 60.0,
    ):
        self._fetch = fetch_fn
        self._ttl = ttl
        self._stale_max = stale_max_age
        self._fail_thresh = failure_threshold
        self._success_thresh = success_threshold
        self._recovery_timeout = recovery_timeout

        self._cache: Dict[str, CacheEntry] = {}
        self._state = CBState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._last_failure: float = 0.0
        self._lock = asyncio.Lock()

        # Metrics
        self._hits = 0
        self._stale_hits = 0
        self._fetch_success = 0
        self._fetch_failure = 0
        self._open_rejections = 0

    def _transition(self, new_state: CBState):
        if new_state != self._state:
            logger.info("circuit_breaker_state old=%s new=%s", self._state.value, new_state.value)
            self._state = new_state

    def _record_success(self):
        self._failure_count = 0
        if self._state == CBState.HALF_OPEN:
            self._success_count += 1
            if self._success_count >= self._success_thresh:
                self._success_count = 0
                self._transition(CBState.CLOSED)

    def _record_failure(self):
        self._failure_count += 1
        self._last_failure = time.monotonic()
        if self._state == CBState.HALF_OPEN:
            self._transition(CBState.OPEN)
        elif self._failure_count >= self._fail_thresh:
            self._transition(CBState.OPEN)

    def _should_attempt(self) -> bool:
        if self._state == CBState.CLOSED:
            return True
        if self._state == CBState.OPEN:
            if time.monotonic() - self._last_failure >= self._recovery_timeout:
                self._transition(CBState.HALF_OPEN)
                return True
            return False
        return True  # HALF_OPEN: allow one probe

    def _get_cached(self, key: str) -> Tuple[Optional[CacheEntry], bool]:
        """Returns (entry, is_fresh). entry=None if not found or too stale."""
        entry = self._cache.get(key)
        if entry is None:
            return None, False
        if not entry.expired:
            return entry, True
        if entry.age_seconds <= self._stale_max:
            return entry, False  # stale but acceptable
        return None, False  # too old

    async def get(self, key: str, **fetch_kwargs) -> Optional[Any]:
        entry, is_fresh = self._get_cached(key)

        # Fresh cache hit — always serve regardless of CB state
        if is_fresh:
            self._hits += 1
            return entry.value

        if not self._should_attempt():
            # Circuit is OPEN — serve stale if available
            if entry is not None:
                self._stale_hits += 1
                logger.info("circuit_open_serving_stale key=%s age_s=%.0f",
                             key, entry.age_seconds)
                return entry.value
            self._open_rejections += 1
            logger.warning("circuit_open_no_cache key=%s", key)
            raise RuntimeError(f"Circuit open and no cached value for '{key}'")

        # Attempt backend fetch
        async with self._lock:
            # Double-check after acquiring lock
            entry, is_fresh = self._get_cached(key)
            if is_fresh:
                self._hits += 1
                return entry.value
            try:
                value = await self._fetch(key, **fetch_kwargs) \
                    if asyncio.iscoroutinefunction(self._fetch) \
                    else self._fetch(key, **fetch_kwargs)
                self._cache[key] = CacheEntry(
                    value=value, stored_at=time.monotonic(), ttl=self._ttl
                )
                self._fetch_success += 1
                self._record_success()
                return value
            except Exception as exc:
                self._fetch_failure += 1
                self._record_failure()
                logger.error("fetch_failed key=%s state=%s error=%s",
                              key, self._state.value, exc)
                # Serve stale on fetch failure
                entry, _ = self._get_cached(key)
                if entry:
                    self._stale_hits += 1
                    logger.info("serving_stale_on_failure key=%s age_s=%.0f",
                                 key, entry.age_seconds)
                    return entry.value
                raise

    def invalidate(self, key: str):
        self._cache.pop(key, None)

    @property
    def state(self) -> CBState:
        return self._state

    @property
    def stats(self) -> Dict[str, Any]:
        return {
            "state": self._state.value,
            "cache_size": len(self._cache),
            "hits": self._hits,
            "stale_hits": self._stale_hits,
            "fetch_success": self._fetch_success,
            "fetch_failure": self._fetch_failure,
            "open_rejections": self._open_rejections,
            "failure_count": self._failure_count,
        }
```

---

## Solution 2: StaleCachePolicy — Configurable Staleness Rules Per Key Pattern

```python
import fnmatch
import logging
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class StalePolicy:
    pattern: str          # glob pattern for cache keys (e.g. "search:*")
    max_stale_seconds: float
    serve_stale_on_error: bool = True
    serve_stale_on_open_circuit: bool = True
    log_staleness: bool = True


class StaleCachePolicy:
    """
    Defines per-key-pattern rules for how stale a cached response can be
    when served as a fallback. Different tool types have different staleness
    tolerances: search results can be hours old; stock prices need to be fresh.

    Usage:
        policy = StaleCachePolicy()
        policy.add("search:*",         max_stale_seconds=3600,  serve_stale_on_error=True)
        policy.add("weather:*",        max_stale_seconds=1800)
        policy.add("stock_price:*",    max_stale_seconds=30,    serve_stale_on_open_circuit=False)
        policy.add("user_profile:*",   max_stale_seconds=300)

        rule = policy.match("search:ai-agents")
        if rule and rule.serve_stale_on_open_circuit:
            return cached_entry.value
    """

    def __init__(self):
        self._rules: List[StalePolicy] = []
        # Default catch-all
        self._default = StalePolicy(
            pattern="*",
            max_stale_seconds=600.0,
            serve_stale_on_error=True,
            serve_stale_on_open_circuit=True,
        )

    def add(self, pattern: str, max_stale_seconds: float,
             serve_stale_on_error: bool = True,
             serve_stale_on_open_circuit: bool = True) -> "StaleCachePolicy":
        self._rules.append(StalePolicy(
            pattern=pattern,
            max_stale_seconds=max_stale_seconds,
            serve_stale_on_error=serve_stale_on_error,
            serve_stale_on_open_circuit=serve_stale_on_open_circuit,
        ))
        return self

    def match(self, key: str) -> StalePolicy:
        for rule in self._rules:
            if fnmatch.fnmatch(key, rule.pattern):
                return rule
        return self._default

    def is_stale_acceptable(self, key: str, age_seconds: float, reason: str) -> bool:
        rule = self.match(key)
        if age_seconds > rule.max_stale_seconds:
            logger.warning("stale_too_old key=%s age_s=%.0f max=%.0f",
                            key, age_seconds, rule.max_stale_seconds)
            return False
        if reason == "circuit_open" and not rule.serve_stale_on_open_circuit:
            return False
        if reason == "error" and not rule.serve_stale_on_error:
            return False
        if rule.log_staleness:
            logger.info("serving_stale key=%s age_s=%.0f reason=%s", key, age_seconds, reason)
        return True
```

---

## Solution 3: MultiLevelCBCache — L1 Memory + L2 Redis with Shared Circuit State

```python
import json
import logging
import time
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


class MultiLevelCBCache:
    """
    Two-level cache (L1: in-process dict, L2: Redis) combined with a
    shared circuit breaker state stored in Redis. When the circuit opens
    on one instance, all instances see it via the shared Redis CB key,
    preventing a thundering herd of probe requests during recovery.

    Usage:
        cache = MultiLevelCBCache(
            redis_client=redis,
            fetch_fn=call_search_api,
            l1_ttl=60,
            l2_ttl=300,
            stale_max_age=1800,
        )
        result = await cache.get("search:latest-ai-news")
    """

    CB_KEY = "agent:circuit_breaker:state"
    CB_FAILURE_KEY = "agent:circuit_breaker:failures"

    def __init__(
        self,
        redis_client: Any,
        fetch_fn: Callable,
        l1_ttl: float = 60.0,
        l2_ttl: float = 300.0,
        stale_max_age: float = 1800.0,
        failure_threshold: int = 5,
        recovery_timeout: float = 60.0,
    ):
        self._redis = redis_client
        self._fetch = fetch_fn
        self._l1_ttl = l1_ttl
        self._l2_ttl = l2_ttl
        self._stale_max = stale_max_age
        self._fail_thresh = failure_threshold
        self._recovery_timeout = recovery_timeout
        self._l1: dict = {}  # key -> (value, expiry)

    async def _l1_get(self, key: str) -> Optional[Any]:
        entry = self._l1.get(key)
        if entry and time.monotonic() < entry[1]:
            return entry[0]
        return None

    async def _l2_get(self, key: str) -> Optional[Any]:
        try:
            raw = await self._redis.get(f"cache:{key}")
            return json.loads(raw) if raw else None
        except Exception:
            return None

    async def _l2_set(self, key: str, value: Any):
        try:
            await self._redis.setex(f"cache:{key}", int(self._l2_ttl),
                                     json.dumps(value, default=str))
        except Exception as exc:
            logger.warning("l2_set_failed key=%s error=%s", key, exc)

    async def _is_circuit_open(self) -> bool:
        try:
            state = await self._redis.get(self.CB_KEY)
            return state == b"open"
        except Exception:
            return False  # assume closed if Redis unreachable

    async def _record_failure(self):
        try:
            failures = await self._redis.incr(self.CB_FAILURE_KEY)
            await self._redis.expire(self.CB_FAILURE_KEY, 60)
            if failures >= self._fail_thresh:
                await self._redis.setex(self.CB_KEY, int(self._recovery_timeout), "open")
                logger.warning("circuit_opened failures=%d", failures)
        except Exception:
            pass

    async def _record_success(self):
        try:
            await self._redis.delete(self.CB_FAILURE_KEY)
            await self._redis.delete(self.CB_KEY)
        except Exception:
            pass

    async def get(self, key: str, **fetch_kwargs) -> Optional[Any]:
        # L1 hit
        val = await self._l1_get(key)
        if val is not None:
            return val

        # L2 hit
        val = await self._l2_get(key)
        if val is not None:
            self._l1[key] = (val, time.monotonic() + self._l1_ttl)
            return val

        # Check circuit
        if await self._is_circuit_open():
            logger.info("circuit_open_cache_miss key=%s", key)
            return None

        # Fetch
        try:
            value = await self._fetch(key, **fetch_kwargs)
            self._l1[key] = (value, time.monotonic() + self._l1_ttl)
            await self._l2_set(key, value)
            await self._record_success()
            return value
        except Exception as exc:
            await self._record_failure()
            logger.error("fetch_failed key=%s error=%s", key, exc)
            raise
```

---

## Solution 4: CacheWarmOnCircuitClose — Pre-Warm Cache When Circuit Recovers

```python
import asyncio
import logging
import time
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


class CacheWarmOnCircuitClose:
    """
    When the circuit breaker transitions from OPEN to CLOSED, immediately
    pre-fetches the most recently accessed keys to replenish the cache before
    user traffic resumes. Prevents a flood of cache misses hitting a freshly
    recovered backend.

    Usage:
        warmer = CacheWarmOnCircuitClose(
            cbc=circuit_breaker_cache,
            warm_fn=fetch_fn,
            max_keys=50,
        )
        warmer.record_access("search:ai-safety")
        # When CB closes:
        await warmer.warm_on_recovery()
    """

    def __init__(
        self,
        cbc: Any,  # CircuitBreakerCache instance
        warm_fn: Callable,
        max_keys: int = 50,
        concurrency: int = 5,
    ):
        self._cbc = cbc
        self._warm_fn = warm_fn
        self._max_keys = max_keys
        self._sem = asyncio.Semaphore(concurrency)
        self._access_log: Dict[str, float] = {}
        self._prev_state = None

    def record_access(self, key: str):
        """Record cache key access for prioritized warming."""
        self._access_log[key] = time.monotonic()

    async def warm_on_recovery(self) -> int:
        """Warm the most recently accessed keys. Returns count warmed."""
        sorted_keys = sorted(
            self._access_log.items(), key=lambda x: x[1], reverse=True
        )[:self._max_keys]

        logger.info("cache_warm_start keys=%d", len(sorted_keys))
        warmed = 0

        async def _warm_one(key: str):
            nonlocal warmed
            async with self._sem:
                try:
                    value = await self._warm_fn(key)
                    self._cbc._cache[key] = CacheEntry(
                        value=value, stored_at=time.monotonic(), ttl=self._cbc._ttl
                    )
                    warmed += 1
                except Exception as exc:
                    logger.warning("warm_failed key=%s error=%s", key, exc)

        await asyncio.gather(*[_warm_one(key) for key, _ in sorted_keys])
        logger.info("cache_warm_complete warmed=%d", warmed)
        return warmed

    async def monitor_and_warm(self, poll_interval: float = 5.0):
        """Background task: warm cache whenever circuit closes."""
        while True:
            await asyncio.sleep(poll_interval)
            current_state = getattr(self._cbc, "state", None)
            if (self._prev_state == CBState.OPEN and
                    current_state in (CBState.CLOSED, CBState.HALF_OPEN)):
                logger.info("circuit_recovered triggering_cache_warm")
                await self.warm_on_recovery()
            self._prev_state = current_state
```

---

## Solution 5: CBCacheMetrics — Staleness and Degradation Tracking

```python
import logging
import time
from collections import deque
from typing import Any, Deque, Dict, Tuple

logger = logging.getLogger(__name__)


class CBCacheMetrics:
    """
    Tracks the quality of responses served during circuit-open periods:
    how old the stale responses were, how long the circuit stayed open,
    and what percentage of requests were served from stale cache vs.
    hard-failed during outages.

    Usage:
        metrics = CBCacheMetrics()
        metrics.record_stale_serve(key="search:q1", age_seconds=450)
        metrics.record_open_rejection(key="search:q2")
        print(metrics.summary())
    """

    def __init__(self, window: int = 1000):
        self._stale_ages: Deque[float] = deque(maxlen=window)
        self._stale_count = 0
        self._rejection_count = 0
        self._fresh_count = 0
        self._open_periods: list = []
        self._open_start: float = 0.0

    def record_stale_serve(self, key: str, age_seconds: float):
        self._stale_count += 1
        self._stale_ages.append(age_seconds)
        logger.debug("stale_served key=%s age_s=%.0f", key, age_seconds)

    def record_open_rejection(self, key: str):
        self._rejection_count += 1

    def record_fresh(self):
        self._fresh_count += 1

    def record_circuit_open(self):
        self._open_start = time.monotonic()

    def record_circuit_close(self):
        if self._open_start:
            duration = time.monotonic() - self._open_start
            self._open_periods.append(duration)
            logger.info("circuit_closed open_duration_s=%.1f", duration)
            self._open_start = 0.0

    def summary(self) -> Dict[str, Any]:
        total = self._fresh_count + self._stale_count + self._rejection_count
        ages = list(self._stale_ages)
        return {
            "total_requests": total,
            "fresh_pct": round(self._fresh_count / max(total, 1) * 100, 1),
            "stale_served_pct": round(self._stale_count / max(total, 1) * 100, 1),
            "hard_rejection_pct": round(self._rejection_count / max(total, 1) * 100, 1),
            "mean_stale_age_s": round(sum(ages) / len(ages), 1) if ages else 0,
            "max_stale_age_s": round(max(ages), 1) if ages else 0,
            "circuit_open_periods": len(self._open_periods),
            "mean_open_duration_s": (
                round(sum(self._open_periods) / len(self._open_periods), 1)
                if self._open_periods else 0
            ),
        }
```

---

## Solution 6: ToolCallCBCache — Drop-In Wrapper for Agent Tool Calls

```python
import asyncio
import logging
import time
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)


class ToolCallCBCache:
    """
    Drop-in wrapper that adds circuit-breaker-aware caching to any
    agent tool call function. Designed to be used as a decorator or
    wrapper at the tool dispatch layer.

    Usage:
        cb_cache = ToolCallCBCache(ttl=120, stale_max_age=600, failure_threshold=3)

        @cb_cache.wrap(tool_name="web_search")
        async def web_search(query: str) -> str:
            return await actual_web_search(query)
    """

    def __init__(
        self,
        ttl: float = 120.0,
        stale_max_age: float = 600.0,
        failure_threshold: int = 3,
        recovery_timeout: float = 30.0,
    ):
        import functools
        self._caches: Dict[str, CircuitBreakerCache] = {}
        self._ttl = ttl
        self._stale_max = stale_max_age
        self._fail_thresh = failure_threshold
        self._recovery = recovery_timeout

    def wrap(self, tool_name: str):
        def decorator(fn: Callable):
            async def fetch(key: str, **kwargs):
                return await fn(**kwargs) if asyncio.iscoroutinefunction(fn) else fn(**kwargs)

            cbc = CircuitBreakerCache(
                fetch_fn=fetch,
                ttl=self._ttl,
                stale_max_age=self._stale_max,
                failure_threshold=self._fail_thresh,
                recovery_timeout=self._recovery,
            )
            self._caches[tool_name] = cbc

            import functools
            @functools.wraps(fn)
            async def wrapper(**kwargs):
                cache_key = f"{tool_name}:{hash(str(sorted(kwargs.items())))}"
                return await cbc.get(cache_key, **kwargs)

            return wrapper
        return decorator

    def stats(self) -> Dict[str, Any]:
        return {name: cbc.stats for name, cbc in self._caches.items()}
```

---

## Comparison

| Approach | CB + Cache Integrated | Stale Serving | Per-Key Policy | Shared State | Cache Warming | Metrics |
|---|---|---|---|---|---|---|
| **CircuitBreakerCache** | Yes | Yes | No | No | No | Basic |
| **StaleCachePolicy** | No | Policy only | Yes | No | No | No |
| **MultiLevelCBCache** | Yes | L2 fallback | No | Redis | No | No |
| **CacheWarmOnCircuitClose** | No | No | No | No | Yes | No |
| **CBCacheMetrics** | No | No | No | No | No | Yes |
| **ToolCallCBCache** | Yes | Yes | No | No | No | Yes |

**Key insight**: the immediate integration is wrapping tool calls with `ToolCallCBCache`—it requires only a decorator addition and immediately enables stale-on-failure behavior. Set `stale_max_age` based on acceptable data freshness per tool: search results can tolerate 30 minutes stale, user profile lookups should tolerate 5 minutes, and real-time data like stock prices should set `stale_max_age=30`. Combine `StaleCachePolicy` for fine-grained per-key rules with `CBCacheMetrics` to measure what percentage of requests during your last outage were served from stale cache vs. hard-failed—this data makes the business case for increasing cache TTLs.
