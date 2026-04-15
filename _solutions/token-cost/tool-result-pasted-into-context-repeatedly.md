---
layout: solution
title: "Large Tool Results Re-Sent Every Turn — Wasting Tokens on Unchanged Data"
category: token-cost
description: "A large tool result (file content, API response, search results) is included in the conversation history and re-sent to the model on every subsequent turn, multiplying token costs."
tags: [token-cost, tool-results, context-pruning, caching]
---

# Large Tool Results Re-Sent Every Turn — Wasting Tokens on Unchanged Data

## Symptom
- One tool call returns 5,000 tokens of content (e.g., reading a large file)
- That result is included in conversation history
- Next 10 turns each re-send those 5,000 tokens unnecessarily
- Total unnecessary tokens: 50,000 just from one tool result
- Costs spike sharply after first large tool call

## Root Cause
LLM APIs are stateless — full conversation history is sent every call. Tool results are stored as messages in history and re-sent verbatim. There is no automatic "this was already processed, summarize it."

A single `read_file` call on a 10KB file = 2,500 tokens. Over 20 turns, that's 50,000 tokens of re-sent content that the model already has full context on.

## Fix

### Option 1: Compress tool results after first use

```python
MAX_TOOL_RESULT_TOKENS = 500  # Compress anything larger

def compress_tool_results_in_history(messages, keep_last_n_turns=2):
    """Replace large tool results in old turns with compact summaries"""
    recent_cutoff = len(messages) - (keep_last_n_turns * 2)

    for i, msg in enumerate(messages):
        if i >= recent_cutoff:
            break  # Don't compress recent turns
        if msg.get('role') == 'tool' and len(str(msg.get('content', ''))) > MAX_TOOL_RESULT_TOKENS * 4:
            original_len = len(str(msg['content']))
            msg['content'] = f"[Tool result compressed: {original_len} chars. Already processed in conversation above.]"

    return messages
```

### Option 2: Store large results externally, pass reference

```python
import hashlib

_result_store = {}

async def call_tool_with_caching(tool_name, params):
    result = await mcp_client.call_tool(tool_name, params)

    if len(str(result)) > 2000:
        # Store externally, give agent a short reference key
        key = hashlib.md5(f"{tool_name}:{params}".encode()).hexdigest()[:8]
        _result_store[key] = result

        # Return a compact summary + key for later retrieval
        summary = str(result)[:300] + "..."
        return f"[Large result stored as REF:{key}. Summary: {summary}]"

    return result  # Small results passed directly
```

### Option 3: Use Anthropic prompt caching for stable tool results

If a tool result won't change (file content, documentation, API schema):

```python
def build_messages_with_cached_tool_result(tool_result, conversation):
    return [
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": tool_result,
                    "cache_control": {"type": "ephemeral"}  # Cache = re-sends cost 10%
                }
            ]
        },
        *conversation  # Rest of conversation after the cached block
    ]
```

### Option 4: Summarize large results before adding to history

```python
async def add_tool_result_to_history(tool_name, result, history):
    result_text = str(result)

    if len(result_text) > 3000:
        # Summarize before adding to history
        summary = await agent.complete(
            f"Summarize this {tool_name} result in 3-5 bullet points, "
            f"preserving all actionable information:\n\n{result_text}"
        )
        history.append({
            "role": "tool",
            "content": f"[{tool_name} result summarized — original: {len(result_text)} chars]\n{summary}"
        })
    else:
        history.append({"role": "tool", "content": result_text})

    return history
```

## Cost Comparison

| File size | Turns | No optimization | With compression |
|-----------|-------|----------------|-----------------|
| 10KB file | 10    | 25,000 tokens  | 6,000 tokens    |
| 50KB file | 10    | 125,000 tokens | 8,000 tokens    |
| 10KB file | 50    | 125,000 tokens | 15,000 tokens   |

## Expected Token Savings
Per session with one large tool result over 10+ turns: 20,000–100,000 tokens
Source: measured in production agent sessions with file reading tools
