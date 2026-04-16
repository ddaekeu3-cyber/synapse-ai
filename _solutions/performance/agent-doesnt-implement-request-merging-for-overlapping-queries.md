---
title: "Agent Doesn't Implement Request Merging for Overlapping Queries"
description: "Agents running concurrent tool calls often issue overlapping queries — multiple calls that fetch the same base dataset or differ only in filter criteria. Implement request merging to detect overlapping queries at dispatch time, execute the superset query once, and fan results back to each caller — eliminating redundant network round trips and backend load."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-request-merging-for-overlapping-queries
tags: [request-merging, query-coalescing, deduplication, performance, concurrency, fan-out]
symptoms:
  - "Three parallel tool calls each fetch the same database table with slightly different filters"
  - "Vector search tool called 5 times with different top-k values against the same query embedding"
  - "API calls with overlapping time ranges each trigger a full backend scan"
  - "Backend observability shows repeated identical queries arriving within milliseconds of each other"
  - "No deduplication between concurrent requests issued by parallel agent branches"
---

## Why This Happens

When agents execute multiple parallel branches, those branches often need overlapping data. Branch A needs users created in the last 7 days; Branch B needs users created in the last 30 days. Without request merging, both queries hit the database separately. A merge layer detects that the 7-day query is a subset of the 30-day query, issues only the 30-day query, and slices the results for Branch A — cutting backend load in half. This pattern is especially valuable for vector search (merge top-k queries into top-max-k), time-series queries (merge overlapping ranges), and pagination (merge page fetches from the same cursor).

## Solution 1: Query Overlap Detector

```python
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

@dataclass
class QueryDescriptor:
    query_id: str
    query_type: str       # "time_range" | "top_k" | "filter" | "exact"
    base_key: str         # cache key ignoring variable parts (e.g., table + base filters)
    variable_params: Dict[str, Any] = field(default_factory=dict)
    requested_at: float = field(default_factory=time.time)
    caller_id: str = ""

class QueryOverlapDetector:
    """
    Detects when two queries are subsets of each other or share a common superset.
    Returns a merge plan: which query to execute and how to slice results per caller.
    """

    def can_merge(
        self,
        q1: QueryDescriptor,
        q2: QueryDescriptor,
    ) -> Tuple[bool, Optional[QueryDescriptor], Dict[str, Any]]:
        """
        Returns (can_merge, merged_query, slice_instructions).
        slice_instructions maps caller_id -> filter to apply to merged results.
        """
        if q1.query_type != q2.query_type or q1.base_key != q2.base_key:
            return False, None, {}

        if q1.query_type == "top_k":
            return self._merge_top_k(q1, q2)
        elif q1.query_type == "time_range":
            return self._merge_time_range(q1, q2)
        elif q1.query_type == "exact":
            return self._merge_exact(q1, q2)
        return False, None, {}

    def _merge_top_k(self, q1, q2):
        k1 = q1.variable_params.get("k", 0)
        k2 = q2.variable_params.get("k", 0)
        max_k = max(k1, k2)
        merged = QueryDescriptor(
            query_id=f"merged_{q1.query_id}_{q2.query_id}",
            query_type="top_k",
            base_key=q1.base_key,
            variable_params={"k": max_k},
        )
        slices = {
            q1.caller_id: {"k": k1},
            q2.caller_id: {"k": k2},
        }
        return True, merged, slices

    def _merge_time_range(self, q1, q2):
        start1 = q1.variable_params.get("start", 0)
        end1 = q1.variable_params.get("end", float("inf"))
        start2 = q2.variable_params.get("start", 0)
        end2 = q2.variable_params.get("end", float("inf"))
        merged_start = min(start1, start2)
        merged_end = max(end1, end2)
        merged = QueryDescriptor(
            query_id=f"merged_{q1.query_id}_{q2.query_id}",
            query_type="time_range",
            base_key=q1.base_key,
            variable_params={"start": merged_start, "end": merged_end},
        )
        slices = {
            q1.caller_id: {"start": start1, "end": end1},
            q2.caller_id: {"start": start2, "end": end2},
        }
        return True, merged, slices

    def _merge_exact(self, q1, q2):
        if q1.variable_params == q2.variable_params:
            merged = QueryDescriptor(
                query_id=f"merged_{q1.query_id}_{q2.query_id}",
                query_type="exact",
                base_key=q1.base_key,
                variable_params=q1.variable_params,
            )
            slices = {q1.caller_id: {}, q2.caller_id: {}}
            return True, merged, slices
        return False, None, {}
```

## Solution 2: Merge Window Collector

```python
import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine, Dict, List, Optional, Tuple

@dataclass
class PendingQuery:
    descriptor: QueryDescriptor
    future: asyncio.Future
    registered_at: float = field(default_factory=time.time)

class MergeWindowCollector:
    """
    Collects queries arriving within a short window (default 5ms) and
    attempts to merge them before execution. Fires the merged query once
    the window closes or the max batch size is reached.
    """

    def __init__(
        self,
        executor: Callable[[QueryDescriptor], Coroutine],
        window_ms: float = 5.0,
        max_batch_size: int = 20,
    ):
        self._execute = executor
        self._window = window_ms / 1000.0
        self._max_batch = max_batch_size
        self._pending: List[PendingQuery] = []
        self._lock = asyncio.Lock()
        self._flush_task: Optional[asyncio.Task] = None
        self._detector = QueryOverlapDetector()
        self._merges_saved = 0

    async def submit(self, descriptor: QueryDescriptor) -> Any:
        """Submit a query. Returns result after merge window closes."""
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        async with self._lock:
            self._pending.append(PendingQuery(descriptor=descriptor, future=future))
            if len(self._pending) >= self._max_batch:
                await self._flush()
            elif self._flush_task is None or self._flush_task.done():
                self._flush_task = asyncio.ensure_future(self._schedule_flush())

        return await future

    async def _schedule_flush(self) -> None:
        await asyncio.sleep(self._window)
        async with self._lock:
            await self._flush()

    async def _flush(self) -> None:
        if not self._pending:
            return

        batch = list(self._pending)
        self._pending.clear()

        # Attempt pairwise merges
        merged_groups = self._plan_merges(batch)

        for group, slice_map in merged_groups:
            merged_descriptor = group[0].descriptor
            if len(group) > 1:
                self._merges_saved += len(group) - 1

            try:
                result = await self._execute(merged_descriptor)
                for pq in group:
                    slices = slice_map.get(pq.descriptor.caller_id, {})
                    sliced = self._apply_slice(result, slices, pq.descriptor.query_type)
                    if not pq.future.done():
                        pq.future.set_result(sliced)
            except Exception as exc:
                for pq in group:
                    if not pq.future.done():
                        pq.future.set_exception(exc)

    def _plan_merges(
        self, batch: List[PendingQuery]
    ) -> List[Tuple[List[PendingQuery], Dict[str, Any]]]:
        """Greedy pairwise merge — O(n²) for small batches."""
        unmerged = list(batch)
        groups = []

        while unmerged:
            base = unmerged.pop(0)
            group = [base]
            merged_desc = base.descriptor
            slice_map: Dict[str, Any] = {base.descriptor.caller_id: {}}
            still_unmerged = []

            for other in unmerged:
                can_merge, new_desc, slices = self._detector.can_merge(merged_desc, other.descriptor)
                if can_merge and new_desc:
                    merged_desc = new_desc
                    slice_map.update(slices)
                    group.append(other)
                else:
                    still_unmerged.append(other)

            groups.append((group, slice_map))
            unmerged = still_unmerged

        return groups

    def _apply_slice(self, result: Any, slices: Dict, query_type: str) -> Any:
        if not slices or not result:
            return result
        if query_type == "top_k":
            k = slices.get("k")
            if k and isinstance(result, list):
                return result[:k]
        elif query_type == "time_range":
            start = slices.get("start")
            end = slices.get("end")
            if start is not None and isinstance(result, list):
                return [r for r in result
                        if start <= r.get("timestamp", 0) <= (end or float("inf"))]
        return result

    def stats(self) -> dict:
        return {"merges_saved": self._merges_saved}
```

## Solution 3: Exact-Match Request Coalescer

```python
import asyncio
import hashlib
import json
import time
from typing import Any, Callable, Coroutine, Dict, Optional, Tuple

class ExactMatchCoalescer:
    """
    Deduplicates concurrent identical requests by hashing arguments.
    All callers waiting for the same in-flight request share the result.
    Different from caching: only dedups concurrent requests, not historical ones.
    """

    def __init__(self, executor: Callable[..., Coroutine]):
        self._execute = executor
        self._in_flight: Dict[str, asyncio.Future] = {}
        self._saved = 0

    def _key(self, *args, **kwargs) -> str:
        payload = json.dumps({"args": args, "kwargs": kwargs}, sort_keys=True, default=str)
        return hashlib.sha256(payload.encode()).hexdigest()[:16]

    async def execute(self, *args, **kwargs) -> Any:
        key = self._key(*args, **kwargs)

        if key in self._in_flight:
            self._saved += 1
            return await asyncio.shield(self._in_flight[key])

        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._in_flight[key] = future

        try:
            result = await self._execute(*args, **kwargs)
            future.set_result(result)
            return result
        except Exception as exc:
            future.set_exception(exc)
            raise
        finally:
            self._in_flight.pop(key, None)

    def stats(self) -> dict:
        return {
            "in_flight": len(self._in_flight),
            "saved_executions": self._saved,
        }
```

## Solution 4: Superset Query Planner

```python
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

@dataclass
class SupersetPlan:
    superset_query: Dict[str, Any]
    callers: List[str]
    slice_params: Dict[str, Dict[str, Any]]   # caller_id -> slice params

class SupersetQueryPlanner:
    """
    Given N pending time-range or top-k queries, computes the minimal superset
    query that satisfies all of them, plus per-caller slice instructions.
    Used for batch planning before issuing to the backend.
    """

    def plan_time_ranges(
        self,
        queries: List[Tuple[str, float, float]],   # (caller_id, start, end)
    ) -> SupersetPlan:
        if not queries:
            raise ValueError("no queries to plan")
        global_start = min(q[1] for q in queries)
        global_end = max(q[2] for q in queries)
        return SupersetPlan(
            superset_query={"start": global_start, "end": global_end},
            callers=[q[0] for q in queries],
            slice_params={q[0]: {"start": q[1], "end": q[2]} for q in queries},
        )

    def plan_top_k(
        self,
        queries: List[Tuple[str, int]],   # (caller_id, k)
    ) -> SupersetPlan:
        if not queries:
            raise ValueError("no queries to plan")
        max_k = max(q[1] for q in queries)
        return SupersetPlan(
            superset_query={"k": max_k},
            callers=[q[0] for q in queries],
            slice_params={q[0]: {"k": q[1]} for q in queries},
        )

    def apply_slice(
        self,
        results: List[Any],
        slice_params: Dict[str, Any],
        query_type: str,
    ) -> List[Any]:
        if query_type == "top_k":
            return results[:slice_params.get("k", len(results))]
        elif query_type == "time_range":
            start = slice_params.get("start", 0)
            end = slice_params.get("end", float("inf"))
            return [r for r in results
                    if start <= r.get("timestamp", 0) <= end]
        return results
```

## Solution 5: Merge Metrics Tracker

```python
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Deque, Dict

@dataclass
class MergeEvent:
    query_type: str
    queries_merged: int
    backend_calls_saved: int
    window_ms: float
    timestamp: float

class MergeMetricsTracker:
    """
    Records merge events and computes efficiency metrics.
    Tracks how many backend calls were avoided by merging.
    """

    def __init__(self):
        self._events: Deque[MergeEvent] = deque(maxlen=10_000)
        self._by_type: Dict[str, int] = defaultdict(int)
        self._total_saved = 0

    def record(
        self,
        query_type: str,
        queries_merged: int,
        window_ms: float = 0.0,
    ) -> None:
        saved = queries_merged - 1
        event = MergeEvent(
            query_type=query_type,
            queries_merged=queries_merged,
            backend_calls_saved=saved,
            window_ms=window_ms,
            timestamp=time.time(),
        )
        self._events.append(event)
        self._by_type[query_type] += saved
        self._total_saved += saved

    def efficiency_rate(self, window_seconds: float = 60.0) -> float:
        cutoff = time.time() - window_seconds
        recent = [e for e in self._events if e.timestamp >= cutoff]
        if not recent:
            return 0.0
        total_submitted = sum(e.queries_merged for e in recent)
        total_executed = len(recent)
        return round(1.0 - total_executed / max(total_submitted, 1), 4)

    def summary(self) -> dict:
        return {
            "total_merges": len(self._events),
            "total_backend_calls_saved": self._total_saved,
            "efficiency_rate_last_60s": self.efficiency_rate(60.0),
            "saved_by_type": dict(self._by_type),
        }
```

## Solution 6: Mergeable Query Registry

```python
import asyncio
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine, Dict, List, Optional, Type

@dataclass
class QueryHandler:
    query_type: str
    executor: Callable[[Dict], Coroutine]
    coalescer: ExactMatchCoalescer
    collector: MergeWindowCollector
    planner: SupersetQueryPlanner

class MergeableQueryRegistry:
    """
    Registry that routes query types to the appropriate merge strategy.
    Exact-match queries go through ExactMatchCoalescer.
    Range/top-k queries go through MergeWindowCollector with SupersetQueryPlanner.
    """

    def __init__(self):
        self._handlers: Dict[str, QueryHandler] = {}

    def register(self, handler: QueryHandler) -> None:
        self._handlers[handler.query_type] = handler

    async def execute(
        self,
        query_type: str,
        params: Dict[str, Any],
        caller_id: str = "",
        merge_strategy: str = "coalesce",   # "coalesce" | "window"
    ) -> Any:
        handler = self._handlers.get(query_type)
        if not handler:
            raise KeyError(f"no handler registered for query type '{query_type}'")

        if merge_strategy == "coalesce":
            return await handler.coalescer.execute(**params)
        elif merge_strategy == "window":
            desc = QueryDescriptor(
                query_id=f"{query_type}_{caller_id}_{id(params)}",
                query_type=query_type,
                base_key=query_type,
                variable_params=params,
                caller_id=caller_id,
            )
            return await handler.collector.submit(desc)

        return await handler.executor(params)

    def stats(self) -> dict:
        return {
            query_type: {
                "coalescer": handler.coalescer.stats(),
                "collector": handler.collector.stats(),
            }
            for query_type, handler in self._handlers.items()
        }
```

## Comparison

| Approach | Merge Strategy | Window-Based | Exact Dedup | Slice Results |
|---|---|---|---|---|
| QueryOverlapDetector | Superset detection | No | Yes (exact) | Yes |
| MergeWindowCollector | Batch + superset | Yes (5ms) | No | Yes |
| ExactMatchCoalescer | In-flight dedup | No | Yes | No |
| SupersetQueryPlanner | Superset planning | No | No | Yes |
| MergeMetricsTracker | N/A (metrics) | N/A | N/A | N/A |
| MergeableQueryRegistry | Route by strategy | Via collector | Via coalescer | Via collector |

**Best for production**: Use `ExactMatchCoalescer` for tool calls that are purely deterministic (same args → same result) — this eliminates duplicate concurrent calls with zero added latency. Use `MergeWindowCollector` for query types where overlap is structural (time ranges, top-k vector search) — a 5ms window captures nearly all concurrent agent branches with negligible latency cost. Monitor `MergeMetricsTracker.efficiency_rate()` to verify merging is working; rates below 0.2 on known-overlapping workloads indicate the window is too short or the `base_key` is too specific.
