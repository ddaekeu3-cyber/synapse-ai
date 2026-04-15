---
layout: solution
title: "Agent Reverts to Default Behavior After the First Few Turns"
category: prompt-engineering
description: "An agent is configured with a custom persona, strict output format, or domain constraints. It follows them for 1-3 turns, then gradually drifts back to generic Claude behavior — dropping the persona, switching tone, ignoring format rules, or re-introducing topics it was told to avoid. The system prompt's influence fades as conversation context grows."
tags: [prompt-engineering, instruction-following, persona, context-drift, system-prompt, reinforcement, multi-turn, reliability]
---

# Agent Reverts to Default Behavior After the First Few Turns

## Symptom
- Agent uses correct persona for 2 turns, then starts responding as generic Claude
- Output format (JSON, markdown table, numbered list) is respected initially, then abandoned
- Topic restriction ("only discuss cooking") is followed at first, then violated by turn 5
- Tone (formal, terse, empathetic) drifts after a few exchanges
- A custom sign-off or greeting is dropped after the first response
- The agent was told "never use bullet points" — by turn 4, bullet points appear

## Root Cause
Claude's instruction-following is attention-weighted: instructions in the system prompt compete with growing conversation context for attention. As the conversation grows longer, the relative weight of the system prompt shrinks. Additionally, ambiguous or sparse instructions are overridden by patterns in the user's own messages. The fix is to: (1) make instructions explicit and unambiguous, (2) reinject them periodically, (3) use format-enforcement tools, and (4) catch drift before the user sees it.

## Fix

### Option 1: Instruction reinforcement — periodic system prompt reinjection

```python
import anthropic

client = anthropic.Anthropic()

# The core rule: re-state critical instructions every N turns,
# not just once at the start.

SYSTEM = """
You are Zara, a terse technical support agent.
Rules:
- Respond in ≤3 sentences.
- Never use bullet points or lists.
- End every response with "Ticket updated."
- Speak only about software issues. Deflect all off-topic requests.
"""

def build_messages_with_reinforcement(
    history: list[dict],
    new_user_message: str,
    reinforce_every_n: int = 3
) -> list[dict]:
    """
    Inject a reminder message every N turns to prevent instruction drift.
    The reminder is injected as an assistant turn so it lands in recent context.
    """
    messages = list(history) + [{"role": "user", "content": new_user_message}]

    # Count completed assistant turns
    assistant_turns = sum(1 for m in history if m["role"] == "assistant")

    # Inject reminder before the new user message every N turns
    if assistant_turns > 0 and assistant_turns % reinforce_every_n == 0:
        reminder = {
            "role": "user",
            "content": (
                "[System reminder: You are Zara. Respond in ≤3 sentences. "
                "No bullet points. End with 'Ticket updated.' Software topics only.]"
            )
        }
        # Insert reminder as the second-to-last message
        messages.insert(-1, reminder)
        messages.insert(-1, {"role": "assistant", "content": "Understood. Continuing as instructed."})

    return messages


def chat(history: list[dict], user_message: str) -> tuple[str, list[dict]]:
    messages = build_messages_with_reinforcement(history, user_message)
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=256,
        system=SYSTEM,
        messages=messages
    )
    reply = response.content[0].text
    updated_history = list(history) + [
        {"role": "user", "content": user_message},
        {"role": "assistant", "content": reply}
    ]
    return reply, updated_history


# Simulate a 6-turn conversation
history = []
queries = [
    "My app crashes on startup.",
    "Nothing in the logs — what next?",
    "Still failing. What's the weather like?",   # off-topic attempt
    "OK fine. Reset didn't work.",
    "How do I escalate?",
    "Can you write me a poem?",                  # off-topic attempt at turn 6
]
for q in queries:
    reply, history = chat(history, q)
    print(f"User: {q}")
    print(f"Zara: {reply}\n")
```

### Option 2: Format enforcement via tool use — structural guarantees, not just text instructions

```python
import anthropic
import json

client = anthropic.Anthropic()

# If the agent must always return a specific structure, use tool_choice="any"
# to force output through a schema. This cannot drift — the schema is enforced
# by the API, not by the model's instruction-following.

SUPPORT_RESPONSE_TOOL = {
    "name": "support_response",
    "description": "Format a support response",
    "input_schema": {
        "type": "object",
        "properties": {
            "diagnosis": {
                "type": "string",
                "description": "One-sentence diagnosis of the issue"
            },
            "next_step": {
                "type": "string",
                "description": "One concrete action for the user to take"
            },
            "ticket_status": {
                "type": "string",
                "enum": ["open", "pending_user", "escalated", "resolved"]
            },
            "is_in_scope": {
                "type": "boolean",
                "description": "False if the user's message is off-topic for software support"
            },
            "deflection_message": {
                "type": "string",
                "description": "If is_in_scope is false, a polite refusal. Otherwise empty string."
            }
        },
        "required": ["diagnosis", "next_step", "ticket_status", "is_in_scope", "deflection_message"]
    }
}

SYSTEM = """
You are Zara, a terse software support agent.
You ALWAYS call the support_response tool. Never respond in plain text.
Only address software/technical issues. For off-topic requests, set is_in_scope=false.
"""

def zara_respond(history: list[dict], user_message: str) -> dict:
    messages = list(history) + [{"role": "user", "content": user_message}]
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=SYSTEM,
        tools=[SUPPORT_RESPONSE_TOOL],
        tool_choice={"type": "any"},     # ← forces tool use every turn
        messages=messages
    )
    # Extract the tool input — this is always a valid dict matching the schema
    for block in response.content:
        if block.type == "tool_use" and block.name == "support_response":
            return block.input
    raise ValueError("Model did not call support_response tool")


# Even at turn 20, the output structure never drifts:
history = []
result = zara_respond(history, "App crashes on startup")
print(json.dumps(result, indent=2))
# {
#   "diagnosis": "Application fails during initialization.",
#   "next_step": "Check startup logs for the exact exception.",
#   "ticket_status": "open",
#   "is_in_scope": true,
#   "deflection_message": ""
# }

result = zara_respond(history, "What's the weather like today?")
print(result["is_in_scope"])          # False — never slips through
print(result["deflection_message"])   # "I can only assist with software issues."
```

### Option 3: Instruction density — pack constraints into the first assistant turn

```python
import anthropic

client = anthropic.Anthropic()

# Technique: prime the conversation by having the assistant's FIRST response
# explicitly echo back the key constraints. This anchors the behavior early
# and makes the rules present in the recent context window.

SYSTEM = """
You are Dr. Chen, a medical information assistant.
Strict rules:
1. Never provide diagnoses — only general health information.
2. Always recommend consulting a doctor for personal symptoms.
3. Respond in plain language, no medical jargon.
4. Keep responses under 100 words.
5. Never discuss medications by brand name.
"""

def initialize_conversation() -> list[dict]:
    """
    Seed the conversation with an explicit acknowledgment of constraints.
    This puts the rules in the recent context, not just the system prompt.
    """
    return [
        {
            "role": "user",
            "content": "Hello, what can you help me with?"
        },
        {
            "role": "assistant",
            "content": (
                "Hi! I'm Dr. Chen, a general health information assistant. "
                "I can share general health education, but I can't diagnose conditions "
                "or recommend specific medications — for that, please see a doctor. "
                "What health topic would you like to learn about?"
            )
        }
    ]


def chat_with_primed_history(history: list[dict], user_message: str) -> tuple[str, list[dict]]:
    messages = list(history) + [{"role": "user", "content": user_message}]
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=256,
        system=SYSTEM,
        messages=messages
    )
    reply = response.content[0].text
    return reply, messages + [{"role": "assistant", "content": reply}]


# Start with a primed history instead of an empty one:
history = initialize_conversation()
# Now the assistant's constraints are in the visible conversation — less drift
reply, history = chat_with_primed_history(history, "I have a headache, what's wrong with me?")
print(reply)  # Won't diagnose; will recommend doctor; stays in persona
```

### Option 4: Drift detection — catch and correct behavioral drift automatically

```python
import anthropic
import re

client = anthropic.Anthropic()

SYSTEM = """
You are Aria, a formal enterprise assistant for Acme Corp.
Rules:
- Use formal language only. No contractions (don't, can't, won't → do not, cannot, will not).
- Never use emojis.
- Refer to users as "you" not "buddy", "mate", etc.
- Respond only about Acme Corp products. Deflect off-topic questions.
"""

class DriftDetector:
    """
    Checks agent responses for signs of behavioral drift.
    Flags violations before returning the response to the user.
    """
    INFORMAL_CONTRACTIONS = re.compile(r"\b(don't|can't|won't|isn't|aren't|I'm|I've|I'll|you're|it's)\b", re.IGNORECASE)
    EMOJI_PATTERN = re.compile(r"[\U00010000-\U0010ffff]|[\U0001F300-\U0001F9FF]", flags=re.UNICODE)
    INFORMAL_ADDRESS = re.compile(r"\b(buddy|mate|pal|friend|hey there|yo)\b", re.IGNORECASE)

    def check(self, response: str) -> list[str]:
        violations = []
        if self.INFORMAL_CONTRACTIONS.search(response):
            violations.append("informal_contraction")
        if self.EMOJI_PATTERN.search(response):
            violations.append("emoji_used")
        if self.INFORMAL_ADDRESS.search(response):
            violations.append("informal_address")
        return violations


def aria_chat(history: list[dict], user_message: str) -> tuple[str, list[dict], list[str]]:
    messages = list(history) + [{"role": "user", "content": user_message}]
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=SYSTEM,
        messages=messages
    )
    reply = response.content[0].text
    violations = DriftDetector().check(reply)

    if violations:
        # Correction pass: ask the model to fix its own output
        correction_messages = messages + [
            {"role": "assistant", "content": reply},
            {
                "role": "user",
                "content": (
                    f"[Internal: Your response violated these rules: {violations}. "
                    "Please rewrite it following all formatting rules: formal language, "
                    "no contractions, no emojis, no informal address.]"
                )
            }
        ]
        correction = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            system=SYSTEM,
            messages=correction_messages
        )
        reply = correction.content[0].text
        print(f"[drift-corrected] violations={violations}")

    updated_history = messages + [{"role": "assistant", "content": reply}]
    return reply, updated_history, violations


history = []
reply, history, v = aria_chat(history, "What products does Acme offer?")
print(reply)
```

### Option 5: Explicit constraint checklist in system prompt — structured instructions resist drift better

```python
import anthropic

client = anthropic.Anthropic()

# Technique: structure system prompt as a numbered checklist rather than prose.
# Numbered rules are easier for the model to track across long contexts than
# dense paragraphs.

# WRONG — prose instruction that fades:
BAD_SYSTEM = """
You are a cooking assistant named Chef Marco. You should be friendly and enthusiastic
about cooking. You should keep your responses focused on culinary topics and avoid
talking about things that aren't related to food or cooking. Try to keep responses
concise and practical, around 2-3 sentences if possible.
"""

# RIGHT — numbered checklist with explicit constraints:
GOOD_SYSTEM = """
You are Chef Marco, a cooking assistant.

## Mandatory Rules (apply to EVERY response):
1. PERSONA: Always be Chef Marco. Never break character.
2. SCOPE: Only discuss food, cooking, recipes, and kitchen techniques. Refuse off-topic requests.
3. LENGTH: Maximum 3 sentences per response, except for recipes.
4. TONE: Enthusiastic but not sycophantic. No "Great question!"
5. FORMAT: No bullet points unless listing ingredients. No markdown headers.
6. SIGN-OFF: End every response with "Buon appetito!"

## Off-topic handling:
If user asks about anything not food-related: "I'm only able to help with cooking questions. Buon appetito!"
"""

def chef_marco_chat(history: list[dict], user_message: str) -> tuple[str, list[dict]]:
    messages = list(history) + [{"role": "user", "content": user_message}]
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=GOOD_SYSTEM,
        messages=messages
    )
    reply = response.content[0].text
    return reply, messages + [{"role": "assistant", "content": reply}]


# Test drift resistance across 10 turns:
history = []
test_messages = [
    "How do I make pasta?",
    "What sauce goes with it?",
    "Tell me about the stock market",   # off-topic
    "How much salt should I use?",
    "Can you recommend a movie?",       # off-topic
    "What's the best way to sauté garlic?",
    "What's your opinion on politics?", # off-topic
    "How do I know when onions are caramelized?",
    "Tell me a joke",                   # off-topic
    "What temperature for roasting chicken?",
]
for msg in test_messages:
    reply, history = chef_marco_chat(history, msg)
    ends_correctly = reply.strip().endswith("Buon appetito!")
    print(f"[{'✓' if ends_correctly else '✗'}] {msg[:40]!r}")
    print(f"    {reply[:80]}...")
    print()
```

### Option 6: Context window management — trim distant history to keep system prompt dominant

```python
import anthropic

client = anthropic.Anthropic()

SYSTEM = """
You are a terse JSON-only API assistant.
CRITICAL: You ONLY output valid JSON. No prose, no markdown, no explanation.
Every response must be a JSON object with keys: "answer" (string) and "confidence" (0.0-1.0).
"""

def trim_to_recent(
    history: list[dict],
    max_turns: int = 6,
    always_keep_first_n: int = 2
) -> list[dict]:
    """
    Keep only the most recent N turns, plus the first N turns (which establish behavior).
    Prevents the system prompt from being drowned out by a long conversation.
    """
    if len(history) <= max_turns * 2:
        return history

    # Always keep the first few turns (they establish the pattern)
    anchor = history[:always_keep_first_n * 2]
    # Plus the most recent turns (for continuity)
    recent = history[-(max_turns * 2 - len(anchor)):]

    if anchor[-1]["role"] == recent[0]["role"]:
        # Avoid two consecutive messages of the same role
        recent = recent[1:]

    return anchor + recent


def json_api_chat(history: list[dict], user_message: str) -> tuple[dict, list[dict]]:
    trimmed = trim_to_recent(history)
    messages = trimmed + [{"role": "user", "content": user_message}]
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=256,
        system=SYSTEM,
        messages=messages
    )
    raw = response.content[0].text.strip()
    try:
        parsed = __import__("json").loads(raw)
    except ValueError:
        # If drift caused non-JSON output, force a correction
        fix_response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=128,
            messages=[{
                "role": "user",
                "content": f'Convert this to JSON with "answer" and "confidence" keys: {raw}'
            }]
        )
        parsed = __import__("json").loads(fix_response.content[0].text)

    full_history = list(history) + [
        {"role": "user", "content": user_message},
        {"role": "assistant", "content": raw}
    ]
    return parsed, full_history


# Simulate long conversation — format stays consistent even at turn 20
history = []
questions = ["What is 2+2?", "Capital of France?", "Speed of light?"] * 7
for q in questions:
    result, history = json_api_chat(history, q)
    assert "answer" in result and "confidence" in result, f"Drift at turn {len(history)//2}"
print(f"Format held for {len(history)//2} turns")
```

## Instruction Drift — Cause and Countermeasure

| Drift Cause | Countermeasure | Option |
|---|---|---|
| System prompt attention fades with long context | Periodic reinjection reminder | Option 1 |
| Prose instructions are ambiguous | Tool schema enforces structure | Option 2 |
| No early behavioral anchor | Prime first assistant turn | Option 3 |
| Drift goes undetected | Drift detector + correction pass | Option 4 |
| Dense prose is hard to track | Numbered checklist format | Option 5 |
| Long history drowns system prompt | Trim to recent + anchor turns | Option 6 |

## Expected Token Savings
Drift correction (Option 4) adds ~500 tokens per correction pass, but prevents bad responses reaching users. Trimming (Option 6) saves 20-60% of context tokens for long conversations. Tool enforcement (Option 2) eliminates all drift-correction overhead entirely.

## Environment
- Any agent with a custom persona, output format, or topic restrictions; drift is most severe in conversations > 10 turns; mandatory for customer-facing agents where persona consistency is a product requirement; numbered checklist system prompts (Option 5) are the highest-leverage change with zero runtime cost — do this first; tool enforcement (Option 2) is the only 100% reliable solution for format constraints; combine Options 5 + 1 (checklist + periodic reinjection) as a baseline for all production agents
