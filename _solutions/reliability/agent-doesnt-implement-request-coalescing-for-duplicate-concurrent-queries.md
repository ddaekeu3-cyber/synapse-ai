---
title: "Agent Doesn't Implement Request Coalescing for Duplicate Concurrent Queries"
description: "Agents serving concurrent users send duplicate LLM or tool requests when multiple sessions ask the same question at the same time — each session pays full latency and token cost independently. Implement request coalescing that detects in-flight duplicate requests by query fingerprint, attaches late arrivals to the in-progress call, and delivers the single result to all waiters simultaneously."
date: 2026-04-16
difficulty: advanced
category: reliability
slug: agent-doesnt-implement-request-coalescing-for-duplicate-concurrent-queries
tags: [request-coalescing, deduplication, concurrent-requests, singleflight, latency-reduction, cost-reduction]
symptoms:
  - "Metrics show 10× duplicate LLM calls when a news event causes concurrent identical queries"
  - "No mechanism to detect that two sessions are asking the same question simultaneously"
  - "Token cost spikes during traffic bursts that are all asking the same trending question"
  - "Cache misses on all concurrent requests because the cache entry isn't written yet"
  - "Thundering herd on tool calls when a scheduled job triggers many sessions simultaneously"
---

## Why This Happens

A standard cache handles repeated requests sequentially — the second request arrives after the first has already written the result. Concurrent duplicate requests all miss the cache simultaneously because the result doesn't exist yet when they check. Request coalescing (the "singleflight" pattern) solves this: the first request creates a pending future; subsequent identical requests attach to the same future rather than launching new calls. When the first request resolves, all waiters receive the result at once.

## Solution 1: Request Fingerprinter

```python
import hashlib
import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass
class CoalescingKey:
    fingerprint: str
    namespace: str   # e.g. "llm_chat", "tool_web_search"

    def full_key(self) -> str:
        return f"{self.namespace}:{self.fingerprint}"


class RequestFingerprinter:
    """
    Produces a stable fingerprint for a request that is identical
    across concurrent callers asking the same question.
    Excludes session-specific fields like session_id or request_id.
    """

    def fingerprint_llm(
        self,
        model: str,
        messages: List[Dict[str, str]],
        max_tokens: int,
        temperature: float = 0.0,
    ) -> CoalescingKey:
        # Only coalesce deterministic requests (temperature=0)
        if temperature > 0.0:
            import secrets
            return CoalescingKey(fingerprint=secrets.token_hex(8), namespace="llm_chat")

        payload = json.dumps(
            {"model": model, "messages": messages, "max_tokens": max_tokens},
            sort_keys=True,
        )
        fp = hashlib.sha256(payload.encode()).hexdigest()[:24]
        return CoalescingKey(fingerprint=fp, namespace="llm_chat")

    def fingerprint_tool(
        self,
        tool_name: str,
        args: Dict[str, Any],
    ) -> CoalescingKey:
        payload = json.dumps({"tool": tool_name, "args": args}, sort_keys=True)
        fp = hashlib.sha256(payload.encode()).hexdigest()[:24]
        return CoalescingKey(fingerprint=fp, namespace=f"tool_{tool_name}")
```

## Solution 2: Coalescing Flight Tracker

```python
import asyncio
import time
from typing import Any, Dict, Optional, Tuple


@dataclass
class CoalescingFlight:
    key: str
    future: asyncio.Future
    started_at: float
    waiter_count: int = 1

    def age_seconds(self) -> float:
        return time.time() - self.started_at


class CoalescingFlightTracker:
    """
    Tracks in-flight requests by their coalescing key.
    New requests with the same key attach to the existing flight
    instead of launching a duplicate call.
    """

    def __init__(self, max_flight_age_seconds: float = 60.0) -> None:
        self._flights: Dict[str, CoalescingFlight] = {}
        self._max_age = max_flight_age_seconds
        self._coalesced_count = 0
        self._total_count = 0

    def _evict_stale(self) -> None:
        stale = [k for k, f in self._flights.items() if f.age_seconds() > self._max_age]
        for k in stale:
            flight = self._flights.pop(k)
            if not flight.future.done():
                flight.future.cancel()

    def get_or_create(self, key: str) -> Tuple[asyncio.Future, bool]:
        """
        Returns (future, is_new).
        is_new=True: caller must execute the request and resolve the future.
        is_new=False: caller should await the future (already in flight).
        """
        self._evict_stale()
        self._total_count += 1

        if key in self._flights:
            flight = self._flights[key]
            if not flight.future.done():
                flight.waiter_count += 1
                self._coalesced_count += 1
                return flight.future, False

        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._flights[key] = CoalescingFlight(
            key=key,
            future=future,
            started_at=time.time(),
        )
        return future, True

    def resolve(self, key: str, result: Any) -> None:
        flight = self._flights.pop(key, None)
        if flight and not flight.future.done():
            flight.future.set_result(result)

    def reject(self, key: str, exc: Exception) -> None:
        flight = self._flights.pop(key, None)
        if flight and not flight.future.done():
            flight.future.set_exception(exc)

    def stats(self) -> dict:
        return {
            "active_flights": len(self._flights),
            "total_requests": self._total_count,
            "coalesced_requests": self._coalesced_count,
            "coalescing_rate": round(self._coalesced_count / max(self._total_count, 1), 4),
        }
```

## Solution 3: Coalescing Executor

```python
import asyncio
from typing import Any, Callable


class CoalescingExecutor:
    """
    Wraps any async callable with request coalescing.
    Identical in-flight requests share a single execution;
    all waiters receive the result when it resolves.
    """

    def __init__(self, tracker: CoalescingFlightTracker) -> None:
        self._tracker = tracker

    async def execute(
        self,
        coalescing_key: CoalescingKey,
        fn: Callable,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        full_key = coalescing_key.full_key()
        future, is_new = self._tracker.get_or_create(full_key)

        if not is_new:
            # Attach to in-flight request — wait for existing future
            return await asyncio.shield(future)

        # We are the designated executor
        try:
            result = await fn(*args, **kwargs)
            self._tracker.resolve(full_key, result)
            return result
        except Exception as exc:
            self._tracker.reject(full_key, exc)
            raise
```

## Solution 4: Coalescing LLM Client

```python
from typing import Any, Callable, Dict, List, Optional


class CoalescingLLMClient:
    """
    Drop-in LLM call wrapper that coalesces identical concurrent requests.
    Deterministic calls (temperature=0) are eligible for coalescing.
    Non-deterministic calls bypass coalescing and execute independently.
    """

    def __init__(
        self,
        executor: CoalescingExecutor,
        fingerprinter: RequestFingerprinter,
        llm_fn: Callable,   # async fn(model, messages, max_tokens, **kwargs) -> dict
    ) -> None:
        self._executor = executor
        self._fingerprinter = fingerprinter
        self._llm_fn = llm_fn

    async def chat(
        self,
        model: str,
        messages: List[Dict[str, str]],
        max_tokens: int = 1024,
        temperature: float = 0.0,
        **kwargs: Any,
    ) -> dict:
        key = self._fingerprinter.fingerprint_llm(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        return await self._executor.execute(
            key,
            self._llm_fn,
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            **kwargs,
        )


class CoalescingToolClient:
    """
    Drop-in tool call wrapper with coalescing for idempotent tools.
    Non-idempotent tools (write operations) must bypass coalescing.
    """

    def __init__(
        self,
        executor: CoalescingExecutor,
        fingerprinter: RequestFingerprinter,
        idempotent_tools: Optional[List[str]] = None,
    ) -> None:
        self._executor = executor
        self._fingerprinter = fingerprinter
        self._idempotent = set(idempotent_tools or [])

    async def call(
        self,
        tool_name: str,
        tool_fn: Callable,
        args: Dict[str, Any],
    ) -> Any:
        if tool_name not in self._idempotent:
            return await tool_fn(**args)

        key = self._fingerprinter.fingerprint_tool(tool_name, args)
        return await self._executor.execute(key, tool_fn, **args)
```

## Solution 5: Coalescing Effectiveness Monitor

```python
import time
from typing import List


class CoalescingEffectivenessMonitor:
    """
    Reports on coalescing savings and alerts when coalescing rate
    is unexpectedly low (suggesting fingerprinting is too narrow)
    or when flights are stacking up (suggesting slow underlying calls).
    """

    def __init__(
        self,
        tracker: CoalescingFlightTracker,
        expected_min_coalescing_rate: float = 0.05,
        max_active_flights_alert: int = 50,
    ) -> None:
        self._tracker = tracker
        self._min_rate = expected_min_coalescing_rate
        self._max_flights = max_active_flights_alert

    def check(self) -> List[dict]:
        stats = self._tracker.stats()
        alerts = []

        if (stats["total_requests"] > 100
                and stats["coalescing_rate"] < self._min_rate):
            alerts.append({
                "type": "low_coalescing_rate",
                "coalescing_rate": stats["coalescing_rate"],
                "expected_min": self._min_rate,
                "recommendation": (
                    "Most requests are unique — coalescing provides little benefit. "
                    "Consider widening the fingerprinting key or disabling coalescing."
                ),
            })

        if stats["active_flights"] >= self._max_flights:
            alerts.append({
                "type": "flight_accumulation",
                "active_flights": stats["active_flights"],
                "threshold": self._max_flights,
                "recommendation": (
                    "Too many in-flight requests — underlying calls may be slow or hanging."
                ),
            })

        return alerts

    def report(self) -> dict:
        return {
            "generated_at": time.time(),
            "stats": self._tracker.stats(),
            "alerts": self.check(),
        }
```

## Solution 6: Coalescing Dashboard

```python
import time


class CoalescingDashboard:
    """
    Combines flight tracker stats, coalescing effectiveness,
    and alerts into a single operational view.
    """

    def __init__(
        self,
        tracker: CoalescingFlightTracker,
        monitor: CoalescingEffectivenessMonitor,
    ) -> None:
        self._tracker = tracker
        self._monitor = monitor

    def render(self) -> dict:
        stats = self._tracker.stats()
        saved_calls = stats["coalesced_requests"]

        return {
            "generated_at": time.time(),
            "coalescing": {
                "active_flights": stats["active_flights"],
                "total_requests": stats["total_requests"],
                "coalesced_requests": saved_calls,
                "coalescing_rate_pct": round(stats["coalescing_rate"] * 100, 1),
                "estimated_calls_avoided": saved_calls,
            },
            "active_alerts": self._monitor.check(),
        }
```

## Comparison

| Approach | Fingerprinting | In-Flight Dedup | Waiter Attachment | Tool Support | Monitoring |
|---|---|---|---|---|---|
| RequestFingerprinter | Yes | No | No | Yes | No |
| CoalescingFlightTracker | No | Yes | Yes | No | No |
| CoalescingExecutor | Via fingerprinter | Via tracker | Via tracker | No | No |
| CoalescingLLMClient | Via fingerprinter | Via executor | Via executor | No | No |
| CoalescingToolClient | Via fingerprinter | Via executor | Via executor | Yes (idempotent) | No |
| CoalescingEffectivenessMonitor | No | No | No | No | Yes |

**Best for production**: Only coalesce deterministic calls (`temperature=0`) — coalescing non-deterministic calls means all waiters get the same response, which may break use cases expecting variety. Mark read-only tools (search, lookup, price fetch) as idempotent; never coalesce write operations. Set `max_flight_age_seconds=30` — flights older than 30 seconds indicate a hung call, not a slow one, and should be cancelled. Monitor `coalescing_rate`: during a traffic spike (news event, viral query), rates above 40% are normal and represent real cost savings; rates below 2% at high traffic suggest the fingerprint is too specific to be useful.
