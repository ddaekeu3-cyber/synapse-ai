---
title: "Agent Doesn't Implement Adaptive Polling for Long-Running Tool Operations"
description: "Agents that either block on long-running tool operations or poll at a fixed interval waste resources and introduce unnecessary latency: blocking ties up the event loop, and fixed-interval polling at 1-second intervals generates 60 unnecessary requests for a 60-second operation. Implement adaptive polling that starts with short intervals, backs off exponentially as the operation continues, and caps polling frequency to avoid upstream quota consumption."
date: 2026-04-16
difficulty: intermediate
category: reliability
slug: agent-doesnt-implement-adaptive-polling-for-long-running-tool-operations
tags: [adaptive-polling, long-running-operations, async-polling, exponential-backoff, job-status, operation-tracking]
symptoms:
  - "Agent blocks the event loop for 30+ seconds waiting for a batch job tool to complete"
  - "Fixed 1-second polling generates thousands of status-check requests for a slow operation"
  - "No distinction between operations that typically finish in 2 seconds vs 2 minutes"
  - "Polling interval is hardcoded rather than adapted to the observed operation duration"
  - "Agent times out on long operations because polling is abandoned after a fixed attempt count"
---

## Why This Happens

Long-running operations — batch processing, database migrations, async job queues — complete on timescales that vary by orders of magnitude. Fixed-interval polling mismatches the operation's natural cadence: too frequent for slow operations (wastes quota), too slow for fast operations (adds latency). Adaptive polling starts with a short interval appropriate for fast completions, then backs off exponentially as the operation continues, converging on an interval proportional to the operation's observed duration. This minimizes both wasted requests and unnecessary latency.

## Solution 1: Polling Configuration

```python
from dataclasses import dataclass


@dataclass
class AdaptivePollingConfig:
    initial_interval_seconds: float = 0.5
    max_interval_seconds: float = 30.0
    backoff_multiplier: float = 1.5
    jitter_pct: float = 0.10          # add ±10% random jitter
    max_total_wait_seconds: float = 600.0   # 10-minute hard timeout
    fast_path_threshold_seconds: float = 2.0  # stay at initial interval for this long
```

## Solution 2: Adaptive Interval Calculator

```python
import random
import time
from typing import Optional


class AdaptiveIntervalCalculator:
    """
    Computes the next polling interval using exponential backoff with jitter.
    Stays at the initial interval during the fast-path window, then backs off.
    """

    def __init__(self, config: AdaptivePollingConfig):
        self._config = config

    def next_interval(
        self,
        current_interval: float,
        elapsed_seconds: float,
    ) -> float:
        cfg = self._config

        # Stay at initial interval during the fast-path window
        if elapsed_seconds < cfg.fast_path_threshold_seconds:
            interval = cfg.initial_interval_seconds
        else:
            interval = min(
                current_interval * cfg.backoff_multiplier,
                cfg.max_interval_seconds,
            )

        # Apply jitter
        jitter = interval * cfg.jitter_pct
        interval += random.uniform(-jitter, jitter)

        return max(cfg.initial_interval_seconds, interval)

    def is_timed_out(self, elapsed_seconds: float) -> bool:
        return elapsed_seconds >= self._config.max_total_wait_seconds
```

## Solution 3: Operation Status Protocol

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class OperationStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class OperationStatusResult:
    operation_id: str
    status: OperationStatus
    result: Optional[Any] = None
    error: Optional[str] = None
    progress_pct: Optional[float] = None
    metadata: dict = field(default_factory=dict)

    def is_terminal(self) -> bool:
        return self.status in (
            OperationStatus.COMPLETED,
            OperationStatus.FAILED,
            OperationStatus.CANCELLED,
        )
```

## Solution 4: Adaptive Poller

```python
import asyncio
import time
from typing import Callable, Optional


class AdaptivePoller:
    """
    Polls a status check function with adaptive intervals until
    the operation reaches a terminal state or the timeout is exceeded.
    Emits progress events at each poll cycle.
    """

    def __init__(
        self,
        config: AdaptivePollingConfig,
        calculator: AdaptiveIntervalCalculator,
    ):
        self._config = config
        self._calculator = calculator

    async def poll(
        self,
        operation_id: str,
        check_fn: Callable[[str], OperationStatusResult],
        progress_fn: Optional[Callable[[OperationStatusResult, int, float], None]] = None,
    ) -> dict:
        start = time.time()
        interval = self._config.initial_interval_seconds
        poll_count = 0
        last_status: Optional[OperationStatusResult] = None

        while True:
            elapsed = time.time() - start

            if self._calculator.is_timed_out(elapsed):
                return {
                    "operation_id": operation_id,
                    "outcome": "timeout",
                    "elapsed_seconds": round(elapsed, 2),
                    "poll_count": poll_count,
                    "last_status": last_status,
                }

            status = await check_fn(operation_id)
            poll_count += 1
            last_status = status

            if progress_fn:
                progress_fn(status, poll_count, elapsed)

            if status.is_terminal():
                return {
                    "operation_id": operation_id,
                    "outcome": status.status.value,
                    "result": status.result,
                    "error": status.error,
                    "elapsed_seconds": round(elapsed, 2),
                    "poll_count": poll_count,
                    "final_interval_seconds": round(interval, 2),
                }

            interval = self._calculator.next_interval(interval, elapsed)
            await asyncio.sleep(interval)
```

## Solution 5: Polling History Tracker

```python
import time
from typing import List


class PollingHistoryTracker:
    """
    Records completed polling sessions to derive optimal initial
    intervals for different operation types from historical durations.
    """

    def __init__(self):
        self._records: List[dict] = []

    def record(self, poll_result: dict, operation_type: str = "") -> None:
        self._records.append({
            **poll_result,
            "operation_type": operation_type,
            "recorded_at": time.time(),
        })

    def median_duration(self, operation_type: str = "") -> float:
        matching = [
            r["elapsed_seconds"] for r in self._records
            if (not operation_type or r.get("operation_type") == operation_type)
            and r.get("outcome") == "completed"
        ]
        if not matching:
            return 0.0
        sorted_m = sorted(matching)
        return sorted_m[len(sorted_m) // 2]

    def suggested_initial_interval(
        self,
        operation_type: str = "",
        divisor: float = 10.0,
    ) -> float:
        median = self.median_duration(operation_type)
        if median <= 0:
            return 0.5
        return max(0.1, min(median / divisor, 5.0))

    def stats(self) -> dict:
        total = len(self._records)
        completed = sum(1 for r in self._records if r.get("outcome") == "completed")
        timed_out = sum(1 for r in self._records if r.get("outcome") == "timeout")
        avg_polls = (
            sum(r.get("poll_count", 0) for r in self._records) / max(total, 1)
        )
        return {
            "total_operations": total,
            "completed": completed,
            "timed_out": timed_out,
            "avg_poll_count": round(avg_polls, 1),
        }
```

## Solution 6: Adaptive Polling Dashboard

```python
import time


class AdaptivePollingDashboard:
    """
    Surfaces polling efficiency metrics, timeout rates, and
    per-operation-type duration profiles.
    """

    def __init__(
        self,
        tracker: PollingHistoryTracker,
        config: AdaptivePollingConfig,
    ):
        self._tracker = tracker
        self._config = config

    def render(self) -> dict:
        stats = self._tracker.stats()
        return {
            "generated_at": time.time(),
            "config": {
                "initial_interval_s": self._config.initial_interval_seconds,
                "max_interval_s": self._config.max_interval_seconds,
                "backoff_multiplier": self._config.backoff_multiplier,
                "max_wait_s": self._config.max_total_wait_seconds,
            },
            "stats": stats,
            "timeout_rate": round(
                stats["timed_out"] / max(stats["total_operations"], 1), 4
            ),
        }
```

## Comparison

| Approach | Adaptive Intervals | Timeout Enforcement | Progress Events | History Tracking | Dashboard |
|---|---|---|---|---|---|
| AdaptiveIntervalCalculator | Yes (backoff+jitter) | Via is_timed_out | No | No | No |
| AdaptivePoller | Via calculator | Yes | Yes (callback) | No | No |
| PollingHistoryTracker | No | No | No | Yes (duration) | No |
| AdaptivePollingDashboard | No | No | No | Via tracker | Yes |

**Best for production**: Set `fast_path_threshold_seconds=2.0` so operations that typically complete quickly are polled aggressively for the first 2 seconds without backoff — this minimizes latency for the common case. Use `PollingHistoryTracker.suggested_initial_interval()` to auto-tune `initial_interval_seconds` per operation type: if a batch job historically takes 45 seconds, the suggested initial interval is ~4.5 seconds, avoiding 45 unnecessary sub-second polls. Set `max_total_wait_seconds` to the operation's SLA timeout, not to a fixed value — a 10-minute SLA requires a 10-minute poller, not the 30-second default used for fast operations.
