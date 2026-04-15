---
layout: solution
title: "Agent Responses Get Slower Over Time — Latency Grows with Session Length"
category: performance
description: "First few responses are fast (<2s). After 30 minutes, responses take 10–15s. Latency grows linearly with session duration. Caused by unbounded context window growth."
tags: [latency, context-growth, performance, session-length, slow]
---

# Agent Responses Get Slower Over Time — Latency Grows with Session Length

## Symptom
- Response time at session start: 1–2 seconds
- Response time after 30 minutes: 8–12 seconds
- Response time after 1 hour: 15–20 seconds
- Restarting the session restores fast responses
- Longer conversations → slower responses, proportionally
- No errors — just increasing latency

## Root Cause
LLM inference time scales with input token count. Each turn sends the entire conversation history. A 100-turn conversation might have 50,000 tokens of context — the model processes all 50,000 tokens to generate each response, even if the question is simple. Inference is O(n²) in attention layers.

## Measurement

```python
import time, anthropic

client = anthropic.Anthropic()
history = []

for turn in range(50):
    start = time.time()
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=100,
        messages=history + [{"role": "user", "content": "Quick question"}]
    )
    latency = time.time() - start
    tokens = response.usage.input_tokens
    print(f"Turn {turn}: {latency:.1f}s | {tokens} input tokens")
    history.append({"role": "user", "content": "Quick question"})
    history.append({"role": "assistant", "content": response.content[0].text})
```

## Fix

### Option 1: Periodic context summarization

```python
SUMMARIZE_EVERY_N_TURNS = 20
TOKEN_WARNING_THRESHOLD = 50000

async def managed_session(agent_client):
    history = []
    turn_count = 0

    while True:
        user_message = await get_user_input()
        history.append({"role": "user", "content": user_message})

        # Check if summarization needed
        if turn_count > 0 and turn_count % SUMMARIZE_EVERY_N_TURNS == 0:
            summary = await summarize_history(history[:-5])  # Keep last 5 turns
            history = [
                {"role": "user", "content": f"[Session summary: {summary}]"},
                {"role": "assistant", "content": "Understood, continuing."},
                *history[-5:]
            ]
            print(f"Context summarized at turn {turn_count}")

        response = await agent_client.complete(history)
        history.append({"role": "assistant", "content": response})
        turn_count += 1
```

### Option 2: Sliding window context

```python
def sliding_window_history(history, max_tokens=30000, always_keep_first=2):
    """Keep only the most recent N tokens of context"""
    if len(history) <= always_keep_first:
        return history

    # Always keep first few messages (system context, initial setup)
    preserved = history[:always_keep_first]
    recent = history[always_keep_first:]

    # Trim from the middle until within token budget
    while estimate_tokens(preserved + recent) > max_tokens and len(recent) > 2:
        recent = recent[2:]  # Remove oldest user+assistant pair

    return preserved + recent
```

### Option 3: Compress tool results in history

```python
def compress_old_tool_results(history, keep_recent=3):
    """Replace large tool results in old turns with summaries"""
    tool_result_indices = [
        i for i, m in enumerate(history)
        if m.get('role') == 'tool'
    ]

    # Keep recent tool results, compress old ones
    old_tool_indices = tool_result_indices[:-keep_recent]
    for i in old_tool_indices:
        content = str(history[i].get('content', ''))
        if len(content) > 500:
            history[i]['content'] = f"[Tool result: {len(content)} chars, already processed]"

    return history
```

### Option 4: Enable prompt caching for stable content

```python
# Cache stable content (system prompt, reference documents)
# Reduces effective input tokens from 50K to 5K for cached portions
messages = [
    {
        "role": "user",
        "content": [
            {
                "type": "text",
                "text": large_stable_context,
                "cache_control": {"type": "ephemeral"}  # 90% cost/latency reduction
            },
            {
                "type": "text",
                "text": new_user_message
            }
        ]
    }
]
```

### Option 5: Monitor and alert on context growth

```python
async def complete_with_latency_monitoring(history, agent):
    input_tokens = estimate_tokens(history)
    start = time.time()

    response = await agent.complete(history)

    latency = time.time() - start
    if latency > 5.0:
        print(f"Warning: High latency {latency:.1f}s with {input_tokens} input tokens")
        print("Consider running context summarization")

    return response
```

## Latency vs Context Size (Approximate)

| Input tokens | Estimated latency |
|-------------|------------------|
| 5,000 | 1–2s |
| 20,000 | 3–5s |
| 50,000 | 8–12s |
| 100,000 | 15–25s |

## Expected Token Savings
Long session without management: 50K+ tokens per turn
With sliding window (30K cap): constant 30K tokens per turn

## Environment
- Any long-running agent session
- Source: direct measurement, Anthropic inference characteristics
