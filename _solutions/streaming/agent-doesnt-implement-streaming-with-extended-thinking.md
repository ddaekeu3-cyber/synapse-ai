---
layout: solution
title: "Agent Doesn't Implement Streaming with Extended Thinking"
category: streaming
description: "Agents using extended thinking (Claude's internal chain-of-thought) block the user for 10-60 seconds with no output during the thinking phase. Streaming extended thinking shows thinking progress in real-time, improving perceived responsiveness and enabling partial-result handling before the final answer arrives."
tags: [streaming, extended-thinking, chain-of-thought, budget-tokens, asyncio, real-time, sse, claude-sonnet]
---

## Problem

Claude's extended thinking feature produces high-quality reasoning but generates thinking tokens before the visible response. Without streaming, users see a blank screen for the entire thinking duration. With streaming, thinking blocks arrive incrementally as `thinking` type events, allowing the UI to show "Agent is reasoning..." progress, display partial thinking for debugging, or process the final answer as soon as it appears — while thinking is still in progress.

## Solutions

### Option 1: Basic Streaming with Thinking Block Detection

```python
import anthropic

client = anthropic.Anthropic()

def stream_with_thinking(prompt: str, budget_tokens: int = 5000) -> tuple[str, str]:
    """
    Stream a response with extended thinking.
    Returns (thinking_text, response_text).
    """
    thinking_parts = []
    response_parts = []
    current_block_type = None

    with client.messages.stream(
        model="claude-sonnet-4-6",
        max_tokens=budget_tokens + 1000,
        thinking={"type": "enabled", "budget_tokens": budget_tokens},
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        for event in stream:
            event_type = type(event).__name__

            if event_type == "RawContentBlockStartEvent":
                block = event.content_block
                current_block_type = block.type
                if block.type == "thinking":
                    print("\n[Thinking...]", end="", flush=True)
                elif block.type == "text":
                    print("\n[Answer] ", end="", flush=True)

            elif event_type == "RawContentBlockDeltaEvent":
                delta = event.delta
                if hasattr(delta, "thinking") and current_block_type == "thinking":
                    thinking_parts.append(delta.thinking)
                    print(".", end="", flush=True)  # progress indicator
                elif hasattr(delta, "text") and current_block_type == "text":
                    response_parts.append(delta.text)
                    print(delta.text, end="", flush=True)

            elif event_type == "RawContentBlockStopEvent":
                if current_block_type == "thinking":
                    print(f" ({len(''.join(thinking_parts))} chars)", flush=True)
                current_block_type = None

    return "".join(thinking_parts), "".join(response_parts)

if __name__ == "__main__":
    thinking, answer = stream_with_thinking(
        "What is the sum of all integers from 1 to 100? Show your reasoning.",
        budget_tokens=3000,
    )
    print(f"\n\nThinking length: {len(thinking)} chars")
    print(f"Answer: {answer[:100]}")

# Expected Token Savings: thinking tokens are billed separately; streaming doesn't reduce thinking cost
# Environment: Claude Sonnet 4.6+ with extended thinking; budget_tokens controls reasoning depth
```

### Option 2: Async Streaming with Thinking Progress Callback

```python
import anthropic
import asyncio
import time
from typing import Callable, Awaitable

client = anthropic.AsyncAnthropic()

async def stream_thinking_with_callbacks(
    prompt: str,
    budget_tokens: int = 8000,
    on_thinking_delta: Callable[[str], Awaitable[None]] | None = None,
    on_text_delta: Callable[[str], Awaitable[None]] | None = None,
    on_thinking_complete: Callable[[str, float], Awaitable[None]] | None = None,
) -> dict:
    """
    Stream extended thinking with optional async callbacks for each event type.
    Returns full result dict with timing information.
    """
    thinking_parts = []
    response_parts = []
    thinking_start = None
    result_start = None
    current_block = None

    async with client.messages.stream(
        model="claude-sonnet-4-6",
        max_tokens=budget_tokens + 2000,
        thinking={"type": "enabled", "budget_tokens": budget_tokens},
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        async for event in stream:
            event_type = type(event).__name__

            if event_type == "RawContentBlockStartEvent":
                current_block = event.content_block.type
                if current_block == "thinking":
                    thinking_start = time.time()
                elif current_block == "text":
                    result_start = time.time()

            elif event_type == "RawContentBlockDeltaEvent":
                delta = event.delta
                if hasattr(delta, "thinking") and current_block == "thinking":
                    thinking_parts.append(delta.thinking)
                    if on_thinking_delta:
                        await on_thinking_delta(delta.thinking)
                elif hasattr(delta, "text") and current_block == "text":
                    response_parts.append(delta.text)
                    if on_text_delta:
                        await on_text_delta(delta.text)

            elif event_type == "RawContentBlockStopEvent":
                if current_block == "thinking" and on_thinking_complete and thinking_start:
                    elapsed = time.time() - thinking_start
                    await on_thinking_complete("".join(thinking_parts), elapsed)
                current_block = None

    thinking_text = "".join(thinking_parts)
    response_text = "".join(response_parts)

    return {
        "thinking": thinking_text,
        "response": response_text,
        "thinking_chars": len(thinking_text),
        "response_chars": len(response_text),
        "thinking_duration_s": (result_start - thinking_start) if thinking_start and result_start else None,
    }

async def main():
    char_count = [0]

    async def on_think(delta: str):
        char_count[0] += len(delta)

    async def on_text(delta: str):
        print(delta, end="", flush=True)

    async def on_think_done(full_thinking: str, elapsed: float):
        print(f"\n[Thinking complete: {len(full_thinking)} chars in {elapsed:.1f}s]")
        print("[Answer streaming...]")

    print("Streaming extended thinking response...")
    result = await stream_thinking_with_callbacks(
        "Explain the time complexity of quicksort in the average and worst case with reasoning.",
        budget_tokens=4000,
        on_thinking_delta=on_think,
        on_text_delta=on_text,
        on_thinking_complete=on_think_done,
    )
    print(f"\n\nFull result: thinking={result['thinking_chars']} chars, response={result['response_chars']} chars")
    if result["thinking_duration_s"]:
        print(f"Thinking phase duration: {result['thinking_duration_s']:.1f}s")

if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: callbacks enable UI to show live progress; thinking_duration reveals planning time
# Environment: async agents; on_text_delta can pipe directly to SSE endpoint for browser streaming
```

### Option 3: SSE Endpoint — Stream Thinking to Browser

```python
import anthropic
import asyncio
import json
from aiohttp import web

client = anthropic.AsyncAnthropic()

async def thinking_sse_handler(request: web.Request) -> web.StreamResponse:
    """
    SSE endpoint that streams thinking blocks and text response separately.
    Client can show "Reasoning..." spinner during thinking, then final answer.
    """
    prompt = request.rel_url.query.get("q", "Solve: what is 17 * 23?")
    budget = int(request.rel_url.query.get("budget", "3000"))

    response = web.StreamResponse(headers={
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    })
    await response.prepare(request)

    async def send_event(event_type: str, data: dict):
        line = f"event: {event_type}\ndata: {json.dumps(data)}\n\n"
        await response.write(line.encode())

    current_block = None
    thinking_chars = 0

    try:
        async with client.messages.stream(
            model="claude-sonnet-4-6",
            max_tokens=budget + 1000,
            thinking={"type": "enabled", "budget_tokens": budget},
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            async for event in stream:
                event_type = type(event).__name__

                if event_type == "RawContentBlockStartEvent":
                    current_block = event.content_block.type
                    if current_block == "thinking":
                        await send_event("thinking_start", {"budget": budget})
                    elif current_block == "text":
                        await send_event("answer_start", {"thinking_chars": thinking_chars})

                elif event_type == "RawContentBlockDeltaEvent":
                    delta = event.delta
                    if hasattr(delta, "thinking") and current_block == "thinking":
                        thinking_chars += len(delta.thinking)
                        # Send thinking progress (char count only, not content)
                        await send_event("thinking_progress", {"chars": thinking_chars})
                    elif hasattr(delta, "text") and current_block == "text":
                        await send_event("text_delta", {"text": delta.text})

                elif event_type == "RawContentBlockStopEvent":
                    if current_block == "thinking":
                        await send_event("thinking_complete", {"total_chars": thinking_chars})
                    elif current_block == "text":
                        await send_event("answer_complete", {})
                    current_block = None

    except Exception as e:
        await send_event("error", {"message": str(e)})

    await response.write(b"event: done\ndata: {}\n\n")
    return response

app = web.Application()
app.router.add_get("/think-stream", thinking_sse_handler)

# Client-side JS to consume:
# const es = new EventSource('/think-stream?q=Your+question&budget=5000');
# es.addEventListener('thinking_progress', e => showSpinner(JSON.parse(e.data).chars));
# es.addEventListener('text_delta', e => appendText(JSON.parse(e.data).text));
# es.addEventListener('answer_complete', () => es.close());

if __name__ == "__main__":
    print("Thinking SSE server starting on http://localhost:8080/think-stream")
    web.run_app(app, host="0.0.0.0", port=8080)

# Expected Token Savings: SSE sends thinking metadata (char count) not content — reduces SSE payload size
# Environment: web apps; separate thinking_start/text_delta events let frontend show different UI states
```

### Option 4: Thinking Budget Optimizer — Auto-Tune Based on Task Complexity

```python
import anthropic
import asyncio
import time

client = anthropic.AsyncAnthropic()

COMPLEXITY_BUDGETS = {
    "simple": 2000,    # arithmetic, factual lookup
    "medium": 5000,    # explanation, comparison
    "complex": 10000,  # multi-step reasoning, proofs
    "deep": 16000,     # complex analysis, strategy
}

async def classify_complexity(prompt: str) -> str:
    """Use Haiku to classify task complexity before allocating thinking budget."""
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=16,
        system="Classify complexity: simple, medium, complex, or deep. One word only.",
        messages=[{"role": "user", "content": f"Task: {prompt[:200]}"}],
    )
    raw = resp.content[0].text.strip().lower()
    for level in COMPLEXITY_BUDGETS:
        if level in raw:
            return level
    return "medium"

async def adaptive_thinking_stream(prompt: str) -> dict:
    """Classify complexity first, then stream with appropriate thinking budget."""
    complexity = await classify_complexity(prompt)
    budget = COMPLEXITY_BUDGETS[complexity]
    print(f"  [complexity={complexity}] budget={budget} tokens")

    thinking_parts = []
    response_parts = []
    current_block = None
    t0 = time.time()

    async with client.messages.stream(
        model="claude-sonnet-4-6",
        max_tokens=budget + 1000,
        thinking={"type": "enabled", "budget_tokens": budget},
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        async for event in stream:
            event_type = type(event).__name__
            if event_type == "RawContentBlockStartEvent":
                current_block = event.content_block.type
            elif event_type == "RawContentBlockDeltaEvent":
                delta = event.delta
                if hasattr(delta, "thinking") and current_block == "thinking":
                    thinking_parts.append(delta.thinking)
                elif hasattr(delta, "text") and current_block == "text":
                    response_parts.append(delta.text)
                    print(delta.text, end="", flush=True)
            elif event_type == "RawContentBlockStopEvent":
                current_block = None

    elapsed = time.time() - t0
    thinking_text = "".join(thinking_parts)
    response_text = "".join(response_parts)

    return {
        "complexity": complexity,
        "budget_used": budget,
        "thinking_chars": len(thinking_text),
        "response_chars": len(response_text),
        "elapsed_s": round(elapsed, 1),
    }

async def main():
    prompts = [
        "What is 2 + 2?",
        "Explain the difference between TCP and UDP protocols.",
        "Prove that the square root of 2 is irrational.",
    ]
    for prompt in prompts:
        print(f"\nQ: {prompt}")
        result = await adaptive_thinking_stream(prompt)
        print(f"\nResult: complexity={result['complexity']}, thinking={result['thinking_chars']}chars, time={result['elapsed_s']}s")

if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: simple tasks use 2k budget vs 16k; Haiku classifier costs ~50 tokens to save thousands
# Environment: mixed-complexity agents; adaptive budgeting prevents overspending on simple questions
```

### Option 5: Thinking Content Extractor for Debugging and Logging

```python
import anthropic
import json
import sqlite3
import time
from pathlib import Path

DB = Path("/tmp/thinking_log.db")
client = anthropic.Anthropic()

def init_db():
    con = sqlite3.connect(DB)
    con.execute("""
        CREATE TABLE IF NOT EXISTS thinking_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            prompt TEXT NOT NULL,
            thinking TEXT,
            response TEXT,
            budget_tokens INTEGER,
            thinking_chars INTEGER,
            response_chars INTEGER,
            elapsed_ms REAL,
            logged_at REAL
        )
    """)
    con.commit()
    con.close()

def stream_and_log(prompt: str, budget_tokens: int = 5000) -> dict:
    """Stream with thinking, log full thinking content for debugging."""
    thinking_parts = []
    response_parts = []
    current_block = None
    t0 = time.time()

    with client.messages.stream(
        model="claude-sonnet-4-6",
        max_tokens=budget_tokens + 1000,
        thinking={"type": "enabled", "budget_tokens": budget_tokens},
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        for event in stream:
            event_type = type(event).__name__
            if event_type == "RawContentBlockStartEvent":
                current_block = event.content_block.type
            elif event_type == "RawContentBlockDeltaEvent":
                delta = event.delta
                if hasattr(delta, "thinking") and current_block == "thinking":
                    thinking_parts.append(delta.thinking)
                elif hasattr(delta, "text") and current_block == "text":
                    response_parts.append(delta.text)
                    print(delta.text, end="", flush=True)
            elif event_type == "RawContentBlockStopEvent":
                current_block = None

    elapsed_ms = (time.time() - t0) * 1000
    thinking_text = "".join(thinking_parts)
    response_text = "".join(response_parts)

    con = sqlite3.connect(DB)
    con.execute("""
        INSERT INTO thinking_log (prompt, thinking, response, budget_tokens, thinking_chars, response_chars, elapsed_ms, logged_at)
        VALUES (?,?,?,?,?,?,?,?)
    """, (prompt[:500], thinking_text, response_text, budget_tokens,
          len(thinking_text), len(response_text), elapsed_ms, time.time()))
    con.commit()
    con.close()

    return {
        "thinking": thinking_text,
        "response": response_text,
        "thinking_chars": len(thinking_text),
        "elapsed_ms": round(elapsed_ms),
    }

def analyze_thinking_patterns():
    """Find patterns in thinking content across logged calls."""
    con = sqlite3.connect(DB)
    rows = con.execute("""
        SELECT prompt, thinking_chars, elapsed_ms
        FROM thinking_log
        ORDER BY thinking_chars DESC
        LIMIT 10
    """).fetchall()
    con.close()
    print("\n--- Thinking Analysis ---")
    for prompt, chars, ms in rows:
        print(f"  {prompt[:40]:40s} | thinking={chars:6d} chars | {ms:.0f}ms")

if __name__ == "__main__":
    init_db()
    prompts = [
        "What is the capital of France?",
        "Explain why the sky is blue using physics.",
    ]
    for p in prompts:
        print(f"\nQ: {p}")
        result = stream_and_log(p, budget_tokens=3000)
        print(f"\n  [thinking={result['thinking_chars']} chars, {result['elapsed_ms']}ms]")
    analyze_thinking_patterns()

# Expected Token Savings: logging enables post-hoc analysis; identify prompts where thinking uses budget poorly
# Environment: development and debugging; thinking log reveals when model over-reasons on simple tasks
```

### Option 6: Thinking-Guided Tool Use — Inspect Reasoning Before Tool Calls

```python
import anthropic
import asyncio

client = anthropic.AsyncAnthropic()

async def stream_thinking_then_tools(task: str, budget_tokens: int = 6000) -> str:
    """
    First use extended thinking to plan tool calls, then execute them.
    The thinking phase reveals which tools the model plans to use and why.
    """
    tools = [
        {"name": "calculate", "description": "Perform mathematical calculations",
         "input_schema": {"type": "object", "properties": {"expression": {"type": "string"}}, "required": ["expression"]}},
        {"name": "lookup_fact", "description": "Look up factual information",
         "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    ]

    thinking_text = ""
    response_text = ""
    tool_calls = []
    current_block = None

    print("[Phase 1: Extended thinking + planning]")
    async with client.messages.stream(
        model="claude-sonnet-4-6",
        max_tokens=budget_tokens + 1000,
        thinking={"type": "enabled", "budget_tokens": budget_tokens},
        tools=tools,
        messages=[{"role": "user", "content": task}],
    ) as stream:
        async for event in stream:
            event_type = type(event).__name__
            if event_type == "RawContentBlockStartEvent":
                block = event.content_block
                current_block = block.type
                if block.type == "tool_use":
                    tool_calls.append({"id": block.id, "name": block.name, "input_parts": []})
            elif event_type == "RawContentBlockDeltaEvent":
                delta = event.delta
                if hasattr(delta, "thinking") and current_block == "thinking":
                    thinking_text += delta.thinking
                elif hasattr(delta, "text") and current_block == "text":
                    response_text += delta.text
                    print(delta.text, end="", flush=True)
                elif hasattr(delta, "partial_json") and current_block == "tool_use" and tool_calls:
                    tool_calls[-1]["input_parts"].append(delta.partial_json)

    # Show thinking summary
    thinking_lines = [l for l in thinking_text.split("\n") if l.strip()]
    print(f"\n[Thinking: {len(thinking_text)} chars, {len(thinking_lines)} lines]")
    if thinking_lines:
        print(f"[First thought: {thinking_lines[0][:80]}]")

    if not tool_calls:
        return response_text

    # Execute tool calls
    print(f"\n[Phase 2: Executing {len(tool_calls)} tool calls]")
    tool_results = []
    for tc in tool_calls:
        import json
        try:
            args = json.loads("".join(tc["input_parts"]))
        except Exception:
            args = {}
        # Simulate tool execution
        if tc["name"] == "calculate":
            result = str(eval(args.get("expression", "0")))  # noqa: S307
        else:
            result = f"[Fact: {args.get('query', 'unknown')}]"
        print(f"  Tool {tc['name']}: {args} → {result}")
        tool_results.append({"type": "tool_result", "tool_use_id": tc["id"], "content": result})

    # Final response with tool results (no thinking needed for follow-up)
    final_resp = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=[
            {"role": "user", "content": task},
            {"role": "assistant", "content": [{"type": "text", "text": response_text or "I'll use the tools."}] +
             [{"type": "tool_use", "id": tc["id"], "name": tc["name"],
               "input": json.loads("".join(tc["input_parts"])) if tc["input_parts"] else {}}
              for tc in tool_calls]},
            {"role": "user", "content": tool_results},
        ],
    )
    return final_resp.content[0].text if final_resp.content else response_text

async def main():
    result = await stream_thinking_then_tools(
        "Calculate 17 * 23 and then look up what that number is known for.",
        budget_tokens=3000,
    )
    print(f"\nFinal answer: {result[:150]}")

if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: thinking phase plans all tool calls upfront; reduces back-and-forth tool rounds
# Environment: complex multi-tool agents; thinking reveals tool selection reasoning for debugging
```

## Comparison

| Option | Streaming | Budget Control | Persistence | Best For |
|--------|----------|---------------|-------------|---------|
| 1 — Basic block detection | Sync stream | Fixed budget | No | Simple thinking visualization |
| 2 — Async with callbacks | Async stream | Fixed budget | No | UI progress events via callbacks |
| 3 — SSE endpoint | Async stream | Configurable | No | Browser real-time display |
| 4 — Adaptive budget | Async + Haiku classifier | Auto-tuned | No | Mixed-complexity agents |
| 5 — Thinking logger | Sync stream | Fixed budget | SQLite | Debugging, pattern analysis |
| 6 — Thinking-guided tools | Async stream | Fixed budget | No | Multi-tool planning transparency |
