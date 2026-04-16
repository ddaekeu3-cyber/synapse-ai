---
title: "Agent Doesn't Implement Request Coalescing for Identical Concurrent Calls"
description: "Agents serving concurrent users issue duplicate upstream requests when multiple sessions ask the same question simultaneously: ten users asking 'what is the weather?' each trigger a separate weather API call at the same moment. Implement request coalescing that deduplicates in-flight calls with identical signatures, so the first caller fetches the result and all concurrent callers share it."
date: 2026-04-16
difficulty: advanced
category: reliability
slug: agent-doesnt-implement-request-coalescing-for-identical-concurrent-calls
tags: [request-coalescing, in-flight-deduplication, concurrent-calls, upstream-protection, thundering-herd, shared-result]
symptoms:
  - "Ten simultaneous identical queries each trigger a separate API call to the same endpoint"
  - "Upstream service rate limits fire during traffic spikes despite low unique query volume"
  - "No in-flight tracking to detect that an identical request is already being processed"
  - "Cache misses under high concurrency cause multiple cache-filling fetches for the same key"
  - "Cost spikes when many users ask identical questions at popular times"
---

## Why This Happens

When a cache miss occurs and multiple concurrent requests share the same cache key, each proceeds to fetch the result independently — the classic thundering herd at the cache layer. Even with a populated cache, the window between the first cache miss and the cache fill allows all subsequent identical requests to also miss and launch their own fetches. Coalescing requires tracking in-flight requests by their call signature, suspending subsequent identical callers on an asyncio.Event or Future, and broadcasting the single fetched result to all waiters once the first fetch completes.

## Solution 1: Call Signature Hasher

```python
import hashlib
import json
from typing import Any, Dict, Optional


class CallSignatureHasher:
    """
    Produces a stable hash representing the identity of a call:
    (tool_name, arguments). Two calls with the same name and arguments
    produce the same signature regardless of argument dict ordering.
    """

    def hash(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        namespace: str = "",
    ) -> str:
        payload = {
            "ns": namespace,
            "tool": tool_name,
            "args": arguments,
        }
        serialized = json.dumps(payload, sort_keys=True, default=str)
        return hashlib.sha256(serialized.encode()).hexdigest()[:24]
```

## Solution 2: In-Flight Request Registry

```python
import asyncio
import time
from typing import Any, Dict, Optional


class InFlightRequest:
    def __init__(self):
        self.event = asyncio.Event()
        self.result: Optional[Any] = None
        self.error: Optional[Exception] = None
        self.waiters: int = 0
        self.started_at: float = time.time()

    def set_result(self, result: Any) -> None:
        self.result = result
        self.event.set()

    def set_error(self, error: Exception) -> None:
        self.error = error
        self.event.set()

    async def wait(self) -> Any:
        self.waiters += 1
        await self.event.wait()
        if self.error:
            raise self.error
        return self.result


class InFlightRequestRegistry:
    """
    Tracks currently executing requests by their call signature.
    New callers with the same signature join the existing in-flight
    request rather than launching a new fetch.
    """

    def __init__(self):
        self._in_flight: Dict[str, InFlightRequest] = {}
        self._lock = asyncio.Lock()
        self._coalesced_count = 0
        self._total_executions = 0

    async def get_or_create(
        self, signature: str
    ) -> tuple:
        """
        Returns (in_flight_request, is_leader).
        is_leader=True means this caller should execute the fetch.
        is_leader=False means this caller should wait on the event.
        """
        async with self._lock:
            if signature in self._in_flight:
                self._coalesced_count += 1
                return self._in_flight[signature], False
            req = InFlightRequest()
            self._in_flight[signature] = req
            self._total_executions += 1
            return req, True

    async def complete(self, signature: str) -> None:
        async with self._lock:
            self._in_flight.pop(signature, None)

    def stats(self) -> dict:
        total = self._total_executions + self._coalesced_count
        return {
            "total_calls": total,
            "actual_executions": self._total_executions,
            "coalesced_calls": self._coalesced_count,
            "coalescing_rate": round(
                self._coalesced_count / max(total, 1), 4
            ),
            "currently_in_flight": len(self._in_flight),
        }
```

## Solution 3: Coalescing Tool Dispatcher

```python
import asyncio
from typing import Any, Callable, Dict


class CoalescingToolDispatcher:
    """
    Dispatches tool calls with in-flight coalescing. When multiple
    concurrent callers request the same (tool, arguments), only the
    first executes; the rest wait and share the result.
    """

    def __init__(
        self,
        hasher: CallSignatureHasher,
        registry: InFlightRequestRegistry,
        namespace: str = "",
    ):
        self._hasher = hasher
        self._registry = registry
        self._namespace = namespace

    async def dispatch(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        execute_fn: Callable[[str, Dict[str, Any]], Any],
    ) -> dict:
        signature = self._hasher.hash(tool_name, arguments, self._namespace)
        req, is_leader = await self._registry.get_or_create(signature)

        if is_leader:
            try:
                result = await execute_fn(tool_name, arguments)
                req.set_result(result)
                return {
                    "result": result,
                    "coalesced": False,
                    "signature": signature,
                    "waiters_served": req.waiters,
                }
            except Exception as exc:
                req.set_error(exc)
                raise
            finally:
                await self._registry.complete(signature)
        else:
            result = await req.wait()
            return {
                "result": result,
                "coalesced": True,
                "signature": signature,
            }
```

## Solution 4: TTL-Backed Coalescing Cache

```python
import asyncio
import time
from typing import Any, Callable, Dict, Optional


class TTLBackedCoalescingCache:
    """
    Combines in-flight coalescing with a short TTL result cache.
    Callers who arrive after the first fetch completes receive the
    cached result immediately without waiting.
    """

    def __init__(
        self,
        dispatcher: CoalescingToolDispatcher,
        ttl_seconds: float = 5.0,
        max_entries: int = 1000,
    ):
        self._dispatcher = dispatcher
        self._ttl = ttl_seconds
        self._max = max_entries
        self._cache: Dict[str, dict] = {}
        self._lock = asyncio.Lock()
        self._cache_hits = 0

    async def get(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        execute_fn: Callable,
    ) -> dict:
        hasher = self._dispatcher._hasher
        sig = hasher.hash(tool_name, arguments, self._dispatcher._namespace)

        async with self._lock:
            entry = self._cache.get(sig)
            if entry and time.time() < entry["expires_at"]:
                self._cache_hits += 1
                return {"result": entry["result"], "coalesced": True, "cached": True}

        dispatch_result = await self._dispatcher.dispatch(tool_name, arguments, execute_fn)

        async with self._lock:
            if len(self._cache) >= self._max:
                oldest = min(self._cache, key=lambda k: self._cache[k]["expires_at"])
                del self._cache[oldest]
            self._cache[sig] = {
                "result": dispatch_result["result"],
                "expires_at": time.time() + self._ttl,
            }

        return {**dispatch_result, "cached": False}

    def cache_hit_count(self) -> int:
        return self._cache_hits
```

## Solution 5: Coalescing Metrics Collector

```python
import time
from typing import List


class CoalescingMetricsCollector:
    """
    Accumulates per-interval coalescing statistics and surfaces
    upstream call reduction ratios over time.
    """

    def __init__(self, sample_interval_seconds: float = 60.0):
        self._interval = sample_interval_seconds
        self._samples: List[dict] = []
        self._last_sample_time = time.time()
        self._last_stats: dict = {}

    def snapshot(self, registry: InFlightRequestRegistry, cache: TTLBackedCoalescingCache) -> None:
        now = time.time()
        current = registry.stats()
        self._samples.append({
            "ts": now,
            **current,
            "cache_hits": cache.cache_hit_count(),
        })
        if len(self._samples) > 1440:  # 24h at 1-min intervals
            self._samples.pop(0)

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [s for s in self._samples if s["ts"] >= cutoff]
        if not recent:
            return {"window_seconds": window_seconds, "samples": 0}
        total_calls = sum(s.get("total_calls", 0) for s in recent)
        total_executions = sum(s.get("actual_executions", 0) for s in recent)
        total_coalesced = sum(s.get("coalesced_calls", 0) for s in recent)
        return {
            "window_seconds": window_seconds,
            "samples": len(recent),
            "total_calls": total_calls,
            "actual_executions": total_executions,
            "coalesced_calls": total_coalesced,
            "upstream_reduction_pct": round(
                total_coalesced / max(total_calls, 1) * 100, 1
            ),
        }
```

## Solution 6: Coalescing Dashboard

```python
import time


class RequestCoalescingDashboard:
    """
    Combines in-flight registry state, cache performance, and
    aggregate coalescing metrics into a single operational view.
    """

    def __init__(
        self,
        registry: InFlightRequestRegistry,
        cache: TTLBackedCoalescingCache,
        metrics: CoalescingMetricsCollector,
    ):
        self._registry = registry
        self._cache = cache
        self._metrics = metrics

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "in_flight": self._registry.stats(),
            "cache_hits": self._cache.cache_hit_count(),
            "hourly_summary": self._metrics.summary(3600.0),
        }
```

## Comparison

| Approach | In-Flight Tracking | Result Sharing | TTL Caching | Metrics | Dashboard |
|---|---|---|---|---|---|
| InFlightRequestRegistry | Yes (asyncio.Event) | Yes | No | Yes | No |
| CoalescingToolDispatcher | Via registry | Yes | No | Via registry | No |
| TTLBackedCoalescingCache | Via dispatcher | Yes | Yes (TTL) | Partial | No |
| CoalescingMetricsCollector | No | No | No | Yes | No |
| RequestCoalescingDashboard | No | No | No | Via collector | Yes |

**Best for production**: Apply coalescing only to idempotent read tools — never coalesce write tools, as sharing a write result between callers means some callers believe a write happened when only one actually did. Set `ttl_seconds=5` as a default for read tools; this covers the typical burst window of concurrent identical queries without serving stale data. Monitor `upstream_reduction_pct` from `CoalescingMetricsCollector`: values consistently above 30% indicate a hot query pattern that should be moved to a longer-lived shared cache rather than relying on coalescing alone.
