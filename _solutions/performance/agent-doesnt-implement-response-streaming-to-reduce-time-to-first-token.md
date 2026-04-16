---
title: "Agent Doesn't Implement Response Streaming to Reduce Time to First Token"
description: "Agents that buffer the complete LLM response before returning it to the user introduce unnecessary latency equal to the full generation time — the user sees nothing for several seconds, then the full response appears at once. Implement response streaming that passes LLM output tokens to the caller as they are generated, reducing perceived latency from full generation time to time-to-first-token."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-response-streaming-to-reduce-time-to-first-token
tags: [streaming, time-to-first-token, ttft, server-sent-events, async-generator, latency-reduction]
symptoms:
  - "Users wait several seconds with no output before the full response appears"
  - "Time-to-first-token equals total generation time — no incremental delivery"
  - "Long responses feel slower than short responses by a larger margin than actual generation time"
  - "No server-sent event or WebSocket streaming from the agent to the frontend"
  - "Streaming is disabled at the LLM client layer even though the API supports it"
---

## Why This Happens

Most LLM APIs support streaming token-by-token delivery. Agents that collect all tokens into a buffer before returning them force users to wait for the last token before seeing the first. Streaming requires threading the async generator from the LLM client through the agent's response path to the caller — this means the agent cannot post-process the full response before delivery, so any processing (safety checks, tool call parsing) must happen either incrementally or on a buffered copy while streaming proceeds.

## Solution 1: Stream Chunk Model

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class StreamChunkKind(str, Enum):
    TEXT = "text"
    TOOL_CALL_START = "tool_call_start"
    TOOL_CALL_DELTA = "tool_call_delta"
    TOOL_CALL_END = "tool_call_end"
    DONE = "done"
    ERROR = "error"


@dataclass
class StreamChunk:
    kind: StreamChunkKind
    delta: str = ""                  # incremental text content
    tool_name: Optional[str] = None
    tool_call_id: Optional[str] = None
    tool_args_delta: str = ""
    finish_reason: Optional[str] = None
    error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_sse(self) -> str:
        """Format as Server-Sent Event line."""
        import json
        data = {
            "kind": self.kind.value,
            "delta": self.delta,
        }
        if self.tool_name:
            data["tool_name"] = self.tool_name
        if self.finish_reason:
            data["finish_reason"] = self.finish_reason
        if self.error:
            data["error"] = self.error
        return f"data: {json.dumps(data)}\n\n"
```

## Solution 2: Streaming LLM Client Adapter

```python
from typing import Any, AsyncGenerator, Callable, Dict, List, Optional


class StreamingLLMClientAdapter:
    """
    Adapts an LLM client's streaming API into an async generator
    of StreamChunk objects. Handles text deltas and tool call events.
    """

    def __init__(self, llm_client: Any):
        self._client = llm_client

    async def stream(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict]] = None,
        **kwargs: Any,
    ) -> AsyncGenerator[StreamChunk, None]:
        try:
            async for raw_chunk in self._client.stream(
                messages=messages,
                tools=tools or [],
                **kwargs,
            ):
                chunk = self._parse_chunk(raw_chunk)
                if chunk:
                    yield chunk
            yield StreamChunk(kind=StreamChunkKind.DONE, finish_reason="stop")
        except Exception as exc:
            yield StreamChunk(kind=StreamChunkKind.ERROR, error=str(exc))

    def _parse_chunk(self, raw: Any) -> Optional[StreamChunk]:
        # Adapt based on actual client response format
        if hasattr(raw, "choices") and raw.choices:
            choice = raw.choices[0]
            delta = getattr(choice, "delta", None)
            if delta:
                content = getattr(delta, "content", None) or ""
                if content:
                    return StreamChunk(kind=StreamChunkKind.TEXT, delta=content)
        return None
```

## Solution 3: Stream Buffer Accumulator

```python
from typing import AsyncGenerator, Optional, Tuple


class StreamBufferAccumulator:
    """
    Passes stream chunks through while simultaneously accumulating
    the full text. Allows post-stream access to the complete response
    without blocking streaming delivery.
    """

    def __init__(self):
        self._text_buffer: list = []
        self._done = False
        self._error: Optional[str] = None

    async def passthrough(
        self,
        source: AsyncGenerator[StreamChunk, None],
    ) -> AsyncGenerator[StreamChunk, None]:
        async for chunk in source:
            if chunk.kind == StreamChunkKind.TEXT:
                self._text_buffer.append(chunk.delta)
            elif chunk.kind == StreamChunkKind.DONE:
                self._done = True
            elif chunk.kind == StreamChunkKind.ERROR:
                self._error = chunk.error
            yield chunk

    def full_text(self) -> str:
        return "".join(self._text_buffer)

    def is_complete(self) -> bool:
        return self._done

    def has_error(self) -> bool:
        return self._error is not None
```

## Solution 4: Streaming Safety Filter

```python
import re
from typing import AsyncGenerator, List


class StreamingSafetyFilter:
    """
    Scans accumulated text for safety violations as chunks arrive.
    Emits an error chunk and stops the stream if a violation is found.
    Uses a rolling window to catch violations that span chunk boundaries.
    """

    def __init__(self, violation_patterns: List[str], window_chars: int = 200):
        self._patterns = [re.compile(p, re.IGNORECASE) for p in violation_patterns]
        self._window = window_chars
        self._buffer = ""
        self._violated = False

    async def filter(
        self,
        source: AsyncGenerator[StreamChunk, None],
    ) -> AsyncGenerator[StreamChunk, None]:
        async for chunk in source:
            if self._violated:
                return

            if chunk.kind == StreamChunkKind.TEXT:
                self._buffer += chunk.delta
                # Keep only the tail for rolling window check
                if len(self._buffer) > self._window * 2:
                    self._buffer = self._buffer[-self._window:]

                for pattern in self._patterns:
                    if pattern.search(self._buffer):
                        self._violated = True
                        yield StreamChunk(
                            kind=StreamChunkKind.ERROR,
                            error="content_policy_violation",
                        )
                        return

            yield chunk
```

## Solution 5: Stream Latency Tracker

```python
import time
from typing import AsyncGenerator, Optional


class StreamLatencyTracker:
    """
    Measures time-to-first-token (TTFT) and total generation time
    by observing the stream without modifying it.
    """

    def __init__(self):
        self._start: Optional[float] = None
        self._first_token_time: Optional[float] = None
        self._end_time: Optional[float] = None
        self._chunk_count = 0

    def record_start(self) -> None:
        self._start = time.time()

    async def observe(
        self,
        source: AsyncGenerator[StreamChunk, None],
    ) -> AsyncGenerator[StreamChunk, None]:
        async for chunk in source:
            self._chunk_count += 1
            if chunk.kind == StreamChunkKind.TEXT and self._first_token_time is None:
                self._first_token_time = time.time()
            if chunk.kind in (StreamChunkKind.DONE, StreamChunkKind.ERROR):
                self._end_time = time.time()
            yield chunk

    def ttft_ms(self) -> Optional[float]:
        if self._start and self._first_token_time:
            return round((self._first_token_time - self._start) * 1000, 2)
        return None

    def total_ms(self) -> Optional[float]:
        if self._start and self._end_time:
            return round((self._end_time - self._start) * 1000, 2)
        return None

    def stats(self) -> dict:
        return {
            "ttft_ms": self.ttft_ms(),
            "total_ms": self.total_ms(),
            "chunk_count": self._chunk_count,
        }
```

## Solution 6: Streaming Response Pipeline

```python
from typing import Any, AsyncGenerator, Dict, List, Optional


class StreamingResponsePipeline:
    """
    Composes the full streaming path: LLM adapter → safety filter →
    accumulator → latency tracker → SSE output.
    """

    def __init__(
        self,
        llm_adapter: StreamingLLMClientAdapter,
        safety_filter: Optional[StreamingSafetyFilter] = None,
    ):
        self._llm = llm_adapter
        self._safety = safety_filter

    async def stream_response(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict]] = None,
    ) -> dict:
        tracker = StreamLatencyTracker()
        accumulator = StreamBufferAccumulator()
        tracker.record_start()

        raw_stream = self._llm.stream(messages, tools=tools)

        if self._safety:
            filtered = self._safety.filter(raw_stream)
        else:
            filtered = raw_stream

        accumulated = accumulator.passthrough(filtered)
        observed = tracker.observe(accumulated)

        return {
            "stream": observed,          # async generator for the caller
            "accumulator": accumulator,  # access full text after stream ends
            "tracker": tracker,          # access latency stats after stream ends
        }
```

## Comparison

| Approach | Token Streaming | Full Text Buffer | Safety Scanning | TTFT Measurement | SSE Output |
|---|---|---|---|---|---|
| StreamingLLMClientAdapter | Yes | No | No | No | No |
| StreamBufferAccumulator | Yes (passthrough) | Yes | No | No | No |
| StreamingSafetyFilter | Yes (passthrough) | No | Yes (rolling window) | No | No |
| StreamLatencyTracker | Yes (passthrough) | No | No | Yes | No |
| StreamingResponsePipeline | Yes | Via accumulator | Via filter | Via tracker | Via StreamChunk.to_sse() |

**Best for production**: Target TTFT < 500 ms as your primary streaming SLO — users perceive responses as responsive when they see the first characters within half a second. Use `StreamingSafetyFilter` with a rolling window rather than waiting for the full response — this prevents violations from rendering in the UI even briefly. Always run `StreamBufferAccumulator` in parallel so the full text is available for logging and audit after the stream ends without a second LLM call. Expose `tracker.ttft_ms()` as a separate metric from total latency — TTFT and total latency respond differently to optimizations and should be tracked and alerted independently.
