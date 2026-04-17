---
title: "Agent Doesn't Implement Incremental Result Streaming to UI"
description: "Agents that accumulate the full response before sending it to the UI force users to wait through the entire generation, losing the latency advantage of streaming LLM APIs. Implement incremental result streaming that delivers partial tokens to the UI as they arrive, with backpressure control, chunk buffering, and graceful fallback to buffered mode when the client cannot keep up."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-incremental-result-streaming-to-ui
tags: [streaming, incremental-results, sse, websocket, backpressure, time-to-first-token]
symptoms:
  - "Users see a spinner for 10+ seconds before any text appears, even though streaming is enabled on the LLM API"
  - "Agent accumulates the full LLM response in memory before forwarding it downstream"
  - "No server-sent events or WebSocket push — response is a single JSON payload"
  - "Slow UI clients cause memory growth in the agent process (no backpressure)"
  - "Cannot measure time-to-first-token because the UI never receives partial tokens"
---

## Why This Happens

Streaming LLM APIs deliver tokens incrementally, but agents often accumulate them with `response = await client.complete(...)` which internally buffers the full stream. Even when the SDK exposes a streaming interface, the agent may still reassemble chunks before passing them forward. Incremental delivery to the UI requires an explicit streaming pipeline: the agent consumes LLM token events, optionally buffers small chunks into meaningful units, and pushes them to the UI transport (SSE, WebSocket, or HTTP chunked transfer) as they arrive. Backpressure must be handled so a slow client does not cause the agent to buffer unboundedly.

## Solution 1: Stream Chunk

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional
import time


class ChunkType(str, Enum):
    TOKEN = "token"           # partial LLM output token(s)
    TOOL_START = "tool_start" # tool call beginning
    TOOL_RESULT = "tool_result"  # tool call result
    THOUGHT = "thought"       # chain-of-thought fragment
    DONE = "done"             # stream complete
    ERROR = "error"           # stream error


@dataclass
class StreamChunk:
    chunk_type: ChunkType
    content: str
    sequence: int             # monotone counter for ordering
    timestamp: float = field(default_factory=time.time)
    metadata: dict = field(default_factory=dict)
    is_final: bool = False

    def to_sse(self) -> str:
        """Format as Server-Sent Events data line."""
        import json
        payload = {
            "type": self.chunk_type.value,
            "content": self.content,
            "seq": self.sequence,
            "ts": round(self.timestamp, 3),
        }
        if self.metadata:
            payload["meta"] = self.metadata
        if self.is_final:
            payload["final"] = True
        return f"data: {json.dumps(payload)}\n\n"
```

## Solution 2: Token Accumulation Buffer

```python
import asyncio
from typing import AsyncIterator, List, Optional


class TokenAccumulationBuffer:
    """
    Accumulates raw LLM tokens until a flush condition is met:
    - Enough characters have accumulated (min_chars)
    - A sentence boundary has been reached
    - A maximum wait time has elapsed
    Prevents single-character SSE events while maintaining low latency.
    """

    SENTENCE_BOUNDARY = frozenset(".!?\n")

    def __init__(
        self,
        min_chars: int = 8,
        max_wait_ms: float = 50.0,
        flush_on_boundary: bool = True,
    ):
        self._min_chars = min_chars
        self._max_wait_ms = max_wait_ms
        self._flush_on_boundary = flush_on_boundary
        self._buffer: List[str] = []
        self._last_flush = asyncio.get_event_loop().time()

    def push(self, token: str) -> Optional[str]:
        """
        Add a token. Returns accumulated content to flush, or None.
        """
        self._buffer.append(token)
        accumulated = "".join(self._buffer)
        now = asyncio.get_event_loop().time()
        elapsed_ms = (now - self._last_flush) * 1000

        should_flush = (
            len(accumulated) >= self._min_chars
            or elapsed_ms >= self._max_wait_ms
            or (self._flush_on_boundary and token and token[-1] in self.SENTENCE_BOUNDARY)
        )

        if should_flush:
            return self._flush()
        return None

    def flush_remaining(self) -> Optional[str]:
        if self._buffer:
            return self._flush()
        return None

    def _flush(self) -> str:
        content = "".join(self._buffer)
        self._buffer.clear()
        self._last_flush = asyncio.get_event_loop().time()
        return content
```

## Solution 3: Streaming Output Queue with Backpressure

```python
import asyncio
from typing import Optional


class StreamingOutputQueue:
    """
    Bounded async queue between the LLM token producer and the UI transport.
    When the queue is full, the producer is back-pressured via asyncio.wait_for.
    Tracks queue depth for monitoring.
    """

    def __init__(
        self,
        maxsize: int = 64,
        producer_timeout_s: float = 5.0,
    ):
        self._queue: asyncio.Queue[Optional[StreamChunk]] = asyncio.Queue(maxsize=maxsize)
        self._timeout = producer_timeout_s
        self._dropped = 0
        self._produced = 0
        self._consumed = 0

    async def put(self, chunk: StreamChunk) -> bool:
        """
        Returns False if backpressure timeout exceeded (chunk dropped).
        """
        try:
            await asyncio.wait_for(
                self._queue.put(chunk),
                timeout=self._timeout,
            )
            self._produced += 1
            return True
        except asyncio.TimeoutError:
            self._dropped += 1
            return False

    async def get(self) -> Optional[StreamChunk]:
        """Returns None as the sentinel to signal stream end."""
        chunk = await self._queue.get()
        if chunk is not None:
            self._consumed += 1
        return chunk

    async def close(self) -> None:
        await self._queue.put(None)

    def stats(self) -> dict:
        return {
            "queue_depth": self._queue.qsize(),
            "produced": self._produced,
            "consumed": self._consumed,
            "dropped": self._dropped,
        }
```

## Solution 4: Incremental Stream Dispatcher

```python
import asyncio
import time
from typing import AsyncIterator, Callable, Optional


class IncrementalStreamDispatcher:
    """
    Consumes a raw LLM token async iterator, buffers tokens via
    TokenAccumulationBuffer, and forwards StreamChunks to the output
    queue. Measures time-to-first-token and total stream duration.
    """

    def __init__(
        self,
        buffer: TokenAccumulationBuffer,
        output_queue: StreamingOutputQueue,
    ):
        self._buffer = buffer
        self._queue = output_queue
        self._sequence = 0
        self._first_token_ms: Optional[float] = None
        self._start: float = 0.0

    def _next_seq(self) -> int:
        self._sequence += 1
        return self._sequence

    async def dispatch(
        self,
        token_stream: AsyncIterator[str],
        on_tool_call: Optional[Callable] = None,
    ) -> dict:
        self._start = time.time()

        async for token in token_stream:
            if self._first_token_ms is None:
                self._first_token_ms = round((time.time() - self._start) * 1000, 2)

            flushed = self._buffer.push(token)
            if flushed:
                await self._queue.put(StreamChunk(
                    chunk_type=ChunkType.TOKEN,
                    content=flushed,
                    sequence=self._next_seq(),
                ))

        # Flush remaining buffer
        remaining = self._buffer.flush_remaining()
        if remaining:
            await self._queue.put(StreamChunk(
                chunk_type=ChunkType.TOKEN,
                content=remaining,
                sequence=self._next_seq(),
            ))

        # Send done sentinel chunk
        await self._queue.put(StreamChunk(
            chunk_type=ChunkType.DONE,
            content="",
            sequence=self._next_seq(),
            is_final=True,
        ))
        await self._queue.close()

        total_ms = round((time.time() - self._start) * 1000, 2)
        return {
            "ttft_ms": self._first_token_ms,
            "total_ms": total_ms,
            "chunks_sent": self._sequence,
            "queue_stats": self._queue.stats(),
        }
```

## Solution 5: SSE Transport Writer

```python
import asyncio
from typing import Any, Callable, Optional


class SSETransportWriter:
    """
    Consumes StreamChunks from the output queue and writes them as
    Server-Sent Events to the response writer (e.g., an aiohttp or
    FastAPI StreamingResponse write callable).
    Switches to buffered fallback if the client disconnects mid-stream.
    """

    def __init__(
        self,
        write_fn: Callable[[str], Any],   # async callable that sends bytes/str
        disconnect_event: Optional[asyncio.Event] = None,
    ):
        self._write = write_fn
        self._disconnect = disconnect_event or asyncio.Event()
        self._bytes_sent = 0
        self._chunks_written = 0
        self._client_dropped = False

    async def run(self, queue: StreamingOutputQueue) -> dict:
        while True:
            if self._disconnect.is_set():
                self._client_dropped = True
                break

            chunk = await queue.get()
            if chunk is None:
                break  # stream finished

            sse_line = chunk.to_sse()
            try:
                result = self._write(sse_line)
                if asyncio.iscoroutine(result):
                    await result
                self._bytes_sent += len(sse_line.encode())
                self._chunks_written += 1
            except (ConnectionResetError, BrokenPipeError):
                self._client_dropped = True
                break

        return {
            "bytes_sent": self._bytes_sent,
            "chunks_written": self._chunks_written,
            "client_dropped": self._client_dropped,
        }
```

## Solution 6: Streaming Latency Tracker

```python
import time
from collections import deque
from threading import Lock
from typing import Deque, List, Optional, Tuple


class StreamingLatencyTracker:
    """
    Records TTFT and total stream duration across sessions.
    Supports percentile queries for streaming SLO tracking.
    """

    def __init__(self, max_records: int = 5000):
        self._max = max_records
        self._records: Deque[Tuple[float, float, float]] = deque()
        # (recorded_at, ttft_ms, total_ms)
        self._lock = Lock()

    def record(self, ttft_ms: Optional[float], total_ms: float) -> None:
        if ttft_ms is None:
            return
        with self._lock:
            self._records.append((time.time(), ttft_ms, total_ms))
            if len(self._records) > self._max:
                self._records.popleft()

    def percentile(
        self, field: str, pct: float, window_seconds: float = 3600.0
    ) -> Optional[float]:
        cutoff = time.time() - window_seconds
        idx_map = {"ttft": 1, "total": 2}
        idx = idx_map.get(field, 1)
        with self._lock:
            values = sorted(r[idx] for r in self._records if r[0] >= cutoff)
        if not values:
            return None
        i = min(int(len(values) * pct / 100.0), len(values) - 1)
        return round(values[i], 2)

    def summary(self, window_seconds: float = 3600.0) -> dict:
        return {
            "window_seconds": window_seconds,
            "ttft_p50_ms": self.percentile("ttft", 50, window_seconds),
            "ttft_p95_ms": self.percentile("ttft", 95, window_seconds),
            "total_p50_ms": self.percentile("total", 50, window_seconds),
            "total_p95_ms": self.percentile("total", 95, window_seconds),
        }
```

## Comparison

| Approach | Token Buffering | Backpressure | SSE Transport | TTFT Tracking | Disconnect Handling |
|---|---|---|---|---|---|
| TokenAccumulationBuffer | Yes (boundary + time) | No | No | No | No |
| StreamingOutputQueue | No | Yes (bounded + timeout) | No | No | No |
| IncrementalStreamDispatcher | Via buffer | Via queue | No | Yes | No |
| SSETransportWriter | No | Via queue | Yes | No | Yes |
| StreamingLatencyTracker | No | No | No | Yes (P50/P95) | No |

**Best for production**: Set `maxsize=64` on `StreamingOutputQueue` and `producer_timeout_s=2.0` — a slow client that cannot drain 64 chunks in 2 seconds will have chunks dropped rather than causing the agent process to buffer indefinitely. Use `min_chars=12` on `TokenAccumulationBuffer` to avoid single-character SSE events that saturate the HTTP connection. Track TTFT P95 as a first-class SLO — it is the metric users notice, not total latency. If TTFT P95 exceeds 800ms for an otherwise fast model, the bottleneck is pre-generation work (tool calls, retrieval) and should be optimized there rather than in the streaming pipeline.
