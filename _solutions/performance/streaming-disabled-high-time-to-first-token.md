---
layout: solution
title: "High Time to First Token — Agent Waits for Full Response Before Displaying Anything"
category: performance
description: "Agent appears unresponsive for 10–30 seconds while generating a response, then shows everything at once. Streaming is disabled. Enable streaming to show output as it's generated."
tags: [streaming, ttft, latency, user-experience, performance]
---

# High Time to First Token — Agent Waits for Full Response Before Displaying Anything

## Symptom
- Agent says nothing for 15–30 seconds, then displays the full response instantly
- Users think the agent is broken or frozen
- Short responses (5 words) appear just as fast as long responses — no proportional delay
- Chat interface shows loading spinner for the entire generation time
- Switching to a different model doesn't help — the delay is the same regardless

## Root Cause
Streaming is disabled. The agent waits for the API to complete the entire response before returning it, even if the first tokens were ready 100ms after the request was made. With streaming enabled, first tokens typically appear in <1 second.

## Fix

### Option 1: Anthropic SDK — enable streaming

```python
from anthropic import Anthropic

client = Anthropic()

# SLOW — waits for full response
response = client.messages.create(
    model="claude-sonnet-4-6",
    max_tokens=1024,
    messages=[{"role": "user", "content": user_message}]
)
print(response.content[0].text)

# FAST — streams as generated
with client.messages.stream(
    model="claude-sonnet-4-6",
    max_tokens=1024,
    messages=[{"role": "user", "content": user_message}]
) as stream:
    for text in stream.text_stream:
        print(text, end="", flush=True)
```

### Option 2: Async streaming for web applications

```python
from anthropic import AsyncAnthropic
from fastapi import FastAPI
from fastapi.responses import StreamingResponse

client = AsyncAnthropic()
app = FastAPI()

@app.post("/chat")
async def chat(message: str):
    async def generate():
        async with client.messages.stream(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            messages=[{"role": "user", "content": message}]
        ) as stream:
            async for text in stream.text_stream:
                yield f"data: {text}\n\n"  # Server-Sent Events format
        yield "data: [DONE]\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")
```

### Option 3: OpenClaw config — enable streaming

```yaml
# openclaw.config.yaml
providers:
  anthropic:
    streaming: true
    stream_buffer_ms: 0    # Flush immediately (don't buffer)
```

### Option 4: Telegram streaming simulation

Telegram doesn't support true streaming, but you can send partial responses:

```python
from telegram import Bot
import asyncio

async def stream_to_telegram(chat_id, message, bot: Bot):
    """Send response in chunks as it's generated"""
    msg = await bot.send_message(chat_id, "▋")  # Typing cursor

    accumulated = ""
    last_update = ""

    async with client.messages.stream(...) as stream:
        async for chunk in stream.text_stream:
            accumulated += chunk

            # Update message every 500ms to avoid rate limits
            if len(accumulated) - len(last_update) > 50:
                await msg.edit_text(accumulated + "▋")
                last_update = accumulated
                await asyncio.sleep(0.5)

    await msg.edit_text(accumulated)  # Final message without cursor
```

## Performance Comparison

| Mode | Short response (50 tokens) | Long response (500 tokens) |
|------|--------------------------|---------------------------|
| No streaming | 3s wait → instant display | 15s wait → instant display |
| Streaming | <1s first token → 2s total | <1s first token → 12s total |

Perceived latency drops from 3–15s to <1s with streaming.

## Expected Token Savings
Token cost is identical with or without streaming.
User experience improvement: massive — eliminates "is it broken?" problem.

## Environment
- Any web-facing agent interface
- Telegram bots (use edit-based partial updates)
- Source: Anthropic API documentation, direct experience
