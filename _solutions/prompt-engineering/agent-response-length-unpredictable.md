---
layout: solution
title: "Agent Response Length Is Unpredictable — Too Long or Too Short"
category: prompt-engineering
description: "Agent writes a three-sentence summary when asked for a one-liner, or gives a one-word answer when a detailed explanation is expected. Response length varies wildly across similar requests with no consistent behavior."
tags: [response-length, verbosity, conciseness, prompt-engineering, output-control, format]
---

# Agent Response Length Is Unpredictable — Too Long or Too Short

## Symptom
- Asked for a summary, agent returns a 2,000-word essay
- Asked for a detailed analysis, agent returns two sentences
- API endpoint returns sometimes 50 tokens, sometimes 2,000 for the same query type
- Agent adds excessive caveats, disclaimers, and preamble to every response
- UI truncates agent responses because they're consistently too long for the display area
- Downstream parser fails because response length varies outside expected bounds

## Root Cause
LLMs have no inherent sense of "appropriate" response length without explicit guidance. Length is influenced by: training data distribution, phrasing of the request, presence of examples, and system prompt style. Without length constraints, models default to thoroughness — which means verbose responses. Without encouragement, they may under-explain.

## Fix

### Option 1: Explicit length instruction in system prompt

```python
# Length ranges that work well in practice
LENGTH_PRESETS = {
    "one_liner": "Respond in exactly 1 sentence. No preamble. No caveats.",
    "brief": "Respond in 2-3 sentences. Be direct. No preamble.",
    "short": "Respond in 100-150 words. Paragraphs only if needed.",
    "medium": "Respond in 200-400 words. Include key details.",
    "detailed": "Respond in 500-800 words. Cover all relevant aspects.",
    "comprehensive": "Respond comprehensively. Use headers and sections. No artificial length limit.",
}

def build_system_prompt(base_prompt: str, length_preset: str) -> str:
    length_instruction = LENGTH_PRESETS.get(length_preset, "")
    if not length_instruction:
        return base_prompt
    return f"{base_prompt}\n\nResponse length: {length_instruction}"

# For a summarization agent:
system = build_system_prompt(
    "You are a document summarizer.",
    length_preset="brief"
)
# → "You are a document summarizer.\n\nResponse length: Respond in 2-3 sentences. Be direct."
```

### Option 2: Dynamic length instruction based on query type

```python
import re

def infer_desired_length(user_message: str) -> str:
    """
    Infer the appropriate response length from the user's request phrasing.
    """
    msg = user_message.lower()

    # Explicit short signals
    if any(p in msg for p in ["one line", "one sentence", "briefly", "in short", "tldr", "tl;dr"]):
        return "Respond in exactly 1 sentence."

    # Explicit long signals
    if any(p in msg for p in ["in detail", "comprehensive", "explain fully", "deep dive", "thorough"]):
        return "Respond comprehensively with full detail."

    # Question types
    if re.search(r"^(what is|who is|when did|where is)", msg):
        return "Respond in 1-2 sentences with just the factual answer."

    if re.search(r"^(how do|how does|why does|explain|describe)", msg):
        return "Respond in 150-300 words with a clear explanation."

    if re.search(r"^(compare|contrast|analyze|evaluate)", msg):
        return "Respond in 300-500 words covering all relevant dimensions."

    # Default: medium
    return "Respond concisely — use as few words as needed to fully answer."

async def chat(user_message: str, client) -> str:
    length_hint = infer_desired_length(user_message)
    response = await client.messages.create(
        model="claude-sonnet-4-6",
        system=f"You are a helpful assistant. {length_hint}",
        messages=[{"role": "user", "content": user_message}],
        max_tokens=1024,
    )
    return response.content[0].text
```

### Option 3: Token budget enforcement via max_tokens

```python
# Use max_tokens to hard-cap response length by use case
MAX_TOKENS_BY_USE_CASE = {
    "classification":   20,    # "positive" / "negative" / "neutral"
    "yes_no":           10,    # "yes" or "no" plus brief reason
    "summary":         150,    # Short paragraph
    "explanation":     400,    # Medium explanation
    "analysis":        800,    # Full analysis
    "report":         2000,    # Comprehensive report
    "code_generation": 4096,   # Code can be long
}

async def call_with_token_budget(
    messages: list,
    use_case: str,
    client,
    system: str = ""
) -> str:
    max_tokens = MAX_TOKENS_BY_USE_CASE.get(use_case, 512)

    response = await client.messages.create(
        model="claude-sonnet-4-6",
        system=system,
        messages=messages,
        max_tokens=max_tokens,
    )

    # Warn if response hit the limit (may be truncated)
    if response.stop_reason == "max_tokens":
        print(f"Warning: Response for '{use_case}' hit max_tokens={max_tokens} — may be truncated")

    return response.content[0].text

# Usage:
result = await call_with_token_budget(
    messages=[{"role": "user", "content": "Is this email spam? [email text]"}],
    use_case="yes_no",
    system="Classify the email as spam or not spam."
)
# → "No. The email is a legitimate newsletter from a subscribed source."
```

### Option 4: Few-shot examples that demonstrate correct length

```python
# Show the model exactly how long responses should be by example
SHORT_SUMMARY_EXAMPLES = [
    {
        "role": "user",
        "content": "Summarize: [long article about climate change]"
    },
    {
        "role": "assistant",
        "content": "Global temperatures rose 1.1°C above pre-industrial levels, accelerating extreme weather events and requiring urgent emissions cuts to limit warming to 1.5°C."
    },
    {
        "role": "user",
        "content": "Summarize: [long article about machine learning]"
    },
    {
        "role": "assistant",
        "content": "Machine learning models learn patterns from data to make predictions, with deep neural networks achieving human-level performance in vision and language tasks."
    },
]

async def summarize_with_examples(text: str, client) -> str:
    messages = SHORT_SUMMARY_EXAMPLES + [
        {"role": "user", "content": f"Summarize: {text}"}
    ]
    response = await client.messages.create(
        model="claude-sonnet-4-6",
        system="You are a summarizer. Match the length of the examples above — one sentence only.",
        messages=messages,
        max_tokens=100,
    )
    return response.content[0].text
```

### Option 5: Post-process to enforce length contract

```python
def enforce_length_contract(
    response: str,
    max_sentences: int = None,
    max_words: int = None,
    max_chars: int = None,
) -> str:
    """
    Truncate response to meet length constraints.
    Use as a last resort — better to get the right length from the model.
    """
    if max_sentences:
        import re
        sentences = re.split(r'(?<=[.!?])\s+', response.strip())
        if len(sentences) > max_sentences:
            response = " ".join(sentences[:max_sentences])
            if not response.endswith((".", "!", "?")):
                response += "."

    if max_words:
        words = response.split()
        if len(words) > max_words:
            response = " ".join(words[:max_words])
            # Find last complete sentence
            last_sentence_end = max(
                response.rfind("."), response.rfind("!"), response.rfind("?")
            )
            if last_sentence_end > len(response) * 0.5:
                response = response[:last_sentence_end + 1]
            else:
                response = response + "..."

    if max_chars and len(response) > max_chars:
        response = response[:max_chars].rsplit(" ", 1)[0] + "..."

    return response

# Usage:
raw = "This is a very long response that goes on and on..."
truncated = enforce_length_contract(raw, max_sentences=2, max_words=50)
```

### Option 6: Structured output with explicit field sizes

```python
from pydantic import BaseModel, Field

class SummaryResponse(BaseModel):
    headline: str = Field(..., max_length=100, description="One sentence, under 100 chars")
    key_points: list[str] = Field(..., max_items=3, description="Exactly 3 bullet points")
    recommendation: str = Field(..., max_length=200, description="Action to take, under 200 chars")

async def structured_summary(text: str, client) -> SummaryResponse:
    """
    Use structured output to enforce exact response shape and size.
    Model fills fields — each field has explicit size constraints.
    """
    import json

    response = await client.messages.create(
        model="claude-sonnet-4-6",
        system=(
            "Return a JSON object with exactly these fields:\n"
            "- headline: one sentence under 100 characters\n"
            "- key_points: exactly 3 items, each under 80 characters\n"
            "- recommendation: one actionable sentence under 200 characters\n"
            "No other text. JSON only."
        ),
        messages=[{"role": "user", "content": f"Summarize this:\n\n{text}"}],
        max_tokens=400,
    )

    data = json.loads(response.content[0].text)
    return SummaryResponse(**data)

# Result is always exactly the right shape and length
```

## Length Control Strategies

| Strategy | Precision | Effort | Best for |
|---|---|---|---|
| System prompt instruction | Medium | Low | General use |
| Dynamic length inference | Medium | Medium | Chatbots with varied queries |
| max_tokens hard cap | Hard cap only | Low | Classification, yes/no |
| Few-shot examples | High | Medium | Consistent format needed |
| Post-processing truncation | Exact | Low | Safety net for UI display |
| Structured output schema | Exact | High | API responses, dashboards |

## Expected Token Savings
Verbose responses at 5× intended length for 1,000 queries: ~40,000 extra tokens
Length instruction reduces average response to target: 80% token reduction

## Environment
- All agent deployments; most impactful for high-volume APIs and chat interfaces with display constraints
- Source: direct experience; length unpredictability is the top UX complaint in agent-powered products
