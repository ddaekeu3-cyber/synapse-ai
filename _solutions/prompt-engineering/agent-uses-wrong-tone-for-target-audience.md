---
layout: solution
title: "Agent uses wrong tone for target audience"
category: prompt-engineering
description: "Agent responds with developer jargon to non-technical users, or over-simplifies for experts. Mismatched tone erodes trust, increases clarification turns, and makes outputs unusable without manual rewriting."
tags: [prompt-engineering, persona, tone, audience, system-prompt, user-experience]
---

## Symptom

Non-technical users receive responses filled with stack traces, API terminology, and code snippets. Technical users receive over-explained, condescending summaries of concepts they already know. Both groups need follow-up turns to extract the information they actually need — burning tokens and frustrating users.

## Root Cause

The system prompt either omits tone/audience instructions entirely, or has a single generic "be helpful" directive. The model defaults to a technical middle-ground that satisfies neither experts nor beginners. Without an explicit audience signal, the model cannot adapt.

## Fix

Inject the target audience and tone profile into the system prompt. For multi-audience products, detect or accept the user's expertise level and route to a matching prompt variant. For single-audience products, write a precise persona description once and test it against real user messages.

---

### Option 1 — Explicit audience tier in system prompt

```python
import anthropic
from enum import Enum

client = anthropic.Anthropic(api_key="sk-live-...")


class AudienceTier(Enum):
    BEGINNER = "beginner"
    INTERMEDIATE = "intermediate"
    EXPERT = "expert"


AUDIENCE_SYSTEM_PROMPTS: dict[AudienceTier, str] = {
    AudienceTier.BEGINNER: (
        "You are a friendly, patient assistant helping people who are new to technology. "
        "Use plain language. Avoid jargon. When you must use a technical term, explain it "
        "in parentheses on first use. Prefer analogies and step-by-step instructions. "
        "Never assume prior knowledge. Encourage the user when they're doing well."
    ),
    AudienceTier.INTERMEDIATE: (
        "You are a knowledgeable assistant for users with some technical background. "
        "You can use standard technical terms without over-explaining them. "
        "Be concise but complete. Offer to elaborate on any point if needed. "
        "Assume familiarity with basic concepts but explain advanced ones briefly."
    ),
    AudienceTier.EXPERT: (
        "You are a peer-level technical advisor. Use precise terminology. "
        "Skip basics. Lead with the answer, not the setup. "
        "Prefer code examples over prose. Reference standards, RFCs, and docs by name. "
        "Be direct — the user values accuracy and density over politeness."
    ),
}


def run_agent(user_message: str, tier: AudienceTier = AudienceTier.INTERMEDIATE) -> str:
    system = AUDIENCE_SYSTEM_PROMPTS[tier]
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text


# Same question, three tones
question = "Why is my Python script slow?"
for tier in AudienceTier:
    print(f"\n=== {tier.value.upper()} ===")
    print(run_agent(question, tier))
```

**Expected Token Savings:** Fewer clarification turns; expert users don't need the explanation re-explained; beginners don't need to ask "what does that mean?"
**Environment:** Any product with a known, homogeneous audience; set the tier once in the system prompt and leave it.

---

### Option 2 — Auto-detect expertise from the user's first message

```python
import anthropic

client = anthropic.Anthropic(api_key="sk-live-...")

DETECTION_SYSTEM = (
    "Classify the technical expertise level of the following user message. "
    "Reply with exactly one word: beginner, intermediate, or expert. "
    "Base your answer on vocabulary, specificity, and assumed context in the message."
)

TONE_PROMPTS = {
    "beginner": "Explain clearly using simple language, analogies, and step-by-step guidance. Avoid jargon.",
    "intermediate": "Be concise and accurate. Use standard terminology. Offer depth on request.",
    "expert": "Lead with the answer. Use precise technical terms. Skip fundamentals. Prefer code over prose.",
}


def detect_expertise(user_message: str) -> str:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=10,
        system=DETECTION_SYSTEM,
        messages=[{"role": "user", "content": user_message}],
    )
    level = response.content[0].text.strip().lower()
    return level if level in TONE_PROMPTS else "intermediate"


def run_agent(user_message: str) -> str:
    level = detect_expertise(user_message)
    print(f"Detected expertise: {level}")

    system = (
        f"You are a helpful assistant. Tone guidance: {TONE_PROMPTS[level]}"
    )
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text


# Test with different message styles
messages = [
    "how do i make my code faster",
    "What's the best approach for reducing GC pressure in a high-throughput JVM service?",
    "I heard Python is slow, is that true?",
]
for msg in messages:
    print(f"\nQ: {msg}")
    print(run_agent(msg))
```

**Expected Token Savings:** Detection costs ~30 Haiku tokens; prevents 1–3 clarification turns that each cost 500–1000 tokens.
**Environment:** General-purpose assistants with a wide user base; detection runs once per session and the result can be cached.

---

### Option 3 — User-declared preference with persistent session state

```python
import anthropic
from dataclasses import dataclass, field

client = anthropic.Anthropic(api_key="sk-live-...")


@dataclass
class UserSession:
    user_id: str
    preferred_tone: str = "intermediate"   # beginner | intermediate | expert | casual
    messages: list[dict] = field(default_factory=list)


# Session store (replace with Redis or DB in production)
SESSIONS: dict[str, UserSession] = {}


TONE_DESCRIPTIONS = {
    "beginner": (
        "Speak plainly. Avoid technical jargon. Use analogies. "
        "Be patient and encouraging. Explain every term."
    ),
    "intermediate": (
        "Use standard technical vocabulary without over-explaining. "
        "Be accurate and reasonably concise."
    ),
    "expert": (
        "Be terse and precise. Lead with the answer. "
        "Use industry-standard terminology. Skip basics entirely."
    ),
    "casual": (
        "Be conversational and friendly. Light humor is fine. "
        "Keep responses short unless asked to elaborate."
    ),
}


def update_tone(session: UserSession, message: str) -> str | None:
    """Check if the user's message contains a tone preference update."""
    lower = message.lower()
    for tone in TONE_DESCRIPTIONS:
        if f"explain like {tone}" in lower or f"speak as {tone}" in lower or f"tone: {tone}" in lower:
            session.preferred_tone = tone
            return tone
    return None


def run_turn(user_id: str, user_message: str) -> str:
    session = SESSIONS.setdefault(user_id, UserSession(user_id=user_id))

    # Check for in-message tone override
    new_tone = update_tone(session, user_message)
    if new_tone:
        return f"Understood — I'll adjust to {new_tone} mode from now on."

    tone_desc = TONE_DESCRIPTIONS[session.preferred_tone]
    system = f"You are a helpful assistant. Communication style: {tone_desc}"

    session.messages.append({"role": "user", "content": user_message})
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=system,
        messages=session.messages,
    )
    reply = response.content[0].text
    session.messages.append({"role": "assistant", "content": reply})
    return reply
```

**Expected Token Savings:** None on token count; saves clarification turns and improves user satisfaction, which reduces session length.
**Environment:** Products with returning users and persistent sessions; allow users to set their tone preference at onboarding or inline.

---

### Option 4 — Domain-specific tone profiles (not just expertise level)

```python
import anthropic
from dataclasses import dataclass

client = anthropic.Anthropic(api_key="sk-live-...")


@dataclass
class ToneProfile:
    name: str
    system_directive: str


TONE_PROFILES: dict[str, ToneProfile] = {
    "medical_patient": ToneProfile(
        "medical_patient",
        "You are a medical information assistant speaking with a patient (not a clinician). "
        "Use plain language. Avoid Latin medical terms unless you define them. "
        "Always recommend consulting a doctor for personal medical decisions. "
        "Be empathetic and reassuring without making diagnoses.",
    ),
    "medical_professional": ToneProfile(
        "medical_professional",
        "You are assisting a licensed clinician. Use clinical terminology freely. "
        "Reference drug names by generic and brand names. "
        "Cite dosing guidelines and contraindications precisely. "
        "Be direct — the clinician needs accurate information quickly.",
    ),
    "legal_layperson": ToneProfile(
        "legal_layperson",
        "You assist non-lawyers with legal questions. "
        "Translate legal concepts into plain English. "
        "Always note that this is general information, not legal advice. "
        "Explain rights and options clearly without implying certainty about outcomes.",
    ),
    "legal_professional": ToneProfile(
        "legal_professional",
        "You assist lawyers and paralegals. Use legal terminology precisely. "
        "Reference relevant statutes, case law, and jurisdictional differences. "
        "Be concise. The reader is trained — skip basics.",
    ),
    "software_developer": ToneProfile(
        "software_developer",
        "You assist software engineers. Prefer code examples over prose. "
        "Use correct technical terminology. Reference official docs and RFCs. "
        "Be direct. Lead with the solution, then the explanation.",
    ),
    "non_technical_manager": ToneProfile(
        "non_technical_manager",
        "You assist business managers without technical backgrounds. "
        "Translate technical concepts into business impact and plain language. "
        "Focus on 'what it means' rather than 'how it works'. "
        "Use bullet points and summaries. Avoid code.",
    ),
}


def run_agent(user_message: str, profile_key: str = "software_developer") -> str:
    profile = TONE_PROFILES.get(profile_key, TONE_PROFILES["software_developer"])
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=profile.system_directive,
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text
```

**Expected Token Savings:** Domain-appropriate responses are accepted on first delivery; no reformulation needed.
**Environment:** Vertical SaaS products (legal tech, health tech, dev tools) where the user base is well-defined by domain and role.

---

### Option 5 — Multi-turn tone consistency enforcement

```python
import anthropic

client = anthropic.Anthropic(api_key="sk-live-...")

TONE_ANCHORS: dict[str, str] = {
    "simple": (
        "TONE RULE: Always use simple words (grade 6 reading level). "
        "Never use words like 'utilize', 'leverage', 'implement', or 'instantiate'. "
        "Use short sentences. If a sentence exceeds 20 words, break it in two."
    ),
    "concise": (
        "TONE RULE: Be maximally concise. No filler phrases like 'Great question' or "
        "'Certainly!'. No trailing summaries. Get to the point immediately."
    ),
    "formal": (
        "TONE RULE: Maintain formal, professional language at all times. "
        "No contractions. No colloquialisms. No humor. "
        "Use complete sentences. Refer to the user as 'you'."
    ),
    "conversational": (
        "TONE RULE: Be warm and conversational. Contractions are fine. "
        "Match the user's energy. Short responses are better than long ones."
    ),
}


def make_system_prompt(tone_key: str, base_prompt: str) -> str:
    anchor = TONE_ANCHORS.get(tone_key, "")
    return f"{base_prompt}\n\n{anchor}" if anchor else base_prompt


class ToneConsistentAgent:
    def __init__(self, tone: str, base_prompt: str = "You are a helpful assistant.") -> None:
        self.system = make_system_prompt(tone, base_prompt)
        self.messages: list[dict] = []
        self.tone = tone

    def chat(self, user_message: str) -> str:
        self.messages.append({"role": "user", "content": user_message})
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=self.system,
            messages=self.messages,
        )
        reply = response.content[0].text
        self.messages.append({"role": "assistant", "content": reply})
        return reply


# Usage
agent = ToneConsistentAgent(tone="concise")
print(agent.chat("Explain what a database index is."))
print(agent.chat("When should I use one?"))
```

**Expected Token Savings:** Tone anchors prevent verbose filler phrases; "concise" mode typically reduces response length by 20–40 %.
**Environment:** Any agent with a specific communication style requirement; the TONE RULE directive outlasts context compression better than vague "be concise" instructions.

---

### Option 6 — Real-time tone feedback loop: detect and correct drift

```python
import anthropic
import re

client = anthropic.Anthropic(api_key="sk-live-...")

JARGON_WORDS = {
    "instantiate", "utilize", "leverage", "paradigm", "synergy",
    "refactor", "idempotent", "ephemeral", "polymorphism",
    "abstraction", "encapsulation", "middleware", "microservice",
}

LONG_WORD_THRESHOLD = 12   # characters


def tone_score(text: str) -> dict:
    """Heuristic score for technical complexity of a response."""
    words = re.findall(r"\b[a-zA-Z]+\b", text.lower())
    jargon_count = sum(1 for w in words if w in JARGON_WORDS)
    long_words = sum(1 for w in words if len(w) > LONG_WORD_THRESHOLD)
    code_blocks = len(re.findall(r"```", text)) // 2
    sentences = len(re.split(r"[.!?]+", text))
    avg_sentence_len = len(words) / max(sentences, 1)

    return {
        "jargon_count": jargon_count,
        "long_words": long_words,
        "code_blocks": code_blocks,
        "avg_sentence_len": round(avg_sentence_len, 1),
        "total_words": len(words),
    }


def is_too_technical(score: dict, target: str) -> bool:
    if target == "beginner":
        return score["jargon_count"] > 1 or score["code_blocks"] > 0 or score["avg_sentence_len"] > 18
    return False


def run_agent_with_tone_check(user_message: str, target_audience: str = "beginner") -> str:
    system = (
        "You are a friendly assistant helping non-technical users. "
        "Use plain language. No code. No jargon. Short sentences."
    )
    messages = [{"role": "user", "content": user_message}]

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=system,
        messages=messages,
    )
    output = response.content[0].text
    score = tone_score(output)

    if is_too_technical(score, target_audience):
        print(f"Tone drift detected: {score} — requesting simplification")
        messages.append({"role": "assistant", "content": output})
        messages.append({
            "role": "user",
            "content": (
                "Please rewrite your last response in simpler language. "
                "Remove any technical terms or code. "
                "Use short sentences that a 12-year-old could understand."
            ),
        })
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=system,
            messages=messages,
        )
        output = response.content[0].text

    return output


# Comparison table
# | Option | Tone Source | Adapts Per User | Extra Cost |
# |--------|------------|----------------|------------|
# | 1 Explicit tier | Enum param | No | None |
# | 2 Auto-detect | Haiku classifier | Yes (per session) | ~30 tok |
# | 3 User-declared | Session preference | Yes (user-driven) | None |
# | 4 Domain profile | Profile key | No (per deployment) | None |
# | 5 TONE anchors | Rule injection | No | None |
# | 6 Drift detector | Heuristic score | Yes (per response) | 0–1 extra turn |
```

**Expected Token Savings:** Correction turns add ~300 tokens; but eliminating 2–3 follow-up clarification turns saves more; overall positive ROI for high-stakes user-facing deployments.
**Environment:** Consumer products where a wrong tone causes user churn; the heuristic score is cheap and catches the most common violations (code blocks, jargon) reliably.
