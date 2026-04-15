---
layout: solution
title: "Agent Doesn't Implement Streaming With Tool Use Responses"
category: streaming
description: "When tool calls are involved, the agent abandons streaming and falls back to batch mode, forcing users to wait for the entire tool execution and follow-up generation before seeing any output."
tags: [streaming, tool-use, ux, latency, production]
---

## Symptom

The agent streams the first model response, but when a tool call is detected, it switches to synchronous mode: it stops streaming, executes the tool, waits for the full second model response, then returns everything at once. The user sees partial text → blank pause (tool execution) → sudden full dump. Or worse, the developer never considered streaming in the tool-use path and the tool-call branch is entirely non-streaming.

## Root Cause

The Anthropic streaming API delivers content blocks incrementally, including `tool_use` blocks. When `stop_reason == "tool_use"`, the stream ends and the agent must execute tools then start a new streaming request. The key insight is that the **second** model call — the one with the tool result injected — can also be streamed. Streaming that second call gives users immediate feedback during what is otherwise a silent wait period.

## Fix

### Option 1 — Stream both turns: initial response and post-tool response

```python
import anthropic
import json

client = anthropic.Anthropic()

CALCULATOR_TOOL = {
    "name": "calculate",
    "description": "Evaluate arithmetic.",
    "input_schema": {
        "type": "object",
        "properties": {"expression": {"type": "string"}},
        "required": ["expression"],
    },
}

def execute_tool(name: str, tool_input: dict) -> str:
    if name == "calculate":
        try:
            return str(eval(tool_input["expression"], {"__builtins__": {}}))
        except Exception as e:
            return f"error: {e}"
    return f"unknown tool: {name}"

def stream_agent(user_query: str) -> str:
    messages  = [{"role": "user", "content": user_query}]
    full_text = ""

    for turn in range(4):
        accumulated_blocks = []
        tool_calls         = []

        with client.messages.stream(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=[CALCULATOR_TOOL],
            messages=messages,
        ) as stream:
            # Stream text as it arrives
            for event in stream:
                if hasattr(event, "type"):
                    if event.type == "content_block_delta":
                        delta = event.delta
                        if hasattr(delta, "text"):
                            full_text += delta.text
                            print(delta.text, end="", flush=True)

            final  = stream.get_final_message()
            reason = final.stop_reason

        # Collect tool calls from final message
        for block in final.content:
            if block.type == "tool_use":
                tool_calls.append(block)

        if not tool_calls:
            break  # done — no more tool calls

        # Execute tools
        tool_results = []
        for tc in tool_calls:
            result = execute_tool(tc.name, tc.input)
            print(f"\n[tool:{tc.name}({tc.input})] → {result}")
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tc.id,
                "content": result,
            })

        # Inject tool results and continue streaming
        messages.append({"role": "assistant", "content": final.content})
        messages.append({"role": "user",      "content": tool_results})

    print()
    return full_text

result = stream_agent("What is (123 * 456) + (789 / 3)?")
print(f"\n[done] {len(result)} chars")
```

**Expected Token Savings:** Streaming the post-tool response reduces perceived latency significantly; users see the answer appearing rather than a full blank wait = fewer "is it broken?" retries.
**Environment:** Any agent with tools; the pattern applies to any tool type — search, database, calculator.

---

### Option 2 — Async streaming agent with parallel tool execution

```python
import asyncio
import anthropic

client = anthropic.AsyncAnthropic()

TOOLS = [
    {
        "name": "search",
        "description": "Search for information.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
    {
        "name": "get_price",
        "description": "Get current price of a product.",
        "input_schema": {
            "type": "object",
            "properties": {"product": {"type": "string"}},
            "required": ["product"],
        },
    },
]

async def execute_tool_async(name: str, tool_input: dict) -> str:
    await asyncio.sleep(0.05)  # simulate async tool I/O
    if name == "search":
        return f"Search results for '{tool_input['query']}': [result1, result2]"
    if name == "get_price":
        return f"Price of {tool_input['product']}: $42.99"
    return "unknown tool"

async def stream_agent_async(user_query: str) -> str:
    messages  = [{"role": "user", "content": user_query}]
    full_text = ""

    for _ in range(5):
        tool_calls = []

        async with client.messages.stream(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=TOOLS,
            messages=messages,
        ) as stream:
            async for event in stream:
                if hasattr(event, "type") and event.type == "content_block_delta":
                    if hasattr(event.delta, "text"):
                        full_text += event.delta.text
                        print(event.delta.text, end="", flush=True)

            final = await stream.get_final_message()

        for block in final.content:
            if block.type == "tool_use":
                tool_calls.append(block)

        if not tool_calls:
            break

        # Execute all tool calls in parallel
        results = await asyncio.gather(*[
            execute_tool_async(tc.name, tc.input) for tc in tool_calls
        ])

        tool_result_blocks = [
            {"type": "tool_result", "tool_use_id": tc.id, "content": result}
            for tc, result in zip(tool_calls, results)
        ]

        print(f"\n[tools] executed {len(tool_calls)} in parallel")
        messages.append({"role": "assistant", "content": final.content})
        messages.append({"role": "user",      "content": tool_result_blocks})

    print()
    return full_text

asyncio.run(stream_agent_async("Search for Python tips and get the price of a Python book."))
```

**Expected Token Savings:** Parallel async tool execution reduces the tool-execution gap between streams; multiple tools fire simultaneously so the second stream starts sooner.
**Environment:** Async agents with multiple tools per turn; FastAPI backends handling concurrent user sessions.

---

### Option 3 — Progressive streaming status updates during tool execution

```python
import anthropic
import time

client = anthropic.Anthropic()

WEATHER_TOOL = {
    "name": "get_weather",
    "description": "Get weather for a city.",
    "input_schema": {
        "type": "object",
        "properties": {"city": {"type": "string"}},
        "required": ["city"],
    },
}

def get_weather(city: str) -> str:
    time.sleep(0.5)  # simulate slow API call
    return f"Sunny, 22°C, humidity 45% in {city}"

def stream_with_status(user_query: str):
    """Yields (type, content) tuples: ('text', delta), ('status', msg), ('done', '')"""
    messages = [{"role": "user", "content": user_query}]

    for _ in range(4):
        tool_calls = []

        with client.messages.stream(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            tools=[WEATHER_TOOL],
            messages=messages,
        ) as stream:
            for event in stream:
                if hasattr(event, "type") and event.type == "content_block_delta":
                    if hasattr(event.delta, "text"):
                        yield ("text", event.delta.text)
            final = stream.get_final_message()

        for block in final.content:
            if block.type == "tool_use":
                tool_calls.append(block)

        if not tool_calls:
            yield ("done", "")
            return

        # Emit status while tools are running
        for tc in tool_calls:
            yield ("status", f"Fetching weather for {tc.input.get('city', '?')}...")
            result = get_weather(tc.input["city"])
            yield ("status", "Got weather data — generating response...")
            messages.append({"role": "assistant", "content": final.content})
            messages.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": tc.id, "content": result}
            ]})

    yield ("done", "")

# Consumer (frontend would handle these event types differently)
for event_type, content in stream_with_status("What's the weather in Paris and Tokyo?"):
    if event_type == "text":
        print(content, end="", flush=True)
    elif event_type == "status":
        print(f"\n⟳ {content}", end="", flush=True)
    elif event_type == "done":
        print("\n[done]")
```

**Expected Token Savings:** Status events keep users engaged during tool execution; engaged users wait longer before abandoning, reducing "retry storms" that double token costs.
**Environment:** Chat UIs with visible tool status indicators; voice agents that need filler phrases during tool execution.

---

### Option 4 — SSE endpoint streaming tool use for browser clients

```python
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
import anthropic
import json
import asyncio

app = FastAPI()
client = anthropic.AsyncAnthropic()

TOOLS = [{
    "name": "lookup",
    "description": "Look up a fact.",
    "input_schema": {
        "type": "object",
        "properties": {"topic": {"type": "string"}},
        "required": ["topic"],
    },
}]

async def execute_lookup(topic: str) -> str:
    await asyncio.sleep(0.1)
    return f"Fact about {topic}: [retrieved data]"

async def stream_tool_use_sse(user_message: str):
    messages = [{"role": "user", "content": user_message}]

    for turn in range(4):
        tool_calls = []

        async with client.messages.stream(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            tools=TOOLS,
            messages=messages,
        ) as stream:
            async for event in stream:
                if hasattr(event, "type") and event.type == "content_block_delta":
                    if hasattr(event.delta, "text"):
                        payload = json.dumps({"type": "delta", "text": event.delta.text})
                        yield f"data: {payload}\n\n"

            final = await stream.get_final_message()

        for block in final.content:
            if block.type == "tool_use":
                tool_calls.append(block)

        if not tool_calls:
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
            return

        # Notify client about tool execution
        for tc in tool_calls:
            yield f"data: {json.dumps({'type': 'tool_start', 'tool': tc.name, 'input': tc.input})}\n\n"
            result = await execute_lookup(tc.input["topic"])
            yield f"data: {json.dumps({'type': 'tool_end', 'result': result[:50]})}\n\n"
            messages.append({"role": "assistant", "content": final.content})
            messages.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": tc.id, "content": result}
            ]})

@app.get("/chat")
async def chat(message: str):
    return StreamingResponse(
        stream_tool_use_sse(message),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

# Frontend events:
# es.addEventListener('message', e => {
#   const d = JSON.parse(e.data);
#   if (d.type === 'delta') appendText(d.text);
#   if (d.type === 'tool_start') showSpinner(d.tool, d.input);
#   if (d.type === 'tool_end') hideSpinner();
#   if (d.type === 'done') es.close();
# });
```

**Expected Token Savings:** Tool events (tool_start/tool_end) let the UI show a spinner during execution rather than a blank pause; better UX without additional tokens.
**Environment:** FastAPI backends serving browser chat UIs; any SSE streaming endpoint that needs to communicate tool execution state to the frontend.

---

### Option 5 — Streaming with extended thinking and tool use combined

```python
import anthropic

client = anthropic.Anthropic()

MATH_TOOL = {
    "name": "calculate",
    "description": "Evaluate math expressions precisely.",
    "input_schema": {
        "type": "object",
        "properties": {"expression": {"type": "string"}},
        "required": ["expression"],
    },
}

def stream_with_thinking_and_tools(problem: str) -> str:
    messages  = [{"role": "user", "content": problem}]
    full_text = ""
    thinking_shown = False

    for _ in range(4):
        tool_calls = []

        with client.messages.stream(
            model="claude-sonnet-4-6",
            max_tokens=8000,
            thinking={"type": "enabled", "budget_tokens": 2000},
            tools=[MATH_TOOL],
            messages=messages,
        ) as stream:
            for event in stream:
                if not hasattr(event, "type"):
                    continue
                if event.type == "content_block_start":
                    block = event.content_block
                    if block.type == "thinking" and not thinking_shown:
                        print("[thinking...] ", end="", flush=True)
                        thinking_shown = True
                elif event.type == "content_block_delta":
                    delta = event.delta
                    if hasattr(delta, "text"):
                        full_text += delta.text
                        print(delta.text, end="", flush=True)
                    elif hasattr(delta, "thinking"):
                        pass  # don't stream thinking to user
                elif event.type == "content_block_stop":
                    if thinking_shown:
                        print(" [done thinking]", flush=True)
                        thinking_shown = False

            final = stream.get_final_message()

        for block in final.content:
            if block.type == "tool_use":
                tool_calls.append(block)

        if not tool_calls:
            break

        for tc in tool_calls:
            try:
                result = str(eval(tc.input["expression"], {"__builtins__": {}}))
            except Exception as e:
                result = f"error: {e}"
            print(f"\n[calc] {tc.input['expression']} = {result}")
            messages.append({"role": "assistant", "content": final.content})
            messages.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": tc.id, "content": result}
            ]})

    print()
    return full_text

result = stream_with_thinking_and_tools(
    "What is the sum of the first 100 prime numbers? Use the calculator for each prime check."
)
```

**Expected Token Savings:** Extended thinking budget tokens are used only for the reasoning phase, not streamed as output tokens; streaming the final answer provides immediacy while thinking runs in background.
**Environment:** Complex reasoning agents using extended thinking + tools; math/logic agents where thinking quality justifies the budget but users still want progressive output.

---

### Option 6 — Resumable streaming agent with checkpoint on tool boundaries

```python
import anthropic
import json
import os

client = anthropic.Anthropic()

CHECKPOINT_PATH = "/tmp/stream_tool_checkpoint.json"

SEARCH_TOOL = {
    "name": "search",
    "description": "Search for information.",
    "input_schema": {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    },
}

def search(query: str) -> str:
    return f"Results for '{query}': [data1, data2, data3]"

def save_checkpoint(messages: list, text_so_far: str) -> None:
    tmp = CHECKPOINT_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"messages": messages, "text": text_so_far}, f)
    os.replace(tmp, CHECKPOINT_PATH)

def load_checkpoint() -> tuple[list, str] | None:
    if os.path.exists(CHECKPOINT_PATH):
        with open(CHECKPOINT_PATH) as f:
            data = json.load(f)
        print(f"[checkpoint] resuming: {len(data['messages'])} messages, {len(data['text'])} chars")
        return data["messages"], data["text"]
    return None

def stream_resumable(user_query: str) -> str:
    # Try to resume from checkpoint
    checkpoint = load_checkpoint()
    if checkpoint:
        messages, full_text = checkpoint
        print(f"[resume] replaying {len(full_text)} chars of prior output")
        print(full_text, end="")
    else:
        messages  = [{"role": "user", "content": user_query}]
        full_text = ""

    for _ in range(5):
        tool_calls = []

        try:
            with client.messages.stream(
                model="claude-haiku-4-5-20251001",
                max_tokens=512,
                tools=[SEARCH_TOOL],
                messages=messages,
            ) as stream:
                for event in stream:
                    if hasattr(event, "type") and event.type == "content_block_delta":
                        if hasattr(event.delta, "text"):
                            full_text += event.delta.text
                            print(event.delta.text, end="", flush=True)
                final = stream.get_final_message()

        except (anthropic.APIConnectionError, anthropic.APITimeoutError):
            save_checkpoint(messages, full_text)
            print(f"\n[checkpoint] saved at {len(full_text)} chars — retry to resume")
            return full_text

        for block in final.content:
            if block.type == "tool_use":
                tool_calls.append(block)

        if not tool_calls:
            # Clean up checkpoint on successful completion
            if os.path.exists(CHECKPOINT_PATH):
                os.remove(CHECKPOINT_PATH)
            break

        for tc in tool_calls:
            result = search(tc.input["query"])
            messages.append({"role": "assistant", "content": final.content})
            messages.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": tc.id, "content": result}
            ]})
            # Checkpoint after each tool result so we can resume from here
            save_checkpoint(messages, full_text)

    print()
    return full_text

stream_resumable("Search for Python best practices and summarise the findings.")
```

**Expected Token Savings:** Checkpointing at tool boundaries saves conversation history; on network failure, the resumed run replays accumulated messages rather than restarting from scratch — saving all prior input tokens.
**Environment:** Long multi-tool conversations in batch or cron environments; agents handling expensive multi-step research that must survive network interruptions.

---

## Comparison

| Option | Streaming Coverage | Tool Execution | Status Events | Resumable | Best For |
|---|---|---|---|---|---|
| 1. Both turns streamed | Turn 1 + Turn 2 | Sequential | No | No | Baseline two-turn tool streaming |
| 2. Async + parallel tools | Both turns | Parallel (async) | No | No | Async agents with concurrent tools |
| 3. Status events | Both turns | Sequential | Yes | No | Chat UIs with tool status indicators |
| 4. SSE frontend events | Both turns | Sequential | Yes (SSE) | No | Browser clients with spinner UI |
| 5. Thinking + tools | Both turns (thinking) | Sequential | Thinking shown | No | Extended thinking + tool agents |
| 6. Checkpoint on tool boundary | Both turns | Sequential | No | Yes | Long batch agents with crash recovery |
