---
layout: solution
title: "Agent Ignores User Corrections — Repeats the Same Mistake"
category: general
description: "User tells the agent 'stop using bullet points'. Next response: bullet points. User says 'use Celsius not Fahrenheit'. Agent switches for one turn, then reverts to Fahrenheit. User corrects an incorrect assumption. Agent acknowledges it, then makes the same assumption again two messages later."
tags: [general, corrections, user-feedback, memory, system-prompt, conversation-history, instruction-following, persistent-behavior]
---

# Agent Ignores User Corrections — Repeats the Same Mistake

## Symptom
- User says "stop adding disclaimers" — agent stops, then adds a disclaimer again 3 messages later
- User corrects a factual error — agent acknowledges, then repeats the same error in the next response
- User specifies "always respond in Spanish" — agent responds in English again after 2 turns
- User says "don't use formal language" — agent switches to casual, then reverts to formal after context grows
- Corrected behavior persists for 1-2 turns, then gradually reverts to the default
- Agent says "you're right, I'll remember that" — and then doesn't

## Root Cause
The model has no persistent instruction layer within a session separate from the conversation history. Corrections made in the middle of a conversation are just another user message — they carry the same weight as everything else and are gradually diluted as the conversation grows. Early corrections get pushed far from the current position, reducing their influence on the model's next output. The fix is to extract corrections from conversation turns and promote them to the system prompt or a persistent instruction register.

## Fix

### Option 1: Extract corrections into a live system prompt

```python
import anthropic
import re

client = anthropic.Anthropic()

class CorrectionAwareSession:
    """
    Detects user corrections and promotes them to persistent instructions.
    Corrections are extracted from conversation turns and prepended to the system prompt,
    ensuring they always appear in the high-attention prefix position.
    """

    CORRECTION_PHRASES = [
        r"don't\s+", r"stop\s+", r"never\s+", r"always\s+",
        r"please\s+don't", r"avoid\s+", r"use\s+\w+\s+instead",
        r"i said\s+", r"i told you\s+", r"i asked you\s+",
        r"you keep\s+", r"again you\s+", r"still\s+doing",
        r"remember to\s+", r"make sure to\s+",
    ]

    def __init__(self, base_system: str):
        self.base_system = base_system
        self.corrections: list[str] = []
        self.history: list[dict] = []

    def _detect_correction(self, user_message: str) -> str | None:
        """
        Detect if a user message contains a correction/instruction.
        Returns the normalized correction or None.
        """
        lower = user_message.lower().strip()
        for pattern in self.CORRECTION_PHRASES:
            if re.search(pattern, lower):
                # Classify as a correction
                return user_message.strip()
        return None

    def _extract_correction_with_model(self, user_message: str) -> str | None:
        """
        Use a fast model to determine if this is a behavioral correction.
        More accurate than regex — handles nuanced corrections.
        """
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=100,
            messages=[{
                "role": "user",
                "content": (
                    f"Is this user message a behavioral correction or instruction for how the AI should behave?\n\n"
                    f"Message: \"{user_message}\"\n\n"
                    f"If yes, rephrase it as a clear instruction starting with 'Always' or 'Never' or 'Use' etc.\n"
                    f"If no, respond only: NO\n\n"
                    f"Respond with the rephrased instruction or NO."
                )
            }]
        )
        reply = response.content[0].text.strip()
        if reply.upper() == "NO" or len(reply) < 5:
            return None
        return reply

    def build_system_prompt(self) -> str:
        """Build system prompt with corrections prepended in high-attention position"""
        if not self.corrections:
            return self.base_system

        corrections_block = (
            "## Persistent User Instructions (ALWAYS FOLLOW)\n"
            "The user has given the following behavioral instructions. "
            "These override any default behavior. Never revert to old behavior:\n\n"
            + "\n".join(f"- {c}" for c in self.corrections)
        )

        return f"{corrections_block}\n\n{self.base_system}"

    def send(self, user_message: str, model: str = "claude-sonnet-4-6") -> str:
        """Send a message, extracting corrections before adding to history"""
        # Check if this is a correction
        correction = self._extract_correction_with_model(user_message)
        if correction:
            self.corrections.append(correction)
            print(f"Correction captured: '{correction}'")

        self.history.append({"role": "user", "content": user_message})

        response = client.messages.create(
            model=model,
            max_tokens=2048,
            system=self.build_system_prompt(),
            messages=self.history
        )

        text = response.content[0].text
        self.history.append({"role": "assistant", "content": text})
        return text

    def list_corrections(self) -> list[str]:
        """Show all captured corrections"""
        return self.corrections.copy()

session = CorrectionAwareSession(
    base_system="You are a helpful assistant."
)
```

### Option 2: Instruction register — accumulate and deduplicate corrections

```python
import anthropic
import json
from pathlib import Path
from typing import Optional

client = anthropic.Anthropic()

class InstructionRegister:
    """
    Maintains a deduplicated register of behavioral instructions.
    New instructions that conflict with existing ones replace them.
    """

    def __init__(self):
        self._instructions: dict[str, str] = {}  # topic → instruction

    def add(self, instruction: str, topic: str = None):
        """Add instruction, replacing any existing instruction on the same topic"""
        resolved_topic = topic or self._infer_topic(instruction)
        self._instructions[resolved_topic] = instruction
        print(f"Instruction registered [{resolved_topic}]: '{instruction}'")

    def _infer_topic(self, instruction: str) -> str:
        """Infer the topic category for deduplication"""
        lower = instruction.lower()
        topic_keywords = {
            "format": ["bullet", "list", "markdown", "format", "numbered"],
            "language": ["english", "spanish", "french", "language", "translate"],
            "tone": ["formal", "casual", "professional", "friendly", "tone"],
            "units": ["celsius", "fahrenheit", "metric", "imperial", "kg", "lb"],
            "length": ["brief", "concise", "short", "detailed", "long", "verbose"],
            "disclaimers": ["disclaimer", "warning", "caveat", "note that"],
            "citations": ["citation", "source", "reference", "cite"],
            "code": ["python", "javascript", "code block", "programming language"],
        }
        for topic, keywords in topic_keywords.items():
            if any(kw in lower for kw in keywords):
                return topic
        return f"instruction_{len(self._instructions)}"

    def build_block(self) -> str:
        """Format all instructions for system prompt injection"""
        if not self._instructions:
            return ""
        lines = ["## Your Instructions (Apply Always)\n"]
        for topic, inst in self._instructions.items():
            lines.append(f"- {inst}")
        return "\n".join(lines)

    def merge(self, other: "InstructionRegister"):
        """Merge another register into this one"""
        self._instructions.update(other._instructions)

    def to_dict(self) -> dict:
        return dict(self._instructions)

    def from_dict(self, data: dict):
        self._instructions = data

class PersistentCorrectionSession:
    """Session that persists corrections across restarts"""

    def __init__(
        self,
        base_system: str,
        persistence_path: str = "/data/user_instructions.json"
    ):
        self.base_system = base_system
        self.persistence_path = Path(persistence_path)
        self.register = InstructionRegister()
        self.history: list[dict] = []
        self._load()

    def _load(self):
        if self.persistence_path.exists():
            data = json.loads(self.persistence_path.read_text())
            self.register.from_dict(data)
            print(f"Loaded {len(data)} persistent instructions")

    def _save(self):
        tmp = self.persistence_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.register.to_dict(), indent=2))
        tmp.replace(self.persistence_path)

    def apply_correction(self, instruction: str, topic: Optional[str] = None):
        """Manually apply a correction"""
        self.register.add(instruction, topic)
        self._save()

    def build_system(self) -> str:
        instruction_block = self.register.build_block()
        if instruction_block:
            return f"{instruction_block}\n\n{self.base_system}"
        return self.base_system

    def send(self, user_message: str, model: str = "claude-sonnet-4-6") -> str:
        self.history.append({"role": "user", "content": user_message})
        response = client.messages.create(
            model=model,
            max_tokens=2048,
            system=self.build_system(),
            messages=self.history
        )
        text = response.content[0].text
        self.history.append({"role": "assistant", "content": text})
        return text
```

### Option 3: Post-response correction check — verify compliance

```python
import anthropic

client = anthropic.Anthropic()

COMPLIANCE_CHECK_PROMPT = """Check if this response violates any of the user's instructions.

User instructions:
{instructions}

Response to check:
{response}

For each instruction, state: COMPLIANT or VIOLATED: [reason]
At the end, state overall: PASS or FAIL"""

def check_response_compliance(
    response: str,
    instructions: list[str],
    model: str = "claude-haiku-4-5-20251001"
) -> tuple[bool, str]:
    """
    Check if a response violates any captured instructions.
    Returns (compliant, report).
    """
    if not instructions:
        return True, "No instructions to check"

    check_response = client.messages.create(
        model=model,
        max_tokens=300,
        messages=[{
            "role": "user",
            "content": COMPLIANCE_CHECK_PROMPT.format(
                instructions="\n".join(f"- {i}" for i in instructions),
                response=response[:2000]
            )
        }]
    )

    report = check_response.content[0].text
    compliant = "FAIL" not in report.split("\n")[-2:]
    return compliant, report

async def send_with_compliance_check(
    session: CorrectionAwareSession,
    user_message: str
) -> str:
    """Send message and verify the response follows all corrections"""
    response = session.send(user_message)

    if session.corrections:
        compliant, report = check_response_compliance(response, session.corrections)
        if not compliant:
            print(f"COMPLIANCE FAILURE:\n{report}")
            # Re-try with explicit instruction reinforcement
            reinforcement = (
                f"[System: Your previous response violated user instructions. "
                f"Please rewrite it while strictly following ALL these rules:\n"
                f"{chr(10).join(f'- {i}' for i in session.corrections)}]"
            )
            session.history.append({"role": "user", "content": reinforcement})
            response = session.send("[Please provide a corrected response following all instructions]")

    return response
```

### Option 4: Correction summary injection — summarize and restate at context growth

```python
import anthropic

client = anthropic.Anthropic()

class ContextAwareCorrectionSession:
    """
    Re-injects correction summary as context grows.
    Prevents corrections from being "forgotten" as they scroll far from current position.
    """

    def __init__(self, base_system: str, reinject_every_n_turns: int = 5):
        self.base_system = base_system
        self.reinject_interval = reinject_every_n_turns
        self.corrections: list[str] = []
        self.history: list[dict] = []
        self._turn_count = 0

    def _correction_reminder(self) -> str:
        if not self.corrections:
            return ""
        return (
            f"[Reminder: You must follow these instructions in your next response:\n"
            f"{chr(10).join(f'- {c}' for c in self.corrections)}]"
        )

    def send(self, user_message: str, model: str = "claude-sonnet-4-6") -> str:
        self._turn_count += 1

        # Inject correction reminder every N turns to counteract dilution
        effective_message = user_message
        if self.corrections and self._turn_count % self.reinject_interval == 0:
            reminder = self._correction_reminder()
            effective_message = f"{reminder}\n\n{user_message}"
            print(f"Reinjecting {len(self.corrections)} corrections at turn {self._turn_count}")

        self.history.append({"role": "user", "content": effective_message})

        system = self.base_system
        if self.corrections:
            system = (
                f"PERSISTENT INSTRUCTIONS (apply to every response, never revert):\n"
                f"{chr(10).join(f'- {c}' for c in self.corrections)}\n\n"
                f"{self.base_system}"
            )

        response = client.messages.create(
            model=model,
            max_tokens=2048,
            system=system,
            messages=self.history
        )
        text = response.content[0].text
        self.history.append({"role": "assistant", "content": text})
        return text

    def add_correction(self, correction: str):
        """Explicitly add a correction (can be called by the application layer)"""
        if correction not in self.corrections:
            self.corrections.append(correction)
```

### Option 5: Two-model pattern — small model extracts instructions, large model executes

```python
import anthropic
from dataclasses import dataclass, field

client = anthropic.Anthropic()

@dataclass
class TurnAnalysis:
    is_correction: bool
    correction_text: str | None
    topic: str | None
    user_intent: str

ANALYZER_PROMPT = """Analyze this user message and return JSON:

{
  "is_correction": true/false,  // is this a behavioral correction or preference?
  "correction_text": "...",     // if correction, the normalized instruction (null if not)
  "topic": "...",               // topic category (format/tone/language/units/etc) or null
  "user_intent": "..."          // brief description of what user wants
}

Message: "{message}" """

def analyze_user_turn(user_message: str) -> TurnAnalysis:
    """Extract intent and corrections from user message"""
    import json as json_module
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        messages=[{
            "role": "user",
            "content": ANALYZER_PROMPT.format(message=user_message[:500])
        }]
    )
    try:
        # Extract JSON from response
        import re
        text = response.content[0].text
        match = re.search(r'\{.*\}', text, re.DOTALL)
        if match:
            data = json_module.loads(match.group())
            return TurnAnalysis(
                is_correction=data.get("is_correction", False),
                correction_text=data.get("correction_text"),
                topic=data.get("topic"),
                user_intent=data.get("user_intent", "")
            )
    except Exception:
        pass
    return TurnAnalysis(is_correction=False, correction_text=None, topic=None, user_intent=user_message)

@dataclass
class AnalyzingSession:
    base_system: str
    _corrections: dict = field(default_factory=dict)  # topic → correction
    _history: list = field(default_factory=list)

    def send(self, user_message: str) -> str:
        analysis = analyze_user_turn(user_message)
        if analysis.is_correction and analysis.correction_text:
            topic = analysis.topic or f"correction_{len(self._corrections)}"
            self._corrections[topic] = analysis.correction_text
            print(f"New instruction: [{topic}] {analysis.correction_text}")

        self._history.append({"role": "user", "content": user_message})
        system = self._build_system()
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2048,
            system=system,
            messages=self._history
        )
        text = response.content[0].text
        self._history.append({"role": "assistant", "content": text})
        return text

    def _build_system(self) -> str:
        if not self._corrections:
            return self.base_system
        block = "REQUIRED BEHAVIORS (never revert, apply to every response):\n" + \
                "\n".join(f"- {v}" for v in self._corrections.values())
        return f"{block}\n\n{self.base_system}"
```

### Option 6: System prompt correction template — explicit user preference slots

```python
PREFERENCE_AWARE_SYSTEM = """You are a helpful assistant.

## User Preferences (Apply to Every Response)
{preferences}

## Rules for Applying Preferences
1. These preferences apply permanently — not just for the next response
2. If a preference conflicts with a default behavior, the preference wins
3. Never revert to previous behavior after being corrected
4. If unsure whether a preference applies to a given response, apply it

## Default Behavior (Overridden by Preferences Above)
- Use markdown formatting
- Respond in English
- Use standard US units
- Include relevant caveats
"""

DEFAULT_PREFERENCES = {
    "format": "Use markdown formatting with headers and bullet points",
    "language": "Respond in English",
    "units": "Use metric units (Celsius, kg, km)",
    "length": "Be concise — aim for the shortest response that fully answers the question",
    "tone": "Be conversational and direct"
}

def build_preference_system(
    user_preferences: dict[str, str] | None = None
) -> str:
    """Build system prompt with user-specific preference slots"""
    prefs = {**DEFAULT_PREFERENCES, **(user_preferences or {})}
    pref_text = "\n".join(f"- **{topic}**: {value}" for topic, value in prefs.items())
    return PREFERENCE_AWARE_SYSTEM.format(preferences=pref_text)

# Preferences can be updated when user makes corrections:
user_prefs = {}

def apply_user_correction(correction: str, topic: str):
    """Update user preferences when they make a correction"""
    user_prefs[topic] = correction
    print(f"Preference updated [{topic}]: {correction}")

# Example: user says "stop using bullet points"
apply_user_correction("Use flowing prose instead of bullet points", "format")
system = build_preference_system(user_prefs)
# Now the system prompt explicitly says prose — model will consistently use it
```

## Why Corrections Are Forgotten

| Mechanism | Why It Fails | Fix |
|---|---|---|
| Correction in conversation turn | Diluted as history grows | Promote to system prompt |
| Single acknowledgment message | Model treats it like any message | Reinject every N turns |
| No deduplication of conflicting instructions | Old and new both present | Topic-keyed register |
| Long conversation pushes correction far back | Model attends to recent context | Position in system prompt |
| Model defaults override corrections at restart | Session state lost | Persist instructions to disk |

## Expected Token Savings
User repeats same correction 5 times per session × 3 sessions: ~9,000 tokens of corrective messages
Persistent instruction register → correction stated once, applied permanently: 0 repeated corrections

## Environment
- Any conversational agent with multi-turn sessions; critical for personal assistants, coding assistants, and any agent that should adapt its behavior based on user preferences — the failure to retain corrections is the top user experience complaint in agents used for more than 10 minutes
- Source: direct experience; correction persistence is the single highest-priority UX improvement in the first month of deploying any customer-facing conversational agent
