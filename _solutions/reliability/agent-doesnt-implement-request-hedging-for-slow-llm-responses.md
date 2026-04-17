---
title: "Agent Doesn't Implement Request Hedging for Slow LLM Responses"
description: "Agents that wait indefinitely for a single LLM API call suffer tail-latency spikes when the upstream model is under load — a P99 that is 10× the P50 with no mitigation. Implement request hedging that launches a duplicate request to a backup endpoint or model after a hedge delay, cancels the slower of the two when the first response arrives, and reports which replica won."
date: 2026-04-16
difficulty: advanced
category: reliability
slug: agent-doesnt-implement-request-hedging-for-slow-llm-responses
tags: [request-hedging, tail-latency, redundant-requests, p99-latency, llm-timeout, backup-model]
symptoms:
  - "P99 LLM latency is 8–15× the P50 with no mitigation strategy"
  - "Slow upstream responses block the agent with no fallback until full timeout"
  - "No use of backup endpoints or secondary model providers during primary slowdowns"
  - "Users experience 30s+ waits during LLM API congestion events"
  - "Timeout-based retry adds the full wait before retrying rather than hedging in parallel"
---

## Why This Happens

LLM API tail latency is dominated by server-side queuing during high-demand periods. A P50 of 2 seconds with a P99 of 20 seconds means 1% of requests wait 10× longer through no fault of the agent. Canceling the wait and retrying adds the full wait time before starting the retry. Hedging instead launches a second request after a hedge delay (e.g., the P75 latency), then cancels whichever of the two arrives second. If both arrive quickly, the hedge request was wasted — but the waste is bounded and the latency gain is large. Hedging is only appropriate for idempotent LLM calls where duplicate execution has no side effects.

## Solution 1: Hedge Policy

```python
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class HedgePolicy:
    hedge_delay_ms: float = 800.0       # launch hedge after this many ms
    max_hedge_requests: int = 1         # usually 1 hedge is enough
    timeout_ms: float = 30000.0        # hard timeout for both requests
    hedge_to_same_endpoint: bool = True # False = use backup_endpoint
    backup_endpoint: str = ""           # alternate base URL or model ID
    cancel_loser: bool = True           # cancel the slower request on win
    enabled: bool = True

    def hedge_delay_s(self) -> float:
        return self.hedge_delay_ms / 1000.0

    def timeout_s(self) -> float:
        return self.timeout_ms / 1000.0
```

## Solution 2: Hedged Request Result

```python
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional


class HedgeOutcome(str, Enum):
    PRIMARY_WON = "primary_won"       # primary responded first
    HEDGE_WON = "hedge_won"           # hedge responded first
    BOTH_FAILED = "both_failed"       # both requests errored
    NO_HEDGE_NEEDED = "no_hedge_needed"  # primary responded before hedge launched
    HEDGING_DISABLED = "hedging_disabled"


@dataclass
class HedgedRequestResult:
    outcome: HedgeOutcome
    response: Any                     # the winning response
    primary_latency_ms: Optional[float]
    hedge_latency_ms: Optional[float]
    hedge_launched: bool
    latency_saved_ms: Optional[float]   # estimated saving vs waiting for loser
    error: Optional[str] = None
```

## Solution 3: Adaptive Hedge Delay Estimator

```python
import time
from collections import deque
from threading import Lock
from typing import Deque, Optional, Tuple


class AdaptiveHedgeDelayEstimator:
    """
    Tracks recent LLM call latencies and computes the P75 as the hedge delay.
    A hedge launched at P75 will fire on ~25% of requests — those that are
    already slower than most — minimizing wasted hedge requests.
    """

    def __init__(self, window: int = 200, default_delay_ms: float = 1000.0):
        self._samples: Deque[float] = deque(maxlen=window)
        self._default = default_delay_ms
        self._lock = Lock()

    def record(self, latency_ms: float) -> None:
        with self._lock:
            self._samples.append(latency_ms)

    def hedge_delay_ms(self, percentile: float = 75.0) -> float:
        with self._lock:
            if len(self._samples) < 10:
                return self._default
            sorted_samples = sorted(self._samples)
            idx = min(int(len(sorted_samples) * percentile / 100.0), len(sorted_samples) - 1)
            return round(sorted_samples[idx], 1)

    def summary(self) -> dict:
        with self._lock:
            if not self._samples:
                return {"samples": 0}
            s = sorted(self._samples)
            n = len(s)
            return {
                "samples": n,
                "p50_ms": round(s[n // 2], 1),
                "p75_ms": round(s[min(int(n * 0.75), n - 1)], 1),
                "p95_ms": round(s[min(int(n * 0.95), n - 1)], 1),
                "current_hedge_delay_ms": self.hedge_delay_ms(),
            }
```

## Solution 4: Hedged LLM Caller

```python
import asyncio
import time
from typing import Any, Callable, Optional


class HedgedLLMCaller:
    """
    Executes an LLM call with request hedging. Launches the primary request
    immediately and a hedge request after hedge_delay_ms if the primary has
    not yet responded. Returns whichever arrives first and cancels the other.
    """

    def __init__(
        self,
        policy: HedgePolicy,
        delay_estimator: Optional[AdaptiveHedgeDelayEstimator] = None,
    ):
        self._policy = policy
        self._estimator = delay_estimator
        self._total_calls = 0
        self._hedges_launched = 0
        self._hedge_wins = 0

    async def call(
        self,
        primary_fn: Callable,       # async () -> response
        hedge_fn: Optional[Callable] = None,  # async () -> response; defaults to primary_fn
        *,
        args: tuple = (),
        kwargs: dict = None,
    ) -> HedgedRequestResult:
        kwargs = kwargs or {}
        if not self._policy.enabled:
            start = time.time()
            response = await primary_fn(*args, **kwargs)
            lat = round((time.time() - start) * 1000, 2)
            if self._estimator:
                self._estimator.record(lat)
            return HedgedRequestResult(
                outcome=HedgeOutcome.HEDGING_DISABLED,
                response=response,
                primary_latency_ms=lat,
                hedge_latency_ms=None,
                hedge_launched=False,
                latency_saved_ms=None,
            )

        hedge_fn = hedge_fn or primary_fn
        delay_ms = (
            self._estimator.hedge_delay_ms() if self._estimator
            else self._policy.hedge_delay_ms
        )
        timeout_s = self._policy.timeout_s()
        self._total_calls += 1

        primary_start = time.time()
        primary_task = asyncio.create_task(primary_fn(*args, **kwargs))

        hedge_task: Optional[asyncio.Task] = None
        hedge_start: Optional[float] = None

        try:
            # Wait for primary up to hedge delay
            try:
                result = await asyncio.wait_for(
                    asyncio.shield(primary_task), timeout=delay_ms / 1000.0
                )
                lat = round((time.time() - primary_start) * 1000, 2)
                if self._estimator:
                    self._estimator.record(lat)
                return HedgedRequestResult(
                    outcome=HedgeOutcome.NO_HEDGE_NEEDED,
                    response=result,
                    primary_latency_ms=lat,
                    hedge_latency_ms=None,
                    hedge_launched=False,
                    latency_saved_ms=None,
                )
            except asyncio.TimeoutError:
                pass  # Primary is slow — launch hedge

            # Launch hedge
            self._hedges_launched += 1
            hedge_start = time.time()
            hedge_task = asyncio.create_task(hedge_fn(*args, **kwargs))

            # Race both to completion
            remaining_timeout = timeout_s - (time.time() - primary_start)
            done, pending = await asyncio.wait(
                [primary_task, hedge_task],
                timeout=max(remaining_timeout, 0.1),
                return_when=asyncio.FIRST_COMPLETED,
            )

            # Cancel losers
            for task in pending:
                task.cancel()

            if not done:
                raise asyncio.TimeoutError("both requests timed out")

            winner = next(iter(done))
            response = winner.result()
            now = time.time()
            primary_lat = round((now - primary_start) * 1000, 2)
            hedge_lat = round((now - hedge_start) * 1000, 2) if hedge_start else None

            if winner is primary_task:
                outcome = HedgeOutcome.PRIMARY_WON
            else:
                outcome = HedgeOutcome.HEDGE_WON
                self._hedge_wins += 1

            loser_lat = hedge_lat if winner is primary_task else primary_lat
            saved = round(loser_lat - min(primary_lat, hedge_lat or primary_lat), 2) if loser_lat else None

            if self._estimator:
                self._estimator.record(primary_lat)

            return HedgedRequestResult(
                outcome=outcome,
                response=response,
                primary_latency_ms=primary_lat,
                hedge_latency_ms=hedge_lat,
                hedge_launched=True,
                latency_saved_ms=saved,
            )

        except Exception as exc:
            for t in [primary_task, hedge_task]:
                if t and not t.done():
                    t.cancel()
            return HedgedRequestResult(
                outcome=HedgeOutcome.BOTH_FAILED,
                response=None,
                primary_latency_ms=None,
                hedge_latency_ms=None,
                hedge_launched=hedge_task is not None,
                latency_saved_ms=None,
                error=str(exc)[:300],
            )

    def stats(self) -> dict:
        return {
            "total_calls": self._total_calls,
            "hedges_launched": self._hedges_launched,
            "hedge_rate": round(self._hedges_launched / max(self._total_calls, 1), 4),
            "hedge_wins": self._hedge_wins,
            "hedge_win_rate": round(self._hedge_wins / max(self._hedges_launched, 1), 4),
        }
```

## Solution 5: Hedge Savings Tracker

```python
import time
from threading import Lock
from typing import List, Optional, Tuple


class HedgeSavingsTracker:
    """
    Accumulates latency savings from hedge wins to quantify the
    tail-latency improvement from hedging over time.
    """

    def __init__(self, max_records: int = 10000):
        self._records: List[Tuple[float, HedgeOutcome, Optional[float]]] = []
        # (ts, outcome, latency_saved_ms)
        self._lock = Lock()
        self._max = max_records

    def record(self, result: HedgedRequestResult) -> None:
        with self._lock:
            self._records.append((time.time(), result.outcome, result.latency_saved_ms))
            if len(self._records) > self._max:
                self._records = self._records[-self._max // 2:]

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        with self._lock:
            recent = [(ts, o, s) for ts, o, s in self._records if ts >= cutoff]

        if not recent:
            return {"window_seconds": window_seconds, "calls": 0}

        total = len(recent)
        hedge_wins = sum(1 for _, o, _ in recent if o == HedgeOutcome.HEDGE_WON)
        savings = [s for _, o, s in recent if o == HedgeOutcome.HEDGE_WON and s is not None]
        mean_saved = round(sum(savings) / len(savings), 2) if savings else 0.0

        return {
            "window_seconds": window_seconds,
            "calls": total,
            "hedge_wins": hedge_wins,
            "hedge_win_rate": round(hedge_wins / max(total, 1), 4),
            "mean_latency_saved_ms": mean_saved,
            "total_latency_saved_ms": round(sum(savings), 2),
        }
```

## Solution 6: Hedging Dashboard

```python
import time


class HedgingDashboard:
    """
    Combines caller stats, delay estimator, and savings tracker
    into a single operational view for hedging configuration and monitoring.
    """

    def __init__(
        self,
        caller: HedgedLLMCaller,
        estimator: AdaptiveHedgeDelayEstimator,
        savings: HedgeSavingsTracker,
        policy: HedgePolicy,
    ):
        self._caller = caller
        self._estimator = estimator
        self._savings = savings
        self._policy = policy

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "policy": {
                "enabled": self._policy.enabled,
                "configured_delay_ms": self._policy.hedge_delay_ms,
                "timeout_ms": self._policy.timeout_ms,
            },
            "adaptive_delay": self._estimator.summary(),
            "caller_stats": self._caller.stats(),
            "savings_last_hour": self._savings.summary(window_seconds=3600.0),
        }
```

## Comparison

| Approach | Hedge Launch | Adaptive Delay | Cancel Loser | Savings Tracking | Dashboard |
|---|---|---|---|---|---|
| HedgedLLMCaller | Yes (asyncio race) | Via estimator | Yes | No | No |
| AdaptiveHedgeDelayEstimator | No | Yes (P75) | No | No | No |
| HedgeSavingsTracker | No | No | No | Yes | No |
| HedgingDashboard | No | No | No | No | Yes |

**Best for production**: Only hedge idempotent calls — LLM completions are safe; tool calls with side effects are not. Set the hedge delay to the observed P75 latency using `AdaptiveHedgeDelayEstimator` — this fires a hedge on the slowest ~25% of requests and is largely invisible on the other 75%. Cap `max_hedge_requests=1`; hedging more than once multiplies API cost without proportional latency gain. Monitor `hedge_win_rate` — above 30% means the primary endpoint is unreliable and the hedge model or endpoint should be promoted to primary. Below 5% means the hedge delay is too long and can be reduced.
