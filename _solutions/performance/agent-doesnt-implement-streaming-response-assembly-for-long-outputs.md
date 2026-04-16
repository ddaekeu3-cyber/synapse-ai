---
title: "Agent Doesn't Implement Streaming Response Assembly for Long Outputs"
description: "Agents that buffer complete LLM responses before returning them force users to wait for the full generation before seeing any output — a 10-second wait for a 2000-token response. Implement streaming response assembly that yields partial response chunks as they arrive from the LLM, assembles tool-call fragments incrementally, and handles stream interruption and reconnection without losing output."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-streaming-response-assembly-for-long-outputs
tags: [streaming, response-assembly, incremental-output, time-to-first-token, chunk-processing, sse]
symptoms:
  - "Users see a blank screen for 8-12 seconds before the full response appears at once"
  - "Time-to-first-token is identical to time-to-last-token — nothing streams"
  - "Long responses timeout at the HTTP gateway because the full generation takes too long"
  - "Tool call arguments cannot be parsed until the entire response is buffered"
  - "No way to cancel a long generation mid-stream from the user side"
---

## Why This Happens

The standard LLM client usage is `response = await client.complete(prompt)` which waits for the full response. Streaming APIs return an async iterator of chunks — partial text tokens, tool-call deltas, finish reasons — that must be assembled into a coherent response. Most agents do not implement streaming because assembling partial tool-call JSON from incremental deltas is non-trivial. Without streaming, perceived latency equals total generation time; with streaming, perceived latency equals time-to-first-token, which is typically 200-500ms regardless of total length.

## Solution 1: Response Chunk

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class ChunkType(str, Enum):
    TEXT_DELTA = "text_delta"
    TOOL_CALL_START = "tool_call_start"
    TOOL_CALL_DELTA = "tool_call_delta"
    TOOL_CALL_END = "tool_call_end"
    FINISH = "finish"
    ERROR = "error"


@dataclass
class ResponseChunk:
    chunk_type: ChunkType
    index: int = 0
    text: Optional[str] = None
    tool_call_id: Optional[str] = None
    tool_name: Optional[str] = None
    tool_args_delta: Optional[str] = None
    finish_reason: Optional[str] = None
    error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
```

## Solution 2: Streaming Response Assembler

```python
import json
from dataclasses import dataclass, field
from typing import AsyncIterator, Dict, List, Optional, Tuple


@dataclass
class AssembledToolCall:
    tool_call_id: str
    tool_name: str
    arguments_json: str = ""
    complete: bool = False

    def parse_arguments(self) -> dict:
        try:
            return json.loads(self.arguments_json)
        except json.JSONDecodeError:
            return {}


@dataclass
class StreamAssemblyState:
    text_chunks: List[str] = field(default_factory=list)
    tool_calls: Dict[str, AssembledToolCall] = field(default_factory=dict)
    finish_reason: Optional[str] = None
    error: Optional[str] = None
    chunk_count: int = 0

    def full_text(self) -> str:
        return "".join(self.text_chunks)

    def complete_tool_calls(self) -> List[AssembledToolCall]:
        return [tc for tc in self.tool_calls.values() if tc.complete]


class StreamingResponseAssembler:
    """
    Consumes a stream of ResponseChunks and assembles them into
    coherent text and tool-call objects.
    Yields text chunks immediately for forwarding to the client.
    Yields complete tool-call objects when all argument deltas have arrived.
    """

    def __init__(self):
        self._state = StreamAssemblyState()

    async def process(
        self,
        stream: AsyncIterator[ResponseChunk],
    ) -> AsyncIterator[Tuple[str, Optional[object]]]:
        """
        Yields (event_type, payload) tuples:
        - ("text", str) for each text delta
        - ("tool_call", AssembledToolCall) when a tool call is complete
        - ("finish", finish_reason) when generation ends
        - ("error", error_message) on stream error
        """
        async for chunk in stream:
            self._state.chunk_count += 1

            if chunk.chunk_type == ChunkType.TEXT_DELTA and chunk.text:
                self._state.text_chunks.append(chunk.text)
                yield "text", chunk.text

            elif chunk.chunk_type == ChunkType.TOOL_CALL_START:
                tc = AssembledToolCall(
                    tool_call_id=chunk.tool_call_id or f"tc_{chunk.index}",
                    tool_name=chunk.tool_name or "",
                )
                self._state.tool_calls[tc.tool_call_id] = tc

            elif chunk.chunk_type == ChunkType.TOOL_CALL_DELTA:
                tc = self._state.tool_calls.get(chunk.tool_call_id or "")
                if tc and chunk.tool_args_delta:
                    tc.arguments_json += chunk.tool_args_delta

            elif chunk.chunk_type == ChunkType.TOOL_CALL_END:
                tc = self._state.tool_calls.get(chunk.tool_call_id or "")
                if tc:
                    tc.complete = True
                    yield "tool_call", tc

            elif chunk.chunk_type == ChunkType.FINISH:
                self._state.finish_reason = chunk.finish_reason
                yield "finish", chunk.finish_reason

            elif chunk.chunk_type == ChunkType.ERROR:
                self._state.error = chunk.error
                yield "error", chunk.error

    def state(self) -> StreamAssemblyState:
        return self._state
```

## Solution 3: Server-Sent Events Formatter

```python
import json
from typing import Any, AsyncIterator, Optional


class SSEFormatter:
    """
    Formats assembled stream events as Server-Sent Events (SSE)
    for delivery to browser or HTTP clients.
    """

    @staticmethod
    def format_event(
        event_type: str,
        data: Any,
        event_id: Optional[int] = None,
    ) -> str:
        payload = json.dumps({"type": event_type, "data": data}, ensure_ascii=False)
        lines = []
        if event_id is not None:
            lines.append(f"id: {event_id}")
        lines.append(f"event: {event_type}")
        lines.append(f"data: {payload}")
        lines.append("")
        return "\n".join(lines) + "\n"

    @staticmethod
    async def stream_to_sse(
        event_stream: AsyncIterator,
    ) -> AsyncIterator[str]:
        event_id = 0
        async for event_type, payload in event_stream:
            event_id += 1
            if event_type == "text":
                yield SSEFormatter.format_event("text_delta", {"text": payload}, event_id)
            elif event_type == "tool_call":
                yield SSEFormatter.format_event("tool_call", {
                    "id": payload.tool_call_id,
                    "name": payload.tool_name,
                    "arguments": payload.parse_arguments(),
                }, event_id)
            elif event_type == "finish":
                yield SSEFormatter.format_event("done", {"finish_reason": payload}, event_id)
            elif event_type == "error":
                yield SSEFormatter.format_event("error", {"message": payload}, event_id)
```

## Solution 4: Interruptible Stream Handler

```python
import asyncio
from typing import AsyncIterator, Callable, Optional


class InterruptibleStreamHandler:
    """
    Wraps a streaming response assembler with cancellation support.
    Callers can call interrupt() to stop consuming the stream.
    Yields the partial assembled state on interruption.
    """

    def __init__(self, assembler: StreamingResponseAssembler):
        self._assembler = assembler
        self._interrupted = False
        self._cancel_event = asyncio.Event()

    def interrupt(self) -> None:
        self._interrupted = True
        self._cancel_event.set()

    async def run(
        self,
        stream: AsyncIterator[ResponseChunk],
        on_text: Optional[Callable[[str], None]] = None,
        on_tool_call: Optional[Callable[["AssembledToolCall"], None]] = None,
    ) -> StreamAssemblyState:
        async def guarded_stream():
            async for chunk in stream:
                if self._interrupted:
                    return
                yield chunk

        async for event_type, payload in self._assembler.process(guarded_stream()):
            if self._interrupted:
                break
            if event_type == "text" and on_text:
                on_text(payload)
            elif event_type == "tool_call" and on_tool_call:
                on_tool_call(payload)

        return self._assembler.state()
```

## Solution 5: Stream Checkpoint Writer

```python
import json
import time
from typing import List, Optional


class StreamCheckpointWriter:
    """
    Periodically checkpoints partial stream state to allow resumption
    after connection interruption.
    Checkpoints are stored in memory; replace _store with Redis for multi-process.
    """

    def __init__(self, checkpoint_interval_chunks: int = 20):
        self._interval = checkpoint_interval_chunks
        self._checkpoints: dict = {}

    def maybe_checkpoint(
        self,
        session_id: str,
        state: StreamAssemblyState,
    ) -> Optional[dict]:
        if state.chunk_count % self._interval != 0:
            return None
        checkpoint = {
            "session_id": session_id,
            "chunk_count": state.chunk_count,
            "partial_text": state.full_text(),
            "tool_calls": [
                {"id": tc.tool_call_id, "name": tc.tool_name,
                 "args": tc.arguments_json, "complete": tc.complete}
                for tc in state.tool_calls.values()
            ],
            "saved_at": time.time(),
        }
        self._checkpoints[session_id] = checkpoint
        return checkpoint

    def load(self, session_id: str) -> Optional[dict]:
        return self._checkpoints.get(session_id)

    def clear(self, session_id: str) -> None:
        self._checkpoints.pop(session_id, None)
```

## Solution 6: Stream Latency Tracker

```python
import time
from typing import Optional


class StreamLatencyTracker:
    """
    Measures time-to-first-token, time-to-first-tool-call, and total stream duration.
    """

    def __init__(self):
        self._started_at: Optional[float] = None
        self._first_token_at: Optional[float] = None
        self._first_tool_call_at: Optional[float] = None
        self._finished_at: Optional[float] = None

    def on_stream_start(self) -> None:
        self._started_at = time.time()

    def on_first_token(self) -> None:
        if self._first_token_at is None:
            self._first_token_at = time.time()

    def on_first_tool_call(self) -> None:
        if self._first_tool_call_at is None:
            self._first_tool_call_at = time.time()

    def on_stream_finish(self) -> None:
        self._finished_at = time.time()

    def metrics(self) -> dict:
        start = self._started_at or time.time()
        return {
            "time_to_first_token_ms": round(
                ((self._first_token_at or 0) - start) * 1000, 1
            ) if self._first_token_at else None,
            "time_to_first_tool_call_ms": round(
                ((self._first_tool_call_at or 0) - start) * 1000, 1
            ) if self._first_tool_call_at else None,
            "total_stream_duration_ms": round(
                ((self._finished_at or time.time()) - start) * 1000, 1
            ) if self._finished_at else None,
        }
```

## Comparison

| Approach | Text Streaming | Tool Call Assembly | SSE Output | Cancellation | Checkpointing |
|---|---|---|---|---|---|
| StreamingResponseAssembler | Yes (yields deltas) | Yes (incremental JSON) | No | No | No |
| SSEFormatter | Via assembler | Via assembler | Yes | No | No |
| InterruptibleStreamHandler | Via assembler | Via assembler | No | Yes | No |
| StreamCheckpointWriter | No | No | No | No | Yes |
| StreamLatencyTracker | No | No | No | No | No |

**Best for production**: Yield text deltas immediately from `StreamingResponseAssembler` to the HTTP response — this drives time-to-first-token below 500ms regardless of total generation length. Use `SSEFormatter` for browser clients (native SSE support) and WebSocket transport for mobile clients. Track `time_to_first_token_ms` as a primary UX SLO — target under 600ms for chat interfaces. Use `InterruptibleStreamHandler` to respect user cancellation: when the user dismisses or edits a query mid-generation, stop consuming the stream immediately to avoid wasting tokens on output that will be discarded.
