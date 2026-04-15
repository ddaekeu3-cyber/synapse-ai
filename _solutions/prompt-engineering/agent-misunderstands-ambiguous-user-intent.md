---
layout: solution
title: "Agent Misunderstands Ambiguous User Intent — Answers the Wrong Question"
category: prompt-engineering
description: "The user asks 'Can you change the background?' and the agent changes the CSS background color instead of the background image. 'Make it faster' means optimize runtime for the developer but reduce animation speed for the designer. The agent picks the most literal interpretation without disambiguating. It confidently completes the wrong task and the user has to repeat the request with more detail."
tags: [prompt-engineering, intent, disambiguation, clarification, ambiguity, user-intent, multi-turn, system-prompt]
---

# Agent Misunderstands Ambiguous User Intent — Answers the Wrong Question

## Symptom
- Agent completes a task confidently but solves the wrong problem
- User has to re-explain the same request 2–3 times with more specificity
- Literal interpretation taken when figurative was intended (and vice versa)
- Agent never asks a clarifying question — just guesses and executes
- Different users with the same request get wildly different results
- Ambiguous pronouns ("change it", "fix that") resolve to the wrong referent
- Agent resolves ambiguity by picking the most common interpretation, not the contextually correct one

## Root Cause
LLMs default to the statistically most common interpretation of ambiguous requests. Without context about who the user is, what they're working on, and what "it" refers to, the model guesses. The fix is to: (1) detect high-ambiguity requests and ask a targeted clarifying question, (2) use conversational context to narrow interpretation, (3) explicitly resolve ambiguity in the system prompt by defining the domain and user persona, and (4) present the interpreted intent before acting on it.

## Fix

### Option 1: Intent classification before execution — detect ambiguity, ask once

```python
import anthropic
import json

client = anthropic.Anthropic()

def classify_intent_ambiguity(
    user_message: str,
    conversation_context: str = "",
    domain: str = "software development"
) -> dict:
    """
    Classify whether a request is ambiguous enough to need clarification.
    Returns: {ambiguous: bool, interpretations: list, clarifying_question: str | None}
    """
    context_section = f"\nConversation context:\n{conversation_context}" if conversation_context else ""

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",  # Fast, cheap model for this triage step
        max_tokens=512,
        messages=[{
            "role": "user",
            "content": (
                f"Domain: {domain}{context_section}\n\n"
                f"User request: {user_message!r}\n\n"
                "Analyze this request. Is it ambiguous? If so, what are the top 2-3 likely interpretations?\n"
                "Respond with JSON:\n"
                '{"ambiguous": true/false, '
                '"interpretations": ["interpretation 1", "interpretation 2"], '
                '"most_likely": "the most likely single interpretation", '
                '"clarifying_question": "a short question to ask the user, or null if not needed"}'
            )
        }]
    )

    try:
        return json.loads(response.content[0].text.strip().strip("```json").strip("```"))
    except json.JSONDecodeError:
        return {"ambiguous": False, "most_likely": user_message, "clarifying_question": None}

def execute_with_disambiguation(
    user_message: str,
    conversation_history: list[dict],
    domain: str = "software development"
) -> dict:
    """
    Check for ambiguity before executing. Ask a clarifying question if needed.
    Returns: {action: "execute" | "clarify", result: str, interpretation: str}
    """
    context = "\n".join(
        f"{m['role']}: {m['content']}" for m in conversation_history[-4:]
    ) if conversation_history else ""

    classification = classify_intent_ambiguity(user_message, context, domain)

    if classification.get("ambiguous") and classification.get("clarifying_question"):
        # Return the clarifying question — don't execute yet
        return {
            "action": "clarify",
            "result": classification["clarifying_question"],
            "interpretation": None,
            "interpretations": classification.get("interpretations", [])
        }

    # Not ambiguous (or can't detect) — execute with explicit interpretation stated
    interpretation = classification.get("most_likely", user_message)

    messages = conversation_history + [{
        "role": "user",
        "content": user_message
    }]

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=(
            f"You are a {domain} assistant. "
            f"The user's request means: {interpretation}. "
            "Complete the task based on this interpretation."
        ),
        messages=messages
    )

    return {
        "action": "execute",
        "result": response.content[0].text,
        "interpretation": interpretation,
        "interpretations": classification.get("interpretations", [interpretation])
    }

# Usage:
result = execute_with_disambiguation(
    user_message="Make it faster",
    conversation_history=[
        {"role": "user", "content": "I'm working on a React animation component."},
        {"role": "assistant", "content": "I can help with that."}
    ],
    domain="frontend development"
)
if result["action"] == "clarify":
    print(f"Question: {result['result']}")
else:
    print(f"Interpreted as: {result['interpretation']}")
    print(f"Result: {result['result']}")
```

### Option 2: State the interpretation before acting — ask for implicit confirmation

```python
import anthropic

client = anthropic.Anthropic()

INTERPRETATION_FIRST_SYSTEM = """## Response Protocol for Ambiguous Requests

When a user request could be interpreted multiple ways:
1. State your interpretation at the start: "I'll interpret this as: [your interpretation]."
2. Complete the task based on that interpretation.
3. End with: "If you meant something different, let me know."

When a request is highly ambiguous (two equally likely interpretations):
1. Ask ONE short clarifying question: "Do you mean X or Y?"
2. Wait for the user's answer before proceeding.

DO NOT silently pick an interpretation and execute without stating it.
DO NOT ask more than one clarifying question at a time.
DO NOT ask for clarification on clearly unambiguous requests.

Example of WRONG behavior (user says "fix the error"):
"I'll fix the error." [then makes changes]

Example of RIGHT behavior (user says "fix the error"):
"I'll interpret this as fixing the TypeError on line 42 in auth.py.
[then makes changes]
If you meant a different error, let me know."
"""

def chat_with_interpretation_first(
    user_message: str,
    history: list[dict],
    domain_context: str = ""
) -> str:
    system = INTERPRETATION_FIRST_SYSTEM
    if domain_context:
        system = f"Domain context: {domain_context}\n\n" + system

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=system,
        messages=history + [{"role": "user", "content": user_message}]
    )
    return response.content[0].text
```

### Option 3: User persona in system prompt — narrow the interpretation space

```python
import anthropic
from dataclasses import dataclass
from typing import Optional

client = anthropic.Anthropic()

@dataclass
class UserPersona:
    role: str                       # "backend developer", "product manager", etc.
    skill_level: str                # "beginner", "intermediate", "expert"
    primary_language: str           # "Python", "JavaScript", etc.
    current_task: Optional[str]     # What they're working on right now
    preferences: list[str]          # Known preferences ["prefers concise answers", etc.]

def build_persona_system_prompt(persona: UserPersona, base_system: str = "") -> str:
    """
    Embed user persona in system prompt to narrow interpretation of ambiguous requests.
    """
    persona_section = f"""## User Context
- Role: {persona.role}
- Skill level: {persona.skill_level}
- Primary language: {persona.primary_language}
- Current task: {persona.current_task or 'unknown'}
- Preferences: {', '.join(persona.preferences) if persona.preferences else 'none specified'}

## Interpretation Rules
Given this user context, when interpreting ambiguous requests:
- "faster" → optimize {persona.primary_language} runtime performance (not UI animation speed)
- "clean it up" → improve code readability for a {persona.skill_level} {persona.role}
- "fix it" → fix the most recent issue discussed in the conversation
- "make it work" → debug and fix the current failing code
- Adjust explanation depth for a {persona.skill_level} developer
"""
    return f"{persona_section}\n\n{base_system}" if base_system else persona_section

def ask_with_persona(
    question: str,
    persona: UserPersona,
    history: list[dict] | None = None
) -> str:
    system = build_persona_system_prompt(persona)
    messages = (history or []) + [{"role": "user", "content": question}]

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=system,
        messages=messages
    )
    return response.content[0].text

# Usage — same ambiguous request, different personas → different correct interpretations:
backend_dev = UserPersona(
    role="backend engineer",
    skill_level="expert",
    primary_language="Python",
    current_task="optimizing database query performance",
    preferences=["concise answers", "show code, not explanations"]
)

frontend_dev = UserPersona(
    role="frontend designer",
    skill_level="intermediate",
    primary_language="JavaScript",
    current_task="building an animated dashboard",
    preferences=["visual examples", "step-by-step"]
)

# "Make it faster" → backend dev: runtime optimization
# "Make it faster" → frontend dev: reduce animation duration
answer_backend = ask_with_persona("Make it faster", backend_dev)
answer_frontend = ask_with_persona("Make it faster", frontend_dev)
```

### Option 4: Anaphora resolution — explicitly resolve pronouns before acting

```python
import anthropic
import json

client = anthropic.Anthropic()

def resolve_references(
    user_message: str,
    conversation_history: list[dict]
) -> str:
    """
    Resolve ambiguous pronouns and references ("it", "that", "this", "the issue")
    by analyzing conversation context.
    Returns the user message with ambiguous references resolved.
    """
    if not any(pronoun in user_message.lower() for pronoun in
               ["it", "that", "this", "the issue", "the error", "the file", "the function", "there"]):
        return user_message  # No pronouns to resolve

    recent_context = "\n".join(
        f"{m['role']}: {m['content']}"
        for m in conversation_history[-6:]
    )

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{
            "role": "user",
            "content": (
                f"Conversation so far:\n{recent_context}\n\n"
                f"Latest message: {user_message!r}\n\n"
                "Rewrite the latest message with all pronouns and vague references "
                "replaced by their specific referents from the conversation context. "
                "If a reference is truly unclear, keep it as-is. "
                "Return only the rewritten message, nothing else."
            )
        }]
    )

    resolved = response.content[0].text.strip()
    return resolved if resolved else user_message

def chat_with_reference_resolution(
    user_message: str,
    history: list[dict]
) -> str:
    """Chat with automatic pronoun/reference resolution."""
    # Resolve references before passing to the main model
    resolved_message = resolve_references(user_message, history)

    if resolved_message != user_message:
        print(f"Resolved: {user_message!r} → {resolved_message!r}")

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=history + [{"role": "user", "content": resolved_message}]
    )
    return response.content[0].text

# Example:
history = [
    {"role": "user", "content": "I have a bug in the authentication module"},
    {"role": "assistant", "content": "I see the issue in auth.py — the JWT validation is missing the expiry check."},
    {"role": "user", "content": "Can you fix it and also check that the tests cover it?"}
]
# "fix it" → "fix the JWT validation expiry check in auth.py"
# "cover it" → "cover the JWT validation expiry check"
reply = chat_with_reference_resolution("Can you fix it and also check that the tests cover it?", history)
```

### Option 5: Multi-interpretation preview — show options, let user choose

```python
import anthropic
import json

client = anthropic.Anthropic()

def preview_interpretations(
    user_message: str,
    history: list[dict],
    n_options: int = 2
) -> dict:
    """
    Generate N interpretations of the user's request and ask which they meant.
    Use for highly ambiguous requests where the cost of misinterpretation is high.
    """
    context = "\n".join(f"{m['role']}: {m['content']}" for m in history[-4:])

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{
            "role": "user",
            "content": (
                f"Context:\n{context}\n\n"
                f"User request: {user_message!r}\n\n"
                f"List {n_options} distinct interpretations of this request. "
                "Each interpretation should lead to a meaningfully different action. "
                f"Return JSON: {{\"interpretations\": [{{\"label\": \"A\", \"description\": \"...\", \"action\": \"what would be done\"}}, ...]}}"
            )
        }]
    )

    try:
        data = json.loads(response.content[0].text.strip().strip("```json").strip("```"))
        interpretations = data.get("interpretations", [])
    except json.JSONDecodeError:
        return {"clarification_needed": False, "interpretations": []}

    if len(interpretations) < 2:
        return {"clarification_needed": False, "interpretations": interpretations}

    # Format the clarification question:
    options_text = "\n".join(
        f"{opt['label']}) {opt['description']} — would {opt['action']}"
        for opt in interpretations
    )

    return {
        "clarification_needed": True,
        "question": f"I want to make sure I understand correctly. Did you mean:\n\n{options_text}\n\nWhich did you have in mind?",
        "interpretations": interpretations
    }

def smart_chat(user_message: str, history: list[dict]) -> str:
    """
    Chat with multi-interpretation preview for ambiguous requests.
    Falls back to direct execution for clear requests.
    """
    preview = preview_interpretations(user_message, history, n_options=2)

    if preview.get("clarification_needed"):
        return preview["question"]  # Return the clarification question to the user

    # Not ambiguous — execute directly
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=history + [{"role": "user", "content": user_message}]
    )
    return response.content[0].text
```

### Option 6: Semantic similarity matching — match request to known intents

```python
import anthropic
import json
from typing import Any

client = anthropic.Anthropic()

# Define canonical intents for your domain:
KNOWN_INTENTS = {
    "refactor_code": {
        "description": "Restructure code for better readability without changing behavior",
        "keywords": ["refactor", "clean up", "reorganize", "restructure", "tidy"],
        "triggers": ["clean", "organize", "restructure"]
    },
    "optimize_performance": {
        "description": "Make code run faster or use less memory",
        "keywords": ["faster", "optimize", "performance", "speed up", "reduce latency"],
        "triggers": ["fast", "slow", "performance", "optimize"]
    },
    "fix_bug": {
        "description": "Find and fix a specific error or unexpected behavior",
        "keywords": ["fix", "bug", "error", "broken", "doesn't work", "failing"],
        "triggers": ["fix", "bug", "error", "broken"]
    },
    "add_feature": {
        "description": "Add new functionality to existing code",
        "keywords": ["add", "implement", "create", "build", "new feature"],
        "triggers": ["add", "implement", "build"]
    },
    "explain_code": {
        "description": "Explain how existing code works",
        "keywords": ["explain", "how does", "what does", "understand", "describe"],
        "triggers": ["explain", "how", "what", "understand"]
    }
}

def match_intent(user_message: str) -> dict:
    """
    Match user message to the closest known intent.
    Returns the matched intent and confidence.
    """
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{
            "role": "user",
            "content": (
                f"Classify this request into exactly one intent:\n\n"
                f"Request: {user_message!r}\n\n"
                f"Available intents:\n"
                + "\n".join(f"- {k}: {v['description']}" for k, v in KNOWN_INTENTS.items())
                + "\n\nReturn JSON: {\"intent\": \"intent_name\", \"confidence\": 0.0-1.0, "
                "\"interpretation\": \"what the user specifically wants\"}"
            )
        }]
    )

    try:
        result = json.loads(response.content[0].text.strip().strip("```json").strip("```"))
        result["intent_config"] = KNOWN_INTENTS.get(result.get("intent", ""), {})
        return result
    except (json.JSONDecodeError, KeyError):
        return {"intent": "unknown", "confidence": 0.0, "interpretation": user_message}

def intent_aware_chat(user_message: str, history: list[dict], code_context: str = "") -> str:
    """
    Classify intent before responding. Use intent to anchor interpretation.
    """
    matched = match_intent(user_message)
    intent = matched.get("intent", "unknown")
    interpretation = matched.get("interpretation", user_message)
    confidence = matched.get("confidence", 0.0)

    system = f"You are a code assistant."
    if code_context:
        system += f"\n\nCurrent code:\n```\n{code_context}\n```"

    if intent != "unknown" and confidence >= 0.7:
        system += (
            f"\n\nThe user's intent has been classified as: {intent}\n"
            f"Specific interpretation: {interpretation}\n"
            f"Complete the task according to this interpretation."
        )
    elif confidence < 0.5:
        # Low confidence — ask for clarification
        return f"I want to make sure I understand — are you asking me to {interpretation}? Let me know if that's right."

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=system,
        messages=history + [{"role": "user", "content": user_message}]
    )
    return response.content[0].text
```

## Disambiguation Strategy by Request Type

| Request Pattern | Disambiguation Approach | Example |
|---|---|---|
| Pronoun ambiguity ("fix it") | Anaphora resolution (Option 4) | Resolve "it" from conversation context |
| Domain keyword overlap ("faster") | User persona in system (Option 3) | Dev → runtime; designer → animation |
| High-stakes action ("delete everything") | Multi-interpretation preview (Option 5) | Show 2 options, ask which |
| General vague request ("clean up") | Intent classification (Option 6) | Match to known refactor intent |
| Casually ambiguous ("make it nice") | Interpret-then-act (Option 2) | State interpretation before executing |
| Any ambiguous input in production | Ambiguity detection + ask once (Option 1) | Detect and ask targeted question |

## Clarifying Question Design Rules
- Ask at most **one** clarifying question per ambiguous request
- Make it a **binary or small-choice** question ("do you mean X or Y?"), not open-ended ("what do you want?")
- **Don't ask** if context makes one interpretation 80%+ likely — just state it and proceed
- **Do ask** if the two most likely interpretations lead to meaningfully different (possibly destructive) actions

## Expected Token Savings
Wrong interpretation → user corrects → agent re-does task: ~3,000–8,000 tokens wasted per correction
Correct interpretation on first try: 0 wasted tokens
In a coding agent with 20% ambiguity rate at 100 daily sessions: save 20 × average correction overhead

## Environment
- Any agent handling natural language requests across diverse user types; ambiguity is highest in: multi-role teams (same word means different things to devs vs. PMs), domain-crossing agents (code + design + content), and agents used by both experts and beginners — build disambiguation into the agent's first-response behavior, not as an afterthought
- Source: direct experience; "the agent did the right thing technically but for the wrong interpretation" is the top complaint in the first two weeks of deployment for any multi-purpose agent
