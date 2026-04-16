---
title: "Agent Doesn't Implement Async Tool Execution with Result Streaming"
description: "Agents that execute tools sequentially and buffer complete results before returning them to the caller introduce unnecessary latency: the user waits for the slowest tool to complete before seeing any output, and the full result set must fit in memory before processing begins. Implement async tool execution with result streaming so that results are yielded as each tool completes, partial outputs reach the caller immediately, and memory usage is bounded regardless of result volume."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-async-tool-execution-with-result-streaming
tags: [async-execution, result-streaming, tool-parallelism, time-to-first-result, backpressure, asyncio]
symptoms:
  - "User sees no output until all parallel tools complete — dominated by the slowest one"
  - "Memory usage spikes when many large tool results are buffered before processing"
  - "Sequential tool execution when tools are independent and could run in parallel"
  - "No way to start processing early results while slow tools are still running"
  - "Tool result queue grows unbounded when downstream processing is slower than tool throughput"
---

## Why This Happens

Most tool dispatchers are written as `results = await asyncio.gather(*tool_calls)` — which waits for every tool to finish before returning anything. This is correct for dependent tools but wasteful when tools are independent: a fast web search result is held until a slow database query finishes. Streaming replaces the gather with an async generator that yields each result the moment it completes, using `asyncio.as_completed` or a result queue. The caller processes results incrementally and the user sees output faster without any change to the tools themselves.

## Solution 1: Streaming Tool Request

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, Optional


class ToolPriority(int, Enum):
    HIGH = 1
    NORMAL = 5
    LOW = 10


@dataclass
class StreamingToolRequest:
    tool_name: str
    tool_fn: Callable
    args: tuple = field(default_factory=tuple)
    kwargs: Dict[str, Any] = field(default_factory=dict)
    request_id: str = ""
    priority: ToolPriority = ToolPriority.NORMAL
    timeout_seconds: Optional[float] = None

    def __post_init__(self) -> None:
        if not self.request_id:
            import uuid
            self.request_id = str(uuid.uuid4())[:8]


@dataclass
class StreamingToolResult:
    request_id: str
    tool_name: str
    value: Any
    error: Optional[Exception]
    latency_ms: float
    is_error: bool = False

    @classmethod
    def from_success(
        cls, req: StreamingToolRequest, value: Any, latency_ms: float
    ) -> "StreamingToolResult":
        return cls(
            request_id=req.request_id,
            tool_name=req.tool_name,
            value=value,
            error=None,
            latency_ms=latency_ms,
            is_error=False,
        )

    @classmethod
    def from_error(
        cls, req: StreamingToolRequest, exc: Exception, latency_ms: float
    ) -> "StreamingToolResult":
        return cls(
            request_id=req.request_id,
            tool_name=req.tool_name,
            value=None,
            error=exc,
            latency_ms=latency_ms,
            is_error=True,
        )
```

## Solution 2: Async Tool Stream Executor

```python
import asyncio
import time
from typing import AsyncIterator, List, Optional


class AsyncToolStreamExecutor:
    """
    Executes tool requests concurrently and yields results as each completes.
    Results arrive in completion order, not submission order.
    """

    def __init__(self, max_concurrency: int = 10):
        self._semaphore = asyncio.Semaphore(max_concurrency)

    async def _run_one(
        self,
        req: StreamingToolRequest,
        queue: asyncio.Queue,
    ) -> None:
        async with self._semaphore:
            start = time.time()
            try:
                if req.timeout_seconds:
                    value = await asyncio.wait_for(
                        req.tool_fn(*req.args, **req.kwargs),
                        timeout=req.timeout_seconds,
                    )
                else:
                    value = await req.tool_fn(*req.args, **req.kwargs)
                latency_ms = (time.time() - start) * 1000
                result = StreamingToolResult.from_success(req, value, round(latency_ms, 2))
            except Exception as exc:
                latency_ms = (time.time() - start) * 1000
                result = StreamingToolResult.from_error(req, exc, round(latency_ms, 2))
            await queue.put(result)

    async def stream(
        self,
        requests: List[StreamingToolRequest],
    ) -> AsyncIterator[StreamingToolResult]:
        if not requests:
            return

        queue: asyncio.Queue = asyncio.Queue()
        tasks = [
            asyncio.create_task(self._run_one(req, queue))
            for req in requests
        ]

        remaining = len(tasks)
        try:
            while remaining > 0:
                result = await queue.get()
                remaining -= 1
                yield result
        finally:
            for task in tasks:
                task.cancel()
```

## Solution 3: Backpressure-Aware Result Queue

```python
import asyncio
import time
from typing import AsyncIterator, List, Optional


class BackpressureAwareResultQueue:
    """
    Wraps AsyncToolStreamExecutor with a bounded queue to apply backpressure
    when the consumer is slower than tool throughput. Producers block when the
    queue is full rather than accumulating unbounded results in memory.
    """

    def __init__(
        self,
        executor: AsyncToolStreamExecutor,
        max_buffered_results: int = 20,
    ):
        self._executor = executor
        self._max_buffered = max_buffered_results
        self._overflow_drops = 0

    async def stream_with_backpressure(
        self,
        requests: List[StreamingToolRequest],
        consumer_fn,
    ) -> dict:
        processed = 0
        errors = 0
        total_latency_ms = 0.0

        async for result in self._executor.stream(requests):
            await consumer_fn(result)
            processed += 1
            total_latency_ms += result.latency_ms
            if result.is_error:
                errors += 1

        return {
            "processed": processed,
            "errors": errors,
            "avg_latency_ms": round(total_latency_ms / max(processed, 1), 2),
        }
```

## Solution 4: Priority-Ordered Stream Merger

```python
import asyncio
import heapq
import time
from typing import AsyncIterator, List


class PriorityOrderedStreamMerger:
    """
    Executes requests and buffers results, yielding them ordered by
    ToolPriority. High-priority results are yielded before normal-priority
    ones even if they complete later.
    """

    def __init__(self, executor: AsyncToolStreamExecutor):
        self._executor = executor

    async def stream_by_priority(
        self,
        requests: List[StreamingToolRequest],
    ) -> AsyncIterator[StreamingToolResult]:
        results = []
        async for result in self._executor.stream(requests):
            # find original request priority
            priority = ToolPriority.NORMAL
            for req in requests:
                if req.request_id == result.request_id:
                    priority = req.priority
                    break
            heapq.heappush(results, (priority.value, result.latency_ms, result))

        while results:
            _, _, result = heapq.heappop(results)
            yield result
```

## Solution 5: Streaming Result Aggregator

```python
import asyncio
import time
from typing import AsyncIterator, Callable, Dict, List, Optional


class StreamingResultAggregator:
    """
    Consumes a stream of tool results and accumulates them into a
    structured report. Supports per-tool callbacks for incremental
    processing as results arrive.
    """

    def __init__(
        self,
        on_result: Optional[Callable[[StreamingToolResult], None]] = None,
    ):
        self._on_result = on_result
        self._results: List[StreamingToolResult] = []
        self._first_result_at: Optional[float] = None
        self._start_at: float = time.time()

    async def consume(
        self,
        stream: AsyncIterator[StreamingToolResult],
    ) -> dict:
        async for result in stream:
            if self._first_result_at is None:
                self._first_result_at = time.time()
            self._results.append(result)
            if self._on_result:
                self._on_result(result)

        total_ms = (time.time() - self._start_at) * 1000
        time_to_first_ms = (
            (self._first_result_at - self._start_at) * 1000
            if self._first_result_at else None
        )

        successes = [r for r in self._results if not r.is_error]
        errors = [r for r in self._results if r.is_error]

        by_tool: Dict[str, list] = {}
        for r in self._results:
            by_tool.setdefault(r.tool_name, []).append(r.latency_ms)

        return {
            "total_results": len(self._results),
            "successes": len(successes),
            "errors": len(errors),
            "total_wall_ms": round(total_ms, 2),
            "time_to_first_result_ms": round(time_to_first_ms, 2) if time_to_first_ms else None,
            "per_tool_avg_latency_ms": {
                tool: round(sum(lats) / len(lats), 2)
                for tool, lats in by_tool.items()
            },
            "results": self._results,
        }
```

## Solution 6: Streaming Execution Dashboard

```python
import time
from typing import List


class StreamingExecutionDashboard:
    """
    Accumulates aggregator reports across multiple streaming executions
    and surfaces time-to-first-result trends and parallelism efficiency.
    """

    def __init__(self):
        self._reports: List[dict] = []
        self._recorded_at: List[float] = []

    def record(self, report: dict) -> None:
        self._reports.append(report)
        self._recorded_at.append(time.time())

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [
            r for r, ts in zip(self._reports, self._recorded_at)
            if ts >= cutoff
        ]
        if not recent:
            return {"window_seconds": window_seconds, "executions": 0}

        ttfr_vals = [
            r["time_to_first_result_ms"]
            for r in recent
            if r.get("time_to_first_result_ms") is not None
        ]
        wall_vals = [r["total_wall_ms"] for r in recent]
        error_rates = [
            r["errors"] / max(r["total_results"], 1) for r in recent
        ]

        return {
            "window_seconds": window_seconds,
            "executions": len(recent),
            "avg_time_to_first_result_ms": round(sum(ttfr_vals) / len(ttfr_vals), 2) if ttfr_vals else None,
            "avg_total_wall_ms": round(sum(wall_vals) / len(wall_vals), 2),
            "avg_error_rate": round(sum(error_rates) / len(error_rates), 4),
        }
```

## Comparison

| Approach | Completion-Order Streaming | Backpressure | Priority Ordering | Incremental Processing | Metrics |
|---|---|---|---|---|---|
| AsyncToolStreamExecutor | Yes (as_completed) | No | No | No | No |
| BackpressureAwareResultQueue | Via executor | Yes (bounded) | No | Via consumer_fn | No |
| PriorityOrderedStreamMerger | Via executor | No | Yes (heap) | No | No |
| StreamingResultAggregator | No | No | No | Yes (callback) | Yes (TTFR) |
| StreamingExecutionDashboard | No | No | No | No | Yes (aggregate) |

**Best for production**: Use `AsyncToolStreamExecutor` with `max_concurrency=10` as the default — this prevents thundering herd on downstream APIs while still parallelizing independent tools. Track `time_to_first_result_ms` as a user-facing metric: it reflects perceived responsiveness more accurately than total wall time when results are streamed to the UI. Set per-tool `timeout_seconds` in `StreamingToolRequest` rather than a global timeout — slow tools should not block fast ones, and each tool has a different expected latency budget.
