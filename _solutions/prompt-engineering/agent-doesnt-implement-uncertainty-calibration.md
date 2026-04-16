---
layout: solution
title: "Agent Doesn't Implement Uncertainty Calibration"
category: prompt-engineering
description: "Teach the agent to express calibrated confidence — saying 'I'm not certain' when appropriate, abstaining from unknowns, and hedging claims proportionally to actual knowledge — instead of confidently hallucinating."
tags: [prompt-engineering, uncertainty, calibration, hallucination, reliability, python]
---

# Agent Doesn't Implement Uncertainty Calibration

Uncalibrated agents express equal confidence whether answering "What is 2+2?" or fabricating a citation. Calibrated agents hedge appropriately, abstain when genuinely ignorant, and distinguish between "I know this" and "I believe this" — making them more trustworthy and less likely to mislead users.

## Option 1: Confidence Score via System Prompt

```python
import anthropic
import re

client = anthropic.Anthropic()

CALIBRATION_SYSTEM = """You are a calibrated assistant. For every response:
1. Answer the question as accurately as possible.
2. End with: CONFIDENCE: [HIGH|MEDIUM|LOW|ABSTAIN]

Use these definitions:
- HIGH: You are certain this is correct based on well-established facts.
- MEDIUM: You believe this is correct but cannot verify all details.
- LOW: You are guessing or the topic is outside your knowledge.
- ABSTAIN: You do not know and providing an answer would risk misinformation.

Never fabricate facts. When in doubt, say so."""

def calibrated_call(question: str) -> dict:
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=CALIBRATION_SYSTEM,
        messages=[{"role": "user", "content": question}],
    )
    text = resp.content[0].text
    # Extract confidence level
    match = re.search(r"CONFIDENCE:\s*(HIGH|MEDIUM|LOW|ABSTAIN)", text, re.IGNORECASE)
    confidence = match.group(1).upper() if match else "UNKNOWN"
    # Remove confidence tag from answer
    answer = re.sub(r"\nCONFIDENCE:.*$", "", text, flags=re.MULTILINE).strip()
    return {"question": question, "answer": answer, "confidence": confidence}

questions = [
    "What is the capital of France?",                          # HIGH expected
    "What is the population of Tokyo as of 2025?",            # MEDIUM
    "What did Aristotle say about neural networks?",           # ABSTAIN/LOW
    "Who won the 2027 World Cup?",                             # ABSTAIN
    "What is Python's GIL?",                                   # HIGH
]

for q in questions:
    r = calibrated_call(q)
    print(f"[{r['confidence']:7s}] {q[:50]}")
    print(f"         {r['answer'][:80]}\n")

# Expected Token Savings: ABSTAIN responses are shorter; LOW/ABSTAIN avoids follow-up correction loops
# Environment: any; CALIBRATION_SYSTEM prompt tunable to domain-specific knowledge boundaries
```

## Option 2: Structured Confidence with Reasoning

```python
import anthropic
import json
import re

client = anthropic.Anthropic()

STRUCTURED_CALIBRATION = """You are a calibrated assistant. Respond in this exact JSON format:
{
  "answer": "your answer here",
  "confidence": 0.0-1.0,
  "reasoning": "why you have this confidence level",
  "caveats": ["list", "of", "caveats"] or []
}

Confidence scale:
- 0.9-1.0: Well-established facts you are certain about
- 0.7-0.89: Likely correct but some uncertainty
- 0.5-0.69: Educated guess or partial knowledge
- 0.0-0.49: Significant uncertainty; abstain if below 0.3

If confidence < 0.3, set answer to "I don't have reliable information about this."
Respond ONLY with valid JSON."""

def structured_calibrated_call(question: str) -> dict:
    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=STRUCTURED_CALIBRATION,
        messages=[{"role": "user", "content": question}],
    )
    text = resp.content[0].text.strip()
    # Extract JSON even if surrounded by markdown
    json_match = re.search(r"\{.*\}", text, re.DOTALL)
    if json_match:
        try:
            return json.loads(json_match.group())
        except json.JSONDecodeError:
            pass
    return {"answer": text, "confidence": 0.5, "reasoning": "parse error", "caveats": []}

def safe_respond(question: str, min_confidence: float = 0.5) -> str:
    result = structured_calibrated_call(question)
    conf = result.get("confidence", 0.0)
    if conf < min_confidence:
        return (f"I'm not confident enough to answer this reliably "
                f"(confidence: {conf:.0%}). {result.get('reasoning', '')}")
    caveats = result.get("caveats", [])
    answer = result.get("answer", "")
    if caveats:
        answer += f"\n\nNote: {'; '.join(caveats)}"
    return f"[{conf:.0%} confident] {answer}"

questions = [
    "What is the boiling point of water at sea level?",
    "What is the exact GDP of France in 2024?",
    "Who will win the next US presidential election?",
    "What does the Python `asyncio.gather()` function do?",
]

for q in questions:
    print(f"Q: {q}")
    print(f"A: {safe_respond(q)}\n")

# Expected Token Savings: Low-confidence abstentions are shorter; prevents hallucinated long answers
# Environment: Sonnet for better calibration; tune min_confidence to your reliability requirements
```

## Option 3: Epistemic Tagging for Multi-Claim Responses

```python
import anthropic

client = anthropic.Anthropic()

EPISTEMIC_SYSTEM = """You are a calibrated assistant. Tag each factual claim with its epistemic status:

- [CERTAIN]: Established fact, definitively true
- [LIKELY]: Well-supported, probably true
- [UNCERTAIN]: Plausible but not verified
- [UNKNOWN]: You don't know; acknowledge this explicitly

Rules:
1. Never fabricate facts. If you don't know, say [UNKNOWN].
2. Tag every specific factual claim.
3. Keep answers complete and useful.

Example: "Python was created [CERTAIN] by Guido van Rossum [CERTAIN] around 1989 [LIKELY], originally inspired by ABC [CERTAIN]."
"""

def epistemically_tagged_call(question: str) -> dict:
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=EPISTEMIC_SYSTEM,
        messages=[{"role": "user", "content": question}],
    )
    text = resp.content[0].text
    # Count epistemic tags
    import re
    tags = re.findall(r"\[(CERTAIN|LIKELY|UNCERTAIN|UNKNOWN)\]", text)
    tag_counts = {t: tags.count(t) for t in set(tags)}
    # Flag if response has mostly uncertain/unknown claims
    total = len(tags)
    uncertain_count = tag_counts.get("UNCERTAIN", 0) + tag_counts.get("UNKNOWN", 0)
    reliability = 1.0 - (uncertain_count / total) if total > 0 else 1.0
    return {
        "text": text,
        "tag_counts": tag_counts,
        "reliability_score": round(reliability, 2),
        "total_claims": total,
    }

questions = [
    "When was Python created and who created it?",
    "What are the exact technical specifications of Claude Opus 4.6?",
    "Explain how the Python GIL works.",
]

for q in questions:
    print(f"Q: {q}")
    r = epistemically_tagged_call(q)
    print(f"Reliability: {r['reliability_score']:.0%} | Tags: {r['tag_counts']}")
    print(f"Answer: {r['text'][:200]}\n")

# Expected Token Savings: UNKNOWN tags replace fabricated paragraphs; epistemic density is measurable
# Environment: any; reliability_score provides programmatic signal for downstream filtering
```

## Option 4: Two-Stage Check — Answer Then Self-Verify

```python
import anthropic

client = anthropic.Anthropic()

def two_stage_calibrated(question: str) -> dict:
    # Stage 1: Generate answer
    answer_resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": question}],
    )
    draft_answer = answer_resp.content[0].text

    # Stage 2: Self-verify the answer
    verify_resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{"role": "user", "content":
            f"Question: {question}\n\nDraft answer: {draft_answer}\n\n"
            "Rate the reliability of this answer:\n"
            "1. Is every factual claim in this answer verifiable? (yes/no/partial)\n"
            "2. Confidence level: HIGH / MEDIUM / LOW / ABSTAIN\n"
            "3. Any corrections needed? (yes/no — if yes, state them briefly)\n"
            "Be critical. If unsure, say LOW."}],
    )
    verification = verify_resp.content[0].text.lower()

    # Parse verification signals
    import re
    confidence = "MEDIUM"
    for level in ["high", "medium", "low", "abstain"]:
        if level in verification:
            confidence = level.upper()
            break

    needs_correction = "yes" in verification and "correction" in verification

    return {
        "draft": draft_answer,
        "verification": verify_resp.content[0].text,
        "confidence": confidence,
        "needs_correction": needs_correction,
        "input_tokens": answer_resp.usage.input_tokens + verify_resp.usage.input_tokens,
        "output_tokens": answer_resp.usage.output_tokens + verify_resp.usage.output_tokens,
    }

def safe_answer(question: str) -> str:
    result = two_stage_calibrated(question)
    print(f"  Confidence: {result['confidence']} | Correction needed: {result['needs_correction']}")
    if result["confidence"] == "ABSTAIN":
        return "I don't have reliable information about this."
    if result["confidence"] == "LOW":
        return f"[Low confidence] {result['draft']}"
    return result["draft"]

questions = [
    "What is the speed of light?",
    "Who invented the internet and exactly when?",
    "What will the unemployment rate be in 2027?",
]

for q in questions:
    print(f"Q: {q}")
    answer = safe_answer(q)
    print(f"A: {answer[:120]}\n")

# Expected Token Savings: Self-verify catches hallucinations before delivery; ABSTAIN avoids long wrong answers
# Environment: 2 Haiku calls per turn; upgrade Stage 2 to Sonnet for critical applications
```

## Option 5: Domain-Specific Knowledge Boundary Enforcement

```python
import anthropic
import re

client = anthropic.Anthropic()

# Define domains the agent is authoritative on
AUTHORITATIVE_DOMAINS = [
    "Python programming",
    "software architecture",
    "asyncio and concurrency",
    "REST APIs",
    "databases",
    "machine learning fundamentals",
]

# Topics the agent should always abstain from
ABSTAIN_TOPICS = [
    "specific future events",
    "medical diagnoses",
    "legal advice",
    "real-time market prices",
    "proprietary company information",
]

def build_calibration_prompt(domains: list[str], abstain: list[str]) -> str:
    auth = "\n".join(f"- {d}" for d in domains)
    abs_topics = "\n".join(f"- {t}" for t in abstain)
    return f"""You are a calibrated assistant with specific expertise.

AUTHORITATIVE DOMAINS (answer with HIGH confidence):
{auth}

ABSTAIN TOPICS (always decline these):
{abs_topics}

For questions in your authoritative domains: answer fully and confidently.
For adjacent topics: answer with appropriate hedging ("I believe...", "typically...").
For abstain topics: clearly state you cannot reliably answer and why.
For unknown topics: say "I don't have reliable information about [X]."

Never fabricate. Calibrate your language to your actual confidence."""

SYSTEM = build_calibration_prompt(AUTHORITATIVE_DOMAINS, ABSTAIN_TOPICS)

def domain_aware_call(question: str) -> str:
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=SYSTEM,
        messages=[{"role": "user", "content": question}],
    )
    return resp.content[0].text

test_questions = [
    "How does Python asyncio event loop work?",           # authoritative
    "What is the current stock price of Apple?",          # abstain
    "Should I take ibuprofen for my headache?",           # abstain
    "What is the best database for a chat application?",  # authoritative
    "Who will win the Champions League next year?",       # abstain
]

for q in test_questions:
    print(f"Q: {q}")
    print(f"A: {domain_aware_call(q)[:120]}\n")

# Expected Token Savings: Abstain responses are short; prevents hallucinated long answers on unknowns
# Environment: customize AUTHORITATIVE_DOMAINS and ABSTAIN_TOPICS to your deployment context
```

## Option 6: Calibration Evaluation Harness

```python
import anthropic
import sqlite3
import time
import re

client = anthropic.Anthropic()
DB = "calibration_eval.db"

# Ground truth for evaluation
EVAL_SET = [
    {"question": "What is 15 * 8?",                    "answer": "120",      "category": "math"},
    {"question": "Capital of Germany?",                 "answer": "berlin",   "category": "geo"},
    {"question": "Who invented Python?",                "answer": "guido",    "category": "tech"},
    {"question": "What year was HTTP/2 finalized?",     "answer": "2015",     "category": "tech"},
    {"question": "Who won the 2099 World Cup?",         "answer": "ABSTAIN",  "category": "unknown"},
    {"question": "What is the exact mass of an electron?", "answer": "9.109", "category": "science"},
]

CALIBRATION_SYSTEM = """Answer questions. After each answer, rate your confidence:
CONFIDENCE: HIGH (certain) | MEDIUM (likely) | LOW (unsure) | ABSTAIN (don't know)
Format: [answer] CONFIDENCE: [level]"""

def init_db():
    con = sqlite3.connect(DB)
    con.execute("""
        CREATE TABLE IF NOT EXISTS eval_results (
            question TEXT, category TEXT,
            response TEXT, confidence TEXT,
            correct INTEGER, ts REAL
        )
    """)
    con.commit(); con.close()

def run_calibration_eval() -> dict:
    init_db()
    results = []
    for item in EVAL_SET:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=128,
            system=CALIBRATION_SYSTEM,
            messages=[{"role": "user", "content": item["question"]}],
        )
        text = resp.content[0].text
        match = re.search(r"CONFIDENCE:\s*(HIGH|MEDIUM|LOW|ABSTAIN)", text, re.I)
        confidence = match.group(1).upper() if match else "UNKNOWN"
        answer_text = re.sub(r"CONFIDENCE:.*$", "", text, flags=re.MULTILINE).strip()

        expected = item["answer"]
        if expected == "ABSTAIN":
            correct = confidence in ("LOW", "ABSTAIN")
        else:
            correct = expected.lower() in answer_text.lower()

        results.append({
            "question": item["question"],
            "category": item["category"],
            "response": answer_text[:100],
            "confidence": confidence,
            "correct": correct,
        })

        con = sqlite3.connect(DB)
        con.execute("INSERT INTO eval_results VALUES (?,?,?,?,?,?)",
                    (item["question"], item["category"], answer_text[:200],
                     confidence, int(correct), time.time()))
        con.commit(); con.close()

    # Calibration analysis: HIGH should correlate with correct
    high_correct = sum(1 for r in results if r["confidence"] == "HIGH" and r["correct"])
    high_total   = sum(1 for r in results if r["confidence"] == "HIGH")
    overall_acc  = sum(1 for r in results if r["correct"]) / len(results)

    return {
        "total": len(results),
        "overall_accuracy": round(overall_acc, 2),
        "high_confidence_precision": round(high_correct / high_total, 2) if high_total else 0,
        "confidence_distribution": {
            level: sum(1 for r in results if r["confidence"] == level)
            for level in ["HIGH", "MEDIUM", "LOW", "ABSTAIN", "UNKNOWN"]
        },
        "results": results,
    }

report = run_calibration_eval()
print(f"Overall accuracy: {report['overall_accuracy']:.0%}")
print(f"HIGH confidence precision: {report['high_confidence_precision']:.0%}")
print(f"Confidence distribution: {report['confidence_distribution']}")
print("\nPer-question results:")
for r in report["results"]:
    status = "✓" if r["correct"] else "✗"
    print(f"  {status} [{r['confidence']:7s}] {r['question'][:45]!r}")

# Expected Token Savings: Calibration eval identifies confidence mis-use; tune prompt to improve HIGH precision
# Environment: run periodically in CI; HIGH confidence precision should be > 90% for production
```

## Comparison

| Option | Confidence Format | Abstention Support | Measurable |
|--------|------------------|-------------------|-----------|
| 1 — System Prompt Tags | HIGH/MEDIUM/LOW/ABSTAIN | Yes | Via tag extraction |
| 2 — Structured JSON | 0.0–1.0 float | Yes (< threshold) | Yes, numeric |
| 3 — Epistemic Tagging | Per-claim tags | UNKNOWN tag | Reliability score |
| 4 — Two-Stage Verify | HIGH/MEDIUM/LOW/ABSTAIN | Yes | Correction flag |
| 5 — Domain Boundary | Domain-aware prose | Yes (topic-based) | Via human review |
| 6 — Calibration Eval | HIGH/MEDIUM/LOW/ABSTAIN | Yes | Accuracy + precision |
