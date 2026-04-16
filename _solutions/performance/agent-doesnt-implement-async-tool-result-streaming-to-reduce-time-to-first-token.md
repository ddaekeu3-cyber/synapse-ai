---
title: "Agent Doesn't Implement Async Tool Result Streaming to Reduce Time-to-First-Token"
description: "Agents that wait for all tool calls to complete before sending any output to the user introduce unnecessary latency: the user waits for the slowest tool in a parallel batch even when earlier results could already be presented. Implement async tool result streaming that yields partial context as each tool completes, allowing the LLM to begin generating with available results while remaining tools are still in flight."
date: 2026-04-16
difficulty: advanced
category: performance
slug: agent-doesnt-implement-async-tool-result-streaming-to-reduce-time-to-first-token
tags: [streaming, time-to-first-token, async-tools, partial-results, tool-concurrency, latency-reduction]
symptoms:
  - "User sees no output until all parallel tool calls finish — slowest tool blocks everything"
  - "Time-to-first-token equals the duration of the longest concurrent tool call"
  - "Tool results with sub-second latency wait idle while a 10-second tool completes"
  - "No mechanism to yield partial context to the LLM as tools complete"
  - "Parallel tool dispatch exists but output streaming does not"
---

## Why This Happens

Parallel tool dispatch reduces total wall-clock time but does not reduce time-to-first-token if the agent collects all results before making the next LLM call. When one tool in a concurrent batch takes 10 seconds and three others complete in 500ms, those fast results sit idle. Async streaming inverts this: as each tool result arrives it is appended to a growing context, and a streaming LLM call begins with whatever is available — backfilling additional tool results as they arrive using a continuation pattern or by structuring the prompt to accept incremental additions.

## Solution 1: Tool Completion Event

```python
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class ToolCompletionStatus(str, Enum):
    SUCCESS = "success"
    ERROR = "error"
    TIMEOUT = "timeout"


@dataclass
class ToolCompletionEvent:
    tool_name: str
    call_id: str
    status: ToolCompletionStatus
    result: Optional[Any]
    error: Optional[str]
    started_at: float
    completed_at: float = field(default_factory=time.time)

    @property
    def latency_ms(self) -> float:
        return round((self.completed_at - self.started_at) * 1000, 2)
```

## Solution 2: Async Tool Completion Stream

```python
import asyncio
import time
import uuid
from typing import AsyncIterator, Callable, Dict, List, Optional


class AsyncToolCompletionStream:
    """
    Dispatches multiple tool coroutines concurrently and yields
    ToolCompletionEvent objects in arrival order — fastest tool first.
    Callers can begin processing results immediately without waiting for all tools.
    """

    def __init__(self, timeout_seconds: float = 30.0):
        self._timeout = timeout_seconds

    async def stream(
        self,
        tool_calls: List[Dict],
        # Each entry: {"tool_name": str, "call_id": str, "fn": coroutine_fn, "args": dict}
    ) -> AsyncIterator[ToolCompletionEvent]:
        queue: asyncio.Queue = asyncio.Queue()

        async def _run_tool(entry: dict) -> None:
            started = time.time()
            tool_name = entry["tool_name"]
            call_id = entry.get("call_id", uuid.uuid4().hex[:8])
            try:
                result = await asyncio.wait_for(
                    entry["fn"](**entry.get("args", {})),
                    timeout=self._timeout,
                )
                await queue.put(ToolCompletionEvent(
                    tool_name=tool_name, call_id=call_id,
                    status=ToolCompletionStatus.SUCCESS,
                    result=result, error=None, started_at=started,
                ))
            except asyncio.TimeoutError:
                await queue.put(ToolCompletionEvent(
                    tool_name=tool_name, call_id=call_id,
                    status=ToolCompletionStatus.TIMEOUT,
                    result=None, error="timeout", started_at=started,
                ))
            except Exception as exc:
                await queue.put(ToolCompletionEvent(
                    tool_name=tool_name, call_id=call_id,
                    status=ToolCompletionStatus.ERROR,
                    result=None, error=str(exc), started_at=started,
                ))

        tasks = [asyncio.create_task(_run_tool(entry)) for entry in tool_calls]
        remaining = len(tasks)

        while remaining > 0:
            event = await queue.get()
            remaining -= 1
            yield event
```

## Solution 3: Incremental Context Assembler

```python
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ContextSnapshot:
    completed_tools: List[str]
    pending_tools: List[str]
    context_text: str
    is_complete: bool


class IncrementalContextAssembler:
    """
    Builds context incrementally as tool results arrive.
    Each call to add_result produces an updated ContextSnapshot
    that can be used to start or continue an LLM streaming call.
    """

    def __init__(self, all_tool_names: List[str]):
        self._all = list(all_tool_names)
        self._completed: Dict[str, Any] = {}
        self._errors: Dict[str, str] = {}

    def add_result(self, event: ToolCompletionEvent) -> ContextSnapshot:
        if event.status == ToolCompletionStatus.SUCCESS:
            self._completed[event.tool_name] = event.result
        else:
            self._errors[event.tool_name] = event.error or "unknown error"

        done = set(self._completed) | set(self._errors)
        pending = [t for t in self._all if t not in done]

        parts = []
        for name, result in self._completed.items():
            parts.append(f"[{name} result]\n{result}")
        for name, err in self._errors.items():
            parts.append(f"[{name} error]\n{err}")
        if pending:
            parts.append(f"[Pending: {', '.join(pending)}]")

        return ContextSnapshot(
            completed_tools=list(self._completed),
            pending_tools=pending,
            context_text="\n\n".join(parts),
            is_complete=len(pending) == 0,
        )
```

## Solution 4: Early-Start LLM Caller

```python
import asyncio
from typing import Any, AsyncIterator, Callable, List, Optional


class EarlyStartLLMCaller:
    """
    Begins an LLM streaming call as soon as a minimum number of tool results
    are available, rather than waiting for all tools to complete.
    Subsequent tool results are appended as follow-up context.
    """

    def __init__(
        self,
        llm_stream_fn: Callable,          # async generator: (messages) -> AsyncIterator[str]
        min_results_before_start: int = 1,
    ):
        self._llm = llm_stream_fn
        self._min = min_results_before_start

    async def call_with_streaming_tools(
        self,
        system_prompt: str,
        user_message: str,
        tool_stream: AsyncIterator[ToolCompletionEvent],
        assembler: IncrementalContextAssembler,
    ) -> AsyncIterator[str]:
        buffer: List[ToolCompletionEvent] = []
        llm_started = False
        llm_task: Optional[asyncio.Task] = None
        output_queue: asyncio.Queue = asyncio.Queue()

        async def _stream_llm(snapshot: ContextSnapshot) -> None:
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
                {"role": "assistant", "content": snapshot.context_text},
            ]
            async for token in self._llm(messages):
                await output_queue.put(("token", token))
            await output_queue.put(("done", None))

        async def _collect_tools() -> None:
            nonlocal llm_started, llm_task
            async for event in tool_stream:
                snapshot = assembler.add_result(event)
                buffer.append(event)
                if not llm_started and len(buffer) >= self._min:
                    llm_started = True
                    llm_task = asyncio.create_task(_stream_llm(snapshot))
            if not llm_started:
                snapshot = assembler.add_result(buffer[-1]) if buffer else ContextSnapshot([], [], "", True)
                llm_task = asyncio.create_task(_stream_llm(snapshot))

        collector = asyncio.create_task(_collect_tools())

        while True:
            item_type, item = await output_queue.get()
            if item_type == "done":
                break
            yield item

        await collector
```

## Solution 5: Streaming Latency Profiler

```python
import time
from typing import Dict, List, Optional


class StreamingLatencyProfiler:
    """
    Records time-to-first-result and time-to-first-token for streaming
    tool call batches to quantify the latency improvement from early start.
    """

    def __init__(self):
        self._sessions: List[dict] = []

    def record_session(
        self,
        batch_start: float,
        first_result_at: float,
        first_token_at: float,
        last_result_at: float,
        tool_count: int,
    ) -> dict:
        entry = {
            "ts": time.time(),
            "tool_count": tool_count,
            "time_to_first_result_ms": round((first_result_at - batch_start) * 1000, 2),
            "time_to_first_token_ms": round((first_token_at - batch_start) * 1000, 2),
            "total_tool_duration_ms": round((last_result_at - batch_start) * 1000, 2),
            "early_start_savings_ms": round((last_result_at - first_result_at) * 1000, 2),
        }
        self._sessions.append(entry)
        return entry

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [s for s in self._sessions if s["ts"] >= cutoff]
        if not recent:
            return {"window_seconds": window_seconds, "sessions": 0}

        def avg(key: str) -> float:
            return round(sum(s[key] for s in recent) / len(recent), 2)

        return {
            "window_seconds": window_seconds,
            "sessions": len(recent),
            "avg_time_to_first_result_ms": avg("time_to_first_result_ms"),
            "avg_time_to_first_token_ms": avg("time_to_first_token_ms"),
            "avg_early_start_savings_ms": avg("early_start_savings_ms"),
        }
```

## Solution 6: Async Streaming Tool Dashboard

```python
import time


class AsyncStreamingToolDashboard:
    """
    Combines streaming latency profiling and tool completion statistics
    into a single operational view for async streaming tool pipelines.
    """

    def __init__(self, profiler: StreamingLatencyProfiler):
        self._profiler = profiler

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "streaming_latency_1h": self._profiler.summary(3600.0),
            "streaming_latency_24h": self._profiler.summary(86400.0),
        }
```

## Comparison

| Approach | Parallel Dispatch | Arrival-Order Streaming | Incremental Context | Early LLM Start | Latency Profiling |
|---|---|---|---|---|---|
| AsyncToolCompletionStream | Yes | Yes (queue-based) | No | No | No |
| IncrementalContextAssembler | No | Via stream events | Yes | No | No |
| EarlyStartLLMCaller | Via stream | Via stream | Via assembler | Yes | No |
| StreamingLatencyProfiler | No | No | No | No | Yes |
| AsyncStreamingToolDashboard | No | No | No | No | Yes (aggregate) |

**Best for production**: Set `min_results_before_start=1` for user-facing chat interfaces where perceived responsiveness matters — begin streaming LLM output as soon as the first tool result is available. For agentic pipelines where correctness requires all context before reasoning, set `min_results_before_start` equal to the tool count to retain full-batch semantics while still gaining parallel execution. Use `StreamingLatencyProfiler` to measure `early_start_savings_ms`: in batches with high tool latency variance (one fast, one slow), savings routinely exceed 3-5 seconds of time-to-first-token. Monitor for cases where `time_to_first_token_ms` exceeds `total_tool_duration_ms` — this indicates the LLM call itself is the bottleneck, not the tools, and early-start provides no benefit for those request shapes.
