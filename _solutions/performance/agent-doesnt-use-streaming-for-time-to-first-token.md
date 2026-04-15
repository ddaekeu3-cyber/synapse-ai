---
layout: solution
title: "Agent doesn't use streaming for time-to-first-token"
category: performance
description: "Agent waits for the entire model response before displaying or processing anything. Users stare at a blank screen for 5–30 seconds while the full response generates. Streaming delivers the first tokens in under a second and makes the agent feel dramatically faster."
tags: [performance, streaming, time-to-first-token, latency, user-experience, asyncio]
---

## Symptom

The agent calls `client.messages.create()` and blocks until the entire response is complete. For a 500-token response at typical generation speed (~40 tokens/second), the user waits 12+ seconds for anything to appear. The UI shows a spinner. Users report the agent "feels slow" even when total latency is acceptable.

## Root Cause

`client.messages.create()` is a blocking call that returns only after the model finishes generating the complete response. Streaming is available via `client.messages.stream()` but requires a small code change. Without streaming, time-to-first-token (TTFT) equals total generation time.

## Fix

Use the streaming API. The first tokens arrive within the same round-trip latency as a non-streaming call — typically under 1 second — but the user sees text appearing immediately rather than all at once.

---

### Option 1 — Basic streaming with print-as-you-go

```python
import anthropic

client = anthropic.Anthropic(api_key="sk-live-...")


def run_agent_streaming(user_message: str) -> str:
    """Stream response tokens to stdout as they arrive."""
    full_text = ""

    with client.messages.stream(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[{"role": "user", "content": user_message}],
    ) as stream:
        for text in stream.text_stream:
            print(text, end="", flush=True)
            full_text += text

    print()  # newline after stream ends
    return full_text


# BEFORE (blocking — user waits for full response):
# response = client.messages.create(model="claude-sonnet-4-6", max_tokens=1024, messages=[...])
# print(response.content[0].text)

# AFTER (streaming — first tokens arrive in <1s):
result = run_agent_streaming("Explain the difference between TCP and UDP in detail.")
```

**Expected Token Savings:** None — same tokens, same cost; but perceived latency drops by 80–95 % for long responses; user sees text in <1s instead of 10–30s.
**Environment:** Any CLI or terminal agent; the one-line change from `messages.create` to `messages.stream` delivers the largest UX improvement per line of code.

---

### Option 2 — Async streaming for concurrent request handling

```python
import anthropic
import asyncio
import time

async_client = anthropic.AsyncAnthropic(api_key="sk-live-...")


async def run_agent_async_stream(
    user_message: str,
    on_token: callable | None = None,
) -> str:
    """Async streaming — other coroutines run while waiting for tokens."""
    full_text = ""
    first_token_time: float | None = None
    start_time = time.perf_counter()

    async with async_client.messages.stream(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[{"role": "user", "content": user_message}],
    ) as stream:
        async for text in stream.text_stream:
            if first_token_time is None:
                first_token_time = time.perf_counter()
                ttft = first_token_time - start_time
                print(f"\n[TTFT: {ttft:.2f}s]", flush=True)

            if on_token:
                on_token(text)
            else:
                print(text, end="", flush=True)
            full_text += text

    total = time.perf_counter() - start_time
    print(f"\n[Total: {total:.2f}s, {len(full_text)} chars]")
    return full_text


async def run_concurrent(messages: list[str]) -> list[str]:
    """Stream multiple requests concurrently."""
    return await asyncio.gather(*[run_agent_async_stream(m) for m in messages])


asyncio.run(run_concurrent([
    "Explain quantum entanglement.",
    "How does HTTPS work?",
]))
```

**Expected Token Savings:** None on cost; concurrent streaming means N requests complete in max(latency_1, latency_2, ...) instead of sum(latencies).
**Environment:** Async servers handling multiple concurrent streaming requests; the async stream yields control to the event loop between tokens.

---

### Option 3 — Server-sent events (SSE) forwarding for web UIs

```python
import anthropic
from typing import Generator

client = anthropic.Anthropic(api_key="sk-live-...")


def stream_to_sse(user_message: str) -> Generator[str, None, None]:
    """
    Yield Server-Sent Events from the model stream.
    For use with Flask/FastAPI streaming endpoints.
    """
    with client.messages.stream(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[{"role": "user", "content": user_message}],
    ) as stream:
        for text in stream.text_stream:
            # SSE format: "data: <content>\n\n"
            escaped = text.replace("\n", "\\n")
            yield f"data: {escaped}\n\n"

    yield "data: [DONE]\n\n"


# Flask example:
# from flask import Flask, Response, request
# app = Flask(__name__)
#
# @app.route("/stream")
# def stream():
#     message = request.args.get("message", "Hello")
#     return Response(
#         stream_to_sse(message),
#         mimetype="text/event-stream",
#         headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
#     )

# FastAPI example:
# from fastapi import FastAPI
# from fastapi.responses import StreamingResponse
# app = FastAPI()
#
# @app.get("/stream")
# async def stream(message: str):
#     return StreamingResponse(
#         stream_to_sse(message),
#         media_type="text/event-stream",
#     )

# Direct usage:
for event in stream_to_sse("List 5 programming languages and their main use cases."):
    if event != "data: [DONE]\n\n":
        print(event.strip())
```

**Expected Token Savings:** None — same tokens; SSE forwarding delivers streaming to browser clients, enabling real-time typewriter-style UX without buffering the full response.
**Environment:** Web applications serving LLM responses to browser clients; the SSE format is natively supported by browser `EventSource` API.

---

### Option 4 — Streaming with tool use: handle tool calls mid-stream

```python
import anthropic

client = anthropic.Anthropic(api_key="sk-live-...")

TOOLS = [
    {
        "name": "get_weather",
        "description": "Get current weather for a city.",
        "input_schema": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    }
]


def fake_get_weather(city: str) -> str:
    return f"Weather in {city}: 22°C, partly cloudy"


def run_agent_with_tools_streaming(user_message: str) -> str:
    messages: list[dict] = [{"role": "user", "content": user_message}]

    for _ in range(5):
        print("\n[Streaming response...]")
        full_text = ""
        tool_uses = []

        with client.messages.stream(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            tools=TOOLS,
            messages=messages,
        ) as stream:
            # Stream text tokens as they arrive
            for event in stream:
                if hasattr(event, "type"):
                    if event.type == "content_block_delta" and hasattr(event.delta, "text"):
                        print(event.delta.text, end="", flush=True)
                        full_text += event.delta.text

            # Get the final message for tool use blocks
            final_message = stream.get_final_message()

        if final_message.stop_reason == "end_turn":
            print()
            return full_text

        if final_message.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": final_message.content})
            results = []
            for block in final_message.content:
                if block.type == "tool_use":
                    print(f"\n[Tool call: {block.name}({block.input})]")
                    result = fake_get_weather(**block.input)
                    print(f"[Tool result: {result}]")
                    results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })
            messages.append({"role": "user", "content": results})

    return full_text
```

**Expected Token Savings:** None on cost; streaming with tool use delivers the text portions of the response immediately while tool calls are being executed, improving perceived responsiveness.
**Environment:** Tool-using agents with a mix of text generation and tool calls; text streams immediately, tool calls interrupt the stream briefly.

---

### Option 5 — Streaming with input validation and early abort

```python
import anthropic
import asyncio

async_client = anthropic.AsyncAnthropic(api_key="sk-live-...")

ABORT_PHRASES = ["i cannot", "i can't help", "as an ai", "i'm not able to"]
MAX_OUTPUT_CHARS = 3000


async def run_agent_with_abort(user_message: str) -> tuple[str, str]:
    """
    Stream response, but abort early if:
    1. A refusal phrase is detected in the first 100 tokens
    2. Output exceeds the character budget

    Returns (text, status) where status is 'ok', 'refusal', or 'truncated'.
    """
    accumulated = ""
    refusal_checked = False

    async with async_client.messages.stream(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        messages=[{"role": "user", "content": user_message}],
    ) as stream:
        async for text in stream.text_stream:
            accumulated += text
            print(text, end="", flush=True)

            # Check for refusal in first 200 chars
            if not refusal_checked and len(accumulated) > 200:
                refusal_checked = True
                lower = accumulated.lower()
                if any(p in lower for p in ABORT_PHRASES):
                    print("\n[Refusal detected — aborting stream]")
                    return accumulated, "refusal"

            # Abort if output budget exceeded
            if len(accumulated) > MAX_OUTPUT_CHARS:
                print(f"\n[Output budget ({MAX_OUTPUT_CHARS} chars) exceeded — truncating]")
                return accumulated[:MAX_OUTPUT_CHARS], "truncated"

    return accumulated, "ok"


async def main() -> None:
    text, status = await run_agent_with_abort("Explain the history of computing in detail.")
    print(f"\n\nStatus: {status}, Length: {len(text)}")


asyncio.run(main())
```

**Expected Token Savings:** Early abort on refusal stops generation after ~50 tokens instead of completing a full 300-token refusal response; budget enforcement prevents runaway long responses.
**Environment:** Async agents with output quality gates; streaming enables early termination that non-streaming cannot provide.

---

### Option 6 — Streaming with live token budget display

```python
import anthropic
import time

client = anthropic.Anthropic(api_key="sk-live-...")

TOKEN_BUDGET = 500   # approximate; ~4 chars per token


def run_agent_with_budget_display(user_message: str) -> str:
    """
    Stream with a live token usage indicator.
    Shows progress: [████░░░░ 62%] as tokens generate.
    """
    start_time = time.perf_counter()
    accumulated = ""
    tokens_estimate = 0

    def progress_bar(pct: float, width: int = 20) -> str:
        filled = int(width * pct / 100)
        return f"[{'█' * filled}{'░' * (width - filled)} {pct:.0f}%]"

    with client.messages.stream(
        model="claude-sonnet-4-6",
        max_tokens=TOKEN_BUDGET,
        messages=[{"role": "user", "content": user_message}],
    ) as stream:
        for text in stream.text_stream:
            accumulated += text
            tokens_estimate = len(accumulated) // 4
            pct = min(tokens_estimate / TOKEN_BUDGET * 100, 100)

            # Update progress on the same line (terminal)
            bar = progress_bar(pct)
            print(f"\r{bar} ~{tokens_estimate}/{TOKEN_BUDGET} tok", end="", flush=True)

        # Move to next line and print final message
        final = stream.get_final_message()
        actual_tokens = final.usage.output_tokens
        elapsed = time.perf_counter() - start_time
        tokens_per_sec = actual_tokens / max(elapsed, 0.001)
        print(f"\r{'[' + '█' * 20 + ']':>24} {actual_tokens} tokens in {elapsed:.1f}s ({tokens_per_sec:.0f} tok/s)")

    return accumulated


# Comparison table
# | Option | Streaming Use Case | Extra Complexity | Key Benefit |
# |--------|------------------|-----------------|-------------|
# | 1 Basic stdout | CLI output | Minimal | TTFT <1s |
# | 2 Async concurrent | Multi-request | asyncio | Parallel TTFT |
# | 3 SSE forwarding | Web UI | HTTP layer | Browser streaming |
# | 4 Tool use stream | Agent with tools | Tool loop | Text streams between tool calls |
# | 5 Early abort | Quality gating | Abort logic | Stop on refusal/budget |
# | 6 Budget display | Developer UX | Progress UI | Live token visibility |

result = run_agent_with_budget_display("Explain the CAP theorem with examples.")
```

**Expected Token Savings:** None on cost; the budget display makes token consumption visible in real-time, helping developers tune `max_tokens` appropriately and catch unexpectedly long responses before they complete.
**Environment:** Developer-facing tools and debugging sessions; the progress bar communicates that the agent is working without requiring a separate loading indicator.
