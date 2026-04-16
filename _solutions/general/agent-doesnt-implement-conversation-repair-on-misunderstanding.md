---
layout: solution
title: "Agent Doesn't Implement Conversation Repair on Misunderstanding"
category: general
description: "Detect when the agent has misunderstood a user's request and proactively repair the conversation — acknowledging the error, re-interpreting the intent, and offering a corrected response without requiring the user to re-explain."
tags: [general, conversation, ux, misunderstanding, repair, intent, dialogue]
---

When an agent misunderstands a request and gives a wrong answer, users typically have to re-explain their entire intent. A well-designed agent detects signals of misunderstanding — negative feedback, correction phrases, abrupt topic shifts — and initiates repair: acknowledging what went wrong, proposing a re-interpretation, and generating a corrected response. This recovers the conversation gracefully instead of forcing the user to start over.

## Option 1: Signal Detection with Repair Injection

Detect explicit correction signals ("no, I meant", "that's not what I asked", "wrong") in user messages. When detected, extract the correction, update the agent's understanding, and re-answer the original question with the corrected interpretation.

```python
import anthropic
import re
from dataclasses import dataclass

CORRECTION_PATTERNS = [
    r"(?i)\bno[,.]?\s+(?:i\s+meant|that'?s?\s+not)",
    r"(?i)\bthat'?s?\s+(?:not\s+what\s+i\s+(?:asked|meant|wanted)|wrong)",
    r"(?i)\bactually[,.]?\s+i\s+(?:meant|wanted|asked)",
    r"(?i)\bsorry[,.]?\s+i\s+meant",
    r"(?i)\bno[,.]?\s+(?:not\s+that|rather)",
    r"(?i)\bwrong[,.]?\s+i\s+(?:meant|wanted)",
    r"(?i)\bthat'?s?\s+not\s+(?:right|correct|what)",
    r"(?i)\bstop[,.]?\s+(?:i\s+(?:didn't|don't)|that'?s?\s+not)",
]

def detect_correction(message: str) -> bool:
    return any(re.search(p, message) for p in CORRECTION_PATTERNS)

def extract_corrected_intent(client: anthropic.Anthropic, history: list[dict], correction: str) -> str:
    """Use the model to infer the user's actual intent from the correction."""
    history_text = "\n".join(f"{m['role'].upper()}: {m['content']}" for m in history[-6:])
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=150,
        messages=[{
            "role": "user",
            "content": (
                f"Conversation so far:\n{history_text}\n\n"
                f"User correction: {correction}\n\n"
                f"In one sentence, what is the user's actual intent? "
                f"Reply with ONLY the corrected intent, no explanation."
            ),
        }],
    )
    return response.content[0].text.strip()

def chat_with_repair(conversation_history: list[dict], new_message: str) -> str:
    client = anthropic.Anthropic()

    if detect_correction(new_message) and len(conversation_history) >= 2:
        corrected_intent = extract_corrected_intent(client, conversation_history, new_message)
        print(f"[Repair] Correction detected. Re-interpreted intent: {corrected_intent}")

        # Acknowledge and re-answer with corrected intent
        repair_system = (
            "The user has corrected a misunderstanding. "
            "First briefly acknowledge the misunderstanding (one sentence), "
            "then answer the corrected intent directly."
        )
        messages = conversation_history[:-1] + [{
            "role": "user",
            "content": f"[Correction] My actual question was: {corrected_intent}",
        }]
    else:
        repair_system = "You are a helpful assistant."
        messages = conversation_history + [{"role": "user", "content": new_message}]

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=repair_system,
        messages=messages,
    )
    return response.content[0].text

if __name__ == "__main__":
    history = [
        {"role": "user", "content": "Tell me about Python"},
        {"role": "assistant", "content": "Python is a programming language created by Guido van Rossum in 1991..."},
    ]
    correction = "No, I meant the Monty Python comedy group, not the programming language"
    result = chat_with_repair(history, correction)
    print(result[:300])

# Expected Token Savings: Avoids full re-explanation turns; repair is 1 additional call vs 2-3 correction turns
# Environment: pip install anthropic
```

## Option 2: Implicit Misunderstanding Detection via Sentiment and Engagement

Detect implicit signals of dissatisfaction — very short follow-ups, repeated similar questions, or low-engagement phrases ("ok", "fine", "whatever") — that may indicate the user got a wrong answer but didn't explicitly say so. Proactively check in and offer repair.

```python
import anthropic
import re
from dataclasses import dataclass

@dataclass
class EngagementSignal:
    likely_misunderstood: bool
    signal_type: str
    confidence: float

LOW_ENGAGEMENT_PHRASES = {
    r"(?i)^(ok|okay|fine|sure|whatever|alright|i\s+see|got\s+it|thanks\.?|k\.?)$",
    r"(?i)^(uh\s+huh|mhm|hmm|i\s+guess)\.?$",
    r"(?i)^(never\s+mind|forget\s+it|doesn'?t\s+matter)\.?$",
}

REPEAT_SIMILARITY_THRESHOLD = 0.6

def is_low_engagement(message: str) -> bool:
    return any(re.match(p, message.strip()) for p in LOW_ENGAGEMENT_PHRASES)

def word_similarity(a: str, b: str) -> float:
    wa, wb = set(a.lower().split()), set(b.lower().split())
    return len(wa & wb) / len(wa | wb) if (wa | wb) else 0.0

def assess_engagement(history: list[dict], new_message: str) -> EngagementSignal:
    # Very short follow-up
    if len(new_message.strip()) < 10 and len(history) > 2:
        if is_low_engagement(new_message):
            return EngagementSignal(True, "low_engagement_phrase", 0.7)

    # Repeated similar question
    user_messages = [m["content"] for m in history if m["role"] == "user"]
    for prev in user_messages[-3:]:
        if word_similarity(prev, new_message) > REPEAT_SIMILARITY_THRESHOLD:
            return EngagementSignal(True, "repeated_question", 0.85)

    # Very short response after a long assistant turn
    if history and history[-1]["role"] == "assistant":
        last_assistant_len = len(history[-1]["content"])
        if last_assistant_len > 500 and len(new_message.strip()) < 15:
            return EngagementSignal(True, "short_after_long_response", 0.6)

    return EngagementSignal(False, "normal", 0.9)

def chat_with_implicit_repair(history: list[dict], new_message: str) -> str:
    client = anthropic.Anthropic()
    signal = assess_engagement(history, new_message)

    if signal.likely_misunderstood and signal.confidence >= 0.65:
        print(f"[ImplicitRepair] Signal: {signal.signal_type} (conf={signal.confidence:.2f})")
        system = (
            "The user's response suggests they may not have gotten what they needed. "
            "Briefly acknowledge your previous response may have missed the mark, "
            "ask a single focused clarifying question to understand what they actually need, "
            "then offer to help with that."
        )
    else:
        system = "You are a helpful assistant."

    messages = history + [{"role": "user", "content": new_message}]
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=system,
        messages=messages,
    )
    return response.content[0].text

if __name__ == "__main__":
    # Scenario 1: Low engagement signal
    history_1 = [
        {"role": "user", "content": "How do I fix my code?"},
        {"role": "assistant", "content": "There are many ways to debug code. First, check the error messages. Then use print statements or a debugger. Review the stack trace carefully..."},
    ]
    print("=== Low Engagement ===")
    print(chat_with_implicit_repair(history_1, "ok"))

    # Scenario 2: Repeated question
    history_2 = [
        {"role": "user", "content": "How do I reverse a list in Python?"},
        {"role": "assistant", "content": "You can use the reversed() function: list(reversed(my_list))"},
        {"role": "user", "content": "OK"},
        {"role": "assistant", "content": "Let me know if you need anything else!"},
    ]
    print("\n=== Repeated Question ===")
    print(chat_with_implicit_repair(history_2, "How to reverse a list in Python in-place?"))

# Expected Token Savings: Catches silent misunderstandings before they become multi-turn correction spirals
# Environment: pip install anthropic
```

## Option 3: Structured Intent Verification Before Long Responses

Before generating a long or complex response, the agent briefly verifies its interpretation of the user's intent. If verification reveals ambiguity, it asks one targeted clarifying question. This prevents spending tokens on a detailed answer to the wrong question.

```python
import anthropic
import json
from dataclasses import dataclass

@dataclass
class IntentVerification:
    understood_intent: str
    confidence: float
    ambiguous_aspects: list[str]
    should_clarify: bool

VERIFY_SYSTEM = """Before answering, verify your understanding of the user's intent.

Respond with ONLY valid JSON:
{
  "understood_intent": "one sentence describing what you think the user wants",
  "confidence": 0.0-1.0,
  "ambiguous_aspects": ["aspect1"],
  "should_clarify": true/false
}

Set should_clarify=true if: confidence < 0.75, or 2+ valid interpretations exist, or answer would be >200 words and intent is unclear.
Set should_clarify=false if: intent is clear, question is specific, or clarifying would be annoying for simple questions."""

COMPLEXITY_THRESHOLD = 150  # words in expected response that trigger pre-verification

def estimate_response_complexity(query: str) -> bool:
    """True if the query likely warrants a long complex response."""
    complexity_signals = [
        "how do i", "explain", "what is the best", "compare",
        "design", "implement", "create a", "build", "architecture",
        "difference between", "pros and cons", "when should i",
    ]
    return any(s in query.lower() for s in complexity_signals)

def verify_intent(client: anthropic.Anthropic, query: str, history: list[dict]) -> IntentVerification:
    context = "\n".join(f"{m['role']}: {m['content'][:100]}" for m in history[-4:])
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=150,
        system=VERIFY_SYSTEM,
        messages=[{
            "role": "user",
            "content": f"Context:\n{context}\n\nNew message: {query}",
        }],
    )
    try:
        data = json.loads(response.content[0].text)
        return IntentVerification(
            understood_intent=data["understood_intent"],
            confidence=float(data["confidence"]),
            ambiguous_aspects=data.get("ambiguous_aspects", []),
            should_clarify=bool(data["should_clarify"]),
        )
    except Exception:
        return IntentVerification(query, 0.9, [], False)

def generate_clarifying_question(client: anthropic.Anthropic, query: str, ambiguous: list[str]) -> str:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=80,
        messages=[{
            "role": "user",
            "content": (
                f"Query: {query}\nAmbiguous aspects: {ambiguous}\n\n"
                f"Ask ONE specific clarifying question that would resolve the main ambiguity. "
                f"Be brief and friendly. Output ONLY the question."
            ),
        }],
    )
    return response.content[0].text.strip()

def smart_answer(query: str, history: list[dict] = None) -> str:
    client = anthropic.Anthropic()
    history = history or []
    is_complex = estimate_response_complexity(query)

    if is_complex:
        verification = verify_intent(client, query, history)
        print(f"[IntentVerify] confidence={verification.confidence:.2f} clarify={verification.should_clarify}")
        print(f"[IntentVerify] understood: {verification.understood_intent}")

        if verification.should_clarify:
            question = generate_clarifying_question(client, query, verification.ambiguous_aspects)
            return f"Before I answer: {question}"

        # Answer with confirmed understanding as context
        system = f"You are answering this specific intent: {verification.understood_intent}. Be direct and complete."
    else:
        system = "You are a helpful assistant."

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=600,
        system=system,
        messages=history + [{"role": "user", "content": query}],
    )
    return response.content[0].text

if __name__ == "__main__":
    tests = [
        ("What is Python?", []),  # Simple — no verify
        ("How do I implement authentication?", []),  # Complex — ambiguous (web? mobile? JWT? OAuth?)
        ("Compare asyncio and threading", []),  # Complex — should verify use case
        ("What time is it?", []),  # Simple — no verify
    ]
    for q, hist in tests:
        print(f"\nQ: {q}")
        result = smart_answer(q, hist)
        print(f"A: {result[:200]}")

# Expected Token Savings: Prevents 400-800 token wasted responses to ambiguous questions
# Environment: pip install anthropic
```

## Option 4: Post-Response Self-Critique with Repair Offer

After generating a response, run a fast self-critique to check whether the answer actually addresses the user's request. If the critique identifies a mismatch, append a repair offer at the end of the response without making the user ask for it.

```python
import anthropic
import json
from dataclasses import dataclass

@dataclass
class SelfCritique:
    addresses_request: bool
    confidence: float
    gap: str | None

CRITIQUE_SYSTEM = """Evaluate whether an AI response fully addresses the user's request.

Respond with ONLY valid JSON:
{"addresses_request": true/false, "confidence": 0.0-1.0, "gap": "description or null"}

addresses_request=false if: response answers a different question, misses the key ask, is off-topic, or ignores an important constraint."""

def self_critique(
    client: anthropic.Anthropic,
    user_request: str,
    agent_response: str,
) -> SelfCritique:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=100,
        system=CRITIQUE_SYSTEM,
        messages=[{
            "role": "user",
            "content": f"User asked: {user_request}\n\nAgent responded: {agent_response[:500]}",
        }],
    )
    try:
        data = json.loads(response.content[0].text)
        return SelfCritique(
            addresses_request=bool(data["addresses_request"]),
            confidence=float(data["confidence"]),
            gap=data.get("gap"),
        )
    except Exception:
        return SelfCritique(True, 0.8, None)

def answer_with_self_critique(query: str, history: list[dict] = None) -> str:
    client = anthropic.Anthropic()
    history = history or []

    # Generate primary response
    primary = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=history + [{"role": "user", "content": query}],
    )
    primary_text = primary.content[0].text

    # Self-critique
    critique = self_critique(client, query, primary_text)
    print(f"[SelfCritique] addresses={critique.addresses_request} conf={critique.confidence:.2f} gap={critique.gap}")

    if not critique.addresses_request and critique.confidence >= 0.75 and critique.gap:
        # Append repair offer
        repair_offer = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=100,
            messages=[{
                "role": "user",
                "content": (
                    f"The user asked: '{query}'\n"
                    f"The response missed: {critique.gap}\n\n"
                    f"Write a ONE-sentence repair offer that acknowledges the gap and asks if they'd like the correct answer. "
                    f"Be natural, not robotic."
                ),
            }],
        )
        return primary_text + f"\n\n---\n💡 {repair_offer.content[0].text}"

    return primary_text

if __name__ == "__main__":
    # Scenario where response likely misses the mark
    tests = [
        "How do I sort a list of dictionaries by a specific key?",
        "What's the difference between is and == in Python?",
        "How do I read a CSV file in Python using pandas, not the csv module?",  # Specific constraint
    ]
    for q in tests:
        print(f"\nQ: {q}")
        result = answer_with_self_critique(q)
        print(f"A: {result[:350]}")

# Expected Token Savings: Haiku critique (~100 tokens) prevents full re-explanation from user
# Environment: pip install anthropic
```

## Option 5: Conversation Repair State Machine

Model conversation health as a state machine: NORMAL → DEGRADED (signals of confusion) → REPAIR (active repair underway) → RECOVERED. Track state across turns and adjust agent behavior per state.

```python
import anthropic
import re
from dataclasses import dataclass, field
from enum import Enum

class ConversationState(Enum):
    NORMAL = "normal"
    DEGRADED = "degraded"
    REPAIR = "repair"
    RECOVERED = "recovered"

@dataclass
class ConversationStateMachine:
    state: ConversationState = ConversationState.NORMAL
    degradation_signals: int = 0
    repair_attempts: int = 0
    last_repair_turn: int = 0
    turn_count: int = 0

    CORRECTION_SIGNALS = [r"(?i)\b(no|wrong|not\s+right|not\s+what|misunderstood|actually)\b"]
    CONFUSION_SIGNALS = [r"(?i)\b(confused|unclear|don'?t\s+understand|what\s+do\s+you\s+mean)\b"]
    RECOVERY_SIGNALS = [r"(?i)\b(yes|perfect|exactly|that'?s?\s+right|got\s+it|thanks)\b"]

    def process_signal(self, message: str) -> None:
        self.turn_count += 1
        lower = message.lower()
        has_correction = any(re.search(p, lower) for p in self.CORRECTION_SIGNALS)
        has_confusion = any(re.search(p, lower) for p in self.CONFUSION_SIGNALS)
        has_recovery = any(re.search(p, lower) for p in self.RECOVERY_SIGNALS)

        if has_recovery and self.state in (ConversationState.REPAIR, ConversationState.DEGRADED):
            self.state = ConversationState.RECOVERED
            self.degradation_signals = 0
        elif has_correction or has_confusion:
            self.degradation_signals += 1
            if self.degradation_signals >= 2 or has_correction:
                self.state = ConversationState.REPAIR
            else:
                self.state = ConversationState.DEGRADED
        elif self.state == ConversationState.RECOVERED:
            self.state = ConversationState.NORMAL
            self.degradation_signals = 0

    def get_system_addendum(self) -> str:
        if self.state == ConversationState.REPAIR:
            return (
                " REPAIR MODE: The user indicated a misunderstanding. "
                "Start with a brief apology, re-state your new understanding of their request, "
                "then provide the corrected answer."
            )
        elif self.state == ConversationState.DEGRADED:
            return " CAUTION: User may be confused. Be extra clear and check your answer matches the request."
        elif self.state == ConversationState.RECOVERED:
            return " User confirmed understanding. Continue naturally."
        return ""

class RepairableAgent:
    def __init__(self):
        self.fsm = ConversationStateMachine()
        self.history: list[dict] = []
        self._client = anthropic.Anthropic()

    def chat(self, message: str) -> str:
        self.fsm.process_signal(message)
        print(f"[StateMachine] State: {self.fsm.state.value} | Signals: {self.fsm.degradation_signals}")

        base_system = "You are a helpful Python tutor."
        system = base_system + self.fsm.get_system_addendum()

        self.history.append({"role": "user", "content": message})
        response = self._client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            system=system,
            messages=self.history,
        )
        reply = response.content[0].text
        self.history.append({"role": "assistant", "content": reply})
        return reply

if __name__ == "__main__":
    agent = RepairableAgent()
    conversation = [
        "How do I add something to a list?",
        "No, I meant insert at a specific position, not append",
        "Actually show me the code",
        "Yes that's exactly what I needed, thanks",
        "Now how do I remove an element?",
    ]
    for turn in conversation:
        print(f"\nUser: {turn}")
        reply = agent.chat(turn)
        print(f"Agent: {reply[:150]}")

# Expected Token Savings: State machine overhead ~0 tokens; prevents correction loops worth 400-1000 tokens
# Environment: pip install anthropic
```

## Option 6: Async Parallel Intent Disambiguation

When an ambiguous query arrives, fan out to 3 parallel model instances each interpreting the query differently. Present all interpretations to the user in a single response and ask which one they meant. Resolves ambiguity in one turn instead of a correction loop.

```python
import anthropic
import asyncio
from dataclasses import dataclass

@dataclass
class Interpretation:
    interpretation_id: str
    restatement: str
    brief_answer: str

async def generate_interpretation(
    client: anthropic.AsyncAnthropic,
    query: str,
    interpretation_angle: str,
    interp_id: str,
) -> Interpretation:
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=100,
        messages=[{
            "role": "user",
            "content": (
                f"Query: '{query}'\n"
                f"Interpretation angle: {interpretation_angle}\n\n"
                f"In 1-2 sentences: how would you restate the query under this angle, "
                f"and what would be the brief answer? Format: RESTATEMENT: ... | ANSWER: ..."
            ),
        }],
    )
    text = response.content[0].text
    parts = text.split("|")
    restatement = parts[0].replace("RESTATEMENT:", "").strip() if parts else text[:80]
    answer = parts[1].replace("ANSWER:", "").strip() if len(parts) > 1 else ""
    return Interpretation(interp_id, restatement, answer)

def is_ambiguous(client: anthropic.Anthropic, query: str) -> bool:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=20,
        messages=[{
            "role": "user",
            "content": f"Is this query ambiguous (multiple valid interpretations)? '{query}' Reply ONLY: yes or no",
        }],
    )
    return "yes" in response.content[0].text.lower()

async def disambiguate_and_answer(query: str) -> str:
    sync_client = anthropic.Anthropic()
    async_client = anthropic.AsyncAnthropic()

    if not is_ambiguous(sync_client, query):
        print("[Disambiguate] Not ambiguous — answering directly")
        response = sync_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            messages=[{"role": "user", "content": query}],
        )
        return response.content[0].text

    print("[Disambiguate] Ambiguous query — generating interpretations in parallel")
    angles = [
        "practical/how-to perspective",
        "conceptual/theoretical perspective",
        "use-case/when-to-use perspective",
    ]
    interpretations = await asyncio.gather(*[
        generate_interpretation(async_client, query, angle, f"A{i+1}")
        for i, angle in enumerate(angles)
    ])

    options = "\n".join(
        f"{interp.interpretation_id}. {interp.restatement}"
        + (f"\n   → {interp.brief_answer}" if interp.brief_answer else "")
        for interp in interpretations
    )

    return (
        f"I want to make sure I answer the right question. Did you mean:\n\n"
        f"{options}\n\n"
        f"Which interpretation matches what you're looking for? (or describe your intent and I'll answer directly)"
    )

async def main():
    queries = [
        "How do I handle Python errors?",        # Ambiguous: try/except? logging? user feedback?
        "What is a Python decorator?",            # Clear enough — should answer directly
        "How do I use Python classes?",           # Ambiguous: basic OOP? dataclasses? metaclasses?
    ]
    for q in queries:
        print(f"\nQ: {q}")
        result = await disambiguate_and_answer(q)
        print(f"A: {result[:300]}")

if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: One disambiguation turn (3 × ~80 tokens) prevents 2-3 correction turns
# Environment: pip install anthropic
```

## Comparison

| Option | Detection | Proactive | Cost | Best For |
|--------|----------|----------|------|----------|
| 1. Signal Detection | Explicit corrections | Yes | +1 repair call | Conversational agents |
| 2. Implicit Signals | Engagement patterns | Proactive check-in | +0 overhead | Chat-heavy UX |
| 3. Pre-Response Verification | Pre-answer | Yes (for complex) | +Haiku verify | Long-form answers |
| 4. Self-Critique | Post-answer | Auto repair offer | +Haiku critique | API services |
| 5. State Machine | Turn-by-turn | State-adaptive | +0 overhead | Long sessions |
| 6. Parallel Disambiguation | Pre-answer | Disambiguation | +3×Haiku | Highly ambiguous domains |
