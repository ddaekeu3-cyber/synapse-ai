---
title: "Agent Doesn't Implement Bulkhead Pattern for Concurrent Tool Calls"
description: "Agents that share a single execution pool across all tool types allow one slow or overloaded tool to consume all available concurrency and starve unrelated tools. A runaway web-scraping tool exhausts the thread pool and blocks database lookups. Implement the bulkhead pattern — isolated concurrency limits per tool group — so that saturation in one pool cannot propagate to others."
date: 2026-04-16
difficulty: intermediate
category: reliability
slug: agent-doesnt-implement-bulkhead-pattern-for-concurrent-tool-calls
tags: [bulkhead, concurrency, isolation, tool-calls, resilience, fault-isolation]
symptoms:
  - "Fast database tool calls queue behind slow web-scraping calls sharing the same executor"
  - "One misbehaving tool causes latency spikes across all unrelated tools"
  - "No per-tool-group concurrency limit — one tool can use all available workers"
  - "Timeout cascades: a hung external API blocks internal tool calls indefinitely"
  - "Cannot tune concurrency independently per tool category"
---

## Why This Happens

A single `asyncio.Semaphore` or thread pool shared across all tools creates an implicit dependency: saturation in one tool class degrades all others. The bulkhead pattern from naval architecture applies directly — partition the concurrency space so a flood in one compartment cannot sink the ship. Each tool group gets its own semaphore with its own limit, so a spike in external API calls cannot block database queries running in a separate compartment.

## Solution 1: Bulkhead Definition

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class BulkheadRejectionPolicy(str, Enum):
    RAISE = "raise"         # raise BulkheadFullError immediately
    WAIT = "wait"           # block until a slot opens
    FALLBACK = "fallback"   # return a static fallback value


@dataclass
class BulkheadConfig:
    name: str
    max_concurrent: int              # maximum simultaneous calls
    max_queue_depth: int = 0         # 0 = no queue (fail-fast)
    rejection_policy: BulkheadRejectionPolicy = BulkheadRejectionPolicy.RAISE
    timeout_seconds: float = 30.0
    fallback_value: Optional[object] = None

    def __post_init__(self) -> None:
        if self.max_concurrent < 1:
            raise ValueError("max_concurrent must be >= 1")
```

## Solution 2: Bulkhead Semaphore

```python
import asyncio
import time
from typing import Any, Optional


class BulkheadFullError(RuntimeError):
    def __init__(self, bulkhead_name: str, queue_depth: int) -> None:
        super().__init__(
            f"Bulkhead '{bulkhead_name}' is full (queue_depth={queue_depth})"
        )
        self.bulkhead_name = bulkhead_name
        self.queue_depth = queue_depth


class BulkheadSemaphore:
    """
    Wraps an asyncio.Semaphore with queue depth tracking,
    rejection policy enforcement, and per-call metrics.
    """

    def __init__(self, config: BulkheadConfig) -> None:
        self._config = config
        self._semaphore = asyncio.Semaphore(config.max_concurrent)
        self._queue_depth = 0
        self._active = 0
        self._rejected = 0
        self._total_calls = 0
        self._total_wait_ms = 0.0

    @property
    def name(self) -> str:
        return self._config.name

    async def acquire(self) -> bool:
        """
        Attempt to acquire a slot.
        Returns True on success.
        Raises BulkheadFullError or returns False based on rejection policy.
        """
        self._total_calls += 1

        if self._semaphore.locked():
            if self._config.max_queue_depth > 0 and self._queue_depth >= self._config.max_queue_depth:
                self._rejected += 1
                if self._config.rejection_policy == BulkheadRejectionPolicy.RAISE:
                    raise BulkheadFullError(self.name, self._queue_depth)
                return False

            if self._config.rejection_policy == BulkheadRejectionPolicy.RAISE:
                self._rejected += 1
                raise BulkheadFullError(self.name, self._queue_depth)

        self._queue_depth += 1
        wait_start = time.time()
        try:
            await asyncio.wait_for(self._semaphore.acquire(), timeout=self._config.timeout_seconds)
        except asyncio.TimeoutError:
            self._queue_depth -= 1
            self._rejected += 1
            raise BulkheadFullError(self.name, self._queue_depth)
        finally:
            self._total_wait_ms += (time.time() - wait_start) * 1000

        self._queue_depth -= 1
        self._active += 1
        return True

    def release(self) -> None:
        self._semaphore.release()
        self._active -= 1

    def stats(self) -> dict:
        return {
            "name": self.name,
            "max_concurrent": self._config.max_concurrent,
            "active": self._active,
            "queue_depth": self._queue_depth,
            "total_calls": self._total_calls,
            "rejected": self._rejected,
            "rejection_rate": round(self._rejected / max(self._total_calls, 1), 4),
            "avg_wait_ms": round(self._total_wait_ms / max(self._total_calls, 1), 2),
        }
```

## Solution 3: Bulkhead Registry

```python
from typing import Dict, List, Optional


class BulkheadRegistry:
    """
    Maintains a named set of bulkheads and maps tool names to bulkhead groups.
    Tools not explicitly mapped fall into the 'default' bulkhead.
    """

    DEFAULT_BULKHEAD = "default"

    def __init__(self) -> None:
        self._bulkheads: Dict[str, BulkheadSemaphore] = {}
        self._tool_to_bulkhead: Dict[str, str] = {}

    def register(self, config: BulkheadConfig) -> BulkheadSemaphore:
        bh = BulkheadSemaphore(config)
        self._bulkheads[config.name] = bh
        return bh

    def assign(self, tool_name: str, bulkhead_name: str) -> None:
        if bulkhead_name not in self._bulkheads:
            raise KeyError(f"Bulkhead '{bulkhead_name}' not registered")
        self._tool_to_bulkhead[tool_name] = bulkhead_name

    def assign_many(self, tool_names: List[str], bulkhead_name: str) -> None:
        for name in tool_names:
            self.assign(name, bulkhead_name)

    def get_for_tool(self, tool_name: str) -> BulkheadSemaphore:
        bulkhead_name = self._tool_to_bulkhead.get(tool_name, self.DEFAULT_BULKHEAD)
        bh = self._bulkheads.get(bulkhead_name)
        if bh is None:
            bh = self._bulkheads.get(self.DEFAULT_BULKHEAD)
        if bh is None:
            raise KeyError(f"No bulkhead found for tool '{tool_name}' and no default registered")
        return bh

    def all_stats(self) -> List[dict]:
        return [bh.stats() for bh in self._bulkheads.values()]
```

## Solution 4: Bulkhead-Protected Tool Executor

```python
import asyncio
import contextlib
from typing import Any, Callable


class BulkheadProtectedToolExecutor:
    """
    Executes tool calls inside the assigned bulkhead compartment.
    On BulkheadFullError, applies the configured rejection policy
    (raise, wait, or return fallback).
    """

    def __init__(self, registry: BulkheadRegistry) -> None:
        self._registry = registry

    async def call(
        self,
        tool_name: str,
        tool_fn: Callable,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        bh = self._registry.get_for_tool(tool_name)

        try:
            acquired = await bh.acquire()
        except BulkheadFullError:
            config = bh._config
            if config.rejection_policy == BulkheadRejectionPolicy.FALLBACK:
                return config.fallback_value
            raise

        if not acquired:
            return bh._config.fallback_value

        try:
            return await tool_fn(*args, **kwargs)
        finally:
            bh.release()

    @contextlib.asynccontextmanager
    async def compartment(self, tool_name: str):
        """Context manager form for manual use."""
        bh = self._registry.get_for_tool(tool_name)
        await bh.acquire()
        try:
            yield
        finally:
            bh.release()
```

## Solution 5: Bulkhead Saturation Monitor

```python
import time
from typing import List


class BulkheadSaturationMonitor:
    """
    Polls bulkhead stats and fires alerts when a compartment is saturated
    (active == max_concurrent) or the rejection rate exceeds a threshold.
    """

    def __init__(
        self,
        registry: BulkheadRegistry,
        rejection_rate_alert_threshold: float = 0.05,
        saturation_pct_alert_threshold: float = 0.90,
    ) -> None:
        self._registry = registry
        self._rejection_threshold = rejection_rate_alert_threshold
        self._saturation_threshold = saturation_pct_alert_threshold

    def check(self) -> List[dict]:
        alerts = []
        for stats in self._registry.all_stats():
            saturation = stats["active"] / max(stats["max_concurrent"], 1)
            if saturation >= self._saturation_threshold:
                alerts.append({
                    "type": "saturation",
                    "bulkhead": stats["name"],
                    "active": stats["active"],
                    "max_concurrent": stats["max_concurrent"],
                    "saturation_pct": round(saturation * 100, 1),
                })
            if stats["rejection_rate"] >= self._rejection_threshold:
                alerts.append({
                    "type": "high_rejection_rate",
                    "bulkhead": stats["name"],
                    "rejection_rate": stats["rejection_rate"],
                    "threshold": self._rejection_threshold,
                    "recommendation": (
                        f"Consider increasing max_concurrent for '{stats['name']}' "
                        "or reducing load on this bulkhead group."
                    ),
                })
        return alerts

    def report(self) -> dict:
        return {
            "generated_at": time.time(),
            "bulkheads": self._registry.all_stats(),
            "alerts": self.check(),
        }
```

## Solution 6: Bulkhead Dashboard

```python
import time


class BulkheadDashboard:
    """
    Combines bulkhead stats, saturation alerts, and isolation health
    into a single operational view.
    """

    def __init__(
        self,
        registry: BulkheadRegistry,
        monitor: BulkheadSaturationMonitor,
    ) -> None:
        self._registry = registry
        self._monitor = monitor

    def render(self) -> dict:
        all_stats = self._registry.all_stats()
        alerts = self._monitor.check()

        total_active = sum(s["active"] for s in all_stats)
        total_capacity = sum(s["max_concurrent"] for s in all_stats)
        total_rejected = sum(s["rejected"] for s in all_stats)
        total_calls = sum(s["total_calls"] for s in all_stats)

        saturated = [s["name"] for s in all_stats
                     if s["active"] >= s["max_concurrent"]]

        return {
            "generated_at": time.time(),
            "summary": {
                "total_bulkheads": len(all_stats),
                "total_active_calls": total_active,
                "total_capacity": total_capacity,
                "fleet_utilization_pct": round(total_active / max(total_capacity, 1) * 100, 1),
                "total_rejected": total_rejected,
                "fleet_rejection_rate": round(total_rejected / max(total_calls, 1), 4),
                "saturated_bulkheads": saturated,
            },
            "bulkheads": all_stats,
            "active_alerts": alerts,
        }
```

## Comparison

| Approach | Isolated Concurrency | Queue Depth Control | Rejection Policy | Saturation Alerts | Dashboard |
|---|---|---|---|---|---|
| BulkheadSemaphore | Yes (per compartment) | Yes | No | No | No |
| BulkheadRegistry | Via semaphore | Via semaphore | No | No | No |
| BulkheadProtectedToolExecutor | Via registry | Via registry | Yes | No | No |
| BulkheadSaturationMonitor | No | No | No | Yes | No |
| BulkheadDashboard | No | No | No | Via monitor | Yes |

**Best for production**: Create three bulkheads — `external_api` (max_concurrent=5, rejection_policy=RAISE), `database` (max_concurrent=20, rejection_policy=WAIT), and `compute` (max_concurrent=4, rejection_policy=FALLBACK). Assign scraping and third-party API tools to `external_api`, all DB query tools to `database`, and CPU-heavy tools to `compute`. This prevents a surge in external API calls from starving database tools. Monitor `rejection_rate` per bulkhead: a sustained rate above 5% means the compartment is undersized for load, not that the tools are broken.
