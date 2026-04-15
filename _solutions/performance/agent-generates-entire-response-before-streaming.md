---
layout: solution
title: "Agent Generates Entire Response Before Streaming to User"
category: performance
description: "Users stare at a blank screen for 3–10 seconds while the agent generates the full response. Perceived latency is far worse than actual latency. Users abandon sessions."
tags: [streaming, latency, ttfb, ux, async, server-sent-events, websocket]
---

## Symptom

Your agent collects the complete response from the Anthropic API before displaying anything. A 500-token response takes 4 seconds of silence, then appears all at once. Users perceive the agent as slow even when generation speed is normal. Dashboards show low time-to-first-byte (TTFB) as a key satisfaction driver — your agent is failing on that metric entirely.

## Root Cause

The code uses `client.messages.create()` in blocking mode, waits for the full response, then returns the entire string. Streaming is available at zero additional cost via `client.messages.stream()`, but the non-streaming path is the SDK default and requires no code changes to the happy path — so teams never switch.

```python
# Anti-pattern: waits for full generation
response = client.messages.create(
    model="claude-sonnet-4-6",
    max_tokens=2048,
    messages=messages,
)
full_text = response.content[0].text  # nothing shown until here
print(full_text)
```

## Fix

---

### Option 1: Basic Streaming with text_stream

One-line change from blocking to streaming. Tokens appear as they are generated.

```python
import anthropic

client = anthropic.Anthropic()


def stream_response(messages: list[dict], system: str = "") -> str:
    """
    Stream response tokens to stdout as they arrive.
    Returns full text for downstream use.
    """
    collected = []

    kwargs = dict(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        messages=messages,
    )
    if system:
        kwargs["system"] = system

    with client.messages.stream(**kwargs) as stream:
        for text in stream.text_stream:
            print(text, end="", flush=True)
            collected.append(text)

    print()  # newline after stream ends
    return "".join(collected)


# Usage
messages = [{"role": "user", "content": "Explain how TCP handshakes work in detail."}]
full_text = stream_response(messages, system="You are a networking expert.")
print(f"\n[Total length: {len(full_text)} chars]")
```

**Expected Token Savings**: Zero — streaming costs the same. Perceived latency drops from 4–8s to ~200ms (time to first token).
**Environment**: Synchronous Python. Works in any terminal or script context.

---

### Option 2: Async Streaming for Concurrent Users

Non-blocking async streaming. Handle multiple users simultaneously without blocking the event loop.

```python
import asyncio
import time
import anthropic

client = anthropic.AsyncAnthropic()


async def async_stream(
    user_id: str,
    message: str,
    on_token=None,
) -> str:
    """
    Async streaming with per-token callback.
    on_token: async callable(user_id, token) for routing to correct client.
    """
    collected = []
    start = time.monotonic()
    first_token_time = None

    async with client.messages.stream(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        messages=[{"role": "user", "content": message}],
    ) as stream:
        async for text in stream.text_stream:
            if first_token_time is None:
                first_token_time = time.monotonic()
                ttfb = first_token_time - start
                print(f"[{user_id}] TTFB: {ttfb*1000:.0f}ms")

            collected.append(text)
            if on_token:
                await on_token(user_id, text)
            else:
                print(text, end="", flush=True)

    total = time.monotonic() - start
    full = "".join(collected)
    print(f"\n[{user_id}] Done in {total*1000:.0f}ms, {len(full)} chars")
    return full


async def demo_concurrent():
    """Show that two streams run concurrently, not sequentially."""

    async def print_token(user_id: str, token: str):
        print(f"[{user_id}] {token}", end="", flush=True)

    print("Starting two concurrent streams...\n")
    await asyncio.gather(
        async_stream("alice", "What is photosynthesis?", on_token=print_token),
        async_stream("bob",   "What is osmosis?",        on_token=print_token),
    )


asyncio.run(demo_concurrent())
```

**Expected Token Savings**: None in tokens; wall-clock time for N users scales as max(individual_times) instead of sum(individual_times).
**Environment**: Async Python. Essential for web servers handling concurrent requests.

---

### Option 3: FastAPI Server-Sent Events (SSE) Endpoint

Stream tokens from the Anthropic API directly to the browser via SSE. Zero buffering.

```python
import asyncio
import json
import anthropic
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

client = anthropic.AsyncAnthropic()


async def token_generator(message: str, system: str = ""):
    """
    Yield SSE-formatted events for each token.
    SSE format: 'data: <json>\n\n'
    """
    kwargs = dict(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        messages=[{"role": "user", "content": message}],
    )
    if system:
        kwargs["system"] = system

    try:
        async with client.messages.stream(**kwargs) as stream:
            async for text in stream.text_stream:
                event = json.dumps({"type": "token", "text": text})
                yield f"data: {event}\n\n"

            # Send final usage stats
            final_msg = await stream.get_final_message()
            usage = {
                "input_tokens":  final_msg.usage.input_tokens,
                "output_tokens": final_msg.usage.output_tokens,
            }
            yield f"data: {json.dumps({'type': 'done', 'usage': usage})}\n\n"

    except Exception as e:
        error_event = json.dumps({"type": "error", "message": str(e)})
        yield f"data: {error_event}\n\n"


@app.post("/stream")
async def stream_endpoint(request: Request):
    body = await request.json()
    message = body.get("message", "")
    system  = body.get("system", "You are a helpful assistant.")

    if not message:
        return {"error": "message required"}

    return StreamingResponse(
        token_generator(message, system),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # Disable nginx buffering
        },
    )


# Browser-side JavaScript to consume the stream:
BROWSER_EXAMPLE = """
const response = await fetch('/stream', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({message: 'Explain quantum computing'}),
});

const reader = response.body.getReader();
const decoder = new TextDecoder();
let buffer = '';

while (true) {
    const {done, value} = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, {stream: true});
    const lines = buffer.split('\\n\\n');
    buffer = lines.pop();

    for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        const event = JSON.parse(line.slice(6));
        if (event.type === 'token') {
            document.getElementById('output').textContent += event.token;
        }
    }
}
"""

# Run with: uvicorn solution:app --reload
```

**Expected Token Savings**: Zero token cost difference. Users see first characters in ~200ms vs 4–8s wait for full response.
**Environment**: FastAPI + uvicorn. Nginx must disable proxy_buffering for SSE to flow in real-time.

---

### Option 4: WebSocket Bidirectional Streaming

Full duplex: user types, tokens stream back immediately. Supports multi-turn without HTTP reconnection overhead.

```python
import asyncio
import json
import anthropic
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

app = FastAPI()
client = anthropic.AsyncAnthropic()


class ConversationSession:
    def __init__(self, websocket: WebSocket):
        self.ws = websocket
        self.history: list[dict] = []

    async def send_token(self, token: str):
        await self.ws.send_json({"type": "token", "text": token})

    async def send_done(self, usage: dict):
        await self.ws.send_json({"type": "done", "usage": usage})

    async def send_error(self, message: str):
        await self.ws.send_json({"type": "error", "message": message})

    async def handle_message(self, user_message: str):
        self.history.append({"role": "user", "content": user_message})

        full_reply = []
        try:
            async with client.messages.stream(
                model="claude-haiku-4-5-20251001",
                max_tokens=1024,
                system="You are a helpful assistant.",
                messages=self.history,
            ) as stream:
                async for token in stream.text_stream:
                    full_reply.append(token)
                    await self.send_token(token)

                final = await stream.get_final_message()
                await self.send_done({
                    "input_tokens":  final.usage.input_tokens,
                    "output_tokens": final.usage.output_tokens,
                })

            self.history.append({"role": "assistant", "content": "".join(full_reply)})

        except Exception as e:
            await self.send_error(str(e))


@app.websocket("/ws/chat")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    session = ConversationSession(websocket)

    try:
        while True:
            data = await websocket.receive_json()
            if data.get("type") == "message":
                await session.handle_message(data["content"])
            elif data.get("type") == "ping":
                await websocket.send_json({"type": "pong"})
    except WebSocketDisconnect:
        pass


# Browser-side JavaScript:
WS_CLIENT = """
const ws = new WebSocket('ws://localhost:8000/ws/chat');

ws.onmessage = (event) => {
    const msg = JSON.parse(event.data);
    if (msg.type === 'token') {
        document.getElementById('output').textContent += msg.text;
    } else if (msg.type === 'done') {
        console.log('Usage:', msg.usage);
    }
};

function sendMessage(text) {
    ws.send(JSON.stringify({type: 'message', content: text}));
}
"""
```

**Expected Token Savings**: Multi-turn sessions via persistent WS avoid HTTP handshake overhead (~50ms per round trip) and enable immediate follow-up without resubmitting history.
**Environment**: FastAPI + WebSocket. Works with React, Vue, plain JS. Keep-alive ping every 30s to prevent proxy timeout.

---

### Option 5: CLI Progress Bar with Streaming

Terminal UX that shows a live token counter and progress bar while streaming.

```python
import sys
import time
import anthropic
from dataclasses import dataclass

client = anthropic.Anthropic()


@dataclass
class StreamStats:
    first_token_ms: float = 0.0
    total_tokens: int = 0
    elapsed_ms: float = 0.0

    @property
    def tokens_per_second(self) -> float:
        return self.total_tokens / (self.elapsed_ms / 1000) if self.elapsed_ms > 0 else 0.0


def stream_with_progress(
    message: str,
    model: str = "claude-haiku-4-5-20251001",
    max_tokens: int = 1024,
    show_stats: bool = True,
) -> tuple[str, StreamStats]:
    """
    Stream with live progress display.
    Returns (full_text, stats).
    """
    stats = StreamStats()
    collected = []
    start = time.monotonic()

    # Status line at bottom (carriage return overwrite)
    def update_status(tokens: int, tps: float):
        status = f"\r  [{tokens} tokens | {tps:.0f} tok/s]   "
        sys.stderr.write(status)
        sys.stderr.flush()

    with client.messages.stream(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": message}],
    ) as stream:
        for text in stream.text_stream:
            if not collected:
                stats.first_token_ms = (time.monotonic() - start) * 1000

            collected.append(text)
            print(text, end="", flush=True)

            elapsed = (time.monotonic() - start) * 1000
            stats.total_tokens += len(text.split())  # rough estimate
            stats.elapsed_ms = elapsed

            if show_stats:
                update_status(stats.total_tokens, stats.tokens_per_second)

    print()  # newline
    if show_stats:
        sys.stderr.write(
            f"\r  TTFB: {stats.first_token_ms:.0f}ms | "
            f"{stats.total_tokens} tokens | "
            f"{stats.tokens_per_second:.0f} tok/s\n"
        )
        sys.stderr.flush()

    stats.elapsed_ms = (time.monotonic() - start) * 1000
    return "".join(collected), stats


# Usage
text, stats = stream_with_progress(
    "Write a detailed explanation of how HTTPS works end-to-end.",
    model="claude-sonnet-4-6",
    max_tokens=2048,
)
print(f"\nFinal stats: {stats}")
```

**Expected Token Savings**: Zero — pure UX improvement. TTFB visibility helps identify model/network bottlenecks.
**Environment**: Terminal/CLI applications. Works with any model.

---

### Option 6: Streaming with Early Abort on User Cancellation

Stream tokens but stop cleanly when the user cancels mid-generation. Avoids paying for tokens not seen.

```python
import asyncio
import signal
import anthropic

client = anthropic.AsyncAnthropic()


async def cancellable_stream(
    message: str,
    abort_event: asyncio.Event,
    on_token=None,
) -> tuple[str, bool]:
    """
    Stream with cancellation support.
    Returns (collected_text, was_cancelled).
    """
    collected = []
    cancelled = False

    async def _stream():
        async with client.messages.stream(
            model="claude-sonnet-4-6",
            max_tokens=2048,
            messages=[{"role": "user", "content": message}],
        ) as stream:
            async for token in stream.text_stream:
                if abort_event.is_set():
                    return  # Stop consuming; SDK closes connection
                collected.append(token)
                if on_token:
                    await on_token(token)
                else:
                    print(token, end="", flush=True)

    stream_task = asyncio.create_task(_stream())
    abort_task  = asyncio.create_task(abort_event.wait())

    done, pending = await asyncio.wait(
        [stream_task, abort_task],
        return_when=asyncio.FIRST_COMPLETED,
    )

    for task in pending:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    cancelled = abort_event.is_set()
    if cancelled:
        print(f"\n[Stream cancelled after {len(collected)} tokens]")

    return "".join(collected), cancelled


async def interactive_demo():
    """Demo: auto-cancel after 1.5 seconds to show partial output."""
    abort = asyncio.Event()

    async def auto_cancel():
        await asyncio.sleep(1.5)
        print("\n[User pressed Ctrl+C — cancelling...]")
        abort.set()

    question = "Write a comprehensive 2000-word essay on the history of computing."
    print(f"Streaming: {question[:60]}...\n")

    asyncio.create_task(auto_cancel())
    text, was_cancelled = await cancellable_stream(question, abort)

    if was_cancelled:
        print(f"\n[Partial response saved: {len(text)} chars]")
    else:
        print(f"\n[Complete response: {len(text)} chars]")
    return text


asyncio.run(interactive_demo())
```

**Expected Token Savings**: Cancelling after 500 of 2000 tokens saves 75% of output cost for that request. Multiply by abandon rate to find real-world savings.
**Environment**: Async Python. Wire `abort_event` to WebSocket disconnect, HTTP client disconnect, or keyboard interrupt.

---

| Option | Transport | Concurrent Users | Cancellation | Best For |
|--------|-----------|-----------------|-------------|----------|
| 1 | stdout / sync | Single | No | Scripts, CLI tools |
| 2 | async generator | Many | Via task cancel | Backend services |
| 3 | SSE (HTTP) | Many | On client disconnect | REST APIs, web frontends |
| 4 | WebSocket | Many | On WS close | Chat UIs, real-time apps |
| 5 | stdout + stats | Single | Ctrl+C | Developer tooling |
| 6 | async + abort event | Many | Explicit signal | Cost-sensitive streaming |
