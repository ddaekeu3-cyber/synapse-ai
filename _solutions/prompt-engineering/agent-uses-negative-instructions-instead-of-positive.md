---
layout: solution
title: "Agent Uses Negative Instructions Instead of Positive"
category: prompt-engineering
description: "Agent system prompt is full of 'don't do X' rules instead of 'do Y' instructions — the model partially ignores prohibitions, especially under pressure or in edge cases, while positive specifications reliably shape behavior."
tags: [prompt-engineering, system-prompt, reliability, hallucination, format]
---

## Symptom

The system prompt is a list of prohibitions, but the agent still violates them:

```
System prompt:
  - Don't use bullet points
  - Don't say "I"
  - Don't include disclaimers
  - Don't use technical jargon
  - Don't mention competitors

Agent response: "I'd recommend using our platform instead of Competitor X.
• Feature A is great
• Feature B is also excellent
Note: This is not financial advice."
```

Every rule was violated despite explicit "don't" instructions.

## Root Cause

Language models process negative constraints differently from positive specifications. "Don't use bullet points" requires the model to:
1. Generate a candidate response
2. Detect bullet points in it
3. Regenerate without them

Under token pressure or with complex tasks, this self-correction loop breaks. The model reverts to its trained defaults. Positive instructions ("Use numbered lists only when listing 3+ items") give the model a concrete target to aim for — no self-correction needed.

Anti-pattern:
```python
import anthropic

client = anthropic.Anthropic(api_key="sk-live-...")

BAD_SYSTEM = """You are a sales assistant.
- Don't use bullet points
- Don't start sentences with "I"
- Don't add disclaimers
- Don't mention competitors
- Don't use jargon"""

# Model frequently violates these prohibitions
response = client.messages.create(
    model="claude-sonnet-4-6",
    max_tokens=256,
    system=BAD_SYSTEM,
    messages=[{"role": "user", "content": "What makes your product good?"}]
)
```

---

## Fix

### Option 1 — Rewrite prohibitions as positive specifications

Convert every "don't" rule into a concrete "do" instruction. Tell the model exactly what to produce.

```python
import anthropic

client = anthropic.Anthropic(api_key="sk-live-...")

# Before (negative):
# - Don't use bullet points
# - Don't say "I"
# - Don't add disclaimers
# - Don't mention competitors

# After (positive):
GOOD_SYSTEM = """You are a sales assistant for Acme Corp.

Output format:
- Write in flowing prose paragraphs only. Never use lists or bullets.
- Refer to yourself as "we" or "Acme" — never use first-person singular.
- End responses with a specific next action the customer can take.
- Describe features using plain language a non-technical buyer understands.
- When comparing options, compare against customer requirements, not named companies.
"""

response = client.messages.create(
    model="claude-sonnet-4-6",
    max_tokens=256,
    system=GOOD_SYSTEM,
    messages=[{"role": "user", "content": "What makes your product good?"}]
)
print(response.content[0].text)
# → "Acme's platform cuts your reporting time by 70% without requiring technical
#    setup. Our customers typically go live in under a week. Schedule a demo call
#    to see the dashboard tailored to your industry."

# Expected Token Savings: fewer follow-up correction turns; reduces retry cost
# Environment: any agent where response format and tone matter
```

---

### Option 2 — Specify the exact output structure

Instead of prohibiting formats, prescribe the exact structure. The model fills a template rather than avoiding a list of forbidden patterns.

```python
import anthropic

client = anthropic.Anthropic(api_key="sk-live-...")

STRUCTURED_SYSTEM = """You are a technical support agent.

For every response, use exactly this structure:

SITUATION: [one sentence restating the customer's issue in neutral terms]
CAUSE: [one sentence explaining why this happens]
SOLUTION: [2–4 numbered steps to fix it]
NEXT: [one sentence telling the customer what to do if the solution doesn't work]

Do not add any text outside these four sections."""

response = client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=300,
    system=STRUCTURED_SYSTEM,
    messages=[{"role": "user", "content": "My login keeps failing even with correct password."}]
)
print(response.content[0].text)
# → SITUATION: Your login is failing despite using the correct password.
#   CAUSE: This usually indicates a session cache conflict or account lock.
#   SOLUTION:
#   1. Clear browser cookies for this site.
#   2. Try an incognito/private window.
#   3. Wait 10 minutes if the account locked due to repeated attempts.
#   4. Use "Forgot password" to force a credential reset.
#   NEXT: Contact support@acme.com with your username if the issue persists.

# Expected Token Savings: templated output is shorter and requires no clarification turns
# Environment: support bots, FAQ agents, structured data extraction
```

---

### Option 3 — Positive persona definition with behavioral examples

Define what the agent IS and DOES, not what it isn't and doesn't. Reinforce with a positive few-shot example.

```python
import anthropic

client = anthropic.Anthropic(api_key="sk-live-...")

PERSONA_SYSTEM = """You are Alex, a concise customer success manager at Acme.

Alex's communication style:
- Writes short paragraphs (2–4 sentences each)
- Uses plain words (says "use" not "utilize", "help" not "facilitate")
- Focuses on the customer's outcome, not Acme's features
- Always ends with one specific action item
- Speaks warmly but efficiently — no filler phrases

Example of Alex's writing:
User: "Can your tool handle large files?"
Alex: "Yes — Acme handles files up to 10 GB with no slowdown. Most teams upload their full data library in under 5 minutes. Start a free trial to test with your actual files: acme.com/trial"

Now respond as Alex."""

response = client.messages.create(
    model="claude-sonnet-4-6",
    max_tokens=200,
    system=PERSONA_SYSTEM,
    messages=[{"role": "user", "content": "How does your pricing work?"}]
)
print(response.content[0].text)

# Expected Token Savings: consistent format reduces parsing failures; fewer correction turns
# Environment: customer-facing chatbots where brand voice consistency matters
```

---

### Option 4 — Positive constraint framing with XML sections

Use XML tags to separate positive behavioral rules from output format rules. Makes the system prompt scannable for the model and for humans maintaining it.

```python
import anthropic

client = anthropic.Anthropic(api_key="sk-live-...")

SYSTEM = """You are a financial summary agent.

<role>
Summarize quarterly earnings reports for retail investors with no finance background.
</role>

<tone>
Write like a knowledgeable friend explaining results over coffee.
Use plain numbers: say "$4 million" not "$4M" or "4,000,000".
Use active voice: "Revenue grew" not "Revenue was grown by".
</tone>

<format>
Start with a one-sentence verdict: good quarter, bad quarter, or mixed.
Follow with 3 specific numbers that support the verdict.
End with one forward-looking sentence from management guidance.
Total length: 4–6 sentences.
</format>

<scope>
Cover only: revenue, profit, and one operational highlight.
If guidance is missing from the report, say "Management gave no outlook."
</scope>"""

response = client.messages.create(
    model="claude-sonnet-4-6",
    max_tokens=200,
    system=SYSTEM,
    messages=[{"role": "user", "content": "Q3: Revenue $12.4M (+18% YoY), Net income $1.1M (+5% YoY), opened 3 new stores, guidance raised to $50M FY revenue."}]
)
print(response.content[0].text)

# Expected Token Savings: structured sections reduce ambiguity → fewer tokens resolving unclear instructions
# Environment: document processing agents where output consistency is critical
```

---

### Option 5 — Positive instruction audit: automated check before deploy

Build a pre-deploy linter that flags negative instructions in system prompts and suggests positive rewrites using the model itself.

```python
import anthropic
import re

client = anthropic.Anthropic(api_key="sk-live-...")

NEGATIVE_PATTERNS = [
    r"\bdon'?t\b",
    r"\bdo not\b",
    r"\bnever\b",
    r"\bavoid\b",
    r"\brefrain from\b",
    r"\bwithout\b.{0,30}\b(using|saying|adding|including)\b",
]


def count_negative_instructions(system_prompt: str) -> list[str]:
    """Return lines containing negative instructions."""
    flagged = []
    for line in system_prompt.splitlines():
        for pattern in NEGATIVE_PATTERNS:
            if re.search(pattern, line, re.IGNORECASE):
                flagged.append(line.strip())
                break
    return flagged


def rewrite_to_positive(negative_instruction: str) -> str:
    """Ask the model to convert a negative rule to a positive specification."""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=100,
        messages=[{
            "role": "user",
            "content": f"""Convert this negative instruction to a positive specification.
Negative: {negative_instruction}
Positive (one sentence, tells the model exactly what to do instead):"""
        }]
    )
    return response.content[0].text.strip()


def audit_system_prompt(prompt: str) -> None:
    flagged = count_negative_instructions(prompt)
    if not flagged:
        print("OK: No negative instructions found.")
        return

    print(f"Found {len(flagged)} negative instruction(s):\n")
    for line in flagged:
        rewritten = rewrite_to_positive(line)
        print(f"  NEGATIVE: {line}")
        print(f"  POSITIVE: {rewritten}\n")


# Example audit
bad_prompt = """You are a customer support agent.
- Don't use bullet points
- Never say you don't know
- Avoid mentioning refund policies
- Don't use technical terms
"""
audit_system_prompt(bad_prompt)
# Output:
#   NEGATIVE: Don't use bullet points
#   POSITIVE: Write all responses in flowing prose paragraphs.
#
#   NEGATIVE: Never say you don't know
#   POSITIVE: When uncertain, say "Let me check on that" and offer to escalate.
#   ...

# Expected Token Savings: prompt quality improvement reduces clarification turns upstream
# Environment: CI/CD pipeline step before deploying new system prompts to production
```

---

### Option 6 — Positive + negative hybrid with priority ordering

When some negative constraints are unavoidable (legal, compliance), place positive rules first and negative rules last, with explicit priority ordering.

```python
import anthropic

client = anthropic.Anthropic(api_key="sk-live-...")

# Hybrid: positive rules first (model internalises these), negative only for hard constraints
HYBRID_SYSTEM = """You are a medical information assistant.

ALWAYS DO (primary behavior — follow these for every response):
1. Write responses as clear numbered steps when explaining a process.
2. Cite the source of any medical claim: "According to [source]..."
3. Recommend consulting a licensed physician for personal health decisions.
4. Use the patient's exact symptom words back to them before explaining.
5. End each response with: "Does this help clarify things?"

HARD LIMITS (legal requirements — these override everything above):
- Never diagnose a specific condition by name.
- Never recommend a specific medication dosage.
- Never contradict advice from a named physician the user has mentioned.
"""

response = client.messages.create(
    model="claude-sonnet-4-6",
    max_tokens=300,
    system=HYBRID_SYSTEM,
    messages=[{"role": "user", "content": "I have chest pain when I breathe deeply. Should I be worried?"}]
)
print(response.content[0].text)

# Expected Token Savings: fewer edge-case failures where negative rules alone would be violated
# Environment: regulated domains (medical, legal, financial) where compliance is non-negotiable
```

---

## Comparison

| Option | Approach | Handles Edge Cases | Maintainability | Best For |
|--------|----------|--------------------|-----------------|----------|
| 1 | Rewrite negatives as positives | Good | High | General-purpose agents |
| 2 | Prescribe exact output structure | Excellent | High | Structured output agents |
| 3 | Positive persona + example | Good | Medium | Brand-voice chatbots |
| 4 | XML-sectioned positive rules | Excellent | High | Complex multi-rule agents |
| 5 | Automated audit + rewrite tool | Good | High | CI/CD prompt validation |
| 6 | Positive-first hybrid | Best | Medium | Regulated/compliance domains |

**Recommended starting point:** Option 1 for any existing system prompt — audit each "don't" and rewrite it as a concrete "do". Option 2 when the output format is the main concern. Option 6 when legal or compliance constraints make some negative rules unavoidable.
