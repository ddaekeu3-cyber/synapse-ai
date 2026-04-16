---
title: "Agent Doesn't Implement Streaming Response Parsing for Incremental Tool Dispatch"
description: "Agents that buffer the entire LLM streaming response before parsing tool calls cannot dispatch the first tool until the last token arrives — wasting the parallelism that streaming enables. Implement incremental streaming response parsing that detects complete tool call JSON as it arrives and dispatches each tool immediately, overlapping LLM generation with tool execution."
date: 2026-04-16
difficulty: advanced
category: performance
slug: agent-doesnt-implement-streaming-response-parsing-for-incremental-tool-dispatch
tags: [streaming-parsing, incremental-dispatch, llm-streaming, tool-overlap, time-to-first-tool, latency-hiding]
symptoms:
  - "Tool calls are dispatched only after the full response is received, not as they appear in the stream"
  - "Time-to-first-tool-result equals LLM generation time plus tool latency instead of overlapping"
  - "Multi-tool responses process tools sequentially despite LLM outputting them simultaneously"
  - "No partial parsing — entire response JSON is accumulated before any processing begins"
  - "LLM streaming is enabled but agent waits for stream completion before acting"
---

## Why This Happens

LLM streaming sends tokens incrementally. When an agent uses streaming but buffers all tokens before parsing, it gains nothing from streaming — latency is identical to non-streaming. The opportunity is to parse the stream incrementally: when the parser detects a complete tool call JSON object in the buffer, it dispatches that tool immediately while the LLM continues generating subsequent tool calls or prose. By the time the LLM finishes generating, the first tool may have already returned its result. This requires a stateful incremental JSON parser that tracks brace depth to detect object boundaries mid-stream.

## Solution 1: Incremental JSON Tool Call Detector

```python
import json
import re
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class DetectedToolCall:
    tool_name: str
    arguments: dict
    raw_json: str
    token_offset: int = 0   # stream position where this tool call completed


class IncrementalToolCallDetector:
    """
    Scans a growing token buffer for complete tool call JSON objects.
    Uses brace depth tracking to detect object boundaries without
    requiring a full JSON parser on partial data.
    """

    TOOL_CALL_OPENER = re.compile(
        r'"(?:tool_calls?|function_call|tool_use)"\s*:\s*\[?\s*\{',
        re.IGNORECASE,
    )

    def __init__(self):
        self._buffer = ""
        self._dispatched_offsets: List[int] = []
        self._detected: List[DetectedToolCall] = []

    def feed(self, token: str) -> List[DetectedToolCall]:
        """Feed a new token. Returns any newly-completed tool calls."""
        self._buffer += token
        return self._scan_for_complete_calls()

    def _scan_for_complete_calls(self) -> List[DetectedToolCall]:
        new_detections = []
        search_start = 0

        while True:
            match = self.TOOL_CALL_OPENER.search(self._buffer, search_start)
            if not match:
                break

            obj_start = self._buffer.rfind("{", search_start, match.end())
            if obj_start == -1:
                break

            if obj_start in self._dispatched_offsets:
                search_start = obj_start + 1
                continue

            obj_end = self._find_object_end(self._buffer, obj_start)
            if obj_end is None:
                break   # object not complete yet

            raw = self._buffer[obj_start: obj_end + 1]
            try:
                parsed = json.loads(raw)
                name = (
                    parsed.get("name")
                    or parsed.get("function", {}).get("name")
                    or parsed.get("tool_name", "unknown")
                )
                args = (
                    parsed.get("arguments")
                    or parsed.get("function", {}).get("arguments", {})
                    or parsed.get("input", {})
                )
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {"raw": args}
                self._dispatched_offsets.append(obj_start)
                call = DetectedToolCall(
                    tool_name=name,
                    arguments=args,
                    raw_json=raw,
                    token_offset=obj_end,
                )
                self._detected.append(call)
                new_detections.append(call)
            except json.JSONDecodeError:
                pass
            search_start = obj_end + 1

        return new_detections

    @staticmethod
    def _find_object_end(text: str, start: int) -> Optional[int]:
        depth = 0
        in_string = False
        escape_next = False
        for i in range(start, len(text)):
            ch = text[i]
            if escape_next:
                escape_next = False
                continue
            if ch == "\\" and in_string:
                escape_next = True
                continue
            if ch == '"' and not escape_next:
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return i
        return None
```

## Solution 2: Stream-Dispatch Coordinator

```python
import asyncio
import time
from typing import Any, AsyncIterator, Callable, Dict, List, Optional


class StreamDispatchCoordinator:
    """
    Consumes an LLM token stream, feeds tokens to the incremental detector,
    and dispatches tool calls immediately as they are detected — without
    waiting for the stream to complete.
    """

    def __init__(
        self,
        detector: IncrementalToolCallDetector,
        tool_fn: Callable,
        max_concurrent_tools: int = 4,
    ):
        self._detector = detector
        self._tool_fn = tool_fn
        self._semaphore = asyncio.Semaphore(max_concurrent_tools)
        self._dispatched_tasks: List[asyncio.Task] = []
        self._first_dispatch_at: Optional[float] = None
        self._stream_start_at: Optional[float] = None

    async def process_stream(
        self,
        token_stream: AsyncIterator[str],
    ) -> dict:
        self._stream_start_at = time.time()
        full_text = ""

        async for token in token_stream:
            full_text += token
            new_calls = self._detector.feed(token)
            for call in new_calls:
                if self._first_dispatch_at is None:
                    self._first_dispatch_at = time.time()
                task = asyncio.create_task(self._dispatch(call))
                self._dispatched_tasks.append(task)

        # Wait for all in-flight tool calls
        results = await asyncio.gather(*self._dispatched_tasks, return_exceptions=True)
        stream_duration_ms = round((time.time() - self._stream_start_at) * 1000, 2)

        ttfd_ms = None
        if self._first_dispatch_at and self._stream_start_at:
            ttfd_ms = round((self._first_dispatch_at - self._stream_start_at) * 1000, 2)

        return {
            "full_text": full_text,
            "tool_calls_dispatched": len(self._dispatched_tasks),
            "tool_results": [r for r in results if not isinstance(r, Exception)],
            "stream_duration_ms": stream_duration_ms,
            "time_to_first_dispatch_ms": ttfd_ms,
        }

    async def _dispatch(self, call: DetectedToolCall) -> dict:
        async with self._semaphore:
            start = time.time()
            try:
                result = await self._tool_fn(call.tool_name, call.arguments)
                return {
                    "tool_name": call.tool_name,
                    "result": result,
                    "latency_ms": round((time.time() - start) * 1000, 2),
                }
            except Exception as exc:
                return {
                    "tool_name": call.tool_name,
                    "error": str(exc),
                    "latency_ms": round((time.time() - start) * 1000, 2),
                }
```

## Solution 3: Streaming Latency Profiler

```python
import time
from typing import List, Optional, Tuple


class StreamingLatencyProfiler:
    """
    Measures the latency components of streaming dispatch:
    time-to-first-token, time-to-first-dispatch, and total stream duration.
    """

    def __init__(self):
        self._sessions: List[dict] = []

    def record(
        self,
        stream_start_ms: float,
        first_token_ms: Optional[float],
        first_dispatch_ms: Optional[float],
        stream_end_ms: float,
        tool_count: int,
    ) -> None:
        self._sessions.append({
            "ts": time.time(),
            "ttft_ms": round(first_token_ms - stream_start_ms, 2) if first_token_ms else None,
            "ttfd_ms": round(first_dispatch_ms - stream_start_ms, 2) if first_dispatch_ms else None,
            "total_stream_ms": round(stream_end_ms - stream_start_ms, 2),
            "tool_count": tool_count,
        })

    def summary(self) -> dict:
        if not self._sessions:
            return {"sessions": 0}
        ttfds = [s["ttfd_ms"] for s in self._sessions if s["ttfd_ms"] is not None]
        totals = [s["total_stream_ms"] for s in self._sessions]
        overlap_savings = [
            s["total_stream_ms"] - s["ttfd_ms"]
            for s in self._sessions
            if s["ttfd_ms"] is not None
        ]
        return {
            "sessions": len(self._sessions),
            "mean_ttfd_ms": round(sum(ttfds) / len(ttfds), 2) if ttfds else None,
            "mean_total_stream_ms": round(sum(totals) / len(totals), 2),
            "mean_overlap_savings_ms": round(
                sum(overlap_savings) / len(overlap_savings), 2
            ) if overlap_savings else None,
        }
```

## Solution 4: Partial Result Accumulator

```python
import asyncio
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class PartialResult:
    tool_name: str
    result: Optional[Any] = None
    error: Optional[str] = None
    ready: bool = False


class PartialResultAccumulator:
    """
    Collects tool results as they complete during streaming.
    Allows the agent loop to access results as they arrive rather
    than waiting for all tools to complete.
    """

    def __init__(self):
        self._results: Dict[str, PartialResult] = {}
        self._ready_event = asyncio.Event()

    def register(self, tool_name: str) -> None:
        self._results[tool_name] = PartialResult(tool_name=tool_name)

    def set_result(self, tool_name: str, result: Any) -> None:
        if tool_name in self._results:
            self._results[tool_name].result = result
            self._results[tool_name].ready = True
        self._ready_event.set()
        self._ready_event.clear()

    def set_error(self, tool_name: str, error: str) -> None:
        if tool_name in self._results:
            self._results[tool_name].error = error
            self._results[tool_name].ready = True
        self._ready_event.set()
        self._ready_event.clear()

    def ready_results(self) -> List[PartialResult]:
        return [r for r in self._results.values() if r.ready]

    def all_ready(self) -> bool:
        return all(r.ready for r in self._results.values())
```

## Solution 5: Incremental Context Injector

```python
from typing import List


class IncrementalContextInjector:
    """
    Injects tool results back into the context as they arrive during
    streaming rather than waiting for all results to build one batch update.
    """

    def __init__(self, accumulator: PartialResultAccumulator):
        self._accumulator = accumulator
        self._injected: List[str] = []

    def get_available_context(self) -> str:
        ready = self._accumulator.ready_results()
        new_context_parts = []
        for result in ready:
            if result.tool_name not in self._injected:
                self._injected.append(result.tool_name)
                if result.error:
                    new_context_parts.append(
                        f"[{result.tool_name} error: {result.error}]"
                    )
                else:
                    new_context_parts.append(
                        f"[{result.tool_name} result: {result.result}]"
                    )
        return "\n".join(new_context_parts)

    def pending_count(self) -> int:
        return sum(
            1 for r in self._accumulator._results.values() if not r.ready
        )
```

## Solution 6: Streaming Dispatch Dashboard

```python
import time


class StreamingDispatchDashboard:
    """
    Combines stream profiler, partial accumulator state, and
    coordinator stats into a streaming pipeline health report.
    """

    def __init__(
        self,
        coordinator: StreamDispatchCoordinator,
        profiler: StreamingLatencyProfiler,
        accumulator: PartialResultAccumulator,
    ):
        self._coordinator = coordinator
        self._profiler = profiler
        self._accumulator = accumulator

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "latency_profile": self._profiler.summary(),
            "current_session": {
                "ready_tool_results": len(self._accumulator.ready_results()),
                "pending_tool_results": self._accumulator.pending_count(),
                "all_ready": self._accumulator.all_ready(),
            },
            "coordinator": {
                "dispatched_tasks": len(self._coordinator._dispatched_tasks),
                "time_to_first_dispatch_ms": (
                    round(
                        (self._coordinator._first_dispatch_at - self._coordinator._stream_start_at) * 1000, 2
                    )
                    if self._coordinator._first_dispatch_at and self._coordinator._stream_start_at
                    else None
                ),
            },
        }
```

## Comparison

| Approach | Incremental Parsing | Immediate Dispatch | Partial Results | Latency Profiling | Dashboard |
|---|---|---|---|---|---|
| IncrementalToolCallDetector | Yes (brace depth) | No | No | No | No |
| StreamDispatchCoordinator | Via detector | Yes (create_task) | No | No | No |
| StreamingLatencyProfiler | No | No | No | Yes (TTFD) | No |
| PartialResultAccumulator | No | No | Yes (as-ready) | No | No |
| IncrementalContextInjector | No | No | Via accumulator | No | No |
| StreamingDispatchDashboard | No | No | No | No | Yes |

**Best for production**: The largest latency win comes from overlapping the last 60–80% of LLM generation time with tool execution — a 500ms stream that dispatches the first tool at token 30% of the way through saves ~350ms of tool waiting time. Cap `max_concurrent_tools=4` to prevent a burst of simultaneous dispatches from exhausting downstream rate limits. Track `time_to_first_dispatch_ms` as a key streaming efficiency metric: it should be significantly less than `total_stream_ms` for multi-tool responses; if they converge, the incremental parser is failing to detect tool calls early in the stream and the detection regex needs tuning.
