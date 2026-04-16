---
title: "Agent Doesn't Implement Tool Call Deduplication for Concurrent Requests"
description: "Agents that process concurrent requests without deduplication fire identical tool calls multiple times in parallel — the same web search, database query, or API call executes N times when N simultaneous requests need the same data. Implement in-flight deduplication that collapses concurrent identical calls into a single execution and fans the result out to all waiters."
date: 2026-04-16
difficulty: advanced
category: reliability
slug: agent-doesnt-implement-tool-call-deduplication-for-concurrent-requests
tags: [deduplication, concurrent-requests, request-coalescing, in-flight-dedup, thundering-herd, tool-efficiency]
symptoms:
  - "Same API endpoint called 10× simultaneously when 10 requests need the same data"
  - "Rate limit errors spike during traffic bursts despite low per-request call counts"
  - "Downstream tools receive identical concurrent requests with identical arguments"
  - "Cost doubles or triples when load spikes even though unique data requests stay flat"
  - "No mechanism to recognize that two in-flight tool calls are semantically identical"
---

## Why This Happens

When an agent handles multiple simultaneous user requests that require the same tool result — a shared knowledge base lookup, a live exchange rate, a weather API call — each request independently dispatches the tool call. Without deduplication, ten concurrent requests trigger ten identical API calls at the same millisecond. The downstream service sees a burst, rate limits fire, and retries cascade. In-flight deduplication solves this by registering the first call for a given cache key as the authoritative execution; subsequent identical calls join a waiter list and receive the result when the first call completes, without making additional network calls.

## Solution 1: In-Flight Call Registry

```python
import asyncio
import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class InFlightEntry:
    key: str
    tool_name: str
    args_hash: str
    started_at: float
    future: asyncio.Future
    waiter_count: int = 0
    completed: bool = False
    result: Any = None
    error: Optional[Exception] = None


class InFlightCallRegistry:
    """
    Tracks tool calls that are currently executing.
    New calls with the same key join the existing future rather than
    launching a new execution.
    """

    def __init__(self):
        self._entries: Dict[str, InFlightEntry] = {}
        self._lock = asyncio.Lock()

    async def get_or_create(
        self,
        tool_name: str,
        args: Dict[str, Any],
    ) -> tuple[str, bool, InFlightEntry]:
        """
        Returns (key, is_new, entry).
        is_new=True means the caller must execute the tool.
        is_new=False means the caller should await entry.future.
        """
        key = self._make_key(tool_name, args)
        async with self._lock:
            if key in self._entries:
                entry = self._entries[key]
                entry.waiter_count += 1
                return key, False, entry
            loop = asyncio.get_event_loop()
            future: asyncio.Future = loop.create_future()
            entry = InFlightEntry(
                key=key,
                tool_name=tool_name,
                args_hash=key,
                started_at=time.time(),
                future=future,
            )
            self._entries[key] = entry
            return key, True, entry

    async def resolve(self, key: str, result: Any) -> None:
        async with self._lock:
            entry = self._entries.pop(key, None)
        if entry and not entry.future.done():
            entry.future.set_result(result)

    async def reject(self, key: str, error: Exception) -> None:
        async with self._lock:
            entry = self._entries.pop(key, None)
        if entry and not entry.future.done():
            entry.future.set_exception(error)

    @staticmethod
    def _make_key(tool_name: str, args: Dict[str, Any]) -> str:
        payload = json.dumps({"tool": tool_name, "args": args}, sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()[:16]

    def active_count(self) -> int:
        return len(self._entries)
```

## Solution 2: Deduplicating Tool Executor

```python
import asyncio
from typing import Any, Callable, Dict


class DeduplicatingToolExecutor:
    """
    Wraps a tool call function with in-flight deduplication.
    Concurrent calls with identical (tool_name, args) share one execution.
    """

    def __init__(self, registry: InFlightCallRegistry):
        self._registry = registry
        self._dedup_hits = 0
        self._dedup_misses = 0

    async def execute(
        self,
        tool_name: str,
        args: Dict[str, Any],
        tool_fn: Callable,
    ) -> Any:
        key, is_new, entry = await self._registry.get_or_create(tool_name, args)

        if not is_new:
            self._dedup_hits += 1
            return await asyncio.shield(entry.future)

        self._dedup_misses += 1
        try:
            result = await tool_fn(tool_name, args)
            await self._registry.resolve(key, result)
            return result
        except Exception as exc:
            await self._registry.reject(key, exc)
            raise

    def stats(self) -> dict:
        total = self._dedup_hits + self._dedup_misses
        return {
            "dedup_hits": self._dedup_hits,
            "dedup_misses": self._dedup_misses,
            "hit_rate": round(self._dedup_hits / max(total, 1), 4),
        }
```

## Solution 3: Keyed Deduplication Policy

```python
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set


@dataclass
class DeduplicationPolicy:
    """
    Controls which tool calls are eligible for deduplication and
    which argument fields are excluded from the deduplication key
    (e.g., request IDs, timestamps that make every call unique).
    """
    deduplicate_tools: Set[str] = field(default_factory=set)
    skip_tools: Set[str] = field(default_factory=set)
    exclude_arg_fields: Dict[str, List[str]] = field(default_factory=dict)
    # tool_name -> list of arg keys to strip before hashing
    max_dedup_window_seconds: float = 5.0

    def is_eligible(self, tool_name: str) -> bool:
        if tool_name in self.skip_tools:
            return False
        if self.deduplicate_tools:
            return tool_name in self.deduplicate_tools
        return True  # deduplicate everything by default

    def normalized_args(self, tool_name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        excluded = self.exclude_arg_fields.get(tool_name, [])
        return {k: v for k, v in args.items() if k not in excluded}
```

## Solution 4: Concurrent Load Simulator (for testing)

```python
import asyncio
import time
from typing import Any, Callable, Dict, List


class ConcurrentCallStats:
    def __init__(self):
        self.actual_executions = 0
        self.total_requests = 0
        self.latencies_ms: List[float] = []

    def record(self, start: float, executed: bool) -> None:
        self.total_requests += 1
        if executed:
            self.actual_executions += 1
        self.latencies_ms.append(round((time.time() - start) * 1000, 2))

    def summary(self) -> dict:
        return {
            "total_requests": self.total_requests,
            "actual_executions": self.actual_executions,
            "dedup_ratio": round(
                1 - self.actual_executions / max(self.total_requests, 1), 4
            ),
            "mean_latency_ms": round(
                sum(self.latencies_ms) / max(len(self.latencies_ms), 1), 2
            ),
        }


class ConcurrentDeduplicationTester:
    """
    Fires N concurrent identical tool calls through the deduplicating executor
    and reports how many actual executions occurred.
    """

    def __init__(self, executor: DeduplicatingToolExecutor):
        self._executor = executor

    async def run(
        self,
        tool_name: str,
        args: Dict[str, Any],
        tool_fn: Callable,
        concurrency: int = 20,
    ) -> ConcurrentCallStats:
        stats = ConcurrentCallStats()

        async def single_call() -> Any:
            start = time.time()
            result = await self._executor.execute(tool_name, args, tool_fn)
            stats.record(start, executed=False)
            return result

        await asyncio.gather(*[single_call() for _ in range(concurrency)])
        return stats
```

## Solution 5: Policy-Aware Deduplication Dispatcher

```python
import asyncio
from typing import Any, Callable, Dict


class PolicyAwareDeduplicationDispatcher:
    """
    Combines DeduplicationPolicy with DeduplicatingToolExecutor.
    Skips deduplication for tools that mutate state (POST/DELETE tools).
    Strips non-deterministic arg fields before key generation.
    """

    def __init__(
        self,
        executor: DeduplicatingToolExecutor,
        policy: DeduplicationPolicy,
    ):
        self._executor = executor
        self._policy = policy
        self._bypassed = 0

    async def dispatch(
        self,
        tool_name: str,
        args: Dict[str, Any],
        tool_fn: Callable,
    ) -> Any:
        if not self._policy.is_eligible(tool_name):
            self._bypassed += 1
            return await tool_fn(tool_name, args)

        normalized = self._policy.normalized_args(tool_name, args)
        return await self._executor.execute(tool_name, normalized, tool_fn)

    def stats(self) -> dict:
        return {
            **self._executor.stats(),
            "policy_bypassed": self._bypassed,
        }
```

## Solution 6: Deduplication Metrics Dashboard

```python
import time
from collections import deque
from typing import Deque, Tuple


class DeduplicationMetricsDashboard:
    """
    Tracks deduplication effectiveness over time.
    Surfaces savings in downstream API calls and estimated cost reduction.
    """

    def __init__(
        self,
        dispatcher: PolicyAwareDeduplicationDispatcher,
        registry: InFlightCallRegistry,
        cost_per_call: float = 0.001,   # USD per tool call
    ):
        self._dispatcher = dispatcher
        self._registry = registry
        self._cost_per_call = cost_per_call
        self._history: Deque[Tuple[float, dict]] = deque(maxlen=1000)

    def snapshot(self) -> dict:
        stats = self._dispatcher.stats()
        saved_calls = stats["dedup_hits"]
        data = {
            "generated_at": time.time(),
            "active_in_flight": self._registry.active_count(),
            **stats,
            "estimated_calls_saved": saved_calls,
            "estimated_cost_saved_usd": round(saved_calls * self._cost_per_call, 4),
        }
        self._history.append((time.time(), data))
        return data

    def trend(self, window_seconds: float = 300.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [d for ts, d in self._history if ts >= cutoff]
        if not recent:
            return {"window_seconds": window_seconds, "snapshots": 0}
        total_hits = sum(d["dedup_hits"] for d in recent)
        total_misses = sum(d["dedup_misses"] for d in recent)
        return {
            "window_seconds": window_seconds,
            "snapshots": len(recent),
            "total_dedup_hits": total_hits,
            "total_executions": total_misses,
            "avg_hit_rate": round(total_hits / max(total_hits + total_misses, 1), 4),
        }
```

## Comparison

| Approach | In-Flight Tracking | Result Fan-Out | Policy Filtering | Arg Normalization | Metrics |
|---|---|---|---|---|---|
| InFlightCallRegistry | Yes (asyncio.Future) | Yes (shared future) | No | No | No |
| DeduplicatingToolExecutor | Via registry | Via registry | No | No | Yes (hit rate) |
| DeduplicationPolicy | No | No | Yes (allow/skip list) | Yes (field exclusion) | No |
| PolicyAwareDeduplicationDispatcher | Via executor | Via executor | Yes | Yes | Yes |
| DeduplicationMetricsDashboard | No | No | No | No | Yes (cost savings) |

**Best for production**: Apply deduplication only to read-only (idempotent) tool calls — never to tools that write, mutate, or have side effects. Use `DeduplicationPolicy.skip_tools` to explicitly exclude mutation tools. Strip request-ID and timestamp fields from the deduplication key via `exclude_arg_fields` — these fields make every call unique even when the payload is semantically identical. Monitor `hit_rate` via `DeduplicationMetricsDashboard`: a hit rate above 30% during peak load means the downstream service was receiving 30% redundant calls. Use `asyncio.shield` on the shared future so that one waiter cancelling does not cancel the executing call.
