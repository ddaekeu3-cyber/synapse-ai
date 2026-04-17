---
title: "Agent Doesn't Implement Streaming Aggregation for Long-Running Tool Results"
description: "Agents that wait for long-running tool calls to complete before processing any output block the agent loop for seconds or minutes: a tool that runs a database query, compiles a report, or crawls a website holds up all downstream processing until the final result arrives. Implement streaming aggregation that begins processing tool output incrementally as chunks arrive, unblocking the agent loop and reducing perceived latency."
date: 2026-04-16
difficulty: advanced
category: performance
slug: agent-doesnt-implement-streaming-aggregation-for-long-running-tool-results
tags: [streaming, long-running-tools, incremental-processing, backpressure, async-generators, latency-reduction]
symptoms:
  - "Agent blocks for 10–30 seconds waiting for a single tool to return before doing anything else"
  - "No partial results surfaced to the user during long tool executions"
  - "Tool calls that produce large outputs (reports, crawl results) cause memory spikes on completion"
  - "Timeout errors on tools that would succeed if the timeout started after first byte"
  - "Downstream tool calls that do not depend on the long-running tool are also blocked"
---

## Why This Happens

Tool call interfaces that return a single value require the tool to complete entirely before the agent can proceed. When a tool produces output incrementally — a database cursor yielding rows, an HTTP response streaming bytes, a subprocess printing lines — the single-return interface forces buffering of the entire output before returning. Streaming aggregation replaces the single return with an async generator that yields chunks as they are produced, allowing the agent to begin processing, display progress, and make routing decisions without waiting for the last byte.

## Solution 1: Stream Chunk Types

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class StreamChunkType(str, Enum):
    DATA = "data"           # actual content chunk
    PROGRESS = "progress"  # progress metadata (pct, count, etc.)
    ERROR = "error"        # non-fatal error in stream
    HEARTBEAT = "heartbeat" # keepalive — no data
    DONE = "done"          # stream complete marker


@dataclass
class StreamChunk:
    chunk_type: StreamChunkType
    data: Any = None
    sequence: int = 0
    progress_pct: Optional[float] = None
    error_message: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=lambda: __import__("time").time())

    @property
    def is_terminal(self) -> bool:
        return self.chunk_type in (StreamChunkType.DONE, StreamChunkType.ERROR)
```

## Solution 2: Streaming Tool Result Aggregator

```python
import asyncio
import time
from typing import Any, AsyncGenerator, Callable, List, Optional


class StreamingToolResultAggregator:
    """
    Consumes an async generator of StreamChunks from a long-running tool.
    Accumulates data chunks, tracks progress, and provides the final
    aggregated result when the stream completes.
    """

    def __init__(
        self,
        chunk_handler: Optional[Callable[[StreamChunk], Any]] = None,
        max_chunks: int = 100_000,
        heartbeat_timeout_seconds: float = 30.0,
    ):
        self._chunk_handler = chunk_handler
        self._max_chunks = max_chunks
        self._heartbeat_timeout = heartbeat_timeout_seconds
        self._chunks: List[StreamChunk] = []
        self._data_chunks: List[Any] = []
        self._last_activity = time.time()
        self._chunk_count = 0
        self._error_chunks = 0

    async def consume(self, stream: AsyncGenerator[StreamChunk, None]) -> dict:
        start = time.time()
        last_heartbeat_check = time.time()

        async for chunk in stream:
            self._last_activity = time.time()
            self._chunk_count += 1

            if self._chunk_count > self._max_chunks:
                break

            if chunk.chunk_type == StreamChunkType.DATA and chunk.data is not None:
                self._data_chunks.append(chunk.data)
            elif chunk.chunk_type == StreamChunkType.ERROR:
                self._error_chunks += 1

            if self._chunk_handler:
                result = self._chunk_handler(chunk)
                if asyncio.iscoroutine(result):
                    await result

            if chunk.is_terminal:
                break

            # Heartbeat timeout check
            if time.time() - last_heartbeat_check > 5.0:
                if time.time() - self._last_activity > self._heartbeat_timeout:
                    break
                last_heartbeat_check = time.time()

        return {
            "data_chunks": self._data_chunks,
            "total_chunks": self._chunk_count,
            "error_chunks": self._error_chunks,
            "elapsed_seconds": round(time.time() - start, 2),
            "truncated": self._chunk_count >= self._max_chunks,
        }
```

## Solution 3: Streaming Tool Wrapper

```python
import asyncio
from typing import Any, AsyncGenerator, Callable, Dict


class StreamingToolWrapper:
    """
    Wraps a non-streaming tool function to emit StreamChunks.
    Useful for tools that return large payloads in one call but
    can be split into logical chunks (e.g., list of records).
    """

    @staticmethod
    async def wrap_list_result(
        fn: Callable,
        args: Dict[str, Any],
        chunk_size: int = 10,
    ) -> AsyncGenerator[StreamChunk, None]:
        result = await fn(**args)
        if not isinstance(result, (list, tuple)):
            yield StreamChunk(chunk_type=StreamChunkType.DATA, data=result, sequence=0)
            yield StreamChunk(chunk_type=StreamChunkType.DONE, sequence=1)
            return

        total = len(result)
        for i in range(0, total, chunk_size):
            batch = result[i:i + chunk_size]
            pct = min(100.0, (i + len(batch)) / total * 100)
            yield StreamChunk(
                chunk_type=StreamChunkType.DATA,
                data=batch,
                sequence=i // chunk_size,
                progress_pct=round(pct, 1),
            )
            await asyncio.sleep(0)  # yield control

        yield StreamChunk(chunk_type=StreamChunkType.DONE, sequence=total // chunk_size + 1)

    @staticmethod
    async def wrap_text_stream(
        stream: AsyncGenerator[str, None],
        sentence_delimiter: str = ". ",
    ) -> AsyncGenerator[StreamChunk, None]:
        buffer = ""
        sequence = 0
        async for text_chunk in stream:
            buffer += text_chunk
            while sentence_delimiter in buffer:
                idx = buffer.index(sentence_delimiter) + len(sentence_delimiter)
                sentence = buffer[:idx]
                buffer = buffer[idx:]
                yield StreamChunk(
                    chunk_type=StreamChunkType.DATA,
                    data=sentence,
                    sequence=sequence,
                )
                sequence += 1
                await asyncio.sleep(0)

        if buffer:
            yield StreamChunk(chunk_type=StreamChunkType.DATA, data=buffer, sequence=sequence)
        yield StreamChunk(chunk_type=StreamChunkType.DONE, sequence=sequence + 1)
```

## Solution 4: Parallel Stream Processor

```python
import asyncio
from typing import Any, AsyncGenerator, Callable, Dict, List, Optional, Tuple


class ParallelStreamProcessor:
    """
    Runs multiple streaming tool calls concurrently and merges their
    chunks into a single ordered stream with source attribution.
    """

    def __init__(self, max_concurrency: int = 5):
        self._semaphore = asyncio.Semaphore(max_concurrency)

    async def _run_one(
        self,
        tool_name: str,
        stream_fn: Callable[[], AsyncGenerator[StreamChunk, None]],
        output_queue: asyncio.Queue,
    ) -> None:
        async with self._semaphore:
            async for chunk in stream_fn():
                tagged = StreamChunk(
                    chunk_type=chunk.chunk_type,
                    data=chunk.data,
                    sequence=chunk.sequence,
                    progress_pct=chunk.progress_pct,
                    metadata={**chunk.metadata, "source_tool": tool_name},
                )
                await output_queue.put(tagged)
        await output_queue.put(StreamChunk(
            chunk_type=StreamChunkType.DONE,
            metadata={"source_tool": tool_name, "sentinel": True},
        ))

    async def merge(
        self,
        streams: List[Tuple[str, Callable]],
    ) -> AsyncGenerator[StreamChunk, None]:
        queue: asyncio.Queue = asyncio.Queue(maxsize=500)
        tasks = [
            asyncio.ensure_future(self._run_one(name, fn, queue))
            for name, fn in streams
        ]
        pending_sentinels = len(streams)

        while pending_sentinels > 0:
            chunk = await asyncio.wait_for(queue.get(), timeout=60.0)
            if chunk.chunk_type == StreamChunkType.DONE and chunk.metadata.get("sentinel"):
                pending_sentinels -= 1
            else:
                yield chunk

        await asyncio.gather(*tasks, return_exceptions=True)
```

## Solution 5: Stream Backpressure Controller

```python
import asyncio
import time
from typing import AsyncGenerator


class StreamBackpressureController:
    """
    Slows chunk production when the consumer cannot keep up.
    Prevents memory accumulation when a fast producer feeds a slow consumer.
    """

    def __init__(
        self,
        max_buffer_size: int = 200,
        slowdown_threshold: int = 100,
        max_wait_seconds: float = 5.0,
    ):
        self._max = max_buffer_size
        self._slowdown = slowdown_threshold
        self._max_wait = max_wait_seconds
        self._buffer_size = 0
        self._drops = 0

    async def throttled(
        self,
        source: AsyncGenerator[StreamChunk, None],
    ) -> AsyncGenerator[StreamChunk, None]:
        async for chunk in source:
            self._buffer_size += 1

            if self._buffer_size > self._max:
                self._drops += 1
                if chunk.chunk_type == StreamChunkType.DATA:
                    continue  # drop data chunk under pressure

            if self._buffer_size > self._slowdown:
                delay = min(
                    self._max_wait,
                    0.01 * (self._buffer_size - self._slowdown),
                )
                await asyncio.sleep(delay)

            yield chunk
            self._buffer_size = max(0, self._buffer_size - 1)

    def stats(self) -> dict:
        return {"buffer_size": self._buffer_size, "drops": self._drops}
```

## Solution 6: Streaming Aggregation Dashboard

```python
import time
from typing import List


class StreamingAggregationDashboard:
    """
    Tracks streaming aggregation run statistics to surface
    throughput, error rates, and truncation patterns.
    """

    def __init__(self):
        self._runs: List[dict] = []
        self._timestamps: List[float] = []

    def record(self, aggregation_result: dict, tool_name: str = "") -> None:
        self._runs.append({**aggregation_result, "tool_name": tool_name})
        self._timestamps.append(time.time())

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [
            r for r, ts in zip(self._runs, self._timestamps) if ts >= cutoff
        ]
        if not recent:
            return {"window_seconds": window_seconds, "runs": 0}

        return {
            "window_seconds": window_seconds,
            "runs": len(recent),
            "avg_chunks": round(sum(r["total_chunks"] for r in recent) / len(recent), 1),
            "avg_elapsed_seconds": round(sum(r["elapsed_seconds"] for r in recent) / len(recent), 2),
            "truncated_runs": sum(1 for r in recent if r.get("truncated")),
            "total_error_chunks": sum(r.get("error_chunks", 0) for r in recent),
        }
```

## Comparison

| Approach | Async Streaming | Chunk Accumulation | Parallel Streams | Backpressure | Stats |
|---|---|---|---|---|---|
| StreamingToolResultAggregator | Yes | Yes | No | No | No |
| StreamingToolWrapper | Yes (wraps sync) | No | No | No | No |
| ParallelStreamProcessor | Yes | No | Yes (merged) | No | No |
| StreamBackpressureController | Yes | No | No | Yes | No |
| StreamingAggregationDashboard | No | No | No | No | Yes |

**Best for production**: Use `StreamingToolWrapper.wrap_list_result()` as a zero-effort upgrade for tools that return large lists — it converts a blocking return into a streaming interface without changing the tool's implementation. Set `max_chunks=10_000` in `StreamingToolResultAggregator` to prevent unbounded memory growth from runaway streams. Apply `StreamBackpressureController` when tool output rate significantly exceeds consumer processing rate — without it, a fast tool filling a slow LLM pipeline will accumulate unbounded memory in the queue. Monitor `truncated_runs` in the dashboard: consistent truncation means tools are returning more data than the agent can usefully process and upstream filters should be applied at the tool level.
