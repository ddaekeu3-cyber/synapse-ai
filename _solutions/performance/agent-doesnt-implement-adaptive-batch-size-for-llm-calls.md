---
title: "Agent Doesn't Implement Adaptive Batch Size for LLM Calls"
description: "Agents that use a fixed batch size for LLM calls either under-utilize throughput (batch too small) or trigger rate limit errors and context window overflows (batch too large). Implement adaptive batch sizing that adjusts based on observed request latency, error rates, and token counts, converging on the optimal batch size for current load conditions."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-adaptive-batch-size-for-llm-calls
tags: [adaptive-batching, batch-size, llm-throughput, rate-limiting, token-budget, dynamic-batching]
symptoms:
  - "Fixed batch size of 10 causes rate limit errors during peak hours"
  - "Fixed batch size of 2 under-utilizes API throughput during off-peak hours"
  - "Batch token counts vary widely — some batches hit context limits, others use 10%"
  - "No mechanism to increase batch size after a period of successful calls"
  - "Batch errors require manual tuning — no automatic recovery to a safe size"
---

## Why This Happens

LLM API throughput is constrained by requests-per-minute (RPM) and tokens-per-minute (TPM) limits, which vary by model tier and time of day. A fixed batch size is a compromise that performs poorly at both extremes: too large causes 429 errors and context overflows, too small wastes throughput. Adaptive batch sizing tracks the success rate and latency of recent batches and adjusts the batch size using a multiplicative increase / additive decrease (MIMD-inspired) algorithm — growing quickly when conditions are good, backing off conservatively when errors occur.

## Solution 1: Batch Size State

```python
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Deque, List, Optional
from collections import deque


class BatchOutcome(str, Enum):
    SUCCESS = "success"
    RATE_LIMITED = "rate_limited"
    CONTEXT_OVERFLOW = "context_overflow"
    TIMEOUT = "timeout"
    OTHER_ERROR = "other_error"


@dataclass
class BatchExecutionRecord:
    batch_size: int
    token_count: int
    outcome: BatchOutcome
    latency_ms: float
    timestamp: float = field(default_factory=time.time)

    @property
    def succeeded(self) -> bool:
        return self.outcome == BatchOutcome.SUCCESS


@dataclass
class AdaptiveBatchState:
    current_size: int
    min_size: int
    max_size: int
    target_latency_ms: float = 5000.0
    increase_factor: float = 1.25      # multiply on success
    decrease_factor: float = 0.5       # multiply on error (additive decrease)
    token_budget_per_batch: int = 50_000
    consecutive_successes: int = 0
    consecutive_errors: int = 0
    last_adjusted_at: float = field(default_factory=time.time)
```

## Solution 2: Adaptive Batch Size Controller

```python
import time
from collections import deque
from threading import Lock
from typing import Deque, Optional


class AdaptiveBatchSizeController:
    """
    Adjusts batch size based on observed outcomes using a MIMD-inspired algorithm:
    - Multiplicative increase on sustained success
    - Multiplicative decrease on errors
    - Target-latency adjustment when latency deviates from target
    """

    def __init__(
        self,
        initial_size: int = 5,
        min_size: int = 1,
        max_size: int = 50,
        window_size: int = 20,
        success_threshold: int = 3,     # consecutive successes before increasing
        target_latency_ms: float = 3000.0,
    ):
        self._state = AdaptiveBatchState(
            current_size=initial_size,
            min_size=min_size,
            max_size=max_size,
            target_latency_ms=target_latency_ms,
        )
        self._history: Deque[BatchExecutionRecord] = deque(maxlen=window_size)
        self._lock = Lock()
        self._success_threshold = success_threshold

    def record_outcome(self, record: BatchExecutionRecord) -> None:
        with self._lock:
            self._history.append(record)
            self._adjust(record)

    def _adjust(self, record: BatchExecutionRecord) -> None:
        state = self._state

        if not record.succeeded:
            # Error: aggressive decrease
            state.consecutive_successes = 0
            state.consecutive_errors += 1

            if record.outcome == BatchOutcome.RATE_LIMITED:
                state.current_size = max(
                    state.min_size,
                    int(state.current_size * state.decrease_factor),
                )
            elif record.outcome == BatchOutcome.CONTEXT_OVERFLOW:
                # Context overflow: reduce by smaller amount, not rate limit
                state.current_size = max(
                    state.min_size,
                    state.current_size - 1,
                )
            else:
                state.current_size = max(
                    state.min_size,
                    int(state.current_size * state.decrease_factor),
                )
        else:
            state.consecutive_errors = 0
            state.consecutive_successes += 1

            # Latency-based adjustment
            if record.latency_ms > state.target_latency_ms * 1.5:
                # Too slow — reduce size
                state.current_size = max(
                    state.min_size,
                    state.current_size - 1,
                )
                state.consecutive_successes = 0
            elif state.consecutive_successes >= self._success_threshold:
                # Sustained success — increase size
                state.current_size = min(
                    state.max_size,
                    int(state.current_size * state.increase_factor),
                )
                state.consecutive_successes = 0

        state.last_adjusted_at = time.time()

    @property
    def current_size(self) -> int:
        with self._lock:
            return self._state.current_size

    def recent_success_rate(self) -> float:
        with self._lock:
            records = list(self._history)
        if not records:
            return 1.0
        return sum(1 for r in records if r.succeeded) / len(records)

    def summary(self) -> dict:
        with self._lock:
            state = self._state
            records = list(self._history)

        return {
            "current_batch_size": state.current_size,
            "min_size": state.min_size,
            "max_size": state.max_size,
            "consecutive_successes": state.consecutive_successes,
            "consecutive_errors": state.consecutive_errors,
            "recent_success_rate": round(
                sum(1 for r in records if r.succeeded) / max(len(records), 1), 3
            ),
            "avg_latency_ms": round(
                sum(r.latency_ms for r in records) / max(len(records), 1), 2
            ),
        }
```

## Solution 3: Token Budget-Aware Batch Builder

```python
from typing import Any, List, Optional, Tuple


class TokenBudgetAwareBatchBuilder:
    """
    Builds batches that respect both a maximum item count (from the adaptive
    controller) and a maximum token budget per batch.
    """

    def __init__(
        self,
        controller: AdaptiveBatchSizeController,
        max_tokens_per_batch: int = 50_000,
        token_estimator: Optional[callable] = None,
    ):
        self._controller = controller
        self._max_tokens = max_tokens_per_batch
        self._estimate_tokens = token_estimator or (lambda item: len(str(item)) // 4)

    def build_batches(self, items: List[Any]) -> List[Tuple[List[Any], int]]:
        """
        Returns list of (batch_items, estimated_tokens) tuples.
        """
        max_size = self._controller.current_size
        batches = []
        current_batch: List[Any] = []
        current_tokens = 0

        for item in items:
            item_tokens = self._estimate_tokens(item)

            if (
                current_batch
                and (
                    len(current_batch) >= max_size
                    or current_tokens + item_tokens > self._max_tokens
                )
            ):
                batches.append((current_batch, current_tokens))
                current_batch = []
                current_tokens = 0

            current_batch.append(item)
            current_tokens += item_tokens

        if current_batch:
            batches.append((current_batch, current_tokens))

        return batches
```

## Solution 4: Adaptive Batch Executor

```python
import asyncio
import time
from typing import Any, Callable, List, Tuple


class AdaptiveBatchExecutor:
    """
    Executes batches using the adaptive controller, records outcomes,
    and retries with smaller batches on recoverable errors.
    """

    def __init__(
        self,
        controller: AdaptiveBatchSizeController,
        batch_fn: Callable[[List[Any]], Any],
        max_retries: int = 2,
    ):
        self._controller = controller
        self._batch_fn = batch_fn
        self._max_retries = max_retries

    async def execute_batch(
        self,
        items: List[Any],
        token_count: int = 0,
    ) -> Tuple[Any, BatchOutcome]:
        start = time.time()
        outcome = BatchOutcome.OTHER_ERROR
        result = None

        for attempt in range(self._max_retries + 1):
            try:
                if asyncio.iscoroutinefunction(self._batch_fn):
                    result = await self._batch_fn(items)
                else:
                    result = await asyncio.to_thread(self._batch_fn, items)
                outcome = BatchOutcome.SUCCESS
                break
            except Exception as exc:
                error_str = str(exc).lower()
                if "rate limit" in error_str or "429" in error_str:
                    outcome = BatchOutcome.RATE_LIMITED
                    await asyncio.sleep(2 ** attempt)
                elif "context" in error_str or "token" in error_str:
                    outcome = BatchOutcome.CONTEXT_OVERFLOW
                    break
                elif "timeout" in error_str:
                    outcome = BatchOutcome.TIMEOUT
                else:
                    outcome = BatchOutcome.OTHER_ERROR
                    break

        latency_ms = (time.time() - start) * 1000
        self._controller.record_outcome(BatchExecutionRecord(
            batch_size=len(items),
            token_count=token_count,
            outcome=outcome,
            latency_ms=round(latency_ms, 2),
        ))
        return result, outcome
```

## Solution 5: Batch Size History Tracker

```python
import time
from threading import Lock
from typing import List


class BatchSizeHistoryTracker:
    """
    Records batch size over time to visualize adaptation behavior
    and identify oscillation or stuck states.
    """

    def __init__(self, max_records: int = 1000):
        self._records: List[dict] = []
        self._max = max_records
        self._lock = Lock()

    def record(self, batch_size: int, outcome: BatchOutcome) -> None:
        with self._lock:
            self._records.append({
                "ts": time.time(),
                "batch_size": batch_size,
                "outcome": outcome.value,
            })
            if len(self._records) > self._max:
                self._records.pop(0)

    def size_trajectory(self, last_n: int = 50) -> List[dict]:
        with self._lock:
            return list(self._records[-last_n:])

    def is_oscillating(self, window: int = 10) -> bool:
        """Detects rapid size oscillation — alternating increase/decrease."""
        with self._lock:
            recent = [r["batch_size"] for r in self._records[-window:]]
        if len(recent) < window:
            return False
        changes = [recent[i + 1] - recent[i] for i in range(len(recent) - 1)]
        sign_changes = sum(
            1 for i in range(len(changes) - 1)
            if changes[i] * changes[i + 1] < 0
        )
        return sign_changes > window // 2
```

## Solution 6: Adaptive Batch Dashboard

```python
import time


class AdaptiveBatchDashboard:
    """
    Operational view of batch size adaptation: current size, success rate,
    trajectory, and oscillation detection.
    """

    def __init__(
        self,
        controller: AdaptiveBatchSizeController,
        history: BatchSizeHistoryTracker,
    ):
        self._controller = controller
        self._history = history

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "controller_summary": self._controller.summary(),
            "is_oscillating": self._history.is_oscillating(),
            "recent_trajectory": self._history.size_trajectory(last_n=20),
        }
```

## Comparison

| Approach | Size Adjustment | Error Classification | Token Budget | Oscillation Detection | Dashboard |
|---|---|---|---|---|---|
| AdaptiveBatchSizeController | Yes (MIMD) | Via outcome type | No | No | No |
| TokenBudgetAwareBatchBuilder | Via controller | No | Yes | No | No |
| AdaptiveBatchExecutor | Via controller | Yes (exception parse) | No | No | No |
| BatchSizeHistoryTracker | No | No | No | Yes | No |
| AdaptiveBatchDashboard | No | No | No | No | Yes |

**Best for production**: Start with `initial_size=5` and let the controller converge — do not start at the maximum. Set `success_threshold=3` to require three consecutive successes before increasing: this prevents premature growth after a single lucky batch. Treat rate-limit errors and context overflows differently — rate limits call for an aggressive halving plus exponential backoff; context overflows call for a conservative reduction by 1 (the item count is the problem, not the rate). Detect oscillation via `BatchSizeHistoryTracker.is_oscillating()` and lock the batch size for 60 seconds when oscillation is detected — oscillation indicates the optimal size is between two values and thrashing wastes API calls.
