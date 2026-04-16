---
title: "Agent Doesn't Implement Response Streaming with Partial Result Processing"
description: "Agents that wait for complete LLM responses before beginning downstream processing add unnecessary latency: the first tokens of a structured response often contain enough information to start tool preparation, UI updates, or validation before generation finishes. Implement response streaming with partial result processing that extracts actionable information from in-flight tokens and begins downstream work concurrently with generation."
date: 2026-04-16
difficulty: advanced
category: performance
slug: agent-doesnt-implement-response-streaming-with-partial-result-processing
tags: [response-streaming, partial-processing, time-to-first-token, concurrent-generation, streaming-pipeline, latency-reduction]
symptoms:
  - "Agent waits for full LLM response before starting any downstream work"
  - "Tool call arguments are known after the first 20% of generation but tool dispatch waits for 100%"
  - "UI shows no output until the entire response is complete — no streaming to the user"
  - "Structured response fields available early in the stream are not processed until stream end"
  - "Time-to-first-meaningful-output equals full generation time rather than first-token time"
---

## Why This Happens

The simplest LLM integration calls the API in non-streaming mode and processes the complete response. This hides generation latency behind a wall: users see nothing until the model finishes. Streaming mode delivers tokens as they are generated, but most agent frameworks still buffer the entire stream before acting on it. Partial result processing requires identifying which parts of the streaming response can be acted on before the stream closes — tool call arguments that appear early, structured fields in a JSON response that can be parsed incrementally, or the first sentence of a text response that can be rendered immediately.

## Solution 1: Stream Chunk

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class ChunkType(str, Enum):
    TEXT_DELTA = "text_delta"
    TOOL_CALL_START = "tool_call_start"
    TOOL_CALL_DELTA = "tool_call_delta"
    TOOL_CALL_COMPLETE = "tool_call_complete"
    STREAM_END = "stream_end"
    ERROR = "error"


@dataclass
class StreamChunk:
    chunk_type: ChunkType
    index: int = 0
    text: str = ""
    tool_name: Optional[str] = None
    tool_call_id: Optional[str] = None
    tool_args_delta: str = ""
    tool_args_complete: Optional[Dict[str, Any]] = None
    finish_reason: Optional[str] = None
    error: Optional[str] = None
```

## Solution 2: Partial Tool Call Assembler

```python
import json
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class PartialToolCall:
    tool_call_id: str
    tool_name: str
    args_buffer: str = ""
    complete: bool = False
    args: Optional[Dict[str, Any]] = None

    def append_delta(self, delta: str) -> None:
        self.args_buffer += delta

    def try_complete(self) -> bool:
        try:
            self.args = json.loads(self.args_buffer)
            self.complete = True
            return True
        except json.JSONDecodeError:
            return False


class PartialToolCallAssembler:
    """
    Accumulates tool call argument deltas from a streaming response
    and attempts to parse complete JSON as soon as the buffer closes.
    Emits completed tool calls for immediate dispatch.
    """

    def __init__(self):
        self._calls: dict[str, PartialToolCall] = {}
        self._completed: list[PartialToolCall] = []

    def on_chunk(self, chunk: StreamChunk) -> Optional[PartialToolCall]:
        if chunk.chunk_type == ChunkType.TOOL_CALL_START:
            call = PartialToolCall(
                tool_call_id=chunk.tool_call_id or f"call_{chunk.index}",
                tool_name=chunk.tool_name or "",
            )
            self._calls[call.tool_call_id] = call
            return None

        if chunk.chunk_type == ChunkType.TOOL_CALL_DELTA:
            call = self._calls.get(chunk.tool_call_id or "")
            if call:
                call.append_delta(chunk.tool_args_delta)
            return None

        if chunk.chunk_type == ChunkType.TOOL_CALL_COMPLETE:
            call = self._calls.pop(chunk.tool_call_id or "", None)
            if call:
                call.try_complete()
                self._completed.append(call)
                return call
            return None

        return None

    def completed_calls(self) -> list[PartialToolCall]:
        return list(self._completed)
```

## Solution 3: Streaming Text Buffer

```python
import re
from typing import Callable, List, Optional


class StreamingTextBuffer:
    """
    Accumulates streaming text deltas and emits complete sentences
    or paragraphs as they become available, enabling partial rendering
    before the stream closes.
    """

    def __init__(
        self,
        sentence_callback: Optional[Callable[[str], None]] = None,
        min_chunk_chars: int = 80,
    ):
        self._buffer = ""
        self._emitted: List[str] = []
        self._sentence_cb = sentence_callback
        self._min_chunk = min_chunk_chars
        self._sentence_end = re.compile(r"(?<=[.!?])\s+")

    def append(self, delta: str) -> List[str]:
        self._buffer += delta
        emitted_now = []

        if len(self._buffer) < self._min_chunk:
            return emitted_now

        parts = self._sentence_end.split(self._buffer)
        if len(parts) > 1:
            for sentence in parts[:-1]:
                if sentence.strip():
                    self._emitted.append(sentence)
                    emitted_now.append(sentence)
                    if self._sentence_cb:
                        self._sentence_cb(sentence)
            self._buffer = parts[-1]

        return emitted_now

    def flush(self) -> Optional[str]:
        if self._buffer.strip():
            remainder = self._buffer.strip()
            self._emitted.append(remainder)
            self._buffer = ""
            if self._sentence_cb:
                self._sentence_cb(remainder)
            return remainder
        return None

    def all_emitted(self) -> List[str]:
        return list(self._emitted)
```

## Solution 4: Partial Result Processor

```python
import asyncio
import time
from typing import Any, AsyncIterator, Callable, Dict, List, Optional


class PartialResultProcessor:
    """
    Consumes a streaming LLM response and concurrently:
    - Assembles partial tool calls and dispatches them as soon as complete
    - Buffers text deltas and emits sentences to the UI callback
    - Collects full stream for final processing
    """

    def __init__(
        self,
        assembler: PartialToolCallAssembler,
        text_buffer: StreamingTextBuffer,
        tool_dispatch_fn: Optional[Callable[[PartialToolCall], Any]] = None,
    ):
        self._assembler = assembler
        self._text_buffer = text_buffer
        self._dispatch = tool_dispatch_fn
        self._prefetch_tasks: List[asyncio.Task] = []
        self._chunks_processed = 0

    async def process_stream(
        self,
        stream: AsyncIterator[StreamChunk],
    ) -> dict:
        start = time.time()
        all_text = ""

        async for chunk in stream:
            self._chunks_processed += 1

            if chunk.chunk_type == ChunkType.TEXT_DELTA:
                all_text += chunk.text
                self._text_buffer.append(chunk.text)

            elif chunk.chunk_type in (
                ChunkType.TOOL_CALL_START,
                ChunkType.TOOL_CALL_DELTA,
                ChunkType.TOOL_CALL_COMPLETE,
            ):
                completed = self._assembler.on_chunk(chunk)
                if completed and self._dispatch:
                    task = asyncio.ensure_future(self._dispatch(completed))
                    self._prefetch_tasks.append(task)

            elif chunk.chunk_type == ChunkType.STREAM_END:
                break

        self._text_buffer.flush()
        tool_results = {}
        if self._prefetch_tasks:
            results = await asyncio.gather(*self._prefetch_tasks, return_exceptions=True)
            for call, result in zip(self._assembler.completed_calls(), results):
                if not isinstance(result, Exception):
                    tool_results[call.tool_call_id] = result

        return {
            "text": all_text,
            "tool_calls": self._assembler.completed_calls(),
            "prefetched_tool_results": tool_results,
            "chunks_processed": self._chunks_processed,
            "elapsed_ms": round((time.time() - start) * 1000, 2),
        }
```

## Solution 5: Stream Latency Profiler

```python
import time
from typing import Optional


class StreamLatencyProfiler:
    """
    Tracks time-to-first-token, time-to-first-sentence, and
    time-to-first-tool-call from the start of a stream request.
    """

    def __init__(self):
        self._request_start: Optional[float] = None
        self._first_token_at: Optional[float] = None
        self._first_sentence_at: Optional[float] = None
        self._first_tool_call_at: Optional[float] = None
        self._stream_end_at: Optional[float] = None

    def on_request_start(self) -> None:
        self._request_start = time.time()

    def on_first_token(self) -> None:
        if self._first_token_at is None:
            self._first_token_at = time.time()

    def on_first_sentence(self) -> None:
        if self._first_sentence_at is None:
            self._first_sentence_at = time.time()

    def on_first_tool_call(self) -> None:
        if self._first_tool_call_at is None:
            self._first_tool_call_at = time.time()

    def on_stream_end(self) -> None:
        self._stream_end_at = time.time()

    def _delta_ms(self, ts: Optional[float]) -> Optional[float]:
        if ts is None or self._request_start is None:
            return None
        return round((ts - self._request_start) * 1000, 2)

    def report(self) -> dict:
        return {
            "ttft_ms": self._delta_ms(self._first_token_at),
            "ttfs_ms": self._delta_ms(self._first_sentence_at),
            "ttfc_ms": self._delta_ms(self._first_tool_call_at),
            "total_ms": self._delta_ms(self._stream_end_at),
        }
```

## Solution 6: Streaming Pipeline Dashboard

```python
import time


class StreamingPipelineDashboard:
    """
    Combines latency profiles, tool prefetch stats, and text buffer
    output into a single report for streaming pipeline observability.
    """

    def __init__(
        self,
        profiler: StreamLatencyProfiler,
        processor: PartialResultProcessor,
        text_buffer: StreamingTextBuffer,
    ):
        self._profiler = profiler
        self._processor = processor
        self._text_buffer = text_buffer

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "latency_profile": self._profiler.report(),
            "processing": {
                "chunks_processed": self._processor._chunks_processed,
                "prefetch_tasks_launched": len(self._processor._prefetch_tasks),
            },
            "text_output": {
                "sentences_emitted": len(self._text_buffer.all_emitted()),
            },
        }
```

## Comparison

| Approach | Token-Level Processing | Partial Tool Dispatch | Sentence Streaming | Latency Profiling | Dashboard |
|---|---|---|---|---|---|
| PartialToolCallAssembler | No | Yes (on complete) | No | No | No |
| StreamingTextBuffer | Yes (delta) | No | Yes (sentence-level) | No | No |
| PartialResultProcessor | Via both | Via assembler | Via buffer | No | No |
| StreamLatencyProfiler | No | No | No | Yes (TTFT/TTFS/TTFC) | No |
| StreamingPipelineDashboard | No | No | No | No | Yes |

**Best for production**: Dispatch tool calls as soon as `TOOL_CALL_COMPLETE` arrives in the stream — for a 3-tool parallel response, all three dispatches can start concurrently with remaining generation rather than sequentially after it. Track `ttfc_ms` (time-to-first-tool-call) separately from `ttft_ms` — a high `ttfc_ms` relative to `ttft_ms` means the model is taking many tokens to produce the first tool call argument JSON, which may indicate a prompt formatting issue. Set `min_chunk_chars=80` in `StreamingTextBuffer` to avoid emitting single-word fragments to the UI while still achieving sub-sentence streaming latency.
