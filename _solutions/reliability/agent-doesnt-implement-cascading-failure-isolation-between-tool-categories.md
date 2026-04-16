---
title: "Agent Doesn't Implement Cascading Failure Isolation Between Tool Categories"
description: "Agents that share a single execution pool across all tool categories allow one misbehaving tool type to starve others: a slow database tool that monopolizes worker threads blocks fast in-memory tools from executing, causing unrelated capabilities to appear unavailable. Implement bulkhead isolation that assigns dedicated resource pools to tool categories, containing failures within a category."
date: 2026-04-16
difficulty: advanced
category: reliability
slug: agent-doesnt-implement-cascading-failure-isolation-between-tool-categories
tags: [bulkhead, cascading-failure, tool-isolation, resource-pools, failure-containment, thread-pool-isolation]
symptoms:
  - "Slow database queries block search tool responses despite no database dependency"
  - "One hanging external API call exhausts shared worker pool, freezing all other tools"
  - "Category A tool failure rate spikes propagate to category B with no logical connection"
  - "No resource ceiling per tool category — any tool type can consume 100% of capacity"
  - "Tool timeouts in one integration cause queuing across unrelated integrations"
---

## Why This Happens

Shared resource pools are a hidden coupling between otherwise independent tool categories. When all tools compete for the same asyncio semaphore or thread pool, a category that suddenly generates slow or blocking calls claims slots and leaves none for other categories. The bulkhead pattern (from resilience engineering) allocates a fixed pool to each category independently: database tools get their own semaphore, external HTTP tools get theirs, in-memory tools get theirs. A database slowdown now exhausts only the database semaphore — search and memory tools remain fully available.

## Solution 1: Tool Category Definition

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Optional


class ToolCategory(str, Enum):
    DATABASE = "database"
    EXTERNAL_HTTP = "external_http"
    INTERNAL_MEMORY = "internal_memory"
    FILE_SYSTEM = "file_system"
    LLM = "llm"
    COMPUTE = "compute"
    MESSAGING = "messaging"
    UNCATEGORIZED = "uncategorized"


@dataclass
class BulkheadConfig:
    category: ToolCategory
    max_concurrent: int
    queue_size: int = 50            # max queued calls before rejection
    timeout_seconds: float = 30.0
    shed_on_full_queue: bool = True


DEFAULT_BULKHEAD_CONFIGS: Dict[ToolCategory, BulkheadConfig] = {
    ToolCategory.DATABASE: BulkheadConfig(ToolCategory.DATABASE, max_concurrent=10, timeout_seconds=15.0),
    ToolCategory.EXTERNAL_HTTP: BulkheadConfig(ToolCategory.EXTERNAL_HTTP, max_concurrent=20, timeout_seconds=30.0),
    ToolCategory.INTERNAL_MEMORY: BulkheadConfig(ToolCategory.INTERNAL_MEMORY, max_concurrent=50, timeout_seconds=5.0),
    ToolCategory.FILE_SYSTEM: BulkheadConfig(ToolCategory.FILE_SYSTEM, max_concurrent=8, timeout_seconds=10.0),
    ToolCategory.LLM: BulkheadConfig(ToolCategory.LLM, max_concurrent=5, timeout_seconds=60.0),
    ToolCategory.COMPUTE: BulkheadConfig(ToolCategory.COMPUTE, max_concurrent=4, timeout_seconds=120.0),
    ToolCategory.MESSAGING: BulkheadConfig(ToolCategory.MESSAGING, max_concurrent=15, timeout_seconds=10.0),
    ToolCategory.UNCATEGORIZED: BulkheadConfig(ToolCategory.UNCATEGORIZED, max_concurrent=10, timeout_seconds=30.0),
}
```

## Solution 2: Category Bulkhead

```python
import asyncio
import time
from typing import Any, Callable


class CategoryBulkhead:
    """
    Enforces concurrency limits for a single tool category.
    Tracks active calls, queue depth, timeouts, and rejections.
    """

    def __init__(self, config: BulkheadConfig):
        self._config = config
        self._semaphore = asyncio.Semaphore(config.max_concurrent)
        self._active = 0
        self._queued = 0
        self._rejected = 0
        self._timeouts = 0
        self._completed = 0

    async def execute(self, fn: Callable, *args, **kwargs) -> Any:
        if self._queued >= self._config.queue_size and self._config.shed_on_full_queue:
            self._rejected += 1
            raise BulkheadRejectedError(
                self._config.category,
                self._queued,
                self._config.queue_size,
            )

        self._queued += 1
        try:
            acquired = await asyncio.wait_for(
                self._acquire(), timeout=self._config.timeout_seconds
            )
        except asyncio.TimeoutError:
            self._queued -= 1
            self._timeouts += 1
            raise BulkheadTimeoutError(
                self._config.category,
                self._config.timeout_seconds,
            )
        finally:
            pass

        self._queued -= 1
        self._active += 1
        try:
            return await asyncio.wait_for(fn(*args, **kwargs), timeout=self._config.timeout_seconds)
        except asyncio.TimeoutError:
            self._timeouts += 1
            raise
        finally:
            self._active -= 1
            self._completed += 1
            self._semaphore.release()

    async def _acquire(self) -> None:
        await self._semaphore.acquire()

    def stats(self) -> dict:
        return {
            "category": self._config.category.value,
            "active": self._active,
            "queued": self._queued,
            "max_concurrent": self._config.max_concurrent,
            "utilization": round(self._active / max(self._config.max_concurrent, 1), 4),
            "rejected_total": self._rejected,
            "timeout_total": self._timeouts,
            "completed_total": self._completed,
        }


class BulkheadRejectedError(Exception):
    def __init__(self, category: ToolCategory, queue_depth: int, queue_max: int):
        super().__init__(
            f"Bulkhead '{category.value}' rejected: queue full ({queue_depth}/{queue_max})"
        )
        self.category = category


class BulkheadTimeoutError(Exception):
    def __init__(self, category: ToolCategory, timeout: float):
        super().__init__(f"Bulkhead '{category.value}' timeout after {timeout}s")
        self.category = category
```

## Solution 3: Bulkhead Registry

```python
from typing import Dict, Optional


class BulkheadRegistry:
    """
    Manages bulkhead instances per tool category.
    Maps tool names to their categories and routes execution.
    """

    def __init__(self, configs: Dict[ToolCategory, BulkheadConfig] = None):
        configs = configs or DEFAULT_BULKHEAD_CONFIGS
        self._bulkheads: Dict[ToolCategory, CategoryBulkhead] = {
            cat: CategoryBulkhead(cfg)
            for cat, cfg in configs.items()
        }
        self._tool_categories: Dict[str, ToolCategory] = {}

    def register_tool(self, tool_name: str, category: ToolCategory) -> None:
        self._tool_categories[tool_name] = category

    def get_bulkhead(self, tool_name: str) -> CategoryBulkhead:
        category = self._tool_categories.get(tool_name, ToolCategory.UNCATEGORIZED)
        return self._bulkheads[category]

    def all_stats(self) -> Dict[str, dict]:
        return {cat.value: bh.stats() for cat, bh in self._bulkheads.items()}

    def saturated_categories(self) -> list:
        return [
            cat.value for cat, bh in self._bulkheads.items()
            if bh.stats()["utilization"] >= 0.90
        ]
```

## Solution 4: Bulkhead-Isolated Tool Dispatcher

```python
import asyncio
from typing import Any, Callable, Dict


class BulkheadIsolatedToolDispatcher:
    """
    Dispatches tool calls through the appropriate category bulkhead.
    Each category's failures are contained within its own semaphore.
    """

    def __init__(self, registry: BulkheadRegistry):
        self._registry = registry
        self._dispatch_count = 0
        self._isolation_events = 0

    async def dispatch(
        self,
        tool_name: str,
        args: Dict[str, Any],
        tool_fn: Callable,
    ) -> dict:
        bulkhead = self._registry.get_bulkhead(tool_name)
        self._dispatch_count += 1

        try:
            result = await bulkhead.execute(tool_fn, tool_name, args)
            return {"result": result, "isolated": True, "category": bulkhead._config.category.value}
        except (BulkheadRejectedError, BulkheadTimeoutError) as exc:
            self._isolation_events += 1
            raise

    def stats(self) -> dict:
        return {
            "total_dispatches": self._dispatch_count,
            "isolation_events": self._isolation_events,
            "category_stats": self._registry.all_stats(),
            "saturated_categories": self._registry.saturated_categories(),
        }
```

## Solution 5: Cross-Category Impact Analyzer

```python
import time
from typing import Dict, List


class CrossCategoryImpactAnalyzer:
    """
    Detects whether a failure in one category is correlated with
    degradation in another — signalling that isolation is insufficient
    or that a shared dependency links the categories.
    """

    def __init__(self, registry: BulkheadRegistry):
        self._registry = registry
        self._snapshots: List[dict] = []

    def snapshot(self) -> None:
        self._snapshots.append({
            "ts": time.time(),
            "stats": self._registry.all_stats(),
        })
        if len(self._snapshots) > 500:
            self._snapshots.pop(0)

    def find_correlated_saturation(self, window_seconds: float = 300.0) -> List[dict]:
        cutoff = time.time() - window_seconds
        recent = [s for s in self._snapshots if s["ts"] >= cutoff]
        if len(recent) < 5:
            return []

        # Find timestamps where multiple categories were saturated simultaneously
        correlated = []
        for snap in recent:
            saturated = [
                cat for cat, stats in snap["stats"].items()
                if stats["utilization"] >= 0.85
            ]
            if len(saturated) >= 2:
                correlated.append({
                    "ts": snap["ts"],
                    "saturated_categories": saturated,
                })
        return correlated
```

## Solution 6: Bulkhead Isolation Dashboard

```python
import time


class BulkheadIsolationDashboard:
    """
    Combines bulkhead stats, cross-category impact analysis, and
    dispatcher metrics into an isolation health report.
    """

    def __init__(
        self,
        dispatcher: BulkheadIsolatedToolDispatcher,
        analyzer: CrossCategoryImpactAnalyzer,
    ):
        self._dispatcher = dispatcher
        self._analyzer = analyzer

    def render(self) -> dict:
        self._analyzer.snapshot()
        stats = self._dispatcher.stats()
        return {
            "generated_at": time.time(),
            "dispatcher": {
                "total_dispatches": stats["total_dispatches"],
                "isolation_events": stats["isolation_events"],
            },
            "category_bulkheads": stats["category_stats"],
            "saturated_categories": stats["saturated_categories"],
            "cross_category_correlation": self._analyzer.find_correlated_saturation(),
        }
```

## Comparison

| Approach | Per-Category Semaphore | Queue Depth Limit | Tool-to-Category Mapping | Correlation Detection | Dashboard |
|---|---|---|---|---|---|
| CategoryBulkhead | Yes | Yes (reject) | No | No | No |
| BulkheadRegistry | Via bulkheads | Via bulkheads | Yes | No | No |
| BulkheadIsolatedToolDispatcher | Via registry | Via registry | Via registry | No | No |
| CrossCategoryImpactAnalyzer | No | No | No | Yes | No |
| BulkheadIsolationDashboard | No | No | No | No | Yes |

**Best for production**: Size each category's `max_concurrent` based on the downstream service's documented rate limit, not agent capacity — a database pool of 10 matches a Postgres `max_connections` of 100 shared across 10 agent instances. Set `queue_size` to 2× `max_concurrent` so brief bursts are absorbed without rejection, but chronic overload is shed quickly. Use `CrossCategoryImpactAnalyzer` to detect hidden shared dependencies: two categories that consistently saturate together despite isolation share a resource (DNS resolver, shared auth service, network interface) that the bulkhead cannot isolate. Monitor `isolation_events` over time: zero means the bulkheads are never exercised; very high means a category is systematically undersized.
