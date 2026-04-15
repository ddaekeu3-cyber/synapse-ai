---
layout: solution
title: "Agent Says 'You Said X Earlier' When X Was Never Said — Confabulating History"
category: hallucination
description: "Agent references something 'the user said earlier' that was never actually said. The agent is confabulating conversation history, presenting fabricated prior context as fact."
tags: [hallucination, conversation-history, confabulation, memory]
---

# Agent Says "You Said X Earlier" When X Was Never Said — Confabulating History

## Symptom
- Agent says "As you mentioned earlier, your project uses Python 3.9..."
- User never mentioned this — agent invented it
- Agent might say "You asked me to prioritize security" when no such instruction was given
- Fabricated history influences subsequent decisions
- Agent is confident about the invented prior statement

## Root Cause
When context is pruned or the model is uncertain about what was actually said, it fills gaps with plausible-sounding history. This is confabulation — the model generates coherent-sounding narrative filler based on statistical patterns, not actual memory.

Compounding factors:
1. Long conversations where context was pruned
2. Vague or implicit user messages that the agent "interprets" over-specifically
3. Model's training bias toward coherent narrative
4. No mechanism to flag uncertainty about conversation history

## Fix

### Option 1: Explicit anti-confabulation instruction

```
System prompt:
"You do not have reliable memory of the full conversation.
Never say 'you said...' or 'you mentioned...' or 'as you noted...' unless:
1. The statement appears verbatim in the current conversation context
2. You are quoting something you can actually see in your context window

If you're uncertain whether something was said, ask:
'Did you mention [X]? I want to confirm before proceeding with that assumption.'

Never invent conversation history — say 'I'm not sure if you specified this —
could you confirm [X]?' instead."
```

### Option 2: Store explicit conversation facts in a verified ledger

```python
VERIFIED_FACTS_FILE = "/workspace/.session_facts.json"

def record_verified_fact(fact, source_message_index):
    """Record facts with their source — prevents confabulation"""
    facts = load_facts()
    facts.append({
        "fact": fact,
        "source_index": source_message_index,
        "recorded_at": time.strftime('%H:%M:%S')
    })
    save_facts(facts)

def get_verified_facts():
    """Load only facts that were explicitly stated, not inferred"""
    return load_facts()

# In system prompt: "Facts explicitly stated by the user this session:"
# + get_verified_facts()
```

### Option 3: Quote-or-don't-say instruction

```
System prompt:
"Conversation history rule: If you reference something the user said, you must
be able to quote it exactly. Format: 'You said: "[exact quote]"'

If you cannot quote it exactly, do not claim the user said it.
Use instead: 'I understand you want [X] — is that correct?'
This distinction matters: confabulated user statements can corrupt task execution."
```

### Option 4: Verify before acting on assumed preferences

```python
async def get_confirmed_preference(preference_name, assumed_value, agent, history):
    """Ask agent to find the actual source before acting on a preference"""
    verification = await agent.complete([
        *history,
        {"role": "user", "content":
            f"Before proceeding: Can you find the exact message where I specified "
            f"'{preference_name}' = '{assumed_value}'? Quote it verbatim. "
            f"If you cannot find an exact quote, say 'not confirmed'."}
    ])

    if "not confirmed" in verification.lower():
        # Ask user explicitly
        return await ask_user(f"What is your preference for {preference_name}?")
    return assumed_value
```

## Recovery

When you detect confabulation:
1. Tell the agent: "I never said [X]. Please check your conversation context."
2. If context was pruned: "The earlier part of our conversation may not be available to you. Do not infer or invent what was said."
3. Add to system prompt: "You have access to [N] messages of context. For anything before that, ask rather than assume."

## Expected Token Savings
Debugging downstream decisions based on confabulated history: ~12,000 tokens
Prevention via explicit instruction: ~200 tokens in system prompt

## Environment
- Long agent sessions with context pruning
- Agents with vague or implicit user instructions
- Source: direct experience, well-documented LLM behavior
