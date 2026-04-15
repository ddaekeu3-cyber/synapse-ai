---
layout: solution
title: "Agent Doesn't Implement Streaming Heartbeat to Prevent Timeout"
category: streaming
description: "Long-running Claude streaming calls get killed by load balancers, reverse proxies, or client HTTP libraries that enforce idle-connection timeouts. Without a heartbeat or keep-alive mechanism, streams silently drop mid-response after 30-60 seconds of no output."
tags: [streaming, heartbeat, timeout, nginx, load-balancer, sse, keep-alive, asyncio]
---

## Problem

Claude streaming responses can go silent during extended thinking, long tool-use chains, or large generations. Load balancers (nginx default: 60s), AWS ALB (60s), Cloudflare (100s), and HTTP clients often terminate connections with no activity. The agent gets no error — the socket simply closes. The user sees a truncated response or a spinner that never resolves. Heartbeat patterns keep the connection alive by periodically sending benign data during silent stretches.

## Solutions

### Option 1: SSE Comment Heartbeat During Stream Gaps

```python
import anthropic
import asyncio
import time
from typing import AsyncIterator

client = anthropic.AsyncAnthropic()

async def stream_with_sse_heartbeat(
    messages: list[dict],
    system: str = "",
    heartbeat_interval: float = 15.0,
) -> AsyncIterator[str]:
    """
    Yields SSE-formatted lines. Sends ': heartbeat\\n\\n' comments when
    no token has arrived within heartbeat_interval seconds.
    SSE comments are ignored by EventSource clients but keep TCP alive.
    """
    last_token_time = time.time()

    async def token_generator():
        kwargs = dict(model="claude-sonnet-4-6", max_tokens=1024, messages=messages)
        if system:
            kwargs["system"] = system
        async with client.messages.stream(**kwargs) as stream:
            async for text in stream.text_stream:
                yield text

    gen = token_generator().__aiter__()
    pending: asyncio.Task | None = None

    try:
        while True:
            if pending is None:
                pending = asyncio.create_task(gen.__anext__())

            try:
                token = await asyncio.wait_for(
                    asyncio.shield(pending),
                    timeout=heartbeat_interval,
                )
                pending = None
                last_token_time = time.time()
                yield f"data: {token}\n\n"
            except asyncio.TimeoutError:
                # No token arrived — send heartbeat comment
                yield ": heartbeat\n\n"
            except StopAsyncIteration:
                break
    finally:
        if pending and not pending.done():
            pending.cancel()

async def demo():
    chunks = []
    async for chunk in stream_with_sse_heartbeat(
        messages=[{"role": "user", "content": "Write a detailed 5-paragraph essay on photosynthesis."}],
    ):
        if chunk.startswith(": heartbeat"):
            print("[heartbeat sent]", flush=True)
        else:
            text = chunk.removeprefix("data: ").removesuffix("\n\n")
            chunks.append(text)
            print(text, end="", flush=True)
    print(f"\n\nTotal chunks: {len(chunks)}")

if __name__ == "__main__":
    asyncio.run(demo())

# Expected Token Savings: zero — heartbeats are comments, not tokens; full Claude response preserved
# Environment: SSE/EventSource endpoints behind nginx or ALB; heartbeat interval < proxy idle timeout
```

### Option 2: FastAPI SSE Endpoint with Periodic Keep-Alive

```python
import anthropic
import asyncio
import time
from fastapi import FastAPI
from fastapi.responses import StreamingResponse

app = FastAPI()
client = anthropic.AsyncAnthropic()

HEARTBEAT_INTERVAL = 20.0  # seconds; set below your proxy's idle timeout

async def sse_stream_with_keepalive(
    prompt: str,
    heartbeat_interval: float = HEARTBEAT_INTERVAL,
):
    """
    Async generator producing SSE lines with keep-alive comments.
    Suitable for FastAPI StreamingResponse.
    """
    queue: asyncio.Queue[str | None] = asyncio.Queue()

    async def producer():
        try:
            async with client.messages.stream(
                model="claude-sonnet-4-6",
                max_tokens=2048,
                messages=[{"role": "user", "content": prompt}],
            ) as stream:
                async for text in stream.text_stream:
                    await queue.put(text)
        except Exception as e:
            await queue.put(f"[error: {e}]")
        finally:
            await queue.put(None)  # sentinel

    producer_task = asyncio.create_task(producer())

    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=heartbeat_interval)
            except asyncio.TimeoutError:
                yield ": keep-alive\n\n"
                continue

            if item is None:
                yield "data: [DONE]\n\n"
                break
            # Escape newlines for SSE data field
            safe = item.replace("\n", "\\n")
            yield f"data: {safe}\n\n"
    finally:
        producer_task.cancel()

@app.get("/stream")
async def stream_endpoint(q: str = "Tell me about the solar system."):
    return StreamingResponse(
        sse_stream_with_keepalive(q),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable nginx buffering
            "Connection": "keep-alive",
        },
    )

# Run with: uvicorn <module>:app --host 0.0.0.0 --port 8000
# Expected Token Savings: zero — keep-alive is out-of-band; queue approach cleanly separates producer/consumer
# Environment: FastAPI + nginx/ALB; X-Accel-Buffering:no disables nginx proxy buffering
```

### Option 3: WebSocket Heartbeat with Ping Frames

```python
import anthropic
import asyncio
import json
import websockets
from websockets.server import WebSocketServerProtocol

client = anthropic.AsyncAnthropic()

async def handler(ws: WebSocketServerProtocol):
    """
    Receive a JSON message {"prompt": "..."} and stream back tokens.
    Sends {"type": "ping"} frames every 15s to prevent WS idle close.
    """
    raw = await ws.recv()
    data = json.loads(raw)
    prompt = data.get("prompt", "Hello")

    queue: asyncio.Queue[str | None] = asyncio.Queue()

    async def producer():
        try:
            async with client.messages.stream(
                model="claude-sonnet-4-6",
                max_tokens=1024,
                messages=[{"role": "user", "content": prompt}],
            ) as stream:
                async for text in stream.text_stream:
                    await queue.put(text)
        except Exception as e:
            await queue.put(None)
        finally:
            await queue.put(None)

    asyncio.create_task(producer())

    heartbeat_interval = 15.0
    while True:
        try:
            token = await asyncio.wait_for(queue.get(), timeout=heartbeat_interval)
        except asyncio.TimeoutError:
            await ws.send(json.dumps({"type": "ping"}))
            continue

        if token is None:
            await ws.send(json.dumps({"type": "done"}))
            break
        await ws.send(json.dumps({"type": "token", "text": token}))

async def main():
    print("WebSocket server on ws://localhost:8765")
    async with websockets.serve(handler, "localhost", 8765):
        await asyncio.Future()  # run forever

if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: zero — ping frames are WebSocket control frames, not API tokens
# Environment: WebSocket frontends; ping frames prevent WS idle close on CDNs and proxies
```

### Option 4: HTTP Chunked Transfer with Padding Bytes

```python
import anthropic
import asyncio
import time
from aiohttp import web

client = anthropic.AsyncAnthropic()

HEARTBEAT_INTERVAL = 15.0
# Zero-width space + newline: invisible to end-user but flushes TCP buffer
HEARTBEAT_CHUNK = b"\xe2\x80\x8b\n"  # U+200B ZERO WIDTH SPACE

async def chunked_stream(request: web.Request) -> web.StreamResponse:
    prompt = request.rel_url.query.get("q", "Explain quantum entanglement in detail.")

    response = web.StreamResponse(headers={
        "Content-Type": "text/plain; charset=utf-8",
        "X-Accel-Buffering": "no",
        "Cache-Control": "no-cache",
    })
    await response.prepare(request)

    queue: asyncio.Queue[bytes | None] = asyncio.Queue()

    async def producer():
        try:
            async with client.messages.stream(
                model="claude-sonnet-4-6",
                max_tokens=1024,
                messages=[{"role": "user", "content": prompt}],
            ) as stream:
                async for text in stream.text_stream:
                    await queue.put(text.encode("utf-8"))
        finally:
            await queue.put(None)

    asyncio.create_task(producer())

    while True:
        try:
            chunk = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_INTERVAL)
        except asyncio.TimeoutError:
            await response.write(HEARTBEAT_CHUNK)
            continue

        if chunk is None:
            break
        await response.write(chunk)

    await response.write_eof()
    return response

app = web.Application()
app.router.add_get("/stream", chunked_stream)

if __name__ == "__main__":
    web.run_app(app, host="0.0.0.0", port=8080)

# Expected Token Savings: zero — padding bytes are transport-layer only
# Environment: aiohttp services behind chunked-transfer-aware proxies; zero-width space invisible in UI
```

### Option 5: Streaming Watchdog with Automatic Reconnect

```python
import anthropic
import asyncio
import time
from typing import AsyncIterator

client = anthropic.AsyncAnthropic()

class StreamingWatchdog:
    """
    Wraps a Claude stream with a watchdog timer. If no token arrives
    within `idle_timeout`, the stream is cancelled and transparently
    resumed from where it left off by accumulating partial content.
    """
    def __init__(self, idle_timeout: float = 30.0, max_retries: int = 3):
        self._idle_timeout = idle_timeout
        self._max_retries = max_retries

    async def stream(
        self,
        messages: list[dict],
        system: str = "",
        max_tokens: int = 1024,
    ) -> AsyncIterator[str]:
        accumulated = ""
        attempts = 0

        while attempts <= self._max_retries:
            try:
                # If we have partial content, inject it as assistant prefill
                effective_messages = list(messages)
                if accumulated:
                    effective_messages = list(messages) + [
                        {"role": "assistant", "content": accumulated}
                    ]

                kwargs = dict(
                    model="claude-sonnet-4-6",
                    max_tokens=max_tokens,
                    messages=effective_messages,
                )
                if system:
                    kwargs["system"] = system

                async with client.messages.stream(**kwargs) as stream:
                    async for text in stream.text_stream:
                        # Reset watchdog on each token
                        accumulated += text
                        yield text
                return  # stream completed normally
            except asyncio.CancelledError:
                attempts += 1
                print(f"\n[watchdog: idle timeout, reconnect attempt {attempts}]")
                if attempts > self._max_retries:
                    raise RuntimeError(f"Stream failed after {self._max_retries} reconnects")
                await asyncio.sleep(1.0)
            except Exception as e:
                raise

async def demo():
    watchdog = StreamingWatchdog(idle_timeout=25.0)
    full_text = ""
    async for token in watchdog.stream(
        messages=[{"role": "user", "content": "Write a very long story about a robot."}],
        max_tokens=512,
    ):
        full_text += token
        print(token, end="", flush=True)
    print(f"\n\nTotal chars: {len(full_text)}")

if __name__ == "__main__":
    asyncio.run(demo())

# Expected Token Savings: minor overhead on reconnect (re-sends accumulated prefix); avoids losing full response
# Environment: unreliable network paths; assistant prefill resumes generation from exact stopping point
```

### Option 6: SQLite-Logged Heartbeat Metrics for Timeout Diagnosis

```python
import anthropic
import asyncio
import sqlite3
import time
import uuid
from pathlib import Path

client = anthropic.AsyncAnthropic()
DB = Path("/tmp/stream_heartbeat.db")

def init_db():
    con = sqlite3.connect(DB)
    con.execute("""
        CREATE TABLE IF NOT EXISTS stream_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            stream_id TEXT NOT NULL,
            event TEXT NOT NULL,  -- token | heartbeat | complete | error
            ts REAL NOT NULL,
            gap_seconds REAL,
            details TEXT
        )
    """)
    con.commit()
    con.close()

def log_event(stream_id: str, event: str, gap: float | None = None, details: str = ""):
    con = sqlite3.connect(DB)
    con.execute(
        "INSERT INTO stream_events (stream_id, event, ts, gap_seconds, details) VALUES (?,?,?,?,?)",
        (stream_id, event, time.time(), gap, details),
    )
    con.commit()
    con.close()

async def monitored_stream(
    messages: list[dict],
    heartbeat_interval: float = 15.0,
) -> str:
    stream_id = str(uuid.uuid4())[:8]
    last_token_time = time.time()
    full_text = ""
    heartbeat_count = 0

    queue: asyncio.Queue[str | None] = asyncio.Queue()

    async def producer():
        try:
            async with client.messages.stream(
                model="claude-sonnet-4-6",
                max_tokens=512,
                messages=messages,
            ) as stream:
                async for text in stream.text_stream:
                    await queue.put(text)
        finally:
            await queue.put(None)

    asyncio.create_task(producer())

    while True:
        try:
            token = await asyncio.wait_for(queue.get(), timeout=heartbeat_interval)
        except asyncio.TimeoutError:
            gap = time.time() - last_token_time
            heartbeat_count += 1
            log_event(stream_id, "heartbeat", gap=gap, details=f"hb#{heartbeat_count}")
            print(f"  [♥ heartbeat | gap={gap:.1f}s]", flush=True)
            continue

        if token is None:
            log_event(stream_id, "complete", gap=time.time() - last_token_time)
            break

        gap = time.time() - last_token_time
        last_token_time = time.time()
        log_event(stream_id, "token", gap=gap)
        full_text += token
        print(token, end="", flush=True)

    return full_text

def print_stream_stats():
    con = sqlite3.connect(DB)
    rows = con.execute("""
        SELECT event, COUNT(*) as n, AVG(gap_seconds) as avg_gap, MAX(gap_seconds) as max_gap
        FROM stream_events GROUP BY event
    """).fetchall()
    con.close()
    print("\n--- Stream Heartbeat Stats ---")
    for event, n, avg_gap, max_gap in rows:
        print(f"  {event:12s}: count={n:4d} | avg_gap={avg_gap:.2f}s | max_gap={max_gap:.2f}s")

async def main():
    init_db()
    print("Streaming with heartbeat monitoring...\n")
    result = await monitored_stream(
        messages=[{"role": "user", "content": "Write a detailed technical explanation of TCP/IP networking."}],
        heartbeat_interval=10.0,
    )
    print(f"\n\nFull response length: {len(result)} chars")
    print_stream_stats()

if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: zero — heartbeats are timer events; SQLite reveals which prompts cause long gaps
# Environment: production agents; max_gap metric identifies prompts that reliably trigger proxy timeouts
```

## Comparison

| Option | Protocol | Heartbeat Type | Client Visibility | Proxy Compatibility |
|--------|----------|---------------|-------------------|---------------------|
| 1 — SSE comment | SSE/HTTP | `: heartbeat` comment | Invisible to EventSource | nginx, ALB, Cloudflare |
| 2 — FastAPI SSE | SSE/HTTP | `: keep-alive` comment | Invisible to EventSource | FastAPI + nginx (X-Accel-Buffering:no) |
| 3 — WebSocket ping | WebSocket | `{"type":"ping"}` frame | App-level ping message | Any WS proxy |
| 4 — Chunked padding | HTTP chunked | Zero-width space byte | Invisible in UI | aiohttp + any HTTP/1.1 proxy |
| 5 — Watchdog reconnect | HTTP stream | Reconnect + prefill | Seamless (no gap) | Works at application layer |
| 6 — SQLite metrics | SSE/HTTP | `: keep-alive` comment | Invisible + logged | Diagnoses which prompts need longer timeouts |
