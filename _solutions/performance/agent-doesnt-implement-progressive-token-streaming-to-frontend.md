---
title: "Agent Doesn't Implement Progressive Token Streaming to Frontend"
description: "Six solutions for streaming LLM tokens progressively to frontend clients using SSE, WebSockets, and chunked HTTP, reducing perceived latency dramatically."
difficulty: intermediate
category: performance
tags: [streaming, sse, websocket, frontend, latency, user-experience]
---

# Agent Doesn't Implement Progressive Token Streaming to Frontend

Without streaming, users stare at a spinner for 5-15 seconds then receive a wall of text. With streaming, users see tokens appear immediately — perceived latency drops from seconds to milliseconds. These six solutions cover SSE, WebSocket, chunked HTTP, sentence-level chunking, and markdown-aware progressive rendering.

## Solution 1: Server-Sent Events (SSE) with FastAPI

Stream tokens from the Anthropic API directly to the browser using SSE — the simplest approach for one-way streaming.

```python
import asyncio
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from anthropic import AsyncAnthropic

app = FastAPI()
client = AsyncAnthropic()


async def token_stream(message: str, model: str = "claude-haiku-4-5-20251001"):
    """Async generator that yields SSE-formatted token events."""
    async with client.messages.stream(
        model=model,
        max_tokens=2048,
        messages=[{"role": "user", "content": message}],
    ) as stream:
        async for text in stream.text_stream:
            # SSE format: "data: <payload>\n\n"
            yield f"data: {text}\n\n"
        # Signal completion
        yield "data: [DONE]\n\n"


@app.post("/chat/stream")
async def stream_chat(request: Request):
    body = await request.json()
    message = body.get("message", "")

    return StreamingResponse(
        token_stream(message),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # Disable Nginx buffering
            "Connection": "keep-alive",
        },
    )


# JavaScript client (browser):
# const source = new EventSource('/chat/stream?message=...');
# source.onmessage = (e) => {
#   if (e.data === '[DONE]') { source.close(); return; }
#   document.getElementById('output').textContent += e.data;
# };

# Full SSE + metadata events version:
async def rich_token_stream(message: str):
    """SSE stream with event types for tokens, usage, and errors."""
    import json
    try:
        async with client.messages.stream(
            model="claude-haiku-4-5-20251001",
            max_tokens=2048,
            messages=[{"role": "user", "content": message}],
        ) as stream:
            async for text in stream.text_stream:
                data = json.dumps({"token": text})
                yield f"event: token\ndata: {data}\n\n"

            # Send final usage stats
            final = await stream.get_final_message()
            usage_data = json.dumps({
                "input_tokens": final.usage.input_tokens,
                "output_tokens": final.usage.output_tokens,
            })
            yield f"event: usage\ndata: {usage_data}\n\n"
            yield "event: done\ndata: {}\n\n"
    except Exception as e:
        error_data = json.dumps({"error": str(e)})
        yield f"event: error\ndata: {error_data}\n\n"


@app.post("/chat/stream/rich")
async def rich_stream_endpoint(request: Request):
    body = await request.json()
    return StreamingResponse(
        rich_token_stream(body.get("message", "")),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
```

## Solution 2: WebSocket Streaming for Bidirectional Chat

Use WebSockets for full-duplex streaming — ideal for multi-turn chat where the client also sends messages while the agent streams.

```python
import asyncio
import json
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from anthropic import AsyncAnthropic

app = FastAPI()
client = AsyncAnthropic()


@app.websocket("/ws/chat")
async def websocket_chat(websocket: WebSocket):
    await websocket.accept()
    conversation_history = []

    try:
        while True:
            # Receive message from client
            raw = await websocket.receive_text()
            data = json.loads(raw)
            message = data.get("message", "")

            conversation_history.append({"role": "user", "content": message})

            # Signal streaming start
            await websocket.send_json({"type": "stream_start"})

            full_response = ""
            async with client.messages.stream(
                model="claude-haiku-4-5-20251001",
                max_tokens=2048,
                messages=conversation_history,
            ) as stream:
                async for text in stream.text_stream:
                    full_response += text
                    await websocket.send_json({
                        "type": "token",
                        "content": text,
                    })

            # Add assistant response to history
            conversation_history.append({
                "role": "assistant",
                "content": full_response,
            })

            # Signal streaming complete
            final = await stream.get_final_message()
            await websocket.send_json({
                "type": "stream_end",
                "usage": {
                    "input_tokens": final.usage.input_tokens,
                    "output_tokens": final.usage.output_tokens,
                },
            })

    except WebSocketDisconnect:
        pass
    except Exception as e:
        await websocket.send_json({"type": "error", "message": str(e)})


# JavaScript client:
# const ws = new WebSocket('ws://localhost:8000/ws/chat');
# ws.onmessage = (e) => {
#   const msg = JSON.parse(e.data);
#   if (msg.type === 'token') output.textContent += msg.content;
#   if (msg.type === 'stream_end') console.log('Done:', msg.usage);
# };
# ws.send(JSON.stringify({ message: 'Hello!' }));
```

## Solution 3: Sentence-Level Chunking for Smoother Rendering

Buffer tokens until a complete sentence is formed; emit sentence-by-sentence for smoother UI updates than per-token.

```python
import asyncio
import re
from anthropic import AsyncAnthropic
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
import json

app = FastAPI()
client = AsyncAnthropic()

SENTENCE_ENDINGS = re.compile(r'(?<=[.!?])\s+')


async def sentence_stream(message: str):
    """Buffer tokens into sentences before yielding."""
    buffer = ""
    async with client.messages.stream(
        model="claude-haiku-4-5-20251001",
        max_tokens=2048,
        messages=[{"role": "user", "content": message}],
    ) as stream:
        async for token in stream.text_stream:
            buffer += token
            # Check if buffer contains a complete sentence
            sentences = SENTENCE_ENDINGS.split(buffer)
            if len(sentences) > 1:
                # Yield all complete sentences, keep remainder in buffer
                for sentence in sentences[:-1]:
                    if sentence.strip():
                        yield f"data: {json.dumps({'chunk': sentence + ' '})}\n\n"
                buffer = sentences[-1]

        # Yield any remaining buffer content
        if buffer.strip():
            yield f"data: {json.dumps({'chunk': buffer})}\n\n"

    yield f"data: {json.dumps({'done': True})}\n\n"


@app.post("/chat/sentence-stream")
async def sentence_stream_endpoint(request: Request):
    body = await request.json()
    return StreamingResponse(
        sentence_stream(body.get("message", "")),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# Paragraph-level variant for long-form content:
async def paragraph_stream(message: str):
    """Emit complete paragraphs (double newline boundaries)."""
    buffer = ""
    async with client.messages.stream(
        model="claude-haiku-4-5-20251001",
        max_tokens=4096,
        messages=[{"role": "user", "content": message}],
    ) as stream:
        async for token in stream.text_stream:
            buffer += token
            if "\n\n" in buffer:
                parts = buffer.split("\n\n")
                for part in parts[:-1]:
                    if part.strip():
                        yield f"data: {json.dumps({'paragraph': part})}\n\n"
                buffer = parts[-1]

        if buffer.strip():
            yield f"data: {json.dumps({'paragraph': buffer})}\n\n"
    yield f"data: {json.dumps({'done': True})}\n\n"
```

## Solution 4: Chunked HTTP Response for Non-SSE Clients

Use chunked transfer encoding for clients that don't support SSE — compatible with any HTTP client.

```python
import asyncio
import json
from aiohttp import web
from anthropic import AsyncAnthropic

client = AsyncAnthropic()


async def chunked_chat_handler(request: web.Request) -> web.StreamResponse:
    data = await request.json()
    message = data.get("message", "")

    response = web.StreamResponse(
        status=200,
        reason="OK",
        headers={
            "Content-Type": "application/x-ndjson",  # Newline-delimited JSON
            "Transfer-Encoding": "chunked",
            "Cache-Control": "no-cache",
        },
    )
    await response.prepare(request)

    async with client.messages.stream(
        model="claude-haiku-4-5-20251001",
        max_tokens=2048,
        messages=[{"role": "user", "content": message}],
    ) as stream:
        async for token in stream.text_stream:
            chunk = json.dumps({"token": token}) + "\n"
            await response.write(chunk.encode())

    final = await stream.get_final_message()
    done_chunk = json.dumps({
        "done": True,
        "input_tokens": final.usage.input_tokens,
        "output_tokens": final.usage.output_tokens,
    }) + "\n"
    await response.write(done_chunk.encode())
    await response.write_eof()
    return response


app = web.Application()
app.router.add_post("/chat/chunked", chunked_chat_handler)


# Python client that reads chunked response:
async def read_chunked_response():
    import aiohttp
    async with aiohttp.ClientSession() as session:
        async with session.post(
            "http://localhost:8080/chat/chunked",
            json={"message": "Tell me about Python async."},
        ) as resp:
            async for line in resp.content:
                line = line.strip()
                if not line:
                    continue
                data = json.loads(line)
                if "token" in data:
                    print(data["token"], end="", flush=True)
                elif data.get("done"):
                    print(f"\n[Done: {data['output_tokens']} output tokens]")
```

## Solution 5: Markdown-Aware Streaming with Block Detection

Detect markdown blocks (code fences, headers, lists) during streaming; emit structured events so the frontend can render correctly.

```python
import asyncio
import json
import re
from dataclasses import dataclass, field
from enum import Enum
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from anthropic import AsyncAnthropic

app = FastAPI()
client = AsyncAnthropic()


class BlockType(Enum):
    TEXT = "text"
    CODE = "code"
    HEADER = "header"
    LIST_ITEM = "list_item"


@dataclass
class StreamParser:
    """Detects markdown structure in streaming tokens."""
    buffer: str = ""
    in_code_block: bool = False
    code_lang: str = ""
    _events: list[dict] = field(default_factory=list)

    def feed(self, token: str) -> list[dict]:
        """Process a token; return any complete events ready to emit."""
        self.buffer += token
        events = []

        # Code block handling
        if "```" in self.buffer:
            parts = self.buffer.split("```")
            for i, part in enumerate(parts):
                if i % 2 == 0 and not self.in_code_block:
                    # Text content
                    if part:
                        events.append({"type": BlockType.TEXT.value, "content": part})
                elif i % 2 == 1 and not self.in_code_block:
                    # Starting code block — first line is language
                    lines = part.split("\n", 1)
                    self.code_lang = lines[0].strip()
                    self.in_code_block = True
                    events.append({
                        "type": "code_start",
                        "language": self.code_lang,
                    })
                    if len(lines) > 1:
                        events.append({"type": "code_token", "content": lines[1]})
                elif self.in_code_block:
                    # Ending code block
                    if part:
                        events.append({"type": "code_token", "content": part})
                    events.append({"type": "code_end"})
                    self.in_code_block = False
                    self.code_lang = ""
            self.buffer = ""
            return events

        # Flush buffer on newlines (for headers and list items)
        if "\n" in self.buffer:
            lines = self.buffer.split("\n")
            for line in lines[:-1]:
                if line.startswith("# "):
                    events.append({"type": "header", "level": 1, "content": line[2:]})
                elif line.startswith("## "):
                    events.append({"type": "header", "level": 2, "content": line[3:]})
                elif re.match(r'^[-*]\s', line):
                    events.append({"type": "list_item", "content": line[2:]})
                elif line:
                    if self.in_code_block:
                        events.append({"type": "code_token", "content": line + "\n"})
                    else:
                        events.append({"type": BlockType.TEXT.value, "content": line + "\n"})
            self.buffer = lines[-1]

        return events

    def flush(self) -> list[dict]:
        if self.buffer:
            t = "code_token" if self.in_code_block else BlockType.TEXT.value
            return [{"type": t, "content": self.buffer}]
        return []


async def markdown_stream(message: str):
    parser = StreamParser()
    async with client.messages.stream(
        model="claude-haiku-4-5-20251001",
        max_tokens=2048,
        messages=[{"role": "user", "content": message}],
    ) as stream:
        async for token in stream.text_stream:
            events = parser.feed(token)
            for event in events:
                yield f"data: {json.dumps(event)}\n\n"

    for event in parser.flush():
        yield f"data: {json.dumps(event)}\n\n"
    yield f"data: {json.dumps({'type': 'done'})}\n\n"


@app.post("/chat/markdown-stream")
async def markdown_stream_endpoint(request: Request):
    body = await request.json()
    return StreamingResponse(
        markdown_stream(body.get("message", "")),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
```

## Solution 6: Multi-Consumer Fan-Out Stream

Stream from one Anthropic call; fan out the token stream to multiple downstream consumers simultaneously.

```python
import asyncio
from dataclasses import dataclass, field
from anthropic import AsyncAnthropic
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
import json
import uuid

app = FastAPI()
client = AsyncAnthropic()


@dataclass
class StreamBroadcaster:
    """Fan out a single LLM stream to multiple consumers."""
    stream_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    _queues: dict[str, asyncio.Queue] = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _done: bool = False
    _full_text: str = ""

    async def subscribe(self) -> tuple[str, asyncio.Queue]:
        consumer_id = str(uuid.uuid4())[:8]
        q: asyncio.Queue = asyncio.Queue()
        async with self._lock:
            self._queues[consumer_id] = q
            # If stream already finished, replay buffered text
            if self._done and self._full_text:
                await q.put(self._full_text)
                await q.put(None)  # EOF
        return consumer_id, q

    async def unsubscribe(self, consumer_id: str):
        async with self._lock:
            self._queues.pop(consumer_id, None)

    async def broadcast(self, token: str):
        self._full_text += token
        async with self._lock:
            for q in self._queues.values():
                await q.put(token)

    async def close(self):
        self._done = True
        async with self._lock:
            for q in self._queues.values():
                await q.put(None)  # EOF sentinel


# Active broadcasters by request_id
_broadcasters: dict[str, StreamBroadcaster] = {}


async def run_stream(request_id: str, message: str):
    """Run the LLM stream and broadcast to all subscribers."""
    broadcaster = StreamBroadcaster(stream_id=request_id)
    _broadcasters[request_id] = broadcaster

    try:
        async with client.messages.stream(
            model="claude-haiku-4-5-20251001",
            max_tokens=2048,
            messages=[{"role": "user", "content": message}],
        ) as stream:
            async for token in stream.text_stream:
                await broadcaster.broadcast(token)
    finally:
        await broadcaster.close()
        _broadcasters.pop(request_id, None)


@app.post("/chat/fanout")
async def start_fanout_stream(request: Request):
    """Start a stream; returns request_id for consumers to subscribe."""
    body = await request.json()
    message = body.get("message", "")
    request_id = str(uuid.uuid4())[:8]
    asyncio.create_task(run_stream(request_id, message))
    return {"request_id": request_id}


@app.get("/chat/fanout/{request_id}/subscribe")
async def subscribe_to_stream(request_id: str):
    """Subscribe to an active stream as a consumer."""
    broadcaster = _broadcasters.get(request_id)
    if broadcaster is None:
        return StreamingResponse(
            iter([f"data: {json.dumps({'error': 'stream not found'})}\n\n"]),
            media_type="text/event-stream",
        )

    consumer_id, queue = await broadcaster.subscribe()

    async def consumer_generator():
        try:
            while True:
                token = await asyncio.wait_for(queue.get(), timeout=30.0)
                if token is None:  # EOF
                    yield f"data: {json.dumps({'done': True})}\n\n"
                    break
                yield f"data: {json.dumps({'token': token})}\n\n"
        except asyncio.TimeoutError:
            yield f"data: {json.dumps({'error': 'timeout'})}\n\n"
        finally:
            await broadcaster.unsubscribe(consumer_id)

    return StreamingResponse(
        consumer_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
```

## Comparison Table

| Solution | Protocol | Bidirectional | Browser Native | Markdown Aware | Fan-Out | Best For |
|---|---|---|---|---|---|---|
| SSE (FastAPI) | HTTP/1.1 | No (server→client) | Yes (EventSource) | No | No | Simple chat UIs, one-way streaming |
| WebSocket | WS | Yes | Yes (WebSocket API) | No | No | Multi-turn chat, interactive agents |
| Sentence Chunking | SSE | No | Yes | No | No | Smoother UX than per-token updates |
| Chunked HTTP | HTTP chunked | No | Via fetch() | No | No | Non-SSE clients, CLI consumers |
| Markdown-Aware | SSE | No | Yes | Yes | No | Rich text editors, code-heavy responses |
| Fan-Out Stream | SSE | No | Yes | No | Yes | Multiple simultaneous viewers, collaborative |

**Recommended**: Start with **SSE** (Solution 1) — it's the simplest, works in all modern browsers natively, and requires minimal backend code. Add **WebSocket** (Solution 2) when you need bidirectional real-time chat. Use **Markdown-Aware** (Solution 5) when your agent produces code blocks or structured text that needs proper rendering. Use **Sentence Chunking** (Solution 3) as a middle ground between per-token jitter and full-response latency.
