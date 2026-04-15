---
layout: solution
title: "Agent Doesn't Implement SSE Endpoint for Frontend Clients"
category: streaming
description: "The backend agent buffers the entire Anthropic response before returning it, forcing users to stare at a blank screen until generation is complete instead of seeing tokens appear in real time."
tags: [streaming, sse, frontend, latency, ux]
---

## Symptom

The agent's HTTP endpoint waits for the full Anthropic response before responding to the browser. Users experience a long blank pause (5–30 seconds for long outputs) followed by sudden appearance of all text at once. Time-to-first-token (TTFT) equals total generation time. Frontend developers cannot implement incremental rendering, typing effects, or progress indicators.

## Root Cause

The default pattern — `client.messages.create()` followed by returning `response.content[0].text` — is a blocking, batch call. The Anthropic SDK supports streaming via `client.messages.stream()`, but the streaming output must be explicitly forwarded to the HTTP response using Server-Sent Events (SSE) or chunked transfer encoding. Without this forwarding layer, the network boundary swallows the streaming benefit.

## Fix

### Option 1 — FastAPI SSE endpoint with EventSourceResponse

```python
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
import anthropic
import json

app = FastAPI()
client = anthropic.Anthropic()

def generate_sse_stream(user_message: str):
    """Generator that yields SSE-formatted chunks from Anthropic stream."""
    with client.messages.stream(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        messages=[{"role": "user", "content": user_message}],
    ) as stream:
        for text_delta in stream.text_stream:
            # SSE format: "data: <payload>\n\n"
            payload = json.dumps({"type": "delta", "text": text_delta})
            yield f"data: {payload}\n\n"

        # Signal completion
        final = stream.get_final_message()
        done_payload = json.dumps({
            "type": "done",
            "stop_reason": final.stop_reason,
            "usage": {
                "input_tokens":  final.usage.input_tokens,
                "output_tokens": final.usage.output_tokens,
            },
        })
        yield f"data: {done_payload}\n\n"

@app.get("/stream")
def stream_endpoint(message: str):
    return StreamingResponse(
        generate_sse_stream(message),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable nginx buffering
        },
    )

# Frontend JavaScript to consume this:
# const es = new EventSource('/stream?message=Hello');
# es.onmessage = (e) => {
#   const data = JSON.parse(e.data);
#   if (data.type === 'delta') appendText(data.text);
#   if (data.type === 'done') es.close();
# };
```

**Expected Token Savings:** No token savings from streaming itself, but dramatically better UX reduces user abandonment; fewer "where is my answer?" follow-up requests.
**Environment:** FastAPI backends serving browser frontends; any REST API that must stream AI-generated text to web clients.

---

### Option 2 — aiohttp async SSE with asyncio streaming

```python
from aiohttp import web
import anthropic
import json

client = anthropic.AsyncAnthropic()

async def stream_handler(request: web.Request) -> web.StreamResponse:
    user_message = request.rel_url.query.get("message", "Hello")

    response = web.StreamResponse(
        status=200,
        headers={
            "Content-Type":    "text/event-stream",
            "Cache-Control":   "no-cache",
            "Connection":      "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
    await response.prepare(request)

    try:
        async with client.messages.stream(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            messages=[{"role": "user", "content": user_message}],
        ) as stream:
            async for text_delta in stream.text_stream:
                payload = json.dumps({"type": "delta", "text": text_delta})
                await response.write(f"data: {payload}\n\n".encode())

            final = await stream.get_final_message()
            done = json.dumps({"type": "done", "stop_reason": final.stop_reason})
            await response.write(f"data: {done}\n\n".encode())
    except Exception as exc:
        error = json.dumps({"type": "error", "message": str(exc)})
        await response.write(f"data: {error}\n\n".encode())
    finally:
        await response.write_eof()

    return response

app = web.Application()
app.router.add_get("/stream", stream_handler)

if __name__ == "__main__":
    web.run_app(app, host="0.0.0.0", port=8080)
```

**Expected Token Savings:** Async SSE handler serves many concurrent streams without thread-per-connection overhead; scales to many simultaneous users.
**Environment:** aiohttp-based backends; high-concurrency servers handling many simultaneous streaming sessions.

---

### Option 3 — Flask SSE with generator and context preservation

```python
from flask import Flask, Response, request, stream_with_context
import anthropic
import json

app = Flask(__name__)
client = anthropic.Anthropic()

def sse_generator(user_message: str):
    """SSE generator compatible with Flask's stream_with_context."""
    # Send a comment to establish connection immediately
    yield ": connected\n\n"

    try:
        with client.messages.stream(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            messages=[{"role": "user", "content": user_message}],
        ) as stream:
            for text_delta in stream.text_stream:
                data = json.dumps({"text": text_delta})
                yield f"data: {data}\n\n"

            final = stream.get_final_message()
            yield f"data: {json.dumps({'done': True, 'stop_reason': final.stop_reason})}\n\n"
    except GeneratorExit:
        # Client disconnected — generator is garbage-collected
        pass
    except Exception as exc:
        yield f"data: {json.dumps({'error': str(exc)})}\n\n"

@app.route("/stream")
def stream():
    message = request.args.get("message", "Tell me something interesting.")
    return Response(
        stream_with_context(sse_generator(message)),
        mimetype="text/event-stream",
        headers={
            "Cache-Control":   "no-cache",
            "X-Accel-Buffering": "no",
            "Access-Control-Allow-Origin": "*",
        },
    )

if __name__ == "__main__":
    # Use threaded=True or a production WSGI server (gunicorn --worker-class gevent)
    app.run(debug=False, threaded=True, port=5000)
```

**Expected Token Savings:** Flask streams tokens as they arrive; `stream_with_context` prevents context teardown mid-stream; comment ping establishes connection before the model starts generating.
**Environment:** Flask-based backends; teams already using Flask who need minimal-change streaming addition.

---

### Option 4 — Named SSE events with event type routing on the frontend

```python
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
import anthropic
import json
import time

app = FastAPI()
client = anthropic.Anthropic()

def typed_sse_stream(user_message: str, session_id: str):
    """SSE with named event types for fine-grained frontend handling."""
    start = time.monotonic()

    # Named event: session start
    yield f"event: session_start\ndata: {json.dumps({'session_id': session_id})}\n\n"

    accumulated = ""
    with client.messages.stream(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        messages=[{"role": "user", "content": user_message}],
    ) as stream:
        for text_delta in stream.text_stream:
            accumulated += text_delta
            # Named event: content delta
            yield f"event: content_delta\ndata: {json.dumps({'text': text_delta})}\n\n"

        final = stream.get_final_message()
        latency = round(time.monotonic() - start, 3)

        # Named event: metadata
        yield (
            f"event: metadata\n"
            f"data: {json.dumps({'latency_s': latency, 'stop_reason': final.stop_reason, 'tokens': final.usage.output_tokens})}\n\n"
        )

        # Named event: done
        yield f"event: done\ndata: {json.dumps({'session_id': session_id})}\n\n"

@app.get("/stream/{session_id}")
def stream(session_id: str, message: str = "Hello"):
    return StreamingResponse(
        typed_sse_stream(message, session_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

# Frontend:
# es.addEventListener('content_delta', e => appendText(JSON.parse(e.data).text));
# es.addEventListener('metadata', e => showLatency(JSON.parse(e.data)));
# es.addEventListener('done', e => es.close());
```

**Expected Token Savings:** Named events let the frontend distinguish text deltas from metadata without parsing every event; session_id enables concurrent multi-stream UIs.
**Environment:** Rich frontend applications (React, Vue) with separate handlers for text, status, and metadata events; multi-chat UIs with concurrent streams.

---

### Option 5 — SSE with heartbeat to prevent proxy timeouts

```python
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
import anthropic
import json
import asyncio

app = FastAPI()
client = anthropic.AsyncAnthropic()

async def stream_with_heartbeat(user_message: str):
    """Sends SSE comment pings every 15s to keep proxies from closing idle connections."""
    HEARTBEAT_INTERVAL = 15.0
    last_ping = asyncio.get_event_loop().time()

    async def maybe_ping():
        nonlocal last_ping
        now = asyncio.get_event_loop().time()
        if now - last_ping > HEARTBEAT_INTERVAL:
            last_ping = now
            return ": heartbeat\n\n"
        return None

    async with client.messages.stream(
        model="claude-haiku-4-5-20251001",
        max_tokens=2048,
        messages=[{"role": "user", "content": user_message}],
    ) as stream:
        async for text_delta in stream.text_stream:
            ping = await maybe_ping()
            if ping:
                yield ping
            yield f"data: {json.dumps({'text': text_delta})}\n\n"

        final = await stream.get_final_message()
        yield f"data: {json.dumps({'done': True, 'stop_reason': final.stop_reason})}\n\n"

@app.get("/stream")
async def stream(message: str = "Write a long story."):
    return StreamingResponse(
        stream_with_heartbeat(message),
        media_type="text/event-stream",
        headers={
            "Cache-Control":     "no-cache",
            "X-Accel-Buffering": "no",
            "Connection":        "keep-alive",
        },
    )
```

**Expected Token Savings:** Heartbeat comments prevent proxy/load-balancer timeout disconnections on long generations; without this, a 60-second response gets cut at the proxy's idle timeout.
**Environment:** Agents deployed behind nginx, AWS ALB, or Cloudflare where default idle timeouts (60–120s) are shorter than generation time for long outputs.

---

### Option 6 — SSE with client disconnect detection and cancellation

```python
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
import anthropic
import asyncio
import json

app = FastAPI()
client = anthropic.AsyncAnthropic()

async def cancellable_stream(request: Request, user_message: str):
    """Stop generation if the client disconnects (saves tokens on abandoned requests)."""
    stop_event = asyncio.Event()

    async def watch_disconnect():
        await request.is_disconnected()
        stop_event.set()

    watcher = asyncio.create_task(watch_disconnect())

    try:
        async with client.messages.stream(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            messages=[{"role": "user", "content": user_message}],
        ) as stream:
            async for text_delta in stream.text_stream:
                if stop_event.is_set():
                    print("[sse] client disconnected — stopping generation")
                    # SDK stream auto-cancels on context manager exit
                    return
                yield f"data: {json.dumps({'text': text_delta})}\n\n"

            final = await stream.get_final_message()
            yield f"data: {json.dumps({'done': True, 'tokens': final.usage.output_tokens})}\n\n"
    finally:
        watcher.cancel()

@app.get("/stream")
async def stream(request: Request, message: str = "Tell me about machine learning."):
    return StreamingResponse(
        cancellable_stream(request, message),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
```

**Expected Token Savings:** Cancelling on disconnect stops the Anthropic API call mid-stream, saving all remaining output tokens for that request; critical for chat UIs where users frequently navigate away.
**Environment:** Production chat applications; any UI where users can close the page, navigate away, or cancel a response mid-generation.

---

## Comparison

| Option | Framework | Async | Heartbeat | Client Disconnect | Named Events | Best For |
|---|---|---|---|---|---|---|
| 1. FastAPI StreamingResponse | FastAPI | No (sync gen) | No | No | No | Simple FastAPI SSE baseline |
| 2. aiohttp StreamResponse | aiohttp | Yes | No | No | No | High-concurrency async servers |
| 3. Flask stream_with_context | Flask | No | Comment ping | GeneratorExit | No | Existing Flask backends |
| 4. Named SSE events | FastAPI | No | No | No | Yes | Rich frontend with event routing |
| 5. Heartbeat pings | FastAPI | Yes | Yes | No | No | Long generations behind proxies |
| 6. Disconnect cancellation | FastAPI | Yes | No | Yes | No | Production chat; token-cost-sensitive |
