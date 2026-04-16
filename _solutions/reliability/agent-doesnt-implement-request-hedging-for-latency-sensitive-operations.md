---
title: "Agent Doesn't Implement Request Hedging for Latency-Sensitive Operations"
description: "Agents that wait for a single slow tool call to complete before proceeding lose the ability to meet tight latency budgets when the primary call is slow. Request hedging — sending a second identical request after a short delay if the first has not returned — provides a practical way to reduce tail latency for idempotent read operations without over-loading backends. Implement request hedging that triggers a backup request after a configurable delay and returns whichever response arrives first."
date: 2026-04-16
difficulty: advanced
category: reliability
slug: agent-doesnt-implement-request-hedging-for-latency-sensitive-operations
tags: [request-hedging, tail-latency, backup-request, p99-reduction, latency-sensitive, read-operations]
symptoms:
  - "P99 tool call latency is 3× median because slow backend shards cause tail latency"
  - "Agent misses response time SLOs on 1% of requests due to occasionally slow database reads"
  - "No mechanism to retry a slow request before it times out"
  - "Tail latency variance is high but backend health looks fine in aggregate"
  - "Users experience occasional 5-second delays for operations that normally take 200ms"
---

## Why This Happens

Tail latency in distributed systems is caused by slow backend instances, GC pauses, network jitter, or queue depth variation. The slowest 1% of requests often take 5–10× longer than median. Waiting for the single slow request to complete means the agent inherits the backend's tail latency. Hedging addresses this by firing a second request after a short hedge delay (e.g., 2× median latency). If the first request is slow, the second request likely hits a different shard or instance and returns faster. The first response to arrive cancels the other. Hedging only applies to idempotent operations — reads, not writes.

## Solution 1: Hedge Policy

```python
from dataclasses import dataclass


@dataclass
class HedgePolicy:
    hedge_delay_ms: float = 200.0        # fire second request after this delay
    max_hedge_requests: int = 2          # total requests including primary
    cancel_slower_on_first_response: bool = True
    idempotent_only: bool = True         # safety guard against non-idempotent tools
```

## Solution 2: Hedge Request Executor

```python
import asyncio
import time
from typing import Any, Callable, Optional


class HedgeResult:
    def __init__(
        self,
        value: Any,
        responding_attempt: int,     # 1 = primary, 2 = hedge
        primary_latency_ms: Optional[float],
        hedge_latency_ms: Optional[float],
        hedge_was_faster: bool,
    ):
        self.value = value
        self.responding_attempt = responding_attempt
        self.primary_latency_ms = primary_latency_ms
        self.hedge_latency_ms = hedge_latency_ms
        self.hedge_was_faster = hedge_was_faster


class HedgeRequestExecutor:
    """
    Executes a primary request and, after hedge_delay_ms, fires an
    identical backup request. Returns whichever finishes first and
    cancels the other. For idempotent (read) operations only.
    """

    def __init__(self, policy: HedgePolicy):
        self._policy = policy

    async def execute(self, fn: Callable[[], Any]) -> HedgeResult:
        loop = asyncio.get_event_loop()
        primary_start = time.time()
        hedge_start: Optional[float] = None

        primary_task = asyncio.create_task(fn())
        hedge_task: Optional[asyncio.Task] = None
        result_value: Any = None
        responding = 1

        try:
            # Wait for primary with hedge_delay timeout
            try:
                result_value = await asyncio.wait_for(
                    asyncio.shield(primary_task),
                    timeout=self._policy.hedge_delay_ms / 1000.0,
                )
                # Primary responded before hedge delay — done
                primary_ms = round((time.time() - primary_start) * 1000, 2)
                return HedgeResult(
                    value=result_value,
                    responding_attempt=1,
                    primary_latency_ms=primary_ms,
                    hedge_latency_ms=None,
                    hedge_was_faster=False,
                )
            except asyncio.TimeoutError:
                pass

            # Primary is slow — fire hedge request
            hedge_start = time.time()
            hedge_task = asyncio.create_task(fn())

            done, pending = await asyncio.wait(
                {primary_task, hedge_task},
                return_when=asyncio.FIRST_COMPLETED,
            )

            # Cancel remaining tasks
            for task in pending:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

            winner = done.pop()
            result_value = winner.result()
            hedge_was_faster = winner is hedge_task
            responding = 2 if hedge_was_faster else 1

            primary_ms = round((time.time() - primary_start) * 1000, 2)
            hedge_ms = round((time.time() - hedge_start) * 1000, 2) if hedge_start else None

            return HedgeResult(
                value=result_value,
                responding_attempt=responding,
                primary_latency_ms=primary_ms,
                hedge_latency_ms=hedge_ms,
                hedge_was_faster=hedge_was_faster,
            )

        except Exception:
            if hedge_task and not hedge_task.done():
                hedge_task.cancel()
            if not primary_task.done():
                primary_task.cancel()
            raise
```

## Solution 3: Idempotency Guard

```python
from typing import Set


class HedgeIdempotencyGuard:
    """
    Tracks which tools are registered as idempotent and safe for hedging.
    Non-idempotent tools (writes, mutations) are never hedged.
    """

    def __init__(self):
        self._idempotent_tools: Set[str] = set()
        self._non_idempotent_tools: Set[str] = set()

    def register_idempotent(self, tool_name: str) -> None:
        self._idempotent_tools.add(tool_name)
        self._non_idempotent_tools.discard(tool_name)

    def register_non_idempotent(self, tool_name: str) -> None:
        self._non_idempotent_tools.add(tool_name)
        self._idempotent_tools.discard(tool_name)

    def is_hedgeable(self, tool_name: str) -> bool:
        """
        Returns True only if explicitly registered as idempotent.
        Defaults to False (safe: do not hedge unknown tools).
        """
        return tool_name in self._idempotent_tools
```

## Solution 4: Hedged Tool Dispatcher

```python
import time
from typing import Any, Callable, Optional


class HedgedToolDispatcher:
    """
    Wraps tool calls with hedging for registered idempotent tools.
    Falls back to direct dispatch for non-hedgeable tools.
    """

    def __init__(
        self,
        executor: HedgeRequestExecutor,
        guard: HedgeIdempotencyGuard,
        stats_collector: Optional[Any] = None,
    ):
        self._executor = executor
        self._guard = guard
        self._stats = stats_collector

    async def dispatch(
        self,
        tool_name: str,
        fn: Callable[[], Any],
    ) -> Any:
        if not self._guard.is_hedgeable(tool_name):
            return await fn()

        result = await self._executor.execute(fn)

        if self._stats:
            self._stats.record(tool_name, result)

        return result.value
```

## Solution 5: Hedge Effectiveness Tracker

```python
import time
from collections import deque
from threading import Lock
from typing import Deque, Dict


class HedgeEffectivenessTracker:
    """
    Records hedge outcomes to measure whether hedging is reducing
    tail latency and how often the hedge request wins.
    """

    def __init__(self, window_seconds: float = 3600.0):
        self._window = window_seconds
        self._records: Deque[dict] = deque()
        self._lock = Lock()

    def record(self, tool_name: str, result: HedgeResult) -> None:
        with self._lock:
            self._records.append({
                "ts": time.time(),
                "tool_name": tool_name,
                "primary_ms": result.primary_latency_ms,
                "hedge_ms": result.hedge_latency_ms,
                "hedge_was_faster": result.hedge_was_faster,
                "hedged": result.hedge_latency_ms is not None,
            })

    def summary(self) -> dict:
        cutoff = time.time() - self._window
        with self._lock:
            recent = [r for r in self._records if r["ts"] >= cutoff]
        if not recent:
            return {"calls": 0}

        hedged = [r for r in recent if r["hedged"]]
        hedge_wins = sum(1 for r in hedged if r["hedge_was_faster"])

        primary_latencies = [r["primary_ms"] for r in recent if r["primary_ms"]]

        return {
            "calls": len(recent),
            "hedged_calls": len(hedged),
            "hedge_rate": round(len(hedged) / len(recent), 4),
            "hedge_win_rate": round(hedge_wins / max(len(hedged), 1), 4),
            "avg_primary_latency_ms": round(
                sum(primary_latencies) / len(primary_latencies), 2
            ) if primary_latencies else None,
        }
```

## Solution 6: Hedge Configuration Dashboard

```python
import time


class RequestHedgingDashboard:
    """
    Renders hedge policy, idempotency registry, and effectiveness
    statistics for operational visibility.
    """

    def __init__(
        self,
        policy: HedgePolicy,
        guard: HedgeIdempotencyGuard,
        tracker: HedgeEffectivenessTracker,
    ):
        self._policy = policy
        self._guard = guard
        self._tracker = tracker

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "policy": {
                "hedge_delay_ms": self._policy.hedge_delay_ms,
                "max_hedge_requests": self._policy.max_hedge_requests,
            },
            "hedgeable_tools": sorted(self._guard._idempotent_tools),
            "non_hedgeable_tools": sorted(self._guard._non_idempotent_tools),
            "effectiveness_1h": self._tracker.summary(),
        }
```

## Comparison

| Approach | Hedge Delay Firing | First-Response Cancel | Idempotency Guard | Effectiveness Tracking | Dashboard |
|---|---|---|---|---|---|
| HedgeRequestExecutor | Yes (asyncio.wait_for) | Yes | No | No | No |
| HedgeIdempotencyGuard | No | No | Yes (allowlist) | No | No |
| HedgedToolDispatcher | Via executor | Via executor | Via guard | Via tracker | No |
| HedgeEffectivenessTracker | No | No | No | Yes | No |
| RequestHedgingDashboard | No | No | No | No | Yes |

**Best for production**: Set `hedge_delay_ms` to the P75 latency of the target tool — hedging triggers only when a call exceeds the typical fast path, covering the slow tail without sending redundant requests on normal calls. Only hedge tools explicitly registered as idempotent — never hedge writes, payments, or state mutations. Monitor `hedge_win_rate` via `HedgeEffectivenessTracker`: a win rate above 0.30 means the backend has significant variance and hedging is providing real value; below 0.05 means the hedge delay is too long (hedge fires after the primary already would have returned) and should be reduced.
