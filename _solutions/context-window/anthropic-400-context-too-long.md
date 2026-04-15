---
layout: solution
title: "Anthropic API 400 Error — Context Length Exceeds Model Maximum"
category: context-window
description: "API returns 400 Bad Request with 'prompt is too long' or 'context_length_exceeded'. Request rejected before the model processes anything. Agent crashes or enters retry loop."
tags: [context-window, 400, token-limit, truncation, claude]
---

# Anthropic API 400 Error — Context Length Exceeds Model Maximum

## Symptom
- API returns `400 Bad Request`
- Error: `{"error": {"type": "invalid_request_error", "message": "prompt is too long"}}`
- Or: `context_length_exceeded` / `max_tokens_exceeded`
- Request fails immediately — no response generated
- Agent may retry with same context and fail again in a loop

## Root Cause
The total tokens in messages + max_tokens exceeds the model's context window. Model limits:
- claude-haiku-4-5: 200K tokens input
- claude-sonnet-4-6: 200K tokens input
- claude-opus-4-6: 200K tokens input

Most common causes: unbounded conversation history, large tool results, or system prompt + conversation combined exceeding limit.

## Fix

### Option 1: Measure before sending

```python
import anthropic

client = anthropic.Anthropic()

def count_tokens(messages, system=""):
    """Count tokens without sending the request"""
    response = client.messages.count_tokens(
        model="claude-sonnet-4-6",
        system=system,
        messages=messages,
    )
    return response.input_tokens

def build_safe_messages(history, system_prompt, max_input_tokens=180000):
    """Trim history to fit within context limit"""
    total = count_tokens(history, system_prompt)

    while total > max_input_tokens and len(history) > 2:
        # Remove oldest non-system message pair
        history = history[2:]  # Remove oldest user + assistant turn
        total = count_tokens(history, system_prompt)

    return history
```

### Option 2: Estimate tokens cheaply and truncate

```python
def estimate_tokens(text):
    """Rough estimate: 1 token ≈ 4 characters"""
    return len(str(text)) // 4

def truncate_messages_to_limit(messages, max_tokens=150000, reserve_for_output=8192):
    budget = max_tokens - reserve_for_output
    total = 0
    result = []

    # Always keep system message
    system_msgs = [m for m in messages if m.get('role') == 'system']
    for msg in system_msgs:
        total += estimate_tokens(msg['content'])
        result.append(msg)

    # Add recent messages until budget runs out
    non_system = [m for m in messages if m.get('role') != 'system']
    for msg in reversed(non_system):
        msg_tokens = estimate_tokens(msg['content'])
        if total + msg_tokens > budget:
            break
        result.insert(len(system_msgs), msg)
        total += msg_tokens

    return result
```

### Option 3: Catch 400 and auto-trim

```python
from anthropic import BadRequestError

async def complete_with_trim(messages, system, model="claude-sonnet-4-6"):
    max_retries = 3
    for attempt in range(max_retries):
        try:
            return client.messages.create(
                model=model,
                system=system,
                messages=messages,
                max_tokens=4096
            )
        except BadRequestError as e:
            if "prompt is too long" in str(e) and attempt < max_retries - 1:
                # Remove 20% of oldest messages and retry
                trim_count = max(2, len(messages) // 5)
                messages = messages[trim_count:]
                print(f"Context too long — trimmed {trim_count} messages, retrying")
            else:
                raise
```

### Option 4: Summarize before hitting limit

```python
CONTEXT_WARNING_THRESHOLD = 150000  # Warn at 75% of 200K

async def managed_conversation(messages, system):
    token_count = count_tokens(messages, system)

    if token_count > CONTEXT_WARNING_THRESHOLD:
        print(f"Context at {token_count} tokens — summarizing old turns")
        # Summarize first half of conversation
        old_messages = messages[:len(messages)//2]
        summary = await client.messages.create(
            model="claude-haiku-4-5-20251001",  # Use cheap model for summarization
            max_tokens=2000,
            messages=[{
                "role": "user",
                "content": f"Summarize this conversation in 5 bullet points:\n{old_messages}"
            }]
        )
        messages = [
            {"role": "user", "content": f"[Earlier summary: {summary.content[0].text}]"},
            {"role": "assistant", "content": "Understood, continuing from that context."},
            *messages[len(messages)//2:]
        ]

    return await complete_with_trim(messages, system)
```

## Model Context Limits Quick Reference

| Model | Max Input | Safe Limit (with output) |
|-------|-----------|--------------------------|
| claude-haiku-4-5 | 200K | 190K |
| claude-sonnet-4-6 | 200K | 190K |
| claude-opus-4-6 | 200K | 190K |

## Expected Token Savings
Agent retry-looping on 400 error: ~5,000 wasted + task failure
Proactive trimming: task completes, 0 extra tokens

## Environment
- Anthropic API (all current models)
- Most common in: long agent sessions, file-reading agents
- Source: Anthropic API documentation, direct experience
