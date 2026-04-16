---
title: "Agent Doesn't Implement Dynamic Batch Sizing for LLM Requests"
description: "Agents that use a fixed batch size for embedding or completion requests either under-utilize throughput (batch too small) or trigger rate limits and timeouts (batch too small to absorb token variance). Implement dynamic batch sizing that adjusts batch dimensions based on real-time token estimates, provider rate limit headroom, and observed error rates to maximize throughput without exceeding quota."
date: 2026-04-16
difficulty: advanced
category: performance
slug: agent-doesnt-implement-dynamic-batch-sizing-for-llm-requests
tags: [dynamic-batching, batch-sizing, throughput-optimization, rate-limits, token-estimation, embedding-batching]
symptoms:
  - "Fixed batch size of 100 embeddings triggers 429s when document lengths vary widely"
  - "Batch size is hardcoded — no adjustment when provider rate limits change"
  - "Small batches under-utilize the provider's tokens-per-minute quota"
  - "No backpressure signal — batch size stays constant even when errors spike"
  - "Cannot adapt batch size between development (small quota) and production (large quota)"
---

## Why This Happens

A fixed batch size ignores two critical dimensions: token count and provider quota. A batch of 100 short documents may use 5,000 tokens while a batch of 100 long documents uses 150,000 tokens — both batches have the same item count but radically different resource consumption. Dynamic batch sizing estimates token cost before dispatching, checks available quota headroom, and adjusts the number of items per batch so that each dispatch stays within a configurable token budget regardless of input size variance.

## Solution 1: Batch Sizing Context

```python
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class BatchSizingContext:
    max_tokens_per_batch: int = 50_000
    max_items_per_batch: int = 200
    min_items_per_batch: int = 1
    target_utilization: float = 0.85      # target 85% of token budget per batch
    backoff_on_error: bool = True
    error_backoff_factor: float = 0.5     # halve batch size on error
    recovery_growth_factor: float = 1.2   # grow 20% per successful batch
    max_token_estimate_per_item: int = 8192


@dataclass
class BatchSizeState:
    current_max_items: int
    current_token_budget: int
    consecutive_errors: int = 0
    consecutive_successes: int = 0
    total_batches: int = 0
    total_items: int = 0
    total_tokens: int = 0
```

## Solution 2: Token Estimator

```python
from typing import Any, Dict, List, Union


class BatchTokenEstimator:
    """
    Estimates token count for a batch of items before submission.
    Uses character-to-token ratio heuristics by content type.
    """

    CHARS_PER_TOKEN = {
        "text": 4.0,
        "code": 3.5,
        "json": 3.0,
        "structured": 3.2,
    }
    DEFAULT_CHARS_PER_TOKEN = 4.0

    def estimate_item(
        self,
        item: Any,
        content_type: str = "text",
    ) -> int:
        ratio = self.CHARS_PER_TOKEN.get(content_type, self.DEFAULT_CHARS_PER_TOKEN)
        if isinstance(item, str):
            return max(1, int(len(item) / ratio))
        if isinstance(item, dict):
            import json
            return max(1, int(len(json.dumps(item)) / self.CHARS_PER_TOKEN["json"]))
        if isinstance(item, list):
            return sum(self.estimate_item(i, content_type) for i in item)
        return 10   # fallback for unknown types

    def estimate_batch(
        self,
        items: List[Any],
        content_type: str = "text",
    ) -> List[int]:
        return [self.estimate_item(item, content_type) for item in items]

    def total_tokens(self, estimates: List[int]) -> int:
        return sum(estimates)
```

## Solution 3: Dynamic Batch Sizer

```python
import time
from typing import Any, Iterator, List, Tuple


class DynamicBatchSizer:
    """
    Splits a list of items into optimally-sized batches based on
    per-item token estimates and current quota headroom.
    Adjusts batch size up or down based on recent error history.
    """

    def __init__(
        self,
        context: BatchSizingContext,
        estimator: BatchTokenEstimator,
    ) -> None:
        self._ctx = context
        self._estimator = estimator
        self._state = BatchSizeState(
            current_max_items=context.max_items_per_batch,
            current_token_budget=int(context.max_tokens_per_batch * context.target_utilization),
        )

    def record_success(self, items_sent: int, tokens_used: int) -> None:
        self._state.consecutive_errors = 0
        self._state.consecutive_successes += 1
        self._state.total_batches += 1
        self._state.total_items += items_sent
        self._state.total_tokens += tokens_used

        if self._state.consecutive_successes >= 3:
            new_max = int(self._state.current_max_items * self._ctx.recovery_growth_factor)
            self._state.current_max_items = min(new_max, self._ctx.max_items_per_batch)
            new_budget = int(self._state.current_token_budget * self._ctx.recovery_growth_factor)
            self._state.current_token_budget = min(
                new_budget,
                int(self._ctx.max_tokens_per_batch * self._ctx.target_utilization),
            )

    def record_error(self, is_rate_limit: bool = True) -> None:
        self._state.consecutive_successes = 0
        self._state.consecutive_errors += 1

        if self._ctx.backoff_on_error and is_rate_limit:
            new_max = max(
                self._ctx.min_items_per_batch,
                int(self._state.current_max_items * self._ctx.error_backoff_factor),
            )
            self._state.current_max_items = new_max
            self._state.current_token_budget = max(
                100,
                int(self._state.current_token_budget * self._ctx.error_backoff_factor),
            )

    def make_batches(
        self,
        items: List[Any],
        content_type: str = "text",
    ) -> List[List[Any]]:
        """Splits items into batches respecting current token budget and item limits."""
        if not items:
            return []

        estimates = self._estimator.estimate_batch(items, content_type)
        batches: List[List[Any]] = []
        current_batch: List[Any] = []
        current_tokens = 0

        for item, est in zip(items, estimates):
            item_tokens = min(est, self._ctx.max_token_estimate_per_item)
            would_exceed_tokens = current_tokens + item_tokens > self._state.current_token_budget
            would_exceed_items = len(current_batch) >= self._state.current_max_items

            if current_batch and (would_exceed_tokens or would_exceed_items):
                batches.append(current_batch)
                current_batch = []
                current_tokens = 0

            current_batch.append(item)
            current_tokens += item_tokens

        if current_batch:
            batches.append(current_batch)

        return batches

    def state_snapshot(self) -> dict:
        return {
            "current_max_items": self._state.current_max_items,
            "current_token_budget": self._state.current_token_budget,
            "consecutive_errors": self._state.consecutive_errors,
            "consecutive_successes": self._state.consecutive_successes,
            "total_batches": self._state.total_batches,
            "total_items": self._state.total_items,
            "avg_items_per_batch": round(
                self._state.total_items / max(self._state.total_batches, 1), 1
            ),
        }
```

## Solution 4: Adaptive Batch Dispatcher

```python
import asyncio
from typing import Any, Callable, List


class AdaptiveBatchDispatcher:
    """
    Dispatches batches produced by DynamicBatchSizer with retry logic.
    Records success/error outcomes to feed back into batch size adaptation.
    """

    def __init__(
        self,
        sizer: DynamicBatchSizer,
        batch_fn: Callable[[List[Any]], Any],   # async fn that processes one batch
        max_retries: int = 2,
        retry_delay_seconds: float = 1.0,
    ) -> None:
        self._sizer = sizer
        self._batch_fn = batch_fn
        self._max_retries = max_retries
        self._retry_delay = retry_delay_seconds

    async def dispatch_all(
        self,
        items: List[Any],
        content_type: str = "text",
    ) -> List[Any]:
        """Process all items in adaptive batches and return concatenated results."""
        batches = self._sizer.make_batches(items, content_type)
        all_results: List[Any] = []

        for batch in batches:
            result = await self._dispatch_one(batch)
            all_results.extend(result if isinstance(result, list) else [result])

        return all_results

    async def _dispatch_one(self, batch: List[Any]) -> Any:
        for attempt in range(self._max_retries + 1):
            try:
                result = await self._batch_fn(batch)
                self._sizer.record_success(
                    items_sent=len(batch),
                    tokens_used=self._sizer._estimator.total_tokens(
                        self._sizer._estimator.estimate_batch(batch)
                    ),
                )
                return result
            except Exception as exc:
                is_rate_limit = "429" in str(exc) or "rate" in str(exc).lower()
                if attempt < self._max_retries:
                    self._sizer.record_error(is_rate_limit)
                    await asyncio.sleep(self._retry_delay * (2 ** attempt))
                else:
                    self._sizer.record_error(is_rate_limit)
                    raise
```

## Solution 5: Quota-Aware Batch Planner

```python
import time
from typing import Optional


class QuotaAwareBatchPlanner:
    """
    Integrates provider quota information into batch sizing decisions.
    Reduces effective token budget when quota headroom is low.
    """

    def __init__(
        self,
        sizer: DynamicBatchSizer,
        tokens_per_minute_quota: int,
        safety_margin: float = 0.80,
    ) -> None:
        self._sizer = sizer
        self._tpm_quota = tokens_per_minute_quota
        self._safety = safety_margin
        self._minute_start = time.time()
        self._minute_tokens = 0

    def record_tokens_used(self, tokens: int) -> None:
        now = time.time()
        if now - self._minute_start >= 60.0:
            self._minute_start = now
            self._minute_tokens = 0
        self._minute_tokens += tokens

    def available_tokens_this_minute(self) -> int:
        budget = int(self._tpm_quota * self._safety)
        return max(0, budget - self._minute_tokens)

    def recommended_batch_token_budget(self) -> int:
        available = self.available_tokens_this_minute()
        ctx_budget = self._sizer._state.current_token_budget
        return min(available, ctx_budget)

    def status(self) -> dict:
        return {
            "tpm_quota": self._tpm_quota,
            "tokens_used_this_minute": self._minute_tokens,
            "available_this_minute": self.available_tokens_this_minute(),
            "recommended_batch_budget": self.recommended_batch_token_budget(),
        }
```

## Solution 6: Dynamic Batch Sizing Dashboard

```python
import time


class DynamicBatchSizingDashboard:
    """
    Combines sizer state, quota status, and throughput metrics
    into a single performance observability report.
    """

    def __init__(
        self,
        sizer: DynamicBatchSizer,
        planner: Optional[QuotaAwareBatchPlanner] = None,
    ) -> None:
        self._sizer = sizer
        self._planner = planner

    def render(self) -> dict:
        state = self._sizer.state_snapshot()
        quota = self._planner.status() if self._planner else None

        utilization = (
            state["current_token_budget"] /
            max(self._sizer._ctx.max_tokens_per_batch, 1) * 100
        )

        return {
            "generated_at": time.time(),
            "batch_sizer": state,
            "token_budget_utilization_pct": round(utilization, 1),
            "quota": quota,
        }
```

## Comparison

| Approach | Token Estimation | Size Adaptation | Error Backoff | Quota Awareness | Dashboard |
|---|---|---|---|---|---|
| BatchTokenEstimator | Yes (heuristic) | No | No | No | No |
| DynamicBatchSizer | Via estimator | Yes (grow/shrink) | Yes | No | No |
| AdaptiveBatchDispatcher | Via sizer | Via sizer | Yes (retry) | No | No |
| QuotaAwareBatchPlanner | No | Partial (budget cap) | No | Yes | No |
| DynamicBatchSizingDashboard | No | No | No | Via planner | Yes |

**Best for production**: Start `current_max_items` at 50% of the theoretical maximum and let the adaptive logic grow it — approaching quota from below is safer than retreating from a 429. Use `error_backoff_factor=0.5` (halve on error) and `recovery_growth_factor=1.2` (grow 20% on 3 consecutive successes) — this asymmetry means recovery is slower than degradation, which is intentional for quota safety. Wire `QuotaAwareBatchPlanner.available_tokens_this_minute()` as the upper bound for each batch submission so that quota headroom is always respected even when the adaptive sizer would allow a larger batch.
