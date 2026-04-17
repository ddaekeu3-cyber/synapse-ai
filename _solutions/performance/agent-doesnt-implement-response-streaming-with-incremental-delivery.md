---
title: "Agent Doesn't Implement Response Streaming with Incremental Delivery"
description: "Agents that buffer the complete LLM response before delivering it to the caller add unnecessary latency: the user sees nothing for several seconds, then receives the full answer at once. Implement response streaming that delivers tokens incrementally as they arrive from the model, reducing time-to-first-token, enabling early rendering in UIs, and allowing callers to cancel mid-stream when they have seen enough."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-response-streaming-with-incremental-delivery
tags: [streaming, incremental-delivery, time-to-first-token, async-generator, backpressure, sse]
symptoms:
  - "Users see a blank screen for 3-8 seconds before any text appears"
  - "LLM response is assembled in memory then returned as a single string"
  - "No way for the caller to cancel a response after seeing the first sentence"
  - "Time-to-first-token and total latency are the same metric — streaming is not tracked separately"
  - "Large responses cause memory spikes because the full content is held before delivery"
---

## Why This Happens

Most LLM SDK calls have both a blocking form (`response = await client.complete(...)`) and a streaming form (`async for chunk in client.stream(...)`). Agents default to the blocking form because it is simpler to handle: a single string comes back, it is passed to tool parsers, and the cycle continues. The cost is that the caller receives nothing until the entire response is generated — seconds of silence for long answers. Switching to streaming requires propagating an async generator through the agent's response path and handling partial chunks carefully so tool-call extraction still works on the assembled stream.

## Solution 1: Stream Chunk

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class ChunkType(str, Enum):
    TEXT = "text"
    TOOL_CALL_START = "tool_call_start"
    TOOL_CALL_DELTA = "tool_call_delta"
    TOOL_CALL_END = "tool_call_end"
    STOP = "stop"
    ERROR = "error"


@dataclass
class StreamChunk:
    chunk_type: ChunkType
    text: str = ""
    tool_name: Optional[str] = None
    tool_call_id: Optional[str] = None
    index: int = 0                        # sequential chunk index
    finish_reason: Optional[str] = None
    error_message: Optional[str] = None
    metadata: dict = field(default_factory=dict)

    @property
    def is_terminal(self) -> bool:
        return self.chunk_type in (ChunkType.STOP, ChunkType.ERROR)
```

## Solution 2: Streaming LLM Adapter

```python
import asyncio
import time
from typing import AsyncGenerator, Callable, List, Optional


class StreamingLLMAdapter:
    """
    Wraps an LLM client's streaming interface and emits StreamChunks.
    Accumulates the full text in parallel so callers that need the
    complete response can await it without re-reading all chunks.
    """

    def __init__(
        self,
        stream_fn: Callable,         # async generator: stream_fn(**kwargs) -> AsyncGenerator[raw_chunk]
        chunk_parser: Callable,      # raw_chunk -> StreamChunk
    ):
        self._stream_fn = stream_fn
        self._chunk_parser = chunk_parser

    async def stream(
        self,
        **kwargs,
    ) -> AsyncGenerator[StreamChunk, None]:
        index = 0
        try:
            async for raw in self._stream_fn(**kwargs):
                chunk = self._chunk_parser(raw)
                chunk.index = index
                index += 1
                yield chunk
                if chunk.is_terminal:
                    return
        except Exception as exc:
            yield StreamChunk(
                chunk_type=ChunkType.ERROR,
                error_message=str(exc),
                index=index,
            )
```

## Solution 3: Stream Buffer and Assembler

```python
import asyncio
from typing import AsyncGenerator, List, Optional, Tuple


class StreamBufferAndAssembler:
    """
    Consumes a StreamChunk generator, buffers chunks, and provides
    both an incremental yield interface and a completed-text awaitable.
    Useful when downstream code needs both streaming output and the
    final assembled string (e.g., for tool-call extraction).
    """

    def __init__(self):
        self._chunks: List[StreamChunk] = []
        self._text_parts: List[str] = []
        self._done = asyncio.Event()
        self._error: Optional[str] = None

    async def feed(self, source: AsyncGenerator[StreamChunk, None]) -> None:
        """Drive the source generator and store all chunks."""
        async for chunk in source:
            self._chunks.append(chunk)
            if chunk.chunk_type == ChunkType.TEXT:
                self._text_parts.append(chunk.text)
            if chunk.chunk_type == ChunkType.ERROR:
                self._error = chunk.error_message
            if chunk.is_terminal:
                break
        self._done.set()

    async def assembled_text(self) -> str:
        """Await completion and return the full response text."""
        await self._done.wait()
        if self._error:
            raise RuntimeError(f"stream error: {self._error}")
        return "".join(self._text_parts)

    def chunks_so_far(self) -> List[StreamChunk]:
        return list(self._chunks)

    def is_complete(self) -> bool:
        return self._done.is_set()
```

## Solution 4: Backpressure-Aware Stream Relay

```python
import asyncio
from typing import AsyncGenerator, Optional


class BackpressureAwareStreamRelay:
    """
    Relays StreamChunks to a caller-controlled queue with bounded
    capacity. When the queue is full, the relay pauses reading from
    the source — providing natural backpressure so a slow consumer
    does not cause memory growth or dropped chunks.
    """

    def __init__(self, queue_capacity: int = 64):
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=queue_capacity)
        self._source_task: Optional[asyncio.Task] = None

    async def start(self, source: AsyncGenerator[StreamChunk, None]) -> None:
        """Start feeding chunks into the queue from the source."""
        async def _feed():
            async for chunk in source:
                await self._queue.put(chunk)
                if chunk.is_terminal:
                    break
            await self._queue.put(None)  # sentinel

        self._source_task = asyncio.create_task(_feed())

    async def __aiter__(self):
        while True:
            item = await self._queue.get()
            if item is None:
                return
            yield item
            if item.is_terminal:
                return

    async def cancel(self) -> None:
        if self._source_task and not self._source_task.done():
            self._source_task.cancel()
            try:
                await self._source_task
            except asyncio.CancelledError:
                pass
```

## Solution 5: Streaming Latency Tracker

```python
import time
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class StreamLatencyRecord:
    request_id: str
    started_at: float
    first_chunk_at: Optional[float] = None
    last_chunk_at: Optional[float] = None
    chunk_count: int = 0
    total_chars: int = 0
    cancelled: bool = False

    @property
    def time_to_first_token_ms(self) -> Optional[float]:
        if self.first_chunk_at is None:
            return None
        return round((self.first_chunk_at - self.started_at) * 1000, 2)

    @property
    def total_duration_ms(self) -> Optional[float]:
        if self.last_chunk_at is None:
            return None
        return round((self.last_chunk_at - self.started_at) * 1000, 2)

    @property
    def throughput_chars_per_sec(self) -> Optional[float]:
        dur = self.total_duration_ms
        if dur is None or dur == 0:
            return None
        return round(self.total_chars / (dur / 1000), 1)


class StreamingLatencyTracker:
    """
    Records TTFT (time-to-first-token) and total stream duration
    separately, enabling dashboards to distinguish streaming latency
    from generation throughput.
    """

    def __init__(self):
        self._records: List[StreamLatencyRecord] = []

    def start(self, request_id: str) -> StreamLatencyRecord:
        record = StreamLatencyRecord(
            request_id=request_id,
            started_at=time.time(),
        )
        self._records.append(record)
        return record

    def on_chunk(self, record: StreamLatencyRecord, chunk: StreamChunk) -> None:
        now = time.time()
        if record.first_chunk_at is None and chunk.chunk_type == ChunkType.TEXT:
            record.first_chunk_at = now
        record.last_chunk_at = now
        record.chunk_count += 1
        record.total_chars += len(chunk.text)

    def on_cancel(self, record: StreamLatencyRecord) -> None:
        record.cancelled = True
        record.last_chunk_at = time.time()

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [r for r in self._records if r.started_at >= cutoff]
        if not recent:
            return {"window_seconds": window_seconds, "streams": 0}

        ttfts = [r.time_to_first_token_ms for r in recent if r.time_to_first_token_ms is not None]
        durations = [r.total_duration_ms for r in recent if r.total_duration_ms is not None]

        def pct(values, p):
            if not values:
                return None
            s = sorted(values)
            return round(s[min(int(len(s) * p / 100), len(s) - 1)], 2)

        return {
            "window_seconds": window_seconds,
            "streams": len(recent),
            "cancelled": sum(1 for r in recent if r.cancelled),
            "ttft_p50_ms": pct(ttfts, 50),
            "ttft_p95_ms": pct(ttfts, 95),
            "duration_p50_ms": pct(durations, 50),
            "duration_p95_ms": pct(durations, 95),
        }
```

## Solution 6: SSE Stream Formatter

```python
import json
from typing import AsyncGenerator, Optional


class SSEStreamFormatter:
    """
    Formats StreamChunks as Server-Sent Events for delivery to web
    clients. Emits data: lines with JSON payloads and a final
    data: [DONE] sentinel compatible with OpenAI's streaming protocol.
    """

    @staticmethod
    def format_chunk(chunk: StreamChunk, event_name: str = "message") -> str:
        payload = {
            "type": chunk.chunk_type.value,
            "index": chunk.index,
        }
        if chunk.text:
            payload["text"] = chunk.text
        if chunk.tool_name:
            payload["tool_name"] = chunk.tool_name
        if chunk.tool_call_id:
            payload["tool_call_id"] = chunk.tool_call_id
        if chunk.finish_reason:
            payload["finish_reason"] = chunk.finish_reason
        if chunk.error_message:
            payload["error"] = chunk.error_message

        lines = [f"event: {event_name}", f"data: {json.dumps(payload)}", ""]
        return "\n".join(lines) + "\n"

    @staticmethod
    def done_sentinel() -> str:
        return "data: [DONE]\n\n"

    @classmethod
    async def format_stream(
        cls,
        source: AsyncGenerator[StreamChunk, None],
    ) -> AsyncGenerator[str, None]:
        async for chunk in source:
            yield cls.format_chunk(chunk)
            if chunk.is_terminal:
                yield cls.done_sentinel()
                return
        yield cls.done_sentinel()
```

## Comparison

| Approach | Incremental Delivery | Backpressure | Full Text Assembly | TTFT Tracking | SSE Output |
|---|---|---|---|---|---|
| StreamingLLMAdapter | Yes (async generator) | No | No | No | No |
| StreamBufferAndAssembler | Via feed() | No | Yes (await) | No | No |
| BackpressureAwareStreamRelay | Yes (bounded queue) | Yes | No | No | No |
| StreamingLatencyTracker | No | No | No | Yes (TTFT+duration) | No |
| SSEStreamFormatter | Via format_stream() | No | No | No | Yes |

**Best for production**: Wrap every LLM call with `StreamingLLMAdapter` and route chunks through `BackpressureAwareStreamRelay` — this prevents memory growth when the consumer (HTTP response writer, WebSocket sender) is slower than the model. Run `StreamBufferAndAssembler.feed()` in a parallel task so tool-call extraction can operate on the assembled text while the stream is still being relayed to the client. Track `ttft_p95_ms` via `StreamingLatencyTracker` as a separate SLO from total latency: a regression in TTFT usually points to model routing or load balancing issues, while a regression in total duration points to context length or generation throughput.
