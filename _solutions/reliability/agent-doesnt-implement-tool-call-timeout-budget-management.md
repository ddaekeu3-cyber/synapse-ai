---
title: "Agent Doesn't Implement Tool Call Timeout Budget Management"
description: "Agents that assign the same fixed timeout to every tool call waste budget on fast tools and starve slow-but-important tools. Implement timeout budget management that allocates a total time budget per request, distributes it across tool calls based on historical latency and priority, and adjusts remaining budget dynamically as the request progresses."
date: 2026-04-16
difficulty: advanced
category: reliability
slug: agent-doesnt-implement-tool-call-timeout-budget-management
tags: [timeout-budget, latency-budget, tool-timeout, time-allocation, request-deadline, deadline-propagation]
symptoms:
  - "Fast tools get the same 30-second timeout as slow ones, wasting no-op wait time"
  - "A slow first tool call consumes the entire request budget, leaving no time for subsequent calls"
  - "No per-request deadline — individual tool timeouts don't add up to a coherent total"
  - "Users see slow responses when early tools take longer than expected with no adjustment"
  - "Cannot tell which tool consumed the most time budget in a slow request"
---

## Why This Happens

Fixed per-tool timeouts (30 seconds for every tool) are easy to implement but ignore two realities: (1) tools have very different latency profiles — a cache lookup takes 5ms, a web scrape takes 10s; (2) requests have end-to-end deadlines that must be respected regardless of how many tools are called. Timeout budget management requires tracking a per-request deadline, allocating time to each tool based on its expected duration and criticality, and recalculating remaining budget after each call so that the last tool in a chain still has meaningful time to complete.

## Solution 1: Request Time Budget

```python
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class ToolTimeAllocation:
    tool_name: str
    allocated_ms: float
    priority: int = 1           # higher = more budget when constrained
    min_ms: float = 500.0       # minimum allocation regardless of budget
    actual_ms: Optional[float] = None
    timed_out: bool = False


@dataclass
class RequestTimeBudget:
    total_ms: float
    started_at: float = field(default_factory=time.time)
    allocations: List[ToolTimeAllocation] = field(default_factory=list)

    @property
    def elapsed_ms(self) -> float:
        return (time.time() - self.started_at) * 1000

    @property
    def remaining_ms(self) -> float:
        return max(0.0, self.total_ms - self.elapsed_ms)

    @property
    def is_expired(self) -> bool:
        return self.remaining_ms <= 0

    def consume(self, tool_name: str, actual_ms: float) -> None:
        for alloc in self.allocations:
            if alloc.tool_name == tool_name:
                alloc.actual_ms = actual_ms
                return
        self.allocations.append(ToolTimeAllocation(
            tool_name=tool_name,
            allocated_ms=actual_ms,
            actual_ms=actual_ms,
        ))

    def utilization(self) -> float:
        return min(1.0, self.elapsed_ms / self.total_ms) if self.total_ms > 0 else 0.0
```

## Solution 2: Tool Latency Profile Store

```python
import time
from collections import deque
from threading import Lock
from typing import Deque, Dict, Optional, Tuple


class ToolLatencyProfileStore:
    """
    Stores historical latency samples per tool and computes
    P50/P95 estimates for timeout budget allocation.
    """

    def __init__(self, max_samples: int = 1000):
        self._samples: Dict[str, Deque[float]] = {}
        self._max = max_samples
        self._lock = Lock()

    def record(self, tool_name: str, latency_ms: float) -> None:
        with self._lock:
            if tool_name not in self._samples:
                self._samples[tool_name] = deque(maxlen=self._max)
            self._samples[tool_name].append(latency_ms)

    def p95(self, tool_name: str, default_ms: float = 10_000.0) -> float:
        with self._lock:
            samples = list(self._samples.get(tool_name, []))
        if not samples:
            return default_ms
        sorted_samples = sorted(samples)
        idx = min(int(len(sorted_samples) * 0.95), len(sorted_samples) - 1)
        return round(sorted_samples[idx], 2)

    def p50(self, tool_name: str, default_ms: float = 5_000.0) -> float:
        with self._lock:
            samples = list(self._samples.get(tool_name, []))
        if not samples:
            return default_ms
        sorted_samples = sorted(samples)
        idx = len(sorted_samples) // 2
        return round(sorted_samples[idx], 2)

    def all_profiles(self) -> Dict[str, dict]:
        with self._lock:
            result = {}
            for tool, samples in self._samples.items():
                s = sorted(samples)
                result[tool] = {
                    "sample_count": len(s),
                    "p50_ms": round(s[len(s) // 2], 2) if s else None,
                    "p95_ms": round(s[min(int(len(s) * 0.95), len(s) - 1)], 2) if s else None,
                }
            return result
```

## Solution 3: Budget Allocator

```python
from typing import Dict, List


class TimeoutBudgetAllocator:
    """
    Distributes remaining request budget across a planned set of tool calls.
    Uses P95 latency estimates weighted by tool priority.
    High-priority tools receive proportionally more budget when constrained.
    """

    def __init__(
        self,
        profile_store: ToolLatencyProfileStore,
        safety_margin_fraction: float = 0.15,
    ):
        self._profiles = profile_store
        self._margin = safety_margin_fraction

    def allocate(
        self,
        budget: RequestTimeBudget,
        planned_tools: List[Dict],  # list of {"name": str, "priority": int}
    ) -> List[ToolTimeAllocation]:
        spendable = budget.remaining_ms * (1 - self._margin)
        if spendable <= 0 or not planned_tools:
            return []

        # Estimate needed time per tool (P95)
        estimates = {
            t["name"]: self._profiles.p95(t["name"], default_ms=10_000.0)
            for t in planned_tools
        }
        total_estimated = sum(estimates.values())

        allocations = []
        for tool in planned_tools:
            name = tool["name"]
            priority = tool.get("priority", 1)
            estimated = estimates[name]

            if total_estimated > 0:
                fraction = estimated / total_estimated
            else:
                fraction = 1.0 / len(planned_tools)

            # Priority bonus: scale up by priority weight
            raw_alloc = spendable * fraction * priority
            # Normalize back across total priority
            total_priority = sum(t.get("priority", 1) for t in planned_tools)
            normalized = (spendable * fraction * priority) / max(total_priority, 1)
            min_ms = tool.get("min_ms", 500.0)
            allocated = max(min_ms, normalized)

            allocations.append(ToolTimeAllocation(
                tool_name=name,
                allocated_ms=round(allocated, 2),
                priority=priority,
                min_ms=min_ms,
            ))

        return allocations
```

## Solution 4: Budget-Aware Tool Executor

```python
import asyncio
import time
from typing import Any, Callable, Dict, Optional


class BudgetExceededError(Exception):
    def __init__(self, tool_name: str, remaining_ms: float):
        super().__init__(
            f"Time budget exceeded before calling tool '{tool_name}' "
            f"({remaining_ms:.1f}ms remaining)"
        )
        self.tool_name = tool_name
        self.remaining_ms = remaining_ms


class BudgetAwareToolExecutor:
    """
    Executes tool calls within their allocated time budget.
    Records actual latency for future profile updates.
    """

    def __init__(self, profile_store: ToolLatencyProfileStore):
        self._profiles = profile_store

    async def execute(
        self,
        tool_name: str,
        tool_fn: Callable,
        args: Dict[str, Any],
        budget: RequestTimeBudget,
        allocation: Optional[ToolTimeAllocation] = None,
    ) -> Any:
        remaining = budget.remaining_ms
        if remaining <= 0:
            raise BudgetExceededError(tool_name, remaining)

        timeout_ms = min(
            allocation.allocated_ms if allocation else remaining,
            remaining,
        )

        start = time.time()
        try:
            result = await asyncio.wait_for(
                tool_fn(**args),
                timeout=timeout_ms / 1000.0,
            )
            actual_ms = (time.time() - start) * 1000
            budget.consume(tool_name, actual_ms)
            self._profiles.record(tool_name, actual_ms)
            if allocation:
                allocation.actual_ms = actual_ms
            return result
        except asyncio.TimeoutError:
            actual_ms = (time.time() - start) * 1000
            budget.consume(tool_name, actual_ms)
            if allocation:
                allocation.timed_out = True
            raise
```

## Solution 5: Budget Propagation Context

```python
import time
from typing import Optional


class BudgetPropagationContext:
    """
    Propagates the request time budget through nested async calls.
    Allows any nested tool call to check the remaining budget without
    explicitly passing the budget object through every call frame.
    """

    _current: Optional[RequestTimeBudget] = None

    @classmethod
    def set(cls, budget: RequestTimeBudget) -> None:
        cls._current = budget

    @classmethod
    def get(cls) -> Optional[RequestTimeBudget]:
        return cls._current

    @classmethod
    def remaining_ms(cls) -> float:
        budget = cls._current
        return budget.remaining_ms if budget else float("inf")

    @classmethod
    def is_expired(cls) -> bool:
        budget = cls._current
        return budget.is_expired if budget else False

    @classmethod
    def clear(cls) -> None:
        cls._current = None
```

## Solution 6: Timeout Budget Dashboard

```python
import time
from typing import List


class TimeoutBudgetDashboard:
    """
    Reports budget utilization across recent requests and identifies
    tools that most frequently consume budget over their allocation.
    """

    def __init__(self, profile_store: ToolLatencyProfileStore):
        self._profiles = profile_store
        self._completed_budgets: List[RequestTimeBudget] = []

    def record_completed(self, budget: RequestTimeBudget) -> None:
        self._completed_budgets.append(budget)
        if len(self._completed_budgets) > 5000:
            self._completed_budgets.pop(0)

    def render(self, last_n: int = 100) -> dict:
        recent = self._completed_budgets[-last_n:]
        if not recent:
            return {"requests": 0}

        avg_util = sum(b.utilization() for b in recent) / len(recent)
        expired = sum(1 for b in recent if b.is_expired)

        over_budget_tools: dict = {}
        for budget in recent:
            for alloc in budget.allocations:
                if alloc.actual_ms and alloc.actual_ms > alloc.allocated_ms:
                    t = alloc.tool_name
                    over_budget_tools[t] = over_budget_tools.get(t, 0) + 1

        return {
            "generated_at": time.time(),
            "requests_analyzed": len(recent),
            "avg_budget_utilization": round(avg_util, 3),
            "expired_budgets": expired,
            "expiry_rate": round(expired / len(recent), 3),
            "over_budget_tools": sorted(
                over_budget_tools.items(), key=lambda x: -x[1]
            )[:5],
            "tool_profiles": self._profiles.all_profiles(),
        }
```

## Comparison

| Approach | Per-Request Deadline | Latency Profiling | Dynamic Allocation | Latency Recording | Dashboard |
|---|---|---|---|---|---|
| RequestTimeBudget | Yes | No | No | Via consume() | No |
| ToolLatencyProfileStore | No | Yes (P50/P95) | No | Yes | No |
| TimeoutBudgetAllocator | Via budget | Via profiles | Yes (priority) | No | No |
| BudgetAwareToolExecutor | Via budget | Via profiles | Via allocations | Yes | No |
| BudgetPropagationContext | Via budget | No | No | No | No |
| TimeoutBudgetDashboard | No | Via profiles | No | No | Yes |

**Best for production**: Set the total request budget at the HTTP gateway level (e.g., `X-Request-Deadline` header converted to milliseconds) rather than inside the agent — this ensures the budget reflects the actual user-facing timeout, not an internal estimate. Allocate 15% safety margin for LLM inference that happens after tool calls complete. Use P95 latency (not P50) for budget allocation: using median causes half of all calls to exceed their allocation, which defeats the purpose. Tools that repeatedly exceed their allocation are candidates for optimization or caching — surface them via the over-budget tool list in the dashboard.
