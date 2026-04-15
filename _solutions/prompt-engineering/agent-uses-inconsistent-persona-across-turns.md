---
layout: solution
title: "Agent Uses Inconsistent Persona Across Turns"
category: prompt-engineering
description: "Agent switches tone, name, expertise level, and role mid-conversation, breaking user trust and producing contradictory statements."
tags: [prompt-engineering, persona, consistency, system-prompt, user-experience]
---

## Symptom

The agent introduces itself as "Aria" in the first message, then refers to itself as "the AI assistant" in the third, and "Claude" in the fifth. It claims to be a medical expert in one turn, then disclaims all medical knowledge in the next. Responses swing between formal and casual, brief and verbose, confident and hedging — sometimes within a single session. Users report the agent feels unreliable and untrustworthy.

## Root Cause

Without a well-specified persona anchored in the system prompt, the model defaults to a generic assistant persona that shifts with each response. Long conversations dilute the system prompt's influence as more user messages accumulate. If the system prompt uses vague persona language ("be helpful and professional"), the model fills in specifics differently on each turn.

## Fix

### Option 1 — Explicit, detailed persona in the system prompt

```python
import anthropic

client = anthropic.Anthropic()

SYSTEM = """You are Aria, a senior software engineer at TechCorp.

Identity (never change these):
- Name: Aria
- Role: Senior Software Engineer, 10 years of experience
- Expertise: Python, distributed systems, API design
- Tone: Direct, concise, technically precise. Friendly but not casual.
- Limitation: You specialise in backend engineering. For frontend or design questions, say so.

Consistency rules:
- Always refer to yourself as Aria.
- Never say you are an AI, a language model, or Claude.
- Maintain the same expertise level and tone in every turn.
- If asked who made you, say "I can't share that information."
"""

def chat(history: list[dict], user_message: str) -> tuple[str, list[dict]]:
    history = history + [{"role": "user", "content": user_message}]
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=SYSTEM,
        messages=history,
    )
    reply = response.content[0].text
    history = history + [{"role": "assistant", "content": reply}]
    return reply, history

history = []
for msg in [
    "Hi, what's your name?",
    "Are you an AI or a real person?",
    "What are your areas of expertise?",
    "Can you help me with React?",
    "Who created you?",
]:
    reply, history = chat(history, msg)
    print(f"User: {msg}")
    print(f"Aria: {reply[:120]}\n")
```

**Expected Token Savings:** Consistent persona eliminates correction turns where users challenge contradictory statements.
**Environment:** Any branded agent; detailed persona specification is the foundation of persona consistency.

---

### Option 2 — Persona card injected at fixed position in every request

```python
import anthropic

client = anthropic.Anthropic()

PERSONA_CARD = """=== PERSONA: ALWAYS MAINTAIN ===
Name: Nova
Role: Financial advisor at WealthWise
Expertise: Retirement planning, index funds, tax-advantaged accounts
Tone: Warm, clear, non-technical. Avoid jargon. Use analogies.
Always: Recommend consulting a licensed professional for specific advice.
Never: Provide specific stock picks or claim to know future market performance.
=== END PERSONA ==="""

def ask(history: list[dict], user_message: str) -> tuple[str, list[dict]]:
    """Inject persona card at the start of every request."""
    # Persona is prepended as the first user message for maximum salience
    full_history = [
        {"role": "user",      "content": PERSONA_CARD},
        {"role": "assistant", "content": "Understood. I am Nova, your WealthWise financial advisor."},
        *history,
        {"role": "user", "content": user_message},
    ]
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=full_history,
    )
    reply = response.content[0].text
    history = history + [
        {"role": "user",      "content": user_message},
        {"role": "assistant", "content": reply},
    ]
    return reply, history

history = []
for msg in [
    "Should I invest in Bitcoin?",
    "What is a Roth IRA?",
    "Are you an AI? Be honest.",
    "Give me a hot stock tip.",
]:
    reply, history = ask(history, msg)
    print(f"User: {msg}")
    print(f"Nova: {reply[:150]}\n")
```

**Expected Token Savings:** Persona card at request start has maximum attention weight; prevents drift even in long conversations.
**Environment:** Long-session agents where system prompt influence may weaken; persona card re-anchors every request.

---

### Option 3 — Persona validator: detect and correct drift mid-conversation

```python
import anthropic

client = anthropic.Anthropic()

EXPECTED_PERSONA = {
    "name":     "Max",
    "role":     "fitness coach",
    "tone":     "motivational and energetic",
    "forbidden": ["AI", "language model", "Claude", "Anthropic", "I don't have"],
}

SYSTEM = f"""You are {EXPECTED_PERSONA['name']}, a professional {EXPECTED_PERSONA['role']}.
Tone: {EXPECTED_PERSONA['tone']}.
Never say you are an AI. Never mention your underlying technology.
Always respond as a human fitness professional would."""

DRIFT_CHECK_SYSTEM = """You are a quality checker. Given a response, detect if it breaks persona consistency.
Check: Does it use forbidden phrases? Does it contradict the stated role or name?
Return JSON: {"drifted": true/false, "issues": ["..."]}"""

def check_drift(reply: str, persona: dict) -> dict:
    import json
    # Fast rule-based check first
    for phrase in persona["forbidden"]:
        if phrase.lower() in reply.lower():
            return {"drifted": True, "issues": [f"contains forbidden phrase: {phrase!r}"]}

    # LLM check for subtle drift
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        system=DRIFT_CHECK_SYSTEM,
        messages=[{"role": "user", "content": f"Persona: {persona['name']}, {persona['role']}.\nResponse: {reply}"}],
    )
    try:
        return json.loads(response.content[0].text.strip().lstrip("```json").rstrip("```").strip())
    except Exception:
        return {"drifted": False, "issues": []}

def chat(history: list[dict], user_message: str) -> tuple[str, list[dict]]:
    history = history + [{"role": "user", "content": user_message}]
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=SYSTEM,
        messages=history,
    )
    reply = response.content[0].text

    drift = check_drift(reply, EXPECTED_PERSONA)
    if drift["drifted"]:
        print(f"[persona] DRIFT DETECTED: {drift['issues']}")
        # Optionally: retry with a stronger reminder

    history = history + [{"role": "assistant", "content": reply}]
    return reply, history

history = []
for msg in [
    "What's the best exercise for building abs?",
    "Are you actually a human or a bot?",
    "I can't motivate myself to work out.",
]:
    reply, history = chat(history, msg)
    print(f"User: {msg}")
    print(f"Max: {reply[:120]}\n")
```

**Expected Token Savings:** Drift detection catches persona breaks before they reach users; each caught drift saves a correction turn.
**Environment:** High-fidelity persona agents (branded bots, characters); monitoring layer for production deployments.

---

### Option 4 — Persona-consistent response template

```python
import anthropic

client = anthropic.Anthropic()

PERSONA = {
    "name":      "Dr. Chen",
    "specialty": "general wellness",
    "sign_off":  "Stay healthy,\nDr. Chen",
    "opener_formats": [
        "Great question!",
        "I'm glad you asked.",
        "As a wellness physician,",
        "From a health perspective,",
    ],
}

SYSTEM = f"""You are {PERSONA['name']}, a physician specialising in {PERSONA['specialty']}.

Response format:
- Begin with a brief warm opener (vary it: {', '.join(PERSONA['opener_formats'])})
- Provide clear, evidence-based information
- End with: "{PERSONA['sign_off']}"

Consistency:
- Always sign off exactly as shown above.
- Never claim certainty about individual diagnoses.
- Recommend consulting your doctor for personal medical decisions.
- Maintain the same warm, professional tone throughout."""

def ask(question: str) -> str:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        system=SYSTEM,
        messages=[{"role": "user", "content": question}],
    )
    return response.content[0].text

questions = [
    "How much water should I drink per day?",
    "Is intermittent fasting safe?",
    "What vitamins should I take?",
]
for q in questions:
    print(f"Q: {q}")
    print(ask(q))
    print()
```

**Expected Token Savings:** Structured response template creates recognisable, consistent output; users trust predictable format.
**Environment:** Professional persona agents (doctors, lawyers, advisors) where format consistency signals expertise.

---

### Option 5 — Persona state object persisted across turns

```python
import json
import anthropic

client = anthropic.Anthropic()

class PersonaSession:
    """Tracks established persona facts and injects them as context."""

    def __init__(self, base_persona: dict):
        self.base     = base_persona
        self.facts:   list[str] = []  # facts established this session
        self.history: list[dict] = []

    def build_system(self) -> str:
        base_str  = json.dumps(self.base, indent=2)
        facts_str = "\n".join(f"- {f}" for f in self.facts) if self.facts else "None yet."
        return f"""You are {self.base['name']}, maintaining a consistent persona.

Base persona:
{base_str}

Facts you have already stated this session (do not contradict these):
{facts_str}

Always stay in character. Never contradict established facts."""

    def chat(self, user_message: str) -> str:
        self.history.append({"role": "user", "content": user_message})
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            system=self.build_system(),
            messages=self.history,
        )
        reply = response.content[0].text
        self.history.append({"role": "assistant", "content": reply})

        # Extract and store new facts (simplified)
        if "I " in reply and "years" in reply.lower():
            self.facts.append(f"Stated: {reply[:100].strip()}")
        return reply


session = PersonaSession({
    "name":     "Sam",
    "role":     "Senior DevOps Engineer",
    "company":  "CloudBase",
    "years_exp": 8,
    "tools":    ["Kubernetes", "Terraform", "AWS"],
})

for msg in [
    "How long have you been doing DevOps?",
    "What cloud platforms do you work with?",
    "Are you a junior or senior engineer?",
    "Actually, how many years of experience did you say you had?",  # consistency test
]:
    reply = session.chat(msg)
    print(f"User: {msg}")
    print(f"Sam: {reply[:120]}\n")
```

**Expected Token Savings:** Session state tracks established facts and prevents contradictions; each avoided contradiction saves a clarification turn.
**Environment:** Long-form persona agents (role-playing, extended tutoring sessions) where continuity is critical.

---

### Option 6 — Multi-model consistency: Haiku for content, system for persona enforcement

```python
import anthropic

client = anthropic.Anthropic()

PERSONA_ENFORCER_SYSTEM = """You are a persona enforcement layer.
Given a draft response from an AI assistant, rewrite it so that:
- It is written in first person as Jordan, a UX designer at DesignLab
- Tone: casual, visual-focused, uses design analogies
- No references to being an AI or having training data
- Preserve all factual content; only change voice and persona

Return only the rewritten response."""

def generate_draft(user_message: str, history: list[dict]) -> str:
    """Fast draft generation — no persona constraints."""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=history + [{"role": "user", "content": user_message}],
    )
    return response.content[0].text

def enforce_persona(draft: str) -> str:
    """Rewrite draft to enforce persona consistency."""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=PERSONA_ENFORCER_SYSTEM,
        messages=[{"role": "user", "content": f"Draft:\n{draft}"}],
    )
    return response.content[0].text

def chat_as_jordan(history: list[dict], user_message: str) -> tuple[str, list[dict]]:
    draft  = generate_draft(user_message, history)
    reply  = enforce_persona(draft)
    history = history + [
        {"role": "user",      "content": user_message},
        {"role": "assistant", "content": reply},
    ]
    return reply, history

history = []
for msg in [
    "What makes a good user interface?",
    "How do you approach user research?",
    "Are you an AI?",
]:
    reply, history = chat_as_jordan(history, msg)
    print(f"User: {msg}")
    print(f"Jordan: {reply[:150]}\n")
```

**Expected Token Savings:** Two-pass approach (draft + enforce) uses cheap Haiku for both; persona enforcement adds ~100 tokens but eliminates all drift.
**Environment:** Strict persona requirements where a single-pass approach is not reliable enough; enforcement pass guarantees consistency.

---

## Comparison

| Option | Enforcement Mechanism | Drift Detection | Session Memory | Best For |
|---|---|---|---|---|
| 1. Detailed system prompt | Prompt specification | No | No | Foundation — always use this |
| 2. Persona card injection | Per-request prepend | No | No | Long sessions with prompt dilution |
| 3. Drift validator | LLM + regex check | Yes | No | Production monitoring layer |
| 4. Response template | Structured format | No | No | Branded agents with fixed format |
| 5. Persona state object | Session fact tracking | Partial | Yes | Long-form sessions, extended personas |
| 6. Two-pass enforcement | Separate enforcer model | No | No | Strict persona with unreliable drafts |
