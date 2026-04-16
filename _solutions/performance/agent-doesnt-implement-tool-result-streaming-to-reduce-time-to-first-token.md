---
title: "Agent Doesn't Implement Tool Result Streaming to Reduce Time to First Token"
description: "Agents that wait for all tool results to arrive before starting the next LLM call force the user to wait for the slowest tool in a parallel batch before seeing any response. When tools return large, independently useful results, streaming partial results to the LLM as they arrive — rather than batching them all — allows the LLM to begin generating a response with the available data while slower tools continue executing, reducing perceived latency."
date: 2026-04-16
difficulty: advanced
category: performance
slug: agent-doesnt-implement-tool-result-streaming-to-reduce-time-to-first-token
tags: [streaming, time-to-first-token, tool-result-streaming, partial-results, progressive-response, latency-reduction]
symptoms:
  - "User sees no output until the slowest tool in a parallel batch completes"
  - "Fast tool results sit idle waiting for a slow tool before being forwarded to the LLM"
  - "Time-to-first-token equals the latency of the slowest parallel tool call"
  - "No progressive response even when some tool results are available immediately"
  - "All-or-nothing batch dispatch with no ability to send partial results"
---

## Why This Happens

The standard agentic loop collects all tool results before constructing the next LLM message. When tools run in parallel, this means waiting for the slowest tool. If a fast tool (100ms) and a slow tool (5000ms) run concurrently, the user waits 5000ms before the LLM sees any results. Streaming partial results changes this: as each tool completes, its result is forwarded to a streaming LLM call. The LLM can begin generating a response with the available data, streaming tokens to the user, while the slow tool continues in the background. The final response incorporates all tool results once they arrive.

## Solution 1: Streaming Tool Result

```python
import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class ToolResultAvailability(str, Enum):
    PENDING = "pending"
    AVAILABLE = "available"
    FAILED = "failed"


@dataclass
class StreamingToolResult:
    tool_use_id: str
    tool_name: str
    dispatch_index: int
    availability: ToolResultAvailability = ToolResultAvailability.PENDING
    content: Optional[Any] = None
    error: Optional[str] = None
    arrived_at: Optional[float] = None
    latency_ms: float = 0.0

    def to_partial_message_block(self) -> dict:
        if self.availability == ToolResultAvailability.AVAILABLE:
            return {
                "type": "tool_result",
                "tool_use_id": self.tool_use_id,
                "content": str(self.content) if not isinstance(self.content, str) else self.content,
            }
        return {
            "type": "tool_result",
            "tool_use_id": self.tool_use_id,
            "is_error": True,
            "content": self.error or "tool failed",
        }
```

## Solution 2: Result Arrival Queue

```python
import asyncio
from typing import AsyncIterator, List, Optional


class ToolResultArrivalQueue:
    """
    Async queue that yields tool results in arrival order (not dispatch order).
    Producers add results as tools complete; consumers iterate as they arrive.
    """

    def __init__(self, expected_count: int):
        self._queue: asyncio.Queue = asyncio.Queue()
        self._expected = expected_count
        self._received = 0

    def put_result(self, result: StreamingToolResult) -> None:
        self._received += 1
        self._queue.put_nowait(result)

    async def results_as_they_arrive(
        self,
        timeout_seconds: float = 60.0,
    ) -> AsyncIterator[StreamingToolResult]:
        """
        Async generator that yields each result as it arrives.
        Stops after all expected results are received or timeout.
        """
        deadline = asyncio.get_event_loop().time() + timeout_seconds
        yielded = 0
        while yielded < self._expected:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                break
            try:
                result = await asyncio.wait_for(
                    self._queue.get(), timeout=remaining
                )
                yield result
                yielded += 1
            except asyncio.TimeoutError:
                break

    def is_complete(self) -> bool:
        return self._received >= self._expected
```

## Solution 3: Progressive Tool Dispatcher

```python
import asyncio
import time
from typing import Any, Callable, Dict, List


class ProgressiveToolDispatcher:
    """
    Dispatches tools concurrently and feeds results into an arrival queue
    as each tool completes — enabling the LLM to start processing
    before all tools have finished.
    """

    def __init__(self, tool_registry: Dict[str, Callable]):
        self._registry = tool_registry

    def dispatch_progressive(
        self,
        dispatches: List[tuple],  # [(tool_use_id, tool_name, index, input_dict)]
        timeout_seconds: float = 60.0,
    ) -> ToolResultArrivalQueue:
        queue = ToolResultArrivalQueue(expected_count=len(dispatches))

        async def _run_one(tool_use_id: str, tool_name: str, index: int, tool_input: dict) -> None:
            start = time.time()
            tool_fn = self._registry.get(tool_name)
            if tool_fn is None:
                queue.put_result(StreamingToolResult(
                    tool_use_id=tool_use_id,
                    tool_name=tool_name,
                    dispatch_index=index,
                    availability=ToolResultAvailability.FAILED,
                    error=f"tool '{tool_name}' not registered",
                    latency_ms=0.0,
                ))
                return
            try:
                content = await asyncio.wait_for(
                    tool_fn(**tool_input), timeout=timeout_seconds
                )
                queue.put_result(StreamingToolResult(
                    tool_use_id=tool_use_id,
                    tool_name=tool_name,
                    dispatch_index=index,
                    availability=ToolResultAvailability.AVAILABLE,
                    content=content,
                    arrived_at=time.time(),
                    latency_ms=round((time.time() - start) * 1000, 2),
                ))
            except Exception as exc:
                queue.put_result(StreamingToolResult(
                    tool_use_id=tool_use_id,
                    tool_name=tool_name,
                    dispatch_index=index,
                    availability=ToolResultAvailability.FAILED,
                    error=str(exc),
                    arrived_at=time.time(),
                    latency_ms=round((time.time() - start) * 1000, 2),
                ))

        for args in dispatches:
            asyncio.ensure_future(_run_one(*args))

        return queue
```

## Solution 4: Streaming LLM Context Builder

```python
import asyncio
from typing import Any, AsyncIterator, Callable, List


class StreamingContextBuilder:
    """
    Consumes tool results from the arrival queue and streams incremental
    LLM calls as results arrive. Each new result triggers a continuation
    LLM call that incorporates the newly available data.
    """

    def __init__(
        self,
        llm_stream_fn: Callable,   # async generator fn(messages) -> token stream
    ):
        self._llm_stream_fn = llm_stream_fn

    async def stream_with_partial_results(
        self,
        base_messages: List[dict],
        arrival_queue: ToolResultArrivalQueue,
        timeout_seconds: float = 60.0,
    ) -> AsyncIterator[str]:
        """
        Yields LLM response tokens as tool results arrive.
        Sends an initial LLM call with whatever results are available,
        then continues incorporating new results as they come in.
        """
        accumulated_results: List[StreamingToolResult] = []

        async for result in arrival_queue.results_as_they_arrive(timeout_seconds):
            accumulated_results.append(result)

            # Build messages with currently available tool results
            tool_result_blocks = [r.to_partial_message_block() for r in accumulated_results]
            messages = base_messages + [
                {"role": "user", "content": tool_result_blocks}
            ]

            # If this is the last result, do a final streaming call
            if arrival_queue.is_complete():
                async for token in self._llm_stream_fn(messages):
                    yield token
                return

        # Fallback: yield with whatever we have
        if accumulated_results:
            tool_result_blocks = [r.to_partial_message_block() for r in accumulated_results]
            messages = base_messages + [
                {"role": "user", "content": tool_result_blocks}
            ]
            async for token in self._llm_stream_fn(messages):
                yield token
```

## Solution 5: Streaming Latency Tracker

```python
import time
from typing import List, Optional


class StreamingLatencyTracker:
    """
    Tracks time-to-first-token and time-to-complete for streaming responses.
    Compares streaming vs batch latency to quantify the benefit.
    """

    def __init__(self):
        self._records: List[dict] = []

    def record(
        self,
        request_start: float,
        first_token_at: Optional[float],
        complete_at: float,
        batch_complete_at: Optional[float],  # when all tools would have been done
    ) -> None:
        ttft = round((first_token_at - request_start) * 1000, 2) if first_token_at else None
        total = round((complete_at - request_start) * 1000, 2)
        batch_wait = round((batch_complete_at - request_start) * 1000, 2) if batch_complete_at else None
        saved_ms = round((batch_complete_at - first_token_at) * 1000, 2) if (batch_complete_at and first_token_at) else None
        self._records.append({
            "ts": time.time(),
            "ttft_ms": ttft,
            "total_ms": total,
            "batch_wait_ms": batch_wait,
            "streaming_saved_ms": saved_ms,
        })

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [r for r in self._records if r["ts"] >= cutoff]
        if not recent:
            return {"window_seconds": window_seconds, "requests": 0}

        ttfts = [r["ttft_ms"] for r in recent if r["ttft_ms"] is not None]
        savings = [r["streaming_saved_ms"] for r in recent if r["streaming_saved_ms"] is not None]
        return {
            "window_seconds": window_seconds,
            "requests": len(recent),
            "avg_ttft_ms": round(sum(ttfts) / len(ttfts), 2) if ttfts else None,
            "avg_streaming_saved_ms": round(sum(savings) / len(savings), 2) if savings else None,
        }
```

## Solution 6: Streaming Tool Result Dashboard

```python
import time


class StreamingToolResultDashboard:
    """
    Renders progressive dispatch and streaming latency stats.
    """

    def __init__(self, tracker: StreamingLatencyTracker):
        self._tracker = tracker

    def render(self, window_seconds: float = 3600.0) -> dict:
        return {
            "generated_at": time.time(),
            "streaming_latency": self._tracker.summary(window_seconds),
        }
```

## Comparison

| Approach | Arrival-Order Queue | Progressive Dispatch | Streaming LLM Call | TTFT Tracking | Dashboard |
|---|---|---|---|---|---|
| ToolResultArrivalQueue | Yes (async gen) | No | No | No | No |
| ProgressiveToolDispatcher | Via queue | Yes | No | No | No |
| StreamingContextBuilder | Via queue | No | Yes | No | No |
| StreamingLatencyTracker | No | No | No | Yes | No |
| StreamingToolResultDashboard | No | No | No | No | Yes |

**Best for production**: Use progressive streaming only when tools have heterogeneous latencies — if all tools complete within 200ms of each other, the overhead of multiple LLM calls exceeds the TTFT benefit. Monitor `avg_streaming_saved_ms` via `StreamingLatencyTracker`: if it is consistently above 1000ms, progressive streaming is meaningfully reducing user-perceived latency. Ensure the LLM call in `StreamingContextBuilder` is idempotent with respect to the tool results already sent — the final call must include all tool results in correct API order even when prior partial calls were made. Set `timeout_seconds` to the P99 latency of your slowest tool; tools that miss the deadline receive an error result and the final LLM call proceeds with a partial tool set.
