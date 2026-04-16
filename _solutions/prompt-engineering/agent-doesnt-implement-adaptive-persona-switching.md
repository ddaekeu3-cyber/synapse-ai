---
layout: solution
title: "Agent Doesn't Implement Adaptive Persona Switching"
category: prompt-engineering
description: "Detect the user's context, expertise level, and intent, then dynamically switch the agent's communication persona — tone, vocabulary, formality, and depth — to maximize clarity and engagement."
tags: [prompt-engineering, persona, adaptive, tone, ux, dynamic-prompts, personalization]
---

# Agent Doesn't Implement Adaptive Persona Switching

## Problem

A single fixed system prompt forces the agent to respond the same way to a 10-year-old asking about dinosaurs and a PhD researcher asking about cladistics. The expert is patronized; the child is confused. Adaptive persona switching detects user context and adjusts communication style dynamically — vocabulary, depth, formality, and examples — without changing the agent's core knowledge or values.

## Solution Options

### Option 1: Expertise-Level Classifier with Persona Select

```python
import anthropic
from enum import Enum


class ExpertiseLevel(Enum):
    BEGINNER     = "beginner"
    INTERMEDIATE = "intermediate"
    EXPERT       = "expert"


PERSONAS: dict[ExpertiseLevel, str] = {
    ExpertiseLevel.BEGINNER: (
        "You are a friendly, patient teacher. Use simple words and everyday analogies. "
        "Avoid jargon. Keep sentences short. Always give a concrete example. "
        "End with an encouraging check-in like 'Does that make sense?'"
    ),
    ExpertiseLevel.INTERMEDIATE: (
        "You are a knowledgeable colleague. Use standard technical terms but define niche ones. "
        "Provide both the concept and its practical implications. "
        "Assume the user understands basics but may not know advanced details."
    ),
    ExpertiseLevel.EXPERT: (
        "You are a peer expert. Use precise technical terminology without definition. "
        "Skip introductory context. Engage with nuance, edge cases, and current research. "
        "Reference specific frameworks, algorithms, or papers when relevant."
    ),
}


def classify_expertise(client: anthropic.Anthropic, user_message: str) -> ExpertiseLevel:
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=10,
        messages=[{
            "role": "user",
            "content": (
                f"Classify the user's expertise level based on their message. "
                f"Reply with exactly one word: beginner, intermediate, or expert.\n\nMessage: {user_message}"
            ),
        }],
    )
    word = resp.content[0].text.strip().lower()
    try:
        return ExpertiseLevel(word)
    except ValueError:
        return ExpertiseLevel.INTERMEDIATE


def adaptive_agent(user_message: str) -> str:
    client = anthropic.Anthropic()
    level = classify_expertise(client, user_message)
    persona = PERSONAS[level]

    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        system=persona,
        messages=[{"role": "user", "content": user_message}],
    )
    print(f"[persona] Detected: {level.value}")
    return resp.content[0].text


if __name__ == "__main__":
    messages = [
        "What is machine learning?",
        "How does gradient descent converge in non-convex loss landscapes?",
        "Can you explain what AI does?",
    ]
    for msg in messages:
        print(f"\nQ: {msg}")
        print(f"A: {adaptive_agent(msg)[:200]}")

# Expected Token Savings: Classification adds ~20 tokens; avoids over-explaining to experts or under-explaining to beginners
# Environment: Public-facing agents serving mixed audiences from students to professionals
```

---

### Option 2: Multi-Signal Persona Detector

```python
import anthropic
from dataclasses import dataclass
from enum import Enum


class Tone(Enum):
    FORMAL      = "formal"
    CASUAL      = "casual"
    TECHNICAL   = "technical"
    EDUCATIONAL = "educational"
    EMPATHETIC  = "empathetic"


@dataclass
class UserProfile:
    tone: Tone
    expertise: str     # "novice" | "competent" | "expert"
    intent: str        # "learn" | "solve" | "explore" | "vent"
    age_group: str     # "child" | "teen" | "adult" | "senior"


TONE_SYSTEMS: dict[Tone, str] = {
    Tone.FORMAL: (
        "Maintain a professional, formal register. Use complete sentences, precise terminology, "
        "and structured responses. Avoid contractions and colloquialisms."
    ),
    Tone.CASUAL: (
        "Be conversational and relaxed. Use contractions, friendly language, and a warm tone. "
        "It's fine to use common expressions. Keep it natural and approachable."
    ),
    Tone.TECHNICAL: (
        "Prioritize technical accuracy. Use domain-specific terminology correctly. "
        "Include implementation details, trade-offs, and code examples when relevant. "
        "Assume comfort with complexity."
    ),
    Tone.EDUCATIONAL: (
        "Act as a patient educator. Build from first principles. Use analogies, step-by-step "
        "explanations, and check for understanding. Encourage curiosity."
    ),
    Tone.EMPATHETIC: (
        "Lead with empathy and validation. Acknowledge feelings before providing information. "
        "Use supportive, non-judgmental language. Focus on the human dimension of the problem."
    ),
}


def detect_profile(client: anthropic.Anthropic, message: str, history: list[dict]) -> UserProfile:
    history_str = "\n".join(f"{m['role']}: {m['content'][:60]}" for m in history[-3:])
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=60,
        messages=[{
            "role": "user",
            "content": (
                f"Analyze this message and conversation history. "
                f"Reply with JSON: {{\"tone\":\"formal|casual|technical|educational|empathetic\","
                f"\"expertise\":\"novice|competent|expert\","
                f"\"intent\":\"learn|solve|explore|vent\","
                f"\"age_group\":\"child|teen|adult|senior\"}}\n\n"
                f"Message: {message}\nHistory:\n{history_str}"
            ),
        }],
    )
    import json
    try:
        data = json.loads(resp.content[0].text.strip())
        return UserProfile(
            tone=Tone(data.get("tone", "casual")),
            expertise=data.get("expertise", "competent"),
            intent=data.get("intent", "learn"),
            age_group=data.get("age_group", "adult"),
        )
    except (json.JSONDecodeError, ValueError):
        return UserProfile(tone=Tone.CASUAL, expertise="competent", intent="learn", age_group="adult")


def build_system(profile: UserProfile) -> str:
    tone_instr = TONE_SYSTEMS[profile.tone]
    expertise_note = {
        "novice":    "Avoid jargon. Define terms. Use simple examples.",
        "competent": "Assume basic knowledge. Explain niche concepts.",
        "expert":    "Use technical depth. Skip basics.",
    }.get(profile.expertise, "")
    intent_note = {
        "learn":   "Structure your response to build understanding.",
        "solve":   "Get to the solution quickly with clear steps.",
        "explore": "Share multiple perspectives and possibilities.",
        "vent":    "Prioritize emotional acknowledgment over information.",
    }.get(profile.intent, "")
    return f"{tone_instr}\n\n{expertise_note}\n{intent_note}".strip()


def adaptive_multi_signal(history: list[dict], new_message: str) -> str:
    client = anthropic.Anthropic()
    profile = detect_profile(client, new_message, history)
    system = build_system(profile)
    print(f"[persona] tone={profile.tone.value} expertise={profile.expertise} intent={profile.intent}")

    messages = history + [{"role": "user", "content": new_message}]
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=system,
        messages=messages,
    )
    return resp.content[0].text


if __name__ == "__main__":
    scenarios = [
        ([], "I'm so frustrated, nothing in this codebase makes sense and I don't know where to start"),
        ([], "What's the time complexity of QuickSort's average case and why?"),
        ([], "My 8-year-old wants to know why the sky is blue"),
        ([], "Draft a formal memo declining a vendor proposal"),
    ]
    for history, msg in scenarios:
        print(f"\nQ: {msg[:60]}")
        print(f"A: {adaptive_multi_signal(history, msg)[:200]}")

# Expected Token Savings: One classification call (~40 tokens) per session open; reused for all turns
# Environment: General-purpose assistants needing to serve diverse user demographics
```

---

### Option 3: Persona Persistence Across Conversation Turns

```python
import anthropic
from dataclasses import dataclass, field


@dataclass
class ActivePersona:
    name: str
    system_prompt: str
    confidence: float
    turn_assigned: int


PERSONA_CATALOG = {
    "tutor": {
        "name": "Tutor",
        "prompt": "You are a patient, encouraging tutor. Build understanding step by step. "
                  "Use the Socratic method: ask guiding questions rather than giving direct answers. "
                  "Celebrate progress and reframe mistakes as learning opportunities.",
    },
    "mentor": {
        "name": "Mentor",
        "prompt": "You are an experienced mentor. Share wisdom from experience. "
                  "Give honest, direct feedback. Challenge assumptions constructively. "
                  "Focus on long-term growth over short-term comfort.",
    },
    "analyst": {
        "name": "Analyst",
        "prompt": "You are a rigorous analyst. Provide data-driven, structured responses. "
                  "Quantify trade-offs where possible. Acknowledge uncertainty explicitly. "
                  "Separate facts from interpretations.",
    },
    "creative": {
        "name": "Creative Partner",
        "prompt": "You are an imaginative creative partner. Embrace unconventional ideas. "
                  "Build on the user's concepts enthusiastically. Use vivid language and metaphors. "
                  "Prioritize inspiration over caution.",
    },
    "support": {
        "name": "Support Agent",
        "prompt": "You are a calm, empathetic support agent. Listen first, then help. "
                  "Validate feelings. Provide clear, actionable next steps. "
                  "Escalate gracefully when the problem is beyond your scope.",
    },
}


class PersonaManager:
    def __init__(self) -> None:
        self._client = anthropic.Anthropic()
        self._active: ActivePersona | None = None
        self._history: list[dict] = []
        self._turn = 0
        self._REASSIGN_EVERY = 5  # re-evaluate persona every 5 turns

    def _select_persona(self, message: str) -> ActivePersona:
        names = list(PERSONA_CATALOG.keys())
        resp = self._client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=20,
            messages=[{
                "role": "user",
                "content": (
                    f"Which persona fits best? Choose one: {', '.join(names)}\n"
                    f"User message: {message[:100]}\n"
                    f"Reply with the persona name only."
                ),
            }],
        )
        chosen = resp.content[0].text.strip().lower()
        if chosen not in PERSONA_CATALOG:
            chosen = "mentor"
        catalog_entry = PERSONA_CATALOG[chosen]
        return ActivePersona(
            name=catalog_entry["name"],
            system_prompt=catalog_entry["prompt"],
            confidence=0.8,
            turn_assigned=self._turn,
        )

    def chat(self, user_message: str) -> str:
        self._turn += 1

        # Assign/reassign persona
        should_reassign = (
            self._active is None
            or self._turn - self._active.turn_assigned >= self._REASSIGN_EVERY
        )
        if should_reassign:
            self._active = self._select_persona(user_message)
            print(f"[persona] Assigned: {self._active.name}")

        self._history.append({"role": "user", "content": user_message})
        resp = self._client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            system=self._active.system_prompt,
            messages=self._history,
        )
        answer = resp.content[0].text
        self._history.append({"role": "assistant", "content": answer})
        return answer


if __name__ == "__main__":
    manager = PersonaManager()
    conversation = [
        "I want to learn Python — where do I start?",
        "I keep making mistakes in my loops",
        "I'm thinking of writing a sci-fi novel about AI",
        "Can you help me analyze the pros and cons of microservices?",
        "I'm feeling really stuck and unmotivated today",
    ]
    for msg in conversation:
        print(f"\nU: {msg}")
        print(f"A: {manager.chat(msg)[:150]}")

# Expected Token Savings: Persona reuse across 5 turns; one classification per session window
# Environment: Multi-turn assistants where conversation context shifts significantly over time
```

---

### Option 4: Domain-Aware Persona with Tool-Based Context Detection

```python
import anthropic
import json


DOMAIN_PERSONAS = {
    "medical": (
        "You are a medical information assistant. Provide accurate, evidence-based information. "
        "Always recommend consulting a healthcare professional for personal medical decisions. "
        "Use precise medical terminology with plain-language explanations. "
        "Never diagnose or prescribe."
    ),
    "legal": (
        "You are a legal information assistant. Explain legal concepts clearly and objectively. "
        "Always state that this is general information, not legal advice. "
        "Recommend consulting a licensed attorney for specific situations. "
        "Cite relevant legal principles without claiming certainty."
    ),
    "financial": (
        "You are a financial education assistant. Explain financial concepts with clarity. "
        "Always distinguish between general education and personalized financial advice. "
        "Present balanced perspectives on investment and risk. "
        "Recommend a financial advisor for personal decisions."
    ),
    "technical": (
        "You are a technical expert. Provide precise, implementation-focused answers. "
        "Include code examples when helpful. Address edge cases and failure modes. "
        "Prefer concrete over abstract explanations."
    ),
    "general": (
        "You are a helpful, balanced assistant. Adapt your tone to the user's style. "
        "Be concise when the answer is simple, thorough when complexity warrants it. "
        "Always be honest about the limits of your knowledge."
    ),
}

DOMAIN_DETECTION_TOOL = {
    "name": "classify_domain",
    "description": "Classify the domain and user intent for persona selection",
    "input_schema": {
        "type": "object",
        "properties": {
            "domain": {
                "type": "string",
                "enum": ["medical", "legal", "financial", "technical", "general"],
                "description": "Primary domain of the user's question",
            },
            "formality": {
                "type": "string",
                "enum": ["formal", "neutral", "casual"],
                "description": "Appropriate formality level",
            },
            "urgency": {
                "type": "string",
                "enum": ["high", "medium", "low"],
                "description": "Urgency of the user's request",
            },
        },
        "required": ["domain", "formality", "urgency"],
    },
}


def detect_domain_with_tool(client: anthropic.Anthropic, message: str) -> dict:
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        tools=[DOMAIN_DETECTION_TOOL],
        tool_choice={"type": "auto"},
        messages=[{"role": "user", "content": f"Classify this message: {message}"}],
    )
    for block in resp.content:
        if block.type == "tool_use" and block.name == "classify_domain":
            return block.input
    return {"domain": "general", "formality": "neutral", "urgency": "medium"}


def tool_aware_agent(user_message: str) -> str:
    client = anthropic.Anthropic()
    classification = detect_domain_with_tool(client, user_message)
    domain = classification.get("domain", "general")
    formality = classification.get("formality", "neutral")
    urgency = classification.get("urgency", "medium")

    base_persona = DOMAIN_PERSONAS.get(domain, DOMAIN_PERSONAS["general"])
    formality_note = {
        "formal": " Maintain a professional, formal tone throughout.",
        "neutral": "",
        "casual": " Keep your tone friendly and conversational.",
    }.get(formality, "")
    urgency_note = " Respond efficiently — the user has an urgent need." if urgency == "high" else ""

    system = base_persona + formality_note + urgency_note
    print(f"[persona] domain={domain} formality={formality} urgency={urgency}")

    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )
    return resp.content[0].text


if __name__ == "__main__":
    queries = [
        "I have chest pain and shortness of breath",
        "My landlord won't return my security deposit",
        "Should I invest in index funds or ETFs?",
        "How do I implement a binary search tree in Python?",
        "What's the weather like in Tokyo?",
    ]
    for q in queries:
        print(f"\nQ: {q}")
        print(f"A: {tool_aware_agent(q)[:180]}")

# Expected Token Savings: Tool-use classification is structured and reliable; domain-specific personas reduce hedging tokens
# Environment: High-stakes domain agents (medical, legal, financial) needing persona-appropriate disclaimers
```

---

### Option 5: Real-Time Persona Adjustment Based on User Feedback Signals

```python
import anthropic
from dataclasses import dataclass, field


@dataclass
class PersonaState:
    detail_level: float = 0.5   # 0=terse, 1=verbose
    formality: float = 0.5      # 0=casual, 1=formal
    depth: float = 0.5          # 0=surface, 1=deep
    turn: int = 0

    def build_system(self) -> str:
        detail = "Be very concise." if self.detail_level < 0.3 else (
            "Provide thorough detail with examples." if self.detail_level > 0.7 else "Balance brevity with completeness."
        )
        formality = "Use a casual, friendly tone." if self.formality < 0.3 else (
            "Maintain a formal, professional tone." if self.formality > 0.7 else "Use a natural, approachable tone."
        )
        depth = "Stick to surface-level explanations." if self.depth < 0.3 else (
            "Go deep: cover edge cases, trade-offs, and nuance." if self.depth > 0.7 else "Cover the essentials with moderate depth."
        )
        return f"You are a helpful assistant. {detail} {formality} {depth}"


FEEDBACK_SIGNALS = {
    "too long": {"detail_level": -0.2},
    "too short": {"detail_level": +0.2},
    "simpler": {"depth": -0.2, "formality": -0.1},
    "more detail": {"depth": +0.2},
    "more formal": {"formality": +0.2},
    "casual": {"formality": -0.2},
    "just give me the answer": {"detail_level": -0.3, "depth": -0.2},
    "explain more": {"depth": +0.2, "detail_level": +0.1},
}


class FeedbackDrivenPersona:
    def __init__(self) -> None:
        self._client = anthropic.Anthropic()
        self._state = PersonaState()
        self._history: list[dict] = []

    def _detect_feedback(self, message: str) -> dict[str, float]:
        adjustments: dict[str, float] = {}
        lower = message.lower()
        for signal, deltas in FEEDBACK_SIGNALS.items():
            if signal in lower:
                adjustments.update(deltas)
        return adjustments

    def _apply_adjustments(self, adjustments: dict[str, float]) -> None:
        for attr, delta in adjustments.items():
            current = getattr(self._state, attr, 0.5)
            setattr(self._state, attr, max(0.0, min(1.0, current + delta)))
        if adjustments:
            print(f"[persona] Adjusted: detail={self._state.detail_level:.1f} "
                  f"formality={self._state.formality:.1f} depth={self._state.depth:.1f}")

    def chat(self, message: str) -> str:
        self._state.turn += 1
        feedback = self._detect_feedback(message)
        self._apply_adjustments(feedback)

        self._history.append({"role": "user", "content": message})
        system = self._state.build_system()
        resp = self._client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            system=system,
            messages=self._history,
        )
        answer = resp.content[0].text
        self._history.append({"role": "assistant", "content": answer})
        return answer


if __name__ == "__main__":
    agent = FeedbackDrivenPersona()
    turns = [
        "What is a REST API?",
        "That was too long — just give me the answer",
        "What is GraphQL?",
        "Explain more",
        "Can you be more formal about this?",
        "What are the trade-offs between REST and GraphQL?",
    ]
    for msg in turns:
        print(f"\nU: {msg}")
        print(f"A: {agent.chat(msg)[:180]}")

# Expected Token Savings: No classification tokens; feedback signals adjust persona at zero extra cost
# Environment: Interactive assistants where users naturally express style preferences during conversation
```

---

### Option 6: Persona A/B Testing with Quality Measurement

```python
import anthropic
import random
from dataclasses import dataclass, field


PERSONAS = {
    "concise": "Be extremely concise. Answer in 1–3 sentences maximum. No preamble.",
    "detailed": "Provide comprehensive answers with examples, context, and follow-up considerations.",
    "socratic": "Guide the user to the answer through questions rather than stating it directly.",
    "structured": "Always use bullet points or numbered lists. Lead with the key point. Use headers for multi-part answers.",
}


@dataclass
class PersonaResult:
    persona: str
    response: str
    quality_score: float = 0.0


@dataclass
class PersonaABTest:
    """
    Tests multiple personas on the same query and picks the best-rated one.
    Accumulates win counts to favor better-performing personas over time.
    """

    test_personas: list[str] = field(default_factory=lambda: list(PERSONAS.keys()))
    win_counts: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.win_counts = {p: 0 for p in self.test_personas}

    def _score(self, client: anthropic.Anthropic, query: str, response: str) -> float:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=10,
            messages=[{
                "role": "user",
                "content": (
                    f"Query: {query}\nResponse: {response[:200]}\n"
                    f"Rate quality 0.0-1.0 (clarity, helpfulness, appropriateness). "
                    f"Reply with one decimal number only."
                ),
            }],
        )
        try:
            return float(resp.content[0].text.strip())
        except ValueError:
            return 0.5

    def run(self, query: str, num_test: int = 2) -> str:
        client = anthropic.Anthropic()

        # Select personas weighted by past wins
        total_wins = sum(self.win_counts.values()) + len(self.test_personas)
        weights = [(self.win_counts[p] + 1) / total_wins for p in self.test_personas]
        test_set = random.choices(self.test_personas, weights=weights, k=min(num_test, len(self.test_personas)))

        results: list[PersonaResult] = []
        for persona_name in set(test_set):
            system = PERSONAS[persona_name]
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=200,
                system=system,
                messages=[{"role": "user", "content": query}],
            )
            result = PersonaResult(persona=persona_name, response=resp.content[0].text)
            result.quality_score = self._score(client, query, result.response)
            results.append(result)

        best = max(results, key=lambda r: r.quality_score)
        self.win_counts[best.persona] = self.win_counts.get(best.persona, 0) + 1
        print(f"[ab-test] Winner: {best.persona} (score={best.quality_score:.2f})")
        return best.response


if __name__ == "__main__":
    tester = PersonaABTest()
    queries = [
        "What is Docker?",
        "How do I center a div in CSS?",
        "Explain the CAP theorem",
        "What is technical debt?",
    ]
    for q in queries:
        print(f"\nQ: {q}")
        answer = tester.run(q)
        print(f"A: {answer[:150]}")

    print("\nPersona win counts:", tester.win_counts)

# Expected Token Savings: A/B test uses 2x tokens per query but identifies optimal persona; amortizes over many turns
# Environment: Product teams evaluating which communication style produces highest user satisfaction
```

---

## Comparison

| Option | Detection Method | Signals Used | Persistence | Cost Overhead |
|--------|-----------------|--------------|-------------|---------------|
| 1 | Single expertise classifier | Message vocabulary | Per-query | ~20 tokens |
| 2 | Multi-signal JSON classifier | Tone + intent + age | Per-session | ~40 tokens |
| 3 | Catalog selector + N-turn refresh | Message intent | N turns | ~15 tokens/N turns |
| 4 | Tool-use structured classification | Domain + formality + urgency | Per-query | ~50 tokens |
| 5 | Inline feedback signal parsing | User's explicit corrections | Continuous | 0 tokens |
| 6 | A/B test with quality scoring | Response quality + history | Accumulating | 2–3x per query |
