---
layout: solution
title: "Agent Repeats the Same Mistake Within the Same Session — No Self-Correction"
category: general
description: "Agent makes an error, user corrects it, agent acknowledges the fix — then makes the exact same mistake 3 turns later. Correction doesn't persist within the session."
tags: [self-correction, mistakes, memory, session, repetition]
---

# Agent Repeats the Same Mistake Within the Same Session — No Self-Correction

## Symptom
- Agent uses wrong variable name → corrected → uses same wrong name again later
- Agent forgets to add error handling → reminded → skips it in next function too
- Pattern: error → fix → same error → fix → same error
- Agent seems to acknowledge feedback but doesn't apply it going forward
- Same structural mistake appears in every code block despite corrections

## Root Cause
The model attends to recent tokens more than older ones. A correction made 10 turns ago has less influence on current output than the immediate context. Without a mechanism to pin corrections into current attention, the same mistake recurs as context grows.

## Fix

### Option 1: Maintain an explicit corrections list in system prompt

```python
class SessionCorrections:
    def __init__(self):
        self.corrections = []

    def add(self, mistake: str, correct: str):
        self.corrections.append({"mistake": mistake, "correct": correct})

    def to_system_prompt_section(self) -> str:
        if not self.corrections:
            return ""
        lines = ["CORRECTIONS — apply these in all future responses:"]
        for i, c in enumerate(self.corrections, 1):
            lines.append(f"{i}. WRONG: {c['mistake']} | RIGHT: {c['correct']}")
        return "\n".join(lines)

corrections = SessionCorrections()

# When user corrects the agent:
corrections.add(
    mistake="using 'conn' variable for database connection",
    correct="use 'db_session' — that's the actual variable name in this codebase"
)

# Rebuild system prompt with corrections injected
system = BASE_SYSTEM_PROMPT + "\n\n" + corrections.to_system_prompt_section()
```

### Option 2: Inject correction reminders before each response

```python
ACTIVE_RULES = []  # Populated as user corrects agent

def inject_correction_reminder(messages: list) -> list:
    """Add correction reminder as the last user message context"""
    if not ACTIVE_RULES:
        return messages

    reminder = "REMINDER — rules from earlier in this session:\n" + \
               "\n".join(f"- {rule}" for rule in ACTIVE_RULES)

    # Append reminder to the latest user message
    if messages and messages[-1]["role"] == "user":
        messages[-1]["content"] += f"\n\n{reminder}"

    return messages

# Usage: when user says "don't do X, do Y instead"
ACTIVE_RULES.append("Use snake_case for all variable names, not camelCase")
ACTIVE_RULES.append("Always add type hints to function signatures")
```

### Option 3: Detect correction intent and extract rule

```python
CORRECTION_PHRASES = [
    "don't", "do not", "stop", "never", "always",
    "I said", "remember", "I told you", "again",
    "like I mentioned", "as I said"
]

async def detect_and_save_correction(user_message: str, agent) -> str | None:
    """If user is correcting agent, extract the rule"""
    is_correction = any(phrase in user_message.lower() for phrase in CORRECTION_PHRASES)
    if not is_correction:
        return None

    rule = await agent.complete([{
        "role": "user",
        "content": f"""Extract the rule from this correction message.
Return a single concise rule in the format "Always X" or "Never Y".
Message: "{user_message}"
Rule:"""
    }])

    return rule.strip()

# In agent loop
correction_rule = await detect_and_save_correction(user_input, agent)
if correction_rule:
    ACTIVE_RULES.append(correction_rule)
    print(f"Rule saved: {correction_rule}")
```

### Option 4: Explicit rule acknowledgment protocol

```
System prompt:
"When the user corrects your behavior:
1. State the rule explicitly: 'Rule noted: I will [specific behavior] from now on.'
2. Apply the rule for the rest of the session without exception
3. If you're about to violate a rule you acknowledged, STOP and self-correct
4. If you've been told something multiple times, prioritize it over your defaults

Before generating each response, check: am I about to repeat a mistake I was corrected on?"
```

### Option 5: Post-response self-review against known corrections

```python
async def generate_with_self_review(prompt: str, agent, active_rules: list) -> str:
    response = await agent.complete(prompt)

    if not active_rules:
        return response

    review_prompt = f"""Review this response for rule violations:

Response:
{response}

Rules to check:
{chr(10).join(f"- {rule}" for rule in active_rules)}

Does the response violate any rules? If yes, rewrite it fixing all violations.
If no violations, return the response unchanged."""

    reviewed = await agent.complete(review_prompt)
    return reviewed
```

## Correction Persistence Patterns

| When correction was made | How long it persists | Fix |
|---|---|---|
| 1-2 turns ago | Usually persists | No fix needed |
| 5-10 turns ago | May drift | Add to rules list |
| 20+ turns ago | Often lost | Must be in system prompt |
| Next session | Always lost | Save to preferences file |
| Structural pattern | Hard to override | Add to system prompt baseline |

## Expected Token Savings
Repeating corrections 5+ times: ~6,000 tokens
Rule extracted and pinned upfront: ~200 tokens per rule

## Environment
- Any long-running agent session with iterative feedback
- Source: direct experience, common failure mode in code-generation agents
