---
title: "Agent Doesn't Implement Response Streaming with Incremental Parsing"
description: "Agents that wait for the full LLM response before processing it block downstream work that could start from the first token: rendering the first paragraph to the user, detecting a tool call intent in the first 100 tokens, or triggering a prefetch from an entity name mentioned early. Implement response streaming with incremental parsing that processes tokens as they arrive, enabling partial rendering, early tool detection, and first-token-to-UI latency reduction."
date: 2026-04-16
difficulty: advanced
category: performance
slug: agent-doesnt-implement-response-streaming-with-incremental-parsing
tags: [streaming, incremental-parsing, first-token-latency, tool-detection, partial-rendering, time-to-first-byte]
symptoms:
  - "User sees nothing until the full LLM response completes — 4-8 second blank screen"
  - "Tool call detected only after full response received — could have been detected at token 50"
  - "No mechanism to render partial markdown while LLM is still generating"
  - "Streaming is enabled at the API level but tokens are buffered until complete"
  - "Cannot measure time-to-first-token independently from total response latency"
---

## Why This Happens

Most LLM client libraries expose streaming as an async generator of token chunks. However, application code frequently collects all chunks into a buffer and processes the complete response, negating the streaming benefit. Incremental parsing requires maintaining parser state across chunk boundaries — a partially received JSON tool call, an incomplete markdown heading, a sentence that spans two chunks. This statefulness is why buffering feels simpler. The latency cost is significant: for a 500-token response at 50 tokens/second, buffering delays UI rendering by 10 seconds compared to first-token-to-UI which delivers the first sentence in under a second.

## Solution 1: Stream Chunk

```python
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class ChunkType(str, Enum):
    TEXT = "text"
    TOOL_CALL_START = "tool_call_start"
    TOOL_CALL_ARGS = "tool_call_args"
    TOOL_CALL_END = "tool_call_end"
    DONE = "done"
    ERROR = "error"


@dataclass
class StreamChunk:
    chunk_type: ChunkType
    content: str = ""
    tool_name: Optional[str] = None
    tool_call_id: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    received_at: float = field(default_factory=time.time)
    token_index: int = 0
```

## Solution 2: Incremental Text Buffer

```python
from typing import Iterator, List


class IncrementalTextBuffer:
    """
    Accumulates streaming text chunks and emits complete sentences or
    paragraphs for incremental rendering. Does not wait for the full response.
    """

    def __init__(self, flush_on: str = "\n"):
        self._flush_on = flush_on
        self._buffer = ""
        self._flushed: List[str] = []

    def push(self, text: str) -> List[str]:
        """
        Add text to buffer. Returns any complete segments ready to render.
        """
        self._buffer += text
        ready = []
        while self._flush_on in self._buffer:
            idx = self._buffer.index(self._flush_on) + len(self._flush_on)
            ready.append(self._buffer[:idx])
            self._buffer = self._buffer[idx:]
        return ready

    def flush_remaining(self) -> str:
        """Call when stream ends to get any buffered content."""
        remaining = self._buffer
        self._buffer = ""
        return remaining
```

## Solution 3: Incremental Tool Call Parser

```python
import json
import re
from enum import Enum
from typing import Any, Dict, Optional, Tuple


class ToolCallParseState(str, Enum):
    IDLE = "idle"
    IN_TOOL_CALL = "in_tool_call"
    COLLECTING_ARGS = "collecting_args"
    COMPLETE = "complete"


class IncrementalToolCallParser:
    """
    Detects and parses tool call JSON as it streams in, character by character.
    Handles partial JSON by maintaining brace depth. Compatible with both
    Anthropic and OpenAI streaming formats.
    """

    def __init__(self):
        self._state = ToolCallParseState.IDLE
        self._tool_name: Optional[str] = None
        self._args_buffer = ""
        self._brace_depth = 0
        self._completed_calls: list = []

    def feed(self, chunk: str) -> list:
        """Returns list of completed tool calls found in this chunk."""
        results = []
        for char in chunk:
            if self._state == ToolCallParseState.COLLECTING_ARGS:
                self._args_buffer += char
                if char == "{":
                    self._brace_depth += 1
                elif char == "}":
                    self._brace_depth -= 1
                    if self._brace_depth == 0:
                        try:
                            args = json.loads(self._args_buffer)
                            results.append({
                                "tool_name": self._tool_name,
                                "args": args,
                            })
                        except json.JSONDecodeError:
                            pass
                        self._state = ToolCallParseState.IDLE
                        self._args_buffer = ""
                        self._tool_name = None
        return results

    def start_tool_call(self, tool_name: str) -> None:
        self._tool_name = tool_name
        self._state = ToolCallParseState.COLLECTING_ARGS
        self._brace_depth = 0
        self._args_buffer = ""
```

## Solution 4: Streaming Response Processor

```python
import asyncio
import time
from typing import Any, AsyncIterator, Callable, Optional


class StreamingResponseProcessor:
    """
    Processes an LLM streaming response incrementally.
    Dispatches text segments for rendering and tool call detections
    to registered handlers as they arrive, without waiting for completion.
    """

    def __init__(
        self,
        text_handler: Callable[[str], None],
        tool_call_handler: Callable[[dict], None],
        done_handler: Callable[[dict], None],
    ):
        self._text_handler = text_handler
        self._tool_call_handler = tool_call_handler
        self._done_handler = done_handler
        self._text_buffer = IncrementalTextBuffer(flush_on="\n")
        self._tool_parser = IncrementalToolCallParser()
        self._first_token_at: Optional[float] = None
        self._start_at = time.time()
        self._total_tokens = 0

    async def process_stream(self, stream: AsyncIterator[StreamChunk]) -> None:
        async for chunk in stream:
            if chunk.chunk_type == ChunkType.TEXT:
                if self._first_token_at is None:
                    self._first_token_at = time.time()
                self._total_tokens += 1

                # Emit completed text segments
                segments = self._text_buffer.push(chunk.content)
                for seg in segments:
                    self._text_handler(seg)

                # Check for tool calls in the text stream
                tool_calls = self._tool_parser.feed(chunk.content)
                for tc in tool_calls:
                    self._tool_call_handler(tc)

            elif chunk.chunk_type == ChunkType.TOOL_CALL_START:
                if chunk.tool_name:
                    self._tool_parser.start_tool_call(chunk.tool_name)

            elif chunk.chunk_type == ChunkType.DONE:
                remaining = self._text_buffer.flush_remaining()
                if remaining:
                    self._text_handler(remaining)
                self._done_handler(self._stats())
                break

            elif chunk.chunk_type == ChunkType.ERROR:
                break

    def _stats(self) -> dict:
        now = time.time()
        ttft = (self._first_token_at - self._start_at) * 1000 if self._first_token_at else None
        return {
            "time_to_first_token_ms": round(ttft, 2) if ttft else None,
            "total_duration_ms": round((now - self._start_at) * 1000, 2),
            "total_tokens": self._total_tokens,
        }
```

## Solution 5: Streaming Latency Tracker

```python
import time
from collections import deque
from threading import Lock
from typing import Deque, Optional, Tuple


class StreamingLatencyTracker:
    """
    Tracks time-to-first-token and streaming throughput across requests.
    Provides percentiles for SLO monitoring of streaming-specific metrics.
    """

    def __init__(self, window_seconds: float = 3600.0, max_samples: int = 5000):
        self._window = window_seconds
        self._max = max_samples
        self._ttft_samples: Deque[Tuple[float, float]] = deque()  # (ts, ms)
        self._tput_samples: Deque[Tuple[float, float]] = deque()  # (ts, tokens/sec)
        self._lock = Lock()

    def record(self, ttft_ms: float, total_tokens: int, total_ms: float) -> None:
        now = time.time()
        tput = total_tokens / max(total_ms / 1000.0, 0.001)
        with self._lock:
            self._ttft_samples.append((now, ttft_ms))
            self._tput_samples.append((now, tput))
            self._trim(now)

    def _trim(self, now: float) -> None:
        cutoff = now - self._window
        for q in (self._ttft_samples, self._tput_samples):
            while q and q[0][0] < cutoff:
                q.popleft()

    def _percentile(self, samples: Deque, pct: float) -> Optional[float]:
        with self._lock:
            values = sorted(s[1] for s in samples)
        if not values:
            return None
        idx = min(int(len(values) * pct / 100.0), len(values) - 1)
        return round(values[idx], 2)

    def summary(self) -> dict:
        return {
            "ttft_p50_ms": self._percentile(self._ttft_samples, 50),
            "ttft_p95_ms": self._percentile(self._ttft_samples, 95),
            "ttft_p99_ms": self._percentile(self._ttft_samples, 99),
            "throughput_p50_tps": self._percentile(self._tput_samples, 50),
            "throughput_p95_tps": self._percentile(self._tput_samples, 95),
        }
```

## Solution 6: Streaming Performance Dashboard

```python
import time


class StreamingPerformanceDashboard:
    def __init__(self, latency_tracker: StreamingLatencyTracker):
        self._tracker = latency_tracker

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "streaming_latency": self._tracker.summary(),
        }
```

## Comparison

| Approach | Incremental Text | Tool Detection | First-Token Timing | Throughput Tracking | Dashboard |
|---|---|---|---|---|---|
| IncrementalTextBuffer | Yes (newline flush) | No | No | No | No |
| IncrementalToolCallParser | No | Yes (brace depth) | No | No | No |
| StreamingResponseProcessor | Via buffer | Via parser | Yes | No | No |
| StreamingLatencyTracker | No | No | Yes (P50/P95/P99) | Yes | No |
| StreamingPerformanceDashboard | No | No | No | No | Yes |

**Best for production**: Flush text to the UI on sentence boundaries (`. `, `\n`) rather than character by character — this reduces render calls while still delivering incremental updates. Set your P95 TTFT SLO explicitly (e.g., 800ms) and alert when `ttft_p95_ms` exceeds it — TTFT is often more impactful on perceived responsiveness than total response time. For tool-calling models, detect tool call intent in the first 100 tokens (before JSON args are fully streamed) to fire prefetches immediately, reducing tool execution latency by up to the LLM planning time.
