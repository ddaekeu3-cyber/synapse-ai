---
title: "Agent Doesn't Implement Adaptive Polling Backoff for Long-Running Tool Calls"
description: "Agents that poll async tool status endpoints at a fixed interval — typically every second — generate unnecessary API traffic and exhaust rate limits when tools take minutes to complete. Implement adaptive polling backoff that starts with short intervals, increases exponentially as the task ages, and adds jitter to prevent thundering herds when multiple agents poll the same endpoint simultaneously."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-adaptive-polling-backoff-for-long-running-tool-calls
tags: [polling, backoff, long-running-tools, rate-limiting, async-tools, adaptive-interval]
symptoms:
  - "Agent polls tool status endpoint every second for a 10-minute task — 600 unnecessary requests"
  - "Rate limit errors from status endpoint during high-concurrency polling"
  - "Fixed polling interval ignores how long the task has already been running"
  - "Multiple agents polling the same job ID at the same second cause burst traffic"
  - "No mechanism to give up after a maximum poll duration"
---

## Why This Happens

Long-running tools — batch inference jobs, data processing pipelines, external API calls with queued execution — are typically queried via a status endpoint. Agents default to polling frequently because they were designed for fast tool calls. A 1-second polling interval is reasonable for a 5-second task but wasteful for a task that takes 10 minutes. Adaptive backoff matches polling frequency to the expected remaining duration: poll quickly at first (the task might finish fast), then slow down as time passes (the task clearly takes longer), and add jitter to spread load when many agents are polling concurrently.

## Solution 1: Polling Backoff Config

```python
from dataclasses import dataclass


@dataclass
class AdaptivePollingConfig:
    initial_interval_seconds: float = 0.5     # first poll interval
    max_interval_seconds: float = 60.0        # ceiling on poll interval
    backoff_multiplier: float = 1.5           # multiply interval after each poll
    jitter_fraction: float = 0.2             # ±20% random jitter
    max_poll_duration_seconds: float = 1800.0  # give up after 30 minutes
    fast_phase_duration_seconds: float = 10.0  # stay at initial interval for first N seconds
    fast_phase_interval_seconds: float = 0.5   # interval during fast phase
```

## Solution 2: Adaptive Interval Calculator

```python
import random
import time
from typing import Optional


class AdaptiveIntervalCalculator:
    """
    Computes the next poll interval based on how long polling has been active.
    Uses a fast phase (short intervals) followed by exponential backoff with jitter.
    """

    def __init__(self, config: AdaptivePollingConfig):
        self._cfg = config

    def next_interval(self, elapsed_seconds: float, poll_count: int) -> float:
        cfg = self._cfg

        # Fast phase: stay at initial interval for first N seconds
        if elapsed_seconds < cfg.fast_phase_duration_seconds:
            interval = cfg.fast_phase_interval_seconds
        else:
            # Exponential backoff based on polls after fast phase
            backoff_polls = max(0, poll_count - int(
                cfg.fast_phase_duration_seconds / max(cfg.fast_phase_interval_seconds, 0.01)
            ))
            interval = min(
                cfg.initial_interval_seconds * (cfg.backoff_multiplier ** backoff_polls),
                cfg.max_interval_seconds,
            )

        # Apply jitter: ±jitter_fraction
        jitter = interval * cfg.jitter_fraction * (2 * random.random() - 1)
        return max(0.1, interval + jitter)

    def is_timed_out(self, elapsed_seconds: float) -> bool:
        return elapsed_seconds >= self._cfg.max_poll_duration_seconds
```

## Solution 3: Async Adaptive Poller

```python
import asyncio
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Optional


class PollOutcome(str, Enum):
    COMPLETED = "completed"
    FAILED = "failed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"


@dataclass
class PollResult:
    outcome: PollOutcome
    final_status: Optional[Any]
    poll_count: int
    elapsed_seconds: float
    total_wait_seconds: float


class AsyncAdaptivePoller:
    """
    Polls an async status function with adaptive backoff until
    the job completes, fails, or the maximum duration is reached.
    """

    def __init__(
        self,
        config: AdaptivePollingConfig,
        interval_calculator: AdaptiveIntervalCalculator,
    ):
        self._cfg = config
        self._calc = interval_calculator

    async def poll(
        self,
        status_fn: Callable,           # async fn() -> dict with {"status": str, ...}
        is_complete_fn: Callable,      # fn(status_dict) -> bool
        is_failed_fn: Callable,        # fn(status_dict) -> bool
        job_id: str = "",
    ) -> PollResult:
        start = time.time()
        poll_count = 0
        total_wait = 0.0
        last_status = None

        while True:
            elapsed = time.time() - start

            if self._calc.is_timed_out(elapsed):
                return PollResult(
                    outcome=PollOutcome.TIMEOUT,
                    final_status=last_status,
                    poll_count=poll_count,
                    elapsed_seconds=round(elapsed, 2),
                    total_wait_seconds=round(total_wait, 2),
                )

            try:
                status = await status_fn()
                last_status = status
                poll_count += 1
            except Exception:
                poll_count += 1
                interval = self._calc.next_interval(elapsed, poll_count)
                await asyncio.sleep(interval)
                total_wait += interval
                continue

            if is_complete_fn(status):
                return PollResult(
                    outcome=PollOutcome.COMPLETED,
                    final_status=status,
                    poll_count=poll_count,
                    elapsed_seconds=round(time.time() - start, 2),
                    total_wait_seconds=round(total_wait, 2),
                )

            if is_failed_fn(status):
                return PollResult(
                    outcome=PollOutcome.FAILED,
                    final_status=status,
                    poll_count=poll_count,
                    elapsed_seconds=round(time.time() - start, 2),
                    total_wait_seconds=round(total_wait, 2),
                )

            interval = self._calc.next_interval(elapsed, poll_count)
            await asyncio.sleep(interval)
            total_wait += interval
```

## Solution 4: Polling Interval Trace

```python
import time
from typing import List


class PollingIntervalTrace:
    """
    Records each poll's interval and elapsed time for a single job.
    Used to verify that backoff is working correctly and to surface
    jobs that are consuming disproportionate polling traffic.
    """

    def __init__(self, job_id: str):
        self.job_id = job_id
        self.started_at = time.time()
        self._intervals: List[float] = []
        self._poll_times: List[float] = []

    def record_poll(self, interval_seconds: float) -> None:
        self._intervals.append(interval_seconds)
        self._poll_times.append(time.time() - self.started_at)

    def summary(self) -> dict:
        if not self._intervals:
            return {"job_id": self.job_id, "polls": 0}
        return {
            "job_id": self.job_id,
            "polls": len(self._intervals),
            "elapsed_seconds": round(self._poll_times[-1], 2) if self._poll_times else 0,
            "min_interval_seconds": round(min(self._intervals), 3),
            "max_interval_seconds": round(max(self._intervals), 3),
            "avg_interval_seconds": round(sum(self._intervals) / len(self._intervals), 3),
            "total_wait_seconds": round(sum(self._intervals), 2),
        }
```

## Solution 5: Multi-Job Polling Coordinator

```python
import asyncio
from typing import Any, Callable, Dict, List, Optional


class MultiJobPollingCoordinator:
    """
    Manages adaptive polling for multiple concurrent long-running jobs.
    Each job gets its own poller instance with independent backoff state
    to prevent one slow job from affecting polling cadence of others.
    """

    def __init__(
        self,
        config: AdaptivePollingConfig,
        max_concurrent_polls: int = 20,
    ):
        self._cfg = config
        self._semaphore = asyncio.Semaphore(max_concurrent_polls)
        self._active: Dict[str, asyncio.Task] = {}

    async def submit(
        self,
        job_id: str,
        status_fn: Callable,
        is_complete_fn: Callable,
        is_failed_fn: Callable,
    ) -> PollResult:
        calc = AdaptiveIntervalCalculator(self._cfg)
        poller = AsyncAdaptivePoller(self._cfg, calc)

        async def _bounded_poll() -> PollResult:
            async with self._semaphore:
                return await poller.poll(status_fn, is_complete_fn, is_failed_fn, job_id)

        task = asyncio.create_task(_bounded_poll())
        self._active[job_id] = task
        try:
            result = await task
        finally:
            self._active.pop(job_id, None)
        return result

    def active_job_count(self) -> int:
        return len(self._active)

    def cancel_job(self, job_id: str) -> bool:
        task = self._active.get(job_id)
        if task:
            task.cancel()
            return True
        return False
```

## Solution 6: Polling Efficiency Dashboard

```python
import time
from typing import List


class PollingEfficiencyDashboard:
    """
    Accumulates poll results and surfaces request savings from
    adaptive backoff compared to fixed-interval polling.
    """

    def __init__(self, fixed_interval_baseline_seconds: float = 1.0):
        self._baseline = fixed_interval_baseline_seconds
        self._results: List[PollResult] = []
        self._recorded_at: List[float] = []

    def record(self, result: PollResult) -> None:
        self._results.append(result)
        self._recorded_at.append(time.time())

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [
            r for r, ts in zip(self._results, self._recorded_at) if ts >= cutoff
        ]
        if not recent:
            return {"window_seconds": window_seconds, "jobs": 0}

        total_polls = sum(r.poll_count for r in recent)
        total_elapsed = sum(r.elapsed_seconds for r in recent)
        fixed_polls_equivalent = int(total_elapsed / self._baseline)
        polls_saved = max(0, fixed_polls_equivalent - total_polls)

        timeouts = sum(1 for r in recent if r.outcome == PollOutcome.TIMEOUT)

        return {
            "window_seconds": window_seconds,
            "jobs": len(recent),
            "total_polls": total_polls,
            "fixed_interval_polls_equivalent": fixed_polls_equivalent,
            "polls_saved": polls_saved,
            "savings_pct": round(polls_saved / max(fixed_polls_equivalent, 1) * 100, 1),
            "timeout_jobs": timeouts,
            "avg_polls_per_job": round(total_polls / len(recent), 1),
        }
```

## Comparison

| Approach | Adaptive Intervals | Jitter | Multi-Job | Timeout | Savings Tracking |
|---|---|---|---|---|---|
| AdaptiveIntervalCalculator | Yes (fast+backoff) | Yes | No | Via config | No |
| AsyncAdaptivePoller | Via calculator | Via calculator | No | Yes | No |
| PollingIntervalTrace | No | No | No | No | Per-job trace |
| MultiJobPollingCoordinator | Via poller | Via poller | Yes | Via poller | No |
| PollingEfficiencyDashboard | No | No | No | No | Yes (aggregate) |

**Best for production**: Set `fast_phase_duration_seconds=10` and `fast_phase_interval_seconds=0.5` — most tool calls complete within 10 seconds, so staying aggressive during this window adds minimal overhead. After the fast phase, use `backoff_multiplier=1.5` with `max_interval_seconds=60`: this reaches 60-second intervals after roughly 10 backoff steps, appropriate for batch jobs measured in minutes. Set `jitter_fraction=0.25` when more than 10 agents might poll the same job — without jitter, all agents poll at identical times and create burst traffic that can trigger rate limiting on the status endpoint. Monitor `timeout_jobs` in `PollingEfficiencyDashboard`: jobs that consistently hit `max_poll_duration_seconds` indicate the tool is silently stuck and needs a separate health check, not just longer polling.
