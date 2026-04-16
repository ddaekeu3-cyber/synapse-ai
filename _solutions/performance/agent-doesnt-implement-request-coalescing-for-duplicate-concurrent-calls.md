---
title: "Agent Doesn't Implement Request Coalescing for Duplicate Concurrent Calls"
description: "Agents running multiple parallel tool calls may dispatch identical requests concurrently — the same search query from two different reasoning branches, the same database lookup triggered by two tool chains. Without coalescing, both requests are sent, doubling cost and latency. Implement request coalescing that detects in-flight duplicate requests by content hash and returns the same future to all callers, executing the underlying operation only once."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-request-coalescing-for-duplicate-concurrent-calls
tags: [request-coalescing, deduplication, concurrent-calls, in-flight-dedup, future-sharing, parallel-tool-calls]
symptoms:
  - "Same search query sent 3 times simultaneously from parallel reasoning branches"
  - "Database lookup executed twice for the same key within the same agent turn"
  - "API cost doubles during parallel tool execution with overlapping queries"
  - "No mechanism to detect that two concurrent callers want the same result"
  - "Cache misses trigger duplicate backend calls before the first call returns"
---

## Why This Happens

Caches prevent duplicate calls across turns, but within a single turn multiple coroutines may be dispatched simultaneously and all miss the cache at the same instant — before any result has been written back. This is the cache stampede problem applied to tool calls. Coalescing differs from caching: it does not store past results; it deduplicates in-flight requests by sharing a single asyncio Future across all concurrent callers with the same request key. When the first call completes, all waiters receive the result simultaneously.

## Solution 1: Request Coalescer

```python
import asyncio
import hashlib
import json
import time
from typing import Any, Callable, Dict, Optional, Tuple


class CoalescedRequest:
    """Represents a single in-flight request shared by multiple callers."""

    def __init__(self, key: str):
        self.key = key
        self.future: asyncio.Future = asyncio.get_event_loop().create_future()
        self.waiter_count: int = 1
        self.started_at: float = time.time()

    def age_ms(self) -> float:
        return round((time.time() - self.started_at) * 1000, 2)


class RequestCoalescer:
    """
    Deduplicates concurrent calls with the same request key.
    The first caller executes the operation; subsequent concurrent
    callers with the same key await the same Future.
    """

    def __init__(self, key_ttl_seconds: float = 30.0):
        self._in_flight: Dict[str, CoalescedRequest] = {}
        self._ttl = key_ttl_seconds
        self._coalesced_count = 0
        self._total_count = 0

    @staticmethod
    def make_key(tool_name: str, args: Any) -> str:
        try:
            canonical = json.dumps({"tool": tool_name, "args": args}, sort_keys=True)
        except (TypeError, ValueError):
            canonical = f"{tool_name}:{str(args)}"
        return hashlib.sha256(canonical.encode()).hexdigest()[:16]

    async def execute(
        self,
        key: str,
        fn: Callable[[], Any],
    ) -> Any:
        self._total_count += 1

        # Check for stale in-flight entries (safety valve)
        stale = [k for k, r in self._in_flight.items() if r.age_ms() > self._ttl * 1000]
        for k in stale:
            self._in_flight.pop(k, None)

        if key in self._in_flight:
            req = self._in_flight[key]
            req.waiter_count += 1
            self._coalesced_count += 1
            return await asyncio.shield(req.future)

        req = CoalescedRequest(key)
        self._in_flight[key] = req

        try:
            result = await fn()
            req.future.set_result(result)
            return result
        except Exception as exc:
            req.future.set_exception(exc)
            raise
        finally:
            self._in_flight.pop(key, None)

    def stats(self) -> dict:
        return {
            "total_calls": self._total_count,
            "coalesced_calls": self._coalesced_count,
            "in_flight": len(self._in_flight),
            "coalesce_rate": round(
                self._coalesced_count / self._total_count if self._total_count else 0.0, 4
            ),
        }
```

## Solution 2: Coalescing Tool Dispatcher

```python
import asyncio
from typing import Any, Callable, Dict, Optional


class CoalescingToolDispatcher:
    """
    Wraps tool calls with request coalescing.
    Each tool can opt in to coalescing via the registry.
    Non-coalesced tools are dispatched directly.
    """

    def __init__(self, coalescer: RequestCoalescer):
        self._coalescer = coalescer
        self._coalesced_tools: set = set()

    def enable_coalescing(self, tool_name: str) -> None:
        self._coalesced_tools.add(tool_name)

    def disable_coalescing(self, tool_name: str) -> None:
        self._coalesced_tools.discard(tool_name)

    async def dispatch(
        self,
        tool_name: str,
        args: Any,
        fn: Callable[[], Any],
    ) -> Any:
        if tool_name not in self._coalesced_tools:
            return await fn()

        key = RequestCoalescer.make_key(tool_name, args)
        return await self._coalescer.execute(key, fn)
```

## Solution 3: Coalesce Window Manager

```python
import asyncio
import time
from typing import Any, Callable, Dict, List, Optional, Tuple


class CoalesceWindowManager:
    """
    Batches requests that arrive within a short window (e.g., 5ms)
    before dispatching. Useful when tool calls are fire-and-forget
    from multiple branches and arrive in a burst within a single event loop tick.
    """

    def __init__(
        self,
        window_ms: float = 5.0,
        max_batch_size: int = 50,
    ):
        self._window_ms = window_ms
        self._max_batch = max_batch_size
        self._pending: Dict[str, List[asyncio.Future]] = {}
        self._dispatch_tasks: Dict[str, asyncio.Task] = {}

    async def submit(
        self,
        key: str,
        fn: Callable[[], Any],
    ) -> Any:
        loop = asyncio.get_event_loop()
        future: asyncio.Future = loop.create_future()

        if key not in self._pending:
            self._pending[key] = []
            self._dispatch_tasks[key] = asyncio.create_task(
                self._dispatch_after_window(key, fn)
            )

        self._pending[key].append(future)
        return await future

    async def _dispatch_after_window(self, key: str, fn: Callable[[], Any]) -> None:
        await asyncio.sleep(self._window_ms / 1000.0)
        waiters = self._pending.pop(key, [])
        self._dispatch_tasks.pop(key, None)

        if not waiters:
            return

        try:
            result = await fn()
            for f in waiters:
                if not f.done():
                    f.set_result(result)
        except Exception as exc:
            for f in waiters:
                if not f.done():
                    f.set_exception(exc)
```

## Solution 4: Coalescing Savings Estimator

```python
from typing import List


class CoalescingSavingsEstimator:
    """
    Estimates the latency and cost savings from coalescing
    based on coalesced call counts and average tool call cost.
    """

    def __init__(
        self,
        avg_tool_latency_ms: float = 200.0,
        avg_tool_cost_usd: float = 0.001,
    ):
        self._avg_latency = avg_tool_latency_ms
        self._avg_cost = avg_tool_cost_usd

    def estimate(self, stats: dict) -> dict:
        coalesced = stats.get("coalesced_calls", 0)
        saved_latency_ms = coalesced * self._avg_latency
        saved_cost_usd = coalesced * self._avg_cost
        return {
            "coalesced_calls": coalesced,
            "estimated_latency_saved_ms": round(saved_latency_ms, 1),
            "estimated_cost_saved_usd": round(saved_cost_usd, 6),
            "coalesce_rate": stats.get("coalesce_rate", 0.0),
        }
```

## Solution 5: Per-Tool Coalesce Monitor

```python
import time
from collections import defaultdict
from typing import Dict


class PerToolCoalesceMonitor:
    """
    Tracks coalescing effectiveness per tool name.
    Identifies which tools benefit most from coalescing.
    """

    def __init__(self):
        self._executed: Dict[str, int] = defaultdict(int)
        self._coalesced: Dict[str, int] = defaultdict(int)

    def record(self, tool_name: str, was_coalesced: bool) -> None:
        self._executed[tool_name] += 1
        if was_coalesced:
            self._coalesced[tool_name] += 1

    def report(self) -> list:
        tools = set(self._executed) | set(self._coalesced)
        rows = []
        for tool in tools:
            executed = self._executed[tool]
            coalesced = self._coalesced[tool]
            rows.append({
                "tool_name": tool,
                "total_calls": executed,
                "coalesced_calls": coalesced,
                "coalesce_rate": round(coalesced / executed if executed else 0.0, 4),
            })
        return sorted(rows, key=lambda r: -r["coalesced_calls"])
```

## Solution 6: Coalescing Dashboard

```python
import time
from typing import Optional


class RequestCoalescingDashboard:
    """
    Combines coalescer stats, per-tool breakdown, and savings
    estimates into a single operational snapshot.
    """

    def __init__(
        self,
        coalescer: RequestCoalescer,
        per_tool_monitor: PerToolCoalesceMonitor,
        savings_estimator: CoalescingSavingsEstimator,
    ):
        self._coalescer = coalescer
        self._monitor = per_tool_monitor
        self._estimator = savings_estimator

    def render(self) -> dict:
        stats = self._coalescer.stats()
        return {
            "generated_at": time.time(),
            "global_stats": stats,
            "savings": self._estimator.estimate(stats),
            "per_tool": self._monitor.report()[:10],
        }
```

## Comparison

| Approach | In-Flight Dedup | Window Batching | Per-Tool Control | Savings Estimate | Dashboard |
|---|---|---|---|---|---|
| RequestCoalescer | Yes (Future share) | No | No | Via stats | No |
| CoalescingToolDispatcher | Via coalescer | No | Yes (opt-in) | No | No |
| CoalesceWindowManager | Via key | Yes (5ms window) | No | No | No |
| CoalescingSavingsEstimator | No | No | No | Yes | No |
| PerToolCoalesceMonitor | No | No | Yes | No | No |
| RequestCoalescingDashboard | No | No | No | No | Yes |

**Best for production**: Enable coalescing for read-only, idempotent tool calls (search, lookup, fetch) and disable it for write operations or tools that return time-sensitive data. Use `asyncio.shield()` in the coalescer so cancelling one waiter does not cancel the underlying request for other waiters. Set `key_ttl_seconds=30` as a safety valve to prevent in-flight entries from accumulating if the future resolution is delayed indefinitely. Monitor `coalesce_rate` per tool — a rate above 0.20 means the same query is being sent frequently in parallel and the tool call strategy should be reviewed to eliminate redundant dispatches at the planner level.
