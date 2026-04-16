---
title: "Agent Doesn't Implement Bulkhead Isolation Between Tool Categories"
description: "Agents that run all tools in a single shared thread pool or async executor allow a slow or stuck tool to exhaust all available concurrency, blocking unrelated tools from executing. A runaway code execution tool consuming all worker threads prevents fast database lookups from completing. Implement bulkhead isolation that assigns each tool category its own bounded executor, so a fault in one category cannot starve another."
date: 2026-04-16
difficulty: advanced
category: reliability
slug: agent-doesnt-implement-bulkhead-isolation-between-tool-categories
tags: [bulkhead, isolation, thread-pool, semaphore, concurrency-control, fault-containment]
symptoms:
  - "Slow code execution tools block fast lookup tools from running"
  - "A single stuck tool causes the entire agent to become unresponsive"
  - "No per-category concurrency limits — all tools compete for the same pool"
  - "External API tools with high latency starve internal tools"
  - "A tool that enters an infinite loop or deadlock takes down all tool execution"
---

## Why This Happens

A shared executor gives every tool equal access to the same pool of workers. When a tool category with naturally high latency (code execution, external API calls) or potential for hangs starts consuming workers, it leaves none for fast, reliable tools (in-memory lookups, math computations). The bulkhead pattern — borrowed from ship hull design — places each category in its own watertight compartment: a flood in one compartment does not spread. In Python, per-category `asyncio.Semaphore` objects or separate `ThreadPoolExecutor` instances implement this pattern.

## Solution 1: Bulkhead Configuration

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Optional


class ToolCategory(str, Enum):
    COMPUTE = "compute"         # in-process computation, math, parsing
    DATABASE = "database"       # internal DB queries
    EXTERNAL_API = "external_api"  # outbound HTTP calls to third-party APIs
    CODE_EXECUTION = "code_execution"  # shell commands, sandboxed code
    FILE_IO = "file_io"         # file read/write
    LLM = "llm"                 # nested LLM calls


@dataclass
class BulkheadConfig:
    category: ToolCategory
    max_concurrent: int         # maximum simultaneous executions
    timeout_seconds: float      # per-call timeout within the bulkhead
    queue_size: int = 0         # 0 = reject immediately when full, >0 = queue up to N
    priority: int = 0           # higher = served first when multiple bulkheads compete


def default_bulkhead_configs() -> Dict[ToolCategory, BulkheadConfig]:
    return {
        ToolCategory.COMPUTE: BulkheadConfig(
            category=ToolCategory.COMPUTE,
            max_concurrent=20,
            timeout_seconds=5.0,
            queue_size=50,
            priority=10,
        ),
        ToolCategory.DATABASE: BulkheadConfig(
            category=ToolCategory.DATABASE,
            max_concurrent=10,
            timeout_seconds=10.0,
            queue_size=20,
            priority=8,
        ),
        ToolCategory.EXTERNAL_API: BulkheadConfig(
            category=ToolCategory.EXTERNAL_API,
            max_concurrent=5,
            timeout_seconds=30.0,
            queue_size=10,
            priority=5,
        ),
        ToolCategory.CODE_EXECUTION: BulkheadConfig(
            category=ToolCategory.CODE_EXECUTION,
            max_concurrent=3,
            timeout_seconds=60.0,
            queue_size=5,
            priority=3,
        ),
        ToolCategory.FILE_IO: BulkheadConfig(
            category=ToolCategory.FILE_IO,
            max_concurrent=8,
            timeout_seconds=15.0,
            queue_size=10,
            priority=6,
        ),
        ToolCategory.LLM: BulkheadConfig(
            category=ToolCategory.LLM,
            max_concurrent=4,
            timeout_seconds=120.0,
            queue_size=8,
            priority=4,
        ),
    }
```

## Solution 2: Bulkhead Semaphore

```python
import asyncio
import time
from typing import Optional


class BulkheadSemaphore:
    """
    Asyncio semaphore with queue depth tracking and rejection on overflow.
    """

    def __init__(self, config: BulkheadConfig):
        self._config = config
        self._semaphore = asyncio.Semaphore(config.max_concurrent)
        self._queue_count = 0
        self._active_count = 0
        self._rejected_count = 0
        self._total_acquired = 0
        self._wait_times: list = []

    async def acquire(self) -> bool:
        """
        Returns True if acquired. Returns False if queue is full (reject).
        Caller must call release() after True.
        """
        if self._queue_count >= self._config.queue_size and self._config.queue_size > 0:
            self._rejected_count += 1
            return False

        self._queue_count += 1
        start = time.time()
        try:
            await asyncio.wait_for(
                self._semaphore.acquire(),
                timeout=self._config.timeout_seconds,
            )
        except asyncio.TimeoutError:
            self._queue_count -= 1
            self._rejected_count += 1
            return False
        finally:
            wait_ms = (time.time() - start) * 1000
            self._wait_times.append(wait_ms)
            if len(self._wait_times) > 500:
                self._wait_times.pop(0)

        self._queue_count -= 1
        self._active_count += 1
        self._total_acquired += 1
        return True

    def release(self) -> None:
        self._active_count = max(0, self._active_count - 1)
        self._semaphore.release()

    def stats(self) -> dict:
        avg_wait = sum(self._wait_times) / max(len(self._wait_times), 1)
        return {
            "category": self._config.category.value,
            "max_concurrent": self._config.max_concurrent,
            "active": self._active_count,
            "queued": self._queue_count,
            "rejected_total": self._rejected_count,
            "total_acquired": self._total_acquired,
            "avg_wait_ms": round(avg_wait, 2),
        }
```

## Solution 3: Bulkhead Registry

```python
import asyncio
import contextlib
from typing import Dict, Optional


class BulkheadRegistry:
    """
    Manages per-category BulkheadSemaphore instances.
    """

    def __init__(self, configs: Dict[ToolCategory, BulkheadConfig]):
        self._bulkheads: Dict[ToolCategory, BulkheadSemaphore] = {
            cat: BulkheadSemaphore(cfg)
            for cat, cfg in configs.items()
        }

    def get(self, category: ToolCategory) -> BulkheadSemaphore:
        bh = self._bulkheads.get(category)
        if bh is None:
            raise KeyError(f"No bulkhead configured for category '{category}'")
        return bh

    @contextlib.asynccontextmanager
    async def acquire(self, category: ToolCategory):
        bh = self.get(category)
        acquired = await bh.acquire()
        if not acquired:
            raise BulkheadFullError(category)
        try:
            yield bh
        finally:
            bh.release()

    def all_stats(self) -> Dict[str, dict]:
        return {cat.value: bh.stats() for cat, bh in self._bulkheads.items()}


class BulkheadFullError(Exception):
    def __init__(self, category: ToolCategory):
        super().__init__(f"bulkhead full for category '{category.value}' — request rejected")
        self.category = category
```

## Solution 4: Bulkhead-Isolated Tool Executor

```python
import asyncio
import time
from typing import Any, Callable


TOOL_CATEGORY_MAP: Dict[str, ToolCategory] = {}


def register_tool_category(tool_name: str, category: ToolCategory) -> None:
    TOOL_CATEGORY_MAP[tool_name] = category


class BulkheadIsolatedToolExecutor:
    """
    Dispatches tool calls through the appropriate bulkhead.
    Tools that exceed the bulkhead capacity receive BulkheadFullError
    immediately rather than waiting indefinitely.
    """

    def __init__(self, registry: BulkheadRegistry):
        self._registry = registry
        self._execution_log: list = []

    async def execute(
        self,
        tool_name: str,
        tool_fn: Callable,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        category = TOOL_CATEGORY_MAP.get(tool_name, ToolCategory.EXTERNAL_API)
        start = time.time()

        async with self._registry.acquire(category):
            try:
                result = await asyncio.wait_for(
                    tool_fn(*args, **kwargs),
                    timeout=self._registry.get(category)._config.timeout_seconds,
                )
                self._log(tool_name, category, time.time() - start, success=True)
                return result
            except asyncio.TimeoutError:
                self._log(tool_name, category, time.time() - start, success=False, reason="timeout")
                raise ToolTimeoutError(tool_name, category)
            except Exception:
                self._log(tool_name, category, time.time() - start, success=False, reason="error")
                raise

    def _log(self, tool: str, cat: ToolCategory, duration: float, success: bool, reason: str = "") -> None:
        self._execution_log.append({
            "ts": time.time(),
            "tool": tool,
            "category": cat.value,
            "duration_ms": round(duration * 1000, 2),
            "success": success,
            "reason": reason,
        })
        if len(self._execution_log) > 5000:
            self._execution_log.pop(0)


class ToolTimeoutError(Exception):
    def __init__(self, tool_name: str, category: ToolCategory):
        super().__init__(f"tool '{tool_name}' (category={category.value}) exceeded bulkhead timeout")
        self.tool_name = tool_name
        self.category = category
```

## Solution 5: Bulkhead Saturation Detector

```python
import time
from typing import Dict, List


class BulkheadSaturationDetector:
    """
    Detects when a bulkhead is consistently near capacity — a signal
    that either the limit is too low or the tool is pathologically slow.
    """

    def __init__(self, registry: BulkheadRegistry, saturation_threshold: float = 0.80):
        self._registry = registry
        self._threshold = saturation_threshold

    def saturated_categories(self) -> List[dict]:
        results = []
        for cat, bh in self._registry._bulkheads.items():
            stats = bh.stats()
            utilization = stats["active"] / max(stats["max_concurrent"], 1)
            if utilization >= self._threshold:
                results.append({
                    "category": cat.value,
                    "utilization": round(utilization, 3),
                    "active": stats["active"],
                    "max_concurrent": stats["max_concurrent"],
                    "queued": stats["queued"],
                    "rejected_total": stats["rejected_total"],
                })
        return results
```

## Solution 6: Bulkhead Dashboard

```python
import time


class BulkheadDashboard:
    """
    Combines registry stats and saturation detection into a single view.
    """

    def __init__(
        self,
        registry: BulkheadRegistry,
        detector: BulkheadSaturationDetector,
    ):
        self._registry = registry
        self._detector = detector

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "bulkheads": self._registry.all_stats(),
            "saturated": self._detector.saturated_categories(),
        }
```

## Comparison

| Approach | Per-Category Limits | Queue + Reject | Timeout Isolation | Saturation Detection | Dashboard |
|---|---|---|---|---|---|
| BulkheadSemaphore | Yes | Yes | Yes (asyncio.wait_for) | No | No |
| BulkheadRegistry | Via semaphores | Via semaphores | Via semaphores | No | No |
| BulkheadIsolatedToolExecutor | Via registry | Via registry | Via config | No | No |
| BulkheadSaturationDetector | No | No | No | Yes | No |
| BulkheadDashboard | No | No | No | No | Yes |

**Best for production**: Start with the six default categories and assign every tool to one during registration — an unassigned tool defaults to `EXTERNAL_API` (most conservative limits). Set `CODE_EXECUTION` max_concurrent to the number of available CPU cores minus one, never higher — code execution tools that spin in tight loops will peg the CPU and the operating system scheduler becomes the real bulkhead. Monitor `rejected_total` per category: a non-zero value means the system is shedding load, which should trigger an alert. Use `BulkheadSaturationDetector` to distinguish between "all workers busy processing normally" (high utilization, low rejection rate) and "something is stuck" (high utilization, rising rejection rate, rising queue depth).
