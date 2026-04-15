---
layout: solution
title: "Agent Loses Track of Earlier Instructions"
category: context-window
description: "In long conversations, the agent forgets constraints, preferences, or goals stated early in the session as the system prompt's influence is diluted by accumulated message history."
tags: [context-window, memory, long-context, system-prompt, instruction-following]
---

## Symptom

A user sets a constraint at the start of a session: "Always respond in bullet points" or "Only recommend open-source tools." Fifty messages later the agent has abandoned both rules and is writing prose paragraphs and suggesting paid SaaS products. The agent did not forget the facts — it can still recall early conversation content when asked directly — but its instruction-following behaviour has drifted as the sheer volume of subsequent messages reduced the relative attention weight on the original constraints.

## Root Cause

Transformer attention is distributed across all tokens in the context window. As more messages accumulate, the system prompt tokens represent a shrinking fraction of total context, and their per-token attention score decreases relative to recent messages. This is not a bug — it is a predictable consequence of how attention works. Constraints stated once at the start of a long session will always drift unless the agent is designed to re-anchor them.

## Fix

### Option 1 — Re-inject critical instructions as a periodic reminder message

```python
import anthropic

client = anthropic.Anthropic()

SYSTEM = """You are a helpful assistant for a Python-only engineering team.
Rules (always follow these):
- Only suggest Python libraries and tools.
- Always include a pip install command for any library you mention.
- Format all code in fenced code blocks with the python language tag.
"""

REMINDER = """[REMINDER — always follow these rules in every response:
• Python tools only — no JavaScript, Go, or other languages
• Include pip install commands for every library
• Use ```python code blocks for all code]"""

def chat(history: list[dict], user_message: str, turn: int) -> tuple[str, list[dict]]:
    history = history + [{"role": "user", "content": user_message}]

    # Inject reminder every 5 turns to re-anchor instructions
    messages_to_send = list(history)
    if turn > 0 and turn % 5 == 0:
        messages_to_send = list(history)
        # Insert reminder as a synthetic assistant acknowledgement pair
        messages_to_send.insert(-1, {"role": "user",      "content": REMINDER})
        messages_to_send.insert(-1, {"role": "assistant", "content": "Understood, I'll continue following these rules."})

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=SYSTEM,
        messages=messages_to_send,
    )
    reply = response.content[0].text
    history = history + [{"role": "assistant", "content": reply}]
    return reply, history

history = []
questions = [
    "How do I read a CSV file?",            # turn 0
    "What's the best HTTP client?",         # turn 1
    "How do I parse JSON?",                 # turn 2
    "Recommend a task queue system.",       # turn 3
    "How do I connect to PostgreSQL?",      # turn 4 — reminder injected before this
    "What web framework should I use?",     # turn 5
]
for i, q in enumerate(questions):
    reply, history = chat(history, q, i)
    print(f"[turn {i}] Q: {q}\nA: {reply[:200]}\n")
```

**Expected Token Savings:** Periodic reminders (~80 tokens) prevent correction turns (~400 tokens each) when instruction drift causes wrong answers.
**Environment:** Long-session agents with strict format or scope constraints; reminder every 5-10 turns maintains compliance.

---

### Option 2 — Instruction pinning: keep constraints at the end of the message list

```python
import anthropic

client = anthropic.Anthropic()

SYSTEM = """You are a concise technical assistant."""

# These constraints are always appended at the END of the message list
# so they have maximum recency weight in the attention window
PINNED_CONSTRAINTS = """

[Active constraints for this session — apply to every response:
• Max response length: 3 sentences
• No markdown — plain text only
• No apologies or filler phrases like "Great question!"
• Always end with a follow-up question]"""

def chat(history: list[dict], user_message: str) -> tuple[str, list[dict]]:
    history = history + [{"role": "user", "content": user_message + PINNED_CONSTRAINTS}]
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=SYSTEM,
        messages=history,
    )
    reply = response.content[0].text
    # Store history without the pinned suffix (keep history clean)
    clean_history = list(history[:-1]) + [{"role": "user", "content": user_message}]
    clean_history = clean_history + [{"role": "assistant", "content": reply}]
    return reply, clean_history

history = []
for turn, question in enumerate([
    "What is a database index?",
    "How does caching work?",
    "Explain load balancing.",
    "What is a CDN?",
    "Describe microservices.",
]):
    reply, history = chat(history, question)
    print(f"[turn {turn}] {reply[:300]}\n")
```

**Expected Token Savings:** Pinning constraints to the most-recent position maximises their attention weight at zero extra API calls; prevents drift more reliably than system-prompt-only placement.
**Environment:** Sessions with strict formatting, length, or tone constraints that must hold throughout a conversation.

---

### Option 3 — Sliding window with system prompt copy preserved

```python
import anthropic

client = anthropic.Anthropic()

SYSTEM = """You are a financial advisor. Rules that must never change:
1. Never give specific stock picks or price targets.
2. Always remind users to consult a licensed professional.
3. Quantify risk in every recommendation (low/medium/high).
4. Use USD for all monetary values."""

MAX_TURNS_IN_WINDOW = 10   # keep only the last N turns in context

def chat_with_window(
    full_history: list[dict],
    user_message: str,
) -> tuple[str, list[dict]]:
    full_history = full_history + [{"role": "user", "content": user_message}]

    # Sliding window: keep only the most recent turns
    # Always preserve system prompt (passed separately)
    window = full_history[-MAX_TURNS_IN_WINDOW * 2:]   # *2 because each turn = user+assistant

    # Ensure window starts with a user message
    while window and window[0]["role"] != "user":
        window = window[1:]

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=SYSTEM,
        messages=window,
    )
    reply = response.content[0].text
    full_history = full_history + [{"role": "assistant", "content": reply}]
    print(f"  [window] sending {len(window)} messages (full history: {len(full_history)})")
    return reply, full_history

history = []
topics = [
    "What is a bond?",
    "Should I invest in real estate?",
    "What about international stocks?",
    "How much should I keep in cash?",
    "What are ETFs?",
    "Should I buy gold?",
    "Tell me about index funds.",
    "What is dollar-cost averaging?",
    "How do I diversify my portfolio?",
    "What is a Roth IRA?",
    "Tell me about emerging markets.",   # turn 11 — earlier turns slide out
    "What are dividend stocks?",
]
for question in topics:
    reply, history = chat_with_window(history, question)
    print(f"Q: {question}")
    print(f"A: {reply[:150]}\n")
```

**Expected Token Savings:** Sliding window keeps context size bounded; combined with system prompt always present, constraints never dilute regardless of conversation length.
**Environment:** Long-running sessions where full history would exceed context limits; sliding window is the standard production pattern.

---

### Option 4 — Session summary with preserved rules

```python
import anthropic

client = anthropic.Anthropic()

SYSTEM = """You are a coding tutor. Follow these rules in every response:
- Explain concepts at a beginner level
- Always include a runnable code example
- Use Python 3.10+ syntax
- Point out common mistakes after each example"""

SUMMARY_TRIGGER = 8   # summarise after this many turns

SUMMARISER_SYSTEM = """Summarise the following conversation.
Keep ALL established facts, user goals, and user preferences.
Output format:
SUMMARY: <2-3 sentence summary of what was discussed>
USER_PROFILE: <what we know about this user's level and goals>
TOPICS_COVERED: <comma-separated list of topics>"""

def summarise_history(history: list[dict]) -> str:
    text = "\n".join(f"{m['role'].upper()}: {m['content'][:200]}" for m in history)
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=SUMMARISER_SYSTEM,
        messages=[{"role": "user", "content": text}],
    )
    return response.content[0].text

def chat(
    history: list[dict],
    summary: str,
    user_message: str,
    turn: int,
) -> tuple[str, list[dict], str]:

    # Build context: summary (if exists) + recent turns
    messages = list(history[-6:])  # last 3 turns after compression
    if messages and messages[0]["role"] != "user":
        messages = messages[1:]

    if summary:
        prefix = f"[Session context]\n{summary}\n\n[Recent conversation continues below]"
        if messages:
            messages[0] = {"role": "user", "content": prefix + "\n\n" + messages[0]["content"]}
        else:
            messages = [{"role": "user", "content": prefix + f"\n\nUser: {user_message}"}]

    messages.append({"role": "user", "content": user_message})

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=SYSTEM,
        messages=messages,
    )
    reply = response.content[0].text
    history = history + [
        {"role": "user", "content": user_message},
        {"role": "assistant", "content": reply},
    ]

    # Summarise and compress when history gets long
    if turn > 0 and turn % SUMMARY_TRIGGER == 0:
        print(f"  [summary] compressing at turn {turn}")
        summary = summarise_history(history[:-2])   # exclude just-added pair
        history = history[-4:]  # keep last 2 turns in full

    return reply, history, summary

history = []
summary = ""
topics = [
    "What are Python variables?",
    "How do loops work?",
    "What is a function?",
    "What are lists?",
    "How do I use dictionaries?",
    "What are classes?",
    "What is inheritance?",
    "How does exception handling work?",   # trigger summary
    "What are decorators?",
    "How do I read files?",
]
for i, q in enumerate(topics):
    reply, history, summary = chat(history, summary, q, i)
    print(f"[turn {i}] Q: {q}")
    print(f"A: {reply[:150]}\n")
```

**Expected Token Savings:** Summarisation reduces history from O(N) tokens to O(1) while preserving user context and facts; turns beyond the window cost only summary tokens, not full history tokens.
**Environment:** Long tutoring, coaching, or support sessions where full history compression is acceptable.

---

### Option 5 — Explicit constraint checklist in every request

```python
import anthropic

client = anthropic.Anthropic()

class ConstraintSession:
    """
    Maintains a named constraint set and appends it as a checklist
    to every request so the model never loses track.
    """
    def __init__(self, constraints: list[str]):
        self.constraints = constraints
        self.history: list[dict] = []
        self.system = "You are a helpful assistant. Always follow the active constraints."

    def build_constraint_block(self) -> str:
        lines = ["[Active constraints — check each before responding:"]
        for i, c in enumerate(self.constraints, 1):
            lines.append(f"  {i}. {c}")
        lines.append("]")
        return "\n".join(lines)

    def add_constraint(self, constraint: str) -> None:
        self.constraints.append(constraint)
        print(f"  [constraint added] {constraint!r}")

    def remove_constraint(self, index: int) -> None:
        removed = self.constraints.pop(index)
        print(f"  [constraint removed] {removed!r}")

    def chat(self, user_message: str) -> str:
        full_message = user_message + "\n\n" + self.build_constraint_block()
        self.history.append({"role": "user", "content": full_message})
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            system=self.system,
            messages=self.history,
        )
        reply = response.content[0].text
        self.history.append({"role": "assistant", "content": reply})
        return reply

session = ConstraintSession([
    "Respond only in British English spelling (colour, realise, etc.)",
    "Keep all responses under 4 sentences.",
    "Start every response with a one-word emoji that matches the topic.",
])

turns = [
    "Explain what a neural network is.",
    "What is the difference between supervised and unsupervised learning?",
    "Tell me about natural language processing.",
]
for q in turns:
    print(f"Q: {q}")
    print(f"A: {session.chat(q)[:250]}\n")

# Add a new constraint mid-session
session.add_constraint("Always mention at least one real-world application.")
print(f"Q: What is reinforcement learning?")
print(f"A: {session.chat('What is reinforcement learning?')[:250]}")
```

**Expected Token Savings:** Explicit constraint checklist adds ~50-100 tokens per turn but eliminates the exponentially more expensive correction loops caused by constraint drift.
**Environment:** Sessions where user-defined formatting rules, scope constraints, or persona rules must hold for the entire conversation.

---

### Option 6 — Instruction compliance verifier as a post-processing step

```python
import json
import anthropic

client = anthropic.Anthropic()

CONSTRAINTS = [
    "Response must be in JSON format with keys: 'answer' and 'confidence'.",
    "Confidence must be a float between 0.0 and 1.0.",
    "Answer must be 1-2 sentences maximum.",
    "Do not use the phrase 'I think' or 'I believe'.",
]

SYSTEM = f"""You are a precise Q&A assistant.
Active constraints:
{chr(10).join(f'- {c}' for c in CONSTRAINTS)}"""

VERIFIER_SYSTEM = """Check if the following response obeys all listed constraints.
Return JSON: {{"compliant": true/false, "violations": ["..."]}}"""

def verify_compliance(response_text: str, constraints: list[str]) -> dict:
    check_prompt = f"Constraints:\n{chr(10).join(f'- {c}' for c in constraints)}\n\nResponse:\n{response_text}"
    result = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        system=VERIFIER_SYSTEM,
        messages=[{"role": "user", "content": check_prompt}],
    )
    raw = result.content[0].text.strip().lstrip("```json").rstrip("```").strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"compliant": True, "violations": []}

def ask_verified(history: list[dict], question: str, max_retries: int = 2) -> tuple[str, list[dict]]:
    history = history + [{"role": "user", "content": question}]

    for attempt in range(max_retries + 1):
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            system=SYSTEM,
            messages=history,
        )
        reply = response.content[0].text

        check = verify_compliance(reply, CONSTRAINTS)
        if check["compliant"]:
            break
        print(f"  [verifier] attempt {attempt + 1} non-compliant: {check['violations']}")
        if attempt < max_retries:
            violation_note = f"[Previous response violated: {'; '.join(check['violations'])}. Please retry, following ALL constraints exactly.]"
            history = history + [
                {"role": "assistant", "content": reply},
                {"role": "user",      "content": violation_note},
            ]

    history = list(history)
    # Clean history: only keep the original question and final answer
    history = [h for h in history if not h["content"].startswith("[Previous response violated")]
    history = history + [{"role": "assistant", "content": reply}]
    return reply, history

history = []
questions = [
    "What is photosynthesis?",
    "How does the internet work?",
    "What causes climate change?",
]
for q in questions:
    reply, history = ask_verified(history, q)
    print(f"Q: {q}\nA: {reply[:300]}\n")
```

**Expected Token Savings:** Verifier catches non-compliance immediately after each turn; one verifier call (~60 tokens) prevents user-visible constraint violations that would require multiple correction turns.
**Environment:** Structured output pipelines where format compliance is mandatory; verifier acts as a quality gate before response delivery.

---

## Comparison

| Option | Mechanism | Handles Very Long Sessions | Token Overhead | Best For |
|---|---|---|---|---|
| 1. Periodic reminder injection | Re-anchors every N turns | Yes | Low | General long-session agents |
| 2. Constraint pinning (end of message) | Recency bias | Moderate | None | Format/tone constraints |
| 3. Sliding window | Bounds context size | Yes | None | Production agents with context limits |
| 4. Summarise + compress | Compresses history | Yes | Medium (summariser call) | Tutoring, coaching, support sessions |
| 5. Constraint checklist per turn | Explicit per-turn checklist | Yes | Low | User-configurable multi-constraint sessions |
| 6. Compliance verifier | Post-generation check | No | Medium (verifier call) | Structured output, format-critical agents |
