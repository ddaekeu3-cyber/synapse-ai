---
layout: solution
title: "Agent Doesn't Stream Large Responses"
category: performance
description: "Agent waits for the entire response to generate before returning anything to the user — causing 10–30 second perceived wait times for long outputs when streaming would show the first tokens within milliseconds."
tags: [performance, streaming, latency, ux, time-to-first-token, async]
---

## Symptom

User asks for a detailed report or long explanation. The UI shows a spinner for 15–25 seconds, then the full response appears at once. With streaming, the first sentence would appear within 300ms and content would flow continuously.

Time-to-first-token without streaming: **15–25 seconds**
Time-to-first-token with streaming: **200–500ms**

## Root Cause

The agent uses `client.messages.create()` (blocking) instead of `client.messages.stream()`. The API generates all tokens before returning. For responses of 500–2,000 tokens, this creates an unnecessarily long wait.

## Fix

---

### Option 1 — Basic Streaming with Text Iterator

Replace `messages.create()` with the streaming context manager. Tokens flow to the client as they are generated.

```python
import anthropic

client = anthropic.Anthropic()

def stream_response(user_message: str) -> str:
    """
    Stream response tokens to the user as they are generated.
    Returns the complete text when done.
    """
    print("Agent: ", end="", flush=True)
    collected = []

    with client.messages.stream(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[{"role": "user", "content": user_message}],
    ) as stream:
        for text in stream.text_stream:
            print(text, end="", flush=True)
            collected.append(text)

    print()  # Final newline
    full_response = "".join(collected)

    # Access final message metadata after stream completes
    final_message = stream.get_final_message()
    usage = final_message.usage
    print(f"\n[Usage] Input: {usage.input_tokens}, Output: {usage.output_tokens}")

    return full_response

# Compare: without streaming, user waits for entire response
def blocking_response(user_message: str) -> str:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text

import time

prompt = "Write a detailed explanation of how TCP/IP works, covering the handshake, packet routing, and error correction."

print("=== Streaming (first tokens appear immediately) ===")
start = time.monotonic()
result = stream_response(prompt)
elapsed = time.monotonic() - start
print(f"Total time: {elapsed:.2f}s | Length: {len(result.split())} words")
```

**Expected Token Savings:** None — same tokens; time-to-first-token drops from ~15s to ~300ms
**Environment:** `pip install anthropic`

---

### Option 2 — Async Streaming with Multiple Concurrent Streams

Use `AsyncAnthropic` with async streaming. Multiple user requests stream concurrently — no blocking between users.

```python
import asyncio
import time
import anthropic

async_client = anthropic.AsyncAnthropic()

async def stream_to_user(user_id: str, message: str) -> str:
    """Stream a response for one user. Non-blocking — other users stream concurrently."""
    collected = []
    first_token_time = None

    async with async_client.messages.stream(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{"role": "user", "content": message}],
    ) as stream:
        async for text in stream.text_stream:
            if first_token_time is None:
                first_token_time = time.monotonic()
                print(f"[User {user_id}] First token arrived")
            collected.append(text)

    return "".join(collected)

async def streaming_server():
    """Simulate multiple concurrent users — all stream in parallel."""
    user_requests = [
        ("Alice", "Explain neural networks in 3 paragraphs."),
        ("Bob", "What are the main benefits of Rust over C++?"),
        ("Charlie", "Describe the CAP theorem with examples."),
        ("Diana", "How does Kubernetes handle container scheduling?"),
    ]

    start = time.monotonic()

    # All streams run concurrently — no user waits for another
    tasks = [
        stream_to_user(user, msg)
        for user, msg in user_requests
    ]
    results = await asyncio.gather(*tasks)

    elapsed = time.monotonic() - start
    print(f"\n4 concurrent streams completed in {elapsed:.2f}s")
    for (user, _), result in zip(user_requests, results):
        print(f"{user}: {result[:60]}...")

asyncio.run(streaming_server())
```

**Expected Token Savings:** None — concurrent streams reduce total wall-clock time by N-fold
**Environment:** `pip install anthropic`

---

### Option 3 — Streaming with Tool Use (Agentic Loop)

Stream responses even when the agent uses tools. Stream the text portions of each turn while executing tools between turns.

```python
import asyncio
import json
import anthropic

async_client = anthropic.AsyncAnthropic()

TOOLS = [
    {
        "name": "get_weather",
        "description": "Get current weather for a city.",
        "input_schema": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    },
    {
        "name": "get_forecast",
        "description": "Get 5-day weather forecast for a city.",
        "input_schema": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    },
]

async def execute_tool(name: str, args: dict) -> str:
    await asyncio.sleep(0.05)  # Simulated async tool call
    if name == "get_weather":
        return json.dumps({"city": args["city"], "temp": 22, "condition": "sunny", "humidity": 65})
    if name == "get_forecast":
        return json.dumps({"city": args["city"], "days": [
            {"day": "Mon", "high": 23, "low": 15},
            {"day": "Tue", "high": 21, "low": 14},
            {"day": "Wed", "high": 19, "low": 13},
        ]})
    return json.dumps({"error": f"Unknown tool: {name}"})

async def streaming_agent(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]
    all_text = []

    while True:
        tool_calls_in_turn = []
        text_in_turn = []

        async with async_client.messages.stream(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            tools=TOOLS,
            messages=messages,
        ) as stream:
            # Stream text tokens as they arrive
            async for event in stream:
                if hasattr(event, "type"):
                    if event.type == "content_block_delta":
                        delta = getattr(event, "delta", None)
                        if delta and hasattr(delta, "text"):
                            print(delta.text, end="", flush=True)
                            text_in_turn.append(delta.text)

            final = await stream.get_final_message()

        all_text.extend(text_in_turn)

        if final.stop_reason == "end_turn":
            print()
            break

        # Handle tool use
        messages.append({"role": "assistant", "content": final.content})
        tool_results = []

        for block in final.content:
            if block.type == "tool_use":
                print(f"\n[Tool: {block.name}({block.input})]")
                result = await execute_tool(block.name, block.input)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })

        messages.append({"role": "user", "content": tool_results})

    return "".join(all_text)

import time
start = time.monotonic()
result = asyncio.run(streaming_agent(
    "What's the weather in Tokyo right now, and what's the 5-day forecast? "
    "Give me a detailed analysis."
))
print(f"\n[Completed in {time.monotonic() - start:.2f}s]")
```

**Expected Token Savings:** None — same tokens; streaming during tool loops reduces perceived wait
**Environment:** `pip install anthropic`

---

### Option 4 — Server-Sent Events (SSE) Adapter for Web Endpoints

Wrap async streaming in a Server-Sent Events generator for web frameworks. Clients receive tokens as SSE events — no WebSocket needed.

```python
import asyncio
import json
import anthropic
from typing import AsyncGenerator

async_client = anthropic.AsyncAnthropic()

async def sse_stream(user_message: str) -> AsyncGenerator[str, None]:
    """
    Yields SSE-formatted strings for streaming to HTTP clients.
    Compatible with EventSource in browsers.
    """
    yield "data: {}\n\n".format(json.dumps({"type": "start"}))

    collected = []

    async with async_client.messages.stream(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[{"role": "user", "content": user_message}],
    ) as stream:
        async for text in stream.text_stream:
            collected.append(text)
            # Each token as an SSE event
            yield "data: {}\n\n".format(json.dumps({"type": "token", "text": text}))

    full_text = "".join(collected)
    final_msg = stream.get_final_message()
    usage = final_msg.usage

    yield "data: {}\n\n".format(json.dumps({
        "type": "done",
        "full_text": full_text,
        "usage": {
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
        },
    }))
    yield "data: [DONE]\n\n"

# FastAPI integration example (run as reference):
# from fastapi import FastAPI
# from fastapi.responses import StreamingResponse
# app = FastAPI()
#
# @app.post("/chat")
# async def chat_endpoint(request: ChatRequest):
#     return StreamingResponse(
#         sse_stream(request.message),
#         media_type="text/event-stream",
#         headers={
#             "Cache-Control": "no-cache",
#             "X-Accel-Buffering": "no",
#         },
#     )

async def simulate_sse_client():
    print("Simulating SSE client receiving events:")
    event_count = 0
    token_count = 0

    async for event in sse_stream("List 5 programming languages and their main use cases."):
        if event.startswith("data:") and not event.strip().endswith("[DONE]"):
            data = json.loads(event.removeprefix("data:").strip())
            event_count += 1

            if data["type"] == "token":
                print(data["text"], end="", flush=True)
                token_count += 1
            elif data["type"] == "done":
                print(f"\n\n[Done: {token_count} tokens streamed in {event_count} events]")
                print(f"Usage: {data['usage']}")

asyncio.run(simulate_sse_client())
```

**Expected Token Savings:** None — same tokens; enables real-time web streaming without WebSockets
**Environment:** `pip install anthropic`

---

### Option 5 — Streaming with Progress Indicators for Tool-Heavy Agents

For agents that use many tools, show progress updates between tool calls so users know the agent is working — even during silent tool execution phases.

```python
import asyncio
import json
import time
import anthropic

async_client = anthropic.AsyncAnthropic()

class ProgressStreamer:
    def __init__(self):
        self._start_time = time.monotonic()
        self._step = 0

    def _elapsed(self) -> str:
        return f"{time.monotonic() - self._start_time:.1f}s"

    def tool_start(self, name: str, args: dict):
        self._step += 1
        print(f"\n⟳ [{self._elapsed()}] Calling {name}({list(args.keys())})...")

    def tool_done(self, name: str):
        print(f"✓ [{self._elapsed()}] {name} completed")

    def stream_chunk(self, text: str):
        print(text, end="", flush=True)

    def done(self):
        print(f"\n[Completed in {self._elapsed()}]")

TOOLS = [{
    "name": "research_topic",
    "description": "Research a topic using multiple sources.",
    "input_schema": {
        "type": "object",
        "properties": {"topic": {"type": "string"}, "depth": {"type": "string", "enum": ["brief", "detailed"]}},
        "required": ["topic"],
    },
}]

async def execute_research(topic: str, depth: str = "brief") -> dict:
    await asyncio.sleep(0.2)  # Simulate research latency
    return {
        "topic": topic,
        "summary": f"Research findings on {topic}: [detailed information would go here]",
        "sources": ["Source A", "Source B", "Source C"],
    }

async def agent_with_progress(task: str):
    progress = ProgressStreamer()
    messages = [{"role": "user", "content": task}]

    while True:
        async with async_client.messages.stream(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            tools=TOOLS,
            messages=messages,
        ) as stream:
            async for event in stream:
                if hasattr(event, "type") and event.type == "content_block_delta":
                    delta = getattr(event, "delta", None)
                    if delta and hasattr(delta, "text"):
                        progress.stream_chunk(delta.text)

            final = await stream.get_final_message()

        if final.stop_reason == "end_turn":
            progress.done()
            break

        messages.append({"role": "assistant", "content": final.content})
        tool_results = []

        for block in final.content:
            if block.type == "tool_use":
                progress.tool_start(block.name, block.input)
                result = await execute_research(**block.input)
                progress.tool_done(block.name)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result),
                })

        messages.append({"role": "user", "content": tool_results})

asyncio.run(agent_with_progress(
    "Research quantum computing and machine learning, then write a comparison."
))
```

**Expected Token Savings:** None — UX improvement; users see continuous activity instead of silence
**Environment:** `pip install anthropic`

---

### Option 6 — Streaming with Abort on User Cancellation

Allow users to cancel a streaming response mid-generation. When cancelled, close the stream immediately rather than waiting for full completion.

```python
import asyncio
import signal
import anthropic

async_client = anthropic.AsyncAnthropic()

async def cancellable_stream(
    user_message: str,
    cancel_event: asyncio.Event,
) -> tuple[str, bool]:
    """
    Stream response. If cancel_event is set, abort immediately.
    Returns (partial_text, was_cancelled).
    """
    collected = []
    cancelled = False

    async with async_client.messages.stream(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        messages=[{"role": "user", "content": user_message}],
    ) as stream:
        try:
            async for text in stream.text_stream:
                if cancel_event.is_set():
                    print("\n[Stream cancelled by user]")
                    cancelled = True
                    break
                print(text, end="", flush=True)
                collected.append(text)
        except asyncio.CancelledError:
            cancelled = True

    print()
    return "".join(collected), cancelled

async def demo_cancellable():
    cancel_event = asyncio.Event()

    # Simulate user pressing Ctrl+C after 1 second
    async def user_cancels_after_delay(seconds: float):
        await asyncio.sleep(seconds)
        print("\n[User cancelled the request]")
        cancel_event.set()

    prompt = (
        "Write a very detailed 2000-word essay on the history of the Internet, "
        "covering ARPANET, TCP/IP, the World Wide Web, and modern developments."
    )

    print("Starting stream (will be cancelled after 1s)...")
    cancel_task = asyncio.create_task(user_cancels_after_delay(1.0))

    partial_text, was_cancelled = await cancellable_stream(prompt, cancel_event)
    cancel_task.cancel()

    print(f"\nPartial response ({len(partial_text.split())} words): {partial_text[:100]}...")
    print(f"Cancelled: {was_cancelled}")

    if was_cancelled:
        print("Partial response can be used as a preview or discarded.")

asyncio.run(demo_cancellable())
```

**Expected Token Savings:** 30–80% when users cancel early — tokens after cancellation are not generated
**Environment:** `pip install anthropic`

---

## Comparison

| Option | Use Case | Complexity | Latency Reduction |
|--------|----------|------------|-------------------|
| Basic Streaming | All responses | Trivial | ~95% TTFT reduction |
| Async Concurrent | Multiple users | Low | Scales with concurrency |
| Streaming + Tools | Agentic loops | Medium | Visible progress between tools |
| SSE for Web | HTTP APIs | Medium | Real-time browser updates |
| Progress Indicators | Long tool chains | Medium | User perceived improvement |
| Cancellable Stream | User-initiated abort | Medium | 30–80% token savings on cancel |

**Recommended starting point:** Option 1 (Basic Streaming) — replace all `messages.create()` with streaming for any user-facing response. Takes 5 minutes to implement, reduces perceived latency by ~95%.
