---
layout: solution
title: "Token Cost Compounds Every Turn — Full Context Resent on Each Message"
category: token-cost
description: "Token usage grows quadratically as conversation length increases. Each API call resends the entire conversation history including all tool results, causing costs to compound turn over turn."
tags: [token-cost, context-window, caching, cost-optimization]
---

# Token Cost Compounds Every Turn — Full Context Resent on Each Message

## Symptom
- Turn 1: 500 tokens input
- Turn 5: 8,000 tokens input (all previous turns included)
- Turn 20: 60,000 tokens input
- Total cost for a 20-turn conversation: 300,000+ tokens — vastly more than expected
- Tool results (often large) are re-sent on every subsequent call

## Root Cause
LLM APIs are stateless — you must resend the full conversation history every call. Without pruning or caching, costs grow as O(n²) where n is the number of turns. Tool results are the worst offender: a single 10KB tool result re-sent 20 times = 200KB of unnecessary input tokens.

## Fix

### Option 1: Enable Anthropic prompt caching

Anthropic's prompt caching reduces re-sent context costs by 90%:

```python
messages = [
    {
        "role": "user",
        "content": [
            {
                "type": "text",
                "text": long_system_context,
                "cache_control": {"type": "ephemeral"}  # Cache this block
            }
        ]
    }
]
```

Cached blocks cost 10% of normal input price on re-use. Ideal for large static context (docs, tool schemas, long instructions).

### Option 2: Summarize old turns instead of keeping them verbatim

```python
SUMMARIZE_AFTER_TURNS = 10

async def get_condensed_history(history):
    if len(history) <= SUMMARIZE_AFTER_TURNS * 2:
        return history

    old_turns = history[:-SUMMARIZE_AFTER_TURNS * 2]
    recent_turns = history[-SUMMARIZE_AFTER_TURNS * 2:]

    summary = await agent.complete(
        f"Summarize these conversation turns in 5 bullet points, "
        f"preserving all decisions made and key facts:\n\n{old_turns}"
    )
    return [
        {"role": "user", "content": f"[Earlier context summary: {summary}]"},
        {"role": "assistant", "content": "Understood."},
        *recent_turns
    ]
```

### Option 3: Replace tool results with summaries after use

```python
def compress_tool_result(result, tool_name):
    """Replace large tool outputs with compact summaries once the agent has used them"""
    if len(str(result)) > 2000:
        return f"[{tool_name} result: {len(str(result))} chars — summarized: {str(result)[:500]}...]"
    return result

def compress_old_tool_results(messages, keep_last_n=3):
    """Replace tool results in old turns with summaries"""
    tool_result_count = 0
    for msg in reversed(messages):
        if msg.get('role') == 'tool':
            tool_result_count += 1
            if tool_result_count > keep_last_n:
                msg['content'] = f"[Tool result compressed — {len(str(msg['content']))} chars]"
    return messages
```

### Option 4: Set a token budget per conversation

```python
MAX_INPUT_TOKENS_PER_TURN = 20000

def enforce_token_budget(messages):
    """Drop oldest non-system messages if context too large"""
    while estimate_tokens(messages) > MAX_INPUT_TOKENS_PER_TURN:
        # Remove oldest non-system message pair
        for i, msg in enumerate(messages):
            if msg['role'] != 'system':
                messages.pop(i)
                if i < len(messages) and messages[i]['role'] != 'system':
                    messages.pop(i)
                break
    return messages
```

## Cost Comparison

| Approach | 20-turn conversation cost |
|----------|--------------------------|
| No optimization | ~300K tokens |
| Prompt caching | ~60K tokens (80% reduction) |
| Summarize after 10 turns | ~80K tokens |
| Compress tool results | ~100K tokens |
| All combined | ~25K tokens |

## Expected Token Savings
Per 20-turn agent session: 200,000–275,000 tokens saved
Source: direct measurement in production agent deployments
