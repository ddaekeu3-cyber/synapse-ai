---
title: "Agent Doesn't Implement Request Hedging for High-Percentile Latency Reduction"
description: "Agents that send a single LLM or tool request and wait for its completion are vulnerable to high-percentile latency spikes: one slow shard, one cold container, one overloaded replica can stall the entire session. Implement request hedging that fires a duplicate request after a short delay, uses whichever response arrives first, and cancels the slower twin — reducing P99 latency without increasing median cost."
date: 2026-04-16
difficulty: advanced
category: reliability
slug: agent-doesnt-implement-request-hedging-for-high-percentile-latency-reduction
tags: [hedging, latency-reduction, p99, parallel-requests, tail-latency, speculative-execution]
symptoms:
  - "P99 tool latency is 8× median — one slow replica stalls the session"
  - "Users experience random 3–10 second hangs with no apparent cause"
  - "Retries after timeout are too slow — the user already gave up"
  - "No mechanism to race multiple backends and take the fastest response"
  - "Latency SLO breaches are frequent even though median latency is fine"
---

## Why This Happens

A single-request model has no escape from tail latency: if the server you hit is slow, you wait. Hedging breaks this by launching a second identical request after a brief delay (the hedge delay) and racing the two. The winner's response is used; the loser is cancelled. The cost is one extra request for every session that hits the hedge delay — typically 1–5% of requests at P95. The latency saving is the difference between the slow server's time and the fast server's time, which at P99 can be 5–10×. Hedging is most effective for idempotent, read-only calls where duplicate execution is safe.

## Solution 1: Hedge Policy

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class HedgeStrategy(str, Enum):
    FIXED_DELAY = "fixed_delay"         # hedge after a fixed ms delay
    PERCENTILE_DELAY = "percentile_delay"  # hedge after the P50 latency of recent calls
    ADAPTIVE = "adaptive"               # switch between fixed and percentile based on load


@dataclass
class HedgePolicy:
    enabled: bool = True
    strategy: HedgeStrategy = HedgeStrategy.FIXED_DELAY
    fixed_delay_ms: float = 200.0       # fire hedge after this many ms
    max_hedges: int = 1                 # max parallel duplicates (1 = one hedge copy)
    hedge_budget_pct: float = 5.0       # max % of requests that may fire a hedge
    only_if_idempotent: bool = True     # skip hedging for non-idempotent calls
    cancel_loser: bool = True           # cancel the slower twin when winner arrives

    def hedge_delay_ms(self, p50_latency_ms: Optional[float] = None) -> float:
        if self.strategy == HedgeStrategy.FIXED_DELAY or p50_latency_ms is None:
            return self.fixed_delay_ms
        if self.strategy == HedgeStrategy.PERCENTILE_DELAY:
            return p50_latency_ms * 1.1   # 10% above median
        # ADAPTIVE: use fixed if p50 is not available, else percentile
        return p50_latency_ms * 1.1 if p50_latency_ms else self.fixed_delay_ms
```

## Solution 2: Hedge Budget Tracker

```python
import time
from threading import Lock


class HedgeBudgetTracker:
    """
    Enforces a cap on what fraction of requests may fire a hedge.
    Uses a sliding-window token bucket: once the budget is exhausted
    for the current window, no more hedges fire until the window resets.
    """

    def __init__(self, budget_pct: float = 5.0, window_seconds: float = 60.0):
        self._budget_pct = budget_pct / 100.0
        self._window = window_seconds
        self._lock = Lock()
        self._total_requests = 0
        self._hedged_requests = 0
        self._window_start = time.time()

    def _maybe_reset(self) -> None:
        if time.time() - self._window_start >= self._window:
            self._total_requests = 0
            self._hedged_requests = 0
            self._window_start = time.time()

    def record_request(self) -> None:
        with self._lock:
            self._maybe_reset()
            self._total_requests += 1

    def can_hedge(self) -> bool:
        with self._lock:
            self._maybe_reset()
            if self._total_requests == 0:
                return True
            current_rate = self._hedged_requests / self._total_requests
            return current_rate < self._budget_pct

    def record_hedge(self) -> None:
        with self._lock:
            self._hedged_requests += 1

    def stats(self) -> dict:
        with self._lock:
            self._maybe_reset()
            total = max(self._total_requests, 1)
            return {
                "window_seconds": self._window,
                "total_requests": self._total_requests,
                "hedged_requests": self._hedged_requests,
                "hedge_rate": round(self._hedged_requests / total, 4),
                "budget_pct": self._budget_pct * 100,
            }
```

## Solution 3: Latency Percentile Estimator

```python
import bisect
import time
from collections import deque
from threading import Lock
from typing import Deque, List, Optional, Tuple


class LatencyPercentileEstimator:
    """
    Maintains a sliding window of recent latency samples and
    computes P50/P95/P99 for use as adaptive hedge delays.
    """

    def __init__(self, window_seconds: float = 120.0, max_samples: int = 2000):
        self._window = window_seconds
        self._max = max_samples
        self._samples: Deque[Tuple[float, float]] = deque()  # (timestamp, latency_ms)
        self._lock = Lock()

    def record(self, latency_ms: float) -> None:
        with self._lock:
            now = time.time()
            self._samples.append((now, latency_ms))
            self._trim(now)
            if len(self._samples) > self._max:
                self._samples.popleft()

    def _trim(self, now: float) -> None:
        cutoff = now - self._window
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()

    def _sorted_latencies(self) -> List[float]:
        return sorted(s[1] for s in self._samples)

    def percentile(self, pct: float) -> Optional[float]:
        with self._lock:
            values = self._sorted_latencies()
            if not values:
                return None
            idx = int(len(values) * pct / 100.0)
            idx = min(idx, len(values) - 1)
            return round(values[idx], 2)

    def p50(self) -> Optional[float]:
        return self.percentile(50)

    def p95(self) -> Optional[float]:
        return self.percentile(95)

    def p99(self) -> Optional[float]:
        return self.percentile(99)
```

## Solution 4: Hedged Request Executor

```python
import asyncio
import time
from typing import Any, Callable, Optional, Tuple


class HedgedRequestExecutor:
    """
    Executes a callable and, if it has not returned within hedge_delay_ms,
    fires a second identical call. Returns the result of whichever finishes
    first and cancels the other.
    """

    def __init__(
        self,
        policy: HedgePolicy,
        budget_tracker: HedgeBudgetTracker,
        latency_estimator: LatencyPercentileEstimator,
    ):
        self._policy = policy
        self._budget = budget_tracker
        self._estimator = latency_estimator
        self._hedge_wins = 0
        self._primary_wins = 0
        self._total_calls = 0

    async def call(
        self,
        fn: Callable,
        *args: Any,
        idempotent: bool = True,
        **kwargs: Any,
    ) -> Tuple[Any, bool]:
        """
        Returns (result, hedged) where hedged=True if the hedge twin won.
        """
        self._total_calls += 1
        self._budget.record_request()

        start = time.time()

        should_hedge = (
            self._policy.enabled
            and idempotent
            and self._budget.can_hedge()
        )

        hedge_delay = self._policy.hedge_delay_ms(self._estimator.p50()) / 1000.0

        primary_task = asyncio.create_task(fn(*args, **kwargs))

        if not should_hedge:
            result = await primary_task
            latency_ms = (time.time() - start) * 1000
            self._estimator.record(latency_ms)
            self._primary_wins += 1
            return result, False

        # Wait for primary or hedge delay, whichever comes first
        try:
            result = await asyncio.wait_for(
                asyncio.shield(primary_task), timeout=hedge_delay
            )
            latency_ms = (time.time() - start) * 1000
            self._estimator.record(latency_ms)
            self._primary_wins += 1
            return result, False
        except asyncio.TimeoutError:
            pass  # primary is still running — fire the hedge

        # Primary is slow — launch hedge
        self._budget.record_hedge()
        hedge_task = asyncio.create_task(fn(*args, **kwargs))

        done, pending = await asyncio.wait(
            {primary_task, hedge_task},
            return_when=asyncio.FIRST_COMPLETED,
        )

        winner = next(iter(done))
        for loser in pending:
            if self._policy.cancel_loser:
                loser.cancel()

        latency_ms = (time.time() - start) * 1000
        self._estimator.record(latency_ms)

        hedged = winner is hedge_task
        if hedged:
            self._hedge_wins += 1
        else:
            self._primary_wins += 1

        return winner.result(), hedged

    def stats(self) -> dict:
        total = max(self._total_calls, 1)
        return {
            "total_calls": self._total_calls,
            "primary_wins": self._primary_wins,
            "hedge_wins": self._hedge_wins,
            "hedge_win_rate": round(self._hedge_wins / total, 4),
        }
```

## Solution 5: Hedged LLM Client Wrapper

```python
import asyncio
from typing import Any, Callable, Dict, Optional


class HedgedLLMClient:
    """
    Wraps any async LLM client with hedging. Streaming responses
    are not hedged (streaming is not idempotent in practice);
    only non-streaming completions are eligible.
    """

    def __init__(
        self,
        base_client_factory: Callable[[], Any],
        executor: HedgedRequestExecutor,
    ):
        self._factory = base_client_factory
        self._executor = executor

    async def complete(
        self,
        messages: list,
        model: str,
        temperature: float = 0.7,
        max_tokens: int = 1024,
        stream: bool = False,
        extra_kwargs: Optional[Dict[str, Any]] = None,
    ) -> Any:
        if stream:
            # Never hedge streaming — return directly
            client = self._factory()
            return await client.complete(
                messages=messages,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
                stream=True,
                **(extra_kwargs or {}),
            )

        async def _call() -> Any:
            client = self._factory()
            return await client.complete(
                messages=messages,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
                stream=False,
                **(extra_kwargs or {}),
            )

        result, hedged = await self._executor.call(
            _call,
            idempotent=True,
        )
        return result

    def hedge_stats(self) -> dict:
        return self._executor.stats()
```

## Solution 6: Hedging Effectiveness Monitor

```python
import time
from typing import List


class HedgingEffectivenessMonitor:
    """
    Tracks whether hedging is delivering meaningful latency savings
    by comparing P99 latency in windows where hedging fired versus
    windows where it did not. Also detects if the budget is too tight.
    """

    def __init__(
        self,
        executor: HedgedRequestExecutor,
        budget_tracker: HedgeBudgetTracker,
        estimator: LatencyPercentileEstimator,
    ):
        self._executor = executor
        self._budget = budget_tracker
        self._estimator = estimator

    def report(self) -> dict:
        exec_stats = self._executor.stats()
        budget_stats = self._budget.stats()

        alerts = []

        if (
            exec_stats["total_calls"] > 100
            and exec_stats["hedge_wins"] > 0
        ):
            hedge_win_rate = exec_stats["hedge_win_rate"]
            if hedge_win_rate > 0.30:
                alerts.append({
                    "type": "high_hedge_win_rate",
                    "value": hedge_win_rate,
                    "message": (
                        f"Hedges win {hedge_win_rate:.0%} of the time — primary server "
                        "may be consistently slow. Consider routing away from it."
                    ),
                })

        if budget_stats["hedge_rate"] >= budget_stats["budget_pct"] / 100 * 0.95:
            alerts.append({
                "type": "budget_saturated",
                "hedge_rate": budget_stats["hedge_rate"],
                "message": "Hedge budget nearly exhausted — increase budget_pct or reduce load.",
            })

        return {
            "generated_at": time.time(),
            "latency_p50_ms": self._estimator.p50(),
            "latency_p95_ms": self._estimator.p95(),
            "latency_p99_ms": self._estimator.p99(),
            "execution": exec_stats,
            "budget": budget_stats,
            "healthy": len(alerts) == 0,
            "alerts": alerts,
        }
```

## Comparison

| Approach | Hedge Trigger | Budget Enforcement | Adaptive Delay | Streaming Safe | Monitoring |
|---|---|---|---|---|---|
| HedgePolicy | Fixed / percentile | No | Yes (percentile mode) | No | No |
| HedgeBudgetTracker | No | Yes (sliding window) | No | No | No |
| LatencyPercentileEstimator | No | No | Yes (P50 source) | No | No |
| HedgedRequestExecutor | Yes (asyncio race) | Via tracker | Via estimator | No | Partial |
| HedgedLLMClient | Via executor | Via executor | Via executor | Yes (skip) | No |
| HedgingEffectivenessMonitor | No | No | No | No | Yes |

**Best for production**: Set `fixed_delay_ms` to your P75 latency — this ensures hedges only fire for requests in the slow tail, not the median. Keep `hedge_budget_pct` at 5% to limit extra cost while covering most P99 events. Never hedge streaming completions or tool calls with side effects (writes, sends). Monitor `hedge_win_rate`: above 20% means your primary backend has a persistent slowness problem that hedging is masking — investigate the root cause rather than increasing the hedge budget.
