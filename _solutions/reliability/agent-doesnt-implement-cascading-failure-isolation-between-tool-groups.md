---
title: "Agent Doesn't Implement Cascading Failure Isolation Between Tool Groups"
description: "Agents that treat all tools as a single undifferentiated pool will propagate failures from one group to another: a flaky search tool exhausting the retry budget delays all other tool calls in the same turn, and a misconfigured enrichment tool timing out repeatedly stalls data-fetch tools that would otherwise succeed. Implement tool group isolation that assigns tools to independent failure domains, bounds the failure budget per group, and prevents one group's failures from consuming shared resources needed by other groups."
date: 2026-04-16
difficulty: advanced
category: reliability
slug: agent-doesnt-implement-cascading-failure-isolation-between-tool-groups
tags: [cascading-failure, failure-isolation, tool-groups, bulkhead-pattern, failure-domain, resource-isolation]
symptoms:
  - "A failing search tool consumes all retry slots, delaying database and API tools in the same turn"
  - "One slow enrichment tool timeout causes the entire tool-execution phase to run over the SLO"
  - "Tool failures in one functional area (search) propagate to unrelated areas (notifications)"
  - "No per-group concurrency or timeout budgets — all tools share one global pool"
  - "A systematic failure in one tool group brings down the agent's ability to use any other tool"
---

## Why This Happens

When all tools compete for the same retry budget, concurrency slots, and timeout windows, a failure in one area depletes shared resources. The bulkhead pattern from fault-tolerance engineering solves this: assign tools to isolated groups, each with its own concurrency limit, retry budget, and timeout ceiling. A failure in the search group can exhaust its own budget without affecting the database group. Implementing bulkheads for tools requires grouping tools by failure domain (search, data-fetch, notification, enrichment), assigning independent semaphores and budgets to each group, and monitoring each group's health independently.

## Solution 1: Tool Group Definition

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Set


class ToolGroupKind(str, Enum):
    SEARCH = "search"
    DATA_FETCH = "data_fetch"
    ENRICHMENT = "enrichment"
    NOTIFICATION = "notification"
    WRITE_OPERATION = "write_operation"
    AI_INFERENCE = "ai_inference"
    CUSTOM = "custom"


@dataclass
class ToolGroupConfig:
    group_id: str
    kind: ToolGroupKind
    tool_names: Set[str]
    max_concurrent: int = 3         # semaphore size for this group
    max_retries: int = 2
    group_timeout_seconds: float = 15.0   # all tools in group must finish within this
    per_tool_timeout_seconds: float = 8.0
    failure_threshold: int = 3      # open group circuit after N failures
    tags: List[str] = field(default_factory=list)

    def contains(self, tool_name: str) -> bool:
        return tool_name in self.tool_names
```

## Solution 2: Tool Group Bulkhead

```python
import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional


class BulkheadState(str, Enum):
    OPEN = "open"           # accepting calls
    SATURATED = "saturated" # at concurrency limit
    TRIPPED = "tripped"     # failure threshold exceeded


@dataclass
class BulkheadMetrics:
    group_id: str
    active_calls: int
    total_calls: int
    failed_calls: int
    rejected_calls: int
    tripped_at: Optional[float]
    state: BulkheadState


class ToolGroupBulkhead:
    """
    Enforces concurrency limits and failure circuit for a single tool group.
    Calls to the group are rejected when at capacity or when the failure
    threshold has been exceeded.
    """

    def __init__(self, config: ToolGroupConfig):
        self._config = config
        self._semaphore = asyncio.Semaphore(config.max_concurrent)
        self._active = 0
        self._total = 0
        self._failures = 0
        self._rejected = 0
        self._tripped_at: Optional[float] = None
        self._last_success_at: Optional[float] = None

    @property
    def state(self) -> BulkheadState:
        if self._tripped_at:
            return BulkheadState.TRIPPED
        if self._active >= self._config.max_concurrent:
            return BulkheadState.SATURATED
        return BulkheadState.OPEN

    def is_available(self) -> bool:
        if self._tripped_at:
            # Auto-reset after double the group timeout
            if time.time() - self._tripped_at > self._config.group_timeout_seconds * 2:
                self._tripped_at = None
                self._failures = 0
                return True
            return False
        return True

    async def execute(
        self,
        tool_fn: Callable,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        if not self.is_available():
            self._rejected += 1
            raise BulkheadTrippedError(self._config.group_id, self._failures)

        try:
            acquired = await asyncio.wait_for(
                self._semaphore.acquire(),
                timeout=0.1,  # don't wait for a slot — fail fast if saturated
            )
        except asyncio.TimeoutError:
            self._rejected += 1
            raise BulkheadSaturatedError(self._config.group_id, self._active)

        self._active += 1
        self._total += 1
        start = time.monotonic()

        try:
            result = await asyncio.wait_for(
                tool_fn(*args, **kwargs),
                timeout=self._config.per_tool_timeout_seconds,
            )
            self._last_success_at = time.time()
            return result
        except Exception as exc:
            self._failures += 1
            if self._failures >= self._config.failure_threshold:
                self._tripped_at = time.time()
            raise
        finally:
            self._active -= 1
            self._semaphore.release()

    def metrics(self) -> BulkheadMetrics:
        return BulkheadMetrics(
            group_id=self._config.group_id,
            active_calls=self._active,
            total_calls=self._total,
            failed_calls=self._failures,
            rejected_calls=self._rejected,
            tripped_at=self._tripped_at,
            state=self.state,
        )


class BulkheadTrippedError(Exception):
    def __init__(self, group_id: str, failure_count: int):
        super().__init__(f"tool group '{group_id}' circuit tripped after {failure_count} failures")
        self.group_id = group_id


class BulkheadSaturatedError(Exception):
    def __init__(self, group_id: str, active: int):
        super().__init__(f"tool group '{group_id}' saturated (active={active})")
        self.group_id = group_id
```

## Solution 3: Tool Group Registry

```python
from typing import Dict, List, Optional


class ToolGroupRegistry:
    """
    Maps tool names to their group and manages group bulkheads.
    Tools not assigned to any group are placed in a default group.
    """

    DEFAULT_GROUP_ID = "default"

    def __init__(self):
        self._groups: Dict[str, ToolGroupConfig] = {}
        self._bulkheads: Dict[str, ToolGroupBulkhead] = {}
        self._tool_to_group: Dict[str, str] = {}

    def register_group(self, config: ToolGroupConfig) -> None:
        self._groups[config.group_id] = config
        self._bulkheads[config.group_id] = ToolGroupBulkhead(config)
        for tool_name in config.tool_names:
            self._tool_to_group[tool_name] = config.group_id

    def group_for_tool(self, tool_name: str) -> str:
        return self._tool_to_group.get(tool_name, self.DEFAULT_GROUP_ID)

    def bulkhead_for_tool(self, tool_name: str) -> Optional[ToolGroupBulkhead]:
        group_id = self.group_for_tool(tool_name)
        return self._bulkheads.get(group_id)

    def all_metrics(self) -> List[BulkheadMetrics]:
        return [bh.metrics() for bh in self._bulkheads.values()]
```

## Solution 4: Isolated Tool Dispatcher

```python
import asyncio
import time
from typing import Any, Callable, Dict, List, Optional


class IsolatedToolDispatcher:
    """
    Dispatches tool calls through their group's bulkhead.
    Runs tools from the same group under shared isolation,
    preventing cross-group failure propagation.
    """

    def __init__(
        self,
        registry: ToolGroupRegistry,
        audit_logger: "IsolationAuditLogger",
    ):
        self._registry = registry
        self._logger = audit_logger

    async def dispatch(
        self,
        tool_name: str,
        tool_fn: Callable,
        *args: Any,
        session_id: str = "",
        **kwargs: Any,
    ) -> dict:
        bulkhead = self._registry.bulkhead_for_tool(tool_name)
        group_id = self._registry.group_for_tool(tool_name)
        start = time.monotonic()

        if bulkhead is None:
            # No bulkhead registered — call directly
            result = await tool_fn(*args, **kwargs)
            return {"result": result, "group_id": "unregistered", "isolated": False}

        try:
            result = await bulkhead.execute(tool_fn, *args, **kwargs)
            latency_ms = round((time.monotonic() - start) * 1000, 2)
            self._logger.record_success(tool_name, group_id, latency_ms, session_id)
            return {
                "result": result,
                "group_id": group_id,
                "isolated": True,
                "latency_ms": latency_ms,
            }
        except (BulkheadTrippedError, BulkheadSaturatedError) as exc:
            self._logger.record_rejection(tool_name, group_id, str(exc), session_id)
            raise
        except Exception as exc:
            latency_ms = round((time.monotonic() - start) * 1000, 2)
            self._logger.record_failure(tool_name, group_id, str(exc), latency_ms, session_id)
            raise
```

## Solution 5: Group-Level Timeout Coordinator

```python
import asyncio
from typing import Any, Callable, Dict, List, Tuple


class GroupLevelTimeoutCoordinator:
    """
    Executes a batch of tool calls across groups with per-group timeout enforcement.
    Tools in the same group share a timeout window; different groups run in parallel.
    """

    def __init__(self, registry: ToolGroupRegistry):
        self._registry = registry

    async def execute_batch(
        self,
        calls: List[Tuple[str, Callable, tuple, dict]],
        # (tool_name, tool_fn, args, kwargs)
    ) -> Dict[str, Any]:
        # Group calls by group_id
        by_group: Dict[str, List] = {}
        for tool_name, tool_fn, args, kwargs in calls:
            group_id = self._registry.group_for_tool(tool_name)
            by_group.setdefault(group_id, []).append((tool_name, tool_fn, args, kwargs))

        # Execute groups in parallel, each with its own timeout
        group_tasks = {}
        for group_id, group_calls in by_group.items():
            config = self._registry._groups.get(group_id)
            timeout = config.group_timeout_seconds if config else 15.0
            group_tasks[group_id] = asyncio.create_task(
                self._execute_group(group_calls, timeout)
            )

        results = {}
        for group_id, task in group_tasks.items():
            try:
                group_results = await task
                results.update(group_results)
            except Exception as exc:
                results[group_id] = {"error": str(exc)}

        return results

    async def _execute_group(
        self, calls: list, timeout: float
    ) -> Dict[str, Any]:
        async def _call(tool_name, tool_fn, args, kwargs):
            result = await tool_fn(*args, **kwargs)
            return tool_name, result

        tasks = [asyncio.create_task(_call(*call)) for call in calls]
        try:
            done, pending = await asyncio.wait(tasks, timeout=timeout)
            for t in pending:
                t.cancel()
            return {name: result for t in done for name, result in [t.result()]}
        except Exception:
            return {}
```

## Solution 6: Isolation Audit Logger

```python
import time
from typing import List


class IsolationAuditLogger:
    def __init__(self, max_records: int = 20000):
        self._max = max_records
        self._records: List[dict] = []

    def record_success(self, tool: str, group: str, latency_ms: float, session: str) -> None:
        self._append({"event": "success", "tool": tool, "group": group, "latency_ms": latency_ms, "session": session})

    def record_failure(self, tool: str, group: str, error: str, latency_ms: float, session: str) -> None:
        self._append({"event": "failure", "tool": tool, "group": group, "error": error[:100], "latency_ms": latency_ms, "session": session})

    def record_rejection(self, tool: str, group: str, reason: str, session: str) -> None:
        self._append({"event": "rejection", "tool": tool, "group": group, "reason": reason[:100], "session": session})

    def _append(self, record: dict) -> None:
        if len(self._records) >= self._max:
            self._records.pop(0)
        record["ts"] = time.time()
        self._records.append(record)

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [r for r in self._records if r["ts"] >= cutoff]
        by_group: dict = {}
        for r in recent:
            g = r.get("group", "unknown")
            if g not in by_group:
                by_group[g] = {"success": 0, "failure": 0, "rejection": 0}
            by_group[g][r.get("event", "success")] = by_group[g].get(r.get("event"), 0) + 1
        return {
            "window_seconds": window_seconds,
            "events": len(recent),
            "by_group": by_group,
        }
```

## Comparison

| Approach | Concurrency Limit | Failure Circuit | Cross-Group Isolation | Group Timeout | Audit Log |
|---|---|---|---|---|---|
| ToolGroupBulkhead | Yes (semaphore) | Yes (threshold) | Via registry | No | No |
| ToolGroupRegistry | No | Via bulkheads | Yes (tool mapping) | No | No |
| IsolatedToolDispatcher | Via bulkhead | Via bulkhead | Yes | No | Via logger |
| GroupLevelTimeoutCoordinator | No | No | Yes | Yes (per-group) | No |
| IsolationAuditLogger | No | No | No | No | Yes |

**Best for production**: Assign tools to groups based on their failure mode and latency profile, not their functional category — a search tool and a notification tool that both call the same external vendor belong in the same group because their failures are correlated. Set `failure_threshold=3` and auto-reset after `group_timeout_seconds * 2` — this gives the tripped group time to recover without requiring manual intervention. Set `max_concurrent` per group based on the tool's upstream capacity: if the search API has a rate limit of 10 RPS and average latency is 500ms, the safe concurrency is ~5. Monitor tripped groups via `all_metrics()`: a group that trips multiple times per hour needs either capacity investigation at the upstream service or a higher `failure_threshold` if the failures are transient noise.
