---
layout: solution
title: "Agent Over-Explains Simple Answers"
category: prompt-engineering
description: "Agent produces verbose multi-paragraph responses to questions that need a single sentence — burning tokens, frustrating users, and slowing down high-throughput applications."
tags: [prompt-engineering, verbosity, conciseness, token-cost, response-length, haiku]
---

## Symptom

User asks: *"What is 2 + 2?"*

Agent responds:
> "Great question! Mathematics is a fascinating field that has been studied for thousands of years. The operation you are asking about is called addition, which is one of the four basic arithmetic operations. When we add the number 2 to itself, we apply the commutative property of addition... The answer is **4**."

The answer is buried in 200 tokens of preamble. For high-throughput applications, this multiplies cost and latency by 5–10x.

## Root Cause

By default, Claude aims to be thorough and helpful. Without an explicit length constraint, it optimises for completeness rather than brevity. The model has no signal that the user wants a short answer.

## Fix

---

### Option 1 — Explicit Brevity Instruction in System Prompt

Add a clear, unambiguous brevity directive to the system prompt. Use specific constraints ("one sentence", "under 20 words") rather than vague guidance ("be brief").

```python
import anthropic

client = anthropic.Anthropic()

CONCISE_SYSTEM_PROMPT = """You are a concise assistant.

RESPONSE RULES:
- Answer in ONE sentence unless the question genuinely requires more
- Maximum 30 words for factual questions
- No preamble ("Great question!", "Of course!", "Certainly!")
- No postamble ("I hope that helps!", "Let me know if you need more!")
- No restating the question
- Never apologise for being brief

If the question requires a list: use a maximum of 5 bullet points, one line each.
If the question requires code: provide the code with a one-line explanation only."""

def ask_concise(question: str) -> str:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=CONCISE_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": question}],
    )
    return response.content[0].text

# Test conciseness
questions = [
    "What is the capital of France?",
    "What is 2 + 2?",
    "What does HTTP stand for?",
    "What is the speed of light?",
]

for q in questions:
    answer = ask_concise(q)
    print(f"Q: {q}")
    print(f"A: {answer}")
    print(f"   ({len(answer.split())} words)")
    print()
```

**Expected Token Savings:** ~70% reduction in output tokens for factual Q&A
**Environment:** `pip install anthropic`

---

### Option 2 — Question Complexity Classifier + Adaptive Max Tokens

Before sending to the main model, classify the question's complexity with Haiku. Use the complexity score to set `max_tokens` dynamically — simple questions get a strict token budget.

```python
import anthropic
import json

client = anthropic.Anthropic()

def classify_complexity(question: str) -> dict:
    """Returns {complexity: 'simple'|'moderate'|'complex', max_tokens: int}"""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        system=(
            'Classify the question complexity. '
            'Respond with ONLY valid JSON: {"complexity": "simple"|"moderate"|"complex"}'
        ),
        messages=[{"role": "user", "content": question}],
    )
    try:
        result = json.loads(response.content[0].text.strip())
        complexity = result.get("complexity", "moderate")
    except (json.JSONDecodeError, KeyError):
        complexity = "moderate"

    token_budgets = {
        "simple": 64,
        "moderate": 512,
        "complex": 2048,
    }

    return {
        "complexity": complexity,
        "max_tokens": token_budgets[complexity],
    }

SYSTEM_BY_COMPLEXITY = {
    "simple": "Answer in one short sentence. No preamble. No explanation unless asked.",
    "moderate": "Be clear and concise. Answer the question directly in 2-4 sentences.",
    "complex": "Provide a thorough, well-structured answer. Use headers and lists as needed.",
}

def adaptive_answer(question: str) -> tuple[str, str, int]:
    classification = classify_complexity(question)
    complexity = classification["complexity"]
    max_tokens = classification["max_tokens"]

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=max_tokens,
        system=SYSTEM_BY_COMPLEXITY[complexity],
        messages=[{"role": "user", "content": question}],
    )

    answer = response.content[0].text
    return answer, complexity, len(answer.split())

test_questions = [
    "What year did World War II end?",
    "How does HTTPS work?",
    "Explain the CAP theorem and its implications for distributed database design.",
]

for q in test_questions:
    answer, complexity, word_count = adaptive_answer(q)
    print(f"Q: {q}")
    print(f"Complexity: {complexity} | Words: {word_count}")
    print(f"A: {answer}")
    print()
```

**Expected Token Savings:** ~80% for simple questions, ~50% for moderate vs unconstrained
**Environment:** `pip install anthropic`

---

### Option 3 — Response Length Validator with Regeneration

After receiving a response, check if it exceeds a word count threshold. If so, request a shorter version with the specific word target.

```python
import anthropic

client = anthropic.Anthropic()

WORD_LIMITS = {
    "factual": 15,
    "explanation": 80,
    "tutorial": 300,
}

def get_word_limit(question: str) -> tuple[str, int]:
    q_lower = question.lower()
    if any(w in q_lower for w in ["how to", "explain how", "step by step", "tutorial"]):
        return "tutorial", WORD_LIMITS["tutorial"]
    if any(w in q_lower for w in ["explain", "why", "what is", "describe"]):
        return "explanation", WORD_LIMITS["explanation"]
    return "factual", WORD_LIMITS["factual"]

def ask_with_length_enforcement(question: str, max_retries: int = 2) -> str:
    question_type, word_limit = get_word_limit(question)
    messages = [{"role": "user", "content": question}]

    for attempt in range(max_retries + 1):
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=max(word_limit * 3, 64),  # tokens ≈ words * 1.3
            system=f"Answer concisely. Target: under {word_limit} words.",
            messages=messages,
        )
        answer = response.content[0].text
        word_count = len(answer.split())

        if word_count <= word_limit * 1.5:
            return answer

        if attempt < max_retries:
            messages.append({"role": "assistant", "content": answer})
            messages.append({
                "role": "user",
                "content": (
                    f"Too long ({word_count} words). Please answer in under {word_limit} words. "
                    "Remove all preamble and explanation. Give only the core answer."
                ),
            })

    # Return last attempt even if still long
    return answer

examples = [
    "What is the boiling point of water in Celsius?",
    "What is machine learning?",
    "How do I reverse a list in Python?",
]

for q in examples:
    answer = ask_with_length_enforcement(q)
    print(f"Q: {q}")
    print(f"A: {answer}")
    print(f"   ({len(answer.split())} words)")
    print()
```

**Expected Token Savings:** ~65% output reduction; slight overhead on retry turns
**Environment:** `pip install anthropic`

---

### Option 4 — Prefill Anchoring for Direct Answers

Use the assistant prefill to force the model into answer-first mode. Starting the response with the key fact leaves no room for preamble.

```python
import anthropic

client = anthropic.Anthropic()

PREFILLS = {
    "what_is": "",           # Let model start directly
    "yes_no": "Yes" ,        # Force yes/no start
    "number": "",            # Let model state the number
    "list": "- ",            # Force bullet list
    "definition": "A ",      # Force article-first definition
}

def detect_prefill(question: str) -> str:
    q = question.strip().lower()
    if q.startswith(("is ", "does ", "can ", "should ", "will ", "are ")):
        return "Yes" if "not" not in q else "No"
    if any(q.startswith(w) for w in ["list ", "give me a list", "what are"]):
        return "- "
    if q.startswith("define ") or "what does" in q:
        return "A "
    return ""

def ask_direct(question: str) -> str:
    prefill = detect_prefill(question)

    messages = [{"role": "user", "content": question}]
    if prefill:
        messages.append({"role": "assistant", "content": prefill})

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        system=(
            "You are a direct assistant. Answer immediately without preamble. "
            "One sentence for facts. Bullet points for lists."
        ),
        messages=messages,
    )

    raw = response.content[0].text
    return (prefill + raw).strip()

test_cases = [
    "Is Python dynamically typed?",
    "What is the capital of Japan?",
    "List the primary colors.",
    "Define REST API.",
    "What is 15% of 200?",
]

for q in test_cases:
    answer = ask_direct(q)
    print(f"Q: {q}")
    print(f"A: {answer}")
    print()
```

**Expected Token Savings:** ~75% reduction; prefill generates 0 additional input cost
**Environment:** `pip install anthropic`

---

### Option 5 — User-Controlled Verbosity Level

Let users set their verbosity preference (terse / normal / detailed) which persists across the session. Inject the appropriate instruction per preference.

```python
import anthropic
from enum import Enum

class Verbosity(str, Enum):
    TERSE = "terse"
    NORMAL = "normal"
    DETAILED = "detailed"

VERBOSITY_INSTRUCTIONS = {
    Verbosity.TERSE: (
        "VERBOSITY: TERSE\n"
        "- Maximum 15 words per answer\n"
        "- Facts only — no context, no explanation\n"
        "- No punctuation beyond commas and periods\n"
        "- Single bullet per list item, max 5 items"
    ),
    Verbosity.NORMAL: (
        "VERBOSITY: NORMAL\n"
        "- 1-3 sentences for most answers\n"
        "- Include brief context only when essential\n"
        "- No preamble or filler phrases"
    ),
    Verbosity.DETAILED: (
        "VERBOSITY: DETAILED\n"
        "- Full explanation with examples\n"
        "- Use headers and structured formatting\n"
        "- Cover edge cases and nuances"
    ),
}

MAX_TOKENS_BY_VERBOSITY = {
    Verbosity.TERSE: 64,
    Verbosity.NORMAL: 256,
    Verbosity.DETAILED: 2048,
}

class VerbosityAwareAgent:
    def __init__(self, verbosity: Verbosity = Verbosity.NORMAL):
        self.client = anthropic.Anthropic()
        self.verbosity = verbosity
        self.messages: list[dict] = []

    def set_verbosity(self, level: Verbosity):
        self.verbosity = level
        print(f"[Verbosity set to: {level.value}]")

    def chat(self, user_message: str) -> str:
        # Check for verbosity command
        lower = user_message.lower().strip()
        if lower in ("terse", "brief", "short"):
            self.set_verbosity(Verbosity.TERSE)
            return "Got it — I'll keep answers very short."
        if lower in ("normal", "default"):
            self.set_verbosity(Verbosity.NORMAL)
            return "Switched to normal response length."
        if lower in ("detailed", "verbose", "explain more"):
            self.set_verbosity(Verbosity.DETAILED)
            return "I'll give detailed explanations from now on."

        self.messages.append({"role": "user", "content": user_message})

        response = self.client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=MAX_TOKENS_BY_VERBOSITY[self.verbosity],
            system=VERBOSITY_INSTRUCTIONS[self.verbosity],
            messages=self.messages,
        )

        reply = response.content[0].text
        self.messages.append({"role": "assistant", "content": reply})
        return reply

agent = VerbosityAwareAgent(verbosity=Verbosity.TERSE)

questions = [
    "What is the speed of light?",
    "What is Python?",
    "detailed",  # Switch to detailed
    "Explain how TCP/IP works.",
]

for q in questions:
    print(f"User: {q}")
    print(f"Agent [{agent.verbosity.value}]: {agent.chat(q)}")
    print()
```

**Expected Token Savings:** ~80% in TERSE mode vs unconstrained; user controls the trade-off
**Environment:** `pip install anthropic`

---

### Option 6 — Post-Processing Trimmer for API Applications

For machine-to-machine contexts where responses are processed programmatically, post-process the output to strip known verbosity patterns before returning to the caller.

```python
import re
import anthropic

client = anthropic.Anthropic()

# Patterns that indicate unnecessary verbosity
PREAMBLE_PATTERNS = [
    r"^(Great|Excellent|Sure|Of course|Certainly|Absolutely|Happy to help)[!,.]?\s*",
    r"^(That'?s? a (great|good|excellent|interesting) question[!.]?\s*)",
    r"^(I'?d be (happy|glad|delighted) to (help|answer|explain)[.!]?\s*)",
    r"^(Thank you for (asking|your question)[.!]?\s*)",
]

POSTAMBLE_PATTERNS = [
    r"\s*(I hope (that|this) (helps?|answers? your question)[.!]?\s*)$",
    r"\s*(Let me know if you (need|have) (any )?(more|other|additional) (questions?|help)[.!]?\s*)$",
    r"\s*(Feel free to ask if you need (more|further) (information|clarification|details?)[.!]?\s*)$",
    r"\s*(Is there anything else I can help (you with)?[?!]?\s*)$",
    r"\s*(Please (let me know|feel free to ask) if (you have|there are) (any )?more (questions?|queries?)[.!]?\s*)$",
]

def strip_verbosity(text: str) -> str:
    # Strip preamble
    for pattern in PREAMBLE_PATTERNS:
        text = re.sub(pattern, "", text, flags=re.IGNORECASE)

    # Strip postamble
    for pattern in POSTAMBLE_PATTERNS:
        text = re.sub(pattern, "", text, flags=re.IGNORECASE)

    # Collapse multiple blank lines
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip()

def ask_trimmed(question: str) -> str:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{"role": "user", "content": question}],
    )
    raw = response.content[0].text
    trimmed = strip_verbosity(raw)

    savings_pct = (1 - len(trimmed) / len(raw)) * 100 if raw else 0
    print(f"[Trimmed {savings_pct:.0f}% of output]")
    return trimmed

# Demo: compare raw vs trimmed
import anthropic as _a
raw_client = _a.Anthropic()

questions = [
    "What is the tallest mountain in the world?",
    "What does JSON stand for?",
]

for q in questions:
    raw_resp = raw_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": q}],
    )
    raw = raw_resp.content[0].text
    trimmed = strip_verbosity(raw)

    print(f"Q: {q}")
    print(f"RAW    ({len(raw.split())}w): {raw[:120]}...")
    print(f"TRIMMED ({len(trimmed.split())}w): {trimmed}")
    print()
```

**Expected Token Savings:** Output tokens aren't billed after generation; reduces downstream processing and UI noise
**Environment:** `pip install anthropic`

---

## Comparison

| Option | Implementation | Token Reduction | User Control | Best For |
|--------|----------------|-----------------|--------------|----------|
| Explicit System Prompt | Trivial | ~70% | No | All agents |
| Complexity Classifier | Medium | ~80% simple | No | Mixed Q&A apps |
| Length Validator | Medium | ~65% | No | Compliance-critical |
| Prefill Anchoring | Low | ~75% | No | Single-answer bots |
| User Verbosity Control | Medium | ~80% terse | Yes | Consumer chat apps |
| Post-Processing Trimmer | Low | Cosmetic | No | API pipeline cleanup |

**Recommended starting point:** Option 1 (Explicit System Prompt) — apply to every agent immediately. Add Option 2 (Complexity Classifier) for high-throughput applications where output token cost matters.
