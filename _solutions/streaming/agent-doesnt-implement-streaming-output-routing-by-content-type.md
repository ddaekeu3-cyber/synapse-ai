---
title: "Agent Doesn't Implement Streaming Output Routing by Content Type"
description: "AI agents stream all output to a single handler regardless of content type — mixing code blocks, JSON payloads, prose, and tool results into one undifferentiated byte stream. Downstream consumers can't efficiently parse, render, or act on typed content without routing."
problem_description: |
  When an agent streams a response containing multiple content types — markdown prose, fenced code blocks, inline JSON, tool call results, structured tables — a single catch-all handler receives everything mixed together. Frontend clients must re-parse the entire stream to split code from prose. Logging pipelines can't index code separately from natural language. Security filters can't apply type-specific scanning. Without content-type routing in the streaming layer, every consumer must independently implement the same fragile split logic.
category: streaming
difficulty: intermediate
tags: [streaming, content-routing, content-type, parser, sse]
---

## Solution 1: Regex-Based Fence Detection Router

Detect markdown fenced code blocks in real time as tokens arrive and route each segment to a type-specific handler without buffering the full response.

```python
import asyncio
import re
from anthropic import AsyncAnthropic
from dataclasses import dataclass, field
from typing import Callable, AsyncIterator


@dataclass
class ContentSegment:
    content_type: str  # "prose" | "code" | "json" | "table"
    language: str | None
    text: str


ContentHandler = Callable[[ContentSegment], None]


class StreamingContentRouter:
    FENCE_OPEN = re.compile(r'^```(\w*)$')
    FENCE_CLOSE = re.compile(r'^```$')
    TABLE_ROW = re.compile(r'^\|.*\|$')
    JSON_START = re.compile(r'^\s*[{\[]')

    def __init__(self):
        self._handlers: dict[str, list[ContentHandler]] = {
            "prose": [],
            "code": [],
            "json": [],
            "table": [],
        }

    def on(self, content_type: str, handler: ContentHandler):
        self._handlers.setdefault(content_type, []).append(handler)
        return self

    def _emit(self, segment: ContentSegment):
        for handler in self._handlers.get(segment.content_type, []):
            handler(segment)
        for handler in self._handlers.get("*", []):
            handler(segment)

    async def route_stream(self, stream: AsyncIterator[str]):
        buffer = ""
        in_fence = False
        fence_language: str | None = None
        fence_buffer: list[str] = []

        async for token in stream:
            buffer += token
            lines = buffer.split('\n')
            buffer = lines.pop()  # Keep incomplete last line

            for line in lines:
                if not in_fence:
                    m = self.FENCE_OPEN.match(line.strip())
                    if m:
                        in_fence = True
                        lang = m.group(1) or None
                        fence_language = lang
                        fence_buffer = []
                    elif self.TABLE_ROW.match(line):
                        self._emit(ContentSegment("table", None, line + '\n'))
                    else:
                        self._emit(ContentSegment("prose", None, line + '\n'))
                else:
                    if self.FENCE_CLOSE.match(line.strip()):
                        code_text = '\n'.join(fence_buffer)
                        ct = "json" if fence_language in ("json", "jsonc") else "code"
                        self._emit(ContentSegment(ct, fence_language, code_text))
                        in_fence = False
                        fence_language = None
                        fence_buffer = []
                    else:
                        fence_buffer.append(line)

        # Flush remaining buffer
        if buffer:
            if in_fence:
                fence_buffer.append(buffer)
                self._emit(ContentSegment("code", fence_language, '\n'.join(fence_buffer)))
            else:
                self._emit(ContentSegment("prose", None, buffer))


# Usage
async def main():
    client = AsyncAnthropic()
    router = StreamingContentRouter()

    prose_parts: list[str] = []
    code_parts: list[str] = []

    router.on("prose", lambda s: prose_parts.append(s.text))
    router.on("code", lambda s: (
        print(f"[CODE:{s.language}] {len(s.text)} chars"),
        code_parts.append(s.text)
    ))
    router.on("json", lambda s: print(f"[JSON] {s.text[:80]}..."))

    async def token_stream():
        async with client.messages.stream(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            messages=[{"role": "user", "content":
                "Show me a Python hello world and explain it. Include a JSON config example."}],
        ) as stream:
            async for token in stream.text_stream:
                yield token

    await router.route_stream(token_stream())
    print(f"Prose segments: {len(prose_parts)}, Code segments: {len(code_parts)}")

asyncio.run(main())
```

## Solution 2: State-Machine Token Classifier

Use a formal state machine with explicit transitions to classify every streamed token — enabling precise routing with context-aware type detection beyond simple regex matching.

```python
import asyncio
from anthropic import AsyncAnthropic
from enum import Enum, auto
from dataclasses import dataclass, field
from typing import AsyncIterator, Callable


class StreamState(Enum):
    PROSE = auto()
    CODE_FENCE_OPENING = auto()
    CODE = auto()
    CODE_FENCE_CLOSING = auto()
    JSON_INLINE = auto()
    TABLE = auto()


@dataclass
class RouterEvent:
    event_type: str  # "prose_token" | "code_start" | "code_token" | "code_end" | "json_token" | "table_row"
    payload: str
    language: str | None = None


EventCallback = Callable[[RouterEvent], None]


class StateMachineStreamRouter:
    def __init__(self):
        self._callbacks: list[EventCallback] = []
        self._state = StreamState.PROSE
        self._line_buffer = ""
        self._fence_lang: str | None = None
        self._json_depth = 0

    def subscribe(self, callback: EventCallback):
        self._callbacks.append(callback)

    def _fire(self, event: RouterEvent):
        for cb in self._callbacks:
            cb(event)

    def _process_line(self, line: str):
        stripped = line.strip()

        if self._state == StreamState.PROSE:
            if stripped.startswith("```"):
                lang = stripped[3:].strip() or None
                self._fence_lang = lang
                self._state = StreamState.CODE
                self._fire(RouterEvent("code_start", "", lang))
            elif stripped.startswith("|") and stripped.endswith("|"):
                self._state = StreamState.TABLE
                self._fire(RouterEvent("table_row", line, None))
                self._state = StreamState.PROSE
            elif stripped.startswith("{") or stripped.startswith("["):
                self._json_depth = stripped.count("{") + stripped.count("[") - \
                                   stripped.count("}") - stripped.count("]")
                self._state = StreamState.JSON_INLINE if self._json_depth > 0 else StreamState.PROSE
                self._fire(RouterEvent("json_token", line, None))
            else:
                self._fire(RouterEvent("prose_token", line, None))

        elif self._state == StreamState.CODE:
            if stripped == "```":
                self._state = StreamState.PROSE
                self._fire(RouterEvent("code_end", "", self._fence_lang))
                self._fence_lang = None
            else:
                self._fire(RouterEvent("code_token", line, self._fence_lang))

        elif self._state == StreamState.JSON_INLINE:
            self._json_depth += line.count("{") + line.count("[")
            self._json_depth -= line.count("}") + line.count("]")
            self._fire(RouterEvent("json_token", line, None))
            if self._json_depth <= 0:
                self._state = StreamState.PROSE

    def feed(self, token: str):
        self._line_buffer += token
        while '\n' in self._line_buffer:
            line, self._line_buffer = self._line_buffer.split('\n', 1)
            self._process_line(line)

    def flush(self):
        if self._line_buffer:
            self._process_line(self._line_buffer)
            self._line_buffer = ""


# Usage
async def main():
    client = AsyncAnthropic()
    router = StateMachineStreamRouter()

    log: list[tuple[str, str]] = []

    def on_event(event: RouterEvent):
        log.append((event.event_type, event.payload[:40]))
        if event.event_type == "code_start":
            print(f">>> Code block starting (lang={event.language})")
        elif event.event_type == "code_end":
            print(f"<<< Code block ended")

    router.subscribe(on_event)

    async with client.messages.stream(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        messages=[{"role": "user", "content":
            "Give me a Python snippet and then explain what it does."}],
    ) as stream:
        async for token in stream.text_stream:
            router.feed(token)

    router.flush()
    print(f"Total events: {len(log)}")
    event_counts = {}
    for etype, _ in log:
        event_counts[etype] = event_counts.get(etype, 0) + 1
    print(f"Event breakdown: {event_counts}")

asyncio.run(main())
```

## Solution 3: Multi-Sink Fan-Out with Type Filters

Route streamed segments to multiple typed sinks simultaneously — a code sink for syntax highlighting, a prose sink for TTS, a JSON sink for structured extraction — each receiving only its relevant content.

```python
import asyncio
from anthropic import AsyncAnthropic
from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class StreamChunk:
    content_type: str
    text: str
    language: str | None = None
    metadata: dict | None = None


class StreamSink(ABC):
    @abstractmethod
    async def accept(self, chunk: StreamChunk) -> None: ...

    @abstractmethod
    def content_types(self) -> set[str]: ...


class CodeSyntaxHighlightSink(StreamSink):
    def __init__(self):
        self.code_blocks: list[dict] = []

    def content_types(self) -> set[str]:
        return {"code"}

    async def accept(self, chunk: StreamChunk):
        self.code_blocks.append({
            "language": chunk.language,
            "code": chunk.text,
            "highlighted": f"<pre><code class='{chunk.language}'>{chunk.text}</code></pre>",
        })
        print(f"[CodeSink] Highlighted {len(chunk.text)} chars of {chunk.language}")


class TTSProseSink(StreamSink):
    def __init__(self):
        self.prose_buffer = ""

    def content_types(self) -> set[str]:
        return {"prose"}

    async def accept(self, chunk: StreamChunk):
        # Strip markdown symbols for TTS
        clean = chunk.text.replace("**", "").replace("*", "").replace("#", "").strip()
        if clean:
            self.prose_buffer += clean + " "
            print(f"[TTSSink] TTS: {clean[:60]}")


class JSONExtractionSink(StreamSink):
    def __init__(self):
        self.json_payloads: list[str] = []

    def content_types(self) -> set[str]:
        return {"json"}

    async def accept(self, chunk: StreamChunk):
        self.json_payloads.append(chunk.text)
        print(f"[JSONSink] Extracted JSON: {chunk.text[:80]}")


class FanOutStreamRouter:
    def __init__(self, sinks: list[StreamSink]):
        self._sinks = sinks
        self._type_map: dict[str, list[StreamSink]] = {}
        for sink in sinks:
            for ct in sink.content_types():
                self._type_map.setdefault(ct, []).append(sink)

    async def route(self, chunk: StreamChunk):
        sinks = self._type_map.get(chunk.content_type, [])
        await asyncio.gather(*[sink.accept(chunk) for sink in sinks])

    async def process_stream(self, client: AsyncAnthropic, prompt: str):
        in_code = False
        fence_lang = None
        code_buffer: list[str] = []
        line_buffer = ""

        async def flush_line(line: str):
            nonlocal in_code, fence_lang, code_buffer
            stripped = line.strip()

            if not in_code:
                if stripped.startswith("```"):
                    fence_lang = stripped[3:].strip() or "text"
                    in_code = True
                    code_buffer = []
                else:
                    ct = "json" if stripped.startswith("{") or stripped.startswith("[") else "prose"
                    await self.route(StreamChunk(ct, line, None))
            else:
                if stripped == "```":
                    in_code = False
                    ct = "json" if fence_lang in ("json", "jsonc") else "code"
                    await self.route(StreamChunk(ct, '\n'.join(code_buffer), fence_lang))
                    code_buffer = []
                    fence_lang = None
                else:
                    code_buffer.append(line)

        async with client.messages.stream(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            async for token in stream.text_stream:
                line_buffer += token
                while '\n' in line_buffer:
                    line, line_buffer = line_buffer.split('\n', 1)
                    await flush_line(line)

        if line_buffer:
            await flush_line(line_buffer)


# Usage
async def main():
    client = AsyncAnthropic()
    router = FanOutStreamRouter(sinks=[
        CodeSyntaxHighlightSink(),
        TTSProseSink(),
        JSONExtractionSink(),
    ])

    await router.process_stream(
        client,
        "Explain recursion with a Python example, then give a JSON config for a recursive task runner.",
    )

asyncio.run(main())
```

## Solution 4: WebSocket Content-Type Framing

Wrap streamed content segments in typed frames before forwarding over WebSocket — enabling browser clients to handle code, prose, and structured data with dedicated renderers without client-side parsing.

```python
import asyncio
import json
from anthropic import AsyncAnthropic
from dataclasses import dataclass, asdict
from enum import IntEnum


class FrameType(IntEnum):
    PROSE = 1
    CODE_START = 2
    CODE_TOKEN = 3
    CODE_END = 4
    JSON_PAYLOAD = 5
    TABLE_ROW = 6
    STREAM_END = 99


@dataclass
class TypedFrame:
    frame_type: int
    sequence: int
    payload: str
    language: str | None = None

    def to_json(self) -> str:
        return json.dumps({
            "t": self.frame_type,
            "s": self.sequence,
            "p": self.payload,
            "l": self.language,
        })


class WebSocketFramingRouter:
    def __init__(self, websocket_send):
        """websocket_send: async callable that takes a JSON string."""
        self._send = websocket_send
        self._seq = 0
        self._in_code = False
        self._fence_lang: str | None = None
        self._line_buffer = ""

    async def _emit(self, frame_type: FrameType, payload: str, language: str | None = None):
        frame = TypedFrame(int(frame_type), self._seq, payload, language)
        await self._send(frame.to_json())
        self._seq += 1

    async def _process_line(self, line: str):
        stripped = line.strip()

        if not self._in_code:
            if stripped.startswith("```"):
                lang = stripped[3:].strip() or "text"
                self._fence_lang = lang
                self._in_code = True
                await self._emit(FrameType.CODE_START, "", lang)
            elif stripped.startswith("|") and stripped.endswith("|"):
                await self._emit(FrameType.TABLE_ROW, line)
            elif stripped.startswith("{") or stripped.startswith("["):
                await self._emit(FrameType.JSON_PAYLOAD, line)
            else:
                await self._emit(FrameType.PROSE, line)
        else:
            if stripped == "```":
                self._in_code = False
                await self._emit(FrameType.CODE_END, "", self._fence_lang)
                self._fence_lang = None
            else:
                await self._emit(FrameType.CODE_TOKEN, line, self._fence_lang)

    async def stream_to_websocket(self, client: AsyncAnthropic, prompt: str):
        async with client.messages.stream(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            async for token in stream.text_stream:
                self._line_buffer += token
                while '\n' in self._line_buffer:
                    line, self._line_buffer = self._line_buffer.split('\n', 1)
                    await self._process_line(line)

        if self._line_buffer:
            await self._process_line(self._line_buffer)

        await self._emit(FrameType.STREAM_END, "")


# Usage (simulated WebSocket)
async def main():
    client = AsyncAnthropic()
    received_frames: list[dict] = []

    async def mock_ws_send(json_str: str):
        frame = json.loads(json_str)
        received_frames.append(frame)

    router = WebSocketFramingRouter(websocket_send=mock_ws_send)
    await router.stream_to_websocket(
        client,
        "Show me a Python sort example and explain the algorithm.",
    )

    frame_type_names = {1: "PROSE", 2: "CODE_START", 3: "CODE_TOKEN", 4: "CODE_END", 99: "END"}
    for frame in received_frames:
        name = frame_type_names.get(frame["t"], str(frame["t"]))
        print(f"Frame {frame['s']:03d} [{name}] lang={frame['l']} payload={frame['p'][:40]!r}")

asyncio.run(main())
```

## Solution 5: Pluggable Content-Type Pipeline with Transform Steps

Apply per-type transform steps (redaction, validation, enrichment) before routing — enabling content-type-specific middleware without coupling transforms to the stream source.

```python
import asyncio
import re
from anthropic import AsyncAnthropic
from dataclasses import dataclass
from typing import Callable, Awaitable


@dataclass
class TypedChunk:
    content_type: str
    text: str
    language: str | None = None


Transform = Callable[[TypedChunk], Awaitable[TypedChunk | None]]


class ContentTypePipeline:
    def __init__(self):
        self._transforms: dict[str, list[Transform]] = {}
        self._sinks: dict[str, list[Callable[[TypedChunk], Awaitable[None]]]] = {}

    def add_transform(self, content_type: str, transform: Transform):
        self._transforms.setdefault(content_type, []).append(transform)
        return self

    def add_sink(self, content_type: str, sink: Callable[[TypedChunk], Awaitable[None]]):
        self._sinks.setdefault(content_type, []).append(sink)
        return self

    async def process(self, chunk: TypedChunk):
        current = chunk
        for transform in self._transforms.get(chunk.content_type, []):
            result = await transform(current)
            if result is None:
                return  # Chunk filtered out
            current = result

        for sink in self._sinks.get(current.content_type, []):
            await sink(current)


def secret_redaction_transform(pattern: str) -> Transform:
    compiled = re.compile(pattern)

    async def transform(chunk: TypedChunk) -> TypedChunk | None:
        redacted = compiled.sub("[REDACTED]", chunk.text)
        return TypedChunk(chunk.content_type, redacted, chunk.language)

    return transform


def max_length_filter(max_chars: int) -> Transform:
    async def transform(chunk: TypedChunk) -> TypedChunk | None:
        if len(chunk.text) > max_chars:
            return None  # Drop oversized chunks
        return chunk
    return transform


def language_allowlist_filter(allowed: set[str]) -> Transform:
    async def transform(chunk: TypedChunk) -> TypedChunk | None:
        if chunk.content_type == "code" and chunk.language not in allowed:
            return None
        return chunk
    return transform


# Usage
async def main():
    client = AsyncAnthropic()
    pipeline = ContentTypePipeline()

    # Code: only allow python and javascript, redact API keys
    pipeline.add_transform("code", secret_redaction_transform(r'sk-[a-zA-Z0-9]{32,}'))
    pipeline.add_transform("code", language_allowlist_filter({"python", "javascript", "js", None}))
    pipeline.add_sink("code", lambda c: asyncio.coroutine(
        lambda: print(f"[CODE:{c.language}] {c.text[:80]}")
    )())

    # Prose: filter very long chunks
    pipeline.add_transform("prose", max_length_filter(2000))
    pipeline.add_sink("prose", lambda c: asyncio.coroutine(
        lambda: print(f"[PROSE] {c.text[:60]}")
    )())

    in_code = False
    fence_lang = None
    code_buf: list[str] = []
    line_buf = ""

    async def flush_line(line: str):
        nonlocal in_code, fence_lang, code_buf
        s = line.strip()
        if not in_code:
            if s.startswith("```"):
                fence_lang = s[3:].strip() or None
                in_code = True
                code_buf = []
            else:
                await pipeline.process(TypedChunk("prose", line))
        else:
            if s == "```":
                in_code = False
                await pipeline.process(TypedChunk("code", '\n'.join(code_buf), fence_lang))
                code_buf = []
            else:
                code_buf.append(line)

    async with client.messages.stream(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        messages=[{"role": "user", "content":
            "Show Python and SQL examples for data processing."}],
    ) as stream:
        async for token in stream.text_stream:
            line_buf += token
            while '\n' in line_buf:
                line, line_buf = line_buf.split('\n', 1)
                await flush_line(line)

    if line_buf:
        await flush_line(line_buf)

asyncio.run(main())
```

## Solution 6: Content-Type Telemetry and Quality Gating

Instrument streaming content routing with per-type metrics and apply quality gates — blocking low-quality segments (empty code blocks, garbled JSON) before they reach downstream consumers.

```python
import asyncio
import json
import time
from anthropic import AsyncAnthropic
from dataclasses import dataclass, field


@dataclass
class RouteMetrics:
    type_counts: dict[str, int] = field(default_factory=dict)
    type_bytes: dict[str, int] = field(default_factory=dict)
    filtered_counts: dict[str, int] = field(default_factory=dict)
    latency_first_token: float | None = None
    stream_start: float = field(default_factory=time.time)

    def record(self, content_type: str, text: str, filtered: bool = False):
        if filtered:
            self.filtered_counts[content_type] = self.filtered_counts.get(content_type, 0) + 1
        else:
            self.type_counts[content_type] = self.type_counts.get(content_type, 0) + 1
            self.type_bytes[content_type] = self.type_bytes.get(content_type, 0) + len(text)

    def report(self) -> dict:
        return {
            "duration_s": round(time.time() - self.stream_start, 3),
            "type_counts": self.type_counts,
            "type_bytes": self.type_bytes,
            "filtered_counts": self.filtered_counts,
            "first_token_ms": round(self.latency_first_token * 1000, 1)
            if self.latency_first_token else None,
        }


def quality_gate_code(text: str, language: str | None) -> tuple[bool, str]:
    """Returns (passes, reason)."""
    if not text.strip():
        return False, "empty_code_block"
    if len(text.strip()) < 10:
        return False, "trivially_short"
    return True, "ok"


def quality_gate_json(text: str) -> tuple[bool, str]:
    try:
        json.loads(text)
        return True, "ok"
    except json.JSONDecodeError as e:
        return False, f"invalid_json:{e}"


class InstrumentedStreamRouter:
    def __init__(self, code_sink, prose_sink, json_sink):
        self.code_sink = code_sink
        self.prose_sink = prose_sink
        self.json_sink = json_sink
        self.metrics = RouteMetrics()

    async def process_stream(self, client: AsyncAnthropic, prompt: str):
        in_code = False
        fence_lang = None
        code_buf: list[str] = []
        line_buf = ""
        first_token = True

        async def route(content_type: str, text: str, language: str | None = None):
            if content_type == "code":
                passes, reason = quality_gate_code(text, language)
                if not passes:
                    self.metrics.record(content_type, text, filtered=True)
                    print(f"[GATE] Code filtered: {reason}")
                    return
                self.metrics.record(content_type, text)
                await self.code_sink(text, language)

            elif content_type == "json":
                passes, reason = quality_gate_json(text)
                if not passes:
                    self.metrics.record(content_type, text, filtered=True)
                    return
                self.metrics.record(content_type, text)
                await self.json_sink(text)

            else:
                self.metrics.record(content_type, text)
                await self.prose_sink(text)

        async def flush_line(line: str):
            nonlocal in_code, fence_lang, code_buf
            s = line.strip()
            if not in_code:
                if s.startswith("```"):
                    fence_lang = s[3:].strip() or None
                    in_code = True
                    code_buf = []
                elif s.startswith("{") or s.startswith("["):
                    await route("json", s)
                else:
                    await route("prose", line)
            else:
                if s == "```":
                    in_code = False
                    await route("code", '\n'.join(code_buf), fence_lang)
                    code_buf = []
                else:
                    code_buf.append(line)

        async with client.messages.stream(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            async for token in stream.text_stream:
                if first_token:
                    self.metrics.latency_first_token = time.time() - self.metrics.stream_start
                    first_token = False
                line_buf += token
                while '\n' in line_buf:
                    line, line_buf = line_buf.split('\n', 1)
                    await flush_line(line)

        if line_buf:
            await flush_line(line_buf)

        return self.metrics.report()


# Usage
async def main():
    client = AsyncAnthropic()

    async def code_sink(text: str, lang: str | None):
        print(f"[CODE:{lang}] {len(text)} chars")

    async def prose_sink(text: str):
        print(f"[PROSE] {text[:60]}")

    async def json_sink(text: str):
        print(f"[JSON] {text[:60]}")

    router = InstrumentedStreamRouter(code_sink, prose_sink, json_sink)
    report = await router.process_stream(
        client,
        "Explain Python decorators with an example and give a JSON config.",
    )
    print(f"\nMetrics: {report}")

asyncio.run(main())
```

## Comparison

| Approach | Latency Impact | Parsing Accuracy | Multi-Sink Support | Middleware Support | Best For |
|---|---|---|---|---|---|
| Regex Fence Detection | Minimal | High | No | No | Simple prose/code split |
| State Machine Classifier | Minimal | Very High | No | No | Complex mixed-type streams |
| Multi-Sink Fan-Out | Minimal | High | Yes | No | Multiple parallel consumers |
| WebSocket Framing | Minimal | High | Implicit | No | Browser streaming clients |
| Pluggable Pipeline | Low | High | Yes | Yes | Enterprise with security rules |
| Telemetry + Quality Gate | Low | High | Yes | Yes | Production with observability |
