---
layout: solution
title: "Agent Includes Redundant Few-Shot Examples in Every Message"
category: context-window
description: "Agent re-injects the same 3–5 few-shot examples into every user turn, consuming hundreds of tokens per call even though the model already learned the pattern from earlier in the session."
tags: [context-window, token-cost, few-shot, prompt-caching, prompt-engineering]
---

## Symptom

Each API call includes identical few-shot examples in the user message, even on turn 20 of a conversation:

```python
# Every single turn looks like this:
messages=[
    {"role": "user", "content": """
Examples:
Input: "customer angry about refund" → Label: URGENT
Input: "billing question" → Label: NORMAL
Input: "password reset" → Label: NORMAL
Input: "server is down" → Label: URGENT

Now classify: "my account was hacked"
"""}
]
```

The 4 examples cost ~60 tokens per turn. At 1,000 calls/day that's 60,000 wasted tokens daily — for examples the model already knows after the first call.

## Root Cause

The few-shot examples were hard-coded into the user message template rather than placed once in the system prompt or in the conversation history. When the agent was built, the developer tested single-turn scenarios where examples in the user message made sense, but failed to account for multi-turn cost accumulation.

---

## Fix

### Option 1 — Move examples to the system prompt (cached)

Put examples in the system prompt once. The model applies the pattern for the entire session without re-reading them each turn.

```python
import anthropic

client = anthropic.Anthropic(api_key="sk-live-...")

CLASSIFICATION_SYSTEM = """You are a ticket classification agent.

Classify each ticket as URGENT or NORMAL based on these examples:

<examples>
Input: "customer angry about refund" → URGENT
Input: "billing question" → NORMAL
Input: "password reset" → NORMAL
Input: "server is down" → URGENT
Input: "account hacked" → URGENT
Input: "change my email" → NORMAL
</examples>

Respond with only the label: URGENT or NORMAL"""

def classify(ticket: str) -> str:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=10,
        system=CLASSIFICATION_SYSTEM,
        messages=[{"role": "user", "content": ticket}]
    )
    return response.content[0].text.strip()

# Examples are in the system prompt — never repeated in user messages
labels = [classify(t) for t in ["my account was hacked", "invoice question"]]
print(labels)  # ["URGENT", "NORMAL"]

# Expected Token Savings: ~60 tokens × N calls per session
# Environment: any single-purpose classification or extraction agent
```

---

### Option 2 — Inject examples only on the first turn

Track whether the current session has already received examples. Include them only in turn 1; subsequent turns are bare user messages.

```python
import anthropic
from dataclasses import dataclass, field

client = anthropic.Anthropic(api_key="sk-live-...")

FEW_SHOT_EXAMPLES = """<examples>
Input: "customer angry about refund" → URGENT
Input: "billing question" → NORMAL
Input: "server is down" → URGENT
Input: "password reset" → NORMAL
</examples>

"""

@dataclass
class ClassificationSession:
    history: list = field(default_factory=list)
    examples_sent: bool = False

    def classify(self, ticket: str) -> str:
        # Only prepend examples on the very first call
        content = (FEW_SHOT_EXAMPLES if not self.examples_sent else "") + f"Classify: {ticket}"
        self.examples_sent = True

        self.history.append({"role": "user", "content": content})

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=10,
            system="You are a ticket classifier. Respond with URGENT or NORMAL only.",
            messages=self.history,
        )

        label = response.content[0].text.strip()
        self.history.append({"role": "assistant", "content": label})
        return label


session = ClassificationSession()
print(session.classify("account hacked"))        # Turn 1: includes examples
print(session.classify("billing question"))      # Turn 2+: no examples
print(session.classify("entire database gone"))  # Turn 3: no examples

# Expected Token Savings: (N-1) × example_tokens saved; e.g., 59 turns × 60 tokens = 3,540 tokens
# Environment: multi-turn conversation agents with consistent task pattern
```

---

### Option 3 — Prompt caching for the few-shot block

Use `cache_control` to cache the few-shot block once per cache TTL (5 min for ephemeral). After the first call the examples cost only 10% of their normal price.

```python
import anthropic

client = anthropic.Anthropic(api_key="sk-live-...")

# The few-shot block is a separate system content block marked for caching
CACHED_EXAMPLES_BLOCK = {
    "type": "text",
    "text": """Classification examples (apply these patterns to all tickets):

URGENT examples:
- "account was hacked" → URGENT
- "server is down" → URGENT
- "data breach detected" → URGENT
- "customer threatening legal action" → URGENT

NORMAL examples:
- "billing question" → NORMAL
- "password reset" → NORMAL
- "update my email address" → NORMAL
- "invoice copy request" → NORMAL
""",
    "cache_control": {"type": "ephemeral"},
}

INSTRUCTION_BLOCK = {
    "type": "text",
    "text": "Classify each ticket as URGENT or NORMAL. Respond with the label only.",
}


def classify(ticket: str) -> str:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=10,
        system=[CACHED_EXAMPLES_BLOCK, INSTRUCTION_BLOCK],
        messages=[{"role": "user", "content": ticket}],
        extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
    )
    return response.content[0].text.strip()


# First call: examples written to cache (~normal token cost)
# Subsequent calls within 5 min: examples read from cache at 10% cost
for ticket in ["my account was hacked", "change email", "site unreachable"]:
    print(classify(ticket))

# Expected Token Savings: 90% discount on example tokens after first call
# Environment: high-volume agents where same examples are reused within a 5-minute window
```

---

### Option 4 — Dynamic few-shot selection (only send relevant examples)

Instead of all examples every time, retrieve the 2 most similar examples from a pool. Smaller relevant examples beat larger irrelevant ones.

```python
import anthropic
import math
from typing import NamedTuple

client = anthropic.Anthropic(api_key="sk-live-...")

class Example(NamedTuple):
    input_text: str
    label: str


EXAMPLE_POOL = [
    Example("account was hacked", "URGENT"),
    Example("server is down", "URGENT"),
    Example("billing question", "NORMAL"),
    Example("password reset", "NORMAL"),
    Example("data breach", "URGENT"),
    Example("invoice copy", "NORMAL"),
    Example("legal threat from customer", "URGENT"),
    Example("update mailing address", "NORMAL"),
]


def simple_similarity(a: str, b: str) -> float:
    """Token overlap similarity — replace with embeddings in production."""
    a_tokens = set(a.lower().split())
    b_tokens = set(b.lower().split())
    if not a_tokens or not b_tokens:
        return 0.0
    intersection = a_tokens & b_tokens
    return len(intersection) / math.sqrt(len(a_tokens) * len(b_tokens))


def select_examples(ticket: str, k: int = 2) -> list[Example]:
    scored = [(simple_similarity(ticket, ex.input_text), ex) for ex in EXAMPLE_POOL]
    scored.sort(key=lambda x: -x[0])
    return [ex for _, ex in scored[:k]]


def classify(ticket: str) -> str:
    relevant = select_examples(ticket)
    examples_text = "\n".join(
        f'Input: "{ex.input_text}" → {ex.label}' for ex in relevant
    )

    prompt = f"""Examples:\n{examples_text}\n\nClassify: "{ticket}"\nRespond with URGENT or NORMAL only."""

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=10,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text.strip()


print(classify("my account was compromised"))   # selects hacked + breach examples
print(classify("need a copy of my invoice"))    # selects invoice + billing examples

# Expected Token Savings: 2 examples instead of 8 → 75% reduction in example tokens
# Environment: agents with large example pools where topic varies across requests
```

---

### Option 5 — Compress examples to a minimal schema

Replace verbose natural-language examples with a compact schema that conveys the same pattern in fewer tokens.

```python
import anthropic

client = anthropic.Anthropic(api_key="sk-live-...")

# Instead of 8 verbose examples (~120 tokens), use a rule-based schema (~40 tokens)
COMPACT_SCHEMA_SYSTEM = """Classify tickets as URGENT or NORMAL.

URGENT: security breach, data loss, system outage, legal threat, account compromise
NORMAL: billing, password reset, address change, documentation request, general question

Reply with the label only."""


def classify(ticket: str) -> str:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=10,
        system=COMPACT_SCHEMA_SYSTEM,
        messages=[{"role": "user", "content": ticket}],
    )
    return response.content[0].text.strip()


tickets = [
    "entire database was deleted",
    "can you send me a receipt",
    "we're getting ransomware alerts",
    "change my subscription plan",
]
for ticket in tickets:
    print(f"{ticket[:40]!r:45} → {classify(ticket)}")

# Expected Token Savings: 80 tokens saved vs 8 examples; accuracy equivalent for clear-cut categories
# Environment: well-defined classification tasks with distinct category boundaries
```

---

### Option 6 — Batch classify to amortize example cost

Send multiple tickets in a single API call. The example overhead is paid once, not once per ticket.

```python
import anthropic
import json

client = anthropic.Anthropic(api_key="sk-live-...")

BATCH_SYSTEM = """You are a ticket classifier. Classify tickets as URGENT or NORMAL.

Examples:
- "account hacked" → URGENT
- "billing question" → NORMAL
- "server down" → URGENT
- "password reset" → NORMAL

You will receive a JSON list of tickets. Return a JSON list of labels in the same order.
Respond with only valid JSON, no explanation."""


def classify_batch(tickets: list[str]) -> list[str]:
    payload = json.dumps(tickets)

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        system=BATCH_SYSTEM,
        messages=[{"role": "user", "content": payload}],
    )

    raw = response.content[0].text.strip()
    labels: list[str] = json.loads(raw)
    return labels


tickets = [
    "account was compromised",
    "need invoice copy",
    "entire site is unreachable",
    "update my email",
    "possible data breach",
]
labels = classify_batch(tickets)
for ticket, label in zip(tickets, labels):
    print(f"{label:6}  {ticket}")

# Expected Token Savings: 4 examples × 15 tokens × (N-1) calls avoided
#   e.g. 5 tickets in 1 call vs 5 calls: 4×15×4 = 240 tokens saved + 4× fewer API round-trips
# Environment: batch processing pipelines with buffered or queued ticket streams
```

---

## Comparison

| Option | Example Tokens Per Call | Implementation | Best For |
|--------|------------------------|----------------|----------|
| 1 | 0 (in system prompt once) | Trivial | Single-purpose agents |
| 2 | 0 after turn 1 | Simple | Multi-turn sessions |
| 3 | 10% after first call | Moderate | High-frequency within 5 min |
| 4 | 2 examples only | Moderate | Variable-topic requests |
| 5 | ~40 tokens (schema) | Simple | Well-defined categories |
| 6 | 0 per ticket (batched) | Moderate | Batch/pipeline workloads |

**Recommended starting point:** Option 1 — move examples to the system prompt. Zero code complexity, immediate token savings. Add Option 3 (caching) if the system prompt is long enough to benefit from the cache discount (minimum ~1,024 tokens for Haiku).
