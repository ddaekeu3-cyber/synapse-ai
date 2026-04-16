---
title: "Agent Doesn't Implement Response Streaming for Long Outputs"
description: "Agents that buffer the complete LLM response before returning it to the user make users wait 8–20 seconds for the first character of a long answer. Implement response streaming that forwards tokens to the client as they are generated, reducing perceived latency from the full generation time to the time-to-first-token."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-response-streaming-for-long-outputs
tags: [streaming, time-to-first-token, sse, websocket, perceived-latency, progressive-rendering]
symptoms:
  - "User sees a blank response area for 12 seconds before the full answer appears"
  - "Long document generation tasks show no progress indicator — users assume the agent crashed"
  - "No distinction between time-to-first-token and time-to-complete — both reported as latency"
  - "HTTP connection held open for the full generation duration with no data flowing"
  - "Client-side timeout fires before a long but valid response finishes generating"
---

## Why This Happens

Non-streaming LLM integrations call the API, wait for the full response object, then return it. The user's browser or API client receives nothing for the entire generation duration. Streaming requires using the LLM provider's streaming API (which yields tokens as they are produced), forwarding each chunk to the client via Server-Sent Events, WebSocket, or chunked HTTP transfer, and handling partial state (incomplete JSON, interrupted tool calls) gracefully.

## Solution 1: Stream Chunk

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class ChunkType(str, Enum):
    TEXT = "text"
    TOOL_CALL_START = "tool_call_start"
    TOOL_CALL_RESULT = "tool_call_result"
    THINKING = "thinking"
    DONE = "done"
    ERROR = "error"
    METADATA = "metadata"


@dataclass
class StreamChunk:
    chunk_type: ChunkType
    content: str = ""
    delta: str = ""              # incremental text delta
    index: int = 0               # chunk sequence number
    metadata: dict = field(default_factory=dict)
    tool_name: Optional[str] = None
    finish_reason: Optional[str] = None

    def to_sse(self) -> str:
        """Formats as a Server-Sent Event line."""
        import json
        data = {
            "type": self.chunk_type.value,
            "delta": self.delta,
            "index": self.index,
        }
        if self.tool_name:
            data["tool_name"] = self.tool_name
        if self.finish_reason:
            data["finish_reason"] = self.finish_reason
        if self.metadata:
            data["metadata"] = self.metadata
        return f"data: {json.dumps(data)}\n\n"
```

## Solution 2: LLM Stream Adapter

```python
import time
from typing import Any, AsyncIterator, Callable, Optional


class LLMStreamAdapter:
    """
    Adapts a streaming LLM response into a stream of StreamChunks.
    Handles both text deltas and tool call chunks from the provider.
    """

    def __init__(self, chunk_extractor: Optional[Callable[[Any], tuple]] = None):
        self._extractor = chunk_extractor or self._default_extractor

    @staticmethod
    def _default_extractor(raw_chunk: Any) -> tuple:
        """Returns (delta_text, finish_reason, tool_call_info)."""
        choices = getattr(raw_chunk, "choices", [])
        if not choices:
            return "", None, None
        choice = choices[0]
        delta = getattr(choice, "delta", None)
        text = getattr(delta, "content", "") or ""
        finish = getattr(choice, "finish_reason", None)
        tool_calls = getattr(delta, "tool_calls", None)
        return text, finish, tool_calls

    async def adapt(
        self,
        raw_stream: AsyncIterator,
        session_id: str = "",
    ) -> AsyncIterator[StreamChunk]:
        index = 0
        start_time = time.time()

        async for raw_chunk in raw_stream:
            text, finish_reason, tool_calls = self._extractor(raw_chunk)

            if text:
                yield StreamChunk(
                    chunk_type=ChunkType.TEXT,
                    delta=text,
                    index=index,
                )
                index += 1

            if tool_calls:
                for tc in tool_calls:
                    tool_name = getattr(getattr(tc, "function", None), "name", "") or ""
                    yield StreamChunk(
                        chunk_type=ChunkType.TOOL_CALL_START,
                        index=index,
                        tool_name=tool_name,
                    )
                    index += 1

            if finish_reason:
                yield StreamChunk(
                    chunk_type=ChunkType.DONE,
                    index=index,
                    finish_reason=finish_reason,
                    metadata={
                        "session_id": session_id,
                        "generation_ms": round((time.time() - start_time) * 1000, 2),
                    },
                )
                return
```

## Solution 3: Stream Buffer with Reassembly

```python
from typing import List, Optional


class StreamBuffer:
    """
    Buffers stream chunks and provides the reassembled full text.
    Useful for post-processing (tool call extraction, logging)
    without blocking the streaming path.
    """

    def __init__(self):
        self._chunks: List[StreamChunk] = []
        self._full_text: List[str] = []
        self._done = False
        self._finish_reason: Optional[str] = None

    def append(self, chunk: StreamChunk) -> None:
        self._chunks.append(chunk)
        if chunk.chunk_type == ChunkType.TEXT and chunk.delta:
            self._full_text.append(chunk.delta)
        if chunk.chunk_type == ChunkType.DONE:
            self._done = True
            self._finish_reason = chunk.finish_reason

    def full_text(self) -> str:
        return "".join(self._full_text)

    def is_complete(self) -> bool:
        return self._done

    def chunk_count(self) -> int:
        return len(self._chunks)

    def tool_calls_seen(self) -> List[str]:
        return [
            c.tool_name for c in self._chunks
            if c.chunk_type == ChunkType.TOOL_CALL_START and c.tool_name
        ]
```

## Solution 4: SSE Stream Writer

```python
import asyncio
from typing import AsyncIterator, Callable, Optional


class SSEStreamWriter:
    """
    Writes StreamChunks to an HTTP response as Server-Sent Events.
    Handles backpressure, client disconnection, and heartbeat keepalives.
    """

    def __init__(
        self,
        write_fn: Callable[[str], None],
        heartbeat_interval_seconds: float = 15.0,
        buffer: Optional[StreamBuffer] = None,
    ):
        self._write = write_fn
        self._heartbeat_interval = heartbeat_interval_seconds
        self._buffer = buffer

    async def write_stream(
        self,
        stream: AsyncIterator[StreamChunk],
        on_complete: Optional[Callable[[StreamBuffer], None]] = None,
    ) -> None:
        heartbeat_task = asyncio.create_task(self._heartbeat())

        try:
            async for chunk in stream:
                sse_line = chunk.to_sse()
                self._write(sse_line)
                if self._buffer:
                    self._buffer.append(chunk)
                if chunk.chunk_type == ChunkType.DONE:
                    break
        finally:
            heartbeat_task.cancel()
            if on_complete and self._buffer:
                on_complete(self._buffer)

    async def _heartbeat(self) -> None:
        while True:
            await asyncio.sleep(self._heartbeat_interval)
            self._write(": keepalive\n\n")
```

## Solution 5: Streaming Latency Tracker

```python
import time
from threading import Lock
from typing import List, Optional


class StreamingLatencyTracker:
    """
    Tracks time-to-first-token (TTFT) and total generation time separately.
    TTFT is the key perceived latency metric for streaming responses.
    """

    def __init__(self):
        self._records: List[dict] = []
        self._lock = Lock()

    def record(
        self,
        ttft_ms: float,
        total_ms: float,
        chunk_count: int,
        session_id: str = "",
    ) -> None:
        with self._lock:
            self._records.append({
                "ts": time.time(),
                "ttft_ms": ttft_ms,
                "total_ms": total_ms,
                "chunk_count": chunk_count,
                "session_id": session_id,
            })
            if len(self._records) > 10000:
                self._records.pop(0)

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        with self._lock:
            recent = [r for r in self._records if r["ts"] >= cutoff]
        if not recent:
            return {"window_seconds": window_seconds, "responses": 0}

        ttfts = sorted(r["ttft_ms"] for r in recent)
        totals = sorted(r["total_ms"] for r in recent)

        def pct(vals: list, p: float) -> float:
            idx = min(int(len(vals) * p / 100), len(vals) - 1)
            return round(vals[idx], 2)

        return {
            "window_seconds": window_seconds,
            "responses": len(recent),
            "ttft_p50_ms": pct(ttfts, 50),
            "ttft_p95_ms": pct(ttfts, 95),
            "total_p50_ms": pct(totals, 50),
            "total_p95_ms": pct(totals, 95),
            "avg_chunks": round(sum(r["chunk_count"] for r in recent) / len(recent), 1),
        }
```

## Solution 6: Streaming Response Dashboard

```python
import time


class StreamingResponseDashboard:
    """
    Combines TTFT tracking, chunk count stats, and streaming health.
    """

    def __init__(self, tracker: StreamingLatencyTracker):
        self._tracker = tracker

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "streaming_metrics_1h": self._tracker.summary(3600.0),
            "streaming_metrics_5m": self._tracker.summary(300.0),
        }
```

## Comparison

| Approach | Token-Level Streaming | SSE Format | Buffer & Reassembly | TTFT Tracking | Heartbeat |
|---|---|---|---|---|---|
| LLMStreamAdapter | Yes | No | No | No | No |
| SSEStreamWriter | No | Yes | Via buffer | No | Yes |
| StreamBuffer | No | No | Yes | No | No |
| StreamingLatencyTracker | No | No | No | Yes (TTFT + total) | No |
| StreamingResponseDashboard | No | No | No | Via tracker | No |

**Best for production**: Track TTFT as a separate SLO from total generation time — users perceive the wait until the first word, not the wait until the last. A P95 TTFT above 2 seconds indicates either a slow first-token generation (model loading or context processing overhead) or a bug in the streaming path that buffers before forwarding. Set a `heartbeat_interval_seconds=15` for long generations — without keepalives, load balancers and proxies close idle connections after 30–60 seconds, aborting long streaming responses. Use `StreamBuffer` in parallel with the SSE path for logging without blocking the stream.
