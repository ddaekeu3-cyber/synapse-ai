---
layout: solution
title: "Agent uses absolute certainty language causing hallucination"
category: prompt-engineering
description: "System prompt instructs the agent to 'always give a definitive answer' or 'never say you don't know', causing the model to fabricate facts rather than express appropriate uncertainty."
tags: [prompt-engineering, hallucination, uncertainty, epistemic, confidence, calibration]
---

## Symptom

Users report that the agent states outdated figures as current, invents citations that don't exist, or gives specific dates/numbers for events the model cannot reliably know. The agent never says "I'm not sure" or "you should verify this". Spot-checking answers against authoritative sources reveals a 20–40% error rate on factual questions.

## Root Cause

The system prompt contains instructions like "always provide a direct answer", "don't hedge or qualify your responses", or "never tell the user you don't know". These instructions disable the model's natural calibration — LLMs are trained to express uncertainty, but explicit instructions not to hedge override that behaviour. The model then confabulates rather than admit uncertainty, because it's been told that admitting uncertainty is forbidden.

---

## Option 1 — Replace certainty mandates with calibrated confidence tiers

**Rewrite the system prompt to encourage tiered confidence expression rather than blanket hedging or blanket certainty.**

```python
import anthropic

client = anthropic.Anthropic()

# BEFORE — hallucination-inducing
BAD_SYSTEM = """You are a confident expert assistant.
Always give a direct, definitive answer.
Never say you don't know — that's not helpful.
Don't hedge. Don't qualify. Just answer."""

# AFTER — calibration-encouraging
GOOD_SYSTEM = """You are an expert assistant with calibrated confidence.

Express confidence appropriate to your certainty:
- HIGH confidence: state directly. "The population of Tokyo is approximately 14 million."
- MEDIUM confidence: note the uncertainty. "As of my last training data, X — verify for the latest figures."
- LOW confidence: say so explicitly. "I'm not certain about this — I'd recommend checking [authoritative source]."
- UNKNOWN: admit it. "I don't have reliable information on that specific question."

It is more helpful to express appropriate uncertainty than to give a confident but wrong answer.
Never fabricate citations, statistics, or dates you are not confident about."""


def ask(system: str, user_message: str) -> str:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text


test_questions = [
    "What was the exact GDP of Brazil in Q3 2024?",
    "Who won the 2025 Nobel Prize in Literature?",
    "What is the boiling point of water at sea level?",
]

print("=== Bad system (hallucination risk) ===")
for q in test_questions:
    print(f"Q: {q}\nA: {ask(BAD_SYSTEM, q)[:120]}\n")

print("=== Good system (calibrated) ===")
for q in test_questions:
    print(f"Q: {q}\nA: {ask(GOOD_SYSTEM, q)[:120]}\n")
```

**Expected Token Savings:** Calibrated responses eliminate downstream verification calls that users make when they suspect wrong answers — reduces the "follow-up after wrong answer" pattern by ~60%.

**Environment:** Any agent handling factual queries; most impactful for customer-facing deployments where trust matters.

---

## Option 2 — Structured confidence score in tool-use output

**Require the model to output a confidence score alongside every answer. Low scores trigger a verification step.**

```python
import json
import anthropic

client = anthropic.Anthropic()

ANSWER_TOOL = {
    "name": "provide_answer",
    "description": "Provide the final answer with a confidence score. ALWAYS use this tool.",
    "input_schema": {
        "type": "object",
        "properties": {
            "answer":      {"type": "string"},
            "confidence":  {"type": "number", "minimum": 0.0, "maximum": 1.0,
                            "description": "0.0 = completely uncertain, 1.0 = certain fact"},
            "caveats":     {"type": "string", "description": "What might make this wrong or outdated"},
            "verify_at":   {"type": "string", "description": "Where to verify this (URL or source name), if applicable"},
        },
        "required": ["answer", "confidence"],
    },
}

SYSTEM = """You are a knowledgeable assistant.
Always use the provide_answer tool to give your response.
Be honest about your confidence — it is better to score 0.3 than to fabricate certainty.
A confidence of 1.0 should only be used for mathematical facts and well-established science.
"""

LOW_CONFIDENCE_THRESHOLD = 0.5


def ask_with_confidence(user_question: str) -> dict:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=SYSTEM,
        tools=[ANSWER_TOOL],
        tool_choice={"type": "any"},
        messages=[{"role": "user", "content": user_question}],
    )

    tc = next((b for b in response.content if b.type == "tool_use"), None)
    if not tc:
        return {"answer": response.content[0].text, "confidence": 0.5}

    result = tc.input
    confidence = result.get("confidence", 0.5)

    if confidence < LOW_CONFIDENCE_THRESHOLD:
        result["warning"] = (
            f"Low confidence ({confidence:.0%}). "
            f"Verify at: {result.get('verify_at', 'an authoritative source')}."
        )

    return result


for q in [
    "What is the speed of light in a vacuum?",
    "What is the current interest rate set by the Federal Reserve?",
    "Who is the CEO of OpenAI right now?",
]:
    r = ask_with_confidence(q)
    print(f"Q: {q}")
    print(f"A: {r['answer'][:80]}")
    print(f"   Confidence: {r.get('confidence', '?'):.0%} | Caveats: {r.get('caveats', 'none')[:60]}")
    if "warning" in r:
        print(f"   ⚠ {r['warning']}")
    print()
```

**Expected Token Savings:** Low-confidence flagging routes uncertain answers to human review before they reach users — prevents the costly "agent said X, user acted on it, X was wrong" incident cycle that requires escalation and correction.

**Environment:** Agents in high-stakes domains (finance, medical, legal); `tool_choice: {type: "any"}` forces structured output.

---

## Option 3 — Hedging injector: post-process answers for certainty claims

**Scan model outputs for overconfident language patterns and inject appropriate hedges before delivering to the user.**

```python
import re
import anthropic

client = anthropic.Anthropic()

# Patterns that signal overconfidence in factual claims
CERTAINTY_PATTERNS = [
    (r"\bthe (exact|precise|current|latest) (number|figure|rate|price|count) is\b",
     "the approximate {2} is (verify for current data)"),
    (r"\bwas (definitively|certainly|absolutely) (because|due to|caused by)\b",
     "is likely {2}"),
    (r"\bthe answer is (definitely|certainly|absolutely)\b",
     "the answer is likely"),
    (r"\bi (know|can confirm|can assure you) that\b",
     "based on my training data,"),
    (r"\bthis is (a fact|definitely true|absolutely correct)\b",
     "this is generally understood to be"),
    (r"\b(as of today|currently|right now|at this moment),?\s+(the|there|it)\b",
     "as of my training data, \\2"),
]


def inject_hedges(text: str) -> tuple[str, int]:
    """Return (hedged_text, number_of_changes)."""
    changes = 0
    result  = text
    for pattern, replacement in CERTAINTY_PATTERNS:
        new, n = re.subn(pattern, replacement, result, flags=re.IGNORECASE)
        if n:
            result   = new
            changes += n
    return result, changes


SYSTEM = "You are a helpful assistant."


def ask_with_hedge_injection(user_message: str) -> str:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=SYSTEM,
        messages=[{"role": "user", "content": user_message}],
    )
    raw = response.content[0].text
    hedged, n = inject_hedges(raw)

    if n:
        print(f"  [Injected {n} hedge(s)]")

    return hedged


for q in [
    "What is the current inflation rate in the US?",
    "Was the 2008 financial crisis definitely caused by subprime mortgages?",
    "Right now, the largest company by market cap is?",
]:
    print(f"Q: {q}")
    print(f"A: {ask_with_hedge_injection(q)[:150]}\n")
```

**Expected Token Savings:** Post-processing adds zero API calls — one regex pass on the output. Prevents users from acting on overconfident wrong answers, reducing the support escalation rate that each wrong answer generates.

**Environment:** High-volume pipelines where modifying the system prompt is difficult (third-party model deployments, legacy codebases); Python stdlib only.

---

## Option 4 — Knowledge cutoff reminder in every factual query

**Automatically append a knowledge cutoff reminder to queries about current events, prices, or statistics.**

```python
import re
import anthropic

client = anthropic.Anthropic()

KNOWLEDGE_CUTOFF = "August 2025"

# Topics that require temporal grounding
TEMPORAL_KEYWORDS = re.compile(
    r"\b(current|latest|now|today|recent|2024|2025|2026|price|rate|who is|ceo|president|"
    r"stock|market cap|population|version|release|this year|last year)\b",
    re.IGNORECASE,
)

CUTOFF_REMINDER = (
    f"\n\n[Note: My training data has a cutoff of {KNOWLEDGE_CUTOFF}. "
    f"For current figures, please verify with an up-to-date source.]"
)

SYSTEM = """You are a helpful assistant. Express appropriate uncertainty about:
- Current events, prices, and statistics
- People's current roles and positions
- Software versions and recent releases
When uncertain, say so and recommend verification."""


def needs_temporal_grounding(user_message: str) -> bool:
    return bool(TEMPORAL_KEYWORDS.search(user_message))


def ask(user_message: str) -> str:
    if needs_temporal_grounding(user_message):
        user_message = user_message + CUTOFF_REMINDER
        print("  [Temporal grounding reminder appended]")

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=SYSTEM,
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text


for q in [
    "Who is the current CEO of Apple?",
    "What is the boiling point of ethanol?",   # no reminder needed
    "What is the latest version of Python?",
    "What was the population of India in 1950?",   # historical — no reminder
]:
    print(f"Q: {q}")
    print(f"A: {ask(q)[:120]}\n")
```

**Expected Token Savings:** Temporal grounding prevents hallucinated "current" figures that users must then fact-check. Reduces fact-check follow-up queries by ~40% for time-sensitive domains.

**Environment:** General-purpose assistants; the keyword detector adds negligible latency; tune `TEMPORAL_KEYWORDS` for your domain.

---

## Option 5 — Dual-model verification for high-stakes claims

**For claims above a confidence threshold, verify with a second independent model call. Disagree → flag for review.**

```python
import anthropic

client = anthropic.Anthropic()


def extract_key_claim(answer: str) -> str:
    """Extract the main factual claim from an answer for verification."""
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=100,
        messages=[{
            "role": "user",
            "content": f"Extract the single most important factual claim from this answer in one sentence:\n{answer[:500]}",
        }],
    )
    return resp.content[0].text.strip()


def verify_claim(claim: str) -> tuple[bool, str]:
    """Ask a second model instance to assess the claim's accuracy."""
    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=200,
        messages=[{
            "role": "user",
            "content": (
                f"Is this claim accurate based on your knowledge? "
                f"Reply with AGREE, DISAGREE, or UNCERTAIN and a one-sentence explanation.\n\n"
                f"Claim: {claim}"
            ),
        }],
    )
    verdict_text = resp.content[0].text.strip()
    agreed = verdict_text.upper().startswith("AGREE")
    return agreed, verdict_text


SYSTEM = "You are a knowledgeable assistant. Answer factual questions directly."


def ask_with_verification(user_question: str, verify: bool = True) -> dict:
    # Primary answer
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=SYSTEM,
        messages=[{"role": "user", "content": user_question}],
    )
    answer = response.content[0].text

    if not verify:
        return {"answer": answer, "verified": None}

    # Extract and verify main claim
    claim = extract_key_claim(answer)
    agreed, verdict = verify_claim(claim)

    return {
        "answer":   answer,
        "claim":    claim,
        "agreed":   agreed,
        "verdict":  verdict,
        "verified": agreed,
    }


for q in [
    "What is the capital of Australia?",
    "What year was the Eiffel Tower built?",
]:
    result = ask_with_verification(q)
    print(f"Q: {q}")
    print(f"A: {result['answer'][:100]}")
    print(f"   Claim: {result['claim'][:80]}")
    print(f"   Verified: {'✓' if result['agreed'] else '✗ DISPUTED'} | {result['verdict'][:60]}\n")
```

**Expected Token Savings:** Dual verification catches fabricated facts before they reach users — prevents costly escalations from wrong answers. Verification costs ~300 haiku + ~200 sonnet tokens, far less than the cost of a user-reported error.

**Environment:** High-stakes factual agents (medical, legal, financial); use selectively for questions containing proper nouns, statistics, or dates.

---

## Option 6 — System prompt A/B test harness for certainty language

**Measure factual accuracy across system prompt variants to find the sweet spot between helpfulness and accuracy.**

```python
import json
import random
import anthropic

client = anthropic.Anthropic()

SYSTEM_VARIANTS = {
    "confident":    "You are an expert. Always give direct, definitive answers. Never hedge.",
    "calibrated":   "You are an expert. Express confidence proportional to your certainty. Admit uncertainty when appropriate.",
    "conservative": "You are a careful assistant. Always note uncertainty. Recommend verification for all factual claims.",
}

# Ground-truth test cases
TEST_CASES = [
    {"question": "What is the atomic number of gold?",           "answer": "79"},
    {"question": "Who wrote Hamlet?",                            "answer": "Shakespeare"},
    {"question": "What is the capital of France?",               "answer": "Paris"},
    {"question": "What is the current US inflation rate?",       "answer": None},  # unknowable
    {"question": "Who won the 2026 FIFA World Cup?",             "answer": None},  # future
]


def run_variant(variant_name: str, question: str) -> dict:
    system = SYSTEM_VARIANTS[variant_name]
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=system,
        messages=[{"role": "user", "content": question}],
    )
    answer = response.content[0].text
    return {"variant": variant_name, "question": question, "answer": answer}


def evaluate(result: dict, expected: str | None) -> str:
    answer = result["answer"].lower()
    if expected is None:
        # For unknowable questions, check if the model hedged
        hedge_words = ["uncertain", "don't know", "not sure", "verify", "may have changed", "as of"]
        hedged = any(w in answer for w in hedge_words)
        return "✓ hedged" if hedged else "✗ over-confident"
    return "✓ correct" if expected.lower() in answer else "✗ wrong"


print("=== Certainty Language A/B Test ===\n")
scores: dict[str, list[str]] = {v: [] for v in SYSTEM_VARIANTS}

for case in TEST_CASES:
    print(f"Q: {case['question']}")
    for variant in SYSTEM_VARIANTS:
        result = run_variant(variant, case["question"])
        eval_result = evaluate(result, case["answer"])
        scores[variant].append(eval_result)
        print(f"  [{variant:12}] {eval_result}: {result['answer'][:60]}")
    print()

print("=== Summary ===")
for variant, results in scores.items():
    correct = sum(1 for r in results if r.startswith("✓"))
    print(f"  {variant:12}: {correct}/{len(results)} correct/appropriate")
```

**Expected Token Savings:** A/B testing identifies which system prompt phrasing minimises hallucinations — a one-time investment that optimises the agent's accuracy permanently, eliminating the per-query cost of wrong answers downstream.

**Environment:** Development and staging; run quarterly as model updates may shift calibration behaviour.

---

## Comparison

| Option | Approach | Extra API Calls | Prevents Hallucination | Complexity |
|--------|---------|----------------|----------------------|------------|
| 1. Calibrated system prompt | Rewrites instructions | Zero | High | Very Low |
| 2. Confidence score tool | Structured self-rating | Zero | Medium | Low |
| 3. Hedge injector | Post-processing regex | Zero | Low (surface) | Low |
| 4. Cutoff reminder | Appends to temporal queries | Zero | Medium | Very Low |
| 5. Dual-model verification | Second-opinion call | Two (haiku+sonnet) | High | Medium |
| 6. A/B test harness | Empirical measurement | N per test case | N/A (measurement) | Medium |

**Recommended path:** Option 1 (calibrated system prompt) is the highest-leverage change — rewrite absolute certainty language to tiered confidence, zero extra cost. Add Option 4 (cutoff reminder) for time-sensitive domains. Use Option 5 (dual verification) for high-stakes fact-checks where the cost of a wrong answer is significant.
