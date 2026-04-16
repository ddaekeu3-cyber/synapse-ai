---
title: "Agent Doesn't Implement Result Deduplication for Concurrent Identical Requests"
description: "Agents handling concurrent sessions that receive identical queries within a short window each execute independent LLM and tool calls, paying full cost and latency N times for the same result. Implement request coalescing that detects in-flight identical requests, makes new arrivals wait on the existing future rather than starting a duplicate execution, and returns the shared result to all waiters simultaneously."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-result-deduplication-for-concurrent-identical-requests
tags: [request-coalescing, deduplication, concurrent-requests, single-flight, cache-stampede, fan-in]
symptoms:
  - "Trending topic query arrives from 500 users simultaneously — 500 identical LLM calls fire"
  - "Cache miss storm: cache expires, 200 concurrent sessions all miss and all execute"
  - "Tool call logs show identical arguments executing in parallel across sessions"
  - "No mechanism to detect that two in-flight requests will produce the same result"
  - "Cost spike correlates with popular queries — same query running hundreds of times"
---

## Why This Happens

Each agent session processes its request independently. When 200 sessions receive the same query within the same second (after a cache miss or during a viral event), they all start independent LLM calls. The first one to complete stores the result, but the other 199 complete anyway and discard their results — having paid full cost. Request coalescing (also called single-flight or request deduplication) holds duplicate in-flight requests at the gate and fans out the single result when the first execution completes.

## Solution 1: Request Fingerprint

```python
import hashlib
import json
from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass
class RequestFingerprint:
    key: str
    query_text: str
    model: str = ""
    tool_names: str = ""    # sorted comma-separated

    @classmethod
    def from_query(
        cls,
        query_text: str,
        model: str = "",
        tool_names: list = None,
    ) -> "RequestFingerprint":
        tools_str = ",".join(sorted(tool_names or []))
        payload = json.dumps({
            "q": query_text.strip().lower(),
            "m": model,
            "t": tools_str,
        }, sort_keys=True)
        key = hashlib.sha256(payload.encode()).hexdigest()[:16]
        return cls(
            key=key,
            query_text=query_text,
            model=model,
            tool_names=tools_str,
        )
```

## Solution 2: In-Flight Request Registry

```python
import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class InFlightRequest:
    fingerprint_key: str
    future: asyncio.Future
    started_at: float = field(default_factory=time.time)
    waiter_count: int = 1

    def age_seconds(self) -> float:
        return time.time() - self.started_at


class InFlightRequestRegistry:
    """
    Tracks in-progress requests by fingerprint key.
    New requests with matching keys attach to the existing future.
    """

    def __init__(self, max_wait_seconds: float = 30.0):
        self._inflight: Dict[str, InFlightRequest] = {}
        self._lock = asyncio.Lock()
        self._max_wait = max_wait_seconds

    async def join_or_create(
        self,
        key: str,
    ) -> tuple:
        """
        Returns (is_leader, future).
        is_leader=True: caller should execute the request and resolve future.
        is_leader=False: caller should await the future (it's a follower/waiter).
        """
        async with self._lock:
            if key in self._inflight:
                inflight = self._inflight[key]
                inflight.waiter_count += 1
                return False, inflight.future

            loop = asyncio.get_event_loop()
            future: asyncio.Future = loop.create_future()
            self._inflight[key] = InFlightRequest(
                fingerprint_key=key,
                future=future,
            )
            return True, future

    async def resolve(self, key: str, result: Any) -> None:
        async with self._lock:
            inflight = self._inflight.pop(key, None)
        if inflight and not inflight.future.done():
            inflight.future.set_result(result)

    async def reject(self, key: str, error: Exception) -> None:
        async with self._lock:
            inflight = self._inflight.pop(key, None)
        if inflight and not inflight.future.done():
            inflight.future.set_exception(error)

    def stats(self) -> dict:
        return {
            "inflight_count": len(self._inflight),
            "keys": list(self._inflight.keys()),
            "total_waiters": sum(r.waiter_count for r in self._inflight.values()),
        }
```

## Solution 3: Single-Flight Executor

```python
import asyncio
from typing import Any, Callable, Optional


class SingleFlightExecutor:
    """
    Implements the single-flight (request coalescing) pattern.
    Callers call execute() with a fingerprint key and an async factory.
    Only the first caller for a given key executes the factory;
    subsequent callers with the same key wait on the shared future.
    """

    def __init__(self, registry: InFlightRequestRegistry):
        self._registry = registry
        self._coalesced_count = 0
        self._executed_count = 0

    async def execute(
        self,
        key: str,
        factory: Callable,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        is_leader, future = await self._registry.join_or_create(key)

        if not is_leader:
            self._coalesced_count += 1
            return await asyncio.wait_for(
                asyncio.shield(future),
                timeout=self._registry._max_wait,
            )

        self._executed_count += 1
        try:
            result = await factory(*args, **kwargs)
            await self._registry.resolve(key, result)
            return result
        except Exception as exc:
            await self._registry.reject(key, exc)
            raise

    def stats(self) -> dict:
        total = self._executed_count + self._coalesced_count
        return {
            "executed": self._executed_count,
            "coalesced": self._coalesced_count,
            "coalesce_rate": round(
                self._coalesced_count / max(total, 1), 4
            ),
        }
```

## Solution 4: Coalescing Query Client

```python
from typing import Any, Callable, Dict, List, Optional


class CoalescingQueryClient:
    """
    Drop-in wrapper around an LLM or tool client that coalesces
    concurrent identical queries via SingleFlightExecutor.
    """

    def __init__(
        self,
        executor: SingleFlightExecutor,
        model: str = "",
    ):
        self._executor = executor
        self._model = model

    async def query(
        self,
        query_text: str,
        execute_fn: Callable,
        tool_names: Optional[List[str]] = None,
    ) -> Any:
        fingerprint = RequestFingerprint.from_query(
            query_text,
            model=self._model,
            tool_names=tool_names or [],
        )
        return await self._executor.execute(
            fingerprint.key,
            execute_fn,
            query_text,
        )

    def coalesce_stats(self) -> dict:
        return self._executor.stats()
```

## Solution 5: Coalescing Cache Integration

```python
import asyncio
from typing import Any, Callable, Dict, Optional


class CoalescingCacheLayer:
    """
    Combines a result cache with single-flight coalescing.
    Order of operations:
    1. Check cache — return immediately on hit.
    2. Check in-flight registry — join existing execution on match.
    3. Execute and populate cache — only if no cache hit and no in-flight match.
    This prevents both cache misses from spawning duplicate requests
    and stampedes when the cache entry expires.
    """

    def __init__(
        self,
        executor: SingleFlightExecutor,
        cache: Dict[str, Any],
        cache_ttl_seconds: float = 60.0,
    ):
        self._executor = executor
        self._cache = cache
        self._cache_ttl = cache_ttl_seconds
        self._cache_timestamps: Dict[str, float] = {}

    def _cache_get(self, key: str) -> Optional[Any]:
        import time
        ts = self._cache_timestamps.get(key, 0)
        if time.time() - ts > self._cache_ttl:
            return None
        return self._cache.get(key)

    def _cache_put(self, key: str, value: Any) -> None:
        import time
        self._cache[key] = value
        self._cache_timestamps[key] = time.time()

    async def get_or_execute(
        self,
        key: str,
        factory: Callable,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        cached = self._cache_get(key)
        if cached is not None:
            return cached

        result = await self._executor.execute(key, factory, *args, **kwargs)
        self._cache_put(key, result)
        return result
```

## Solution 6: Coalescing Metrics Dashboard

```python
import time


class CoalescingMetricsDashboard:
    """Combines single-flight stats and registry state into a dashboard."""

    def __init__(
        self,
        executor: SingleFlightExecutor,
        registry: InFlightRequestRegistry,
    ):
        self._executor = executor
        self._registry = registry

    def render(self) -> dict:
        exec_stats = self._executor.stats()
        reg_stats = self._registry.stats()
        return {
            "generated_at": time.time(),
            "coalesce_rate": exec_stats["coalesce_rate"],
            "total_executed": exec_stats["executed"],
            "total_coalesced": exec_stats["coalesced"],
            "currently_inflight": reg_stats["inflight_count"],
            "current_waiters": reg_stats["total_waiters"],
            "cost_savings_estimate_pct": round(exec_stats["coalesce_rate"] * 100, 1),
        }
```

## Comparison

| Approach | In-Flight Detection | Fan-Out | Cache Integration | Metrics | Gateway |
|---|---|---|---|---|---|
| InFlightRequestRegistry | Yes (by key) | Yes (shared future) | No | No | No |
| SingleFlightExecutor | Via registry | Via future | No | Yes | No |
| CoalescingQueryClient | Via executor | Via executor | No | Via executor | No |
| CoalescingCacheLayer | Via executor | Via executor | Yes | No | No |
| CoalescingMetricsDashboard | No | No | No | Yes | No |

**Best for production**: Use `RequestFingerprint.from_query()` to normalize the key — lowercase, strip whitespace, and sort tool names so minor formatting differences do not prevent coalescing. The coalesce rate is a direct cost-reduction metric: a 40% coalesce rate means 40% of LLM calls were served for free by reusing an in-flight result. Monitor `currently_inflight` — if it grows unboundedly, the `max_wait_seconds` timeout may be too long or the factory is hanging. Combine `CoalescingCacheLayer` with `SingleFlightExecutor` for maximum efficiency: cache hits require no coalescing overhead, and cache stampedes are prevented by the single-flight gate.
