---
layout: solution
title: "Agent Pays for Verbose Preamble Before Actual Answer"
category: token-cost
description: "Agent outputs 'Certainly! I'd be happy to help you with that. Let me think through this carefully...' before every answer, burning output tokens on filler text that adds no value and increases latency."
tags: [token-cost, output-tokens, prompt-engineering, latency, preamble]
---

## Symptom

Every agent response starts with 3–6 sentences of acknowledgement before the actual content:

```
Certainly! I'd be happy to help you with that request.
Let me think through this carefully and provide you with a comprehensive answer.
This is a great question that touches on several important concepts.

The answer is: Paris.
```

The user pays for ~60 output tokens of filler before reaching the 5-token real answer. At scale, preamble can account for 20–40% of output token spend.

## Root Cause

The model learned from RLHF that users rate polite, enthusiastic responses higher — so it defaults to social pleasantries. Without explicit instructions to suppress this, the model front-loads filler. The anti-pattern is a system prompt that says nothing about output format:

```python
import anthropic

client = anthropic.Anthropic(api_key="sk-live-...")

response = client.messages.create(
    model="claude-sonnet-4-6",
    max_tokens=1024,
    system="You are a helpful assistant.",  # no format guidance
    messages=[{"role": "user", "content": "What is the capital of France?"}]
)
print(response.content[0].text)
# → "Certainly! I'd be happy to answer that question. The capital of France is Paris,
#    a city known for the Eiffel Tower and rich cultural heritage. I hope that helps!"
```

---

## Fix

### Option 1 — Direct suppression in system prompt

Add explicit no-preamble instructions to the system prompt. The simplest and most universally effective fix.

```python
import anthropic

client = anthropic.Anthropic(api_key="sk-live-...")

NO_PREAMBLE_SYSTEM = """You are a precise assistant.

Rules:
- Start your response with the answer immediately. No preamble.
- Do not say "Certainly", "Of course", "Sure", "I'd be happy to", or similar openers.
- Do not restate the question before answering.
- Do not add closing remarks like "I hope that helps!" or "Let me know if you need anything else."
- If the answer is one sentence, output one sentence.
"""

response = client.messages.create(
    model="claude-sonnet-4-6",
    max_tokens=512,
    system=NO_PREAMBLE_SYSTEM,
    messages=[{"role": "user", "content": "What is the capital of France?"}]
)
print(response.content[0].text)
# → "Paris."

# Expected Token Savings: 40–80 output tokens per call (varies by question complexity)
# Environment: any agent where response conciseness matters — chatbots, pipelines, CLIs
```

---

### Option 2 — Prefill the assistant turn to skip preamble

Inject the start of the assistant's reply to force it to begin mid-sentence, skipping any warm-up text.

```python
import anthropic

client = anthropic.Anthropic(api_key="sk-live-...")

def ask_with_prefill(question: str, prefill: str = "") -> str:
    """
    Prefill the assistant turn so the model continues from `prefill`
    rather than generating an opener.
    """
    messages = [{"role": "user", "content": question}]

    if prefill:
        # The assistant turn already "started" — model completes from here
        messages.append({"role": "assistant", "content": prefill})

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=messages
    )

    text = response.content[0].text if response.content else ""
    # Prepend the prefill so the caller gets the full coherent response
    return prefill + text


# Force the answer to start with a capital letter — no preamble possible
answer = ask_with_prefill("What is the capital of France?", prefill="The capital of France is")
print(answer)
# → "The capital of France is Paris."

# Code summary generation with forced opener
summary = ask_with_prefill(
    "Summarize this function: def add(a, b): return a + b",
    prefill="This function"
)
print(summary)
# → "This function adds two numbers and returns their sum."

# Expected Token Savings: 30–70 tokens per call; model skips the preamble generation entirely
# Environment: structured pipelines where the answer format is known in advance
```

---

### Option 3 — Output format schema with JSON mode

Request structured JSON output. JSON-mode responses never include preamble because the model must produce valid syntax from token 1.

```python
import anthropic
import json

client = anthropic.Anthropic(api_key="sk-live-...")

JSON_SYSTEM = """You are a precise assistant. Always respond with valid JSON matching the schema provided.
Never include text outside the JSON object."""

def ask_json(question: str, schema_description: str) -> dict:
    prompt = f"""Question: {question}

Respond with JSON matching this schema: {schema_description}"""

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=JSON_SYSTEM,
        messages=[{"role": "user", "content": prompt}]
    )

    text = response.content[0].text if response.content else "{}"

    # Strip accidental markdown code fences if present
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1])

    return json.loads(text)


result = ask_json(
    "What is the capital of France?",
    '{"answer": "string", "country": "string"}'
)
print(result)
# → {"answer": "Paris", "country": "France"}

# For extraction tasks:
extraction = ask_json(
    "Extract entities: 'Elon Musk founded SpaceX in 2002.'",
    '{"people": ["string"], "organizations": ["string"], "years": ["integer"]}'
)
print(extraction)
# → {"people": ["Elon Musk"], "organizations": ["SpaceX"], "years": [2002]}

# Expected Token Savings: 50–100 tokens per call; JSON framing eliminates all filler
# Environment: data extraction, classification, and any structured-output pipeline
```

---

### Option 4 — Post-generation preamble stripper

For cases where you cannot change the system prompt (shared clients, third-party integrations), strip preamble from the output as a post-processing step.

```python
import re
import anthropic

client = anthropic.Anthropic(api_key="sk-live-...")

# Patterns that indicate preamble — match from start of string
PREAMBLE_PATTERNS = [
    r"^(Certainly|Of course|Sure|Absolutely|Great question)[!,.]?\s*",
    r"^I('d| would) (be happy|love|be glad) to (help|assist)[^.]*\.\s*",
    r"^Let me (think|help|explain|break)[^.]*\.\s*",
    r"^(This is a|That's a|What an?) (great|excellent|interesting|good)[^.]*\.\s*",
    r"^Thank you for (asking|your question)[^.]*\.\s*",
    r"^Of course! ",
]

CLOSING_PATTERNS = [
    r"\s*I hope (this|that) helps[!.]?\s*$",
    r"\s*Let me know if you (need|have)[^.]*\.\s*$",
    r"\s*Feel free to ask[^.]*\.\s*$",
    r"\s*Is there anything else[^?]*\?\s*$",
]

def strip_preamble(text: str) -> str:
    """Remove common LLM preamble and closing filler."""
    for pattern in PREAMBLE_PATTERNS:
        text = re.sub(pattern, "", text, flags=re.IGNORECASE)
    for pattern in CLOSING_PATTERNS:
        text = re.sub(pattern, "", text, flags=re.IGNORECASE)
    return text.strip()


def ask(question: str) -> str:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=[{"role": "user", "content": question}]
    )
    raw = response.content[0].text if response.content else ""
    clean = strip_preamble(raw)
    return clean

print(ask("What is 2 + 2?"))
# Input:  "Certainly! I'd be happy to help. 2 + 2 = 4. I hope that helps!"
# Output: "2 + 2 = 4."

# Expected Token Savings: tokens are still generated (preamble was paid for); saves downstream
#   processing overhead and improves UX. Best combined with Option 1 for real savings.
# Environment: legacy integrations where system prompt cannot be changed
```

---

### Option 5 — Token-budget-aware dynamic instruction injection

Monitor output token consumption per turn. If preamble is detected in the first 30 tokens, inject a correction and retry with stronger instructions.

```python
import anthropic

client = anthropic.Anthropic(api_key="sk-live-...")

PREAMBLE_OPENERS = (
    "certainly", "of course", "sure", "absolutely",
    "i'd be happy", "i would be happy", "great question",
    "thank you for", "let me think", "let me help",
)

BASE_SYSTEM = "You are a concise assistant. Answer directly without preamble."

STRICT_SYSTEM = """You are a concise assistant.

CRITICAL: Start your response with the actual answer. The very first word must be
substantive content — never an acknowledgement, greeting, or meta-comment.

BAD: "Certainly! The answer is Paris."
GOOD: "Paris."

BAD: "I'd be happy to help. Here's a Python function..."
GOOD: "def greet(name): return f'Hello, {name}'"
"""

def ask_no_preamble(question: str) -> str:
    for attempt, system in enumerate([BASE_SYSTEM, STRICT_SYSTEM]):
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            system=system,
            messages=[{"role": "user", "content": question}]
        )

        text = response.content[0].text if response.content else ""

        # Detect preamble in first 30 tokens (~first 20 words)
        first_words = text[:120].lower()
        has_preamble = any(opener in first_words for opener in PREAMBLE_OPENERS)

        if not has_preamble or attempt == 1:
            return text  # Good response or gave up after retry

        # First attempt had preamble — retry with stricter instructions
        print(f"[preamble detected on attempt {attempt+1}, retrying]")

    return text

print(ask_no_preamble("What does async/await do in Python?"))

# Expected Token Savings: retry costs ~1.5x on the rare call where preamble slips through;
#   overall savings 30–60 tokens on 95%+ of calls that pass on first attempt
# Environment: high-volume agents where per-call preamble cost compounds significantly
```

---

### Option 6 — Per-persona system prompt with cached no-preamble block

For agents serving multiple personas (customer support, code assistant, analyst), cache a shared no-preamble block using prompt caching to avoid paying to re-process it on every call.

```python
import anthropic

client = anthropic.Anthropic(api_key="sk-live-...")

# Shared no-preamble rules — cached once, reused across all personas
NO_PREAMBLE_BLOCK = {
    "type": "text",
    "text": """Output format rules (apply to every response):
1. Start with the answer. Never with an acknowledgement.
2. Forbidden openers: Certainly, Of course, Sure, Absolutely, Great question,
   I'd be happy to, I would love to, Let me think, Thank you for asking.
3. Forbidden closers: I hope that helps, Let me know if you need anything,
   Feel free to ask, Is there anything else I can help you with.
4. Match length to complexity: one-word answer → one word. Code question → code block.
5. No padding, no restating the question, no meta-commentary.
""",
    "cache_control": {"type": "ephemeral"},  # Cache this block
}

PERSONAS = {
    "support": "You are a customer support agent for a SaaS product.",
    "code":    "You are a senior Python engineer reviewing code.",
    "analyst": "You are a data analyst interpreting business metrics.",
}

def ask_persona(persona: str, question: str) -> str:
    persona_text = PERSONAS.get(persona, "You are a helpful assistant.")

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=[
            NO_PREAMBLE_BLOCK,  # cached — paid once per cache TTL
            {"type": "text", "text": persona_text},  # persona-specific, not cached
        ],
        messages=[{"role": "user", "content": question}],
        extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
    )

    return response.content[0].text if response.content else ""


# First call — pays to cache the no-preamble block
r1 = ask_persona("support", "How do I reset my password?")
print(r1)
# → "Go to Settings → Account → Reset Password and follow the email link."

# Second call (different persona) — no-preamble block is already cached
r2 = ask_persona("code", "What does list comprehension do?")
print(r2)
# → "List comprehension creates a new list by applying an expression to each item in an iterable."

# Expected Token Savings: ~60–80 tokens preamble elimination per call PLUS
#   ~85% cache discount on the shared no-preamble block (~45 tokens) after first call
# Environment: multi-persona agents or high-volume single-persona services
```

---

## Comparison

| Option | Approach | Actual Token Saved | Retry Risk | Works Without System Prompt Access |
|--------|----------|--------------------|-----------|-------------------------------------|
| 1 | System prompt suppression | Yes (preamble never generated) | None | No |
| 2 | Assistant turn prefill | Yes (model skips preamble) | None | Yes |
| 3 | JSON output schema | Yes (format forces direct start) | None | No |
| 4 | Post-generation stripper | No (tokens paid, text removed) | None | Yes |
| 5 | Dynamic retry on detection | Yes on 95%+ of calls | 1 retry (~5%) | No |
| 6 | Cached no-preamble block | Yes + cache discount | None | No |

**Recommended starting point:** Option 1 (zero complexity, immediate savings). Add Option 2 (prefill) for structured pipelines where the response opener is predictable. Use Option 6 when serving multiple personas at high volume to combine preamble elimination with prompt cache savings.
