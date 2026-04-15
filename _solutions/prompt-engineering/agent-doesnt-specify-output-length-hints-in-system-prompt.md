---
layout: solution
title: "Agent Doesn't Specify Output Length Hints in System Prompt"
category: prompt-engineering
description: "Without explicit length guidance in the system prompt, the model produces inconsistently-sized outputs — sometimes a single sentence, sometimes five paragraphs — for the same task type."
tags: [prompt-engineering, system-prompt, token-cost, consistency, production]
---

## Symptom

The agent's output length is unpredictable. A "summarise this article" request returns a single sentence one time and eight paragraphs the next. A customer support response is two words or two pages depending on phrasing. Downstream components that expect bounded output — UI text boxes, API field limits, token budgets — break intermittently. Developers tune `max_tokens` reactively rather than proactively guiding the model to the right length.

## Root Cause

Without explicit length instructions, the model infers appropriate output length from context alone. This inference is inconsistent because the model weighs many signals (question complexity, examples seen in training, conversational tone) and their interaction is not deterministic. Explicit length guidance — word counts, sentence counts, paragraph limits, or structural constraints — anchors the model's output size at the prompt level, which is more reliable than relying on `max_tokens` as a hard cut-off.

## Fix

### Option 1 — Word count instruction in system prompt

```python
import anthropic

client = anthropic.Anthropic()

SYSTEM_PROMPTS = {
    "tweet":     "Respond in exactly 1 sentence of at most 280 characters.",
    "bullet":    "Respond with exactly 3–5 bullet points. Each bullet ≤ 15 words.",
    "summary":   "Respond with a summary of 50–80 words. No bullet points.",
    "paragraph": "Respond with exactly 2 paragraphs. Each paragraph 3–5 sentences.",
    "one_liner": "Respond with exactly one sentence. Maximum 20 words.",
}

def ask(task_type: str, user_message: str) -> str:
    system = SYSTEM_PROMPTS[task_type]
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )
    text = resp.content[0].text
    words = len(text.split())
    print(f"[{task_type}] {words} words | {resp.usage.output_tokens} tokens")
    return text

print(ask("tweet",     "Explain what a neural network is."))
print(ask("bullet",    "What are the benefits of using Python?"))
print(ask("summary",   "Summarise: The Transformer architecture introduced self-attention..."))
print(ask("one_liner", "What is an API?"))
```

**Expected Token Savings:** Constraining to 50–80 words instead of unconstrained output saves 100–500 output tokens per call on verbose models; consistent length also reduces follow-up "can you shorten that?" correction calls.
**Environment:** Any agent with multiple task types that require different output lengths; UI-bound agents where text must fit in a fixed-size container.

---

### Option 2 — Structural length hints: "N items, each ≤ X words"

```python
import anthropic

client = anthropic.Anthropic()

EXTRACTION_SYSTEM = (
    "Extract exactly 5 key facts from the provided text.\n"
    "Format: numbered list 1–5.\n"
    "Each item: at most 12 words.\n"
    "No preamble, no conclusion, just the list."
)

COMPARISON_SYSTEM = (
    "Compare the two options provided.\n"
    "Structure:\n"
    "  Option A: 2 sentences.\n"
    "  Option B: 2 sentences.\n"
    "  Recommendation: 1 sentence.\n"
    "Total response: 5 sentences maximum."
)

DEFINITION_SYSTEM = (
    "Define the term in exactly 2 sentences:\n"
    "  Sentence 1: What it is.\n"
    "  Sentence 2: Why it matters or a real-world example.\n"
    "Do not add any other content."
)

def ask(system: str, user_message: str) -> str:
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )
    return resp.content[0].text

print(ask(EXTRACTION_SYSTEM,
    "Python is a high-level language known for readability. It was created by Guido van Rossum "
    "in 1991. Python supports multiple programming paradigms. It is widely used in data science, "
    "web development, and automation. The language emphasises code readability with significant whitespace."))

print(ask(COMPARISON_SYSTEM,
    "Option A: PostgreSQL — mature, ACID compliant, excellent for complex queries.\n"
    "Option B: MongoDB — flexible schema, horizontal scaling, good for document workloads."))

print(ask(DEFINITION_SYSTEM, "What is gradient descent?"))
```

**Expected Token Savings:** Structural hints (5 items, each ≤ 12 words) produce output of predictable token counts; eliminates verbose preambles and trailing summaries that can double output length.
**Environment:** Extraction agents, comparison tools, definition generators; any task with a defined output schema where structural consistency matters.

---

### Option 3 — Adaptive length hint based on input length

```python
import anthropic

client = anthropic.Anthropic()

def length_hint_for_input(input_text: str) -> str:
    """Select appropriate length constraint based on input size."""
    words = len(input_text.split())
    if words < 50:
        return "Respond in 1–2 sentences only."
    elif words < 200:
        return "Respond in 3–5 sentences."
    elif words < 500:
        return "Respond in 1–2 short paragraphs (4–8 sentences total)."
    else:
        return "Respond in 2–3 paragraphs. Each paragraph 4–6 sentences."

SYSTEM_BASE = (
    "You are a concise summariser. Extract the key ideas and insights. "
    "{length_hint}"
)

def summarise(text: str) -> str:
    hint   = length_hint_for_input(text)
    system = SYSTEM_BASE.format(length_hint=hint)
    resp   = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=system,
        messages=[{"role": "user", "content": f"Summarise:\n\n{text}"}],
    )
    result = resp.content[0].text
    print(f"[input={len(text.split())}w] hint='{hint}' → output={len(result.split())}w")
    return result

# Short input → 1–2 sentences
print(summarise("Python was created in 1991 by Guido van Rossum. It is known for readability."))
print()
# Medium input → 3–5 sentences
print(summarise(
    "Machine learning is a subset of artificial intelligence that enables computers to learn "
    "from data without being explicitly programmed. " * 4
))
print()
# Long input → 2–3 paragraphs
print(summarise(
    "Deep learning is a subset of machine learning that uses neural networks with many layers. "
    "These networks can learn hierarchical representations of data. " * 12
))
```

**Expected Token Savings:** Adaptive hints prevent one-sentence summaries of long documents (user follow-up) and multi-paragraph responses to short inputs (wasted tokens); right-sizes output to input complexity.
**Environment:** Summarisation agents handling inputs of varying length; content processing pipelines where output should scale proportionally to input.

---

### Option 4 — Length hint via assistant prefill

```python
import anthropic

client = anthropic.Anthropic()

def ask_with_prefill(prompt: str, prefill: str, system: str = "", max_tokens: int = 256) -> str:
    """
    Use assistant prefill to anchor the model's output structure and length.
    The prefill becomes the first tokens of the response.
    """
    messages = [
        {"role": "user",      "content": prompt},
        {"role": "assistant", "content": prefill},
    ]
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=max_tokens,
        system=system,
        messages=messages,
    )
    full_text = prefill + resp.content[0].text
    return full_text

# Prefill anchors output to exactly the right format/length
result = ask_with_prefill(
    prompt="What is Docker?",
    prefill="Docker is",  # Forces a definitional one-sentence structure
    system="Answer in exactly one sentence.",
    max_tokens=64,
)
print("Definition:", result)
print()

# Prefill forces numbered list with exactly 3 items
result = ask_with_prefill(
    prompt="Give me 3 reasons to use async programming.",
    prefill="1.",  # Forces numbered list structure
    system="Provide exactly 3 numbered reasons. Each reason: one sentence.",
    max_tokens=128,
)
print("Three reasons:\n", result)
print()

# Prefill forces JSON structure
result = ask_with_prefill(
    prompt="Describe Python the programming language as a JSON object with fields: name, year, creator.",
    prefill='{"name":',
    system="Respond with only valid JSON.",
    max_tokens=64,
)
print("JSON:", result)
```

**Expected Token Savings:** Prefill eliminates preambles ("Sure! Here's the answer...") that consume 10–30 tokens before the actual content begins; forces output into the right format immediately.
**Environment:** Agents requiring JSON output, numbered lists, or specific opening phrases; tasks where the first tokens determine the structure of the rest of the response.

---

### Option 5 — Length hint with post-generation word count validation

```python
import anthropic
import re

client = anthropic.Anthropic()

def count_words(text: str) -> int:
    return len(re.findall(r'\b\w+\b', text))

def ask_with_length_validation(
    prompt: str,
    system: str,
    min_words: int,
    max_words: int,
    max_retries: int = 2,
) -> str:
    for attempt in range(max_retries + 1):
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        text = resp.content[0].text
        words = count_words(text)
        print(f"[attempt {attempt+1}] {words} words (target {min_words}–{max_words})")

        if min_words <= words <= max_words:
            return text

        if words < min_words and attempt < max_retries:
            # Too short — ask for expansion
            system = system + f" Important: your response must be at least {min_words} words."
        elif words > max_words and attempt < max_retries:
            # Too long — enforce brevity
            system = system + f" Important: your response must be at most {max_words} words. Be more concise."

    # Return best attempt even if out of range
    return text

SYSTEM = "You are a product description writer. Write engaging, accurate descriptions."

result = ask_with_length_validation(
    prompt="Describe a noise-cancelling headphone.",
    system=SYSTEM,
    min_words=40,
    max_words=60,
)
print(f"\nFinal ({count_words(result)} words): {result}")
```

**Expected Token Savings:** Validation loop catches length violations before content reaches downstream systems; correction prompt is much cheaper than a customer support ticket about truncated product descriptions.
**Environment:** E-commerce agents, marketing copy generators, or any system with hard character/word limits in downstream display fields.

---

### Option 6 — Per-audience length profile: verbose for humans, terse for APIs

```python
import anthropic
from dataclasses import dataclass

client = anthropic.Anthropic()

@dataclass
class AudienceProfile:
    name:       str
    length_hint: str
    max_tokens: int
    model:      str = "claude-haiku-4-5-20251001"

PROFILES = {
    "executive":  AudienceProfile("executive",  "2–3 sentences. Lead with the key number or decision.",  64,  "claude-haiku-4-5-20251001"),
    "developer":  AudienceProfile("developer",  "Technical, precise. Use bullet points for steps. 100–150 words.",  256, "claude-haiku-4-5-20251001"),
    "customer":   AudienceProfile("customer",   "Friendly, jargon-free. 2–3 short paragraphs.",         200, "claude-haiku-4-5-20251001"),
    "api_caller": AudienceProfile("api_caller", "JSON only. No explanation.",                            128, "claude-haiku-4-5-20251001"),
    "slack":      AudienceProfile("slack",      "1–2 short sentences. No markdown. Emoji OK.",           64,  "claude-haiku-4-5-20251001"),
}

SYSTEM_TEMPLATE = (
    "You are a helpful assistant. Tailor your response for a {name} audience.\n"
    "Length and format: {length_hint}"
)

def ask_for_audience(audience_key: str, question: str) -> str:
    profile = PROFILES[audience_key]
    system  = SYSTEM_TEMPLATE.format(name=profile.name, length_hint=profile.length_hint)
    resp = client.messages.create(
        model=profile.model,
        max_tokens=profile.max_tokens,
        system=system,
        messages=[{"role": "user", "content": question}],
    )
    result = resp.content[0].text
    print(f"[{profile.name}] {resp.usage.output_tokens} tokens: {result[:120]}")
    return result

question = "What happened to server response time after the cache layer was added?"

for audience in ["executive", "developer", "customer", "slack"]:
    ask_for_audience(audience, question)
    print()
```

**Expected Token Savings:** Per-audience `max_tokens` caps prevent over-generation for terse audiences (executive, slack); using 64 tokens instead of 512 for an executive summary saves 85% of output cost on those calls.
**Environment:** Multi-channel agents serving the same content to different audiences (executive dashboard, developer docs, customer portal, Slack bot).

---

## Comparison

| Option | Length Control Method | Adaptive | Validated | Best For |
|---|---|---|---|---|
| 1. Word count in system | Explicit word range | No | No | Simple, consistent length per task type |
| 2. Structural hints | Item count + item length | No | No | Lists, comparisons, definitions |
| 3. Adaptive by input | Input-length-based | Yes | No | Summarisation agents with varied input length |
| 4. Assistant prefill | First-token anchoring | No | Implicitly | JSON, numbered lists, format-critical output |
| 5. Post-gen validation | Word count check + retry | Via retry | Yes | Hard character limits; product copy |
| 6. Audience profiles | Per-audience length + model | No | No | Multi-channel; different verbosity per audience |
