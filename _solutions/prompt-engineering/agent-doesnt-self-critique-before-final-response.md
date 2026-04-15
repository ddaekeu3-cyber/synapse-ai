---
layout: solution
title: "Agent Doesn't Self-Critique Before Final Response"
category: prompt-engineering
description: "Agent returns the first plausible answer without reviewing it for errors, gaps, or incorrect assumptions — missing obvious mistakes that a single additional pass would catch."
tags: [prompt-engineering, quality, self-critique, reflection, accuracy]
---

## Symptom

An agent answers a complex multi-part question and returns immediately:

```
Q: "What are the tax implications of converting a traditional IRA to a Roth IRA in 2026,
    and how does this interact with the pro-rata rule?"

A: "Converting to a Roth IRA is straightforward. The converted amount is taxed as
    ordinary income in the year of conversion. You'll pay taxes on the full amount."
```

The answer misses the pro-rata rule entirely — the very thing the user asked about. A single self-review pass would have flagged the omission.

## Root Cause

The model generates a response that satisfies the surface pattern of the question without verifying completeness or correctness:

```python
import anthropic

client = anthropic.Anthropic(api_key="sk-live-...")

# Anti-pattern: single pass, no review
response = client.messages.create(
    model="claude-sonnet-4-6",
    max_tokens=500,
    messages=[{"role": "user", "content": "Explain the pro-rata rule for IRA conversions"}]
)
print(response.content[0].text)  # May miss key details on first pass
```

---

## Fix

### Option 1 — Two-pass: generate then critique in separate calls

Generate a draft, then make a second call asking the model to critique the draft before finalising.

```python
import anthropic

client = anthropic.Anthropic(api_key="sk-live-...")


def generate_with_critique(question: str, domain: str = "general") -> str:
    # Pass 1: Generate draft
    draft_response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=600,
        messages=[{"role": "user", "content": question}]
    )
    draft = draft_response.content[0].text.strip()

    # Pass 2: Critique the draft
    critique_response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=300,
        system=f"""You are a rigorous {domain} reviewer.
Review the following draft answer for:
1. Factual errors or incorrect claims
2. Missing key points the question explicitly asked about
3. Logical gaps or unsupported leaps
4. Misleading simplifications

List specific issues found. If the answer is complete and correct, say "No issues found."
Be concise — bullet points only.""",
        messages=[
            {"role": "user", "content": f"Question: {question}\n\nDraft answer:\n{draft}"}
        ]
    )
    critique = critique_response.content[0].text.strip()

    # Pass 3: Revise only if issues found
    if "no issues found" in critique.lower():
        return draft

    revised_response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=700,
        messages=[
            {"role": "user", "content": question},
            {"role": "assistant", "content": draft},
            {"role": "user", "content": f"Please revise your answer to address these issues:\n{critique}"}
        ]
    )
    return revised_response.content[0].text.strip()


answer = generate_with_critique(
    "What are the tax implications of converting a traditional IRA to a Roth IRA, "
    "including the pro-rata rule?",
    domain="tax and financial"
)
print(answer)

# Expected Token Savings: catching errors before delivery prevents clarification follow-ups (2-3 turns saved)
# Environment: high-stakes Q&A agents; legal, medical, financial, technical explanation tasks
```

---

### Option 2 — Single-call self-critique via assistant prefill

Use assistant prefill to make the model produce a draft and critique in one response, then extract the final answer.

```python
import anthropic
import re

client = anthropic.Anthropic(api_key="sk-live-...")


def generate_with_inline_critique(question: str) -> str:
    """
    Use assistant prefill to force the model to critique its own draft
    within a single API call, then return only the final answer.
    """
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1000,
        system="""When answering questions, follow this format exactly:

<draft>
[Your initial answer here]
</draft>

<critique>
[List any errors, missing points, or gaps in the draft. Be specific.]
</critique>

<final_answer>
[Revised answer addressing all critique points. If no critique, copy draft here.]
</final_answer>""",
        messages=[
            {"role": "user", "content": question},
            {"role": "assistant", "content": "<draft>"}  # Prefill starts the structured output
        ]
    )

    full_text = "<draft>" + response.content[0].text

    # Extract final answer
    match = re.search(r'<final_answer>(.*?)</final_answer>', full_text, re.DOTALL)
    if match:
        return match.group(1).strip()

    # Fallback: extract draft if structure wasn't followed
    draft_match = re.search(r'<draft>(.*?)</draft>', full_text, re.DOTALL)
    return draft_match.group(1).strip() if draft_match else full_text


answer = generate_with_inline_critique(
    "How does the carried interest loophole work and who benefits from it?"
)
print(answer)

# Expected Token Savings: single API call vs two — saves ~50% of critique-pass token cost
# Environment: agents where latency matters; medium-complexity questions needing light review
```

---

### Option 3 — Checklist-driven critique anchored to question requirements

Extract the explicit requirements from the question first, then check the draft against each requirement.

```python
import anthropic
import json

client = anthropic.Anthropic(api_key="sk-live-...")


def extract_requirements(question: str) -> list[str]:
    """Extract explicit answer requirements from the question."""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        system="Extract the specific things the question is asking for. Return a JSON array of strings.",
        messages=[{"role": "user", "content": f"Question: {question}\n\nRequirements (JSON array):"}]
    )
    raw = response.content[0].text.strip()
    try:
        if "```" in raw:
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return json.loads(raw.strip())
    except (json.JSONDecodeError, IndexError):
        return ["Answer the question completely and accurately"]


def check_requirement(draft: str, requirement: str) -> dict:
    """Check if a specific requirement is met in the draft."""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=100,
        system='Return JSON: {"met": true/false, "issue": "description or null"}',
        messages=[{
            "role": "user",
            "content": f"Requirement: {requirement}\n\nDraft answer:\n{draft[:1000]}\n\nIs this requirement met?"
        }]
    )
    raw = response.content[0].text.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"met": True, "issue": None}


def generate_checklist_validated(question: str) -> str:
    # Extract requirements
    requirements = extract_requirements(question)
    print(f"[checklist] Requirements: {requirements}")

    # Generate draft
    draft_resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=600,
        messages=[{"role": "user", "content": question}]
    )
    draft = draft_resp.content[0].text.strip()

    # Check each requirement
    issues = []
    for req in requirements:
        check = check_requirement(draft, req)
        if not check.get("met", True):
            issue = check.get("issue") or f"Requirement not met: {req}"
            issues.append(f"- {issue}")
            print(f"[checklist] FAIL: {req}")
        else:
            print(f"[checklist] PASS: {req}")

    if not issues:
        return draft

    # Revise to address gaps
    issues_text = "\n".join(issues)
    revised_resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=700,
        messages=[
            {"role": "user", "content": question},
            {"role": "assistant", "content": draft},
            {"role": "user", "content": f"Your answer is missing these points:\n{issues_text}\n\nPlease revise."}
        ]
    )
    return revised_resp.content[0].text.strip()


answer = generate_checklist_validated(
    "Explain both the advantages AND disadvantages of microservices architecture, "
    "with at least two concrete examples of each."
)
print(answer)

# Expected Token Savings: checklist catches omissions before user follow-up; targeted revision is cheaper than full redo
# Environment: structured Q&A; technical interviews; specification-heavy questions
```

---

### Option 4 — Confidence scoring: only critique low-confidence responses

Generate a confidence score with the draft. Only invoke the critique pass when confidence is below a threshold.

```python
import anthropic
import json

client = anthropic.Anthropic(api_key="sk-live-...")

CRITIQUE_THRESHOLD = 0.80  # Critique if confidence < 80%


def generate_with_confidence(question: str) -> tuple[str, float]:
    """Generate an answer with a self-assessed confidence score."""
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=700,
        system="""After answering, rate your confidence in the accuracy and completeness of the answer.
Return your response in this format:
<answer>
[Your answer here]
</answer>
<confidence>0.XX</confidence>
Where 0.XX is a number between 0.0 (no confidence) and 1.0 (certain).""",
        messages=[{"role": "user", "content": question}]
    )

    text = response.content[0].text.strip()

    import re
    answer_match = re.search(r'<answer>(.*?)</answer>', text, re.DOTALL)
    conf_match = re.search(r'<confidence>([\d.]+)</confidence>', text)

    answer = answer_match.group(1).strip() if answer_match else text
    confidence = float(conf_match.group(1)) if conf_match else 0.7

    return answer, confidence


def adaptive_critique(question: str) -> str:
    answer, confidence = generate_with_confidence(question)
    print(f"[confidence] Score: {confidence:.2f} (threshold: {CRITIQUE_THRESHOLD})")

    if confidence >= CRITIQUE_THRESHOLD:
        print("[confidence] High confidence — skipping critique pass")
        return answer

    print("[confidence] Low confidence — invoking critique pass")
    critique_resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=200,
        system="Identify specific errors or gaps in this answer. Bullet points only.",
        messages=[{"role": "user", "content": f"Q: {question}\n\nA: {answer}"}]
    )
    critique = critique_resp.content[0].text.strip()

    if "no issues" in critique.lower() or not critique.strip():
        return answer

    revised_resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=700,
        messages=[
            {"role": "user", "content": question},
            {"role": "assistant", "content": answer},
            {"role": "user", "content": f"Please fix these issues:\n{critique}"}
        ]
    )
    return revised_resp.content[0].text.strip()


# High-confidence question (likely skips critique)
print("=== Clear factual question ===")
print(adaptive_critique("What is the capital of France?"))
print()

# Low-confidence question (likely triggers critique)
print("=== Complex question ===")
print(adaptive_critique("What are the nuanced differences between GARCH and ARCH models for volatility forecasting?"))

# Expected Token Savings: critique runs only ~30% of the time → 70% of requests save a full pass
# Environment: mixed-complexity question answering; agents where token budget varies by question
```

---

### Option 5 — Peer review: two model instances independently critique

Generate a draft, then ask a second model instance to independently review it without seeing the first model's self-assessment.

```python
import anthropic
import asyncio

client = anthropic.AsyncAnthropic(api_key="sk-live-...")


async def generate_draft(question: str) -> str:
    response = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=600,
        messages=[{"role": "user", "content": question}]
    )
    return response.content[0].text.strip()


async def independent_review(question: str, draft: str) -> str:
    """Independent reviewer — no knowledge of who wrote the draft."""
    response = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=300,
        system="""You are a critical reviewer. You did NOT write this answer.
Review it objectively for:
- Factual inaccuracies
- Missing essential points
- Logical errors
- Misleading claims

Format: bullet list of issues, or "Approved: no significant issues" if clean.""",
        messages=[{
            "role": "user",
            "content": f"Question: {question}\n\nAnswer to review:\n{draft}"
        }]
    )
    return response.content[0].text.strip()


async def apply_review(question: str, draft: str, review: str) -> str:
    """Apply reviewer's feedback to produce final answer."""
    if review.lower().startswith("approved"):
        return draft

    response = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=700,
        messages=[
            {"role": "user", "content": question},
            {"role": "assistant", "content": draft},
            {"role": "user", "content": f"An independent reviewer flagged these issues:\n{review}\n\nPlease address them in a revised answer."}
        ]
    )
    return response.content[0].text.strip()


async def peer_reviewed_answer(question: str) -> str:
    # Generate draft and run independent review in parallel
    draft, review = await asyncio.gather(
        generate_draft(question),
        asyncio.coroutine(lambda: None)()  # Placeholder — review needs draft
    )

    # Review must happen after draft
    review = await independent_review(question, draft)
    print(f"[peer-review] {review[:100]}...")

    return await apply_review(question, draft, review)


result = asyncio.run(peer_reviewed_answer(
    "Explain the difference between correlation and causation with two concrete examples."
))
print(result)

# Expected Token Savings: independent review catches issues that self-review misses → fewer user corrections
# Environment: high-quality content generation; educational agents; fact-checked knowledge bases
```

---

### Option 6 — Constitutional critique: check against explicit quality principles

Define a set of quality principles and check each one explicitly before finalising the response.

```python
import anthropic

client = anthropic.Anthropic(api_key="sk-live-...")

QUALITY_PRINCIPLES = [
    "The answer directly addresses the question without unnecessary preamble.",
    "All specific claims (numbers, dates, names, laws) are accurate to the best of the model's knowledge.",
    "The answer is complete — it covers all parts of the question.",
    "The answer acknowledges uncertainty where it exists, rather than stating guesses as facts.",
    "The answer avoids harmful, biased, or misleading framings.",
]


def constitutional_critique_pass(question: str, draft: str) -> list[str]:
    """Check draft against each principle. Returns list of violated principles."""
    violations = []

    for principle in QUALITY_PRINCIPLES:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=80,
            system='Answer ONLY "yes" or "no: <brief reason>".',
            messages=[{
                "role": "user",
                "content": (
                    f"Principle: {principle}\n\n"
                    f"Question: {question}\n\n"
                    f"Answer: {draft[:800]}\n\n"
                    f"Does the answer satisfy this principle?"
                )
            }]
        )
        verdict = response.content[0].text.strip().lower()
        if not verdict.startswith("yes"):
            violations.append(f"• {principle}\n  Issue: {verdict}")

    return violations


def generate_constitutional(question: str) -> str:
    # Generate draft
    draft_resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=600,
        messages=[{"role": "user", "content": question}]
    )
    draft = draft_resp.content[0].text.strip()

    # Run constitutional check
    violations = constitutional_critique_pass(question, draft)

    if not violations:
        print("[constitutional] All principles satisfied")
        return draft

    print(f"[constitutional] {len(violations)} violation(s):")
    for v in violations:
        print(f"  {v[:100]}")

    # Revise
    violations_text = "\n".join(violations)
    revised_resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=700,
        system=f"""Revise your answer to satisfy these quality principles:
{violations_text}

Original question: {question}""",
        messages=[
            {"role": "user", "content": f"Original answer:\n{draft}\n\nRevised answer:"}
        ]
    )
    return revised_resp.content[0].text.strip()


answer = generate_constitutional(
    "Is intermittent fasting effective for weight loss? What does the research say?"
)
print(answer)

# Expected Token Savings: constitutional check costs ~5 Haiku calls; saves 1-2 sonnet correction turns
# Environment: health, legal, financial, educational agents requiring accuracy and epistemic honesty
```

---

## Comparison

| Option | Passes | Critique Type | Conditional | Complexity |
|--------|--------|--------------|-------------|------------|
| 1 | 2-3 | Free-form | No | Low |
| 2 | 1 (prefill) | Inline structured | No | Low |
| 3 | 3 | Checklist per requirement | No | Medium |
| 4 | 1-2 | Confidence-gated | Yes | Medium |
| 5 | 3 | Independent peer review | No | Medium |
| 6 | 2 | Constitutional principles | No | Medium |

**Recommended starting point:** Option 2 (single-call structured prefill) for most agents — one API call, low latency overhead, and the inline `<draft>/<critique>/<final_answer>` structure forces the model to catch obvious errors. Upgrade to Option 3 (checklist) for multi-part questions where omission is the primary failure mode, and Option 4 (confidence-gated) for mixed-complexity workloads where you want to avoid critique overhead on easy questions.
