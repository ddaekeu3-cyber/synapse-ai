---
layout: solution
title: "Streaming Response Not Used — User Waits for Full Response Before Seeing Anything"
category: token-cost
description: "Agent generates a 2,000-token response. User sees nothing for 20 seconds, then the full text appears at once. Streaming would show the first token in <1 second. Not a token-cost issue but a perceived latency issue that kills UX."
tags: [streaming, latency, ux, response-time, ttfb, perceived-performance, real-time]
---

# Streaming Response Not Used — User Waits for Full Response Before Seeing Anything

## Symptom
- User submits a question, waits 15-30 seconds, then full response appears at once
- Chat UI feels unresponsive — users think the app is broken and refresh
- Long responses (1000+ tokens) have noticeably worse UX than short responses
- Server-side agent accumulates full response before sending HTTP response
- "Time to first byte" is the full generation time instead of milliseconds

## Root Cause
The default `client.messages.create()` call waits for the full response before returning. For long responses, this means the user waits the full generation time with zero feedback. Streaming sends tokens as they're generated — first token arrives in <1 second even for very long responses.

## Fix

### Option 1: Basic streaming with Anthropic SDK

```python
import anthropic

client = anthropic.Anthropic()

def stream_response(prompt: str, system: str = "") -> str:
    """Stream response — print tokens as they arrive"""
    full_text = ""

    with client.messages.stream(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=system,
        messages=[{"role": "user", "content": prompt}]
    ) as stream:
        for text_chunk in stream.text_stream:
            print(text_chunk, end="", flush=True)  # Show immediately
            full_text += text_chunk

    print()  # Newline after response
    return full_text

# Non-streaming: user waits 15s, sees response
# Streaming: user sees first word in <1s, full response in 15s
```

### Option 2: FastAPI SSE endpoint for web clients

```python
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
import anthropic
import json

app = FastAPI()
client = anthropic.Anthropic()

@app.post("/chat/stream")
async def chat_stream(request: dict):
    """
    Server-sent events endpoint — sends tokens as they're generated.
    Client receives each chunk immediately.
    """
    prompt = request.get("message", "")
    system = request.get("system", "")

    async def generate():
        with client.messages.stream(
            model="claude-sonnet-4-6",
            max_tokens=2048,
            system=system,
            messages=[{"role": "user", "content": prompt}]
        ) as stream:
            for text_chunk in stream.text_stream:
                # SSE format: "data: {...}\n\n"
                yield f"data: {json.dumps({'text': text_chunk})}\n\n"

            # Send final event with usage stats
            message = stream.get_final_message()
            yield f"data: {json.dumps({'done': True, 'usage': {'input_tokens': message.usage.input_tokens, 'output_tokens': message.usage.output_tokens}})}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # Disable nginx buffering
        }
    )
```

### Option 3: WebSocket streaming for real-time chat

```python
from fastapi import WebSocket
import anthropic
import json

client = anthropic.Anthropic()

@app.websocket("/ws/chat")
async def websocket_chat(websocket: WebSocket):
    await websocket.accept()

    try:
        while True:
            # Receive message from client
            data = await websocket.receive_json()
            prompt = data.get("message", "")

            # Stream response back token by token
            full_response = ""
            with client.messages.stream(
                model="claude-sonnet-4-6",
                max_tokens=2048,
                messages=[{"role": "user", "content": prompt}]
            ) as stream:
                for chunk in stream.text_stream:
                    full_response += chunk
                    await websocket.send_json({
                        "type": "chunk",
                        "text": chunk
                    })

            # Send completion signal
            await websocket.send_json({
                "type": "done",
                "full_text": full_response
            })

    except Exception as e:
        await websocket.send_json({"type": "error", "message": str(e)})
    finally:
        await websocket.close()
```

### Option 4: Async streaming with asyncio

```python
import asyncio
import anthropic

client = anthropic.AsyncAnthropic()

async def stream_to_callback(
    prompt: str,
    on_chunk: callable,
    on_complete: callable = None,
    system: str = ""
) -> str:
    """
    Stream response, calling on_chunk for each token.
    Useful for updating UI state without blocking.
    """
    full_text = ""

    async with client.messages.stream(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        system=system,
        messages=[{"role": "user", "content": prompt}]
    ) as stream:
        async for chunk in stream.text_stream:
            full_text += chunk
            await on_chunk(chunk)

    if on_complete:
        await on_complete(full_text)

    return full_text

# Example: update a UI buffer as tokens arrive
buffer = []

async def handle_chunk(text: str):
    buffer.append(text)
    # In a real UI: update React state, write to terminal, etc.

async def handle_complete(full_text: str):
    print(f"\nComplete. Total: {len(full_text)} chars")

await stream_to_callback(
    prompt="Explain quantum computing",
    on_chunk=handle_chunk,
    on_complete=handle_complete
)
```

### Option 5: Stream with graceful interruption

```python
import asyncio
import anthropic

client = anthropic.AsyncAnthropic()

class StreamController:
    """Allow user to interrupt a stream mid-generation"""

    def __init__(self):
        self._cancelled = False
        self._partial_text = ""

    def cancel(self):
        self._cancelled = True

    async def stream(self, prompt: str, on_chunk: callable) -> str:
        async with client.messages.stream(
            model="claude-sonnet-4-6",
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}]
        ) as stream:
            async for chunk in stream.text_stream:
                if self._cancelled:
                    print("Stream cancelled by user")
                    break
                self._partial_text += chunk
                await on_chunk(chunk)

        return self._partial_text

controller = StreamController()

async def run_with_timeout(prompt: str, timeout: float = 30.0) -> str:
    """Stream with automatic timeout"""
    chunks = []

    async def collect(chunk: str):
        chunks.append(chunk)

    try:
        await asyncio.wait_for(
            controller.stream(prompt, collect),
            timeout=timeout
        )
    except asyncio.TimeoutError:
        print(f"Streaming timed out after {timeout}s — returning partial response")

    return "".join(chunks)
```

### Option 6: Progress indicator for non-streaming cases

```python
import asyncio
import time

async def call_with_progress_indicator(
    prompt: str,
    client,
    indicator_interval: float = 0.5
) -> str:
    """
    When streaming is not available (e.g., function calling),
    show a progress indicator so users know the agent is working.
    """
    result_container = []
    error_container = []

    async def fetch():
        try:
            response = await client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=1024,
                messages=[{"role": "user", "content": prompt}]
            )
            result_container.append(response.content[0].text)
        except Exception as e:
            error_container.append(e)

    # Start fetch in background
    fetch_task = asyncio.create_task(fetch())

    # Show progress while waiting
    start = time.time()
    indicators = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
    i = 0
    while not fetch_task.done():
        elapsed = time.time() - start
        print(f"\r{indicators[i % len(indicators)]} Thinking... {elapsed:.1f}s", end="", flush=True)
        i += 1
        await asyncio.sleep(indicator_interval)

    print("\r" + " " * 30 + "\r", end="")  # Clear indicator

    if error_container:
        raise error_container[0]
    return result_container[0]
```

## Streaming vs Non-Streaming UX Impact

| Response length | Non-streaming wait | Streaming first token |
|---|---|---|
| 100 tokens (~75 words) | ~1s | <0.5s |
| 500 tokens (~375 words) | ~5s | <0.5s |
| 1000 tokens (~750 words) | ~10s | <0.5s |
| 2000 tokens (~1500 words) | ~20s | <0.5s |

## Expected Token Savings
Streaming doesn't reduce tokens — it reduces perceived latency.
Users who abandon slow non-streaming UIs → 100% token waste (no response seen).
Streaming ensures users see progress — drop-off rate drops significantly.

## Environment
- Any user-facing agent with a chat or text generation UI; streaming is essential for responses over 200 tokens
- Source: direct experience; streaming is the single highest-impact UX improvement for LLM-powered products
