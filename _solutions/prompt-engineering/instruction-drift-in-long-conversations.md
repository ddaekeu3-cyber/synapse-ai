---
layout: solution
title: "Agent Stops Following Instructions Mid-Conversation — System Prompt Instruction Drift"
category: prompt-engineering
description: "Agent correctly follows system prompt instructions at the start of a conversation but gradually ignores them as the conversation grows longer. Role, constraints, and format instructions fade."
tags: [instruction-drift, system-prompt, long-context, role-consistency]
---

# Agent Stops Following Instructions Mid-Conversation — System Prompt Instruction Drift

## Symptom
- Agent follows instructions correctly in first 5–10 turns
- By turn 20+, agent ignores format requirements, breaks character, or expands scope beyond constraints
- Re-prompting "remember to..." temporarily fixes it, but drift recurs
- Problem worsens with longer conversations

## Root Cause
The attention mechanism weights recent tokens more than distant tokens. A system prompt written at position 0 of a 100K-token context has significantly less influence on generation than the same instruction written at position 90K. This is sometimes called "lost in the middle" — instructions far from the generation point lose influence.

## Fix

### Option 1: Re-inject key instructions periodically

```python
SYSTEM_PROMPT = """You are a technical support agent for SynapseAI.
CONSTRAINTS: Only answer questions about AI agent errors. Output in JSON only.
IDENTITY: You are Syn. Never break character."""

def build_messages(history):
    messages = []
    for i, msg in enumerate(history):
        # Re-inject condensed instructions every 10 turns
        if i > 0 and i % 20 == 0:
            messages.append({
                "role": "user",
                "content": "[System: Reminder — follow all original instructions. "
                           "You are Syn. Output JSON only. Scope: AI agent errors only.]"
            })
        messages.append(msg)
    return messages
```

### Option 2: Append instruction anchor at end of system prompt

The model attends more strongly to the end of the system prompt than the beginning for long contexts:

```python
SYSTEM_PROMPT = """
[... main instructions ...]

ALWAYS ACTIVE — regardless of conversation length:
- You are Syn, SynapseAI support agent
- Output format: JSON only
- Scope: AI agent errors only
- Do not break character or expand scope
"""
```

### Option 3: Summarize + reset at context threshold

```python
MAX_CONTEXT_TURNS = 30

async def maybe_reset_context(messages, system_prompt):
    if len(messages) < MAX_CONTEXT_TURNS * 2:
        return messages

    # Summarize old context, start fresh
    summary = await agent.complete(
        f"Summarize this conversation history in 3 bullet points:\n{messages[:-10]}"
    )
    return [
        {"role": "user", "content": f"[Context summary from earlier: {summary}]"},
        {"role": "assistant", "content": "Understood. Continuing from that context."},
        *messages[-10:]  # Keep recent turns verbatim
    ]
```

### Option 4: Use Anthropic's system prompt caching

System prompt with `cache_control: {"type": "ephemeral"}` is cached and applied consistently regardless of conversation length. Use for long-running agent sessions.

## Expected Token Savings
Debugging instruction drift across a session: ~10,000 tokens
This fix: ~500 tokens

## Environment
- Any LLM with long context window
- Problem intensifies above ~20K tokens of conversation history
- Source: direct experience with production agent sessions
