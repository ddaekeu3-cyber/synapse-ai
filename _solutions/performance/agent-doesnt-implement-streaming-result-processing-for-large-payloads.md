---
title: "Agent Doesn't Implement Streaming Result Processing for Large Payloads"
description: "Agents that buffer complete tool results before processing block the event loop while waiting for large payloads to fully arrive: a 5MB database export, a 10-second log stream, or a large file read all must complete before any processing begins. Implement streaming result processing that consumes tool output incrementally, applies per-chunk transformations, and surfaces partial results to the context as they arrive."
date: 2026-04-16
difficulty: advanced
category: performance
slug: agent-doesnt-implement-streaming-result-processing-for-large-payloads
tags: [streaming-results, incremental-processing, async-iteration, large-payloads, event-loop, chunked-consumption]
symptoms:
  - "Agent appears frozen for 10+ seconds while waiting for a large tool result to buffer"
  - "Memory spikes when a tool returns a multi-megabyte payload that is buffered in full"
  - "No partial results are available until the entire tool output is received"
  - "Event loop is blocked while a synchronous tool call reads a large file into memory"
  - "Database export tool times out the HTTP connection before the full result is received"
---

## Why This Happens

Tool implementations typically return a single value — a string, dict, or list — that the agent buffers entirely in memory before processing. For large payloads this creates a head-of-line blocking problem: the agent cannot start processing, transforming, or injecting results into the context until the last byte arrives. Streaming requires tools to return async generators or iterator objects, a processing pipeline that applies transformations chunk-by-chunk, and an accumulator that progressively builds the final context contribution without waiting for completion.

## Solution 1: Streaming Tool Result Protocol

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, AsyncGenerator, Optional


class StreamChunkType(str, Enum):
    DATA = "data"
    PROGRESS = "progress"
    ERROR = "error"
    DONE = "done"


@dataclass
class StreamResultChunk:
    chunk_type: StreamChunkType
    sequence: int
    payload: Any = None
    bytes_received: int = 0
    total_bytes: Optional[int] = None  # None if unknown
    error_message: str = ""

    def progress_pct(self) -> Optional[float]:
        if self.total_bytes and self.total_bytes > 0:
            return round(self.bytes_received / self.total_bytes * 100, 1)
        return None
```

## Solution 2: Async Stream Transformer

```python
import asyncio
from typing import Any, AsyncGenerator, Callable, Optional


class AsyncStreamTransformer:
    """
    Applies a transformation function to each DATA chunk in a stream.
    Passes PROGRESS, ERROR, and DONE chunks through unchanged.
    Useful for decoding, parsing, filtering, or summarizing chunks.
    """

    def __init__(
        self,
        transform_fn: Callable[[Any], Any],
        skip_empty: bool = True,
    ):
        self._transform = transform_fn
        self._skip_empty = skip_empty

    async def transform(
        self,
        source: AsyncGenerator[StreamResultChunk, None],
    ) -> AsyncGenerator[StreamResultChunk, None]:
        seq = 0
        async for chunk in source:
            if chunk.chunk_type != StreamChunkType.DATA:
                yield chunk
                continue
            transformed = self._transform(chunk.payload)
            if self._skip_empty and not transformed:
                continue
            seq += 1
            yield StreamResultChunk(
                chunk_type=StreamChunkType.DATA,
                sequence=seq,
                payload=transformed,
                bytes_received=chunk.bytes_received,
                total_bytes=chunk.total_bytes,
            )
```

## Solution 3: Progressive Context Accumulator

```python
import time
from typing import Any, AsyncGenerator, Callable, List, Optional


class ProgressiveContextAccumulator:
    """
    Consumes a stream of result chunks and incrementally builds
    the context contribution. Fires a callback each time the
    accumulated content grows by a configurable threshold, so
    the calling layer can progressively update the context.
    """

    def __init__(
        self,
        max_chars: int = 16000,
        progress_callback: Optional[Callable[[str, float], None]] = None,
        progress_interval_chars: int = 2000,
    ):
        self._max_chars = max_chars
        self._callback = progress_callback
        self._progress_interval = progress_interval_chars
        self._chunks: List[str] = []
        self._total_chars = 0
        self._last_callback_at = 0
        self._truncated = False

    async def consume(
        self,
        stream: AsyncGenerator[StreamResultChunk, None],
    ) -> dict:
        start = time.time()
        chunk_count = 0
        error = None

        async for chunk in stream:
            if chunk.chunk_type == StreamChunkType.ERROR:
                error = chunk.error_message
                break
            if chunk.chunk_type == StreamChunkType.DONE:
                break
            if chunk.chunk_type != StreamChunkType.DATA:
                continue

            text = str(chunk.payload) if not isinstance(chunk.payload, str) else chunk.payload
            remaining_budget = self._max_chars - self._total_chars

            if remaining_budget <= 0:
                self._truncated = True
                break

            if len(text) > remaining_budget:
                text = text[:remaining_budget]
                self._truncated = True

            self._chunks.append(text)
            self._total_chars += len(text)
            chunk_count += 1

            # Fire progress callback
            if (self._callback and
                    self._total_chars - self._last_callback_at >= self._progress_interval):
                progress = chunk.progress_pct() or 0.0
                self._callback(self.current_content(), progress)
                self._last_callback_at = self._total_chars

            if self._truncated:
                break

        elapsed_ms = round((time.time() - start) * 1000, 2)
        return {
            "content": self.current_content(),
            "total_chars": self._total_chars,
            "chunk_count": chunk_count,
            "truncated": self._truncated,
            "error": error,
            "elapsed_ms": elapsed_ms,
        }

    def current_content(self) -> str:
        return "".join(self._chunks)
```

## Solution 4: File Stream Adapter

```python
import asyncio
from typing import AsyncGenerator, Optional


class FileStreamAdapter:
    """
    Adapts synchronous file reading to the StreamResultChunk protocol.
    Reads a file in configurable chunks and yields them asynchronously
    to avoid blocking the event loop.
    """

    def __init__(self, chunk_size_bytes: int = 65536):
        self._chunk_size = chunk_size_bytes

    async def stream_file(
        self,
        file_path: str,
    ) -> AsyncGenerator[StreamResultChunk, None]:
        import os
        try:
            total = os.path.getsize(file_path)
        except OSError:
            total = None

        bytes_read = 0
        seq = 0
        try:
            with open(file_path, "r", errors="replace") as f:
                while True:
                    chunk = await asyncio.get_event_loop().run_in_executor(
                        None, f.read, self._chunk_size
                    )
                    if not chunk:
                        break
                    bytes_read += len(chunk.encode("utf-8"))
                    seq += 1
                    yield StreamResultChunk(
                        chunk_type=StreamChunkType.DATA,
                        sequence=seq,
                        payload=chunk,
                        bytes_received=bytes_read,
                        total_bytes=total,
                    )
        except Exception as exc:
            yield StreamResultChunk(
                chunk_type=StreamChunkType.ERROR,
                sequence=seq + 1,
                error_message=str(exc),
            )
            return

        yield StreamResultChunk(
            chunk_type=StreamChunkType.DONE,
            sequence=seq + 1,
            bytes_received=bytes_read,
            total_bytes=total,
        )
```

## Solution 5: Streaming Tool Execution Pipeline

```python
import asyncio
import time
from typing import Any, AsyncGenerator, Callable, Dict, Optional


class StreamingToolExecutionPipeline:
    """
    Orchestrates streaming tool execution: invokes a tool that returns
    an async generator, optionally transforms chunks, and accumulates
    results up to a token budget.
    """

    def __init__(
        self,
        accumulator: ProgressiveContextAccumulator,
        transformer: Optional[AsyncStreamTransformer] = None,
    ):
        self._accumulator = accumulator
        self._transformer = transformer

    async def execute(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        stream_fn: Callable[[str, Dict[str, Any]], AsyncGenerator],
    ) -> dict:
        stream = stream_fn(tool_name, arguments)

        if self._transformer:
            stream = self._transformer.transform(stream)

        result = await self._accumulator.consume(stream)
        return {
            "tool_name": tool_name,
            **result,
        }
```

## Solution 6: Streaming Performance Monitor

```python
import time
from typing import List


class StreamingPerformanceMonitor:
    """
    Tracks streaming pipeline execution metrics: throughput,
    truncation rate, and time-to-first-chunk across calls.
    """

    def __init__(self):
        self._records: List[dict] = []

    def record(self, pipeline_result: dict, ttfc_ms: float = 0.0) -> None:
        self._records.append({
            **pipeline_result,
            "ttfc_ms": ttfc_ms,
            "recorded_at": time.time(),
        })

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [r for r in self._records if r.get("recorded_at", 0) >= cutoff]
        if not recent:
            return {"window_seconds": window_seconds, "calls": 0}

        total_chars = sum(r.get("total_chars", 0) for r in recent)
        total_ms = sum(r.get("elapsed_ms", 0) for r in recent)
        truncated = sum(1 for r in recent if r.get("truncated"))

        return {
            "window_seconds": window_seconds,
            "calls": len(recent),
            "total_chars_processed": total_chars,
            "avg_throughput_chars_per_sec": round(
                total_chars / max(total_ms / 1000, 0.001), 0
            ),
            "avg_elapsed_ms": round(total_ms / len(recent), 2),
            "truncation_rate": round(truncated / len(recent), 4),
        }
```

## Comparison

| Approach | Async Streaming | Chunk Transformation | Progressive Accumulation | File Adapter | Metrics |
|---|---|---|---|---|---|
| AsyncStreamTransformer | Yes | Yes (per chunk) | No | No | No |
| ProgressiveContextAccumulator | Yes | No | Yes (with callback) | No | No |
| FileStreamAdapter | Yes (run_in_executor) | No | No | Yes | No |
| StreamingToolExecutionPipeline | Via stream_fn | Via transformer | Via accumulator | No | No |
| StreamingPerformanceMonitor | No | No | No | No | Yes |

**Best for production**: Set `chunk_size_bytes=65536` (64KB) for file adapters — this matches typical OS page sizes and avoids excessive context switches while keeping individual chunks small enough to yield to the event loop regularly. Use `max_chars=16000` in `ProgressiveContextAccumulator` to enforce a hard context budget regardless of stream length. Fire the progress callback every 2000 chars so the user sees incremental updates on long streams rather than a 10-second wait followed by a single large response. Monitor `truncation_rate` — consistently above 30% means the context budget is too small for the payload sizes being processed and either the budget should increase or the tool should support server-side filtering before streaming.
