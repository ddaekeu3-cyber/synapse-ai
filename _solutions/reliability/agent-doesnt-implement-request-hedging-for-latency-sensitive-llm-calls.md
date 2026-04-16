---
title: "Agent Doesn't Implement Request Hedging for Latency-Sensitive LLM Calls"
description: "Agents that issue a single LLM request and wait are fully exposed to tail latency: the 99th percentile response time can be 5–10× the median. Request hedging fires a duplicate request to a secondary provider after a short delay and uses whichever response arrives first, capping the worst-case latency at the hedge delay plus the faster provider's median. Implement hedging that fires a second request after a configurable threshold, cancels the loser, and tracks hedge activation rates."
date: 2026-04-16
difficulty: advanced
category: reliability
slug: agent-doesnt-implement-request-hedging-for-latency-sensitive-llm-calls
tags: [request-hedging, tail-latency, llm-redundancy, latency-p99, duplicate-requests, provider-fallback]
symptoms:
  - "P99 LLM latency is 8s while P50 is 1.2s — tail dominates user-facing response time"
  - "No mechanism to detect a slow request and fire a backup before it times out"
  - "Single provider dependency means any provider slowdown is fully user-visible"
  - "Retries only fire after full timeout — the user already waited the full duration"
  - "Cannot bound worst-case latency without hedging or speculative execution"
---

## Why This Happens

A single LLM request follows one execution path: send, wait, receive. Tail latency spikes (cold containers, overloaded routing, network jitter) are invisible until the timeout fires. Hedging adds a second path: after waiting `hedge_delay_ms`, if no response has arrived, fire a duplicate request to the same or a different provider. The first response wins; the slower request is cancelled. This trades a small increase in average cost (hedge activation rate × duplicate cost) for a large reduction in P99 latency. The hedge delay is the key parameter: set it at the P75 latency to catch the slow tail while minimizing unnecessary duplicates.

## Solution 1: Hedge Policy

```python
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class HedgePolicy:
    hedge_delay_ms: float = 500.0       # fire duplicate after this delay
    max_hedges: int = 1                  # number of duplicate requests to fire
    hedge_providers: List[str] = field(default_factory=list)  # provider names; empty = same provider
    cancel_loser: bool = True            # cancel slower request when winner arrives
    min_request_ms: float = 100.0        # don't hedge requests expected to be fast
    hedge_on_errors: bool = False        # also hedge when first attempt errors (not just slow)
    budget_pct: float = 0.10             # max fraction of requests that may be hedged
```

## Solution 2: Hedge Budget Tracker

```python
import time
from collections import deque
from typing import Deque


class HedgeBudgetTracker:
    """
    Enforces the hedge budget: if too many requests are being hedged
    (indicating the hedge delay is too aggressive), suppress new hedges
    until the rate drops below budget_pct.
    Uses a sliding window to measure the recent hedge rate.
    """

    def __init__(self, window_seconds: float = 60.0, budget_pct: float = 0.10):
        self._window = window_seconds
        self._budget = budget_pct
        self._requests: Deque[float] = deque()
        self._hedges: Deque[float] = deque()

    def record_request(self) -> None:
        now = time.time()
        self._requests.append(now)
        self._trim(now)

    def record_hedge(self) -> None:
        self._hedges.append(time.time())

    def _trim(self, now: float) -> None:
        cutoff = now - self._window
        while self._requests and self._requests[0] < cutoff:
            self._requests.popleft()
        while self._hedges and self._hedges[0] < cutoff:
            self._hedges.popleft()

    def hedge_allowed(self) -> bool:
        self._trim(time.time())
        total = len(self._requests)
        if total == 0:
            return True
        return len(self._hedges) / total < self._budget

    def stats(self) -> dict:
        self._trim(time.time())
        total = max(len(self._requests), 1)
        return {
            "requests_in_window": len(self._requests),
            "hedges_in_window": len(self._hedges),
            "hedge_rate": round(len(self._hedges) / total, 4),
            "budget_pct": self._budget,
            "budget_exhausted": not self.hedge_allowed(),
        }
```

## Solution 3: LLM Provider Interface and Hedge Executor

```python
import asyncio
from dataclasses import dataclass
from typing import Any, Callable, Coroutine, List, Optional, Tuple


@dataclass
class LLMResponse:
    content: str
    provider: str
    latency_ms: float
    was_hedge: bool = False
    tokens_used: int = 0


class HedgeExecutor:
    """
    Fires the primary LLM request immediately, then after hedge_delay_ms
    fires a duplicate request. Returns whichever completes first and
    cancels the other.
    """

    def __init__(
        self,
        policy: HedgePolicy,
        budget_tracker: HedgeBudgetTracker,
    ):
        self._policy = policy
        self._budget = budget_tracker
        self._hedge_activations = 0
        self._total_requests = 0
        self._winner_was_hedge = 0

    async def call(
        self,
        primary_fn: Callable[[], Coroutine[Any, Any, LLMResponse]],
        hedge_fn: Optional[Callable[[], Coroutine[Any, Any, LLMResponse]]] = None,
    ) -> LLMResponse:
        self._total_requests += 1
        self._budget.record_request()

        primary_task = asyncio.create_task(primary_fn())

        if hedge_fn is None or not self._budget.hedge_allowed():
            return await primary_task

        # Wait for hedge delay; if primary finishes first, skip hedge
        try:
            result = await asyncio.wait_for(
                asyncio.shield(primary_task),
                timeout=self._policy.hedge_delay_ms / 1000.0,
            )
            return result
        except asyncio.TimeoutError:
            pass  # primary is slow — fire hedge

        self._hedge_activations += 1
        self._budget.record_hedge()
        hedge_task = asyncio.create_task(hedge_fn())

        done, pending = await asyncio.wait(
            [primary_task, hedge_task],
            return_when=asyncio.FIRST_COMPLETED,
        )

        winner_task = next(iter(done))
        for loser in pending:
            if self._policy.cancel_loser:
                loser.cancel()

        result = winner_task.result()
        if winner_task is hedge_task:
            self._winner_was_hedge += 1
            result.was_hedge = True
        return result

    def stats(self) -> dict:
        total = max(self._total_requests, 1)
        return {
            "total_requests": self._total_requests,
            "hedge_activations": self._hedge_activations,
            "hedge_activation_rate": round(self._hedge_activations / total, 4),
            "hedge_wins": self._winner_was_hedge,
            "hedge_win_rate": round(
                self._winner_was_hedge / max(self._hedge_activations, 1), 4
            ),
        }
```

## Solution 4: Provider-Aware Hedge Client

```python
import asyncio
import time
from typing import Any, Callable, Dict, List, Optional


class ProviderAwareHedgeClient:
    """
    Manages multiple LLM provider callables and selects the hedge provider
    based on recent latency measurements. Tracks per-provider P50/P99 estimates.
    """

    def __init__(
        self,
        executor: HedgeExecutor,
        providers: Dict[str, Callable],
    ):
        self._executor = executor
        self._providers = providers
        self._latencies: Dict[str, List[float]] = {p: [] for p in providers}
        self._primary: str = next(iter(providers))

    def set_primary(self, provider_name: str) -> None:
        if provider_name not in self._providers:
            raise KeyError(f"Unknown provider: {provider_name}")
        self._primary = provider_name

    def _best_hedge_provider(self) -> Optional[str]:
        candidates = [p for p in self._providers if p != self._primary]
        if not candidates:
            return self._primary  # hedge same provider
        # pick candidate with lowest P50
        def p50(p: str) -> float:
            lats = sorted(self._latencies[p])
            if not lats:
                return 0.0
            return lats[len(lats) // 2]
        return min(candidates, key=p50)

    async def complete(self, prompt: str, **kwargs: Any) -> LLMResponse:
        primary_name = self._primary
        hedge_name = self._best_hedge_provider()

        async def call_provider(name: str) -> LLMResponse:
            start = time.time()
            fn = self._providers[name]
            content = await fn(prompt, **kwargs)
            ms = (time.time() - start) * 1000
            self._latencies[name].append(ms)
            if len(self._latencies[name]) > 200:
                self._latencies[name] = self._latencies[name][-200:]
            return LLMResponse(
                content=content,
                provider=name,
                latency_ms=round(ms, 2),
            )

        return await self._executor.call(
            primary_fn=lambda: call_provider(primary_name),
            hedge_fn=(lambda: call_provider(hedge_name)) if hedge_name else None,
        )

    def latency_percentiles(self) -> Dict[str, dict]:
        result = {}
        for provider, lats in self._latencies.items():
            if not lats:
                result[provider] = {}
                continue
            s = sorted(lats)
            n = len(s)
            result[provider] = {
                "p50_ms": round(s[n // 2], 1),
                "p75_ms": round(s[int(n * 0.75)], 1),
                "p99_ms": round(s[int(n * 0.99)], 1),
                "samples": n,
            }
        return result
```

## Solution 5: Hedge Latency Monitor

```python
import time
from collections import deque
from typing import Deque, List


class HedgeLatencyMonitor:
    """
    Tracks request latency with and without hedge activation.
    Computes the P99 reduction attributable to hedging.
    Recommends hedge_delay_ms adjustment based on recent P75.
    """

    def __init__(self, window_seconds: float = 300.0):
        self._window = window_seconds
        self._events: Deque[dict] = deque()

    def record(
        self,
        latency_ms: float,
        hedge_activated: bool,
        was_hedge_winner: bool,
    ) -> None:
        self._events.append({
            "ts": time.time(),
            "latency_ms": latency_ms,
            "hedge_activated": hedge_activated,
            "was_hedge_winner": was_hedge_winner,
        })

    def _trim(self) -> None:
        cutoff = time.time() - self._window
        while self._events and self._events[0]["ts"] < cutoff:
            self._events.popleft()

    def _percentile(self, values: List[float], pct: float) -> float:
        if not values:
            return 0.0
        s = sorted(values)
        return round(s[int(len(s) * pct / 100)], 1)

    def report(self) -> dict:
        self._trim()
        all_lats = [e["latency_ms"] for e in self._events]
        non_hedged = [e["latency_ms"] for e in self._events if not e["hedge_activated"]]

        recommended_delay = None
        if all_lats:
            p75 = self._percentile(all_lats, 75)
            recommended_delay = round(p75, 0)

        return {
            "sample_count": len(all_lats),
            "all_requests": {
                "p50_ms": self._percentile(all_lats, 50),
                "p99_ms": self._percentile(all_lats, 99),
            },
            "non_hedged_requests": {
                "p50_ms": self._percentile(non_hedged, 50),
                "p99_ms": self._percentile(non_hedged, 99),
                "count": len(non_hedged),
            },
            "recommended_hedge_delay_ms": recommended_delay,
        }
```

## Solution 6: Hedging Dashboard

```python
import time


class HedgingDashboard:
    """
    Combines hedge executor stats, budget tracker state,
    latency monitor report, and provider percentiles into a
    single observability snapshot.
    """

    def __init__(
        self,
        executor: HedgeExecutor,
        budget_tracker: HedgeBudgetTracker,
        monitor: HedgeLatencyMonitor,
        client: ProviderAwareHedgeClient,
    ):
        self._executor = executor
        self._budget = budget_tracker
        self._monitor = monitor
        self._client = client

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "hedge_stats": self._executor.stats(),
            "budget": self._budget.stats(),
            "latency": self._monitor.report(),
            "provider_percentiles": self._client.latency_percentiles(),
        }
```

## Comparison

| Approach | Fires Duplicate | Budget Enforcement | Provider Selection | Latency Tracking | Tuning Guidance |
|---|---|---|---|---|---|
| HedgeExecutor | Yes | Via budget tracker | No | No | No |
| HedgeBudgetTracker | No | Yes (sliding window) | No | No | No |
| ProviderAwareHedgeClient | Via executor | Via executor | Yes (P50-based) | Yes (per-provider) | No |
| HedgeLatencyMonitor | No | No | No | Yes (P50/P99) | Yes (recommended delay) |
| HedgingDashboard | No | No | No | No | Aggregate |

**Best for production**: Set `hedge_delay_ms` to the P75 of your primary provider's recent latency — this hedges only the slow tail while leaving the fast majority un-hedged. Set `budget_pct=0.10` to cap duplicate request costs at 10% overhead. Use `HedgeLatencyMonitor.report()` to recalibrate `hedge_delay_ms` weekly: as provider performance changes, the optimal delay shifts. Monitor `hedge_win_rate` in `HedgeExecutor.stats()` — if the hedge wins more than 60% of the time, the primary provider is consistently slower than the hedge provider and you should consider swapping them.
