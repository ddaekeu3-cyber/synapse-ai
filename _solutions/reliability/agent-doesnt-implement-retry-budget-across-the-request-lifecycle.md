---
title: "Agent Doesn't Implement Retry Budget Across the Request Lifecycle"
description: "Agents that allow each tool and LLM call to retry independently can amplify a single user request into dozens of backend calls: three tools each retrying 3 times plus the LLM retrying 3 times produces up to 12 backend calls from one user action, overwhelming already-degraded dependencies. Implement a per-request retry budget that is shared across all sub-operations so total retries are bounded regardless of how many components fail."
date: 2026-04-16
difficulty: advanced
category: reliability
slug: agent-doesnt-implement-retry-budget-across-the-request-lifecycle
tags: [retry-budget, fault-tolerance, retry-amplification, thundering-herd, backpressure, request-lifecycle]
symptoms:
  - "Single user request triggers 15+ backend calls due to per-component retry logic"
  - "Degraded dependency gets worse during recovery due to retry amplification from agents"
  - "No visibility into how many retries have been consumed within one request"
  - "Tool A uses its 3 retries, tool B uses its 3, LLM uses its 3 — 9 retries total, uncapped"
  - "Retry storms during incidents traced to per-component retry limits that compose multiplicatively"
---

## Why This Happens

Per-component retry limits (each tool retries up to 3 times) compose multiplicatively. If an agent makes 4 tool calls and each has its own retry limit of 3, a total failure scenario can produce 12 tool calls before the request fails — plus LLM retries on top. Under load, this amplification worsens degraded dependencies exactly when they need relief. A per-request retry budget allocates a fixed number of retry tokens to each request; any sub-operation that wants to retry must first claim a token from the shared budget. When the budget is exhausted, no further retries are allowed within that request lifecycle.

## Solution 1: Request Retry Budget

```python
import threading
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class RequestRetryBudget:
    request_id: str
    total_tokens: int = 6             # max retries across the whole request
    tokens_remaining: int = field(init=False)
    consumed_by: dict = field(default_factory=dict)
    _lock: object = field(default_factory=threading.Lock, repr=False, compare=False)

    def __post_init__(self):
        self.tokens_remaining = self.total_tokens

    def claim(self, component: str, count: int = 1) -> bool:
        """Returns True if tokens were claimed, False if budget exhausted."""
        with self._lock:
            if self.tokens_remaining < count:
                return False
            self.tokens_remaining -= count
            self.consumed_by[component] = self.consumed_by.get(component, 0) + count
            return True

    def remaining(self) -> int:
        with self._lock:
            return self.tokens_remaining

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "request_id": self.request_id,
                "total_tokens": self.total_tokens,
                "remaining": self.tokens_remaining,
                "consumed": self.total_tokens - self.tokens_remaining,
                "consumed_by": dict(self.consumed_by),
            }
```

## Solution 2: Budget-Aware Retry Executor

```python
import asyncio
import time
from typing import Any, Callable, Optional, Tuple


class BudgetExhaustedError(Exception):
    def __init__(self, component: str, budget: RequestRetryBudget):
        super().__init__(
            f"Retry budget exhausted for '{component}' "
            f"(remaining={budget.remaining()}, total={budget.total_tokens})"
        )
        self.component = component
        self.budget = budget


class BudgetAwareRetryExecutor:
    """
    Executes a callable with retries, but only if the shared request
    retry budget has tokens available. Raises BudgetExhaustedError
    when the budget is depleted rather than retrying.
    """

    def __init__(
        self,
        base_delay_seconds: float = 0.5,
        backoff_multiplier: float = 2.0,
        max_delay_seconds: float = 10.0,
        jitter: bool = True,
    ):
        self._base_delay = base_delay_seconds
        self._multiplier = backoff_multiplier
        self._max_delay = max_delay_seconds
        self._jitter = jitter

    def _delay(self, attempt: int) -> float:
        import random
        delay = min(self._base_delay * (self._multiplier ** attempt), self._max_delay)
        if self._jitter:
            delay *= (0.5 + random.random() * 0.5)
        return delay

    async def execute(
        self,
        fn: Callable,
        component: str,
        budget: RequestRetryBudget,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        attempt = 0
        last_exc = None

        while True:
            try:
                return await fn(*args, **kwargs)
            except Exception as exc:
                last_exc = exc
                if not budget.claim(component):
                    raise BudgetExhaustedError(component, budget) from exc
                delay = self._delay(attempt)
                await asyncio.sleep(delay)
                attempt += 1
```

## Solution 3: Request Retry Budget Registry

```python
import time
import uuid
from threading import Lock
from typing import Dict, Optional


class RequestRetryBudgetRegistry:
    """
    Creates and tracks per-request retry budgets.
    Automatically expires budgets after a configurable TTL.
    """

    def __init__(
        self,
        default_total_tokens: int = 6,
        ttl_seconds: float = 300.0,
    ):
        self._default_tokens = default_total_tokens
        self._ttl = ttl_seconds
        self._budgets: Dict[str, RequestRetryBudget] = {}
        self._created_at: Dict[str, float] = {}
        self._lock = Lock()

    def create(
        self,
        request_id: Optional[str] = None,
        total_tokens: Optional[int] = None,
    ) -> RequestRetryBudget:
        rid = request_id or str(uuid.uuid4())[:8]
        budget = RequestRetryBudget(
            request_id=rid,
            total_tokens=total_tokens or self._default_tokens,
        )
        with self._lock:
            self._budgets[rid] = budget
            self._created_at[rid] = time.time()
        return budget

    def get(self, request_id: str) -> Optional[RequestRetryBudget]:
        with self._lock:
            return self._budgets.get(request_id)

    def expire(self, request_id: str) -> None:
        with self._lock:
            self._budgets.pop(request_id, None)
            self._created_at.pop(request_id, None)

    def prune_expired(self) -> int:
        cutoff = time.time() - self._ttl
        with self._lock:
            expired = [rid for rid, ts in self._created_at.items() if ts < cutoff]
            for rid in expired:
                self._budgets.pop(rid, None)
                self._created_at.pop(rid, None)
        return len(expired)

    def all_snapshots(self) -> list:
        with self._lock:
            return [b.snapshot() for b in self._budgets.values()]
```

## Solution 4: Budget-Integrated Tool Dispatcher

```python
from typing import Any, Callable, Dict, Optional


class BudgetIntegratedToolDispatcher:
    """
    Dispatches tool calls using the budget-aware retry executor.
    All tool calls within one request share the same retry budget.
    """

    def __init__(
        self,
        executor: BudgetAwareRetryExecutor,
        tool_registry: Dict[str, Callable],
    ):
        self._executor = executor
        self._tool_registry = tool_registry
        self._exhaustion_count = 0
        self._total_dispatches = 0

    async def dispatch(
        self,
        tool_name: str,
        args: Dict[str, Any],
        budget: RequestRetryBudget,
    ) -> Any:
        self._total_dispatches += 1
        tool_fn = self._tool_registry.get(tool_name)
        if not tool_fn:
            raise KeyError(f"Tool '{tool_name}' not registered")
        try:
            return await self._executor.execute(
                tool_fn, tool_name, budget, **args
            )
        except BudgetExhaustedError:
            self._exhaustion_count += 1
            raise

    def stats(self) -> dict:
        return {
            "total_dispatches": self._total_dispatches,
            "budget_exhaustions": self._exhaustion_count,
            "exhaustion_rate": round(
                self._exhaustion_count / max(self._total_dispatches, 1), 4
            ),
        }
```

## Solution 5: Retry Budget Usage Reporter

```python
import time
from threading import Lock
from typing import List


class RetryBudgetUsageReporter:
    """
    Records completed request retry budget snapshots and surfaces
    how often budgets are exhausted and which components consume most tokens.
    """

    def __init__(self):
        self._records: List[dict] = []
        self._lock = Lock()

    def record(self, budget: RequestRetryBudget) -> None:
        with self._lock:
            snap = budget.snapshot()
            snap["recorded_at"] = time.time()
            snap["exhausted"] = snap["remaining"] == 0
            self._records.append(snap)
            if len(self._records) > 10000:
                self._records.pop(0)

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        with self._lock:
            recent = [r for r in self._records if r.get("recorded_at", 0) >= cutoff]
        if not recent:
            return {"window_seconds": window_seconds, "requests": 0}

        exhausted = sum(1 for r in recent if r.get("exhausted"))
        total_consumed = sum(r.get("consumed", 0) for r in recent)
        component_totals: dict = {}
        for r in recent:
            for comp, count in r.get("consumed_by", {}).items():
                component_totals[comp] = component_totals.get(comp, 0) + count

        return {
            "window_seconds": window_seconds,
            "requests": len(recent),
            "budget_exhaustions": exhausted,
            "exhaustion_rate": round(exhausted / len(recent), 4),
            "avg_tokens_consumed": round(total_consumed / len(recent), 2),
            "top_consumers": dict(
                sorted(component_totals.items(), key=lambda x: -x[1])[:5]
            ),
        }
```

## Solution 6: Retry Budget Dashboard

```python
import time


class RetryBudgetDashboard:
    """
    Combines live budget registry state, usage trends, and dispatcher stats.
    """

    def __init__(
        self,
        registry: RequestRetryBudgetRegistry,
        reporter: RetryBudgetUsageReporter,
        dispatcher: BudgetIntegratedToolDispatcher,
    ):
        self._registry = registry
        self._reporter = reporter
        self._dispatcher = dispatcher

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "active_budgets": len(self._registry.all_snapshots()),
            "dispatcher_stats": self._dispatcher.stats(),
            "usage_summary_1h": self._reporter.summary(3600.0),
        }
```

## Comparison

| Approach | Shared Budget | Claim on Retry | Per-Component Attribution | Expiry | Usage Reporting |
|---|---|---|---|---|---|
| RequestRetryBudget | Yes | Yes (thread-safe) | Yes | No | No |
| BudgetAwareRetryExecutor | Via budget | Yes | Via budget | No | No |
| RequestRetryBudgetRegistry | No | No | No | Yes (TTL) | No |
| BudgetIntegratedToolDispatcher | Via executor | Via executor | Via executor | No | No |
| RetryBudgetUsageReporter | No | No | Yes (aggregate) | No | Yes |

**Best for production**: Set `total_tokens=6` for standard requests — this allows 2 retries each across 3 tool calls, or 6 retries on a single failing tool. Lower it to 3 during incidents by adjusting `default_total_tokens` in the registry, forcing faster failure and reducing amplification. Log `budget.snapshot()` on every request completion via `RetryBudgetUsageReporter` — a sustained `exhaustion_rate` above 5% means either the total_tokens budget is too small or a dependency is persistently degraded and needs a circuit breaker, not more retries. Propagate the budget via request context (asyncio `contextvars.ContextVar`) so nested async calls automatically share the same budget without explicit passing.
