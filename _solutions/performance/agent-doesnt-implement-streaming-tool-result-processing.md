---
title: "Agent Doesn't Implement Streaming Tool Result Processing"
description: "Agents that wait for a tool to return its complete result before processing it will experience high time-to-first-token when tools produce large responses: a database query that returns 10,000 rows, a file reader that loads a large document, or an API that returns a paginated stream all force the agent to buffer the entire payload before any processing begins. Implement streaming tool result processing that reads results incrementally, begins processing as soon as the first chunk arrives, and passes streaming context to the LLM without buffering the full result."
date: 2026-04-16
difficulty: advanced
category: performance
slug: agent-doesnt-implement-streaming-tool-result-processing
tags: [streaming, tool-results, incremental-processing, time-to-first-token, async-generator, backpressure]
symptoms:
  - "Agent waits 8 seconds for a large tool result to fully load before any processing begins"
  - "Memory usage spikes when large tool results are fully buffered before injection into context"
  - "Time-to-first-token is dominated by tool response time, not LLM inference time"
  - "Streaming tool APIs are called but results are collected into a list before processing"
  - "No incremental processing — the agent cannot begin reasoning until the last byte arrives"
---

## Why This Happens

Most tool call patterns follow a request-response model: call the tool, await the complete result, inject it into context, call the LLM. When tools return large payloads, the await-complete step becomes the bottleneck. Many tools and APIs support streaming — they return data incrementally as it becomes available. Exploiting streaming requires an async generator interface at the tool level, a chunk accumulation strategy that makes partial results available to the agent, and a decision about when to begin LLM processing: after enough data has arrived (a head-first strategy) or after the stream completes (the current default).

## Solution 1: Tool Stream Chunk

```python
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class ChunkSignal(str, Enum):
    DATA = "data"
    PROGRESS = "progress"       # progress metadata, no content
    ERROR = "error"
    DONE = "done"               # stream complete


@dataclass
class ToolStreamChunk:
    signal: ChunkSignal
    content: Any                # the actual data chunk
    chunk_index: int
    tool_name: str
    stream_id: str
    byte_offset: int = 0
    total_bytes: Optional[int] = None   # if known upfront
    error: Optional[str] = None
    received_at: float = field(default_factory=time.time)

    @property
    def is_final(self) -> bool:
        return self.signal in (ChunkSignal.DONE, ChunkSignal.ERROR)

    @property
    def progress_pct(self) -> Optional[float]:
        if self.total_bytes and self.total_bytes > 0:
            return round(self.byte_offset / self.total_bytes * 100, 1)
        return None
```

## Solution 2: Streaming Tool Adapter

```python
import asyncio
import uuid
from typing import Any, AsyncGenerator, Callable, Optional


class StreamingToolAdapter:
    """
    Wraps a streaming tool function that yields chunks into a standardized
    async generator of ToolStreamChunks.
    """

    def __init__(self, tool_name: str):
        self._tool_name = tool_name

    async def stream(
        self,
        tool_fn: Callable,               # async generator fn(*args) -> yields raw chunks
        *args: Any,
        total_bytes: Optional[int] = None,
        **kwargs: Any,
    ) -> AsyncGenerator[ToolStreamChunk, None]:
        stream_id = str(uuid.uuid4())[:8]
        chunk_index = 0
        byte_offset = 0

        try:
            async for raw_chunk in tool_fn(*args, **kwargs):
                content = raw_chunk
                chunk_bytes = len(str(content).encode()) if content else 0
                yield ToolStreamChunk(
                    signal=ChunkSignal.DATA,
                    content=content,
                    chunk_index=chunk_index,
                    tool_name=self._tool_name,
                    stream_id=stream_id,
                    byte_offset=byte_offset,
                    total_bytes=total_bytes,
                )
                chunk_index += 1
                byte_offset += chunk_bytes

            yield ToolStreamChunk(
                signal=ChunkSignal.DONE,
                content=None,
                chunk_index=chunk_index,
                tool_name=self._tool_name,
                stream_id=stream_id,
                byte_offset=byte_offset,
                total_bytes=total_bytes or byte_offset,
            )

        except Exception as exc:
            yield ToolStreamChunk(
                signal=ChunkSignal.ERROR,
                content=None,
                chunk_index=chunk_index,
                tool_name=self._tool_name,
                stream_id=stream_id,
                byte_offset=byte_offset,
                error=str(exc),
            )
```

## Solution 3: Incremental Result Accumulator

```python
import time
from typing import Any, Callable, List, Optional


class IncrementalResultAccumulator:
    """
    Accumulates streaming chunks with configurable head-first processing:
    triggers a callback when enough data has arrived to begin processing,
    without waiting for the stream to complete.
    """

    def __init__(
        self,
        head_trigger_chunks: int = 10,      # call head_fn after this many chunks
        head_trigger_bytes: int = 4096,     # or after this many bytes
        max_buffer_chunks: int = 1000,
    ):
        self._head_trigger_chunks = head_trigger_chunks
        self._head_trigger_bytes = head_trigger_bytes
        self._max_buffer = max_buffer_chunks
        self._chunks: List[ToolStreamChunk] = []
        self._total_bytes = 0
        self._head_fired = False
        self._complete = False

    async def consume(
        self,
        stream: Any,                         # async generator of ToolStreamChunk
        head_fn: Optional[Callable] = None,  # async fn(chunks_so_far) -> None
        done_fn: Optional[Callable] = None,  # async fn(all_chunks) -> None
    ) -> List[ToolStreamChunk]:
        async for chunk in stream:
            if chunk.signal == ChunkSignal.ERROR:
                raise RuntimeError(f"stream error: {chunk.error}")

            if chunk.signal == ChunkSignal.DATA:
                if len(self._chunks) < self._max_buffer:
                    self._chunks.append(chunk)
                self._total_bytes += len(str(chunk.content).encode()) if chunk.content else 0

                # Head-first trigger
                if not self._head_fired and head_fn:
                    chunks_ok = len(self._chunks) >= self._head_trigger_chunks
                    bytes_ok = self._total_bytes >= self._head_trigger_bytes
                    if chunks_ok or bytes_ok:
                        self._head_fired = True
                        await head_fn(list(self._chunks))

            elif chunk.signal == ChunkSignal.DONE:
                self._complete = True
                if done_fn:
                    await done_fn(list(self._chunks))
                break

        return self._chunks

    def head_content(self) -> List[Any]:
        return [c.content for c in self._chunks[:self._head_trigger_chunks]]

    def all_content(self) -> List[Any]:
        return [c.content for c in self._chunks]

    def stats(self) -> dict:
        return {
            "chunks_received": len(self._chunks),
            "total_bytes": self._total_bytes,
            "head_fired": self._head_fired,
            "complete": self._complete,
        }
```

## Solution 4: Head-First Context Builder

```python
from typing import Any, List, Optional


class HeadFirstContextBuilder:
    """
    Builds a partial LLM context from the first N chunks of a stream,
    allowing the LLM call to begin before the tool stream completes.
    Appends a continuation marker so the model knows more data is coming.
    """

    CONTINUATION_MARKER = "\n[...stream continues — more data loading...]\n"
    COMPLETE_MARKER = "\n[end of stream — {total_chunks} total items]\n"

    def __init__(
        self,
        max_head_tokens: int = 2000,
        chars_per_token: float = 4.0,
    ):
        self._max_chars = int(max_head_tokens * chars_per_token)

    def build_head_context(
        self,
        head_chunks: List[Any],
        tool_name: str,
        stream_complete: bool = False,
        total_chunks: int = 0,
    ) -> str:
        parts = [f"[Tool: {tool_name}]\n"]
        char_budget = self._max_chars - len(parts[0])

        for chunk_content in head_chunks:
            text = str(chunk_content) if chunk_content is not None else ""
            if len(text) > char_budget:
                parts.append(text[:char_budget])
                char_budget = 0
                break
            parts.append(text)
            char_budget -= len(text)
            if char_budget <= 0:
                break

        if stream_complete:
            parts.append(self.COMPLETE_MARKER.format(total_chunks=total_chunks))
        else:
            parts.append(self.CONTINUATION_MARKER)

        return "".join(parts)
```

## Solution 5: Streaming Tool Executor

```python
import asyncio
import time
from typing import Any, Callable, Optional


class StreamingToolExecutor:
    """
    Executes a streaming tool and returns partial context to the caller
    as soon as the head trigger fires, then completes in the background.
    """

    def __init__(
        self,
        adapter: StreamingToolAdapter,
        accumulator: IncrementalResultAccumulator,
        context_builder: HeadFirstContextBuilder,
        stats_recorder: "StreamingToolStatsRecorder",
    ):
        self._adapter = adapter
        self._accumulator = accumulator
        self._context_builder = context_builder
        self._stats = stats_recorder

    async def execute_with_head_first(
        self,
        tool_fn: Callable,
        *args: Any,
        **kwargs: Any,
    ) -> dict:
        start = time.monotonic()
        head_context = None
        head_latency_ms = None

        async def on_head(chunks):
            nonlocal head_context, head_latency_ms
            head_latency_ms = round((time.monotonic() - start) * 1000, 2)
            head_context = self._context_builder.build_head_context(
                [c.content for c in chunks],
                tool_name=self._adapter._tool_name,
                stream_complete=False,
            )

        stream = self._adapter.stream(tool_fn, *args, **kwargs)
        all_chunks = await self._accumulator.consume(stream, head_fn=on_head)

        total_ms = round((time.monotonic() - start) * 1000, 2)
        full_context = self._context_builder.build_head_context(
            [c.content for c in all_chunks],
            tool_name=self._adapter._tool_name,
            stream_complete=True,
            total_chunks=len(all_chunks),
        )

        self._stats.record(
            tool_name=self._adapter._tool_name,
            total_ms=total_ms,
            head_latency_ms=head_latency_ms,
            chunk_count=len(all_chunks),
        )

        return {
            "head_context": head_context,
            "full_context": full_context,
            "head_latency_ms": head_latency_ms,
            "total_latency_ms": total_ms,
            "chunks": len(all_chunks),
            "stats": self._accumulator.stats(),
        }
```

## Solution 6: Streaming Tool Stats Recorder

```python
import time
from typing import List, Optional


class StreamingToolStatsRecorder:
    """
    Tracks head-first latency improvements vs full-buffer baseline.
    """

    def __init__(self, max_records: int = 5000):
        self._max = max_records
        self._records: List[dict] = []

    def record(
        self,
        tool_name: str,
        total_ms: float,
        head_latency_ms: Optional[float],
        chunk_count: int,
    ) -> None:
        if len(self._records) >= self._max:
            self._records.pop(0)
        self._records.append({
            "ts": time.time(),
            "tool_name": tool_name,
            "total_ms": total_ms,
            "head_latency_ms": head_latency_ms,
            "chunk_count": chunk_count,
        })

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [r for r in self._records if r["ts"] >= cutoff]
        if not recent:
            return {"window_seconds": window_seconds, "streams": 0}

        with_head = [r for r in recent if r["head_latency_ms"] is not None]
        if not with_head:
            return {"window_seconds": window_seconds, "streams": len(recent)}

        avg_total = sum(r["total_ms"] for r in with_head) / len(with_head)
        avg_head = sum(r["head_latency_ms"] for r in with_head) / len(with_head)
        savings_pct = round((1 - avg_head / max(avg_total, 1)) * 100, 1)

        return {
            "window_seconds": window_seconds,
            "streams": len(recent),
            "avg_total_ms": round(avg_total, 2),
            "avg_head_latency_ms": round(avg_head, 2),
            "head_first_savings_pct": savings_pct,
            "avg_chunks_per_stream": round(
                sum(r["chunk_count"] for r in recent) / len(recent), 1
            ),
        }
```

## Comparison

| Approach | Async Generator | Head-First Trigger | Partial Context | Background Completion | Latency Stats |
|---|---|---|---|---|---|
| StreamingToolAdapter | Yes | No | No | No | No |
| IncrementalResultAccumulator | No | Yes (chunks + bytes) | No | No | No |
| HeadFirstContextBuilder | No | No | Yes | No | No |
| StreamingToolExecutor | Via adapter | Via accumulator | Via builder | No | Via recorder |
| StreamingToolStatsRecorder | No | No | No | No | Yes |

**Best for production**: Set `head_trigger_chunks=10` and `head_trigger_bytes=4096` — these thresholds provide enough data for a meaningful partial context while firing early enough to overlap LLM processing with stream completion. Use `HeadFirstContextBuilder` with a `CONTINUATION_MARKER` so the LLM understands the context is incomplete — without this marker, the model may treat the truncated result as complete and produce incorrect answers. Monitor `head_first_savings_pct` via `StreamingToolStatsRecorder`: a savings below 20% means the head trigger fires too late (increase `head_trigger_chunks`) or the tool is not genuinely streaming (the first chunk contains the full response). Cap `max_buffer_chunks` to prevent memory exhaustion when streams produce millions of small records.
